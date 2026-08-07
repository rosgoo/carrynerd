/* The star on a static page.
 *
 * The browse island has its own, wired into the grid it draws — see
 * flipFavourite() in scripts/browse.js. A model page draws no grid and mounts
 * no island: it is HTML, built once, and the only thing about it that differs
 * per reader is whether this particular bag is already saved. So this is the
 * whole of it — find the buttons, ask lib/store.js, paint.
 *
 * The markup ships hidden and this is what shows it. Without JavaScript there
 * is nowhere to keep a favourite, and a button that cannot do its job is worse
 * than no button.
 */

import { isFavourite, toggleFavourite, MAX_FAVOURITES } from '../lib/store.js';

for (const btn of document.querySelectorAll('[data-fav-toggle]')) {
  const id = btn.dataset.favToggle;
  const bar = btn.closest('[data-favbar]') ?? btn;
  const note = bar.querySelector('[data-favmsg]');

  const paint = () => {
    const on = isFavourite(id);
    btn.setAttribute('aria-pressed', String(on));
    btn.textContent = on ? '★ Saved' : '☆ Save to favourites';
    bar.hidden = false;
  };

  btn.addEventListener('click', () => {
    const { full } = toggleFavourite(id);
    if (note) {
      note.textContent = full
        ? `That is ${MAX_FAVOURITES} favourites — the most this keeps.`
        : '';
      note.hidden = !full;
    }
    paint();
  });

  paint();
}
