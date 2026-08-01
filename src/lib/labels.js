/* Display names for the machine-readable enum values normalize.py emits.
   Imported by both the browse island and the server-rendered model pages, so
   the two cannot drift apart. */

export const FEATURE_LABELS = {
  laptop_sleeve: "Laptop sleeve", water_bottle: "Bottle pocket",
  clamshell: "Clamshell opening", luggage_passthrough: "Luggage pass-through",
  rfid: "RFID blocking", expandable: "Expandable", hip_belt: "Hip belt",
  sternum_strap: "Sternum strap", molle: "MOLLE / PALS",
  compression: "Compression straps", shoe_compartment: "Shoe compartment",
  water_resistant: "Water resistant", carry_on: "Carry-on claimed",
  lockable_zips: "Lockable zips",
};

export const CAT_LABELS = {
  "travel-backpack": "Travel pack", daypack: "Daypack", sling: "Sling",
  duffel: "Duffel", tote: "Tote", messenger: "Messenger", briefcase: "Briefcase",
  "hip-pack": "Hip pack", luggage: "Luggage", "camera-bag": "Camera",
  "hiking-pack": "Hiking", pouch: "Pouch", "bike-bag": "Bike bag",
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

export const colourLabel = (c) => COLOUR_LABELS[c] ?? c;

export const catLabel = (c) => CAT_LABELS[c] ?? c;
export const featureLabel = (f) => FEATURE_LABELS[f] ?? f;
export const sourceLabel = (s) => (s ? SOURCE_LABELS[s] ?? s : null);
