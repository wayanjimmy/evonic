/**
 * System alert banner — surfaces critical background errors below the navbar.
 *
 * Polls /api/system/alerts on load (and every 60s) and renders persistent
 * dismissible banners. Follows the same pattern as update-banner.js.
 */
(function() {
    'use strict';

    var container = document.getElementById('ev-system-alerts');
    if (!container) return;

    var STYLES = {
        error:   'ev-banner ev-banner-danger',
        warning: 'ev-banner ev-banner-warn',
    };

    function updateHeaderHeight() {
        var header = document.querySelector('header');
        if (header) {
            document.documentElement.style.setProperty('--header-h', header.offsetHeight + 'px');
        }
    }

    function renderAlerts(alerts) {
        container.innerHTML = '';
        if (!alerts || !alerts.length) {
            updateHeaderHeight();
            return;
        }
        alerts.forEach(function(alert) {
            var div = document.createElement('div');
            div.className = STYLES[alert.level] || STYLES.error;
            div.innerHTML =
                '<div class="max-w-[1600px] mx-auto px-4 py-2 flex items-center justify-between text-sm">' +
                    '<div class="flex items-center gap-2">' +
                        '<span>' + escapeHtml(alert.message) + '</span>' +
                    '</div>' +
                    '<button onclick="dismissSystemAlert(\'' + escapeHtml(alert.category) + '\', this)" ' +
                        'class="ml-4 px-2 py-0.5 rounded text-xs font-medium opacity-70 hover:opacity-100 cursor-pointer ' +
                        'border border-current/20">Dismiss</button>' +
                '</div>';
            container.appendChild(div);
        });
        updateHeaderHeight();
    }

    function escapeHtml(str) {
        var d = document.createElement('div');
        d.textContent = str;
        return d.innerHTML;
    }

    window.dismissSystemAlert = function(category, btn) {
        fetch('/api/system/alerts/' + encodeURIComponent(category) + '/dismiss', {method: 'POST'})
            .then(function() {
                var banner = btn.closest('.ev-banner');
                if (banner) banner.remove();
                updateHeaderHeight();
            });
    };

    function fetchAlerts() {
        fetch('/api/system/alerts')
            .then(function(r) { return r.ok ? r.json() : null; })
            .then(function(data) {
                if (data && data.alerts) renderAlerts(data.alerts);
            })
            .catch(function() {});
    }

    document.addEventListener('DOMContentLoaded', function() {
        fetchAlerts();
        setInterval(fetchAlerts, 60000);
    });
})();
