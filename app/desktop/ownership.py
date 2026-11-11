"""In-process admission for an explicitly selected native desktop task.

This does not lock the operating system or stop human/other-application input.
It prevents Misty's legacy pointer dispatch from interleaving with its native
pilot while the task controller owns the foreground operation.
"""
import threading
from contextlib import contextmanager

_LOCK = threading.Lock()
_STATE = threading.Lock()
_ACTIVE = False
_BLOCKED = False


def native_task_active():
    with _STATE:
        return _ACTIVE


def native_task_blocked():
    with _STATE:
        return _BLOCKED


def block_native_admission():
    """Keep all pointer admission closed until restart after unknown cleanup."""
    global _BLOCKED, _ACTIVE
    with _STATE:
        _BLOCKED = True
        _ACTIVE = True


@contextmanager
def native_task_lease():
    global _ACTIVE
    if not _LOCK.acquire(blocking=False):
        raise RuntimeError("Another native desktop task is active")
    with _STATE:
        if _BLOCKED:
            _LOCK.release()
            raise RuntimeError("Native desktop admission is blocked until restart")
        _ACTIVE = True
    try:
        yield
    finally:
        with _STATE:
            _ACTIVE = _BLOCKED
        _LOCK.release()
