/* GET /api/catalog?ids=… — a bag, or a hundred, by id.
 *
 * /api/browse answers "which bags", and its pages now carry the photography,
 * the storefront link and the per-variant rows with them, so nothing on the
 * page routinely asks this any more. It stays for the two jobs the query
 * endpoint cannot do: looking a bag up when only its id is known — the island's
 * fallback for a drawer opened on a record it was never sent — and being a
 * plain, honest public API for anyone who wants one bag rather than a filter.
 *
 * The cap below is the load-bearing half of that: at 100 ids a request, the
 * whole catalogue is eighty-odd requests rather than one, which is the
 * difference between copying it and crawling it. Making the crawl *expensive*
 * is not this file's job — that is rate limiting and BotID in the Vercel
 * dashboard, which is where the platform can see a client across requests and
 * this function cannot. The cap is what makes bulk access something you have to
 * do repeatedly and visibly.
 */

import type { APIRoute } from 'astro';
import { bags } from '../../lib/catalog.js';
import type { Bag, Variant } from '../../lib/types.d.ts';

export const prerender = false;

const json = (body: unknown, status = 200, cache?: string) =>
  new Response(JSON.stringify(body), {
    status,
    headers: {
      'content-type': 'application/json',
      ...(cache ? { 'cache-control': cache } : {}),
    },
  });

/* The answer changes once per deploy and never in between, so it is worth
 * caching hard at the edge. stale-while-revalidate means a deploy never makes
 * anybody wait for a cold function — the previous answer serves while the new
 * one is fetched behind it. */
const CACHE = 'public, s-maxage=3600, stale-while-revalidate=86400';

/* The same ceiling /api/browse puts on a page, for the same reason: whichever
 * door the catalogue is asked through, it leaves a hundred bags at a time. */
const MAX_IDS = 100;

const VARIANT_FIELDS = [
  'sku', 'title', 'color', 'color_family', 'price', 'compare_at',
  'available', 'image', 'image_bg',
] as const satisfies readonly (keyof Variant)[];

const pick = (obj: Variant, keys: readonly (keyof Variant)[]) => {
  const out: Record<string, unknown> = {};
  for (const k of keys) if (obj[k] !== undefined) out[k] = obj[k];
  return out;
};

/* Built at module scope, not per request. The catalog import is already in this
 * function's bundle, so the map costs one pass on a cold start and nothing at
 * all on the warm invocations that serve almost every request. */
const BY_ID = new Map<string, Bag>(bags.map((b) => [b.id, b]));

const project = (b: Bag) => ({
  id: b.id,
  image: b.image,
  image_bg: b.image_bg,
  url: b.url,
  variants: (b.variants ?? []).map((v) => pick(v, VARIANT_FIELDS)),
});

export const GET: APIRoute = ({ url }) => {
  const raw = (url.searchParams.get('ids') ?? '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);

  if (!raw.length) return json({ error: 'expected ?ids=<comma-separated>' }, 400);
  if (raw.length > MAX_IDS) {
    return json({ error: `at most ${MAX_IDS} ids per request` }, 400);
  }

  /* Unknown ids are dropped rather than refused. A caller asking about a bag
   * this build retired is not making a bad request, and failing the whole batch
   * over one dead id would take the other ninety-nine with it. The response is
   * keyed by id; anything absent from it was simply never here. */
  const out = [];
  for (const id of raw) {
    const bag = BY_ID.get(id);
    if (bag) out.push(project(bag));
  }

  return json({ bags: out }, 200, CACHE);
};

export const POST: APIRoute = () =>
  json({ error: 'GET with ?ids=<comma-separated>' }, 405);
