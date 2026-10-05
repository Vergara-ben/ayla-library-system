/* live.js: on a pushed change, refetch this page and swap its [data-live] regions; custom pages listen for "ayla:live". */
(function () {
  'use strict';

  var script = document.currentScript;
  var pageTopics = ((script && script.dataset.topics) || '').split(/[\s,]+/).filter(Boolean);
  if (!pageTopics.length) return;

  var DEBOUNCE_MS = 300;
  var FALLBACK_POLL_MS = 20000;   // only while Pusher is not connected

  var cfg = script.dataset;
  var pollTimer = null;
  var pending = {};
  var pendingOthers = false;   // something in the batch came from another session
  var debounceTimer = null;
  var fetching = false;
  var again = false;
  var heldRegions = {};
  var handlers = [];

  function wanted(topics) {
    return topics.filter(function (t) { return pageTopics.indexOf(t) !== -1; });
  }

  // ---- Pusher ---------------------------------------------------------------

  function connect() {
    if (!cfg.key || !window.Pusher) { startFallbackPoll(); return; }
    var pusher = new window.Pusher(cfg.key, {
      cluster: cfg.cluster,
      forceTLS: true,
      channelAuthorization: { endpoint: cfg.auth, transport: 'ajax' }
    });
    var everConnected = false;
    pusher.connection.bind('state_change', function (s) {
      api.connected = s.current === 'connected';
      if (api.connected) {
        stopFallbackPoll();
        // Anything could have changed while the connection was down.
        if (everConnected) queue(pageTopics);
        everConnected = true;
      } else if (s.current !== 'connecting') {
        startFallbackPoll();
      }
    });
    (cfg.channels || '').split(/\s+/).filter(Boolean).forEach(function (name) {
      pusher.subscribe(name).bind('changed', function (msg) {
        if (msg && msg.topics) queue(wanted(msg.topics), !!cfg.origin && msg.origin === cfg.origin);
      });
    });
  }

  function startFallbackPoll() {
    if (pollTimer) return;
    pollTimer = setInterval(function () { queue(pageTopics); }, FALLBACK_POLL_MS);
  }

  function stopFallbackPoll() {
    clearInterval(pollTimer);
    pollTimer = null;
  }

  // ---- refresh ------------------------------------------------------------

  function queue(topics, self) {
    if (!topics.length) return;
    if (!self) pendingOthers = true;
    topics.forEach(function (t) { pending[t] = true; });
    if (document.hidden) return;   // caught up on visibilitychange
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(flush, DEBOUNCE_MS);
  }

  function flush() {
    var topics = Object.keys(pending);
    if (!topics.length) return;
    if (fetching) { again = true; return; }
    var self = !pendingOthers;
    pending = {};
    pendingOthers = false;

    // detail.self: every change in this batch was made from this browser session.
    var ev = new CustomEvent('ayla:live', { detail: { topics: topics, self: self }, cancelable: true });
    document.dispatchEvent(ev);
    handlers.forEach(function (h) {
      var hit = topics.filter(function (t) { return h.topics.indexOf(t) !== -1; });
      if (hit.length) { try { h.fn(hit); } catch (err) { console.error(err); } }
    });
    if (ev.defaultPrevented || !document.querySelector('[data-live]')) return;

    fetching = true;
    swapRegions().catch(function () { /* offline; the next change catches up */ })
      .then(function () {
        fetching = false;
        if (again) { again = false; flush(); }
      });
  }

  function swapRegions() {
    return fetch(location.href, {
      headers: { 'X-Ayla-Live': '1' },
      credentials: 'same-origin',
      cache: 'no-store'
    }).then(function (res) {
      // Signed out, or the account was closed: show the real page.
      if (res.redirected && new URL(res.url).pathname !== location.pathname) {
        location.reload();
        return null;
      }
      return res.ok ? res.text() : null;
    }).then(function (html) {
      if (!html) return;
      var fresh = new DOMParser().parseFromString(html, 'text/html');
      document.querySelectorAll('[data-live]').forEach(function (el) {
        var key = el.getAttribute('data-live');
        var next = fresh.querySelector('[data-live="' + key + '"]');
        if (!next) return;
        if (isBusy(el)) { heldRegions[key] = next; return; }
        apply(el, next);
      });
    });
  }

  function apply(el, next) {
    delete heldRegions[el.getAttribute('data-live')];
    var changed = false;
    // data-live-attrs: the region's own class (open/closed, overdue...) follows too.
    if (el.hasAttribute('data-live-attrs') && el.className !== next.className) {
      el.className = next.className;
      changed = true;
    }
    if (el.innerHTML !== next.innerHTML) {
      el.innerHTML = next.innerHTML;
      changed = true;
    }
    if (changed) el.dispatchEvent(new CustomEvent('ayla:live-swapped', { bubbles: true }));
  }

  // Never pull a field out from under someone typing in it, or throw away
  // what they ticked or typed but have not saved yet.
  function isBusy(el) {
    if (el.hasAttribute('data-live-hold')) return true;
    var a = document.activeElement;
    if (a && a !== document.body && el.contains(a) &&
        (a.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName))) return true;
    return isDirty(el);
  }

  function isDirty(el) {
    var fields = el.querySelectorAll('input, textarea, select');
    for (var i = 0; i < fields.length; i++) {
      var f = fields[i];
      if (f.type === 'hidden' || f.hasAttribute('data-live-ignore')) continue;
      if (f.tagName === 'SELECT') {
        for (var j = 0; j < f.options.length; j++) {
          if (f.options[j].selected !== f.options[j].defaultSelected) return true;
        }
      } else if (f.type === 'checkbox' || f.type === 'radio') {
        if (f.checked !== f.defaultChecked) return true;
      } else if (f.value !== f.defaultValue) {
        return true;
      }
    }
    return false;
  }

  function releaseHeld() {
    Object.keys(heldRegions).forEach(function (key) {
      var el = document.querySelector('[data-live="' + key + '"]');
      if (el && !isBusy(el)) apply(el, heldRegions[key]);
    });
  }

  document.addEventListener('focusout', function () { setTimeout(releaseHeld, 0); });
  document.addEventListener('change', function () { setTimeout(releaseHeld, 0); });
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden && Object.keys(pending).length) flush();
  });

  var api = window.AylaLive = {
    connected: false,
    topics: pageTopics,
    /** Run fn(topicsThatChanged) whenever one of these topics changes. */
    on: function (topics, fn) {
      handlers.push({ topics: [].concat(topics), fn: fn });
    },
    /** Fetch and swap now, e.g. after a page's own action. */
    refresh: function () { queue(pageTopics); },
    /** Apply a region held back while it was busy (call after unsetting data-live-hold). */
    release: releaseHeld
  };

  connect();
})();
