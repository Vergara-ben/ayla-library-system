/* =====================================================================
   theme.js — light/dark switching for AYLA.

   Load this as the FIRST script in <head>, before any stylesheet, so the
   theme attribute is on <html> before the first paint (no dark flash).

   Rules:
   - Light is the default. The operating system preference is deliberately
     ignored: a user whose phone is in dark mode still lands on light.
   - The choice is remembered per browser in localStorage.
   ===================================================================== */
(function () {
    'use strict';

    var STORAGE_KEY = 'ayla-theme';
    var root = document.documentElement;

    function read() {
        try {
            var v = window.localStorage.getItem(STORAGE_KEY);
            return (v === 'dark' || v === 'light') ? v : null;
        } catch (e) {
            return null;               /* private mode / storage disabled */
        }
    }

    function write(theme) {
        try {
            window.localStorage.setItem(STORAGE_KEY, theme);
        } catch (e) { /* ignore — the theme still applies for this page */ }
    }

    /* The AYLA logo's wordmark is black ink, so the dark themes need the
       variant that has a light one. This swaps the <img> source rather than
       hiding a second copy or painting it as a CSS background: the image is
       a plain <img> with a real src, so it stays visible even if this script
       or the stylesheet never loads. Worst case it shows the light logo. */
    function applyLogos(theme) {
        var logos = document.querySelectorAll('img[data-logo-dark]');
        for (var i = 0; i < logos.length; i++) {
            var el = logos[i];
            var want = (theme === 'dark')
                ? el.getAttribute('data-logo-dark')
                : el.getAttribute('data-logo-light');
            if (want && el.getAttribute('src') !== want) {
                el.setAttribute('src', want);
            }
        }
    }

    function apply(theme) {
        root.setAttribute('data-theme', theme);
        var meta = document.querySelector('meta[name="theme-color"]');
        if (meta) {
            meta.setAttribute('content', theme === 'dark' ? '#0b1220' : '#ffffff');
        }
        applyLogos(theme);
    }

    /* Run immediately, before the body exists. */
    apply(read() || 'light');

    /* The images do not exist yet on that first pass, so set them again once
       the document is parsed. */
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () {
            applyLogos(root.getAttribute('data-theme') || 'light');
        });
    } else {
        applyLogos(root.getAttribute('data-theme') || 'light');
    }

    var AylaTheme = {
        get: function () {
            return root.getAttribute('data-theme') || 'light';
        },
        set: function (theme) {
            theme = (theme === 'dark') ? 'dark' : 'light';
            apply(theme);
            write(theme);
            document.dispatchEvent(new CustomEvent('ayla:themechange', { detail: { theme: theme } }));
        },
        toggle: function () {
            AylaTheme.set(AylaTheme.get() === 'dark' ? 'light' : 'dark');
        },
        /* Resolve a token to a real colour string.
           Needed wherever a colour is written as an SVG attribute rather than
           a CSS property — Leaflet markers, for one — because SVG presentation
           attributes do not understand var(). */
        color: function (token) {
            var value = getComputedStyle(root).getPropertyValue(token);
            return (value || '').trim() || '#888888';
        }
    };
    window.AylaTheme = AylaTheme;

    /* ---- Toggle buttons ---------------------------------------------
       Any element marked [data-theme-toggle] becomes a switch. Pages with
       no natural home for one (the sign-in screens) get a small floating
       button instead. */
    function buttonMarkup(wide) {
        return '<span class="icon-dark" aria-hidden="true">\u{1F319}</span>' +
               '<span class="icon-light" aria-hidden="true">☀️</span>' +
               (wide ? '<span class="label-dark">Dark mode</span>' +
                       '<span class="label-light">Light mode</span>' : '');
    }

    function label() {
        return AylaTheme.get() === 'dark' ? 'Switch to light mode' : 'Switch to dark mode';
    }

    function wire(el) {
        if (el.getAttribute('data-theme-bound')) { return; }
        el.setAttribute('data-theme-bound', '1');

        if (!el.innerHTML.trim()) {
            el.innerHTML = buttonMarkup(el.classList.contains('is-wide'));
        }
        el.setAttribute('title', label());
        el.setAttribute('aria-label', label());

        el.addEventListener('click', function (e) {
            e.preventDefault();
            AylaTheme.toggle();
            el.setAttribute('title', label());
            el.setAttribute('aria-label', label());
        });
    }

    function mount() {
        var toggles = document.querySelectorAll('[data-theme-toggle]');

        /* Pages with no chrome to hang a button on (the sign-in screens)
           opt in with <html data-theme-floating>. */
        if (!toggles.length && root.hasAttribute('data-theme-floating')) {
            var floating = document.createElement('button');
            floating.type = 'button';
            floating.className = 'theme-toggle';
            floating.setAttribute('data-theme-toggle', '');
            floating.style.cssText =
                'position:fixed;top:16px;right:16px;z-index:9999;' +
                'background:var(--bg-card,var(--surface,#fff));';
            document.body.appendChild(floating);
            toggles = [floating];
        }

        Array.prototype.forEach.call(toggles, wire);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', mount);
    } else {
        mount();
    }
})();
