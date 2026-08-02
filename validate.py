#!/usr/bin/env python3
"""
gearherd quality gate — the thing standing between a bad parse and a live site.

The nightly workflow commits and deploys with nobody watching. That is fine
right up until a regex change halves the index, at which point the pipeline
succeeds, the commit lands, the site rebuilds, and Google recrawls a catalogue
missing half its bags. Nothing else in the system would notice.

So: run this after normalize/enrich/track_prices and before the commit. It
exits non-zero if the data is structurally wrong, or if it has moved further
than a plausible night's crawl should move it.

Two kinds of check:

  structural   Absolute truths about a well-formed catalogue — required fields,
               unique permalinks, values inside physically sane bounds. These
               need no history and always run. The brand mark is checked here
               too: the header logo and the favicon must be the same drawing.
  regression   Compared against the last committed data/bags.json, read
               straight out of git. That is the copy currently live, which is
               exactly the thing worth not regressing against. Skipped with a
               note when there is no baseline, so a first run is not a failure.

Usage:
    python3 validate.py
    python3 validate.py --warn-only     # report, always exit 0
    python3 validate.py --max-drop 0.3  # allow a 30% fall in bag count
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BAGS = os.path.join(HERE, "data", "bags.json")
LOGO = os.path.join(HERE, "src", "components", "Logo.astro")
FAVICON = os.path.join(HERE, "public", "favicon.svg")

REQUIRED = ("id", "brand", "brand_slug", "name", "category", "url", "slug")

# Physical plausibility, not precision. These are wide on purpose: the job is
# to catch a parser that has started reading pixel widths as centimetres, not
# to second-guess a brand about its own bag.
BOUNDS = {
    "price_min": (1, 20000),
    "price_max": (1, 20000),
    "weight_g":  (20, 25000),
    "volume_l":  (0.2, 300),
    "linear_cm": (5, 600),
}


class Report:
    def __init__(self):
        self.failures = []
        self.warnings = []
        self.notes = []

    def fail(self, check, detail):
        self.failures.append((check, detail))

    def warn(self, check, detail):
        self.warnings.append((check, detail))

    def note(self, text):
        self.notes.append(text)


def sample(items, n=4):
    items = list(items)
    shown = ", ".join(str(i) for i in items[:n])
    return f"{shown}{'…' if len(items) > n else ''}"


def structural(payload, rep):
    bags = payload.get("bags")
    if not isinstance(bags, list) or not bags:
        rep.fail("non-empty", "data/bags.json has no bags at all")
        return
    meta = payload.get("meta") or {}
    if not meta.get("coverage"):
        rep.fail("meta", "meta.coverage is missing — enrich.py did not run")

    missing, ids, slugs, bad_bounds, bad_dims, no_variants, inverted = (
        {}, {}, {}, [], [], [], [])

    for bag in bags:
        bid = bag.get("id", "<no id>")

        for field in REQUIRED:
            if not bag.get(field):
                missing.setdefault(field, []).append(bid)

        ids.setdefault(bag.get("id"), []).append(bid)
        # Permalink collisions are worse than they look: two models would
        # generate the same page and one would silently overwrite the other.
        slugs.setdefault((bag.get("brand_slug"), bag.get("slug")), []).append(bid)

        for field, (lo, hi) in BOUNDS.items():
            value = bag.get(field)
            if value is not None and not (lo <= value <= hi):
                bad_bounds.append(f"{bid}.{field}={value}")

        if (bag.get("price_min") is not None
                and bag.get("price_max") is not None
                and bag["price_min"] > bag["price_max"]):
            inverted.append(bid)

        dims = bag.get("dims_cm")
        if dims is not None:
            if (not isinstance(dims, list) or not 2 <= len(dims) <= 3
                    or not all(isinstance(d, (int, float)) and 1 <= d <= 250
                               for d in dims)):
                bad_dims.append(f"{bid}={dims}")
            elif bag.get("linear_cm") is not None:
                if abs(bag["linear_cm"] - sum(dims)) > 1.0:
                    bad_dims.append(
                        f"{bid} linear_cm={bag['linear_cm']} != sum{dims}")

        if not bag.get("variants"):
            no_variants.append(bid)

    for field, offenders in missing.items():
        rep.fail(f"required:{field}",
                 f"{len(offenders)} bags missing {field} — {sample(offenders)}")
    dupe_ids = [k for k, v in ids.items() if len(v) > 1]
    if dupe_ids:
        rep.fail("unique:id", f"{len(dupe_ids)} duplicate ids — {sample(dupe_ids)}")
    dupe_slugs = [f"{b}/{s}" for (b, s), v in slugs.items() if len(v) > 1]
    if dupe_slugs:
        rep.fail("unique:permalink",
                 f"{len(dupe_slugs)} colliding permalinks — {sample(dupe_slugs)}")
    if bad_bounds:
        # One implausible row is a brand publishing a packed-box weight; it is
        # worth seeing, not worth blocking a deploy over. A *lot* of them at
        # once is a parser reading the wrong unit, which is worth blocking.
        # So the count is the signal, not the presence.
        share = len(bad_bounds) / len(bags)
        detail = (f"{len(bad_bounds)} values outside sane range "
                  f"({share:.1%} of bags) — {sample(bad_bounds)}")
        (rep.fail if share > 0.01 else rep.warn)("bounds", detail)
    if inverted:
        rep.fail("price-order",
                 f"{len(inverted)} bags with price_min > price_max — {sample(inverted)}")
    if bad_dims:
        rep.fail("dims", f"{len(bad_dims)} malformed dimensions — {sample(bad_dims)}")
    if no_variants:
        # A bag with no SKUs has no price and no stock; it is a dead row.
        rep.warn("variants",
                 f"{len(no_variants)} bags have no variants — {sample(no_variants)}")


def brandmark(rep):
    """The header logo and the favicon have to be the same drawing.

    They are two files with no link between them, so they drift silently: the
    logo gets redrawn, the favicon keeps the old mark, and the only place that
    shows up is a browser tab nobody looks at during review. That is exactly how
    the caliper mark outlived the rename.

    Compares the path geometry and ignores everything else, so the favicon is
    free to keep its own background plate and its hardcoded hex fills while the
    component inherits currentColor.
    """
    def paths(path):
        try:
            with open(path) as f:
                return re.findall(r'\bd="([^"]+)"', f.read())
        except FileNotFoundError:
            rep.fail("brandmark", f"{os.path.relpath(path, HERE)} is missing")
            return None

    logo, icon = paths(LOGO), paths(FAVICON)
    if logo is None or icon is None:
        return
    if not logo:
        rep.fail("brandmark", "no path data in src/components/Logo.astro")
        return

    # Whitespace only — a reformat is not a drift.
    norm = lambda ds: [" ".join(d.split()) for d in ds]
    if norm(logo) != norm(icon):
        rep.fail("brandmark",
                 f"Logo.astro and favicon.svg are different drawings "
                 f"({len(logo)} vs {len(icon)} path(s)) — update both")


def baseline(path=None):
    """The last published catalogue — i.e. the one currently deployed.

    `git show HEAD:data/bags.json` was the whole implementation until the
    catalogue moved to the private data repo, at which point that path stopped
    existing in this checkout and this returned None — silently, because a
    missing baseline is a legitimate state on a first run. The regression
    checks then skipped themselves and the gate reported success having checked
    nothing, which is the worst way for a safety check to fail: it does not
    fire, and it does not say that it did not fire.

    So a caller can name the file instead. The nightly pulls the published
    catalogue before rebuilding over it and hands that copy here, which is what
    the git lookup used to mean. The git path stays as the fallback, for local
    runs in a checkout that still tracks it.
    """
    if path:
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
    try:
        out = subprocess.run(
            ["git", "show", "HEAD:data/bags.json"],
            cwd=HERE, capture_output=True, check=True,
        )
        return json.loads(out.stdout)
    except (subprocess.CalledProcessError, FileNotFoundError,
            json.JSONDecodeError):
        return None


# Brands that are meant to have stopped. A retired or walled entry going stale
# is the intended outcome, not a fault, and a check that shouts about it is a
# check people learn to skip.
DORMANT = {"retired", "walled", "unreachable", "custom", "no-adapter"}


def freshness(rep, stale_days, fail_days):
    """Has any brand quietly stopped producing?

    Borrowed from the sister project, which hit this failure mode from the
    other side and named it well: a broken parser produces *wrong* data and the
    aggregate checks catch it, but a broken adapter produces *nothing*,
    silently, forever, and nobody notices until someone asks why a brand has
    not listed anything since March.

    gearherd is more exposed to that than it looks, because fetch.py keeps
    yesterday's catalogue when a refetch fails — deliberately, so one bad night
    does not drop a brand out of the index. The cost of that resilience is that
    a brand can fail every night for a month while its models sit in the
    catalogue looking current. Nothing in the count or coverage checks moves:
    the models are all still there. Only their age changes, and nothing was
    watching it.

    Per-brand, because that is the granularity the failure happens at. The
    aggregate is fine by construction while any one brand rots.
    """
    path = os.path.join(HERE, "data", "fetch-log.json")
    try:
        with open(path) as f:
            log = json.load(f)
    except (OSError, json.JSONDecodeError):
        rep.note("no fetch-log.json — freshness checks skipped")
        return

    now = datetime.datetime.now(datetime.timezone.utc)
    stale, dead = [], []
    for slug, entry in sorted(log.items()):
        if not isinstance(entry, dict) or slug.startswith("_"):
            continue
        if entry.get("status") != "ok":
            continue                       # never fetched, or failing loudly
        if (entry.get("platform") or "") in DORMANT:
            continue
        ts = entry.get("fetched_at")
        if not ts:
            continue
        try:
            when = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
        age = (now - when).days
        if age >= fail_days:
            dead.append(f"{slug} ({age}d)")
        elif age >= stale_days:
            stale.append(f"{slug} ({age}d)")

    if dead:
        rep.fail("freshness",
                 f"{len(dead)} brand(s) last fetched over {fail_days}d ago and "
                 f"still in the catalogue: {sample(dead, 6)}. Their models are "
                 f"being served as current. Retire them deliberately or fix "
                 f"the fetch.")
    if stale:
        rep.warn("freshness",
                 f"{len(stale)} brand(s) not refreshed in {stale_days}d: "
                 f"{sample(stale, 6)}")
    if not dead and not stale:
        rep.note(f"freshness: every active brand fetched within {stale_days}d")


def regression(payload, prev, rep, max_drop, max_coverage_drop):
    meta, pmeta = payload.get("meta") or {}, prev.get("meta") or {}

    for field in ("bag_count", "sku_count"):
        now, before = meta.get(field), pmeta.get(field)
        if not before or now is None:
            continue
        if now < before * (1 - max_drop):
            rep.fail(
                f"drop:{field}",
                f"{field} fell {before} -> {now} "
                f"({100 * (before - now) // before}%, limit "
                f"{int(max_drop * 100)}%). If this is intentional — a merge fix "
                f"or a stricter classifier — rerun with --max-drop.")
        elif now != before:
            rep.note(f"{field}: {before} -> {now}")

    now_b, before_b = meta.get("brand_count"), pmeta.get("brand_count")
    if before_b and now_b is not None and now_b < before_b:
        rep.fail("drop:brand_count",
                 f"brand_count fell {before_b} -> {now_b}; a brand disappearing "
                 f"usually means a failed fetch, not a real change")

    cov, pcov = meta.get("coverage") or {}, pmeta.get("coverage") or {}
    for field, before in pcov.items():
        now = cov.get(field)
        if not now or "pct" not in before:
            continue
        delta = now["pct"] - before["pct"]
        if delta < -max_coverage_drop:
            rep.fail(f"coverage:{field}",
                     f"{field} coverage fell {before['pct']}% -> {now['pct']}% "
                     f"(limit {max_coverage_drop} points)")
        elif delta:
            rep.note(f"coverage {field}: {before['pct']}% -> {now['pct']}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--warn-only", action="store_true",
                    help="report problems but always exit 0")
    ap.add_argument("--max-drop", type=float, default=0.25,
                    help="tolerated fractional fall in bag/SKU count (default .25)")
    ap.add_argument("--max-coverage-drop", type=float, default=10.0,
                    help="tolerated fall in a coverage percentage, in points")
    ap.add_argument("--stale-days", type=int, default=3,
                    help="warn when an active brand has not been refetched in "
                         "this many days (default 3; the nightly refetches "
                         "everything, so 3 already means two missed nights)")
    ap.add_argument("--fail-days", type=int, default=14,
                    help="fail when an active brand's catalogue is this old and "
                         "still being served as current (default 14)")
    ap.add_argument("--baseline", default="",
                    help="the published catalogue to compare against. Defaults "
                         "to `git show HEAD:data/bags.json`, which only works "
                         "in a checkout that still tracks it — the nightly "
                         "pulls the live copy from the data repo and names it")
    ap.add_argument("--require-baseline", action="store_true",
                    help="fail rather than skip when no baseline is found. The "
                         "nightly sets this: with the catalogue in another "
                         "repo, a missing baseline means the pull failed, not "
                         "that this is a first run")
    args = ap.parse_args()

    try:
        with open(BAGS) as f:
            payload = json.load(f)
    except FileNotFoundError:
        sys.exit("no data/bags.json — run normalize.py first")
    except json.JSONDecodeError as e:
        sys.exit(f"data/bags.json is not valid JSON: {e}")

    rep = Report()
    structural(payload, rep)
    brandmark(rep)
    freshness(rep, args.stale_days, args.fail_days)

    prev = baseline(args.baseline or None)
    if prev is None and args.require_baseline:
        # Skipping is right when there is genuinely nothing to compare against.
        # It is wrong when the baseline was supposed to be fetched and was not:
        # the gate then passes having checked nothing, and says so only in a
        # note nobody reads. Anywhere the baseline is expected, demand it.
        rep.fail("baseline",
                 f"no baseline at {args.baseline or 'HEAD:data/bags.json'} — "
                 f"regression checks would silently pass. Did the data pull "
                 f"run?")
    elif prev is None:
        rep.note("no committed baseline in git — regression checks skipped")
    else:
        regression(payload, prev, rep, args.max_drop, args.max_coverage_drop)

    meta = payload.get("meta") or {}
    print(f"validating {meta.get('bag_count', '?')} bags, "
          f"{meta.get('brand_count', '?')} brands, "
          f"{meta.get('sku_count', '?')} SKUs")

    for text in rep.notes:
        print(f"  · {text}")
    for check, detail in rep.warnings:
        print(f"  ! {check}: {detail}")
    for check, detail in rep.failures:
        print(f"  ✗ {check}: {detail}")

    if not rep.failures:
        print(f"\nOK — {len(rep.warnings)} warning(s), nothing blocking")
        return 0

    print(f"\nFAILED — {len(rep.failures)} blocking problem(s)")
    if args.warn_only:
        print("(--warn-only, exiting 0 anyway)")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
