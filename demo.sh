#!/usr/bin/env bash
# The whole PR Guardian loop, run from this laptop. One command.
#
#   ./demo.sh violation      open planted PR 01, judge it, fix it, test it, merge it
#   ./demo.sh decoy          PR 02 -- the one Guardian must ALLOW
#   ./demo.sh ambiguous      PR 03 -- the one Guardian must ESCALATE
#   ./demo.sh reset          close the PRs, put the demo shop back to `baseline`
#
# Same four steps, same four Snowflake tables and the same real GitHub PR as the
# Action would run. The Action is step-for-step identical; this just drives it
# from here so a CI queue can't decide whether the demo happens.
#
#   1. trigger (Erik)   PR_DIFFS  + PR_EVENTS
#   2. engine (Kaushik) reads PR_DIFFS, judges, sandboxes the fix, runs pytest,
#                       retries up to 3x, writes VERDICTS
#   3. merge (Tejas)    reads VERDICTS, pushes the fix, merges or labels,
#                       RECEIPTS + receipt comment + Slack
#
# Set SLACK_WEBHOOK_URL before running or step 3 stops at the Slack ping (the
# merge and the receipt have already happened by then).
set -euo pipefail

FROSTY="$(cd "$(dirname "$0")" && pwd)"
SHOP_REPO="${DEMO_REPO:-chezzz439-arch/frosty-demo-shop}"
SHOP_DIR="${DEMO_REPO_DIR:-$FROSTY/../frosty-demo-shop}"
CONNECTION="${PR_GUARDIAN_SNOWFLAKE_CONNECTION:-hackathon2}"

step() { printf '\n\033[1;36m=== %s ===\033[0m\n' "$*"; }
die()  { printf '\033[1;31m%s\033[0m\n' "$*" >&2; exit 1; }

[ -d "$SHOP_DIR" ] || die "No demo shop clone at $SHOP_DIR. Run: demo-data/make_demo_repo.sh $SHOP_REPO"
command -v gh >/dev/null || die "gh not found"

WHICH="${1:-violation}"
if [ "$WHICH" = reset ]; then
  DEMO_REPO_DIR="$SHOP_DIR" "$FROSTY/demo-data/planted.sh" reset
  exit 0
fi

# Snowflake, the same key-pair the snow CLI uses. Nothing is printed.
export SNOWFLAKE_ACCOUNT="${SNOWFLAKE_ACCOUNT:-FQWENHP-EC45741}"
export SNOWFLAKE_USER="${SNOWFLAKE_USER:-CHEZZZ439@GMAIL.COM}"
export SNOWFLAKE_ROLE="${SNOWFLAKE_ROLE:-ACCOUNTADMIN}"
export SNOWFLAKE_PRIVATE_KEY_FILE="${SNOWFLAKE_PRIVATE_KEY_FILE:-$HOME/.snowflake_keys/rsa_key.p8}"
export SNOWFLAKE_DATABASE=PR_GUARDIAN SNOWFLAKE_SCHEMA=CORE SNOWFLAKE_WAREHOUSE=PR_GUARDIAN_WH
[ -f "$SNOWFLAKE_PRIVATE_KEY_FILE" ] || die "No private key at $SNOWFLAKE_PRIVATE_KEY_FILE"

# GitHub, from the login already on this laptop -- no secret to set.
export GITHUB_REPOSITORY="$SHOP_REPO"
GITHUB_TOKEN="$(gh auth token)" || die "gh auth token failed; run: gh auth login"
export GITHUB_TOKEN GH_TOKEN="$GITHUB_TOKEN"

# The judge reads the rules from frosty, never from the PR under review.
export PR_GUARDIAN_RULES="$FROSTY/demo-data/rules.md"
export PR_GUARDIAN_TEST_CMD="${PR_GUARDIAN_TEST_CMD:-python3 -m pytest -q}"

step "0 · Open planted PR: $WHICH"
DEMO_REPO_DIR="$SHOP_DIR" "$FROSTY/demo-data/planted.sh" open "$WHICH"

BRANCH="$(sed -n 's/^branch: //p' "$FROSTY/demo-data/planted-prs/"*"-$WHICH/pr.md" | head -1)"
PR="$(gh pr view "$BRANCH" -R "$SHOP_REPO" --json number -q .number)"
[ -n "$PR" ] || die "Could not find an open PR for branch $BRANCH"
echo "PR #$PR  ($BRANCH)  https://github.com/$SHOP_REPO/pull/$PR"

step "1 · trigger (Erik) -- read the diff, write PR_DIFFS"
python3 "$FROSTY/trigger/send_diff.py" --repo "$SHOP_REPO" --pr "$PR" \
        --store snowflake --connection "$CONNECTION"

step "2 · engine (Kaushik) -- judge, fix, test, write VERDICTS"
# The engine reads whole files and tests the fix, so it needs the PR's tree on
# disk, not just the diff.
git -C "$SHOP_DIR" fetch -q origin "$BRANCH"
git -C "$SHOP_DIR" checkout -q -B "$BRANCH" "origin/$BRANCH"
PR_GUARDIAN_REPO="$SHOP_DIR" python3 "$FROSTY/engine/run.py" "$PR"

step "3 · merge (Tejas) -- merge or label, RECEIPTS, receipt comment, Slack"
if [ -z "${SLACK_WEBHOOK_URL:-}" ]; then
  echo "SLACK_WEBHOOK_URL is not set: the merge and the receipt will still happen," >&2
  echo "but step 3 will stop at the Slack ping." >&2
fi
python3 "$FROSTY/merge/run.py" "$PR"

step "4 · the trail, as the dashboard sees it"
python3 "$FROSTY/engine/run.py" "$PR" --show
echo
echo "PR:        https://github.com/$SHOP_REPO/pull/$PR"
echo "Dashboard: PR_GUARDIAN.CORE -- PR_DIFFS, VERDICTS, RECEIPTS, PR_EVENTS"
