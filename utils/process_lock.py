"""Prevent multiple instances of a process from running simultaneously.

Uses fcntl.flock() for an exclusive file lock that is automatically released
by the OS when the process exits (even on SIGKILL or power loss).
"""

import atexit
import fcntl
import logging
import sys

logger = logging.getLogger(__name__)

_lock_fd = None


def acquire_lock(name: str) -> None:
    """Acquire an exclusive process lock, or exit if another instance is running.

    Args:
        name: Identifier for the lock (e.g. "node", "gateway").
              Lock file will be /tmp/data_log_{name}.lock
    """
    global _lock_fd

    lock_path = f"/tmp/data_log_{name}.lock"
    try:
        _lock_fd = open(lock_path, "w")
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(
            f"Another instance of {name} is already running. Exiting.",
            file=sys.stderr,
        )
        sys.exit(1)

    atexit.register(_release_lock)


def _release_lock():
    global _lock_fd
    if _lock_fd is not None:
        fcntl.flock(_lock_fd, fcntl.LOCK_UN)
        _lock_fd.close()
        _lock_fd = None
