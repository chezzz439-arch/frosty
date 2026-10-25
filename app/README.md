# Rohan's PR Guardian dashboard

Streamlit dashboard for the updated team guide: application-code log leaks, Claude and a temporary repo sandbox on the GitHub runner, Snowflake for dashboard hosting and history. This app does not call Claude, GitHub or Slack.

## Run

From the repository root:

```sh
python3 -m pip install -r app/requirements.txt
python3 -m streamlit run app/streamlit_app.py
python3 -m unittest discover -s app/tests -v
```

**Demo data** mode works without credentials. It has three completed decisions and a manual replay of fix/retry, allow, escalate or failure. All actions, confidence scores and outcomes in this mode are scripted and labeled synthetic. Data lasts for the viewer session. No real merges or comments happen.

**Team Snowflake tables** mode reads the updated guide's columns directly:

| Table | Columns read |
| --- | --- |
| PR_EVENTS | pr_number, step, status, detail, ts |
| RECEIPTS | pr_number, decision, reason, merged, receipt_url, created_at |
| VERDICTS (optional) | pr_number, decision, confidence, reason, created_at |

Choose the team's DATABASE.SCHEMA and GitHub repository in the sidebar. No code change is needed to switch data sources. Optional defaults: `GUARDIAN_TABLE_PREFIX`, `GUARDIAN_REPO`. Missing verdict permissions show confidence as “Not supplied”; required event/receipt failures remain visible and never fall back silently to fake data.

One expandable card per PR shows steps oldest first, confidence and reason, pass/fail/waiting states and decision colors: blue fix, green allow, amber escalate. Cards are sorted by newest activity. Live feed refreshes every three seconds; receipt search and the manual form do not auto-refresh while editing. Receipt history supports decision filters, text search, details, PR links and JSON download. Times are UTC.

ASSUMPTION: guide tables cover one repository and one current pipeline run per PR. They have no run IDs or commit SHAs, so reruns are grouped under their PR number. The dashboard does not invent commit identities. The engine team should agree on future run identifiers separately before using this across repositories.

## Manual backup

In Team mode, the button executes only this parameterized write:

```sql
INSERT INTO <configured_database>.<configured_schema>.PR_EVENTS
  (PR_NUMBER, STEP, STATUS, DETAIL, TS)
SELECT ?, 'manual run requested', 'waiting', ?, CURRENT_TIMESTAMP();
```

The detail includes a request ID and repository. Agree on these exact step/status values with Erik's worker. Inserting an event does not start a GitHub Action by itself: the worker must consume it, deduplicate/authorize it and apply the normal checks. The button has no merge credentials. Standard table INSERT privileges cannot restrict writes to a particular row type; the app itself limits its only Team-mode write to this event. Deploy under a least-privilege app-owner role before sharing.

## Insert fake rows into Snowflake

`fake_data.py` generates SQL only; it never connects to Snowflake or changes shared tables automatically. It labels every synthetic detail/reason `[DEMO]` and uses reserved fictional PRs 901–903. Review the SQL before running it.

```sh
python3 app/fake_data.py --create-demo-tables --output /tmp/guardian-demo.sql
snow sql -f /tmp/guardian-demo.sql -c YOUR_CONNECTION
```

The default target is PR_GUARDIAN_DEMO.APP, separate from team data. The optional create flag only accepts database names ending in `_DEMO`. Without the flag, the target tables must already exist. To target an agreed schema, use `--prefix DATABASE.SCHEMA`. Sequential repeats skip identical demo inserts; concurrent seeding is not supported. No cleanup or deletion is performed. The app warns when table receipts contain `[DEMO]` rows and does not link fictional PRs to GitHub.

To verify table integration, select Team mode and enter PR_GUARDIAN_DEMO.APP. Later point the same app at the real team schema.

## Deploy to Snowflake

Edit `app/snowflake.yml` to match Kaushik's database/schema/XS warehouse, then:

```sh
cd app
snow connection test -c YOUR_CONNECTION
snow streamlit deploy --replace --open -c YOUR_CONNECTION
```

Only supported Snowflake packages are used: Streamlit, pandas and Snowpark. The target database/schema must exist. CLI configuration is supplied for the team; this version has not been deployed to the team account in this commit.

## Checks

10 tests cover demo outcomes, elapsed-time progression, duplicate refresh/requests, failure behavior, input and URL validation, receipt filtering, the guide-column adapter, the exact manual event write and labeled fake SQL generation. The prior dashboard version was browser-tested in Snowflake; these updated guide changes have been syntax-checked and unit-tested, but need a team-table dry run.

No engine, trigger, merge or shared SQL files are changed by this contribution.
