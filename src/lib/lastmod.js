/* When each page last actually changed, for the sitemap's <lastmod>.
 *
 * Every <url> shipped as a bare <loc>, which is a wasted signal on a catalogue
 * that rebuilds nightly: freshness is the whole advantage this index has over
 * the hand-maintained spreadsheets it competes with, and it was invisible.
 *
 * The hard part is not plumbing it through, it is choosing the timestamp. Two
 * candidates exist on every bag and only one of them is honest:
 *
 *   fetched_at  — when we last *looked*. Present on all 8,043 bags, and moves
 *                 every night whether or not a single byte of the page differs.
 *                 Stamping that on 4,000 URLs tells Google every page changed
 *                 today, every day, which is precisely the pattern that gets
 *                 lastmod discounted sitewide. A checked page is not a changed
 *                 page.
 *   updated_at  — the brand's own record of when the product last changed.
 *                 Genuine, and missing on 1,277 bags.
 *
 * So: updated_at, plus the price ledger. A price move is a real change to what
 * the page says — it is rendered on it, and it is the field a returning reader
 * came back for — and price-history.jsonl already records exactly when each one
 * happened, one row per actual move rather than a nightly snapshot. The later
 * of the two is the answer.
 *
 * Where neither exists, the entry ships with no lastmod rather than a guess.
 * That is the same rule the rest of the index runs on: a "—" is never a zero,
 * and an omitted optional tag costs nothing while a wrong one costs the tag's
 * credibility everywhere else in the file.
 */

import { bags, bagHref, brands, priceChanges } from './catalog.js';
import { hubs } from './hubs.js';

/** ISO 8601 in UTC, or null for anything unparseable.
 *
 *  Normalising matters: `fetched_at` is written as `...Z` but Shopify's
 *  `updated_at` carries an offset (`2026-08-08T07:21:42-07:00`), and the two
 *  are compared against each other below. Lexicographic comparison across
 *  mixed offsets silently picks the wrong one — 07:21-07:00 is *later* than
 *  09:00Z and sorts earlier.
 *  @param {string | null | undefined} value */
function iso(value) {
  if (!value) return null;
  const at = new Date(value);
  return Number.isNaN(at.getTime()) ? null : at.toISOString();
}

/** @param {(string | null)[]} values */
const latest = (values) => values.filter(Boolean).sort().pop() ?? null;

/** @param {import('./types.d.ts').Bag} bag */
function bagLastmod(bag) {
  // priceChanges() is newest-first and already filtered to rows carrying a
  // direction, so the first entry is the last real move. A bag's opening
  // observation has no direction and is correctly not a change.
  return latest([iso(bag.updated_at), iso(priceChanges(bag)[0]?.ts)]);
}

/* Path -> ISO timestamp, for everything the sitemap filter lets through.
 *
 * Three kinds of page are in here and one deliberately is not:
 *
 *   model pages   the bag's own timestamp.
 *   brand pages   the latest across that brand's bags, because the page is a
 *   hubs, facets  table of them and any row moving changes it. This does mean
 *                 a big hub carries a recent date most nights — which is true,
 *                 not inflation: a hub renders the price range and the medians
 *                 of everything on it.
 *
 * Not here: `/`, `/brands/`, `/carry-on/*`, `/privacy/`, `/bot/`. The first two
 * are indexes of indexes, whose max-over-everything would be today's date
 * forever and would mean nothing; the last three change when someone edits
 * them, which is a fact no field in the data plane records. No timestamp beats
 * a meaningless one.
 */
export const lastmodByPath = (() => {
  /** @type {Map<string, string>} */
  const by = new Map();

  /** @type {Map<string, string>} */
  const byBag = new Map();
  for (const bag of bags) {
    const stamp = bagLastmod(bag);
    if (!stamp) continue;
    byBag.set(bag.id, stamp);
    by.set(bagHref(bag), stamp);
  }

  /** @param {import('./types.d.ts').Bag[]} list */
  const over = (list) => latest(list.map((bag) => byBag.get(bag.id) ?? null));

  for (const brand of brands) {
    const stamp = over(brand.bags);
    if (stamp) by.set(`/brands/${brand.slug}/`, stamp);
  }

  for (const hub of hubs) {
    const stamp = over(hub.bags);
    if (stamp) by.set(hub.href, stamp);
    for (const facet of hub.facets) {
      const facetStamp = over(facet.bags);
      if (facetStamp) by.set(facet.href, facetStamp);
    }
  }

  return by;
})();
