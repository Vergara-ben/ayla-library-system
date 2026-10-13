/* donor-intake.js: on the receiving forms, a new donor's contact details, and "did you mean" for a near-duplicate name. */
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

  function attach(input, names) {
    const box = document.getElementById('newDonorFields');
    const hint = document.getElementById('donorSuggest');
    if (!input || !box || !hint) return null;

    function update() {
      const q = norm(input.value);
      const known = names.some(function (n) { return norm(n) === q; });
      const isNew = !!q && !known;
      box.classList.toggle('hidden', !isNew);
      // Hidden fields are not sent, so a saved donor's record is never touched by them.
      box.querySelectorAll('input, select').forEach(function (el) { el.disabled = !isNew; });

      let best = null;
      let bestScore = 0;
      if (isNew) {
        names.forEach(function (n) {
          const other = norm(n);
          let score = likeness(q, other);
          if (q.length >= 5 && (other.indexOf(q) !== -1 || q.indexOf(other) !== -1)) score = Math.max(score, 0.86);
          if (score >= 0.85 && score > bestScore) { best = n; bestScore = score; }
        });
      }
      hint.textContent = '';
      hint.classList.toggle('hidden', !best);
      if (best) {
        hint.appendChild(document.createTextNode('Did you mean '));
        const pick = document.createElement('button');
        pick.type = 'button';
        pick.className = 'font-bold underline';
        pick.textContent = best;
        pick.addEventListener('click', function () { input.value = best; update(); });
        hint.appendChild(pick);
        hint.appendChild(document.createTextNode('? Otherwise a new donor is registered.'));
      }
    }

    input.addEventListener('input', update);
    input.addEventListener('change', update);
    update();
    return { update: update };
  }

  window.AylaDonorIntake = { attach: attach };
})();
