"""Unit coverage for simradio orchestration without launching firmware."""

from __future__ import annotations

import errno
import logging
import signal
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pubsub import pub

from meshtastic.protobuf import mesh_pb2, portnums_pb2
from meshtastic.tcp_interface import TCPInterface

from . import conftest as simradio_conftest
from . import simradio_harness, simradio_helpers
from .simradio_harness import (
    CHAIN_TOPOLOGY,
    REBOOT_RECOVERY_FILENAME,
    RebootCheckpoint,
    SimMesh,
    SimNode,
    _build_mesh_packet,
    _copy_channel_topology,
    _ensure_port_available,
    _inject_simulator_packet,
    find_meshtasticd,
)
from .simradio_helpers import (
    _DEFAULT_RETRIES,
    _DESTRUCTIVE_ARGUMENTS,
    _READ_ONLY_ARGUMENTS,
    PacketCollector,
    _classify_cli_operation,
    _redact_cli_diagnostics,
    cli_then_verify,
    connect_iface,
    run_cli,
    verify_state_eventually,
)


@pytest.mark.unit
def test_simradio_topology_is_defensively_copied_and_validated() -> None:
    """Caller mutations must not alter a validated topology snapshot."""
    source = {0: {1}, 1: {0}}
    copied = _copy_channel_topology(source, 2)
    source[0].clear()

    assert copied == {0: frozenset({1}), 1: frozenset({0})}
    with pytest.raises(ValueError, match="cannot receive its own"):
        _copy_channel_topology({0: {0}}, 1)
    with pytest.raises(ValueError, match="receiver indices out of range"):
        _copy_channel_topology({0: {2}}, 2)
    with pytest.raises(ValueError, match="transmitter index out of range"):
        _copy_channel_topology({2: {0}}, 2)


@pytest.mark.unit
def test_send_trace_route_budgets_relay_delay_and_restores_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The raised traceroute budget applies only for the duration of the request."""
    mesh = SimMesh(1)
    timeout = SimpleNamespace(expireTimeout=10.0)
    iface = MagicMock()
    iface._timeout = timeout
    monkeypatch.setattr(mesh, "get_iface", lambda _index: iface)
    observed: list[float] = []

    def _record_expire_timeout(**kwargs: float) -> None:
        assert kwargs == {"dest": 7, "hopLimit": 3}
        observed.append(timeout.expireTimeout)

    iface.sendTraceRoute.side_effect = _record_expire_timeout
    mesh.send_trace_route(0, dest=7, hopLimit=3)

    assert observed == [simradio_harness.TRACEROUTE_EXPIRE_TIMEOUT_SECONDS]
    assert timeout.expireTimeout == 10.0

    iface.sendTraceRoute.side_effect = RuntimeError("firmware refused")
    with pytest.raises(RuntimeError, match="firmware refused"):
        mesh.send_trace_route(0, dest=7, hopLimit=3)
    assert timeout.expireTimeout == 10.0


@pytest.mark.unit
def test_single_node_simulators_receive_fresh_sequential_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Function-scoped fixtures must not immediately rebind one TCP port."""
    monkeypatch.setenv("MESHTASTICD_SIM_BASE_PORT", "4404")
    monkeypatch.setattr(
        simradio_conftest,
        "_SINGLE_NODE_PORT_SEQUENCE",
        iter((0, 1, 2)),
    )

    assert simradio_conftest._next_simradio_single_node_port() == 4504
    assert simradio_conftest._next_simradio_single_node_port() == 4505
    assert simradio_conftest._next_simradio_single_node_port() == 4506


@pytest.mark.unit
def test_simradio_node_waits_for_next_boot_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A TCP-capable pre-reboot window must not satisfy reboot readiness."""
    node = SimNode(0, base_port=44_404)
    node.workdir = tmp_path
    node.process = MagicMock(returncode=None)
    node.process.poll.return_value = None
    log_path = tmp_path / "meshtasticd.log"
    marker = f"Using config file {node.port}\n"
    log_path.write_text(marker, encoding="utf-8")

    def _record_reboot(_seconds: float) -> None:
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(marker)

    monkeypatch.setattr(simradio_harness.time, "sleep", _record_reboot)
    monkeypatch.setattr(
        simradio_harness.time,
        "monotonic",
        iter((0.0, 0.0, 0.1)).__next__,
    )

    node.wait_for_reboot(node.boot_count(), timeout=1.0)

    assert node.boot_count() == 2


@pytest.mark.unit
def test_factory_reset_reboot_recovers_known_portduino_sigsegv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Known 2.7 reboot crash is recoverable only after exact reset evidence."""
    node = SimNode(0, base_port=44_404)
    node.workdir = tmp_path
    marker = f"Using config file {node.port}\n"
    version = "INFO S:B:37,2.7.26,native-tft,unknown\n"
    reset_evidence = (
        "Factory config reset finished, rebooting soon\n" "Reboot in 7 seconds\n"
    )
    log_path = tmp_path / "meshtasticd.log"
    prefix = marker + version
    log_path.write_text(prefix + reset_evidence, encoding="utf-8")
    checkpoint = RebootCheckpoint(1, len(prefix.encode()), 123)
    old_process = MagicMock(pid=123, returncode=-signal.SIGSEGV)
    old_process.poll.return_value = -signal.SIGSEGV
    node.process = old_process

    def _restart(**kwargs: object) -> None:
        assert kwargs["checkpoint"] == checkpoint
        assert kwargs["exit_status"] == -signal.SIGSEGV
        assert reset_evidence in str(kwargs["evidence"])
        replacement = MagicMock(pid=456, returncode=None)
        replacement.poll.return_value = None
        node.process = replacement
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(marker)

    monkeypatch.setattr(node, "_restart_after_expected_reboot_exit", _restart)

    assert node.wait_for_factory_reset_reboot(checkpoint, timeout=1.0) == (
        -signal.SIGSEGV
    )
    assert node.boot_count() == 2


@pytest.mark.unit
def test_factory_reset_reboot_rejects_crash_without_reset_evidence(
    tmp_path: Path,
) -> None:
    """An arbitrary SIGSEGV must never be normalized into a successful reboot."""
    node = SimNode(0, base_port=44_404)
    node.workdir = tmp_path
    marker = f"Using config file {node.port}\n"
    version = "INFO S:B:37,2.7.26,native-tft,unknown\n"
    prefix = marker + version
    (tmp_path / "meshtasticd.log").write_text(prefix, encoding="utf-8")
    checkpoint = RebootCheckpoint(1, len(prefix.encode()), 123)
    process = MagicMock(pid=123, returncode=-signal.SIGSEGV)
    process.poll.return_value = -signal.SIGSEGV
    node.process = process

    with pytest.raises(RuntimeError, match="missing markers"):
        node.wait_for_factory_reset_reboot(checkpoint, timeout=1.0)


@pytest.mark.unit
def test_factory_reset_reboot_rejects_unexpected_exit_status(
    tmp_path: Path,
) -> None:
    """Even complete reset logs do not authorize recovery from another exit."""
    node = SimNode(0, base_port=44_404)
    node.workdir = tmp_path
    marker = f"Using config file {node.port}\n"
    version = "INFO S:B:37,2.7.26,native-tft,unknown\n"
    evidence = "Factory config reset finished, rebooting soon\n" "Reboot in 7 seconds\n"
    prefix = marker + version
    log_path = tmp_path / "meshtasticd.log"
    log_path.write_text(prefix + evidence, encoding="utf-8")
    checkpoint = RebootCheckpoint(1, len(prefix.encode()), 123)
    process = MagicMock(pid=123, returncode=-signal.SIGKILL)
    process.poll.return_value = -signal.SIGKILL
    node.process = process

    with pytest.raises(RuntimeError, match="unexpected status -9"):
        node.wait_for_factory_reset_reboot(checkpoint, timeout=1.0)


@pytest.mark.unit
def test_factory_reset_reboot_rejects_same_crash_on_non_27_firmware(
    tmp_path: Path,
) -> None:
    """The compatibility recovery must not hide a future 2.8+ reboot crash."""
    node = SimNode(0, base_port=44_404)
    node.workdir = tmp_path
    marker = f"Using config file {node.port}\n"
    version = "INFO S:B:37,2.8.0,native-tft,unknown\n"
    evidence = "Factory config reset finished, rebooting soon\n" "Reboot in 7 seconds\n"
    prefix = marker + version
    (tmp_path / "meshtasticd.log").write_text(prefix + evidence, encoding="utf-8")
    checkpoint = RebootCheckpoint(1, len(prefix.encode()), 123)
    process = MagicMock(pid=123, returncode=-signal.SIGSEGV)
    process.poll.return_value = -signal.SIGSEGV
    node.process = process

    with pytest.raises(RuntimeError, match="firmware 2.7 boot banner"):
        node.wait_for_factory_reset_reboot(checkpoint, timeout=1.0)


@pytest.mark.unit
def test_expected_reboot_exit_relaunches_same_vfs_and_records_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery must preserve VFS state, append logs, and remain auditable."""
    node = SimNode(0, base_port=44_404)
    node.workdir = tmp_path
    (tmp_path / "vfs").mkdir()
    stdout_path = tmp_path / "meshtasticd.log"
    stderr_path = tmp_path / "meshtasticd.err"
    stdout_path.write_text("existing stdout\n", encoding="utf-8")
    stderr_path.write_text("existing stderr\n", encoding="utf-8")
    node._binary = "/usr/bin/meshtasticd"
    old_process = MagicMock(pid=123, returncode=-signal.SIGSEGV)
    old_process.poll.return_value = -signal.SIGSEGV
    node.process = old_process
    checkpoint = RebootCheckpoint(1, 0, 123)
    replacement = MagicMock(pid=456, returncode=None)
    replacement.poll.return_value = None
    popen = MagicMock(return_value=replacement)
    monkeypatch.setattr(simradio_harness, "_ensure_port_available", lambda _port: None)
    monkeypatch.setattr(simradio_harness.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(subprocess, "Popen", popen)

    node._restart_after_expected_reboot_exit(
        checkpoint=checkpoint,
        exit_status=-signal.SIGSEGV,
        evidence="Factory config reset finished, rebooting soon\nReboot in 7 seconds",
    )

    command = popen.call_args.args[0]
    assert command[command.index("-d") + 1] == str(tmp_path / "vfs")
    assert stdout_path.read_text(encoding="utf-8").startswith("existing stdout")
    assert stderr_path.read_text(encoding="utf-8").startswith("existing stderr")
    recovery = (tmp_path / REBOOT_RECOVERY_FILENAME).read_text(encoding="utf-8")
    assert "old_pid=123" in recovery
    assert "new_pid=456" in recovery
    assert "exit_status=-11" in recovery
    assert "exit_signal=SIGSEGV" in recovery
    context = (tmp_path / simradio_harness.CONTEXT_FILENAME).read_text(encoding="utf-8")
    assert "restart_count=1" in context
    assert "last_reboot_exit_status=-11" in context
    node._close_log_files()


@pytest.mark.unit
def test_set_region_drains_local_write_and_verifies_persisted_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fixture setup must verify the live write without entering a legacy ACK race."""
    cli_calls: list[tuple[int, tuple[str, ...]]] = []
    verification_ports: list[int] = []

    def _run_cli(
        port: int, *arguments: str, **_kwargs: object
    ) -> simradio_helpers.CLIResult:
        cli_calls.append((port, arguments))
        return simradio_helpers.CLIResult(returncode=0, output="", attempts=1)

    def _verify_state(
        port: int,
        verifier: Callable[[TCPInterface], None],
        *,
        no_nodes: bool = False,
    ) -> None:
        assert no_nodes is False
        verification_ports.append(port)
        iface = MagicMock()
        iface.localNode.localConfig.lora.region = 1
        verifier(iface)

    monkeypatch.setattr(simradio_helpers, "run_cli", _run_cli)
    monkeypatch.setattr(simradio_helpers, "verify_state_eventually", _verify_state)

    simradio_helpers.set_region(44_404, "US")

    assert cli_calls == [
        (
            44_404,
            (
                "--set",
                "lora.region",
                "US",
                "--wait-to-disconnect",
                "2",
            ),
        )
    ]
    assert verification_ports == [44_404]


@pytest.mark.unit
def test_set_region_rejects_unpersisted_firmware_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful CLI exit must not hide a region write that failed to persist."""
    monkeypatch.setattr(
        simradio_helpers,
        "run_cli",
        MagicMock(
            return_value=simradio_helpers.CLIResult(
                returncode=0,
                output="",
                attempts=1,
            )
        ),
    )

    def _verify_state(
        _port: int,
        verifier: Callable[[TCPInterface], None],
        *,
        no_nodes: bool = False,
    ) -> None:
        assert no_nodes is False
        iface = MagicMock()
        iface.localNode.localConfig.lora.region = 0
        verifier(iface)

    monkeypatch.setattr(simradio_helpers, "verify_state_eventually", _verify_state)

    with pytest.raises(AssertionError, match="expected US, got UNSET"):
        simradio_helpers.set_region(44_404, "US")


@pytest.mark.unit
def test_set_region_rejects_unknown_region_before_running_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid fixture input should fail before spawning a CLI process."""
    run_cli_mock = MagicMock()
    monkeypatch.setattr(simradio_helpers, "run_cli", run_cli_mock)

    with pytest.raises(ValueError, match="Unknown LoRa region"):
        simradio_helpers.set_region(44_404, "NOT_A_REGION")

    run_cli_mock.assert_not_called()


@pytest.mark.unit
def test_simradio_mesh_receiver_selection_is_deterministic() -> None:
    """Full-mesh and chain receiver lists should be stable and exclude self."""
    full = SimMesh(node_count=3)
    chain = SimMesh(node_count=3, topology=CHAIN_TOPOLOGY)

    assert full._receiver_indices(1) == (0, 2)
    assert chain._receiver_indices(0) == (1,)
    assert chain._receiver_indices(1) == (0, 2)
    assert chain._receiver_indices(2) == (1,)


@pytest.mark.unit
def test_simradio_mesh_paces_text_sends_per_originating_node() -> None:
    """Fast consecutive sends should wait out the firmware text throttle."""
    mesh = SimMesh(node_count=2)
    iface_zero = MagicMock(spec=TCPInterface)
    iface_one = MagicMock(spec=TCPInterface)
    iface_zero.sendText.side_effect = [
        mesh_pb2.MeshPacket(id=1),
        mesh_pb2.MeshPacket(id=2),
    ]
    iface_one.sendText.return_value = mesh_pb2.MeshPacket(id=3)
    mesh.nodes[0].iface = iface_zero
    mesh.nodes[1].iface = iface_one

    with (
        patch.object(
            simradio_harness.time,
            "monotonic",
            side_effect=(10.0, 10.0, 10.25, 11.1, 11.1, 11.1),
        ),
        patch.object(simradio_harness.time, "sleep") as sleep,
    ):
        first = mesh.send_text(0, "first", wantAck=False)
        second = mesh.send_text(0, "second", wantAck=False)
        other = mesh.send_text(1, "other", wantAck=False)

    assert first.id == 1
    assert second.id == 2
    assert other.id == 3
    sleep.assert_called_once_with(
        pytest.approx(simradio_harness.TEXT_MESSAGE_MIN_INTERVAL_SECONDS - 0.25)
    )
    iface_zero.sendText.assert_any_call("first", wantAck=False)
    iface_zero.sendText.assert_any_call("second", wantAck=False)
    iface_one.sendText.assert_called_once_with("other", wantAck=False)


@pytest.mark.unit
def test_build_simradio_mesh_packet_preserves_routing_metadata() -> None:
    """Simulator reconstruction should retain all routing and request fields."""
    packet = _build_mesh_packet(
        {
            "from": 11,
            "to": 22,
            "id": 33,
            "wantAck": True,
            "hopLimit": 4,
            "hopStart": 5,
            "viaMQTT": True,
            "relayNode": 6,
            "nextHop": 7,
            "channel": 2,
            "decoded": {
                "requestId": 44,
                "wantResponse": True,
                "bitfield": 0,
            },
        },
        b"payload",
    )

    assert getattr(packet, "from") == 11
    assert packet.to == 22
    assert packet.id == 33
    assert packet.want_ack is True
    assert packet.hop_limit == 4
    assert packet.hop_start == 5
    assert packet.via_mqtt is True
    assert packet.relay_node == 6
    assert packet.next_hop == 7
    assert packet.channel == 2
    assert packet.decoded.portnum == portnums_pb2.PortNum.SIMULATOR_APP
    assert packet.decoded.payload == b"payload"
    assert packet.decoded.HasField("bitfield")
    assert packet.decoded.bitfield == 0
    assert packet.decoded.request_id == 44
    assert packet.decoded.want_response is True


@pytest.mark.unit
def test_simradio_bridge_forwards_independent_packet_copies() -> None:
    """Each selected receiver should get an isolated ToRadio message."""
    mesh = SimMesh(node_count=3, topology=CHAIN_TOPOLOGY)
    transmitter = MagicMock(portNumber=mesh.nodes[1].port)
    receiver_a = MagicMock(spec=TCPInterface)
    receiver_c = MagicMock(spec=TCPInterface)
    mesh.nodes[0].iface = receiver_a
    mesh.nodes[2].iface = receiver_c
    mesh._port_to_index[mesh.nodes[1].port] = 1
    mesh._started = True

    mesh._on_sim_packet(
        packet={
            "from": 17,
            "to": 0xFFFFFFFF,
            "id": 99,
            "decoded": {"payload": b"compressed"},
        },
        interface=transmitter,
    )

    receiver_a._send_to_radio.assert_called_once()
    receiver_c._send_to_radio.assert_called_once()
    to_a = receiver_a._send_to_radio.call_args.args[0]
    to_c = receiver_c._send_to_radio.call_args.args[0]
    assert isinstance(to_a, mesh_pb2.ToRadio)
    assert isinstance(to_c, mesh_pb2.ToRadio)
    assert to_a is not to_c
    assert to_a.packet.SerializeToString() == to_c.packet.SerializeToString()


@pytest.mark.unit
def test_simradio_bridge_drops_oversized_payload() -> None:
    """Oversized simulator payloads must never be injected into firmware."""
    mesh = SimMesh(node_count=2)
    transmitter = MagicMock(portNumber=mesh.nodes[0].port)
    receiver = MagicMock(spec=TCPInterface)
    mesh.nodes[1].iface = receiver
    mesh._port_to_index[mesh.nodes[0].port] = 0
    mesh._started = True

    mesh._on_sim_packet(
        packet={
            "decoded": {"payload": b"x" * (mesh_pb2.Constants.DATA_PAYLOAD_LEN + 1)}
        },
        interface=transmitter,
    )

    receiver._send_to_radio.assert_not_called()


@pytest.mark.unit
def test_blocked_simradio_injection_does_not_prevent_teardown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Firmware injection must run after releasing the teardown bridge lock."""
    mesh = SimMesh(node_count=2)
    transmitter = MagicMock(portNumber=mesh.nodes[0].port)
    mesh.nodes[1].iface = MagicMock(spec=TCPInterface)
    mesh._port_to_index[mesh.nodes[0].port] = 0
    mesh._started = True
    injection_started = threading.Event()
    release_injection = threading.Event()
    teardown_finished = threading.Event()

    def _blocked_injection(_iface: TCPInterface, _packet: mesh_pb2.ToRadio) -> None:
        injection_started.set()
        release_injection.wait(timeout=2.0)

    monkeypatch.setattr(
        simradio_harness, "_inject_simulator_packet", _blocked_injection
    )
    forwarding = threading.Thread(
        target=mesh._on_sim_packet,
        kwargs={
            "packet": {"decoded": {"payload": b"blocked"}},
            "interface": transmitter,
        },
        daemon=True,
    )
    forwarding.start()
    assert injection_started.wait(timeout=1.0)

    def _stop_mesh() -> None:
        mesh.stop()
        teardown_finished.set()

    teardown = threading.Thread(target=_stop_mesh, daemon=True)
    teardown.start()
    completed_while_blocked = teardown_finished.wait(timeout=0.5)
    release_injection.set()
    forwarding.join(timeout=1.0)
    teardown.join(timeout=1.0)

    assert completed_while_blocked
    assert not forwarding.is_alive()
    assert not teardown.is_alive()


@pytest.mark.unit
def test_simradio_bridge_drops_malformed_routing_metadata() -> None:
    """Invalid decoded routing values must not escape the pubsub callback."""
    mesh = SimMesh(node_count=2)
    transmitter = MagicMock(portNumber=mesh.nodes[0].port)
    receiver = MagicMock(spec=TCPInterface)
    mesh.nodes[1].iface = receiver
    mesh._port_to_index[mesh.nodes[0].port] = 0
    mesh._started = True

    mesh._on_sim_packet(
        packet={"to": "not-a-node-number", "decoded": {"payload": b"payload"}},
        interface=transmitter,
    )

    receiver._send_to_radio.assert_not_called()


@pytest.mark.unit
def test_simradio_injection_accepts_current_upstream_internal_spelling() -> None:
    """Harness adaptation should tolerate upstream's pre-refactor sender name."""
    legacy_iface = MagicMock(spec_set=["_sendToRadio"])
    to_radio = mesh_pb2.ToRadio()

    _inject_simulator_packet(legacy_iface, to_radio)

    legacy_iface._sendToRadio.assert_called_once_with(to_radio)


@pytest.mark.unit
def test_find_meshtasticd_prefers_executable_environment_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MESHTASTICD_BIN should win when it names an executable file."""
    executable = tmp_path / "meshtasticd"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    monkeypatch.setenv("MESHTASTICD_BIN", str(executable))

    assert find_meshtasticd() == str(executable.resolve())


@pytest.mark.unit
def test_simradio_node_rejects_occupied_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Harness startup must not attach to an unrelated existing listener."""
    probe = MagicMock()
    probe.__enter__.return_value = probe
    probe.connect_ex.return_value = 0
    monkeypatch.setattr(
        simradio_harness.socket,
        "socket",
        lambda *_args, **_kwargs: probe,
    )

    with pytest.raises(RuntimeError, match="already in use"):
        _ensure_port_available(44_404)

    probe.settimeout.assert_called_once_with(
        simradio_harness.PORT_LISTENER_PROBE_TIMEOUT_SECONDS
    )
    probe.connect_ex.assert_called_once_with(("127.0.0.1", 44_404))
    probe.bind.assert_not_called()


@pytest.mark.unit
def test_simradio_port_preflight_allows_non_listening_kernel_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused connection must not be mistaken for a live port owner."""
    probe = MagicMock()
    probe.__enter__.return_value = probe
    probe.connect_ex.return_value = errno.ECONNREFUSED
    monkeypatch.setattr(
        simradio_harness.socket,
        "socket",
        lambda *_args, **_kwargs: probe,
    )

    _ensure_port_available(44_404)

    probe.connect_ex.assert_called_once_with(("127.0.0.1", 44_404))
    probe.bind.assert_not_called()


@pytest.mark.unit
def test_simradio_port_preflight_rejects_ambiguous_probe_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timeouts and unknown socket states must not be treated as availability."""
    probe = MagicMock()
    probe.__enter__.return_value = probe
    probe.connect_ex.return_value = errno.ETIMEDOUT
    monkeypatch.setattr(
        simradio_harness.socket,
        "socket",
        lambda *_args, **_kwargs: probe,
    )

    with pytest.raises(RuntimeError, match="cannot confirm"):
        _ensure_port_available(44_404)


@pytest.mark.unit
def test_simradio_context_is_archived_with_source_and_fixture_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every daemon log directory should identify the exact source run and port."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    log_root = tmp_path / "archive"
    node = SimNode(2, base_port=44_404, log_root=log_root)
    node.workdir = workdir
    node.process = SimpleNamespace(pid=12345)  # type: ignore[assignment]
    (workdir / "meshtasticd.log").write_text("stdout", encoding="utf-8")
    (workdir / "meshtasticd.err").write_text("stderr", encoding="utf-8")
    monkeypatch.setenv("GITHUB_SHA", "mergebeef")
    monkeypatch.setenv("SIMRADIO_SOURCE_SHA", "deadbeef")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("MESHTASTICD_CHANNEL", "beta")
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "module.py::test_case (setup)")

    node._write_context("/usr/bin/meshtasticd")
    node._archive_logs()

    destination = log_root / "node-2" / workdir.name
    context = (destination / simradio_harness.CONTEXT_FILENAME).read_text(
        encoding="utf-8"
    )
    assert "node_id=2" in context
    assert "port=44406" in context
    assert "pid=12345" in context
    assert "github_sha=mergebeef" in context
    assert "source_sha=deadbeef" in context
    assert "github_run_id=123" in context
    assert "meshtasticd_channel=beta" in context
    assert "pytest_current_test=module.py::test_case (setup)" in context
    assert (destination / "meshtasticd.log").read_text(encoding="utf-8") == "stdout"
    assert (destination / "meshtasticd.err").read_text(encoding="utf-8") == "stderr"


@pytest.mark.unit
def test_simradio_node_cleans_resources_when_process_launch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Popen failure should close logs and remove the temporary VFS."""

    def _fail_launch(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated launch failure")

    monkeypatch.setattr(simradio_harness, "_ensure_port_available", lambda _port: None)
    monkeypatch.setattr(subprocess, "Popen", _fail_launch)
    node = SimNode(0, base_port=44_405)

    with pytest.raises(RuntimeError, match="failed to start"):
        node.start("/does/not/matter")

    assert node.process is None
    assert node.workdir is None
    assert node.iface is None
    assert node._temporary_directory is None
    assert node._log_files == []


@pytest.mark.unit
def test_run_cli_retries_transient_failure_without_logging_values(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Transient connection failures should retry and return attempt metadata."""
    completed = iter(
        (
            subprocess.CompletedProcess(
                args=[], returncode=1, stdout="Connection refused"
            ),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="ok"),
        )
    )
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: next(completed))
    monkeypatch.setattr(simradio_helpers.time, "sleep", lambda _seconds: None)

    with caplog.at_level(logging.DEBUG, logger="meshtastic.tests.simradio_helpers"):
        result = run_cli(
            4404,
            "--set",
            "network.wifi_psk",
            "distinctive-secret",
        )

    assert result.returncode == 0
    assert result.output == "ok"
    assert result.attempts == 2
    assert "distinctive-secret" not in caplog.text


@pytest.mark.unit
def test_connect_iface_retries_the_owned_interface_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Readiness retries must retain the successful API client, not probe first."""
    iface = MagicMock(spec=TCPInterface)
    constructor = MagicMock(side_effect=(ConnectionRefusedError(), iface))
    monkeypatch.setattr(simradio_helpers, "TCPInterface", constructor)
    monkeypatch.setattr(simradio_helpers.time, "sleep", lambda _seconds: None)

    assert connect_iface(4404, wait_timeout=1.0) is iface
    assert constructor.call_count == 2


@pytest.mark.unit
def test_verify_state_eventually_retries_only_state_mismatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asynchronous state polling should reconnect without replaying mutations."""
    first = MagicMock(spec=TCPInterface)
    second = MagicMock(spec=TCPInterface)
    connect = MagicMock(side_effect=(first, second))
    monkeypatch.setattr(simradio_helpers, "connect_iface", connect)
    monkeypatch.setattr(simradio_helpers.time, "sleep", lambda _seconds: None)

    attempts = 0

    def _verifier(iface: TCPInterface) -> None:
        nonlocal attempts
        attempts += 1
        if iface is first:
            raise AssertionError("not visible yet")
        assert iface is second

    verify_state_eventually(
        44_404,
        _verifier,
        timeout=1.0,
        retry_delay=0.0,
    )

    assert attempts == 2
    assert connect.call_count == 2
    first.close.assert_called_once_with()
    second.close.assert_called_once_with()


@pytest.mark.unit
def test_verify_state_eventually_does_not_retry_programming_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected verifier failures must not be hidden by convergence retries."""
    iface = MagicMock(spec=TCPInterface)
    connect = MagicMock(return_value=iface)
    monkeypatch.setattr(simradio_helpers, "connect_iface", connect)

    with pytest.raises(TypeError, match="bad verifier"):
        verify_state_eventually(
            44_404,
            lambda _iface: (_ for _ in ()).throw(TypeError("bad verifier")),
            timeout=1.0,
        )

    connect.assert_called_once()
    iface.close.assert_called_once_with()


@pytest.mark.unit
def test_cli_failure_diagnostics_redact_positional_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Assertion diagnostics must not echo credentials supplied to the CLI."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="Rejected distinctive-secret for network.wifi_psk",
        ),
    )

    with pytest.raises(AssertionError) as exc_info:
        cli_then_verify(
            4404,
            ("--set", "network.wifi_psk", "distinctive-secret"),
            None,
        )

    diagnostic = str(exc_info.value)
    assert "distinctive-secret" not in diagnostic
    assert "<redacted>" in diagnostic
    assert "network.wifi_psk" in diagnostic


@pytest.mark.unit
@pytest.mark.parametrize(
    ("arguments", "secret"),
    (
        (
            ("--set", "security.private_key", "private-key-material"),
            "private-key-material",
        ),
        (("--set", "security.admin_key", "admin-key-material"), "admin-key-material"),
        (("--set", "network.wifiPsk", "wifi-secret-material"), "wifi-secret-material"),
        (("--ch-set", "psk", "channel-key-material"), "channel-key-material"),
        (
            ("--seturl", "https://meshtastic.org/e/#secret-url"),
            "https://meshtastic.org/e/#secret-url",
        ),
        (
            ("--ch-set-url", "https://meshtastic.org/e/#channel-url"),
            "https://meshtastic.org/e/#channel-url",
        ),
        (
            ("--ch-add-url", "https://meshtastic.org/e/#added-url"),
            "https://meshtastic.org/e/#added-url",
        ),
    ),
)
def test_cli_diagnostics_redact_each_secret_bearing_field(
    arguments: tuple[str, ...],
    secret: str,
) -> None:
    """Keys and channel URL payloads must not escape into CI diagnostics."""
    diagnostic = _redact_cli_diagnostics(f"Rejected value {secret}", arguments)

    assert secret not in diagnostic
    assert diagnostic == "Rejected value <redacted>"


@pytest.mark.unit
def test_cli_diagnostics_preserve_ordinary_values() -> None:
    """Region, numeric, and boolean values should remain useful diagnostics."""
    arguments = (
        "--set",
        "lora.region",
        "US",
        "--set",
        "lora.hop_limit",
        "0",
        "--set",
        "position.fixed_position",
        "true",
    )
    output = "region=US hop_limit=0 fixed_position=true"

    assert _redact_cli_diagnostics(output, arguments) == output


@pytest.mark.unit
def test_cli_diagnostics_redact_bare_channel_url_fragment() -> None:
    """A decoded channel-key fragment must remain secret without its URL prefix."""
    arguments = ("--seturl", "https://meshtastic.org/e/#channel-key-fragment")

    diagnostic = _redact_cli_diagnostics("Rejected channel-key-fragment", arguments)

    assert diagnostic == "Rejected <redacted>"


@pytest.mark.unit
def test_packet_collectors_unsubscribe_independently() -> None:
    """Closing one collector must not remove another subscriber on the topic."""
    iface = MagicMock(spec=TCPInterface)
    first = PacketCollector(iface, "meshtastic.receive.text")
    second = PacketCollector(iface, "meshtastic.receive.text")
    try:
        first.close()
        pub.sendMessage(
            "meshtastic.receive.text",
            packet={
                "decoded": {
                    "portnum": "TEXT_MESSAGE_APP",
                    "text": "still-subscribed",
                }
            },
            interface=iface,
        )
        assert first.texts == []
        assert second.texts == ["still-subscribed"]
    finally:
        first.close()
        second.close()


@pytest.mark.unit
def test_simradio_stop_cleans_partially_started_nodes() -> None:
    """Stop should close all nodes even before the mesh marks itself started."""
    mesh = SimMesh(node_count=3)
    close_calls: list[int] = []
    for node in mesh.nodes:
        node.close = MagicMock(  # type: ignore[method-assign]
            side_effect=lambda node_id=node.node_id: close_calls.append(node_id)
        )

    mesh.stop()

    assert close_calls == [2, 1, 0]


# =============================================================================
# Operation-aware retry policy
# =============================================================================


@pytest.mark.unit
@pytest.mark.parametrize(
    ("arguments", "expected_kind"),
    (
        # Read-only
        (("--info",), "read_only"),
        (("--nodes",), "read_only"),
        (("--export-config",), "read_only"),
        (("--get", "lora.region"), "read_only"),
        # Idempotent mutations
        (("--set", "lora.region", "US"), "idempotent_mutation"),
        (("--set-owner", "Test"), "idempotent_mutation"),
        (("--seturl", "https://example.com/#abc"), "idempotent_mutation"),
        # Destructive flags
        (("--ch-add", "LongFast"), "non_idempotent"),
        (("--ch-del",), "non_idempotent"),
        (("--ch-enable",), "non_idempotent"),
        (("--ch-disable",), "non_idempotent"),
        (("--factory-reset",), "non_idempotent"),
        (("--factory-reset-config",), "non_idempotent"),
        (("--factory-reset-device",), "non_idempotent"),
        (("--reboot",), "non_idempotent"),
        (("--reboot-ota",), "non_idempotent"),
        (("--enter-dfu",), "non_idempotent"),
        (("--shutdown",), "non_idempotent"),
        (("--ota-update", "fw.bin"), "non_idempotent"),
        (("--reset-nodedb",), "non_idempotent"),
        (("--test",), "non_idempotent"),
        # Semantic destructive --set forms
        (("--set", "factory_reset", "true"), "non_idempotent"),
        (("--set", "reboot", "true"), "non_idempotent"),
        (("--set", "shutdown", "true"), "non_idempotent"),
        (("--set", "ota_update", "true"), "non_idempotent"),
        # Unknown / future commands default to no retries
        (("--new-cmd",), "non_idempotent"),
        ((), "non_idempotent"),
    ),
)
def test_classify_cli_operation_maps_arguments_to_kind(
    arguments: tuple[str, ...],
    expected_kind: str,
) -> None:
    """Every CLI argument should map to the correct retry-policy bucket."""
    assert _classify_cli_operation(list(arguments)) == expected_kind


@pytest.mark.unit
def test_classify_destructive_wins_over_read_only() -> None:
    """A mixed argument list is classified conservatively."""
    assert _classify_cli_operation(["--ch-del", "--info"]) == "non_idempotent"


@pytest.mark.unit
def test_classify_semantic_set_destructive_has_priority() -> None:
    """--set factory_reset true is destructive even combined with --info."""
    assert (
        _classify_cli_operation(["--info", "--set", "factory_reset", "true"])
        == "non_idempotent"
    )


@pytest.mark.unit
def test_classify_unknown_arguments_default_to_non_idempotent() -> None:
    """Future or unknown CLI commands get zero retries by default."""
    assert _classify_cli_operation(["--future-flag"]) == "non_idempotent"
    assert _classify_cli_operation([]) == "non_idempotent"


@pytest.mark.unit
def test_run_cli_defaults_to_zero_retries_for_destructive_ops() -> None:
    """Destructive CLI invocations should not retry by default."""
    fake_stdout = MagicMock()
    fake_stdout.configure_mock(**{"stdout": "connected", "returncode": 1})
    with patch("subprocess.run", return_value=fake_stdout):
        result = run_cli(4404, "--factory-reset")
    assert result.attempts == 1


@pytest.mark.unit
def test_run_cli_bounds_device_waits_inside_subprocess_timeout() -> None:
    """The helper must not leave the CLI's 300-second request timeout active."""
    completed = MagicMock(returncode=0, stdout="ok")
    with patch("subprocess.run", return_value=completed) as run:
        result = run_cli(4404, "--export-config", "config.yaml", timeout=90.0)

    assert result.returncode == 0
    argv = run.call_args.args[0]
    timeout_index = argv.index("--timeout")
    assert argv[timeout_index + 1] == str(
        simradio_helpers.DEFAULT_DEVICE_REQUEST_TIMEOUT_SECONDS
    )
    assert run.call_args.kwargs["timeout"] == 90.0


@pytest.mark.unit
def test_run_cli_preserves_explicit_device_request_timeout() -> None:
    """A caller-provided CLI --timeout must not be duplicated or replaced."""
    completed = MagicMock(returncode=0, stdout="ok")
    with patch("subprocess.run", return_value=completed) as run:
        run_cli(4404, "--timeout", "7", "--info", request_timeout=20.0)

    argv = run.call_args.args[0]
    assert argv.count("--timeout") == 1
    timeout_index = argv.index("--timeout")
    assert argv[timeout_index + 1] == "7"


@pytest.mark.unit
def test_run_cli_can_preserve_cli_default_request_timeout() -> None:
    """request_timeout=None should omit the helper-injected CLI option."""
    completed = MagicMock(returncode=0, stdout="ok")
    with patch("subprocess.run", return_value=completed) as run:
        run_cli(4404, "--info", request_timeout=None)

    assert "--timeout" not in run.call_args.args[0]


@pytest.mark.unit
def test_run_cli_retries_read_only_commands() -> None:
    """Read-only CLI invocations should retry on transient failures."""
    run_results = [
        MagicMock(returncode=1, stdout="error connecting"),
        MagicMock(returncode=0, stdout="ok"),
    ]
    with patch("subprocess.run", side_effect=run_results):
        result = run_cli(4404, "--info")
    assert result.attempts == 2
    assert result.returncode == 0


@pytest.mark.unit
def test_run_cli_explicit_retries_override_and_retry_on_failure() -> None:
    """Explicit retries= overrides auto-classification and retries on failure."""
    run_results = [
        MagicMock(returncode=1, stdout="error connecting"),
        MagicMock(returncode=0, stdout="ok"),
    ]
    with patch("subprocess.run", side_effect=run_results):
        result = run_cli(4404, "--factory-reset", retries=2)
    # Despite being destructive, explicit retries=2 was respected.
    # First call failed transiently, second succeeded.
    assert result.attempts == 2
    assert result.returncode == 0


@pytest.mark.unit
def test_default_retries_maps_each_kind() -> None:
    """Validation: every recognized operation kind has a retry default."""
    for kind in ("read_only", "idempotent_mutation", "non_idempotent"):
        assert kind in _DEFAULT_RETRIES
        assert isinstance(_DEFAULT_RETRIES[kind], int)
        assert _DEFAULT_RETRIES[kind] >= 0


@pytest.mark.unit
def test_destructive_and_read_only_are_disjoint() -> None:
    """An argument cannot appear in both the destructive and read-only sets."""
    overlap = _DESTRUCTIVE_ARGUMENTS & _READ_ONLY_ARGUMENTS
    assert not overlap, f"ambiguous arguments: {sorted(overlap)}"
