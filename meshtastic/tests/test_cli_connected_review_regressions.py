"""Regression coverage for connected-action ownership and failure boundaries."""

from __future__ import annotations

import argparse
import threading
from types import SimpleNamespace
from typing import NoReturn
from unittest.mock import MagicMock

import pytest

from meshtastic.cli import (
    channel_contact_actions,
    configure_actions,
    dispatch,
    messaging_service_actions,
    preference_runtime,
    set_actions,
)
from meshtastic.cli.invocation import CliInvocation, activate_invocation
from meshtastic.configure_verify import (
    ConfigureReconnectResult,
    LocalApplyStatus,
    LocalApplyVerification,
)
from meshtastic.errors import RequestRejectedError
from meshtastic.protobuf import channel_pb2, localonly_pb2

pytestmark = pytest.mark.unit


def _exit(message: str, return_value: int = 1) -> NoReturn:
    raise RuntimeError(message, return_value)


def _node() -> MagicMock:
    node = MagicMock()
    node.localConfig = localonly_pb2.LocalConfig()
    node.moduleConfig = localonly_pb2.LocalModuleConfig()
    node.localConfig.bluetooth.fixed_pin = 123456
    node.localConfig.display.use_12h_clock = True
    node.localConfig.lora.hop_limit = 2
    node.localConfig.power.ls_secs = 10
    node.noProto = False
    return node


@pytest.mark.parametrize(
    "path", ["bluetooth.bluetooth", "bluetooth.enabled.extra", "power.ls_secs.extra"]
)
def test_invalid_get_path_does_not_read_or_print_values(path: str) -> None:
    node = _node()
    output: list[str] = []
    assert not preference_runtime.get_pref(
        node, path, allow_secrets=True, cli_print=output.append
    )
    assert "123456" not in "\n".join(output)
    node.requestConfig.assert_not_called()


def test_section_read_honors_naming_mode() -> None:
    node = _node()
    output: list[str] = []
    with activate_invocation(
        CliInvocation(argparse.Namespace(), argparse.ArgumentParser(), camel_case=True)
    ):
        assert preference_runtime.get_pref(node, "display", cli_print=output.append)
    assert any(line.startswith("display.use12HClock:") for line in output)


def test_channel_check_never_logs_psk(caplog: pytest.LogCaptureFixture) -> None:
    channel = channel_pb2.Channel(index=0, role=channel_pb2.Channel.PRIMARY)
    channel.settings.psk = b"secret-psk-sentinel"
    node = SimpleNamespace(getChannelByChannelIndex=lambda _index: channel)
    with caplog.at_level("DEBUG", logger=channel_contact_actions.logger.name):
        assert channel_contact_actions.check_channel(SimpleNamespace(localNode=node), 0)
    assert "secret-psk-sentinel" not in caplog.text
    assert "Channel 0 enabled: True" in caplog.text


@pytest.mark.parametrize("option", ["--show-fields", "--sort"])
def test_node_query_validation_fails_closed_when_exit_returns(option: str) -> None:
    """Fatal node-query errors must stop dispatch with a returning exit seam."""
    interface = SimpleNamespace(nodesByNum={})
    cli_exit = MagicMock()
    field = "definitely_not_a_node_field"
    with pytest.raises(AssertionError):
        if option == "--show-fields":
            messaging_service_actions._validate_cli_show_fields(
                interface, [field], cli_exit=cli_exit
            )
        else:
            messaging_service_actions._validate_cli_sort_field(
                interface, field, cli_exit=cli_exit
            )
    assert cli_exit.call_count == 1
    assert option in cli_exit.call_args.args[0]
    assert cli_exit.call_args.args[1] == 1


def _interface(node: MagicMock) -> MagicMock:
    interface = MagicMock()
    interface.getNode.return_value = node
    interface.localNode = node
    interface.configId = 1
    interface.isConnected = threading.Event()
    interface.isConnected.set()
    return interface


def _set_args() -> SimpleNamespace:
    return SimpleNamespace(
        dest="^local", set=[["lora.hop_limit", "3"], ["power.ls_secs", "20"]]
    )


def test_expected_values_are_detached_before_device_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = _node()
    interface = _interface(node)
    verify = MagicMock()
    monkeypatch.setattr(set_actions, "_verify_local_set_apply", verify)
    node.commitSettingsTransaction.side_effect = lambda: setattr(
        node.localConfig.lora, "hop_limit", 2
    )
    sleeps: list[float] = []
    set_actions._handle_set_command(
        interface,
        _set_args(),
        {},
        cli_exit=_exit,
        cli_print=lambda *_a, **_k: None,
        sleep=sleeps.append,
    )
    assert verify.call_args.kwargs["config_fields"] == {
        "lora": {"hop_limit": 3},
        "power": {"ls_secs": 20},
    }
    assert node.localConfig.lora.hop_limit == 2
    assert len(sleeps) == 1


def test_partial_transaction_failure_attempts_close_and_preserves_error() -> None:
    node = _node()
    error = OSError("second write failed")
    node.writeConfig.side_effect = [None, error]
    interface = _interface(node)
    output = MagicMock()
    with pytest.raises(OSError) as caught:
        set_actions._handle_set_command(
            interface,
            _set_args(),
            {},
            cli_exit=_exit,
            cli_print=output,
            sleep=lambda _seconds: None,
        )
    assert caught.value is error
    assert node.writeConfig.call_count == 2
    node.beginSettingsTransaction.assert_called_once_with()
    node.commitSettingsTransaction.assert_called_once_with()
    assert "Any writes already accepted may be committed" in str(output.call_args_list)


def test_transaction_cleanup_survives_reporter_failure() -> None:
    node = _node()
    reporter = MagicMock(side_effect=RuntimeError("sink failed"))
    configure_actions._close_failed_settings_transaction(
        node, commit_attempted=False, cli_print=reporter
    )
    node.commitSettingsTransaction.assert_called_once_with()


def test_secret_preflight_error_and_returning_exit_fail_closed() -> None:
    node = _node()
    exit_callback = MagicMock()
    output = MagicMock()
    with pytest.raises(AssertionError, match="cli_exit returned"):
        set_actions._preflight_set_entries(
            node,
            [("bluetooth.fixed_pin", "secret-sentinel")],
            cli_exit=exit_callback,
            cli_print=output,
        )
    assert "secret-sentinel" not in str(exit_callback.call_args_list)
    assert "secret-sentinel" not in str(output.call_args_list)
    assert node.localConfig.bluetooth.fixed_pin == 123456


@pytest.mark.parametrize(
    "failure",
    [
        OSError("radio rebooted"),
        LocalApplyVerification(
            LocalApplyStatus.RELOAD_FAILED, missing_sections=("power",)
        ),
    ],
)
def test_readback_send_failure_or_missing_section_attempts_reconnect(
    monkeypatch: pytest.MonkeyPatch, failure: Exception | LocalApplyVerification
) -> None:
    node = _node()
    interface = _interface(node)
    interface.isConnected.clear()
    readback = MagicMock(
        side_effect=failure if isinstance(failure, Exception) else None,
        return_value=failure,
    )
    reconnect = MagicMock(return_value=ConfigureReconnectResult.VERIFIED)
    monkeypatch.setattr(set_actions, "verify_local_config_apply", readback)
    monkeypatch.setattr(
        configure_actions, "_post_configure_reconnect_and_verify", reconnect
    )
    set_actions._verify_local_set_apply(
        interface,
        node,
        node_dest="^local",
        pre_write_config_id=1,
        config_fields={"lora": {"region": 1}, "power": {"ls_secs": 20}},
        module_config_fields={},
        cli_exit=_exit,
        cli_print=MagicMock(),
    )
    reconnect.assert_called_once()


def test_region_only_warning_is_forced_when_reload_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = _node()
    interface = _interface(node)
    monkeypatch.setattr(
        set_actions,
        "verify_local_config_apply",
        MagicMock(
            return_value=LocalApplyVerification(
                LocalApplyStatus.RELOAD_FAILED, missing_sections=("lora",)
            )
        ),
    )
    report = MagicMock()
    set_actions._verify_local_set_apply(
        interface,
        node,
        node_dest="^local",
        pre_write_config_id=1,
        config_fields={"lora": {"region": 1}},
        module_config_fields={},
        cli_exit=_exit,
        cli_print=report,
    )
    assert "sent, not verified" in report.call_args.args[0]
    assert report.call_args.kwargs == {"force": True}


def test_dispatch_forwards_sleep_to_both_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delay = MagicMock()
    set_handler = MagicMock()
    monkeypatch.setattr(set_actions, "_handle_set_command", set_handler)
    hooks = dispatch._build_connected_dispatch_hooks(
        cli_exit=_exit,
        cli_print=MagicMock(),
        powermon_available=lambda: False,
        powermon_error=lambda: None,
        get_meter=lambda: None,
        log_set_factory=None,
        power_stress_factory=None,
        sleep=delay,
    )
    assert hooks.services.sleep is delay
    hooks.configure.handle_set_command(MagicMock(), argparse.Namespace(), {})
    assert set_handler.call_args.kwargs["sleep"] is delay


def test_readback_routing_rejection_remains_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = _node()
    interface = _interface(node)
    interface.isConnected.clear()
    monkeypatch.setattr(
        set_actions,
        "verify_local_config_apply",
        MagicMock(side_effect=RequestRejectedError("NOT_AUTHORIZED")),
    )
    reconnect = MagicMock()
    monkeypatch.setattr(
        configure_actions, "_post_configure_reconnect_and_verify", reconnect
    )
    with pytest.raises(RuntimeError, match="could not read fresh device state"):
        set_actions._verify_local_set_apply(
            interface,
            node,
            node_dest="^local",
            pre_write_config_id=1,
            config_fields={"power": {"ls_secs": 20}},
            module_config_fields={},
            cli_exit=_exit,
            cli_print=MagicMock(),
        )
    reconnect.assert_not_called()
