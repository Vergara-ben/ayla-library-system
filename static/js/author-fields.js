/* The author boxes: one row per name, a person's first, middle and surname or an organization's
   name, and what they did on the book. Two hidden inputs carry them: the names as JSON, and the
   author line built from them. */
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
    return c.kind === 'Organization' ? c.name : [c.first, c.middle, c.last].filter(Boolean).join(' ');
  }

  // The same author line the server builds: "A & B", "A, B & C", "A, B et al.".
  function join(names, others) {
    if (others) return names.join(', ') + ' et al.';
    if (names.length > 1) return names.slice(0, -1).join(', ') + ' & ' + names[names.length - 1];
    return names[0] || '';
  }
  function line(data) {
    var authors = data.credits.filter(function (c) { return c.role === 'Author'; }).map(shown);
    if (authors.length) return join(authors, data.others);
    if (data.unknown || !data.credits.length) return data.unknown ? (data.others ? 'Various Authors' : 'Unknown') : '';
    var role = data.credits[0].role;
    return join(data.credits.filter(function (c) { return c.role === role; }).map(shown), data.others) + ' (' + role + ')';
  }

  function attach(root) {
    if (!root || root.aylaAuthors) return root && root.aylaAuthors;
    var rows = root.querySelector('[data-af-rows]');
    var template = root.querySelector('[data-af-template]');
    var unknownBox = root.querySelector('[data-af-unknown]');
    var othersBox = root.querySelector('[data-af-others]');
    var textInput = root.querySelector('[data-af-text]');
    var jsonInput = root.querySelector('[data-af-json]');

    function rowEls() { return Array.prototype.slice.call(rows.querySelectorAll('[data-af-row]')); }
    function box(row, name) { return row.querySelector('[data-af="' + name + '"]'); }

    function setKind(row, kind) {
      row.dataset.kind = kind;
      row.querySelectorAll('[data-af-kind]').forEach(function (b) {
        var on = b.dataset.afKind === kind;
        b.classList.toggle('is-on', on);
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
      row.querySelector('[data-af-person]').classList.toggle('hidden', kind === 'Organization');
      row.querySelector('[data-af-org]').classList.toggle('hidden', kind !== 'Organization');
    }

    function addRow(c, focus) {
      c = c || {};
      var row = template.content.firstElementChild.cloneNode(true);
      row.querySelector('[data-af-role]').value = c.role || 'Author';
      ['first', 'middle', 'last', 'name'].forEach(function (k) { box(row, k).value = c[k] || ''; });
      setKind(row, c.kind === 'Organization' ? 'Organization' : 'Person');
      rows.appendChild(row);
      if (focus) (row.dataset.kind === 'Organization' ? box(row, 'name') : box(row, 'first')).focus();
      return row;
    }

    function read() {
      var credits = [];
      rowEls().forEach(function (row) {
        var role = row.querySelector('[data-af-role]').value;
        if (unknownBox.checked && role === 'Author') return;    // set aside while "unknown" is ticked
        var org = row.dataset.kind === 'Organization';
        var c = { kind: org ? 'Organization' : 'Person', role: role,
                  first: org ? '' : tidy(box(row, 'first').value), middle: org ? '' : tidy(box(row, 'middle').value),
                  last: org ? '' : tidy(box(row, 'last').value), name: org ? tidy(box(row, 'name').value) : '' };
        if (c.first || c.middle || c.last || c.name) credits.push(c);
      });
      return { credits: credits, unknown: unknownBox.checked, others: othersBox.checked };
    }

    function problem() {
      var data = read();
      var half = data.credits.filter(function (c) { return c.kind === 'Person' && !c.last; })[0];
      if (half) return 'Type the surname of ' + shown(half) + '. A writer known by one name goes in Surname.';
      if (!data.credits.length && !data.unknown) return 'Type the author, or tick "Author unknown" if the book names none.';
      return '';
    }

    function sync(quiet) {
      var data = read();
      rowEls().forEach(function (row) {
        var off = unknownBox.checked && row.querySelector('[data-af-role]').value === 'Author';
        row.classList.toggle('is-off', off);
        row.querySelectorAll('input').forEach(function (i) { i.disabled = off; });
        row.querySelector('[data-af-remove]').classList.toggle('invisible', rowEls().length < 2);
      });
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
      if (!input || !row || row.dataset.kind === 'Organization') return;
      var text = tidy((e.clipboardData || window.clipboardData).getData('text'));
      var names = text.split(/\s*(?:;|&|\band\b|\/)\s*/).filter(Boolean);
      var empty = ['first', 'middle', 'last'].every(function (k) { return k === input.dataset.af || !box(row, k).value.trim(); });
      if (!empty || (names.length < 2 && text.split(' ').length < 2 && text.indexOf(',') === -1)) return;
      e.preventDefault();
      names.forEach(function (n, i) {
        var parts = splitName(n);
        var target = i === 0 ? row : addRow({ role: row.querySelector('[data-af-role]').value });
        ['first', 'middle', 'last'].forEach(function (k) { box(target, k).value = parts[k]; });
      });
      changed();
    });
    rows.addEventListener('input', function () { sync(); });
    rows.addEventListener('change', changed);
    rows.addEventListener('click', function (e) {
      var kind = e.target.closest('[data-af-kind]');
      var remove = e.target.closest('[data-af-remove]');
      var row = e.target.closest('[data-af-row]');
      if (kind && row) {
        setKind(row, kind.dataset.afKind);
        (kind.dataset.afKind === 'Organization' ? box(row, 'name') : box(row, 'first')).focus();
        changed();
      } else if (remove && row && rowEls().length > 1) {
        row.remove();
        changed();
      }
    });
    root.querySelector('[data-af-add]').addEventListener('click', function () {
      // A second name is most often a co-author; after an author line is set aside, an illustrator.
      addRow({ role: unknownBox.checked ? 'Illustrator' : 'Author' }, true);
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
    // The first box still to fill: an organization's name, or a person's first name or surname.
    function focus() {
      var boxes = [];
      rowEls().forEach(function (r) {
        if (r.classList.contains('is-off')) return;
        if (r.dataset.kind === 'Organization') boxes.push(box(r, 'name'));
        else boxes.push(box(r, 'first'), box(r, 'last'));
      });
      var target = boxes.filter(function (i) { return !i.value.trim(); })[0] || boxes[0] || unknownBox;
      target.focus();
    }

    var api = { get: read, set: set, clear: function () { set({}); }, text: function () { return sync(true) && textInput.value; },
                focus: focus, problem: problem, root: root };
    // Existing scripts focus the author by its id; send that to the first empty box.
    textInput.focus = focus;
    root.aylaAuthors = api;
    var initial = null;
    try { initial = root.dataset.initial ? JSON.parse(root.dataset.initial) : null; } catch (e) { initial = null; }
    set(initial);
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
