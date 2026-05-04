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
        self.failure: BaseException | None = None

    def set_queue_status(self, queue_status: mesh_pb2.QueueStatus | None) -> None:
        self.queue_status = queue_status


def test_claim_records_queue_slot_without_status() -> None:
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
    runtime.claim()

    assert list(harness.queue.items()) == [(0, False)]


def test_pop_for_send_skips_claim_marker_and_returns_packet() -> None:
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
    harness.queue[0] = False
    harness.queue[123] = packet

    popped = runtime.pop_for_send()

    assert popped == (123, packet)
    assert list(harness.queue.items()) == [(0, False)]


def test_queue_status_reply_clears_matching_packet() -> None:
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
