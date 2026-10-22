/* Manage Books' Check authors: the author lines split into names by a guess, looked over and saved. */
(function () {
  'use strict';

  var URL = '/admin-portal/books/check-authors/';
  var button = document.getElementById('checkAuthorsBtn');
  var modal = document.getElementById('checkAuthorsModal');
  var body = document.getElementById('checkAuthorsBody');
  if (!button || !modal || !body) return;
  var saved = false;
  var AJAX = { 'X-Requested-With': 'XMLHttpRequest' };

  function csrf() {
    var el = document.querySelector('[name=csrfmiddlewaretoken]');
    return el ? el.value : '';
  }
  function setCount(n) {
    button.classList.toggle('hidden', !n);
    button.querySelector('[data-count]').textContent = n;
  }
  function toast(text, kind) {
    if (window.AylaDialog) AylaDialog.toast(text, kind); else alert(text);
  }

  fetch(URL + '?count=1', { headers: AJAX })
    .then(function (r) { return r.json(); })
    .then(function (d) { setCount(d.lines || 0); })
    .catch(function () {});

  function load(page) {
    return fetch(URL + '?page=' + page, { headers: AJAX })
      .then(function (r) { return r.text(); })
      .then(function (html) {
        var holder = document.createElement('div');
        holder.innerHTML = html;
        var list = holder.querySelector('[data-check-authors]');
        var current = body.querySelector('[data-check-authors]');
        if (page > 1 && current) {
          var more = current.querySelector('[data-check-more]');
          if (more) more.remove();
          Array.prototype.slice.call(list.children).forEach(function (el) { current.appendChild(el); });
        } else {
          body.innerHTML = '';
          body.appendChild(list);
        }
        body.querySelectorAll('[data-author-fields]').forEach(function (el) { AylaAuthors.attach(el); });
        setCount(+list.dataset.lines || 0);
      })
      .catch(function () {
        body.innerHTML = '<p class="text-sm text-rose-400">Could not load the list. Try again.</p>';
      });
  }

  window.openCheckAuthors = function () {
    modal.classList.remove('hidden');
    body.innerHTML = '<p class="text-sm text-slate-500">Loading…</p>';
    load(1);
  };
  window.closeCheckAuthors = function () {
    modal.classList.add('hidden');
    // The list behind shows the saved names.
    if (saved) location.reload();
  };

  body.addEventListener('click', function (e) {
    var more = e.target.closest('[data-check-more]');
    if (more) {
      more.disabled = true;
      load(+more.dataset.checkMore);
    }
  });

  body.addEventListener('submit', function (e) {
    var form = e.target.closest('[data-check-line]');
    if (!form) return;
    e.preventDefault();
    var widget = AylaAuthors.attach(form.querySelector('[data-author-fields]'));
    var error = form.querySelector('[data-check-error]');
    var bad = widget.problem();
    error.textContent = bad;
    error.classList.toggle('hidden', !bad);
    if (bad) { widget.focus(); return; }
    var submit = form.querySelector('[type=submit]');
    submit.disabled = true;
    fetch(URL + 'save/', { method: 'POST', body: new FormData(form),
                           headers: { 'X-CSRFToken': csrf(), 'X-Requested-With': 'XMLHttpRequest' } })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        submit.disabled = false;
        if (!d.success) {
          error.textContent = d.error || 'Could not save.';
          error.classList.remove('hidden');
          return;
        }
        saved = true;
        form.remove();
        setCount(d.lines || 0);
        toast('Saved as ' + d.display + ' on ' + d.books + ' book' + (d.books === 1 ? '' : 's') + '.', 'success');
        // The last one on this page: the next lot, or the all-done note.
        if (!body.querySelector('[data-check-line]')) load(1);
      })
      .catch(function () {
        submit.disabled = false;
        error.textContent = 'Could not reach the server. Try again.';
        error.classList.remove('hidden');
      });
  });
})();
