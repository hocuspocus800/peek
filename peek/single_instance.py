"""One Peek per user session.

ScreenCaptureKit completion handlers can hang on macOS 14 when two processes with
the same bundle id run, and two event taps would fire every hotkey twice. A
`flock` on a file in Application Support is released by the kernel when the
process dies, so a crash never leaves a stale lock.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self, *, wait_for_pid: int | None = None, timeout: float = 10.0) -> bool:
        """Take the lock. With *wait_for_pid* (a relaunch), first wait for that
        process to exit so the old instance can release the lock."""
        deadline = time.monotonic() + timeout
        if wait_for_pid is not None:
            while _pid_alive(wait_for_pid) and time.monotonic() < deadline:
                time.sleep(0.05)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                    os.close(fd)
                    raise
                # During a relaunch the old process may still be shutting down.
                if wait_for_pid is None or time.monotonic() >= deadline:
                    os.close(fd)
                    return False
                time.sleep(0.05)

        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None
