#!/usr/bin/env python3
"""
bagdex enricher — stage 2, fills in what the Shopify feed leaves out.

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
import json
import os
import re
import sys
import time

from normalize import (CM_PER_IN, find_dims_cm, find_laptop_in, find_volume,
                       find_weight_g, strip_html)
from fetch import Pacer, get

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "data", "enrich-cache.json")
BAGS = os.path.join(HERE, "data", "bags.json")

LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I)

# Brands overwhelmingly label the spec block with one of these words, so we
# narrow to that neighbourhood before pattern matching. Searching the whole
# page produces false positives from unrelated copy and size charts.
SPEC_WINDOW = re.compile(
    r"(dimensions?|measurements?|specs?\b|specifications?|exterior|external)"
    r"(.{0,600})", re.I | re.S)


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

    text = strip_html(html_text)
    window = SPEC_WINDOW.search(text)
    scoped = window.group(0) if window else ""

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

    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--brand", default="")
    ap.add_argument("--refresh", action="store_true", help="ignore cache")
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

    targets = [b for b in bags
               if (not args.brand or b["brand_slug"] == args.brand)
               and b["id"] not in cache
               and (b.get("dims_cm") is None or b.get("laptop_in") is None)]
    if args.limit:
        targets = targets[:args.limit]

    print(f"{len(targets)} products to enrich "
          f"({len(cache)} already cached)", flush=True)

    pacer = Pacer(args.delay)
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
            cache[bag["id"]] = {"_status": status}
        else:
            cache[bag["id"]] = parse_product_page(
                body.decode("utf-8", "replace"))
        if i % 10 == 0 or i == len(targets):
            with open(CACHE, "w") as f:
                json.dump(cache, f)
            hit = sum(1 for v in cache.values() if v.get("dims_cm"))
            print(f"  [{i}/{len(targets)}] dims found for {hit}", flush=True)

    with open(CACHE, "w") as f:
        json.dump(cache, f)

    # Merge. Enrichment fills gaps and does not overwrite feed data, with one
    # exception: Shopify's `grams` is a shipping field, and plenty of stores
    # put the packed box weight in it (Aer's 35L Travel Pack reports 4536 g —
    # exactly 10 lb — against a real 1.77 kg). A weight stated on the product
    # page is the spec; grams is packaging. So the page wins.
    filled = weight_fixed = 0
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

    def coverage(field):
        n = sum(1 for b in bags if b.get(field) is not None)
        return {"have": n, "pct": round(100.0 * n / len(bags)) if bags else 0}

    payload["meta"]["coverage"] = {
        f: coverage(f) for f in
        ("volume_l", "dims_cm", "weight_g", "laptop_in", "price_min")}
    payload["meta"]["enriched"] = True

    with open(BAGS, "w") as f:
        json.dump(payload, f, separators=(",", ":"))

    print(f"\nfilled dimensions for {filled} bags; "
          f"corrected {weight_fixed} shipping-weight values")
    print(json.dumps(payload["meta"]["coverage"], indent=2))


if __name__ == "__main__":
    main()
