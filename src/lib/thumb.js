/* Ask the brand's CDN for the size we actually paint.
 *
 * The feed records whatever URL a store publishes, which is the original
 * upload — a 1 to 1.5 MB PNG is typical, and the largest in the catalogue are
 * past 2 MB. A grid card paints that into a 232 px slot, so a screenful of
 * thirty cards was pulling something like 30 MB to show about 2 MB worth of
 * pixels. That is the whole of "the images take forever and the page looks
 * broken": it was not broken, it was still downloading.
 *
 * Only hosts with a documented resize parameter are rewritten, and an
 * unrecognised host is handed back untouched rather than guessed at — a
 * parameter the CDN does not understand is a broken image, which is a worse
 * outcome than a slow one. Shopify's CDN alone is 88% of the catalogue's
 * photography and imgix a further 2%; the remaining ~600 images load exactly
 * as they did before, and adding a host here is the way to shrink that.
 *
 * Widths come from a small fixed set rather than being computed per element,
 * so a second visit, a shared link and a second reader all hit the same
 * derivative in the CDN's cache instead of minting a new one each time.
 */

/** The four sizes the site asks for, named by where they are painted. */
export const THUMB = {
  card: 480, // grid card: 232–300 css px, so this covers 2× and the 3× phone
  chip: 96, // the 26 px colourway thumbnails in the detail drawer
  compare: 240, // the 84 px heads across the comparison table
  hero: 960, // the drawer's own full-width shot
};

export function thumb(url, width) {
  if (!url) return url;

  let u;
  try {
    u = new URL(url);
  } catch {
    return url; // relative or malformed — leave it for the browser to resolve
  }

  // Shopify's CDN takes ?width=N and clamps to the original, so there is no
  // upscaling to defend against. The URL already carries a ?v=<timestamp>
  // cache buster, which is why this goes through URLSearchParams rather than
  // concatenating a query string.
  if (u.hostname === 'cdn.shopify.com') {
    u.searchParams.set('width', width);
    return u.href;
  }

  // imgix, which is where Bellroy's photography lives. auto=format lets it
  // answer with WebP or AVIF when the browser's Accept header says so.
  if (u.hostname.endsWith('.imgix.net')) {
    u.searchParams.set('w', width);
    u.searchParams.set('auto', 'format');
    return u.href;
  }

  return url;
}
