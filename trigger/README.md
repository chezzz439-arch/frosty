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

`--store auto` (the default) writes **through `engine/db.py`** when it's checked out, so the row
lands in whatever store the engine reads: Snowflake `PR_GUARDIAN.CORE` by default, using the snow
connection named in `PR_GUARDIAN_SNOWFLAKE_CONNECTION` (or SQLite with `PR_GUARDIAN_DB_BACKEND=sqlite`).
Without `engine/`, it writes to Snowflake directly with `sf.py`. Never both, so rows aren't doubled.

## Run it

```
export PR_GUARDIAN_SNOWFLAKE_CONNECTION=<your snow connection>   # `snow connection list`
python trigger/send_diff.py --planted violation              # 101; decoy 102, ambiguous 103
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
| `PR_GUARDIAN_REPO_DIR`, `PR_GUARDIAN_TEST_CMD` = `python -m pytest -q` | engine: the PR tree to read, sandbox and test (both names confirmed in `engine/workspace.py`) |
| `ANTHROPIC_API_KEY` **or** `CLAUDE_CODE_OAUTH_TOKEN` | engine judge. The CLI is installed only when the token is the one set. |
| `GITHUB_TOKEN`, `GH_TOKEN`, `SLACK_WEBHOOK_URL` | merge |
| `SNOWFLAKE_*` | everyone |

The PR's tests run with these secrets in the environment. That's fine for a demo repo only the team
pushes to. Don't point this workflow at a repo that takes PRs from forks.
