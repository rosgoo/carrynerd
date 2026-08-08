/* Opening and submitting the brand-request dialog.
 *
 * Delegated from the document for the same reason watch.js is: the openers are
 * not all in the HTML. The footer's is in the layout, the one on /brands/ is on
 * the page, and the one in the filter rail is created by browse.js the moment
 * the Brand list grows a search box. A delegated listener covers all three and
 * covers a rail that is re-rendered between requests.
 *
 * Guarded against a double import — the layout renders the component and the
 * browse island's bundle pulls it in as well.
 */
if (!window.__requestBrandBound) {
  window.__requestBrandBound = true;

  const dialog = () => document.getElementById('reqbrand');

  /* The static openers ship hidden and are revealed here, the same way the
     rail's saved-filters panel is. Everything this dialog does happens in
     JavaScript — there is no page it can post to and come back from — so with
     scripting off a visible "Request a brand" button would be a button that
     does nothing, which is worse than no button. */
  for (const el of document.querySelectorAll('[data-request-brand]')) {
    el.hidden = false;
  }

  document.addEventListener('click', (e) => {
    const opener = e.target.closest('[data-request-brand]');
    if (!opener) return;
    e.preventDefault();

    const dlg = dialog();
    if (!dlg) return;

    const form = dlg.querySelector('[data-request-brand-form]');
    const name = form.querySelector('input[name=name]');

    /* Whatever they typed into the Brand filter comes along as the brand they
       are asking for. Someone who searched the filter for "tom bihn", found
       nothing, and pressed this has already told us the answer; making them
       type it a second time is the sort of thing that turns a request into a
       shrug. Only when the field is empty, so a half-finished request is never
       overwritten by the next opening. */
    const suggested = opener.dataset.brand?.trim();
    if (suggested && !name.value) name.value = suggested;

    dlg.showModal();
    name.focus();
  });

  /* Close on the button, and on the backdrop. Escape is the dialog element's
     own behaviour and needs nothing here. The backdrop test works because a
     click on the padding *around* the form still reports the dialog itself as
     the target — the form covers everything inside it. */
  document.addEventListener('click', (e) => {
    const dlg = dialog();
    if (!dlg?.open) return;
    if (e.target.closest('[data-request-brand-close]') || e.target === dlg) dlg.close();
  });

  document.addEventListener('submit', async (e) => {
    const form = e.target.closest('[data-request-brand-form]');
    if (!form) return;
    e.preventDefault();

    const button = form.querySelector('button[type=submit]');
    const msg = form.querySelector('.reqmsg');
    const data = Object.fromEntries(new FormData(form));

    button.disabled = true;
    msg.hidden = false;
    msg.className = 'reqmsg';
    msg.textContent = 'Sending…';

    try {
      const res = await fetch('/api/request-brand', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(data),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.error || `error ${res.status}`);
      msg.textContent =
        'Noted — thank you. Brands asked for most often are the ones crawled next.';
      form.reset();
    } catch (err) {
      msg.className = 'reqmsg bad';
      msg.textContent = `Could not send that: ${err.message}`;
    } finally {
      button.disabled = false;
    }
  });
}
