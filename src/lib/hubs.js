/* Category hubs, and the facet pages beneath them.
 *
 * Every page the catalogue had before this file answered "what is this bag" —
 * a question nobody types until they already know the model name. These answer
 * the ones people do type: "30–40 L travel backpack", "daypack for a 16-inch
 * laptop". They are generated, and the thing that keeps generated pages on the
 * right side of the scaled-content line is that each is a *computed* answer
 * over a set big enough to be worth computing. Nothing here is a template with
 * a noun swapped in: the medians, the price range and the carry-on count are
 * different numbers on every page, and a slice too small to compare does not
 * get a page at all.
 *
 * One source for the routes, the counts and the copy, so a hub cannot link to
 * a facet page that was never built — the same reason indexablePaths lives in
 * catalog.js rather than being recomputed in astro.config.mjs.
 */

import { bags, isIndexable } from './catalog.js';
import { gramsPerLitre } from './format.js';

/* How many indexable models a slice needs before it earns a page.
 *
 * Below this there is not enough to compare and the page is thin by
 * construction — the same judgement isIndexable() makes about a model with no
 * specs, applied to a list instead of a bag. It gates three things: which
 * facet pages exist at all, which hubs are worth indexing, and (because those
 * two must agree) what goes in the sitemap. */
export const MIN_FACET = 25;

/* The categories normalize.py assigns, in the words the pages are written in.
 *
 * `blurb` is the one hand-written line on each hub. It is here rather than
 * generated because a sentence saying what we mean by "sling" is the part a
 * reader cannot get from the table, and the part a scraper of this site
 * cannot get from the data. `plural` reads after a size ("40–60 L duffels"),
 * `counted` after a number ("197 pieces of luggage") — English does not let
 * one word do both for luggage.
 */
export const CATEGORIES = [
  {
    key: 'daypack', slug: 'daypacks', title: 'Daypacks',
    plural: 'daypacks', counted: 'daypacks',
    blurb:
      'One main compartment, usually a laptop sleeve, no frame.',
  },
  {
    key: 'travel-backpack', slug: 'travel-backpacks', title: 'Travel backpacks',
    plural: 'travel backpacks', counted: 'travel backpacks',
    blurb:
      'Clamshell packs cut to the overhead bin.',
  },
  {
    key: 'hiking-pack', slug: 'hiking-packs', title: 'Hiking packs',
    plural: 'hiking packs', counted: 'hiking packs',
    blurb:
      'Built to move weight onto a hip belt and keep it there for days.',
  },
  {
    key: 'sling', slug: 'slings', title: 'Slings',
    plural: 'slings', counted: 'slings',
    blurb:
      'One strap, worn across the body.',
  },
  {
    key: 'duffel', slug: 'duffels', title: 'Duffels',
    plural: 'duffels', counted: 'duffels',
    blurb:
      'A soft cylinder with handles, often with straps to wear it.',
  },
  {
    key: 'luggage', slug: 'luggage', title: 'Luggage',
    plural: 'luggage', counted: 'pieces of luggage',
    blurb:
      'Wheeled cases and the soft bags that behave like them. Stated '
      + 'dimensions here usually exclude the wheels and handle; airlines '
      + 'measure with them, so treat a near-miss as a miss.',
  },
  {
    key: 'tote', slug: 'totes', title: 'Totes',
    plural: 'totes', counted: 'totes',
    blurb:
      'Open at the top or lightly closed, carried by hand or on one shoulder.',
  },
  {
    key: 'pouch', slug: 'pouches', title: 'Pouches & organisers',
    plural: 'pouches', counted: 'pouches',
    blurb:
      'Dopp kits, cubes, cable rolls and ditty bags.',
  },
  {
    key: 'hip-pack', slug: 'hip-packs', title: 'Hip packs',
    plural: 'hip packs', counted: 'hip packs',
    blurb:
      'Worn on the waist or slung across the chest.',
  },
  {
    key: 'bike-bag', slug: 'bike-bags', title: 'Bike bags',
    plural: 'bike bags', counted: 'bike bags',
    blurb:
      'Panniers, frame bags, handlebar rolls and saddle packs.',
  },
  {
    key: 'messenger', slug: 'messengers', title: 'Messenger bags',
    plural: 'messenger bags', counted: 'messenger bags',
    blurb:
      'A flap over a single shoulder strap.',
  },
  {
    key: 'wallet', slug: 'wallets', title: 'Wallets',
    plural: 'wallets', counted: 'wallets',
    blurb:
      'Card and note carriers, from leather bifolds to the fabric ones '
      + 'hikers weigh in grams. Here because the same makers build them, '
      + 'not because they are bags.',
  },
  {
    key: 'briefcase', slug: 'briefcases', title: 'Briefcases',
    plural: 'briefcases', counted: 'briefcases',
    blurb:
      'A structured case for a laptop and paper, carried by a handle.',
  },
  {
    key: 'camera-bag', slug: 'camera-bags', title: 'Camera bags',
    plural: 'camera bags', counted: 'camera bags',
    blurb:
      'Padded, divided interiors built around a body and lenses.',
  },
];

/* Volume bands. Adjacent, non-overlapping and closed at the bottom, so every
 * bag with a stated volume lands in exactly one — two bands that both claimed
 * a 30 L pack would be two pages competing for the same query with the same
 * rows on them. */
export const VOLUME_BANDS = [
  { slug: 'under-10l', label: 'Under 10 L', min: 0, max: 10 },
  { slug: '10-20l', label: '10–20 L', min: 10, max: 20 },
  { slug: '20-30l', label: '20–30 L', min: 20, max: 30 },
  { slug: '30-40l', label: '30–40 L', min: 30, max: 40 },
  { slug: '40-60l', label: '40–60 L', min: 40, max: 60 },
  { slug: 'over-60l', label: 'Over 60 L', min: 60, max: Infinity },
];

/* Laptop sizes worth a page. `laptop_in` is the largest machine a bag takes,
 * so these sets nest — everything that swallows a 16" swallows a 13" — and
 * only the sizes people actually type are here. 14" and 17" are dropped: they
 * are search terms, but on this inventory they resolve to almost exactly the
 * same set as the size below them. */
export const LAPTOP_SIZES = [13, 15, 16];

const median = (nums) => {
  if (!nums.length) return null;
  const sorted = [...nums].sort((a, b) => a - b);
  const mid = sorted.length >> 1;
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
};

/* The linear total most airlines converge on, and the one the browse page's
 * carry-on preset already uses. Stated once so the hub copy and the filter
 * cannot drift. */
export const COMMON_CARRYON_CM = 115;

/**
 * The figures every hub and facet page opens with.
 *
 * Prices are USD-only on purpose. A few brands sell in euros, kroner or
 * pounds, nothing here converts them — a rate would be a number we invented,
 * and it would move between crawls independently of any price — so a range
 * spanning currencies would compare figures rather than worth. The count of
 * what was left out is returned alongside so the page can say so.
 *
 * @param {import('./types.d.ts').Bag[]} list
 */
export function statsFor(list) {
  const priced = list.filter((b) => b.price_min != null);
  const usd = priced.filter((b) => b.currency === 'USD');
  const prices = usd.map((b) => b.price_min);
  const measured = list.filter((b) => b.dims_cm && b.dims_cm.length >= 3);
  return {
    count: list.length,
    brands: new Set(list.map((b) => b.brand_slug)).size,
    priceLow: prices.length ? Math.min(...prices) : null,
    priceHigh: prices.length ? Math.max(...prices) : null,
    otherCurrency: priced.length - usd.length,
    medianVolume: median(list.map((b) => b.volume_l).filter((v) => v != null)),
    medianWeight: median(list.map((b) => b.weight_g).filter((v) => v != null)),
    medianGpl: median(list.map(gramsPerLitre).filter((v) => v != null)),
    measured: measured.length,
    carryOn: measured.filter((b) => b.linear_cm <= COMMON_CARRYON_CM).length,
  };
}

/* The floor under every ranking on these pages.
 *
 * A stated volume and a stated weight are a check on each other. Forty litres
 * that weigh eighty grams means one of the two numbers is wrong, and nothing
 * in the record says which — a Shopify shipping weight left at its default, a
 * volume parsed off the wrong line, a colourway row attached to the wrong
 * product. We have no basis for correcting either, so the pair is not ranked.
 *
 * Five is where that line falls on this inventory. The genuinely lightest
 * things here — silnylon dry sacks, Dyneema pack bodies sold without a
 * suspension — land between five and eight grams a litre; below five, every
 * record inspected was a bad number rather than a remarkable bag. It is a
 * judgement and it has a cost: a real sub-five-gram-per-litre bag would be
 * kept out of the leaderboards too. It stays in the index, in the tables and
 * on its own page, with both figures shown as published — this rule governs
 * what we are willing to put in rank order, not what we are willing to say. */
export const MIN_PLAUSIBLE_GPL = 5;

/** @param {import('./types.d.ts').Bag} bag */
export const rankable = (bag) => {
  const density = gramsPerLitre(bag);
  return density != null && density >= MIN_PLAUSIBLE_GPL;
};

/** Lightest per litre first. Bags with no density, and bags whose density we
 *  do not believe, go last rather than being dropped — absent is not the same
 *  as light, and neither is wrong. */
export const byDensity = (a, b) => {
  const x = rankable(a) ? gramsPerLitre(a) : null;
  const y = rankable(b) ? gramsPerLitre(b) : null;
  if (x == null && y == null) return a.brand.localeCompare(b.brand);
  if (x == null) return 1;
  if (y == null) return -1;
  return x - y;
};

function facetsFor(cat, list) {
  const out = [];

  for (const band of VOLUME_BANDS) {
    const members = list.filter(
      (b) => b.volume_l != null && b.volume_l >= band.min && b.volume_l < band.max,
    );
    if (members.length < MIN_FACET) continue;
    out.push({
      kind: 'volume',
      slug: band.slug,
      label: band.label,
      title: `${band.label} ${cat.plural}`,
      band,
      bags: members,
    });
  }

  /* Because the laptop sets nest, two adjacent sizes can be two pages saying
   * the same thing about nearly the same rows. A size only earns a page when
   * it drops at least a quarter of what the last kept size held — enough that
   * the table, the medians and the answer are all visibly different. */
  let kept = null;
  for (const inches of LAPTOP_SIZES) {
    const members = list.filter((b) => b.laptop_in != null && b.laptop_in >= inches);
    if (members.length < MIN_FACET) continue;
    if (kept != null && members.length > kept * 0.75) continue;
    kept = members.length;
    out.push({
      kind: 'laptop',
      slug: `${inches}-inch-laptop`,
      label: `${inches}-inch laptop`,
      title: `${cat.title} for a ${inches}-inch laptop`,
      inches,
      bags: members,
    });
  }

  return out;
}

/** Every hub, widest first — the order they are listed in on each other. */
export const hubs = CATEGORIES.map((cat) => {
  const all = bags.filter((b) => b.category === cat.key);
  const list = all.filter(isIndexable);
  return {
    ...cat,
    href: `/${cat.slug}/`,
    /** Every model in the category, indexable or not — what the brand pages
     *  and the browse grid show, and the honest denominator for coverage. */
    total: all.length,
    /** The ones with specs worth comparing, which is what these pages list. */
    bags: list,
    stats: statsFor(list),
    facets: facetsFor(cat, list).map((facet) => ({
      ...facet,
      href: `/${cat.slug}/${facet.slug}/`,
      stats: statsFor(facet.bags),
    })),
    /* Same judgement as a model page with no specs on it: the page ships,
     * is linked and is crawlable either way, and only the indexing waits. */
    indexable: list.length >= MIN_FACET,
  };
})
  .filter((hub) => hub.total > 0)
  .sort((a, b) => b.bags.length - a.bags.length);

/** @type {Map<string, typeof hubs[number]>} */
export const hubByCategory = new Map(hubs.map((hub) => [hub.key, hub]));

/** The hub a model page should link back to, or null for a category with no
 *  hub — which today cannot happen, and is still not worth a broken link. */
export const hubHref = (category) => hubByCategory.get(category)?.href ?? null;

/* The sitemap gate, built here for the same reason catalog.js builds its own:
 * a page in the sitemap and noindex in the markup is the confusing half-state,
 * and the only way the two cannot disagree is for one list to drive both. */
export const hubPaths = new Set(
  hubs.flatMap((hub) => [hub.href, ...hub.facets.map((f) => f.href)]),
);
export const indexableHubPaths = new Set(
  hubs
    .filter((hub) => hub.indexable)
    .flatMap((hub) => [hub.href, ...hub.facets.map((f) => f.href)]),
);
