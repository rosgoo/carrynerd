/* Unsubscribe — one click, no login, and it actually deletes the row.
 *
 * Not a flag, not a soft delete: the address is gone from the database. The
 * sent_alerts rows cascade with it, because their only job was to stop us
 * mailing the same drop twice and there is nobody left to mail.
 *
 * POST exists for RFC 8058 one-click unsubscribe, which mail clients send
 * without a human ever seeing the page.
 */

import type { APIRoute } from 'astro';
import { sql } from '../../lib/db.ts';

export const prerender = false;

async function drop(value: string | null): Promise<boolean> {
  if (!value) return false;
  const db = sql();
  const rows = await db`
    delete from subscriptions where unsub_token = ${value} returning id
  `;
  if (rows[0]) console.log(`watch deleted id=${rows[0].id}`);
  // A token that matches nothing is still "unsubscribed" from the reader's
  // point of view — most often it is a second click on the same link.
  return true;
}

export const GET: APIRoute = async ({ url, redirect }) => {
  try {
    await drop(url.searchParams.get('token'));
    return redirect('/watch/unsubscribed/', 302);
  } catch (err) {
    console.error('unsubscribe failed:', (err as Error).message);
    return redirect('/watch/invalid/', 302);
  }
};

export const POST: APIRoute = async ({ url }) => {
  try {
    await drop(url.searchParams.get('token'));
    return new Response(null, { status: 204 });
  } catch (err) {
    console.error('unsubscribe failed:', (err as Error).message);
    return new Response(null, { status: 500 });
  }
};
