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

/* Metric only, and deliberately kept alongside the pair below rather than
   folded into it. What is left calling it is the crawler-facing copy — the meta
   description on a model page and on a size band — where there is no reader
   with a toggle to honour, only a string baked at build time, and where "820 g
   / 29 oz" would be noise in a search result. The same call the dimensions
   already make there, taking .cm off the pair and leaving the inches. Anything
   a reader looks at on the page itself goes through fmtWeightBoth(). */
export const fmtWeight = (g) =>
  g == null ? null : g >= 1000 ? `${(g / 1000).toFixed(2)} kg` : `${g} g`;

export const CM_PER_IN = 2.54;
export const G_PER_LB = 453.59237;
export const G_PER_OZ = 28.349523;

/* A weight in both systems, each half still in two pieces.
 *
 * A card sets the unit eight pixels tall under the figure rather than beside
 * it, so it needs the figure and the unit separately; everywhere else the unit
 * is simply the next word and fmtWeightBoth() below joins them. Splitting a
 * formatted string back apart at the call site would work until the day a
 * figure carries a space, so the split is done here, once, and the joined form
 * is what is built from it.
 *
 * Mirrors weightPair() in scripts/browse.js, deliberately and exactly: the
 * island and the hub pages draw the same card now, and a bag that reads 1.70 kg
 * on the front page and 1.7 kg on /daypacks/ is a bag a reader will go back and
 * check. */
export const fmtWeightPair = (g) =>
  g == null
    ? null
    : {
        metric: g >= 1000 ? [(g / 1000).toFixed(2), 'kg'] : [String(g), 'g'],
        imperial:
          g >= G_PER_LB
            ? [(g / G_PER_LB).toFixed(1), 'lb']
            : [String(Math.round(g / G_PER_OZ)), 'oz'],
      };

/* Weight formats to *both* unit systems at once, exactly as the lengths below
   do and for the same reason — see the note above fmtDims.

   That it did not was a plain bug: the toggle writes data-units="in" and every
   length on the page obeys, while the weight beside them stayed in grams. The
   button says "in" and means imperial; a reader who pressed it and was still
   told a bag weighs 1.42 kg was reading half a translation.

   Imperial changes unit where metric does — ounces under a pound, pounds over
   it — mirroring the g→kg step above. Holding to one unit either way was the
   alternative and it reads badly at both ends: 0.4 lb for a sling, 52 oz for a
   travel pack.

   The two halves are named for the systems rather than for the units, because
   unlike a length neither half here *has* one unit: it is g or kg against oz or
   lb. <Measure /> takes that spelling as well as the cm/in one. */
export const fmtWeightBoth = (g) => {
  const pair = fmtWeightPair(g);
  return pair && {
    metric: pair.metric.join(' '),
    imperial: pair.imperial.join(' '),
  };
};

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

/* The same triple, sized for a card's spec cell rather than a table row.
 *
 * A spec cell on a 232px card is about 49px wide and the type in it is 11.5px,
 * which "44.0 × 30.0 × 20.0 cm" does not fit into by a factor of three. Whole
 * centimetres, no spaces, no suffix — the cell's own label says H×W×D and the
 * unit toggle says which system, so neither is worth the width twice.
 *
 * Mirrors dualDims() in scripts/browse.js, and has to: the island and the hub
 * pages now draw the same card, and a bag whose dimensions round differently
 * on /daypacks/ and on the front page is a bag a reader will think they have
 * mis-remembered. Same rounding, same separator, same order.
 *
 * fmtDims() above is unchanged and is still what a table cell and a model page
 * want — those have room for the decimals, and the decimals are the point when
 * a reader is checking a bag against a gauge. */
export const fmtDimsCompact = (d) =>
  d && d.length
    ? {
        cm: d.map((n) => n.toFixed(0)).join('×'),
        in: d.map((n) => (n / CM_PER_IN).toFixed(0)).join('×'),
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
