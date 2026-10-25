"""Tests for engine/db.py, engine/judge.py and engine/backends.py.

    python engine/test_judge.py

No API key, no CLI, no network: the judge takes its backend as an argument, so
these pass fakes that return canned responses. That covers the plumbing and the
fail-safe paths. It does NOT check that the real model rules correctly on the
three planted PRs -- that needs a live call.

The store is pinned to SQLite below for the same reason. These tests are not
an integration check against Snowflake: they must pass on a laptop with no
credentials, and they must not spend warehouse time or write rows into the
table the dashboard is reading.

stdlib unittest, not pytest, so the engine's own tests add no dependency.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest

# Both set before importing anything that opens the store.
_TMP = tempfile.mkdtemp(prefix="pr-guardian-test-")
os.environ["PR_GUARDIAN_DB"] = os.path.join(_TMP, "test.db")
os.environ["PR_GUARDIAN_DB_BACKEND"] = "sqlite"

import shutil  # noqa: E402

import backends  # noqa: E402
import db  # noqa: E402
import fixer  # noqa: E402
import workspace  # noqa: E402
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
        rules, source = judge_mod.load_rules(os.path.join(_TMP, "nope.md"))
        self.assertEqual(rules, judge_mod.FALLBACK_RULES)
        # run.py logs this as a failed step: the fallback is thinner than the
        # real file and changes verdicts, so it must not pass silently.
        self.assertEqual(source, "built-in")

    def test_load_rules_reads_the_file_when_it_exists(self):
        path = os.path.join(_TMP, "rules.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("1. No secrets in logs.\n")
        self.assertEqual(judge_mod.load_rules(path), ("1. No secrets in logs.", path))

    def test_an_empty_rules_file_counts_as_missing(self):
        path = os.path.join(_TMP, "empty-rules.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("   \n")
        self.assertEqual(judge_mod.load_rules(path)[1], "built-in")

    def test_the_action_can_point_the_rules_somewhere_else(self):
        # trigger/guardian.yml sets PR_GUARDIAN_RULES into its own checkout of
        # frosty, so a PR cannot edit the rules it is judged by.
        path = os.path.join(_TMP, "trusted-rules.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("R1 - trusted.\n")
        saved = os.environ.get("PR_GUARDIAN_RULES")
        os.environ["PR_GUARDIAN_RULES"] = path
        try:
            self.assertEqual(judge_mod.load_rules(), ("R1 - trusted.", path))
        finally:
            if saved is None:
                os.environ.pop("PR_GUARDIAN_RULES", None)
            else:
                os.environ["PR_GUARDIAN_RULES"] = saved


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


# --- the sandbox and the self-correcting loop --------------------------------


PASSING_TEST = (
    "import unittest\n"
    "class T(unittest.TestCase):\n"
    "    def test_ok(self):\n"
    "        from app.code import value\n"
    "        self.assertEqual(value(), 1)\n"
)


def make_repo(root):
    """A tiny repo whose suite passes."""
    os.makedirs(os.path.join(root, "app"), exist_ok=True)
    os.makedirs(os.path.join(root, "tests"), exist_ok=True)
    for path, text in (
        ("app/__init__.py", ""),
        ("app/code.py", "def value():\n    return 1\n"),
        ("tests/__init__.py", ""),
        ("tests/test_code.py", PASSING_TEST),
    ):
        with open(os.path.join(root, path), "w", encoding="utf-8") as handle:
            handle.write(text)
    return root


class TestRepoRoot(unittest.TestCase):
    """Which tree the engine reads, sandboxes and tests.

    trigger/guardian.yml checks the PR out into repo/ next to guardian/, so
    the working directory is the workspace root rather than the tree under
    review. Getting this wrong is silent: the engine would copy both
    checkouts and run the wrong test suite.
    """

    def setUp(self):
        self._saved = {
            name: os.environ.pop(name, None)
            for name in ("PR_GUARDIAN_REPO", "PR_GUARDIAN_REPO_DIR")
        }

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_defaults_to_the_working_directory(self):
        self.assertEqual(workspace.repo_root(), os.path.abspath(os.getcwd()))

    def test_accepts_the_name_the_action_sets(self):
        os.environ["PR_GUARDIAN_REPO_DIR"] = _TMP
        self.assertEqual(workspace.repo_root(), os.path.abspath(_TMP))

    def test_accepts_the_engines_own_name(self):
        os.environ["PR_GUARDIAN_REPO"] = _TMP
        self.assertEqual(workspace.repo_root(), os.path.abspath(_TMP))

    def test_the_engines_own_name_wins_when_both_are_set(self):
        os.environ["PR_GUARDIAN_REPO"] = _TMP
        os.environ["PR_GUARDIAN_REPO_DIR"] = os.path.join(_TMP, "other")
        self.assertEqual(workspace.repo_root(), os.path.abspath(_TMP))


class TestWorkspace(unittest.TestCase):
    def setUp(self):
        self.root = make_repo(tempfile.mkdtemp(prefix="pr-guardian-repo-"))

    def test_read_sources_returns_whole_files(self):
        sources = workspace.read_sources(self.root, ["app/code.py"])
        self.assertEqual(sources[0]["path"], "app/code.py")
        self.assertIn("return 1", sources[0]["content"])

    def test_an_unreadable_file_is_reported_not_silently_dropped(self):
        # The judge must not be left to assume a missing file was empty.
        sources = workspace.read_sources(self.root, ["app/code.py", "gone.py"])
        self.assertEqual(len(sources), 2)
        self.assertIsNone(sources[1]["content"])

    def test_a_path_escaping_the_repo_is_refused(self):
        # fixed_code paths come from a model reading an attacker-controlled diff.
        for escape in ("../outside.py", "app/../../outside.py", "/etc/passwd"):
            with self.assertRaises(workspace.UnsafePath):
                workspace.apply_fix(self.root, [{"path": escape, "content": "x"}])
        self.assertFalse(os.path.exists(os.path.join(self.root, "..", "outside.py")))

    def test_the_sandbox_is_a_copy_and_the_real_checkout_is_untouched(self):
        sandbox = workspace.copy_repo(self.root)
        try:
            workspace.apply_fix(sandbox, [{"path": "app/code.py", "content": "BROKEN"}])
            with open(os.path.join(self.root, "app/code.py"), encoding="utf-8") as f:
                self.assertIn("return 1", f.read())
        finally:
            shutil.rmtree(os.path.dirname(sandbox), ignore_errors=True)

    def test_run_tests_reports_green_and_red(self):
        sandbox = workspace.copy_repo(self.root)
        try:
            passed, _ = workspace.run_tests(sandbox)
            self.assertTrue(passed)
            workspace.apply_fix(
                sandbox, [{"path": "app/code.py", "content": "def value():\n    return 2\n"}]
            )
            passed, output = workspace.run_tests(sandbox)
            self.assertFalse(passed)
            self.assertIn("FAILED", output)
        finally:
            shutil.rmtree(os.path.dirname(sandbox), ignore_errors=True)


class ScriptedBackend:
    """Returns a queued verdict payload per call, recording the prompts."""

    name = "scripted"

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.prompts = []

    def complete(self, system, user, schema, model):
        self.prompts.append(user)
        return self.payloads.pop(0)


GOOD = "def value():\n    return 1\n"
BAD = "def value():\n    return 99\n"


def payload(content, decision="fix", path="app/code.py"):
    return {
        "decision": decision,
        "confidence": 0.9,
        "reason": "logs a card number",
        "fixed_code": [{"path": path, "content": content}] if decision == "fix" else [],
    }


class TestFixLoop(unittest.TestCase):
    def setUp(self):
        reset_db()
        self.root = make_repo(tempfile.mkdtemp(prefix="pr-guardian-repo-"))
        self.diff = dict(DIFF, files=["app/code.py"])

    def loop(self, payloads, first=None):
        backend = ScriptedBackend(payloads)
        verdict = judge_mod._coerce(first or payload(GOOD))
        return backend, fixer.run_fix_loop(
            self.diff, verdict, backend=backend, root=self.root
        )

    def steps(self):
        return [(e["step"], e["status"]) for e in db.events(101)]

    def test_a_fix_that_passes_first_time(self):
        _, outcome = self.loop([])
        self.assertEqual(outcome.verdict.decision, "fix")
        self.assertIs(outcome.tests_passed, True)
        self.assertEqual(outcome.attempts, 1)
        self.assertIn(("fix attempt 1", "ok"), self.steps())
        self.assertIn(("tests passed", "ok"), self.steps())

    def test_a_failing_fix_is_retried_and_can_recover(self):
        _, outcome = self.loop([payload(GOOD)], first=payload(BAD))
        self.assertEqual(outcome.verdict.decision, "fix")
        self.assertIs(outcome.tests_passed, True)
        self.assertEqual(outcome.attempts, 2)
        self.assertIn(("fix attempt 1", "failed"), self.steps())
        self.assertIn(("fix attempt 2", "ok"), self.steps())

    def test_the_test_failure_is_fed_back_to_the_judge(self):
        backend, _ = self.loop([payload(GOOD)], first=payload(BAD))
        retry_prompt = backend.prompts[0]
        self.assertIn("FAILED", retry_prompt)
        self.assertIn("Attempt 1", retry_prompt)
        self.assertIn("app/code.py", retry_prompt)

    def test_three_failures_become_an_escalation_that_cannot_merge(self):
        _, outcome = self.loop(
            [payload(BAD), payload(BAD)], first=payload(BAD)
        )
        self.assertEqual(outcome.verdict.decision, "escalate")
        self.assertIs(outcome.tests_passed, False)
        self.assertEqual(outcome.attempts, 3)
        # /merge gates on "fix AND tests_passed"; escalate never merges.
        self.assertIn(("fix attempt 3", "failed"), self.steps())
        self.assertIn(("tests passed", "failed"), self.steps())

    def test_failed_fixes_are_not_carried_into_the_verdict(self):
        # A VERDICTS row holding code that failed its tests invites someone to
        # apply it anyway.
        _, outcome = self.loop([payload(BAD), payload(BAD)], first=payload(BAD))
        self.assertEqual(outcome.verdict.fixed_code, [])

    def test_it_never_tries_a_fourth_time(self):
        backend, outcome = self.loop([payload(BAD), payload(BAD)], first=payload(BAD))
        # Two retries after the first attempt, then it stops.
        self.assertEqual(len(backend.prompts), 2)
        self.assertEqual(outcome.attempts, 3)

    def test_the_judge_backing_off_mid_loop_is_respected(self):
        _, outcome = self.loop(
            [payload("", decision="escalate")], first=payload(BAD)
        )
        self.assertEqual(outcome.verdict.decision, "escalate")
        self.assertIs(outcome.tests_passed, False)
        self.assertEqual(outcome.attempts, 1)

    def test_an_unplaceable_fix_escalates_without_being_tested(self):
        _, outcome = self.loop([], first=payload(GOOD, path="../escape.py"))
        self.assertEqual(outcome.verdict.decision, "escalate")
        self.assertIsNone(outcome.tests_passed)
        self.assertIn(("fix attempt 1", "failed"), self.steps())

    def test_every_attempt_writes_an_event_for_the_dashboard(self):
        # Each attempt logs "running" and then its outcome, so the dashboard
        # can show a step in flight rather than jumping from nothing to done.
        self.loop([payload(BAD), payload(BAD)], first=payload(BAD))
        attempts = [s for s in self.steps() if s[0].startswith("fix attempt")]
        self.assertEqual(attempts, [
            ("fix attempt 1", "running"), ("fix attempt 1", "failed"),
            ("fix attempt 2", "running"), ("fix attempt 2", "failed"),
            ("fix attempt 3", "running"), ("fix attempt 3", "failed"),
        ])

    def test_the_real_checkout_is_never_written_to(self):
        self.loop([payload(BAD), payload(BAD)], first=payload(BAD))
        with open(os.path.join(self.root, "app/code.py"), encoding="utf-8") as handle:
            self.assertIn("return 1", handle.read())


class TestFullFileLoading(unittest.TestCase):
    def test_file_contents_reach_the_prompt(self):
        content = build_user_content(
            DIFF, "RULE", [], sources=[{"path": "a.py", "content": "WHOLE FILE"}]
        )
        self.assertIn("WHOLE FILE", content)
        self.assertIn("complete new text", content)

    def test_an_unreadable_file_is_declared_in_the_prompt(self):
        content = build_user_content(
            DIFF, "RULE", [], sources=[{"path": "a.py", "content": None}]
        )
        self.assertIn("not readable", content)

    def test_the_ambiguity_rules_are_in_the_fallback(self):
        # Until demo-data/rules.md lands, these are what make #103 escalate.
        self.assertIn("escalate unless clearly safe", judge_mod.FALLBACK_RULES)
        self.assertIn("breaks behavior", judge_mod.FALLBACK_RULES)


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


def _generate_pem() -> str:
    """A throwaway RSA key, so these tests need no real credential on disk."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


class TestSnowflakeCredentialsFromEnv(unittest.TestCase):
    """The CI path: no snow CLI config, key arrives as a secret.

    These check the shape of what db.py hands the connector. They never open a
    connection -- the live check is `snow connection test`, not a unit test.
    """

    def setUp(self):
        self._saved = {
            name: os.environ.pop(name, None)
            for name in (
                *db.REQUIRED_ENV,
                "SNOWFLAKE_WAREHOUSE",
                "SNOWFLAKE_DATABASE",
                "SNOWFLAKE_SCHEMA",
                "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE",
            )
        }
        self.pem = _generate_pem()

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _set_required(self):
        os.environ["SNOWFLAKE_ACCOUNT"] = "ACC-1"
        os.environ["SNOWFLAKE_USER"] = "someone"
        os.environ["SNOWFLAKE_PRIVATE_KEY"] = self.pem

    def test_no_env_means_fall_back_to_the_snow_cli_connection(self):
        self.assertIsNone(db._env_connect_args())

    def test_a_partial_set_is_ignored_rather_than_half_used(self):
        # Half-configured CI must not look like a working connection.
        os.environ["SNOWFLAKE_ACCOUNT"] = "ACC-1"
        self.assertIsNone(db._env_connect_args())

    def test_the_key_is_converted_to_der_not_passed_as_pem(self):
        # The connector rejects PEM text outright:
        # "251008: Failed to decode private key: Incorrect padding".
        self._set_required()
        args = db._env_connect_args()
        self.assertIsInstance(args["private_key"], bytes)
        self.assertNotIn(b"BEGIN PRIVATE KEY", args["private_key"])
        self.assertEqual(args["authenticator"], "SNOWFLAKE_JWT")

    def test_escaped_newlines_are_accepted(self):
        # A key pasted into a secret often arrives with literal backslash-n.
        self._set_required()
        os.environ["SNOWFLAKE_PRIVATE_KEY"] = self.pem.replace("\n", "\\n")
        flattened = db._env_connect_args()["private_key"]

        os.environ["SNOWFLAKE_PRIVATE_KEY"] = self.pem
        self.assertEqual(flattened, db._env_connect_args()["private_key"])

    def test_context_defaults_to_the_contract_tables(self):
        self._set_required()
        args = db._env_connect_args()
        self.assertEqual(args["database"], db.DATABASE)
        self.assertEqual(args["schema"], db.SCHEMA)
        self.assertEqual(args["warehouse"], db.WAREHOUSE)

    def test_context_can_be_overridden(self):
        self._set_required()
        os.environ["SNOWFLAKE_SCHEMA"] = "STAGING"
        self.assertEqual(db._env_connect_args()["schema"], "STAGING")

    def test_an_unreadable_key_says_what_to_fix(self):
        self._set_required()
        os.environ["SNOWFLAKE_PRIVATE_KEY"] = "not a key"
        with self.assertRaises(RuntimeError) as caught:
            db._env_connect_args()
        self.assertIn("BEGIN", str(caught.exception))

    def test_a_blank_key_fails_loudly_rather_than_falling_back(self):
        # A secret that exists but is empty is a misconfigured workflow.
        # Falling back to the CLI connection would report "no config file" on
        # a runner that has none, pointing at the wrong thing entirely.
        self._set_required()
        os.environ["SNOWFLAKE_PRIVATE_KEY"] = "   "
        with self.assertRaises(RuntimeError) as caught:
            db._env_connect_args()
        self.assertIn("empty", str(caught.exception))

    def test_the_private_key_never_appears_in_an_error_message(self):
        # An exception from here lands in CI logs.
        self._set_required()
        os.environ["SNOWFLAKE_PRIVATE_KEY"] = self.pem[:120]
        with self.assertRaises(RuntimeError) as caught:
            db._env_connect_args()
        self.assertNotIn(self.pem[60:120], str(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
