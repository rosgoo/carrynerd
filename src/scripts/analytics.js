/* gearherd — the two events Vercel cannot infer.
 *
 * <Analytics /> in the layout already gives pageviews and Speed Insights gives
 * web vitals, both for free and both without any help from this file. What it
 * cannot know is what a page *means*, and there are exactly two meanings worth
 * recording here:
 *
 *   product_view — a reader looked at one specific bag
 *   buy_click    — a reader left for a seller's site
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
 */
import { track } from '@vercel/analytics';

// Vercel takes flat string/number/boolean properties and rejects the event
// outright if one is an object. Empty and absent values are dropped rather than
// sent as "" — an absent property reads as unknown in the dashboard, where an
// empty string reads as a value we measured to be blank.
const clean = (props) =>
  Object.fromEntries(Object.entries(props).filter(([, v]) => v != null && v !== ''));

// Analytics must never be why a buy link fails to open. track() is already
// tolerant of the script not having loaded, but this runs inside a click
// handler on an anchor, so a throw here would be a broken outbound link.
function send(name, props) {
  try {
    track(name, clean(props));
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
 * Property ORDER is also deliberate, and is the one thing here that depends on
 * billing. Vercel keeps 2 properties per custom event on Pro and 8 only with
 * the Web Analytics Plus add-on, so on plain Pro four of the six below are
 * dropped. They are listed most useful first so that what survives the cut is
 * the widest cut (brand) and the exact join key (bag_id) — enough for a
 * per-brand and per-bag referral rate on its own. Everything after that is
 * detail that arrives for free the day the add-on is switched on.
 *
 * Sending six either way costs nothing extra: billing counts events, not
 * properties.
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
function initBuyClicks() {
  const fire = (e) => {
    const a = e.target?.closest?.('a[data-buy]');
    if (!a) return;
    // Same leading two properties as product_view, for the same reason: those
    // are the ones that survive on plain Pro, and a rate cannot be computed
    // from a numerator and denominator cut along different lines.
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
  initBuyClicks();
  initProductView();
}
