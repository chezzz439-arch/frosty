# /sql — Kaushik

`setup.sql`: the warehouse, database, schema and the four tables every part
reads and writes.

## Create them

```
snow sql -f sql/setup.sql -c <your connection>
```

Everything lands in `PR_GUARDIAN.CORE`, warehouse `PR_GUARDIAN_WH`.

## The four tables

| Table | Written by | Read by |
| --- | --- | --- |
| `PR_DIFFS` | trigger | engine |
| `VERDICTS` | engine | merge |
| `RECEIPTS` | merge | app, engine |
| `PR_EVENTS` | everyone | app |

Two rules the whole system depends on:

- `VERDICTS.decision` is exactly one of `fix`, `allow`, `escalate`.
- Merge only if `decision` is `allow`, or `decision` is `fix` and
  `tests_passed` is true.

## This file is shared

It is the contract between four people's code. **Change a table only after
telling the team** — a column rename here silently breaks someone else's
folder.

## Status

See the top-level README for whether these tables exist in Snowflake yet. The
engine does not require them to run: `engine/db.py` keeps every table read and
write behind four functions and is currently backed by a local SQLite file
using these exact table and column names, so the backend can be swapped without
touching anything else in `engine/`.
