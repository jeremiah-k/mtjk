"""Focused behavioral tests for connected messaging/service CLI actions."""

from __future__ import annotations

import argparse
from typing import Any, NoReturn
from unittest.mock import MagicMock, create_autospec

import pytest

from meshtastic.cli import messaging_service_actions as actions
from meshtastic.cli.context import ActionOutcome, CliContext
from meshtastic.cli.messaging_service_actions import (
    MessagingServiceHooks,
    _handle_content_reads,
    _handle_information_actions,
    _handle_long_running_services,
    _handle_messaging_actions,
)
from meshtastic.mesh_interface import MeshInterface
from meshtastic.protobuf import portnums_pb2


def _args(**overrides: Any) -> argparse.Namespace:
    """Build parser-faithful defaults for messaging/service action tests."""
    values: dict[str, Any] = {
        "sendtext": None,
        "private": False,
        "dest": "!00000002",
        "traceroute": None,
        "request_telemetry": None,
        "request_position": False,
        "gpio_wrb": None,
        "gpio_rd": None,
        "gpio_watch": None,
        "get_canned_message": False,
        "get_ringtone": False,
        "info": False,
        "get": None,
        "nodes": False,
        "show_fields": None,
        "role": None,
        "hwmodel": None,
        "sort": None,
        "limit": None,
        "slog": None,
        "power_stress": False,
        "listen": False,
        "tunnel": False,
        "tunnel_net": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _interface_double() -> MagicMock:
    """Return an interface double constrained to the MeshInterface method surface."""
    return create_autospec(MeshInterface, instance=True)


def _context(interface: Any, **arg_overrides: Any) -> CliContext:
    """Build a connected action context around a test interface double."""
    return CliContext(
        interface=interface,
        args=_args(**arg_overrides),
        get_node_kwargs={"timeout": 5.0},
        outcome=ActionOutcome(),
    )


def _cli_exit(_message: str, return_value: int = 1) -> NoReturn:
    """Raise ``SystemExit`` in place of the production CLI-exit hook."""
    raise SystemExit(return_value)


def _hooks(**overrides: Any) -> MessagingServiceHooks:
    """Build messaging-service hooks with deterministic defaults."""
    values: dict[str, Any] = {
        "cli_exit": _cli_exit,
        "cli_print": MagicMock(),
        "get_channel_index": lambda: 2,
        "check_channel": MagicMock(return_value=True),
        "remote_hardware_client": MagicMock(),
        "get_pref": MagicMock(return_value=True),
        "validate_cli_show_fields": MagicMock(),
        "validate_cli_sort_field": MagicMock(),
        "newer_version": MagicMock(return_value=None),
        "install_upgrade_hint": "pipx upgrade mtjk",
        "powermon_available": MagicMock(return_value=True),
        "powermon_error": MagicMock(return_value=None),
        "log_set_factory": MagicMock(),
        "power_stress_factory": MagicMock(),
        "get_meter": MagicMock(return_value=object()),
        "platform_system": MagicMock(return_value="Linux"),
    }
    values.update(overrides)
    return MessagingServiceHooks(**values)


@pytest.mark.unit
def test_messaging_actions_send_traceroute_with_selected_channel() -> None:
    """Traceroute should use the selected channel and local hop limit."""
    interface = _interface_double()
    interface.localNode = MagicMock()
    interface.localNode.localConfig.lora.hop_limit = 5
    context = _context(interface, traceroute="!00000003")
    hooks = _hooks()

    _handle_messaging_actions(context, hooks)

    interface.sendTraceRoute.assert_called_once_with("!00000003", 5, channelIndex=2)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("device_metrics", "device_metrics"),
        ("environment", "environment_metrics"),
        ("environment_metrics", "environment_metrics"),
        ("air_quality", "air_quality_metrics"),
        ("air_quality_metrics", "air_quality_metrics"),
        ("power_metrics", "power_metrics"),
        ("local_stats", "local_stats"),
        ("unknown", "device_metrics"),
    ],
)
def test_messaging_actions_maps_telemetry_types(requested: str, expected: str) -> None:
    """Telemetry request aliases should map to the historical metric names."""
    interface = _interface_double()
    context = _context(interface, request_telemetry=requested)
    hooks = _hooks()

    _handle_messaging_actions(context, hooks)

    interface.sendTelemetry.assert_called_once_with(
        destinationId="!00000002",
        wantResponse=True,
        channelIndex=2,
        telemetryType=expected,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("private", "expected_port"),
    [
        (False, portnums_pb2.PortNum.TEXT_MESSAGE_APP),
        (True, portnums_pb2.PortNum.PRIVATE_APP),
    ],
)
def test_sendtext_selects_port_for_private_flag(
    private: bool, expected_port: int
) -> None:
    """Private text sends must use the private application port."""
    interface = _interface_double()
    context = _context(interface, sendtext="hello", private=private)

    _handle_messaging_actions(context, _hooks())

    assert interface.sendText.call_args.kwargs["portNum"] == expected_port


@pytest.mark.unit
@pytest.mark.parametrize(
    ("overrides", "method_name"),
    [
        ({"request_telemetry": "device"}, "sendTelemetry"),
        ({"request_position": True}, "sendPosition"),
    ],
)
def test_messaging_actions_reject_broadcast_response_requests(
    overrides: dict[str, Any], method_name: str
) -> None:
    """Telemetry and position response requests require a concrete destination."""
    interface = _interface_double()
    context = _context(interface, dest="^all", **overrides)
    hooks = _hooks()

    with pytest.raises(SystemExit):
        _handle_messaging_actions(context, hooks)

    getattr(interface, method_name).assert_not_called()


@pytest.mark.unit
def test_gpio_actions_reject_broadcast_destination_before_client_creation() -> None:
    """GPIO response operations require a concrete node before creating a client."""
    interface = _interface_double()
    context = _context(interface, dest="^all", gpio_rd="ff")
    remote_hardware_client = MagicMock()
    hooks = _hooks(remote_hardware_client=remote_hardware_client)

    with pytest.raises(SystemExit):
        _handle_messaging_actions(context, hooks)

    remote_hardware_client.assert_not_called()


@pytest.mark.unit
def test_messaging_actions_writes_gpio_bitmask() -> None:
    """GPIO writes should combine bit/value pairs before sending one request."""
    interface = _interface_double()
    client = MagicMock()
    context = _context(interface, gpio_wrb=[("4", "1"), ("7", "1")])
    hooks = _hooks(remote_hardware_client=MagicMock(return_value=client))

    _handle_messaging_actions(context, hooks)

    client.writeGPIOs.assert_called_once_with("!00000002", 0x90, 0x90)
    assert context.outcome.close_now is True


@pytest.mark.unit
def test_information_actions_remote_nodes_stops_dispatch() -> None:
    """Remote node-list requests should retain the historical early-return behavior."""
    interface = _interface_double()
    context = _context(interface, nodes=True)
    cli_print = MagicMock()
    hooks = _hooks(cli_print=cli_print)

    _handle_information_actions(context, hooks)

    assert context.outcome.stop_processing is True
    interface.showNodes.assert_not_called()
    cli_print.assert_called_once_with(
        "Showing node list of a remote node is not supported."
    )


@pytest.mark.unit
def test_information_actions_validates_selected_node_fields() -> None:
    """Local node listing should validate requested fields before rendering."""
    interface = _interface_double()
    context = _context(interface, dest="^all", nodes=True, show_fields=["user.id"])
    validate = MagicMock()
    hooks = _hooks(validate_cli_show_fields=validate)

    _handle_information_actions(context, hooks)

    validate.assert_called_once_with(interface, ["user.id"])
    interface.showNodes.assert_called_once_with(
        True,
        ["user.id"],
        roleFilter=None,
        hwModelFilter=None,
        sortField=None,
        sortDirection=None,
        limit=0,
    )


@pytest.mark.unit
def test_long_running_services_registers_log_cleanup_and_runs_stress() -> None:
    """Register slog failure rollback while power stress still requests closure."""
    interface = _interface_double()
    log_set = MagicMock()
    stress = MagicMock()
    context = _context(interface, slog="default", power_stress=True)
    hooks = _hooks(
        log_set_factory=MagicMock(return_value=log_set),
        power_stress_factory=MagicMock(return_value=stress),
    )

    _handle_long_running_services(context, hooks)

    stress.run.assert_called_once_with()
    log_set.close.assert_called_once_with()
    assert context.outcome.close_now is True
    assert context.outcome.failure_cleanup_callbacks == []


@pytest.mark.unit
def test_power_stress_failure_retains_slog_failure_cleanup() -> None:
    """A failed stress run must leave the log rollback armed for dispatch cleanup."""
    interface = _interface_double()
    log_set = MagicMock()
    stress = MagicMock()
    stress.run.side_effect = RuntimeError("stress failed")
    context = _context(interface, slog="default", power_stress=True)
    hooks = _hooks(
        log_set_factory=MagicMock(return_value=log_set),
        power_stress_factory=MagicMock(return_value=stress),
    )

    with pytest.raises(RuntimeError, match="stress failed"):
        _handle_long_running_services(context, hooks)

    log_set.close.assert_not_called()
    assert context.outcome.failure_cleanup_callbacks == [log_set.close]


@pytest.mark.unit
def test_long_running_services_listen_overrides_close_request() -> None:
    """Listen mode should keep the interface open after earlier close requests."""
    interface = _interface_double()
    context = _context(interface, listen=True)
    context.outcome.close_now = True

    _handle_long_running_services(context, _hooks())

    assert context.outcome.close_now is False


@pytest.mark.unit
def test_rejected_no_proto_tunnel_preserves_prior_close_request() -> None:
    """A tunnel that cannot start must not undo an earlier one-shot close request."""
    interface = _interface_double()
    interface.noProto = True
    context = _context(interface, tunnel=True, dest="^all")
    context.outcome.close_now = True

    _handle_long_running_services(context, _hooks())

    assert context.outcome.close_now is True


@pytest.mark.unit
def test_sendtext_requests_protocol_ack_without_owning_final_wait() -> None:
    """Text sends request an ACK while final blocking waits remain dispatch-owned."""
    interface = _interface_double()
    node = MagicMock()
    interface.getNode.return_value = node
    context = _context(interface, sendtext="hello")

    _handle_messaging_actions(context, _hooks())

    assert context.outcome.close_now is True
    interface.sendText.assert_called_once()
    assert interface.sendText.call_args.kwargs["wantAck"] is True


@pytest.mark.unit
def test_gpio_read_rejects_invalid_hex_mask() -> None:
    """Malformed GPIO masks should exit cleanly rather than raising ValueError."""
    interface = _interface_double()
    context = _context(interface, gpio_rd="zz")
    client = MagicMock()
    client_factory = MagicMock(return_value=client)
    hooks = _hooks(remote_hardware_client=client_factory)

    with pytest.raises(SystemExit) as exc_info:
        _handle_messaging_actions(context, hooks)

    assert exc_info.value.code == 1
    client_factory.assert_called_once_with(interface)
    client.readGPIOs.assert_not_called()


@pytest.mark.unit
def test_information_get_accumulates_success_across_preferences() -> None:
    """Any successful preference lookup should retain the completion message."""
    interface = _interface_double()
    context = _context(interface, get=[["lora"], ["missing"]])
    get_pref = MagicMock(side_effect=[True, False])
    cli_print = MagicMock()

    _handle_information_actions(
        context,
        _hooks(get_pref=get_pref, cli_print=cli_print),
    )

    cli_print.assert_any_call("Completed getting preferences")


@pytest.mark.unit
def test_information_get_routes_preference_output_through_cli_print() -> None:
    """--get preference reads should receive the dispatch print seam."""
    interface = _interface_double()
    node = MagicMock()
    interface.getNode.return_value = node
    context = _context(interface, get=[["lora.region"]])
    get_pref = MagicMock(return_value=True)
    cli_print = MagicMock()

    _handle_information_actions(context, _hooks(get_pref=get_pref, cli_print=cli_print))

    get_pref.assert_called_once_with(node, "lora.region", cli_print=cli_print)
    cli_print.assert_any_call("Completed getting preferences")


@pytest.mark.unit
def test_information_get_keeps_results_visible_with_separate_quiet_sink() -> None:
    """Quiet suppresses ordinary progress, not explicit --get results."""
    interface = _interface_double()
    interface.getNode.return_value = node = MagicMock()
    context = _context(interface, get=[["lora.region"]])
    ordinary_output = MagicMock()
    preference_output = MagicMock()
    get_pref = MagicMock(return_value=True)

    _handle_information_actions(
        context,
        _hooks(
            cli_print=ordinary_output,
            preference_print=preference_output,
            get_pref=get_pref,
        ),
    )

    get_pref.assert_called_once_with(node, "lora.region", cli_print=preference_output)
    preference_output.assert_called_once_with("Completed getting preferences")


@pytest.mark.unit
def test_content_reads_route_values_through_preference_print(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--get-canned-message/--get-ringtone values must reach the injected sink."""
    interface = _interface_double()
    node = MagicMock()
    node.get_canned_message.return_value = "hello"
    node.get_ringtone.return_value = "tone"
    interface.getNode.return_value = node
    context = _context(interface, get_canned_message=True, get_ringtone=True)
    sink: list[str] = []

    _handle_content_reads(
        context, _hooks(preference_print=MagicMock(side_effect=sink.append))
    )

    out, err = capsys.readouterr()
    assert out == ""
    assert err == ""
    joined = "".join(sink)
    assert "canned_plugin_message:hello" in joined
    assert "ringtone:tone" in joined


@pytest.mark.unit
def test_content_reads_fall_back_to_cli_print_without_preference_print() -> None:
    """Without preference_print the values must still go through the print seam."""
    interface = _interface_double()
    node = MagicMock()
    node.get_canned_message.return_value = "hello"
    interface.getNode.return_value = node
    context = _context(interface, get_canned_message=True)
    cli_print = MagicMock()

    _handle_content_reads(context, _hooks(cli_print=cli_print))

    cli_print.assert_any_call("canned_plugin_message:hello")


@pytest.mark.unit
def test_information_info_routes_output_through_preference_print(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--info separators and upgrade banner must reach the injected sink."""
    interface = _interface_double()
    interface.getNode.return_value = MagicMock()
    context = _context(interface, info=True, dest="^all")
    sink: list[str] = []
    ordinary_output = MagicMock()

    _handle_information_actions(
        context,
        _hooks(
            cli_print=ordinary_output,
            preference_print=MagicMock(side_effect=sink.append),
            newer_version=MagicMock(return_value="9.9.9"),
        ),
    )

    out, err = capsys.readouterr()
    assert out == ""
    assert err == ""
    joined = "".join(sink)
    assert "*** A newer version v9.9.9 is available!" in joined
    assert any(line == "" for line in sink)
    interface.showInfo.assert_called_once()
    ordinary_output.assert_not_called()


@pytest.mark.unit
def test_information_get_accepts_legacy_two_argument_hook() -> None:
    """Old embedding callbacks still work without swallowing their errors."""
    interface = _interface_double()
    interface.getNode.return_value = node = MagicMock()
    context = _context(interface, get=[["lora.region"]])
    reads: list[tuple[Any, str]] = []
    output: list[str] = []

    def legacy_get_pref(remote_node: Any, pref_name: str) -> bool:
        reads.append((remote_node, pref_name))
        return True

    _handle_information_actions(
        context, _hooks(get_pref=legacy_get_pref, cli_print=output.append)
    )
    assert reads == [(node, "lora.region")]
    assert output == ["Completed getting preferences"]


@pytest.mark.unit
def test_information_get_does_not_retry_a_failing_sink_aware_hook() -> None:
    """A hook's own TypeError must not trigger a second legacy invocation."""
    interface = _interface_double()
    context = _context(interface, get=[["lora.region"]])
    calls = []

    def failing_hook(_node: Any, _name: str, *, cli_print: Any) -> bool:
        calls.append(cli_print)
        raise TypeError("failure inside the hook")

    with pytest.raises(TypeError, match="failure inside the hook"):
        _handle_information_actions(context, _hooks(get_pref=failing_hook))
    assert len(calls) == 1


@pytest.mark.unit
def test_content_reads_escape_terminal_control_sequences(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Remote text content must not inject terminal control sequences."""
    interface = _interface_double()
    node = MagicMock()
    node.get_canned_message.return_value = "hello\x1b[31mred\x07"
    node.get_ringtone.return_value = "tone\nnext"
    interface.getNode.return_value = node
    context = _context(interface, get_canned_message=True, get_ringtone=True)

    _handle_content_reads(context, _hooks(cli_print=print))

    output = capsys.readouterr().out
    assert "\x1b" not in output
    assert "\x07" not in output
    assert r"\x1b[31m" in output
    assert r"\x07" in output
    assert r"tone\nnext" in output


@pytest.mark.unit
@pytest.mark.parametrize("raw_mask", ["-1", "10000000000000000"])
def test_gpio_read_rejects_masks_outside_uint64(raw_mask: str) -> None:
    """GPIO read masks must fit the uint64 protobuf field before transmission."""
    interface = _interface_double()
    client = MagicMock()
    context = _context(interface, gpio_rd=raw_mask)
    hooks = _hooks(remote_hardware_client=MagicMock(return_value=client))

    with pytest.raises(SystemExit):
        _handle_messaging_actions(context, hooks)

    client.readGPIOs.assert_not_called()


@pytest.mark.unit
def test_gpio_write_rejects_bit_index_outside_uint64() -> None:
    """GPIO write bit indices must not exceed the uint64 hardware mask."""
    interface = _interface_double()
    client = MagicMock()
    context = _context(interface, gpio_wrb=[("64", "1")])
    hooks = _hooks(remote_hardware_client=MagicMock(return_value=client))

    with pytest.raises(SystemExit):
        _handle_messaging_actions(context, hooks)

    client.writeGPIOs.assert_not_called()


@pytest.mark.unit
def test_slog_overrides_prior_one_shot_close_request() -> None:
    """Keep the connection alive after structured logging starts successfully."""
    interface = _interface_double()
    log_set = MagicMock()
    context = _context(interface, slog="default")
    context.outcome.close_now = True
    hooks = _hooks(log_set_factory=MagicMock(return_value=log_set))

    _handle_long_running_services(context, hooks)

    assert context.outcome.close_now is False
    assert context.outcome.failure_cleanup_callbacks == [log_set.close]


@pytest.mark.unit
@pytest.mark.parametrize("service", ["slog", "power_stress"])
def test_missing_optional_service_factory_fails_closed(service: str) -> None:
    """Missing optional factories must never be called after a returning exit seam."""
    interface = _interface_double()
    returning_exit = MagicMock()
    overrides: dict[str, Any] = {"cli_exit": returning_exit}
    context_overrides: dict[str, Any] = {}
    if service == "slog":
        overrides["log_set_factory"] = None
        context_overrides["slog"] = "default"
    else:
        overrides["power_stress_factory"] = None
        context_overrides["power_stress"] = True
    context = _context(interface, **context_overrides)

    with pytest.raises(AssertionError, match="cli_exit returned unexpectedly"):
        _handle_long_running_services(context, _hooks(**overrides))


@pytest.mark.unit
@pytest.mark.parametrize(
    ("overrides", "expected_message"),
    [
        ({"gpio_rd": "10000000000000000"}, "GPIO mask"),
        ({"gpio_wrb": [("not-a-bit", "1")]}, "GPIO bit/value"),
        ({"gpio_wrb": [("64", "1")]}, "GPIO bit/value"),
    ],
)
def test_gpio_validation_fails_closed_with_returning_exit(
    overrides: dict[str, Any], expected_message: str
) -> None:
    """Invalid GPIO input must not reach remote hardware if the exit seam returns."""
    interface = _interface_double()
    client = MagicMock()
    cli_exit = MagicMock()
    hooks = _hooks(
        cli_exit=cli_exit, remote_hardware_client=MagicMock(return_value=client)
    )
    context = _context(interface, **overrides)

    with pytest.raises(AssertionError, match="cli_exit returned unexpectedly"):
        _handle_messaging_actions(context, hooks)

    assert expected_message in str(cli_exit.call_args)
    client.writeGPIOs.assert_not_called()
    client.readGPIOs.assert_not_called()


@pytest.mark.unit
def test_gpio_mask_accepts_full_uint64_range() -> None:
    """The largest valid uint64 GPIO mask should parse without truncation."""
    assert actions._parse_gpio_mask("ffffffffffffffff", _hooks()) == (1 << 64) - 1


@pytest.mark.unit
def test_position_request_sends_on_selected_channel() -> None:
    """A valid position request should use the selected channel and await response."""
    interface = _interface_double()
    context = _context(interface, request_position=True)

    _handle_messaging_actions(context, _hooks())

    interface.sendPosition.assert_called_once_with(
        destinationId="!00000002", wantResponse=True, channelIndex=2
    )


@pytest.mark.unit
def test_gpio_read_resets_state_and_stops_on_own_response() -> None:
    """Each GPIO read must reset stale response state and stop on its own callback."""
    interface = _interface_double()
    interface.gotResponse = True
    client = MagicMock()

    def _respond(*_args: Any) -> None:
        assert interface.gotResponse is False
        interface.gotResponse = True

    client.readGPIOs.side_effect = _respond
    sleep = MagicMock()
    context = _context(interface, gpio_rd="0x10")

    _handle_messaging_actions(
        context,
        _hooks(
            remote_hardware_client=MagicMock(return_value=client),
            sleep=sleep,
        ),
    )

    assert interface.mask == 0x10
    client.readGPIOs.assert_called_once_with("!00000002", 0x10, None)
    sleep.assert_called_once_with(actions.GPIO_READ_POLL_INTERVAL_SECONDS)


@pytest.mark.unit
def test_gpio_read_timeout_is_diagnostic_not_attribute_error() -> None:
    """An unanswered GPIO read should exhaust its poll budget and warn cleanly."""
    interface = _interface_double()
    client = MagicMock()
    cli_print = MagicMock()
    sleep = MagicMock()
    context = _context(interface, gpio_rd="0x1")

    _handle_messaging_actions(
        context,
        _hooks(
            cli_print=cli_print,
            remote_hardware_client=MagicMock(return_value=client),
            sleep=sleep,
        ),
    )

    assert interface.gotResponse is False
    assert sleep.call_count == actions.GPIO_READ_MAX_POLLS
    cli_print.assert_any_call("Warning: no GPIO response received.")


@pytest.mark.unit
def test_gpio_watch_sends_watch_before_propagating_interrupt() -> None:
    """Watch mode should issue the remote watch request before its long-running sleep."""
    interface = _interface_double()
    client = MagicMock()
    client.watchGPIOs.side_effect = [None, KeyboardInterrupt()]
    sleep = MagicMock()
    context = _context(interface, gpio_watch="0x4")

    with pytest.raises(KeyboardInterrupt):
        _handle_messaging_actions(
            context,
            _hooks(
                remote_hardware_client=MagicMock(return_value=client),
                sleep=sleep,
            ),
        )

    assert client.watchGPIOs.call_count == 2
    client.watchGPIOs.assert_called_with("!00000002", 0x4)
    sleep.assert_called_once_with(actions.GPIO_WATCH_INTERVAL_SECONDS)


@pytest.mark.unit
def test_information_actions_info_paths_and_upgrade_notice() -> None:
    """Local info should include node detail and a discovered upgrade notice."""
    interface = _interface_double()
    interface.getNode.return_value = MagicMock()
    context = _context(interface, dest="^all", info=True)
    output = MagicMock()

    _handle_information_actions(
        context,
        _hooks(
            cli_print=output,
            newer_version=MagicMock(return_value="9.9.9"),
        ),
    )

    interface.showInfo.assert_called_once()
    assert "file" in interface.showInfo.call_args.kwargs
    interface.getNode.return_value.showInfo.assert_called_once_with(cli_print=output)
    assert any(
        "newer version v9.9.9" in str(call.args[0]) for call in output.call_args_list
    )


@pytest.mark.unit
def test_information_actions_remote_info_is_non_mutating() -> None:
    """Remote --info should explain the supported --get alternative via the quiet-aware hook."""
    interface = _interface_double()
    context = _context(interface, info=True)
    cli_print = MagicMock()

    _handle_information_actions(context, _hooks(cli_print=cli_print))

    cli_print.assert_any_call("Showing info of remote node is not supported.")
    interface.showInfo.assert_not_called()


@pytest.mark.unit
def test_show_fields_without_nodes_stops_processing() -> None:
    """--show-fields alone should stop before later connected actions execute."""
    interface = _interface_double()
    context = _context(interface, show_fields=["user.id"])
    cli_print = MagicMock()

    _handle_information_actions(context, _hooks(cli_print=cli_print))

    assert context.outcome.stop_processing is True
    cli_print.assert_called_once_with("--show-fields can only be used with --nodes")


@pytest.mark.unit
def test_tunnel_rejects_remote_destination() -> None:
    """Tunnel mode must fail closed for any non-local destination."""
    interface = _interface_double()
    context = _context(interface, tunnel=True, dest="!remote")

    with pytest.raises(SystemExit):
        actions._start_tunnel(context, _hooks())


@pytest.mark.unit
def test_tunnel_is_skipped_on_non_linux_platforms() -> None:
    """Unsupported platforms must not start a tunnel or alter lifecycle state."""
    interface = _interface_double()
    context = _context(interface, tunnel=True, dest="^all")
    context.outcome.close_now = True

    actions._start_tunnel(
        context,
        _hooks(platform_system=MagicMock(return_value="Windows")),
    )

    assert context.outcome.close_now is True
    assert context.outcome.failure_cleanup_callbacks == []


@pytest.mark.unit
@pytest.mark.parametrize("subnet", [None, "10.42.0.0/16"])
def test_tunnel_starts_with_optional_subnet(
    monkeypatch: pytest.MonkeyPatch, subnet: str | None
) -> None:
    """Eligible tunnel requests should keep the connection open and forward subnet."""
    from meshtastic import tunnel

    interface = _interface_double()
    interface.noProto = False
    context = _context(interface, tunnel=True, dest="^all", tunnel_net=subnet)
    context.outcome.close_now = True
    tunnel_instance = MagicMock()
    tunnel_factory = MagicMock(return_value=tunnel_instance)
    monkeypatch.setattr(tunnel, "Tunnel", tunnel_factory)

    actions._start_tunnel(context, _hooks())

    assert context.outcome.close_now is False
    assert context.outcome.failure_cleanup_callbacks == [tunnel_instance.close]
    if subnet is None:
        tunnel_factory.assert_called_once_with(interface)
    else:
        tunnel_factory.assert_called_once_with(interface, subnet=subnet)


@pytest.mark.unit
def test_powermon_unavailable_reports_import_error() -> None:
    """Optional power actions should fail with the captured import diagnostic."""
    interface = _interface_double()
    context = _context(interface, slog="default")
    cli_exit = MagicMock(side_effect=SystemExit(1))

    with pytest.raises(SystemExit):
        _handle_long_running_services(
            context,
            _hooks(
                cli_exit=cli_exit,
                powermon_available=MagicMock(return_value=False),
                powermon_error=MagicMock(return_value=ImportError("missing meter")),
            ),
        )

    assert "missing meter" in str(cli_exit.call_args)


@pytest.mark.unit
def test_invalid_channel_rejects_traceroute_without_sending() -> None:
    """Invalid selected channels must fail visibly before traceroute transmission."""
    interface = _interface_double()
    interface.localNode = MagicMock()
    context = _context(interface, traceroute="!00000003")
    hooks = _hooks(check_channel=MagicMock(return_value=False))

    with pytest.raises(SystemExit):
        _handle_messaging_actions(context, hooks)

    interface.sendTraceRoute.assert_not_called()


@pytest.mark.unit
def test_missing_channel_selection_defaults_to_primary_channel() -> None:
    """An omitted channel index must send requests on the primary channel."""
    interface = _interface_double()
    interface.localNode = MagicMock()
    interface.localNode.localConfig.lora.hop_limit = 5
    context = _context(interface, traceroute="!00000003")
    hooks = _hooks(get_channel_index=lambda: None)

    _handle_messaging_actions(context, hooks)

    interface.sendTraceRoute.assert_called_once_with("!00000003", 5, channelIndex=0)


@pytest.mark.unit
def test_slog_transfers_cleanup_to_active_cli_session() -> None:
    """Successful slog startup should survive dispatch and close with the invocation."""
    import contextlib

    from meshtastic.cli.session_resources import CliSessionResources

    interface = _interface_double()
    log_set = MagicMock()
    with contextlib.ExitStack() as stack:
        session = CliSessionResources(stack)
        session.activate()
        context = _context(interface, slog="default")
        hooks = _hooks(log_set_factory=MagicMock(return_value=log_set))

        _handle_long_running_services(context, hooks)

        assert context.outcome.failure_cleanup_callbacks == []
        log_set.close.assert_not_called()

    log_set.close.assert_called_once_with()


@pytest.mark.unit
def test_tunnel_transfers_cleanup_to_active_cli_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful tunnel startup should be owned until the invocation ends."""
    import contextlib

    from meshtastic import tunnel
    from meshtastic.cli.session_resources import CliSessionResources

    interface = _interface_double()
    interface.noProto = False
    tunnel_instance = MagicMock()
    monkeypatch.setattr(tunnel, "Tunnel", MagicMock(return_value=tunnel_instance))

    with contextlib.ExitStack() as stack:
        session = CliSessionResources(stack)
        session.activate()
        context = _context(interface, tunnel=True, dest="^all")

        actions._start_tunnel(context, _hooks())

        assert context.outcome.failure_cleanup_callbacks == []
        tunnel_instance.close.assert_not_called()

    tunnel_instance.close.assert_called_once_with()


@pytest.mark.unit
def test_power_stress_closes_retained_slog_only_once() -> None:
    """Eager slog shutdown for power stress should disarm later session cleanup."""
    import contextlib

    from meshtastic.cli.session_resources import CliSessionResources

    interface = _interface_double()
    log_set = MagicMock()
    stress = MagicMock()
    with contextlib.ExitStack() as stack:
        session = CliSessionResources(stack)
        session.activate()
        context = _context(interface, slog="default", power_stress=True)
        hooks = _hooks(
            log_set_factory=MagicMock(return_value=log_set),
            power_stress_factory=MagicMock(return_value=stress),
        )

        _handle_long_running_services(context, hooks)

        log_set.close.assert_called_once_with()
        assert context.outcome.failure_cleanup_callbacks == []

    log_set.close.assert_called_once_with()


@pytest.mark.unit
def test_information_actions_threads_node_query_flags() -> None:
    """Local node listing should receive role, hwmodel, sort, and limit."""
    interface = _interface_double()
    context = _context(
        interface,
        dest="^all",
        nodes=True,
        role=["Client_Mute"],
        hwmodel=["RAK4631"],
        sort="snr:desc",
        limit=5,
    )
    hooks = _hooks()

    _handle_information_actions(context, hooks)

    interface.showNodes.assert_called_once_with(
        True,
        None,
        roleFilter=["client_mute"],
        hwModelFilter=["rak4631"],
        sortField="snr",
        sortDirection="desc",
        limit=5,
    )


@pytest.mark.unit
def test_information_actions_sort_flag_is_validated() -> None:
    """--sort should be resolved and validated before listing."""
    interface = _interface_double()
    context = _context(interface, dest="^all", nodes=True, sort="hwmodel:asc")
    validate_sort = MagicMock()
    hooks = _hooks(validate_cli_sort_field=validate_sort)

    _handle_information_actions(context, hooks)

    validate_sort.assert_called_once_with(interface, "hwmodel")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("sort_value", "message"),
    [
        ("snr:sideways", "--sort direction must be 'asc' or 'desc', got 'sideways'"),
        (":desc", "--sort requires a field name, got ':desc'"),
        ("", "--sort requires a field name, got ''"),
    ],
)
def test_information_actions_rejects_bad_sort_spec(
    sort_value: str, message: str
) -> None:
    """Malformed --sort values stop processing without listing."""
    interface = _interface_double()
    context = _context(interface, dest="^all", nodes=True, sort=sort_value)
    cli_print = MagicMock()
    hooks = _hooks(cli_print=cli_print)

    _handle_information_actions(context, hooks)

    assert context.outcome.stop_processing is True
    cli_print.assert_called_once_with(message)
    interface.showNodes.assert_not_called()


@pytest.mark.unit
def test_information_actions_rejects_negative_limit() -> None:
    """--limit below zero stops processing without listing."""
    interface = _interface_double()
    context = _context(interface, dest="^all", nodes=True, limit=-1)
    cli_print = MagicMock()
    hooks = _hooks(cli_print=cli_print)

    _handle_information_actions(context, hooks)

    assert context.outcome.stop_processing is True
    cli_print.assert_called_once_with("--limit expects a non-negative integer")
    interface.showNodes.assert_not_called()


@pytest.mark.unit
def test_node_query_flags_without_nodes_stop_processing() -> None:
    """--role/--hwmodel/--sort/--limit alone should stop before connected actions."""
    interface = _interface_double()
    context = _context(interface, role=["client"], hwmodel=["rak"], sort="snr", limit=3)
    cli_print = MagicMock()

    _handle_information_actions(context, _hooks(cli_print=cli_print))

    assert context.outcome.stop_processing is True
    cli_print.assert_called_once_with(
        "--role, --hwmodel, --sort, --limit can only be used with --nodes"
    )
