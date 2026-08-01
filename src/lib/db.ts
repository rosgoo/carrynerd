/* Neon connection for the alert endpoints.
 *
 * This is the *only* database in the system. The catalog is static files;
 * nothing on a model page or the browse grid touches this. Import it only from
 * routes that set `prerender = false`, or the build will try to connect while
 * generating pages.
 */

import { neon } from '@neondatabase/serverless';

export function sql() {
  const url = process.env.DATABASE_URL;
  if (!url) {
    throw new Error(
      'DATABASE_URL is not set — the alerts plane needs a Neon connection string',
    );
  }
  return neon(url);
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
