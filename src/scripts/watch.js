/* Price-drop signup, submitted without a page load.
 *
 * Delegated from the document rather than bound to a form, because the form is
 * not always in the HTML: the model page renders one at build time, and the
 * browse drawer injects one whenever a bag is opened. A delegated listener
 * covers both, and covers a drawer that is re-rendered between openings.
 *
 * Lives here rather than inside WatchForm.astro so the browse island can have
 * the behaviour without rendering the component. Importing it twice is free —
 * the bundler dedupes, and the guard below makes a double import harmless
 * anyway.
 */
if (!window.__watchFormBound) {
  window.__watchFormBound = true;

  document.addEventListener('submit', async (e) => {
    const form = e.target.closest('[data-watch]');
    if (!form) return;
    e.preventDefault();

    const button = form.querySelector('button');
    const msg = form.querySelector('.watchmsg');
    const data = Object.fromEntries(new FormData(form));
    if (!data.max_price) delete data.max_price;

    button.disabled = true;
    msg.hidden = false;
    msg.className = 'watchmsg';
    msg.textContent = 'Sending…';

    try {
      const res = await fetch('/api/watch', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(data),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.error || `error ${res.status}`);
      msg.textContent =
        'Check your inbox — the watch starts once you confirm the link.';
      form.querySelector('input[name=email]').value = '';
    } catch (err) {
      msg.className = 'watchmsg bad';
      msg.textContent = `Could not save that watch: ${err.message}`;
    } finally {
      button.disabled = false;
    }
  });
}
