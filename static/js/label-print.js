/* label-print.js: the print settings for a sheet of QR labels, then the PDF. */
(function () {
  'use strict';

  var KEY = 'ayla.labels.settings';
  var DEFAULTS = { page: 'A4', qr_mm: 25, sort: 'location', show_title: true, show_copy: true,
                   show_call_number: true, show_id: false, show_author: false, show_location: true,
                   cut_guides: true };
  // Labels per page, worked out the same way as the PDF.
  var PAGE_MM = { A4: [210, 297], Letter: [215.9, 279.4], Legal: [215.9, 355.6] };
  var MARGIN_MM = 10, PAD_MM = 2, LEADING = 1.18, PT_MM = 25.4 / 72;
  var SHOW = [['show_title', 'Title'], ['show_copy', 'Copy number'], ['show_call_number', 'Code label'],
              ['show_id', 'Internal ID'], ['show_author', 'Author'], ['show_location', 'Shelf location']];

  function remembered() {
    var saved = {};
    try { saved = JSON.parse(localStorage.getItem(KEY) || '{}') || {}; } catch (e) { saved = {}; }
    var out = {};
    Object.keys(DEFAULTS).forEach(function (k) { out[k] = k in saved ? saved[k] : DEFAULTS[k]; });
    return out;
  }
  function remember(s) {
    try { localStorage.setItem(KEY, JSON.stringify(s)); } catch (e) { /* private mode: not kept */ }
  }

  function esc(text) {
    return String(text == null ? '' : text).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function toast(message, kind) {
    if (window.AylaDialog) AylaDialog.toast(message, kind); else alert(message);
  }

  var FIELD = 'w-full rounded-lg px-3 py-2.5 text-slate-200 border border-slate-700/60 focus:outline-none font-medium';
  var LABEL = 'block uppercase font-bold tracking-wider text-slate-500 text-[10px]';

  function build(accent) {
    var box = document.createElement('div');
    box.id = 'aylaLabelModal';
    box.className = 'hidden fixed inset-0 bg-black/60 z-[70] items-center justify-center p-4 overflow-y-auto';
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-modal', 'true');
    box.setAttribute('aria-labelledby', 'aylaLabelTitle');
    box.innerHTML =
      '<div class="w-full max-w-lg rounded-2xl border p-6 flex flex-col gap-4 shadow-2xl relative my-auto text-xs"'
      + ' style="background-color: var(--bg-modal); border-color: var(--border-color);">'
      + '<div class="flex items-center justify-between">'
      +   '<h3 id="aylaLabelTitle" class="text-base font-bold text-slate-200 tracking-wide">Print QR labels</h3>'
      +   '<button type="button" data-l="close" class="w-7 h-7 rounded-lg flex items-center justify-center text-slate-400 hover:text-slate-200 hover:bg-slate-800/50 border border-slate-700/40" aria-label="Close"><i class="fa-solid fa-xmark text-sm"></i></button>'
      + '</div>'
      + '<div class="rounded-lg border border-slate-800/80 px-3 py-2.5" style="background-color: var(--bg-input);">'
      +   '<p class="text-[13px] text-slate-200"><strong data-l="count" class="text-' + accent + '-400"></strong> <span data-l="where" class="text-slate-400"></span></p>'
      +   '<p data-l="estimate" class="text-[11px] text-slate-400 mt-1"></p>'
      + '</div>'
      + '<p data-l="error" class="hidden rounded-lg border border-rose-500/25 bg-rose-500/10 px-3 py-2 text-[11px] text-rose-300"></p>'
      + '<div class="grid grid-cols-2 gap-3">'
      +   '<div class="space-y-1.5"><label for="aylaLabelPage" class="' + LABEL + '">Paper</label>'
      +     '<select id="aylaLabelPage" data-s="page" class="' + FIELD + '" style="background-color: var(--bg-input);">'
      +       '<option value="A4">A4 (210 &times; 297 mm)</option><option value="Letter">Short bond / Letter</option>'
      +       '<option value="Legal">Long bond / Legal</option></select></div>'
      +   '<div class="space-y-1.5"><label for="aylaLabelSize" class="' + LABEL + '">QR size (mm)</label>'
      +     '<input id="aylaLabelSize" data-s="qr_mm" type="number" min="12" max="60" step="1" class="' + FIELD + '" style="background-color: var(--bg-input);"></div>'
      + '</div>'
      + '<div class="space-y-1.5"><label for="aylaLabelSort" class="' + LABEL + '">Print order</label>'
      +   '<select id="aylaLabelSort" data-s="sort" class="' + FIELD + '" style="background-color: var(--bg-input);">'
      +     '<option value="location">Shelf order &mdash; walk the room</option><option value="title">A to Z by title</option></select></div>'
      + '<div class="space-y-2"><span class="' + LABEL + '">Print under each QR</span>'
      +   '<div class="grid grid-cols-2 gap-y-2 gap-x-3 text-slate-300">'
      +     SHOW.map(function (s) {
              return '<label class="flex items-center gap-2 cursor-pointer"><input type="checkbox" data-s="' + s[0]
                + '" class="accent-' + accent + '-600 w-3.5 h-3.5"> ' + s[1] + '</label>';
            }).join('')
      +     '<label class="flex items-center gap-2 cursor-pointer"><input type="checkbox" data-s="cut_guides" class="accent-' + accent + '-600 w-3.5 h-3.5"> Cutting guides</label>'
      +   '</div></div>'
      + '<div class="grid grid-cols-2 gap-3">'
      +   '<div class="space-y-1.5"><label for="aylaLabelSkip" class="' + LABEL + '">Skip first N spaces</label>'
      +     '<input id="aylaLabelSkip" data-l="skip" type="number" min="0" step="1" value="0" class="' + FIELD + '" style="background-color: var(--bg-input);">'
      +     '<p class="text-[10px] text-slate-500">For a part-used sheet.</p></div>'
      +   '<div class="space-y-1.5"><label for="aylaLabelFile" class="' + LABEL + '">File name</label>'
      +     '<input id="aylaLabelFile" data-l="file" type="text" maxlength="100" placeholder="AYLA-QR-labels" class="' + FIELD + ' placeholder-slate-600" style="background-color: var(--bg-input);">'
      +     '<p class="text-[10px] text-slate-500">Blank for the usual name.</p></div>'
      + '</div>'
      + '<div class="rounded-lg border border-amber-900/50 bg-amber-950/20 px-3 py-2.5"><p class="text-[10px] text-amber-300 leading-relaxed">'
      +   '<i class="fa-solid fa-triangle-exclamation mr-1"></i> Print at <strong>100% scale</strong> &mdash; turn off &ldquo;Fit to page&rdquo;. '
      +   'Measure one square before cutting the rest.</p></div>'
      + '<div class="flex items-center justify-between gap-3 pt-1">'
      +   '<p class="text-[10px] text-slate-500">Your settings are kept for next time.</p>'
      +   '<div class="flex gap-2">'
      +     '<button type="button" data-l="cancel" class="px-4 py-2.5 rounded-lg border border-slate-700/80 text-slate-300 hover:bg-slate-800/40 font-semibold">Cancel</button>'
      +     '<button type="button" data-l="go" class="bg-' + accent + '-700 hover:bg-' + accent + '-600 text-white font-bold px-4 py-2.5 rounded-lg inline-flex items-center gap-2 disabled:opacity-50"><i class="fa-solid fa-file-pdf text-xs"></i> Download PDF</button>'
      +   '</div>'
      + '</div>'
      + '</div>';
    document.body.appendChild(box);
    return box;
  }

  function read(box) {
    var s = {};
    box.querySelectorAll('[data-s]').forEach(function (el) {
      var k = el.getAttribute('data-s');
      s[k] = el.type === 'checkbox' ? el.checked : el.value;
    });
    var qr = parseFloat(s.qr_mm) || DEFAULTS.qr_mm;
    s.qr_mm = Math.max(12, Math.min(60, qr));
    return s;
  }

  function estimate(box, count) {
    var s = read(box);
    var size = PAGE_MM[s.page] || PAGE_MM.A4;
    var lines = SHOW.filter(function (x) { return s[x[0]]; }).length;
    var fs = Math.max(3.6, Math.min(7.0, s.qr_mm * 0.235));
    var cellW = s.qr_mm + PAD_MM, cellH = s.qr_mm + lines * fs * LEADING * PT_MM + PAD_MM;
    var cols = Math.max(1, Math.floor((size[0] - 2 * MARGIN_MM) / cellW));
    var rows = Math.max(1, Math.floor((size[1] - 2 * MARGIN_MM) / cellH));
    var skip = Math.max(0, parseInt(box.querySelector('[data-l="skip"]').value, 10) || 0);
    var pages = Math.max(1, Math.ceil((count + skip) / (cols * rows)));
    box.querySelector('[data-l="estimate"]').textContent =
      cols + ' × ' + rows + ' = ' + (cols * rows) + ' labels a page · '
      + pages + ' page' + (pages === 1 ? '' : 's');
  }

  // The PDF itself, with these settings. Resolves to true once the browser has it.
  async function sheet(url, ids, s, fileName) {
    var q = new URLSearchParams({ ids: ids.join(','), page: s.page, qr_mm: String(s.qr_mm), sort: s.sort,
                                  skip: String(s.skip || 0), filename: fileName || '' });
    ['show_title', 'show_copy', 'show_call_number', 'show_id', 'show_author', 'show_location', 'cut_guides']
      .forEach(function (k) { q.set(k, s[k] ? '1' : '0'); });
    var res;
    try {
      res = await fetch(url + '?' + q.toString(), { headers: { 'X-Requested-With': 'XMLHttpRequest' } });
    } catch (e) {
      throw new Error('The server did not answer. No labels were printed.');
    }
    var type = res.headers.get('Content-Type') || '';
    if (!res.ok || type.indexOf('application/pdf') !== 0) {
      var message = 'The label sheet could not be made. Check that you are still signed in, then try again.';
      try { var data = await res.json(); if (data && data.error) message = data.error; } catch (e) { /* not JSON */ }
      throw new Error(message);
    }
    var skipped = Number(res.headers.get('X-Labels-Skipped') || 0);
    if (skipped) {
      toast(skipped + ' book' + (skipped === 1 ? ' has' : 's have') + ' no shelf yet, so '
            + (skipped === 1 ? 'it was' : 'they were') + ' left off the sheet.', 'warning');
    }
    var blob = await res.blob();
    var named = /filename="?([^";]+)"?/.exec(res.headers.get('Content-Disposition') || '');
    var link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = named ? named[1] : 'AYLA-QR-labels.pdf';
    link.style.display = 'none';
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(function () { URL.revokeObjectURL(link.href); }, 1000);
    return true;
  }

  /* Opens the settings for these books. Resolves to true once a sheet was downloaded, false otherwise.
     opts.where says what is being printed ("Shelf A · Board 3"). */
  function download(url, ids, name, opts) {
    opts = opts || {};
    ids = (ids || []).map(Number).filter(Boolean);
    if (!ids.length) { toast('Choose at least one book first.', 'warning'); return Promise.resolve(false); }
    var accent = opts.accent || AylaLabels.accent || 'cyan';
    var box = document.getElementById('aylaLabelModal') || build(accent);
    var s = remembered();
    box.querySelectorAll('[data-s]').forEach(function (el) {
      var k = el.getAttribute('data-s');
      if (el.type === 'checkbox') el.checked = !!s[k]; else el.value = s[k];
    });
    box.querySelector('[data-l="skip"]').value = '0';
    box.querySelector('[data-l="file"]').value = name || '';
    box.querySelector('[data-l="count"]').textContent = ids.length + ' label' + (ids.length === 1 ? '' : 's');
    box.querySelector('[data-l="where"]').textContent = opts.where ? '· ' + opts.where : '';
    var err = box.querySelector('[data-l="error"]');
    err.classList.add('hidden');
    var go = box.querySelector('[data-l="go"]');
    go.disabled = false;
    var previous = document.activeElement;

    return new Promise(function (resolve) {
      function refresh() { estimate(box, ids.length); }
      function close(answer) {
        box.classList.add('hidden');
        box.classList.remove('flex');
        box.removeEventListener('input', refresh);
        box.removeEventListener('change', refresh);
        document.removeEventListener('keydown', onKey, true);
        box.onclick = null;
        if (previous && previous.focus) { try { previous.focus(); } catch (e) { /* gone */ } }
        resolve(answer);
      }
      function onKey(e) { if (e.key === 'Escape') { e.preventDefault(); close(false); } }
      box.onclick = async function (e) {
        if (e.target === box || e.target.closest('[data-l="close"],[data-l="cancel"]')) { close(false); return; }
        if (!e.target.closest('[data-l="go"]') || go.disabled) return;
        var chosen = read(box);
        var keep = {};
        Object.keys(DEFAULTS).forEach(function (k) { keep[k] = chosen[k]; });
        remember(keep);
        chosen.skip = Math.max(0, parseInt(box.querySelector('[data-l="skip"]').value, 10) || 0);
        go.disabled = true;
        go.innerHTML = '<i class="fa-solid fa-spinner fa-spin text-xs"></i> Preparing PDF…';
        try {
          await sheet(url, ids, chosen, box.querySelector('[data-l="file"]').value.trim());
          close(true);
        } catch (ex) {
          err.textContent = ex.message;
          err.classList.remove('hidden');
        }
        go.disabled = false;
        go.innerHTML = '<i class="fa-solid fa-file-pdf text-xs"></i> Download PDF';
      };
      box.addEventListener('input', refresh);
      box.addEventListener('change', refresh);
      document.addEventListener('keydown', onKey, true);
      refresh();
      box.classList.remove('hidden');
      box.classList.add('flex');
      go.focus();
    });
  }

  window.AylaLabels = { download: download, sheet: sheet, accent: 'cyan' };
})();
