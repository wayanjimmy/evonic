/* AI brand marks (static/img/ai, see the README there): aiLogoHtml(model, size) -> HTML.
   Shared by the chat's Switch model dialog and Settings -> Models. */
(function () {
    if (!document.getElementById('ev-ai-style')) {
        var st = document.createElement('style');
        st.id = 'ev-ai-style';
        st.textContent =
            '.ev-ai{flex:none;display:inline-flex;align-items:center;justify-content:center}' +
            '.ev-ai img{width:100%;height:100%;object-fit:contain}' +
            '.ev-ai-mono{background:currentColor;-webkit-mask-size:contain;mask-size:contain;-webkit-mask-repeat:no-repeat;mask-repeat:no-repeat;-webkit-mask-position:center;mask-position:center}' +
            '.ev-ai-fallback{border-radius:.5rem;background:rgb(148 163 184 / .22);color:#64748b;font-weight:600}' +
            'html.dark .ev-ai-fallback{color:#94a3b8}';
        document.head.appendChild(st);
    }
})();

function _aiEsc(str) {
    return String(str == null ? '' : str).replace(/[&<>"']/g, function (c) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
}

var _AI_LOGO_RULES = [
    [/claude|anthropic/i, 'claude'],
    [/gpt|openai|chatgpt|codex|(^|[\/\s_-])o[134](\b|-)/i, 'openai'],
    [/deepseek/i, 'deepseek'],
    [/gemini|gemma|google/i, 'gemini'],
    [/qwen|qwq|alibaba/i, 'qwen'],
    [/mistral|mixtral|codestral|devstral/i, 'mistral'],
    [/llama|(^|[\/\s_-])meta($|[\/\s_-])/i, 'meta'],
    [/grok|(^|[\/\s_-])xai($|[\/\s_-])/i, 'grok'],
    [/kimi|moonshot/i, 'kimi'],
    [/glm|zhipu|z\.ai/i, 'zhipu'],
    [/minimax/i, 'minimax'],
    [/ollama/i, 'ollama'],
    [/openrouter/i, 'openrouter']
];
var _AI_LOGO_MONO = { openai: 1, grok: 1, ollama: 1, openrouter: 1, kimi: 1 };

// Model text wins (an OpenRouter model named deepseek/... shows DeepSeek); the provider is the fallback.
function _aiLogoKey(m) {
    var texts = [(m.name || '') + ' ' + (m.model_name || ''), (m.provider_name || '') + ' ' + (m.provider || '')];
    for (var t = 0; t < texts.length; t++) {
        for (var i = 0; i < _AI_LOGO_RULES.length; i++) {
            if (_AI_LOGO_RULES[i][0].test(texts[t])) return _AI_LOGO_RULES[i][1];
        }
    }
    return null;
}

function aiLogoHtml(m, size) {
    var key = _aiLogoKey(m);
    var box = 'width:' + size + 'px;height:' + size + 'px;';
    if (!key) {
        var ch = String(m.provider_name || m.name || '?').trim().charAt(0).toUpperCase() || '?';
        return '<span class="ev-ai ev-ai-fallback" style="' + box + 'font-size:' + Math.round(size * 0.46) + 'px">' + _aiEsc(ch) + '</span>';
    }
    var url = '/static/img/ai/' + key + '.svg';
    if (_AI_LOGO_MONO[key]) {
        return '<span class="ev-ai ev-ai-mono" style="' + box + '-webkit-mask-image:url(' + url + ');mask-image:url(' + url + ')" role="img" aria-label="' + key + '"></span>';
    }
    return '<span class="ev-ai" style="' + box + '"><img src="' + url + '" alt="' + key + '" loading="lazy" draggable="false"></span>';
}

