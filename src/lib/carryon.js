/* Carry-on compliance, computed at build time from data/airlines.json.
 *
 * The rule: sort the bag's dims and the airline's limit largest-first and
 * require every axis to fit (or the linear sum, for airlines that publish a
 * total-cm rule instead). Sorting both sides is what makes the comparison
 * orientation-proof — brands and airlines disagree about which axis is
 * "height", but a bag fits a frame in whichever orientation works.
 *
 * Deliberately conservative and honest about it:
 *  - Stated dims only. Soft bags compress, so a borderline soft bag may fit
 *    in practice; we do not invent a squish allowance.
 *  - Bags with fewer than three known axes get null (unknown), not a guess.
 *  - Weight caps apply to the PACKED bag and the index only knows the empty
 *    weight, so weight is surfaced as context and never fails a bag.
 */
import airlinesData from '../../data/airlines.json';

export const airlines = airlinesData.airlines;
export const airlinesUpdated = airlinesData.updated;

export const REGION_LABELS = {
  'americas': 'Americas',
  'europe': 'Europe',
  'asia-pacific-me': 'Asia-Pacific & Middle East',
};

/* The airline's own page. Derived from the name rather than stored in
 * airlines.json, because that file is maintained by hand against each
 * carrier's published policy and a slug would be a second thing to keep
 * correct there for no gain.
 *
 * Two carriers whose names differ only in punctuation would collide into one
 * route and silently publish one page where two were meant, so the collision
 * is a failed build rather than something to notice later — the same stance
 * catalog.js takes on an empty catalog. */
export const airlineSlug = (airline) =>
  airline.name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');

export const airlineHref = (airline) => `/carry-on/${airlineSlug(airline)}/`;

{
  const seen = new Map();
  for (const airline of airlines) {
    const slug = airlineSlug(airline);
    const clash = seen.get(slug);
    if (clash) {
      throw new Error(
        `data/airlines.json: "${airline.name}" and "${clash}" both slug to ` +
          `"${slug}" — /carry-on/${slug}/ can only be one of them.`,
      );
    }
    seen.set(slug, airline.name);
  }
}

/** Does this carrier publish a carry-on size rule we can compare against?
 *  Two ways to publish one — three axes, or a linear total — and a carrier
 *  doing neither can neither pass nor fail a bag. */
export const publishesCarryOn = (airline) =>
  Boolean(airline.carry_on_cm || airline.carry_on_linear_cm != null);

/* The carriers a bag can actually be checked against, and therefore the
 * denominator in every "fits N of M" on the site. All 34 of them today; the
 * filter is here because data/airlines.json is allowed to hold a carrier whose
 * policy states a weight cap and no size, and one of those must not quietly
 * make every bag in the index non-compliant. */
export const carryOnAirlines = airlines.filter(publishesCarryOn);

/** Airlines grouped for display, in the region order REGION_LABELS declares. */
export const airlinesByRegion = Object.entries(REGION_LABELS).map(
  ([key, label]) => ({ key, label, airlines: airlines.filter((a) => a.region === key) }),
);

function fitsTriple(dims, limit) {
  if (!dims || dims.length < 3 || !limit) return null;
  const bag = [...dims].sort((a, b) => b - a);
  const lim = [...limit].sort((a, b) => b - a);
  return bag.every((v, i) => v <= lim[i]);
}

/** {carryOn, personal} for one airline — each true/false, or null (unknown). */
export function airlineFit(bag, airline) {
  const dims = bag.dims_cm;
  if (!dims || dims.length < 3) return { carryOn: null, personal: null };
  let carryOn = null;
  if (airline.carry_on_cm) carryOn = fitsTriple(dims, airline.carry_on_cm);
  if (airline.carry_on_linear_cm != null) {
    const linearOk =
      dims.reduce((a, b) => a + b, 0) <= airline.carry_on_linear_cm;
    carryOn = carryOn == null ? linearOk : carryOn && linearOk;
  }
  return {
    carryOn,
    personal: airline.personal_cm ? fitsTriple(dims, airline.personal_cm) : null,
  };
}

/** "Fits N of M airlines" counts, or null when the bag's dims are unknown. */
export function complianceSummary(bag) {
  if (!bag.dims_cm || bag.dims_cm.length < 3) return null;
  const out = { carryOn: 0, carryOnOf: 0, personal: 0, personalOf: 0 };
  for (const airline of airlines) {
    const fit = airlineFit(bag, airline);
    if (fit.carryOn != null) {
      out.carryOnOf += 1;
      if (fit.carryOn) out.carryOn += 1;
    }
    if (fit.personal != null) {
      out.personalOf += 1;
      if (fit.personal) out.personal += 1;
    }
  }
  return out;
}

export function fmtLimit(airline) {
  if (airline.carry_on_cm) return airline.carry_on_cm.join(' × ') + ' cm';
  if (airline.carry_on_linear_cm != null)
    return `≤ ${airline.carry_on_linear_cm} cm total`;
  return null;
}
