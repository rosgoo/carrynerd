/* Site identity in one place, because it is not settled.
 *
 * "calipered" is the second working name, replacing "gearherd". The old one
 * had two problems worth not rediscovering: spoken aloud it was
 * indistinguishable from "gearhead", an existing outdoor retailer, and "herd"
 * connoted herd mentality, which cut against an audience that prides itself on
 * doing its own research. "calipered" names the thing the site actually does —
 * every bag measured the same way — and the past tense reads as a finished
 * state rather than an instruction. Known weakness: it is a verb form nobody
 * types unprompted, so it earns no search traffic on the name alone. The
 * traffic model is model pages, not the brand, so that is an accepted cost.
 *
 * Changing the name means changing this file and `site` in astro.config.mjs,
 * plus the places outside the Astro build that cannot import from here:
 * the Python pipeline's User-Agent and docstrings, alerts/ (match.py,
 * schema.sql), src/lib/email.ts, the nightly workflow's CALIPERED_CONTACT and
 * bot identity, the `calipered-theme` localStorage key in Base.astro, and
 * README.md. Grep for the name; it is not confined to this file. The mark in
 * components/Logo.astro and public/favicon.svg is a caliper, so it is tied to
 * this name too — a rename that keeps the art leaves a pun with no setup.
 */

export const SITE_NAME = 'calipered';
export const SITE_NAME_DISPLAY = 'CALIPERED';

export const TAGLINE = 'carry spec index';

/** Contact addresses referenced by /bot and /privacy. */
export const CONTACT = {
  crawler: 'crawler@calipered.com',
  privacy: 'privacy@calipered.com',
};
