/* POST /api/webhooks/resend — what became of the mail we sent.
 *
 * Resend reports delivery out of band: we hand it a message and it tells us
 * later whether that message landed, bounced, was reported as spam, or was
 * opened. Nothing in this system could hear any of that until this route
 * existed, which meant `sent_alerts` recorded intent and the actual outcome
 * was unobservable.
 *
 * Two jobs, and the second is the one that matters:
 *
 *   1. Record every event, keyed by Resend's message id, so the dashboard can
 *      show delivery and bounce rates rather than send counts.
 *   2. Suppress the mailbox on a hard bounce or a spam complaint. That is the
 *      part with teeth — a sending domain that keeps writing to dead addresses
 *      and collecting complaints gets throttled, and a suppression list is the
 *      only thing that stops the nightly doing exactly that, every night,
 *      forever.
 *
 * Configure in the Resend dashboard against RESEND_WEBHOOK_SECRET. With the
 * secret unset the route rejects everything, which is the safe direction for a
 * variable that is easy to forget on a new deployment — the same posture
 * DASHBOARD_PASSWORD takes in src/lib/dash.ts.
 */

import type { APIRoute } from 'astro';
import { sql } from '../../../lib/db.ts';
import { emailHash } from '../../../lib/email.ts';

export const prerender = false;

/* Svix's replay window. An event older than this is either a replay attack or
 * a webhook so late that its information is worthless; five minutes is Svix's
 * own default and there is no reason to differ. */
const TOLERANCE_SECONDS = 5 * 60;

/* Signature verification, by hand rather than via the svix package.
 *
 * The scheme is small enough to state completely: sign `${id}.${timestamp}.${body}`
 * with HMAC-SHA256 under the base64 secret, and compare against the `v1,` entries
 * in the svix-signature header. Doing it here keeps a dependency out of a route
 * that runs on every delivery report, and keeps the whole trust boundary of this
 * endpoint readable in one screen.
 *
 * The body must be the *raw* text. Parsing and re-serialising changes byte order
 * and whitespace and the signature stops matching — which is why this reads
 * request.text() and parses afterwards.
 */
/* `whsec_` is a human-readable prefix on the Resend dashboard, not part of the
 * key. A secret that is not valid base64 returns null and fails the whole
 * verification, deliberately: a misconfigured secret must never become a way
 * to get unsigned payloads accepted. */
function decodeSecret(secret: string) {
  const raw = secret.startsWith('whsec_') ? secret.slice(6) : secret;
  try {
    const binary = atob(raw);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return bytes;
  } catch {
    return null;
  }
}

async function verify(
  secret: string,
  id: string,
  timestamp: string,
  signatureHeader: string,
  body: string,
): Promise<boolean> {
  const at = Number(timestamp);
  if (!Number.isFinite(at)) return false;
  if (Math.abs(Date.now() / 1000 - at) > TOLERANCE_SECONDS) return false;

  const keyBytes = decodeSecret(secret);
  if (!keyBytes) return false;

  const key = await crypto.subtle.importKey(
    'raw', keyBytes, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign'],
  );
  const mac = await crypto.subtle.sign(
    'HMAC', key, new TextEncoder().encode(`${id}.${timestamp}.${body}`),
  );
  const expected = btoa(String.fromCharCode(...new Uint8Array(mac)));

  // The header carries a space-separated list so a secret can be rotated with
  // both keys live; any one match is a pass.
  for (const part of signatureHeader.split(' ')) {
    const [version, value] = part.split(',');
    if (version !== 'v1' || !value) continue;
    if (constantTimeEqual(value, expected)) return true;
  }
  return false;
}

/* Length is not secret — both operands are base64 of a fixed-width digest — so
 * an early return on mismatched length leaks nothing, and past that the compare
 * runs to the end regardless of where the first difference falls. */
function constantTimeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/* Every Resend payload echoes the recipient in `data.to`, and storing that
 * would put an address outside `subscriptions` — the one table an unsubscribe
 * knows how to empty. So the address is used for the suppression digest and
 * then dropped before the payload is written.
 *
 * `from`, `subject` and the bounce detail are kept: they are ours, or they are
 * the provider's description of what went wrong, and both are what makes a row
 * on the dashboard diagnosable rather than just a count. */
function redact(data: Record<string, unknown>): Record<string, unknown> {
  const { to, ...rest } = data;
  return rest;
}

/* Which failures are permanent.
 *
 * Resend reports `data.bounce.type` as Permanent, Transient or Undetermined.
 * A transient bounce is a full mailbox or a greylisting — the address is fine
 * and suppressing it would lose a real subscriber. Anything else suppresses.
 *
 * Undetermined counts as permanent on purpose, and it is the one judgement
 * call here: the asymmetry is that a wrongly suppressed reader can sign up
 * again and tell us, while a wrongly retained dead address is invisible and
 * compounds nightly into the reputation damage this table exists to prevent.
 */
function isPermanent(data: Record<string, unknown>): boolean {
  const bounce = data.bounce as { type?: string } | undefined;
  return (bounce?.type ?? '').toLowerCase() !== 'transient';
}

export const POST: APIRoute = async ({ request }) => {
  const secret = process.env.RESEND_WEBHOOK_SECRET;
  if (!secret) {
    console.error('[resend-webhook] RESEND_WEBHOOK_SECRET is not set — rejecting');
    return new Response(null, { status: 503 });
  }

  const svixId = request.headers.get('svix-id');
  const timestamp = request.headers.get('svix-timestamp');
  const signature = request.headers.get('svix-signature');
  if (!svixId || !timestamp || !signature) return new Response(null, { status: 400 });

  const body = await request.text();
  if (!(await verify(secret, svixId, timestamp, signature, body))) {
    console.error(`[resend-webhook] bad signature svix=${svixId}`);
    return new Response(null, { status: 401 });
  }

  let event: { type?: string; created_at?: string; data?: Record<string, unknown> };
  try {
    event = JSON.parse(body);
  } catch {
    return new Response(null, { status: 400 });
  }

  const type = typeof event.type === 'string' ? event.type : null;
  if (!type) return new Response(null, { status: 400 });

  const data = event.data ?? {};
  const providerId = typeof data.email_id === 'string' ? data.email_id : null;
  const recipients = Array.isArray(data.to) ? data.to : [];

  try {
    const db = sql();

    // `on conflict do nothing` on svix_id is the idempotency: Resend retries
    // any webhook it did not get a 2xx for, and a slow response here would
    // otherwise double-count on the dashboard.
    await db`
      insert into email_events (svix_id, provider_id, type, occurred_at, payload)
      values (
        ${svixId},
        ${providerId},
        ${type},
        ${event.created_at ?? null},
        ${JSON.stringify(redact(data))}::jsonb
      )
      on conflict (svix_id) do nothing
    `;

    const complaint = type === 'email.complained';
    const hardBounce = type === 'email.bounced' && isPermanent(data);

    if (complaint || hardBounce) {
      const reason = complaint ? 'complaint' : 'bounce';
      const bounce = data.bounce as
        { type?: string; subType?: string; message?: string } | undefined;
      const detail = complaint
        ? 'reported as spam'
        : [bounce?.type, bounce?.subType].filter(Boolean).join('/') || 'bounced';

      for (const address of recipients) {
        if (typeof address !== 'string') continue;
        await db`
          insert into suppressions (email_sha256, reason, detail)
          values (${await emailHash(address)}, ${reason}, ${detail})
          on conflict (email_sha256) do nothing
        `;
      }
      // No address and no digest in the log line: a digest is a stable
      // identifier for a person across log entries, which is the thing the
      // subscription-id convention exists to avoid.
      console.log(`[resend-webhook] suppressed ${recipients.length} (${reason})`);
    }
  } catch (err) {
    // 500 rather than swallowing, unlike /api/click. A dropped click is an
    // acceptable loss of a measurement; a dropped bounce is a mailbox we go on
    // writing to. Resend retries a non-2xx, and the insert is idempotent, so
    // failing loudly here is what makes the retry useful.
    console.error('[resend-webhook] insert failed:', (err as Error).message);
    return new Response(null, { status: 500 });
  }

  return new Response(null, { status: 204 });
};

export const GET: APIRoute = () => new Response(null, { status: 405 });
