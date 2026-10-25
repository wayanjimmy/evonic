/**
 * ev-toast.js — the one toast system for every page.
 *
 *   evToast(message, type = 'success', opts)     type: 'success' | 'error' | 'warning' | 'info'
 *   showToast(message, typeOrIsError)            legacy signature, kept so existing call sites work unchanged
 *
 * Glass pill under the navbar, status icon, close button, hover pauses the timer, identical messages are merged into a
 * "×N" counter instead of stacking. Styles live in style.css (.ev-toast*). No dependencies.
 */
(function () {
    'use strict';
    var MAX = 4, DEFAULT_MS = 3200, MERGE_MS = 2500;
    var ICONS = {
        success: '<path d="M20 6 9 17l-5-5"/>',
        error: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
        warning: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
        info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>'
    };
    var host = null;

    function ensureHost() {
        if (host && document.body.contains(host)) return host;
        host = document.createElement('div');
        host.id = 'ev-toasts';
        host.className = 'ev-toasts';
        host.setAttribute('role', 'region');
        host.setAttribute('aria-label', 'Notifications');
        document.body.appendChild(host);
        return host;
    }

    function normalizeType(t) {
        if (t === true) return 'error';
        if (t === false || t == null) return 'success';
        t = String(t).toLowerCase();
        if (t === 'success' || t === 'error' || t === 'warning' || t === 'info') return t;
        if (t === 'danger' || t === 'err' || t === 'fail' || t === 'failed') return 'error';
        if (t === 'warn') return 'warning';
        return 'info';
    }

    function dismiss(el) {
        if (!el || el._gone) return;
        el._gone = true;
        clearTimeout(el._timer);
        el.classList.remove('is-in');
        el.classList.add('is-out');
        setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 220);
    }

    function arm(el, ms) {
        clearTimeout(el._timer);
        el._ms = ms;
        el._timer = setTimeout(function () { dismiss(el); }, ms);
    }

    function evToast(message, type, opts) {
        opts = opts || {};
        type = normalizeType(type);
        var text = String(message == null ? '' : message);
        var ms = opts.duration || (type === 'error' ? DEFAULT_MS + 1800 : DEFAULT_MS);
        var root = ensureHost();

        // Same message already on screen (and recent)? Count it instead of stacking a twin.
        var kids = root.children;
        for (var i = 0; i < kids.length; i++) {
            var k = kids[i];
            if (!k._gone && k._text === text && k._type === type && Date.now() - k._at < MERGE_MS) {
                k._n = (k._n || 1) + 1;
                k._at = Date.now();
                var badge = k.querySelector('.ev-toast-n');
                badge.textContent = '×' + k._n;
                badge.hidden = false;
                k.classList.remove('is-bump'); void k.offsetWidth; k.classList.add('is-bump');
                arm(k, ms);
                return k;
            }
        }
        var live = Array.prototype.filter.call(root.children, function (c) { return !c._gone; });
        while (live.length >= MAX) dismiss(live.shift());

        var el = document.createElement('div');
        el.className = 'ev-toast ev-toast-' + type;
        el.setAttribute('role', type === 'error' ? 'alert' : 'status');
        el._text = text; el._type = type; el._at = Date.now(); el._n = 1;

        var ic = document.createElement('span');
        ic.className = 'ev-toast-ic';
        ic.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + ICONS[type] + '</svg>';
        var msg = document.createElement('span');
        msg.className = 'ev-toast-msg';
        msg.textContent = text;
        var n = document.createElement('span');
        n.className = 'ev-toast-n'; n.hidden = true;
        var x = document.createElement('button');
        x.type = 'button'; x.className = 'ev-toast-x'; x.setAttribute('aria-label', 'Dismiss');
        x.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>';
        x.addEventListener('click', function (e) { e.stopPropagation(); dismiss(el); });
        el.appendChild(ic); el.appendChild(msg); el.appendChild(n); el.appendChild(x);
        el.addEventListener('mouseenter', function () { clearTimeout(el._timer); });
        el.addEventListener('mouseleave', function () { arm(el, 1600); });

        root.appendChild(el);
        requestAnimationFrame(function () { requestAnimationFrame(function () { el.classList.add('is-in'); }); });
        arm(el, ms);
        return el;
    }

    window.evToast = evToast;
    window.evToast.dismissAll = function () { if (host) Array.prototype.slice.call(host.children).forEach(dismiss); };
    // Legacy entry points: showToast(msg, 'success'|'error'|'info') and showToast(msg, isError)
    window.showToast = function (message, typeOrIsError, duration) {
        return evToast(message, typeOrIsError, duration ? { duration: duration } : undefined);
    };
})();
