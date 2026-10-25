"""Tests for engine/db.py and engine/judge.py.

    python engine/test_judge.py

No API key and no network: the judge takes its client as an argument, so these
pass a fake that returns canned responses. That covers the plumbing and the
fail-safe paths. It does NOT check that the real model rules correctly on the
three planted PRs -- that needs a key and a live call.

stdlib unittest, not pytest, so the engine's own tests add no dependency.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

# Point db at a scratch file before importing anything that opens it.
_TMP = tempfile.mkdtemp(prefix="pr-guardian-test-")
os.environ["PR_GUARDIAN_DB"] = os.path.join(_TMP, "test.db")

import db  # noqa: E402
import judge as judge_mod  # noqa: E402
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
