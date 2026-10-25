"""
Unit tests for the ``share_artifact`` sender tool (plugins/artifact-share).

Covers: transport selection per execution-context pairs, sender/recipient
validation and messaging-ACL parity, filename/path safety, size limits, the
kill switch, token-hash-only persistence, spool snapshot immutability, and the
audit record contents.
"""

import os
import sys
import hashlib
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.tools.lib.simulation_scope as simulation_scope
from backend.tools.lib import artifact_delivery as delivery
from models.db import db

from unit_tests.artifact_share_testkit import load_share_db, load_tool_module


class ShareArtifactToolTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.artifacts_root = os.path.join(self.tmp, "shared", "agents")
        self.spool_dir = os.path.join(self.tmp, "spool")
        os.makedirs(self.spool_dir, exist_ok=True)

        # Redirect the artifact registry and spool into the temp tree.
        self._orig_shared = simulation_scope.shared_agents_dir
        simulation_scope.shared_agents_dir = lambda agent=None: self.artifacts_root
        self._patch_spool = mock.patch.object(
            delivery, "get_spool_dir", lambda: self.spool_dir
        )
        self._patch_spool.start()

        # Fresh plugin store on a temp DB.
        db.invalidate_settings_cache()
        self.plugin_db = load_share_db(os.path.join(self.tmp, "shares.db"))
        self.tool = load_tool_module(os.path.join(self.tmp, "shares.db"))

        self.sender = {
            "id": "sender",
            "name": "Sender",
            "is_super": False,
            "enabled": 1,
            "sandbox_enabled": 0,
            "workplace_id": None,
        }
        self.recipient_local = {
            "id": "recipient",
            "name": "Recipient",
            "is_super": False,
            "enabled": 1,
            "sandbox_enabled": 0,
            "workplace_id": None,
        }

        # Write a sender artifact.
        self.dir = os.path.join(self.artifacts_root, "sender", "artifacts")
        os.makedirs(self.dir, exist_ok=True)
        self.payload = b"binary-payload-\x00\x01\x02" * 100
        self.file = os.path.join(self.dir, "report.bin")
        with open(self.file, "wb") as fh:
            fh.write(self.payload)

    def tearDown(self):
        self._patch_spool.stop()
        simulation_scope.shared_agents_dir = self._orig_shared
        self._tmp.cleanup()

    # -- helpers ----------------------------------------------------------

    def _agents(self, *records):
        table = {r["id"]: dict(r) for r in records}

        def _get_agent(agent_id):
            return table.get(agent_id)

        return table, _get_agent

    def _run(self, args, recipients=(), workplaces=None, sender=None):
        workplaces = workplaces or {}
        table, get_agent = self._agents(*recipients)
        with mock.patch.object(db, "get_agent", side_effect=get_agent), \
             mock.patch.object(db, "get_workplace", side_effect=lambda wid: workplaces.get(wid)), \
             mock.patch("backend.audit_logger.audit.log_artifact_share") as audit_mock:
            result = self.tool.execute(dict(sender or self.sender), args)
        return result, audit_mock

    # -- local_path branch ------------------------------------------------

    def test_local_to_local_returns_exact_local_path_shape(self):
        result, audit_mock = self._run(
            {"filename": "report.bin", "recipient_agent_id": "recipient"},
            recipients=[self.recipient_local],
        )
        self.assertEqual(
            set(result.keys()),
            {"transport", "artifact_path", "filename", "size_bytes", "sha256"},
        )
        self.assertEqual(result["transport"], "local_path")
        self.assertEqual(result["artifact_path"], os.path.realpath(self.file))
        self.assertEqual(result["filename"], "report.bin")
        self.assertEqual(result["size_bytes"], len(self.payload))
        self.assertEqual(result["sha256"], hashlib.sha256(self.payload).hexdigest())
        audit_mock.assert_called_once()
        kwargs = audit_mock.call_args.kwargs
        self.assertEqual(kwargs["transport"], "local_path")
        self.assertEqual(kwargs["sender_agent_id"], "sender")
        self.assertEqual(kwargs["recipient_agent_id"], "recipient")

    def test_local_to_remote_returns_token_url_without_any_path(self):
        remote_recipient = {
            "id": "remote_agent",
            "name": "Remote",
            "is_super": False,
            "enabled": 1,
            "sandbox_enabled": 0,
            "workplace_id": "wp_remote",
        }
        result, _ = self._run(
            {"filename": "report.bin", "recipient_agent_id": "remote_agent"},
            recipients=[remote_recipient],
            workplaces={"wp_remote": {"id": "wp_remote", "type": "remote"}},
        )
        self.assertEqual(result["transport"], "token_url")
        self.assertNotIn("artifact_path", result)
        # No host path may leak anywhere in the token response.
        blob = repr(result)
        self.assertNotIn(self.dir, blob)
        self.assertNotIn("artifacts", blob)
        self.assertTrue(result["url"].startswith("http"))
        self.assertIn("/plugin/artifact-share/dl/", result["url"])
        self.assertEqual(result["ttl_seconds"], 300)
        self.assertTrue(result["single_use"])
        self.assertEqual(result["sha256"], hashlib.sha256(self.payload).hexdigest())
        self.assertIn("expires_at", result)

    def test_sandbox_and_bwrap_and_tunnel_all_fall_back_to_token_url(self):
        cases = [
            ("sandbox", {"sandbox_enabled": 1, "workplace_id": None}, {}),
            ("bwrap", {"sandbox_enabled": 0, "workplace_id": "wpb"},
             {"wpb": {"id": "wpb", "type": "bwrap"}}),
            ("tunnel", {"sandbox_enabled": 0, "workplace_id": "wpt"},
             {"wpt": {"id": "wpt", "type": "tunnel"}}),
        ]
        for label, overrides, workplaces in cases:
            recipient = {
                "id": "rcpt_" + label, "name": label, "is_super": False,
                "enabled": 1, "sandbox_enabled": 0, "workplace_id": None,
            }
            recipient.update(overrides)
            result, _ = self._run(
                {"filename": "report.bin", "recipient_agent_id": recipient["id"]},
                recipients=[recipient], workplaces=workplaces,
            )
            self.assertEqual(result["transport"], "token_url", label)
            self.assertNotIn("artifact_path", result, label)

    def test_unknown_recipient_context_fails_closed(self):
        # Recipient has a workplace id that no longer exists -> unknown context.
        stale = {
            "id": "stale", "name": "Stale", "is_super": False, "enabled": 1,
            "sandbox_enabled": 0, "workplace_id": "deleted_wp",
        }
        result, _ = self._run(
            {"filename": "report.bin", "recipient_agent_id": "stale"},
            recipients=[stale], workplaces={},
        )
        self.assertEqual(result["transport"], "token_url")

    # -- token persistence -------------------------------------------------

    def test_only_token_hash_is_persisted(self):
        result, _ = self._run(
            {"filename": "report.bin", "recipient_agent_id": "recipient"},
            recipients=[self.recipient_local],
        )
        # Force a token_url by using a remote recipient instead.
        remote = {
            "id": "r2", "name": "R2", "is_super": False, "enabled": 1,
            "sandbox_enabled": 0, "workplace_id": "wp2",
        }
        result, _ = self._run(
            {"filename": "report.bin", "recipient_agent_id": "r2"},
            recipients=[remote], workplaces={"wp2": {"id": "wp2", "type": "tunnel"}},
        )
        token = result["url"].rsplit("/", 1)[-1]
        expected_hash = hashlib.sha256(token.encode()).hexdigest()

        with self.plugin_db.db._connect() as conn:
            rows = conn.execute("SELECT * FROM artifact_share_tokens").fetchall()
        self.assertEqual(len(rows), 1)
        row = dict(rows[0])
        self.assertEqual(row["token_hash"], expected_hash)
        # The raw token must not appear in any stored column.
        for key, value in row.items():
            if isinstance(value, str):
                self.assertNotIn(token, value, f"raw token leaked in column {key}")
        self.assertEqual(row["recipient_agent_id"], "r2")
        self.assertEqual(row["owner_agent_id"], "sender")
        self.assertEqual(row["display_filename"], "report.bin")
        self.assertEqual(row["size_bytes"], len(self.payload))
        self.assertEqual(row["max_uses"], 1)
        self.assertEqual(row["used_count"], 0)

    def test_spool_snapshot_is_immutable(self):
        remote = {
            "id": "r3", "name": "R3", "is_super": False, "enabled": 1,
            "sandbox_enabled": 1, "workplace_id": None,
        }
        result, _ = self._run(
            {"filename": "report.bin", "recipient_agent_id": "r3"},
            recipients=[remote],
        )
        # Mutate the source artifact after the share was issued.
        with open(self.file, "wb") as fh:
            fh.write(b"CHANGED-AFTER-SHARE")

        with self.plugin_db.db._connect() as conn:
            row = dict(conn.execute(
                "SELECT * FROM artifact_share_tokens").fetchone())
        with open(row["staging_path"], "rb") as fh:
            self.assertEqual(fh.read(), self.payload)

    # -- validation --------------------------------------------------------

    def test_filename_traversal_rejected(self):
        result, _ = self._run(
            {"filename": "../secret.txt", "recipient_agent_id": "recipient"},
            recipients=[self.recipient_local],
        )
        self.assertIn("error", result)

    def test_missing_artifact_rejected(self):
        result, _ = self._run(
            {"filename": "nope.bin", "recipient_agent_id": "recipient"},
            recipients=[self.recipient_local],
        )
        self.assertIn("error", result)

    def test_recipient_not_found(self):
        result, _ = self._run(
            {"filename": "report.bin", "recipient_agent_id": "ghost"},
            recipients=[],
        )
        self.assertIn("error", result)

    def test_recipient_disabled(self):
        disabled = dict(self.recipient_local, enabled=0)
        result, _ = self._run(
            {"filename": "report.bin", "recipient_agent_id": "recipient"},
            recipients=[disabled],
        )
        self.assertIn("error", result)

    def test_self_share_blocked(self):
        result, _ = self._run(
            {"filename": "report.bin", "recipient_agent_id": "sender"},
            recipients=[self.sender],
        )
        self.assertIn("error", result)

    def test_invalid_recipient_id(self):
        result, _ = self._run(
            {"filename": "report.bin", "recipient_agent_id": "Bad ID!"},
            recipients=[self.recipient_local],
        )
        self.assertIn("error", result)

    # -- ACL parity --------------------------------------------------------

    def test_acl_whitelist_blocks(self):
        sender = dict(self.sender, messaging_acl='["someone_else"]',
                      messaging_acl_mode="whitelist")
        result, _ = self._run(
            {"filename": "report.bin", "recipient_agent_id": "recipient"},
            recipients=[self.recipient_local], sender=sender,
        )
        self.assertIn("error", result)
        self.assertIn("whitelist", result["error"])

    def test_acl_blacklist_blocks(self):
        sender = dict(self.sender, messaging_acl='["recipient"]',
                      messaging_acl_mode="blacklist")
        result, _ = self._run(
            {"filename": "report.bin", "recipient_agent_id": "recipient"},
            recipients=[self.recipient_local], sender=sender,
        )
        self.assertIn("error", result)
        self.assertIn("blacklist", result["error"])

    def test_acl_allows_when_permitted(self):
        sender = dict(self.sender, messaging_acl='["recipient"]',
                      messaging_acl_mode="whitelist")
        result, _ = self._run(
            {"filename": "report.bin", "recipient_agent_id": "recipient"},
            recipients=[self.recipient_local], sender=sender,
        )
        self.assertEqual(result.get("transport"), "local_path")

    def test_super_agent_bypasses_acl(self):
        sender = dict(self.sender, is_super=True,
                      messaging_acl='["nobody"]', messaging_acl_mode="whitelist")
        result, _ = self._run(
            {"filename": "report.bin", "recipient_agent_id": "recipient"},
            recipients=[self.recipient_local], sender=sender,
        )
        self.assertEqual(result.get("transport"), "local_path")

    # -- limits / kill switch ---------------------------------------------

    def test_size_limit_enforced(self):
        with mock.patch.object(delivery, "get_max_share_bytes", lambda: 10):
            result, _ = self._run(
                {"filename": "report.bin", "recipient_agent_id": "recipient"},
                recipients=[self.recipient_local],
            )
        self.assertIn("error", result)

    def test_kill_switch_disables_tool(self):
        with mock.patch.object(delivery, "is_share_enabled", lambda: False):
            result, _ = self._run(
                {"filename": "report.bin", "recipient_agent_id": "recipient"},
                recipients=[self.recipient_local],
            )
        self.assertIn("error", result)

    def test_allowed_extensions_allowlist(self):
        with mock.patch.object(delivery, "get_allowed_extensions",
                               lambda: frozenset({".txt"})):
            result, _ = self._run(
                {"filename": "report.bin", "recipient_agent_id": "recipient"},
                recipients=[self.recipient_local],
            )
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
