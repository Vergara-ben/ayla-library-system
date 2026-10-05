/* picker.js: a search box with a dropdown list, for picking one of many (a donor, a title). */
(function () {
  'use strict';

  var MAX_SHOWN = 60;

  function injectStyle() {
    if (document.getElementById('ayla-picker-style')) return;
    var st = document.createElement('style');
    st.id = 'ayla-picker-style';
    st.textContent =
      '.ayla-picker{position:relative}' +
      '.ayla-picker-list{position:absolute;left:0;right:0;min-width:260px;top:calc(100% + 4px);z-index:80;max-height:260px;overflow-y:auto;' +
      'border-radius:10px;border:1px solid var(--border-modal);background:var(--bg-modal);box-shadow:0 12px 28px rgba(0,0,0,.3);padding:4px}' +
      '.ayla-picker-item{display:block;width:100%;text-align:left;padding:7px 10px;border-radius:7px;border:0;background:none;' +
      'color:var(--text-main);font-size:12px;cursor:pointer;line-height:1.35}' +
      '.ayla-picker-item small{display:block;color:var(--text-muted);font-size:10.5px}' +
      '.ayla-picker-item.is-on,.ayla-picker-item:hover{background:var(--tint-accent)}' +
      '.ayla-picker-new{color:var(--accent);font-weight:700}' +
      '.ayla-picker-more{padding:6px 10px;font-size:10.5px;color:var(--text-muted)}';
    document.head.appendChild(st);
  }

  function attach(input, opts) {
    injectStyle();
    var wrap = document.createElement('div');
    wrap.className = 'ayla-picker';
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);
    input.setAttribute('autocomplete', 'off');
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-expanded', 'false');

    var list = document.createElement('div');
    list.className = 'ayla-picker-list';
    list.setAttribute('role', 'listbox');
    list.hidden = true;
    wrap.appendChild(list);

    var shown = [];
    var active = -1;

    function items() { return typeof opts.items === 'function' ? opts.items() : opts.items; }

    function render() {
      var q = input.value.trim().toLowerCase();
      var all = items();
      var matches = q ? all.filter(function (it) {
        return (it.label + ' ' + (it.sub || '') + ' ' + (it.search || '')).toLowerCase().indexOf(q) !== -1;
      }) : all;
      shown = matches.slice(0, MAX_SHOWN);
      var exact = q && all.some(function (it) { return it.label.toLowerCase() === q; });
      var rows = [];
      if (opts.newLabel && !exact) {
        rows.push({ isNew: true, label: opts.newLabel(input.value.trim()) });
      }
      shown = rows.concat(shown);
      active = shown.length ? (rows.length && q && matches.length ? 1 : 0) : -1;
      list.innerHTML = '';
      shown.forEach(function (it, i) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'ayla-picker-item' + (it.isNew ? ' ayla-picker-new' : '') + (i === active ? ' is-on' : '');
        b.setAttribute('role', 'option');
        b.textContent = it.label;
        if (it.sub) {
          var sm = document.createElement('small');
          sm.textContent = it.sub;
          b.appendChild(sm);
        }
        b.addEventListener('mousedown', function (e) { e.preventDefault(); choose(i); });
        list.appendChild(b);
      });
      if (matches.length > MAX_SHOWN) {
        var more = document.createElement('div');
        more.className = 'ayla-picker-more';
        more.textContent = (matches.length - MAX_SHOWN) + ' more — keep typing to narrow the list';
        list.appendChild(more);
      }
      if (!shown.length) {
        var none = document.createElement('div');
        none.className = 'ayla-picker-more';
        none.textContent = 'Nothing matches';
        list.appendChild(none);
      }
      open();
    }

    function open() { list.hidden = false; input.setAttribute('aria-expanded', 'true'); }
    function close() { list.hidden = true; input.setAttribute('aria-expanded', 'false'); }

    function highlight(i) {
      var buttons = list.querySelectorAll('.ayla-picker-item');
      if (!buttons.length) return;
      active = (i + buttons.length) % buttons.length;
      buttons.forEach(function (b, j) { b.classList.toggle('is-on', j === active); });
      buttons[active].scrollIntoView({ block: 'nearest' });
    }

    function choose(i) {
      var it = shown[i];
      if (!it) return;
      close();
      if (it.isNew) { if (opts.onNew) opts.onNew(input.value.trim()); return; }
      input.value = opts.keepLabel === false ? '' : it.label;
      if (opts.onPick) opts.onPick(it);
    }

    input.addEventListener('focus', render);
    input.addEventListener('click', render);
    input.addEventListener('input', function () {
      if (opts.onType) opts.onType(input.value.trim());
      render();
    });
    input.addEventListener('keydown', function (e) {
      if (list.hidden && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) { render(); return; }
      if (e.key === 'ArrowDown') { e.preventDefault(); highlight(active + 1); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); highlight(active - 1); }
      else if (e.key === 'Enter' && !list.hidden && active >= 0) { e.preventDefault(); choose(active); }
      else if (e.key === 'Escape') { close(); }
    });
    input.addEventListener('blur', function () { setTimeout(close, 120); });
    return { close: close, refresh: render };
  }

  window.AylaPicker = { attach: attach };
})();
