#!/bin/bash
# One-time: create the GitHub repo, push this folder, set the email and
# location secrets from config.env, and trigger a first cloud run.
# Prerequisite: gh auth login   (already done if `gh auth status` is green)
#
# The repo is PUBLIC by default, on purpose: GitHub Actions minutes are
# unlimited on public repos, while the two existing private watchers
# (tendercuts + instagram, ~72 runs/day combined) already track close to
# the 2,000 free private-repo minutes per month. This watcher adds ~96
# runs/day, which would blow that shared quota and silently kill all
# three. Nothing personal is in the code: coordinates, place names, and
# email credentials all live in repo secrets (set below).
# If you still prefer private, run: VISIBILITY=private bash setup_github.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
REPO="rain-alert"
VISIBILITY="${VISIBILITY:-public}"

gh auth status >/dev/null

# Read values from config.env (never committed - see .gitignore)
get() { grep -E "^$1=" config.env | head -1 | cut -d= -f2-; }
for key in SMTP_USER SMTP_PASS EMAIL_TO HOME_LAT HOME_LON OFFICE_LAT OFFICE_LON; do
  if [ -z "$(get "$key")" ]; then
    echo "ERROR: $key missing from config.env" >&2
    exit 1
  fi
done

if [ ! -d .git ]; then
  git init -b main
fi
git add -A
git -c user.useConfigOnly=false commit -m "Rain alert watcher" || true

if ! git remote get-url origin >/dev/null 2>&1; then
  gh repo create "$REPO" "--$VISIBILITY" --source . --remote origin --push
else
  git push -u origin main
fi

for key in SMTP_USER SMTP_PASS EMAIL_TO HOME_LAT HOME_LON OFFICE_LAT OFFICE_LON; do
  gh secret set "$key" --body "$(get "$key")"
done

echo "Triggering a first cloud run..."
gh workflow run check.yml || echo "(If this failed, trigger it once from the repo's Actions tab.)"

echo ""
echo "Done. Watch it at: https://github.com/$(gh api user -q .login)/$REPO/actions"
echo "Verify the email path with: gh workflow run check.yml -f test_email=true"
