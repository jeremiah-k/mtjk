"""Internal port through which ``node_runtime`` modules reach the Node facade.

Runtime collaborators must not touch ``Node`` private attributes directly.
They receive a :class:`_NodePort` and perform the operations they need
(sending admin messages, raising facade errors, waiting on device state,
writing channel snapshots, and metadata reporting) through it. The adapter
forwards every operation to the live ``Node`` at call time, so attributes
monkeypatched on the facade are honored.

Every ``Node``-private attribute access used by ``node_runtime`` modules is
concentrated in this module.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from typing import TYPE_CHECKING, Any, NoReturn, TypedDict, TypeVar

from meshtastic.protobuf import admin_pb2, channel_pb2, mesh_pb2
from meshtastic.util import Timeout

if TYPE_CHECKING:
    from meshtastic.node import Node

_ResultT = TypeVar("_ResultT")


class _SendAdminKwargs(TypedDict, total=False):
    """Keyword arguments forwarded to the facade's admin-send seam."""

    wantResponse: bool
    onResponse: Callable[[dict[str, Any]], Any] | None
    adminIndex: int | None
    responseWaitAttr: str | None


class _Omitted(Enum):
    VALUE = 0


class _NodePort:
    """Expose the Node capabilities that ``node_runtime`` collaborators use.

    The adapter is created once per ``Node`` and handed to every runtime
    collaborator in place of the facade itself.

    Attributes
    ----------
    _node : Node
        The live facade the operations forward to.
    """

    def __init__(self, node: "Node") -> None:
        """Bind the adapter to a facade.

        Parameters
        ----------
        node : Node
            The live facade the operations forward to.
        """
        self._node = node

    @property
    def timeout(self) -> Timeout:
        """Return the facade's operation-wait timeout helper.

        Returns
        -------
        Timeout
            The facade timeout used by request/response waits.
        """
        return self._node._timeout  # noqa: SLF001

    @property
    def factory_reset_request_value(self) -> int:
        """Return the admin request value that triggers a factory reset.

        Returns
        -------
        int
            The sentinel value carried by factory-reset admin messages.
        """
        return self._node._get_factory_reset_request_value()  # noqa: SLF001

    def raise_interface_error(self, message: str) -> NoReturn:
        """Raise the facade's interface error with the provided message.

        Parameters
        ----------
        message : str
            The error message carried by the raised exception.

        Raises
        ------
        MeshInterface.MeshInterfaceError
            Always raised with the provided message.
        """
        self._node._raise_interface_error(message)  # noqa: SLF001

    @property
    def admin_sender(self) -> Callable[..., mesh_pb2.MeshPacket | None]:
        """Resolve the live sender for ACK-scope signature inspection."""
        return self._node._send_admin  # noqa: SLF001

    def send_admin(
        self,
        admin_message: admin_pb2.AdminMessage,
        want_response: bool | _Omitted = _Omitted.VALUE,
        on_response: Callable[[dict[str, Any]], Any] | None | _Omitted = _Omitted.VALUE,
        admin_index: int | None | _Omitted = _Omitted.VALUE,
        response_wait_attr: str | None | _Omitted = _Omitted.VALUE,
    ) -> mesh_pb2.MeshPacket | None:
        """Send an AdminMessage through the facade's admin channel.

        Parameters
        ----------
        admin_message : admin_pb2.AdminMessage
            Message to send; a session passkey may be attached.
        want_response : bool
            Request a response from the recipient when True.
        on_response : Callable[[dict[str, Any]], Any] | None
            Optional callback invoked with the received response packet.
        admin_index : int | None
            Channel index to use; when None the node's configured admin
            channel is used.
        response_wait_attr : str | None
            Optional request-scoped wait attribute registered before
            transmission.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The MeshPacket returned by the send operation, or ``None`` when
            sending was skipped because protocol use is disabled.

        Notes
        -----
        Only explicitly supplied arguments are forwarded, including None and False, so the
        facade seam observes the exact call shape the runtime caller wrote.
        """
        forwarded: _SendAdminKwargs = {}
        if not isinstance(want_response, _Omitted):
            forwarded["wantResponse"] = want_response
        if not isinstance(on_response, _Omitted):
            forwarded["onResponse"] = on_response
        if not isinstance(admin_index, _Omitted):
            forwarded["adminIndex"] = admin_index
        if not isinstance(response_wait_attr, _Omitted):
            forwarded["responseWaitAttr"] = response_wait_attr
        return self.admin_sender(admin_message, **forwarded)

    def execute_with_node_db_lock(self, func: Callable[[], _ResultT]) -> _ResultT:
        """Execute ``func`` while holding the interface node-database lock.

        Parameters
        ----------
        func : Callable[[], _ResultT]
            Zero-argument callable to run under the lock when available.

        Returns
        -------
        _ResultT
            Whatever ``func`` returns.
        """
        return self._node._execute_with_node_db_lock(func)  # noqa: SLF001

    def write_channel_snapshot(
        self,
        channel_to_write: channel_pb2.Channel,
        admin_index: int | None | _Omitted = _Omitted.VALUE,
    ) -> None:
        """Write a pre-built channel snapshot to the device.

        Parameters
        ----------
        channel_to_write : channel_pb2.Channel
            Snapshot payload to send via ``set_channel``.
        admin_index : int | None
            Admin channel index to use; when None the node's configured
            admin channel is used.
        """
        if not isinstance(admin_index, _Omitted):
            self._node._write_channel_snapshot(  # noqa: SLF001
                channel_to_write,
                adminIndex=admin_index,
            )
            return
        self._node._write_channel_snapshot(channel_to_write)  # noqa: SLF001

    def request_channel(self, channel_num: int) -> mesh_pb2.MeshPacket | None:
        """Request one channel's settings from the device.

        Parameters
        ----------
        channel_num : int
            Channel index to request.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The request packet, or ``None`` when nothing was sent.
        """
        return self._node._request_channel(channel_num)  # noqa: SLF001

    def set_metadata_snapshot(self, metadata_snapshot: mesh_pb2.DeviceMetadata) -> None:
        """Persist a device metadata snapshot on the interface.

        Parameters
        ----------
        metadata_snapshot : mesh_pb2.DeviceMetadata
            Snapshot to store under the node-database lock when available.
        """
        self._node._set_metadata_snapshot(metadata_snapshot)  # noqa: SLF001

    def emit_metadata_line(self, line: str) -> None:
        """Log a metadata line and mirror it to redirected stdout.

        Parameters
        ----------
        line : str
            Formatted metadata line.
        """
        self._node._emit_metadata_line(line)  # noqa: SLF001

    def signal_metadata_stdout_event(self) -> None:
        """Signal redirected-stdout metadata waiters of a terminal response."""
        self._node._signal_metadata_stdout_event()  # noqa: SLF001
