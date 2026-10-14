/* donor-intake.js: the donor on a receiving form. A person is typed as first name, middle name (optional)
   and surname; a school, office or organisation as one name. Past donors are suggested as you type, a
   near-duplicate is caught, and a new donor's contact details are asked for. */
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

  function attach(donors) {
    const el = function (id) { return document.getElementById(id); };
    const type = el('rcvDonorType');
    const first = el('rcvDonorFirst'), middle = el('rcvDonorMiddle'), last = el('rcvDonorLast');
    const org = el('rcvDonor');
    const person = el('donorPersonFields'), orgBox = el('donorOrgFields');
    const box = el('newDonorFields'), hint = el('donorSuggest');
    if (!type || !first || !last || !org || !box || !hint) return null;

    const isPerson = function () { return type.value === 'Individual'; };
    function show() {
      person.classList.toggle('hidden', !isPerson());
      orgBox.classList.toggle('hidden', isPerson());
      // Hidden boxes are not sent, so only one way of naming the donor reaches the server.
      [first, middle, last].forEach(function (i) { i.disabled = !isPerson(); });
      org.disabled = isPerson();
      el('donorOrgLabel').textContent = (TYPE_LABELS[type.value] || 'Organisation') + ' name';
    }
    function fullName() {
      return isPerson()
        ? [first.value, middle.value, last.value].map(function (v) { return v.trim(); }).filter(Boolean).join(' ')
        : org.value.trim();
    }
    function savedMatch() {
      const q = norm(fullName());
      return donors.find(function (d) {
        if (norm(d.name) === q) return true;
        return isPerson() && d.first && norm(d.first) === norm(first.value) && norm(d.last) === norm(last.value)
          && norm(d.middle) === norm(middle.value);
      });
    }
    function fill(d) {
      type.value = d.type || 'Individual';
      show();
      if (isPerson()) {
        first.value = d.first || d.name; middle.value = d.middle || ''; last.value = d.last || '';
      } else {
        org.value = d.name;
      }
      update();
    }

    function update() {
      const q = norm(fullName());
      const known = !!savedMatch();
      const isNew = !!q && !known;
      box.classList.toggle('hidden', !isNew);
      // A saved donor's record is never touched by these.
      box.querySelectorAll('input, select').forEach(function (i) { i.disabled = !isNew; });

      let best = null, bestScore = 0;
      if (isNew && (!isPerson() || (first.value.trim() && last.value.trim()))) {
        donors.forEach(function (d) {
          const other = norm(d.name);
          let score = likeness(q, other);
          if (q.length >= 5 && (other.indexOf(q) !== -1 || q.indexOf(other) !== -1)) score = Math.max(score, 0.86);
          if (score >= 0.85 && score > bestScore) { best = d; bestScore = score; }
        });
      }
      hint.textContent = '';
      hint.classList.toggle('hidden', !best);
      if (best) {
        hint.appendChild(document.createTextNode('Did you mean '));
        const pick = document.createElement('button');
        pick.type = 'button';
        pick.className = 'font-bold underline';
        pick.textContent = best.name;
        pick.addEventListener('click', function () { fill(best); });
        hint.appendChild(pick);
        hint.appendChild(document.createTextNode('? Otherwise a new donor is registered.'));
      }
    }

    // Typing in a name box suggests past donors; picking one fills every box.
    if (window.AylaPicker) {
      const people = function () { return donors.filter(function (d) { return d.type === 'Individual'; }); };
      const asItems = function (list) {
        return list.map(function (d) { return { label: d.name, sub: TYPE_LABELS[d.type] || d.type, donor: d }; });
      };
      [first, last].forEach(function (input) {
        AylaPicker.attach(input, {
          items: function () { return asItems(people()); },
          keepLabel: false,
          onPick: function (it) { fill(it.donor); },
        });
      });
      AylaPicker.attach(org, {
        items: function () { return asItems(donors.filter(function (d) { return d.type !== 'Individual'; })); },
        onPick: function (it) { fill(it.donor); },
      });
    }

    type.addEventListener('change', function () { show(); update(); });
    [first, middle, last, org].forEach(function (i) {
      i.addEventListener('input', update);
      i.addEventListener('change', update);
    });
    show();
    update();
    return { update: update };
  }

  window.AylaDonorIntake = { attach: attach };
})();
