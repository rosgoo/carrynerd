/* The site's own entity, as structured data.
 *
 * Every other piece of JSON-LD here describes something the catalogue *holds*
 * — a Product, an ItemList, a BreadcrumbList. None of it describes the thing
 * doing the holding, which left the site with no machine-readable answer to
 * "what is carrynerd", on a domain weeks old with no history to infer one
 * from. These two nodes are that answer.
 *
 * They exist for two different search behaviours, and it is worth keeping
 * straight which is which, because the requirements differ:
 *
 *   Organization — entity resolution. This is what a name gets reconciled
 *     against: the logo, the description, and above all `sameAs`, which is the
 *     only field here that points at evidence not under our own control.
 *   WebSite — the site name Google prints above the URL in a result. That
 *     feature is documented as home-page-only and it reads `name` and
 *     `alternateName`, which is why the site graph ships on `/` and the
 *     Organization alone ships on /about.
 *
 * Both carry an `@id`, so the two pages that declare the Organization declare
 * the *same* Organization rather than two that happen to match, and anything
 * later wanting a `publisher` can reference the node instead of restating it.
 */

import {
  SITE_NAME,
  SITE_NAME_DISPLAY,
  SITE_DESCRIPTION,
  TAGLINE,
  PROFILES,
} from './brand.js';

/* The spellings a reader might type or write that are not the one on the page.
 *
 * "Carry Nerd" is the load-bearing entry and the reason this field is here at
 * all. The name is a two-word phrase the carry community already uses about
 * *people* — there is a video titled "A Carry Nerd's 10 Essential Backpack
 * Accessories" — so the site is not competing for the spaced spelling against
 * a rival brand, it is competing against the phrase's ordinary descriptive
 * sense. Nothing on the site spells it with a space, which leaves a search
 * engine no reason to connect the two. This is the reason.
 *
 * Lowercase "carrynerd" is here because it is what the URL, the crawler's
 * User-Agent and the repository all say; the display form is a styling
 * decision (see brand.js) and does not need teaching.
 */
const ALTERNATE_NAMES = ['Carry Nerd', 'CarryNerd', SITE_NAME];

/** @param {URL | undefined} site @param {string} path */
const at = (site, path) => new URL(path, site).href;

/* Square, 512px, drawn by scripts/og_image.py alongside the share card.
 *
 * Not public/og.png, which is 1200x630 and has the wordmark and a tagline
 * baked in — a share card, which is a different job from a logo and the wrong
 * shape for the panel this appears in. Not public/favicon.svg either: its
 * viewBox is 32 units and it declares no intrinsic width, and the documented
 * minimum for this field is 112x112, which an SVG with no stated size cannot
 * be checked against. A PNG that states its dimensions can.
 *
 * @param {URL | undefined} site
 */
const logo = (site) => ({
  '@type': 'ImageObject',
  '@id': at(site, '/#logo'),
  url: at(site, '/logo.png'),
  contentUrl: at(site, '/logo.png'),
  width: 512,
  height: 512,
  caption: `${SITE_NAME_DISPLAY} logo`,
});

/** The entity. Declared identically on `/` and /about; `@id` makes those one
 *  node rather than two.
 *  @param {URL | undefined} site */
export const organization = (site) => ({
  '@type': 'Organization',
  '@id': at(site, '/#organization'),
  name: SITE_NAME_DISPLAY,
  alternateName: ALTERNATE_NAMES,
  url: at(site, '/'),
  logo: logo(site),
  // The same drawing again, because `logo` is read by one feature and `image`
  // by others, and a node with a logo and no image is a node half the
  // consumers of it cannot render.
  image: { '@id': at(site, '/#logo') },
  description: SITE_DESCRIPTION,
  slogan: TAGLINE,
  ...(PROFILES.length ? { sameAs: PROFILES } : {}),
});

/* The home page's own node, for the site-name feature.
 *
 * `name` matches og:site_name exactly and deliberately: the two are read by
 * the same consumers and disagreeing about the name is worse than either
 * spelling on its own. Changing how the brand is capitalised in search results
 * means changing SITE_NAME_DISPLAY, which changes both together.
 *
 * `alternateName` is a single string here where the Organization takes a list.
 * The site-name feature is documented as accepting one alternate, and handing
 * a three-element array to a field that expects a scalar risks all three being
 * discarded. Entity resolution has no such limit, so the list lives there and
 * the one spelling worth teaching lives here.
 *
 * @param {URL | undefined} site
 */
const website = (site) => ({
  '@type': 'WebSite',
  '@id': at(site, '/#website'),
  name: SITE_NAME_DISPLAY,
  alternateName: 'Carry Nerd',
  url: at(site, '/'),
  description: SITE_DESCRIPTION,
  publisher: { '@id': at(site, '/#organization') },
  inLanguage: 'en',
});

/** Home page only — see the note on `website` above.
 *  @param {URL | undefined} site */
export const siteGraph = (site) => ({
  '@context': 'https://schema.org',
  '@graph': [organization(site), website(site)],
});
