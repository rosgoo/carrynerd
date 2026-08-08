/* Site identity in one place. Settled, at last, 2026-08.
 *
 * "carrynerd" is the fourth working name: gearherd -> calipered -> gearherd
 * -> carrynerd. The objections that dogged "gearherd" through both of its
 * tenures are kept rather than deleted, because they are why the rename
 * finally happened and the bar any future name has to clear:
 *
 *   - Spoken aloud it was close to indistinguishable from "gearhead", an
 *     existing outdoor retailer and a common word — a real collision on any
 *     spoken channel (podcast reads, word of mouth, someone dictating the
 *     URL), and one letter of edit distance in print, so search kept
 *     correcting the brand into somebody else's.
 *   - "herd" connotes herd mentality, which cuts against an audience that
 *     prides itself on doing its own research.
 *
 * What "carrynerd" buys where gearherd paid: it spells itself when heard,
 * the token was unclaimed everywhere that matters, and "carry nerd" is what
 * this audience already calls itself — the name is the community's own
 * self-descriptor, not a coinage to teach. "calipered" named the method
 * precisely and earned no search traffic on the name alone, because it is a
 * verb form nobody types — and its caliper mark, drawn asymmetric to avoid
 * reading as the letter H, reads instead as a rifle in profile. That is not
 * fixable: symmetric goes back to H, asymmetric stays a weapon. A gear site
 * cannot ship it, and a name whose art cannot be drawn is not a name.
 *
 * Changing the name means changing this file and `site` in astro.config.mjs,
 * plus the places outside the Astro build that cannot import from here:
 * the Python pipeline's User-Agent and docstrings, alerts/ (match.py,
 * schema.sql), src/lib/email.ts, and the nightly workflow's CARRYNERD_CONTACT
 * and bot identity, and README.md. Grep for the name; it is not confined to
 * this file. The localStorage keys in Base.astro used to be on that list and
 * deliberately are not any more — see the note there.
 *
 * The mark in components/Logo.astro and public/favicon.svg is a bison. It
 * predates this name and stays by choice: the herd moved from the domain to
 * the community layer, and the animal keeps its job (the identity working
 * docs in the notes store hold that argument). validate.py fails the nightly
 * if those two files ever disagree.
 */

export const SITE_NAME = 'carrynerd';
export const SITE_NAME_DISPLAY = 'CARRYNERD';

/* "backpack", not "carry": the tab is a search result and a bookmark before it
   is anything else, and the word people actually type is the noun, not the
   category the name already says. */
export const TAGLINE = 'backpack spec index';

/* The one-line answer to "what is this", in the layout's default <meta
   description>, in the Organization schema and on /about. One string because
   the three have to agree: a search engine reconciling an entity across a site
   treats a description that varies page to page as weaker evidence than one
   that does not, and this is the sentence it is reconciling. */
export const SITE_DESCRIPTION =
  'A searchable, comparable index of bags normalised across brands from public product feeds.';

/* Other pages that are demonstrably this same entity, for schema's `sameAs`.
 *
 * This is the list that answers "is carrynerd a thing, or just a domain" —
 * `sameAs` is how an entity gets corroborated somewhere other than its own
 * site, and a brand-name search result leans on that corroboration more than
 * on anything the site says about itself.
 *
 * The bar for an entry is deliberately high: the page has to name this project
 * and link back here, so the relationship is checkable in both directions. A
 * `sameAs` pointing at a profile that does not exist, or that never mentions
 * the site, is not a neutral omission — it is a claim that does not survive
 * being followed, on the one node whose whole job is to be trusted.
 *
 * The repository qualifies: it is public, it carries the same one-line
 * description as SITE_DESCRIPTION, and its homepage field points here. Social
 * handles belong here too as they are claimed, not before.
 */
export const PROFILES = ['https://github.com/rosgoo/carrynerd'];

/** Contact addresses referenced by /bot and /privacy. */
export const CONTACT = {
  crawler: 'crawler@carrynerd.com',
  privacy: 'privacy@carrynerd.com',
};
