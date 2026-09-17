"""CLI tests for protobuf schema field metadata presentation."""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

import meshtastic.cli.config_io as config_io
import meshtastic.cli.preference_runtime as preference_runtime
from meshtastic.__main__ import main
from meshtastic.cli.schema_metadata import _SchemaMetadata


@pytest.mark.unit
def test_describe_field_prints_current_hop_limit_metadata(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Numeric metadata is exposed without requiring a device connection."""
    assert config_io._describe_config_field(
        "lora.hop_limit",
        normalize_pref_name=preference_runtime.normalize_pref_name,
        display_pref_name=lambda value: value,
        type_label=preference_runtime.protobuf_field_type_label,
        bitfield_enums=preference_runtime.BITFIELD_ENUMS,
    )

    out, err = capsys.readouterr()
    assert err == ""
    assert "Field: lora.hop_limit" in out
    assert "Type: integer" in out
    assert "Label: Hop Limit" in out
    assert "Range: 0 to 7" in out
    assert "Keywords: hops, ttl, range, rebroadcast" in out


@pytest.mark.unit
def test_describe_field_prints_diy_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """Boolean metadata flags remain distinguishable from absent options."""
    assert config_io._describe_config_field(
        "position.rx_gpio",
        normalize_pref_name=preference_runtime.normalize_pref_name,
        display_pref_name=lambda value: value,
        type_label=preference_runtime.protobuf_field_type_label,
        bitfield_enums=preference_runtime.BITFIELD_ENUMS,
    )

    out, _ = capsys.readouterr()
    assert "Flags: DIY only" in out


@pytest.mark.unit
def test_describe_field_prints_enum_value_labels_and_keywords(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Schema-provided enum labels augment symbolic names without replacing them."""
    assert config_io._describe_config_field(
        "lora.modem_preset",
        normalize_pref_name=preference_runtime.normalize_pref_name,
        display_pref_name=lambda value: value,
        type_label=preference_runtime.protobuf_field_type_label,
        bitfield_enums=preference_runtime.BITFIELD_ENUMS,
    )

    out, _ = capsys.readouterr()
    assert "Choices:" in out
    assert "LONG_FAST = 0 - Long Range - Fast" in out
    assert "Keywords: longfast, default" in out


@pytest.mark.unit
def test_describe_field_prints_bitfield_enum_metadata(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Integer-backed bitfields expose their related enum metadata."""
    assert config_io._describe_config_field(
        "position.position_flags",
        normalize_pref_name=preference_runtime.normalize_pref_name,
        display_pref_name=lambda value: value,
        type_label=preference_runtime.protobuf_field_type_label,
        bitfield_enums=preference_runtime.BITFIELD_ENUMS,
    )

    out, _ = capsys.readouterr()
    assert "Type: integer (bitfield)" in out
    assert "ALTITUDE = 1 - Altitude" in out
    assert "Include an altitude value in position reports" in out


@pytest.mark.unit
def test_describe_unknown_field_returns_false_without_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unknown paths remain an explicit caller-handled error."""
    assert not config_io._describe_config_field(
        "lora.not_a_real_field",
        normalize_pref_name=preference_runtime.normalize_pref_name,
        display_pref_name=lambda value: value,
        type_label=preference_runtime.protobuf_field_type_label,
        bitfield_enums=preference_runtime.BITFIELD_ENUMS,
    )
    assert capsys.readouterr() == ("", "")


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_cli_describe_field_is_offline_and_skips_transport(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Schema description exits successfully without creating any interface."""
    monkeypatch.setattr(
        sys, "argv", ["meshtastic", "--describe-field", "lora.hop_limit"]
    )

    with patch("meshtastic.tcp_interface.TCPInterface") as tcp_interface:
        main()

    tcp_interface.assert_not_called()
    out, err = capsys.readouterr()
    assert "Field: lora.hop_limit" in out
    assert "Range: 0 to 7" in out
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_cli_describe_unknown_field_exits_before_transport(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unknown schema paths fail clearly without attempting a connection."""
    monkeypatch.setattr(
        sys, "argv", ["meshtastic", "--describe-field", "lora.no_such_field"]
    )

    with patch("meshtastic.tcp_interface.TCPInterface") as tcp_interface:
        with pytest.raises(SystemExit) as exc_info:
            main()

    assert exc_info.value.code == 1
    tcp_interface.assert_not_called()
    out, err = capsys.readouterr()
    assert "Unknown configurable field: lora.no_such_field" in err
    assert "Traceback" not in out + err


@pytest.mark.unit
def test_describe_field_resolves_module_config_fields(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Description lookup spans both local and module configuration wrappers."""
    assert config_io._describe_config_field(
        "mqtt.enabled",
        normalize_pref_name=preference_runtime.normalize_pref_name,
        display_pref_name=lambda value: value,
        type_label=preference_runtime.protobuf_field_type_label,
        bitfield_enums=preference_runtime.BITFIELD_ENUMS,
    )

    out, _ = capsys.readouterr()
    assert "Field: mqtt.enabled" in out
    assert "Type: boolean" in out


@pytest.mark.unit
def test_describe_field_resolves_compatibility_aliases(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Offline schema lookup uses the same alias normalization as --set/--get."""
    assert config_io._describe_config_field(
        "display.use12_hour",
        normalize_pref_name=preference_runtime.normalize_pref_name,
        display_pref_name=lambda value: value,
        type_label=preference_runtime.protobuf_field_type_label,
        bitfield_enums=preference_runtime.BITFIELD_ENUMS,
    )

    out, _ = capsys.readouterr()
    assert "Field: display.use_12h_clock" in out
    assert "Type: boolean" in out


@pytest.mark.unit
def test_describe_field_reports_standard_deprecation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Standard protobuf deprecation remains visible even without custom metadata."""
    assert config_io._describe_config_field(
        "device.serial_enabled",
        normalize_pref_name=preference_runtime.normalize_pref_name,
        display_pref_name=lambda value: value,
        type_label=preference_runtime.protobuf_field_type_label,
        bitfield_enums=preference_runtime.BITFIELD_ENUMS,
    )

    out, _ = capsys.readouterr()
    assert "Flags: deprecated" in out


@pytest.mark.unit
@pytest.mark.parametrize(
    ("metadata", "expected_lines"),
    (
        (_SchemaMetadata(min_value=1.5), ("Minimum: 1.5",)),
        (_SchemaMetadata(max_value=2.5), ("Maximum: 2.5",)),
        (
            _SchemaMetadata(unit="dBm", admin_only=True),
            ("Unit: dBm", "Flags: admin only"),
        ),
    ),
)
def test_describe_field_covers_optional_metadata_shapes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    metadata: _SchemaMetadata,
    expected_lines: tuple[str, ...],
) -> None:
    """One-sided bounds, units, and admin flags render when present."""
    monkeypatch.setattr(config_io, "_get_field_metadata", lambda _field: metadata)

    assert config_io._describe_config_field(
        "lora.hop_limit",
        normalize_pref_name=preference_runtime.normalize_pref_name,
        display_pref_name=lambda value: value,
        type_label=preference_runtime.protobuf_field_type_label,
        bitfield_enums=preference_runtime.BITFIELD_ENUMS,
    )

    out, err = capsys.readouterr()
    assert err == ""
    for expected in expected_lines:
        assert expected in out


@pytest.mark.unit
def test_describe_enum_reports_standard_deprecation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Deprecated enum choices expose the standard EnumValueOptions flag."""
    assert config_io._describe_config_field(
        "device.role",
        normalize_pref_name=preference_runtime.normalize_pref_name,
        display_pref_name=lambda value: value,
        type_label=preference_runtime.protobuf_field_type_label,
        bitfield_enums=preference_runtime.BITFIELD_ENUMS,
    )

    out, err = capsys.readouterr()
    assert err == ""
    assert "ROUTER_CLIENT = 3" in out
    router_client = out.split("ROUTER_CLIENT = 3", maxsplit=1)[1]
    assert "Flags: deprecated" in router_client.split("REPEATER = 4", maxsplit=1)[0]


@pytest.mark.unit
@pytest.mark.parametrize(
    "field_name",
    ("lora..hop_limit", "lora.tx_power.extra"),
)
def test_describe_field_rejects_malformed_or_scalar_nested_paths(
    field_name: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Malformed paths and traversal through scalar fields fail without output."""
    assert not config_io._describe_config_field(
        field_name,
        normalize_pref_name=preference_runtime.normalize_pref_name,
        display_pref_name=lambda value: value,
        type_label=preference_runtime.protobuf_field_type_label,
        bitfield_enums=preference_runtime.BITFIELD_ENUMS,
    )
    assert capsys.readouterr() == ("", "")
