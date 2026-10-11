"""Tests for --export-format and DeviceProfile (.cfg) config round-trips."""

import argparse
import base64
import os
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
import yaml

from meshtastic.__main__ import setPref
from meshtastic.cli import config_io, configure_actions
from meshtastic.cli.configure_actions import ConfigureActionHooks
from meshtastic.cli.context import ActionOutcome, CliContext, CliExit
from meshtastic.mesh_interface import MeshInterface
from meshtastic.protobuf import clientonly_pb2, config_pb2, localonly_pb2


def _hooks(
    *,
    export_config: Any = None,
    export_profile: Any = None,
    cli_exit: Any = None,
    is_local: bool = True,
) -> ConfigureActionHooks:
    return ConfigureActionHooks(
        handle_set_command=MagicMock(),
        handle_configure_command=MagicMock(return_value=(False, False)),
        export_config=export_config or MagicMock(return_value="yaml: true\n"),
        export_profile=export_profile or MagicMock(return_value=b"\x00profile"),
        cli_exit=cli_exit or cast(CliExit, lambda message, code=0: None),
        cli_print=MagicMock(),
        is_local_destination=MagicMock(return_value=is_local),
    )


def _context(
    interface: Any, *, export_config: Any, export_format: Any = "auto"
) -> CliContext:
    return CliContext(
        interface=interface,
        args=argparse.Namespace(
            set=None,
            configure=None,
            export_config=export_config,
            export_format=export_format,
            dest="^local",
        ),
        get_node_kwargs={},
        outcome=ActionOutcome(),
    )


@pytest.mark.unit
def test_configure_action_hooks_preserve_legacy_positional_constructor() -> None:
    """The new binary-export hook must not shift historical positional fields."""
    handle_set = MagicMock()
    handle_configure = MagicMock(return_value=(False, False))
    export_yaml = MagicMock(return_value="yaml: true\n")
    cli_exit = cast(CliExit, MagicMock())
    cli_print = MagicMock()
    is_local = MagicMock(return_value=True)

    hooks = ConfigureActionHooks(
        handle_set, handle_configure, export_yaml, cli_exit, cli_print, is_local
    )

    assert hooks.handle_set_command is handle_set
    assert hooks.handle_configure_command is handle_configure
    assert hooks.export_config is export_yaml
    assert hooks.cli_exit is cli_exit
    assert hooks.cli_print is cli_print
    assert hooks.is_local_destination is is_local
    assert hooks.export_profile is config_io._export_profile


@pytest.mark.unit
@pytest.mark.parametrize(
    ("fmt", "destination", "expected"),
    [
        ("auto", "out.cfg", "binary"),
        ("auto", "out.bin", "binary"),
        ("auto", "out.CFG", "binary"),
        ("auto", "out.yaml", "yaml"),
        ("auto", "-", "yaml"),
        ("yaml", "out.cfg", "yaml"),
        ("binary", "out.txt", "binary"),
        ("protobuf", "-", "binary"),
    ],
)
def test_resolve_export_format(fmt: str, destination: str, expected: str) -> None:
    """Format resolution honors explicit choices and extension auto-detection."""
    assert config_io._resolve_export_format(fmt, destination) == expected


@pytest.mark.unit
def test_export_writes_binary_profile_for_cfg_extension(tmp_path: Path) -> None:
    """Auto format with a .cfg destination writes serialized DeviceProfile bytes."""
    interface = cast(MeshInterface, MagicMock())
    payload = clientonly_pb2.DeviceProfile()
    payload.long_name = "Binary Owner"
    raw = payload.SerializeToString()
    export_path = tmp_path / "node.cfg"
    context = _context(interface, export_config=str(export_path))
    hooks = _hooks(export_profile=MagicMock(return_value=raw))

    configure_actions._handle_configure_actions(context, hooks)

    assert export_path.read_bytes() == raw
    assert (os.stat(export_path).st_mode & 0o777) == 0o600


@pytest.mark.unit
def test_export_binary_to_stdout_is_rejected() -> None:
    """Binary payloads must not be spewed to a text console."""
    interface = cast(MeshInterface, MagicMock())
    export_profile = MagicMock(return_value=b"\x00profile")
    exits: list[tuple[str, int]] = []

    def fake_exit(message: str, code: int = 0) -> None:
        exits.append((message, code))
        raise SystemExit(code)

    context = _context(interface, export_config="-", export_format="binary")
    hooks = _hooks(cli_exit=fake_exit, export_profile=export_profile)

    with pytest.raises(SystemExit):
        configure_actions._handle_configure_actions(context, hooks)
    assert exits and exits[0][1] == 1
    assert "Binary export requires a file path" in exits[0][0]
    # The payload is built lazily: termination must happen before the device
    # round-trip that get_payload() performs for a stdout export.
    export_profile.assert_not_called()


@pytest.mark.unit
def test_export_yaml_still_writes_text(tmp_path: Path) -> None:
    """Explicit yaml format keeps the historical text export path."""
    interface = cast(MeshInterface, MagicMock())
    export_path = tmp_path / "node.yaml"
    context = _context(interface, export_config=str(export_path), export_format="yaml")
    hooks = _hooks(export_config=MagicMock(return_value="owner: Someone\n"))

    configure_actions._handle_configure_actions(context, hooks)

    assert export_path.read_text(encoding="utf8") == "owner: Someone\n"


@pytest.mark.unit
def test_decode_prefers_yaml_mappings() -> None:
    """UTF-8 YAML mappings decode as YAML."""
    decoded = configure_actions._decode_configure_document(
        MagicMock(), b"owner: Tester\n", "config.yaml"
    )
    assert decoded == {"owner": "Tester"}


@pytest.mark.unit
@pytest.mark.parametrize("suffix", [".cfg", ".bin"])
def test_decode_accepts_explicit_yaml_export_with_binary_extension(suffix: str) -> None:
    """An explicitly requested YAML export remains importable regardless of suffix."""
    decoded = configure_actions._decode_configure_document(
        MagicMock(), b"owner: YAML In CFG\n", f"profile{suffix}"
    )

    assert decoded == {"owner": "YAML In CFG"}


@pytest.mark.unit
def test_decode_accepts_binary_profiles() -> None:
    """Serialized DeviceProfile bytes decode through the profile adapter."""
    payload = clientonly_pb2.DeviceProfile()
    payload.long_name = "Binary Owner"
    payload.config.lora.region = config_pb2.Config.LoRaConfig.RegionCode.US

    decoded = configure_actions._decode_configure_document(
        MagicMock(), payload.SerializeToString(), "node.cfg"
    )

    assert decoded is not None
    assert decoded["owner"] == "Binary Owner"
    assert decoded["config"]


@pytest.mark.unit
def test_decode_accepts_printable_binary_profile_without_cfg_extension() -> None:
    """A protobuf made only of YAML-safe UTF-8 bytes still auto-detects."""
    payload = clientonly_pb2.DeviceProfile(long_name="123456789")

    decoded = configure_actions._decode_configure_document(
        MagicMock(), payload.SerializeToString(), "profile.data"
    )

    assert decoded == {"owner": "123456789"}


@pytest.mark.unit
def test_decode_rejects_non_mapping_yaml() -> None:
    """Valid YAML that is not a mapping keeps the historical shape error."""
    exits: list[str] = []

    def fake_exit(message: str, code: int = 0) -> None:
        exits.append(message)
        raise SystemExit(code)

    hooks = MagicMock()
    hooks.cli_exit = fake_exit
    with pytest.raises(SystemExit):
        configure_actions._decode_configure_document(hooks, b"[]", "bad.yaml")
    assert any("mapping/dictionary" in message for message in exits)


@pytest.mark.unit
def test_decode_rejects_garbage_with_combined_error() -> None:
    """Files that are neither YAML nor DeviceProfile fail with both formats named."""
    exits: list[str] = []

    def fake_exit(message: str, code: int = 0) -> None:
        exits.append(message)
        raise SystemExit(code)

    hooks = MagicMock()
    hooks.cli_exit = fake_exit
    with pytest.raises(SystemExit):
        configure_actions._decode_configure_document(
            hooks, b"\xff\xfe\xfd\xfc\xfb\xfa", "garbage.cfg"
        )
    assert exits
    assert "YAML config" in exits[0]
    assert "DeviceProfile" in exits[0]


@pytest.mark.unit
def test_profile_export_round_trip() -> None:
    """Exported profile bytes parse back into the YAML document shape."""
    mock = MagicMock(spec=MeshInterface)
    mock.localNode = MagicMock()
    mock.getLongName.return_value = "Jeremiah K"
    mock.getShortName.return_value = "JK"
    mock.localNode.getURL.return_value = "https://meshtastic.org/e/#ABC"
    mock.getCannedMessage.return_value = "ping||pong"
    mock.getRingtone.return_value = "ring"
    mock.getMyUser.return_value = {
        "isUnmessagable": False,
        "isLicensed": True,
    }
    mock.getMyNodeInfo.return_value = {
        "position": {"latitude": 37.5, "longitude": -122.1, "altitude": 52}
    }
    mock.localNode.localConfig = localonly_pb2.LocalConfig()
    mock.localNode.localConfig.lora.region = config_pb2.Config.LoRaConfig.RegionCode.US
    mock.localNode.moduleConfig = localonly_pb2.LocalModuleConfig()
    interface = cast(MeshInterface, mock)

    raw = config_io._export_profile(interface)
    configuration = config_io._profile_to_configuration(
        config_io._parse_profile_bytes(raw)
    )

    assert configuration["owner"] == "Jeremiah K"
    assert configuration["owner_short"] == "JK"
    assert configuration["channel_url"] == "https://meshtastic.org/e/#ABC"
    assert configuration["canned_messages"] == "ping||pong"
    assert configuration["ringtone"] == "ring"
    assert configuration["is_unmessagable"] is False
    assert configuration["is_licensed"] is True
    assert configuration["location"]["alt"] == 52
    assert configuration["config"]


@pytest.mark.unit
def test_profile_export_does_not_synthesize_location_from_altitude_only() -> None:
    """Binary export matches YAML and ignores an unusable altitude-only position."""
    mock = MagicMock()
    mock.getLongName.return_value = None
    mock.getShortName.return_value = None
    mock.localNode.getURL.return_value = None
    mock.getCannedMessage.return_value = None
    mock.getRingtone.return_value = None
    mock.getMyUser.return_value = {}
    mock.getMyNodeInfo.return_value = {"position": {"altitude": 123}}
    mock.localNode.localConfig = localonly_pb2.LocalConfig()
    mock.localNode.moduleConfig = localonly_pb2.LocalModuleConfig()

    profile = config_io._parse_profile_bytes(
        config_io._export_profile(cast(MeshInterface, mock))
    )

    assert not profile.HasField("fixed_position")


@pytest.mark.unit
def test_profile_conversion_preserves_present_zero_fixed_position() -> None:
    """An explicitly stored (0, 0, 0) position is not mistaken for absence."""
    profile = clientonly_pb2.DeviceProfile()
    profile.fixed_position.SetInParent()

    configuration = config_io._profile_to_configuration(profile)

    assert configuration["location"] == {"lat": 0.0, "lon": 0.0, "alt": 0}


@pytest.mark.unit
def test_profile_conversion_normalizes_bytes_and_true_defaults() -> None:
    """Binary config sections use the same bytes/default normalization as YAML."""
    profile = clientonly_pb2.DeviceProfile()
    profile.config.security.private_key = b"\x01\x02"
    profile.module_config.mqtt.address = "mqtt.example"

    configuration = config_io._profile_to_configuration(profile)

    security = configuration["config"]["security"]
    assert security["privateKey"] == "base64:AQI="
    assert security["serialEnabled"] is False
    mqtt = configuration["module_config"]["mqtt"]
    assert mqtt["encryptionEnabled"] is False


@pytest.mark.unit
def test_profile_repeated_message_section_reapplies_through_configure_engine() -> None:
    """DeviceProfile repeated submessages survive conversion and preference apply."""
    profile = clientonly_pb2.DeviceProfile()
    target = profile.module_config.mesh_beacon.broadcast_targets.add()
    target.preset = config_pb2.Config.LoRaConfig.ModemPreset.SHORT_FAST
    target.region = config_pb2.Config.LoRaConfig.RegionCode.US
    target.channel_index = 4

    configuration = config_io._profile_to_configuration(profile)
    raw_targets = configuration["module_config"]["mesh_beacon"]["broadcastTargets"]
    local = localonly_pb2.LocalModuleConfig()

    assert setPref(local, "mesh_beacon.broadcastTargets", raw_targets) is True

    assert len(local.mesh_beacon.broadcast_targets) == 1
    restored = local.mesh_beacon.broadcast_targets[0]
    assert restored.preset == config_pb2.Config.LoRaConfig.ModemPreset.SHORT_FAST
    assert restored.region == config_pb2.Config.LoRaConfig.RegionCode.US
    assert restored.channel_index == 4


@pytest.mark.unit
def test_profile_conversion_preserves_optional_ringtone() -> None:
    """An explicitly present ringtone survives DeviceProfile conversion."""
    profile = clientonly_pb2.DeviceProfile(ringtone="ring")
    assert config_io._profile_to_configuration(profile)["ringtone"] == "ring"


@pytest.mark.unit
def test_parse_profile_rejects_empty_payload() -> None:
    """An empty protobuf payload is not accepted as a valid DeviceProfile."""
    with pytest.raises(ValueError, match="no recognized fields"):
        config_io._parse_profile_bytes(b"")


@pytest.mark.unit
def test_decode_control_byte_garbage_without_profile_suffix_exits_cleanly() -> None:
    """Binary-looking garbage without a profile extension still gets a clear failure."""
    exits: list[str] = []

    def fake_exit(message: str, code: int = 0) -> None:
        exits.append(message)
        raise SystemExit(code)

    with pytest.raises(SystemExit):
        config_io._decode_configure_document(
            b"\x00", "profile.data", cli_exit=cast(CliExit, fake_exit)
        )
    assert any("DeviceProfile" in message for message in exits)


@pytest.mark.unit
def test_binary_profile_write_reports_open_failure(
    tmp_path: Path,
) -> None:
    """Filesystem open errors terminate through the CLI error surface."""
    exits: list[str] = []

    def fake_exit(message: str, code: int = 0) -> None:
        exits.append(message)
        raise SystemExit(code)

    with pytest.raises(SystemExit):
        config_io._write_binary_profile(
            str(tmp_path / "missing_dir" / "profile.cfg"),
            lambda: b"payload",
            cast(CliExit, fake_exit),
            MagicMock(),
        )
    assert "Failed to write config file" in exits[0]


@pytest.mark.unit
def test_binary_profile_write_closes_raw_descriptor_on_fdopen_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A descriptor acquired before fdopen failure is closed exactly once.

    ``os.open`` and ``fchmod`` stay real so the descriptor is a genuine open
    file. Only config_io-owned aliases for ``fdopen`` and ``close`` are replaced,
    so the fault injection cannot affect unrelated threads or test code.
    """
    exits: list[str] = []
    real_path = tmp_path / "profile.cfg"
    close_calls: list[int] = []
    real_close = os.close

    def spying_close(fd: int) -> None:
        close_calls.append(fd)
        real_close(fd)

    def failing_fdopen(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("fdopen forced")

    monkeypatch.setattr(config_io, "_os_fdopen", failing_fdopen)
    monkeypatch.setattr(config_io, "_os_close", spying_close)

    def fake_exit(message: str, return_value: int = 1) -> None:
        exits.append(message)
        raise SystemExit(return_value)

    with pytest.raises(SystemExit):
        config_io._write_binary_profile(
            str(real_path), lambda: b"payload", cast(CliExit, fake_exit), MagicMock()
        )

    # The helper acquired exactly one real descriptor via os.open and the
    # ownership-transfer bookkeeping closed it exactly once on the failure.
    assert len(close_calls) == 1
    assert "Failed to write config file" in exits[0]


@pytest.mark.unit
def test_yaml_export_includes_all_optional_local_snapshot_fields() -> None:
    """YAML export exercises every populated direct-state field from one snapshot."""
    mock = MagicMock(spec=MeshInterface)
    mock.localNode = MagicMock()
    mock.getLongName.return_value = "Owner"
    mock.getShortName.return_value = "OS"
    mock.localNode.getURL.return_value = "https://meshtastic.org/e/#ABC"
    mock.getCannedMessage.return_value = "one|two"
    mock.getRingtone.return_value = "ring"
    mock.getMyUser.return_value = {"isUnmessagable": True, "isLicensed": False}
    mock.getMyNodeInfo.return_value = {
        "position": {"latitude": 1.25, "longitude": -2.5, "altitude": 33}
    }
    mock.localNode.localConfig = localonly_pb2.LocalConfig()
    mock.localNode.moduleConfig = localonly_pb2.LocalModuleConfig()

    rendered = config_io.export_config(cast(MeshInterface, mock), camel_case=False)
    configuration = yaml.safe_load(rendered.split("\n", 1)[1])

    assert configuration["canned_messages"] == "one|two"
    assert configuration["ringtone"] == "ring"
    assert configuration["is_unmessagable"] is True
    assert configuration["is_licensed"] is False
    assert configuration["location"] == {"lat": 1.25, "lon": -2.5, "alt": 33}


@pytest.mark.unit
def test_partial_profile_import_does_not_invent_absent_sections() -> None:
    """Firmware-true defaults materialize only inside sections the profile carries.

    A partial binary profile naming a single config section must not gain the
    other sections with disabled flags: the configure path writes every
    section of the converted document, so invented sections would disable
    settings the profile never specified.
    """
    profile = clientonly_pb2.DeviceProfile()
    profile.config.network.wifi_ssid = "ssid"

    configuration = config_io._profile_to_configuration(profile)

    assert configuration["config"]["network"]["wifiSsid"] == "ssid"
    assert "bluetooth" not in configuration["config"]
    assert "lora" not in configuration["config"]
    assert "position" not in configuration["config"]
    assert "security" not in configuration["config"]


@pytest.mark.unit
def test_partial_profile_keeps_defaults_within_present_sections() -> None:
    """A section present in the profile still gains its omitted true defaults."""
    profile = clientonly_pb2.DeviceProfile()
    profile.config.lora.region = config_pb2.Config.LoRaConfig.RegionCode.US

    configuration = config_io._profile_to_configuration(profile)

    assert configuration["config"]["lora"]["usePreset"] is False


@pytest.mark.unit
def test_profile_export_retains_raw_neighbor_info_and_security_keys() -> None:
    """Export keeps raw values; equivalence lives only in verification.

    A stored NeighborInfo ``update_interval`` of 0 stays 0 in the binary
    profile and is never rewritten to 21600. Proto3 implicit presence omits
    the zero-valued leaf from dict exports, which is itself proof that no
    fabricated 21600 is written. Security key bytes survive untouched.
    """
    mock = MagicMock(spec=MeshInterface)
    mock.localNode = MagicMock()
    mock.getLongName.return_value = "Profile Owner"
    mock.getShortName.return_value = "PO"
    mock.localNode.getURL.return_value = "https://meshtastic.org/e/#ABC"
    mock.getCannedMessage.return_value = None
    mock.getRingtone.return_value = None
    mock.getMyUser.return_value = {"isUnmessagable": True, "isLicensed": False}
    mock.getMyNodeInfo.return_value = {
        "position": {"latitude": 1.25, "longitude": -2.5, "altitude": 33}
    }
    mock.localNode.localConfig = localonly_pb2.LocalConfig()
    mock.localNode.localConfig.security.private_key = bytes(range(32))
    mock.localNode.localConfig.security.admin_key.append(b"\xaa" * 32)
    mock.localNode.moduleConfig = localonly_pb2.LocalModuleConfig()
    mock.localNode.moduleConfig.neighbor_info.update_interval = 0
    mock.localNode.moduleConfig.neighbor_info.transmit_over_lora = True

    yaml_text = config_io.export_config(cast(MeshInterface, mock), camel_case=False)
    profile = config_io._parse_profile_bytes(
        config_io._export_profile(cast(MeshInterface, mock))
    )
    configuration = yaml.safe_load(yaml_text.split("\n", 1)[1])
    converted = config_io._profile_to_configuration(profile)

    assert mock.localNode.moduleConfig.neighbor_info.update_interval == 0
    assert profile.module_config.neighbor_info.update_interval == 0
    yaml_neighbor = configuration["module_config"]["neighbor_info"]
    converted_neighbor = converted["module_config"]["neighbor_info"]
    assert "updateInterval" not in yaml_neighbor
    assert "updateInterval" not in converted_neighbor
    assert yaml_neighbor["transmitOverLora"] is True
    private_b64 = "base64:" + base64.b64encode(bytes(range(32))).decode()
    admin_b64 = "base64:" + base64.b64encode(b"\xaa" * 32).decode()
    assert configuration["config"]["security"]["privateKey"] == private_b64
    assert configuration["config"]["security"]["adminKey"] == [admin_b64]
    assert profile.config.security.private_key == bytes(range(32))
    assert list(profile.config.security.admin_key) == [b"\xaa" * 32]
