/* shelf-count.js: count one board against its records (markup in templates/_shelf_count.html). */
(function () {
  'use strict';

  const root = document.getElementById('shelfCount');
  if (!root) return;
  const URLS = { sheet: root.dataset.sheetUrl, file: root.dataset.fileUrl, qr: root.dataset.qrUrl,
                 labels: root.dataset.labelsUrl };
  const CSRF = root.dataset.csrf;
  const ON_LOAN = ['Borrowed', 'Overdue'];

  let sheet = { level: null, books: [], marks: {}, nolabel: {}, where: '', strays: [] };
  let auditStream = null, auditInterval = null, lastAuditCode = '';
  let lastUnlabelled = [];

  function esc(t) {
    return String(t == null ? '' : t).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function byId(id) { return document.getElementById(id); }

  async function loadAuditSheet(levelId) {
    const wrap = byId('auditSheetWrap');
    byId('auditReport').classList.add('hidden');
    if (!levelId) {
      wrap.classList.add('hidden');
      sheet = { level: null, books: [], marks: {}, nolabel: {}, where: '', strays: [] };
      renderStrays();
      return;
    }
    const r = await fetch(URLS.sheet + '?level=' + encodeURIComponent(levelId),
                          { headers: { 'X-Requested-With': 'XMLHttpRequest' } });
    const d = await r.json();
    if (!d.success) { toast(d.error || 'That board could not be opened.', 'error'); return; }
    sheet = { level: d.level, books: d.books, marks: {}, nolabel: {}, strays: [],
              where: d.shelf + ' · ' + d.label };
    renderStrays();
    byId('auditWhere').textContent = sheet.where;
    wrap.classList.remove('hidden');
    renderSheet();
    wrap.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function statusNote(b) {
    if (b.status === 'Missing') {
      return '<span class="text-rose-400">missing since ' + esc(b.missing_since || 'an earlier count')
           + (b.audit_misses > 1 ? ' · ' + b.audit_misses + ' counts' : '') + '</span>';
    }
    if (!b.expected_present) {
      return '<span class="text-blue-400">' + esc(b.status) + ' — recorded automatically'
           + (ON_LOAN.indexOf(b.status) !== -1 ? '; if it is here, tap It&rsquo;s here' : '') + '</span>';
    }
    return b.last_seen ? '<span class="text-slate-600">last seen ' + esc(b.last_seen) + '</span>'
                       : '<span class="text-slate-600">never confirmed</span>';
  }

  function renderSheet() {
    byId('auditSheet').innerHTML = sheet.books.map(function (b, i) {
      const mark = sheet.marks[b.book_id] || '';
      // Copies the system accounts for are not marked, except a loan found on the shelf.
      const locked = !b.expected_present;
      const onLoan = locked && ON_LOAN.indexOf(b.status) !== -1;
      const labelButton = '<button type="button" class="agone alabel' + (sheet.nolabel[b.book_id] ? ' on' : '')
          + '" title="Here, but no label on it" onclick="event.stopPropagation(); markNoLabel(' + b.book_id + ')">'
          + '<i class="fa-solid fa-tag"></i></button>';
      return '<div class="arow ' + (locked ? 'locked ' + (mark === 'onshelf' ? 'onshelf' : '') : mark) + '"'
           + (locked ? '' : ' onclick="markHere(' + b.book_id + ')"') + '>'
           + '<span class="apos">' + (b.slot || (i + 1)) + '</span>'
           + '<span class="atick">'
              + (mark === 'here' || mark === 'bulk' || mark === 'onshelf' ? '<i class="fa-solid fa-check"></i>'
                 : mark === 'gone' ? '<i class="fa-solid fa-xmark"></i>'
                 : mark === 'damaged' ? '<i class="fa-solid fa-exclamation"></i>' : '') + '</span>'
           + '<span class="atitle">' + esc(b.title) + '</span>'
           + '<span class="ameta">' + esc(b.call_number || b.author || '') + '</span>'
           + '<span class="anote">' + statusNote(b) + '</span>'
           + (onLoan
              ? '<button type="button" class="ahere" title="This copy is on the shelf, though it is recorded on loan"'
                + ' onclick="event.stopPropagation(); markOnShelf(' + b.book_id + ')">It&rsquo;s here</button>'
                + (mark === 'onshelf' ? labelButton : '')
              : '')
           + (locked ? ''
              : labelButton
                + '<button type="button" class="agone adamage" title="Here, but damaged"'
                + ' onclick="event.stopPropagation(); markDamaged(' + b.book_id + ')">'
                + '<i class="fa-solid fa-exclamation"></i></button>'
                + '<button type="button" class="agone" title="Not found"'
                + ' onclick="event.stopPropagation(); markGone(' + b.book_id + ')">'
                + '<i class="fa-solid fa-xmark"></i></button>')
           + '</div>';
    }).join('');
    paintTally();
  }

  function paintTally() {
    let here = 0, gone = 0, damaged = 0, untouched = 0, elsewhere = 0, onshelf = 0;
    sheet.books.forEach(function (b) {
      const m = sheet.marks[b.book_id];
      if (!b.expected_present) { if (m === 'onshelf') onshelf++; else elsewhere++; return; }
      if (m === 'here' || m === 'bulk') here++;
      else if (m === 'gone') gone++;
      else if (m === 'damaged') damaged++;
      else untouched++;
    });
    const strays = sheet.strays.length;
    const nolabel = Object.keys(sheet.nolabel).length;
    byId('auditTally').innerHTML =
        '<span class="text-emerald-400 font-semibold">' + here + ' here</span> · '
      + '<span class="text-rose-400 font-semibold">' + gone + ' not found</span> · '
      + (damaged ? '<span class="text-amber-400 font-semibold">' + damaged + ' damaged</span> · ' : '')
      + (onshelf ? '<span class="text-blue-400 font-semibold">' + onshelf + ' on loan but here</span> · ' : '')
      + (nolabel ? '<span class="text-violet-400 font-semibold">' + nolabel + ' no label</span> · ' : '')
      + (strays ? strays + ' stray' + (strays === 1 ? '' : 's') + ' · ' : '')
      + untouched + ' untouched'
      + (elsewhere ? ' · ' + elsewhere + ' recorded automatically' : '');
    // Filing needs a verdict on something.
    byId('auditFileBtn').disabled = !(here || gone || damaged || onshelf || strays || nolabel);
    byId('auditFileLabel').textContent = untouched
      ? 'File this count — the ' + untouched + ' untouched count as here'
      : 'File this count';
  }

  function toggle(id, value) {
    sheet.marks[id] = sheet.marks[id] === value ? '' : value;
    // A book not found carries no label to judge.
    if (sheet.marks[id] === 'gone') delete sheet.nolabel[id];
    renderSheet();
  }
  window.markHere = function (id) { toggle(id, 'here'); };
  window.markGone = function (id) { toggle(id, 'gone'); };
  window.markDamaged = function (id) { toggle(id, 'damaged'); };
  window.markOnShelf = function (id) { toggle(id, 'onshelf'); };
  // No label: the book is here too, so a bare row is ticked as well.
  window.markNoLabel = function (id) {
    if (sheet.nolabel[id]) { delete sheet.nolabel[id]; }
    else {
      sheet.nolabel[id] = true;
      if (!sheet.marks[id] || sheet.marks[id] === 'gone') {
        const b = sheet.books.find(function (x) { return x.book_id === id; });
        sheet.marks[id] = b && !b.expected_present ? 'onshelf' : 'here';
      }
    }
    renderSheet();
  };

  // Books from another board found here: recorded in this count and sent back to their own shelf.
  function renderStrays() {
    const box = byId('auditStrays');
    if (!box) return;
    box.classList.toggle('hidden', !sheet.strays.length);
    box.innerHTML = '<p class="text-[10px] uppercase font-bold tracking-wider text-slate-500">Found here, but belonging elsewhere</p>'
      + sheet.strays.map(function (st, i) {
          return '<div class="flex items-center justify-between gap-2"><span><strong class="text-slate-200">'
            + esc(st.title) + '</strong> <span class="text-slate-500">belongs on ' + esc(st.belongs) + '</span></span>'
            + '<button type="button" class="text-[10px] text-slate-500 hover:text-rose-400 underline" onclick="dropStray(' + i + ')">Remove</button></div>';
        }).join('');
    if (sheet.level) paintTally();
  }
  window.dropStray = function (i) { sheet.strays.splice(i, 1); renderStrays(); };
  // A sweep is its own mark, not the same one a tap writes.
  window.markRest = function () {
    sheet.books.forEach(function (b) {
      if (b.expected_present && !sheet.marks[b.book_id]) sheet.marks[b.book_id] = 'bulk';
    });
    renderSheet();
  };
  window.clearMarks = function () { sheet.marks = {}; sheet.nolabel = {}; renderSheet(); };

  // Scan a label to tick a book, or to find one that belongs elsewhere.
  window.checkStray = async function (code) {
    const label = (code || '').trim();
    const status = byId('auditScanStatus');
    if (!label) return;
    if (!sheet.level) { status.innerHTML = '<span class="text-amber-400">Open a board first.</span>'; return; }
    const hit = sheet.books.find(function (b) { return String(b.qr_code) === label; });
    if (hit) {
      sheet.marks[hit.book_id] = 'here';
      renderSheet();
      status.innerHTML = '<span class="text-emerald-400">Ticked ' + esc(hit.title) + '.</span>';
      return;
    }
    const r = await fetch(URLS.qr + '?qr_code=' + encodeURIComponent(label),
                          { headers: { 'X-Requested-With': 'XMLHttpRequest' } });
    const d = await r.json();
    // An older copy label still names a book on this board.
    const owner = d.success && sheet.books.find(function (b) { return b.book_id === d.book.book_id; });
    if (owner) {
      sheet.marks[owner.book_id] = 'here';
      renderSheet();
      status.innerHTML = '<span class="text-emerald-400">Ticked ' + esc(owner.title) + '.</span>';
      return;
    }
    if (d.success) {
      if (!sheet.strays.some(function (st) { return st.book_id === d.book.book_id; })) {
        sheet.strays.push({ book_id: d.book.book_id, title: d.book.title,
                            belongs: (d.book.category && d.book.category !== 'N/A' ? d.book.category + ', ' : '')
                                     + d.book.shelf_level });
        renderStrays();
      }
      status.innerHTML = '<span class="text-amber-400">' + esc(d.book.title) + ' belongs on '
        + esc(d.book.shelf_level) + ', not this board. Recorded as a stray; it goes on the reshelving cart when you file.</span>';
      return;
    }
    status.innerHTML = '<span class="text-rose-400">No book carries that label.</span>';
  };

  window.toggleAuditCamera = async function () {
    const wrap = byId('auditCamWrap');
    const status = byId('auditScanStatus');
    if (auditStream) {
      stopAuditCamera();
      wrap.classList.add('hidden');
      status.textContent = 'Camera stopped.';
      return;
    }
    try {
      wrap.classList.remove('hidden');
      status.textContent = 'Starting camera…';
      auditStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' } });
      const video = byId('auditVideo');
      video.srcObject = auditStream;
      video.onloadedmetadata = function () {
        status.textContent = 'Point the camera at a label.';
        const canvas = byId('auditCanvas');
        const ctx = canvas.getContext('2d');
        auditInterval = setInterval(function () {
          if (video.readyState !== video.HAVE_ENOUGH_DATA) return;
          canvas.width = video.videoWidth; canvas.height = video.videoHeight;
          ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
          const img = ctx.getImageData(0, 0, canvas.width, canvas.height);
          const code = jsQR(img.data, img.width, img.height);
          if (code && code.data && code.data.trim() !== lastAuditCode) {
            lastAuditCode = code.data.trim();
            window.checkStray(lastAuditCode);
          }
        }, 150);
      };
    } catch (err) {
      status.textContent = 'Camera unavailable — type the label instead.';
    }
  };
  function stopAuditCamera() {
    if (auditInterval) { clearInterval(auditInterval); auditInterval = null; }
    if (auditStream) { auditStream.getTracks().forEach(function (t) { t.stop(); }); auditStream = null; }
    const v = byId('auditVideo');
    if (v) v.srcObject = null;
  }

  window.fileShelfRead = async function () {
    const body = new URLSearchParams();
    body.append('level', sheet.level);
    sheet.books.forEach(function (b) {
      const m = sheet.marks[b.book_id];
      if (sheet.nolabel[b.book_id]) body.append('nolabel_ids', b.book_id);
      if (!b.expected_present) {
        if (m === 'onshelf') body.append('onshelf_ids', b.book_id);
        return;
      }
      if (m === 'here') body.append('found_ids', b.book_id);
      else if (m === 'gone') body.append('missing_ids', b.book_id);
      else if (m === 'damaged') body.append('damaged_ids', b.book_id);
      // Swept and untouched rows are counted together.
      else body.append('bulk_ids', b.book_id);
    });
    sheet.strays.forEach(function (st) { body.append('stray_ids', st.book_id); });

    const count = function (mark) {
      return sheet.books.filter(function (b) { return sheet.marks[b.book_id] === mark; }).length; };
    const gone = count('gone'), damaged = count('damaged'), onshelf = count('onshelf');
    const nolabel = Object.keys(sheet.nolabel).length;
    const notes = [];
    if (gone) notes.push(gone + ' marked Missing (not written off: a book absent from one count is usually mislaid)');
    if (damaged) notes.push(damaged + ' recorded as Damaged and taken out of circulation for repair');
    if (onshelf) notes.push(onshelf + ' on loan but here: the desk is alerted to check them in');
    if (nolabel) notes.push(nolabel + ' with no label: listed for printing');
    if (sheet.strays.length) notes.push(sheet.strays.length + ' stray(s) put on the reshelving cart');
    const ok = await AylaDialog.confirm('File this count for ' + sheet.where + '?', {
      title: 'File the count',
      detail: (notes.length ? notes.join('. ') + '. ' : 'Nothing will be marked missing. ')
            + 'Every other book confirmed here is recorded as labelled.',
      confirmLabel: 'File it', danger: false });
    if (!ok) return;

    const btn = byId('auditFileBtn');
    btn.disabled = true;
    const r = await fetch(URLS.file, {
      method: 'POST', body: body,
      headers: { 'X-CSRFToken': CSRF, 'Content-Type': 'application/x-www-form-urlencoded',
                 'X-Requested-With': 'XMLHttpRequest' },
    });
    const d = await r.json();
    if (!d.success) { toast(d.error || 'The count could not be filed.', 'error'); btn.disabled = false; return; }

    lastUnlabelled = d.nolabel_ids || [];
    const report = byId('auditReport');
    report.innerHTML =
        '<p class="text-sm font-bold text-slate-200">' + esc(sheet.where) + ' — counted</p>'
      + '<ul class="text-xs text-slate-400 space-y-1">'
      + '<li><span class="text-emerald-400 font-semibold">' + d.confirmed + '</span> confirmed on the shelf'
      + ' <span class="text-slate-600">(' + d.individually + ' one by one, ' + d.in_bulk + ' swept)</span></li>'
      + (d.flagged ? '<li><span class="text-rose-400 font-semibold">' + d.flagged
            + '</span> marked Missing — not written off. Review them on the Missing tab.</li>' : '')
      + (d.recovered ? '<li><span class="text-cyan-400 font-semibold">' + d.recovered
            + '</span> turned up again and are back in stock.</li>' : '')
      + (d.damaged ? '<li><span class="text-amber-400 font-semibold">' + d.damaged
            + '</span> found damaged: under repair, not lent until fixed.</li>' : '')
      + (d.onshelf ? '<li><span class="text-blue-400 font-semibold">' + d.onshelf
            + '</span> on the shelf while on loan: check them in at the desk.</li>' : '')
      + (d.strays ? '<li><span class="text-slate-200 font-semibold">' + d.strays
            + '</span> from other boards: on the reshelving cart.</li>' : '')
      + (d.labelled ? '<li><span class="text-emerald-400 font-semibold">' + d.labelled
            + '</span> newly recorded as labelled.</li>' : '')
      + (d.nolabel ? '<li><span class="text-violet-400 font-semibold">' + d.nolabel
            + '</span> with no label. <button type="button" onclick="printCountedUnlabelled()" '
            + 'class="underline font-semibold text-violet-400">Print their labels</button></li>' : '')
      + '</ul>';
    report.classList.remove('hidden');
    stopAuditCamera();
    toast(d.message, 'success');
    const filedLevel = sheet.level;
    await loadAuditSheet(filedLevel);
    report.classList.remove('hidden');
    if (window.onShelfCountFiled) window.onShelfCountFiled(filedLevel, d);
  };

  window.printCountedUnlabelled = function () {
    if (window.AylaLabels) AylaLabels.download(URLS.labels, lastUnlabelled);
  };

  function toast(message, kind) {
    if (window.AylaDialog) AylaDialog.toast(message, kind); else alert(message);
  }

  window.loadAuditSheet = loadAuditSheet;
})();
