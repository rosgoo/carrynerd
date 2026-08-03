/* POST /api/click — record a product view or a buy click.
 *
 * The referral rate lives here: buy_click over product_view, grouped however
 * the question is being asked. Written by scripts/analytics.js via sendBeacon,
 * which means three things this route has to respect.
 *
 * One: it is fire-and-forget. Nobody reads the response, so it returns 204 and
 * says nothing. A failure here must never surface to a reader who is, at that
 * exact moment, in the middle of leaving for a seller's site.
 *
 * Two: sendBeacon sets its own content-type (text/plain for a string body), so
 * the type header is not worth checking. request.json() parses the body text
 * regardless, which is the only thing we actually need.
 *
 * Three: it is a public, unauthenticated write, and the numbers it produces are
 * the ones the index will be judged on. Everything below is about making junk
 * expensive to insert — a strict allowlist on both enums, hard length caps, and
 * a bot filter — without ever making an honest click fail.
 */

import type { APIRoute } from 'astro';
import { sql } from '../../lib/db.ts';

export const prerender = false;

/* Both enums are closed sets, defined by the two senders in
 * scripts/analytics.js. Anything else is not a client of ours and is dropped
 * rather than stored: a free-text `placement` column would let a stranger write
 * whatever they liked into the report. */
const NAMES = new Set(['product_view', 'buy_click', 'brand_click']);
const PLACEMENTS = new Set([
  'product_page', 'offers_table', 'browse_overlay',
  // Where a brand_click can come from. Kept in the same set rather than a
  // second one because the column is the same column; the pairing that matters
  // (which name goes with which placement) is enforced by the two senders in
  // scripts/analytics.js, and a mismatched pair here is inert rather than
  // dangerous — it groups under itself in the report and is visibly odd.
  'brand_page',
]);

/* Long enough for the longest real brand, model or bag id in the catalog, short
 * enough that the column cannot be used as free storage. Deliberately a
 * truncation rather than a rejection — a bag with a 200-character name is our
 * data problem, not a reason to lose the click. */
const cap = (value: unknown, max = 120): string | null => {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  return trimmed ? trimmed.slice(0, max) : null;
};

/* Prices are the only number, and an implausible one is a sign the payload was
 * not written by us. Nulled rather than clamped: a wrong price silently
 * recorded is worse than an absent one. */
const price = (value: unknown): number | null => {
  const n = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(n) && n >= 0 && n < 100_000 ? n : null;
};

/* Bots that run JavaScript would otherwise inflate product_view and leave
 * buy_click alone, which is the one failure mode that bends the rate rather
 * than just adding noise to it. This catches the declared ones. It cannot catch
 * a crawler that lies about its user agent, and it is not trying to — see the
 * note about honest headroom in the table comment. */
const BOT = /bot|crawler|spider|crawling|headless|preview|lighthouse|pingdom|slurp/i;

/* The catalog is not consulted to check that bag_id is real. It could be —
 * data/bags.json is right there — but pulling a megabyte of JSON into this
 * function's bundle would put a cold start in front of every click to save us
 * from rows that the allowlists above already make useless. An unknown bag_id
 * simply groups under itself and is obvious in the report. */

export const POST: APIRoute = async ({ request }) => {
  // 204 for everything, including every rejection below. The client cannot act
  // on a failure and a distinct error code would only tell someone probing the
  // endpoint which of their guesses got closest.
  const done = () => new Response(null, { status: 204 });

  try {
    if (BOT.test(request.headers.get('user-agent') ?? '')) return done();

    const body = await request.json();
    if (!body || typeof body !== 'object') return done();

    const name = cap(body.name, 32);
    if (!name || !NAMES.has(name)) return done();

    const placement = cap(body.placement, 32);
    if (placement && !PLACEMENTS.has(placement)) return done();

    await sql()`
      insert into events (name, brand, bag_id, placement, seller, model, price)
      values (
        ${name},
        ${cap(body.brand)},
        ${cap(body.bag_id)},
        ${placement},
        ${cap(body.seller)},
        ${cap(body.model)},
        ${price(body.price)}
      )
    `;
  } catch (err) {
    // Swallowed on purpose. A dropped measurement is an acceptable loss; a
    // 500 on the path a reader takes to leave the site is not.
    //
    // But swallowed loudly. Silence here is indistinguishable from a missing
    // DATABASE_URL, and the failure mode that costs the most is the one where
    // every click has been quietly returning 204 into no database for a month.
    // Nothing reader-facing changes; this only reaches the function log.
    console.error('[click] dropped:', err instanceof Error ? err.message : err);
  }

  return done();
};
