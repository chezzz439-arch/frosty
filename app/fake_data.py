"""Generate clearly labeled Snowflake demo inserts; never connects or writes remotely.

Usage: python app/fake_data.py --output /tmp/guardian-demo.sql
Then inspect and execute it with the team's Snowflake CLI connection.
"""
import argparse
from datetime import datetime, timezone
from pathlib import Path
from streamlit_app import qualified_prefix, seed_demo


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def build_sql(prefix="PR_GUARDIAN_DEMO.APP", create_demo_tables=False):
    prefix = qualified_prefix(prefix)
    if create_demo_tables and not prefix.split('.')[0].endswith('_DEMO'):
        raise ValueError("Demo table creation requires a database name ending in _DEMO.")
    # ASSUMPTION: PRs 901-903 are reserved for fake data, never real GitHub PRs.
    data = seed_demo(datetime(2026, 10, 25, 18, 0, tzinfo=timezone.utc))
    lines = ["-- SYNTHETIC DEMO ONLY. PRs 901-903 are not real GitHub PRs.",
             "-- Generated SQL does not execute engine actions or merge PRs."]
    if create_demo_tables:
        database, schema = prefix.split('.')
        lines += [f"CREATE DATABASE IF NOT EXISTS {database};",
                  f"CREATE SCHEMA IF NOT EXISTS {prefix};",
                  f"CREATE TABLE IF NOT EXISTS {prefix}.PR_EVENTS (PR_NUMBER NUMBER, STEP VARCHAR, STATUS VARCHAR, DETAIL VARCHAR, TS TIMESTAMP_TZ);",
                  f"CREATE TABLE IF NOT EXISTS {prefix}.RECEIPTS (PR_NUMBER NUMBER, DECISION VARCHAR, REASON VARCHAR, MERGED BOOLEAN, RECEIPT_URL VARCHAR, CREATED_AT TIMESTAMP_TZ);"]
    lines.append("BEGIN;")
    for e in data['events']:
        pr = e['pr_number'] + 800
        detail = '[DEMO] ' + e['detail']
        lines.append(f"INSERT INTO {prefix}.PR_EVENTS (PR_NUMBER,STEP,STATUS,DETAIL,TS) SELECT {pr},{quote(e['step'])},{quote(e['status'])},{quote(detail)},{quote(e['ts'])}::TIMESTAMP_TZ WHERE NOT EXISTS (SELECT 1 FROM {prefix}.PR_EVENTS WHERE PR_NUMBER={pr} AND STEP={quote(e['step'])} AND DETAIL={quote(detail)});")
    for r in data['receipts']:
        pr = r['pr_number'] + 800
        reason = '[DEMO] ' + r['reason']
        merged = 'TRUE' if r['merged'] else 'FALSE'
        lines.append(f"INSERT INTO {prefix}.RECEIPTS (PR_NUMBER,DECISION,REASON,MERGED,RECEIPT_URL,CREATED_AT) SELECT {pr},{quote(r['decision'])},{quote(reason)},{merged},NULL,{quote(r['created_at'])}::TIMESTAMP_TZ WHERE NOT EXISTS (SELECT 1 FROM {prefix}.RECEIPTS WHERE PR_NUMBER={pr} AND REASON={quote(reason)});")
    lines.append("COMMIT;")
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', default='PR_GUARDIAN_DEMO.APP')
    parser.add_argument('--create-demo-tables', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    sql = build_sql(args.prefix, args.create_demo_tables)
    if args.output:
        args.output.write_text(sql)
        print(f"Wrote synthetic SQL to {args.output}. Review it before running it.")
    else:
        print(sql, end='')


if __name__ == '__main__':
    main()
