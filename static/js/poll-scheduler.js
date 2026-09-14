/* poll-scheduler.js: polling that slows down and stops when idle. */
(function () {
  'use strict';

  /* * * @param {Object} options * @param {Function} options.poll async () => boolean. */
  function AylaPoll(options) {
    var tiers = options.tiers;
    var stopAfter = options.stopAfter;
    var enabled = options.enabled || function () { return true; };
    var clock = options.clock || Date.now;
    var timer = null;
    var lastActivity = clock();
    var running = false;

    function idleFor() {
      return clock() - lastActivity;
    }

    function interval() {
      var idle = idleFor();
      var chosen = tiers[0][1];
      for (var i = 0; i < tiers.length; i++) {
        if (idle >= tiers[i][0]) chosen = tiers[i][1];
      }
      return chosen;
    }

    function clear() {
      if (timer) { clearTimeout(timer); timer = null; }
    }

    // Schedule the next poll after this one finishes.
    function schedule() {
      clear();
      if (!running || document.hidden) return;
      if (!enabled()) return;
      if (idleFor() >= stopAfter) { running = false; return; }
      timer = setTimeout(function () {
        Promise.resolve()
          .then(options.poll)
          .then(function (changed) { if (changed) lastActivity = clock(); })
          .catch(function () { /* offline; the next tick catches up */ })
          .then(schedule);
      }, interval());
    }

    var api = {
      start: function () {
        running = true;
        schedule();
        return api;
      },
      stop: function () {
        running = false;
        clear();
        return api;
      },
      /** Something happened: back to the fast tier, and running again. */
      bump: function () {
        lastActivity = clock();
        if (!running) running = true;
        schedule();
        return api;
      },
      /* * How long the next wait would be, given how quiet it has been. */
      nextDelay: function () { return running ? interval() : null; },
      /** Poll once right now, whatever the schedule says. */
      now: function () {
        return Promise.resolve()
          .then(options.poll)
          .then(function (changed) { if (changed) lastActivity = clock(); })
          .catch(function () {})
          .then(schedule);
      },
    };

    document.addEventListener('visibilitychange', function () {
      if (document.hidden) {
        clear();
        return;
      }
      // Back on screen.
      lastActivity = clock();
      running = true;
      if (enabled()) api.now();
    });

    return api;
  }

  window.AylaPoll = AylaPoll;
}());
