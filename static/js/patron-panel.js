/* patron-panel.js: a patron's details in a side panel, opened by any [data-patron-panel="<id>"]. */
(function () {
  'use strict';

  var script = document.currentScript;
  var BASE = script ? script.getAttribute('data-url') : '';   // ".../patron-panel/0/"
  var box = null, body = null, current = null, previous = null;

  function url(id, tail) { return BASE.replace(/0\/$/, encodeURIComponent(id) + '/') + (tail || ''); }

  function toast(message, kind) {
    if (window.AylaDialog) AylaDialog.toast(message, kind); else alert(message);
  }

  function build() {
    box = document.createElement('div');
    box.id = 'aylaPatronPanel';
    box.className = 'hidden fixed inset-0 z-[80]';
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-modal', 'true');
    box.setAttribute('aria-labelledby', 'patronPanelTitle');
    box.innerHTML = '<div data-panel-backdrop class="absolute inset-0 bg-black/50"></div>'
      // Not an <aside>: the portal styles those as its sidebar.
      + '<div data-panel-sheet class="absolute right-0 top-0 h-full w-full max-w-md shadow-2xl border-l transition-transform duration-200 translate-x-full"'
      + ' style="background-color: var(--bg-modal); border-color: var(--border-color);"><div data-panel-body class="h-full"></div></div>';
    document.body.appendChild(box);
    body = box.querySelector('[data-panel-body]');
    box.addEventListener('click', onClick);
    box.addEventListener('submit', onSubmit);
  }

  async function load(id) {
    body.innerHTML = '<p class="p-6 text-sm text-slate-400"><i class="fa-solid fa-spinner fa-spin mr-2"></i>Loading&hellip;</p>';
    try {
      var res = await fetch(url(id), { headers: { 'X-Requested-With': 'XMLHttpRequest' } });
      if (!res.ok || res.redirected) throw new Error('not loaded');
      body.innerHTML = await res.text();
    } catch (e) {
      body.innerHTML = '<div class="p-6 space-y-3"><p class="text-sm text-slate-300">The details could not be loaded. '
        + 'Check that you are still signed in, then try again.</p>'
        + '<button type="button" data-panel-close class="px-3 py-1.5 rounded-lg border border-slate-700/60 text-slate-300 text-xs font-bold">Close</button></div>';
    }
  }

  function open(id) {
    if (!box) build();
    if (current === null) previous = document.activeElement;
    current = String(id);
    box.classList.remove('hidden');
    requestAnimationFrame(function () { box.querySelector('[data-panel-sheet]').classList.remove('translate-x-full'); });
    window.addEventListener('keydown', onKey, true);
    return load(current).then(function () {
      var c = box.querySelector('[data-panel-close]');
      if (c) c.focus();
    });
  }

  function close() {
    if (!box || current === null) return;
    box.querySelector('[data-panel-sheet]').classList.add('translate-x-full');
    setTimeout(function () { box.classList.add('hidden'); }, 200);
    window.removeEventListener('keydown', onKey, true);
    current = null;
    if (previous && previous.focus) { try { previous.focus(); } catch (e) { /* gone */ } }
  }

  // Keys belong to the panel while it is open, not to a dialog or scanner behind it.
  function onKey(e) {
    if (e.key === 'Escape') { e.preventDefault(); e.stopImmediatePropagation(); close(); return; }
    if (box.contains(document.activeElement)) e.stopPropagation();
  }

  function token() {
    var f = body.querySelector('[name="csrfmiddlewaretoken"]');
    return f ? f.value : '';
  }

  async function hold(data) {
    data.csrfmiddlewaretoken = token();
    var r;
    try {
      var res = await fetch(url(current, 'hold/'), {
        method: 'POST', body: new URLSearchParams(data),
        headers: { 'X-CSRFToken': token(), 'X-Requested-With': 'XMLHttpRequest' },
      });
      r = await res.json();
    } catch (e) {
      r = { success: false, error: 'That could not be saved. Check that you may edit patrons, then try again.' };
    }
    toast(r.success ? r.message : r.error, r.success ? 'success' : 'error');
    if (r.success) {
      document.dispatchEvent(new CustomEvent('ayla:patron-changed', { detail: { id: current } }));
      load(current);
    }
  }

  async function onClick(e) {
    if (e.target.closest('[data-panel-close]') || e.target.hasAttribute('data-panel-backdrop')) { close(); return; }
    var form = body.querySelector('[data-hold-form]');
    if (e.target.closest('[data-hold-change]')) {
      form.classList.remove('hidden');
      body.querySelector('[data-hold-buttons]').classList.add('hidden');
      form.querySelector('textarea').focus();
      return;
    }
    if (e.target.closest('[data-hold-cancel]')) {
      form.classList.add('hidden');
      body.querySelector('[data-hold-buttons]').classList.remove('hidden');
      return;
    }
    if (e.target.closest('[data-hold-lift]')) {
      var name = (body.querySelector('#patronPanelTitle') || {}).textContent || 'this patron';
      var yes = window.AylaDialog
        ? await AylaDialog.confirm('Lift the hold on ' + name + '? They can sign in and borrow again.',
                                   { title: 'Lift the hold', confirmLabel: 'Lift it', danger: false })
        : window.confirm('Lift the hold on ' + name + '?');
      if (yes) hold({ action: 'lift' });
    }
  }

  function onSubmit(e) {
    if (!e.target.matches('[data-hold-form]')) return;
    e.preventDefault();
    hold({ action: 'change', hold_reason: e.target.querySelector('textarea').value });
  }

  // Any patron name or button marked with its id opens the panel.
  document.addEventListener('click', function (e) {
    var t = e.target.closest('[data-patron-panel]');
    if (!t || (box && box.contains(t))) return;
    e.preventDefault();
    open(t.getAttribute('data-patron-panel'));
  });

  window.AylaPatron = { open: open, close: close };
})();
