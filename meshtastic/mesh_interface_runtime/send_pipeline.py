"""Send pipeline for transmitting packets to the radio."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol, TypeAlias, cast

from meshtastic import BROADCAST_ADDR, BROADCAST_NUM, LOCAL_ADDR
from meshtastic.mesh_interface_runtime.flows import (
    DEFAULT_TELEMETRY_TYPE,
    TelemetryType,
    _on_response_position,
    _on_response_telemetry,
    _on_response_traceroute,
    _on_response_waypoint,
    deleteWaypoint,
    sendPosition,
    sendTelemetry,
    sendTraceroute,
    sendWaypoint,
)
from meshtastic.mesh_interface_runtime.request_wait import (
    LEGACY_UNSCOPED_WAIT_ATTR_BY_PORTNUM,
    WAIT_ATTR_NAK,
    WAIT_ATTR_POSITION,
    WAIT_ATTR_TELEMETRY,
    WAIT_ATTR_TRACEROUTE,
    WAIT_ATTR_WAYPOINT,
    _RequestWaitRuntime,
)
from meshtastic.protobuf import mesh_pb2, portnums_pb2
from meshtastic.util import Acknowledgment, Timeout, stripnl

if TYPE_CHECKING:
    from meshtastic.mesh_interface import MeshInterface

logger = logging.getLogger(__name__)

PACKET_ID_MASK = 0xFFFFFFFF
PACKET_ID_COUNTER_MASK = 0x3FF
PACKET_ID_RANDOM_MAX = 0x3FFFFF
PACKET_ID_RANDOM_SHIFT_BITS = 10

PACKET_ID_GENERATION_MAX_RETRIES = 10
DEFAULT_HOP_LIMIT = 3

QUEUE_WAIT_DELAY_SECONDS = 0.5
LORA_CONFIG_WAIT_SECONDS = 15.0
"""Timeout for waiting for localConfig.lora after initial config stream."""

HEX_NODE_ID_TAIL_CHARS = frozenset("0123456789abcdefABCDEF")
MISSING_NODE_NUM_ERROR_TEMPLATE = "NodeId {destination_id} has no numeric 'num' in DB"
NODE_NOT_FOUND_IN_DB_ERROR_TEMPLATE = "NodeId {destination_id} not found in DB"
NODE_NOT_FOUND_DB_UNAVAILABLE_ERROR_TEMPLATE = (
    "NodeId {destination_id} not found and node DB is unavailable"
)


class _SerializablePayload(Protocol):
    """Protocol for payloads that can serialize to bytes."""

    def SerializeToString(self) -> bytes:
        """Return serialized payload bytes."""
        ...  # pylint: disable=unnecessary-ellipsis


PayloadData: TypeAlias = bytes | bytearray | memoryview | _SerializablePayload


def _format_missing_node_num_error(destination_id: int | str) -> str:
    """Return a consistent error message for nodes missing numeric IDs."""
    return MISSING_NODE_NUM_ERROR_TEMPLATE.format(destination_id=destination_id)


def _format_node_not_found_in_db_error(destination_id: int | str) -> str:
    """Return a consistent error for node IDs missing from an available node DB."""
    return NODE_NOT_FOUND_IN_DB_ERROR_TEMPLATE.format(destination_id=destination_id)


def _format_node_db_unavailable_error(destination_id: int | str) -> str:
    """Return a consistent error for node IDs when node DB is unavailable."""
    return NODE_NOT_FOUND_DB_UNAVAILABLE_ERROR_TEMPLATE.format(
        destination_id=destination_id
    )


def _extract_hex_node_id_body(destination_id: str) -> str | None:
    """Return a compact 8-hex node-id body when ``destination_id`` matches supported forms."""
    candidate = destination_id
    if destination_id.startswith("!"):
        candidate = destination_id[1:]
    elif destination_id.startswith(("0x", "0X")):
        candidate = destination_id[2:]
    if len(candidate) != 8:
        return None
    if not all(ch in HEX_NODE_ID_TAIL_CHARS for ch in candidate):
        return None
    return candidate


def extract_request_id_from_packet(packet: dict[str, Any]) -> int | None:
    """Return decoded requestId as an int when present and valid."""
    decoded = packet.get("decoded")
    if not isinstance(decoded, dict):
        return None
    raw_request_id = decoded.get("requestId")
    if isinstance(raw_request_id, bool):
        return None
    if isinstance(raw_request_id, int):
        return raw_request_id if raw_request_id > 0 else None
    if isinstance(raw_request_id, str) and raw_request_id.isdigit():
        parsed_request_id = int(raw_request_id)
        return parsed_request_id if parsed_request_id > 0 else None
    return None


def extract_request_id_from_sent_packet(packet: object) -> int | None:
    """Return sent packet id when present and positive."""
    raw_packet_id = getattr(packet, "id", None)
    if isinstance(raw_packet_id, bool) or not isinstance(raw_packet_id, int):
        return None
    return raw_packet_id if raw_packet_id > 0 else None


def _emit_response_summary(message: str) -> None:
    """Emit a short response summary without hiding legacy stdout behavior."""
    logger.info("%s", message)


class SendPipeline:
    """Send pipeline for transmitting packets to the radio.

    This class encapsulates all send-related functionality, including data transmission,
    position, telemetry, waypoint, and traceroute operations.
    """

    def __init__(self, interface: "MeshInterface") -> None:
        """Initialize the send pipeline with a parent MeshInterface.

        Parameters
        ----------
        interface : MeshInterface
            The parent MeshInterface instance providing access to interface state.
        """
        self._interface = interface

    @property
    def _node_db_lock(self) -> threading.RLock:
        """Return the node database lock from the parent interface."""
        return self._interface._node_db_lock

    @property
    def _request_wait_runtime(self) -> _RequestWaitRuntime:
        """Return the request wait runtime from the parent interface."""
        return self._interface._request_wait_runtime

    @property
    def _queue_send_runtime(self) -> Any:
        """Return the queue send runtime from the parent interface."""
        return self._interface._queue_send_runtime

    @property
    def localNode(self) -> Any:
        """Return the local node from the parent interface."""
        return self._interface.localNode

    @property
    def myInfo(self) -> Any:
        """Return the myInfo from the parent interface."""
        return self._interface.myInfo

    @property
    def nodes(self) -> dict[str, dict[str, Any]] | None:
        """Return the nodes dictionary from the parent interface."""
        return self._interface.nodes

    @property
    def nodesByNum(self) -> dict[int, dict[str, Any]] | None:
        """Return the nodes by number dictionary from the parent interface."""
        return self._interface.nodesByNum

    @property
    def configId(self) -> int | None:
        """Return the config ID from the parent interface."""
        return self._interface.configId

    @property
    def noProto(self) -> bool:
        """Return the noProto flag from the parent interface."""
        return self._interface.noProto

    @property
    def _acknowledgment(self) -> Acknowledgment:
        """Return the acknowledgment from the parent interface."""
        return self._interface._acknowledgment

    @property
    def _timeout(self) -> Timeout:
        """Return the timeout from the parent interface."""
        return self._interface._timeout

    # pylint: disable=too-many-positional-arguments
    def sendText(
        self,
        text: str,
        destinationId: int | str = BROADCAST_ADDR,
        wantAck: bool = False,
        wantResponse: bool = False,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        channelIndex: int = 0,
        portNum: portnums_pb2.PortNum.ValueType = portnums_pb2.PortNum.TEXT_MESSAGE_APP,
        replyId: int | None = None,
        hopLimit: int | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Send UTF-8 text to a node (or broadcast) and return the transmitted MeshPacket."""
        return self.sendData(
            text.encode("utf-8"),
            destinationId,
            portNum=portNum,
            wantAck=wantAck,
            wantResponse=wantResponse,
            onResponse=onResponse,
            channelIndex=channelIndex,
            replyId=replyId,
            hopLimit=hopLimit,
        )

    # pylint: disable=too-many-positional-arguments
    def sendAlert(
        self,
        text: str,
        destinationId: int | str = BROADCAST_ADDR,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        channelIndex: int = 0,
        hopLimit: int | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Send a high-priority alert text to a node."""
        return self.sendData(
            text.encode("utf-8"),
            destinationId,
            portNum=portnums_pb2.PortNum.ALERT_APP,
            wantAck=False,
            wantResponse=onResponse is not None,
            onResponse=onResponse,
            channelIndex=channelIndex,
            priority=mesh_pb2.MeshPacket.Priority.ALERT,
            hopLimit=hopLimit,
        )

    def sendMqttClientProxyMessage(self, topic: str, data: bytes) -> None:
        """Send an MQTT client-proxy message through the radio."""
        prox = mesh_pb2.MqttClientProxyMessage()
        prox.topic = topic
        prox.data = data
        toRadio = mesh_pb2.ToRadio()
        toRadio.mqttClientProxyMessage.CopyFrom(prox)
        self._send_to_radio(toRadio)

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    def sendData(
        self,
        data: PayloadData,
        destinationId: int | str = BROADCAST_ADDR,
        portNum: portnums_pb2.PortNum.ValueType = portnums_pb2.PortNum.PRIVATE_APP,
        wantAck: bool = False,
        wantResponse: bool = False,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        onResponseAckPermitted: bool = False,
        channelIndex: int = 0,
        hopLimit: int | None = None,
        pkiEncrypted: bool = False,
        publicKey: bytes | None = None,
        priority: mesh_pb2.MeshPacket.Priority.ValueType = mesh_pb2.MeshPacket.Priority.RELIABLE,
        replyId: int | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Send a payload to a mesh node."""
        legacy_wait_attr = LEGACY_UNSCOPED_WAIT_ATTR_BY_PORTNUM.get(portNum)
        if legacy_wait_attr is not None:
            self._clear_wait_error(
                legacy_wait_attr,
                request_id=None,
                clear_scoped=False,
            )
        return self._send_data_with_wait(
            data,
            destinationId=destinationId,
            portNum=portNum,
            wantAck=wantAck,
            wantResponse=wantResponse,
            onResponse=onResponse,
            onResponseAckPermitted=onResponseAckPermitted,
            channelIndex=channelIndex,
            hopLimit=hopLimit,
            pkiEncrypted=pkiEncrypted,
            publicKey=publicKey,
            priority=priority,
            replyId=replyId,
            response_wait_attr=None,
        )

    # pylint: disable=too-many-arguments
    def _send_data_with_wait(
        self,
        data: PayloadData,
        destinationId: int | str = BROADCAST_ADDR,
        portNum: portnums_pb2.PortNum.ValueType = portnums_pb2.PortNum.PRIVATE_APP,
        *,
        wantAck: bool = False,
        wantResponse: bool = False,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        onResponseAckPermitted: bool = False,
        channelIndex: int = 0,
        hopLimit: int | None = None,
        pkiEncrypted: bool = False,
        publicKey: bytes | None = None,
        priority: mesh_pb2.MeshPacket.Priority.ValueType = mesh_pb2.MeshPacket.Priority.RELIABLE,
        replyId: int | None = None,
        response_wait_attr: str | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Send payload data while optionally pre-registering request-scoped wait bookkeeping."""
        serializer = getattr(data, "SerializeToString", None)
        payload: bytes | bytearray | memoryview
        if callable(serializer):
            logger.debug("Serializing protobuf as data: %s", stripnl(data))
            payload = cast(bytes, serializer())
        else:
            payload = cast(bytes | bytearray | memoryview, data)
        if isinstance(payload, memoryview):
            payload = payload.tobytes()
        elif isinstance(payload, bytearray):
            payload = bytes(payload)

        logger.debug("len(data): %s", len(payload))
        logger.debug(
            "mesh_pb2.Constants.DATA_PAYLOAD_LEN: %s",
            mesh_pb2.Constants.DATA_PAYLOAD_LEN,
        )
        if len(payload) > mesh_pb2.Constants.DATA_PAYLOAD_LEN:
            raise self._interface.MeshInterfaceError("Data payload too big")

        if portNum == portnums_pb2.PortNum.UNKNOWN_APP:
            raise self._interface.MeshInterfaceError(
                "A non-zero port number must be specified"
            )

        meshPacket = mesh_pb2.MeshPacket()
        meshPacket.channel = channelIndex
        meshPacket.decoded.payload = payload
        meshPacket.decoded.portnum = portNum
        meshPacket.decoded.want_response = wantResponse
        meshPacket.id = self._interface._generate_packet_id()
        for _ in range(PACKET_ID_GENERATION_MAX_RETRIES):
            if meshPacket.id != 0:
                break
            meshPacket.id = self._interface._generate_packet_id()
        else:
            raise self._interface.MeshInterfaceError(
                "Failed to generate non-zero packet ID"
            )
        if replyId is not None:
            meshPacket.decoded.reply_id = replyId
        meshPacket.priority = priority

        if response_wait_attr is not None:
            self._clear_wait_error(response_wait_attr, request_id=meshPacket.id)

        if onResponse is not None:
            logger.debug("Setting a response handler for requestId %s", meshPacket.id)
            self._add_response_handler(
                meshPacket.id, onResponse, ackPermitted=onResponseAckPermitted
            )
        try:
            return self._interface._send_packet(
                meshPacket,
                destinationId,
                wantAck=wantAck,
                hopLimit=hopLimit,
                pkiEncrypted=pkiEncrypted,
                publicKey=publicKey,
            )
        except Exception:
            if response_wait_attr is not None:
                self._retire_wait_request(
                    response_wait_attr,
                    request_id=meshPacket.id,
                )
            elif onResponse is not None:
                self._request_wait_runtime.drop_response_handler(meshPacket.id)
            raise

    def _extract_request_id_from_packet(self, packet: dict[str, Any]) -> int | None:
        """Return decoded requestId as an int when present and valid."""
        return extract_request_id_from_packet(packet)

    def _extract_request_id_from_sent_packet(self, packet: object) -> int | None:
        """Return sent packet id when present and positive."""
        return extract_request_id_from_sent_packet(packet)

    def _clear_wait_error(
        self,
        acknowledgment_attr: str,
        request_id: int | None = None,
        *,
        clear_scoped: bool = True,
    ) -> None:
        """Clear wait error state for an attribute and optional request id."""
        self._request_wait_runtime.clear_wait_error(
            acknowledgment_attr,
            request_id=request_id,
            clear_scoped=clear_scoped,
        )

    def _prune_retired_wait_request_ids_locked(
        self, acknowledgment_attr: str
    ) -> dict[int, float]:
        """Prune expired retired request ids for a wait attribute."""
        return self._request_wait_runtime.prune_retired_wait_request_ids_locked(
            acknowledgment_attr
        )

    def _set_wait_error(
        self,
        acknowledgment_attr: str,
        message: str,
        *,
        request_id: int | None = None,
    ) -> None:
        """Record a wait error and wake the matching waiter."""
        self._request_wait_runtime.set_wait_error(
            acknowledgment_attr,
            message,
            request_id=request_id,
        )

    def _mark_wait_acknowledged(
        self, acknowledgment_attr: str, *, request_id: int | None = None
    ) -> None:
        """Set acknowledgment flag for the matching request scope."""
        self._request_wait_runtime.mark_wait_acknowledged(
            acknowledgment_attr,
            request_id=request_id,
        )

    def _raise_wait_error_if_present(
        self, acknowledgment_attr: str, request_id: int | None = None
    ) -> None:
        """Raise and clear any pending wait error for the given wait scope."""
        self._request_wait_runtime.raise_wait_error_if_present(
            acknowledgment_attr,
            request_id=request_id,
            error_factory=self._interface.MeshInterfaceError,
        )

    def _retire_wait_request(
        self, acknowledgment_attr: str, request_id: int | None = None
    ) -> None:
        """Retire response handler and wait bookkeeping for a completed wait."""
        self._request_wait_runtime.retire_wait_request(
            acknowledgment_attr,
            request_id=request_id,
        )

    def _wait_for_request_ack(
        self,
        acknowledgment_attr: str,
        request_id: int,
        *,
        timeout_seconds: float,
    ) -> bool:
        """Wait for a request-scoped acknowledgment flag."""
        return self._request_wait_runtime.wait_for_request_ack(
            acknowledgment_attr,
            request_id,
            timeout_seconds=timeout_seconds,
        )

    def _record_routing_wait_error(
        self,
        *,
        acknowledgment_attr: str,
        routing_error_reason: str | None,
        request_id: int | None = None,
    ) -> None:
        """Record non-success routing responses into shared wait state."""
        self._request_wait_runtime.record_routing_wait_error(
            acknowledgment_attr=acknowledgment_attr,
            routing_error_reason=routing_error_reason,
            request_id=request_id,
        )

    def onResponsePosition(self, p: dict[str, Any]) -> None:
        """Process a position response packet and emit a concise human-readable summary."""
        _on_response_position(self._interface, p)

    # pylint: disable=too-many-positional-arguments
    def sendPosition(
        self,
        latitude: float = 0.0,
        longitude: float = 0.0,
        altitude: int = 0,
        destinationId: int | str = BROADCAST_ADDR,
        wantAck: bool = False,
        wantResponse: bool = False,
        channelIndex: int = 0,
        hopLimit: int | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Send the device's position to a specific node or to broadcast."""
        return sendPosition(
            self._interface,
            latitude=latitude,
            longitude=longitude,
            altitude=altitude,
            destinationId=destinationId,
            wantAck=wantAck,
            wantResponse=wantResponse,
            channelIndex=channelIndex,
            hopLimit=hopLimit,
        )

    def onResponseTraceRoute(self, p: dict[str, Any]) -> None:
        """Emit human-readable traceroute results from a RouteDiscovery payload."""
        _on_response_traceroute(self._interface, p)

    # pylint: disable=too-many-positional-arguments
    def sendTraceRoute(
        self, dest: int | str, hopLimit: int, channelIndex: int = 0
    ) -> None:
        """Initiate a traceroute request toward a destination node and wait for responses."""
        return sendTraceroute(
            self._interface, dest, hopLimit, channelIndex=channelIndex
        )

    def sendTelemetry(
        self,
        destinationId: int | str = BROADCAST_ADDR,
        wantResponse: bool = False,
        channelIndex: int = 0,
        telemetryType: TelemetryType | str = DEFAULT_TELEMETRY_TYPE,
        hopLimit: int | None = None,
    ) -> None:
        """Send a telemetry message to a node or broadcast and optionally wait for a telemetry response."""
        return sendTelemetry(
            self._interface,
            destinationId=destinationId,
            wantResponse=wantResponse,
            channelIndex=channelIndex,
            telemetryType=telemetryType,
            hopLimit=hopLimit,
        )

    def onResponseTelemetry(self, p: dict[str, Any]) -> None:
        """Handle an incoming telemetry response."""
        _on_response_telemetry(self._interface, p)

    def onResponseWaypoint(self, p: dict[str, Any]) -> None:
        """Handle a waypoint response or routing error contained in a received packet."""
        _on_response_waypoint(self._interface, p)

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    def sendWaypoint(
        self,
        name: str,
        description: str,
        icon: int | str,
        expire: int,
        waypoint_id: int | None = None,
        latitude: float = 0.0,
        longitude: float = 0.0,
        destinationId: int | str = BROADCAST_ADDR,
        wantAck: bool = True,
        wantResponse: bool = False,
        channelIndex: int = 0,
        hopLimit: int | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Send a waypoint to a node or broadcast."""
        return sendWaypoint(
            self._interface,
            name=name,
            description=description,
            icon=icon,
            expire=expire,
            waypointId=waypoint_id,
            latitude=latitude,
            longitude=longitude,
            destinationId=destinationId,
            wantAck=wantAck,
            wantResponse=wantResponse,
            channelIndex=channelIndex,
            hopLimit=hopLimit,
        )

    # pylint: disable=too-many-positional-arguments
    def deleteWaypoint(
        self,
        waypoint_id: int,
        destinationId: int | str = BROADCAST_ADDR,
        wantAck: bool = True,
        wantResponse: bool = False,
        channelIndex: int = 0,
        hopLimit: int | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Delete a waypoint by sending a Waypoint message with expire=0 to a destination."""
        return deleteWaypoint(
            self._interface,
            waypointId=waypoint_id,
            destinationId=destinationId,
            wantAck=wantAck,
            wantResponse=wantResponse,
            channelIndex=channelIndex,
            hopLimit=hopLimit,
        )

    def _add_response_handler(
        self,
        requestId: int,
        callback: Callable[[dict[str, Any]], Any],
        ackPermitted: bool = False,
    ) -> None:
        """Register a response callback for a specific request identifier."""
        self._request_wait_runtime.add_response_handler(
            requestId,
            callback,
            ack_permitted=ackPermitted,
        )

    # pylint: disable=too-many-positional-arguments
    def _send_packet(
        self,
        meshPacket: mesh_pb2.MeshPacket,
        destinationId: int | str = BROADCAST_ADDR,
        wantAck: bool = False,
        hopLimit: int | None = None,
        pkiEncrypted: bool | None = False,
        publicKey: bytes | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Send a MeshPacket to a specific node or broadcast."""
        with self._node_db_lock:
            my_node_num = self.myInfo.my_node_num if self.myInfo is not None else None

        if my_node_num is not None and destinationId != my_node_num:
            self._interface._wait_connected()

        toRadio = mesh_pb2.ToRadio()

        nodeNum: int = 0
        if destinationId is None:
            raise self._interface.MeshInterfaceError(
                f"Invalid destinationId: {destinationId}"
            )
        elif isinstance(destinationId, int):
            # Note: bool is a subclass of int in Python, so True/False are
            # handled here as node numbers 1/0 for compatibility.
            nodeNum = destinationId
        elif destinationId == BROADCAST_ADDR:
            nodeNum = BROADCAST_NUM
        elif destinationId == LOCAL_ADDR:
            if my_node_num is not None:
                nodeNum = my_node_num
            else:
                raise self._interface.MeshInterfaceError("No myInfo found.")
        elif isinstance(destinationId, str):
            compact_hex_body = _extract_hex_node_id_body(destinationId)
            if compact_hex_body is not None:
                nodeNum = int(compact_hex_body, 16)
            else:
                with self._node_db_lock:
                    node = self.nodes.get(destinationId) if self.nodes else None
                    has_nodes = self.nodes is not None
                    node_found = node is not None
                    node_num = node.get("num") if isinstance(node, dict) else None
                if node_found:
                    if isinstance(node_num, int):
                        nodeNum = node_num
                    else:
                        raise self._interface.MeshInterfaceError(
                            _format_missing_node_num_error(destinationId)
                        )
                elif has_nodes:
                    raise self._interface.MeshInterfaceError(
                        _format_node_not_found_in_db_error(destinationId)
                    )
                else:
                    raise self._interface.MeshInterfaceError(
                        _format_node_db_unavailable_error(destinationId)
                    )
        else:
            # Defensive: should be unreachable given type hints (int | str)
            raise self._interface.MeshInterfaceError(
                f"Unexpected destinationId type: {type(destinationId)}"
            )

        meshPacket.to = nodeNum
        meshPacket.want_ack = wantAck

        if hopLimit is not None:
            meshPacket.hop_limit = hopLimit
        else:
            with self._node_db_lock:
                local_node = self.localNode
                if local_node is None or local_node.localConfig is None:
                    default_hop_limit = DEFAULT_HOP_LIMIT  # Sensible default
                else:
                    default_hop_limit = local_node.localConfig.lora.hop_limit
            meshPacket.hop_limit = default_hop_limit

        if pkiEncrypted:
            meshPacket.pki_encrypted = True

        if publicKey is not None:
            meshPacket.public_key = publicKey

        if meshPacket.id == 0:
            meshPacket.id = self._interface._generate_packet_id()

        toRadio.packet.CopyFrom(meshPacket)
        if self.noProto:
            logger.warning(
                "Not sending packet because protocol use is disabled by noProto"
            )
        else:
            logger.debug("Sending packet: %s", stripnl(meshPacket))
            self._interface._send_to_radio(toRadio)
        return meshPacket

    # pylint: disable=too-many-positional-arguments
    def _sendPacket(
        self,
        meshPacket: mesh_pb2.MeshPacket,
        destinationId: int | str = BROADCAST_ADDR,
        wantAck: bool = False,
        hopLimit: int | None = None,
        pkiEncrypted: bool | None = False,
        publicKey: bytes | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Backward-compatible alias for `_send_packet`."""
        # COMPAT_STABLE_SHIM
        return self._send_packet(
            meshPacket=meshPacket,
            destinationId=destinationId,
            wantAck=wantAck,
            hopLimit=hopLimit,
            pkiEncrypted=pkiEncrypted,
            publicKey=publicKey,
        )

    def waitForConfig(self) -> None:
        """Block until the radio configuration and the local node's configuration are available."""
        success = (
            self._timeout.waitForSet(self._interface, attrs=("myInfo", "nodes"))
            and self.localNode.waitForConfig()
            and self.localNode._channel_request_runtime._timeout_for_field(
                "lora", LORA_CONFIG_WAIT_SECONDS
            )
        )
        if not success:
            raise self._interface.MeshInterfaceError(
                "Timed out waiting for interface config"
            )

    def waitForAckNak(self) -> None:
        """Wait until an acknowledgement (ACK) or negative acknowledgement (NAK) is received or the wait times out."""
        success = self._timeout.waitForAckNak(self._acknowledgment)
        self._raise_wait_error_if_present(WAIT_ATTR_NAK)
        if not success:
            raise self._interface.MeshInterfaceError(
                "Timed out waiting for an acknowledgment"
            )

    def waitForTraceRoute(
        self, waitFactor: float, request_id: int | None = None
    ) -> None:
        """Wait for trace route completion using the configured timeout."""
        try:
            if request_id is None:
                success = self._timeout.waitForTraceRoute(
                    waitFactor, self._acknowledgment
                )
            else:
                success = self._wait_for_request_ack(
                    WAIT_ATTR_TRACEROUTE,
                    request_id,
                    timeout_seconds=self._timeout.expireTimeout * waitFactor,
                )
            self._raise_wait_error_if_present(
                WAIT_ATTR_TRACEROUTE, request_id=request_id
            )
            if not success:
                raise self._interface.MeshInterfaceError(
                    "Timed out waiting for traceroute"
                )
        finally:
            self._retire_wait_request(WAIT_ATTR_TRACEROUTE, request_id=request_id)

    def waitForTelemetry(self, request_id: int | None = None) -> None:
        """Wait for a telemetry response or until the configured timeout elapses."""
        try:
            if request_id is None:
                success = self._timeout.waitForTelemetry(self._acknowledgment)
            else:
                success = self._wait_for_request_ack(
                    WAIT_ATTR_TELEMETRY,
                    request_id,
                    timeout_seconds=self._timeout.expireTimeout,
                )
            self._raise_wait_error_if_present(
                WAIT_ATTR_TELEMETRY, request_id=request_id
            )
            if not success:
                raise self._interface.MeshInterfaceError(
                    "Timed out waiting for telemetry"
                )
        finally:
            self._retire_wait_request(WAIT_ATTR_TELEMETRY, request_id=request_id)

    def waitForPosition(self, request_id: int | None = None) -> None:
        """Block until a position acknowledgment is received."""
        try:
            if request_id is None:
                success = self._timeout.waitForPosition(self._acknowledgment)
            else:
                success = self._wait_for_request_ack(
                    WAIT_ATTR_POSITION,
                    request_id,
                    timeout_seconds=self._timeout.expireTimeout,
                )
            self._raise_wait_error_if_present(WAIT_ATTR_POSITION, request_id=request_id)
            if not success:
                raise self._interface.MeshInterfaceError(
                    "Timed out waiting for position"
                )
        finally:
            self._retire_wait_request(WAIT_ATTR_POSITION, request_id=request_id)

    def waitForWaypoint(self, request_id: int | None = None) -> None:
        """Block until a waypoint acknowledgment is received."""
        try:
            if request_id is None:
                success = self._timeout.waitForWaypoint(self._acknowledgment)
            else:
                success = self._wait_for_request_ack(
                    WAIT_ATTR_WAYPOINT,
                    request_id,
                    timeout_seconds=self._timeout.expireTimeout,
                )
            self._raise_wait_error_if_present(WAIT_ATTR_WAYPOINT, request_id=request_id)
            if not success:
                raise self._interface.MeshInterfaceError(
                    "Timed out waiting for waypoint"
                )
        finally:
            self._retire_wait_request(WAIT_ATTR_WAYPOINT, request_id=request_id)

    def _send_to_radio(self, toRadio: mesh_pb2.ToRadio) -> None:
        """Queue and transmit a ToRadio protobuf to the radio device."""
        if self.noProto:
            logger.warning(
                "Not sending packet because protocol use is disabled by noProto"
            )
            return

        self._queue_send_runtime._send_to_radio(
            toRadio,
            send_impl=self._send_to_radio_impl,
            pop_for_send=self._interface._queue_pop_for_send,
            sleep_fn=time.sleep,
        )

    def _send_to_radio_impl(self, toRadio: mesh_pb2.ToRadio) -> None:
        """Transport hook that delivers a ToRadio protobuf to the radio device."""
        self._interface._send_to_radio_impl(toRadio)

    def _send_disconnect(self) -> None:
        """Notify the radio device that this interface is disconnecting."""
        m = mesh_pb2.ToRadio()
        m.disconnect = True
        self._send_to_radio(m)

    def sendHeartbeat(self) -> None:
        """Send a heartbeat message to the radio to indicate the interface is alive."""
        p = mesh_pb2.ToRadio()
        p.heartbeat.CopyFrom(mesh_pb2.Heartbeat())
        self._send_to_radio(p)
