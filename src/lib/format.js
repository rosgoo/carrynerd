/* Value formatting shared by the server-rendered pages. The browse island has
   its own compact variants because it renders into a dense grid; these are the
   long-form ones for a page that has room to breathe. */

/* Prices carry their currency because they are never converted.
 *
 * A handful of brands sell from storefronts that quote something other than
 * dollars, and normalize.py stamps each bag with which. Rendering those as "$"
 * was the bug that put Bedouin Foundry's £340 bags in the index as $340 — the
 * number was right and the symbol made it a lie.
 *
 * The symbol, not the ISO code, for the currencies a reader recognises on
 * sight; the code for everything else, because "kr" is Danish, Norwegian and
 * Swedish at once and an ambiguous symbol is worse than a plain one. */
const SYMBOLS = { USD: '$', GBP: '£', EUR: '€', CAD: 'CA$', AUD: 'AU$', JPY: '¥' };

export const currencySymbol = (code) =>
  SYMBOLS[(code || 'USD').toUpperCase()] ?? `${(code || '').toUpperCase()} `;

export const fmtPrice = (n, currency = 'USD') =>
  n == null
    ? '—'
    : currencySymbol(currency) + (Number.isInteger(n) ? n : n.toFixed(2));

export const fmtPriceRange = (min, max, currency = 'USD') =>
  min == null ? '—'
  : max == null || max === min ? fmtPrice(min, currency)
  : `${fmtPrice(min, currency)} – ${fmtPrice(max, currency)}`;

export const fmtWeight = (g) =>
  g == null ? null : g >= 1000 ? `${(g / 1000).toFixed(2)} kg` : `${g} g`;

export const CM_PER_IN = 2.54;

/* Lengths format to *both* units at once, and the caller renders both.
   These pages are static HTML with no island on them, so a unit preference
   cannot be a re-render — and shipping only the saved unit would mean either
   building every page twice or a visible flash while JavaScript swapped the
   text. Emitting both and letting CSS show one costs a span and is correct
   before first paint. See .u-cm/.u-in in app.css and <Measure />. */
export const fmtDims = (d) =>
  d && d.length
    ? {
        cm: `${d.map((n) => n.toFixed(1)).join(' × ')} cm`,
        in: `${d.map((n) => (n / CM_PER_IN).toFixed(1)).join(' × ')} in`,
      }
    : null;

export const fmtVolume = (l) => (l == null ? null : `${l} L`);

export const fmtLaptop = (i) => (i == null ? null : `${i}″`);

export const fmtLinear = (cm) =>
  cm == null
    ? null
    : { cm: `${Math.round(cm)} cm`, in: `${Math.round(cm / CM_PER_IN)} in` };

export const gramsPerLitre = (b) =>
  b.weight_g && b.volume_l ? b.weight_g / b.volume_l : null;

export const pricePerLitre = (b) =>
  b.price_min && b.volume_l ? b.price_min / b.volume_l : null;

export const fmtDate = (iso) =>
  iso
    ? new Date(iso).toLocaleDateString('en-GB', {
        day: 'numeric', month: 'short', year: 'numeric',
      })
    : null;
