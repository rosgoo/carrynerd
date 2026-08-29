/* Display names for the machine-readable enum values normalize.py emits.
   Imported by both the browse island and the server-rendered model pages, so
   the two cannot drift apart. */

/* The one feature key nothing extracted.
 *
 * Every other value in FEATURE_LABELS is something normalize.py read off a
 * product page. This one is computed — a bag whose stated dimensions clear the
 * published carry-on limit at every carrier in data/airlines.json — and it is
 * injected into the feature facet by api/browse.ts rather than written into
 * bags.json, because it is a fact about the airline table as much as about the
 * bag and it changes when that table is reviewed.
 *
 * It lives here, in the module with no imports, so the server that injects it
 * and the rail that labels it are naming the same string. Defining it in
 * lib/carryon.js instead would be the natural home right up until labels.js
 * imported it, which would drag data/airlines.json into the browse bundle for
 * the sake of one constant.
 *
 * Deliberately next to `carry_on`, which is the brand's claim rather than a
 * measurement, and reads "Carry-on claimed" for exactly that reason. The pair
 * is the point: one is what the marketing copy says, the other is what the
 * numbers say.
 */
export const FIT_FEATURE = "carry_on_fit";

export const FEATURE_LABELS = {
  laptop_sleeve: "Laptop sleeve", water_bottle: "Bottle pocket",
  clamshell: "Clamshell opening", luggage_passthrough: "Luggage pass-through",
  rfid: "RFID blocking", expandable: "Expandable", hip_belt: "Hip belt",
  sternum_strap: "Sternum strap", molle: "MOLLE / PALS",
  compression: "Compression straps", shoe_compartment: "Shoe compartment",
  water_resistant: "Water resistant", carry_on: "Carry-on claimed",
  lockable_zips: "Lockable zips",
  // The stronger water claims, which water_resistant on its own cannot tell
  // apart. "(stated)" because this is the brand's word, not a test result —
  // the same reason every measurement carries its source.
  waterproof: "Waterproof (stated)", taped_seams: "Taped / welded seams",
  waterproof_zips: "Waterproof zips",
  ykk_zips: "YKK zippers", airtag_pocket: "AirTag pocket",
  [FIT_FEATURE]: "Carry-on compliant",
};

export const CAT_LABELS = {
  "travel-backpack": "Travel pack", daypack: "Daypack", sling: "Sling",
  duffel: "Duffel", tote: "Tote", messenger: "Messenger", briefcase: "Briefcase",
  "hip-pack": "Hip pack", luggage: "Luggage", "camera-bag": "Camera",
  "hiking-pack": "Hiking", pouch: "Pouch", "bike-bag": "Bike bag",
  wallet: "Wallet",
};

// Where a value came from, spelled out. normalize.py records one of these on
// every extracted field; the model page surfaces it so a reader can judge how
// much to trust a number rather than taking it on faith.
export const SOURCE_LABELS = {
  "shopify:grams": "Shopify shipping weight",
  "product-page": "stated on the product page",
  "product-page:labelled": "labelled spec on the product page",
  "product-page:inline": "inline spec on the product page",
  "product-page:axis": "per-axis spec on the product page",
  title: "parsed from the product title",
  option: "parsed from a variant option",
  "variant-option": "parsed from a variant option",
  description: "parsed from the description",
  tag: "matched from a product tag — the weakest signal we use",
  collection: "the brand's own shelving",
  // The one source that is not an extraction: somebody looked at this product
  // and said so, in data/category-overrides.json.
  override: "a reviewed ruling, not an extraction",
};

/* Colour families. Brands name colourways for places and moods — "Wasatch
   Green", "Atacama Clay" — so the raw names are unfilterable; nobody searches
   for Atacama. normalize.py maps each to one of these. */
export const COLOUR_LABELS = {
  black: 'Black', grey: 'Grey', white: 'White / cream', brown: 'Brown / tan',
  blue: 'Blue / navy', green: 'Green / olive', red: 'Red / burgundy',
  orange: 'Orange', yellow: 'Yellow / gold', purple: 'Purple',
  pink: 'Pink', multi: 'Camo / print',
};

// One representative swatch per family, legible on the light and dark themes.
export const COLOUR_SWATCH = {
  black: '#141514', grey: '#8a8d8a', white: '#f0ede6', brown: '#9a7550',
  blue: '#2f5ea8', green: '#4a6b3f', red: '#a8322b', orange: '#d4601f',
  yellow: '#d9b02c', purple: '#6b4f8f', pink: '#c98099',
  multi: 'linear-gradient(135deg,#4a5335 0 33%,#8a6f4b 33% 66%,#3a3d3c 66%)',
};

export const COLOUR_ORDER = [
  'black', 'grey', 'white', 'brown', 'blue', 'green', 'red', 'orange',
  'yellow', 'purple', 'pink', 'multi',
];

/* The dots in a card's foot, which are the *colourway* names rather than the
 * families above: a card lists what the brand calls each variant, so "Coyote"
 * and "Ranger Green" get their own swatches instead of collapsing into two
 * browns. Substring matching because the names are phrases — "Solution Dyed
 * Black", "Multicam Arid" — and an unknown one falls back to a neutral block
 * rather than an invented colour.
 *
 * Here rather than in scripts/browse.js, where it lived while the grid was the
 * island's alone. The hub, facet and airline pages draw the same card from
 * Astro now, and two copies of this table would mean the same bag's dots coming
 * out different colours depending on which page you found it on. */
export function cssColor(name) {
  const n = String(name ?? '').toLowerCase();
  const map = {
    black: '#111', jet: '#111', navy: '#1c2b4a', blue: '#2f5ea8', olive: '#4a5335',
    green: '#33623f', grey: '#7c7f7c', gray: '#7c7f7c', charcoal: '#3a3d3c',
    white: '#eee', cream: '#e6ddc9', tan: '#c2a178', brown: '#5f4632',
    red: '#a8322b', orange: '#d4601f', yellow: '#d9b02c', purple: '#5b4076',
    pink: '#c98099', sand: '#cbbb9a', khaki: '#9d8a63', coyote: '#8a6f4b',
    silver: '#b9bcbb', clear: '#8fa3a8', multicam: '#7a7150',
  };
  for (const k in map) if (n.includes(k)) return map[k];
  return 'var(--line-2)';
}

export const colourLabel = (c) => COLOUR_LABELS[c] ?? c;

export const catLabel = (c) => CAT_LABELS[c] ?? c;
export const featureLabel = (f) => FEATURE_LABELS[f] ?? f;
export const sourceLabel = (s) => (s ? SOURCE_LABELS[s] ?? s : null);
