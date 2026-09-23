"""
Prevents two bot processes from ever running against the same data
directory at once - two instances both writing to the same SQLite file (or
both trying to move the same item through its pipeline) is a straightforward
way to corrupt data or double-process work.

Uses an OS-level advisory lock (fcntl.flock) on a small lock file rather
than a PID file with manual staleness checks: the kernel releases the lock
automatically the moment the holding process exits for any reason,
including a crash or `kill -9`, so there's nothing to clean up and no
"is that PID actually still alive" guessing. This only works within one
machine's filesystem (not over NFS) - fine here, since nothing in this
project's deployment story runs the data directory over a network mount.
"""
import fcntl
import os
from pathlib import Path

_lock_handle = None


class AlreadyRunningError(RuntimeError):
    pass


def acquire(lock_path) -> None:
    """
    Grabs the lock or raises AlreadyRunningError. The open file handle is
    kept in a module-level variable for the life of the process - closing
    it (or the process exiting) releases the lock, so callers don't need
    to do anything special on normal exit, only on the error path (there's
    nothing to unwind - the process is about to exit anyway).
    """
    global _lock_handle
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise AlreadyRunningError(
            f"Another bot process already holds the lock at {lock_path} - "
            "refusing to start a second instance against the same data "
            "directory (this protects against two processes corrupting the "
            "same database). Stop the other process first, or point "
            "DATABASE_PATH/PHOTO_DIR at a different data directory."
        )
    handle.write(str(os.getpid()))
    handle.flush()
    _lock_handle = handle


def release() -> None:
    """Releases the lock early (used on a clean shutdown) - safe to call
    even if acquire() was never called or already released."""
    global _lock_handle
    if _lock_handle is None:
        return
    try:
        fcntl.flock(_lock_handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    _lock_handle.close()
    _lock_handle = None
