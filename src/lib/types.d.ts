/* The shape of data/bags.json, stated once.
 *
 * bags.json is a 40MB build-time import, and TypeScript declines to infer a
 * structure from a file that size — it hands back `any[]`, which then spreads
 * an implicit `any` through every `.map((bag) => …)` on every page. Writing the
 * shape here is what gives `astro check` something to check.
 *
 * These are descriptive, not authoritative: normalize.py writes the file and is
 * the source of truth. A field is optional here when the pipeline genuinely
 * leaves it off some records — feed-sourced fields absent on aggregator rows,
 * and the price/photo fields added by later passes — and `| null` when it is
 * always present but not always known.
 */

/** One purchasable SKU: a colourway, a size, or both. */
export interface Variant {
  sku: string | null;
  title: string | null;
  color: string | null;
  /** The colour bucket a filter can match. Null until classified — the gap the
   *  internal coverage audit reports on. */
  color_family: string | null;
  size: string | null;
  price: number | null;
  compare_at: number | null;
  grams: number | null;
  image: string | null;
  available: boolean;
  barcode?: string | null;
  options?: Record<string, string> | null;
  variant_id?: number;
  /** Written by image_bg.py, absent until it has run. */
  image_bg?: string | null;
}

/** A product photo with its intrinsic size. */
export interface BagImage {
  src: string;
  w: number;
  h: number;
}

/** One model: the merge of every storefront product that is the same bag. */
export interface Bag {
  id: string;
  slug: string;
  name: string;
  brand: string;
  brand_slug: string;
  url: string;
  source: string;
  fetched_at: string;
  updated_at: string | null;

  category: string;
  category_source: string;
  features: string[];
  features_source: string;
  materials: string[];
  sizes: string[];
  tags: string[];
  colors: string[];
  color_families: string[];

  price_min: number | null;
  price_max: number | null;
  currency: string;
  currency_source: string;
  on_sale: boolean;
  in_stock: boolean;

  /** Sorted largest-axis-first — brands disagree about ordering. */
  dims_cm: number[] | null;
  dims_source: string | null;
  linear_cm: number | null;
  volume_l: number | null;
  volume_source: string | null;
  weight_g: number | null;
  weight_source: string | null;
  laptop_in: number | null;

  image: string | null;
  variants: Variant[];
  variant_count: number;
  merged_from: number;

  /* Feed-sourced. Present on Shopify records, absent on aggregator ones. */
  handle?: string;
  vendor?: string;
  product_type?: string | null;
  shopify_id?: number;
  gtin?: string;
  created_at?: string;
  published_at?: string;
  images?: BagImage[];
  axis_names?: string[];
  variant_axes?: Record<string, string[]>;
  variants_collapsed_from?: number;
  merged_ids?: string[];
  merged_urls?: string[];

  /* Written by track_prices.py after the crawl, so absent from a build that
   * has not tracked yet. `price_direction` is the net move over the tracker's
   * drop window (5 days), not the last run: a price that fell and held writes
   * one ledger row and would otherwise stop being a sale the next night, while
   * one that fell and rebounded nets out and carries no direction at all.
   * `previous_price` is accordingly what it cost entering that window. */
  price_direction?: 'up' | 'down' | null;
  previous_price?: number | null;
  price_changed_at?: string | null;
  lowest_ever?: number | null;
  at_lowest?: boolean;

  /** The colour this photo was shot against — written by image_bg.py. */
  image_bg?: string | null;
}

/** How a ruling file was applied on the last pipeline run. */
export interface OverrideReport {
  file: string;
  entries: number;
  used: number;
  /** Rulings in the file that matched nothing in the catalog. */
  stale: { brand: string; key: string }[];
}

/** The `meta` block normalize.py writes alongside the bags. */
export interface CatalogMeta {
  generated_from?: string;
  generated_at?: string;
  bag_count?: number;
  brand_count?: number;
  sku_count?: number;
  categories?: string[];
  products_merged?: number;
  models_collapsed?: number;
  currencies?: Record<string, number>;
  currency_sources?: Record<string, number>;
  /** Per-field fill rate across the whole catalog, keyed by field name — the
   *  numbers /about publishes and api/browse recomputes per scope. Both halves
   *  are written: `pct` is what a reader is shown, `have` is what makes it
   *  checkable against `bag_count`. */
  coverage?: Record<string, { have: number; pct: number }>;
  enriched?: number;
  features_scanned?: number;
  /** Reason -> count, for products the pipeline declined to index. Reasons are
   *  prefixed by family (`not-a-bag:*`, `unclassified:*`), which is what lets
   *  /about total them by kind without listing every leaf. */
  rejected?: Record<string, number>;
  enrich_gaps?: Record<string, unknown>;
  image_bg_sampled?: number;
  /** Absent until the pipeline has run since the override files appeared. */
  overrides?: Record<string, OverrideReport>;
  [key: string]: unknown;
}

export interface CatalogPayload {
  meta?: CatalogMeta;
  bags?: Bag[];
}

/** Every model from one maker, name-sorted. */
export interface Brand {
  slug: string;
  name: string;
  bags: Bag[];
}

/** One product normalize.py declined to index, from data/rejected.json. */
export interface RejectedProduct {
  brand_slug: string;
  title: string;
  url: string;
  reason: string;
  product_type: string | null;
  tags: string[];
  reviewed: boolean;
}

export interface RejectedPayload {
  generated_at?: string;
  counts?: Record<string, number>;
  rejected?: RejectedProduct[];
}

/** One row of data/price-history.jsonl: a price as it stood on a crawl. */
export interface PriceRow {
  ts: string;
  bag_id: string;
  brand: string;
  name: string;
  sku: string | null;
  color: string | null;
  price: number | null;
  compare_at: number | null;
  available: boolean;
  /** Only rows that recorded a move carry these. */
  direction?: 'up' | 'down';
  prev_price?: number;
  delta?: number;
}

/** The cheapest price on one day the bag moved. */
export interface PricePoint {
  date: string;
  price: number;
}
