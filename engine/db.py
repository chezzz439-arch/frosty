"""The engine's only door to the four tables.

Snowflake is the real backend. SQLite is kept as a second one so the test
suite still runs with no network, no credentials and no warehouse spend --
the same seam engine/backends.py uses for the judge.

    PR_GUARDIAN_DB_BACKEND=snowflake   (default)
    PR_GUARDIAN_DB_BACKEND=sqlite      (tests, offline work)

Everything outside this module uses only:

    read_diff(pr_number)        -> dict | None
    write_verdict(...)          -> None
    log_event(pr_number, ...)   -> None
    recent_receipts(limit)      -> list[dict]

Both backends return the same Python types, so nothing above this file knows
which one is live: `files` and `fixed_code` are list/dict, `tests_passed` and
`merged` are bool or None, and every timestamp is a string.

Two things about Snowflake that shaped the code below, both verified against
the live tables rather than assumed:

  * ARRAY and VARIANT columns cannot be bound directly. PARSE_JSON is illegal
    in a VALUES clause ("Invalid expression [PARSE_JSON(...)] in VALUES
    clause"), so every insert touching `files` or `fixed_code` is written as
    INSERT ... SELECT, which does allow it.
  * Those columns read back as JSON *strings*, so they are parsed on the way
    out -- the same _load_json the SQLite path uses.

Type mapping (Snowflake -> SQLite), since SQLite has no ARRAY/VARIANT/BOOLEAN:

    NUMBER        -> INTEGER
    STRING        -> TEXT
    FLOAT         -> REAL
    ARRAY         -> TEXT holding a JSON array   (files)
    VARIANT       -> TEXT holding a JSON value   (fixed_code)
    BOOLEAN       -> INTEGER 0/1                 (tests_passed, merged)
    TIMESTAMP_NTZ -> TEXT, CURRENT_TIMESTAMP default
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

# The three legal values of VERDICTS.decision, per the contract in CLAUDE.md.
DECISIONS = ("fix", "allow", "escalate")

SNOWFLAKE = "snowflake"
SQLITE = "sqlite"

# The snow CLI connection, defined in ~/Library/Application Support/snowflake/
# config.toml with key-pair auth. Overridable so a teammate with a differently
# named connection doesn't have to edit this file.
DEFAULT_CONNECTION = "hackathon2"

# The connection's own defaults point at SNOWFLAKE_LEARNING_DB/WH, so the
# engine names its context explicitly instead of inheriting whatever the
# config happens to say.
DATABASE = "PR_GUARDIAN"
SCHEMA = "CORE"
WAREHOUSE = "PR_GUARDIAN_WH"

# ASSUMPTION: a local file is fine for the SQLite path. Override with
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


def backend() -> str:
    """Which store is live. Snowflake unless something asks for SQLite."""
    choice = (os.environ.get("PR_GUARDIAN_DB_BACKEND") or SNOWFLAKE).strip().lower()
    if choice not in (SNOWFLAKE, SQLITE):
        raise ValueError(
            f"PR_GUARDIAN_DB_BACKEND must be {SNOWFLAKE!r} or {SQLITE!r}, got {choice!r}"
        )
    return choice


def connection_name() -> str:
    return os.environ.get("PR_GUARDIAN_SNOWFLAKE_CONNECTION") or DEFAULT_CONNECTION


def db_path() -> str:
    return os.environ.get("PR_GUARDIAN_DB") or DEFAULT_DB_PATH


def connect() -> sqlite3.Connection:
    """Open the local SQLite database, creating the file and tables if needed."""
    path = db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


# One connection per process, reused. Opening a Snowflake session costs about
# a second, and a single run writes seven or eight PR_EVENTS rows -- paying
# that each time would be most of the engine's runtime on demo day.
_SNOWFLAKE_CONN: Any = None


def snowflake_connection() -> Any:
    """The shared Snowflake session, opened on first use.

    Nothing closes this deliberately: the process is a short-lived CLI run or
    one Action step, and the connector closes the session on exit.
    """
    global _SNOWFLAKE_CONN
    if _SNOWFLAKE_CONN is not None:
        return _SNOWFLAKE_CONN

    try:
        import snowflake.connector
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError(
            "snowflake-connector-python is not installed. "
            "pip install -r engine/requirements.txt, or set "
            "PR_GUARDIAN_DB_BACKEND=sqlite to run without Snowflake."
        ) from exc

    _SNOWFLAKE_CONN = snowflake.connector.connect(
        connection_name=connection_name(),
        database=DATABASE,
        schema=SCHEMA,
        warehouse=WAREHOUSE,
    )
    return _SNOWFLAKE_CONN


def _query(sql: str, params: tuple = ()) -> list[dict]:
    """Run a Snowflake query and return rows as dicts keyed by column name."""
    cursor = snowflake_connection().cursor()
    try:
        cursor.execute(sql, params)
        columns = [description[0].lower() for description in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        cursor.close()


def _execute(sql: str, params: tuple = ()) -> None:
    """Run a Snowflake statement that returns nothing worth reading."""
    cursor = snowflake_connection().cursor()
    try:
        cursor.execute(sql, params)
    finally:
        cursor.close()


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
    if backend() == SNOWFLAKE:
        # Snowflake has no rowid to break a tie on, but created_at is written
        # with sub-second precision, so two pushes can't share a timestamp.
        rows = _query(
            """
            SELECT pr_number, repo, branch, files, diff_text,
                   TO_VARCHAR(created_at) AS created_at
              FROM PR_DIFFS
             WHERE pr_number = %s
             ORDER BY created_at DESC
             LIMIT 1
            """,
            (pr_number,),
        )
        if not rows:
            return None
        diff = rows[0]
        diff["files"] = _load_json(diff["files"], [])
        return diff

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
    if backend() == SNOWFLAKE:
        receipts = _query(
            """
            SELECT pr_number, decision, reason, merged, receipt_url,
                   TO_VARCHAR(created_at) AS created_at
              FROM RECEIPTS
             ORDER BY created_at DESC
             LIMIT %s
            """,
            (limit,),
        )
        # Snowflake BOOLEAN already arrives as a real bool or None.
        return receipts

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

    if backend() == SNOWFLAKE:
        # INSERT ... SELECT, not VALUES: fixed_code is a VARIANT and PARSE_JSON
        # is rejected inside a VALUES clause.
        _execute(
            """
            INSERT INTO VERDICTS
                   (pr_number, decision, confidence, reason,
                    fixed_code, tests_passed, attempts)
            SELECT %s, %s, %s, %s, PARSE_JSON(%s), %s, %s
            """,
            (
                pr_number,
                decision,
                confidence,
                reason,
                json.dumps(fixed_code if fixed_code is not None else []),
                tests_passed,
                attempts,
            ),
        )
        return

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
    if backend() == SNOWFLAKE:
        _execute(
            "INSERT INTO PR_EVENTS (pr_number, step, status, detail) "
            "VALUES (%s, %s, %s, %s)",
            (pr_number, step, status, detail),
        )
        return

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
    if backend() == SNOWFLAKE:
        # INSERT ... SELECT again: `files` is an ARRAY.
        _execute(
            """
            INSERT INTO PR_DIFFS (pr_number, repo, branch, files, diff_text)
            SELECT %s, %s, %s, PARSE_JSON(%s), %s
            """,
            (pr_number, repo, branch, json.dumps(files), diff_text),
        )
        return

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
    if backend() == SNOWFLAKE:
        _execute(
            """
            INSERT INTO RECEIPTS (pr_number, decision, reason, merged, receipt_url)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (pr_number, decision, reason, merged, receipt_url),
        )
        return

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
    if backend() == SNOWFLAKE:
        return _query(
            """
            SELECT pr_number, step, status, detail, TO_VARCHAR(ts) AS ts
              FROM PR_EVENTS
             WHERE pr_number = %s
             ORDER BY ts ASC
            """,
            (pr_number,),
        )

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
    if backend() == SNOWFLAKE:
        rows = _query(
            """
            SELECT pr_number, decision, confidence, reason, fixed_code,
                   tests_passed, attempts, TO_VARCHAR(created_at) AS created_at
              FROM VERDICTS
             WHERE pr_number = %s
             ORDER BY created_at DESC
            """,
            (pr_number,),
        )
        for verdict in rows:
            # VARIANT arrives as a JSON string; tests_passed is already a bool.
            verdict["fixed_code"] = _load_json(verdict["fixed_code"], [])
        return rows

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
