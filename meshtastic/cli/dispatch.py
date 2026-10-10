"""Connected CLI action dispatcher and lifecycle finalization."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass

from meshtastic._core_constants import BROADCAST_ADDR
from meshtastic.cli import (
    channel_contact_actions,
    configure_actions,
    device_actions,
    messaging_service_actions,
)
from meshtastic.cli.context import CliContext
from meshtastic.cli.invocation import _activate_cli_output

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
