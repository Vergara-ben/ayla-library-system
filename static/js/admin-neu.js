/* admin-neu.js: walkers in the dashboard banner and stat numbers that count up. Decorative only. */
(function () {
    'use strict';
    var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    // Runs before auth-decor.js, which fills this spot with the walkers.
    var hero = document.querySelector('.hero-banner');
    if (hero && !reduce && !hero.querySelector('[data-decor-walkers]')) {
        var host = document.createElement('div');
        host.className = 'hero-walkers';
        host.setAttribute('data-decor-walkers', '');
        host.setAttribute('aria-hidden', 'true');
        hero.appendChild(host);
    }

    if (reduce) return;
    var nums = document.querySelectorAll('.stat-value, main > .grid > .p-5[style*="--bg-card"] > :nth-child(2)');
    Array.prototype.forEach.call(nums, function (el) {
        var text = el.textContent.trim();
        if (!/^\d+$/.test(text)) return;
        var end = parseInt(text, 10);
        if (end === 0) return;
        var start = null, dur = Math.min(1400, 500 + end * 20);
        el.textContent = '0';
        function step(t) {
            if (start === null) start = t;
            var p = Math.min(1, (t - start) / dur);
            el.textContent = String(Math.round(end * (1 - Math.pow(1 - p, 3))));
            if (p < 1) requestAnimationFrame(step);
        }
        requestAnimationFrame(step);
    });
})();
