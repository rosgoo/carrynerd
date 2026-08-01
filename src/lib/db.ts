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
