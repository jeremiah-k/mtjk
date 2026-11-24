"""Metadata and channel response runtimes for Node admin requests."""

import logging
from typing import TYPE_CHECKING, Any

from meshtastic.node_runtime.admin_wait import (
    _mark_admin_wait_acknowledged_for_packet,
    _record_admin_wait_error_for_packet,
)
from meshtastic.node_runtime.channel_state import _NodeChannelState
from meshtastic.node_runtime.ports import _NodePort
from meshtastic.node_runtime.shared import ERROR_REASON_NONE, MAX_CHANNELS
from meshtastic.protobuf import channel_pb2, config_pb2, mesh_pb2, portnums_pb2
from meshtastic.util import stripnl

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)
CHANNEL_ROUTING_RETRY_LIMIT = 1


def _has_protobuf_field(obj: Any, field_name: str) -> bool:
    """Safely check whether a protobuf message has a field set."""
    has_field = getattr(obj, "HasField", None)
    if not callable(has_field):
        return False
    try:
        return bool(has_field(field_name))
    except (TypeError, ValueError):
        return False


class _NodeMetadataResponseRuntime:
    """Owns metadata response routing/error handling, state mutation, and signaling."""

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

    def _handle_routing_portnum(
        self, decoded: dict[str, Any], packet: dict[str, Any] | None = None
    ) -> bool:
        """Handle ROUTING_APP metadata responses and indicate whether processing is complete."""
        if decoded.get("portnum") != portnums_pb2.PortNum.Name(
            portnums_pb2.PortNum.ROUTING_APP
        ):
            return False
        routing = decoded.get("routing")
        if not isinstance(routing, dict):
            logger.warning(
                "Received malformed metadata response (missing routing): %s",
                decoded,
            )
            self._node.iface._acknowledgment.receivedNak = True
            if packet is not None:
                _record_admin_wait_error_for_packet(
                    self._node,
                    packet,
                    "Received malformed metadata response (missing routing).",
                )
            self._port.signal_metadata_stdout_event()
            return True
        error_reason = routing.get("errorReason")
        if not isinstance(error_reason, str):
            logger.warning(
                "Received malformed metadata response (invalid routing.errorReason): %s",
                decoded,
            )
            self._node.iface._acknowledgment.receivedNak = True
            if packet is not None:
                _record_admin_wait_error_for_packet(
                    self._node,
                    packet,
                    "Received malformed metadata response (invalid routing.errorReason).",
                )
            self._port.signal_metadata_stdout_event()
            return True
        if error_reason != ERROR_REASON_NONE:
            logger.warning(
                "Metadata request failed, error reason: %s",
                error_reason,
            )
            self._node.iface._acknowledgment.receivedNak = True
            if packet is not None:
                _record_admin_wait_error_for_packet(
                    self._node, packet, f"Routing error on response: {error_reason}"
                )
            self._port.signal_metadata_stdout_event()
            return True
        logger.debug(
            "Metadata request routed successfully; waiting for ADMIN_APP payload."
        )
        return True

    def _handle_generic_routing_error(
        self, decoded: dict[str, Any], packet: dict[str, Any] | None = None
    ) -> bool:
        """Handle non-routing-port metadata errors and indicate completion."""
        routing = decoded.get("routing")
        if not isinstance(routing, dict):
            return False
        error_reason = routing.get("errorReason")
        if not isinstance(error_reason, str):
            logger.warning(
                "Received malformed metadata response (invalid routing.errorReason): %s",
                decoded,
            )
            self._node.iface._acknowledgment.receivedNak = True
            if packet is not None:
                _record_admin_wait_error_for_packet(
                    self._node,
                    packet,
                    "Received malformed metadata response (invalid routing.errorReason).",
                )
            self._port.signal_metadata_stdout_event()
            return True
        if error_reason != ERROR_REASON_NONE:
            logger.error("Error on response: %s", error_reason)
            self._node.iface._acknowledgment.receivedNak = True
            if packet is not None:
                _record_admin_wait_error_for_packet(
                    self._node, packet, f"Routing error on response: {error_reason}"
                )
            self._port.signal_metadata_stdout_event()
            return True
        return False

    def _format_and_emit_enum(
        self,
        *,
        field_name: str,
        value: int,
        enum_type: Any,
    ) -> None:
        """Emit enum metadata field using enum name when valid and raw value otherwise."""
        formatted = enum_type.Name(value) if value in enum_type.values() else value
        self._port.emit_metadata_line(f"{field_name}: {formatted}")

    def _emit_metadata_lines(self, metadata: mesh_pb2.DeviceMetadata) -> None:
        """Emit metadata lines with historical formatting and enum fallback behavior."""
        self._port.emit_metadata_line(
            f"\nfirmware_version: {metadata.firmware_version}"
        )
        self._port.emit_metadata_line(
            f"device_state_version: {metadata.device_state_version}"
        )
        self._format_and_emit_enum(
            field_name="role",
            value=metadata.role,
            enum_type=config_pb2.Config.DeviceConfig.Role,
        )
        self._port.emit_metadata_line(
            f"position_flags: {self._node.position_flags_list(metadata.position_flags)}"
        )
        self._format_and_emit_enum(
            field_name="hw_model",
            value=metadata.hw_model,
            enum_type=mesh_pb2.HardwareModel,
        )
        self._port.emit_metadata_line(f"hasPKC: {metadata.hasPKC}")
        if metadata.excluded_modules > 0:
            self._port.emit_metadata_line(
                f"excluded_modules: {self._node.excluded_modules_list(metadata.excluded_modules)}"
            )

    def handle_metadata_response(self, packet: dict[str, Any]) -> None:
        """Process metadata response packet and preserve historical ACK/timeout semantics."""
        logger.debug("onRequestGetMetadata() p:%s", packet)
        decoded = packet.get("decoded")
        if not isinstance(decoded, dict):
            logger.warning(
                "Received malformed metadata response (missing decoded): %s",
                packet,
            )
            self._node.iface._acknowledgment.receivedNak = True
            _record_admin_wait_error_for_packet(
                self._node,
                packet,
                "Received malformed metadata response (missing decoded).",
            )
            self._port.signal_metadata_stdout_event()
            return
        if self._handle_routing_portnum(decoded, packet):
            return
        if self._handle_generic_routing_error(decoded, packet):
            return

        admin_message = decoded.get("admin")
        if not isinstance(admin_message, dict):
            logger.warning(
                "Received malformed metadata response (missing admin): %s",
                packet,
            )
            self._node.iface._acknowledgment.receivedNak = True
            _record_admin_wait_error_for_packet(
                self._node,
                packet,
                "Received malformed metadata response (missing admin).",
            )
            self._port.signal_metadata_stdout_event()
            return
        raw_admin = admin_message.get("raw")
        if raw_admin is None or not _has_protobuf_field(
            raw_admin, "get_device_metadata_response"
        ):
            logger.warning(
                "Received malformed metadata response (missing admin.raw): %s",
                packet,
            )
            self._node.iface._acknowledgment.receivedNak = True
            _record_admin_wait_error_for_packet(
                self._node,
                packet,
                "Received malformed metadata response (missing admin.raw).",
            )
            self._port.signal_metadata_stdout_event()
            return
        metadata_response = raw_admin.get_device_metadata_response
        metadata_snapshot = mesh_pb2.DeviceMetadata()
        metadata_snapshot.CopyFrom(metadata_response)
        self._node.iface._acknowledgment.receivedAck = True
        _mark_admin_wait_acknowledged_for_packet(self._node, packet)
        self._port.set_metadata_snapshot(metadata_snapshot)
        self._port.timeout.reset()  # We made forward progress
        logger.debug("Received metadata %s", stripnl(metadata_response))
        self._emit_metadata_lines(metadata_response)
        self._port.signal_metadata_stdout_event()


class _NodeChannelResponseRuntime:
    """Owns channel-response routing/error handling, sequencing, and final installation."""

    def __init__(
        self,
        node: "Node",
        port: _NodePort | None = None,
        *,
        channel_state: _NodeChannelState,
    ) -> None:
        """Initialize the runtime with its facade and collaborator port.

        Parameters
        ----------
        node : Node
            The facade whose public state the runtime reads.
        port : _NodePort | None
            Narrow port over the facade's internals; derived from ``node``
            when omitted.
        channel_state : _NodeChannelState
            Shared channel-state owner.
        """
        self._node = node
        self._port = port if port is not None else _NodePort(node)
        self._channel_state = channel_state
        self._pending_channel_request_index: int | None = None
        self._pending_channel_retry_count = 0
        self._channel_request_failed = False

    def mark_channel_request_sent(self, channel_index: int) -> None:
        """Record the outstanding zero-based channel request index."""
        if self._pending_channel_request_index != channel_index:
            self._pending_channel_retry_count = 0
        self._pending_channel_request_index = channel_index
        self._channel_request_failed = False

    def mark_channel_request_send_failed(self, channel_index: int) -> None:
        """Record terminal failure when a channel request could not be sent."""
        if self._pending_channel_request_index != channel_index:
            self._pending_channel_retry_count = 0
        self._pending_channel_request_index = channel_index
        self._channel_request_failed = True
        self._port.timeout.reset()

    def has_channel_request_failed(self) -> bool:
        """Return whether the active channel download has terminally failed."""
        return self._channel_request_failed

    def _retry_pending_channel_request(self, error_reason: str) -> None:
        """Retry the in-flight request once for transient routing failures."""
        request_index = self._pending_channel_request_index
        if request_index is None:
            logger.warning(
                "Channel request failed, error reason: %s (no pending index to retry).",
                error_reason,
            )
            self._channel_request_failed = True
            self._port.timeout.reset()
            return

        if self._pending_channel_retry_count >= CHANNEL_ROUTING_RETRY_LIMIT:
            logger.warning(
                "Channel request failed, error reason: %s (retry limit reached for channel %s).",
                error_reason,
                request_index,
            )
            self._channel_request_failed = True
            self._port.timeout.reset()
            return

        self._pending_channel_retry_count += 1
        logger.warning(
            "Channel request failed, error reason: %s (retrying channel %s, attempt %s/%s).",
            error_reason,
            request_index,
            self._pending_channel_retry_count,
            CHANNEL_ROUTING_RETRY_LIMIT,
        )
        retry_request = self._port.request_channel(request_index)
        if retry_request is None:
            logger.warning(
                "Channel retry for index %s was not started.",
                request_index,
            )
            self._channel_request_failed = True
            self._port.timeout.reset()
            return
        self._port.timeout.reset()  # We retried the in-flight request

    def _handle_routing_response(self, decoded: dict[str, Any]) -> bool:
        """Handle ROUTING_APP channel responses and indicate whether processing is complete."""
        if decoded.get("portnum") != portnums_pb2.PortNum.Name(
            portnums_pb2.PortNum.ROUTING_APP
        ):
            return False

        routing = decoded.get("routing")
        if not isinstance(routing, dict):
            logger.warning(
                "Received malformed channel response (missing routing): %s", decoded
            )
            self._channel_request_failed = True
            self._port.timeout.reset()
        else:
            error_reason = routing.get("errorReason")
            if not isinstance(error_reason, str):
                logger.warning(
                    "Received malformed channel response (invalid routing.errorReason): %s",
                    decoded,
                )
                self._channel_request_failed = True
                self._port.timeout.reset()
            elif error_reason != ERROR_REASON_NONE:
                self._retry_pending_channel_request(error_reason)
            else:
                logger.debug(
                    "Channel request routed successfully; waiting for ADMIN_APP payload."
                )
        return True

    def _validate_channel_response_packet(
        self, packet: dict[str, Any]
    ) -> tuple[bool, channel_pb2.Channel | None]:
        """Validate channel response packet and return (is_valid, channel_response).

        Returns (True, channel) if valid, (False, None) if invalid (logs warning and sets failure state).
        """
        decoded = packet.get("decoded")
        if not isinstance(decoded, dict):
            logger.warning(
                "Received malformed channel response without decoded payload"
            )
            self._channel_request_failed = True
            self._port.timeout.reset()
            return (False, None)
        logger.debug(
            "onResponseRequestChannel() portnum=%s",
            decoded.get("portnum"),
        )
        if self._handle_routing_response(decoded):
            return (False, None)

        admin_message = decoded.get("admin")
        if not isinstance(admin_message, dict):
            logger.warning("Received malformed channel response without admin payload")
            self._channel_request_failed = True
            self._port.timeout.reset()
            return (False, None)
        raw_admin = admin_message.get("raw")
        if raw_admin is None or not _has_protobuf_field(
            raw_admin, "get_channel_response"
        ):
            logger.warning(
                "Received malformed channel response without admin.raw payload"
            )
            self._channel_request_failed = True
            self._port.timeout.reset()
            return (False, None)

        response_channel = raw_admin.get_channel_response
        channel_response = channel_pb2.Channel()
        channel_response.CopyFrom(response_channel)
        return (True, channel_response)

    def _should_skip_channel_response(
        self, channel_response: channel_pb2.Channel
    ) -> bool:
        """Check if channel response should be skipped (stale/duplicate/failure).

        Returns True if should skip, False if should process.
        """
        expected_index = self._pending_channel_request_index
        if self._channel_request_failed:
            logger.debug(
                "Ignoring channel response index=%s after terminal failure.",
                channel_response.index,
            )
            return True
        if expected_index is None:
            logger.debug(
                "Ignoring unexpected channel response index=%s with no pending request.",
                channel_response.index,
            )
            return True
        if channel_response.index != expected_index:
            logger.debug(
                "Ignoring stale channel response index=%s; expected %s.",
                channel_response.index,
                expected_index,
            )
            return True
        return False

    def handle_channel_response(self, packet: dict[str, Any]) -> None:
        """Process channel response packet and maintain partial/final channel sequencing."""
        is_valid, channel_response = self._validate_channel_response_packet(packet)
        if not is_valid:
            return
        if channel_response is None:
            return  # Defensive: should never happen given is_valid=True

        if self._should_skip_channel_response(channel_response):
            return

        if not self._channel_state.append_partial_if_new(channel_response):
            logger.debug(
                "Ignoring duplicate channel response index=%s.",
                channel_response.index,
            )
            return
        self._port.timeout.reset()  # We made forward progress
        safe_role = (
            channel_pb2.Channel.Role.Name(channel_response.role)
            if channel_response.role in channel_pb2.Channel.Role.values()
            else f"UNKNOWN({channel_response.role})"
        )
        logger.debug(
            "Received channel index=%s role=%s name=%s settings=%s",
            channel_response.index,
            safe_role,
            channel_response.settings.name,
            "<REDACTED>",
        )
        self._channel_request_failed = False
        self._pending_channel_retry_count = 0
        index = channel_response.index

        if index >= MAX_CHANNELS - 1:
            logger.debug("Finished downloading channels")
            self._channel_state.install_partial_channels()
            self._pending_channel_request_index = None
            self._pending_channel_retry_count = 0
            return
        next_request = self._port.request_channel(index + 1)
        if next_request is None:
            logger.warning(
                "Failed to request next channel index %s after receiving index %s.",
                index + 1,
                index,
            )
            self._channel_request_failed = True
            self._port.timeout.reset()
