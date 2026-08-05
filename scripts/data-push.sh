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
# Usage: data-push.sh [path...]
#   With no arguments, pushes everything in scripts/private-paths.txt.
#   With arguments, pushes only those — which is how the nightly separates
#   *memory* from *output*.
#
# That separation exists because the gate protects the wrong thing otherwise.
# validate.py guards what gets published, and publishing is the catalogue. But
# enrich-cache.json is not output, it is memory: enrich.py picks its next batch
# by excluding whatever is already in that file. Gate the memory behind the
# same check as the catalogue and a failing night discards its own progress —
# the next run re-enriches the identical 60 products, and the crawl never
# advances no matter how many nights it runs. The ledger has the same shape of
# problem for a worse reason: a night's price observations cannot be recovered
# later at any cost.
#
# So: memory and the ledger are banked unconditionally, the catalogue only when
# it validates.
#
# The deploy hook fires only when the catalogue is among the paths pushed. It
# matters because the site builds from the *code* repo, so a data commit lands
# somewhere Vercel is not watching — without it the catalogue updates and the
# site never rebuilds, and nothing errors to say so.
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

if [[ $# -gt 0 ]]; then
  PATHS=("$@")
else
  PATHS=()
  while IFS= read -r line; do
    [[ -z "$line" || "$line" == \#* ]] && continue
    PATHS+=("$line")
  done < scripts/private-paths.txt
fi

copied=0
pushed_catalogue=0
for path in "${PATHS[@]}"; do
  # price-events.json is a hand-off to the alert matcher, not a record. The
  # ledger is the record, and it is listed above this.
  [[ "$path" == "data/price-events.json" ]] && continue
  if [[ -e "$path" ]]; then
    mkdir -p "$TMP/data-repo/$(dirname "$path")"
    # rm -rf first, because `cp -R dir dest` copies *into* dest when dest
    # already exists as a directory. data/collections came back from the first
    # real run as data/collections/collections, and left alone it would have
    # grown a level a night. Replacing the path outright is also what makes a
    # deleted source file propagate as a deletion rather than lingering.
    rm -rf "$TMP/data-repo/$path"
    cp -R "$path" "$TMP/data-repo/$path"
    copied=$((copied + 1))
    [[ "$path" == "data/bags.json" ]] && pushed_catalogue=1
  fi
done

cd "$TMP/data-repo"
git config user.name  "carrynerd-bot"
git config user.email "carrynerd-bot@users.noreply.github.com"
git add -A

if git diff --cached --quiet; then
  echo "data-push: nothing moved tonight"
  exit 0
fi

if [[ "$pushed_catalogue" == "1" ]]; then
  subject="data: nightly crawl $(date -u +%Y-%m-%d)"
else
  # Named differently on purpose. These commits land on nights the gate
  # rejected the catalogue, and a log full of identical "nightly crawl"
  # messages would hide the fact that nothing was published.
  subject="data: bank ledger and caches $(date -u +%Y-%m-%d)"
fi

git commit --quiet -m "$subject"
git push --quiet
echo "data-push: pushed $copied paths ($subject)"

if [[ "$pushed_catalogue" != "1" ]]; then
  echo "data-push: catalogue not in this push — no rebuild needed"
elif [[ -n "$HOOK" ]]; then
  echo "data-push: triggering rebuild"
  curl -fsS -X POST "$HOOK" -o /dev/null
else
  echo "data-push: DEPLOY_HOOK unset — catalogue updated, site NOT rebuilt" >&2
fi
