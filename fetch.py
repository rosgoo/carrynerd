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
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

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
# No placeholder URL when the contact is unset: a UA carrying example.com is a
# stock bot signature (Bonfus's WAF 403s exactly that and nothing else), and a
# fake contact address is worse than none. Set GEARHERD_CONTACT to the real
# /bot page before running at scale.
CONTACT = os.environ.get("GEARHERD_CONTACT", "")
UA = (f"gearherd/0.1 (product catalog indexer; +{CONTACT})" if CONTACT
      else "gearherd/0.1 (product catalog indexer)")

PAGE_LIMIT = 250
MAX_PAGES = 12
MAX_RETRIES = 4

# Consecutive rate-limited brands before a run gives up on the feed endpoints
# entirely. Three, because the block is per-store often enough that one or two
# proves nothing — Aer served us right through a block that had every other
# store returning 429 — while three in a row is about this client.
RATE_LIMIT_STREAK = 3


class _Redirect308(urllib.request.HTTPRedirectHandler):
    """
    Python 3.9's urllib follows 301/302/303/307 but not 308, so a store that
    normalises its URLs with a permanent 308 reads as a dead end rather than a
    redirect — Klattermusen sends /en-eu -> /en-eu/ that way and looked like a
    broken site for it. Same policy the handler already applies to 301; only
    the status code is new.
    """

    def http_error_308(self, req, fp, code, msg, headers):
        return self.http_error_301(req, fp, 301, msg, headers)


_OPENER = urllib.request.build_opener(_Redirect308)


def get(url, timeout=30, accept="application/json, text/plain, */*"):
    """Single GET. Returns (status, headers, body_bytes)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), (e.read() or b"")
    except Exception as e:
        return 0, {"x-error": str(e)}, b""


def post_json(url, payload, timeout=45):
    """Single POST of a JSON body. Returns (status, headers, body_bytes)."""
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
        "User-Agent": UA,
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), (e.read() or b"")
    except Exception as e:
        return 0, {"x-error": str(e)}, b""


# One parse per domain per run. robots.txt was being refetched for every
# adapter that consults it, and the page crawlers below want the Crawl-delay
# out of the same file rather than paying for a second copy of it.
_ROBOTS = {}


def robots(domain):
    """
    Minimal robots.txt parse, cached. A group addressed to our own UA outranks
    the `User-agent: *` group — CampSaver blocks named product scrapers (Indix,
    TheFind) while allowing *, so a site that ever adds a group for our UA is
    speaking to us specifically and gets obeyed over the general rule.
    Returns (disallows, crawl_delay_seconds, note).
    """
    if domain in _ROBOTS:
        return _ROBOTS[domain]
    ua_name = UA.split("/", 1)[0].lower()
    status, _, body = get(f"https://{domain}/robots.txt", timeout=20)
    if status != 200:
        _ROBOTS[domain] = ([], 0.0, f"robots.txt status {status}; proceeding")
        return _ROBOTS[domain]
    group = None
    rules = {"*": [], ua_name: []}
    delays = {"*": 0.0, ua_name: 0.0}
    for raw in body.decode("utf-8", "replace").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, value = (p.strip() for p in line.split(":", 1))
        field = field.lower()
        if field == "user-agent":
            v = value.lower()
            group = v if v in rules else (ua_name if ua_name in v else None)
        elif field == "disallow" and group and value:
            rules[group].append(value)
        elif field == "crawl-delay" and group:
            try:
                delays[group] = float(value)
            except ValueError:
                pass
    key = ua_name if (rules[ua_name] or delays[ua_name]) else "*"
    _ROBOTS[domain] = (rules[key], delays[key], "allowed")
    return _ROBOTS[domain]


def robots_allows(domain, path="/products.json"):
    """Returns (allowed: bool, note: str)."""
    disallows, _, note = robots(domain)
    for rule in disallows:
        if path.startswith(rule):
            return False, f"robots.txt disallows {rule}"
    return True, note


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


# --- non-Shopify platforms ----------------------------------------------------
#
# Same rules as the Shopify path: only endpoints served to anyone, no login,
# no challenge circumvention, honest UA. Each fetcher writes the source's own
# shape into raw/ untouched — normalize.py owns the mapping, so a parser fix
# never costs a refetch.

def fetch_bellroy(brand, pacer):
    """
    Bellroy's storefront is not Shopify, but its product data comes from the
    same public unauthenticated API its own frontend calls on every page view:
    /api/v1/customer/context hands out the currency and price-list identifiers,
    then {products host}/v2/products returns the whole catalog in one response
    — richer than Shopify plus enrichment (internal AND external dims, net
    weight, material composition, GTINs). Two requests per run, politer than
    paginating a products.json. The identifiers can rotate, so they are fetched
    fresh every time and never cached. Mapping notes: Notes/gearherd/bellroy-api.md.
    """
    domain = brand["domain"]
    result = {
        "slug": brand["slug"], "name": brand["name"], "domain": domain,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": "bellroy", "status": None, "note": "", "products": [],
    }

    pacer.wait()
    allowed, note = robots_allows(domain, "/api/v1/customer/context")
    if not allowed:
        result["status"], result["note"] = "skipped", note
        return result

    pacer.wait()
    status, _, body = get(f"https://{domain}/api/v1/customer/context")
    if status != 200:
        result["status"], result["note"] = f"http_{status}", "customer/context"
        return result
    try:
        commerce = json.loads(body)["commerce_information"]
        currency = commerce["currency_identifier"]
        price_list = commerce["price_list_identifier"]
    except (json.JSONDecodeError, KeyError) as e:
        result["status"], result["note"] = "bad_json", f"context: {e}"
        return result

    # The host is Bellroy's own product service, named in the page config the
    # frontend boots from (applications["product-detail"].sources.products).
    query = urllib.parse.urlencode({
        "currency_identifier": currency,
        "channel": domain,
        "price_role_identifier": price_list,
    })
    pacer.wait()
    status, _, body = get(
        f"https://production.products.boobook-services.com/v2/products?{query}",
        timeout=60)
    if status != 200:
        result["status"], result["note"] = f"http_{status}", "v2/products"
        return result
    try:
        result["products"] = json.loads(body).get("products", [])
    except json.JSONDecodeError:
        result["status"] = "bad_json"
        return result

    result["status"] = "ok"
    return result


def fetch_woocommerce(brand, pacer):
    """
    WooCommerce's Store API — /wp-json/wc/store/v1/products — is public and
    unauthenticated by design (it is what the shop's own frontend calls).
    Cottage pack makers skew WordPress, so this one adapter covers ULA, LiteAF,
    Bonfus and whatever the domain-resolution pass turns up next. No specs in
    the payload; the description HTML carries them, and normalize.py parses it
    with the same helpers the Shopify path uses.
    """
    domain = brand["domain"]
    result = {
        "slug": brand["slug"], "name": brand["name"], "domain": domain,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": "woocommerce", "status": None, "note": "", "products": [],
    }

    pacer.wait()
    allowed, note = robots_allows(domain, "/wp-json/")
    if not allowed:
        result["status"], result["note"] = "skipped", note
        return result

    products, page = [], 1
    while page <= MAX_PAGES:
        pacer.wait()
        status, headers, body = get(
            f"https://{domain}/wp-json/wc/store/v1/products"
            f"?per_page=100&page={page}")
        if status == 429:
            wait_s = float(headers.get("Retry-After", 30) or 30)
            time.sleep(wait_s + 2)
            continue
        if status != 200:
            result["status"] = f"http_{status}"
            return result
        try:
            batch = json.loads(body)
        except json.JSONDecodeError:
            result["status"] = "bad_json"
            return result
        if not isinstance(batch, list):
            result["status"], result["note"] = "bad_json", "not a list"
            return result
        products.extend(batch)
        if len(batch) < 100:
            break
        page += 1

    result["status"] = "ok"
    result["products"] = products
    return result


# --- Magento 2 storefront GraphQL -------------------------------------------
#
# Magento ships /graphql as the storefront's own read API: public, no token,
# no account — it is what the shop's frontend queries on every page view. The
# REST catalogue next door is the opposite (/rest/V1/products 401s without an
# admin token) and stays untouched. Verified open on rab.equipment and
# vaude.com 2026-08-01; both run Hyvä themes over stock Magento.
#
# It is the richest non-Shopify source found so far, because Magento stores
# keep their spec sheet in custom attributes and hand the whole set over:
# Rab publishes `dimensions: "54 x 32 x 26cm"`, `volume: "28lt / 1710cu.in"`
# and `r_weight: "1.09kg / 2lb 6oz"`; Vaude publishes height/width/length and
# volume as plain numbers. normalize.py's build_magento owns the mapping.

MAGENTO_PRODUCT_FIELDS = """
      name sku url_key url_suffix stock_status
      ... on PhysicalProductInterface { weight }
      description { html }
      short_description { html }
      image { url }
      media_gallery { url }
      price_range {
        minimum_price { final_price { value currency } regular_price { value } }
        maximum_price { final_price { value } }
      }
      categories { name url_path }
      custom_attributesV2 {
        items {
          code
          ... on AttributeValue { value }
          ... on AttributeSelectedOptions { selected_options { label } }
        }
      }
      ... on ConfigurableProduct {
        configurable_options { label attribute_code values { label } }
        variants {
          attributes { code label }
          product {
            sku name stock_status
            ... on PhysicalProductInterface { weight }
            price_range {
              minimum_price { final_price { value currency } regular_price { value } }
            }
            image { url }
          }
        }
      }
"""

# `categoryList` with no argument, not `categoryList(filters: {})`: the empty
# filter is legal on Rab's Magento and a 500 on Vaude's, while the bare call
# returns the store root — and therefore the whole tree — on both.
MAGENTO_CATEGORY_QUERY = """
{ categoryList { %s } }
""" % ("uid name url_path product_count children { %s }" % (
    "uid name url_path product_count children { %s }" % (
        "uid name url_path product_count children "
        "{ uid name url_path product_count }")))

MAGENTO_PRODUCT_QUERY = """
query ($uid: String!, $page: Int!, $size: Int!) {
  products(filter: {category_uid: {eq: $uid}}, pageSize: $size, currentPage: $page) {
    total_count
    page_info { current_page total_pages }
    items { __typename %s }
  }
}
""" % MAGENTO_PRODUCT_FIELDS

# Which shelves are worth querying. A brand that also sells jackets should
# cost a handful of requests, not its whole catalogue — and the negative list
# is about spending requests, not about classification: normalize.py still has
# the final say on everything that comes back.
#
# Bilingual because Magento is where the European brands are: Vaude's tree is
# entirely German, Rab's entirely English, and one adapter covers both.
MAGENTO_CARRY_CATEGORY = re.compile(
    r"rucks(?:a|ä|ae)ck|backpack|daypack|\bpacks?\b|\bbags?\b|tasche|"
    r"luggage|gep(?:ä|ae)ck|koffer|duffel|duffle|holdall|tote|sling|"
    r"pouch|beutel|wallet|geldb(?:ö|oe)rse", re.I)
MAGENTO_NOT_CARRY = re.compile(
    r"sleeping|schlafs(?:a|ä|ae)ck|tent|zelt|jacket|jacke|apparel|bekleidung|"
    r"shoes?\b|schuh|shirt|glove|handschuh|repair|spare|ersatzteil|"
    r"sale\b|outlet|archive|gift|geschenk", re.I)


def magento_gql(domain, query, variables=None, pacer=None):
    """One GraphQL POST. Returns (data_or_None, note)."""
    if pacer:
        pacer.wait()
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    status, _, body = post_json(f"https://{domain}/graphql", payload)
    if status != 200:
        return None, f"http_{status}"
    try:
        res = json.loads(body)
    except json.JSONDecodeError:
        return None, "bad_json"
    if res.get("errors"):
        return None, str(res["errors"][0].get("message"))[:120]
    return res.get("data"), ""


def _magento_flatten(nodes, depth=0):
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        yield node
        if depth < 4:
            for child in _magento_flatten(node.get("children"), depth + 1):
                yield child


def fetch_magento(brand, pacer):
    domain = brand["domain"]
    result = {
        "slug": brand["slug"], "name": brand["name"], "domain": domain,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": "magento", "status": None, "note": "", "products": [],
    }

    pacer.wait()
    allowed, note = robots_allows(domain, "/graphql")
    if not allowed:
        result["status"], result["note"] = "skipped", note
        return result

    tree, note = magento_gql(domain, MAGENTO_CATEGORY_QUERY, pacer=pacer)
    if tree is None:
        result["status"] = note if note.startswith("http_") else "bad_json"
        result["note"] = f"categoryList: {note}"
        return result

    wanted = []
    for node in _magento_flatten(tree.get("categoryList")):
        name = node.get("name") or ""
        if not node.get("uid") or not (node.get("product_count") or 0):
            continue
        if MAGENTO_NOT_CARRY.search(name) or not MAGENTO_CARRY_CATEGORY.search(name):
            continue
        wanted.append(node)
    # Biggest shelf first. A store's "All packs" category usually subsumes the
    # narrower ones, and products are deduplicated by SKU as they arrive, so
    # starting wide means the later categories mostly confirm what is already
    # in hand rather than adding to it.
    wanted.sort(key=lambda n: -(n.get("product_count") or 0))
    print(f"    {len(wanted)} carry categories "
          f"({', '.join(n['name'] for n in wanted[:6])}"
          f"{' ...' if len(wanted) > 6 else ''})", flush=True)

    by_sku = {}
    for node in wanted:
        page = 1
        while page <= MAX_PAGES:
            data, note = magento_gql(
                domain, MAGENTO_PRODUCT_QUERY,
                {"uid": node["uid"], "page": page, "size": 50}, pacer)
            if data is None:
                result["note"] = (result["note"] + f"; {node['name']}: {note}").strip("; ")
                break
            block = data.get("products") or {}
            items = block.get("items") or []
            for item in items:
                if item.get("sku"):
                    by_sku.setdefault(item["sku"], item)
            info = block.get("page_info") or {}
            if page >= (info.get("total_pages") or 1) or not items:
                break
            page += 1

    result["products"] = list(by_sku.values())
    result["status"] = "ok" if by_sku else "empty"
    return result


# --- storefront pages -------------------------------------------------------
#
# The adapter of last resort, and the one most storefronts answer to: no API,
# just the pages a store already publishes for search engines, reached through
# the sitemap it advertises in robots.txt. It is what covers Salesforce
# Commerce Cloud (Gregory, CamelBak) and the server-rendered schema.org sites
# (Deuter, Mammut) — four brands on four unrelated platforms, one crawler,
# because every difference between them is in what the page *says*, and
# normalize.py's build_pages owns that.

SPEC_LABEL_CLASS = re.compile(r"spec[\w-]*[-_](?:name|label)|attribute[-_]label",
                              re.I)
SPEC_VALUE_CLASS = re.compile(r"spec[\w-]*[-_]value|attribute[-_]value", re.I)


class PageDigest(HTMLParser):
    """
    Reduces a product page to the record normalize.py needs: schema.org
    blocks, head metadata, the h1, tables and label/value pairs as arrays,
    elements carrying a `content` attribute (how SFCC publishes prices), and
    the visible text in document order.

    Not the raw HTML. A brand catalogue is a few hundred pages of half a
    megabyte each and none of the markup survives a theme change, so keeping
    it would cost gigabytes to protect nothing. Everything in a page that can
    carry a fact is kept instead, which is what the no-refetch-to-fix-a-parser
    rule actually asks for.
    """

    SKIP = {"script", "style", "noscript", "svg", "template"}
    CAPTURE = {"h1", "td", "th", "dt", "dd"}
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.title = None
        self.meta = {}
        self.jsonld = []
        self.h1 = None
        self.pairs = []
        self.tables = []
        self.values = []
        self.images = []
        self._text = []
        self._skip = 0
        self._ld = None
        self._in_title = False
        self._stack = []
        self._elems = []
        self._table = None
        self._row = None
        self._label = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in self.SKIP:
            if tag == "script" and (a.get("type") or "").lower() == "application/ld+json":
                self._ld = []
            else:
                self._skip += 1
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and a.get("content"):
                self.meta.setdefault(key, a["content"])
            return
        if tag == "img":
            src = a.get("src") or a.get("data-src") or ""
            # Forty rather than a handful: a storefront's first dozen <img>
            # are its nav thumbnails, and CamelBak's product shot does not
            # appear until well past them. Choosing between them is
            # normalize.py's job; getting them all here is this one's.
            if src and not src.startswith("data:") and len(self.images) < 40:
                self.images.append(src)
            return
        cls = a.get("class") or ""
        if a.get("content") and len(self.values) < 80:
            # With the nearest classed ancestors, because the element itself
            # rarely says what the number means: SFCC prints both the sale and
            # the list price as <span class="value" content="...">, and only
            # the wrapper (`sales` vs `strike-through`) tells them apart.
            near = " ".join(c for _, c in self._elems[-3:] if c)
            self.values.append([f"{cls} {near}".strip(), a["content"]])
        if tag not in self.VOID:
            self._elems.append((tag, cls))
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        if SPEC_LABEL_CLASS.search(cls):
            kind = "label"
        elif SPEC_VALUE_CLASS.search(cls):
            kind = "value"
        elif tag in self.CAPTURE:
            kind = tag
        else:
            kind = None
        if kind:
            self._stack.append([tag, kind, []])

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            if tag == "script" and self._ld is not None:
                raw, self._ld = "".join(self._ld), None
                try:
                    data = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    return
                for item in (data if isinstance(data, list) else [data]):
                    if isinstance(item, dict):
                        self.jsonld.append(item)
            elif self._skip:
                self._skip -= 1
            return
        if tag == "title":
            self._in_title = False
            return
        for i in range(len(self._elems) - 1, -1, -1):
            if self._elems[i][0] == tag:
                del self._elems[i:]
                break
        # Unwind to the matching open element. Themes leave tags unclosed all
        # the time; closing everything above the match keeps one stray <td>
        # from swallowing the rest of the page into a single cell.
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                for entry in self._stack[i:]:
                    self._close(entry)
                del self._stack[i:]
                break
        if tag == "tr" and self._row is not None:
            if self._table is not None:
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table and len(self.tables) < 12:
                self.tables.append(self._table)
            self._table = None

    def _close(self, entry):
        tag, kind, buf = entry
        text = " ".join("".join(buf).split())
        if kind in ("td", "th"):
            if self._row is not None:
                self._row.append(text)
            return
        if not text:
            return
        if kind in ("label", "dt"):
            self._label = text
        elif kind in ("value", "dd"):
            if self._label and len(self.pairs) < 100:
                self.pairs.append([self._label, text])
            self._label = None
        elif kind == "h1" and self.h1 is None:
            self.h1 = text

    def handle_data(self, data):
        if self._ld is not None:
            self._ld.append(data)
            return
        if self._skip:
            return
        if self._in_title and self.title is None:
            self.title = " ".join(data.split()) or None
            return
        if self._stack:
            self._stack[-1][2].append(data)
        if len(self._text) < 6000:
            chunk = " ".join(data.split())
            if chunk:
                self._text.append(chunk)

    def digest(self, url):
        return {
            "url": url, "title": self.title, "meta": self.meta,
            "h1": self.h1, "jsonld": self.jsonld, "pairs": self.pairs,
            "tables": self.tables, "values": self.values,
            "images": self.images, "text": " ".join(self._text)[:40000],
        }


# A challenge page is not a product page that happened to fail. Gregory sits
# behind PerimeterX and answers a crawl with a 307 to a px-captcha page that
# carries no Location header — invisible to a status check, and indistinguishable
# from a parse failure unless you look for it. Recognising it is what turns
# twelve more polite knocks into none: the answer is already no.
CHALLENGE = re.compile(
    rb"px-captcha|_pxVid|Access to this page has been denied|"
    rb"Checking your browser|cf-browser-verification|Just a moment\.\.\.|"
    rb"/cdn-cgi/challenge-platform|Attention Required!", re.I)


def _xml_locs(body):
    if body[:2] == b"\x1f\x8b":
        try:
            body = gzip.decompress(body)
        except OSError:
            return []
    return re.findall(r"<loc>\s*([^<\s]+)\s*</loc>",
                      body.decode("utf-8", "replace"))


def sitemap_urls(start, pacer, patterns, extra=0.0, max_shards=40):
    """
    Walk a sitemap — index or urlset, plain or gzipped — sorting the URLs it
    reaches into {name: [url]} by the {name: regex} in `patterns`. A sitemap is
    the one discovery surface a store publishes *for* crawlers, which is why
    every page adapter here starts at one instead of following links out of its
    catalogue pages.

    One walk, several patterns, because a store's sitemap is 35 files and the
    product pages and the category pages are interleaved through all of them.
    """
    # Dicts, not lists: a store can list the same URL in several of its
    # sitemaps — Mammut's /us/en and /int/en shards each hold part of the US
    # catalogue and overlap in the middle — and a duplicate here is a second
    # request for a page already in hand.
    out = {name: {} for name in patterns}
    seen, queue, shards = set(), [start], 0
    while queue and shards < max_shards:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        pacer.wait(extra)
        status, _, body = get(url, timeout=60,
                              accept="application/xml, text/xml, */*")
        if status != 200:
            continue
        shards += 1
        for loc in _xml_locs(body):
            if re.search(r"\.xml(?:\.gz)?(?:$|[?#])", loc, re.I):
                queue.append(loc)
                continue
            for name, rx in patterns.items():
                if rx.search(loc):
                    out[name][loc] = True
                    break
    return {name: list(urls) for name, urls in out.items()}


def shelf_hints(shelf_urls, pacer, keep, extra=0.0):
    """
    Product slug -> category, read off the store's own shelving.

    The non-Shopify half of what fetch_collections does, and for the same
    reason. Mammut ships a "Trion 38" and a "Lithium 25": the title names no
    category, the schema.org block carries none, and there is no breadcrumb —
    every signal a classifier could use is absent, and 267 crawled pages
    yielded seven bags. The one place the brand does say what these are is the
    shelf it hangs them on, and that is a statement rather than a guess.

    Keyed on the product's slug, not its URL: a shelf links to a specific
    colourway (`/products/2530-00301-1334/lithium-15`) while the sitemap lists
    the model (`/products/2530-00301/lithium-15`), and only the tail agrees.
    """
    shelves = []
    for url in shelf_urls:
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        name = slug.replace("-", " ")
        if NOISE.search(slug) or SPARE_PART.search(name) or NOT_CARRY_SHELF.search(name):
            continue
        category = collection_category(name)
        if not category and not GENERIC_BAG.search(name):
            continue
        shelves.append((url, slug, name, category))

    # Specific shelves first, so `hiking-backpacks` is read before the
    # `backpacks-and-bags` catch-all it sits under and the narrower ruling is
    # the one that sticks.
    shelves.sort(key=lambda s: (s[3] is None, s[3] == "daypack"))

    hints, urls = {}, {}
    for url, slug, name, category in shelves:
        pacer.wait(extra)
        status, _, body = get(url, timeout=60, accept="text/html,*/*")
        if status != 200:
            continue
        text = body.decode("utf-8", "replace")
        base = f"https://{urllib.parse.urlparse(url).netloc}"

        # Two ways to read a shelf, and the second is what makes stores with a
        # category-only sitemap reachable at all.
        #
        # `keep` is the URL shape from the brand's crawl config, which works
        # wherever product URLs are distinguishable by path — /en/product/x/.
        # Mystery Ranch hangs its products off the root (/blitz-30-pack), where
        # no pattern separates a product from /affiliates or /contact.
        #
        # But the shelf marks them itself: a product card is wrapped in
        # schema.org/Product microdata, so the first link inside each card is
        # the product. That is the store stating which of its links are
        # products rather than us guessing from their shape, which is the same
        # reason shelves beat keyword classification in the first place.
        marked = [m.group(1) for m in re.finditer(
            r'itemtype="[^"]*schema\.org/Product"[^>]*>.{0,3000}?'
            r'href="([^"#?]+)"', text, re.S)]
        loose = [h for h in set(re.findall(r'href="([^"#?]+)"', text))
                 if keep.search(h)]
        hrefs = marked or loose

        found = 0
        for href in dict.fromkeys(hrefs):
            product = href.rstrip("/").rsplit("/", 1)[-1]
            # First shelf wins, and shelves are visited in sitemap order, so a
            # narrow one ("hiking-backpacks") beats the catch-all it sits
            # under only if it comes first. Categories still lose to the
            # product's own title in normalize.py either way.
            if product and product not in hints:
                hints[product] = category
            if product:
                urls.setdefault(product,
                                href if href.startswith("http")
                                else base + href)
            found += 1
        print(f"    shelf {slug}: {found} products -> {category or 'bag'}"
              f"{' (microdata)' if marked else ''}", flush=True)
    return hints, urls


def fetch_pages(brand, pacer, limit=0, force=False):
    """
    Crawl a storefront's sitemap and keep a digest of each product page.

    Per-brand configuration lives in brands.json under `crawl`:
        sitemap  where to start (default: whatever robots.txt advertises)
        product  regex a URL must match to be worth a request
        delay    seconds between requests, when robots.txt names no Crawl-delay

    Incremental like the CampSaver crawler and with the same circuit breaker:
    a long run of pages that parse to nothing is what a soft block looks like
    from here, and every request served while blocked argues for keeping the
    block.

    Chunked crawls take `--limit N --max-age H`, never `--force`: --force is
    what tells this to drop the pages it already has and start over, so a run
    carrying both fetches the same first N pages every night and never reaches
    the rest of the catalogue.
    """
    domain = brand["domain"]
    cfg = brand.get("crawl") or {}
    result = {
        "slug": brand["slug"], "name": brand["name"], "domain": domain,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": "pages", "status": None, "note": "", "products": [],
    }

    pacer.wait()
    _, crawl_delay, note = robots(domain)
    # A store that asks for ten seconds between requests gets ten seconds.
    extra = max(0.0, (crawl_delay or cfg.get("delay") or 0.0) - pacer.delay)

    start = cfg.get("sitemap")
    if not start:
        pacer.wait(extra)
        status, _, body = get(f"https://{domain}/robots.txt", timeout=20)
        found = re.findall(r"(?im)^sitemap:\s*(\S+)", body.decode("utf-8", "replace"))
        if not found:
            result["status"], result["note"] = "no-sitemap", note
            return result
        start = found[0]

    keep = re.compile(cfg.get("product") or r"\.html$", re.I)
    patterns = {"product": keep}
    if cfg.get("shelf"):
        patterns["shelf"] = re.compile(cfg["shelf"], re.I)
    found = sitemap_urls(start, pacer, patterns, extra)
    candidates = found["product"]

    # The store's own shelving, where it publishes one. Gathered before the
    # product crawl so every page fetched can be filed as it arrives — and now
    # before the no-products bail-out, because for some stores the shelves are
    # the only place products appear at all.
    hints, shelf_urls = shelf_hints(found.get("shelf") or [], pacer, keep, extra)
    result["shelves"] = hints

    # Shelves as the discovery surface, not just as labels.
    #
    # A sitemap is the surface a store publishes *for* crawlers, so it is the
    # right place to start — but plenty of stores list only their categories in
    # it. Mystery Ranch publishes 580 URLs and not one is a product; Klättermusen
    # does the same. Both were unreachable, and both were one already-crawled
    # page away from being reachable: the shelves we fetch for classification
    # are the product index.
    #
    # Only as a fallback. Where a sitemap does list products it is complete and
    # authoritative, while a shelf shows what a category chose to display — the
    # first page of it, possibly paginated, possibly filtered.
    if not candidates and shelf_urls:
        candidates = sorted(shelf_urls.values())
        result["discovery"] = "shelf"
        print(f"    sitemap listed no products; taking {len(candidates)} "
              f"from the shelves instead", flush=True)

    if not candidates:
        result["status"] = "no-products"
        result["note"] = (f"nothing in {start} matched {keep.pattern!r}, "
                          f"and no shelf yielded products")
        return result

    # Shelf scoping: crawl what the brand shelves as carry rather than its
    # whole sitemap. shelf_hints has already dropped the non-carry shelves, so
    # its keys are a ready-made frontier — the change here is spending them
    # instead of only labelling with them.
    #
    # This is the difference between a page adapter that finishes and one that
    # does not. Deuter lists ~1,054 product URLs and yields 187 models; Mammut
    # yielded seven bags from 267 crawled pages. The remainder is gaiters,
    # flasks and spare buckles, fetched at 65s each to be rejected later.
    #
    # Opt-in per brand, and it fails open. A brand reorganising its navigation
    # would otherwise halve the crawl silently — unlike a rejected product, a
    # page never fetched leaves no trace in rejected.json — so a scope that
    # reaches implausibly little of the sitemap is read as a broken pattern
    # rather than a small catalogue, and the full list is used instead. The
    # count is printed either way, so the log says what scoping cost.
    if cfg.get("shelf_scoped"):
        scoped = [u for u in candidates
                  if u.rstrip("/").rsplit("/", 1)[-1] in hints] if hints else []
        share = len(scoped) / len(candidates)
        if share < 0.05:
            print(f"    shelf scope reached {len(scoped)}/{len(candidates)} "
                  f"({share:.0%}) — too little to trust, crawling all",
                  flush=True)
            result["note"] = f"shelf-scope suspect: {len(scoped)}/{len(candidates)}"
        else:
            print(f"    shelf-scoped: {len(scoped)} of {len(candidates)} "
                  f"sitemap products sit on carry shelves", flush=True)
            result["scoped_from"] = len(candidates)
            candidates = scoped

    path = urllib.parse.urlparse(candidates[0]).path
    allowed, why = robots_allows(domain, path)
    if not allowed:
        result["status"], result["note"] = "skipped", why
        return result

    seen = {}
    prior = os.path.join(RAW, f"{brand['slug']}.json")
    if not force and os.path.exists(prior):
        try:
            for page in json.load(open(prior)).get("products", []):
                seen[page["url"]] = page
        except (json.JSONDecodeError, KeyError):
            seen = {}

    fresh = [u for u in candidates if u not in seen]
    if limit:
        fresh = fresh[:limit]
    print(f"    {len(candidates)} candidate URLs, {len(seen)} cached, "
          f"fetching {len(fresh)} at {pacer.delay + extra:.0f}s", flush=True)

    # Two counters, because they mean opposite things and conflating them
    # stopped a healthy crawl. A page that answers 200 and contains no product
    # is the soft-block signature — CampSaver's forbidden.html does exactly
    # that — and twelve in a row is a decision, not chance. A timeout or a 502
    # is just a bad minute: Deuter served a run of them mid-crawl and the
    # single counter read it as a block, on pages that parse perfectly well.
    fetched, empty, errors, stopped = 0, 0, 0, ""
    for url in fresh:
        pacer.wait(extra)
        status, _, body = get(url, timeout=45, accept="text/html,*/*")
        if CHALLENGE.search(body[:4000]):
            print(f"    stopping: {url} answered a bot challenge", flush=True)
            stopped = "challenge"
            break
        digest = None
        if status == 200:
            parser = PageDigest()
            try:
                parser.feed(body.decode("utf-8", "replace"))
            except Exception:
                # A malformed page is one lost product, not a lost run.
                parser = None
            if parser and (parser.jsonld or parser.h1):
                digest = parser.digest(url)
        if digest:
            seen[url] = digest
            fetched += 1
            empty = errors = 0
            if fetched % 50 == 0:
                snap = dict(result, status="partial",
                            note=f"checkpoint at {fetched} pages",
                            products=list(seen.values()))
                with open(prior, "w") as f:
                    json.dump(snap, f)
        elif status == 200:
            empty += 1
            if empty >= 12:
                print(f"    aborting: {empty} consecutive pages answered 200 "
                      f"with no product — soft block likely", flush=True)
                stopped = "soft-block"
                break
        else:
            errors += 1
            if errors >= 8:
                print(f"    aborting: {errors} consecutive transport errors "
                      f"(last {status}) — the site or the link is unwell",
                      flush=True)
                stopped = "errors"
                break

    # File every page against the shelving, cached ones included: the
    # shelf pass is fifteen requests and re-reading it must never cost a
    # re-crawl of the catalogue behind it.
    for page_url, page in seen.items():
        slug = page_url.rstrip("/").rsplit("/", 1)[-1]
        if slug in hints:
            page["shelf"] = hints[slug]
            page["shelved"] = True

    result["products"] = list(seen.values())
    # A crawl something else ended is not a crawl that finished, and the
    # fetch log is the only place that difference is visible afterwards.
    # Named by *how* it ended, so a WAF and a bad network are told apart
    # in the log rather than in somebody's memory of the night.
    if stopped:
        result["status"] = f"stopped:{stopped}"
        # Keep what the run did get. main() only persists an "ok" result — the
        # right rule when a failed fetch would replace a good catalogue with an
        # empty one, but here `seen` is the previous catalogue *plus* this
        # run's pages, so the write is strictly additive and dropping it means
        # re-fetching up to fifty pages that were already politely fetched.
        if result["products"]:
            with open(prior, "w") as f:
                json.dump(dict(result, status="partial",
                               note=f"stopped:{stopped} at {fetched} pages"), f)
    else:
        result["status"] = "ok" if result["products"] else "empty"
    result["note"] = f"{fetched} pages fetched this run"
    return result


# CampSaver is an aggregator, not a brand: one adapter reaches every brand it
# stocks that has no feed of its own. Discovery goes through the orderable-
# product sitemaps (robots.txt allows them; the /api/ search endpoints its own
# grid uses are disallowed, so those are off-limits and unused). Product pages
# carry schema.org Product JSON-LD with per-variant offers (price, stock,
# GTIN) and spec text in the description.
#
# URL slugs decide what is worth a page fetch. The positive list errs open and
# the negative list trims the obvious non-carry noise (backpacking guidebooks,
# sleeping bags, stove carry-cases); normalize.py's classifier is the real
# gate, this filter only spends requests.
CAMPSAVER_SLUG_YES = re.compile(
    r"backpack|back-pack|daypack|day-pack|rucksack|duffel|duffle|sling|"
    r"crossbody|tote|messenger|briefcase|hip-pack|waist-pack|fanny|lumbar|"
    r"travel-pack|carry-on|luggage|suitcase|dopp|toiletry|pouch|organizer|"
    r"wallet|billfold|card-holder",
    re.I)
CAMPSAVER_SLUG_NO = re.compile(
    r"sleeping|bivy|guide|book|map|dvd|strap|buckle|repair|rain-?cover|"
    r"pack-cover|liner|towel|stove|oven|grill|griddle|tent|chair|table|"
    r"cooler(?!-bag)|holster|shaker|press|tether|harness|carrier|litter|"
    r"stuff-sack|compression|dry-bag|dry-sack|cube|insert|divider|"
    r"first-aid|hydration|reservoir|bottle|filter|shovel|trowel",
    re.I)


def fetch_campsaver(brand, pacer, limit=0, force=False):
    domain = brand["domain"]
    result = {
        "slug": brand["slug"], "name": brand["name"], "domain": domain,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": "campsaver", "status": None, "note": "", "products": [],
    }

    pacer.wait()
    allowed, note = robots_allows(domain, "/sitemap.product.orderable.index.xml")
    if not allowed:
        result["status"], result["note"] = "skipped", note
        return result

    # Incremental: a full candidate crawl is ~1-2k pages, so keep what earlier
    # runs already fetched and only visit new URLs. --force refetches all.
    seen = {}
    prior = os.path.join(RAW, f"{brand['slug']}.json")
    if not force and os.path.exists(prior):
        try:
            for p in json.load(open(prior)).get("products", []):
                seen[p["url"]] = p
        except (json.JSONDecodeError, KeyError):
            seen = {}

    def locs(xml_text):
        return re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml_text)

    pacer.wait()
    status, _, body = get(f"https://{domain}/sitemap.product.orderable.index.xml")
    if status != 200:
        result["status"], result["note"] = f"http_{status}", "sitemap index"
        return result
    shards = locs(body.decode("utf-8", "replace"))

    candidates = []
    for shard_url in shards:
        pacer.wait()
        status, _, body = get(shard_url, timeout=60)
        if status != 200:
            continue
        for url in locs(body.decode("utf-8", "replace")):
            slug = url.rsplit("/", 1)[-1]
            if CAMPSAVER_SLUG_YES.search(slug) and not CAMPSAVER_SLUG_NO.search(slug):
                candidates.append(url)
        # A test run should not pay for all 38 shards to fetch 8 pages.
        if limit and len([u for u in candidates if u not in seen]) >= limit:
            break

    fresh = [u for u in candidates if u not in seen]
    if limit:
        fresh = fresh[:limit]
    print(f"    {len(candidates)} candidate URLs, {len(seen)} cached, "
          f"fetching {len(fresh)}", flush=True)

    LD_RE = re.compile(
        r'<script type="application/ld\+json">(.*?)</script>', re.S)
    fetched, misses, blocked = 0, 0, False
    for url in fresh:
        pacer.wait()
        status, _, body = get(url, timeout=30)
        product, crumbs = None, []
        if status == 200:
            for block in LD_RE.findall(body.decode("utf-8", "replace")):
                try:
                    data = json.loads(block)
                except json.JSONDecodeError:
                    continue
                if data.get("@type") == "Product":
                    product = data
                elif data.get("@type") == "BreadcrumbList":
                    crumbs = [e.get("item", {}).get("name") or e.get("name") or ""
                              for e in data.get("itemListElement", [])]
        if product:
            seen[url] = {"url": url, "product": product, "breadcrumbs": crumbs}
            fetched += 1
            misses = 0
            # Checkpoint: an hour of polite requests should not be lost to a
            # dropped connection at page 900. Same shape main() writes, so an
            # interrupted run resumes from the last mark instead of zero.
            if fetched % 50 == 0:
                snap = dict(result, status="partial",
                            note=f"checkpoint at {fetched} pages",
                            products=list(seen.values()))
                with open(prior, "w") as f:
                    json.dump(snap, f)
        else:
            # CampSaver's bot protection soft-blocks: pages redirect to a
            # forbidden.html that answers 200 with no Product JSON-LD, so a
            # block looks like an endless run of product-less pages, not an
            # error code. Legit misses ran ~40% in testing, so twelve in a row
            # is a block, not chance (0.4^12) — stop knocking; every request
            # served while blocked argues for keeping the block. Learned the
            # hard way 2026-08-01: a sustained run tripped it a few hundred
            # pages in and ground on invisibly.
            misses += 1
            if misses >= 12:
                print(f"    aborting: {misses} consecutive pages without "
                      f"product data — soft block likely", flush=True)
                result["note"] = "aborted on soft block; "
                break

    result["products"] = list(seen.values())
    result["status"] = "ok"
    result["note"] = (result["note"] or "") + f"{fetched} pages fetched this run"
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
    # First, because bike luggage is named after everything else: a pannier
    # shelf is "roller bags", a bikepacking shelf is "seat packs", and the
    # luggage and daypack patterns below were taking both.
    ("bike-bag",        r"pannier|handlebar bags?|frame bags?|saddle bags?|"
                        r"rack[- ]top|bikepacking|\bbike bags?\b|fork bags?"),
    ("sling",           r"sling|crossbody"),
    ("hip-pack",        r"hip pack|fanny|waist pack|belt bag|bum bag"),
    ("duffel",          r"duffel|duffle|gym bag"),
    ("luggage",         r"luggage|suitcase|carry[- ]on|roller|spinner"),
    ("tote",            r"tote|shopper"),
    ("messenger",       r"messenger|courier|satchel"),
    ("briefcase",       r"briefcase|portfolio"),
    ("camera-bag",      r"camera bag|camera backpack|photo bag"),
    ("hiking-pack",     r"hiking|backpacking|trekking|mountaineering|alpine"),
    ("travel-backpack", r"travel pack|travel backpack"),
    ("daypack",         r"backpack|daypack|rucksack"),
    ("pouch",           r"pouch|dopp|toiletry|organi[sz]er"),
]

# Collections that mean "this is a bag" without saying which kind.
GENERIC_BAG = re.compile(r"\bbags?\b", re.I)

# Checked before all of them, including GENERIC_BAG. A shelf of spare parts
# for panniers is named after panniers and is not one: Ortlieb files 39
# products under `spare-parts-bike-bags` and 12 under
# `spare-parts-luggage-racks`, and the `\bbags?\b` vote swept every mounting
# hook and buckle into the index as carry. A luggage rack is the same story
# without the word "spare" — it is the thing a pannier hangs on.
SPARE_PART = re.compile(
    r"spare[- ]parts?|replacement|ersatzteil|luggage[- ]racks?|mounting", re.I)

# A brand's own category tree is its whole catalogue, not a bag shop's
# collection list, so the patterns above meet things they were never meant to
# judge: Mammut shelves `hiking-pants` and `hiking-shoes` beside
# `hiking-backpacks`, and `hiking` takes all three. What a shelf is *for* has
# to be settled before what it is about.
NOT_CARRY_SHELF = re.compile(
    r"pants?|trousers?|shorts?|shoes?|boots?|jackets?|shirts?|gloves?|hats?|"
    r"apparel|clothing|footwear|socks?|helmets?|harness|ropes?|cords?|"
    r"sleeping|tents?|poles?|axes?|crampons?|shovels?|probes?|beacons?|"
    r"accessor|spare|nutrition|care|sunglass", re.I)

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
        if SPARE_PART.search(name):
            wanted.append((handle, None, False))
        elif category:
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
    ap.add_argument("--limit", type=int, default=0,
                    help="cap page fetches for crawl-style platforms "
                         "(campsaver, pages) — for test runs and chunked crawls")
    ap.add_argument("--page-delay", type=float, default=0.0,
                    help="seconds between requests for the page adapters. "
                         "--delay is calibrated for /products.json, which "
                         "rate-limits per client IP; product pages are "
                         "ordinary HTML with no such limit, and inheriting the "
                         "feed's pacing costs hours (CamelBak: 124 pages, 2.2h "
                         "at 65s). Defaults to --delay so nothing changes "
                         "unless asked. robots Crawl-delay still wins.")
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
            # Collections are a Shopify endpoint; other platforms carry their
            # category signal in the feed itself.
            if brand.get("platform", "shopify") != "shopify":
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
    # A separate pacer for the page adapters, because they are pacing against a
    # different thing. --delay is set by Shopify's per-IP limit on
    # /products.json; a product page is ordinary HTML on a CDN and does not
    # share it. One brand's page crawl at feed pacing costs hours — the same
    # confusion that put enrich.py and the plate sampler on the wrong number.
    # robots Crawl-delay is still honoured on top, per brand, as before.
    page_pacer = Pacer(args.page_delay or args.delay)
    log = {}
    if count == 1 and os.path.exists(LOG):
        try:
            log = json.load(open(LOG))
        except json.JSONDecodeError:
            log = {}

    blocked_streak = 0
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
        platform = brand.get("platform", "shopify")
        if platform == "bellroy":
            res = fetch_bellroy(brand, pacer)
        elif platform == "campsaver":
            res = fetch_campsaver(brand, page_pacer, limit=args.limit,
                                  force=args.force)
        elif platform == "woocommerce":
            res = fetch_woocommerce(brand, pacer)
        elif platform == "magento":
            res = fetch_magento(brand, pacer)
        elif platform == "pages":
            res = fetch_pages(brand, page_pacer, limit=args.limit,
                              force=args.force)
        elif platform == "shopify":
            res = fetch_brand(brand, pacer)
        else:
            # A detected platform with no adapter yet (squarespace, custom).
            # Skip loudly rather than hammering it with Shopify requests.
            print(f"    no adapter for platform {platform!r}, skipping",
                  flush=True)
            log[brand["slug"]] = {"slug": brand["slug"], "name": brand["name"],
                                  "domain": brand["domain"],
                                  "platform": platform,
                                  "status": "no-adapter", "note": "",
                                  "product_count": 0}
            with open(log_path, "w") as f:
                json.dump(log, f, indent=2)
            continue
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

        # Run-level circuit breaker for the feed endpoints.
        #
        # A single brand already backs off properly — four attempts honouring
        # Retry-After, then `rate_limited` and move on. What was missing is the
        # decision above that: when store after store answers 429, the block is
        # on us rather than on them, and continuing means burning four minutes
        # of retries per brand while making the case for extending it. The
        # posture in the README is that every request served while blocked is
        # an argument against us, so the crawler should be the one to stop.
        #
        # Consecutive, and reset by any success, because the block has been
        # observed to be per-store: Aer served us throughout one block while
        # everything around it 429'd. An isolated blocked store is not a signal
        # and must not stop a healthy run. Three in a row is a decision.
        if res["status"] in ("rate_limited", "http_429"):
            blocked_streak += 1
            if blocked_streak >= RATE_LIMIT_STREAK:
                print(f"\n  stopping: {blocked_streak} brands in a row "
                      f"rate-limited. The block is on this client, not on any "
                      f"one store — leaving the rest for another day.",
                      flush=True)
                log["_stopped"] = {
                    "reason": "rate-limited",
                    "after": i,
                    "of": len(brands),
                }
                with open(log_path, "w") as f:
                    json.dump(log, f, indent=2)
                break
        else:
            blocked_streak = 0

    ok = sum(1 for v in log.values() if v.get("status") == "ok")
    print(f"\ndone: {ok}/{len(log)} brands with catalogs")


if __name__ == "__main__":
    main()
