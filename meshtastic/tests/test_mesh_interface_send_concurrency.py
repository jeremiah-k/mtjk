"""Meshtastic unit tests for mesh_interface.py."""

# pylint: disable=too-many-lines

import io
import logging
import sys
import threading
import time
import types
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

import meshtastic.mesh_interface as mesh_interface_module
from meshtastic.mesh_interface_runtime import flows as flows_module
from meshtastic.mesh_interface_runtime.request_wait import (
    WAIT_ATTR_POSITION,
    WAIT_ATTR_TELEMETRY,
    WAIT_ATTR_TRACEROUTE,
    WAIT_ATTR_WAYPOINT,
)
from meshtastic.traceroute import TraceRouteResult

from ..mesh_interface import MeshInterface
from ..protobuf import (
    channel_pb2,
    mesh_pb2,
    portnums_pb2,
    telemetry_pb2,
)
from ._mesh_interface_legacy_support import (
    _start_wait_thread,
    _wait_for_scoped_wait_registration,
)

# TODO
# from ..config import Config


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_concurrent_node_database_access() -> None:
    """Test that node database access is thread-safe."""
    with MeshInterface(noProto=True) as iface:
        iface.nodes = {}
        iface.nodesByNum = {}
        errors: list[BaseException] = []
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
        errors: list[BaseException] = []
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
        errors: list[BaseException] = []
        added_ids: list[int] = []
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
    errors: list[BaseException] = []
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

        errors: list[BaseException] = []
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
        errors: list[BaseException] = []
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
@pytest.mark.usefixtures("reset_mt_config")
def test_packet_id_counter_prevents_collision_until_counter_wrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fixed random prefix should leave the 10-bit counter unique until wrap."""
    random_prefix = 0x15555
    monkeypatch.setattr(
        "meshtastic.mesh_interface.random.randint",
        lambda _a, _b: random_prefix,
    )

    with MeshInterface(noProto=True) as iface:
        iface.currentPacketId = 0
        packet_ids = [iface._generate_packet_id() for _ in range(1 << 10)]
        wrapped_packet_id = iface._generate_packet_id()

    assert len(set(packet_ids)) == 1 << 10
    assert wrapped_packet_id == packet_ids[0]
    assert all(packet_id >> 10 == random_prefix for packet_id in packet_ids)


@pytest.mark.unit
def test_concurrent_sendText_with_queue() -> None:
    """Test that sendText() with queue is thread-safe."""
    with MeshInterface(noProto=True) as iface:
        iface.myInfo = MagicMock()
        iface.myInfo.my_node_num = 12345
        iface._localChannels = [channel_pb2.Channel(index=0)]
        errors: list[BaseException] = []
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
    real_close = iface.close
    iface.close = MagicMock(side_effect=RuntimeError("close failed"))  # type: ignore[method-assign]

    try:
        with caplog.at_level(logging.WARNING):
            iface.__exit__(ValueError, ValueError("inner"), None)
        assert "close() failed while unwinding an existing exception." in caplog.text

        with pytest.raises(RuntimeError, match="close failed"):
            iface.__exit__(None, None, None)
    finally:
        iface.close = real_close  # type: ignore[method-assign]
        iface.close()


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
        monkeypatch.setattr(iface._send_pipeline, "send_alert", send_alert)
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
        monkeypatch.setattr(
            iface._send_pipeline, "send_mqtt_client_proxy_message", send_mqtt
        )
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


def _assert_position_response_log(
    iface: MeshInterface,
    caplog: pytest.LogCaptureFixture,
    *,
    request_id: int,
    position: mesh_pb2.Position,
    expected: tuple[str, ...],
) -> None:
    """Drive one position response and assert its waiter and log output.

    Parameters
    ----------
    iface : MeshInterface
        Interface receiving the position response.
    caplog : pytest.LogCaptureFixture
        Log capture fixture used to inspect the position summary.
    request_id : int
        Scoped request identifier for the response.
    position : mesh_pb2.Position
        Position payload delivered to the response handler.
    expected : tuple[str, ...]
        Log substrings expected after the response is processed.
    """
    iface._clear_wait_error(WAIT_ATTR_POSITION, request_id=request_id)
    wait_thread, wait_errors = _start_wait_thread(
        lambda: iface.waitForPosition(request_id=request_id)
    )
    _wait_for_scoped_wait_registration(
        iface,
        acknowledgment_attr=WAIT_ATTR_POSITION,
        request_id=request_id,
    )
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=flows_module.__name__):
        iface.onResponsePosition(
            {
                "decoded": {
                    "requestId": request_id,
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
    for text in expected:
        assert text in caplog.text


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
        _assert_position_response_log(
            iface,
            caplog,
            request_id=1001,
            position=position,
            expected=("Position received:", "full precision"),
        )

        unknown_position = mesh_pb2.Position()
        unknown_position.precision_bits = 5
        _assert_position_response_log(
            iface,
            caplog,
            request_id=1002,
            position=unknown_position,
            expected=("(unknown)", "precision:5"),
        )

        disabled_position = mesh_pb2.Position()
        disabled_position.precision_bits = 0
        _assert_position_response_log(
            iface,
            caplog,
            request_id=1003,
            position=disabled_position,
            expected=("position disabled",),
        )

    with MeshInterface(noProto=True) as iface:
        iface._clear_wait_error(WAIT_ATTR_POSITION, request_id=1004)
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
        iface._clear_wait_error(WAIT_ATTR_TRACEROUTE, request_id=88)
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
def test_request_traceroute_returns_structured_routes(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Library traceroutes should return typed paths without CLI-oriented logging."""
    with MeshInterface(noProto=True) as iface:
        iface.nodes = {
            "!0000000b": {"num": 11},
            "!0000000c": {"num": 12},
            "!00000014": {"num": 20},
            "!00000015": {"num": 21},
        }
        response = mesh_pb2.RouteDiscovery()
        response.route.extend([11])
        response.snr_towards.extend([8, 12])
        response.route_back.extend([12])
        response.snr_back.extend([16, 20])
        sent_packet = mesh_pb2.MeshPacket(id=88)
        response_callback: Callable[[dict[str, Any]], None] | None = None

        def _send_data_with_response(
            _payload: object, **kwargs: Any
        ) -> mesh_pb2.MeshPacket:
            nonlocal response_callback
            response_callback = cast(
                Callable[[dict[str, Any]], None], kwargs["onResponse"]
            )
            return sent_packet

        def _wait_for_response(
            wait_factor: float, request_id: int | None = None
        ) -> None:
            assert wait_factor == 3
            assert request_id == 88
            assert response_callback is not None
            response_callback(
                {
                    "decoded": {
                        "payload": response.SerializeToString(),
                        "requestId": 88,
                    },
                    "to": 20,
                    "from": 21,
                    "hopStart": 1,
                }
            )

        monkeypatch.setattr(iface, "_send_data_with_wait", _send_data_with_response)
        monkeypatch.setattr(iface, "waitForTraceRoute", _wait_for_response)
        with caplog.at_level(logging.INFO, logger=flows_module.__name__):
            result = iface.requestTraceRoute(dest=21, hopLimit=3, channelIndex=1)

    assert result.request_id == 88
    assert [hop.node_num for hop in result.route_towards] == [20, 11, 21]
    assert [hop.snr_db for hop in result.route_towards] == [None, 2.0, 3.0]
    assert result.route_back is not None
    assert [hop.node_num for hop in result.route_back] == [21, 12, 20]
    assert [hop.snr_db for hop in result.route_back] == [None, 4.0, 5.0]
    assert result.source.node_num == 20
    assert result.destination.node_num == 21
    assert "Route traced" not in caplog.text


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_request_traceroute_preserves_unknown_link_snr() -> None:
    """Incomplete firmware SNR arrays should produce unknown links, not bad indexing."""
    with MeshInterface(noProto=True) as iface:
        response = mesh_pb2.RouteDiscovery()
        response.route.extend([11])
        response.snr_towards.extend([8])
        result = (
            flows_module._on_response_traceroute(  # pylint: disable=protected-access
                iface,
                {
                    "decoded": {
                        "payload": response.SerializeToString(),
                        "requestId": 89,
                    },
                    "to": 20,
                    "from": 21,
                },
                emit_summary=False,
            )
        )

    assert result is not None
    assert [hop.snr_db for hop in result.route_towards] == [None, None, None]
    assert result.route_back is None


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_request_traceroute_preserves_reverse_route_with_unknown_snr() -> None:
    """Reverse topology should survive incomplete firmware SNR arrays."""
    with MeshInterface(noProto=True) as iface:
        response = mesh_pb2.RouteDiscovery()
        response.route_back.extend([12])
        response.snr_back.extend([16])
        result = (
            flows_module._on_response_traceroute(  # pylint: disable=protected-access
                iface,
                {
                    "decoded": {
                        "payload": response.SerializeToString(),
                        "requestId": 90,
                    },
                    "to": 20,
                    "from": 21,
                    "hopStart": 1,
                },
                emit_summary=False,
            )
        )

    assert result is not None
    assert result.route_back is not None
    assert [hop.node_num for hop in result.route_back] == [21, 12, 20]
    assert [hop.snr_db for hop in result.route_back] == [None, None, None]


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_request_traceroute_stores_result_before_releasing_waiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The response result must be visible before acknowledgment wakes the waiter."""
    with MeshInterface(noProto=True) as iface:
        response = mesh_pb2.RouteDiscovery()
        response.route.extend([11])
        response.snr_towards.extend([8, 12])
        sent_packet = mesh_pb2.MeshPacket(id=91)
        response_callback: Callable[[dict[str, Any]], None] | None = None
        request_finished = threading.Event()
        outcome: dict[str, object] = {}

        def _send_data_with_response(
            _payload: object, **kwargs: Any
        ) -> mesh_pb2.MeshPacket:
            nonlocal response_callback
            response_callback = cast(
                Callable[[dict[str, Any]], None], kwargs["onResponse"]
            )
            iface._clear_wait_error(WAIT_ATTR_TRACEROUTE, request_id=91)
            return sent_packet

        original_mark_acknowledged = iface._mark_wait_acknowledged

        def _mark_and_hold(*args: Any, **kwargs: Any) -> None:
            original_mark_acknowledged(*args, **kwargs)
            assert request_finished.wait(timeout=1.0)

        def _request() -> None:
            try:
                outcome["result"] = iface.requestTraceRoute(
                    dest=21, hopLimit=3, channelIndex=1
                )
            except Exception as exc:  # pragma: no cover - asserted below
                outcome["error"] = exc
            finally:
                request_finished.set()

        monkeypatch.setattr(iface, "_send_data_with_wait", _send_data_with_response)
        monkeypatch.setattr(iface, "_mark_wait_acknowledged", _mark_and_hold)
        request_thread = threading.Thread(target=_request, daemon=True)
        request_thread.start()
        _wait_for_scoped_wait_registration(
            iface, acknowledgment_attr=WAIT_ATTR_TRACEROUTE, request_id=91
        )
        assert response_callback is not None
        response_callback(
            {
                "decoded": {
                    "payload": response.SerializeToString(),
                    "requestId": 91,
                },
                "to": 20,
                "from": 21,
            }
        )
        request_thread.join(timeout=1.0)

    assert not request_thread.is_alive()
    assert "error" not in outcome
    result = cast(TraceRouteResult, outcome["result"])
    assert result.request_id == 91
    assert [hop.node_num for hop in result.route_towards] == [20, 11, 21]


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_request_traceroute_keeps_first_structured_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Duplicate route responses should not replace the first completed result."""
    with MeshInterface(noProto=True) as iface:
        first = mesh_pb2.RouteDiscovery()
        first.route.extend([11])
        first.snr_towards.extend([8, 12])
        second = mesh_pb2.RouteDiscovery()
        second.route.extend([12])
        second.snr_towards.extend([16, 20])
        sent_packet = mesh_pb2.MeshPacket(id=92)

        def _send_duplicate_responses(
            _payload: object, **kwargs: Any
        ) -> mesh_pb2.MeshPacket:
            callback = cast(Callable[[dict[str, Any]], None], kwargs["onResponse"])
            for route in (first, second):
                callback(
                    {
                        "decoded": {
                            "payload": route.SerializeToString(),
                            "requestId": 92,
                        },
                        "to": 20,
                        "from": 21,
                    }
                )
            return sent_packet

        monkeypatch.setattr(iface, "_send_data_with_wait", _send_duplicate_responses)
        monkeypatch.setattr(iface, "waitForTraceRoute", lambda *_args, **_kwargs: None)
        result = iface.requestTraceRoute(dest=21, hopLimit=3, channelIndex=1)

    assert [hop.node_num for hop in result.route_towards] == [20, 11, 21]


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_on_response_traceroute_routing_no_response_raises() -> None:
    """Traceroute routing NO_RESPONSE replies should be surfaced by waitForTraceRoute()."""
    with MeshInterface(noProto=True) as iface:
        iface._clear_wait_error(WAIT_ATTR_TRACEROUTE, request_id=9101)
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
        iface._clear_wait_error(WAIT_ATTR_TRACEROUTE, request_id=9102)
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
def test_request_traceroute_routing_ack_does_not_consume_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful routing ack must not be parsed as a route nor block the response.

    Firmware acks reliable traceroute requests (ROUTING_APP, Routing body)
    before the actual RouteDiscovery response arrives. The ack previously
    consumed the one-shot response handler and its body failed to parse as a
    RouteDiscovery, surfacing 'Wire format was corrupt' to the waiter.
    """
    with MeshInterface(noProto=True) as iface:
        iface.currentPacketId = 0
        iface.nodesByNum = {20: {"num": 20}, 21: {"num": 21}}
        iface.nodes = {"!14": {"num": 20}, "!15": {"num": 21}}

        def _fake_send(
            meshPacket: mesh_pb2.MeshPacket, *_args: Any, **_kwargs: Any
        ) -> mesh_pb2.MeshPacket:
            return meshPacket

        monkeypatch.setattr(iface, "_send_packet", _fake_send)
        request_finished = threading.Event()
        outcome: dict[str, object] = {}

        def _request() -> None:
            try:
                outcome["result"] = iface.requestTraceRoute(dest=21, hopLimit=3)
            except Exception as exc:  # noqa: BLE001 - asserted below
                outcome["error"] = exc
            finally:
                request_finished.set()

        request_thread = threading.Thread(target=_request, daemon=True)
        request_thread.start()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not iface.responseHandlers:
            time.sleep(0.005)
        assert iface.responseHandlers, "traceroute request never registered its handler"
        request_id = next(iter(iface.responseHandlers))
        _wait_for_scoped_wait_registration(
            iface, acknowledgment_attr=WAIT_ATTR_TRACEROUTE, request_id=request_id
        )

        # The routing ack arrives before the trace response.
        ack = mesh_pb2.MeshPacket()
        setattr(ack, "from", 21)
        ack.to = 20
        ack.decoded.portnum = portnums_pb2.PortNum.ROUTING_APP
        ack.decoded.request_id = request_id
        routing = mesh_pb2.Routing()
        routing.error_reason = mesh_pb2.Routing.Error.NONE
        ack.decoded.payload = routing.SerializeToString()
        iface._handle_packet_from_radio(ack, hack=True)

        # The ack is not the route: the wait stays pending and the response
        # handler was re-armed for the real response.
        assert not request_finished.is_set()
        assert request_id in iface.responseHandlers

        reply = mesh_pb2.MeshPacket()
        setattr(reply, "from", 21)
        reply.to = 20
        reply.decoded.portnum = portnums_pb2.PortNum.TRACEROUTE_APP
        reply.decoded.request_id = request_id
        response = mesh_pb2.RouteDiscovery()
        response.route.extend([11])
        response.snr_towards.extend([8, 12])
        reply.decoded.payload = response.SerializeToString()
        iface._handle_packet_from_radio(reply, hack=True)
        request_finished.wait(timeout=1.0)

    assert not request_thread.is_alive()
    assert "error" not in outcome
    result = cast(TraceRouteResult, outcome["result"])
    assert result.request_id == request_id
    assert [hop.node_num for hop in result.route_towards] == [20, 11, 21]


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
        iface._clear_wait_error(WAIT_ATTR_TELEMETRY, request_id=2001)
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
        iface._clear_wait_error(WAIT_ATTR_TELEMETRY, request_id=2002)
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
        iface._clear_wait_error(WAIT_ATTR_TELEMETRY, request_id=2003)
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

        iface._clear_wait_error(WAIT_ATTR_TELEMETRY, request_id=2004)
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
        iface._clear_wait_error(WAIT_ATTR_WAYPOINT, request_id=3001)
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
        iface._clear_wait_error(WAIT_ATTR_WAYPOINT, request_id=3002)
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
