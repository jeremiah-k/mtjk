"""setFixedPosition/removeFixedPosition/setTime command orchestration."""

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from meshtastic.node_runtime.admin_wait import (
    _send_admin_with_ack_scope,
    _wait_for_admin_ack,
)
from meshtastic.node_runtime.ports import _NodePort
from meshtastic.protobuf import admin_pb2, mesh_pb2

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)

# Scaling factor for converting lat/lon degrees to integer representation
DEGREES_TO_INT_SCALE = int(1e7)


class _NodePositionTimeCommandRuntime:
    """Owns setFixedPosition/removeFixedPosition/setTime command orchestration."""

    def __init__(self, node: "Node", port: _NodePort | None = None) -> None:
        """Initialize the runtime with its facade and collaborator port.

        Parameters
        ----------
        node : Node
            The facade whose public state the runtime reads.
        port : _NodePort | None
            Narrow port over the facade's internals; derived from ``node``
            when omitted.
        """
        self._node = node
        self._port = port if port is not None else _NodePort(node)

    def _select_remote_ack_callback(self) -> Callable[[dict[str, Any]], Any] | None:
        """Return callback policy used by remote admin command sends."""
        if self._node is self._node.iface.localNode:
            return None
        return self._node.onAckNak

    def _send_position_time_command(
        self,
        admin_message: admin_pb2.AdminMessage,
    ) -> mesh_pb2.MeshPacket | None:
        """Send position/time admin command and wait for remote ACK/NAK when needed."""
        on_response = self._select_remote_ack_callback()
        request = _send_admin_with_ack_scope(
            self._node,
            admin_message,
            scope_ack=on_response is not None,
            onResponse=on_response,
            wantResponse=True,
        )
        if on_response is not None and request is not None:
            _wait_for_admin_ack(self._node, request)
        return request

    def _set_fixed_position(
        self,
        *,
        lat: int | float | None,
        lon: int | float | None,
        alt: int | float | None,
    ) -> mesh_pb2.MeshPacket | None:
        """Send set_fixed_position admin command with preserved conversion semantics.

        Parameters
        ----------
        lat : int | float | None
            Latitude value. Floats are interpreted as degrees and scaled by
            1e7; ints are treated as pre-scaled ``latitude_i`` values.
            ``None`` omits ``latitude_i`` from the sent message.
            Zero values (``0``/``0.0``) are now treated as valid coordinates.
        lon : int | float | None
            Longitude value. Floats are interpreted as degrees and scaled by
            1e7; ints are treated as pre-scaled ``longitude_i`` values.
            ``None`` omits ``longitude_i`` from the sent message.
            Zero values (``0``/``0.0``) are now treated as valid coordinates.
        alt : int | float | None
            Altitude in meters. Floats are truncated to int before sending.
            ``None`` omits altitude from the sent message.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The sent packet, or ``None`` if the send failed.
        """
        if lat is not None and (
            isinstance(lat, bool) or not isinstance(lat, (int, float))
        ):
            self._port.raise_interface_error(
                f"Invalid latitude type: {type(lat).__name__}. Expected int or float."
            )
        if lon is not None and (
            isinstance(lon, bool) or not isinstance(lon, (int, float))
        ):
            self._port.raise_interface_error(
                f"Invalid longitude type: {type(lon).__name__}. Expected int or float."
            )
        if alt is not None and (
            isinstance(alt, bool) or not isinstance(alt, (int, float))
        ):
            self._port.raise_interface_error(
                f"Invalid altitude type: {type(alt).__name__}. Expected int or float."
            )

        position_message = mesh_pb2.Position()
        if lat is not None:
            if isinstance(lat, float):
                position_message.latitude_i = int(lat * DEGREES_TO_INT_SCALE)
            elif isinstance(lat, int):
                position_message.latitude_i = lat

        if lon is not None:
            if isinstance(lon, float):
                position_message.longitude_i = int(lon * DEGREES_TO_INT_SCALE)
            elif isinstance(lon, int):
                position_message.longitude_i = lon

        if alt is not None:
            if isinstance(alt, float):
                position_message.altitude = int(alt)
            else:
                position_message.altitude = alt

        self._node.ensureSessionKey()
        admin_message = admin_pb2.AdminMessage()
        admin_message.set_fixed_position.CopyFrom(position_message)
        logger.info("Setting fixed position: lat=%s, lon=%s, alt=%s", lat, lon, alt)
        return self._send_position_time_command(admin_message)

    def _remove_fixed_position(self) -> mesh_pb2.MeshPacket | None:
        """Send remove_fixed_position admin command."""
        self._node.ensureSessionKey()
        admin_message = admin_pb2.AdminMessage()
        admin_message.remove_fixed_position = True
        logger.info("Telling node to remove fixed position")
        return self._send_position_time_command(admin_message)

    def _set_time(self, *, time_sec: int = 0) -> mesh_pb2.MeshPacket | None:
        """Send set_time_only admin command with current-time fallback."""
        if isinstance(time_sec, bool) or not isinstance(time_sec, int):
            self._port.raise_interface_error(
                f"Invalid time_sec type: {type(time_sec).__name__}. Expected int."
            )
        self._node.ensureSessionKey()
        if time_sec == 0:
            time_sec = int(time.time())
        admin_message = admin_pb2.AdminMessage()
        admin_message.set_time_only = time_sec
        logger.info("Setting node time to %s", time_sec)
        return self._send_position_time_command(admin_message)
