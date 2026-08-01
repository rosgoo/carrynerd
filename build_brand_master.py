#!/usr/bin/env python3
"""
Merge every brand mentioned across the carry communities into one list.

Each brand records which sources mentioned it, so a brand seen by three
independent communities can be prioritised over one seen once. Names are
canonicalised through an alias table — the same company appears as "TOM BIHN",
"Tom Bihn", "EVERGOODS" and "Evergoods" depending on who is writing.

Output: data/brands-master.json
"""

import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "brands-master.json")

# --- raw source lists -------------------------------------------------------

ONEBAG_SHEET = """3FULGear|5.11|Able Carry|Aer|Airback|Alchemy Equipment|Almond Oak|
Alpaka Gear|Alpha One-Niner|Alpine Sea|Arc'teryx|Arcido|Arktype|Attitude Supply|
Avio Gear|Away|Baboon to the Moon|Baggu|Bagsmart|Bellroy|Black Ember|Black Voyage|
Boundary Supply|Burton|Cabelas|Cabin Zero|Calpak|Caribee|Code of Bell|Corsurf|
Cotopaxi|Craghoppers|Dakine|Day Owl|Db Journey|Deuter|Eagle Creek|Eastpak|Ecohub|
Evergoods|Everyman|Exped|FHF Gear|Fjallraven|Forclaz|Fyro|GORUCK|Gossamer Gear|
Graphene-x|Greenroom|Gregory|Hanchor|Heimplanet|HikePro|Hill People Gear|Homiee|
Hynes Eagle|Hyperlite Mountain Gear|ILE|Ikea|Incase|Jansport|July|KS Ultralight|
Kathmandu|Kifaru|Knack Bags|L.L.Bean|Lowe|MEC|Mammut|Matador|Millican|Minaal|
Ministry of Supply|Mixed Works|Moment|Mountainsmith|Mystery Ranch|Nemo Equipment|
Nike|Nomatic|Northface|Ogio|Ortleib|Osprey|Outlander|Pacsafe|Pakt|Patagonia|
Peak Design|Piorama|Portland Gear|Quechua|REI Co-op|Rab|Rangeland|Red Oxx|
Remote Equipment|Rework Gear|Salkan|Salomon|Samsonite|Sherpani|Simond|
Six Moon Design|Solgaard|Stio|Stubble & Co|SwissGear|Tatonka|The Brown Buffalo|
Thule|Timbuk2|Tobiq|Tom Bihn|Topo Designs|Tortuga|Trakke|Tropicfeel|Troubadour|
ULA Equipment|Uniqlo|United by Blue|Vaude|Vertx|Wandrd|Waterfield|Wave|Waymark|
YNOT|Yeti|Zeeksack|Zorali|Zpacks|eBags|haize project|kipling|tomtoc"""

PACKHACKER = """Osprey|EVERGOODS|Tortuga|Topo Designs|TOM BIHN|The North Face|
ALPAKA|Cotopaxi|Matador|Fjallraven"""

# Union of three Carry Awards pages (8th annual carry-on, 8th annual sling,
# Carry Awards XI winners part 1).
CARRYOLOGY = """Tom Bihn|Matador|Black Ember|Snow Peak|Bellroy|Aer|Topo Designs|
Fuller Foundry|Gregory|Property Of|Osprey|PROSPECS|RAWROW|Inside Line Equipment|
Boundary Supply|Pakt|Mystery Ranch|Chrome|Grams|Packolab|Porter|Arc'Teryx|
EVERGOODS|Alpha One Niner|Wilboro|Db|DEFY|dyborg|Bedouin Foundry|WANDRD|
Modern Dayfarer|Peak Design|Hyperlite Mountain Gear|Code Of Bell|CamelBak|
côte&ciel|ANONYM CRAFTSMAN DESIGN|Fredrik Packers|Timbuk2|Mismo|Klättermusen|
The North Face|JanSport|GORUCK"""

HUCKBERRY = """Aer|Bellroy|Topo Designs|Filson"""

# Brands already wired up with a domain in the fetch pipeline.
SEEDED = {}
try:
    with open(os.path.join(HERE, "brands.json")) as f:
        SEEDED = {b["name"]: b["domain"] for b in json.load(f)}
except (OSError, json.JSONDecodeError):
    pass

# Pack Hacker reviews non-bag gear too; these appeared on the same page and are
# not carry brands.
NOT_CARRY_BRANDS = {"logitech", "olight", "owala", "dyson", "anker", "huckberry"}

# --- canonicalisation -------------------------------------------------------

ALIASES = {
    "tom bihn": "Tom Bihn", "evergoods": "EVERGOODS", "alpaka": "Alpaka",
    "alpaka gear": "Alpaka", "arc'teryx": "Arc'teryx", "northface": "The North Face",
    "the north face": "The North Face", "alpha one niner": "Alpha One-Niner",
    "alpha one-niner": "Alpha One-Niner", "db": "Db Journey",
    "db journey": "Db Journey", "côte&ciel": "Côte&Ciel", "coteetciel": "Côte&Ciel",
    "six moon design": "Six Moon Designs", "ortleib": "Ortlieb",
    "kipling": "Kipling", "tomtoc": "Tomtoc", "ebags": "eBags",
    "haize project": "Haize Project", "code of bell": "Code of Bell",
    "code ofbell": "Code of Bell", "jansport": "JanSport",
    "ile": "Inside Line Equipment", "inside line equipment": "Inside Line Equipment",
    "cabin zero": "CabinZero", "cabinzero": "CabinZero",
    "hyperlite mountain gear": "Hyperlite Mountain Gear",
    "l.l.bean": "L.L.Bean", "rei co-op": "REI Co-op", "5.11": "5.11 Tactical",
    "wandrd": "WANDRD", "prospecs": "PROSPECS", "defy": "DEFY",
    "anonym craftsman design": "ANONYM CRAFTSMAN DESIGN", "goruck": "GORUCK",
    "ula equipment": "ULA Equipment", "waterfield": "WaterField Designs",
    "calpak": "CALPAK", "chrome": "Chrome Industries",
    "chrome industries": "Chrome Industries", "brevite": "Brevite",
    "béis": "BÉIS", "beis": "BÉIS",
}


def canon(raw):
    name = re.sub(r"\s+", " ", raw).strip()
    return ALIASES.get(name.lower(), name)


def parse(blob):
    return [n.strip() for n in re.sub(r"\s*\n\s*", "", blob).split("|") if n.strip()]


def main():
    sources = {
        "onebag-sheet": parse(ONEBAG_SHEET),
        "packhacker": parse(PACKHACKER),
        "carryology": parse(CARRYOLOGY),
        "huckberry": parse(HUCKBERRY),
    }

    brands, dropped = {}, set()
    for source, names in sources.items():
        for raw in names:
            if raw.strip().lower() in NOT_CARRY_BRANDS:
                dropped.add(raw.strip())
                continue
            name = canon(raw)
            entry = brands.setdefault(name, {
                "name": name, "sources": [], "aliases": set(), "domain": None,
            })
            if source not in entry["sources"]:
                entry["sources"].append(source)
            if raw.strip() != name:
                entry["aliases"].add(raw.strip())

    for name, entry in brands.items():
        if name in SEEDED:
            entry["domain"] = SEEDED[name]
            entry["sources"].append("calipered-seed")
    for name, domain in SEEDED.items():
        if name not in brands:
            brands[name] = {"name": name, "sources": ["calipered-seed"],
                            "aliases": set(), "domain": domain}

    out = []
    for entry in brands.values():
        entry["aliases"] = sorted(entry["aliases"])
        entry["source_count"] = len(entry["sources"])
        out.append(entry)
    # Most-corroborated first: a brand three communities name is a better
    # crawl target than one that showed up once.
    out.sort(key=lambda b: (-b["source_count"], b["name"].lower()))

    payload = {
        "meta": {
            "brand_count": len(out),
            "sources": {
                "onebag-sheet": "r/onebag community spreadsheet (via qaya.cc)",
                "packhacker": "packhacker.com/travel-gear listing page",
                "carryology": "Carry Awards VIII + XI finalist/winner pages",
                "huckberry": "brand pages confirmed via search",
                "calipered-seed": "domains already wired into fetch.py",
            },
            "source_totals": {k: len(v) for k, v in sources.items()},
            "excluded_non_carry": sorted(dropped),
            "gaps": [
                "r/manybaggers not covered — reddit.com, old.reddit.com and the "
                "redlib mirrors all return 403 or a browser-verification "
                "challenge, which was not worked around. Needs the official "
                "Reddit API with an app credential.",
                "Pack Hacker list is one listing page, not their full brand "
                "index; their /brands/ path 404s.",
                "Huckberry blocked by Cloudflare (403); only brands confirmed "
                "via search are included, so its coverage is thin.",
                "Carryology is award finalists only, not their full tag index.",
            ],
        },
        "brands": out,
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    print(f"{len(out)} unique brands -> {OUT}")
    for n in range(max(b["source_count"] for b in out), 0, -1):
        hits = [b["name"] for b in out if b["source_count"] == n]
        if hits:
            print(f"\n{n} source(s) — {len(hits)}:\n  " + ", ".join(hits))


if __name__ == "__main__":
    main()
