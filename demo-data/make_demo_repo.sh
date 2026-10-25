#!/usr/bin/env bash
# One time: create the demo shop repo on GitHub, the repo Guardian watches.
#
#   demo-data/make_demo_repo.sh <owner>/frosty-demo-shop [teammate ...]
#
# Copies demo-data/shop/ + trigger/guardian.yml into ../<name> (next to frosty), commits,
# tags the commit `baseline` (resets go back to it), creates the GitHub repo and invites teammates.
# Why a separate repo: Guardian merges PRs on its own, and the demo resets force-push main.
# Neither should ever happen to frosty, where our real code lives.
set -euo pipefail
FULL="${1:?usage: make_demo_repo.sh OWNER/NAME [collaborators...]}"; shift || true
FROSTY="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$FROSTY/../$(basename "$FULL")"

[ -e "$DEST" ] && { echo "$DEST already exists"; exit 1; }
cp -R "$FROSTY/demo-data/shop" "$DEST"
mkdir -p "$DEST/.github/workflows"
cp "$FROSTY/trigger/guardian.yml" "$DEST/.github/workflows/guardian.yml"
printf '__pycache__/\n.pytest_cache/\n.guardian-local/\n' > "$DEST/.gitignore"

cd "$DEST"
git init -q -b main
git add -A
git commit -q -m "Acme Shop checkout service with PR Guardian"
git tag baseline
# Public: free Actions minutes, and the planted PRs only contain published test card numbers.
gh repo create "$FULL" --public --source . --push
git push -q origin baseline

for user in "$@"; do
  gh api -X PUT "repos/$FULL/collaborators/$user" -f permission=push >/dev/null && echo "invited $user"
done
echo
echo "Created https://github.com/$FULL  (local clone: $DEST)"
echo "Next: add the secrets (demo-data/README.md, step 5), then demo-data/planted.sh open violation"
