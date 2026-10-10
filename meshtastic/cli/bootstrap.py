"""CLI bootstrap and transport-session orchestration.

This module owns pre-connect validation, transport selection, invocation resources,
and the optional long-running listen loop. The public ``meshtastic.__main__.common``
function remains the compatibility entrypoint and supplies its historical seams
through :class:`BootstrapHooks`.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import platform
import sys
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from typing import IO, Any, NoReturn

import meshtastic.cli.config_preview as cli_config_preview
from meshtastic._core_constants import BROADCAST_ADDR
from meshtastic.cli.context import CliExit
from meshtastic.cli.context import _terminate_cli as _terminate_cli_with_exit
from meshtastic.cli.messaging_service_actions import _node_query_error
from meshtastic.cli.session_resources import CliSessionResources
from meshtastic.mesh_interface import MeshInterface

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BootstrapHooks:  # pylint: disable=too-many-instance-attributes
    """Entrypoint-owned dependencies used by CLI bootstrap orchestration."""

    cli_exit: CliExit
    support_info: Callable[[], None]
    print_available_config_fields: Callable[..., None]
    describe_config_field: Callable[..., bool]
    create_power_meter: Callable[[], None]
    get_power_meter: Callable[[], Any]
    release_power_meter: Callable[[Any], None]
    set_logfile: Callable[[IO[str] | None], None]
    clear_session_logfile: Callable[[IO[str]], None]
    subscribe: Callable[[], None]
    unsubscribe_receive: Callable[[], None]
    on_connected: Callable[[MeshInterface], None]
    parse_host_port: Callable[[str, int], tuple[str, int]]
    listen_loop_poll_once: Callable[[MeshInterface], bool]
    set_channel_index: Callable[[int], None]
    ble_interface: Any
    tcp_interface: Any
    default_tcp_port: int
    serial_interface: Any
    mesh_interface_error: type[Exception]
    test_module: Any


def _terminate_cli(
    hooks: BootstrapHooks, message: str, return_value: int = 1
) -> NoReturn:
    """Invoke the entrypoint exit seam and fail closed if an injected seam returns."""
    _terminate_cli_with_exit(hooks.cli_exit, message, return_value)


def _configure_logging(args: argparse.Namespace) -> None:
    """Configure process logging from parsed CLI flags."""
    if args.quiet:
        log_level = logging.WARNING
    elif args.debug or args.listen:
        log_level = logging.DEBUG
    else:
        log_level = logging.INFO

    logging.basicConfig(
        level=log_level,
        format="%(levelname)s file:%(filename)s %(funcName)s line:%(lineno)s %(message)s",
    )
    if not (args.debug or args.listen or args.quiet) and args.debuglib:
        logging.getLogger("meshtastic").setLevel(logging.DEBUG)


_DryRunActionFlag = tuple[str, str]


_DRY_RUN_CONFLICTING_ACTIONS: tuple[_DryRunActionFlag, ...] = (
    # (parsed attribute, user-facing flag label). Every argument that performs a
    # device action, hardware access, long-running loop, pre-connect exit, or
    # file-producing/read query is refused with --dry-run so a preview
    # invocation can never reach a mutating or blocking code path. Connection
    # selection and global output flags (for example --host, --dest, --debug,
    # --quiet, --seriallog) are intentionally absent. ``--ch-index`` is also
    # refused on purpose: a channel selector is meaningless for a preview, so
    # ambiguous selectors are conservatively rejected rather than silently
    # previewed against the wrong channel.
    ("restore_preferences", "--restore-preferences"),
    ("store_ui_config", "--store-ui-config"),
    ("get_ui_config", "--get-ui-config"),
    ("toggle_muted_node", "--toggle-muted-node"),
    ("remove_position", "--remove-position"),
    ("request_connection_status", "--request-connection-status"),
    ("delete_file", "--delete-file"),
    ("send_input_event", "--send-input-event"),
    ("input_kb_char", "--input-kb-char"),
    ("input_touch_x", "--input-touch-x"),
    ("input_touch_y", "--input-touch-y"),
    ("key_verify", "--key-verify"),
    ("ch_preset", "--ch-preset"),
    ("ch_vlongslow", "--ch-vlongslow"),
    ("ch_longslow", "--ch-longslow"),
    ("ch_longmod", "--ch-longmod"),
    ("ch_longfast", "--ch-longfast"),
    ("ch_longturbo", "--ch-longturbo"),
    ("ch_medslow", "--ch-medslow"),
    ("ch_medfast", "--ch-medfast"),
    ("ch_shortslow", "--ch-shortslow"),
    ("ch_shortfast", "--ch-shortfast"),
    ("ch_shortturbo", "--ch-shortturbo"),
    ("reboot", "--reboot"),
    ("reboot_ota", "--reboot-ota"),
    ("enter_dfu", "--enter-dfu"),
    ("shutdown", "--shutdown"),
    ("factory_reset", "--factory-reset"),
    ("factory_reset_device", "--factory-reset-device"),
    ("reset_nodedb", "--reset-nodedb"),
    ("backup_preferences", "--backup-preferences"),
    ("remove_backup_preferences", "--remove-backup-preferences"),
    ("remove_node", "--remove-node"),
    ("remove_favorite_node", "--remove-favorite-node"),
    ("remove_ignored_node", "--remove-ignored-node"),
    ("set_favorite_node", "--set-favorite-node"),
    ("set_ignored_node", "--set-ignored-node"),
    ("set_time", "--set-time"),
    ("traceroute", "--traceroute"),
    ("request_position", "--request-position"),
    ("request_telemetry", "--request-telemetry"),
    ("device_metadata", "--device-metadata"),
    ("gpio_rd", "--gpio-rd"),
    ("gpio_wrb", "--gpio-wrb"),
    ("gpio_watch", "--gpio-watch"),
    ("ota_update", "--ota-update"),
    ("info", "--info"),
    ("nodes", "--nodes"),
    ("pos_fields", "--pos-fields"),
    ("show_fields", "--show-fields"),
    ("ack", "--ack"),
    ("lockdown_passphrase", "--lockdown-passphrase"),
    ("lockdown_passphrase_file", "--lockdown-passphrase-file"),
    (
        "insecure_lockdown_passphrase_on_command_line",
        "--insecure-lockdown-passphrase-on-command-line",
    ),
    ("lockdown_provision", "--lockdown-provision"),
    ("lockdown_lock_now", "--lockdown-lock-now"),
    ("lockdown_unlock", "--lockdown-unlock"),
    ("lockdown_disable", "--lockdown-disable"),
    ("lockdown_yes", "--lockdown-yes"),
    ("set_owner", "--set-owner"),
    ("set_owner_short", "--set-owner-short"),
    ("set_ham", "--set-ham"),
    ("set_is_unmessageable", "--set-is-unmessageable"),
    ("setalt", "--setalt"),
    ("setlat", "--setlat"),
    ("setlon", "--setlon"),
    ("set_canned_message", "--set-canned-message"),
    ("set_ringtone", "--set-ringtone"),
    ("begin_edit", "--begin-edit"),
    ("commit_edit", "--commit-edit"),
    ("get", "--get"),
    ("get_canned_message", "--get-canned-message"),
    ("get_ringtone", "--get-ringtone"),
    ("ch_set", "--ch-set"),
    ("ch_add", "--ch-add"),
    ("ch_add_url", "--ch-add-url"),
    ("ch_del", "--ch-del"),
    ("ch_disable", "--ch-disable"),
    ("ch_enable", "--ch-enable"),
    ("ch_set_url", "--seturl"),
    # Deliberately rejected despite being "only" a selector: --ch-index cannot
    # meaningfully scope a preview, so ambiguous selectors are refused.
    ("ch_index", "--ch-index"),
    ("show_region_presets", "--show-region-presets"),
    ("contact_qr", "--contact-qr"),
    ("add_contact", "--add-contact"),
    ("contact_verified", "--contact-verified"),
    ("contact_ignore", "--contact-ignore"),
    ("sendtext", "--sendtext"),
    ("reply", "--reply"),
    ("listen", "--listen"),
    ("tunnel", "--tunnel"),
    ("tunnel_net", "--tunnel-net"),
    ("noproto", "--noproto"),
    ("wait_to_disconnect", "--wait-to-disconnect"),
    ("qr", "--qr"),
    ("qr_all", "--qr-all"),
    ("export_config", "--export-config"),
    ("slog", "--slog"),
    ("test", "--test"),
    ("support", "--support"),
    ("deprecated", "--deprecated"),
    ("list_fields", "--list-fields"),
    ("describe_field", "--describe-field"),
    ("ble_scan", "--ble-scan"),
    ("power_riden", "--power-riden"),
    ("power_ppk2_meter", "--power-ppk2-meter"),
    ("power_ppk2_supply", "--power-ppk2-supply"),
    ("power_sim", "--power-sim"),
    ("power_stress", "--power-stress"),
    ("power_wait", "--power-wait"),
    ("power_voltage", "--power-voltage"),
)


def _action_requested(value: Any) -> bool:
    """Return whether one parsed action argument was actually provided."""
    return value is not None and value is not False and value != []


def _validate_dry_run_invocation(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> None:
    """Reject ``--dry-run`` invocations that cannot be previewed safely.

    The preview workflow supports only ``--set`` and ``--configure`` batches.
    Every other action argument is refused before pre-connect actions run and
    before any transport is initialized, so an unrelated mutating or
    long-running flag can never execute underneath a preview.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed CLI arguments for this invocation.
    parser : argparse.ArgumentParser
        Parser that owns usage/error reporting.
    """
    if not (args.set or args.configure):
        parser.error("--dry-run requires --set or --configure")
    conflicts = [
        label
        for attribute, label in _DRY_RUN_CONFLICTING_ACTIONS
        if _action_requested(getattr(args, attribute, None))
    ]
    if conflicts:
        parser.error(
            "--dry-run only supports --set and --configure; remove: "
            + ", ".join(conflicts)
        )


def _validate_and_normalize_args(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    hooks: BootstrapHooks,
) -> None:
    """Validate pre-connect arguments and apply historical default mutations."""
    if cli_config_preview.preview_requested(args):
        _validate_dry_run_invocation(args, parser)
    if error := _node_query_error(args):
        parser.error(error)
    if args.quiet and (args.debug or args.listen or args.debuglib):
        parser.error("--quiet cannot be used with --debug, --listen, or --debuglib")
    if (args.contact_verified or args.contact_ignore) and not args.contact_qr:
        parser.error("--contact-verified and --contact-ignore require --contact-qr")
    if args.configure and len(args.configure) != 1:
        parser.error("--configure may be specified only once per invocation")
    if getattr(args, "json", False) and not (
        args.list_fields or getattr(args, "describe_field", None) is not None
    ):
        parser.error("--json requires --list-fields or --describe-field")

    for value, label in (
        (args.set_owner, "Long Name"),
        (args.set_owner_short, "Short Name"),
        (args.set_ham, "Ham radio callsign"),
    ):
        if value is not None and not value.strip():
            _terminate_cli(
                hooks,
                f"ERROR: {label} cannot be empty or contain only whitespace characters",
                1,
            )

    if args.ota_update is not None and not os.path.isfile(args.ota_update):
        _terminate_cli(
            hooks, f"Error: OTA firmware file not found: {args.ota_update}", 1
        )
    if args.ota_update is not None or args.reboot_ota:
        # Always skip node loading for OTA. The OTA path resolves LOCAL_ADDR
        # directly to local_node and does not need the node database; the
        # same holds for rebootOTA, where a large node DB dump can stall the
        # link until the firmware closes the connection before the reboot
        # request is processed.
        args.no_nodes = True

    if args.ch_index is not None:
        hooks.set_channel_index(int(args.ch_index))
    if not args.dest:
        args.dest = BROADCAST_ADDR
    if not args.seriallog:
        args.seriallog = "stdout" if args.noproto else "none"


def _run_preconnect_action(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    hooks: BootstrapHooks,
    *,
    argv: Sequence[str],
) -> bool:
    """Run an action that does not require a device and report whether it handled CLI flow."""
    if len(argv) == 1:
        parser.print_help(sys.stderr)
        _terminate_cli(hooks, "", 1)
    if args.support:
        hooks.support_info()
        _terminate_cli(hooks, "", 0)
    as_json = getattr(args, "json", False)
    if args.list_fields:
        hooks.print_available_config_fields(as_json=as_json)
        return True
    describe_field = getattr(args, "describe_field", None)
    if describe_field is not None:
        if not hooks.describe_config_field(describe_field, as_json=as_json):
            _terminate_cli(hooks, f"Unknown configurable field: {describe_field}", 1)
        return True
    if args.deprecated is not None:
        logger.error(
            "This option has been deprecated, see help below for the correct replacement..."
        )
        parser.print_help(sys.stderr)
        _terminate_cli(hooks, "", 1)
    if not args.test:
        return False
    if hooks.test_module is None:
        _terminate_cli(
            hooks,
            "Test module could not be imported.",
            1,
        )
    result = hooks.test_module.testAll()
    if not result:
        _terminate_cli(hooks, "Warning: Test was not successful.", 1)
    _terminate_cli(hooks, "Test was a success.", 0)


def _open_serial_transport(
    args: argparse.Namespace,
    hooks: BootstrapHooks,
    session: CliSessionResources,
    logfile: IO[str] | None,
) -> MeshInterface:
    """Open serial transport, preserving localhost TCP fallback semantics."""
    try:
        client = session.enter_context(
            hooks.serial_interface(
                args.port,
                debugOut=logfile,
                noProto=args.noproto,
                noNodes=args.no_nodes,
                timeout=args.timeout,
            )
        )
    except FileNotFoundError:
        message = (
            "File Not Found Error:\n"
            f"  The serial device at '{args.port}' was not found.\n"
            "  Please check the following:\n"
            "    1. Is the device connected properly?\n"
            "    2. Is the correct serial port specified?\n"
            "    3. Are the necessary drivers installed?\n"
            "    4. Are you using a **power-only USB cable**? A power-only cable cannot transmit data.\n"
            "       Ensure you are using a **data-capable USB cable**.\n"
        )
        _terminate_cli(hooks, message, 1)
    except PermissionError as exc:
        try:
            username = os.getlogin()
        except OSError:
            username = getpass.getuser()
        message = (
            "Permission Error:\n"
            "  Need to add yourself to the 'dialout' group by running:\n"
            f"     sudo usermod -a -G dialout {username}\n"
            "  After running that command, log out and re-login for it to take effect.\n"
            f"Error was: {exc}"
        )
        _terminate_cli(hooks, message, 1)
    except hooks.mesh_interface_error as exc:
        _terminate_cli(hooks, f"[Serial] {exc}", 1)
    except OSError as exc:
        message = (
            "OS Error:\n"
            "  The serial device couldn't be opened, it might be in use by another process.\n"
            "  Please close any applications or webpages that may be using the device and try again.\n"
            f"\nOriginal error: {exc}"
        )
        _terminate_cli(hooks, message, 1)

    if client.devPath is not None:
        return client

    logger.info(
        "Serial device unavailable after initialization; falling back to localhost TCP interface."
    )
    try:
        return session.enter_context(
            hooks.tcp_interface(
                "localhost",
                debugOut=logfile,
                noProto=args.noproto,
                noNodes=args.no_nodes,
                timeout=args.timeout,
            )
        )
    except hooks.mesh_interface_error as exc:
        _terminate_cli(hooks, f"[TCP localhost] {exc}", 1)
    except OSError as exc:
        _terminate_cli(
            hooks,
            f"No Meshtastic device detected and no TCP listener on localhost: {exc}",
            1,
        )


def _open_transport(
    args: argparse.Namespace,
    hooks: BootstrapHooks,
    session: CliSessionResources,
    logfile: IO[str] | None,
) -> MeshInterface:
    """Open the selected BLE, TCP, or serial transport under invocation ownership."""
    if args.ble:
        try:
            return session.enter_context(
                hooks.ble_interface(
                    args.ble if args.ble != "any" else None,
                    debugOut=logfile,
                    noProto=args.noproto,
                    noNodes=args.no_nodes,
                    timeout=args.timeout,
                    auto_reconnect=args.ble_auto_reconnect,
                )
            )
        except hooks.ble_interface.BLEError as exc:
            _terminate_cli(hooks, f"[BLE] {exc}", 1)
        except hooks.mesh_interface_error as exc:
            _terminate_cli(hooks, f"[BLE] {exc}", 1)

    if args.host:
        tcp_hostname, tcp_port = hooks.parse_host_port(
            args.host, hooks.default_tcp_port
        )
        try:
            return session.enter_context(
                hooks.tcp_interface(
                    tcp_hostname,
                    portNumber=tcp_port,
                    debugOut=logfile,
                    noProto=args.noproto,
                    noNodes=args.no_nodes,
                    timeout=args.timeout,
                )
            )
        except (hooks.mesh_interface_error, OSError) as exc:
            _terminate_cli(
                hooks, f"Error connecting to {tcp_hostname}:{tcp_port}: {exc}", 1
            )

    return _open_serial_transport(args, hooks, session, logfile)


def _open_serial_log(
    args: argparse.Namespace, hooks: BootstrapHooks, session: CliSessionResources
) -> IO[str] | None:
    """Open the requested serial debug stream and mirror its legacy global state."""
    hooks.set_logfile(None)
    if args.seriallog == "stdout":
        return sys.stdout
    if args.seriallog == "none":
        args.seriallog = None
        logger.debug("Not logging serial output")
        return None

    logger.info("Logging serial output to %s", args.seriallog)
    logfile = session.enter_context(
        open(  # pylint: disable=consider-using-with
            args.seriallog, "w+", buffering=1, encoding="utf8"
        )
    )
    hooks.set_logfile(logfile)
    session.register_cleanup(lambda: hooks.clear_session_logfile(logfile))
    return logfile


def _run_connected_session(
    args: argparse.Namespace,
    hooks: BootstrapHooks,
) -> None:
    """Own all invocation resources while running connected CLI actions."""
    with ExitStack() as stack:
        session = CliSessionResources(stack)
        session.activate()

        if any(
            (
                args.power_riden,
                args.power_ppk2_meter,
                args.power_ppk2_supply,
                args.power_sim,
                args.power_voltage is not None,
            )
        ):
            hooks.create_power_meter()
            active_meter = hooks.get_power_meter()
            if active_meter is not None:
                session.register_cleanup(
                    lambda: hooks.release_power_meter(active_meter)
                )

        logfile = _open_serial_log(args, hooks, session)
        hooks.subscribe()
        session.register_cleanup(hooks.unsubscribe_receive)

        if args.ble_scan:
            logger.debug("BLE scan starting")
            for device in hooks.ble_interface.scan():
                print(f"Found: name='{device.name}' address='{device.address}'")
            _terminate_cli(hooks, "BLE scan finished", 0)

        client = _open_transport(args, hooks, session, logfile)
        hooks.on_connected(client)

        have_tunnel = platform.system() == "Linux"
        if not (
            args.noproto or args.reply or (have_tunnel and args.tunnel) or args.listen
        ):
            return
        try:
            while True:
                # Return value is intentionally ignored: False means "sleep
                # already handled, continue normally"; True means "reconnect
                # timing was self-contained". Both values iterate again.
                # Every branch sleeps, so there is no busy-wait.
                hooks.listen_loop_poll_once(client)
        except KeyboardInterrupt:
            logger.info("Exiting due to keyboard interrupt")


def run_common(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    hooks: BootstrapHooks,
    *,
    argv: Sequence[str] | None = None,
) -> None:
    """Run the CLI pre-connect and connected-session bootstrap flow."""
    actual_argv = sys.argv if argv is None else argv
    _validate_and_normalize_args(args, parser, hooks)
    _configure_logging(args)
    if _run_preconnect_action(args, parser, hooks, argv=actual_argv):
        return
    _run_connected_session(args, hooks)
