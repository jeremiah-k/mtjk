"""Exercise the real deferred publisher through BLE's compatibility adapter."""

import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import pytest

from meshtastic.interfaces.ble.compatibility_service import (
    BLECompatibilityEventPublisher,
)
from meshtastic.util import DeferredExecution

pytestmark = pytest.mark.unit
_WAIT = 5.0


def test_ble_admission_recovers_dead_worker_in_fifo_order() -> None:
    executor = DeferredExecution._create_lazy("ble-dead-worker")
    order: list[str] = []
    with patch.object(executor, "_run", lambda: None):
        executor.queueWork(lambda: order.append("old"))
        assert executor.join(_WAIT)
    publisher = BLECompatibilityEventPublisher(
        Mock(), publishing_thread_provider=lambda: executor
    )
    try:
        assert publisher._resolve_publishing_thread() is executor
        assert publisher._enqueue_publish_callback(
            executor, lambda: order.append("new"), prefer_non_blocking=True
        )
        executor.stop()
        assert executor.join(_WAIT)
        assert order == ["old", "new"]
        assert not publisher._enqueue_publish_callback(
            executor, lambda: order.append("late")
        )
    finally:
        executor.stop()
        assert executor.join(_WAIT)


def test_ble_flush_waits_for_terminal_drain_on_the_worker() -> None:
    executor = DeferredExecution("ble-terminal-drain")
    entered = threading.Event()
    release = threading.Event()
    flushed = threading.Event()
    workers: list[threading.Thread] = []

    def block() -> None:
        entered.set()
        assert release.wait(_WAIT)
        workers.append(threading.current_thread())

    publisher = BLECompatibilityEventPublisher(
        Mock(), publishing_thread_provider=lambda: executor
    )
    try:
        executor.queueWork(block)
        assert entered.wait(_WAIT)
        executor.queueWork(lambda: workers.append(threading.current_thread()))
        executor.stop()
        assert publisher._resolve_publishing_thread(for_flush=True) is executor
        with ThreadPoolExecutor() as pool:
            flush = pool.submit(publisher.drain_publish_queue, flushed)
            try:
                assert not flushed.is_set()
                assert workers == []
            finally:
                release.set()
            flush.result(_WAIT)
        assert flushed.is_set()
        assert executor.join(_WAIT)
        assert executor.thread is not None
        assert workers == [executor.thread, executor.thread]
    finally:
        release.set()
        executor.stop()
        assert executor.join(_WAIT)


def test_ble_flush_recovers_failed_terminal_worker() -> None:
    executor = DeferredExecution._create_lazy("ble-failed-terminal-worker")
    release = threading.Event()
    delivered = threading.Event()
    publisher = BLECompatibilityEventPublisher(
        Mock(), publishing_thread_provider=lambda: executor
    )
    with patch.object(executor, "_run", release.wait):
        executor.queueWork(delivered.set)
        executor.stop()
        release.set()
        assert not executor.join(_WAIT)
    try:
        publisher.wait_for_disconnect_notifications(_WAIT)
        assert delivered.is_set()
    finally:
        executor.stop()
        assert executor.join(_WAIT)
