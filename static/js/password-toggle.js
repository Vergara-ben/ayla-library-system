/* password-toggle.js: a show/hide button on every password field, including ones added later. */
(function () {
    'use strict';
    if (window.__aylaPasswordToggle) return;
    window.__aylaPasswordToggle = true;

    var SVG = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" '
            + 'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">';
    var EYE = SVG + '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>';
    var EYE_OFF = SVG + '<path d="M17.94 17.94A10.4 10.4 0 0 1 12 19c-6.4 0-10-7-10-7a18.6 18.6 0 0 1 5.06-5.94"/>'
                + '<path d="M9.9 4.24A9.6 9.6 0 0 1 12 5c6.4 0 10 7 10 7a18.5 18.5 0 0 1-2.16 3.19"/>'
                + '<path d="M14.12 14.12a3 3 0 1 1-4.24-4.24"/><path d="M2 2l20 20"/></svg>';

    function addStyles() {
        if (document.getElementById('pw-toggle-styles')) return;
        var style = document.createElement('style');
        style.id = 'pw-toggle-styles';
        style.textContent =
            '.pw-wrap{position:relative;display:block;width:100%;flex:1 1 auto;min-width:0}'
            + '.pw-wrap>input{width:100%}'
            + '.pw-eye{position:absolute;top:50%;right:6px;transform:translateY(-50%);z-index:2;'
            + 'display:inline-flex;align-items:center;justify-content:center;width:34px;height:34px;'
            + 'padding:0;margin:0;border:0;border-radius:8px;background:transparent;color:inherit;'
            + 'opacity:.6;cursor:pointer;box-shadow:none}'
            + '.pw-eye:hover{opacity:1}'
            + '.pw-eye:focus-visible{opacity:1;outline:2px solid currentColor;outline-offset:1px}';
        document.head.appendChild(style);
    }

    function show(input, button, visible) {
        input.type = visible ? 'text' : 'password';
        button.innerHTML = visible ? EYE_OFF : EYE;
        button.setAttribute('aria-pressed', visible ? 'true' : 'false');
        button.setAttribute('aria-label', visible ? 'Hide password' : 'Show password');
        button.title = visible ? 'Hide password' : 'Show password';
    }

    function enhance(input) {
        if (input.getAttribute('data-pw-toggle')) return;
        input.setAttribute('data-pw-toggle', '1');
        addStyles();

        var computed = window.getComputedStyle(input);
        var wrap = document.createElement('span');
        wrap.className = 'pw-wrap';
        // The field's outer spacing moves to the wrapper, so the button centres on the box itself.
        wrap.style.margin = computed.marginTop + ' ' + computed.marginRight + ' '
                          + computed.marginBottom + ' ' + computed.marginLeft;
        input.style.margin = '0';
        var right = parseFloat(computed.paddingRight) || 0;
        if (right < 42) input.style.paddingRight = '42px';

        input.parentNode.insertBefore(wrap, input);
        wrap.appendChild(input);

        var button = document.createElement('button');
        button.type = 'button';
        button.className = 'pw-eye';
        button.style.color = computed.color;
        show(input, button, false);
        // Keep the caret in the field when the button is pressed.
        button.addEventListener('mousedown', function (e) { e.preventDefault(); });
        button.addEventListener('click', function () {
            show(input, button, input.type === 'password');
        });
        wrap.appendChild(button);

        // Hidden again before sending, so password managers still see a password field.
        if (input.form) {
            input.form.addEventListener('submit', function () { show(input, button, false); });
        }
    }

    function scan(root) {
        if (!root || !root.querySelectorAll) return;
        if (root.matches && root.matches('input[type="password"]')) enhance(root);
        var found = root.querySelectorAll('input[type="password"]');
        for (var i = 0; i < found.length; i++) enhance(found[i]);
    }

    function start() {
        scan(document.body);
        if (window.MutationObserver) {
            new MutationObserver(function (records) {
                records.forEach(function (r) {
                    for (var i = 0; i < r.addedNodes.length; i++) scan(r.addedNodes[i]);
                });
            }).observe(document.body, { childList: true, subtree: true });
        }
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();
})();
