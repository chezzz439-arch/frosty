"""Tests for trigger/send_diff.py. No network, no Snowflake, no GitHub.

    python trigger/test_send_diff.py
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import send_diff  # noqa: E402


def git(*args, cwd):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class Planted(unittest.TestCase):
    def test_each_planted_pr_touches_the_right_files(self):
        expected = {
            "violation": ["checkout/payments.py", "tests/test_payments.py"],
            "decoy": ["tests/test_cards.py"],
            "ambiguous": ["checkout/gateway.py", "tests/test_gateway.py"],
        }
        for name, files in expected.items():
            pr = send_diff.from_planted(name, None, "local/demo-shop")
            self.assertEqual(sorted(pr["files"]), files, name)
            self.assertTrue(pr["diff_text"].startswith("diff --git"), name)

    def test_default_numbers_match_engine_seeds(self):
        self.assertEqual([send_diff.from_planted(n, None, "r")["pr_number"]
                          for n in ("violation", "decoy", "ambiguous")], [101, 102, 103])

    def test_violation_and_decoy_diffs_share_digits(self):
        for name in ("violation", "decoy"):
            self.assertIn("+", send_diff.from_planted(name, 7, "r")["diff_text"])
            self.assertIn("4111111111111111", send_diff.from_planted(name, 7, "r")["diff_text"])

    def test_checkout_writes_the_pr_branch_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "shop-101"
            send_diff.from_planted("violation", None, "r", out)
            self.assertIn("card.number)", (out / "checkout/payments.py").read_text())
            self.assertTrue((out / "tests/test_log_safety.py").exists())
            self.assertFalse((out / ".git").exists())

    def test_violation_diff_contains_the_leak(self):
        diff = send_diff.from_planted("violation", 1, "r")["diff_text"]
        self.assertIn('+    logger.info("Card used: %s", card.number)', diff)


class FromEvent(unittest.TestCase):
    def test_reads_three_dot_diff_from_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            (repo / "a.py").write_text("x = 1\n")
            git("init", "-q", "-b", "main", cwd=repo); git("add", "-A", cwd=repo); git("commit", "-qm", "base", cwd=repo)
            base = git("rev-parse", "HEAD", cwd=repo)
            git("checkout", "-qb", "feature", cwd=repo)
            (repo / "b.py").write_text("y = 2\n")
            git("add", "-A", cwd=repo); git("commit", "-qm", "feat", cwd=repo)
            head = git("rev-parse", "HEAD", cwd=repo)
            event = repo / "event.json"
            event.write_text(json.dumps({
                "pull_request": {"number": 5, "title": "T", "base": {"sha": base},
                                 "head": {"sha": head, "ref": "feature"}},
                "repository": {"full_name": "o/demo"}}))
            old = os.getcwd(); os.chdir(repo)
            os.environ["GITHUB_EVENT_PATH"] = str(event)
            try:
                pr = send_diff.from_event()
            finally:
                os.chdir(old)
            self.assertEqual((pr["pr_number"], pr["repo"], pr["branch"], pr["files"]), (5, "o/demo", "feature", ["b.py"]))
            self.assertIn("+y = 2", pr["diff_text"])


class FakeCursor:
    def __init__(self, log): self.log = log
    def execute(self, sql, params=None): self.log.append((sql, params))


class FakeConn:
    def __init__(self): self.log, self.commits = [], 0
    def cursor(self): return FakeCursor(self.log)
    def commit(self): self.commits += 1


class Stores(unittest.TestCase):
    PR = {"pr_number": 9, "repo": "o/r", "branch": "b", "title": "T", "files": ["a.py"], "diff_text": "d%s"}

    def test_snowflake_sql_matches_setup_sql_columns(self):
        store = send_diff.SnowflakeStore.__new__(send_diff.SnowflakeStore)
        store.conn = FakeConn()
        store.write(self.PR, "1 file(s)")
        sqls = [s for s, _ in store.conn.log]
        self.assertIn("INSERT INTO PR_DIFFS (pr_number, repo, branch, files, diff_text) "
                      "SELECT %s, %s, %s, PARSE_JSON(%s), %s", sqls)
        diff_params = store.conn.log[1][1]
        self.assertEqual(diff_params, (9, "o/r", "b", '["a.py"]', "d%s"))
        self.assertEqual([p[1] for _, p in store.conn.log if "PR_EVENTS" in _], ["pr opened", "diff written"])
        self.assertEqual(store.conn.commits, 1)

    def test_engine_store_uses_engine_db_functions(self):
        calls = []
        fake_db = types.SimpleNamespace(
            insert_diff=lambda **kw: calls.append(("diff", kw)),
            log_event=lambda *a: calls.append(("event", a)))
        store = send_diff.EngineStore.__new__(send_diff.EngineStore)
        store.db = fake_db
        store.write(self.PR, "1 file(s)")
        self.assertEqual([c[0] for c in calls], ["event", "diff", "event"])
        self.assertEqual(calls[1][1]["files"], ["a.py"])
        self.assertEqual(calls[2][1][1:3], ("diff written", "ok"))

    def test_auto_prefers_engine_and_never_writes_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            (pathlib.Path(tmp) / "db.py").write_text("def insert_diff(**kw): pass\ndef log_event(*a): pass\n")
            os.environ["SNOWFLAKE_ACCOUNT"] = "x"
            try:
                stores = send_diff.open_stores("auto", pathlib.Path(tmp), None)
            finally:
                os.environ.pop("SNOWFLAKE_ACCOUNT")
                sys.modules.pop("db", None)
            self.assertEqual([s.name for s in stores], ["engine"])

    def test_auto_with_nothing_available_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ.pop("SNOWFLAKE_ACCOUNT", None)
            with self.assertRaises(SystemExit):
                send_diff.open_stores("auto", pathlib.Path(tmp), None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
