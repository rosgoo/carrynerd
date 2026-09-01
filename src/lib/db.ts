/* Postgres connection for the alert endpoints.
 *
 * This is the *only* database in the system. The catalog is static files;
 * nothing on a model page or the browse grid touches this. Import it only from
 * routes that set `prerender = false`, or the build will try to connect while
 * generating pages.
 *
 * Supabase, used as plain Postgres — no PostgREST, no anon key, no client-side
 * access. That is deliberate: this table holds email addresses, and reaching it
 * only from a server function with a connection string means there is no
 * row-level-security policy standing between the public internet and the
 * subscriber list. Nothing here is Supabase-specific, so moving to Neon, RDS or
 * a box in a cupboard is a connection-string change.
 */

import postgres from 'postgres';

let client: ReturnType<typeof postgres> | null = null;

export function sql() {
  if (client) return client;

  const url = process.env.DATABASE_URL;
  if (!url) {
    throw new Error(
      'DATABASE_URL is not set — the alerts plane needs a Postgres connection string',
    );
  }

  client = postgres(url, {
    // Serverless: every invocation is its own short-lived process, so a pool
    // of one is the whole pool. Anything larger just holds connections the
    // next invocation cannot see.
    max: 1,
    idle_timeout: 20,
    connect_timeout: 10,
    // Supabase's transaction-mode pooler (port 6543) multiplexes connections
    // and cannot carry prepared statements between them. Turning them off
    // costs a plan cache we would not benefit from anyway at this query volume.
    prepare: false,
  });
  return client;
}

/** URL-safe random token for confirm/unsubscribe links. */
export function token(bytes = 24): string {
  const buf = crypto.getRandomValues(new Uint8Array(bytes));
  return Array.from(buf, (b) => b.toString(16).padStart(2, '0')).join('');
}

/* Deliberately permissive. The confirmation email is the real validator — an
   address that does not exist simply never completes double opt-in — so this
   only needs to reject obvious junk before we spend a send on it. */
const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;

export function validEmail(value: unknown): value is string {
  return typeof value === 'string' && value.length <= 254 && EMAIL.test(value);
}

/** Never log an address. Subscription ids are the identifier everywhere else. */
export function redact(email: string): string {
  const at = email.indexOf('@');
  return at < 1 ? '<redacted>' : `${email[0]}***${email.slice(at)}`;
}

/* Who is asking, to the precision the throttles actually need and no further.
 *
 * Two endpoints cap what one requester can do in a day — /api/request-brand,
 * so the crawl queue cannot be filled by one person, and /api/watch, so the
 * Resend quota and the sending domain's reputation cannot be spent by one
 * script. Both need to tell requesters apart. Neither needs to know who they
 * are, and the events table next door makes a point of holding no address at
 * all — so the IP is never stored, only a digest of it with the UTC date mixed
 * in. That date is what keeps this from being a durable identifier: the same
 * person hashes to a different value tomorrow, so the column cannot be used to
 * follow anybody across days even by whoever holds the database.
 *
 * Unsalted beyond the date, for the same reason `suppressions` is unpeppered:
 * anyone who can read these tables can already read the plaintext addresses in
 * `subscriptions` beside them, so a shared secret would buy nothing and add one
 * more thing a deployment can lose.
 *
 * It lives here rather than in either route because the two have to agree on
 * the digest: they are the same person to both tables or to neither, and two
 * copies of this that drift are two caps that quietly stop counting the same
 * requester.
 */
export async function requesterHash(ctx: {
  request: Request;
  clientAddress: string;
}): Promise<string> {
  // Vercel sets x-forwarded-for; clientAddress is the adapter's reading of the
  // same thing and is the fallback for running this anywhere else. Neither is
  // trustworthy on its own — a forged header just splits one requester into
  // several, which costs the forger nothing and protects nobody else, and that
  // is the honest ceiling of an unauthenticated endpoint. Refusing a burst from
  // one source is what these caps are for; refusing a distributed one is the
  // WAF's job, where the platform can see a client across requests.
  //
  // clientAddress is a getter that throws where the adapter cannot supply one
  // (it is only defined on server-rendered routes), so it is read here inside
  // the try rather than destructured out of the context at the top of a
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
