"""Tests for the cross-platform update lock in ``backend.update_manager``.

GitHub Issue #124: the update manager previously imported ``fcntl`` lazily and
treated Windows as a lock *no-op* (``return True, None``), silently losing the
mutual exclusion that protects concurrent web/CLI update and rollback runs.
The lock now goes through :mod:`backend.file_lock`, which is real on Windows too.
"""

import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.update_manager as um
from backend import file_lock


class _FakeMsvcrt(types.ModuleType):
    LK_LOCK = 1
    LK_NBLCK = 2
    LK_UNLCK = 3

    def __init__(self):
        super().__init__("msvcrt")
        self.calls = []

    def locking(self, fd, mode, nbytes):
        self.calls.append((fd, mode, nbytes))


class TestUpdateLock(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self._tmp.name, "update_state.json")
        self._patch = mock.patch.object(
            um, "_get_state_file_path", return_value=self.state_file
        )
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def test_acquire_release_then_reacquire(self):
        acquired, fd = um._acquire_update_lock()
        self.assertTrue(acquired)
        self.assertIsInstance(fd, int)

        um._release_update_lock(fd)

        acquired_again, fd_again = um._acquire_update_lock()
        self.assertTrue(acquired_again)
        um._release_update_lock(fd_again)

    def test_concurrent_acquire_is_rejected(self):
        acquired, fd = um._acquire_update_lock()
        self.assertTrue(acquired)
        try:
            second, second_fd = um._acquire_update_lock()
            self.assertFalse(second)
            self.assertIsNone(second_fd)
        finally:
            um._release_update_lock(fd)

    def test_release_of_none_is_safe(self):
        um._release_update_lock(None)  # must not raise

    def test_windows_is_no_longer_a_noop(self):
        """On Windows the lock must actually be taken, not silently skipped."""
        fake = _FakeMsvcrt()
        with mock.patch.object(file_lock, "_IS_WINDOWS", True), \
                mock.patch.dict(sys.modules, {"msvcrt": fake}):
            acquired, fd = um._acquire_update_lock()
            self.assertTrue(acquired)
            self.assertIsNotNone(fd)
            self.assertEqual(fake.calls[0][1], fake.LK_NBLCK)
            um._release_update_lock(fd)
            self.assertEqual(fake.calls[-1][1], fake.LK_UNLCK)


if __name__ == "__main__":
    unittest.main(verbosity=2)
