from __future__ import annotations

import collections
import threading
from typing import Any

import pytest

from meshtastic.mesh_interface_runtime.queue_send import _QueueSendRuntime
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


@pytest.mark.unit
def test_has_free_space_returns_true_when_no_status() -> None:
    harness = _QueueHarness()
    runtime = _QueueSendRuntime(
        lock=harness.lock,
        get_queue=lambda: harness.queue,
        get_queue_status=lambda: harness.queue_status,
        set_queue_status=harness.set_queue_status,
        queue_wait_delay_seconds=0.0,
    )

    assert runtime._has_free_space()


@pytest.mark.unit
def test_claim_does_nothing_when_no_queue_status() -> None:
    harness = _QueueHarness()
    runtime = _QueueSendRuntime(
        lock=harness.lock,
        get_queue=lambda: harness.queue,
        get_queue_status=lambda: harness.queue_status,
        set_queue_status=harness.set_queue_status,
        queue_wait_delay_seconds=0.0,
    )

    runtime._claim()

    assert len(harness.queue) == 0


@pytest.mark.unit
def test_claim_decrements_free_when_status_available() -> None:
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

    runtime._claim()

    assert status.free == 2


@pytest.mark.unit
def test_pop_for_send_returns_oldest_entry_when_no_status() -> None:
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

    popped = runtime._pop_for_send()

    assert popped == (123, packet)


@pytest.mark.unit
def test_pop_for_send_returns_none_when_queue_empty() -> None:
    harness = _QueueHarness()
    runtime = _QueueSendRuntime(
        lock=harness.lock,
        get_queue=lambda: harness.queue,
        get_queue_status=lambda: harness.queue_status,
        set_queue_status=harness.set_queue_status,
        queue_wait_delay_seconds=0.0,
    )

    popped = runtime._pop_for_send()

    assert popped is None


@pytest.mark.unit
def test_pop_for_send_obeys_free_space_limit() -> None:
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

    popped = runtime._pop_for_send()

    assert popped is None


@pytest.mark.unit
def test_correlate_queue_status_reply_removes_matching_packet() -> None:
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

    runtime._correlate_queue_status_reply(status)

    assert 456 not in harness.queue


@pytest.mark.unit
def test_correlate_queue_status_reply_mismatched_id_preserves_queue() -> None:
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
    status.mesh_packet_id = 999
    status.free = 1

    runtime._correlate_queue_status_reply(status)

    assert 456 in harness.queue


@pytest.mark.unit
def test_record_queue_status_persists_status() -> None:
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

    runtime._record_queue_status(status)

    assert harness.queue_status == status


@pytest.mark.unit
def test_non_packet_send_does_not_drain_existing_queue() -> None:
    harness = _QueueHarness()
    runtime = _QueueSendRuntime(
        lock=harness.lock,
        get_queue=lambda: harness.queue,
        get_queue_status=lambda: harness.queue_status,
        set_queue_status=harness.set_queue_status,
        queue_wait_delay_seconds=0.0,
    )
    queued_packet = mesh_pb2.ToRadio()
    queued_packet.packet.id = 321
    harness.queue[321] = queued_packet
    control_frame = mesh_pb2.ToRadio()
    control_frame.disconnect = True
    sent: list[mesh_pb2.ToRadio] = []

    runtime._send_to_radio(
        control_frame,
        send_impl=sent.append,
        pop_for_send=runtime._pop_for_send,
        sleep_fn=lambda _: None,
    )

    assert sent == [control_frame]
    assert list(harness.queue.items()) == [(321, queued_packet)]


@pytest.mark.unit
def test_sent_packet_without_queue_status_does_not_track_awaiting_correlation() -> None:
    harness = _QueueHarness()
    runtime = _QueueSendRuntime(
        lock=harness.lock,
        get_queue=lambda: harness.queue,
        get_queue_status=lambda: harness.queue_status,
        set_queue_status=harness.set_queue_status,
        queue_wait_delay_seconds=0.0,
    )
    packet = mesh_pb2.ToRadio()
    packet.packet.id = 654

    runtime._send_to_radio(
        packet,
        send_impl=lambda _: None,
        pop_for_send=runtime._pop_for_send,
        sleep_fn=lambda _: None,
    )

    assert runtime._awaiting_queue_status_ids == {}


@pytest.mark.unit
def test_send_to_radio_propagates_transport_failure() -> None:
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
        runtime._send_to_radio(
            packet,
            send_impl=fail_send,
            pop_for_send=runtime._pop_for_send,
            sleep_fn=lambda _: None,
        )
