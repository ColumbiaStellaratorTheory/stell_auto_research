"""Advisory file locks that the operating system releases when their holder dies.

Used for the machine-wide run-slot pool and for per-spec claims. A crashed or
killed run never leaves a stale lock behind, because the lock is a property of
the open file descriptor, not of a file's existence. POSIX uses flock; Windows
locks the file's first byte with msvcrt.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Callable

if os.name == "nt":
    import msvcrt
else:
    import fcntl

SLOT_POLL_SECONDS = 1.0


def try_lock(path: Path) -> int | None:
    """Exclusively lock `path` (created if absent) without waiting; the fd, or None if held."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if os.name == "nt":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def acquire(path: Path, poll: float = 0.05) -> int:
    """Block until `path` is exclusively locked; return the fd."""
    while True:
        fd = try_lock(path)
        if fd is not None:
            return fd
        time.sleep(poll)


def release(fd: int) -> None:
    if os.name == "nt":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    os.close(fd)


def acquire_slot(
    slots_dir: Path, capacity: int, on_wait: Callable[[], None] = lambda: None
) -> int:
    """Block until one of `capacity` machine-wide run slots is free; return its lock fd.

    `on_wait` is called once, the first time every slot is taken.
    """
    waited = False
    while True:
        for index in range(capacity):
            fd = try_lock(slots_dir / f"slot-{index}.lock")
            if fd is not None:
                return fd
        if not waited:
            on_wait()
            waited = True
        time.sleep(SLOT_POLL_SECONDS)


def report_waiting(capacity: int) -> Callable[[], None]:
    return lambda: print(
        f"All {capacity} run slots on this machine are busy; waiting.", file=sys.stderr
    )
