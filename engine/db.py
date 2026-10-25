"""The engine's only door to the four tables.

Right now this is backed by a local SQLite file so the engine can be built and
tested before Snowflake is reachable. The table and column names are exactly
the ones in sql/setup.sql, so swapping the backend to
snowflake-connector-python means rewriting the bodies of the functions below
and nothing else in engine/.

Everything outside this module uses only:

    read_diff(pr_number)        -> dict | None
    write_verdict(...)          -> None
    log_event(pr_number, ...)   -> None
    recent_receipts(limit)      -> list[dict]

Type mapping (Snowflake -> SQLite), since SQLite has no ARRAY/VARIANT/BOOLEAN:

    NUMBER        -> INTEGER
    STRING        -> TEXT
    FLOAT         -> REAL
    ARRAY         -> TEXT holding a JSON array   (files)
    VARIANT       -> TEXT holding a JSON value   (fixed_code)
    BOOLEAN       -> INTEGER 0/1                 (tests_passed, merged)
    TIMESTAMP_NTZ -> TEXT, CURRENT_TIMESTAMP default

This module owns that conversion in both directions, so callers always deal in
real Python types: `files` and `fixed_code` are list/dict, `tests_passed` and
`merged` are bool or None.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

# The three legal values of VERDICTS.decision, per the contract in CLAUDE.md.
DECISIONS = ("fix", "allow", "escalate")

# ASSUMPTION: a local file is fine for the pre-Snowflake phase. Override with
# PR_GUARDIAN_DB to point tests or a second terminal at a different file.
DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), ".local", "pr_guardian.db")

# Mirrors sql/setup.sql. Kept here (rather than parsed from the .sql) because
# the Snowflake DDL isn't valid SQLite -- ARRAY, VARIANT and TIMESTAMP_NTZ have
# no SQLite equivalent. If sql/setup.sql changes, change this too.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS PR_DIFFS (
  pr_number   INTEGER,
  repo        TEXT,
  branch      TEXT,
  files       TEXT,
  diff_text   TEXT,
  created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS VERDICTS (
  pr_number     INTEGER,
  decision      TEXT,
  confidence    REAL,
  reason        TEXT,
  fixed_code    TEXT,
  tests_passed  INTEGER,
  attempts      INTEGER,
  created_at    TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS RECEIPTS (
  pr_number    INTEGER,
  decision     TEXT,
  reason       TEXT,
  merged       INTEGER,
  receipt_url  TEXT,
  created_at   TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS PR_EVENTS (
  pr_number  INTEGER,
  step       TEXT,
  status     TEXT,
  detail     TEXT,
  ts         TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

# PR_EVENTS.status, per CLAUDE.md.
EVENT_STATUSES = ("ok", "failed", "running")


def db_path() -> str:
    return os.environ.get("PR_GUARDIAN_DB") or DEFAULT_DB_PATH


def connect() -> sqlite3.Connection:
    """Open the local database, creating the file and tables if needed."""
    path = db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _to_bool(value: Any) -> bool | None:
    """SQLite stores booleans as 0/1; give callers a real bool (or None)."""
    return None if value is None else bool(value)


def _load_json(value: Any, fallback: Any) -> Any:
    """Decode a JSON column, tolerating NULL and anything malformed."""
    if value is None or value == "":
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        # A hand-inserted fake row may hold a bare string rather than JSON.
        # Surface it instead of crashing the engine on demo day.
        return fallback


# --- reads -------------------------------------------------------------------


def read_diff(pr_number: int) -> dict | None:
    """The diff the trigger wrote for this PR, or None if it hasn't landed yet.

    Returns the newest row if the trigger wrote more than once (a PR can be
    pushed to repeatedly), so the engine always judges the latest code.
    """
    with connect() as conn:
        row = conn.execute(
            """
            SELECT pr_number, repo, branch, files, diff_text, created_at
              FROM PR_DIFFS
             WHERE pr_number = ?
             ORDER BY created_at DESC, rowid DESC
             LIMIT 1
            """,
            (pr_number,),
        ).fetchone()

    if row is None:
        return None

    diff = dict(row)
    diff["files"] = _load_json(diff["files"], [])
    return diff


def recent_receipts(limit: int = 10) -> list[dict]:
    """Past decisions, newest first -- the engine's memory.

    CLAUDE.md scopes memory as "SQL lookup over RECEIPTS added to the Claude
    prompt". This is that lookup. Returns [] before /merge lands, which is
    fine: the judge just runs without prior context.
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT pr_number, decision, reason, merged, receipt_url, created_at
              FROM RECEIPTS
             ORDER BY created_at DESC, rowid DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()

    receipts = []
    for row in rows:
        receipt = dict(row)
        receipt["merged"] = _to_bool(receipt["merged"])
        receipts.append(receipt)
    return receipts


# --- writes ------------------------------------------------------------------


def write_verdict(
    pr_number: int,
    decision: str,
    confidence: float,
    reason: str,
    fixed_code: list[dict] | None = None,
    tests_passed: bool | None = None,
    attempts: int = 0,
) -> None:
    """Record the engine's verdict -- the row /merge reads to decide what to do.

    `decision` is validated here rather than trusted: a value outside
    fix/allow/escalate would quietly break /merge's gate, and a loud failure in
    our own folder is cheaper than a silent one in someone else's.
    """
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}, got {decision!r}")

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO VERDICTS
                   (pr_number, decision, confidence, reason,
                    fixed_code, tests_passed, attempts)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pr_number,
                decision,
                confidence,
                reason,
                json.dumps(fixed_code if fixed_code is not None else []),
                None if tests_passed is None else int(tests_passed),
                attempts,
            ),
        )


def log_event(pr_number: int, step: str, status: str, detail: str = "") -> None:
    """One row per step of the loop. The dashboard reads these live.

    An unknown status is written anyway rather than raised on -- losing a step
    from the dashboard is worse than an off-contract string in it.
    """
    with connect() as conn:
        conn.execute(
            "INSERT INTO PR_EVENTS (pr_number, step, status, detail) VALUES (?, ?, ?, ?)",
            (pr_number, step, status, detail),
        )


# --- seeding fake rows (local only) ------------------------------------------


def insert_diff(
    pr_number: int,
    repo: str,
    branch: str,
    files: list[str],
    diff_text: str,
) -> None:
    """Write a PR_DIFFS row.

    /trigger owns this table in production; this exists so the engine can be
    driven from hand-made rows while /trigger is still being built, per
    "build against fake rows first so nobody waits".
    """
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO PR_DIFFS (pr_number, repo, branch, files, diff_text)
            VALUES (?, ?, ?, ?, ?)
            """,
            (pr_number, repo, branch, json.dumps(files), diff_text),
        )


def insert_receipt(
    pr_number: int,
    decision: str,
    reason: str,
    merged: bool,
    receipt_url: str = "",
) -> None:
    """Write a RECEIPTS row. /merge owns this table; local seeding only, so the
    memory lookup has something to return before /merge lands."""
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO RECEIPTS (pr_number, decision, reason, merged, receipt_url)
            VALUES (?, ?, ?, ?, ?)
            """,
            (pr_number, decision, reason, int(merged), receipt_url),
        )


def events(pr_number: int) -> list[dict]:
    """Every step logged for one PR, oldest first. Used by tests and by
    `run.py --show`; the real dashboard reads PR_EVENTS from Snowflake."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT pr_number, step, status, detail, ts
              FROM PR_EVENTS
             WHERE pr_number = ?
             ORDER BY ts ASC, rowid ASC
            """,
            (pr_number,),
        ).fetchall()
    return [dict(row) for row in rows]


def verdicts(pr_number: int) -> list[dict]:
    """Every verdict for one PR, newest first. /merge reads VERDICTS from
    Snowflake; this is for tests and local inspection."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT pr_number, decision, confidence, reason, fixed_code,
                   tests_passed, attempts, created_at
              FROM VERDICTS
             WHERE pr_number = ?
             ORDER BY created_at DESC, rowid DESC
            """,
            (pr_number,),
        ).fetchall()

    out = []
    for row in rows:
        verdict = dict(row)
        verdict["fixed_code"] = _load_json(verdict["fixed_code"], [])
        verdict["tests_passed"] = _to_bool(verdict["tests_passed"])
        out.append(verdict)
    return out
