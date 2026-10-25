/**
 * ev-combobox.js — themed replacement for a native <select>, with keyword filtering.
 *
 *   Every single-value <select> on the page is enhanced automatically (also the ones added later by scripts).
 *   Opt out with data-ev-native; <select multiple> and <select size=…> are always left native.
 *   <select id="x" data-ev-combobox>…</select>          still accepted (explicit)
 *   EvCombobox.enhance(selectEl, opts)  ·  EvCombobox.enhanceAll(root)  ·  EvCombobox.refresh(selectEl)  ·  EvCombobox.destroy(selectEl)
 *
 * The native <select> stays in the DOM (hidden) and remains the source of truth, so existing code keeps working:
 * `sel.value = …`, `sel.selectedIndex = …`, `sel.disabled`, options added/removed later, and `change` listeners all behave as before.
 * Picking an option fires a bubbling `change` event on the select.
 *
 * Options (or data-* on the select):
 *   search: 'auto' | true | false   data-ev-search     show the filter box ('auto' = when there are >= searchMin options)
 *   searchMin: number               data-ev-search-min (default 7)
 *   placeholder: string             data-ev-placeholder
 * Per <option>: data-desc="secondary text", data-keywords="extra search terms". <optgroup label> renders as a group heading.
 * Filtering: every whitespace-separated word must appear in label + value + description + keywords (case-insensitive).
 */
(function () {
    'use strict';
    var uid = 0, open = null;
    var CHEV = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>';
    var CHECK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>';
    var SEARCH = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>';

    function el(tag, cls, html) { var e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; }
    function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }

    function enhance(sel, opts) {
        if (!sel || sel._evcb) return sel && sel._evcb;
        opts = opts || {};
        var ds = sel.dataset;
        var cfg = {
            search: opts.search != null ? opts.search : (ds.evSearch === 'false' ? false : ds.evSearch === 'true' ? true : 'auto'),
            searchMin: +(opts.searchMin || ds.evSearchMin || 7),
            placeholder: opts.placeholder || ds.evPlaceholder || 'Select…'
        };
        var id = 'evcb' + (++uid);
        var root = el('div', 'ev-cb');
        var trigger = el('button', 'ev-cb-trigger');
        trigger.type = 'button';
        trigger.setAttribute('role', 'combobox');
        trigger.setAttribute('aria-haspopup', 'listbox');
        trigger.setAttribute('aria-expanded', 'false');
        trigger.setAttribute('aria-controls', id + '-list');
        var label = el('span', 'ev-cb-label');
        trigger.appendChild(label);
        trigger.insertAdjacentHTML('beforeend', '<span class="ev-cb-chev">' + CHEV + '</span>');
        root.appendChild(trigger);
        sel.parentNode.insertBefore(root, sel);
        root.appendChild(sel);
        // mirror the layout the select had (width / flex / margins) onto the wrapper, and a compact look for small selects
        var cls = sel.className || '';
        cls.split(/\s+/).forEach(function (c) { if (/^((sm|md|lg):)?(w-|min-w-|max-w-|flex-|shrink|grow|self-|m[trblxy]?-)/.test(c)) root.classList.add(c); });
        if (/(^|\s)(w-full|flex-1|grow)(\s|$)/.test(cls)) root.classList.add('ev-cb-block');
        if (/(^|\s)(text-xs|p-1|p-1\.5|py-1|py-1\.5|py-0\.5|h-7|h-8|h-9)(\s|$)/.test(cls)) root.classList.add('ev-cb-sm');
        function syncVis() { root.style.display = (sel.hidden || sel.classList.contains('hidden') || sel.style.display === 'none') ? 'none' : ''; }
        sel.classList.add('ev-cb-native');
        sel.tabIndex = -1;
        sel.setAttribute('aria-hidden', 'true');
        // A <label for=sel> should focus the visible control.
        if (sel.id) { var lb = document.querySelector('label[for="' + sel.id + '"]'); if (lb) lb.addEventListener('click', function (e) { e.preventDefault(); trigger.focus(); }); }

        var pop = null, input = null, list = null, items = [], active = -1;

        function selectedOption() { return sel.options[sel.selectedIndex] || null; }
        function optText(o) { return (o.textContent || '').trim(); }
        function paint() {
            var o = selectedOption();
            var t = o ? optText(o) : '';
            label.textContent = t || cfg.placeholder;
            label.classList.toggle('is-placeholder', !t || (o && o.value === '' && !!o.disabled));
            trigger.disabled = sel.disabled;
            root.classList.toggle('is-disabled', sel.disabled);
        }

        function showSearch() { return cfg.search === true || (cfg.search === 'auto' && sel.options.length >= cfg.searchMin); }

        function buildItems(q) {
            var words = (q || '').toLowerCase().split(/\s+/).filter(Boolean);
            list.innerHTML = ''; items = [];
            var lastGroup = null, shown = 0;
            Array.prototype.forEach.call(sel.options, function (o, i) {
                var desc = o.dataset.desc || '', text = optText(o);
                var hay = (text + ' ' + o.value + ' ' + desc + ' ' + (o.dataset.keywords || '')).toLowerCase();
                if (words.length && !words.every(function (w) { return hay.indexOf(w) > -1; })) return;
                var g = o.parentNode && o.parentNode.tagName === 'OPTGROUP' ? o.parentNode.label : null;
                if (g && g !== lastGroup) list.appendChild(el('div', 'ev-cb-group', esc(g)));
                lastGroup = g;
                var it = el('div', 'ev-cb-opt' + (g ? ' in-group' : '') + (i === sel.selectedIndex ? ' is-selected' : '') + (o.disabled ? ' is-disabled' : ''));
                it.id = id + '-o' + i; it.setAttribute('role', 'option'); it.setAttribute('aria-selected', i === sel.selectedIndex ? 'true' : 'false');
                it.dataset.index = i;
                it.innerHTML = '<span class="ev-cb-opt-main"><span class="ev-cb-opt-text">' + hl(text, words) + '</span>' + (desc ? '<span class="ev-cb-opt-desc">' + esc(desc) + '</span>' : '') + '</span><span class="ev-cb-tick">' + CHECK + '</span>';
                list.appendChild(it); items.push(it); shown++;
            });
            if (!shown) list.appendChild(el('div', 'ev-cb-empty', 'No matches'));
            var sIdx = items.findIndex(function (n) { return n.classList.contains('is-selected'); });
            setActive(words.length ? items.findIndex(function (n) { return !n.classList.contains('is-disabled'); }) : (sIdx > -1 ? sIdx : items.findIndex(function (n) { return !n.classList.contains('is-disabled'); })), true);
        }
        function hl(text, words) {
            var out = esc(text);
            words.forEach(function (w) { out = out.replace(new RegExp('(' + w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')(?![^<]*>)', 'ig'), '<mark>$1</mark>'); });
            return out;
        }
        function setActive(i, scroll) {
            if (active > -1 && items[active]) items[active].classList.remove('is-active');
            active = i;
            if (i > -1 && items[i]) {
                items[i].classList.add('is-active');
                trigger.setAttribute('aria-activedescendant', items[i].id);
                if (scroll) items[i].scrollIntoView({ block: 'nearest' });
            } else trigger.removeAttribute('aria-activedescendant');
        }
        function move(d) {
            if (!items.length) return;
            var i = active, n = items.length;
            for (var k = 0; k < n; k++) { i = (i + d + n) % n; if (!items[i].classList.contains('is-disabled')) break; }
            setActive(i, true);
        }
        function choose(i) {
            if (i < 0 || !items[i]) return;
            var idx = +items[i].dataset.index;
            if (sel.options[idx].disabled) return;
            var changed = sel.selectedIndex !== idx;
            setSel(idx);
            close(true);
            if (changed) sel.dispatchEvent(new Event('change', { bubbles: true }));
        }
        var protoIdx = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'selectedIndex');
        function setSel(idx) { protoIdx.set.call(sel, idx); paint(); }

        function place() {
            var r = trigger.getBoundingClientRect(), vh = window.innerHeight, vw = window.innerWidth;
            var below = vh - r.bottom - 12, above = r.top - 12, h = Math.min(pop.scrollHeight, 340);
            var up = below < Math.min(h, 220) && above > below;
            pop.style.left = Math.max(8, Math.min(r.left, vw - Math.max(r.width, 176) - 8)) + 'px';
            pop.style.width = Math.max(r.width, 176) + 'px';
            pop.style.maxHeight = Math.max(140, Math.min(340, up ? above : below)) + 'px';
            if (up) { pop.style.top = 'auto'; pop.style.bottom = (vh - r.top + 6) + 'px'; } else { pop.style.bottom = 'auto'; pop.style.top = (r.bottom + 6) + 'px'; }
            pop.classList.toggle('is-up', up);
        }
        function openPop() {
            if (sel.disabled || pop) return;
            if (open && open !== api) open.close();
            pop = el('div', 'ev-cb-pop'); pop.id = id + '-pop';
            if (showSearch()) {
                var sw = el('div', 'ev-cb-search', SEARCH);
                input = el('input'); input.type = 'text'; input.placeholder = 'Search…'; input.autocomplete = 'off'; input.spellcheck = false;
                input.setAttribute('aria-label', 'Filter options'); input.setAttribute('aria-controls', id + '-list');
                sw.appendChild(input); pop.appendChild(sw);
                input.addEventListener('input', function () { buildItems(input.value); place(); });
            } else input = null;
            list = el('div', 'ev-cb-list'); list.id = id + '-list'; list.setAttribute('role', 'listbox');
            pop.appendChild(list);
            document.body.appendChild(pop);
            buildItems('');
            place();
            void pop.offsetWidth; pop.classList.add('is-in');
            trigger.setAttribute('aria-expanded', 'true'); root.classList.add('is-open');
            if (input) setTimeout(function () { input && input.focus(); }, 0);
            list.addEventListener('mousemove', function (e) { var n = e.target.closest('.ev-cb-opt'); if (n && !n.classList.contains('is-disabled')) { var i = items.indexOf(n); if (i !== active) setActive(i); } });
            list.addEventListener('mousedown', function (e) { e.preventDefault(); });
            list.addEventListener('click', function (e) { var n = e.target.closest('.ev-cb-opt'); if (n) choose(items.indexOf(n)); });
            pop.addEventListener('keydown', onKey);
            open = api;
        }
        function close(refocus) {
            if (!pop) return;
            var p = pop; pop = null; input = null; list = null; items = []; active = -1;
            p.classList.remove('is-in'); setTimeout(function () { p.parentNode && p.parentNode.removeChild(p); }, 120);
            trigger.setAttribute('aria-expanded', 'false'); trigger.removeAttribute('aria-activedescendant'); root.classList.remove('is-open');
            if (open === api) open = null;
            if (refocus) trigger.focus();
        }
        function onKey(e) {
            var k = e.key;
            if (k === 'ArrowDown') { e.preventDefault(); move(1); }
            else if (k === 'ArrowUp') { e.preventDefault(); move(-1); }
            else if (k === 'Home' && !input) { e.preventDefault(); setActive(0, true); }
            else if (k === 'End' && !input) { e.preventDefault(); setActive(items.length - 1, true); }
            else if (k === 'Enter') { e.preventDefault(); choose(active); }
            else if (k === 'Escape') { e.preventDefault(); e.stopPropagation(); close(true); }
            else if (k === 'Tab') close(false);
        }
        var typed = '', typedAt = 0;
        trigger.addEventListener('click', function () { pop ? close(true) : openPop(); });
        trigger.addEventListener('keydown', function (e) {
            if (pop) { onKey(e); if (!input && e.key.length === 1) typeAhead(e.key); return; }
            if (e.key === 'ArrowDown' || e.key === 'ArrowUp' || e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openPop(); }
            else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey) {   // closed: type to jump, like a native select
                var now = Date.now(); typed = (now - typedAt > 700 ? '' : typed) + e.key.toLowerCase(); typedAt = now;
                for (var i = 0; i < sel.options.length; i++) if (!sel.options[i].disabled && optText(sel.options[i]).toLowerCase().indexOf(typed) === 0) { if (sel.selectedIndex !== i) { setSel(i); sel.dispatchEvent(new Event('change', { bubbles: true })); } break; }
            }
        });
        function typeAhead(ch) {
            var now = Date.now(); typed = (now - typedAt > 700 ? '' : typed) + ch.toLowerCase(); typedAt = now;
            var i = items.findIndex(function (n) { return n.textContent.trim().toLowerCase().indexOf(typed) === 0; });
            if (i > -1) setActive(i, true);
        }

        // keep in sync with programmatic changes
        var protoVal = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value');
        Object.defineProperty(sel, 'value', { configurable: true, get: function () { return protoVal.get.call(sel); }, set: function (v) { protoVal.set.call(sel, v); paint(); } });
        Object.defineProperty(sel, 'selectedIndex', { configurable: true, get: function () { return protoIdx.get.call(sel); }, set: function (v) { protoIdx.set.call(sel, v); paint(); } });
        sel.addEventListener('change', paint);
        var mo = new MutationObserver(function () { paint(); syncVis(); if (pop) { buildItems(input ? input.value : ''); place(); } });
        mo.observe(sel, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['disabled', 'selected', 'class', 'style', 'hidden'] });
        var fm = sel.form; if (fm) fm.addEventListener('reset', function () { setTimeout(paint, 0); });

        var api = {
            select: sel, root: root, contains: function (n) { return root.contains(n) || (!!pop && pop.contains(n)); }, open: openPop, close: close, refresh: paint,
            destroy: function () {
                close(); mo.disconnect(); delete sel.value; delete sel.selectedIndex;
                root.parentNode.insertBefore(sel, root); root.remove(); sel.classList.remove('ev-cb-native'); sel.removeAttribute('aria-hidden'); sel.removeAttribute('tabindex'); delete sel._evcb;
            }
        };
        sel._evcb = api;
        paint();
        syncVis();
        return api;
    }

    document.addEventListener('mousedown', function (e) { if (open && !open.contains(e.target)) open.close(); }, true);
    window.addEventListener('resize', function () { if (open) open.close(); });
    window.addEventListener('scroll', function (e) { if (open && !(e.target.closest && e.target.closest('.ev-cb-pop'))) open.close(); }, true);

    var AUTO = 'select:not([multiple]):not([size]):not([data-ev-native]):not(.ev-cb-native)';
    function enhanceAll(root) {
        Array.prototype.forEach.call((root || document).querySelectorAll(AUTO + ', select[data-ev-combobox]'), function (s) { if (!s._evcb && s.parentNode) enhance(s); });
    }
    // code like jQuery .val(x) / option.selected = true changes the selection without touching select.value: keep the label in sync
    (function patchOptionSelected() {
        var od = Object.getOwnPropertyDescriptor(HTMLOptionElement.prototype, 'selected');
        if (!od || !od.set || od.set._evcb) return;
        var set = function (v) {
            od.set.call(this, v);
            var p = this.parentNode; if (p && p.tagName === 'OPTGROUP') p = p.parentNode;
            if (p && p._evcb) p._evcb.refresh();
        };
        set._evcb = true;
        Object.defineProperty(HTMLOptionElement.prototype, 'selected', { configurable: true, enumerable: od.enumerable, get: od.get, set: set });
    })();
    function init() {
        enhanceAll(document);
        // selects rendered later by scripts (modals, settings rows, innerHTML templates)
        var queued = false;
        new MutationObserver(function (muts) {
            for (var i = 0; i < muts.length; i++) {
                var added = muts[i].addedNodes;
                for (var j = 0; j < added.length; j++) {
                    var n = added[j];
                    if (n.nodeType === 1 && (n.tagName === 'SELECT' || n.querySelector('select'))) {
                        if (!queued) { queued = true; (window.requestAnimationFrame || setTimeout)(function () { queued = false; enhanceAll(document); }); }
                        return;
                    }
                }
            }
        }).observe(document.documentElement, { childList: true, subtree: true });
    }
    window.EvCombobox = { enhance: enhance, enhanceAll: enhanceAll, refresh: function (s) { s && s._evcb && s._evcb.refresh(); }, destroy: function (s) { s && s._evcb && s._evcb.destroy(); } };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
