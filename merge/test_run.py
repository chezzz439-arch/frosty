"""Tests for the merge step.

    python3 -m unittest discover -s merge

Covers the parts that decide whether a PR merges and whether a run is called a
failure. The GitHub and Snowflake calls are stubbed; nothing here touches the
network or a real repository.
"""

import os
import sys
import unittest
from unittest.mock import patch

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run  # noqa: E402


class TestSlackIsOptional(unittest.TestCase):
    """A missing webhook must not turn a completed merge into a failed run."""

    def setUp(self):
        os.environ.pop("SLACK_WEBHOOK_URL", None)

    def test_no_webhook_skips_rather_than_raising(self):
        self.assertIs(run.slack(1, "allow", "reason", True, "url"), False)

    def test_a_blank_webhook_counts_as_unset(self):
        os.environ["SLACK_WEBHOOK_URL"] = ""
        self.assertIs(run.slack(1, "allow", "reason", True, "url"), False)

    def test_a_configured_webhook_is_posted_to(self):
        os.environ["SLACK_WEBHOOK_URL"] = "https://hooks.invalid/abc"
        with patch.object(run.requests, "post") as post:
            self.assertIs(run.slack(7, "fix", "masked the PAN", True, "u"), True)
        self.assertEqual(post.call_args.args[0], "https://hooks.invalid/abc")
        text = post.call_args.kwargs["json"]["text"]
        self.assertIn("#7", text)
        self.assertIn("FIX", text)

    def test_a_broken_webhook_still_raises(self):
        # Set but failing is a real fault, unlike simply not being configured.
        os.environ["SLACK_WEBHOOK_URL"] = "https://hooks.invalid/abc"
        response = requests.Response()
        response.status_code = 500
        with patch.object(run.requests, "post", return_value=response):
            with self.assertRaises(requests.HTTPError):
                run.slack(1, "allow", "r", True, "u")


class TestMergeWaitsForMergeability(unittest.TestCase):
    """GitHub answers 405 for a few seconds after a push while it recomputes."""

    def test_null_mergeable_is_waited_out(self):
        pulls = [{"mergeable": None}, {"mergeable": True}]
        calls = []

        def fake(method, path, **kwargs):
            calls.append((method, path))
            return pulls.pop(0) if method == "GET" else {"merged": True}

        with patch.object(run, "github", side_effect=fake), patch.object(run.time, "sleep"):
            run.merge_pr(1)
        self.assertIn(("PUT", "/pulls/1/merge"), calls)

    def test_a_405_is_retried_and_then_succeeds(self):
        response = requests.Response()
        response.status_code = 405
        error = requests.HTTPError(response=response)
        outcomes = [{"mergeable": True}, error, {"mergeable": True}, {"merged": True}]

        def fake(method, path, **kwargs):
            item = outcomes.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        with patch.object(run, "github", side_effect=fake), patch.object(run.time, "sleep"):
            self.assertEqual(run.merge_pr(1), {"merged": True})

    def test_an_unmergeable_pr_is_not_retried(self):
        with patch.object(run, "github", return_value={"mergeable": False, "mergeable_state": "dirty"}):
            with self.assertRaises(RuntimeError):
                run.merge_pr(1)

    def test_a_403_is_not_retried(self):
        # Only 405 means "ask again"; a permissions failure must surface at once.
        response = requests.Response()
        response.status_code = 403
        calls = []

        def fake(method, path, **kwargs):
            calls.append(method)
            if method == "GET":
                return {"mergeable": True}
            raise requests.HTTPError(response=response)

        with patch.object(run, "github", side_effect=fake), patch.object(run.time, "sleep"):
            with self.assertRaises(requests.HTTPError):
                run.merge_pr(1)
        self.assertEqual(calls.count("PUT"), 1)


class TestTheMergeGate(unittest.TestCase):
    """fix merges only with tests_passed True; NULL is 'not run', not 'passed'."""

    def _decision(self, verdict):
        seen = {}

        def fake_event(db, pr, step, status, detail):
            seen.setdefault("events", []).append((step, status))

        with patch.object(run, "db_connect"), patch.object(run, "verdict", return_value=verdict), \
             patch.object(run, "event", fake_event), patch.dict(os.environ, {"GUARDIAN_DRY_RUN": "1"}), \
             patch("builtins.print") as printer:
            run.process(1)
        return printer.call_args.args[0]

    def test_tests_passed_null_escalates(self):
        out = self._decision({"decision": "fix", "confidence": 0.97, "reason": "r",
                              "fixed_code": [], "tests_passed": None, "attempts": 0})
        self.assertIn('"effective_decision": "escalate"', out)

    def test_tests_passed_false_escalates(self):
        out = self._decision({"decision": "fix", "confidence": 0.97, "reason": "r",
                              "fixed_code": [], "tests_passed": False, "attempts": 3})
        self.assertIn('"effective_decision": "escalate"', out)

    def test_low_confidence_escalates(self):
        out = self._decision({"decision": "fix", "confidence": 0.10, "reason": "r",
                              "fixed_code": [], "tests_passed": True, "attempts": 1})
        self.assertIn('"effective_decision": "escalate"', out)

    def test_a_tested_confident_fix_stays_a_fix(self):
        out = self._decision({"decision": "fix", "confidence": 0.92, "reason": "r",
                              "fixed_code": [], "tests_passed": True, "attempts": 1})
        self.assertIn('"effective_decision": "fix"', out)


class TestFixedCodeFormat(unittest.TestCase):
    """The engine writes a bare list; a path that escapes the repo is refused."""

    def test_a_bare_list_is_accepted(self):
        files = run.decode_files('[{"path":"a.py","content":"x"}]')
        self.assertEqual(files[0]["path"], "a.py")

    def test_a_files_object_is_accepted(self):
        files = run.decode_files('{"files":[{"path":"a.py","content":"x"}]}')
        self.assertEqual(files[0]["path"], "a.py")

    def test_a_traversing_path_is_refused(self):
        with self.assertRaises(ValueError):
            run.decode_files('[{"path":"../../etc/passwd","content":"x"}]')

    def test_a_workflow_path_is_refused(self):
        with self.assertRaises(ValueError):
            run.decode_files('[{"path":".github/workflows/ci.yml","content":"x"}]')


if __name__ == "__main__":
    unittest.main()
