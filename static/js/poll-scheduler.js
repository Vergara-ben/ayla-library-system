/**
 * poll-scheduler.js — polling that gives up when nobody is looking.
 *
 * The messages pages refresh themselves by asking the server for the thread
 * every few seconds. A fixed setInterval does that forever: a tab left open
 * behind another window, or forgotten on a desk PC overnight, keeps asking all
 * night for a conversation nobody is reading. On a small host that is the
 * whole day's CPU budget spent on an empty room, and the symptom -- every page
 * suddenly slow, for no reason visible on screen -- is miserable to diagnose.
 *
 * So three rules, in order of how much they save:
 *
 *   1. A hidden tab does not poll at all. document.hidden is true whenever the
 *      tab is backgrounded or the window minimised, which is where forgotten
 *      tabs spend their time. Coming back fires one immediate catch-up poll,
 *      so returning to the tab never shows a stale thread.
 *   2. A quiet conversation is polled more slowly. The tiers step down as the
 *      silence grows; anything that counts as activity puts it back on the
 *      fastest tier.
 *   3. A conversation quiet for long enough stops being polled. Sending a
 *      message, or coming back to the tab, starts it again.
 *
 * The fast tier is unchanged from the fixed interval it replaces, so a live
 * conversation feels exactly as it did.
 */
(function () {
  'use strict';

  /**
   * @param {Object} options
   * @param {Function} options.poll      async () => boolean. Resolve true when
   *                                     something changed, which resets to the
   *                                     fast tier. Errors are swallowed: a
   *                                     dropped request is not a reason to stop.
   * @param {Array}   options.tiers      [[idleMsAtLeast, intervalMs], ...],
   *                                     ascending. The last one whose threshold
   *                                     has been passed wins.
   * @param {number}  options.stopAfter  Idle ms after which polling stops.
   * @param {Function} [options.enabled] () => boolean. False means there is
   *                                     nothing to poll for yet -- a patron who
   *                                     has never asked a question, say.
   * @param {Function} [options.clock]   () => ms. Defaults to Date.now. A seam
   *                                     for tests: the ladder is a function of
   *                                     elapsed time, and asserting it against
   *                                     the wall clock means either a slow test
   *                                     or a flaky one.
   */
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

    // setTimeout rather than setInterval, rescheduled after each poll finishes:
    // an interval would stack requests on top of a slow server, which is the
    // worst thing to do to a host that is already struggling.
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
      /** How long the next wait would be, given how quiet it has been.
       *  Exposed so the ladder can be asserted directly rather than inferred
       *  from wall-clock gaps, which a backgrounded tab clamps to a second. */
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
      // Back on screen. Treat that as activity -- someone is reading again --
      // and catch up immediately rather than after a full interval.
      lastActivity = clock();
      running = true;
      if (enabled()) api.now();
    });

    return api;
  }

  window.AylaPoll = AylaPoll;
}());
