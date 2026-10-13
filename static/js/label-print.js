/* label-print.js: download a sheet of QR labels for some books, with the usual settings. */
(function () {
  'use strict';

  async function download(url, ids, name) {
    if (!ids || !ids.length) return false;
    let res;
    try {
      res = await fetch(url + '?' + new URLSearchParams({ ids: ids.join(','), sort: 'location' }).toString(),
                        { headers: { 'X-Requested-With': 'XMLHttpRequest' } });
    } catch (e) {
      toast('The server did not answer. No labels were printed.', 'error');
      return false;
    }
    const type = res.headers.get('Content-Type') || '';
    if (!res.ok || type.indexOf('application/pdf') !== 0) {
      let message = 'The label sheet could not be made. Check that you are still signed in, then try again.';
      try { const data = await res.json(); if (data && data.error) message = data.error; } catch (e) { /* not JSON */ }
      toast(message, 'error');
      return false;
    }
    const skipped = Number(res.headers.get('X-Labels-Skipped') || 0);
    if (skipped) {
      toast(skipped + ' book' + (skipped === 1 ? ' has' : 's have') + ' no shelf yet, so '
            + (skipped === 1 ? 'it was' : 'they were') + ' left off the sheet.', 'warning');
    }
    const blob = await res.blob();
    const named = /filename="?([^";]+)"?/.exec(res.headers.get('Content-Disposition') || '');
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = named ? named[1] : (name || 'AYLA-QR-labels.pdf');
    link.style.display = 'none';
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(function () { URL.revokeObjectURL(link.href); }, 1000);
    return true;
  }

  function toast(message, kind) {
    if (window.AylaDialog) AylaDialog.toast(message, kind); else alert(message);
  }

  window.AylaLabels = { download: download };
})();
