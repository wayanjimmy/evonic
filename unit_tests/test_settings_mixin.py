"""Settings cache regression tests."""


def test_get_setting_does_not_cache_missing_fallback_defaults():
    from models.db import db
    db.invalidate_settings_cache()

    assert db.get_setting('missing-key', 'a') == 'a'
    assert db.get_setting('missing-key', 'b') == 'b'
    assert db.get_setting('missing-key') is None


def test_get_setting_caches_only_actual_database_values():
    from models.db import db
    db.invalidate_settings_cache()

    db.set_setting('present-key', 'stored')
    assert db.get_setting('present-key', 'fallback') == 'stored'
    assert db.get_setting('present-key', 'other') == 'stored'

