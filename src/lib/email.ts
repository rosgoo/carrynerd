/* Resend delivery. Free tier is 100/day, which is plenty until it isn't;
   swapping to SES is a change to `send()` and nothing else.

   Note for the privacy posture: Resend retains recipient addresses in its own
   delivery logs. That is normal and unavoidable with any ESP, but it means the
   vendor list is also the disclosure list.

   Two things happen around every send, and neither is optional:

   - Suppression is checked first. A mailbox that hard-bounced or reported us
     as spam is never written to again, whatever the subscriptions table says.
     Repeatedly mailing dead addresses is the single fastest way to lose a
     sending domain, and before this the system had no mechanism that could
     ever have stopped doing it.
   - The returned message id is kept. It is the only join between a send of
     ours and the webhook that later says what became of it; discarding it, as
     this file used to, made delivery unobservable by construction.

   alerts/match.py does the same two things in Python for drop alerts. The
   duplication is deliberate — it is two runtimes, not two designs — but the
   hash and the suppression rule have to stay in step, so they are stated in
   one place each and cross-referenced. */

import { sql } from './db.ts';

const ENDPOINT = 'https://api.resend.com/emails';

export interface Mail {
  to: string;
  subject: string;
  html: string;
  text: string;
  /** List-Unsubscribe target, so clients can offer one-click unsubscribe. */
  unsubscribeUrl?: string;
}

export interface SendRecord {
  kind: 'confirm' | 'alert';
  subscriptionId: number;
}

export interface SendResult {
  /** Resend's message id, or null when the send was suppressed. */
  id: string | null;
  /** True when a prior bounce or complaint stopped this before the API call. */
  suppressed: boolean;
}

/* The suppression key, and the one in alerts/match.py's `email_sha256` must
   produce the same digest for the same address or a bounce recorded by one
   sender is invisible to the other. Lowercased and trimmed, nothing else — no
   gmail dot-folding or plus-stripping, because normalising beyond case would
   suppress addresses the reader considers distinct and we have no standing to
   decide otherwise. */
export async function emailHash(address: string): Promise<string> {
  const bytes = new TextEncoder().encode(address.trim().toLowerCase());
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return [...new Uint8Array(digest)]
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
}

/** Has this address hard-bounced or reported us? Fails *open* — see below. */
export async function isSuppressed(address: string): Promise<boolean> {
  try {
    const rows = await sql()`
      select 1 from suppressions where email_sha256 = ${await emailHash(address)}
    `;
    return rows.length > 0;
  } catch (err) {
    // Open rather than closed, which is the unusual direction and deserves the
    // reason: a database blip must not silently stop every confirmation email
    // in the system. The cost of failing open is that we mail one known-bad
    // address during an outage; the cost of failing closed is a signup flow
    // that appears to work and never delivers, with nothing anywhere saying so.
    console.error('[email] suppression check failed:', (err as Error).message);
    return false;
  }
}

export async function send(mail: Mail, record?: SendRecord): Promise<SendResult> {
  const key = process.env.RESEND_API_KEY;
  if (!key) throw new Error('RESEND_API_KEY is not set');

  if (await isSuppressed(mail.to)) {
    // Subscription id, never the address — same rule as everywhere else.
    console.log(`[email] suppressed ${record?.kind ?? 'send'} sub=${record?.subscriptionId ?? '?'}`);
    return { id: null, suppressed: true };
  }

  const headers: Record<string, string> = {};
  if (mail.unsubscribeUrl) {
    headers['List-Unsubscribe'] = `<${mail.unsubscribeUrl}>`;
    headers['List-Unsubscribe-Post'] = 'List-Unsubscribe=One-Click';
  }

  const res = await fetch(ENDPOINT, {
    method: 'POST',
    headers: {
      authorization: `Bearer ${key}`,
      'content-type': 'application/json',
    },
    body: JSON.stringify({
      from: process.env.ALERT_FROM ?? 'carrynerd <alerts@carrynerd.com>',
      to: [mail.to],
      subject: mail.subject,
      html: mail.html,
      text: mail.text,
      ...(Object.keys(headers).length ? { headers } : {}),
    }),
  });

  if (!res.ok) {
    // The body can echo the recipient address, so it does not go in the error.
    throw new Error(`Resend rejected the send: ${res.status}`);
  }

  const body = (await res.json().catch(() => ({}))) as { id?: string };
  const id = typeof body.id === 'string' ? body.id : null;

  if (record) {
    try {
      await sql()`
        insert into email_sends (provider_id, kind, subscription_id)
        values (${id}, ${record.kind}, ${record.subscriptionId})
        on conflict (provider_id) do nothing
      `;
    } catch (err) {
      // The mail is already gone; losing its log line is not worth failing the
      // request over. It costs one message's worth of delivery reporting.
      console.error('[email] could not log send:', (err as Error).message);
    }
  }

  return { id, suppressed: false };
}

const esc = (s: string) =>
  String(s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]!,
  );

export function confirmEmail(confirmUrl: string, what: string): Mail {
  const text = [
    `Confirm your carrynerd price watch`,
    ``,
    `You asked to be told when ${what} drops in price.`,
    `Confirm that here — the watch does not start until you do:`,
    ``,
    confirmUrl,
    ``,
    `If this wasn't you, ignore this email. Nothing was saved that a`,
    `single unconfirmed click can't undo, and we won't write again.`,
  ].join('\n');

  const html = `
<div style="font-family:ui-monospace,SFMono-Regular,Menlo,monospace;max-width:520px;color:#16181a">
  <p style="letter-spacing:.1em;text-transform:uppercase;font-size:11px;color:#8e938f">carrynerd</p>
  <h1 style="font-size:18px;margin:0 0 14px">Confirm your price watch</h1>
  <p style="font-size:13px;line-height:1.7">
    You asked to be told when ${esc(what)} drops in price. The watch does not
    start until you confirm.
  </p>
  <p style="margin:22px 0">
    <a href="${esc(confirmUrl)}"
       style="background:#e8430a;color:#fff;padding:11px 18px;text-decoration:none;font-size:13px">
      Confirm the watch
    </a>
  </p>
  <p style="font-size:11px;color:#8e938f;line-height:1.7">
    If this wasn't you, ignore this email. We won't write again.
  </p>
</div>`.trim();

  return { to: '', subject: 'Confirm your carrynerd price watch', html, text };
}
