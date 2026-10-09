"""Dry-run preview tests for the CLI ``--set`` batch path."""

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import meshtastic.__main__ as main_module
from meshtastic.__main__ import _handle_set_command, _preview_set_command
from meshtastic.cli.config_preview import ConfigSnapshotCopies, render_preview_value
from meshtastic.node import Node
from meshtastic.protobuf import (
    channel_pb2,
    config_pb2,
    localonly_pb2,
    module_config_pb2,
)
from meshtastic.tcp_interface import TCPInterface


def _preview_interface() -> tuple[MagicMock, MagicMock]:
    """Return a mocked TCP interface whose node carries real protobuf configs."""
    interface = MagicMock(autospec=TCPInterface)
    interface.__enter__ = MagicMock(return_value=interface)
    interface.__exit__ = MagicMock(return_value=None)
    node = MagicMock(autospec=Node)
    node.noProto = False
    node.localConfig = localonly_pb2.LocalConfig()
    node.moduleConfig = localonly_pb2.LocalModuleConfig()
    interface.getNode.return_value = node
    return interface, node


def _set_args(entries: list[list[Any]], dest: str = "^local") -> SimpleNamespace:
    """Return parsed-argument namespace with ``--set`` entries and a dest.

    Values are ``Any`` because repeated-field assignments carry lists exactly
    like the production ``_normalize_set_entries`` contract.
    """
    return SimpleNamespace(dest=dest, set=entries)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_preview_renders_enum_bool_and_current_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Successful previews render new and current values through one renderer."""
    interface, node = _preview_interface()
    for section in ("lora", "bluetooth", "power"):
        getattr(node.localConfig, section).SetInParent()
    node.localConfig.power.ls_secs = 60
    args = _set_args(
        [
            ["lora.region", "US"],
            ["bluetooth.enabled", "true"],
            ["power.ls_secs", "300"],
        ]
    )

    snapshot = _preview_set_command(interface, args, {})

    out, err = capsys.readouterr()
    assert err == ""
    lines = out.splitlines()
    assert lines[0] == "Dry run: previewing --set batch without writing changes."
    assert "Would set lora.region = US (current: UNSET)" in lines
    assert "Would set bluetooth.enabled = true (current: false)" in lines
    assert "Would set power.ls_secs = 300 (current: 60)" in lines
    assert "Would write modified preferences to device" in lines
    assert "Would use a configuration transaction" in lines
    assert "Would write power configuration to device" in lines
    assert "Would write lora configuration to device" in lines
    assert "Would write bluetooth configuration to device" in lines
    node.requestConfig.assert_not_called()
    node.writeConfig.assert_not_called()
    node.beginSettingsTransaction.assert_not_called()
    node.commitSettingsTransaction.assert_not_called()
    assert snapshot.local_config.lora.region == (
        config_pb2.Config.LoRaConfig.RegionCode.US
    )
    assert snapshot.local_config.bluetooth.enabled is True
    assert snapshot.local_config.power.ls_secs == 300


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_preview_redacts_secret_fields_on_both_sides(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Secret-bearing paths render redacted on both new and current sides."""
    interface, node = _preview_interface()
    node.localConfig.security.SetInParent()
    node.localConfig.security.private_key = b"\xaa" * 32
    node.moduleConfig.mqtt.SetInParent()
    node.moduleConfig.mqtt.password = "old-passphrase"
    secret_hex = "0x" + "bb" * 32
    args = _set_args(
        [
            ["security.private_key", secret_hex],
            ["mqtt.password", "new-passphrase"],
        ]
    )

    snapshot = _preview_set_command(interface, args, {})

    out, err = capsys.readouterr()
    lines = out.splitlines()
    assert "Would set security.private_key = <redacted> (current: <redacted>)" in lines
    assert "Would set mqtt.password = <redacted> (current: <redacted>)" in lines
    assert "new-passphrase" not in out + err
    assert "old-passphrase" not in out + err
    assert secret_hex not in out + err
    assert snapshot.local_config.security.private_key == b"\xbb" * 32
    assert snapshot.module_config.mqtt.password == "new-passphrase"
    assert node.localConfig.security.private_key == b"\xaa" * 32
    assert node.moduleConfig.mqtt.password == "old-passphrase"


@pytest.mark.unit
def test_set_preview_leaf_helpers_fail_closed_for_stale_paths() -> None:
    """Stale roots/leaves render as absent instead of fabricating values."""
    config = localonly_pb2.LocalConfig()
    config.power.SetInParent()

    assert main_module._resolve_set_leaf(config, "missing.value") is None
    assert main_module._resolve_set_leaf(config, "power.missing") is None
    assert (
        main_module._render_preview_current_value(
            "missing.value", (config, localonly_pb2.LocalModuleConfig())
        )
        == "not set"
    )
    assert (
        main_module._render_preview_current_value(
            "power.missing", (config, localonly_pb2.LocalModuleConfig())
        )
        == "not set"
    )


@pytest.mark.unit
def test_set_preview_current_value_handles_scalar_root_presence() -> None:
    """Scalar top-level protobuf fields do not require message presence checks."""
    config = localonly_pb2.LocalConfig()
    config.version = 7

    parent, field = main_module._resolve_set_leaf(config, "version") or (None, None)

    assert parent is config
    assert field is not None and field.name == "version"
    assert (
        main_module._render_preview_current_value(
            "version", (config, localonly_pb2.LocalModuleConfig())
        )
        == "7"
    )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_preview_reports_not_set_for_unloaded_section(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Current values of unloaded sections render as ``not set`` after a read."""
    interface, node = _preview_interface()
    args = _set_args([["power.ls_secs", "300"]])

    snapshot = _preview_set_command(interface, args, {})

    out, err = capsys.readouterr()
    lines = out.splitlines()
    assert lines[0] == "Dry run: previewing --set batch without writing changes."
    assert "Would set power.ls_secs = 300 (current: not set)" in lines
    assert "Would write power configuration to device" in lines
    assert "Would use a configuration transaction" not in lines
    node.requestConfig.assert_called_once()
    assert node.requestConfig.call_args.args[0].name == "power"
    node.writeConfig.assert_not_called()
    assert snapshot.local_config.power.ls_secs == 300
    assert node.localConfig.power.ls_secs == 0


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_preview_set_waits_for_requested_section_before_rendering(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A requested section is awaited and its arrival backs the current value."""
    interface, node = _preview_interface()

    def _arrive(_probe: object, attrs: object) -> bool:
        del attrs
        node.localConfig.power.SetInParent()
        node.localConfig.power.ls_secs = 900
        return True

    node._timeout.waitForSet.side_effect = _arrive
    args = _set_args([["power.ls_secs", "300"]])

    _preview_set_command(interface, args, {})

    out, _err = capsys.readouterr()
    assert "Would set power.ls_secs = 300 (current: 900)" in out.splitlines()
    node.requestConfig.assert_called_once()
    assert node.requestConfig.call_args.args[0].name == "power"
    node._timeout.waitForSet.assert_called_once()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_preview_set_exits_when_requested_section_never_arrives(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A section that never arrives aborts the preview before rendering."""
    interface, node = _preview_interface()
    node._timeout.waitForSet.return_value = False
    args = _set_args([["power.ls_secs", "300"]])

    with pytest.raises(SystemExit) as exc_info:
        _preview_set_command(interface, args, {})

    assert exc_info.value.code == 1
    _out, err = capsys.readouterr()
    assert "timed out waiting for the power configuration" in err
    node.writeConfig.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_normal_set_exits_when_requested_section_never_arrives(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The write path refuses to proceed when a requested section never arrives."""
    interface, node = _preview_interface()
    node._timeout.waitForSet.return_value = False
    args = _set_args([["power.ls_secs", "300"]])

    with pytest.raises(SystemExit) as exc_info:
        _handle_set_command(interface, args, {})

    assert exc_info.value.code == 1
    _out, err = capsys.readouterr()
    assert "timed out waiting for the power configuration" in err
    node.writeConfig.assert_not_called()
    node.beginSettingsTransaction.assert_not_called()
    node.commitSettingsTransaction.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_preview_set_waits_once_for_all_requested_sections(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Multiple requested sections share one bounded wait, not one per section."""
    interface, node = _preview_interface()

    def _arrive(_probe: object, attrs: object) -> bool:
        del attrs
        node.localConfig.power.SetInParent()
        node.moduleConfig.external_notification.SetInParent()
        return True

    node._timeout.waitForSet.side_effect = _arrive
    args = _set_args(
        [["power.ls_secs", "300"], ["external_notification.enabled", "true"]]
    )

    _preview_set_command(interface, args, {})

    node.requestConfig.assert_called()
    assert node.requestConfig.call_count == 2
    node._timeout.waitForSet.assert_called_once()
    out, _err = capsys.readouterr()
    assert "Would set power.ls_secs = 300 (current: 0)" in out.splitlines()
    assert (
        "Would set external_notification.enabled = true (current: false)"
        in out.splitlines()
    )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_set_skips_section_wait_for_noproto_node(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``noProto`` nodes are never waited on because responses cannot arrive."""
    interface, node = _preview_interface()
    node.noProto = True
    args = _set_args([["power.ls_secs", "300"]])

    _preview_set_command(interface, args, {})

    node.requestConfig.assert_called_once()
    node._timeout.waitForSet.assert_not_called()
    out, _err = capsys.readouterr()
    assert "Would set power.ls_secs = 300 (current: not set)" in out.splitlines()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_set_skips_section_wait_when_already_loaded(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Loaded sections neither re-request nor wait before previewing."""
    interface, node = _preview_interface()
    node.localConfig.power.SetInParent()
    node.localConfig.power.ls_secs = 900
    args = _set_args([["power.ls_secs", "300"]])

    _preview_set_command(interface, args, {})

    node.requestConfig.assert_not_called()
    node._timeout.waitForSet.assert_not_called()
    out, _err = capsys.readouterr()
    assert "Would set power.ls_secs = 300 (current: 900)" in out.splitlines()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_preview_renders_repeated_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Repeated assignments render as plain lists on both preview sides."""
    interface, node = _preview_interface()
    node.localConfig.lora.SetInParent()
    node.localConfig.lora.ignore_incoming.append(123)
    args = _set_args([["lora.ignore_incoming", ["456", "789"]]])

    snapshot = _preview_set_command(interface, args, {})

    out, err = capsys.readouterr()
    assert (
        "Would set lora.ignore_incoming = [456, 789] (current: [123])"
        in out.splitlines()
    )
    assert list(snapshot.local_config.lora.ignore_incoming) == [456, 789]
    assert list(node.localConfig.lora.ignore_incoming) == [123]


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_preview_rejects_invalid_batch_without_success_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One invalid entry rejects the whole batch before any preview output."""
    interface, node = _preview_interface()
    args = _set_args(
        [
            ["power.ls_secs", "300"],
            ["lora.hop_limit", "not_a_number"],
        ]
    )

    with pytest.raises(SystemExit) as exc_info:
        _preview_set_command(interface, args, {})

    assert exc_info.value.code == 1
    out, err = capsys.readouterr()
    assert "Would set" not in out
    assert "Dry run:" not in out
    assert "expected integer" in out + err
    node.writeConfig.assert_not_called()
    node.beginSettingsTransaction.assert_not_called()
    node.commitSettingsTransaction.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_preview_rejects_unknown_field_without_success_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unknown fields print guidance and exit without previewing any entry."""
    interface, node = _preview_interface()
    args = _set_args([["power.nope", "1"]])

    with pytest.raises(SystemExit) as exc_info:
        _preview_set_command(interface, args, {})

    assert exc_info.value.code == 1
    out, err = capsys.readouterr()
    assert "Would set" not in out
    assert "Dry run:" not in out
    assert "Choices are..." in out
    node.writeConfig.assert_not_called()
    node.beginSettingsTransaction.assert_not_called()
    node.commitSettingsTransaction.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_preview_mutates_only_the_returned_copies() -> None:
    """Assignments land in the snapshot copies; live config and node stay intact."""
    interface, node = _preview_interface()
    node.localConfig.power.SetInParent()
    node.localConfig.power.ls_secs = 60
    args = _set_args(
        [
            ["power.ls_secs", "300"],
            ["external_notification.enabled", "true"],
        ]
    )

    snapshot = _preview_set_command(interface, args, {})

    assert isinstance(snapshot, ConfigSnapshotCopies)
    assert snapshot.local_config is not node.localConfig
    assert snapshot.module_config is not node.moduleConfig
    assert snapshot.local_config.power.ls_secs == 300
    assert snapshot.module_config.external_notification.enabled is True
    assert node.localConfig.power.ls_secs == 60
    assert node.moduleConfig.external_notification.enabled is False
    called_methods = {
        record[0] for record in node.mock_calls if "().__" not in record[0]
    }
    # waitForSet is the local section-arrival poll, not device traffic; dunder
    # records (mock truthiness bookkeeping) are filtered out above.
    assert called_methods <= {"requestConfig", "_timeout.waitForSet"}
    node.writeConfig.assert_not_called()
    node.beginSettingsTransaction.assert_not_called()
    node.commitSettingsTransaction.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_preview_set_fails_closed_if_target_disappears_after_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-preflight target-resolution divergence aborts the preview."""
    interface, _node = _preview_interface()
    args = _set_args([["power.ls_secs", "300"]])
    monkeypatch.setattr(main_module, "_ensure_set_sections_loaded", lambda *_a: None)
    monkeypatch.setattr(
        main_module, "_validate_set_entries_against_configs", lambda *_a, **_kw: True
    )
    monkeypatch.setattr(main_module, "_resolve_set_target", lambda *_a: None)

    with pytest.raises(SystemExit) as exc_info:
        main_module._preview_set_command(interface, args, {})

    assert exc_info.value.code == 1


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_preview_set_fails_closed_if_leaf_disappears_after_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-preflight leaf-resolution divergence aborts the preview."""
    interface, node = _preview_interface()
    node.localConfig.power.SetInParent()
    args = _set_args([["power.ls_secs", "300"]])
    monkeypatch.setattr(main_module, "_ensure_set_sections_loaded", lambda *_a: None)
    monkeypatch.setattr(
        main_module, "_validate_set_entries_against_configs", lambda *_a, **_kw: True
    )
    monkeypatch.setattr(main_module, "_resolve_set_leaf", lambda *_a: None)

    with pytest.raises(SystemExit) as exc_info:
        main_module._preview_set_command(interface, args, {})

    assert exc_info.value.code == 1


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_normal_set_command_still_writes_multi_section_batch_with_transaction(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The non-preview path keeps writing multi-section batches transactionally."""
    interface, node = _preview_interface()
    node.localConfig.power.SetInParent()
    node.moduleConfig.external_notification.SetInParent()
    args = _set_args(
        [
            ["power.ls_secs", "300"],
            ["external_notification.enabled", "true"],
        ]
    )

    _handle_set_command(interface, args, {})

    out, err = capsys.readouterr()
    assert "Would set" not in out
    assert "Dry run:" not in out
    assert "Writing modified preferences to device" in out
    assert "Using a configuration transaction" in out
    assert "Writing power configuration to device" in out
    assert "Writing external_notification configuration to device" in out
    node.beginSettingsTransaction.assert_called_once_with()
    node.commitSettingsTransaction.assert_called_once_with()
    written = sorted(call.args[0] for call in node.writeConfig.call_args_list)
    assert written == ["external_notification", "power"]
    method_order = [record[0] for record in node.mock_calls]
    assert method_order[0] == "beginSettingsTransaction"
    assert method_order[-1] == "commitSettingsTransaction"
    assert node.localConfig.power.ls_secs == 300
    assert node.moduleConfig.external_notification.enabled is True


@pytest.mark.unit
def test_renderer_redacts_secret_leaves_inside_nested_messages() -> None:
    """Nested message rendering redacts secret-named leaves of the message."""
    settings = channel_pb2.ChannelSettings()
    settings.psk = b"\x01\x02\x03\x04"
    settings.name = "upstairs"

    rendered = render_preview_value("channels.example", None, settings)

    assert rendered.startswith("{")
    assert "psk: <redacted>" in rendered
    assert "name: upstairs" in rendered


@pytest.mark.unit
def test_renderer_reports_nonsecret_bytes_by_length() -> None:
    """Non-secret bytes render as a length, never raw binary content."""
    assert render_preview_value("telemetry.payload", None, b"\x00\xff") == "<2 bytes>"


@pytest.mark.unit
def test_renderer_preserves_parent_path_for_nested_path_secrets() -> None:
    """Nested rendering redacts secrets classified by their canonical parent path."""
    mqtt = module_config_pb2.ModuleConfig.MQTTConfig()
    mqtt.enabled = True
    mqtt.username = "preview-user-secret"
    mqtt.password = "preview-password-secret"

    rendered = render_preview_value("mqtt", None, mqtt)

    assert "username: <redacted>" in rendered
    assert "password: <redacted>" in rendered
    assert "preview-user-secret" not in rendered
    assert "preview-password-secret" not in rendered


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_preview_preflight_reports_secret_without_raw_value(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    """A rejected secret assignment never reports its raw value anywhere."""
    interface, node = _preview_interface()
    node.localConfig.security.SetInParent()
    secret = "definitely-not-hex-!!"
    args = _set_args([["security.private_key", secret]])

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(SystemExit) as exc_info:
            _preview_set_command(interface, args, {})

    assert exc_info.value.code == 1
    out, err = capsys.readouterr()
    combined = out + err + caplog.text
    assert secret not in combined
    assert "<redacted>" in out + err
    assert "Would set" not in out
    assert "Dry run:" not in out


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_preview_resolves_remote_destination_read_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A remote ``--dest`` preview reads that node and stays copy-only."""
    interface, node = _preview_interface()
    node.localConfig.power.SetInParent()
    node.localConfig.power.ls_secs = 60
    args = _set_args([["power.ls_secs", "600"]], dest="!deadbeef")

    snapshot = _preview_set_command(interface, args, {})

    interface.getNode.assert_called_once_with("!deadbeef", False)
    out, _err = capsys.readouterr()
    assert "Would set power.ls_secs = 600 (current: 60)" in out.splitlines()
    assert snapshot.local_config.power.ls_secs == 600
    assert node.localConfig.power.ls_secs == 60
    node.writeConfig.assert_not_called()
    node.beginSettingsTransaction.assert_not_called()
    node.commitSettingsTransaction.assert_not_called()
