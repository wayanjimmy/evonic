"""Live thinking preview: throttling, reset and journal cleanup."""

from backend.agent_runtime.thinking_stream import ThinkingStreamEmitter


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def make(interval=0.1):
    sent, clock = [], Clock()
    return ThinkingStreamEmitter(lambda e, p: sent.append((e, p)), interval, clock), sent, clock


def test_first_delta_is_sent_immediately():
    em, sent, _ = make()
    em('thinking', 'Hel')
    assert sent == [('thinking_delta', {'content': 'Hel'})]


def test_deltas_inside_the_interval_are_coalesced():
    em, sent, clock = make()
    em('thinking', 'a')
    clock.t += 0.01
    em('thinking', 'b')
    clock.t += 0.01
    em('thinking', 'c')
    assert sent == [('thinking_delta', {'content': 'a'})]
    clock.t += 0.2
    em('thinking', 'd')
    assert sent[-1] == ('thinking_delta', {'content': 'bcd'})


def test_flush_sends_the_remainder_once():
    em, sent, clock = make()
    em('thinking', 'a')
    clock.t += 0.01
    em('thinking', 'b')
    em.flush()
    em.flush()
    assert [p['content'] for _, p in sent] == ['a', 'b']


def test_reset_clears_buffer_and_notifies_only_if_something_was_sent():
    em, sent, clock = make()
    em('reset', '')
    assert sent == []
    em('thinking', 'a')
    clock.t += 0.01
    em('thinking', 'b')          # buffered
    em('reset', '')
    assert sent[-1] == ('thinking_reset', {})
    em.flush()                    # buffer was dropped
    assert [e for e, _ in sent] == ['thinking_delta', 'thinking_reset']
    em('thinking', 'x')           # first delta after a reset goes out at once
    assert sent[-1] == ('thinking_delta', {'content': 'x'})


def test_forced_reset_for_a_fresh_emitter():
    em, sent, _ = make()
    em.reset(force=True)
    assert sent == [('thinking_reset', {})]


def test_publish_errors_never_propagate():
    def boom(*_):
        raise RuntimeError('db locked')
    em = ThinkingStreamEmitter(boom)
    em('thinking', 'a')
    em.flush()
    em.reset(force=True)


def test_ignores_other_kinds_and_empty_text():
    em, sent, _ = make()
    em('content', 'x')
    em('thinking', '')
    assert sent == []


def test_finish_turn_drops_preview_events(tmp_path):
    from backend.realtime_store import RealtimeStore
    store = RealtimeStore(db_path=str(tmp_path / 'rt.db'))
    turn_id, _ = store.queue_turn('agent', 'sess')
    store.start_turn(turn_id)
    store.publish('chat', 'thinking_delta', {'content': 'a'}, agent_id='agent', session_id='sess', turn_id=turn_id)
    store.publish('chat', 'thinking_reset', {}, agent_id='agent', session_id='sess', turn_id=turn_id)
    store.publish('chat', 'thinking', {'content': 'abc'}, agent_id='agent', session_id='sess', turn_id=turn_id)
    store.finish_turn(turn_id)
    kinds = [e['event'] for e in store.events_after(0, {'chat'}, session_id='sess')]
    assert 'thinking' in kinds
    assert 'thinking_delta' not in kinds and 'thinking_reset' not in kinds
