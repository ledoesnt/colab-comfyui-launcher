"""One monotonic submission cutoff, shared by local locks and CLI calls.

Only submission uses this context. The independent release process does not
inherit a submitter's cutoff. Nested operations may shorten it, never reset it.
"""

import fcntl
import time
from contextlib import contextmanager
from contextvars import ContextVar

_deadline = ContextVar("colab_submission_deadline", default=None)


class DeadlineExceeded(TimeoutError):
    pass


def current():
    return _deadline.get()


def timeout(seconds):
    limit = current()
    remaining = seconds if limit is None else min(seconds, limit - time.monotonic())
    if remaining <= 0:
        raise DeadlineExceeded(
            "Original submission deadline reached; no new request allowed."
        )
    return remaining


def pause(seconds):
    time.sleep(timeout(seconds))
    timeout(1)


@contextmanager
def bounded(absolute_monotonic):
    old = current()
    token = _deadline.set(
        min(old, absolute_monotonic) if old is not None else absolute_monotonic
    )
    try:
        timeout(1)
        yield
    finally:
        _deadline.reset(token)


def flock(fd, *, nonblocking=False):
    if current() is None or nonblocking:
        fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        return
    while True:
        timeout(1)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            pause(0.02)
