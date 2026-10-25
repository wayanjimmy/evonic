/**
 * Toast Notification Module (legacy API)
 * Usage: toast.show(message, type, duration)  ·  toast.success / error / warning / info(message, duration)  ·  toast.clear()
 * Everything is rendered by the shared themed system in ev-toast.js (loaded globally by base.html).
 */
(function () {
    'use strict';
    function show(message, type, duration) {
        return window.evToast(message, type || 'info', duration ? { duration: duration } : undefined);
    }
    window.toast = {
        show: show,
        success: function (m, d) { return show(m, 'success', d); },
        error: function (m, d) { return show(m, 'error', d); },
        warning: function (m, d) { return show(m, 'warning', d); },
        info: function (m, d) { return show(m, 'info', d); },
        clear: function () { window.evToast.dismissAll(); },
        configure: function () { /* position/limits are fixed by the shared system */ }
    };
})();
