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
               need no history and always run.
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
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BAGS = os.path.join(HERE, "data", "bags.json")

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


def baseline():
    """The last committed catalogue — i.e. the one currently deployed."""
    try:
        out = subprocess.run(
            ["git", "show", "HEAD:data/bags.json"],
            cwd=HERE, capture_output=True, check=True,
        )
        return json.loads(out.stdout)
    except (subprocess.CalledProcessError, FileNotFoundError,
            json.JSONDecodeError):
        return None


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

    prev = baseline()
    if prev is None:
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
