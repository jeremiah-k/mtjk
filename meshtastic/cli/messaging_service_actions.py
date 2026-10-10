"""Connected CLI actions for messaging, reads, and long-running services."""

from __future__ import annotations

import argparse
import inspect
import io
import json
import logging
import platform
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from meshtastic._core_constants import BROADCAST_ADDR
from meshtastic.cli.context import CliContext, CliExit, _terminate_cli
from meshtastic.cli.parser import parse_cli_args
from meshtastic.cli.session_resources import SessionCleanup
from meshtastic.protobuf import portnums_pb2

logger = logging.getLogger(__name__)

GPIO_WATCH_INTERVAL_SECONDS = 1.0
GPIO_READ_POLL_INTERVAL_SECONDS = 1.0
GPIO_READ_MAX_POLLS = 10
GPIO_MASK_BITS = 64
GPIO_MASK_MAX = (1 << GPIO_MASK_BITS) - 1
INVALID_CHANNEL_MESSAGE = (
    "Warning: {index} is not a valid channel. Channel must not be DISABLED."
)
REQUIRE_DESTINATION_MESSAGE = "Warning: Must use a destination node ID."
INVALID_GPIO_PAIR_MESSAGE = "Warning: Invalid GPIO bit/value pair: {bit!r}={value!r}"
TELEMETRY_TYPE_ALIASES = {
    "device": "device_metrics",
    "device_metrics": "device_metrics",
    "environment": "environment_metrics",
    "environment_metrics": "environment_metrics",
    "air_quality": "air_quality_metrics",
    "airquality": "air_quality_metrics",
    "air_quality_metrics": "air_quality_metrics",
    "power": "power_metrics",
    "power_metrics": "power_metrics",
    "localstats": "local_stats",
    "local_stats": "local_stats",
}


class GetPrefHook(Protocol):
    """Callable contract for reading and displaying one preference path."""

    def __call__(
        self,
        node: Any,
        comp_name: str,
        *,
        allow_secrets: bool = ...,
        cli_print: Callable[[str], None] = ...,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class MessagingServiceHooks:
    """Compatibility and optional-subsystem seams for service actions."""

    cli_exit: CliExit
    cli_print: Callable[[str], None]
    get_channel_index: Callable[[], int | None]
    check_channel: Callable[[Any, int], bool]
    remote_hardware_client: Callable[[Any], Any]
    get_pref: GetPrefHook
    validate_cli_show_fields: Callable[[Any, list[str]], None]
    validate_cli_sort_field: Callable[[Any, str], None]
    newer_version: Callable[[], str | None]
    install_upgrade_hint: str
    powermon_available: Callable[[], bool]
    powermon_error: Callable[[], BaseException | None]
    log_set_factory: Callable[[Any, str | None, Any], Any] | None
    power_stress_factory: Callable[[Any], Any] | None
    get_meter: Callable[[], Any]
    platform_system: Callable[[], str] = platform.system
    sleep: Callable[[float], None] = time.sleep
    preference_print: Callable[[str], None] | None = None


def _selected_channel(hooks: MessagingServiceHooks) -> int:
    """Return the selected channel index, defaulting to the primary channel.

    Parameters
    ----------
    hooks : MessagingServiceHooks
        Entrypoint-owned channel-selection seam.

    Returns
    -------
    int
        Selected channel index, or ``0`` when no explicit channel is selected.
    """
    return hooks.get_channel_index() or 0


def _require_channel(interface: Any, hooks: MessagingServiceHooks) -> int:
    """Return the selected channel index after validating that it is enabled.

    Parameters
    ----------
    interface : Any
        Connected interface used for channel validation.
    hooks : MessagingServiceHooks
        Channel-selection, validation, and CLI-exit seams.

    Returns
    -------
    int
        Validated channel index.
    """
    channel_index = _selected_channel(hooks)
    if not hooks.check_channel(interface, channel_index):
        _terminate_cli(
            hooks.cli_exit, INVALID_CHANNEL_MESSAGE.format(index=channel_index)
        )
    return channel_index


_CONTROL_ESCAPES = {
    "\n": r"\n",
    "\r": r"\r",
    "\t": r"\t",
}


def _escape_terminal_controls(value: Any) -> str:
    """Render terminal control characters as inert escape text.

    Parameters
    ----------
    value : Any
        Remote/user-provided value destined for terminal output.

    Returns
    -------
    str
        Printable text with C0/C1 controls escaped and visible Unicode retained.
    """
    text = str(value)
    escaped: list[str] = []
    for character in text:
        codepoint = ord(character)
        if codepoint < 0x20 or 0x7F <= codepoint <= 0x9F:
            escaped.append(_CONTROL_ESCAPES.get(character, f"\\x{codepoint:02x}"))
        else:
            escaped.append(character)
    return "".join(escaped)


def _parse_gpio_mask(raw_value: Any, hooks: MessagingServiceHooks) -> int:
    """Parse one hexadecimal GPIO mask or terminate with a CLI diagnostic.

    Parameters
    ----------
    raw_value : Any
        Raw mask value supplied by argparse.
    hooks : MessagingServiceHooks
        CLI exit hook used for invalid input.

    Returns
    -------
    int
        Parsed hexadecimal bit mask.
    """
    try:
        mask = int(raw_value, 16)
    except (TypeError, ValueError):
        _terminate_cli(hooks.cli_exit, f"Warning: Invalid GPIO mask: {raw_value}", 1)
    if not 0 <= mask <= GPIO_MASK_MAX:
        _terminate_cli(
            hooks.cli_exit,
            f"Warning: GPIO mask must fit in {GPIO_MASK_BITS} bits: {raw_value}",
            1,
        )
    return mask


def _handle_messaging_actions(
    context: CliContext, hooks: MessagingServiceHooks
) -> None:
    """Send messages, requests, and remote-hardware operations in CLI order."""
    args = context.args
    interface = context.interface
    get_node_kwargs = context.get_node_kwargs

    if args.sendtext:
        context.outcome.close_now = True
        channel_index = _require_channel(interface, hooks)
        hooks.cli_print(
            f"Sending text message {args.sendtext} to {args.dest} "
            f"on channelIndex:{channel_index}"
            f" {'using PRIVATE_APP port' if args.private else ''}"
        )
        interface.sendText(
            args.sendtext,
            args.dest,
            wantAck=True,
            channelIndex=channel_index,
            onResponse=interface.getNode(args.dest, False, **get_node_kwargs).onAckNak,
            portNum=(
                portnums_pb2.PortNum.PRIVATE_APP
                if args.private
                else portnums_pb2.PortNum.TEXT_MESSAGE_APP
            ),
        )

    if args.traceroute:
        channel_index = _require_channel(interface, hooks)
        hop_limit = interface.localNode.localConfig.lora.hop_limit
        destination = str(args.traceroute)
        hooks.cli_print(
            f"Sending traceroute request to {destination} on "
            f"channelIndex:{channel_index} (this could take a while)"
        )
        interface.sendTraceRoute(destination, hop_limit, channelIndex=channel_index)

    if args.request_telemetry:
        if args.dest == BROADCAST_ADDR:
            _terminate_cli(hooks.cli_exit, REQUIRE_DESTINATION_MESSAGE)
        channel_index = _require_channel(interface, hooks)
        telemetry_type = TELEMETRY_TYPE_ALIASES.get(
            args.request_telemetry, "device_metrics"
        )
        hooks.cli_print(
            f"Sending {telemetry_type} telemetry request to {args.dest} on "
            f"channelIndex:{channel_index} (this could take a while)"
        )
        interface.sendTelemetry(
            destinationId=args.dest,
            wantResponse=True,
            channelIndex=channel_index,
            telemetryType=telemetry_type,
        )

    if args.request_position:
        if args.dest == BROADCAST_ADDR:
            _terminate_cli(hooks.cli_exit, REQUIRE_DESTINATION_MESSAGE)
        channel_index = _require_channel(interface, hooks)
        hooks.cli_print(
            f"Sending position request to {args.dest} on "
            f"channelIndex:{channel_index} (this could take a while)"
        )
        interface.sendPosition(
            destinationId=args.dest,
            wantResponse=True,
            channelIndex=channel_index,
        )

    if args.gpio_wrb or args.gpio_rd or args.gpio_watch:
        if args.dest == BROADCAST_ADDR:
            _terminate_cli(hooks.cli_exit, REQUIRE_DESTINATION_MESSAGE)
        client = hooks.remote_hardware_client(interface)

        if args.gpio_wrb:
            bitmask = 0
            bitval = 0
            for bit, value in args.gpio_wrb:
                try:
                    bit_index = int(bit)
                    bit_value = int(value)
                except (TypeError, ValueError):
                    _terminate_cli(
                        hooks.cli_exit,
                        INVALID_GPIO_PAIR_MESSAGE.format(bit=bit, value=value),
                        1,
                    )
                if not 0 <= bit_index < GPIO_MASK_BITS or bit_value not in {0, 1}:
                    _terminate_cli(
                        hooks.cli_exit,
                        INVALID_GPIO_PAIR_MESSAGE.format(bit=bit, value=value),
                        1,
                    )
                bitmask |= 1 << bit_index
                bitval |= bit_value << bit_index
            hooks.cli_print(
                f"Writing GPIO mask 0x{bitmask:x} with value 0x{bitval:x} "
                f"to {args.dest}"
            )
            client.writeGPIOs(args.dest, bitmask, bitval)
            context.outcome.close_now = True

        if args.gpio_rd:
            bitmask = _parse_gpio_mask(args.gpio_rd, hooks)
            hooks.cli_print(f"Reading GPIO mask 0x{bitmask:x} from {args.dest}")
            interface.mask = bitmask
            # Reset per-request state so a prior GPIO response cannot make this read
            # appear complete before its own callback arrives.
            interface.gotResponse = False
            client.readGPIOs(args.dest, bitmask, None)
            for _ in range(GPIO_READ_MAX_POLLS):
                hooks.sleep(GPIO_READ_POLL_INTERVAL_SECONDS)
                if interface.gotResponse:
                    break
            else:
                hooks.cli_print("Warning: no GPIO response received.")
            logger.debug("end of gpio_rd")

        if args.gpio_watch:
            bitmask = _parse_gpio_mask(args.gpio_watch, hooks)
            hooks.cli_print(
                f"Watching GPIO mask 0x{bitmask:x} from {args.dest}. "
                "Press ctrl-c to exit"
            )
            while True:
                client.watchGPIOs(args.dest, bitmask)
                hooks.sleep(GPIO_WATCH_INTERVAL_SECONDS)


def _handle_content_reads(context: CliContext, hooks: MessagingServiceHooks) -> None:
    """Read canned-message and ringtone content in their historical position."""
    args = context.args
    interface = context.interface
    output = hooks.preference_print or hooks.cli_print

    if args.get_canned_message:
        context.outcome.close_now = True
        output("")
        messages = interface.getNode(
            args.dest, **context.get_node_kwargs
        ).get_canned_message()
        output(f"canned_plugin_message:{_escape_terminal_controls(messages)}")

    if args.get_ringtone:
        context.outcome.close_now = True
        output("")
        ringtone = interface.getNode(
            args.dest, **context.get_node_kwargs
        ).get_ringtone()
        output(f"ringtone:{_escape_terminal_controls(ringtone)}")


def _call_get_pref(
    get_pref: GetPrefHook,
    node: Any,
    name: str,
    cli_print: Callable[[str], None],
) -> bool:
    """Support the original two-positional-argument preference hook contract.

    Select the calling convention from the hook signature, not by catching a
    TypeError raised inside the hook (which would incorrectly execute it twice).
    """
    try:
        signature = inspect.signature(get_pref)
    except (TypeError, ValueError):
        # An opaque callable is treated as the current sink-aware contract.
        return get_pref(node, name, cli_print=cli_print)
    try:
        signature.bind(node, name, cli_print=cli_print)
    except TypeError:
        signature.bind(node, name)
        return get_pref(node, name)
    return get_pref(node, name, cli_print=cli_print)


def _parse_sort_spec(spec: str) -> tuple[str, str | None]:
    """Split a --sort value into a field name and optional asc/desc direction.

    Parameters
    ----------
    spec : str
        Raw --sort value, for example "snr", "hwmodel:asc", or
        "deviceMetrics.batteryLevel:desc".

    Returns
    -------
    tuple[str, str | None]
        The field token and the lowercased direction, or None when no
        direction suffix was given.

    Raises
    ------
    ValueError
        When the field is empty or the direction suffix is not asc/desc.
    """
    field, separator, direction = spec.partition(":")
    field = field.strip()
    direction = direction.strip().lower()
    if not field:
        raise ValueError(f"--sort requires a field name, got '{spec}'")
    if separator and direction not in ("asc", "desc"):
        raise ValueError(f"--sort direction must be 'asc' or 'desc', got '{direction}'")
    return field, direction if separator else None


def _clean_filter_patterns(values: list[str] | None) -> list[str] | None:
    """Normalize comma-split filter values to stripped, casefolded substrings."""
    if not values:
        return None
    cleaned = [value.strip().casefold() for value in values]
    return [value for value in cleaned if value] or None


def _node_json_error(args: Any) -> str | None:
    """Keep a node JSON invocation limited to one result-producing action."""
    if not (getattr(args, "nodes", False) and getattr(args, "json", False)):
        return None
    # Compare with parser-owned defaults so additions fail closed until their
    # role in a JSON-only invocation is deliberately selected here.
    defaults = parse_cli_args(
        argparse.ArgumentParser(add_help=False), version="", argv=[]
    )
    allowed = {
        "nodes",
        "json",
        "role",
        "hwmodel",
        "sort",
        "limit",
        "dest",
        "port",
        "host",
        "ble",
        "ble_auto_reconnect",
        "timeout",
        "channel_fetch_attempts",
        "quiet",
        "ch_index",
        "no_time",
    }
    conflicts = sorted(
        "--" + name.replace("_", "-")
        for name, value in vars(args).items()
        if not name.startswith("_")
        and name not in allowed
        and value != getattr(defaults, name, None)
    )
    if conflicts:
        return "--nodes --json cannot be combined with: " + ", ".join(conflicts)
    return None


def _node_query_error(args: Any) -> str | None:
    """Validate query syntax before a transport or mutating action can run."""
    if getattr(args, "dry_run", False):
        return None
    flags = [
        flag
        for name, flag in (
            ("role", "--role"),
            ("hwmodel", "--hwmodel"),
            ("sort", "--sort"),
            ("limit", "--limit"),
        )
        if getattr(args, name, None) is not None
    ]
    if flags and not getattr(args, "nodes", False):
        return f"{', '.join(flags)} can only be used with --nodes"
    if (limit := getattr(args, "limit", None)) is not None and limit < 0:
        return "--limit expects a non-negative integer"
    for name in ("role", "hwmodel"):
        values = getattr(args, name, None)
        if values is not None and not _clean_filter_patterns(values):
            return f"--{name} requires at least one non-empty filter value"
    if (sort := getattr(args, "sort", None)) is not None:
        try:
            _parse_sort_spec(sort)
        except ValueError as error:
            return str(error)
    return None


def _validate_nodes_query(context: CliContext, hooks: MessagingServiceHooks) -> None:
    """Reject invalid embedded queries before connected device actions."""
    if error := _node_query_error(context.args) or _node_json_error(context.args):
        _terminate_cli(hooks.cli_exit, error, 1)
    if getattr(context.args, "dry_run", False) or not getattr(
        context.args, "nodes", False
    ):
        return
    if context.args.dest != BROADCAST_ADDR:
        _terminate_cli(
            hooks.cli_exit, "Showing node list of a remote node is not supported.", 1
        )
    if getattr(context.args, "show_fields", None):
        hooks.validate_cli_show_fields(context.interface, context.args.show_fields)
    if getattr(context.args, "sort", None) is not None:
        field, _direction = _parse_sort_spec(context.args.sort)
        hooks.validate_cli_sort_field(context.interface, field)


def _handle_information_actions(
    context: CliContext,
    hooks: MessagingServiceHooks,
    *,
    nodes_query_validated: bool = False,
) -> None:
    """Handle info, preference reads, node listing, and show-field validation."""
    args = context.args
    interface = context.interface
    if error := _node_query_error(args) or _node_json_error(args):
        context.outcome.close_now = True
        hooks.cli_print(error)
        context.outcome.stop_processing = True
        return

    if args.info:
        # Requested results stay visible even under --quiet and remain
        # capturable by embedded dispatchers, matching the --get policy.
        output = hooks.preference_print or hooks.cli_print
        output("")
        if args.dest == BROADCAST_ADDR:
            summary = io.StringIO()
            interface.showInfo(file=summary)
            output(summary.getvalue().rstrip("\n"))
            output("")
            interface.getNode(args.dest, **context.get_node_kwargs).showInfo(
                cli_print=output
            )
            context.outcome.close_now = True
            output("")
            pypi_version = hooks.newer_version()
            if pypi_version:
                output(
                    f"*** A newer version v{pypi_version} is available!"
                    f' Consider running "{hooks.install_upgrade_hint}" ***\n'
                )
        else:
            hooks.cli_print("Showing info of remote node is not supported.")
            hooks.cli_print(
                "Use the '--get' command for a specific configuration "
                "(e.g. 'lora') instead."
            )

    if args.get:
        context.outcome.close_now = True
        node = interface.getNode(args.dest, False, **context.get_node_kwargs)
        found = False
        # Preference data is a requested result, not a progress banner: keep
        # it visible even when the standalone CLI is running with --quiet.
        output = hooks.preference_print or hooks.cli_print
        for pref in args.get:
            found = _call_get_pref(hooks.get_pref, node, pref[0], output) or found
        if found:
            output("Completed getting preferences")

    if args.nodes:
        context.outcome.close_now = True
        if args.dest != BROADCAST_ADDR:
            hooks.cli_print("Showing node list of a remote node is not supported.")
            context.outcome.stop_processing = True
            return
        if args.show_fields and not nodes_query_validated:
            hooks.validate_cli_show_fields(interface, args.show_fields)
        sort_field: str | None = None
        sort_direction: str | None = None
        if args.sort is not None:
            sort_field, sort_direction = _parse_sort_spec(args.sort)
            if not nodes_query_validated:
                hooks.validate_cli_sort_field(interface, sort_field)
        query_kwargs: dict[str, Any] = {
            "roleFilter": _clean_filter_patterns(args.role),
            "hwModelFilter": _clean_filter_patterns(args.hwmodel),
            "sortField": sort_field or None,
            "sortDirection": sort_direction,
            "limit": args.limit if args.limit is not None else 0,
        }
        if getattr(args, "json", False):
            output = hooks.preference_print or hooks.cli_print
            result = interface.queryNodes(**query_kwargs)
            output(json.dumps(result.toDict(), indent=2, allow_nan=False))
            return
        interface.showNodes(
            True,
            args.show_fields,
            **query_kwargs,
        )

    if args.show_fields and not args.nodes:
        context.outcome.close_now = True
        hooks.cli_print("--show-fields can only be used with --nodes")
        context.outcome.stop_processing = True


def _start_tunnel(context: CliContext, hooks: MessagingServiceHooks) -> None:
    """Start the local tunnel service when platform and CLI state permit it.

    Parameters
    ----------
    context : CliContext
        Connected invocation and lifecycle state. ``close_now`` is cleared only
        when a tunnel is actually eligible to start.
    hooks : MessagingServiceHooks
        Platform, reporting, and exit seams.
    """
    args = context.args
    if hooks.platform_system() != "Linux" or not args.tunnel:
        return
    if args.dest != BROADCAST_ADDR:
        _terminate_cli(
            hooks.cli_exit, "A tunnel can only be created using the local node.", 1
        )

    if context.interface.noProto:
        logger.warning("Not starting Tunnel - disabled by noProto")
        return

    from meshtastic import tunnel  # pylint: disable=import-outside-toplevel

    if args.tunnel_net:
        tunnel_instance = tunnel.Tunnel(context.interface, subnet=args.tunnel_net)
    else:
        tunnel_instance = tunnel.Tunnel(context.interface)
    context.outcome.close_now = False
    context.outcome.failure_cleanup_callbacks.append(tunnel_instance.close)
    context.retain_failure_cleanup(tunnel_instance.close)


def _handle_long_running_services(
    context: CliContext, hooks: MessagingServiceHooks
) -> None:
    """Start structured logging, power stress, listening, and tunnel services."""
    args = context.args
    interface = context.interface

    log_set_close: Callable[[], None] | None = None
    log_set_session_cleanup: SessionCleanup | None = None
    if args.slog or args.power_stress:
        if not hooks.powermon_available():
            _terminate_cli(
                hooks.cli_exit,
                "The powermon module could not be loaded. "
                "You may need to run `uv sync --locked --group powermon`. "
                f"Import Error was: {hooks.powermon_error()}",
            )

        if args.slog:
            log_set_factory = hooks.log_set_factory
            if log_set_factory is None:
                _terminate_cli(
                    hooks.cli_exit,
                    "LogSet is required for --slog but not available. "
                    "The powermon module loaded incompletely.",
                )
            log_set = log_set_factory(
                interface,
                args.slog if args.slog != "default" else None,
                hooks.get_meter(),
            )
            log_set_close = log_set.close
            context.outcome.failure_cleanup_callbacks.append(log_set_close)
            log_set_session_cleanup = context.retain_failure_cleanup(log_set_close)
            context.outcome.close_now = False

        if args.power_stress:
            power_stress_factory = hooks.power_stress_factory
            if power_stress_factory is None:
                _terminate_cli(
                    hooks.cli_exit,
                    "PowerStress is required for --power-stress but not available. "
                    "The powermon module loaded incompletely.",
                )
            power_stress_factory(interface).run()
            if log_set_close is not None:
                if log_set_session_cleanup is not None:
                    log_set_session_cleanup.run()
                else:
                    log_set_close()
                    context.outcome.failure_cleanup_callbacks.remove(log_set_close)
            context.outcome.close_now = True

    if args.listen:
        context.outcome.close_now = False

    _start_tunnel(context, hooks)
