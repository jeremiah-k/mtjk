"""Meshtastic unit tests for __main__.py."""

# pylint: disable=C0302,W0613,R0917

import base64
import logging
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, call, patch

import pytest
import yaml

import meshtastic.__main__ as main_module
from meshtastic import mt_config
from meshtastic.__main__ import (
    _prefix_base64_key,
    _set_missing_flags_false,
    export_config,
    main,
    onConnection,
    onReceive,
    traverseConfig,
)

# from ..ble_interface import BLEInterface
from ..node import Node

# from ..radioconfig_pb2 import UserPreferences
# import meshtastic.config_pb2
from ..protobuf import config_pb2, localonly_pb2
from ..protobuf.channel_pb2 import Channel  # pylint: disable=E0611
from ..serial_interface import SerialInterface
from ._main_legacy_support import (
    _build_configure_interface,
    _build_export_interface,
    _patch_fast_monotonic,
    _run_main_configure_file,
)

# from ..remote_hardware import onGPIOreceive
# from ..config_pb2 import Config

SDS_DISABLED_SENTINEL: int = 4_294_967_295
MAIN_LOCAL_ADDR: str = cast(str, main_module.__dict__["LOCAL_ADDR"])


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
def test_configure_paces_between_section_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pace a write burst without delaying after the final section."""
    config_path = tmp_path / "paced_config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "config": {
                    "bluetooth": {"enabled": True},
                    "display": {"screen_on_secs": 30},
                },
                "module_config": {
                    "ambient_lighting": {"current": 5},
                    "mqtt": {"enabled": True},
                },
            }
        ),
        encoding="utf-8",
    )
    iface, target_node = _build_configure_interface()
    events: list[tuple[str, object]] = []
    original_write = target_node.writeConfig.side_effect

    def _write_config(section: str) -> None:
        events.append(("write", section))
        original_write(section)

    target_node.writeConfig.side_effect = _write_config
    monkeypatch.setattr(
        main_module,
        "_pace_configure_write",
        lambda remaining: events.append(("pace", remaining)) if remaining else None,
    )
    args = SimpleNamespace(
        configure=[str(config_path)],
        dest="!12345678",
    )

    main_module._handle_configure_command(iface, args, {})

    assert events == [
        ("write", "bluetooth"),
        ("pace", 3),
        ("write", "display"),
        ("pace", 2),
        ("write", "ambient_lighting"),
        ("pace", 1),
        ("write", "mqtt"),
    ]
    target_node.commitSettingsTransaction.assert_called_once_with()


@pytest.mark.unit
def test_configure_write_pacer_uses_short_dedicated_delay() -> None:
    sleep = MagicMock()

    main_module._pace_configure_write(1, sleep_fn=sleep)
    main_module._pace_configure_write(0, sleep_fn=sleep)

    sleep.assert_called_once()
    delay = sleep.call_args.args[0]
    assert 0 < delay < main_module.CONFIG_APPLY_DELAY_SECONDS


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_rejects_invalid_enum_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --configure fails fast when enum values are invalid."""
    config_path = tmp_path / "invalid_enum.yaml"
    config_path.write_text(
        yaml.safe_dump({"config": {"bluetooth": {"mode": "NOT_A_MODE"}}}),
        encoding="utf-8",
    )
    iface, target_node = _build_configure_interface()

    with pytest.raises(SystemExit) as excinfo:
        _run_main_configure_file(config_path, iface, monkeypatch)

    out, err = capsys.readouterr()
    assert "does not have an enum called NOT_A_MODE" in out
    assert "Failed to apply config section 'bluetooth'" in err
    assert excinfo.value.code == 1
    target_node.writeConfig.assert_not_called()
    target_node.commitSettingsTransaction.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_rejects_invalid_security_base64(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --configure exits when base64-encoded security keys are malformed."""
    config_path = tmp_path / "invalid_base64.yaml"
    config_path.write_text(
        yaml.safe_dump({"config": {"security": {"privateKey": "base64:A"}}}),
        encoding="utf-8",
    )
    iface, target_node = _build_configure_interface()

    with pytest.raises(SystemExit) as excinfo:
        _run_main_configure_file(config_path, iface, monkeypatch)

    _, err = capsys.readouterr()
    assert "Failed to apply config section 'security'" in err
    assert excinfo.value.code == 1
    target_node.commitSettingsTransaction.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_applies_mixed_case_and_security_encodings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test --configure accepts mixed key casing and supported security key encodings."""
    private_key = bytes(range(32))
    public_key = bytes(range(32, 64))
    admin_key_1 = bytes(range(64, 96))
    admin_key_2 = bytes(range(96, 128))

    config_path = tmp_path / "mixed_case.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "config": {
                    "bluetooth": {
                        "enabled": True,
                        "mode": "NO_PIN",
                        "fixedPin": 777777,
                    },
                    "display": {
                        "units": "IMPERIAL",
                        "use12HClock": True,
                        "screenOnSecs": 66,
                    },
                    "power": {
                        "lsSecs": 222,
                        "waitBluetoothSecs": 77,
                        "minWakeSecs": 11,
                        "sdsSecs": SDS_DISABLED_SENTINEL,
                    },
                    "security": {
                        "privateKey": f"base64:{base64.b64encode(private_key).decode()}",
                        "public_key": "0x" + public_key.hex(),
                        "adminKey": [
                            f"base64:{base64.b64encode(admin_key_1).decode()}",
                            "0x" + admin_key_2.hex(),
                        ],
                    },
                },
                "module_config": {
                    "telemetry": {
                        "deviceUpdateInterval": 321,
                        "environment_display_fahrenheit": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    target_local = localonly_pb2.LocalConfig()
    target_module = localonly_pb2.LocalModuleConfig()
    iface, target_node = _build_configure_interface(target_local, target_module)
    monkeypatch.setattr(
        main_module,
        "_post_configure_reconnect_and_verify",
        MagicMock(return_value=main_module._ConfigureReconnectResult.VERIFIED),
    )
    _run_main_configure_file(config_path, iface, monkeypatch)

    assert target_local.bluetooth.enabled is True
    assert target_local.bluetooth.mode == config_pb2.Config.BluetoothConfig.NO_PIN
    assert target_local.bluetooth.fixed_pin == 777777
    assert target_local.display.units == config_pb2.Config.DisplayConfig.IMPERIAL
    assert target_local.display.use_12h_clock is True
    assert target_local.display.screen_on_secs == 66
    assert target_local.power.ls_secs == 222
    assert target_local.power.wait_bluetooth_secs == 77
    assert target_local.power.min_wake_secs == 11
    assert target_local.power.sds_secs == SDS_DISABLED_SENTINEL
    assert target_local.security.private_key == private_key
    assert target_local.security.public_key == public_key
    assert list(target_local.security.admin_key) == [admin_key_1, admin_key_2]
    assert target_module.telemetry.device_update_interval == 321
    assert target_module.telemetry.environment_display_fahrenheit is True

    write_sections = [call.args[0] for call in target_node.writeConfig.call_args_list]
    for required in ("bluetooth", "display", "power", "security", "telemetry"):
        assert required in write_sections


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_applies_power_snake_case_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test --configure applies canonical snake_case power keys directly."""
    config_path = tmp_path / "power-snake-case.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "config": {
                    "power": {
                        "ls_secs": 222,
                        "wait_bluetooth_secs": 77,
                        "min_wake_secs": 11,
                        "sds_secs": SDS_DISABLED_SENTINEL,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    target_local = localonly_pb2.LocalConfig()
    iface, target_node = _build_configure_interface(
        target_local, localonly_pb2.LocalModuleConfig()
    )
    _run_main_configure_file(config_path, iface, monkeypatch)

    assert target_local.power.ls_secs == 222
    assert target_local.power.wait_bluetooth_secs == 77
    assert target_local.power.min_wake_secs == 11
    assert target_local.power.sds_secs == SDS_DISABLED_SENTINEL
    target_node.writeConfig.assert_called_once_with("power")
    target_node.commitSettingsTransaction.assert_called_once_with()
    assert target_node.method_calls.index(call.writeConfig("power")) < (
        target_node.method_calls.index(call.commitSettingsTransaction())
    )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize(
    "alias_key",
    ["use_12_hour", "use12Hour", "use12hClock", "use12HClock"],
)
def test_main_configure_accepts_display_use_12h_alias_spellings(
    alias_key: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test --configure accepts all known alias spellings for display.use_12h_clock."""
    config_path = tmp_path / f"display_alias_{alias_key}.yaml"
    config_path.write_text(
        yaml.safe_dump({"config": {"display": {alias_key: True}}}),
        encoding="utf-8",
    )
    target_local = localonly_pb2.LocalConfig()
    iface, _ = _build_configure_interface(
        target_local, localonly_pb2.LocalModuleConfig()
    )
    monkeypatch.setattr(
        main_module,
        "_post_configure_reconnect_and_verify",
        MagicMock(return_value=main_module._ConfigureReconnectResult.VERIFIED),
    )
    _run_main_configure_file(config_path, iface, monkeypatch)
    assert target_local.display.use_12h_clock is True


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_rejects_empty_config_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --configure rejects an empty config mapping."""
    config_path = tmp_path / "empty_config.yaml"
    config_path.write_text(yaml.safe_dump({"config": {}}), encoding="utf-8")
    iface, target_node = _build_configure_interface()

    with pytest.raises(SystemExit) as excinfo:
        _run_main_configure_file(config_path, iface, monkeypatch)

    _, err = capsys.readouterr()
    assert "config" in err
    assert excinfo.value.code == 1
    target_node.beginSettingsTransaction.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_rejects_empty_module_config_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --configure rejects an empty module_config mapping."""
    config_path = tmp_path / "empty_module_config.yaml"
    config_path.write_text(yaml.safe_dump({"module_config": {}}), encoding="utf-8")
    iface, target_node = _build_configure_interface()

    with pytest.raises(SystemExit) as excinfo:
        _run_main_configure_file(config_path, iface, monkeypatch)

    _, err = capsys.readouterr()
    assert "module_config" in err
    assert excinfo.value.code == 1
    target_node.beginSettingsTransaction.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_rejects_non_dict_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --configure rejects a non-dict config value."""
    config_path = tmp_path / "non_dict_config.yaml"
    config_path.write_text(yaml.safe_dump({"config": "invalid"}), encoding="utf-8")
    iface, target_node = _build_configure_interface()

    with pytest.raises(SystemExit) as excinfo:
        _run_main_configure_file(config_path, iface, monkeypatch)

    _, err = capsys.readouterr()
    assert "config" in err
    assert excinfo.value.code == 1
    target_node.beginSettingsTransaction.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_rejects_non_dict_module_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --configure rejects a non-dict module_config value."""
    config_path = tmp_path / "non_dict_module_config.yaml"
    config_path.write_text(yaml.safe_dump({"module_config": 42}), encoding="utf-8")
    iface, target_node = _build_configure_interface()

    with pytest.raises(SystemExit) as excinfo:
        _run_main_configure_file(config_path, iface, monkeypatch)

    _, err = capsys.readouterr()
    assert "module_config" in err
    assert excinfo.value.code == 1
    target_node.beginSettingsTransaction.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize(
    ("top_key", "section_name", "section_value"),
    [
        ("config", "lora", 1),
        ("module_config", "mqtt", 1),
    ],
)
def test_main_configure_rejects_invalid_subsection_payloads(
    top_key: str,
    section_name: str,
    section_value: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --configure rejects non-mapping subsection payloads."""
    config_path = tmp_path / f"invalid_{top_key}_{section_name}.yaml"
    config_path.write_text(
        yaml.safe_dump({top_key: {section_name: section_value}}),
        encoding="utf-8",
    )
    iface, target_node = _build_configure_interface()

    with pytest.raises(SystemExit) as excinfo:
        _run_main_configure_file(config_path, iface, monkeypatch)

    _, err = capsys.readouterr()
    assert f"{top_key}.{section_name}" in err
    assert "must be a mapping" in err
    assert excinfo.value.code == 1
    target_node.beginSettingsTransaction.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_rejects_malformed_yaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --configure exits cleanly on malformed YAML input."""
    config_path = tmp_path / "malformed_config.yaml"
    config_path.write_text("config:\n  lora: [\n", encoding="utf-8")
    iface, target_node = _build_configure_interface()

    with pytest.raises(SystemExit) as excinfo:
        _run_main_configure_file(config_path, iface, monkeypatch)

    _, err = capsys.readouterr()
    assert "Failed to parse YAML configuration" in err
    assert excinfo.value.code == 1
    target_node.beginSettingsTransaction.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_add_valid(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-add with valid channel name, and that channel name does not already exist."""
    sys.argv = ["", "--ch-add", "testing"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_channel = MagicMock(autospec=Channel)
    # TODO: figure out how to get it to print the channel name instead of MagicMock

    mocked_node = MagicMock(autospec=Node)
    # set it up so we do not already have a channel named this
    mocked_node.getChannelByName.return_value = False
    # set it up so we have free channels
    mocked_node.getDisabledChannel.return_value = mocked_channel

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Writing modified channels to device", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_add_invalid_name_too_long(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-add with invalid channel name, name too long."""
    sys.argv = ["", "--ch-add", "testingtestingtesting"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_channel = MagicMock(autospec=Channel)
    # TODO: figure out how to get it to print the channel name instead of MagicMock

    mocked_node = MagicMock(autospec=Node)
    # set it up so we do not already have a channel named this
    mocked_node.getChannelByName.return_value = False
    # set it up so we have free channels
    mocked_node.getDisabledChannel.return_value = mocked_channel

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        combined = out + err
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(
            r"Warning: Channel name must be shorter", combined, re.MULTILINE
        )
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_add_but_name_already_exists(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --ch-add with a channel name that already exists."""
    sys.argv = ["", "--ch-add", "testing"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)
    # set it up so we do not already have a channel named this
    mocked_node.getChannelByName.return_value = True

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        combined = out + err
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Warning: This node already has", combined, re.MULTILINE)
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_add_but_no_more_channels(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-add with but there are no more channels."""
    sys.argv = ["", "--ch-add", "testing"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)
    # set it up so we do not already have a channel named this
    mocked_node.getChannelByName.return_value = False
    # set it up so we have free channels
    mocked_node.getDisabledChannel.return_value = None

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        combined = out + err
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(
            r"Warning: No free channels were found", combined, re.MULTILINE
        )
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_del(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-del with valid secondary channel to be deleted."""
    sys.argv = ["", "--ch-del", "--ch-index", "1"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Deleting channel", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_del_no_ch_index_specified(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-del without a valid ch-index."""
    sys.argv = ["", "--ch-del"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        combined = out + err
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Warning: Need to specify", combined, re.MULTILINE)
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_del_primary_channel(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-del on ch-index=0."""
    sys.argv = ["", "--ch-del", "--ch-index", "0"]
    mt_config.args = sys.argv  # type: ignore[assignment]
    mt_config.channel_index = 1

    mocked_node = MagicMock(autospec=Node)

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        combined = out + err
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(
            r"Warning: Cannot delete primary channel", combined, re.MULTILINE
        )
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_enable_valid_secondary_channel(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --ch-enable with --ch-index."""
    sys.argv = ["", "--ch-enable", "--ch-index", "1"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Writing modified channels", out, re.MULTILINE)
        assert err == ""
        assert mt_config.channel_index == 1
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_disable_valid_secondary_channel(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --ch-disable with --ch-index."""
    sys.argv = ["", "--ch-disable", "--ch-index", "1"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Writing modified channels", out, re.MULTILINE)
        assert err == ""
        assert mt_config.channel_index == 1
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_enable_without_a_ch_index(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-enable without --ch-index."""
    sys.argv = ["", "--ch-enable"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        combined = out + err
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Warning: Need to specify", combined, re.MULTILINE)
        assert mt_config.channel_index is None
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_enable_primary_channel(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-enable with --ch-index = 0."""
    sys.argv = ["", "--ch-enable", "--ch-index", "0"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        combined = out + err
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(
            r"Warning: Cannot enable/disable PRIMARY", combined, re.MULTILINE
        )
        assert mt_config.channel_index == 0
        mo.assert_called()


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_ch_range_options(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test changing the various range options."""
#    range_options = ['--ch-vlongslow', '--ch-longslow', '--ch-longfast', '--ch-midslow',
#                     '--ch-midfast', '--ch-shortslow', '--ch-shortfast']
#    for range_option in range_options:
#        sys.argv = ['', f"{range_option}" ]
#        mt_config.args = sys.argv  # type: ignore[assignment]
#
#        mocked_node = MagicMock(autospec=Node)
#
#        iface = MagicMock(autospec=SerialInterface)
#        iface.getNode.return_value = mocked_node
#
#        with patch('meshtastic.serial_interface.SerialInterface', return_value=iface) as mo:
#            main()
#            out, err = capsys.readouterr()
#            assert re.search(r'Connected to radio', out, re.MULTILINE)
#            assert re.search(r'Writing modified channels', out, re.MULTILINE)
#            assert err == ''
#            mo.assert_called()


# PositionFlags:
# Misc info that might be helpful (this info will grow stale, just
# a snapshot of the values.) The radioconfig_pb2.PositionFlags.Name and bit values are:
# POS_UNDEFINED 0
# POS_ALTITUDE 1
# POS_ALT_MSL 2
# POS_GEO_SEP 4
# POS_DOP 8
# POS_HVDOP 16
# POS_BATTERY 32
# POS_SATINVIEW 64
# POS_SEQ_NOS 128
# POS_TIMESTAMP 256

# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_pos_fields_no_args(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test --pos-fields no args (which shows settings)"""
#    sys.argv = ['', '--pos-fields']
#    mt_config.args = sys.argv  # type: ignore[assignment]
#
#    pos_flags = MagicMock(autospec=meshtastic.radioconfig_pb2.PositionFlags)
#
#    with patch('meshtastic.serial_interface.SerialInterface') as mo:
#        mo().getNode().radioConfig.preferences.position_flags = 35
#        with patch('meshtastic.radioconfig_pb2.PositionFlags', return_value=pos_flags) as mrc:
#
#            mrc.values.return_value = [0, 1, 2, 4, 8, 16, 32, 64, 128, 256]
#            # Note: When you use side_effect and a list, each call will use a value from the front of the list then
#            # remove that value from the list. If there are three values in the list, we expect it to be called
#            # three times.
#            mrc.Name.side_effect = ['POS_ALTITUDE', 'POS_ALT_MSL', 'POS_BATTERY']
#
#            main()
#
#            mrc.Name.assert_called()
#            mrc.values.assert_called()
#            mo.assert_called()
#
#            out, err = capsys.readouterr()
#            assert re.search(r'Connected to radio', out, re.MULTILINE)
#            assert re.search(r'POS_ALTITUDE POS_ALT_MSL POS_BATTERY', out, re.MULTILINE)
#            assert err == ''


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_pos_fields_arg_of_zero(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test --pos-fields an arg of 0 (which shows list)"""
#    sys.argv = ['', '--pos-fields', '0']
#    mt_config.args = sys.argv  # type: ignore[assignment]
#
#    pos_flags = MagicMock(autospec=meshtastic.radioconfig_pb2.PositionFlags)
#
#    with patch('meshtastic.serial_interface.SerialInterface') as mo:
#        with patch('meshtastic.radioconfig_pb2.PositionFlags', return_value=pos_flags) as mrc:
#
#            def throw_value_error_exception(exc):
#                raise ValueError()
#            mrc.Value.side_effect = throw_value_error_exception
#            mrc.keys.return_value = [ 'POS_UNDEFINED', 'POS_ALTITUDE', 'POS_ALT_MSL',
#                                      'POS_GEO_SEP', 'POS_DOP', 'POS_HVDOP', 'POS_BATTERY',
#                                      'POS_SATINVIEW', 'POS_SEQ_NOS', 'POS_TIMESTAMP']
#
#            main()
#
#            mrc.Value.assert_called()
#            mrc.keys.assert_called()
#            mo.assert_called()
#
#            out, err = capsys.readouterr()
#            assert re.search(r'Connected to radio', out, re.MULTILINE)
#            assert re.search(r'ERROR: supported position fields are:', out, re.MULTILINE)
#            assert re.search(r"['POS_UNDEFINED', 'POS_ALTITUDE', 'POS_ALT_MSL', 'POS_GEO_SEP',"\
#                              "'POS_DOP', 'POS_HVDOP', 'POS_BATTERY', 'POS_SATINVIEW', 'POS_SEQ_NOS',"\
#                              "'POS_TIMESTAMP']", out, re.MULTILINE)
#            assert err == ''


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_pos_fields_valid_values(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test --pos-fields with valid values"""
#    sys.argv = ['', '--pos-fields', 'POS_GEO_SEP', 'POS_ALT_MSL']
#    mt_config.args = sys.argv  # type: ignore[assignment]
#
#    pos_flags = MagicMock(autospec=meshtastic.radioconfig_pb2.PositionFlags)
#
#    with patch('meshtastic.serial_interface.SerialInterface') as mo:
#        with patch('meshtastic.radioconfig_pb2.PositionFlags', return_value=pos_flags) as mrc:
#
#            mrc.Value.side_effect = [ 4, 2 ]
#
#            main()
#
#            mrc.Value.assert_called()
#            mo.assert_called()
#
#            out, err = capsys.readouterr()
#            assert re.search(r'Connected to radio', out, re.MULTILINE)
#            assert re.search(r'Setting position fields to 6', out, re.MULTILINE)
#            assert re.search(r'Set position_flags to 6', out, re.MULTILINE)
#            assert re.search(r'Writing modified preferences to device', out, re.MULTILINE)
#            assert err == ''


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_get_with_valid_values(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test --get with valid values (with string, number, boolean)"""
#    sys.argv = ['', '--get', 'ls_secs', '--get', 'wifi_ssid', '--get', 'fixed_position']
#    mt_config.args = sys.argv  # type: ignore[assignment]
#
#    with patch('meshtastic.serial_interface.SerialInterface') as mo:
#
#        mo().getNode().radioConfig.preferences.wifi_ssid = 'foo'
#        mo().getNode().radioConfig.preferences.ls_secs = 300
#        mo().getNode().radioConfig.preferences.fixed_position = False
#
#        main()
#
#        mo.assert_called()
#
#        out, err = capsys.readouterr()
#        assert re.search(r'Connected to radio', out, re.MULTILINE)
#        assert re.search(r'ls_secs: 300', out, re.MULTILINE)
#        assert re.search(r'wifi_ssid: foo', out, re.MULTILINE)
#        assert re.search(r'fixed_position: False', out, re.MULTILINE)
#        assert err == ''


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_get_with_valid_values_camel(capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture) -> None:
#    """Test --get with valid values (with string, number, boolean)"""
#    sys.argv = ["", "--get", "lsSecs", "--get", "wifiSsid", "--get", "fixedPosition"]
#    mt_config.args = sys.argv  # type: ignore[assignment]
#    mt_config.camel_case = True
#
#    with caplog.at_level(logging.DEBUG):
#        with patch("meshtastic.serial_interface.SerialInterface") as mo:
#            mo().getNode().radioConfig.preferences.wifi_ssid = "foo"
#            mo().getNode().radioConfig.preferences.ls_secs = 300
#            mo().getNode().radioConfig.preferences.fixed_position = False
#
#            main()
#
#            mo.assert_called()
#
#            out, err = capsys.readouterr()
#            assert re.search(r"Connected to radio", out, re.MULTILINE)
#            assert re.search(r"lsSecs: 300", out, re.MULTILINE)
#            assert re.search(r"wifiSsid: foo", out, re.MULTILINE)
#            assert re.search(r"fixedPosition: False", out, re.MULTILINE)
#            assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_get_with_invalid(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --get with invalid field."""
    sys.argv = ["", "--get", "foo"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_user_prefs = MagicMock()
    mocked_user_prefs.DESCRIPTOR.fields_by_name.get.return_value = None

    mocked_node = MagicMock(autospec=Node)
    mocked_node.localConfig = mocked_user_prefs
    mocked_node.moduleConfig = mocked_user_prefs

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"do not have an attribute foo", out, re.MULTILINE)
        assert re.search(r"Choices are...", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_onReceive_empty(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test onReceive with empty packet - should handle gracefully without error."""
    args = MagicMock()
    mt_config.args = args
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    # Need 'decoded' to be truthy so the code path reaches packet.get("to")
    packet: dict[str, Any] = {"decoded": {}}
    with caplog.at_level(logging.DEBUG):
        onReceive(packet, iface)
    assert re.search(r"in onReceive", caplog.text, re.MULTILINE)
    out, err = capsys.readouterr()
    # Should not print any warnings - packet.get("to") returns None gracefully
    assert out == ""
    assert err == ""


#    TODO: use this captured position app message (might want/need in the future)
#    packet = {
#            'to': 4294967295,
#            'decoded': {
#                'portnum': 'POSITION_APP',
#                'payload': "M69\306a"
#                },
#            'id': 334776976,
#            'hop_limit': 3
#            }


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_onReceive_with_sendtext(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test onReceive with sendtext.

    The entire point of this test is to make sure the interface.close() call
    is made in onReceive().

    """
    sys.argv = ["", "--sendtext", "hello"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    # Note: 'TEXT_MESSAGE_APP' value is 1
    packet = {
        "to": 4294967295,
        "decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": "hello"},
        "id": 334776977,
        "hop_limit": 3,
        "want_ack": True,
    }

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.myInfo.my_node_num = 4294967295

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        with caplog.at_level(logging.DEBUG):
            main()
            onReceive(packet, iface)
        assert re.search(r"in onReceive", caplog.text, re.MULTILINE)
        mo.assert_called()
        out, err = capsys.readouterr()
        assert re.search(r"Sending text message hello to", out, re.MULTILINE)
        assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_onReceive_with_text(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test onReceive with text."""
    args = MagicMock()
    args.sendtext.return_value = "foo"
    args.reply = True
    args.ch_index = None
    mt_config.args = args

    # Note: 'TEXT_MESSAGE_APP' value is 1
    # Note: Some of this is faked below.
    packet = {
        "to": 4294967295,
        "decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": "hello", "text": "faked"},
        "id": 334776977,
        "hop_limit": 3,
        "want_ack": True,
        "rxSnr": 6.0,
        "hopLimit": 3,
        "raw": "faked",
        "fromId": "!28b5465c",
        "toId": "^all",
    }

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.myInfo.my_node_num = 4294967295

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        with caplog.at_level(logging.DEBUG):
            onReceive(packet, iface)
        assert re.search(r"in onReceive", caplog.text, re.MULTILINE)
        out, err = capsys.readouterr()
        assert re.search(r"Sending reply", out, re.MULTILINE)
        assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_onReceive_reply_uses_rx_channel(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reply is sent on the same channel the message was received on."""
    args = MagicMock()
    args.sendtext.return_value = ""
    args.reply = True
    args.ch_index = None
    mt_config.args = args

    packet = {
        "to": 4294967295,
        "from": 999,
        "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hello"},
        "channel": 3,
        "rxSnr": 6.0,
        "hopLimit": 3,
    }

    iface = MagicMock(autospec=SerialInterface)
    iface.myInfo.my_node_num = 4294967295

    onReceive(packet, iface)

    iface.sendText.assert_called_once()
    call_kwargs = iface.sendText.call_args
    assert call_kwargs[1].get("channelIndex") == 3 or (
        len(call_kwargs[0]) > 1 and call_kwargs[0][1] == 3
    )
    out, err = capsys.readouterr()
    assert "Received channel 3" in out
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_onReceive_ch_index_filter_mismatch(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With --ch-index set, messages on a different channel are ignored."""
    args = MagicMock()
    args.sendtext.return_value = ""
    args.reply = True
    args.ch_index = 1
    mt_config.args = args

    packet = {
        "to": 4294967295,
        "from": 999,
        "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hello"},
        "channel": 5,
        "rxSnr": 6.0,
        "hopLimit": 3,
    }

    iface = MagicMock(autospec=SerialInterface)
    iface.myInfo.my_node_num = 4294967295

    onReceive(packet, iface)

    iface.sendText.assert_not_called()
    out, err = capsys.readouterr()
    assert "Ignored message on channel 5" in out
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_onReceive_own_packet_no_reply(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Messages from our own node number are not replied to (prevent loop)."""
    args = MagicMock()
    args.sendtext.return_value = ""
    args.reply = True
    args.ch_index = None
    mt_config.args = args

    my_node = 4294967295
    packet = {
        "to": 4294967295,
        "from": my_node,
        "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "my own msg"},
        "channel": 0,
        "rxSnr": 6.0,
        "hopLimit": 3,
    }

    iface = MagicMock(autospec=SerialInterface)
    iface.myInfo.my_node_num = my_node

    onReceive(packet, iface)

    iface.sendText.assert_not_called()
    out, err = capsys.readouterr()
    assert "Sending reply" not in out
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_onReceive_auto_reply_echo_no_reply(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Auto-reply echoes (starting with 'got msg ') are not replied to (prevent loop)."""
    args = MagicMock()
    args.sendtext.return_value = ""
    args.reply = True
    args.ch_index = None
    mt_config.args = args

    packet = {
        "to": 4294967295,
        "from": 999,
        "decoded": {
            "portnum": "TEXT_MESSAGE_APP",
            "text": "got msg 'hello' with rxSnr: 6.0 and hopLimit: 3",
        },
        "channel": 0,
        "rxSnr": 6.0,
        "hopLimit": 3,
    }

    iface = MagicMock(autospec=SerialInterface)
    iface.myInfo.my_node_num = 4294967295

    onReceive(packet, iface)

    iface.sendText.assert_not_called()
    out, err = capsys.readouterr()
    assert "Sending reply" not in out
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_onConnection(capsys: pytest.CaptureFixture[str]) -> None:
    """Test onConnection."""
    sys.argv = [""]
    mt_config.args = sys.argv  # type: ignore[assignment]
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)

    class TempTopic:
        """temp class for topic."""

        def getName(self) -> str:
            """Get a fake topic name.

            Returns
            -------
            str
                The fixed fake topic name `'foo'`.
            """
            return "foo"

    mytopic = TempTopic()
    onConnection(iface, mytopic)
    out, err = capsys.readouterr()
    assert re.search(r"Connection changed: foo", out, re.MULTILINE)
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_onConnection_with_non_topic(capsys: pytest.CaptureFixture[str]) -> None:
    """Test onConnection with non-topic objects."""
    sys.argv = [""]
    mt_config.args = sys.argv  # type: ignore[assignment]
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    onConnection(iface, topic="raw-topic")
    out, err = capsys.readouterr()
    assert re.search(r"Connection changed: raw-topic", out, re.MULTILINE)
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_export_config(capsys: pytest.CaptureFixture[str]) -> None:
    """Test export_config() function directly."""
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        mo.getLongName.return_value = "foo"
        mo.getShortName.return_value = "oof"
        mo.localNode.getURL.return_value = "bar"
        mo.getCannedMessage.return_value = "foo|bar"
        mo.getRingtone.return_value = "24:d=32,o=5"
        mo.getMyNodeInfo().get.return_value = {
            "latitudeI": 1100000000,
            "longitudeI": 1200000000,
            "altitude": 100,
            "batteryLevel": 34,
            "latitude": 110.0,
            "longitude": 120.0,
        }
        mo.localNode.radioConfig.preferences = """phone_timeout_secs: 900
ls_secs: 300
position_broadcast_smart: true
fixed_position: true
position_flags: 35"""
        out = export_config(mo)
    _, err = capsys.readouterr()

    # ensure we do not output this line
    assert not re.search(r"Connected to radio", out, re.MULTILINE)

    assert re.search(r"owner: foo", out, re.MULTILINE)
    assert re.search(r"owner_short: oof", out, re.MULTILINE)
    assert re.search(r"channel_url: bar", out, re.MULTILINE)
    assert re.search(r"location:", out, re.MULTILINE)
    assert re.search(r"lat: 110.0", out, re.MULTILINE)
    assert re.search(r"lon: 120.0", out, re.MULTILINE)
    assert re.search(r"alt: 100", out, re.MULTILINE)
    # TODO: rework above config to test the following
    # assert re.search(r"user_prefs:", out, re.MULTILINE)
    # assert re.search(r"phone_timeout_secs: 900", out, re.MULTILINE)
    # assert re.search(r"ls_secs: 300", out, re.MULTILINE)
    # assert re.search(r"position_broadcast_smart: 'true'", out, re.MULTILINE)
    # assert re.search(r"fixed_position: 'true'", out, re.MULTILINE)
    # assert re.search(r"position_flags: 35", out, re.MULTILINE)
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_export_config_omits_empty_optional_fields() -> None:
    """Test export_config omits optional top-level fields when values are empty/missing."""
    iface = _build_export_interface(
        localonly_pb2.LocalConfig(), localonly_pb2.LocalModuleConfig()
    )
    iface.getLongName.return_value = ""
    iface.getShortName.return_value = ""
    iface.localNode.getURL.return_value = ""
    iface.getCannedMessage.return_value = ""
    iface.getRingtone.return_value = ""
    iface.getMyNodeInfo.return_value = {}

    exported = yaml.safe_load(export_config(iface))

    assert "owner" not in exported
    assert "owner_short" not in exported
    assert "channel_url" not in exported
    assert "canned_messages" not in exported
    assert "ringtone" not in exported
    assert "location" not in exported


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_export_config_sets_missing_true_default_flags_false() -> None:
    """Test export_config explicitly writes known true-default flags as false when missing."""
    source_local = localonly_pb2.LocalConfig()
    source_module = localonly_pb2.LocalModuleConfig()
    source_local.display.units = config_pb2.Config.DisplayConfig.IMPERIAL
    source_module.telemetry.device_update_interval = 1

    exported = yaml.safe_load(
        export_config(_build_export_interface(source_local, source_module))
    )
    config = exported["config"]
    module_config = exported["module_config"]

    assert config["bluetooth"]["enabled"] is False
    assert config["lora"]["sx126xRxBoostedGain"] is False
    assert config["lora"]["txEnabled"] is False
    assert config["lora"]["usePreset"] is False
    assert config["position"]["positionBroadcastSmartEnabled"] is False
    assert config["security"]["serialEnabled"] is False
    assert module_config["mqtt"]["encryptionEnabled"] is False


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_export_config_configure_round_trip_security_keys(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ensure export->configure->export preserves security keys and structure."""
    source_local = localonly_pb2.LocalConfig()
    source_module = localonly_pb2.LocalModuleConfig()
    source_local.bluetooth.enabled = True
    source_local.security.serial_enabled = True
    source_local.security.private_key = b"\x01" * 32
    source_local.security.public_key = b"\x02" * 32
    source_local.security.admin_key.extend([b"\x03" * 32, b"\x04" * 32])
    source_module.mqtt.address = "mqtt.meshtastic.org"

    exported_yaml = export_config(_build_export_interface(source_local, source_module))
    exported = yaml.safe_load(exported_yaml)
    security = exported["config"]["security"]
    assert security["privateKey"].startswith("base64:")
    assert security["publicKey"].startswith("base64:")
    assert all(
        isinstance(item, str) and item.startswith("base64:")
        for item in security["adminKey"]
    )
    assert "base64:base64:" not in security["privateKey"]
    assert "base64:base64:" not in security["publicKey"]

    restored_local = localonly_pb2.LocalConfig()
    restored_module = localonly_pb2.LocalModuleConfig()
    for section, values in exported["config"].items():
        traverseConfig(section, values, restored_local)
    for section, values in exported["module_config"].items():
        traverseConfig(section, values, restored_module)

    assert restored_local.security.private_key == source_local.security.private_key
    assert restored_local.security.public_key == source_local.security.public_key
    assert list(restored_local.security.admin_key) == list(
        source_local.security.admin_key
    )

    exported_round_trip = yaml.safe_load(
        export_config(_build_export_interface(restored_local, restored_module))
    )
    assert exported_round_trip == exported
    _, err = capsys.readouterr()
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_export_config_and_configure_round_trip_nonstandard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Round-trip --export-config/--configure with nonstandard fully-configured settings."""
    source_local = localonly_pb2.LocalConfig()
    source_module = localonly_pb2.LocalModuleConfig()

    source_local.bluetooth.enabled = True
    source_local.bluetooth.mode = config_pb2.Config.BluetoothConfig.NO_PIN
    source_local.bluetooth.fixed_pin = 654321
    source_local.display.units = config_pb2.Config.DisplayConfig.IMPERIAL
    source_local.display.use_12h_clock = True
    source_local.display.screen_on_secs = 45
    source_local.power.ls_secs = 111
    source_local.security.serial_enabled = True
    source_local.security.private_key = b"\xaa" * 32
    source_local.security.public_key = b"\xbb" * 32
    source_local.security.admin_key.extend([b"\xcc" * 32, b"\xdd" * 32])

    source_module.telemetry.device_update_interval = 321
    source_module.telemetry.environment_display_fahrenheit = True
    source_module.remote_hardware.enabled = True

    export_iface = _build_export_interface(source_local, source_module)
    export_iface.__enter__ = MagicMock(return_value=export_iface)
    export_iface.__exit__ = MagicMock(return_value=None)
    export_iface.getCannedMessage.return_value = "Alpha|Bravo|Charlie"
    export_iface.getRingtone.return_value = "24:d=16,o=5,b=100:c"
    export_iface.localNode.getURL.return_value = "https://meshtastic.org/e/#CgYSAQABAA"
    export_iface.getMyNodeInfo.return_value = {
        "position": {"latitude": 12.345, "longitude": -98.765, "altitude": 432}
    }

    export_path = tmp_path / "roundtrip_config.yaml"
    sys.argv = ["", "--export-config", str(export_path)]
    mt_config.args = cast(Any, sys.argv)
    with patch(
        "meshtastic.serial_interface.SerialInterface", return_value=export_iface
    ):
        main()

    exported = yaml.safe_load(export_path.read_text(encoding="utf-8"))
    assert exported["owner"] == "Roundtrip Node"
    assert exported["owner_short"] == "RT"
    assert exported["channel_url"] == "https://meshtastic.org/e/#CgYSAQABAA"
    assert exported["canned_messages"] == "Alpha|Bravo|Charlie"
    assert exported["ringtone"] == "24:d=16,o=5,b=100:c"
    assert exported["location"] == {"lat": 12.345, "lon": -98.765, "alt": 432}
    bluetooth_cfg = exported["config"]["bluetooth"]
    display_cfg = exported["config"]["display"]
    power_cfg = exported["config"]["power"]
    telemetry_cfg = exported["module_config"]["telemetry"]
    assert bluetooth_cfg["mode"] == "NO_PIN"
    assert bluetooth_cfg.get("fixed_pin", bluetooth_cfg.get("fixedPin")) == 654321
    assert display_cfg["units"] == "IMPERIAL"
    assert display_cfg.get("use_12h_clock", display_cfg.get("use12hClock")) is True
    assert power_cfg.get("ls_secs", power_cfg.get("lsSecs")) == 111
    assert exported["config"]["security"]["privateKey"].startswith("base64:")
    assert exported["config"]["security"]["publicKey"].startswith("base64:")
    assert all(
        isinstance(v, str) and v.startswith("base64:")
        for v in exported["config"]["security"]["adminKey"]
    )
    assert (
        telemetry_cfg.get(
            "device_update_interval", telemetry_cfg.get("deviceUpdateInterval")
        )
        == 321
    )
    assert (
        telemetry_cfg.get(
            "environment_display_fahrenheit",
            telemetry_cfg.get("environmentDisplayFahrenheit"),
        )
        is True
    )
    assert exported["module_config"]["remote_hardware"]["enabled"] is True

    target_local = localonly_pb2.LocalConfig()
    target_module = localonly_pb2.LocalModuleConfig()
    device_local = localonly_pb2.LocalConfig()
    device_module = localonly_pb2.LocalModuleConfig()
    target_node = MagicMock()
    target_node.localConfig = target_local
    target_node.moduleConfig = target_module
    target_node.beginSettingsTransaction = MagicMock()
    target_node.commitSettingsTransaction = MagicMock()
    target_node.setOwner = MagicMock()
    target_node.setURL = MagicMock()
    target_node.set_canned_message = MagicMock()
    target_node.set_ringtone = MagicMock()
    target_node.channels = []
    target_node.partialChannels = []
    target_node.requestChannels = MagicMock()

    def _write_config_side_effect(config_name: str) -> None:
        local_field = target_local.DESCRIPTOR.fields_by_name.get(config_name)
        if local_field is not None:
            device_local.ClearField(config_name)  # type: ignore[arg-type]
            if target_local.HasField(config_name):  # type: ignore[arg-type]
                getattr(device_local, config_name).CopyFrom(
                    getattr(target_local, config_name)
                )
            return
        module_field = target_module.DESCRIPTOR.fields_by_name.get(config_name)
        if module_field is not None:
            device_module.ClearField(config_name)  # type: ignore[arg-type]
            if target_module.HasField(config_name):  # type: ignore[arg-type]
                getattr(device_module, config_name).CopyFrom(
                    getattr(target_module, config_name)
                )

    target_node.writeConfig = MagicMock(side_effect=_write_config_side_effect)

    def _request_config_side_effect(config_type: object, *_args: object) -> None:
        field_name = getattr(config_type, "name", None)
        containing_type = getattr(config_type, "containing_type", None)
        containing_name = getattr(containing_type, "name", None)
        if not isinstance(field_name, str):
            return
        if containing_name == "LocalConfig":
            target_local.ClearField(field_name)  # type: ignore[arg-type]
            if device_local.HasField(field_name):  # type: ignore[arg-type]
                getattr(target_local, field_name).CopyFrom(
                    getattr(device_local, field_name)
                )
            return
        if containing_name == "LocalModuleConfig":
            target_module.ClearField(field_name)  # type: ignore[arg-type]
            if device_module.HasField(field_name):  # type: ignore[arg-type]
                getattr(target_module, field_name).CopyFrom(
                    getattr(device_module, field_name)
                )

    target_node.requestConfig = MagicMock(side_effect=_request_config_side_effect)
    target_node.getURL = MagicMock(return_value="https://meshtastic.org/e/#CgYSAQABAA")
    target_node.setFixedPosition = MagicMock()

    configure_iface = MagicMock(autospec=SerialInterface)
    configure_iface.__enter__ = MagicMock(return_value=configure_iface)
    configure_iface.__exit__ = MagicMock(return_value=None)
    configure_iface.getNode.return_value = target_node
    configure_iface.localNode = target_node

    monkeypatch.setattr("time.sleep", lambda _: None)
    _patch_fast_monotonic(monkeypatch)
    monkeypatch.setattr(
        main_module,
        "_post_configure_reconnect_and_verify",
        MagicMock(return_value=main_module._ConfigureReconnectResult.VERIFIED),
    )
    monkeypatch.setattr(
        "meshtastic.__main__._post_seturl_stability_check",
        lambda *a, **k: True,
    )
    configure_iface.waitForConfig = MagicMock()
    sys.argv = ["", "--configure", str(export_path)]
    mt_config.args = cast(Any, sys.argv)
    with patch(
        "meshtastic.serial_interface.SerialInterface", return_value=configure_iface
    ):
        main()

    target_node.beginSettingsTransaction.assert_called_once()
    target_node.commitSettingsTransaction.assert_called_once()
    assert target_node.setOwner.call_count == 2
    target_node.setURL.assert_called_once_with("https://meshtastic.org/e/#CgYSAQABAA")
    target_node.set_canned_message.assert_called_once_with("Alpha|Bravo|Charlie")
    target_node.set_ringtone.assert_called_once_with("24:d=16,o=5,b=100:c")
    target_node.setFixedPosition.assert_called_once_with(12.345, -98.765, 432)

    assert target_local.bluetooth.enabled is True
    assert target_local.bluetooth.mode == config_pb2.Config.BluetoothConfig.NO_PIN
    assert target_local.bluetooth.fixed_pin == 654321
    assert target_local.display.units == config_pb2.Config.DisplayConfig.IMPERIAL
    assert target_local.display.use_12h_clock is True
    assert target_local.display.screen_on_secs == 45
    assert target_local.power.ls_secs == 111
    assert target_local.security.serial_enabled is True
    assert target_local.security.private_key == source_local.security.private_key
    assert target_local.security.public_key == source_local.security.public_key
    assert list(target_local.security.admin_key) == list(
        source_local.security.admin_key
    )

    assert target_module.telemetry.device_update_interval == 321
    assert target_module.telemetry.environment_display_fahrenheit is True
    assert target_module.remote_hardware.enabled is True

    write_sections = [c.args[0] for c in target_node.writeConfig.call_args_list]
    for required in (
        "bluetooth",
        "display",
        "power",
        "security",
        "telemetry",
        "remote_hardware",
    ):
        assert required in write_sections

    out, err = capsys.readouterr()
    assert re.search(r"Exported configuration to", out, re.MULTILINE)
    assert re.search(r"Configuration transaction committed", out, re.MULTILINE)
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_export_config_round_trip_with_camel_case_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test export->traverse round trip when mt_config.camel_case is enabled."""
    source_local = localonly_pb2.LocalConfig()
    source_module = localonly_pb2.LocalModuleConfig()
    source_local.display.use_12h_clock = True
    source_local.power.ls_secs = 123
    source_local.security.serial_enabled = True
    source_module.telemetry.device_update_interval = 77

    monkeypatch.setattr(mt_config, "camel_case", True)
    exported = yaml.safe_load(
        export_config(_build_export_interface(source_local, source_module))
    )

    assert "channelUrl" in exported
    assert exported["config"]["display"]["use12hClock"] is True
    assert exported["config"]["power"]["lsSecs"] == 123
    assert exported["config"]["security"]["serialEnabled"] is True
    assert exported["module_config"]["telemetry"]["deviceUpdateInterval"] == 77

    restored_local = localonly_pb2.LocalConfig()
    restored_module = localonly_pb2.LocalModuleConfig()
    for section, values in exported["config"].items():
        assert traverseConfig(section, values, restored_local)
    for section, values in exported["module_config"].items():
        assert traverseConfig(section, values, restored_module)

    assert restored_local.display.use_12h_clock is True
    assert restored_local.power.ls_secs == 123
    assert restored_local.security.serial_enabled is True
    assert restored_module.telemetry.device_update_interval == 77


@pytest.mark.unit
def test_prefix_base64_key_skips_existing_prefixes() -> None:
    """Ensure _prefix_base64_key does not double-prefix already-normalized values."""
    security = {
        "privateKey": "base64:abc123==",
        "adminKey": ["base64:def456==", "ghi789==", 7],
    }
    normalized_key_map = {
        "privateKey": "privateKey",
        "adminKey": "adminKey",
    }
    _prefix_base64_key(security, normalized_key_map, "privateKey")
    _prefix_base64_key(security, normalized_key_map, "adminKey")

    assert security["privateKey"] == "base64:abc123=="
    assert security["adminKey"] == ["base64:def456==", "base64:ghi789==", 7]


# TODO
# recursion depth exceeded error
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_export_config_use_camel(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test export_config() function directly"""
#    mt_config.camel_case = True
#    iface = MagicMock(autospec=SerialInterface)
#    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
#        mo.getLongName.return_value = "foo"
#        mo.localNode.getURL.return_value = "bar"
#        mo.getMyNodeInfo().get.return_value = {
#            "latitudeI": 1100000000,
#            "longitudeI": 1200000000,
#            "altitude": 100,
#            "batteryLevel": 34,
#            "latitude": 110.0,
#            "longitude": 120.0,
#        }
#        mo.localNode.radioConfig.preferences = """phone_timeout_secs: 900
# ls_secs: 300
# position_broadcast_smart: true
# fixed_position: true
# position_flags: 35"""
#        export_config(mo)
#    out, err = capsys.readouterr()
#
#    # ensure we do not output this line
#    assert not re.search(r"Connected to radio", out, re.MULTILINE)
#
#    assert re.search(r"owner: foo", out, re.MULTILINE)
#    assert re.search(r"channelUrl: bar", out, re.MULTILINE)
#    assert re.search(r"location:", out, re.MULTILINE)
#    assert re.search(r"lat: 110.0", out, re.MULTILINE)
#    assert re.search(r"lon: 120.0", out, re.MULTILINE)
#    assert re.search(r"alt: 100", out, re.MULTILINE)
#    assert re.search(r"userPrefs:", out, re.MULTILINE)
#    assert re.search(r"phoneTimeoutSecs: 900", out, re.MULTILINE)
#    assert re.search(r"lsSecs: 300", out, re.MULTILINE)
#    # TODO: should True be capitalized here?
#    assert re.search(r"positionBroadcastSmart: 'True'", out, re.MULTILINE)
#    assert re.search(r"fixedPosition: 'True'", out, re.MULTILINE)
#    assert re.search(r"positionFlags: 35", out, re.MULTILINE)
#    assert err == ""


# TODO
# maximum recursion depth error
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_export_config_called_from_main(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test --export-config"""
#    sys.argv = ["", "--export-config"]
#    mt_config.args = sys.argv  # type: ignore[assignment]
#
#    iface = MagicMock(autospec=SerialInterface)
#    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
#        main()
#        out, err = capsys.readouterr()
#        assert not re.search(r"Connected to radio", out, re.MULTILINE)
#        assert re.search(r"# start of Meshtastic configure yaml", out, re.MULTILINE)
#        assert err == ""
#        mo.assert_called()


@pytest.mark.unit
def test_set_missing_flags_false() -> None:
    """Test _set_missing_flags_false() function."""
    config = {"bluetooth": {"enabled": True}, "lora": {"txEnabled": True}}

    false_defaults: set[tuple[str, ...]] = {
        ("bluetooth", "enabled"),
        ("lora", "sx126xRxBoostedGain"),
        ("lora", "txEnabled"),
        ("lora", "usePreset"),
        ("position", "positionBroadcastSmartEnabled"),
        ("security", "serialEnabled"),
        ("mqtt", "encryptionEnabled"),
    }

    _set_missing_flags_false(config, false_defaults)

    # Preserved
    assert config["bluetooth"]["enabled"] is True
    assert config["lora"]["txEnabled"] is True

    # Added
    assert config["lora"]["usePreset"] is False
    assert config["lora"]["sx126xRxBoostedGain"] is False
    assert config["position"]["positionBroadcastSmartEnabled"] is False
    assert config["security"]["serialEnabled"] is False
    assert config["mqtt"]["encryptionEnabled"] is False


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_gpio_rd_no_gpio_channel(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --gpio_rd with no named gpio channel."""
    sys.argv = ["", "--gpio-rd", "0x10", "--dest", "!foo"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.localNode.getChannelByName.return_value = None
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        # Error messages go to stderr, stdout contains "Connected to radio"
        assert re.search(r"No channel named 'gpio'", err)
        assert "Connected to radio" in out


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_configure_missing_file_exits_cleanly(tmp_path: Path) -> None:
    """Unreadable configure paths should use the CLI error path without traceback."""
    iface, _target_node = _build_configure_interface()
    args = SimpleNamespace(
        configure=[str(tmp_path / "missing.yaml")],
        dest=MAIN_LOCAL_ADDR,
    )

    with pytest.raises(SystemExit) as exc_info:
        main_module._handle_configure_command(iface, args, {})

    assert exc_info.value.code == 1


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize("owner_key", ["owner", "owner_short", "ownerShort"])
def test_configure_rejects_null_owner_values_before_mutation(
    owner_key: str,
    tmp_path: Path,
) -> None:
    """YAML null owner values must never be written as the literal string 'None'."""
    config_path = tmp_path / f"null-{owner_key}.yaml"
    config_path.write_text(yaml.safe_dump({owner_key: None}), encoding="utf-8")
    iface, target_node = _build_configure_interface()
    args = SimpleNamespace(configure=[str(config_path)], dest=MAIN_LOCAL_ADDR)

    with pytest.raises(SystemExit):
        main_module._handle_configure_command(iface, args, {})

    target_node.setOwner.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize(
    ("location", "expected_fragment"),
    [
        ({"lat": 91, "lon": 0}, "location.lat"),
        ({"lat": 0, "lon": -181}, "location.lon"),
    ],
)
def test_configure_rejects_out_of_range_location_before_mutation(
    location: dict[str, int],
    expected_fragment: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Configure location must enforce geographic latitude/longitude bounds."""
    config_path = tmp_path / "location.yaml"
    config_path.write_text(yaml.safe_dump({"location": location}), encoding="utf-8")
    iface, target_node = _build_configure_interface()
    args = SimpleNamespace(configure=[str(config_path)], dest=MAIN_LOCAL_ADDR)

    with pytest.raises(SystemExit):
        main_module._handle_configure_command(iface, args, {})

    _out, err = capsys.readouterr()
    assert expected_fragment in err
    target_node.setFixedPosition.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_remote_channel_url_with_config_rejected_before_direct_write(
    tmp_path: Path,
) -> None:
    """Remote mixed setURL/config operations must fail before changing the channel URL."""
    config_path = tmp_path / "remote-mixed.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "channel_url": "https://meshtastic.org/e/#remote-key",
                "config": {"bluetooth": {"enabled": True}},
            }
        ),
        encoding="utf-8",
    )
    iface, target_node = _build_configure_interface()
    args = SimpleNamespace(configure=[str(config_path)], dest="!87654321")

    with pytest.raises(SystemExit):
        main_module._handle_configure_command(iface, args, {})

    iface.getNode.assert_not_called()
    target_node.setURL.assert_not_called()
    target_node.setOwner.assert_not_called()
    target_node.beginSettingsTransaction.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_configure_write_failure_best_effort_closes_settings_transaction(
    tmp_path: Path,
) -> None:
    """Ensure write failure does not silently leave the edit transaction open."""
    config_path = tmp_path / "write-failure.yaml"
    config_path.write_text(
        yaml.safe_dump({"config": {"bluetooth": {"enabled": True}}}),
        encoding="utf-8",
    )
    iface, target_node = _build_configure_interface()
    target_node.writeConfig.side_effect = RuntimeError("write failed")
    args = SimpleNamespace(configure=[str(config_path)], dest=MAIN_LOCAL_ADDR)

    with pytest.raises(RuntimeError, match="write failed"):
        main_module._handle_configure_command(iface, args, {})

    target_node.beginSettingsTransaction.assert_called_once_with()
    target_node.commitSettingsTransaction.assert_called_once_with()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_configure_commit_failure_is_not_retried(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An uncertain commit failure must propagate without sending a second commit."""
    config_path = tmp_path / "commit-failure.yaml"
    config_path.write_text(
        yaml.safe_dump({"config": {"bluetooth": {"enabled": True}}}),
        encoding="utf-8",
    )
    iface, target_node = _build_configure_interface()
    target_node.commitSettingsTransaction.side_effect = RuntimeError("commit failed")
    args = SimpleNamespace(configure=[str(config_path)], dest=MAIN_LOCAL_ADDR)

    with pytest.raises(RuntimeError, match="commit failed"):
        main_module._handle_configure_command(iface, args, {})

    target_node.commitSettingsTransaction.assert_called_once_with()
    assert "transaction state is unknown" in capsys.readouterr().out


@pytest.mark.unit
def test_legacy_mapping_validation_wrapper_delegates_to_renamed_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retained __main__ compatibility seam must call the renamed validator."""
    sections: dict[str, dict[str, Any]] = {"audio": {}}
    delegate = MagicMock(return_value=sections)
    monkeypatch.setattr(
        "meshtastic.cli.configure_actions._validate_mapping_sections", delegate
    )

    result = main_module._validate_non_empty_mapping_sections(
        top_level_key="module_config", section_mapping=sections
    )

    delegate.assert_called_once()
    assert delegate.call_args.kwargs["top_level_key"] == "module_config"
    assert delegate.call_args.kwargs["section_mapping"] == sections
    assert result == sections


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_configure_reconnect_verifies_normalized_channel_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-reconnect verification should compare the stripped URL that was written."""
    channel_url = "https://meshtastic.org/e/#normalized"
    config_path = tmp_path / "normalized-url.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "channel_url": f"  {channel_url}  ",
                "config": {"bluetooth": {"enabled": True}},
            }
        ),
        encoding="utf-8",
    )
    iface, target_node = _build_configure_interface()
    args = SimpleNamespace(configure=[str(config_path)], dest=MAIN_LOCAL_ADDR)
    reconnect = MagicMock(return_value=main_module._ConfigureReconnectResult.VERIFIED)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        main_module, "_post_seturl_stability_check", lambda *_a, **_k: True
    )
    monkeypatch.setattr(main_module, "_post_configure_reconnect_and_verify", reconnect)

    main_module._handle_configure_command(iface, args, {})

    target_node.setURL.assert_called_once_with(channel_url)
    assert reconnect.call_args.kwargs["verify_channel_url"] == channel_url


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_configure_prevalidates_all_direct_values_before_any_mutation(
    tmp_path: Path,
) -> None:
    """Reject malformed direct values before applying any direct writes."""
    config_file = tmp_path / "invalid-direct-location.yaml"
    config_file.write_text(
        "owner: valid-owner\nlocation:\n  lat: 91\n  lon: 1\n",
        encoding="utf-8",
    )
    iface, node = _build_configure_interface()
    args = SimpleNamespace(configure=[str(config_file)], dest=MAIN_LOCAL_ADDR)

    with pytest.raises(SystemExit):
        main_module._handle_configure_command(iface, args, {})

    node.setOwner.assert_not_called()
    node.setFixedPosition.assert_not_called()
    node.setURL.assert_not_called()
    node.beginSettingsTransaction.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize(
    ("location", "expected_fragment"),
    [
        ({"lat": True, "lon": 0}, "location.lat must be a number"),
        ({"lat": 0, "lon": False}, "location.lon must be a number"),
        ({"lat": 0, "lon": 0, "alt": True}, "location.alt must be an integer"),
        (
            {"lat": 0, "lon": 0, "alt": 1 << 31},
            "location.alt must fit the signed 32-bit position field",
        ),
        (
            {"lat": 0, "lon": 0, "alt": -(1 << 31) - 1},
            "location.alt must fit the signed 32-bit position field",
        ),
    ],
)
def test_configure_rejects_invalid_location_wire_values_before_mutation(
    location: dict[str, object],
    expected_fragment: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Location values must fit their semantic and protobuf wire domains."""
    config_path = tmp_path / "invalid-location-wire.yaml"
    config_path.write_text(yaml.safe_dump({"location": location}), encoding="utf-8")
    iface, target_node = _build_configure_interface()
    args = SimpleNamespace(configure=[str(config_path)], dest=MAIN_LOCAL_ADDR)

    with pytest.raises(SystemExit):
        main_module._handle_configure_command(iface, args, {})

    _out, err = capsys.readouterr()
    assert expected_fragment in err
    target_node.setFixedPosition.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize(
    ("lat", "lon"),
    [(90.0, 180.0), (-90.0, -180.0)],
)
def test_configure_accepts_exact_location_boundaries(
    lat: float,
    lon: float,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inclusive geographic boundaries must remain valid configure inputs."""
    config_path = tmp_path / "boundary-location.yaml"
    config_path.write_text(
        yaml.safe_dump({"location": {"lat": lat, "lon": lon}}), encoding="utf-8"
    )
    monkeypatch.setattr(
        "meshtastic.cli.configure_actions.time.sleep", lambda _seconds: None
    )
    iface, target_node = _build_configure_interface()
    args = SimpleNamespace(configure=[str(config_path)], dest=MAIN_LOCAL_ADDR)

    main_module._handle_configure_command(iface, args, {})

    target_node.setFixedPosition.assert_called_once_with(lat, lon, 0)
