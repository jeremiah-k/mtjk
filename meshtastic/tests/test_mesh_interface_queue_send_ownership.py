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
from collections.abc import Callable
from types import SimpleNamespace

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
        assert harness.queue[31] is False

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
    # Replace only this runtime module's clock reference, not the process-wide
    # stdlib time.monotonic used by pytest and other threads.
    clock = [created_at]
    monkeypatch.setattr(
        queue_send_module,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0]),
    )

    runtime._correlate_queue_status_reply(_status(packet_id=101))

    assert harness.queue[101] is False

    live = _packet(101)
    harness.queue[101] = live
    clock[0] = created_at + queue_send_module.UNEXPECTED_ACK_TTL_SECONDS + 1.0

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
    queued.packet.decoded.payload = b"first"
    harness.queue[0] = queued

    followup = _packet(0)
    followup.packet.decoded.payload = b"second"
    sent: list[mesh_pb2.ToRadio] = []
    runtime._send_to_radio(
        followup,
        send_impl=sent.append,
        sleep_fn=lambda _seconds: None,
    )

    assert sent == [queued, followup]
    assert [packet.packet.decoded.payload for packet in sent] == [b"first", b"second"]
    assert queued not in harness.queue.values()
    assert 0 not in harness.queue
    assert 0 not in runtime._active_send_ids


@pytest.mark.unit
def test_backlog_packet_id_stays_owned_while_another_sender_drains_it() -> None:
    harness = _QueueHarness()
    runtime = _make_runtime(harness)
    queued = _packet(0xDEED)
    harness.queue[0xDEED] = queued
    entered = threading.Event()
    release = threading.Event()
    failure: list[BaseException] = []

    def blocking_send(packet: mesh_pb2.ToRadio) -> None:
        if packet is queued:
            entered.set()
            assert release.wait(10.0)

    def drain() -> None:
        try:
            runtime._send_to_radio(
                _packet(0xFEED),
                send_impl=blocking_send,
                sleep_fn=lambda _seconds: None,
            )
        except BaseException as exc:  # noqa: BLE001 - preserve assertion from thread
            failure.append(exc)

    sender = threading.Thread(target=drain)
    sender.start()
    try:
        assert entered.wait(10.0)
        assert 0xDEED in runtime._active_send_ids
        with pytest.raises(ValueError, match="already queued or in flight"):
            runtime._send_to_radio(
                _packet(0xDEED),
                send_impl=lambda _packet: None,
                sleep_fn=lambda _seconds: None,
            )
    finally:
        release.set()
        sender.join(10.0)

    assert not sender.is_alive()
    assert failure == []
    assert runtime._active_send_ids == set()
    assert runtime._active_send_id_claims == {}


@pytest.mark.unit
def test_release_without_matching_claim_is_a_tolerated_noop() -> None:
    harness = _QueueHarness()
    runtime = _make_runtime(harness)
    with runtime._lock:
        # A release inside a finally block must never mask the exception it
        # unwinds with a KeyError, even when this path never claimed the ID.
        runtime._release_active_send_id_locked(0x51)
        assert runtime._active_send_id_claims == {}
        assert 0x51 not in runtime._active_send_ids

        runtime._claim_active_send_id_locked(0x51)
        runtime._release_active_send_id_locked(0x51)
        runtime._release_active_send_id_locked(0x51)

    assert runtime._active_send_id_claims == {}
    assert 0x51 not in runtime._active_send_ids


@pytest.mark.unit
def test_preserved_local_replacement_refreshes_capacity_class() -> None:
    harness = _QueueHarness()
    harness.queue_status = _status(free=0)
    original = _packet(900)
    replacement = _packet(900)
    followup = _packet(901)
    local_ids = {id(replacement), id(followup)}

    def replace_then_abort() -> str:
        harness.queue[900] = replacement
        return "closing"

    runtime = _make_runtime(
        harness,
        abort_wait=replace_then_abort,
        uses_tx_queue_capacity=lambda packet: id(packet) not in local_ids,
    )
    with pytest.raises(queue_send_module.QueueWaitError, match="closing"):
        runtime._send_to_radio(
            original,
            send_impl=lambda _packet: None,
            sleep_fn=lambda _seconds: None,
        )
    assert harness.queue[900] is replacement
    assert runtime._packet_uses_capacity_by_id[900] is False

    sent: list[mesh_pb2.ToRadio] = []
    runtime._send_to_radio(followup, send_impl=sent.append, sleep_fn=lambda _: None)
    assert sent == [replacement, followup]
    assert harness.queue_status.free == 0
    assert not harness.queue


@pytest.mark.unit
def test_transport_failure_reconciles_local_replacement_at_zero_capacity() -> None:
    harness = _QueueHarness()
    harness.queue_status = _status(free=1)
    original = _packet(920)
    replacement = _packet(920)
    local_packet = _packet(921)
    local_ids = {id(replacement), id(local_packet)}
    runtime = _make_runtime(
        harness,
        uses_tx_queue_capacity=lambda packet: id(packet) not in local_ids,
    )

    def replace_then_fail(_packet: mesh_pb2.ToRadio) -> None:
        harness.queue[920] = replacement
        harness.queue_status = _status(free=0)
        raise OSError("transport closed")

    with pytest.raises(OSError, match="transport closed"):
        runtime._send_to_radio(
            original, send_impl=replace_then_fail, sleep_fn=lambda _: None
        )

    assert harness.queue[920] is replacement
    assert runtime._packet_uses_capacity_by_id[920] is False
    sent: list[mesh_pb2.ToRadio] = []
    runtime._send_to_radio(local_packet, send_impl=sent.append, sleep_fn=lambda _: None)
    assert sent == [replacement, local_packet]
    assert not harness.queue


@pytest.mark.unit
def test_early_ack_evidence_outlives_tombstone_eviction_until_reconciled() -> None:
    harness = _QueueHarness()
    runtime = _make_runtime(harness)
    original = _packet(600)

    def ack_then_fail(_packet: mesh_pb2.ToRadio) -> None:
        runtime._correlate_queue_status_reply(_status(packet_id=600))
        assert harness.queue[600] is False
        for packet_id in range(
            1000, 1000 + queue_send_module.MAX_UNEXPECTED_ACK_TOMBSTONES + 40
        ):
            runtime._correlate_queue_status_reply(_status(packet_id=packet_id))
        assert harness.queue[600] is False
        assert 600 in runtime._unexpected_ack_tombstones
        raise OSError("already reached radio")

    with pytest.raises(OSError, match="already reached radio"):
        runtime._send_to_radio(
            original,
            send_impl=ack_then_fail,
            sleep_fn=lambda _seconds: None,
        )
    assert 600 not in harness.queue
    assert 600 not in runtime._unexpected_ack_tombstones
    assert runtime._active_send_ids == set()
    assert (
        len(runtime._unexpected_ack_tombstones)
        <= queue_send_module.MAX_UNEXPECTED_ACK_TOMBSTONES
    )


@pytest.mark.unit
def test_unmatched_zero_ack_does_not_discard_firmware_assigned_packet() -> None:
    harness = _QueueHarness()
    runtime = _make_runtime(harness)
    queued = _packet(0)
    harness.queue[0] = queued
    runtime._correlate_queue_status_reply(_status(packet_id=0))
    assert harness.queue[0] is queued
    assert 0 not in runtime._unexpected_ack_tombstones


@pytest.mark.unit
def test_concurrent_zero_id_senders_keep_distinct_queue_reservations() -> None:
    harness = _QueueHarness()
    runtime = _make_runtime(harness)
    entered = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []
    sent: list[bytes] = []
    first = _packet(0)
    first.packet.decoded.payload = b"first"
    second = _packet(0)
    second.packet.decoded.payload = b"second"

    def blocked_send(packet: mesh_pb2.ToRadio) -> None:
        if packet is first:
            entered.set()
            assert release.wait(10.0)
        sent.append(bytes(packet.packet.decoded.payload))

    def sender() -> None:
        try:
            runtime._send_to_radio(
                first, send_impl=blocked_send, sleep_fn=lambda _: None
            )
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=sender)
    thread.start()
    try:
        assert entered.wait(10.0)
        runtime._send_to_radio(second, send_impl=blocked_send, sleep_fn=lambda _: None)
        with harness.lock:
            assert 0 not in harness.queue
            assert all(key < 0 for key in harness.queue)
    finally:
        release.set()
        thread.join(10.0)
    assert not thread.is_alive()
    assert errors == []
    assert sorted(sent) == [b"first", b"second"]
    assert not harness.queue


@pytest.mark.unit
def test_zero_id_deadline_drops_private_key_even_after_key_zero_backlog_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meshtastic._deadline import _DeadlineExpired

    harness = _QueueHarness()
    runtime = _make_runtime(harness)
    harness.queue[0] = _packet(0)
    incoming = _packet(0)
    calls = [0]
    sent: list[mesh_pb2.ToRadio] = []

    def bounded_timeout(seconds: float) -> float:
        calls[0] += 1
        if calls[0] == 3:
            raise _DeadlineExpired("cancel after backlog send")
        return seconds

    monkeypatch.setattr(queue_send_module, "_remaining_timeout", bounded_timeout)
    with pytest.raises(_DeadlineExpired, match="cancel after backlog"):
        runtime._send_to_radio(incoming, send_impl=sent.append, sleep_fn=lambda _: None)
    assert len(sent) == 1
    assert not harness.queue


@pytest.mark.unit
@pytest.mark.parametrize("uses_capacity", [True, False])
def test_retry_of_failed_transport_packet_id_is_allowed_when_unclaimed(
    uses_capacity: bool,
) -> None:
    harness = _QueueHarness()
    runtime = _make_runtime(harness, uses_tx_queue_capacity=lambda _: uses_capacity)
    first = _packet(0xF1)

    def fail(_packet: mesh_pb2.ToRadio) -> None:
        raise OSError("transport lost")

    with pytest.raises(OSError, match="transport lost"):
        runtime._send_to_radio(first, send_impl=fail, sleep_fn=lambda _: None)
    assert harness.queue[0xF1] is first
    assert not runtime._active_send_ids

    replacement = _packet(0xF1)
    sent: list[mesh_pb2.ToRadio] = []
    runtime._send_to_radio(replacement, send_impl=sent.append, sleep_fn=lambda _: None)
    assert len(sent) == 1
    assert sent[0] is replacement
    assert not harness.queue
    assert not runtime._retryable_failed_packets


@pytest.mark.unit
def test_retry_does_not_replace_unrelated_queued_packet_with_same_id() -> None:
    harness = _QueueHarness()
    runtime = _make_runtime(harness)
    harness.queue[0xFB] = _packet(0xFB)
    with pytest.raises(ValueError, match="already queued or in flight"):
        runtime._send_to_radio(
            _packet(0xFB), send_impl=lambda _: None, sleep_fn=lambda _: None
        )
    survivor = harness.queue[0xFB]
    assert isinstance(survivor, mesh_pb2.ToRadio)
    assert survivor.packet.id == 0xFB
