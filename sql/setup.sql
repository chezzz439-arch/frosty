CREATE WAREHOUSE IF NOT EXISTS PR_GUARDIAN_WH WAREHOUSE_SIZE = 'XSMALL' AUTO_SUSPEND = 60 AUTO_RESUME = TRUE;
CREATE DATABASE IF NOT EXISTS PR_GUARDIAN;
CREATE SCHEMA IF NOT EXISTS PR_GUARDIAN.CORE;
USE SCHEMA PR_GUARDIAN.CORE;

CREATE TABLE IF NOT EXISTS PR_DIFFS (
  pr_number   NUMBER,
  repo        STRING,
  branch      STRING,
  files       ARRAY,
  diff_text   STRING,
  created_at  TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

CREATE TABLE IF NOT EXISTS VERDICTS (
  pr_number     NUMBER,
  decision      STRING,   -- exactly 'fix', 'allow' or 'escalate'
  confidence    FLOAT,    -- 0 to 1
  reason        STRING,
  fixed_code    VARIANT,  -- [{ "path": ..., "content": ... }] for each corrected file
  tests_passed  BOOLEAN,
  attempts      NUMBER,
  created_at    TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

CREATE TABLE IF NOT EXISTS RECEIPTS (
  pr_number    NUMBER,
  decision     STRING,
  reason       STRING,
  merged       BOOLEAN,
  receipt_url  STRING,
  created_at   TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

CREATE TABLE IF NOT EXISTS PR_EVENTS (
  pr_number  NUMBER,
  step       STRING,   -- e.g. 'diff read', 'decision made', 'fix attempt 2', 'tests passed', 'merged'
  status     STRING,   -- 'ok', 'failed' or 'running'
  detail     STRING,
  ts         TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);
