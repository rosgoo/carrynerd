#!/usr/bin/env python3
"""
gearherd enricher — stage 2, fills in what the Shopify feed leaves out.

/products.json gives us SKUs, colourways, prices, stock and weight, but brands
keep dimensions and laptop fit in metafields that the feed does not expose.
Those render into the product page, and almost every store also emits a
schema.org Product block for Google Shopping. So stage 2 fetches each product
page once and reads both.

This is one request per product, so it is the expensive stage. It caches every
page result and is fully resumable — rerun it and it picks up where it stopped.
Run it from a residential connection; Shopify's edge throttles datacenter IPs
on these endpoints hard enough to make it impractical from a cloud host.

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

from normalize import (CM_PER_IN, FEATURES, MATERIALS, detect, find_dims_cm,
                       find_laptop_in, find_volume, find_weight_g, strip_html)
from fetch import Pacer, get

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "data", "enrich-cache.json")
BAGS = os.path.join(HERE, "data", "bags.json")

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

LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I)

# Brands overwhelmingly label the spec block with one of these words, so we
# narrow to that neighbourhood before pattern matching. Searching the whole
# page produces false positives from unrelated copy and size charts.
SPEC_WINDOW = re.compile(
    r"(dimensions?|measurements?|specs?\b|specifications?|exterior|external)"
    r"(.{0,600})", re.I | re.S)

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

    # Feature and material detection previously ran only against `body_html`,
    # which for brands like Aer is three sentences of marketing copy with no
    # feature vocabulary in it — so those bags landed with empty feature lists
    # that the filters then read as "does not have it".
    #
    # Deliberately NOT the whole page. Site chrome, nav and related-product
    # rails would leak one bag's features onto every other bag in the store.
    # The product's own description plus the spec block is the honest scope.
    product_text = "\n".join(filter(None, [
        found.get("ld_description", ""),
        scoped,
        found.get("spec_text", ""),
    ]))
    if product_text.strip():
        found["features"] = detect(product_text, FEATURES)
        found["materials"] = detect(product_text, MATERIALS)
        found["features_source"] = "product-page"

    for source in (scoped, found.get("ld_description", ""), text):
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
            vol = find_volume(source)
            if vol:
                found["volume_l"] = vol
                found["volume_source"] = "product-page"
        if "weight_g" not in found:
            wt = find_weight_g(source)
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
        found["_no_dims"] = (
            "no-spec-text" if not scoped and "dimension" not in text.lower()
            else "unparsed")

    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--brand", default="")
    ap.add_argument("--refresh", action="store_true", help="ignore cache")
    ap.add_argument("--cache-pages", action="store_true",
                    help="keep the raw page in data/page-cache for --reparse")
    ap.add_argument("--reparse", action="store_true",
                    help="re-run the parser over cached pages, no network")
    args = ap.parse_args()

    if not os.path.exists(BAGS):
        sys.exit("no data/bags.json — run normalize.py first")
    payload = json.load(open(BAGS))
    bags = payload["bags"]

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
        for bag in bags:
            if args.brand and bag["brand_slug"] != args.brand:
                continue
            html_text = load_page(bag["id"])
            if html_text is None:
                continue
            cache[bag["id"]] = parse_product_page(html_text)
            reparsed += 1
        with open(CACHE, "w") as f:
            json.dump(cache, f)
        hit = sum(1 for v in cache.values() if v.get("dims_cm"))
        print(f"reparsed {reparsed} cached pages, 0 requests; "
              f"dims now found for {hit}", flush=True)
        if not reparsed:
            print("  nothing in data/page-cache — crawl once with "
                  "--cache-pages first", flush=True)
    else:
        targets = [b for b in bags
                   if (not args.brand or b["brand_slug"] == args.brand)
                   and b["id"] not in cache
                   and (b.get("dims_cm") is None or b.get("laptop_in") is None)]
        if args.limit:
            targets = targets[:args.limit]

        print(f"{len(targets)} products to enrich "
              f"({len(cache)} already cached)", flush=True)

        pacer = Pacer(args.delay)
        transient = 0
        for i, bag in enumerate(targets, 1):
            pacer.wait()
            status, headers, body = get(bag["url"], timeout=25)
            if status == 429:
                wait_s = float(headers.get("Retry-After", 60) or 60)
                print(f"  rate-limited, sleeping {wait_s:.0f}s", flush=True)
                time.sleep(wait_s + 2)
                pacer.wait()
                status, headers, body = get(bag["url"], timeout=25)
            if status != 200:
                # Only cache *permanent* answers. A 404 means the page is gone
                # and re-asking tomorrow is rude and pointless; a 429 or a 5xx
                # means try later. Caching those was poisoning products
                # permanently, because the target filter skips anything already
                # in the cache — one rate-limited moment and that bag never got
                # enriched again.
                if status in (404, 410, 451):
                    cache[bag["id"]] = {"_status": status}
                else:
                    transient += 1
            else:
                text = body.decode("utf-8", "replace")
                if args.cache_pages:
                    save_page(bag["id"], text)
                cache[bag["id"]] = parse_product_page(text)
            if i % 10 == 0 or i == len(targets):
                with open(CACHE, "w") as f:
                    json.dump(cache, f)
                hit = sum(1 for v in cache.values() if v.get("dims_cm"))
                print(f"  [{i}/{len(targets)}] dims found for {hit}", flush=True)

        with open(CACHE, "w") as f:
            json.dump(cache, f)
        if transient:
            print(f"  {transient} transient failures (429/5xx) left uncached "
                  f"— rerun to pick them up", flush=True)

    # Merge. Enrichment fills gaps and does not overwrite feed data, with one
    # exception: Shopify's `grams` is a shipping field, and plenty of stores
    # put the packed box weight in it (Aer's 35L Travel Pack reports 4536 g —
    # exactly 10 lb — against a real 1.77 kg). A weight stated on the product
    # page is the spec; grams is packaging. So the page wins.
    filled = weight_fixed = feat_gained = 0
    for bag in bags:
        extra = cache.get(bag["id"]) or {}

        if (extra.get("weight_g") and bag.get("weight_source") == "shopify:grams"
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
        entry = cache.get(bag["id"]) or {}
        # Gate on the entry genuinely lacking dimensions rather than trusting
        # the recorded reason: a cache written by an older parser can carry a
        # stale `_no_dims` next to dimensions it did in fact find.
        why = entry.get("_no_dims") if not entry.get("dims_cm") else None
        if why:
            slug = bag["brand_slug"]
            diag.setdefault(slug, {"no-spec-text": 0, "unparsed": 0})
            diag[slug][why] += 1
    payload["meta"]["enrich_gaps"] = diag

    with open(BAGS, "w") as f:
        json.dump(payload, f, separators=(",", ":"))

    print(f"\nfilled dimensions for {filled} bags; "
          f"corrected {weight_fixed} shipping-weight values; "
          f"added features to {feat_gained}")
    print(json.dumps(payload["meta"]["coverage"], indent=2))
    if diag:
        print("\nwhere dimensions are still missing:")
        for slug, counts in sorted(
                diag.items(), key=lambda kv: -sum(kv[1].values())):
            print(f"  {slug:16} js-rendered={counts['no-spec-text']:4} "
                  f"unparsed={counts['unparsed']:4}")


if __name__ == "__main__":
    main()
