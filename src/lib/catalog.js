/* Build-time access to the data plane.
 *
 * The catalog has no runtime database by design: these files are read once
 * while Astro is generating pages and the output is plain HTML. Nothing here
 * runs in a request.
 */

/* The data files are imported, not read off disk at runtime. Resolving them
 * relative to `import.meta.url` breaks the moment this module gets bundled
 * into the server entry — which is exactly what the adapter does — and the
 * failure is silent: you get a build that succeeds and publishes an empty
 * catalog. Letting Vite resolve them makes the data a build-time dependency,
 * so a missing file is a failed build instead of a deployed blank site. */

import payload from '../../data/bags.json';
import historyText from '../../data/price-history.jsonl?raw';

export const meta = payload.meta ?? {};
export const bags = payload.bags ?? [];

if (!bags.length) {
  throw new Error(
    'data/bags.json has no bags — run normalize.py before building. ' +
      'Refusing to publish an empty index.',
  );
}

/** bag ids are `<brand-slug>__<model-slug>`; the route mirrors that split. */
export function splitId(id) {
  const at = id.indexOf('__');
  return at < 0
    ? { brand: 'unknown', model: id }
    : { brand: id.slice(0, at), model: id.slice(at + 2) };
}

export function bagHref(bag) {
  const { brand, model } = splitId(bag.id);
  return `/bags/${brand}/${model}/`;
}

export const brands = (() => {
  const by = new Map();
  for (const bag of bags) {
    let entry = by.get(bag.brand_slug);
    if (!entry) {
      entry = { slug: bag.brand_slug, name: bag.brand, bags: [] };
      by.set(bag.brand_slug, entry);
    }
    entry.bags.push(bag);
  }
  for (const entry of by.values()) {
    entry.bags.sort((a, b) => a.name.localeCompare(b.name));
  }
  return [...by.values()].sort((a, b) => a.name.localeCompare(b.name));
})();

/* ---------- price history ----------
 * price-history.jsonl is a change log, not a daily dump, so a series is
 * already sparse by construction: one point per actual move. Read it once and
 * index by bag.
 */
const history = (() => {
  const by = new Map();
  for (const line of historyText.split('\n')) {
    if (!line.trim()) continue;
    let row;
    try {
      row = JSON.parse(line);
    } catch {
      continue; // a torn final line from an interrupted append
    }
    const list = by.get(row.bag_id);
    if (list) list.push(row);
    else by.set(row.bag_id, [row]);
  }
  for (const list of by.values()) list.sort((a, b) => a.ts.localeCompare(b.ts));
  return by;
})();

/**
 * The cheapest price seen on each date this bag changed, oldest first.
 * Collapses the per-SKU rows into one series — a reader wants "what did this
 * bag cost", not one line per colourway.
 */
export function priceSeries(bagId) {
  const rows = history.get(bagId) ?? [];
  const byDay = new Map();
  for (const row of rows) {
    if (row.price == null) continue;
    const day = row.ts.slice(0, 10);
    const low = byDay.get(day);
    if (low == null || row.price < low) byDay.set(day, row.price);
  }
  return [...byDay.entries()]
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([date, price]) => ({ date, price }));
}

/** Every recorded change for a bag, newest first — the table under the chart. */
export function priceChanges(bagId) {
  return (history.get(bagId) ?? [])
    .filter((row) => row.direction)
    .sort((a, b) => b.ts.localeCompare(a.ts));
}

export const trackingSince = (() => {
  let earliest = null;
  for (const list of history.values()) {
    const first = list[0]?.ts;
    if (first && (earliest == null || first < earliest)) earliest = first;
  }
  return earliest;
})();
