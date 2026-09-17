"""Connected CLI actions for channel and contact management."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, cast

import meshtastic.util
from meshtastic.cli.context import CliContext, CliExit, _terminate_cli
from meshtastic.protobuf import channel_pb2, config_pb2

MAX_CHANNEL_NAME_LENGTH: int = 10


@dataclass(frozen=True, slots=True)
class ChannelContactHooks:
    """Compatibility and process-state seams used by channel/contact actions."""

    cli_exit: CliExit
    cli_print: Callable[[str], None]
    get_channel_index: Callable[[], int | None]
    set_channel_index: Callable[[int], None]
    resolve_pref: Callable[[Any, str], bool]
    set_pref: Callable[[Any, str, Any], bool]
    fatal_preference_value_errors: Callable[[], AbstractContextManager[None]]
    preference_value_error: type[Exception]
    print_channel_field_choices: Callable[[Any, str], None]
    is_local_destination: Callable[[Any, str], bool]
    modem_preset_shorthands: tuple[tuple[tuple[str, ...], str, str, str], ...]
    qr_render: Callable[[str], str] | None = None


def _handle_contact_import(context: CliContext) -> None:
    """Import a contact URL before other connected message actions."""
    args = context.args
    if not args.add_contact:
        return

    context.outcome.close_now = True
    context.outcome.wait_for_ack_nak = True
    context.outcome.skip_ack_wait = True  # addContactURL owns the remote ACK wait.
    context.interface.getNode(
        args.dest, False, **context.get_node_kwargs
    ).addContactURL(args.add_contact)


def _set_simple_config(
    context: CliContext,
    hooks: ChannelContactHooks,
    modem_preset: config_pb2.Config.LoRaConfig.ModemPreset.ValueType,
) -> None:
    """Set and persist the LoRa modem preset on the primary channel."""
    channel_index = hooks.get_channel_index()
    if channel_index not in (None, 0):
        _terminate_cli(
            hooks.cli_exit,
            "Warning: Cannot set modem preset for non-primary channel",
            1,
        )

    node = context.interface.getNode(
        context.args.dest, False, **context.get_node_kwargs
    )
    if len(node.localConfig.ListFields()) == 0:
        lora_descriptor = node.localConfig.DESCRIPTOR.fields_by_name.get("lora")
        if lora_descriptor is None:
            _terminate_cli(
                hooks.cli_exit,
                "The active protobuf schema does not provide LoRa configuration",
                1,
            )
        node.requestConfig(lora_descriptor)
    node.localConfig.lora.modem_preset = modem_preset
    context.outcome.close_now = True
    node.writeConfig("lora")


def _resolve_requested_modem_preset(
    context: CliContext, hooks: ChannelContactHooks
) -> config_pb2.Config.LoRaConfig.ModemPreset.ValueType | None:
    """Resolve historical shorthand and schema-driven modem-preset arguments."""
    args = context.args
    preset_val: config_pb2.Config.LoRaConfig.ModemPreset.ValueType | None = None
    for _, destination, preset_name, _ in hooks.modem_preset_shorthands:
        if getattr(args, destination, False):
            preset_val = config_pb2.Config.LoRaConfig.ModemPreset.Value(preset_name)

    generic_preset_name = getattr(args, "ch_preset", None)
    if generic_preset_name is None:
        return preset_val

    if isinstance(generic_preset_name, int):
        # Round-trip the value through Name() so invalid integers fail clearly.
        try:
            config_pb2.Config.LoRaConfig.ModemPreset.Name(
                generic_preset_name  # type: ignore[arg-type]
            )
        except ValueError as exc:
            _terminate_cli(hooks.cli_exit, f"Invalid modem preset: {exc}", 1)
        return cast(
            config_pb2.Config.LoRaConfig.ModemPreset.ValueType,
            generic_preset_name,
        )
    try:
        return config_pb2.Config.LoRaConfig.ModemPreset.Value(generic_preset_name)
    except ValueError as exc:
        _terminate_cli(hooks.cli_exit, f"Invalid modem preset: {exc}", 1)


def _handle_channel_add(context: CliContext, hooks: ChannelContactHooks) -> None:
    """Add one secondary channel using the next disabled channel slot.

    Parameters
    ----------
    context : CliContext
        Connected invocation state. ``close_now`` is enabled when an add is
        requested and the selected channel index is updated after success.
    hooks : ChannelContactHooks
        Channel-index, reporting, preference, and exit seams.
    """
    args = context.args
    if not args.ch_add:
        return

    if hooks.get_channel_index() is not None:
        _terminate_cli(
            hooks.cli_exit,
            "Warning: --ch-add chooses the next free channel index automatically; "
            "remove --ch-index and retry. Use --ch-set, --ch-del, --ch-enable, "
            "or --ch-disable when targeting a specific index.",
        )
    context.outcome.close_now = True
    if len(args.ch_add) > MAX_CHANNEL_NAME_LENGTH:
        _terminate_cli(
            hooks.cli_exit, "Warning: Channel name must be shorter. Channel not added."
        )

    node = context.interface.getNode(args.dest, **context.get_node_kwargs)
    channel = node.getChannelByName(args.ch_add)
    if channel:
        _terminate_cli(
            hooks.cli_exit,
            f"Warning: This node already has a '{args.ch_add}' channel. "
            "No changes were made.",
        )

    channel = node.getDisabledChannel()
    if not channel:
        _terminate_cli(hooks.cli_exit, "Warning: No free channels were found")
    settings = channel_pb2.ChannelSettings()
    settings.psk = meshtastic.util.genPSK256()
    settings.name = args.ch_add
    channel.settings.CopyFrom(settings)
    channel.role = channel_pb2.Channel.Role.SECONDARY
    hooks.cli_print("Writing modified channels to device")
    node.writeChannel(channel.index)
    hooks.cli_print(
        f"Setting newly-added channel's {channel.index} as '--ch-index' "
        "for further modifications"
    )
    hooks.set_channel_index(channel.index)


def _handle_channel_delete(context: CliContext, hooks: ChannelContactHooks) -> None:
    """Delete the explicitly selected non-primary channel.

    Parameters
    ----------
    context : CliContext
        Connected invocation state. ``close_now`` is enabled for a delete.
    hooks : ChannelContactHooks
        Channel-index, reporting, and exit seams.
    """
    args = context.args
    if not args.ch_del:
        return

    context.outcome.close_now = True
    channel_index = hooks.get_channel_index()
    if channel_index is None:
        _terminate_cli(
            hooks.cli_exit, "Warning: Need to specify '--ch-index' for '--ch-del'.", 1
        )
    if channel_index == 0:
        _terminate_cli(hooks.cli_exit, "Warning: Cannot delete primary channel.", 1)

    hooks.cli_print(f"Deleting channel {channel_index}")
    context.interface.getNode(args.dest, **context.get_node_kwargs).deleteChannel(
        channel_index
    )


def _handle_channel_update(context: CliContext, hooks: ChannelContactHooks) -> None:
    """Apply channel settings or role changes to the selected channel.

    Parameters
    ----------
    context : CliContext
        Connected invocation state. ``close_now`` is enabled for channel
        mutation requests.
    hooks : ChannelContactHooks
        Channel-index, preference, reporting, and exit seams.
    """
    args = context.args
    if not (args.ch_set or args.ch_enable or args.ch_disable):
        return

    context.outcome.close_now = True
    channel_index = hooks.get_channel_index()
    if channel_index is None:
        _terminate_cli(hooks.cli_exit, "Warning: Need to specify '--ch-index'.", 1)

    node = context.interface.getNode(args.dest, **context.get_node_kwargs)
    channels = node.channels
    if channels is None:
        _terminate_cli(hooks.cli_exit, "Warning: Device channels are not available.", 1)
    if channel_index < 0:
        _terminate_cli(
            hooks.cli_exit,
            f"Warning: Channel index {channel_index} is out of range.",
            1,
        )
    try:
        channel = channels[channel_index]
    except (IndexError, TypeError):
        _terminate_cli(
            hooks.cli_exit,
            f"Warning: Channel index {channel_index} is out of range.",
            1,
        )

    enable = not args.ch_disable
    if args.ch_enable or args.ch_disable:
        hooks.cli_print(
            "Warning: --ch-enable and --ch-disable can produce noncontiguous channels, "
            "which can cause errors in some clients. Whenever possible, use --ch-add "
            "and --ch-del instead."
        )
        if channel_index == 0:
            _terminate_cli(
                hooks.cli_exit, "Warning: Cannot enable/disable PRIMARY channel."
            )
    pending_settings = type(channel.settings)()
    pending_settings.CopyFrom(channel.settings)
    channel_update_valid = True
    for pref in args.ch_set or []:
        if pref[0] == "psk":
            try:
                pending_settings.psk = meshtastic.util.fromPSK(pref[1])
            except ValueError as exc:
                _terminate_cli(hooks.cli_exit, f"Invalid channel PSK: {exc}", 1)
        else:
            if not hooks.resolve_pref(pending_settings, pref[0]):
                hooks.print_channel_field_choices(pending_settings, pref[0])
                channel_update_valid = False
                continue
            try:
                with hooks.fatal_preference_value_errors():
                    found = hooks.set_pref(pending_settings, pref[0], pref[1])
            except hooks.preference_value_error as exc:
                _terminate_cli(hooks.cli_exit, str(exc), 1)
            if not found:
                _terminate_cli(
                    hooks.cli_exit, f"Invalid value for channel setting {pref[0]}.", 1
                )
    if not channel_update_valid:
        _terminate_cli(
            hooks.cli_exit,
            "Warning: Unknown channel setting name. No changes were made.",
            1,
        )

    if args.ch_set:
        channel.settings.CopyFrom(pending_settings)
    if enable:
        channel.role = (
            channel_pb2.Channel.Role.PRIMARY
            if channel_index == 0
            else channel_pb2.Channel.Role.SECONDARY
        )
    else:
        channel.role = channel_pb2.Channel.Role.DISABLED

    hooks.cli_print("Writing modified channels to device")
    node.writeChannel(channel_index)


def _handle_channel_mutations(context: CliContext, hooks: ChannelContactHooks) -> None:
    """Apply channel URL, add/delete, preset, and setting mutations in CLI order."""
    args = context.args
    interface = context.interface

    if args.ch_set_url:
        context.outcome.close_now = True
        interface.getNode(args.dest, **context.get_node_kwargs).setURL(
            args.ch_set_url, addOnly=False
        )
    if args.ch_add_url:
        context.outcome.close_now = True
        interface.getNode(args.dest, **context.get_node_kwargs).setURL(
            args.ch_add_url, addOnly=True
        )

    _handle_channel_add(context, hooks)
    _handle_channel_delete(context, hooks)

    preset_val = _resolve_requested_modem_preset(context, hooks)
    if preset_val is not None:
        _set_simple_config(context, hooks, preset_val)

    _handle_channel_update(context, hooks)


def _enum_name_or_fallback(enum_wrapper: Any, value: Any, prefix: str) -> str:
    """Return a protobuf enum name or a stable numeric fallback.

    Parameters
    ----------
    enum_wrapper : Any
        Protobuf enum wrapper exposing ``Name``.
    value : Any
        Enum numeric value to render.
    prefix : str
        Prefix for unknown numeric values.

    Returns
    -------
    str
        Schema enum name, or ``prefix`` followed by the numeric value.
    """
    try:
        return cast(str, enum_wrapper.Name(cast(Any, value)))
    except ValueError:
        return f"{prefix}{value}"


def _handle_region_preset_display(
    context: CliContext, hooks: ChannelContactHooks
) -> None:
    """Print firmware-provided region/preset compatibility metadata."""
    args = context.args
    interface = context.interface
    if not args.show_region_presets:
        return

    context.outcome.close_now = True
    if not hooks.is_local_destination(interface, args.dest):
        hooks.cli_print(
            "Region/preset capabilities are available only from the local node."
        )
        return
    if not interface.regionPresets:
        hooks.cli_print(
            "This firmware did not provide usable region/preset compatibility metadata; "
            "preset choices remain unconstrained."
        )
        return

    for region, info in sorted(interface.regionPresets.items()):
        region_name = _enum_name_or_fallback(
            config_pb2.Config.LoRaConfig.RegionCode, region, "REGION_"
        )
        preset_names = [
            _enum_name_or_fallback(
                config_pb2.Config.LoRaConfig.ModemPreset, value, "PRESET_"
            )
            for value in info.presets
        ]
        default_name = _enum_name_or_fallback(
            config_pb2.Config.LoRaConfig.ModemPreset,
            info.default_preset,
            "PRESET_",
        )
        license_note = " licensed-only" if info.licensed_only else ""
        hooks.cli_print(
            f"{region_name}: default={default_name}{license_note}; "
            f"presets={','.join(preset_names)}"
        )


def _print_qr(
    url: str,
    *,
    description: str,
    qr_render: Callable[[str], str] | None,
    cli_print: Callable[[str], None],
) -> None:
    """Render a channel/contact URL and optional terminal QR through CLI reporting."""
    cli_print(f"{description}: {url}")
    if qr_render is None:
        cli_print("Install segno to view a QR code printed to terminal.")
        return
    cli_print(qr_render(url))


def _handle_channel_contact_display(
    context: CliContext, hooks: ChannelContactHooks
) -> None:
    """Render channel and contact URLs/QR codes in their historical CLI position."""
    args = context.args
    interface = context.interface

    if args.qr or args.qr_all:
        context.outcome.close_now = True
        url = interface.getNode(args.dest, True, **context.get_node_kwargs).getURL(
            includeAll=args.qr_all
        )
        description = (
            "Complete URL (includes all channels)"
            if args.qr_all
            else "Primary channel URL"
        )
        _print_qr(
            url,
            description=description,
            qr_render=hooks.qr_render,
            cli_print=hooks.cli_print,
        )

    if args.contact_qr:
        context.outcome.close_now = True
        url = interface.localNode.getContactURL(
            args.contact_qr,
            should_ignore=args.contact_ignore,
            manually_verified=args.contact_verified,
        )
        _print_qr(
            url,
            description="Contact URL",
            qr_render=hooks.qr_render,
            cli_print=hooks.cli_print,
        )
