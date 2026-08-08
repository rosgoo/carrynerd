/* POST /api/request-brand — record a reader asking for a brand we do not have.
 *
 * The second write endpoint, and a much smaller one than /api/watch: there is
 * no confirmation flow here because there is nothing to confirm. A price watch
 * is a promise to mail somebody repeatedly and needs double opt-in before the
 * first send; a brand request is one inbound message, and the address on it is
 * optional, used once to answer, and belongs to no list. Making a reader
 * confirm an address before we are allowed to *receive* their suggestion would
 * be double opt-in pointed the wrong way round.
 *
 * What it does have is the same problem /api/click has — a public,
 * unauthenticated write whose whole value is the count it produces. A brand
 * requested forty times is what decides the next crawl, so anything that can
 * insert forty rows for free can decide it instead. Three cheap defences,
 * none of which can fail an honest request: a bot filter on the user agent, a
 * honeypot field, and a daily cap per requester.
 */

import type { APIRoute } from 'astro';
import { sql, validEmail } from '../../lib/db.ts';

export const prerender = false;

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });

/* Enough for the longest real maker's name, and short enough that the column
 * cannot be used as free storage. Rejected rather than truncated, unlike the
 * click endpoint's cap(): a truncated brand name is a different brand, and
 * this row is read by a person who would have no way to tell. */
const NAME_MAX = 80;
const URL_MAX = 200;

/* Per requester per UTC day. High enough that nobody naming every bag brand
 * they own in one sitting hits it, low enough that stuffing the queue costs
 * real addresses. */
const DAILY_CAP = 10;

const BOT = /bot|crawler|spider|crawling|headless|preview|lighthouse|pingdom|slurp/i;

const text = (value: unknown, max: number): string | null => {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim().replace(/\s+/g, ' ');
  return trimmed && trimmed.length <= max ? trimmed : null;
};

/* Stored as given if it is a real http(s) URL, and dropped otherwise rather
 * than repaired. A reviewer pastes this into a browser; a value that looks
 * like a URL and is not one wastes exactly the time this field was meant to
 * save. Bare hostnames are the common honest case, so they get the scheme
 * added rather than being thrown away. */
const storefront = (value: unknown): string | null => {
  const raw = text(value, URL_MAX);
  if (!raw) return null;
  try {
    const url = new URL(/^https?:\/\//i.test(raw) ? raw : `https://${raw}`);
    // A hostname with no dot is not a public storefront — it is somebody
    // typing the brand's name again into the wrong box.
    if (!url.hostname.includes('.')) return null;
    return url.href.slice(0, URL_MAX);
  } catch {
    return null;
  }
};

/* Who asked, to the precision the two jobs below actually need and no further.
 *
 * The daily cap and the one-request-per-brand index both need to tell
 * requesters apart. Neither needs to know who they are, and the events table
 * next door makes a point of holding no address at all — so the IP is never
 * stored, only a digest of it with the UTC date mixed in. That date is what
 * keeps this from being a durable identifier: the same person hashes to a
 * different value tomorrow, so the column cannot be used to follow anybody
 * across days even by whoever holds the database.
 *
 * Unsalted beyond the date, for the same reason `suppressions` is unpeppered:
 * anyone who can read this table can already read the plaintext addresses in
 * `subscriptions` beside it, so a shared secret would buy nothing and add one
 * more thing a deployment can lose.
 */
async function requesterHash(ctx: { request: Request; clientAddress: string }): Promise<string> {
  // Vercel sets x-forwarded-for; clientAddress is the adapter's reading of the
  // same thing and is the fallback for running this anywhere else. Neither is
  // trustworthy on its own — a forged header just splits one requester into
  // several, which costs the forger nothing and protects nobody else, and that
  // is the honest ceiling of an unauthenticated endpoint.
  //
  // clientAddress is a getter that throws where the adapter cannot supply one
  // (it is only defined on server-rendered routes), so it is read here inside
  // the try rather than destructured out of the context at the top of the
  // handler, where the throw would take the whole request with it.
  let fallback = '';
  try {
    fallback = ctx.clientAddress ?? '';
  } catch {}
  const forwarded = ctx.request.headers.get('x-forwarded-for')?.split(',')[0]?.trim();
  const ip = forwarded || fallback || 'unknown';
  const day = new Date().toISOString().slice(0, 10);
  const digest = await crypto.subtle.digest(
    'SHA-256',
    new TextEncoder().encode(`${day}|${ip}`),
  );
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

export const POST: APIRoute = async (ctx) => {
  const { request } = ctx;
  let body: Record<string, unknown>;
  try {
    body = await request.json();
  } catch {
    return json({ error: 'expected a JSON body' }, 400);
  }

  const name = text(body.name, NAME_MAX);
  if (!name) {
    return json({ error: 'which brand? a name is the one thing we need' }, 400);
  }

  /* Honeypot. A field with no label, hidden from the layout and from the
     accessibility tree, that a form-filling script sees and a reader cannot.
     Answered with the success body rather than an error, because telling a bot
     which field gave it away is telling it how to pass. */
  const trap = typeof body.company === 'string' ? body.company.trim() : '';
  if (trap) return json({ ok: true, status: 'recorded' });

  if (BOT.test(request.headers.get('user-agent') ?? '')) {
    return json({ ok: true, status: 'recorded' });
  }

  // Optional — but if they went to the trouble of typing one, a typo means we
  // silently never answer. Worth one sentence back.
  const email = body.email == null || body.email === '' ? null : String(body.email).trim();
  if (email !== null && !validEmail(email)) {
    return json({ error: "that doesn't look like an email address" }, 400);
  }

  const url = storefront(body.url);

  try {
    const db = sql();
    const who = await requesterHash(ctx);

    /* One statement, because the cap and the insert have to agree: checking
     * first and inserting after leaves a window where a burst of concurrent
     * requests each read a count under the cap and then all insert.
     *
     * The conflict clause is an upgrade rather than a no-op. Someone who asks
     * for a brand, then finds its storefront and asks again with the URL,
     * should end up with the URL recorded — but still one row, because the
     * queue counts rows and they are still one person asking once.
     *
     * Which is why the cap is `under the limit OR already asked for this one`
     * and not the count alone. The cap exists to stop the queue being filled;
     * a repeat adds nothing to fill it with, and gating it on the count too
     * means the reader who hits the limit and then goes to find the storefront
     * for their first request is turned away for it.
     */
    const rows = await db`
      with allowed as (
        select count(*) < ${DAILY_CAP}
               or bool_or(lower(btrim(name)) = lower(${name})) as ok
          from brand_requests where ip_hash = ${who}
      )
      insert into brand_requests (name, url, email, ip_hash)
      select ${name}, ${url}, ${email}, ${who} from allowed where ok
      on conflict (ip_hash, lower(btrim(name))) do update
        set url   = coalesce(excluded.url, brand_requests.url),
            email = coalesce(excluded.email, brand_requests.email)
      returning id
    `;

    // No row means the `where ok` filtered the insert out — the cap, and the
    // only case that gets a distinct answer. A conflict still returns its row.
    if (!rows[0]) {
      return json(
        { error: "that's a lot of brands for one day — try again tomorrow" },
        429,
      );
    }

    // The request id, never the address. Same rule as the watch endpoint.
    console.log(`brand request id=${rows[0].id}`);
    return json({ ok: true, status: 'recorded' });
  } catch (err) {
    console.error('brand request failed:', (err as Error).message);
    return json({ error: 'could not save that request right now' }, 500);
  }
};

export const GET: APIRoute = () =>
  json({ error: 'POST a brand name, optionally with a url and an email' }, 405);
