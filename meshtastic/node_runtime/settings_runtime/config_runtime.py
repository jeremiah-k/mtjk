"""Settings request/write orchestration and callback policy."""

import logging
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from google.protobuf.descriptor import FieldDescriptor

from meshtastic.node_runtime.admin_wait import (
    _scoped_request_id,
    _send_admin_with_ack_scope,
    _wait_for_admin_ack,
)
from meshtastic.node_runtime.settings_runtime.message import (  # pylint: disable=no-name-in-module
    _NodeSettingsMessageBuilder,
)
from meshtastic.node_runtime.shared import ERROR_REASON_NONE
from meshtastic.payload_limits import _validate_firmware_payload_limits
from meshtastic.protobuf import admin_pb2

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)

CONFIG_VERIFY_TIMEOUT_SECONDS = 15.0
CONFIG_VERIFY_POLL_INTERVAL_SECONDS = 0.1


class _NodeSettingsRuntime:
    """Owns settings request/write orchestration and callback policy."""

    def __init__(
        self,
        node: "Node",
        *,
        message_builder: _NodeSettingsMessageBuilder,
    ) -> None:
        self._node = node
        self._message_builder = message_builder

    def request_config(
        self,
        config_type: int | FieldDescriptor,
        *,
        admin_index: int | None = None,
        on_response: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """Send one settings request and register its response application.

        The response handler runs for local and remote nodes alike: without it
        the device's config response is never correlated, so a cleared section
        (e.g. a pre-verification refresh) silently stays at defaults and any
        subsequent comparison reads manufactured values instead of device
        state. Scoped requests wait for correlated completion. The historical
        local compatibility path has no scoped wait bookkeeping, so it returns
        after registering the response callback and applies that response
        asynchronously when it arrives.
        """
        if self._node is not self._node.iface.localNode:
            logger.info(
                "Requesting current config from remote node (this can take a while)."
            )
        if on_response is None:
            on_response = self._node.onResponseRequestSettings

        message = self._message_builder.build_request_message(config_type)
        request = _send_admin_with_ack_scope(
            self._node,
            message,
            scope_ack=on_response is not None,
            wantResponse=True,
            onResponse=on_response,
            adminIndex=admin_index,
        )
        # In noProto mode, _send_admin legitimately returns None (no actual sending)
        if request is None and not getattr(self._node, "noProto", False):
            self._node._raise_interface_error(
                f"requestConfig failed: admin message not started (admin_index={admin_index})"
            )
        if on_response is not None and request is not None:
            if (
                self._node is self._node.iface.localNode
                and _scoped_request_id(self._node, request) is None
            ):
                # A want_response settings request to the local node completes
                # through its correlated data response, not a Routing ACK.
                # Without scoped wait bookkeeping there is no bounded,
                # correlated wait to run, so fall back to the registered
                # response handler alone instead of the legacy interface ACK
                # wait, which would block on an acknowledgment the firmware
                # never sends for want_response admin requests.
                return
            _wait_for_admin_ack(self._node, request)

    def _validate_write_configs_loaded(self, config_name: str) -> None:
        """Preserve historical writeConfig loaded-state behavior.

        Historical behavior only required that *some* local/module config had
        been loaded before writes. Keep that compatibility for configure flows
        that intentionally write empty/default sections.
        """
        config_entry = self._message_builder.get_write_config_entry(config_name)
        if config_entry is None:
            self._node._raise_interface_error(  # noqa: SLF001
                f"Error: No valid config with name {config_name}"
            )

        _, source_config = config_entry
        if len(source_config.ListFields()) > 0:
            return
        if (
            len(self._node.localConfig.ListFields()) > 0
            or len(self._node.moduleConfig.ListFields()) > 0
        ):
            logger.debug(
                "Writing %s with empty payload to preserve historical compatibility.",
                config_name,
            )
            return
        self._node._raise_interface_error(  # noqa: SLF001
            "Error: No config has been read. "
            "Request config from the device before writing."
        )

    def write_config(self, config_name: str, *, verify: bool = False) -> None:
        """Send one settings write, optionally verifying the device applied it."""
        self._message_builder.validate_config_name(config_name)
        self._validate_write_configs_loaded(config_name)
        message = self._message_builder.build_write_message(config_name)
        if not getattr(self._node, "noProto", False):
            # Session preparation can request a passkey over the transport.
            # Reject invalid staged values before those requests or snapshot
            # serialization, even when read-back verification is disabled.
            _validate_firmware_payload_limits(message, context="Config write")
        staged_bytes: bytes | None = None
        if verify:
            config_entry = self._message_builder.get_write_config_entry(config_name)
            if config_entry is None:
                self._node._raise_interface_error(  # noqa: SLF001
                    f"Error: No valid config with name {config_name}"
                )
                raise AssertionError("Unreachable: _raise_interface_error must raise")
            setter_name, _source_config = config_entry
            staged_bytes = getattr(
                getattr(message, setter_name), config_name
            ).SerializeToString()
        logger.debug("Sending write: %s", config_name)
        self._node.ensureSessionKey()
        on_response = (
            None if self._node is self._node.iface.localNode else self._node.onAckNak
        )
        request = _send_admin_with_ack_scope(
            self._node,
            message,
            scope_ack=on_response is not None,
            onResponse=on_response,
        )
        # In noProto mode, _send_admin legitimately returns None (no actual sending)
        if request is None and not getattr(self._node, "noProto", False):
            self._node._raise_interface_error(
                f"writeConfig failed: admin message not started (config_name={config_name})"
            )
        if on_response is not None and request is not None:
            _wait_for_admin_ack(self._node, request)
        if verify:
            assert staged_bytes is not None
            self._verify_written_config(config_name, staged_bytes=staged_bytes)
        logger.debug("Config write completed: %s", config_name)

    def _section_root_and_descriptor(
        self, config_name: str
    ) -> tuple[Any, FieldDescriptor]:
        """Return the config root holding one section plus its descriptor."""
        for source_config in (self._node.localConfig, self._node.moduleConfig):
            descriptor = source_config.DESCRIPTOR.fields_by_name.get(config_name)
            if descriptor is not None:
                return source_config, descriptor
        self._node._raise_interface_error(  # noqa: SLF001
            f"Error: No valid config with name {config_name}"
        )
        raise AssertionError("Unreachable: _raise_interface_error must raise")

    def _verify_written_config(self, config_name: str, *, staged_bytes: bytes) -> None:
        """Re-request one config section and require the sent values back.

        Local-node writes are fire-and-forget: the send returns without an
        ACK/NAK, so a frame the device rejects or drops (nanopb decode
        overflow, another client holding the single API link) reports success
        while applying nothing. The cached section itself cannot vouch for
        the write — it still holds the staged values until a device response
        replaces them, and an all-default section serializes to the same
        empty bytes as a freshly cleared one — so the section is cleared,
        re-requested, and accepted only when the requested section in the
        correlated response carries exactly the staged bytes. That response
        snapshot remains authoritative for verification even if another
        response subsequently replaces the shared cache. A timed-out verification
        or a refresh request that raises restores the sent values into the
        cache unless the device already repopulated it; a mismatched response
        leaves the device-reported values in place. A routing refusal
        correlated to the read-back raises promptly with its error instead of
        waiting out the timeout.
        """
        if getattr(self._node, "noProto", False):
            logger.warning(
                "Skipping %s write verification because protocol use is disabled"
                " by noProto",
                config_name,
            )
            return
        config_root, descriptor = self._section_root_and_descriptor(config_name)
        response_received = threading.Event()
        response_state_lock = threading.Lock()
        response_bytes: bytes | None = None
        response_failure: str | None = None
        original_handler = self._node.onResponseRequestSettings
        response_field: Literal["get_config_response", "get_module_config_response"] = (
            "get_config_response"
            if config_root is self._node.localConfig
            else "get_module_config_response"
        )

        def _signal_on_response(packet: dict[str, Any]) -> None:
            nonlocal response_bytes, response_failure
            # Capture the correlated payload itself: later requests may replace
            # the shared cache before this verification thread resumes.
            with response_state_lock:
                original_handler(packet)
                decoded = packet.get("decoded")
                routing = decoded.get("routing") if isinstance(decoded, dict) else None
                error_reason = (
                    routing.get("errorReason") if isinstance(routing, dict) else None
                )
                if isinstance(error_reason, str) and error_reason != ERROR_REASON_NONE:
                    # A routing NAK correlated to the read-back is a terminal
                    # refusal: record the cause so the wait fails promptly
                    # instead of polling to the timeout. A routing ACK (NONE)
                    # can precede the data response and stays nonterminal.
                    response_failure = f"routing error {error_reason}"
                    response_received.set()
                    return
                admin = decoded.get("admin") if isinstance(decoded, dict) else None
                raw = admin.get("raw") if isinstance(admin, dict) else None
                if isinstance(raw, admin_pb2.AdminMessage) and raw.HasField(
                    response_field
                ):
                    section = getattr(raw, response_field)
                    if section.HasField(config_name):
                        response_bytes = getattr(
                            section, config_name
                        ).SerializeToString()
                        response_received.set()

        def _restore_sent_values_if_missing() -> None:
            # Section presence, not response-packet arrival, decides authority:
            # a response the applier refused leaves the sent intent as the
            # best-known state, while a populated section is device truth.
            with response_state_lock:
                if not config_root.HasField(config_name):
                    getattr(config_root, config_name).ParseFromString(staged_bytes)

        config_root.ClearField(config_name)
        try:
            self.request_config(descriptor, on_response=_signal_on_response)
            deadline = time.monotonic() + CONFIG_VERIFY_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                with response_state_lock:
                    snapshot = response_bytes
                    failure = response_failure
                if response_received.is_set() and snapshot is not None:
                    if snapshot == staged_bytes:
                        logger.debug(
                            "Verified %s write via device read-back", config_name
                        )
                        return
                    self._node._raise_interface_error(  # noqa: SLF001
                        f"writeConfig verification for {config_name} failed: the"
                        " device reported different values than the staged write"
                    )
                if response_received.is_set() and failure is not None:
                    self._node._raise_interface_error(  # noqa: SLF001
                        f"writeConfig verification for {config_name} failed:"
                        f" {failure}"
                    )
                time.sleep(CONFIG_VERIFY_POLL_INTERVAL_SECONDS)
            self._node._raise_interface_error(  # noqa: SLF001
                f"writeConfig verification for {config_name} failed: the device"
                f" did not report the staged values within"
                f" {CONFIG_VERIFY_TIMEOUT_SECONDS:g} seconds"
            )
        except BaseException:
            # Request failure, timeout, and cancellation preserve sent intent
            # when no response supplied authoritative state.
            _restore_sent_values_if_missing()
            raise
