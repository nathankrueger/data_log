"""Prevent multiple instances of a process from running simultaneously.

Uses fcntl.flock() for an exclusive file lock that is automatically released
by the OS when the process exits (even on SIGKILL or power loss).

The lock file contains the PID of the holder. Launch scripts read this to
kill existing processes before starting a new instance.
"""

import atexit
import fcntl
import os
import sys

_lock_fd = None


def acquire_lock(name: str) -> None:
    """Acquire an exclusive process lock, or exit if another instance is running.

    Args:
        name: Identifier for the lock (e.g. "node", "gateway").
              Lock file will be /tmp/data_log_{name}.lock
    """
    global _lock_fd

    lock_path = f"/tmp/data_log_{name}.lock"

    _lock_fd = open(lock_path, "w+")
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Read PID of existing holder for a useful error message
        _lock_fd.seek(0)
        existing_pid = _lock_fd.read().strip()
        pid_info = f" (PID {existing_pid})" if existing_pid else ""
        print(
            f"Another instance of {name} is already running{pid_info}. Exiting.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Write our PID so launch scripts can find us
    _lock_fd.seek(0)
    _lock_fd.truncate()
    _lock_fd.write(str(os.getpid()))
    _lock_fd.flush()

    atexit.register(_release_lock)


def _release_lock():
    global _lock_fd
    if _lock_fd is not None:
        fcntl.flock(_lock_fd, fcntl.LOCK_UN)
        _lock_fd.close()
        _lock_fd = None
