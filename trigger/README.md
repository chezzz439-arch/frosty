# /trigger — Erik

The GitHub Action on `pull_request`. Writes `PR_DIFFS`, then runs the engine and merge.

```
guardian.yml        the workflow (template: make_demo_repo.sh copies it into the demo shop repo)
send_diff.py        step 1: PR diff -> PR_DIFFS + PR_EVENTS
sf.py               Snowflake connection (key-pair preferred), reusable by anyone
keygen.sh           makes the key pair for the Action's Snowflake sign-in
test_send_diff.py   python trigger/test_send_diff.py
```

## Contract (from CLAUDE.md)

Writes `PR_DIFFS` (`pr_number`, `repo`, `branch`, `files` ARRAY, `diff_text`; `created_at` defaults),
one row per push. The engine reads the newest row per PR. Writes `PR_EVENTS` `pr opened` / `ok` and
`diff written` / `ok` (or `failed` with the error). The engine logs `diff read` when it picks the row up.

## Where it writes

`--store auto` (the default) writes to **every store that's available**:
- **local**: `engine/db.py`'s own functions, at `PR_GUARDIAN_DB`. That's what the engine reads today,
  so the chain works before the Snowflake swap.
- **snowflake**: `PR_GUARDIAN.CORE` on warehouse `PR_GUARDIAN_WH` (from `sql/setup.sql`), when
  `SNOWFLAKE_ACCOUNT` is set or `--connection` is given.

When `engine/db.py` moves to Snowflake, nothing here changes.

## Run it

```
python trigger/send_diff.py --planted violation              # 101; decoy 102, ambiguous 103
python trigger/send_diff.py --planted decoy --connection <snow conn>   # also into Snowflake
python trigger/send_diff.py --repo <owner>/frosty-demo-shop --pr 4     # any real PR (manual backup)
python trigger/send_diff.py --from-event                     # what the Action runs
... --dry-run                                                # print, write nothing
```

## What the Action does (`guardian.yml`)

Checks out the PR (`repo/`) and frosty at `vars.GUARDIAN_REF` (`guardian/`, default `main`), then:
1. `python guardian/trigger/send_diff.py --from-event`
2. `python guardian/engine/run.py $PR_NUMBER`
3. `python guardian/merge/run.py $PR_NUMBER`

Steps 2 and 3 are skipped until those files exist on `GUARDIAN_REF`. Environment it provides:

| Var | For |
| --- | --- |
| `PR_NUMBER`, `PR_REPO`, `PR_BRANCH` | everyone |
| `PR_GUARDIAN_RULES` = frosty's `demo-data/rules.md` | engine. The trusted copy, so a PR can't rewrite its own rules. |
| `PR_GUARDIAN_REPO_DIR`, `PR_GUARDIAN_TEST_CMD` = `python -m pytest -q` | engine sandbox. **ASSUMPTION**: names to confirm with Kaushik. |
| `PR_GUARDIAN_DB` | shared local db for trigger + engine |
| `ANTHROPIC_API_KEY` **or** `CLAUDE_CODE_OAUTH_TOKEN` | engine judge. The CLI is installed only when the token is the one set. |
| `GITHUB_TOKEN`, `GH_TOKEN`, `SLACK_WEBHOOK_URL` | merge |
| `SNOWFLAKE_*` | everyone |

The PR's tests run with these secrets in the environment. That's fine for a demo repo only the team
pushes to. Don't point this workflow at a repo that takes PRs from forks.
