/* POST /api/watch — create a pending price watch and send the opt-in email.
 *
 * The one write endpoint in the system. Nothing is ever sent to an address
 * that has not clicked the confirmation link: rows land with confirmed_at
 * null, and the matcher only ever selects confirmed rows.
 */

import type { APIRoute } from 'astro';
import { sql, token, validEmail, requesterHash } from '../../lib/db.ts';
import { confirmEmail, send } from '../../lib/email.ts';

export const prerender = false;

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });

/** Re-sending the confirmation is the obvious way to make us spam somebody, so
 *  a repeat signup for the same watch stays quiet unless the link has had time
 *  to go stale. Measured from confirm_sent_at — when a mail actually went —
 *  and not from created_at, which is also true of a row whose send failed. */
const RESEND_AFTER_MINUTES = 15;

/* How many watches one requester can file in a day.
 *
 * This is the only endpoint in the system that spends money and sending
 * reputation on being called, and until now nothing bounded it: every new
 * criteria is a new row and therefore a new confirmation mail, so one script
 * could empty the day's Resend quota — and a young domain that emits a burst
 * of unrequested confirmations is a domain that gets throttled, which breaks
 * alerts for everybody long after the burst stops.
 *
 * Set where a real reader will not meet it. Watching ten bags in one sitting
 * is enthusiasm and the cap should not be what ends it; the hundredth in an
 * afternoon from one address is not a reader.
 *
 * What this cannot do is bound the *total*: ten sources at the cap still spend
 * a hundred sends, and the free Resend tier is a hundred a day. It refuses the
 * cheap version of the attack — one source, one loop — which is the one a
 * launch actually attracts. A distributed one is the WAF's to refuse, in the
 * Vercel dashboard, where the platform can see a client across requests and
 * this function cannot.
 */
const DAILY_CAP = 25;

export const POST: APIRoute = async (ctx) => {
  const { request, url } = ctx;
  let body: Record<string, unknown>;
  try {
    body = await request.json();
  } catch {
    return json({ error: 'expected a JSON body' }, 400);
  }

  const email = typeof body.email === 'string' ? body.email.trim() : '';
  if (!validEmail(email)) {
    return json({ error: "that doesn't look like an email address" }, 400);
  }

  // Build criteria from a fixed allowlist. Whatever else the client sent is
  // discarded rather than stored — this column is read back by the matcher.
  const criteria: Record<string, string | number> = {};
  if (typeof body.bag_id === 'string' && body.bag_id.length <= 200) {
    criteria.bag_id = body.bag_id;
  }
  if (typeof body.brand_slug === 'string' && body.brand_slug.length <= 80) {
    criteria.brand_slug = body.brand_slug;
  }
  const maxPrice = Number(body.max_price);
  if (Number.isFinite(maxPrice) && maxPrice > 0 && maxPrice < 100_000) {
    criteria.max_price = Math.round(maxPrice);
  }
  if (!criteria.bag_id && !criteria.brand_slug) {
    return json({ error: 'pick a bag or a brand to watch' }, 400);
  }

  const what = criteria.bag_id
    ? String(criteria.bag_id).split('__')[1]?.replace(/-/g, ' ') ?? 'this bag'
    : `anything from ${criteria.brand_slug}`;

  try {
    const db = sql();
    const confirm = token();

    // Hashed once and reused: the digest has the UTC date in it, so computing
    // it twice either side of midnight would count against one day and insert
    // against the next.
    const who = await requesterHash(ctx);

    /* The cap, claimed before the subscription is touched.
     *
     * One statement, and the row is inserted by the same statement that checks
     * the count, because checking first and inserting after leaves a window
     * where a burst of concurrent requests each read a count under the cap and
     * then all proceed — which is precisely the shape of the traffic this is
     * here to refuse. Same reasoning, same construction, as the cap in
     * /api/request-brand.
     *
     * It counts attempts, not sends. A repeat for a watch that already exists
     * is suppressed further down and costs no mail, but it still costs a
     * request, and a loop that discovers it can retry the same watch for free
     * has found a way around the thing standing in front of the mailer.
     */
    const claim = await db`
      with allowed as (
        select count(*) < ${DAILY_CAP} as ok
          from watch_requests where ip_hash = ${who}
      )
      insert into watch_requests (ip_hash)
      select ${who} from allowed where ok
      returning id
    `;

    // No row means the `where ok` filtered the insert out: over the cap. The
    // one case here that gets a distinct answer, and it says what to do about
    // it — a reader who genuinely watched their way to the limit should not be
    // left wondering whether the form is broken.
    if (!claim[0]) {
      return json(
        { error: "that's a lot of watches for one day — try again tomorrow" },
        429,
      );
    }

    // `db.json()` and not `JSON.stringify(...)::jsonb`. postgres.js infers the
    // parameter type from the cast and serialises the value itself, so handing
    // it an already-serialised string encodes it twice: the column ends up
    // holding a jsonb *string* rather than an object, and every
    // `criteria->>'brand_slug'` in alerts/match.py reads NULL. Nothing errors —
    // the matcher simply selects no subscriptions, for anyone, forever.
    //
    // Idempotent: the same address watching the same thing updates the token
    // in place instead of stacking rows, and only when the window below says
    // a fresh link is owed — a repeat inside it must not invalidate the link
    // already sitting in somebody's inbox.
    const rows = await db`
      insert into subscriptions (email, criteria, confirm_token, unsub_token)
      values (${email}, ${db.json(criteria)}, ${confirm}, ${token()})
      on conflict (lower(email), md5(criteria::text)) do update
        set confirm_token = case
              when subscriptions.confirmed_at is null
               and (subscriptions.confirm_sent_at is null
                    or subscriptions.confirm_sent_at
                         < now() - make_interval(mins => ${RESEND_AFTER_MINUTES}))
              then excluded.confirm_token
              else subscriptions.confirm_token
            end
      returning id, confirm_token, confirmed_at, confirm_sent_at
    `;

    const row = rows[0];
    if (!row) return json({ error: 'could not save that watch' }, 500);

    if (row.confirmed_at) {
      return json({ ok: true, status: 'already-watching' });
    }

    /* Null covers the new row and the row whose last send failed, which is the
     * point of reading this column rather than created_at: those two are the
     * same situation — nothing has reached this address — and the old check
     * could not tell them apart, so a reader who hit a Resend outage and
     * immediately retried was answered by the suppression rule and never got a
     * mail at all. `xmax = 0` went with it; whether the row was inserted or
     * updated was only ever a proxy for this.
     *
     * `== null` and not `=== null`: on a database where the migration has not
     * been applied this reads undefined, and the strict form would make that
     * `fresh = false` — an endpoint that answers 200 and silently never mails
     * anybody. The loose form fails the other way, towards sending. */
    const fresh =
      row.confirm_sent_at == null ||
      Date.now() - new Date(row.confirm_sent_at).getTime() >
        RESEND_AFTER_MINUTES * 60_000;

    if (fresh) {
      const confirmUrl = new URL(
        `/api/confirm?token=${row.confirm_token}`,
        url.origin,
      ).href;
      const mail = confirmEmail(confirmUrl, what);
      // A suppressed address gets the same answer as everybody else. Saying
      // "that address bounced" to an unauthenticated poster would turn this
      // endpoint into a way to test whether a given mailbox is dead.
      await send({ ...mail, to: email }, { kind: 'confirm', subscriptionId: row.id });

      /* Stamped only now, on the far side of the send. If `send` threw we are
       * already in the catch below and this never ran, which leaves the column
       * null and the next attempt free to try again — the whole reason the
       * window is measured from a column the failure path cannot set. */
      await db`
        update subscriptions set confirm_sent_at = now() where id = ${row.id}
      `;
    }

    // Subscription id, never the address.
    console.log(`watch pending id=${row.id} sent=${fresh}`);
    return json({ ok: true, status: 'pending' });
  } catch (err) {
    console.error('watch failed:', (err as Error).message);
    return json({ error: 'could not save that watch right now' }, 500);
  }
};

export const GET: APIRoute = () =>
  json({ error: 'POST an email and a bag_id or brand_slug' }, 405);
