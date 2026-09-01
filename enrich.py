#!/usr/bin/env python3
"""
carrynerd enricher — stage 2, fills in what the Shopify feed leaves out.

/products.json gives us SKUs, colourways, prices, stock and weight, but brands
keep dimensions and laptop fit in metafields that the feed does not expose.
Those render into the product page, and almost every store also emits a
schema.org Product block for Google Shopping. So stage 2 fetches each product
page once and reads both.

This is one request per product, so it is the expensive stage. It caches every
page result and is fully resumable — rerun it and it picks up where it stopped.
Unlike /products.json — which rate-limits per client IP and throttles
datacenter ranges on sight — product pages are ordinary CDN-fronted HTML, and
the nightly crawls them from Actions runners without pushback. robots.txt is
honoured per store: a disallowed path is never fetched, and a declared
Crawl-delay floors how soon the same store is asked again.

Usage:
    python3 enrich.py                 # everything missing dims, 1.5s pacing
    python3 enrich.py --limit 50      # a taste
    python3 enrich.py --brand aer
    python3 enrich.py --delay 4       # slower, for stores that push back
"""

import argparse
import gzip
import json
import os
import re
import sys
import time
from urllib.parse import urlsplit

from normalize import (CM_PER_IN, FEATURES, MATERIALS, PAGE_LABEL, VOLUME_RE,
                       detect, find_dims_cm, find_laptop_in, find_volume_bag,
                       find_weight_g, page_near_label, strip_html)
from fetch import Pacer, get, robots, robots_allows

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "data", "enrich-cache.json")
BAGS = os.path.join(HERE, "data", "bags.json")

# What this run did, for the nightly's health check to read. Not a durable
# artifact — the same standing as data/price-events.json, and gitignored beside
# it. It exists because the facts that separate "the crawl broke" from "the
# crawl is finished" live in this loop and nowhere else: how many pages we
# actually asked for, how many answered, and whether the global breaker fired.
# The workflow was inferring all three from the cache growing, which is a proxy
# that stops working the moment there is nothing left to add.
RUN = os.path.join(HERE, "data", "enrich-run.json")

# Development page cache. `enrich-cache.json` holds *parsed* results, which
# means every parser improvement needs a fresh crawl of every product before it
# applies to anything already seen — a quarter-hour and a few hundred requests
# to test a regex. Keeping the raw page makes that loop free: `--reparse` runs
# the current parser over pages already on disk and touches the network zero
# times.
#
# Local only, gitignored, and never published: this holds brands' full pages
# including their marketing copy, which the index deliberately does not
# reproduce. It is a working file, not a dataset.
PAGES = os.path.join(HERE, "data", "page-cache")


def page_path(bag_id):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", bag_id)
    return os.path.join(PAGES, f"{safe}.html.gz")


def save_page(bag_id, text):
    os.makedirs(PAGES, exist_ok=True)
    with gzip.open(page_path(bag_id), "wt", encoding="utf-8") as f:
        f.write(text)


def load_page(bag_id):
    path = page_path(bag_id)
    if not os.path.exists(path):
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return f.read()
    except (OSError, EOFError):
        return None

# Which sources are worth a product-page crawl.
#
# Shopify feeds carry no dimensions at all (267 of 274 dimensions in the
# validated catalog came from pages, 7 from feed text), and the WooCommerce
# Store API carries none either — those brands publish specs in the
# description HTML and on the page. Bellroy's API already returns dimensions,
# net weight and materials for every SKU, so crawling it would spend requests
# to learn nothing; CampSaver is an aggregator whose JSON-LD is parsed at
# fetch time and whose WAF soft-blocks sustained crawling.
ENRICHABLE_SOURCES = ("shopify", "woocommerce")


def enrichable(bag):
    return (bag.get("source") or "shopify").startswith(ENRICHABLE_SOURCES)


def page_key(bag):
    """The cache line for the page this entry was read off.

    Not the entry's own id, because they are not one to one. A model sold in
    three capacities is three catalogue entries behind one product URL — see
    split_sizes() in normalize.py — and keying the cache by id would crawl that
    page three times and store three identical copies of the answer.

    Keying by the pre-split id rather than by the URL keeps every line already
    in data/enrich-cache.json valid: that id is exactly what the cache was
    written under before the split existed.
    """
    return bag.get("split_from") or bag["id"]


def dedupe(items, key):
    """First occurrence of each key, order preserved."""
    seen, out = set(), []
    for item in items:
        k = key(item)
        if k not in seen:
            seen.add(k)
            out.append(item)
    return out


# What a product page says that is true of every capacity it describes, and
# what is true of only one.
#
# A page for a model sold in three sizes states one set of dimensions and one
# weight, and says nothing about which size they belong to. It also states the
# fabric and the feature list, which are the same bag whichever size you buy.
#
# So the second group is applied only to the entry whose volume the page's own
# stated volume matches, and to no other. Where the page states no volume there
# is nothing to match on and none of them get it — the dash is the honest
# answer, and it is a great deal cheaper than the alternative, which is the
# 20L's measurements sitting in the carry-on tables under the 35L's name.
#
# The laptop figure was in the second group and belongs in the first. A brand
# that sells one bag in three capacities builds one laptop sleeve and states it
# once — see the CIVIC Panel Loader note in split_by_size(), where Evergoods
# state 16in for the 16L and the 24L independently. Treating it as size-bound
# emptied `laptopMin` of most of the split catalogue: the figure landed on the
# one child whose volume the page happened to name and every sibling showed a
# dash, which that filter reads as "does not fit" rather than "not known".
SIZED_PAGE_FIELDS = ("dims_cm", "dims_source", "linear_cm",
                     "volume_l", "volume_source", "weight_g", "weight_source",
                     "gtin")


# Weight sources that a figure read off the product page supersedes.
#
# Enrichment fills gaps and does not overwrite feed data — except where the
# feed's own field is known to be answering a different question, and these
# are the three ways that happens.
#
# The shipping fields are the box, not the bag. Shopify's `grams` reports
# Aer's 35L Travel Pack at 4536 g, exactly 10 lb, against a real 1.77 kg, and
# normalize.py says of Magento's bare `weight` that it is "the same thing plus
# a box".
#
# The bare sweeps over prose are the parsers that cannot tell the bag from
# what is written next to it. LiteAF's packs sat at 133 g — the smallest of
# four hip-belt options listed above a "Weights & Measurements" heading that
# gave 31.8 oz — and Bonfus at 109 g, which was its shell fabric in ounces per
# square yard. Both parsers are fixed now; the point of this list is that the
# fix can reach the products it already got wrong.
#
# Which is also why `product-page` supersedes itself. Without it, a re-parse
# can never correct an answer this step gave on an earlier run: the cache held
# ULA's Dragonfly at 743 g while the catalogue kept the 1814 g — exactly 4 lb
# — that an older parser had put there, and nothing would ever have moved it.
#
# Everything absent from this list is a *labelled* figure: pages:label,
# pages:pairs, pages:table, description:labelled, magento:r_weight,
# bellroy-api:net_weight_g. That is the product speaking about itself, and it
# outranks anything swept off a page, including ours.
SUPERSEDED_WEIGHT_SOURCES = frozenset({
    "shopify:grams", "magento:weight",
    "description", "campsaver:description",
    "product-page",
})


def interleave(items, key):
    """Round-robin the items across their key groups, preserving order within
    each group. [a1 a2 a3 b1 c1] -> [a1 b1 c1 a2 a3]."""
    groups = {}
    for item in items:
        groups.setdefault(key(item), []).append(item)
    out = []
    while groups:
        for k in list(groups):
            out.append(groups[k].pop(0))
            if not groups[k]:
                del groups[k]
    return out


LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I)

# Deliberately looser than the parsers in normalize.py: this only answers "is
# there anything dimension-shaped here at all", to tell a parser gap apart from
# a brand that publishes no dimensions.
DIM_SHAPED = re.compile(
    r"\d[\d.]*\s*(?:\"|”|in\b|inch(?:es)?|cm)?\s*[x×*]\s*\d[\d.]*\s*"
    r"(?:\"|”|in\b|inch(?:es)?|cm)|"
    r"\b(?:height|width|depth|length)\b\s*[:\-–]?\s*\d", re.I)

# Brands overwhelmingly label the spec block with one of these words, so we
# narrow to that neighbourhood before pattern matching. Searching the whole
# page produces false positives from unrelated copy and size charts.
SPEC_WINDOW = re.compile(
    r"(dimensions?|measurements?|specs?\b|specifications?|exterior|external)"
    r"(.{0,600})", re.I | re.S)

# The marketing-copy problem, and the fix.
#
# Aer is the worst case and a useful yardstick: its schema.org description is
# three sentences that say "premium materials and thoughtful product details"
# and name not one of them, and its theme emits no spec JSON at all. Detecting
# features from that yields nothing, which the filters then read as "this bag
# has no laptop sleeve".
#
# But the facts *are* on the page — just further down, under a "Product
# Details / Features" heading, as a plain bulleted list naming CORDURA, YKK,
# the laptop pocket and its 16" fit, the water bottle pocket, the luggage
# passthrough. The old parser missed them only because it took a fixed 600
# characters after the first spec-ish word and stopped.
#
# So: anchor on the headings that introduce a product's own detail prose, and
# read forward until the page stops talking about this product. That boundary
# is what keeps the scope honest — the standing objection to just using the
# whole page is that a "You may also like" rail would smear one bag's features
# across every other bag in the store, and SCOPE_END is what prevents it.
SCOPE_END = re.compile(
    r"\b(you may also like|you might also like|related products?|"
    r"you'?ll also love|recommended for you|complete the look|"
    r"shop the look|customers also (?:bought|viewed)|frequently bought|"
    r"pairs? well with|more from|similar items?|others? also|"
    r"customer reviews?|write a review|based on \d+ reviews?|"
    # Merchandising rails, which are the same leak wearing a different heading:
    # LiteAF ends every product page with "Top rated products LiteAF Multi-Day
    # 35L Frameless Ultralight Backpack", and a $8 pack of adhesive tabs was
    # reading that as its own capacity.
    r"top (?:rated|selling) products?|best ?sellers?|featured products?|"
    r"new arrivals?|trending now|recently viewed|shop by|product filters?|"
    r"join our newsletter|sign up|subscribe|follow us|"
    r"all rights reserved)\b", re.I)


def _squash(s):
    return re.sub(r"\s+", " ", s or "").strip()


def product_scope(text, description, before=400, after=3000):
    """The part of a stripped page that is about THIS product.

    Anchored on the product's own schema.org description, because that string
    is unique to the product and sits exactly where the detail block follows
    it. Anchoring on headings instead does not survive contact with real
    stores: Baboon inlines its entire catalogue into every product page — 3 MB
    of stripped text — so heading anchors returned the same 16 KB to every
    product and handed a dopp kit a laptop sleeve and a 17" laptop fit.

    Returns "" when the description cannot be located, which is the honest
    answer: better an empty scope and a known gap than confident wrong specs.
    """
    probe = _squash(description)[:80]
    if len(probe) < 30:
        return ""
    flat = _squash(text)
    at = flat.find(probe)
    if at < 0:
        return ""
    chunk = flat[max(0, at - before):at + len(_squash(description)) + after]
    end = SCOPE_END.search(chunk)
    if end:
        chunk = chunk[:end.start()]
    return chunk


JSON_BLOCK = re.compile(
    r'<script[^>]+type=["\']application/json["\'][^>]*>(.*?)</script>',
    re.S | re.I)

# Cheap gate before parsing a block: no point running json.loads over a 5 KB
# analytics payload to discover it has nothing to do with the product.
SPEC_KEY = re.compile(
    r'"(?:dimension|measurement|spec|height|width|depth|volume|capacity|'
    r'weight|litre|liter)', re.I)

# Metafield blocks nest arbitrarily and name things inconsistently, so walk for
# anything whose key looks like a spec and whose value looks like a measurement.
SPEC_LABEL = re.compile(
    r"dimension|measurement|size|spec|height|width|depth|volume|capacity|weight",
    re.I)


def walk_specs(node, out, depth=0):
    """Collect spec-shaped strings out of a theme's embedded JSON."""
    if depth > 8:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str) and SPEC_LABEL.search(str(key)):
                out["spec_text"] = (out.get("spec_text", "") + "\n"
                                    + f"{key}: {strip_html(value)}")[:4000]
            else:
                walk_specs(value, out, depth + 1)
    elif isinstance(node, list):
        for item in node[:200]:
            walk_specs(item, out, depth + 1)


def walk_ld(node, out):
    """schema.org blocks nest inconsistently — @graph, arrays, bare objects."""
    if isinstance(node, list):
        for n in node:
            walk_ld(n, out)
    elif isinstance(node, dict):
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        if "Product" in types:
            out.append(node)
        for key in ("@graph", "mainEntity", "itemListElement"):
            if key in node:
                walk_ld(node[key], out)


# How close two capacities have to sit to be reading as one list, and how many
# distinct ones it takes before that list is furniture rather than a spec.
CAPACITY_RUN_SPAN = 200
CAPACITY_RUN_COUNT = 3


def strip_capacity_lists(text):
    """
    Blank the spans that name several capacities at once.

    A product states its own capacity once. A nav rail, a filter sidebar and a
    related-products carousel all name a handful together — Bonfus's menu reads
    "Aerus 55L Duos 2p Middus 1p Framus 58L Maxus 80L", LiteAF's facets read
    "20L 30L 35L 40L 46L" — and a parser reading the first figure it meets takes
    one of those and publishes it as the product's.

    Anchoring on a Volume/Capacity label deals with the whole-page fallback, but
    not with this: on a store whose menu markup sits inside the product region,
    the rail lands in the scoped text too, where a labelled figure is not
    required and should not be. So the shape is what gets recognised. Blanking
    the run rather than rejecting the source keeps a page that carries both a
    menu and a real spec, which is most of them.

    A labelled run is left alone, because the other thing that lists several
    capacities together is a pack itemising itself. ULA prints "TOTAL VOLUME:
    3,274 CI | 54 L ... Main Body: 2184 CI | 35.8 L ... Internal Zip Pockets:
    210 CI | 3.4 L", which is four figures inside 200 characters and the best
    volume data on the page. The rail names products and the breakdown names
    compartments, and the label is what tells them apart.
    """
    marks = [(m.start(), m.end(), m.group(1)) for m in VOLUME_RE.finditer(text or "")]
    if len(marks) < CAPACITY_RUN_COUNT:
        return text

    out, run = text, []
    for mark in marks + [None]:
        if run and (mark is None or mark[0] - run[-1][1] > CAPACITY_RUN_SPAN):
            start, end = run[0][0], run[-1][1]
            # Reach back past the first figure so a heading introducing the
            # block ("PACK VOLUME") counts as labelling it.
            labelled = PAGE_LABEL["volume"].search(
                text, max(0, start - CAPACITY_RUN_SPAN), end)
            if len({v for _, _, v in run}) >= CAPACITY_RUN_COUNT and not labelled:
                out = out[:start] + " " * (end - start) + out[end:]
            run = []
        if mark is not None:
            run.append(mark)
    return out


def parse_product_page(html_text):
    """Returns dict of whatever we could establish from one product page."""
    found = {}

    products = []
    for m in LD_RE.finditer(html_text):
        try:
            walk_ld(json.loads(m.group(1).strip()), products)
        except json.JSONDecodeError:
            continue

    for p in products:
        gtin = next((p[k] for k in ("gtin13", "gtin12", "gtin14", "gtin", "mpn")
                     if p.get(k)), None)
        if gtin and "gtin" not in found:
            found["gtin"] = str(gtin)
        desc = p.get("description")
        if desc:
            found.setdefault("ld_description", strip_html(desc)[:2000])
        offers = p.get("offers")
        offers = offers if isinstance(offers, list) else [offers] if offers else []
        for o in offers:
            if isinstance(o, dict) and o.get("priceCurrency"):
                found.setdefault("currency", o["priceCurrency"])
                break

    # Theme-embedded JSON. Some stores put the metafields the feed hides into a
    # <script type="application/json"> block; most put nothing useful there at
    # all. Worth reading when present, not worth relying on — Able Carry's
    # product pages carry no spec text or spec JSON whatsoever, because the
    # spec table is rendered client-side after load.
    for block in JSON_BLOCK.finditer(html_text):
        raw = block.group(1).strip()
        if len(raw) > 400_000 or not SPEC_KEY.search(raw):
            continue
        try:
            walk_specs(json.loads(raw), found)
        except (json.JSONDecodeError, ValueError):
            continue

    text = strip_html(html_text)
    window = SPEC_WINDOW.search(text)
    scoped = window.group(0) if window else ""
    detail = product_scope(text, found.get("ld_description", ""))

    # Feature and material detection previously ran only against `body_html`,
    # which for brands like Aer is three sentences of marketing copy with no
    # feature vocabulary in it — so those bags landed with empty feature lists
    # that the filters then read as "does not have it".
    #
    # Deliberately NOT the whole page: `detail` is bounded by SCOPE_END so
    # related-product rails cannot leak one bag's features onto another.
    product_text = "\n".join(filter(None, [
        found.get("ld_description", ""),
        scoped,
        detail,
        found.get("spec_text", ""),
    ]))
    if product_text.strip():
        found["features"] = detect(product_text, FEATURES)
        found["materials"] = detect(product_text, MATERIALS)
        found["features_source"] = "product-page"

    # The last resort is the whole page, and it is only safe on a page that is
    # actually about one product. Baboon's pages carry the entire catalogue
    # inline, and reading those end-to-end is how a dopp kit and a drawstring
    # pouch both ended up recorded as fitting a 17" laptop — a number lifted
    # from a backpack elsewhere in the same HTML. Above this size the page is a
    # catalogue, not a product, and the scoped sources are all we trust.
    whole_page = text if len(text) < 60_000 else ""
    for source, product_scoped in ((scoped, True), (detail, True),
                                   (found.get("ld_description", ""), True),
                                   (whole_page, False)):
        if not source:
            continue
        if "dims_cm" not in found:
            dims, src = find_dims_cm(source)
            if dims:
                found["dims_cm"] = dims
                found["dims_source"] = f"product-page:{src.split(':')[-1]}"
        if "laptop_in" not in found:
            lap = find_laptop_in(source)
            if lap:
                found["laptop_in"] = lap
        if "volume_l" not in found:
            # A litre figure is the one spec that carries its own label, which
            # makes it the one every storefront prints as furniture. Nav rails
            # and filter facets read "20L 30L 35L 40L 46L", and on the
            # whole-page fallback that is what a bare sweep finds first: every
            # ULA product came back 30 L, every Bonfus 55 L, every LiteAF 20 L
            # — one number per brand, off the chrome, on packs and accessories
            # alike. The size guard above does not help, because a 5 KB
            # accessory page has a filter sidebar too.
            #
            # So off the scoped text a sweep is still right, and off the whole
            # page only a labelled figure counts. Dims and weights keep the
            # sweep: chrome rarely prints an H x W x D or a gram figure, and
            # when it does it is a real one.
            pruned = strip_capacity_lists(source)
            vol = (find_volume_bag(pruned) if product_scoped
                   else page_near_label(pruned, "volume", find_volume_bag))
            if vol:
                found["volume_l"] = vol
                found["volume_source"] = "product-page"
        if "weight_g" not in found:
            # Under the label first, then the sweep.
            #
            # The note above says weights keep the bare sweep because chrome
            # rarely prints a gram figure. True — but accessories do, and they
            # print it *above* the pack's. LiteAF lists four hip-belt options
            # ("Small Hip Belt (28″ – 32″ 4.7 Oz)") and then, further down,
            # "Weights & Measurements / 31.8 Oz without hip belt". First match
            # wins took the smallest belt and published a 902 g pack as 133 g,
            # which then led the lightest-per-litre table for daypacks.
            #
            # Scoping to the label alone would lose the pages that state a
            # weight and never label it, so this is a preference rather than a
            # replacement: a labelled figure if the page has one, the old
            # behaviour if it does not.
            wt = (page_near_label(source, "weight", find_weight_g)
                  or find_weight_g(source))
            if wt:
                found["weight_g"] = wt
                found["weight_source"] = "product-page"
        if "dims_cm" in found and "laptop_in" in found:
            break

    # Why did this page give us nothing? Recorded per product so the brands
    # that need their own adapter come out as a ranked list rather than a
    # hunch. Must run *after* extraction — checking before it means every
    # product looks like a failure.
    if "dims_cm" not in found:
        # Three different failures, and conflating them sends the next person
        # to fix the wrong thing:
        #   no-spec-text  the page has no spec prose at all — JS-rendered, so
        #                 only a per-brand adapter helps.
        #   not-published the prose is there and states no H×W×D anywhere. The
        #                 cottage ultralight makers are all like this (ULA,
        #                 LiteAF, Bonfus): a frameless pack is specified by
        #                 volume, torso range and weight, because external
        #                 dimensions of a soft bag are not a meaningful number.
        #                 Nothing to fix — the data does not exist.
        #   unparsed      dimension-shaped text is present and the regexes
        #                 missed it. This is the only bucket a parser change
        #                 can win, and `--reparse` tests a fix for free.
        if not scoped and "dimension" not in text.lower():
            found["_no_dims"] = "no-spec-text"
        elif DIM_SHAPED.search(scoped or text):
            found["_no_dims"] = "unparsed"
        else:
            found["_no_dims"] = "not-published"

    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--brand", default="",
                    help="comma-separated brand slugs")
    ap.add_argument("--refresh", action="store_true", help="ignore cache")
    ap.add_argument("--cache-pages", action="store_true",
                    help="keep the raw page in data/page-cache for --reparse")
    ap.add_argument("--reparse", action="store_true",
                    help="re-run the parser over cached pages, no network")
    ap.add_argument("--refresh-missing-pages", action="store_true",
                    help="re-crawl only the cached entries whose page was "
                         "never kept, so --reparse can reach them")
    ap.add_argument("--give-up-after", type=int, default=8,
                    help="stop after N consecutive 429/5xx across different "
                         "stores — an IP block, not a blip; continuing "
                         "extends it")
    ap.add_argument("--cool-off", type=int, default=4,
                    help="drop a single store from the run after N "
                         "consecutive failures; the rest of the crawl "
                         "carries on without it")
    args = ap.parse_args()

    # --refresh empties the cache rather than reading it, and every write is
    # open(CACHE, "w") on what this run collected — so the two together find
    # nothing to refresh and then publish the emptiness. Refused rather than
    # allowed to no-op, because the no-op is not silent: it overwrites.
    if args.refresh_missing_pages and args.refresh:
        ap.error("--refresh-missing-pages reads the cache to find its gaps; "
                 "--refresh discards the cache. Pick one.")
    # Re-crawling a page and then not keeping it would rebuild the same gap it
    # was asked to close, so the flag carries --cache-pages rather than
    # trusting the caller to remember it.
    if args.refresh_missing_pages:
        args.cache_pages = True

    if not os.path.exists(BAGS):
        sys.exit("no data/bags.json — run normalize.py first")
    payload = json.load(open(BAGS))
    bags = payload["bags"]

    brands_wanted = {s.strip() for s in args.brand.split(",") if s.strip()}

    cache = {}
    if os.path.exists(CACHE) and not args.refresh:
        try:
            cache = json.load(open(CACHE))
        except json.JSONDecodeError:
            cache = {}

    if args.reparse:
        # No network at all. Rebuild every cached entry from the page we kept,
        # so a parser change can be evaluated against the real corpus in
        # seconds instead of a fresh crawl.
        reparsed = 0
        unreachable = 0
        for bag in dedupe(bags, page_key):
            if brands_wanted and bag["brand_slug"] not in brands_wanted:
                continue
            html_text = load_page(page_key(bag))
            if html_text is None:
                # Cached, but the page behind it was never kept — so this
                # parser change does not reach it. Counted rather than passed
                # over in silence: a reparse that says only what it rebuilt
                # reads as total coverage, and the shortfall took a hand count
                # to notice. --refresh-missing-pages is what closes it.
                if page_key(bag) in cache:
                    unreachable += 1
                continue
            cache[page_key(bag)] = parse_product_page(html_text)
            reparsed += 1
        with open(CACHE, "w") as f:
            json.dump(cache, f, sort_keys=True, indent=1)
        hit = sum(1 for v in cache.values() if v.get("dims_cm"))
        print(f"reparsed {reparsed} cached pages, 0 requests; "
              f"dims now found for {hit}", flush=True)
        if unreachable:
            print(f"  {unreachable} cached entries have no saved page and "
                  f"were not reached — run --refresh-missing-pages to close "
                  f"the gap", flush=True)
        if not reparsed:
            print("  nothing in data/page-cache — crawl once with "
                  "--cache-pages first", flush=True)
    else:
        # The gap --reparse cannot see.
        #
        # --cache-pages arrived after the crawl had already been running, so
        # the entries from before it have a parse result and no page behind
        # them. Every later parser change reaches everything except those, and
        # reaches them silently: --reparse reports what it rebuilt and never
        # what it could not open. This re-crawls exactly that set.
        #
        # Three deliberate choices about what "exactly" means:
        #
        #   in cache          — an entry with no cache line is ordinary
        #                       backlog and the normal path already has it.
        #                       This flag closes gaps; it does not race the
        #                       crawl for the same work.
        #   no page on disk   — the whole and only test. Not gated on missing
        #                       dims, because the point is the page rather
        #                       than the fields: an entry whose dims are
        #                       already known still owes us its HTML, or the
        #                       next regex fix skips it too.
        #   no _status        — 404, 410 and 451 have no page because there is
        #                       no page. Re-asking is the exact behaviour the
        #                       status cache exists to prevent.
        #
        # Nothing is deleted. Each answer overwrites its own cache line with a
        # fresh parse and saves the page, so coverage cannot dip while this
        # runs and the gate sees an ordinary night.
        if args.refresh_missing_pages:
            targets = [b for b in bags
                       if (not brands_wanted or b["brand_slug"] in brands_wanted)
                       and enrichable(b)
                       and page_key(b) in cache
                       and "_status" not in (cache.get(page_key(b)) or {})
                       and not os.path.exists(page_path(page_key(b)))]
        else:
            targets = [b for b in bags
                       if (not brands_wanted or b["brand_slug"] in brands_wanted)
                       and page_key(b) not in cache
                       and enrichable(b)
                       and (b.get("dims_cm") is None
                            or b.get("laptop_in") is None)]
        # One request per page, not per entry. A model split across three
        # capacities is three entries pointing at one product page — see
        # split_sizes() in normalize.py — and asking a store for the same URL
        # three times a night is exactly the behaviour the pacing here exists
        # to avoid. First entry wins the crawl; all three read its cache line.
        targets = dedupe(targets, page_key)
        # Round-robin across brands rather than finishing one store before
        # starting the next. Two reasons, and the first is the one that bit:
        # grouped order means 32 consecutive requests to one store, so a single
        # throttled store stalls the entire crawl behind it — a run against 33
        # brands spent twelve minutes on Baboon and never reached brand two.
        # Interleaved, each store sees a request roughly every (delay × brands)
        # seconds instead of every `delay`, which is also markedly politer.
        targets = interleave(targets, lambda b: b["brand_slug"])
        if args.limit:
            targets = targets[:args.limit]

        brands_hit = len({b['brand_slug'] for b in targets})
        if args.refresh_missing_pages:
            print(f"{len(targets)} cached entries have no saved page, across "
                  f"{brands_hit} brands — re-crawling to close the gap "
                  f"({len(cache)} entries in the cache)", flush=True)
        else:
            print(f"{len(targets)} products to enrich across "
                  f"{brands_hit} brands "
                  f"({len(cache)} already cached)", flush=True)

        pacer = Pacer(args.delay)
        transient = 0
        robots_skipped = 0
        # Two circuit breakers, because "one store is angry" and "our IP is
        # blocked" need opposite responses and the old loop conflated them.
        #
        # Per-brand: a 429 puts just that store on ice until its Retry-After
        # elapses; we keep working through the others meanwhile. The old code
        # slept 60s globally on any 429, so one throttled store cost every
        # brand its throughput. After --cool-off strikes a brand is dropped
        # from the run entirely and reported.
        #
        # Global: with interleaved order, consecutive failures are consecutive
        # *different* stores, so a streak really does mean the IP is blocked
        # rather than one store objecting. That is when to stop knocking —
        # each 429 served while blocked is what extends the block.
        # Distinct stores in the current failure run, not failures. The line
        # above says a streak means different stores, and that was true only
        # while interleaving kept them different — which holds when the crawl
        # has many brands of work left and stops holding at the end, when the
        # queue is down to the two or three shops that refuse us and the same
        # ones cycle. Counting failures there reaches eight by asking four
        # stores twice, and reports an IP block on the night the crawl has
        # nothing left but its known refusers. Counting stores says what the
        # breaker is actually for: it takes eight *different* shops turning us
        # away at once to mean the problem is us.
        streak_stores = set()
        landed = 0       # pages that answered and parsed
        aborted = False  # the global breaker fired: IP-wide block
        cooling = {}     # brand -> epoch seconds it may be tried again
        next_ok = {}     # brand -> earliest epoch its Crawl-delay permits
        strikes = {}     # brand -> consecutive transient failures
        cooled_out = {}  # brand -> status that retired it
        deferred = []
        i = 0
        queue = list(targets)
        while queue:
            bag = queue.pop(0)
            slug = bag["brand_slug"]
            if slug in cooled_out:
                continue
            if time.time() < cooling.get(slug, 0):
                deferred.append(bag)
                continue
            # robots.txt, one cached parse per domain (fetch.robots). A
            # disallowed path is cached like a 404 — a permanent answer, not
            # worth re-asking nightly.
            parts = urlsplit(bag["url"])
            allowed, _why = robots_allows(parts.netloc, parts.path)
            if not allowed:
                cache[page_key(bag)] = {"_status": "robots-disallowed"}
                robots_skipped += 1
                continue
            _, crawl_delay, _ = robots(parts.netloc)
            i += 1
            pacer.wait()
            if crawl_delay:
                # A declared Crawl-delay is per store, not global, so it is a
                # sleep here rather than an entry in `cooling`: a cooling
                # store's bags get deferred, and deferred bags are dropped
                # when the queue empties — which would ration a declaring
                # store to one page a night once its brand dominates the
                # queue's tail. Interleaving spaces a store's requests by
                # (delay × live brands), so this sleep is zero until then.
                behind = next_ok.get(slug, 0.0) - time.time()
                if behind > 0:
                    time.sleep(behind)
            status, headers, body = get(bag["url"], timeout=25)
            if crawl_delay:
                next_ok[slug] = time.time() + crawl_delay
            if status in (429, 0) or 500 <= status < 600:
                if status == 429:
                    wait_s = float(headers.get("Retry-After", 60) or 60)
                    cooling[slug] = time.time() + wait_s + 2
                else:
                    cooling[slug] = time.time() + 30
                strikes[slug] = strikes.get(slug, 0) + 1
                streak_stores.add(slug)
                if strikes[slug] >= args.cool_off:
                    cooled_out[slug] = status
                    print(f"  dropping {slug}: {strikes[slug]} consecutive "
                          f"failures (status {status})", flush=True)
                if len(streak_stores) >= args.give_up_after:
                    aborted = True
                    with open(CACHE, "w") as f:
                        json.dump(cache, f, sort_keys=True, indent=1)
                    print(f"\n  ABORTING: {len(streak_stores)} different "
                          f"stores turned us away without a success between "
                          f"them (last status {status}) at product {i}. That "
                          f"is an IP-wide block, not one store objecting — "
                          f"knocking harder makes it last longer. "
                          f"{len(cache)} products are cached; rerun later to "
                          f"resume.", flush=True)
                    break
                # Not lost: try it again on the deferred pass once the store's
                # cooldown has elapsed.
                deferred.append(bag)
            else:
                streak_stores.clear()
                strikes[slug] = 0
            if not queue and deferred:
                # Second pass over everything a cooling store owed us. Only
                # once — anything still failing after this is a brand problem
                # for the next run, not something to keep circling on.
                queue, deferred = deferred, []
                print(f"  retrying {len(queue)} deferred products", flush=True)
            if status != 200:
                # Only cache *permanent* answers. A 404 means the page is gone
                # and re-asking tomorrow is rude and pointless; a 429 or a 5xx
                # means try later. Caching those was poisoning products
                # permanently, because the target filter skips anything already
                # in the cache — one rate-limited moment and that bag never got
                # enriched again.
                if status in (404, 410, 451):
                    cache[page_key(bag)] = {"_status": status}
                else:
                    transient += 1
            else:
                text = body.decode("utf-8", "replace")
                if args.cache_pages:
                    save_page(page_key(bag), text)
                cache[page_key(bag)] = parse_product_page(text)
                landed += 1
            if i % 10 == 0 or not queue:
                with open(CACHE, "w") as f:
                    json.dump(cache, f, sort_keys=True, indent=1)
                hit = sum(1 for v in cache.values() if v.get("dims_cm"))
                print(f"  [{i}/{len(targets)}] dims found for {hit}", flush=True)

        with open(CACHE, "w") as f:
            json.dump(cache, f, sort_keys=True, indent=1)
        if transient:
            print(f"  {transient} transient failures (429/5xx) left uncached "
                  f"— rerun to pick them up", flush=True)
        if robots_skipped:
            print(f"  {robots_skipped} products skipped — robots.txt "
                  f"disallows their path; cached so they are not re-asked",
                  flush=True)
        if cooled_out:
            print("  stores dropped this run (throttling us, try later): "
                  + ", ".join(f"{k} [{v}]" for k, v in cooled_out.items()),
                  flush=True)

        # `attempted` is i: the requests actually issued, after robots and the
        # per-store cooldowns have taken their share out of the target list.
        # That is the number the health check needs — a night with nothing to
        # ask for and a night that asked and got nothing are the two cases it
        # has to tell apart, and only this end of the pipe can see which.
        with open(RUN, "w") as f:
            json.dump({"attempted": i, "landed": landed,
                       "transient": transient, "aborted": aborted,
                       "dropped": sorted(cooled_out),
                       "mode": ("refresh" if args.refresh_missing_pages
                                else "crawl")}, f, indent=1)

    # Merge. Enrichment fills gaps and does not overwrite feed data, except for
    # the weight sources that are answering a different question — a shipping
    # box, a fabric, an accessory. A weight stated on the product page is the
    # spec; see SUPERSEDED_WEIGHT_SOURCES for which figures it beats and, more
    # to the point, which labelled ones it does not.
    filled = weight_fixed = feat_gained = withheld = 0
    for bag in bags:
        extra = cache.get(page_key(bag)) or {}

        # A split entry only takes the page's size-dependent figures when the
        # page is demonstrably talking about its size.
        if bag.get("split_from") and extra.get("volume_l") != bag.get("volume_l"):
            if any(extra.get(f) is not None for f in SIZED_PAGE_FIELDS):
                withheld += 1
            extra = {k: v for k, v in extra.items()
                     if k not in SIZED_PAGE_FIELDS}

        if (extra.get("weight_g")
                and bag.get("weight_source") in SUPERSEDED_WEIGHT_SOURCES
                and extra["weight_g"] != bag.get("weight_g")):
            bag["weight_g"] = extra["weight_g"]
            bag["weight_source"] = extra.get("weight_source", "product-page")
            weight_fixed += 1

        for field in ("dims_cm", "dims_source", "laptop_in", "volume_l",
                      "volume_source", "weight_g", "weight_source", "gtin"):
            if extra.get(field) is not None and bag.get(field) is None:
                bag[field] = extra[field]
                if field == "dims_cm":
                    bag["linear_cm"] = round(sum(extra[field]), 1)
                    filled += 1

        # Features and materials union rather than overwrite: the feed and the
        # page each see things the other misses. `features_source` is what the
        # front end reads to tell "we looked and it has none" apart from "we
        # never got a good look" — the distinction the filters were getting
        # wrong, silently dropping bags that do have the feature.
        if extra.get("features_source"):
            before = len(bag.get("features") or [])
            bag["features"] = sorted(set(bag.get("features") or [])
                                     | set(extra.get("features") or []))
            bag["materials"] = sorted(set(bag.get("materials") or [])
                                      | set(extra.get("materials") or []))
            bag["features_source"] = "product-page"
            if len(bag["features"]) > before:
                feat_gained += 1
        else:
            bag.setdefault("features_source", "description")

    def coverage(field):
        n = sum(1 for b in bags if b.get(field) is not None)
        return {"have": n, "pct": round(100.0 * n / len(bags)) if bags else 0}

    payload["meta"]["coverage"] = {
        f: coverage(f) for f in
        ("volume_l", "dims_cm", "weight_g", "laptop_in", "price_min")}
    payload["meta"]["enriched"] = True
    payload["meta"]["features_scanned"] = sum(
        1 for b in bags if b.get("features_source") == "product-page")

    # Which brands need their own adapter, ranked. "no-spec-text" means the
    # page genuinely has no specs in its HTML — the store renders them with
    # JavaScript, so no amount of better parsing will help and only a per-brand
    # adapter will. "unparsed" means the text is there and we failed to read
    # it, which is a regex problem and much cheaper to fix.
    diag = {}
    for bag in bags:
        entry = cache.get(page_key(bag)) or {}
        # Gate on the entry genuinely lacking dimensions rather than trusting
        # the recorded reason: a cache written by an older parser can carry a
        # stale `_no_dims` next to dimensions it did in fact find.
        why = entry.get("_no_dims") if not entry.get("dims_cm") else None
        if why:
            slug = bag["brand_slug"]
            diag.setdefault(slug, {"no-spec-text": 0, "not-published": 0,
                                   "unparsed": 0})
            diag[slug][why] = diag[slug].get(why, 0) + 1
    payload["meta"]["enrich_gaps"] = diag

    with open(BAGS, "w") as f:
        json.dump(payload, f, separators=(",", ":"))

    print(f"\nfilled dimensions for {filled} bags; "
          f"corrected {weight_fixed} shipping-weight values; "
          f"added features to {feat_gained}")
    if withheld:
        print(f"  held back page specs from {withheld} split entries whose "
              f"size the page does not state")
    print(json.dumps(payload["meta"]["coverage"], indent=2))
    if diag:
        print("\nwhere dimensions are still missing:")
        for slug, counts in sorted(
                diag.items(), key=lambda kv: -sum(kv[1].values())):
            print(f"  {slug:16} js-rendered={counts['no-spec-text']:4} "
                  f"not-published={counts.get('not-published', 0):4} "
                  f"unparsed={counts['unparsed']:4}")


if __name__ == "__main__":
    main()
