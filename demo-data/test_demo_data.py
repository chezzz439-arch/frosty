"""Self-check for /demo-data: proves the planted PRs behave the way the demo needs.

    python demo-data/test_demo_data.py

Each case copies shop/ to a temp dir, applies a planted PR (and optionally a fix), and runs
the shop's test suite. No network, no Claude: this checks the test suite and PRs, not the judge.

What it proves:
  * main is green
  * the violation PR fails the suite (log-safety test catches the full card number)
  * two lazy fixes fail -- deleting the log line, or masking away the last 4 digits --
    so a sloppy first attempt by the engine gets a red run and a real retry
  * the reference fix (mask_pan) passes
  * the decoy and ambiguous PRs pass the suite, so neither is "obviously broken"
  * the violation and decoy carry the same card number, as CLAUDE.md requires
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SHOP = HERE / "shop"
PLANTED = HERE / "planted-prs"
SAME_DIGITS = "4111111111111111"
TEST_CMD = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]


def run_suite(*overlays: pathlib.Path, edit=None) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory(prefix="demo-shop-") as tmp:
        work = pathlib.Path(tmp) / "shop"
        shutil.copytree(SHOP, work)
        for overlay in overlays:
            shutil.copytree(overlay, work, dirs_exist_ok=True)
        if edit:
            edit(work)
        return subprocess.run(TEST_CMD, cwd=work, capture_output=True, text=True)


def replace_in(rel: str, old: str, new: str):
    def edit(work: pathlib.Path):
        path = work / rel
        text = path.read_text()
        assert old in text, f"{old!r} not in {rel}"
        path.write_text(text.replace(old, new))
    return edit


VIOLATION = PLANTED / "01-violation" / "files"
REFERENCE_FIX = PLANTED / "01-violation" / "reference_fix"
DECOY = PLANTED / "02-decoy" / "files"
AMBIGUOUS = PLANTED / "03-ambiguous" / "files"
LEAK = 'logger.info("Card used: %s", card.number)\n'


class DemoData(unittest.TestCase):
    def assertGreen(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def assertRed(self, result, mentions: str):
        self.assertNotEqual(result.returncode, 0, "suite passed but should have failed")
        self.assertIn(mentions, result.stdout)

    def test_main_is_green(self):
        self.assertGreen(run_suite())

    def test_violation_fails_log_safety(self):
        self.assertRed(run_suite(VIOLATION), "test_log_safety")

    def test_lazy_fix_delete_log_line_fails(self):
        result = run_suite(VIOLATION, edit=replace_in("checkout/payments.py", "    " + LEAK, ""))
        self.assertRed(result, "test_charge_logs_which_card_was_used")

    def test_lazy_fix_mask_everything_fails(self):
        result = run_suite(VIOLATION, edit=replace_in(
            "checkout/payments.py", LEAK, 'logger.info("Card used: %s", "****")\n'))
        self.assertRed(result, "test_charge_logs_which_card_was_used")

    def test_reference_fix_passes(self):
        self.assertGreen(run_suite(VIOLATION, REFERENCE_FIX))

    def test_decoy_passes(self):
        self.assertGreen(run_suite(DECOY))

    def test_ambiguous_passes(self):
        self.assertGreen(run_suite(AMBIGUOUS))

    def test_violation_and_decoy_share_the_card_number(self):
        violation_text = "".join(p.read_text() for p in VIOLATION.rglob("*.py"))
        decoy_text = "".join(p.read_text() for p in DECOY.rglob("*.py"))
        self.assertIn(SAME_DIGITS, violation_text)
        self.assertIn(SAME_DIGITS, decoy_text)

    def test_planted_prs_say_what_they_expect(self):
        expected = {"01-violation": "fix", "02-decoy": "allow", "03-ambiguous": "escalate"}
        for name, decision in expected.items():
            meta = (PLANTED / name / "pr.md").read_text()
            self.assertIn(f"expect: {decision}\n", meta)
            self.assertIn("branch: ", meta)
            self.assertIn("title: ", meta)


if __name__ == "__main__":
    unittest.main(verbosity=2)
