/* session.js: when a background request is sent to sign-in, say so and go there. */
(function () {
    'use strict';
    if (!window.fetch) return;

    var SIGN_IN = /\/(admin-portal|library-staff|patron)\/login\/?$/;
    var original = window.fetch;
    var leaving = false;

    window.fetch = function () {
        return original.apply(this, arguments).then(function (response) {
            try {
                if (!leaving && response.redirected && SIGN_IN.test(new URL(response.url).pathname)) {
                    leaving = true;
                    var toast = window.AylaDialog && window.AylaDialog.toast;
                    if (toast) toast('Please sign in to continue. Your session may have ended.');
                    setTimeout(function () { window.location.href = response.url; }, toast ? 1800 : 0);
                }
            } catch (e) { /* leave the response as it is */ }
            return response;
        });
    };
})();
