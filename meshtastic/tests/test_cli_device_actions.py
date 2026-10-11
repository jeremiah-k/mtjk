"""Focused unit tests for extracted device-action failure boundaries."""

from __future__ import annotations

import argparse
import getpass
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from pubsub import pub

from meshtastic import key_verification, ota, serial_interface, tcp_interface
from meshtastic.cli import device_actions
from meshtastic.cli.context import ActionOutcome, CliExit, CliContext
from meshtastic.mesh_interface import MeshInterface
from meshtastic.protobuf import mesh_pb2


class _DummyTCPInterface:
    """Minimal TCP-interface stand-in for OTA failure-path tests."""

    hostname = "mesh.local"

    def __init__(self) -> None:
        self.node = MagicMock()

    def getNode(self, *_args: object, **_kwargs: object) -> MagicMock:  # noqa: N802
        """Return the local-node test double expected by the OTA helper."""
        return self.node


def _returning_cli_exit(*_args: object, **_kwargs: object) -> None:
    """Model an injected exit seam that incorrectly returns to its caller."""


def _reset_args(**overrides: Any) -> argparse.Namespace:
    """Build reboot/reset argument defaults for factory-reset dispatch tests.

    Parameters
    ----------
    **overrides : Any
        Argument values that replace the shared defaults.

    Returns
    -------
    argparse.Namespace
        Reset command arguments consumed by the reboot/reset handler.
    """
    values: dict[str, Any] = {
        "reboot": False,
        "reboot_ota": False,
        "enter_dfu": False,
        "shutdown": False,
        "ota_update": None,
        "device_metadata": False,
        "begin_edit": False,
        "commit_edit": False,
        "factory_reset": False,
        "factory_reset_device": False,
        "dest": "^local",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _hooks(**overrides: Any) -> device_actions.DeviceActionHooks:
    """Build device-action hooks with a deliberately returning exit seam."""
    values: dict[str, Any] = {
        "cli_exit": _returning_cli_exit,
        "cli_print": MagicMock(),
        "set_pref": MagicMock(return_value=True),
        "is_local_destination": MagicMock(return_value=True),
        "send_local_factory_reset_and_wait": MagicMock(),
        "post_factory_reset_ready_probe": MagicMock(),
        "handle_ota_update": MagicMock(),
        "build_lockdown_auth": MagicMock(return_value=object()),
        "read_lockdown_passphrase_file": MagicMock(return_value=b"secret"),
        "send_lockdown_auth": MagicMock(return_value=None),
        "validate_lockdown_passphrase": MagicMock(return_value=b"secret"),
        "build_key_verification_admin": MagicMock(),
        "send_key_verification": MagicMock(),
    }
    values.update(overrides)
    return device_actions.DeviceActionHooks(**values)


def _context(interface: object, **args: object) -> CliContext:
    """Build the minimal connected CLI context needed by a focused handler test."""
    return CliContext(
        interface=cast(MeshInterface, interface),
        args=argparse.Namespace(**args),
        get_node_kwargs={},
        outcome=ActionOutcome(),
    )


def _install_clock(
    monkeypatch: pytest.MonkeyPatch,
    *,
    monotonic: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> None:
    """Install a device-runtime clock without changing the shared ``time`` module."""
    current_time = time
    monkeypatch.setattr(
        device_actions,
        "time",
        SimpleNamespace(
            monotonic=current_time.monotonic if monotonic is None else monotonic,
            sleep=current_time.sleep if sleep is None else sleep,
        ),
    )


@pytest.fixture
def isolated_device_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub nested device handlers so focused tests isolate top-level behavior."""
    for name in (
        "_handle_content_updates",
        "_handle_position_fields",
        "_handle_reboot_and_reset_actions",
        "_handle_node_database_actions",
    ):
        monkeypatch.setattr(device_actions, name, lambda *_args: None)


def _lockdown_context() -> CliContext:
    """Build a local unlock request that avoids interactive confirmation."""
    return _context(
        MagicMock(),
        lockdown_provision=False,
        lockdown_unlock=True,
        lockdown_lock_now=False,
        lockdown_disable=False,
        lockdown_yes=False,
        lockdown_passphrase_file="secret.txt",
        lockdown_passphrase=None,
        insecure_lockdown_passphrase_on_command_line=False,
        lockdown_boots=None,
        lockdown_valid_until=None,
        lockdown_max_session_seconds=None,
        lockdown_wait=1.0,
        dest="^local",
    )


def _prepare_ota(
    monkeypatch: pytest.MonkeyPatch, ota_factory: Any
) -> _DummyTCPInterface:
    """Install OTA seams and return a compatible TCP-interface test double."""
    monkeypatch.setattr(
        tcp_interface, "TCPInterface", _DummyTCPInterface
    )
    monkeypatch.setattr(ota, "ESP32WiFiOTA", ota_factory)
    _install_clock(monkeypatch, sleep=lambda _seconds: None)
    return _DummyTCPInterface()


def _run_ota(interface: _DummyTCPInterface) -> None:
    """Invoke the OTA helper with the returning-exit test seam."""
    device_actions._handle_ota_update(
        cast(MeshInterface, interface),
        SimpleNamespace(ota_update="firmware.bin", dest="^local"),
        {},
        cli_exit=cast(CliExit, _returning_cli_exit),
        cli_print=MagicMock(),
        is_local_destination=lambda *_args: True,
    )


@pytest.mark.unit
def test_device_action_hooks_preserve_legacy_keyword_constructor() -> None:
    """Key-verification seams default without breaking pre-2.8 hook construction."""
    values = {
        "cli_exit": MagicMock(),
        "cli_print": MagicMock(),
        "set_pref": MagicMock(),
        "is_local_destination": MagicMock(),
        "send_local_factory_reset_and_wait": MagicMock(),
        "post_factory_reset_ready_probe": MagicMock(),
        "handle_ota_update": MagicMock(),
        "build_lockdown_auth": MagicMock(),
        "read_lockdown_passphrase_file": MagicMock(),
        "send_lockdown_auth": MagicMock(),
        "validate_lockdown_passphrase": MagicMock(),
    }

    hooks = device_actions.DeviceActionHooks(**values)

    assert (
        hooks.build_key_verification_admin is key_verification.buildKeyVerificationAdmin
    )
    assert hooks.send_key_verification is key_verification.sendKeyVerification


@pytest.mark.unit
@pytest.mark.parametrize("raw_altitude", ["not-an-altitude", str(1 << 31)])
def test_altitude_guards_reject_returning_cli_exit(raw_altitude: str) -> None:
    """Invalid altitude input must not continue into position assignment."""
    context = _context(
        MagicMock(),
        set_time=None,
        remove_position=False,
        setlat=None,
        setlon=None,
        setalt=raw_altitude,
        dest="^local",
    )

    with pytest.raises(AssertionError, match="cli_exit returned unexpectedly"):
        device_actions._handle_device_actions(context, _hooks())


@pytest.mark.unit
def test_invalid_position_fields_guard_rejects_returning_cli_exit() -> None:
    """Unknown position fields must not fall through after the exit seam returns."""
    interface = MagicMock()
    position = interface.getNode.return_value.localConfig.position
    position.PositionFlags.Value.side_effect = ValueError("unknown field")
    context = _context(interface, pos_fields=["bogus"], dest="^local")

    with pytest.raises(AssertionError, match="cli_exit returned unexpectedly"):
        device_actions._handle_position_fields(context, _hooks())


@pytest.mark.unit
def test_ota_constructor_guard_rejects_returning_cli_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OTA setup failure must not continue with an unbound OTA client."""

    def _raise_ota_error(*_args: object, **_kwargs: object) -> None:
        raise ota.OTAError("invalid image")

    interface = _prepare_ota(monkeypatch, _raise_ota_error)

    with pytest.raises(AssertionError, match="cli_exit returned unexpectedly"):
        _run_ota(interface)


@pytest.mark.unit
@pytest.mark.parametrize(
    "failure",
    [
        ota.OTAError("invalid"),
        ota.OTATransportError("lost"),
    ],
)
def test_ota_update_guards_reject_returning_cli_exit(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """OTA update failures must not fall through to the success path."""
    ota_client = MagicMock()
    ota_client.hash_bytes.return_value = b"hash"
    ota_client.update.side_effect = failure
    interface = _prepare_ota(monkeypatch, MagicMock(return_value=ota_client))

    with pytest.raises(AssertionError, match="cli_exit returned unexpectedly"):
        _run_ota(interface)

    expected_calls = (
        device_actions.OTA_MAX_RETRIES
        if isinstance(failure, ota.OTATransportError)
        else 1
    )
    assert ota_client.update.call_count == expected_calls


@pytest.mark.unit
@pytest.mark.parametrize(
    ("hook_name", "failure"),
    [
        ("build_lockdown_auth", ValueError("invalid")),
        ("send_lockdown_auth", RuntimeError("lost")),
    ],
)
def test_lockdown_action_guards_reject_returning_cli_exit(
    hook_name: str, failure: Exception
) -> None:
    """Lockdown failures must not continue with unbound auth or status values."""
    hooks = _hooks(**{hook_name: MagicMock(side_effect=failure)})

    with pytest.raises(AssertionError, match="cli_exit returned unexpectedly"):
        device_actions._handle_lockdown_action(_lockdown_context(), hooks)


@pytest.mark.unit
def test_lockdown_cli_secret_guard_rejects_returning_cli_exit() -> None:
    """A refused command-line secret must not be validated after the exit call."""
    args = SimpleNamespace(
        lockdown_passphrase_file=None,
        lockdown_passphrase="secret",
        insecure_lockdown_passphrase_on_command_line=False,
    )

    with pytest.raises(AssertionError, match="cli_exit returned unexpectedly"):
        device_actions._read_lockdown_passphrase(args, "unlock", _hooks())


@pytest.mark.unit
def test_lockdown_confirmation_mismatch_guard_rejects_returning_cli_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mismatched provisioning secrets must not continue into validation."""
    monkeypatch.setattr(
        getpass, "getpass", MagicMock(side_effect=["a", "b"])
    )
    args = SimpleNamespace(
        lockdown_passphrase_file=None,
        lockdown_passphrase=None,
        insecure_lockdown_passphrase_on_command_line=False,
    )

    with pytest.raises(AssertionError, match="cli_exit returned unexpectedly"):
        device_actions._read_lockdown_passphrase(args, "provision", _hooks())


@pytest.mark.unit
def test_lockdown_confirmation_reports_closed_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Destructive lockdown confirmation should explain non-interactive usage."""
    context = _lockdown_context()
    context.args.lockdown_unlock = False
    context.args.lockdown_provision = True
    context.args.lockdown_passphrase_file = "secret.txt"
    exit_mock = MagicMock()
    send_lockdown_auth = MagicMock(return_value=None)
    monkeypatch.setattr(
        "builtins.input", MagicMock(side_effect=EOFError("closed stdin"))
    )

    with pytest.raises(AssertionError, match="cli_exit returned unexpectedly"):
        device_actions._handle_lockdown_action(
            context,
            _hooks(cli_exit=exit_mock, send_lockdown_auth=send_lockdown_auth),
        )

    assert "--lockdown-yes" in str(exit_mock.call_args)
    send_lockdown_auth.assert_not_called()


@pytest.mark.unit
def test_lockdown_passphrase_reports_closed_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interactive passphrase EOF should recommend the file-based input path."""
    args = SimpleNamespace(
        lockdown_passphrase_file=None,
        lockdown_passphrase=None,
        insecure_lockdown_passphrase_on_command_line=False,
    )
    exit_mock = MagicMock()
    validate = MagicMock(return_value=b"secret")
    monkeypatch.setattr(
        getpass,
        "getpass",
        MagicMock(side_effect=EOFError("closed stdin")),
    )

    with pytest.raises(AssertionError, match="cli_exit returned unexpectedly"):
        device_actions._read_lockdown_passphrase(
            args,
            "unlock",
            _hooks(cli_exit=exit_mock, validate_lockdown_passphrase=validate),
        )

    assert "--lockdown-passphrase-file" in str(exit_mock.call_args)
    validate.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("failure_kind", "expected_fragment"),
    [
        ("transport", "requires a TCP connection"),
        ("destination", "directly connected local node"),
    ],
)
def test_ota_preconditions_reject_returning_cli_exit(
    monkeypatch: pytest.MonkeyPatch, failure_kind: str, expected_fragment: str
) -> None:
    """Rejected OTA preconditions must fail closed with the correct diagnostic."""
    interface: object
    if failure_kind == "transport":
        interface = MagicMock()
        local_destination = MagicMock(return_value=True)
    else:
        interface = _prepare_ota(monkeypatch, MagicMock())
        local_destination = MagicMock(return_value=False)
    exit_mock = MagicMock()

    with pytest.raises(AssertionError, match="cli_exit returned unexpectedly"):
        device_actions._handle_ota_update(
            cast(MeshInterface, interface),
            argparse.Namespace(ota_update="firmware.bin", dest="!remote"),
            {},
            cli_exit=cast(CliExit, exit_mock),
            cli_print=MagicMock(),
            is_local_destination=local_destination,
        )

    assert expected_fragment in str(exit_mock.call_args)


@pytest.mark.unit
@pytest.mark.usefixtures("isolated_device_handlers")
@pytest.mark.parametrize(
    ("args", "expected_fragment"),
    [
        ({"setlat": "91.0", "setlon": None, "setalt": None}, "latitude"),
        ({"setlat": None, "setlon": "181.0", "setalt": None}, "longitude"),
        ({"set_owner": "   "}, "Long Name"),
        ({"set_owner_short": "   "}, "Short Name"),
        ({"set_ham": "   "}, "Ham radio callsign"),
    ],
)
def test_device_validation_guards_reject_returning_cli_exit(
    args: dict[str, object],
    expected_fragment: str,
) -> None:
    """Fatal device validation must not reach a mutating operation after exit."""
    interface = MagicMock()
    defaults: dict[str, object] = {
        "set_time": None,
        "remove_position": False,
        "setlat": None,
        "setlon": None,
        "setalt": None,
        "set_owner": None,
        "set_owner_short": None,
        "set_is_unmessageable": None,
        "set_ham": None,
        "dest": "^local",
    }
    defaults.update(args)
    context = _context(interface, **defaults)
    exit_mock = MagicMock()
    hooks = _hooks(cli_exit=exit_mock)

    with pytest.raises(AssertionError, match="cli_exit returned unexpectedly"):
        device_actions._handle_device_actions(context, hooks)

    assert expected_fragment in str(exit_mock.call_args)
    interface.getNode.return_value.setFixedPosition.assert_not_called()
    interface.getNode.return_value.setOwner.assert_not_called()


@pytest.mark.unit
def test_lockdown_preconditions_reject_returning_cli_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lockdown must fail closed on non-local targets and rejected confirmation."""
    remote = _lockdown_context()
    remote.args.dest = "!remote"
    remote_send = MagicMock(return_value=None)
    hooks = _hooks(
        is_local_destination=MagicMock(return_value=False),
        send_lockdown_auth=remote_send,
    )
    with pytest.raises(AssertionError, match="cli_exit returned unexpectedly"):
        device_actions._handle_lockdown_action(remote, hooks)
    remote_send.assert_not_called()

    confirm = _lockdown_context()
    confirm.args.lockdown_unlock = False
    confirm.args.lockdown_provision = True
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")
    confirm_send = MagicMock(return_value=None)
    hooks = _hooks(
        is_local_destination=MagicMock(return_value=True),
        send_lockdown_auth=confirm_send,
    )
    with pytest.raises(AssertionError, match="cli_exit returned unexpectedly"):
        device_actions._handle_lockdown_action(confirm, hooks)
    confirm_send.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("isolated_device_handlers")
def test_numeric_zero_fixed_position_is_not_treated_as_omitted() -> None:
    """Programmatic zero coordinates must still enter the fixed-position action."""
    interface = MagicMock()
    context = _context(
        interface,
        set_time=None,
        remove_position=False,
        setlat=0.0,
        setlon=None,
        setalt=None,
        set_owner=None,
        set_owner_short=None,
        set_is_unmessageable=None,
        set_ham=None,
        dest="^local",
    )

    device_actions._handle_device_actions(context, _hooks())

    interface.getNode.return_value.setFixedPosition.assert_called_once_with(0.0, 0.0, 0)


@pytest.mark.unit
@pytest.mark.usefixtures("isolated_device_handlers")
def test_false_unmessageable_value_is_applied() -> None:
    """A programmatic False unmessageable value must not be dropped by truthiness."""
    interface = MagicMock()
    context = _context(
        interface,
        set_time=None,
        remove_position=False,
        setlat=None,
        setlon=None,
        setalt=None,
        set_owner=None,
        set_owner_short=None,
        set_is_unmessageable=False,
        set_ham=None,
        dest="^local",
    )

    device_actions._handle_device_actions(context, _hooks())

    interface.getNode.return_value.setOwner.assert_called_once_with(
        long_name=None, short_name=None, is_unmessagable=False
    )


@pytest.mark.unit
@pytest.mark.usefixtures("isolated_device_handlers")
def test_owner_long_and_short_names_share_one_write() -> None:
    """Combined owner names should be logged and written together exactly once."""
    interface = MagicMock()
    cli_print = MagicMock()
    context = _context(
        interface,
        set_time=None,
        remove_position=False,
        setlat=None,
        setlon=None,
        setalt=None,
        set_owner="Long Name",
        set_owner_short="LN",
        set_is_unmessageable=None,
        set_ham=None,
        dest="^local",
    )

    device_actions._handle_device_actions(context, _hooks(cli_print=cli_print))

    cli_print.assert_any_call("Setting device owner to Long Name and short name to LN")
    interface.getNode.return_value.setOwner.assert_called_once_with(
        long_name="Long Name", short_name="LN", is_unmessagable=None
    )


@pytest.mark.unit
def test_coordinate_parser_rejects_boolean_and_unparseable_values() -> None:
    """Boolean and nonnumeric coordinate values should terminate through the CLI seam."""
    for raw in (True, object()):
        with pytest.raises(AssertionError, match="cli_exit returned unexpectedly"):
            device_actions._parse_coordinate(raw, "latitude", _hooks())


@pytest.mark.unit
def test_position_fields_write_and_read_paths() -> None:
    """Position-field actions should cover both mutation and display contracts."""
    interface = MagicMock()
    node = interface.getNode.return_value
    position = node.localConfig.position
    position.PositionFlags.Value.side_effect = [1, 4]
    write_context = _context(interface, pos_fields=["ALTITUDE", "TIME"], dest="^local")
    set_pref = MagicMock(return_value=True)
    hooks = _hooks(set_pref=set_pref, cli_print=MagicMock())

    device_actions._handle_position_fields(write_context, hooks)

    set_pref.assert_called_once_with(position, "position_flags", "5")
    node.writeConfig.assert_called_once_with("position")
    interface.getNode.assert_called_once_with("^local")

    interface.getNode.reset_mock()
    position.PositionFlags.values.return_value = [1, 2, 4]
    position.position_flags = 5
    position.PositionFlags.Name.side_effect = lambda value: {1: "ALTITUDE", 4: "TIME"}[
        value
    ]
    read_context = _context(interface, pos_fields=[], dest="^local")
    read_print = MagicMock()
    device_actions._handle_position_fields(read_context, _hooks(cli_print=read_print))
    read_print.assert_called_once_with("ALTITUDE TIME")
    interface.getNode.assert_called_once_with("^local")


@pytest.mark.unit
def test_remote_factory_reset_uses_direct_factory_reset() -> None:
    """Remote reset must use Node.factoryReset without the local acceptance probe."""
    interface = MagicMock()
    reset_node = interface.getNode.return_value
    args = _reset_args(factory_reset=True, factory_reset_device=False, dest="!remote")
    context = CliContext(
        interface=cast(MeshInterface, interface),
        args=args,
        get_node_kwargs={},
        outcome=ActionOutcome(),
    )
    local_wait = MagicMock()
    hooks = _hooks(
        is_local_destination=MagicMock(return_value=False),
        send_local_factory_reset_and_wait=local_wait,
    )

    device_actions._handle_reboot_and_reset_actions(context, hooks)

    reset_node.factoryReset.assert_called_once_with(full=False)
    local_wait.assert_not_called()


@pytest.mark.unit
def test_local_factory_reset_device_returns_without_readiness_probe() -> None:
    """The command returns to the shell without the blocking readiness probe."""
    interface = MagicMock()
    reset_node = interface.getNode.return_value
    reset_node.factoryReset = MagicMock()
    args = _reset_args(factory_reset_device=True)
    context = CliContext(
        interface=cast(MeshInterface, interface),
        args=args,
        get_node_kwargs={},
        outcome=ActionOutcome(),
    )
    cli_exit = MagicMock(side_effect=SystemExit(1))
    local_wait = MagicMock()
    readiness_probe = MagicMock(return_value=False)
    hooks = _hooks(
        cli_exit=cli_exit,
        is_local_destination=MagicMock(return_value=True),
        send_local_factory_reset_and_wait=local_wait,
        post_factory_reset_ready_probe=readiness_probe,
    )

    device_actions._handle_reboot_and_reset_actions(context, hooks)

    local_wait.assert_called_once()
    readiness_probe.assert_not_called()
    cli_exit.assert_not_called()


@pytest.mark.unit
def test_factory_reset_probe_logs_close_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Serial readiness probing should tolerate both initial and final close failures."""

    class SerialDouble:
        """Serial-interface double whose close calls fail on both attempts."""

        def __init__(self) -> None:
            self.close = MagicMock(
                side_effect=[RuntimeError("initial"), RuntimeError("final")]
            )
            self.connect = MagicMock()

    serial = SerialDouble()
    monkeypatch.setattr(
        serial_interface, "SerialInterface", SerialDouble
    )
    ticks = iter([0.0, 0.1])
    _install_clock(monkeypatch, monotonic=lambda: next(ticks, 0.1))

    device_actions._post_factory_reset_ready_probe(cast(MeshInterface, serial))

    assert serial.close.call_count == 2
    serial.connect.assert_called_once_with()


@pytest.mark.unit
def test_factory_reset_accepts_implicit_ack_in_scoped_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An uncorrelated implicit ACK must accept the scoped wait without transport loss."""
    monkeypatch.setattr(pub, "subscribe", MagicMock())
    monkeypatch.setattr(pub, "unsubscribe", MagicMock())
    iface = MagicMock()
    raise_wait_error = MagicMock()

    def _never_completed(
        acknowledgment_attr: str, request_id: int, timeout_seconds: float = 0.0
    ) -> bool:
        return False

    iface._wait_for_request_ack = _never_completed
    iface._raise_wait_error_if_present = raise_wait_error
    node = MagicMock(iface=iface)

    def _factory_reset(*, full: bool) -> SimpleNamespace:
        # The implicit ACK is not request-correlated, so it can only surface
        # through the legacy acknowledgment flag.
        iface._acknowledgment.receivedImplAck = True
        return SimpleNamespace(id=4242)

    node.factoryReset = MagicMock(side_effect=_factory_reset)

    result = device_actions._send_local_factory_reset_and_wait(
        node, full=False, cli_print=MagicMock(), timeout=5.0
    )

    assert result is not None
    assert result.id == 4242
    raise_wait_error.assert_not_called()


@pytest.mark.unit
def test_factory_reset_legacy_acknowledgment_rejects_nak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy acknowledgment state should turn a received NAK into a reset error."""
    iface = MagicMock()
    iface.MeshInterfaceError = MeshInterface.MeshInterfaceError
    iface._wait_for_request_ack = None
    iface._raise_wait_error_if_present = None
    iface._acknowledgment.receivedAck = False
    iface._acknowledgment.receivedImplAck = False
    iface._acknowledgment.receivedNak = True
    node = MagicMock(iface=iface)
    node.factoryReset.return_value = SimpleNamespace(id=1)
    monkeypatch.setattr(pub, "subscribe", MagicMock())
    monkeypatch.setattr(pub, "unsubscribe", MagicMock())

    with pytest.raises(
        MeshInterface.MeshInterfaceError, match="rejected"
    ):
        device_actions._send_local_factory_reset_and_wait(
            node, full=False, cli_print=MagicMock(), timeout=1.0
        )


@pytest.mark.unit
def test_factory_reset_transport_change_checks_scoped_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reboot disconnect should still surface any scoped ACK/NAK error first."""
    iface = MagicMock()
    iface._wait_for_request_ack = None
    raise_wait_error = MagicMock()
    iface._raise_wait_error_if_present = raise_wait_error
    iface._acknowledgment.receivedAck = False
    iface._acknowledgment.receivedImplAck = False
    iface._acknowledgment.receivedNak = False
    iface.isConnected.is_set.return_value = False
    node = MagicMock(iface=iface)
    node.factoryReset.return_value = SimpleNamespace(id=0)
    monkeypatch.setattr(pub, "subscribe", MagicMock())
    monkeypatch.setattr(pub, "unsubscribe", MagicMock())
    _install_clock(monkeypatch, sleep=lambda _seconds: None)

    result = device_actions._send_local_factory_reset_and_wait(
        node, full=False, cli_print=MagicMock(), timeout=1.0
    )

    assert result is node.factoryReset.return_value
    raise_wait_error.assert_called_with("receivedNak", request_id=None)


@pytest.mark.unit
def test_factory_reset_scoped_wait_checks_error_before_return_and_retires_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request-scoped ACK completion must surface NAK state before cleanup."""
    events: list[str] = []
    iface = MagicMock()

    def _record_wait(*_args: object, **_kwargs: object) -> bool:
        events.append("wait")
        return True

    wait_for_request_ack = MagicMock(side_effect=_record_wait)
    raise_wait_error = MagicMock(
        side_effect=lambda *_args, **_kwargs: events.append("raise")
    )
    retire_wait = MagicMock(
        side_effect=lambda *_args, **_kwargs: events.append("retire")
    )
    iface._wait_for_request_ack = wait_for_request_ack
    iface._raise_wait_error_if_present = raise_wait_error
    iface._retire_wait_request = retire_wait
    node = MagicMock(iface=iface)
    request = cast(mesh_pb2.MeshPacket, SimpleNamespace(id=73))
    node.factoryReset.return_value = request
    monkeypatch.setattr(pub, "subscribe", MagicMock())
    monkeypatch.setattr(pub, "unsubscribe", MagicMock())

    result = device_actions._send_local_factory_reset_and_wait(
        node, full=False, cli_print=MagicMock(), timeout=1.0
    )

    assert result is request
    assert events == ["wait", "raise", "retire"]
    wait_for_request_ack.assert_called_once()
    wait_args = wait_for_request_ack.call_args
    assert wait_args.args[:2] == ("receivedNak", 73)
    assert (
        0
        < wait_args.kwargs["timeout_seconds"]
        <= (device_actions.FACTORY_RESET_ACCEPTANCE_POLL_SECONDS)
    )
    raise_wait_error.assert_called_once_with("receivedNak", request_id=73)
    retire_wait.assert_called_once_with("receivedNak", request_id=73)


@pytest.mark.unit
def test_factory_reset_timeout_checks_scoped_error_and_tolerates_unsubscribe_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Budget expiry should probe scoped error state and never mask unsubscribe failure."""
    iface = MagicMock()
    iface.MeshInterfaceError = MeshInterface.MeshInterfaceError
    iface._wait_for_request_ack = None
    raise_wait_error = MagicMock()
    iface._raise_wait_error_if_present = raise_wait_error
    iface._acknowledgment.receivedAck = False
    iface._acknowledgment.receivedImplAck = False
    iface._acknowledgment.receivedNak = False
    node = MagicMock(iface=iface)
    node.factoryReset.return_value = SimpleNamespace(id=0)
    # Two monotonic reads precede the deadline: the last-send stamp and the deadline
    # itself. The first loop check must then see a clock past the 1.0s deadline so the
    # wait times out instead of polling on a frozen clock.
    ticks = iter([0.0, 0.0, 2.0])
    _install_clock(monkeypatch, monotonic=lambda: next(ticks, 2.0))
    monkeypatch.setattr(pub, "subscribe", MagicMock())
    monkeypatch.setattr(
        pub,
        "unsubscribe",
        MagicMock(side_effect=RuntimeError("unsubscribe")),
    )

    result = device_actions._send_local_factory_reset_and_wait(
        node, full=False, cli_print=MagicMock(), timeout=1.0
    )

    assert result is node.factoryReset.return_value
    raise_wait_error.assert_called_with("receivedNak", request_id=None)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("argument_name", "value", "setter_name"),
    [
        ("set_canned_message", "hello", "set_canned_message"),
        ("set_ringtone", "ring", "set_ringtone"),
    ],
)
def test_content_update_waits_for_ack_only_when_write_is_sent(
    argument_name: str, value: str, setter_name: str
) -> None:
    """Supported content updates should close after the shared ACK/NAK wait."""
    interface = MagicMock()
    node = interface.getNode.return_value
    node.module_available.return_value = True
    args = {"set_canned_message": None, "set_ringtone": None, "dest": "^local"}
    args[argument_name] = value
    context = _context(interface, **args)

    device_actions._handle_content_updates(context, _hooks())

    assert context.outcome.close_now is True
    assert context.outcome.wait_for_ack_nak is True
    getattr(node, setter_name).assert_called_once_with(value)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("argument_name", "value", "setter_name"),
    [
        ("set_canned_message", "hello", "set_canned_message"),
        ("set_ringtone", "ring", "set_ringtone"),
    ],
)
def test_content_update_skips_ack_wait_when_module_is_unavailable(
    argument_name: str, value: str, setter_name: str
) -> None:
    """Excluded firmware modules must not arm an acknowledgment that cannot arrive."""
    interface = MagicMock()
    node = interface.getNode.return_value
    node.module_available.return_value = False
    args = {"set_canned_message": None, "set_ringtone": None, "dest": "^local"}
    args[argument_name] = value
    context = _context(interface, **args)

    device_actions._handle_content_updates(context, _hooks())

    assert context.outcome.close_now is True
    assert context.outcome.wait_for_ack_nak is False
    getattr(node, setter_name).assert_not_called()


@pytest.mark.unit
def test_position_fields_does_not_write_when_preference_assignment_fails() -> None:
    """Failed position preference assignment must stop before writeConfig."""
    interface = MagicMock()
    node = interface.getNode.return_value
    position = node.localConfig.position
    position.PositionFlags.Value.return_value = 1
    context = _context(interface, pos_fields=["ALTITUDE"], dest="^local")
    set_pref = MagicMock(return_value=False)

    with pytest.raises(AssertionError, match="cli_exit returned unexpectedly"):
        device_actions._handle_position_fields(
            context, _hooks(set_pref=set_pref, cli_print=MagicMock())
        )

    node.writeConfig.assert_not_called()
