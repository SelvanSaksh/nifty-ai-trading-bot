"""
Cross-process advisory lock that guarantees a single trading engine.

The bot must never run in two processes at once: each process keeps its own
in-memory active symbol, candle buffers and open-trade state, so two engines
means duplicate orders *and* a shared "active symbol" that appears to flip on
its own as requests round-robin between processes.

Only the process holding this lock may run the market loop and place orders.
Additional (read-only) API workers can still start, serve requests from the
shared SQLite state, and are reported as ``engine_leader=False``.

The lock is released automatically by the operating system if the process
dies, so there are no stale locks to clean up.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Union


class EngineLockedError(RuntimeError):
    """Raised when another process already owns the trading engine lock."""


class SingletonFileLock:
    """Advisory, OS-level lock held for the lifetime of the owning process."""

    def __init__(self, path: Union[str, Path]):
        self.path = Path(path)
        self._handle = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> bool:
        """Try to take the lock. Returns False if another process holds it."""
        if self._handle is not None:
            return True

        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "r+b" if self.path.exists() else "w+b")
        try:
            self._lock(handle)
        except OSError:
            handle.close()
            return False

        self._handle = handle
        return True

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            self._unlock(handle)
        finally:
            handle.close()

    def __enter__(self) -> "SingletonFileLock":
        if not self.acquire():
            raise EngineLockedError(
                f"Lock held by another process: {self.path}"
            )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    @staticmethod
    def _lock(handle) -> None:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(handle) -> None:
        try:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
