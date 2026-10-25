"""Tests for the ``evonic`` launcher's Python interpreter discovery.

GitHub Issue #124: the launcher only probed ``.venv/bin/python`` and
``venv/bin/python``, so a native Windows virtualenv (``Scripts/python.exe``)
was ignored and the launcher fell back to an unrelated system interpreter,
producing a misleading ``ModuleNotFoundError``.

The tests run the real launcher script inside a throwaway project directory
with fake "interpreters" (tiny shell scripts that print a marker) so we can
assert which candidate the launcher picks.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = REPO_ROOT / "evonic"

BASH = shutil.which("bash")


def _write_fake_interpreter(path: Path, marker: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/bash\nprintf '%s\\n' '{marker}'\n")
    path.chmod(0o755)


@unittest.skipUnless(BASH, "bash is required to exercise the launcher")
class LauncherInterpreterTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name)
        shutil.copy(LAUNCHER, self.project / "evonic")
        os.chmod(self.project / "evonic", 0o755)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, path_value=None):
        env = os.environ.copy()
        if path_value is not None:
            env["PATH"] = str(path_value)
        return subprocess.run(
            [BASH, str(self.project / "evonic")],
            capture_output=True,
            text=True,
            env=env,
        )

    def _bin_with(self, *tools):
        """Create a temp bin dir containing only symlinks to ``tools``."""
        bindir = Path(tempfile.mkdtemp())
        for tool in tools:
            real = shutil.which(tool)
            if real:
                os.symlink(real, bindir / tool)
        return bindir

    # -- Windows layout ----------------------------------------------------

    def test_windows_dot_venv_is_detected(self):
        _write_fake_interpreter(
            self.project / ".venv" / "Scripts" / "python.exe", "PICKED_WIN_DOTVENV"
        )
        result = self._run()
        self.assertIn("PICKED_WIN_DOTVENV", result.stdout)

    def test_windows_plain_venv_is_detected(self):
        _write_fake_interpreter(
            self.project / "venv" / "Scripts" / "python.exe", "PICKED_WIN_VENV"
        )
        result = self._run()
        self.assertIn("PICKED_WIN_VENV", result.stdout)

    def test_windows_layout_wins_over_posix_layout(self):
        _write_fake_interpreter(
            self.project / ".venv" / "Scripts" / "python.exe", "PICKED_WIN"
        )
        _write_fake_interpreter(
            self.project / ".venv" / "bin" / "python", "PICKED_POSIX"
        )
        result = self._run()
        self.assertIn("PICKED_WIN", result.stdout)
        self.assertNotIn("PICKED_POSIX", result.stdout)

    # -- POSIX layout ------------------------------------------------------

    def test_posix_dot_venv_still_detected(self):
        _write_fake_interpreter(
            self.project / ".venv" / "bin" / "python", "PICKED_POSIX_DOTVENV"
        )
        result = self._run()
        self.assertIn("PICKED_POSIX_DOTVENV", result.stdout)

    def test_posix_plain_venv_still_detected(self):
        _write_fake_interpreter(
            self.project / "venv" / "bin" / "python", "PICKED_POSIX_VENV"
        )
        result = self._run()
        self.assertIn("PICKED_POSIX_VENV", result.stdout)

    # -- Fallbacks / failure ----------------------------------------------

    def test_system_python3_fallback(self):
        bindir = self._bin_with("dirname", "env", "bash")
        _write_fake_interpreter(bindir / "python3", "PICKED_SYSTEM_PYTHON3")
        result = self._run(path_value=bindir)
        self.assertIn("PICKED_SYSTEM_PYTHON3", result.stdout)

    def test_actionable_error_when_no_interpreter_found(self):
        # A PATH with coreutils but no python/python3 -> launcher must fail
        # with a clear, actionable message rather than silently proceeding.
        bindir = self._bin_with("dirname", "env", "bash")
        result = self._run(path_value=bindir)
        self.assertEqual(result.returncode, 1)
        self.assertIn("no Python interpreter found", result.stderr)
        self.assertIn("python -m venv .venv", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
