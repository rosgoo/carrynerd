/* gearherd — the two events Vercel cannot infer.
 *
 * <Analytics /> in the layout already gives pageviews, free and without any
 * help from this file. What it cannot know is what a page *means*, and there
 * are exactly two meanings worth recording here:
 *
 *   product_view — a reader looked at one specific bag
 *   buy_click    — a reader left for a seller's page for that bag
 *   brand_click  — a reader left for a brand's own site, not for a product
 *
 * brand_click is a separate name rather than another buy_click placement, and
 * that separation is load-bearing. Folding the two together would add clicks to
 * the numerator that were never attempts to buy anything, and the referral rate
 * would climb without a single extra sale — the most flattering way a metric
 * can lie. They are different intents, so they are different events, and the
 * rate below is computed from buy_click alone.
 *
 * The pair is the point, not either half. Referral rate is buy_click over
 * product_view, and that ratio is only honest if both halves are counted on
 * every surface where a bag can be both seen and bought. There are two such
 * surfaces — the static model page and the browse island's detail drawer — and
 * a reader can go from the grid straight into the drawer and out to the brand
 * without ever loading a model page. Counting those clicks while counting only
 * model-page views would divide by a denominator missing a whole surface and
 * quietly overstate the rate. So both surfaces fire both events, and every
 * event carries `placement` so the two can also be read apart.
 *
 * These go to our own /api/click, not to a vendor. Vercel bills custom events
 * from the Pro plan up and keeps only two properties per event below the Web
 * Analytics Plus add-on, which for a click counter is a lot of money to be told
 * less than we already know. The alerts plane needs a Postgres anyway, so the
 * events land in the same database and the referral rate is a GROUP BY.
 */

// Absent and empty values are dropped rather than sent as "": a missing column
// reads as unknown, where an empty string reads as a value we measured to be
// blank. Everything that survives is a flat scalar, which is all the table has
// columns for.
const clean = (props) =>
  Object.fromEntries(Object.entries(props).filter(([, v]) => v != null && v !== ''));

/* Analytics must never be why a buy link fails to open, so every failure path
 * here is silent: a full beacon queue, an offline reader, a blocked request.
 *
 * sendBeacon rather than fetch because the browser owns the request once it is
 * queued — it survives the page being closed or navigated away from, which a
 * plain fetch from a click handler does not. Today every buy link opens in a
 * new tab and the page stays alive either way, but that is a property of the
 * markup that a future link could quietly not have, and this is the cheaper
 * side to be wrong on.
 */
function send(name, props) {
  const event = { name, ...clean(props) };

  // Local browsing must not write rows. This is what @vercel/analytics did for
  // us before — it detected development and logged instead of sending — and
  // dropping it would mean every `astro dev` session silently seeding the
  // referral numbers with our own clicks. Vite replaces this at build time, so
  // the branch is not in the shipped bundle at all.
  if (import.meta.env.DEV) {
    console.debug('[analytics]', event);
    return;
  }

  try {
    const body = JSON.stringify(event);
    if (navigator.sendBeacon) navigator.sendBeacon('/api/click', body);
    else fetch('/api/click', { method: 'POST', body, keepalive: true });
  } catch {}
}

// The seller is the whole referral question, so it is derived from the href we
// are actually sending the reader to rather than passed in alongside it. Those
// two can disagree; the href cannot be wrong.
const seller = (url) => {
  try {
    return new URL(url, location.href).hostname.replace(/^www\./, '');
  } catch {
    return null;
  }
};

/* Both events share these property names on purpose: brand, bag_id and model
 * mean the same thing in each, which is what makes views and clicks joinable
 * per bag instead of two unrelated counters.
 *
 * All six are kept. Our own table has no per-event property cap to design
 * around, which is most of why the events go there: the properties are the
 * analysis, and a referral rate that cannot be cut by brand, by bag or by
 * placement is barely worth collecting.
 */
export function trackProductView(bag, placement) {
  send('product_view', {
    brand: bag.brand,
    bag_id: bag.id,
    placement,
    category: bag.category,
    model: bag.name,
    price: bag.price_min ?? null,
  });
}

/* One listener on the document, not one per link.
 *
 * The drawer re-renders its whole innerHTML every time it opens, so anything
 * bound to those anchors would need rebinding on each open and would silently
 * stop counting the first time that was forgotten. Delegation covers links that
 * do not exist yet, which is the case for every buy link in the island.
 *
 * Nothing is deferred or preventDefault-ed to get the beacon out: every buy
 * link on the site opens in a new tab, so the page sending the event stays
 * alive and loses no events to navigation. If a buy link ever ships without
 * target="_blank", that assumption is what breaks.
 */
function initOutboundClicks() {
  const fire = (e) => {
    // Two link families, checked in order of how much they are worth. They keep
    // separate attribute names because they carry genuinely different payloads
    // — a brand link has no bag, no model and no price — and flattening both
    // into one schema would mean half the columns are always null.
    const brand = e.target?.closest?.('a[data-brandlink]');
    if (brand) {
      send('brand_click', {
        brand: brand.dataset.brandlinkBrand,
        placement: brand.dataset.brandlink,
        seller: seller(brand.href),
      });
      return;
    }

    const a = e.target?.closest?.('a[data-buy]');
    if (!a) return;
    // Same property names as product_view, in the same order. A rate cannot be
    // computed from a numerator and denominator cut along different lines, so
    // the two events stay deliberately shaped alike.
    send('buy_click', {
      brand: a.dataset.buyBrand,
      bag_id: a.dataset.buyId,
      placement: a.dataset.buy,
      seller: seller(a.href),
      model: a.dataset.buyModel,
      price: Number(a.dataset.buyPrice) || null,
    });
  };

  document.addEventListener('click', fire);
  // Middle-click opens a seller in a background tab and never fires `click`.
  // That is a real visit and it counts. Button 2 is the context menu, which is
  // usually someone copying the link rather than going, so it does not.
  document.addEventListener('auxclick', (e) => {
    if (e.button === 1) fire(e);
  });
}

// The model page is static HTML with no island on it, so its bag details reach
// this module the only way they can: as data attributes rendered at build time.
function initProductView() {
  const el = document.querySelector('[data-product-view]');
  if (!el) return;
  trackProductView(
    {
      id: el.dataset.bagId,
      brand: el.dataset.brand,
      name: el.dataset.model,
      category: el.dataset.category,
      price_min: Number(el.dataset.price) || null,
    },
    'product_page',
  );
}

// Guarded because binding the click handler twice would not misbehave visibly,
// it would silently count every buy click twice and double the referral rate. A
// metric wrong by exactly 2× is far harder to notice than one that is missing.
export function initAnalytics() {
  if (window.__analyticsBound) return;
  window.__analyticsBound = true;
  initOutboundClicks();
  initProductView();
}
