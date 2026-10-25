#!/usr/bin/env bash
# Open, list and reset the planted PRs in the demo shop repo.
#
#   demo-data/planted.sh open [violation|decoy|ambiguous|all]   push branch(es), open PR(s)
#   demo-data/planted.sh status                                 what's open
#   demo-data/planted.sh reset                                  between takes: close the PRs, delete
#                                                               the branches, put main back to `baseline`
#
# DEMO_REPO_DIR = local clone of the demo shop (default ../frosty-demo-shop next to frosty).
# Needs git and gh (logged in). main must NOT have branch protection (reset force-pushes it).
set -euo pipefail

FROSTY="$(cd "$(dirname "$0")/.." && pwd)"
PLANTED="$FROSTY/demo-data/planted-prs"
cd "${DEMO_REPO_DIR:-$FROSTY/../frosty-demo-shop}"

field() { sed -n "s/^$2: //p" "$1/pr.md" | head -1; }
body()  { sed '1,/^---$/d' "$1/pr.md"; }
dir_for() {
  case "$1" in
    violation) echo "$PLANTED/01-violation" ;;
    decoy)     echo "$PLANTED/02-decoy" ;;
    ambiguous) echo "$PLANTED/03-ambiguous" ;;
    *) echo "unknown PR '$1' (use violation, decoy, ambiguous or all)" >&2; exit 1 ;;
  esac
}

open_one() {
  local d branch title
  d="$(dir_for "$1")"; branch="$(field "$d" branch)"; title="$(field "$d" title)"
  git fetch -q origin main
  git checkout -q -B "$branch" origin/main
  cp -R "$d/files/." .            # only files/, never reference_fix/
  git add -A
  git commit -q -m "$title"
  git push -q -f origin "$branch"
  git checkout -q main
  if [ "$(gh pr view "$branch" --json state -q .state 2>/dev/null || true)" = "OPEN" ]; then
    echo "already open: $(gh pr view "$branch" --json url -q .url)"
  else
    gh pr create --base main --head "$branch" --title "$title" --body "$(body "$d")"
  fi
}

case "${1:-}" in
  open)
    if [ "${2:-all}" = all ]; then for p in violation decoy ambiguous; do open_one "$p"; done
    else open_one "$2"; fi
    ;;
  status)
    for p in violation decoy ambiguous; do
      b="$(field "$(dir_for "$p")" branch)"
      printf '%-10s %-24s ' "$p" "$b"
      gh pr view "$b" --json number,state,url -q '"#\(.number) \(.state) \(.url)"' 2>/dev/null || echo "no PR"
    done
    ;;
  reset)
    for p in violation decoy ambiguous; do
      b="$(field "$(dir_for "$p")" branch)"
      if gh pr close "$b" --delete-branch >/dev/null 2>&1; then echo "closed $b"
      else git push -q origin --delete "$b" 2>/dev/null || true; fi
      git branch -q -D "$b" 2>/dev/null || true
    done
    git fetch -q origin --tags
    git checkout -q main
    git reset -q --hard baseline
    git push -q -f origin main
    echo "main reset to baseline. Reopen with: demo-data/planted.sh open <violation|decoy|ambiguous>"
    ;;
  *)
    sed -n '2,10p' "$0"; exit 1 ;;
esac
