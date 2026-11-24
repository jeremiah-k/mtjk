"""Regression tests for atomic validation of multi-entry --set commands."""

import sys
from unittest.mock import MagicMock, patch

import pytest

from meshtastic.__main__ import _REDACTED_PREF_VALUE, _is_secret_pref, main
from meshtastic.node import Node
from meshtastic.protobuf import localonly_pb2
from meshtastic.tcp_interface import TCPInterface


def _interface_with_config_node() -> tuple[MagicMock, MagicMock]:
    """
    Create a mocked interface and node with empty local and module configurations.

    Returns
    -------
    tuple[MagicMock, MagicMock]
        The configured mock interface and associated mock node.
    """
    interface = MagicMock(autospec=TCPInterface)
    interface.__enter__ = MagicMock(return_value=interface)
    interface.__exit__ = MagicMock(return_value=None)
    node = MagicMock(autospec=Node)
    node.localConfig = localonly_pb2.LocalConfig()
    node.moduleConfig = localonly_pb2.LocalModuleConfig()
    interface.getNode.return_value = node
    return interface, node


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_invalid_batch_is_rejected_before_any_config_mutation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A fatal later entry must leave every earlier setting untouched."""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meshtastic",
            "--host",
            "meshtastic.local",
            "--set",
            "bluetooth.enabled",
            "true",
            "--set",
            "lora.hop_limit",
            "not_a_number",
        ],
    )
    interface, node = _interface_with_config_node()

    with patch("meshtastic.tcp_interface.TCPInterface", return_value=interface):
        with pytest.raises(SystemExit) as exc_info:
            main()

    assert exc_info.value.code == 1
    assert node.localConfig.bluetooth.enabled is False
    assert node.localConfig.lora.hop_limit == 0
    node.beginSettingsTransaction.assert_not_called()
    node.writeConfig.assert_not_called()
    node.commitSettingsTransaction.assert_not_called()
    _out, err = capsys.readouterr()
    assert "--set batch rejected before applying changes" in err
    assert "lora.hop_limit" in err


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_preflight_reports_every_invalid_entry(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Preflight must aggregate every fatal value error in one invocation."""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meshtastic",
            "--host",
            "meshtastic.local",
            "--set",
            "lora.hop_limit",
            "bad",
            "--set",
            "power.ls_secs",
            "also_bad",
        ],
    )
    interface, node = _interface_with_config_node()

    with patch("meshtastic.tcp_interface.TCPInterface", return_value=interface):
        with pytest.raises(SystemExit):
            main()

    _out, err = capsys.readouterr()
    assert "lora.hop_limit" in err
    assert "power.ls_secs" in err
    node.writeConfig.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_valid_multi_section_batch_still_uses_one_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meshtastic",
            "--host",
            "meshtastic.local",
            "--set",
            "bluetooth.enabled",
            "true",
            "--set",
            "power.ls_secs",
            "300",
        ],
    )
    interface, node = _interface_with_config_node()

    with patch("meshtastic.tcp_interface.TCPInterface", return_value=interface):
        main()

    assert node.localConfig.bluetooth.enabled is True
    assert node.localConfig.power.ls_secs == 300
    assert [request.args[0].name for request in node.requestConfig.call_args_list] == [
        "bluetooth",
        "power",
    ]
    node.beginSettingsTransaction.assert_called_once_with()
    assert {call.args[0] for call in node.writeConfig.call_args_list} == {
        "bluetooth",
        "power",
    }
    node.commitSettingsTransaction.assert_called_once_with()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_nonfatal_validation_rejection_still_prevents_prior_batch_mutation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A validation warning later in a batch must not apply an earlier entry."""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meshtastic",
            "--host",
            "meshtastic.local",
            "--set",
            "bluetooth.enabled",
            "true",
            "--set",
            "network.wifi_psk",
            "short",
        ],
    )
    interface, node = _interface_with_config_node()

    with patch("meshtastic.tcp_interface.TCPInterface", return_value=interface):
        main()

    assert node.localConfig.bluetooth.enabled is False
    node.writeConfig.assert_not_called()
    out, err = capsys.readouterr()
    assert "network.wifi_psk must be 8 or more characters" in out
    assert "do not have an attribute network.wifi_psk" not in out
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_preflight_reports_all_unknown_fields_with_choices_once(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A batch should expose every unknown field without repeating the choice dump."""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meshtastic",
            "--host",
            "meshtastic.local",
            "--set",
            "lora.not_a_field",
            "1",
            "--set",
            "power.also_not_a_field",
            "2",
        ],
    )
    interface, node = _interface_with_config_node()

    with patch("meshtastic.tcp_interface.TCPInterface", return_value=interface):
        main()

    out, err = capsys.readouterr()
    assert "do not have an attribute lora.not_a_field" in out
    assert "do not have an attribute power.also_not_a_field" in out
    assert out.count("Choices are...") == 1
    assert err == ""
    node.writeConfig.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_preflight_surfaces_unknown_fields_alongside_fatal_values(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Mixed invalid batches should expose all independently actionable failures."""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meshtastic",
            "--host",
            "meshtastic.local",
            "--set",
            "lora.not_a_field",
            "1",
            "--set",
            "lora.hop_limit",
            "not_a_number",
        ],
    )
    interface, node = _interface_with_config_node()

    with patch("meshtastic.tcp_interface.TCPInterface", return_value=interface):
        with pytest.raises(SystemExit) as exc_info:
            main()

    assert exc_info.value.code == 1
    out, err = capsys.readouterr()
    assert "lora.not_a_field" in out
    assert "lora.hop_limit" in err
    node.writeConfig.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_camel_case_multiword_section_preflights_and_applies_consistently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CamelCase multi-word roots should resolve identically in both phases."""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meshtastic",
            "--host",
            "meshtastic.local",
            "--set",
            "externalNotification.enabled",
            "true",
        ],
    )
    interface, node = _interface_with_config_node()

    with patch("meshtastic.tcp_interface.TCPInterface", return_value=interface):
        main()

    assert node.moduleConfig.external_notification.enabled is True
    node.writeConfig.assert_called_once_with("external_notification")
    node.beginSettingsTransaction.assert_not_called()
    node.commitSettingsTransaction.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_unknown_field_cancels_prior_valid_entry_without_error_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unknown fields preserve exit-0 guidance while cancelling the whole batch."""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meshtastic",
            "--host",
            "meshtastic.local",
            "--set",
            "bluetooth.enabled",
            "true",
            "--set",
            "lora.not_a_field",
            "1",
        ],
    )
    interface, node = _interface_with_config_node()

    with patch("meshtastic.tcp_interface.TCPInterface", return_value=interface):
        main()

    assert node.localConfig.bluetooth.enabled is False
    node.writeConfig.assert_not_called()
    out, err = capsys.readouterr()
    assert "do not have an attribute lora.not_a_field" in out
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize(
    ("field", "value", "diagnostic"),
    (
        ("lora.region", "NOT_A_REGION", "does not have an enum called"),
        ("network.enabled_protocols", "TCP", "Unknown flag 'TCP'"),
    ),
)
def test_semantic_rejection_cancels_prior_valid_entry_and_reports_reason(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    field: str,
    value: str,
    diagnostic: str,
) -> None:
    """Enum and bitfield failures remain nonfatal but cannot partially apply a batch."""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meshtastic",
            "--host",
            "meshtastic.local",
            "--set",
            "bluetooth.enabled",
            "true",
            "--set",
            field,
            value,
        ],
    )
    interface, node = _interface_with_config_node()

    with patch("meshtastic.tcp_interface.TCPInterface", return_value=interface):
        main()

    assert node.localConfig.bluetooth.enabled is False
    node.writeConfig.assert_not_called()
    out, err = capsys.readouterr()
    assert diagnostic in out
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_semantic_and_fatal_rejections_are_reported_together_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A fatal batch reports captured semantic diagnostics in the same error stream."""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meshtastic",
            "--host",
            "meshtastic.local",
            "--set",
            "lora.region",
            "NOT_A_REGION",
            "--set",
            "power.ls_secs",
            "not-a-number",
        ],
    )
    interface, node = _interface_with_config_node()

    with patch("meshtastic.tcp_interface.TCPInterface", return_value=interface):
        with pytest.raises(SystemExit) as exc_info:
            main()

    assert exc_info.value.code == 1
    node.writeConfig.assert_not_called()
    out, err = capsys.readouterr()
    assert "does not have an enum called" not in out
    assert "power.ls_secs" in err
    assert "lora.region: value rejected by validation" in err
    assert "does not have an enum called" in err


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_preflight_exception_redacts_secret_value_across_runtime_messages(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Runtime-specific exception wording must never defeat secret redaction."""
    secret = "SENTINEL_PRIVATE_VALUE"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meshtastic",
            "--host",
            "meshtastic.local",
            "--set",
            "bluetooth.fixed_pin",
            secret,
        ],
    )
    interface, node = _interface_with_config_node()

    with (
        patch("meshtastic.tcp_interface.TCPInterface", return_value=interface),
        patch(
            "meshtastic.cli.set_actions._set_pref",
            side_effect=TypeError(f"{secret!r} has type str"),
        ),
    ):
        with pytest.raises(SystemExit) as exc_info:
            main()

    assert _is_secret_pref("bluetooth.fixed_pin")
    assert exc_info.value.code == 1
    node.writeConfig.assert_not_called()
    out, err = capsys.readouterr()
    assert (
        f"bluetooth.fixed_pin: invalid value {_REDACTED_PREF_VALUE} (TypeError)" in err
    )
    assert secret not in out + err
