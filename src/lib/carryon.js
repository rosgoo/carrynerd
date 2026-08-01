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
