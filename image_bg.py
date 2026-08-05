#!/usr/bin/env python3
"""
carrynerd image-plate sampler — reads the background colour out of each product
photo so the plate behind it can match.

Brands all shoot on white, but not the same white. An eggshell JPEG dropped on
a #fff plate draws a hard rectangle where the two whites meet, and in a grid of
484 of them that seam is the first thing you see. The front end feathers each
photo's outer edge, which softens the boundary without knowing anything about
the image. This stage removes the boundary instead: it reads the colour the
brand actually shot on and hands it to the plate.

The two are complementary, not alternatives. Feathering covers every image;
sampling covers the ones it can read confidently and leaves the rest alone. A
photo whose plate matches exactly still gets feathered, and fading a colour
into itself is invisible, so the belt survives if the braces are wrong by a
shade.

One pass is about 1400 requests but nowhere near 1400 full images: every photo
in the catalog is on Shopify's CDN, which resizes on demand, so we sample a
96px render of each and the whole crawl is a few megabytes.

Results are cached by image URL in data/image-bg.json and the crawl is
resumable. Rerunning after fetch.py rebuilds the catalog costs nothing — the
cache is keyed by URL, and a product keeps its image URL across rebuilds — so
this belongs after enrich in the nightly, not on its own schedule.

Usage:
    python3 image_bg.py                # sample anything new, then apply
    python3 image_bg.py --apply-only   # no network; rewrite bags.json from cache
    python3 image_bg.py --limit 50     # a taste
    python3 image_bg.py --refresh      # re-sample everything, ignore cache
"""

import argparse
import io
import json
import os
import sys
import time
import urllib.parse

from fetch import Pacer, get

# The one stage that is not stdlib-only, because reading a pixel out of a JPEG
# needs a JPEG decoder and the standard library has none. It is quarantined
# here: the four crawl stages stay dependency-free, and a catalog built without
# Pillow is a complete catalog — every plate just falls back to white and the
# CSS edge feather, which is where they all were before this stage existed.
try:
    from PIL import Image, ImageStat
except ImportError:
    Image = ImageStat = None

HERE = os.path.dirname(os.path.abspath(__file__))
BAGS = os.path.join(HERE, "data", "bags.json")
CACHE = os.path.join(HERE, "data", "image-bg.json")

# Sampling is done on a 96px render, not the 2000px original. Shopify's CDN
# resizes on demand, so this is a straight bandwidth win — and the downscale
# averages out JPEG ringing along the way, which makes it a *cleaner* read of
# the background than any single pixel of the source.
THUMB = 96

# An 8px square in each corner of that render. Corners rather than the full
# border ring: a product cropped tight to one edge would poison a ring, and
# the four-corner spread below is what tells us that happened.
PATCH = 8

# How far apart the four corners may be, per channel, before we decide this
# photo has no single background colour — a lifestyle shot, a gradient, a
# vignette. Those keep the white plate and the feather.
AGREE_TOL = 6

# Backgrounds this close to #fff already match the default plate. Emitting an
# override for them costs bytes in bags.json and changes nothing.
NEAR_WHITE = 3

# The chips that sit on the plate (.cat, .flags, .wayname) are fixed light
# values with dark text, so the plate has to stay light for them to read. A
# photo shot on charcoal is a real background colour and we still decline it —
# matching the plate to it would hide the category label. Those keep white.
MIN_LUMA = 205

# Below this the corner is see-through, so its RGB is whatever the encoder
# happened to leave under the transparency and means nothing.
ALPHA_MIN = 250


def thumb_url(url):
    """Ask Shopify's CDN for a THUMB-wide render. Non-Shopify hosts (none in
    the catalog today) fall through and get fetched whole."""
    parts = urllib.parse.urlsplit(url)
    if not (parts.netloc == "cdn.shopify.com"
            or parts.netloc.endswith(".cdn.shopify.com")):
        return url
    query = [(k, v) for k, v in
             urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
             if k not in ("width", "height")]
    query.append(("width", str(THUMB)))
    return urllib.parse.urlunsplit(
        parts._replace(query=urllib.parse.urlencode(query)))


def classify(data):
    """Return (hex or None, reason). None means 'leave the plate white'."""
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception as e:
        return None, f"undecodable:{type(e).__name__}"

    im = im.convert("RGBA")
    if min(im.size) < 2 * PATCH:
        return None, "too-small"

    w, h = im.size
    means = []
    for x, y in ((0, 0), (w - PATCH, 0), (0, h - PATCH), (w - PATCH, h - PATCH)):
        means.append(ImageStat.Stat(im.crop((x, y, x + PATCH, y + PATCH))).mean)

    # Cut-outs first: a transparent corner means there is no background to
    # match. These PNGs were drawn to sit on whatever the page provides, and
    # the page provides white.
    if min(m[3] for m in means) < ALPHA_MIN:
        return None, "transparent"

    spread = max(max(m[c] for m in means) - min(m[c] for m in means)
                 for c in range(3))
    if spread > AGREE_TOL:
        return None, "corners-disagree"

    rgb = tuple(int(round(sum(m[c] for m in means) / 4.0)) for c in range(3))
    if all(v >= 255 - NEAR_WHITE for v in rgb):
        return None, "already-white"
    if 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2] < MIN_LUMA:
        return None, "too-dark"
    return "#%02x%02x%02x" % rgb, "ok"


def image_urls(bags):
    """Every distinct image the front end can put on a plate, in catalog order.

    Variant photos count: with a colour filter on, the card leads with the
    matching colourway's shot rather than the default one, so its plate needs
    a colour too. The 26px swatch thumbnails in the detail table are too small
    for a seam to register and cost nothing extra here — same URLs."""
    seen = {}
    for bag in bags:
        for url in [bag.get("image")] + [v.get("image") for v
                                         in (bag.get("variants") or [])]:
            if url:
                seen.setdefault(url, None)
    return list(seen)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delay", type=float, default=0.2,
                    help="seconds between CDN requests")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--refresh", action="store_true", help="ignore cache")
    ap.add_argument("--apply-only", action="store_true",
                    help="skip the network; rewrite bags.json from cache")
    ap.add_argument("--give-up-after", type=int, default=12,
                    help="consecutive transient failures before aborting")
    args = ap.parse_args()

    if Image is None and not args.apply_only:
        print("image_bg.py needs Pillow to decode product photos:\n"
              "    pip install -r requirements-image-bg.txt\n"
              "Skipping. Plates fall back to white and the CSS edge feather; "
              "run --apply-only to reapply an existing cache without it.",
              file=sys.stderr)
        return 1

    with open(BAGS) as f:
        payload = json.load(f)
    bags = payload["bags"]

    cache = {}
    if os.path.exists(CACHE) and not args.refresh:
        with open(CACHE) as f:
            cache = json.load(f)

    urls = image_urls(bags)
    todo = [u for u in urls if u not in cache]
    if args.limit:
        todo = todo[:args.limit]

    if args.apply_only:
        todo = []
    print(f"{len(urls)} distinct images, {len(cache)} cached, "
          f"{len(todo)} to sample", flush=True)

    pacer = Pacer(args.delay)
    transient = streak = 0
    for i, url in enumerate(todo, 1):
        pacer.wait()
        status, headers, body = get(thumb_url(url), timeout=25)

        # Same rule as enrich: cache permanent answers only. A 404 means the
        # brand pulled the asset; a 429 or 5xx means ask again later. Caching
        # the second kind would pin an image to the white plate forever on the
        # strength of one bad moment.
        if status != 200:
            if status in (404, 410, 451):
                cache[url] = {"bg": None, "why": f"http-{status}"}
                streak = 0
            else:
                transient += 1
                streak += 1
                if streak >= args.give_up_after:
                    print(f"\n  ABORTING: {streak} consecutive transient "
                          f"failures (last status {status}) at image {i}. "
                          f"{len(cache)} cached; rerun to resume.", flush=True)
                    break
        else:
            bg, why = classify(body)
            cache[url] = {"bg": bg, "why": why}
            streak = 0

        if i % 100 == 0 or i == len(todo):
            with open(CACHE, "w") as f:
                json.dump(cache, f, indent=0, sort_keys=True)
            hit = sum(1 for v in cache.values() if v.get("bg"))
            print(f"  [{i}/{len(todo)}] {hit} plates coloured", flush=True)

    with open(CACHE, "w") as f:
        json.dump(cache, f, indent=0, sort_keys=True)
    if transient:
        print(f"  {transient} transient failures left uncached — rerun to "
              f"pick them up", flush=True)

    # Apply. Written as set-or-clear rather than set-only so that retuning a
    # threshold and rerunning actually takes effect; a set-only pass would
    # leave yesterday's colours behind on bags that no longer qualify.
    applied = 0
    for bag in bags:
        for holder in [bag] + list(bag.get("variants") or []):
            bg = (cache.get(holder.get("image") or "") or {}).get("bg")
            if bg:
                holder["image_bg"] = bg
                applied += 1
            else:
                holder.pop("image_bg", None)

    payload["meta"]["image_bg_sampled"] = sum(
        1 for u in urls if u in cache)
    with open(BAGS, "w") as f:
        json.dump(payload, f, separators=(",", ":"))

    reasons = {}
    for url in urls:
        entry = cache.get(url)
        if entry:
            reasons[entry.get("why", "?")] = reasons.get(entry.get("why", "?"), 0) + 1
    unsampled = len(urls) - sum(reasons.values())
    print(f"\ncoloured {applied} plates across {len(bags)} bags")
    for why, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"  {why:20} {n:5}")
    if unsampled:
        print(f"  {'not yet sampled':20} {unsampled:5}")


if __name__ == "__main__":
    sys.exit(main())
