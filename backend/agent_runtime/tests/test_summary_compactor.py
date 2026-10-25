"""
Unit tests for summary_compactor.py.

Covers the hard character cap on persisted session summaries:
- summaries already within budget are returned untouched,
- LLM semantic compaction is used when available,
- an LLM that overshoots (or fails, or raises) falls back to deterministic
  line-level pruning,
- the deterministic pruner never slices text mid-line and keeps high-priority
  sections (user info / entities) over low-priority ones (facts),
- ``compact_summary`` always returns text within the cap for multi-line input.
"""
import os
import sys
import unittest

# Add project root to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))

from backend.agent_runtime.summary_compactor import (
    compact_summary,
    _prune_to_limit,
)


class _FakeLLM:
    """Minimal stand-in for backend.llm_client.LLMClient."""

    def __init__(self, reply: str = "", exc: Exception = None):
        self.reply = reply
        self.exc = exc
        self.calls = 0
        self.last_prompt = None

    def chat_completion(self, messages=None, **kwargs):
        self.calls += 1
        self.last_prompt = (messages or [{}])[0].get("content")
        if self.exc is not None:
            raise self.exc
        return {
            "success": True,
            "response": {
                "choices": [{"message": {"content": self.reply}}],
            },
        }


def _long_summary(marker: str, n: int) -> str:
    return "".join(
        f"- {marker} item {i} " + ("x" * 60) + "\n" for i in range(n)
    )


SAMPLE_SUMMARY = (
    "- User Information:\n"
    "    - Full Name: Jane Doe\n"
    "    - Phone Number: +62-812-0000-0000\n"
    "- Requests:\n"
    "    - USERREQAAA book a room for the team offsite next month\n"
    "    - USERREQBBB send the pricing sheet to procurement\n"
    "- Facts:\n"
    "    - FACTA00 the venue has an outdoor area\n"
    "    - FACTB00 wifi password changes weekly\n"
    "## Entities & Relationships\n"
    "- Acme Corp \u2014 vendor; user works_at Acme Corp; photo: ![a.jpg](/api/attachments/1/view)\n"
    "- Jakarta \u2014 city in Indonesia; user lives_in Jakarta\n"
)


class TestCompactSummary(unittest.TestCase):
    def test_within_limit_returned_untouched(self):
        text = "short summary"
        self.assertIs(compact_summary(text, max_chars=2000, use_llm=False), text)

    def test_empty_summary_returned(self):
        self.assertEqual(compact_summary("", max_chars=200), "")

    def test_no_llm_prunes_and_respects_cap(self):
        limit = 260
        out = compact_summary(SAMPLE_SUMMARY, max_chars=limit, use_llm=False)
        self.assertLessEqual(len(out), limit)
        # High-priority info survives the prune.
        self.assertIn("Phone Number", out)
        self.assertIn("Acme Corp", out)

    def test_pruner_keeps_entities_over_facts(self):
        limit = 260
        out = _prune_to_limit(SAMPLE_SUMMARY, limit)
        self.assertLessEqual(len(out), limit)
        self.assertIn("Acme Corp", out)
        # Facts are the lowest priority and should be dropped first.
        self.assertNotIn("FACTA00", out)

    def test_pruner_never_slices_lines_mid_text(self):
        # Every retained line must be an intact original line (or the prune note).
        original_lines = set(SAMPLE_SUMMARY.split("\n"))
        out = _prune_to_limit(SAMPLE_SUMMARY, 220)
        for line in out.split("\n"):
            if not line:
                continue
            self.assertTrue(
                line in original_lines or line.startswith("- [older"),
                msg=f"unexpected partial line: {line!r}",
            )

    def test_llm_success_returns_llm_output(self):
        llm = _FakeLLM(reply="- User Information:\n    - Full Name: Jane Doe\n")
        out = compact_summary(SAMPLE_SUMMARY, max_chars=200, llm=llm)
        # Output is normalized (surrounding whitespace stripped) — see _finalize.
        self.assertEqual(out, "- User Information:\n    - Full Name: Jane Doe")
        self.assertEqual(llm.calls, 1)

    def test_llm_overshoot_falls_back_to_prune(self):
        # LLM always returns something bigger than the cap -> deterministic prune.
        llm = _FakeLLM(reply=_long_summary("STILLLONG", 20))
        out = compact_summary(SAMPLE_SUMMARY, max_chars=260, llm=llm)
        self.assertLessEqual(len(out), 260)
        self.assertIn("Acme Corp", out)
        self.assertGreaterEqual(llm.calls, 1)

    def test_llm_exception_falls_back_to_prune(self):
        llm = _FakeLLM(exc=RuntimeError("boom"))
        out = compact_summary(SAMPLE_SUMMARY, max_chars=260, llm=llm)
        self.assertLessEqual(len(out), 260)
        self.assertIn("Acme Corp", out)

    def test_llm_returns_empty_falls_back_to_prune(self):
        llm = _FakeLLM(reply="")
        out = compact_summary(SAMPLE_SUMMARY, max_chars=260, llm=llm)
        self.assertLessEqual(len(out), 260)
        self.assertIn("Acme Corp", out)

    def test_cap_guarantee_on_large_multiline_summary(self):
        big = ""
        for i in range(30):
            big += f"- Section {i} heading {i}:\n" + _long_summary(f"S{i}", 5)
        out = compact_summary(big, max_chars=500, use_llm=False)
        self.assertLessEqual(len(out), 500)

    def test_use_llm_false_skips_llm(self):
        llm = _FakeLLM(reply="should not be used")
        out = compact_summary(SAMPLE_SUMMARY, max_chars=260, llm=llm,
                              use_llm=False)
        self.assertEqual(llm.calls, 0)
        self.assertNotEqual(out, "should not be used")
        self.assertLessEqual(len(out), 260)


if __name__ == "__main__":
    unittest.main()
