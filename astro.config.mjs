import { defineConfig } from 'astro/config';
import sitemap from '@astrojs/sitemap';
import vercel from '@astrojs/vercel';

import { indexablePaths } from './src/lib/catalog.js';
import { hubPaths, indexableHubPaths } from './src/lib/hubs.js';

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
  })],
  build: { format: 'directory' },
  devToolbar: { enabled: false },
});
