/* Site identity in one place, because it is not settled.
 *
 * "gearherd" is a working name — the pun is gearhead → gearherd, a crowd of
 * enthusiasts. Two known problems with it, recorded so they are not
 * rediscovered: spoken aloud it is indistinguishable from "gearhead", which is
 * an existing outdoor retailer, and "herd" connotes herd mentality, which cuts
 * against an audience that prides itself on doing its own research.
 *
 * Changing the name means changing this file and `site` in astro.config.mjs.
 * Nothing else hardcodes it.
 */

export const SITE_NAME = 'gearherd';
export const SITE_NAME_DISPLAY = 'GEARHERD';

export const TAGLINE = 'carry spec index';

/** Contact addresses referenced by /bot and /privacy. */
export const CONTACT = {
  crawler: 'crawler@gearherd.com',
  privacy: 'privacy@gearherd.com',
};
