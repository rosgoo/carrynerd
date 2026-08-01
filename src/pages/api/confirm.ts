/* GET /api/confirm?token=… — completes double opt-in.
 *
 * A GET that mutates, which is normally a smell, but it is the only shape an
 * email client can follow. The token is single-purpose, unguessable and does
 * nothing but flip one row from pending to confirmed.
 */

import type { APIRoute } from 'astro';
import { sql } from '../../lib/db.ts';

export const prerender = false;

export const GET: APIRoute = async ({ url, redirect }) => {
  const value = url.searchParams.get('token');
  if (!value) return redirect('/watch/invalid/', 302);

  try {
    const db = sql();
    const rows = await db`
      update subscriptions
         set confirmed_at = coalesce(confirmed_at, now())
       where confirm_token = ${value}
      returning id, unsub_token
    `;

    if (!rows[0]) return redirect('/watch/invalid/', 302);

    console.log(`watch confirmed id=${rows[0].id}`);
    return redirect(`/watch/confirmed/?u=${rows[0].unsub_token}`, 302);
  } catch (err) {
    console.error('confirm failed:', (err as Error).message);
    return redirect('/watch/invalid/', 302);
  }
};
