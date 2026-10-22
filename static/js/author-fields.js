/* The author boxes: one row per author, first, middle and surname. Two hidden inputs carry them:
   the names as JSON, and the author line built from them. */
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

  // The same author line the server builds: "A & B", "A, B & C", "A, B et al.".
  function line(data) {
    var names = data.credits.map(shown);
    if (!names.length) return data.unknown ? (data.others ? 'Various Authors' : 'Unknown') : '';
    if (data.others) return names.join(', ') + ' et al.';
    if (names.length > 1) return names.slice(0, -1).join(', ') + ' & ' + names[names.length - 1];
    return names[0];
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

    function rowEls() { return Array.prototype.slice.call(rows.querySelectorAll('[data-af-row]')); }
    function box(row, name) { return row.querySelector('[data-af="' + name + '"]'); }

    function addRow(c, focus) {
      c = c || {};
      var row = template.content.firstElementChild.cloneNode(true);
      ['first', 'middle', 'last'].forEach(function (k) { box(row, k).value = c[k] || ''; });
      rows.appendChild(row);
      if (focus) box(row, 'first').focus();
      return row;
    }

    function read() {
      var credits = [];
      // The names are set aside while "Author unknown" is ticked.
      if (!unknownBox.checked) {
        rowEls().forEach(function (row) {
          var c = { first: tidy(box(row, 'first').value), middle: tidy(box(row, 'middle').value),
                    last: tidy(box(row, 'last').value) };
          if (c.first || c.middle || c.last) credits.push(c);
        });
      }
      return { credits: credits, unknown: unknownBox.checked, others: othersBox.checked };
    }

    function problem() {
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
        row.querySelectorAll('input').forEach(function (i) { i.disabled = off; });
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
        var target = i === 0 ? row : addRow();
        ['first', 'middle', 'last'].forEach(function (k) { box(target, k).value = parts[k]; });
      });
      changed();
    });
    rows.addEventListener('input', function () { sync(); });
    rows.addEventListener('change', changed);
    rows.addEventListener('click', function (e) {
      var remove = e.target.closest('[data-af-remove]');
      var row = e.target.closest('[data-af-row]');
      if (remove && row && rowEls().length > 1) {
        row.remove();
        changed();
      }
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
    // The first box still to fill: a first name or a surname.
    function focus() {
      var boxes = [];
      rowEls().forEach(function (r) {
        if (!r.classList.contains('is-off')) boxes.push(box(r, 'first'), box(r, 'last'));
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
