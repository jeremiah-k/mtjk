"""CLI regression tests for invalid preference and channel value types."""

import sys
from unittest.mock import patch

import pytest

import meshtastic.cli.preference_runtime as preference_runtime
from meshtastic.__main__ import main, setPref
from meshtastic.protobuf import config_pb2, localonly_pb2
from meshtastic.schema_metadata import FieldMetadata as _SchemaMetadata

from .cli_validation_test_helpers import _mock_tcp_interface_with_channels


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "value", "expected_type"),
    (
        ("lora.hop_limit", "not_a_number", "integer"),
        ("bluetooth.enabled", "not_a_boolean", "boolean"),
        ("power.adc_multiplier_override", "not_a_number", "number"),
        ("power.ls_secs", str(1 << 40), "integer"),
    ),
)
def test_set_pref_rejects_invalid_scalar_types_without_exception(
    field: str,
    value: str,
    expected_type: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = localonly_pb2.LocalConfig()

    assert setPref(config, field, value) is False

    out, err = capsys.readouterr()
    assert f"Invalid value {value!r} for {field}; expected {expected_type}." in out
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_cli_invalid_integer_set_exits_without_writing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meshtastic",
            "--host",
            "meshtastic.local",
            "--set",
            "lora.hop_limit",
            "not_a_number",
        ],
    )
    interface, node = _mock_tcp_interface_with_channels()

    with patch("meshtastic.tcp_interface.TCPInterface", return_value=interface):
        with pytest.raises(SystemExit) as exc_info:
            main()

    assert exc_info.value.code == 1
    node.writeConfig.assert_not_called()
    out, err = capsys.readouterr()
    assert "expected integer" in err
    assert "Traceback" not in out + err


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_cli_invalid_channel_psk_exits_without_writing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meshtastic",
            "--host",
            "meshtastic.local",
            "--ch-index",
            "1",
            "--ch-set",
            "psk",
            "0xNOTHEX",
        ],
    )
    interface, node = _mock_tcp_interface_with_channels()

    with patch("meshtastic.tcp_interface.TCPInterface", return_value=interface):
        with pytest.raises(SystemExit) as exc_info:
            main()

    assert exc_info.value.code == 1
    node.writeChannel.assert_not_called()
    out, err = capsys.readouterr()
    assert "Invalid channel PSK: Invalid hex PSK" in err
    assert "Traceback" not in out + err


@pytest.mark.unit
def test_set_pref_preserves_numeric_and_numeric_string_behavior() -> None:
    config = localonly_pb2.LocalConfig()

    assert setPref(config, "lora.hop_limit", "5") is True
    assert config.lora.hop_limit == 5
    assert setPref(config, "network.ntp_server", "123") is True
    assert config.network.ntp_server == "123"


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_cli_valid_hex_channel_psk_still_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meshtastic",
            "--host",
            "meshtastic.local",
            "--ch-index",
            "1",
            "--ch-set",
            "psk",
            "0x1a1a",
        ],
    )
    interface, node = _mock_tcp_interface_with_channels()

    with patch("meshtastic.tcp_interface.TCPInterface", return_value=interface):
        main()

    assert node.channels[1].settings.psk == b"\x1a\x1a"
    node.writeChannel.assert_called_once_with(1)


@pytest.mark.unit
def test_set_pref_valid_enum_still_uses_symbolic_name() -> None:
    config = localonly_pb2.LocalConfig()

    assert setPref(config, "lora.region", "US") is True
    assert config.lora.region == config_pb2.Config.LoRaConfig.RegionCode.US


@pytest.mark.unit
def test_set_pref_redacts_secret_values_in_validation_errors(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Invalid secret-bearing values must not be echoed in diagnostics."""
    config = localonly_pb2.LocalConfig()
    secret = "definitely-secret-not-bytes"

    assert setPref(config, "security.private_key", secret) is False

    out, err = capsys.readouterr()
    assert "Invalid value <redacted> for security.private_key" in out
    assert secret not in out + err


@pytest.mark.unit
def test_set_pref_rejects_malformed_encoded_value_without_exception(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Malformed encoded input is rejected through the normal validation path."""
    config = localonly_pb2.LocalConfig()
    malformed = "0xNOTHEX"

    assert setPref(config, "security.private_key", malformed) is False

    out, err = capsys.readouterr()
    assert "Invalid value <redacted> for security.private_key" in out
    assert malformed not in out + err
    assert config.security.private_key == b""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_cli_malformed_encoded_value_exits_without_writing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    malformed = "0xNOTHEX"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meshtastic",
            "--host",
            "meshtastic.local",
            "--set",
            "security.private_key",
            malformed,
        ],
    )
    interface, node = _mock_tcp_interface_with_channels()

    with patch("meshtastic.tcp_interface.TCPInterface", return_value=interface):
        with pytest.raises(SystemExit) as exc_info:
            main()

    assert exc_info.value.code == 1
    node.writeConfig.assert_not_called()
    out, err = capsys.readouterr()
    assert "Invalid value <redacted> for security.private_key" in err
    assert malformed not in out + err
    assert "Traceback" not in out + err


@pytest.mark.unit
def test_set_pref_repeated_failure_does_not_partially_mutate(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Repeated scalar validation must complete on a copy before mutation."""
    config = localonly_pb2.LocalConfig()
    config.lora.ignore_incoming.append(123)

    assert setPref(config, "lora.ignore_incoming", "not-a-number") is False

    assert list(config.lora.ignore_incoming) == [123]
    out, err = capsys.readouterr()
    assert "expected integer" in out
    assert "Adding 'not-a-number'" not in out
    assert err == ""


@pytest.mark.unit
def test_set_pref_repeated_list_preserves_historical_string_conversion() -> None:
    """Repeated list entries retain historical per-item ``fromStr`` conversion."""
    config = localonly_pb2.LocalConfig()
    config.lora.ignore_incoming.append(123)

    assert setPref(config, "lora.ignore_incoming", ["456"]) is True

    assert list(config.lora.ignore_incoming) == [456]


@pytest.mark.unit
def test_set_pref_invalid_repeated_list_is_atomic(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A bad converted list element must not partially replace the live container."""
    config = localonly_pb2.LocalConfig()
    config.lora.ignore_incoming.append(123)

    assert setPref(config, "lora.ignore_incoming", ["456", "not-a-number"]) is False

    assert list(config.lora.ignore_incoming) == [123]
    out, err = capsys.readouterr()
    assert "expected integer" in out
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_cli_invalid_repeated_value_exits_without_mutation_or_write(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meshtastic",
            "--host",
            "meshtastic.local",
            "--set",
            "lora.ignore_incoming",
            "not-a-number",
        ],
    )
    interface, node = _mock_tcp_interface_with_channels()
    node.localConfig.lora.ignore_incoming.append(123)

    with patch("meshtastic.tcp_interface.TCPInterface", return_value=interface):
        with pytest.raises(SystemExit) as exc_info:
            main()

    assert exc_info.value.code == 1
    assert list(node.localConfig.lora.ignore_incoming) == [123]
    node.writeConfig.assert_not_called()
    out, err = capsys.readouterr()
    assert "expected integer" in err
    assert "Traceback" not in out + err


@pytest.mark.unit
def test_set_pref_enforces_schema_metadata_numeric_bounds(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Schema presentation bounds reject values the firmware would reject."""
    config = localonly_pb2.LocalConfig()

    assert setPref(config, "lora.hop_limit", "8") is False
    assert config.lora.hop_limit == 0

    out, err = capsys.readouterr()
    assert "Invalid value 8 for lora.hop_limit; expected between 0 and 7." in out
    assert err == ""


@pytest.mark.unit
def test_set_pref_enforces_expanded_schema_metadata_bounds(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Bounds added by later upstream annotation expansions activate too."""
    config = localonly_pb2.LocalConfig()

    assert setPref(config, "lora.tx_power", "31") is False
    assert config.lora.tx_power == 0

    out, err = capsys.readouterr()
    assert "Invalid value 31 for lora.tx_power; expected between 0 and 30." in out
    assert err == ""


@pytest.mark.unit
@pytest.mark.parametrize("value", ("0", "7"))
def test_set_pref_accepts_schema_metadata_boundaries(value: str) -> None:
    """Inclusive schema bounds remain valid preference values."""
    config = localonly_pb2.LocalConfig()

    assert setPref(config, "lora.hop_limit", value) is True
    assert config.lora.hop_limit == int(value)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_cli_out_of_metadata_bounds_exits_without_writing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI preflight rejects out-of-range metadata values before device writes."""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meshtastic",
            "--host",
            "meshtastic.local",
            "--set",
            "lora.hop_limit",
            "8",
        ],
    )
    interface, node = _mock_tcp_interface_with_channels()

    with patch("meshtastic.tcp_interface.TCPInterface", return_value=interface):
        with pytest.raises(SystemExit) as exc_info:
            main()

    assert exc_info.value.code == 1
    node.writeConfig.assert_not_called()
    out, err = capsys.readouterr()
    assert "expected between 0 and 7" in err
    assert "Traceback" not in out + err


@pytest.mark.unit
def test_metadata_bounds_use_fatal_preflight_policy() -> None:
    """Configure preflight receives bounds failures through the shared fatal path."""
    from meshtastic.cli.preference_runtime import (
        PreferenceValueError,
        fatal_preference_value_errors,
    )

    config = localonly_pb2.LocalConfig()

    with fatal_preference_value_errors():
        with pytest.raises(PreferenceValueError, match=r"expected between 0 and 7"):
            setPref(config, "lora.hop_limit", "8")

    assert config.lora.hop_limit == 0


@pytest.mark.unit
@pytest.mark.parametrize(
    ("metadata", "value", "expected"),
    (
        (_SchemaMetadata(min_value=1.0), "0", "at least 1"),
        (_SchemaMetadata(max_value=1.0), "2", "at most 1"),
    ),
)
def test_set_pref_reports_one_sided_metadata_bounds(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    metadata: _SchemaMetadata,
    value: str,
    expected: str,
) -> None:
    """One-sided schema bounds retain precise validation diagnostics."""
    monkeypatch.setattr(
        preference_runtime, "_get_field_metadata", lambda _field: metadata
    )
    config = localonly_pb2.LocalConfig()

    assert setPref(config, "power.adc_multiplier_override", value) is False

    out, err = capsys.readouterr()
    assert expected in out
    assert err == ""


@pytest.mark.unit
@pytest.mark.parametrize(
    ("metadata", "value"),
    (
        (_SchemaMetadata(min_value=0.0, max_value=1.0), "nan"),
        (_SchemaMetadata(min_value=0.0), "inf"),
        (_SchemaMetadata(max_value=1.0), "-inf"),
    ),
)
def test_set_pref_rejects_non_finite_values_for_bounded_float_fields(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    metadata: _SchemaMetadata,
    value: str,
) -> None:
    """NaN and infinities cannot bypass finite schema presentation bounds."""
    monkeypatch.setattr(
        preference_runtime, "_get_field_metadata", lambda _field: metadata
    )
    config = localonly_pb2.LocalConfig()

    assert setPref(config, "power.adc_multiplier_override", value) is False
    assert config.power.adc_multiplier_override == 0.0

    out, err = capsys.readouterr()
    assert "Invalid value" in out
    assert "power.adc_multiplier_override" in out
    assert err == ""


@pytest.mark.unit
def test_set_pref_enforces_firmware_string_size_limits(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Encoded string length beyond the firmware limit is rejected."""
    config = localonly_pb2.LocalConfig()

    assert setPref(config, "network.wifi_ssid", "x" * 33) is False
    assert config.network.wifi_ssid == ""

    out, err = capsys.readouterr()
    assert "encoded length 33 bytes exceeds the firmware limit of 32 bytes" in out
    assert err == ""


@pytest.mark.unit
def test_set_pref_accepts_firmware_string_size_boundary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A string exactly filling the firmware capacity is accepted."""
    config = localonly_pb2.LocalConfig()

    assert setPref(config, "network.wifi_ssid", "x" * 32) is True
    assert config.network.wifi_ssid == "x" * 32


@pytest.mark.unit
def test_set_pref_measures_utf8_bytes_not_code_points(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Multi-byte characters count as their encoded byte length."""
    config = localonly_pb2.LocalConfig()

    assert setPref(config, "network.ntp_server", "é" * 33) is False
    assert config.network.ntp_server == ""

    out, _ = capsys.readouterr()
    assert "encoded length 66 bytes exceeds the firmware limit of 32 bytes" in out


@pytest.mark.unit
def test_set_pref_enforces_repeated_entry_count(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Repeated assignments beyond the firmware count limit are rejected."""
    config = localonly_pb2.LocalConfig()

    assert setPref(config, "lora.ignore_incoming", "1,2,3,4") is False
    assert list(config.lora.ignore_incoming) == []

    out, err = capsys.readouterr()
    assert "4 entries exceeds the firmware limit of 3" in out
    assert err == ""


@pytest.mark.unit
def test_set_pref_accepts_repeated_entry_count_boundary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A repeated assignment filling the firmware count is accepted."""
    config = localonly_pb2.LocalConfig()

    assert setPref(config, "lora.ignore_incoming", "1,2,3") is True
    assert list(config.lora.ignore_incoming) == [1, 2, 3]


@pytest.mark.unit
def test_set_pref_enforces_string_limit_after_historical_coercion(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Numeric-looking strings are size-checked after string-field coercion."""
    config = localonly_pb2.LocalConfig()

    assert setPref(config, "network.ntp_server", "1" * 33) is False
    assert config.network.ntp_server == ""

    out, err = capsys.readouterr()
    assert "encoded length 33 bytes exceeds the firmware limit of 32 bytes" in out
    assert err == ""


@pytest.mark.unit
def test_set_pref_accepts_string_limit_after_historical_coercion_boundary() -> None:
    """A numeric-looking string at the usable nanopb boundary still assigns."""
    config = localonly_pb2.LocalConfig()

    assert setPref(config, "network.ntp_server", "1" * 32) is True
    assert config.network.ntp_server == "1" * 32


@pytest.mark.unit
def test_set_pref_enforces_repeated_element_size(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Oversized elements in repeated assignments are rejected."""
    config = localonly_pb2.LocalConfig()

    assert setPref(config, "security.admin_key", [b"x" * 33]) is False
    assert list(config.security.admin_key) == []

    out, err = capsys.readouterr()
    assert (
        "element encoded length 33 bytes exceeds the firmware limit of 32 bytes" in out
    )
    assert err == ""


@pytest.mark.unit
def test_set_pref_accepts_repeated_element_size_boundary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Repeated byte elements filling the firmware capacity are accepted."""
    config = localonly_pb2.LocalConfig()

    assert setPref(config, "security.admin_key", [b"x" * 32]) is True
    assert list(config.security.admin_key) == [b"x" * 32]


@pytest.mark.unit
def test_set_pref_rejects_unencodable_utf8_string(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Strings with lone surrogates are rejected instead of mis-measured."""
    config = localonly_pb2.LocalConfig()

    assert setPref(config, "network.wifi_ssid", "ab\ud800cd") is False
    assert config.network.wifi_ssid == ""

    out, err = capsys.readouterr()
    assert "not encodable as UTF-8" in out
    assert err == ""


def test_set_pref_warns_once_for_deprecated_field(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Writing a deprecated field warns once per process but still assigns."""
    from meshtastic.cli import preference_runtime

    monkeypatch.setattr(preference_runtime, "_DEPRECATED_FIELD_WARNINGS", set())
    config = localonly_pb2.LocalConfig()

    assert setPref(config, "device.serial_enabled", "true") is True
    assert config.device.serial_enabled is True
    out, _ = capsys.readouterr()
    assert "Warning: device.serial_enabled is deprecated" in out

    capsys.readouterr()
    assert setPref(config, "device.serial_enabled", "false") is True
    out, _ = capsys.readouterr()
    assert "deprecated" not in out


@pytest.mark.unit
def test_deprecated_warning_reporter_failure_is_advisory(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failed warning reporter cannot fail or consume a successful write."""
    from meshtastic import __main__ as main_module
    from meshtastic.cli import preference_runtime

    monkeypatch.setattr(preference_runtime, "_DEPRECATED_FIELD_WARNINGS", set())
    original_cli_print = main_module._cli_print

    def flaky_cli_print(message: str, *, force: bool = False) -> None:
        if "deprecated" in message:
            raise RuntimeError("synthetic warning reporter failure")
        original_cli_print(message, force=force)

    monkeypatch.setattr(main_module, "_cli_print", flaky_cli_print)
    config = localonly_pb2.LocalConfig()

    assert main_module.setPref(config, "device.serial_enabled", "true") is True
    assert config.device.serial_enabled is True
    assert "device.serial_enabled" not in preference_runtime._DEPRECATED_FIELD_WARNINGS

    monkeypatch.setattr(main_module, "_cli_print", original_cli_print)
    assert main_module.setPref(config, "device.serial_enabled", "false") is True
    out, err = capsys.readouterr()
    assert "Warning: device.serial_enabled is deprecated" in out
    assert err == ""


@pytest.mark.unit
def test_set_pref_does_not_warn_for_undeprecated_field(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fields without a deprecation marker assign without a warning."""
    config = localonly_pb2.LocalConfig()

    assert setPref(config, "lora.hop_limit", "3") is True

    out, _ = capsys.readouterr()
    assert "deprecated" not in out


@pytest.mark.unit
def test_set_pref_no_warning_for_rejected_deprecated_value(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected value on a deprecated field does not consume the warning."""
    from meshtastic.cli import preference_runtime

    monkeypatch.setattr(preference_runtime, "_DEPRECATED_FIELD_WARNINGS", set())
    config = localonly_pb2.LocalConfig()

    assert setPref(config, "display.gps_format", "NOSUCH_FORMAT") is False

    out, _ = capsys.readouterr()
    assert "deprecated" not in out
