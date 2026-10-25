"""Live "thinking" preview: coalesces streamed reasoning deltas into a small
number of realtime events for the browser.

The LLM client streams ``reasoning_content`` deltas through a callback.  Writing
one journal row per token would be wasteful, so this batches deltas and
publishes at most one ``thinking_delta`` event every ``min_interval`` seconds.
The deltas are only a preview: the authoritative ``thinking`` event (full text)
still fires once the response is complete and replaces the live row.
"""

import time
from typing import Callable, Optional


class ThinkingStreamEmitter:
    """Callable ``emitter(kind, text)`` suitable as ``stream_callback``."""

    def __init__(self, publish: Callable[[str, dict], None], min_interval: float = 0.12,
                 clock: Callable[[], float] = time.monotonic):
        self._publish = publish
        self._min_interval = min_interval
        self._clock = clock
        self._buffer = []
        self._last = None          # time of the last publish; None → first delta goes out at once
        self._sent_any = False

    def __call__(self, kind: str, text: str) -> None:
        if kind == 'reset':
            self.reset()
            return
        if kind != 'thinking' or not text:
            return
        self._buffer.append(text)
        now = self._clock()
        if self._last is None or now - self._last >= self._min_interval:
            self._flush(now)

    def flush(self) -> None:
        """Publish whatever is still buffered (call when the response ends)."""
        if self._buffer:
            self._flush(self._clock())

    def reset(self, force: bool = False) -> None:
        """A retry started over: drop buffered text and tell the UI to clear its live row.

        ``force`` publishes the reset even if this emitter sent nothing itself (a fresh
        emitter taking over after a failed attempt that used a different one).
        """
        self._buffer.clear()
        self._last = None
        if self._sent_any or force:
            self._sent_any = False
            self._safe_publish('thinking_reset', {})

    def _flush(self, now: float) -> None:
        content = ''.join(self._buffer)
        self._buffer.clear()
        self._last = now
        self._sent_any = True
        self._safe_publish('thinking_delta', {'content': content})

    def _safe_publish(self, event_type: str, payload: dict) -> None:
        try:
            self._publish(event_type, payload)
        except Exception:
            pass   # a preview must never break the turn


def make_realtime_thinking_emitter(agent_id: str, session_id: Optional[str]) -> ThinkingStreamEmitter:
    """Emitter that publishes to the durable realtime journal for one session."""
    from backend.realtime_store import realtime_store
    turn_id = realtime_store.current_turn_id(session_id) if session_id else None

    def publish(event_type: str, payload: dict) -> None:
        realtime_store.publish('chat', event_type, payload, agent_id=agent_id,
                               session_id=session_id, turn_id=turn_id)

    return ThinkingStreamEmitter(publish)
