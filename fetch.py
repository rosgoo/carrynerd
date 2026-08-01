#!/usr/bin/env python3
"""
gearherd fetcher — pulls public Shopify product catalogs.

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
    python3 fetch.py --max-age 20     # refetch catalogues older than 20h
    python3 fetch.py --shard 0/4      # this runner takes every 4th brand

On --shard: the nightly workflow splits the brand list across a small matrix so
the daily price pass finishes inside one Actions run. Each shard is an ordinary
polite client — it paces at --delay, honours Retry-After, and never retries a
block from somewhere else. Splitting the *list* is capacity; splitting a single
brand's requests across runners to outrun a limit would be evasion, and this
does not do that. Keep the shard count low and never raise it in response to a
429.
"""

import argparse
import glob
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "raw")
# Collection membership lives under data/, not raw/. It is small, changes
# only when a brand restructures its site, and normalize.py needs it in CI —
# raw/ is gitignored, so keeping it there would silently strip category ground
# truth from every nightly run.
COLLECTIONS = os.path.join(HERE, "data", "collections")
LOG = os.path.join(HERE, "data", "fetch-log.json")
# Sharded runs each write their own slice of the log; --merge-logs folds them
# back together in the job that assembles the artifacts.
LOG_PART = os.path.join(HERE, "data", "fetch-log.part-{}.json")

# Identify the crawler honestly and give operators a way to reach you.
# Put a real contact URL here before running this at any scale.
CONTACT = os.environ.get("GEARHERD_CONTACT", "https://example.com/gearherd-bot")
UA = f"gearherd/0.1 (product catalog indexer; +{CONTACT})"

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


# --- collection membership --------------------------------------------------
#
# Keyword classification reads the product title and guesses. A store's own
# collections are not a guess: WANDRD shelves "PRVKE Zip 21L" under /backpacks
# even though nothing in the title, product_type or tags says so, and it shelves
# zipper pullers under /accessories. That is ground truth from the only party
# who actually knows.
#
# Only collections whose name maps to something is worth a request, so this
# fetches a handful per brand rather than all 116.

COLLECTION_CATEGORY = [
    ("sling",           r"sling|crossbody"),
    ("hip-pack",        r"hip pack|fanny|waist pack|belt bag|bum bag"),
    ("duffel",          r"duffel|duffle|gym bag"),
    ("luggage",         r"luggage|suitcase|carry[- ]on|roller|spinner"),
    ("tote",            r"tote|shopper"),
    ("messenger",       r"messenger|courier|satchel"),
    ("briefcase",       r"briefcase|portfolio"),
    ("camera-bag",      r"camera bag|camera backpack|photo bag"),
    ("hiking-pack",     r"hiking|backpacking|trekking"),
    ("travel-backpack", r"travel pack|travel backpack"),
    ("daypack",         r"backpack|daypack|rucksack"),
    ("pouch",           r"pouch|dopp|toiletry|organi[sz]er"),
]

# Collections that mean "this is a bag" without saying which kind.
GENERIC_BAG = re.compile(r"\bbags?\b", re.I)

# Collections that mean "this is not a bag at all" — the negative signal that
# keeps zipper pullers and camera cubes out of a bag index.
#
# Checked *after* GENERIC_BAG, because plenty of bag collections are named for
# an accessory they relate to: "Carry Strap Bags" and "Camera Cube Compatible"
# are both shelves of bags, and matching `strap` or `cube` first threw real
# packs out of the index.
ACCESSORY = re.compile(
    r"accessor|apparel|strap|cube|divider|insert|sticker|gift|"
    r"cards?$|patch|pull(?:er)?s?$|clothing|tee|hat", re.I)

# Colour, sale and marketing collections carry no category signal, and "all"
# is every product in the store — a big request for nothing.
NOISE = re.compile(
    r"^\d|off$|%|sale|bundle|best|new\b|featured|gift|upsell|waitlist|"
    r"^all(?:[- ]products)?$|collection$|restock|final|clearance", re.I)


def collection_category(name):
    for category, pattern in COLLECTION_CATEGORY:
        if re.search(pattern, name, re.I):
            return category
    return None


def fetch_collections(brand, pacer):
    """Map product id -> {category, is_bag} using the store's own shelving."""
    domain = brand["domain"]
    out = {"slug": brand["slug"], "domain": domain,
           "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "collections": [], "products": {}}

    pacer.wait()
    status, _, body = get(f"https://{domain}/collections.json?limit=250")
    if status != 200:
        out["status"] = f"http_{status}"
        return out
    try:
        listing = json.loads(body).get("collections", [])
    except json.JSONDecodeError:
        out["status"] = "bad_json"
        return out

    wanted = []
    for c in listing:
        handle = c.get("handle") or ""
        title = c.get("title") or ""
        name = f"{handle} {title}"
        if NOISE.search(handle) or NOISE.search(title):
            continue
        category = collection_category(name)
        if category:
            wanted.append((handle, category, True))
        elif GENERIC_BAG.search(name):
            wanted.append((handle, None, True))
        elif ACCESSORY.search(name):
            wanted.append((handle, None, False))

    print(f"    {len(listing)} collections, {len(wanted)} carry a signal",
          flush=True)

    for handle, category, is_bag in wanted:
        page, seen = 1, 0
        while page <= 4:
            pacer.wait()
            url = (f"https://{domain}/collections/{handle}/products.json"
                   f"?limit={PAGE_LIMIT}&page={page}")
            status, _, body = get(url)
            if status != 200:
                break
            try:
                batch = json.loads(body).get("products", [])
            except json.JSONDecodeError:
                break
            if not batch:
                break
            for product in batch:
                pid = str(product.get("id"))
                entry = out["products"].setdefault(
                    pid, {"category": None, "bag_votes": 0,
                          "accessory_votes": 0, "in": []})
                entry["in"].append(handle)
                if category and not entry["category"]:
                    entry["category"] = category
                # Votes, not last-write-wins. Products land in several
                # collections at once, and being shelved under /backpacks is a
                # much stronger statement than also appearing under
                # /camera-cube-compatible.
                if is_bag:
                    entry["bag_votes"] += 1
                else:
                    entry["accessory_votes"] += 1
                seen += 1
            if len(batch) < PAGE_LIMIT:
                break
            page += 1
        out["collections"].append(
            {"handle": handle, "category": category, "is_bag": is_bag,
             "products": seen})

    # Resolve the votes once, so normalize.py reads a decision rather than
    # re-implementing the tie-break. Majority, not "any positive vote": broad
    # shelves like /all-bags sweep in accessories too, so a zipper puller can
    # pick up a single bag vote against seven accessory ones and win.
    for entry in out["products"].values():
        entry["is_bag"] = entry["bag_votes"] > entry["accessory_votes"]

    out["status"] = "ok"
    return out


def parse_shard(spec):
    """'2/4' -> (2, 4). Returns (0, 1) — the whole list — when unset."""
    if not spec:
        return 0, 1
    try:
        index, count = (int(p) for p in spec.split("/", 1))
    except ValueError:
        raise SystemExit(f"--shard wants INDEX/COUNT, got {spec!r}")
    if count < 1 or not 0 <= index < count:
        raise SystemExit(f"--shard {spec} is out of range")
    return index, count


def merge_logs():
    """Fold data/fetch-log.part-*.json back into the single committed log."""
    log = {}
    if os.path.exists(LOG):
        try:
            log = json.load(open(LOG))
        except json.JSONDecodeError:
            log = {}
    parts = sorted(glob.glob(LOG_PART.format("*")))
    for path in parts:
        try:
            log.update(json.load(open(path)))
        except (json.JSONDecodeError, OSError):
            sys.stderr.write(f"  skipping unreadable {path}\n")
            continue
        os.remove(path)
    with open(LOG, "w") as f:
        json.dump(log, f, indent=2)
    ok = sum(1 for v in log.values() if v.get("status") == "ok")
    print(f"merged {len(parts)} shard logs -> {ok}/{len(log)} brands with "
          f"catalogs")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delay", type=float, default=2.0,
                    help="seconds between requests (use ~65 on a cloud IP)")
    ap.add_argument("--only", default="", help="comma-separated brand slugs")
    ap.add_argument("--force", action="store_true", help="refetch cached brands")
    ap.add_argument("--max-age", type=float, default=0.0,
                    help="refetch catalogues older than N hours "
                         "(0 = keep whatever is cached)")
    ap.add_argument("--shard", default="",
                    help="INDEX/COUNT — take every COUNTth brand, offset INDEX")
    ap.add_argument("--merge-logs", action="store_true",
                    help="fold shard logs into fetch-log.json and exit")
    ap.add_argument("--collections", action="store_true",
                    help="fetch collection membership (category ground truth)")
    args = ap.parse_args()

    os.makedirs(RAW, exist_ok=True)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)

    if args.merge_logs:
        return merge_logs()

    with open(os.path.join(HERE, "brands.json")) as f:
        brands = json.load(f)
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        brands = [b for b in brands if b["slug"] in wanted]

    if args.collections:
        os.makedirs(COLLECTIONS, exist_ok=True)
        pacer = Pacer(args.delay)
        for i, brand in enumerate(brands, 1):
            out = os.path.join(COLLECTIONS, f"{brand['slug']}.json")
            if os.path.exists(out) and not args.force:
                print(f"[{i}/{len(brands)}] {brand['name']}: cached, skipping")
                continue
            # Skip brands whose catalogue we could not fetch — no products to
            # classify means no point spending requests on their shelving.
            if not os.path.exists(os.path.join(RAW, f"{brand['slug']}.json")):
                print(f"[{i}/{len(brands)}] {brand['name']}: no catalogue, skipping")
                continue
            print(f"[{i}/{len(brands)}] {brand['name']} collections ...",
                  flush=True)
            res = fetch_collections(brand, pacer)
            with open(out, "w") as f:
                json.dump(res, f, indent=1)
            print(f"    -> {res['status']}, {len(res['products'])} products "
                  f"mapped", flush=True)
        return

    index, count = parse_shard(args.shard)
    # Stride rather than contiguous blocks: brands are roughly ordered by how
    # well known they are, so contiguous chunks would hand one runner all the
    # big catalogues.
    if count > 1:
        brands = brands[index::count]
        print(f"shard {index}/{count}: {len(brands)} brands", flush=True)

    log_path = LOG_PART.format(index) if count > 1 else LOG

    pacer = Pacer(args.delay)
    log = {}
    if count == 1 and os.path.exists(LOG):
        try:
            log = json.load(open(LOG))
        except json.JSONDecodeError:
            log = {}

    for i, brand in enumerate(brands, 1):
        out = os.path.join(RAW, f"{brand['slug']}.json")
        if os.path.exists(out) and not args.force:
            age_h = (time.time() - os.path.getmtime(out)) / 3600.0
            if not args.max_age or age_h < args.max_age:
                print(f"[{i}/{len(brands)}] {brand['name']}: cached "
                      f"({age_h:.1f}h old), skipping")
                continue

        print(f"[{i}/{len(brands)}] {brand['name']} ({brand['domain']}) ...",
              flush=True)
        res = fetch_brand(brand, pacer)
        n = len(res["products"])
        if res["status"] == "ok" and n:
            with open(out, "w") as f:
                json.dump(res, f)
        else:
            # A failed refetch keeps yesterday's catalogue on disk rather than
            # dropping the brand out of the index for a night.
            if os.path.exists(out):
                res["note"] = (res["note"] + "; kept cached catalogue").strip("; ")
        print(f"    -> {res['status']} {n} products {res['note']}", flush=True)

        log[brand["slug"]] = {k: v for k, v in res.items() if k != "products"}
        log[brand["slug"]]["product_count"] = n
        with open(log_path, "w") as f:
            json.dump(log, f, indent=2)

    ok = sum(1 for v in log.values() if v.get("status") == "ok")
    print(f"\ndone: {ok}/{len(log)} brands with catalogs")


if __name__ == "__main__":
    main()
