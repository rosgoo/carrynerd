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

/* Counted against the filter the reader has set, not against the catalogue.
 *
 * This reverses what stood here. The rail used to be counted over the rows the
 * page is about and nothing else — static per deploy, built once per scope,
 * shipped only on the boot request — on the argument that a count beside an
 * option answers how many bags exist with that property rather than how many
 * are left. The argument is coherent and it lost anyway: filtering to tomtoc
 * left a grid of 173 bags beside a Colour group offering nine hundred black
 * ones, and every reader who met that read it as the rail being broken rather
 * than as the rail answering a different question. A number printed next to a
 * control is read as a prediction of what pressing it does. Ours was not one.
 *
 * So the counts move with the filter, under the rule that keeps a multi-select
 * rail usable: each group is counted over the rows matching every *other*
 * active filter, with its own selection lifted. Counted against a match set
 * that already includes its own selection, ticking black would drop every other
 * colour to zero and a second colour could never be added — the rail becomes a
 * door that locks behind you. Lifted, "Blue — 412" goes on meaning "412 more if
 * you add blue".
 *
 * Lifted for the groups that read as any-of, that is. Features and airlines are
 * every-of — two ticked means a bag with both, not either — so lifting their
 * own selection would print the pool the group is choosing from instead of what
 * another tick would do, and with one carrier ticked the airline group would
 * sit there showing precisely the catalogue-wide numbers this change exists to
 * remove. Those two are counted over the match set itself, where "Ryanair — 40"
 * means forty of the bags on screen also clear Ryanair. Which of the two a
 * group gets is `lift` in GROUPS, down in the query section beside the matcher
 * it is a decomposition of.
 *
 * Laptop is lifted too, and for the reason rather than the shape: it is one-of
 * rather than any-of — laptopMin holds a single rung — so a second choice does
 * not narrow the first, it replaces it. Counted with itself in place, the four
 * rungs you did not pick would read nought or read the pool above the one you
 * did, and the group would stop being able to say what moving from 15" to 16"
 * costs. Lifted, that is exactly what it says.
 *
 * What does not move is which options are printed and the order they come in.
 * That is still settled once per scope, from the scope's own rows — re-ordering
 * by the live count would shuffle the list under the cursor on every keystroke,
 * and dropping the options that fell to zero would make the rail change height
 * while it is being read. A zero ships as a zero and the client dims it; see
 * paintFacetCounts() in scripts/browse.js. */
const tally = (rows: Row[], of: (r: Row) => Iterable<string>) => {
  const m = new Map<string, number>();
  for (const r of rows) for (const v of of(r)) m.set(v, (m.get(v) ?? 0) + 1);
  return [...m.entries()].sort((a, b) => b[1] - a[1]);
};

/* The laptop group's rungs, and the one facet whose options are written here
 * rather than read off the catalogue.
 *
 * laptop_in has 29 distinct values in the data because brands state 15.6 and
 * 16.2 and 16.9, and a group 29 rows deep is a group nobody reads. The sizes
 * people actually own cluster on the whole inches, and because the filter is a
 * minimum — see laptopMin in the query section — "at least 16" already collects
 * the 16.2. Five rungs, so five rows. This is the same list the rail used to
 * spell out as <option>s in BrowseShell.astro; it lives here now so that the
 * options and the counts beside them cannot disagree about how many there are. */
const LAPTOP_STEPS = [13, 14, 15, 16, 17];

/* Which options a scope prints and the order it prints them in — everything
 * about the rail that a request cannot change. The numbers that ride beside
 * them are counted per request and married back on by dress() below; what is
 * kept here is only the ordering the scope-wide count produced, so that the
 * most common option stays at the top of its group however far the filter has
 * narrowed things. */
const shapeOf = (rows: Row[]) => ({
  categories: tally(rows, (r) => (r.category ? [r.category] : [])).map(([v]) => v),
  features: tally(rows, (r) => r.features).map(([v]) => v),
  /* Slug and name — the same shape as brands, and for the same reason: the
   * rail prints the carrier's name and would otherwise have to reconstruct it
   * from the slug.
   *
   * Filed alphabetically rather than by count. Thirty-four carriers is a list
   * you scan for the one you are flying on Thursday, and ordering it by how
   * generous each one is would bury Ryanair — the carrier most worth checking
   * — at the bottom. Listed the same way every other facet is: over the rows
   * the page is about, so a brand page's rail offers the carriers that brand's
   * bags have anything to say about. */
  airlines: (() => {
    const counted = new Map(tally(rows, (r) => r.airlines));
    return CARRIERS.filter((c) => counted.has(c.slug))
      .sort((a, b) => byLabel(a.name, b.name))
      .map((c) => [c.slug, c.name] as const);
  })(),
  /* Written as strings, like every other option list, because the first column
   * of a facet is the value the rail puts in data-v and the reader's URL puts
   * after `laptopMin=` — and those are the same string or the client cannot
   * find its own count.
   *
   * Filtered to the rungs the scope can reach, the same way the lists above are
   * built from the scope's own rows. Every rung survives on the whole catalogue;
   * what this drops is the 17" row on a brand that makes slings, which would
   * otherwise sit there reading nought for the life of the deploy. A rung that
   * merely counts nought under the current filter is a different thing and is
   * kept — that is the per-request number, and the client dims it. */
  laptop: LAPTOP_STEPS
    .filter((n) => rows.some((r) => r.laptop_in != null && r.laptop_in >= n))
    .map(String),
  materials: tally(rows, (r) => r.materials).map(([v]) => v),
  colors: tally(rows, (r) => r.colours).map(([v]) => v),
  /* Slug and display name — the rail prints the name and files by it, and
   * without the name here it would have to guess one from the slug. */
  brands: (() => {
    const m = new Map<string, string>();
    for (const r of rows) if (!m.has(r.brand_slug)) m.set(r.brand_slug, r.brand);
    return [...m.entries()].sort((a, b) => byLabel(a[1], b[1]));
  })(),
});

type Shape = ReturnType<typeof shapeOf>;

/** One tally per group, keyed the way the wire is. Handed to the request loop
 *  empty and filled as it goes — see the single pass in GET. */
type Counts = Record<keyof Shape, Map<string, number>>;

const noCounts = (): Counts => ({
  categories: new Map(), features: new Map(), airlines: new Map(),
  laptop: new Map(), materials: new Map(), colors: new Map(), brands: new Map(),
});

/** The scope's option list with this request's numbers written into it. The
 *  arrays are the ones the rail has always been drawn from — value and count,
 *  or slug, name and count where the rail prints a name — so nothing about the
 *  wire changed except how often it is sent and what the last column means. An
 *  option nothing matched is kept and sent as a zero rather than dropped. */
const dress = (shape: Shape, c: Counts) => ({
  categories: shape.categories.map((v) => [v, c.categories.get(v) ?? 0]),
  features: shape.features.map((v) => [v, c.features.get(v) ?? 0]),
  airlines: shape.airlines.map(([slug, name]) => [slug, name, c.airlines.get(slug) ?? 0]),
  laptop: shape.laptop.map((v) => [v, c.laptop.get(v) ?? 0]),
  materials: shape.materials.map((v) => [v, c.materials.get(v) ?? 0]),
  colors: shape.colors.map((v) => [v, c.colors.get(v) ?? 0]),
  brands: shape.brands.map(([slug, name]) => [slug, name, c.brands.get(slug) ?? 0]),
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

const SHAPE = shapeOf(ROWS);
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
 *
 * /carry-on/ryanair/ is the third, and the first whose scope is a measurement
 * rather than a field: its rows are the bags whose stated dimensions clear that
 * carrier's published frame. The counting argument is the same one and the
 * numbers are starker than on any shelf. The catalogue's price track runs to
 * $10,999 and the dearest bag clearing Ryanair's gauge is $1,999, so five
 * sixths of an unscoped slider is cases the page cannot show; the Category
 * group says 583 daypacks rather than 2,708; and the coverage meter reads 100%
 * on dimensions, because a bag stating fewer than three axes is not on this
 * page at all.
 *
 * It parts company with the other two in one place. A brand page drops its
 * Brand group because a second brand ticked under it can only empty the grid;
 * the airline group stays, because the airline filter is every-of and a second
 * carrier ticked on top of this one is an itinerary rather than a
 * contradiction. Only the page's own carrier goes, and it goes where the rail
 * is drawn — see paintFacets() in scripts/browse.js. Hand-write it back into
 * the query string and nothing moves either way: `.every` over a set the row
 * has already cleared is idempotent, which is why the scope and the ordinary
 * filter naming the same carrier cannot disagree.
 *
 * All three spell out as an ordinary filter when you leave the page — `brand=`,
 * `sale=1`, `airline=` — which is what the "compare against everything" link in
 * the rail does with them.
 *
 * What a scope settles is fixed for the life of a deploy, so it is built on
 * first sight and kept. Bounded by the vocabulary the site links to — a brand
 * apiece, a carrier apiece, and the sale — and only the scopes anyone actually
 * opens are ever built. The 34 carriers are the expensive ones to hold and
 * still cost little: an entry keeps the option lists and four numbers rather
 * than the rows they were counted over, so a carrier's boot is a few hundred
 * brand names and change, not the two thousand bags that cleared its gauge.
 * The facet counts left this cache when they started moving with the filter —
 * see the facets section — but the scope is still what decides the universe
 * they are counted inside, which is the load-bearing half: a brand page's rail
 * counts within that brand's shelf whatever the reader has ticked on top of
 * it. */
type Boot = {
  /* Not the counts — those move with the filter now and are taken per request.
   * What a scope still settles for the life of the deploy is which options its
   * rail offers at all, which is the half of the rail that never had to be
   * recomputed and still does not. */
  shape: Shape;
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
      shape: shapeOf(rows),
      bounds: boundsOf(rows),
      coverage: coverageOf(rows),
      stats: statsOf(rows),
    };
    SCOPES.set(scope.key, hit);
  }
  return hit;
}

/** `brand:<slug>`, `airline:<slug>` or `sale`, or nothing. An unreadable scope
 *  is refused rather than ignored: dropping it would answer with the whole
 *  catalogue on a page whose heading promises one brand, which is the one wrong
 *  answer available. */
/* The two vocabularies a scope may name, built once from the rows and the
 * carrier table.
 *
 * SCOPES is a cache keyed by the scope string, and until now nothing checked
 * that the string named anything: `scope=brand:qqq1` minted an entry, built it
 * over zero rows and kept it, and so did qqq2. Each one is small — option
 * lists, bounds, coverage and four numbers, never rows — but the count had no
 * ceiling, and this endpoint is crawled hard enough that a firewall rule was
 * written for it. Two sets and a membership test turn an unbounded map into
 * one bounded by what exists: the brands in the index, the carriers in the
 * table, and sale.
 *
 * The check belongs here rather than in scoped() because an unknown slug is a
 * bad request and not an empty page — 400 already says so and the caller
 * already has the branch. A brand leaving the catalogue stops being a valid
 * scope on the next deploy, which is the same moment its page stops existing. */
const BRAND_SLUGS = new Set(ROWS.map((r) => r.brand_slug));
const AIRLINE_SLUGS = new Set(CARRIERS.map((c) => c.slug));

function readScope(raw: string | null) {
  if (!raw) return { ok: true as const, scope: null };
  if (raw === 'sale') {
    return { ok: true as const, scope: { key: raw, test: (r: Row) => r.on_sale } };
  }
  const at = raw.indexOf(':');
  const kind = at < 0 ? raw : raw.slice(0, at);
  const value = at < 0 ? '' : raw.slice(at + 1);
  if (!value) return { ok: false as const, scope: null };
  if (kind === 'brand') {
    if (!BRAND_SLUGS.has(value)) return { ok: false as const, scope: null };
    return {
      ok: true as const,
      scope: { key: raw, test: (r: Row) => r.brand_slug === value },
    };
  }
  /* The same membership test the airline filter runs, off the same set, which
   * is what keeps /carry-on/ryanair/ and `?airline=ryanair` describing one list
   * of bags rather than two that nearly agree. A bag stating fewer than three
   * axes is in no carrier's set at all — see NO_FIT — so it is absent from
   * every airline page rather than failing one, which is the abstention the
   * whole carry-on half of this site is built on. */
  if (kind === 'airline') {
    if (!AIRLINE_SLUGS.has(value)) return { ok: false as const, scope: null };
    return {
      ok: true as const,
      scope: { key: raw, test: (r: Row) => r.airlines.has(value) },
    };
  }
  return { ok: false as const, scope: null };
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
   * hub must not disagree about what "fits" means.
   *
   * Filed here among the ranges, and parsed by RANGES below, although the rail
   * now draws it as a counted group like Colour or Brand — it is one rung or
   * none rather than a set, and `laptopMin=16` is what saved filters and shared
   * links already spell. Its test therefore sits in GROUPS rather than in
   * matchesFixed; see the laptop entry there. */
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

/* matches(), split where the rail needs it split.
 *
 * It used to be one function; it is now this half plus the table below, because
 * a facet count has to be able to ask what a row would do if one group of the
 * filter were lifted — see the facets section. Which bags match did not change
 * and must not: the two halves together are still the line-for-line port of
 * matches() in browse.js, and every test in both is an and, which is what makes
 * the seam movable without moving an answer. laptopMin moved across it when the
 * rail started drawing that group as counted rows — it is spelled the same and
 * now lives in the table below, because a group with a number beside every rung
 * is a group whose own choice has to be liftable.
 *
 * This half is the part no count is ever allowed to lift: the search box, the
 * two toggles, the remaining ranges and the presets. Ranges use `!(x >= min)`
 * rather than `x < min` so that a null fails every comparison — a bag with no
 * measured volume cannot be asserted to sit inside a volume window, and reading
 * it as zero would be an assertion. */
function matchesFixed(r: Row, q: Query) {
  // The scope is deliberately not tested here — it is not a filter, and the
  // loop below applies it first so that a bag belonging to another page cannot
  // be counted as one this page's filters excluded. See the caveat counters.
  //
  // The cheapest of the filters first, and the one most likely to reject:
  // "favourites only" is at most sixty bags out of eight thousand.
  if (q.favOnly && !q.favs.has(r.id)) return false;
  for (const t of q.terms) if (!r.hay.includes(t)) return false;
  if (q.stock && !r.in_stock) return false;
  if (q.sale && !r.on_sale) return false;
  if (q.volMin != null && !(r.volume_l! >= q.volMin)) return false;
  if (q.volMax != null && !(r.volume_l! <= q.volMax)) return false;
  if (q.priceMin != null && !(r.price_min! >= q.priceMin)) return false;
  if (q.priceMax != null && !(r.price_min! <= q.priceMax)) return false;
  if (q.weightMin != null && !(r.weight_g! >= q.weightMin)) return false;
  if (q.weightMax != null && !(r.weight_g! <= q.weightMax)) return false;
  if (q.linearMax != null && !(r.linear_cm! <= q.linearMax)) return false;
  if (q.carryon && !r.carryon) return false;
  if (q.underseat && !r.underseat) return false;
  return true;
}

/* The other half: the seven groups the rail draws as lists of options, each as
 * the two things a per-request count needs of it — the test its own selection
 * applies, and what to add to its tally off a row that got that far.
 *
 * `lift` is the reading of the group, and it decides which rows its tally sees.
 * A lifted group is any-of — one row has one category and one brand, and
 * materials and colours are `some`, so ticking a second option can only widen
 * the answer — and it is counted with its own selection set aside, which is
 * what makes "Blue — 412" a promise about adding blue rather than a zero.
 * Features and airlines are every-of: a second tick narrows, so the honest
 * number beside an option is how many of the bags already on screen also have
 * it, and their tallies see only rows that matched outright. Laptop is lifted
 * on the same reasoning arrived at from the other side — it is one-of, so a
 * second choice replaces the first rather than narrowing it. The facets section
 * argues all three at length.
 *
 * A bag stating fewer than three axes has an empty airline set and fails any
 * carrier named — it is not being judged against the gauge and cannot be
 * offered as clearing it; how many were set aside that way is counted in the
 * loop below and printed above the grid. */
type Group = {
  key: keyof Shape;
  lift: boolean;
  ok: (r: Row, q: Query) => boolean;
  count: (r: Row, m: Map<string, number>) => void;
};

const bump = (m: Map<string, number>, v: string) => m.set(v, (m.get(v) ?? 0) + 1);

const GROUPS: Group[] = [
  { key: 'categories', lift: true,
    ok: (r, q) => !q.cats.size || q.cats.has(r.category),
    count: (r, m) => { if (r.category) bump(m, r.category); } },
  { key: 'brands', lift: true,
    ok: (r, q) => !q.brands.size || q.brands.has(r.brand_slug),
    count: (r, m) => bump(m, r.brand_slug) },
  { key: 'features', lift: false,
    ok: (r, q) => q.feats.every((f) => r.features.has(f)),
    count: (r, m) => { for (const v of r.features) bump(m, v); } },
  { key: 'materials', lift: true,
    ok: (r, q) => !q.mats.length || q.mats.some((v) => r.materials.has(v)),
    count: (r, m) => { for (const v of r.materials) bump(m, v); } },
  // Any-of, like materials: picking black and green means "comes in either".
  { key: 'colors', lift: true,
    ok: (r, q) => !q.colours.length || q.colours.some((v) => r.colours.has(v)),
    count: (r, m) => { for (const v of r.colours) bump(m, v); } },
  // Every-of, like features: a bag has to clear every carrier named, because an
  // itinerary is an and.
  { key: 'airlines', lift: false,
    ok: (r, q) => q.airlines.every((a) => r.airlines.has(a)),
    count: (r, m) => { for (const v of r.airlines) bump(m, v); } },
  /* One-of and a minimum, which is what makes this group's tally the odd one:
   * a row does not fall under one rung, it falls under every rung it clears, so
   * a bag taking 16" is counted at 13, 14, 15 and 16. That is not double
   * counting, it is what the rows say — "fits 13 and up" is a promise that
   * bag keeps too — and it is why the five numbers come out as a decreasing
   * series with 13 at the top rather than as a histogram summing to the total.
   *
   * The null test is spelled out rather than left to the comparison, the way
   * the volume and weight windows leave theirs. A bag with no stated laptop
   * size is an unknown, not a no: it is matched by no rung, counted at no rung,
   * and "fits 16 inches" never answers with bags nobody has said that about.
   * Same rule the rest of the index runs on, and the reason the group's own
   * numbers now say what a note under the old <select> used to have to. */
  { key: 'laptop', lift: true,
    ok: (r, q) => q.laptopMin == null
                  || (r.laptop_in != null && r.laptop_in >= q.laptopMin),
    count: (r, m) => {
      if (r.laptop_in == null) return;
      for (const n of LAPTOP_STEPS) if (r.laptop_in >= n) bump(m, String(n));
    } },
];

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
    return json({ error: 'scope must be brand:<slug>, airline:<slug> or sale' }, 400);
  }

  const q = parse(p, scope.scope);
  const order = ORDERS[p.get('sort') ?? 'brand'] ?? ORDERS.brand!;

  const from = page * per;
  const to = from + per;

  /* One pass, still. The page, the totals, both caveat counts and all seven
   * facet tallies are decided by the same per-bag verdict, so they are all taken
   * from it — and only the rows inside the requested window are ever held, which
   * is what keeps a query matching the whole catalogue from building an
   * 8,000-element array to send sixty of them.
   *
   * The rail is what that verdict grew. Counting seven groups each with its own
   * selection lifted is seven questions per row, not one, and the obvious way to
   * answer them is seven passes with a different filter dropped each time. The
   * cheap way is this: ask which groups the row is *out* of, and note that a row
   * out of two or more counts nowhere at all, a row out of exactly one counts
   * only towards that one, and a row out of none is a match and counts towards
   * every group. So the test stops as soon as a second group rejects, and the
   * common case — a rail with nothing ticked — never rejects at all.
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
  /* The rail is painted from the answer to page 0 and from nothing else — a
   * scroll page appends cards below a panel that is already drawn — so the
   * tallies are only worth keeping on the request that will be read. It also
   * buys back the early exit: with nothing to count, the group tests can stop
   * at the first rejection the way the undivided matches() did. */
  const wantFacets = page === 0;
  const counts = noCounts();
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

    /* How many of the rail's groups rejected this row, and — while that is
     * exactly one — which. Two is the ceiling and means "more than one": past
     * it there is nothing left to learn, since the row is neither a match nor
     * anything's near miss. A row failing the fixed half, or an every-of group
     * that is never lifted, is out of everything by the same reckoning. */
    let out = 0;
    let only = -1;
    if (!matchesFixed(r, q)) {
      out = 2;
    } else {
      for (let i = 0; i < GROUPS.length; i++) {
        const g = GROUPS[i]!;
        if (g.ok(r, q)) continue;
        if (!g.lift || !wantFacets) { out = 2; break; }
        only = i;
        if (++out > 1) break;
      }
    }

    if (out === 0) {
      if (wantFacets) for (const g of GROUPS) g.count(r, counts[g.key]);
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
      /* Out of one group and one group only: not a match, but the row every
       * option in that group is counting — it is what ticking a second colour
       * would let back in. */
      if (out === 1) { const g = GROUPS[only]!; g.count(r, counts[g.key]); }
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

  /* The rail rides on every answer that repaints the grid, because its numbers
   * are now about the answer rather than about the deploy. It is the option list
   * of the scope with this request's tallies written into it — the list itself
   * is still the cached, per-scope one, so what is being paid for per request is
   * seven maps and the JSON, not a second count of who exists. */
  if (wantFacets) {
    body.facets = dress(q.scope ? scoped(q.scope).shape : SHAPE, counts);
  }

  /* The slider tracks and the coverage meter did not join them, and should not:
   * the largest volume in the catalogue and the share of bags stating a weight
   * are facts about the shelf, not about the filter, and a track that rescaled
   * itself as you dragged a handle would be a track you could not aim. So they
   * still ship exactly once — on the boot request, which is the unfiltered first
   * page or anything asking for it by name. Folding them into that request
   * rather than giving them their own keeps the island's first paint at one
   * round trip. */
  if (p.get('boot') === '1' || (page === 0 && !isFiltered(q))) {
    const boot = q.scope
      ? scoped(q.scope)
      : { bounds: BOUNDS, coverage: COVERAGE };
    body.bounds = boot.bounds;
    body.coverage = boot.coverage;
  }

  return json(body, 200, CACHE);
};

export const POST: APIRoute = () =>
  json({ error: 'GET with the filter as a query string' }, 405);
