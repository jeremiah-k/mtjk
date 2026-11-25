"""Admin session runtime for session-key readiness checks."""

import logging
from typing import TYPE_CHECKING

from meshtastic.node_runtime.ports import _NodePort
from meshtastic.protobuf import admin_pb2

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)

TIMEOUT_SESSION_KEY_MSG = "Timeout waiting for adminSessionPassKey after request"


class _AdminSessionPassKeyProbe:
    """Probe to wait for adminSessionPassKey in node data.

    Parameters
    ----------
    node : Node
        The node instance to probe for session key presence.
    """

    def __init__(self, node: "Node") -> None:
        self._node = node

    @property
    def is_set(self) -> bool:
        """Check if adminSessionPassKey is present in node data.

        Returns
        -------
        bool
            True if adminSessionPassKey is present and non-None.
        """
        node_info = self._node.iface._get_or_create_by_num(self._node.nodeNum)
        return node_info.get("adminSessionPassKey") is not None


class _NodeAdminSessionRuntime:
    """Owns admin-session readiness checks and session-key request behavior."""

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

    def _ensure_session_key(self, *, admin_index: int | None = None) -> None:
        """Ensure session key is present, preserving historical noProto behavior.

        Parameters
        ----------
        admin_index : int | None, optional
            Admin index to use for the session-key request, by default None.

        Raises
        ------
        TimeoutError
            If the session key is not received within the timeout period.
        """
        if self._node.noProto:
            logger.warning(
                "Not ensuring session key, because protocol use is disabled by noProto"
            )
            return
        if (
            self._node.iface._get_or_create_by_num(self._node.nodeNum).get(
                "adminSessionPassKey"
            )
            is None
        ):
            self._node.requestConfig(
                admin_pb2.AdminMessage.SESSIONKEY_CONFIG,
                adminIndex=admin_index,
            )
            # Wait for adminSessionPassKey to be populated
            probe = _AdminSessionPassKeyProbe(self._node)
            key_received = self._port.timeout.waitForSet(probe, attrs=("is_set",))
            if not key_received:
                raise TimeoutError(TIMEOUT_SESSION_KEY_MSG)
