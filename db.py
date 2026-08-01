#!/usr/bin/env python3
"""
bagdex store — SQLite as the query layer.

The JSONL price ledger stays the append-only source of truth: it is durable,
greppable, and diffs cleanly in git. This mirrors it into SQLite so the alert
matcher can ask indexed questions ("every drop on this bag since Tuesday")
instead of scanning a file that only grows, and so subscriptions get the
uniqueness and transactional guarantees a pile of JSON cannot offer.

Sync is idempotent. Every price event carries a deterministic event_key, so
re-running after a partial import inserts exactly the rows that were missing.

    python3 db.py init     create schema
    python3 db.py sync     import bags.json + price-history.jsonl
    python3 db.py stats    what is in there
"""

import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "bagdex.db")
BAGS = os.path.join(HERE, "data", "bags.json")
HISTORY = os.path.join(HERE, "data", "price-history.jsonl")

SCHEMA = """
CREATE TABLE IF NOT EXISTS bags (
  id TEXT PRIMARY KEY,
  brand TEXT, brand_slug TEXT, name TEXT, category TEXT,
  url TEXT, image TEXT,
  volume_l REAL, dims_cm TEXT, linear_cm REAL, weight_g INTEGER, laptop_in REAL,
  price_min REAL, price_max REAL, lowest_ever REAL,
  in_stock INTEGER, on_sale INTEGER,
  colors TEXT, materials TEXT, features TEXT,
  variant_count INTEGER, fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS variants (
  key TEXT PRIMARY KEY,
  bag_id TEXT NOT NULL REFERENCES bags(id) ON DELETE CASCADE,
  sku TEXT, color TEXT, size TEXT,
  price REAL, compare_at REAL, available INTEGER
);
CREATE INDEX IF NOT EXISTS idx_variants_bag ON variants(bag_id);
CREATE INDEX IF NOT EXISTS idx_variants_sku ON variants(sku);

CREATE TABLE IF NOT EXISTS price_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_key TEXT NOT NULL UNIQUE,
  ts TEXT NOT NULL,
  bag_id TEXT NOT NULL,
  sku TEXT, color TEXT,
  price REAL, compare_at REAL, available INTEGER,
  prev_price REAL, delta REAL, direction TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_bag_ts ON price_events(bag_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_ts ON price_events(ts);
CREATE INDEX IF NOT EXISTS idx_events_dir ON price_events(direction);

-- One row per thing a person asked to be told about. UNIQUE(email, bag_id,
-- sku) stops a double-submitted form creating two subscriptions; COALESCE
-- keeps that constraint working when sku is null (any variant).
CREATE TABLE IF NOT EXISTS watchers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL,
  bag_id TEXT NOT NULL,
  sku TEXT,
  target_price REAL,
  token TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  confirmed_at TEXT,
  unsubscribed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_watchers_unique
  ON watchers(email, bag_id, COALESCE(sku, ''));
CREATE INDEX IF NOT EXISTS idx_watchers_bag ON watchers(bag_id);

-- Composite primary key is the dedupe: a given watcher can be told about a
-- given price event exactly once, enforced by the database rather than by
-- remembering to check.
CREATE TABLE IF NOT EXISTS sent_alerts (
  watcher_id INTEGER NOT NULL REFERENCES watchers(id) ON DELETE CASCADE,
  event_id INTEGER NOT NULL REFERENCES price_events(id) ON DELETE CASCADE,
  sent_at TEXT NOT NULL,
  PRIMARY KEY (watcher_id, event_id)
);
"""


def connect():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def event_key(row):
    """Deterministic identity for a price observation, so sync is idempotent."""
    return "|".join(str(row.get(f) or "") for f in
                    ("ts", "bag_id", "sku", "color", "price", "available"))


def sync(conn):
    if not os.path.exists(BAGS):
        sys.exit("no data/bags.json — run normalize.py first")

    payload = json.load(open(BAGS))
    bags = payload["bags"]

    with conn:
        for bag in bags:
            conn.execute("""
                INSERT INTO bags (id, brand, brand_slug, name, category, url,
                    image, volume_l, dims_cm, linear_cm, weight_g, laptop_in,
                    price_min, price_max, lowest_ever, in_stock, on_sale,
                    colors, materials, features, variant_count, fetched_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    price_min=excluded.price_min, price_max=excluded.price_max,
                    lowest_ever=excluded.lowest_ever, in_stock=excluded.in_stock,
                    on_sale=excluded.on_sale, fetched_at=excluded.fetched_at
            """, (
                bag["id"], bag["brand"], bag["brand_slug"], bag["name"],
                bag["category"], bag["url"], bag.get("image"),
                bag.get("volume_l"),
                json.dumps(bag.get("dims_cm")) if bag.get("dims_cm") else None,
                bag.get("linear_cm"), bag.get("weight_g"), bag.get("laptop_in"),
                bag.get("price_min"), bag.get("price_max"), bag.get("lowest_ever"),
                int(bool(bag.get("in_stock"))), int(bool(bag.get("on_sale"))),
                json.dumps(bag.get("colors") or []),
                json.dumps(bag.get("materials") or []),
                json.dumps(bag.get("features") or []),
                bag.get("variant_count") or 0, bag.get("fetched_at"),
            ))

            for i, v in enumerate(bag.get("variants") or []):
                key = (f"{bag['id']}::{v['sku']}" if v.get("sku")
                       else f"{bag['id']}::#{i}:{v.get('color') or v.get('title') or i}")
                conn.execute("""
                    INSERT INTO variants (key, bag_id, sku, color, size, price,
                        compare_at, available)
                    VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(key) DO UPDATE SET
                        price=excluded.price, compare_at=excluded.compare_at,
                        available=excluded.available
                """, (key, bag["id"], v.get("sku"), v.get("color"), v.get("size"),
                      v.get("price"), v.get("compare_at"),
                      int(bool(v.get("available")))))

    imported = 0
    if os.path.exists(HISTORY):
        with conn:
            for line in open(HISTORY):
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                cur = conn.execute("""
                    INSERT OR IGNORE INTO price_events (event_key, ts, bag_id,
                        sku, color, price, compare_at, available, prev_price,
                        delta, direction)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """, (event_key(row), row["ts"], row["bag_id"], row.get("sku"),
                      row.get("color"), row.get("price"), row.get("compare_at"),
                      int(bool(row.get("available"))), row.get("prev_price"),
                      row.get("delta"), row.get("direction")))
                imported += cur.rowcount

    print(f"synced {len(bags)} bags, {imported} new price events")


def stats(conn):
    q = lambda sql: conn.execute(sql).fetchone()[0]
    print(f"bags            {q('SELECT COUNT(*) FROM bags')}")
    print(f"variants (SKUs) {q('SELECT COUNT(*) FROM variants')}")
    print(f"price events    {q('SELECT COUNT(*) FROM price_events')}")
    drops = q("SELECT COUNT(*) FROM price_events WHERE direction='down'")
    print(f"  drops         {drops}")
    print(f"watchers        {q('SELECT COUNT(*) FROM watchers')}")
    print(f"  confirmed     {q('SELECT COUNT(*) FROM watchers WHERE confirmed_at IS NOT NULL')}")
    print(f"alerts sent     {q('SELECT COUNT(*) FROM sent_alerts')}")
    row = conn.execute("SELECT MIN(ts), MAX(ts) FROM price_events").fetchone()
    if row[0]:
        print(f"history spans   {row[0]} .. {row[1]}")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "sync"
    conn = connect()
    init(conn)
    if cmd == "init":
        print(f"schema ready at {DB}")
    elif cmd == "sync":
        sync(conn)
        stats(conn)
    elif cmd == "stats":
        stats(conn)
    else:
        sys.exit(f"unknown command: {cmd}")
    conn.close()


if __name__ == "__main__":
    main()
