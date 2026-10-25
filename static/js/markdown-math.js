/**
 * markdown-math.js — single KaTeX renderer for already-parsed markdown HTML.
 *
 * Shared by base.html (window.renderMathGlobal) and the chat renderers, so the
 * math rules live in exactly one place.
 *
 * The previous implementation paired any two `$` characters, even across blank
 * lines and HTML tags. A chat message quoting prices ("± $334/bulan" ...
 * "× $0,04194119") therefore had whole tables handed to KaTeX as one formula:
 * KaTeX escaped the markup (tags appeared as literal text) and dropped the
 * whitespace, mangling the message. The rules below keep currency literal:
 *
 *  - inline `$…$`: the opening `$` must not follow a word character, `$` or `\`,
 *    and must not be followed by whitespace or `$`; the formula must stay on a
 *    single line, contain no HTML tag and be at most 240 characters; the closing
 *    `$` must not be preceded by whitespace and must not be followed by a digit
 *    or word character — so `…$0,04194119` can never terminate a formula;
 *  - display `$$…$$`: may span lines, but a body containing a tag close or a
 *    code placeholder is rejected;
 *  - `<pre>` / `<code>` contents are protected before scanning;
 *  - a KaTeX failure always falls back to the original text.
 */
(function (global) {
    'use strict';

    var CODE_PLACEHOLDER = /\u0000CODE\d+\u0000/;
    var INLINE_MATH = /(?<![\w$\\])\$(?![\s$])([^\n<]{1,240}?)(?<!\s)\$(?![\d$\w])/g;
    var DISPLAY_MATH = /\$\$([\s\S]{1,4000}?)\$\$/g;
    var PRE_BLOCK = /<pre\b[^>]*>[\s\S]*?<\/pre>/gi;
    var CODE_BLOCK = /<code\b[^>]*>[\s\S]*?<\/code>/gi;

    function protect(html, store) {
        return html.replace(PRE_BLOCK, function (match) {
            store.push(match);
            return '\u0000CODE' + (store.length - 1) + '\u0000';
        }).replace(CODE_BLOCK, function (match) {
            store.push(match);
            return '\u0000CODE' + (store.length - 1) + '\u0000';
        });
    }

    function renderWith(katex, html) {
        var codeBlocks = [];
        var safe = protect(html, codeBlocks);

        safe = safe.replace(DISPLAY_MATH, function (match, formula) {
            if (formula.indexOf('</') !== -1 || CODE_PLACEHOLDER.test(formula)) return match;
            try {
                return katex.renderToString(formula, { displayMode: true, throwOnError: false });
            } catch (e) {
                return match;
            }
        });

        safe = safe.replace(INLINE_MATH, function (match, formula) {
            if (CODE_PLACEHOLDER.test(formula)) return match;
            try {
                return katex.renderToString(formula, { displayMode: false, throwOnError: false });
            } catch (e) {
                return match;
            }
        });

        return safe.replace(/\u0000CODE(\d+)\u0000/g, function (match, index) {
            return codeBlocks[parseInt(index, 10)] || match;
        });
    }

    function render(html) {
        if (!html) return html || '';
        var katex = global.katex;
        if (typeof katex === 'undefined' || !katex || typeof katex.renderToString !== 'function') {
            return html;
        }
        try {
            return renderWith(katex, html);
        } catch (e) {
            return html;
        }
    }

    global.EvMarkdownMath = { render: render };
})(typeof window !== 'undefined' ? window : this);
