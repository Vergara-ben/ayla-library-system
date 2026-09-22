/* auth-decor.js: pixel readers walking behind the sign-in pages. Decorative only. */
(function () {
    'use strict';

    // 12 x 16 pixel reader, facing right. Two leg frames for the walk.
    var BODY = [
        '...HHHHH....',
        '..HHHHHHH...',
        '..HSSSSSH...',
        '..HSSSSES...',
        '...SSSSSS...',
        '...TTTTTT...',
        '..TTTTTTWWW.',
        '..TTTTSSWWW.',
        '..TTTTTTBBBB',
        '...TTTTTT...',
        '...PPPPPP...',
        '...PPPPPP...'
    ];
    var LEGS_A = ['...PP..PP...', '..PP....PP..', '..PP....PP..', '.KKK....KKK.'];
    var LEGS_B = ['....PPPP....', '....PP.PP...', '....PP.PP...', '...KKK.KKK..'];
    var FILL = { H: '#3b2417', S: '#f1c27d', E: '#1f2937', T: 'var(--walker-shirt)', W: '#ffffff',
                 B: 'var(--walker-book)', P: '#334155', K: '#111827' };

    function rects(rows, offset) {
        var out = '';
        rows.forEach(function (row, y) {
            for (var x = 0; x < row.length; x++) {
                var c = row.charAt(x);
                if (FILL[c]) {
                    out += '<rect x="' + x + '" y="' + (y + offset) + '" width="1.02" height="1.02" style="fill:' + FILL[c] + '"/>';
                }
            }
        });
        return out;
    }

    function walker(cls) {
        return '<div class="decor-walker ' + cls + '"><svg class="decor-sprite" viewBox="0 0 12 16" shape-rendering="crispEdges" aria-hidden="true">'
            + rects(BODY, 0)
            + '<g class="legs-a">' + rects(LEGS_A, 12) + '</g>'
            + '<g class="legs-b">' + rects(LEGS_B, 12) + '</g>'
            + '</svg></div>';
    }

    function build() {
        if (!document.body || document.querySelector('.auth-decor')) return;
        // A page can host the walkers inside its own element instead of across the screen.
        var host = document.querySelector('[data-decor-walkers]');
        if (host) {
            if (!host.firstChild) host.innerHTML = walker('walk-right') + walker('walk-left');
            return;
        }
        var layer = document.createElement('div');
        layer.className = 'auth-decor';
        layer.setAttribute('aria-hidden', 'true');

        var html = '';
        html += walker('walk-right') + walker('walk-left');
        layer.innerHTML = html;
        document.body.insertBefore(layer, document.body.firstChild);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', build);
    } else {
        build();
    }
})();
