"""Ownership regressions for the BLE thread coordinator.

These tests pin the coordinator's cleanup state machine: timed-out workers and
in-flight starts stay owned across cleanup so a later cleanup can join them,
rejected native starts are retired from tracking, and start-after-cleanup is
refused.
"""

import threading
from collections.abc import Generator

import pytest

from meshtastic.interfaces.ble.coordination import ThreadCoordinator

_SHORT_JOIN_TIMEOUT = 0.05
_WAIT_TIMEOUT = 2.0


@pytest.fixture
def short_join_timeout(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Shrink the coordinator's per-thread join timeout for fast tests."""
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.coordination.EVENT_THREAD_JOIN_TIMEOUT",
        _SHORT_JOIN_TIMEOUT,
    )
    yield


@pytest.mark.unit
def test_cleanup_timeout_retains_worker_for_later_join(
    short_join_timeout: None,
) -> None:
    """A worker that outlives the cleanup join stays owned and joins later."""
    coordinator = ThreadCoordinator()
    entered = threading.Event()
    release = threading.Event()

    def _work() -> None:
        entered.set()
        release.wait(timeout=_WAIT_TIMEOUT)

    worker = coordinator._create_thread(_work, "slow-worker")
    coordinator._start_thread(worker)
    assert entered.wait(timeout=_WAIT_TIMEOUT)

    try:
        coordinator._cleanup()
        assert coordinator._cleaned_up
        # The join timed out; the timed-out worker remains owned so a later
        # cleanup can join it instead of leaking it detached.
        assert worker in coordinator._threads
    finally:
        release.set()
        worker.join(timeout=_WAIT_TIMEOUT)

    coordinator._cleanup()
    assert worker not in coordinator._threads
    assert coordinator._threads == []


@pytest.mark.unit
def test_start_in_progress_worker_retained_across_cleanup(
    short_join_timeout: None,
) -> None:
    """A thread blocked inside start() during cleanup stays tracked and joins later."""
    coordinator = ThreadCoordinator()
    start_entering = threading.Event()
    start_gate = threading.Event()
    worker_release = threading.Event()

    def _work() -> None:
        worker_release.wait(timeout=_WAIT_TIMEOUT)

    worker = coordinator._create_thread(_work, "late-worker")
    original_start = worker.start

    def _gated_start() -> None:
        start_entering.set()
        assert start_gate.wait(timeout=_WAIT_TIMEOUT)
        original_start()

    worker.start = _gated_start  # type: ignore[method-assign]

    starter = threading.Thread(
        target=coordinator._start_thread, args=(worker,), name="late-starter"
    )
    starter.start()
    try:
        assert start_entering.wait(timeout=_WAIT_TIMEOUT)
        coordinator._cleanup()
        # The starter holds the create/start handoff, so cleanup must retain
        # the pending registration instead of orphaning the startup.
        assert worker in coordinator._threads
        assert worker in coordinator._pending_start

        start_gate.set()
        starter.join(timeout=_WAIT_TIMEOUT)
        assert not starter.is_alive()
        assert worker.is_alive()
        assert worker in coordinator._threads
    finally:
        start_gate.set()
        worker_release.set()
        if worker.ident is not None:
            worker.join(timeout=_WAIT_TIMEOUT)

    coordinator._cleanup()
    assert coordinator._threads == []
    assert coordinator._pending_start == set()


@pytest.mark.unit
def test_native_start_failure_retires_worker_from_tracking(
    short_join_timeout: None,
) -> None:
    """A rejected native start leaves no runnable owner and is retired."""
    coordinator = ThreadCoordinator()

    def _work() -> None:
        return None

    worker = coordinator._create_thread(_work, "failed-worker")

    def _failing_start() -> None:
        raise OSError("thread resources exhausted")

    worker.start = _failing_start  # type: ignore[method-assign]

    with pytest.raises(OSError, match="thread resources exhausted"):
        coordinator._start_thread(worker)

    assert worker not in coordinator._pending_start
    assert worker not in coordinator._starting_now
    assert worker not in coordinator._threads


@pytest.mark.unit
def test_cleanup_blocks_previously_created_unstarted_thread(
    short_join_timeout: None,
) -> None:
    """Start-after-cleanup is refused so an unstarted thread never detaches."""
    coordinator = ThreadCoordinator()
    entered = threading.Event()

    worker = coordinator._create_thread(entered.set, "not-started-worker")
    coordinator._cleanup()
    coordinator._start_thread(worker)

    assert not entered.is_set()
    assert worker.ident is None


@pytest.mark.unit
def test_cleanup_wakes_waiters_holding_event_references(
    short_join_timeout: None,
) -> None:
    """Existing waiters are woken by cleanup even after the registry is cleared."""
    coordinator = ThreadCoordinator()
    event = coordinator._create_event("ownership-event")
    waiter_result: list[bool] = []

    def _wait() -> None:
        waiter_result.append(event.wait(timeout=_WAIT_TIMEOUT))

    waiter = threading.Thread(target=_wait, name="event-waiter")
    waiter.start()

    coordinator._cleanup()
    waiter.join(timeout=_WAIT_TIMEOUT)

    assert waiter_result == [True]
    # Post-cleanup lookups find no registry entry and never block.
    assert coordinator._get_event("ownership-event") is None
    assert coordinator._wait_for_event("ownership-event", timeout=0.01) is False
    assert coordinator._create_event("ownership-event").is_set() is True
