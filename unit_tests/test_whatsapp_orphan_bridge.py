import os
import signal

from backend.channels.whatsapp import reap_orphan_bridge

BRIDGE_CMD = "node /srv/evonic/backend/channels/whatsapp-bridge/index.js"


def _owner(tmp_path, pid):
    session = tmp_path / "sess"
    session.mkdir()
    owner = tmp_path / "sess.owner"
    owner.mkdir()
    (owner / "pid").write_text(f"{pid}\n")
    return str(session)


class Fake:
    """A tiny process table + kill that removes the process when it gets SIGTERM (or only on SIGKILL when stubborn)."""

    def __init__(self, procs, stubborn=False):
        self.procs = dict(procs)
        self.kills = []
        self.stubborn = stubborn

    def info(self, pid):
        return self.procs.get(pid)

    def kill(self, pid, sig):
        self.kills.append((pid, sig))
        if pid not in self.procs:
            raise ProcessLookupError()
        if sig == signal.SIGKILL or not self.stubborn:
            del self.procs[pid]


def test_orphaned_bridge_is_terminated(tmp_path):
    s = _owner(tmp_path, 4242)
    f = Fake({4242: (1, BRIDGE_CMD)})
    assert reap_orphan_bridge(s, proc_info=f.info, kill=f.kill, sleep=lambda _: None, own_pid=100) == 4242
    assert f.kills == [(4242, signal.SIGTERM)]


def test_stubborn_orphan_gets_sigkill_after_the_timeout(tmp_path):
    s = _owner(tmp_path, 4242)
    f = Fake({4242: (1, BRIDGE_CMD)}, stubborn=True)
    assert reap_orphan_bridge(s, proc_info=f.info, kill=f.kill, sleep=lambda _: None, own_pid=100, timeout=0.5) == 4242
    assert [k[1] for k in f.kills] == [signal.SIGTERM, signal.SIGKILL]


def test_our_own_child_is_left_alone(tmp_path):
    s = _owner(tmp_path, 4242)
    f = Fake({4242: (100, BRIDGE_CMD)})            # parent is this app
    assert reap_orphan_bridge(s, proc_info=f.info, kill=f.kill, sleep=lambda _: None, own_pid=100) is None
    assert f.kills == []


def test_a_reused_pid_that_is_not_a_bridge_is_never_killed(tmp_path):
    s = _owner(tmp_path, 4242)
    f = Fake({4242: (1, "/usr/bin/postgres -D /var/lib/postgresql")})
    assert reap_orphan_bridge(s, proc_info=f.info, kill=f.kill, sleep=lambda _: None, own_pid=100) is None
    assert f.kills == []


def test_dead_owner_and_missing_lock_are_noops(tmp_path):
    f = Fake({})
    assert reap_orphan_bridge(_owner(tmp_path, 4242), proc_info=f.info, kill=f.kill, sleep=lambda _: None, own_pid=100) is None
    assert reap_orphan_bridge(str(tmp_path / "nothing-here"), proc_info=f.info, kill=f.kill, sleep=lambda _: None, own_pid=100) is None
    assert f.kills == []


def test_garbage_or_system_pids_are_ignored(tmp_path):
    f = Fake({1: (0, "init")})
    for bad in ("", "abc", "0", "1"):
        s = tmp_path / f"s{bad or 'e'}"
        s.mkdir()
        (tmp_path / f"s{bad or 'e'}.owner").mkdir()
        (tmp_path / f"s{bad or 'e'}.owner" / "pid").write_text(bad)
        assert reap_orphan_bridge(str(s), proc_info=f.info, kill=f.kill, sleep=lambda _: None, own_pid=100) is None
    assert f.kills == []


def test_shutdown_path_stops_channels():
    import inspect
    from backend.agent_runtime import runtime
    src = inspect.getsource(runtime.AgentRuntime.graceful_shutdown) if hasattr(runtime, "AgentRuntime") else ""
    assert "channel_manager.stop_all" in src
