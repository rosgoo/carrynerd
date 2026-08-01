#!/usr/bin/env python3
"""
calipered domain resolver — turns a brand name into a verified storefront.

`brands-master.json` holds 170 researched brands and only 40 have somewhere to
fetch from. Names like "Code of Bell" or "Alpha One-Niner" need a domain before
they are worth anything.

The important half is not guessing, it is *proving*. `chrome.com` is not Chrome
Industries, and indexing the wrong company's products is worse than a gap —
it is the one failure the "nothing is estimated" rule cannot absorb. So every
candidate has to earn its place:

    DNS resolves          free, no traffic to anyone, kills most candidates
    homepage says so      brand name in <title>, og:site_name or JSON-LD
    the feed agrees       /products.json exists and its `vendor` matches

A domain that clears DNS but fails the name checks is recorded as a *candidate*
for a human to look at, never written in as the answer. Confidence is stated,
not implied.

Usage:
    python3 resolve_domains.py                  # everything missing a domain
    python3 resolve_domains.py --limit 10       # a taste
    python3 resolve_domains.py --only "Tom Bihn,Bellroy"
    python3 resolve_domains.py --apply          # write results into the file
"""

import argparse
import csv
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from fetch import Pacer, UA, robots_allows

HERE = os.path.dirname(os.path.abspath(__file__))
MASTER = os.path.join(HERE, "data", "brands-master.json")
BRANDS = os.path.join(HERE, "brands.json")
# Everything the resolver could not prove, in a form a person can work through
# quickly. Filling the `domain` column and running --import closes the loop.
TODO = os.path.join(HERE, "data", "domains-todo.csv")

# Suffix patterns that DTC carry brands actually use. Taken from the 40 already
# resolved: aersf.com, alpakagear.com, tortugabackpacks.com, northstbags.com,
# boundarysupply.com, matadorup.com — the pattern is real and productive.
SUFFIXES = [
    "{s}.com", "{s}bags.com", "{s}gear.com", "{s}packs.com", "{s}.co",
    "{s}supply.com", "{s}designs.com", "shop{s}.com", "{s}backpacks.com",
    "{s}travel.com", "{s}.co.uk", "{s}carry.com", "get{s}.com",
    "{s}.myshopify.com",
]

TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
OG_SITE_RE = re.compile(
    r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)', re.I)


def key(text):
    """Comparison form: lowercase, letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def slugs(name, aliases):
    """Candidate name stems, most likely first."""
    out = []
    for label in [name] + list(aliases or []):
        base = key(label)
        if base and base not in out:
            out.append(base)
        # "The North Face" is as likely to be northface.com as thenorthface.com
        stripped = re.sub(r"^the", "", base)
        if stripped and stripped != base and stripped not in out:
            out.append(stripped)
        # "Alpha One-Niner" -> "alphaoneniner" is covered; also try first word
        # only for two-word names where the first is distinctive.
        parts = [p for p in re.split(r"[^A-Za-z0-9]+", label or "") if p]
        if len(parts) > 1 and len(parts[0]) >= 5:
            first = key(parts[0])
            if first not in out:
                out.append(first)
    return out


def candidates(name, aliases):
    seen, out = set(), []
    for stem in slugs(name, aliases):
        for pattern in SUFFIXES:
            domain = pattern.format(s=stem)
            if domain not in seen:
                seen.add(domain)
                out.append(domain)
    return out


def dns_ok(domain, retries=1):
    """
    Free filter. Most invented domains simply do not exist.

    Retries once with a pause, because a local resolver will start refusing
    after a few hundred rapid queries — and a throttled lookup is
    indistinguishable from NXDOMAIN unless you ask twice. Without this the tail
    of a long run comes back "nothing resolves" for brands like Yeti and Vertx,
    which plainly do.
    """
    for attempt in range(retries + 1):
        try:
            socket.getaddrinfo(domain, 443, proto=socket.IPPROTO_TCP)
            return True
        except UnicodeError:
            return False                    # not a resolvable name at all
        except (socket.gaierror, OSError):
            if attempt < retries:
                time.sleep(0.4)
                continue
            return False
    return False


def probe(url, timeout=15, retries=2):
    """
    GET returning (status, final_url, text). Follows redirects.

    Retries on 429, and that matters more here than in the crawler: a rate
    limit that silently reads as "no match" does not produce a gap, it produces
    a *wrong answer*, because the resolver falls through to a worse candidate
    and accepts it. Shopify's edge limit is per-IP across all stores, so any
    other crawl running on the same connection will trip this.
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "text/html,*/*"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.geturl(), r.read(200_000).decode(
                    "utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                wait_s = float((e.headers or {}).get("Retry-After", 60) or 60)
                print(f"        429 from {url} — waiting {wait_s:.0f}s",
                      flush=True)
                time.sleep(wait_s + 2)
                continue
            return e.code, url, ""
        except Exception:
            return 0, url, ""
    return 429, url, ""


# Parking and marketplace pages put the domain — and therefore the brand name —
# straight in their <title>, so the name check alone waves them through.
# ospreygear.com and blackember.co are both for sale, and both passed.
PARKED = re.compile(
    r"domain (?:is )?for sale|premium domain|buy this domain|domain parking|"
    r"this domain may be for sale|hugedomains|afternic|sedo|dan\.com|"
    r"godaddy|namecheap|parked (?:free )?(?:at|by)|atom\.com", re.I)

# Shopify staging and pre-launch stores answer on the right hostname with the
# right brand name and no real catalogue.
NOT_LIVE = re.compile(
    r"\bdev store\b|opening soon|coming soon|password|store is unavailable",
    re.I)


def registrable(host):
    """Rough eTLD+1 — enough to tell a www redirect from a hop to a broker."""
    host = re.sub(r"^www\.", "", (host or "").lower())
    parts = host.split(".")
    if len(parts) > 2 and parts[-2] in ("co", "com", "org", "net", "ac"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def name_in_page(name, aliases, html_text, final_url):
    """Does this page claim to be this brand? Returns the evidence, or None."""
    wanted = {key(n) for n in [name] + list(aliases or []) if key(n)}

    title = TITLE_RE.search(html_text)
    if title:
        got = key(title.group(1))
        if any(w in got for w in wanted):
            return f"title: {title.group(1).strip()[:70]}"

    og = OG_SITE_RE.search(html_text)
    if og and any(w in key(og.group(1)) for w in wanted):
        return f"og:site_name: {og.group(1)[:50]}"

    # A redirect landing on the brand's own name is decent evidence too.
    host = re.sub(r"^https?://([^/]+).*", r"\1", final_url)
    if any(w in key(host) for w in wanted):
        return f"redirects to {host}"
    return None


def check_feed(domain, name, aliases, pacer):
    """Strongest signal: a Shopify feed whose vendor is this brand."""
    allowed, _ = robots_allows(domain)
    if not allowed:
        return None, "robots.txt disallows /products.json"
    pacer.wait()
    status, _, text = probe(f"https://{domain}/products.json?limit=5")
    if status != 200:
        return None, None
    try:
        products = json.loads(text).get("products", [])
    except json.JSONDecodeError:
        return None, None
    if not products:
        return None, None
    wanted = {key(n) for n in [name] + list(aliases or []) if key(n)}
    vendors = {p.get("vendor") for p in products if p.get("vendor")}
    for vendor in vendors:
        if any(w in key(vendor) or key(vendor) in w for w in wanted):
            return True, f"products.json vendor={vendor}"
    return False, f"products.json vendor={sorted(vendors)[:2]} (no match)"


def resolve(brand, pacer, max_candidates):
    name, aliases = brand["name"], brand.get("aliases") or []
    tried, near_misses = 0, []

    for domain in candidates(name, aliases):
        if tried >= max_candidates:
            break
        if not dns_ok(domain):
            continue                      # costs nothing, sends nothing
        tried += 1

        pacer.wait()
        status, final_url, html_text = probe(f"https://{domain}/")
        if status == 429:
            # Do not let a rate limit read as "not this brand" — that is how a
            # transient block becomes a permanently wrong domain in the file.
            near_misses.append({"domain": domain,
                                "why": "rate limited, undetermined"})
            continue
        if status != 200 or not html_text:
            continue

        head = html_text[:6000]
        if PARKED.search(head) or registrable(
                re.sub(r"^https?://([^/]+).*", r"\1", final_url)
        ) != registrable(domain):
            near_misses.append({"domain": domain,
                                "why": f"parked or redirected off-domain "
                                       f"({final_url[:48]})"})
            continue
        if NOT_LIVE.search(head):
            near_misses.append({"domain": domain,
                                "why": "staging or pre-launch store"})
            continue

        evidence = name_in_page(name, aliases, html_text, final_url)
        if not evidence:
            near_misses.append({"domain": domain, "why": "name not on page"})
            continue

        matched, feed = check_feed(domain, name, aliases, pacer)
        if matched:
            return {"domain": domain, "confidence": "verified",
                    "evidence": [evidence, feed], "shopify": True}
        if matched is False:
            # Page says the right brand but the feed says someone else —
            # usually a retailer that stocks them. Not their storefront.
            near_misses.append({"domain": domain, "why": feed})
            continue
        return {"domain": domain, "confidence": "probable",
                "evidence": [evidence] + ([feed] if feed else []),
                "shopify": False}

    return {"domain": None, "confidence": "unresolved",
            "candidates": near_misses[:4]}


def slugify(name):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", name.lower())).strip("-")


def write_todo(unresolved):
    """
    The hand-off. Anything the resolver could not prove lands here with its
    best guess, why that guess failed, and a search link — so the human job is
    "click, look, paste" rather than "start from a name".
    """
    with open(TODO, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name", "domain", "best_guess", "why_unsure", "search"])
        for brand, result in unresolved:
            near = result.get("candidates") or []
            guess = near[0]["domain"] if near else ""
            why = near[0]["why"] if near else "no candidate resolved in DNS"
            query = urllib.parse.urlencode(
                {"q": f'{brand["name"]} bags official site'})
            # `domain` is intentionally the second column: it is the one a
            # person fills in, and it should not be off the side of the screen.
            w.writerow([brand["name"], "", guess, why,
                        f"https://duckduckgo.com/?{query}"])
    print(f"\n{len(unresolved)} brands need a human -> {TODO}")
    print("   fill the `domain` column, then: "
          "python3 resolve_domains.py --import")


def import_todo():
    """Read the filled-in CSV back into brands-master.json and brands.json."""
    if not os.path.exists(TODO):
        raise SystemExit(f"no {TODO} — run the resolver first")

    filled = {}
    with open(TODO, newline="") as f:
        for row in csv.DictReader(f):
            domain = (row.get("domain") or "").strip()
            if not domain:
                continue
            # Accept a pasted URL as readily as a bare host.
            domain = re.sub(r"^https?://", "", domain).split("/")[0]
            domain = re.sub(r"^www\.", "", domain).strip().lower()
            if domain:
                filled[row["name"]] = domain
    if not filled:
        raise SystemExit(f"no domains filled in yet in {TODO}")

    with open(MASTER) as f:
        master = json.load(f)
    rows = master["brands"] if isinstance(master, dict) else master
    by_name = {b["name"]: b for b in rows}

    with open(BRANDS) as f:
        brands = json.load(f)
    have = {b["slug"] for b in brands}

    added = 0
    for name, domain in filled.items():
        entry = by_name.get(name)
        if entry is not None:
            entry["domain"] = domain
            entry["domain_source"] = "human"
        slug = slugify(name)
        if slug not in have:
            brands.append({"slug": slug, "name": name, "domain": domain})
            have.add(slug)
            added += 1

    with open(MASTER, "w") as f:
        json.dump(master, f, indent=1)
    with open(BRANDS, "w") as f:
        json.dump(brands, f, indent=2)
    print(f"imported {len(filled)} domains; added {added} to brands.json "
          f"({len(brands)} total)")
    print("next: python3 fetch.py --delay 2")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delay", type=float, default=3.0,
                    help="seconds between requests (higher when a crawl is "
                         "already running on the same IP)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default="", help="comma-separated brand names")
    ap.add_argument("--max-candidates", type=int, default=6,
                    help="HTTP probes per brand before giving up")
    ap.add_argument("--apply", action="store_true",
                    help="write verified domains into brands-master.json")
    ap.add_argument("--import", dest="do_import", action="store_true",
                    help="read the filled-in domains-todo.csv back in")
    ap.add_argument("--dns-only", action="store_true",
                    help="build the review list from DNS alone, no HTTP — "
                         "safe to run while a crawl is going")
    args = ap.parse_args()

    if args.do_import:
        return import_todo()

    with open(MASTER) as f:
        master = json.load(f)
    rows = master["brands"] if isinstance(master, dict) else master

    targets = [b for b in rows if not b.get("domain")]
    if args.only:
        wanted = {n.strip().lower() for n in args.only.split(",") if n.strip()}
        targets = [b for b in rows if b["name"].lower() in wanted]
    if args.limit:
        targets = targets[:args.limit]

    if args.dns_only:
        # DNS answers "does this name exist" without sending anything to the
        # brand's server, so this is safe to run beside a crawl and gives a
        # person a real starting point instead of a bare list of names.
        socket.setdefaulttimeout(3)
        unresolved = []
        for i, brand in enumerate(targets, 1):
            live = []
            for domain in candidates(brand["name"], brand.get("aliases"))[:8]:
                if dns_ok(domain):
                    live.append(domain)
                time.sleep(0.05)            # keep the local resolver friendly
                if len(live) >= 3:
                    break
            print(f"[{i}/{len(targets)}] {brand['name']:26} "
                  f"{', '.join(live) or '(nothing resolves)'}", flush=True)
            unresolved.append((brand, {
                "confidence": "unresolved",
                "candidates": [
                    {"domain": d, "why": "DNS resolves — not yet verified"}
                    for d in live],
            }))
        write_todo(unresolved)
        return 0

    print(f"{len(targets)} brands to resolve\n", flush=True)
    pacer = Pacer(args.delay)
    verified = probable = 0
    unresolved = []

    for i, brand in enumerate(targets, 1):
        result = resolve(brand, pacer, args.max_candidates)
        mark = {"verified": "OK  ", "probable": "~   ",
                "unresolved": "    "}[result["confidence"]]
        print(f"{mark}[{i}/{len(targets)}] {brand['name']:26} -> "
              f"{result['domain'] or '(none)'}", flush=True)
        for line in result.get("evidence", []):
            print(f"        {line}", flush=True)
        for near in result.get("candidates", []):
            print(f"        candidate {near['domain']}: {near['why']}",
                  flush=True)

        brand["domain_result"] = result
        if result["confidence"] == "verified":
            verified += 1
            if args.apply:
                brand["domain"] = result["domain"]
                brand["domain_source"] = "resolver:verified"
        else:
            # Probable is not proven. A storefront that merely says the right
            # name could be a stockist, a fan site or a parked page, and a
            # wrong domain is worse than a blank one — so it goes to the human
            # list with its evidence rather than into the data.
            if result["confidence"] == "probable":
                probable += 1
                result.setdefault("candidates", []).insert(0, {
                    "domain": result["domain"],
                    "why": "; ".join(result.get("evidence", []))[:110],
                })
            unresolved.append((brand, result))

    print(f"\n{verified} verified automatically, "
          f"{probable} probable, {len(targets) - verified - probable} unresolved")
    if unresolved:
        write_todo(unresolved)
    if args.apply:
        with open(MASTER, "w") as f:
            json.dump(master, f, indent=1)
        print(f"wrote verified domains into {MASTER}")
    else:
        print("dry run — rerun with --apply to write verified domains in")


if __name__ == "__main__":
    raise SystemExit(main())
