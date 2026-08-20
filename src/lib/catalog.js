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

/**
 * @import { Bag, Brand, CatalogMeta, CatalogPayload, PricePoint, PriceRow } from './types.d.ts'
 */

import rawPayload from '../../data/bags.json';
import historyText from '../../data/price-history.jsonl?raw';

/* bags.json is too large for TypeScript to infer a shape from, so it arrives as
 * `any` and would hand an implicit `any` to every page that maps over it. The
 * cast names what normalize.py actually writes — see types.d.ts. */
const payload = /** @type {CatalogPayload} */ (rawPayload);

/** @type {CatalogMeta} */
export const meta = payload.meta ?? {};
/** @type {Bag[]} */
export const bags = payload.bags ?? [];

if (!bags.length) {
  throw new Error(
    'data/bags.json has no bags — run normalize.py before building. ' +
      'Refusing to publish an empty index.',
  );
}

/* Alphabetical order for anything a reader scans as a list: brands, and models
 * within a brand.
 *
 * Plain collation opens the catalogue with 3F UL Gear and 5.11 Tactical, so
 * the first thing on a brand-sorted page is two numeric outliers standing in
 * front of the alphabet — and Able Carry, the actual first entry, is below the
 * fold of the A's. A name beginning with a digit has no place in an A-Z scan,
 * so it sorts after it, the way a phone book has always done it.
 *
 * One collator for the whole module: localeCompare builds its collation table
 * on every call, and the brand sort in api/browse.ts asks for a comparison on
 * the order of a hundred thousand times.
 *
 * Only the first character picks the bucket; inside each, ordinary collation.
 */
const collator = new Intl.Collator();

/** @param {string} s */
const opensWithDigit = (s) => {
  const c = s.charCodeAt(0);
  return c >= 48 && c <= 57; // NaN on the empty string, which is correctly false
};

/** @param {string} a @param {string} b */
export function byLabel(a, b) {
  const x = opensWithDigit(a);
  if (x !== opensWithDigit(b)) return x ? 1 : -1;
  return collator.compare(a, b);
}

/** bag ids are `<brand-slug>__<model-slug>`; the route mirrors that split.
 *  @param {string} id */
export function splitId(id) {
  const at = id.indexOf('__');
  return at < 0
    ? { brand: 'unknown', model: id }
    : { brand: id.slice(0, at), model: id.slice(at + 2) };
}

/** The permalink. `slug` is derived from the merged model name; the id is a
 *  storage key and can carry a colourway the page is not about.
 *  @param {Bag} bag */
export function bagSlug(bag) {
  return bag.slug || splitId(bag.id).model;
}

/** @param {Bag} bag */
export function bagHref(bag) {
  return `/bags/${bag.brand_slug ?? splitId(bag.id).brand}/${bagSlug(bag)}/`;
}

/* Where one colourway can be bought.
 *
 * A model page merges every storefront product that is the same bag, so the
 * colourway table is the only place those separate listings still exist. Two
 * ways to reach one, best destination first:
 *
 * 1. Its own listing, when the brand publishes a product per fabric. Those
 *    URLs are in `merged_urls`, and the only safe match is a colour that names
 *    exactly one of them — "Black" sits inside both .../ripstop-black and
 *    .../x-pac-black, and a wrong guess sends the reader to a different bag.
 *    Ambiguity therefore falls through rather than picking a winner.
 * 2. The Shopify variant query string, which is not a distinct page but does
 *    land on the right swatch. Every Shopify record here carries variant ids;
 *    the other platforms in the feed layer have no equivalent we can trust.
 *
 * Anything else gets no link. The model's page is already one click away on
 * the buy button, and a link that lands on the wrong colour is worse than
 * plain text.
 */

/** Lowercased alphanumerics only, parentheticals dropped: "X-Pac Black (VX21)"
 *  and "x-pac-black" have to compare equal, and the code in the brackets is a
 *  fabric spec that never appears in a URL.
 *  @param {string | null | undefined} s */
const alnum = (s) => String(s ?? '').toLowerCase().replace(/\([^)]*\)/g, '').replace(/[^a-z0-9]+/g, '');

/** The product handle: the last path segment, normalised.
 *  @param {string} url */
function urlHandle(url) {
  try {
    const parts = new URL(url).pathname.split('/').filter(Boolean);
    return alnum(parts[parts.length - 1]);
  } catch {
    return '';
  }
}

/** One entry per variant, positionally, null where we cannot place it.
 *  @param {Bag} bag
 *  @returns {(string | null)[]} */
export function variantUrls(bag) {
  const variants = bag.variants ?? [];
  if (!variants.length) return [];

  const listings = [...new Set([bag.url, ...(bag.merged_urls ?? [])].filter(Boolean))]
    .map((url) => ({ url, handle: urlHandle(url) }));
  const deepLink = String(bag.source ?? '').startsWith('shopify:') ? bag.url : null;

  return variants.map((v) => {
    const key = alnum(v.color || v.title);
    // Two-character colour names ("XS", and the odd bare initial) match too
    // much of a handle to be evidence of anything.
    if (listings.length > 1 && key.length >= 3) {
      const hits = listings.filter((l) => l.handle.includes(key));
      if (hits.length === 1) return hits[0].url;
    }
    return deepLink && v.variant_id ? `${deepLink}?variant=${v.variant_id}` : null;
  });
}

/* How deep the sale goes, as a fraction of the compare-at price.
 *
 * `on_sale` is a boolean the feed layer sets when any colourway is listed
 * below its own compare-at price — see normalize.py — and a boolean is enough
 * to badge a card but not enough to rank the thousand-odd bags carrying it.
 * This is the number that ranks them, and it is deliberately the *deepest*
 * colourway rather than an average or the one behind `price_min`: a reader
 * sorting by discount is asking which listings have something worth looking at
 * on them, and the answer is per-colourway on every storefront in the feed.
 *
 * A compare-at price is the seller's own claim about what the bag used to
 * cost, not an observation — this site's price ledger is the stronger evidence
 * and it is what the model page's chart draws. Nothing here corrects a
 * compare-at that was never charged; the sale page says so instead.
 *
 * Both ends of the cut come back together, because the two places that show a
 * discount show the price beside it and re-deriving one from the other would
 * pair a percentage from one colourway with a price from another.
 *
 * null when no colourway states both numbers, which is not the same as zero:
 * some sources set `on_sale` off a bare compare-at field with no price to
 * divide it by, and those sort last rather than sorting as "no discount".
 *
 * @param {Bag} bag
 * @returns {{ off: number, price: number, was: number } | null}
 */
export function deepestCut(bag) {
  /** @type {{ off: number, price: number, was: number } | null} */
  let best = null;
  for (const v of bag.variants ?? []) {
    if (!v.price || !v.compare_at || v.compare_at <= v.price) continue;
    const off = 1 - v.price / v.compare_at;
    if (!best || off > best.off) best = { off, price: v.price, was: v.compare_at };
  }
  return best;
}

/** Just the fraction, which is all the sort in api/browse.ts needs.
 *  @param {Bag} bag
 *  @returns {number | null} 0–1, or null when it cannot be computed */
export const saleDiscount = (bag) => deepestCut(bag)?.off ?? null;

/* Is this model's page worth arriving on from a search result?
 *
 * The catalogue is wide before it is deep. The feed layer reaches thousands of
 * models in one pass; dimensions arrive later, one crawled page at a time.
 * Publishing all of it at once means asking Google to index several thousand
 * pages showing a name, a price and a column of dashes — which is what "thin
 * content" names, and an expensive thing to be classified as on a domain
 * carrying five years of someone else's history.
 *
 * So the page ships either way and the *indexing* is what waits. A model joins
 * the index once it has something to say, and enrichment moves a few dozen
 * across that line a night. Nothing is hidden from a reader: every page is
 * linked, crawlable and identical whichever side of the line it sits on. This
 * is a statement about search results, not about access.
 *
 * The rule is dimensions, or volume and weight together. Dimensions are the
 * load-bearing field — the carry-on matrix needs three axes and abstains
 * without them — but requiring them alone would hold back a whole category
 * unfairly. Cottage ultralight makers do not publish external dimensions and
 * are not being evasive: a frameless pack is genuinely specified by volume,
 * weight and torso range, and the outside measurements of a soft bag are not a
 * meaningful number. For that category, volume and weight together *is* the
 * full spec.
 */
/** @param {Bag | null | undefined} bag */
export function isIndexable(bag) {
  if (!bag) return false;
  if (bag.dims_cm) return true;
  return Boolean(bag.volume_l && bag.weight_g);
}

/** Model paths that belong in the sitemap. Built here rather than recomputed
 *  in astro.config.mjs so the sitemap and the meta tag cannot disagree — a
 *  page excluded from one and not the other is the confusing half-state. */
export const indexablePaths = new Set(
  (payload.bags ?? []).filter(isIndexable).map(bagHref),
);

/* Where a link to a page that has been split now goes.
 *
 * /bags/evergoods/civic-travel-bag/ was a real page for as long as the index
 * has existed, and it is in Google, in link previews and in whatever anyone
 * saved. Splitting the model into three capacities takes that URL out of the
 * build, and a static site answers a path it generates nothing for with a
 * blank 404 — the failure /404.astro was written for and the one it says a
 * catalogue earns more of than an ordinary site.
 *
 * It resolves to the capacity the old page was actually about, which is the
 * volume that record published: that is the number the page showed, the specs
 * Google indexed against it, and the bag anyone who bookmarked it was looking
 * at. normalize.py marks that entry with `legacy_slug` and marks no other.
 *
 * A permanent redirect and not the 404: the page did not stop existing, it
 * acquired two siblings, and everything the old URL earned should move with it.
 */
export const legacyRedirects = (() => {
  /** @type {Record<string, string>} */
  const out = {};
  const live = new Set(bags.map(bagHref));
  for (const bag of bags) {
    if (!bag.legacy_slug) continue;
    const from = `/bags/${bag.brand_slug}/${bag.legacy_slug}/`;
    // A split whose old name is another model's live permalink is not a
    // redirect, it is an outage on that model. Nothing in the catalogue does
    // this today; the guard is here because the names come from brands.
    if (live.has(from)) continue;
    out[from] = bagHref(bag);
  }
  return out;
})();

export const brands = (() => {
  /** @type {Map<string, Brand>} */
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
    entry.bags.sort((a, b) => byLabel(a.name, b.name));
  }
  return [...by.values()].sort((a, b) => byLabel(a.name, b.name));
})();

/* ---------- price history ----------
 * price-history.jsonl is a change log, not a daily dump, so a series is
 * already sparse by construction: one point per actual move. Read it once and
 * index by bag.
 */
const history = (() => {
  /** @type {Map<string, PriceRow[]>} */
  const by = new Map();
  for (const line of historyText.split('\n')) {
    if (!line.trim()) continue;
    /** @type {PriceRow} */
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

/* Every ledger row that is this bag's, whatever id it was written under.
 *
 * A model sold in three capacities is three entries as of the split — see
 * split_sizes() in normalize.py — and the months of history behind it were all
 * written against the one id the record used to have. Reading only the new id
 * would open three pages with an empty chart on a bag we have tracked since
 * the day it was indexed.
 *
 * Inherited rows are filtered by SKU, which is the only honest way to divide
 * them: the ledger records a price against a SKU, and a SKU belongs to exactly
 * one size. Rows written for a colourway the feed gave no SKU cannot be
 * attributed to a size at all and are left behind rather than shown under one
 * — a 20L chart with the 35L's prices in it is worse than a short chart.
 *
 * @param {Bag} bag
 * @returns {PriceRow[]}
 */
function historyRows(bag) {
  const own = history.get(bag.id) ?? [];
  if (!bag.split_from) return own;

  const skus = new Set((bag.variants ?? []).map((v) => v.sku).filter(Boolean));
  const inherited = (history.get(bag.split_from) ?? []).filter(
    (row) => row.sku && skus.has(row.sku),
  );
  if (!inherited.length) return own;
  return [...inherited, ...own].sort((a, b) => a.ts.localeCompare(b.ts));
}

/**
 * The cheapest price seen on each date this bag changed, oldest first.
 * Collapses the per-SKU rows into one series — a reader wants "what did this
 * bag cost", not one line per colourway.
 *
 * @param {Bag} bag
 * @returns {PricePoint[]}
 */
export function priceSeries(bag) {
  const rows = historyRows(bag);
  /** @type {Map<string, number>} */
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

/** Every recorded change for a bag, newest first — the table under the chart.
 *  @param {Bag} bag
 *  @returns {PriceRow[]} */
export function priceChanges(bag) {
  return historyRows(bag)
    .filter((row) => row.direction)
    .sort((a, b) => b.ts.localeCompare(a.ts));
}

export const trackingSince = (() => {
  /** @type {string | null} */
  let earliest = null;
  for (const list of history.values()) {
    const first = list[0]?.ts;
    if (first && (earliest == null || first < earliest)) earliest = first;
  }
  return earliest;
})();
