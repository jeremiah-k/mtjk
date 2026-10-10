"""Send pipeline for transmitting packets to the radio."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol, TypeAlias, cast

from google.protobuf.message import Message

from meshtastic._command_scope import _get_command_scope
from meshtastic._core_constants import BROADCAST_ADDR, BROADCAST_NUM, LOCAL_ADDR
from meshtastic.mesh_interface_runtime.flows import (
    DEFAULT_TELEMETRY_TYPE,
    TelemetryType,
    _on_response_position,
    _on_response_telemetry,
    _on_response_traceroute,
    _on_response_waypoint,
    _request_traceroute,
    delete_waypoint,
    send_position,
    send_telemetry,
    send_traceroute,
    send_waypoint,
)
from meshtastic.mesh_interface_runtime.ports import _SendPipelinePort
from meshtastic.mesh_interface_runtime.queue_send import _QueueSendRuntime
from meshtastic.mesh_interface_runtime.request_wait import (
    LEGACY_UNSCOPED_WAIT_ATTR_BY_PORTNUM,
    WAIT_ATTR_NAK,
    WAIT_ATTR_POSITION,
    WAIT_ATTR_TELEMETRY,
    WAIT_ATTR_TRACEROUTE,
    WAIT_ATTR_WAYPOINT,
    _RequestWaitRuntime,
)
from meshtastic.payload_limits import _validate_firmware_payload_limits
from meshtastic.protobuf import mesh_pb2, portnums_pb2
from meshtastic.traceroute import TraceRouteResult
from meshtastic.util import Acknowledgment, Timeout, stripnl

if TYPE_CHECKING:
    from meshtastic.node import Node

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

    def __init__(self, port: _SendPipelinePort) -> None:
        """Initialize the send pipeline with its interface capability port.

        Parameters
        ----------
        port : _SendPipelinePort
            Narrow access to send-side interface state and compatibility seams.
        """
        self._port = port

    @property
    def _node_db_lock(self) -> threading.RLock:
        """Return the node database lock from the parent interface."""
        return self._port.node_db_lock

    @property
    def _request_wait_runtime(self) -> _RequestWaitRuntime:
        """Return the request wait runtime from the parent interface."""
        return self._port.request_wait_runtime

    def _reserved_response_ids(self) -> set[int]:
        """Snapshot callback, active-wait, and quarantined correlation ids."""
        return self._request_wait_runtime._reserved_response_ids()

    def _try_activate_wait_request(
        self, acknowledgment_attr: str, request_id: int
    ) -> bool:
        """Atomically reserve a fresh request id for a scoped wait."""
        return self._request_wait_runtime._try_activate_wait_request(
            acknowledgment_attr, request_id
        )

    @property
    def _queue_send_runtime(self) -> _QueueSendRuntime:
        """Return the queue send runtime from the parent interface."""
        return self._port.queue_send_runtime

    @property
    def local_node(self) -> "Node":
        """Return the local node from the parent interface."""
        return self._port.local_node

    @property
    def my_info(self) -> mesh_pb2.MyNodeInfo | None:
        """Return the my_info from the parent interface."""
        return self._port.my_info

    @property
    def nodes(self) -> dict[str, dict[str, Any]] | None:
        """Return the nodes dictionary from the parent interface."""
        return self._port.nodes

    @property
    def nodes_by_num(self) -> dict[int, dict[str, Any]] | None:
        """Return the nodes by number dictionary from the parent interface."""
        return self._port.nodes_by_num

    @property
    def config_id(self) -> int | None:
        """Return the config ID from the parent interface."""
        return self._port.config_id

    @property
    def no_proto(self) -> bool:
        """Return the no_proto flag from the parent interface."""
        return self._port.no_proto

    @property
    def _acknowledgment(self) -> Acknowledgment:
        """Return the acknowledgment from the parent interface."""
        return self._port.acknowledgment

    @property
    def _timeout(self) -> Timeout:
        """Return the timeout from the parent interface."""
        return self._port.timeout

    # pylint: disable=too-many-positional-arguments
    def send_text(
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
        return self.send_data(
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
    def send_alert(
        self,
        text: str,
        destinationId: int | str = BROADCAST_ADDR,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        channelIndex: int = 0,
        hopLimit: int | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Send a high-priority alert text to a node."""
        return self.send_data(
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

    def send_mqtt_client_proxy_message(self, topic: str, data: bytes) -> None:
        """Send an MQTT client-proxy message through the radio."""
        prox = mesh_pb2.MqttClientProxyMessage()
        prox.topic = topic
        prox.data = data
        _validate_firmware_payload_limits(prox, context="MQTT client-proxy message")
        toRadio = mesh_pb2.ToRadio()
        toRadio.mqttClientProxyMessage.CopyFrom(prox)
        self._send_to_radio(toRadio)

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    def send_data(
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
        responseMatcher: Callable[[dict[str, Any]], bool] | None = None,
        responseFeedbackMatcher: Callable[[dict[str, Any]], bool] | None = None,
        channelIndex: int = 0,
        hopLimit: int | None = None,
        pkiEncrypted: bool = False,
        publicKey: bytes | None = None,
        priority: mesh_pb2.MeshPacket.Priority.ValueType = mesh_pb2.MeshPacket.Priority.RELIABLE,
        replyId: int | None = None,
        response_wait_attr: str | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Send payload data while optionally pre-registering request-scoped wait bookkeeping."""
        command_scope = _get_command_scope(self._port.facade)
        if (
            command_scope is not None
            and wantAck
            and not wantResponse
            and response_wait_attr is None
        ):
            response_wait_attr = WAIT_ATTR_NAK
        if (
            command_scope is not None
            and response_wait_attr == WAIT_ATTR_NAK
            and not wantResponse
        ):
            # Node's legacy ACK reporter also writes shared acknowledgment
            # latches. Embedded actions own only their correlated request.
            from meshtastic.node import Node  # pylint: disable=import-outside-toplevel

            if (
                onResponse is None
                or getattr(onResponse, "__func__", None) is Node.onAckNak
            ):
                onResponse = command_scope._on_ack
                onResponseAckPermitted = True
                local_num = self.local_node.nodeNum

                def _reject_data(_packet: dict[str, Any]) -> bool:
                    return False

                def _matches_ack_source(packet: dict[str, Any]) -> bool:
                    # _send_packet resolves broadcast, local and database
                    # identifiers before transmitting this packet.
                    return packet.get("from") in {meshPacket.to, local_num}

                responseMatcher = _reject_data
                responseFeedbackMatcher = _matches_ack_source
        serializer = getattr(data, "SerializeToString", None)
        payload: bytes | bytearray | memoryview
        if callable(serializer):
            logger.debug("Serializing protobuf as data: %s", stripnl(data))
            if isinstance(data, Message):
                # Device firmware drops whole ToRadio frames whose nested
                # messages overflow a nanopb buffer, with no feedback on
                # either side; surface the violation client-side instead.
                _validate_firmware_payload_limits(data, context="Outbound payload")
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
            raise self._port.error_type("Data payload too big")

        if portNum == portnums_pb2.PortNum.UNKNOWN_APP:
            raise self._port.error_type("A non-zero port number must be specified")

        meshPacket = mesh_pb2.MeshPacket()
        meshPacket.channel = channelIndex
        meshPacket.decoded.payload = payload
        meshPacket.decoded.portnum = portNum
        meshPacket.decoded.want_response = wantResponse
        # Response correlation keys live handlers by request id alone, so any
        # send that can elicit correlated routing/data feedback must not reuse
        # an id owned by a callback, active wait, or reply quarantine. Zero-id
        # regeneration stays unconditional. Callback-bearing sends then claim the handler id
        # under the response-state lock so a late registration collision is
        # rejected before the packet is sent. Selection snapshots are locked
        # advisory reads, and feedback-only sends (no callback) have no
        # registration-time claim, so a concurrent registration between the
        # final check and transmit can still reuse an id for those sends;
        # callback-bearing sends cannot.
        will_register_handler = onResponse is not None
        expects_correlated_feedback = (
            will_register_handler
            or wantAck
            or wantResponse
            or response_wait_attr is not None
        )
        wait_request_registered = False
        meshPacket.id = self._port.generate_packet_id()
        for _ in range(PACKET_ID_GENERATION_MAX_RETRIES):
            if meshPacket.id != 0:
                if response_wait_attr is not None and onResponse is None:
                    wait_request_registered = self._try_activate_wait_request(
                        response_wait_attr, meshPacket.id
                    )
                    if wait_request_registered:
                        break
                elif not (
                    expects_correlated_feedback
                    and meshPacket.id in self._reserved_response_ids()
                ):
                    break
            meshPacket.id = self._port.generate_packet_id()
        else:
            if meshPacket.id == 0:
                raise self._port.error_type("Failed to generate non-zero packet ID")
            if response_wait_attr is not None and onResponse is None:
                wait_request_registered = self._try_activate_wait_request(
                    response_wait_attr, meshPacket.id
                )
                if not wait_request_registered:
                    raise self._port.error_type(
                        "Failed to generate packet ID not already used by a live "
                        "response handler or quarantined request"
                    )
            elif (
                expects_correlated_feedback
                and meshPacket.id in self._reserved_response_ids()
            ):
                raise self._port.error_type(
                    "Failed to generate packet ID not already used by a live "
                    "response handler or quarantined request"
                )
        try:
            if replyId is not None:
                meshPacket.decoded.reply_id = replyId
            meshPacket.priority = priority
        except (TypeError, ValueError, OverflowError):
            if wait_request_registered and response_wait_attr is not None:
                self._retire_wait_request(response_wait_attr, request_id=meshPacket.id)
            raise

        handler_registered = False
        if onResponse is not None:
            logger.debug("Setting a response handler for requestId %s", meshPacket.id)
            handler_registered = self._add_response_handler(
                meshPacket.id,
                onResponse,
                ackPermitted=onResponseAckPermitted,
                matcher=responseMatcher,
                feedbackMatcher=responseFeedbackMatcher,
                rejectIfRegistered=True,
            )
            if not handler_registered:
                raise self._port.error_type(
                    f"Packet id {meshPacket.id} is already used by a live response handler or quarantined request"
                )
        try:
            if command_scope is not None:
                command_scope._track(
                    meshPacket.id, response_wait_attr, packet=meshPacket
                )
            if response_wait_attr is not None and not wait_request_registered:
                self._clear_wait_error(response_wait_attr, request_id=meshPacket.id)
            return self._port.send_packet(
                meshPacket,
                destinationId,
                want_ack=wantAck,
                hop_limit=hopLimit,
                pki_encrypted=pkiEncrypted,
                public_key=publicKey,
            )
        except Exception:
            if response_wait_attr is not None:
                self._retire_wait_request(
                    response_wait_attr,
                    request_id=meshPacket.id,
                )
            elif handler_registered:
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
            error_factory=self._port.error_type,
        )

    def _retire_wait_request(
        self, acknowledgment_attr: str, request_id: int | None = None
    ) -> None:
        """Retire response handler and wait bookkeeping for a completed wait."""
        self._request_wait_runtime.retire_wait_request(
            acknowledgment_attr,
            request_id=request_id,
        )

    def _has_active_wait_request(
        self, acknowledgment_attr: str, request_id: int
    ) -> bool:
        """Return whether one request is active in the given wait scope."""
        return self._request_wait_runtime.has_active_wait_request(
            acknowledgment_attr, request_id
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

    def on_response_position(self, p: dict[str, Any]) -> None:
        """Process a position response packet and emit a concise human-readable summary."""
        _on_response_position(self._port.facade, p)

    # pylint: disable=too-many-positional-arguments
    def send_position(
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
        return send_position(
            self._port.facade,
            latitude=latitude,
            longitude=longitude,
            altitude=altitude,
            destinationId=destinationId,
            wantAck=wantAck,
            wantResponse=wantResponse,
            channelIndex=channelIndex,
            hopLimit=hopLimit,
        )

    def on_response_trace_route(self, p: dict[str, Any]) -> None:
        """Emit human-readable traceroute results from a RouteDiscovery payload."""
        _on_response_traceroute(self._port.facade, p)

    # pylint: disable=too-many-positional-arguments
    def send_trace_route(
        self, dest: int | str, hopLimit: int, channelIndex: int = 0
    ) -> None:
        """Initiate a traceroute request toward a destination node and wait for responses."""
        return send_traceroute(
            self._port.facade, dest, hopLimit, channelIndex=channelIndex
        )

    def request_trace_route(
        self, dest: int | str, hopLimit: int, channelIndex: int = 0
    ) -> TraceRouteResult:
        """Initiate a traceroute request and return its structured response."""
        return _request_traceroute(
            self._port.facade, dest, hopLimit, channelIndex=channelIndex
        )

    def send_telemetry(
        self,
        destinationId: int | str = BROADCAST_ADDR,
        wantResponse: bool = False,
        channelIndex: int = 0,
        telemetryType: TelemetryType | str = DEFAULT_TELEMETRY_TYPE,
        hopLimit: int | None = None,
    ) -> None:
        """Send a telemetry message to a node or broadcast and optionally wait for a telemetry response."""
        return send_telemetry(
            self._port.facade,
            destinationId=destinationId,
            wantResponse=wantResponse,
            channelIndex=channelIndex,
            telemetryType=telemetryType,
            hopLimit=hopLimit,
        )

    def on_response_telemetry(self, p: dict[str, Any]) -> None:
        """Handle an incoming telemetry response."""
        _on_response_telemetry(self._port.facade, p)

    def on_response_waypoint(self, p: dict[str, Any]) -> None:
        """Handle a waypoint response or routing error contained in a received packet."""
        _on_response_waypoint(self._port.facade, p)

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    def send_waypoint(
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
        return send_waypoint(
            self._port.facade,
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
    def delete_waypoint(
        self,
        waypoint_id: int,
        destinationId: int | str = BROADCAST_ADDR,
        wantAck: bool = True,
        wantResponse: bool = False,
        channelIndex: int = 0,
        hopLimit: int | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Delete a waypoint by sending a Waypoint message with expire=0 to a destination."""
        return delete_waypoint(
            self._port.facade,
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
        matcher: Callable[[dict[str, Any]], bool] | None = None,
        feedbackMatcher: Callable[[dict[str, Any]], bool] | None = None,
        rejectIfRegistered: bool = False,
    ) -> bool:
        """Register a response callback for a specific request identifier."""
        kwargs: dict[str, Any] = {"ack_permitted": ackPermitted}
        if matcher is not None:
            kwargs["matcher"] = matcher
        if feedbackMatcher is not None:
            kwargs["feedback_matcher"] = feedbackMatcher
        if rejectIfRegistered:
            kwargs["reject_if_registered"] = True
        return self._request_wait_runtime.add_response_handler(
            requestId,
            callback,
            **kwargs,
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
            my_node_num = self.my_info.my_node_num if self.my_info is not None else None

        if my_node_num is not None and destinationId != my_node_num:
            self._port.wait_connected()

        toRadio = mesh_pb2.ToRadio()

        nodeNum: int = 0
        if destinationId is None:
            raise self._port.error_type(f"Invalid destinationId: {destinationId}")
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
                raise self._port.error_type("No myInfo found.")
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
                        raise self._port.error_type(
                            _format_missing_node_num_error(destinationId)
                        )
                elif has_nodes:
                    raise self._port.error_type(
                        _format_node_not_found_in_db_error(destinationId)
                    )
                else:
                    raise self._port.error_type(
                        _format_node_db_unavailable_error(destinationId)
                    )
        else:
            # Defensive: should be unreachable given type hints (int | str)
            raise self._port.error_type(
                f"Unexpected destinationId type: {type(destinationId)}"
            )

        meshPacket.to = nodeNum
        meshPacket.want_ack = wantAck

        if hopLimit is not None:
            meshPacket.hop_limit = hopLimit
        else:
            with self._node_db_lock:
                local_node = self.local_node
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
            meshPacket.id = self._port.generate_packet_id()

        toRadio.packet.CopyFrom(meshPacket)
        if self.no_proto:
            logger.warning(
                "Not sending packet because protocol use is disabled by noProto"
            )
        else:
            logger.debug("Sending packet: %s", stripnl(meshPacket))
            self._port.send_to_radio(toRadio)
        return meshPacket

    def wait_for_config(self) -> None:
        """Block until the radio configuration and the local node's configuration are available."""
        success = (
            self._port.wait_for_initial_config()
            and self.local_node.waitForConfig()
            and self.local_node._channel_request_runtime._timeout_for_field(
                "lora", LORA_CONFIG_WAIT_SECONDS
            )
        )
        if not success:
            raise self._port.error_type("Timed out waiting for interface config")

    def _wait_for_ack_nak(self, request_id: int) -> None:
        """Wait for one request-scoped admin ACK/NAK response."""
        try:
            success = self._wait_for_request_ack(
                WAIT_ATTR_NAK,
                request_id,
                timeout_seconds=self._timeout.expireTimeout,
            )
            self._raise_wait_error_if_present(WAIT_ATTR_NAK, request_id=request_id)
            if not success:
                raise self._port.error_type("Timed out waiting for an acknowledgment")
        finally:
            self._retire_wait_request(WAIT_ATTR_NAK, request_id=request_id)

    def wait_for_ack_nak(self) -> None:
        """Wait until an acknowledgement (ACK) or negative acknowledgement (NAK) is received or the wait times out."""
        command_scope = _get_command_scope(self._port.facade)
        if command_scope is not None:
            command_scope._wait_for_acks()
            return
        success = self._timeout.waitForAckNak(self._acknowledgment)
        self._raise_wait_error_if_present(WAIT_ATTR_NAK)
        if not success:
            raise self._port.error_type("Timed out waiting for an acknowledgment")

    def wait_for_trace_route(
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
                raise self._port.error_type("Timed out waiting for traceroute")
        finally:
            self._retire_wait_request(WAIT_ATTR_TRACEROUTE, request_id=request_id)

    def wait_for_telemetry(self, request_id: int | None = None) -> None:
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
                raise self._port.error_type("Timed out waiting for telemetry")
        finally:
            self._retire_wait_request(WAIT_ATTR_TELEMETRY, request_id=request_id)

    def wait_for_position(self, request_id: int | None = None) -> None:
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
                raise self._port.error_type("Timed out waiting for position")
        finally:
            self._retire_wait_request(WAIT_ATTR_POSITION, request_id=request_id)

    def wait_for_waypoint(self, request_id: int | None = None) -> None:
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
                raise self._port.error_type("Timed out waiting for waypoint")
        finally:
            self._retire_wait_request(WAIT_ATTR_WAYPOINT, request_id=request_id)

    def _send_to_radio(self, toRadio: mesh_pb2.ToRadio) -> None:
        """Queue and transmit a ToRadio protobuf to the radio device."""
        if self.no_proto:
            logger.warning(
                "Not sending packet because protocol use is disabled by noProto"
            )
            return

        self._queue_send_runtime._send_to_radio(
            toRadio,
            send_impl=self._send_to_radio_impl,
            sleep_fn=time.sleep,
        )

    def _send_to_radio_impl(self, toRadio: mesh_pb2.ToRadio) -> None:
        """Transport hook that delivers a ToRadio protobuf to the radio device."""
        self._port.send_to_radio_impl(toRadio)

    def _send_disconnect(self) -> None:
        """Notify the radio device that this interface is disconnecting."""
        m = mesh_pb2.ToRadio()
        m.disconnect = True
        self._send_to_radio(m)

    def send_heartbeat(self) -> None:
        """Send a heartbeat message to the radio to indicate the interface is alive."""
        p = mesh_pb2.ToRadio()
        p.heartbeat.CopyFrom(mesh_pb2.Heartbeat())
        self._send_to_radio(p)
