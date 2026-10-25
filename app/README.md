# /app — Rohan

Streamlit in Snowflake: live feed, receipts, manual backup button.

## What you read

| Table | Shows |
| --- | --- |
| `PR_EVENTS` | the live feed — one row per step, newest last |
| `VERDICTS` | the decision, confidence, reason, whether tests passed, attempts |
| `RECEIPTS` | what was merged or escalated, with a link |
| `PR_DIFFS` | the diff being judged |

`PR_EVENTS.status` is one of `ok`, `failed`, `running`. A `running` row with
nothing after it means a step died mid-way — worth showing differently.

Steps are free text, written by whichever part logged them, e.g.
`diff read`, `decision made`, `fix attempt 2`, `tests passed`, `merged`.

Exact DDL: `sql/setup.sql`. Don't change a table without telling the team.

## Constraints (from CLAUDE.md)

- Only packages Streamlit in Snowflake supports.
- Use `st.connection("snowflake")`.
- The Snowflake trial has no outbound network access, so the dashboard can
  read and display but cannot call GitHub, Claude or Slack. The "manual backup
  button" has to work within that.

## Commands

```
streamlit run app/streamlit_app.py
snow streamlit deploy --replace --open -c <your connection>
```

## Status

Not started. The four tables may not exist in Snowflake yet — see `sql/README.md`.
