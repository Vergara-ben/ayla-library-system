/* Two ways to read a QR code, on every screen that reads one.
 *
 * A desk may have a handheld QR scanner plugged into it, or it may have nothing
 * but the computer's own camera, and which one is sitting in front of the staff
 * member changes from desk to desk and from shift to shift. Every scanning
 * screen therefore offers both rather than assuming one: the camera, and the
 * handheld -- which, to a computer, is a keyboard that types the code very fast
 * and presses Enter.
 *
 * The pages keep their own cameras. Each already had one, with its own start and
 * stop, and rewriting four working scanners to share a fifth would risk all of
 * them to save a little duplication. What lives here is only the part that was
 * missing everywhere: the choice between the two inputs, and the handheld's
 * keystroke capture.
 *
 * Usage:
 *
 *     var src = AylaQRSource.attach({
 *         panel: '#book-scan-panel',   // the toggle is inserted at the top of this
 *         camera: '#book-cam-box',     // shown for Camera, hidden for Scanner device
 *         startCamera: startCamera,
 *         stopCamera: stopCamera,
 *         onCode: handleQRCode,
 *         hint: 'Scan the label on the book.',
 *     });
 *
 *     src.activate();     // opening the scan panel
 *     src.deactivate();   // leaving it -- always call this, it frees the camera
 */
(function () {
    'use strict';

    // A handheld scanner types a whole code in well under a tenth of a second;
    // a person cannot. Anything slower than this between keystrokes is treated
    // as someone typing, and the buffer starts again.
    var SCANNER_GAP_MS = 120;
    var MIN_CODE_LENGTH = 3;
    // One card held a moment too long must not scan twice.
    var REPEAT_LOCK_MS = 900;
    var PREF_KEY = 'ayla.qrsource';

    var STYLE_ID = 'ayla-qrsource-style';
    var CSS =
        '.qrsrc{display:flex;gap:4px;padding:3px;border-radius:9px;width:max-content;margin:0 auto 12px;' +
        'border:1px solid var(--border-color,#5e7092);background:rgba(120,130,150,.10)}' +
        '.qrsrc button{font:inherit;font-size:11px;font-weight:700;padding:5px 12px;border-radius:6px;' +
        'border:1px solid transparent;background:transparent;cursor:pointer;display:flex;align-items:center;' +
        'gap:6px;transition:background-color .15s ease,color .15s ease;' +
        'color:var(--text-muted,#94a3b8)}' +
        '.qrsrc button:hover{color:var(--text-main,var(--text-primary,#e9eef7))}' +
        '.qrsrc button.on{background:var(--tint-accent,rgba(96,165,250,.12));color:var(--accent,#60a5fa);' +
        'border-color:var(--accent,#60a5fa)}' +
        '.qrsrc-wait{text-align:center;padding:20px 14px;border-radius:10px;max-width:260px;margin:0 auto;' +
        'border:1px dashed var(--border-color,#5e7092)}' +
        '.qrsrc-wait .qrsrc-icon{font-size:22px;color:var(--accent,#60a5fa);margin-bottom:7px}' +
        '.qrsrc-wait strong{display:block;font-size:12px;color:var(--text-main,var(--text-primary,#e9eef7))}' +
        '.qrsrc-wait small{display:block;font-size:10.5px;color:var(--text-muted,#94a3b8);margin-top:4px;line-height:1.5}' +
        '@media (prefers-reduced-motion: reduce){.qrsrc button{transition:none}}';

    function injectStyle() {
        if (document.getElementById(STYLE_ID)) return;
        var el = document.createElement('style');
        el.id = STYLE_ID;
        el.textContent = CSS;
        document.head.appendChild(el);
    }

    function pick(ref) {
        if (!ref) return null;
        return typeof ref === 'string' ? document.querySelector(ref) : ref;
    }

    // The desk's preference, not the session's: a library with a handheld wants
    // it every time, and re-picking on every transaction is the kind of friction
    // that gets a feature abandoned. Storage can throw (private windows, blocked
    // site data), and a scanner that will not open because of that would be a
    // poor trade, so every access is guarded.
    function readPref() {
        try {
            var v = window.localStorage.getItem(PREF_KEY);
            return (v === 'device' || v === 'camera') ? v : 'camera';
        } catch (e) {
            return 'camera';
        }
    }

    function writePref(mode) {
        try { window.localStorage.setItem(PREF_KEY, mode); } catch (e) { /* not important enough to fail over */ }
    }

    function attach(opts) {
        injectStyle();

        var panel = pick(opts.panel);
        var cameraBox = pick(opts.camera);
        // The page's own status line, if it has one. It is written by the camera
        // ("Starting camera...", "Please allow camera access"), and leaving that
        // standing under a handheld prompt tells the reader to fix a camera they
        // have just chosen not to use.
        var statusLine = pick(opts.status);
        if (!panel) return null;

        var mode = readPref();
        var live = false;             // is the scan panel actually on screen
        var buffer = '';
        var lastKeyAt = 0;
        var lockedUntil = 0;

        // ---- the toggle -------------------------------------------------
        var bar = document.createElement('div');
        bar.className = 'qrsrc';
        bar.setAttribute('role', 'group');
        bar.setAttribute('aria-label', 'How to scan');

        var camBtn = document.createElement('button');
        camBtn.type = 'button';
        camBtn.innerHTML = '<i class="fa-solid fa-camera"></i> Camera';

        var devBtn = document.createElement('button');
        devBtn.type = 'button';
        devBtn.innerHTML = '<i class="fa-solid fa-barcode"></i> Scanner device';

        bar.appendChild(camBtn);
        bar.appendChild(devBtn);
        panel.insertBefore(bar, panel.firstChild);

        // ---- what the handheld shows instead of a camera ----------------
        var wait = document.createElement('div');
        wait.className = 'qrsrc-wait';
        wait.style.display = 'none';
        wait.innerHTML =
            '<div class="qrsrc-icon"><i class="fa-solid fa-barcode"></i></div>' +
            '<strong>Waiting for the scanner…</strong>' +
            '<small>' + (opts.hint || 'Point the handheld scanner at the QR code.') + '</small>';

        if (cameraBox && cameraBox.parentNode) {
            cameraBox.parentNode.insertBefore(wait, cameraBox.nextSibling);
        } else {
            panel.insertBefore(wait, bar.nextSibling);
        }

        function paint() {
            camBtn.classList.toggle('on', mode === 'camera');
            devBtn.classList.toggle('on', mode === 'device');
            camBtn.setAttribute('aria-pressed', String(mode === 'camera'));
            devBtn.setAttribute('aria-pressed', String(mode === 'device'));
            if (cameraBox) cameraBox.style.display = (mode === 'camera') ? '' : 'none';
            wait.style.display = (mode === 'device' && live) ? '' : 'none';
            if (statusLine && mode === 'device') {
                statusLine.textContent = opts.deviceStatus || '';
            }
        }

        function deliver(code) {
            var now = Date.now();
            if (!code || code.length < MIN_CODE_LENGTH || now < lockedUntil) return;
            lockedUntil = now + REPEAT_LOCK_MS;
            if (typeof opts.onCode === 'function') opts.onCode(code);
        }

        // A wedge scanner types into whatever holds focus. Capturing at the
        // document means nothing has to be focused first -- no invisible input
        // stealing the cursor back from a staff member trying to type. When a
        // real field does hold focus the scan lands there instead and the page's
        // own Enter handling applies, so this stays out of the way.
        function onKey(e) {
            if (!live || mode !== 'device') return;
            var t = e.target;
            if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT')) return;

            var now = Date.now();
            if (now - lastKeyAt > SCANNER_GAP_MS) buffer = '';
            lastKeyAt = now;

            if (e.key === 'Enter') {
                var code = buffer.trim();
                buffer = '';
                if (code.length >= MIN_CODE_LENGTH) {
                    e.preventDefault();
                    deliver(code);
                }
                return;
            }
            if (e.key && e.key.length === 1) buffer += e.key;
        }

        document.addEventListener('keydown', onKey, true);

        function setMode(next, remember) {
            if (next !== 'camera' && next !== 'device') return;
            if (next === mode) { paint(); return; }
            mode = next;
            if (remember !== false) writePref(mode);
            buffer = '';
            if (!live) { paint(); return; }
            if (mode === 'device') {
                if (typeof opts.stopCamera === 'function') opts.stopCamera();
            } else {
                if (typeof opts.startCamera === 'function') opts.startCamera();
            }
            paint();
        }

        camBtn.addEventListener('click', function () { setMode('camera'); });
        devBtn.addEventListener('click', function () { setMode('device'); });

        paint();

        return {
            mode: function () { return mode; },
            setMode: setMode,

            activate: function () {
                live = true;
                buffer = '';
                paint();
                if (mode === 'camera' && typeof opts.startCamera === 'function') opts.startCamera();
            },

            // Always stops the camera, whichever mode is showing: leaving the
            // device held open is what keeps the light on and blocks the next
            // page from opening it.
            deactivate: function () {
                live = false;
                buffer = '';
                if (typeof opts.stopCamera === 'function') opts.stopCamera();
                paint();
            },
        };
    }

    window.AylaQRSource = { attach: attach };
}());
