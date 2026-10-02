"""Consumer-seam regression tests for local ``--configure`` CLI outcomes.

The real ``_handle_configure_command``/``_report_configure_result`` handlers
run through the real ``main()`` entry point; the device-behavior seam
(``post_configure_reconnect_and_verify``) is injected through the existing
``ConfigureHooks`` wiring so each reconnect-verification outcome can be
driven deterministically.
"""

# pylint: disable=W0613

from __future__ import annotations

import sys
import time as time_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, create_autospec, patch

import pytest
import yaml

import meshtastic.__main__ as main_module
from meshtastic import mt_config
from meshtastic.__main__ import main
from meshtastic.cli import configure_actions
from meshtastic.configure_verify import ConfigureReconnectResult
from meshtastic.node import Node
from meshtastic.protobuf import admin_pb2, localonly_pb2
from meshtastic.tests._main_legacy_support import _build_configure_interface
from meshtastic.util import Timeout


def _patch_configure_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove real sleeping from the configure runtime's settle and pacing."""
    monkeypatch.setattr(
        configure_actions,
        "time",
        SimpleNamespace(monotonic=time_module.monotonic, sleep=lambda _s: None),
        raising=True,
    )


def _run_main_argv(
    argv: list[str],
    iface: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run ``main()`` with the supplied argv against the supplied interface."""
    _patch_configure_clock(monkeypatch)
    time_attrs = vars(main_module.time).copy()
    time_attrs["sleep"] = lambda _seconds: None
    with monkeypatch.context() as apply_patch:
        apply_patch.setattr(sys, "argv", ["", *argv])
        apply_patch.setattr(mt_config, "args", cast(Any, ["", *argv]))
        apply_patch.setattr(
            main_module, "time", SimpleNamespace(**time_attrs), raising=True
        )
        with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
            main()


def _inject_reconnect_result(
    monkeypatch: pytest.MonkeyPatch,
    result: Any,
) -> MagicMock:
    """Drive the reconnect-verification seam through the ConfigureHooks wiring."""
    hook = MagicMock(return_value=result)
    monkeypatch.setattr(main_module, "_post_configure_reconnect_and_verify", hook)
    return hook


def _write_document(tmp_path: Path, document: dict[str, Any]) -> Path:
    """Write one configure YAML document to a temporary path."""
    config_path = tmp_path / "outcome.yaml"
    config_path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return config_path


@pytest.fixture(autouse=True)
def _mock_newer_version_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent external network calls during unit tests in this module."""
    monkeypatch.setattr("meshtastic.util.check_if_newer_version", lambda: None)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_local_configure_reconnect_failed_exits_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A local configure whose device never reconnects must exit nonzero."""
    config_path = _write_document(tmp_path, {"config": {"power": {"ls_secs": 222}}})
    iface, _node = _build_configure_interface()
    hook = _inject_reconnect_result(
        monkeypatch, ConfigureReconnectResult.RECONNECT_FAILED
    )

    with pytest.raises(SystemExit) as exit_info:
        _run_main_argv(["--configure", str(config_path)], iface, monkeypatch)

    out, err = capsys.readouterr()
    captured = out + err
    assert exit_info.value.code == 1
    hook.assert_called_once()
    assert "did not reconnect" in captured
    assert "device did not reconnect within the timeout" in captured


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_local_configure_reload_failed_exits_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A local configure whose config reload fails must exit nonzero."""
    config_path = _write_document(tmp_path, {"config": {"power": {"ls_secs": 222}}})
    iface, _node = _build_configure_interface()
    _inject_reconnect_result(monkeypatch, ConfigureReconnectResult.CONFIG_RELOAD_FAILED)

    with pytest.raises(SystemExit) as exit_info:
        _run_main_argv(["--configure", str(config_path)], iface, monkeypatch)

    out, err = capsys.readouterr()
    captured = out + err
    assert exit_info.value.code == 1
    assert "did not reload" in captured
    assert "configuration reload failed" in captured


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_local_configure_verification_incomplete_exits_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A local configure with unconfirmed settings must exit nonzero."""
    config_path = _write_document(tmp_path, {"config": {"power": {"ls_secs": 222}}})
    iface, _node = _build_configure_interface()
    _inject_reconnect_result(
        monkeypatch, ConfigureReconnectResult.VERIFICATION_INCOMPLETE
    )

    with pytest.raises(SystemExit) as exit_info:
        _run_main_argv(["--configure", str(config_path)], iface, monkeypatch)

    out, err = capsys.readouterr()
    captured = out + err
    assert exit_info.value.code == 1
    assert "did not confirm" in captured
    assert "not all requested settings could be verified" in captured


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_local_configure_unrecognized_result_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unrecognized verification result must never report success."""
    config_path = _write_document(tmp_path, {"config": {"power": {"ls_secs": 222}}})
    iface, _node = _build_configure_interface()
    _inject_reconnect_result(monkeypatch, "future-result")

    with pytest.raises(SystemExit) as exit_info:
        _run_main_argv(["--configure", str(config_path)], iface, monkeypatch)

    out, err = capsys.readouterr()
    captured = out + err
    assert exit_info.value.code == 1
    assert "could not be completed" in captured
    assert "future-result" in captured


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_local_configure_verified_exits_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A verified local configure stays a zero-exit success."""
    config_path = _write_document(tmp_path, {"config": {"power": {"ls_secs": 222}}})
    iface, _node = _build_configure_interface()
    hook = _inject_reconnect_result(monkeypatch, ConfigureReconnectResult.VERIFIED)

    _run_main_argv(["--configure", str(config_path)], iface, monkeypatch)

    out, _err = capsys.readouterr()
    hook.assert_called_once()
    assert "all requested settings were verified" in out


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_remote_configure_transaction_stays_informational(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Remote transaction outcomes keep the historical informational path."""
    config_path = _write_document(tmp_path, {"config": {"power": {"ls_secs": 222}}})
    iface, target_node = _build_configure_interface()
    remote_node = create_autospec(Node, instance=True)
    remote_node.noProto = True
    remote_node.localConfig = localonly_pb2.LocalConfig()
    remote_node.localConfig.power.ls_secs = 100
    remote_node.moduleConfig = localonly_pb2.LocalModuleConfig()
    remote_node.beginSettingsTransaction = MagicMock()
    remote_node.commitSettingsTransaction = MagicMock()
    remote_node.writeConfig = MagicMock()
    remote_node.requestChannels = MagicMock()
    iface.getNode.return_value = remote_node
    hook = _inject_reconnect_result(
        monkeypatch, ConfigureReconnectResult.RECONNECT_FAILED
    )

    _run_main_argv(
        ["--configure", str(config_path), "--dest", "!98765432"],
        iface,
        monkeypatch,
    )

    out, _err = capsys.readouterr()
    hook.assert_not_called()
    remote_node.beginSettingsTransaction.assert_called_once()
    assert "Post-reconnect verification skipped for remote target" in out


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_direct_only_configure_stays_informational(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Non-transaction configure runs keep their historical success output."""
    config_path = _write_document(tmp_path, {"owner": "Outcome Check"})
    iface, _node = _build_configure_interface()
    hook = _inject_reconnect_result(
        monkeypatch, ConfigureReconnectResult.RECONNECT_FAILED
    )

    _run_main_argv(["--configure", str(config_path)], iface, monkeypatch)

    out, _err = capsys.readouterr()
    hook.assert_not_called()
    assert "Configuration applied (no reboot expected)." in out


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_combined_dry_run_issues_no_writes_or_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Combined --set + --configure --dry-run never writes or verifies."""
    config_path = _write_document(tmp_path, {"config": {"power": {"ls_secs": 333}}})
    device_local = localonly_pb2.LocalConfig()
    device_local.power.ls_secs = 100
    iface, node = _build_configure_interface(device_local)
    hook = _inject_reconnect_result(monkeypatch, ConfigureReconnectResult.VERIFIED)

    _run_main_argv(
        ["--set", "power.ls_secs", "222", "--configure", str(config_path), "--dry-run"],
        iface,
        monkeypatch,
    )

    out, _err = capsys.readouterr()
    node.writeConfig.assert_not_called()
    node.requestConfig.assert_not_called()
    node.beginSettingsTransaction.assert_not_called()
    node.commitSettingsTransaction.assert_not_called()
    hook.assert_not_called()
    assert "Dry run" in out


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_combined_set_then_configure_verifies_each_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Set-stage verification uses fresh reads; configure verifies the document."""
    config_path = _write_document(tmp_path, {"config": {"power": {"ls_secs": 333}}})
    device_local = localonly_pb2.LocalConfig()
    device_local.power.ls_secs = 100
    device_truth = localonly_pb2.LocalConfig()
    device_truth.CopyFrom(device_local)
    iface, node = _build_configure_interface(device_local)
    node.noProto = False
    node.iface = iface
    node._timeout = Timeout(maxSecs=30.0)
    calls: list[str] = []

    original_write_config = node.writeConfig

    def _record_write(config_name: str) -> None:
        calls.append(f"writeConfig:{config_name}")
        section_field = device_truth.DESCRIPTOR.fields_by_name.get(config_name)
        if section_field is not None and node.localConfig.HasField(config_name):
            getattr(device_truth, config_name).CopyFrom(
                getattr(node.localConfig, config_name)
            )
        return original_write_config(config_name)

    node.writeConfig = MagicMock(side_effect=_record_write)

    def _device_send_admin(message: Any, **kwargs: Any) -> None:
        """Model the device answering the set-stage readback from device truth."""
        variant = message.WhichOneof("payload_variant")
        if variant != "get_config_request":
            return
        config_name = admin_pb2.AdminMessage.ConfigType.Name(message.get_config_request)
        section = config_name.removesuffix("_CONFIG").lower()
        calls.append(f"readback:{section}")
        node.localConfig.ClearField(section)
        if cast(Any, device_truth).HasField(section):
            getattr(node.localConfig, section).CopyFrom(getattr(device_truth, section))
            response = admin_pb2.AdminMessage()
            getattr(response.get_config_response, section).CopyFrom(
                getattr(device_truth, section)
            )
            kwargs["onResponse"]({"decoded": {"admin": {"raw": response}}})

    node._send_admin = MagicMock(side_effect=_device_send_admin)
    original_begin = node.beginSettingsTransaction

    def _record_begin() -> None:
        calls.append("beginSettingsTransaction")
        original_begin()

    node.beginSettingsTransaction = MagicMock(side_effect=_record_begin)
    original_commit = node.commitSettingsTransaction

    def _record_commit() -> None:
        calls.append("commitSettingsTransaction")
        original_commit()

    node.commitSettingsTransaction = MagicMock(side_effect=_record_commit)
    _patch_seam_clock(monkeypatch)
    hook = _inject_reconnect_result(monkeypatch, ConfigureReconnectResult.VERIFIED)

    _run_main_argv(
        ["--set", "power.ls_secs", "222", "--configure", str(config_path)],
        iface,
        monkeypatch,
    )

    out, err = capsys.readouterr()
    captured = out + err
    # Exactly one fresh device readback for the set stage, before the
    # configure transaction opens.
    assert calls.count("readback:power") == 1
    assert calls.index("readback:power") < calls.index("beginSettingsTransaction")
    hook.assert_called_once()
    assert hook.call_args.kwargs["verify_config_fields"] == {"power": {"ls_secs": 333}}
    assert "Verified: fresh device state matches the requested --set values." in out
    assert "all requested settings were verified" in captured


def _patch_seam_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Advance the verification seam's clock so bounded waits cannot stall."""
    clock = {"now": 100.0}

    def _fast_monotonic() -> float:
        clock["now"] += 5.0
        return clock["now"]

    monkeypatch.setattr(
        "meshtastic.configure_verify.time",
        SimpleNamespace(
            monotonic=_fast_monotonic,
            sleep=lambda _seconds: None,
            time=time_module.time,
        ),
        raising=True,
    )
