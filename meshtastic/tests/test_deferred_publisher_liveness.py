"""Worker ownership, failure isolation, and terminal shutdown regressions."""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from meshtastic.util import DeferredExecution

pytestmark = pytest.mark.unit

_WAIT = 5.0


@pytest.mark.parametrize(
    "error", [ValueError, SystemExit, KeyboardInterrupt, GeneratorExit, BaseException]
)
def test_callback_failure_does_not_interrupt_fifo_drain(
    error: type[BaseException], caplog: pytest.LogCaptureFixture
) -> None:
    """Even callback-local termination signals cannot kill the shared worker."""
    executor = DeferredExecution("callback-failure")
    worker = executor.thread
    order: list[str] = []

    def fail() -> None:
        order.append("failed")
        raise error("synthetic callback failure")

    try:
        with caplog.at_level(logging.ERROR):
            executor.queueWork(lambda: order.append("first"))
            executor.queueWork(fail)
            assert executor._queue_work_non_blocking(lambda: order.append("last"))
            executor.stop()
            assert executor.join(_WAIT)
        assert order == ["first", "failed", "last"]
        assert executor.thread is worker
        assert "Unexpected error in deferred execution" in caplog.text
        assert not executor._queue_work_non_blocking(lambda: order.append("late"))
        executor.queueWork(lambda: order.append("late"))
        assert order == ["first", "failed", "last"]
    finally:
        executor.stop()
        assert executor.join(_WAIT)


@pytest.mark.parametrize("recover_with", ["blocking", "nonblocking", "stop"])
def test_dead_worker_recovers_pending_work_in_fifo_order(
    recover_with: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A dead owner is replaced on admission or stop without replaying work."""
    executor = DeferredExecution._create_lazy("unexpected-worker-exit")
    started = threading.Event()
    release = threading.Event()
    order: list[str] = []

    def exit_worker() -> None:
        started.set()
        release.wait(_WAIT)

    try:
        with patch.object(executor, "_run", exit_worker):
            executor.queueWork(lambda: order.append("old"))
            assert started.wait(_WAIT)
            previous = executor.thread
            release.set()
            assert executor.join(_WAIT)
        assert previous is not None and not previous.is_alive()
        with caplog.at_level(logging.ERROR):
            if recover_with == "blocking":
                executor.queueWork(lambda: order.append("new"))
            elif recover_with == "nonblocking":
                assert executor._queue_work_non_blocking(lambda: order.append("new"))
            executor.stop()
            assert executor.join(_WAIT)
        assert executor.thread is not previous
        assert order == (["old"] if recover_with == "stop" else ["old", "new"])
        assert "exited; restarting" in caplog.text
    finally:
        release.set()
        executor.stop()
        assert executor.join(_WAIT)


def test_concurrent_recovery_has_only_one_worker() -> None:
    """Many producers racing a dead owner cannot start overlapping consumers."""
    executor = DeferredExecution._create_lazy("concurrent-recovery")
    with patch.object(executor, "_run", lambda: None):
        executor.queueWork(lambda: None)
        assert executor.join(_WAIT)
    barrier = threading.Barrier(9)
    workers: set[threading.Thread] = set()
    completed: list[int] = []

    def record(index: int) -> None:
        workers.add(threading.current_thread())
        completed.append(index)

    def produce(index: int) -> None:
        barrier.wait(_WAIT)
        executor.queueWork(lambda: record(index))

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(produce, index) for index in range(8)]
            try:
                barrier.wait(_WAIT)
                for future in futures:
                    future.result(_WAIT)
            finally:
                barrier.abort()
        executor.stop()
        assert executor.join(_WAIT)
        assert sorted(completed) == list(range(8))
        assert workers == {executor.thread}
    finally:
        executor.stop()
        assert executor.join(_WAIT)


def test_failed_replacement_keeps_pending_work_and_allows_retry() -> None:
    """A thread-start failure must not admit new work or discard the old queue."""
    executor = DeferredExecution._create_lazy("restart-failure")
    completed: list[str] = []
    with patch.object(executor, "_run", lambda: None):
        executor.queueWork(lambda: completed.append("accepted"))
        assert executor.join(_WAIT)
    old_worker = executor.thread
    try:
        with patch.object(
            threading.Thread, "start", side_effect=RuntimeError("start failed")
        ):
            with pytest.raises(RuntimeError, match="start failed"):
                executor.queueWork(lambda: completed.append("not accepted"))
            assert executor.thread is old_worker
            with pytest.raises(RuntimeError, match="start failed"):
                executor.stop()
        executor.queueWork(lambda: completed.append("recovered"))
        executor.stop()
        assert executor.join(_WAIT)
        assert completed == ["accepted", "recovered"]
    finally:
        executor.stop()
        assert executor.join(_WAIT)


def test_concurrent_producers_and_stop_respect_admission_boundary() -> None:
    """All successfully admitted work drains; rejected work never runs."""
    executor = DeferredExecution._create_lazy("stop-race")
    gate = threading.Barrier(9)
    executed: list[int] = []

    def produce(index: int) -> tuple[int, bool]:
        gate.wait(_WAIT)
        return index, executor._queue_work_non_blocking(lambda: executed.append(index))

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(produce, index) for index in range(8)]
            try:
                gate.wait(_WAIT)
                executor.stop()
                results = [future.result(_WAIT) for future in futures]
            finally:
                gate.abort()
        assert executor.join(_WAIT)
        assert sorted(executed) == [index for index, accepted in results if accepted]
        assert not executor._queue_work_non_blocking(lambda: executed.append(99))
    finally:
        executor.stop()
        assert executor.join(_WAIT)
