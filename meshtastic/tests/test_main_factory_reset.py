"""Meshtastic unit tests for __main__.py."""

# pylint: disable=C0302,W0613,R0917

import argparse
import logging
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from pubsub import pub

import meshtastic.__main__ as main_module
import meshtastic.cli.device_actions as device_actions
from meshtastic import mt_config
from meshtastic.__main__ import main
from meshtastic.cli.context import ActionOutcome, CliContext
from meshtastic.mesh_interface import MeshInterface
from meshtastic.serial_interface import SerialInterface
from meshtastic.util import Acknowledgment

# from ..ble_interface import BLEInterface


# from ..remote_hardware import onGPIOreceive
# from ..config_pb2 import Config

SDS_DISABLED_SENTINEL: int = 4_294_967_295
MAIN_LOCAL_ADDR: str = cast(str, main_module.__dict__["LOCAL_ADDR"])


def _get_config_field(config: Any, dotted_path: str) -> Any:
    """Walk a dotted `section.field` path on a protobuf Config message."""
    obj = config
    for part in dotted_path.split("."):
        obj = getattr(obj, part)
    return obj


@pytest.fixture(autouse=True)
def _mock_newer_version_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent external network calls during unit tests in this module.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Pytest monkeypatching fixture.
    """
    monkeypatch.setattr("meshtastic.util.check_if_newer_version", lambda: None)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize(
    ("reset_flag", "expected_full"),
    [("--factory-reset", False), ("--factory-reset-device", True)],
)
def test_main_factory_reset_local_accepts_ack_before_close(
    capsys: pytest.CaptureFixture[str],
    reset_flag: str,
    expected_full: bool,
) -> None:
    """Both local reset variants should accept their correlated ACK before closing."""
    sys.argv = ["", reset_flag]
    mt_config.args = sys.argv  # type: ignore[assignment]
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.isConnected = threading.Event()
    iface.isConnected.set()
    iface._acknowledgment = Acknowledgment()
    iface._acknowledgment.receivedAck = True  # stale state must be cleared
    iface._timeout.expireTimeout = 1.0
    iface._wait_for_request_ack.return_value = True
    iface._raise_wait_error_if_present.return_value = None
    reset_node = iface.getNode.return_value
    reset_node.iface = iface

    def _factory_reset(*, full: bool) -> SimpleNamespace:
        assert full is expected_full
        assert iface._acknowledgment.receivedAck is False
        iface._acknowledgment.receivedImplAck = True
        return SimpleNamespace(id=123)

    reset_node.factoryReset.side_effect = _factory_reset

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()

    out, err = capsys.readouterr()
    reset_node.factoryReset.assert_called_once_with(full=expected_full)
    iface._wait_for_request_ack.assert_called_once()
    wait_args, wait_kwargs = iface._wait_for_request_ack.call_args
    assert wait_args[:2] == ("receivedNak", 123)
    assert (
        0
        < wait_kwargs["timeout_seconds"]
        <= (main_module.FACTORY_RESET_ACCEPTANCE_POLL_SECONDS)
    )
    iface._raise_wait_error_if_present.assert_called_once_with(
        "receivedNak", request_id=123
    )
    iface._retire_wait_request.assert_called_once_with("receivedNak", request_id=123)
    iface.waitForAckNak.assert_not_called()
    assert iface._acknowledgment.receivedImplAck is False
    assert "Waiting for factory reset acknowledgment or reboot disconnect" in out
    assert err == ""


@pytest.mark.unit
def test_local_factory_reset_accepts_transient_reboot_disconnect() -> None:
    """A 2.7-style dropped ACK is accepted only after reboot disconnect is observed."""
    connected = threading.Event()
    connected.set()
    iface = SimpleNamespace(
        isConnected=connected,
        _acknowledgment=Acknowledgment(),
        _timeout=SimpleNamespace(expireTimeout=1.0),
        _retire_wait_request=MagicMock(),
        MeshInterfaceError=RuntimeError,
    )
    reset_node = SimpleNamespace(iface=iface)

    def _factory_reset(*, full: bool) -> SimpleNamespace:
        assert full is False
        request = SimpleNamespace(id=456)

        def _disconnect_after_send() -> None:
            time.sleep(0.01)
            # Model a fast reconnect: the pubsub edge must remain observable even
            # though isConnected is already set again by the time the wait polls.
            pub.sendMessage("meshtastic.connection.lost", interface=iface)
            connected.set()

        threading.Thread(target=_disconnect_after_send, daemon=True).start()
        return request

    reset_node.factoryReset = MagicMock(side_effect=_factory_reset)

    request = main_module._send_local_factory_reset_and_wait(
        reset_node,
        full=False,
        timeout=0.5,
    )

    assert request is not None
    assert request.id == 456
    iface._retire_wait_request.assert_called_once_with("receivedNak", request_id=456)


@pytest.mark.unit
def test_local_factory_reset_accepts_implicit_ack_with_legacy_completion_flag() -> None:
    """A valid implicit ACK must win over the legacy receivedNak completion latch."""
    connected = threading.Event()
    connected.set()
    acknowledgment = Acknowledgment()
    iface = SimpleNamespace(
        isConnected=connected,
        _acknowledgment=acknowledgment,
        _retire_wait_request=MagicMock(),
        _raise_wait_error_if_present=MagicMock(),
        MeshInterfaceError=RuntimeError,
    )

    def _factory_reset(*, full: bool) -> SimpleNamespace:
        assert full is False
        acknowledgment.receivedImplAck = True
        acknowledgment.receivedNak = True
        return SimpleNamespace(id=457)

    reset_node = SimpleNamespace(
        iface=iface,
        factoryReset=MagicMock(side_effect=_factory_reset),
    )

    request = main_module._send_local_factory_reset_and_wait(
        reset_node,
        full=False,
        timeout=0.5,
    )

    assert request is not None
    assert request.id == 457
    iface._raise_wait_error_if_present.assert_called()


@pytest.mark.unit
def test_local_factory_reset_accepts_tcp_socket_generation_change() -> None:
    """A TCP reconnect is observable even when isConnected never clears."""
    connected = threading.Event()
    connected.set()
    original_socket = object()
    iface = SimpleNamespace(
        isConnected=connected,
        socket=original_socket,
        stream=None,
        _acknowledgment=Acknowledgment(),
        _timeout=SimpleNamespace(expireTimeout=0.01),
        _retire_wait_request=MagicMock(),
        MeshInterfaceError=RuntimeError,
    )
    reset_node = SimpleNamespace(iface=iface)

    def _factory_reset(*, full: bool) -> SimpleNamespace:
        assert full is False

        def _replace_socket() -> None:
            time.sleep(0.05)
            iface.socket = object()

        threading.Thread(target=_replace_socket, daemon=True).start()
        return SimpleNamespace(id=458)

    reset_node.factoryReset = MagicMock(side_effect=_factory_reset)

    started = time.monotonic()
    request = main_module._send_local_factory_reset_and_wait(
        reset_node,
        full=False,
    )

    assert request is not None
    assert request.id == 458
    assert time.monotonic() - started >= 0.04
    # The interface's ordinary 10 ms timeout must not truncate the reset's
    # firmware-defined seven-second reboot window.
    assert iface._timeout.expireTimeout == 0.01


@pytest.mark.unit
def test_local_factory_reset_ignores_socket_change_before_send_returns() -> None:
    """A transport change before a concrete request returns is not acceptance."""
    connected = threading.Event()
    connected.set()
    iface = SimpleNamespace(
        isConnected=connected,
        socket=object(),
        stream=None,
        _acknowledgment=Acknowledgment(),
        _retire_wait_request=MagicMock(),
        MeshInterfaceError=RuntimeError,
    )

    def _factory_reset(*, full: bool) -> SimpleNamespace:
        assert full is False
        iface.socket = object()
        return SimpleNamespace(id=459)

    reset_node = SimpleNamespace(
        iface=iface,
        factoryReset=MagicMock(side_effect=_factory_reset),
    )

    request = main_module._send_local_factory_reset_and_wait(
        reset_node,
        full=False,
        timeout=0.01,
    )

    assert request is not None
    assert request.id == 459


@pytest.mark.unit
def test_local_factory_reset_ignores_pre_send_disconnect_event() -> None:
    """A stale disconnect published before the request is queued cannot prove acceptance."""
    connected = threading.Event()
    connected.set()
    iface = SimpleNamespace(
        isConnected=connected,
        _acknowledgment=Acknowledgment(),
        _timeout=SimpleNamespace(expireTimeout=0.01),
        _retire_wait_request=MagicMock(),
        MeshInterfaceError=RuntimeError,
    )

    def _factory_reset(*, full: bool) -> SimpleNamespace:
        assert full is False
        pub.sendMessage("meshtastic.connection.lost", interface=iface)
        return SimpleNamespace(id=654)

    reset_node = SimpleNamespace(
        iface=iface,
        factoryReset=MagicMock(side_effect=_factory_reset),
    )

    request = main_module._send_local_factory_reset_and_wait(
        reset_node,
        full=False,
        timeout=0.01,
    )

    assert request is not None
    assert request.id == 654
    iface._retire_wait_request.assert_called_once_with("receivedNak", request_id=654)


@pytest.mark.unit
def test_local_factory_reset_returns_when_no_acceptance_signal_arrives() -> None:
    """A queued reset without ACK, NAK, or reboot still returns to the shell."""
    connected = threading.Event()
    connected.set()
    iface = SimpleNamespace(
        isConnected=connected,
        _acknowledgment=Acknowledgment(),
        _timeout=SimpleNamespace(expireTimeout=0.01),
        _retire_wait_request=MagicMock(),
        MeshInterfaceError=RuntimeError,
    )
    reset_node = SimpleNamespace(
        iface=iface,
        factoryReset=MagicMock(return_value=SimpleNamespace(id=789)),
    )

    request = main_module._send_local_factory_reset_and_wait(
        reset_node,
        full=False,
        timeout=0.01,
    )

    assert request is not None
    assert request.id == 789
    iface._retire_wait_request.assert_called_once_with("receivedNak", request_id=789)


@pytest.mark.unit
def test_local_factory_reset_nak_remains_failure() -> None:
    """A request-scoped routing error must win over any transport signal."""
    connected = threading.Event()
    connected.set()
    iface = SimpleNamespace(
        isConnected=connected,
        socket=object(),
        stream=None,
        _acknowledgment=Acknowledgment(),
        _wait_for_request_ack=MagicMock(return_value=True),
        _raise_wait_error_if_present=MagicMock(
            side_effect=RuntimeError("Routing error on response: NOT_AUTHORIZED")
        ),
        _retire_wait_request=MagicMock(),
        MeshInterfaceError=RuntimeError,
    )
    reset_node = SimpleNamespace(
        iface=iface,
        factoryReset=MagicMock(return_value=SimpleNamespace(id=987)),
    )

    with pytest.raises(RuntimeError, match="Routing error on response: NOT_AUTHORIZED"):
        main_module._send_local_factory_reset_and_wait(
            reset_node,
            full=False,
            timeout=0.5,
        )

    iface._wait_for_request_ack.assert_called_once()
    iface._raise_wait_error_if_present.assert_called_once_with(
        "receivedNak", request_id=987
    )
    iface._retire_wait_request.assert_called_once_with("receivedNak", request_id=987)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_factory_reset_skips_ack_wait_when_send_is_disabled() -> None:
    """A noProto reset returning None must not enter an impossible ACK wait."""
    sys.argv = ["", "--factory-reset"]
    mt_config.args = sys.argv  # type: ignore[assignment]
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    reset_node = iface.getNode.return_value
    reset_node.iface = iface
    reset_node.factoryReset.return_value = None

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()

    iface._wait_for_request_ack.assert_not_called()
    iface._raise_wait_error_if_present.assert_not_called()
    iface._retire_wait_request.assert_not_called()
    iface.waitForAckNak.assert_not_called()


@pytest.mark.unit
def test_post_factory_reset_ready_probe_closes_and_probes_reconnect() -> None:
    iface = cast(Any, object.__new__(SerialInterface))
    iface.connect = MagicMock()
    iface.close = MagicMock()

    main_module._post_factory_reset_ready_probe(cast(Any, iface))

    iface.connect.assert_called_once()
    assert iface.close.call_count >= 2


@pytest.mark.unit
def test_post_factory_reset_ready_probe_bounds_and_quiets_expected_failure(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Log a concise warning when all readiness attempts are exhausted.

    Parameters
    ----------
    caplog : pytest.LogCaptureFixture
        Captured log records used to verify the bounded failure diagnostic.

    The legacy "Factory reset accepted; device is still rebooting" log line
    was a single-shot, single-attempt suppression. The new probe retires up
    to [FACTORY_RESET_READY_PROBE_MAX_ATTEMPTS] times, and the final
    exhausted-state is now a WARNING (not INFO) that explains the next
    command may have to reconnect. The function returns ``False``; the
    reset command intentionally continues and reports the uncertain state
    instead of failing closed.
    """
    iface = cast(Any, object.__new__(SerialInterface))
    clock = _VirtualClock()
    monkeypatch.setattr(device_actions, "time", clock)
    iface.close = MagicMock()

    def _connect() -> None:
        assert iface._connect_wait_timeout_seconds == (
            main_module.FACTORY_RESET_READY_PROBE_TIMEOUT_SECONDS
        )
        assert iface._connect_retry_budget_seconds == (
            main_module.FACTORY_RESET_READY_PROBE_TIMEOUT_SECONDS
        )
        assert iface._suppress_connect_failure_logging is True
        raise RuntimeError("device still rebooting")

    iface.connect = MagicMock(side_effect=_connect)

    with caplog.at_level(logging.DEBUG):
        result = main_module._post_factory_reset_ready_probe(cast(Any, iface))

    assert result is False
    assert (
        iface.connect.call_count == main_module.FACTORY_RESET_READY_PROBE_MAX_ATTEMPTS
    )
    assert "device did not respond" in caplog.text
    assert "Traceback" not in caplog.text
    assert not hasattr(iface, "_connect_wait_timeout_seconds")
    assert not hasattr(iface, "_connect_retry_budget_seconds")
    assert not hasattr(iface, "_suppress_connect_failure_logging")
    # The probe closes the port once before the retry loop, once after every
    # failed attempt (so a failed attempt cannot hold the tty against the
    # next one), and once after exhausting the budget.
    assert iface.close.call_count == (
        main_module.FACTORY_RESET_READY_PROBE_MAX_ATTEMPTS + 2
    )


@pytest.mark.unit
def test_post_factory_reset_ready_probe_retries_until_device_returns(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Report success when the device returns on the second attempt.

    Parameters
    ----------
    caplog : pytest.LogCaptureFixture
        Captured log records used while exercising the retry path.
    """
    iface = cast(Any, object.__new__(SerialInterface))
    clock = _VirtualClock()
    monkeypatch.setattr(device_actions, "time", clock)
    iface.close = MagicMock()

    # First attempt fails, second succeeds.
    failures = iter([RuntimeError("not yet")])
    success_seen = {"value": False}

    def _connect() -> None:
        try:
            err = next(failures)
        except StopIteration:
            success_seen["value"] = True
            return
        raise err

    iface.connect = MagicMock(side_effect=_connect)

    with caplog.at_level(logging.DEBUG):
        result = main_module._post_factory_reset_ready_probe(cast(Any, iface))

    assert result is True
    assert success_seen["value"] is True
    assert iface.connect.call_count == 2
    assert iface.close.call_count == 3


@pytest.mark.unit
def test_post_factory_reset_ready_probe_releases_port_between_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed probe attempt must release the port before the retry delay so
    the next attempt cannot fail because the previous attempt still holds the
    serial device open locally instead of the device being absent."""
    iface = cast(Any, object.__new__(SerialInterface))
    clock = _VirtualClock()
    monkeypatch.setattr(device_actions, "time", clock)
    iface.close = MagicMock()
    iface.connect = MagicMock(side_effect=RuntimeError("port held by previous attempt"))

    result = main_module._post_factory_reset_ready_probe(cast(Any, iface))

    assert result is False
    assert iface.connect.call_count == (
        main_module.FACTORY_RESET_READY_PROBE_MAX_ATTEMPTS
    )
    assert iface.close.call_count == (
        main_module.FACTORY_RESET_READY_PROBE_MAX_ATTEMPTS + 2
    )


@pytest.mark.unit
def test_post_factory_reset_ready_probe_returns_true_for_non_serial_interface() -> None:
    """Wi-Fi / TCP interfaces should be ignored (return ``True``) so the caller never errors out."""

    class _NotSerial(MeshInterface):
        def __init__(self) -> None:  # noqa: D401 - test double
            pass

    iface = cast(Any, _NotSerial())
    # No connect/close methods are needed because the function short-circuits.
    assert main_module._post_factory_reset_ready_probe(iface) is True


@pytest.mark.unit
def test_temporary_instance_attributes_restores_existing_and_missing_values() -> None:
    instance = SimpleNamespace(existing="before")

    with main_module._temporary_instance_attributes(
        instance, {"existing": "during", "temporary": 42}
    ):
        assert instance.existing == "during"
        assert instance.temporary == 42

    assert instance.existing == "before"
    assert not hasattr(instance, "temporary")


@pytest.mark.unit
def test_temporary_instance_attributes_does_not_shadow_inherited_values() -> None:
    class WithInheritedValue:
        inherited = "class-value"

    instance = WithInheritedValue()

    with main_module._temporary_instance_attributes(
        instance, {"inherited": "temporary"}
    ):
        assert instance.inherited == "temporary"
        assert vars(instance)["inherited"] == "temporary"

    assert instance.inherited == "class-value"
    assert "inherited" not in vars(instance)


@pytest.mark.unit
def test_post_factory_reset_ready_probe_restores_existing_overrides() -> None:
    iface = cast(Any, object.__new__(SerialInterface))
    iface.close = MagicMock()
    iface.connect = MagicMock()
    iface._connect_wait_timeout_seconds = 11.0
    iface._connect_retry_budget_seconds = 12.0
    iface._suppress_connect_failure_logging = False

    main_module._post_factory_reset_ready_probe(cast(Any, iface))

    assert iface._connect_wait_timeout_seconds == 11.0
    assert iface._connect_retry_budget_seconds == 12.0
    assert iface._suppress_connect_failure_logging is False


@pytest.mark.unit
def test_post_factory_reset_ready_probe_logs_final_close_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    iface = cast(Any, object.__new__(SerialInterface))
    iface.connect = MagicMock()
    iface.close = MagicMock(side_effect=[None, RuntimeError("close failed")])

    with caplog.at_level(logging.DEBUG):
        main_module._post_factory_reset_ready_probe(cast(Any, iface))

    assert "Factory reset: serial close failed." in caplog.text


@pytest.mark.unit
def test_local_factory_reset_rejects_invalid_timeout_before_send() -> None:
    """Invalid reset timeout input must fail before the destructive request is sent."""
    iface = SimpleNamespace(_acknowledgment=Acknowledgment())
    reset_node = SimpleNamespace(
        iface=iface,
        factoryReset=MagicMock(return_value=SimpleNamespace(id=123)),
    )

    with pytest.raises(
        ValueError, match="factory reset acceptance timeout must be positive"
    ):
        main_module._send_local_factory_reset_and_wait(
            reset_node,
            full=False,
            timeout=0,
        )

    reset_node.factoryReset.assert_not_called()


class _VirtualClock:
    """Deterministic monotonic clock and sleeper for factory-reset seams."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        """Return the virtual clock reading."""
        return self.now

    def sleep(self, seconds: float) -> None:
        """Advance the virtual clock instead of blocking.

        Parameters
        ----------
        seconds : float
            Virtual duration to advance.
        """
        self.now += seconds


def _failing_exit(message: str, return_value: int = 1) -> None:
    """Exit through the test seam by raising ``SystemExit``.

    Parameters
    ----------
    message : str
        CLI diagnostic associated with the exit.
    return_value : int
        Exit status to raise.
    """
    raise SystemExit(return_value)


def _command_hooks(prints: list[str]) -> device_actions.DeviceActionHooks:
    """Build hooks wiring the real reset wait and readiness probe.

    Parameters
    ----------
    prints : list[str]
        Sink receiving CLI output emitted by the command.

    Returns
    -------
    device_actions.DeviceActionHooks
        Hooks configured for the factory-reset command seam.
    """
    return device_actions.DeviceActionHooks(
        cli_exit=cast(Any, _failing_exit),
        cli_print=prints.append,
        set_pref=MagicMock(return_value=True),
        is_local_destination=MagicMock(return_value=True),
        send_local_factory_reset_and_wait=(
            lambda node, *, full: device_actions._send_local_factory_reset_and_wait(
                node, full=full, cli_print=prints.append
            )
        ),
        post_factory_reset_ready_probe=main_module._post_factory_reset_ready_probe,
        handle_ota_update=MagicMock(),
        build_lockdown_auth=MagicMock(),
        read_lockdown_passphrase_file=MagicMock(return_value=b"x"),
        send_lockdown_auth=MagicMock(),
        validate_lockdown_passphrase=MagicMock(return_value=b"x"),
    )


def _reset_command_iface(reset_node: Any, connect: Any) -> Any:
    """Build a bare SerialInterface double for the command-level seam.

    Parameters
    ----------
    reset_node : Any
        Node double returned by ``getNode``.
    connect : Any
        Callable installed as the serial reconnect seam.

    Returns
    -------
    Any
        Minimal SerialInterface-compatible command double.
    """
    iface = cast(Any, object.__new__(SerialInterface))
    iface._acknowledgment = Acknowledgment()
    iface.isConnected = threading.Event()
    iface.isConnected.set()
    iface.socket = object()
    iface.stream = object()
    iface._wait_for_request_ack = None
    iface._raise_wait_error_if_present = None
    iface._retire_wait_request = None
    iface.close = MagicMock()
    iface.connect = connect
    iface.getNode = MagicMock(return_value=reset_node)
    return iface


def _reset_command_context(iface: Any) -> CliContext:
    """Build factory-reset-device CLI arguments for the command seam.

    Parameters
    ----------
    iface : Any
        Interface double used by the command context.

    Returns
    -------
    CliContext
        Context configured for a local full factory reset.
    """
    return CliContext(
        interface=cast(MeshInterface, iface),
        args=argparse.Namespace(
            reboot=False,
            reboot_ota=False,
            enter_dfu=False,
            shutdown=False,
            ota_update=None,
            device_metadata=False,
            begin_edit=False,
            commit_edit=False,
            factory_reset=False,
            factory_reset_device=True,
            dest="^local",
        ),
        get_node_kwargs={},
        outcome=ActionOutcome(),
    )


def _run_reset_command(
    monkeypatch: pytest.MonkeyPatch,
    clock: _VirtualClock,
    iface: Any,
    reset_node: Any,
) -> tuple[list[str], _VirtualClock]:
    """Drive the real reset handler under a virtual clock.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Fixture used to replace the device-action clock.
    clock : _VirtualClock
        Deterministic clock used by reset acceptance and readiness probing.
    iface : Any
        Serial-interface command double.
    reset_node : Any
        Node double receiving the destructive reset request.

    Returns
    -------
    tuple[list[str], _VirtualClock]
        Captured CLI output and the advanced virtual clock.
    """
    monkeypatch.setattr(device_actions, "time", clock)
    prints: list[str] = []
    device_actions._handle_reboot_and_reset_actions(
        _reset_command_context(iface), _command_hooks(prints)
    )
    return prints, clock


@pytest.mark.unit
def test_factory_reset_implicit_ack_ends_acceptance_before_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End the acceptance phase promptly when an implicit ACK arrives.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Fixture used to install the deterministic reset clock.
    """
    clock = _VirtualClock()
    timeline: dict[str, float] = {}

    def _connect() -> None:
        raise AssertionError("the reset path must not probe readiness")

    def _factory_reset(*, full: bool) -> SimpleNamespace:
        assert full is True
        timeline["sent_at"] = clock.now
        return SimpleNamespace(id=123)

    reset_node = SimpleNamespace(factoryReset=MagicMock(side_effect=_factory_reset))
    iface = _reset_command_iface(reset_node, _connect)
    reset_node.iface = iface
    acceptance_finished_at: dict[str, float | None] = {"value": None}
    real_wait = device_actions._send_local_factory_reset_and_wait

    def _timed_wait(node: Any, *, full: bool, cli_print: Any) -> Any:
        result = real_wait(node, full=full, cli_print=cli_print)
        acceptance_finished_at["value"] = clock.now
        return result

    monkeypatch.setattr(
        device_actions, "_send_local_factory_reset_and_wait", _timed_wait
    )

    def _implicit_ack_when_settled(seconds: float) -> None:
        clock.now += seconds
        if clock.now >= 0.05:
            iface._acknowledgment.receivedImplAck = True

    clock.sleep = _implicit_ack_when_settled  # type: ignore[method-assign]
    prints, clock = _run_reset_command(monkeypatch, clock, iface, reset_node)

    assert reset_node.factoryReset.call_count == 1
    assert timeline["sent_at"] == 0.0
    assert acceptance_finished_at["value"] is not None
    assert acceptance_finished_at["value"] < 1.0
    assert acceptance_finished_at["value"] < (
        device_actions.FACTORY_RESET_ACCEPTANCE_TIMEOUT_SECONDS
    )
    assert any("Waiting for factory reset acknowledgment" in text for text in prints)
    assert not hasattr(iface, "_connect_wait_timeout_seconds")


@pytest.mark.unit
def test_factory_reset_returns_to_shell_when_session_stays_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A quiet session still returns to the shell inside the bounded budget.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Fixture used to install the deterministic reset clock.

    The destructive request is resent on its interval; when no acceptance
    signal arrives the command informs the user and exits successfully
    instead of hanging on a transport that cannot report the reboot.
    """
    clock = _VirtualClock()
    connect_calls: list[float] = []

    def _connect() -> None:
        connect_calls.append(clock.now)

    reset_node = SimpleNamespace(
        factoryReset=MagicMock(return_value=SimpleNamespace(id=321))
    )
    iface = _reset_command_iface(reset_node, _connect)
    reset_node.iface = iface
    _run_reset_command(monkeypatch, clock, iface, reset_node)

    assert reset_node.factoryReset.call_count == 2
    assert connect_calls == []
    assert clock.now == pytest.approx(
        device_actions.FACTORY_RESET_ACCEPTANCE_TIMEOUT_SECONDS
    )


@pytest.mark.unit
def test_local_factory_reset_resends_while_acceptance_pending() -> None:
    """A silently dropped first request is resent until the device reboots."""
    connected = threading.Event()
    connected.set()
    iface = SimpleNamespace(
        isConnected=connected,
        _acknowledgment=Acknowledgment(),
        _retire_wait_request=MagicMock(),
        MeshInterfaceError=RuntimeError,
    )
    reset_node = SimpleNamespace(iface=iface)
    ids = iter([100, 200])

    def _factory_reset(*, full: bool) -> SimpleNamespace:
        request = SimpleNamespace(id=next(ids))
        if request.id == 200:

            def _disconnect_after_resend() -> None:
                time.sleep(0.02)
                pub.sendMessage("meshtastic.connection.lost", interface=iface)

            threading.Thread(target=_disconnect_after_resend, daemon=True).start()
        return request

    reset_node.factoryReset = MagicMock(side_effect=_factory_reset)

    with patch.object(device_actions, "FACTORY_RESET_RESEND_INTERVAL_SECONDS", 0.05):
        request = main_module._send_local_factory_reset_and_wait(
            reset_node,
            full=False,
            timeout=5.0,
        )

    assert request is not None
    assert request.id == 200
    assert reset_node.factoryReset.call_count == 2


@pytest.mark.unit
def test_local_factory_reset_skip_resend_when_original_request_acknowledged() -> None:
    """An original request completed between polls is returned, not resent."""
    connected = threading.Event()
    connected.set()
    iface = SimpleNamespace(
        isConnected=connected,
        _acknowledgment=Acknowledgment(),
        _retire_wait_request=MagicMock(),
        _raise_wait_error_if_present=MagicMock(),
        MeshInterfaceError=RuntimeError,
    )
    reset_node = SimpleNamespace(
        iface=iface,
        factoryReset=MagicMock(
            side_effect=[SimpleNamespace(id=300), SimpleNamespace(id=400)]
        ),
    )
    wait_calls = {"count": 0}

    def _acknowledged_after_first_poll(
        acknowledgment_attr: str, request_id: int, *, timeout_seconds: float = 0.0
    ) -> bool:
        _ = (acknowledgment_attr, request_id)
        wait_calls["count"] += 1
        if wait_calls["count"] == 1:
            # Hold the first acceptance poll past the resend interval so the
            # resend decision observes a completion that arrived right after
            # the poll saw nothing.
            time.sleep(0.06)
            return False
        return True

    iface._wait_for_request_ack = _acknowledged_after_first_poll

    with patch.object(device_actions, "FACTORY_RESET_RESEND_INTERVAL_SECONDS", 0.05):
        request = main_module._send_local_factory_reset_and_wait(
            reset_node,
            full=False,
            timeout=0.4,
        )

    assert request is not None
    assert request.id == 300
    assert reset_node.factoryReset.call_count == 1


@pytest.mark.unit
def test_local_factory_reset_resend_caps_at_max_sends() -> None:
    """Resend attempts stop at the configured cap and the timeout still fires."""
    connected = threading.Event()
    connected.set()
    iface = SimpleNamespace(
        isConnected=connected,
        _acknowledgment=Acknowledgment(),
        _retire_wait_request=MagicMock(),
        MeshInterfaceError=RuntimeError,
    )
    reset_node = SimpleNamespace(
        iface=iface,
        factoryReset=MagicMock(return_value=SimpleNamespace(id=789)),
    )

    with patch.object(
        device_actions,
        "FACTORY_RESET_RESEND_INTERVAL_SECONDS",
        0.05,
    ), patch.object(device_actions, "FACTORY_RESET_MAX_SENDS", 3):
        request = main_module._send_local_factory_reset_and_wait(
            reset_node,
            full=False,
            timeout=0.4,
        )

    assert request is not None
    assert reset_node.factoryReset.call_count == 3


@pytest.mark.unit
def test_local_factory_reset_resend_send_failure_does_not_crash() -> None:
    """A failed resend attempt is contained and the canonical timeout remains."""
    connected = threading.Event()
    connected.set()
    iface = SimpleNamespace(
        isConnected=connected,
        _acknowledgment=Acknowledgment(),
        _retire_wait_request=MagicMock(),
        MeshInterfaceError=RuntimeError,
    )
    reset_node = SimpleNamespace(iface=iface)
    reset_node.factoryReset = MagicMock(
        side_effect=[SimpleNamespace(id=911), RuntimeError("port closed")]
    )

    with patch.object(device_actions, "FACTORY_RESET_RESEND_INTERVAL_SECONDS", 0.05):
        request = main_module._send_local_factory_reset_and_wait(
            reset_node,
            full=False,
            timeout=0.3,
        )

    assert request is not None
    assert request.id == 911
    assert reset_node.factoryReset.call_count == 2
