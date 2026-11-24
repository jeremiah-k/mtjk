"""Connected CLI action dispatcher, hook assembly, and lifecycle finalization."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from google.protobuf.json_format import MessageToDict

import meshtastic.util
from meshtastic import key_verification, lockdown
from meshtastic._core_constants import BROADCAST_ADDR
from meshtastic.cli import (
    channel_contact_actions,
    config_io,
    configure_actions,
    device_actions,
    messaging_service_actions,
    preference_runtime,
    qr,
    set_actions,
)
from meshtastic.cli.context import CliContext, CliExit
from meshtastic.cli.invocation import (
    _activate_cli_output,
    current_camel_case,
    current_channel_index,
    set_current_channel_index,
)
from meshtastic.cli.parser import _MODEM_PRESET_SHORTHANDS
from meshtastic.cli.values import is_local_destination
from meshtastic.mesh_interface import MeshInterface
from meshtastic.remote_hardware import RemoteHardwareClient
from meshtastic.version import INSTALL_UPGRADE_HINT

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DispatchHooks:
    """Action hooks and compatibility seams used by connected dispatch."""

    cli_print: Callable[[str], None]
    device: device_actions.DeviceActionHooks
    channel_contact: channel_contact_actions.ChannelContactHooks
    configure: configure_actions.ConfigureActionHooks
    services: messaging_service_actions.MessagingServiceHooks
    sleep: Callable[[float], None] = time.sleep


def _export_config(interface: MeshInterface) -> str:
    """Export configuration through the cli runtime with the active naming mode."""
    return config_io.export_config(
        interface,
        camel_case=current_camel_case(),
        message_to_dict=MessageToDict,
        prefix_base64_bytes_fields_fn=config_io.prefix_base64_bytes_fields,
        set_missing_flags_false_fn=config_io.set_missing_flags_false,
        config_true_defaults=config_io.CONFIG_TRUE_DEFAULTS,
        module_true_defaults=config_io.MODULE_TRUE_DEFAULTS,
    )


def _build_connected_dispatch_hooks(
    *,
    cli_exit: CliExit,
    cli_print: Callable[..., None],
    powermon_available: Callable[[], bool],
    powermon_error: Callable[[], BaseException | None],
    get_meter: Callable[[], Any],
    log_set_factory: Callable[[Any, str | None, Any], Any] | None,
    power_stress_factory: Callable[[Any], Any] | None,
    sleep: Callable[[float], None] = time.sleep,
) -> DispatchHooks:
    """Assemble the connected dispatch hooks with entrypoint-owned effects.

    Process exit, user-facing reporting, the optional power-monitor probes,
    the optional logging/power-stress factories, and sleep belong to the CLI
    entrypoint and are supplied by the caller. Every action implementation
    bound below is owned by the cli package.

    Parameters
    ----------
    cli_exit : CliExit
        Entrypoint-owned exit effect.
    cli_print : Callable[..., None]
        Entrypoint-owned quiet-aware reporter; forced guidance passes
        ``force=True``.
    powermon_available : Callable[[], bool]
        Whether the optional power-monitor subsystem imported successfully.
    powermon_error : Callable[[], BaseException | None]
        Import failure of the optional power-monitor subsystem, if any.
    get_meter : Callable[[], Any]
        Accessor for the entrypoint's active power meter, if any.
    log_set_factory : Callable[[Any, str | None, Any], Any] | None
        Optional structured-log factory, available only with the slog extra.
    power_stress_factory : Callable[[Any], Any] | None
        Optional power-stress factory, available only with the powermon extra.
    sleep : Callable[[float], None]
        Sleep effect, defaulting to :func:`time.sleep`.

    Returns
    -------
    DispatchHooks
        Fully bound hooks for one connected dispatch.
    """

    def set_pref_current(config: Any, comp_name: str, raw_value: Any) -> bool:
        """Assign one preference with the injected reporter."""
        return set_actions._set_pref(config, comp_name, raw_value, cli_print=cli_print)

    def traverse_config(
        config_root: str,
        config: dict[str, Any],
        interface_config: Any,
        failed_fields: list[str] | None = None,
    ) -> bool:
        """Apply one nested configure mapping with the injected reporter."""
        return preference_runtime.traverse_config(
            config_root,
            config,
            interface_config,
            resolve_pref_fn=preference_runtime.resolve_pref,
            set_pref_fn=set_pref_current,
            failed_fields=failed_fields,
        )

    def handle_configure_command(
        interface: MeshInterface,
        args: Any,
        get_node_kwargs: dict[str, Any],
    ) -> configure_actions._ConfigureCommandResult:
        """Execute one configure document with the assembled configure seams."""
        return configure_actions._handle_configure_command(
            _configure_seams(), interface, args, get_node_kwargs
        )

    def preview_configure_command(
        interface: MeshInterface,
        args: Any,
        get_node_kwargs: dict[str, Any],
        snapshot: Any,
    ) -> None:
        """Preview one configure document with the assembled configure seams."""
        configure_actions._preview_configure_command(
            _configure_seams(), interface, args, get_node_kwargs, snapshot=snapshot
        )

    def _configure_seams() -> configure_actions.ConfigureHooks:
        """Build the configure execution seams for this dispatch."""
        return configure_actions.ConfigureHooks(
            cli_exit=cli_exit,
            cli_print=cli_print,
            traverse_config=traverse_config,
            preflight_mode=preference_runtime.CONFIGURE_PREFLIGHT_MODE,
            is_local_destination=is_local_destination,
            post_seturl_stability_check=configure_actions._post_seturl_stability_check,
            post_configure_reconnect_and_verify=(
                configure_actions._post_configure_reconnect_and_verify
            ),
            channel_url_matches_current_device_state=(
                configure_actions._channel_url_matches_current_device_state
            ),
            pace_configure_write=configure_actions._pace_configure_write,
        )

    device_hooks = device_actions.DeviceActionHooks(
        cli_exit=cli_exit,
        cli_print=cli_print,
        set_pref=set_pref_current,
        is_local_destination=is_local_destination,
        send_local_factory_reset_and_wait=(
            lambda reset_node, *, full, timeout=None: device_actions._send_local_factory_reset_and_wait(
                reset_node, full=full, timeout=timeout, cli_print=cli_print
            )
        ),
        post_factory_reset_ready_probe=device_actions._post_factory_reset_ready_probe,
        handle_ota_update=(
            lambda interface, args, get_node_kwargs: device_actions._handle_ota_update(
                interface,
                args,
                get_node_kwargs,
                cli_exit=cli_exit,
                cli_print=cli_print,
                is_local_destination=is_local_destination,
            )
        ),
        build_lockdown_auth=lockdown.build_lockdown_auth,
        read_lockdown_passphrase_file=lockdown.read_lockdown_passphrase_file,
        send_lockdown_auth=lockdown.send_lockdown_auth,
        validate_lockdown_passphrase=lockdown.validate_lockdown_passphrase,
        build_key_verification_admin=key_verification.buildKeyVerificationAdmin,
        send_key_verification=key_verification.sendKeyVerification,
    )
    channel_contact_hooks = channel_contact_actions.ChannelContactHooks(
        cli_exit=cli_exit,
        cli_print=cli_print,
        get_channel_index=current_channel_index,
        set_channel_index=set_current_channel_index,
        resolve_pref=preference_runtime.resolve_pref,
        set_pref=set_pref_current,
        fatal_preference_value_errors=preference_runtime.fatal_preference_value_errors,
        preference_value_error=preference_runtime.PreferenceValueError,
        print_channel_field_choices=lambda settings, name: (
            channel_contact_actions._print_channel_field_choices(
                settings,
                name,
                cli_print=lambda message: cli_print(message, force=True),
            )
        ),
        is_local_destination=is_local_destination,
        modem_preset_shorthands=_MODEM_PRESET_SHORTHANDS,
        qr_render=(qr.renderTerminalQr if qr.segno is not None else None),
    )
    configure_action_hooks = configure_actions.ConfigureActionHooks(
        handle_set_command=(
            lambda interface, args, get_node_kwargs: set_actions._handle_set_command(
                interface,
                args,
                get_node_kwargs,
                cli_exit=cli_exit,
                cli_print=cli_print,
                sleep=sleep,
            )
        ),
        handle_configure_command=handle_configure_command,
        export_config=_export_config,
        cli_exit=cli_exit,
        cli_print=cli_print,
        is_local_destination=is_local_destination,
        preview_set_command=(
            lambda interface, args, get_node_kwargs: set_actions._preview_set_command(
                interface,
                args,
                get_node_kwargs,
                cli_exit=cli_exit,
                cli_print=cli_print,
            )
        ),
        preview_configure_command=preview_configure_command,
    )
    service_hooks = messaging_service_actions.MessagingServiceHooks(
        cli_exit=cli_exit,
        cli_print=cli_print,
        preference_print=lambda message: cli_print(message, force=True),
        get_channel_index=current_channel_index,
        check_channel=channel_contact_actions.check_channel,
        remote_hardware_client=RemoteHardwareClient,
        get_pref=preference_runtime.get_pref,
        validate_cli_show_fields=lambda interface, fields: (
            messaging_service_actions._validate_cli_show_fields(
                interface, fields, cli_exit=cli_exit
            )
        ),
        validate_cli_sort_field=lambda interface, field: (
            messaging_service_actions._validate_cli_sort_field(
                interface, field, cli_exit=cli_exit
            )
        ),
        newer_version=meshtastic.util.check_if_newer_version,
        install_upgrade_hint=INSTALL_UPGRADE_HINT,
        powermon_available=powermon_available,
        powermon_error=powermon_error,
        log_set_factory=log_set_factory,
        power_stress_factory=power_stress_factory,
        get_meter=get_meter,
        sleep=sleep,
    )
    return DispatchHooks(
        cli_print=cli_print,
        device=device_hooks,
        channel_contact=channel_contact_hooks,
        configure=configure_action_hooks,
        services=service_hooks,
        sleep=sleep,
    )


def _print_connection(context: CliContext, hooks: DispatchHooks) -> None:
    """Print the historical connection banner unless config export is active."""
    if context.args.export_config or (
        getattr(context.args, "json", False) and getattr(context.args, "nodes", False)
    ):
        return

    dev_path = getattr(context.interface, "devPath", "")
    if not dev_path:
        hooks.cli_print("Connected to radio")
        return

    tty_name = os.path.basename(dev_path)
    stable_path = getattr(context.interface, "_stable_path", None)
    if stable_path and stable_path != dev_path:
        hooks.cli_print(f"Connected to radio on {tty_name} (stable: {stable_path})")
    else:
        hooks.cli_print(f"Connected to radio on {tty_name}")


def _finalize_connected_actions(context: CliContext, hooks: DispatchHooks) -> None:
    """Perform final ACK handling and any requested disconnect delay."""
    args = context.args
    outcome = context.outcome
    interface = context.interface

    if not outcome.skip_ack_wait and (
        args.ack or (args.dest != BROADCAST_ADDR and outcome.wait_for_ack_nak)
    ):
        hooks.cli_print(
            "Waiting for an acknowledgment from remote node (this could take a while)"
        )
        interface.getNode(
            args.dest, False, **context.get_node_kwargs
        ).iface.waitForAckNak()

    if args.wait_to_disconnect:
        hooks.cli_print(
            f"Waiting {args.wait_to_disconnect} seconds before disconnecting"
        )
        hooks.sleep(int(args.wait_to_disconnect))


def _close_interface_if_requested(context: CliContext) -> None:
    """Close a one-shot interface at most once despite earlier lifecycle failures."""
    args = context.args
    outcome = context.outcome
    if (
        not context.owns_interface
        or args.seriallog
        or not outcome.close_now
        or outcome.interface_close_attempted
    ):
        return

    outcome.interface_close_attempted = True
    try:
        context.interface.close()
    except Exception:
        logger.debug("Error during interface close", exc_info=True)


def _cleanup_failed_resources(context: CliContext) -> BaseException | None:
    """Roll back retained services and return the highest-priority failure.

    Control-flow exceptions such as :class:`KeyboardInterrupt` and
    :class:`SystemExit` take precedence over ordinary cleanup exceptions so a
    later rollback callback cannot accidentally suppress an operator interrupt.
    Within the same priority class, the first failure in rollback order wins.

    Returns
    -------
    BaseException | None
        Highest-priority rollback failure, or ``None`` when every callback
        completes.
    """
    errors: list[BaseException] = []
    for cleanup in reversed(context.outcome.failure_cleanup_callbacks):
        try:
            cleanup()
        except BaseException as exc:  # noqa: BLE001 - preserve primary action failure
            errors.append(exc)
    context.outcome.failure_cleanup_callbacks.clear()
    if not errors:
        return None

    selected_error = next(
        (error for error in errors if not isinstance(error, Exception)), errors[0]
    )
    for error in errors:
        if error is selected_error:
            continue
        logger.warning(
            "Additional connected-action cleanup failed",
            exc_info=(type(error), error, error.__traceback__),
        )
    return selected_error


def _disarm_failure_cleanups(context: CliContext) -> None:
    """Discard rollback callbacks without stopping successfully started services."""
    context.outcome.failure_cleanup_callbacks.clear()


def _dispatch_connected(context: CliContext, hooks: DispatchHooks) -> None:
    """Execute all connected CLI action groups in their historical order."""
    with _activate_cli_output(hooks.cli_print):
        _run_connected_actions(context, hooks)


def _run_connected_actions(context: CliContext, hooks: DispatchHooks) -> None:
    """Run actions while the entrypoint reporters share the dispatch sink."""
    outcome = context.outcome
    action_error: BaseException | None = None
    try:
        messaging_service_actions._validate_nodes_query(context, hooks.services)
        if context.owns_interface:
            _print_connection(context, hooks)

        # Action modules are internal implementation packages; these intentionally
        # private entrypoints avoid expanding the backwards-compatible public API.
        device_actions._handle_device_actions(context, hooks.device)

        if not outcome.stop_processing:
            channel_contact_actions._handle_contact_import(context)
            messaging_service_actions._handle_messaging_actions(context, hooks.services)
            configure_actions._handle_configure_actions(context, hooks.configure)

        if not outcome.stop_processing:
            channel_contact_actions._handle_channel_mutations(
                context, hooks.channel_contact
            )
            messaging_service_actions._handle_content_reads(context, hooks.services)
            channel_contact_actions._handle_region_preset_display(
                context, hooks.channel_contact
            )
            device_actions._handle_lockdown_action(context, hooks.device)
            device_actions._handle_key_verification_action(context, hooks.device)
            messaging_service_actions._handle_information_actions(
                context, hooks.services, nodes_query_validated=True
            )

        if not outcome.stop_processing:
            channel_contact_actions._handle_channel_contact_display(
                context, hooks.channel_contact
            )
            messaging_service_actions._handle_long_running_services(
                context, hooks.services
            )

        _finalize_connected_actions(context, hooks)
    except BaseException as exc:
        action_error = exc
        raise
    finally:
        cleanup_error: BaseException | None = None
        if action_error is None:
            _disarm_failure_cleanups(context)
        else:
            cleanup_error = _cleanup_failed_resources(context)

        _close_interface_if_requested(context)

        if cleanup_error is not None:
            # Control-flow BaseExceptions (for example KeyboardInterrupt/SystemExit)
            # raised by cleanup must remain observable, even while another error is
            # unwinding. The original action failure remains on ``__context__``.
            if not isinstance(cleanup_error, Exception):
                raise cleanup_error
            logger.warning(
                "Connected-action cleanup failed while unwinding another error",
                exc_info=(
                    type(cleanup_error),
                    cleanup_error,
                    cleanup_error.__traceback__,
                ),
            )
