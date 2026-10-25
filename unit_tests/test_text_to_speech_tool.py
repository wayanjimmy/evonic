"""Coverage for the Text to Speech skill tool backend and providers."""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
import wave
from io import BytesIO
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT_DIR / "skills" / "text-to-speech"
BACKEND_DIR = SKILL_DIR / "backend"


def _load_tool_module():
    package_name = "test_text_to_speech_backend"
    for name in tuple(sys.modules):
        if name == package_name or name.startswith(f"{package_name}."):
            del sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        package_name, BACKEND_DIR / "__init__.py", submodule_search_locations=[str(BACKEND_DIR)],
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package
    spec.loader.exec_module(package)
    return __import__(f"{package_name}.tools.generate_speech", fromlist=["execute"])


def _config(**overrides):
    base = {"allow_agent_overrides": True, "default_provider": "mock", "allow_local_providers": True, "mock_enabled": True}
    base.update(overrides)
    return base


@pytest.fixture
def tts_tool(monkeypatch, tmp_path):
    tool = _load_tool_module()
    import backend.tools.save_artifact as save_artifact

    monkeypatch.setattr(save_artifact, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr("backend.skills_manager.skills_manager.get_skill_config", lambda _id: _config())
    return tool


def test_manifest_and_tools_are_consistent():
    manifest = json.loads((SKILL_DIR / "skill.json").read_text())
    tools = json.loads((SKILL_DIR / "tools.json").read_text())
    assert manifest["id"] == "text-to-speech"
    assert tools[0]["function"]["name"] == "generate_speech"
    assert (BACKEND_DIR / "tools" / "generate_speech.py").exists()
    ids = {p["id"] for p in manifest["settings_ui"]["provider_selector"]["providers"]}
    assert ids == {"google-gemini", "openai", "openrouter", "elevenlabs", "mock"}
    for variable in manifest["variables"]:
        if variable.get("provider"):
            assert variable["provider"] in ids


def test_generate_speech_persists_valid_wav(tts_tool, tmp_path):
    result = tts_tool.execute({"id": "tts-agent"}, {"text": "Halo dunia", "voice": "mock-voice"})

    assert result["status"] == "success"
    assert result["provider"] == "mock"
    artifact = result["artifact"]
    assert artifact["mime_type"] == "audio/wav"
    assert artifact["filename"].startswith("speech-mock-")
    assert artifact["duration_seconds"] > 0
    persisted = tmp_path / "shared" / "agents" / "tts-agent" / "artifacts" / artifact["filename"]
    with wave.open(BytesIO(persisted.read_bytes())) as wav:
        assert wav.getnframes() > 0


def test_no_implicit_provider_and_disabled_provider(tts_tool, monkeypatch):
    monkeypatch.setattr("backend.skills_manager.skills_manager.get_skill_config", lambda _id: _config(default_provider=""))
    assert tts_tool.execute({"id": "a"}, {"text": "x"})["error_code"] == "provider_configuration"

    monkeypatch.setattr("backend.skills_manager.skills_manager.get_skill_config", lambda _id: _config(mock_enabled=False))
    assert tts_tool.execute({"id": "a"}, {"text": "x"})["error_code"] == "provider_disabled"

    monkeypatch.setattr("backend.skills_manager.skills_manager.get_skill_config", lambda _id: _config(allow_local_providers=False))
    assert tts_tool.execute({"id": "a"}, {"text": "x"})["error_code"] == "provider_disabled"


@pytest.mark.parametrize("args", [
    {"text": ""},
    {"text": "x" * 4001},
    {"text": "x", "speed": 9},
    {"text": "x", "output_format": "mp3"},   # mock only supports wav
    {"text": "x", "voice": "nope"},
    {"text": "x", "provider": "missing"},
])
def test_invalid_requests_are_rejected(tts_tool, args):
    assert tts_tool.execute({"id": "a"}, args)["status"] == "error"


def test_character_quota_is_enforced(tts_tool, monkeypatch):
    monkeypatch.setattr("backend.skills_manager.skills_manager.get_skill_config", lambda _id: _config(characters_per_day=5))
    assert tts_tool.execute({"id": "quota"}, {"text": "abc"})["status"] == "success"
    assert tts_tool.execute({"id": "quota"}, {"text": "abcd"})["error_code"] == "quota_exceeded"


def test_gemini_wraps_pcm_into_wav(tts_tool, monkeypatch):
    providers = tts_tool._providers
    gemini = providers.provider_registry.get("google-gemini")
    pcm = b"\x01\x00" * 2400
    captured = {}

    def fake_json(self, method, path, payload=None, *, headers=None):
        captured.update(path=path, payload=payload, headers=headers)
        return {"candidates": [{"content": {"parts": [{"inlineData": {
            "mimeType": "audio/L16;codec=pcm;rate=24000", "data": base64.b64encode(pcm).decode()}}]}}]}

    monkeypatch.setattr(providers.google.BoundedHttpClient, "json", fake_json)
    monkeypatch.setattr("backend.skills_manager.skills_manager.get_skill_config", lambda _id: {
        "allow_agent_overrides": True, "default_provider": "google-gemini", "google_gemini_enabled": True, "google_gemini_api_key": "k"})

    result = tts_tool.execute({"id": "g"}, {"text": "hello", "voice": "Puck"})

    assert result["status"] == "success", result
    assert result["voice"] == "Puck"
    assert captured["path"].endswith(":generateContent")
    assert captured["payload"]["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"] == "Puck"
    assert captured["headers"] == {"x-goog-api-key": "k"}
    assert result["artifact"]["duration_seconds"] == 0.1
    assert gemini.id == "google-gemini"


def test_openai_requests_mp3(tts_tool, monkeypatch):
    providers = tts_tool._providers
    captured = {}

    def fake_request(self, method, path, *, body=None, headers=None, max_bytes=0):
        captured.update(path=path, body=json.loads(body), headers=headers)
        return b"ID3\x04\x00\x00" + b"\x00" * 64, "audio/mpeg"

    monkeypatch.setattr(providers.openai.BoundedHttpClient, "request", fake_request)
    monkeypatch.setattr("backend.skills_manager.skills_manager.get_skill_config", lambda _id: {
        "allow_agent_overrides": True, "default_provider": "openai", "openai_enabled": True, "openai_api_key": "sk"})

    result = tts_tool.execute({"id": "o"}, {"text": "hello", "speed": 1.2, "instructions": "calm"})

    assert result["status"] == "success", result
    assert result["artifact"]["mime_type"] == "audio/mpeg"
    assert captured["path"] == "/audio/speech"
    assert captured["body"]["response_format"] == "mp3"
    assert captured["body"]["speed"] == 1.2
    assert captured["body"]["instructions"] == "calm"
    assert captured["headers"]["Authorization"] == "Bearer sk"


def test_openrouter_enforces_model_allowlist_and_sends_mp3(tts_tool, monkeypatch):
    providers = tts_tool._providers
    captured = {}

    def fake_request(self, method, path, *, body=None, headers=None, max_bytes=0):
        captured.update(path=path, body=json.loads(body), headers=headers)
        return b"ID3\x04\x00\x00" + b"\x00" * 64, "audio/mpeg"

    monkeypatch.setattr(providers.openrouter.BoundedHttpClient, "request", fake_request)
    config = {"allow_agent_overrides": True, "default_provider": "openrouter", "openrouter_enabled": True, "openrouter_api_key": "or-key",
              "openrouter_allowed_models": "mistralai/voxtral-mini-tts-2603"}
    monkeypatch.setattr("backend.skills_manager.skills_manager.get_skill_config", lambda _id: config)

    ok = tts_tool.execute({"id": "r"}, {"text": "hi", "model": "mistralai/voxtral-mini-tts-2603", "voice": "en_paul_neutral", "speed": 1.5})
    assert ok["status"] == "success", ok
    assert ok["artifact"]["mime_type"] == "audio/mpeg"
    assert captured["path"] == "/audio/speech"
    assert captured["body"] == {"model": "mistralai/voxtral-mini-tts-2603", "input": "hi",
                                "voice": "en_paul_neutral", "response_format": "mp3", "speed": 1.5}
    assert captured["headers"]["Authorization"] == "Bearer or-key"

    default = tts_tool.execute({"id": "r"}, {"text": "hi"})
    assert captured["body"]["model"] == "hexgrad/kokoro-82m"
    assert captured["body"]["voice"] == "am_michael"
    assert default["status"] == "success"

    assert tts_tool.execute({"id": "r"}, {"text": "hi", "model": "evil/model"})["error_code"] == "invalid_request"
    assert tts_tool.execute({"id": "r"}, {"text": "hi", "voice": "../x"})["error_code"] == "invalid_request"
    # Free-form voices are rejected, with the approved list in the message.
    rejected = tts_tool.execute({"id": "r"}, {"text": "hi", "voice": "made_up_voice"})
    assert rejected["error_code"] == "invalid_request"
    assert "Approved voices:" in rejected["error"] and "Puck" in rejected["error"]
    config["openrouter_allowed_voices"] = "jf_alpha"
    assert tts_tool.execute({"id": "r"}, {"text": "hi", "voice": "jf_alpha"})["status"] == "success"
    assert captured["body"]["voice"] == "jf_alpha"

    hinted = tts_tool.execute({"id": "r"}, {"text": "hi", "instructions": "calm"})
    assert hinted["status"] == "success"
    assert "instructions" not in captured["body"]
    assert hinted["warnings"] == ["instructions were ignored: the selected provider does not support them."]


def _elevenlabs_config(**overrides):
    config = {"allow_agent_overrides": True, "default_provider": "elevenlabs", "elevenlabs_enabled": True, "elevenlabs_api_key": "xi-key"}
    config.update(overrides)
    return config


def test_elevenlabs_sends_mp3_request_with_voice_settings(tts_tool, monkeypatch):
    providers = tts_tool._providers
    captured = {}

    def fake_request(self, method, path, *, body=None, headers=None, max_bytes=0):
        captured.update(method=method, path=path, body=json.loads(body), headers=headers)
        return b"ID3\x04\x00\x00" + b"\x00" * 64, "audio/mpeg"

    monkeypatch.setattr(providers.elevenlabs.BoundedHttpClient, "request", fake_request)
    config = _elevenlabs_config()
    monkeypatch.setattr("backend.skills_manager.skills_manager.get_skill_config", lambda _id: config)

    result = tts_tool.execute({"id": "el"}, {"text": "Halo [whispers] dunia"})
    assert result["status"] == "success", result
    assert result["artifact"]["mime_type"] == "audio/mpeg"
    assert captured["method"] == "POST"
    assert captured["path"] == "/v1/text-to-speech/o5s6XRBkPSTD4syv6mZg?output_format=mp3_44100_128"
    assert captured["headers"]["xi-api-key"] == "xi-key"
    assert captured["headers"]["Accept"] == "audio/*"
    assert captured["body"] == {"text": "Halo [whispers] dunia", "model_id": "eleven_v4"}

    config["elevenlabs_language_code"] = "id"
    fast = tts_tool.execute({"id": "el"}, {"text": "hi", "model": "eleven_v3", "voice": "JBFqnCBsd6RMkjVDRZzb", "speed": 2.0})
    assert fast["status"] == "success", fast
    assert captured["path"].startswith("/v1/text-to-speech/JBFqnCBsd6RMkjVDRZzb?")
    assert captured["body"] == {"text": "hi", "model_id": "eleven_v3", "language_code": "id", "voice_settings": {"speed": 1.2}}
    tts_tool.execute({"id": "el"}, {"text": "hi", "speed": 0.5})
    assert captured["body"]["voice_settings"] == {"speed": 0.7}

    config["elevenlabs_language_code"] = "Indonesian!"
    assert tts_tool.execute({"id": "el"}, {"text": "hi"})["error_code"] == "provider_configuration"


def test_elevenlabs_enforces_voice_and_model_allowlists(tts_tool, monkeypatch):
    providers = tts_tool._providers
    captured = {}

    def fake_request(self, method, path, *, body=None, headers=None, max_bytes=0):
        captured.update(path=path)
        return b"ID3\x04\x00\x00" + b"\x00" * 64, "audio/mpeg"

    monkeypatch.setattr(providers.elevenlabs.BoundedHttpClient, "request", fake_request)
    config = _elevenlabs_config()
    monkeypatch.setattr("backend.skills_manager.skills_manager.get_skill_config", lambda _id: config)

    rejected = tts_tool.execute({"id": "ev"}, {"text": "hi", "voice": "CustomVoice123"})
    assert rejected["error_code"] == "invalid_request"
    assert "Approved voices:" in rejected["error"] and "o5s6XRBkPSTD4syv6mZg" in rejected["error"]
    assert tts_tool.execute({"id": "ev"}, {"text": "hi", "voice": "../x"})["error_code"] == "invalid_request"
    assert tts_tool.execute({"id": "ev"}, {"text": "hi", "model": "eleven_unknown"})["error_code"] == "invalid_request"

    config["elevenlabs_allowed_voices"] = "CustomVoice123, OtherVoice"
    ok = tts_tool.execute({"id": "ev"}, {"text": "hi", "voice": "CustomVoice123"})
    assert ok["status"] == "success", ok
    assert captured["path"].startswith("/v1/text-to-speech/CustomVoice123?")


def test_elevenlabs_wav_wraps_pcm(tts_tool, monkeypatch):
    providers = tts_tool._providers
    captured = {}

    def fake_request(self, method, path, *, body=None, headers=None, max_bytes=0):
        captured.update(path=path)
        return b"\x01\x00" * 2400 + b"\x01", "audio/pcm"

    monkeypatch.setattr(providers.elevenlabs.BoundedHttpClient, "request", fake_request)
    monkeypatch.setattr("backend.skills_manager.skills_manager.get_skill_config", lambda _id: _elevenlabs_config())

    result = tts_tool.execute({"id": "ew"}, {"text": "hi", "output_format": "wav"})
    assert result["status"] == "success", result
    assert captured["path"].endswith("?output_format=pcm_24000")
    assert result["artifact"]["mime_type"] == "audio/wav"
    assert result["artifact"]["duration_seconds"] == 0.1


def test_elevenlabs_detail_error_message_is_surfaced(tts_tool, monkeypatch):
    import io
    from urllib.error import HTTPError

    network = tts_tool._providers.network

    def run(body):
        def fake_open(_self, request, timeout=0):
            raise HTTPError(request.full_url, 402, "x", {}, io.BytesIO(body))
        monkeypatch.setattr(network, "build_opener", lambda *_a: type("O", (), {"open": fake_open})())
        return tts_tool.execute({"id": "ee"}, {"text": "hi"})

    monkeypatch.setattr(network.BoundedHttpClient, "_url_for_path", lambda self, path: "https://api.elevenlabs.io" + path)
    monkeypatch.setattr("backend.skills_manager.skills_manager.get_skill_config", lambda _id: _elevenlabs_config())

    nested = run(b'{"detail":{"code":"paid_plan_required","message":"Free users cannot use library voices via the API."}}')
    assert "Provider said: Free users cannot use library voices via the API." in nested["error"]
    flat = run(b'{"detail":"Model eleven_x not found"}')
    assert "Provider said: Model eleven_x not found" in flat["error"]


def test_provider_4xx_message_is_surfaced_but_5xx_is_generic(tts_tool, monkeypatch):
    import io
    from urllib.error import HTTPError

    providers = tts_tool._providers
    network = providers.network

    class FakeOpener:
        def __init__(self, code, body):
            self.code, self.body = code, body

        def open(self, request, timeout=0):
            raise HTTPError(request.full_url, self.code, "x", {}, io.BytesIO(self.body))

    monkeypatch.setattr(network.BoundedHttpClient, "_url_for_path", lambda self, path: "https://openrouter.ai/api/v1" + path)
    monkeypatch.setattr("backend.skills_manager.skills_manager.get_skill_config", lambda _id: {
        "allow_agent_overrides": True, "default_provider": "openrouter", "openrouter_enabled": True, "openrouter_api_key": "k"})

    def run(code, body):
        monkeypatch.setattr(network, "build_opener", lambda *_a: type("O", (), {"open": FakeOpener(code, body).open})())
        return tts_tool.execute({"id": "e%d" % code}, {"text": "hi"})

    bad = run(400, b'{"error":{"message":"Voice alloy does not exist","code":400}}')
    assert "Provider said: Voice alloy does not exist" in bad["error"]
    down = run(502, b'{"error":{"message":"secret upstream detail"}}')
    assert "secret" not in down["error"] and "HTTP 502" in down["error"]


def test_openrouter_falls_back_to_pcm_wav_for_pcm_only_models(tts_tool, monkeypatch):
    providers = tts_tool._providers
    formats = []

    def fake_request(self, method, path, *, body=None, headers=None, max_bytes=0):
        fmt = json.loads(body)["response_format"]
        formats.append(fmt)
        if fmt == "mp3":
            raise providers.SpeechGenerationError(
                providers.SafeErrorCode.GENERATION_FAILED,
                'The provider rejected the speech request. Provider said: Gemini TTS only supports response_format="pcm". Got "mp3".')
        return b"\x01\x00" * 2400, "audio/pcm"

    monkeypatch.setattr(providers.openrouter.BoundedHttpClient, "request", fake_request)
    monkeypatch.setattr("backend.skills_manager.skills_manager.get_skill_config", lambda _id: {
        "allow_agent_overrides": True, "default_provider": "openrouter", "openrouter_enabled": True, "openrouter_api_key": "k"})

    result = tts_tool.execute({"id": "pcm"}, {"text": "hi"})
    assert result["status"] == "success", result
    assert formats == ["mp3", "pcm"]
    assert result["artifact"]["mime_type"] == "audio/wav"
    assert result["artifact"]["duration_seconds"] == 0.1
    assert "only offers PCM" in result["warnings"][0]

    formats.clear()
    direct = tts_tool.execute({"id": "pcm"}, {"text": "hi", "output_format": "wav"})
    assert direct["status"] == "success" and formats == ["pcm"]


def test_voice_and_model_fields_offer_options_with_custom_where_not_allowlisted():
    manifest = json.loads((SKILL_DIR / "skill.json").read_text())
    variables = {v["name"]: v for v in manifest["variables"]}
    assert len(variables["google_gemini_voice"]["options"]) == 30
    assert variables["google_gemini_voice"]["type"] == "select"
    # Code-side allowlists reject unknown values, so no custom entry there.
    for name in ("google_gemini_voice", "google_gemini_model", "openai_voice", "openai_model"):
        assert variables[name]["type"] == "select" and not variables[name].get("allow_custom")
    # OpenRouter models/voices are open-ended: suggestions plus custom.
    for name in ("openrouter_voice", "openrouter_model"):
        assert variables[name]["allow_custom"] is True and variables[name]["options"]
    template = (ROOT_DIR / "templates" / "skill_detail.html").read_text()
    assert "data-combo-select" in template and "__custom__" in template


def test_openrouter_personas_match_settings_dropdown(tts_tool):
    manifest = json.loads((SKILL_DIR / "skill.json").read_text())
    dropdown = [o["value"] for o in next(v for v in manifest["variables"] if v["name"] == "openrouter_voice")["options"]]
    assert dropdown == list(tts_tool._providers.openrouter._PERSONAS)


def test_agent_overrides_are_ignored_unless_allowed(tts_tool, monkeypatch):
    providers = tts_tool._providers
    captured = {}

    def fake_request(self, method, path, *, body=None, headers=None, max_bytes=0):
        captured.update(json.loads(body))
        return b"ID3\x04\x00\x00" + b"\x00" * 64, "audio/mpeg"

    monkeypatch.setattr(providers.openrouter.BoundedHttpClient, "request", fake_request)
    config = {"default_provider": "openrouter", "openrouter_enabled": True, "openrouter_api_key": "k",
              "openrouter_model": "hexgrad/kokoro-82m", "openrouter_voice": "am_michael", "mock_enabled": True,
              "allow_local_providers": True}
    monkeypatch.setattr("backend.skills_manager.skills_manager.get_skill_config", lambda _id: config)

    locked = tts_tool.execute({"id": "lock"}, {"text": "hi", "voice": "Zephyr", "model": "x/y", "provider": "mock"})
    assert locked["status"] == "success", locked
    assert (locked["provider"], locked["voice"], captured["model"]) == ("openrouter", "am_michael", "hexgrad/kokoro-82m")
    assert "voice" in locked["warnings"][0] and "ignored" in locked["warnings"][0]

    config["allow_agent_overrides"] = True
    free = tts_tool.execute({"id": "lock"}, {"text": "hi", "voice": "Zephyr"})
    assert free["voice"] == "Zephyr" and "warnings" not in free
