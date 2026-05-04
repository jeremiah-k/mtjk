from __future__ import annotations

import collections
import threading
from typing import Any

import pytest

from meshtastic.protobuf import mesh_pb2


class _QueueHarness:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.queue: collections.OrderedDict[int, mesh_pb2.ToRadio | bool] = (
            collections.OrderedDict()
        )
        self.queue_status: mesh_pb2.QueueStatus | None = None

    def set_queue_status(self, queue_status: mesh_pb2.QueueStatus | None) -> None:
        self.queue_status = queue_status


def test_has_free_space_returns_true_when_no_status() -> None:
    from meshtastic.mesh_interface_runtime.queue_send import _QueueSendRuntime

    harness = _QueueHarness()
    runtime = _QueueSendRuntime(
        lock=harness.lock,
        get_queue=lambda: harness.queue,
        get_queue_status=lambda: harness.queue_status,
        set_queue_status=harness.set_queue_status,
        queue_wait_delay_seconds=0.0,
    )

    assert runtime.has_free_space()


def test_claim_does_nothing_when_no_queue_status() -> None:
    from meshtastic.mesh_interface_runtime.queue_send import _QueueSendRuntime

    harness = _QueueHarness()
    runtime = _QueueSendRuntime(
        lock=harness.lock,
        get_queue=lambda: harness.queue,
        get_queue_status=lambda: harness.queue_status,
        set_queue_status=harness.set_queue_status,
        queue_wait_delay_seconds=0.0,
    )

    runtime.claim()

    assert len(harness.queue) == 0


def test_claim_decrements_free_when_status_available() -> None:
    from meshtastic.mesh_interface_runtime.queue_send import _QueueSendRuntime

    harness = _QueueHarness()
    runtime = _QueueSendRuntime(
        lock=harness.lock,
        get_queue=lambda: harness.queue,
        get_queue_status=lambda: harness.queue_status,
        set_queue_status=harness.set_queue_status,
        queue_wait_delay_seconds=0.0,
    )
    status = mesh_pb2.QueueStatus()
    status.free = 3
    status.maxlen = 10
    harness.set_queue_status(status)

    runtime.claim()

    assert status.free == 2


def test_pop_for_send_returns_oldest_entry_when_no_status() -> None:
    from meshtastic.mesh_interface_runtime.queue_send import _QueueSendRuntime

    harness = _QueueHarness()
    runtime = _QueueSendRuntime(
        lock=harness.lock,
        get_queue=lambda: harness.queue,
        get_queue_status=lambda: harness.queue_status,
        set_queue_status=harness.set_queue_status,
        queue_wait_delay_seconds=0.0,
    )
    packet = mesh_pb2.ToRadio()
    packet.packet.id = 123
    harness.queue[123] = packet

    popped = runtime.pop_for_send()

    assert popped == (123, packet)


def test_pop_for_send_returns_none_when_queue_empty() -> None:
    from meshtastic.mesh_interface_runtime.queue_send import _QueueSendRuntime

    harness = _QueueHarness()
    runtime = _QueueSendRuntime(
        lock=harness.lock,
        get_queue=lambda: harness.queue,
        get_queue_status=lambda: harness.queue_status,
        set_queue_status=harness.set_queue_status,
        queue_wait_delay_seconds=0.0,
    )

    popped = runtime.pop_for_send()

    assert popped is None


def test_pop_for_send_obeys_free_space_limit() -> None:
    from meshtastic.mesh_interface_runtime.queue_send import _QueueSendRuntime

    harness = _QueueHarness()
    runtime = _QueueSendRuntime(
        lock=harness.lock,
        get_queue=lambda: harness.queue,
        get_queue_status=lambda: harness.queue_status,
        set_queue_status=harness.set_queue_status,
        queue_wait_delay_seconds=0.0,
    )
    status = mesh_pb2.QueueStatus()
    status.free = 0
    status.maxlen = 10
    harness.set_queue_status(status)
    packet = mesh_pb2.ToRadio()
    packet.packet.id = 456
    harness.queue[456] = packet

    popped = runtime.pop_for_send()

    assert popped is None


def test_correlate_queue_status_reply_removes_matching_packet() -> None:
    from meshtastic.mesh_interface_runtime.queue_send import _QueueSendRuntime

    harness = _QueueHarness()
    runtime = _QueueSendRuntime(
        lock=harness.lock,
        get_queue=lambda: harness.queue,
        get_queue_status=lambda: harness.queue_status,
        set_queue_status=harness.set_queue_status,
        queue_wait_delay_seconds=0.0,
    )
    packet = mesh_pb2.ToRadio()
    packet.packet.id = 456
    harness.queue[456] = packet
    status = mesh_pb2.QueueStatus()
    status.mesh_packet_id = 456
    status.free = 1

    runtime.correlate_queue_status_reply(status)

    assert 456 not in harness.queue


def test_record_queue_status_persists_status() -> None:
    from meshtastic.mesh_interface_runtime.queue_send import _QueueSendRuntime

    harness = _QueueHarness()
    runtime = _QueueSendRuntime(
        lock=harness.lock,
        get_queue=lambda: harness.queue,
        get_queue_status=lambda: harness.queue_status,
        set_queue_status=harness.set_queue_status,
        queue_wait_delay_seconds=0.0,
    )
    status = mesh_pb2.QueueStatus()
    status.free = 4
    status.maxlen = 10

    runtime.record_queue_status(status)

    assert harness.queue_status == status


def test_send_to_radio_propagates_transport_failure() -> None:
    from meshtastic.mesh_interface_runtime.queue_send import _QueueSendRuntime

    harness = _QueueHarness()
    runtime = _QueueSendRuntime(
        lock=harness.lock,
        get_queue=lambda: harness.queue,
        get_queue_status=lambda: harness.queue_status,
        set_queue_status=harness.set_queue_status,
        queue_wait_delay_seconds=0.0,
    )
    packet = mesh_pb2.ToRadio()
    packet.packet.id = 789
    harness.queue[789] = packet

    def fail_send(_: Any) -> None:
        raise OSError("radio unavailable")

    with pytest.raises(OSError, match="radio unavailable"):
        runtime.send_to_radio(
            packet,
            send_impl=fail_send,
            pop_for_send=runtime.pop_for_send,
            sleep_fn=lambda _: None,
        )
