#!/usr/bin/env python3
"""
bagdex normalizer — turns raw Shopify catalogs into one comparable schema.

The hard part of a bag directory is not fetching, it is that no two brands
describe a bag the same way. Volume shows up in the title ("Travel Pack 45L"),
in a variant option ("21L"), or buried in prose. Dimensions arrive in inches or
centimetres, sometimes as H x W x D and sometimes L x W x H. Weight is
sometimes a structured field and sometimes "2.4 lbs" in a paragraph.

Every extracted field carries a `*_source` sibling recording where it came
from, so a wrong number is auditable rather than mysterious. Fields we cannot
find are null — never guessed.

Usage: python3 normalize.py
"""

import glob
import html
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "raw")
OUT = os.path.join(HERE, "data", "bags.json")

CM_PER_IN = 2.54
G_PER_LB = 453.592
G_PER_OZ = 28.3495

# --- classification ---------------------------------------------------------

# Ordered: first match wins, so specific beats generic.
CATEGORIES = [
    ("sling",            r"\bsling\b|\bcrossbody\b|\bchest (?:pack|bag)\b"),
    ("hip-pack",         r"\b(?:hip pack|fanny pack|waist pack|bum bag|belt bag)\b"),
    ("duffel",           r"\bduff?le\b|\bduffel\b|\bgym bag\b"),
    ("luggage",          r"\b(?:suitcase|carry[- ]on(?: luggage)?|spinner|roller|check[- ]in)\b"),
    ("tote",             r"\btote\b|\bshopper\b"),
    ("messenger",        r"\bmessenger\b|\bcourier\b|\bsatchel\b"),
    ("briefcase",        r"\b(?:briefcase|attach[eé]|portfolio)\b"),
    ("camera-bag",       r"\bcamera (?:bag|backpack|cube)\b|\bphoto (?:bag|pack)\b"),
    ("hiking-pack",      r"\b(?:hiking|backpacking|trekking|mountaineering|thru[- ]hik)\w*\b"),
    ("travel-backpack",  r"\btravel (?:pack|backpack)\b|\bcarry[- ]on backpack\b"),
    ("daypack",          r"\b(?:daypack|day pack|rucksack|backpack|\bpack\b)\b"),
    ("pouch",            r"\b(?:pouch|dopp|kit bag|toiletry|organi[sz]er case)\b"),
]

# Things a bag catalogue should not contain.
NOT_A_BAG = re.compile(
    r"\b(gift card|t-?shirt|tee\b|hoodie|jacket|cap\b|hat\b|beanie|sock|glove|"
    r"pant|short|sweatshirt|wallet|cardholder|card case|keychain|key ?ring|"
    r"patch|sticker|pin\b|lanyard|strap\b|harness|tripod|lens|filter\b|"
    r"battery|charger|cable|adapter|power bank|bottle\b|mug\b|tumbler|"
    r"notebook|journal|pen\b|sunglass|watch\b|towel|blanket|pillow|"
    r"tent\b|sleeping bag|stove|trekking pole|insert\b|packing cube|"
    r"rain ?(?:cover|fly)|repair|warranty|spare|replacement|sample)\b",
    re.I,
)

MATERIALS = [
    ("CORDURA",        r"\bcordura\b"),
    ("X-Pac",          r"\bx-?pac\b|\bvx\d{2}\b"),
    ("Dyneema",        r"\bdyneema\b|\bdcf\b|\bultra ?200\b|\bultra ?400\b"),
    ("ECOPAK",         r"\becopak\b|\bepx\d{2}\b"),
    ("Ballistic nylon",r"\bballistic\b"),
    ("Ripstop nylon",  r"\bripstop\b"),
    ("Robic",          r"\brobic\b"),
    ("Sailcloth",      r"\bsailcloth\b|\bhalcyon\b"),
    ("Canvas",         r"\bcanvas\b|\bwaxed cotton\b"),
    ("Leather",        r"\bfull[- ]grain leather\b|\bleather\b"),
    ("Tarpaulin",      r"\btarpaulin\b|\btarpaulin\b|\bTPU[- ]coated\b"),
    ("Recycled polyester", r"\brecycled (?:pet|polyester)\b|\brepreve\b"),
    ("Polyester",      r"\bpolyester\b"),
    ("Nylon",          r"\bnylon\b"),
]

FEATURES = [
    ("laptop_sleeve",    r"\blaptop (?:sleeve|compartment|pocket)\b|\bpadded laptop\b"),
    ("water_bottle",     r"\bwater bottle pocket\b|\bbottle pocket\b"),
    ("clamshell",        r"\bclamshell\b|\b180[- ]?degree\b|\bfull[- ]zip opening\b"),
    ("luggage_passthrough", r"\b(?:luggage|trolley) (?:pass[- ]?through|sleeve)\b"),
    ("rfid",             r"\brfid\b"),
    ("expandable",       r"\bexpand(?:able|s)\b|\bexpansion zip\b"),
    ("hip_belt",         r"\b(?:hip|waist) belt\b"),
    ("sternum_strap",    r"\bsternum strap\b|\bchest strap\b"),
    ("molle",            r"\bmolle\b|\bpals\b"),
    ("compression",      r"\bcompression strap\b"),
    ("shoe_compartment", r"\bshoe (?:compartment|pocket|garage)\b"),
    ("water_resistant",  r"\bwater[- ]?(?:resistant|repellent|proof)\b|\bdwr\b"),
    ("carry_on",         r"\bcarry[- ]on (?:compliant|approved|size|friendly)\b|\bpersonal item\b"),
    ("lockable_zips",    r"\block(?:able|ing) zip\b|\blockable\b"),
]


def strip_html(s):
    if not s:
        return ""
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</p>|</li>|</div>", " \n ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"[ \t]+", " ", html.unescape(s)).strip()


# --- extraction -------------------------------------------------------------

VOLUME_RE = re.compile(
    r"(?<![A-Za-z0-9.])(\d{1,3}(?:\.\d)?)\s*(?:L\b|l\b|lit(?:er|re)s?\b)", re.I)


def find_volume(text):
    """Liters, sanity-bounded. Returns None rather than a guess."""
    for m in VOLUME_RE.finditer(text or ""):
        try:
            v = float(m.group(1))
        except ValueError:
            continue
        if 0.5 <= v <= 150:
            return round(v, 1)
    return None


DIM_CM = re.compile(
    r"(\d{1,3}(?:\.\d+)?)\s*(?:cm)?\s*[x×*]\s*"
    r"(\d{1,3}(?:\.\d+)?)\s*(?:cm)?\s*[x×*]\s*"
    r"(\d{1,3}(?:\.\d+)?)\s*(?:cm|centimet)", re.I)

DIM_IN = re.compile(
    r"(\d{1,2}(?:\.\d+)?)\s*(?:\"|”|in\b|inch(?:es)?)?\s*[x×*]\s*"
    r"(\d{1,2}(?:\.\d+)?)\s*(?:\"|”|in\b|inch(?:es)?)?\s*[x×*]\s*"
    r"(\d{1,2}(?:\.\d+)?)\s*(?:\"|”|in\b|inch(?:es)?)", re.I)


# The other common shape: axes spelled out one per line, often with the metric
# value in parentheses after the imperial one — Aer's
# `Length: 21.5" (54.5 cm)  Width: 13.5" (34 cm)  Depth: 9" (23 cm)`.
DIM_LABELLED = re.compile(
    r"\b(length|height|tall|width|wide|depth|deep|thickness)\b\s*[:\-–]?\s*"
    r"(\d{1,3}(?:\.\d+)?)\s*"
    r"(?:(cm|centimet\w*)|(\"|”|in\b|inch(?:es)?))?\s*"
    r"(?:\(\s*(\d{1,3}(?:\.\d+)?)\s*cm\s*\))?",
    re.I)

AXIS = {"length": "h", "height": "h", "tall": "h",
        "width": "w", "wide": "w",
        "depth": "d", "deep": "d", "thickness": "d"}


def find_dims_labelled(text):
    """Parse per-axis labelled dimensions. Needs at least two axes to count."""
    axes = {}
    for m in DIM_LABELLED.finditer(text or ""):
        label, value, metric, imperial, paren_cm = m.groups()
        axis = AXIS[label.lower()]
        if axis in axes:
            continue
        if paren_cm:                      # `21.5" (54.5 cm)` — trust the cm
            cm = float(paren_cm)
        elif metric:                      # `54.5 cm`
            cm = float(value)
        elif imperial:                    # `21.5"`
            cm = float(value) * CM_PER_IN
        else:
            continue                      # bare number, unit unknown — skip
        if 2 <= cm <= 120:
            axes[axis] = round(cm, 1)
    if len(axes) >= 2:
        return sorted(axes.values(), reverse=True), "labelled"
    return None, None


def find_dims_cm(text):
    """
    Returns ([h, w, d] in cm, source) — largest value first, since brands
    disagree on ordering and height is the dimension airlines care about.
    """
    text = text or ""
    dims, _ = find_dims_labelled(text)
    if dims and len(dims) == 3:
        return dims, "description:labelled"
    m = DIM_CM.search(text)
    if m:
        vals = [float(g) for g in m.groups()]
        if all(2 <= v <= 120 for v in vals):
            return sorted(vals, reverse=True), "description:cm"
    m = DIM_IN.search(text)
    if m:
        vals = [float(g) * CM_PER_IN for g in m.groups()]
        if all(2 <= v <= 120 for v in vals):
            return sorted((round(v, 1) for v in vals), reverse=True), "description:in"
    if dims:                              # two axes is better than nothing
        return dims, "description:labelled-partial"
    return None, None


WEIGHT_RES = [
    (re.compile(r"(\d{1,4}(?:\.\d+)?)\s*(?:kg|kilogram)", re.I), 1000.0),
    (re.compile(r"(\d{1,2}(?:\.\d+)?)\s*(?:lbs?\b|pounds?\b)", re.I), G_PER_LB),
    (re.compile(r"(\d{1,3}(?:\.\d+)?)\s*(?:oz\b|ounces?\b)", re.I), G_PER_OZ),
    (re.compile(r"(\d{2,4})\s*(?:g\b|grams?\b)", re.I), 1.0),
]


def find_weight_g(text):
    for rx, mult in WEIGHT_RES:
        m = rx.search(text or "")
        if m:
            g = float(m.group(1)) * mult
            if 50 <= g <= 8000:
                return int(round(g))
    return None


LAPTOP_RES = [
    re.compile(r"(\d{2}(?:\.\d)?)\s*(?:\"|”|in\b|inch(?:es)?)?\s*laptop", re.I),
    re.compile(r"laptop[^.\n]{0,40}?(\d{2}(?:\.\d)?)\s*(?:\"|”|inch)", re.I),
]


def find_laptop_in(text):
    for rx in LAPTOP_RES:
        m = rx.search(text or "")
        if m:
            v = float(m.group(1))
            if 10 <= v <= 18:
                return v
    return None


def classify(title, product_type, tags):
    blob = " ".join([title or "", product_type or "", " ".join(tags or [])])
    for name, pattern in CATEGORIES:
        if re.search(pattern, blob, re.I):
            return name
    return None


def detect(text, table):
    out = []
    for name, pattern in table:
        if re.search(pattern, text or "", re.I):
            out.append(name)
    return out


# --- assembly ---------------------------------------------------------------

def option_index(options, pattern):
    for o in options or []:
        if re.search(pattern, o.get("name", ""), re.I):
            return o.get("position", 1) - 1
    return None


def build(product, brand):
    title = product.get("title") or ""
    tags = product.get("tags") or []
    ptype = product.get("product_type") or ""
    body = strip_html(product.get("body_html"))
    blob = f"{title}\n{ptype}\n{' '.join(tags)}\n{body}"

    if NOT_A_BAG.search(title) or NOT_A_BAG.search(ptype):
        return None, "not-a-bag"
    category = classify(title, ptype, tags)
    if not category:
        return None, "unclassified"

    options = product.get("options") or []
    color_idx = option_index(options, r"colou?r|finish|colorway")
    size_idx = option_index(options, r"size|capacity|volume|litre|liter")

    variants, colors, sizes, prices = [], [], [], []
    for v in product.get("variants") or []:
        opts = [v.get("option1"), v.get("option2"), v.get("option3")]
        color = opts[color_idx] if color_idx is not None and color_idx < 3 else None
        size = opts[size_idx] if size_idx is not None and size_idx < 3 else None
        try:
            price = float(v.get("price") or 0) or None
        except (TypeError, ValueError):
            price = None
        try:
            compare = float(v.get("compare_at_price") or 0) or None
        except (TypeError, ValueError):
            compare = None
        if price:
            prices.append(price)
        if color and color not in colors:
            colors.append(color)
        if size and size not in sizes:
            sizes.append(size)
        variants.append({
            "sku": v.get("sku") or None,
            "title": v.get("title"),
            "color": color,
            "size": size,
            "price": price,
            "compare_at": compare,
            "available": bool(v.get("available")),
            "grams": v.get("grams") or None,
        })

    # volume: title > size option > body copy. Most specific wins.
    volume = find_volume(title)
    vol_src = "title" if volume else None
    if volume is None:
        for s in sizes:
            volume = find_volume(s)
            if volume:
                vol_src = "variant-option"
                break
    if volume is None:
        volume = find_volume(body)
        vol_src = "description" if volume else None

    dims, dims_src = find_dims_cm(body)

    grams = [v["grams"] for v in variants if v.get("grams")]
    weight = max(grams) if grams else None
    w_src = "shopify:grams" if weight else None
    if not weight:
        weight = find_weight_g(body)
        w_src = "description" if weight else None

    on_sale = any(v["compare_at"] and v["price"] and v["compare_at"] > v["price"]
                  for v in variants)

    images = product.get("images") or []
    handle = product.get("handle") or ""

    return {
        "id": f"{brand['slug']}__{handle}",
        "brand": brand["name"],
        "brand_slug": brand["slug"],
        "name": title,
        "category": category,
        "url": f"https://{brand['domain']}/products/{handle}",
        "image": (images[0].get("src") if images else None),
        "volume_l": volume,
        "volume_source": vol_src,
        "dims_cm": dims,
        "dims_source": dims_src,
        "linear_cm": round(sum(dims), 1) if dims else None,
        "weight_g": weight,
        "weight_source": w_src,
        "laptop_in": find_laptop_in(blob),
        "price_min": round(min(prices), 2) if prices else None,
        "price_max": round(max(prices), 2) if prices else None,
        "on_sale": on_sale,
        "in_stock": any(v["available"] for v in variants),
        "colors": colors,
        "sizes": sizes,
        "variant_count": len(variants),
        "variants": variants,
        "materials": detect(blob, MATERIALS),
        "features": detect(blob, FEATURES),
        "tags": tags,
        "updated_at": product.get("updated_at"),
        "source": "shopify:products.json",
        "fetched_at": brand.get("fetched_at"),
    }, None


# --- model merging ----------------------------------------------------------

# Some brands publish one Shopify product per colourway (Able Carry ships four
# separate "Daily Backpack" products). Others use a single product with a Color
# option. Both describe one model, so the index has to collapse the first shape
# into the second or the same bag appears four times.
#
# Only actual colours are stripped. Fabric names like X-Pac, Ultra and CORDURA
# stay, because a Travel Pack in X-Pac genuinely differs in weight and price
# from the nylon one.
COLOUR_WORD = (
    r"(?:jet\s+)?black|navy|blue|olive|green|gr[ae]y|charcoal|white|cream|tan|"
    r"brown|red|orange|yellow|purple|pink|sand|khaki|coyote(?:\s+brown)?|"
    r"silver|clear|multicam|natural|stone|slate|midnight|graphite|mustard|"
    r"burgundy|teal|ranger\s+green|bone|ash|sage|rust|wine|forest"
)
TRAILING_COLOUR = re.compile(
    r"\s*[-–—|,:/]\s*(?:" + COLOUR_WORD + r")\s*$", re.I)


def model_key(name):
    key = name or ""
    for _ in range(2):                       # "Pack - Black - Large"
        key = TRAILING_COLOUR.sub("", key)
    return re.sub(r"\s+", " ", key).strip().lower()


def merge_models(bags):
    groups = {}
    for bag in bags:
        groups.setdefault((bag["brand_slug"], model_key(bag["name"])), []).append(bag)

    merged = []
    for (_, key), group in groups.items():
        if len(group) == 1:
            group[0]["merged_from"] = 1
            merged.append(group[0])
            continue

        # Canonical record = the one with the most populated spec fields.
        spec_fields = ("volume_l", "dims_cm", "weight_g", "laptop_in")
        base = max(group, key=lambda b: sum(b.get(f) is not None for f in spec_fields))
        out = dict(base)
        out["name"] = min((b["name"] for b in group), key=len)

        for bag in group:
            if bag is base:
                continue
            for field in spec_fields:
                if out.get(field) is None and bag.get(field) is not None:
                    out[field] = bag[field]
                    out[field.replace("_l", "_source").replace("_g", "_source")] = \
                        bag.get(field.replace("_l", "_source").replace("_g", "_source"))
            if out.get("image") is None:
                out["image"] = bag.get("image")

        out["variants"] = [v for b in group for v in b["variants"]]
        out["variant_count"] = len(out["variants"])
        out["colors"] = list(dict.fromkeys(
            [c for b in group for c in b["colors"]] +
            # A per-colourway product often carries the colour only in its
            # title, so recover it from the part we stripped.
            [m.group(0).strip(" -–—|,:/") for b in group
             for m in [TRAILING_COLOUR.search(b["name"])] if m]))
        out["sizes"] = list(dict.fromkeys(s for b in group for s in b["sizes"]))
        out["materials"] = list(dict.fromkeys(m for b in group for m in b["materials"]))
        out["features"] = list(dict.fromkeys(f for b in group for f in b["features"]))
        out["tags"] = list(dict.fromkeys(t for b in group for t in b["tags"]))

        prices = [v["price"] for v in out["variants"] if v["price"]]
        out["price_min"] = round(min(prices), 2) if prices else None
        out["price_max"] = round(max(prices), 2) if prices else None
        out["in_stock"] = any(b["in_stock"] for b in group)
        out["on_sale"] = any(b["on_sale"] for b in group)
        out["linear_cm"] = round(sum(out["dims_cm"]), 1) if out.get("dims_cm") else None
        out["merged_from"] = len(group)
        out["merged_urls"] = [b["url"] for b in group]
        merged.append(out)

    return merged


def main():
    files = sorted(glob.glob(os.path.join(RAW, "*.json")))
    if not files:
        sys.exit("no raw catalogs — run fetch.py first")

    bags, rejects = [], {}
    for path in files:
        with open(path) as f:
            payload = json.load(f)
        brand = {
            "slug": payload["slug"], "name": payload["name"],
            "domain": payload["domain"], "fetched_at": payload.get("fetched_at"),
        }
        for product in payload.get("products", []):
            bag, why = build(product, brand)
            if bag:
                bags.append(bag)
            else:
                rejects[why] = rejects.get(why, 0) + 1

    before = len(bags)
    bags = merge_models(bags)
    bags.sort(key=lambda b: (b["brand"].lower(), b["name"].lower()))

    def coverage(field):
        n = sum(1 for b in bags if b.get(field) is not None)
        return {"have": n, "pct": round(100.0 * n / len(bags)) if bags else 0}

    meta = {
        "generated_from": "public Shopify /products.json feeds",
        "bag_count": len(bags),
        "brand_count": len({b["brand_slug"] for b in bags}),
        "sku_count": sum(b["variant_count"] for b in bags),
        "categories": sorted({b["category"] for b in bags}),
        "products_merged": before - len(bags),
        "rejected": rejects,
        "coverage": {f: coverage(f) for f in
                     ("volume_l", "dims_cm", "weight_g", "laptop_in", "price_min")},
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump({"meta": meta, "bags": bags}, f, separators=(",", ":"))

    print(json.dumps(meta, indent=2))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
