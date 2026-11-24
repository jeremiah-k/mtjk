"""SetOwner validation/truncation/message-build and send orchestration."""

import logging
from typing import TYPE_CHECKING

from meshtastic.node_runtime.ports import _NodePort
from meshtastic.node_runtime.shared import (
    EMPTY_LONG_NAME_MSG,
    EMPTY_SHORT_NAME_MSG,
    MAX_LONG_NAME_LEN,
    MAX_SHORT_NAME_LEN,
)
from meshtastic.protobuf import admin_pb2, mesh_pb2

if TYPE_CHECKING:
    from meshtastic.node import Node
    from meshtastic.node_runtime.settings_runtime.admin import (
        _NodeAdminCommandRuntime,
    )

logger = logging.getLogger(__name__)


class _NodeOwnerProfileRuntime:
    """Owns owner-profile validation, truncation, message build, and sending."""

    def __init__(
        self,
        node: "Node",
        port: _NodePort | None = None,
        *,
        admin_command_runtime: "_NodeAdminCommandRuntime",
    ) -> None:
        """Initialize the runtime with its facade and collaborator port.

        Parameters
        ----------
        node : Node
            The facade whose public state the runtime reads.
        port : _NodePort | None
            Narrow port over the facade's internals; derived from ``node``
            when omitted.
        admin_command_runtime : _NodeAdminCommandRuntime
            Shared admin-command send policy owner.
        """
        self._node = node
        self._port = port if port is not None else _NodePort(node)
        self._admin_command_runtime = admin_command_runtime

    def set_owner(
        self,
        *,
        long_name: str | None = None,
        short_name: str | None = None,
        is_licensed: bool = False,
        is_unmessagable: bool | None = None,
    ) -> mesh_pb2.MeshPacket | None:
        """Apply owner-profile validation, truncation, and send policy.

        Parameters
        ----------
        long_name : str | None, optional
            The long name to set. Will be stripped and truncated if necessary.
        short_name : str | None, optional
            The short name to set. Will be stripped and truncated if necessary.
        is_licensed : bool, default False
            Whether the node operator is licensed.
        is_unmessagable : bool | None, optional
            Whether the node should be marked as unmessagable.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The mesh packet returned from sending the owner message, or None.
        """
        logger.debug("in setOwner nodeNum:%s", self._node.nodeNum)
        message = admin_pb2.AdminMessage()

        if long_name is not None:
            long_name = long_name.strip()
            # Validate that long_name is not empty or whitespace-only
            if not long_name:
                self._port.raise_interface_error(EMPTY_LONG_NAME_MSG)
            if len(long_name) > MAX_LONG_NAME_LEN:
                long_name = long_name[:MAX_LONG_NAME_LEN]
                logger.warning(
                    "Long name is longer than %s characters; truncating.",
                    MAX_LONG_NAME_LEN,
                )
            message.set_owner.long_name = long_name

        if short_name is not None:
            short_name = short_name.strip()
            # Validate that short_name is not empty or whitespace-only
            if not short_name:
                self._port.raise_interface_error(EMPTY_SHORT_NAME_MSG)
            if len(short_name) > MAX_SHORT_NAME_LEN:
                short_name = short_name[:MAX_SHORT_NAME_LEN]
                logger.warning(
                    "Short name is longer than %s characters; truncating.",
                    MAX_SHORT_NAME_LEN,
                )
            message.set_owner.short_name = short_name

        message.set_owner.is_licensed = is_licensed
        if is_unmessagable is not None:
            message.set_owner.is_unmessagable = is_unmessagable

        # Note: These debug lines are used in unit tests
        logger.debug("p.set_owner.long_name_set:%s", bool(message.set_owner.long_name))
        logger.debug(
            "p.set_owner.short_name_set:%s", bool(message.set_owner.short_name)
        )
        logger.debug("p.set_owner.is_licensed:%s", message.set_owner.is_licensed)
        logger.debug(
            "p.set_owner.is_unmessagable:%s",
            message.set_owner.is_unmessagable,
        )
        return self._admin_command_runtime.send_owner_message(message)
