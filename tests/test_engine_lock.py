"""The trading engine must exist in exactly one process."""
import subprocess
import sys
from pathlib import Path

import pytest

from utils.engine_lock import EngineLockedError, SingletonFileLock

REPO_ROOT = Path(__file__).resolve().parents[1]

CHILD_ACQUIRE = (
    "import sys\n"
    "from utils.engine_lock import SingletonFileLock\n"
    "lock = SingletonFileLock(sys.argv[1])\n"
    "sys.exit(0 if lock.acquire() else 3)\n"
)


def _child_acquire(lock_path: Path) -> int:
    result = subprocess.run(
        [sys.executable, "-c", CHILD_ACQUIRE, str(lock_path)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(REPO_ROOT),
    )
    if result.returncode not in (0, 3):
        raise AssertionError(f"child failed: {result.stderr}")
    return result.returncode


def test_acquire_release_cycle(tmp_path):
    lock_path = tmp_path / "engine.lock"
    lock = SingletonFileLock(lock_path)

    assert lock.acquire() is True
    assert lock.held is True
    # Re-acquiring on the owning instance is a no-op, not a failure.
    assert lock.acquire() is True

    lock.release()
    assert lock.held is False

    # Released lock can be taken again.
    assert SingletonFileLock(lock_path).acquire() is True


def test_context_manager_releases_on_exit(tmp_path):
    with SingletonFileLock(tmp_path / "engine.lock") as lock:
        assert lock.held is True
    assert lock.held is False


def test_context_manager_raises_engine_locked(tmp_path, monkeypatch):
    def _fail(handle):
        raise OSError("already locked")

    monkeypatch.setattr(SingletonFileLock, "_lock", staticmethod(_fail))

    with pytest.raises(EngineLockedError):
        with SingletonFileLock(tmp_path / "engine.lock"):
            pass


def test_second_process_cannot_take_the_lock(tmp_path):
    """Two engine processes = duplicate orders + desynchronized symbols."""
    lock_path = tmp_path / "engine.lock"
    holder = SingletonFileLock(lock_path)
    assert holder.acquire()
    try:
        assert _child_acquire(lock_path) == 3, "second process grabbed the engine lock"
    finally:
        holder.release()

    assert _child_acquire(lock_path) == 0, "lock not released for the next process"


def test_error_type_is_runtime_error():
    assert issubclass(EngineLockedError, RuntimeError)
