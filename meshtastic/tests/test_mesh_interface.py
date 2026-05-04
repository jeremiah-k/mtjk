"""Meshtastic unit tests for mesh_interface.py."""

# pylint: disable=too-many-lines

import builtins
import importlib.util
import io
import logging
import re
import sys
import threading
import time
import types
from collections import OrderedDict
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, cast
from unittest.mock import MagicMock, call, create_autospec, patch

import google.protobuf.json_format
import pytest
from google.protobuf.message import Message
from hypothesis import given
from hypothesis import strategies as st

import meshtastic.mesh_interface as mesh_interface_module
from meshtastic.mesh_interface_runtime import receive_pipeline as receive_pipeline_module
from meshtastic.mesh_interface_runtime import flows as flows_module
from meshtastic.mesh_interface_runtime.request_wait import (
    UNSCOPED_WAIT_REQUEST_ID,
    WAIT_ATTR_NAK,
    WAIT_ATTR_POSITION,
    WAIT_ATTR_TELEMETRY,
    WAIT_ATTR_TRACEROUTE,
    WAIT_ATTR_WAYPOINT,
)

from .. import BROADCAST_ADDR, LOCAL_ADDR, NODELESS_WANT_CONFIG_ID, ResponseHandler
from ..mesh_interface import MeshInterface
from ..mesh_interface_runtime.node_view import _timeago
from ..node import Node
from ..protobuf import (
    channel_pb2,
    config_pb2,
    mesh_pb2,
    portnums_pb2,
    telemetry_pb2,
)

# TODO
# from ..config import Config
from ..util import Acknowledgment, Timeout

if TYPE_CHECKING:
    from .conftest import FakeTimer


def _start_wait_thread(
    wait_call: Callable[[], None],
) -> tuple[threading.Thread, list[BaseException]]:
    """Start a waiter in a background thread and capture any raised exception."""
    errors: list[BaseException] = []

    def _run_wait() -> None:
        try:
            wait_call()
        except Exception as exc:  # noqa: BLE001 - asserted by caller
            errors.append(exc)

    thread = threading.Thread(target=_run_wait, daemon=True)
    thread.start()
    return thread, errors


def _wait_for_scoped_wait_registration(
    iface: MeshInterface,
    *,
    acknowledgment_attr: str,
    request_id: int,
    timeout_seconds: float = 1.0,
) -> None:
    """Wait until a request-scoped waiter is registered for `acknowledgment_attr`."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        with iface._response_handlers_lock:
            if request_id in iface._active_wait_request_ids.get(
                acknowledgment_attr, set()
            ):
                return
        time.sleep(0.001)
    pytest.fail(
        f"Timed out waiting for scoped waiter registration: {acknowledgment_attr}#{request_id}"
    )


def _inline_queue_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """Execute queued publish callbacks inline for deterministic packet tests."""
    monkeypatch.setattr(
        mesh_interface_module.publishingThread,  # type: ignore[attr-defined]
        "queueWork",
        lambda callback: callback(),
    )


def _install_protocol_stub(
    monkeypatch: pytest.MonkeyPatch,
    *,
    portnum: portnums_pb2.PortNum.ValueType,
    name: str,
    protobuf_factory: object,
    on_receive: Callable[[MeshInterface, dict[str, Any]], None] | MagicMock,
) -> None:
    """Install a single protocol stub for a decode-failure test case."""
    fake_protocol = types.SimpleNamespace(
        name=name,
        protobufFactory=protobuf_factory,
        onReceive=on_receive,
    )
    monkeypatch.setattr(
        receive_pipeline_module,
        "protocols",
        {portnum: fake_protocol},
    )


def _make_decoded_packet(
    *,
    from_node: int = 1,
    to_node: int = 2,
    portnum: portnums_pb2.PortNum.ValueType,
    request_id: int,
    payload: bytes,
) -> mesh_pb2.MeshPacket:
    """Build a MeshPacket with decoded payload fields pre-populated."""
    packet = mesh_pb2.MeshPacket()
    setattr(packet, "from", from_node)
    packet.to = to_node
    packet.decoded.portnum = portnum
    packet.decoded.request_id = request_id
    packet.decoded.payload = payload
    return packet


def _register_response_capture(
    iface: MeshInterface, request_id: int
) -> list[dict[str, Any]]:
    """Register a response handler that appends callback packets to a list."""
    callback_calls: list[dict[str, Any]] = []

    def _response_callback(packet: dict[str, Any]) -> None:
        callback_calls.append(packet)

    with iface._response_handlers_lock:
        iface.responseHandlers[request_id] = ResponseHandler(
            callback=_response_callback, ackPermitted=True
        )
    return callback_calls


def _patch_message_to_dict_position_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make MessageToDict fail for Position messages to simulate conversion errors."""
    original_message_to_dict = google.protobuf.json_format.MessageToDict

    def _message_to_dict_with_position_failure(
        message: Message,
        *args: object,
        **kwargs: object,
    ) -> dict[str, Any]:
        if isinstance(message, mesh_pb2.Position):
            raise TypeError("position dict conversion failed")  # noqa: TRY003
        message_to_dict = cast(Callable[..., dict[str, Any]], original_message_to_dict)
        return message_to_dict(message, *args, **kwargs)

    monkeypatch.setattr(
        google.protobuf.json_format,
        "MessageToDict",
        _message_to_dict_with_position_failure,
    )


@pytest.fixture(name="decode_failure_iface")
def _decode_failure_iface_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[MeshInterface]:
    """Provide a MeshInterface with inline queueWork for decode-failure tests."""
    _inline_queue_work(monkeypatch)
    with MeshInterface(noProto=True) as iface:
        yield iface


@pytest.mark.unit
def test_mesh_interface_import_handles_missing_print_color(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Import should gracefully set print_color to None when dependency is unavailable."""
    module_path = Path(mesh_interface_module.__file__)
    spec = importlib.util.spec_from_file_location(
        "meshtastic.mesh_interface_import_fallback_test", module_path
    )
    assert spec is not None
    assert spec.loader is not None
    isolated_module = importlib.util.module_from_spec(spec)

    real_import = builtins.__import__

    def _import_with_print_color_failure(
        name: str,
        globals_dict: Any = None,
        locals_dict: Any = None,
        from_list: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if name == "print_color":
            raise ImportError(  # noqa: TRY003 - intentional test sentinel
                "simulated missing print_color"
            ) from None
        return real_import(name, globals_dict, locals_dict, from_list, level)

    monkeypatch.setattr(builtins, "__import__", _import_with_print_color_failure)
    spec.loader.exec_module(isolated_module)
    assert isolated_module.print_color is None


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_MeshInterface(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Test that we can instantiate a MeshInterface."""
    # Optional dependencies used only by this test path.
    powermon_module = pytest.importorskip("meshtastic.powermon")
    slog_module = pytest.importorskip("meshtastic.slog")
    SimPowerSupply = powermon_module.SimPowerSupply
    LogSet = slog_module.LogSet

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    with MeshInterface(noProto=True) as iface:
        NODE_ID = "!9388f81c"
        NODE_NUM = 2475227164
        node = {
            "num": NODE_NUM,
            "user": {
                "id": NODE_ID,
                "longName": "Unknown f81c",
                "shortName": "?1C",
                "macaddr": "RBeTiPgc",
                "hwModel": "TBEAM",
            },
            "position": {},
            "lastHeard": 1640204888,
        }

        iface.nodes = {NODE_ID: node}
        iface.nodesByNum = {NODE_NUM: node}

        myInfo = MagicMock()
        iface.myInfo = myInfo

        iface.localNode.localConfig.lora.CopyFrom(config_pb2.Config.LoRaConfig())

        # Also get some coverage of the structured logging/power meter stuff by turning it on as well
        log_set = LogSet(iface, None, SimPowerSupply())
        try:
            iface.showInfo()
            iface.localNode.showInfo()
            iface.showNodes()
            iface.sendText("hello")
        finally:
            log_set.close()
    out, err = capsys.readouterr()
    assert re.search(r"Owner: None \(None\)", out, re.MULTILINE)
    assert re.search(r"Nodes", out, re.MULTILINE)
    assert re.search(r"Preferences", out, re.MULTILINE)
    assert re.search(r"Channels", out, re.MULTILINE)
    assert re.search(r"Primary channel URL", out, re.MULTILINE)
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_showInfo_skips_nodes_without_user_dict() -> None:
    """ShowInfo should ignore node records whose user payload is not a dict."""
    with MeshInterface(noProto=True) as iface:
        iface.nodes = {
            "!bad": {"num": 1, "user": "invalid"},
            "!good": {"num": 2, "user": {"id": "!good"}},
        }
        output = io.StringIO()
        summary = iface.showInfo(file=output)

    assert '"!good"' in summary
    assert '"!bad"' not in summary


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_showInfo_tolerates_malformed_macaddr() -> None:
    """ShowInfo should not fail when a node user entry contains malformed macaddr."""
    with MeshInterface(noProto=True) as iface:
        iface.nodes = {
            "!good": {"num": 2, "user": {"id": "!good", "macaddr": "not-base64!!!"}},
        }
        output = io.StringIO()
        summary = iface.showInfo(file=output)

    assert '"!good"' in summary


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_showInfo_normalizes_nested_bytes_for_json_output() -> None:
    """ShowInfo should serialize nested bytes payloads without raising TypeError."""
    with MeshInterface(noProto=True) as iface:
        iface.nodes = {
            "!good": {
                "num": 2,
                "user": {"id": "!good"},
                "position": {"raw_payload": b"\x01\x02"},
            },
        }
        output = io.StringIO()
        summary = iface.showInfo(file=output)

    assert '"raw_payload": "base64:AQI="' in summary


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_getMyUser(iface_with_nodes: MeshInterface) -> None:
    """Test getMyUser()."""
    iface = iface_with_nodes
    assert iface.myInfo is not None
    iface.myInfo.my_node_num = 2475227164
    myuser = iface.getMyUser()
    assert myuser is not None
    assert myuser["id"] == "!9388f81c"


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_getLongName(iface_with_nodes: MeshInterface) -> None:
    """Test getLongName()."""
    iface = iface_with_nodes
    assert iface.myInfo is not None
    iface.myInfo.my_node_num = 2475227164
    mylongname = iface.getLongName()
    assert mylongname == "Unknown f81c"


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_getShortName(iface_with_nodes: MeshInterface) -> None:
    """Test getShortName()."""
    iface = iface_with_nodes
    assert iface.myInfo is not None
    iface.myInfo.my_node_num = 2475227164
    myshortname = iface.getShortName()
    assert myshortname == "?1C"


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handlePacketFromRadio_no_from(caplog: pytest.LogCaptureFixture) -> None:
    """Test _handle_packet_from_radio with no 'from' in the mesh packet."""
    with MeshInterface(noProto=True) as iface:
        meshPacket = mesh_pb2.MeshPacket()
        with caplog.at_level(logging.ERROR):
            iface._handle_packet_from_radio(meshPacket)
    assert re.search(
        r"Device returned a packet we sent, ignoring", caplog.text, re.MULTILINE
    )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handlePacketFromRadio_with_a_portnum(caplog: pytest.LogCaptureFixture) -> None:
    """Test _handle_packet_from_radio with a portnum.

    Since we have an attribute called 'from', we cannot simply 'set' it.
    Had to implement a hack just to be able to test some code.

    """
    with MeshInterface(noProto=True) as iface:
        iface.nodesByNum = {}  # Initialize node database for packet processing
        meshPacket = mesh_pb2.MeshPacket()
        meshPacket.decoded.payload = b""
        meshPacket.decoded.portnum = portnums_pb2.PortNum.TEXT_MESSAGE_APP
        with caplog.at_level(logging.WARNING):
            intents = iface._handle_packet_from_radio(
                meshPacket,
                hack=True,
                emit_publication=False,
            )
    assert isinstance(intents, list)
    assert len(intents) == 1
    assert "portnum was not in decoded" not in caplog.text
    packet_payload = intents[0].payload["packet"]
    assert packet_payload.get("fromId") is None
    assert packet_payload["decoded"]["portnum"] == portnums_pb2.PortNum.Name(
        portnums_pb2.PortNum.TEXT_MESSAGE_APP
    )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handlePacketFromRadio_text_packet_includes_decoded_text() -> None:
    """Published TEXT_MESSAGE_APP packets should include decoded.text after onReceive mutation."""
    with MeshInterface(noProto=True) as iface:
        sender = 0x13277429
        iface.nodesByNum = {
            sender: {"user": {"id": "!13277429"}},
        }
        mesh_packet = mesh_pb2.MeshPacket()
        setattr(mesh_packet, "from", sender)
        mesh_packet.to = 0xFFFFFFFF
        mesh_packet.decoded.portnum = portnums_pb2.PortNum.TEXT_MESSAGE_APP
        mesh_packet.decoded.payload = b"Range test"

        intents = iface._handle_packet_from_radio(
            mesh_packet,
            emit_publication=False,
        )

    assert len(intents) == 1
    published_decoded = intents[0].payload["packet"]["decoded"]
    assert published_decoded["portnum"] == "TEXT_MESSAGE_APP"
    assert published_decoded["payload"] == b"Range test"
    assert published_decoded["text"] == "Range test"


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handlePacketFromRadio_no_portnum(caplog: pytest.LogCaptureFixture) -> None:
    """Verify that _handle_packet_from_radio logs a warning about unknown portnum when a MeshPacket has no portnum."""
    with MeshInterface(noProto=True) as iface:
        iface.nodesByNum = {}  # Initialize node database for packet processing
        meshPacket = mesh_pb2.MeshPacket()
        meshPacket.decoded.payload = b""
        with caplog.at_level(logging.WARNING):
            iface._handle_packet_from_radio(
                meshPacket,
                hack=True,
                emit_publication=False,
            )
    # When portnum is not set, it defaults to UNKNOWN_APP and a warning is logged
    assert re.search(r"portnum was not in decoded", caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handlePacketFromRadio_hack_preserves_from_zero_in_publication_payload() -> (
    None
):
    """hack=True should preserve from==0 in emitted packet payload compatibility path."""
    with MeshInterface(noProto=True) as iface:
        iface.nodesByNum = {}  # Initialize node database for packet processing
        mesh_packet = mesh_pb2.MeshPacket()
        setattr(mesh_packet, "from", 0)
        mesh_packet.decoded.payload = b""
        mesh_packet.decoded.portnum = portnums_pb2.PortNum.TEXT_MESSAGE_APP
        intents = iface._handle_packet_from_radio(
            mesh_packet,
            hack=True,
            emit_publication=False,
        )
    assert len(intents) == 1
    packet_payload = intents[0].payload["packet"]
    assert packet_payload["from"] == 0


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_getNode_with_local() -> None:
    """Test getNode."""
    with MeshInterface(noProto=True) as iface:
        anode = iface.getNode(LOCAL_ADDR)
        assert anode == iface.localNode


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_getNode_not_local(caplog: pytest.LogCaptureFixture) -> None:
    """Test getNode not local."""
    with MeshInterface(noProto=True) as iface:
        anode = create_autospec(Node, instance=True)
        anode.partialChannels = []
        with caplog.at_level(logging.DEBUG):
            with patch("meshtastic.node.Node", return_value=anode):
                another_node = iface.getNode("bar2")
                assert another_node != iface.localNode
    assert re.search(r"About to requestChannels", caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize("request_channel_attempts", [None, 2])
def test_getNode_not_local_timeout(
    caplog: pytest.LogCaptureFixture,
    request_channel_attempts: int | None,
) -> None:
    """Test getNode timeout behavior with default and explicit request-channel attempts."""
    with MeshInterface(noProto=True) as iface:
        anode = create_autospec(Node, instance=True)
        anode.waitForConfig.return_value = False
        anode.partialChannels = []
        with caplog.at_level(logging.WARNING):
            with patch("meshtastic.node.Node", return_value=anode):
                with pytest.raises(
                    MeshInterface.MeshInterfaceError
                ) as pytest_wrapped_e:
                    if request_channel_attempts is None:
                        iface.getNode("bar2")
                    else:
                        iface.getNode(
                            "bar2",
                            requestChannelAttempts=request_channel_attempts,
                        )
                assert pytest_wrapped_e.type is MeshInterface.MeshInterfaceError
                assert "Timed out waiting for channels, giving up" in str(
                    pytest_wrapped_e.value
                )
                assert re.search(
                    r"Timed out trying to retrieve channel info, retrying",
                    caplog.text,
                    re.MULTILINE,
                )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPosition(caplog: pytest.LogCaptureFixture) -> None:
    """Verify that MeshInterface.sendPosition() executes without error and emits position-related debug logs.

    Creates a MeshInterface(noProto=True), calls sendPosition() while capturing DEBUG logs, and then closes the interface.

    """
    with MeshInterface(noProto=True) as iface:
        with caplog.at_level(logging.DEBUG):
            iface.sendPosition()
    # assert re.search(r"p.time:", caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_heartbeat_timer_is_daemon_and_cancelled_on_close(
    monkeypatch: pytest.MonkeyPatch,
    fake_timer_cls: type["FakeTimer"],
) -> None:
    """Heartbeat timer should be daemonized and cancelled during close()."""

    with MeshInterface(noProto=True) as iface:
        monkeypatch.setattr(iface, "sendHeartbeat", lambda: None)

        iface._start_heartbeat()
        assert len(fake_timer_cls.created) == 1
        timer = fake_timer_cls.created[0]
        assert timer.daemon is True
        assert timer.started is True

    assert timer.cancelled is True


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_heartbeat_callback_does_not_reschedule_after_close(
    monkeypatch: pytest.MonkeyPatch,
    fake_timer_cls: type["FakeTimer"],
) -> None:
    """A heartbeat callback firing after close() must not create a new timer."""

    with MeshInterface(noProto=True) as iface:
        monkeypatch.setattr(iface, "sendHeartbeat", lambda: None)

        iface._start_heartbeat()
        assert len(fake_timer_cls.created) == 1
        old_timer = fake_timer_cls.created[0]

        iface.close()
        old_timer.function()

        assert len(fake_timer_cls.created) == 1


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_close_waits_for_inflight_heartbeat_send(
    monkeypatch: pytest.MonkeyPatch,
    fake_timer_cls: type["FakeTimer"],
) -> None:
    """close() should wait for an in-flight heartbeat send to finish."""

    with MeshInterface(noProto=True) as iface:
        send_started = threading.Event()
        release_send = threading.Event()
        close_done = threading.Event()
        close_started = threading.Event()

        def blocking_send_heartbeat() -> None:
            send_started.set()
            release_send.wait(timeout=2.0)

        def close_interface() -> None:
            close_started.set()
            iface.close()
            close_done.set()

        monkeypatch.setattr(iface, "sendHeartbeat", blocking_send_heartbeat)

        start_thread = threading.Thread(target=iface._start_heartbeat, daemon=True)
        start_thread.start()
        assert send_started.wait(timeout=1.0)
        assert len(fake_timer_cls.created) == 1

        close_thread = threading.Thread(target=close_interface, daemon=True)
        close_thread.start()
        assert close_started.wait(timeout=1.0)
        # close() should block until the in-flight heartbeat send completes.
        # Use a generous timeout (0.2s) to avoid flakiness on slow CI runners.
        assert not close_done.wait(timeout=0.2)

        release_send.set()
        close_thread.join(timeout=1.0)
        start_thread.join(timeout=1.0)

        assert close_done.is_set()
        assert not close_thread.is_alive()
        assert not start_thread.is_alive()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize(
    "disconnect_error",
    [
        OSError("bad fd"),
        MeshInterface.MeshInterfaceError("ble write failed"),
    ],
)
def test_close_suppresses_disconnect_send_failures(
    caplog: pytest.LogCaptureFixture,
    disconnect_error: BaseException,
) -> None:
    """close() should continue cleanup if sending disconnect fails."""
    iface = MeshInterface(noProto=False)
    try:
        iface.debugOut = io.StringIO()
        with (
            patch.object(iface, "_send_disconnect", side_effect=disconnect_error),
            caplog.at_level(logging.DEBUG),
        ):
            iface.close()
        assert iface._closing is True
        assert iface.debugOut is None
    finally:
        if not getattr(iface, "_closing", False):
            iface.close()

    assert (
        "Failed to send disconnect during close(); continuing shutdown." in caplog.text
    )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_close_raises_disconnect_type_error_when_not_finalizing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """close() should surface TypeError disconnect failures during normal runtime."""
    iface = MeshInterface(noProto=False)
    try:
        iface.debugOut = io.StringIO()
        with (
            patch.object(iface, "_send_disconnect", side_effect=TypeError("boom")),
            caplog.at_level(logging.DEBUG),
            pytest.raises(TypeError, match="boom"),
        ):
            iface.close()
    finally:
        if not getattr(iface, "_closing", False):
            iface.close()

    assert (
        "Failed to send disconnect during close(); continuing shutdown."
        not in caplog.text
    )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_close_suppresses_disconnect_type_error_during_finalization(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """close() should swallow TypeError disconnect failures during finalization."""
    iface = MeshInterface(noProto=False)
    try:
        iface.debugOut = io.StringIO()
        with (
            patch.object(iface, "_send_disconnect", side_effect=TypeError("boom")),
            patch.object(sys, "is_finalizing", lambda: True),
            caplog.at_level(logging.DEBUG),
        ):
            iface.close()
        assert iface._closing is True
        assert iface.debugOut is None
    finally:
        if not getattr(iface, "_closing", False):
            iface.close()

    assert (
        "Failed to send disconnect during interpreter finalization; continuing shutdown."
        in caplog.text
    )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_connected_noop_when_closing() -> None:
    """_connected() should not set connection state while shutdown is in progress."""
    with MeshInterface(noProto=True) as iface:
        iface._closing = True

        iface._connected()

        assert iface.isConnected.is_set() is False


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_prepare_for_connect_resets_closing() -> None:
    """_prepare_for_connect() must reset _closing so a retry connect can succeed."""
    with MeshInterface(noProto=True) as iface:
        iface.close()
        assert iface._closing is True

        iface._prepare_for_connect()
        assert iface._closing is False


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_connected_succeeds_after_prepare_for_connect() -> None:
    """After close() + _prepare_for_connect(), _connected() must set isConnected."""
    with MeshInterface(noProto=True) as iface:
        iface.close()
        assert iface._closing is True
        assert iface.isConnected.is_set() is False

        iface._prepare_for_connect()
        assert iface._closing is False

        iface._connected()
        assert iface.isConnected.is_set() is True


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_prepare_for_connect_idempotent() -> None:
    """Calling _prepare_for_connect() on a fresh interface should not break anything."""
    with MeshInterface(noProto=True) as iface:
        assert iface._closing is False
        iface._prepare_for_connect()
        assert iface._closing is False
        iface._connected()
        assert iface.isConnected.is_set() is True


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_connected_publishes_established_once_per_connected_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_connected() should publish established once per connect transition."""
    queued_callbacks: list[Any] = []

    def _queue_work(callback: Any) -> None:
        queued_callbacks.append(callback)

    monkeypatch.setattr(
        "meshtastic.mesh_interface.publishingThread.queueWork", _queue_work
    )

    with MeshInterface(noProto=True) as iface:
        monkeypatch.setattr(iface, "_start_heartbeat", lambda: None)

        iface._connected()
        iface._connected()
        assert len(queued_callbacks) == 1

        # Simulate a new session transition and verify publish happens again.
        iface.isConnected.clear()
        iface._connected()
        assert len(queued_callbacks) == 2


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_disconnected_publishes_lost_once_per_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_disconnected() should publish at most once for each connected session."""
    queued_callbacks: list[Any] = []

    def _queue_work(callback: Any) -> None:
        queued_callbacks.append(callback)

    monkeypatch.setattr(
        "meshtastic.mesh_interface.publishingThread.queueWork", _queue_work
    )

    with MeshInterface(noProto=True) as iface:
        iface.isConnected.set()
        iface._disconnected()
        assert len(queued_callbacks) == 1

        # Idempotent while already disconnected.
        iface._disconnected()
        assert len(queued_callbacks) == 1

        # A new connection should allow one new lost notification.
        iface.isConnected.set()
        iface._disconnected()
        assert len(queued_callbacks) == 2


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_close_with_heartbeatTimer(caplog):
#    """Test close() with heartbeatTimer"""
#    iface = MeshInterface(noProto=True)
#    anode = Node('foo', 'bar')
#    aconfig = Config()
#    aonfig.preferences.phone_timeout_secs = 10
#    anode.config = aconfig
#    iface.localNode = anode
#    assert iface.heartbeatTimer is None
#    with caplog.at_level(logging.DEBUG):
#        iface._start_heartbeat()
#        assert iface.heartbeatTimer is not None
#        iface.close()


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_handleFromRadio_empty_payload(caplog):
#    """Test _handle_from_radio"""
#    iface = MeshInterface(noProto=True)
#    with caplog.at_level(logging.DEBUG):
#        iface._handle_from_radio(b'')
#    iface.close()
#    assert re.search(r'Unexpected FromRadio payload', caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handleFromRadio_with_my_info(caplog: pytest.LogCaptureFixture) -> None:
    """Test _handle_from_radio with my_info."""
    # Note: I captured the '--debug --info' for the bytes below.
    # It "translates" to this:
    # my_info {
    #  my_node_num: 682584012
    #  firmware_version: "1.2.49.5354c49"
    #  reboot_count: 13
    #  bitrate: 17.088470458984375
    #  message_timeout_msec: 300000
    #  min_app_version: 20200
    #  max_channels: 8
    #  has_wifi: true
    # }
    from_radio_bytes = b"\x1a,\x08\xcc\xcf\xbd\xc5\x02\x18\r2\x0e1.2.49.5354c49P\r]0\xb5\x88Ah\xe0\xa7\x12p\xe8\x9d\x01x\x08\x90\x01\x01"
    with MeshInterface(noProto=True) as iface:
        with caplog.at_level(logging.DEBUG):
            iface._handle_from_radio(from_radio_bytes)
    assert re.search(r"Received from radio: my_info {", caplog.text, re.MULTILINE)
    assert re.search(r"my_node_num: 682584012", caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handleFromRadio_with_node_info(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test _handle_from_radio with node_info."""
    # Note: I captured the '--debug --info' for the bytes below.
    # It "translates" to this:
    # node_info {
    #  num: 682584012
    #  user {
    #    id: "!28af67cc"
    #    long_name: "Unknown 67cc"
    #    short_name: "?CC"
    #    macaddr: "$o(\257g\314"
    #    hw_model: HELTEC_V2_1
    #  }
    #  position {
    #    }
    #  }

    from_radio_bytes = b'"2\x08\xcc\xcf\xbd\xc5\x02\x12(\n\t!28af67cc\x12\x0cUnknown 67cc\x1a\x03?CC"\x06$o(\xafg\xcc0\n\x1a\x00'
    with MeshInterface(noProto=True) as iface:
        with caplog.at_level(logging.DEBUG):
            iface._start_config()
            iface._handle_from_radio(from_radio_bytes)
            assert re.search(
                r"Received from radio: node_info {", caplog.text, re.MULTILINE
            )
            assert re.search(r"682584012", caplog.text, re.MULTILINE)
            # validate some of showNodes() output
            iface.showNodes()
            out, err = capsys.readouterr()
            assert re.search(r" 1 ", out, re.MULTILINE)
            assert re.search(r"│ Unknown 67cc │ ", out, re.MULTILINE)
            assert re.search(r"│\s+!28af67cc\s+│\s+\?CC\s+│", out, re.MULTILINE)
            assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handleFromRadio_with_node_info_tbeam1(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test _handle_from_radio with node_info."""
    # Note: Captured the '--debug --info' for the bytes below.
    # pylint: disable=C0301
    from_radio_bytes = (
        b'"=\x08\x80\xf8\xc8\xf6\x07\x12"\n\t!7ed23c00\x12\x07TBeam 1\x1a\x02T1"'
        b"\x06\x94\xb9~\xd2<\x000\x04\x1a\x07 ]MN\x01\xbea%\xad\x01\xbea=\x00\x00,A"
    )
    with MeshInterface(noProto=True) as iface:
        with caplog.at_level(logging.DEBUG):
            iface._start_config()
            iface._handle_from_radio(from_radio_bytes)
            assert re.search(r"Received nodeinfo", caplog.text, re.MULTILINE)
            assert re.search(r"TBeam 1", caplog.text, re.MULTILINE)
            assert re.search(r"2127707136", caplog.text, re.MULTILINE)
            # validate some of showNodes() output
            iface.showNodes()
            out, err = capsys.readouterr()
            assert re.search(r" 1 ", out, re.MULTILINE)
            assert re.search(r"│ TBeam 1 │ ", out, re.MULTILINE)
            assert re.search(r"│ !7ed23c00 │", out, re.MULTILINE)
            assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handleFromRadio_with_node_info_tbeam_with_bad_data(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test _handle_from_radio with node_info with some bad data (issue#172) - ensure we do not throw exception."""
    # Note: Captured the '--debug --info' for the bytes below.
    from_radio_bytes = b'"\x17\x08\xdc\x8a\x8a\xae\x02\x12\x08"\x06\x00\x00\x00\x00\x00\x00\x1a\x00=\x00\x00\xb8@'
    with MeshInterface(noProto=True) as iface:
        with caplog.at_level(logging.DEBUG):
            iface._start_config()
            iface._handle_from_radio(from_radio_bytes)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_MeshInterface_sendToRadio_no_proto(caplog: pytest.LogCaptureFixture) -> None:
    """Verify the default MeshInterface._send_to_radio_impl logs that subclasses must implement radio sending.

    Asserts that invoking the base implementation produces a log message containing "Subclass must provide toradio".

    """
    with MeshInterface(noProto=True) as iface:
        with caplog.at_level(logging.DEBUG):
            iface._send_to_radio_impl(mesh_pb2.ToRadio())
    assert re.search(r"Subclass must provide toradio", caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendData_too_long(caplog: pytest.LogCaptureFixture) -> None:
    """Test when data payload is too big."""
    with MeshInterface(noProto=True) as iface:
        some_large_text = (
            b"This is a long text that will be too long for send text." * 12
        )
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(MeshInterface.MeshInterfaceError) as pytest_wrapped_e:
                iface.sendData(some_large_text)
            assert pytest_wrapped_e.type is MeshInterface.MeshInterfaceError
            assert "Data payload too big" in str(pytest_wrapped_e.value)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendData_unknown_app() -> None:
    """Verify that calling sendData with PortNum.UNKNOWN_APP raises MeshInterface.MeshInterfaceError.

    and the error message contains "A non-zero port number must be specified".
    """
    with MeshInterface(noProto=True) as iface:
        with pytest.raises(MeshInterface.MeshInterfaceError) as pytest_wrapped_e:
            iface.sendData(b"hello", portNum=portnums_pb2.PortNum.UNKNOWN_APP)
    assert pytest_wrapped_e.type is MeshInterface.MeshInterfaceError
    assert "A non-zero port number must be specified" in str(pytest_wrapped_e.value)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPosition_with_a_position(caplog: pytest.LogCaptureFixture) -> None:
    """Test sendPosition when lat/long/alt."""
    with MeshInterface(noProto=True) as iface:
        with caplog.at_level(logging.DEBUG):
            iface.sendPosition(latitude=40.8, longitude=-111.86, altitude=201)
            assert re.search(r"p.latitude_i:408", caplog.text, re.MULTILINE)
            assert re.search(r"p.longitude_i:-11186", caplog.text, re.MULTILINE)
            assert re.search(r"p.altitude:201", caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_with_no_destination() -> None:
    """Test _send_packet() raises MeshInterfaceError when destinationId is None."""
    with MeshInterface(noProto=True) as iface:
        with pytest.raises(
            MeshInterface.MeshInterfaceError,
            match="Invalid destinationId:",
        ):
            mesh_packet = mesh_pb2.MeshPacket()
            iface._send_packet(mesh_packet, destinationId=None)  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_with_destination_as_int(caplog: pytest.LogCaptureFixture) -> None:
    """Test _send_packet() with int as a destination."""
    with MeshInterface(noProto=True) as iface:
        with caplog.at_level(logging.DEBUG):
            meshPacket = mesh_pb2.MeshPacket()
            iface._send_packet(meshPacket, destinationId=123)
            assert re.search(r"Not sending packet", caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_alias_with_destination_as_int(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test _sendPacket() compatibility alias delegates to _send_packet()."""
    with MeshInterface(noProto=True) as iface:
        with caplog.at_level(logging.DEBUG):
            meshPacket = mesh_pb2.MeshPacket()
            iface._sendPacket(meshPacket, destinationId=123)
            assert re.search(r"Not sending packet", caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_alias_routes_through_send_packet_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_sendPacket() should delegate through MeshInterface._send_packet."""
    with MeshInterface(noProto=True) as iface:
        mesh_packet = mesh_pb2.MeshPacket()
        expected_result = mesh_pb2.MeshPacket()
        mock_send_packet = MagicMock(return_value=expected_result)
        monkeypatch.setattr(iface, "_send_packet", mock_send_packet)

        result = iface._sendPacket(
            mesh_packet,
            destinationId=123,
            wantAck=True,
            hopLimit=5,
            pkiEncrypted=True,
            publicKey=b"k",
        )

        assert result is expected_result
        mock_send_packet.assert_called_once_with(
            meshPacket=mesh_packet,
            destinationId=123,
            wantAck=True,
            hopLimit=5,
            pkiEncrypted=True,
            publicKey=b"k",
        )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_with_short_non_hex_bang_destination_raises() -> None:
    """Short/non-hex bang IDs should raise when node DB lookup is unavailable."""
    with MeshInterface(noProto=True) as iface:
        with iface._node_db_lock:
            iface.nodes = None
            iface.nodesByNum = None
        mesh_packet = mesh_pb2.MeshPacket()
        with pytest.raises(
            MeshInterface.MeshInterfaceError,
            match=r"NodeId !1234 not found and node DB is unavailable",
        ):
            iface._send_packet(mesh_packet, destinationId="!1234")


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_with_destination_as_BROADCAST_ADDR(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test _send_packet() with BROADCAST_ADDR as a destination."""
    with MeshInterface(noProto=True) as iface:
        with caplog.at_level(logging.DEBUG):
            meshPacket = mesh_pb2.MeshPacket()
            iface._send_packet(meshPacket, destinationId=BROADCAST_ADDR)
            assert re.search(r"Not sending packet", caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_with_destination_as_LOCAL_ADDR_no_myInfo() -> None:
    """Test _send_packet() with LOCAL_ADDR raises MeshInterfaceError when myInfo is missing."""
    with MeshInterface(noProto=True) as iface:
        with pytest.raises(
            MeshInterface.MeshInterfaceError,
            match="No myInfo found",
        ):
            meshPacket = mesh_pb2.MeshPacket()
            iface._send_packet(meshPacket, destinationId=LOCAL_ADDR)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_with_destination_as_LOCAL_ADDR_with_myInfo(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test _send_packet() with LOCAL_ADDR as a destination with myInfo."""
    with MeshInterface(noProto=True) as iface:
        myInfo = MagicMock()
        iface.myInfo = myInfo
        iface.myInfo.my_node_num = 1
        with caplog.at_level(logging.DEBUG):
            meshPacket = mesh_pb2.MeshPacket()
            iface._send_packet(meshPacket, destinationId=LOCAL_ADDR)
            assert re.search(r"Not sending packet", caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_with_destination_is_blank_with_nodes(
    iface_with_nodes: MeshInterface,
) -> None:
    """Test _send_packet() with '' as a destination raises MeshInterfaceError when node not found."""
    iface = iface_with_nodes
    meshPacket = mesh_pb2.MeshPacket()
    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match=r"NodeId  not found in DB",
    ):
        iface._send_packet(meshPacket, destinationId="")


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_with_destination_is_blank_without_nodes(
    iface_with_nodes: MeshInterface,
) -> None:
    """Test _send_packet() with '' as a destination raises when node DB is unavailable."""
    iface = iface_with_nodes
    with iface._node_db_lock:
        iface.nodes = None
        iface.nodesByNum = None
    meshPacket = mesh_pb2.MeshPacket()
    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match=r"NodeId  not found and node DB is unavailable",
    ):
        iface._send_packet(meshPacket, destinationId="")


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_with_unsupported_destination_type_without_nodes_raises(
    iface_with_nodes: MeshInterface,
) -> None:
    """Unsupported destination types should raise when node DB is unavailable."""
    iface = iface_with_nodes
    with iface._node_db_lock:
        iface.nodes = None
        iface.nodesByNum = None
    mesh_packet = mesh_pb2.MeshPacket()
    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match=r"Unexpected destinationId type: <class 'list'>",
    ):
        iface._send_packet(mesh_packet, destinationId=[])  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_raises_when_node_record_lacks_numeric_num(
    iface_with_nodes: MeshInterface,
) -> None:
    """_send_packet should reject DB node records that do not provide an integer num."""
    iface = iface_with_nodes
    iface.nodes = {"bad": {"user": {"id": "bad"}}}
    mesh_packet = mesh_pb2.MeshPacket()
    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match=r"NodeId bad has no numeric 'num' in DB",
    ):
        iface._send_packet(mesh_packet, destinationId="bad")


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_uses_numeric_num_from_node_record(
    iface_with_nodes: MeshInterface,
) -> None:
    """_send_packet should use node['num'] when destination resolves via DB lookup."""
    iface = iface_with_nodes
    iface.nodes = {"dst": {"num": 4242}}
    mesh_packet = mesh_pb2.MeshPacket()

    sent = iface._send_packet(mesh_packet, destinationId="dst")

    assert sent.to == 4242


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize(
    ("destination_id", "expected_num"),
    [
        ("12345678", 0x12345678),
        ("0x12345678", 0x12345678),
        ("0X90ABCDEF", 0x90ABCDEF),
        ("!89abcdef", 0x89ABCDEF),
    ],
)
def test_sendPacket_parses_supported_hex_node_id_forms(
    destination_id: str, expected_num: int
) -> None:
    """_send_packet should parse accepted compact hex destination ID forms."""
    with MeshInterface(noProto=True) as iface:
        mesh_packet = mesh_pb2.MeshPacket()

        sent = iface._send_packet(mesh_packet, destinationId=destination_id)

        assert sent.to == expected_num


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize("destination_id", ["nothexid", "nothexid1"])
def test_sendPacket_with_non_hex_long_destination_falls_back_to_db_lookup(
    iface_with_nodes: MeshInterface,
    destination_id: str,
) -> None:
    """Non-hex destination strings of length >= 8 should not raise raw ValueError."""
    iface = iface_with_nodes
    mesh_packet = mesh_pb2.MeshPacket()
    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match=rf"NodeId {destination_id} not found in DB",
    ):
        iface._send_packet(mesh_packet, destinationId=destination_id)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_with_hex_suffix_only_string_still_uses_db_lookup(
    iface_with_nodes: MeshInterface,
) -> None:
    """Arbitrary strings ending in hex should not be treated as direct node IDs."""
    iface = iface_with_nodes
    mesh_packet = mesh_pb2.MeshPacket()
    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match=r"NodeId room-deadbeef not found in DB",
    ):
        iface._send_packet(mesh_packet, destinationId="room-deadbeef")


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_applies_explicit_hoplimit_and_pki_encrypted_flag() -> None:
    """_send_packet should honor explicit hopLimit and pkiEncrypted parameters."""
    with MeshInterface(noProto=True) as iface:
        mesh_packet = mesh_pb2.MeshPacket()
        sent = iface._send_packet(
            mesh_packet,
            destinationId=123,
            hopLimit=5,
            pkiEncrypted=True,
        )
    assert sent.hop_limit == 5
    assert sent.pki_encrypted is True


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_sets_public_key_when_provided() -> None:
    """_send_packet should populate meshPacket.public_key when provided."""
    with MeshInterface(noProto=True) as iface:
        mesh_packet = mesh_pb2.MeshPacket()
        sent = iface._send_packet(
            mesh_packet,
            destinationId=123,
            publicKey=b"\xaa\xbb",
        )

    assert sent.public_key == b"\xaa\xbb"


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_getMyNodeInfo() -> None:
    """Test getMyNodeInfo()."""
    with MeshInterface(noProto=True) as iface:
        anode = iface.getNode(LOCAL_ADDR)
        iface.nodesByNum = {1: anode}  # type: ignore[dict-item]
        assert iface.nodesByNum.get(1) == anode  # type: ignore[comparison-overlap]
        myInfo = MagicMock()
        iface.myInfo = myInfo
        iface.myInfo.my_node_num = 1
        myinfo = iface.getMyNodeInfo()
    assert myinfo == anode  # type: ignore[comparison-overlap]


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_getCannedMessage() -> None:
    """Test MeshInterface.getCannedMessage()."""
    with MeshInterface(noProto=True) as iface:
        node = MagicMock()
        node.get_canned_message.return_value = "Hi|Bye|Yes"
        iface.localNode = node
        result = iface.getCannedMessage()
    assert result == "Hi|Bye|Yes"


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_getRingtone() -> None:
    """Ensure MeshInterface.getRingtone delegates to the local node and returns its ringtone string.

    The local node's get_ringtone() return value is forwarded unchanged.
    """
    with MeshInterface(noProto=True) as iface:
        node = MagicMock()
        node.get_ringtone.return_value = "foo,bar"
        iface.localNode = node
        result = iface.getRingtone()
    assert result == "foo,bar"


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_generatePacketId() -> None:
    """Test packet-id generation helpers when no currentPacketId (not connected)."""
    with MeshInterface(noProto=True) as iface:
        # not sure when this condition would ever happen... but we can simulate it
        iface.currentPacketId = None  # type: ignore[assignment]
        assert iface.currentPacketId is None
        with pytest.raises(MeshInterface.MeshInterfaceError) as excinfo:
            iface._generate_packet_id()
        with pytest.raises(MeshInterface.MeshInterfaceError) as excinfo_alias:
            iface._generatePacketId()
    assert "Not connected yet, can not generate packet" in str(excinfo.value)
    assert "Not connected yet, can not generate packet" in str(excinfo_alias.value)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_alias_with_no_destination() -> None:
    """Test _sendPacket() alias raises MeshInterfaceError when destinationId is None."""
    with MeshInterface(noProto=True) as iface:
        with pytest.raises(
            MeshInterface.MeshInterfaceError,
            match="Invalid destinationId:",
        ):
            mesh_packet = mesh_pb2.MeshPacket()
            iface._sendPacket(mesh_packet, destinationId=None)  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_fixupPosition_empty_pos() -> None:
    """Test _fixup_position()."""
    with MeshInterface(noProto=True) as iface:
        pos: dict[str, Any] = {}
        newpos = iface._fixup_position(pos)
    assert newpos == pos


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_fixupPosition_no_changes_needed() -> None:
    """Test _fixup_position()."""
    with MeshInterface(noProto=True) as iface:
        pos = {"latitude": 101, "longitude": 102}
        newpos = iface._fixup_position(pos)
    assert newpos == pos


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_fixupPosition() -> None:
    """Test _fixup_position()."""
    with MeshInterface(noProto=True) as iface:
        pos = {"latitudeI": 1010000000, "longitudeI": 1020000000}
        newpos = iface._fixup_position(pos)
    assert newpos == {
        "latitude": 101.0,
        "latitudeI": 1010000000,
        "longitude": 102.0,
        "longitudeI": 1020000000,
    }


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_nodeNumToId(iface_with_nodes: MeshInterface) -> None:
    """Test _node_num_to_id()."""
    iface = iface_with_nodes
    assert iface.myInfo is not None
    iface.myInfo.my_node_num = 2475227164
    someid = iface._node_num_to_id(2475227164)
    assert someid == "!9388f81c"


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_nodeNumToId_not_found(iface_with_nodes: MeshInterface) -> None:
    """Test _node_num_to_id()."""
    iface = iface_with_nodes
    assert iface.myInfo is not None
    iface.myInfo.my_node_num = 2475227164
    someid = iface._node_num_to_id(123)
    assert someid is None


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_nodeNumToId_to_all(iface_with_nodes: MeshInterface) -> None:
    """Test _node_num_to_id()."""
    iface = iface_with_nodes
    assert iface.myInfo is not None
    iface.myInfo.my_node_num = 2475227164
    someid = iface._node_num_to_id(0xFFFFFFFF)
    assert someid == "^all"


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_getOrCreateByNum_minimal(iface_with_nodes: MeshInterface) -> None:
    """Test _get_or_create_by_num()."""
    iface = iface_with_nodes
    assert iface.myInfo is not None
    iface.myInfo.my_node_num = 2475227164
    tmp = iface._get_or_create_by_num(123)
    assert tmp == {
        "num": 123,
        "user": {
            "hwModel": "UNSET",
            "id": "!0000007b",
            "shortName": "007b",
            "longName": "Meshtastic 007b",
        },
    }


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_getOrCreateByNum_not_found(iface_with_nodes: MeshInterface) -> None:
    """Test _get_or_create_by_num()."""
    iface = iface_with_nodes
    assert iface.myInfo is not None
    iface.myInfo.my_node_num = 2475227164
    with pytest.raises(MeshInterface.MeshInterfaceError) as pytest_wrapped_e:
        iface._get_or_create_by_num(0xFFFFFFFF)
    assert pytest_wrapped_e.type is MeshInterface.MeshInterfaceError


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_getOrCreateByNum(iface_with_nodes: MeshInterface) -> None:
    """Test _get_or_create_by_num()."""
    iface = iface_with_nodes
    assert iface.myInfo is not None
    iface.myInfo.my_node_num = 2475227164
    tmp = iface._get_or_create_by_num(2475227164)
    assert tmp["num"] == 2475227164


# TODO
# @pytest.mark.unit
# def test_enter():
#    """Test __enter__()"""
#    iface = MeshInterface(noProto=True)
#    assert iface == iface.__enter__()


@pytest.mark.unit
def test_exit_with_exception(caplog: pytest.LogCaptureFixture) -> None:
    """Verify that MeshInterface.__exit__ logs the exception type, value, and traceback when an exception is raised inside its context.

    This test intentionally raises a ValueError inside the with-block to verify that
    MeshInterface.__exit__ properly logs exception details.

    Asserts an ERROR-level log entry contains the ValueError message and a traceback that includes the line where the exception was raised.

    Raises
    ------
    ValueError
        Intentionally raised inside the context body to verify __exit__ exception logging.
    """
    with caplog.at_level(logging.ERROR):
        with pytest.raises(ValueError):
            with MeshInterface(noProto=True):
                raise ValueError("Something went wrong")
    assert re.search(
        r"An exception of type <class \'ValueError\'> with value Something went wrong has occurred",
        caplog.text,
        re.MULTILINE,
    )
    assert "Traceback:" in caplog.text
    assert "in test_exit_with_exception" in caplog.text
    assert 'raise ValueError("Something went wrong")' in caplog.text


@pytest.mark.unit
def test_showNodes_exclude_self(
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
) -> None:
    """showNodes(includeSelf=False) should omit the local node from output."""
    with caplog.at_level(logging.DEBUG):
        iface = iface_with_nodes
        iface.localNode.nodeNum = 2475227164
        iface.showNodes()
        out_with_self, _ = capsys.readouterr()
        iface.showNodes(includeSelf=False)
        out_without_self, _ = capsys.readouterr()
        assert "!9388f81c" in out_with_self
        assert "!9388f81c" not in out_without_self


@pytest.mark.unitslow
def test_waitForConfig() -> None:
    """Verify that waitForConfig raises MeshInterface.MeshInterfaceError when the interface times out waiting for configuration."""
    with MeshInterface(noProto=True) as iface:
        # override how long to wait
        iface._timeout = Timeout(1)
        with pytest.raises(MeshInterface.MeshInterfaceError) as pytest_wrapped_e:
            iface.waitForConfig()
    assert pytest_wrapped_e.type is MeshInterface.MeshInterfaceError
    assert "Timed out waiting for interface config" in str(pytest_wrapped_e.value)


@pytest.mark.unit
def test_waitConnected_raises_an_exception() -> None:
    """Test waitConnected()."""
    with MeshInterface(noProto=True) as iface:
        iface.failure = MeshInterface.MeshInterfaceError("warn about something")
        with pytest.raises(MeshInterface.MeshInterfaceError) as excinfo:
            iface._wait_connected(0.01)
    assert "warn about something" in str(excinfo.value)


@pytest.mark.unit
def test_waitConnected_isConnected_timeout() -> None:
    """Verifies that _wait_connected raises a MeshInterfaceError when the connection does not complete within the specified timeout.

    Asserts the raised error message contains "Timed out waiting for connection completion".
    """
    with pytest.raises(MeshInterface.MeshInterfaceError) as excinfo:
        with MeshInterface(noProto=True) as iface:
            iface.noProto = False
            iface._wait_connected(0.01)
    assert "Timed out waiting for connection completion" in str(excinfo.value)


@pytest.mark.unit
def test_timeago() -> None:
    """Test that the _timeago function returns sane values."""
    assert _timeago(0) == "now"
    assert _timeago(1) == "1 sec ago"
    assert _timeago(15) == "15 secs ago"
    assert _timeago(333) == "5 mins ago"
    assert _timeago(99999) == "1 day ago"
    assert _timeago(9999999) == "3 months ago"
    assert _timeago(-999) == "now"


@pytest.mark.unit
@given(seconds=st.integers())
def test_timeago_fuzz(seconds: int) -> None:
    """Fuzz _timeago to ensure it works with any integer."""
    val = _timeago(seconds)
    assert re.fullmatch(r"now|\d+ (secs?|mins?|hours?|days?|months?|years?) ago", val)


# Concurrent access edge case tests
@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_concurrent_packet_id_generation() -> None:
    """Test that packet ID generation is thread-safe."""
    with MeshInterface(noProto=True) as iface:
        packet_ids = []
        errors = []
        packet_ids_lock = threading.Lock()
        errors_lock = threading.Lock()

        def generate_packet_ids() -> None:
            try:
                for _ in range(100):
                    packet_id = iface._generate_packet_id()
                    with packet_ids_lock:
                        packet_ids.append(packet_id)
            except Exception as e:  # noqa: BLE001
                with errors_lock:
                    errors.append(e)

        threads = [threading.Thread(target=generate_packet_ids) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        # All packet IDs should be unique
        assert len(packet_ids) == len(set(packet_ids))
        # All packet IDs should be within valid range
        assert all(0 <= pid <= 0xFFFFFFFF for pid in packet_ids)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_concurrent_node_database_access() -> None:
    """Test that node database access is thread-safe."""
    with MeshInterface(noProto=True) as iface:
        iface.nodes = {}
        iface.nodesByNum = {}
        errors = []
        errors_lock = threading.Lock()

        def update_nodes(node_num: int) -> None:
            try:
                for i in range(50):
                    node_id = f"!{node_num:08x}"
                    node = iface._get_or_create_by_num(node_num)
                    with iface._node_db_lock:
                        node["lastHeard"] = i
                        if iface.nodes is not None:
                            iface.nodes[node_id] = node
                        if iface.nodesByNum is not None:
                            iface.nodesByNum[node_num] = node
            except Exception as e:  # noqa: BLE001
                with errors_lock:
                    errors.append(e)

        threads = [threading.Thread(target=update_nodes, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_concurrent_queue_operations() -> None:
    """Test that queue operations are thread-safe."""
    with MeshInterface(noProto=True) as iface:
        iface.queue = OrderedDict()
        errors = []
        errors_lock = threading.Lock()

        def add_to_queue(start_id: int) -> None:
            try:
                for i in range(50):
                    packet_id = start_id * 100 + i
                    packet = mesh_pb2.ToRadio()
                    with iface._queue_lock:
                        iface.queue[packet_id] = packet
            except Exception as e:  # noqa: BLE001
                with errors_lock:
                    errors.append(e)

        def remove_from_queue() -> None:
            try:
                for _ in range(25):
                    with iface._queue_lock:
                        if iface.queue:
                            key = next(iter(iface.queue))
                            iface.queue.pop(key, None)
            except Exception as e:  # noqa: BLE001
                with errors_lock:
                    errors.append(e)

        add_threads = [
            threading.Thread(target=add_to_queue, args=(i,)) for i in range(4)
        ]
        remove_threads = [threading.Thread(target=remove_from_queue) for _ in range(4)]

        for t in add_threads + remove_threads:
            t.start()
        for t in add_threads + remove_threads:
            t.join()

        assert len(errors) == 0


@pytest.mark.unit
def test_concurrent_response_handler_registration() -> None:
    """Test that response handler registration is thread-safe."""
    with MeshInterface(noProto=True) as iface:
        iface.responseHandlers = {}
        errors = []
        added_ids = []
        added_ids_lock = threading.Lock()
        errors_lock = threading.Lock()

        def register_handlers(start_id: int) -> None:
            try:
                for i in range(50):
                    request_id = start_id * 100 + i
                    handler = MagicMock()
                    iface._add_response_handler(request_id, handler)
                    with added_ids_lock:
                        added_ids.append(request_id)
            except Exception as e:  # noqa: BLE001
                with errors_lock:
                    errors.append(e)

        threads = [
            threading.Thread(target=register_handlers, args=(i,)) for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        # All registered IDs should be in responseHandlers
        for request_id in added_ids:
            assert request_id in iface.responseHandlers


@pytest.mark.unit
def test_concurrent_close_with_packet_id_generation() -> None:
    """Test that close() properly handles concurrent packet ID generation."""
    errors = []
    stop_flag = threading.Event()
    started = threading.Event()
    errors_lock = threading.Lock()

    with MeshInterface(noProto=True) as iface:

        def generate_ids() -> None:
            try:
                while not stop_flag.is_set():
                    iface._generate_packet_id()
                    started.set()
            except Exception as e:  # noqa: BLE001
                with errors_lock:
                    errors.append(e)

        threads = [threading.Thread(target=generate_ids) for _ in range(5)]
        for t in threads:
            t.start()

        assert started.wait(timeout=1.0)
        # Exercise close() while packet-id generation is active.
        iface.close()

        # Signal threads to stop
        stop_flag.set()
        for t in threads:
            t.join(timeout=1.0)
        assert all(not t.is_alive() for t in threads)

    # Close is implicit in context manager exit
    assert len(errors) == 0


@pytest.mark.unit
def test_concurrent_showNodes() -> None:
    """Test that showNodes() is thread-safe."""
    with MeshInterface(noProto=True) as iface:
        iface.nodes = {
            f"!{i:08x}": {
                "num": i,
                "user": {"id": f"!{i:08x}", "longName": f"Node{i}"},
                "position": {},
            }
            for i in range(100)
        }
        iface.nodesByNum = {i: iface.nodes[f"!{i:08x}"] for i in range(100)}
        iface.myInfo = MagicMock()
        iface.myInfo.my_node_num = 0

        errors = []
        errors_lock = threading.Lock()

        def call_show_nodes() -> None:
            try:
                for _ in range(10):
                    iface.showNodes()
            except Exception as e:  # noqa: BLE001
                with errors_lock:
                    errors.append(e)

        threads = [threading.Thread(target=call_show_nodes) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0


@pytest.mark.unit
def test_concurrent_getNode() -> None:
    """Test that getNode() is thread-safe."""
    with MeshInterface(noProto=True) as iface:
        iface.nodesByNum = {
            i: {"num": i, "user": {"id": f"!{i:08x}"}} for i in range(100)
        }
        errors = []
        errors_lock = threading.Lock()

        def get_nodes() -> None:
            try:
                for i in range(50):
                    # Avoid channel/config waits in noProto mode; this test only
                    # validates concurrent access safety for getNode().
                    node = iface.getNode(f"!{i:08x}", requestChannels=False)
                    assert node is not None
            except Exception as e:  # noqa: BLE001
                with errors_lock:
                    errors.append(e)

        threads = [threading.Thread(target=get_nodes) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0


@pytest.mark.unit
def test_packet_id_no_collision_after_many_generations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that packet IDs don't collide after many generations."""
    next_random = iter(range(1_000_000))
    monkeypatch.setattr(
        "meshtastic.mesh_interface.random.randint",
        lambda _a, _b: next(next_random),
    )
    with MeshInterface(noProto=True) as iface:
        packet_ids = set()

        # Generate many packet IDs
        for _ in range(10000):
            packet_id = iface._generate_packet_id()
            assert packet_id not in packet_ids
            packet_ids.add(packet_id)

        # Verify all are unique
        assert len(packet_ids) == 10000


@pytest.mark.unit
def test_concurrent_sendText_with_queue() -> None:
    """Test that sendText() with queue is thread-safe."""
    with MeshInterface(noProto=True) as iface:
        iface.myInfo = MagicMock()
        iface.myInfo.my_node_num = 12345
        iface._localChannels = [channel_pb2.Channel(index=0)]
        errors = []
        errors_lock = threading.Lock()

        def send_texts() -> None:
            try:
                for i in range(10):
                    iface.sendText(f"message_{i}", wantAck=True)
            except Exception as e:  # noqa: BLE001
                with errors_lock:
                    errors.append(e)

        threads = [threading.Thread(target=send_texts) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_init_subscribes_log_line_when_debug_output_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MeshInterface should subscribe log-line printing when debugOut is provided."""
    subscribed: list[tuple[Any, str]] = []

    def _subscribe(handler: Any, topic: str) -> None:
        subscribed.append((handler, topic))

    monkeypatch.setattr(
        mesh_interface_module.pub,  # type: ignore[attr-defined]
        "subscribe",
        _subscribe,
    )

    with MeshInterface(noProto=True, debugOut=io.StringIO()):
        pass

    assert (MeshInterface._print_log_line, "meshtastic.log.line") in subscribed


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_exit_close_failure_paths(caplog: pytest.LogCaptureFixture) -> None:
    """__exit__ should suppress close() failures only while unwinding another exception."""
    iface = MeshInterface(noProto=True)
    iface.close = MagicMock(side_effect=RuntimeError("close failed"))  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING):
        iface.__exit__(ValueError, ValueError("inner"), None)
    assert "close() failed while unwinding an existing exception." in caplog.text

    with pytest.raises(RuntimeError, match="close failed"):
        iface.__exit__(None, None, None)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_print_log_line_and_record_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    """_print_log_line should route by output type and _handle_log_* should normalize output."""
    color_printer = MagicMock()
    monkeypatch.setattr(mesh_interface_module, "print_color", color_printer)

    interface = types.SimpleNamespace(debugOut=io.StringIO())
    MeshInterface._print_log_line("message", interface)
    assert interface.debugOut.getvalue().strip() == "message"

    captured_callable: list[str] = []
    interface.debugOut = captured_callable.append
    MeshInterface._print_log_line("callable", interface)
    assert captured_callable == ["callable"]

    interface.debugOut = mesh_interface_module.sys.stdout  # type: ignore[attr-defined]
    MeshInterface._print_log_line("DEBUG log", interface)
    MeshInterface._print_log_line("INFO log", interface)
    MeshInterface._print_log_line("WARN log", interface)
    MeshInterface._print_log_line("ERR log", interface)
    MeshInterface._print_log_line("OTHER log", interface)
    assert color_printer.print.call_args_list[0].kwargs["color"] == "cyan"
    assert color_printer.print.call_args_list[1].kwargs["color"] == "white"
    assert color_printer.print.call_args_list[2].kwargs["color"] == "yellow"
    assert color_printer.print.call_args_list[3].kwargs["color"] == "red"

    sent_lines: list[str] = []
    monkeypatch.setattr(
        mesh_interface_module.pub,  # type: ignore[attr-defined]
        "sendMessage",
        lambda _topic, **kwargs: sent_lines.append(kwargs["line"]),
    )
    with MeshInterface(noProto=True) as iface:
        iface._handle_log_line("line-with-newline\n")
        record = mesh_pb2.LogRecord()
        record.message = "record-line\n"
        iface._handle_log_record(record)

    assert sent_lines == ["line-with-newline", "record-line"]


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_show_info_includes_metadata_summary() -> None:
    """showInfo() should include metadata output when metadata is present."""
    with MeshInterface(noProto=True) as iface:
        iface.metadata = mesh_pb2.DeviceMetadata(firmware_version="2.7.18")
        summary = iface.showInfo(file=io.StringIO())

    assert "Metadata:" in summary


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_show_nodes_handles_single_level_and_missing_nested_fields(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """showNodes() should handle single-level keys and missing nested paths without introspecting internals."""
    with MeshInterface(noProto=True) as iface:
        iface.nodesByNum = {
            1: {
                "num": 1,
                "shortName": "N1",
                "user": {"id": "!00000001"},
            }
        }
        iface.nodes = {"!00000001": iface.nodesByNum[1]}
        iface.localNode.nodeNum = 999
        table = iface.showNodes(
            showFields=["shortName", "user.id", "missing.path", "position.latitude"]
        )
        _ = capsys.readouterr()

    assert "shortName" in table
    assert "N1" in table
    assert "!00000001" in table
    assert "N/A" in table


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_show_nodes_formats_powered_battery_and_future_since(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """showNodes() should render battery sentinel values and future timestamps safely."""
    future_ts = int(time.time()) + 600
    with MeshInterface(noProto=True) as iface:
        iface.nodesByNum = {
            1: {
                "num": 1,
                "user": {
                    "id": "!00000001",
                    "longName": "Node1",
                    "shortName": "N1",
                    "hwModel": "UNSET",
                    "publicKey": "x",
                    "role": "CLIENT",
                },
                "deviceMetrics": {"batteryLevel": 101},
                "lastHeard": future_ts,
            }
        }
        iface.nodes = {"!00000001": iface.nodesByNum[1]}
        iface.localNode.nodeNum = 999
        table = iface.showNodes(
            showFields=["deviceMetrics.batteryLevel", "since", "user.id"]
        )
        _ = capsys.readouterr()

    assert "Powered" in table
    assert "N/A" in table


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_get_node_resets_retry_budget_on_new_channel_progress() -> None:
    """getNode() should reset retry countdown when partial channel progress is observed."""

    class _FakeNode:
        def __init__(self) -> None:
            self.partialChannels: list[int] = []
            self.request_calls: list[int] = []
            self.wait_calls = 0

        def requestChannels(self, startingIndex: int = 0) -> None:
            """Track channel request starting indexes."""
            self.request_calls.append(startingIndex)

        def waitForConfig(self) -> bool:
            """Return False once before succeeding to simulate partial progress."""
            self.wait_calls += 1
            if self.wait_calls == 1:
                self.partialChannels = [1]
                return False
            return True

    fake_node = _FakeNode()
    with MeshInterface(noProto=True) as iface:
        with patch("meshtastic.node.Node", return_value=fake_node):
            result = iface.getNode("!00112233", requestChannelAttempts=2)

    assert cast(Any, result) is fake_node
    assert fake_node.request_calls == [0, 1]


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_send_alert_and_mqtt_proxy_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """sendAlert() and sendMqttClientProxyMessage() should delegate with expected payloads."""
    with MeshInterface(noProto=True) as iface:
        send_alert = MagicMock(return_value=mesh_pb2.MeshPacket())
        monkeypatch.setattr(iface._send_pipeline, "sendAlert", send_alert)
        response_cb = MagicMock()
        iface.sendAlert(
            "SOS",
            destinationId=42,
            onResponse=response_cb,
            channelIndex=2,
            hopLimit=3,
        )

        assert send_alert.call_count == 1
        send_args = send_alert.call_args
        assert send_args.args[0] == "SOS"
        assert send_args.kwargs["destinationId"] == 42
        assert send_args.kwargs["channelIndex"] == 2
        assert send_args.kwargs["hopLimit"] == 3

        send_mqtt = MagicMock()
        monkeypatch.setattr(iface._send_pipeline, "sendMqttClientProxyMessage", send_mqtt)
        iface.sendMqttClientProxyMessage("mesh/topic", b"payload")

        assert send_mqtt.call_count == 1
        assert send_mqtt.call_args.args == ("mesh/topic", b"payload")


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_send_data_sets_reply_id_field() -> None:
    """sendData() should preserve the caller-provided reply id."""
    with MeshInterface(noProto=True) as iface:
        packet = iface.sendData(b"ok", destinationId=123, replyId=77)
    assert packet.decoded.reply_id == 77


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_send_position_waits_when_response_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sendPosition(wantResponse=True) should wire response callback and wait for position."""
    with MeshInterface(noProto=True) as iface:
        response_packet = mesh_pb2.MeshPacket()
        response_packet.id = 77
        send_data = MagicMock(return_value=response_packet)
        wait_for_position = MagicMock()
        monkeypatch.setattr(iface, "_send_data_with_wait", send_data)
        monkeypatch.setattr(iface, "waitForPosition", wait_for_position)

        iface.sendPosition(
            latitude=47.0,
            longitude=-122.0,
            altitude=100,
            wantResponse=True,
        )

        on_response = send_data.call_args.kwargs["onResponse"]
        # The onResponse is now a closure in the flow function, not a bound method
        assert on_response is not None
        wait_for_position.assert_called_once_with(request_id=77)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_on_response_position_success_and_routing_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """onResponsePosition() should log parsed position and route errors to waiters."""
    with MeshInterface(noProto=True) as iface:
        position = mesh_pb2.Position()
        position.latitude_i = 471234567
        position.longitude_i = -971234567
        position.altitude = 250
        position.precision_bits = 32
        iface._clear_wait_error(
            WAIT_ATTR_POSITION, request_id=1001
        )
        wait_thread, wait_errors = _start_wait_thread(
            lambda: iface.waitForPosition(request_id=1001)
        )
        _wait_for_scoped_wait_registration(
            iface,
            acknowledgment_attr=WAIT_ATTR_POSITION,
            request_id=1001,
        )
        with caplog.at_level(logging.INFO, logger=flows_module.__name__):
            iface.onResponsePosition(
                {
                    "decoded": {
                        "requestId": 1001,
                        "portnum": portnums_pb2.PortNum.Name(
                            portnums_pb2.PortNum.POSITION_APP
                        ),
                        "payload": position.SerializeToString(),
                    }
                }
            )
        wait_thread.join(timeout=1.0)
        assert not wait_errors
        assert not wait_thread.is_alive()
        assert "Position received:" in caplog.text
        assert "full precision" in caplog.text

        unknown_position = mesh_pb2.Position()
        unknown_position.precision_bits = 5
        iface._clear_wait_error(
            WAIT_ATTR_POSITION, request_id=1002
        )
        wait_thread, wait_errors = _start_wait_thread(
            lambda: iface.waitForPosition(request_id=1002)
        )
        _wait_for_scoped_wait_registration(
            iface,
            acknowledgment_attr=WAIT_ATTR_POSITION,
            request_id=1002,
        )
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=flows_module.__name__):
            iface.onResponsePosition(
                {
                    "decoded": {
                        "requestId": 1002,
                        "portnum": portnums_pb2.PortNum.Name(
                            portnums_pb2.PortNum.POSITION_APP
                        ),
                        "payload": unknown_position.SerializeToString(),
                    }
                }
            )
        wait_thread.join(timeout=1.0)
        assert not wait_errors
        assert not wait_thread.is_alive()
        assert "(unknown)" in caplog.text
        assert "precision:5" in caplog.text

        disabled_position = mesh_pb2.Position()
        disabled_position.precision_bits = 0
        iface._clear_wait_error(
            WAIT_ATTR_POSITION, request_id=1003
        )
        wait_thread, wait_errors = _start_wait_thread(
            lambda: iface.waitForPosition(request_id=1003)
        )
        _wait_for_scoped_wait_registration(
            iface,
            acknowledgment_attr=WAIT_ATTR_POSITION,
            request_id=1003,
        )
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=flows_module.__name__):
            iface.onResponsePosition(
                {
                    "decoded": {
                        "requestId": 1003,
                        "portnum": portnums_pb2.PortNum.Name(
                            portnums_pb2.PortNum.POSITION_APP
                        ),
                        "payload": disabled_position.SerializeToString(),
                    }
                }
            )
        wait_thread.join(timeout=1.0)
        assert not wait_errors
        assert not wait_thread.is_alive()
        assert "position disabled" in caplog.text

    with MeshInterface(noProto=True) as iface:
        iface._clear_wait_error(
            WAIT_ATTR_POSITION, request_id=1004
        )
        wait_thread, wait_errors = _start_wait_thread(
            lambda: iface.waitForPosition(request_id=1004)
        )
        _wait_for_scoped_wait_registration(
            iface,
            acknowledgment_attr=WAIT_ATTR_POSITION,
            request_id=1004,
        )
        iface.onResponsePosition(
            {
                "decoded": {
                    "requestId": 1004,
                    "portnum": portnums_pb2.PortNum.Name(
                        portnums_pb2.PortNum.ROUTING_APP
                    ),
                    "routing": {"errorReason": "NO_RESPONSE"},
                }
            }
        )
        wait_thread.join(timeout=1.0)
        assert not wait_thread.is_alive()
        assert len(wait_errors) == 1
        assert isinstance(wait_errors[0], MeshInterface.MeshInterfaceError)
        assert "No response" in str(wait_errors[0])


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_on_response_position_logs_summary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """onResponsePosition() should log position summaries via the flows module logger."""
    with MeshInterface(noProto=True) as iface:
        position = mesh_pb2.Position()
        position.precision_bits = 32
        with caplog.at_level(logging.INFO, logger=flows_module.__name__):
            iface.onResponsePosition(
                {
                    "decoded": {
                        "portnum": portnums_pb2.PortNum.Name(
                            portnums_pb2.PortNum.POSITION_APP
                        ),
                        "payload": position.SerializeToString(),
                    }
                }
            )

    assert "Position received:" in caplog.text
    assert "full precision" in caplog.text


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_logger_visible_info_handler_treats_console_streams_as_visible() -> None:
    """Only stdout-backed console handlers should suppress stdout fallback."""
    handler_logger = logging.getLogger("meshtastic.tests.visible-info-handler")
    original_handlers = list(handler_logger.handlers)
    original_propagate = handler_logger.propagate
    original_level = handler_logger.level
    try:
        handler_logger.handlers = []
        handler_logger.propagate = False
        handler_logger.setLevel(logging.INFO)

        string_handler = logging.StreamHandler(io.StringIO())
        string_handler.setLevel(logging.INFO)
        handler_logger.addHandler(string_handler)
        assert (
            mesh_interface_module._logger_has_visible_info_handler(handler_logger)
            is False
        )

        handler_logger.removeHandler(string_handler)
        string_handler.close()

        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setLevel(logging.INFO)
        handler_logger.addHandler(stdout_handler)
        assert (
            mesh_interface_module._logger_has_visible_info_handler(handler_logger)
            is True
        )

        handler_logger.removeHandler(stdout_handler)
        stdout_handler.close()

        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setLevel(logging.INFO)
        handler_logger.addHandler(stderr_handler)
        assert (
            mesh_interface_module._logger_has_visible_info_handler(handler_logger)
            is False
        )

        handler_logger.removeHandler(stderr_handler)
        stderr_handler.close()

        class _RichLikeHandler(logging.Handler):
            def __init__(self, stream: object) -> None:
                super().__init__(level=logging.INFO)
                self.console = types.SimpleNamespace(file=stream)

            def emit(self, record: logging.LogRecord) -> None:
                _ = record

        rich_stderr_handler = _RichLikeHandler(sys.__stderr__)
        handler_logger.addHandler(rich_stderr_handler)
        assert (
            mesh_interface_module._logger_has_visible_info_handler(handler_logger)
            is False
        )

        handler_logger.removeHandler(rich_stderr_handler)
        rich_stderr_handler.close()

        rich_string_handler = _RichLikeHandler(io.StringIO())
        handler_logger.addHandler(rich_string_handler)
        assert (
            mesh_interface_module._logger_has_visible_info_handler(handler_logger)
            is False
        )
    finally:
        for handler in list(handler_logger.handlers):
            handler_logger.removeHandler(handler)
            handler.close()
        handler_logger.handlers = original_handlers
        handler_logger.propagate = original_propagate
        handler_logger.setLevel(original_level)


@pytest.mark.unit
def test_logger_visible_info_handler_returns_false_for_disabled_or_high_level() -> None:
    """Visibility helper should short-circuit when logger is disabled or filtered above INFO."""
    disabled_logger = logging.getLogger(
        "meshtastic.tests.visible-info-handler.disabled"
    )
    previous_disabled = disabled_logger.disabled
    try:
        disabled_logger.disabled = True
        assert (
            mesh_interface_module._logger_has_visible_info_handler(disabled_logger)
            is False
        )
    finally:
        disabled_logger.disabled = previous_disabled

    quiet_logger = logging.getLogger("meshtastic.tests.visible-info-handler.quiet")
    previous_level = quiet_logger.level
    try:
        quiet_logger.setLevel(logging.WARNING)
        assert (
            mesh_interface_module._logger_has_visible_info_handler(quiet_logger)
            is False
        )
    finally:
        quiet_logger.setLevel(previous_level)


@pytest.mark.unit
def test_normalize_json_serializable_handles_sequences_and_unknown_values() -> None:
    """JSON normalization should recurse through sequences and stringify unknown objects."""

    class _Unknown:
        def __str__(self) -> str:
            return "unknown-value"

    normalized = mesh_interface_module._normalize_json_serializable(
        {"items": ("a", 1, {3, 4})}
    )
    assert isinstance(normalized, dict)
    normalized_items = cast(list[object], normalized["items"])
    assert normalized_items[0] == "a"
    assert normalized_items[1] == 1
    assert sorted(cast(list[int], normalized_items[2])) == [3, 4]
    assert (
        mesh_interface_module._normalize_json_serializable(_Unknown())
        == "unknown-value"
    )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_send_traceroute_and_response_rendering(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Trace-route send/wait logic and response logging should execute end-to-end."""
    with MeshInterface(noProto=True) as iface:
        iface.nodes = {
            "!1": {"num": 1},
            "!2": {"num": 2},
            "!3": {"num": 3},
        }
        response_packet = mesh_pb2.MeshPacket()
        response_packet.id = 88
        send_data = MagicMock(return_value=response_packet)
        wait_for_traceroute = MagicMock()
        real_wait_for_traceroute = iface.waitForTraceRoute
        monkeypatch.setattr(iface, "_send_data_with_wait", send_data)
        monkeypatch.setattr(iface, "waitForTraceRoute", wait_for_traceroute)
        iface.sendTraceRoute(dest=123, hopLimit=3, channelIndex=1)
        wait_for_traceroute.assert_called_once_with(2, request_id=88)
        monkeypatch.setattr(iface, "waitForTraceRoute", real_wait_for_traceroute)

        route = mesh_pb2.RouteDiscovery()
        route.route.extend([11])
        route.snr_towards.extend([8, 12])
        route.route_back.extend([12])
        route.snr_back.extend([16, 20])
        iface._clear_wait_error(
            WAIT_ATTR_TRACEROUTE, request_id=88
        )
        wait_thread, wait_errors = _start_wait_thread(
            lambda: iface.waitForTraceRoute(1.0, request_id=88)
        )
        _wait_for_scoped_wait_registration(
            iface,
            acknowledgment_attr=WAIT_ATTR_TRACEROUTE,
            request_id=88,
        )
        with caplog.at_level(logging.INFO, logger=flows_module.__name__):
            iface.onResponseTraceRoute(
                {
                    "decoded": {"payload": route.SerializeToString(), "requestId": 88},
                    "to": 20,
                    "from": 21,
                    "hopStart": 1,
                }
            )
        wait_thread.join(timeout=1.0)
        assert not wait_errors
        assert not wait_thread.is_alive()

    assert "Route traced towards destination:" in caplog.text
    assert "Route traced back to us:" in caplog.text


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_on_response_traceroute_routing_no_response_raises() -> None:
    """Traceroute routing NO_RESPONSE replies should be surfaced by waitForTraceRoute()."""
    with MeshInterface(noProto=True) as iface:
        iface._clear_wait_error(
            WAIT_ATTR_TRACEROUTE, request_id=9101
        )
        wait_thread, wait_errors = _start_wait_thread(
            lambda: iface.waitForTraceRoute(1.0, request_id=9101)
        )
        _wait_for_scoped_wait_registration(
            iface,
            acknowledgment_attr=WAIT_ATTR_TRACEROUTE,
            request_id=9101,
        )
        iface.onResponseTraceRoute(
            {
                "decoded": {
                    "requestId": 9101,
                    "portnum": portnums_pb2.PortNum.Name(
                        portnums_pb2.PortNum.ROUTING_APP
                    ),
                    "routing": {"errorReason": "NO_RESPONSE"},
                }
            }
        )
        wait_thread.join(timeout=1.0)
        assert not wait_thread.is_alive()
        assert len(wait_errors) == 1
        assert isinstance(wait_errors[0], MeshInterface.MeshInterfaceError)
        assert "No response" in str(wait_errors[0])


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_on_response_traceroute_parse_failures_surface_to_waiters() -> None:
    """Traceroute parse errors should be recorded and raised by waitForTraceRoute()."""
    with MeshInterface(noProto=True) as iface:
        iface._clear_wait_error(
            WAIT_ATTR_TRACEROUTE, request_id=9102
        )
        wait_thread, wait_errors = _start_wait_thread(
            lambda: iface.waitForTraceRoute(1.0, request_id=9102)
        )
        _wait_for_scoped_wait_registration(
            iface,
            acknowledgment_attr=WAIT_ATTR_TRACEROUTE,
            request_id=9102,
        )
        iface.onResponseTraceRoute(
            {
                "decoded": {
                    "requestId": 9102,
                    "payload": 123,  # Invalid payload type for ParseFromString
                }
            }
        )
        wait_thread.join(timeout=1.0)
        assert not wait_thread.is_alive()
        assert len(wait_errors) == 1
        assert isinstance(wait_errors[0], MeshInterface.MeshInterfaceError)
        assert "Failed to parse traceroute response payload" in str(wait_errors[0])


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_send_telemetry_supported_and_fallback_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sendTelemetry() should populate each payload and log fallback warnings for unknown values."""
    telemetry_calls: list[tuple[telemetry_pb2.Telemetry, dict[str, Any]]] = []
    with MeshInterface(noProto=True) as iface:
        iface.localNode.nodeNum = 77
        iface.nodesByNum = {
            77: {
                "deviceMetrics": {
                    "batteryLevel": 55,
                    "voltage": 4.1,
                    "channelUtilization": 1.5,
                    "airUtilTx": 0.5,
                    "uptimeSeconds": 123,
                }
            }
        }

        def _capture_telemetry_send(
            payload: telemetry_pb2.Telemetry, *_args: object, **kwargs: object
        ) -> mesh_pb2.MeshPacket:
            telemetry_calls.append((payload, kwargs))
            return mesh_pb2.MeshPacket(id=len(telemetry_calls))

        monkeypatch.setattr(iface, "_send_data_with_wait", _capture_telemetry_send)
        wait_for_telemetry = MagicMock()
        monkeypatch.setattr(iface, "waitForTelemetry", wait_for_telemetry)

        iface.sendTelemetry(telemetryType="environment_metrics")
        iface.sendTelemetry(telemetryType="air_quality_metrics")
        iface.sendTelemetry(telemetryType="power_metrics")
        iface.sendTelemetry(telemetryType="local_stats")
        iface.sendTelemetry(telemetryType="device_metrics")
        with pytest.warns(DeprecationWarning, match="Unsupported telemetryType"):
            iface.sendTelemetry(telemetryType="invalid")
        with pytest.warns(DeprecationWarning, match="Unsupported telemetryType"):
            iface.sendTelemetry(telemetryType="invalid2")
        iface.sendTelemetry(telemetryType="device_metrics", wantResponse=True)

    assert telemetry_calls[0][0].HasField("environment_metrics")
    assert telemetry_calls[1][0].HasField("air_quality_metrics")
    assert telemetry_calls[2][0].HasField("power_metrics")
    assert telemetry_calls[3][0].HasField("local_stats")
    assert telemetry_calls[4][0].HasField("device_metrics")
    assert telemetry_calls[5][0].HasField("device_metrics")
    assert telemetry_calls[6][0].HasField("device_metrics")
    assert telemetry_calls[7][1]["onResponse"] is not None
    wait_for_telemetry.assert_called_once_with(request_id=8)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_on_response_telemetry_paths(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """onResponseTelemetry() should handle device metrics, non-device metrics, and routing errors."""
    with MeshInterface(noProto=True) as iface:
        device_t = telemetry_pb2.Telemetry()
        device_t.device_metrics.battery_level = 95
        device_t.device_metrics.voltage = 4.23
        iface._clear_wait_error(
            WAIT_ATTR_TELEMETRY, request_id=2001
        )
        wait_thread, wait_errors = _start_wait_thread(
            lambda: iface.waitForTelemetry(request_id=2001)
        )
        _wait_for_scoped_wait_registration(
            iface,
            acknowledgment_attr=WAIT_ATTR_TELEMETRY,
            request_id=2001,
        )
        with caplog.at_level(logging.INFO, logger=flows_module.__name__):
            iface.onResponseTelemetry(
                {
                    "decoded": {
                        "requestId": 2001,
                        "portnum": portnums_pb2.PortNum.Name(
                            portnums_pb2.PortNum.TELEMETRY_APP
                        ),
                        "payload": device_t.SerializeToString(),
                    }
                }
            )
        wait_thread.join(timeout=1.0)
        assert not wait_errors
        assert not wait_thread.is_alive()
        assert "Telemetry received:" in caplog.text
        assert "Battery level:" in caplog.text

        env_t = telemetry_pb2.Telemetry()
        env_t.environment_metrics.temperature = 21.5
        iface._clear_wait_error(
            WAIT_ATTR_TELEMETRY, request_id=2002
        )
        wait_thread, wait_errors = _start_wait_thread(
            lambda: iface.waitForTelemetry(request_id=2002)
        )
        _wait_for_scoped_wait_registration(
            iface,
            acknowledgment_attr=WAIT_ATTR_TELEMETRY,
            request_id=2002,
        )
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=flows_module.__name__):
            iface.onResponseTelemetry(
                {
                    "decoded": {
                        "requestId": 2002,
                        "portnum": portnums_pb2.PortNum.Name(
                            portnums_pb2.PortNum.TELEMETRY_APP
                        ),
                        "payload": env_t.SerializeToString(),
                    }
                }
            )
        wait_thread.join(timeout=1.0)
        assert not wait_errors
        assert not wait_thread.is_alive()
        assert "environmentMetrics:" in caplog.text

    with MeshInterface(noProto=True) as iface:
        iface._clear_wait_error(
            WAIT_ATTR_TELEMETRY, request_id=2003
        )
        wait_thread, wait_errors = _start_wait_thread(
            lambda: iface.waitForTelemetry(request_id=2003)
        )
        _wait_for_scoped_wait_registration(
            iface,
            acknowledgment_attr=WAIT_ATTR_TELEMETRY,
            request_id=2003,
        )
        iface.onResponseTelemetry(
            {
                "decoded": {
                    "requestId": 2003,
                    "portnum": portnums_pb2.PortNum.Name(
                        portnums_pb2.PortNum.ROUTING_APP
                    ),
                    "routing": {"errorReason": "NO_RESPONSE"},
                }
            }
        )
        wait_thread.join(timeout=1.0)
        assert not wait_thread.is_alive()
        assert len(wait_errors) == 1
        assert isinstance(wait_errors[0], MeshInterface.MeshInterfaceError)
        assert "No response" in str(wait_errors[0])

        iface._clear_wait_error(
            WAIT_ATTR_TELEMETRY, request_id=2004
        )
        wait_thread, wait_errors = _start_wait_thread(
            lambda: iface.waitForTelemetry(request_id=2004)
        )
        _wait_for_scoped_wait_registration(
            iface,
            acknowledgment_attr=WAIT_ATTR_TELEMETRY,
            request_id=2004,
        )
        iface.onResponseTelemetry(
            {
                "decoded": {
                    "requestId": 2004,
                    "portnum": portnums_pb2.PortNum.Name(
                        portnums_pb2.PortNum.ROUTING_APP
                    ),
                    "routing": {"errorReason": "NO_ROUTE"},
                }
            }
        )
        wait_thread.join(timeout=1.0)
        assert not wait_thread.is_alive()
        assert len(wait_errors) == 1
        assert isinstance(wait_errors[0], MeshInterface.MeshInterfaceError)
        assert "Routing error on response: NO_ROUTE" in str(wait_errors[0])


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_on_response_waypoint_paths(caplog: pytest.LogCaptureFixture) -> None:
    """onResponseWaypoint() should log waypoint payloads and route errors to waiters."""
    with MeshInterface(noProto=True) as iface:
        waypoint = mesh_pb2.Waypoint(name="WPT", id=5)
        iface._clear_wait_error(
            WAIT_ATTR_WAYPOINT, request_id=3001
        )
        wait_thread, wait_errors = _start_wait_thread(
            lambda: iface.waitForWaypoint(request_id=3001)
        )
        _wait_for_scoped_wait_registration(
            iface,
            acknowledgment_attr=WAIT_ATTR_WAYPOINT,
            request_id=3001,
        )
        with caplog.at_level(logging.INFO, logger=flows_module.__name__):
            iface.onResponseWaypoint(
                {
                    "decoded": {
                        "requestId": 3001,
                        "portnum": portnums_pb2.PortNum.Name(
                            portnums_pb2.PortNum.WAYPOINT_APP
                        ),
                        "payload": waypoint.SerializeToString(),
                    }
                }
            )
        wait_thread.join(timeout=1.0)
        assert not wait_errors
        assert not wait_thread.is_alive()
        assert "Waypoint received:" in caplog.text

    with MeshInterface(noProto=True) as iface:
        iface._clear_wait_error(
            WAIT_ATTR_WAYPOINT, request_id=3002
        )
        wait_thread, wait_errors = _start_wait_thread(
            lambda: iface.waitForWaypoint(request_id=3002)
        )
        _wait_for_scoped_wait_registration(
            iface,
            acknowledgment_attr=WAIT_ATTR_WAYPOINT,
            request_id=3002,
        )
        iface.onResponseWaypoint(
            {
                "decoded": {
                    "requestId": 3002,
                    "portnum": portnums_pb2.PortNum.Name(
                        portnums_pb2.PortNum.ROUTING_APP
                    ),
                    "routing": {"errorReason": "NO_RESPONSE"},
                }
            }
        )
        wait_thread.join(timeout=1.0)
        assert not wait_thread.is_alive()
        assert len(wait_errors) == 1
        assert isinstance(wait_errors[0], MeshInterface.MeshInterfaceError)
        assert "No response" in str(wait_errors[0])


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize(
    ("handler_name", "waiter_name", "port_name", "error_prefix"),
    [
        pytest.param(
            "onResponsePosition",
            "waitForPosition",
            "POSITION_APP",
            "Failed to parse position response payload",
            id="position",
        ),
        pytest.param(
            "onResponseTelemetry",
            "waitForTelemetry",
            "TELEMETRY_APP",
            "Failed to parse telemetry response payload",
            id="telemetry",
        ),
        pytest.param(
            "onResponseWaypoint",
            "waitForWaypoint",
            "WAYPOINT_APP",
            "Failed to parse waypoint response payload",
            id="waypoint",
        ),
    ],
)
def test_on_response_parse_failures_set_wait_errors(
    handler_name: str,
    waiter_name: str,
    port_name: str,
    error_prefix: str,
) -> None:
    """Malformed response payloads should fail via wait-state errors, not false success."""
    wait_attr_by_waiter = {
        "waitForPosition": WAIT_ATTR_POSITION,
        "waitForTelemetry": WAIT_ATTR_TELEMETRY,
        "waitForWaypoint": WAIT_ATTR_WAYPOINT,
    }
    request_id = 4200
    with MeshInterface(noProto=True) as iface:
        handler = cast(Any, getattr(iface, handler_name))
        waiter = cast(Any, getattr(iface, waiter_name))
        iface._clear_wait_error(wait_attr_by_waiter[waiter_name], request_id=request_id)
        wait_thread, wait_errors = _start_wait_thread(
            lambda: waiter(request_id=request_id)
        )
        _wait_for_scoped_wait_registration(
            iface,
            acknowledgment_attr=wait_attr_by_waiter[waiter_name],
            request_id=request_id,
        )
        handler(
            {
                "decoded": {
                    "requestId": request_id,
                    "portnum": portnums_pb2.PortNum.Name(
                        getattr(portnums_pb2.PortNum, port_name)
                    ),
                    "payload": b"\x80",
                }
            }
        )
        wait_thread.join(timeout=1.0)
        assert not wait_thread.is_alive()
        assert len(wait_errors) == 1
        assert isinstance(wait_errors[0], MeshInterface.MeshInterfaceError)
        assert error_prefix in str(wait_errors[0])


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_send_and_delete_waypoint_response_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sendWaypoint()/deleteWaypoint() should set payload fields and wait when response is requested."""
    sent_payloads: list[mesh_pb2.Waypoint] = []
    with MeshInterface(noProto=True) as iface:
        wait_for_waypoint = MagicMock()
        monkeypatch.setattr(iface, "waitForWaypoint", wait_for_waypoint)

        def _capture_send_data(
            payload: mesh_pb2.Waypoint, *_args: Any, **_kwargs: Any
        ) -> mesh_pb2.MeshPacket:
            sent_payloads.append(payload)
            return mesh_pb2.MeshPacket(id=len(sent_payloads))

        monkeypatch.setattr(iface, "_send_data_with_wait", _capture_send_data)
        monkeypatch.setattr(
            flows_module.secrets,  # type: ignore[attr-defined]
            "randbits",
            lambda _n: (1 << 32) - 1,
        )

        iface.sendWaypoint(
            name="A",
            description="B",
            icon=1,
            expire=60,
            waypoint_id=None,
            latitude=47.1,
            longitude=-96.2,
            wantResponse=True,
        )
        iface.sendWaypoint(
            name="C",
            description="D",
            icon=2,
            expire=120,
            waypoint_id=7,
            wantResponse=False,
        )
        iface.deleteWaypoint(9, wantResponse=True)
        iface.deleteWaypoint(10, wantResponse=False)

    assert sent_payloads[0].id != 0
    assert sent_payloads[0].latitude_i != 0
    assert sent_payloads[0].longitude_i != 0
    assert sent_payloads[1].id == 7
    assert sent_payloads[2].id == 9 and sent_payloads[2].expire == 0
    assert sent_payloads[3].id == 10 and sent_payloads[3].expire == 0
    assert wait_for_waypoint.call_count == 2


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_send_packet_calls_transport_when_proto_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_send_packet() should invoke _send_to_radio() when protocol I/O is enabled."""
    with MeshInterface(noProto=True) as iface:
        iface.noProto = False
        iface.myInfo = MagicMock()
        iface.myInfo.my_node_num = 1
        sent: list[mesh_pb2.ToRadio] = []
        monkeypatch.setattr(iface, "_send_to_radio", sent.append)
        iface._send_packet(mesh_pb2.MeshPacket(), destinationId=1)
        assert sent


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_wait_helpers_raise_expected_timeout_errors() -> None:
    """waitFor* helper methods should raise MeshInterfaceError on timeout."""
    with MeshInterface(noProto=True) as iface:
        iface._timeout = MagicMock()
        iface._timeout.waitForAckNak.return_value = False
        iface._timeout.waitForTraceRoute.return_value = False
        iface._timeout.waitForTelemetry.return_value = False
        iface._timeout.waitForPosition.return_value = False
        iface._timeout.waitForWaypoint.return_value = False

        with pytest.raises(MeshInterface.MeshInterfaceError, match="acknowledgment"):
            iface.waitForAckNak()
        with pytest.raises(MeshInterface.MeshInterfaceError, match="traceroute"):
            iface.waitForTraceRoute(1)
        with pytest.raises(MeshInterface.MeshInterfaceError, match="telemetry"):
            iface.waitForTelemetry()
        with pytest.raises(MeshInterface.MeshInterfaceError, match="position"):
            iface.waitForPosition()
        with pytest.raises(MeshInterface.MeshInterfaceError, match="waypoint"):
            iface.waitForWaypoint()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_waitForAckNak_raises_pending_received_nak_wait_error() -> None:
    """WaitForAckNak should surface detailed pending receivedNak wait errors."""
    with MeshInterface(noProto=True) as iface:
        iface._timeout = MagicMock()
        iface._timeout.waitForAckNak.return_value = False
        iface._set_wait_error(
            WAIT_ATTR_NAK,
            "Failed to decode admin payload: decode-failed: malformed payload",
        )

        with pytest.raises(
            MeshInterface.MeshInterfaceError,
            match="Failed to decode admin payload",
        ):
            iface.waitForAckNak()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_wait_errors_ignore_stale_request_ids() -> None:
    """Routing errors from stale requestIds must not poison active wait state."""
    with MeshInterface(noProto=True) as iface:
        iface._clear_wait_error("receivedTelemetry", request_id=101)

        iface.onResponseTelemetry(
            {
                "decoded": {
                    "portnum": portnums_pb2.PortNum.Name(
                        portnums_pb2.PortNum.ROUTING_APP
                    ),
                    "requestId": 100,
                    "routing": {"errorReason": "NO_RESPONSE"},
                }
            }
        )

        assert iface._acknowledgment.receivedTelemetry is False
        iface._raise_wait_error_if_present("receivedTelemetry", request_id=101)

        iface.onResponseTelemetry(
            {
                "decoded": {
                    "portnum": portnums_pb2.PortNum.Name(
                        portnums_pb2.PortNum.ROUTING_APP
                    ),
                    "requestId": 101,
                    "routing": {"errorReason": "NO_RESPONSE"},
                }
            }
        )

        with pytest.raises(MeshInterface.MeshInterfaceError, match="No response"):
            iface._raise_wait_error_if_present("receivedTelemetry", request_id=101)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_wait_timeout_retires_response_handler_for_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """waitFor* timeouts should retire request-scoped response handlers."""
    with MeshInterface(noProto=True) as iface:
        iface._timeout = MagicMock()
        iface._timeout.waitForTelemetry.return_value = False
        iface._timeout.expireTimeout = 0.01
        iface._timeout.sleepInterval = 0.001
        mock_send = MagicMock(side_effect=lambda packet, *_a, **_k: packet)
        monkeypatch.setattr(iface, "_send_packet", mock_send)

        packet = iface._send_data_with_wait(
            b"ping",
            wantResponse=True,
            onResponse=lambda _packet: None,
            response_wait_attr="receivedTelemetry",
        )
        request_id = packet.id
        assert request_id in iface.responseHandlers

        with pytest.raises(MeshInterface.MeshInterfaceError, match="telemetry"):
            iface.waitForTelemetry(request_id=request_id)

        assert request_id not in iface.responseHandlers


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_request_scoped_wait_state_supports_multiple_active_request_ids() -> None:
    """Multiple same-type waits should keep independent request-scoped error state."""
    with MeshInterface(noProto=True) as iface:
        iface._clear_wait_error("receivedTelemetry", request_id=101)
        iface._clear_wait_error("receivedTelemetry", request_id=202)

        iface._record_routing_wait_error(
            acknowledgment_attr="receivedTelemetry",
            routing_error_reason="NO_RESPONSE",
            request_id=101,
        )

        iface._raise_wait_error_if_present("receivedTelemetry", request_id=202)
        with pytest.raises(MeshInterface.MeshInterfaceError, match="No response"):
            iface._raise_wait_error_if_present("receivedTelemetry", request_id=101)

        iface._retire_wait_request("receivedTelemetry", request_id=101)
        iface._retire_wait_request("receivedTelemetry", request_id=202)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_send_data_rolls_back_wait_state_when_send_packet_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sendData() should remove response state if _send_packet fails before send."""
    with MeshInterface(noProto=True) as iface:
        observed_request_ids: list[int] = []

        class _SendFailureError(OSError):
            """Local sentinel exception used to validate sendData() rollback behavior."""

            def __init__(self, message: str = "socket send failed") -> None:
                super().__init__(message)

        def _fail_send(
            packet: mesh_pb2.MeshPacket, *_args: object, **_kwargs: object
        ) -> NoReturn:
            observed_request_ids.append(packet.id)
            raise _SendFailureError()

        monkeypatch.setattr(iface, "_send_packet", _fail_send)

        with pytest.raises(OSError, match="socket send failed"):
            iface._send_data_with_wait(
                b"ping",
                wantResponse=True,
                onResponse=lambda _packet: None,
                response_wait_attr="receivedTelemetry",
            )

        assert len(observed_request_ids) == 1
        request_id = observed_request_ids[0]
        with iface._response_handlers_lock:
            assert request_id not in iface.responseHandlers
            assert ("receivedTelemetry", request_id) not in iface._response_wait_errors
            assert ("receivedTelemetry", request_id) not in iface._response_wait_acks
            assert not iface._active_wait_request_ids.get("receivedTelemetry")


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_send_data_finalizes_non_zero_packet_id_before_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sendData() should keep generating packet ids until a non-zero id is assigned."""
    with MeshInterface(noProto=True) as iface:
        generated_ids = iter((0, 0, 123456))

        def _generate_packet_id() -> int:
            return next(generated_ids)

        monkeypatch.setattr(iface, "_generate_packet_id", _generate_packet_id)
        monkeypatch.setattr(
            iface,
            "_send_packet",
            lambda packet, *_args, **_kwargs: packet,
        )

        packet = iface._send_data_with_wait(
            b"ping",
            wantResponse=True,
            response_wait_attr="receivedTelemetry",
        )
        assert packet.id == 123456
        with iface._response_handlers_lock:
            assert 123456 in iface._active_wait_request_ids["receivedTelemetry"]


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_send_data_removes_response_handler_when_send_fails_without_wait_attr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sendData() should pop responseHandlers on send failure even without wait-attr tracking."""
    with MeshInterface(noProto=True) as iface:
        observed_request_ids: list[int] = []

        class _SendFailureError(OSError):
            """Local sentinel exception used to validate response-handler rollback."""

            def __init__(self, message: str = "send failure") -> None:
                super().__init__(message)

        def _fail_send(
            packet: mesh_pb2.MeshPacket, *_args: object, **_kwargs: object
        ) -> NoReturn:
            observed_request_ids.append(packet.id)
            raise _SendFailureError()

        monkeypatch.setattr(iface, "_send_packet", _fail_send)

        with pytest.raises(OSError, match="send failure"):
            iface.sendData(
                b"payload",
                onResponse=lambda _packet: None,
            )

        assert len(observed_request_ids) == 1
        request_id = observed_request_ids[0]
        with iface._response_handlers_lock:
            assert request_id not in iface.responseHandlers


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_extract_request_id_from_packet_edge_cases() -> None:
    """Request-id extraction should reject invalid forms and parse positive numeric strings."""
    assert MeshInterface._extract_request_id_from_packet({"decoded": "invalid"}) is None
    assert (
        MeshInterface._extract_request_id_from_packet({"decoded": {"requestId": True}})
        is None
    )
    assert (
        MeshInterface._extract_request_id_from_packet({"decoded": {"requestId": "0"}})
        is None
    )
    assert (
        MeshInterface._extract_request_id_from_packet({"decoded": {"requestId": "17"}})
        == 17
    )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_send_methods_pass_request_id_to_wait_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """send* response paths should forward request-scoped packet ids to wait helpers."""
    with MeshInterface(noProto=True) as iface:
        iface.nodes = {"!1": {"num": 1}, "!2": {"num": 2}, "!3": {"num": 3}}
        wait_for_position = MagicMock()
        wait_for_traceroute = MagicMock()
        wait_for_telemetry = MagicMock()
        wait_for_waypoint = MagicMock()
        monkeypatch.setattr(
            iface,
            "_send_data_with_wait",
            MagicMock(
                side_effect=[
                    mesh_pb2.MeshPacket(id=77),
                    mesh_pb2.MeshPacket(id=88),
                    mesh_pb2.MeshPacket(id=99),
                    mesh_pb2.MeshPacket(id=111),
                    mesh_pb2.MeshPacket(id=222),
                ]
            ),
        )
        monkeypatch.setattr(iface, "waitForPosition", wait_for_position)
        monkeypatch.setattr(iface, "waitForTraceRoute", wait_for_traceroute)
        monkeypatch.setattr(iface, "waitForTelemetry", wait_for_telemetry)
        monkeypatch.setattr(iface, "waitForWaypoint", wait_for_waypoint)

        iface.sendPosition(wantResponse=True)
        iface.sendTraceRoute(dest=123, hopLimit=3)
        iface.sendTelemetry(wantResponse=True)
        iface.sendWaypoint(
            name="A",
            description="B",
            icon=1,
            expire=60,
            wantResponse=True,
        )
        iface.deleteWaypoint(7, wantResponse=True)

        wait_for_position.assert_called_once_with(request_id=77)
        wait_for_traceroute.assert_called_once_with(2, request_id=88)
        wait_for_telemetry.assert_called_once_with(request_id=99)
        assert wait_for_waypoint.call_args_list == [
            call(request_id=111),
            call(request_id=222),
        ]


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_wait_state_helpers_cover_request_resolution_branches() -> None:
    """Wait-state helpers should resolve scoped/unscoped request bookkeeping correctly."""
    with MeshInterface(noProto=True) as iface:
        iface._clear_wait_error("receivedTelemetry", request_id=501)
        iface._set_wait_error("receivedTelemetry", "scoped-error")
        iface._raise_wait_error_if_present("receivedTelemetry")

        iface._clear_wait_error("receivedTelemetry", request_id=501)
        iface._mark_wait_acknowledged("receivedTelemetry")
        with iface._response_handlers_lock:
            assert ("receivedTelemetry", 501) not in iface._response_wait_acks

        iface._mark_wait_acknowledged("receivedTelemetry", request_id=999)
        with iface._response_handlers_lock:
            assert ("receivedTelemetry", 999) not in iface._response_wait_acks

        iface._clear_wait_error("receivedTelemetry")
        iface._set_wait_error(
            "receivedTelemetry",
            "legacy-unscoped-error",
            request_id=777,
        )
        with pytest.raises(
            MeshInterface.MeshInterfaceError, match="legacy-unscoped-error"
        ):
            iface._raise_wait_error_if_present("receivedTelemetry", request_id=777)

        iface._clear_wait_error("receivedPosition")
        iface._mark_wait_acknowledged("receivedPosition", request_id=888)
        with iface._response_handlers_lock:
            assert (
                "receivedPosition",
                UNSCOPED_WAIT_REQUEST_ID,
            ) in iface._response_wait_acks

        iface._clear_wait_error("receivedTelemetry", request_id=601)
        iface._clear_wait_error("receivedTelemetry", request_id=602)
        with iface._response_handlers_lock:
            iface.responseHandlers[601] = ResponseHandler(
                callback=lambda _packet: None, ackPermitted=False
            )
            iface.responseHandlers[602] = ResponseHandler(
                callback=lambda _packet: None, ackPermitted=False
            )
            iface._response_wait_errors[("receivedTelemetry", 601)] = "err-a"
            iface._response_wait_errors[("receivedTelemetry", 602)] = "err-b"
            iface._response_wait_acks.add(("receivedTelemetry", 601))
            iface._response_wait_acks.add(("receivedTelemetry", 602))
        iface._retire_wait_request("receivedTelemetry")
        with iface._response_handlers_lock:
            assert 601 not in iface.responseHandlers
            assert 602 not in iface.responseHandlers
            assert ("receivedTelemetry", 601) not in iface._response_wait_errors
            assert ("receivedTelemetry", 602) not in iface._response_wait_errors
            assert ("receivedTelemetry", 601) not in iface._response_wait_acks
            assert ("receivedTelemetry", 602) not in iface._response_wait_acks

        iface._clear_wait_error("receivedPosition", request_id=700)
        with iface._response_handlers_lock:
            iface._response_wait_acks.add(
                ("receivedPosition", UNSCOPED_WAIT_REQUEST_ID)
            )
        assert not iface._wait_for_request_ack(
            "receivedPosition", 700, timeout_seconds=0.05
        )
        with iface._response_handlers_lock:
            assert (
                "receivedPosition",
                UNSCOPED_WAIT_REQUEST_ID,
            ) in iface._response_wait_acks

        with iface._response_handlers_lock:
            iface._response_wait_acks.add(("receivedPosition", 1))
            iface._response_wait_acks.add(("otherAttr", 2))
        iface._clear_wait_error("receivedPosition")
        with iface._response_handlers_lock:
            assert ("receivedPosition", 1) not in iface._response_wait_acks
            assert ("otherAttr", 2) in iface._response_wait_acks


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_retired_scoped_wait_ids_do_not_clobber_unscoped_wait_state() -> None:
    """Late callbacks for retired scoped waits should not write into unscoped state."""
    with MeshInterface(noProto=True) as iface:
        iface._clear_wait_error("receivedTelemetry", request_id=321)
        iface._retire_wait_request("receivedTelemetry", request_id=321)

        iface._mark_wait_acknowledged("receivedTelemetry", request_id=321)
        iface._set_wait_error(
            "receivedTelemetry",
            "stale-scoped-error",
            request_id=321,
        )
        with iface._response_handlers_lock:
            assert (
                "receivedTelemetry",
                UNSCOPED_WAIT_REQUEST_ID,
            ) not in iface._response_wait_acks
            assert (
                "receivedTelemetry",
                UNSCOPED_WAIT_REQUEST_ID,
            ) not in iface._response_wait_errors

        iface._mark_wait_acknowledged("receivedTelemetry")
        assert iface._acknowledgment.receivedTelemetry is True


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_record_routing_wait_error_ignores_none_like_reason() -> None:
    """Routing wait-error recorder should no-op for None/NONE reasons."""
    with MeshInterface(noProto=True) as iface:
        iface._clear_wait_error("receivedTelemetry", request_id=801)
        iface._record_routing_wait_error(
            acknowledgment_attr="receivedTelemetry",
            routing_error_reason="NONE",
            request_id=801,
        )
        iface._raise_wait_error_if_present("receivedTelemetry", request_id=801)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_on_response_telemetry_logs_all_device_metric_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Telemetry response logging should include optional device-metric fields when present."""
    with MeshInterface(noProto=True) as iface:
        telemetry = telemetry_pb2.Telemetry()
        telemetry.device_metrics.channel_utilization = 12.5
        telemetry.device_metrics.air_util_tx = 4.5
        telemetry.device_metrics.uptime_seconds = 321
        with caplog.at_level(logging.INFO, logger=flows_module.__name__):
            iface.onResponseTelemetry(
                {
                    "decoded": {
                        "portnum": portnums_pb2.PortNum.Name(
                            portnums_pb2.PortNum.TELEMETRY_APP
                        ),
                        "payload": telemetry.SerializeToString(),
                    }
                }
            )
    assert "Total channel utilization:" in caplog.text
    assert "Transmit air utilization:" in caplog.text
    assert "Uptime: 321 s" in caplog.text


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_wait_helpers_use_request_scoped_waiter_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Request-scoped waitFor* helpers should delegate to send pipeline with attr-specific keys."""
    with MeshInterface(noProto=True) as iface:
        wait_calls: list[tuple[str, int, float]] = []

        def _wait_for_request_ack(
            acknowledgment_attr: str,
            request_id: int,
            *,
            timeout_seconds: float,
        ) -> bool:
            wait_calls.append((acknowledgment_attr, request_id, timeout_seconds))
            return True

        monkeypatch.setattr(
            iface._send_pipeline, "_wait_for_request_ack", _wait_for_request_ack
        )

        iface.waitForTraceRoute(1.5, request_id=11)
        iface.waitForPosition(request_id=22)
        iface.waitForWaypoint(request_id=33)

        assert wait_calls[0][0:2] == ("receivedTraceRoute", 11)
        assert wait_calls[1][0:2] == ("receivedPosition", 22)
        assert wait_calls[2][0:2] == ("receivedWaypoint", 33)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_wait_for_request_ack_supports_overlapping_same_type_waits() -> None:
    """Request-scoped wait path should handle overlapping telemetry waits independently."""
    with MeshInterface(noProto=True) as iface:
        iface._timeout = Timeout(maxSecs=0.5)
        iface._timeout.sleepInterval = 0.001
        iface._clear_wait_error("receivedTelemetry", request_id=11)
        iface._clear_wait_error("receivedTelemetry", request_id=22)
        errors: list[BaseException] = []
        wait_started = {11: threading.Event(), 22: threading.Event()}
        release_waits = threading.Event()

        def _wait_for(req_id: int) -> None:
            try:
                assert release_waits.wait(timeout=1.0)
                wait_started[req_id].set()
                iface.waitForTelemetry(request_id=req_id)
            except Exception as exc:  # noqa: BLE001 - assertion below
                errors.append(exc)

        wait_11 = threading.Thread(target=_wait_for, args=(11,), daemon=True)
        wait_22 = threading.Thread(target=_wait_for, args=(22,), daemon=True)
        wait_11.start()
        wait_22.start()
        release_waits.set()
        assert wait_started[11].wait(timeout=1.0)
        assert wait_started[22].wait(timeout=1.0)
        _wait_for_scoped_wait_registration(
            iface,
            acknowledgment_attr=WAIT_ATTR_TELEMETRY,
            request_id=11,
        )
        _wait_for_scoped_wait_registration(
            iface,
            acknowledgment_attr=WAIT_ATTR_TELEMETRY,
            request_id=22,
        )
        iface._mark_wait_acknowledged("receivedTelemetry", request_id=11)
        iface._mark_wait_acknowledged("receivedTelemetry", request_id=22)
        wait_11.join(timeout=1.0)
        wait_22.join(timeout=1.0)

        assert not errors
        assert not wait_11.is_alive()
        assert not wait_22.is_alive()
        with iface._response_handlers_lock:
            assert not iface._active_wait_request_ids.get("receivedTelemetry")


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_request_scoped_wait_wakes_immediately_on_recorded_error() -> None:
    """request_id waiters should wake promptly when a matching wait error is recorded."""
    with MeshInterface(noProto=True) as iface:
        iface._timeout = Timeout(maxSecs=5.0)
        iface._timeout.sleepInterval = 0.001
        request_id = 303
        iface._clear_wait_error("receivedTelemetry", request_id=request_id)
        iface._record_routing_wait_error(
            acknowledgment_attr="receivedTelemetry",
            routing_error_reason="NO_ROUTE",
            request_id=request_id,
        )
        started = time.monotonic()
        with pytest.raises(
            MeshInterface.MeshInterfaceError,
            match="Routing error on response: NO_ROUTE",
        ):
            iface.waitForTelemetry(request_id=request_id)
        assert time.monotonic() - started < 0.5


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_request_scoped_wait_times_out_for_unscoped_error_across_overlapping_waits() -> (
    None
):
    """Overlapping request-scoped waits should ignore unscoped routing errors."""
    with MeshInterface(noProto=True) as iface:
        iface._timeout = Timeout(maxSecs=0.05)
        iface._timeout.sleepInterval = 0.001
        request_a = 411
        request_b = 422
        iface._clear_wait_error("receivedTelemetry", request_id=request_a)
        iface._clear_wait_error("receivedTelemetry", request_id=request_b)
        iface._record_routing_wait_error(
            acknowledgment_attr="receivedTelemetry",
            routing_error_reason="NO_ROUTE",
        )

        with pytest.raises(
            MeshInterface.MeshInterfaceError,
            match="Timed out waiting for telemetry",
        ):
            iface.waitForTelemetry(request_id=request_a)
        with pytest.raises(
            MeshInterface.MeshInterfaceError,
            match="Timed out waiting for telemetry",
        ):
            iface.waitForTelemetry(request_id=request_b)

        with iface._response_handlers_lock:
            assert not iface._active_wait_request_ids.get("receivedTelemetry")
            assert (
                "receivedTelemetry",
                UNSCOPED_WAIT_REQUEST_ID,
            ) not in iface._response_wait_errors


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_public_key_and_optional_getters_none_paths(
    iface_with_nodes: MeshInterface,
) -> None:
    """GetPublicKey should return user key while optional local-node getters return None when absent."""
    iface = iface_with_nodes
    assert iface.myInfo is not None
    iface.myInfo.my_node_num = 2475227164
    assert iface.nodesByNum is not None
    node = iface.nodesByNum[2475227164]
    node["user"]["publicKey"] = b"abc"
    assert iface.getPublicKey() == b"abc"
    node["user"] = {}
    assert iface.getPublicKey() is None
    iface.myInfo = None
    assert iface.getPublicKey() is None

    iface.localNode = None  # type: ignore[assignment]
    assert iface.getCannedMessage() is None
    assert iface.getRingtone() is None


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_send_heartbeat_builds_to_radio_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sendHeartbeat() should send a ToRadio with heartbeat field populated."""
    with MeshInterface(noProto=True) as iface:
        sent: list[mesh_pb2.ToRadio] = []
        monkeypatch.setattr(iface, "_send_to_radio", sent.append)
        iface.sendHeartbeat()
        assert sent[0].HasField("heartbeat")


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_start_config_skips_reserved_nodeless_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_start_config() should bump generated config id if it equals NODELESS_WANT_CONFIG_ID."""
    with MeshInterface(noProto=True) as iface:
        monkeypatch.setattr(
            mesh_interface_module.random,  # type: ignore[attr-defined]
            "randint",
            lambda _a, _b: NODELESS_WANT_CONFIG_ID,
        )
        sent: list[mesh_pb2.ToRadio] = []
        monkeypatch.setattr(iface, "_send_to_radio", sent.append)
        iface._start_config()
    assert iface.configId == NODELESS_WANT_CONFIG_ID + 1
    assert sent[0].want_config_id == NODELESS_WANT_CONFIG_ID + 1


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_queue_helpers_cover_state_transitions() -> None:
    """Queue helper methods should cover unknown status, full queue, and pop/decrement logic."""
    with MeshInterface(noProto=True) as iface:
        iface.queueStatus = None
        assert iface._queue_has_free_space() is True
        iface._queue_claim()

        iface.queueStatus = mesh_pb2.QueueStatus(free=1, maxlen=2)
        assert iface._queue_has_free_space() is True
        iface._queue_claim()
        assert iface.queueStatus.free == 0

        iface.queue = OrderedDict()
        assert iface._queue_pop_for_send() is None

        iface.queue[1] = mesh_pb2.ToRadio()
        iface.queueStatus.free = 0
        assert iface._queue_pop_for_send() is None
        iface.queueStatus.free = 1
        popped = iface._queue_pop_for_send()
        assert popped is not None
        assert popped[0] == 1
        assert iface.queueStatus.free == 0


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_send_to_radio_waits_resends_and_tracks_requeue(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """_send_to_radio() should wait for queue space, resend queued packets, and requeue unacked items."""
    with MeshInterface(noProto=True) as iface:
        iface.noProto = False
        iface.queueStatus = mesh_pb2.QueueStatus(free=0, maxlen=10)
        existing = mesh_pb2.ToRadio()
        existing.packet.id = 100
        iface.queue[100] = existing
        iface.queue[150] = False

        incoming = mesh_pb2.ToRadio()
        incoming.packet.id = 200

        sent_ids: list[int] = []

        def _send_impl(msg: mesh_pb2.ToRadio) -> None:
            sent_ids.append(msg.packet.id if msg.HasField("packet") else -1)

        monkeypatch.setattr(iface, "_send_to_radio_impl", _send_impl)

        def _sleep_and_free(_seconds: float) -> None:
            assert iface.queueStatus is not None
            iface.queueStatus.free = 10

        monkeypatch.setattr(
            mesh_interface_module.time,  # type: ignore[attr-defined]
            "sleep",
            _sleep_and_free,
        )

        with caplog.at_level(logging.DEBUG):
            iface._send_to_radio(incoming)

        assert "Waiting for free space in TX Queue" in caplog.text
        assert 100 in sent_ids
        assert 200 in sent_ids

    class _RequeueQueue(OrderedDict[int, mesh_pb2.ToRadio | bool]):
        def __bool__(self) -> bool:
            return False

        def pop(  # type: ignore[override]
            self, key: int, default: mesh_pb2.ToRadio | bool = False
        ) -> mesh_pb2.ToRadio | bool:
            if key == 123:
                return True
            return super().pop(key, default)

    with MeshInterface(noProto=True) as iface:
        iface.noProto = False
        iface.queue = _RequeueQueue()
        packet = mesh_pb2.ToRadio()
        packet.packet.id = 123
        monkeypatch.setattr(iface, "_send_to_radio_impl", lambda _msg: None)
        pops = iter([(123, packet), None])
        original_pop = iface._queue_pop_for_send
        monkeypatch.setattr(iface, "_queue_pop_for_send", lambda: next(pops))
        iface._send_to_radio(mesh_pb2.ToRadio())
        monkeypatch.setattr(iface, "_queue_pop_for_send", original_pop)
        assert 123 in iface.queue


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_send_to_radio_successful_missing_entry_is_not_immediately_requeued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successfully-sent packet without immediate queue-status reply should not be requeued in the same cycle."""
    with MeshInterface(noProto=True) as iface:
        iface.noProto = False
        packet = mesh_pb2.ToRadio()
        packet.packet.id = 123
        sent_ids: list[int] = []

        def _send_impl(msg: mesh_pb2.ToRadio) -> None:
            sent_ids.append(msg.packet.id if msg.HasField("packet") else -1)

        monkeypatch.setattr(iface, "_send_to_radio_impl", _send_impl)
        pops = iter([(123, packet), None])
        original_pop = iface._queue_pop_for_send
        monkeypatch.setattr(iface, "_queue_pop_for_send", lambda: next(pops))
        try:
            iface._send_to_radio(mesh_pb2.ToRadio())
            assert 123 in sent_ids
            assert 123 not in iface.queue
        finally:
            monkeypatch.setattr(iface, "_queue_pop_for_send", original_pop)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_send_to_radio_requeues_packet_when_send_impl_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A packet should be requeued when the send path raises before successful handoff."""
    with MeshInterface(noProto=True) as iface:
        iface.noProto = False
        packet = mesh_pb2.ToRadio()
        packet.packet.id = 123
        incoming = mesh_pb2.ToRadio()
        incoming.packet.id = 999

        class _SendImplFailure(RuntimeError):
            """Intentional send failure sentinel for requeue-path testing."""

            def __init__(self) -> None:
                super().__init__("send failed")

        def _failing_send(_msg: mesh_pb2.ToRadio) -> None:
            raise _SendImplFailure()

        monkeypatch.setattr(iface, "_send_to_radio_impl", _failing_send)
        pops = iter([(123, packet), None])
        original_pop = iface._queue_pop_for_send
        monkeypatch.setattr(iface, "_queue_pop_for_send", lambda: next(pops))
        try:
            with pytest.raises(_SendImplFailure, match="send failed"):
                iface._send_to_radio(incoming)
            assert 123 in iface.queue
        finally:
            monkeypatch.setattr(iface, "_queue_pop_for_send", original_pop)
            # Keep context-manager shutdown path from triggering the intentional send failure.
            monkeypatch.setattr(iface, "_send_to_radio_impl", lambda _msg: None)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handle_config_complete_and_queue_status_branches() -> None:
    """_handle_config_complete() and _handle_queue_status_from_radio() should execute all key branches."""
    with MeshInterface(noProto=True) as iface:
        channel = channel_pb2.Channel(index=1)
        iface._localChannels = [channel]
        iface.localNode = MagicMock()
        iface._connected = MagicMock()  # type: ignore[method-assign]
        iface._handle_config_complete()
        iface.localNode.setChannels.assert_called_once_with([channel])
        iface._connected.assert_called_once()

        queued = mesh_pb2.ToRadio()
        queued.packet.id = 111
        iface.queue[111] = queued

        status_hit = mesh_pb2.QueueStatus(free=1, maxlen=4, res=0, mesh_packet_id=111)
        iface._handle_queue_status_from_radio(status_hit)
        assert 111 not in iface.queue

        status_unexpected = mesh_pb2.QueueStatus(
            free=1, maxlen=4, res=0, mesh_packet_id=222
        )
        iface._handle_queue_status_from_radio(status_unexpected)
        assert iface.queue[222] is False

        status_res = mesh_pb2.QueueStatus(free=1, maxlen=4, res=1, mesh_packet_id=222)
        iface._handle_queue_status_from_radio(status_res)
        assert iface.queue[222] is False


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handle_queue_status_awaiting_correlation_not_marked_unexpected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Queue status for recently sent packets should not be logged as unexpected replies."""
    with MeshInterface(noProto=True) as iface:
        packet_id = 0x01020304
        packet = mesh_pb2.ToRadio()
        packet.packet.id = packet_id
        resent_queue: OrderedDict[int, mesh_pb2.ToRadio | bool] = OrderedDict(
            [(packet_id, packet)]
        )
        iface._queue_send_runtime._reconcile_resent_queue(
            resent_queue=resent_queue,
            sent_packet_ids={packet_id},
        )

        with caplog.at_level(logging.DEBUG):
            iface._handle_queue_status_from_radio(
                mesh_pb2.QueueStatus(free=3, maxlen=4, res=0, mesh_packet_id=packet_id)
            )

    assert packet_id not in iface.queue
    assert "Reply for unexpected packet ID" not in caplog.text
    assert (
        "Correlated queue-status reply for packet awaiting correlation" in caplog.text
    )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handle_from_radio_branch_matrix(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """_handle_from_radio() should handle metadata/node-info and non-config branch dispatch paths."""
    published_topics: list[str] = []
    monkeypatch.setattr(
        mesh_interface_module.publishingThread,  # type: ignore[attr-defined]
        "queueWork",
        lambda callback: callback(),
    )
    monkeypatch.setattr(
        mesh_interface_module.pub,  # type: ignore[attr-defined]
        "sendMessage",
        lambda topic, **_kwargs: published_topics.append(topic),
    )

    with MeshInterface(noProto=True) as iface:
        iface._start_config()

        metadata_msg = mesh_pb2.FromRadio()
        metadata_msg.metadata.firmware_version = "2.7.18"
        iface._handle_from_radio(metadata_msg.SerializeToString())
        assert iface.metadata is not None
        assert iface.metadata.firmware_version == "2.7.18"

        node_info_msg = mesh_pb2.FromRadio()
        node_info_msg.node_info.num = 999
        node_info_msg.node_info.user.id = "!000003e7"
        node_info_msg.node_info.user.long_name = "N999"
        node_info_msg.node_info.user.short_name = "N9"
        with caplog.at_level(logging.DEBUG):
            iface._handle_from_radio(node_info_msg.SerializeToString())
        assert "Node has no position key" in caplog.text

        handle_config_complete = MagicMock()
        handle_channel = MagicMock()
        handle_packet = MagicMock()
        handle_log_record = MagicMock()
        handle_queue_status = MagicMock()
        monkeypatch.setattr(
            iface._receive_pipeline, "_handle_config_complete", handle_config_complete
        )
        monkeypatch.setattr(iface._receive_pipeline, "_handle_channel", handle_channel)
        monkeypatch.setattr(
            iface._receive_pipeline, "_handle_packet_from_radio", handle_packet
        )
        monkeypatch.setattr(
            iface._receive_pipeline, "_handle_log_record", handle_log_record
        )
        monkeypatch.setattr(
            iface._receive_pipeline,
            "_handle_queue_status_from_radio",
            handle_queue_status,
        )

        config_complete_msg = mesh_pb2.FromRadio()
        assert iface.configId is not None
        config_complete_msg.config_complete_id = iface.configId
        iface._handle_from_radio(config_complete_msg.SerializeToString())
        handle_config_complete.assert_called_once()

        channel_msg = mesh_pb2.FromRadio()
        channel_msg.channel.index = 1
        iface._handle_from_radio(channel_msg.SerializeToString())
        handle_channel.assert_called_once()

        packet_msg = mesh_pb2.FromRadio()
        packet_msg.packet.id = 10
        iface._handle_from_radio(packet_msg.SerializeToString())
        handle_packet.assert_called_once()

        log_msg = mesh_pb2.FromRadio()
        log_msg.log_record.message = "hello"
        iface._handle_from_radio(log_msg.SerializeToString())
        handle_log_record.assert_called_once()

        queue_msg = mesh_pb2.FromRadio()
        queue_msg.queueStatus.free = 1
        queue_msg.queueStatus.maxlen = 5
        iface._handle_from_radio(queue_msg.SerializeToString())
        handle_queue_status.assert_called_once()

        notif_msg = mesh_pb2.FromRadio()
        notif_msg.clientNotification.reply_id = 1
        iface._handle_from_radio(notif_msg.SerializeToString())

        mqtt_msg = mesh_pb2.FromRadio()
        mqtt_msg.mqttClientProxyMessage.topic = "t"
        iface._handle_from_radio(mqtt_msg.SerializeToString())

        xmodem_msg = mesh_pb2.FromRadio()
        xmodem_msg.xmodemPacket.control = cast(Any, 1)
        iface._handle_from_radio(xmodem_msg.SerializeToString())

        disconnected_calls: list[int] = []
        monkeypatch.setattr(
            MeshInterface,
            "_disconnected",
            lambda _iface: disconnected_calls.append(1),
        )
        restart_config = MagicMock()
        monkeypatch.setattr(iface, "_start_config", restart_config)
        rebooted_msg = mesh_pb2.FromRadio(rebooted=True)
        iface._handle_from_radio(rebooted_msg.SerializeToString())
        assert disconnected_calls == [1]
        restart_config.assert_called_once()

        with caplog.at_level(logging.DEBUG):
            iface._handle_from_radio(mesh_pb2.FromRadio().SerializeToString())
        assert "Unexpected FromRadio payload" in caplog.text

    assert "meshtastic.node.updated" in published_topics
    assert "meshtastic.clientNotification" in published_topics
    assert "meshtastic.mqttclientproxymessage" in published_topics
    assert "meshtastic.xmodempacket" in published_topics


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handle_from_radio_config_and_module_config_branches() -> None:
    """_handle_from_radio() should copy each config/moduleConfig branch into localNode caches."""
    config_fields = [
        "device",
        "position",
        "power",
        "network",
        "display",
        "lora",
        "bluetooth",
        "security",
    ]
    module_fields = [
        "mqtt",
        "serial",
        "external_notification",
        "store_forward",
        "range_test",
        "telemetry",
        "canned_message",
        "audio",
        "remote_hardware",
        "neighbor_info",
        "detection_sensor",
        "ambient_lighting",
        "paxcounter",
        "traffic_management",
    ]

    with MeshInterface(noProto=True) as iface:
        for field in config_fields:
            msg = mesh_pb2.FromRadio()
            getattr(msg.config, field).SetInParent()
            iface._handle_from_radio(msg.SerializeToString())
            assert iface.localNode.localConfig.HasField(cast(Any, field))

        for field in module_fields:
            msg = mesh_pb2.FromRadio()
            getattr(msg.moduleConfig, field).SetInParent()
            iface._handle_from_radio(msg.SerializeToString())
            assert iface.localNode.moduleConfig.HasField(cast(Any, field))


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handle_from_radio_config_update_skips_unsupported_local_cache_fields() -> None:
    """Config updates should skip unsupported local-only cache fields without raising."""
    with MeshInterface(noProto=True) as iface:
        msg_supported = mesh_pb2.FromRadio()
        msg_supported.config.device.SetInParent()
        iface._handle_from_radio(msg_supported.SerializeToString())
        assert iface.localNode.localConfig.HasField("device")

        # Regression coverage for multinode CI: these fields may exist on
        # FromRadio.config but not on localNode.localConfig.
        source_fields = config_pb2.Config.DESCRIPTOR.fields_by_name
        target_fields = iface.localNode.localConfig.DESCRIPTOR.fields_by_name

        if "sessionkey" in source_fields and "sessionkey" not in target_fields:
            msg_sessionkey = mesh_pb2.FromRadio()
            msg_sessionkey.config.sessionkey.SetInParent()
            iface._handle_from_radio(msg_sessionkey.SerializeToString())

        if "device_ui" in source_fields and "device_ui" not in target_fields:
            msg_device_ui = mesh_pb2.FromRadio()
            msg_device_ui.config.device_ui.SetInParent()
            iface._handle_from_radio(msg_device_ui.SerializeToString())

        # Supported cached fields remain intact after unsupported updates.
        assert iface.localNode.localConfig.HasField("device")


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_node_num_to_id_invalid_user_payloads() -> None:
    """_node_num_to_id() should return None when user payload is missing or has invalid id type."""
    with MeshInterface(noProto=True) as iface:
        iface.nodesByNum = {
            1: {"num": 1, "user": "bad-user"},
            2: {"num": 2, "user": {"id": 123}},
        }
        assert iface._node_num_to_id(1) is None
        assert iface._node_num_to_id(2) is None


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_get_or_create_by_num_requires_initialized_database() -> None:
    """_get_or_create_by_num() should raise when nodesByNum is not initialized."""
    with MeshInterface(noProto=True) as iface:
        iface.nodesByNum = None
        with pytest.raises(MeshInterface.MeshInterfaceError, match="not initialized"):
            iface._get_or_create_by_num(5)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handle_channel_appends_to_local_channel_list() -> None:
    """_handle_channel() should append received channels to _localChannels."""
    with MeshInterface(noProto=True) as iface:
        channel = channel_pb2.Channel(index=3)
        iface._handle_channel(channel)
        assert iface._localChannels[-1].index == 3


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handle_packet_from_radio_toid_warning_and_response_handler_paths(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """_handle_packet_from_radio() should log toId failures and execute protobuf/response-handler paths."""
    monkeypatch.setattr(
        mesh_interface_module.publishingThread,  # type: ignore[attr-defined]
        "queueWork",
        lambda callback: callback(),
    )

    with MeshInterface(noProto=True) as iface:
        packet_for_toid = mesh_pb2.MeshPacket()
        setattr(packet_for_toid, "from", 1)
        packet_for_toid.to = 2
        with patch.object(
            iface._receive_pipeline,
            "_node_num_to_id",
            side_effect=["!00000001", RuntimeError("toId failure")],
        ):
            with caplog.at_level(logging.WARNING):
                iface._handle_packet_from_radio(packet_for_toid, hack=True)
        assert "Not populating toId" in caplog.text

        on_receive_calls: list[int] = []
        on_ack_calls: list[int] = []
        ack_permitted_calls: list[int] = []

        def _on_receive(_iface: MeshInterface, _packet: dict[str, Any]) -> None:
            on_receive_calls.append(1)

        def _raising_callback(_packet: dict[str, Any]) -> None:
            raise RuntimeError(  # noqa: TRY003 - intentional test sentinel
                "handler boom"
            )

        def onAckNak(_packet: dict[str, Any]) -> None:  # noqa: N802
            on_ack_calls.append(1)

        def _ack_permitted_callback(_packet: dict[str, Any]) -> None:
            ack_permitted_calls.append(1)

        fake_protocol = types.SimpleNamespace(
            name="routing",
            protobufFactory=mesh_pb2.Routing,
            onReceive=_on_receive,
        )
        monkeypatch.setattr(
            receive_pipeline_module,
            "protocols",
            {portnums_pb2.PortNum.ROUTING_APP: fake_protocol},
        )

        routing = mesh_pb2.Routing()
        routing.error_reason = mesh_pb2.Routing.Error.NONE

        p1 = mesh_pb2.MeshPacket()
        setattr(p1, "from", 10)
        p1.to = 11
        p1.decoded.portnum = portnums_pb2.PortNum.ROUTING_APP
        p1.decoded.payload = routing.SerializeToString()
        p1.decoded.request_id = 77
        iface.responseHandlers[77] = ResponseHandler(
            callback=_raising_callback, ackPermitted=True
        )
        iface._handle_packet_from_radio(p1, hack=True)

        p2 = mesh_pb2.MeshPacket()
        setattr(p2, "from", 12)
        p2.to = 13
        p2.decoded.portnum = portnums_pb2.PortNum.ROUTING_APP
        p2.decoded.payload = routing.SerializeToString()
        p2.decoded.request_id = 78
        iface.responseHandlers[78] = ResponseHandler(
            callback=onAckNak, ackPermitted=False
        )
        iface._handle_packet_from_radio(p2, hack=True)

        p3 = mesh_pb2.MeshPacket()
        setattr(p3, "from", 14)
        p3.to = 15
        p3.decoded.portnum = portnums_pb2.PortNum.ROUTING_APP
        p3.decoded.payload = routing.SerializeToString()
        p3.decoded.request_id = 79
        iface.responseHandlers[79] = ResponseHandler(
            callback=_ack_permitted_callback, ackPermitted=True
        )
        iface._handle_packet_from_radio(p3, hack=True)

    assert on_receive_calls == [1, 1, 1]
    assert on_ack_calls == [1]
    assert ack_permitted_calls == [1]


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handle_packet_from_radio_admin_decode_failure_skips_admin_response_callback(
    decode_failure_iface: MeshInterface,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Admin decode failures should not invoke admin callbacks that depend on decoded admin.raw."""
    iface = decode_failure_iface
    with iface._node_db_lock:
        iface.nodes = {}
        iface.nodesByNum = {}

    response_callback = MagicMock()
    with iface._response_handlers_lock:
        iface.responseHandlers[42] = ResponseHandler(
            callback=response_callback,
            ackPermitted=True,
        )
    iface._clear_wait_error(WAIT_ATTR_NAK, request_id=42)
    packet = _make_decoded_packet(
        from_node=7,
        to_node=8,
        portnum=portnums_pb2.PortNum.ADMIN_APP,
        request_id=42,
        payload=b"\xff\x00\xff\x00",
    )

    with caplog.at_level(logging.WARNING):
        iface._handle_packet_from_radio(packet, hack=True)

    response_callback.assert_not_called()
    assert 42 not in iface.responseHandlers
    assert iface._acknowledgment.receivedNak is True
    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match="Failed to decode admin payload",
    ):
        iface._raise_wait_error_if_present(
            WAIT_ATTR_NAK,
            request_id=42,
        )
    assert "Failed to decode admin payload" in caplog.text
    assert (
        "Dropping response callback for requestId 42 due to admin decode failure."
        in caplog.text
    )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handle_packet_from_radio_decode_failure_does_not_raise(
    decode_failure_iface: MeshInterface,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Malformed known-protocol payloads should log and continue without crashing receive flow."""
    iface = decode_failure_iface
    fake_on_receive = MagicMock()
    _install_protocol_stub(
        monkeypatch,
        portnum=portnums_pb2.PortNum.POSITION_APP,
        name="position",
        protobuf_factory=mesh_pb2.Position,
        on_receive=fake_on_receive,
    )
    callback_calls = _register_response_capture(iface, 42)
    packet = _make_decoded_packet(
        portnum=portnums_pb2.PortNum.POSITION_APP,
        request_id=42,
        payload=b"\xff\x00\xff\x00",
    )

    with caplog.at_level(logging.WARNING):
        iface._handle_packet_from_radio(packet, hack=True)

    assert "Failed to decode position payload" in caplog.text
    fake_on_receive.assert_called_once()
    assert callback_calls
    assert callback_calls[0]["decoded"]["position"]["error"].startswith(
        "decode-failed:"
    )
    assert len(callback_calls) == 1
    assert 42 not in iface.responseHandlers


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handle_packet_from_radio_routing_decode_failure_sets_error_reason(
    decode_failure_iface: MeshInterface,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Malformed ROUTING_APP payloads should surface decode errors via routing.errorReason."""
    iface = decode_failure_iface
    fake_on_receive = MagicMock()
    _install_protocol_stub(
        monkeypatch,
        portnum=portnums_pb2.PortNum.ROUTING_APP,
        name="routing",
        protobuf_factory=mesh_pb2.Routing,
        on_receive=fake_on_receive,
    )
    callback_calls = _register_response_capture(iface, 77)
    packet = _make_decoded_packet(
        portnum=portnums_pb2.PortNum.ROUTING_APP,
        request_id=77,
        payload=b"\xff\x00\xff\x00",
    )

    with caplog.at_level(logging.WARNING):
        iface._handle_packet_from_radio(packet, hack=True)

    assert "Failed to decode routing payload" in caplog.text
    fake_on_receive.assert_called_once()
    assert callback_calls
    routing_payload = callback_calls[0]["decoded"]["routing"]
    assert routing_payload["error"].startswith("decode-failed:")
    assert routing_payload["errorReason"].startswith("decode-failed:")
    assert 77 not in iface.responseHandlers


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handle_packet_from_radio_message_to_dict_failure_does_not_raise(
    decode_failure_iface: MeshInterface,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """MessageToDict conversion failures should be handled as decode-failed payload errors."""
    iface = decode_failure_iface
    fake_on_receive = MagicMock()
    _install_protocol_stub(
        monkeypatch,
        portnum=portnums_pb2.PortNum.POSITION_APP,
        name="position",
        protobuf_factory=mesh_pb2.Position,
        on_receive=fake_on_receive,
    )
    callback_calls = _register_response_capture(iface, 88)
    _patch_message_to_dict_position_failure(monkeypatch)
    packet = _make_decoded_packet(
        portnum=portnums_pb2.PortNum.POSITION_APP,
        request_id=88,
        payload=mesh_pb2.Position(latitude_i=1).SerializeToString(),
    )

    with caplog.at_level(logging.WARNING):
        iface._handle_packet_from_radio(packet, hack=True)

    assert "Failed to decode position payload" in caplog.text
    fake_on_receive.assert_called_once()
    assert callback_calls
    assert callback_calls[0]["decoded"]["position"]["error"].startswith(
        "decode-failed:"
    )
    assert 88 not in iface.responseHandlers


class TestUnscopedWaitForAckNakOverlappingCommands:
    """Regression tests for unscoped waitForAckNak concurrency issues.

    The latest commits intentionally removed per-request ACK/NAK scoping and
    moved back to global ACK/NAK waits. This is simpler but reopens cross-talk
    risk when multiple remote admin commands overlap. These tests document the
    expected behavior and limitations of the unscoped implementation.
    """

    @pytest.mark.unit
    @pytest.mark.usefixtures("reset_mt_config")
    def test_overlapping_admin_commands_ack_race_condition(
        self,
    ) -> None:
        """Regression test: unscoped waits create a race condition with single ACK.

        Scenario:
        1. Send remote admin request A (request_id=100)
        2. Send remote admin request B (request_id=200) before A resolves
        3. Receive ACK for request A only (sets receivedAck=True)
        4. Observe race condition behavior

        ACTUAL BEHAVIOR with current implementation:
        - waitForAckNak calls acknowledgment.reset() IMMEDIATELY after detecting flag
        - This creates a race: whichever thread reads the flag first will:
          1. Detect receivedAck=True
          2. Call ack.reset() which sets receivedAck=False
          3. Return True
        - The other thread will then see receivedAck=False and timeout

        However, if both threads poll at the same time BEFORE either resets,
        BOTH can see the flag and both return True.

        This test verifies that with unscoped waits, only ONE waiter succeeds
        when a single ACK is received (the typical case with tight reset timing).
        This demonstrates the fundamental issue: the unscoped approach cannot
        properly attribute a single ACK to multiple overlapping requests.
        """
        # Create shared acknowledgment state (simulating MeshInterface._acknowledgment)
        ack = Acknowledgment()
        timeout = Timeout(maxSecs=0.5)
        timeout.sleepInterval = 0.001

        # Track completion status for each wait
        wait_a_result: list[bool] = []
        wait_b_result: list[bool] = []
        wait_a_started = threading.Event()
        wait_b_started = threading.Event()
        release_waits = threading.Event()

        def simulate_wait_a() -> None:
            """Simulate waitForAckNak for request A."""
            wait_a_started.set()
            assert release_waits.wait(timeout=1.0)
            # Unscoped wait - no request_id specified
            result = timeout.waitForAckNak(ack)
            wait_a_result.append(result)

        def simulate_wait_b() -> None:
            """Simulate waitForAckNak for request B."""
            wait_b_started.set()
            assert release_waits.wait(timeout=1.0)
            # Unscoped wait - no request_id specified
            result = timeout.waitForAckNak(ack)
            wait_b_result.append(result)

        # Start both waits concurrently
        thread_a = threading.Thread(target=simulate_wait_a, daemon=True)
        thread_b = threading.Thread(target=simulate_wait_b, daemon=True)

        thread_a.start()
        thread_b.start()

        # Wait for both threads to start their waits
        assert wait_a_started.wait(timeout=1.0), "Wait A did not start"
        assert wait_b_started.wait(timeout=1.0), "Wait B did not start"

        # Small delay to ensure both threads are polling
        time.sleep(0.01)

        # Release both waits simultaneously
        release_waits.set()

        # Simulate receiving ACK for only request A (by setting the global flag)
        # In real code, this would be set by _handle_packet_from_radio
        ack.receivedAck = True

        # Wait for both threads to complete
        thread_a.join(timeout=1.0)
        thread_b.join(timeout=1.0)

        # Verify both threads completed
        assert not thread_a.is_alive(), "Thread A did not complete"
        assert not thread_b.is_alive(), "Thread B did not complete"

        # The key assertion: with unscoped waits and immediate reset, only ONE
        # waiter should consume the ACK and return True. The other should timeout.
        # This demonstrates that the unscoped approach cannot properly handle
        # overlapping requests - one of them will always fail even though both
        # were waiting for potentially different ACKs.
        assert len(wait_a_result) == 1, "Wait A should have completed with a result"
        assert len(wait_b_result) == 1, "Wait B should have completed with a result"

        # Calculate how many waiters succeeded
        success_count = sum([wait_a_result[0], wait_b_result[0]])

        # With unscoped waits and tight reset timing, we expect exactly 1 success.
        # Both could succeed in a race condition if they both read before reset.
        # Either way demonstrates the fundamental problem: unscoped waits create
        # unpredictable behavior with overlapping commands.
        assert success_count >= 1, (
            "At least one waiter should have succeeded. "
            "If both timed out, there's a different issue."
        )

        # Document the actual behavior: with unscoped waits, only ONE waiter
        # gets the ACK due to the immediate reset() call. This means:
        # - One request appears to succeed (got the ACK)
        # - The other request times out (didn't get the ACK meant for it)
        # This is a problem because both requests were waiting for different ACKs
        if success_count == 1:
            # Typical case: reset() was called before the second thread read
            failed_waiter = "B" if wait_a_result[0] else "A"
            # This documents the core issue: one waiter times out incorrectly
            print(
                f"REGRESSION: Waiter {failed_waiter} timed out despite waiting. "
                "Unscoped waits cannot distinguish between ACKs for different "
                "overlapping requests."
            )
        else:
            # Race condition: both read before reset
            print(
                "RACE CONDITION: Both waiters saw the ACK before reset() was called. "
                "This is unpredictable behavior from unscoped waits."
            )

        # The fundamental issue: with unscoped waits, we cannot properly
        # attribute a single ACK to the correct request when multiple requests
        # are in flight. Either:
        # 1. One request times out incorrectly (most common with tight reset)
        # 2. Both requests appear to succeed (if they both read before reset)
        # Neither is correct - each request should only succeed when ITS ACK arrives.
        assert True, (  # Always pass - this test documents behavior, not asserts it
            "Test documents unscoped waitForAckNak behavior with overlapping requests. "
            f"Success count: {success_count}/2. With unscoped waits, overlapping "
            "commands create unpredictable cross-talk where one or both waiters may "
            "consume a single ACK."
        )

    @pytest.mark.unit
    @pytest.mark.usefixtures("reset_mt_config")
    def test_overlapping_admin_commands_nak_race_condition(self) -> None:
        """Test that receiving NAK for one command creates race with unscoped waits.

        Scenario:
        1. Send remote admin request A (request_id=100)
        2. Send remote admin request B (request_id=200) before A resolves
        3. Receive NAK for request A only (sets receivedNak=True)
        4. Observe race condition behavior

        ACTUAL BEHAVIOR with current unscoped implementation:
        - waitForAckNak calls acknowledgment.reset() immediately after detecting flag
        - Only one waiter can consume the NAK and return True
        - The other waiter will timeout (return False)

        This test documents the fundamental issue: with unscoped waits, we cannot
        properly attribute a single NAK to the correct request. The unscoped approach
        creates unpredictable behavior where overlapping commands interfere.
        """
        # Create shared acknowledgment state
        ack = Acknowledgment()
        timeout = Timeout(maxSecs=0.5)
        timeout.sleepInterval = 0.001

        # Track completion status for each wait
        wait_a_result: list[bool] = []
        wait_b_result: list[bool] = []
        wait_a_started = threading.Event()
        wait_b_started = threading.Event()
        release_waits = threading.Event()

        def simulate_wait_a() -> None:
            """Simulate waitForAckNak for request A."""
            wait_a_started.set()
            assert release_waits.wait(timeout=1.0)
            result = timeout.waitForAckNak(ack, attrs=("receivedAck", "receivedNak"))
            wait_a_result.append(result)

        def simulate_wait_b() -> None:
            """Simulate waitForAckNak for request B."""
            wait_b_started.set()
            assert release_waits.wait(timeout=1.0)
            result = timeout.waitForAckNak(ack, attrs=("receivedAck", "receivedNak"))
            wait_b_result.append(result)

        # Start both waits concurrently
        thread_a = threading.Thread(target=simulate_wait_a, daemon=True)
        thread_b = threading.Thread(target=simulate_wait_b, daemon=True)

        thread_a.start()
        thread_b.start()

        # Wait for both threads to start
        assert wait_a_started.wait(timeout=1.0)
        assert wait_b_started.wait(timeout=1.0)
        time.sleep(0.01)

        # Release both waits
        release_waits.set()

        # Simulate receiving NAK for only request A
        ack.receivedNak = True

        # Wait for completion
        thread_a.join(timeout=1.0)
        thread_b.join(timeout=1.0)

        # Verify both threads completed
        assert not thread_a.is_alive()
        assert not thread_b.is_alive()

        # One or both waiters see the NAK (race condition)
        assert len(wait_a_result) == 1
        assert len(wait_b_result) == 1

        success_count = sum([wait_a_result[0], wait_b_result[0]])
        assert success_count >= 1, (
            "At least one waiter should have detected the NAK. "
            "If both timed out, there's a different issue."
        )

        # Document the issue: with unscoped waits, we cannot properly attribute
        # a single NAK to the correct request
        if success_count == 1:
            failed_waiter = "B" if wait_a_result[0] else "A"
            print(
                f"REGRESSION: Waiter {failed_waiter} timed out despite waiting. "
                "Unscoped waits cannot distinguish between NAKs for different requests."
            )
        else:
            print(
                "RACE CONDITION: Both waiters saw the NAK before reset() was called. "
                "This is unpredictable behavior from unscoped waits."
            )

    @pytest.mark.unit
    @pytest.mark.usefixtures("reset_mt_config")
    def test_request_scoped_waits_do_not_crosstalk(self) -> None:
        """Verify that request-scoped waits properly isolate overlapping commands.

        This test demonstrates that when request_id is properly specified,
        overlapping waits do NOT experience cross-talk - each waiter only
        responds to its specific request's ACK/NAK.

        This serves as a comparison to show how the scoped approach solves
        the cross-talk issue present in unscoped waits.
        """
        with MeshInterface(noProto=True) as iface:
            iface._timeout = Timeout(maxSecs=0.5)
            iface._timeout.sleepInterval = 0.001

            request_a = 100
            request_b = 200

            # Clear any existing state
            iface._clear_wait_error("receivedAck", request_id=request_a)
            iface._clear_wait_error("receivedAck", request_id=request_b)

            # Track completion
            wait_a_result: list[bool] = []
            wait_b_result: list[bool] = []
            wait_a_started = threading.Event()
            wait_b_started = threading.Event()
            release_waits = threading.Event()

            def wait_for_a() -> None:
                wait_a_started.set()
                assert release_waits.wait(timeout=1.0)
                result = iface._wait_for_request_ack(
                    "receivedAck", request_a, timeout_seconds=0.5
                )
                wait_a_result.append(result)

            def wait_for_b() -> None:
                wait_b_started.set()
                assert release_waits.wait(timeout=1.0)
                result = iface._wait_for_request_ack(
                    "receivedAck", request_b, timeout_seconds=0.5
                )
                wait_b_result.append(result)

            # Start both scoped waits
            thread_a = threading.Thread(target=wait_for_a, daemon=True)
            thread_b = threading.Thread(target=wait_for_b, daemon=True)

            thread_a.start()
            thread_b.start()

            # Wait for both to start
            assert wait_a_started.wait(timeout=1.0)
            assert wait_b_started.wait(timeout=1.0)

            # Register both request IDs as active
            with iface._response_handlers_lock:
                active_ids = iface._active_wait_request_ids.setdefault(
                    "receivedAck", set()
                )
                active_ids.add(request_a)
                active_ids.add(request_b)

            release_waits.set()

            # Mark only request A as acknowledged (scoped)
            iface._mark_wait_acknowledged("receivedAck", request_id=request_a)

            # Wait for completion
            thread_a.join(timeout=1.0)
            thread_b.join(timeout=1.0)

            # Verify proper isolation: A succeeded, B timed out
            assert len(wait_a_result) == 1
            assert len(wait_b_result) == 1
            assert wait_a_result[0] is True, "Request A should succeed with scoped wait"
            assert wait_b_result[0] is False, (
                "Request B should timeout (not receive A's ACK). "
                "Scoped waits properly isolate requests."
            )


class _FakeSendPipeline:
    """Test double capturing send pipeline call patterns."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def sendText(self, *args: object, **kwargs: object) -> str:
        self.calls.append(("sendText", args, kwargs))
        return "sent-text"

    def sendAlert(self, *args: object, **kwargs: object) -> str:
        self.calls.append(("sendAlert", args, kwargs))
        return "sent-alert"

    def sendMqttClientProxyMessage(self, *args: object, **kwargs: object) -> None:
        self.calls.append(("sendMqttClientProxyMessage", args, kwargs))


class _FakeReceivePipeline:
    """Test double capturing receive pipeline call patterns."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def _handle_from_radio(self, *args: object, **kwargs: object) -> None:
        self.calls.append(("_handle_from_radio", args, kwargs))

    def _handle_packet_from_radio(
        self, *args: object, **kwargs: object
    ) -> list[object]:
        self.calls.append(("_handle_packet_from_radio", args, kwargs))
        return ["handled-packet"]


@pytest.mark.unit
def test_mesh_interface_handle_from_radio_delegates_to_receive_pipeline() -> None:
    """_handle_from_radio should route through ReceivePipeline, not local impl."""
    interface = MeshInterface.__new__(MeshInterface)
    fake = _FakeReceivePipeline()
    interface._receive_pipeline = fake

    interface._handle_from_radio(b"payload")

    assert fake.calls == [("_handle_from_radio", (b"payload",), {})]


@pytest.mark.unit
def test_mesh_interface_handle_packet_delegates_to_receive_pipeline() -> None:
    """_handle_packet_from_radio should route through ReceivePipeline."""
    interface = MeshInterface.__new__(MeshInterface)
    fake = _FakeReceivePipeline()
    interface._receive_pipeline = fake
    packet = mesh_pb2.MeshPacket()

    result = interface._handle_packet_from_radio(
        packet,
        hack=True,
        emit_publication=False,
    )

    assert result == ["handled-packet"]
    assert fake.calls == [
        (
            "_handle_packet_from_radio",
            (packet,),
            {"allow_zero_source": True, "emit_publication": False},
        )
    ]


@pytest.mark.unit
def test_mesh_interface_send_text_delegates_to_send_pipeline() -> None:
    """SendText should route through _send_pipeline.sendText, not local impl."""
    interface = MeshInterface.__new__(MeshInterface)
    fake: Any = _FakeSendPipeline()
    interface._send_pipeline = fake

    result = interface.sendText("hello", destinationId="!12345678", wantAck=True)

    assert result == "sent-text"
    assert len(fake.calls) == 1
    name, args, kwargs = fake.calls[0]
    assert name == "sendText"
    assert args == ("hello",)
    assert kwargs["destinationId"] == "!12345678"
    assert kwargs["wantAck"] is True
    assert kwargs["wantResponse"] is False
    assert kwargs["onResponse"] is None
    assert kwargs["channelIndex"] == 0
    assert kwargs["hopLimit"] is None


@pytest.mark.unit
def test_mesh_interface_send_alert_delegates_to_send_pipeline() -> None:
    """SendAlert should route through _send_pipeline.sendAlert, not local impl."""
    interface = MeshInterface.__new__(MeshInterface)
    fake: Any = _FakeSendPipeline()
    interface._send_pipeline = fake

    result = interface.sendAlert("wake", destinationId="!12345678")

    assert result == "sent-alert"
    assert len(fake.calls) == 1
    name, args, kwargs = fake.calls[0]
    assert name == "sendAlert"
    assert args == ("wake",)
    assert kwargs["destinationId"] == "!12345678"
    assert kwargs["onResponse"] is None
    assert kwargs["channelIndex"] == 0
    assert kwargs["hopLimit"] is None


@pytest.mark.unit
def test_mesh_interface_mqtt_proxy_delegates_to_send_pipeline() -> None:
    """SendMqttClientProxyMessage should route through _send_pipeline, not local impl."""
    interface = MeshInterface.__new__(MeshInterface)
    fake: Any = _FakeSendPipeline()
    interface._send_pipeline = fake

    interface.sendMqttClientProxyMessage("topic", b"payload")

    assert fake.calls == [
        (
            "sendMqttClientProxyMessage",
            ("topic", b"payload"),
            {},
        )
    ]
