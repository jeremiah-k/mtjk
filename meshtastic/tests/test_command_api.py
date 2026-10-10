"""Exercise the public embedded entry point over a shared real interface."""

import importlib
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import cast
from unittest.mock import Mock

import pytest

from meshtastic import mt_config
from meshtastic._command_scope import _CommandScope
from meshtastic._core_constants import DECODE_ERROR_KEY
from meshtastic.commands import executeCommand, getCommandCapabilities
from meshtastic.errors import RequestRejectedError, RequestTimeoutError
from meshtastic.mesh_interface import MeshInterface
from meshtastic.protobuf import admin_pb2, channel_pb2, mesh_pb2, portnums_pb2

pytestmark = pytest.mark.unit


@pytest.fixture
def client(monkeypatch):
    importlib.import_module("meshtastic.__main__")
    with MeshInterface(noProto=True) as interface:
        interface.localNode.nodeNum = 1
        interface.myInfo = mesh_pb2.MyNodeInfo(my_node_num=1)
        interface.nodesByNum = {
            1: {
                "num": 1,
                "user": {"id": "!00000001", "longName": "Local"},
                "adminSessionPassKey": b"key",
            },
            2: {
                "num": 2,
                "user": {"id": "!00000002", "longName": "Remote"},
                "adminSessionPassKey": b"key",
            },
        }
        interface.nodes = {
            record["user"]["id"]: record for record in interface.nodesByNum.values()
        }
        interface.localNode.channels = [
            channel_pb2.Channel(index=0, role=channel_pb2.Channel.PRIMARY)
        ]
        interface.localNode.noProto = False
        interface.noProto = False
        interface.isConnected.set()
        monkeypatch.setattr(interface, "close", Mock(wraps=interface.close))
        yield interface
        interface.noProto = True


def deliver(client, packet):
    client._request_wait_runtime.correlate_inbound_response(
        packet_dict=packet,
        skip_response_callback_for_decode_failure=False,
        extract_request_id=client._extract_request_id_from_packet,
    )


def config_response(packet, *, hop_limit=3):
    raw = admin_pb2.AdminMessage()
    raw.get_config_response.lora.hop_limit = hop_limit
    return {
        "from": packet.to,
        "decoded": {"requestId": packet.id, "admin": {"raw": raw}},
    }


def test_nodes_capture_preserves_streams_globals_and_transport(client, capsys):
    stdout, stderr = sys.stdout, sys.stderr
    global_state = (
        mt_config.args,
        mt_config.parser,
        mt_config.channel_index,
        mt_config.camel_case,
        mt_config.logfile,
    )
    chunks = []
    result = executeCommand(client, ["--nodes", "--quiet"], output=chunks.append)
    assert result.succeeded and result.error is None
    assert "Local" in result.output and "Remote" in result.output
    assert "Connected to radio" not in result.output
    assert "".join(chunks) == result.output
    assert sys.stdout is stdout and sys.stderr is stderr
    assert global_state == (
        mt_config.args,
        mt_config.parser,
        mt_config.channel_index,
        mt_config.camel_case,
        mt_config.logfile,
    )
    client.close.assert_not_called()
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "argv",
    [
        ["--host", "localhost"],
        ["--listen"],
        ["--seriallog", "-"],
        ["--ota-update", "file"],
        ["--configure", "file"],
        ["--set", "lora.region"],
        ["--node"],
        [],
        "--nodes",
        [1],
    ],
)
def test_argument_failures_precede_actions_and_do_not_exit_process(
    client, monkeypatch, capsys, argv
):
    monkeypatch.setattr(
        client,
        "_send_to_radio_impl",
        Mock(side_effect=AssertionError("invalid command sent a packet")),
    )
    result = executeCommand(client, argv)
    assert result.exitCode == 2 and isinstance(result.error, ValueError)
    assert result.output
    client.close.assert_not_called()
    assert capsys.readouterr() == ("", "")


def test_capabilities_help_and_version_have_the_same_supported_options(client, capsys):
    capabilities = getCommandCapabilities()
    assert capabilities.apiVersion == 1
    assert (
        "--get" in capabilities.supportedOptions
        and "--sendtext" in capabilities.supportedOptions
    )
    assert (
        "--host" not in capabilities.supportedOptions
        and "--listen" not in capabilities.supportedOptions
    )
    help_result = executeCommand(client, ["--help"])
    assert (
        help_result.succeeded
        and "--get" in help_result.output
        and "--listen" not in help_result.output
    )
    assert executeCommand(client, ["--version"]).succeeded
    assert capsys.readouterr() == ("", "")


def test_get_uses_fresh_response_and_captures_quiet_preference_output(
    client, monkeypatch, capsys
):
    client.localNode.localConfig.lora.hop_limit = 7
    packets = []

    def send(envelope):
        packets.append(envelope.packet)
        deliver(client, config_response(envelope.packet))

    monkeypatch.setattr(client, "_send_to_radio_impl", send)
    result = executeCommand(client, ["--get", "lora.hopLimit", "--quiet"])
    assert result.succeeded
    assert "lora.hop_limit: 3" in result.output
    assert client.localNode.localConfig.lora.hop_limit == 7
    assert packets and client.responseHandlers == {}
    client.close.assert_not_called()
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("reject", [True, False])
def test_get_returns_typed_failure_and_retains_other_response_state(
    client, monkeypatch, reject
):
    sentinel = Mock()
    client.responseHandlers[1234] = sentinel

    def send(envelope):
        if reject:
            deliver(
                client,
                {
                    "from": 2,
                    "decoded": {
                        "requestId": envelope.packet.id,
                        "routing": {"errorReason": "NOT_AUTHORIZED"},
                    },
                },
            )

    monkeypatch.setattr(client, "_send_to_radio_impl", send)
    result = executeCommand(
        client, ["--dest", "!00000002", "--get", "lora.hopLimit"], timeout=0.03
    )
    assert result.exitCode == 1
    assert isinstance(
        result.error, RequestRejectedError if reject else RequestTimeoutError
    )
    assert client.responseHandlers == {1234: sentinel}
    assert client._response_wait_errors == {}
    client.close.assert_not_called()


@pytest.mark.parametrize(
    ("source", "reason", "rejected"),
    [
        (1, "PKI_FAILED", True),
        (2, "NOT_AUTHORIZED", True),
        (3, "PKI_FAILED", False),
        (0, "PKI_FAILED", False),
        (1, 0, False),
        (1, "NONE", False),
        (1, "malformed routing payload", False),
    ],
)
def test_command_scope_records_only_valid_routing_rejections(
    client: MeshInterface, source: int, reason: str | int, rejected: bool
) -> None:
    """A request ID alone is insufficient to admit a command-owned NAK."""
    scope = _CommandScope(client, lambda _message: None)
    request = mesh_pb2.MeshPacket(to=2, id=0xAABB)
    scope._track(request.id, None, packet=request)
    scope._record_routing_rejection(
        {
            "from": source,
            "decoded": {"requestId": request.id, "routing": {"errorReason": reason}},
        }
    )
    if rejected:
        with pytest.raises(RequestRejectedError):
            scope._raise_if_rejected()
    else:
        scope._raise_if_rejected()
    scope._cleanup()


@pytest.mark.parametrize(
    ("action", "operation"),
    [
        (["--get", "lora.hopLimit"], "get_config_request"),
        (["--get", "mqtt.enabled"], "get_module_config_request"),
        (["--get-ui-config"], "get_ui_config_request"),
        (["--request-connection-status"], "get_device_connection_status_request"),
        (["--reboot"], "command"),
        (["--get-canned-message"], "command"),
        (["--get-ringtone"], "command"),
    ],
)
def test_remote_admin_rejects_origin_router_nak_without_waiting_out_budget(
    client: MeshInterface,
    monkeypatch: pytest.MonkeyPatch,
    action: list[str],
    operation: str,
) -> None:
    packets: list[mesh_pb2.MeshPacket] = []
    sentinel = Mock()
    client.responseHandlers[1234] = sentinel
    client._acknowledgment.receivedNak = False

    def send(envelope: mesh_pb2.ToRadio) -> None:
        request = envelope.packet
        packets.append(request)
        nak = mesh_pb2.MeshPacket(to=1)
        setattr(nak, "from", 1)
        nak.decoded.portnum = portnums_pb2.PortNum.ROUTING_APP
        nak.decoded.request_id = request.id
        nak.decoded.payload = mesh_pb2.Routing(
            error_reason=mesh_pb2.Routing.Error.PKI_FAILED
        ).SerializeToString()
        client._handle_packet_from_radio(nak)

    monkeypatch.setattr(client, "_send_to_radio_impl", send)
    started = time.monotonic()
    result = executeCommand(client, ["--dest", "!00000002", *action], timeout=0.5)
    assert isinstance(result.error, RequestRejectedError), result
    assert result.exitCode == 1
    assert result.error.reason == "PKI_FAILED"
    assert result.error.nodeNum == 2
    assert result.error.requestId == packets[0].id
    assert result.error.operation == operation
    assert all(packet.pki_encrypted for packet in packets)
    assert time.monotonic() - started < 0.25
    assert client.responseHandlers == {1234: sentinel}
    assert client._response_wait_errors == {}
    assert client._acknowledgment.receivedNak is False
    cast(Mock, client.close).assert_not_called()


@pytest.mark.parametrize(
    "feedback",
    [
        "local_ack",
        "local_data",
        "local_decode_error",
        "other_nak",
        "other_id",
        "other_nak_legacy",
        "local_numeric_ack",
    ],
)
def test_remote_get_keeps_waiting_for_peer_after_unrelated_feedback(
    client: MeshInterface, monkeypatch: pytest.MonkeyPatch, feedback: str
) -> None:
    def send(envelope: mesh_pb2.ToRadio) -> None:
        request = envelope.packet
        packet = mesh_pb2.MeshPacket(to=1)
        setattr(
            packet, "from", 3 if feedback in {"other_nak", "other_nak_legacy"} else 1
        )
        packet.decoded.request_id = (
            request.id ^ 1 if feedback == "other_id" else request.id
        )
        if feedback in {"local_data", "local_decode_error"}:
            packet.decoded.portnum = portnums_pb2.PortNum.ADMIN_APP
            raw = admin_pb2.AdminMessage()
            raw.get_config_response.lora.hop_limit = 7
            packet.decoded.payload = (
                b"\xff" if feedback == "local_decode_error" else raw.SerializeToString()
            )
        else:
            packet.decoded.portnum = portnums_pb2.PortNum.ROUTING_APP
            packet.decoded.payload = mesh_pb2.Routing(
                error_reason=(
                    mesh_pb2.Routing.Error.NONE
                    if feedback in {"local_ack", "local_numeric_ack"}
                    else mesh_pb2.Routing.Error.PKI_FAILED
                )
            ).SerializeToString()
        client._handle_packet_from_radio(packet)
        assert request.id in client.responseHandlers
        setattr(packet, "from", 2)
        packet.decoded.portnum = portnums_pb2.PortNum.ADMIN_APP
        packet.decoded.request_id = request.id
        raw = admin_pb2.AdminMessage()
        raw.get_config_response.lora.hop_limit = 3
        packet.decoded.payload = raw.SerializeToString()
        client._handle_packet_from_radio(packet)

    monkeypatch.setattr(client, "_send_to_radio_impl", send)
    result = executeCommand(
        client, ["--dest", "!00000002", "--get", "lora.hopLimit"], timeout=0.5
    )
    assert result.succeeded, result.error
    assert "lora.hop_limit: 3" in result.output
    assert client.responseHandlers == {}
    assert client._response_wait_errors == {}


def test_remote_get_does_not_replace_timeout_with_a_late_routing_nak(
    client: MeshInterface, monkeypatch: pytest.MonkeyPatch
) -> None:
    def send(envelope: mesh_pb2.ToRadio) -> None:
        time.sleep(0.03)
        packet = mesh_pb2.MeshPacket(to=1)
        setattr(packet, "from", 1)
        packet.decoded.portnum = portnums_pb2.PortNum.ROUTING_APP
        packet.decoded.request_id = envelope.packet.id
        packet.decoded.payload = mesh_pb2.Routing(
            error_reason=mesh_pb2.Routing.Error.PKI_FAILED
        ).SerializeToString()
        client._handle_packet_from_radio(packet)

    monkeypatch.setattr(client, "_send_to_radio_impl", send)
    result = executeCommand(
        client, ["--dest", "!00000002", "--get", "lora.region"], timeout=0.01
    )
    assert isinstance(result.error, RequestTimeoutError), result
    assert client.responseHandlers == {}
    assert client._response_wait_errors == {}


@pytest.mark.parametrize("destination", [None, "^local", "!00000002"])
def test_text_ack_uses_resolved_destination(client, monkeypatch, destination, capsys):
    sent = []

    def send(envelope):
        packet = envelope.packet
        sent.append(packet)
        deliver(
            client,
            {
                "from": 1 if packet.to == 0xFFFFFFFF else packet.to,
                "decoded": {"requestId": packet.id, "routing": {"errorReason": "NONE"}},
            },
        )

    monkeypatch.setattr(client, "_send_to_radio_impl", send)
    args = ["--sendtext", "hello", "--ack"]
    if destination is not None:
        args.extend(["--dest", destination])
    result = executeCommand(client, args)
    assert result.succeeded, result.error
    assert sent and sent[0].decoded.payload == b"hello"
    assert sent[0].to == (
        0xFFFFFFFF if destination is None else 1 if destination == "^local" else 2
    )
    assert client.responseHandlers == {}
    client.close.assert_not_called()
    assert capsys.readouterr() == ("", "")


def test_ack_wait_ignores_shared_ack_and_wrong_source(client, monkeypatch):
    client._acknowledgment.receivedAck = True
    packets = []

    def send(envelope):
        packet = envelope.packet
        packets.append(packet)
        deliver(
            client,
            {
                "from": 3,
                "decoded": {"requestId": packet.id, "routing": {"errorReason": "NONE"}},
            },
        )
        assert packet.id in client.responseHandlers
        # The ACK must belong to both the command request and its destination.
        deliver(
            client,
            {
                "from": 2,
                "decoded": {"requestId": packet.id, "routing": {"errorReason": "NONE"}},
            },
        )

    monkeypatch.setattr(client, "_send_to_radio_impl", send)
    result = executeCommand(
        client, ["--dest", "!00000002", "--backup-preferences", "flash"]
    )
    assert result.succeeded
    assert packets and client.responseHandlers == {}
    assert client._response_wait_errors == {}
    assert client._acknowledgment.receivedAck is True
    assert client._acknowledgment.receivedNak is False


def test_ack_wait_cannot_be_completed_by_unrelated_shared_state(client, monkeypatch):
    client._acknowledgment.receivedAck = True
    monkeypatch.setattr(client, "_send_to_radio_impl", lambda envelope: None)
    start = time.monotonic()
    result = executeCommand(
        client, ["--dest", "!00000002", "--backup-preferences", "flash"], timeout=0.03
    )
    assert result.exitCode == 1 and isinstance(result.error, TimeoutError)
    assert time.monotonic() - start < 0.5
    assert client.responseHandlers == {}
    assert client._response_wait_errors == {}
    assert client._acknowledgment.receivedAck is True


def test_command_serialization_includes_lock_wait_in_budget(client, monkeypatch):
    started, release = threading.Event(), threading.Event()
    original = client._node_view._format_nodes

    def slow(*args, **kwargs):
        started.set()
        assert release.wait(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(client._node_view, "_format_nodes", slow)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(executeCommand, client, ["--nodes"])
        assert started.wait(1)
        second = pool.submit(executeCommand, client, ["--nodes"], timeout=0.02)
        failed = second.result(timeout=1)
        assert failed.exitCode == 1 and isinstance(failed.error, TimeoutError)
        release.set()
        assert first.result(timeout=1).succeeded
    assert executeCommand(client, ["--nodes"]).succeeded


def test_independent_interfaces_can_execute_concurrently(client, monkeypatch):
    barrier = threading.Barrier(2)
    with MeshInterface(noProto=True) as other:
        other.nodesByNum = {}
        original = type(client._node_view)._format_nodes

        def concurrent(view, *args, **kwargs):
            barrier.wait(timeout=2)
            return original(view, *args, **kwargs)

        monkeypatch.setattr(type(client._node_view), "_format_nodes", concurrent)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(executeCommand, client, ["--nodes"])
            second = pool.submit(executeCommand, other, ["--nodes"])
            assert first.result(timeout=3).succeeded
            assert second.result(timeout=3).succeeded


def test_control_flow_cleanup_propagates_and_releases_serialization(
    client, monkeypatch
):
    def interrupt(envelope):
        raise KeyboardInterrupt()

    monkeypatch.setattr(client, "_send_to_radio_impl", interrupt)
    with pytest.raises(KeyboardInterrupt):
        executeCommand(client, ["--get", "lora.hopLimit"])
    assert client.responseHandlers == {}
    client.close.assert_not_called()
    assert executeCommand(client, ["--nodes"]).succeeded


def test_stream_callback_error_returns_failure_and_releases_lock(client):
    def broken_output(chunk):
        raise LookupError("consumer failed")

    result = executeCommand(client, ["--nodes"], output=broken_output)
    assert isinstance(result.error, LookupError)
    assert executeCommand(client, ["--nodes"]).succeeded


def test_output_byte_limit_is_utf8_safe_and_stops_streaming(client):
    chunks = []
    client.nodesByNum[1]["user"]["longName"] = "🌍" * 20
    result = executeCommand(
        client, ["--nodes"], maxOutputBytes=80, output=chunks.append
    )
    assert result.succeeded and result.truncated
    assert len(result.output.encode("utf-8")) <= 80
    assert "".join(chunks) == result.output
    assert "�" not in result.output


def test_invalid_set_value_returns_failure_without_mutating_cache(client, capsys):
    client.localNode.localConfig.lora.region = 1
    result = executeCommand(client, ["--set", "lora.region", "nonsense"])
    assert result.exitCode == 2
    assert client.localNode.localConfig.lora.region == 1
    assert capsys.readouterr() == ("", "")


def test_scoped_write_preserves_unscoped_error_and_ack_state(client, monkeypatch):
    key = ("receivedNak", -1)
    client._response_wait_errors[key] = "unrelated legacy failure"
    client._response_wait_acks.add(key)

    def send(envelope):
        deliver(
            client,
            {
                "from": 2,
                "decoded": {
                    "requestId": envelope.packet.id,
                    "routing": {"errorReason": "NONE"},
                },
            },
        )

    monkeypatch.setattr(client, "_send_to_radio_impl", send)
    result = executeCommand(
        client, ["--dest", "!00000002", "--backup-preferences", "flash"]
    )
    assert result.succeeded
    assert client._response_wait_errors == {key: "unrelated legacy failure"}
    assert key in client._response_wait_acks
    assert client._acknowledgment.receivedAck is False
    assert client._acknowledgment.receivedNak is False


def test_reader_thread_telemetry_summary_is_captured(client, monkeypatch, capsys):
    from meshtastic.protobuf import telemetry_pb2

    workers = []

    def send(envelope):
        payload = telemetry_pb2.Telemetry()
        payload.device_metrics.battery_level = 73
        packet = {
            "from": 2,
            "decoded": {
                "requestId": envelope.packet.id,
                "portnum": "TELEMETRY_APP",
                "payload": payload.SerializeToString(),
            },
        }
        worker = threading.Thread(target=deliver, args=(client, packet))
        workers.append(worker)
        worker.start()

    monkeypatch.setattr(client, "_send_to_radio_impl", send)
    try:
        result = executeCommand(
            client, ["--dest", "!00000002", "--request-telemetry", "device"]
        )
        assert result.succeeded
        assert "Battery level: 73.00%" in result.output
        assert client.responseHandlers == {}
        assert capsys.readouterr() == ("", "")
    finally:
        for worker in workers:
            worker.join(timeout=1)


def test_reader_thread_output_callback_failure_reports_error_and_cleans_up(
    client, monkeypatch, capsys
):
    """Reader callback failures are returned without leaking a response handler."""
    from meshtastic.protobuf import telemetry_pb2

    completed = threading.Event()
    failure_threads = []
    workers = []

    def fail_output(chunk):
        if "Battery level:" in chunk:
            failure_threads.append(threading.current_thread())
            raise RuntimeError("reader output sink failed")

    def send(envelope):
        payload = telemetry_pb2.Telemetry()
        payload.device_metrics.battery_level = 73
        packet = {
            "from": 2,
            "decoded": {
                "requestId": envelope.packet.id,
                "portnum": "TELEMETRY_APP",
                "payload": payload.SerializeToString(),
            },
        }

        def receive():
            try:
                deliver(client, packet)
            finally:
                completed.set()

        worker = threading.Thread(target=receive)
        workers.append(worker)
        worker.start()

    monkeypatch.setattr(client, "_send_to_radio_impl", send)
    try:
        result = executeCommand(
            client,
            ["--dest", "!00000002", "--request-telemetry", "device"],
            output=fail_output,
            timeout=0.3,
        )
    finally:
        for worker in workers:
            worker.join(timeout=1)
            assert not worker.is_alive()
    assert result.exitCode == 1
    assert isinstance(result.error, RuntimeError)
    assert str(result.error) == "reader output sink failed"
    assert completed.is_set()
    assert len(failure_threads) == 1
    assert failure_threads[0] is not threading.current_thread()
    assert client.responseHandlers == {}
    assert executeCommand(client, ["--nodes"], timeout=0.2).succeeded
    assert capsys.readouterr() == ("", "")


def test_ui_query_inherits_operation_deadline(client, monkeypatch):
    monkeypatch.setattr(client, "_send_to_radio_impl", lambda envelope: None)
    start = time.monotonic()
    result = executeCommand(client, ["--get-ui-config"], timeout=0.03)
    assert result.exitCode == 1 and isinstance(result.error, TimeoutError)
    assert time.monotonic() - start < 0.5
    assert client.responseHandlers == {}


def test_reader_rearm_cannot_restore_handler_after_command_timeout(client, monkeypatch):
    rearming, release = threading.Event(), threading.Event()
    workers = []
    original = client._add_response_handler

    def register(*args, **kwargs):
        if threading.current_thread() in workers:
            rearming.set()
            assert release.wait(2)
        return original(*args, **kwargs)

    def send(envelope):
        packet = envelope.packet
        worker = threading.Thread(
            target=deliver,
            args=(
                client,
                {
                    "from": 2,
                    "to": 1,
                    "decoded": {
                        "requestId": packet.id,
                        "portnum": "ROUTING_APP",
                        "routing": {"errorReason": "NONE"},
                    },
                },
            ),
        )
        workers.append(worker)
        worker.start()
        assert rearming.wait(1)

    monkeypatch.setattr(client, "_add_response_handler", register)
    monkeypatch.setattr(client, "_send_to_radio_impl", send)
    try:
        result = executeCommand(client, ["--traceroute", "!00000002"], timeout=0.2)
        assert rearming.is_set()
        assert result.exitCode == 1 and isinstance(result.error, TimeoutError)
        assert client.responseHandlers == {}
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=1)
            assert not worker.is_alive()
    assert client.responseHandlers == {}


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf")])
def test_invalid_programmatic_timeout_is_rejected(client, timeout):
    with pytest.raises(ValueError, match="timeout"):
        executeCommand(client, ["--nodes"], timeout=timeout)


def test_nested_execution_refuses_without_deadlock(client):
    nested = []

    def stream(chunk):
        nested.append(executeCommand(client, ["--nodes"]))

    assert executeCommand(client, ["--nodes"], output=stream).succeeded
    assert nested and all(isinstance(result.error, RuntimeError) for result in nested)


def test_quiet_invalid_set_retains_error_detail(client, capsys):
    client.localNode.localConfig.lora.region = 1
    result = executeCommand(client, ["--set", "lora.region", "nonsense", "--quiet"])
    assert result.exitCode == 2 and isinstance(result.error, ValueError)
    assert "nonsense" in result.output
    assert "Invalid --set preference batch" in str(result.error)
    assert capsys.readouterr() == ("", "")


def test_output_callback_system_exit_propagates_and_releases_lock(client):
    def stop(chunk):
        raise SystemExit(7)

    with pytest.raises(SystemExit) as stopped:
        executeCommand(client, ["--nodes"], output=stop)
    assert stopped.value.code == 7
    client.close.assert_not_called()
    assert executeCommand(client, ["--nodes"]).succeeded


def test_max_output_bytes_must_be_positive(client):
    for bad in (0, True):
        with pytest.raises(ValueError, match="maxOutputBytes must be a positive"):
            executeCommand(client, ["--nodes"], maxOutputBytes=bad)


def test_output_budget_exhaustion_stops_retaining_text(client):
    result = executeCommand(client, ["--nodes"], maxOutputBytes=1)

    assert result.succeeded
    assert result.truncated
    assert len(result.output) <= 1


def test_argparse_error_write_failure_returns_sink_error(client):
    def failing_sink(chunk):
        raise RuntimeError("sink down")

    result = executeCommand(client, ["--definitely-not-an-option"], output=failing_sink)

    assert result.exitCode == 1
    assert isinstance(result.error, RuntimeError)
    assert str(result.error) == "sink down"
    assert "--definitely-not-an-option" in result.output


def test_get_with_unknown_section_returns_argument_error(client):
    result = executeCommand(client, ["--get", "bogus.section"])

    assert result.exitCode == 2
    assert isinstance(result.error, ValueError)
    assert "Unknown" in result.output


def test_get_reads_fresh_module_section(client, monkeypatch):
    client.localNode.moduleConfig.mqtt.enabled = False

    def send(envelope):
        raw = admin_pb2.AdminMessage()
        raw.get_module_config_response.mqtt.enabled = True
        deliver(
            client,
            {
                "from": envelope.packet.to,
                "decoded": {"requestId": envelope.packet.id, "admin": {"raw": raw}},
            },
        )

    monkeypatch.setattr(client, "_send_to_radio_impl", send)
    result = executeCommand(client, ["--get", "mqtt.enabled"])

    assert result.succeeded, result.error
    assert "mqtt.enabled: True" in result.output
    assert client.localNode.moduleConfig.mqtt.enabled is False


def test_get_reads_fresh_deep_preference_path(client, monkeypatch):
    def send(envelope):
        raw = admin_pb2.AdminMessage()
        raw.get_module_config_response.mqtt.map_report_settings.should_report_location = (
            True
        )
        deliver(
            client,
            {
                "from": envelope.packet.to,
                "decoded": {"requestId": envelope.packet.id, "admin": {"raw": raw}},
            },
        )

    monkeypatch.setattr(client, "_send_to_radio_impl", send)
    result = executeCommand(
        client, ["--get", "mqtt.mapReportSettings.shouldReportLocation"]
    )

    assert result.succeeded, result.error
    assert "should_report_location: True" in result.output


def test_text_ack_ignores_data_feedback_packets(client, monkeypatch):
    def send(envelope):
        packet = envelope.packet
        deliver(
            client,
            {
                "from": 2,
                "decoded": {
                    "requestId": packet.id,
                    "portnum": "TEXT_MESSAGE_APP",
                    "payload": b"spoof",
                },
            },
        )
        deliver(
            client,
            {
                "from": 2,
                "decoded": {"requestId": packet.id, "routing": {"errorReason": "NONE"}},
            },
        )

    monkeypatch.setattr(client, "_send_to_radio_impl", send)
    result = executeCommand(
        client, ["--dest", "!00000002", "--sendtext", "hello", "--ack"]
    )

    assert result.succeeded, result.error
    assert client.responseHandlers == {}


def test_command_scope_ack_decode_error_records_nak():
    interface = Mock()
    scope = _CommandScope(interface, print)

    scope._on_ack(
        {"decoded": {"requestId": 7, "admin": {DECODE_ERROR_KEY: "bad payload"}}}
    )

    interface._set_wait_error.assert_called_once_with(
        "receivedNak",
        "Failed to decode admin payload: bad payload",
        request_id=7,
    )
    interface._mark_wait_acknowledged.assert_not_called()


def test_command_scope_routing_nak_records_error_before_ack_mark():
    interface = Mock()
    scope = _CommandScope(interface, print)

    scope._on_ack(
        {"decoded": {"requestId": 8, "routing": {"errorReason": "NOT_AUTHORIZED"}}}
    )

    interface._set_wait_error.assert_called_once_with(
        "receivedNak",
        "Routing error on response: NOT_AUTHORIZED",
        request_id=8,
    )
    interface._mark_wait_acknowledged.assert_called_once_with(
        "receivedNak", request_id=8
    )


def test_command_scope_ack_wait_timeout_raises():
    interface = Mock()
    interface._has_active_wait_request.return_value = True
    interface._wait_for_request_ack.return_value = False
    scope = _CommandScope(interface, print)
    scope._track(9, "receivedNak")

    with pytest.raises(
        TimeoutError, match="Timed out waiting for command acknowledgment"
    ):
        scope._wait_for_acks()


def test_command_scope_track_after_cleanup_raises():
    scope = _CommandScope(Mock(), print)
    scope._cleanup()

    with pytest.raises(RuntimeError, match="Command has completed"):
        scope._track(10, "receivedNak")


def test_command_scope_callback_after_cleanup_is_noop():
    callback = Mock()
    scope = _CommandScope(Mock(), print)
    invoke = scope._bind_callback(callback)
    scope._cleanup()

    assert invoke({"decoded": {}}) is None
    callback.assert_not_called()
