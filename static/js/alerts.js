/* alerts.js: a pop-up on every portal page when a registration, extension request, overdue loan or message arrives. */
(function () {
  'use strict';

  var script = document.currentScript;
  var url = script && script.dataset.url;
  if (!url) return;

  var STORE = 'ayla.alerts';
  var TOPICS = ['patrons', 'transactions', 'chat', 'books', 'door'];
  var stack = null;
  var busy = false;

  function seen() {
    try { return JSON.parse(sessionStorage.getItem(STORE) || 'null'); } catch (e) { return null; }
  }
  function remember(map) {
    try { sessionStorage.setItem(STORE, JSON.stringify(map)); } catch (e) { /* storage refused */ }
  }

  function isNew(item, before) {
    // A quiet count keeps its badge up to date without a pop-up.
    if (!item.count || item.quiet) return false;
    if (!before) return false;
    return item.latest > before.latest || item.count > before.count
      || (item.stamp && item.stamp > (before.stamp || ''));
  }

  function check() {
    if (busy) return;
    busy = true;
    fetch(url, { headers: { 'X-Ayla-Live': '1' }, credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data || !data.success) return;
        var before = seen();
        var now = {};
        data.items.forEach(function (item) {
          now[item.key] = { count: item.count, latest: item.latest, stamp: item.stamp || '' };
          // The first look in this tab sets the baseline instead of raising old news,
          // except the once-per-sign-in notice of what is already waiting.
          if (before && isNew(item, before[item.key])) show(item);
          else if (item.notice) show({ key: item.key, icon: item.icon, url: item.url,
                                       title: 'Waiting for review', text: item.notice });
          paintBadges(item);
        });
        remember(now);
      })
      .catch(function () { /* offline; the next change tries again */ })
      .then(function () { busy = false; });
  }

  // Counts shown on the sidebar follow every check, so they are live on every page.
  function paintBadges(item) {
    document.querySelectorAll('[data-alert-badge="' + item.key + '"]').forEach(function (el) {
      el.textContent = item.count;
      el.hidden = !item.count;
    });
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function show(item) {
    if (!stack) {
      stack = document.createElement('div');
      stack.className = 'ayla-alerts';
      stack.setAttribute('aria-live', 'polite');
      document.body.appendChild(stack);
    }
    var old = stack.querySelector('[data-key="' + item.key + '"]');
    if (old) old.remove();
    var card = document.createElement('div');
    card.className = 'ayla-alert ayla-alert-' + item.key;
    card.setAttribute('data-key', item.key);
    card.setAttribute('role', 'status');
    card.innerHTML = '<i class="fa-solid ' + esc(item.icon) + ' ayla-alert-icon" aria-hidden="true"></i>'
      + '<div class="ayla-alert-body"><strong>' + esc(item.title) + '</strong>'
      + '<span>' + esc(item.text) + '</span>'
      + '<a href="' + esc(item.url) + '">Open</a></div>'
      + '<button type="button" aria-label="Dismiss">&times;</button>';
    card.querySelector('button').onclick = function () { card.remove(); };
    stack.insertBefore(card, stack.firstChild);
    while (stack.children.length > 4) stack.lastChild.remove();
  }

  document.addEventListener('ayla:live-any', function (e) {
    if (e.detail.topics.some(function (t) { return t === '*' || TOPICS.indexOf(t) !== -1; })) check();
  });
  check();
})();
