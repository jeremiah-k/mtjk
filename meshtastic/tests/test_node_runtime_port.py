"""Contracts for the internal Node port used by ``node_runtime`` collaborators.

The port is the single seam between runtime modules and the ``Node`` facade:
every operation must forward to the live facade at call time, so attributes
monkeypatched on the facade are honored, and the facade must create the
adapter exactly once.
"""

from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest

from ..mesh_interface import MeshInterface
from ..node import Node
from ..node_runtime.ports import _NodePort
from ..protobuf import admin_pb2, channel_pb2, mesh_pb2
from ..util import Timeout


@pytest.fixture
def anode(autospec_local_node_iface: Callable[[type[Any]], MagicMock]) -> Node:
    """Provide a real Node over an autospecced local interface."""
    return Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)


@pytest.mark.unit
def test_port_binds_one_facade(anode: Node) -> None:
    """The adapter exposes the facade it was created over."""
    port = _NodePort(anode)
    assert port.timeout is anode._timeout  # noqa: SLF001


@pytest.mark.unit
def test_timeout_property_reads_live_facade(
    anode: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The timeout property resolves on the facade at property access time."""
    replacement = Timeout(maxSecs=1.0)
    monkeypatch.setattr(anode, "_timeout", replacement)
    assert _NodePort(anode).timeout is replacement


@pytest.mark.unit
def test_factory_reset_request_value_matches_facade(anode: Node) -> None:
    """The factory-reset value mirrors the facade's configuration access."""
    assert (
        _NodePort(anode).factory_reset_request_value
        == anode._get_factory_reset_request_value()  # noqa: SLF001
    )


@pytest.mark.unit
def test_raise_interface_error_uses_facade_error_type(
    anode: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error raising goes through the facade seam, honoring facade patches."""
    sentinel = RuntimeError("patched error")

    def _patched_error(message: str) -> None:
        raise sentinel

    monkeypatch.setattr(anode, "_raise_interface_error", _patched_error)
    with pytest.raises(RuntimeError, match="patched error"):
        _NodePort(anode).raise_interface_error("ignored")


@pytest.mark.unit
def test_send_admin_forwards_keyword_arguments(
    anode: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Admin sends forward every argument to the facade seam."""
    packet = mesh_pb2.MeshPacket()
    observed: dict[str, Any] = {}

    def _patched_send_admin(*args: Any, **kwargs: Any) -> mesh_pb2.MeshPacket | None:
        observed["args"] = args
        observed["kwargs"] = kwargs
        return packet

    monkeypatch.setattr(anode, "_send_admin", _patched_send_admin)
    message = admin_pb2.AdminMessage()

    def _on_response(decoded: dict[str, Any]) -> None:
        return None

    result = _NodePort(anode).send_admin(
        message,
        want_response=True,
        on_response=_on_response,
        admin_index=2,
        response_wait_attr="WAIT_ATTR",
    )
    assert result is packet
    assert observed["args"] == (message,)
    assert observed["kwargs"] == {
        "wantResponse": True,
        "onResponse": _on_response,
        "adminIndex": 2,
        "responseWaitAttr": "WAIT_ATTR",
    }


@pytest.mark.unit
def test_send_admin_forwards_only_supplied_arguments(
    anode: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unsupplied admin-send arguments are not forwarded to the facade seam.

    Historical call sites pass only the arguments they need, and real
    functions patching the facade seam may accept exactly that call shape.
    """

    def _capture_admin_index(
        admin_message: admin_pb2.AdminMessage, *, adminIndex: int
    ) -> mesh_pb2.MeshPacket | None:
        assert adminIndex == 2
        return mesh_pb2.MeshPacket()

    monkeypatch.setattr(anode, "_send_admin", _capture_admin_index)
    result = _NodePort(anode).send_admin(admin_pb2.AdminMessage(), admin_index=2)
    assert result is not None


@pytest.mark.unit
def test_write_channel_snapshot_forwards_only_supplied_arguments(
    anode: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Snapshot writes without an admin index forward the channel alone."""

    def _capture_write(channel_to_write: channel_pb2.Channel) -> None:
        assert channel_to_write.index == 0

    monkeypatch.setattr(anode, "_write_channel_snapshot", _capture_write)
    _NodePort(anode).write_channel_snapshot(channel_pb2.Channel())


@pytest.mark.unit
def test_execute_with_node_db_lock_returns_func_result(anode: Node) -> None:
    """The node-database lock helper returns the wrapped callable's result."""
    assert _NodePort(anode).execute_with_node_db_lock(lambda: "value") == "value"


@pytest.mark.unit
def test_write_channel_snapshot_forwards_admin_index(
    anode: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Channel snapshot writes forward the channel and admin index."""
    observed: dict[str, Any] = {}

    def _patched_write(*args: Any, **kwargs: Any) -> None:
        observed["args"] = args
        observed["kwargs"] = kwargs

    monkeypatch.setattr(anode, "_write_channel_snapshot", _patched_write)
    snapshot = channel_pb2.Channel()
    _NodePort(anode).write_channel_snapshot(snapshot, admin_index=1)
    assert observed["args"] == (snapshot,)
    assert observed["kwargs"] == {"adminIndex": 1}


@pytest.mark.unit
def test_metadata_operations_forward_to_facade(
    anode: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Metadata reporting operations resolve on the facade at call time."""
    port = _NodePort(anode)
    emitted: list[str] = []
    signalled: list[bool] = []
    stored: list[mesh_pb2.DeviceMetadata] = []
    monkeypatch.setattr(anode, "_emit_metadata_line", emitted.append)
    monkeypatch.setattr(
        anode, "_signal_metadata_stdout_event", lambda: signalled.append(True)
    )
    monkeypatch.setattr(anode, "_set_metadata_snapshot", stored.append)
    port.emit_metadata_line("fw: 1.2.3")
    port.signal_metadata_stdout_event()
    metadata = mesh_pb2.DeviceMetadata()
    port.set_metadata_snapshot(metadata)
    assert emitted == ["fw: 1.2.3"]
    assert signalled == [True]
    assert stored == [metadata]


@pytest.mark.unit
def test_request_channel_forwards_channel_number(
    anode: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Channel requests forward the channel number positionally."""
    packet = mesh_pb2.MeshPacket()

    def _patched_request(channel_num: int) -> mesh_pb2.MeshPacket | None:
        assert channel_num == 3
        return packet

    monkeypatch.setattr(anode, "_request_channel", _patched_request)
    assert _NodePort(anode).request_channel(3) is packet


@pytest.mark.unit
def test_node_creates_the_port_once(anode: Node) -> None:
    """The facade lazily creates one shared adapter."""
    first = anode._node_port  # noqa: SLF001
    second = anode._node_port  # noqa: SLF001
    assert first is second
    assert first._node is anode  # noqa: SLF001
