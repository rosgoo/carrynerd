/* bagdex — the browse/compare island.
 *
 * Loads the whole catalog once and filters in memory. That is deliberate: at a
 * few thousand models it beats a round trip per keystroke, and it means the
 * catalog needs no runtime service behind it. The per-model pages are static
 * HTML generated at build time; this is the one interactive surface.
 */

import { CAT_LABELS, FEATURE_LABELS } from '../lib/labels.js';

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

let DATA = { meta: {}, bags: [] };
let VIEW = "grid";
const compare = new Set();

const S = {
  q: "", cats: new Set(), brands: new Set(), feats: new Set(), mats: new Set(),
  volMin: null, volMax: null, priceMin: null, priceMax: null,
  weightMin: null, weightMax: null, linearMax: null,
  presets: new Set(), stock: false, sale: false, sort: "brand",
};

/* ---------- formatting ---------- */

const nil = '<b class="nil">—</b>';
const fmtVol    = b => b.volume_l ? `${b.volume_l}<em style="font-size:8px">L</em>` : null;
const fmtWeight = b => b.weight_g ? (b.weight_g >= 1000
  ? `${(b.weight_g / 1000).toFixed(2)}<em style="font-size:8px">kg</em>`
  : `${b.weight_g}<em style="font-size:8px">g</em>`) : null;
const fmtDims   = b => b.dims_cm ? b.dims_cm.map(n => Math.round(n)).join("×") : null;
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

function matches(b) {
  if (S.q) {
    const terms = S.q.toLowerCase().split(/\s+/).filter(Boolean);
    const hay = haystack(b);
    if (!terms.every(t => hay.includes(t))) return false;
  }
  if (S.cats.size && !S.cats.has(b.category)) return false;
  if (S.brands.size && !S.brands.has(b.brand_slug)) return false;
  if (S.feats.size && ![...S.feats].every(f => (b.features || []).includes(f))) return false;
  if (S.mats.size && ![...S.mats].some(m => (b.materials || []).includes(m))) return false;
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

function card(b) {
  const on = compare.has(b.id);
  const swatches = (b.colors || []).slice(0, 5)
    .map(c => `<i class="dot" title="${esc(c)}" style="background:${cssColor(c)}"></i>`).join("");
  const extra = (b.colors || []).length > 5 ? `<em>+${b.colors.length - 5}</em>` : "";
  const sale = b.on_sale ? '<div class="flag sale">Sale</div>' : "";
  const oos = !b.in_stock ? '<div class="flag oos">Out</div>' : "";
  const cell = (label, val) =>
    `<div class="spec"><em>${label}</em>${val ? `<b>${val}</b>` : nil}</div>`;

  return `<article class="card${on ? " sel" : ""}" data-id="${esc(b.id)}">
    <div class="shot" data-detail>
      ${b.image ? `<img loading="lazy" src="${esc(b.image)}" alt="${esc(b.name)}">`
                : '<span class="none">NO IMAGE</span>'}
      <div class="cat">${esc(CAT_LABELS[b.category] || b.category)}</div>
      <div class="flags">${sale}${oos}</div>
    </div>
    <div class="cbody" data-detail>
      <div class="cbrand">${esc(b.brand)}</div>
      <div class="cname">${esc(b.name)}</div>
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

function tableRows(list) {
  const head = ["Brand", "Model", "Category", "Vol L", "Weight g", "H×W×D cm",
                "Linear", "Laptop", "Price", "g/L", "$/L", "Colours"];
  const rows = list.map(b => {
    const on = compare.has(b.id);
    const td = v => v == null ? '<td class="nil">—</td>' : `<td>${v}</td>`;
    return `<tr class="${on ? "sel" : ""}" data-id="${esc(b.id)}">
      <td>${esc(b.brand)}</td>
      <td class="name" data-detail>${esc(b.name)}</td>
      <td>${esc(CAT_LABELS[b.category] || b.category)}</td>
      ${td(b.volume_l)}${td(b.weight_g)}
      ${td(b.dims_cm ? b.dims_cm.map(n => n.toFixed(0)).join("×") : null)}
      ${td(b.linear_cm ? Math.round(b.linear_cm) : null)}
      ${td(b.laptop_in ? b.laptop_in + "″" : null)}
      ${td(b.price_min ? fmtPrice(b.price_min) : null)}
      ${td(gpl(b) ? gpl(b).toFixed(0) : null)}
      ${td(ppl(b) ? ppl(b).toFixed(1) : null)}
      ${td((b.colors || []).length || null)}
    </tr>`;
  }).join("");
  return `<div class="tablewrap"><table>
    <thead><tr>${head.map(h => `<th>${h}</th>`).join("")}</tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

// A bag whose feature list was only ever built from the brand's marketing copy
// cannot be said to lack a feature. Excluding it is still the right call — a
// filter that returns maybes is useless — but it has to be admitted to, not
// done silently, which is what was happening before.
function featuresUnknown(b) {
  return b.features_source !== "product-page";
}

function render() {
  const list = DATA.bags.filter(matches).sort(SORTS[S.sort] || SORTS.brand);

  let caveat = "";
  if (S.feats.size || S.mats.size) {
    const unsure = DATA.bags.filter(b => featuresUnknown(b) && !matches(b)).length;
    if (unsure) {
      caveat = ` · <em class="warnnote">${unsure} excluded — features not yet` +
        ` detected on those, not known to be absent</em>`;
    }
  }

  $("#count").innerHTML = `<b>${list.length}</b> of ${DATA.bags.length} bags` +
    ` · ${new Set(list.map(b => b.brand_slug)).size} brands` +
    ` · ${list.reduce((n, b) => n + (b.variant_count || 0), 0)} SKUs` + caveat;

  const out = $("#results");
  if (!list.length) {
    out.innerHTML = `<div class="empty"><b>No matches</b>
      Every active range filter excludes bags whose value is unknown.
      Widen a range or clear filters.</div>`;
  } else {
    out.innerHTML = VIEW === "grid"
      ? `<div class="grid">${list.map(card).join("")}</div>`
      : tableRows(list);
  }
  renderTray();
  syncURL();
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

  $("#srcnote").innerHTML =
    `Built from public Shopify product feeds and schema.org Product data.
     Specs are extracted, not hand-checked — verify against the brand before
     buying. Prices as fetched, USD.`;
}

/* ---------- compare ---------- */

function renderTray() {
  const tray = $("#tray");
  tray.classList.toggle("on", compare.size > 0);
  $("#picked").innerHTML = [...compare].map(id => {
    const b = DATA.bags.find(x => x.id === id);
    return b ? `<span class="pick">${esc(b.brand)} ${esc(b.name)}
      <button data-drop="${esc(id)}">✕</button></span>` : "";
  }).join("");
  $("#cmpopen").textContent = `Compare (${compare.size})`;
}

function renderCompare() {
  const bags = [...compare].map(id => DATA.bags.find(b => b.id === id)).filter(Boolean);
  if (!bags.length) return;

  const rows = [
    ["Brand",      b => esc(b.brand), null],
    ["Category",   b => esc(CAT_LABELS[b.category] || b.category), null],
    ["Price",      b => fmtPrice(b.price_min), b => b.price_min, "min"],
    ["Volume",     b => b.volume_l ? b.volume_l + " L" : null, b => b.volume_l, "max"],
    ["Weight",     b => b.weight_g ? b.weight_g + " g" : null, b => b.weight_g, "min"],
    ["Dimensions", b => b.dims_cm ? b.dims_cm.map(n => n.toFixed(0)).join(" × ") + " cm" : null, null],
    ["Linear",     b => b.linear_cm ? Math.round(b.linear_cm) + " cm" : null, b => b.linear_cm, "min"],
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
    ${b.image ? `<img src="${esc(b.image)}" alt="">` : ""}
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

function openDetail(id) {
  const b = DATA.bags.find(x => x.id === id);
  if (!b) return;
  $("#dettitle").textContent = `${b.brand} — ${b.name}`;

  const row = (label, val, prov) => `<div class="drow"><em>${label}</em>
    <span>${val ?? "—"}${prov ? `<i class="prov">${esc(prov)}</i>` : ""}</span></div>`;

  const variants = (b.variants || []).map(v => `<tr>
      <td>${esc(v.color || v.title || "—")}</td>
      <td>${esc(v.sku || "—")}</td>
      <td>${v.price ? fmtPrice(v.price) : "—"}${
        v.compare_at && v.price && v.compare_at > v.price
          ? ` <s style="color:var(--ink-mute)">${fmtPrice(v.compare_at)}</s>` : ""}</td>
      <td style="color:${v.available ? "var(--ok)" : "var(--ink-mute)"}">${
        v.available ? "in stock" : "out"}</td>
    </tr>`).join("");

  $("#detbody").innerHTML = `
    ${b.image ? `<div class="shot" style="aspect-ratio:16/10;border-bottom:1px solid var(--line)">
      <img src="${esc(b.image)}" alt="${esc(b.name)}"></div>` : ""}
    ${row("Category", esc(CAT_LABELS[b.category] || b.category))}
    ${row("Price", b.price_min === b.price_max ? fmtPrice(b.price_min)
        : `${fmtPrice(b.price_min)} – ${fmtPrice(b.price_max)}`)}
    ${row("Volume", b.volume_l ? b.volume_l + " L" : null, b.volume_source)}
    ${row("Dimensions", b.dims_cm ? b.dims_cm.map(n => n.toFixed(1)).join(" × ") + " cm" : null, b.dims_source)}
    ${row("Linear", b.linear_cm ? Math.round(b.linear_cm) + " cm" : null)}
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
      Source: ${esc(b.source || "—")} · fetched ${esc((b.fetched_at || "").slice(0, 10))}<br>
      <a href="${esc(b.url)}" target="_blank" rel="noopener nofollow">Open on ${esc(b.brand)} ↗</a>
    </div>
    <a class="btn detfull" href="${esc(bagHref(b))}">Full specs &amp; price history →</a>`;
  $("#detoverlay").classList.add("on");
}

/* ---------- URL state ---------- */

function syncURL() {
  const p = new URLSearchParams();
  if (S.q) p.set("q", S.q);
  const sets = { cat: S.cats, brand: S.brands, feat: S.feats, mat: S.mats, preset: S.presets };
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
  const sets = { cat: S.cats, brand: S.brands, feat: S.feats, mat: S.mats, preset: S.presets };
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
  $("#linear-max").value = S.linearMax ?? "";
  $("#f-stock").setAttribute("aria-pressed", S.stock);
  $("#f-sale").setAttribute("aria-pressed", S.sale);
  $("#viewgrid").setAttribute("aria-pressed", VIEW === "grid");
  $("#viewtable").setAttribute("aria-pressed", VIEW === "table");
  syncFacetButtons();
}

function syncFacetButtons() {
  const sets = { cat: S.cats, brand: S.brands, feat: S.feats, mat: S.mats };
  $$("[data-f]").forEach(el =>
    el.setAttribute("aria-pressed", sets[el.dataset.f]?.has(el.dataset.v) || false));
  $$("[data-preset]").forEach(el =>
    el.setAttribute("aria-pressed", S.presets.has(el.dataset.preset)));
}

/* ---------- events ---------- */

function wire() {
  let t;
  $("#q").addEventListener("input", e => {
    clearTimeout(t);
    t = setTimeout(() => { S.q = e.target.value.trim(); render(); }, 130);
  });

  document.addEventListener("click", e => {
    const facet = e.target.closest("[data-f]");
    if (facet) {
      const sets = { cat: S.cats, brand: S.brands, feat: S.feats, mat: S.mats };
      const set = sets[facet.dataset.f];
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
      return render();
    }
    const drop = e.target.closest("[data-drop]");
    if (drop) { compare.delete(drop.dataset.drop); return render(); }

    const det = e.target.closest("[data-detail]");
    if (det) return openDetail(det.closest("[data-id]").dataset.id);

    if (e.target.closest("[data-close]") || e.target.classList.contains("overlay")) {
      $$(".overlay").forEach(o => o.classList.remove("on"));
    }
  });

  const num = (id, key) => $(id).addEventListener("input", e => {
    S[key] = e.target.value === "" ? null : Number(e.target.value);
    render();
  });
  num("#vol-min", "volMin"); num("#vol-max", "volMax");
  num("#price-min", "priceMin"); num("#price-max", "priceMax");
  num("#weight-min", "weightMin"); num("#weight-max", "weightMax");
  num("#linear-max", "linearMax");

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
    [S.cats, S.brands, S.feats, S.mats, S.presets].forEach(s => s.clear());
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
  $("#cmpclear").addEventListener("click", () => { compare.clear(); render(); });

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
    console.error("bagdex: could not load /bags.json", err);
    return;
  }
  buildFacets();
  loadURL();
  wire();
  render();
})();
