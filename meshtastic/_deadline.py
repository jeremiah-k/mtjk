"""Context-local budgets for synchronous library operations."""

from __future__ import annotations

import math
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_DEADLINE: ContextVar[float | None] = ContextVar("meshtastic_deadline", default=None)


class _DeadlineExpired(TimeoutError):
    """An enclosing operation exhausted its monotonic budget."""


def _remaining_timeout(timeout: float) -> float:
    """Clamp a managed wait to its enclosing operation's remaining budget."""
    deadline = _DEADLINE.get()
    if deadline is None:
        return timeout
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _DeadlineExpired("Operation deadline expired")
    return min(timeout, remaining)


@contextmanager
def _operation_deadline(timeout: float) -> Iterator[float]:
    """Apply a finite positive budget; nested operations cannot extend it."""
    _validate_timeout(timeout)
    deadline = time.monotonic() + timeout
    outer = _DEADLINE.get()
    if outer is not None:
        deadline = min(deadline, outer)
    token = _DEADLINE.set(deadline)
    try:
        _remaining_timeout(timeout)
        yield deadline
    finally:
        _DEADLINE.reset(token)


def _current_deadline() -> float | None:
    """Return the enclosing operation deadline for callbacks on other threads."""
    return _DEADLINE.get()


def _validate_timeout(timeout: float) -> None:
    """Reject invalid programmatic budgets before creating invocation state."""
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("timeout must be finite and positive")
