"""Run the engine for one PR: read the diff, judge it, write the verdict.

    python engine/run.py <pr_number>
    python engine/run.py <pr_number> --show   # print the trail, judge nothing

Which backend asks Claude is decided in engine/backends.py: the anthropic SDK
when ANTHROPIC_API_KEY is set, otherwise `claude -p` against the Claude Code
login. Force one with PR_GUARDIAN_BACKEND=api|claude-cli.

Not yet built (next step): applying `fixed_code` into a temp copy of the repo,
running the test suite there, and retrying up to 3 times. Until that lands, a
'fix' verdict is written with tests_passed NULL and attempts 0, so /merge's
gate ("fix AND tests_passed") correctly refuses to merge it.
"""

from __future__ import annotations

import sys

import db
import workspace
from backends import build_backend
from fixer import run_fix_loop
from judge import judge, load_rules


def show(pr_number: int) -> int:
    diff = db.read_diff(pr_number)
    print(f"PR #{pr_number}")
    if diff is None:
        print("  no PR_DIFFS row")
    else:
        print(f"  repo    {diff['repo']} ({diff['branch']})")
        print(f"  files   {', '.join(diff['files']) or '(none)'}")
        print(f"  diff    {len(diff['diff_text'] or '')} chars")

    print("\nVERDICTS")
    for verdict in db.verdicts(pr_number) or []:
        print(
            f"  {verdict['decision']:<9} conf={verdict['confidence']:.2f} "
            f"tests_passed={verdict['tests_passed']} "
            f"attempts={verdict['attempts']}"
        )
        print(f"    {verdict['reason']}")
        for changed in verdict["fixed_code"]:
            print(f"    rewrote {changed['path']}")

    print("\nPR_EVENTS")
    for event in db.events(pr_number) or []:
        print(f"  [{event['status']:<7}] {event['step']}  {event['detail']}")
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0

    try:
        pr_number = int(argv[0])
    except ValueError:
        print(f"pr_number must be a number, got {argv[0]!r}", file=sys.stderr)
        return 2

    if "--show" in argv[1:]:
        return show(pr_number)

    # Built before anything is logged: a missing SDK, key or CLI login is a
    # setup problem, not a step of the loop, and failing here avoids leaving a
    # "running" event on the dashboard with nothing to follow it.
    try:
        backend = build_backend()
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc))

    diff = db.read_diff(pr_number)
    if diff is None:
        # Not an error: the engine may simply have run before /trigger wrote
        # the row. Log it so the gap is visible on the dashboard.
        db.log_event(pr_number, "diff read", "failed", "no PR_DIFFS row for this PR")
        print(f"No PR_DIFFS row for PR #{pr_number}.", file=sys.stderr)
        return 1

    db.log_event(
        pr_number,
        "diff read",
        "ok",
        f"{len(diff['files'])} file(s), {len(diff['diff_text'] or '')} chars",
    )

    # The judge is asked for COMPLETE corrected files, so it has to be given
    # the complete current ones. With only a diff hunk it can't honestly
    # produce a whole file and correctly escalates instead.
    root = workspace.repo_root()
    sources = workspace.read_sources(root, diff["files"])
    unreadable = [s["path"] for s in sources if s["content"] is None]
    db.log_event(
        pr_number,
        "files read",
        "failed" if unreadable else "ok",
        f"{len(sources) - len(unreadable)}/{len(sources)} file(s) from {root}"
        + (f"; could not read {', '.join(unreadable)}" if unreadable else ""),
    )

    # Loaded once and passed down, so a retry can't silently judge against
    # different rules than the first attempt did.
    rules, rules_source = load_rules()
    db.log_event(
        pr_number,
        "rules loaded",
        "failed" if rules_source == "built-in" else "ok",
        f"{len(rules)} chars from {rules_source}"
        + (
            "; the rules file was missing, so verdicts may differ"
            if rules_source == "built-in"
            else ""
        ),
    )

    receipts = db.recent_receipts(limit=10)
    db.log_event(
        pr_number,
        "decision made",
        "running",
        f"{backend.name} backend, memory: {len(receipts)} receipt(s)",
    )

    verdict = judge(
        diff, backend=backend, rules=rules, receipts=receipts, sources=sources
    )

    detail = f"{verdict.decision} (confidence {verdict.confidence:.2f})"
    if verdict.is_fix:
        detail += f", rewrote {', '.join(verdict.changed_paths)}"
    db.log_event(pr_number, "decision made", "ok", detail)

    # Only a 'fix' touches the code, so only a 'fix' has anything to prove.
    # allow/escalate leave tests_passed NULL: "not run", not "failed".
    tests_passed, attempts = None, 0
    if verdict.is_fix:
        outcome = run_fix_loop(
            diff, verdict, backend=backend, rules=rules, receipts=receipts, root=root
        )
        verdict, tests_passed, attempts = (
            outcome.verdict,
            outcome.tests_passed,
            outcome.attempts,
        )

    db.write_verdict(
        pr_number=pr_number,
        decision=verdict.decision,
        confidence=verdict.confidence,
        reason=verdict.reason,
        fixed_code=verdict.fixed_code,
        tests_passed=tests_passed,
        attempts=attempts,
    )

    print(f"PR #{pr_number}: {verdict.decision} (confidence {verdict.confidence:.2f})")
    if attempts:
        print(f"  tests_passed={tests_passed} after {attempts} attempt(s)")
    print(verdict.reason)
    for changed in verdict.fixed_code:
        print(f"  rewrote {changed['path']} ({len(changed['content'])} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
