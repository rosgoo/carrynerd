#!/usr/bin/env bash
# Commit tonight's data to the private repo and ask Vercel to rebuild.
#
# Replaces the nightly's old "commit the diff" step. The reasoning behind that
# step is unchanged and still the point — git is the database history, the
# backup and the audit log, for nothing — it simply happens in a repo that is
# not public now that the ledger is understood to be the asset.
#
#   DATA_REPO      owner/name of the private data repo
#   DATA_TOKEN     a token with contents:write on it, and nothing else
#   DEPLOY_HOOK    Vercel deploy hook URL (optional; skipped when unset)
#
# The deploy hook matters because the site builds from *this* repo, so a data
# commit lands somewhere Vercel is not watching. Without the hook the catalogue
# updates and the site never rebuilds — the failure is invisible, because
# nothing errors and the site simply stops changing.
set -euo pipefail

REPO="${DATA_REPO:-}"
TOKEN="${DATA_TOKEN:-}"
HOOK="${DEPLOY_HOOK:-}"
REF="${DATA_REF:-main}"

if [[ -z "$REPO" || -z "$TOKEN" ]]; then
  echo "data-push: DATA_REPO and DATA_TOKEN must both be set." >&2
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Full clone, not --depth 1: this one pushes, and a shallow clone cannot.
git clone --quiet --branch "$REF" \
  "https://x-access-token:${TOKEN}@github.com/${REPO}.git" "$TMP/data-repo"

copied=0
while IFS= read -r path; do
  [[ -z "$path" || "$path" == \#* ]] && continue
  # price-events.json is a hand-off to the alert matcher, not a record. The
  # ledger is the record, and it is listed above this.
  [[ "$path" == "data/price-events.json" ]] && continue
  if [[ -e "$path" ]]; then
    mkdir -p "$TMP/data-repo/$(dirname "$path")"
    cp -R "$path" "$TMP/data-repo/$path"
    copied=$((copied + 1))
  fi
done < scripts/private-paths.txt

cd "$TMP/data-repo"
git config user.name  "gearherd-bot"
git config user.email "gearherd-bot@users.noreply.github.com"
git add -A

if git diff --cached --quiet; then
  echo "data-push: nothing moved tonight"
  exit 0
fi

git commit --quiet -m "data: nightly crawl $(date -u +%Y-%m-%d)"
git push --quiet
echo "data-push: pushed $copied paths"

if [[ -n "$HOOK" ]]; then
  echo "data-push: triggering rebuild"
  curl -fsS -X POST "$HOOK" -o /dev/null
else
  echo "data-push: DEPLOY_HOOK unset — data updated, site NOT rebuilt" >&2
fi
