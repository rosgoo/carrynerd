/* GET /api/browse — the filter itself, run where the catalogue already lives.
 *
 * The island used to answer its own questions: it downloaded an index of every
 * bag and matched against it in memory, which is the fastest possible filter
 * and also hands the whole enriched catalogue to anyone who reads a network
 * tab once. The specs in it are the work — a crawl, a normaliser and a
 * enrichment pass per model — and a file that publishes all of it at one URL
 * gives that away for the price of a single GET. So the matching moved here.
 * The trade is a round trip per filter change, and it was made deliberately.
 *
 * What that buys is only worth having if there is no second door: a page is
 * capped, so the whole catalogue is eighty-odd requests at the cap and about
 * 135 at the size the island actually asks for. Requests that have to be
 * enumerated are requests a WAF can see, count and throttle — which is where
 * bulk access is actually refused, since rate limiting lives in the Vercel
 * dashboard and can watch a client across requests where this function cannot.
 * The cap does not make crawling impossible. It makes it visible.
 *
 * Everything below the parsing is a port of what browse.js used to do to its
 * own copy, and it has to stay a faithful one: the same haystack, the same
 * any-of/every-of rules, the same refusal to let an unknown value satisfy a
 * range. A filter that answers differently on the server than it did in the
 * browser is a bug the reader sees as the catalogue changing under them.
 */

import type { APIRoute } from 'astro';
import { bags, meta, byLabel, saleDiscount } from '../../lib/catalog.js';
import { carryOnAirlines, airlineFit, airlineSlug } from '../../lib/carryon.js';
import type { Bag as CatalogBag } from '../../lib/types.d.ts';
import { FIT_FEATURE } from '../../lib/labels.js';

export const prerender = false;

const json = (body: unknown, status = 200, cache?: string) =>
  new Response(JSON.stringify(body), {
    status,
    headers: {
      'content-type': 'application/json',
      ...(cache ? { 'cache-control': cache } : {}),
    },
  });

/* Same bargain as /api/catalog: the answer changes once per deploy and never
 * in between, so a query string that has been asked before can be served from
 * the edge without waking anything. That is what makes a round trip per
 * keystroke affordable — the popular filters stop being function invocations
 * within minutes of a deploy.
 *
 * `fav` is the one parameter that is nobody else's query. A reader with
 * favourites saved is asking a question no other reader asks, so their
 * requests miss the shared cache and reach the function — which is the price
 * of pinning their bags to the top of a page that is otherwise the same for
 * everyone, and is paid only by the readers who asked for it. There is nothing
 * private in the response either way: it is the same public catalogue, in a
 * different order. */
const CACHE = 'public, s-maxage=3600, stale-while-revalidate=86400';

/* A screenful and a bit. browse.js asks for exactly PER_DEFAULT and appends the
 * next page as the sentinel comes into reach; the ceiling is what stops a
 * client asking for the catalogue in one go, so it is the number that matters
 * and the one to leave alone. */
const PER_DEFAULT = 60;
const PER_MAX = 100;

/* Favourites are saved in the reader's browser — see lib/store.js — and sent
 * back as ids so their bags can be lifted above the paged results. They ride
 * outside the page: the strip is not something you scroll to the end of. The
 * cap is what keeps that honest, and it agrees with MAX_FAVOURITES on the
 * client so a list that was allowed to be saved is a list that is honoured in
 * full. Anything past it is ignored rather than refused — a reader whose
 * storage has more ids in it than this build accepts should still get a page. */
const MAX_FAVS = 60;

type Bag = Record<string, any>;

/* Everything a card, a table row, the comparison sheet and the detail drawer
 * read. Photography and the storefront link ride along now rather than being
 * fetched per painted chunk: the page cap is what protects them, and it protects
 * them just as well here as it did behind /api/catalog. What does not ride
 * along is `tags` — searching reaches tag text, but that happens below and the
 * text itself never leaves this function. */
const CARD_FIELDS = [
  'id', 'slug', 'name', 'brand', 'brand_slug', 'category', 'currency',
  'in_stock', 'on_sale', 'price_min', 'price_max',
  'volume_l', 'volume_source', 'dims_cm', 'dims_source', 'linear_cm',
  'weight_g', 'weight_source', 'laptop_in', 'features', 'features_source',
  'materials', 'colors', 'color_families', 'variant_count',
  'image', 'image_bg', 'url',
] as const;

const VARIANT_FIELDS = [
  'sku', 'title', 'color', 'color_family', 'price', 'compare_at',
  'available', 'image', 'image_bg',
] as const;

const pick = (obj: Bag, keys: readonly string[]) => {
  const out: Record<string, unknown> = {};
  for (const k of keys) if (obj[k] !== undefined) out[k] = obj[k];
  return out;
};

/* Takes the row rather than the record, for one field: `discount` is computed
 * here and is not in bags.json. The card draws it instead of the bare "Sale"
 * flag it used to draw, which is the difference between a badge that says a
 * price moved and one that says how far. */
const project = (r: Row) => ({
  ...pick(r.bag, CARD_FIELDS),
  ...(r.discount != null ? { discount: r.discount } : {}),
  variants: (r.bag.variants ?? []).map((v: Bag) => pick(v, VARIANT_FIELDS)),
});

/* ---------- the filterable projection ---------- */

/* One row per bag, built once per instance rather than once per request.
 *
 * Fluid Compute keeps a warm instance between invocations, so anything that
 * depends only on the deploy can be paid for on the cold start and then never
 * again — the haystack especially, which is a string concatenation per bag and
 * the single most expensive thing a text search would otherwise do 8,079 times
 * on every keystroke.
 *
 * The rows hold the comparanda and a reference back to the record; the shipped
 * projection is built per request, for the sixty bags actually being sent. That
 * split is on purpose: precomputing the projections too would keep a second
 * copy of most of the catalogue resident to save a millisecond.
 */
type Row = {
  bag: Bag;
  id: string;
  hay: string;
  category: string;
  brand_slug: string;
  features: Set<string>;
  materials: Set<string>;
  colours: Set<string>;
  in_stock: boolean;
  on_sale: boolean;
  /* How far the deepest colourway is below its compare-at price — see
   * saleDiscount() in lib/catalog.js. null for anything not on sale, and also
   * for the handful whose source sets the flag off a compare-at with no price
   * beside it. */
  discount: number | null;
  volume_l: number | null;
  price_min: number | null;
  weight_g: number | null;
  linear_cm: number | null;
  laptop_in: number | null;
  carryon: boolean;
  underseat: boolean;
  /* The carriers whose published carry-on limit this bag clears, by slug.
   * Empty — and shared — for a bag stating fewer than three axes, which is
   * abstention rather than failure; see noDims below. */
  airlines: ReadonlySet<string>;
  /* Stated fewer than three axes, so no gauge can be run on it at all. The
   * counterpart to featuresUnknown, for the filters that measure. */
  noDims: boolean;
  /* A feature list built only from marketing copy cannot be read as "does not
   * have one" — see the caveat the count line prints. */
  featuresUnknown: boolean;
  noColour: boolean;
  variant_count: number;
  brand: string;
  name: string;
  gpl: number | null;
  ppl: number | null;
};

const num = (v: unknown): number | null =>
  typeof v === 'number' && Number.isFinite(v) ? v : null;

/* The carrier list, with each slug computed once rather than 8,000 times.
 *
 * lib/carryon.js answers "does this bag fit this airline" one bag at a time,
 * which is what a model page needs, and lib/carryon-index.js builds the
 * airline→bags direction at build time for the airline pages. This is the
 * third orientation and the one a filter wants: bag→airlines, so a request
 * naming two carriers costs two hash lookups per bag instead of two geometry
 * comparisons. Like the haystack, it depends only on the deploy and is
 * therefore paid for on the cold start and never again. */
const CARRIERS = carryOnAirlines.map((airline) => ({
  airline,
  slug: airlineSlug(airline),
  name: airline.name,
}));

/* One empty set, shared by the ~6,000 bags that state fewer than three axes.
 * airlineFit() abstains on those and so does this: an unmeasured bag is not a
 * bag that fails, which is why an airline filter reports how many it could not
 * judge rather than quietly returning a shorter list. */
const NO_FIT: ReadonlySet<string> = new Set();

function carriersFitting(b: Bag): ReadonlySet<string> {
  if (!b.dims_cm || b.dims_cm.length < 3) return NO_FIT;
  const out = new Set<string>();
  for (const c of CARRIERS) {
    if (airlineFit(b, c.airline).carryOn) out.add(c.slug);
  }
  return out;
}

/* Sorted descending and read as height/width/depth: an underseat allowance is
 * about the shape of the bag, not about which axis the brand called which. */
function fitsUnderseat(b: Bag) {
  if (!b.dims_cm || b.dims_cm.length < 3) return false;
  const d = [...(b.dims_cm as number[])].sort((x, y) => y - x);
  return d[0]! <= 40 && d[1]! <= 30 && d[2]! <= 20;
}

const ROWS: Row[] = (bags as Bag[]).map((b) => {
  const volume_l = num(b.volume_l);
  const price_min = num(b.price_min);
  const weight_g = num(b.weight_g);
  const linear_cm = num(b.linear_cm);
  const laptop_in = num(b.laptop_in);
  const fitting = carriersFitting(b);
  /* The one feature nothing extracted: it is measured, here, against the
   * airline table — see FIT_FEATURE in lib/labels.js. Injected into the
   * feature set rather than given a control of its own so that the rail lists
   * it, counts it, searches it and files it in a saved filter with no new
   * machinery, and so that the reader meets it beside "Carry-on claimed",
   * which is the same sentence from the brand instead of from the numbers.
   *
   * Every tracked carrier, not most of them. A bag that clears 33 of 34 is a
   * bag with a specific airline problem, and the airline group above the
   * Features group is where that question gets answered. */
  const features = new Set<string>(b.features ?? []);
  if (fitting.size === CARRIERS.length) features.add(FIT_FEATURE);
  return {
    bag: b,
    id: b.id,
    // Field order agrees with haystack() as it stood in browse.js. It only
    // matters in that a term may span the join — "black tote" must not match a
    // bag that is a tote and comes in black — and keeping the order fixed keeps
    // that one edge answering the way it always has.
    hay: [b.brand, b.name, b.category, ...(b.materials ?? []),
          ...(b.features ?? []), ...(b.colors ?? []), ...(b.tags ?? [])]
      .join(' ').toLowerCase(),
    category: b.category,
    brand_slug: b.brand_slug,
    features,
    materials: new Set<string>(b.materials ?? []),
    colours: new Set<string>(b.color_families ?? []),
    in_stock: Boolean(b.in_stock),
    on_sale: Boolean(b.on_sale),
    // The cast is the one on `bags` above, run backwards: the filters work
    // through a loose record and the helper is typed against the catalogue's
    // own shape, and it is the same object either way.
    discount: b.on_sale ? saleDiscount(b as CatalogBag) : null,
    volume_l, price_min, weight_g, linear_cm, laptop_in,
    carryon: Boolean(linear_cm && linear_cm <= 115),
    underseat: fitsUnderseat(b),
    airlines: fitting,
    noDims: fitting === NO_FIT,
    featuresUnknown: b.features_source !== 'product-page',
    noColour: !(b.color_families ?? []).length,
    variant_count: b.variant_count ?? 0,
    brand: b.brand,
    name: b.name,
    gpl: weight_g && volume_l ? weight_g / volume_l : null,
    ppl: price_min && volume_l ? price_min / volume_l : null,
  };
});

/* ---------- sorting ---------- */

function nullsLast(x: number | null, y: number | null, dir: number) {
  if (x == null && y == null) return 0;
  if (x == null) return 1;
  if (y == null) return -1;
  return (x - y) * dir;
}

const SORTS: Record<string, (a: Row, b: Row) => number> = {
  brand: (a, b) => byLabel(a.brand, b.brand) || byLabel(a.name, b.name),
  'price-asc': (a, b) => nullsLast(a.price_min, b.price_min, 1),
  'price-desc': (a, b) => nullsLast(a.price_min, b.price_min, -1),
  'vol-asc': (a, b) => nullsLast(a.volume_l, b.volume_l, 1),
  'vol-desc': (a, b) => nullsLast(a.volume_l, b.volume_l, -1),
  'weight-asc': (a, b) => nullsLast(a.weight_g, b.weight_g, 1),
  gpl: (a, b) => nullsLast(a.gpl, b.gpl, 1),
  ppl: (a, b) => nullsLast(a.ppl, b.ppl, 1),
  /* Deepest cut first, and the default order of /sale/ — see the scope section
   * below. Everything not on sale has a null discount and sorts last, which is
   * what makes this a sane thing to offer on the front page too rather than a
   * control that only means something on one route. The tail is the whole
   * catalogue with nothing to say, so it falls back to the order the catalogue
   * is read in rather than to the order normalize.py happened to write. */
  discount: (a, b) =>
    nullsLast(a.discount, b.discount, -1) ||
    byLabel(a.brand, b.brand) || byLabel(a.name, b.name),
};

/* The catalogue pre-sorted eight ways, once, so a request never sorts anything.
 *
 * Filtering a stably-sorted array leaves the survivors in the order a stable
 * sort of just the survivors would have produced, so this is the same answer
 * the island used to compute — it is only the moment of paying for it that
 * moved. Eight arrays of references cost about half a megabyte and turn every
 * request into one linear pass. */
const ORDERS: Record<string, Row[]> = Object.fromEntries(
  Object.entries(SORTS).map(([key, cmp]) => [key, [...ROWS].sort(cmp)]),
);

/* ---------- facets ---------- */

/* Counted over the rows the page is about rather than over the current match
 * set, the same way the rail has always been built: a facet count that moved as
 * filters were applied would say how many bags are left, when the question it
 * is answering is how many exist. Static per deploy, so it is built once per
 * scope and only shipped on the boot request. */
const tally = (rows: Row[], of: (r: Row) => Iterable<string>) => {
  const m = new Map<string, number>();
  for (const r of rows) for (const v of of(r)) m.set(v, (m.get(v) ?? 0) + 1);
  return [...m.entries()].sort((a, b) => b[1] - a[1]);
};

const facetsOf = (rows: Row[]) => ({
  categories: tally(rows, (r) => (r.category ? [r.category] : [])),
  features: tally(rows, (r) => r.features),
  /* Slug, name, count — the same shape as brands, and for the same reason: the
   * rail prints the carrier's name and would otherwise have to reconstruct it
   * from the slug.
   *
   * Filed alphabetically rather than by count. Thirty-four carriers is a list
   * you scan for the one you are flying on Thursday, and ordering it by how
   * generous each one is would bury Ryanair — the carrier most worth checking
   * — at the bottom. Counted the same way every other facet is: over the rows
   * the page is about, so a brand page's rail says how many of that brand's
   * bags clear each limit. */
  airlines: (() => {
    const counted = new Map(tally(rows, (r) => r.airlines));
    return CARRIERS.filter((c) => counted.has(c.slug))
      .sort((a, b) => byLabel(a.name, b.name))
      .map((c) => [c.slug, c.name, counted.get(c.slug)!] as const);
  })(),
  materials: tally(rows, (r) => r.materials),
  colors: tally(rows, (r) => r.colours),
  /* Slug, display name, count — the rail prints the name and files by it, and
   * without the name here it would have to guess one from the slug. */
  brands: (() => {
    const m = new Map<string, { name: string; n: number }>();
    for (const r of rows) {
      const e = m.get(r.brand_slug) ?? { name: r.brand, n: 0 };
      e.n++;
      m.set(r.brand_slug, e);
    }
    return [...m.entries()]
      .sort((a, b) => byLabel(a[1].name, b[1].name))
      .map(([slug, e]) => [slug, e.name, e.n] as const);
  })(),
});

/* What the slider tracks span. Taken from the data rather than from constants
 * so a handle parked at either end means "no bound" rather than "a bound that
 * happens to equal the extreme" — which is what keeps an untouched slider out
 * of the URL and out of the filter. */
const boundsOf = (rows: Row[]) => ({
  volume_l: Math.max(0, ...rows.map((r) => r.volume_l ?? 0)),
  price_min: Math.max(0, ...rows.map((r) => r.price_min ?? 0)),
  weight_g: Math.max(0, ...rows.map((r) => r.weight_g ?? 0)),
});

/* The fields the coverage meter reports on. normalize.py already writes these
 * percentages for the whole catalogue — meta.coverage — and this recomputes
 * the same five for a scope, because "47% have a volume" is a fact about the
 * catalogue and the rail on a brand page is asking about the brand. */
const COVERAGE_FIELDS = ['volume_l', 'dims_cm', 'weight_g', 'laptop_in',
                         'price_min'] as const;

const coverageOf = (rows: Row[]) =>
  Object.fromEntries(COVERAGE_FIELDS.map((f) => {
    const have = rows.reduce((n, r) => n + (r.bag[f] != null ? 1 : 0), 0);
    return [f, { have, pct: rows.length ? Math.round((have / rows.length) * 100) : 0 }];
  }));

const statsOf = (rows: Row[]) => ({
  bags: rows.length,
  brands: new Set(rows.map((r) => r.brand_slug)).size,
  skus: rows.reduce((n, r) => n + r.variant_count, 0),
});

const FACETS = facetsOf(ROWS);
const BOUNDS = boundsOf(ROWS);
const COVERAGE = meta.coverage ?? {};

/* Read off the manifest rather than recounted, because these are the numbers
 * the rest of the site prints and they have to be the same ones. */
const STATS = {
  bags: meta.bag_count ?? ROWS.length,
  brands: meta.brand_count ?? null,
  skus: meta.sku_count ?? null,
};

/* ---------- scope ---------- */

/* A scope is the filter a page *is*, as opposed to the filters a reader sets.
 *
 * /brands/able-carry/ runs the same island as the front page with one answer
 * already settled by the URL path, so the brand rides on every request as
 * `scope=brand:able-carry` instead of as an ordinary `brand=` filter. The
 * difference is not decoration: a scope also decides what the rail is counting.
 * Facet counts, slider bounds, the coverage meter and the count line's
 * denominator all have to be about the brand, or the panel is describing a
 * catalogue the page is not showing — a colour with 900 bags behind it that
 * empties the grid when pressed, and a price track spanning four thousand
 * dollars of other people's luggage.
 *
 * /sale/ is the second one, and the reason this is a kind rather than a brand
 * slug: it is the same instrument with `on_sale` already settled, and its rail
 * has to count against the bags actually on sale for exactly the same reason.
 * Both spell out as an ordinary filter when you leave the page — `brand=` and
 * `sale=1` — which is what the "compare against everything" link in the rail
 * does with them.
 *
 * Everything a scope needs is fixed for the life of a deploy, so it is built on
 * first sight and kept. Bounded by the number of brands plus one, and only the
 * scopes anyone actually opens are ever built. */
type Boot = {
  facets: ReturnType<typeof facetsOf>;
  bounds: ReturnType<typeof boundsOf>;
  coverage: Record<string, unknown>;
  stats: Record<string, number | null>;
};

/** What a page is: the parameter as written, and the test it stands for. */
type Scope = { key: string; test: (r: Row) => boolean };

const SCOPES = new Map<string, Boot>();

function scoped(scope: Scope): Boot {
  let hit = SCOPES.get(scope.key);
  if (!hit) {
    const rows = ROWS.filter(scope.test);
    hit = {
      facets: facetsOf(rows),
      bounds: boundsOf(rows),
      coverage: coverageOf(rows),
      stats: statsOf(rows),
    };
    SCOPES.set(scope.key, hit);
  }
  return hit;
}

/** `brand:<slug>` or `sale`, or nothing. An unreadable scope is refused rather
 *  than ignored: dropping it would answer with the whole catalogue on a page
 *  whose heading promises one brand, which is the one wrong answer available. */
function readScope(raw: string | null) {
  if (!raw) return { ok: true as const, scope: null };
  if (raw === 'sale') {
    return { ok: true as const, scope: { key: raw, test: (r: Row) => r.on_sale } };
  }
  const at = raw.indexOf(':');
  const kind = at < 0 ? raw : raw.slice(0, at);
  const value = at < 0 ? '' : raw.slice(at + 1);
  if (kind !== 'brand' || !value) return { ok: false as const, scope: null };
  return {
    ok: true as const,
    scope: { key: raw, test: (r: Row) => r.brand_slug === value },
  };
}

/* ---------- the query ---------- */

type Query = {
  terms: string[];
  /* What the page is, from `scope` — not a filter the reader set and not one
   * they can clear. See the scope section above. */
  scope: Scope | null;
  cats: Set<string>;
  brands: Set<string>;
  feats: string[];
  mats: string[];
  colours: string[];
  /* Carriers by slug, every-of: two of them means a bag that clears both.
   * Any-of would answer "fits at least one airline", which is a question
   * nobody packs for. */
  airlines: string[];
  volMin: number | null; volMax: number | null;
  priceMin: number | null; priceMax: number | null;
  weightMin: number | null; weightMax: number | null;
  linearMax: number | null;
  /* Minimum, not maximum, because laptop_in is the largest machine a bag takes:
   * a reader with a 16" laptop wants everything that fits it or more. Same
   * reading as the laptop hub pages, which is deliberate — the filter and the
   * hub must not disagree about what "fits" means. */
  laptopMin: number | null;
  carryon: boolean;
  underseat: boolean;
  stock: boolean;
  sale: boolean;
  /* The reader's own saved ids. Not a filter on their own — they only decide
   * which matches are lifted out of the page and sent ahead of it — until
   * `favOnly`, which is a filter and is spelled in the URL like one. */
  favs: Set<string>;
  favOnly: boolean;
};

const RANGES = ['volMin', 'volMax', 'priceMin', 'priceMax',
                'weightMin', 'weightMax', 'linearMax', 'laptopMin'] as const;

const list = (p: URLSearchParams, k: string) =>
  (p.get(k) ?? '').split(',').map((s) => s.trim()).filter(Boolean);

function parse(p: URLSearchParams, scope: Scope | null): Query {
  const presets = new Set(list(p, 'preset'));
  const q: Query = {
    terms: (p.get('q') ?? '').toLowerCase().split(/\s+/).filter(Boolean),
    scope,
    cats: new Set(list(p, 'cat')),
    brands: new Set(list(p, 'brand')),
    feats: list(p, 'feat'),
    mats: list(p, 'mat'),
    colours: list(p, 'color'),
    airlines: list(p, 'airline'),
    volMin: null, volMax: null, priceMin: null, priceMax: null,
    weightMin: null, weightMax: null, linearMax: null, laptopMin: null,
    carryon: presets.has('carryon'),
    underseat: presets.has('underseat'),
    stock: p.get('stock') === '1',
    sale: p.get('sale') === '1',
    favs: new Set(list(p, 'fav').slice(0, MAX_FAVS)),
    favOnly: p.get('favonly') === '1',
  };
  /* A bound that is not a number is dropped rather than honoured. Taken
   * literally it would compare against NaN and empty the grid, which reads to
   * whoever hand-edited the URL as the catalogue being broken rather than as
   * their typo. */
  for (const k of RANGES) {
    if (!p.has(k)) continue;
    const n = Number(p.get(k));
    if (Number.isFinite(n)) q[k] = n;
  }
  return q;
}

/** True when the query asks anything at all — the boot payload rides on the
 *  unfiltered first page, and this is how that page is recognised. A scope is
 *  deliberately not counted: it is the page rather than a filter on it, and a
 *  brand page's own first request is exactly the one that needs the rail. */
const isFiltered = (q: Query) =>
  q.terms.length > 0 || q.cats.size > 0 || q.brands.size > 0 ||
  q.feats.length > 0 || q.mats.length > 0 || q.colours.length > 0 ||
  q.airlines.length > 0 ||
  RANGES.some((k) => q[k] != null) ||
  q.carryon || q.underseat || q.stock || q.sale || q.favOnly;

/* A line-for-line port of matches() in browse.js, down to the order of the
 * tests. Ranges use `!(x >= min)` rather than `x < min` so that a null fails
 * every comparison: a bag with no measured volume cannot be asserted to sit
 * inside a volume window, and reading it as zero would be an assertion. */
function matches(r: Row, q: Query) {
  // The scope is deliberately not tested here — it is not a filter, and the
  // loop below applies it first so that a bag belonging to another page cannot
  // be counted as one this page's filters excluded. See the caveat counters.
  //
  // The cheapest of the filters first, and the one most likely to reject:
  // "favourites only" is at most sixty bags out of eight thousand.
  if (q.favOnly && !q.favs.has(r.id)) return false;
  for (const t of q.terms) if (!r.hay.includes(t)) return false;
  if (q.cats.size && !q.cats.has(r.category)) return false;
  if (q.brands.size && !q.brands.has(r.brand_slug)) return false;
  if (q.feats.length && !q.feats.every((f) => r.features.has(f))) return false;
  if (q.mats.length && !q.mats.some((m) => r.materials.has(m))) return false;
  // Any-of, like materials: picking black and green means "comes in either".
  if (q.colours.length && !q.colours.some((c) => r.colours.has(c))) return false;
  /* Every-of, like features: a bag has to clear every carrier named, because an
   * itinerary is an and. A bag stating fewer than three axes has an empty set
   * and fails here — it is not being judged against the gauge and cannot be
   * offered as clearing it; how many were set aside that way is counted below
   * and printed above the grid. */
  if (q.airlines.length && !q.airlines.every((a) => r.airlines.has(a))) return false;
  if (q.stock && !r.in_stock) return false;
  if (q.sale && !r.on_sale) return false;
  if (q.volMin != null && !(r.volume_l! >= q.volMin)) return false;
  if (q.volMax != null && !(r.volume_l! <= q.volMax)) return false;
  if (q.priceMin != null && !(r.price_min! >= q.priceMin)) return false;
  if (q.priceMax != null && !(r.price_min! <= q.priceMax)) return false;
  if (q.weightMin != null && !(r.weight_g! >= q.weightMin)) return false;
  if (q.weightMax != null && !(r.weight_g! <= q.weightMax)) return false;
  if (q.linearMax != null && !(r.linear_cm! <= q.linearMax)) return false;
  // Spelled with an explicit null test rather than leaning on the comparison
  // the way the lines above do. A bag with no stated laptop size is an unknown,
  // not a no, and "fits 16 inches" must not answer with bags nobody has said
  // that about — which is the same rule the rest of the index runs on.
  if (q.laptopMin != null
      && !(r.laptop_in != null && r.laptop_in >= q.laptopMin)) return false;
  if (q.carryon && !r.carryon) return false;
  if (q.underseat && !r.underseat) return false;
  return true;
}

export const GET: APIRoute = ({ url }) => {
  const p = url.searchParams;

  const per = p.has('per') ? Number(p.get('per')) : PER_DEFAULT;
  if (!Number.isInteger(per) || per < 1 || per > PER_MAX) {
    return json({ error: `per must be an integer from 1 to ${PER_MAX}` }, 400);
  }
  const pageRaw = Number(p.get('page') ?? 0);
  const page = Number.isInteger(pageRaw) && pageRaw >= 0 ? pageRaw : 0;

  const scope = readScope(p.get('scope'));
  if (!scope.ok) {
    return json({ error: 'scope must be brand:<slug> or sale' }, 400);
  }

  const q = parse(p, scope.scope);
  const order = ORDERS[p.get('sort') ?? 'brand'] ?? ORDERS.brand!;

  const from = page * per;
  const to = from + per;

  /* One pass, not four. The page, the totals and both caveat counts are all
   * decided by the same per-bag verdict, so they are all taken from it — and
   * only the rows inside the requested window are ever held, which is what
   * keeps a query matching the whole catalogue from building an 8,000-element
   * array to send sixty of them.
   *
   * A matching favourite leaves through a different door. It is sent ahead of
   * the page, whole, and taken out of the pagination entirely — which is what
   * stops the same bag appearing twice, once pinned at the top and once in its
   * alphabetical place forty cards down. `total` still counts it, because the
   * count line above the grid answers "how many bags match", and a favourite
   * matches; `paged` is what the client's scroll arithmetic runs on. */
  /* Three filters exclude bags for want of a value rather than for having the
   * wrong one, and they do not share an unknown. A bag whose features were
   * never read off a product page cannot be said to lack a laptop sleeve; a bag
   * stating fewer than three axes cannot be said to fail a gauge. So the
   * measured filters — the airlines, and the derived carry-on feature that is
   * not an extracted one — count against the dimension caveat, and the stated
   * features count against theirs. */
  const statedFeats = q.feats.filter((f) => f !== FIT_FEATURE);
  const wantFeature = statedFeats.length > 0 || q.mats.length > 0;
  const wantColour = q.colours.length > 0;
  const wantDims = q.airlines.length > 0 || statedFeats.length !== q.feats.length;
  const wantFavs = q.favs.size > 0;
  const brandSet = new Set<string>();
  const pageRows: Row[] = [];
  const favRows: Row[] = [];
  let total = 0, paged = 0, skus = 0, noFeature = 0, noColour = 0, noDims = 0;

  for (const r of order) {
    /* Before anything is counted, because a scope is what the page *is*: on
     * /sale/ the eight thousand bags that are not discounted were never
     * candidates, and counting them among the ones a filter set aside made the
     * caveat below read "excluded 6,016" on a page holding 1,184 bags. It is
     * also the most selective test here by a wide margin. */
    if (q.scope && !q.scope.test(r)) continue;
    if (matches(r, q)) {
      total++;
      brandSet.add(r.brand_slug);
      skus += r.variant_count;
      if (wantFavs && q.favs.has(r.id)) {
        // Collected only for the first page: the strip is painted once per
        // answer and appending to it as the reader scrolls would mean sending
        // it again with every page.
        if (page === 0) favRows.push(r);
        continue;
      }
      if (paged >= from && paged < to) pageRows.push(r);
      paged++;
    } else {
      // Say so when a filter is dropping bags we simply have not established a
      // value for, rather than silently returning a shorter list.
      if (wantFeature && r.featuresUnknown) noFeature++;
      if (wantColour && r.noColour) noColour++;
      if (wantDims && r.noDims) noDims++;
    }
  }

  const body: Record<string, unknown> = {
    total,
    // How many of those matches are actually paged. Equal to `total` for
    // anyone with no favourites saved, which is why the client falls back to
    // `total` when it is absent.
    paged,
    page,
    per,
    bags: pageRows.map(project),
    ...(page === 0 && wantFavs ? { favs: favRows.map(project) } : {}),
    counts: { brands: brandSet.size, skus, noFeature, noColour, noDims },
    // Three integers, on every response rather than only at boot: the count
    // line reads "N of 8,079 bags" on every render, and a client that had to
    // remember the denominator from a request several filters ago is a client
    // that prints the wrong one after a reload. Scoped, the denominator is the
    // shelf the page is about — "12 of 34 Able Carry bags".
    stats: q.scope ? scoped(q.scope).stats : STATS,
  };

  /* The rail, the slider tracks and the coverage meter are the same on every
   * response, so they ship exactly once — on the boot request, which is the
   * unfiltered first page or anything asking for it by name. Folding them into
   * that request rather than giving them their own keeps the island's first
   * paint at one round trip. */
  if (p.get('boot') === '1' || (page === 0 && !isFiltered(q))) {
    const boot = q.scope
      ? scoped(q.scope)
      : { facets: FACETS, bounds: BOUNDS, coverage: COVERAGE };
    body.facets = boot.facets;
    body.bounds = boot.bounds;
    body.coverage = boot.coverage;
  }

  return json(body, 200, CACHE);
};

export const POST: APIRoute = () =>
  json({ error: 'GET with the filter as a query string' }, 405);
