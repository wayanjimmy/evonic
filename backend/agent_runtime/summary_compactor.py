"""
summary_compactor.py — keep persisted session summaries within a hard character
cap **without** naive mid-text truncation.

Why
---
The summarizer folds the previous summary plus new turns into a fresh summary.
With a "no information loss" prompt, each cycle preserves everything, so the
stored summary grows without bound. The fix is *semantic compaction*: rewrite
the summary more tersely, dropping details that are no longer relevant to the
current session state, while always preserving high-value information
(user identity/contact, entities & relationships, unresolved issues, pending
tasks).

Strategy (applied in order)
---------------------------
1. If the summary already fits, it is returned untouched.
2. Up to ``AGENT_SUMMARY_COMPACT_ATTEMPTS`` LLM compaction passes that rewrite
   the summary to fit the budget and drop stale content.
3. A deterministic, line-level fallback (:func:`_prune_to_limit`) that removes
   whole bullets/blocks — lowest-priority sections first — never slicing text.
   A word-boundary slice exists only as a documented absolute last resort for a
   single unbreakable block that alone exceeds the cap.

The public entry point is :func:`compact_summary`; it always returns text whose
length is ``<= max_chars`` (except the impossible single-block case described
above, where it is sliced at a word boundary).
"""

import contextlib
import logging
import re
from typing import Optional

from config import (
    AGENT_MAX_SUMMARY_CHARS,
    AGENT_SUMMARY_COMPACT_ATTEMPTS,
)

_logger = logging.getLogger(__name__)

# Public alias so callers can import the effective cap from one place.
MAX_SUMMARY_CHARS = AGENT_MAX_SUMMARY_CHARS

# Shown when the deterministic fallback had to drop content.
_PRUNE_NOTE = (
    "- [older / least-relevant details were omitted so this summary stays "
    "within its size limit]"
)

# ── Section priority ────────────────────────────────────────────────────────
# Higher number = kept longer. Matched case-insensitively against a line's
# label text. First matching keyword wins.
_SECTION_KEYWORDS = [
    ("entities", 6), ("relationship", 6), ("relasi", 6), ("entitas", 6),
    ("user information", 5), ("user info", 5), ("identity", 5),
    ("contact", 5), ("profil", 5), ("user", 5),
    ("issue", 5), ("problem", 5), ("complaint", 5), ("unresolved", 5),
    ("blocker", 5), ("keluhan", 5), ("masalah", 5),
    ("follow", 4), ("pending", 4), ("task", 4), ("todo", 4), ("next", 4),
    ("tindak lanjut", 4), ("rencana", 4), ("plan", 4),
    ("request", 3), ("permintaan", 3), ("scope", 3),
    ("fact", 2), ("note", 2), ("catatan", 2), ("detail", 2),
    ("history", 1), ("resolved", 1), ("done", 1), ("selesai", 1),
]
_DEFAULT_SECTION_PRIO = 3  # between requests (3) and facts (2) → keep over facts

_HEADER_RE = re.compile(
    r"^\s*(?:#{1,6}\s+\S|\*\*[^*]+\*\*\s*$|-?\s*[A-Za-z][A-Za-z0-9 /&,'()_.-]{2,70}:\s*$)"
)
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
_CODE_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*\n(.*?)\n?\s*```\s*$", re.DOTALL)


def _section_prio_for(line: str) -> Optional[int]:
    """Return a priority if *line* looks like a section label, else ``None``."""
    text = line.strip().lstrip("#*-• \t").rstrip(": ").strip().lower()
    if not text or len(text) > 80:
        return None
    for key, prio in _SECTION_KEYWORDS:
        if key in text:
            return prio
    return None


def _strip_code_fences(text: str) -> str:
    m = _CODE_FENCE_RE.match(text or "")
    return m.group(1).strip() if m else (text or "").strip()


def _finalize(text: str, max_chars: int) -> str:
    """Guarantee ``len(text) <= max_chars``.

    This is the *only* place that slices text, and it only fires when a single
    unbreakable block alone exceeds the cap (the deterministic pruner cannot
    drop its way under the limit). The slice is taken at a word boundary where
    possible to avoid cutting mid-word.
    """
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    space = cut.rfind(" ")
    if space >= max_chars * 0.8:
        cut = cut[:space]
    return cut.rstrip()


def _prune_to_limit(summary: str, max_chars: int) -> str:
    """Deterministically drop whole bullets/blocks until the text fits.

    Blocks are grouped as a bullet/header line plus its continuation lines.
    Low-priority sections (Facts/Notes first, then Requests, Follow-ups,
    Issues, User info) are dropped before Entities & Relationships. Header
    lines are kept unless nothing else remains. Text is never sliced mid-block
    except by :func:`_finalize` as an absolute last resort.
    """
    if len(summary) <= max_chars:
        return summary

    blocks = []  # {"lines": [str, ...], "prio": int, "header": bool}
    current_prio = _DEFAULT_SECTION_PRIO
    for line in summary.split("\n"):
        header_prio = _section_prio_for(line)
        is_header = bool(_HEADER_RE.match(line))
        if header_prio is not None:
            current_prio = header_prio
        is_bullet = bool(_BULLET_RE.match(line))
        if not blocks or is_bullet or is_header:
            blocks.append({
                "lines": [line],
                "prio": 7 if is_header else current_prio,
                "header": is_header,
            })
        else:
            # Continuation / blank line — belongs to the previous block.
            blocks[-1]["lines"].append(line)

    def render(idxs) -> str:
        return "\n".join(line for i in idxs for line in blocks[i]["lines"])

    active = list(range(len(blocks)))
    dropped_any = False
    while len(render(active)) > max_chars and active:
        candidates = [i for i in active if not blocks[i]["header"]]
        if not candidates:
            candidates = list(active)
        # Lowest priority first; ties → the later block (usually older/less
        # relevant). ``-i`` makes the largest index the minimum.
        victim = min(candidates, key=lambda i: (blocks[i]["prio"], -i))
        active.remove(victim)
        dropped_any = True

    text = render(active)
    if dropped_any and len(text) + 1 + len(_PRUNE_NOTE) <= max_chars:
        text = f"{text}\n{_PRUNE_NOTE}" if text else _PRUNE_NOTE
    return _finalize(text, max_chars)


# ── LLM semantic compaction ─────────────────────────────────────────────────

def _default_llm_client():
    """Lazily resolve the global LLM client (avoids import cycles)."""
    try:
        from backend.llm_client import llm_client
        return llm_client
    except Exception as e:  # pragma: no cover - defensive
        _logger.warning("summary_compactor: no default LLM client (%s)", e)
        return None


def _build_compact_prompt(summary: str, max_chars: int, focus_text: str,
                          attempt: int, current_len: int) -> str:
    focus_section = ""
    if focus_text:
        focus_section = (
            "## Current session focus (what matters right now)\n"
            f"{focus_text[:1500]}\n\n"
        )
    retry_note = ""
    if attempt > 0:
        retry_note = (
            f"\nNOTE: a previous rewrite was still {current_len} characters — "
            f"too long. Cut harder: drop more stale detail, merge bullets, and "
            f"shorten wording. Absolute maximum is {max_chars} characters.\n"
        )
    return (
        "You are a session-summary compaction engine. Rewrite the summary below "
        f"so that it is at most {max_chars} characters. Aim comfortably under "
        "the limit.\n\n"
        "Hard rules:\n"
        f"- The rewritten summary MUST be at most {max_chars} characters.\n"
        "- Output ONLY the rewritten summary — no preamble, no explanation, no "
        "code fences.\n"
        "- Keep the existing section headings (e.g. \"## Entities & "
        "Relationships\").\n"
        "- Preserve information in this priority order:\n"
        "  1. User identity & contact details (name, phone, contact person).\n"
        "  2. The full \"## Entities & Relationships\" section — named entities, "
        "their relationships, and any image markdown.\n"
        "  3. Unresolved issues / open problems and their current status.\n"
        "  4. Pending tasks / follow-ups and their latest status.\n"
        "  5. Recent requests, decisions, and outcomes.\n"
        "- Compress wording: merge duplicate bullets, drop redundant "
        "qualifiers, use terse fragments.\n"
        "- DELETE information that is no longer relevant to the current session "
        "(resolved issues, superseded plans, stale small talk, obsolete "
        "intermediate steps).\n"
        "- Do not invent facts. Never drop user identity/contact info or "
        "entities.\n"
        f"{retry_note}\n"
        f"{focus_section}"
        "## Summary to compact:\n"
        f"{summary}\n\n"
        "## Compacted summary:"
    )


def _llm_compact(client, llm_lock, summary: str, max_chars: int,
                 focus_text: str, attempt: int, current_len: int) -> str:
    """Run one LLM compaction pass; return '' on any failure."""
    prompt = _build_compact_prompt(summary, max_chars, focus_text, attempt,
                                   current_len)
    max_tokens = max(256, min(4096, max_chars))
    try:
        ctx = llm_lock if llm_lock is not None else contextlib.nullcontext()
        with ctx:
            result = client.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                tools=None,
                temperature=0.0,
                enable_thinking=False,
                max_tokens=max_tokens,
            )
    except Exception as e:
        _logger.warning("summary_compactor: LLM compaction raised: %s", e)
        return ""

    if not isinstance(result, dict) or not result.get("success"):
        _logger.warning(
            "summary_compactor: LLM compaction failed: %s",
            str((result or {}).get("error_type") or (result or {}).get("response", ""))[:200],
        )
        return ""

    choice = result["response"].get("choices", [{}])[0]
    text = (choice.get("message", {}).get("content") or "").strip()
    if not text:
        return ""
    try:
        from backend.llm_client import strip_thinking_tags
        text, _ = strip_thinking_tags(text)
    except Exception:
        pass
    return _strip_code_fences(text)


def compact_summary(summary: str, *, max_chars: Optional[int] = None,
                    llm=None, llm_lock=None, focus_text: Optional[str] = None,
                    use_llm: bool = True) -> str:
    """Return *summary* constrained to ``max_chars`` characters.

    Uses LLM semantic compaction (up to ``AGENT_SUMMARY_COMPACT_ATTEMPTS``
    passes) and then a deterministic line-level fallback. Never returns text
    longer than ``max_chars`` except the documented single-block last resort.

    Args:
        summary: The summary text to constrain.
        max_chars: Character budget; defaults to ``AGENT_MAX_SUMMARY_CHARS``.
        llm: Optional LLM client. When ``None`` the global client is used
            (unless ``use_llm`` is False).
        llm_lock: Optional lock serialising the underlying LLM call.
        focus_text: Recent conversation text used to judge relevance.
        use_llm: When False, skip LLM passes and use deterministic pruning only.
    """
    if not summary:
        return summary
    limit = max_chars if max_chars else MAX_SUMMARY_CHARS
    if len(summary) <= limit:
        return summary

    if use_llm and AGENT_SUMMARY_COMPACT_ATTEMPTS > 0:
        client = llm if llm is not None else _default_llm_client()
        if client is not None:
            best = summary
            for attempt in range(AGENT_SUMMARY_COMPACT_ATTEMPTS):
                candidate = _llm_compact(client, llm_lock, summary, limit,
                                         focus_text or "", attempt, len(best))
                if not candidate:
                    continue
                if len(candidate) < len(best) or best is summary:
                    best = candidate
                if len(candidate) <= limit:
                    _logger.info(
                        "summary_compactor: LLM pass %d fit %d -> %d chars",
                        attempt + 1, len(summary), len(candidate))
                    return candidate
                _logger.info(
                    "summary_compactor: LLM pass %d still over (%d > %d)",
                    attempt + 1, len(candidate), limit)
            # Feed the best (shortest) LLM result to the deterministic pruner.
            if len(best) < len(summary):
                summary = best

    result = _prune_to_limit(summary, limit)
    _logger.info("summary_compactor: pruned %d -> %d chars (limit %d)",
                 len(summary), len(result), limit)
    return result
