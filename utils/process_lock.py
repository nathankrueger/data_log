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

# Distinct exit code so supervisors (scripts/launch_gateway_server.sh) don't
# restart-loop against an instance that is already running.
EXIT_ALREADY_RUNNING = 3


def lock_path_for(name: str) -> str:
    """
    Lock file location: ~/.cache/data_log/{name}.lock.

    Not /tmp: periodic temp cleaners (macOS daily, systemd-tmpfiles) can
    delete a long-held lock file, after which a second instance would start
    and both would fight over the radio. Keep in sync with scripts/launch_*.sh.
    """
    lock_dir = os.path.join(os.path.expanduser("~"), ".cache", "data_log")
    os.makedirs(lock_dir, exist_ok=True)
    return os.path.join(lock_dir, f"{name}.lock")


def acquire_lock(name: str) -> None:
    """Acquire an exclusive process lock, or exit if another instance is running.

    Args:
        name: Identifier for the lock (e.g. "node", "gateway").
              Lock file will be ~/.cache/data_log/{name}.lock
    """
    global _lock_fd

    lock_path = lock_path_for(name)

    # Open without truncating so kill_existing() can still read the PID
    # of the lock holder.  Fall back to "w+" if the file doesn't exist yet.
    try:
        _lock_fd = open(lock_path, "r+")
    except FileNotFoundError:
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
        sys.exit(EXIT_ALREADY_RUNNING)

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
