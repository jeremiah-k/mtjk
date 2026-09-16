"""CLI regression tests for invalid preference and channel value types."""

import sys
from unittest.mock import patch

import pytest

from meshtastic.__main__ import main, setPref
from meshtastic.protobuf import config_pb2, localonly_pb2

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
