/* Value formatting shared by the server-rendered pages. The browse island has
   its own compact variants because it renders into a dense grid; these are the
   long-form ones for a page that has room to breathe. */

export const fmtPrice = (n) =>
  n == null ? '—' : '$' + (Number.isInteger(n) ? n : n.toFixed(2));

export const fmtPriceRange = (min, max) =>
  min == null ? '—'
  : max == null || max === min ? fmtPrice(min)
  : `${fmtPrice(min)} – ${fmtPrice(max)}`;

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
