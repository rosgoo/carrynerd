import { defineConfig } from 'astro/config';
import sitemap from '@astrojs/sitemap';
import vercel from '@astrojs/vercel';

import { indexablePaths } from './src/lib/catalog.js';

// The catalog is entirely static: every model page is generated at build time
// from data/bags.json, so `output: 'static'` is the default and the adapter is
// here only for the handful of routes that opt out with `prerender = false`
// (the alerts endpoints, which need a database connection at request time).
export default defineConfig({
  // Canonical URLs, sitemap entries and JSON-LD all key off this. The name is
  // not settled — see the open decisions in the handoff — so it reads from the
  // environment and the placeholder is the only thing to change once it is.
  site: process.env.SITE_URL ?? 'https://gearherd.com',
  output: 'static',
  adapter: vercel(),
  // Two exclusions, for two unrelated reasons.
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
  integrations: [sitemap({
    filter: (page) => {
      const { pathname } = new URL(page);
      if (pathname.startsWith('/internal/')) return false;
      if (pathname.startsWith('/bags/')) return indexablePaths.has(pathname);
      return true;
    },
  })],
  build: { format: 'directory' },
  devToolbar: { enabled: false },
});
