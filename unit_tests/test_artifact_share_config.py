"""
Tests for artifact-share configuration resolution.

Verifies the documented precedence for every share setting:

    system setting  ->  plugin config  ->  environment variable  ->  default

and that defaults match the task requirements (TTL 300 s, 100 MB max, enabled
kill switch, controlled spool directory).
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.tools.lib import artifact_delivery as delivery


def _patch_settings(values):
    """Return a context manager that stubs DB setting / plugin-config lookups.

    ``values`` maps a key suffix (e.g. ``artifact_share_token_ttl`` or the plugin
    variable ``TOKEN_TTL_SECONDS``) to the string value it should resolve to.
    """
    def fake_setting(key):
        return values.get(key)

    def fake_plugin_config(plugin_id, name):
        if plugin_id != "artifact-share":
            return None
        return values.get(name)

    return mock.patch.multiple(
        delivery,
        _db_setting=mock.MagicMock(side_effect=fake_setting),
        _plugin_config=mock.MagicMock(side_effect=fake_plugin_config),
    )


class PublicBaseUrlTest(unittest.TestCase):
    def test_default_when_unset(self):
        with _patch_settings({}), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EVONIC_PUBLIC_URL", None)
            url, source = delivery.get_public_base_url()
        self.assertEqual(url, delivery.DEFAULT_PUBLIC_BASE_URL)
        self.assertEqual(source, "default")

    def test_env_used_and_trailing_slash_stripped(self):
        with _patch_settings({}), mock.patch.dict(
            os.environ, {"EVONIC_PUBLIC_URL": "https://agents.example.com/"}
        ):
            url, source = delivery.get_public_base_url()
        self.assertEqual(url, "https://agents.example.com")
        self.assertEqual(source, "env")

    def test_plugin_config_beats_env(self):
        with _patch_settings({"PUBLIC_BASE_URL": "https://cfg.example.com"}), \
                mock.patch.dict(os.environ, {"EVONIC_PUBLIC_URL": "https://env.example.com"}):
            url, source = delivery.get_public_base_url()
        self.assertEqual(url, "https://cfg.example.com")
        self.assertEqual(source, "plugin_config")

    def test_system_setting_beats_all(self):
        with _patch_settings({
            "artifact_share_public_base_url": "https://setting.example.com",
            "PUBLIC_BASE_URL": "https://cfg.example.com",
        }), mock.patch.dict(os.environ, {"EVONIC_PUBLIC_URL": "https://env.example.com"}):
            url, source = delivery.get_public_base_url()
        self.assertEqual(url, "https://setting.example.com")
        self.assertEqual(source, "setting")


class NumericSettingsTest(unittest.TestCase):
    def test_default_ttl_is_300(self):
        with _patch_settings({}), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ARTIFACT_SHARE_TOKEN_TTL", None)
            self.assertEqual(delivery.get_token_ttl_seconds(), 300)

    def test_ttl_clamped_to_bounds(self):
        with _patch_settings({"artifact_share_token_ttl": "5"}):
            self.assertEqual(delivery.get_token_ttl_seconds(), 30)
        with _patch_settings({"artifact_share_token_ttl": "99999"}):
            self.assertEqual(delivery.get_token_ttl_seconds(), 3600)

    def test_default_max_share_is_100mb(self):
        with _patch_settings({}), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ARTIFACT_SHARE_MAX_MB", None)
            self.assertEqual(delivery.get_max_share_mb(), 100)
            self.assertEqual(delivery.get_max_share_bytes(), 100 * 1024 * 1024)

    def test_max_share_env_override(self):
        with _patch_settings({}), mock.patch.dict(
            os.environ, {"ARTIFACT_SHARE_MAX_MB": "7"}
        ):
            self.assertEqual(delivery.get_max_share_mb(), 7)

    def test_spool_quota_default_and_bounds(self):
        with _patch_settings({}), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ARTIFACT_SHARE_SPOOL_QUOTA_MB", None)
            self.assertEqual(delivery.get_spool_quota_mb(), delivery.DEFAULT_SPOOL_QUOTA_MB)
        with _patch_settings({"artifact_share_spool_quota_mb": "1"}):
            self.assertEqual(delivery.get_spool_quota_mb(), 16)


class KillSwitchTest(unittest.TestCase):
    def test_enabled_by_default(self):
        with _patch_settings({}), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ARTIFACT_SHARE_ENABLED", None)
            self.assertTrue(delivery.is_share_enabled())

    def test_setting_disables(self):
        with _patch_settings({"artifact_share_enabled": "0"}):
            self.assertFalse(delivery.is_share_enabled())

    def test_plugin_config_disables(self):
        with _patch_settings({"ENABLED": "false"}):
            self.assertFalse(delivery.is_share_enabled())

    def test_env_disables(self):
        with _patch_settings({}), mock.patch.dict(
            os.environ, {"ARTIFACT_SHARE_ENABLED": "0"}
        ):
            self.assertFalse(delivery.is_share_enabled())


class SpoolDirTest(unittest.TestCase):
    def test_default_under_app_root(self):
        with _patch_settings({}), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ARTIFACT_SHARE_SPOOL_DIR", None)
            spool = delivery.get_spool_dir()
        self.assertTrue(spool.endswith(os.path.join("data", "spool", "artifact_share")))

    def test_override_wins(self):
        with _patch_settings({"artifact_share_spool_dir": "/tmp/custom-spool"}):
            self.assertEqual(delivery.get_spool_dir(), "/tmp/custom-spool")


class AllowedExtensionsTest(unittest.TestCase):
    def test_none_means_any_type(self):
        with _patch_settings({}), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ARTIFACT_SHARE_ALLOWED_EXTENSIONS", None)
            self.assertIsNone(delivery.get_allowed_extensions())
            self.assertIsNone(delivery.validate_artifact_type("report.exe"))

    def test_allowlist_parsing_and_enforcement(self):
        with _patch_settings({"artifact_share_allowed_extensions": "pdf, .PNG ,zip"}):
            allowed = delivery.get_allowed_extensions()
            self.assertEqual(allowed, frozenset({".pdf", ".png", ".zip"}))
            self.assertIsNone(delivery.validate_artifact_type("a.pdf"))
            self.assertIsNone(delivery.validate_artifact_type("a.PNG"))
            self.assertIsNotNone(delivery.validate_artifact_type("a.sh"))


if __name__ == "__main__":
    unittest.main()
