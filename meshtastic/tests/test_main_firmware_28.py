"""Meshtastic unit tests for __main__.py."""

# pylint: disable=C0302,W0613,R0917

import sys
import threading
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from pubsub import pub

import meshtastic.__main__ as main_module
from meshtastic.__main__ import (
    initParser,
)
from meshtastic._topics import LOCKDOWN_STATUS_TOPIC
from meshtastic.cli.device_actions import _wait_for_initial_lockdown_status
from meshtastic.region_presets import RegionPresetInfo, decode_region_preset_map

# from ..radioconfig_pb2 import UserPreferences
# import meshtastic.config_pb2
from ..protobuf import config_pb2, mesh_pb2

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


def _run_region_preset_cli(
    monkeypatch: pytest.MonkeyPatch,
    *extra_args: str,
    region_preset_map: mesh_pb2.LoRaRegionPresetMap | None = None,
    region_presets: dict[int, RegionPresetInfo] | None = None,
) -> MagicMock:
    cli_args = ["meshtastic", "--show-region-presets"]
    if "--dest" not in extra_args:
        cli_args.extend(("--dest", MAIN_LOCAL_ADDR))
    cli_args.extend(extra_args)
    monkeypatch.setattr(sys, "argv", cli_args)
    initParser()
    interface = MagicMock()
    interface.devPath = ""
    interface.myInfo = SimpleNamespace(my_node_num=int("25d6e474", 16))
    interface.regionPresetMap = region_preset_map
    if region_presets is not None:
        interface.regionPresets = region_presets
    elif region_preset_map is None:
        interface.regionPresets = {}
    else:
        interface.regionPresets = decode_region_preset_map(region_preset_map)
    main_module.onConnected(interface)
    return interface


def _init_lockdown_cli(monkeypatch: pytest.MonkeyPatch, *cli_args: str) -> None:
    effective_args = list(cli_args)
    if "--dest" not in effective_args:
        effective_args.extend(("--dest", MAIN_LOCAL_ADDR))
    monkeypatch.setattr(sys, "argv", ["meshtastic", *effective_args])
    initParser()


def _run_lockdown_cli(
    monkeypatch: pytest.MonkeyPatch,
    *cli_args: str,
    status: mesh_pb2.LockdownStatus | None = None,
    authorization_state: int = mesh_pb2.LockdownStatus.UNLOCKED,
    initial_state: int | None = mesh_pb2.LockdownStatus.LOCKED,
) -> tuple[MagicMock, MagicMock, MagicMock]:
    _init_lockdown_cli(monkeypatch, *cli_args)
    interface = MagicMock()
    interface.devPath = ""
    interface.myInfo = SimpleNamespace(my_node_num=int("25d6e474", 16))
    build = MagicMock(return_value=object())
    send = MagicMock(return_value=status)
    if "--lockdown-lock-now" in cli_args:
        interface.lockdownStatus = (
            None if initial_state is None else mesh_pb2.LockdownStatus(state=initial_state)  # type: ignore[arg-type]
        )
        send.side_effect = [
            mesh_pb2.LockdownStatus(state=authorization_state),  # type: ignore[arg-type]
            status,
        ]
        monkeypatch.setattr(main_module.getpass, "getpass", lambda _prompt: "secret")
    monkeypatch.setattr(main_module, "build_lockdown_auth", build)
    monkeypatch.setattr(main_module, "send_lockdown_auth", send)
    main_module.onConnected(interface)
    return interface, build, send


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_show_region_presets_reports_absent_metadata_and_closes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    interface = _run_region_preset_cli(
        monkeypatch,
        region_preset_map=None,
    )

    output = capsys.readouterr().out
    assert "did not provide usable region/preset compatibility metadata" in output
    interface.close.assert_called_once_with()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_lockdown_cli_unlock_from_file(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    read_file = MagicMock(return_value=b"file-secret")
    monkeypatch.setattr(main_module, "read_lockdown_passphrase_file", read_file)
    status = mesh_pb2.LockdownStatus(state=mesh_pb2.LockdownStatus.UNLOCKED)

    interface, build, send = _run_lockdown_cli(
        monkeypatch,
        "--lockdown-unlock",
        "--lockdown-passphrase-file",
        "/tmp/secret",
        status=status,
    )

    read_file.assert_called_once_with("/tmp/secret")
    build.assert_called_once_with(
        b"file-secret",
        boots_remaining=0,
        valid_until_epoch=0,
        max_session_seconds=0,
        lock_now=False,
        disable=False,
    )
    send.assert_called_once_with(
        interface, build.return_value, timeout=20.0, allow_reboot_without_status=False
    )
    assert "Lockdown status: UNLOCKED" in capsys.readouterr().out


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_show_region_presets_reports_empty_metadata(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    empty_map = mesh_pb2.LoRaRegionPresetMap()
    interface = _run_region_preset_cli(
        monkeypatch,
        region_preset_map=empty_map,
    )

    output = capsys.readouterr().out
    assert "did not provide usable region/preset compatibility metadata" in output
    assert "preset choices remain unconstrained" in output
    assert interface.regionPresetMap is empty_map
    assert dict(interface.regionPresets) == {}
    interface.close.assert_called_once_with()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_show_region_presets_reports_malformed_metadata(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    malformed_map = mesh_pb2.LoRaRegionPresetMap()
    malformed_entry = malformed_map.region_groups.add()
    malformed_entry.region = config_pb2.Config.LoRaConfig.US
    malformed_entry.group_index = 1

    interface = _run_region_preset_cli(
        monkeypatch,
        region_preset_map=malformed_map,
    )

    output = capsys.readouterr().out
    assert "did not provide usable region/preset compatibility metadata" in output
    assert "preset choices remain unconstrained" in output
    assert interface.regionPresetMap is malformed_map
    assert dict(interface.regionPresets) == {}
    interface.close.assert_called_once_with()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_show_region_presets_rejects_remote_destination(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    interface = _run_region_preset_cli(
        monkeypatch,
        "--dest",
        "!deadbeef",
        region_preset_map=mesh_pb2.LoRaRegionPresetMap(),
    )

    output = capsys.readouterr().out
    assert "available only from the local node" in output
    interface.close.assert_called_once_with()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_show_region_presets_formats_known_unknown_and_licensed_values(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    known = RegionPresetInfo(
        presets=(config_pb2.Config.LoRaConfig.LONG_FAST, 999),
        default_preset=config_pb2.Config.LoRaConfig.LONG_FAST,
        licensed_only=True,
    )
    unknown = RegionPresetInfo(
        presets=(998,),
        default_preset=997,
        licensed_only=False,
    )

    interface = _run_region_preset_cli(
        monkeypatch,
        "--dest",
        MAIN_LOCAL_ADDR,
        region_preset_map=mesh_pb2.LoRaRegionPresetMap(),
        region_presets={config_pb2.Config.LoRaConfig.US: known, 999: unknown},
    )

    output = capsys.readouterr().out
    assert "US: default=LONG_FAST licensed-only; presets=LONG_FAST,PRESET_999" in output
    assert "REGION_999: default=PRESET_997; presets=PRESET_998" in output
    interface.close.assert_called_once_with()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_region_preset_cli_without_flag_does_not_read_capability_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["meshtastic", "--dest", MAIN_LOCAL_ADDR],
    )
    initParser()
    interface = MagicMock()
    interface.devPath = ""
    interface.myInfo = SimpleNamespace(my_node_num=int("25d6e474", 16))

    main_module.onConnected(interface)

    assert "region/preset" not in capsys.readouterr().out


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_lockdown_cli_rejects_remote_destination(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meshtastic",
            "--lockdown-unlock",
            "--lockdown-passphrase",
            "secret",
            "--dest",
            "!deadbeef",
        ],
    )
    initParser()
    interface = MagicMock()
    interface.devPath = ""
    interface.myInfo = SimpleNamespace(my_node_num=int("25d6e474", 16))
    build = MagicMock()
    send = MagicMock()
    monkeypatch.setattr(main_module, "build_lockdown_auth", build)
    monkeypatch.setattr(main_module, "send_lockdown_auth", send)

    with pytest.raises(SystemExit) as exc_info:
        main_module.onConnected(interface)

    assert exc_info.value.code == 1
    assert (
        "Lockdown commands apply only to the directly connected local node."
        in capsys.readouterr().err
    )
    build.assert_not_called()
    send.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_lockdown_cli_rejects_unacknowledged_command_line_secret(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meshtastic",
            "--lockdown-unlock",
            "--lockdown-passphrase",
            "secret",
            "--dest",
            MAIN_LOCAL_ADDR,
        ],
    )
    initParser()
    interface = MagicMock()
    interface.devPath = ""
    interface.myInfo = SimpleNamespace(my_node_num=int("25d6e474", 16))
    validate = MagicMock()
    build = MagicMock()
    send = MagicMock()
    monkeypatch.setattr(main_module, "validate_lockdown_passphrase", validate)
    monkeypatch.setattr(main_module, "build_lockdown_auth", build)
    monkeypatch.setattr(main_module, "send_lockdown_auth", send)

    with pytest.raises(SystemExit) as exc_info:
        main_module.onConnected(interface)

    assert exc_info.value.code == 1
    assert (
        "--lockdown-passphrase requires "
        "--insecure-lockdown-passphrase-on-command-line" in capsys.readouterr().err
    )
    validate.assert_not_called()
    build.assert_not_called()
    send.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_lockdown_cli_command_line_secret_formats_unknown_state_and_backoff(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    validate = MagicMock(return_value=b"secret")
    monkeypatch.setattr(main_module, "validate_lockdown_passphrase", validate)
    status = mesh_pb2.LockdownStatus(state=999, backoff_seconds=7)  # type: ignore[arg-type]

    _interface, build, _send = _run_lockdown_cli(
        monkeypatch,
        "--lockdown-unlock",
        "--lockdown-passphrase",
        "secret",
        "--insecure-lockdown-passphrase-on-command-line",
        status=status,
    )

    validate.assert_called_once_with(b"secret")
    assert build.call_args.args == (b"secret",)
    output = capsys.readouterr().out
    assert "Lockdown status: STATE_999" in output
    assert "Retry backoff: 7s" in output


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_lockdown_cli_provision_confirmation_abort(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")
    _init_lockdown_cli(monkeypatch, "--lockdown-provision")
    interface = MagicMock()
    interface.devPath = ""
    interface.myInfo = SimpleNamespace(my_node_num=int("25d6e474", 16))

    with pytest.raises(SystemExit):
        main_module.onConnected(interface)

    assert "Aborted." in capsys.readouterr().err


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_lockdown_cli_provision_rejects_mismatched_interactive_passphrases(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    prompts = iter(["secret", "different"])
    monkeypatch.setattr(main_module.getpass, "getpass", lambda _prompt: next(prompts))
    _init_lockdown_cli(monkeypatch, "--lockdown-provision")
    interface = MagicMock()
    interface.devPath = ""
    interface.myInfo = SimpleNamespace(my_node_num=int("25d6e474", 16))

    with pytest.raises(SystemExit):
        main_module.onConnected(interface)

    assert "Lockdown passphrases do not match." in capsys.readouterr().err


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_lockdown_cli_provision_reports_auth_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    prompts = iter(["secret", "secret"])
    monkeypatch.setattr(main_module.getpass, "getpass", lambda _prompt: next(prompts))
    monkeypatch.setattr(
        main_module, "validate_lockdown_passphrase", lambda value: value
    )
    failed = mesh_pb2.LockdownStatus(state=mesh_pb2.LockdownStatus.UNLOCK_FAILED)
    _init_lockdown_cli(monkeypatch, "--lockdown-provision")
    interface = MagicMock()
    interface.devPath = ""
    interface.myInfo = SimpleNamespace(my_node_num=int("25d6e474", 16))
    monkeypatch.setattr(
        main_module, "build_lockdown_auth", MagicMock(return_value=object())
    )
    monkeypatch.setattr(
        main_module, "send_lockdown_auth", MagicMock(return_value=failed)
    )

    with pytest.raises(SystemExit):
        main_module.onConnected(interface)

    captured = capsys.readouterr()
    assert "Lockdown status: UNLOCK_FAILED" in captured.out
    assert "Lockdown authentication failed." in captured.err


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_lockdown_cli_lock_now_allows_reboot_without_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    read = MagicMock(return_value=b"file secret")
    monkeypatch.setattr(main_module, "read_lockdown_passphrase_file", read)
    interface, build, send = _run_lockdown_cli(
        monkeypatch,
        "--lockdown-lock-now",
        "--lockdown-yes",
        "--lockdown-passphrase-file",
        "/tmp/secret",
        status=None,
    )
    read.assert_called_once_with("/tmp/secret")
    assert build.call_args.kwargs["lock_now"] is True
    assert build.call_args_list[0].args == (b"file secret",)
    assert build.call_args_list[0].kwargs["lock_now"] is False
    assert send.call_count == 2
    assert all(call.args[0] is interface for call in send.call_args_list)
    assert send.call_args_list[0].kwargs["allow_reboot_without_status"] is False
    assert send.call_args.kwargs["allow_reboot_without_status"] is True
    assert "device may already be rebooting" in capsys.readouterr().out


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_lockdown_cli_disable_sets_disable_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        main_module, "read_lockdown_passphrase_file", lambda _path: b"secret"
    )
    status = mesh_pb2.LockdownStatus(state=mesh_pb2.LockdownStatus.UNLOCKED)
    _interface, build, _send = _run_lockdown_cli(
        monkeypatch,
        "--lockdown-disable",
        "--lockdown-yes",
        "--lockdown-passphrase-file",
        "/tmp/secret",
        status=status,
    )
    assert build.call_args.kwargs["disable"] is True


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_lockdown_cli_accepts_explicit_local_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = mesh_pb2.LockdownStatus(state=mesh_pb2.LockdownStatus.UNLOCKED)
    interface, _build, send = _run_lockdown_cli(
        monkeypatch,
        "--lockdown-lock-now",
        "--lockdown-yes",
        "--dest",
        MAIN_LOCAL_ADDR,
        status=status,
    )
    assert send.call_count == 2
    interface.close.assert_called_once_with()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize(
    "auth_result",
    [
        mesh_pb2.LockdownStatus.UNLOCK_FAILED,
        mesh_pb2.LockdownStatus.LOCKED,
        mesh_pb2.LockdownStatus.DISABLED,
        TimeoutError("authentication status timed out"),
    ],
)
def test_lockdown_cli_lock_now_refuses_unauthorized_reboot(
    monkeypatch: pytest.MonkeyPatch, auth_result: int | Exception
) -> None:
    """A fresh CLI connection must prove ownership before firmware can reboot."""
    send = (
        MagicMock(side_effect=auth_result)
        if isinstance(auth_result, Exception)
        else MagicMock(return_value=mesh_pb2.LockdownStatus(state=auth_result))  # type: ignore[arg-type]
    )
    monkeypatch.setattr(main_module, "send_lockdown_auth", send)
    monkeypatch.setattr(main_module.getpass, "getpass", lambda _prompt: "secret")
    _init_lockdown_cli(monkeypatch, "--lockdown-lock-now", "--lockdown-yes")
    interface = MagicMock()
    interface.devPath = ""
    interface.myInfo = SimpleNamespace(my_node_num=int("25d6e474", 16))
    interface.lockdownStatus = mesh_pb2.LockdownStatus(
        state=mesh_pb2.LockdownStatus.LOCKED
    )

    with pytest.raises(SystemExit) as exc_info:
        main_module.onConnected(interface)

    assert exc_info.value.code == 1
    send.assert_called_once()
    assert not send.call_args.args[1].lock_now


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize("initial_state", [None, mesh_pb2.LockdownStatus.DISABLED])
def test_lockdown_cli_lock_now_cannot_provision_disabled_device(
    monkeypatch: pytest.MonkeyPatch, initial_state: int | None
) -> None:
    """Reject unsupported or disabled lockdown without sending a provision request."""
    build = MagicMock()
    send = MagicMock()
    monkeypatch.setattr(main_module, "build_lockdown_auth", build)
    monkeypatch.setattr(main_module, "send_lockdown_auth", send)
    _init_lockdown_cli(
        monkeypatch, "--lockdown-lock-now", "--lockdown-yes", "--lockdown-wait", "0.01"
    )
    interface = MagicMock()
    interface.devPath = ""
    interface.myInfo = SimpleNamespace(my_node_num=int("25d6e474", 16))
    interface.lockdownStatus = None if initial_state is None else mesh_pb2.LockdownStatus(state=initial_state)  # type: ignore[arg-type]

    with pytest.raises(SystemExit) as exc_info:
        main_module.onConnected(interface)

    assert exc_info.value.code == 1
    build.assert_not_called()
    send.assert_not_called()


@pytest.mark.unit
def test_lockdown_cli_waits_for_status_after_configuration_completion() -> None:
    """The capability event can follow config_complete on a fresh USB connection."""
    interface = SimpleNamespace(lockdownStatus=None)
    status = mesh_pb2.LockdownStatus(state=mesh_pb2.LockdownStatus.LOCKED)

    def _deliver() -> None:
        interface.lockdownStatus = status  # type: ignore[assignment]
        pub.sendMessage(LOCKDOWN_STATUS_TOPIC, interface=interface, status=status)

    timer = threading.Timer(0.01, _deliver)
    timer.start()
    try:
        assert _wait_for_initial_lockdown_status(interface, 1.0) is status
    finally:
        timer.join(timeout=1.0)


@pytest.mark.unit
def test_lockdown_cli_status_wait_is_scoped_and_bounded() -> None:
    """Another interface's status cannot authorize the CLI's local connection."""
    interface = SimpleNamespace(lockdownStatus=None)
    status = mesh_pb2.LockdownStatus(state=mesh_pb2.LockdownStatus.UNLOCKED)
    timer = threading.Timer(
        0.01,
        lambda: pub.sendMessage(
            LOCKDOWN_STATUS_TOPIC, interface=object(), status=status
        ),
    )
    timer.start()
    try:
        assert _wait_for_initial_lockdown_status(interface, 0.05) is None
    finally:
        timer.join(timeout=1.0)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize(
    ("cli_args", "error", "expected"),
    (
        (
            (
                "--lockdown-unlock",
                "--lockdown-passphrase",
                "",
                "--insecure-lockdown-passphrase-on-command-line",
            ),
            ValueError("lockdown passphrase must be 1..32 bytes"),
            "Invalid lockdown options",
        ),
        (
            (
                "--lockdown-unlock",
                "--lockdown-passphrase-file",
                "/tmp/insecure-secret",
            ),
            PermissionError(
                "/tmp/insecure-secret mode is 0o640; "
                "lockdown passphrase files must be operator-only (0600)"
            ),
            "operator-only (0600)",
        ),
    ),
)
def test_lockdown_cli_maps_passphrase_errors_to_user_facing_exit(
    monkeypatch: pytest.MonkeyPatch,
    cli_args: tuple[str, ...],
    error: Exception,
    expected: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _init_lockdown_cli(monkeypatch, *cli_args)
    interface = MagicMock()
    interface.devPath = ""
    interface.myInfo = SimpleNamespace(my_node_num=1)
    if "--lockdown-passphrase-file" in cli_args:
        monkeypatch.setattr(
            main_module, "read_lockdown_passphrase_file", MagicMock(side_effect=error)
        )
    else:
        monkeypatch.setattr(
            main_module, "validate_lockdown_passphrase", MagicMock(side_effect=error)
        )

    with pytest.raises(SystemExit):
        main_module.onConnected(interface)

    assert expected in capsys.readouterr().err


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_lockdown_cli_maps_invalid_limits_to_user_facing_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        main_module, "read_lockdown_passphrase_file", lambda _path: b"secret"
    )
    _init_lockdown_cli(
        monkeypatch,
        "--lockdown-unlock",
        "--lockdown-passphrase-file",
        "/tmp/secret",
        "--lockdown-boots",
        "-1",
    )
    interface = MagicMock()
    interface.devPath = ""
    interface.myInfo = SimpleNamespace(my_node_num=1)

    with pytest.raises(SystemExit):
        main_module.onConnected(interface)

    output = capsys.readouterr().err
    assert "Invalid lockdown options" in output
    assert "boots_remaining must be between 0 and 255" in output


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize(
    "error",
    (
        TimeoutError("no LockdownStatus received before timeout"),
        ValueError("lockdown authentication is USB-serial only"),
        RuntimeError("device did not provide my_info"),
    ),
)
def test_lockdown_cli_maps_send_failures_to_user_facing_exit(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        main_module, "read_lockdown_passphrase_file", lambda _path: b"secret"
    )
    monkeypatch.setattr(main_module, "send_lockdown_auth", MagicMock(side_effect=error))
    _init_lockdown_cli(
        monkeypatch,
        "--lockdown-unlock",
        "--lockdown-passphrase-file",
        "/tmp/secret",
    )
    interface = MagicMock()
    interface.devPath = ""
    interface.myInfo = SimpleNamespace(my_node_num=1)

    with pytest.raises(SystemExit):
        main_module.onConnected(interface)

    output = capsys.readouterr().err
    assert "Lockdown command failed" in output
    assert str(error) in output


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_lockdown_cli_interactive_unlock_uses_single_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    getpass_mock = MagicMock(return_value="secret")
    validate_mock = MagicMock(return_value=b"secret")
    monkeypatch.setattr(main_module.getpass, "getpass", getpass_mock)
    monkeypatch.setattr(main_module, "validate_lockdown_passphrase", validate_mock)
    status = mesh_pb2.LockdownStatus(state=mesh_pb2.LockdownStatus.UNLOCKED)

    _interface, build, _send = _run_lockdown_cli(
        monkeypatch,
        "--lockdown-unlock",
        status=status,
    )

    getpass_mock.assert_called_once_with("Lockdown passphrase: ")
    validate_mock.assert_called_once_with(b"secret")
    assert build.call_args.args == (b"secret",)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_lockdown_cli_without_action_does_not_call_lockdown_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_lockdown_cli(monkeypatch)
    interface = MagicMock()
    interface.devPath = ""
    interface.myInfo = SimpleNamespace(my_node_num=int("25d6e474", 16))
    build = MagicMock()
    send = MagicMock()
    monkeypatch.setattr(main_module, "build_lockdown_auth", build)
    monkeypatch.setattr(main_module, "send_lockdown_auth", send)

    main_module.onConnected(interface)

    build.assert_not_called()
    send.assert_not_called()
    interface.close.assert_not_called()
