# /merge — Tejas

Pushes the fix, merges or labels, posts the receipt comment, pings Slack,
writes `RECEIPTS`.

## The gate (from CLAUDE.md)

Read the newest `VERDICTS` row for the PR, then:

**Merge only if** `decision` is `allow`, **or** `decision` is `fix` **and**
`tests_passed` is true.

Otherwise — including `escalate`, and including a `fix` whose tests did not
pass — label `needs-human` and **do not merge**.

`tests_passed` is three-valued on purpose:

| Value | Meaning |
| --- | --- |
| `true` | the engine applied the fix and the suite passed |
| `false` | the engine applied the fix and the suite failed |
| `NULL` | not run |

`NULL` is not `false`. Both refuse the merge, but only `false` means a fix was
actually attempted and failed.

## What you write

`RECEIPTS`: `pr_number`, `decision`, `reason`, `merged`, `receipt_url`.

The engine reads this back as its memory — a lookup over recent `RECEIPTS` goes
into the Claude prompt, so past decisions keep future ones consistent. Write an
honest `reason`; it is what Claude sees next time.

Plus one `PR_EVENTS` row per step (`merged`, etc.).

Exact DDL: `sql/setup.sql`. Don't change a table without telling the team.

## Secrets

`GITHUB_TOKEN`, `SLACK_WEBHOOK_URL`, Snowflake connection details. Never committed.

## Status

Not started. The engine already writes `VERDICTS` rows you can read today —
run `python engine/run.py 101` after `python engine/seed_fake_rows.py`.
