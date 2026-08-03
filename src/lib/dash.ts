/* Password gate for the internal dashboard.
 *
 * The other /internal/ pages are unlinked, noindex and nothing more, which is
 * proportionate for a coverage audit: the worst case is a stranger reading a
 * list of bags we have not classified yet. Conversion numbers are a different
 * kind of secret — they are the commercial position of the index — so this one
 * gets an actual lock rather than a quiet URL.
 *
 * A shared password rather than accounts, because there is one reader. The
 * cookie is an HMAC of its own expiry keyed on that password, so there is no
 * session table to keep and no second secret to lose: changing the password
 * invalidates every outstanding cookie for free.
 *
 * Fails closed. With DASHBOARD_PASSWORD unset nothing authenticates, which is
 * the safe direction for a variable that is easy to forget on a new deployment.
 */

const enc = new TextEncoder();

export const COOKIE = 'dash';
const TTL_MS = 14 * 24 * 60 * 60 * 1000; // Two weeks; it is one person's laptop.

export function secret(): string | null {
  const value = process.env.DASHBOARD_PASSWORD;
  return value && value.length > 0 ? value : null;
}

async function hmac(key: string, message: string): Promise<string> {
  const k = await crypto.subtle.importKey(
    'raw', enc.encode(key), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign'],
  );
  const sig = await crypto.subtle.sign('HMAC', k, enc.encode(message));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

/* Both operands are hashed before comparison so the compare is over two
 * fixed-length hex strings. A plain === on the submitted password would leak
 * its length, and an early-exit compare would leak how much of it was right. */
async function equal(a: string, b: string): Promise<boolean> {
  const [ha, hb] = await Promise.all([hmac('cmp', a), hmac('cmp', b)]);
  let diff = 0;
  for (let i = 0; i < ha.length; i++) diff |= ha.charCodeAt(i) ^ hb.charCodeAt(i);
  return diff === 0;
}

export async function checkPassword(submitted: unknown): Promise<boolean> {
  const key = secret();
  if (!key || typeof submitted !== 'string') return false;
  return equal(submitted, key);
}

/** Cookie value: `<expiry>.<hmac of expiry>`. Self-describing, no server state. */
export async function mint(): Promise<string | null> {
  const key = secret();
  if (!key) return null;
  const exp = String(Date.now() + TTL_MS);
  return `${exp}.${await hmac(key, exp)}`;
}

export async function verify(token: unknown): Promise<boolean> {
  const key = secret();
  if (!key || typeof token !== 'string') return false;

  const dot = token.lastIndexOf('.');
  if (dot < 1) return false;

  const exp = token.slice(0, dot);
  const sig = token.slice(dot + 1);

  // Expiry is checked before the signature only because an expired cookie is
  // not an attack and does not deserve the hash.
  const at = Number(exp);
  if (!Number.isFinite(at) || at < Date.now()) return false;

  return equal(sig, await hmac(key, exp));
}

/* The gate, as one line at the top of every page behind it.
 *
 * Returns a redirect to hand straight back from the page, or null to carry on.
 * A helper rather than three copies of the same four lines, because the failure
 * mode of copies is that a page added later quietly gets none of them.
 */
export async function guard(ctx: {
  cookies: { get(name: string): { value: string } | undefined };
  url: URL;
  redirect(path: string, status?: 302 | 303): Response;
}): Promise<Response | null> {
  if (await verify(ctx.cookies.get(COOKIE)?.value)) return null;
  return ctx.redirect(`/internal/login/?next=${encodeURIComponent(ctx.url.pathname)}`, 303);
}

export const cookieOptions = {
  httpOnly: true,
  secure: true,
  sameSite: 'lax',
  path: '/',
  maxAge: Math.floor(TTL_MS / 1000),
} as const;
