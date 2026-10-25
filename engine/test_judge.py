"""Tests for engine/db.py, engine/judge.py and engine/backends.py.

    python engine/test_judge.py

No API key, no CLI, no network: the judge takes its backend as an argument, so
these pass fakes that return canned responses. That covers the plumbing and the
fail-safe paths. It does NOT check that the real model rules correctly on the
three planted PRs -- that needs a live call.

stdlib unittest, not pytest, so the engine's own tests add no dependency.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest

# Point db at a scratch file before importing anything that opens it.
_TMP = tempfile.mkdtemp(prefix="pr-guardian-test-")
os.environ["PR_GUARDIAN_DB"] = os.path.join(_TMP, "test.db")

import backends  # noqa: E402
import db  # noqa: E402
import judge as judge_mod  # noqa: E402
from backends import BackendError, CliBackend  # noqa: E402
from judge import Verdict, build_user_content, judge  # noqa: E402


class FakeBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class FakeResponse:
    def __init__(self, payload=None, stop_reason="end_turn", blocks=None):
        self.stop_reason = stop_reason
        if blocks is not None:
            self.content = blocks
        else:
            self.content = [FakeBlock(json.dumps(payload))]


class FakeMessages:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


class FakeClient:
    def __init__(self, response=None, error=None):
        self.messages = FakeMessages(response, error)


DIFF = {
    "pr_number": 101,
    "repo": "chezzz439-arch/frosty",
    "branch": "planted/violation",
    "files": ["checkout/payments.py"],
    "diff_text": "+    logger.info('Card used: %s', card.number)",
}


def reset_db():
    """Drop every row between tests."""
    with db.connect() as conn:
        for table in ("PR_DIFFS", "VERDICTS", "RECEIPTS", "PR_EVENTS"):
            conn.execute(f"DELETE FROM {table}")


class TestDb(unittest.TestCase):
    def setUp(self):
        reset_db()

    def test_read_diff_round_trips_files_as_a_list(self):
        db.insert_diff(101, "o/r", "main", ["a.py", "b.py"], "some diff")
        diff = db.read_diff(101)
        self.assertEqual(diff["files"], ["a.py", "b.py"])
        self.assertEqual(diff["diff_text"], "some diff")
        self.assertEqual(diff["repo"], "o/r")

    def test_read_diff_returns_none_when_trigger_has_not_written(self):
        self.assertIsNone(db.read_diff(999))

    def test_read_diff_returns_the_newest_row(self):
        db.insert_diff(101, "o/r", "main", ["old.py"], "first push")
        db.insert_diff(101, "o/r", "main", ["new.py"], "second push")
        self.assertEqual(db.read_diff(101)["diff_text"], "second push")

    def test_write_verdict_round_trips_fixed_code_and_bool(self):
        db.write_verdict(
            101, "fix", 0.91, "logs a card number",
            fixed_code=[{"path": "a.py", "content": "clean"}],
            tests_passed=True,
            attempts=2,
        )
        verdict = db.verdicts(101)[0]
        self.assertEqual(verdict["decision"], "fix")
        self.assertAlmostEqual(verdict["confidence"], 0.91)
        self.assertEqual(verdict["fixed_code"], [{"path": "a.py", "content": "clean"}])
        self.assertIs(verdict["tests_passed"], True)
        self.assertEqual(verdict["attempts"], 2)

    def test_write_verdict_keeps_tests_passed_null_distinct_from_false(self):
        db.write_verdict(101, "fix", 0.9, "r", tests_passed=None)
        self.assertIsNone(db.verdicts(101)[0]["tests_passed"])
        reset_db()
        db.write_verdict(101, "fix", 0.9, "r", tests_passed=False)
        self.assertIs(db.verdicts(101)[0]["tests_passed"], False)

    def test_write_verdict_rejects_an_off_contract_decision(self):
        # /merge gates on exactly fix/allow/escalate; anything else must fail loudly.
        for bad in ("FIX", "reject", "needs-human", ""):
            with self.assertRaises(ValueError):
                db.write_verdict(101, bad, 0.5, "r")

    def test_log_event_and_read_back_in_order(self):
        db.log_event(101, "diff read", "ok", "1 file")
        db.log_event(101, "decision made", "ok", "fix")
        steps = [event["step"] for event in db.events(101)]
        self.assertEqual(steps, ["diff read", "decision made"])

    def test_recent_receipts_is_newest_first_and_bools_are_bools(self):
        db.insert_receipt(98, "allow", "docs sample", True)
        db.insert_receipt(99, "escalate", "unclear", False)
        receipts = db.recent_receipts(limit=10)
        self.assertEqual([r["pr_number"] for r in receipts], [99, 98])
        self.assertIs(receipts[0]["merged"], False)
        self.assertIs(receipts[1]["merged"], True)

    def test_recent_receipts_is_empty_before_merge_lands(self):
        self.assertEqual(db.recent_receipts(), [])

    def test_malformed_json_column_does_not_crash_the_engine(self):
        # A hand-inserted fake row might hold a bare string, not JSON.
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO PR_DIFFS (pr_number, repo, branch, files, diff_text) "
                "VALUES (?, ?, ?, ?, ?)",
                (101, "o/r", "main", "not json at all", "d"),
            )
        self.assertEqual(db.read_diff(101)["files"], [])


class TestPrompt(unittest.TestCase):
    def test_user_content_carries_rules_diff_and_file_list(self):
        content = build_user_content(DIFF, "RULE ONE", [])
        self.assertIn("RULE ONE", content)
        self.assertIn("checkout/payments.py", content)
        self.assertIn("card.number", content)
        self.assertIn("#101", content)

    def test_rules_come_before_the_diff(self):
        # The model should read the standard before the thing being judged.
        content = build_user_content(DIFF, "RULE ONE", [])
        self.assertLess(content.index("RULE ONE"), content.index("```diff"))

    def test_memory_is_included_when_receipts_exist(self):
        content = build_user_content(
            DIFF,
            "RULE",
            [{"pr_number": 98, "decision": "allow", "reason": "docs sample"}],
        )
        self.assertIn("PR #98", content)
        self.assertIn("docs sample", content)

    def test_no_memory_section_when_there_are_no_receipts(self):
        self.assertNotIn("earlier PRs", build_user_content(DIFF, "RULE", []))

    def test_long_reasons_are_truncated_so_memory_cannot_crowd_out_the_diff(self):
        content = build_user_content(
            DIFF, "RULE", [{"pr_number": 1, "decision": "fix", "reason": "x" * 5000}]
        )
        self.assertIn("...", content)
        self.assertLess(len(content), 2000)

    def test_load_rules_falls_back_when_demo_data_is_missing(self):
        rules = judge_mod.load_rules(os.path.join(_TMP, "nope.md"))
        self.assertEqual(rules, judge_mod.FALLBACK_RULES)

    def test_load_rules_reads_the_file_when_it_exists(self):
        path = os.path.join(_TMP, "rules.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("1. No secrets in logs.\n")
        self.assertEqual(judge_mod.load_rules(path), "1. No secrets in logs.")


class TestJudge(unittest.TestCase):
    def test_parses_a_fix_verdict(self):
        client = FakeClient(FakeResponse({
            "decision": "fix",
            "confidence": 0.93,
            "reason": "payments.py logs card.number on every charge.",
            "fixed_code": [{"path": "checkout/payments.py", "content": "clean file"}],
        }))
        verdict = judge(DIFF, client=client, rules="RULE")
        self.assertEqual(verdict.decision, "fix")
        self.assertAlmostEqual(verdict.confidence, 0.93)
        self.assertTrue(verdict.is_fix)
        self.assertEqual(verdict.changed_paths, ["checkout/payments.py"])

    def test_parses_an_allow_verdict(self):
        client = FakeClient(FakeResponse({
            "decision": "allow",
            "confidence": 0.88,
            "reason": "Test fixture, not production code.",
            "fixed_code": [],
        }))
        verdict = judge(DIFF, client=client, rules="RULE")
        self.assertEqual(verdict.decision, "allow")
        self.assertFalse(verdict.is_fix)

    def test_sends_the_schema_and_the_adaptive_thinking_params(self):
        client = FakeClient(FakeResponse({
            "decision": "allow", "confidence": 0.5, "reason": "r", "fixed_code": [],
        }))
        judge(DIFF, client=client, rules="RULE")
        call = client.messages.calls[0]
        self.assertEqual(call["thinking"], {"type": "adaptive"})
        self.assertEqual(
            call["output_config"]["format"]["schema"], judge_mod.VERDICT_SCHEMA
        )
        self.assertEqual(call["output_config"]["format"]["type"], "json_schema")
        # Opus 4.8 rejects temperature/top_p/top_k and budget_tokens.
        for removed in ("temperature", "top_p", "top_k"):
            self.assertNotIn(removed, call)
        self.assertNotIn("budget_tokens", call["thinking"])

    def test_schema_allows_only_the_three_decisions(self):
        enum = judge_mod.VERDICT_SCHEMA["properties"]["decision"]["enum"]
        self.assertEqual(sorted(enum), ["allow", "escalate", "fix"])

    # --- fail-safe paths: every failure has to become an escalation ---

    def test_api_error_escalates_rather_than_raising(self):
        client = FakeClient(error=RuntimeError("connection reset"))
        verdict = judge(DIFF, client=client, rules="RULE")
        self.assertEqual(verdict.decision, "escalate")
        self.assertEqual(verdict.confidence, 0.0)
        self.assertIn("connection reset", verdict.reason)

    def test_refusal_escalates(self):
        client = FakeClient(FakeResponse({}, stop_reason="refusal"))
        self.assertEqual(judge(DIFF, client=client, rules="RULE").decision, "escalate")

    def test_truncated_response_escalates(self):
        client = FakeClient(FakeResponse(
            blocks=[FakeBlock('{"decision": "fi')], stop_reason="max_tokens"
        ))
        verdict = judge(DIFF, client=client, rules="RULE")
        self.assertEqual(verdict.decision, "escalate")
        self.assertIn("cut off", verdict.reason)

    def test_unparseable_json_escalates(self):
        client = FakeClient(FakeResponse(blocks=[FakeBlock("not json")]))
        self.assertEqual(judge(DIFF, client=client, rules="RULE").decision, "escalate")

    def test_missing_text_block_escalates(self):
        client = FakeClient(FakeResponse(blocks=[]))
        self.assertEqual(judge(DIFF, client=client, rules="RULE").decision, "escalate")

    def test_fix_with_no_files_escalates_because_there_is_nothing_to_apply(self):
        client = FakeClient(FakeResponse({
            "decision": "fix", "confidence": 0.95, "reason": "bad", "fixed_code": [],
        }))
        verdict = judge(DIFF, client=client, rules="RULE")
        self.assertEqual(verdict.decision, "escalate")
        self.assertIn("nothing to apply", verdict.reason)

    def test_files_on_a_non_fix_decision_are_dropped(self):
        client = FakeClient(FakeResponse({
            "decision": "allow", "confidence": 0.8, "reason": "fine",
            "fixed_code": [{"path": "a.py", "content": "x"}],
        }))
        self.assertEqual(judge(DIFF, client=client, rules="RULE").fixed_code, [])

    def test_confidence_is_clamped_since_the_schema_cannot_bound_it(self):
        for sent, expected in ((1.7, 1.0), (-0.3, 0.0), ("high", 0.0), (None, 0.0)):
            client = FakeClient(FakeResponse({
                "decision": "allow", "confidence": sent, "reason": "r", "fixed_code": [],
            }))
            self.assertEqual(
                judge(DIFF, client=client, rules="RULE").confidence, expected
            )

    def test_unknown_decision_escalates(self):
        client = FakeClient(FakeResponse({
            "decision": "merge_it", "confidence": 0.9, "reason": "r", "fixed_code": [],
        }))
        self.assertEqual(judge(DIFF, client=client, rules="RULE").decision, "escalate")

    def test_every_verdict_is_writable_to_the_verdicts_table(self):
        # The judge's output must always satisfy db.write_verdict's contract.
        reset_db()
        for payload in (
            {"decision": "fix", "confidence": 0.9, "reason": "r",
             "fixed_code": [{"path": "a.py", "content": "c"}]},
            {"decision": "allow", "confidence": 0.8, "reason": "r", "fixed_code": []},
            {"decision": "escalate", "confidence": 0.2, "reason": "r", "fixed_code": []},
            {"decision": "nonsense", "confidence": 9, "reason": "r", "fixed_code": []},
        ):
            verdict = judge(DIFF, client=FakeClient(FakeResponse(payload)), rules="RULE")
            db.write_verdict(
                101, verdict.decision, verdict.confidence, verdict.reason,
                verdict.fixed_code,
            )
        self.assertEqual(len(db.verdicts(101)), 4)


# --- the claude -p backend ---------------------------------------------------


def cli_result(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(
        args=["claude"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def envelope(**fields):
    """A CLI result envelope, shaped like the real one."""
    base = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "Allowed.",
        "session_id": "abc",
        "total_cost_usd": 0.01,
    }
    base.update(fields)
    return json.dumps(base)


VERDICT_PAYLOAD = {
    "decision": "allow",
    "confidence": 0.9,
    "reason": "Test fixture.",
    "fixed_code": [],
}


class FakeCliBackend(CliBackend):
    """CliBackend with the subprocess call swapped out, so the flag-building
    and envelope-reading logic is tested without invoking the real CLI."""

    def __init__(self, completed):
        super().__init__(binary="claude", timeout=30)
        self.completed = completed
        self.argv_used = None
        self.stdin_used = None

    def complete(self, system, user, schema, model):
        self.argv_used = self.argv(system, schema, model)
        self.stdin_used = user
        return backends._parse_cli_output(self.completed)


class TestCliBackendFlags(unittest.TestCase):
    def argv(self):
        return CliBackend(binary="claude").argv(
            "SYSTEM", judge_mod.VERDICT_SCHEMA, "claude-opus-4-8"
        )

    def test_asks_for_json_and_pins_the_schema(self):
        argv = self.argv()
        self.assertEqual(argv[:3], ["claude", "-p", backends.CLI_PROMPT])
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")
        sent = json.loads(argv[argv.index("--json-schema") + 1])
        self.assertEqual(sent, judge_mod.VERDICT_SCHEMA)

    def test_passes_the_system_prompt_and_model_through(self):
        argv = self.argv()
        self.assertEqual(argv[argv.index("--system-prompt") + 1], "SYSTEM")
        self.assertEqual(argv[argv.index("--model") + 1], "claude-opus-4-8")

    def test_the_judge_gets_no_tools_and_cannot_block_on_a_prompt(self):
        # A diff is attacker-controlled text; the judge only has to read it.
        argv = self.argv()
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("--disable-slash-commands", argv)

    def test_does_not_use_bare_mode(self):
        # --bare never reads OAuth or CLAUDE_CODE_OAUTH_TOKEN, which is the one
        # thing this backend exists for.
        self.assertNotIn("--bare", self.argv())

    def test_leaves_room_for_the_structured_output_turn(self):
        # A verdict takes two turns: the answer, then the structured output.
        argv = self.argv()
        self.assertGreaterEqual(int(argv[argv.index("--max-turns") + 1]), 4)

    def test_runs_outside_the_repo_being_judged(self):
        # Without --bare the CLI reads the working directory's CLAUDE.md,
        # hooks and .mcp.json. Those must not come from the branch under review,
        # so the subprocess runs in a scratch directory instead.
        seen = {}

        def fake_run(argv, **kwargs):
            seen.update(kwargs)
            seen["argv"] = argv
            return cli_result(envelope(structured_output=VERDICT_PAYLOAD))

        original = backends.subprocess.run
        backends.subprocess.run = fake_run
        try:
            CliBackend().complete("SYS", "USER", judge_mod.VERDICT_SCHEMA, "m")
        finally:
            backends.subprocess.run = original

        cwd = seen["cwd"]
        repo = os.path.dirname(os.path.dirname(os.path.abspath(backends.__file__)))
        self.assertTrue(os.path.isabs(cwd))
        self.assertFalse(os.path.abspath(cwd).startswith(repo))
        # And the content really went to stdin, not the argument list.
        self.assertEqual(seen["input"], "USER")


class TestCliBackendOutput(unittest.TestCase):
    def parse(self, completed):
        return backends._parse_cli_output(completed)

    def test_reads_the_structured_verdict(self):
        payload = self.parse(cli_result(envelope(structured_output=VERDICT_PAYLOAD)))
        self.assertEqual(payload, VERDICT_PAYLOAD)

    def test_missing_structured_output_is_an_error_not_a_guess(self):
        # Documented: the field is absent when the run produced no valid
        # structured output. Never fall back to parsing the prose `result`.
        with self.assertRaises(BackendError):
            self.parse(cli_result(envelope()))

    def test_an_in_run_failure_is_detected_even_though_the_exit_code_is_zero(self):
        # The CLI reports a missing login as is_error on a clean exit.
        with self.assertRaises(BackendError) as caught:
            self.parse(cli_result(envelope(
                is_error=True,
                subtype="error_during_execution",
                result="Invalid API key · Please run /login",
            )))
        message = str(caught.exception)
        self.assertIn("error_during_execution", message)
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", message)

    def test_max_turns_is_reported_as_a_failure(self):
        with self.assertRaises(BackendError):
            self.parse(cli_result(envelope(is_error=True, subtype="error_max_turns")))

    def test_non_json_output_escalates_with_the_text_quoted(self):
        with self.assertRaises(BackendError) as caught:
            self.parse(cli_result("command not found: claude", returncode=127))
        self.assertIn("command not found", str(caught.exception))

    def test_empty_output_reports_the_exit_code(self):
        with self.assertRaises(BackendError) as caught:
            self.parse(cli_result("", stderr="boom", returncode=1))
        self.assertIn("boom", str(caught.exception))

    def test_long_errors_are_clipped_so_a_pr_comment_stays_readable(self):
        with self.assertRaises(BackendError) as caught:
            self.parse(cli_result("x" * 5000, returncode=1))
        self.assertLess(len(str(caught.exception)), 600)

    def test_a_missing_cli_escalates_with_install_instructions(self):
        backend = CliBackend(binary="definitely-not-a-real-binary-xyz")
        with self.assertRaises(BackendError) as caught:
            backend.complete("s", "u", judge_mod.VERDICT_SCHEMA, "m")
        self.assertIn("isn't on PATH", str(caught.exception))


class TestJudgeOverTheCli(unittest.TestCase):
    """The judge must behave identically whichever backend answers."""

    def test_a_cli_verdict_parses_like_an_api_one(self):
        backend = FakeCliBackend(cli_result(envelope(structured_output={
            "decision": "fix",
            "confidence": 0.93,
            "reason": "payments.py logs card.number.",
            "fixed_code": [{"path": "checkout/payments.py", "content": "clean"}],
        })))
        verdict = judge(DIFF, backend=backend, rules="RULE")
        self.assertEqual(verdict.decision, "fix")
        self.assertEqual(verdict.changed_paths, ["checkout/payments.py"])

    def test_the_diff_goes_on_stdin_not_in_the_argument_list(self):
        # A large PR would blow past the OS argument-length limit.
        backend = FakeCliBackend(cli_result(envelope(structured_output=VERDICT_PAYLOAD)))
        judge(DIFF, backend=backend, rules="RULE ONE")
        self.assertIn("RULE ONE", backend.stdin_used)
        self.assertIn("card.number", backend.stdin_used)
        self.assertNotIn("card.number", " ".join(backend.argv_used))

    def test_a_cli_failure_escalates_rather_than_raising(self):
        backend = FakeCliBackend(cli_result(envelope(
            is_error=True, subtype="error_during_execution", result="not logged in"
        )))
        verdict = judge(DIFF, backend=backend, rules="RULE")
        self.assertEqual(verdict.decision, "escalate")
        self.assertEqual(verdict.confidence, 0.0)
        self.assertIn("not logged in", verdict.reason)

    def test_the_same_fail_safes_apply(self):
        # 'fix' with nothing to apply escalates over the CLI too.
        backend = FakeCliBackend(cli_result(envelope(structured_output={
            "decision": "fix", "confidence": 0.95, "reason": "bad", "fixed_code": [],
        })))
        verdict = judge(DIFF, backend=backend, rules="RULE")
        self.assertEqual(verdict.decision, "escalate")
        self.assertIn("nothing to apply", verdict.reason)


class TestBackendSelection(unittest.TestCase):
    def setUp(self):
        self._saved = {
            key: os.environ.pop(key, None)
            for key in ("PR_GUARDIAN_BACKEND", "ANTHROPIC_API_KEY")
        }

    def tearDown(self):
        for key, value in self._saved.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value

    def test_defaults_to_the_cli_when_there_is_no_api_key(self):
        self.assertEqual(backends.select_backend_name(), backends.CLAUDE_CLI)

    def test_defaults_to_the_api_when_a_key_is_set(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-test"
        self.assertEqual(backends.select_backend_name(), backends.API)

    def test_the_env_var_overrides_the_key(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-test"
        os.environ["PR_GUARDIAN_BACKEND"] = "claude-cli"
        self.assertEqual(backends.select_backend_name(), backends.CLAUDE_CLI)

    def test_an_unknown_backend_name_fails_loudly(self):
        os.environ["PR_GUARDIAN_BACKEND"] = "gpt"
        with self.assertRaises(ValueError):
            backends.select_backend_name()

    def test_build_backend_returns_a_cli_backend_with_no_key(self):
        self.assertIsInstance(backends.build_backend(), CliBackend)


if __name__ == "__main__":
    unittest.main(verbosity=2)
