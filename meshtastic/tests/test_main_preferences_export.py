"""Meshtastic unit tests for __main__.py."""

# pylint: disable=W0613,R0917

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, call, patch

import pytest
import yaml
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from google.protobuf.json_format import MessageToDict

import meshtastic.__main__ as main_module
import time
from meshtastic import mt_config
from meshtastic.__main__ import (
    _prefix_base64_bytes_fields,
    export_config,
    main,
    setPref,
    traverseConfig,
)

# from ..radioconfig_pb2 import UserPreferences
# import meshtastic.config_pb2
from ..protobuf import config_pb2, localonly_pb2
from ._main_legacy_support import (
    _build_configure_interface,
    _build_export_interface,
    _build_nested_bytes_test_message,
    _get_config_field,
    _run_main_configure_file,
)

# from ..ble_interface import BLEInterface


# from ..remote_hardware import onGPIOreceive
# from ..config_pb2 import Config


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
def test_flatten_leaf_paths_flat_dict() -> None:
    """_flatten_leaf_paths handles a flat dict."""
    result = main_module._flatten_leaf_paths(
        "lora", {"hop_limit": 3, "tx_enabled": True}
    )
    assert sorted(result) == ["lora.hop_limit", "lora.tx_enabled"]


@pytest.mark.unit
def test_flatten_leaf_paths_nested_dict() -> None:
    """_flatten_leaf_paths recursively flattens nested dicts."""
    result = main_module._flatten_leaf_paths(
        "display", {"screen_on_secs": 60, "nested": {"foo": 1, "bar": 2}}
    )
    assert sorted(result) == [
        "display.nested.bar",
        "display.nested.foo",
        "display.screen_on_secs",
    ]


@pytest.mark.unit
def test_flatten_leaf_paths_empty_nested_dict() -> None:
    """_flatten_leaf_paths treats an empty nested dict as a leaf."""
    result = main_module._flatten_leaf_paths("lora", {"hop_limit": 3, "empty": {}})
    assert sorted(result) == ["lora.empty", "lora.hop_limit"]


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize(
    "field,value,expected,expected_output_substring",
    [
        ("position.position_flags", 513, 513, "513"),
        ("position.position_flags", "513", 513, "513"),
        ("position.position_flags", "0x201", 513, "0x201"),
        ("position.position_flags", "0b1000000001", 513, "0b1000000001"),
        ("network.enabled_protocols", "UDP_BROADCAST", 1, "UDP_BROADCAST"),
        ("network.enabled_protocols", "0x1", 1, "0x1"),
        ("position.position_flags", "ALTITUDE,SPEED", 513, "ALTITUDE,SPEED"),
        ("position.position_flags", "ALTITUDE, SPEED", 513, "ALTITUDE, SPEED"),
        ("network.enabled_protocols", "0", 0, "0"),
        ("network.enabled_protocols", "0x0", 0, "0x0"),
        ("network.enabled_protocols", "0b0", 0, "0b0"),
        ("position.position_flags", "ALTITUDE, , SPEED", 513, "ALTITUDE, , SPEED"),
        (
            "network.enabled_protocols",
            "NO_BROADCAST,UDP_BROADCAST",
            1,
            "NO_BROADCAST,UDP_BROADCAST",
        ),
    ],
    ids=[
        "raw_integer",
        "decimal_string",
        "hex_string",
        "binary_string",
        "single_flag",
        "network_hex_string",
        "comma_separated_flags",
        "comma_separated_flags_with_spaces",
        "zero_decimal_string",
        "zero_hex_string",
        "zero_binary_string",
        "whitespace_and_empty_entries",
        "zero_valued_member_is_noop",
    ],
)
def test_main_setPref_bitfield(
    field: str,
    value: Any,
    expected: int,
    expected_output_substring: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """setPref() accepts bitfield flag names and numeric masks."""
    config = config_pb2.Config()
    assert setPref(config, field, value) is True
    assert _get_config_field(config, field) == expected
    out, _ = capsys.readouterr()
    assert f"Set {field} to {expected_output_substring}" in out


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_setPref_bitfield_invalid_name(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """setPref() rejects unknown bitfield flag names."""
    config = config_pb2.Config()
    assert setPref(config, "network.enabled_protocols", "TCP") is False
    out, _ = capsys.readouterr()
    assert "Unknown flag 'TCP'" in out
    assert "NO_BROADCAST" in out
    assert "UDP_BROADCAST" in out


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize("value", ["0xZZ", "0o10"], ids=["invalid_hex", "octal"])
def test_main_setPref_bitfield_invalid_numeric_string(
    value: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """setPref() rejects malformed numeric-looking bitfield values cleanly."""
    config = config_pb2.Config()
    assert setPref(config, "position.position_flags", value) is False
    assert config.position.position_flags == 0
    out, _ = capsys.readouterr()
    assert f"Invalid numeric bitfield value '{value}'" in out
    assert "decimal" in out
    assert "0x" in out
    assert "0b" in out


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ota_update_file_not_found(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--ota-update with a missing firmware file exits early before connecting."""
    monkeypatch.setattr(
        sys,
        "argv",
        ["", "--host", "localhost", "--ota-update", "/nonexistent/firmware.bin"],
    )
    mt_config.args = sys.argv  # type: ignore[assignment]

    with (
        patch("meshtastic.tcp_interface.TCPInterface") as tcp_cls,
        patch("meshtastic.serial_interface.SerialInterface") as serial_cls,
        pytest.raises(SystemExit) as excinfo,
    ):
        main()

    assert excinfo.value.code == 1
    _, err = capsys.readouterr()
    assert "OTA firmware file not found" in err
    assert "/nonexistent/firmware.bin" in err
    # Verify no transport was constructed before the file check fired
    tcp_cls.assert_not_called()
    serial_cls.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_setpref_updates_deeply_nested_mesh_beacon_channel_settings() -> None:
    """Apply valid 2.8 fields nested more than one protobuf message deep."""
    module_config = localonly_pb2.LocalModuleConfig()

    assert setPref(
        module_config,
        "mesh_beacon.broadcast_offer_channel.module_settings.position_precision",
        12,
    )

    assert (
        module_config.mesh_beacon.broadcast_offer_channel.module_settings.position_precision
        == 12
    )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_traverse_config_skips_unknown_deep_intermediate_field(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Treat an unknown nested object as a skipped field instead of crashing."""
    module_config = localonly_pb2.LocalModuleConfig()

    assert traverseConfig(
        "mesh_beacon",
        {"broadcast_offer_channel": {"unknown_group": {"value": 1}}},
        module_config,
    )

    assert "unknown_group.value" in caplog.text


@pytest.mark.unit
def test_walk_config_path_handles_empty_path() -> None:
    config = localonly_pb2.LocalModuleConfig()

    parent, descriptor = main_module._walk_config_path(config, [])

    assert parent is config
    assert descriptor is None


@pytest.mark.unit
def test_walk_config_path_normalizes_first_component() -> None:
    config = localonly_pb2.LocalModuleConfig()

    parent, descriptor = main_module._walk_config_path(
        config, ["meshBeacon", "broadcastOfferChannel", "psk"]
    )

    assert parent is config.mesh_beacon
    assert descriptor is not None
    assert descriptor.name == "broadcast_offer_channel"


@pytest.mark.unit
def test_walk_config_path_stops_at_scalar_intermediate() -> None:
    config = localonly_pb2.LocalModuleConfig()

    parent, descriptor = main_module._walk_config_path(
        config, ["mqtt", "enabled", "value"]
    )

    assert parent is config.mqtt
    assert descriptor is None


@pytest.mark.unit
def test_walk_config_path_stops_after_unknown_intermediate() -> None:
    config = localonly_pb2.LocalModuleConfig()

    parent, descriptor = main_module._walk_config_path(
        config,
        [
            "mesh_beacon",
            "broadcast_offer_channel",
            "unknown_group",
            "child",
            "value",
        ],
    )

    assert parent is config.mesh_beacon.broadcast_offer_channel
    assert descriptor is None


@pytest.mark.unit
def test_resolve_pref_accepts_nested_message_field() -> None:
    config = localonly_pb2.LocalModuleConfig()

    assert main_module._resolve_pref(
        config,
        "meshBeacon.broadcastOfferChannel.psk",
    )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_set_pref_redacts_network_mqtt_and_pin_credentials(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Configure progress output must not disclose local network or broker credentials."""
    local_config = localonly_pb2.LocalConfig()
    module_config = localonly_pb2.LocalModuleConfig()

    assert setPref(local_config, "network.wifi_ssid", "Private LAN") is True
    assert setPref(local_config, "network.wifi_psk", "distinctive-passphrase") is True
    assert setPref(local_config, "bluetooth.fixed_pin", "123456") is True
    assert setPref(module_config, "mqtt.username", "private-user") is True
    assert setPref(module_config, "mqtt.password", "private-password") is True

    out, err = capsys.readouterr()
    assert out.count("<redacted>") == 5
    for secret in (
        "Private LAN",
        "distinctive-passphrase",
        "123456",
        "private-user",
        "private-password",
    ):
        assert secret not in out
    assert local_config.network.wifi_ssid == "Private LAN"
    assert local_config.network.wifi_psk == "distinctive-passphrase"
    assert local_config.bluetooth.fixed_pin == 123456
    assert module_config.mqtt.username == "private-user"
    assert module_config.mqtt.password == "private-password"
    assert err == ""


@pytest.mark.unit
def test_secret_redaction_is_scoped_to_sensitive_preference_paths() -> None:
    assert main_module._redact_pref_value("mqtt.username", "alice") == "<redacted>"
    assert main_module._redact_pref_value("unrelated.username", "alice") == "alice"
    assert main_module._redact_pref_value("unrelated.password", "visible") == "visible"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "message"),
    [(123, "must be a string"), ("   ", "must not be blank")],
)
def test_apply_configure_channel_url_rejects_invalid_values(
    value: object,
    message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target_node = MagicMock()

    with pytest.raises(SystemExit) as excinfo:
        main_module._apply_configure_channel_url(
            target_node,
            value,
            config_key="channel_url",
        )

    assert excinfo.value.code == 1
    assert message in capsys.readouterr().err
    target_node.setURL.assert_not_called()


@pytest.mark.unit
def test_apply_configure_channel_url_skips_matching_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target_node = MagicMock()
    monkeypatch.setattr(
        main_module,
        "_channel_url_matches_current_device_state",
        lambda _node, _url: True,
    )

    applied = main_module._apply_configure_channel_url(
        target_node,
        " https://meshtastic.org/e/#CgYSAQABAA ",
        config_key="channelUrl",
    )

    assert applied is False
    target_node.setURL.assert_not_called()
    assert "already matches" in capsys.readouterr().out


@pytest.mark.unit
def test_apply_configure_channel_url_redacts_and_applies(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target_node = MagicMock()
    sleep = MagicMock()
    monkeypatch.setattr(
        main_module,
        "_channel_url_matches_current_device_state",
        lambda _node, _url: False,
    )
    monkeypatch.setattr(time, "sleep", sleep)
    channel_url = "https://meshtastic.org/e/#sensitive"

    applied = main_module._apply_configure_channel_url(
        target_node,
        channel_url,
        config_key="channel_url",
    )

    assert applied is True
    target_node.setURL.assert_called_once_with(channel_url)
    sleep.assert_called_once_with(main_module.CONFIG_SETURL_DELAY_SECONDS)
    output = capsys.readouterr().out
    assert "<redacted>" in output
    assert channel_url not in output


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_preflights_before_direct_mutations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject invalid transaction values before applying direct settings."""
    config_path = tmp_path / "invalid_after_owner.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "owner": "Must Not Be Applied",
                "config": {"bluetooth": {"mode": "NOT_A_MODE"}},
            }
        ),
        encoding="utf-8",
    )
    iface, target_node = _build_configure_interface()

    with pytest.raises(SystemExit) as excinfo:
        _run_main_configure_file(config_path, iface, monkeypatch)

    assert excinfo.value.code == 1
    target_node.setOwner.assert_not_called()
    target_node.beginSettingsTransaction.assert_not_called()
    target_node.writeConfig.assert_not_called()
    target_node.commitSettingsTransaction.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_configure_preflight_reuses_target_node_and_preserves_logger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "valid_preflight.yaml"
    config_path.write_text(
        yaml.safe_dump({"config": {"bluetooth": {"enabled": True}}}),
        encoding="utf-8",
    )
    iface, target_node = _build_configure_interface()
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    args = SimpleNamespace(configure=[str(config_path)], dest=None)

    main_module._handle_configure_command(iface, args, {})

    iface.getNode.assert_called_once_with(None, False)
    assert target_node.writeConfig.call_args_list == [call("bluetooth")]
    target_node.commitSettingsTransaction.assert_called_once_with()
    assert main_module.logger.disabled is False


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_configure_preflight_reports_structured_invalid_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "bad_pin.yaml"
    config_path.write_text(
        yaml.safe_dump({"config": {"bluetooth": {"fixed_pin": "not-an-int"}}}),
        encoding="utf-8",
    )
    iface, target_node = _build_configure_interface()

    with pytest.raises(SystemExit):
        _run_main_configure_file(config_path, iface, monkeypatch)

    out, err = capsys.readouterr()
    assert "Set bluetooth.fixed_pin" not in out
    assert "Invalid field: bluetooth.fixed_pin" in err
    target_node.beginSettingsTransaction.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_set_pref_repeated_field_progress_outside_preflight(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Normal repeated-field updates should retain their progress messages."""
    config = localonly_pb2.LocalConfig()

    assert setPref(config, "lora.ignore_incoming", "123") is True
    assert setPref(config, "lora.ignore_incoming", "0") is True

    out, err = capsys.readouterr()
    assert "Adding '123' to the ignore_incoming list" in out
    assert "Clearing ignore_incoming list" in out
    assert list(config.lora.ignore_incoming) == []
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_export_config_round_trips_nested_module_bytes_fields() -> None:
    """Firmware 2.8 Mesh Beacon channel PSKs must remain bytes after restore."""
    source_local = localonly_pb2.LocalConfig()
    source_module = localonly_pb2.LocalModuleConfig()
    source_module.mesh_beacon.broadcast_interval_secs = 60
    source_module.mesh_beacon.broadcast_offer_channel.psk = b"\x01\x02\x03\x04"
    source_module.mesh_beacon.broadcast_targets.add().channel_index = 2

    exported_yaml = export_config(_build_export_interface(source_local, source_module))
    exported = yaml.safe_load(exported_yaml)
    mesh_beacon = exported["module_config"]["mesh_beacon"]
    assert mesh_beacon["broadcastOfferChannel"]["psk"].startswith("base64:")
    # broadcast_targets replaces broadcast_on_channel; the CLI restore path does
    # not accept repeated composite lists yet, so verify the export contract and
    # drop it before the restore leg.
    assert mesh_beacon.pop("broadcastTargets") == [{"channelIndex": 2}]

    restored = localonly_pb2.LocalModuleConfig()
    assert traverseConfig("mesh_beacon", mesh_beacon, restored) is True
    assert restored.mesh_beacon.broadcast_offer_channel.psk == b"\x01\x02\x03\x04"


@pytest.mark.unit
def test_prefix_base64_bytes_fields_handles_bytes_map_values() -> None:
    file_proto = descriptor_pb2.FileDescriptorProto(
        name="bytes_map_test.proto", package="mtjk.tests", syntax="proto3"
    )
    container = file_proto.message_type.add(name="BytesMap")
    entry = container.nested_type.add(name="ValuesEntry")
    entry.options.map_entry = True
    entry.field.add(
        name="key",
        number=1,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
    )
    entry.field.add(
        name="value",
        number=2,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_BYTES,
    )
    container.field.add(
        name="values",
        number=1,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE,
        type_name=".mtjk.tests.BytesMap.ValuesEntry",
    )
    pool = descriptor_pool.DescriptorPool()
    pool.Add(file_proto)
    message_class = message_factory.GetMessageClass(
        pool.FindMessageTypeByName("mtjk.tests.BytesMap")
    )
    message = cast(Any, message_class())
    message.values["primary"] = b"\x01\x02"
    values = MessageToDict(message)

    _prefix_base64_bytes_fields(message, values)

    assert values == {"values": {"primary": "base64:AQI="}}


@pytest.mark.unit
def test_prefix_base64_bytes_fields_rejects_invalid_repeated_values() -> None:
    message = localonly_pb2.LocalConfig()
    values: dict[str, Any] = {"security": {"adminKey": ["AQI=", 7]}}

    with pytest.raises(TypeError, match="repeated bytes field security.admin_key"):
        _prefix_base64_bytes_fields(message, values)


@pytest.mark.unit
def test_prefix_base64_bytes_fields_walks_nested_message_shapes() -> None:
    message = _build_nested_bytes_test_message()
    message.child_map["primary"].payload = b"\x01"
    message.children.add().payload = b"\x02"
    message.child.payload = b"\x03"
    message.blobs.extend([b"\x04", b"\x05"])
    message.scalar_blob = b"\x06"
    values = MessageToDict(message)

    _prefix_base64_bytes_fields(message, values)

    assert values == {
        "childMap": {"primary": {"payload": "base64:AQ=="}},
        "children": [{"payload": "base64:Ag=="}],
        "child": {"payload": "base64:Aw=="},
        "blobs": ["base64:BA==", "base64:BQ=="],
        "scalarBlob": "base64:Bg==",
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"scalarBlob": 7}, "bytes field scalar_blob"),
        ({"childMap": []}, "protobuf map field child_map"),
        (
            {"childMap": {"primary": "not-a-mapping"}},
            "protobuf message map value child_map",
        ),
        ({"children": {}}, "repeated message field children"),
        ({"children": ["not-a-mapping"]}, "children\\[0\\]"),
        ({"child": []}, "message field child"),
    ],
)
def test_prefix_base64_bytes_fields_rejects_invalid_message_shapes(
    values: dict[str, Any],
    message: str,
) -> None:
    proto = _build_nested_bytes_test_message()

    with pytest.raises(TypeError, match=message):
        _prefix_base64_bytes_fields(proto, values)
