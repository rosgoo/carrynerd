#!/usr/bin/env python3
"""
carrynerd affiliate programme detector — which brands can be joined, and how.

Monetisation planning has been running on an assumption: that Sovrn/Skimlinks
gives "instant coverage across 170 brands". It cannot. Those are sub-affiliate
networks — resellers that piggyback on Impact/CJ/Awin as one large publisher —
so their reach is a *subset* of brands already on a network, and it structurally
excludes every brand running its own programme from a Shopify app. Those
in-house programmes are the ones that approve at zero traffic and pay full
commission, which makes them the most valuable thing on the board right now and
invisible to the plan of record.

This turns that guess into a column. One homepage fetch per brand, plus at most
two follow-ups, produces a verdict per brand:

    in-house:<vendor>   runs its own programme — apply directly, today
    network:<network>   programme page hands off to a network's signup
    page-only           a programme exists, vendor not identified — human look
    none-found          no evidence either way
    unreachable         blocked, timed out, or no DNS

The distinction that matters is the last two. A brand not on a network leaves
almost no trace of that fact on its own storefront, so absence of evidence is
recorded as `none-found`, never as "no programme". Network membership is only
claimed when the brand's own page names the network. Same rule as the domain
resolver: probable is not proven, and a wrong answer is worse than a gap.

Results are ranked by catalogue depth from `data/bags.json`, because applying to
a brand carrying 400 indexed models is worth more than one carrying three.

Deliberately never touches /products.json — platform is read from homepage HTML
instead. Shopify's edge rate-limits per IP across all stores, and this scan is
not worth re-triggering that block.

Usage:
    python3 affiliate_scan.py                    # dry run over every brand
    python3 affiliate_scan.py --limit 10         # a taste
    python3 affiliate_scan.py --only "Aer,Tortuga"
    python3 affiliate_scan.py --apply            # write the JSON + CSV
    python3 affiliate_scan.py --unknown-markers  # third-party hosts we do not
                                                 # recognise, to grow the table
"""

import argparse
import collections
import csv
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from fetch import Pacer, UA, robots_allows

HERE = os.path.dirname(os.path.abspath(__file__))
BRANDS = os.path.join(HERE, "brands.json")
MASTER = os.path.join(HERE, "data", "brands-master.json")
BAGS = os.path.join(HERE, "data", "bags.json")
OUT_JSON = os.path.join(HERE, "data", "affiliate-programs.json")
# The application worklist, highest-value first. Filling the `applied` and
# `status` columns by hand is how this stays useful after the first run.
OUT_CSV = os.path.join(HERE, "data", "affiliate-todo.csv")

# ---------------------------------------------------------------------------
# Vendor markers.
#
# SEEDED, NOT AUTHORITATIVE. These are the hosts and identifiers the common
# affiliate apps and networks load from; they were written from knowledge of the
# vendors, not measured against this catalogue. Treat a miss as a gap in this
# table rather than proof of absence, and run --unknown-markers periodically to
# find vendors worth adding. Every verdict records which marker fired so a wrong
# entry here is visible rather than silent.
# ---------------------------------------------------------------------------

# In-house / storefront apps: the brand runs the programme itself. These are the
# apply-now tier — no traffic gate, full commission, no sub-affiliate haircut.
IN_HOUSE = {
    "refersion": r"refersion\.com|refersion\.js|_refersion",
    "goaffpro": r"goaffpro\.com|goaffpro",
    "uppromote": r"uppromote\.com|secomapp",
    "social-snowball": r"socialsnowball\.io|snwbl\.io",
    "shopify-collabs": r"collabs\.shopify\.com|shopify_collabs",
    "leaddyno": r"leaddyno\.com",
    "affiliatly": r"affiliatly\.com",
    "tapfiliate": r"tapfiliate\.com",
    "post-affiliate-pro": r"postaffiliatepro|pap\.live",
    "firstpromoter": r"firstpromoter\.com|fprom\.co",
    "referralcandy": r"referralcandy\.com",
}

# Networks: the brand joined a third party. Detected only where the brand's own
# page names it — usually an "apply via ..." link on the programme page. Tracking
# domains are included because a network-hosted signup link uses them.
NETWORKS = {
    "impact": r"impactradius|impact\.com/campaign|impactcdn\.com",
    "shareasale": r"shareasale\.com",
    "avantlink": r"avantlink\.com|avantmetrics\.com",
    "cj": r"cj\.com/member|dpbolvw\.net|anrdoezrs\.net|tkqlhce\.com|"
          r"jdoqocy\.com|emjcd\.com",
    "awin": r"awin1\.com|dwin1\.com|zenaps\.com|awin\.com",
    "rakuten": r"linksynergy\.com|rakutenadvertising\.com|rakutenmarketing\.com",
    "partnerize": r"prf\.hn|partnerize\.com",
    "pepperjam": r"pepperjam\.com|pjtra\.com|pjatr\.com",
    "levanta": r"levanta\.io",
}

IN_HOUSE_RE = {k: re.compile(v, re.I) for k, v in IN_HOUSE.items()}
NETWORKS_RE = {k: re.compile(v, re.I) for k, v in NETWORKS.items()}
# Anything already in the tables above must not be reported as "unrecognised".
KNOWN_HOST = re.compile(
    "|".join(list(IN_HOUSE.values()) + list(NETWORKS.values())), re.I)

# href/anchor text that suggests a programme page. Split by strength: the strong
# set is enough on its own, the weak set only counts when the page it leads to
# confirms it — "partners" is as often a stockist list as an affiliate scheme.
STRONG_HINT = re.compile(r"affiliate|ambassador", re.I)
WEAK_HINT = re.compile(
    r"creator|influencer|partner[-_ ]?program|refer[-_ ]?a[-_ ]?friend|"
    r"referral[-_ ]?program|pro[-_ ]?deal", re.I)

# Confirms a page really is an affiliate programme rather than a stockist page.
PROGRAMME_TEXT = re.compile(
    r"affiliate|commission|ambassador|referral|creator program|"
    r"earn (?:a |up to )?\d+%", re.I)

LINK_RE = re.compile(r"<a\b[^>]*?href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>",
                     re.I | re.S)
SCRIPT_RE = re.compile(r"<script\b[^>]*?src=[\"']([^\"']+)[\"']", re.I)
TAG_RE = re.compile(r"<[^>]+>")

# Paths worth a blind guess when the homepage links nothing. Kept short on
# purpose: every entry is a request to somebody's server, and the footer link is
# how a real programme is found in practice.
GUESS_PATHS = ["/pages/affiliate-program", "/pages/affiliates", "/affiliates"]

# Third-party hosts that are never affiliate infrastructure. Only used to keep
# --unknown-markers readable.
BORING = re.compile(
    r"googletagmanager|google-analytics|googleapis|gstatic|doubleclick|"
    r"facebook\.net|connect\.facebook|klaviyo|shopify|shopifycdn|cloudflare|"
    r"jquery|cdnjs|jsdelivr|unpkg|hotjar|tiktok|snapchat|pinterest|bing\.com|"
    r"clarity\.ms|sentry|intercom|zendesk|gorgias|yotpo|judge\.me|okendo|"
    r"attentivemobile|postscript|recharge|bold|stamped\.io|trustpilot|"
    r"cookiebot|onetrust|osano|typekit|fonts\.|adroll|criteo|hubspot|"
    r"segment\.(?:com|io)|amplitude|mixpanel|heap|fullstory|smile\.io", re.I)


def probe(url, timeout=15, retries=1):
    """
    GET returning (status, final_url, text).

    Mirrors resolve_domains.probe, including the 429 retry: a rate limit that
    silently reads as "no programme" is not a gap, it is a wrong answer written
    into the worklist, and this table exists precisely to stop people guessing.
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "text/html,*/*"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.geturl(), r.read(400_000).decode(
                    "utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                wait_s = float((e.headers or {}).get("Retry-After", 30) or 30)
                print(f"        429 from {url} — waiting {wait_s:.0f}s",
                      flush=True)
                time.sleep(wait_s + 2)
                continue
            return e.code, url, ""
        except Exception:
            return 0, url, ""
    return 429, url, ""


def markers_in(text, table):
    """Every vendor in `table` whose marker appears in `text`."""
    return [name for name, rx in table.items() if rx.search(text)]


def programme_links(html_text, base_url):
    """
    Candidate programme URLs from the page's own links, strong hints first.

    Same-host only. An affiliate link pointing off-site is either the network
    handoff — which the network markers catch anyway — or somebody else's
    programme, and following it would attribute a stranger's scheme to this
    brand.
    """
    base_host = urllib.parse.urlparse(base_url).netloc.lower()
    strong, weak = [], []
    for href, inner in LINK_RE.findall(html_text):
        text = TAG_RE.sub(" ", inner)
        target = urllib.parse.urljoin(base_url, href.strip())
        if urllib.parse.urlparse(target).netloc.lower() != base_host:
            continue
        haystack = f"{href} {text}"
        if STRONG_HINT.search(haystack):
            bucket = strong
        elif WEAK_HINT.search(haystack):
            bucket = weak
        else:
            continue
        if target not in bucket:
            bucket.append(target)
    return strong[:2] + weak[:1]


def platform_of(html_text):
    """Storefront platform from homepage HTML alone — no /products.json."""
    if re.search(r"cdn\.shopify\.com|Shopify\.theme|shopifycloud", html_text,
                 re.I):
        return "shopify"
    if re.search(r"/wp-content/|woocommerce", html_text, re.I):
        return "woocommerce"
    if re.search(r"squarespace", html_text, re.I):
        return "squarespace"
    if re.search(r"bigcommerce", html_text, re.I):
        return "bigcommerce"
    return "unknown"


def scan(brand, pacer, guess, unknown_hosts):
    """One brand: homepage, then any programme page it links to."""
    domain = brand["domain"]
    result = {"brand": brand["name"], "domain": domain, "verdict": "none-found",
              "vendor": None, "network": None, "programme_url": None,
              "platform": "unknown", "evidence": []}

    allowed, note = robots_allows(domain, "/")
    if not allowed:
        result["verdict"] = "unreachable"
        result["evidence"].append(f"robots.txt disallows / ({note})")
        return result

    pacer.wait()
    status, final_url, home = probe(f"https://{domain}/")
    if status != 200 or not home:
        result["verdict"] = "unreachable"
        result["evidence"].append(
            f"homepage {status or 'no response'}"
            + (" — rate limited, undetermined" if status == 429 else ""))
        return result

    result["platform"] = platform_of(home)

    for host in SCRIPT_RE.findall(home):
        netloc = urllib.parse.urlparse(
            urllib.parse.urljoin(final_url, host)).netloc.lower()
        if netloc and urllib.parse.urlparse(final_url).netloc.lower() \
                not in netloc and not BORING.search(netloc) \
                and not KNOWN_HOST.search(netloc):
            unknown_hosts[netloc] += 1

    # An app's JS on the storefront is the strongest in-house signal there is:
    # it means the programme is installed and running, not merely advertised.
    for vendor in markers_in(home, IN_HOUSE_RE):
        result.update(verdict=f"in-house:{vendor}", vendor=vendor)
        result["evidence"].append(f"{vendor} marker in homepage source")
        break

    # Network tracking on the storefront is direct evidence of membership: the
    # pixel exists to attribute conversions, so it only ships once the brand has
    # actually joined. Checking networks on the programme page alone undercounts
    # them badly — the first full run found Awin's dwin1.com on three
    # storefronts and still filed all three as none-found.
    for network in markers_in(home, NETWORKS_RE):
        result["network"] = network
        result["evidence"].append(f"{network} tracking in homepage source")
        if not result["vendor"]:
            result["verdict"] = f"network:{network}"
        break

    candidates = programme_links(home, final_url)
    if not candidates and guess:
        candidates = [f"https://{domain}{p}" for p in GUESS_PATHS]

    for url in candidates:
        path = urllib.parse.urlparse(url).path or "/"
        ok, _ = robots_allows(domain, path)
        if not ok:
            continue
        pacer.wait()
        status, page_url, page = probe(url)
        if status != 200 or not page:
            continue
        if not PROGRAMME_TEXT.search(TAG_RE.sub(" ", page)[:20_000]):
            continue

        result["programme_url"] = page_url
        result["evidence"].append(f"programme page {page_url}")

        for vendor in markers_in(page, IN_HOUSE_RE):
            result.update(verdict=f"in-house:{vendor}", vendor=vendor)
            result["evidence"].append(f"{vendor} marker on programme page")
            break
        for network in markers_in(page, NETWORKS_RE):
            result["network"] = network
            result["evidence"].append(f"{network} named on programme page")
            if not result["vendor"]:
                result["verdict"] = f"network:{network}"
            break
        if result["verdict"] == "none-found":
            result["verdict"] = "page-only"
            result["evidence"].append("no vendor identified — needs a look")
        break

    return result


def catalogue_depth():
    """Indexed models per brand — the ranking weight for the worklist."""
    with open(BAGS) as f:
        data = json.load(f)
    bags = data["bags"] if isinstance(data, dict) else data
    depth = collections.Counter()
    for bag in bags:
        depth[bag.get("brand_slug") or bag.get("brand")] += 1
    return depth


def load_targets(only, limit):
    """brands.json is the fetched set; brands-master.json is the researched
    superset. Scan both, since a brand worth applying to need not be indexed
    yet — but rank by what is actually on the site."""
    with open(BRANDS) as f:
        rows = list(json.load(f))
    have = {b.get("domain") for b in rows}
    with open(MASTER) as f:
        master = json.load(f)
    for b in (master["brands"] if isinstance(master, dict) else master):
        if b.get("domain") and b["domain"] not in have:
            rows.append({"slug": None, "name": b["name"],
                         "domain": b["domain"]})
    rows = [b for b in rows if b.get("domain")]
    if only:
        wanted = {n.strip().lower() for n in only.split(",") if n.strip()}
        rows = [b for b in rows if b["name"].lower() in wanted]
    if limit:
        rows = rows[:limit]
    return rows


def write_outputs(results):
    with open(OUT_JSON, "w") as f:
        json.dump({"meta": {
            "note": "Verdicts are evidence-based. 'none-found' means no signal "
                    "either way, NOT that no programme exists — network "
                    "membership is largely undetectable from a brand's own "
                    "storefront. Markers are seeded, not measured; see "
                    "--unknown-markers.",
            "brand_count": len(results),
        }, "brands": results}, f, indent=1)

    with open(OUT_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rank", "brand", "domain", "models", "verdict", "vendor",
                    "network", "programme_url", "platform", "evidence",
                    "applied", "status"])
        for i, r in enumerate(results, 1):
            w.writerow([i, r["brand"], r["domain"], r["models"], r["verdict"],
                        r["vendor"] or "", r["network"] or "",
                        r["programme_url"] or "", r["platform"],
                        "; ".join(r["evidence"])[:180], "", ""])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delay", type=float, default=3.0,
                    help="seconds between requests (raise if a crawl is "
                         "already running on this IP)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default="", help="comma-separated brand names")
    ap.add_argument("--no-guess", action="store_true",
                    help="only follow programme links the site itself "
                         "publishes; never probe guessed paths")
    ap.add_argument("--apply", action="store_true",
                    help=f"write {os.path.basename(OUT_JSON)} and "
                         f"{os.path.basename(OUT_CSV)}")
    ap.add_argument("--unknown-markers", action="store_true",
                    help="list unrecognised third-party script hosts, most "
                         "common first, to grow the marker table")
    args = ap.parse_args()

    depth = catalogue_depth()
    targets = load_targets(args.only, args.limit)
    print(f"{len(targets)} brands to scan\n", flush=True)

    pacer = Pacer(args.delay)
    unknown_hosts = collections.Counter()
    results = []

    for i, brand in enumerate(targets, 1):
        r = scan(brand, pacer, not args.no_guess, unknown_hosts)
        r["models"] = depth.get(brand.get("slug"), 0) or depth.get(
            brand["name"], 0)
        results.append(r)
        mark = {"in-house": "APPLY", "network": "net  ", "page-only": "look ",
                "none-found": "     ", "unreachable": "?    "}[
            r["verdict"].split(":")[0]]
        print(f"{mark}[{i}/{len(targets)}] {brand['name'][:24]:24} "
              f"{r['models']:>4} models  {r['verdict']}", flush=True)
        for line in r["evidence"]:
            print(f"        {line}", flush=True)

    results.sort(key=lambda r: (
        0 if r["verdict"].startswith("in-house") else
        1 if r["verdict"] == "page-only" else
        2 if r["verdict"].startswith("network") else
        3 if r["verdict"] == "unreachable" else 4,
        -r["models"]))

    by_verdict = collections.Counter(r["verdict"].split(":")[0]
                                     for r in results)
    total_models = sum(r["models"] for r in results) or 1
    print("\n" + "=" * 62)
    for kind in ("in-house", "page-only", "network", "none-found",
                 "unreachable"):
        n = by_verdict.get(kind, 0)
        models = sum(r["models"] for r in results
                     if r["verdict"].split(":")[0] == kind)
        print(f"  {kind:12} {n:>4} brands  {models:>5} models "
              f"({100 * models / total_models:4.1f}% of catalogue)")

    apply_now = sum(r["models"] for r in results
                    if r["verdict"].split(":")[0] in ("in-house", "page-only"))
    print(f"\n  reachable at zero traffic: {100 * apply_now / total_models:.1f}"
          f"% of indexed models")

    vendors = collections.Counter(r["vendor"] for r in results if r["vendor"])
    if vendors:
        print("  vendors: " + ", ".join(f"{v} x{n}"
                                        for v, n in vendors.most_common()))

    if args.unknown_markers and unknown_hosts:
        print("\nunrecognised third-party hosts (candidates for the table):")
        for host, n in unknown_hosts.most_common(30):
            print(f"  {n:>3}  {host}")

    if args.apply:
        write_outputs(results)
        print(f"\nwrote {OUT_JSON}\n      {OUT_CSV}")
    else:
        print("\ndry run — rerun with --apply to write the worklist")


if __name__ == "__main__":
    main()
