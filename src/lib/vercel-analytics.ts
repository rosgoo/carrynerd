/* Read side of Vercel Web Analytics.
 *
 * The pageview script in the layout writes; this reads the same data back out,
 * so the internal dashboard can show the top of the funnel next to the bottom
 * of it. Without this the two halves live in two tabs and the only number
 * anyone actually wants — how many of the people who arrived left for a seller
 * — cannot be computed at all.
 *
 * Everything here fails soft and returns null. The referral tables are the
 * point of that page and they come from our own Postgres; an expired token or
 * a rate limit upstream must degrade one section, not the page.
 *
 * https://vercel.com/docs/analytics/web-analytics-api
 */

const BASE = 'https://api.vercel.com/v1/query/web-analytics';

/* The route pattern, not a path. The Astro component sends computeRoute(), so
 * every model page reports as this one string rather than as several hundred
 * distinct URLs — which is exactly what makes "how many model pages were
 * looked at" a single query instead of a sum over the catalog. */
export const MODEL_ROUTE = '/bags/[brand]/[model]';

function credentials(): { token: string; project: string; team: string | null } | null {
  const token = process.env.VERCEL_ANALYTICS_TOKEN;
  // Vercel injects VERCEL_PROJECT_ID into deployments itself; it only has to be
  // set by hand when running this locally.
  const project = process.env.VERCEL_PROJECT_ID;
  if (!token || !project) return null;
  return { token, project, team: process.env.VERCEL_TEAM_ID || null };
}

export function configured(): boolean {
  return credentials() !== null;
}

/** YYYY-MM-DD, which is the only date format the API takes. */
const day = (offsetDays = 0) =>
  new Date(Date.now() - offsetDays * 86_400_000).toISOString().slice(0, 10);

async function query(path: string, params: Record<string, string | undefined>) {
  const creds = credentials();
  if (!creds) return null;

  const url = new URL(`${BASE}/${path}`);
  url.searchParams.set('projectId', creds.project);
  if (creds.team) url.searchParams.set('teamId', creds.team);
  for (const [k, v] of Object.entries(params)) if (v) url.searchParams.set(k, v);

  try {
    // The dashboard is one reader hitting refresh, so a short timeout is the
    // right trade: a slow upstream should cost a missing panel, not a page that
    // hangs until the function times out.
    const res = await fetch(url, {
      headers: { Authorization: `Bearer ${creds.token}` },
      signal: AbortSignal.timeout(6000),
    });
    if (!res.ok) {
      console.error(`[vercel-analytics] ${path} -> ${res.status} ${res.statusText}`);
      return null;
    }
    return await res.json();
  } catch (err) {
    console.error('[vercel-analytics]', err instanceof Error ? err.message : err);
    return null;
  }
}

export interface Traffic {
  pageviews: number;
  visitors: number;
  /** Pageviews of model pages only — the funnel step our own table also counts. */
  modelPageviews: number | null;
  daily: { day: string; pageviews: number; visitors: number }[];
  routes: { route: string; pageviews: number }[];
  referrers: { referrer: string; pageviews: number }[];
}

const rows = (result: unknown): Record<string, unknown>[] =>
  Array.isArray((result as { data?: unknown })?.data)
    ? ((result as { data: Record<string, unknown>[] }).data)
    : [];

const num = (v: unknown) => Number(v ?? 0) || 0;

export async function traffic(days: number): Promise<Traffic | null> {
  if (!configured()) return null;

  const since = day(days - 1);
  const until = day(0);
  const range = { since, until };

  // One round trip each, in parallel. Counts and aggregates are separate
  // endpoints upstream, so there is no single call that answers all of this.
  const [total, model, byDay, byRoute, byReferrer] = await Promise.all([
    query('visits/count', range),
    query('visits/count', { ...range, filter: `route eq '${MODEL_ROUTE}'` }),
    query('visits/aggregate', { ...range, by: 'day' }),
    query('visits/aggregate', { ...range, by: 'route', limit: '15' }),
    query('visits/aggregate', { ...range, by: 'referrerHostname', limit: '15' }),
  ]);

  // The totals are the only part worth failing on: without them there is no
  // funnel to draw, and a panel of empty tables would read as "no traffic"
  // rather than "not connected".
  if (!total?.data) return null;

  return {
    pageviews: num(total.data.pageviews),
    visitors: num(total.data.visitors),
    modelPageviews: model?.data ? num(model.data.pageviews) : null,
    daily: rows(byDay).map((r) => ({
      day: String(r.timestamp ?? '').slice(0, 10),
      pageviews: num(r.pageviews),
      visitors: num(r.visitors),
    })),
    routes: rows(byRoute)
      .map((r) => ({ route: String(r.route ?? '—'), pageviews: num(r.pageviews) }))
      .filter((r) => r.pageviews > 0),
    referrers: rows(byReferrer)
      .map((r) => ({
        // A direct visit has no referrer host; the API returns it empty rather
        // than omitting the row, and "" in a table reads as a bug.
        referrer: String(r.referrerHostname || '') || 'direct / none',
        pageviews: num(r.pageviews),
      }))
      .filter((r) => r.pageviews > 0),
  };
}
