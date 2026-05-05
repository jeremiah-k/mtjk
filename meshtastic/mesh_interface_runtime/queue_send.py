"""Queue send runtime for MeshInterface.

Internal module — not part of stable public API.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from meshtastic.protobuf import mesh_pb2

logger = logging.getLogger(__name__)

QUEUE_WAIT_DELAY_SECONDS: float = 0.5
AWAITING_QUEUE_STATUS_TTL_SECONDS: float = 300.0


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
    ) -> None:
        self._lock = lock
        self._get_queue = get_queue
        self._get_queue_status = get_queue_status
        self._set_queue_status = set_queue_status
        self._queue_wait_delay_seconds = queue_wait_delay_seconds
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

    def _pop_for_send(self) -> tuple[int, mesh_pb2.ToRadio | bool] | None:
        """Pop the next sendable queue entry while honoring queue free-space state."""
        with self._lock:
            queue = self._get_queue()
            if not queue:
                return None
            queue_status = self._get_queue_status()
            if queue_status is not None and queue_status.free <= 0:
                packet_id, packet = next(iter(queue.items()))
                if not isinstance(packet, mesh_pb2.ToRadio):
                    queue.pop(packet_id, None)
                    return packet_id, packet
                return None
            to_resend = queue.popitem(last=False)
            if queue_status is not None and isinstance(
                to_resend[1], mesh_pb2.ToRadio
            ):
                queue_status.free -= 1
            return to_resend

    def pop_for_send(self) -> tuple[int, mesh_pb2.ToRadio | bool] | None:
        """Pop the next sendable queue entry while honoring queue free-space state."""
        return self._pop_for_send()

    def _send_to_radio(
        self,
        to_radio: mesh_pb2.ToRadio,
        *,
        send_impl: Callable[[mesh_pb2.ToRadio], None],
        sleep_fn: Callable[[float], None],
    ) -> None:
        """Run outbound send/resend loop using queue ownership semantics."""
        if not to_radio.HasField("packet"):
            send_impl(to_radio)
            return
        else:
            with self._lock:
                self._get_queue()[to_radio.packet.id] = to_radio

        resent_queue: OrderedDict[int, mesh_pb2.ToRadio | bool] = OrderedDict()
        sent_packet_ids: set[int] = set()
        try:
            while True:
                to_resend = self._pop_for_send()
                if to_resend is None:
                    with self._lock:
                        queue_has_items = bool(self._get_queue())
                    if not queue_has_items:
                        break
                    logger.debug("Waiting for free space in TX Queue")
                    sleep_fn(self._queue_wait_delay_seconds)
                    continue

                packet_id, packet = to_resend
                resent_queue[packet_id] = packet
                if not isinstance(packet, mesh_pb2.ToRadio):
                    continue
                if packet is not to_radio:
                    logger.debug(
                        "Resending packet ID %08x %s", packet_id, packet
                    )
                send_impl(packet)
                sent_packet_ids.add(packet_id)
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

    def _reconcile_resent_queue(
        self,
        *,
        resent_queue: OrderedDict[int, mesh_pb2.ToRadio | bool],
        sent_packet_ids: set[int],
    ) -> None:
        """Reconcile resent packets against ACK-under-us and requeue semantics."""
        missing = object()
        for packet_id, packet in resent_queue.items():
            restore_queue_slot = False
            with self._lock:
                queued_value: mesh_pb2.ToRadio | bool | object = self._get_queue().pop(
                    packet_id,
                    missing,
                )
                acked = queued_value is False
            if acked:
                logger.debug("packet %08x got acked under us", packet_id)
                continue
            if queued_value is missing and packet_id in sent_packet_ids:
                with self._lock:
                    self._prune_awaiting_queue_status_ids_locked(time.monotonic())
                    if self._queue_status_seen:
                        self._awaiting_queue_status_ids[packet_id] = time.monotonic()
                logger.debug(
                    "packet %08x sent and awaiting queue-status correlation",
                    packet_id,
                )
                continue
            packet_to_requeue: mesh_pb2.ToRadio | bool | None = None
            if isinstance(queued_value, mesh_pb2.ToRadio):
                packet_to_requeue = queued_value
            elif isinstance(packet, mesh_pb2.ToRadio):
                packet_to_requeue = packet
                restore_queue_slot = packet_id not in sent_packet_ids
            elif queued_value is not missing and isinstance(queued_value, bool):
                packet_to_requeue = queued_value
            if packet_to_requeue is not None:
                with self._lock:
                    if restore_queue_slot:
                        queue_status = self._get_queue_status()
                        if queue_status is not None:
                            queue_status.free = min(
                                queue_status.maxlen,
                                queue_status.free + 1,
                            )
                    self._get_queue()[packet_id] = packet_to_requeue

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

    def _correlate_queue_status_reply(
        self, queue_status: mesh_pb2.QueueStatus
    ) -> None:
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

    def correlate_queue_status_reply(
        self, queue_status: mesh_pb2.QueueStatus
    ) -> None:
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
                    self._awaiting_queue_status_ids.pop(packet_id, None)
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
