/* The author boxes: one row per author, either one picked from the authors on file or a new one
   typed as first, middle and surname. Two hidden inputs carry them: the names as JSON (with the id
   of a picked author), and the author line built from them. */
(function () {
  'use strict';

  var PARTICLES = ['de', 'dela', 'del', 'delos', 'delas', 'della', 'di', 'da', 'das', 'dos', 'la', 'las', 'los',
                   'le', 'du', 'san', 'santa', 'santo', 'sta', 'sto', 'st', 'van', 'von', 'der', 'den', 'y'];
  // The Spanish and Filipino ones never stand as a first name.
  var SURNAME_LEADS = ['de', 'dela', 'del', 'delos', 'delas', 'della', 'di', 'da', 'das', 'dos', 'la', 'las', 'los',
                       'san', 'santa', 'santo', 'sta', 'sto', 'y', 'ng'];
  var SUFFIX = /^(jr\.?|sr\.?|ii|iii|iv)$/i;

  function tidy(text) { return String(text == null ? '' : text).split(/\s+/).filter(Boolean).join(' '); }

  // "Roald Dahl", "Dahl, Roald" or "Juan Dela Cruz Jr." into the three boxes.
  function splitName(text) {
    text = tidy(text);
    if (text.indexOf(',') !== -1) {
      var parts = text.split(',');
      var given = tidy(parts.slice(1).join(' ')).split(' ').filter(Boolean);
      return { first: given[0] || '', middle: given.slice(1).join(' '), last: tidy(parts[0]) };
    }
    var words = text.split(' ').filter(Boolean);
    var suffix = words.length > 1 && SUFFIX.test(words[words.length - 1]) ? words.pop() : '';
    // "De Vega" alone is a surname; "Van Morrison" is not.
    var leads = function (w) { return SURNAME_LEADS.indexOf(w.toLowerCase().split('-').pop().replace(/\.$/, '')) !== -1; };
    if (words.length < 2 || words.slice(0, -1).every(leads)) {
      return { first: '', middle: '', last: tidy(words.join(' ') + ' ' + suffix) };
    }
    var cut = words.length - 1;
    while (cut > 1 && PARTICLES.indexOf(words[cut - 1].toLowerCase().replace(/\.$/, '')) !== -1) cut -= 1;
    return { first: words[0], middle: words.slice(1, cut).join(' '),
             last: tidy(words.slice(cut).join(' ') + ' ' + suffix) };
  }

  function shown(c) {
    return [c.first, c.middle, c.last].filter(Boolean).join(' ');
  }
  // Surname first, as the reports sort: "Dahl, Roald".
  function filed(c) {
    var given = [c.first, c.middle].filter(Boolean).join(' ');
    return given ? c.last + ', ' + given : c.last;
  }

  // The same author line the server builds: "A & B", "A, B & C", "A, B et al.".
  function line(data) {
    var names = data.credits.map(shown);
    if (!names.length) return data.unknown ? (data.others ? 'Various Authors' : 'Unknown') : '';
    if (data.others) return names.join(', ') + ' et al.';
    if (names.length > 1) return names.slice(0, -1).join(', ') + ' & ' + names[names.length - 1];
    return names[0];
  }

  // The authors on file, fetched once for every author box on the page: [id, first, middle, last, books].
  var onFile = { items: [], byId: {}, promise: null };
  function loadOnFile(url) {
    if (!onFile.promise && url) {
      onFile.promise = fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          (d.authors || []).forEach(function (r) {
            var a = { id: r[0], first: r[1], middle: r[2], last: r[3], books: r[4] };
            onFile.byId[a.id] = a;
            onFile.items.push({ value: String(a.id), label: shown(a), search: filed(a),
                                sub: filed(a) + ' · ' + a.books + ' book' + (a.books === 1 ? '' : 's') });
          });
        })
        .catch(function () { onFile.promise = null; });
    }
    return onFile.promise || Promise.resolve();
  }

  function attach(root) {
    if (!root || root.aylaAuthors) return root && root.aylaAuthors;
    var rows = root.querySelector('[data-af-rows]');
    var template = root.querySelector('[data-af-template]');
    var addButton = root.querySelector('[data-af-add]');
    var unknownBox = root.querySelector('[data-af-unknown]');
    var othersBox = root.querySelector('[data-af-others]');
    var textInput = root.querySelector('[data-af-text]');
    var jsonInput = root.querySelector('[data-af-json]');
    var canPick = !!window.AylaPicker;

    function rowEls() { return Array.prototype.slice.call(rows.querySelectorAll('[data-af-row]')); }
    function box(row, name) { return row.querySelector('[data-af="' + name + '"]'); }
    function search(row) { return row.querySelector('[data-af-search]'); }

    // Existing: find one on file; New: the three name boxes.
    function setMode(row, mode, focus) {
      if (!canPick) mode = 'new';
      row.dataset.mode = mode;
      row.querySelectorAll('[data-af-mode]').forEach(function (b) {
        var on = b.dataset.afMode === mode;
        b.classList.toggle('is-on', on);
        b.setAttribute('aria-selected', on ? 'true' : 'false');
      });
      row.querySelector('[data-af-saved]').classList.toggle('hidden', mode !== 'saved');
      row.querySelector('[data-af-person]').classList.toggle('hidden', mode !== 'new');
      if (focus) setTimeout(function () { focusRow(row); }, 0);
    }

    function pick(row, a) {
      row.dataset.id = a.id;
      row.picked = a;
      row.querySelector('[data-af-card-name]').textContent = shown(a);
      row.querySelector('[data-af-card-sub]').textContent = filed(a)
        + (a.books ? ' · ' + a.books + ' book' + (a.books === 1 ? '' : 's') : '');
      searchWrap(row).classList.add('hidden');
      row.querySelector('[data-af-card]').classList.remove('hidden');
    }
    function unpick(row) {
      delete row.dataset.id;
      row.picked = null;
      row.querySelector('[data-af-card]').classList.add('hidden');
      searchWrap(row).classList.remove('hidden');
    }
    // The picker wraps the search box; the wrapper is what hides.
    function searchWrap(row) { return search(row).closest('.ayla-picker') || search(row); }

    function addRow(c, focus) {
      c = c || {};
      var row = template.content.firstElementChild.cloneNode(true);
      ['first', 'middle', 'last'].forEach(function (k) { box(row, k).value = c[k] || ''; });
      rows.appendChild(row);
      if (canPick) {
        AylaPicker.attach(search(row), {
          items: function () { return onFile.items; },
          keepLabel: false,
          newLabel: function (q) { return q ? '+ New author: “' + q + '”' : '+ New author (not on file yet)'; },
          onPick: function (it) { pick(row, onFile.byId[it.value]); changed(); },
          // Not on file: the name typed is spread over the three boxes.
          onNew: function (q) {
            var parts = splitName(q);
            ['first', 'middle', 'last'].forEach(function (k) { box(row, k).value = q ? parts[k] : ''; });
            search(row).value = '';
            setMode(row, 'new', true);
            changed();
          },
        });
      }
      if (c.id) {
        setMode(row, 'saved');
        pick(row, onFile.byId[c.id] || { id: c.id, first: c.first || '', middle: c.middle || '', last: c.last || '' });
      } else {
        setMode(row, (c.first || c.middle || c.last) ? 'new' : 'saved');
      }
      if (focus) focusRow(row);
      return row;
    }

    function read() {
      var credits = [];
      // The names are set aside while "Author unknown" is ticked.
      if (!unknownBox.checked) {
        rowEls().forEach(function (row) {
          if (row.dataset.mode === 'saved') {
            var a = row.picked;
            if (a) credits.push({ id: a.id, first: a.first || '', middle: a.middle || '', last: a.last || '' });
            return;
          }
          var c = { first: tidy(box(row, 'first').value), middle: tidy(box(row, 'middle').value),
                    last: tidy(box(row, 'last').value) };
          if (c.first || c.middle || c.last) credits.push(c);
        });
      }
      return { credits: credits, unknown: unknownBox.checked, others: othersBox.checked };
    }

    function problem() {
      if (!unknownBox.checked) {
        var typed = rowEls().filter(function (r) {
          return r.dataset.mode === 'saved' && !r.picked && search(r).value.trim();
        })[0];
        if (typed) return 'Pick "' + search(typed).value.trim() + '" from the authors on file, or choose New author.';
      }
      var data = read();
      var half = data.credits.filter(function (c) { return !c.last; })[0];
      if (half) return 'Type the surname of ' + shown(half) + '. A writer known by one name goes in Surname.';
      if (!data.credits.length && !data.unknown) return 'Type the author, or tick "Author unknown" if the book names none.';
      return '';
    }

    function sync(quiet) {
      var data = read();
      var off = unknownBox.checked;
      rowEls().forEach(function (row) {
        row.classList.toggle('is-off', off);
        row.querySelectorAll('input, [data-af-mode], [data-af-change]').forEach(function (i) { i.disabled = off; });
        row.querySelector('[data-af-remove]').classList.toggle('invisible', rowEls().length < 2);
      });
      addButton.classList.toggle('hidden', off);
      var text = problem() && !data.unknown ? '' : line(data);
      var changed = textInput.value !== text || jsonInput.value !== JSON.stringify(data);
      textInput.value = text;
      jsonInput.value = JSON.stringify(data);
      if (changed && !quiet) textInput.dispatchEvent(new Event('input', { bubbles: true }));
      return data;
    }
    function changed() {
      sync(true);
      textInput.dispatchEvent(new Event('change', { bubbles: true }));
    }

    // A whole name pasted into one box is spread over the three; several names make several rows.
    rows.addEventListener('paste', function (e) {
      var input = e.target.closest('[data-af]');
      var row = e.target.closest('[data-af-row]');
      if (!input || !row) return;
      var text = tidy((e.clipboardData || window.clipboardData).getData('text'));
      var names = text.split(/\s*(?:;|&|\band\b|\/)\s*/).filter(Boolean);
      var empty = ['first', 'middle', 'last'].every(function (k) { return k === input.dataset.af || !box(row, k).value.trim(); });
      if (!empty || (names.length < 2 && text.split(' ').length < 2 && text.indexOf(',') === -1)) return;
      e.preventDefault();
      names.forEach(function (n, i) {
        var parts = splitName(n);
        var target = i === 0 ? row : addRow(parts);
        ['first', 'middle', 'last'].forEach(function (k) { box(target, k).value = parts[k]; });
      });
      changed();
    });
    rows.addEventListener('input', function (e) {
      // Typing again after a pick means that pick no longer stands.
      var row = e.target.closest('[data-af-row]');
      if (row && e.target.hasAttribute('data-af-search') && row.picked) unpick(row);
      sync();
    });
    rows.addEventListener('change', changed);
    rows.addEventListener('click', function (e) {
      var row = e.target.closest('[data-af-row]');
      if (!row) return;
      var mode = e.target.closest('[data-af-mode]');
      if (mode) { setMode(row, mode.dataset.afMode, true); changed(); return; }
      if (e.target.closest('[data-af-change]')) { unpick(row); search(row).value = ''; focusRow(row); changed(); return; }
      if (e.target.closest('[data-af-remove]') && rowEls().length > 1) { row.remove(); changed(); }
    });
    addButton.addEventListener('click', function () {
      addRow({}, true);
      sync(true);
    });
    unknownBox.addEventListener('change', changed);
    othersBox.addEventListener('change', changed);

    function set(data) {
      data = data || {};
      rows.innerHTML = '';
      (data.credits || []).forEach(function (c) { addRow(c); });
      if (!rowEls().length) addRow();
      unknownBox.checked = !!data.unknown;
      othersBox.checked = !!data.others;
      sync(true);
    }
    function focusRow(row) {
      if (row.dataset.mode === 'saved') {
        if (row.picked) row.querySelector('[data-af-change]').focus(); else search(row).focus();
        return;
      }
      var empty = ['first', 'last'].map(function (k) { return box(row, k); }).filter(function (i) { return !i.value.trim(); })[0];
      (empty || box(row, 'first')).focus();
    }
    // The first row still to fill: an author to find, or a first name or surname to type.
    function focus() {
      var open = rowEls().filter(function (r) {
        if (r.classList.contains('is-off')) return false;
        return r.dataset.mode === 'saved' ? !r.picked : !box(r, 'first').value.trim() || !box(r, 'last').value.trim();
      })[0] || rowEls()[0];
      if (open && !open.classList.contains('is-off')) focusRow(open); else unknownBox.focus();
    }

    var api = { get: read, set: set, clear: function () { set({}); }, text: function () { return sync(true) && textInput.value; },
                focus: focus, problem: problem, root: root };
    // Existing scripts focus the author by its id; send that to the first empty box.
    textInput.focus = focus;
    root.aylaAuthors = api;
    var initial = null;
    try { initial = root.dataset.initial ? JSON.parse(root.dataset.initial) : null; } catch (e) { initial = null; }
    set(initial);
    // Once the list arrives, picked authors show their book counts too.
    loadOnFile(root.dataset.listUrl).then(function () {
      rowEls().forEach(function (row) { if (row.picked && onFile.byId[row.picked.id]) pick(row, onFile.byId[row.picked.id]); });
    });
    return api;
  }

  window.AylaAuthors = {
    attach: attach,
    get: function (el) { return attach(typeof el === 'string' ? document.getElementById(el) : el); },
    line: line,
    splitName: splitName,
  };
  function attachAll() { document.querySelectorAll('[data-author-fields]').forEach(attach); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', attachAll);
  else attachAll();
})();
