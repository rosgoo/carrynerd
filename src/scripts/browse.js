/* gearherd — the browse/compare island.
 *
 * Loads the whole catalog once and filters in memory. That is deliberate: at a
 * few thousand models it beats a round trip per keystroke, and it means the
 * catalog needs no runtime service behind it. The per-model pages are static
 * HTML generated at build time; this is the one interactive surface.
 *
 * Filtering in memory scales; *drawing* the answer does not, and at 7,700
 * models the two had to be separated. Matching is one pass over an array and
 * costs a few milliseconds. Rendering paints a screenful and lets an
 * IntersectionObserver ask for the rest — see render() and paintChunk(), and
 * `content-visibility` on .card in app.css, which is the half of that bargain
 * the stylesheet holds.
 */

import { CAT_LABELS, FEATURE_LABELS, COLOUR_LABELS, COLOUR_SWATCH,
         COLOUR_ORDER } from '../lib/labels.js';
import { thumb, THUMB } from '../lib/thumb.js';
import { trackProductView } from './analytics.js';
import './watch.js';

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// Mirrors bagHref() in src/lib/catalog.js. `slug` is the permalink, derived
// from the merged model name; the id is a storage key and may still carry a
// colourway. Fall back to the id only for data written before slugs existed.
const bagHref = b => {
  const at = b.id.indexOf("__");
  const brand = b.brand_slug || (at < 0 ? "" : b.id.slice(0, at));
  const slug = b.slug || (at < 0 ? b.id : b.id.slice(at + 2));
  return brand ? `/bags/${brand}/${slug}/` : "/";
};

// Every facet's state in one place. This mapping used to be re-declared in
// four separate functions, which meant adding a facet meant remembering all
// four.
const facetSets = () => ({
  cat: S.cats, brand: S.brands, feat: S.feats, mat: S.mats, color: S.colors,
});

let DATA = { meta: {}, bags: [] };
let VIEW = "grid";
const compare = new Set();

// id → bag. The tray, the comparison sheet and the drawer all resolve ids back
// to records, and a linear scan of 7,700 for each one adds up on a page where
// the whole point is that nothing scans the catalog more than it must.
let BY_ID = new Map();

const S = {
  q: "", cats: new Set(), brands: new Set(), feats: new Set(), mats: new Set(),
  colors: new Set(),
  volMin: null, volMax: null, priceMin: null, priceMax: null,
  weightMin: null, weightMax: null, linearMax: null,
  presets: new Set(), stock: false, sale: false, sort: "brand",
};

/* ---------- units ---------- */

/* Lengths render in both units at once and CSS reveals one, keyed off
 * data-units on <html>. Same mechanism the static pages use, and for the same
 * reason: switching units then costs no re-render and cannot flash the wrong
 * number.
 *
 * Everything in S stays metric. The filter arithmetic, the shareable URL and
 * the catalog all speak centimetres; inches exist only at the point a number
 * meets an eye. A link someone sends means the same bags whichever unit either
 * end happens to be reading in. */
const CM_PER_IN = 2.54;
const inches = () => document.documentElement.getAttribute("data-units") === "in";
const toIn = cm => cm / CM_PER_IN;
const dual = (cm, inch) =>
  `<span class="u-cm">${cm}</span><span class="u-in">${inch}</span>`;

const dualLen = (cm, suffix = false) => cm == null ? null
  : dual(Math.round(cm) + (suffix ? " cm" : ""),
         Math.round(toIn(cm)) + (suffix ? " in" : ""));

const dualDims = (d, { sep = "×", digits = 0, suffix = false } = {}) => !d ? null
  : dual(d.map(n => n.toFixed(digits)).join(sep) + (suffix ? " cm" : ""),
         d.map(n => toIn(n).toFixed(digits)).join(sep) + (suffix ? " in" : ""));

/* ---------- formatting ---------- */

const nil = '<b class="nil">—</b>';
const fmtVol    = b => b.volume_l ? `${b.volume_l}<em style="font-size:8px">L</em>` : null;
const fmtWeight = b => b.weight_g ? (b.weight_g >= 1000
  ? `${(b.weight_g / 1000).toFixed(2)}<em style="font-size:8px">kg</em>`
  : `${b.weight_g}<em style="font-size:8px">g</em>`) : null;
const fmtDims   = b => dualDims(b.dims_cm);
const fmtLap    = b => b.laptop_in ? `${b.laptop_in}″` : null;
const fmtPrice  = n => n == null ? "—" : "$" + (Number.isInteger(n) ? n : n.toFixed(2));
const gpl = b => (b.weight_g && b.volume_l) ? b.weight_g / b.volume_l : null;
const ppl = b => (b.price_min && b.volume_l) ? b.price_min / b.volume_l : null;

/* ---------- filtering ---------- */

function haystack(b) {
  if (!b._hay) {
    b._hay = [b.brand, b.name, b.category, ...(b.materials || []),
              ...(b.features || []), ...(b.colors || []), ...(b.tags || [])]
             .join(" ").toLowerCase();
  }
  return b._hay;
}

function fitsUnderseat(b) {
  if (!b.dims_cm || b.dims_cm.length < 3) return false;
  const d = [...b.dims_cm].sort((x, y) => y - x);
  return d[0] <= 40 && d[1] <= 30 && d[2] <= 20;
}

/* Derived once per render and read by matches(), which runs once per bag.
 *
 * Splitting the query string and spreading three Sets *inside* matches() meant
 * doing all four thousands of times over for an answer that cannot change
 * during the pass — at 7,700 bags that is the allocation, not the comparing,
 * that made a keystroke cost what it did. prepareFilters() is the only writer
 * and render() is the only caller. */
let QTERMS = [], FEATS = [], MATS = [], COLORS = [];

function prepareFilters() {
  QTERMS = S.q ? S.q.toLowerCase().split(/\s+/).filter(Boolean) : [];
  FEATS = [...S.feats];
  MATS = [...S.mats];
  COLORS = [...S.colors];
}

function matches(b) {
  if (QTERMS.length) {
    const hay = haystack(b);
    for (const t of QTERMS) if (!hay.includes(t)) return false;
  }
  if (S.cats.size && !S.cats.has(b.category)) return false;
  if (S.brands.size && !S.brands.has(b.brand_slug)) return false;
  if (FEATS.length && !FEATS.every(f => (b.features || []).includes(f))) return false;
  if (MATS.length && !MATS.some(m => (b.materials || []).includes(m))) return false;
  // Any-of, like materials: picking black and green means "comes in either".
  if (COLORS.length
      && !COLORS.some(c => (b.color_families || []).includes(c))) return false;
  // Feature and material lists are only trustworthy once enrichment has read
  // the product page. Before that an empty list means "we never got a good
  // look", not "it doesn't have one" — see featuresUnknown() and the count.
  if (S.stock && !b.in_stock) return false;
  if (S.sale && !b.on_sale) return false;

  // Range filters exclude unknowns: a bag with no measured volume cannot be
  // asserted to sit inside a volume window.
  if (S.volMin != null && !(b.volume_l >= S.volMin)) return false;
  if (S.volMax != null && !(b.volume_l <= S.volMax)) return false;
  if (S.priceMin != null && !(b.price_min >= S.priceMin)) return false;
  if (S.priceMax != null && !(b.price_min <= S.priceMax)) return false;
  if (S.weightMin != null && !(b.weight_g >= S.weightMin)) return false;
  if (S.weightMax != null && !(b.weight_g <= S.weightMax)) return false;
  if (S.linearMax != null && !(b.linear_cm <= S.linearMax)) return false;
  if (S.presets.has("carryon") && !(b.linear_cm && b.linear_cm <= 115)) return false;
  if (S.presets.has("underseat") && !fitsUnderseat(b)) return false;
  return true;
}

const SORTS = {
  brand: (a, b) => a.brand.localeCompare(b.brand) || a.name.localeCompare(b.name),
  "price-asc":  (a, b) => nullsLast(a.price_min, b.price_min, 1),
  "price-desc": (a, b) => nullsLast(a.price_min, b.price_min, -1),
  "vol-asc":    (a, b) => nullsLast(a.volume_l, b.volume_l, 1),
  "vol-desc":   (a, b) => nullsLast(a.volume_l, b.volume_l, -1),
  "weight-asc": (a, b) => nullsLast(a.weight_g, b.weight_g, 1),
  gpl: (a, b) => nullsLast(gpl(a), gpl(b), 1),
  ppl: (a, b) => nullsLast(ppl(a), ppl(b), 1),
};
function nullsLast(x, y, dir) {
  if (x == null && y == null) return 0;
  if (x == null) return 1;
  if (y == null) return -1;
  return (x - y) * dir;
}

/* ---------- rendering ---------- */

// Filtering for brown and being shown the black colourway is a small lie.
// When a colour filter is on, the card leads with a variant that actually
// matches it — the feed ships per-colourway photography, so this costs nothing.
function shotFor(b) {
  if (!S.colors.size) return { src: b.image, bg: b.image_bg, label: null };
  const hit = (b.variants || []).find(
    v => v.image && S.colors.has(v.color_family));
  return hit ? { src: hit.image, bg: hit.image_bg, label: hit.color }
             : { src: b.image, bg: b.image_bg, label: null };
}

// image_bg.py reads the colour each photo was actually shot on, for the photos
// it can read confidently. Handing it to the plate is what stops an off-white
// product shot drawing a rectangle on a #fff plate. Absent — a cut-out PNG, a
// lifestyle shot, a background too dark for the chips to stay legible — the
// plate keeps its default white and the CSS edge feather covers the seam.
function plate(bg) {
  return bg ? ` style="--shot-bg:${esc(bg)}"` : "";
}

function card(b) {
  const on = compare.has(b.id);
  const shot = shotFor(b);
  const swatches = (b.colors || []).slice(0, 5)
    .map(c => `<i class="dot" title="${esc(c)}" style="background:${cssColor(c)}"></i>`).join("");
  const extra = (b.colors || []).length > 5 ? `<em>+${b.colors.length - 5}</em>` : "";
  const sale = b.on_sale ? '<div class="flag sale">Sale</div>' : "";
  const oos = !b.in_stock ? '<div class="flag oos">Out</div>' : "";
  const cell = (label, val) =>
    `<div class="spec"><em>${label}</em>${val ? `<b>${val}</b>` : nil}</div>`;

  return `<article class="card${on ? " sel" : ""}" data-id="${esc(b.id)}">
    <div class="shot" data-detail${plate(shot.bg)}>
      ${shot.src ? `<img loading="lazy" decoding="async" src="${
          esc(thumb(shot.src, THUMB.card))}" alt="${esc(b.name)}${
          shot.label ? ` in ${esc(shot.label)}` : ""}">`
                : '<span class="none">NO IMAGE</span>'}
      ${shot.label ? `<div class="wayname">${esc(shot.label)}</div>` : ""}
      <div class="cat">${esc(CAT_LABELS[b.category] || b.category)}</div>
      <div class="flags">${sale}${oos}</div>
    </div>
    <div class="cbody" data-detail>
      <div class="cbrand">${esc(b.brand)}</div>
      <a class="cname" href="${esc(bagHref(b))}">${esc(b.name)}</a>
    </div>
    <div class="specs">
      ${cell("Vol", fmtVol(b))}${cell("Weight", fmtWeight(b))}
      ${cell("H×W×D", fmtDims(b))}${cell("Laptop", fmtLap(b))}
    </div>
    <div class="cfoot">
      <div class="price">${fmtPrice(b.price_min)}${
        b.price_max && b.price_max !== b.price_min ? `<s>–${fmtPrice(b.price_max)}</s>` : ""}</div>
      <div class="dots">${swatches}${extra}</div>
      <button class="cmp" data-cmp aria-pressed="${on}" title="Add to comparison">${on ? "✓" : "+"}</button>
    </div>
  </article>`;
}

// Map common colourway names to a swatch. Unknown names fall back to a neutral
// block rather than an invented colour.
function cssColor(name) {
  const n = name.toLowerCase();
  const map = {
    black: "#111", jet: "#111", navy: "#1c2b4a", blue: "#2f5ea8", olive: "#4a5335",
    green: "#33623f", grey: "#7c7f7c", gray: "#7c7f7c", charcoal: "#3a3d3c",
    white: "#eee", cream: "#e6ddc9", tan: "#c2a178", brown: "#5f4632",
    red: "#a8322b", orange: "#d4601f", yellow: "#d9b02c", purple: "#5b4076",
    pink: "#c98099", sand: "#cbbb9a", khaki: "#9d8a63", coyote: "#8a6f4b",
    silver: "#b9bcbb", clear: "#8fa3a8", multicam: "#7a7150",
  };
  for (const k in map) if (n.includes(k)) return map[k];
  return "var(--line-2)";
}

// The head and one row are separate so the table can be extended a chunk at a
// time, the same way the grid is. See paintChunk().
function tableShell() {
  const head = ["Brand", "Model", "Category", "Vol L", "Weight g",
                `H×W×D ${dual("cm", "in")}`,
                `Linear ${dual("cm", "in")}`,
                "Laptop", "Price", "g/L", "$/L", "Colours"];
  return `<div class="tablewrap"><table>
    <thead><tr>${head.map(h => `<th>${h}</th>`).join("")}</tr></thead>
    <tbody></tbody></table></div>`;
}

function row(b) {
  const on = compare.has(b.id);
  const td = v => v == null ? '<td class="nil">—</td>' : `<td>${v}</td>`;
  return `<tr class="${on ? "sel" : ""}" data-id="${esc(b.id)}">
    <td>${esc(b.brand)}</td>
    <td class="name"><a href="${esc(bagHref(b))}">${esc(b.name)}</a></td>
    <td>${esc(CAT_LABELS[b.category] || b.category)}</td>
    ${td(b.volume_l)}${td(b.weight_g)}
    ${td(dualDims(b.dims_cm))}
    ${td(dualLen(b.linear_cm))}
    ${td(b.laptop_in ? b.laptop_in + "″" : null)}
    ${td(b.price_min ? fmtPrice(b.price_min) : null)}
    ${td(gpl(b) ? gpl(b).toFixed(0) : null)}
    ${td(ppl(b) ? ppl(b).toFixed(1) : null)}
    ${td((b.colors || []).length || null)}
  </tr>`;
}

// A bag whose feature list was only ever built from the brand's marketing copy
// cannot be said to lack a feature. Excluding it is still the right call — a
// filter that returns maybes is useless — but it has to be admitted to, not
// done silently, which is what was happening before.
function featuresUnknown(b) {
  return b.features_source !== "product-page";
}

/* The result set is the whole catalog when nothing is filtered, and building
 * that as one HTML string was the single thing making the page hang: 7,700
 * cards is on the order of 115,000 nodes, parsed, laid out and painted from
 * scratch on every keystroke, every facet click and every frame of a slider
 * drag.
 *
 * So a render paints a screenful and stops. An IntersectionObserver watching a
 * sentinel below the last card paints the next chunk as it approaches the
 * viewport, which makes the cost of a filter change a function of the viewport
 * rather than of the size of the catalog.
 *
 * Cards that scroll out of view stay in the DOM rather than being recycled —
 * `content-visibility` in app.css is what stops them costing layout — because
 * the alternative loses find-in-page, native scroll restoration and the link
 * targets that make a card a card. This is progressive rendering, not
 * virtualisation, and the distinction is deliberate.
 */
const CHUNK = 60;
let LIST = [];      // the current filtered, sorted result set
let painted = 0;    // how much of LIST is actually in the DOM
let sentinelIO = null;

function paintChunk() {
  const next = LIST.slice(painted, painted + CHUNK);
  if (!next.length) return false;
  const host = VIEW === "grid" ? $("#results .grid") : $("#results tbody");
  if (!host) return false;
  host.insertAdjacentHTML("beforeend",
    (VIEW === "grid" ? next.map(card) : next.map(row)).join(""));
  painted += next.length;
  return true;
}

/* Keep painting while the sentinel is still within reach of the viewport.
 *
 * The observer only fires on a *change* of intersection, so a chunk that fails
 * to push the sentinel off screen — a tall window, a short chunk, a filter
 * that leaves eighty results — would otherwise stall with the observer
 * satisfied and the page half-drawn. Reading the sentinel's position back
 * forces a synchronous layout per iteration, which is why this is bounded by
 * the viewport and not by LIST.length. */
function fill() {
  const el = $("#sentinel");
  if (!el) return;
  const reach = () => el.getBoundingClientRect().top < innerHeight + 800;
  while (painted < LIST.length && reach()) paintChunk();
  if (painted >= LIST.length) {
    sentinelIO?.disconnect();
    el.remove();
  }
}

function observeSentinel() {
  sentinelIO?.disconnect();
  const el = $("#sentinel");
  if (!el) return;
  // The margin is what keeps the next chunk ahead of the scroll rather than
  // arriving under it — nobody should watch a gap fill in.
  sentinelIO = new IntersectionObserver(
    entries => { if (entries.some(e => e.isIntersecting)) fill(); },
    { rootMargin: "800px 0px" },
  );
  sentinelIO.observe(el);
}

function render() {
  prepareFilters();

  /* One pass, not three. Each caveat count below used to re-run matches()
   * across the entire catalog, so a colour filter combined with a material
   * filter cost three full passes to answer one question. They are all
   * decided by the same per-bag verdict, so they are all taken from it. */
  LIST = [];
  const brandSet = new Set();
  let skus = 0, noFeature = 0, noColour = 0;
  const wantFeature = S.feats.size > 0 || S.mats.size > 0;
  const wantColour = S.colors.size > 0;

  for (const b of DATA.bags) {
    if (matches(b)) {
      LIST.push(b);
      brandSet.add(b.brand_slug);
      skus += b.variant_count || 0;
    } else {
      // Say so when a filter is dropping bags we simply have not established a
      // value for, rather than silently returning a shorter list.
      if (wantFeature && featuresUnknown(b)) noFeature++;
      if (wantColour && !(b.color_families || []).length) noColour++;
    }
  }
  LIST.sort(SORTS[S.sort] || SORTS.brand);

  const unknown = [];
  if (noFeature) unknown.push(`${noFeature} with no feature detection yet`);
  if (noColour) unknown.push(`${noColour} whose colourway names state no colour`);
  const caveat = unknown.length
    ? ` · <em class="warnnote">excluded: ${unknown.join(", ")} — unknown, not` +
      ` known to be absent</em>`
    : "";

  $("#count").innerHTML = `<b>${LIST.length}</b> of ${DATA.bags.length} bags` +
    ` · ${brandSet.size} brands · ${skus} SKUs` + caveat;

  const out = $("#results");
  painted = 0;
  if (!LIST.length) {
    sentinelIO?.disconnect();
    out.innerHTML = `<div class="empty"><b>No matches</b>
      Every active range filter excludes bags whose value is unknown.
      Widen a range or clear filters.</div>`;
  } else {
    out.innerHTML = (VIEW === "grid" ? '<div class="grid"></div>' : tableShell())
      + '<div id="sentinel" aria-hidden="true"></div>';
    paintChunk();
    observeSentinel();
    fill();
  }
  renderTray();
  syncURL();
}

/* Toggling a comparison used to re-render. At 7,700 cards that meant rebuilding
 * the entire result set to flip one border — and now that a render paints from
 * the top, it would also throw away everything the reader had scrolled to. The
 * selection is visible on exactly one card, so only that card is touched. */
function syncSelection(id) {
  const on = compare.has(id);
  $$(`[data-id="${CSS.escape(id)}"]`).forEach(el => {
    el.classList.toggle("sel", on);
    const btn = $("[data-cmp]", el);
    if (btn) {
      btn.setAttribute("aria-pressed", on);
      btn.textContent = on ? "✓" : "+";
    }
  });
  renderTray();
}

/* ---------- facets ---------- */

function buildFacets() {
  const count = (key, fn) => {
    const m = new Map();
    DATA.bags.forEach(b => (fn(b) || []).forEach(v =>
      m.set(v, (m.get(v) || 0) + 1)));
    return [...m.entries()].sort((a, b) => b[1] - a[1]);
  };

  $("#f-cat").innerHTML = count("cat", b => [b.category])
    .map(([v, n]) => `<button class="chip" data-f="cat" data-v="${esc(v)}" aria-pressed="false">${
      esc(CAT_LABELS[v] || v)}<span>${n}</span></button>`).join("");

  $("#f-feat").innerHTML = count("feat", b => b.features)
    .map(([v, n]) => `<button class="check" data-f="feat" data-v="${esc(v)}" aria-pressed="false"><i></i>${
      esc(FEATURE_LABELS[v] || v)}<b>${n}</b></button>`).join("");

  const colorCounts = new Map();
  DATA.bags.forEach(b => (b.color_families || []).forEach(f =>
    colorCounts.set(f, (colorCounts.get(f) || 0) + 1)));
  $("#f-color").innerHTML = COLOUR_ORDER.filter(f => colorCounts.has(f))
    .map(f => `<button class="check" data-f="color" data-v="${esc(f)}" aria-pressed="false"><i></i><span class="sw" style="background:${
      COLOUR_SWATCH[f]}"></span>${esc(COLOUR_LABELS[f])}<b>${colorCounts.get(f)}</b></button>`).join("");

  $("#f-mat").innerHTML = count("mat", b => b.materials)
    .map(([v, n]) => `<button class="check" data-f="mat" data-v="${esc(v)}" aria-pressed="false"><i></i>${
      esc(v)}<b>${n}</b></button>`).join("");

  const brands = new Map();
  DATA.bags.forEach(b => {
    const e = brands.get(b.brand_slug) || { name: b.brand, n: 0 };
    e.n++; brands.set(b.brand_slug, e);
  });
  $("#f-brand").innerHTML = [...brands.entries()]
    .sort((a, b) => a[1].name.localeCompare(b[1].name))
    .map(([slug, e]) => `<button class="check" data-f="brand" data-v="${esc(slug)}" aria-pressed="false"><i></i>${
      esc(e.name)}<b>${e.n}</b></button>`).join("");

  ["#f-brand", "#f-color", "#f-feat", "#f-mat"].forEach(mountFacetSearch);

  const cov = DATA.meta.coverage || {};
  const labels = { volume_l: "Volume", dims_cm: "Dims", weight_g: "Weight",
                   laptop_in: "Laptop", price_min: "Price" };
  $("#coverage").innerHTML = Object.entries(cov).map(([k, v]) =>
    `<div class="covrow"><em>${labels[k] || k}</em>
      <div class="covbar"><i style="width:${v.pct}%"></i></div>
      <b>${v.pct}%</b></div>`).join("");

  $("#stats").innerHTML =
    `<span><b>${DATA.meta.bag_count ?? DATA.bags.length}</b> BAGS</span>` +
    `<span><b>${DATA.meta.brand_count ?? "—"}</b> BRANDS</span>` +
    `<span><b>${DATA.meta.sku_count ?? "—"}</b> SKUS</span>`;

}

/* A list long enough to scroll inside its own panel is a list you scan rather
 * than read, and scanning for one brand among dozens is the slow way to do it.
 * Past this many options the group grows a filter box.
 *
 * Driving it off the count rather than a hand-kept list of facets means the box
 * arrives on its own the first time a facet outgrows the panel — the catalog
 * gains brands every crawl, and nobody has to remember this file when it does. */
const FACET_SEARCH_AT = 8;

// The visible label, minus the trailing <b> that carries the match count —
// otherwise typing a digit would match every option's tally.
const optionText = el => [...el.childNodes]
  .filter(n => !(n.nodeType === 1 && n.tagName === "B"))
  .map(n => n.textContent).join(" ").replace(/\s+/g, " ").trim().toLowerCase();

function mountFacetSearch(sel) {
  const list = $(sel);
  if (!list) return;
  const rows = $$(".check", list).map(el => ({ el, hay: optionText(el) }));
  if (rows.length < FACET_SEARCH_AT) return;

  // Named after its own heading, so the placeholder cannot drift out of step
  // with the group it sits under.
  const group = list.closest(".fgroup");
  const heading = ((group && $("summary", group)?.textContent) || "options")
    .split("—")[0].trim().toLowerCase();

  const box = document.createElement("input");
  Object.assign(box, { type: "search", className: "fsearch", autocomplete: "off",
                       spellcheck: false, placeholder: `filter ${heading}…` });
  box.setAttribute("aria-label", `Filter the ${heading} list`);

  const none = document.createElement("div");
  none.className = "fnone";
  none.textContent = "no match";
  none.hidden = true;

  list.before(box);
  list.after(none);

  box.addEventListener("input", () => {
    const q = box.value.trim().toLowerCase();
    let shown = 0;
    for (const r of rows) {
      // A ticked option never hides. It is still narrowing the grid, and a
      // filter you cannot see is one you cannot turn off — the count above the
      // results would be arguing with a list that no longer admits why.
      const hit = !q || r.hay.includes(q)
                  || r.el.getAttribute("aria-pressed") === "true";
      r.el.hidden = !hit;
      if (hit) shown++;
    }
    none.hidden = shown > 0;
  });
}

function clearFacetSearches() {
  $$(".fsearch").forEach(box => {
    if (!box.value) return;
    box.value = "";
    box.dispatchEvent(new Event("input"));
  });
}

/* ---------- compare ---------- */

function renderTray() {
  const tray = $("#tray");
  tray.classList.toggle("on", compare.size > 0);
  $("#picked").innerHTML = [...compare].map(id => {
    const b = BY_ID.get(id);
    return b ? `<span class="pick">${esc(b.brand)} ${esc(b.name)}
      <button data-drop="${esc(id)}">✕</button></span>` : "";
  }).join("");
  $("#cmpopen").textContent = `Compare (${compare.size})`;
}

function renderCompare() {
  const bags = [...compare].map(id => BY_ID.get(id)).filter(Boolean);
  if (!bags.length) return;

  const rows = [
    ["Brand",      b => esc(b.brand), null],
    ["Category",   b => esc(CAT_LABELS[b.category] || b.category), null],
    ["Price",      b => fmtPrice(b.price_min), b => b.price_min, "min"],
    ["Volume",     b => b.volume_l ? b.volume_l + " L" : null, b => b.volume_l, "max"],
    ["Weight",     b => b.weight_g ? b.weight_g + " g" : null, b => b.weight_g, "min"],
    ["Dimensions", b => dualDims(b.dims_cm, { sep: " × ", suffix: true }), null],
    ["Linear",     b => dualLen(b.linear_cm, true), b => b.linear_cm, "min"],
    ["Grams / L",  b => gpl(b) ? gpl(b).toFixed(0) : null, b => gpl(b), "min"],
    ["Price / L",  b => ppl(b) ? "$" + ppl(b).toFixed(1) : null, b => ppl(b), "min"],
    ["Laptop",     b => b.laptop_in ? b.laptop_in + "″" : null, null],
    ["Colourways", b => (b.colors || []).length || null, null],
    ["SKUs",       b => b.variant_count || null, null],
    ["Materials",  b => (b.materials || []).join(", ") || null, null],
    ["Features",   b => (b.features || []).map(f => FEATURE_LABELS[f] || f).join(", ") || null, null],
    ["In stock",   b => b.in_stock ? "Yes" : "No", null],
  ];

  const head = bags.map(b => `<th class="cmphead">
    ${b.image ? `<img src="${esc(thumb(b.image, THUMB.compare))}" alt=""${
      plate(b.image_bg)}>` : ""}
    <div>${esc(b.name)}</div></th>`).join("");

  const body = rows.map(([label, get, metric, best]) => {
    const vals = bags.map(get);
    const differs = new Set(vals.map(v => String(v))).size > 1;
    let bestIdx = -1;
    if (metric && bags.length > 1) {
      const nums = bags.map(metric);
      const valid = nums.filter(n => n != null);
      if (valid.length > 1) {
        const target = best === "max" ? Math.max(...valid) : Math.min(...valid);
        bestIdx = nums.indexOf(target);
      }
    }
    const tds = vals.map((v, i) =>
      `<td class="${v == null ? "nil" : ""}${i === bestIdx ? " best" : ""}">${v ?? "—"}</td>`).join("");
    return `<tr class="${differs ? "diff" : ""}"><th>${label}</th>${tds}</tr>`;
  }).join("");

  $("#cmpbody").innerHTML =
    `<table class="cmptable"><thead><tr><th></th>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

/* ---------- detail ---------- */

// Mirrors components/WatchForm.astro. The drawer is a bag's whole story for
// anyone who never opens the model page, so it gets the signup too; both share
// the submit handler in scripts/watch.js and the .watch rules in app.css, so
// only the markup is restated here.
const watchForm = b => `
  <form class="watch" data-watch>
    <input type="hidden" name="bag_id" value="${esc(b.id)}">
    <h3>Watch the price</h3>
    <p>
      Tell us where to write when the ${esc(b.name)} drops. We confirm by email
      first and every message carries a one-click unsubscribe.
    </p>
    <div class="watchrow">
      <input type="email" name="email" required placeholder="you@example.com"
             autocomplete="email" aria-label="Email address">
      <input type="number" name="max_price" min="0" step="10"
             placeholder="under $" aria-label="Only alert below this price">
      <button class="btn" type="submit">Watch</button>
    </div>
    <p class="watchmsg" role="status" aria-live="polite" hidden></p>
  </form>`;

function openDetail(id) {
  const b = BY_ID.get(id);
  if (!b) return;
  $("#dettitle").textContent = `${b.brand} — ${b.name}`;

  const row = (label, val, prov) => `<div class="drow"><em>${label}</em>
    <span>${val ?? "—"}${prov ? `<i class="prov">${esc(prov)}</i>` : ""}</span></div>`;

  const variants = (b.variants || []).map(v => `<tr>
      <td class="waycell">${v.image
          ? `<img class="waythumb" loading="lazy" decoding="async" src="${
              esc(thumb(v.image, THUMB.chip))}" alt=""${
              plate(v.image_bg)}>` : ""}${
        esc(v.color || v.title || "—")}</td>
      <td>${esc(v.sku || "—")}</td>
      <td>${v.price ? fmtPrice(v.price) : "—"}${
        v.compare_at && v.price && v.compare_at > v.price
          ? ` <s style="color:var(--ink-mute)">${fmtPrice(v.compare_at)}</s>` : ""}</td>
      <td style="color:${v.available ? "var(--ok)" : "var(--ink-mute)"}">${
        v.available ? "in stock" : "out"}</td>
    </tr>`).join("");

  $("#detbody").innerHTML = `
    ${b.image ? `<div class="shot" style="aspect-ratio:16/10;border-bottom:1px solid var(--line)${
        b.image_bg ? `;--shot-bg:${esc(b.image_bg)}` : ""}">
      <img src="${esc(thumb(b.image, THUMB.hero))}" alt="${esc(b.name)}"></div>` : ""}
    ${row("Brand", `<a href="/brands/${esc(b.brand_slug)}/">${esc(b.brand)} →</a>`)}
    ${row("Category", esc(CAT_LABELS[b.category] || b.category))}
    ${row("Price", b.price_min === b.price_max ? fmtPrice(b.price_min)
        : `${fmtPrice(b.price_min)} – ${fmtPrice(b.price_max)}`)}
    ${row("Volume", b.volume_l ? b.volume_l + " L" : null, b.volume_source)}
    ${row("Dimensions", dualDims(b.dims_cm, { sep: " × ", digits: 1, suffix: true }), b.dims_source)}
    ${row("Linear", dualLen(b.linear_cm, true))}
    ${row("Weight", b.weight_g ? b.weight_g + " g" : null, b.weight_source)}
    ${row("Grams / litre", gpl(b) ? gpl(b).toFixed(0) : null)}
    ${row("Laptop", b.laptop_in ? b.laptop_in + "″" : null)}
    ${row("Colourways", (b.colors || []).length || null)}
    ${b.materials?.length ? `<div class="tagrow">${b.materials.map(m =>
        `<span class="tag">${esc(m)}</span>`).join("")}</div>` : ""}
    ${b.features?.length ? `<div class="tagrow">${b.features.map(f =>
        `<span class="tag">${esc(FEATURE_LABELS[f] || f)}</span>`).join("")}</div>` : ""}
    ${variants ? `<table class="vartable">
      <thead><tr><th>Colourway</th><th>SKU</th><th>Price</th><th>Stock</th></tr></thead>
      <tbody>${variants}</tbody></table>` : ""}
    <div class="note">
      <a href="${esc(b.url)}" target="_blank" rel="noopener nofollow"
         data-buy="browse_overlay"
         data-buy-id="${esc(b.id)}"
         data-buy-brand="${esc(b.brand)}"
         data-buy-model="${esc(b.name)}"
         data-buy-price="${esc(b.price_min ?? "")}">Open on ${esc(b.brand)} ↗</a>
    </div>
    ${watchForm(b)}
    <a class="btn detfull" href="${esc(bagHref(b))}">Full specs &amp; price history →</a>`;
  $("#detoverlay").classList.add("on");
  // The drawer is the other place a bag gets looked at and bought from, and the
  // only one that never loads a model page. Without this the clicks it produces
  // would have no views to divide by. See scripts/analytics.js.
  trackProductView(b, "browse_overlay");
}

/* ---------- URL state ---------- */

function syncURL() {
  const p = new URLSearchParams();
  if (S.q) p.set("q", S.q);
  const sets = { ...facetSets(), preset: S.presets };
  for (const k in sets) if (sets[k].size) p.set(k, [...sets[k]].join(","));
  for (const k of ["volMin", "volMax", "priceMin", "priceMax", "weightMin", "weightMax", "linearMax"])
    if (S[k] != null) p.set(k, S[k]);
  if (S.stock) p.set("stock", "1");
  if (S.sale) p.set("sale", "1");
  if (S.sort !== "brand") p.set("sort", S.sort);
  if (VIEW !== "grid") p.set("view", VIEW);
  history.replaceState(null, "", p.toString() ? "?" + p : location.pathname);
}

function loadURL() {
  const p = new URLSearchParams(location.search);
  S.q = p.get("q") || "";
  $("#q").value = S.q;
  const sets = { ...facetSets(), preset: S.presets };
  for (const k in sets) (p.get(k) || "").split(",").filter(Boolean).forEach(v => sets[k].add(v));
  for (const k of ["volMin", "volMax", "priceMin", "priceMax", "weightMin", "weightMax", "linearMax"])
    if (p.has(k)) S[k] = Number(p.get(k));
  S.stock = p.get("stock") === "1";
  S.sale = p.get("sale") === "1";
  S.sort = p.get("sort") || "brand";
  VIEW = p.get("view") || "grid";

  $("#sort").value = S.sort;
  $("#vol-min").value = S.volMin ?? ""; $("#vol-max").value = S.volMax ?? "";
  $("#price-min").value = S.priceMin ?? ""; $("#price-max").value = S.priceMax ?? "";
  $("#weight-min").value = S.weightMin ?? ""; $("#weight-max").value = S.weightMax ?? "";
  syncUnits();
  syncSliders();
  $("#f-stock").setAttribute("aria-pressed", S.stock);
  $("#f-sale").setAttribute("aria-pressed", S.sale);
  $("#viewgrid").setAttribute("aria-pressed", VIEW === "grid");
  $("#viewtable").setAttribute("aria-pressed", VIEW === "table");
  syncFacetButtons();
}

function syncFacetButtons() {
  const sets = facetSets();
  $$("[data-f]").forEach(el =>
    el.setAttribute("aria-pressed", sets[el.dataset.f]?.has(el.dataset.v) || false));
  $$("[data-preset]").forEach(el =>
    el.setAttribute("aria-pressed", S.presets.has(el.dataset.preset)));
}

/* ---------- range sliders ---------- */

/* A dual-handle slider is two stacked <input type="range">. Native range has
 * no two-thumb mode, and stacking real inputs keeps keyboard control, focus
 * order and screen-reader labels for free, which reimplementing dragging on a
 * div would have thrown away. The CSS turns off pointer events on the tracks
 * and back on for the thumbs — that is what keeps the lower thumb grabbable
 * where the two overlap. */
const DUALS = [
  { key: "vol",    lo: "volMin",    hi: "volMax",    of: b => b.volume_l,  step: 1 },
  { key: "price",  lo: "priceMin",  hi: "priceMax",  of: b => b.price_min, step: 5 },
  { key: "weight", lo: "weightMin", hi: "weightMax", of: b => b.weight_g,  step: 25 },
];

/* Dragging fires `input` at pointer rate, and re-rendering that often is
 * exactly what makes a slider feel heavy. One render per frame is plenty —
 * and since a render now paints one chunk rather than the whole result set,
 * a frame during a drag costs sixty cards instead of several thousand. */
let frame = 0;
const schedule = () => {
  if (frame) return;
  frame = requestAnimationFrame(() => { frame = 0; render(); });
};

function paintFill(d) {
  const span = (d.bound[1] - d.bound[0]) || 1;
  const pct = v => ((Number(v) - d.bound[0]) / span) * 100;
  d.el.fill.style.left = pct(d.el.lo.value) + "%";
  d.el.fill.style.right = (100 - pct(d.el.hi.value)) + "%";
}

function initSliders() {
  for (const d of DUALS) {
    const root = $(`[data-dual="${d.key}"]`);
    if (!root) continue;
    const vals = DATA.bags.map(d.of).filter(v => v != null && isFinite(v));
    // Bounds come from the catalog, not a constant, so the track always spans
    // what actually exists. A handle parked at either end means "no bound"
    // rather than "a bound that happens to equal the extreme" — that is what
    // keeps an untouched slider out of the URL and out of the filter.
    d.bound = [0, Math.max(d.step,
      Math.ceil(Math.max(0, ...vals) / d.step) * d.step)];
    d.el = { lo: $(".lo", root), hi: $(".hi", root), fill: $(".fill", root) };

    for (const side of ["lo", "hi"]) {
      Object.assign(d.el[side], {
        min: d.bound[0], max: d.bound[1], step: d.step,
      });
      d.el[side].addEventListener("input", () => {
        let lo = Number(d.el.lo.value), hi = Number(d.el.hi.value);
        // Handles may meet but not cross; whichever one is moving pushes the
        // other rather than being blocked by it.
        if (lo > hi) side === "lo" ? (hi = lo) : (lo = hi);
        d.el.lo.value = lo; d.el.hi.value = hi;
        S[d.lo] = lo <= d.bound[0] ? null : lo;
        S[d.hi] = hi >= d.bound[1] ? null : hi;
        $(`#${d.key}-min`).value = S[d.lo] ?? "";
        $(`#${d.key}-max`).value = S[d.hi] ?? "";
        paintFill(d);
        schedule();
      });
    }
  }
  syncSliders();
}

// State → handles. Used by the number boxes, by Clear, and on load, so a
// shared URL arrives with the handles already where its query string says.
function syncSliders() {
  for (const d of DUALS) {
    if (!d.el) continue;
    d.el.lo.value = S[d.lo] ?? d.bound[0];
    d.el.hi.value = S[d.hi] ?? d.bound[1];
    paintFill(d);
  }
}

// The linear filter is the one input holding a length, so it is the one that
// converts. S.linearMax stays centimetres; only what the box shows changes.
function syncUnits() {
  const el = $("#linear-max");
  if (!el) return;
  el.placeholder = inches() ? el.dataset.phIn : el.dataset.phCm;
  el.value = S.linearMax == null ? ""
    : String(Math.round(inches() ? toIn(S.linearMax) : S.linearMax));
}

/* ---------- events ---------- */

function wire() {
  let t;
  $("#q").addEventListener("input", e => {
    clearTimeout(t);
    t = setTimeout(() => { S.q = e.target.value.trim(); render(); }, 130);
  });

  document.addEventListener("click", e => {
    // A link is a link. The card still opens the drawer, but the model name
    // navigates, so there are two ways through and neither fights the other.
    if (e.target.closest("a[href]")) return;

    const facet = e.target.closest("[data-f]");
    if (facet) {
      const set = facetSets()[facet.dataset.f];
      set.has(facet.dataset.v) ? set.delete(facet.dataset.v) : set.add(facet.dataset.v);
      facet.setAttribute("aria-pressed", set.has(facet.dataset.v));
      return render();
    }
    const preset = e.target.closest("[data-preset]");
    if (preset) {
      const k = preset.dataset.preset;
      S.presets.has(k) ? S.presets.delete(k) : S.presets.add(k);
      preset.setAttribute("aria-pressed", S.presets.has(k));
      return render();
    }
    const cmp = e.target.closest("[data-cmp]");
    if (cmp) {
      const id = cmp.closest("[data-id]").dataset.id;
      if (compare.has(id)) compare.delete(id);
      else if (compare.size >= 6) return;
      else compare.add(id);
      return syncSelection(id);
    }
    const drop = e.target.closest("[data-drop]");
    if (drop) {
      compare.delete(drop.dataset.drop);
      return syncSelection(drop.dataset.drop);
    }

    const det = e.target.closest("[data-detail]");
    if (det) return openDetail(det.closest("[data-id]").dataset.id);

    if (e.target.closest("[data-close]") || e.target.classList.contains("overlay")) {
      $$(".overlay").forEach(o => o.classList.remove("on"));
    }
  });

  const num = (id, key) => $(id).addEventListener("input", e => {
    S[key] = e.target.value === "" ? null : Number(e.target.value);
    syncSliders();
    render();
  });
  num("#vol-min", "volMin"); num("#vol-max", "volMax");
  num("#price-min", "priceMin"); num("#price-max", "priceMax");
  num("#weight-min", "weightMin"); num("#weight-max", "weightMax");

  $("#linear-max").addEventListener("input", e => {
    const v = e.target.value === "" ? null : Number(e.target.value);
    S.linearMax = v == null ? null
      : Number((inches() ? v * CM_PER_IN : v).toFixed(1));
    render();
  });

  // Only the length inputs need touching: every other length on the page ships
  // in both units already and CSS is doing the switching.
  document.addEventListener("unitchange", syncUnits);

  const toggle = (id, key) => $(id).addEventListener("click", e => {
    S[key] = !S[key];
    e.currentTarget.setAttribute("aria-pressed", S[key]);
    render();
  });
  toggle("#f-stock", "stock");
  toggle("#f-sale", "sale");

  $("#sort").addEventListener("change", e => { S.sort = e.target.value; render(); });

  $("#clear").addEventListener("click", () => {
    Object.assign(S, {
      q: "", volMin: null, volMax: null, priceMin: null, priceMax: null,
      weightMin: null, weightMax: null, linearMax: null, stock: false,
      sale: false, sort: "brand",
    });
    [S.cats, S.brands, S.feats, S.mats, S.colors, S.presets].forEach(s => s.clear());
    // The facet boxes hide options rather than filter bags, so they survive a
    // render untouched — Clear has to say so explicitly or the rail keeps a
    // half-hidden brand list after everything else has reset.
    clearFacetSearches();
    history.replaceState(null, "", location.pathname);
    loadURL();
    render();
  });

  const setView = v => {
    VIEW = v;
    $("#viewgrid").setAttribute("aria-pressed", v === "grid");
    $("#viewtable").setAttribute("aria-pressed", v === "table");
    render();
  };
  $("#viewgrid").addEventListener("click", () => setView("grid"));
  $("#viewtable").addEventListener("click", () => setView("table"));

  $("#cmpopen").addEventListener("click", () => {
    renderCompare();
    $("#cmpoverlay").classList.add("on");
  });
  $("#cmpclear").addEventListener("click", () => {
    const ids = [...compare];
    compare.clear();
    ids.forEach(syncSelection);
  });

  $("#railtoggle").addEventListener("click", () => $("#rail").classList.toggle("on"));

  // The theme toggle lives in the layout now — every page has one, so wiring it
  // here as well would bind it twice and cancel itself out.

  document.addEventListener("keydown", e => {
    if (e.key === "Escape") $$(".overlay").forEach(o => o.classList.remove("on"));
    if (e.key === "/" && document.activeElement !== $("#q")) {
      e.preventDefault(); $("#q").focus();
    }
  });
}

/* ---------- boot ---------- */

(async function init() {
  try {
    const res = await fetch("/bags.json", { cache: "no-cache" });
    if (!res.ok) throw new Error(res.status);
    DATA = await res.json();
  } catch (err) {
    // Leave the server-rendered fallback list in place — it is every model on
    // the site, just without the filters. A degraded page beats a dead one.
    $("#count").textContent = "filters unavailable — showing all models";
    console.error("gearherd: could not load /bags.json", err);
    return;
  }
  BY_ID = new Map(DATA.bags.map(b => [b.id, b]));
  buildFacets();
  // Before loadURL, which pushes any query-string bounds onto the handles.
  initSliders();
  loadURL();
  wire();
  render();
})();
