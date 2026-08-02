#!/usr/bin/env bash
# Fetch the private data into data/ so a build has something to build from.
#
# Run by Vercel ahead of `astro build`, and by anyone working on the site from
# a fresh clone. The catalogue and the price ledger live in a private repo —
# see scripts/private-paths.txt for what and why — so this repo on its own
# holds the code and none of the data.
#
#   DATA_REPO    owner/name of the private data repo
#   DATA_TOKEN   a token with contents:read on it, and nothing else
#
# Fails loudly and early on purpose. A build that quietly proceeds without the
# catalogue does not error — Astro would emit a site with zero model pages and
# deploy it over a working one, which reads as "the crawl broke" rather than
# "the token expired" and costs an afternoon to diagnose.
set -euo pipefail

REPO="${DATA_REPO:-}"
TOKEN="${DATA_TOKEN:-}"
REF="${DATA_REF:-main}"

if [[ -z "$REPO" || -z "$TOKEN" ]]; then
  echo "data-pull: DATA_REPO and DATA_TOKEN must both be set." >&2
  echo "  This repo holds no catalogue. See scripts/private-paths.txt." >&2
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "data-pull: cloning $REPO@$REF"
# --depth 1: the build wants today's catalogue, not the history. The history is
# the point of the private repo being a git repo at all, but it is for humans
# and for the ledger, not for a build that reads one revision.
git clone --quiet --depth 1 --branch "$REF" \
  "https://x-access-token:${TOKEN}@github.com/${REPO}.git" "$TMP/data-repo"

mkdir -p data
copied=0
while IFS= read -r path; do
  [[ -z "$path" || "$path" == \#* ]] && continue
  src="$TMP/data-repo/$path"
  if [[ -e "$src" ]]; then
    mkdir -p "$(dirname "$path")"
    cp -R "$src" "$path"
    copied=$((copied + 1))
  fi
done < scripts/private-paths.txt

echo "data-pull: restored $copied paths"

# The one file the build genuinely cannot proceed without. Checked explicitly
# rather than left to Astro's import error, so the message names the cause.
if [[ ! -f data/bags.json ]]; then
  echo "data-pull: data/bags.json missing from $REPO — refusing to build." >&2
  exit 1
fi
