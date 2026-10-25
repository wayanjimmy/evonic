"""Regression tests: runpy/bash must honour exact-boolean ``_skip_safety``.

Only a server-owned ``_skip_safety is True`` (set on trusted post-approval
replay) may bypass the safety pipeline.  Truthy user/LLM values (``1``,
``'true'``, ``{}`` …) must NOT bypass safety — this guards against a
prompt-injection attempt to disable the classifier via a tool argument.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.tools import bash, runpy


class _FakePipeline:
    def __init__(self):
        self.calls = 0

    def check(self, code, tool_type="python", agent_context=None):
        self.calls += 1
        return {
            "level": "requires_approval",
            "score": 12,
            "reasons": ["test"],
            "blocked_patterns": ["test"],
            "requires_approval": True,
            "approval_info": {"risk_level": "high", "description": "x", "categories": ["test"]},
            "decim_safety": {"event_id": "evt-1"},
        }


class _FakeBackend:
    def __init__(self):
        self.ran = False

    def run_python(self, code, timeout, env):
        self.ran = True
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    def run_bash(self, script, timeout, env):
        self.ran = True
        return {"exit_code": 0, "stdout": "", "stderr": ""}


class _FakeRegistry:
    def __init__(self, backend):
        self._backend = backend

    def get_backend(self, session_id, agent):
        return self._backend


def _install(monkeypatch, module, pipeline, backend):
    monkeypatch.setattr("backend.tools.lib.safety_pipeline.get_safety_pipeline", lambda: pipeline)
    # bash.py imports get_safety_pipeline at module import time, so patch the
    # module attribute too (runpy imports it lazily inside execute()).
    if hasattr(module, "get_safety_pipeline"):
        monkeypatch.setattr(module, "get_safety_pipeline", lambda: pipeline)
    monkeypatch.setattr(module, "registry", _FakeRegistry(backend))


def test_runpy_string_true_does_not_bypass_safety(monkeypatch):
    pipeline, backend = _FakePipeline(), _FakeBackend()
    _install(monkeypatch, runpy, pipeline, backend)

    result = runpy.execute({"_skip_safety": "true"}, {"code": "print(1)"})

    assert pipeline.calls == 1
    assert backend.ran is False
    assert result["level"] == "requires_approval"


def test_runpy_integer_one_does_not_bypass_safety(monkeypatch):
    pipeline, backend = _FakePipeline(), _FakeBackend()
    _install(monkeypatch, runpy, pipeline, backend)

    result = runpy.execute({"_skip_safety": 1}, {"code": "print(1)"})

    assert pipeline.calls == 1
    assert backend.ran is False
    assert result["level"] == "requires_approval"


def test_runpy_exact_true_bypasses_safety(monkeypatch):
    pipeline, backend = _FakePipeline(), _FakeBackend()
    _install(monkeypatch, runpy, pipeline, backend)

    result = runpy.execute({"_skip_safety": True}, {"code": "print(1)"})

    assert pipeline.calls == 0
    assert backend.ran is True
    assert result.get("exit_code") == 0


def test_bash_string_true_does_not_bypass_safety(monkeypatch):
    pipeline, backend = _FakePipeline(), _FakeBackend()
    _install(monkeypatch, bash, pipeline, backend)
    # The root-fs scan guard would otherwise need the DB; the script is benign.
    monkeypatch.setattr(bash, "_root_fs_scan_guard_enabled", lambda: False)
    monkeypatch.setattr(bash, "_get_long_running_setting", lambda: False)

    result = bash.execute({"_skip_safety": "yes"}, {"script": "echo hi"})

    assert pipeline.calls == 1
    assert backend.ran is False
    assert result["level"] == "requires_approval"


def test_bash_exact_true_bypasses_safety(monkeypatch):
    pipeline, backend = _FakePipeline(), _FakeBackend()
    _install(monkeypatch, bash, pipeline, backend)
    monkeypatch.setattr(bash, "_root_fs_scan_guard_enabled", lambda: False)
    monkeypatch.setattr(bash, "_get_long_running_setting", lambda: False)

    result = bash.execute({"_skip_safety": True}, {"script": "echo hi"})

    assert pipeline.calls == 0
    assert backend.ran is True
    assert result.get("exit_code") == 0
