"""Typed correlation contracts for ADMIN_APP request/response pairs."""

from __future__ import annotations

from dataclasses import dataclass

from meshtastic._core_constants import DECODE_ERROR_KEY
from meshtastic.protobuf import admin_pb2, mesh_pb2

_REQUEST_TO_RESPONSE: dict[str, str] = {
    "get_channel_request": "get_channel_response",
    "get_owner_request": "get_owner_response",
    "get_config_request": "get_config_response",
    "get_module_config_request": "get_module_config_response",
    "get_canned_message_module_messages_request": (
        "get_canned_message_module_messages_response"
    ),
    "get_device_metadata_request": "get_device_metadata_response",
    "get_ringtone_request": "get_ringtone_response",
    "get_device_connection_status_request": "get_device_connection_status_response",
    "get_node_remote_hardware_pins_request": ("get_node_remote_hardware_pins_response"),
    "get_ui_config_request": "get_ui_config_response",
}

# These maps deliberately bind named AdminMessage enum values to named response
# oneof fields. Descriptor field order is not an API contract and must not be used
# as an enum-to-field index.
_CONFIG_RESPONSE_SUBTYPE_BY_REQUEST: dict[int, str] = {
    admin_pb2.AdminMessage.ConfigType.DEVICE_CONFIG: "device",
    admin_pb2.AdminMessage.ConfigType.POSITION_CONFIG: "position",
    admin_pb2.AdminMessage.ConfigType.POWER_CONFIG: "power",
    admin_pb2.AdminMessage.ConfigType.NETWORK_CONFIG: "network",
    admin_pb2.AdminMessage.ConfigType.DISPLAY_CONFIG: "display",
    admin_pb2.AdminMessage.ConfigType.LORA_CONFIG: "lora",
    admin_pb2.AdminMessage.ConfigType.BLUETOOTH_CONFIG: "bluetooth",
    admin_pb2.AdminMessage.ConfigType.SECURITY_CONFIG: "security",
    admin_pb2.AdminMessage.ConfigType.SESSIONKEY_CONFIG: "sessionkey",
    admin_pb2.AdminMessage.ConfigType.DEVICEUI_CONFIG: "device_ui",
}

_MODULE_CONFIG_RESPONSE_SUBTYPE_BY_REQUEST: dict[int, str] = {
    admin_pb2.AdminMessage.ModuleConfigType.MQTT_CONFIG: "mqtt",
    admin_pb2.AdminMessage.ModuleConfigType.SERIAL_CONFIG: "serial",
    admin_pb2.AdminMessage.ModuleConfigType.EXTNOTIF_CONFIG: "external_notification",
    admin_pb2.AdminMessage.ModuleConfigType.STOREFORWARD_CONFIG: "store_forward",
    admin_pb2.AdminMessage.ModuleConfigType.RANGETEST_CONFIG: "range_test",
    admin_pb2.AdminMessage.ModuleConfigType.TELEMETRY_CONFIG: "telemetry",
    admin_pb2.AdminMessage.ModuleConfigType.CANNEDMSG_CONFIG: "canned_message",
    admin_pb2.AdminMessage.ModuleConfigType.AUDIO_CONFIG: "audio",
    admin_pb2.AdminMessage.ModuleConfigType.REMOTEHARDWARE_CONFIG: "remote_hardware",
    admin_pb2.AdminMessage.ModuleConfigType.NEIGHBORINFO_CONFIG: "neighbor_info",
    admin_pb2.AdminMessage.ModuleConfigType.AMBIENTLIGHTING_CONFIG: "ambient_lighting",
    admin_pb2.AdminMessage.ModuleConfigType.DETECTIONSENSOR_CONFIG: "detection_sensor",
    admin_pb2.AdminMessage.ModuleConfigType.PAXCOUNTER_CONFIG: "paxcounter",
    admin_pb2.AdminMessage.ModuleConfigType.STATUSMESSAGE_CONFIG: "statusmessage",
    admin_pb2.AdminMessage.ModuleConfigType.TRAFFICMANAGEMENT_CONFIG: (
        "traffic_management"
    ),
    admin_pb2.AdminMessage.ModuleConfigType.TAK_CONFIG: "tak",
    admin_pb2.AdminMessage.ModuleConfigType.MESHBEACON_CONFIG: "mesh_beacon",
}


def _routing_rejection_reason(decoded: dict[str, object]) -> str | int | None:
    """Return a valid non-success routing reason, never a decode diagnostic."""
    routing = decoded.get("routing")
    if not isinstance(routing, dict) or DECODE_ERROR_KEY in routing:
        return None
    reason = routing.get("errorReason")
    if isinstance(reason, str):
        return (
            reason
            if reason != "NONE" and reason in mesh_pb2.Routing.Error.keys()
            else None
        )
    if isinstance(reason, int) and not isinstance(reason, bool):
        return reason if reason != 0 else None
    return None


@dataclass(frozen=True, slots=True)
class AdminResponseContract:
    """Expected source, response variant, and optional config subtype."""

    expected_sources: frozenset[int]
    response_variant: str
    response_subtype: str | None = None
    local_node_num: int | None = None

    def matches_source(self, packet: dict[str, object]) -> bool:
        """Return whether ``packet`` came from an allowed response source."""
        source = packet.get("from")
        return isinstance(source, int) and source in self.expected_sources

    def _matches_feedback(self, packet: dict[str, object]) -> bool:
        """Accept peer feedback or a rejection by the originating router.

        Request IDs are correlated by the response-handler registry before this
        gate runs. A local ACK only confirms submission; it cannot satisfy a
        remote read. Local data and decode failures cannot stand in for the peer.
        """
        if self.matches_source(packet):
            return True
        source = packet.get("from")
        if self.local_node_num is None or source != self.local_node_num:
            return False
        decoded = packet.get("decoded")
        return (
            _routing_rejection_reason(decoded) is not None
            if isinstance(decoded, dict)
            else False
        )

    def matches(self, packet: dict[str, object]) -> bool:
        """Return whether ``packet`` satisfies this request's response contract."""
        decoded = packet.get("decoded")
        admin = decoded.get("admin") if isinstance(decoded, dict) else None
        raw = admin.get("raw") if isinstance(admin, dict) else None
        if (
            not self.matches_source(packet)
            or not isinstance(raw, admin_pb2.AdminMessage)
            or raw.WhichOneof("payload_variant") != self.response_variant
        ):
            return False
        if self.response_subtype is None:
            return True
        response: admin_pb2.AdminMessage = getattr(raw, self.response_variant)
        return response.WhichOneof("payload_variant") == self.response_subtype


def _response_subtype_for_request(
    request_variant: str, request: admin_pb2.AdminMessage
) -> str | None:
    if request_variant == "get_config_request":
        return _CONFIG_RESPONSE_SUBTYPE_BY_REQUEST.get(int(request.get_config_request))
    if request_variant == "get_module_config_request":
        return _MODULE_CONFIG_RESPONSE_SUBTYPE_BY_REQUEST.get(
            int(request.get_module_config_request)
        )
    return None


def contract_for_admin_request(
    request: admin_pb2.AdminMessage,
    *,
    destination: int,
    local_node_num: int | None,
) -> AdminResponseContract | None:
    """Build a response contract for a getter request, or ``None`` otherwise."""
    request_variant = request.WhichOneof("payload_variant")
    if request_variant is None:
        return None
    response_variant = _REQUEST_TO_RESPONSE.get(request_variant)
    if response_variant is None:
        return None
    expected_sources = {int(destination)}
    if local_node_num is not None and destination == local_node_num:
        # PhoneAPI-local packets have historically appeared with either zero or
        # the local node number depending on firmware/transport generation.
        expected_sources.add(0)
    return AdminResponseContract(
        expected_sources=frozenset(expected_sources),
        response_variant=response_variant,
        response_subtype=_response_subtype_for_request(request_variant, request),
        local_node_num=local_node_num,
    )
