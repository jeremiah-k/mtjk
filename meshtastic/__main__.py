"""Main Meshtastic."""

# pylint: disable=R0917,C0302

import argparse
import contextlib
import getpass  # noqa: F401  # pylint: disable=unused-import  # compatibility seam
import importlib
import logging
import os
import platform
import signal
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from types import FrameType, ModuleType
from typing import IO, Any, NoReturn, Protocol

from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.json_format import MessageToDict
from pubsub import pub

import meshtastic.cli.bootstrap as cli_bootstrap
import meshtastic.cli.channel_contact_actions as cli_channel_contact_actions
import meshtastic.cli.config_io as cli_config_io
import meshtastic.cli.config_preview as cli_config_preview
import meshtastic.cli.configure_actions as cli_configure_actions
import meshtastic.cli.device_actions as cli_device_actions
import meshtastic.cli.dispatch as cli_dispatch
import meshtastic.cli.invocation as cli_invocation
import meshtastic.cli.messaging_service_actions as cli_messaging_service_actions
import meshtastic.cli.notification_rendering as cli_notification_rendering
import meshtastic.cli.preference_runtime as cli_preference_runtime
import meshtastic.cli.runtime as cli_runtime
import meshtastic.cli.set_actions as cli_set_actions
import meshtastic.cli.values as cli_values
import meshtastic.ota
import meshtastic.serial_interface
import meshtastic.tcp_interface
import meshtastic.util
from meshtastic import _self_signal, _topics, mt_config
from meshtastic._branding import (
    PRIMARY_CLI_NAME,
    PROJECT_ISSUE_URL,
    _format_cli_version,
)

# COMPAT_STABLE_SHIM: LOCAL_ADDR remains importable from meshtastic.__main__.
# pylint: disable=unused-import
from meshtastic._core_constants import BROADCAST_ADDR  # noqa: F401
from meshtastic._core_constants import LOCAL_ADDR  # noqa: F401
from meshtastic.cli.context import ActionOutcome, CliContext

# COMPAT_STABLE_SHIM: Preserve legacy imports from meshtastic.cli.parser.
# pylint: disable=unused-import,useless-import-alias
from meshtastic.cli.parser import addChannelConfigArgs as addChannelConfigArgs
from meshtastic.cli.parser import addConfigArgs as addConfigArgs
from meshtastic.cli.parser import addConnectionArgs as addConnectionArgs
from meshtastic.cli.parser import addImportExportArgs as addImportExportArgs
from meshtastic.cli.parser import addLocalActionArgs as addLocalActionArgs
from meshtastic.cli.parser import addPositionConfigArgs as addPositionConfigArgs
from meshtastic.cli.parser import addRemoteActionArgs as addRemoteActionArgs
from meshtastic.cli.parser import addRemoteAdminArgs as addRemoteAdminArgs
from meshtastic.cli.parser import addSelectionArgs as addSelectionArgs
from meshtastic.cli.parser import parse_cli_args as parse_cli_args
from meshtastic.cli.values import is_local_destination as _is_local_destination
from meshtastic.cli.values import (  # noqa: F401,W0611 - legacy __main__ compatibility export
    looks_like_integer_literal as _looks_like_integer_literal,
)

# COMPAT_STABLE_SHIM: lockdown auth seams remain importable from
# meshtastic.__main__ for historical callers.
from meshtastic.lockdown import build_lockdown_auth  # noqa: F401
from meshtastic.lockdown import read_lockdown_passphrase_file  # noqa: F401
from meshtastic.lockdown import send_lockdown_auth  # noqa: F401
from meshtastic.lockdown import validate_lockdown_passphrase  # noqa: F401

# black and isort disagree on these three statements: isort's line-length math
# excludes the noqa comment (so it collapses the statement to one line) while
# black includes it (so it must stay exploded). They are frozen compat
# re-exports, so skip them with an isort directive rather than fight it.
# isort: off
from meshtastic.cli.parser import (
    _MODEM_PRESET_SHORTHANDS as _MODEM_PRESET_SHORTHANDS,
)  # noqa: F401,W0611 - legacy __main__ compatibility export
from meshtastic.cli.values import (
    parse_integer_literal as _parse_integer_literal,  # noqa: F401,W0611 - legacy __main__ compatibility export
)
from meshtastic.cli.values import (
    parse_modem_preset_name as _parse_modem_preset_name,  # noqa: F401,W0611 - legacy __main__ compatibility export
)

# isort: on
from meshtastic.configure_verify import (  # noqa: F401 - legacy __main__ compatibility export
    LocalApplyStatus,
    _verify_channel_url_against_state,
    verify_local_config_apply,
)
from meshtastic.host_port import parseHostAndPort
from meshtastic.interfaces.ble.interface import BLEInterface
from meshtastic.mesh_interface import MeshInterface
from meshtastic.protobuf import (  # noqa: F401 - legacy __main__ compatibility export
    admin_pb2,
    channel_pb2,
    config_pb2,
    localonly_pb2,
    mesh_pb2,
    portnums_pb2,
)
from meshtastic.version import (
    PROJECT_DISPLAY_NAME,
    get_active_version,
)

# pylint: enable=unused-import


# COMPAT_STABLE_SHIM: Preserve legacy private imports while runtime owns behavior.
MAIN_LOOP_IDLE_SLEEP_SECONDS = cli_runtime.MAIN_LOOP_IDLE_SLEEP_SECONDS
SERIAL_RECONNECT_RETRY_SECONDS = cli_runtime.SERIAL_RECONNECT_RETRY_SECONDS
SERIAL_LISTEN_CONNECTED_SLEEP_SECONDS = (
    cli_runtime.SERIAL_LISTEN_CONNECTED_SLEEP_SECONDS
)
SERIAL_RX_THREAD_JOIN_TIMEOUT_SECONDS = (
    cli_runtime.SERIAL_RX_THREAD_JOIN_TIMEOUT_SECONDS
)
_is_serial_reconnect_client = cli_runtime._is_serial_reconnect_client
_serial_transport_is_live = cli_runtime._serial_transport_is_live
_serial_should_reconnect = cli_runtime._serial_should_reconnect
_poll_serial_reconnect = cli_runtime._poll_serial_reconnect
_listen_loop_poll_once = cli_runtime._listen_loop_poll_once

argcomplete: ModuleType | None = None
try:
    import argcomplete as _argcomplete

    argcomplete = _argcomplete
except ImportError:
    pass

meshtastic_test: ModuleType | None = None
try:
    meshtastic_test = importlib.import_module("meshtastic.test")
except ImportError:
    pass

PowerMeter: Any | None = None
PowerStress: Any | None = None
PPK2PowerSupply: Any | None = None
RidenPowerSupply: Any | None = None
SimPowerSupply: Any | None = None
LogSet: Any | None = None

try:
    powermon_module = importlib.import_module("meshtastic.powermon")
    slog_module = importlib.import_module("meshtastic.slog")
    PowerMeter = powermon_module.PowerMeter
    PowerStress = powermon_module.PowerStress
    PPK2PowerSupply = powermon_module.PPK2PowerSupply
    RidenPowerSupply = powermon_module.RidenPowerSupply
    SimPowerSupply = powermon_module.SimPowerSupply
    LogSet = slog_module.LogSet
    powermon_constants = importlib.import_module("meshtastic.powermon.constants")
    MIN_SUPPLY_VOLTAGE_V = powermon_constants.MIN_SUPPLY_VOLTAGE_V
    MAX_SUPPLY_VOLTAGE_V = powermon_constants.MAX_SUPPLY_VOLTAGE_V

    have_powermon = True
    powermon_exception = None
    meter = None
except (ImportError, AttributeError) as exc:
    PowerMeter = None
    PowerStress = None
    PPK2PowerSupply = None
    RidenPowerSupply = None
    SimPowerSupply = None
    LogSet = None
    have_powermon = False
    powermon_exception = exc
    meter = None
    # Provide fallback constants if powermon is not available
    MIN_SUPPLY_VOLTAGE_V = 0.8
    MAX_SUPPLY_VOLTAGE_V = 5.0
    logging.getLogger(__name__).debug("powermon/slog not available: %s", exc)

logger = logging.getLogger(__name__)

_CONFIGURE_PREFLIGHT_MODE = cli_preference_runtime.CONFIGURE_PREFLIGHT_MODE
_SET_PREF_VALUE_ERRORS_FATAL = cli_preference_runtime.SET_PREF_VALUE_ERRORS_FATAL
_PREF_VALIDATION_REPORTER = cli_preference_runtime.PREF_VALIDATION_REPORTER
_PreferenceValueError = cli_preference_runtime.PreferenceValueError
_fatal_preference_value_errors = cli_preference_runtime.fatal_preference_value_errors
BITFIELD_ENUMS = cli_preference_runtime.BITFIELD_ENUMS


# Public CLI shorthands for common modem presets. Keep this ordered to preserve
# the historical behavior when callers supply more than one shorthand: later
# options win. ``--ch-preset`` below is the scalable path for every enum value
# present in the active protobuf schema, including future additions.


# ==============================================================================
# CLI Timing Constants
# ==============================================================================

# Delay after applying configuration changes (owner, channel, etc.)
CONFIG_APPLY_DELAY_SECONDS = cli_configure_actions.CONFIG_APPLY_DELAY_SECONDS
CONFIG_WRITE_PACE_SECONDS = cli_configure_actions.CONFIG_WRITE_PACE_SECONDS
"""Short inter-write cadence that drains transport queues without stretching transactions."""

# Delay after setURL operations, which write up to 8 channel snapshots
# plus LoRa config; the device needs extra time to commit all changes
# before accepting further admin messages.
CONFIG_SETURL_DELAY_SECONDS = cli_configure_actions.CONFIG_SETURL_DELAY_SECONDS

CONFIG_COMMIT_SETTLE_SECONDS = cli_configure_actions.CONFIG_COMMIT_SETTLE_SECONDS
"""Settle delay after commitSettingsTransaction before assuming the session may end."""

CONFIG_RECONNECT_WAIT_SECONDS = cli_configure_actions.CONFIG_RECONNECT_WAIT_SECONDS
"""Maximum time to wait for device reconnect after a reboot-capable configure commit."""

LOCAL_SET_APPLY_VERIFY_SECONDS = 12.0
"""Shared verification budget for one local ``--set`` apply, across all sections.

Local config writes have no routing acknowledgment, so the only honest proof is
a fresh config readback. One bounded budget (never multiplied per section)
covers every section reload of a single batch; 12s matches the node's
correlated admin-response wait, which is generous for a healthy local getter
round trip while still failing a dead device within seconds.
"""

PREFS_WITH_AVAILABILITY_CHANGES = cli_configure_actions.PREFS_WITH_AVAILABILITY_CHANGES


SETURL_STABILITY_TIMEOUT_SECONDS = (
    cli_configure_actions.SETURL_STABILITY_TIMEOUT_SECONDS
)
"""Timeout for post-setURL transport stability before transactional writes."""

FACTORY_RESET_READY_PROBE_TIMEOUT_SECONDS: float = (
    cli_device_actions.FACTORY_RESET_READY_PROBE_TIMEOUT_SECONDS
)
"""Timeout for post-reset reconnect probe inside factory-reset command."""

FACTORY_RESET_READY_PROBE_MAX_ATTEMPTS: int = (
    cli_device_actions.FACTORY_RESET_READY_PROBE_MAX_ATTEMPTS
)
"""Number of reconnect attempts the factory-reset readiness probe makes before
declaring the device unresponsive."""

FACTORY_RESET_READY_PROBE_RETRY_DELAY_SECONDS: float = (
    cli_device_actions.FACTORY_RESET_READY_PROBE_RETRY_DELAY_SECONDS
)
"""Sleep between consecutive factory-reset readiness probe attempts."""

FACTORY_RESET_ACCEPTANCE_TIMEOUT_SECONDS: float = (
    cli_device_actions.FACTORY_RESET_ACCEPTANCE_TIMEOUT_SECONDS
)
"""Maximum time to observe a local reset ACK/NAK or reboot disconnect."""

FACTORY_RESET_ACCEPTANCE_POLL_SECONDS: float = (
    cli_device_actions.FACTORY_RESET_ACCEPTANCE_POLL_SECONDS
)
"""Polling interval while waiting for local reset command acceptance."""

CONFIGURE_DIRECT_SETTINGS_HEADER = (
    cli_configure_actions.CONFIGURE_DIRECT_SETTINGS_HEADER
)
"""Printed once when --configure starts applying non-transactional values."""

_ALLOWED_CONFIGURE_KEYS = cli_configure_actions.ALLOWED_CONFIGURE_KEYS

# Delay between GPIO watch iterations
GPIO_WATCH_INTERVAL_SECONDS = 1.0

# Maximum wait time for GPIO read response (each iteration)
GPIO_READ_POLL_INTERVAL_SECONDS = 1.0
GPIO_READ_MAX_POLLS = 10

# Time to wait for device boot after power-on
POWER_ON_BOOT_DELAY_SECONDS = 5.0

# OTA CLI timing and retry delay
OTA_REBOOT_WAIT_SECONDS = cli_device_actions.OTA_REBOOT_WAIT_SECONDS
OTA_RETRY_DELAY_SECONDS = cli_device_actions.OTA_RETRY_DELAY_SECONDS
OTA_MAX_RETRIES = cli_device_actions.OTA_MAX_RETRIES

# Keep-alive sleep interval for main loop (effectively infinite wait)
"""Sleep duration for the CLI main loop when listening on non-serial interfaces."""

"""Polling interval for serial reconnect attempts after device reboot/disconnect."""

"""Sleep duration when a serial listen session is connected and healthy."""

"""Timeout for joining the old reader thread before reconnecting."""


# COMPAT_STABLE_SHIM: accept historical config field spellings.
# Backward-compatible aliases for renamed config fields.
_PREFERENCE_FIELD_ALIASES = cli_preference_runtime.PREFERENCE_FIELD_ALIASES


def _cli_exit(message: str, return_value: int = 1) -> NoReturn:
    """Exit this CLI entrypoint with a user-facing message.

    This helper centralizes CLI exit behavior in the entrypoint module while
    keeping ``meshtastic.util.our_exit`` available as a legacy compatibility
    shim for external callers.

    Parameters
    ----------
    message : str
        Message to print before exiting.
    return_value : int
        Process exit code (0 for success, non-zero for error).
    """
    invocation = cli_invocation.get_current_invocation()
    if invocation is not None and invocation.exit_handler is not None:
        invocation.exit_handler(message, return_value)
    output = cli_invocation._get_cli_output()  # noqa: SLF001
    if output is not None and output is not _cli_print:
        _cli_print(message, force=True)
        raise SystemExit(return_value)
    meshtastic.util.our_exit(message, return_value)


def _current_invocation_args() -> argparse.Namespace | None:
    """Return invocation-owned arguments, falling back to legacy ``mt_config``."""
    invocation = cli_invocation.get_current_invocation()
    return invocation.args if invocation is not None else mt_config.args


def _current_camel_case() -> bool:
    """Compatibility wrapper returning the active naming mode."""
    return cli_invocation.current_camel_case()


def _current_channel_index() -> int | None:
    """Compatibility wrapper returning the invocation-selected channel index."""
    return cli_invocation.current_channel_index()


def _set_current_channel_index(value: int) -> None:
    """Compatibility wrapper updating the invocation-selected channel index."""
    cli_invocation.set_current_channel_index(value)


def _set_current_logfile(value: IO[str] | None) -> None:
    """Update invocation-owned logfile state and mirror the compatibility global."""
    invocation = cli_invocation.get_current_invocation()
    if invocation is not None:
        invocation.logfile = value
    mt_config.logfile = value


def _cli_print(message: str, *, force: bool = False) -> None:
    """Print a CLI message, optionally bypassing ``--quiet`` suppression.

    Parameters
    ----------
    message : str
        User-facing message to print to stdout.
    force : bool
        When ``True``, print even if ``--quiet`` is active. Use this for
        validation output that must remain visible to the user.
    """
    args = _current_invocation_args()
    if not force and args and getattr(args, "quiet", False):
        return
    output = cli_invocation._get_cli_output()  # noqa: SLF001
    if output is not None and output is not _cli_print:
        # A sink may itself wrap _cli_print. Suspend routing while calling it
        # so the standalone binding cannot recurse into the same sink.
        with cli_invocation._activate_cli_output(None):  # noqa: SLF001
            output(message)
        return
    print(message)


def _report_pref_validation(message: str) -> None:
    """Report preference validation through the extracted runtime."""
    cli_preference_runtime.report_pref_validation(message, cli_print=_cli_print)


def supportInfo(*, cli_print: Callable[[str], None] = print) -> None:
    """Print troubleshooting guidance and environment details useful for reporting CLI or library issues.

    Specifically prints the issue tracker URL and the running environment: system,
    platform string, kernel release, machine architecture, stdin/stdout encodings,
    installed distribution version (and available newer PyPI version if any),
    executable path, and Python implementation/version. Advises adding the output
    of the preferred CLI ``--info`` command when filing an issue.

    Parameters
    ----------
    cli_print : Callable[[str], None]
        Sink receiving each output line; defaults to ``print``.
    """
    cli_print("")
    cli_print(f"If having issues with {PROJECT_DISPLAY_NAME} CLI / python library")
    cli_print("or wish to make feature requests, visit:")
    cli_print(PROJECT_ISSUE_URL)
    cli_print("When adding an issue, be sure to include the following info:")
    cli_print(f" System: {platform.system()}")
    cli_print(f"   Platform: {platform.platform()}")
    cli_print(f"   Release: {platform.uname().release}")
    cli_print(f"   Machine: {platform.uname().machine}")
    cli_print(f"   Encoding (stdin): {sys.stdin.encoding}")
    cli_print(f"   Encoding (stdout): {sys.stdout.encoding}")
    the_version = get_active_version()
    pypi_version = meshtastic.util.check_if_newer_version()
    if pypi_version:
        cli_print(
            f" {PROJECT_DISPLAY_NAME}: v{the_version} (*** newer version v{pypi_version} available ***)"
        )
    else:
        cli_print(f" {PROJECT_DISPLAY_NAME}: v{the_version}")
    cli_print(f" Executable: {sys.argv[0]}")
    cli_print(
        f" Python: {platform.python_version()} {platform.python_implementation()} {platform.python_compiler()}"
    )
    cli_print("")
    cli_print(f"Please add the output from the command: {PRIMARY_CLI_NAME} --info")


_ConfigureReconnectResult = cli_configure_actions.ConfigureReconnectResult


def _configure_hooks() -> cli_configure_actions.ConfigureHooks:
    """Build configure dependencies from current entrypoint compatibility seams."""
    return cli_configure_actions.ConfigureHooks(
        cli_exit=_cli_exit,
        cli_print=_cli_print,
        traverse_config=traverseConfig,
        preflight_mode=_CONFIGURE_PREFLIGHT_MODE,
        is_local_destination=_is_local_destination,
        post_seturl_stability_check=_post_seturl_stability_check,
        post_configure_reconnect_and_verify=_post_configure_reconnect_and_verify,
        channel_url_matches_current_device_state=_channel_url_matches_current_device_state,
        pace_configure_write=_pace_configure_write,
    )


def _post_configure_reconnect_and_verify(
    interface: MeshInterface,
    *,
    timeout: float,
    node_dest: str,
    verify_channel_url: str | None = None,
    verify_config_fields: dict[str, dict[str, Any]] | None = None,
    verify_module_config_fields: dict[str, dict[str, Any]] | None = None,
) -> _ConfigureReconnectResult:
    """Compatibility wrapper for configure reconnect verification."""
    return cli_configure_actions._post_configure_reconnect_and_verify(
        interface,
        timeout=timeout,
        node_dest=node_dest,
        verify_channel_url=verify_channel_url,
        verify_config_fields=verify_config_fields,
        verify_module_config_fields=verify_module_config_fields,
        verify_channel_url_against_state=_verify_channel_url_against_state,
    )


def _post_seturl_stability_check(
    interface: MeshInterface,
    *,
    timeout: float = cli_configure_actions.SETURL_STABILITY_TIMEOUT_SECONDS,
) -> bool:
    """Compatibility wrapper for post-SetURL transport stabilization."""
    return cli_configure_actions._post_seturl_stability_check(
        interface, timeout=timeout
    )


def _send_local_factory_reset_and_wait(
    reset_node: Any,
    *,
    full: bool,
    timeout: float | None = None,
) -> mesh_pb2.MeshPacket | None:
    """Compatibility wrapper for the canonical device reset helper."""
    return cli_device_actions._send_local_factory_reset_and_wait(
        reset_node, full=full, timeout=timeout, cli_print=_cli_print
    )


@contextlib.contextmanager
def _temporary_instance_attributes(
    instance: Any, overrides: dict[str, Any]
) -> Iterator[None]:
    """Compatibility wrapper for temporary instance attribute overrides."""
    with cli_device_actions._temporary_instance_attributes(instance, overrides):
        yield


def _post_factory_reset_ready_probe(interface: MeshInterface) -> bool:
    """Compatibility wrapper for the canonical factory-reset readiness probe."""
    return cli_device_actions._post_factory_reset_ready_probe(interface)


def _validate_non_empty_mapping_sections(
    *, top_level_key: str, section_mapping: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Compatibility wrapper for configure section mapping validation.

    The legacy helper name is intentionally retained for external callers even
    though empty mappings are valid protobuf-default section payloads.
    """
    return cli_configure_actions._validate_mapping_sections(
        _configure_hooks(),
        top_level_key=top_level_key,
        section_mapping=section_mapping,
    )


def _preflight_configure_sections(
    target_node: Any,
    *,
    config_sections: dict[str, dict[str, Any]],
    module_config_sections: dict[str, dict[str, Any]],
) -> None:
    """Compatibility wrapper for configure preflight validation."""
    cli_configure_actions._preflight_configure_sections(
        _configure_hooks(),
        target_node,
        config_sections=config_sections,
        module_config_sections=module_config_sections,
    )


def _refresh_no_disconnect_verify_state(
    target_node: Any,
    *,
    verify_channel_url: str | None,
    verify_config_fields: dict[str, dict[str, Any]] | None,
    verify_module_config_fields: dict[str, dict[str, Any]] | None,
) -> None:
    """Compatibility wrapper for touched-state refresh before verification."""
    cli_configure_actions._refresh_no_disconnect_verify_state(
        target_node,
        verify_channel_url=verify_channel_url,
        verify_config_fields=verify_config_fields,
        verify_module_config_fields=verify_module_config_fields,
    )


def _channel_url_matches_current_device_state(
    target_node: Any, requested_channel_url: str
) -> bool:
    """Compatibility wrapper for channel URL equality checks."""
    return cli_configure_actions._channel_url_matches_current_device_state(
        target_node,
        requested_channel_url,
        verify_channel_url_against_state=_verify_channel_url_against_state,
    )


def _flatten_leaf_paths(prefix: str, mapping: dict[str, Any]) -> list[str]:
    """Compatibility wrapper for configure verification path flattening."""
    return cli_configure_actions._flatten_leaf_paths(prefix, mapping)


def _verify_config_sections(
    config_fields: dict[str, dict[str, Any]],
    proto_config: Any,
    label: str,
    verified_fields: list[str] | None = None,
) -> bool:
    """Compatibility wrapper for protobuf section verification."""
    return cli_configure_actions._verify_config_sections(
        config_fields, proto_config, label, verified_fields=verified_fields
    )


def _verify_post_reconnect_config(
    interface: MeshInterface,
    node_dest: str,
    *,
    verify_channel_url: str | None = None,
    verify_config_fields: dict[str, dict[str, Any]] | None = None,
    verify_module_config_fields: dict[str, dict[str, Any]] | None = None,
) -> _ConfigureReconnectResult:
    """Compatibility wrapper for post-reconnect value verification."""
    return cli_configure_actions._verify_post_reconnect_config(
        interface,
        node_dest,
        verify_channel_url=verify_channel_url,
        verify_config_fields=verify_config_fields,
        verify_module_config_fields=verify_module_config_fields,
        verify_channel_url_against_state=_verify_channel_url_against_state,
    )


# COMPAT_STABLE_SHIM: historical snake_case helper name.
def support_info() -> None:
    """Compatibility alias for supportInfo()."""
    supportInfo()


def onReceive(packet: dict[str, Any], interface: MeshInterface) -> None:
    """Handle an incoming mesh packet, optionally send a text reply, and close the interface when appropriate."""
    _on_receive_with_sink(packet, interface)


def _on_receive_with_sink(
    packet: dict[str, Any],
    interface: MeshInterface,
    *,
    cli_print: Callable[[str], None] = print,
) -> None:
    """Sink-aware implementation backing the historical onReceive pubsub callback.

    The public ``onReceive`` signature must stay exactly ``(packet, interface)``
    because pypubsub derives the ``meshtastic.receive`` topic specification from
    listener signatures; sink injection therefore happens through this internal
    entrypoint instead.
    """
    args = _current_invocation_args()
    try:
        d = packet.get("decoded")
        logger.debug("in onReceive() d:%s", d)

        is_text_reply = (
            args
            and args.sendtext
            and d is not None
            and interface.myInfo is not None
            and packet.get("to") == interface.myInfo.my_node_num
            and d.get("portnum")
            == portnums_pb2.PortNum.Name(portnums_pb2.PortNum.TEXT_MESSAGE_APP)
        )
        if is_text_reply:
            interface.close()

        if d is not None and args and args.reply:
            msg = d.get("text")
            if msg:
                # Prevent infinite loop: ignore own messages and auto-reply echoes
                if (
                    interface.myInfo
                    and packet.get("from") == interface.myInfo.my_node_num
                ):
                    return
                if msg.startswith("got msg '"):
                    return
                rxChannel = packet.get("channel", 0)
                targetChannel = (
                    int(args.ch_index) if args.ch_index is not None else None
                )
                if targetChannel is None or rxChannel == targetChannel:
                    rxSnr = packet.get("rxSnr", "unknown")
                    hopLimit = packet.get("hopLimit", "unknown")
                    cli_print(f"message: {msg}")
                    reply = (
                        f"got msg '{msg}' with rxSnr: {rxSnr} and hopLimit: {hopLimit}"
                    )
                    cli_print(f"Received channel {rxChannel}. Sending reply: {reply}")
                    interface.sendText(reply, channelIndex=rxChannel)
                else:
                    cli_print(
                        f"Ignored message on channel {rxChannel} (waiting for channel {targetChannel})"
                    )

    except Exception as ex:
        logger.warning("Error processing received packet: %s", ex)


def onClientNotification(
    notification: mesh_pb2.ClientNotification, interface: MeshInterface
) -> None:
    """Render spontaneous client notifications for long-lived CLI sessions.

    Parameters
    ----------
    notification : mesh_pb2.ClientNotification
        Notification packet decoded from the ``meshtastic.clientNotification``
        pubsub topic. Key-verification variants render through their dedicated
        flow; every other notification renders its message prefixed by the
        schema label for its type (for example ``Notification (Duty Cycle Limit
        Exceeded): ...``). Firmware that does not set a type falls back to
        ``Notification: ...``.

    interface : MeshInterface
        Interface that received the notification. Unused by this handler and
        retained for the shared pubsub subscriber signature.

    Returns
    -------
    None
        The active ``--key-verify`` action owns its own subscription and renders its
        response after the bounded wait, so this generic subscriber is suppressed
        during that action to avoid duplicate output.
    """
    _ = interface
    args = _current_invocation_args()
    if args is not None and getattr(args, "key_verify", None):
        return
    try:
        if notification.WhichOneof("payload_variant") in {
            "key_verification_number_inform",
            "key_verification_number_request",
            "key_verification_final",
        }:
            cli_device_actions._render_key_verification_notification(  # noqa: SLF001
                notification, "", _cli_print
            )
        else:
            cli_notification_rendering._render_client_notification(  # noqa: SLF001
                notification, _cli_print
            )
    except Exception as ex:
        logger.warning("Error rendering client notification: %s", ex)


def onConnection(interface: MeshInterface, topic: Any = pub.AUTO_TOPIC) -> None:
    """Notify about a change in the radio connection state."""
    _ = interface
    topic_name = topic.getName() if hasattr(topic, "getName") else str(topic)
    _cli_print(f"Connection changed: {topic_name}")


def checkChannel(interface: MeshInterface, channelIndex: int) -> bool:
    """Compatibility wrapper for the cli channel-enabled check."""
    return cli_channel_contact_actions.check_channel(interface, channelIndex)


_normalize_pref_name = cli_preference_runtime.normalize_pref_name
_parse_bitfield_value = cli_values.parse_bitfield_value


def _display_pref_name(comp_name: str) -> str:
    """Format a canonical preference path for user-facing output."""
    if not _current_camel_case():
        return comp_name
    return ".".join(
        meshtastic.util.snake_to_camel(part) for part in comp_name.split(".")
    )


_SECRET_PREF_FIELDS = cli_preference_runtime.SECRET_PREF_FIELDS
_SECRET_PREF_PATHS = cli_preference_runtime.SECRET_PREF_PATHS
_REDACTED_PREF_VALUE = cli_preference_runtime.REDACTED_PREF_VALUE
_SET_VALUE_REJECTED_MESSAGE = cli_preference_runtime.SET_VALUE_REJECTED_MESSAGE


class _DescriptorLike(Protocol):
    """Structural descriptor type shared by pure-Python and upb protobuf runtimes."""

    @property
    def fields(self) -> Sequence[FieldDescriptor]:
        """Return fields declared by this message descriptor."""


_is_secret_pref = cli_preference_runtime.is_secret_pref
_redact_pref_value = cli_preference_runtime.redact_pref_value


def getPref(
    node: Any,
    comp_name: str,
    *,
    allow_secrets: bool = False,
    cli_print: Callable[[str], None] = print,
) -> bool:
    """Compatibility wrapper for preference retrieval and display."""
    return cli_preference_runtime.get_pref(
        node, comp_name, allow_secrets=allow_secrets, cli_print=cli_print
    )


splitCompoundName = cli_preference_runtime.split_compound_name


def traverseConfig(
    config_root: str,
    config: dict[str, Any],
    interface_config: Any,
    failed_fields: list[str] | None = None,
) -> bool:
    """COMPAT_STABLE_SHIM: apply a nested configure mapping to a protobuf message."""
    return cli_preference_runtime.traverse_config(
        config_root,
        config,
        interface_config,
        resolve_pref_fn=_resolve_pref,
        set_pref_fn=setPref,
        failed_fields=failed_fields,
    )


_walk_config_path = cli_preference_runtime.walk_config_path
_resolve_pref = cli_preference_runtime.resolve_pref
_protobuf_field_type_label = cli_preference_runtime.protobuf_field_type_label


_print_channel_field_choices = cli_channel_contact_actions._print_channel_field_choices


def _reject_pref_value(
    field: FieldDescriptor, *, field_path: str, raw_value: Any
) -> bool:
    """Compatibility wrapper for preference value rejection/reporting."""
    return cli_preference_runtime.reject_pref_value(
        field,
        field_path=field_path,
        raw_value=raw_value,
        cli_print=_cli_print,
    )


def _assign_scalar_pref_value(
    target: Any,
    field: FieldDescriptor,
    value: Any,
    *,
    field_path: str,
    raw_value: Any,
) -> bool:
    """Compatibility wrapper for scalar protobuf preference assignment."""
    return cli_preference_runtime.assign_scalar_pref_value(
        target,
        field,
        value,
        field_path=field_path,
        raw_value=raw_value,
        cli_print=_cli_print,
    )


def setPref(config: Any, comp_name: str, raw_val: Any) -> bool:
    """COMPAT_STABLE_SHIM: set a protobuf preference through the CLI runtime."""
    return cli_preference_runtime.set_pref(
        config,
        comp_name,
        raw_val,
        camel_case=_current_camel_case(),
        cli_print=_cli_print,
        is_repeated_field=_is_repeated_field,
    )


def _handle_ota_update(
    interface: MeshInterface,
    args: Any,
    getNode_kwargs: dict[str, Any],
) -> None:
    """Compatibility wrapper for the canonical Wi-Fi OTA action."""
    cli_device_actions._handle_ota_update(
        interface,
        args,
        getNode_kwargs,
        cli_exit=_cli_exit,
        cli_print=_cli_print,
        is_local_destination=_is_local_destination,
    )


MODULE_PREFIX_SEGMENT = cli_set_actions.MODULE_PREFIX_SEGMENT
MODULE_PREFIX_HINT = cli_set_actions.MODULE_PREFIX_HINT

_module_prefix_strip_hint = cli_set_actions._module_prefix_strip_hint
_print_set_field_choices = cli_set_actions._print_set_field_choices
_format_set_preflight_exception = cli_set_actions._format_set_preflight_exception
_render_preview_current_value = cli_set_actions._render_preview_current_value

LOCAL_SET_APPLY_VERIFY_SECONDS = cli_set_actions.LOCAL_SET_APPLY_VERIFY_SECONDS


def _handle_set_command(
    interface: MeshInterface,
    args: Any,
    getNode_kwargs: dict[str, Any],
) -> None:
    """Compatibility wrapper for the cli ``--set`` apply pipeline."""
    cli_set_actions._handle_set_command(
        interface, args, getNode_kwargs, cli_exit=_cli_exit, cli_print=_cli_print
    )


def _verify_local_set_apply(
    interface: MeshInterface,
    node: Any,
    *,
    node_dest: str,
    pre_write_config_id: int | None,
    config_fields: dict[str, dict[str, Any]],
    module_config_fields: dict[str, dict[str, Any]],
) -> None:
    """Compatibility wrapper for local ``--set`` apply verification."""
    cli_set_actions._verify_local_set_apply(
        interface,
        node,
        node_dest=node_dest,
        pre_write_config_id=pre_write_config_id,
        config_fields=config_fields,
        module_config_fields=module_config_fields,
        cli_exit=_cli_exit,
        cli_print=_cli_print,
    )


def _preview_set_command(
    interface: MeshInterface,
    args: Any,
    get_node_kwargs: dict[str, Any],
) -> cli_config_preview.ConfigSnapshotCopies:
    """Compatibility wrapper for the cli ``--set`` dry-run preview."""
    return cli_set_actions._preview_set_command(
        interface, args, get_node_kwargs, cli_exit=_cli_exit, cli_print=_cli_print
    )


def _pace_configure_write(
    remaining_writes: int, *, sleep_fn: Callable[[float], None] = time.sleep
) -> None:
    """Compatibility wrapper for configure write pacing."""
    cli_configure_actions._pace_configure_write(remaining_writes, sleep_fn=sleep_fn)


def _apply_configure_channel_url(
    target_node: Any, raw_channel_url: Any, *, config_key: str
) -> bool:
    """Compatibility wrapper for configure channel URL application."""
    return cli_configure_actions._apply_configure_channel_url(
        _configure_hooks(), target_node, raw_channel_url, config_key=config_key
    )


def _handle_configure_command(
    interface: MeshInterface, args: Any, getNode_kwargs: dict[str, Any]
) -> cli_configure_actions._ConfigureCommandResult:
    """Compatibility wrapper for configure-file transaction execution."""
    return cli_configure_actions._handle_configure_command(
        _configure_hooks(), interface, args, getNode_kwargs
    )


def _run_configure_preview(
    interface: MeshInterface,
    args: Any,
    getNode_kwargs: dict[str, Any],
    snapshot: cli_config_preview.ConfigSnapshotCopies | None,
) -> None:
    """Compatibility wrapper previewing a configure document without writes."""
    cli_configure_actions._preview_configure_command(
        _configure_hooks(), interface, args, getNode_kwargs, snapshot=snapshot
    )


def _validate_cli_show_fields(interface: MeshInterface, show_fields: list[str]) -> None:
    """Compatibility wrapper for ``--show-fields`` validation."""
    cli_messaging_service_actions._validate_cli_show_fields(
        interface, show_fields, cli_exit=_cli_exit
    )


def _validate_cli_sort_field(interface: MeshInterface, sort_field: str) -> None:
    """Compatibility wrapper for ``--sort`` field validation."""
    cli_messaging_service_actions._validate_cli_sort_field(
        interface, sort_field, cli_exit=_cli_exit
    )


def _build_connected_dispatch_hooks() -> cli_dispatch.DispatchHooks:
    """Compatibility wrapper assembling connected dispatch hooks."""
    return cli_dispatch._build_connected_dispatch_hooks(
        cli_exit=_cli_exit,
        cli_print=_cli_print,
        powermon_available=lambda: have_powermon,
        powermon_error=lambda: powermon_exception,
        get_meter=lambda: meter,
        log_set_factory=LogSet,
        power_stress_factory=PowerStress,
        sleep=time.sleep,
    )


def onConnected(interface: MeshInterface) -> None:
    """Execute parsed connected CLI actions on an established interface."""
    try:
        args = _current_invocation_args()
        if args is None:
            raise RuntimeError("onConnected called without args being set up")
        context = CliContext(
            interface=interface,
            args=args,
            get_node_kwargs={
                "requestChannelAttempts": args.channel_fetch_attempts,
                "timeout": args.timeout,
            },
            outcome=ActionOutcome(),
        )
        cli_dispatch._dispatch_connected(context, _build_connected_dispatch_hooks())
    except Exception as ex:
        logger.exception("Unhandled exception in onConnected: %s", ex)
        _cli_exit(f"Aborting due to: {ex}", 1)


def printConfig(config: Any, *, cli_print: Callable[[str], None] = print) -> None:
    """COMPAT_STABLE_SHIM: print config fields through the CLI config I/O runtime."""
    cli_config_io.print_config(
        config, camel_case=_current_camel_case(), cli_print=cli_print
    )


def printAvailableConfigFields(
    as_json: bool = False, *, cli_print: Callable[[str], None] = print
) -> None:
    """COMPAT_STABLE_SHIM: print config fields and aliases through the runtime."""
    cli_config_io.print_available_config_fields(
        camel_case=_current_camel_case(),
        aliases=_PREFERENCE_FIELD_ALIASES,
        display_pref_name=_display_pref_name,
        type_label=cli_preference_runtime.protobuf_field_type_label,
        bitfield_enums=cli_preference_runtime.BITFIELD_ENUMS,
        local_config_factory=localonly_pb2.LocalConfig,
        module_config_factory=localonly_pb2.LocalModuleConfig,
        as_json=as_json,
        cli_print=cli_print,
    )


def _describe_config_field(field_name: str, as_json: bool = False) -> bool:
    """Describe one protobuf-backed CLI field without connecting to a device."""
    return cli_config_io._describe_config_field(
        field_name,
        normalize_pref_name=cli_preference_runtime.normalize_pref_name,
        display_pref_name=_display_pref_name,
        type_label=cli_preference_runtime.protobuf_field_type_label,
        bitfield_enums=cli_preference_runtime.BITFIELD_ENUMS,
        local_config_factory=localonly_pb2.LocalConfig,
        module_config_factory=localonly_pb2.LocalModuleConfig,
        as_json=as_json,
    )


def onNode(node: Any) -> None:
    """Notify about a node database change by printing the changed node.

    Parameters
    ----------
    node : Any
        The node object or identifier that changed; printed to standard output.
    """
    _cli_print(f"Node changed: {node}")


def subscribe() -> None:
    """Register the default pub-sub handlers needed to receive incoming mesh messages.

    Subscribes the local receive callback to the "meshtastic.receive" topic so incoming packets
    are delivered to the onReceive handler. Other topic subscriptions are intentionally left
    commented out.
    """
    pub.subscribe(onReceive, "meshtastic.receive")
    pub.subscribe(onClientNotification, _topics.CLIENT_NOTIFICATION_TOPIC)
    # pub.subscribe(onConnection, "meshtastic.connection")

    # We now call onConnected from main
    # pub.subscribe(onConnected, "meshtastic.connection.established")

    # pub.subscribe(onNode, "meshtastic.node")


# COMPAT_STABLE_SHIM: preserve historical private helper imports.
_is_repeated_field = cli_config_io.is_repeated_field
_set_missing_flags_false = cli_config_io.set_missing_flags_false
_prefix_base64_bytes_fields = cli_config_io.prefix_base64_bytes_fields
_prefix_base64_key = cli_config_io.prefix_base64_key
CONFIG_TRUE_DEFAULTS = cli_config_io.CONFIG_TRUE_DEFAULTS
MODULE_TRUE_DEFAULTS = cli_config_io.MODULE_TRUE_DEFAULTS


def exportConfig(interface: MeshInterface) -> str:
    """COMPAT_STABLE_SHIM: export configuration through the CLI config I/O runtime."""
    return cli_config_io.export_config(
        interface,
        camel_case=_current_camel_case(),
        message_to_dict=MessageToDict,
        prefix_base64_bytes_fields_fn=_prefix_base64_bytes_fields,
        set_missing_flags_false_fn=_set_missing_flags_false,
        config_true_defaults=CONFIG_TRUE_DEFAULTS,
        module_true_defaults=MODULE_TRUE_DEFAULTS,
    )


# COMPAT_STABLE_SHIM: snake_case alias for exportConfig
export_config = exportConfig


def exportProfile(interface: MeshInterface) -> bytes:
    """Export the local node configuration as a binary DeviceProfile.

    Parameters
    ----------
    interface : MeshInterface
        Connected interface whose local node configuration is exported.

    Returns
    -------
    bytes
        Serialized ``clientonly_pb2.DeviceProfile`` payload suitable for a
        ``.cfg`` destination or later ``--configure`` import.
    """
    return cli_config_io._export_profile(interface)  # noqa: SLF001


def _close_power_meter_quietly(candidate: Any) -> None:
    """Best-effort close used while rolling back meter construction/configuration."""
    close_method = getattr(candidate, "close", None)
    if callable(close_method):
        try:
            close_method()
        except Exception:
            logger.debug("Power meter cleanup failed", exc_info=True)


def _validated_power_voltage(args: argparse.Namespace) -> float:
    """Validate power-related CLI arguments before opening hardware."""
    voltage = 0.0
    if args.power_voltage is not None:
        if not any(
            (
                args.power_riden,
                args.power_ppk2_meter,
                args.power_ppk2_supply,
                args.power_sim,
            )
        ):
            _cli_exit(
                "--power-voltage requires one of --power-riden, --power-ppk2-meter, --power-ppk2-supply, or --power-sim"
            )
        try:
            voltage = float(args.power_voltage)
        except ValueError:
            _cli_exit("--power-voltage must be a number")
        if not MIN_SUPPLY_VOLTAGE_V <= voltage <= MAX_SUPPLY_VOLTAGE_V:
            _cli_exit(
                f"Voltage must be between {MIN_SUPPLY_VOLTAGE_V}V and {MAX_SUPPLY_VOLTAGE_V}V"
            )

    if (args.power_ppk2_supply or args.power_ppk2_meter) and voltage <= 0:
        _cli_exit("Voltage must be specified for PPK2")
    return voltage


def _build_power_meter(args: argparse.Namespace, voltage: float) -> Any:
    """Construct and configure one power meter transactionally."""
    candidate: Any = None
    try:
        if args.power_riden:
            riden_factory = RidenPowerSupply
            if riden_factory is None:
                _cli_exit("The Riden power meter backend is unavailable")
            candidate = riden_factory(args.power_riden)
        elif args.power_ppk2_supply or args.power_ppk2_meter:
            ppk2_factory = PPK2PowerSupply
            if ppk2_factory is None:
                _cli_exit("The PPK2 power meter backend is unavailable")
            candidate = ppk2_factory()
            candidate.setVoltage(voltage)
            candidate.setIsSupply(args.power_ppk2_supply)
        elif args.power_sim:
            sim_factory = SimPowerSupply
            if sim_factory is None:
                _cli_exit("The simulated power meter backend is unavailable")
            candidate = sim_factory()

        if candidate is None:
            _cli_exit("A power meter backend must be selected")

        if voltage:
            logger.info("Setting power supply to %s volts", voltage)
            candidate.setVoltage(voltage)
            candidate.powerOn()
            if args.power_wait:
                input("Powered on, press enter to continue...")
            else:
                logger.info("Powered-on, waiting for device to boot")
                time.sleep(POWER_ON_BOOT_DELAY_SECONDS)
        return candidate
    except BaseException:
        if candidate is not None:
            _close_power_meter_quietly(candidate)
        raise


def _create_power_meter() -> None:
    """Initialize and configure the global power meter from parsed CLI arguments.

    Validation is performed before opening hardware. A newly-created meter is
    fully configured before replacing the historical module-global ``meter``;
    failures close the partial meter and preserve the previous global instance.

    Raises
    ------
    RuntimeError
        If no parsed CLI arguments are available from the active invocation
        or the legacy ``mt_config`` fallback.
    """
    global meter  # pylint: disable=global-statement
    args = _current_invocation_args()
    if args is None:
        raise RuntimeError(
            "CLI arguments must be initialized before calling _create_power_meter()"
        )

    if not have_powermon:
        _cli_exit(
            "The powermon module could not be loaded. "
            "You may need to run `uv sync --locked --group powermon`. "
            f"Import Error was: {powermon_exception}"
        )
    voltage = _validated_power_voltage(args)
    replacement = _build_power_meter(args, voltage)
    previous = meter
    meter = replacement
    if previous is not None and previous is not replacement:
        _close_power_meter_quietly(previous)


def _power_meter_requested(args: argparse.Namespace) -> bool:
    """Return whether parsed CLI arguments require powermon meter setup."""
    return any(
        (
            args.power_riden,
            args.power_ppk2_meter,
            args.power_ppk2_supply,
            args.power_sim,
            args.power_voltage is not None,
        )
    )


# COMPAT_STABLE_SHIM: legacy snake_case helper for callers importing this module.
create_power_meter = _create_power_meter


def _parse_host_port(host_str: str, default_port: int) -> tuple[str, int]:
    """Compatibility wrapper for shared host/port parsing in CLI code paths.

    Delegates parsing to `parseHostAndPort()` and preserves historical CLI
    behavior by converting validation failures into `_cli_exit(..., 1)`.

    Parameters
    ----------
    host_str : str
        Raw host string from CLI (`--host`).
    default_port : int
        Port to use when no explicit valid port is provided.

    Returns
    -------
    tuple[str, int]
        Parsed hostname/address and resolved TCP port.
    """
    try:
        return parseHostAndPort(
            host_str,
            default_port=default_port,
            env_var="--host",
        )
    except ValueError as exc:
        _cli_exit(f"Error: {exc}", 1)


def _release_session_power_meter(active_meter: Any) -> None:
    """Close an invocation-owned meter and clear the legacy global reference."""
    global meter  # pylint: disable=global-statement
    _close_power_meter_quietly(active_meter)
    if meter is active_meter:
        meter = None


def _clear_session_logfile(active_logfile: Any) -> None:
    """Clear invocation and legacy logfile references when ownership ends."""
    invocation = cli_invocation.get_current_invocation()
    if invocation is not None and invocation.logfile is active_logfile:
        invocation.logfile = None
    if mt_config.logfile is active_logfile:
        mt_config.logfile = None


def _unsubscribe_cli_receive() -> None:
    """Best-effort removal of the invocation-level receive subscription."""
    try:
        pub.unsubscribe(onReceive, "meshtastic.receive")
        pub.unsubscribe(onClientNotification, _topics.CLIENT_NOTIFICATION_TOPIC)
    except Exception:
        logger.debug("Unable to remove CLI receive subscription", exc_info=True)


def _build_bootstrap_hooks() -> cli_bootstrap.BootstrapHooks:
    """Build current entrypoint seams for the CLI bootstrap runtime."""
    return cli_bootstrap.BootstrapHooks(
        cli_exit=_cli_exit,
        support_info=supportInfo,
        print_available_config_fields=printAvailableConfigFields,
        describe_config_field=_describe_config_field,
        create_power_meter=_create_power_meter,
        get_power_meter=lambda: meter,
        release_power_meter=_release_session_power_meter,
        set_logfile=_set_current_logfile,
        clear_session_logfile=_clear_session_logfile,
        subscribe=subscribe,
        unsubscribe_receive=_unsubscribe_cli_receive,
        on_connected=onConnected,
        parse_host_port=_parse_host_port,
        listen_loop_poll_once=cli_runtime._listen_loop_poll_once,
        set_channel_index=_set_current_channel_index,
        ble_interface=BLEInterface,
        tcp_interface=meshtastic.tcp_interface.TCPInterface,
        default_tcp_port=meshtastic.tcp_interface.DEFAULT_TCP_PORT,
        serial_interface=meshtastic.serial_interface.SerialInterface,
        mesh_interface_error=MeshInterface.MeshInterfaceError,
        test_module=meshtastic_test,
    )


def common() -> None:
    """Run the historical CLI bootstrap flow through the internal session runtime.

    Raises
    ------
    RuntimeError
        If ``mt_config.args`` or ``mt_config.parser`` has not been initialized.
    """
    args = mt_config.args
    parser = mt_config.parser
    if args is None:
        raise RuntimeError("mt_config.args must be initialized before calling common()")
    if parser is None:
        raise RuntimeError(
            "mt_config.parser must be initialized before calling common()"
        )
    invocation = cli_invocation.CliInvocation(
        args=args,
        parser=parser,
        channel_index=mt_config.channel_index,
        camel_case=mt_config.camel_case,
        logfile=mt_config.logfile,
    )
    with cli_invocation.activate_invocation(invocation):
        cli_bootstrap.run_common(args, parser, _build_bootstrap_hooks())


# ---------------------------------------------------------------------------
# End of reconnect helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CLI signal handling
# ---------------------------------------------------------------------------

# Signals the CLI converts into a graceful SystemExit so the normal teardown
# paths (interface close, finally blocks) run. ``atexit`` does not run when a
# supervised stop sends SIGTERM, and a BLE link left open at the OS level keeps
# the radio from advertising until something disconnects it. SIGKILL is
# deliberately not covered: by design it cannot be caught, and it remains the
# supervisor's fallback for wedged cleanup.
_CLI_HANDLED_SIGNALS: tuple[Any, ...] = tuple(
    sig
    for sig in (
        getattr(signal, "SIGTERM", None),
        getattr(signal, "SIGHUP", None),
    )
    if sig is not None
)
_cli_signal_shutdown_started = False
# Signal number recorded by the first delivery of the graceful handler, read
# by the entrypoint's finally to re-deliver the signal after teardown.
_cli_signal_received: int | None = None
# Lock acquire is a single atomic operation: nested signal handlers cannot
# both enter the graceful path between a flag read and a flag assignment.
_cli_signal_shutdown_lock = threading.Lock()
_cli_signal_defer_exit = False
_cli_signal_invocation_active = False
_CLI_OUTPUT_FLUSH_TIMEOUT_SECONDS = 0.2
_cli_signal_flush_lock = threading.Lock()
_cli_signal_flush_worker: threading.Thread | None = None


def _flush_cli_output_streams() -> None:
    """Flush stdout/stderr before a re-delivered fatal signal ends the process.

    The re-delivery path terminates the process by the signal itself, so
    interpreter finalization never flushes buffered output afterwards. Replaced
    streams may lack ``flush()`` (AttributeError) or reject it (OSError on a
    dead terminal, ValueError on a closed stream); the CLI's diagnostics are
    best-effort at this point and must never block the re-delivery.
    """
    streams = (sys.stdout, sys.stderr)

    def _flush() -> None:
        for stream in streams:
            with contextlib.suppress(OSError, ValueError, AttributeError):
                stream.flush()

    global _cli_signal_flush_worker  # pylint: disable=global-statement
    with _cli_signal_flush_lock:
        # A blocked flush survives an embedded caller that handles SystemExit.
        # Reusing the live worker bounds accumulation across repeated shutdowns
        # instead of retaining one thread and stream pair per attempt.
        if _cli_signal_flush_worker is not None and _cli_signal_flush_worker.is_alive():
            return
        worker = threading.Thread(target=_flush, daemon=True, name="mtjk-signal-flush")
        _cli_signal_flush_worker = worker
    try:
        worker.start()
        worker.join(timeout=_CLI_OUTPUT_FLUSH_TIMEOUT_SECONDS)
    except RuntimeError:
        # A fatal-signal fallback must also work when threads cannot start.
        pass


def _die_by_signal(signum: int) -> NoReturn:
    """Terminate the process immediately by dying to the received signal.

    Second-delivery path: graceful cleanup may be wedged, so no unwinding is
    attempted. Delivery resets the disposition and re-raises the signal at
    the process level through the shared self-signal seam, keeping
    supervisors reporting a clean stop; if the platform cannot re-deliver,
    the immediate ``os._exit`` fallback still ends the process without
    running any further Python cleanup.
    """
    _self_signal._signal_self(signum, reset_to_default=True)  # noqa: SLF001
    os._exit(128 + signum)


def _cli_signal_handler(signum: int, _frame: FrameType | None) -> None:
    """Convert SIGTERM/SIGHUP into a SystemExit so existing cleanup runs.

    The first delivery records the signal number and raises
    ``SystemExit(128 + signum)``, unwinding the CLI's normal finally/close
    paths instead of reimplementing teardown; the entrypoint's finally
    re-delivers the recorded signal once teardown completed. A second delivery
    must not be swallowed and must not wait on wedged cleanup: it dies by the
    signal immediately. Admission to the graceful path is one non-blocking
    lock acquire, so nested deliveries cannot both enter cleanup between a
    flag read and a flag assignment.
    """
    global _cli_signal_shutdown_started  # pylint: disable=global-statement
    global _cli_signal_received  # pylint: disable=global-statement
    if (
        _cli_signal_shutdown_started
        or not _cli_signal_shutdown_lock.acquire(  # pylint: disable=consider-using-with
            blocking=False
        )
    ):
        _die_by_signal(signum)
    _cli_signal_shutdown_started = True
    _cli_signal_received = signum
    # Logging can acquire a non-reentrant queue lock interrupted by this very
    # signal. Keep delivery free of logging and output. During installation or
    # restoration, record the first signal and let the owning invocation finish
    # restoring dispositions before it exits.
    if not _cli_signal_defer_exit:
        raise SystemExit(128 + signum)


def _install_cli_signal_handlers(
    previous: dict[int, Any] | None = None,
) -> dict[int, Any]:
    """Install SIGTERM/SIGHUP handlers from the CLI entry path.

    Library imports never install handlers; only the CLI entrypoint does, so
    embedders keep full control of their own signal policy. Registration is
    skipped off the main thread and where the platform forbids it. An
    inherited ``SIG_IGN`` disposition is preserved (a supervisor or ``nohup``
    expressed a policy we must not override), and so is a non-Python handler
    (``signal.getsignal()`` returning None): we could never restore it.
    """
    global _cli_signal_shutdown_started  # pylint: disable=global-statement
    global _cli_signal_shutdown_lock  # pylint: disable=global-statement
    global _cli_signal_received  # pylint: disable=global-statement
    if threading.current_thread() is not threading.main_thread():
        logger.debug("CLI signal handlers not installed off the main thread")
        return {}
    # Scope the forced-exit guard and the recorded signal to this CLI
    # invocation, not the process.
    _cli_signal_shutdown_started = False
    _cli_signal_received = None
    _cli_signal_shutdown_lock = threading.Lock()
    if previous is None:
        previous = {}
    for handled_signal in _CLI_HANDLED_SIGNALS:
        try:
            inherited = signal.getsignal(handled_signal)
            if inherited is signal.SIG_IGN:
                logger.debug(
                    "Preserving inherited SIG_IGN for %s",
                    getattr(handled_signal, "name", handled_signal),
                )
                continue
            if inherited is None:
                logger.debug(
                    "Preserving non-Python handler for %s",
                    getattr(handled_signal, "name", handled_signal),
                )
                continue
            previous[handled_signal] = inherited
            signal.signal(handled_signal, _cli_signal_handler)
        except (ValueError, OSError):
            logger.debug(
                "Could not install CLI handler for %s",
                getattr(handled_signal, "name", handled_signal),
            )
    return previous


def _restore_cli_signal_handlers(
    previous: dict[int, Any] | None, *, reset_state: bool = True
) -> None:
    """Restore host dispositions only where our handler is still installed.

    A disposition we did not replace (inherited ``SIG_IGN`` or a non-Python
    handler) is never written back, and a disposition someone else has since
    replaced is left alone: restoration must not clobber an owner that
    installed its own handler while the CLI ran.
    """
    global _cli_signal_shutdown_started  # pylint: disable=global-statement
    global _cli_signal_shutdown_lock  # pylint: disable=global-statement
    global _cli_signal_received  # pylint: disable=global-statement
    if threading.current_thread() is not threading.main_thread():
        return
    if previous:
        for handled_signal, inherited in previous.items():
            try:
                if signal.getsignal(handled_signal) is _cli_signal_handler:
                    signal.signal(handled_signal, inherited)
            except (ValueError, OSError):
                logger.debug(
                    "Could not restore CLI handler for %s",
                    getattr(handled_signal, "name", handled_signal),
                )
    if reset_state:
        _cli_signal_shutdown_started = False
        _cli_signal_received = None
        _cli_signal_shutdown_lock = threading.Lock()


# ---------------------------------------------------------------------------
# End of CLI signal handling
# ---------------------------------------------------------------------------


def initParser() -> None:
    """Parse CLI arguments and update the legacy global ``mt_config`` state."""
    parser = mt_config.parser
    if parser is None:
        raise RuntimeError(
            "mt_config.parser must be initialized before calling initParser()"
        )
    mt_config.args = parse_cli_args(
        parser,
        version=_format_cli_version(get_active_version()),
        argcomplete_module=argcomplete,
    )
    mt_config.parser = parser


def _re_deliver_recorded_signal(signum: int) -> NoReturn:
    """End a signal-driven shutdown by dying to the received signal.

    An exit status of 143 is reported by supervisors such as systemd as a
    failed stop, while death by SIGTERM is a clean stop. The dispositions the
    CLI replaced are restored by now, so the default disposition terminates
    the process and an embedder's restored Python handler receives the signal
    instead. Interpreter finalization and atexit handlers do not run after
    the re-delivered signal: session teardown releases the interface, the
    slog LogSet, the tunnel, and the session log file through the ExitStack,
    while the BLE runner's daemon loop — normally stopped by an atexit
    handler — dies with the process holding no BLE link after interface
    close.
    """
    _flush_cli_output_streams()
    # reset_to_default stays off: the dispositions the CLI replaced were just
    # restored, so an embedder's restored handler receives the signal instead.
    _self_signal._signal_self(signum, reset_to_default=False)  # noqa: SLF001
    raise SystemExit(128 + signum)


def _sigint_re_delivery_eligible() -> bool:
    """Return whether a Ctrl+C teardown may end by re-delivering SIGINT.

    Every condition must hold: a POSIX platform, a non-interactive
    interpreter on the main thread, and the interpreter's own
    ``default_int_handler`` still installed for SIGINT. An interactive session
    (``python -i`` or a REPL) owns SIGINT itself — killing it would take down
    the interpreter's read-eval loop instead of handing back the prompt. A
    custom handler means an embedder owns Ctrl+C and receives the interrupt
    itself; ``SIG_IGN`` and ``SIG_DFL`` mean a KeyboardInterrupt cannot have
    come from a real SIGINT, and a missing handler could never be restored.
    In those cases the CLI keeps the plain ``SystemExit(130)``.
    """
    if os.name != "posix":
        return False
    if getattr(sys.flags, "interactive", False) or hasattr(sys, "ps1"):
        return False
    if threading.current_thread() is not threading.main_thread():
        return False
    try:
        current_handler = signal.getsignal(signal.SIGINT)
    except (ValueError, OSError):
        return False
    return current_handler is signal.default_int_handler


def _run_cli_entrypoint(
    body: Callable[[], None], *, owns_process: bool = False
) -> None:
    """Install CLI signal handlers, run ``body``, and map exit exceptions.

    Both CLI entrypoints (``main()`` and ``tunnelMain()``) share this helper so
    a Ctrl+C reports "Interrupted." with exit status 130 and an interface
    failure reports ``ERROR: ...`` with exit status 1 no matter which
    entrypoint launched the session. Signal dispositions are installed before
    ``body`` runs and restored in a ``finally`` afterward, whether ``body``
    returned, raised SystemExit from an exit mapping, or propagated another
    exception. A signal recorded during ``body`` is re-delivered after
    restoration so the process ends by the same signal it received; the signal
    then rides the host's restored disposition, so this re-delivery is
    universal for every caller. A Ctrl+C whose teardown produced the
    ``SystemExit(130)`` mapping is ended by re-delivering SIGINT only when
    ``owns_process`` is true — the interpreter's own behavior for an unhandled
    Ctrl+C, and the only death a shell's cooperative-exit logic treats as an
    abort. Only the process-owning entrypoints (the console scripts and
    ``python -m meshtastic``) may forge that fatal SIGINT; an application
    calling ``main()`` or ``tunnelMain()`` in-process keeps the plain
    catchable ``SystemExit(130)`` without a self-signal. When ``_cli_exit``
    raised anything else, or the recorded TERM/HUP path already terminated
    the process, the SIGINT re-delivery does not run.
    """
    redeliver_sigint = False
    global _cli_signal_defer_exit  # pylint: disable=global-statement
    global _cli_signal_invocation_active  # pylint: disable=global-statement
    global _cli_signal_received  # pylint: disable=global-statement
    global _cli_signal_shutdown_started  # pylint: disable=global-statement
    global _cli_signal_shutdown_lock  # pylint: disable=global-statement
    owns_handlers = (
        threading.current_thread() is threading.main_thread()
        and not _cli_signal_invocation_active
    )
    previous: dict[int, Any] = {}
    try:
        if owns_handlers:
            _cli_signal_invocation_active = True
            _cli_signal_defer_exit = True
        try:
            if owns_handlers:
                # Installation runs inside the interrupt mapping: Ctrl+C
                # during disposition setup still reports "Interrupted." and
                # the outer finally restores whatever was replaced.
                _install_cli_signal_handlers(previous)
                _cli_signal_defer_exit = False
                if _cli_signal_received is not None:
                    raise SystemExit(128 + _cli_signal_received)
            body()
        except KeyboardInterrupt:
            try:
                _cli_exit("Interrupted.", 130)
            except SystemExit as exc:
                if exc.code == 130:
                    redeliver_sigint = True
                raise
        except MeshInterface.MeshInterfaceError as exc:
            _cli_exit(f"ERROR: {exc}", 1)
        finally:
            if owns_handlers:
                _cli_signal_defer_exit = True
    finally:
        signum = None
        try:
            if owns_handlers:
                try:
                    _restore_cli_signal_handlers(previous, reset_state=False)
                    signum = _cli_signal_received
                finally:
                    _cli_signal_received = None
            if signum is not None:
                # A recorded TERM/HUP is fatal here (NoReturn), so it takes
                # precedence over the SIGINT re-delivery below. This re-delivery
                # is universal: the signal rides the host's restored disposition,
                # so a host handler runs and a host with SIG_DFL dies.
                _re_deliver_recorded_signal(signum)
            if owns_process and redeliver_sigint and _sigint_re_delivery_eligible():
                _flush_cli_output_streams()
                # Die by SIGINT like CPython does for an unhandled Ctrl+C; when
                # the delivery returns, the in-flight SystemExit(130) stands.
                _self_signal._signal_self(  # noqa: SLF001
                    signal.SIGINT, reset_to_default=True
                )
        finally:
            if owns_handlers:
                # The ownership reset must run even when restoration or the
                # re-delivery itself is interrupted: a KeyboardInterrupt
                # escaping either boundary would otherwise leave the
                # invocation-ownership flags set, and a later embedded
                # entrypoint would silently lose handler ownership for the
                # rest of the process.
                _cli_signal_shutdown_started = False
                _cli_signal_shutdown_lock = threading.Lock()
                _cli_signal_defer_exit = False
                _cli_signal_invocation_active = False


def _main_impl(*, owns_process: bool) -> None:
    """Run the shared CLI flow with the caller's process-ownership policy."""
    parser = argparse.ArgumentParser(
        add_help=False,
        epilog="If no connection arguments are specified, we search for a compatible serial device, "
        "and if none is found, then attempt a TCP connection to localhost.",
    )
    mt_config.parser = parser

    def body() -> None:
        initParser()
        common()

    _run_cli_entrypoint(body, owns_process=owns_process)


def main() -> None:
    """
    Run the Meshtastic-compatible command-line entry point.

    This function initializes the global parser via ``initParser()``,
    executes the shared CLI flow in ``common()``, and closes resources through
    the normal CLI session lifecycle. An application calling this in-process
    keeps the catchable ``SystemExit(130)`` after Ctrl+C; only the
    console-script entrypoints end a Ctrl+C by re-delivering SIGINT.
    """
    _main_impl(owns_process=False)


def _tunnel_main_impl(*, owns_process: bool) -> None:
    """Run the tunnel CLI flow with the caller's process-ownership policy."""
    parser = argparse.ArgumentParser(add_help=False)
    mt_config.parser = parser

    def body() -> None:
        initParser()
        args = mt_config.args
        if args is None:
            raise RuntimeError("initParser() did not set mt_config.args")
        args.tunnel = True
        mt_config.args = args
        common()

    _run_cli_entrypoint(body, owns_process=owns_process)


def tunnelMain() -> None:
    """Start the Meshtastic CLI in IP-tunnel mode.

    Set tunnel mode on the parsed CLI arguments and run the shared CLI initialization and execution flow.

    An application calling this in-process keeps the catchable
    ``SystemExit(130)`` after Ctrl+C; only the console-script entrypoints end
    a Ctrl+C by re-delivering SIGINT.

    Raises
    ------
    RuntimeError
        If CLI arguments could not be parsed or initialization failed.
    """
    _tunnel_main_impl(owns_process=False)


# The process-owning entrypoints are private and reserved for the console
# scripts and ``python -m meshtastic``: only an entrypoint that owns the
# process may forge a fatal SIGINT after a Ctrl+C teardown, while an
# application calling ``main()`` or ``tunnelMain()`` in-process keeps the
# plain catchable ``SystemExit(130)`` instead of being killed by a
# self-signal it never opted into.
def _console_main() -> None:
    """Process-owning entrypoint for the ``mtjk``/``meshtastic`` scripts."""
    _main_impl(owns_process=True)


def _console_tunnel_main() -> None:
    """Process-owning entrypoint for the ``mesh-tunnel`` script."""
    _tunnel_main_impl(owns_process=True)


if __name__ == "__main__":
    _console_main()
