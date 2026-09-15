/* theme.js: light and dark theme switching. */
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

    /* Swap the logo for dark mode. */
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

    /* Run again after the page loads. */
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
        /* Resolve a token to a real colour string. */
        color: function (token) {
            var value = getComputedStyle(root).getPropertyValue(token);
            return (value || '').trim() || '#888888';
        }
    };
    window.AylaTheme = AylaTheme;

    /* Theme toggle buttons. */
    function buttonMarkup(wide) {
        return '<span class="icon-dark" aria-hidden="true"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg></span>' +
               '<span class="icon-light" aria-hidden="true"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg></span>' +
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

        /* Floating toggle for pages without a header. */
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
