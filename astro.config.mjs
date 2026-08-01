import { defineConfig } from 'astro/config';
import sitemap from '@astrojs/sitemap';
import vercel from '@astrojs/vercel';

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
  // /internal/ is the working list behind the coverage banners — real pages,
  // built from the same data, but addressed to whoever is maintaining the
  // index rather than to a reader. Nothing links to them and they carry
  // noindex; keeping them out of the sitemap is the third leg of that.
  integrations: [sitemap({ filter: (page) => !new URL(page).pathname.startsWith('/internal/') })],
  build: { format: 'directory' },
  devToolbar: { enabled: false },
});
