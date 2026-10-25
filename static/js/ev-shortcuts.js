/**
 * ev-shortcuts.js — keyboard shortcuts declared in markup.
 *
 *   <input data-shortcut="/,mod+k" …>            focuses (and selects) the field
 *   <button data-shortcut="n" …>                  clicks the button
 *   data-shortcut-label="Search agents"          name shown in the cheat sheet (falls back to aria-label / title / placeholder / text)
 *   data-shortcut-hint="off"                     no key badge on the element
 *
 * Spec: comma-separated alternatives; each is optional modifiers + a key, e.g. "n", "/", "mod+k", "shift+?".
 * "mod" = Ctrl on Windows/Linux, ⌘ on macOS. Plain-key shortcuts are ignored while you are typing in a field or while a dialog is open.
 * When several elements share a key (the same "n" on different tabs), the first visible one wins.
 * Extras:  "?" shows the cheat sheet · Esc inside a shortcut field clears it, then leaves it · ⌘/Ctrl+Enter submits the open dialog.
 */
(function () {
    'use strict';
    var isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || '');
    var sheet = null;

    function parse(spec) {
        return String(spec || '').split(',').map(function (part) {
            var t = part.trim().toLowerCase().split('+').map(function (x) { return x.trim(); });
            var key = t.pop();
            if (part.trim() === '+') key = '+';
            return { key: key, mod: t.indexOf('mod') > -1, shift: t.indexOf('shift') > -1, alt: t.indexOf('alt') > -1 };
        }).filter(function (s) { return s.key; });
    }
    function visible(el) {
        if (!el || el.disabled) return false;
        var r = el.getClientRects();
        if (!r.length) return false;
        var cs = getComputedStyle(el);
        return cs.visibility !== 'hidden';
    }
    function typing(t) {
        if (!t || !t.tagName) return false;
        return /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable;
    }
    function dialogOpen() {
        var d = document.querySelectorAll('.ev-modal, [role="dialog"]');
        for (var i = 0; i < d.length; i++) if (visible(d[i])) return d[i];
        return null;
    }
    function matches(spec, e) {
        var mod = isMac ? e.metaKey : e.ctrlKey;
        if (spec.mod !== !!mod || spec.alt !== e.altKey) return false;
        if (spec.key === '?') return e.key === '?';
        var k = e.key.toLowerCase();
        if (/^[a-z0-9]$/.test(spec.key) && spec.shift !== e.shiftKey) return false;   // symbols like "/" ignore Shift
        return k === spec.key;
    }
    function label(el) {
        return (el.getAttribute('data-shortcut-label') || el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('placeholder') || el.textContent || '').replace(/\s+/g, ' ').trim();
    }
    function keycap(spec) {
        var k = spec.key.length === 1 ? spec.key.toUpperCase() : spec.key.charAt(0).toUpperCase() + spec.key.slice(1);
        return (spec.mod ? (isMac ? '⌘' : 'Ctrl+') : '') + (spec.alt ? (isMac ? '⌥' : 'Alt+') : '') + (spec.shift ? '⇧' : '') + k;
    }
    function targets() { return Array.prototype.slice.call(document.querySelectorAll('[data-shortcut]')); }

    function activate(el) {
        if (/^(INPUT|TEXTAREA)$/.test(el.tagName)) { el.removeAttribute('readonly'); el.focus(); if (el.select) el.select(); }
        else el.click();
    }

    function addHints() {
        targets().forEach(function (el) {
            if (el._evkHint || el.getAttribute('data-shortcut-hint') === 'off') return;
            var spec = parse(el.getAttribute('data-shortcut'))[0];
            if (!spec) return;
            var kbd = document.createElement('kbd');
            kbd.className = 'ev-kbd';
            kbd.textContent = keycap(spec);
            kbd.setAttribute('aria-hidden', 'true');
            if (/^(INPUT|TEXTAREA)$/.test(el.tagName)) {
                kbd.classList.add('ev-kbd-field');
                el.insertAdjacentElement('afterend', kbd);          // sits inside the field's relative wrapper
            } else {
                kbd.classList.add('ev-kbd-btn');
                el.appendChild(kbd);
            }
            el._evkHint = kbd;
        });
    }

    function closeSheet() { if (sheet) { sheet.remove(); sheet = null; } }
    function showSheet() {
        closeSheet();
        var rows = [];
        targets().forEach(function (el) {
            if (!visible(el)) return;
            rows.push([parse(el.getAttribute('data-shortcut')).map(keycap), label(el)]);
        });
        rows.push([['?'], 'Show this list']);
        rows.push([['Esc'], 'Clear a search field, then leave it']);
        sheet = document.createElement('div');
        sheet.className = 'ev-keys';
        sheet.setAttribute('role', 'dialog');
        sheet.setAttribute('aria-label', 'Keyboard shortcuts');
        sheet.innerHTML = '<p class="ev-keys-t">Keyboard shortcuts</p>' + rows.map(function (r) {
            return '<div class="ev-keys-r"><span class="ev-keys-k">' + r[0].map(function (k) { return '<kbd class="ev-kbd">' + k.replace(/[&<>]/g, '') + '</kbd>'; }).join('') + '</span><span>' + r[1].replace(/[&<>]/g, '') + '</span></div>';
        }).join('');
        document.body.appendChild(sheet);
        setTimeout(function () { document.addEventListener('keydown', closeOnce, true); document.addEventListener('mousedown', closeOnce, true); }, 0);
        function closeOnce(e) {
            if (e.type === 'keydown' && e.key === '?') return;
            document.removeEventListener('keydown', closeOnce, true); document.removeEventListener('mousedown', closeOnce, true);
            closeSheet();
        }
    }

    document.addEventListener('keydown', function (e) {
        if (e.defaultPrevented || e.isComposing) return;
        var t = e.target;
        // Esc in a shortcut field: clear it first, then leave it
        if (e.key === 'Escape' && t && t.hasAttribute && t.hasAttribute('data-shortcut') && /^(INPUT|TEXTAREA)$/.test(t.tagName)) {
            if (t.value) { t.value = ''; t.dispatchEvent(new Event('input', { bubbles: true })); } else t.blur();
            e.preventDefault();
            return;
        }
        // Cmd/Ctrl+Enter submits the open dialog
        if (e.key === 'Enter' && (isMac ? e.metaKey : e.ctrlKey)) {
            var dlg = dialogOpen();
            var btn = dlg && dlg.querySelector('.ev-btn-primary:not(:disabled), .var-btn-primary:not(:disabled)');
            if (btn && visible(btn)) { e.preventDefault(); btn.click(); return; }
        }
        var modKey = isMac ? e.metaKey : e.ctrlKey;
        var plain = !modKey && !e.altKey;
        if (plain && (typing(t) || dialogOpen())) { return; }
        if (e.key === '?' && plain && !typing(t)) { e.preventDefault(); showSheet(); return; }
        var els = targets();
        for (var i = 0; i < els.length; i++) {
            var el = els[i];
            if (!visible(el)) continue;
            var specs = parse(el.getAttribute('data-shortcut'));
            for (var j = 0; j < specs.length; j++) {
                if (matches(specs[j], e)) {
                    if (!specs[j].mod && !specs[j].alt && typing(t)) return;
                    e.preventDefault();
                    activate(el);
                    return;
                }
            }
        }
    });

    window.EvShortcuts = { refresh: addHints, sheet: showSheet };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', addHints); else addHints();
})();
