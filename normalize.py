#!/usr/bin/env python3
"""
gearherd normalizer — turns raw Shopify catalogs into one comparable schema.

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
import time

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "raw")
COLLECTIONS = os.path.join(HERE, "data", "collections")
OUT = os.path.join(HERE, "data", "bags.json")
# Dropped products, kept so the classifier can be audited rather than
# guessed at — a count says 141 were dropped, this says which.
REJECTS = os.path.join(HERE, "data", "rejected.json")

CM_PER_IN = 2.54
G_PER_LB = 453.592
G_PER_OZ = 28.3495

# --- classification ---------------------------------------------------------

# Ordered: first match wins, so specific beats generic.
#
# Every noun here takes an optional plural. That is not fussiness: `\bbackpack\b`
# does not match "backpacks", and brands tag products with the plural far more
# often than the singular, so the singular-only forms silently dropped real
# bags — WANDRD's whole PRVKE line among them.
CATEGORIES = [
    ("sling",            r"\bslings?\b|\bcrossbody\b|\bchest (?:packs?|bags?)\b"),
    ("hip-pack",         r"\b(?:hip packs?|fanny packs?|waist packs?|bum bags?|belt bags?)\b"),
    ("duffel",           r"\bduff?les?\b|\bduffels?\b|\bgym bags?\b"),
    ("luggage",          r"\b(?:suitcases?|carry[- ]ons?(?: luggage)?|spinners?|rollers?|check[- ]in)\b"),
    ("tote",             r"\btotes?\b|\bshoppers?\b"),
    ("messenger",        r"\bmessengers?\b|\bcouriers?\b|\bsatchels?\b"),
    ("briefcase",        r"\b(?:briefcases?|attach[eé]s?|portfolios?)\b"),
    ("camera-bag",       r"\bcamera (?:bags?|backpacks?)\b|\bphoto (?:bags?|packs?)\b"),
    ("hiking-pack",      r"\b(?:hiking|backpacking|trekking|mountaineering|thru[- ]hik)\w*\b"),
    ("travel-backpack",  r"\btravel (?:packs?|backpacks?)\b|\bcarry[- ]on backpacks?\b"),
    ("daypack",          r"\b(?:daypacks?|day packs?|rucksacks?|backpacks?|packs?)\b"),
    ("pouch",            r"\b(?:pouch(?:es)?|dopp|kit bags?|toiletry|organi[sz]er cases?)\b"),
]

# Things a bag catalogue should not contain. Same plural rule, same reason —
# `\bstrap\b` never matched "Accessory Straps", so a pile of accessories were
# classified as bags and then showed up with no specs and no features.
NOT_A_BAG = re.compile(
    r"\b(gift cards?|t-?shirts?|tee\b|hoodies?|jackets?|caps?\b|hats?\b|beanies?|"
    r"socks?|gloves?|pants?|shorts?|sweatshirts?|wallets?|cardholders?|"
    r"card cases?|keychains?|key ?rings?|patch(?:es)?|stickers?|pins?\b|"
    r"lanyards?|straps?\b|harness(?:es)?|tripods?|lens(?:es)?|filters?\b|"
    r"batteries|battery|chargers?|cables?|adapters?|power banks?|bottles?\b|"
    r"mugs?\b|tumblers?|notebooks?|journals?|pens?\b|sunglass(?:es)?|"
    r"watch(?:es)?\b|towels?|blankets?|pillows?|tents?\b|sleeping bags?|stoves?|"
    r"trekking poles?|inserts?\b|packing cubes?|camera cubes?|dividers?|"
    r"luggage tags?|bag tags?|"
    r"zip(?:per)? pull(?:er)?s?|playing cards?|rain ?(?:covers?|fly|flies)|repairs?|"
    r"warranty|spares?|replacements?|samples?)\b",
    re.I,
)

# Ordered, first match wins, so the specific fabric beats the generic fibre —
# waxed canvas before canvas, recycled polyester before polyester.
MATERIALS = [
    ("CORDURA",        r"\bcordura\b"),
    ("X-Pac",          r"\bx-?pac\b|\bvx\d{2}\b"),
    ("Dyneema",        r"\bdyneema\b|\bdcf\b"),
    ("UltraWeave",     r"\bultra ?(?:weave|100|200|400|800)\b|\bultra\b(?!\s*light)"),
    ("ECOPAK",         r"\becopak\b|\bepx\d{2}\b"),
    ("Ballistic nylon",r"\bballistic\b"),
    ("Ripstop nylon",  r"\bripstop\b"),
    ("Robic",          r"\brobic\b"),
    ("Sailcloth",      r"\bsailcloth\b|\bhalcyon\b"),
    ("Waxed canvas",   r"\bwaxed\s+(?:canvas|cotton)\b|\bwax(?:ed)?\s+cotton\b"),
    ("Canvas",         r"\bcanvas\b"),
    ("Twill",          r"\btwill\b"),
    ("Suede",          r"\bsuede\b"),
    ("Leather",        r"\bfull[- ]grain leather\b|\bleather\b"),
    ("Tarpaulin",      r"\btarpaulin\b|\bTPU[- ]coated\b"),
    ("Recycled polyester", r"\brecycled (?:pet|polyester)\b|\brepreve\b"),
    ("Polyester",      r"\bpolyester\b"),
    ("Nylon",          r"\bnylon\b"),
]

# --- colour families --------------------------------------------------------
#
# Brands name colourways for places and moods, not colours: "Wasatch Green",
# "Atacama Clay", "Rhone Burgundy". Filtering on the raw strings is useless —
# nobody searches for Atacama. So each name is matched to a family, and the
# brand's own word for the colour is the evidence.
#
# Deliberately not derived from the product photo. The brand has already stated
# the colour in words; a dominant-colour pass over a photo shot on white has to
# guess which region is "the" colour, and gets confounded by lighting, shadow
# and material sheen. It would be more expensive *and* less accurate, and it
# would replace a published fact with an estimate — which is the one thing this
# index does not do.
#
# Ordered: patterns first (a "Multicam Black" is a pattern, not a black), then
# colours. A name that states no colour gets no family rather than a guess.
COLOUR_FAMILIES = [
    ("multi",  r"\b(?:multicam|camo|camouflage|print|plaid|floral|rainbow|"
               r"tie[- ]?dye|grid|geo)\b"),
    ("black",  r"\b(?:black|jet|midnight|onyx|obsidian|graphite|ink|noir|"
               r"nightshade|carbon)\b"),
    ("white",  r"\b(?:white|cream|bone|ivory|chalk|snow|alabaster|ice)\b"),
    ("grey",   r"\b(?:gr[ae]y|charcoal|slate|ash|stone|silver|gunmetal|steel|"
               r"granite|concrete|cobblestone|castle ?rock|chromium|fog|"
               r"pewter|smoke)\b"),
    ("brown",  r"\b(?:brown|tan|khaki|coyote|sand|sandstone|clay|camel|"
               r"chestnut|walnut|mocha|espresso|rust|bronze|natural|beige|"
               r"taupe|earth|desert|dune|saddle|cognac|whisk?ey|copper|sienna|"
               r"elmwood|umber|tobacco|caramel|oat)\b"),
    ("blue",   r"\b(?:blue|navy|teal|cobalt|indigo|denim|aegean|azure|"
               r"sapphire|marine|ocean|caribbean|glacier)\b"),
    ("green",  r"\b(?:green|olive|sage|forest|moss|hunter|ranger|fern|jade|"
               r"emerald|pine|spruce|juniper|woodland|meadow|cypress|"
               r"seaweed)\b"),
    ("red",    r"\b(?:red|burgundy|wine|maroon|crimson|oxblood|cherry|"
               r"scarlet|brick|garnet|salsa|ruby)\b"),
    ("orange", r"\b(?:orange|amber|coral|terracotta|apricot|tangerine|"
               r"persimmon|sunset)\b"),
    ("yellow", r"\b(?:yellow|mustard|gold|dijon|lemon|ochre|honey)\b"),
    ("purple", r"\b(?:purple|plum|violet|lavender|aubergine|eggplant|lilac|"
               r"mauve|huckleberry|loganberry|peri|amethyst)\b"),
    ("pink",   r"\b(?:pink|blush|rose|magenta|fuchsia|salmon)\b"),
]


def colour_family(name):
    """
    The family a colourway name belongs to, or None when it names no colour.
    Material words are stripped first: plenty of brands put the fabric in the
    colour field ("Heritage Suede", "Castlerock Twill", "X-Pac"), and those are
    a material statement, not a colour one.
    """
    text = name or ""
    for _, pattern in MATERIALS:
        text = re.sub(pattern, " ", text, flags=re.I)
    for family, pattern in COLOUR_FAMILIES:
        if re.search(pattern, text, re.I):
            return family
    return None

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
    """
    Returns (category, source). The title is the strongest signal available —
    "Travel Pack 45L" says exactly what it is. product_type and tags are much
    weaker: WANDRD tags its camera daypacks for "backpacking", which read as a
    hiking pack when the two were pooled into one blob. Separating them lets a
    store's own shelving outrank a stray tag while still losing to a title.
    """
    for name, pattern in CATEGORIES:
        if re.search(pattern, title or "", re.I):
            return name, "title"
    blob = " ".join([product_type or "", " ".join(tags or [])])
    for name, pattern in CATEGORIES:
        if re.search(pattern, blob, re.I):
            return name, "tag"
    return None, None


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


def build(product, brand, hint=None):
    title = product.get("title") or ""
    tags = product.get("tags") or []
    ptype = product.get("product_type") or ""
    body = strip_html(product.get("body_html"))
    blob = f"{title}\n{ptype}\n{' '.join(tags)}\n{body}"

    # `hint` is the store's own shelving, from fetch.py --collections. It is
    # not a guess the way a keyword match is: a brand filing something under
    # /accessories is telling us plainly, and it catches products whose title
    # carries no category word at all (WANDRD's entire PRVKE line).
    if hint is not None and hint.get("is_bag") is False:
        return None, "not-a-bag:collection"
    if NOT_A_BAG.search(title) or NOT_A_BAG.search(ptype):
        return None, "not-a-bag"

    # Precedence: the product's own title, then the store's shelving, then
    # tags. A title is specific and deliberate; a shelf is deliberate but
    # broad; a tag is neither, and letting tags win put WANDRD's whole camera
    # line under hiking-pack.
    keyword, keyword_src = classify(title, ptype, tags)
    if keyword_src == "title":
        category, cat_src = keyword, "title"
    elif hint and hint.get("category"):
        category, cat_src = hint["category"], "collection"
    elif keyword:
        category, cat_src = keyword, "tag"
    else:
        category, cat_src = None, None
    if not category:
        # Knowing it is a bag but not what kind is still not knowing. Counted
        # separately so the gap is visible rather than lost in one number.
        return None, ("unclassified:known-bag" if hint and hint.get("is_bag")
                      else "unclassified")

    options = product.get("options") or []
    color_idx = option_index(options, r"colou?r|finish|colorway")
    size_idx = option_index(options, r"size|capacity|volume|litre|liter")

    # Per-colourway photography, already in the feed — no extra requests. Most
    # variants carry featured_image outright; the images[] array also tags
    # entries with variant_ids, which covers the rest.
    by_variant_image = {}
    for img in product.get("images") or []:
        for vid in img.get("variant_ids") or []:
            by_variant_image.setdefault(vid, img.get("src"))

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
            # The family is what makes colour filterable. Nobody searches for
            # "Atacama"; they search for brown.
            "color_family": colour_family(color or v.get("title")),
            "size": size,
            "price": price,
            "compare_at": compare,
            "available": bool(v.get("available")),
            "grams": v.get("grams") or None,
            "image": ((v.get("featured_image") or {}).get("src")
                      or by_variant_image.get(v.get("id"))),
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
        "category_source": cat_src,
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
        "color_families": sorted({f for f in (
            [colour_family(c) for c in colors]
            + [v["color_family"] for v in variants]) if f}),
        # Colourway names are a real source of material facts — brands ship a
        # "Heritage Suede" or an "X-Pac Black" and never mention the fabric in
        # the description at all.
        "materials": sorted(set(detect(blob, MATERIALS))
                            | {m for c in colors
                               for m in detect(c, MATERIALS)}
                            | {m for v in variants if v.get("title")
                               for m in detect(v["title"], MATERIALS)}),
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
    r"burgundy|teal|ranger\s+green|bone|ash|sage|rust|wine|forest|clay"
)

# Fabrics that must survive stripping, because a Travel Pack in X-Pac really is
# a different bag from the nylon one — different weight, different price.
FABRIC_WORD = (
    r"cordura|x-?pac|pac|vx\d{2}|dyneema|dcf|ultra\d*|ecopak|epx\d{2}|"
    r"ballistic|ripstop|robic|sailcloth|halcyon|canvas|leather|tarpaulin|"
    r"polyester|nylon|waxed|cotton|eco"
)

# Two separator shapes, because brands split the colour off differently:
# Able Carry writes "Stash Pouch - Black", WANDRD writes "PRVKE 15L in Wasatch
# Green". The optional word before the colour catches brand-invented names
# ("Wasatch" Green, "Sedona" Orange) but refuses to eat a fabric.
#
# Whitespace is required *before* a punctuation separator so the hyphen inside
# "X-Pac" is never mistaken for one — without that, "Travel Pack 3 in X-Pac
# Black" strips down to "Travel Pack 3 in X".
TRAILING_COLOUR = re.compile(
    r"(?:\s+in\s+|\s+[-–—|,:/]\s*)"
    # The modifier may be hyphenated — WANDRD ships a "High-Gloss Black".
    r"(?:(?!(?:" + FABRIC_WORD + r")\b)[A-Za-z]+(?:-[A-Za-z]+)?\s+)?"
    r"(?:" + COLOUR_WORD + r")\s*$", re.I)


def strip_colour_from_name(name, colors):
    """
    A merged model should not be called "PRVKE 15L in Black" when it has
    fourteen colourways. Take the colour out of the display name and make sure
    it survives in `colors`, where it belongs.
    """
    match = TRAILING_COLOUR.search(name or "")
    if not match:
        return name, colors
    stripped = TRAILING_COLOUR.sub("", name).strip()
    if not stripped:
        return name, colors            # the colour was the whole name
    colour = re.sub(r"^(?:in\s+|[-–—|,:/]\s*)", "", match.group(0).strip(),
                    flags=re.I).strip()
    if colour and colour not in colors:
        colors = colors + [colour]
    return stripped, colors


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
            only = group[0]
            only["merged_from"] = 1
            # Applies to single products too: a lone "PRVKE 41L in Black" is
            # still the PRVKE 41L, and the colour is data, not part of a name.
            only["name"], only["colors"] = strip_colour_from_name(
                only["name"], only.get("colors") or [])
            merged.append(only)
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
        out["name"], out["colors"] = strip_colour_from_name(
            out["name"], out["colors"])
        out["sizes"] = list(dict.fromkeys(s for b in group for s in b["sizes"]))
        out["materials"] = sorted({m for b in group for m in b["materials"]})
        out["color_families"] = sorted(
            {f for b in group for f in (b.get("color_families") or [])}
            | {f for f in (colour_family(c) for c in out["colors"]) if f})
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

    bags, rejects, quarantine = [], {}, []
    for path in files:
        with open(path) as f:
            payload = json.load(f)
        brand = {
            "slug": payload["slug"], "name": payload["name"],
            "domain": payload["domain"], "fetched_at": payload.get("fetched_at"),
        }
        # Optional — produced by `fetch.py --collections`. Absent for brands
        # whose shelving has not been crawled, and the keyword path still works.
        hints = {}
        hint_path = os.path.join(COLLECTIONS, f"{payload['slug']}.json")
        if os.path.exists(hint_path):
            try:
                with open(hint_path) as f:
                    hints = json.load(f).get("products", {})
            except (json.JSONDecodeError, KeyError):
                hints = {}

        for product in payload.get("products", []):
            bag, why = build(product, brand, hints.get(str(product.get("id"))))
            if bag:
                bags.append(bag)
            else:
                rejects[why] = rejects.get(why, 0) + 1
                # Quarantine, not just a tally. A count tells you 141 products
                # were dropped; it does not tell you that they were all one
                # brand whose titles use a plural the classifier could not
                # match. Keeping the rows makes the classifier auditable.
                quarantine.append({
                    "brand_slug": brand["slug"],
                    "reason": why,
                    "title": product.get("title"),
                    "product_type": product.get("product_type") or None,
                    "tags": (product.get("tags") or [])[:6],
                    "url": f"https://{brand['domain']}/products/"
                           f"{product.get('handle') or ''}",
                })

    before = len(bags)
    bags = merge_models(bags)
    bags.sort(key=lambda b: (b["brand"].lower(), b["name"].lower()))

    # URLs come from the merged model name, not from the surviving Shopify
    # handle. Merging picks whichever product had the most specs as the base,
    # so the handle can easily be a colourway — the PRVKE 31L was landing on
    # /bags/wandrd/prvke-31l-in-high-gloss-black/ while the page itself was
    # about all sixteen colourways. `id` stays put because it keys the
    # enrichment cache and the price ledger; only the permalink is derived.
    used = {}
    for bag in bags:
        slug = re.sub(r"-+", "-",
                      re.sub(r"[^a-z0-9]+", "-", bag["name"].lower())).strip("-")
        if not slug:
            slug = bag["id"].split("__", 1)[-1]
        key = (bag["brand_slug"], slug)
        used[key] = used.get(key, 0) + 1
        if used[key] > 1:                 # two models, one name — keep both
            slug = f"{slug}-{used[key]}"
        bag["slug"] = slug

    def coverage(field):
        n = sum(1 for b in bags if b.get(field) is not None)
        return {"have": n, "pct": round(100.0 * n / len(bags)) if bags else 0}

    meta = {
        "generated_from": "public Shopify /products.json feeds",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
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

    quarantine.sort(key=lambda r: (r["reason"], r["brand_slug"], r["title"] or ""))
    with open(REJECTS, "w") as f:
        json.dump({"generated_at": meta["generated_at"],
                   "counts": rejects, "rejected": quarantine}, f, indent=1)

    print(json.dumps(meta, indent=2))
    print(f"\nwrote {OUT}")
    print(f"wrote {REJECTS} ({len(quarantine)} quarantined products)")


if __name__ == "__main__":
    main()
