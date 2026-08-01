/* The catalog the browse island loads.
 *
 * Emitted as a build artifact at /bags.json rather than copied into public/,
 * so there is exactly one copy of the data on disk and no prebuild step to
 * forget. At 214 models this is ~300 KB; the architecture note is that
 * client-side filtering over one file stays fine into the thousands, and the
 * answer past a few MB is to shard this endpoint per category rather than to
 * introduce a query service.
 */

import { bags, meta } from '../lib/catalog.js';

export const prerender = true;

export function GET() {
  return new Response(JSON.stringify({ meta, bags }), {
    headers: {
      'content-type': 'application/json; charset=utf-8',
      'cache-control': 'public, max-age=0, must-revalidate',
    },
  });
}
