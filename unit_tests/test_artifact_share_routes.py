"""
Tests for the artifact-share tokenized download route.

Covers the security-critical download behaviour: single-use consumption,
expiry / revocation / uniform-failure handling, safe headers, snapshot
immutability, hash-only persistence and staging cleanup.
"""

import hashlib
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

from backend.tools.lib import artifact_delivery as delivery
from unit_tests import artifact_share_testkit as kit

_UNIFORM = {"error": "not_found"}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ArtifactShareRoutesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.spool = os.path.join(root, "spool")
        os.makedirs(self.spool, exist_ok=True)
        self.dbpath = os.path.join(root, "shares.db")

        self.routes = kit.load_routes_module(self.dbpath)

        # Point the spool at the temp dir and keep sharing enabled, then silence
        # the background cleanup thread so it never outlives the test.
        patcher = mock.patch.multiple(
            delivery,
            get_spool_dir=mock.MagicMock(return_value=self.spool),
            is_share_enabled=mock.MagicMock(return_value=True),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        with mock.patch.object(self.routes, "start_cleanup_thread"):
            bp = self.routes.create_blueprint()

        self.app = Flask(__name__)
        self.app.register_blueprint(bp)
        self.client = self.app.test_client()

        self.addCleanup(self.tmp.cleanup)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _stage(self, content: bytes, *, filename="report.pdf", ttl=300.0,
               revoked=False, token=None):
        token = token or delivery.mint_token()
        token_hash = delivery.hash_token(token)
        staging = os.path.join(self.spool, "stage-" + os.urandom(8).hex() + ".blob")
        with open(staging, "wb") as fh:
            fh.write(content)
        now = time.time()
        self.routes.share_db.create_share(
            token_hash=token_hash,
            owner_agent_id="alpha",
            sender_agent_id="alpha",
            recipient_agent_id="beta",
            display_filename=filename,
            size_bytes=len(content),
            sha256=_sha(content),
            staging_path=staging,
            created_at=now,
            expires_at=now + ttl,
        )
        if revoked:
            self.routes.share_db.revoke(token_hash, now)
        return token, token_hash, staging

    def _get(self, token):
        return self.client.get(f"/plugin/artifact-share/dl/{token}")

    # ------------------------------------------------------------------
    # Happy path
    # ------------------------------------------------------------------

    def test_valid_token_streams_bytes_with_safe_headers(self):
        content = b"PDF\x00\x01\x02-binary\xff" * 64
        token, _, staging = self._stage(content)

        resp = self._get(token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, content)
        self.assertEqual(resp.headers["Cache-Control"], "no-store")
        self.assertEqual(resp.headers["Pragma"], "no-cache")
        self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(resp.headers["Content-Length"], str(len(content)))
        self.assertIn("attachment", resp.headers["Content-Disposition"])
        self.assertIn("report.pdf", resp.headers["Content-Disposition"])

        resp.close()
        # Staging bytes are removed once the response is closed.
        self.assertFalse(os.path.exists(staging))

    def test_snapshot_bytes_are_served_not_live_source(self):
        # The spool holds an immutable snapshot; a share serves exactly those
        # bytes regardless of any later change to the original artifact.
        snapshot = b"snapshot-v1"
        token, _, staging = self._stage(snapshot)
        with open(staging, "ab") as fh:  # simulate a stray append
            fh.write(b"-extra")
        resp = self._get(token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, b"snapshot-v1-extra")
        self.assertEqual(resp.headers["Content-Length"], str(len(resp.data)))
        resp.close()

    # ------------------------------------------------------------------
    # Single-use + uniform failure
    # ------------------------------------------------------------------

    def test_single_use_token_cannot_be_replayed(self):
        token, _, staging = self._stage(b"one-shot")
        first = self._get(token)
        self.assertEqual(first.status_code, 200)
        first.close()

        second = self._get(token)
        self.assertEqual(second.status_code, 404)
        self.assertEqual(second.get_json(), _UNIFORM)

    def test_consume_is_atomic(self):
        token, token_hash, _ = self._stage(b"race")
        now = time.time()
        self.assertIsNotNone(self.routes.share_db.consume(token_hash, now))
        self.assertIsNone(self.routes.share_db.consume(token_hash, now))

    def test_unknown_token_is_uniform_404(self):
        resp = self._get(delivery.mint_token())
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json(), _UNIFORM)

    def test_malformed_token_is_uniform_404(self):
        for bad in ("short", "bad!!token", "%2e%2e%2f", "a" * 300):
            resp = self._get(bad)
            self.assertEqual(resp.status_code, 404, bad)
            self.assertEqual(resp.get_json(), _UNIFORM)

    def test_expired_token_is_uniform_404_and_cleaned(self):
        token, token_hash, staging = self._stage(b"stale", ttl=-5.0)
        resp = self._get(token)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json(), _UNIFORM)
        # Opportunistic cleanup removed both the row and the staging bytes.
        self.assertIsNone(self.routes.share_db.get_by_hash(token_hash))
        self.assertFalse(os.path.exists(staging))

    def test_revoked_token_is_uniform_404(self):
        token, _, _ = self._stage(b"revoked", revoked=True)
        resp = self._get(token)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json(), _UNIFORM)

    def test_missing_staging_file_is_uniform_404(self):
        token, _, staging = self._stage(b"gone")
        os.remove(staging)
        resp = self._get(token)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json(), _UNIFORM)

    def test_disabled_kill_switch_fails_closed(self):
        token, _, _ = self._stage(b"blocked")
        with mock.patch.object(delivery, "is_share_enabled", return_value=False):
            resp = self._get(token)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json(), _UNIFORM)

    def test_failure_bodies_are_indistinguishable(self):
        known = self._get("not-a-valid-token!")
        unknown = self._get(delivery.mint_token())
        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual(known.get_json(), unknown.get_json())

    # ------------------------------------------------------------------
    # Persistence + cleanup
    # ------------------------------------------------------------------

    def test_only_token_hash_is_persisted(self):
        token, token_hash, _ = self._stage(b"secret-bytes")
        row = self.routes.share_db.get_by_hash(token_hash)
        self.assertIsNotNone(row)
        self.assertEqual(row["token_hash"], token_hash)
        self.assertNotEqual(row["token_hash"], token)

        # The raw token must not appear anywhere in the database file.
        with open(self.dbpath, "rb") as fh:
            self.assertNotIn(token.encode(), fh.read())

    def test_display_filename_is_sanitized_on_download(self):
        token, _, _ = self._stage(b"x", filename="../evil path.pdf")
        resp = self._get(token)
        self.assertEqual(resp.status_code, 200)
        disposition = resp.headers["Content-Disposition"]
        self.assertIn("evil path.pdf", disposition)
        self.assertNotIn("..", disposition)
        resp.close()

    def test_cleanup_expired_removes_rows_and_bytes(self):
        _t1, _h1, s1 = self._stage(b"old-1", ttl=-1.0)
        _t2, _h2, s2 = self._stage(b"old-2", ttl=-1.0)
        _t3, h3, s3 = self._stage(b"live", ttl=300.0)

        removed = self.routes.cleanup_expired()
        self.assertEqual(removed, 2)
        self.assertFalse(os.path.exists(s1))
        self.assertFalse(os.path.exists(s2))
        self.assertTrue(os.path.exists(s3))
        self.assertIsNotNone(self.routes.share_db.get_by_hash(h3))

    def test_info_endpoint_reports_enabled(self):
        resp = self.client.get("/plugin/artifact-share/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["plugin"], "artifact-share")
        self.assertTrue(resp.get_json()["enabled"])


if __name__ == "__main__":
    unittest.main()
