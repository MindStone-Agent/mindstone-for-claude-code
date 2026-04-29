"""Cross-platform file locking for the sibling bridge.

Uses atomic file creation (open 'x' mode = CREATE_NEW on Windows).
Stale locks (> stale_after seconds old) are removed and retried,
handling the case where a writer process was killed mid-write.
"""

import os
import time


def acquire(path, timeout=5.0, stale_after=30.0):
    """Acquire exclusive lock for path. Returns lock file path."""
    lock = path + ".lock"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            open(lock, "x").close()
            return lock
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock) > stale_after:
                    os.remove(lock)
                    continue
            except OSError:
                pass
            time.sleep(0.05)
    raise TimeoutError(f"Could not acquire lock for {os.path.basename(path)} after {timeout}s")


def release(lock_path):
    try:
        os.remove(lock_path)
    except FileNotFoundError:
        pass
