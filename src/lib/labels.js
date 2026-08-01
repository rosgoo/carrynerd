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
  "hiking-pack": "Hiking", pouch: "Pouch",
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
  description: "parsed from the description",
};

export const catLabel = (c) => CAT_LABELS[c] ?? c;
export const featureLabel = (f) => FEATURE_LABELS[f] ?? f;
export const sourceLabel = (s) => (s ? SOURCE_LABELS[s] ?? s : null);
