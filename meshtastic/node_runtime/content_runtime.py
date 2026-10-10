"""Ringtone/canned-message content cache and request/response runtimes."""

import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from meshtastic._deadline import _remaining_timeout
from meshtastic.node_runtime.shared import (
    MAX_CANNED_MESSAGE_LENGTH,
    MAX_RINGTONE_LENGTH,
)
from meshtastic.protobuf import admin_pb2, mesh_pb2

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)

CANNED_MESSAGE_CHUNK_SIZE = 50


class _NodeContentCacheStore:
    """Owns ringtone/canned-message cache state, fragment storage, and invalidation."""

    def __init__(self, node: "Node") -> None:
        self._node = node

    def _get_cached_ringtone(self) -> str | None:
        """Return cached full ringtone value when already present.

        Locking: Assumes caller holds ringtone operation lock.
        """
        if self._node.ringtone is not None:
            logger.debug("ringtone cached (%d chars)", len(self._node.ringtone))
            return self._node.ringtone
        return None

    def _clear_ringtone_fragment(self) -> None:
        """Clear stale ringtone fragment state before issuing a read request.

        Locking: Assumes caller holds ringtone operation lock.
        """
        self._node.ringtonePart = None
        logger.debug("ringtone fragment cache cleared")

    def _store_ringtone_fragment(self, ringtone_fragment: str) -> None:
        """Store ringtone fragment from the latest response packet.

        Locking: Assumes caller holds ringtone operation lock.
        """
        self._node.ringtonePart = ringtone_fragment
        logger.debug("ringtone fragment stored (%d chars)", len(ringtone_fragment))

    def _resolve_ringtone_after_read(self) -> str | None:
        """Resolve ringtone result after a read wait by preferring full cache, then fragment.

        Locking: Assumes caller holds ringtone operation lock.
        """
        if self._node.ringtone is not None:
            logger.debug(
                "ringtone resolved from full cache (%d chars)",
                len(self._node.ringtone),
            )
            return self._node.ringtone
        if self._node.ringtonePart is not None:
            self._node.ringtone = self._node.ringtonePart
            logger.debug(
                "ringtone resolved from fragment cache (%d chars)",
                len(self._node.ringtone),
            )
            return self._node.ringtone
        return None

    def _invalidate_ringtone_cache(self) -> None:
        """Invalidate ringtone full/fragment cache after writes.

        Locking: Assumes caller holds ringtone operation lock.
        """
        self._node.ringtone = None
        self._node.ringtonePart = None
        logger.debug("ringtone cache invalidated")

    def _get_cached_canned_message(self) -> str | None:
        """Return cached full canned-message value when already present.

        Locking: Assumes caller holds canned-message operation lock.
        """
        if self._node.cannedPluginMessage is not None:
            logger.debug(
                "canned message cached (%d chars)",
                len(self._node.cannedPluginMessage),
            )
            return self._node.cannedPluginMessage
        return None

    def _clear_canned_message_fragment(self) -> None:
        """Clear stale canned-message fragment state before issuing a read request.

        Locking: Assumes caller holds canned-message operation lock.
        """
        self._node.cannedPluginMessageMessages = None
        logger.debug("canned message fragment cache cleared")

    def _store_canned_message_fragment(self, canned_messages: str) -> None:
        """Store canned-message fragment payload from response packets.

        Locking: Assumes caller holds canned-message operation lock.
        """
        self._node.cannedPluginMessageMessages = canned_messages
        logger.debug("canned message fragment stored (%d chars)", len(canned_messages))

    def _resolve_canned_message_after_read(self) -> str | None:
        """Resolve canned-message result after a read wait.

        Locking: Assumes caller holds canned-message operation lock.
        """
        if self._node.cannedPluginMessage is not None:
            logger.debug(
                "canned message resolved from full cache (%d chars)",
                len(self._node.cannedPluginMessage),
            )
            return self._node.cannedPluginMessage
        if self._node.cannedPluginMessageMessages is not None:
            self._node.cannedPluginMessage = self._node.cannedPluginMessageMessages
            logger.debug(
                "canned message resolved from fragment cache (%d chars)",
                len(self._node.cannedPluginMessage),
            )
            return self._node.cannedPluginMessage
        return None

    def _invalidate_canned_message_cache(self) -> None:
        """Invalidate canned-message full/fragment cache after writes.

        Locking: Assumes caller holds canned-message operation lock.
        """
        self._node.cannedPluginMessage = None
        self._node.cannedPluginMessageMessages = None
        logger.debug("canned message cache invalidated")


class _NodeContentResponseRuntime:
    """Owns ringtone/canned-message response parsing and fragment cache updates."""

    def __init__(self, node: "Node", *, cache_store: _NodeContentCacheStore) -> None:
        self._node = node
        self._cache_store = cache_store

    @staticmethod
    def _has_routing_error(decoded: dict[str, Any]) -> bool:
        """Return True when decoded routing payload contains a non-NONE error reason."""
        routing = decoded.get("routing")
        if not isinstance(routing, dict):
            return False
        error_reason = routing.get("errorReason")
        if isinstance(error_reason, str) and error_reason != "NONE":
            logger.error("Error on response: %s", error_reason)
            return True
        return False

    def _validate_admin_response_packet(
        self, decoded: dict[str, Any] | None, content_type: str
    ) -> tuple[bool, bool, Any | None]:
        """Validate decoded packet and return (is_terminal, has_routing_ack, raw_admin or None).

        has_routing_ack is True when we received a routing ACK (errorReason == "NONE")
        but no admin payload yet - caller should continue waiting. The request-wait
        runtime now holds typed handlers pending across routing ACKs, so this branch
        is defensive for untyped flows only.
        """
        if not isinstance(decoded, dict):
            logger.warning(
                "Unexpected %s response without decoded payload", content_type
            )
            return (True, False, None)
        routing = decoded.get("routing")
        admin_message = decoded.get("admin")
        if isinstance(routing, dict):
            if self._has_routing_error(decoded):
                return (True, False, None)
            if routing.get("errorReason") == "NONE" and not isinstance(
                admin_message, dict
            ):
                # Defensive for untyped flows: typed handlers are held pending
                # across routing ACKs by the request-wait runtime.
                return (False, True, None)
        if not isinstance(admin_message, dict):
            logger.warning("Unexpected %s response without admin payload", content_type)
            return (True, False, None)
        return (False, False, admin_message.get("raw"))

    def _handle_ringtone_response(
        self, packet: dict[str, Any]
    ) -> tuple[bool, str | None]:
        """Parse ringtone response packet and return (is_terminal, payload)."""
        logger.debug("onResponseRequestRingtone()")
        is_terminal, has_routing_ack, raw_admin = self._validate_admin_response_packet(
            packet.get("decoded"), "ringtone"
        )
        if is_terminal:
            return (True, None)
        if has_routing_ack:
            return (False, None)
        if raw_admin is None:
            logger.warning("Unexpected ringtone response without raw ringtone data")
            return (True, None)
        has_field = getattr(raw_admin, "HasField", None)
        try:
            has_ringtone_response = callable(
                has_field
            ) and has_field(  # pylint: disable=not-callable
                "get_ringtone_response"
            )
        except (TypeError, ValueError):
            has_ringtone_response = False
        if not has_ringtone_response:
            logger.warning("Unexpected ringtone response without raw ringtone data")
            return (True, None)
        try:
            payload = raw_admin.get_ringtone_response
            return (True, payload)
        except AttributeError:
            logger.warning("Failed to parse ringtone response payload")
            return (True, None)

    def _handle_canned_message_response(
        self, packet: dict[str, Any]
    ) -> tuple[bool, str | None]:
        """Parse canned-message response packet and return (is_terminal, payload)."""
        logger.debug("onResponseRequestCannedMessagePluginMessageMessages()")
        is_terminal, has_routing_ack, raw_admin = self._validate_admin_response_packet(
            packet.get("decoded"), "canned-message"
        )
        if is_terminal:
            return (True, None)
        if has_routing_ack:
            return (False, None)
        if raw_admin is None:
            logger.warning(
                "Unexpected canned-message response without raw message data"
            )
            return (True, None)
        has_field = getattr(raw_admin, "HasField", None)
        try:
            has_canned_response = callable(
                has_field
            ) and has_field(  # pylint: disable=not-callable
                "get_canned_message_module_messages_response"
            )
        except (TypeError, ValueError):
            has_canned_response = False
        if not has_canned_response:
            logger.warning(
                "Unexpected canned-message response without raw message data"
            )
            return (True, None)
        try:
            payload = raw_admin.get_canned_message_module_messages_response
            return (True, payload)
        except AttributeError:
            logger.warning("Failed to parse canned-message response payload")
            return (True, None)


class _NodeAdminContentRuntime:
    """Owns admin request/wait orchestration for ringtone and canned-message reads/writes."""

    def __init__(
        self,
        node: "Node",
        *,
        cache_store: _NodeContentCacheStore,
        response_runtime: _NodeContentResponseRuntime,
    ) -> None:
        self._node = node
        self._cache_store = cache_store
        self._response_runtime = response_runtime
        self._ringtone_operation_lock = threading.Lock()
        self._canned_message_operation_lock = threading.Lock()
        self._read_state_lock = threading.Lock()
        self._ringtone_read_generation = 0
        self._active_ringtone_read_generation: int | None = None
        self._canned_message_read_generation = 0
        self._active_canned_message_read_generation: int | None = None

    def _begin_ringtone_read(self) -> int:
        """Start a ringtone read generation and mark it active."""
        with self._read_state_lock:
            self._ringtone_read_generation += 1
            self._active_ringtone_read_generation = self._ringtone_read_generation
            return self._ringtone_read_generation

    def _retire_ringtone_read(self, generation: int) -> None:
        """Retire ringtone read generation to ignore late callbacks."""
        with self._read_state_lock:
            if self._active_ringtone_read_generation == generation:
                self._active_ringtone_read_generation = None

    def _is_ringtone_read_active(self, generation: int) -> bool:
        """Return whether ringtone generation is still active."""
        with self._read_state_lock:
            return self._active_ringtone_read_generation == generation

    def _begin_canned_message_read(self) -> int:
        """Start a canned-message read generation and mark it active."""
        with self._read_state_lock:
            self._canned_message_read_generation += 1
            self._active_canned_message_read_generation = (
                self._canned_message_read_generation
            )
            return self._canned_message_read_generation

    def _retire_canned_message_read(self, generation: int) -> None:
        """Retire canned-message read generation to ignore late callbacks."""
        with self._read_state_lock:
            if self._active_canned_message_read_generation == generation:
                self._active_canned_message_read_generation = None

    def _is_canned_message_read_active(self, generation: int) -> bool:
        """Return whether canned-message generation is still active."""
        with self._read_state_lock:
            return self._active_canned_message_read_generation == generation

    def _module_available_or_warn(
        self, excluded_bit: int, warning_message: str
    ) -> bool:
        """Evaluate module availability and emit the legacy warning when unavailable."""
        if not self._node.module_available(excluded_bit):
            logger.warning("%s", warning_message)
            return False
        return True

    def _send_content_read_request(
        self,
        *,
        begin_read_generation: Callable[[], int],
        is_read_generation_active: Callable[[int], bool],
        retire_read_generation: Callable[[int], None],
        build_request: Callable[[admin_pb2.AdminMessage], None],
        handle_response: Callable[[dict[str, Any]], tuple[bool, str | None]],
        commit_payload: Callable[[str], None],
        skipped_send_debug_message: str,
        timeout_warning_message: str,
        resolve_result: Callable[[], str | None],
    ) -> str | None:
        """Send content read request, wait for callback, and resolve cached result."""
        read_generation = begin_read_generation()
        response_event = threading.Event()
        callback_state_lock = threading.Lock()
        buffered_payloads: list[str] = []

        def _on_response(packet: dict[str, Any]) -> None:
            if not is_read_generation_active(read_generation):
                logger.debug(
                    "Ignoring late content response for retired read generation %s",
                    read_generation,
                )
                return
            with callback_state_lock:
                if response_event.is_set():
                    return
            terminal, payload = handle_response(packet)
            if not is_read_generation_active(read_generation):
                logger.debug(
                    "Read generation %s retired during response handling; discarding result",
                    read_generation,
                )
                return
            with callback_state_lock:
                if response_event.is_set():
                    return
                if payload is not None:
                    buffered_payloads.append(payload)
                if terminal:
                    response_event.set()

        try:
            request_message = admin_pb2.AdminMessage()
            build_request(request_message)
            request = self._node._send_admin(  # noqa: SLF001
                request_message,
                wantResponse=True,
                onResponse=_on_response,
            )
            if request is None:
                logger.debug("%s", skipped_send_debug_message)
                return None
            extract_request_id = getattr(
                self._node.iface, "_extract_request_id_from_sent_packet", None
            )
            request_id_value = (
                extract_request_id(request)
                if callable(extract_request_id)
                else getattr(request, "id", None)
            )
            request_id = request_id_value if isinstance(request_id_value, int) else None
            if not response_event.wait(
                timeout=_remaining_timeout(self._node._timeout.expireTimeout)
            ):
                logger.warning("%s", timeout_warning_message)
                request_wait_runtime = getattr(
                    self._node.iface, "_request_wait_runtime", None
                )
                drop_response_handler = (
                    getattr(request_wait_runtime, "drop_response_handler", None)
                    if request_wait_runtime is not None
                    else None
                )
                if request_id is not None and callable(drop_response_handler):
                    drop_response_handler(request_id)
                return None
            with callback_state_lock:
                payloads_to_commit = list(buffered_payloads)
            for payload in payloads_to_commit:
                commit_payload(payload)
            return resolve_result()
        finally:
            retire_read_generation(read_generation)

    def _select_write_response_handler(
        self,
    ) -> Callable[[dict[str, Any]], Any] | None:
        """Return legacy ACK callback selection for local-vs-remote writes."""
        if self._node is self._node.iface.localNode:
            return None
        return self._node.onAckNak

    def read_ringtone(self) -> str | None:
        """Read ringtone using cached-short-circuit + request/wait orchestration."""
        with self._ringtone_operation_lock:
            logger.debug("in get_ringtone()")
            if not self._module_available_or_warn(
                mesh_pb2.EXTNOTIF_CONFIG,
                "External Notification module not present (excluded by firmware)",
            ):
                return None
            cached_ringtone = self._cache_store._get_cached_ringtone()
            if cached_ringtone is not None:
                return cached_ringtone
            self._cache_store._clear_ringtone_fragment()
            return self._send_content_read_request(
                begin_read_generation=self._begin_ringtone_read,
                is_read_generation_active=self._is_ringtone_read_active,
                retire_read_generation=self._retire_ringtone_read,
                build_request=lambda message: setattr(
                    message, "get_ringtone_request", True
                ),
                handle_response=self._response_runtime._handle_ringtone_response,
                commit_payload=self._cache_store._store_ringtone_fragment,
                skipped_send_debug_message=(
                    "Skipping ringtone wait because protocol send was not started"
                ),
                timeout_warning_message="Timed out waiting for ringtone response",
                resolve_result=self._cache_store._resolve_ringtone_after_read,
            )

    def write_ringtone(self, ringtone: str) -> mesh_pb2.MeshPacket | None:
        """Write ringtone payload and invalidate local ringtone cache."""
        with self._ringtone_operation_lock:
            if not self._module_available_or_warn(
                mesh_pb2.EXTNOTIF_CONFIG,
                "External Notification module not present (excluded by firmware)",
            ):
                return None
            if len(ringtone) > MAX_RINGTONE_LENGTH:
                self._node._raise_interface_error(  # noqa: SLF001
                    f"The ringtone must be {MAX_RINGTONE_LENGTH} characters or fewer."
                )
            self._node.ensureSessionKey()
            request_message = admin_pb2.AdminMessage()
            request_message.set_ringtone_message = ringtone
            logger.debug("Setting ringtone (%d chars)", len(ringtone))
            send_result = self._node._send_admin(
                request_message,
                onResponse=self._select_write_response_handler(),
            )
            if send_result is not None:
                self._cache_store._invalidate_ringtone_cache()
            return send_result

    def read_canned_message(self) -> str | None:
        """Read canned-message payload using cached-short-circuit + request/wait orchestration."""
        with self._canned_message_operation_lock:
            logger.debug("in get_canned_message()")
            if not self._module_available_or_warn(
                mesh_pb2.CANNEDMSG_CONFIG,
                "Canned Message module not present (excluded by firmware)",
            ):
                return None
            cached_canned_message = self._cache_store._get_cached_canned_message()
            if cached_canned_message is not None:
                return cached_canned_message
            self._cache_store._clear_canned_message_fragment()
            return self._send_content_read_request(
                begin_read_generation=self._begin_canned_message_read,
                is_read_generation_active=self._is_canned_message_read_active,
                retire_read_generation=self._retire_canned_message_read,
                build_request=lambda message: setattr(
                    message,
                    "get_canned_message_module_messages_request",
                    True,
                ),
                handle_response=self._response_runtime._handle_canned_message_response,
                commit_payload=self._cache_store._store_canned_message_fragment,
                skipped_send_debug_message=(
                    "Skipping canned-message wait because protocol send was not started"
                ),
                timeout_warning_message="Timed out waiting for canned message response",
                resolve_result=self._cache_store._resolve_canned_message_after_read,
            )

    def write_canned_message(self, message: str) -> mesh_pb2.MeshPacket | None:
        """Write canned-message payload and invalidate local canned-message cache."""
        with self._canned_message_operation_lock:
            if not self._module_available_or_warn(
                mesh_pb2.CANNEDMSG_CONFIG,
                "Canned Message module not present (excluded by firmware)",
            ):
                return None
            if len(message) > MAX_CANNED_MESSAGE_LENGTH:
                self._node._raise_interface_error(  # noqa: SLF001
                    f"The canned message must be {MAX_CANNED_MESSAGE_LENGTH} characters or fewer."
                )
            self._node.ensureSessionKey()

            if len(message) <= CANNED_MESSAGE_CHUNK_SIZE:
                request_message = admin_pb2.AdminMessage()
                request_message.set_canned_message_module_messages = message
                logger.debug("Setting canned message (%d chars)", len(message))
                send_result = self._node._send_admin(
                    request_message,
                    onResponse=self._select_write_response_handler(),
                )
                if send_result is not None:
                    self._cache_store._invalidate_canned_message_cache()
                return send_result

            chunks = [
                message[i : i + CANNED_MESSAGE_CHUNK_SIZE]
                for i in range(0, len(message), CANNED_MESSAGE_CHUNK_SIZE)
            ]
            first_send_result: mesh_pb2.MeshPacket | None = None
            response_handler = self._select_write_response_handler()
            wrote_any_chunk = False
            logger.debug(
                "Setting canned message in %d chunks (%d chars total)",
                len(chunks),
                len(message),
            )

            try:
                for chunk_index, chunk in enumerate(chunks):
                    request_message = admin_pb2.AdminMessage()
                    request_message.set_canned_message_module_messages = chunk
                    send_result = self._node._send_admin(
                        request_message,
                        onResponse=response_handler,
                    )
                    if chunk_index == 0:
                        first_send_result = send_result
                    if send_result is None:
                        if wrote_any_chunk:
                            self._cache_store._invalidate_canned_message_cache()
                        return None
                    wrote_any_chunk = True
            except Exception:
                if wrote_any_chunk:
                    self._cache_store._invalidate_canned_message_cache()
                raise

            self._cache_store._invalidate_canned_message_cache()
            return first_send_result
