"""Native future completion and cancellation remain authoritative at poll boundaries."""

import threading
from concurrent.futures import Future

import pytest

from meshtastic.cancellation import (
    ConnectionCancelledError,
    _cancellable_init,
    _wait_future,
)


class CompletesAtPollBoundary(Future[int]):
    """Represent completion after a timed wait wakes but before state inspection."""

    def __init__(self, event: threading.Event | None = None) -> None:
        super().__init__()
        self.event = event

    def result(self, timeout: float | None = None) -> int:
        if not self.done():
            self.set_result(42)
            if self.event is not None:
                self.event.set()
            raise TimeoutError("poll interval elapsed")
        return super().result(timeout)


def await_in_constructor(future: Future[int], event: threading.Event) -> int:
    """Exercise the actual invocation scope rather than modifying thread-local state."""

    @_cancellable_init
    def wait(*, cancelEvent: threading.Event) -> int:
        return _wait_future(future, timeout=5)

    return wait(cancelEvent=event)


def test_success_after_poll_timeout_is_not_reported_as_native_failure() -> None:
    """A completed success wins over an elapsed intermediate polling interval."""
    assert await_in_constructor(CompletesAtPollBoundary(), threading.Event()) == 42


def test_native_timeout_is_still_propagated() -> None:
    """Recovering a completed future does not hide the native operation's error."""
    future: Future[int] = Future()
    future.set_exception(TimeoutError("native deadline"))
    with pytest.raises(TimeoutError, match="native deadline"):
        await_in_constructor(future, threading.Event())


def test_cancellation_wins_over_a_completed_native_result() -> None:
    """A late success cannot be adopted after its constructor was abandoned."""
    event = threading.Event()
    with pytest.raises(ConnectionCancelledError):
        await_in_constructor(CompletesAtPollBoundary(event), event)
