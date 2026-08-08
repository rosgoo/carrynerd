/* The join between the airline limits and the catalogue.
 *
 * lib/carryon.js answers "does this bag fit this airline" for one bag at a
 * time, which is what a model page needs. The airline pages ask it the other
 * way round — "which bags fit this airline" — over the whole index, and doing
 * that per page would run the same comparison 34 times. The matrix below is
 * built once at build time and every airline page reads its column out of it.
 *
 * Kept out of carryon.js so that module stays a pure function of
 * data/airlines.json, importable without dragging the 40MB catalogue in.
 */

import { bags, isIndexable } from './catalog.js';
import { airlines, airlineFit, airlineSlug } from './carryon.js';

/* The bags these pages can say anything at all about. Three known axes is the
 * floor: airlineFit() abstains below that, and a page listing bags it has not
 * actually checked would be worse than a shorter page. */
export const measured = bags.filter(
  (bag) => isIndexable(bag) && bag.dims_cm && bag.dims_cm.length >= 3,
);

/** Carriers publishing a carry-on rule at all — the denominator in "fits N of M". */
export const carryOnAirlineCount = airlines.filter(
  (a) => a.carry_on_cm || a.carry_on_linear_cm != null,
).length;

const matrix = (() => {
  /** @type {Map<string, import('./types.d.ts').Bag[]>} */
  const carryOn = new Map();
  /** @type {Map<string, import('./types.d.ts').Bag[]>} */
  const personal = new Map();
  /** @type {Map<string, number>} */
  const count = new Map();

  for (const airline of airlines) {
    carryOn.set(airlineSlug(airline), []);
    personal.set(airlineSlug(airline), []);
  }
  for (const bag of measured) {
    let fitted = 0;
    for (const airline of airlines) {
      const slug = airlineSlug(airline);
      const fit = airlineFit(bag, airline);
      if (fit.carryOn === true) {
        carryOn.get(slug).push(bag);
        fitted += 1;
      }
      if (fit.personal === true) personal.get(slug).push(bag);
    }
    count.set(bag.id, fitted);
  }
  return { carryOn, personal, count };
})();

/** bag id → how many tracked carriers' carry-on limits it clears. */
export const fitCount = matrix.count;

/** Bags that clear this carrier's carry-on limit. */
export const fittingCarryOn = (airline) => matrix.carryOn.get(airlineSlug(airline)) ?? [];

/** Bags that clear this carrier's personal-item limit. */
export const fittingPersonal = (airline) => matrix.personal.get(airlineSlug(airline)) ?? [];

/* Bags that clear most of the other carriers and fail this one.
 *
 * This is the section of an airline page that could not be written anywhere
 * else: it is a statement about one carrier measured against the other 33, and
 * it is the practical question a reader arrives with — not "what fits" but
 * "what have I got away with before that will not work here". Sorted by how
 * widely a bag is otherwise accepted, so the surprises come first.
 */
export function justMisses(airline) {
  const bar = Math.ceil(carryOnAirlineCount * 0.6);
  return measured
    .filter(
      (bag) =>
        airlineFit(bag, airline).carryOn === false &&
        (fitCount.get(bag.id) ?? 0) >= bar,
    )
    .sort(
      (a, b) =>
        (fitCount.get(b.id) ?? 0) - (fitCount.get(a.id) ?? 0) ||
        (b.volume_l ?? 0) - (a.volume_l ?? 0),
    );
}
