"""Exercise the real deferred publisher through BLE's compatibility adapter."""

import threading
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


def test_ble_fallback_cannot_steal_terminal_sentinel() -> None:
    executor = DeferredExecution("ble-terminal-drain")
    entered = threading.Event()
    release = threading.Event()
    flushed = threading.Event()
    order: list[str] = []

    def block() -> None:
        entered.set()
        assert release.wait(_WAIT)
        order.append("first")

    publisher = BLECompatibilityEventPublisher(
        Mock(), publishing_thread_provider=lambda: executor
    )
    try:
        executor.queueWork(block)
        assert entered.wait(_WAIT)
        executor.queueWork(lambda: order.append("second"))
        executor.stop()
        publisher._drain_publish_queue_on_thread(flushed, publishing_thread=executor)
        assert not flushed.is_set()
        assert order == []
        release.set()
        assert executor.join(_WAIT)
        assert order == ["first", "second"]
    finally:
        release.set()
        executor.stop()
        assert executor.join(_WAIT)
