/* admin-enhance.js — usability & accessibility behaviour for the AYLA admin panel.
 * Included on every admin page. Defensive: every step is wrapped so a failure on
 * one page never breaks the others. It augments existing markup (ARIA, focus
 * management, keyboard handling) without changing page logic.
 */
(function () {
    'use strict';

    function ready(fn) {
        if (document.readyState !== 'loading') fn();
        else document.addEventListener('DOMContentLoaded', fn);
    }

    var FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type=hidden]), ' +
                    'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

    var openModal = null;
    var lastFocus = null;

    ready(function () {
        safe(skipLink);
        safe(hideDecorativeIcons);
        safe(labelIconButtons);
        safe(markActiveNav);
        safe(enhanceMobileMenu);
        safe(enhanceTables);
        safe(initModals);
    });

    function safe(fn) { try { fn(); } catch (e) { /* keep going */ } }

    /* ── Skip to main content ── */
    function skipLink() {
        var main = document.querySelector('main');
        if (!main) return;
        if (!main.id) main.id = 'main-content';
        main.setAttribute('tabindex', '-1');
        if (document.querySelector('.skip-link')) return;
        var a = document.createElement('a');
        a.href = '#' + main.id;
        a.className = 'skip-link';
        a.textContent = 'Skip to main content';
        a.addEventListener('click', function () { setTimeout(function () { try { main.focus(); } catch (e) {} }, 0); });
        document.body.insertBefore(a, document.body.firstChild);
    }

    /* ── Decorative icons should not be announced ── */
    function hideDecorativeIcons() {
        document.querySelectorAll('i[class*="fa-"]').forEach(function (ic) {
            if (!ic.hasAttribute('aria-hidden')) ic.setAttribute('aria-hidden', 'true');
        });
    }

    /* ── Give icon-only controls an accessible name ── */
    function labelIconButtons() {
        document.querySelectorAll('button, a').forEach(function (el) {
            if ((el.textContent || '').trim()) return;          // has visible text already
            if (el.getAttribute('aria-label')) return;
            var label = el.getAttribute('title') || inferLabel(el);
            if (label) el.setAttribute('aria-label', label);
        });
    }

    function inferLabel(el) {
        var icon = el.querySelector('i[class*="fa-"]');
        var c = icon ? icon.className : '';
        var map = [
            ['fa-bell', 'Notifications'], ['fa-bars', 'Open menu'], ['fa-xmark', 'Close'], ['fa-times', 'Close'],
            ['fa-trash', 'Delete'], ['fa-pen', 'Edit'], ['fa-pencil', 'Edit'], ['fa-plus', 'Add'],
            ['fa-eye', 'View'], ['fa-power-off', 'Deactivate'], ['fa-check', 'Activate'],
            ['fa-hammer', 'Renovation notice'], ['fa-magnifying-glass', 'Search'], ['fa-search', 'Search'],
            ['fa-rotate', 'Refresh'], ['fa-download', 'Download'], ['fa-upload', 'Upload'],
            ['fa-file-excel', 'Download template'], ['fa-file-invoice', 'Export']
        ];
        for (var i = 0; i < map.length; i++) if (c.indexOf(map[i][0]) !== -1) return map[i][1];
        return '';
    }

    /* ── Mark the current sidebar item ── */
    function markActiveNav() {
        var path = location.pathname.replace(/\/+$/, '');
        document.querySelectorAll('aside a[href]').forEach(function (a) {
            var href = (a.getAttribute('href') || '').replace(/\/+$/, '');
            if (href && href === path) a.setAttribute('aria-current', 'page');
        });
    }

    /* ── Mobile menu button: announce its state ── */
    function enhanceMobileMenu() {
        var aside = document.querySelector('aside');
        var btn = document.querySelector('.mobile-menu-btn');
        if (!btn || !aside) return;
        if (!aside.id) aside.id = 'admin-sidebar';
        btn.setAttribute('aria-controls', aside.id);
        if (!btn.getAttribute('aria-label')) btn.setAttribute('aria-label', 'Toggle navigation menu');
        var sync = function () { btn.setAttribute('aria-expanded', aside.classList.contains('open') ? 'true' : 'false'); };
        sync();
        new MutationObserver(sync).observe(aside, { attributes: true, attributeFilter: ['class'] });
        // also expose the sidebar as a navigation landmark
        var nav = aside.querySelector('nav');
        if (nav && !nav.getAttribute('aria-label')) nav.setAttribute('aria-label', 'Primary');
    }

    /* ── Table header semantics ── */
    function enhanceTables() {
        document.querySelectorAll('table thead th').forEach(function (th) {
            if (!th.getAttribute('scope')) th.setAttribute('scope', 'col');
        });
    }

    /* ── Accessible modals: role, label, focus-in, focus-trap, focus-return, Escape ── */
    function isVisible(el) {
        if (el.classList.contains('hidden')) return false;
        var cs = getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') return false;
        return el.offsetParent !== null || cs.position === 'fixed';
    }

    function modalCandidates() {
        var found = [];
        var seen = new Set();
        document.querySelectorAll('[class*="bg-black/"], .modal-overlay, [id$="Modal"]').forEach(function (el) {
            var cls = typeof el.className === 'string' ? el.className : '';
            var looksModal = /inset-0/.test(cls) || /modal-overlay/.test(cls) ||
                             ((/fixed|absolute/.test(cls)) && /bg-black/.test(cls));
            if (looksModal && !seen.has(el)) { seen.add(el); found.push(el); }
        });
        return found;
    }

    function initModals() {
        modalCandidates().forEach(function (el) {
            el.setAttribute('role', el.getAttribute('role') || 'dialog');
            el.setAttribute('aria-modal', 'true');
            var h = el.querySelector('h1, h2, h3, h4');
            if (h) {
                if (!h.id) h.id = 'mh-' + Math.random().toString(36).slice(2, 8);
                el.setAttribute('aria-labelledby', h.id);
            }
            if (isVisible(el)) onOpen(el);
            new MutationObserver(function () {
                var v = isVisible(el);
                if (v && !el.__open) onOpen(el);
                else if (!v && el.__open) onClose(el);
            }).observe(el, { attributes: true, attributeFilter: ['class', 'style'] });
        });
    }

    function onOpen(el) {
        el.__open = true;
        openModal = el;
        lastFocus = document.activeElement;
        var target = el.querySelector('input:not([type=hidden]):not([disabled]), select, textarea') ||
                     el.querySelector(FOCUSABLE);
        if (target) setTimeout(function () { try { target.focus(); } catch (e) {} }, 30);
    }

    function onClose(el) {
        el.__open = false;
        if (openModal === el) openModal = null;
        if (lastFocus && lastFocus.focus) { try { lastFocus.focus(); } catch (e) {} }
    }

    document.addEventListener('keydown', function (e) {
        if (!openModal || !isVisible(openModal)) return;
        if (e.key === 'Tab') {
            var nodes = Array.prototype.filter.call(openModal.querySelectorAll(FOCUSABLE),
                function (n) { return n.offsetParent !== null; });
            if (!nodes.length) return;
            var first = nodes[0], last = nodes[nodes.length - 1];
            if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
            else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
        } else if (e.key === 'Escape') {
            // Prefer the page's own close control (restores body scroll etc.).
            var closer = openModal.querySelector('button[onclick*="close" i], [data-dismiss], button[aria-label="Close"]');
            if (closer) closer.click();
            else openModal.classList.add('hidden');
        }
    });
})();
