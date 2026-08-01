#!/usr/bin/env python3
"""
bagdex fetcher — pulls public Shopify product catalogs.

Only touches endpoints that stores publish for anyone: /products.json (the
storefront JSON feed Shopify serves by default) and /robots.txt. No login, no
accounts, no CAPTCHA solving, no proxy rotation. If a store's robots.txt
disallows the path, we skip it and record why.

Shopify rate-limits /products.json per client IP at its edge. Residential
connections rarely trip it; datacenter/cloud IPs get `local_rate_limited` with
a Retry-After header almost immediately. We honour Retry-After and pace
globally, because the limit is applied per-IP across all Shopify stores rather
than per-store.

Usage:
    python3 fetch.py                  # default 2s pacing (residential)
    python3 fetch.py --delay 65       # cloud/datacenter IP
    python3 fetch.py --only aer,pakt  # subset
    python3 fetch.py --force          # refetch even if cached
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "raw")
LOG = os.path.join(HERE, "data", "fetch-log.json")

# Identify the crawler honestly and give operators a way to reach you.
# Put a real contact URL here before running this at any scale.
CONTACT = os.environ.get("BAGDEX_CONTACT", "https://example.com/bagdex-bot")
UA = f"bagdex/0.1 (product catalog indexer; +{CONTACT})"

PAGE_LIMIT = 250
MAX_PAGES = 12
MAX_RETRIES = 4


def get(url, timeout=30):
    """Single GET. Returns (status, headers, body_bytes)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), (e.read() or b"")
    except Exception as e:
        return 0, {"x-error": str(e)}, b""


def robots_allows(domain, path="/products.json"):
    """
    Minimal robots.txt check for User-agent: * — good enough for a feed we only
    hit once per brand. Returns (allowed: bool, note: str).
    """
    status, _, body = get(f"https://{domain}/robots.txt", timeout=20)
    if status != 200:
        return True, f"robots.txt status {status}; proceeding"
    lines = body.decode("utf-8", "replace").splitlines()
    applies, disallows = False, []
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, value = (p.strip() for p in line.split(":", 1))
        field = field.lower()
        if field == "user-agent":
            applies = value == "*"
        elif field == "disallow" and applies and value:
            disallows.append(value)
    for rule in disallows:
        if path.startswith(rule):
            return False, f"robots.txt disallows {rule}"
    return True, "allowed"


class Pacer:
    """Global spacing between outbound requests, with Retry-After support."""

    def __init__(self, delay):
        self.delay = delay
        self.last = 0.0

    def wait(self, extra=0.0):
        gap = (self.last + self.delay + extra) - time.time()
        if gap > 0:
            time.sleep(gap)
        self.last = time.time()


def fetch_brand(brand, pacer):
    domain = brand["domain"]
    result = {
        "slug": brand["slug"],
        "name": brand["name"],
        "domain": domain,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": None,
        "status": None,
        "note": "",
        "products": [],
    }

    pacer.wait()
    allowed, note = robots_allows(domain)
    if not allowed:
        result["status"] = "skipped"
        result["note"] = note
        return result

    products, page = [], 1
    while page <= MAX_PAGES:
        url = f"https://{domain}/products.json?limit={PAGE_LIMIT}&page={page}"
        body = None
        for attempt in range(MAX_RETRIES):
            pacer.wait()
            status, headers, raw = get(url)
            if status == 200:
                body = raw
                break
            if status == 429:
                wait_s = float(headers.get("Retry-After", 60) or 60)
                sys.stderr.write(
                    f"  {domain} 429 rate-limited, waiting {wait_s:.0f}s "
                    f"(attempt {attempt + 1}/{MAX_RETRIES})\n")
                sys.stderr.flush()
                time.sleep(wait_s + 2)
                continue
            # 404 = not Shopify (or feed disabled); anything else, back off once.
            result["status"] = f"http_{status}"
            result["note"] = headers.get("x-error", "") or "no products.json"
            return result
        if body is None:
            result["status"] = "rate_limited"
            result["note"] = "exhausted retries against Shopify edge limit"
            return result

        try:
            batch = json.loads(body).get("products", [])
        except json.JSONDecodeError:
            result["status"] = "bad_json"
            return result

        result["platform"] = "shopify"
        if not batch:
            break
        products.extend(batch)
        if len(batch) < PAGE_LIMIT:
            break
        page += 1

    result["status"] = "ok"
    result["products"] = products
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delay", type=float, default=2.0,
                    help="seconds between requests (use ~65 on a cloud IP)")
    ap.add_argument("--only", default="", help="comma-separated brand slugs")
    ap.add_argument("--force", action="store_true", help="refetch cached brands")
    args = ap.parse_args()

    os.makedirs(RAW, exist_ok=True)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)

    with open(os.path.join(HERE, "brands.json")) as f:
        brands = json.load(f)
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        brands = [b for b in brands if b["slug"] in wanted]

    pacer = Pacer(args.delay)
    log = {}
    if os.path.exists(LOG):
        try:
            log = json.load(open(LOG))
        except json.JSONDecodeError:
            log = {}

    for i, brand in enumerate(brands, 1):
        out = os.path.join(RAW, f"{brand['slug']}.json")
        if os.path.exists(out) and not args.force:
            print(f"[{i}/{len(brands)}] {brand['name']}: cached, skipping")
            continue

        print(f"[{i}/{len(brands)}] {brand['name']} ({brand['domain']}) ...",
              flush=True)
        res = fetch_brand(brand, pacer)
        n = len(res["products"])
        if res["status"] == "ok" and n:
            with open(out, "w") as f:
                json.dump(res, f)
        print(f"    -> {res['status']} {n} products {res['note']}", flush=True)

        log[brand["slug"]] = {k: v for k, v in res.items() if k != "products"}
        log[brand["slug"]]["product_count"] = n
        with open(LOG, "w") as f:
            json.dump(log, f, indent=2)

    ok = sum(1 for v in log.values() if v.get("status") == "ok")
    print(f"\ndone: {ok}/{len(log)} brands with catalogs")


if __name__ == "__main__":
    main()
