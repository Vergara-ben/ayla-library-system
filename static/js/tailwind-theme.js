/* =====================================================================
   tailwind-theme.js — makes the Tailwind CDN colour utilities follow the
   light/dark theme.

   Load immediately AFTER <script src="https://cdn.tailwindcss.com">.

   The admin and library-staff templates carry ~2,000 hard-coded colour
   utilities (text-slate-400, bg-cyan-950/50, border-violet-700 …) that
   were chosen for a dark background. Rather than rewrite that markup,
   every colour is redefined as a CSS variable, and theme.css swaps the
   variable values per theme. The "R G B / <alpha-value>" form is required
   so slash-opacity classes such as bg-slate-800/50 keep working.

   Only the shades actually used in the templates are declared. Any other
   shade simply falls back to Tailwind's stock colour.
   ===================================================================== */
(function () {
    'use strict';

    function ramp(name, shades) {
        var out = {};
        shades.forEach(function (shade) {
            out[shade] = 'rgb(var(--tw-' + name + '-' + shade + ') / <alpha-value>)';
        });
        return out;
    }

    var FULL = [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950];

    window.tailwind = window.tailwind || {};
    window.tailwind.config = {
        theme: {
            extend: {
                colors: {
                    slate:   ramp('slate',   FULL),
                    cyan:    ramp('cyan',    FULL),
                    violet:  ramp('violet',  FULL),
                    emerald: ramp('emerald', FULL),
                    green:   ramp('green',   FULL),
                    rose:    ramp('rose',    FULL),
                    amber:   ramp('amber',   FULL),
                    red:     ramp('red',     [50, 100, 300, 400, 500, 600, 700, 800, 900, 950]),
                    blue:    ramp('blue',    [50, 100, 300, 400, 500, 600, 700, 800, 900, 950]),
                    teal:    ramp('teal',    [400, 500, 600, 800, 900, 950]),
                    purple:  ramp('purple',  [400, 500, 900, 950]),
                    pink:    ramp('pink',    [400, 500, 900, 950]),
                    indigo:  ramp('indigo',  [400, 500, 900, 950]),
                    yellow:  ramp('yellow',  [400, 500, 600]),
                    orange:  ramp('orange',  [400, 500, 600])
                }
            }
        }
    };
})();
