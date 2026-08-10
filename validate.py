#!/usr/bin/env python3
"""
carrynerd quality gate — the thing standing between a bad parse and a live site.

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
    python3 validate.py --max-brand-drop 0.6   # a brand really did shrink
    python3 validate.py --allow-brands-gone dmm,mammut   # they really did leave
"""

import argparse
import collections
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
#
# Widened at the bottom end for wallets, which the index carries now and did
# not when these were written. The floors were set around the smallest *bag*
# and duly failed a build on 85 values that were all correct: an Alpaka
# cardholder really does weigh 19 g, and Bellroy's Apex Slim Sleeve really is
# 0.1 L. A gate that fires on true data is worse than no gate, because the
# next person reads past it.
#
# Still floors rather than none. 5 g catches the 1 g accessories that were a
# parser fault, and 0.05 L is 50 cm³ — smaller than any card slip and larger
# than zero.
BOUNDS = {
    "price_min": (1, 20000),
    "price_max": (1, 20000),
    "weight_g":  (5, 25000),
    "volume_l":  (0.05, 300),
    "linear_cm": (5, 600),
}

# Per-axis dimension sanity. The floor is 0.2 cm because a card sleeve is 2 mm
# thick — the old 1 cm floor rejected three Bellroy card products for being
# accurately thin.
DIM_MIN, DIM_MAX = 0.2, 250


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
                    or not all(isinstance(d, (int, float))
                               and DIM_MIN <= d <= DIM_MAX
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

    splits(bags, slugs, rep)


def splits(bags, slugs, rep):
    """The two invariants a size split has to hold, both of them URL-shaped.

    A model sold in several capacities is published as one entry per capacity,
    and exactly one of them inherits the record's old permalink as a redirect —
    see split_sizes() in normalize.py and legacyRedirects in lib/catalog.js.

    Neither failure here is visible in a build log. A group with two claimants
    generates two redirects from one address and whichever route the platform
    matches first wins, silently and differently per deploy. A redirect whose
    source is some other model's live permalink is worse: that page is still
    built, still in the sitemap, and now unreachable — a 301 sitting in front of
    a page that exists is an outage no 404 check would ever catch.
    """
    live = {(brand, slug) for brand, slug in slugs}
    groups, shadowed = {}, []
    for bag in bags:
        if bag.get("split_from"):
            groups.setdefault(bag["split_from"], []).append(bag)
        if bag.get("legacy_slug"):
            key = (bag["brand_slug"], bag["legacy_slug"])
            if key in live:
                shadowed.append(f"{key[0]}/{key[1]} -> {bag['id']}")

    claimed = {pid: [b["id"] for b in group if b.get("legacy_slug")]
               for pid, group in groups.items()}
    contested = [f"{pid} ({len(ids)})" for pid, ids in claimed.items()
                 if len(ids) != 1]
    lonely = [pid for pid, group in groups.items() if len(group) < 2]

    if shadowed:
        rep.fail("split:shadowed",
                 f"{len(shadowed)} legacy redirect(s) point away from a "
                 f"permalink that is still built — {sample(shadowed)}")
    if contested:
        rep.fail("split:legacy",
                 f"{len(contested)} split group(s) do not have exactly one "
                 f"entry carrying legacy_slug — {sample(contested)}")
    if lonely:
        rep.fail("split:orphan",
                 f"{len(lonely)} split group(s) have a single member, so the "
                 f"model was split into itself — {sample(lonely)}")
    if groups and not (shadowed or contested or lonely):
        rep.note(f"split: {len(groups)} multi-size models across "
                 f"{sum(len(g) for g in groups.values())} entries, each with "
                 f"one inherited permalink")


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
#
# `todo` joins them for the same reason and from the other direction: it marks a
# brand that is wired but has never worked, so it has no catalogue to go stale
# in the first place. It is here so that the day one of them starts working and
# is switched back on, freshness starts watching it again automatically.
DORMANT = {"retired", "walled", "unreachable", "custom", "no-adapter", "todo"}


def freshness(rep, stale_days, fail_days):
    """Has any brand quietly stopped producing?

    Borrowed from the sister project, which hit this failure mode from the
    other side and named it well: a broken parser produces *wrong* data and the
    aggregate checks catch it, but a broken adapter produces *nothing*,
    silently, forever, and nobody notices until someone asks why a brand has
    not listed anything since March.

    carrynerd is more exposed to that than it looks, because fetch.py keeps
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


# The most expensive median any genuine brand in this catalogue reaches is
# Eberlestock at $429, and the next is a bespoke leather maker at $340. Nothing
# that sells bags in dollars has a median in four figures. A brand that does is
# not expensive, it is quoting a different currency: Db's NOK median read as
# $1,999 and Mismo's DKK median as $3,600, both roughly an order of magnitude
# above the catalogue's own ~$95.
#
# So the ceiling sits far above every real brand and far below both faults. It
# is a smoke alarm and not a price policy — it cannot tell kroner from dollars,
# it can only tell that a number is not a bag price.
USD_MEDIAN_CEILING = 1000.0


def currency(payload, rep, ceiling):
    """Is a brand quoting something that is not dollars?

    Shopify's /products.json carries no currency field anywhere in the payload,
    so a store selling in DKK is byte-for-byte indistinguishable from one
    selling in USD. There is nothing to parse and nothing to assert on. The
    only signal available is the prices themselves being implausible, which is
    a weak check — and it went unmade for long enough that 554 models, 7% of
    the index, were served at 6-10x their real price, fed the ledger, and would
    have mailed alerts about drops that never happened.

    Weak and present beats rigorous and absent. This is the backstop under
    fetch.py's per-brand `locale`: the locale is what fixes the prices, and
    this is what stops a missing or wrong one from being invisible again.
    """
    locales, roster_currency = {}, {}
    roster = os.path.join(HERE, "brands.json")
    try:
        with open(roster) as f:
            entries = json.load(f)
        locales = {b["slug"]: b.get("locale") for b in entries}
        roster_currency = {b["slug"]: (b.get("currency") or "").upper()
                           for b in entries if b.get("currency")}
    except (OSError, json.JSONDecodeError, TypeError, KeyError):
        # The roster is advisory here — it sharpens the message, it is not what
        # the check stands on. A median in four figures is wrong either way.
        rep.note("brands.json unreadable — currency check ran without locales")

    # A brand that has *declared* it sells in pounds is not the fault this is
    # looking for — it is the fix. The ceiling is a dollar heuristic and means
    # nothing applied to another currency, so declared brands are counted and
    # skipped rather than measured against it. What is left is the dangerous
    # case: a brand still labelled USD whose prices are not dollars.
    prices, declared, sources = {}, {}, collections.Counter()
    for bag in payload.get("bags") or []:
        code = (bag.get("currency") or "USD").upper()
        sources[bag.get("currency_source") or "unknown"] += 1
        if code != "USD":
            declared[bag.get("brand_slug")] = code
            continue
        price = bag.get("price_min")
        if isinstance(price, (int, float)) and price > 0:
            prices.setdefault(bag.get("brand_slug"), []).append(price)

    # The roster going stale against the evidence. normalize.py prefers what the
    # source says, so the *published* price is already right — this is a note
    # that a hand-written declaration has been overtaken and should be cleared,
    # not a claim that anything shipped wrong. A fail here would block a build
    # over a comment.
    published = {}
    for bag in payload.get("bags") or []:
        published.setdefault(bag.get("brand_slug"),
                             ((bag.get("currency") or "USD").upper(),
                              bag.get("currency_source")))
    stale_roster = []
    for slug, want in sorted(roster_currency.items()):
        got, src = published.get(slug, (None, None))
        if got and want and got != want and src in ("feed", "product-page",
                                                    "locale"):
            stale_roster.append(f"{slug} (roster says {want}, {src} says {got})")
    if stale_roster:
        rep.warn("currency:roster",
                 f"{len(stale_roster)} brand(s) whose declared currency the "
                 f"evidence disagrees with: {sample(stale_roster, 4)}. The "
                 f"evidence won, so prices are right — clear the stale "
                 f"declaration in brands.json.")

    suspect = []
    for slug, values in sorted(prices.items()):
        # Under a handful of models the median is one product rather than a
        # distribution, and the bounds check already covers absurd singletons.
        if len(values) < MIN_BRAND_BAGS:
            continue
        values.sort()
        median = values[len(values) // 2]
        if median <= ceiling:
            continue
        locale = locales.get(slug)
        why = (f"locale {locale!r} did not take" if locale
               else "no locale configured")
        suspect.append(f"{slug} (median ${median:,.0f} over {len(values)} "
                       f"bags, {why})")

    if suspect:
        rep.fail("currency",
                 f"{len(suspect)} brand(s) priced implausibly for USD: "
                 f"{sample(suspect, 4)}. A four-figure median is a storefront "
                 f"quoting its own currency, not an expensive brand. Set the "
                 f"brand's `locale` in brands.json to a market that prices in "
                 f"USD (Shopify mirrors the feed under it, e.g. \"en-us\"), or "
                 f"retire the brand. Publishing this ships wrong prices and "
                 f"mails wrong alerts.")
    else:
        extra = ""
        if declared:
            named = sorted(f"{s} {c}" for s, c in declared.items())
            extra = f"; {len(declared)} non-USD ({sample(named, 3)})"
        rep.note(f"currency: no USD brand's median price exceeds "
                 f"${ceiling:,.0f}{extra}")
        # How much of the catalogue's currency is known versus assumed. Worth a
        # line every night: "assumed" climbing means a source stopped stating
        # it, which is the silent half of this failure mode.
        if sources:
            rep.note("currency provenance: " + ", ".join(
                f"{n} bags {src}" for src, n in sources.most_common()))


# Below this many bags in the baseline, a percentage is noise rather than
# signal: a brand with three models loses a third of its catalogue by
# discontinuing one colourway. The per-brand check skips them, and the
# aggregate and brand_count checks still cover the case where they vanish.
MIN_BRAND_BAGS = 5


def per_brand(payload, prev, rep, fail_drop, warn_drop):
    """Has one brand's catalogue collapsed while the totals stayed fine?

    The regression checks above are all aggregates, and the aggregate is the
    wrong granularity for the failure that actually happens. carrynerd carries
    484 bags across 11 brands, unevenly: aer has 88, able-carry has 8. A paging
    bug that costs aer 40% of its models drops 35 bags — 7% of the catalogue,
    against a 25% tolerance. Nothing moves. brand_count does not move, because
    aer still has bags. Coverage does not move, because the bags that survived
    are as well-specified as they ever were. freshness() does not move either:
    the fetch *succeeded*, it just came back short, so fetched_at is today's.

    So the gate passes, the short catalogue publishes, and the only trace is a
    number in a log nobody reads. That is the same shape as the paging bug that
    was quietly losing 22% of every fetch, and nothing here would have caught
    it.

    Two tiers, because a brand genuinely discontinuing a line should not block a
    deploy at 3am. A fall past the whole-catalogue tolerance is worth saying out
    loud; a fall past the per-brand tolerance is worth stopping for. The
    per-brand limit is the looser of the two on purpose: one brand's count is a
    far noisier series than the total, so holding it to the aggregate's standard
    would fail on ordinary churn.

    Note this compares catalogue bags, not fetch-log product_count. Those differ
    — merging and classification sit between them — and the catalogue is the
    thing being published, so it is the thing worth gating. Erosion too slow to
    trip a night-over-night check is the other half of this, and belongs to
    scripts/crawl_history.py, which has the whole series to compare against
    rather than just last night.
    """
    def counts(doc):
        out = {}
        for bag in doc.get("bags") or []:
            slug = bag.get("brand_slug")
            if slug:
                out[slug] = out.get(slug, 0) + 1
        return out

    now, before = counts(payload), counts(prev)
    if not before:
        return

    collapsed, shrunk = [], []
    for slug, was in sorted(before.items()):
        if was < MIN_BRAND_BAGS:
            continue
        remains = now.get(slug, 0)
        if remains >= was:
            continue
        drop = (was - remains) / was
        entry = f"{slug} {was} -> {remains} ({drop:.0%})"
        if drop > fail_drop:
            collapsed.append(entry)
        elif drop > warn_drop:
            shrunk.append(entry)

    if collapsed:
        rep.fail("drop:brand",
                 f"{len(collapsed)} brand(s) lost more than {fail_drop:.0%} of "
                 f"their catalogue: {sample(collapsed, 6)}. The totals absorb "
                 f"this — check the fetch before assuming it is real. Rerun "
                 f"with --max-brand-drop if the brand did discontinue them.")
    if shrunk:
        rep.warn("drop:brand",
                 f"{len(shrunk)} brand(s) shrank by more than {warn_drop:.0%}: "
                 f"{sample(shrunk, 6)}")
    if not collapsed and not shrunk:
        rep.note(f"per-brand: no brand lost more than {warn_drop:.0%} of its bags")


# Volume read off a page rather than out of a feed field or a title. These are
# the sources that sweep prose, and so the ones that can pick up furniture.
SWEPT_VOLUME_SOURCES = ("product-page", "description", "pages:description")

# Below this a repeated figure is a coincidence: three 30 L daypacks from one
# brand is an ordinary catalogue, not a bug.
MIN_SWEPT_VOLUMES = 4

# And below this it is one product wearing several coats. Feeds routinely list
# every colourway as its own row — Zorali ships eight Escapade Backpacks,
# Filson five All-Weather Totes — and eight rows agreeing on 30 L is one
# product saying it once, which is not the failure here.
#
# Distinct prices rather than distinct names, because names do not separate
# them: a colourway is named for a place or a plant ("Daintree", "Wattle
# Seed"), so no colour vocabulary strips it reliably, and the classifier's own
# list says as much. Price does separate them, and separates exactly along the
# line that matters — chrome does not care what it is attached to, so it lands
# on the $12 shock cord and the $380 pack alike, while a colourway family sits
# at one price by definition.
MIN_SWEPT_PRICES = 3


def swept_volume(payload, rep, share=0.6):
    """Is one brand's volume actually one number, copied off its own chrome?

    A litre figure is the only spec that carries its own unit inline, which
    makes it the one storefronts print as furniture: nav rails read "Aerus 55L
    Duos 2p Framus 58L", filter sidebars read "20L 30L 35L 40L 46L". A parser
    sweeping a product page for the first thing shaped like a capacity finds
    those before it finds the product, and the tell is that it finds the *same*
    one every time — the chrome does not change between products.

    That is what this looks for, because it is the only cheap signal that
    separates the failure from the data. Every individual answer is plausible:
    30 L is a real daypack size, and nothing about ULA's Circuit at 30 L looks
    wrong until you notice its Catalyst, its rain kilt and its framesheet are
    all 30 L too. Bounds checks cannot see it, coverage cannot see it — it
    raises coverage — and per-brand counts cannot see it. Sorting the live site
    by price per litre was what surfaced it, which is a reader's job, not a
    gate's.

    Warns rather than fails: a brand really can build one line at one size, and
    a small catalogue reaches four products quickly.
    """
    # Keyed by source as well as brand, because these are separate parsers with
    # separate failure modes and pooling them hides the one that is broken. ULA
    # read 30 L off its nav rail for all 31 products it swept a page for, and
    # its 24 description-derived volumes are fine — pooled, that is 32/55, which
    # sits just under any threshold worth setting. Split, the page sweep is
    # 31/31 and unmissable.
    swept = {}
    for bag in payload.get("bags") or []:
        source = bag.get("volume_source")
        if source in SWEPT_VOLUME_SOURCES and bag.get("volume_l"):
            swept.setdefault((bag.get("brand_slug"), source), []).append(
                (bag["volume_l"], bag.get("price_min")))

    suspects = []
    for (slug, source), rows in sorted(swept.items()):
        volumes = [v for v, _ in rows]
        if len(volumes) < MIN_SWEPT_VOLUMES:
            continue
        top = max(set(volumes), key=volumes.count)
        n = volumes.count(top)
        prices = {p for v, p in rows if v == top and p is not None}
        if n / len(volumes) >= share and len(prices) >= MIN_SWEPT_PRICES:
            suspects.append(f"{slug} {n}/{len(volumes)} at {top}L via {source} "
                            f"across {len(prices)} price points")

    if suspects:
        rep.warn("volume:swept",
                 f"{len(suspects)} brand(s) report one repeated volume from "
                 f"page text: {sample(suspects, 6)}. Check the product page for "
                 f"a nav rail or filter sidebar listing capacities — that is "
                 f"the number, not the product's.")
    else:
        rep.note(f"volume: no brand's page-derived volumes collapse to one "
                 f"figure ({sum(len(v) for v in swept.values())} checked)")


# Grams per litre below which a bag is not a bag.
#
# The genuinely lightest things in the index — silnylon dry sacks, Dyneema pack
# bodies sold without a suspension — sit between five and eight. Below five,
# every record inspected has been a parse rather than a product. The site holds
# the same number as MIN_PLAUSIBLE_GPL in src/lib/hubs.js, where it decides
# what is allowed into a ranking; here it decides what gets reported. Two
# languages, one judgement — change both together.
DENSITY_FLOOR_GPL = 5.0


def density(payload, rep):
    """Do a bag's stated volume and weight agree that it is a bag?

    Neither figure can be checked on its own. 133 g is a real weight, 46 L is a
    real capacity, and only together do they say that something has gone wrong
    — a hip belt read instead of the pack, a fabric's grams per square metre
    read as the fabric's product, a recommended load read as a mass. Every one
    of those passed the 50 g - 8 kg bounds gate, because each number was
    individually ordinary.

    So this is the check that only exists in the join. It is one-sided on
    purpose: there is no ceiling here, because dense is a real property of
    small leather goods and a 0.3 L wallet at 100 g is 333 g/L and correct,
    while nothing is genuinely lighter than air-filled fabric.

    Grouped by weight source, since that names the parser to go and read.
    Warns rather than fails — these are records to fix, not a reason to refuse
    to publish a catalogue that is otherwise sound.
    """
    thin = {}
    for bag in payload.get("bags") or []:
        weight, volume = bag.get("weight_g"), bag.get("volume_l")
        if not weight or not volume:
            continue
        gpl = weight / volume
        if gpl < DENSITY_FLOOR_GPL:
            thin.setdefault(bag.get("weight_source") or "?", []).append(
                f"{bag['id']} {gpl:.1f} g/L ({weight} g / {volume} L, "
                f"volume via {bag.get('volume_source') or '?'})")

    total = sum(len(v) for v in thin.values())
    if not total:
        rep.note(f"density: no bag states a weight under "
                 f"{DENSITY_FLOOR_GPL:.0f} g per litre of its own capacity")
        return

    worst = sorted(thin.items(), key=lambda kv: -len(kv[1]))
    rep.warn("density:implausible",
             f"{total} bag(s) weigh under {DENSITY_FLOOR_GPL:.0f} g per litre, "
             f"which means one of the two figures is wrong. By weight source: "
             + ", ".join(f"{src} ({len(rows)})" for src, rows in worst)
             + ". " + sample([r for _, rows in worst for r in rows], 4))


def brands_gone(payload, prev, rep, allowed):
    """Which brands fell out of the catalogue — by name, not by count.

    This compared meta.brand_count and printed the two numbers, which is the one
    thing about a missing brand that does not help: "brand_count fell 150 -> 146"
    says a disappearance usually means a failed fetch and then leaves working out
    *which four* to whoever is reading a 4am log. Both sides of the comparison
    are right here in full, so the check may as well answer the question it
    raises.

    Naming them is also what makes the check overridable without loosening it. A
    brand does legitimately leave the index — retired from the roster, or a
    classifier correction taking its last row with it. The sling veto took the
    whole of Advanced Base Camp, DMM, Mammut and Singing Rock: four stores whose
    only entries here were loops of climbing webbing, correctly removed, and
    nothing about that is a fetch failure. A percentage tolerance is the wrong
    shape for it, because the honest tolerance is zero and the honest exception
    is a list — so a deliberate removal is named once, by whoever decided it,
    and every brand not on that list still fails.

    Comparing sets rather than counts also catches what counting cannot: a brand
    vanishing on the same night another arrives leaves brand_count unmoved, and
    that is a failed fetch wearing a roster addition as cover.
    """
    def slugs(doc):
        return {bag.get("brand_slug") for bag in doc.get("bags") or []
                if bag.get("brand_slug")}

    now, before = slugs(payload), slugs(prev)
    if not before:
        return

    gone = sorted(before - now)
    unexplained = [slug for slug in gone if slug not in allowed]
    expected = [slug for slug in gone if slug in allowed]
    # Named on the command line but still in the catalogue. Worth saying because
    # the flag is a one-off by design: carried into a later run it is a hole in
    # the gate for a brand nobody is removing any more.
    unused = sorted(slug for slug in allowed if slug not in gone)

    if unexplained:
        rep.fail("drop:brand_count",
                 f"{len(unexplained)} brand(s) left the catalogue: "
                 f"{sample(unexplained, 8)}. A brand disappearing usually means "
                 f"a failed fetch, not a real change — check the fetch log for "
                 f"it before assuming otherwise. If the removal is deliberate, "
                 f"rerun with --allow-brands-gone "
                 f"{','.join(unexplained)}")
    if expected:
        rep.note(f"brands gone: {sample(expected, 8)} — allowed on this run")
    if unused:
        rep.warn("allow:brands-gone",
                 f"{len(unused)} brand(s) named as removed are still in the "
                 f"catalogue: {sample(unused, 8)}. Drop them from "
                 f"--allow-brands-gone; it is meant to be spent on the one run "
                 f"that does the removing.")
    if not gone:
        rep.note(f"brands: all {len(before)} in the published catalogue are "
                 f"still here")


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
    ap.add_argument("--max-brand-drop", type=float, default=0.4,
                    help="tolerated fractional fall in any one brand's bag "
                         "count (default .4). Looser than --max-drop because a "
                         "single brand's count is a noisier series than the "
                         "total; a fall past --max-drop warns, past this fails")
    ap.add_argument("--max-coverage-drop", type=float, default=10.0,
                    help="tolerated fall in a coverage percentage, in points")
    ap.add_argument("--allow-brands-gone", default="",
                    help="comma-separated brand slugs that are meant to have "
                         "left the catalogue — a roster retirement, or a "
                         "classifier fix that took a brand's last row with it. "
                         "Any other brand disappearing still fails. Named "
                         "rather than counted, and a one-off: the night after "
                         "the removal, the baseline no longer has them")
    ap.add_argument("--stale-days", type=int, default=3,
                    help="warn when an active brand has not been refetched in "
                         "this many days (default 3; the nightly refetches "
                         "everything, so 3 already means two missed nights)")
    ap.add_argument("--fail-days", type=int, default=14,
                    help="fail when an active brand's catalogue is this old and "
                         "still being served as current (default 14)")
    ap.add_argument("--max-usd-median", type=float, default=USD_MEDIAN_CEILING,
                    help=f"fail when a brand's median price exceeds this, which "
                         f"means its storefront is quoting another currency "
                         f"(default {USD_MEDIAN_CEILING:,.0f})")
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
    currency(payload, rep, args.max_usd_median)
    swept_volume(payload, rep)
    density(payload, rep)

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
        brands_gone(payload, prev, rep,
                    {s.strip() for s in args.allow_brands_gone.split(",")
                     if s.strip()})
        per_brand(payload, prev, rep, args.max_brand_drop, args.max_drop)

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
