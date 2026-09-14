/* Toasts and confirmations, in place of the browser's own dialogs. */
(function () {
  'use strict';

  var STACK_ID = 'ayla-toast-stack';
  var TOAST_MS = 4200;

  function el(tag, cls, html) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (html != null) node.innerHTML = html;
    return node;
  }

  function esc(text) {
    return String(text == null ? '' : text).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  // A message written for alert() carries no severity flag, so it is guessed from the wording.
  function toneOf(message) {
    var t = String(message || '').toLowerCase();
    if (/fail|error|could not|cannot|unable|invalid|required|not found|too many|denied/.test(t)) {
      return 'error';
    }
    if (/success|saved|added|updated|removed|deleted|complete|done/.test(t)) {
      return 'success';
    }
    return 'info';
  }

  function whenReady(fn) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', fn, { once: true });
    } else {
      fn();
    }
  }

  function stack() {
    // Wait for the body before showing a toast.
    if (!document.body) return null;
    var node = document.getElementById(STACK_ID);
    if (node) return node;
    node = el('div', null);
    node.id = STACK_ID;
    node.setAttribute('role', 'status');
    node.setAttribute('aria-live', 'polite');
    node.style.cssText = 'position:fixed;z-index:9999;right:16px;bottom:16px;display:flex;'
      + 'flex-direction:column;gap:8px;max-width:min(420px,calc(100vw - 32px));'
      + 'pointer-events:none;';
    document.body.appendChild(node);
    return node;
  }

  var TONES = {
    info: { border: '#334155', accent: '#38bdf8', icon: 'fa-circle-info' },
    success: { border: '#14532d', accent: '#4ade80', icon: 'fa-circle-check' },
    error: { border: '#7f1d1d', accent: '#f87171', icon: 'fa-circle-exclamation' }
  };

  function toast(message, kind) {
    if (message == null || String(message).trim() === '') return;
    var tone = TONES[kind] || TONES[toneOf(message)];
    var card = el('div', null);
    card.style.cssText = 'pointer-events:auto;display:flex;align-items:flex-start;gap:10px;'
      + 'padding:11px 13px;border-radius:10px;font-size:12.5px;line-height:1.5;'
      + 'background:#0f172a;color:#e2e8f0;border:1px solid ' + tone.border + ';'
      + 'border-left:3px solid ' + tone.accent + ';'
      + 'box-shadow:0 8px 24px rgba(0,0,0,.4);opacity:0;transform:translateY(6px);'
      + 'transition:opacity .18s ease,transform .18s ease;';
    card.innerHTML = '<i class="fa-solid ' + tone.icon + '" style="color:' + tone.accent
      + ';margin-top:2px;font-size:12px"></i>'
      // Keep line breaks.
      + '<span style="white-space:pre-wrap">' + esc(message) + '</span>';

    var host = stack();
    if (!host) {                       // too early: show it once the body exists
      whenReady(function () { toast(message, kind); });
      return;
    }
    // Carry the message across a redirect.
    card.dataset.aylaShownAt = String(Date.now());
    card.dataset.aylaMessage = String(message);
    card.dataset.aylaKind = kind || toneOf(message);
    host.appendChild(card);
    requestAnimationFrame(function () {
      card.style.opacity = '1';
      card.style.transform = 'translateY(0)';
    });

    var gone = false;
    function dismiss() {
      if (gone) return;
      gone = true;
      card.style.opacity = '0';
      card.style.transform = 'translateY(6px)';
      setTimeout(function () { if (card.parentNode) card.parentNode.removeChild(card); }, 200);
    }
    card.addEventListener('click', dismiss);
    // An error stays until it is read; the rest clear themselves.
    if (tone !== TONES.error) setTimeout(dismiss, TOAST_MS);
    else setTimeout(dismiss, TOAST_MS * 2.5);
    return dismiss;
  }

  /* A real confirmation. */
  function confirm(message, opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      var danger = opts.danger !== false && /delete|remove|clear|discard|write off|deactivate|reject/i
        .test(String(opts.title || '') + ' ' + String(message || ''));
      var accent = danger ? '#e11d48' : '#0e7490';
      var previous = document.activeElement;

      var overlay = el('div', null);
      overlay.style.cssText = 'position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,.6);'
        + 'display:flex;align-items:center;justify-content:center;padding:16px;';
      overlay.setAttribute('role', 'dialog');
      overlay.setAttribute('aria-modal', 'true');

      var card = el('div', null);
      card.style.cssText = 'width:100%;max-width:440px;background:#101826;color:#e2e8f0;'
        + 'border:1px solid #334155;border-radius:14px;padding:22px;'
        + 'box-shadow:0 18px 48px rgba(0,0,0,.55);';
      card.innerHTML =
        '<h3 style="margin:0 0 8px;font-size:15px;font-weight:700;letter-spacing:.01em">'
        + esc(opts.title || (danger ? 'Please confirm' : 'Confirm')) + '</h3>'
        + '<p style="margin:0;font-size:12.5px;line-height:1.6;color:#cbd5e1;white-space:pre-wrap">'
        + esc(message) + '</p>'
        + (opts.detail
            ? '<p style="margin:10px 0 0;font-size:11px;line-height:1.6;color:#94a3b8;'
              + 'white-space:pre-wrap">' + esc(opts.detail) + '</p>'
            : '')
        + '<div style="display:flex;justify-content:flex-end;gap:10px;margin-top:20px">'
        + '<button type="button" data-role="cancel" style="padding:9px 18px;border-radius:8px;'
        + 'border:1px solid #475569;background:transparent;color:#cbd5e1;font-size:12px;'
        + 'font-weight:600;cursor:pointer">' + esc(opts.cancelLabel || 'Cancel') + '</button>'
        + '<button type="button" data-role="ok" style="padding:9px 18px;border-radius:8px;'
        + 'border:1px solid ' + accent + ';background:' + accent + ';color:#fff;font-size:12px;'
        + 'font-weight:700;cursor:pointer">'
        + esc(opts.confirmLabel || (danger ? 'Delete' : 'Confirm')) + '</button>'
        + '</div>';

      overlay.appendChild(card);
      document.body.appendChild(overlay);

      var ok = card.querySelector('[data-role="ok"]');
      var cancel = card.querySelector('[data-role="cancel"]');

      function close(answer) {
        document.removeEventListener('keydown', onKey, true);
        if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
        // Restore focus.
        if (previous && previous.focus) { try { previous.focus(); } catch (e) {} }
        resolve(answer);
      }

      function onKey(e) {
        if (e.key === 'Escape') { e.preventDefault(); close(false); }
        else if (e.key === 'Enter' && document.activeElement !== cancel) {
          e.preventDefault(); close(true);
        } else if (e.key === 'Tab') {
          // Two buttons, so the trap is just a toggle.
          e.preventDefault();
          (document.activeElement === ok ? cancel : ok).focus();
        }
      }

      ok.addEventListener('click', function () { close(true); });
      cancel.addEventListener('click', function () { close(false); });
      overlay.addEventListener('mousedown', function (e) {
        if (e.target === overlay) close(false);      // clicking away means no
      });
      document.addEventListener('keydown', onKey, true);
      // Focus Cancel on destructive prompts.
      (danger ? cancel : ok).focus();
    });
  }

  /* A one-field prompt. */
  function prompt(message, initial, opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      var previous = document.activeElement;
      var overlay = el('div', null);
      overlay.style.cssText = 'position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,.6);'
        + 'display:flex;align-items:center;justify-content:center;padding:16px;';
      overlay.setAttribute('role', 'dialog');
      overlay.setAttribute('aria-modal', 'true');

      var card = el('div', null);
      card.style.cssText = 'width:100%;max-width:460px;background:#101826;color:#e2e8f0;'
        + 'border:1px solid #334155;border-radius:14px;padding:22px;'
        + 'box-shadow:0 18px 48px rgba(0,0,0,.55);';
      card.innerHTML =
        '<h3 style="margin:0 0 8px;font-size:15px;font-weight:700">'
        + esc(opts.title || 'Enter a value') + '</h3>'
        + '<p style="margin:0 0 12px;font-size:12.5px;line-height:1.6;color:#cbd5e1;'
        + 'white-space:pre-wrap">' + esc(message) + '</p>'
        + '<' + (opts.multiline ? 'textarea rows="3"' : 'input type="text"')
        + ' data-role="field" style="width:100%;box-sizing:border-box;padding:9px 11px;'
        + 'border-radius:8px;border:1px solid #475569;background:#0b1220;color:#e2e8f0;'
        + 'font-size:12.5px;font-family:inherit"'
        + (opts.multiline ? '></textarea>' : '>')
        + '<div style="display:flex;justify-content:flex-end;gap:10px;margin-top:18px">'
        + '<button type="button" data-role="cancel" style="padding:9px 18px;border-radius:8px;'
        + 'border:1px solid #475569;background:transparent;color:#cbd5e1;font-size:12px;'
        + 'font-weight:600;cursor:pointer">Cancel</button>'
        + '<button type="button" data-role="ok" style="padding:9px 18px;border-radius:8px;'
        + 'border:1px solid #0e7490;background:#0e7490;color:#fff;font-size:12px;'
        + 'font-weight:700;cursor:pointer">' + esc(opts.confirmLabel || 'OK') + '</button>'
        + '</div>';

      overlay.appendChild(card);
      document.body.appendChild(overlay);

      var field = card.querySelector('[data-role="field"]');
      field.value = initial == null ? '' : String(initial);

      function close(answer) {
        document.removeEventListener('keydown', onKey, true);
        if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
        if (previous && previous.focus) { try { previous.focus(); } catch (e) {} }
        resolve(answer);
      }
      function onKey(e) {
        if (e.key === 'Escape') { e.preventDefault(); close(null); }
        else if (e.key === 'Enter' && !opts.multiline) { e.preventDefault(); close(field.value); }
      }
      card.querySelector('[data-role="ok"]').addEventListener('click', function () {
        close(field.value);
      });
      card.querySelector('[data-role="cancel"]').addEventListener('click', function () {
        close(null);
      });
      overlay.addEventListener('mousedown', function (e) {
        if (e.target === overlay) close(null);
      });
      document.addEventListener('keydown', onKey, true);
      field.focus();
      field.select();
    });
  }

  window.AylaDialog = { toast: toast, confirm: confirm, prompt: prompt };

  // Every existing alert() becomes a toast, with no call site touched.
  var nativeAlert = window.alert.bind(window);
  window.alert = function (message) { toast(message); };
  window.AylaDialog.nativeAlert = nativeAlert;

  /* Carry a just-raised message across a redirect. */
  var HANDOFF = 'ayla.toast.handoff';
  var HANDOFF_GRACE_MS = 2500;

  window.addEventListener('beforeunload', function () {
    try {
      var now = Date.now();
      var carry = [];
      var host = document.getElementById(STACK_ID);
      if (host) {
        Array.prototype.forEach.call(host.children, function (card) {
          var at = Number(card.dataset.aylaShownAt || 0);
          // Only the ones too new to have been read.
          if (now - at <= HANDOFF_GRACE_MS) {
            carry.push({ m: card.dataset.aylaMessage, k: card.dataset.aylaKind });
          }
        });
      }
      if (carry.length) sessionStorage.setItem(HANDOFF, JSON.stringify(carry));
      else sessionStorage.removeItem(HANDOFF);
    } catch (e) { /* private mode: the message is simply lost, as before */ }
  });

  try {
    var carried = JSON.parse(sessionStorage.getItem(HANDOFF) || '[]');
    sessionStorage.removeItem(HANDOFF);
    if (Array.isArray(carried) && carried.length) {
      // Show the carried message once the page loads.
      whenReady(function () {
        carried.forEach(function (item) { toast(item.m, item.k); });
      });
    }
  } catch (e) { /* nothing to carry */ }

  // Declarative form confirmation: data-confirm="Delete this?" on the <form>.
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form || !form.getAttribute) return;
    var message = form.getAttribute('data-confirm');
    if (!message || form.dataset.aylaConfirmed === '1') return;
    e.preventDefault();
    e.stopPropagation();
    confirm(message, {
      title: form.getAttribute('data-confirm-title') || undefined,
      confirmLabel: form.getAttribute('data-confirm-label') || undefined
    }).then(function (yes) {
      if (!yes) return;
      form.dataset.aylaConfirmed = '1';
      // Use requestSubmit when available.
      if (form.requestSubmit) form.requestSubmit();
      else form.submit();
    });
  }, true);
})();
