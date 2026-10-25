"""
Unit tests for backend/tools/lib/artifact_delivery.py — the conservative
execution-context classifier, transport selector, config accessors and the
streaming/hashing/token helpers.
"""

import hashlib
import os

import pytest

from backend.tools.lib import artifact_delivery as ad


# ---------------------------------------------------------------------------
# Execution-context classification matrix
# ---------------------------------------------------------------------------

def _lookup(workplace_type):
    return lambda _wid: ({"type": workplace_type} if workplace_type else None)


class TestExecutionContextClassifier:
    def test_no_workplace_no_sandbox_is_host_local(self):
        result = ad.resolve_execution_context({}, is_simulation=False)
        assert result["context"] == ad.CTX_HOST_LOCAL

    def test_no_workplace_sandbox_is_sandbox(self):
        result = ad.resolve_execution_context({"sandbox_enabled": 1}, is_simulation=False)
        assert result["context"] == ad.CTX_SANDBOX

    def test_local_workplace_no_sandbox_is_host_local(self):
        result = ad.resolve_execution_context(
            {"workplace_id": "w1"}, workplace_lookup=_lookup("local")
        )
        assert result["context"] == ad.CTX_HOST_LOCAL

    def test_local_workplace_with_sandbox_is_sandbox(self):
        result = ad.resolve_execution_context(
            {"workplace_id": "w1", "sandbox_enabled": 1},
            workplace_lookup=_lookup("local"),
        )
        assert result["context"] == ad.CTX_SANDBOX

    def test_bwrap_workplace(self):
        result = ad.resolve_execution_context(
            {"workplace_id": "w1"}, workplace_lookup=_lookup("bwrap")
        )
        assert result["context"] == ad.CTX_BWRAP

    def test_remote_workplace(self):
        result = ad.resolve_execution_context(
            {"workplace_id": "w1"}, workplace_lookup=_lookup("remote")
        )
        assert result["context"] == ad.CTX_REMOTE

    def test_tunnel_workplace(self):
        result = ad.resolve_execution_context(
            {"workplace_id": "w1"}, workplace_lookup=_lookup("tunnel")
        )
        assert result["context"] == ad.CTX_TUNNEL

    def test_remote_never_host_local_even_with_sandbox_disabled(self):
        # Policy forces sandbox_enabled=0 for remote workpaces; that must NOT
        # make a remote agent look host-local.
        result = ad.resolve_execution_context(
            {"workplace_id": "w1", "sandbox_enabled": 0},
            workplace_lookup=_lookup("remote"),
        )
        assert result["context"] == ad.CTX_REMOTE

    def test_tunnel_never_host_local_even_with_sandbox_disabled(self):
        result = ad.resolve_execution_context(
            {"workplace_id": "w1", "sandbox_enabled": 0},
            workplace_lookup=_lookup("tunnel"),
        )
        assert result["context"] == ad.CTX_TUNNEL

    def test_missing_workplace_is_unknown(self):
        result = ad.resolve_execution_context(
            {"workplace_id": "gone"}, workplace_lookup=lambda _w: None
        )
        assert result["context"] == ad.CTX_UNKNOWN

    def test_unrecognized_workplace_type_is_unknown(self):
        result = ad.resolve_execution_context(
            {"workplace_id": "w1"}, workplace_lookup=_lookup("weird")
        )
        assert result["context"] == ad.CTX_UNKNOWN

    def test_simulation_is_isolated_unknown(self):
        result = ad.resolve_execution_context({}, is_simulation=True)
        assert result["context"] == ad.CTX_UNKNOWN

    @pytest.mark.parametrize("raw,sub", [
        (0, "no"), ("0", "no"), ("false", "no"), (False, "no"), (None, "no"),
        (1, "yes"), ("1", "yes"), ("true", "yes"), ("yes", "yes"), ("on", "yes"),
    ])
    def test_sandbox_flag_parsing(self, raw, sub):
        result = ad.resolve_execution_context({"sandbox_enabled": raw}, is_simulation=False)
        if sub == "yes":
            assert result["context"] == ad.CTX_SANDBOX
        else:
            assert result["context"] == ad.CTX_HOST_LOCAL


# ---------------------------------------------------------------------------
# Transport selection
# ---------------------------------------------------------------------------

def _ctx(name):
    return {"context": name}


class TestSelectTransport:
    def test_host_local_pair_uses_local_path(self):
        result = ad.select_transport(_ctx(ad.CTX_HOST_LOCAL), _ctx(ad.CTX_HOST_LOCAL))
        assert result["transport"] == ad.TRANSPORT_LOCAL_PATH

    @pytest.mark.parametrize("recipient", [
        ad.CTX_SANDBOX, ad.CTX_BWRAP, ad.CTX_REMOTE, ad.CTX_TUNNEL, ad.CTX_UNKNOWN,
    ])
    def test_host_local_to_anything_else_uses_token_url(self, recipient):
        result = ad.select_transport(_ctx(ad.CTX_HOST_LOCAL), _ctx(recipient))
        assert result["transport"] == ad.TRANSPORT_TOKEN_URL

    @pytest.mark.parametrize("sender", [
        ad.CTX_SANDBOX, ad.CTX_BWRAP, ad.CTX_REMOTE, ad.CTX_TUNNEL, ad.CTX_UNKNOWN,
    ])
    def test_anything_else_to_host_local_uses_token_url(self, sender):
        result = ad.select_transport(_ctx(sender), _ctx(ad.CTX_HOST_LOCAL))
        assert result["transport"] == ad.TRANSPORT_TOKEN_URL

    def test_missing_contexts_fail_closed(self):
        assert ad.select_transport(None, None)["transport"] == ad.TRANSPORT_TOKEN_URL
        assert ad.select_transport({}, {})["transport"] == ad.TRANSPORT_TOKEN_URL

    def test_unknown_pair_fails_closed(self):
        result = ad.select_transport(_ctx(ad.CTX_UNKNOWN), _ctx(ad.CTX_UNKNOWN))
        assert result["transport"] == ad.TRANSPORT_TOKEN_URL


# ---------------------------------------------------------------------------
# safe_basename
# ---------------------------------------------------------------------------

class TestSafeBasename:
    @pytest.mark.parametrize("name", [
        "report.pdf", "chart.png", "my file (1).zip", "a_b-c.d.tar.gz", "X",
    ])
    def test_accepts_plain_names(self, name):
        assert ad.safe_basename(name) == name

    @pytest.mark.parametrize("name", [
        "", "   ", None, 123,
        "../etc/passwd", "a/b", "a\\b", "..", ".hidden", "sub/../../x",
        "bad\nname", "bad\x00name", "x" * 300,
    ])
    def test_rejects_unsafe_names(self, name):
        assert ad.safe_basename(name) is None


# ---------------------------------------------------------------------------
# Streaming hash / snapshot helpers
# ---------------------------------------------------------------------------

class TestStreamingHelpers:
    def test_sha256_and_size_matches_hashlib(self, tmp_path):
        data = os.urandom(4096)
        path = tmp_path / "blob.bin"
        path.write_bytes(data)
        digest, size = ad.sha256_and_size(str(path), chunk_size=512)
        assert digest == hashlib.sha256(data).hexdigest()
        assert size == len(data)

    def test_snapshot_is_immutable(self, tmp_path):
        src = tmp_path / "src.bin"
        dst = tmp_path / "snap.bin"
        src.write_bytes(b"original-content")
        digest, size = ad.snapshot_and_digest(str(src), str(dst))
        assert dst.read_bytes() == b"original-content"
        assert digest == hashlib.sha256(b"original-content").hexdigest()
        assert size == len(b"original-content")

        # Mutating the source must not change the snapshot.
        src.write_bytes(b"changed!!")
        assert dst.read_bytes() == b"original-content"


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

class TestTokenHelpers:
    def test_minted_token_is_valid_and_high_entropy(self):
        token = ad.mint_token()
        assert ad.is_valid_token_format(token)
        assert len(token) >= 40  # 32 bytes -> 43 urlsafe chars
        assert ad.mint_token() != token

    def test_hash_token_is_stable(self):
        token = ad.mint_token()
        assert ad.hash_token(token) == ad.hash_token(token)
        assert len(ad.hash_token(token)) == 64

    @pytest.mark.parametrize("bad", ["", "short", "has space", "sl/ash", "a" * 200, None, 42])
    def test_rejects_malformed_tokens(self, bad):
        assert ad.is_valid_token_format(bad) is False

    def test_constant_time_equal(self):
        assert ad.constant_time_equal("abc", "abc") is True
        assert ad.constant_time_equal("abc", "abd") is False


# ---------------------------------------------------------------------------
# Configuration precedence: setting -> env -> default
# ---------------------------------------------------------------------------

@pytest.fixture
def clean_settings():
    """Restore the artifact_share_* settings after each test."""
    from models.db import db

    keys = [
        "artifact_share_public_base_url",
        "artifact_share_token_ttl",
        "artifact_share_max_mb",
    ]
    db.invalidate_settings_cache()
    try:
        yield db
    finally:
        for key in keys:
            db.set_setting(key, "")
        db.invalidate_settings_cache()


class TestConfigPrecedence:
    def test_public_base_url_default(self, monkeypatch, clean_settings):
        monkeypatch.delenv("EVONIC_PUBLIC_URL", raising=False)
        value, source = ad.get_public_base_url()
        assert source == "default"
        assert value == ad.DEFAULT_PUBLIC_BASE_URL

    def test_public_base_url_env(self, monkeypatch, clean_settings):
        monkeypatch.setenv("EVONIC_PUBLIC_URL", "https://env.example")
        value, source = ad.get_public_base_url()
        assert (value, source) == ("https://env.example", "env")

    def test_public_base_url_setting_beats_env(self, monkeypatch, clean_settings):
        monkeypatch.setenv("EVONIC_PUBLIC_URL", "https://env.example")
        clean_settings.set_setting("artifact_share_public_base_url", "https://setting.example/")
        value, source = ad.get_public_base_url()
        assert (value, source) == ("https://setting.example", "setting")

    def test_token_ttl_default(self, monkeypatch, clean_settings):
        monkeypatch.delenv("ARTIFACT_SHARE_TOKEN_TTL", raising=False)
        assert ad.get_token_ttl_seconds() == 300

    def test_token_ttl_env(self, monkeypatch, clean_settings):
        monkeypatch.setenv("ARTIFACT_SHARE_TOKEN_TTL", "120")
        assert ad.get_token_ttl_seconds() == 120

    def test_token_ttl_setting(self, monkeypatch, clean_settings):
        monkeypatch.setenv("ARTIFACT_SHARE_TOKEN_TTL", "120")
        clean_settings.set_setting("artifact_share_token_ttl", "600")
        assert ad.get_token_ttl_seconds() == 600

    def test_max_share_default(self, monkeypatch, clean_settings):
        monkeypatch.delenv("ARTIFACT_SHARE_MAX_MB", raising=False)
        assert ad.get_max_share_mb() == 100
        assert ad.get_max_share_bytes() == 100 * 1024 * 1024

    def test_enabled_default_true(self, monkeypatch, clean_settings):
        from models.db import db
        monkeypatch.delenv("ARTIFACT_SHARE_ENABLED", raising=False)
        db.set_setting("artifact_share_enabled", "")
        db.invalidate_settings_cache()
        assert ad.is_share_enabled() is True
