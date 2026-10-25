"""The self-correcting loop: apply the fix, run the tests, retry on red.

    fix -> sandbox -> rewrite -> test -> retry until green (max 3) -> escalate

This is the step CLAUDE.md says to make impressive, and the only place the
engine changes its own mind. The rule it enforces comes straight from the
contract: a 'fix' that hasn't been proved green must never reach /merge as
mergeable, so after the third failed attempt the decision becomes 'escalate'.

Every attempt writes a PR_EVENTS row, because the dashboard's story is the
retries: "fix attempt 1 failed, fix attempt 2 passed" is the demo.
"""

from __future__ import annotations

import shutil
import os
from dataclasses import dataclass

import db
import workspace
from judge import Verdict, judge

# CLAUDE.md: "retry until green (max 3)".
MAX_ATTEMPTS = 3


@dataclass
class FixOutcome:
    """What the loop settled on, ready for VERDICTS."""

    verdict: Verdict
    tests_passed: bool | None
    attempts: int


def _feedback(output: str, attempt: int, paths: list[str]) -> str:
    """Tell the judge what its own fix broke."""
    changed = ", ".join(paths) or "(no files)"
    return (
        f"You already tried to fix this. Attempt {attempt} rewrote {changed}, "
        f"and the test suite then FAILED. This is the output:\n\n"
        f"```\n{output}\n```"
    )


def run_fix_loop(
    diff: dict,
    verdict: Verdict,
    backend,
    rules: str | None = None,
    receipts: list[dict] | None = None,
    root: str | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> FixOutcome:
    """Prove a 'fix' verdict green, or turn it into an escalation.

    Only ever called for a 'fix'. 'allow' and 'escalate' change nothing on
    disk, so there is nothing to test.

    Never raises: like the judge, every failure path ends in an escalation,
    because an exception in an unattended Action leaves no VERDICTS row and
    /merge reads that as "not judged yet".
    """
    pr_number = diff.get("pr_number")
    root = root or workspace.repo_root()

    current = verdict
    sandbox = None

    for attempt in range(1, max_attempts + 1):
        step = f"fix attempt {attempt}"
        db.log_event(
            pr_number, step, "running", f"rewriting {', '.join(current.changed_paths)}"
        )

        try:
            sandbox = workspace.copy_repo(root)
        except OSError as exc:
            db.log_event(pr_number, step, "failed", f"could not sandbox the repo: {exc}")
            return FixOutcome(
                verdict=Verdict(
                    decision="escalate",
                    confidence=0.0,
                    reason=(
                        f"Could not copy the repository to a sandbox ({exc}), so the "
                        f"fix was never tested. Original finding: {current.reason}"
                    ),
                ),
                tests_passed=None,
                attempts=attempt - 1,
            )

        try:
            try:
                written = workspace.apply_fix(sandbox, current.fixed_code)
            except (workspace.UnsafePath, OSError) as exc:
                # A path we can't place safely isn't a fix. Don't retry: the
                # model already had the file list and got it wrong.
                db.log_event(pr_number, step, "failed", f"could not apply the fix: {exc}")
                return FixOutcome(
                    verdict=Verdict(
                        decision="escalate",
                        confidence=0.0,
                        reason=(
                            f"The proposed fix could not be applied ({exc}), so it was "
                            f"never tested. Original finding: {current.reason}"
                        ),
                    ),
                    tests_passed=None,
                    attempts=attempt,
                )

            passed, output = workspace.run_tests(sandbox)
        finally:
            if sandbox:
                shutil.rmtree(os.path.dirname(sandbox), ignore_errors=True)
                sandbox = None

        if passed:
            db.log_event(pr_number, step, "ok", f"tests passed, rewrote {', '.join(written)}")
            db.log_event(pr_number, "tests passed", "ok", f"after {attempt} attempt(s)")
            return FixOutcome(verdict=current, tests_passed=True, attempts=attempt)

        db.log_event(pr_number, step, "failed", _first_line(output))

        if attempt == max_attempts:
            break

        # Hand the failure back and let it try again.
        retried = judge(
            diff,
            backend=backend,
            rules=rules,
            receipts=receipts,
            sources=workspace.read_sources(root, diff.get("files") or []),
            feedback=_feedback(output, attempt, written),
        )

        if not retried.is_fix:
            # The judge looked at its own failure and backed off. Respect that
            # -- it's the fail-safe working, not a loop to push through.
            db.log_event(
                pr_number,
                "decision made",
                "ok",
                f"{retried.decision} after attempt {attempt} failed",
            )
            return FixOutcome(
                verdict=retried, tests_passed=False, attempts=attempt
            )

        current = retried

    # Three attempts, still red. CLAUDE.md: escalate, never merge.
    db.log_event(
        pr_number, "tests passed", "failed", f"still failing after {max_attempts} attempts"
    )
    return FixOutcome(
        verdict=Verdict(
            decision="escalate",
            confidence=0.0,
            reason=(
                f"The violation is real, but {max_attempts} attempted fixes each left "
                f"the test suite failing, so none of them is safe to merge. A human "
                f"should take it from here. The finding was: {current.reason}"
            ),
            # Deliberately dropped: these files failed their tests, and a
            # VERDICTS row carrying them invites someone to apply them anyway.
            fixed_code=[],
        ),
        tests_passed=False,
        attempts=max_attempts,
    )


def _first_line(output: str, limit: int = 200) -> str:
    """A one-line summary of a failure, for the dashboard's detail column."""
    for line in reversed((output or "").strip().splitlines()):
        line = line.strip()
        if line and not line.startswith("-") and not line.startswith("="):
            return line[:limit]
    return "tests failed"
