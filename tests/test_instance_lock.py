"""
instance_lock.py - a second process must be refused, release() must let a
new acquire() succeed again, and a process that dies without calling
release() must not leave the lock stuck forever (the OS releases it).

Uses multiprocessing (not manual os.fork - not available on Windows) so
these run on whichever platform actually executes them; on Linux/macOS
this exercises the fcntl path, on Windows the msvcrt path (see
instance_lock.py) - either way, the same acquire()/release() contract is
what's under test.
"""
import multiprocessing
import sys

import pytest

import instance_lock


def _try_acquire(lock_path, result_queue):
    import importlib
    import instance_lock as child_lock
    importlib.reload(child_lock)  # fresh _lock_fd, independent of the parent's
    try:
        child_lock.acquire(lock_path)
        result_queue.put("acquired")
    except child_lock.AlreadyRunningError:
        result_queue.put("refused")


def _acquire_and_die_without_release(lock_path, result_queue):
    import importlib
    import instance_lock as child_lock
    importlib.reload(child_lock)
    child_lock.acquire(lock_path)
    result_queue.put("acquired")
    # Deliberately exit without calling release() - simulates a crash/kill.


@pytest.fixture(autouse=True)
def _release_after_test():
    yield
    instance_lock.release()


def test_second_process_is_refused(tmp_path):
    lock_path = tmp_path / "bot.lock"
    instance_lock.acquire(lock_path)

    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    proc = ctx.Process(target=_try_acquire, args=(lock_path, queue))
    proc.start()
    proc.join(timeout=10)

    assert queue.get(timeout=5) == "refused"


def test_release_then_reacquire_succeeds(tmp_path):
    lock_path = tmp_path / "bot.lock"
    instance_lock.acquire(lock_path)
    instance_lock.release()

    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    proc = ctx.Process(target=_try_acquire, args=(lock_path, queue))
    proc.start()
    proc.join(timeout=10)

    assert queue.get(timeout=5) == "acquired"
    proc.terminate()  # release it so tmp_path cleanup doesn't fight an open handle
    proc.join(timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="proc.kill() semantics for lock release differ enough on Windows to not be worth asserting here")
def test_lock_is_released_if_holder_is_killed(tmp_path):
    lock_path = tmp_path / "bot.lock"

    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    proc = ctx.Process(target=_acquire_and_die_without_release, args=(lock_path, queue))
    proc.start()
    assert queue.get(timeout=10) == "acquired"
    proc.kill()  # SIGKILL - no chance to run release()
    proc.join(timeout=5)

    # The OS should have released the lock the instant the process died.
    instance_lock.acquire(lock_path)
