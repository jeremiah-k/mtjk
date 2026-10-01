"""Admin transport mechanics for ADMIN_APP sends."""

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from meshtastic.admin_response import contract_for_admin_request
from meshtastic.protobuf import admin_pb2, mesh_pb2, portnums_pb2

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)


class _NodeAdminTransportRuntime:
    """Owns admin transport mechanics for ADMIN_APP sends."""

    def __init__(self, node: "Node") -> None:
        self._node = node

    def _resolve_admin_index(self, admin_index: int | None) -> int:
        """Resolve None to auto-detected admin channel index; preserve explicit zero."""
        if admin_index is None:
            return self._node.iface.localNode._get_admin_channel_index()
        return admin_index

    def _send_admin(
        self,
        message: admin_pb2.AdminMessage,
        *,
        want_response: bool = False,
        on_response: Callable[[dict[str, Any]], Any] | None = None,
        admin_index: int | None = None,
        response_wait_attr: str | None = None,
    ) -> mesh_pb2.MeshPacket | None:
        """Send an AdminMessage via iface.sendData with preserved transport semantics.

        Parameters
        ----------
        message : admin_pb2.AdminMessage
            The admin message to send.
        want_response : bool, optional
            Whether to request a response from the device. Default is False.
        on_response : Callable[[dict[str, Any]], Any] | None, optional
            Callback invoked when response is received. Default is None.
        admin_index : int | None, optional
            Channel index for admin messages. None auto-detects. Default is None.
        response_wait_attr : str | None, optional
            Internal request-scoped wait attribute to register before the packet
            is sent. Default is None.

        Returns
        -------
        MeshPacket | None
            The sent packet, or None if the send was skipped (e.g., when noProto is enabled).
            Callers must check for None to avoid applying local state changes when the
            device was not actually updated.
        """
        if self._node.noProto:
            logger.warning(
                "Not sending packet because protocol use is disabled by noProto"
            )
            return None

        resolved_admin_index = self._resolve_admin_index(admin_index)
        logger.debug("adminIndex:%s", resolved_admin_index)

        node_info = self._node.iface._get_or_create_by_num(self._node.nodeNum)
        passkey = node_info.get("adminSessionPassKey")
        outbound_message = admin_pb2.AdminMessage()
        outbound_message.CopyFrom(message)
        if isinstance(passkey, bytes):
            outbound_message.session_passkey = passkey
        elif passkey is not None:
            logger.warning(
                "adminSessionPassKey expected bytes, got %s; ignoring",
                type(passkey).__name__,
            )

        response_matcher = None
        response_feedback_matcher = None
        if on_response is not None and want_response:
            local_node_num = getattr(self._node.iface.localNode, "nodeNum", None)
            contract = contract_for_admin_request(
                outbound_message,
                destination=self._node.nodeNum,
                local_node_num=(
                    local_node_num if isinstance(local_node_num, int) else None
                ),
            )
            if contract is not None:
                response_matcher = contract.matches
                response_feedback_matcher = contract.matches_source

        send_kwargs: dict[str, Any] = {
            "portNum": portnums_pb2.PortNum.ADMIN_APP,
            "wantAck": True,
            "wantResponse": want_response,
            "onResponse": on_response,
            "channelIndex": resolved_admin_index,
            "pkiEncrypted": True,
        }
        if response_wait_attr is not None or response_matcher is not None:
            send_kwargs["responseMatcher"] = response_matcher
            if response_feedback_matcher is not None:
                send_kwargs["responseFeedbackMatcher"] = response_feedback_matcher
            send_kwargs["response_wait_attr"] = response_wait_attr
            return self._node.iface._send_data_with_wait(
                outbound_message,
                self._node.nodeNum,
                **send_kwargs,
            )
        return self._node.iface.sendData(
            outbound_message,
            self._node.nodeNum,
            **send_kwargs,
        )
