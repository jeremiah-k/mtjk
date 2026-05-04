"""Queue send runtime for MeshInterface.

Internal module — not part of stable public API.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from meshtastic.protobuf import mesh_pb2

logger = logging.getLogger(__name__)

QUEUE_WAIT_DELAY_SECONDS = 0.5


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
        self._awaiting_queue_status_ids: set[int] = set()

    def has_free_space(self) -> bool:
        """Return whether queue status indicates free TX slots."""
        with self._lock:
            queue_status = self._get_queue_status()
            if queue_status is None:
                return True
            return queue_status.free > 0

    def claim(self) -> None:
        """Claim one queue slot when queue status is available."""
        with self._lock:
            queue_status = self._get_queue_status()
            if queue_status is None:
                return
            if queue_status.free <= 0:
                return
            queue_status.free -= 1

    def pop_for_send(self) -> tuple[int, mesh_pb2.ToRadio | bool] | None:
        """Pop the next sendable queue entry while honoring queue free-space state."""
        with self._lock:
            queue = self._get_queue()
            if not queue:
                return None
            queue_status = self._get_queue_status()
            if queue_status is not None and queue_status.free <= 0:
                return None
            to_resend = queue.popitem(last=False)
            if queue_status is not None and isinstance(
                to_resend[1], mesh_pb2.ToRadio
            ):
                queue_status.free -= 1
            return to_resend

    def send_to_radio(
        self,
        to_radio: mesh_pb2.ToRadio,
        *,
        send_impl: Callable[[mesh_pb2.ToRadio], None],
        pop_for_send: Callable[[], tuple[int, mesh_pb2.ToRadio | bool] | None],
        sleep_fn: Callable[[float], None],
    ) -> None:
        """Run outbound send/resend loop using queue ownership semantics."""
        if not to_radio.HasField("packet"):
            send_impl(to_radio)
        else:
            with self._lock:
                self._get_queue()[to_radio.packet.id] = to_radio

        resent_queue: OrderedDict[int, mesh_pb2.ToRadio | bool] = OrderedDict()
        sent_packet_ids: set[int] = set()
        try:
            while True:
                to_resend = pop_for_send()
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
                if packet != to_radio:
                    logger.debug(
                        "Resending packet ID %08x %s", packet_id, packet
                    )
                send_impl(packet)
                sent_packet_ids.add(packet_id)
        finally:
            self.reconcile_resent_queue(
                resent_queue=resent_queue,
                sent_packet_ids=sent_packet_ids,
            )

    def reconcile_resent_queue(
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
                queued_value: (
                    mesh_pb2.ToRadio | bool | object
                ) = self._get_queue().pop(packet_id, missing)
                acked = queued_value is False
            if acked:
                logger.debug("packet %08x got acked under us", packet_id)
                continue
            if queued_value is missing and packet_id in sent_packet_ids:
                with self._lock:
                    self._awaiting_queue_status_ids.add(packet_id)
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

    def record_queue_status(self, queue_status: mesh_pb2.QueueStatus) -> None:
        """Persist latest queue status update."""
        with self._lock:
            self._set_queue_status(queue_status)
        logger.debug(
            "TX QUEUE free %s of %s, res = %s, id = %08x ",
            queue_status.free,
            queue_status.maxlen,
            queue_status.res,
            queue_status.mesh_packet_id,
        )

    def correlate_queue_status_reply(
        self, queue_status: mesh_pb2.QueueStatus
    ) -> None:
        """Correlate queue status mesh_packet_id replies to pending entries."""
        packet_id = queue_status.mesh_packet_id
        debug_enabled = logger.isEnabledFor(logging.DEBUG)
        with self._lock:
            queue = self._get_queue()
            queue_snapshot = tuple(queue.keys()) if debug_enabled else ()
            just_queued = queue.pop(packet_id, None)
            was_awaiting = packet_id in self._awaiting_queue_status_ids
            if packet_id != 0:
                self._awaiting_queue_status_ids.discard(packet_id)
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

    def handle_queue_status_from_radio(
        self, queue_status: mesh_pb2.QueueStatus
    ) -> None:
        """Apply queue status updates and queue reply correlation."""
        self.record_queue_status(queue_status)
        if queue_status.res:
            packet_id = queue_status.mesh_packet_id
            if packet_id != 0:
                with self._lock:
                    self._awaiting_queue_status_ids.discard(packet_id)
            return
        self.correlate_queue_status_reply(queue_status)
