/* Site identity in one place, because it is not settled.
 *
 * "gearherd" is the third working name, and the second time it has held the
 * slot: gearherd -> calipered -> gearherd. The objections that got it dropped
 * the first time are recorded here rather than deleted, because they have not
 * gone away and rediscovering them a third time would be worse than living
 * with them knowingly:
 *
 *   - Spoken aloud it is close to indistinguishable from "gearhead", which is
 *     an existing outdoor retailer. This is a real collision on any spoken
 *     channel — podcast reads, word of mouth, someone dictating the URL.
 *   - "herd" connotes herd mentality, which cuts against an audience that
 *     prides itself on doing its own research.
 *
 * What it buys in exchange: it says what the site is in one compound, it is a
 * word people type unprompted, and it carries a mark. "calipered" named the
 * method precisely and earned no search traffic on the name alone, because it
 * is a verb form nobody types — and its caliper mark, drawn asymmetric to
 * avoid reading as the letter H, reads instead as a rifle in profile. That is
 * not fixable: symmetric goes back to H, asymmetric stays a weapon. A gear
 * site cannot ship it, and a name whose art cannot be drawn is not a name.
 *
 * Changing the name means changing this file and `site` in astro.config.mjs,
 * plus the places outside the Astro build that cannot import from here:
 * the Python pipeline's User-Agent and docstrings, alerts/ (match.py,
 * schema.sql), src/lib/email.ts, and the nightly workflow's GEARHERD_CONTACT
 * and bot identity, and README.md. Grep for the name; it is not confined to
 * this file. The localStorage keys in Base.astro used to be on that list and
 * deliberately are not any more — see the note there.
 *
 * The mark in components/Logo.astro and public/favicon.svg is a bison, so it
 * is tied to this name: a rename that keeps the art leaves an animal with no
 * setup. validate.py fails the nightly if those two files ever disagree.
 */

export const SITE_NAME = 'gearherd';
export const SITE_NAME_DISPLAY = 'GEARHERD';

export const TAGLINE = 'carry spec index';

/** Contact addresses referenced by /bot and /privacy. */
export const CONTACT = {
  crawler: 'crawler@gearherd.com',
  privacy: 'privacy@gearherd.com',
};
