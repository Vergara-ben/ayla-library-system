/* inventory-checks.js: the inventory's data-entry rules, checked as you type. A box marked data-check="..."
   shows what is wrong under it once you leave it, and a form marked data-checked-form will not send until
   every such box is right. The server checks the same rules again (library/inventory_rules.py). */
(function () {
  'use strict';

  function tidy(v) { return String(v || '').replace(/\s+/g, ' ').trim(); }

  function today() {
    const d = new Date();
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  }

  const RULES = {
    reason: function (v) {
      const t = tidy(v);
      if (!t) return 'Give the reason.';
      const words = t.split(' ').filter(function (w) { return /[A-Za-z]{2,}/.test(w); });
      const letters = t.toLowerCase().replace(/[^a-z]/g, '');
      if (t.length < 6 || words.length < 2 || !/[A-Za-z]{3,}/.test(t) || new Set(letters).size < 4) {
        return 'Write the reason in a few words, so the history still makes sense later.';
      }
      return '';
    },
    supplier: function (v) {
      return tidy(v).length < 2 ? 'Type who supplied the shipment.' : '';
    },
    donated: function (v) {
      if (!v) return '';
      if (v > today()) return 'The donation date cannot be in the future.';
      if (v < '1950-01-01') return 'Check the donation date: it is too far back.';
      return '';
    },
    price: function (v) {
      const t = tidy(v).replace(/,/g, '').replace(/^(₱|php|p)\s*/i, '');
      if (!t) return '';
      const n = Number(t);
      if (!isFinite(n)) return 'The price is not a number.';
      if (n < 1) return 'A price must be at least ₱1: it is what a patron pays if the copy is lost.';
      if (n > 100000) return 'The price must be at most ₱100,000.';
      return '';
    },
  };

  function hidden(input) { return input.disabled || !!input.closest('.hidden'); }

  function show(input, message) {
    let note = input.nextElementSibling;
    if (!note || !note.classList || !note.classList.contains('field-problem')) {
      note = document.createElement('p');
      note.className = 'field-problem text-[11px] text-rose-400 mt-1';
      note.setAttribute('role', 'alert');
      input.insertAdjacentElement('afterend', note);
    }
    note.textContent = message || '';
    note.hidden = !message;
    input.classList.toggle('!border-rose-500', !!message);
    input.setAttribute('aria-invalid', message ? 'true' : 'false');
  }

  function check(input) {
    const rule = RULES[input.dataset.check];
    const message = !rule || hidden(input) ? '' : rule(input.value, input);
    show(input, message);
    return message;
  }

  function attach(root) {
    root.querySelectorAll('[data-check]').forEach(function (input) {
      if (input.dataset.checkReady) return;
      input.dataset.checkReady = '1';
      if (input.dataset.check === 'donated') input.max = today();
      input.addEventListener('input', function () { if (input.dataset.touched) check(input); });
      input.addEventListener('change', function () { input.dataset.touched = '1'; check(input); });
      input.addEventListener('blur', function () { input.dataset.touched = '1'; check(input); });
    });
  }

  // Every marked box in the form, at once; the first wrong one gets the cursor.
  function form(f) {
    let first = null;
    f.querySelectorAll('[data-check]').forEach(function (input) {
      input.dataset.touched = '1';
      if (check(input) && !first) first = input;
    });
    if (first) first.focus();
    return !first;
  }

  // Ahead of any confirmation the form asks for: nothing is confirmed that would be refused.
  window.addEventListener('submit', function (e) {
    const f = e.target;
    if (f && f.matches && f.matches('[data-checked-form]') && !form(f)) {
      e.preventDefault();
      e.stopImmediatePropagation();
      e.stopPropagation();
    }
  }, true);

  document.addEventListener('DOMContentLoaded', function () { attach(document); });

  window.AylaChecks = { attach: attach, form: form, check: check, rules: RULES };
})();
