#!/usr/bin/env python3
"""
carrynerd price tracker — the one asset that cannot be backfilled.

Specs can be re-scraped at any time; a price you did not record on the day it
changed is gone forever. So this runs after every normalize and records what
changed, starting from whenever you first run it.

It writes a *change log*, not a daily dump. A row is appended only when a
variant's price, sale price or stock state differs from the last recorded
state, which keeps the file proportional to how often things actually move
rather than to how often the crawler runs. Running this hourly and running it
weekly produce the same file size if nothing changed.

    data/price-history.jsonl   append-only, one JSON object per change
    data/price-state.json      last known state per SKU, for fast diffing

It also writes back onto data/bags.json so the site can surface drops:
`previous_price`, `price_changed_at`, `price_direction`, `lowest_ever`.

Usage:
    python3 track_prices.py              # record changes since last run
    python3 track_prices.py --dry-run    # show what would be recorded
    python3 track_prices.py --events-out data/price-events.json

`--events-out` writes just the changes from *this* run, joined against the bag
they belong to. That file is what the alert matcher consumes: it needs "what
moved tonight", and re-deriving that by tailing the ledger would mean trusting
a timestamp comparison to be exact. The ledger stays the durable record; this
is a hand-off, not an artifact, and the workflow throws it away afterwards.
"""

import argparse
import collections
import json
import os
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
BAGS = os.path.join(HERE, "data", "bags.json")
HISTORY = os.path.join(HERE, "data", "price-history.jsonl")
STATE = os.path.join(HERE, "data", "price-state.json")


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except json.JSONDecodeError:
        return default


def variant_key(bag, variant, index, ambiguous=()):
    """SKU when the brand supplies one, otherwise a stable positional key.

    A SKU earns the job only if it names one variant. Some storefronts ship a
    placeholder instead: Cotopaxi's Allpa 28L lists every colourway under
    `A28-CHOICE` at two prices, 190 and 205. Keyed on that, several variants
    collapse onto one row of state, whichever was walked last wins, and the
    recorded price alternates between them on every single run — an endless
    up/down that the site published as a standing 7% sale nobody was running.
    Twenty-three of the twenty-four bags with a colliding SKU were on the strip,
    out of 7,653.

    So a repeated SKU is not a SKU. Those variants fall back to position, which
    is what the ones with no SKU at all already use. `ambiguous` is passed in
    rather than derived here because it is a property of the whole variant list,
    and recomputing it per variant would make this quadratic on a 119-variant
    model.
    """
    sku = variant.get("sku")
    if sku and sku not in ambiguous:
        return f"{bag['id']}::{sku}"
    label = variant.get("color") or variant.get("title") or str(index)
    return f"{bag['id']}::#{index}:{label}"


def ambiguous_skus(variants):
    """SKUs that appear on more than one variant, and so identify none of them."""
    counts = collections.Counter(v.get("sku") for v in variants if v.get("sku"))
    return {sku for sku, n in counts.items() if n > 1}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--events-out", default="",
                    help="write this run's changes here for the alert matcher")
    ap.add_argument("--forget", default="",
                    help="comma-separated brand slugs whose price state to drop "
                         "before comparing, so the next observation baselines "
                         "instead of reading as a change. For when a brand's "
                         "prices were wrong rather than different and the delta "
                         "is an artefact of ours, not a move by the seller. A "
                         "change of currency or market no longer needs this — "
                         "it re-baselines on its own; this is for the ones only "
                         "a person can call, like a parser fix that leaves the "
                         "unit alone and changes the number")
    args = ap.parse_args()

    payload = load_json(BAGS, None)
    if not payload:
        raise SystemExit("no data/bags.json — run normalize.py first")

    bags = payload["bags"]
    state = load_json(STATE, {})
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # A price that was wrong is not a price that moved.
    #
    # The cost of getting this wrong is not hypothetical. Pointing Db's feed at
    # /en-us took 364 models from ~2,500 to ~259 in one run, and every one was
    # published as a ~91% discount: 364 rows in an append-only ledger recording
    # a sale no seller ever ran, and 550 events handed to alerts/match.py, which
    # would have mailed the biggest sale in the site's history on the night we
    # fixed a bug. It did not, only because DATABASE_URL was unset.
    #
    # The loop below now catches that class on its own — a price quoted in a
    # different currency or market re-baselines rather than subtracting. This
    # flag is what is left over: the corrections where the unit is unchanged and
    # only our reading of the number was wrong, which no rule can recognise
    # because the new number is a perfectly ordinary price. A parser fix is the
    # example. Somebody has to say so.
    #
    # Dropping the state makes the corrected price a `first_seen` baseline
    # instead of a delta. It forfeits those SKUs' recorded low, which is the
    # right trade: the low was never a price anyone was charged. History already
    # written stays written — this only governs what the *next* observation is
    # compared against.
    forget = {s.strip() for s in args.forget.split(",") if s.strip()}
    if forget:
        stale = [k for k in state if k.split("__", 1)[0] in forget]
        for key in stale:
            del state[key]
        print(f"forgot {len(stale)} SKU states across {len(forget)} brand(s): "
              f"{', '.join(sorted(forget))}")

    rows, changes, first_run, rebased = [], 0, not state, 0

    for bag in bags:
        # What the number is denominated in. Two prices are only comparable
        # when this matches, and it is not the currency code alone: Db's feed
        # moved from the bare path to /en-us and every model went from ~2,500
        # to ~259 with "USD" on both sides, because the old number was NOK that
        # nothing had labelled. Currency catches a relabel, market catches a
        # move between storefronts, and the pair catches both.
        basis = f"{bag.get('currency') or 'USD'}@{bag.get('price_market') or 'default'}"
        ambiguous = ambiguous_skus(bag.get("variants") or [])
        for i, variant in enumerate(bag.get("variants") or []):
            key = variant_key(bag, variant, i, ambiguous)
            current = {
                "price": variant.get("price"),
                "compare_at": variant.get("compare_at"),
                "available": bool(variant.get("available")),
                "basis": basis,
            }
            previous = state.get(key)

            # A price in a different unit is not a price that moved. Subtracting
            # 259 USD from 2,500 NOK is a category error, and publishing the
            # result called it the biggest sale in the site's history across 364
            # models. Drop the state and let the new number baseline, which is
            # what --forget does by hand for the case a person has to judge: a
            # parser fix, where the unit is unchanged and only we were wrong.
            # This is the case nobody needs to judge, so it does not wait for a
            # human to notice. The recorded low goes with it — it was in the old
            # unit and was never a price anyone was charged.
            #
            # Entries written before this field existed carry no basis. Those
            # are treated as matching rather than as a change, so introducing
            # this does not re-baseline all 30,459 tracked SKUs on the first run
            # and blank the site's drops for a night.
            was_rebased = bool(previous and previous.get("basis", basis) != basis)
            if was_rebased:
                previous = None
                del state[key]
                rebased += 1

            if previous and all(previous.get(f) == current[f]
                                for f in current if f != "basis"):
                # Carry the basis forward on an otherwise unchanged SKU, so a
                # pre-existing entry acquires one without being called a change.
                if previous.get("basis") != basis:
                    state[key] = {**previous, "basis": basis}
                continue

            row = {
                "ts": now,
                "bag_id": bag["id"],
                "brand": bag["brand"],
                "name": bag["name"],
                "sku": variant.get("sku"),
                "color": variant.get("color"),
                "price": current["price"],
                "compare_at": current["compare_at"],
                "available": current["available"],
                # A recorded price with no unit on it is what let 2,500 NOK and
                # 259 USD sit in the same series looking like a discount. Rows
                # written before this line lack it; from here the ledger says
                # what it is denominated in.
                "basis": basis,
            }
            # Said out loud, because otherwise a re-baseline is indistinguishable
            # from a first sighting: a SKU that has been tracked for months
            # suddenly appears with no previous price and nothing explains why.
            # Anyone reading the series later needs to know the break is ours
            # and not a gap in the crawl.
            if was_rebased:
                row["rebased"] = True
            if previous:
                row["prev_price"] = previous.get("price")
                if previous.get("price") and current["price"]:
                    delta = current["price"] - previous["price"]
                    row["delta"] = round(delta, 2)
                    row["direction"] = "down" if delta < 0 else "up"
                changes += 1

            rows.append(row)
            # `first_seen` and `low` survive across runs so "lowest ever" stays
            # meaningful even after many price moves.
            state[key] = {
                **current,
                "first_seen": (previous or {}).get("first_seen", now),
                "low": min([p for p in (current["price"],
                                        (previous or {}).get("low")) if p],
                           default=current["price"]),
                "last_change": now,
            }

    if args.dry_run:
        print(f"would record {len(rows)} rows ({changes} actual changes)")
        for row in rows[:10]:
            print(" ", json.dumps(row))
        return

    if rows:
        with open(HISTORY, "a") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
    with open(STATE, "w") as f:
        # sort_keys because this is committed nightly: unsorted, the key
        # order is whatever order SKUs were walked in, so every run
        # rewrites the whole file and the diff says nothing about what
        # actually changed. Sorted and indented, a night that moves 60
        # prices is a 60-line diff. Costs ~2.5% in size.
        json.dump(state, f, sort_keys=True, indent=1)

    # Annotate bags.json so the front end can show drops without parsing the
    # whole history file.
    annotated = 0
    for bag in bags:
        prices, lows, changed, direction, prev = [], [], None, None, None
        ambiguous = ambiguous_skus(bag.get("variants") or [])
        for i, variant in enumerate(bag.get("variants") or []):
            entry = state.get(variant_key(bag, variant, i, ambiguous))
            if not entry:
                continue
            if entry.get("price"):
                prices.append(entry["price"])
            if entry.get("low"):
                lows.append(entry["low"])
            if entry.get("last_change"):
                changed = max(changed or "", entry["last_change"])

        matching = [r for r in rows
                    if r["bag_id"] == bag["id"] and r.get("direction")]
        if matching:
            drop = min(matching, key=lambda r: r.get("delta", 0))
            direction = drop["direction"]
            prev = drop.get("prev_price")
            annotated += 1

        bag["lowest_ever"] = round(min(lows), 2) if lows else None
        bag["price_changed_at"] = changed
        bag["price_direction"] = direction
        bag["previous_price"] = prev
        bag["at_lowest"] = bool(
            prices and lows and round(min(prices), 2) <= round(min(lows), 2))

    # Written after annotation so events carry the freshly computed
    # `lowest_ever` — "cheapest it has ever been" is the line that makes an
    # alert email worth opening.
    if args.events_out:
        by_id = {bag["id"]: bag for bag in bags}
        events = []
        for row in rows:
            # Only rows with a `direction`. The first run baselines every SKU,
            # and a baseline is not a price movement.
            if not row.get("direction"):
                continue
            bag = by_id.get(row["bag_id"]) or {}
            events.append({
                **row,
                "brand_slug": bag.get("brand_slug"),
                # The permalink, carried rather than re-derived. `bag_id` holds
                # the *source* handle, which is a colourway for any merged
                # model — deriving a URL from it lands on a page that does not
                # exist. normalize.py owns this slug; everything else reads it.
                "slug": bag.get("slug"),
                "category": bag.get("category"),
                # Carried so the email can name the price correctly. Prices are
                # never converted, so a bare "$" on a brand that charges pounds
                # is a wrong number in someone's inbox.
                "currency": bag.get("currency") or "USD",
                "url": bag.get("url"),
                "image": bag.get("image"),
                "lowest_ever": bag.get("lowest_ever"),
                "at_lowest": bag.get("at_lowest"),
            })
        out_dir = os.path.dirname(os.path.abspath(args.events_out))
        os.makedirs(out_dir, exist_ok=True)
        with open(args.events_out, "w") as f:
            json.dump({"generated_at": now, "events": events}, f, indent=2)
        print(f"{len(events)} price/stock events -> {args.events_out}")

    payload["meta"]["price_tracking"] = {
        "history_file": "data/price-history.jsonl",
        "tracked_skus": len(state),
        "last_run": now,
        "rows_appended": len(rows),
        "changes_this_run": changes,
        "rebased_this_run": rebased,
    }
    with open(BAGS, "w") as f:
        json.dump(payload, f, separators=(",", ":"))

    if first_run:
        print(f"baseline established: {len(rows)} SKUs recorded at {now}")
        print("no changes to report yet — run again after the next fetch")
    else:
        print(f"{len(rows)} rows appended, {changes} price/stock changes, "
              f"{annotated} bags annotated")
    # Loud, because a re-baseline is a decision the tracker made on its own. A
    # handful is a brand switching storefront; thousands means a currency or
    # locale edit landed wider than whoever made it thought.
    if rebased:
        print(f"{rebased} SKU(s) re-baselined: the currency or market their "
              f"price is quoted in changed, so this run's number was recorded "
              f"as a new baseline rather than compared against the old one")
    print(f"tracking {len(state)} SKUs -> {HISTORY}")


if __name__ == "__main__":
    main()
