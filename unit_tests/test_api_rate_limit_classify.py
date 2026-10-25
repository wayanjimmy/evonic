"""Tests for api_rate_limit.classify_request tier classification.

Regression guard for #627 / FINDING-004: cheap chat reads/polls must NOT consume
the small `chat` tier — only actual LLM-send POSTs do.
"""
import unittest

from models.api_rate_limit import classify_request


class TestClassifyRequest(unittest.TestCase):
    AID = "/api/agents/agent_x"

    def test_chat_send_post_is_chat_tier(self):
        self.assertEqual(classify_request(f"{self.AID}/chat", "POST"), "chat")

    def test_chat_approve_post_is_chat_tier(self):
        self.assertEqual(classify_request(f"{self.AID}/chat/approve", "POST"), "chat")

    def test_chat_get_messages_is_poll_tier(self):
        # GET /chat is the polling/messages read, not a send
        self.assertEqual(classify_request(f"{self.AID}/chat", "GET"), "poll")

    def test_chat_read_endpoints_are_poll_tier(self):
        for sub in ("history", "poll", "state", "session", "summary", "events",
                    "stream", "llm-preview"):
            self.assertEqual(
                classify_request(f"{self.AID}/chat/{sub}", "GET"), "poll",
                f"/chat/{sub} should be poll tier",
            )

    def test_chat_clear_post_is_poll_tier(self):
        # clear is a cheap mutation, not an LLM send
        self.assertEqual(classify_request(f"{self.AID}/chat/clear", "POST"), "poll")

    def test_upload_endpoints_unchanged(self):
        self.assertEqual(classify_request(f"{self.AID}/artifacts", "POST"), "upload")
        self.assertEqual(classify_request(f"{self.AID}/avatar", "POST"), "upload")
        self.assertEqual(classify_request("/api/plugins/install", "POST"), "upload")

    def test_crud_and_general_unchanged(self):
        self.assertEqual(classify_request("/api/agents", "GET"), "crud")
        self.assertEqual(classify_request(f"{self.AID}", "GET"), "crud")
        self.assertEqual(classify_request("/api/settings", "GET"), "general")

    def test_evaluator_polling_reads_are_exempt(self):
        # High-frequency evaluator GET polls must not be rate-limited at all.
        self.assertIsNone(classify_request("/api/evaluator/log_poll", "GET"))
        self.assertIsNone(classify_request("/api/evaluator/test_matrix", "GET"))
        self.assertIsNone(classify_request("/api/evaluator/test_matrix?run_id=3", "GET"))
        self.assertIsNone(classify_request("/api/v1/history/last/id", "GET"))
        self.assertIsNone(classify_request("/api/v1/history/5/math/1", "GET"))
        self.assertIsNone(classify_request("/api/dashboard/data", "GET"))
        self.assertIsNone(classify_request("/api/models", "GET"))
        self.assertIsNone(classify_request("/api/config", "GET"))
        self.assertIsNone(classify_request("/api/system/update/status", "GET"))

    def test_mutations_on_exempt_prefixes_still_limited(self):
        # Only GET reads are exempt — mutations stay rate-limited.
        self.assertEqual(classify_request("/api/models", "POST"), "general")
        self.assertEqual(classify_request("/api/models/m1", "PUT"), "general")
        self.assertEqual(classify_request("/api/models/m1", "DELETE"), "general")
        self.assertEqual(classify_request("/api/config/model", "POST"), "general")
        self.assertEqual(
            classify_request("/api/system/update/start", "POST"), "general"
        )

    def test_static_and_non_api_unchanged(self):
        self.assertEqual(classify_request("/static/js/app.js", "GET"), "static")
        self.assertIsNone(classify_request("/login", "POST"))

    def test_get_avatar_is_static_not_crud(self):
        # GET avatar serves an image file — should NOT consume the CRUD budget
        self.assertEqual(classify_request(f"{self.AID}/avatar", "GET"), "static")
        self.assertEqual(classify_request(f"{self.AID}/avatar?size=small", "GET"), "static")

    def test_get_artifacts_file_is_static_not_crud(self):
        # GET artifacts/<file> serves a static file — should NOT consume CRUD budget
        self.assertEqual(
            classify_request(f"{self.AID}/artifacts/screenshot.png", "GET"), "static"
        )
        self.assertEqual(
            classify_request(f"{self.AID}/artifacts/report.pdf", "GET"), "static"
        )

    def test_post_avatar_still_upload(self):
        # POST avatar is still an upload (unchanged behavior)
        self.assertEqual(classify_request(f"{self.AID}/avatar", "POST"), "upload")

    def test_post_artifacts_still_upload(self):
        # POST artifacts is still an upload (unchanged behavior)
        self.assertEqual(classify_request(f"{self.AID}/artifacts", "POST"), "upload")

    def test_upload_kb_still_upload(self):
        # POST kb is still an upload (unchanged behavior)
        self.assertEqual(classify_request(f"{self.AID}/kb", "POST"), "upload")

    # -- Artifact-share downloads (task #806) ---------------------------

    def test_artifact_share_download_is_download_tier(self):
        # Tokenized artifact downloads get their own bounded tier so a burst of
        # shares cannot exhaust the general budget or go entirely unmetered.
        self.assertEqual(
            classify_request("/plugin/artifact-share/dl/sometoken", "GET"),
            "download",
        )
        self.assertEqual(
            classify_request("/plugin/artifact-share/", "GET"),
            "download",
        )
        self.assertEqual(
            classify_request("/plugin/artifact-share/status", "HEAD"),
            "download",
        )

    def test_download_tier_is_bounded(self):
        # The tier must exist and impose a finite per-minute bound.
        from models.api_rate_limit import TIERS
        self.assertIn("download", TIERS)
        limit, window = TIERS["download"]
        self.assertGreater(limit, 0)
        self.assertEqual(window, 60)


if __name__ == "__main__":
    unittest.main()
