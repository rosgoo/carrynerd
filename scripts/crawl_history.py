#!/usr/bin/env python3
"""Build a queryable history of the crawl out of data/fetch-log.json's commits.

The nightly overwrites fetch-log.json every run, so the file only ever answers
"how did the crawl go last night". Every previous answer is still there — the
data repo has been committing the file since it was seeded — but sitting in git
history, which nothing can query. This walks those commits and lays them out as
one row per brand per night.

The question it exists to answer is *erosion*. validate.py gates the cliff: it
compares tonight against the last published catalogue and stops the deploy when
a brand falls off it. What a night-over-night check cannot see is a brand
shedding 3% a night for a month, because no single night trips anything and each
night's baseline is the previous night's already-diminished count. That failure
needs the whole series, which is what this builds.

Deliberately not git-history, and this is worth writing down because the obvious
move is to reach for it. `git-history file db data/fetch-log.json` on the
current release (0.6.1) exits 0, prints nothing, and produces a database
containing no rows at all. The cause is cli.py:31, which looks the file up as
`[b for b in commit.tree.blobs if b.name == relative_path]` — `tree.blobs` is
the blobs at the *root* of the tree and `b.name` is a bare filename, so any path
with a directory in it matches nothing, forever, silently. Every file in this
project lives under data/. The 0.7a0 alpha fixes it with a plain
`commit.tree[relative_path]`, but that is a 2022 pre-release, and pinning one to
get a tool whose stable version silently reports success on an empty result is
the wrong trade for the ~70 lines of it we actually need. sqlite3 is stdlib.

So: stdlib only, like everything else here, and it raises rather than shrugging
when it finds nothing to read.

    DATA_REPO    owner/name of the private data repo   (unless --repo is given)
    DATA_TOKEN   a token with contents:read on it

Usage:
    python3 scripts/crawl_history.py --out crawl-history.db
    python3 scripts/crawl_history.py --repo ../gearherd-data   # local checkout
    python3 scripts/crawl_history.py --quiet                   # build, no report

Query it afterwards with anything that speaks SQLite:

    select slug, committed_at, product_count from crawl
     where slug = 'aer' order by committed_at;
"""

import argparse
import json
import os
import sqlite3
import statistics
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Hardcoded rather than a flag: everything below understands this file's shape
# specifically — a dict keyed by brand slug — so a --path option would only
# offer callers a way to get an empty database.
LOG_PATH = "data/fetch-log.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS crawl (
    commit_sha    TEXT NOT NULL,
    committed_at  TEXT NOT NULL,
    slug          TEXT NOT NULL,
    name          TEXT,
    platform      TEXT,
    status        TEXT,
    product_count INTEGER,
    fetched_at    TEXT,
    PRIMARY KEY (commit_sha, slug)
);
CREATE INDEX IF NOT EXISTS crawl_slug ON crawl (slug, committed_at);
CREATE INDEX IF NOT EXISTS crawl_at   ON crawl (committed_at);
"""

# A brand that is meant to have stopped is not eroding. Kept in step with
# validate.py's list of the same name, and for the same reason: a check that
# shouts about the intended outcome is a check people learn to skip.
DORMANT = {"retired", "walled", "unreachable", "custom", "no-adapter"}


def git(repo, *args):
    out = subprocess.run(["git", "-C", repo, *args],
                         capture_output=True, check=True)
    return out.stdout


def clone(repo_spec, token, into):
    """Full clone, not --depth 1. The history is the entire point here."""
    url = f"https://x-access-token:{token}@github.com/{repo_spec}.git"
    subprocess.run(["git", "clone", "--quiet", url, into], check=True)
    return into


def read_versions(repo):
    """Every committed version of the fetch log, oldest first."""
    log = git(repo, "log", "--reverse", "--format=%H\t%cI", "--", LOG_PATH)
    versions = []
    for line in log.decode().splitlines():
        if not line.strip():
            continue
        sha, when = line.split("\t", 1)
        try:
            blob = git(repo, "show", f"{sha}:{LOG_PATH}")
        except subprocess.CalledProcessError:
            # The file did not exist at this commit. `git log -- path` should
            # not return those, but a rename or a deletion can produce one.
            continue
        try:
            payload = json.loads(blob)
        except json.JSONDecodeError as e:
            print(f"  ! {sha[:8]} has unparseable fetch-log.json ({e}) — skipped",
                  file=sys.stderr)
            continue
        if isinstance(payload, dict):
            versions.append((sha, when, payload))
    return versions


def build(conn, versions):
    conn.executescript(SCHEMA)
    rows = []
    for sha, when, payload in versions:
        for slug, entry in payload.items():
            if not isinstance(entry, dict) or slug.startswith("_"):
                continue
            rows.append((
                sha, when, slug,
                entry.get("name"), entry.get("platform"), entry.get("status"),
                entry.get("product_count"), entry.get("fetched_at"),
            ))
    conn.executemany(
        "INSERT OR REPLACE INTO crawl (commit_sha, committed_at, slug, name, "
        "platform, status, product_count, fetched_at) VALUES (?,?,?,?,?,?,?,?)",
        rows)
    conn.commit()
    return len(rows)


def erosion(conn, window, min_obs, threshold):
    """Brands whose latest count sits below their own recent normal.

    Median rather than mean, and the median of the *prior* observations rather
    than of all of them, because one collapsed night should move the thing being
    compared against as little as possible. A mean over a window that includes
    tonight drags the baseline toward the very number under suspicion.

    Calibrated against a synthetic 20-night history — a steady brand, one
    holding at ±8% noise, a 3%/night eroder, a 6%/night eroder, and one falling
    off a cliff. What that showed: the window is the lever and the threshold
    barely is. Every threshold from 0.25 down to 0.10 caught exactly the same
    brands at a given window, because the gap between a real eroder and an
    ordinary noisy one is enormous — at a 21-night window the eroders register
    26% and 46% down while the noisy brand sits at 1.5%. Nothing lives in
    between, so the threshold only has to land somewhere in a very wide dead
    zone. The window is what decides whether slow erosion is visible at all: at
    14 nights the 3%/night brand reads 21% down and at 21 nights it reads 26%,
    because a longer baseline reaches back to before the decline started.

    Hence 21 and 0.20 rather than 14 and 0.25. Compounding is the reason to care
    about the slow case: 3% a night is 46% gone in three weeks, and no
    night-over-night check will ever see a 3% step.
    """
    findings = []
    slugs = [r[0] for r in conn.execute(
        "SELECT DISTINCT slug FROM crawl ORDER BY slug")]
    for slug in slugs:
        series = list(conn.execute(
            "SELECT committed_at, product_count, status, platform FROM crawl "
            "WHERE slug = ? ORDER BY committed_at DESC LIMIT ?",
            (slug, window + 1)))
        if (series and (series[0][3] or "") in DORMANT) or len(series) < min_obs:
            continue
        latest = series[0]
        prior = [r[1] for r in series[1:] if r[1] is not None]
        if latest[1] is None or len(prior) < min_obs - 1:
            continue
        baseline = statistics.median(prior)
        if baseline <= 0:
            continue
        drop = (baseline - latest[1]) / baseline
        if drop > threshold:
            findings.append((slug, baseline, latest[1], drop, len(prior)))
    return findings


def main():
    ap = argparse.ArgumentParser(
        description="Build a SQLite history of the crawl from fetch-log commits")
    ap.add_argument("--repo", default="",
                    help="path to a data-repo checkout. Without it, clones "
                         "$DATA_REPO using $DATA_TOKEN")
    ap.add_argument("--out", default=os.path.join(HERE, "crawl-history.db"),
                    help="database to write (default ./crawl-history.db)")
    ap.add_argument("--window", type=int, default=21,
                    help="how many prior nights the erosion baseline spans "
                         "(default 21; see calibration note above — this is the "
                         "lever that matters, not --threshold)")
    ap.add_argument("--min-observations", type=int, default=5,
                    help="skip a brand until it has this many nights on record")
    ap.add_argument("--threshold", type=float, default=0.20,
                    help="report a brand below this fraction of its median")
    ap.add_argument("--quiet", action="store_true",
                    help="build the database, skip the erosion report")
    args = ap.parse_args()

    tmp = None
    try:
        if args.repo:
            repo = args.repo
        else:
            spec, token = os.environ.get("DATA_REPO"), os.environ.get("DATA_TOKEN")
            if not spec or not token:
                sys.exit("crawl_history: set --repo, or DATA_REPO and DATA_TOKEN.")
            tmp = tempfile.mkdtemp()
            print(f"crawl_history: cloning {spec}")
            repo = clone(spec, token, os.path.join(tmp, "data-repo"))

        versions = read_versions(repo)
        if not versions:
            # The failure this whole script was written to avoid repeating.
            sys.exit(f"crawl_history: no commits touch {LOG_PATH} in {repo}. "
                     f"That is a broken checkout or a moved file, not an empty "
                     f"history — refusing to write an empty database.")

        if os.path.exists(args.out):
            os.remove(args.out)          # derived artefact; rebuild, never merge
        conn = sqlite3.connect(args.out)
        rows = build(conn, versions)
        span = f"{versions[0][1][:10]} to {versions[-1][1][:10]}"
        shown = os.path.relpath(args.out, HERE)
        if shown.startswith(".."):
            shown = args.out          # outside the repo; the relative form is noise
        print(f"crawl_history: {rows} rows from {len(versions)} commits "
              f"({span}) -> {shown}")

        if args.quiet:
            return 0

        if len(versions) < args.min_observations:
            print(f"  · {len(versions)} nights on record; erosion needs "
                  f"{args.min_observations}. Nothing to compare yet.")
            return 0

        findings = erosion(conn, args.window, args.min_observations,
                           args.threshold)
        if not findings:
            print(f"  · no brand is below {args.threshold:.0%} of its "
                  f"{args.window}-night median")
            return 0
        # Reported, never blocking. The gate's job is to stop a bad catalogue
        # from publishing; this is the slower signal, and a slow signal that can
        # fail a build is one that gets muted.
        print(f"  ! {len(findings)} brand(s) below their recent normal:")
        for slug, base, now, drop, n in sorted(findings, key=lambda f: -f[3]):
            print(f"      {slug:<18} {now} vs median {base:g} "
                  f"over {n} nights ({drop:.0%} down)")
        return 0
    finally:
        if tmp:
            subprocess.run(["rm", "-rf", tmp], check=False)


if __name__ == "__main__":
    raise SystemExit(main())
