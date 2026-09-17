"""Edge-case tests for internal channel/contact CLI action contracts."""

from __future__ import annotations

import argparse
from contextlib import nullcontext
from typing import Any, NoReturn, cast
from unittest.mock import MagicMock, create_autospec

import pytest

from meshtastic.cli import channel_contact_actions as actions
from meshtastic.cli.channel_contact_actions import ChannelContactHooks
from meshtastic.cli.context import ActionOutcome, CliContext
from meshtastic.mesh_interface import MeshInterface
from meshtastic.node import Node
from meshtastic.protobuf import channel_pb2


def _cli_exit(_message: str, return_value: int = 1) -> NoReturn:
    """Raise ``SystemExit`` with the status supplied by the action under test."""
    raise SystemExit(return_value)


def _hooks(**overrides: Any) -> ChannelContactHooks:
    """Build channel/contact hooks with deterministic defaults."""
    values: dict[str, Any] = {
        "cli_exit": _cli_exit,
        "cli_print": MagicMock(),
        "get_channel_index": MagicMock(return_value=1),
        "set_channel_index": MagicMock(),
        "resolve_pref": MagicMock(return_value=True),
        "set_pref": MagicMock(return_value=True),
        "fatal_preference_value_errors": lambda: nullcontext(),
        "preference_value_error": ValueError,
        "print_channel_field_choices": MagicMock(),
        "is_local_destination": MagicMock(return_value=True),
        "modem_preset_shorthands": (),
        "qr_render": None,
    }
    values.update(overrides)
    return ChannelContactHooks(**values)


def _args(**overrides: Any) -> argparse.Namespace:
    """Build parser-faithful defaults for channel/contact action tests."""
    values: dict[str, Any] = {
        "dest": "^all",
        "add_contact": None,
        "ch_add": None,
        "ch_del": False,
        "ch_set": None,
        "ch_enable": False,
        "ch_disable": False,
        "ch_set_url": None,
        "ch_add_url": None,
        "ch_preset": None,
        "show_region_presets": False,
        "qr": False,
        "qr_all": False,
        "contact_qr": None,
        "contact_ignore": False,
        "contact_verified": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _context(interface: Any, **overrides: Any) -> CliContext:
    """Build a connected action context around a test interface double."""
    return CliContext(
        interface=cast(MeshInterface, interface),
        args=_args(**overrides),
        get_node_kwargs={},
        outcome=ActionOutcome(),
    )


def _interface_double() -> MagicMock:
    """Return an autospecced connected-interface double."""
    return create_autospec(MeshInterface, instance=True)


def _node_double() -> MagicMock:
    """Return an autospecced node double for configured node behavior."""
    return create_autospec(Node, instance=True)


def _interface_with_node() -> tuple[MagicMock, MagicMock]:
    """Return a specced interface whose ``getNode`` returns a specced node."""
    interface = _interface_double()
    node = _node_double()
    interface.getNode.return_value = node
    return interface, node


@pytest.mark.unit
def test_invalid_numeric_modem_preset_is_rejected() -> None:
    """Unknown numeric presets should fail instead of leaking invalid protobuf values."""
    with pytest.raises(SystemExit):
        actions._resolve_requested_modem_preset(
            _context(_interface_double(), ch_preset=999999), _hooks()
        )


@pytest.mark.unit
def test_channel_add_rejects_name_over_protocol_limit() -> None:
    """Channel names longer than the firmware field limit must fail before lookup."""
    interface = _interface_double()
    hooks = _hooks(get_channel_index=MagicMock(return_value=None))
    context = _context(interface, ch_add="x" * (actions.MAX_CHANNEL_NAME_LENGTH + 1))

    with pytest.raises(SystemExit):
        actions._handle_channel_add(context, hooks)

    interface.getNode.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize("case", ["missing", "negative", "out_of_range"])
def test_channel_update_rejects_unavailable_or_invalid_channel(case: str) -> None:
    """Channel mutation must fail before dereferencing unavailable channel state."""
    interface, node = _interface_with_node()
    hooks = _hooks()
    if case == "missing":
        node.channels = None
    elif case == "negative":
        node.channels = [MagicMock()]
        hooks = _hooks(get_channel_index=MagicMock(return_value=-1))
    else:
        node.channels = [MagicMock()]
        hooks = _hooks(get_channel_index=MagicMock(return_value=4))

    with pytest.raises(SystemExit):
        actions._handle_channel_update(_context(interface, ch_enable=True), hooks)
    node.writeChannel.assert_not_called()


@pytest.mark.unit
def test_channel_update_rejects_failed_preference_write() -> None:
    """A resolved setting whose value cannot be applied must not write the channel."""
    interface, node = _interface_with_node()
    channel = MagicMock()
    node.channels = [MagicMock(), channel]
    hooks = _hooks(set_pref=MagicMock(return_value=False))

    with pytest.raises(SystemExit):
        actions._handle_channel_update(
            _context(interface, ch_set=[["name", "mesh"]]), hooks
        )
    interface.getNode.return_value.writeChannel.assert_not_called()


@pytest.mark.unit
def test_channel_disable_remains_authoritative_when_settings_change() -> None:
    """A settings update must not re-enable an explicitly disabled channel."""
    interface, node = _interface_with_node()
    primary = channel_pb2.Channel(index=0, role=channel_pb2.Channel.Role.PRIMARY)
    channel = channel_pb2.Channel(index=1, role=channel_pb2.Channel.Role.SECONDARY)
    channel.settings.name = "old"
    node.channels = [primary, channel]

    def set_pref(target: Any, name: str, value: Any) -> bool:
        setattr(target, name, value)
        return True

    hooks = _hooks(set_pref=MagicMock(side_effect=set_pref))

    actions._handle_channel_update(
        _context(
            interface,
            ch_disable=True,
            ch_set=[["name", "mesh"]],
        ),
        hooks,
    )

    assert channel.settings.name == "mesh"
    assert channel.role == channel_pb2.Channel.Role.DISABLED
    node.writeChannel.assert_called_once_with(1)


@pytest.mark.unit
def test_add_channel_url_uses_add_only_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """--ch-add-url should preserve existing channels by selecting addOnly mode."""
    interface, node = _interface_with_node()
    context = _context(interface, ch_add_url="https://example.invalid/#abc")
    monkeypatch.setattr(actions, "_handle_channel_add", MagicMock())
    monkeypatch.setattr(actions, "_handle_channel_delete", MagicMock())
    monkeypatch.setattr(
        actions, "_resolve_requested_modem_preset", MagicMock(return_value=None)
    )
    monkeypatch.setattr(actions, "_handle_channel_update", MagicMock())

    actions._handle_channel_mutations(context, _hooks())

    node.setURL.assert_called_once_with("https://example.invalid/#abc", addOnly=True)
    assert context.outcome.close_now is True


@pytest.mark.unit
def test_enum_name_or_fallback_handles_known_and_unknown_values() -> None:
    """Region/preset display should remain stable across future enum values."""
    enum_wrapper = MagicMock()
    enum_wrapper.Name.side_effect = ["KNOWN", ValueError("unknown")]

    assert actions._enum_name_or_fallback(enum_wrapper, 1, "VALUE_") == "KNOWN"
    assert actions._enum_name_or_fallback(enum_wrapper, 99, "VALUE_") == "VALUE_99"


@pytest.mark.unit
def test_region_display_uses_numeric_fallbacks_for_future_firmware_values() -> None:
    """Unknown region and preset values should still produce readable metadata output."""
    interface = _interface_double()
    interface.regionPresets = {
        999: argparse.Namespace(presets=[998], default_preset=997, licensed_only=True)
    }
    cli_print = MagicMock()

    actions._handle_region_preset_display(
        _context(interface, show_region_presets=True), _hooks(cli_print=cli_print)
    )

    rendered = str(cli_print.call_args)
    assert "REGION_999" in rendered
    assert "PRESET_998" in rendered
    assert "PRESET_997" in rendered
    assert "licensed-only" in rendered


@pytest.mark.unit
def test_qr_without_optional_dependency_prints_install_guidance() -> None:
    """Missing segno should still emit the URL and an actionable installation hint."""
    cli_print = MagicMock()
    actions._print_qr(
        "https://example.invalid/#abc",
        description="Primary channel URL",
        qr_render=None,
        cli_print=cli_print,
    )

    assert cli_print.call_count == 2
    cli_print.assert_any_call("Install segno to view a QR code printed to terminal.")


@pytest.mark.unit
def test_qr_with_renderer_prints_rendered_qr() -> None:
    """An installed renderer should be invoked with the URL and its output printed."""
    cli_print = MagicMock()
    qr_render = MagicMock(return_value="<qr-terminal>")

    actions._print_qr(
        "https://example.invalid/#abc",
        description="Primary channel URL",
        qr_render=qr_render,
        cli_print=cli_print,
    )

    qr_render.assert_called_once_with("https://example.invalid/#abc")
    cli_print.assert_any_call("Primary channel URL: https://example.invalid/#abc")
    cli_print.assert_any_call("<qr-terminal>")


@pytest.mark.unit
def test_invalid_named_modem_preset_is_rejected() -> None:
    """Unknown preset names should fail through the same diagnostic path as bad integers."""
    with pytest.raises(SystemExit):
        actions._resolve_requested_modem_preset(
            _context(_interface_double(), ch_preset="NOT_A_PRESET"), _hooks()
        )


@pytest.mark.unit
def test_modem_preset_only_action_requests_interface_close() -> None:
    """A preset-only write is a one-shot mutation and must close after persistence."""
    from meshtastic.protobuf import config_pb2

    interface, node = _interface_with_node()
    node.localConfig = MagicMock()
    node.localConfig.ListFields.return_value = [object()]
    context = _context(interface, ch_preset="LONG_FAST")
    hooks = _hooks(get_channel_index=MagicMock(return_value=0))

    actions._handle_channel_mutations(context, hooks)

    assert context.outcome.close_now is True
    assert (
        node.localConfig.lora.modem_preset
        == config_pb2.Config.LoRaConfig.ModemPreset.Value("LONG_FAST")
    )
    node.writeConfig.assert_called_once_with("lora")


@pytest.mark.unit
def test_modem_preset_rejects_negative_channel_index() -> None:
    """A negative channel index must not fall through to a primary preset write."""
    interface, node = _interface_with_node()
    context = _context(interface, ch_preset="LONG_FAST")
    hooks = _hooks(get_channel_index=MagicMock(return_value=-1))

    with pytest.raises(SystemExit):
        actions._handle_channel_mutations(context, hooks)

    interface.getNode.assert_not_called()
    node.writeConfig.assert_not_called()
    assert context.outcome.close_now is False


@pytest.mark.unit
def test_contact_qr_uses_parser_argument_name() -> None:
    """Contact QR display must consume argparse's contact_qr destination."""
    interface = _interface_double()
    interface.localNode = _node_double()
    interface.localNode.getContactURL.return_value = "https://example.invalid/contact"
    context = _context(
        interface,
        contact_qr="!12345678",
        contact_ignore=True,
        contact_verified=True,
    )
    cli_print = MagicMock()

    actions._handle_channel_contact_display(context, _hooks(cli_print=cli_print))

    interface.localNode.getContactURL.assert_called_once_with(
        "!12345678", should_ignore=True, manually_verified=True
    )
    assert context.outcome.close_now is True
