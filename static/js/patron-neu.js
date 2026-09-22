/* patron-neu.js: side decoration and number count-up for the patron pages. Decorative only. */
(function () {
    'use strict';
    var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    var DECOR = '<span class="orb orb-a"></span><span class="orb orb-b"></span>'
        + '<span class="dots dots-l"></span><span class="dots dots-r"></span>'
        + '<span class="tile t1"><i class="fa-solid fa-book-open"></i></span>'
        + '<span class="tile t2"><i class="fa-solid fa-bookmark"></i></span>'
        + '<span class="tile t3"><i class="fa-solid fa-lightbulb"></i></span>'
        + '<span class="tile t4"><i class="fa-solid fa-magnifying-glass"></i></span>'
        + '<span class="tile t5"><i class="fa-solid fa-star"></i></span>'
        + '<span class="tile t6"><i class="fa-solid fa-graduation-cap"></i></span>'
        + '<span class="ring r1"></span><span class="ring r2"></span>';

    // The map fills the whole screen, so it has no side space to decorate.
    function addDecor() {
        if (document.querySelector('.side-decor') || document.getElementById('map')) return;
        var layer = document.createElement('div');
        layer.className = 'side-decor';
        layer.setAttribute('aria-hidden', 'true');
        layer.innerHTML = DECOR;
        document.body.insertBefore(layer, document.body.firstChild);
    }

    function countUp(el) {
        var text = el.textContent.trim();
        if (!/^\d+$/.test(text)) return;
        var end = parseInt(text, 10);
        if (end === 0) return;
        var start = null, dur = Math.min(1200, 500 + end * 60);
        el.textContent = '0';
        function step(t) {
            if (start === null) start = t;
            var p = Math.min(1, (t - start) / dur);
            el.textContent = String(Math.round(end * (1 - Math.pow(1 - p, 3))));
            if (p < 1) requestAnimationFrame(step);
        }
        requestAnimationFrame(step);
    }

    document.addEventListener('DOMContentLoaded', function () {
        if (!document.body.classList.contains('neu-app')) return;
        addDecor();
        if (reduce) return;
        // The patron ID is an identifier, not a count, so it is left alone.
        var nums = document.querySelectorAll('[data-count-up]');
        for (var i = 0; i < nums.length; i++) countUp(nums[i]);
    });
})();
