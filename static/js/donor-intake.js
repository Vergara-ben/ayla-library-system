/* donor-intake.js: the donor on a receiving form, as one of two: a new donor typed in full (a person's first,
   middle and surname, or one name), or a donor already on file, found by name or number and picked. A new
   donor whose name is already on file is offered the saved one, so nobody is registered twice. */
(function () {
  'use strict';

  function norm(text) {
    return String(text || '').toLowerCase().replace(/[^a-z0-9 ]/g, ' ').replace(/\s+/g, ' ').trim();
  }

  // 1 for the same text, falling towards 0 as more letters differ.
  function likeness(a, b) {
    if (!a || !b) return 0;
    const prev = new Array(b.length + 1);
    for (let j = 0; j <= b.length; j++) prev[j] = j;
    for (let i = 1; i <= a.length; i++) {
      let diag = prev[0];
      prev[0] = i;
      for (let j = 1; j <= b.length; j++) {
        const keep = prev[j];
        prev[j] = Math.min(prev[j] + 1, prev[j - 1] + 1, diag + (a[i - 1] === b[j - 1] ? 0 : 1));
        diag = keep;
      }
    }
    return 1 - prev[b.length] / Math.max(a.length, b.length);
  }

  const TYPE_LABELS = { Individual: 'Person', Organization: 'Organisation', School: 'School',
                        Government: 'Government office' };
  const TYPE_ICONS = { Individual: 'fa-user', Organization: 'fa-people-group', School: 'fa-school',
                       Government: 'fa-landmark' };

  function attach(donors) {
    const el = function (id) { return document.getElementById(id); };
    const root = el('donorIntake');
    if (!root) return null;
    const accent = root.dataset.accent || 'cyan';
    const type = el('rcvDonorType');
    const first = el('rcvDonorFirst'), middle = el('rcvDonorMiddle'), last = el('rcvDonorLast');
    const org = el('rcvDonor');
    const extras = [el('rcvDonorContact'), el('rcvDonorEmail'), el('rcvDonorAddress')];
    const hint = el('donorSuggest'), search = el('donorSearch'), donorId = el('rcvDonorId');
    const modeButtons = root.querySelectorAll('[data-donor-mode]');
    const typeButtons = root.querySelectorAll('[data-donor-type]');
    const ON_MODE = ['bg-' + accent + '-700/30', 'text-' + accent + '-400', 'border', 'border-' + accent + '-800/50'];
    const OFF_MODE = ['text-slate-400', 'hover:text-slate-200', 'hover:bg-slate-800/40'];
    const ON_TYPE = ['border-' + accent + '-700', 'text-' + accent + '-400', 'bg-' + accent + '-500/10'];
    const OFF_TYPE = ['border-slate-700', 'text-slate-400', 'hover:text-slate-200'];
    let mode = 'new', picked = null;

    function paint(button, on, onClasses, offClasses) {
      onClasses.forEach(function (c) { button.classList.toggle(c, on); });
      offClasses.forEach(function (c) { button.classList.toggle(c, !on); });
      button.setAttribute('aria-selected', on ? 'true' : 'false');
    }
    const isPerson = function () { return type.value === 'Individual'; };

    // Only the boxes in use are sent, so the server reads one way of naming the donor.
    function showType() {
      const asNew = mode === 'new';
      el('donorPersonFields').classList.toggle('hidden', !isPerson());
      el('donorOrgFields').classList.toggle('hidden', isPerson());
      [first, middle, last].forEach(function (i) { i.disabled = !asNew || !isPerson(); });
      org.disabled = !asNew || isPerson();
      el('donorOrgLabel').textContent = (TYPE_LABELS[type.value] || 'Organisation') + ' name';
      typeButtons.forEach(function (b) { paint(b, b.dataset.donorType === type.value, ON_TYPE, OFF_TYPE); });
    }

    function setMode(next, quiet) {
      mode = next;
      el('donorNewPanel').classList.toggle('hidden', mode !== 'new');
      el('donorSavedPanel').classList.toggle('hidden', mode !== 'saved');
      type.disabled = mode !== 'new';
      extras.forEach(function (i) { if (i) i.disabled = mode !== 'new'; });
      donorId.disabled = mode !== 'saved';
      modeButtons.forEach(function (b) { paint(b, b.dataset.donorMode === mode, ON_MODE, OFF_MODE); });
      showType();
      update();
      if (quiet) return;
      if (mode === 'saved' && !picked) setTimeout(function () { search.focus(); }, 0);
      if (mode === 'new') setTimeout(function () { (isPerson() ? first : org).focus(); }, 0);
    }

    function pick(d) {
      picked = d;
      donorId.value = d.id;
      el('donorCardName').textContent = d.name;
      el('donorCardType').textContent = TYPE_LABELS[d.type] || d.type;
      el('donorCardIcon').className = 'fa-solid ' + (TYPE_ICONS[d.type] || 'fa-user');
      el('donorCardContact').textContent = [d.contact, d.email, d.address].filter(Boolean).join(' · ') || 'No contact details on file';
      el('donorCardHistory').textContent = d.gifts
        ? 'Has given ' + d.gifts + ' time' + (d.gifts === 1 ? '' : 's') + (d.last ? ', last on ' + d.last : '')
        : 'Nothing received from them yet';
      el('donorSearchWrap').classList.add('hidden');
      el('donorCard').classList.remove('hidden');
    }
    function unpick() {
      picked = null;
      donorId.value = '';
      el('donorCard').classList.add('hidden');
      el('donorSearchWrap').classList.remove('hidden');
      search.value = '';
      search.focus();
    }

    function fullName() {
      return isPerson()
        ? [first.value, middle.value, last.value].map(function (v) { return v.trim(); }).filter(Boolean).join(' ')
        : org.value.trim();
    }
    function savedMatch() {
      const q = norm(fullName());
      return donors.find(function (d) {
        if (q && norm(d.name) === q) return true;
        return isPerson() && d.first && norm(d.first) === norm(first.value) && norm(d.last) === norm(last.value)
          && norm(d.middle) === norm(middle.value);
      });
    }

    // A new donor who is already on file, or nearly so: offer the saved one.
    function update() {
      hint.textContent = '';
      hint.classList.add('hidden');
      if (mode !== 'new') return;
      const q = norm(fullName());
      if (!q || (isPerson() && !(first.value.trim() && last.value.trim()))) return;
      let best = savedMatch(), exact = !!best;
      if (!best) {
        let bestScore = 0;
        donors.forEach(function (d) {
          const other = norm(d.name);
          let score = likeness(q, other);
          if (q.length >= 5 && (other.indexOf(q) !== -1 || q.indexOf(other) !== -1)) score = Math.max(score, 0.86);
          if (score >= 0.85 && score > bestScore) { best = d; bestScore = score; }
        });
      }
      if (!best) return;
      hint.appendChild(document.createTextNode(exact ? best.name + ' is already on file. '
                                                     : 'Did you mean ' + best.name + '? '));
      const use = document.createElement('button');
      use.type = 'button';
      use.className = 'font-bold underline';
      use.textContent = 'Use the saved donor';
      use.addEventListener('click', function () { setMode('saved'); pick(best); });
      hint.appendChild(use);
      if (!exact) hint.appendChild(document.createTextNode(' Otherwise a new donor is registered.'));
      hint.classList.remove('hidden');
    }

    if (window.AylaPicker) {
      AylaPicker.attach(search, {
        items: function () {
          return donors.map(function (d) {
            return { label: d.name, donor: d, search: (d.contact || '') + ' ' + (d.email || ''),
                     sub: [TYPE_LABELS[d.type] || d.type, d.contact].filter(Boolean).join(' · ')
                          + (d.gifts ? ' · gave ' + d.gifts + (d.gifts === 1 ? ' time' : ' times') : '') };
          });
        },
        keepLabel: false,
        onPick: function (it) { pick(it.donor); },
      });
    }

    modeButtons.forEach(function (b) { b.addEventListener('click', function () { setMode(b.dataset.donorMode); }); });
    typeButtons.forEach(function (b) {
      b.addEventListener('click', function () { type.value = b.dataset.donorType; showType(); update(); });
    });
    el('donorCardChange').addEventListener('click', unpick);
    [first, middle, last, org].forEach(function (i) { i.addEventListener('input', update); });
    setMode('new', true);

    const api = {
      update: update,
      setMode: setMode,
      // What stops the donation from being received, or null.
      // Who the donation is from, for the confirmation.
      label: function () { return mode === 'saved' ? (picked ? picked.name : '') : fullName(); },
      problem: function () {
        return mode === 'saved' && !picked ? 'Choose the donor from the list, or switch to New donor.' : null;
      },
    };
    window.AylaDonorIntake.current = api;
    return api;
  }

  window.AylaDonorIntake = { attach: attach, current: null };
})();
