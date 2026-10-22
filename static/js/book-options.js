/* The catalogue for a book picker, sent once as a short list and turned into <option>s here,
   instead of an option per copy in the page itself. Rows are [id, title, copy, author, isbn]. */
(function () {
  'use strict';
  var rows = null;

  function list() {
    if (rows === null) {
      var el = document.getElementById('bookListData');
      try { rows = el ? JSON.parse(el.textContent || '[]') : []; } catch (e) { rows = []; }
    }
    return rows;
  }

  window.AylaBookOptions = {
    // One option per title and author, carrying what the picker searches.
    titles: function (select) {
      var seen = {};
      var frag = document.createDocumentFragment();
      list().forEach(function (r) {
        var key = (r[1] + '|' + r[3]).toLowerCase();
        if (seen[key]) return;
        seen[key] = true;
        var o = new Option(r[1] + ' — ' + r[3], r[0]);
        o.dataset.title = r[1];
        o.dataset.author = r[3];
        o.dataset.isbn = r[4] || '';
        frag.appendChild(o);
      });
      select.appendChild(frag);
    },
    // One option per copy.
    copies: function (select) {
      var frag = document.createDocumentFragment();
      list().forEach(function (r) {
        frag.appendChild(new Option(r[1] + (r[2] ? ' · Copy ' + r[2] : '') + ' — ' + r[3], r[0]));
      });
      select.appendChild(frag);
    },
  };
})();
