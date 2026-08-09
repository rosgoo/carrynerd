import { readFile, writeFile } from 'node:fs/promises';

import { defineConfig } from 'astro/config';
import sitemap from '@astrojs/sitemap';
import vercel from '@astrojs/vercel';

import { indexablePaths, legacyRedirects } from './src/lib/catalog.js';
import { hubPaths, indexableHubPaths } from './src/lib/hubs.js';
import { lastmodByPath } from './src/lib/lastmod.js';

/* Make the adapter's redirects match the URLs this site actually publishes.
 *
 * Every page here is built as a directory, so its address ends in a slash —
 * /bags/evergoods/civic-travel-bag/ is what the sitemap submitted, what the
 * internal links point at and what anyone bookmarked. The Vercel adapter
 * compiles a redirect into an anchored regex with no slash on the end
 * (`^/bags/evergoods/civic-travel-bag$`), which is the one form of the URL
 * nothing ever linked to. Astro's own SSR routes in the same file are emitted
 * as `/?$` and match both, so this is the redirect path specifically.
 *
 * Left alone the redirect never fires and the old address falls through to the
 * catch-all 404 — which is precisely the failure it was added to prevent, and
 * the kind that looks fine in a build log.
 *
 * It rewrites nothing it cannot account for: the expected pattern for every
 * redirect is reconstructed from the same map the config was built from, and a
 * pattern that is not in that map, or missing from the output, fails the build.
 * If a future adapter emits `/?$` itself, this stops finding work and says so
 * rather than silently double-patching.
 */
function matchTrailingSlash() {
  const sources = Object.keys(legacyRedirects);
  return {
    name: 'legacy-redirects-trailing-slash',
    hooks: {
      'astro:build:done': async ({ logger }) => {
        if (!sources.length) return;
        const path = new URL('.vercel/output/config.json', import.meta.url);
        const config = JSON.parse(await readFile(path, 'utf8'));
        const wanted = new Map(
          sources.map((from) => [`^${from.replace(/\/$/, '')}$`, from]),
        );

        const patched = new Set();
        for (const route of config.routes ?? []) {
          const from = wanted.get(route.src);
          if (!from) continue;
          route.src = `${route.src.slice(0, -1)}/?$`;
          patched.add(from);
        }
        if (patched.size !== sources.length) {
          throw new Error(
            `legacy redirects: patched ${patched.size} of ${sources.length} ` +
              `routes in .vercel/output/config.json. The adapter has changed ` +
              `how it compiles redirects — see matchTrailingSlash() in ` +
              `astro.config.mjs.`,
          );
        }
        await writeFile(path, JSON.stringify(config));
        logger.info(`${patched.size} legacy model URLs redirect with or ` +
                    `without a trailing slash`);
      },
    },
  };
}

// The catalog is entirely static: every model page is generated at build time
// from data/bags.json, so `output: 'static'` is the default and the adapter is
// here only for the handful of routes that opt out with `prerender = false`
// (the alerts endpoints, which need a database connection at request time).
export default defineConfig({
  // Canonical URLs, sitemap entries and JSON-LD all key off this. The env
  // override outlived the naming question it was added for: it now exists so
  // links can stay on the old domain until the new one is attached and out of
  // DNS quarantine — see SITE_URL in .env.template.
  // `||` and not `??` — `.env` ships SITE_URL empty, and `??` keeps the empty
  // string, which fails the build with "site: Invalid url".
  site: process.env.SITE_URL || 'https://carrynerd.com',
  output: 'static',
  adapter: vercel(),
  // Model pages whose URL moved when a multi-size record became one entry per
  // capacity. Built from the data rather than listed here, because the set
  // changes with the catalogue — see legacyRedirects in lib/catalog.js for
  // which capacity each one lands on. The adapter turns these into real 301s
  // in the Vercel routing config; on `output: 'static'` alone they would be
  // meta-refresh pages, which pass no authority and are not what an indexed
  // URL deserves.
  redirects: legacyRedirects,
  // Astro distrusts the Host and X-Forwarded-Host headers unless the domain is
  // listed here, and with the list empty it falls back to believing every
  // request is for "localhost". Behind Vercel's proxy that made the CSRF check
  // reject all form POSTs — the /internal/ login, and the /api/click beacon,
  // whose sendBeacon body arrives as text/plain and counts as a form — with
  // "Cross-site POST form submissions are forbidden", for every visitor. The
  // vercel.app entry is for preview deployments, which sit behind Vercel SSO
  // anyway.
  security: {
    allowedDomains: [
      { hostname: 'carrynerd.com', protocol: 'https' },
      { hostname: '**.vercel.app', protocol: 'https' },
    ],
  },
  // Three exclusions, for three unrelated reasons.
  //
  // /internal/ is the working list behind the coverage banners — real pages,
  // built from the same data, but addressed to whoever is maintaining the
  // index rather than to a reader. Nothing links to them and they carry
  // noindex; keeping them out of the sitemap is the third leg of that.
  //
  // Model pages are for readers, and every one of them ships. They stay out of
  // the sitemap only until they carry specs worth arriving for — submitting a
  // URL while serving it noindex is a contradiction, so the same rule has to
  // drive both. It lives in lib/catalog.js precisely so these two cannot drift
  // apart; a page in one and not the other is the confusing half-state.
  //
  // Category hubs answer to the same rule one level up: a category with too
  // few comparable models has nothing to compare, and lib/hubs.js decides
  // that once for the meta tag and this list together.
  integrations: [sitemap({
    filter: (page) => {
      const { pathname } = new URL(page);
      if (pathname.startsWith('/internal/')) return false;
      if (pathname.startsWith('/bags/')) return indexablePaths.has(pathname);
      if (hubPaths.has(pathname)) return indexableHubPaths.has(pathname);
      return true;
    },
    // When each surviving page last actually changed — see lib/lastmod.js for
    // which timestamp that is and why it is not the one the crawl writes.
    // Pages the map has no honest answer for keep their bare <loc>; returning
    // the item untouched is how this integration spells "no lastmod", and it
    // is the right answer rather than a fallback.
    // The 404 needs no exclusion here: the integration drops status-code pages
    // itself.
    serialize: (item) => {
      const lastmod = lastmodByPath.get(new URL(item.url).pathname);
      return lastmod ? { ...item, lastmod } : item;
    },
  }), matchTrailingSlash()],
  build: { format: 'directory' },
  devToolbar: { enabled: false },
});
