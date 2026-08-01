/* Resend delivery. Free tier is 100/day, which is plenty until it isn't;
   swapping to SES is a change to `send()` and nothing else.

   Note for the privacy posture: Resend retains recipient addresses in its own
   delivery logs. That is normal and unavoidable with any ESP, but it means the
   vendor list is also the disclosure list. */

const ENDPOINT = 'https://api.resend.com/emails';

export interface Mail {
  to: string;
  subject: string;
  html: string;
  text: string;
  /** List-Unsubscribe target, so clients can offer one-click unsubscribe. */
  unsubscribeUrl?: string;
}

export async function send(mail: Mail): Promise<void> {
  const key = process.env.RESEND_API_KEY;
  if (!key) throw new Error('RESEND_API_KEY is not set');

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
      from: process.env.ALERT_FROM ?? 'calipered <alerts@calipered.com>',
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
}

const esc = (s: string) =>
  String(s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]!,
  );

export function confirmEmail(confirmUrl: string, what: string): Mail {
  const text = [
    `Confirm your calipered price watch`,
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
  <p style="letter-spacing:.1em;text-transform:uppercase;font-size:11px;color:#8e938f">calipered</p>
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

  return { to: '', subject: 'Confirm your calipered price watch', html, text };
}
