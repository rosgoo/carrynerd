#!/usr/bin/env python3
"""
carrynerd normalizer — turns raw Shopify catalogs into one comparable schema.

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

import collections
import glob
import html
import json
import os
import re
import sys
import time
from urllib.parse import urljoin

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "raw")
COLLECTIONS = os.path.join(HERE, "data", "collections")
OUT = os.path.join(HERE, "data", "bags.json")
# Dropped products, kept so the classifier can be audited rather than
# guessed at — a count says 141 were dropped, this says which.
REJECTS = os.path.join(HERE, "data", "rejected.json")
# Human rulings on the tail the patterns cannot reach. See `Overrides`.
COLOUR_OVERRIDES_PATH = os.path.join(HERE, "data", "colour-overrides.json")
CATEGORY_OVERRIDES_PATH = os.path.join(HERE, "data", "category-overrides.json")
# Read, not written, here: enrich.py owns this file and runs after us. It has
# been recording each product page's stated priceCurrency all along and nothing
# ever read it — which is why Db shipped as dollars for as long as it did while
# a cache entry three directories away said NOK. See observed_currency().
ENRICH_CACHE = os.path.join(HERE, "data", "enrich-cache.json")

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
    # Spaced as often as compounded. "Cross Body Bag XL" matched nothing at all
    # while "Crossbody" matched here, which is the same one-character miss as
    # "Waistpack" below.
    ("sling",            r"\bslings?\b|\bcross ?body\b|\bchest (?:packs?|bags?)\b"),
    # Optional space in each: brands compound these as often as they space
    # them, and "Waistpack" was reading as no category at all.
    ("hip-pack",         r"\b(?:hip ?packs?|fanny ?packs?|waist ?packs?|"
                         r"bum ?bags?|belt ?(?:bags?|packs?)|lumbar ?packs?)\b"),
    # go-bag: Baboon's entire flagship line (32L/40L/60L) is named this and
    # nothing else, so the whole range sat in `unclassified` while the brand
    # showed as indexed. A store's own coinage beats a generic noun.
    # Bike luggage is its own thing and was arriving as `daypack`, which is
    # both wrong and unfilterable. Above daypack because half these names end
    # in "pack" — Ortlieb's Seat-Pack, a bikepacking frame pack — and \bpacks?\b
    # would take every one of them.
    # Both nouns for each mount point, because a brand picks one and sticks to
    # it: Ortlieb sells a Handlebar-Pack and a Toptube-Bag and means the same
    # kind of thing by both.
    ("bike-bag",         r"\bpanniers?\b|\bbikepacking\b|"
                         r"\b(?:handle ?bar|fork|frame|top[- ]?tube|seat|"
                         r"saddle|gravel|down ?tube|stem)[- ]?(?:bags?|packs?)\b|"
                         r"\bbike[- ]?(?:bags?|packs?|packers?)\b|"
                         r"\btrunk[- ]?bags?\b"),
    # weekender/carryall are what brands call a duffel when they would rather
    # not say duffel. Db, Béis, Monos, Baboon and Heimplanet all ship a
    # "Weekender" at 34-40L, and Dagne Dover's product copy calls its Landon
    # Carryall "the ultimate duffle bag" outright. Neither word appears in a
    # single title here that is not one.
    ("duffel",           r"\bduff?les?\b|\bduffels?\b|\bgym bags?\b|\bgo-?bags?\b|"
                         r"\bweekenders?\b|\bcarry ?alls?\b"),
    ("luggage",          r"\b(?:suitcases?|carry[- ]ons?(?: luggage)?|spinners?|rollers?|check[- ]in)\b"),
    ("tote",             r"\btotes?\b|\bshoppers?\b"),
    ("messenger",        r"\bmessengers?\b|\bcouriers?\b|\bsatchels?\b"),
    ("briefcase",        r"\b(?:briefcases?|attach[eé]s?|portfolios?|folios?)\b"),
    ("camera-bag",       r"\bcamera (?:bags?|backpacks?)\b|\bphoto (?:bags?|packs?)\b"),
    ("hiking-pack",      r"\b(?:hiking|backpacking|trekking|mountaineering|thru[- ]hik)\w*\b"),
    ("travel-backpack",  r"\btravel (?:packs?|backpacks?)\b|\bcarry[- ]on backpacks?\b"),
    # Above daypack, not below: wallets named "Card Pack" would otherwise be
    # eaten by \bpacks?\b, while backpacks with "wallet" in the title are
    # essentially unheard of.
    ("wallet",           r"\bwallets?\b|\bbillfolds?\b|"
                         r"\bcard ?(?:holders?|cases?|sleeves?)\b|"
                         r"\bpassport (?:holders?|covers?|wallets?)\b"),
    ("daypack",          r"\b(?:daypacks?|day packs?|rucksacks?|backpacks?|packs?)\b"),
    # The long tail of soft carry that is not a backpack. "kit" is qualified
    # rather than bare because "Sewing Kit" and "Rift Camera Kit" are contents,
    # not containers — only the toiletry/tech senses name a bag.
    ("pouch",            r"\b(?:pouch(?:es)?|dopp|kit bags?|toiletry|"
                         r"organi[sz]er cases?|organi[sz]ers?|"
                         r"(?:travel|wash|shave|shaving|grooming|tech|split|"
                         r"pro|simple)\s+kits?|"
                         r"zip bags?|pencil cases?|cosmetic cases?|"
                         r"jewel(?:le)?ry cases?|laptop (?:cases?|sleeves?)|"
                         r"stuff sacks?|caddy|caddies)\b"),
]

# Phrases that say a product is carried, not what it is. They classify only
# when nothing else did, and they rank with tags rather than with titles, so a
# store's own shelving still beats them — see classify().
#
# "shoulder bag" is the whole list for now, and both halves of that ranking are
# load-bearing. Ranked as a title it beat the shelf and moved Cotopaxi's Viaje
# and Topo's Mini out of `sling`, where the stores had put them and where they
# belong. Matched plurally it also caught Tatonka, whose adapter prefixes every
# title with the shelf it came from — "Shoulder Bags - Capture Pouch WP BC" —
# so a breadcrumb was overriding a hand-curated shelf map with an artifact of
# how the title was assembled. Shelves are plural, products are singular.
#
# What is left is the case worth having: a bag on a strap that nothing else
# named. Dagne Dover's Nora, GORUCK's waxed canvas. Pakt's MODE Shoulder Bag
# calls itself a briefcase in its own copy and is a fair override candidate —
# data/category-overrides.json, not a cleverer pattern.
CATEGORIES_WEAK = [
    ("messenger",        r"\bshoulder bag\b"),
]

# Things a bag catalogue should not contain. Same plural rule, same reason —
# `\bstrap\b` never matched "Accessory Straps", so a pile of accessories were
# classified as bags and then showed up with no specs and no features.
NOT_A_BAG = re.compile(
    r"\b(gift cards?|t-?shirts?|tee\b|hoodies?|jackets?|caps?\b|hats?\b|beanies?|"
    r"socks?|gloves?|pants?|shorts?|sweatshirts?|"
    r"keychains?|key ?rings?|patch(?:es)?|stickers?|pins?\b|"
    r"lanyards?|straps?\b|harness(?:es)?|buckles?|clips?|hooks?|"
    r"tripods?|lens(?:es)?|filters?\b|"
    r"batteries|battery|chargers?|cables?|adapters?|power banks?|bottles?\b|"
    r"mugs?\b|tumblers?|notebooks?|journals?|pens?\b|sunglass(?:es)?|"
    r"watch(?:es)?\b|towels?|blankets?|pillows?|tents?\b|sleeping bags?|stoves?|"
    r"trekking poles?|inserts?\b|packing cubes?|camera cubes?|dividers?|"
    r"luggage tags?|bag tags?|"
    r"zip(?:per)? pull(?:er)?s?|playing cards?|rain ?(?:covers?|fly|flies)|repairs?|"
    r"warranty|spares?|replacements?|samples?|bundles?|"
    # A "Multi-Pack" is a quantity, not a bag — CamelBak sells straws and
    # bite valves that way and `\bpacks?\b` took every one of them.
    r"straws?|multi[- ]?packs?|\d+\s*pk|packs? of \d+|packages?|"
    # Pack hardware: the parts a pack is assembled from, and the covers it
    # ships under. None of it has a capacity of its own, so every litre figure
    # these carried was lifted off the pack they attach to — ULA's entire
    # accessory shelf was indexed at 30 L, Bonfus's at 55 L, LiteAF's at 20 L.
    # Sorting the index by price per litre floated all of them above the real
    # bags, which is how they were found.
    #
    # Belts match only alongside the word that makes them a component. A "Belt
    # Bag" and a "belt pack" are real hip packs and a bare `\bbelts?\b` takes
    # both; a pocket or pouch that hangs off a hip belt is a real small bag
    # with a real capacity, hence the lookahead rather than a longer list.
    r"(?:hip|waist|webbing)[- ]?belts?(?!\s*(?:pocket|pouch|bags?|packs?))|"
    r"belt extenders?|shock ?cords?|bungee (?:cords?|attachments?)|"
    r"frame ?sheets?|frame ?stays?|alumini?um stays?|"
    r"foam (?:back )?panels?|wheel wells?|casters?|stick[- ]?em tabs?|"
    r"pack ?covers?|rain ?kilts?|pack ?liners?|"
    # Climbing hardware, which an alpine brand sells beside its packs.
    # `sling` is the sharp one: it is a real bag category *and* the word for a
    # loop of webbing, so Mammut's "Contact Sling 8.0 180cm" was being indexed
    # as carry. The length in centimetres is the tell — a bag's name states
    # its litres, never its span.
    r"quickdraws?|carabiners?|belay|crampons?|ice axes?|pitons?|"
    r"down bags?|slings? [^,|]{0,16}\d{2,3} ?cm)\b",
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


# --- reviewed overrides ------------------------------------------------------
#
# The classifier is regex over other people's prose, so it will always have a
# tail it cannot reach: a colourway named after a collaborator ("J Prince"), a
# product whose title names no category any pattern knows. An override is a
# human ruling recorded as data and consulted before the patterns run.
#
# Three states, deliberately. An absent key has not been looked at; a key with
# a value carries the ruling; a key with `null` records that the right answer
# *is* nothing — "X-Pac" names a fabric, not a colour, and saying so once is
# what stops it heading the audit list forever. Without that third state the
# list can only grow, and a list that never shrinks stops being read.
#
# Keyed by brand, because these names are not global. Chrome's "Royale" is a
# red; the next brand's Royale will be a blue, and a flat name -> family map
# would quietly repaint it.

MISSING = object()


def norm_key(s):
    """Keys are matched on their content, not their typing."""
    return re.sub(r"\s+", " ", str(s or "")).strip().casefold()


def product_key(url):
    """
    The last path segment of a product URL — a Shopify handle, a WooCommerce
    slug, a Bellroy path. Stable across a domain move, and the one identifier
    every adapter here can produce for a product it has just rejected.
    """
    path = re.sub(r"[?#].*$", "", url or "").rstrip("/")
    return norm_key(path.rsplit("/", 1)[-1])


class Overrides:
    """A file of reviewed rulings, plus which of them this run actually used."""

    def __init__(self, path, valid, label):
        self.path, self.label = path, label
        self.valid = set(valid)
        self.table, self.used = {}, set()
        if not os.path.exists(path):
            return
        with open(path) as f:
            try:
                raw = json.load(f)
            except json.JSONDecodeError as e:
                sys.exit(f"{path}: not valid JSON ({e})")
        for brand, entries in raw.items():
            # `_`-prefixed keys are notes to whoever opens the file. JSON has
            # nowhere else to put one.
            if brand.startswith("_"):
                continue
            if not isinstance(entries, dict):
                sys.exit(f"{path}: '{brand}' must map keys to rulings, "
                         f"got {type(entries).__name__}")
            for key, value in entries.items():
                # A typo here would otherwise land as a category no filter
                # offers and no page links to — silently missing, which is the
                # failure mode this whole file exists to prevent.
                if value is not None and value not in self.valid:
                    sys.exit(f"{path}: {brand}/{key} -> {value!r} is not a known "
                             f"{label}. Known: {', '.join(sorted(self.valid))}")
                self.table.setdefault(brand, {})[norm_key(key)] = value

    def decide(self, brand, key):
        """The recorded ruling, or MISSING when nobody has ruled on this."""
        entries = self.table.get(brand)
        if not entries:
            return MISSING
        k = norm_key(key)
        if k not in entries:
            return MISSING
        self.used.add((brand, k))
        return entries[k]

    def report(self):
        """
        Counts plus the rulings nothing matched this run — a product the brand
        stopped selling, a colourway they renamed. Reported rather than
        dropped: a hand-off file nobody prunes is one nobody trusts, and the
        audit page is where a stale line should surface.
        """
        stale = [{"brand": b, "key": k}
                 for b, entries in self.table.items() for k in entries
                 if (b, k) not in self.used]
        return {
            "file": os.path.relpath(self.path, HERE),
            "entries": sum(len(e) for e in self.table.values()),
            "used": len(self.used),
            "stale": sorted(stale, key=lambda r: (r["brand"], r["key"])),
        }


COLOUR_OVERRIDES = Overrides(COLOUR_OVERRIDES_PATH,
                             [f for f, _ in COLOUR_FAMILIES], "colour family")
CATEGORY_OVERRIDES = Overrides(CATEGORY_OVERRIDES_PATH,
                               [c for c, _ in CATEGORIES], "category")


# A colour option carrying an internal part number rather than a colour name.
# Cotopaxi's Del Día line is built from offcuts, so every unit is unique and
# the colour axis holds the SKU instead: A35-CHOICEDDC_ZL-10, BATAC24-CHOICEDDC_
# ACR-8. 752 values corpus-wide, 723 of them Cotopaxi's, and every one of them
# reaches the colour filter as a distinct "colour" that matches nothing and
# heads the coverage audit forever.
#
# Deliberately narrow: all-caps with a separator and a digit run. Real colour
# names do not look like this, and anything ambiguous is left for a ruling in
# colour-overrides.json rather than guessed at here.
SKU_CODE_COLOUR = re.compile(r"^[A-Z0-9]+[-_][A-Z0-9][A-Z0-9_-]{2,}$")


def is_sku_code(name):
    """True when a colour option is really a part number."""
    s = (name or "").strip()
    return bool(s) and bool(SKU_CODE_COLOUR.match(s)) and bool(re.search(r"\d", s))


def colour_family(name, brand=None):
    """
    The family a colourway name belongs to, or None when it names no colour.
    Material words are stripped first: plenty of brands put the fabric in the
    colour field ("Heritage Suede", "Castlerock Twill", "X-Pac"), and those are
    a material statement, not a colour one.

    A reviewed ruling for this brand and name wins outright — including a ruling
    of "no colour", which is why the override is consulted before the patterns
    rather than only as a fallback for what they miss.
    """
    if brand:
        decided = COLOUR_OVERRIDES.decide(brand, name)
        if decided is not MISSING:
            return decided
    # After the override, so a ruling can still name one, and before the
    # patterns, so a part number never accidentally matches a colour word.
    if is_sku_code(name):
        return None
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

# `lt` because Rab writes capacity as "28lt / 1710cu.in" and nothing else in
# the corpus spells a litre that way — without it their whole catalogue loses
# its volume to a two-letter abbreviation.
VOLUME_RE = re.compile(
    r"(?<![A-Za-z0-9.])(\d{1,3}(?:[.,]\d)?)\s*(?:L\b|l\b|lt\b|lit(?:er|re)s?\b)",
    re.I)


def find_volume(text):
    """Liters, sanity-bounded. Returns None rather than a guess."""
    for m in VOLUME_RE.finditer(text or ""):
        try:
            v = float(m.group(1).replace(",", "."))
        except ValueError:
            continue
        if 0.5 <= v <= 150:
            return round(v, 1)
    return None


# A litre figure in prose is only this product's capacity if the prose is
# talking about this product. Two ways it isn't, and both put a number shaped
# like an answer in front of the first-match-wins sweep above.
#
# A hydration pack states two capacities and means different things by them:
# CamelBak's "Octane 16 ... with Fusion 2L Reservoir" is a 16-litre pack
# holding a 2-litre bladder, and the only one with a unit attached is the
# bladder. Publishing that as the pack's volume is worse than publishing none.
#
# Both lookbehinds guard the same spec-block shape, where rows run back to back
# with no punctuation between them: "Capacity: 20 liters  Hydration Bladder
# Capacity: No". The first is the digit boundary VOLUME_RE has always had and
# this pattern was missing — without it the match can start at the *second*
# digit and read "0 liters Hydration Bladder", which is true of the text and
# says nothing about the product. The second keeps the next row's label from
# reaching back over the gap to claim a figure that carries its own: a stated
# capacity is the pack's, whatever the row after it happens to mention.
VOLUME_RESERVOIR = re.compile(
    r"(?<![A-Za-z0-9.])(?<![:\-]\s)"
    r"\d{1,3}(?:[.,]\d)?\s*(?:L|l|lt|lit(?:er|re)s?)\s*\W{0,3}\s*"
    r"(?:\w+\s+){0,2}(?:reservoir|bladder|crux)", re.I)

# The other way: an accessory names the pack it attaches to. Bonfus sell a €6
# webbing belt whose copy reads "can be easily attached to or removed from our
# Iterus 38L", and the index published the belt as a 38-litre bag — top of the
# list when you sort by price per litre, which is how this was found.
#
# Scoped to the clause, not to the number: a sentence that has started listing
# other products keeps listing them, and Dakine's "Fits Session 16L, Drafter
# 18L, Drafter 14L" would otherwise give up the first figure and hand over the
# second. A newline ends a clause too — without that, a spec block following
# "fits laptops up to 16in" loses its own capacity to the line above it.
#
# Deliberately not "designed for": brands write "designed for the on-the-go
# traveler ... this versatile 17L bag", where the phrase introduces a buyer
# rather than another product. "designed to fit" only ever introduces a thing.
#
# The clause also ends where the sentence turns back to the product — Sherpani's
# "this personal item bag fits under most airline seats and offers 18 liters of
# organized space" is one sentence doing both jobs, and running to the full stop
# takes the bag's own capacity with it. A conjunction followed by a verb is the
# handover; a conjunction followed by another noun is still the list, which is
# what keeps "Hugger 25L and Hugger 30L Backpack" whole.
VOLUME_CROSSREF = re.compile(
    r"\b(?:fits?|fitting|attach(?:e[sd]|ing)?|compatible|designed to fit|"
    r"works? with|pairs? with|use[sd]? with|insert for|liner for|cover for|"
    r"replacement for|newer version|older version|upgrade (?:to|from)|"
    r"consider our|removed? from|combination with|combined with|addition to|"
    r"organi[sz]\w+ insert)\b"
    r"(?:(?!\b(?:and|or|plus|but|which|that)\s+(?:also\s+)?"
    r"(?:offers?|provides?|holds?|carries|features?|has|have|gives?|"
    r"delivers?|boasts?|includes?)\b)[^.;:|!?\n])*", re.I)


def find_volume_bag(text):
    """
    find_volume over prose, minus any capacity that belongs to something other
    than the bag being described — a water bladder, or a pack the copy is
    cross-referencing. Use this for anything written as sentences; find_volume
    itself is still right for a title or a spec-table cell, where there is no
    room for a second product to be mentioned.
    """
    text = VOLUME_RESERVOIR.sub(" ", text or "")
    return find_volume(VOLUME_CROSSREF.sub(" ", text))


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

# The third shape: the axis letter *follows* its number, which is how the
# Salesforce Commerce Cloud themes write a spec row —
# Gregory's `22.5in H x 11in L x 12.8in W`. The labelled parser cannot see it
# (it wants the word first) and the bare H x W x D parser chokes on the
# letters between the numbers, so it needed its own pattern rather than a
# looser version of either.
DIM_AXIS_SUFFIX = re.compile(
    r"(\d{1,3}(?:\.\d+)?)\s*(cm|mm|\"|”|in\b|inch(?:es)?)?\s*([HWDL])\b\s*[x×*]\s*"
    r"(\d{1,3}(?:\.\d+)?)\s*(cm|mm|\"|”|in\b|inch(?:es)?)?\s*([HWDL])\b\s*[x×*]\s*"
    r"(\d{1,3}(?:\.\d+)?)\s*(cm|mm|\"|”|in\b|inch(?:es)?)?\s*([HWDL])\b",
    re.I)

# Slash-separated metric triples — Deuter's `72 / 34 / 26 cm`. Deliberately
# *not* folded into DIM_CM: a slash between numbers is far more often a size
# range ("38/40/42 cm" of back length) than a measurement, so only the callers
# that know their source writes dimensions this way opt in.
# The trailing `(...)` is Deuter naming the axes after the numbers rather than
# before them — `52 / 23 / 18 (L x W x D) cm` — which put the unit out of
# reach of a pattern that expected it to follow the last number directly.
DIM_CM_SLASH = re.compile(
    r"(\d{1,3}(?:\.\d+)?)\s*/\s*(\d{1,3}(?:\.\d+)?)\s*/\s*"
    r"(\d{1,3}(?:\.\d+)?)\s*(?:\([^)]{0,30}\)\s*)?(?:cm|centimet)", re.I)


def find_dims_axis_suffix(text):
    """Parse `22.5in H x 11in L x 12.8in W`. Returns ([h, w, d] cm, unit)."""
    m = DIM_AXIS_SUFFIX.search(text or "")
    if not m:
        return None, None
    g = m.groups()
    vals, units = [], []
    for i in range(0, 9, 3):
        vals.append(float(g[i]))
        units.append((g[i + 1] or "").lower())
    # Brands mix a unit onto one axis and leave the others bare; whichever
    # unit the row does state governs the whole row.
    stated = next((u for u in units if u), "")
    if stated in ("cm",):
        cm, src = vals, "cm"
    elif stated == "mm":
        cm, src = [v / 10.0 for v in vals], "mm"
    elif stated:
        cm, src = [v * CM_PER_IN for v in vals], "in"
    else:
        return None, None                 # no unit anywhere — not a guess
    if not all(2 <= v <= 120 for v in cm):
        return None, None
    return sorted((round(v, 1) for v in cm), reverse=True), src


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


def find_dims_cm(text, slash=False):
    """
    Returns ([h, w, d] in cm, source) — largest value first, since brands
    disagree on ordering and height is the dimension airlines care about.

    `slash` opts into reading `72 / 34 / 26 cm` as dimensions. Off by default:
    see DIM_CM_SLASH.
    """
    text = text or ""
    dims, _ = find_dims_labelled(text)
    if dims and len(dims) == 3:
        return dims, "description:labelled"
    axis_dims, unit = find_dims_axis_suffix(text)
    if axis_dims:
        return axis_dims, f"description:axis-{unit}"
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
    if slash:
        m = DIM_CM_SLASH.search(text)
        if m:
            vals = [float(g) for g in m.groups()]
            if all(2 <= v <= 120 for v in vals):
                return sorted(vals, reverse=True), "description:cm-slash"
    if dims:                              # two axes is better than nothing
        return dims, "description:labelled-partial"
    return None, None


# `[.,]` for the decimal: half the brands now in the index are European and
# write "2,7 kg". Reading that as an English decimal-free string matched the
# "7 kg" and published a 2.7 kg pack as a 7 kg one — a plausible number,
# silently wrong. The 50 g - 8 kg gate below is what makes taking the comma as
# a decimal point safe: read as a thousands separator instead, every one of
# these lands far outside it and is thrown away rather than believed.
#
# Three guards wrap every one of these, because a weight regex loose over
# free prose finds a number next to a unit long before it finds the product's.
#
# NUM_START — a match may not begin inside a run of digits. On its own this is
# hygiene; with the range guard below it is what stops "10-11kg" from being
# refused at the 11 and then quietly accepted as "1kg".
#
# NOT_RANGE_TOP — the top of a range is not a weight. Bonfus prints a
# recommended load beside every pack ("6-7kg"), and first-match-wins read the 7
# and published the 380 g Iterus 38L as a 7 kg one — which is a pack heavier
# than the load it is rated to carry, and nothing said so.
#
# NOT_AREAL — grams per square metre and ounces per square yard describe the
# fabric, not the bag. Cottage makers name the shell by its areal weight, and
# "3.85oz/sqy" taken as a pack weight is 109 g: that is how the 480 g Bonfus
# Saccus 48L came to lead the lightest-per-litre table. A bare slash cannot be
# the tell — Rab writes "865g / 1lb 14.5oz", one weight restated twice, and
# that 865 is exactly the number we want — so only an area unit after the
# slash disqualifies the match.
NUM_START = r"(?<!\d)"
NOT_RANGE_TOP = r"(?<!\d-)(?<!\d–)(?<!\d—)"
NOT_AREAL = r"(?!\s*(?:/|per)\s*(?:sq|m2|m²|yd|yard|metre|meter|m\b))"
_LEAD = NUM_START + NOT_RANGE_TOP

WEIGHT_KG = (re.compile(_LEAD + r"(\d{1,4}(?:[.,]\d+)?)\s*(?:kg|kilogram)" + NOT_AREAL, re.I), 1000.0)
WEIGHT_LB = (re.compile(_LEAD + r"(\d{1,2}(?:[.,]\d+)?)\s*(?:lbs?\b|pounds?\b)" + NOT_AREAL, re.I), G_PER_LB)
WEIGHT_OZ = (re.compile(_LEAD + r"(\d{1,3}(?:[.,]\d+)?)\s*(?:oz\b|ounces?\b)" + NOT_AREAL, re.I), G_PER_OZ)
WEIGHT_G = (re.compile(_LEAD + r"(\d{2,4})\s*(?:g\b|grams?\b)" + NOT_AREAL, re.I), 1.0)

# Order matters, and the right order depends on what is being read.
#
# Over free prose, the imperial units come first: a description that mentions
# both "600 grams" (of yarn, explaining a denier) and "2.4 lbs" (the pack) is
# talking about two different things, and the pounds are the one being asked
# about.
#
# Over a single spec field it is the opposite. Rab publishes weight as
# "865g / 1lb 14.5oz" — one measurement, restated — and taking the pounds
# meant reading "1lb" and rounding a 865 g pack down to 454 g. Every gram of
# that error was invisible: the number was real, the unit was real, and only
# the choice between them was wrong.
WEIGHT_RES = [WEIGHT_KG, WEIGHT_LB, WEIGHT_OZ, WEIGHT_G]
WEIGHT_RES_METRIC_FIRST = [WEIGHT_KG, WEIGHT_G, WEIGHT_LB, WEIGHT_OZ]


def find_weight_g(text, prefer_metric=False):
    for rx, mult in (WEIGHT_RES_METRIC_FIRST if prefer_metric else WEIGHT_RES):
        m = rx.search(text or "")
        if m:
            g = float(m.group(1).replace(",", ".")) * mult
            if 50 <= g <= 8000:
                return int(round(g))
    return None


# A labelled weight is the product speaking; a bare one in the same paragraph
# might be the fabric. Cottage makers quote "5.8 oz VX21" beside the pack
# weight and Rab explains that "600D means 9,000 meters of yarn weigh 600
# grams" — first-match-wins takes the wrong number in both cases.
#
# NOT_AREAL applies here too: "Fabric weight: 130 g/m²" is labelled with the
# word this pattern keys off, and is still a property of the cloth.
WEIGHT_LABELLED = re.compile(
    r"\b(?:weights?|gewicht)\s*[:\-]?\s*(\d+(?:[.,]\d+)?)\s*"
    r"(oz\b|ounces?\b|g\b|grams?\b|gramm\b|lbs?\b|pounds?\b|kg\b)" + NOT_AREAL, re.I)
WEIGHT_UNIT = {"oz": G_PER_OZ, "ounce": G_PER_OZ, "ounces": G_PER_OZ,
               "g": 1.0, "gram": 1.0, "grams": 1.0, "gramm": 1.0,
               "lb": G_PER_LB, "lbs": G_PER_LB,
               "pound": G_PER_LB, "pounds": G_PER_LB, "kg": 1000.0}


def find_weight_labelled(text):
    m = WEIGHT_LABELLED.search(text or "")
    if not m:
        return None
    g = float(m.group(1).replace(",", ".")) * WEIGHT_UNIT[m.group(2).lower().rstrip(".")]
    return int(round(g)) if 50 <= g <= 8000 else None


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


# Negative signal from the store's own product_type, and *only* product_type.
#
# `unclassified` had grown to 1,786 products, which made it useless as an audit
# list — the genuine misses (a tech organiser, a laptop folio) were buried under
# flannel shirts, down mittens, seatbelt buckles and Béis charms. Those are not
# classification failures; they are things the brand told us are apparel or
# hardware and we simply were not listening.
#
# Matched against product_type rather than the title because the title lies more
# often: "Kadet Organizer" reads like clothing to nobody, but "Fuego Down Scarf"
# and "Rough Runner" are unambiguous once the brand files them under Apparel.
#
# Deliberately NOT triggered by a bare "Accessories". Brands file genuine
# pouches, dopp kits and tech cases there — Aer's Zip Bag and Travel Kit are
# both product_type Accessories — so treating that word as disqualifying would
# throw away real index entries to tidy a counter.
NOT_A_BAG_TYPE = re.compile(
    r"\b(apparel|outerwear|sportswear|activewear|swimwear|underwear|"
    r"fleece|insulation|baselayer|base layer|tops?|bottoms?|shirts?|"
    r"tees?|hoodies?|jackets?|vests?|trousers?|leggings?|dress(?:es)?|"
    r"footwear|shoes?|boots?|trainers?|runners?|sandals?|"
    r"headwear|eyewear|jewell?ery|charms?|"
    r"parts?|hardware|buckles?|components?|spares?|repairs?|"
    r"beauty|skincare|fragrance|grooming|"
    r"cooking|hydration|shelters?|sleep(?:ing)?|tents?|"
    r"gift cards?|events?|resale)\b",
    re.I,
)

# …unless the same product_type also names a carry item. Shopify product_type
# is frequently a breadcrumb rather than a single word, and Béis files its
# Cosmetic Case and Jewelry Case under "Beauty, Cosmetic Case" — a soft case is
# a pouch by any other name, and the leading "Beauty" alone was enough to throw
# eight real products out of the index.
BAGGISH_TYPE = re.compile(
    r"\b(bags?|packs?|backpacks?|pouch(?:es)?|cases?|totes?|slings?|"
    r"duffels?|luggage|folios?|organi[sz]ers?|sleeves?|carriers?)\b",
    re.I,
)


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
    # Weak phrases report themselves as "tag" rather than "title" on purpose.
    # Every builder ranks a title match above the store's shelving and the
    # shelving above tags, so returning "tag" is what puts these last without
    # each builder needing to learn a fourth precedence level.
    for name, pattern in CATEGORIES_WEAK:
        if re.search(pattern, f"{title or ''}\n{blob}", re.I):
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


def build_shopify(product, brand, hint=None, force=None):
    title = product.get("title") or ""
    tags = product.get("tags") or []
    ptype = product.get("product_type") or ""
    body = strip_html(product.get("body_html"))
    blob = f"{title}\n{ptype}\n{' '.join(tags)}\n{body}"

    # `hint` is the store's own shelving, from fetch.py --collections. It is
    # not a guess the way a keyword match is: a brand filing something under
    # /accessories is telling us plainly, and it catches products whose title
    # carries no category word at all (WANDRD's entire PRVKE line).
    # A reviewed ruling states this is a bag and which kind, so the exclusion
    # rules get no vote — overriding them is the entire point of ruling on it.
    if force:
        category, cat_src = force, "override"
    else:
        if hint is not None and hint.get("is_bag") is False:
            return None, "not-a-bag:collection"
        if NOT_A_BAG.search(title) or NOT_A_BAG.search(ptype):
            return None, "not-a-bag"
        # Only after the title has had its say: a "Camera Bag" filed under a
        # store's "Photo Accessories" shelf is still a camera bag.
        if (NOT_A_BAG_TYPE.search(ptype) and not BAGGISH_TYPE.search(ptype)
                and not classify(title, "", [])[0]):
            return None, "not-a-bag:product-type"

        # Precedence: the product's own title, then the store's shelving, then
        # tags. A title is specific and deliberate; a shelf is deliberate but
        # broad; a tag is neither, and letting tags win put WANDRD's whole
        # camera line under hiking-pack.
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
            # Knowing it is a bag but not what kind is still not knowing.
            # Counted separately so the gap is visible rather than lost in one
            # number.
            return None, ("unclassified:known-bag" if hint and hint.get("is_bag")
                          else "unclassified")

    options = product.get("options") or []
    # German axis names matter now that half the index is European — Heimplanet
    # ships Farbe/Größe and Vaude, Deuter, Ortlieb and Tatonka are all on the
    # same footing. Missing the axis does not just lose a label, it drops the
    # colour and size off every variant of that product.
    color_idx = option_index(options, r"colou?r|finish|colorway|farbe")
    # Größe carries an eszett, not a double s, and a pattern written for
    # "grosse" silently matches nothing at all — the same shape of miss as
    # reading "2,7 kg" as 7 kg. All four spellings are in the wild.
    size_idx = option_index(
        options, r"size|capacity|volume|litre|liter|gr(ö|oe|o)(ß|ss)e")

    # Per-colourway photography, already in the feed — no extra requests. Most
    # variants carry featured_image outright; the images[] array also tags
    # entries with variant_ids, which covers the rest.
    by_variant_image = {}
    for img in product.get("images") or []:
        for vid in img.get("variant_ids") or []:
            by_variant_image.setdefault(vid, img.get("src"))

    # Every axis the store defines, by its own name. colour and size get
    # promoted to first-class fields because the filters are built on them; the
    # rest were being discarded outright, and some of them are the only spec
    # their category has. Zpacks and Six Moon sell frameless packs by "Torso
    # Length" and "Belt Length" — the enrichment crawl concluded those brands
    # "do not publish dimensions", which is true and beside the point, because
    # the measurement that actually specifies the pack was sitting in the feed
    # the whole time. Also seen: Material, Style, Length, Width, Type.
    axis_names = [(o.get("name") or "").strip() for o in options[:3]]

    variants, colors, sizes, prices = [], [], [], []
    for v in product.get("variants") or []:
        opts = [v.get("option1"), v.get("option2"), v.get("option3")]
        color = opts[color_idx] if color_idx is not None and color_idx < 3 else None
        # Dropped here rather than in colour_family, which was the first place
        # I put it and did nothing at all: these codes contain no colour word,
        # so the patterns already returned None for them. The leak is `colors`,
        # which is built from the raw option value and never asks. Del Día's
        # first variant is genuinely named "Del Día" and survives; the 700-odd
        # part numbers behind it do not.
        if is_sku_code(color):
            color = None
        size = opts[size_idx] if size_idx is not None and size_idx < 3 else None
        # Keep the raw axis->value map whatever the axis is called. A filter
        # nobody has written yet costs a schema addition, not another crawl.
        axes = {name: val for name, val in zip(axis_names, opts)
                if name and val and name.lower() != "title"}
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
            # 9% of variants ship a blank sku, so sku alone cannot key a
            # colourway. The numeric id is always there and never changes,
            # which is what price history needs to stay attached to a variant
            # across a retitle or a sku backfill.
            "variant_id": v.get("id"),
            "title": v.get("title"),
            "options": axes or None,
            "color": color,
            # The family is what makes colour filterable. Nobody searches for
            # "Atacama"; they search for brown.
            "color_family": colour_family(color or v.get("title"), brand["slug"]),
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
        volume = find_volume_bag(body)
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
        # The locale prefix rides along, so a reader clicking through from a
        # USD listing lands on the USD page rather than the store default.
        "url": f"https://{brand['domain']}{'/' + brand['locale'] if brand.get('locale') else ''}/products/{handle}",
        "image": (images[0].get("src") if images else None),
        # 87% of products carry more than one shot, mean 12.3, and the catalog
        # was keeping exactly one. Referenced at source per the crawl posture,
        # never rehosted, so a gallery costs no requests and no storage — and
        # on a feed-only build it is the difference between a page that shows
        # a product and a page that shows a thumbnail and a row of dashes.
        # width/height come along because they are what stops the layout
        # shifting while they load.
        "images": [{"src": im.get("src"), "w": im.get("width"),
                    "h": im.get("height")}
                   for im in images if im.get("src")],
        # The store's own numeric id. `id` above is brand__handle, so a brand
        # renaming a product's handle reads as a new product and silently
        # orphans its price history; this is the key that survives that.
        "shopify_id": product.get("id"),
        "handle": handle or None,
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
            [colour_family(c, brand["slug"]) for c in colors]
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
        # The store's own shelf label. Already consulted by the classifier as a
        # signal and then thrown away, which meant a misfiled product could not
        # be audited without going back to raw/. It is also the cheapest
        # negative signal available: Apparel (595), Accessories (558),
        # Outerwear (162), Patches (54) and Parts (42) are, between them, most
        # of the 5,033 rows sitting in the quarantine.
        "product_type": ptype or None,
        # vendor is NOT a reliable sub-brand field — Matador files 37 feature
        # strings in it and Calpak uses "-". It earns its place because when it
        # disagrees with the brand it usually means a resold third-party item
        # (Six Moon carries LEKI and CNOC) or a test product (GORUCK-DEMO,
        # "Db Journey - Sandbox/Testing"), neither of which should be indexed
        # as that brand's own. Stored, not acted on, until someone rules on it.
        "vendor": product.get("vendor") or None,
        "axis_names": [n for n in axis_names if n and n.lower() != "title"],
        # Publication dates support new-arrival sorting and, more usefully,
        # tell a discontinued model from one nobody has touched this year.
        "published_at": product.get("published_at"),
        "created_at": product.get("created_at"),
        "updated_at": product.get("updated_at"),
        "source": "shopify:products.json",
        "fetched_at": brand.get("fetched_at"),
    }, None


# --- other platforms ----------------------------------------------------------
#
# One builder per source, all converging on the exact dict build_shopify
# returns. raw/ keeps each source's own shape (so a mapping fix is free, no
# refetch); this is the only place that knows what those shapes are.

def slugify(text):
    return re.sub(r"-+", "-",
                  re.sub(r"[^a-z0-9]+", "-", (text or "").lower())).strip("-")


# Bellroy's /v2/products API — one item per SKU (colourway × material), specs
# under attributes.dimensions with every value wrapped in a single-item list.
# Field notes and the endpoint recipe: Notes/carrynerd/bellroy-api.md.

BELLROY_IMG = ("https://bellroy-product-images.imgix.net/"
               "bellroy_dot_com_gallery_image/{currency}/{sku}/0?auto=format")

# Their shelving, pre-classified. Title still wins — a "Venture Travel Pack"
# shelved under `backpack` is a travel backpack — this is the fallback.
BELLROY_CATEGORY = {
    "backpack": "daypack",
    "tote_bag": "tote",
    "bucket_bag": "tote",
    "cooler_bag": "tote",
    "duffel": "duffel",
    "crossbody_bag": "sling",
    "messenger": "messenger",
    "laptop_case": "pouch",
    "pouch": "pouch",
    "folio": "briefcase",
    "luggage": "luggage",
    "wallet": "wallet",
    "passport_holder": "wallet",
}

# Real products, not bags. Their shelf name is trusted the same way a Shopify
# product_type is — it is the brand speaking, not a keyword guess. (Wallets
# were in this set until the vertical opened on 2026-08-01 — see
# Notes/carrynerd/wallets-later.md for the flip record.)
BELLROY_NOT_A_BAG = {"phone_case", "tech_accessory", "key_holder"}

# product_type is even blunter than the shelf: {Bag, Wheeled Luggage,
# Accessory} carry, the rest (Wallet, Phone Case, Marketing) never do. This is
# what catches "Card Pack" — a promo deck of cards whose title reads as a
# daypack and which has no shelf at all, only product_type: Marketing.
BELLROY_BAG_TYPES = {"Bag", "Wheeled Luggage", "Accessory", "Wallet"}

# Feature tokens that map straight onto ours; anything unmapped still gets a
# chance via detect() over the humanised token text.
BELLROY_FEATURE = {
    "water_resistant": "water_resistant",
    "laptop_sleeve": "laptop_sleeve",
    "sternum_strap": "sternum_strap",
    "trolley_sleeve": "luggage_passthrough",
    "water_bottle_holder": "water_bottle",
    "expandable": "expandable",
}


def _bellroy_val(dims, key):
    v = dims.get(key)
    return v[0] if isinstance(v, list) and v else None


def _bellroy_num(dims, key):
    v = _bellroy_val(dims, key)
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


def group_bellroy(products):
    """One model per (name, size); the feed's item is a single SKU."""
    groups = {}
    for item in products or []:
        a = item.get("attributes") or {}
        dims = a.get("dimensions") or {}
        key = (a.get("name"), _bellroy_val(dims, "size"))
        groups.setdefault(key, []).append(a)
    return list(groups.values())


def build_bellroy(skus, brand, hint=None, force=None):
    rep = skus[0]
    dims = rep.get("dimensions") or {}
    name = rep.get("name") or ""
    size = _bellroy_val(dims, "size")

    # "26l" -> "26L", "16in" -> '16"' (their size axis doubles as laptop fit).
    pretty_size = None
    if size:
        pretty_size = re.sub(r"^(\d+(?:\.\d+)?)IN$", r'\1"', size.upper())

    # "Venture Travel Pack" + 26L -> "Venture Travel Pack 26L"; sized names
    # like "Laptop Caddy 14\"" already carry it and are left alone.
    title = name
    if pretty_size and re.sub(r"[^a-z0-9]", "", size.lower()) not in \
            re.sub(r"[^a-z0-9]", "", name.lower()):
        title = f"{name} {pretty_size}"

    subcat = _bellroy_val(dims, "filter_sub_category") or ""
    ptype = _bellroy_val(dims, "product_type")
    if force:
        category, cat_src = force, "override"
    else:
        if ptype and ptype not in BELLROY_BAG_TYPES:
            return None, "not-a-bag:bellroy-type"
        if NOT_A_BAG.search(title):
            return None, "not-a-bag"
        if subcat in BELLROY_NOT_A_BAG:
            return None, "not-a-bag:bellroy-shelf"
        category, cat_src = classify(title, "", [])
        if not category and subcat in BELLROY_CATEGORY:
            category, cat_src = BELLROY_CATEGORY[subcat], "bellroy-shelf"
        if not category:
            return None, "unclassified"

    axes = [v for v in (_bellroy_num(dims, "product_dim_h_cm"),
                        _bellroy_num(dims, "product_dim_l_cm"),
                        _bellroy_num(dims, "product_dim_d_cm")) if v]
    dims_cm = sorted(axes, reverse=True) if len(axes) >= 2 else None

    # `or None`: a zero is the feed saying "not applicable", not a measurement.
    volume = _bellroy_num(dims, "capacity_litres") or None
    vol_src = "bellroy-api:capacity_litres" if volume else None
    if volume is None:
        ml = _bellroy_num(dims, "product_volume_ml") or None
        if ml:
            volume, vol_src = round(ml / 1000.0, 1), "bellroy-api:product_volume_ml"

    weight = _bellroy_num(dims, "net_weight_g") or None
    weight = int(weight) if weight else None

    laptop = None
    for token in dims.get("filter_device_storage") or []:
        m = re.match(r"laptop_(\d+)_inch", token)
        if m:
            laptop = max(laptop or 0, float(m.group(1)))

    # Features and materials vary per SKU (a leather edition beside a woven
    # one), so union across the whole group like the Shopify path does.
    feat_tokens, mat_text = set(), []
    for a in skus:
        d2 = a.get("dimensions") or {}
        for key in ("filter_feature", "product_feature", "table_features"):
            feat_tokens.update(d2.get(key) or [])
        mat_text.extend(t.replace("_", " ") for t in d2.get("material") or [])
        mat_text.extend(k.replace("material_composition_", "").replace("_", " ")
                        for k in d2 if k.startswith("material_composition_"))
    features = sorted(
        {BELLROY_FEATURE[t] for t in feat_tokens if t in BELLROY_FEATURE}
        | set(detect(" ".join(t.replace("_", " ") for t in feat_tokens),
                     FEATURES)))
    materials = sorted(set(detect(" ".join(mat_text), MATERIALS)))

    variants, colors, prices = [], [], []
    for a in skus:
        d2 = a.get("dimensions") or {}
        color = (_bellroy_val(d2, "color") or "").replace("_", " ").title() or None
        price_obj = a.get("price") or {}
        cents = price_obj.get("price_in_cents")
        price = round(cents / 100.0, 2) if isinstance(cents, (int, float)) else None
        currency = (price_obj.get("currency_code") or "usd").upper()
        material = (_bellroy_val(d2, "material") or "").replace("_", " ").title()
        if price:
            prices.append(price)
        if color and color not in colors:
            colors.append(color)
        variants.append({
            "sku": a.get("sku"),
            "title": " / ".join(x for x in (color, material or None) if x) or None,
            "color": color,
            "color_family": colour_family(color, brand["slug"]),
            "size": pretty_size,
            "price": price,
            "compare_at": None,
            # The feed lists only live products and carries no stock counts;
            # "listed for sale" is what it states, so that is what is stored.
            "available": True,
            "grams": None,
            "barcode": a.get("barcode"),
            "image": BELLROY_IMG.format(currency=currency, sku=a.get("sku")),
        })

    path = (rep.get("canonical_uri") or "").split("?")[0]
    return {
        "id": f"{brand['slug']}__{slugify(title)}",
        "brand": brand["name"],
        "brand_slug": brand["slug"],
        "name": title,
        "category": category,
        "category_source": cat_src,
        "url": f"https://{brand['domain']}{path}",
        "image": variants[0]["image"] if variants else None,
        "volume_l": volume,
        "volume_source": vol_src,
        "dims_cm": dims_cm,
        "dims_source": "bellroy-api:product_dim" if dims_cm else None,
        "linear_cm": round(sum(dims_cm), 1) if dims_cm else None,
        "weight_g": weight,
        "weight_source": "bellroy-api:net_weight_g" if weight else None,
        "laptop_in": laptop,
        "price_min": round(min(prices), 2) if prices else None,
        "price_max": round(max(prices), 2) if prices else None,
        "on_sale": False,
        "in_stock": True,
        "colors": colors,
        "sizes": [pretty_size] if pretty_size else [],
        "variant_count": len(variants),
        "variants": variants,
        "color_families": sorted({f for f in (
            [colour_family(c, brand["slug"]) for c in colors]) if f}),
        "materials": materials,
        "features": features,
        "tags": [],
        "updated_at": None,
        "source": "bellroy:v2-products",
        "fetched_at": brand.get("fetched_at"),
    }, None


# CampSaver — schema.org Product JSON-LD from an aggregator's product pages.
# One page per model; offers[] is the variant level (price, stock, GTIN). The
# spec facts live as labelled text inside `description`, which is what the
# existing extraction helpers already parse.

CUIN_RE = re.compile(r"([\d,]{3,7})\s*(?:cu\.?\s*in\b|cubic\s*inch)", re.I)
CUIN_TO_L = 0.0163871


def build_campsaver(item, brand, hint=None, force=None):
    ld = item.get("product") or {}
    crumbs = [c for c in (item.get("breadcrumbs") or []) if c]
    title = (ld.get("name") or "").strip()
    if not title:
        return None, "no-title"
    brand_name = ((ld.get("brand") or {}).get("name")
                  or ld.get("manufacturer") or "").strip()
    if not brand_name:
        return None, "no-brand"

    crumb_text = " ".join(crumbs)
    if force:
        category, cat_src = force, "override"
    else:
        if NOT_A_BAG.search(title):
            return None, "not-a-bag"
        # Breadcrumbs are the retailer's shelving — same standing as a Shopify
        # product_type, same guard against a shelf name that also names a bag.
        if (NOT_A_BAG_TYPE.search(crumb_text)
                and not BAGGISH_TYPE.search(crumb_text)
                and not classify(title, "", [])[0]):
            return None, "not-a-bag:breadcrumb"
        category, cat_src = classify(title, crumb_text, [])
        if not category:
            return None, "unclassified"

    desc = strip_html(ld.get("description") or "")
    dims_cm, dims_src = find_dims_cm(desc)
    volume = find_volume_bag(desc)
    vol_src = "campsaver:description" if volume else None
    if volume is None:
        m = CUIN_RE.search(desc)
        if m:
            v = float(m.group(1).replace(",", "")) * CUIN_TO_L
            if 0.5 <= v <= 150:
                volume, vol_src = round(v, 1), "campsaver:cubic-inches"
    weight = find_weight_g(desc)

    offers = ld.get("offers") or []
    if isinstance(offers, dict):
        offers = [offers]
    variants, prices = [], []
    for o in offers:
        try:
            price = float(o.get("price")) or None
        except (TypeError, ValueError):
            price = None
        if price:
            prices.append(price)
        variants.append({
            "sku": o.get("sku"),
            "title": None,
            "color": None,
            "color_family": None,
            "size": None,
            "price": price,
            "compare_at": None,
            "available": (o.get("availability") or "").endswith("InStock"),
            "grams": None,
            "barcode": o.get("gtin13") or o.get("gtin"),
            "image": None,
        })

    image = ld.get("image")
    if isinstance(image, list):
        image = image[0] if image else None

    blob = f"{title}\n{desc}"
    return {
        "id": f"campsaver__{slugify(re.sub(r'[.]html$', '', (item.get('url') or '').rsplit('/', 1)[-1]))}",
        "brand": brand_name,
        "brand_slug": slugify(brand_name),
        "name": title,
        "category": category,
        "category_source": cat_src,
        "url": item.get("url") or ld.get("url"),
        "image": image,
        "volume_l": volume,
        "volume_source": vol_src,
        "dims_cm": dims_cm,
        "dims_source": f"campsaver:{dims_src}" if dims_src else None,
        "linear_cm": round(sum(dims_cm), 1) if dims_cm else None,
        "weight_g": weight,
        "weight_source": "campsaver:description" if weight else None,
        "laptop_in": find_laptop_in(blob),
        "price_min": round(min(prices), 2) if prices else None,
        "price_max": round(max(prices), 2) if prices else None,
        "on_sale": False,
        "in_stock": any(v["available"] for v in variants),
        "colors": [],
        "sizes": [],
        "variant_count": len(variants),
        "variants": variants,
        "color_families": [],
        "materials": sorted(set(detect(blob, MATERIALS))),
        "features": detect(blob, FEATURES),
        "tags": [],
        "updated_at": None,
        "source": "campsaver:jsonld",
        "fetched_at": brand.get("fetched_at"),
    }, None


# WooCommerce Store API — public storefront JSON, one item per product with
# attribute terms for colours/sizes. Prices are strings in minor units. The
# payload carries no spec fields at all; everything physical is parsed out of
# the description HTML, which for the cottage makers using Woo (ULA, LiteAF,
# Bonfus) is where they publish exact weights and volumes.

def _woo_price(prices, key):
    raw = (prices or {}).get(key)
    if not raw:
        return None
    try:
        minor = int((prices or {}).get("currency_minor_unit", 2))
        return round(float(raw) / (10 ** minor), 2)
    except (TypeError, ValueError):
        return None


def build_woocommerce(product, brand, hint=None, force=None):
    title = strip_html(product.get("name") or "")
    if not title:
        return None, "no-title"
    cats = [c.get("name") or "" for c in product.get("categories") or []]
    tags = [t.get("name") or "" for t in product.get("tags") or []]
    cats_text = " ".join(cats)

    if force:
        category, cat_src = force, "override"
    else:
        if NOT_A_BAG.search(title):
            return None, "not-a-bag"
        # Categories are the store's own shelving — same standing and same
        # guard as a Shopify product_type.
        if (NOT_A_BAG_TYPE.search(cats_text)
                and not BAGGISH_TYPE.search(cats_text)
                and not classify(title, "", [])[0]):
            return None, "not-a-bag:category"
        category, cat_src = classify(title, cats_text, tags)
        if not category:
            return None, "unclassified"

    desc = strip_html(f"{product.get('description') or ''}\n"
                      f"{product.get('short_description') or ''}")
    blob = f"{title}\n{cats_text}\n{' '.join(tags)}\n{desc}"

    volume = find_volume(title)
    vol_src = "title" if volume else None
    if volume is None:
        volume = find_volume_bag(desc)
        vol_src = "description" if volume else None
    dims, dims_src = find_dims_cm(desc)

    weight = find_weight_labelled(desc)
    w_src = "description:labelled" if weight else None
    if weight is None:
        weight = find_weight_g(desc)
        w_src = "description" if weight else None

    colors, sizes = [], []
    for attr in product.get("attributes") or []:
        terms = [t.get("name") for t in attr.get("terms") or [] if t.get("name")]
        if re.search(r"colou?r|finish", attr.get("name") or "", re.I):
            colors.extend(t for t in terms if t not in colors)
        elif re.search(r"size|capacity|volume|litre|liter",
                       attr.get("name") or "", re.I):
            sizes.extend(t for t in terms if t not in sizes)

    prices = product.get("prices") or {}
    price = _woo_price(prices, "price")
    regular = _woo_price(prices, "regular_price")
    images = product.get("images") or []
    in_stock = bool(product.get("is_in_stock"))

    # The Store API lists variations only as ids; attribute terms above carry
    # the colour/size axes, so one variant row stands for the product.
    variants = [{
        "sku": product.get("sku") or None,
        "title": None,
        "color": colors[0] if len(colors) == 1 else None,
        "color_family": (colour_family(colors[0], brand["slug"])
                         if len(colors) == 1 else None),
        "size": sizes[0] if len(sizes) == 1 else None,
        "price": price,
        "compare_at": regular if regular and price and regular > price else None,
        "available": in_stock,
        "grams": None,
        "image": images[0].get("src") if images else None,
    }]

    return {
        "id": f"{brand['slug']}__{product.get('slug') or slugify(title)}",
        "brand": brand["name"],
        "brand_slug": brand["slug"],
        "name": title,
        "category": category,
        "category_source": cat_src,
        "url": product.get("permalink")
               or f"https://{brand['domain']}/product/{product.get('slug') or ''}",
        "image": images[0].get("src") if images else None,
        "volume_l": volume,
        "volume_source": vol_src,
        "dims_cm": dims,
        "dims_source": dims_src,
        "linear_cm": round(sum(dims), 1) if dims else None,
        "weight_g": weight,
        "weight_source": w_src,
        "laptop_in": find_laptop_in(blob),
        "price_min": price,
        "price_max": price,
        "on_sale": bool(product.get("on_sale")),
        "in_stock": in_stock,
        "colors": colors,
        "sizes": sizes,
        "variant_count": 1,
        "variants": variants,
        "color_families": sorted({f for f in (colour_family(c, brand["slug"])
                                              for c in colors) if f}),
        "materials": sorted(set(detect(blob, MATERIALS))),
        "features": detect(blob, FEATURES),
        "tags": tags,
        "updated_at": None,
        "source": "woocommerce:store-api",
        "fetched_at": brand.get("fetched_at"),
    }, None


# Magento 2 storefront GraphQL — one item per product, variants nested, and
# the store's entire custom-attribute set alongside. The attributes are where
# the specs are, and every store names them differently: Rab writes
# `dimensions: "54 x 32 x 26cm"` and `volume: "28lt / 1710cu.in"` as strings,
# Vaude writes `height`/`width`/`length`/`volume` as bare numbers. Both are
# read here rather than configured per brand, because the shapes are few and a
# brand that matches neither still gets its description parsed.

# Attribute codes carrying a physical fact, most specific first.
MAGENTO_AXIS = {"height": "h", "hoehe": "h", "length": "h", "laenge": "h",
                "width": "w", "breite": "w",
                "depth": "d", "tiefe": "d", "thickness": "d"}
MAGENTO_DIM_ATTR = re.compile(r"dimension|abmessung|ma(?:ß|ss)e|\bsize_cm\b", re.I)
MAGENTO_VOLUME_ATTR = re.compile(r"^(?:volume|volumen|capacity|kapazit)", re.I)
MAGENTO_WEIGHT_ATTR = re.compile(r"weight|gewicht", re.I)

# Attribute codes that are plumbing rather than description. Excluded from the
# text blob so a store's SEO copy and image paths cannot invent a material.
# What a German-language catalogue calls out that the English exclusion list
# has no word for. Vaude sells a "Regenhülle Doppel-Fahrradtaschen" — a rain
# cover for pannier bags — and every pattern we own reads that as a bag.
MAGENTO_NOT_A_BAG = re.compile(
    r"regenh(?:ü|ue)lle|regenschutz|ersatzteil|abdeckung|halterung|"
    r"schloss|adapter|zubeh(?:ö|oe)r", re.I)

MAGENTO_ATTR_NOISE = re.compile(
    r"^(?:image|small_image|thumbnail|swatch|media|url_|meta_|status|"
    r"visibility|tax_class|options_container|msrp|gift_|price|special_|"
    r"sale_flag|mp_|amxnotif|compare_|hide_|enable_|disallow_|disable_|"
    r"listing_|content_block|vimeo|seasonality|mf_pfc|m_percent|"
    r"fourteen_day)", re.I)

# The store's own shelving, as a fallback after the title has had its say.
# Substring patterns rather than \b-anchored words, because German compounds
# are single words: a "Trekkingrucksack" is shelved under "Rucksäcke" and no
# word boundary exists between the noun and its qualifier.
MAGENTO_CATEGORY = [
    ("bike-bag",     r"fahrradtasche|lenkertasche|rahmentasche|satteltasche|"
                     r"gep(?:ä|ae)cktr(?:ä|ae)gertasche|oberrohrtasche|"
                     r"pannier|bikepacking"),
    ("hip-pack",     r"h(?:ü|ue)fttasche|g(?:ü|ue)rteltasche|bauchtasche|"
                     r"hip pack|waist pack|lumbar"),
    ("duffel",       r"reisetasche|sporttasche|duffel|duffle|holdall"),
    ("luggage",      r"koffer|trolley|luggage|suitcase"),
    ("tote",         r"shopper|tote"),
    ("messenger",    r"umh(?:ä|ae)ngetasche|schultertasche|messenger|satchel"),
    ("sling",        r"sling|brusttasche|crossbody"),
    ("pouch",        r"kulturbeutel|waschbeutel|packsack|pouch|organi[sz]er|"
                     r"dopp|toiletry"),
    ("camera-bag",   r"kamerata|camera bag"),
    ("hiking-pack",  r"trekking|wander|bergsteig|alpin|backpacking|mountaineer"),
    ("travel-backpack", r"reiserucks|travel pack|travel backpack"),
    ("daypack",      r"rucks(?:ä|ae)ck|rucksack|daypack|backpack|tagesrucks"),
]


def _magento_attrs(product):
    """code -> value string, flattening both attribute shapes into one map."""
    attrs = {}
    for item in ((product.get("custom_attributesV2") or {}).get("items") or []):
        code = item.get("code")
        if not code:
            continue
        if item.get("value") is not None:
            attrs[code] = str(item["value"])
        elif item.get("selected_options"):
            attrs[code] = "; ".join(
                o.get("label") or "" for o in item["selected_options"]
                if o.get("label"))
    return attrs


def _magento_num(value):
    try:
        n = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n or None                      # a zero is "not set", not a fact


def _magento_grams(value):
    """
    Magento stores weight in whatever unit the store configured and never says
    which. Vaude's packs weigh `880`, Rab's weigh `1.184` — nothing else in a
    bag catalogue is ambiguous at that scale, so the number picks its own unit:
    under 20 it is kilograms, over it is grams.
    """
    n = _magento_num(value)
    if n is None:
        return None
    grams = n * 1000.0 if n <= 20 else n
    return int(round(grams)) if 50 <= grams <= 8000 else None


def build_magento(product, brand, hint=None, force=None):
    title = strip_html(product.get("name") or "")
    if not title:
        return None, "no-title"
    attrs = _magento_attrs(product)
    desc = strip_html(f"{(product.get('description') or {}).get('html') or ''}\n"
                      f"{(product.get('short_description') or {}).get('html') or ''}")
    cats = [c.get("name") or "" for c in product.get("categories") or []]
    shelf = " ".join(cats + [attrs.get(k, "") for k in
                             ("main_category", "internal_category",
                              "filter_product_type", "vaude_category",
                              "programme", "compare_category")])

    if force:
        category, cat_src = force, "override"
    else:
        if NOT_A_BAG.search(title):
            return None, "not-a-bag"
        if MAGENTO_NOT_A_BAG.search(title):
            return None, "not-a-bag:de"
        if (NOT_A_BAG_TYPE.search(shelf) and not BAGGISH_TYPE.search(shelf)
                and not classify(title, "", [])[0]):
            return None, "not-a-bag:category"
        category, cat_src = classify(title, shelf, [])
        if not category:
            for name, pattern in MAGENTO_CATEGORY:
                if re.search(pattern, shelf, re.I):
                    category, cat_src = name, "magento-shelf"
                    break
        if not category:
            return None, "unclassified"

    # Prose attributes only: the feature lists (r_feature1..10), the material
    # breakdown and the buying copy, which is where a Magento brand states the
    # fabric and the pocket count the description skips.
    prose = [v for code, v in sorted(attrs.items())
             if v and not MAGENTO_ATTR_NOISE.search(code) and not v.isdigit()]
    blob = f"{title}\n{shelf}\n{desc}\n" + "\n".join(prose)

    # dims: the store's own axis attributes, then a dimensions string, then
    # the description. Slash-separated triples are allowed out of the
    # attributes (where a lone "72 / 34 / 26 cm" is unambiguous) but not out of
    # prose, where a slash is usually a size range.
    axes = {}
    for code, value in attrs.items():
        axis = MAGENTO_AXIS.get(code.lower())
        if axis and axis not in axes:
            n = _magento_num(value)
            if n and 2 <= n <= 120:
                axes[axis] = round(n, 1)
    dims, dims_src = None, None
    if len(axes) >= 2:
        dims, dims_src = sorted(axes.values(), reverse=True), "magento:attributes"
    if not dims:
        for code, value in attrs.items():
            if MAGENTO_DIM_ATTR.search(code):
                dims, src = find_dims_cm(value, slash=True)
                if dims:
                    dims_src = f"magento:{code}"
                    break
    if not dims:
        dims, dims_src = find_dims_cm(desc)

    volume, vol_src = None, None
    for code, value in attrs.items():
        if not MAGENTO_VOLUME_ATTR.search(code):
            continue
        volume = _magento_num(value) or find_volume(value)
        if volume is None:
            # Rab restates capacity in cubic inches and occasionally drops the
            # litre unit off the front — "22 / 1343cu.in".
            m = CUIN_RE.search(value)
            if m:
                volume = round(float(m.group(1).replace(",", "")) * CUIN_TO_L, 1)
        if volume and 0.5 <= volume <= 150:
            vol_src = f"magento:{code}"
            break
        volume = None
    if volume is None:
        volume = find_volume(title)
        vol_src = "title" if volume else None
    if volume is None:
        volume = find_volume_bag(desc)
        vol_src = "description" if volume else None

    variants_in = product.get("variants") or []

    # A weight attribute that carries its unit is the published spec; the bare
    # number on the product is Magento's shipping weight, which is the same
    # thing plus a box.
    weight, w_src = None, None
    for code, value in sorted(attrs.items()):
        if MAGENTO_WEIGHT_ATTR.search(code) and re.search(r"[a-z]", value, re.I):
            weight = find_weight_g(value, prefer_metric=True)
            if weight:
                w_src = f"magento:{code}"
                break
    if weight is None:
        shipping = [_magento_grams((v.get("product") or {}).get("weight"))
                    for v in variants_in]
        shipping = [g for g in shipping if g]
        weight = max(shipping) if shipping else _magento_grams(product.get("weight"))
        w_src = "magento:weight" if weight else None
    if weight is None:
        weight = find_weight_labelled(desc)
        w_src = "description:labelled" if weight else None

    option_axis = {}
    for option in product.get("configurable_options") or []:
        code = option.get("attribute_code") or ""
        label = option.get("label") or ""
        if re.search(r"colou?r|farbe|finish", f"{code} {label}", re.I):
            option_axis[code] = "color"
        elif re.search(r"size|gr(?:ö|oe)(?:ss|ß)e|capacity|volume|litre|liter",
                       f"{code} {label}", re.I):
            option_axis[code] = "size"

    variants, colors, sizes, prices = [], [], [], []
    for entry in variants_in:
        child = entry.get("product") or {}
        color = size = None
        for attribute in entry.get("attributes") or []:
            axis = option_axis.get(attribute.get("code") or "")
            if axis == "color" and not color:
                color = attribute.get("label")
            elif axis == "size" and not size:
                size = attribute.get("label")
        money = ((child.get("price_range") or {}).get("minimum_price") or {})
        price = _magento_num((money.get("final_price") or {}).get("value"))
        regular = _magento_num((money.get("regular_price") or {}).get("value"))
        if price:
            prices.append(price)
        if color and color not in colors:
            colors.append(color)
        if size and size not in sizes:
            sizes.append(size)
        variants.append({
            "sku": child.get("sku"),
            "title": " / ".join(x for x in (size, color) if x) or child.get("name"),
            "color": color,
            "color_family": colour_family(color, brand["slug"]),
            "size": size,
            "price": round(price, 2) if price else None,
            "compare_at": (round(regular, 2)
                           if regular and price and regular > price else None),
            "available": (child.get("stock_status") or "") == "IN_STOCK",
            "grams": _magento_grams(child.get("weight")),
            "image": (child.get("image") or {}).get("url"),
        })

    money = (product.get("price_range") or {}).get("minimum_price") or {}
    price = _magento_num((money.get("final_price") or {}).get("value"))
    regular = _magento_num((money.get("regular_price") or {}).get("value"))
    top = (((product.get("price_range") or {}).get("maximum_price") or {})
           .get("final_price") or {}).get("value")
    top = _magento_num(top)
    if not variants:
        # A simple product with no configurable axis is still one buyable
        # thing; the index expects at least one row per model.
        if price:
            prices.append(price)
        variants.append({
            "sku": product.get("sku"), "title": None, "color": None,
            "color_family": None, "size": None,
            "price": round(price, 2) if price else None,
            "compare_at": (round(regular, 2)
                           if regular and price and regular > price else None),
            "available": (product.get("stock_status") or "") == "IN_STOCK",
            "grams": _magento_grams(product.get("weight")),
            "image": (product.get("image") or {}).get("url"),
        })

    price_min = round(min(prices), 2) if prices else (round(price, 2) if price else None)
    price_max = round(max(prices), 2) if prices else (round(top or price, 2) if (top or price) else None)

    url_key = product.get("url_key") or slugify(title)
    return {
        "id": f"{brand['slug']}__{url_key}",
        "brand": brand["name"],
        "brand_slug": brand["slug"],
        "name": title,
        "category": category,
        "category_source": cat_src,
        "url": f"https://{brand['domain']}/{url_key}{product.get('url_suffix') or ''}",
        "image": (product.get("image") or {}).get("url"),
        "volume_l": round(volume, 1) if volume else None,
        "volume_source": vol_src,
        "dims_cm": dims,
        "dims_source": dims_src,
        "linear_cm": round(sum(dims), 1) if dims else None,
        "weight_g": weight,
        "weight_source": w_src,
        "laptop_in": find_laptop_in(blob),
        "price_min": price_min,
        "price_max": price_max,
        "on_sale": any(v["compare_at"] for v in variants),
        "in_stock": ((product.get("stock_status") or "") == "IN_STOCK"
                     or any(v["available"] for v in variants)),
        "colors": colors,
        "sizes": sizes,
        "variant_count": len(variants),
        "variants": variants,
        "color_families": sorted({f for f in (colour_family(c, brand["slug"])
                                              for c in colors) if f}),
        "materials": sorted(set(detect(blob, MATERIALS))),
        "features": detect(blob, FEATURES),
        "tags": [],
        "updated_at": None,
        "source": "magento:graphql",
        "fetched_at": brand.get("fetched_at"),
    }, None


# Storefront pages — the digest fetch.py's page crawler keeps of each product
# page. Four brands on four unrelated platforms arrive through here, and no
# two of them publish the same way: Gregory nests schema.org inside a WebPage
# and marks its spec sheet up as name/value divs; CamelBak ships no schema.org
# at all and hides the price in a `content` attribute; Deuter ships a
# ProductGroup with its variants; Mammut ships a Product plus a size table.
#
# So every field below is a short ladder of sources, each rung of which some
# real brand needs. What none of them do is guess: a page that states nothing
# leaves the field null, exactly like the feed adapters.

PAGE_SPEC_HEADING = re.compile(
    r"dimensions?\s*(?:&|and)?\s*weight|specifications?|"
    r"tech(?:nical)?\s*(?:data|details|specs?)|product\s+details|"
    r"technische\s+daten|ma(?:ß|ss)e\s*(?:&|und)?\s*gewicht", re.I)

# Column headers that name a physical axis. Which axis is which does not
# matter — the schema sorts the three values descending anyway — so this only
# has to recognise that a column holds a measurement.
PAGE_TABLE_AXIS = re.compile(
    r"^(?:height|h(?:ö|oe)he|length|l(?:ä|ae)nge|width|breite|depth|tiefe|"
    r"thickness|dicke)\b", re.I)
PAGE_TABLE_WEIGHT = re.compile(r"^(?:weight|gewicht)\b", re.I)


def _ld_walk(blocks):
    """Yield every schema.org node in the page, however it is nested."""
    queue = list(blocks or [])
    seen = 0
    while queue and seen < 200:
        node = queue.pop(0)
        if not isinstance(node, dict):
            continue
        seen += 1
        yield node
        for key in ("mainEntity", "@graph", "hasVariant", "itemOffered"):
            child = node.get(key)
            if isinstance(child, dict):
                queue.append(child)
            elif isinstance(child, list):
                queue.extend(c for c in child if isinstance(c, dict))


def _ld_type(node):
    t = node.get("@type")
    return t if isinstance(t, str) else (t[0] if isinstance(t, list) and t else "")


def _ld_product(blocks):
    """The Product (or ProductGroup) node, and the breadcrumb trail."""
    product, crumbs = None, []
    for node in _ld_walk(blocks):
        kind = _ld_type(node)
        if kind in ("Product", "ProductGroup") and product is None:
            product = node
        elif kind == "BreadcrumbList" and not crumbs:
            # Gregory wraps the list in a second list; flatten whatever comes.
            stack = list(node.get("itemListElement") or [])
            while stack:
                entry = stack.pop(0)
                if isinstance(entry, list):
                    stack = list(entry) + stack
                    continue
                if not isinstance(entry, dict):
                    continue
                item = entry.get("item")
                name = entry.get("name") or (item.get("name")
                                             if isinstance(item, dict) else None)
                if name:
                    crumbs.append(name)
    return product or {}, crumbs


def _ld_number(value):
    """schema.org numbers arrive as floats, as strings, and as QuantitativeValue."""
    if isinstance(value, dict):
        value = value.get("value")
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None


def _ld_offers(node):
    """Flatten Offer / AggregateOffer / a list of either into offer dicts."""
    offers, queue = [], []
    raw = node.get("offers")
    queue.extend(raw if isinstance(raw, list) else ([raw] if raw else []))
    while queue:
        offer = queue.pop(0)
        if not isinstance(offer, dict):
            continue
        if _ld_type(offer) == "AggregateOffer":
            inner = offer.get("offers")
            if inner:
                queue.extend(inner if isinstance(inner, list) else [inner])
                continue
            offers.append({"price": _ld_number(offer.get("lowPrice")),
                           "high": _ld_number(offer.get("highPrice")),
                           "currency": offer.get("priceCurrency"),
                           "availability": offer.get("availability") or "",
                           "sku": offer.get("sku"), "node": offer})
        else:
            offers.append({"price": _ld_number(offer.get("price")),
                           "high": None,
                           "currency": offer.get("priceCurrency"),
                           "availability": offer.get("availability") or "",
                           "sku": offer.get("sku") or offer.get("gtin13"),
                           "node": offer})
    return offers


def _page_prices(values):
    """(sale prices, list prices) out of the elements carrying `content`."""
    sale, listed = [], []
    for entry in values or []:
        if not isinstance(entry, list) or len(entry) != 2:
            continue
        cls, content = entry
        try:
            n = float(str(content).replace(",", ""))
        except (TypeError, ValueError):
            continue
        if not 1 <= n <= 100000:
            continue
        if re.search(r"strike|\blist\b|was[- ]price|original|regular", cls, re.I):
            listed.append(n)
        elif re.search(r"\bsales?\b|\bprices?\b|\bvalue\b", cls, re.I):
            sale.append(n)
    return sale, listed


def _page_table_specs(tables):
    """Dims, volume and weight out of a spec table. Returns a dict."""
    for table in tables or []:
        rows = [r for r in table if r]
        if len(rows) < 2:
            continue
        header = [c.strip() for c in rows[0]]
        axis_cols = [i for i, c in enumerate(header) if PAGE_TABLE_AXIS.search(c)]
        if len(axis_cols) < 2:
            continue
        weight_cols = [i for i, c in enumerate(header) if PAGE_TABLE_WEIGHT.search(c)]
        body = rows[1]
        cells = [body[i] if i < len(body) else "" for i in range(len(header))]

        # The unit is usually printed once, on whichever axis cell the theme
        # felt like — Mammut writes `7.00  14.00  26.50 cm`.
        unit = ""
        for i in axis_cols:
            m = re.search(r"(cm|mm|\"|”|in\b|inch)", cells[i], re.I)
            if m:
                unit = m.group(1).lower()
                break
        values = []
        for i in axis_cols:
            m = re.search(r"(\d{1,3}(?:[.,]\d+)?)", cells[i])
            if m:
                values.append(float(m.group(1).replace(",", ".")))
        if len(values) < 2:
            continue
        if unit == "mm":
            values = [v / 10.0 for v in values]
        elif unit in ('"', "”", "in", "inch"):
            values = [v * CM_PER_IN for v in values]
        values = [round(v, 1) for v in values[:3]]
        if not all(2 <= v <= 120 for v in values):
            continue

        out = {"dims_cm": sorted(values, reverse=True), "dims_source": "table"}
        for i in weight_cols:
            grams = find_weight_g(cells[i])
            if grams:
                out["weight_g"] = grams
                break
        for i, cell in enumerate(cells):
            if i in axis_cols or i in weight_cols:
                continue
            litres = find_volume(cell)
            if litres:
                out["volume_l"] = litres
                break
        return out
    return {}


# Where a page states a fact in prose, it labels it: "Volume: 75 l",
# "Weight: 2,25 kg", "Dimensions & Weight 46 cm x 25 cm x 25 cm 750 g". Anchor
# on the label and read only what follows it — a bare sweep over forty
# kilobytes of page text will find a number shaped like an answer somewhere,
# which is how an index ends up publishing a related product's capacity.

# Nav thumbnails, payment badges and logos, which every storefront serves
# before it serves the product shot.
PAGE_IMAGE_NOISE = re.compile(
    r"/navigation/|/nav/|logo|sprite|icon|placeholder|badge|flag|payment|"
    r"social|swatch|\.svg(?:$|[?#])", re.I)


PAGE_LABEL = {
    "dims": re.compile(r"\b(?:dimensions?|ma(?:ß|ss)e|abmessungen?)\b\s*[:\-]?", re.I),
    "volume": re.compile(r"\b(?:volume|volumen|capacity|kapazit\w*)\b\s*[:\-]?", re.I),
    "weight": re.compile(r"\b(?:weights?|gewicht)\b\s*[:\-]?", re.I),
}


def page_near_label(text, key, finder, span=140):
    for m in PAGE_LABEL[key].finditer(text or ""):
        value = finder(text[m.end():m.end() + span])
        if value:
            return value
    return None


def _page_windows(text, desc):
    """
    Text slices to read specs out of when nothing is labelled: whatever
    follows a "Dimensions & Weight"-style heading, then the description.

    Deliberately not the whole page. Forty kilobytes of nav, footer, related
    products and sustainability copy always contains *a* number next to *a*
    unit, and sweeping it gave a Deuter pencil case a 10-litre capacity and a
    16-litre running pack a 3-litre one — both from text about something else
    entirely. A null is worth more than either.
    """
    windows = []
    for m in PAGE_SPEC_HEADING.finditer(text or ""):
        windows.append(text[m.start():m.start() + 600])
        if len(windows) >= 3:
            break
    if desc:
        windows.append(desc)
    return windows


def build_pages(item, brand, hint=None, force=None):
    url = item.get("url") or ""
    meta = item.get("meta") or {}
    product, crumbs = _ld_product(item.get("jsonld"))

    title = strip_html(product.get("name") or item.get("h1") or "")
    if not title:
        # og:title and <title> both carry the site name; the product's own
        # name is whatever comes before the separator.
        title = re.split(r"\s+[|–—-]\s+",
                         meta.get("og:title") or item.get("title") or "")[0].strip()
    if not title:
        return None, "no-title"

    crumb_text = " ".join(crumbs)
    if force:
        category, cat_src = force, "override"
    else:
        if NOT_A_BAG.search(title):
            return None, "not-a-bag"
        if (NOT_A_BAG_TYPE.search(crumb_text) and not BAGGISH_TYPE.search(crumb_text)
                and not classify(title, "", [])[0]):
            return None, "not-a-bag:breadcrumb"
        category, cat_src = classify(title, crumb_text, [])
        # The shelf the store hangs it on, from fetch.py's shelf pass. Same
        # standing as a Shopify collection and the same precedence: the
        # product's own title wins, the brand's shelving comes next, and for
        # Mammut — whose bags are called "Trion 38" and nothing else — it is
        # the only signal there is.
        if not category and item.get("shelf"):
            category, cat_src = item["shelf"], "shelf"
        if not category:
            return None, ("unclassified:known-bag" if item.get("shelved")
                          else "unclassified")

    desc = strip_html(product.get("description") or meta.get("og:description")
                      or meta.get("description") or "")
    text = item.get("text") or ""
    pairs = {norm_key(k): v for k, v in (item.get("pairs") or []) if k}
    blob = f"{title}\n{crumb_text}\n{desc}"

    # Specs. The store's own label/value markup first — a row headed
    # "Dimensions" is the brand answering the question, not us finding a
    # number that looks like an answer.
    dims = dims_src = volume = vol_src = weight = w_src = None
    for label, value in pairs.items():
        if dims is None and re.search(r"dimension|ma(?:ß|ss)e|abmessung", label):
            dims, src = find_dims_cm(value, slash=True)
            # find_dims_cm names the shape it matched with a `description:`
            # prefix; the shape is the useful half, the provenance is `pairs`.
            dims_src = f"pairs:{src.split(':')[-1]}" if dims else None
        if volume is None and re.search(r"volume|capacity|volumen", label):
            volume = find_volume_bag(value)
            vol_src = "pairs" if volume else None
        if weight is None and re.search(r"weight|gewicht", label):
            weight = find_weight_g(value)
            w_src = "pairs" if weight else None

    table = _page_table_specs(item.get("tables"))
    if dims is None and table.get("dims_cm"):
        dims, dims_src = table["dims_cm"], "table"
    if volume is None and table.get("volume_l"):
        volume, vol_src = table["volume_l"], "table"
    if weight is None and table.get("weight_g"):
        weight, w_src = table["weight_g"], "table"

    # schema.org's own physical properties, which Deuter fills in.
    if dims is None:
        axes = [_ld_number(product.get(k))
                for k in ("height", "width", "depth", "length")]
        axes = [round(v, 1) for v in axes if v and 2 <= v <= 120]
        if len(axes) >= 2:
            dims, dims_src = sorted(axes, reverse=True)[:3], "schema.org:axes"
    if weight is None:
        weight = find_weight_g(str(product.get("weight") or "")) or None
        w_src = "schema.org:weight" if weight else None

    # The name, where it states a unit: CamelBak's "Arete Sling 8, 0.6L". The
    # bare number in "SnoBlast 22" is left alone — it is a litre count by
    # convention and by nothing the page actually says.
    if volume is None:
        volume = find_volume_bag(title)
        vol_src = "title" if volume else None

    # Labelled prose, which is how Deuter, CamelBak and Tatonka all publish.
    if dims is None:
        found = page_near_label(text, "dims",
                                 lambda s: find_dims_cm(s, slash=True)[0])
        if found:
            dims, dims_src = found, "label"
    if volume is None:
        volume = page_near_label(text, "volume", find_volume_bag)
        vol_src = "label" if volume else None
    if weight is None:
        weight = page_near_label(
            text, "weight", lambda s: find_weight_labelled(s) or find_weight_g(s))
        w_src = "label" if weight else None

    for window in _page_windows(text, desc):
        source = "description" if window is desc else "spec-block"
        if dims is None:
            dims, src = find_dims_cm(window, slash=True)
            dims_src = f"{source}:{src}" if dims else None
        if volume is None:
            volume = find_volume_bag(window)
            vol_src = source if volume else None
        if weight is None:
            weight = find_weight_labelled(window) or find_weight_g(window)
            w_src = source if weight else None
        if dims and volume and weight:
            break

    offers = _ld_offers(product)
    prices = [o["price"] for o in offers if o.get("price")]
    prices += [o["high"] for o in offers if o.get("high")]
    sale, listed = _page_prices(item.get("values"))
    if not prices:
        prices = sale
    available = (any("InStock" in (o.get("availability") or "")
                     or "LimitedAvailability" in (o.get("availability") or "")
                     for o in offers)
                 if offers else bool(prices))

    variants = []
    for node in (product.get("hasVariant") or []):
        if not isinstance(node, dict):
            continue
        colour = node.get("color")
        if isinstance(colour, list):
            colour = colour[0] if colour else None
        colour = (colour or "").replace("-", " ").title() or None
        node_offers = _ld_offers(node)
        price = next((o["price"] for o in node_offers if o.get("price")), None)
        variants.append({
            "sku": node.get("sku") or node.get("gtin13"),
            "title": strip_html(node.get("name") or "") or None,
            "color": colour,
            "color_family": colour_family(colour, brand["slug"]),
            "size": None,
            "price": round(price, 2) if price else None,
            "compare_at": None,
            "available": any("InStock" in (o.get("availability") or "")
                             for o in node_offers),
            "grams": find_weight_g(str(node.get("weight") or "")) or None,
            "barcode": node.get("gtin13"),
            "image": None,
        })
        if price:
            prices.append(price)
    if not variants:
        for offer in offers:
            node = offer.get("node") or {}
            offered = node.get("itemOffered") or {}
            colour = offered.get("color") if isinstance(offered, dict) else None
            if isinstance(colour, list):
                colour = colour[0] if colour else None
            variants.append({
                "sku": offer.get("sku") or (offered or {}).get("model"),
                "title": strip_html((offered or {}).get("name") or "") or None,
                "color": colour,
                "color_family": colour_family(colour, brand["slug"]),
                "size": None,
                "price": round(offer["price"], 2) if offer.get("price") else None,
                "compare_at": None,
                "available": "InStock" in (offer.get("availability") or ""),
                "grams": None,
                "image": None,
            })
    if not variants:
        # No schema.org at all — CamelBak. The page still states a price and
        # a name, which is one buyable thing.
        variants.append({
            "sku": None, "title": None, "color": None, "color_family": None,
            "size": None,
            "price": round(min(sale), 2) if sale else None,
            "compare_at": (round(max(listed), 2)
                           if listed and sale and max(listed) > min(sale) else None),
            "available": available,
            "grams": None, "image": None,
        })

    image = product.get("image") or meta.get("og:image")
    if isinstance(image, list):
        image = image[0] if image else None
    if isinstance(image, dict):
        image = image.get("url") or image.get("contentUrl")
    if not image:
        candidates = [src for src in (item.get("images") or [])
                      if not PAGE_IMAGE_NOISE.search(src)]
        image = next((src for src in candidates
                      if re.search(r"product|catalog|media", src, re.I)),
                     candidates[0] if candidates else None)
    # Unlike a storefront API, a page hands back whatever the markup says, and
    # Thule's <img src> is site-root-relative ("/-/p/…"). Left alone it is
    # resolved against carrynerd.com by the browser and 74 photos 404.
    if image:
        image = urljoin(url, image)

    colors = [v["color"] for v in variants if v.get("color")]
    handle = re.sub(r"\.html?$", "", url.rstrip("/").rsplit("/", 1)[-1])
    return {
        "id": f"{brand['slug']}__{slugify(handle) or slugify(title)}",
        "brand": brand["name"],
        "brand_slug": brand["slug"],
        "name": title,
        "category": category,
        "category_source": cat_src,
        "url": url,
        "image": image,
        "volume_l": volume,
        "volume_source": f"pages:{vol_src}" if vol_src else None,
        "dims_cm": dims,
        "dims_source": f"pages:{dims_src}" if dims_src else None,
        "linear_cm": round(sum(dims), 1) if dims else None,
        "weight_g": weight,
        "weight_source": f"pages:{w_src}" if w_src else None,
        "laptop_in": find_laptop_in(f"{blob}\n{text[:4000]}"),
        "price_min": round(min(prices), 2) if prices else None,
        "price_max": round(max(prices), 2) if prices else None,
        "on_sale": bool(listed and sale and max(listed) > min(sale)),
        "in_stock": available or any(v["available"] for v in variants),
        "colors": sorted(set(colors), key=colors.index),
        "sizes": [],
        "variant_count": len(variants),
        "variants": variants,
        "color_families": sorted({f for f in (colour_family(c, brand["slug"])
                                              for c in colors) if f}),
        "materials": sorted(set(detect(blob, MATERIALS))),
        "features": detect(f"{blob}\n{text[:8000]}", FEATURES),
        "tags": [],
        "updated_at": None,
        "source": "pages:crawl",
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


# Above this many SKUs on one model, a variant list is a cross-product rather
# than a list of things a reader chooses between. Zpacks sells the Arc Haul in
# six colours and prices every combination of torso length, belt size and
# shoulder strap separately: 241 SKUs, six colours.
COLLAPSE_ABOVE = 25


def collapse_variants(bags):
    """Store the axes, not their cross-product.

    A model with 241 SKUs and six colours is not offering 241 choices. It is
    offering six colours and a fitting, and the 241 rows are every combination
    of the two written out. Carrying them costs on three fronts: they are 31%
    of the catalogue JSON, they ship to the browser in the browse payload, and
    track_prices.py writes a ledger row and a state entry *per variant*, so the
    append-only history grows by the cross-product every night, permanently.

    So keep one variant per distinct colourway — which is what the drawer
    shows and what a reader picks — and record the other axes as the lists they
    are. Nothing a page displays is lost; what goes is the enumeration.

    Keyed on colour *and* price together, which is what keeps it safe without
    knowing the brand. Requiring a single price first seemed like the careful
    choice and turned out to reject exactly the cases worth collapsing: the Arc
    Haul Ultra 60L carries 241 SKUs across 21 colours at 2 prices, so a
    uniform-price rule skipped it for having two. Keying on the pair keeps
    every distinct price — 241 rows become 39 — and where price really does
    vary per SKU, the pairs are the SKUs and nothing collapses, which is the
    correct outcome rather than a special case.
    """
    collapsed = 0
    for bag in bags:
        vs = bag.get("variants") or []
        if len(vs) <= COLLAPSE_ABOVE:
            continue

        # First occurrence wins, so the kept row is the one whose image and sku
        # the page would already have shown. Availability is OR'd across the
        # group: the colour is buyable if any fitting of it is, which is what
        # "in stock" means on a page that no longer lists every fitting.
        groups = {}
        for v in vs:
            key = (v.get("color") or v.get("color_family") or v.get("title"),
                   v.get("price"))
            if key in groups:
                if v.get("available"):
                    groups[key]["available"] = True
            else:
                groups[key] = dict(v)
        kept = list(groups.values())
        if len(kept) >= len(vs):
            continue

        # The axes that were being enumerated, so the page can still say a
        # model comes in five torso lengths without carrying a row per length.
        axes = {}
        for v in vs:
            for name, val in (v.get("options") or {}).items():
                if val:
                    axes.setdefault(name, []).append(val)
        bag["variant_axes"] = {k: sorted(set(v)) for k, v in axes.items()}
        bag["variants_collapsed_from"] = len(vs)
        bag["variants"] = kept
        # variant_count keeps saying how many SKUs exist, because that is true
        # and the page reports it. Only the enumeration is gone.
        collapsed += 1
    return collapsed


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
            | {f for f in (colour_family(c, out["brand_slug"])
                           for c in out["colors"]) if f})
        out["features"] = list(dict.fromkeys(f for b in group for f in b["features"]))
        out["tags"] = list(dict.fromkeys(t for b in group for t in b["tags"]))
        # A brand that publishes one product per colourway — Able Carry ships
        # 13 of the Stash Pouch — has its photography spread across the whole
        # group, so taking the gallery off the canonical record alone would
        # throw away every other colour's shots at the moment of merging.
        # Deduped on src, first occurrence wins the ordering.
        out["images"] = list({im["src"]: im for b in group
                              for im in (b.get("images") or [])
                              if im.get("src")}.values())
        out["axis_names"] = list(dict.fromkeys(
            n for b in group for n in (b.get("axis_names") or [])))
        # Every source id the merge swallowed, so price history written against
        # a colourway product still resolves after it stops existing on its own.
        out["merged_ids"] = [b["id"] for b in group]

        prices = [v["price"] for v in out["variants"] if v["price"]]
        if not prices:
            # Some sources price the model rather than the colourway: a
            # schema.org ProductGroup lists its variants with no offer on any
            # of them. Recomputing purely from variants dropped the price off a
            # third of Deuter's models, every one of which was priced correctly
            # until the moment it was merged.
            prices = [p for b in group
                      for p in (b.get("price_min"), b.get("price_max")) if p]
        out["price_min"] = round(min(prices), 2) if prices else None
        out["price_max"] = round(max(prices), 2) if prices else None
        out["in_stock"] = any(b["in_stock"] for b in group)
        out["on_sale"] = any(b["on_sale"] for b in group)
        out["linear_cm"] = round(sum(out["dims_cm"]), 1) if out.get("dims_cm") else None
        out["merged_from"] = len(group)
        out["merged_urls"] = [b["url"] for b in group]
        merged.append(out)

    return merged


# Keys a storefront states its currency under, across every source we read.
# WooCommerce says `currency_code`, Magento and Bellroy say `currency`,
# schema.org says `priceCurrency`. Shopify's /products.json says nothing at
# all, which is the entire reason this module exists.
CURRENCY_KEYS = {"currency", "currency_code", "pricecurrency", "currencycode"}
# ISO 4217 is three letters. Guards against picking up a `currency: {...}`
# object or a display string like "US Dollar".
CURRENCY_RE = re.compile(r"^[A-Za-z]{3}$")


def walk_currency(node, out, depth=0):
    """Collect every currency code stated anywhere in a product record."""
    if depth > 8:
        return
    if isinstance(node, list):
        for n in node:
            walk_currency(n, out, depth + 1)
    elif isinstance(node, dict):
        for key, value in node.items():
            if (key.lower() in CURRENCY_KEYS and isinstance(value, str)
                    and CURRENCY_RE.match(value)):
                out[value.upper()] += 1
            elif isinstance(value, (dict, list)):
                walk_currency(value, out, depth + 1)


# A market path is a statement about which storefront we are pricing against,
# and the region subtag is what settles the currency: /en-us/ is the US market
# and the US market bills dollars. Only regions we might plausibly fetch are
# here, and an unrecognised one deliberately falls through to the next signal
# rather than guessing — a wrong guess here is the exact failure being fixed.
LOCALE_CURRENCY = {
    "us": "USD", "gb": "GBP", "uk": "GBP", "ca": "CAD", "au": "AUD",
    "nz": "NZD", "jp": "JPY", "ch": "CHF", "se": "SEK", "no": "NOK",
    "dk": "DKK", "pl": "PLN", "cz": "CZK", "hk": "HKD", "sg": "SGD",
    **{c: "EUR" for c in ("de", "fr", "it", "es", "nl", "be", "at", "ie",
                          "fi", "pt", "gr", "sk", "si", "ee", "lv", "lt",
                          "lu", "cy", "mt", "eu")},
}


def locale_currency(locale):
    """USD from "en-us". None when the region is unknown or absent."""
    if not locale:
        return None
    parts = re.split(r"[-_]", locale.strip("/").lower())
    return LOCALE_CURRENCY.get(parts[-1]) if len(parts) > 1 else None


def observed_currency(payload, slug, page_currencies, declared):
    """What currency does this brand actually price in, and how do we know?

    Five of the six sources state it outright and we were throwing it away.
    Reading it turned up four brands published as dollars that never were —
    Vaude and Deuter and Tatonka in euros, Rab in pounds — on top of the two
    found by hand. The evidence had been sitting in raw/ the whole time.

    Shopify is the exception and the majority: /products.json carries no
    currency field anywhere, so for those brands the best available evidence is
    the product page, whose schema.org offers block states priceCurrency.
    enrich.py has been parsing and caching that all along.

    Order is strongest evidence first. A source's own statement beats a page
    scrape, which beats a human's note in the roster, which beats the
    assumption. Whatever wins is recorded on every bag as `currency_source`,
    the same way volume and dimensions carry theirs — the point of this index
    is that you can always ask a number where it came from.
    """
    counts = collections.Counter()
    # A sample, not the whole catalogue: currency is a property of the store,
    # so 25 products settle it and 1,400 only cost time.
    for product in (payload.get("products") or [])[:25]:
        walk_currency(product, counts)
    if counts:
        return counts.most_common(1)[0][0], "feed"

    # Above the page scrape on purpose. When a locale is configured we are
    # deliberately fetching a specific market, and enrich.py's cache may still
    # hold pages crawled at the old URL — Db's cached entries say NOK because
    # that is what its pages said before the /en-us/ switch. Trusting the stale
    # scrape over the market we are actually pulling would undo the fix.
    from_locale = locale_currency(payload.get("locale"))
    if from_locale:
        return from_locale, "locale"

    if page_currencies:
        # Majority across the brand's cached pages. Not unanimity: a single
        # page can carry a stray offer in another currency (a marketplace
        # widget, a pre-order in the brand's home market) without that being
        # what the store charges.
        return page_currencies.most_common(1)[0][0], "product-page"

    if declared:
        return declared.upper(), "roster"

    # Not "we checked and it is dollars" — "nothing said otherwise". The
    # currency gate in validate.py exists to catch exactly this case going
    # wrong, because it is the only one with no evidence under it.
    return "USD", "assumed"


def main():
    files = sorted(glob.glob(os.path.join(RAW, "*.json")))
    if not files:
        sys.exit("no raw catalogs — run fetch.py first")

    # What currency a storefront quotes in, declared per brand.
    #
    # No feed states it. Shopify's /products.json, WooCommerce's Store API and
    # the rest all emit bare numbers, so a Danish store selling in DKK is
    # byte-identical to one selling in USD and the index read both as dollars —
    # which is how Db, Mismo and Bedouin Foundry ended up published at the
    # wrong price with nothing to catch it.
    #
    # There are two honest answers to that and the roster carries both. Where
    # the brand runs a US market, `locale` points fetch.py at it and the prices
    # arrive in dollars for real (see Db). Where it does not, or where the
    # native price is the true one, `currency` says so and the number is shown
    # as what it is — £340, not $340. Nothing is ever converted: a rate would
    # be a number we made up, it would drift between crawls, and it would make
    # the price ledger a record of the exchange rate rather than of the seller.
    #
    # Read from brands.json rather than the raw payload deliberately. This is
    # curation, not crawl output, so it takes effect on the next normalize
    # instead of waiting for the brand's turn in the crawl. It is the *fallback*
    # — see observed_currency(), which prefers what the source itself says.
    declared = {}
    try:
        with open(os.path.join(HERE, "brands.json")) as f:
            declared = {b["slug"]: (b.get("currency") or "").upper()
                        for b in json.load(f)}
    except (OSError, json.JSONDecodeError, TypeError, KeyError) as e:
        sys.exit(f"brands.json unreadable ({e}) — refusing to normalize, "
                 f"because every price would silently default to USD")

    # Currencies enrich.py already read off product pages, folded per brand.
    # Absent on a first run, which is fine: the roster and the gate cover it.
    page_currencies = collections.defaultdict(collections.Counter)
    try:
        with open(ENRICH_CACHE) as f:
            for bag_id, entry in json.load(f).items():
                code = (entry or {}).get("currency")
                if isinstance(code, str) and CURRENCY_RE.match(code):
                    page_currencies[bag_id.split("__", 1)[0]][code.upper()] += 1
    except (OSError, json.JSONDecodeError, AttributeError):
        pass

    currency_sources = collections.Counter()

    bags, rejects, quarantine = [], {}, []
    unreadable = []
    direct_brand_names = set()
    for path in files:
        # A raw catalogue that will not parse is one brand's problem; it used to
        # be the whole run's. The traceback that taught this was a merge in
        # `assemble` writing one shard's copy of a brand over a longer one
        # without truncating, leaving a valid document with 16 stray bytes after
        # it — and normalize.py died on the third file of 116, having produced
        # nothing, on a night whose crawl was entirely fine.
        #
        # Skipping is safe because it is not silent twice over: the brand's
        # absence lands in the coverage numbers validate.py gates on, so a hole
        # big enough to matter still stops the publish, and the file is named in
        # meta and shouted at the end of the run.
        try:
            with open(path) as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"  unreadable {os.path.basename(path)} ({e}) — skipping "
                  f"this brand", file=sys.stderr, flush=True)
            unreadable.append({"file": os.path.basename(path), "error": str(e)})
            continue
        code, source = observed_currency(
            payload, payload["slug"],
            page_currencies.get(payload["slug"]),
            declared.get(payload["slug"]))
        currency_sources[source] += 1
        brand = {
            "slug": payload["slug"], "name": payload["name"],
            "domain": payload["domain"], "fetched_at": payload.get("fetched_at"),
            "currency": code, "currency_source": source,
            # The market prefix fetch.py pulled the feed under, if any. Carried
            # so product URLs land on the same storefront the prices came from:
            # linking a reader from a USD listing to the kroner page would be
            # its own version of the bug this is all about, and it would also
            # feed enrich.py the wrong page to read a currency off.
            "locale": (payload.get("locale") or "").strip("/"),
        }
        platform = payload.get("platform") or "shopify"
        if platform != "campsaver":
            direct_brand_names.add(brand["name"].lower())

        # Optional — produced by `fetch.py --collections`. Absent for brands
        # whose shelving has not been crawled, and the keyword path still works.
        hints = {}
        hint_path = os.path.join(COLLECTIONS, f"{payload['slug']}.json")
        if platform == "shopify" and os.path.exists(hint_path):
            try:
                with open(hint_path) as f:
                    hints = json.load(f).get("products", {})
            except (json.JSONDecodeError, KeyError):
                hints = {}

        if platform == "bellroy":
            items, builder = group_bellroy(payload.get("products", [])), build_bellroy
        elif platform == "campsaver":
            items, builder = payload.get("products", []), build_campsaver
        elif platform == "woocommerce":
            items, builder = payload.get("products", []), build_woocommerce
        elif platform == "magento":
            items, builder = payload.get("products", []), build_magento
        elif platform == "pages":
            items, builder = payload.get("products", []), build_pages
        else:
            items, builder = payload.get("products", []), build_shopify

        def describe(product):
            """(title, type, tags, url) for the quarantine row, per shape."""
            if platform == "bellroy":
                a = product[0]
                d2 = a.get("dimensions") or {}
                return (a.get("name"),
                        _bellroy_val(d2, "filter_sub_category"), [],
                        f"https://{brand['domain']}"
                        f"{(a.get('canonical_uri') or '').split('?')[0]}")
            if platform == "campsaver":
                ld = product.get("product") or {}
                return (ld.get("name"),
                        " > ".join(product.get("breadcrumbs") or []) or None,
                        [], product.get("url"))
            if platform == "magento":
                return (strip_html(product.get("name") or ""),
                        ", ".join(c.get("name") or ""
                                  for c in product.get("categories") or [])
                        or None,
                        [], f"https://{brand['domain']}/"
                            f"{product.get('url_key') or ''}"
                            f"{product.get('url_suffix') or ''}")
            if platform == "pages":
                ld, crumbs = _ld_product(product.get("jsonld"))
                return (strip_html(ld.get("name") or product.get("h1") or ""),
                        " > ".join(crumbs) or None, [], product.get("url"))
            if platform == "woocommerce":
                return (strip_html(product.get("name") or ""),
                        ", ".join(c.get("name") or ""
                                  for c in product.get("categories") or [])
                        or None,
                        [t.get("name") for t in (product.get("tags") or [])][:6],
                        product.get("permalink"))
            return (product.get("title"),
                    product.get("product_type") or None,
                    (product.get("tags") or [])[:6],
                    f"https://{brand['domain']}"
                    f"{'/' + brand['locale'] if brand.get('locale') else ''}"
                    f"/products/{product.get('handle') or ''}")

        for product in items:
            hint = (hints.get(str(product.get("id")))
                    if platform == "shopify" else None)
            # Described up front rather than only on rejection: the product's
            # URL is what a ruling is keyed on, and a ruling has to be consulted
            # before the builder decides whether to admit it.
            title, ptype, tags, url = describe(product)
            ruling = CATEGORY_OVERRIDES.decide(brand["slug"], product_key(url))
            bag, why = builder(product, brand, hint,
                               force=None if ruling is MISSING else ruling)
            if bag:
                # Stamped here rather than in each of the six builders: it is a
                # property of the storefront the product came from, identical
                # for every product in the file, and one line that covers all
                # adapters cannot fall out of step the way six copies would.
                bag["currency"] = brand["currency"]
                bag["currency_source"] = brand["currency_source"]
                # Which storefront quoted the number, carried for the same
                # reason the currency is. A price only means something against
                # the market that charged it, and the market can change under a
                # stable currency code: Db's feed moved from the bare path to
                # /en-us and went 2,500 to 259 with "USD" on both sides of the
                # move. track_prices.py compares against this, so a change of
                # market re-baselines instead of being published as a 90% sale.
                bag["price_market"] = brand["locale"] or "default"
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
                    "title": title,
                    "product_type": ptype,
                    "tags": tags,
                    "url": url,
                    # A ruling of `null` says someone looked and agreed it does
                    # not belong. Carried through so the audit list can show
                    # what is still waiting on a human rather than everything
                    # that was ever dropped.
                    "reviewed": ruling is None,
                })

    # Aggregator entries only fill brands with no direct source. A retailer's
    # listing of a brand fetched first-hand would shadow it — same model,
    # thinner specs, a retailer's price — so direct always wins.
    kept = []
    for bag in bags:
        if (bag["source"].startswith("campsaver:")
                and bag["brand"].lower() in direct_brand_names):
            rejects["aggregator:direct-source-exists"] = \
                rejects.get("aggregator:direct-source-exists", 0) + 1
        else:
            kept.append(bag)
    bags = kept

    before = len(bags)
    bags = merge_models(bags)
    collapsed = collapse_variants(bags)
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
        "generated_from": "public storefront sources: Shopify products.json, "
                          "WooCommerce Store API, Magento storefront GraphQL, "
                          "the Bellroy products API, and published product pages",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "bag_count": len(bags),
        "brand_count": len({b["brand_slug"] for b in bags}),
        "sku_count": sum(b["variant_count"] for b in bags),
        "categories": sorted({b["category"] for b in bags}),
        "products_merged": before - len(bags),
        "models_collapsed": collapsed,
        # Both halves of the currency picture, because either one alone is
        # misleading. The mix says what is actually being published; the
        # provenance says how much of it we know versus assume, and "assumed"
        # trending up is the early warning that a source stopped stating it.
        "currencies": dict(collections.Counter(
            b["currency"] for b in bags).most_common()),
        "currency_sources": dict(currency_sources.most_common()),
        "rejected": rejects,
        # Empty on every healthy night. Non-empty means a brand is missing from
        # this build for a reason that has nothing to do with the brand, which
        # is worth a line in the published metadata rather than only in a log
        # that ages out of Actions in ninety days.
        "unreadable_catalogues": unreadable,
        "overrides": {
            "colour": COLOUR_OVERRIDES.report(),
            "category": CATEGORY_OVERRIDES.report(),
        },
        "coverage": {f: coverage(f) for f in
                     ("volume_l", "dims_cm", "weight_g", "laptop_in", "price_min")},
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    # Indented because this file is committed nightly and the whole argument
    # for keeping data in git is that the diff tells you what changed. On one
    # line it cannot: a night that moved 60 prices shows as +1/-1, the entire
    # 23 MB rewritten, and `git log -p` is useless for the one question worth
    # asking it. Indented, that night is a 60-line diff you can read.
    #
    # The size objection does not survive measurement. The file grows 25%, but
    # git's delta compression is binary and the added whitespace all but
    # disappears: two nightly commits pack to 2.27 MB flat against 2.44 MB
    # indented, 1.07x, for history that is legible instead of opaque.
    #
    # Field order is left alone rather than sorted — the builders construct it
    # the same way every run, so it is already deterministic, and id/brand/name
    # first is worth more when reading a diff than alphabetical would be.
    with open(OUT, "w") as f:
        json.dump({"meta": meta, "bags": bags}, f, indent=1)

    quarantine.sort(key=lambda r: (r["reason"], r["brand_slug"], r["title"] or ""))
    with open(REJECTS, "w") as f:
        json.dump({"generated_at": meta["generated_at"],
                   "counts": rejects, "rejected": quarantine}, f, indent=1)

    print(json.dumps(meta, indent=2))
    print(f"\nwrote {OUT}")
    print(f"wrote {REJECTS} ({len(quarantine)} quarantined products)")

    # Loud for the same reason the gate exists: a brand that dropped out because
    # its file would not parse looks exactly like a brand that stopped selling
    # bags, and only one of those is a bug in this pipeline.
    if unreadable:
        print(f"\n{len(unreadable)} raw catalogue(s) would not parse and were "
              f"skipped — these brands are missing from this build:")
        for row in unreadable:
            print(f"  {row['file']}: {row['error']}")

    # Loud, because a stale ruling is invisible by nature: the product it was
    # about is gone, so nothing else in the output mentions it.
    for kind, report in meta["overrides"].items():
        if report["stale"]:
            print(f"\n{len(report['stale'])} stale {kind} override(s) in "
                  f"{report['file']} — nothing matched this run:")
            for row in report["stale"]:
                print(f"  {row['brand']}: {row['key']}")


if __name__ == "__main__":
    main()
