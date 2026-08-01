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
  site: process.env.SITE_URL ?? 'https://bagdex.com',
  output: 'static',
  adapter: vercel(),
  integrations: [sitemap()],
  build: { format: 'directory' },
  devToolbar: { enabled: false },
});
