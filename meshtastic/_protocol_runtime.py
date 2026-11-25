"""Internal protocol registry and receive-state update runtime.

The package root re-exports the historical protocol types, registry, and private
handler names. Internal consumers should import this module directly so packet
processing does not depend on :mod:`meshtastic` package-root implementation.
"""

import base64 as _base64
import copy as _copy
import logging
from typing import Any as _Any
from typing import Callable as _Callable
from typing import NamedTuple as _NamedTuple
from typing import TypeGuard as _TypeGuard

from meshtastic._core_constants import DECODE_ERROR_KEY
from meshtastic.protobuf import (
    admin_pb2,
    mesh_beacon_pb2,
    mesh_pb2,
    mqtt_pb2,
    paxcount_pb2,
    portnums_pb2,
    powermon_pb2,
    remote_hardware_pb2,
    storeforward_pb2,
    telemetry_pb2,
)

# Keep handler records in the historical ``meshtastic`` logger hierarchy.
logger = logging.getLogger("meshtastic")

_TELEMETRY_SUBVIEWS: tuple[str, ...] = (
    "deviceMetrics",
    "environmentMetrics",
    "airQualityMetrics",
    "powerMetrics",
    "localStats",
    "healthMetrics",
    "hostMetrics",
    "trafficManagementStats",
)

REDACTED_TEXT = "<redacted>"
REDACTED_BYTES = b"<redacted>"

ProtobufFactory = _Callable[[], _Any]
OnReceive = _Callable[[_Any, dict[str, _Any]], None]

# Package-root names owned by this module.  The private module remains an internal
# implementation detail; this inventory keeps compatibility aliases and dependency
# boundary tests synchronized as the protocol runtime evolves.
__all__ = (
    "KnownProtocol",
    "OnReceive",
    "ProtobufFactory",
    "REDACTED_BYTES",
    "REDACTED_TEXT",
    "protocols",
)

# Complete protocol-owned alias inventory kept at the historical package root.
# The public-looking names above also define this private module's ``__all__``; the
# underscored handlers remain compatibility aliases without becoming star exports.
PACKAGE_ROOT_COMPAT_EXPORTS = __all__ + (
    "_packet_debug_summary",
    "_sanitize_last_received",
    "_on_text_receive",
    "_on_position_receive",
    "_on_node_info_receive",
    "_on_node_status_receive",
    "_on_telemetry_receive",
    "_receive_info_update",
    "_on_admin_receive",
)


class KnownProtocol(_NamedTuple):
    """Used to automatically decode known protocol payloads."""

    #: A descriptive name (e.g. "text", "user", "admin")
    name: str
    #: If set, will be called to parse as a protocol buffer
    protobufFactory: ProtobufFactory | None = None
    #: If set, invoked as onReceive(interface, packet)
    onReceive: OnReceive | None = None


# Preserve historical introspection/pickle metadata for the public re-export.
KnownProtocol.__module__ = "meshtastic"


def _packet_debug_summary(as_dict: dict[str, _Any]) -> dict[str, _Any]:
    """Return a sanitized packet summary for debug logging.

    The summary intentionally omits sensitive payload/body fields while retaining
    enough context for troubleshooting receive-handler flows.
    """
    decoded = as_dict.get("decoded")
    decoded_dict = decoded if isinstance(decoded, dict) else {}
    payload = decoded_dict.get("payload")
    payload_len = (
        len(payload)
        if isinstance(payload, (bytes, bytearray, memoryview, str))
        else None
    )
    decoded_keys = sorted(str(key) for key in decoded_dict.keys())
    return {
        "from": as_dict.get("from"),
        "to": as_dict.get("to"),
        "portnum": decoded_dict.get("portnum"),
        "decoded_keys": decoded_keys,
        "payload_len": payload_len,
    }


def _redact_admin_key_fields(
    fields: dict[str, _Any], *, protobuf_json: bool = False
) -> dict[str, _Any]:
    """Copy a decoded admin mapping and redact either passkey spelling."""
    redacted = dict(fields)
    for key in ("sessionPasskey", "session_passkey"):
        if key not in redacted:
            continue
        if isinstance(redacted[key], (bytes, bytearray, memoryview)):
            redacted[key] = REDACTED_BYTES
        elif protobuf_json and isinstance(redacted[key], str):
            redacted[key] = _base64.b64encode(REDACTED_BYTES).decode("ascii")
        else:
            redacted[key] = REDACTED_TEXT
    return redacted


def _redact_admin_object(raw_admin: _Any) -> _Any:
    """Retain compatible object-shaped payloads when a detached copy is safe."""
    try:
        redacted = _copy.deepcopy(raw_admin)
        if redacted is not raw_admin:
            try:
                redacted.session_passkey = REDACTED_BYTES
            except Exception:  # noqa: BLE001 - legacy read-only payload objects
                del redacted.session_passkey
            if getattr(redacted, "session_passkey", REDACTED_BYTES) == REDACTED_BYTES:
                return redacted
    except Exception:  # noqa: BLE001 - fail closed on uncopyable legacy objects
        logger.debug("Could not redact legacy admin payload object; using sentinel")
    return {"session_passkey": REDACTED_BYTES}


def _sanitize_last_received(as_dict: dict[str, _Any]) -> dict[str, _Any]:
    """Return a node-cache-safe packet copy for ``node['lastReceived']``.

    Keep the packet structure while redacting each representation of the admin
    session passkey. Rebuild serialized payloads from the sanitized protobuf;
    if decoding failed, omit opaque payload bytes from the cache instead.
    The original packet remains available to response and publication callbacks.
    """
    sanitized = dict(as_dict)
    decoded = as_dict.get("decoded")
    outer_raw = as_dict.get("raw")
    raw_is_admin = (
        isinstance(outer_raw, mesh_pb2.MeshPacket)
        and outer_raw.HasField("decoded")
        and outer_raw.decoded.portnum == portnums_pb2.ADMIN_APP
    )
    decoded_is_admin = isinstance(decoded, dict) and (
        "admin" in decoded
        or decoded.get("portnum") in ("ADMIN_APP", portnums_pb2.ADMIN_APP)
    )
    if not (raw_is_admin or decoded_is_admin):
        return sanitized

    # An opaque or malformed admin payload cannot safely be retained. Only a
    # successfully sanitized AdminMessage may supply replacement wire bytes.
    payload = b""
    if isinstance(decoded, dict):
        decoded_sanitized = dict(decoded)
        admin_payload = decoded.get("admin")
        if isinstance(admin_payload, dict):
            raw_admin = admin_payload.get("raw")
            admin_sanitized = _redact_admin_key_fields(
                admin_payload,
                protobuf_json=isinstance(raw_admin, admin_pb2.AdminMessage),
            )
            if isinstance(raw_admin, admin_pb2.AdminMessage):
                redacted_admin = admin_pb2.AdminMessage()
                redacted_admin.CopyFrom(raw_admin)
                # Keep an omitted key omitted; do not invent a credential field.
                if redacted_admin.session_passkey:
                    redacted_admin.session_passkey = REDACTED_BYTES
                admin_sanitized["raw"] = redacted_admin
                payload = redacted_admin.SerializeToString()
            elif isinstance(raw_admin, dict):
                admin_sanitized["raw"] = _redact_admin_key_fields(raw_admin)
            elif hasattr(raw_admin, "session_passkey"):
                admin_sanitized["raw"] = _redact_admin_object(raw_admin)
            elif "raw" in admin_sanitized:
                admin_sanitized["raw"] = REDACTED_TEXT
            decoded_sanitized["admin"] = admin_sanitized
        elif "admin" in decoded:
            decoded_sanitized["admin"] = REDACTED_TEXT
        if "payload" in decoded:
            decoded_sanitized["payload"] = payload
        sanitized["decoded"] = decoded_sanitized
    elif "decoded" in as_dict:
        sanitized["decoded"] = REDACTED_TEXT

    if isinstance(outer_raw, mesh_pb2.MeshPacket):
        redacted_packet = mesh_pb2.MeshPacket()
        redacted_packet.CopyFrom(outer_raw)
        if redacted_packet.HasField("decoded"):
            redacted_packet.decoded.payload = payload
        else:
            # An inconsistent outer representation may carry opaque data.
            redacted_packet.ClearField("encrypted")
        sanitized["raw"] = redacted_packet
    elif "raw" in as_dict:
        sanitized["raw"] = REDACTED_TEXT
    return sanitized


def _is_valid_node_num(value: object) -> _TypeGuard[int]:
    """Return True when value is an integer node number (excluding bool)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _extract_sender_and_decoded(
    as_dict: dict[str, _Any],
) -> tuple[int, dict[str, _Any]] | None:
    """Return validated packet sender and decoded payload dictionary.

    Parameters
    ----------
    as_dict : dict[str, _Any]
        Packet dictionary expected to contain an integer ``from`` and dict
        ``decoded`` payload.

    Returns
    -------
    tuple[int, dict[str, Any]] | None
        ``(sender, decoded)`` when both fields are present with expected types;
        otherwise ``None``.
    """
    sender = as_dict.get("from")
    decoded = as_dict.get("decoded")
    if not _is_valid_node_num(sender) or not isinstance(decoded, dict):
        return None
    return sender, decoded


def _on_text_receive(iface: _Any, as_dict: dict[str, _Any]) -> None:
    """Decode text payloads from a received packet and update per-node metadata.

    If the packet's decoded.payload contains valid UTF-8, store the decoded string in
    as_dict["decoded"]["text"]. If decoding fails, leave that field unset and log an error.
    Always invokes the interface's info-update path to refresh node metadata based on the packet.

    Parameters
    ----------
    iface : _Any
        The interface instance that received the packet.
    as_dict : dict[str, _Any]
        Packet dictionary expected to contain
        decoded.payload (bytes) where the text is stored.
    """
    # We don't throw if the utf8 is invalid in the text message.  Instead we just don't populate
    # the decoded.data.text and we log an error message.  This at least allows some delivery to
    # the app and the app can deal with the missing decoded representation.
    #
    # Usually btw this problem is caused by apps sending binary data but setting the payload type to
    # text.
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("in _on_text_receive() %s", _packet_debug_summary(as_dict))
    try:
        as_bytes = as_dict["decoded"]["payload"]
        as_dict["decoded"]["text"] = as_bytes.decode("utf-8")
    except (UnicodeDecodeError, KeyError, AttributeError, TypeError):
        logger.exception("Malformatted utf8 in text message")
    _receive_info_update(iface, as_dict)


def _on_position_receive(iface: _Any, as_dict: dict[str, _Any]) -> None:
    """Update the sender node's stored position when a received packet contains position data.

    If as_dict contains a "from" field and a decoded "position", the position is normalized
    using the interface's fixup routine and written to that node's "position" entry.

    Parameters
    ----------
    iface : _Any
        Interface instance that provides position normalization and node lookup helpers.
    as_dict : dict[str, _Any]
        Packet dictionary expected to contain
        "from" and "decoded"->"position".
    """
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("in _on_position_receive() %s", _packet_debug_summary(as_dict))
    packet_guard = _extract_sender_and_decoded(as_dict)
    if packet_guard is None:
        return
    sender, decoded = packet_guard
    if "position" in decoded:
        _receive_info_update(iface, as_dict)
        p = decoded["position"]
        if not isinstance(p, dict):
            logger.debug(
                "Skipping position update from=%s: unexpected payload type %s",
                sender,
                type(p).__name__,
            )
            return
        if DECODE_ERROR_KEY in p:
            logger.debug(
                "Skipping position state update from=%s due to decode error payload",
                sender,
            )
            return
        logger.debug("position payload received from=%s", sender)
        p = iface._fixup_position(p)
        logger.debug("position payload normalized from=%s", sender)
        node = iface._get_or_create_by_num(sender)
        with iface._node_db_lock:
            node["position"] = p


def _on_node_info_receive(iface: _Any, as_dict: dict[str, _Any]) -> None:
    """Update the local node record from a received NodeInfo ("user") payload.

    When `as_dict` contains a decoded `"user"` entry and a `"from"` sender, stores
    the decoded user protobuf on the sender's node under `node["user"]`, ensures
    `iface.nodes` maps the user's `id` to that node, and refreshes per-node metadata
    via _receive_info_update(iface, as_dict).

    Parameters
    ----------
    iface : _Any
        Interface instance managing the node database.
    as_dict : dict[str, _Any]
        Received packet dictionary; expected to contain
        `"decoded" -> "user"` and `"from"`.
    """
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("in _on_node_info_receive() %s", _packet_debug_summary(as_dict))
    packet_guard = _extract_sender_and_decoded(as_dict)
    if packet_guard is None:
        return
    sender, decoded = packet_guard
    if "user" in decoded:
        _receive_info_update(iface, as_dict)
        p = decoded["user"]
        if not isinstance(p, dict):
            logger.debug(
                "Skipping user update from=%s: unexpected payload type %s",
                sender,
                type(p).__name__,
            )
            return
        if DECODE_ERROR_KEY in p:
            logger.debug(
                "Skipping user state update from=%s due to decode error payload",
                sender,
            )
            return
        # decode user protobufs and update nodedb, provide decoded version as "position" in the published msg
        # update node DB as needed
        n = iface._get_or_create_by_num(sender)
        with iface._node_db_lock:
            n["user"] = p
            # We now have a node ID, make sure it is up-to-date in that table
            node_id = p.get("id")
            nodes_by_id = iface.nodes
            if isinstance(node_id, str) and node_id and isinstance(nodes_by_id, dict):
                nodes_by_id[node_id] = n


def _on_telemetry_receive(iface: _Any, as_dict: dict[str, _Any]) -> None:
    """Update the appropriate telemetry section on the sender node when a telemetry packet is received.

    Merges metrics from the packet's `decoded.telemetry` into one of the node's telemetry
    sections: `deviceMetrics`, `environmentMetrics`, `airQualityMetrics`, `powerMetrics`,
    `localStats`, `healthMetrics`, `hostMetrics`, or `trafficManagementStats`. If the packet
    lacks a `from` field or none of these sections are present, no change is made.

    Parameters
    ----------
    iface : _Any
        Interface instance used to look up or create the target node.
    as_dict : dict[str, _Any]
        Received packet dictionary; expected to include
        a `from` key and may include `decoded.telemetry`.
    """
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("in _on_telemetry_receive() %s", _packet_debug_summary(as_dict))
    packet_guard = _extract_sender_and_decoded(as_dict)
    if packet_guard is None:
        return
    sender, decoded = packet_guard
    _receive_info_update(iface, as_dict)

    telemetry = decoded.get("telemetry") or {}
    if not isinstance(telemetry, dict):
        logger.debug(
            "Skipping telemetry update from=%s: unexpected telemetry payload type %s",
            sender,
            type(telemetry).__name__,
        )
        return
    if DECODE_ERROR_KEY in telemetry:
        logger.debug(
            "Skipping telemetry state update from=%s due to decode error payload",
            sender,
        )
        return
    to_update = next(
        (subview for subview in _TELEMETRY_SUBVIEWS if subview in telemetry), None
    )
    if to_update is None:
        return

    update_obj = telemetry.get(to_update)
    if not isinstance(update_obj, dict):
        return
    node = iface._get_or_create_by_num(sender)
    with iface._node_db_lock:
        new_metrics = node.get(to_update, {})
        if not isinstance(new_metrics, dict):
            new_metrics = {}
        new_metrics.update(update_obj)
        logger.debug("updating %s metrics for %s to %s", to_update, sender, new_metrics)
        node[to_update] = new_metrics


def _receive_info_update(iface: _Any, as_dict: dict[str, _Any]) -> None:
    """Update per-node metadata fields based on information present in a received packet dictionary.

    Parameters
    ----------
    iface : _Any
        The interface instance whose node store will be updated.
    as_dict : dict[str, _Any]
        Parsed packet dictionary; if it contains an integer "from" key, the
        node identified by that value will have these fields set:
        - lastReceived: packet dictionary copy with admin session passkey redacted
        - lastHeard: value of `rxTime` from the packet (or None)
        - snr: value of `rxSnr` from the packet (or None)
        - hopLimit: value of `hopLimit` from the packet (or None)
    """
    sender = as_dict.get("from")
    if not _is_valid_node_num(sender):
        if sender is not None:
            logger.debug(
                "Skipping receive info update due to non-integer sender type: %s",
                type(sender).__name__,
            )
        return
    node = iface._get_or_create_by_num(sender)
    sanitized_last_received = _sanitize_last_received(as_dict)
    with iface._node_db_lock:
        node["lastReceived"] = sanitized_last_received
        node["lastHeard"] = as_dict.get("rxTime")
        node["snr"] = as_dict.get("rxSnr")
        node["hopLimit"] = as_dict.get("hopLimit")


def _on_node_status_receive(iface: _Any, as_dict: dict[str, _Any]) -> None:
    """Retain the latest status string on the sender node from a NODE_STATUS_APP packet.

    When `as_dict` contains a decoded `"nodestatus"` payload and a `"from"` sender,
    store the decoded status text on the sender node under `node["status"]`.
    Decode-error or malformed payloads must not overwrite the cached status;
    packets with a decoded node-status payload still update receive metadata.

    Parameters
    ----------
    iface : _Any
        Interface instance managing the node database.
    as_dict : dict[str, _Any]
        Received packet dictionary; expected to contain
        `"decoded" -> "nodestatus"` and `"from"`.
    """
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("in _on_node_status_receive() %s", _packet_debug_summary(as_dict))
    packet_guard = _extract_sender_and_decoded(as_dict)
    if packet_guard is None:
        return
    sender, decoded = packet_guard
    if "nodestatus" in decoded:
        _receive_info_update(iface, as_dict)
        p = decoded["nodestatus"]
        if not isinstance(p, dict):
            logger.debug(
                "Skipping node status update from=%s: unexpected payload type %s",
                sender,
                type(p).__name__,
            )
            return
        if DECODE_ERROR_KEY in p:
            logger.debug(
                "Skipping node status state update from=%s due to decode error payload",
                sender,
            )
            return
        status_text = p.get("status")
        if not isinstance(status_text, str):
            logger.debug(
                "Skipping node status update from=%s: missing or invalid status string",
                sender,
            )
            return
        n = iface._get_or_create_by_num(sender)
        with iface._node_db_lock:
            n["status"] = status_text


def _on_admin_receive(iface: _Any, as_dict: dict[str, _Any]) -> None:
    """Store the admin session passkey from an admin packet on the sending node.

    If the expected fields are present in `as_dict`, sets the sender node's
    "adminSessionPassKey" to the extracted `session_passkey`.

    Parameters
    ----------
    iface : _Any
        The interface instance managing the node database.
    as_dict : dict[str, _Any]
        Received packet dictionary; expected to contain
        `decoded.admin.raw.session_passkey` and a `from` sender field.
    """
    logger.debug("in _on_admin_receive() from=%s", as_dict.get("from"))
    packet_guard = _extract_sender_and_decoded(as_dict)
    if packet_guard is None:
        sender = as_dict.get("from")
        decoded = as_dict.get("decoded")
        if sender is None:
            logger.debug("Dropping admin packet because 'from' field is missing")
        elif not _is_valid_node_num(sender):
            logger.debug("Admin packet has invalid 'from' field type: %r", type(sender))
        elif not isinstance(decoded, dict):
            logger.debug("Admin packet missing decoded dict from=%s", sender)
        return

    sender, decoded = packet_guard
    _receive_info_update(iface, as_dict)

    admin_payload = decoded.get("admin")
    if not isinstance(admin_payload, dict):
        logger.debug("Admin packet missing admin payload dict from=%s", sender)
        return

    raw_admin = admin_payload.get("raw")
    session_passkey = getattr(raw_admin, "session_passkey", None)
    if session_passkey is None and isinstance(raw_admin, dict):
        session_passkey = raw_admin.get("session_passkey")

    if not session_passkey:
        logger.debug(
            "Admin session passkey not extracted from admin packet from=%s",
            sender,
        )
        return

    node = iface._get_or_create_by_num(sender)
    with iface._node_db_lock:
        node["adminSessionPassKey"] = session_passkey


# Well known message payloads can register decoders for automatic protobuf parsing.
protocols = {
    portnums_pb2.PortNum.TEXT_MESSAGE_APP: KnownProtocol(
        "text", onReceive=_on_text_receive
    ),
    portnums_pb2.PortNum.RANGE_TEST_APP: KnownProtocol(
        "rangetest", onReceive=_on_text_receive
    ),
    portnums_pb2.PortNum.DETECTION_SENSOR_APP: KnownProtocol(
        "detectionsensor", onReceive=_on_text_receive
    ),
    portnums_pb2.PortNum.POSITION_APP: KnownProtocol(
        "position", mesh_pb2.Position, _on_position_receive
    ),
    portnums_pb2.PortNum.NODEINFO_APP: KnownProtocol(
        "user", mesh_pb2.User, _on_node_info_receive
    ),
    portnums_pb2.PortNum.ADMIN_APP: KnownProtocol(
        "admin", admin_pb2.AdminMessage, _on_admin_receive
    ),
    portnums_pb2.PortNum.ROUTING_APP: KnownProtocol("routing", mesh_pb2.Routing),
    portnums_pb2.PortNum.TELEMETRY_APP: KnownProtocol(
        "telemetry", telemetry_pb2.Telemetry, _on_telemetry_receive
    ),
    portnums_pb2.PortNum.REMOTE_HARDWARE_APP: KnownProtocol(
        "remotehw", remote_hardware_pb2.HardwareMessage
    ),
    portnums_pb2.PortNum.SIMULATOR_APP: KnownProtocol("simulator", mesh_pb2.Compressed),
    portnums_pb2.PortNum.TRACEROUTE_APP: KnownProtocol(
        "traceroute", mesh_pb2.RouteDiscovery
    ),
    portnums_pb2.PortNum.POWERSTRESS_APP: KnownProtocol(
        "powerstress", powermon_pb2.PowerStressMessage
    ),
    portnums_pb2.PortNum.WAYPOINT_APP: KnownProtocol("waypoint", mesh_pb2.Waypoint),
    portnums_pb2.PortNum.PAXCOUNTER_APP: KnownProtocol(
        "paxcounter", paxcount_pb2.Paxcount
    ),
    portnums_pb2.PortNum.STORE_FORWARD_APP: KnownProtocol(
        "storeforward", storeforward_pb2.StoreAndForward
    ),
    portnums_pb2.PortNum.NEIGHBORINFO_APP: KnownProtocol(
        "neighborinfo", mesh_pb2.NeighborInfo
    ),
    portnums_pb2.PortNum.MAP_REPORT_APP: KnownProtocol("mapreport", mqtt_pb2.MapReport),
    # Firmware 2.8 mesh beacons carry a text message plus optional
    # channel/preset offers for client apps (see portnums.proto).
    portnums_pb2.PortNum.MESH_BEACON_APP: KnownProtocol(
        "meshbeacon", mesh_beacon_pb2.MeshBeacon
    ),
    # Firmware 2.8 status broadcasts carry a single status string per node
    # (StatusMessageModule); clients surface it alongside node metadata.
    portnums_pb2.PortNum.NODE_STATUS_APP: KnownProtocol(
        "nodestatus", mesh_pb2.StatusMessage, _on_node_status_receive
    ),
    # Firmware 2.8 PKI key-verification handshakes (KeyVerificationModule):
    # nonce plus hash1/hash2 challenge-response pairs.
    portnums_pb2.PortNum.KEY_VERIFICATION_APP: KnownProtocol(
        "keyverification",  # gitleaks:allow
        mesh_pb2.KeyVerification,
    ),
    # Critical alerts carry a plain UTF-8 text payload, like detection
    # sensor messages; they break through client notification suppression.
    portnums_pb2.PortNum.ALERT_APP: KnownProtocol("alert", onReceive=_on_text_receive),
}
