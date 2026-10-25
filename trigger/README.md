# /trigger — Erik

The GitHub Action on `pull_request`. Writes `PR_DIFFS`, then runs the engine and merge.

## Contract (from CLAUDE.md)

You **write** `PR_DIFFS`, one row per push to a PR:

| Column | Type | Notes |
| --- | --- | --- |
| `pr_number` | NUMBER | |
| `repo` | STRING | e.g. `chezzz439-arch/frosty` |
| `branch` | STRING | the PR's head branch |
| `files` | ARRAY | repo-relative paths the PR changes |
| `diff_text` | STRING | the unified diff |
| `created_at` | TIMESTAMP_NTZ | defaults to now |

The engine reads the **newest** row for a `pr_number`, so pushing twice is safe.

You also write one `PR_EVENTS` row per step (`step`, `status` of `ok`/`failed`/`running`, `detail`).

Exact DDL: `sql/setup.sql`. Don't change a table without telling the team.

## Then run

```
python engine/run.py <pr_number>
python merge/run.py <pr_number>
```

## Secrets the Action needs

Never commit these; set them as GitHub repository secrets.

- `GITHUB_TOKEN` — provided by Actions
- `SLACK_WEBHOOK_URL` — for /merge
- Snowflake connection details
- **One of** `ANTHROPIC_API_KEY` **or** `CLAUDE_CODE_OAUTH_TOKEN` for the engine's judge.
  `CLAUDE_CODE_OAUTH_TOKEN` is generated once with `claude setup-token` and
  authenticates with a Claude Pro/Max/Team subscription. Set only one: an API
  key outranks the OAuth token in Claude Code's credential precedence.
  See `engine/backends.py` for how the engine picks.

## Status

Not started. The engine currently runs against hand-inserted rows from
`engine/seed_fake_rows.py`, so it isn't blocked on this.
