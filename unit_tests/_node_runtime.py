"""Shared helper for tests that execute JavaScript via a Node.js subprocess.

``shutil.which("node")`` is not enough: a binary can be present but unusable
(mis-matched libc/V8, crashing on startup, etc.).  These harness tests skip only
when Node is genuinely able to run a trivial program, so the suite degrades
gracefully on hosts where Node crashes while still running the tests in CI
(which provisions a supported Node).
"""

import functools
import shutil
import subprocess


@functools.lru_cache(maxsize=1)
def node_bin():
    """Return a path to a *working* Node.js binary, or ``None``.

    Probes ``node`` then ``nodejs`` by evaluating a trivial program.  A binary
    that is absent or that fails to execute (non-zero exit, including signal
    aborts) is treated as unavailable.
    """
    for candidate in ("node", "nodejs"):
        path = shutil.which(candidate)
        if not path:
            continue
        try:
            proc = subprocess.run(
                [path, "-e", "0"], capture_output=True, timeout=30
            )
        except Exception:
            continue
        if proc.returncode == 0:
            return path
    return None
