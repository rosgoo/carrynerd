/* carrynerd — the browse/compare island.
 *
 * Filtering happens at /api/browse and this file draws the answer. That is a
 * deliberate reversal of how it worked: the island used to hold an index of the
 * whole catalog and match against it in memory, which costs nothing per
 * keystroke and also publishes every spec the crawl has assembled to anyone who
 * opens a network tab once. The catalog is the work. A round trip per filter
 * change is what it costs not to hand it over in a single file.
 *
 * So state lives here, matching lives there, and the two meet in a query
 * string — the same one in the address bar, so a shareable link and a request
 * are spelled the same way. stateParams() is the only place that mapping is
 * written down.
 *
 * A response carries one page of sixty bags, whole: the specs a card prints,
 * the photography, the storefront link and the per-variant rows. There is no
 * second fetch to fill a card in behind the reader any more — the page cap is
 * what keeps the expensive columns from leaving in bulk, and it does that job
 * whether or not they travel with the specs.
 *
 * Drawing the answer is still the expensive half, so it is still bounded by the
 * viewport: a render paints a page and an IntersectionObserver asks for the
 * next one as the sentinel comes into reach — for five pages at a stretch,
 * after which it retires and a button takes its place, because a scroll that
 * never ends is a scroll that never reaches the footer. See BURST below. Cards
 * that scroll out of view stay in the DOM rather than being recycled —
 * `content-visibility` on .card in app.css is what stops them costing layout —
 * because the alternative loses find-in-page, native scroll restoration and the
 * link targets that make a card a card. This is progressive rendering, not
 * virtualisation.
 */

import { CAT_LABELS, FEATURE_LABELS, COLOUR_LABELS, COLOUR_SWATCH,
         COLOUR_ORDER, cssColor } from '../lib/labels.js';
import { thumb, THUMB } from '../lib/thumb.js';
import { favourites, toggleFavourite, MAX_FAVOURITES,
         savedFilters, saveFilter, forgetFilter, MAX_FILTERS } from '../lib/store.js';
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

/* Every facet's state in one place. This mapping used to be re-declared in
 * four separate functions, which meant adding a facet meant remembering all
 * four.
 *
 * Laptop is drawn as a group in the rail and is missing from here on purpose.
 * It is one-of rather than any-of — a single rung, or none — so there is no set
 * to add to, and it spells itself `laptopMin=15` in the URL, which is what
 * shared links and saved filters have always said and what the server has
 * always parsed. Wrapping a Set around it would have bought one line here at
 * the price of a second spelling for the same filter, and the spelling is the
 * half that is load-bearing. So it lives in S.laptopMin with the ranges below,
 * RANGE_KEYS carries it through the URL both ways, and the two functions that
 * walk [data-f] know about it by name: the click handler in wire(), and
 * syncFacetButtons(). Anything else added to the rail belongs in this table. */
const facetSets = () => ({
  cat: S.cats, brand: S.brands, feat: S.feats, mat: S.mats, color: S.colors,
  airline: S.airlines,
});

// Same reasoning, for the numeric half of the state: the URL writer, the URL
// reader and the query builder all walk this list rather than each keeping
// their own copy of it.
const RANGE_KEYS = ["volMin", "volMax", "priceMin", "priceMax",
                    "weightMin", "weightMax", "linearMax", "laptopMin"];

const compare = new Set();

// id → bag, for every bag this session has been sent. A page of results is the
// only door a record comes in through, so the tray, the comparison sheet and
// the drawer can all resolve an id here rather than asking again for something
// already on screen.
let BY_ID = new Map();

// The catalog's own numbers, from the boot response: the denominator in the
// count line, and the strip in the header.
let STATS = {};

// slug → printed name, taken from the boot response's brand facet. Only the
// name of a saved filter needs it: "Peak Design" rather than "peak-design".
let BRAND_NAMES = new Map();

// The same, for carriers: "Ryanair" rather than "ryanair", and "Air New
// Zealand" rather than a slug that reads as three words run together.
let AIRLINE_NAMES = new Map();

/* The bags this browser has starred, as a Set for the card renderer.
 *
 * lib/store.js owns the list and localStorage owns the storage; this is a copy
 * taken at the top of every render and after every toggle, so a second tab
 * cannot leave it lying. Deliberately not part of S: S is the filter, and a
 * favourite is not one — it changes where a bag is drawn, not whether it
 * matches. The exception is S.favOnly, which is a filter and is spelled in the
 * URL like the rest of them. */
let FAV = new Set();

/* ---------- the page's own filter ---------- */

/* Some pages are a filter before a reader touches one. /brands/able-carry/ runs
 * this same island with the brand already settled by the path, and /sale/ runs
 * it with `on_sale` settled the same way; the shell says which in markup — see
 * components/BrowseShell.astro.
 *
 * It is deliberately not folded into S. S is what the reader chose, and what
 * the reader chose is what goes in the address bar, what a saved filter keeps
 * and what Clear empties; the brand on a brand page survives all three, because
 * the path already says it and clearing your way onto the whole catalogue
 * without moving is not something a page should let you do. So it rides on the
 * request as `scope`, which is also what tells the server to count the rail
 * against that brand's shelf rather than against the catalogue.
 *
 * `unlock` is the same filter spelled as an ordinary one, for the way out: the
 * front page with this brand merely ticked, or with On sale merely pressed.
 *
 * A page may also arrive with an opinion about the order. /sale/ opens on the
 * deepest discount, because brand A–Z on a page whose whole premise is a price
 * cut is a table of contents rather than an answer. It is a default and not a
 * lock: the sort select still works, and picking anything else leaves the
 * markup's opinion behind. */
const shell = document.getElementById("shell");
const LOCK = new URLSearchParams(shell?.dataset.lock || "");
const UNLOCK = shell?.dataset.unlock || "";
/* Which kind of scope, for the two places that print a count of brands: on a
 * brand page that count is always 1 and both leave it out. Not "is this page
 * scoped at all" — on /sale/ it is 75 brands and worth printing. */
const ONE_BRAND = (LOCK.get("scope") || "").startsWith("brand:");
/* The carrier an airline page is, where that is what this page is. Read out of
 * the same lock rather than given a data attribute of its own: the scope is
 * already spelled there, and a second spelling of one fact is a second thing
 * that can be wrong.
 *
 * The Airline group survives on that page — every-of, so a second carrier is an
 * itinerary rather than a contradiction — but this row does not: it is the
 * page, so pressing it changes nothing and unpressing it is not on offer, and a
 * control that answers neither way is worse than one that is not there. The
 * scope panel at the top of the rail is what says it is on. */
const SCOPE_AIRLINE = (LOCK.get("scope") || "").startsWith("airline:")
  ? LOCK.get("scope").slice("airline:".length)
  : "";
const DEFAULT_SORT = shell?.dataset.sort || "brand";

const S = {
  q: "", cats: new Set(), brands: new Set(), feats: new Set(), mats: new Set(),
  colors: new Set(),
  // Carriers whose published carry-on limit a bag has to clear — every one
  // ticked, not any. See the airline group in components/BrowseShell.astro.
  airlines: new Set(),
  volMin: null, volMax: null, priceMin: null, priceMax: null,
  weightMin: null, weightMax: null, linearMax: null, laptopMin: null,
  presets: new Set(), stock: false, sale: false, favOnly: false,
  sort: DEFAULT_SORT,
};

/* ---------- units ---------- */

/* Lengths and weights render in both units at once and CSS reveals one, keyed
 * off data-units on <html>. Same mechanism the static pages use, and for the
 * same reason: switching units then costs no re-render and cannot flash the
 * wrong number.
 *
 * Weight was not in that sentence until now, and its absence was a bug rather
 * than a decision: the toggle wrote "in", every length obeyed, and the grams
 * beside them sat there unconverted. It is the same construction as the lengths
 * because it is the same problem — nothing here converts on the fly, both
 * spellings ship and the attribute decides which one is displayed.
 *
 * Everything in S stays metric. The filter arithmetic, the shareable URL and
 * the catalog all speak centimetres and grams; inches and pounds exist only at
 * the point a number meets an eye. A link someone sends means the same bags
 * whichever unit either end happens to be reading in — which is also why the
 * weight range in the rail is still spelled in grams; see the note on that
 * group in components/BrowseShell.astro. */
const CM_PER_IN = 2.54;
const G_PER_LB = 453.59237;
const G_PER_OZ = 28.349523;
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

/* Both halves change unit partway up the scale — grams to kilograms at a kilo,
 * ounces to pounds at a pound — so this is a figure and its unit rather than a
 * figure with a unit known in advance, which is what the callers below need it
 * split into. Mirrors fmtWeightBoth() in lib/format.js; the static pages and
 * the island have to round a bag's weight to the same string or the same bag
 * reads differently on its model page and in the grid. */
const weightPair = g => [
  g >= 1000 ? [(g / 1000).toFixed(2), "kg"] : [String(g), "g"],
  g >= G_PER_LB ? [(g / G_PER_LB).toFixed(1), "lb"]
                : [String(Math.round(g / G_PER_OZ)), "oz"],
];

/* `unit` is how the suffix is drawn, because the cards set it eight pixels tall
 * against the figure and everywhere else it is simply the next word. */
const dualWeight = (g, unit = u => ` ${u}`) => {
  if (!g) return null;
  const [metric, imperial] = weightPair(g);
  return dual(metric[0] + unit(metric[1]), imperial[0] + unit(imperial[1]));
};

/* ---------- formatting ---------- */

/* The dash, and the whole of what used to be said about it.
 *
 * Four pages carried a sentence explaining that "—" means not established
 * rather than zero, to readers who had not asked about the dash. The title is
 * where an answer belongs when the question is optional: it costs no words on
 * the page and it is there for the one reader who hovers. The long version is
 * on /about, and the coverage panel still says it once in prose. */
const NIL_TITLE = 'Not established — never estimated';
const nil = `<b class="nil" title="${NIL_TITLE}">—</b>`;

/* Thousands separators on the counts.
 *
 * The island wrote these raw — "33414 SKUs" — while the static pages' header
 * printed the same three numbers through toLocaleString, so the front page and
 * a hub page disagreed about how to spell the size of the same catalogue. Five
 * figures is where a separator stops being decoration and starts being how you
 * read the number at a glance, which is the whole job of that line. */
const num = n => (n == null ? "—" : Number(n).toLocaleString("en-US"));
const fmtVol    = b => b.volume_l ? `${b.volume_l}<em style="font-size:8px">L</em>` : null;
const fmtWeight = b =>
  dualWeight(b.weight_g, u => `<em style="font-size:8px">${u}</em>`);
const fmtDims   = b => dualDims(b.dims_cm);
const fmtLap    = b => b.laptop_in ? `${b.laptop_in}″` : null;
/* Prices are never converted, so every one has to carry its own symbol.
   normalize.py stamps each bag with the currency its storefront quotes; a
   bare "$" here is what published Bedouin Foundry's pounds as dollars. */
const CUR_SYMBOLS = { USD: "$", GBP: "£", EUR: "€", CAD: "CA$", AUD: "AU$", JPY: "¥" };
const cur = c => CUR_SYMBOLS[(c || "USD").toUpperCase()] ?? `${(c || "").toUpperCase()} `;
const fmtPrice  = (n, c) => n == null ? "—" : cur(c) + (Number.isInteger(n) ? n : n.toFixed(2));
const gpl = b => (b.weight_g && b.volume_l) ? b.weight_g / b.volume_l : null;

/* The price to reckon with, as opposed to the price to print.
 *
 * Eight brands quote something other than dollars. `price_min` is what they
 * charge and is what the card shows; `price_usd_approx` is normalize.py's
 * estimate of it in dollars at a dated ECB rate, and it is what anything
 * comparing two bags has to use or the comparison is between a number of
 * pounds and a number of dollars. The server sorts and filters on exactly this
 * — see price_cmp in pages/api/browse.ts — and the table below sorts on it too,
 * because a column that reorders differently from the control above it reads
 * as a bug whichever one is right. */
const cmpPrice = b => b.price_usd_approx ?? b.price_min;
const ppl = b => (cmpPrice(b) && b.volume_l) ? cmpPrice(b) / b.volume_l : null;

/* "≈ $433", and null on a bag priced in dollars, which is most of them. */
const approxUsd = b => b.price_usd_approx == null ? null
  : "≈ $" + (b.price_usd_approx >= 100
      ? Math.round(b.price_usd_approx).toLocaleString("en-US")
      : b.price_usd_approx.toFixed(2));

/* Named on the page rather than left implicit: a converted figure with no rate
 * beside it is a number the reader has to trust. Filled from the boot payload.
 * See fx.py for why the rate carries a date instead of being live. */
let FX = null;
const fxNote = currency => {
  const rate = FX?.rates?.[(currency || "").toUpperCase()];
  if (!FX?.date || !rate) return "";
  const d = new Date(`${FX.date}T00:00:00Z`);
  const when = isNaN(d) ? FX.date : d.toLocaleDateString("en-GB",
    { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
  return `European Central Bank reference rate, ${when} — 1 USD = `
       + `${rate < 10 ? rate.toFixed(4) : rate.toFixed(2)} `
       + `${(currency || "").toUpperCase()}`;
};

/* ---------- asking the server ---------- */

/* Matching used to be a function here — haystack(), matches(), a table of
 * comparators — and it is now a port of the same code in pages/api/browse.ts.
 * The rules did not change: AND across the search terms, every-of on features,
 * any-of on materials and colours, and a range that refuses to be satisfied by
 * an unknown value. If a filter ever starts answering differently from the way
 * this file documents it, that file is where the answer is decided.
 *
 * What is left here is the asking, and the asking has to survive being
 * interrupted. A reader typing into the search box and then clicking a facet
 * has two answers coming, and only the second is about the page in front of
 * them. So every state change takes a number: it aborts what the previous state
 * left open and refuses to paint any response that lands under an older one.
 * Both halves are needed — abort does not reach a response already sitting in
 * the promise queue. */
let gen = 0;        // the current state, as a number
let ctrl = null;    // aborts what the state before it left in flight
let loading = false;

/* Aborting a fetch rejects a promise inside the Response that nothing here can
 * reach — Chrome does it even when the body has already been read — so a
 * cancelled request prints an uncaught AbortError however carefully the call
 * site catches its own. Swallowed by name and by name only: an abort is this
 * file doing its job, and the console has to stay somewhere a real error is
 * visible. Nothing else on the page aborts anything. */
addEventListener("unhandledrejection", e => {
  if (e.reason?.name === "AbortError") e.preventDefault();
});

/* One page. Agrees with the default in pages/api/browse.ts, which caps a
 * request at 100 — the cap is what makes acquiring the catalog a crawl rather
 * than a download, so the number to leave alone is that one, not this. */
const PER = 60;

let PAGE = 0;       // the last page painted
/* How many matches are paged. Favourites are lifted out of the pages and drawn
 * above them in one go, so they count towards the total in the line above the
 * grid — which answers "how many match" — and not towards this, which is what
 * the scroll asks whether there is another page of. */
let PAGED = 0;

/* How many pages the scroll is allowed to add on its own before it stops and
 * asks.
 *
 * It used to page forever, and on the front page that meant the footer could
 * not be reached at all: 7,639 bags at sixty a page is a hundred and twenty-
 * seven pages, and every one of them arrived while the reader was still on
 * their way to the bottom of the one before. The footer carries the category
 * hubs, About and Privacy, and on the front page it is the only route to
 * several of them — a scroll that keeps the bottom of the document permanently
 * five hundred pixels away is not a convenience, it is a trap.
 *
 * Three pages — a hundred and eighty bags — from each start the reader asked
 * for, then the observer retires and a Load more button stands where the
 * sentinel was. A button from the very first page was the other obvious fix
 * and it charges everybody a press to get anywhere; this keeps the scrolling
 * effortless for someone browsing and still guarantees the page comes to rest.
 *
 * Five to begin with, and three after somebody said the footer was still out
 * of reach — three hundred cards is a long way to scroll before the first stop
 * and the trap was still a trap, just a shorter one. Three is roughly a
 * screenful on a desk and several on a phone, which is enough to be browsing
 * rather than clicking.
 *
 * The number is only half the answer, and the smaller half. A footer is a bad
 * place to keep the only route to a section of the site whatever the scroll
 * does, so /carry-on/ is in the header now — see the note in layouts/Base.astro
 * — and this stopped being the thing standing between a reader and half the
 * catalogue. It is a reachable bottom of a document, which is all it should
 * ever have been.
 */
const BURST = 3;
let burst = 0;      // pages loaded on the scroll's own initiative since then

async function ask(page, boot = false) {
  const p = stateParams();
  // The one thing in the request that is not in the address bar, because the
  // path is already carrying it.
  for (const [k, v] of LOCK) p.set(k, v);
  p.set("page", page);
  p.set("per", PER);
  if (boot) p.set("boot", "1");
  /* Not in stateParams, and so not in the address bar: these ids are this
   * browser's, and a link someone shares should carry their filter rather than
   * a list of the bags they happen to have starred. The server only uses them
   * to decide what to lift above the page — unless favonly is on, which is in
   * stateParams because it genuinely does change which bags match. */
  if (FAV.size) p.set("fav", [...FAV].join(","));
  const res = await fetch(`/api/browse?${p}`, { signal: ctrl?.signal });
  if (!res.ok) throw new Error(res.status);
  const data = await res.json();
  // The one door a record comes in through. Everything downstream — the drawer,
  // the tray, the comparison sheet — reads bags out of here rather than asking
  // for one it has already been given.
  for (const b of data.bags) BY_ID.set(b.id, b);
  return data;
}

/* ---------- rendering ---------- */

// Filtering for brown and being shown the black colourway is a small lie.
// When a colour filter is on, the card leads with a variant that actually
// matches it — the feed ships per-colourway photography, so this costs nothing.
// A bag with no photograph anywhere returns the empty shot and the card draws
// its placeholder, which is a state the markup already has.
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

/* The whole plate, not just the <img>: the photograph, the colour it was shot
 * on and the colourway caption are one decision — see shotFor() — and drawing
 * them from three places would be three chances for them to disagree. */
function shotBlock(b) {
  const shot = shotFor(b);
  // How far, when the listing states both numbers — "Sale" says a price moved,
  // "−30%" says whether it is worth opening. The deepest colourway, which is
  // what /sale/ ranks on; see saleDiscount() in lib/catalog.js. A move that
  // rounds to nothing keeps the word rather than printing "−0%".
  const off = b.discount ? Math.round(b.discount * 100) : 0;
  const sale = b.on_sale
    ? `<div class="flag sale">${off >= 1 ? `−${off}%` : "Sale"}</div>` : "";
  const oos = !b.in_stock ? '<div class="flag oos">Out</div>' : "";
  return `<div class="shot" data-detail${plate(shot.bg)}>
      ${shot.src ? `<img loading="lazy" decoding="async" src="${
          esc(thumb(shot.src, THUMB.card))}" alt="${esc(b.name)}${
          shot.label ? ` in ${esc(shot.label)}` : ""}">`
                : '<span class="none">NO IMAGE</span>'}
      ${shot.label ? `<div class="wayname">${esc(shot.label)}</div>` : ""}
      <div class="cat">${esc(CAT_LABELS[b.category] || b.category)}</div>
      <div class="flags">${sale}${oos}</div>
    </div>`;
}

/* The star, for a card. Filled or hollow rather than a pressed-in colour block
 * like the comparison tick: a favourite is a thing you keep, and the two
 * controls sit next to each other in the card foot, so they have to be told
 * apart at a glance rather than by reading them. */
const favLabel = on => on ? "Remove from favourites" : "Save to favourites";

const favBtn = (id, cls = "fav") => {
  const on = FAV.has(id);
  return `<button class="${cls}" data-fav aria-pressed="${on}"
    title="${favLabel(on)}" aria-label="${favLabel(on)}">${on ? "★" : "☆"}</button>`;
};

/** The same button, repainted in place — used for the drawer's copy, which
 *  outlives the grid under it, and by flipFavourite() for every other. */
function paintFav(btn, on) {
  btn.setAttribute("aria-pressed", on);
  btn.setAttribute("aria-label", favLabel(on));
  btn.title = favLabel(on);
  btn.textContent = on ? "★" : "☆";
}

function card(b) {
  const on = compare.has(b.id);
  const swatches = (b.colors || []).slice(0, 5)
    .map(c => `<i class="dot" title="${esc(c)}" style="background:${cssColor(c)}"></i>`).join("");
  const extra = (b.colors || []).length > 5 ? `<em>+${b.colors.length - 5}</em>` : "";
  const cell = (label, val) =>
    `<div class="spec"><em>${label}</em>${val ? `<b>${val}</b>` : nil}</div>`;

  return `<article class="card${on ? " sel" : ""}" data-id="${esc(b.id)}">
    ${shotBlock(b)}
    <div class="cbody" data-detail>
      <div class="cbrand">${esc(b.brand)}</div>
      <a class="cname" href="${esc(bagHref(b))}">${esc(b.name)}</a>
    </div>
    <div class="specs">
      ${cell("Vol", fmtVol(b))}${cell("Weight", fmtWeight(b))}
      ${cell("H×W×D", fmtDims(b))}${cell("Laptop", fmtLap(b))}
    </div>
    <div class="cfoot">
      <div class="price">${fmtPrice(b.price_min, b.currency)}${
        b.price_max && b.price_max !== b.price_min ? `<s>–${fmtPrice(b.price_max, b.currency)}</s>` : ""}</div>${
        approxUsd(b) ? `<div class="approx" title="${esc(fxNote(b.currency))}">${approxUsd(b)}</div>` : ""}
      <div class="dots">${swatches}${extra}</div>
      ${favBtn(b.id)}
      <button class="cmp" data-cmp aria-pressed="${on}" title="Add to comparison">${on ? "✓" : "+"}</button>
    </div>
  </article>`;
}

/* What boot paints while the first answer is in flight. Until now the reader
 * spent that round trip looking at the crawler's fallback — a text list that
 * shares nothing but data with the grid about to replace it, so the page
 * appeared to change its mind on arrival. These are cards with nothing in them
 * yet: same shell, same shot plate, same proportions, so the first paint is
 * the shape of the answer and the answer only fills it in. Twelve covers the
 * first viewport on anything reasonable; below the fold there is no one to
 * lie to. */
const SKEL = 12;

function skeletonCard() {
  return `<article class="card skel" aria-hidden="true">
    <div class="shot"></div>
    <div class="cbody">
      <div class="cbrand"><i class="bone"></i></div>
      <div class="cname"><i class="bone"></i></div>
    </div>
    <div class="specs">${'<div class="spec"><i class="bone"></i></div>'.repeat(4)}</div>
    <div class="cfoot"><i class="bone"></i></div>
  </article>`;
}

function skeletonPage() {
  $("#results").innerHTML = `<div class="grid">${skeletonCard().repeat(SKEL)}</div>`;
}

/* The result set is the whole catalog when nothing is filtered, and drawing
 * that as one HTML string was the single thing making this page hang: 8,000
 * cards is on the order of 120,000 nodes, parsed, laid out and painted from
 * scratch on every keystroke and every facet click.
 *
 * So the server sends a page and this paints it, and an IntersectionObserver
 * watching a sentinel below the last card asks for the next one as it comes
 * into reach. The cost of a filter change is a function of the viewport rather
 * than of the size of the catalog — which is now true of the response as well
 * as of the paint, and is the reason a round trip per filter is affordable at
 * all. */
let sentinelIO = null;

/* The two things that can sit under the last page painted, written down once
 * because paintPage builds one of them into a string and loadMore swaps them
 * over as nodes. Only ever one of the two is in the document. */
const SENTINEL = '<div id="sentinel" aria-hidden="true"></div>';
const LOADMORE = '<button class="btn ghost loadmore" id="loadmore" type="button"' +
  ' data-more>Load more</button>';

/* Direct children of #results, because the favourites strip contains a grid of
 * exactly the same shape, and a descendant selector would append every page of
 * results into it. */
const paintHost = () => $("#results > .grid");

const favHost = () => $("#favs .grid");

/* The saved bags that match, drawn above the answer they are part of.
 *
 * The server lifts them out of the pages and sends them whole — see the pass
 * in api/browse.ts — so this is the same card, in the same catalog sort order,
 * simply drawn first. Filtered rather than pinned regardless: a favourite that
 * does not match the filter on screen is not an exception to the filter, it is
 * a bag you are not currently asking about. What the caption is for is saying
 * so, when the two numbers disagree. */
function favBlock(list) {
  if (!list?.length) return "";
  return `<section class="favs" id="favs" aria-label="Favourites">
    <div class="favhead"><h2>★ Favourites</h2>${favCaption(list.length)}</div>
    <div class="grid">${list.map(card).join("")}</div></section>`;
}

const favCaptionText = shown => shown === FAV.size
  ? `${shown} saved` : `${shown} of ${FAV.size} saved match these filters`;
const favCaption = shown => `<em class="favnote">${favCaptionText(shown)}</em>`;

/* Four different nothings, and they want different sentences. "No matches"
 * under a strip of favourites is a lie, and it is the wrong advice for someone
 * who has simply not starred anything yet. */
function emptyState(pinned) {
  if (S.favOnly) {
    // The strip is the entire answer: every favourite that matches is already
    // drawn above, so there is nothing left to apologise for.
    if (pinned) return "";
    return FAV.size
      ? `<div class="empty"><b>No favourites match</b>
         None of your ${FAV.size} saved bags get through the other filters.</div>`
      : `<div class="empty"><b>Nothing saved yet</b>
         Star a bag — on a card here, or on its own page — and it is kept in
         this browser and pinned to the top of every search.</div>`;
  }
  if (pinned) {
    return `<div class="empty"><b>Nothing else matches</b>
      Your favourites above are the only bags this filter leaves.</div>`;
  }
  return `<div class="empty"><b>No matches</b>
    Every active range filter excludes bags whose value is unknown.
    Widen a range or clear filters.</div>`;
}

/* One page of bags into the DOM. `fresh` says whether this is the first page of
 * a new answer — which rebuilds the shell and throws away the scroll position,
 * deliberately, because the results below it are about a different question —
 * or the next page of the one already drawn, which must not. */
function paintPage(list, fresh, favs) {
  const out = $("#results");
  if (fresh) {
    sentinelIO?.disconnect();
    const pinned = favBlock(favs);
    if (!list.length) {
      out.innerHTML = pinned + emptyState(Boolean(pinned));
      return;
    }
    out.innerHTML = pinned + '<div class="grid"></div>' + SENTINEL;
  }
  const host = paintHost();
  if (!host) return;
  // Whole cards, photography and all: a page arrives complete, so there is no
  // placeholder state to draw and nothing to patch in behind the reader.
  host.insertAdjacentHTML("beforeend", list.map(card).join(""));
  // A photo already in cache can finish before the listener in wire() ever sees
  // an event for it, and a card whose image never fades in is a card with a
  // blank plate. Anything already complete is marked here instead.
  for (const img of host.querySelectorAll(".shot img")) {
    if (img.complete) img.classList.add("in");
  }
  if (fresh) {
    observeSentinel();
    fill();
  } else {
    retireSentinel();
  }
}

/** What sits under the last page painted, in its three states: the sentinel,
 *  while there is another page and this burst still has budget to spend on it;
 *  the button, when there is another page and the budget is gone; and nothing
 *  at all once the answer is complete, which is the state that lets the scroll
 *  end and the footer arrive. */
function retireSentinel() {
  const more = (PAGE + 1) * PER < PAGED;
  if (more && burst < BURST) return;
  sentinelIO?.disconnect();
  const el = $("#sentinel");
  if (!el) return;
  if (more) el.insertAdjacentHTML("afterend", LOADMORE);
  el.remove();
}

/** The next page, appended. Resolves false when there was nothing to add —
 *  because the answer is complete, because one is already on the way, or
 *  because the state moved on while this was in flight. */
async function nextPage() {
  if (loading || (PAGE + 1) * PER >= PAGED) return false;
  loading = true;
  const mine = gen;
  try {
    const data = await ask(PAGE + 1);
    // A filter changed while this was in flight, so this page belongs to a
    // grid that is no longer on screen. render() has already asked for the
    // right one; appending this would splice the old answer onto the new.
    if (mine !== gen) return false;
    PAGE++;
    paintPage(data.bags, false);
    return true;
  } catch (err) {
    if (err.name !== "AbortError") console.error("carrynerd: could not load more", err);
    return false;
  } finally {
    loading = false;
  }
}

/* Keep asking while the sentinel is still within reach of the viewport, and
 * while this burst has pages left in it.
 *
 * The observer only fires on a *change* of intersection, so a page that fails
 * to push the sentinel off screen — a tall window, a short last page, a filter
 * that leaves eighty results — would otherwise stall with the observer
 * satisfied and the page half-drawn. Bounded by the viewport rather than by the
 * result count: this asks for one more page than it needs, never for all of
 * them.
 *
 * The burst is the second bound and it is the one that ends the scroll. It is
 * counted here rather than in nextPage() because this is the only caller that
 * is the scroll acting on its own; a press of the button is the reader asking,
 * and what a press does is set the count back to nothing. */
async function fill() {
  const reach = () => {
    const el = $("#sentinel");
    return Boolean(el) && el.getBoundingClientRect().top < innerHeight + 800;
  };
  while (burst < BURST && reach() && await nextPage()) burst++;
  retireSentinel();
}

/** Another burst, starting with the page this press is asking for. The sentinel
 *  goes back exactly where it stood and the observer is handed the scroll again;
 *  fill() takes the first page immediately, because the button the reader just
 *  pressed was by definition within reach of the viewport. */
async function loadMore() {
  const btn = $("#loadmore");
  if (!btn) return;
  burst = 0;
  btn.insertAdjacentHTML("beforebegin", SENTINEL);
  btn.remove();
  observeSentinel();
  await fill();
  // If the budget ran out again inside that fill — a short answer, a tall
  // window — the button is back where it stood, and somebody who pressed it
  // from the keyboard has just had the element under their focus deleted out
  // from under them. Handing it back is the difference between carrying on and
  // tabbing from the wordmark again.
  $("#loadmore")?.focus();
}

function observeSentinel() {
  sentinelIO?.disconnect();
  const el = $("#sentinel");
  if (!el) return;
  // The margin is what keeps the next page ahead of the scroll rather than
  // arriving under it — nobody should watch a gap fill in, and now the gap
  // takes a round trip to fill.
  sentinelIO = new IntersectionObserver(
    entries => { if (entries.some(e => e.isIntersecting)) fill(); },
    { rootMargin: "800px 0px" },
  );
  sentinelIO.observe(el);
}

/* The line above the grid. Every number in it is counted over the whole match
 * set rather than over the page — "60 of 8,079" would be a report on the
 * scroll, not on the filter — which is why the server sends them alongside the
 * page instead of leaving them to be added up here.
 *
 * The caveat is the important half: a bag whose feature list was only ever
 * built from marketing copy cannot be said to lack a feature. Excluding it is
 * still the right call — a filter that returns maybes is useless — but it has
 * to be admitted to rather than done silently. */
function countLine({ total, counts, stats }) {
  const unknown = [];
  if (counts.noFeature) unknown.push(`${counts.noFeature} with no feature detection yet`);
  if (counts.noColour) unknown.push(`${counts.noColour} whose colourway names state no colour`);
  // The airline filters, and the carry-on feature that is measured rather than
  // read off a page. A bag stating fewer than three axes cannot be held up to a
  // gauge at all, and saying so is the difference between a shorter list and a
  // wrong one.
  if (counts.noDims) unknown.push(`${counts.noDims} that do not state all three dimensions`);
  const caveat = unknown.length
    ? ` · <em class="warnnote">excluded: ${unknown.join(", ")} — unknown, not` +
      ` known to be absent</em>`
    : "";

  // "· 1 brands" is not a finding about a brand page, and the heading above it
  // has already said whose shelf this is.
  const brands = ONE_BRAND ? "" : ` · ${num(counts.brands)} brands`;
  $("#count").innerHTML = `<b>${num(total)}</b> of ${num((stats || STATS).bags)} bags` +
    `${brands} · ${num(counts.skus)} SKUs` + caveat;
}

/* Ask the current state what it matches, and draw the first page of the answer.
 *
 * Everything that changes a filter ends up here, so this is where a request
 * gets its generation number and where the previous state's requests get
 * cancelled. The old results stay on screen until the new ones land — blanking
 * the grid for the length of a round trip would make every facet click flash —
 * and #results carries aria-busy meanwhile, which app.css only acts on if the
 * wait is long enough to be worth mentioning. */
async function render({ boot = false } = {}) {
  const mine = ++gen;
  ctrl?.abort();
  ctrl = new AbortController();
  loading = false;
  // Re-read rather than trusted: another tab may have starred something since
  // the last answer, and this is the one place every path through the island
  // passes on its way to asking for a new one.
  FAV = new Set(favourites());
  syncURL();
  renderSaved();

  const out = $("#results");
  out.setAttribute("aria-busy", "true");

  // At boot what is on screen is the crawler's list, and the reader's first
  // paint should be the shape of the grid instead — see skeletonPage(). The
  // list is moved aside rather than discarded — as nodes, because a string
  // round-trip of every model would serialise and reparse the longest list
  // this page ever holds — since it is also the page's only working fallback
  // and the failure path below hands it back.
  let fallback = null;
  if (boot) {
    fallback = document.createDocumentFragment();
    fallback.append(...out.childNodes);
    skeletonPage();
  }

  let data;
  try {
    data = await ask(0, boot);
  } catch (err) {
    // An abort is this function being right, not failing: a newer render is
    // already on its way and owns the busy state and the message line.
    if (err.name === "AbortError") return null;
    console.error("carrynerd: could not reach /api/browse", err);
    out.removeAttribute("aria-busy");
    // On the first request the skeletons give way to the server-rendered list
    // of every model — degraded, but every bag is still reachable. Later on,
    // the results on screen are simply one filter out of date, which is worth
    // saying rather than pretending.
    if (boot) out.replaceChildren(fallback);
    $("#count").textContent = boot
      ? "filters unavailable — showing all models"
      : "could not load that filter — showing the previous results";
    return null;
  }
  if (mine !== gen) return null;

  out.removeAttribute("aria-busy");
  PAGE = 0;
  // A different question gets a fresh budget. Every filter, search and sort
  // change comes through here, which is what makes this the one place the burst
  // has to be reset — a reader who has just narrowed the list is starting the
  // scroll again, not continuing the one they spent on the answer before it.
  burst = 0;
  // Absent for a reader with nothing saved, in which case every match is
  // paged and the two numbers are the same.
  PAGED = data.paged ?? data.total;
  countLine(data);
  // On the boot answer this finds an empty rail and does nothing — the options
  // it would write into are built from this same response a moment later, by
  // init(), which paints the counts itself once they exist.
  paintFacetCounts(data.facets);
  paintPage(data.bags, true, data.favs);
  renderTray();
  return data;
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

/* ---------- favourites ---------- */

/* A star pressed now contradicts an answer drawn a moment ago, and the cheap
 * way to settle that is to re-render — which would throw away the scroll
 * position of somebody who has just scrolled two hundred cards to reach the
 * bag they starred. So the card is moved rather than redrawn: the same node,
 * out of the results and into the strip at the top, which is where the next
 * answer would have put it anyway.
 *
 * Going the other way it goes back to the front of the results rather than to
 * the place the sort would give it, which this file cannot work out without
 * asking. Nothing vanishes and nothing is drawn twice; the next filter change
 * files it properly. */
function flipFavourite(id) {
  const { on, full } = toggleFavourite(id);
  if (full) {
    return say(`That is ${MAX_FAVOURITES} favourites — the most this keeps.` +
               ' Un-star one to make room.');
  }
  FAV = new Set(favourites());

  // Every copy of the button. A bag is drawn once in the results, but the
  // drawer keeps its own star for whatever it last opened, and that one is
  // still on screen while this is clicked from underneath it.
  $$("[data-fav]").forEach(btn => {
    if (btn.closest("[data-id]")?.dataset.id === id) paintFav(btn, on);
  });

  // Scoped to the results: the drawer's star carries the same id, and moving
  // the drawer into the favourites strip is not what anybody meant.
  const node = $(`#results [data-id="${CSS.escape(id)}"]`);
  const host = on ? openFavStrip() : paintHost();
  // No host going back means the results are showing an empty state — under
  // "favourites only", un-starring genuinely does remove the bag from the
  // answer, so letting the node go is the honest outcome rather than a bug.
  if (node) host ? host[on ? "append" : "prepend"](node) : node.remove();

  trimFavStrip();
  paintFavCount();
}

/** The strip, made if it is not already there. Drawn by hand rather than by
 *  re-rendering because the answer it belongs to is still on screen. */
function openFavStrip() {
  let sec = $("#favs");
  if (!sec) {
    sec = document.createElement("section");
    sec.id = "favs";
    sec.className = "favs";
    sec.setAttribute("aria-label", "Favourites");
    sec.innerHTML =
      '<div class="favhead"><h2>★ Favourites</h2><em class="favnote"></em></div>'
      + '<div class="grid"></div>';
    $("#results").prepend(sec);
  }
  return favHost();
}

/** Keep the caption honest, and take the strip away once it is empty rather
 *  than leaving a heading over nothing. */
function trimFavStrip() {
  const sec = $("#favs");
  if (!sec) return;
  const shown = favHost()?.children.length ?? 0;
  if (!shown) return sec.remove();
  const note = $(".favnote", sec);
  if (note) note.textContent = favCaptionText(shown);
}

const paintFavCount = () => {
  const el = $("#favcount");
  if (el) el.textContent = FAV.size;
};

/* ---------- saved filters ---------- */

/* A saved filter is a name and the query string this page already speaks — the
 * one in the address bar, which is also the one /api/browse is asked. Keeping
 * the URL's own spelling rather than a structured copy of S means applying a
 * saved filter is the same operation as opening a link somebody sent, and that
 * the two can never come to mean different things. lib/store.js does the
 * keeping; everything here is about naming and applying. */

/** A suggested name, from what is actually switched on. Nobody wants to name
 *  a filter, and "Peak Design · under 30 L · in stock" is what they would have
 *  typed. Truncated hard: this is a chip in a 264px rail. */
function describeState() {
  const names = (set, label) => [...set].map(v => label(v)).join(", ");
  const span = (lo, hi, unit) =>
    lo != null && hi != null ? `${lo}–${hi}${unit}`
      : lo != null ? `over ${lo}${unit}`
      : hi != null ? `under ${hi}${unit}` : null;

  const bits = [
    S.q && `“${S.q}”`,
    S.brands.size && names(S.brands, s => BRAND_NAMES.get(s) || s),
    S.cats.size && names(S.cats, c => CAT_LABELS[c] || c),
    S.colors.size && names(S.colors, c => COLOUR_LABELS[c] || c),
    S.mats.size && [...S.mats].join(", "),
    S.feats.size && names(S.feats, f => FEATURE_LABELS[f] || f),
    S.airlines.size && `fits ${names(S.airlines, a => AIRLINE_NAMES.get(a) || a)}`,
    // No currency symbol on the price: the catalog quotes a few brands in
    // pounds and euros and never converts, so a "$" here would be a claim the
    // filter itself does not make.
    span(S.priceMin, S.priceMax, "") && `price ${span(S.priceMin, S.priceMax, "")}`,
    span(S.volMin, S.volMax, " L"),
    span(S.weightMin, S.weightMax, " g"),
    S.linearMax != null && `≤${Math.round(S.linearMax)}cm linear`,
    S.laptopMin != null && `${S.laptopMin}″ laptop`,
    S.presets.has("carryon") && "carry-on",
    S.presets.has("underseat") && "under seat",
    S.stock && "in stock",
    S.sale && "on sale",
    S.favOnly && "favourites",
  ].filter(Boolean);

  if (!bits.length) return "";
  const name = bits.slice(0, 3).join(" · ") + (bits.length > 3 ? " +more" : "");
  return name.length > 48 ? name.slice(0, 47) + "…" : name;
}

/* Redrawn on every render, which is also every time the filter changes — that
 * is what keeps the chip for the filter you are currently looking at lit, and
 * the save button disabled while there is nothing to save. */
function renderSaved() {
  const list = savedFilters();
  const current = stateParams().toString();

  const box = $("#savedlist");
  if (box) {
    box.innerHTML = list.length ? list.map(f => `<span class="savedchip">
        <button data-apply="${esc(f.id)}" aria-pressed="${f.query === current}"
                title="${esc(f.name)}">${esc(f.name)}</button>
        <button class="forget" data-forget="${esc(f.id)}"
                aria-label="Forget ${esc(f.name)}" title="Forget">✕</button>
      </span>`).join("")
      : '<p class="savednone">Nothing saved yet. Set some filters, then Save.</p>';
  }

  const save = $("#savefilter");
  if (save) {
    const chosen = chosenParams().toString();
    save.disabled = !chosen;
    save.title = chosen ? "Save these filters" : "Set a filter first";
  }
  paintFavCount();
}

/** A line in the rail for anything that went wrong quietly — a storage
 *  refusal, a cap. Cleared by the next thing that succeeds. */
function say(msg) {
  const el = $("#savedmsg");
  if (!el) return;
  el.textContent = msg || "";
  el.hidden = !msg;
}

/** Everything in S back to its default. Shared by Clear and by applying a
 *  saved filter, which are the same operation with a different query string. */
function resetState() {
  Object.assign(S, {
    q: "", volMin: null, volMax: null, priceMin: null, priceMax: null,
    weightMin: null, weightMax: null, linearMax: null, laptopMin: null,
    stock: false, sale: false, favOnly: false, sort: DEFAULT_SORT,
  });
  [S.cats, S.brands, S.feats, S.mats, S.colors, S.airlines, S.presets]
    .forEach(s => s.clear());
}

/** Become this query string: the address bar, the controls and the results. */
function applyQuery(query) {
  resetState();
  // The facet boxes hide options rather than filter bags, so they survive a
  // render untouched and have to be told explicitly — otherwise the rail keeps
  // a half-hidden brand list after everything else has changed.
  clearFacetSearches();
  history.replaceState(null, "", query ? "?" + query : location.pathname);
  loadURL();
  say("");
  render();
}

/* Saving is two steps rather than a prompt(): a suggested name to accept or
 * type over, in a field in the rail. prompt() is blocked outright in some
 * browsers and in every embedded webview, and a filter you cannot name is a
 * filter you cannot save. */
function mountSaveForm() {
  const form = $("#savedform");
  const name = $("#savedname");

  const open = on => {
    form.hidden = !on;
    if (!on) return;
    name.value = describeState();
    name.focus();
    name.select();
  };

  $("#savefilter").addEventListener("click", () => {
    if (!chosenParams().toString()) {
      return say("Nothing to save — no filters are set.");
    }
    say("");
    open(form.hidden);
  });

  $("#savedcancel").addEventListener("click", () => open(false));

  form.addEventListener("submit", e => {
    e.preventDefault();
    // The whole state, including a sort the page defaulted to: what is saved
    // has to reproduce what is on screen, wherever it is applied from.
    const { ok, reason } = saveFilter(name.value, stateParams().toString());
    if (!ok) {
      return say(reason === "full"
        ? `That is ${MAX_FILTERS} saved filters — the most this keeps.` +
          " Forget one to make room."
        : reason === "empty"
        ? "Nothing to save — no filters are set."
        : "This browser would not store it — private mode, or no room left.");
    }
    open(false);
    renderSaved();
  });

  // Before the page-wide handler sees it, which would shut the filter panel
  // out from under the field being cancelled.
  form.addEventListener("keydown", e => {
    if (e.key !== "Escape") return;
    e.stopPropagation();
    open(false);
  });
}

/* ---------- facets ---------- */

/* The rail's options, drawn once. Which options exist and the order they come
 * in is fixed for the life of a deploy — see shapeOf() in api/browse.ts — so
 * this runs on the boot answer and never again; only the numbers beside them
 * move, and paintFacetCounts() below writes those into the markup this leaves
 * behind.
 *
 * That split is the whole reason the rail can be recounted at all without
 * coming apart. Rebuilding these lists per answer would reset the scroll inside
 * every group deep enough to scroll, throw away whatever a group's own search
 * box was hiding, and hand mountFacetSearch()'s closure a set of nodes that are
 * no longer in the document. */
function buildFacets({ facets, coverage, stats }) {
  $("#f-cat").innerHTML = facets.categories
    .map(([v, n]) => `<button class="chip" data-f="cat" data-v="${esc(v)}" aria-pressed="false">${
      esc(CAT_LABELS[v] || v)}<span class="n">${n}</span></button>`).join("");

  $("#f-feat").innerHTML = facets.features
    .map(([v, n]) => `<button class="check" data-f="feat" data-v="${esc(v)}" aria-pressed="false"><i></i>${
      esc(FEATURE_LABELS[v] || v)}<b class="n">${n}</b></button>`).join("");

  // Colours keep the printed order rather than the counted one: a swatch list
  // reads as a spectrum, and sorting it by popularity puts black next to beige.
  const colorCounts = new Map(facets.colors);
  $("#f-color").innerHTML = COLOUR_ORDER.filter(f => colorCounts.has(f))
    .map(f => `<button class="check" data-f="color" data-v="${esc(f)}" aria-pressed="false"><i></i><span class="sw" style="background:${
      COLOUR_SWATCH[f]}"></span>${esc(COLOUR_LABELS[f])}<b class="n">${colorCounts.get(f)}</b></button>`).join("");

  $("#f-mat").innerHTML = facets.materials
    .map(([v, n]) => `<button class="check" data-f="mat" data-v="${esc(v)}" aria-pressed="false"><i></i>${
      esc(v)}<b class="n">${n}</b></button>`).join("");

  /* One carrier per row, with the number of bags matching the rest of the
     filter that clear its published carry-on limit. Every-of, like features:
     two ticked means a bag that fits both, because an itinerary is an and — and
     because it is an and, this group is counted over the results themselves
     rather than with its own ticks lifted, so the number beside a carrier is
     how many of the bags on screen would survive adding it.
     The count is over bags stating all three of their dimensions — the rest
     cannot be measured against a gauge and are neither passed nor failed, which
     is what the caveat above the grid owns up to when this filter is on.
     Minus the carrier an airline page already is, where this is one: see
     SCOPE_AIRLINE. */
  const airlineBox = $("#f-airline");
  if (airlineBox) airlineBox.innerHTML = (facets.airlines || [])
    .filter(([slug]) => slug !== SCOPE_AIRLINE)
    .map(([slug, name, n]) => `<button class="check" data-f="airline" data-v="${esc(slug)}" aria-pressed="false"><i></i>${
      esc(name)}<b class="n">${n}</b></button>`).join("");

  // For describeState(), which would otherwise name a saved filter after a
  // slug — the same reason the brand names are kept below.
  AIRLINE_NAMES = new Map((facets.airlines || []).map(([slug, name]) => [slug, name]));

  /* One row per whole inch the catalogue reaches, and the label built here
     rather than sent: the server ships the rung as the string that goes in
     data-v and in the URL, and "Fits 13″ and up" is that string with words
     around it — the same division of labour CAT_LABELS and FEATURE_LABELS
     already do, minus the table, because this one is a formula.

     A minimum, so the counts fall as the rows go down and a bag taking 16″ is
     inside all four of 13, 14, 15 and 16. And one-of, so pressing a second row
     moves the choice rather than adding to it; see the click handler in wire(),
     which is where the difference between this group and the rest lives. */
  $("#f-laptop").innerHTML = (facets.laptop || [])
    .map(([v, n]) => `<button class="check" data-f="laptop" data-v="${esc(v)}" aria-pressed="false"><i></i>Fits ${
      esc(v)}″ and up<b class="n">${n}</b></button>`).join("");

  // Kept for describeState(), which names a saved filter after what is in it
  // and would otherwise call it "peak-design".
  BRAND_NAMES = new Map(facets.brands.map(([slug, name]) => [slug, name]));

  // Filed by name, not by slug and not by count: the rail prints names, and a
  // reader looking for one scans the alphabet. Absent on a scoped page, where
  // the brand is the page rather than one of the choices in it.
  const brandBox = $("#f-brand");
  if (brandBox) brandBox.innerHTML = facets.brands
    .map(([slug, name, n]) => `<button class="check" data-f="brand" data-v="${esc(slug)}" aria-pressed="false"><i></i>${
      esc(name)}<b class="n">${n}</b></button>`).join("");

  ["#f-brand", "#f-color", "#f-feat", "#f-mat", "#f-airline"].forEach(mountFacetSearch);

  /* A group that came out empty is hidden rather than left to open onto
   * nothing. It never happens on the whole catalogue — every facet has
   * thousands behind it — but a scoped rail is counting one brand's shelf, and
   * a maker whose colourways name no colour we can read would otherwise get a
   * Colour heading with a blank panel under it. Laptop joined the list when it
   * stopped being a dropdown: a brand that states no laptop size anywhere sends
   * no rungs, and five rows of nought would be worse than no group. Only the
   * data-driven groups: Availability and the ranges are always worth offering. */
  for (const sel of ["#f-cat", "#f-color", "#f-feat", "#f-mat", "#f-brand",
                     "#f-airline", "#f-laptop"]) {
    const group = $(sel)?.closest(".fgroup");
    if (group) group.hidden = !$(sel).children.length;
  }

  const labels = { volume_l: "Volume", dims_cm: "Dims", weight_g: "Weight",
                   laptop_in: "Laptop", price_min: "Price" };
  $("#coverage").innerHTML = Object.entries(coverage || {}).map(([k, v]) =>
    `<div class="covrow"><em>${labels[k] || k}</em>
      <div class="covbar"><i style="width:${v.pct}%"></i></div>
      <b>${v.pct}%</b></div>`).join("");

  // Scoped, these are the scope's own three numbers — and on a brand page the
  // middle one is then always 1, so it goes.
  $("#stats").innerHTML =
    `<span><b>${num(stats.bags)}</b> BAGS</span>` +
    (ONE_BRAND ? "" : `<span><b>${num(stats.brands)}</b> BRANDS</span>`) +
    `<span><b>${num(stats.skus)}</b> SKUS</span>`;
}

/* The numbers beside the options, from this answer rather than from boot.
 *
 * They used to be catalogue-wide and permanent, which put a Colour group
 * offering nine hundred black bags next to a grid of a hundred and seventy —
 * the count was answering how many exist while the reader was reading it as how
 * many are left. The server now counts each group against the rest of the
 * filter; see the facets section of api/browse.ts for what "the rest" means and
 * why a group is not counted against its own ticks.
 *
 * Written into the markup in place rather than rendered again. Every option
 * already carries the two attributes that name it, so the numbers can be
 * changed under a rail that is mid-scroll, half-searched and holding focus,
 * without any of that moving — which is the difference between a rail that
 * keeps up with the grid and one that fights the reader for it. */
function paintFacetCounts(facets) {
  if (!facets) return;
  // Keyed by data-f, the same names facetSets() files them under.
  const num = {
    cat: new Map(facets.categories),
    feat: new Map(facets.features),
    mat: new Map(facets.materials),
    color: new Map(facets.colors),
    // Slug, name, count — the name is already in the markup and only the last
    // column is being replaced.
    airline: new Map((facets.airlines || []).map(([slug, , n]) => [slug, n])),
    brand: new Map((facets.brands || []).map(([slug, , n]) => [slug, n])),
    // Rung and count, keyed by the string the server sent — data-v holds "16",
    // not 16, and a Map does not consider those the same key.
    laptop: new Map(facets.laptop || []),
  };

  for (const el of $$("[data-f]")) {
    const n = num[el.dataset.f]?.get(el.dataset.v);
    if (n === undefined) continue;
    const box = $(".n", el);
    if (box) box.textContent = n;
    /* Nothing left to add, so the option is dimmed and stops answering rather
     * than being taken out of the list: an option that vanishes is one the
     * reader goes looking for, and a list that loses rows changes height while
     * it is being read.
     *
     * Except when it is ticked. A ticked option is the one number here that can
     * honestly read zero while the control is still doing something — it is
     * what emptied the grid — and it has to stay pressable, or there is no way
     * back off it. */
    el.disabled = n === 0 && el.getAttribute("aria-pressed") !== "true";
  }
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

  /* Searching this particular list is the one moment on the site where a
   * reader is actively looking for a brand by name, which makes it the one
   * moment where "we don't have it" is a finding rather than an absence they
   * had to go looking for. So the Brand list — and only the Brand list — grows
   * a way to say so, right where they are looking.
   *
   * It appears with the filtered list rather than only when nothing matched:
   * a search for "tom" that turns up one unrelated maker is the same
   * disappointment as a search that turns up none, and a button that arrives
   * only on the empty state is a button nobody knows exists.
   *
   * The dialog itself is in the layout — see components/RequestBrand.astro.
   * This is only an opener, so it needs no markup of its own beyond the
   * attribute the delegated listener watches for. */
  const ask = sel === "#f-brand" ? document.createElement("button") : null;
  if (ask) {
    ask.type = "button";
    ask.className = "freq";
    ask.hidden = true;
    ask.setAttribute("data-request-brand", "");
    none.after(ask);
  }

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
    if (ask) {
      ask.hidden = !q;
      // What they typed, carried into the dialog's Brand field. Their spelling
      // rather than a matched slug: there is nothing here to match against,
      // and their spelling is the whole content of the request.
      ask.dataset.brand = box.value.trim();
      ask.textContent = shown
        ? "Request a brand"
        : `Request “${box.value.trim()}”`;
    }
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
  // Everything in the tray was ticked on a card or a row, and both are drawn
  // from a whole record — so the photographs for the column heads are already
  // here and the sheet draws in one go.
  const bags = [...compare].map(id => BY_ID.get(id)).filter(Boolean);
  if (!bags.length) return;

  const rows = [
    ["Brand",      b => esc(b.brand), null],
    ["Category",   b => esc(CAT_LABELS[b.category] || b.category), null],
    ["Price",      b => fmtPrice(b.price_min, b.currency)
                        + (approxUsd(b) ? ` <em class="approx">${approxUsd(b)}</em>` : ""),
                   b => cmpPrice(b), "min"],
    ["Volume",     b => b.volume_l ? b.volume_l + " L" : null, b => b.volume_l, "max"],
    ["Weight",     b => dualWeight(b.weight_g), b => b.weight_g, "min"],
    ["Dimensions", b => dualDims(b.dims_cm, { sep: " × ", suffix: true }), null],
    ["Linear",     b => dualLen(b.linear_cm, true), b => b.linear_cm, "min"],
    ["Grams / L",  b => gpl(b) ? gpl(b).toFixed(0) : null, b => gpl(b), "min"],
    // Always in dollars, because it is only ever read against the row above
    // and below it. The Price column keeps the seller's own currency.
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

/* The drawer opens from a card or a row, so the bag is in BY_ID and no request
 * is made. The fallback is for the case that should not happen — a click
 * arriving for an id this session was never sent — and /api/catalog is the only
 * question this file can ask about one bag. It answers with the photography,
 * the link and the variants and nothing else, so a drawer built from it alone
 * is mostly dashes. That is still better than a click that does nothing. */
async function fetchOne(id) {
  try {
    const res = await fetch(`/api/catalog?ids=${encodeURIComponent(id)}`);
    if (!res.ok) throw new Error(res.status);
    const { bags = [] } = await res.json();
    if (!bags[0]) return null;
    const b = { id, ...bags[0] };
    BY_ID.set(id, b);
    return b;
  } catch (err) {
    console.error("carrynerd: could not load", id, err);
    return null;
  }
}

async function openDetail(id) {
  const b = BY_ID.get(id) ?? await fetchOne(id);
  if (!b) return;

  // Every way out of the drawer to the seller carries the same attributes,
  // because analytics.js reads them off whichever one was clicked. Three of
  // them share it now — the title, the row under the photo and the bar pinned
  // at the bottom — so the drawer's job, sending the reader on, is legible
  // before any of the specs are.
  const outAttrs = `href="${esc(b.url)}" target="_blank" rel="noopener nofollow"
    data-buy="browse_overlay"
    data-buy-id="${esc(b.id)}"
    data-buy-brand="${esc(b.brand)}"
    data-buy-model="${esc(b.name)}"
    data-buy-price="${esc(b.price_min ?? "")}"`;

  const title = `${esc(b.brand)} — ${esc(b.name)}`;
  $("#dettitle").innerHTML = b.url ? `<a ${outAttrs}>${title} ↗</a>` : title;

  const row = (label, val, prov) => `<div class="drow"><em>${label}</em>
    <span>${val ?? "—"}${prov ? `<i class="prov">${esc(prov)}</i>` : ""}</span></div>`;

  const variants = (b.variants || []).map(v => `<tr>
      <td class="waycell">${v.image
          ? `<img class="waythumb" loading="lazy" decoding="async" src="${
              esc(thumb(v.image, THUMB.chip))}" alt=""${
              plate(v.image_bg)}>` : ""}${
        esc(v.color || v.title || "—")}</td>
      <td>${esc(v.sku || "—")}</td>
      <td>${v.price ? fmtPrice(v.price, b.currency) : "—"}${
        v.compare_at && v.price && v.compare_at > v.price
          ? ` <s style="color:var(--ink-mute)">${fmtPrice(v.compare_at, b.currency)}</s>` : ""}</td>
      <td style="color:${v.available ? "var(--ok)" : "var(--ink-mute)"}">${
        v.available ? "in stock" : "out"}</td>
    </tr>`).join("");

  $("#detbody").innerHTML = `
    ${b.image ? `<div class="shot" style="aspect-ratio:16/10;border-bottom:1px solid var(--line)${
        b.image_bg ? `;--shot-bg:${esc(b.image_bg)}` : ""}">
      <img src="${esc(thumb(b.image, THUMB.hero))}" alt="${esc(b.name)}"></div>` : ""}
    <div class="detgo">
      ${b.url ? `<a class="detgo-out" ${outAttrs}>Open on ${esc(b.brand)} ↗</a>` : ""}
      <a class="detgo-alt" href="${esc(bagHref(b))}">Full specs →</a>
    </div>
    ${row("Brand", `<a href="/brands/${esc(b.brand_slug)}/">${esc(b.brand)} →</a>`)}
    ${row("Category", esc(CAT_LABELS[b.category] || b.category))}
    ${row("Price", (b.price_min === b.price_max ? fmtPrice(b.price_min, b.currency)
        : `${fmtPrice(b.price_min, b.currency)} – ${fmtPrice(b.price_max, b.currency)}`)
        + (approxUsd(b)
            ? ` <em class="approx">${approxUsd(b)}</em>`
              + `<i class="fxnote">${esc(fxNote(b.currency))}</i>`
            : ""))}
    ${row("Volume", b.volume_l ? b.volume_l + " L" : null, b.volume_source)}
    ${row("Dimensions", dualDims(b.dims_cm, { sep: " × ", digits: 1, suffix: true }), b.dims_source)}
    ${row("Linear", dualLen(b.linear_cm, true))}
    ${row("Weight", dualWeight(b.weight_g), b.weight_source)}
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
    ${watchForm(b)}`;

  // Lives in the drawer's own markup, pinned below the scrolling body — see
  // index.astro. Everything about it changes per bag, including where it goes:
  // outbound for anything with a store URL, and the model page for the few
  // without one, which is also the only case where it must not report a
  // buy_click.
  const buy = $("#detbuy");
  if (b.url) {
    buy.href = b.url;
    buy.target = "_blank";
    buy.rel = "noopener nofollow";
    buy.textContent = `Open on ${b.brand} ↗`;
    Object.assign(buy.dataset, {
      buy: "browse_overlay",
      buyId: b.id,
      buyBrand: b.brand,
      buyModel: b.name,
      buyPrice: b.price_min ?? "",
    });
  } else {
    buy.href = bagHref(b);
    buy.removeAttribute("target");
    buy.removeAttribute("rel");
    buy.textContent = "Full specs & price history →";
    for (const k of ["buy", "buyId", "buyBrand", "buyModel", "buyPrice"]) {
      delete buy.dataset[k];
    }
  }
  // The star does the same, in the header. It carries the id itself rather
  // than sitting inside something that does, which is what lets one delegated
  // handler serve it and the cards alike.
  const star = $("#detfav");
  star.dataset.id = b.id;
  paintFav(star, FAV.has(b.id));
  openOverlay($("#detoverlay"));
  // The drawer is the other place a bag gets looked at and bought from, and the
  // only one that never loads a model page. Without this the clicks it produces
  // would have no views to divide by. See scripts/analytics.js.
  trackProductView(b, "browse_overlay");
}


/* Back closes the drawer, which is the one thing it did not do.
 *
 * The bag drawer and the comparison sheet are full-screen on a phone, so they
 * read as pages — and neither was in the history, so the gesture everybody
 * tries on a page took the reader off the site instead of back to the grid
 * they were halfway down. On Android that is the system back button; on iOS it
 * is the edge swipe. Both meant "leave".
 *
 * An entry with the same URL, so the address bar does not move: this is not a
 * link, nobody should be able to paste it, and giving it a real URL means
 * fighting the filter round-trip in syncURL() for a feature nobody asked for.
 * What it buys is one entry to go back to.
 *
 * Closing from the UI spends that entry rather than leaving it, which is what
 * `history.back()` is doing in a close handler. Without it, dismissing the
 * drawer with Escape and then pressing back would appear to do nothing the
 * first time — the reader would be popping the entry the drawer left behind.
 * The flag is what keeps the two paths from fighting: a close that came *from*
 * popstate must not push, and must not pop again. */
let overlayEntry = false;

function openOverlay(el) {
  el.classList.add("on");
  if (!overlayEntry) {
    history.pushState({ ...(history.state || {}), cnOverlay: true }, "");
    overlayEntry = true;
  }
}

function closeOverlays(fromPop = false) {
  const wasOpen = $$(".overlay.on").length > 0;
  $$(".overlay").forEach(o => o.classList.remove("on"));
  if (!wasOpen) return;
  if (fromPop) { overlayEntry = false; return; }
  if (overlayEntry) { overlayEntry = false; history.back(); }
}

addEventListener("popstate", () => {
  if ($$(".overlay.on").length) closeOverlays(true);
});

/* ---------- URL state ---------- */

/* The filter, as a query string. Written once and used twice: for the address
 * bar and for the request. That is not a coincidence to be tidied away later —
 * it is what makes a shared link and the thing the server is asked the same
 * object, so a URL someone pastes cannot mean something the grid does not show.
 *
 * Only non-default keys are written, so an untouched control leaves no trace in
 * either place. `view` is the exception and is added by syncURL alone: it
 * changes how the answer is drawn, not what the answer is, and sending it would
 * split the edge cache in two for no reason. */
function stateParams() {
  const p = new URLSearchParams();
  if (S.q) p.set("q", S.q);
  const sets = { ...facetSets(), preset: S.presets };
  for (const k in sets) if (sets[k].size) p.set(k, [...sets[k]].join(","));
  for (const k of RANGE_KEYS) if (S[k] != null) p.set(k, S[k]);
  if (S.stock) p.set("stock", "1");
  if (S.sale) p.set("sale", "1");
  // Written like any other filter, because it is one. What it means travels
  // with the reader rather than with the link: somebody else opening this URL
  // gets their own favourites, which is the only thing it could honestly do
  // without a copy of the list riding along in the address bar.
  if (S.favOnly) p.set("favonly", "1");
  /* Left out only when the reader's order and the page's are both the order
     /api/browse sorts by when asked for nothing. A page with an opinion of its
     own therefore spells the sort out on every request — /sale/ shows
     ?sort=discount from the moment it opens — because the alternative is a URL
     that means one thing here and another when pasted: omitted, the server
     would answer in brand order, and a reader who chose Brand A–Z on /sale/
     would get the discount order back on reload. */
  if (S.sort !== "brand" || DEFAULT_SORT !== "brand") p.set("sort", S.sort);
  return p;
}

/** The same, minus anything the page arrived already saying. Only the saved-
 *  filter panel wants this: /sale/ carries a sort nobody chose, and "Save
 *  filter" lighting up on an untouched page would offer to remember it. */
function chosenParams() {
  const p = stateParams();
  if (S.sort === DEFAULT_SORT) p.delete("sort");
  return p;
}

function syncURL() {
  const p = stateParams();
  history.replaceState(null, "", p.toString() ? "?" + p : location.pathname);

  /* The way off a scoped page, kept in step with the filters on it. Whatever is
   * set here still means the same thing on the front page, so it travels — with
   * the brand demoted from the page's premise to a ticked box, which is the
   * whole point of following the link. Rebuilt rather than wired once: it is a
   * link to the state, and the state is what keeps changing. */
  const out = $("#unscope");
  if (!out) return;
  const q = new URLSearchParams(UNLOCK);
  for (const [k, v] of p) q.set(k, v);
  out.href = "/?" + q;
}

function loadURL() {
  const p = new URLSearchParams(location.search);
  S.q = p.get("q") || "";
  $("#q").value = S.q;
  const sets = { ...facetSets(), preset: S.presets };
  for (const k in sets) (p.get(k) || "").split(",").filter(Boolean).forEach(v => sets[k].add(v));
  for (const k of RANGE_KEYS) if (p.has(k)) S[k] = Number(p.get(k));
  S.stock = p.get("stock") === "1";
  S.sale = p.get("sale") === "1";
  S.favOnly = p.get("favonly") === "1";
  S.sort = p.get("sort") || DEFAULT_SORT;

  $("#sort").value = S.sort;
  $("#vol-min").value = S.volMin ?? ""; $("#vol-max").value = S.volMax ?? "";
  $("#price-min").value = S.priceMin ?? ""; $("#price-max").value = S.priceMax ?? "";
  $("#weight-min").value = S.weightMin ?? ""; $("#weight-max").value = S.weightMax ?? "";
  // laptopMin has no box to fill any more: it is a row in the rail now, and
  // syncFacetButtons() at the bottom of this function lights the right one.
  syncUnits();
  syncSliders();
  $("#f-stock").setAttribute("aria-pressed", S.stock);
  // Absent on /sale/, where On sale is the page rather than a filter on it —
  // the same way the Brand group is absent on a brand page.
  $("#f-sale")?.setAttribute("aria-pressed", S.sale);
  $("#f-fav").setAttribute("aria-pressed", S.favOnly);
  syncFacetButtons();
}

function syncFacetButtons() {
  const sets = facetSets();
  // Laptop has no set to ask — see the note above facetSets(). Compared as a
  // number, because S.laptopMin is one and data-v is the string beside it.
  $$("[data-f]").forEach(el =>
    el.setAttribute("aria-pressed", el.dataset.f === "laptop"
      ? S.laptopMin === Number(el.dataset.v)
      : sets[el.dataset.f]?.has(el.dataset.v) || false));
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
  { key: "vol",    lo: "volMin",    hi: "volMax",    of: "volume_l",  step: 1 },
  { key: "price",  lo: "priceMin",  hi: "priceMax",  of: "price_min", step: 5 },
  { key: "weight", lo: "weightMin", hi: "weightMax", of: "weight_g",  step: 25 },
];

/* Dragging fires `input` at pointer rate, and asking the server that often is
 * not a filter, it is a flood. So a drag moves the fill and the number boxes
 * and nothing else; the results — and the count above them, and the shareable
 * URL — catch up when the handle is let go, on `change`, which a range input
 * fires on pointer-up and on a keyboard commit.
 *
 * The count used to keep up with the handle mid-drag, off a local match pass
 * that no longer exists. Losing it is the visible cost of moving the filter to
 * the server, and it is the cheap half: what the drag is for is the number
 * under the thumb, which is still live. */

function paintFill(d) {
  const span = (d.bound[1] - d.bound[0]) || 1;
  const pct = v => ((Number(v) - d.bound[0]) / span) * 100;
  d.el.fill.style.left = pct(d.el.lo.value) + "%";
  d.el.fill.style.right = (100 - pct(d.el.hi.value)) + "%";
}

function initSliders(bounds = {}) {
  for (const d of DUALS) {
    const root = $(`[data-dual="${d.key}"]`);
    if (!root) continue;
    // Bounds come from the catalog, not a constant, so the track always spans
    // what actually exists — the boot response carries the largest value of
    // each, since the client no longer holds the column to take a maximum of.
    // A handle parked at either end means "no bound" rather than "a bound that
    // happens to equal the extreme", which is what keeps an untouched slider
    // out of the URL and out of the filter.
    const top = Number(bounds[d.of]) || 0;
    d.bound = [0, Math.max(d.step, Math.ceil(top / d.step) * d.step)];
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
      });

      // Pointer-up, or a keyboard commit: the one boundary in a drag worth
      // spending a request on.
      d.el[side].addEventListener("change", () => render());
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

/* ---------- responsive chrome ---------- */

// Everything here is layout, not data, so it is mounted before the catalog is
// fetched: a failed fetch leaves a degraded page, and a degraded page still
// has to have a reachable theme toggle and a search field you can see.
//
// 900px, not the 760px this used to watch. The number has to be the width at
// which app.css turns the rail from a column in the page into a fixed drawer
// over it, because the only thing left keyed to it is closing that drawer —
// and 760 was the old nav-relocation breakpoint, which no longer exists. The
// gap between the two was a real hole: opening the drawer at 850px and then
// widening past 900 left the rail styled as a column again with body.railopen
// still set, so the page behind it could not be scrolled and nothing was over
// it to explain why.
const DRAWER = matchMedia("(max-width: 900px)");

function mountChrome() {
  const rail = $("#rail");
  const toggle = $("#railtoggle");

  const setRail = on => {
    rail.classList.toggle("on", on);
    toggle.setAttribute("aria-expanded", String(on));
    document.body.classList.toggle("railopen", on);
  };

  // This used to also carry the header nav into the rail and back out again as
  // the breakpoint was crossed, which is why it needed a media query listener
  // and a remembered home element. The nav stays in the header at every width
  // now — see the note in components/BrowseHead.astro — so all that is left is
  // opening and shutting the drawer. CSS does the rest.
  //
  // Crossing the breakpoint still has to close it: the rail restyles from a
  // column to a fixed drawer underneath the reader, and shut is the only state
  // that reads the same on both sides of the line. It is also what keeps the
  // body scroll lock from being stranded on a page with no drawer over it.
  DRAWER.addEventListener("change", () => setRail(false));

  toggle.addEventListener("click", () => setRail(!rail.classList.contains("on")));
  $("#railclose").addEventListener("click", () => setRail(false));
  document.addEventListener("keydown", e => {
    if (e.key === "Escape") setRail(false);
  });
}

/* ---------- events ---------- */

/* Typing is the one input that fires per character, and every character is now
 * a request. 130ms is long enough that a word costs one round trip and short
 * enough that nobody notices waiting for it — it was already the search box's
 * delay, and the number boxes get it too now that they are no longer free. */
const debounce = (fn, ms = 130) => {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
};

function wire() {
  /* Fade each photograph in as it arrives — see .grid .shot img in app.css for
   * why. `load` does not bubble, so this listens in the capture phase on the
   * container rather than binding a handler per card; there are thousands of
   * cards in a long scroll and one listener is the whole cost.
   *
   * `error` marks the image too. A broken photo has nothing to show either way,
   * and leaving it at opacity 0 would hide the alt text with it. */
  const shown = e => {
    const img = e.target;
    if (img.tagName === "IMG") img.classList.add("in");
  };
  $("#results").addEventListener("load", shown, true);
  $("#results").addEventListener("error", shown, true);

  $("#q").addEventListener("input", debounce(e => {
    S.q = e.target.value.trim();
    render();
  }));

  document.addEventListener("click", e => {
    // A link is a link. The card still opens the drawer, but the model name
    // navigates, so there are two ways through and neither fights the other.
    if (e.target.closest("a[href]")) return;

    /* Laptop first, because it is the one group in the rail with no set behind
       it — see the note above facetSets(). One-of, so pressing a rung replaces
       whatever was chosen rather than adding to it, and pressing the lit rung
       is the way back to any size: every other group has that in unticking, and
       a group where a press could only ever move the choice sideways would be
       one you could enter and never leave. syncFacetButtons() rather than
       setting the attribute here, because two rows change on a press and only
       one of them was clicked. */
    const lap = e.target.closest('[data-f="laptop"]');
    if (lap) {
      const rung = Number(lap.dataset.v);
      S.laptopMin = S.laptopMin === rung ? null : rung;
      syncFacetButtons();
      return render();
    }

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
    const fav = e.target.closest("[data-fav]");
    if (fav) return flipFavourite(fav.closest("[data-id]").dataset.id);

    const apply = e.target.closest("[data-apply]");
    if (apply) {
      const hit = savedFilters().find(f => f.id === apply.dataset.apply);
      // Pressing the chip for the filter already on screen turns it off, the
      // way every other control in the rail does.
      if (hit) applyQuery(hit.query === stateParams().toString() ? "" : hit.query);
      return;
    }
    const forget = e.target.closest("[data-forget]");
    if (forget) {
      forgetFilter(forget.dataset.forget);
      say("");
      return renderSaved();
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

    // Delegated like everything else here, because the button is drawn and
    // taken away again by retireSentinel() as the burst runs out and is
    // refilled — there is nothing stable to bind a listener to.
    if (e.target.closest("[data-more]")) return loadMore();

    const det = e.target.closest("[data-detail]");
    if (det) return openDetail(det.closest("[data-id]").dataset.id);

    if (e.target.closest("[data-close]") || e.target.classList.contains("overlay")) {
      closeOverlays();
    }
  });

  // The handles follow every keystroke; the results wait for a pause, the same
  // way they wait for a handle to be let go. Typing "250" into a max box is one
  // filter, not three.
  const num = (id, key) => {
    const ask = debounce(render);
    $(id).addEventListener("input", e => {
      S[key] = e.target.value === "" ? null : Number(e.target.value);
      syncSliders();
      ask();
    });
  };
  num("#vol-min", "volMin"); num("#vol-max", "volMax");
  num("#price-min", "priceMin"); num("#price-max", "priceMax");
  num("#weight-min", "weightMin"); num("#weight-max", "weightMax");

  const askLinear = debounce(render);
  $("#linear-max").addEventListener("input", e => {
    const v = e.target.value === "" ? null : Number(e.target.value);
    S.linearMax = v == null ? null
      : Number((inches() ? v * CM_PER_IN : v).toFixed(1));
    askLinear();
  });

  // Only the length inputs need touching. Every other measurement on the page —
  // the dimensions, the linear total, and now the weights — ships in both units
  // already and CSS is doing the switching, so a unit flip repaints the whole
  // grid without a single card being redrawn. An input is the one thing a
  // stylesheet cannot rewrite, which is the entire reason this listener exists.
  document.addEventListener("unitchange", syncUnits);

  const toggle = (id, key) => $(id)?.addEventListener("click", e => {
    S[key] = !S[key];
    e.currentTarget.setAttribute("aria-pressed", S[key]);
    render();
  });
  toggle("#f-stock", "stock");
  toggle("#f-sale", "sale");
  toggle("#f-fav", "favOnly");

  $("#sort").addEventListener("change", e => { S.sort = e.target.value; render(); });

  $("#clear").addEventListener("click", () => applyQuery(""));

  // Nothing in here can do anything without JavaScript and localStorage, so
  // the markup ships hidden and this is what admits it exists.
  $("#saved").hidden = false;
  mountSaveForm();

  $("#cmpopen").addEventListener("click", () => {
    renderCompare();
    openOverlay($("#cmpoverlay"));
  });
  $("#cmpclear").addEventListener("click", () => {
    const ids = [...compare];
    compare.clear();
    ids.forEach(syncSelection);
  });

  // The rail toggle is wired in mountChrome(), with the rest of the chrome.

  // The theme toggle lives in the layout now — every page has one, so wiring it
  // here as well would bind it twice and cancel itself out.

  document.addEventListener("keydown", e => {
    if (e.key === "Escape") closeOverlays();
    if (e.key === "/" && document.activeElement !== $("#q")) {
      e.preventDefault(); $("#q").focus();
    }
  });
}

/* ---------- boot ---------- */

(async function init() {
  mountChrome();
  // State before the first request: a link someone shared is part of the
  // question, not a correction to the answer that follows. The facet buttons
  // and the slider handles do not exist to be pressed yet, which is why the
  // other half of loadURL's job is repeated below once they do.
  loadURL();
  wire();

  // boot=1 asks for the rail, the slider tracks, the coverage meter and the
  // first page of results in one go, so arriving on a filtered link still costs
  // exactly one request before there is something to look at. A failure swaps
  // the boot skeletons back out for the server-rendered list of every model —
  // see render(); a degraded page beats a dead one.
  const data = await render({ boot: true });
  if (!data) return;

  STATS = data.stats;
  if (data.facets) buildFacets(data);
  FX = data.fx ?? null;
  initSliders(data.bounds);
  syncFacetButtons();
  // After the buttons know which of them are pressed, because that is what
  // decides whether an option counting zero is dimmed or left alone — arriving
  // on a shared link means arriving with ticks already set and a rail already
  // counted against them.
  paintFacetCounts(data.facets);
})();
