/* tailwind-theme.js: maps Tailwind colours to theme variables. */
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
