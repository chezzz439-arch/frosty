# Progress — engine + sql (Kaushik)

Last updated 25 Oct 2026. Covers `/engine` and `/sql` only; other folders are
their owners' to report on.

| # | Task | Status |
|---|------|--------|
| 1 | Repo scaffold + CLAUDE.md | Done |
| 2 | `sql/setup.sql` written and run | Done |
| 3 | Decision step (Claude decides) | Done |
| 4 | Self-correction loop (rewrite, test, retry ×3) | Done |
| 5 | Write VERDICTS + PR_EVENTS | Done |
| 6 | One command: `python3 engine/run.py <pr>` | Done |

**Done means verified running, not written.** What has not happened: the engine
has never run inside a GitHub Action. The CI credential path was checked
locally by forcing the engine to use only environment variables, which is as
close as can be got from a laptop. Erik's first workflow run is the real test.

## Verified live

All three planted PRs, against Snowflake, on the Claude Code CLI backend:

| PR | Decision | Confidence | Notes |
|----|----------|-----------|-------|
| 101 violation | `fix` | 0.97 | `tests_passed=true`, `attempts=1` |
| 102 decoy | `allow` | 0.95 | never flagged |
| 103 ambiguous | `escalate` | 0.82 | same both runs |

81 tests pass with no network, no credentials and no warehouse spend.

## Running it

```bash
pip install -r engine/requirements.txt
python3 engine/run.py <pr_number>          # judge a PR
python3 engine/run.py <pr_number> --show   # print the trail, judge nothing
```

Locally, seed demo rows and a demo checkout first:

```bash
python3 engine/seed_fake_rows.py
PR_GUARDIAN_REPO=engine/.local/demo-repo python3 engine/run.py 101
```

In the Action no `PR_GUARDIAN_REPO` is needed — it defaults to the working
directory, which is the checked-out PR branch.

### Environment

| Variable | Default | Purpose |
|----------|---------|---------|
| `PR_GUARDIAN_BACKEND` | `api` if `ANTHROPIC_API_KEY` is set, else `claude-cli` | which backend judges |
| `PR_GUARDIAN_DB_BACKEND` | `snowflake` | `sqlite` for offline work |
| `PR_GUARDIAN_REPO` | current directory | the checked-out PR branch |
| `PR_GUARDIAN_TEST_CMD` | stdlib unittest discovery | how the fix loop runs tests |
| `PR_GUARDIAN_SNOWFLAKE_CONNECTION` | `hackathon2` | snow CLI connection, laptop only |

## For Erik — the Action

Four repository secrets:

| Secret | Value |
|--------|-------|
| `SNOWFLAKE_ACCOUNT` | `FQWENHP-EC45741` |
| `SNOWFLAKE_USER` | `CHEZZZ439@GMAIL.COM` |
| `SNOWFLAKE_PRIVATE_KEY` | contents of `rsa_key.p8`, including the BEGIN/END lines |
| `CLAUDE_CODE_OAUTH_TOKEN` | output of `claude setup-token` |

```yaml
- run: pip install -r engine/requirements.txt
- run: python3 engine/run.py ${{ github.event.pull_request.number }}
  env:
    CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
    SNOWFLAKE_ACCOUNT:       ${{ secrets.SNOWFLAKE_ACCOUNT }}
    SNOWFLAKE_USER:          ${{ secrets.SNOWFLAKE_USER }}
    SNOWFLAKE_PRIVATE_KEY:   ${{ secrets.SNOWFLAKE_PRIVATE_KEY }}
```

`SNOWFLAKE_WAREHOUSE`, `SNOWFLAKE_DATABASE` and `SNOWFLAKE_SCHEMA` are
supported but unnecessary — they default to `PR_GUARDIAN_WH`, `PR_GUARDIAN`
and `CORE`.

Two things that will cost an hour if missed:

- **`SNOWFLAKE_USER` is the email, not `chezzz439`.** `CURRENT_USER()` reports
  `chezzz439`, but the JWT subject has to match the name the key is registered
  against. Using the short name fails with `390144: JWT token is invalid`.
- **Do not also set `ANTHROPIC_API_KEY`.** An API key outranks
  `CLAUDE_CODE_OAUTH_TOKEN` in the credential order, so setting both bills the
  key instead of the subscription and fails confusingly if the key is stale.

## For Tejas — the merge gate

`VERDICTS` has real rows. Merge if:

```
decision = 'allow'
  OR (decision = 'fix' AND tests_passed = TRUE)
```

`tests_passed` is three-valued and the distinction matters:

| Value | Meaning |
|-------|---------|
| `TRUE` | fix applied, suite green |
| `FALSE` | fix applied, suite red |
| `NULL` | not run — `allow` and `escalate` never touch code |

`NULL` is not `FALSE`. Treat it as "not proved" and refuse.

An `escalate` row may carry an empty `fixed_code` even when a fix was
attempted: after three failed attempts the code is dropped deliberately, so
nobody can apply a patch that failed its own tests.

## Known gaps

- The engine's test command is a placeholder (stdlib unittest discovery),
  marked `# ASSUMPTION:` in `engine/workspace.py`. It should become whatever
  `/demo-data` actually uses.
- `FALLBACK_RULES` in `engine/judge.py` is standing in for
  `demo-data/rules.md`. PR 103's escalation depends on two of those rules.
- The root `README.md` still describes SQL and data-pipeline guarding, which
  contradicts the app-code-PR system we built. Someone should rewrite it.
- `SNOWFLAKE_PRIVATE_KEY` as a repo secret means anyone who can push a
  workflow can print it. Fine for a hackathon; a real deployment wants a
  separate CI key pair registered as `RSA_PUBLIC_KEY_2` so it can be rotated.
