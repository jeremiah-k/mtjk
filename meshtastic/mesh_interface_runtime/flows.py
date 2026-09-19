"""Send flow methods for telemetry, position, traceroute, and waypoint operations."""

from __future__ import annotations

import logging
import secrets
import warnings
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, TypeAlias

import google.protobuf.json_format
from google.protobuf import message as protobuf_message

from meshtastic._core_constants import BROADCAST_ADDR
from meshtastic.mesh_interface_runtime.request_wait import (
    RESPONSE_WAIT_REQID_ERROR,
    WAIT_ATTR_POSITION,
    WAIT_ATTR_TELEMETRY,
    WAIT_ATTR_TRACEROUTE,
    WAIT_ATTR_WAYPOINT,
)
from meshtastic.protobuf import mesh_pb2, portnums_pb2, telemetry_pb2
from meshtastic.traceroute import TraceRouteHop, TraceRouteResult

if TYPE_CHECKING:
    from meshtastic.mesh_interface import MeshInterface

logger = logging.getLogger(__name__)

TelemetryType: TypeAlias = Literal[
    "environment_metrics",
    "air_quality_metrics",
    "power_metrics",
    "local_stats",
    "device_metrics",
]
DEFAULT_TELEMETRY_TYPE: TelemetryType = "device_metrics"
VALID_TELEMETRY_TYPES: tuple[TelemetryType, ...] = (
    "environment_metrics",
    "air_quality_metrics",
    "power_metrics",
    "local_stats",
    "device_metrics",
)
VALID_TELEMETRY_TYPE_SET: frozenset[str] = frozenset(VALID_TELEMETRY_TYPES)
UNKNOWN_SNR_QUARTER_DB = -128


def _emit_response_summary(message: str) -> None:
    """Emit a short response summary without hiding legacy stdout behavior."""
    logger.info("%s", message)


def _node_label(interface: "MeshInterface", node_num: int) -> str:
    """Return a human-readable identifier for a numeric node."""
    return interface._node_num_to_id(node_num, False) or f"{node_num:08x}"


def _on_response_position(interface: "MeshInterface", p: dict[str, Any]) -> None:
    """Process a position response packet and emit a concise human-readable summary."""
    request_id = interface._extract_request_id_from_packet(p)
    if p["decoded"]["portnum"] == portnums_pb2.PortNum.Name(
        portnums_pb2.PortNum.POSITION_APP
    ):
        position = mesh_pb2.Position()
        try:
            position.ParseFromString(p["decoded"]["payload"])
        except (KeyError, TypeError, protobuf_message.DecodeError) as exc:
            interface._set_wait_error(
                WAIT_ATTR_POSITION,
                f"Failed to parse position response payload: {exc}",
                request_id=request_id,
            )
            return

        ret = "Position received: "
        if position.latitude_i != 0 and position.longitude_i != 0:
            ret += f"({position.latitude_i * 10**-7}, {position.longitude_i * 10**-7})"
        else:
            ret += "(unknown)"
        if position.altitude != 0:
            ret += f" {position.altitude}m"

        if position.precision_bits not in (0, 32):
            ret += f" precision:{position.precision_bits}"
        elif position.precision_bits == 32:
            ret += " full precision"
        elif position.precision_bits == 0:
            ret += " position disabled"

        _emit_response_summary(ret)
        interface._mark_wait_acknowledged(
            WAIT_ATTR_POSITION,
            request_id=request_id,
        )

    elif p["decoded"]["portnum"] == portnums_pb2.PortNum.Name(
        portnums_pb2.PortNum.ROUTING_APP
    ):
        interface._record_routing_wait_error(
            acknowledgment_attr=WAIT_ATTR_POSITION,
            routing_error_reason=p["decoded"].get("routing", {}).get("errorReason"),
            request_id=request_id,
        )


# pylint: disable=too-many-positional-arguments
def send_position(
    interface: "MeshInterface",
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
    p = mesh_pb2.Position()
    if latitude != 0.0:
        p.latitude_i = int(latitude * 1e7)  # was / 1e-7
        logger.debug("p.latitude_i:%s", p.latitude_i)

    if longitude != 0.0:
        p.longitude_i = int(longitude * 1e7)  # was / 1e-7
        logger.debug("p.longitude_i:%s", p.longitude_i)

    if altitude != 0:
        p.altitude = int(altitude)
        logger.debug("p.altitude:%s", p.altitude)

    onResponse: Callable[[dict[str, Any]], None] | None
    response_wait_attr: str | None
    if wantResponse:

        def _on_response(packet: dict[str, Any]) -> None:
            _on_response_position(interface, packet)

        onResponse = _on_response
        response_wait_attr = WAIT_ATTR_POSITION
    else:
        onResponse = None
        response_wait_attr = None

    d = interface._send_data_with_wait(
        p,
        destinationId,
        portNum=portnums_pb2.PortNum.POSITION_APP,
        wantAck=wantAck,
        wantResponse=wantResponse,
        onResponse=onResponse,
        channelIndex=channelIndex,
        hopLimit=hopLimit,
        response_wait_attr=response_wait_attr,
    )
    if wantResponse:
        request_id = interface._extract_request_id_from_sent_packet(d)
        if request_id is None:
            raise interface.MeshInterfaceError(RESPONSE_WAIT_REQID_ERROR)
        interface.waitForPosition(request_id=request_id)
    return d


def _snr_db(snr_value: int | None) -> float | None:
    """Convert firmware quarter-dB SNR values to dB when known."""
    if snr_value is None or snr_value == UNKNOWN_SNR_QUARTER_DB:
        return None
    return snr_value / 4


def _build_trace_route_hops(
    interface: "MeshInterface",
    start_node: int,
    intermediate_nodes: list[int],
    end_node: int,
    snr_values: list[int],
) -> tuple[TraceRouteHop, ...]:
    """Build a full route while preserving incomplete firmware SNR data."""
    snr_values_valid = len(snr_values) == len(intermediate_nodes) + 1
    hops = [
        TraceRouteHop(
            node_num=start_node,
            node_id=_node_label(interface, start_node),
        )
    ]
    for index, node_num in enumerate((*intermediate_nodes, end_node)):
        snr_db = _snr_db(snr_values[index]) if snr_values_valid else None
        hops.append(
            TraceRouteHop(
                node_num=node_num,
                node_id=_node_label(interface, node_num),
                snr_db=snr_db,
            )
        )
    return tuple(hops)


def _format_trace_route(hops: tuple[TraceRouteHop, ...]) -> str:
    """Format a structured route using the historical CLI representation."""
    route_text = hops[0].node_id
    for hop in hops[1:]:
        snr_text = "?" if hop.snr_db is None else str(hop.snr_db)
        route_text = f"{route_text} --> {hop.node_id} ({snr_text}dB)"
    return route_text


def _on_response_traceroute(
    interface: "MeshInterface",
    p: dict[str, Any],
    *,
    emit_summary: bool = True,
    on_result: Callable[[TraceRouteResult], None] | None = None,
) -> TraceRouteResult | None:
    """Parse a traceroute response and publish its result before waking waiters."""
    decoded = p["decoded"]
    request_id = interface._extract_request_id_from_packet(p)
    if decoded.get("portnum") == portnums_pb2.PortNum.Name(
        portnums_pb2.PortNum.ROUTING_APP
    ):
        error_reason = decoded.get("routing", {}).get("errorReason")
        if error_reason is not None and error_reason != "NONE":
            interface._record_routing_wait_error(
                acknowledgment_attr=WAIT_ATTR_TRACEROUTE,
                routing_error_reason=error_reason,
                request_id=request_id,
            )
            return None
        # A successful routing ack proves delivery but carries no route
        # payload: parsing its Routing body as RouteDiscovery fails the wire
        # type check. Firmware acks reliable traceroute requests before the
        # actual response arrives, so re-arm the one-shot response handler and
        # leave the wait pending for the real RouteDiscovery response.
        if request_id is not None:
            interface._add_response_handler(
                request_id,
                lambda packet: _on_response_traceroute(
                    interface,
                    packet,
                    emit_summary=emit_summary,
                    on_result=on_result,
                ),
            )
        return None

    route_discovery = mesh_pb2.RouteDiscovery()
    try:
        route_discovery.ParseFromString(decoded["payload"])
        source_node = int(p["to"])
        destination_node = int(p["from"])
        route_towards = _build_trace_route_hops(
            interface,
            source_node,
            list(route_discovery.route),
            destination_node,
            list(route_discovery.snr_towards),
        )
        route_back = (
            _build_trace_route_hops(
                interface,
                destination_node,
                list(route_discovery.route_back),
                source_node,
                list(route_discovery.snr_back),
            )
            if "hopStart" in p
            else None
        )
    except (
        protobuf_message.DecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        interface._set_wait_error(
            WAIT_ATTR_TRACEROUTE,
            f"Failed to parse traceroute response payload: {exc}",
            request_id=request_id,
        )
        return None

    result = TraceRouteResult(
        route_towards=route_towards,
        route_back=route_back,
        request_id=request_id,
    )
    if emit_summary:
        _emit_response_summary("Route traced towards destination:")
        _emit_response_summary(_format_trace_route(result.route_towards))
        if result.route_back is not None:
            _emit_response_summary("Route traced back to us:")
            _emit_response_summary(_format_trace_route(result.route_back))

    if on_result is not None:
        on_result(result)
    interface._mark_wait_acknowledged(
        WAIT_ATTR_TRACEROUTE,
        request_id=request_id,
    )
    return result


def _send_traceroute(
    interface: "MeshInterface",
    dest: int | str,
    hopLimit: int,
    channelIndex: int,
    *,
    emit_summary: bool,
) -> TraceRouteResult | None:
    """Send one traceroute request and return its structured response when available."""
    result: TraceRouteResult | None = None

    def _capture_response(trace_result: TraceRouteResult) -> None:
        nonlocal result
        # Response handlers are normally one-shot; retain the first valid route
        # if a duplicate packet reaches the callback before handler retirement.
        if result is None:
            result = trace_result

    def _handle_response(packet: dict[str, Any]) -> None:
        _on_response_traceroute(
            interface,
            packet,
            emit_summary=emit_summary,
            on_result=_capture_response,
        )

    packet = interface._send_data_with_wait(
        mesh_pb2.RouteDiscovery(),
        destinationId=dest,
        portNum=portnums_pb2.PortNum.TRACEROUTE_APP,
        wantResponse=True,
        onResponse=_handle_response,
        channelIndex=channelIndex,
        hopLimit=hopLimit,
        response_wait_attr=WAIT_ATTR_TRACEROUTE,
    )
    with interface._node_db_lock:
        node_count = len(interface.nodes) if interface.nodes else 0
    nodes_based_factor = (node_count - 1) if node_count else (hopLimit + 1)
    wait_factor = max(1, min(nodes_based_factor, hopLimit + 1))
    request_id = interface._extract_request_id_from_sent_packet(packet)
    if request_id is None:
        raise interface.MeshInterfaceError(RESPONSE_WAIT_REQID_ERROR)
    interface.waitForTraceRoute(wait_factor, request_id=request_id)
    return result


def send_traceroute(
    interface: "MeshInterface",
    dest: int | str,
    hopLimit: int,
    channelIndex: int = 0,
) -> None:
    """Initiate a traceroute and retain historical human-readable output."""
    _send_traceroute(
        interface,
        dest,
        hopLimit,
        channelIndex,
        emit_summary=True,
    )


def _request_traceroute(
    interface: "MeshInterface",
    dest: int | str,
    hopLimit: int,
    channelIndex: int = 0,
) -> TraceRouteResult:
    """Initiate a traceroute and return its structured result."""
    result = _send_traceroute(
        interface,
        dest,
        hopLimit,
        channelIndex,
        emit_summary=False,
    )
    if result is None:
        raise interface.MeshInterfaceError(
            "Traceroute completed without a route result"
        )
    return result


def _on_response_telemetry(interface: "MeshInterface", p: dict[str, Any]) -> None:
    """Handle an incoming telemetry response."""
    request_id = interface._extract_request_id_from_packet(p)
    if p["decoded"]["portnum"] == portnums_pb2.PortNum.Name(
        portnums_pb2.PortNum.TELEMETRY_APP
    ):
        telemetry = telemetry_pb2.Telemetry()
        try:
            telemetry.ParseFromString(p["decoded"]["payload"])
        except (KeyError, TypeError, protobuf_message.DecodeError) as exc:
            interface._set_wait_error(
                WAIT_ATTR_TELEMETRY,
                f"Failed to parse telemetry response payload: {exc}",
                request_id=request_id,
            )
            return
        try:
            _emit_response_summary("Telemetry received:")
            if telemetry.HasField("device_metrics"):
                device_metrics_fields = {
                    field.name for field, _ in telemetry.device_metrics.ListFields()
                }
                if "battery_level" in device_metrics_fields:
                    _emit_response_summary(
                        f"Battery level: {telemetry.device_metrics.battery_level:.2f}%"
                    )
                if "voltage" in device_metrics_fields:
                    _emit_response_summary(
                        f"Voltage: {telemetry.device_metrics.voltage:.2f} V"
                    )
                if "channel_utilization" in device_metrics_fields:
                    _emit_response_summary(
                        "Total channel utilization: "
                        f"{telemetry.device_metrics.channel_utilization:.2f}%"
                    )
                if "air_util_tx" in device_metrics_fields:
                    _emit_response_summary(
                        f"Transmit air utilization: {telemetry.device_metrics.air_util_tx:.2f}%"
                    )
                if "uptime_seconds" in device_metrics_fields:
                    _emit_response_summary(
                        f"Uptime: {telemetry.device_metrics.uptime_seconds} s"
                    )
            else:
                telemetry_dict = google.protobuf.json_format.MessageToDict(telemetry)
                for key, value in telemetry_dict.items():
                    if key != "time":
                        if isinstance(value, dict):
                            _emit_response_summary(f"{key}:")
                            for sub_key, sub_value in value.items():
                                _emit_response_summary(f"  {sub_key}: {sub_value}")
                        else:
                            _emit_response_summary(f"{key}: {value}")
        except (ValueError, KeyError, AttributeError) as exc:
            interface._set_wait_error(
                WAIT_ATTR_TELEMETRY,
                f"Failed to format telemetry response: {exc}",
                request_id=request_id,
            )
            return
        interface._mark_wait_acknowledged(
            WAIT_ATTR_TELEMETRY,
            request_id=request_id,
        )

    elif p["decoded"]["portnum"] == portnums_pb2.PortNum.Name(
        portnums_pb2.PortNum.ROUTING_APP
    ):
        interface._record_routing_wait_error(
            acknowledgment_attr=WAIT_ATTR_TELEMETRY,
            routing_error_reason=p["decoded"].get("routing", {}).get("errorReason"),
            request_id=request_id,
        )


# pylint: disable=too-many-positional-arguments
def send_telemetry(
    interface: "MeshInterface",
    destinationId: int | str = BROADCAST_ADDR,
    wantResponse: bool = False,
    channelIndex: int = 0,
    telemetryType: TelemetryType | str = DEFAULT_TELEMETRY_TYPE,
    hopLimit: int | None = None,
) -> None:
    """Send a telemetry message to a node or broadcast and optionally wait for a telemetry response."""
    telemetry_type = telemetryType
    if telemetry_type not in VALID_TELEMETRY_TYPE_SET:
        logger.warning(
            f"Unsupported telemetryType '{telemetry_type}' is deprecated. "
            f"Supported values: {sorted(VALID_TELEMETRY_TYPES)}. "
            f"Falling back to '{DEFAULT_TELEMETRY_TYPE}'. "
            "This will raise an error in a future version."
        )
        warnings.warn(
            f"Unsupported telemetryType '{telemetry_type}' is deprecated. "
            f"Supported values: {sorted(VALID_TELEMETRY_TYPES)}. "
            f"Falling back to '{DEFAULT_TELEMETRY_TYPE}'. "
            "This will raise an error in a future version.",
            DeprecationWarning,
            stacklevel=2,
        )
        telemetry_type = DEFAULT_TELEMETRY_TYPE
    r = telemetry_pb2.Telemetry()

    if telemetry_type == "environment_metrics":
        r.environment_metrics.CopyFrom(telemetry_pb2.EnvironmentMetrics())
    elif telemetry_type == "air_quality_metrics":
        r.air_quality_metrics.CopyFrom(telemetry_pb2.AirQualityMetrics())
    elif telemetry_type == "power_metrics":
        r.power_metrics.CopyFrom(telemetry_pb2.PowerMetrics())
    elif telemetry_type == "local_stats":
        r.local_stats.CopyFrom(telemetry_pb2.LocalStats())
    elif telemetry_type == DEFAULT_TELEMETRY_TYPE:
        local_node = interface.localNode
        with interface._node_db_lock:
            node = (
                interface.nodesByNum.get(local_node.nodeNum)
                if interface.nodesByNum is not None and local_node is not None
                else None
            )
            if node is not None:
                metrics = node.get("deviceMetrics")
                if metrics:
                    batteryLevel = metrics.get("batteryLevel")
                    if batteryLevel is not None:
                        r.device_metrics.battery_level = batteryLevel
                    voltage = metrics.get("voltage")
                    if voltage is not None:
                        r.device_metrics.voltage = voltage
                    channel_utilization = metrics.get("channelUtilization")
                    if channel_utilization is not None:
                        r.device_metrics.channel_utilization = channel_utilization
                    air_util_tx = metrics.get("airUtilTx")
                    if air_util_tx is not None:
                        r.device_metrics.air_util_tx = air_util_tx
                    uptime_seconds = metrics.get("uptimeSeconds")
                    if uptime_seconds is not None:
                        r.device_metrics.uptime_seconds = uptime_seconds

    onResponse: Callable[[dict[str, Any]], None] | None
    response_wait_attr: str | None
    if wantResponse:

        def _on_response(packet: dict[str, Any]) -> None:
            _on_response_telemetry(interface, packet)

        onResponse = _on_response
        response_wait_attr = WAIT_ATTR_TELEMETRY
    else:
        onResponse = None
        response_wait_attr = None

    packet = interface._send_data_with_wait(
        r,
        destinationId=destinationId,
        portNum=portnums_pb2.PortNum.TELEMETRY_APP,
        wantResponse=wantResponse,
        onResponse=onResponse,
        channelIndex=channelIndex,
        hopLimit=hopLimit,
        response_wait_attr=response_wait_attr,
    )
    if wantResponse:
        request_id = interface._extract_request_id_from_sent_packet(packet)
        if request_id is None:
            raise interface.MeshInterfaceError(RESPONSE_WAIT_REQID_ERROR)
        interface.waitForTelemetry(request_id=request_id)


def _on_response_waypoint(interface: "MeshInterface", p: dict[str, Any]) -> None:
    """Handle a waypoint response or routing error contained in a received packet."""
    request_id = interface._extract_request_id_from_packet(p)
    if p["decoded"]["portnum"] == portnums_pb2.PortNum.Name(
        portnums_pb2.PortNum.WAYPOINT_APP
    ):
        w = mesh_pb2.Waypoint()
        try:
            w.ParseFromString(p["decoded"]["payload"])
        except (KeyError, TypeError, protobuf_message.DecodeError) as exc:
            interface._set_wait_error(
                WAIT_ATTR_WAYPOINT,
                f"Failed to parse waypoint response payload: {exc}",
                request_id=request_id,
            )
            return
        _emit_response_summary(f"Waypoint received: {w}")
        interface._mark_wait_acknowledged(
            WAIT_ATTR_WAYPOINT,
            request_id=request_id,
        )
    elif p["decoded"]["portnum"] == portnums_pb2.PortNum.Name(
        portnums_pb2.PortNum.ROUTING_APP
    ):
        interface._record_routing_wait_error(
            acknowledgment_attr=WAIT_ATTR_WAYPOINT,
            routing_error_reason=p["decoded"].get("routing", {}).get("errorReason"),
            request_id=request_id,
        )


# pylint: disable=too-many-arguments,too-many-positional-arguments
def send_waypoint(
    interface: "MeshInterface",
    name: str,
    description: str,
    icon: int | str,
    expire: int,
    waypointId: int | None = None,
    latitude: float = 0.0,
    longitude: float = 0.0,
    destinationId: int | str = BROADCAST_ADDR,
    wantAck: bool = True,
    wantResponse: bool = False,
    channelIndex: int = 0,
    hopLimit: int | None = None,
) -> mesh_pb2.MeshPacket:
    """Send a waypoint to a node or broadcast."""
    w = mesh_pb2.Waypoint()
    w.name = name
    w.description = description
    try:
        w.icon = int(icon)
    except (ValueError, TypeError) as exc:
        raise interface.MeshInterfaceError(
            f"Invalid icon value '{icon}': must be an integer or numeric string"
        ) from exc
    w.expire = expire
    if waypointId is None:
        w.id = secrets.randbelow(1_000_000_000)
        logger.debug("w.id:%s", w.id)
    else:
        w.id = waypointId
    if latitude != 0.0:
        w.latitude_i = int(latitude * 1e7)
        logger.debug("w.latitude_i:%s", w.latitude_i)
    if longitude != 0.0:
        w.longitude_i = int(longitude * 1e7)
        logger.debug("w.longitude_i:%s", w.longitude_i)

    onResponse: Callable[[dict[str, Any]], None] | None
    response_wait_attr: str | None
    if wantResponse:

        def _on_response(packet: dict[str, Any]) -> None:
            _on_response_waypoint(interface, packet)

        onResponse = _on_response
        response_wait_attr = WAIT_ATTR_WAYPOINT
    else:
        onResponse = None
        response_wait_attr = None

    d = interface._send_data_with_wait(
        w,
        destinationId,
        portNum=portnums_pb2.PortNum.WAYPOINT_APP,
        wantAck=wantAck,
        wantResponse=wantResponse,
        onResponse=onResponse,
        channelIndex=channelIndex,
        hopLimit=hopLimit,
        response_wait_attr=response_wait_attr,
    )
    if wantResponse:
        request_id = interface._extract_request_id_from_sent_packet(d)
        if request_id is None:
            raise interface.MeshInterfaceError(RESPONSE_WAIT_REQID_ERROR)
        interface.waitForWaypoint(request_id=request_id)
    return d


# pylint: disable=too-many-positional-arguments
def delete_waypoint(
    interface: "MeshInterface",
    waypointId: int,
    destinationId: int | str = BROADCAST_ADDR,
    wantAck: bool = True,
    wantResponse: bool = False,
    channelIndex: int = 0,
    hopLimit: int | None = None,
) -> mesh_pb2.MeshPacket:
    """Delete a waypoint by sending a Waypoint message with expire=0 to a destination."""
    p = mesh_pb2.Waypoint()
    p.id = waypointId
    p.expire = 0

    if wantResponse:
        onResponse: Callable[[dict[str, Any]], None] | None
        response_wait_attr: str | None

        def _on_response(packet: dict[str, Any]) -> None:
            _on_response_waypoint(interface, packet)

        onResponse = _on_response
        response_wait_attr = WAIT_ATTR_WAYPOINT
    else:
        onResponse = None
        response_wait_attr = None

    d = interface._send_data_with_wait(
        p,
        destinationId,
        portNum=portnums_pb2.PortNum.WAYPOINT_APP,
        wantAck=wantAck,
        wantResponse=wantResponse,
        onResponse=onResponse,
        channelIndex=channelIndex,
        hopLimit=hopLimit,
        response_wait_attr=response_wait_attr,
    )
    if wantResponse:
        request_id = interface._extract_request_id_from_sent_packet(d)
        if request_id is None:
            raise interface.MeshInterfaceError(RESPONSE_WAIT_REQID_ERROR)
        interface.waitForWaypoint(request_id=request_id)
    return d
