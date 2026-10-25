# Tejas: PR Guardian merge component

## Setup

Install dependencies: `pip install -r merge/requirements.txt`

GitHub Actions secrets: `GITHUB_TOKEN` (Actions token with `contents: write`, `pull-requests: write`, `issues: write`), `SLACK_WEBHOOK_URL`, `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_PASSWORD`, `SNOWFLAKE_DATABASE`, `SNOWFLAKE_SCHEMA`, `SNOWFLAKE_WAREHOUSE`.

Run after Kaushik writes a VERDICTS row:

`python merge/run.py 12`

Dry run (reads Snowflake, no GitHub writes): `GUARDIAN_DRY_RUN=1 python merge/run.py 12`

## Engine handoff contract

`VERDICTS.fixed_code` must be JSON with `{"files":[{"path":"app/example.py","content":"complete corrected file contents"}]}`. Agree on this format with Kaushik. `tests_passed` must be a Snowflake BOOLEAN and `decision` one of `fix`, `allow`, `escalate`.

## Important safety / integration requirements

- Do **not** use this on production repositories without additional safeguards. The prototype assumes a trusted hackathon demo repository.
- The current schema does not bind a verdict to the exact PR head SHA. Add `head_sha` to VERDICTS (coordinate with Kaushik), compare it with the current PR head before every merge, and prevent old verdicts being reused.
- Fixes create a **new commit**. Require GitHub branch protection checks on that new commit before merging. GitHub may reject merge while checks are pending; rerun the merge step only after checks pass.
- Do not enable the Action on untrusted fork code with write credentials. Use least-privilege GitHub Actions permissions.
- Retry and idempotency handling are needed before running the Action on repeated pull_request events. Avoid duplicate comments/notifications/receipts.
- If Slack fails after merging, the run fails but the merge is not rolled back. The stored receipt remains and Slack can be retried manually.
- Configure GitHub Actions `permissions: contents: write`, `pull-requests: write`, `issues: write` and repository Actions settings to allow PR merges. Branch rules may prevent automated merging.
