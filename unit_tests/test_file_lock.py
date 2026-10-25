"""Tests for ``backend.file_lock`` — the cross-platform advisory lock helper.

These tests exercise both platform backends without needing a Windows runner:
the POSIX path is tested against the real :mod:`fcntl`, while the Windows path
is tested by injecting a fake :mod:`msvcrt` module and flipping the module's
``_IS_WINDOWS`` switch.
"""

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import file_lock

REPO_ROOT = Path(__file__).resolve().parent.parent


class _FakeFcntl(types.ModuleType):
    """Minimal stand-in for :mod:`fcntl` that records ``flock`` calls."""

    LOCK_EX = 2
    LOCK_NB = 4
    LOCK_UN = 8

    def __init__(self):
        super().__init__("fcntl")
        self.calls = []

    def flock(self, fd, flags):
        self.calls.append((fd, flags))


class _FakeMsvcrt(types.ModuleType):
    """Minimal stand-in for :mod:`msvcrt` that records ``locking`` calls.

    When ``raise_errno`` is set the first ``locking`` call raises ``OSError``
    with that errno, emulating a lock already held by another process.
    """

    LK_LOCK = 1
    LK_NBLCK = 2
    LK_UNLCK = 3

    def __init__(self, raise_errno=None):
        super().__init__("msvcrt")
        self.calls = []
        self.raise_errno = raise_errno

    def locking(self, fd, mode, nbytes):
        self.calls.append((fd, mode, nbytes))
        if self.raise_errno is not None and mode != self.LK_UNLCK:
            raise OSError(self.raise_errno, os.strerror(self.raise_errno))


class TestPosixBackend(unittest.TestCase):
    def test_open_and_lock_calls_flock_nonblocking(self):
        fake = _FakeFcntl()
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "pid.lock")
            with mock.patch.object(file_lock, "_IS_WINDOWS", False), \
                    mock.patch.dict(sys.modules, {"fcntl": fake}):
                fd = file_lock.open_and_lock(path)
                self.assertIsInstance(fd, int)
                self.assertEqual(
                    fake.calls, [(fd, fake.LOCK_EX | fake.LOCK_NB)]
                )
                file_lock.release(fd)
                self.assertEqual(
                    fake.calls[-1], (fd, fake.LOCK_UN)
                )

    def test_release_closes_descriptor(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "pid.lock")
            fd = file_lock.open_and_lock(path)
            file_lock.release(fd)
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_real_contention_maps_to_already_locked(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "pid.lock")
            fd = file_lock.open_and_lock(path)
            try:
                with self.assertRaises(file_lock.AlreadyLockedError):
                    file_lock.open_and_lock(path)
            finally:
                file_lock.release(fd)

    def test_already_locked_is_oserror(self):
        self.assertTrue(issubclass(file_lock.AlreadyLockedError, OSError))


class TestWindowsBackend(unittest.TestCase):
    def test_empty_file_is_padded_and_one_byte_locked(self):
        fake = _FakeMsvcrt()
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "pid.lock")
            with mock.patch.object(file_lock, "_IS_WINDOWS", True), \
                    mock.patch.dict(sys.modules, {"msvcrt": fake}):
                fd = file_lock.open_and_lock(path)
                self.assertGreaterEqual(os.path.getsize(path), 1)
                self.assertEqual(fake.calls[0], (fd, fake.LK_NBLCK, 1))
                file_lock.release(fd)
                self.assertEqual(fake.calls[-1], (fd, fake.LK_UNLCK, 1))

    def test_contention_maps_to_already_locked_and_closes_fd(self):
        fake = _FakeMsvcrt(raise_errno=13)  # EACCES
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "pid.lock")
            with mock.patch.object(file_lock, "_IS_WINDOWS", True), \
                    mock.patch.dict(sys.modules, {"msvcrt": fake}):
                with self.assertRaises(file_lock.AlreadyLockedError):
                    file_lock.open_and_lock(path)
                # the failed attempt must not leak its descriptor
                leaked_fd = fake.calls[0][0]
                with self.assertRaises(OSError):
                    os.fstat(leaked_fd)

    def test_blocking_mode_uses_blocking_flag(self):
        fake = _FakeMsvcrt()
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "pid.lock")
            with mock.patch.object(file_lock, "_IS_WINDOWS", True), \
                    mock.patch.dict(sys.modules, {"msvcrt": fake}):
                fd = file_lock.open_and_lock(path, blocking=True)
                self.assertEqual(fake.calls[0][1], fake.LK_LOCK)
                file_lock.release(fd)

    def test_release_none_is_a_noop(self):
        file_lock.release(None)  # must not raise


class TestNoUnixOnlyImports(unittest.TestCase):
    """Regression guard for GitHub Issue #124.

    ``fcntl`` does not exist on Windows, so it must never be imported at module
    import time from the CLI, the app entrypoint, or the update manager.
    """

    def test_no_top_level_fcntl_import(self):
        for rel in ("cli/commands.py", "app.py", "backend/update_manager.py"):
            src = (REPO_ROOT / rel).read_text()
            for lineno, line in enumerate(src.splitlines(), start=1):
                self.assertNotEqual(
                    line.strip(),
                    "import fcntl",
                    f"{rel}:{lineno} must not import fcntl at module scope",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
