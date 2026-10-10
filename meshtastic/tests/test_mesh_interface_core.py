"""Meshtastic unit tests for mesh_interface.py."""

# pylint: disable=too-many-lines

import builtins
import importlib.util
import io
import logging
import re
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, create_autospec, patch

import pytest
from hypothesis import given
from hypothesis import strategies as st

import meshtastic.mesh_interface as mesh_interface_module

from .. import BROADCAST_ADDR, LOCAL_ADDR
from ..mesh_interface import MeshInterface
from ..mesh_interface_runtime.node_view import _timeago
from ..node import Node
from ..protobuf import (
    config_pb2,
    mesh_pb2,
    portnums_pb2,
)

# TODO
# from ..config import Config
from ..util import Timeout

if TYPE_CHECKING:
    from .conftest import FakeTimer


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
def test_sendPosition_default_path_delegates_empty_position_payload() -> None:
    """Default position sends should use POSITION_APP without inventing coordinates."""
    sent_packet = mesh_pb2.MeshPacket(id=123)
    with (
        MeshInterface(noProto=True) as iface,
        patch.object(
            iface, "_send_data_with_wait", return_value=sent_packet
        ) as send_mock,
    ):
        result = iface.sendPosition()

        assert result is sent_packet
        send_call = send_mock.call_args
        assert send_call is not None
        position = send_call.args[0]
        assert isinstance(position, mesh_pb2.Position)
        assert position.ListFields() == []
        assert send_call.args[1] == BROADCAST_ADDR
        assert send_call.kwargs == {
            "portNum": portnums_pb2.PortNum.POSITION_APP,
            "wantAck": False,
            "wantResponse": False,
            "onResponse": None,
            "channelIndex": 0,
            "hopLimit": None,
            "response_wait_attr": None,
        }


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
            iface.showNodes(showFields=["user.longName", "user.id", "user.shortName"])
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
            iface.showNodes(showFields=["user.longName", "user.id"])
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
    with iface._node_db_lock:
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
    with iface._node_db_lock:
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
        id_fields = ["user.longName", "user.id"]
        iface.showNodes(showFields=id_fields)
        out_with_self, _ = capsys.readouterr()
        iface.showNodes(includeSelf=False, showFields=id_fields)
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
        packet_ids: list[int] = []
        errors: list[Exception] = []
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
