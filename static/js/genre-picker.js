/* genre-picker.js: a genre comes from the library's list; a new one is added only on purpose. */
(function () {
  'use strict';

  function attach(input, opts) {
    opts = opts || {};
    // Each genre is a name, or {label, sub, search} with the finer wording its books use.
    var genres = (opts.genres || []).map(function (g) { return typeof g === 'string' ? { label: g } : g; });
    var byLower = {};
    genres.forEach(function (g) { byLower[g.label.toLowerCase()] = g.label; });
    var flag = opts.flag || null;
    var note = opts.note || null;
    var isNew = false;

    function setNew(on) {
      isNew = !!on;
      if (flag) flag.value = isNew ? '1' : '';
      if (note) {
        note.textContent = isNew
          ? 'New genre: “' + input.value.trim() + '” is added to the list when you save.' : '';
        note.classList.toggle('hidden', !isNew);
      }
      if (opts.onChange) opts.onChange();
    }

    // A known genre typed in any case is taken as the list spells it.
    function settle() {
      var known = byLower[input.value.trim().toLowerCase()];
      if (known && input.value !== known) input.value = known;
      if (known && isNew) setNew(false);
      if (opts.onChange) opts.onChange();
    }

    AylaPicker.attach(input, {
      items: genres,
      newLabel: function (q) {
        return q ? '+ Add “' + q + '” as a new genre' : '+ New genre — type its name first';
      },
      onPick: function () { setNew(false); },
      onNew: function (q) { if (q) setNew(true); else input.focus(); },
      // Typing again means the earlier choice no longer stands.
      onType: function () { if (isNew) setNew(false); },
    });
    input.addEventListener('change', settle);
    input.addEventListener('blur', function () { setTimeout(settle, 130); });

    return {
      // Why the typed genre cannot be saved yet, or ''.
      problem: function () {
        var v = input.value.trim();
        if (!v) return 'Choose a genre.';
        if (byLower[v.toLowerCase()] || isNew) return '';
        return '“' + v + '” is not one of the library’s genres. Pick one from the list, '
          + 'or choose “+ Add as a new genre”.';
      },
      known: function (v) { return !!byLower[String(v || '').trim().toLowerCase()]; },
      isNew: function () { return isNew; },
      set: function (value) { input.value = value || ''; setNew(false); settle(); },
      reset: function () { input.value = ''; setNew(false); },
    };
  }

  window.AylaGenrePicker = { attach: attach };
})();
