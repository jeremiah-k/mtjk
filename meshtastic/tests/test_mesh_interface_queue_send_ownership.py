"""Firmware TX-queue packet-ID ownership and unexpected-ACK tombstone behavior.

These tests pin the queue-send runtime contract that a nonzero packet ID is
owned for the whole duration of its send: a live duplicate (queued or in
flight) is rejected instead of silently replacing the owner's queue entry,
firmware-assigned IDs (packet ID 0) are exempt, and unmatched ACK sentinels
are bounded and retired without ever dropping a live packet.
"""

from __future__ import annotations

import collections
import threading
import time
from collections.abc import Callable

import pytest

from meshtastic.mesh_interface_runtime import queue_send as queue_send_module
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


def _make_runtime(
    harness: _QueueHarness,
    *,
    abort_wait: Callable[[], str | None] | None = None,
    uses_tx_queue_capacity: Callable[[mesh_pb2.ToRadio], bool] | None = None,
) -> _QueueSendRuntime:
    return _QueueSendRuntime(
        lock=harness.lock,
        get_queue=lambda: harness.queue,
        get_queue_status=lambda: harness.queue_status,
        set_queue_status=harness.set_queue_status,
        queue_wait_delay_seconds=0.0,
        abort_wait=abort_wait,
        uses_tx_queue_capacity=uses_tx_queue_capacity,
    )


def _packet(packet_id: int) -> mesh_pb2.ToRadio:
    to_radio = mesh_pb2.ToRadio()
    to_radio.packet.id = packet_id
    return to_radio


def _status(packet_id: int = 0, *, free: int = 1) -> mesh_pb2.QueueStatus:
    queue_status = mesh_pb2.QueueStatus()
    queue_status.mesh_packet_id = packet_id
    queue_status.free = free
    queue_status.maxlen = 16
    return queue_status


@pytest.mark.unit
def test_in_flight_packet_id_rejects_reuse_until_send_completes() -> None:
    harness = _QueueHarness()
    runtime = _make_runtime(harness)
    entered = threading.Event()
    release = threading.Event()
    transport_failures: list[str] = []

    def blocked_send(_to_radio: mesh_pb2.ToRadio) -> None:
        entered.set()
        if not release.wait(10.0):
            transport_failures.append("send_impl was never released")

    first = _packet(0x42)
    sender = threading.Thread(
        target=lambda: runtime._send_to_radio(
            first,
            send_impl=blocked_send,
            sleep_fn=lambda _seconds: None,
        ),
    )
    sender.start()
    try:
        assert entered.wait(10.0)
        assert 0x42 not in harness.queue
        with pytest.raises(ValueError, match="already queued or in flight"):
            runtime._send_to_radio(
                _packet(0x42),
                send_impl=lambda _to_radio: None,
                sleep_fn=lambda _seconds: None,
            )
        assert 0x42 in runtime._active_send_ids
    finally:
        release.set()
        sender.join(10.0)

    assert transport_failures == []
    assert not sender.is_alive()
    assert runtime._active_send_ids == set()
    assert 0x42 not in harness.queue


@pytest.mark.unit
def test_queued_packet_id_rejects_overwrite() -> None:
    harness = _QueueHarness()
    runtime = _make_runtime(harness)
    queued = _packet(0xABC)
    harness.queue[0xABC] = queued

    with pytest.raises(ValueError, match="already queued or in flight"):
        runtime._send_to_radio(
            _packet(0xABC),
            send_impl=lambda _to_radio: None,
            sleep_fn=lambda _seconds: None,
        )

    assert harness.queue[0xABC] is queued
    assert runtime._active_send_ids == set()


@pytest.mark.unit
def test_packet_id_is_reusable_after_send_completes() -> None:
    harness = _QueueHarness()
    runtime = _make_runtime(harness)
    packet = _packet(0x77)

    runtime._send_to_radio(
        packet,
        send_impl=lambda _to_radio: None,
        sleep_fn=lambda _seconds: None,
    )
    runtime._send_to_radio(
        packet,
        send_impl=lambda _to_radio: None,
        sleep_fn=lambda _seconds: None,
    )

    assert runtime._active_send_ids == set()


@pytest.mark.unit
def test_queue_wait_abort_preserves_external_replacement() -> None:
    harness = _QueueHarness()
    harness.queue_status = _status(free=0)
    original = _packet(900)
    replacement = _packet(900)

    def replace_then_abort() -> str | None:
        harness.queue[900] = replacement
        return "transport closed"

    runtime = _make_runtime(harness, abort_wait=replace_then_abort)

    with pytest.raises(queue_send_module.QueueWaitError, match="transport closed"):
        runtime._send_to_radio(
            original,
            send_impl=lambda _to_radio: None,
            sleep_fn=lambda _seconds: None,
        )

    assert harness.queue[900] is replacement
    assert runtime._active_send_ids == set()


@pytest.mark.unit
def test_capacity_exempt_send_failure_releases_id_ownership() -> None:
    harness = _QueueHarness()
    runtime = _make_runtime(harness, uses_tx_queue_capacity=lambda _packet: False)

    def failing_send(_to_radio: mesh_pb2.ToRadio) -> None:
        raise OSError("radio write failed")

    packet = _packet(0x44)
    with pytest.raises(OSError, match="radio write failed"):
        runtime._send_to_radio(
            packet,
            send_impl=failing_send,
            sleep_fn=lambda _seconds: None,
        )

    assert runtime._active_send_ids == set()
    assert harness.queue[0x44] is packet


@pytest.mark.unit
def test_early_ack_is_reconciled_without_leak() -> None:
    harness = _QueueHarness()
    runtime = _make_runtime(harness)
    packet = _packet(31)

    def ack_immediately(_to_radio: mesh_pb2.ToRadio) -> None:
        runtime._correlate_queue_status_reply(_status(packet_id=31))

    runtime._send_to_radio(
        packet,
        send_impl=ack_immediately,
        sleep_fn=lambda _seconds: None,
    )

    assert 31 not in harness.queue
    assert 31 not in runtime._unexpected_ack_tombstones
    assert runtime._active_send_ids == set()


@pytest.mark.unit
def test_reconciliation_retires_ack_marker_bookkeeping() -> None:
    harness = _QueueHarness()
    runtime = _make_runtime(harness)
    packet = _packet(0xDEAD)
    harness.queue[0xDEAD] = False
    runtime._unexpected_ack_tombstones[0xDEAD] = 123.0
    resent_queue: collections.OrderedDict[int, mesh_pb2.ToRadio | bool] = (
        collections.OrderedDict([(0xDEAD, packet)])
    )

    runtime.reconcile_resent_queue(
        resent_queue=resent_queue,
        sent_packet_ids={0xDEAD},
    )

    assert 0xDEAD not in harness.queue
    assert 0xDEAD not in runtime._unexpected_ack_tombstones


@pytest.mark.unit
def test_unsolicited_queue_status_replies_cannot_grow_without_bound() -> None:
    harness = _QueueHarness()
    runtime = _make_runtime(harness)

    for packet_id in range(1, queue_send_module.MAX_UNEXPECTED_ACK_TOMBSTONES + 201):
        runtime._correlate_queue_status_reply(_status(packet_id=packet_id))

    assert len(harness.queue) <= queue_send_module.MAX_UNEXPECTED_ACK_TOMBSTONES
    assert len(harness.queue) == len(runtime._unexpected_ack_tombstones)
    assert 1 not in harness.queue
    assert queue_send_module.MAX_UNEXPECTED_ACK_TOMBSTONES + 200 in harness.queue


@pytest.mark.unit
def test_expired_tombstone_is_retired_without_touching_real_packet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _QueueHarness()
    runtime = _make_runtime(harness)
    created_at = 100.0
    # queue_send reads time.monotonic through the stdlib time module; the
    # constant lambda keeps the test deterministic even for extra callers.
    monkeypatch.setattr(
        time,
        "monotonic",
        lambda: created_at,
    )

    runtime._correlate_queue_status_reply(_status(packet_id=101))

    assert harness.queue[101] is False

    live = _packet(101)
    harness.queue[101] = live
    expired_at = created_at + queue_send_module.UNEXPECTED_ACK_TTL_SECONDS + 1.0
    monkeypatch.setattr(
        time,
        "monotonic",
        lambda: expired_at,
    )

    runtime._correlate_queue_status_reply(_status(packet_id=102))

    assert harness.queue.get(101) is live
    assert 101 not in runtime._unexpected_ack_tombstones
    assert harness.queue[102] is False


@pytest.mark.unit
def test_zero_packet_id_send_is_never_tracked_as_in_flight() -> None:
    harness = _QueueHarness()
    runtime = _make_runtime(harness)
    entered = threading.Event()
    release = threading.Event()

    def blocked_send(_to_radio: mesh_pb2.ToRadio) -> None:
        entered.set()
        if not release.wait(10.0):
            raise AssertionError("send_impl was never released")

    # Packet ID 0 means the firmware assigns the real ID on transmit.
    firmware_assigned = _packet(0)
    sender = threading.Thread(
        target=lambda: runtime._send_to_radio(
            firmware_assigned,
            send_impl=blocked_send,
            sleep_fn=lambda _seconds: None,
        ),
    )
    sender.start()
    try:
        assert entered.wait(10.0)
        assert 0 not in runtime._active_send_ids
        followup = _packet(0)
        runtime._send_to_radio(
            followup,
            send_impl=lambda _to_radio: None,
            sleep_fn=lambda _seconds: None,
        )
        assert 0 not in runtime._active_send_ids
    finally:
        release.set()
        sender.join(10.0)

    assert not sender.is_alive()
    assert runtime._active_send_ids == set()
    assert 0 not in harness.queue


@pytest.mark.unit
def test_zero_packet_id_send_is_allowed_while_zero_id_entry_is_queued() -> None:
    harness = _QueueHarness()
    runtime = _make_runtime(harness)
    queued = _packet(0)
    harness.queue[0] = queued

    followup = _packet(0)
    sent: list[mesh_pb2.ToRadio] = []
    runtime._send_to_radio(
        followup,
        send_impl=sent.append,
        sleep_fn=lambda _seconds: None,
    )

    assert sent == [followup]
    assert queued not in harness.queue.values()
    assert 0 not in harness.queue
    assert 0 not in runtime._active_send_ids
