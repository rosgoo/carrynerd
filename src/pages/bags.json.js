/* The catalog the browse island loads.
 *
 * Emitted as a build artifact at /bags.json rather than copied into public/,
 * so there is exactly one copy of the data on disk and no prebuild step to
 * forget. At 214 models this is ~300 KB; the architecture note is that
 * client-side filtering over one file stays fine into the thousands, and the
 * answer past a few MB is to shard this endpoint per category rather than to
 * introduce a query service.
 *
 * This is now a *projection*, not the whole record. data/bags.json is a build
 * input and keeps everything the feed offered — galleries, publication dates,
 * per-variant option axes, store ids — because re-fetching it is expensive and
 * currently impossible. None of that is read by browse.js, and shipping it
 * would have put the browse payload into the tens of megabytes at 3,662
 * models. Build-time consumers (the model pages, the sitemap) import from
 * lib/catalog.js and still see the full record.
 *
 * Keep FIELDS in step with what browse.js actually touches. Anything it reads
 * and this omits comes back as undefined and fails a filter silently, so if a
 * facet starts returning nothing, look here first.
 */

import { bags, meta } from '../lib/catalog.js';

export const prerender = true;

const FIELDS = [
  'id', 'slug', 'name', 'brand', 'brand_slug', 'category', 'url', 'currency',
  'image', 'image_bg', 'in_stock', 'on_sale', 'price_min', 'price_max',
  'volume_l', 'volume_source', 'dims_cm', 'dims_source', 'linear_cm',
  'weight_g', 'weight_source', 'laptop_in', 'features', 'features_source',
  'materials', 'colors', 'color_families', 'tags', 'variant_count',
];

const VARIANT_FIELDS = [
  'sku', 'title', 'color', 'color_family', 'price', 'compare_at',
  'available', 'image', 'image_bg', 'pct',
];

const pick = (obj, keys) => {
  const out = {};
  for (const k of keys) if (obj[k] !== undefined) out[k] = obj[k];
  return out;
};

export function GET() {
  const slim = bags.map((b) => ({
    ...pick(b, FIELDS),
    variants: (b.variants ?? []).map((v) => pick(v, VARIANT_FIELDS)),
  }));

  return new Response(JSON.stringify({ meta, bags: slim }), {
    headers: {
      'content-type': 'application/json; charset=utf-8',
      'cache-control': 'public, max-age=0, must-revalidate',
    },
  });
}
