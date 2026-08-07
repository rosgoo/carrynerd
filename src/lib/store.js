/* carrynerd — the two things the browser remembers for you.
 *
 * There are no accounts here and adding them to hold a starred bag would be a
 * poor trade: a sign-in wall in front of a spec sheet, and a database of
 * people for us to look after, in exchange for a list of ids. So the two
 * things worth keeping — the bags you saved and the filters you named — live
 * in localStorage, on your machine. Neither is ever sent anywhere except as
 * ids in the query string the results are drawn from, and that request is the
 * same public one everybody else makes.
 *
 * The consequence is worth stating plainly rather than hiding: this is
 * per-browser. Clearing site data forgets it, a second device never had it,
 * and private windows lose it on close. That is the whole cost of not asking
 * anyone to register.
 *
 * Keys are brand-neutral for the same reason the theme and unit keys are — the
 * site has been renamed twice, and a brand-named key hands every returning
 * reader a reset each time. See the migration note in layouts/Base.astro.
 *
 * Everything here fails soft. Storage throws when it is disabled, full or
 * partitioned, and a browser that cannot remember a favourite is still a
 * browser that has to be able to browse.
 */

const FAV_KEY = 'favourites';
const FILTER_KEY = 'saved-filters';

/* Both caps are about what stays usable rather than about what fits. The
 * favourite ids ride on every filter request and their bags come back whole,
 * ahead of the paged results — sixty is already a long strip and two pages of
 * payload. A saved-filter list longer than twenty is a list you scan instead
 * of a shortcut you reach for. Neither is enforced by discarding: the write is
 * refused and the caller says so, because silently dropping the oldest
 * favourite is the one behaviour nobody would ever ask for. */
export const MAX_FAVOURITES = 60;
export const MAX_FILTERS = 20;

/* Parsed once and kept, because the card renderer asks whether a bag is a
 * favourite sixty times per painted page and JSON.parse per card is a silly
 * way to spend a frame. The `storage` listener below is what stops the cache
 * going stale: it fires in the *other* tabs, so a second tab writing its own
 * stale copy over this one's changes cannot happen. */
let favCache = null;
let filterCache = null;

const readList = (key) => {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return [];
    const val = JSON.parse(raw);
    return Array.isArray(val) ? val : [];
  } catch {
    // Unreadable is the same as absent. A hand-edited or half-written value is
    // not worth a thrown exception on every page load.
    return [];
  }
};

const writeList = (key, val) => {
  try {
    localStorage.setItem(key, JSON.stringify(val));
    return true;
  } catch {
    return false;
  }
};

/* ---------- favourites ---------- */

/** The saved ids, oldest first. Order is insertion order and nothing reads it
 *  for display — the strip above the results is drawn in the catalog's own
 *  sort order, so that a favourite sits where the same bag would sit. */
export function favourites() {
  if (!favCache) favCache = readList(FAV_KEY).filter((id) => typeof id === 'string');
  return [...favCache];
}

export function isFavourite(id) {
  if (!favCache) favourites();
  return favCache.includes(id);
}

/** Toggle one bag. Returns `on` — the state it ended in — and `full`, which is
 *  set when the star was refused for being past the cap, so the caller can say
 *  so rather than leaving a button that appears not to work. */
export function toggleFavourite(id) {
  const list = favourites();
  const at = list.indexOf(id);
  if (at >= 0) {
    list.splice(at, 1);
  } else {
    if (list.length >= MAX_FAVOURITES) return { on: false, full: true };
    list.push(id);
  }
  favCache = list;
  writeList(FAV_KEY, list);
  return { on: at < 0, full: false };
}

/* ---------- saved filters ---------- */

/* One saved filter is a name and a query string — the same query string the
 * address bar carries and /api/browse is asked, produced by stateParams() in
 * scripts/browse.js. Storing the spelling the URL already uses rather than a
 * structured copy of the filter state means a saved filter cannot drift out of
 * step with what a shared link does, and that applying one is just navigating
 * to it. */

const filterShape = (f) =>
  f && typeof f === 'object' && typeof f.id === 'string' &&
  typeof f.name === 'string' && typeof f.query === 'string';

export function savedFilters() {
  if (!filterCache) filterCache = readList(FILTER_KEY).filter(filterShape);
  return filterCache.map((f) => ({ ...f }));
}

/** Save the current filter under a name.
 *
 *  Returns `{ ok }`, and on refusal a `reason` the caller prints: `empty` for
 *  an unfiltered page, which is a bookmark for the home page rather than a
 *  filter; `full` at the cap; `stored` when the browser would not keep it.
 *  Saving a query that is already saved renames the one that is there instead
 *  of filing a second chip that does the identical thing. */
export function saveFilter(name, query) {
  if (!query) return { ok: false, reason: 'empty' };
  const list = savedFilters();
  const clean = (name || '').trim().slice(0, 48) || 'Untitled filter';

  const existing = list.find((f) => f.query === query);
  if (existing) {
    existing.name = clean;
  } else {
    if (list.length >= MAX_FILTERS) return { ok: false, reason: 'full' };
    list.push({
      // Time plus noise: two filters saved in the same millisecond is not a
      // thing a person does, but an id collision would silently delete the
      // other one, and the fix costs four characters.
      id: Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
      name: clean,
      query,
    });
  }

  filterCache = list;
  if (!writeList(FILTER_KEY, list)) {
    filterCache = null;
    return { ok: false, reason: 'stored' };
  }
  return { ok: true, filter: list.find((f) => f.query === query) };
}

export function forgetFilter(id) {
  const list = savedFilters().filter((f) => f.id !== id);
  filterCache = list;
  writeList(FILTER_KEY, list);
}

/* ---------- other tabs ---------- */

/* Fires only in the tabs that did not do the writing, which is exactly the set
 * whose cache is now wrong. Dropping it rather than reparsing here means a tab
 * left open in the background pays nothing until it is looked at again. */
if (typeof addEventListener === 'function') {
  addEventListener('storage', (e) => {
    if (e.key === FAV_KEY || e.key === null) favCache = null;
    if (e.key === FILTER_KEY || e.key === null) filterCache = null;
  });
}
