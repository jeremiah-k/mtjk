"""Queue send runtime for MeshInterface.

Internal module — not part of stable public API.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from meshtastic._deadline import _DeadlineExpired, _remaining_timeout
from meshtastic.payload_limits import _validate_firmware_payload_limits
from meshtastic.protobuf import mesh_pb2

logger = logging.getLogger(__name__)

QUEUE_WAIT_DELAY_SECONDS: float = 0.5
QUEUE_WAIT_TIMEOUT_SECONDS: float = 30.0
AWAITING_QUEUE_STATUS_TTL_SECONDS: float = 300.0


class QueueWaitError(TimeoutError):
    """Raised when a packet cannot make progress through the firmware TX queue."""


class _QueueSendRuntime:
    """Owns queue state mutation, resend orchestration, and queue-status correlation."""

    def __init__(
        self,
        *,
        lock: Any,
        get_queue: Callable[[], OrderedDict[int, mesh_pb2.ToRadio | bool]],
        get_queue_status: Callable[[], mesh_pb2.QueueStatus | None],
        set_queue_status: Callable[[mesh_pb2.QueueStatus], None],
        queue_wait_delay_seconds: float,
        queue_wait_timeout_seconds: float = QUEUE_WAIT_TIMEOUT_SECONDS,
        abort_wait: Callable[[], str | None] | None = None,
        uses_tx_queue_capacity: Callable[[mesh_pb2.ToRadio], bool] | None = None,
    ) -> None:
        self._lock = lock
        self._get_queue = get_queue
        self._get_queue_status = get_queue_status
        self._set_queue_status = set_queue_status
        self._queue_wait_delay_seconds = queue_wait_delay_seconds
        self._queue_wait_timeout_seconds = max(0.0, queue_wait_timeout_seconds)
        self._abort_wait = abort_wait
        self._uses_tx_queue_capacity = uses_tx_queue_capacity
        self._packet_uses_capacity_by_id: dict[int, bool] = {}
        # Remember the exact QueueStatus snapshot decremented for each in-flight
        # packet. A newer firmware report supersedes that local claim and must not
        # be incremented if transport I/O later fails.
        self._claimed_queue_status_by_id: dict[int, mesh_pb2.QueueStatus] = {}
        self._awaiting_queue_status_ids: dict[int, float] = {}
        self._queue_status_seen = False

    def _has_free_space(self) -> bool:
        """Return whether queue status indicates free TX slots."""
        with self._lock:
            queue_status = self._get_queue_status()
            if queue_status is None:
                return True
            return queue_status.free > 0

    def has_free_space(self) -> bool:
        """Return whether queue status indicates free TX slots."""
        return self._has_free_space()

    def _claim(self) -> None:
        """Claim one queue slot when queue status is available."""
        with self._lock:
            queue_status = self._get_queue_status()
            if queue_status is None:
                return
            if queue_status.free <= 0:
                return
            queue_status.free -= 1

    def claim(self) -> None:
        """Claim one queue slot when queue status is available."""
        self._claim()

    def _packet_uses_tx_queue_capacity(self, packet: mesh_pb2.ToRadio) -> bool:
        """Return whether a packet consumes one firmware RF TX-queue slot."""
        if self._uses_tx_queue_capacity is None:
            return True
        return self._uses_tx_queue_capacity(packet)

    def _queued_packet_uses_capacity_locked(
        self,
        packet_id: int,
        packet: mesh_pb2.ToRadio,
    ) -> bool:
        """Return stable capacity ownership for a queued packet.

        The caller must hold ``_lock``. Queue entries that predate the runtime's
        own enqueue path are classified lazily once and then retain that decision
        through send, retry, and queue-status reconciliation.
        """
        existing = self._packet_uses_capacity_by_id.get(packet_id)
        if existing is not None:
            return existing
        uses_capacity = self._packet_uses_tx_queue_capacity(packet)
        self._packet_uses_capacity_by_id[packet_id] = uses_capacity
        return uses_capacity

    def _forget_capacity_state_locked(self, packet_id: int) -> None:
        """Forget completed packet capacity state. Caller must hold ``_lock``."""
        self._packet_uses_capacity_by_id.pop(packet_id, None)
        self._claimed_queue_status_by_id.pop(packet_id, None)

    def _enqueue_packet_locked(
        self,
        packet: mesh_pb2.ToRadio,
        *,
        uses_capacity: bool,
    ) -> None:
        """Queue one incoming packet with its snapshotted capacity class."""
        packet_id = packet.packet.id
        self._get_queue()[packet_id] = packet
        self._packet_uses_capacity_by_id[packet_id] = uses_capacity
        self._claimed_queue_status_by_id.pop(packet_id, None)

    def _pop_capacity_exempt_locked(
        self,
    ) -> tuple[int, mesh_pb2.ToRadio] | None:
        """Pop the next RF-capacity-exempt packet. Caller must hold ``_lock``."""
        queue = self._get_queue()
        for packet_id, packet in tuple(queue.items()):
            if not isinstance(packet, mesh_pb2.ToRadio):
                continue
            if self._queued_packet_uses_capacity_locked(packet_id, packet):
                continue
            queue.pop(packet_id, None)
            return packet_id, packet
        return None

    def _pop_for_send(self) -> tuple[int, mesh_pb2.ToRadio | bool] | None:
        """Pop the next sendable entry while honoring firmware RF queue capacity."""
        with self._lock:
            queue = self._get_queue()
            if not queue:
                return None

            queue_status = self._get_queue_status()
            if queue_status is not None and queue_status.free <= 0:
                first_packet_id, first_packet = next(iter(queue.items()))
                if not isinstance(first_packet, mesh_pb2.ToRadio):
                    queue.pop(first_packet_id, None)
                    return first_packet_id, first_packet
                return self._pop_capacity_exempt_locked()

            packet_id, packet = queue.popitem(last=False)
            if queue_status is not None and isinstance(packet, mesh_pb2.ToRadio):
                if self._queued_packet_uses_capacity_locked(packet_id, packet):
                    queue_status.free -= 1
                    self._claimed_queue_status_by_id[packet_id] = queue_status
            return packet_id, packet

    def pop_for_send(self) -> tuple[int, mesh_pb2.ToRadio | bool] | None:
        """Pop the next sendable queue entry while honoring queue free-space state."""
        return self._pop_for_send()

    def _pop_capacity_exempt_for_send(
        self,
    ) -> tuple[int, mesh_pb2.ToRadio] | None:
        """Pop the next entry that can progress without firmware RF capacity."""
        with self._lock:
            return self._pop_capacity_exempt_locked()

    def _send_capacity_exempt_packets(
        self,
        *,
        send_impl: Callable[[mesh_pb2.ToRadio], None],
    ) -> None:
        """Send RF-capacity-exempt backlog without waiting on firmware RF capacity."""
        resent_queue: OrderedDict[int, mesh_pb2.ToRadio | bool] = OrderedDict()
        sent_packet_ids: set[int] = set()
        try:
            while True:
                to_resend = self._pop_capacity_exempt_for_send()
                if to_resend is None:
                    break
                packet_id, packet = to_resend
                resent_queue[packet_id] = packet
                send_impl(packet)
                sent_packet_ids.add(packet_id)
        finally:
            self._reconcile_resent_queue(
                resent_queue=resent_queue,
                sent_packet_ids=sent_packet_ids,
            )

    def _send_to_radio(
        self,
        to_radio: mesh_pb2.ToRadio,
        *,
        send_impl: Callable[[mesh_pb2.ToRadio], None],
        sleep_fn: Callable[[float], None],
    ) -> None:
        """Run outbound send/resend loop using queue ownership semantics."""
        # Validate the final envelope as well as any typed inner payload. This
        # catches nanopb limits on fields populated by the send pipeline itself
        # (for example MeshPacket.channel/hop_limit) and direct ToRadio messages.
        _remaining_timeout(self._queue_wait_timeout_seconds)
        _validate_firmware_payload_limits(to_radio, context="ToRadio")
        if not to_radio.HasField("packet"):
            send_impl(to_radio)
            return

        uses_capacity = self._packet_uses_tx_queue_capacity(to_radio)
        with self._lock:
            self._enqueue_packet_locked(
                to_radio,
                uses_capacity=uses_capacity,
            )
        if not uses_capacity:
            self._send_capacity_exempt_packets(send_impl=send_impl)
            return

        resent_queue: OrderedDict[int, mesh_pb2.ToRadio | bool] = OrderedDict()
        sent_packet_ids: set[int] = set()
        wait_deadline: float | None = None

        def _drop_unsent_incoming() -> None:
            with self._lock:
                packet_id = to_radio.packet.id
                self._get_queue().pop(packet_id, None)
                self._forget_capacity_state_locked(packet_id)

        try:
            while True:
                _remaining_timeout(self._queue_wait_timeout_seconds)
                to_resend = self._pop_for_send()
                if to_resend is None:
                    with self._lock:
                        queue_has_items = bool(self._get_queue())
                    if not queue_has_items:
                        break

                    abort_reason = self._abort_wait() if self._abort_wait else None
                    if abort_reason:
                        _drop_unsent_incoming()
                        raise QueueWaitError(
                            f"Stopped waiting for free space in TX queue: {abort_reason}"
                        )

                    now = time.monotonic()
                    if wait_deadline is None:
                        wait_deadline = now + self._queue_wait_timeout_seconds
                    if now >= wait_deadline:
                        _drop_unsent_incoming()
                        raise QueueWaitError(
                            "Timed out waiting for free space in TX queue "
                            f"after {self._queue_wait_timeout_seconds:.1f}s"
                        )

                    logger.debug("Waiting for free space in TX Queue")
                    sleep_fn(_remaining_timeout(self._queue_wait_delay_seconds))
                    continue

                wait_deadline = None

                packet_id, packet = to_resend
                if packet is False and packet_id in sent_packet_ids:
                    logger.debug("packet %08x got acked during send", packet_id)
                    resent_queue.pop(packet_id, None)
                    sent_packet_ids.remove(packet_id)
                    with self._lock:
                        self._forget_capacity_state_locked(packet_id)
                    continue
                resent_queue[packet_id] = packet
                if not isinstance(packet, mesh_pb2.ToRadio):
                    continue
                if packet is not to_radio:
                    logger.debug("Resending packet ID %08x %s", packet_id, packet)
                send_impl(packet)
                sent_packet_ids.add(packet_id)
        except _DeadlineExpired:
            if to_radio.packet.id not in sent_packet_ids:
                _drop_unsent_incoming()
            raise
        finally:
            self._reconcile_resent_queue(
                resent_queue=resent_queue,
                sent_packet_ids=sent_packet_ids,
            )

    def send_to_radio(
        self,
        to_radio: mesh_pb2.ToRadio,
        *,
        send_impl: Callable[[mesh_pb2.ToRadio], None],
        sleep_fn: Callable[[float], None],
    ) -> None:
        """Run outbound send/resend loop using queue ownership semantics."""
        self._send_to_radio(
            to_radio,
            send_impl=send_impl,
            sleep_fn=sleep_fn,
        )

    def _requeue_capacity_exempt_packet_locked(
        self,
        packet_id: int,
        packet: mesh_pb2.ToRadio,
    ) -> None:
        """Requeue a failed local packet ahead of newer capacity-exempt work."""
        queue = self._get_queue()
        queued_items = tuple(queue.items())
        queue.clear()
        inserted = False
        for queued_id, queued_packet in queued_items:
            if not inserted and isinstance(queued_packet, mesh_pb2.ToRadio):
                if not self._queued_packet_uses_capacity_locked(
                    queued_id,
                    queued_packet,
                ):
                    queue[packet_id] = packet
                    inserted = True
            queue[queued_id] = queued_packet
        if not inserted:
            queue[packet_id] = packet

    def _reconcile_resent_queue(
        self,
        *,
        resent_queue: OrderedDict[int, mesh_pb2.ToRadio | bool],
        sent_packet_ids: set[int],
    ) -> None:
        """Reconcile resent packets against ACK-under-us and requeue semantics."""
        missing = object()
        for packet_id, packet in resent_queue.items():
            with self._lock:
                uses_capacity = (
                    self._queued_packet_uses_capacity_locked(packet_id, packet)
                    if isinstance(packet, mesh_pb2.ToRadio)
                    else None
                )
                queued_value: mesh_pb2.ToRadio | bool | object = self._get_queue().pop(
                    packet_id,
                    missing,
                )
                acked = queued_value is False

            if acked:
                logger.debug("packet %08x got acked under us", packet_id)
                with self._lock:
                    self._forget_capacity_state_locked(packet_id)
                continue

            if queued_value is missing and packet_id in sent_packet_ids:
                with self._lock:
                    self._prune_awaiting_queue_status_ids_locked(time.monotonic())
                    self._claimed_queue_status_by_id.pop(packet_id, None)
                    should_track_reply = (
                        self._queue_status_seen or uses_capacity is False
                    )
                    if should_track_reply:
                        self._awaiting_queue_status_ids[packet_id] = time.monotonic()
                    else:
                        self._forget_capacity_state_locked(packet_id)
                if should_track_reply:
                    logger.debug(
                        "packet %08x sent and awaiting queue-status correlation",
                        packet_id,
                    )
                else:
                    logger.debug(
                        "packet %08x sent without queue-status correlation",
                        packet_id,
                    )
                continue

            packet_to_requeue: mesh_pb2.ToRadio | bool | None = None
            if isinstance(queued_value, mesh_pb2.ToRadio):
                packet_to_requeue = queued_value
            elif isinstance(packet, mesh_pb2.ToRadio):
                packet_to_requeue = packet
            elif queued_value is not missing and isinstance(queued_value, bool):
                packet_to_requeue = queued_value

            if packet_to_requeue is None:
                with self._lock:
                    self._forget_capacity_state_locked(packet_id)
                continue

            with self._lock:
                claimed_status = self._claimed_queue_status_by_id.pop(
                    packet_id,
                    None,
                )
                if claimed_status is not None and packet_id not in sent_packet_ids:
                    current_status = self._get_queue_status()
                    if current_status is claimed_status:
                        current_status.free = min(
                            current_status.maxlen,
                            current_status.free + 1,
                        )

                if isinstance(packet_to_requeue, mesh_pb2.ToRadio):
                    if uses_capacity is None:
                        uses_capacity = self._queued_packet_uses_capacity_locked(
                            packet_id,
                            packet_to_requeue,
                        )
                    if not uses_capacity:
                        self._requeue_capacity_exempt_packet_locked(
                            packet_id,
                            packet_to_requeue,
                        )
                    else:
                        self._get_queue()[packet_id] = packet_to_requeue
                else:
                    self._get_queue()[packet_id] = packet_to_requeue
                    self._forget_capacity_state_locked(packet_id)

    def reconcile_resent_queue(
        self,
        *,
        resent_queue: OrderedDict[int, mesh_pb2.ToRadio | bool],
        sent_packet_ids: set[int],
    ) -> None:
        """Reconcile resent packets against ACK-under-us and requeue semantics."""
        self._reconcile_resent_queue(
            resent_queue=resent_queue,
            sent_packet_ids=sent_packet_ids,
        )

    def _record_queue_status(self, queue_status: mesh_pb2.QueueStatus) -> None:
        """Persist latest queue status update."""
        with self._lock:
            self._queue_status_seen = True
            self._set_queue_status(queue_status)
        logger.debug(
            "TX QUEUE free %s of %s, res = %s, id = %08x ",
            queue_status.free,
            queue_status.maxlen,
            queue_status.res,
            queue_status.mesh_packet_id,
        )

    def record_queue_status(self, queue_status: mesh_pb2.QueueStatus) -> None:
        """Persist latest queue status update."""
        self._record_queue_status(queue_status)

    def _correlate_queue_status_reply(self, queue_status: mesh_pb2.QueueStatus) -> None:
        """Correlate queue status mesh_packet_id replies to pending entries."""
        packet_id = queue_status.mesh_packet_id
        debug_enabled = logger.isEnabledFor(logging.DEBUG)
        with self._lock:
            self._prune_awaiting_queue_status_ids_locked(time.monotonic())
            queue = self._get_queue()
            queue_snapshot = tuple(queue.keys()) if debug_enabled else ()
            just_queued = queue.pop(packet_id, None)
            was_awaiting = packet_id in self._awaiting_queue_status_ids
            if packet_id != 0:
                self._awaiting_queue_status_ids.pop(packet_id, None)
                if just_queued is not None or was_awaiting:
                    self._forget_capacity_state_locked(packet_id)
        if debug_enabled:
            logger.debug(
                "queue: %s",
                " ".join(f"{key:08x}" for key in queue_snapshot),
            )
        if just_queued is None and packet_id != 0:
            if was_awaiting:
                logger.debug(
                    "Correlated queue-status reply for packet awaiting correlation %08x",
                    packet_id,
                )
                return
            with self._lock:
                self._get_queue()[packet_id] = False
            logger.debug(
                "Reply for unexpected packet ID %08x",
                packet_id,
            )

    def correlate_queue_status_reply(self, queue_status: mesh_pb2.QueueStatus) -> None:
        """Correlate queue status mesh_packet_id replies to pending entries."""
        self._correlate_queue_status_reply(queue_status)

    def _handle_queue_status_from_radio(
        self, queue_status: mesh_pb2.QueueStatus
    ) -> None:
        """Apply queue status updates and queue reply correlation."""
        self._record_queue_status(queue_status)
        if queue_status.res:
            packet_id = queue_status.mesh_packet_id
            if packet_id != 0:
                with self._lock:
                    was_awaiting = (
                        self._awaiting_queue_status_ids.pop(packet_id, None) is not None
                    )
                    if was_awaiting:
                        self._forget_capacity_state_locked(packet_id)
                    elif (
                        packet_id in self._packet_uses_capacity_by_id
                        and packet_id not in self._get_queue()
                    ):
                        # A response can arrive during send_impl(), before
                        # reconciliation registers the packet as awaiting it.
                        self._get_queue()[packet_id] = False
            return
        self._correlate_queue_status_reply(queue_status)

    def handle_queue_status_from_radio(
        self, queue_status: mesh_pb2.QueueStatus
    ) -> None:
        """Apply queue status updates and queue reply correlation."""
        self._handle_queue_status_from_radio(queue_status)

    def _prune_awaiting_queue_status_ids_locked(self, now: float) -> None:
        """Drop stale queue-status correlation IDs. Caller must hold _lock."""
        expired_before = now - AWAITING_QUEUE_STATUS_TTL_SECONDS
        for packet_id, tracked_at in list(self._awaiting_queue_status_ids.items()):
            if tracked_at < expired_before:
                self._awaiting_queue_status_ids.pop(packet_id, None)
                self._forget_capacity_state_locked(packet_id)
