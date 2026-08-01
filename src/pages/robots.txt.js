/* Generated rather than static so the sitemap URL follows `site` in
   astro.config.mjs — the domain is not settled yet and one forgotten hardcoded
   host is a silently broken sitemap. */

export const prerender = true;

export function GET({ site }) {
  const body = [
    'User-agent: *',
    'Allow: /',
    // The alert endpoints are stateful and have nothing to index.
    'Disallow: /api/',
    // Coverage audit pages: same data, addressed to whoever maintains the
    // index. They carry noindex too — this only saves the crawl.
    'Disallow: /internal/',
    '',
    `Sitemap: ${new URL('sitemap-index.xml', site).href}`,
    '',
  ].join('\n');

  return new Response(body, {
    headers: { 'content-type': 'text/plain; charset=utf-8' },
  });
}
