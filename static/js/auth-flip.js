/* auth-flip.js: flips the sign-in card before moving to another portal's login. */
(function () {
    'use strict';
    var KEY = 'aylaAuthFlip';
    var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    function card() { return document.querySelector('.neu-card'); }

    function read() {
        try { var t = +sessionStorage.getItem(KEY); sessionStorage.removeItem(KEY); return t; } catch (e) { return 0; }
    }
    function mark() {
        try { sessionStorage.setItem(KEY, String(Date.now())); } catch (e) {}
    }

    document.addEventListener('DOMContentLoaded', function () {
        var c = card();
        if (!c) return;
        // Only play the flip-in when we arrived from a flip a moment ago.
        if (!reduce && Date.now() - read() < 4000) {
            c.classList.add('is-flipping-in');
            c.addEventListener('animationend', function () { c.classList.remove('is-flipping-in'); }, { once: true });
        }

        document.addEventListener('click', function (e) {
            var link = e.target.closest && e.target.closest('a[data-flip]');
            if (!link || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
            if (reduce) return;
            e.preventDefault();
            mark();
            c.classList.remove('is-flipping-in');
            c.classList.add('is-flipping-out');
            setTimeout(function () { window.location.href = link.href; }, 380);
        });
    });

    // Coming back with the Back button should not leave the card turned away.
    window.addEventListener('pageshow', function (e) {
        var c = card();
        if (e.persisted && c) c.classList.remove('is-flipping-out');
    });
})();
