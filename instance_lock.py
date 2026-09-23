"""
Prevents two bot processes from ever running against the same data
directory at once - two instances both writing to the same SQLite file (or
both trying to move the same item through its pipeline) is a straightforward
way to corrupt data or double-process work.

Uses an OS-level advisory lock on a small lock file rather than a PID file
with manual staleness checks: the kernel releases the lock automatically
the moment the holding process exits for any reason, including a crash or
being killed, so there's nothing to clean up and no "is that PID actually
still alive" guessing. Linux/macOS use fcntl.flock; Windows has no fcntl at
all, so it uses msvcrt.locking on the same file instead - both lock the
same 1-byte region of the same file, just via each platform's own API.
This only works within one machine's filesystem (not over a network share)
- fine here, since nothing in this project's deployment story runs the
data directory over a network mount.
"""
import os
import sys
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

_lock_fd = None


class AlreadyRunningError(RuntimeError):
    pass


def acquire(lock_path) -> None:
    """
    Grabs the lock or raises AlreadyRunningError. The open file descriptor
    is kept in a module-level variable for the life of the process -
    closing it (or the process exiting) releases the lock, so callers
    don't need to do anything special on normal exit, only on the error
    path (there's nothing to unwind - the process is about to exit anyway).

    Opens with O_CREAT (create if missing) but deliberately not O_TRUNC -
    truncating an already-locked file out from under the process holding
    it would be a mess. Not being able to open/lock it is exactly how a
    second instance is detected.
    """
    global _lock_fd
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    try:
        if sys.platform == "win32":
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise AlreadyRunningError(
            f"Another bot process already holds the lock at {lock_path} - "
            "refusing to start a second instance against the same data "
            "directory (this protects against two processes corrupting the "
            "same database). Stop the other process first, or point "
            "DATABASE_PATH/PHOTO_DIR at a different data directory."
        )

    pid_bytes = str(os.getpid()).encode()
    os.write(fd, pid_bytes)
    os.ftruncate(fd, len(pid_bytes))  # drop any leftover bytes from a longer previous PID
    _lock_fd = fd


def release() -> None:
    """Releases the lock early (used on a clean shutdown) - safe to call
    even if acquire() was never called or already released."""
    global _lock_fd
    if _lock_fd is None:
        return
    try:
        if sys.platform == "win32":
            os.lseek(_lock_fd, 0, os.SEEK_SET)  # msvcrt unlocks the same region it locked, by position
            msvcrt.locking(_lock_fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
    except OSError:
        pass
    os.close(_lock_fd)
    _lock_fd = None
