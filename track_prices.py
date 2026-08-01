#!/usr/bin/env python3
"""
bagdex price tracker — the one asset that cannot be backfilled.

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


def variant_key(bag, variant, index):
    """SKU when the brand supplies one, otherwise a stable positional key."""
    if variant.get("sku"):
        return f"{bag['id']}::{variant['sku']}"
    label = variant.get("color") or variant.get("title") or str(index)
    return f"{bag['id']}::#{index}:{label}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--events-out", default="",
                    help="write this run's changes here for the alert matcher")
    args = ap.parse_args()

    payload = load_json(BAGS, None)
    if not payload:
        raise SystemExit("no data/bags.json — run normalize.py first")

    bags = payload["bags"]
    state = load_json(STATE, {})
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    rows, changes, first_run = [], 0, not state

    for bag in bags:
        for i, variant in enumerate(bag.get("variants") or []):
            key = variant_key(bag, variant, i)
            current = {
                "price": variant.get("price"),
                "compare_at": variant.get("compare_at"),
                "available": bool(variant.get("available")),
            }
            previous = state.get(key)

            if previous and all(previous.get(f) == current[f] for f in current):
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
            }
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
        json.dump(state, f)

    # Annotate bags.json so the front end can show drops without parsing the
    # whole history file.
    annotated = 0
    for bag in bags:
        prices, lows, changed, direction, prev = [], [], None, None, None
        for i, variant in enumerate(bag.get("variants") or []):
            entry = state.get(variant_key(bag, variant, i))
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
                "category": bag.get("category"),
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
    }
    with open(BAGS, "w") as f:
        json.dump(payload, f, separators=(",", ":"))

    if first_run:
        print(f"baseline established: {len(rows)} SKUs recorded at {now}")
        print("no changes to report yet — run again after the next fetch")
    else:
        print(f"{len(rows)} rows appended, {changes} price/stock changes, "
              f"{annotated} bags annotated")
    print(f"tracking {len(state)} SKUs -> {HISTORY}")


if __name__ == "__main__":
    main()
