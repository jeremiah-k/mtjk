"""Main Meshtastic."""

# pylint: disable=R0917,C0302

import argparse
import binascii
import contextlib
import getpass  # noqa: F401  # pylint: disable=unused-import  # compatibility seam
import importlib
import logging
import platform
import sys
import textwrap
import time
from collections.abc import Callable, Iterator, Sequence
from types import ModuleType
from typing import IO, Any, NoReturn, Protocol

from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.json_format import MessageToDict
from pubsub import pub

import meshtastic.cli.bootstrap as cli_bootstrap
import meshtastic.cli.channel_contact_actions as cli_channel_contact_actions
import meshtastic.cli.config_io as cli_config_io
import meshtastic.cli.configure_actions as cli_configure_actions
import meshtastic.cli.device_actions as cli_device_actions
import meshtastic.cli.dispatch as cli_dispatch
import meshtastic.cli.invocation as cli_invocation
import meshtastic.cli.messaging_service_actions as cli_messaging_service_actions
import meshtastic.cli.preference_runtime as cli_preference_runtime
import meshtastic.cli.qr as cli_qr
import meshtastic.cli.runtime as cli_runtime
import meshtastic.ota
import meshtastic.serial_interface
import meshtastic.tcp_interface
import meshtastic.util
from meshtastic import _topics, mt_config, remote_hardware
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
# pylint: disable=unused-import
from meshtastic.cli.parser import (  # noqa: F401,W0611 - legacy __main__ compatibility export
    _MODEM_PRESET_SHORTHANDS,
    addChannelConfigArgs,
    addConfigArgs,
    addConnectionArgs,
    addImportExportArgs,
    addLocalActionArgs,
    addPositionConfigArgs,
    addRemoteActionArgs,
    addRemoteAdminArgs,
    addSelectionArgs,
    parse_cli_args,
)
from meshtastic.cli.values import is_local_destination as _is_local_destination
from meshtastic.cli.values import (  # noqa: F401,W0611 - legacy __main__ compatibility export
    looks_like_integer_literal as _looks_like_integer_literal,
)

# black and isort disagree on these two statements: isort's line-length math
# excludes the noqa comment (so it collapses the statement to one line) while
# black includes it (so it must stay exploded). They are frozen compat
# re-exports, so skip them with an isort directive rather than fight it.
# isort: off
from meshtastic.cli.values import (
    parse_integer_literal as _parse_integer_literal,  # noqa: F401,W0611 - legacy __main__ compatibility export
)
from meshtastic.cli.values import (
    parse_modem_preset_name as _parse_modem_preset_name,  # noqa: F401,W0611 - legacy __main__ compatibility export
)

# isort: on
from meshtastic.configure_verify import (  # noqa: F401 - legacy __main__ compatibility export
    _verify_channel_url_against_state,
)
from meshtastic.host_port import parseHostAndPort
from meshtastic.interfaces.ble.interface import BLEInterface
from meshtastic.key_verification import (
    buildKeyVerificationAdmin,
    sendKeyVerification,
)
from meshtastic.lockdown import (
    build_lockdown_auth,
    read_lockdown_passphrase_file,
    send_lockdown_auth,
    validate_lockdown_passphrase,
)
from meshtastic.mesh_interface import MeshInterface
from meshtastic.mesh_interface_runtime import node_data
from meshtastic.protobuf import (  # noqa: F401 - legacy __main__ compatibility export
    admin_pb2,
    channel_pb2,
    config_pb2,
    localonly_pb2,
    mesh_pb2,
    portnums_pb2,
)
from meshtastic.version import (
    INSTALL_UPGRADE_HINT,
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
    meshtastic.util.our_exit(message, return_value)


def _current_invocation_args() -> argparse.Namespace | None:
    """Return invocation-owned arguments, falling back to legacy ``mt_config``."""
    invocation = cli_invocation.get_current_invocation()
    return invocation.args if invocation is not None else mt_config.args


def _current_camel_case() -> bool:
    """Return the active naming mode with legacy-global fallback."""
    invocation = cli_invocation.get_current_invocation()
    return invocation.camel_case if invocation is not None else mt_config.camel_case


def _current_channel_index() -> int | None:
    """Return the invocation-selected channel index with legacy fallback."""
    invocation = cli_invocation.get_current_invocation()
    return (
        invocation.channel_index if invocation is not None else mt_config.channel_index
    )


def _set_current_channel_index(value: int) -> None:
    """Update invocation-owned channel selection and mirror the compatibility global."""
    invocation = cli_invocation.get_current_invocation()
    if invocation is not None:
        invocation.channel_index = value
    mt_config.channel_index = value


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
    print(message)


def _report_pref_validation(message: str) -> None:
    """Report preference validation through the extracted runtime."""
    cli_preference_runtime.report_pref_validation(message, cli_print=_cli_print)


def supportInfo() -> None:
    """Print troubleshooting guidance and environment details useful for reporting CLI or library issues.

    Specifically prints the issue tracker URL and the running environment: system,
    platform string, kernel release, machine architecture, stdin/stdout encodings,
    installed distribution version (and available newer PyPI version if any),
    executable path, and Python implementation/version. Advises adding the output
    of the preferred CLI ``--info`` command when filing an issue.
    """
    print("")
    print(f"If having issues with {PROJECT_DISPLAY_NAME} CLI / python library")
    print("or wish to make feature requests, visit:")
    print(PROJECT_ISSUE_URL)
    print("When adding an issue, be sure to include the following info:")
    print(f" System: {platform.system()}")
    print(f"   Platform: {platform.platform()}")
    print(f"   Release: {platform.uname().release}")
    print(f"   Machine: {platform.uname().machine}")
    print(f"   Encoding (stdin): {sys.stdin.encoding}")
    print(f"   Encoding (stdout): {sys.stdout.encoding}")
    the_version = get_active_version()
    pypi_version = meshtastic.util.check_if_newer_version()
    if pypi_version:
        print(
            f" {PROJECT_DISPLAY_NAME}: v{the_version} (*** newer version v{pypi_version} available ***)"
        )
    else:
        print(f" {PROJECT_DISPLAY_NAME}: v{the_version}")
    print(f" Executable: {sys.argv[0]}")
    print(
        f" Python: {platform.python_version()} {platform.python_implementation()} {platform.python_compiler()}"
    )
    print("")
    print(f"Please add the output from the command: {PRIMARY_CLI_NAME} --info")


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
                    print(f"message: {msg}")
                    reply = (
                        f"got msg '{msg}' with rxSnr: {rxSnr} and hopLimit: {hopLimit}"
                    )
                    print(f"Received channel {rxChannel}. Sending reply: {reply}")
                    interface.sendText(reply, channelIndex=rxChannel)
                else:
                    print(
                        f"Ignored message on channel {rxChannel} (waiting for channel {targetChannel})"
                    )

    except Exception as ex:
        logger.warning("Error processing received packet: %s", ex)


def onClientNotification(
    notification: mesh_pb2.ClientNotification, interface: MeshInterface
) -> None:
    """Render spontaneous key-verification notifications for long-lived CLI sessions.

    Parameters
    ----------
    notification : mesh_pb2.ClientNotification
        Notification packet decoded from the ``meshtastic.clientNotification``
        pubsub topic. Only key-verification variants are rendered; all other
        notification types are intentionally ignored.

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
    if notification.WhichOneof("payload_variant") not in {
        "key_verification_number_inform",
        "key_verification_number_request",
        "key_verification_final",
    }:
        return
    try:
        cli_device_actions._render_key_verification_notification(  # noqa: SLF001
            notification, "", _cli_print
        )
    except Exception as ex:
        logger.warning("Error rendering client notification: %s", ex)


def onConnection(interface: MeshInterface, topic: Any = pub.AUTO_TOPIC) -> None:
    """Notify about a change in the radio connection state."""
    _ = interface
    topic_name = topic.getName() if hasattr(topic, "getName") else str(topic)
    _cli_print(f"Connection changed: {topic_name}")


def checkChannel(interface: MeshInterface, channelIndex: int) -> bool:
    """Determine whether the local node has the channel at the given index enabled."""
    if hasattr(type(interface.localNode), "getChannelCopyByChannelIndex"):
        ch = interface.localNode.getChannelCopyByChannelIndex(channelIndex)
    else:
        ch = interface.localNode.getChannelByChannelIndex(channelIndex)
    logger.debug("ch:%s", ch)
    return bool(ch and ch.role != channel_pb2.Channel.Role.DISABLED)


_normalize_pref_name = cli_preference_runtime.normalize_pref_name
_parse_bitfield_value = cli_preference_runtime.parse_bitfield_value


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


class _NamedConfigType(Protocol):
    """Protocol for config section objects exposing a `name` attribute."""

    name: str


_is_secret_pref = cli_preference_runtime.is_secret_pref
_redact_pref_value = cli_preference_runtime.redact_pref_value


def getPref(node: Any, comp_name: str, *, allow_secrets: bool = False) -> bool:
    """Retrieve and display a node configuration preference or populated section fields.

    Parameters
    ----------
    node : Any
        Node exposing local and module configuration data and configuration requests.
    comp_name : str
        Preference path or configuration section name to retrieve.
    allow_secrets : bool
        Whether sensitive preference values may be displayed without redaction.

    Returns
    -------
    bool
        ``True`` if the preference exists and values were displayed or requested,
        ``False`` if it was not found.
    """

    def _print_setting(
        config_type: _NamedConfigType,
        uni_name: str,
        pref_value: str | list[str],
        *,
        repeated: bool,
        secret_name: str,
    ) -> None:
        """Print a configuration preference and its value to stdout and the debug log.

        When `repeated` is True, `pref_value` is treated as an iterable and
            each element is converted to a string; otherwise the single value is
            converted to a string. Output is formatted as "<section>.<name>: <value>".

        Parameters
        ----------
        config_type : _NamedConfigType
            Object with a `name` attribute identifying the configuration section.
        uni_name : str
            The preference name within the configuration section.
        pref_value : str | list[str]
            The preference value to print; an iterable when `repeated` is True.
        repeated : bool
            If True, treat `pref_value` as a sequence and print the list of stringified values.
        secret_name : str
            Canonical snake_case field name used to determine whether to redact.
        """
        if repeated:
            display_value: str | list[str] = [
                (
                    meshtastic.util.toStr(v)
                    if allow_secrets
                    else _redact_pref_value(secret_name, meshtastic.util.toStr(v))
                )
                for v in pref_value
            ]
            log_value: str | list[str] = [
                _redact_pref_value(secret_name, meshtastic.util.toStr(v))
                for v in pref_value
            ]
        else:
            raw_display = meshtastic.util.toStr(pref_value)
            display_value = (
                raw_display
                if allow_secrets
                else _redact_pref_value(secret_name, raw_display)
            )
            log_value = _redact_pref_value(secret_name, raw_display)
        print(f"{str(config_type.name)}.{uni_name}: {str(display_value)}")
        logger.debug("%s.%s: %s", config_type.name, uni_name, log_value)

    comp_name = _normalize_pref_name(comp_name)
    name = splitCompoundName(comp_name)
    wholeField = name[0] == name[1]  # We want the whole field

    camel_name = meshtastic.util.snake_to_camel(name[1])
    # Note: protobufs has the keys in snake_case, so snake internally
    snake_name = meshtastic.util.camel_to_snake(name[1])
    uni_name = camel_name if _current_camel_case() else snake_name
    logger.debug("snake_name:%s camel_name:%s", snake_name, camel_name)
    logger.debug("use camel:%s", _current_camel_case())

    # First validate the input
    localConfig = node.localConfig
    moduleConfig = node.moduleConfig
    found: bool = False
    config = localConfig
    config_type = None
    pref = None
    for config in [localConfig, moduleConfig]:
        objDesc = config.DESCRIPTOR
        config_type = objDesc.fields_by_name.get(name[0])
        pref = None
        if config_type:
            pref = config_type.message_type.fields_by_name.get(snake_name)
            if pref is not None or wholeField:
                found = True
                break

    if not found:
        print(
            f"{localConfig.__class__.__name__} and {moduleConfig.__class__.__name__} do not have an attribute {uni_name}."
        )
        print("Choices are...")
        printConfig(localConfig)
        printConfig(moduleConfig)
        return False

    # Check if we need to request the config
    if config_type is None:
        return False

    if len(config.ListFields()) != 0 and (pref is not None or wholeField):
        # read the value
        config_values = getattr(config, config_type.name)
        if not wholeField:
            if pref is None:
                return False
            pref_value = getattr(config_values, pref.name)
            repeated = _is_repeated_field(pref)
            _print_setting(
                config_type,
                uni_name,
                pref_value,
                repeated=repeated,
                secret_name=f"{config_type.name}.{snake_name}",
            )
        else:
            for field in config_values.ListFields():
                repeated = _is_repeated_field(field[0])
                _print_setting(
                    config_type,
                    field[0].name,
                    field[1],
                    repeated=repeated,
                    secret_name=f"{config_type.name}.{field[0].name}",
                )
    else:
        # Always show whole field for remote node
        node.requestConfig(config_type)

    return True


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


def _print_channel_field_choices(settings: Any, pref_name: str) -> None:
    """Print available channel-setting fields after an unknown --ch-set name."""
    print(f"{settings.__class__.__name__} does not have an attribute {pref_name}.")
    print("Choices are...")
    for field in settings.DESCRIPTOR.fields:
        if field.name != "module_settings":
            print(field.name)
            continue
        print(f"{field.name}:")
        if field.message_type is None:
            continue
        for sub_field in sorted(field.message_type.fields, key=lambda item: item.name):
            print(f"    {field.name}.{sub_field.name}")


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


def _print_set_field_choices(node: Any, pref_names: Sequence[str]) -> None:
    """Print historical field-not-found guidance for one or more --set names.

    Parameters
    ----------
    node : Any
        Node whose local and module configuration schemas provide the choices.
    pref_names : Sequence[str]
        Unknown preference names to identify before printing choices.
    """
    names = list(dict.fromkeys(pref_names))
    for pref_name in names:
        print(
            f"{node.localConfig.__class__.__name__} and "
            f"{node.moduleConfig.__class__.__name__} do not have an attribute {pref_name}."
        )
    print("Choices are...")
    printConfig(node.localConfig)
    printConfig(node.moduleConfig)


def _normalize_set_entries(
    set_entries: Sequence[Sequence[Any] | None],
) -> list[tuple[str, Any]]:
    """Normalize argparse --set entries into explicit name/value pairs.

    Parameters
    ----------
    set_entries : Sequence[Sequence[Any] | None]
        Parsed ``--set`` entries, each containing at least a field name and value.
        Any additional elements are ignored for compatibility with the historical
        ``item[0]``/``item[1]`` parsing contract.

    Returns
    -------
    list[tuple[str, Any]]
        Normalized ``(field_name, value)`` pairs with incomplete entries omitted.
    """
    return [
        (str(item[0]), item[1])
        for item in set_entries
        if item is not None and len(item) >= 2
    ]


def _format_set_preflight_exception(pref_name: str, exc: Exception) -> str:
    """Format a preflight exception without exposing secret preference values.

    Parameters
    ----------
    pref_name : str
        Canonical preference path whose validation raised ``exc``.
    exc : Exception
        Validation exception raised by the protobuf/runtime conversion path.

    Returns
    -------
    str
        Safe error detail suitable for the aggregated CLI failure message.
    """
    if isinstance(exc, _PreferenceValueError):
        return str(exc)
    if _is_secret_pref(pref_name):
        return (
            f"{pref_name}: invalid value {_REDACTED_PREF_VALUE} ({type(exc).__name__})"
        )
    return f"{pref_name}: {exc}"


def _resolve_set_target(
    configs: Sequence[Any], pref_name: str
) -> tuple[Any, FieldDescriptor] | None:
    """Resolve the owning config message and root field for a normalized preference.

    Parameters
    ----------
    configs : Sequence[Any]
        Configuration wrapper messages to search in resolution order.
    pref_name : str
        Normalized dotted preference path.

    Returns
    -------
    tuple[Any, FieldDescriptor] | None
        Owning config wrapper and root field descriptor, or ``None`` when the
        root field does not exist in any supplied configuration.
    """
    root_field = splitCompoundName(pref_name)[0]
    for config in configs:
        config_type = config.DESCRIPTOR.fields_by_name.get(root_field)
        if config_type is not None:
            return config, config_type
    return None


def _ensure_set_sections_loaded(
    node: Any, set_entries: Sequence[tuple[str, Any]]
) -> None:
    """Request missing config sections before creating preflight snapshots.

    Parameters
    ----------
    node : Any
        Target node whose cached local/module configuration will be validated.
    set_entries : Sequence[tuple[str, Any]]
        Parsed ``--set`` name/value entries. Names are normalized before resolution.

    Notes
    -----
    Requests are deduplicated by config section. Protobuf message presence, not
    ``ListFields()``, distinguishes an already-loaded default-valued section from
    a section that has never been received. Unknown preference paths do not
    trigger device reads.
    """
    configs = (node.localConfig, node.moduleConfig)
    requested_sections: set[tuple[str, str]] = set()
    for raw_pref_name, _raw_value in set_entries:
        pref_name = _normalize_pref_name(raw_pref_name)
        resolved = _resolve_set_target(configs, pref_name)
        if resolved is None:
            continue
        config, config_type = resolved
        if not _resolve_pref(config, pref_name):
            continue

        section_key = (config.DESCRIPTOR.full_name, config_type.name)
        if section_key in requested_sections:
            continue
        requested_sections.add(section_key)
        if not config.HasField(config_type.name):
            node.requestConfig(config_type)


def _preflight_set_entries(node: Any, set_entries: Sequence[tuple[str, Any]]) -> bool:
    """
    Validate all --set entries against configuration copies before applying changes.

    Parameters
    ----------
    node : Any
        Node providing the current local and module configuration.
    set_entries : Sequence[tuple[str, Any]]
        Preference names and raw values to validate as one batch.

    Returns
    -------
    bool
        ``True`` if every entry is valid; ``False`` if an unknown field or semantic
        validation failure rejects the batch.
    """
    candidates: list[Any] = []
    for source in (node.localConfig, node.moduleConfig):
        candidate = type(source)()
        candidate.CopyFrom(source)
        candidates.append(candidate)

    fatal_errors: list[str] = []
    unknown_fields: list[str] = []
    value_rejections: list[tuple[str, tuple[str, ...]]] = []
    token = _CONFIGURE_PREFLIGHT_MODE.set(True)
    try:
        for raw_pref_name, raw_value in set_entries:
            pref_name = _normalize_pref_name(raw_pref_name)
            resolved = _resolve_set_target(candidates, pref_name)
            if resolved is None:
                unknown_fields.append(pref_name)
                continue
            candidate, _config_type = resolved
            if not _resolve_pref(candidate, pref_name):
                unknown_fields.append(pref_name)
                continue

            validation_messages: list[str] = []
            reporter_token = _PREF_VALIDATION_REPORTER.set(validation_messages.append)
            try:
                try:
                    with _fatal_preference_value_errors():
                        valid = setPref(candidate, pref_name, raw_value)
                finally:
                    _PREF_VALIDATION_REPORTER.reset(reporter_token)
            except (TypeError, ValueError, OverflowError, binascii.Error) as exc:
                fatal_errors.append(_format_set_preflight_exception(pref_name, exc))
                continue
            if not valid:
                value_rejections.append((pref_name, tuple(validation_messages)))
    finally:
        _CONFIGURE_PREFLIGHT_MODE.reset(token)

    if unknown_fields:
        _print_set_field_choices(node, unknown_fields)

    if fatal_errors:
        detail_lines = [f"  - {error}" for error in fatal_errors]
        for pref_name, messages in value_rejections:
            detail_lines.append(f"  - {pref_name}: {_SET_VALUE_REJECTED_MESSAGE}")
            detail_lines.extend(f"      {message}" for message in messages)
        details = "\n".join(detail_lines)
        _cli_exit(f"ERROR: --set batch rejected before applying changes:\n{details}")

    for pref_name, messages in value_rejections:
        if messages:
            for message in messages:
                _report_pref_validation(message)
        else:
            _report_pref_validation(f"{pref_name}: {_SET_VALUE_REJECTED_MESSAGE}")

    return not (unknown_fields or value_rejections)


def _handle_set_command(
    interface: MeshInterface,
    args: Any,
    getNode_kwargs: dict[str, Any],
) -> None:
    """
    Validate and apply a CLI ``--set`` batch without partial updates.

    Parameters
    ----------
    interface : MeshInterface
        Active interface used to resolve the target node.
    args : Any
        Parsed CLI arguments containing the ``--set`` entries and destination.
    getNode_kwargs : dict[str, Any]
        Additional keyword arguments forwarded to ``interface.getNode``.

    Notes
    -----
    Invalid batches are rejected before modifying the device. Valid batches write
    all affected configuration sections and use a transaction when multiple
    sections are changed.
    """
    node = interface.getNode(args.dest, False, **getNode_kwargs)
    set_entries = _normalize_set_entries(args.set)
    _ensure_set_sections_loaded(node, set_entries)
    if not _preflight_set_entries(node, set_entries):
        return

    live_configs = (node.localConfig, node.moduleConfig)
    fields: set[str] = set()
    for raw_pref_name, raw_value in set_entries:
        normalized_pref_name = _normalize_pref_name(raw_pref_name)
        resolved = _resolve_set_target(live_configs, normalized_pref_name)
        if resolved is None:
            _cli_exit(
                "ERROR: --set field no longer resolves after successful preflight: "
                f"{normalized_pref_name}."
            )
        config, config_type = resolved
        try:
            found = setPref(config, normalized_pref_name, raw_value)
        except (TypeError, ValueError, OverflowError, binascii.Error) as exc:
            detail = _format_set_preflight_exception(normalized_pref_name, exc)
            _cli_exit(
                f"ERROR: --set apply diverged after successful preflight:\n  - {detail}"
            )
        if not found:
            _cli_exit(
                "ERROR: --set apply diverged after successful preflight for "
                f"{normalized_pref_name}."
            )
        fields.add(config_type.name)

    if fields:
        _cli_print("Writing modified preferences to device")
        if len(fields) > 1:
            _cli_print("Using a configuration transaction")
            node.beginSettingsTransaction()
        for field in fields:
            _cli_print(f"Writing {field} configuration to device")
            node.writeConfig(field)
        if len(fields) > 1:
            node.commitSettingsTransaction()


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


def _validate_cli_show_fields(interface: MeshInterface, show_fields: list[str]) -> None:
    """Reject unavailable --show-fields values with a concrete choice list."""
    nodes_by_num = getattr(interface, "nodesByNum", None)
    observed_nodes = (
        list(nodes_by_num.values()) if isinstance(nodes_by_num, dict) else []
    )
    available = node_data.get_known_field_paths(observed_nodes)
    available_set = set(available)
    invalid = [field for field in show_fields if field not in available_set]
    if invalid:
        choices = textwrap.fill(
            ", ".join(available),
            width=100,
            initial_indent="  ",
            subsequent_indent="  ",
            break_long_words=False,
            break_on_hyphens=False,
        )
        _cli_exit(
            "Unknown --show-fields value(s): "
            f"{', '.join(invalid)}.\nAvailable fields:\n{choices}",
            1,
        )


def _build_connected_dispatch_hooks() -> cli_dispatch.DispatchHooks:
    """Build connected-action hooks from historical ``__main__`` seams."""
    device_hooks = cli_device_actions.DeviceActionHooks(
        cli_exit=_cli_exit,
        cli_print=_cli_print,
        set_pref=setPref,
        is_local_destination=_is_local_destination,
        send_local_factory_reset_and_wait=_send_local_factory_reset_and_wait,
        post_factory_reset_ready_probe=_post_factory_reset_ready_probe,
        handle_ota_update=_handle_ota_update,
        build_lockdown_auth=build_lockdown_auth,
        read_lockdown_passphrase_file=read_lockdown_passphrase_file,
        send_lockdown_auth=send_lockdown_auth,
        validate_lockdown_passphrase=validate_lockdown_passphrase,
        build_key_verification_admin=buildKeyVerificationAdmin,
        send_key_verification=sendKeyVerification,
    )
    channel_contact_hooks = cli_channel_contact_actions.ChannelContactHooks(
        cli_exit=_cli_exit,
        cli_print=_cli_print,
        get_channel_index=_current_channel_index,
        set_channel_index=_set_current_channel_index,
        resolve_pref=_resolve_pref,
        set_pref=setPref,
        fatal_preference_value_errors=_fatal_preference_value_errors,
        preference_value_error=_PreferenceValueError,
        print_channel_field_choices=_print_channel_field_choices,
        is_local_destination=_is_local_destination,
        modem_preset_shorthands=_MODEM_PRESET_SHORTHANDS,
        qr_render=(cli_qr.render_terminal_qr if cli_qr.segno is not None else None),
    )
    configure_hooks = cli_configure_actions.ConfigureActionHooks(
        handle_set_command=_handle_set_command,
        export_profile=exportProfile,
        handle_configure_command=_handle_configure_command,
        export_config=exportConfig,
        cli_exit=_cli_exit,
        cli_print=_cli_print,
        is_local_destination=_is_local_destination,
    )
    service_hooks = cli_messaging_service_actions.MessagingServiceHooks(
        cli_exit=_cli_exit,
        cli_print=_cli_print,
        get_channel_index=_current_channel_index,
        check_channel=checkChannel,
        remote_hardware_client=remote_hardware.RemoteHardwareClient,
        get_pref=getPref,
        validate_cli_show_fields=_validate_cli_show_fields,
        newer_version=meshtastic.util.check_if_newer_version,
        install_upgrade_hint=INSTALL_UPGRADE_HINT,
        powermon_available=lambda: have_powermon,
        powermon_error=lambda: powermon_exception,
        log_set_factory=LogSet,
        power_stress_factory=PowerStress,
        get_meter=lambda: meter,
    )
    return cli_dispatch.DispatchHooks(
        cli_print=_cli_print,
        device=device_hooks,
        channel_contact=channel_contact_hooks,
        configure=configure_hooks,
        services=service_hooks,
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


def printConfig(config: Any) -> None:
    """COMPAT_STABLE_SHIM: print config fields through the CLI config I/O runtime."""
    cli_config_io.print_config(config, camel_case=_current_camel_case())


def printAvailableConfigFields() -> None:
    """COMPAT_STABLE_SHIM: print config fields and aliases through the runtime."""
    cli_config_io.print_available_config_fields(
        camel_case=_current_camel_case(),
        aliases=_PREFERENCE_FIELD_ALIASES,
        display_pref_name=_display_pref_name,
        local_config_factory=localonly_pb2.LocalConfig,
        module_config_factory=localonly_pb2.LocalModuleConfig,
    )


def _describe_config_field(field_name: str) -> bool:
    """Describe one protobuf-backed CLI field without connecting to a device."""
    return cli_config_io._describe_config_field(
        field_name,
        normalize_pref_name=cli_preference_runtime.normalize_pref_name,
        display_pref_name=_display_pref_name,
        type_label=cli_preference_runtime.protobuf_field_type_label,
        bitfield_enums=cli_preference_runtime.BITFIELD_ENUMS,
        local_config_factory=localonly_pb2.LocalConfig,
        module_config_factory=localonly_pb2.LocalModuleConfig,
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
            "You may need to run `poetry install --with powermon`. "
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


def main() -> None:
    """
    Run the Meshtastic-compatible command-line entry point.

    This function initializes the global parser via ``initParser()``,
    executes the shared CLI flow in ``common()``, and closes resources through
    the normal CLI session lifecycle.
    """
    parser = argparse.ArgumentParser(
        add_help=False,
        epilog="If no connection arguments are specified, we search for a compatible serial device, "
        "and if none is found, then attempt a TCP connection to localhost.",
    )
    mt_config.parser = parser
    try:
        initParser()
        common()
    except KeyboardInterrupt:
        _cli_exit("Interrupted.", 130)
    except MeshInterface.MeshInterfaceError as exc:
        _cli_exit(f"ERROR: {exc}", 1)


def tunnelMain() -> None:
    """Start the Meshtastic CLI in IP-tunnel mode.

    Set tunnel mode on the parsed CLI arguments and run the shared CLI initialization and execution flow.

    Raises
    ------
    RuntimeError
        If CLI arguments could not be parsed or initialization failed.
    """
    parser = argparse.ArgumentParser(add_help=False)
    mt_config.parser = parser
    initParser()
    args = mt_config.args
    if args is None:
        raise RuntimeError("initParser() did not set mt_config.args")
    args.tunnel = True
    mt_config.args = args
    common()


if __name__ == "__main__":
    main()
