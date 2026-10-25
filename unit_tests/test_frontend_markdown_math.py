"""Regression tests for the shared KaTeX renderer in ``static/js/markdown-math.js``.

The chat renderer used to pair *any* two ``$`` characters as an inline formula,
even across blank lines and HTML tags. A message quoting cloud prices
(``± $334/bulan`` … ``× $0,04194119``) therefore had whole markdown tables handed
to KaTeX as one formula: the markup was escaped (tags displayed as literal text)
and whitespace was dropped, so tables and headings rendered corrupted.

These tests run the real script in Node with a recording ``katex`` stub and pin
the currency-safe behaviour.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from unit_tests._node_runtime import node_bin


ROOT = Path(__file__).resolve().parents[1]
MATH_JS = ROOT / "static/js/markdown-math.js"

CURRENCY_HTML = (
    '<h2>Ringkasan: \u00b1 <strong>$334/bulan</strong> (\u00b1 Rp 5,3\u20135,5 juta)</h2>\n'
    '<p>Angka ini pakai harga resmi SKU <code>asia-southeast2</code>.</p>\n'
    '<table>\n<thead>\n<tr>\n<th>Komponen</th>\n<th>USD/bulan</th>\n</tr>\n</thead>\n'
    '<tbody>\n<tr>\n<td>Compute (8 vCPU \u00d7 $0,04194119 + 16 GiB \u00d7 $0,004760238)</td>\n'
    '<td>300,54</td>\n</tr>\n<tr>\n<td>Egress internet \u00b128 GB \u00d7 $0,19</td>\n<td>5,32</td>\n</tr>\n'
    '<tr>\n<td><strong>Total</strong></td>\n<td><strong>334,29</strong></td>\n</tr>\n</tbody>\n</table>'
)

_RUNNER_SCRIPT = """\
import { readFileSync } from 'fs';
const source = readFileSync(__MATH_PATH__, 'utf8');

const cases = JSON.parse(readFileSync(0, 'utf8'));
const results = [];

for (const c of cases) {
    const calls = [];
    const katex = {
        renderToString(formula, opts) {
            const display = !!(opts && opts.displayMode);
            calls.push({ formula, display });
            return '<katex-stub data-mode="' + (display ? 'display' : 'inline') + '"></katex-stub>';
        }
    };
    const win = { katex };
    const factory = new Function('window', source + '\\nreturn window.EvMarkdownMath;');
    const api = factory(win);
    results.push({ calls, out: api.render(c.input) });
}

process.stdout.write(JSON.stringify(results));
"""


def _run(cases):
    node = node_bin()
    if not node:
        pytest.skip("Node.js is not available")

    with subprocess.Popen(
        [node, "--input-type=module", "-e", _RUNNER_SCRIPT.replace("__MATH_PATH__", json.dumps(str(MATH_JS)))],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as proc:
        stdout, stderr = proc.communicate(json.dumps(cases), timeout=120)
    assert proc.returncode == 0, stderr
    return json.loads(stdout)


def test_currency_amounts_are_never_treated_as_math():
    [result] = _run([{"input": CURRENCY_HTML}])

    assert result["calls"] == []
    out = result["out"]
    assert out == CURRENCY_HTML
    for tag in ("<table>", "<h2>", "<strong>", "<td>"):
        assert tag in out
    assert "&lt;" not in out


def test_currency_never_spans_blocks_and_loses_whitespace():
    # The exact failure mode: whole sentences handed to KaTeX, which drops spaces.
    [result] = _run([{"input": CURRENCY_HTML}])
    out = result["out"]

    assert "<katex-stub" not in out
    assert "Komponen" in out and "USD/bulan" in out
    assert "Compute mendominasi" not in out  # sanity: fixture text untouched
    assert out.count("<tr>") == 4


def test_inline_formula_still_renders():
    [result] = _run([{"input": "Rumus: $x^2 + y^2$ selesai."}])

    assert len(result["calls"]) == 1
    assert result["calls"][0] == {"formula": "x^2 + y^2", "display": False}
    assert '<katex-stub data-mode="inline">' in result["out"]


def test_display_formula_still_renders():
    [result] = _run([{"input": "<p>$$E = mc^2$$</p>"}])

    assert len(result["calls"]) == 1
    assert result["calls"][0] == {"formula": "E = mc^2", "display": True}
    assert '<katex-stub data-mode="display">' in result["out"]


def test_delimiters_across_blank_lines_are_not_math():
    [result] = _run([{"input": "<p>harga $5</p>\n<p>ongkir $6</p>"}])

    assert result["calls"] == []
    assert result["out"] == "<p>harga $5</p>\n<p>ongkir $6</p>"


def test_code_blocks_are_protected():
    html = "<pre><code>$x^2$</code></pre> dan <code>$a+b$</code>"
    [result] = _run([{"input": html}])

    assert result["calls"] == []
    assert result["out"] == html


def test_closing_dollar_followed_by_digit_never_matches():
    [result] = _run([{"input": "<p>biaya $334 dan $0,19 per GB</p>"}])

    assert result["calls"] == []
    assert result["out"] == "<p>biaya $334 dan $0,19 per GB</p>"


def test_script_is_defensive_when_katex_is_missing():
    node = node_bin()
    if not node:
        pytest.skip("Node.js is not available")

    script = (
        "const src = require('fs').readFileSync(process.env.MATH_JS, 'utf8');"
        "const win = {};"
        "const api = new Function('window', src + '\\nreturn window.EvMarkdownMath;')(win);"
        "process.stdout.write(api.render('<p>$x$</p>'));"
    )
    env = dict(os.environ, MATH_JS=str(MATH_JS))
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=120, env=env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "<p>$x$</p>"
