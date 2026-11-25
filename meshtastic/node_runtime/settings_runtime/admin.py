"""Generic admin-command session/callback/send policy for command family methods."""

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from meshtastic.node_runtime.admin_wait import (
    WAIT_ATTR_NAK,
    _send_admin_with_ack_scope,
    _wait_for_admin_ack,
)
from meshtastic.node_runtime.ports import _NodePort
from meshtastic.protobuf import admin_pb2, mesh_pb2
from meshtastic.util import toNodeNum

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)


class _NodeAdminCommandRuntime:
    """Owns generic admin-command session/callback/send policy for command family methods."""

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

    def _send_command(
        self,
        message: admin_pb2.AdminMessage,
        *,
        ensure_session_key: bool,
        use_remote_ack_callback: bool,
    ) -> mesh_pb2.MeshPacket | None:
        """Apply shared session/callback/send policy for admin commands."""
        if ensure_session_key:
            self._node.ensureSessionKey()
        on_response = (
            self._select_remote_ack_callback() if use_remote_ack_callback else None
        )
        request = _send_admin_with_ack_scope(
            self._node,
            message,
            scope_ack=on_response is not None,
            onResponse=on_response,
        )
        if on_response is not None and request is not None:
            _wait_for_admin_ack(self._node, request)
        return request

    def send_owner_message(
        self, message: admin_pb2.AdminMessage
    ) -> mesh_pb2.MeshPacket | None:
        """Send set_owner message with historical session and remote-ACK behavior."""
        return self._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=True,
        )

    def exit_simulator(self) -> mesh_pb2.MeshPacket | None:
        """Send exit-simulator admin command."""
        message = admin_pb2.AdminMessage()
        message.exit_simulator = True
        logger.debug("in exitSimulator()")
        return self._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=False,
        )

    def reboot(self, secs: int) -> mesh_pb2.MeshPacket | None:
        """Send reboot command with delayed reboot seconds."""
        message = admin_pb2.AdminMessage()
        message.reboot_seconds = secs
        logger.info("Telling node to reboot in %s seconds", secs)
        return self._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=True,
        )

    def begin_settings_transaction(self) -> mesh_pb2.MeshPacket | None:
        """Send begin-edit-settings command."""
        message = admin_pb2.AdminMessage()
        message.begin_edit_settings = True
        logger.info("Telling node to open a transaction to edit settings")
        return self._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=True,
        )

    def commit_settings_transaction(self) -> mesh_pb2.MeshPacket | None:
        """Send commit-edit-settings command."""
        message = admin_pb2.AdminMessage()
        message.commit_edit_settings = True
        logger.info("Telling node to commit open transaction for editing settings")
        return self._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=True,
        )

    def reboot_ota(self, secs: int) -> mesh_pb2.MeshPacket | None:
        """Send reboot-to-OTA command."""
        message = admin_pb2.AdminMessage()
        message.reboot_ota_seconds = secs
        logger.info("Telling node to reboot to OTA in %s seconds", secs)
        return self._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=True,
        )

    @staticmethod
    def _resolve_ota_hash(
        *,
        ota_file_hash: bytes | None,
        ota_hash: bytes | None,
        legacy_hash: bytes | None,
    ) -> bytes:
        """Resolve one OTA hash input while preserving legacy validation behavior."""
        # Build list of provided hash values first
        provided_hashes: list[bytes] = []
        param_names = ("ota_file_hash", "ota_hash", "legacy_hash")
        for name, value in zip(
            param_names, (ota_file_hash, ota_hash, legacy_hash), strict=False
        ):
            if value is not None:
                if not isinstance(value, bytes):
                    raise TypeError(f"{name} must be bytes")
                provided_hashes.append(value)
        if not provided_hashes:
            raise TypeError("startOTA() missing required argument: 'ota_file_hash'")
        # Deduplicate after validation
        hash_values = set(provided_hashes)
        if len(hash_values) > 1:
            raise ValueError("Conflicting OTA hash arguments provided")
        return hash_values.pop()

    def start_ota(
        self,
        mode: int | None = None,
        ota_file_hash: bytes | None = None,
        *,
        ota_mode: int | None = None,
        ota_hash: bytes | None = None,
        **extra_kwargs: Any,
    ) -> mesh_pb2.MeshPacket | None:
        """Validate OTA args and send ota_request command."""
        if self._node is not self._node.iface.localNode:
            self._port.raise_interface_error("startOTA only possible on local node")

        # COMPAT_STABLE_SHIM: support legacy keyword aliases used by older callers:
        # `ota_mode` -> `mode`, and `ota_hash`/`hash` -> `ota_file_hash`.
        legacy_hash = extra_kwargs.pop("hash", None)
        if extra_kwargs:
            unexpected = ", ".join(sorted(extra_kwargs))
            raise TypeError(
                f"startOTA() got unexpected keyword argument(s): {unexpected}"
            )

        if mode is not None and ota_mode is not None and mode != ota_mode:
            raise ValueError("Conflicting OTA mode arguments provided")
        resolved_mode = mode if mode is not None else ota_mode
        if resolved_mode is None:
            raise TypeError("startOTA() missing required argument: 'mode'")

        resolved_hash = self._resolve_ota_hash(
            ota_file_hash=ota_file_hash,
            ota_hash=ota_hash,
            legacy_hash=legacy_hash,
        )

        message = admin_pb2.AdminMessage()
        message.ota_request.reboot_ota_mode = resolved_mode  # type: ignore[assignment]
        message.ota_request.ota_hash = resolved_hash
        return self._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=False,
        )

    def enter_dfu_mode(self) -> mesh_pb2.MeshPacket | None:
        """Send enter-DFU-mode command."""
        message = admin_pb2.AdminMessage()
        message.enter_dfu_mode_request = True
        logger.info("Telling node to enable DFU mode")
        return self._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=True,
        )

    def shutdown(self, secs: int) -> mesh_pb2.MeshPacket | None:
        """Send shutdown command with delayed shutdown seconds."""
        message = admin_pb2.AdminMessage()
        message.shutdown_seconds = secs
        logger.info("Telling node to shutdown in %s seconds", secs)
        return self._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=True,
        )

    def factory_reset(self, *, full: bool) -> mesh_pb2.MeshPacket | None:
        """Send factory-reset command, preserving full/config split behavior."""
        message = admin_pb2.AdminMessage()
        if full:
            message.factory_reset_device = self._port.factory_reset_request_value
            logger.info("Telling node to factory reset (full device reset)")
        else:
            message.factory_reset_config = self._port.factory_reset_request_value
            logger.info("Telling node to factory reset (config reset)")
        if self._node is self._node.iface.localNode:
            # The CLI waits after factoryReset() returns so it can keep the
            # transport open until firmware accepts the destructive command.
            # Register the ACK/NAK callback before sending: a local simulator or
            # fast serial link can return the routing ACK before the CLI begins
            # its legacy wait, and without a response handler that ACK is not
            # reflected in the compatibility acknowledgment flags.
            self._node.ensureSessionKey()
            return self._port.send_admin(
                message,
                on_response=self._node.onAckNak,
                response_wait_attr=WAIT_ATTR_NAK,
            )
        return self._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=True,
        )

    def _send_node_id_command(
        self,
        *,
        node_id: int | str,
        set_field: Callable[[admin_pb2.AdminMessage, int], None],
    ) -> mesh_pb2.MeshPacket | None:
        """Send one node-id command after toNodeNum conversion."""
        node_num = toNodeNum(node_id)
        message = admin_pb2.AdminMessage()
        set_field(message, node_num)
        return self._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=True,
        )

    def remove_node(self, node_id: int | str) -> mesh_pb2.MeshPacket | None:
        """Send remove-by-nodenum command."""
        return self._send_node_id_command(
            node_id=node_id,
            set_field=lambda message, node_num: setattr(
                message, "remove_by_nodenum", node_num
            ),
        )

    def set_favorite(self, node_id: int | str) -> mesh_pb2.MeshPacket | None:
        """Send set-favorite command."""
        return self._send_node_id_command(
            node_id=node_id,
            set_field=lambda message, node_num: setattr(
                message, "set_favorite_node", node_num
            ),
        )

    def remove_favorite(self, node_id: int | str) -> mesh_pb2.MeshPacket | None:
        """Send remove-favorite command."""
        return self._send_node_id_command(
            node_id=node_id,
            set_field=lambda message, node_num: setattr(
                message, "remove_favorite_node", node_num
            ),
        )

    def set_ignored(self, node_id: int | str) -> mesh_pb2.MeshPacket | None:
        """Send set-ignored command."""
        return self._send_node_id_command(
            node_id=node_id,
            set_field=lambda message, node_num: setattr(
                message, "set_ignored_node", node_num
            ),
        )

    def remove_ignored(self, node_id: int | str) -> mesh_pb2.MeshPacket | None:
        """Send remove-ignored command."""
        return self._send_node_id_command(
            node_id=node_id,
            set_field=lambda message, node_num: setattr(
                message, "remove_ignored_node", node_num
            ),
        )

    def reset_node_db(self) -> mesh_pb2.MeshPacket | None:
        """Send NodeDB reset command."""
        message = admin_pb2.AdminMessage()
        message.nodedb_reset = True
        logger.info("Telling node to reset the NodeDB")
        return self._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=True,
        )
