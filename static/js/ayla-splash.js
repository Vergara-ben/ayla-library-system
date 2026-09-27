/* ayla-splash.js: 4s welcome + pixel loading screen for the patron auth pages.
   Purely decorative: it overlays the page, then removes itself. */
(function () {
    'use strict';

    var WELCOME_MS = 1700;   /* phase 1: welcome         */
    var LOADING_MS = 2300;   /* phase 2: pixel loading   */
    var FADE_MS    = 350;    /* overlap of the fade-out  */

    /* Plays on every load, refresh included. Only a reduced-motion request skips it. */
    var reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduced) { return; }

    /* --- pixel sprite -------------------------------------------------- */

    /* One character cell per pixel. Rows 0-17 are shared; the legs swap per frame. */
    var BODY = [
        '........................',
        '.....HHHHHH.............',
        '....HHHHHHHH............',
        '....HHSSSSSH............',
        '....HHSSSESS............',
        '....HHSSSSSS............',
        '.....SSSSSS.............',
        '......SSSS..............',
        '....BBBBBBBB............',
        '....BBBBBBBB..111111WW..',
        '....BBBBBBBB..111111WW..',
        '....BBBBBBBBS222222WW...',
        '....BBBBBBBBS222222WW...',
        '....BBBBBBBBS.333333WW..',
        '....BBBBBBBB..333333WW..',
        '....PPPPPPPP............',
        '....PPPPPPPP............',
        '....PPPPPPPP............'
    ];
    var LEGS_A = [
        '....PPP..PPP............',
        '...PPP....PPP...........',
        '...PPP.....PPP..........',
        '..OOOO.....OOOO.........'
    ];
    var LEGS_B = [
        '....PPPPPP..............',
        '....PPP.PPP.............',
        '....PPP.PPP.............',
        '...OOOO.OOOO............'
    ];

    var SVG_NS = 'http://www.w3.org/2000/svg';

    /* Paint a grid into a <g>, merging runs of one colour into a single rect. */
    function drawGrid(group, rows, yOffset) {
        rows.forEach(function (row, y) {
            var x = 0;
            while (x < row.length) {
                var ch = row.charAt(x);
                if (ch === '.') { x++; continue; }
                var run = 1;
                while (x + run < row.length && row.charAt(x + run) === ch) { run++; }
                var rect = document.createElementNS(SVG_NS, 'rect');
                rect.setAttribute('x', x);
                rect.setAttribute('y', y + yOffset);
                rect.setAttribute('width', run);
                rect.setAttribute('height', 1);
                rect.setAttribute('data-c', ch);
                group.appendChild(rect);
                x += run;
            }
        });
    }

    function buildSprite() {
        var svg = document.createElementNS(SVG_NS, 'svg');
        svg.setAttribute('viewBox', '0 0 24 22');
        svg.setAttribute('class', 'ayla-px');
        svg.setAttribute('aria-hidden', 'true');
        svg.setAttribute('focusable', 'false');

        ['a', 'b'].forEach(function (key) {
            var g = document.createElementNS(SVG_NS, 'g');
            g.setAttribute('class', 'ayla-px__frame ayla-px__frame--' + key);
            drawGrid(g, BODY, 0);
            drawGrid(g, key === 'a' ? LEGS_A : LEGS_B, 18);
            svg.appendChild(g);
        });
        return svg;
    }

    /* --- overlay ------------------------------------------------------- */

    var host = document.currentScript;
    /* theme.js has already set data-theme; pick the matching logo ourselves,
       since this overlay is built after its swap pass has run. */
    var dark = document.documentElement.getAttribute('data-theme') === 'dark';
    var logo = (host && host.getAttribute(dark ? 'data-logo-dark' : 'data-logo-light')) || '';

    var overlay = document.createElement('div');
    overlay.className = 'ayla-splash';
    /* Decorative: the page underneath stays the accessible content. */
    overlay.setAttribute('role', 'presentation');
    overlay.setAttribute('aria-hidden', 'true');

    var drift = '';
    [
        { c: 'var(--px-book1)', top: '12px',  delay: '0s'   },
        { c: 'var(--px-book3)', top: '52px',  delay: '1.1s' },
        { c: 'var(--px-book2)', top: '30px',  delay: '2s'   }
    ].forEach(function (b) {
        drift += '<i class="ayla-splash__book" style="top:' + b.top +
                 ';background:' + b.c + ';animation-delay:' + b.delay + '"></i>';
    });

    overlay.innerHTML =
        '<div class="ayla-splash__stage">' +
            '<div class="ayla-splash__phase is-active" data-phase="welcome">' +
                (logo ? '<img class="ayla-splash__logo" src="' + logo + '" alt="">' : '') +
                '<div class="ayla-splash__hello">Welcome to</div>' +
                '<div class="ayla-splash__name">AYLA <span>Library</span></div>' +
                '<div class="ayla-splash__rule"></div>' +
            '</div>' +
            '<div class="ayla-splash__phase" data-phase="loading">' +
                '<div class="ayla-splash__scene">' +
                    '<div class="ayla-splash__drift">' + drift + '</div>' +
                    '<div class="ayla-splash__walker"></div>' +
                    '<div class="ayla-splash__floor"></div>' +
                '</div>' +
                '<div class="ayla-splash__caption">Loading your library&hellip;</div>' +
                '<div class="ayla-splash__bar"><div class="ayla-splash__fill"></div></div>' +
            '</div>' +
        '</div>';

    overlay.querySelector('.ayla-splash__walker').appendChild(buildSprite());

    function mount() {
        document.body.appendChild(overlay);
        var welcome = overlay.querySelector('[data-phase="welcome"]');
        var loading = overlay.querySelector('[data-phase="loading"]');

        /* Hand over to the pixel loader. */
        setTimeout(function () { welcome.classList.add('is-out'); }, WELCOME_MS - 450);
        setTimeout(function () {
            welcome.classList.remove('is-active', 'is-out');
            loading.classList.add('is-active');
        }, WELCOME_MS);

        /* Fade so the form is uncovered exactly at WELCOME_MS + LOADING_MS. */
        setTimeout(function () { overlay.classList.add('is-leaving'); },
                   WELCOME_MS + LOADING_MS - FADE_MS);
        setTimeout(function () {
            if (overlay.parentNode) { overlay.parentNode.removeChild(overlay); }
        }, WELCOME_MS + LOADING_MS);
    }

    if (document.body) { mount(); }
    else { document.addEventListener('DOMContentLoaded', mount); }
}());
