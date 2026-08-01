/* POST /api/watch — create a pending price watch and send the opt-in email.
 *
 * The one write endpoint in the system. Nothing is ever sent to an address
 * that has not clicked the confirmation link: rows land with confirmed_at
 * null, and the matcher only ever selects confirmed rows.
 */

import type { APIRoute } from 'astro';
import { sql, token, validEmail } from '../../lib/db.ts';
import { confirmEmail, send } from '../../lib/email.ts';

export const prerender = false;

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });

/** Re-sending the confirmation is the obvious way to make us spam somebody, so
 *  a repeat signup for the same watch stays quiet unless the link has had time
 *  to go stale. */
const RESEND_AFTER_MINUTES = 15;

export const POST: APIRoute = async ({ request, url }) => {
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

    // Idempotent: the same address watching the same thing updates the token
    // in place instead of stacking rows. `xmax = 0` is the standard Postgres
    // tell for "this was an insert, not an update".
    const rows = await db`
      insert into subscriptions (email, criteria, confirm_token, unsub_token)
      values (${email}, ${JSON.stringify(criteria)}::jsonb, ${confirm}, ${token()})
      on conflict (lower(email), md5(criteria::text)) do update
        set confirm_token = case
              when subscriptions.confirmed_at is null
               and subscriptions.created_at
                     < now() - make_interval(mins => ${RESEND_AFTER_MINUTES})
              then excluded.confirm_token
              else subscriptions.confirm_token
            end
      returning id, confirm_token, confirmed_at, created_at,
                (xmax = 0) as inserted
    `;

    const row = rows[0];
    if (!row) return json({ error: 'could not save that watch' }, 500);

    if (row.confirmed_at) {
      return json({ ok: true, status: 'already-watching' });
    }

    const fresh =
      row.inserted ||
      Date.now() - new Date(row.created_at).getTime() >
        RESEND_AFTER_MINUTES * 60_000;

    if (fresh) {
      const confirmUrl = new URL(
        `/api/confirm?token=${row.confirm_token}`,
        url.origin,
      ).href;
      const mail = confirmEmail(confirmUrl, what);
      await send({ ...mail, to: email });
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
