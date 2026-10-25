# CLAUDE.md: PR Guardian (Purdue Hacktoberfest)

An agent that guards a codebase. A PR opens, a GitHub Action wakes the agent, it reads the diff, checks it against our rules, and decides: fix, allow, or escalate. On a real violation it rewrites the code in a sandbox, runs the tests, retries until green, merges, posts a receipt, and pings Slack. The point is judgment, not a script.

Snowflake hosts the live dashboard (Streamlit in Snowflake) and stores every step. Everything else runs in the GitHub Action. Our Snowflake trial account has no Cortex and no outbound network access, so nothing that calls GitHub, Claude or Slack runs inside Snowflake.

## The core loop

PR opens -> read diff -> check rules -> decide (fix | allow | escalate) -> if fix: sandbox -> rewrite -> test -> retry until green (max 3) -> merge -> receipt -> Slack

## The three decisions

- High confidence + real violation -> fix in the sandbox, test, merge.
- Confident it's fine (a fake test card number in a test file) -> allow, log why.
- Unsure, risky, or 3 failed fix attempts -> escalate: label "needs-human", do NOT merge.

A wrong fix that merges is worse than an escalation. When in doubt, escalate.

## Honesty rules (read this)

- If you don't know something, say so. Never invent API names, endpoints, flags, env vars, file paths or Snowflake syntax.
- Before writing integration code, check the official docs: Snowflake CLI, Streamlit in Snowflake, Anthropic API, GitHub REST API, Slack webhooks.
- Mark every assumption in code with a comment starting `# ASSUMPTION:`.
- When choosing between approaches, say which you picked and why.
- If a command, table or value isn't in the repo or the docs, verify it or ask. Don't assume it exists.

## Folders and owners

Only edit your own folder. `sql/setup.sql` is shared: change a table only after telling the team.

| Folder | Owner | What it does |
| --- | --- | --- |
| `/engine` | Kaushik | Reads `PR_DIFFS`, calls Claude, fixes and tests in a temp copy of the repo, writes `VERDICTS` |
| `/sql` | Kaushik | `setup.sql`: database, schema, warehouse, the four tables |
| `/trigger` | Erik | GitHub Action on `pull_request`: writes `PR_DIFFS`, runs engine then merge |
| `/demo-data` | Erik | Demo app, test suite, rules file, the three planted PRs |
| `/app` | Rohan | Streamlit app hosted on Snowflake: live feed, receipts, manual backup button |
| `/merge` | Tejas | Pushes fix, merges or labels, receipt comment, Slack, writes `RECEIPTS` |

## The contract: four Snowflake tables (PR_GUARDIAN.CORE, warehouse PR_GUARDIAN_WH)

The parts never call each other. They only read and write these tables. Build against hand-inserted fake rows until the real part lands. Exact DDL is in sql/setup.sql.

| Table | Written by | Read by | Columns |
| --- | --- | --- | --- |
| `PR_DIFFS` | trigger | engine | pr_number, repo, branch, files, diff_text, created_at |
| `VERDICTS` | engine | merge | pr_number, decision, confidence, reason, fixed_code, tests_passed, attempts, created_at |
| `RECEIPTS` | merge | app, engine | pr_number, decision, reason, merged, receipt_url, created_at |
| `PR_EVENTS` | everyone | app | pr_number, step, status, detail, ts |

Rules:
- `VERDICTS.decision` is exactly one of `fix`, `allow`, `escalate`.
- Merge only if decision is `allow`, or decision is `fix` and `tests_passed` is true.
- Every script writes one `PR_EVENTS` row per step ("diff read", "decision made", "fix attempt 2", "tests passed", "merged"). The dashboard shows these.

## The planted PRs (the demo's whole argument)

- Violation: app code that writes a card number to the logs. Guardian must catch and fix it.
- Decoy: a fake test card number used as test data in a test file. Guardian must ALLOW it. Catching the decoy = failing the demo. Context beats keyword matching.
- Ambiguous: a change Guardian isn't sure about. Must escalate.

## Secrets (env vars / GitHub secrets, never committed)

`ANTHROPIC_API_KEY`, `SLACK_WEBHOOK_URL`, `GITHUB_TOKEN`, and the Snowflake connection details. Locally, Snowflake uses the snow CLI connection.

## Scope: build vs pitch

BUILD: the full loop, the three decisions, the three planted PRs, the self-correcting retry, receipts, Slack, the live dashboard, the manual backup button, memory (SQL lookup over `RECEIPTS` added to the Claude prompt).
PITCH ONLY, do not build: multi-repo, security-advisory triggers, rules learned automatically, Jira tickets. If asked, say "that's where it's going."

## How we work (hackathon mode)

- Build against fake rows first so nobody waits.
- Small commits. Run tests before pushing.
- Ship the loop end to end first, then make ONE step impressive: the self-correction.
- Streamlit app: only use packages Streamlit in Snowflake supports; use `st.connection("snowflake")`.

## Commands (update these as the repo takes shape)

- Create tables: `snow sql -f sql/setup.sql -c <your connection>`
- Run the engine: `python engine/run.py <pr_number>` (ASSUMPTION: confirm once built)
- Run merge: `python merge/run.py <pr_number>` (ASSUMPTION: confirm once built)
- Dashboard locally: `streamlit run app/streamlit_app.py`
- Deploy dashboard: `snow streamlit deploy --replace --open -c <your connection>`

If any of these are wrong, fix this section. An out-of-date command costs everyone time.
