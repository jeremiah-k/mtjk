"""Argument-group builders for the Meshtastic command-line interface."""

from __future__ import annotations

import argparse
import math
import platform
import sys
from collections.abc import Sequence
from typing import Protocol

from meshtastic.cli.values import parse_modem_preset_name as _parse_modem_preset_name
from meshtastic.key_verification import (
    DEFAULT_KEY_VERIFICATION_TIMEOUT_SECONDS as _DEFAULT_KEY_VERIFY_WAIT,
)
from meshtastic.key_verification import (
    KEY_VERIFICATION_STAGES as _KEY_VERIFICATION_STAGES,
)
from meshtastic.key_verification import SECURITY_NUMBER_MAX, SECURITY_NUMBER_MIN
from meshtastic.node_runtime.shared import MAX_INPUT_EVENT_CODE as _MAX_INPUT_EVENT_CODE
from meshtastic.node_runtime.shared import MAX_INPUT_KB_CHAR as _MAX_INPUT_KB_CHAR
from meshtastic.node_runtime.shared import MAX_INPUT_TOUCH_X as _MAX_INPUT_TOUCH_X
from meshtastic.node_runtime.shared import MAX_INPUT_TOUCH_Y as _MAX_INPUT_TOUCH_Y
from meshtastic.node_runtime.shared import _delete_file_path_error


class _ArgcompleteModule(Protocol):
    """Structural type for the optional argcomplete dependency."""

    def autocomplete(self, parser: argparse.ArgumentParser) -> object:
        """Enable shell completion for ``parser``."""


_UINT64_MAX: int = (1 << 64) - 1


def _parse_uint64(value: str) -> int:
    """Parse one CLI integer constrained to an unsigned protobuf 64-bit field."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected an integer, got {value!r}") from exc
    if not 0 <= parsed <= _UINT64_MAX:
        raise argparse.ArgumentTypeError(
            f"value must be between 0 and {_UINT64_MAX}, got {parsed}"
        )
    return parsed


def _parse_key_verification_security_number(value: str) -> int:
    """Parse the six-digit firmware key-verification security number."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected an integer, got {value!r}") from exc
    if not SECURITY_NUMBER_MIN <= parsed <= SECURITY_NUMBER_MAX:
        raise argparse.ArgumentTypeError(
            f"security number must be between {SECURITY_NUMBER_MIN} and "
            f"{SECURITY_NUMBER_MAX}, got {parsed}"
        )
    return parsed


def _parse_positive_finite_float(value: str) -> float:
    """Parse one positive finite floating-point CLI value."""
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a number, got {value!r}") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("value must be finite and greater than zero")
    return parsed


def _parse_firmware_keyboard_char(value: str) -> str:
    """Validate one character representable by the firmware's 8-bit input field."""
    if len(value) != 1 or ord(value) > _MAX_INPUT_KB_CHAR:
        raise argparse.ArgumentTypeError(
            "keyboard input must be exactly one character with code point "
            f"<= {_MAX_INPUT_KB_CHAR}"
        )
    return value


def _parse_input_event_uint8(value: str) -> int:
    """Parse an input-event field constrained to the firmware's unsigned byte range."""
    return _parse_bounded_uint(value, _MAX_INPUT_EVENT_CODE, "--send-input-event")


def _parse_input_touch_x(value: str) -> int:
    """Parse the touch X coordinate using the firmware field width."""
    return _parse_bounded_uint(value, _MAX_INPUT_TOUCH_X, "--input-touch-x")


def _parse_input_touch_y(value: str) -> int:
    """Parse the touch Y coordinate using the firmware field width."""
    return _parse_bounded_uint(value, _MAX_INPUT_TOUCH_Y, "--input-touch-y")


def _parse_bounded_uint(value: str, upper: int, label: str) -> int:
    """Parse one CLI integer constrained to ``[0, upper]`` for a named field."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected an integer, got {value!r}") from exc
    if not 0 <= parsed <= upper:
        raise argparse.ArgumentTypeError(
            f"{label} must be between 0 and {upper}, got {parsed}"
        )
    return parsed


def _parse_absolute_device_path(value: str) -> str:
    """Validate a device-side file path before opening a transport session.

    Reuses the Node-side MAX_DELETE_FILE_PATH_BYTES bound and the shared
    over-length message so CLI and library callers report identical failure
    text and agree on the firmware UTF-8 byte budget.
    """
    if not value.startswith("/"):
        raise argparse.ArgumentTypeError("device file path must be absolute")
    validation_error = _delete_file_path_error(value)
    if validation_error is not None:
        raise argparse.ArgumentTypeError(validation_error)
    return value


_MODEM_PRESET_SHORTHANDS: tuple[tuple[tuple[str, ...], str, str, str], ...] = (
    (
        ("--ch-vlongslow",),
        "ch_vlongslow",
        "VERY_LONG_SLOW",
        "Change to the VERY_LONG_SLOW modem preset. Deprecated since 2.5 firmware.",
    ),
    (
        ("--ch-longslow",),
        "ch_longslow",
        "LONG_SLOW",
        "Change to the LONG_SLOW modem preset. Deprecated since 2.7 firmware.",
    ),
    (
        ("--ch-longmod", "--ch-longmoderate"),
        "ch_longmod",
        "LONG_MODERATE",
        "Change to the LONG_MODERATE modem preset",
    ),
    (
        ("--ch-longfast",),
        "ch_longfast",
        "LONG_FAST",
        "Change to the LONG_FAST modem preset",
    ),
    (
        ("--ch-longturbo",),
        "ch_longturbo",
        "LONG_TURBO",
        "Change to the LONG_TURBO modem preset",
    ),
    (
        ("--ch-medslow",),
        "ch_medslow",
        "MEDIUM_SLOW",
        "Change to the MEDIUM_SLOW modem preset",
    ),
    (
        ("--ch-medfast",),
        "ch_medfast",
        "MEDIUM_FAST",
        "Change to the MEDIUM_FAST modem preset",
    ),
    (
        ("--ch-shortslow",),
        "ch_shortslow",
        "SHORT_SLOW",
        "Change to the SHORT_SLOW modem preset",
    ),
    (
        ("--ch-shortfast",),
        "ch_shortfast",
        "SHORT_FAST",
        "Change to the SHORT_FAST modem preset",
    ),
    (
        ("--ch-shortturbo",),
        "ch_shortturbo",
        "SHORT_TURBO",
        "Change to the SHORT_TURBO modem preset",
    ),
)


def addConnectionArgs(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Register connection-related command-line arguments (serial, TCP, and BLE) on the given parser.

    Adds a mutually exclusive group for serial (--port / --serial / -s), TCP (--host / --tcp
    / -t), and BLE (--ble / -b), and also adds the --ble-scan and
    --ble-auto-reconnect flags.

    Parameters
    ----------
    parser : argparse.ArgumentParser
        The argument parser to add connection arguments to.

    Returns
    -------
    argparse.ArgumentParser
        The same parser with the connection arguments added.
    """

    outer = parser.add_argument_group(
        "Connection",
        "Optional arguments that specify how to connect to a Meshtastic device.",
    )
    group = outer.add_mutually_exclusive_group()
    group.add_argument(
        "--port",
        "--serial",
        "-s",
        help="The port of the device to connect to using serial, e.g. /dev/ttyUSB0. (defaults to trying to detect a port)",
        nargs="?",
        const=None,
        default=None,
    )

    group.add_argument(
        "--host",
        "--tcp",
        "-t",
        help="Connect to a device using TCP, optionally passing hostname/IP or host:port (default port 4403). (defaults to '%(const)s')",
        nargs="?",
        default=None,
        const="localhost",
        metavar="HOST[:PORT]",
    )

    group.add_argument(
        "--ble",
        "-b",
        help="Connect to a BLE device, optionally specifying a device name (defaults to '%(const)s')",
        nargs="?",
        default=None,
        const="any",
    )

    outer.add_argument(
        "--ble-scan",
        help="Scan for Meshtastic BLE devices that may be available to connect to",
        action="store_true",
    )

    outer.add_argument(
        "--ble-auto-reconnect",
        help="Enable BLE auto-reconnect after unexpected disconnects (disabled by default)",
        action="store_true",
    )

    return parser


def addSelectionArgs(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Add destination and channel selection arguments to the provided ArgumentParser.

    Adds the `--dest` option for specifying a destination node (node ID with '!' or '0x'
    prefix, or node number) and the `--ch-index` option for selecting a channel index
    (channels start at 0; 0 is the PRIMARY channel).

    Parameters
    ----------
    parser : argparse.ArgumentParser
        The argument parser to extend.

    Returns
    -------
    argparse.ArgumentParser
        The same parser instance with the selection arguments added.
    """
    group = parser.add_argument_group(
        "Selection", "Arguments that select channels to use, destination nodes, etc."
    )

    group.add_argument(
        "--dest",
        help="The destination node id for any sent commands. If not set '^all' or '^local' is assumed."
        "Use the node ID with a '!' or '0x' prefix or the node number.",
        default=None,
        metavar="!xxxxxxxx",
    )

    group.add_argument(
        "--ch-index",
        help="Set the specified channel index for channel-specific commands. Channels start at 0 (0 is the PRIMARY channel).",
        action="store",
        metavar="INDEX",
    )

    return parser


def addImportExportArgs(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Register CLI options for importing and exporting device configuration.

    Parameters
    ----------
    parser : argparse.ArgumentParser
        The argument parser to extend.

    Returns
    -------
    argparse.ArgumentParser
        The same parser instance with the import/export arguments added.
    """
    group = parser.add_argument_group(
        "Import/Export",
        "Arguments that concern importing and exporting configuration of Meshtastic devices",
    )

    group.add_argument(
        "--configure",
        help=(
            "Specify a YAML (.yaml/.yml) or binary DeviceProfile (.cfg/.bin) "
            "file containing settings for the connected device."
        ),
        action="append",
    )
    group.add_argument(
        "--export-config",
        nargs="?",
        const="-",  # default to "-" if no value provided
        metavar="FILE",
        help="Export device config (to stdout if no file given); format set by --export-format",
    )
    group.add_argument(
        "--export-format",
        choices=["auto", "yaml", "binary", "protobuf"],
        default="auto",
        help="Config export format; auto picks binary for .cfg/.bin files, else YAML",
    )
    return parser


def addConfigArgs(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Add configuration-related CLI arguments to the given ArgumentParser.

    Adds options for reading and writing preference fields, beginning/committing configuration transactions,
    managing canned messages and ringtones, selecting modem preset shortcuts, setting owner/ham/messageability,
    and helpers for channel URLs.

    Parameters
    ----------
    parser : argparse.ArgumentParser
        The argument parser to add connection arguments to.

    Returns
    -------
    argparse.ArgumentParser
        The same parser instance with configuration arguments added.
    """

    group = parser.add_argument_group(
        "Configuration",
        "Arguments that concern general configuration of Meshtastic devices",
    )

    group.add_argument(
        "--get",
        help=(
            "Get a preferences field. Use --list-fields to print all available fields"
            " and --describe-field for schema metadata. Can use either snake_case or camelCase"
            " format. (ex: 'power.ls_secs' or 'power.lsSecs')"
        ),
        nargs=1,
        action="append",
        metavar="FIELD",
    )

    group.add_argument(
        "--list-fields",
        help=(
            "List configurable fields from protobuf schemas and exit. Includes"
            " compatibility aliases. Use --describe-field for schema metadata."
        ),
        action="store_true",
    )

    group.add_argument(
        "--describe-field",
        metavar="FIELD",
        help=(
            "Describe a configurable field from the current protobuf schema and exit."
            " Shows type information plus any schema-provided labels, bounds, flags,"
            " keywords, and enum value metadata."
        ),
    )

    group.add_argument(
        "--set",
        help=(
            "Set a preferences field. Can use either snake_case or camelCase format."
            " Use --describe-field to inspect schema bounds and enum choices."
            " (ex: 'power.ls_secs' or 'power.lsSecs'). May be less reliable when"
            " setting properties from more than one configuration section."
        ),
        nargs=2,
        action="append",
        metavar=("FIELD", "VALUE"),
    )

    group.add_argument(
        "--begin-edit",
        help="Tell the node to open a transaction to edit settings",
        action="store_true",
    )

    group.add_argument(
        "--commit-edit",
        help="Tell the node to commit open settings transaction",
        action="store_true",
    )

    group.add_argument(
        "--get-canned-message",
        help="Show the canned message plugin message",
        action="store_true",
    )

    group.add_argument(
        "--set-canned-message",
        help="Set the canned messages plugin message (up to 200 characters).",
        action="store",
    )

    group.add_argument(
        "--get-ringtone", help="Show the stored ringtone", action="store_true"
    )

    group.add_argument(
        "--set-ringtone",
        help="Set the Notification Ringtone (up to 230 characters).",
        action="store",
        metavar="RINGTONE",
    )

    for flags, destination, _, help_text in _MODEM_PRESET_SHORTHANDS:
        group.add_argument(
            *flags,
            dest=destination,
            help=help_text,
            action="store_true",
        )

    group.add_argument(
        "--ch-preset",
        type=_parse_modem_preset_name,
        metavar="PRESET",
        help=(
            "Change to any modem preset defined by the active protobuf schema. "
            "Names are case-insensitive and may use '-' or '_' separators."
        ),
    )

    group.add_argument("--set-owner", help="Set device owner name", action="store")

    group.add_argument(
        "--set-owner-short", help="Set device owner short name", action="store"
    )

    group.add_argument(
        "--set-ham", help="Set licensed Ham ID and turn off encryption", action="store"
    )

    group.add_argument(
        "--set-is-unmessageable",
        "--set-is-unmessagable",
        help="Set if a node is messageable or not",
        action="store",
    )

    group.add_argument(
        "--ch-set-url",
        "--seturl",
        help="Set all channels and set LoRa config from a supplied URL",
        metavar="URL",
        action="store",
    )

    group.add_argument(
        "--ch-add-url",
        help="Add secondary channels and set LoRa config from a supplied URL",
        metavar="URL",
        default=None,
    )

    return parser


def addChannelConfigArgs(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Add channel-related CLI options to the provided argument parser.

    Adds arguments for adding/deleting channels, setting channel parameters (including PSK),
    QR display for channels, enable/disable flags, and a retry count for fetching channel settings.

    Parameters
    ----------
    parser : argparse.ArgumentParser
        The argument parser to extend.

    Returns
    -------
    argparse.ArgumentParser
        The same parser instance with channel configuration options added.
    """

    group = parser.add_argument_group(
        "Channel Configuration",
        "Arguments that concern configuration of channels",
    )

    group.add_argument(
        "--ch-add",
        help="Add a secondary channel, you must specify a channel name",
        default=None,
    )

    group.add_argument(
        "--ch-del", help="Delete the ch-index channel", action="store_true"
    )

    group.add_argument(
        "--ch-set",
        help=(
            "Set a channel parameter. To see channel settings available:'--ch-set all all --ch-index 0'. "
            "Can set the 'psk' using this command. To disable encryption on primary channel:'--ch-set psk none --ch-index 0'. "
            "To set encryption with a new random key on second channel:'--ch-set psk random --ch-index 1'. "
            "To set encryption back to the default:'--ch-set psk default --ch-index 0'. To set encryption with your "
            "own key: '--ch-set psk 0x1a1a1a1a2b2b2b2b1a1a1a1a2b2b2b2b1a1a1a1a2b2b2b2b1a1a1a1a2b2b2b2b --ch-index 0'. "
            "Base64-encoded keys are also accepted: '--ch-set psk HR8D2KziD3IfvpHlwHAfCAh4JP/I7dsHwKdVllfKoD0= --ch-index 1'."
        ),
        nargs=2,
        action="append",
        metavar=("FIELD", "VALUE"),
    )

    group.add_argument(
        "--channel-fetch-attempts",
        help=(
            "Attempt to retrieve channel settings for --ch-set this many times before giving up. Default %(default)s."
        ),
        default=3,
        type=int,
        metavar="ATTEMPTS",
    )

    group.add_argument(
        "--qr",
        help=(
            "Display a QR code for the node's primary channel (or all channels with --qr-all). "
            "Also shows the shareable channel URL."
        ),
        action="store_true",
    )

    group.add_argument(
        "--qr-all",
        help="Display a QR code and URL for all of the node's channels.",
        action="store_true",
    )

    group.add_argument(
        "--contact-qr",
        help="Display a QR code for a node's contact data. "
        "Use the node ID with a '!' or '0x' prefix or the node number. "
        "Also shows the shareable contact URL.",
        metavar="!xxxxxxxx",
    )
    group.add_argument(
        "--contact-verified",
        help="Set the IS_KEY_MANUALLY_VERIFIED bit in the generated contact URL",
        action="store_true",
    )
    group.add_argument(
        "--contact-ignore",
        help="Mark this contact as blocked/ignored in the generated contact URL",
        action="store_true",
    )

    group.add_argument(
        "--ch-enable",
        help="Enable the specified channel. Use --ch-add instead whenever possible.",
        action="store_true",
        dest="ch_enable",
        default=False,
    )

    # Note: We are doing a double negative here (Do we want to disable? If ch_disable==True, then disable.)
    group.add_argument(
        "--ch-disable",
        help="Disable the specified channel Use --ch-del instead whenever possible.",
        action="store_true",
        dest="ch_disable",
        default=False,
    )

    return parser


def addPositionConfigArgs(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Add command-line arguments for configuring fixed position and which position fields to send.

    Adds flags to set latitude, longitude, and altitude (enabling a fixed position), to
    remove the fixed position, and to specify which position fields are included when
    sending position updates.

    Parameters
    ----------
    parser : argparse.ArgumentParser
        The argument parser to extend.

    Returns
    -------
    argparse.ArgumentParser
        The same parser instance with position-related arguments added.
    """

    group = parser.add_argument_group(
        "Position Configuration",
        "Arguments that modify fixed position and other position-related configuration.",
    )
    group.add_argument(
        "--setalt",
        help="Set device altitude in meters (allows use without GPS), and enable fixed position. "
        "When providing positions with `--setlat`, `--setlon`, and `--setalt`, missing values will be set to 0.",
    )

    group.add_argument(
        "--setlat",
        help="Set device latitude (allows use without GPS), and enable fixed position. Accepts a decimal value or an integer premultiplied by 1e7. "
        "When providing positions with `--setlat`, `--setlon`, and `--setalt`, missing values will be set to 0.",
    )

    group.add_argument(
        "--setlon",
        help="Set device longitude (allows use without GPS), and enable fixed position. Accepts a decimal value or an integer premultiplied by 1e7. "
        "When providing positions with `--setlat`, `--setlon`, and `--setalt`, missing values will be set to 0.",
    )

    group.add_argument(
        "--remove-position",
        help="Clear any existing fixed position and disable fixed position.",
        action="store_true",
    )

    group.add_argument(
        "--pos-fields",
        help="Specify fields to send when sending a position. Use no argument for a list of valid values. "
        "Can pass multiple values as a space separated list like "
        "this: '--pos-fields ALTITUDE HEADING SPEED'",
        nargs="*",
        action="store",
    )
    return parser


def addLocalActionArgs(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Register CLI arguments for local-only actions that query or display information from the local node.

    Adds --info (display radio configuration), --nodes (print a formatted node list), and
    --show-fields (comma-separated fields to display with --nodes).

    Parameters
    ----------
    parser : argparse.ArgumentParser
        The argument parser to add connection arguments to.

    Returns
    -------
    argparse.ArgumentParser
        The same parser instance with local-action arguments added.
    """
    group = parser.add_argument_group(
        "Local Actions",
        "Arguments that take actions or request information from the local node only.",
    )

    group.add_argument(
        "--info",
        help="Read and display the radio config information",
        action="store_true",
    )

    lockdown_actions = group.add_mutually_exclusive_group()
    lockdown_actions.add_argument(
        "--lockdown-provision",
        action="store_true",
        help="Provision or unlock a hardened device over USB serial",
    )
    lockdown_actions.add_argument(
        "--lockdown-unlock",
        action="store_true",
        help="Authenticate this USB connection to a provisioned hardened device",
    )
    lockdown_actions.add_argument(
        "--lockdown-lock-now",
        action="store_true",
        help="Revoke current lockdown sessions and reboot into the locked state",
    )
    lockdown_actions.add_argument(
        "--lockdown-disable",
        action="store_true",
        help="Disable lockdown, revert encrypted storage to plaintext, and reboot",
    )
    group.add_argument(
        "--lockdown-passphrase-file",
        help="Read the passphrase from an operator-only (0600) file",
    )
    group.add_argument(
        "--lockdown-passphrase",
        help="INSECURE: passphrase on argv; requires explicit acknowledgement",
    )
    group.add_argument(
        "--insecure-lockdown-passphrase-on-command-line",
        action="store_true",
        help="Acknowledge that an argv passphrase is exposed in shell history and ps",
    )
    group.add_argument(
        "--lockdown-boots", type=int, default=0, help="Authorized boot-count limit"
    )
    group.add_argument(
        "--lockdown-valid-until",
        type=int,
        default=0,
        help="Authorization expiration as a Unix epoch (0 disables)",
    )
    group.add_argument(
        "--lockdown-max-session-seconds",
        type=int,
        default=0,
        help="Maximum unlocked session duration in seconds (0 uses firmware policy)",
    )
    group.add_argument(
        "--lockdown-wait",
        type=float,
        default=20.0,
        help="Seconds to wait for structured LockdownStatus",
    )
    group.add_argument(
        "--lockdown-yes",
        action="store_true",
        help="Skip typed confirmation for destructive lockdown actions",
    )

    group.add_argument(
        "--key-verify",
        choices=_KEY_VERIFICATION_STAGES,
        help="Run one stage of the firmware 2.8 PKI key-verification handshake",
    )
    group.add_argument(
        "--key-verify-nonce",
        type=_parse_uint64,
        default=0,
        help="Handshake nonce from the device notification (stages after initiate)",
    )
    group.add_argument(
        "--key-verify-security-number",
        type=_parse_key_verification_security_number,
        help="Six-digit security number shown on the remote node (provide stage)",
    )
    group.add_argument(
        "--key-verify-wait",
        type=_parse_positive_finite_float,
        default=_DEFAULT_KEY_VERIFY_WAIT,
        help="Seconds to wait for the device key-verification notification",
    )

    group.add_argument(
        "--show-region-presets",
        help="Show firmware-declared legal modem presets for each LoRa region",
        action="store_true",
    )

    group.add_argument(
        "--nodes",
        help="Print Node List in a pretty formatted table",
        action="store_true",
    )

    group.add_argument(
        "--show-fields",
        help="Specify fields to show (comma-separated) when using --nodes",
        type=lambda s: s.split(","),
        default=None,
    )

    return parser


def addRemoteActionArgs(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Register remote-action CLI flags on the provided ArgumentParser.

    Parameters
    ----------
    parser : argparse.ArgumentParser
        The parser to extend with remote action arguments (e.g.,
        sendtext, traceroute, request-telemetry, request-position, reply).

    Returns
    -------
    argparse.ArgumentParser
        The same parser instance with remote-action arguments added.
    """
    group = parser.add_argument_group(
        "Remote Actions",
        "Arguments that take actions or request information from either the local node or remote nodes via the mesh.",
    )

    group.add_argument(
        "--sendtext",
        help="Send a text message. Can specify a destination '--dest', use of PRIVATE_APP port '--private', and/or channel index '--ch-index'.",
        metavar="TEXT",
    )

    group.add_argument(
        "--private",
        help="Optional argument for sending text messages to the PRIVATE_APP port. Use in combination with --sendtext.",
        action="store_true",
    )

    group.add_argument(
        "--traceroute",
        help="Traceroute from connected node to a destination. "
        "You need pass the destination ID as argument, like "
        "this: '--traceroute !ba4bf9d0' | '--traceroute 0xba4bf9d0'"
        "Only nodes with a shared channel can be traced.",
        metavar="!xxxxxxxx",
    )

    group.add_argument(
        "--request-telemetry",
        help="Request telemetry from a node. With an argument, requests that specific type of telemetry.  "
        "You need to pass the destination ID as argument with '--dest'. "
        "For repeaters, the nodeNum is required.",
        action="store",
        nargs="?",
        default=None,
        const="device",
        metavar="TYPE",
    )

    group.add_argument(
        "--request-position",
        help="Request the position from a node. "
        "You need to pass the destination ID as an argument with '--dest'. "
        "For repeaters, the nodeNum is required.",
        action="store_true",
    )

    group.add_argument(
        "--reply",
        help="Reply to received messages on the channel they were received. "
        "If '--ch-index' is set, only messages on that channel are replied to.",
        action="store_true",
    )

    return parser


def addRemoteAdminArgs(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Add command-line options for remote administrative actions that require admin privileges.

    Adds a mutually exclusive group of flags for operations such as reboot, reboot-OTA, enter DFU, shutdown,
    device metadata query, factory reset variants, node DB edits (remove/favorite/ignore), reset NodeDB,
    and setting the node's time.

    Parameters
    ----------
    parser : argparse.ArgumentParser
        The argument parser to extend.

    Returns
    -------
    argparse.ArgumentParser
        The same parser instance with the remote-admin arguments added.
    """

    outer = parser.add_argument_group(
        "Remote Admin Actions",
        "Arguments that interact with local node or remote nodes via the mesh, requiring admin access.",
    )

    group = outer.add_mutually_exclusive_group()

    group.add_argument(
        "--reboot", help="Tell the destination node to reboot", action="store_true"
    )

    group.add_argument(
        "--reboot-ota",
        help="Tell the destination node to reboot into factory firmware (ESP32, firmware version <2.7.18)",
        action="store_true",
    )

    group.add_argument(
        "--ota-update",
        help="Perform an OTA update on the local node (ESP32, firmware version >=2.7.18, WiFi/TCP only for now). "
        "Specify the path to the firmware file.",
        metavar="FIRMWARE_FILE",
        action="store",
    )

    group.add_argument(
        "--enter-dfu",
        help="Tell the destination node to enter DFU mode (NRF52)",
        action="store_true",
    )

    group.add_argument(
        "--shutdown", help="Tell the destination node to shutdown", action="store_true"
    )

    group.add_argument(
        "--device-metadata",
        help="Get the device metadata from the node",
        action="store_true",
    )

    group.add_argument(
        "--factory-reset",
        "--factory-reset-config",
        help="Tell the destination node to install the default config, preserving BLE bonds & PKI keys",
        action="store_true",
    )

    group.add_argument(
        "--factory-reset-device",
        help="Tell the destination node to install the default config and clear BLE bonds & PKI keys",
        action="store_true",
    )

    group.add_argument(
        "--remove-node",
        help="Tell the destination node to remove a specific node from its NodeDB. "
        "Use the node ID with a '!' or '0x' prefix or the node number.",
        metavar="!xxxxxxxx",
    )
    group.add_argument(
        "--set-favorite-node",
        help="Tell the destination node to set the specified node to be favorited on the NodeDB. "
        "Use the node ID with a '!' or '0x' prefix or the node number.",
        metavar="!xxxxxxxx",
    )
    group.add_argument(
        "--remove-favorite-node",
        help="Tell the destination node to set the specified node to be un-favorited on the NodeDB. "
        "Use the node ID with a '!' or '0x' prefix or the node number.",
        metavar="!xxxxxxxx",
    )
    group.add_argument(
        "--set-ignored-node",
        help="Tell the destination node to set the specified node to be ignored on the NodeDB. "
        "Use the node ID with a '!' or '0x' prefix or the node number.",
        metavar="!xxxxxxxx",
    )
    group.add_argument(
        "--remove-ignored-node",
        help="Tell the destination node to set the specified node to be un-ignored on the NodeDB. "
        "Use the node ID with a '!' or '0x' prefix or the node number.",
        metavar="!xxxxxxxx",
    )
    group.add_argument(
        "--reset-nodedb",
        help="Tell the destination node to clear its list of nodes",
        action="store_true",
    )

    group.add_argument(
        "--add-contact",
        help="Add a contact (User) to the NodeDB from a shareable contact URL. "
        "Quote the URL in your shell because it contains '#'. "
        "Example: --add-contact 'https://meshtastic.org/v/#<base64>'",
        metavar="URL",
    )

    group.add_argument(
        "--set-time",
        help="Set the time to the provided unix epoch timestamp, or the system's current time if omitted or 0.",
        action="store",
        type=int,
        nargs="?",
        default=None,
        const=0,
        metavar="TIMESTAMP",
    )

    backup_group = outer.add_mutually_exclusive_group()
    backup_group.add_argument(
        "--backup-preferences",
        nargs="?",
        const="flash",
        default=None,
        choices=["flash", "sd"],
        metavar="LOCATION",
        help="Back up preferences (default: flash); current firmware does not implement SD backups",
    )
    backup_group.add_argument(
        "--restore-preferences",
        nargs="?",
        const="flash",
        default=None,
        choices=["flash", "sd"],
        metavar="LOCATION",
        help="Restore preferences (default: flash); SD support depends on firmware",
    )
    backup_group.add_argument(
        "--remove-backup-preferences",
        nargs="?",
        const="flash",
        default=None,
        choices=["flash", "sd"],
        metavar="LOCATION",
        help="Remove a preferences backup (default: flash); current firmware does not implement SD removal",
    )
    outer.add_argument(
        "--toggle-muted-node",
        metavar="!xxxxxxxx",
        help="Toggle the muted flag for a node in the device's NodeDB",
    )
    outer.add_argument(
        "--delete-file",
        type=_parse_absolute_device_path,
        metavar="PATH",
        help="Delete a file from the node's filesystem (absolute path)",
    )
    outer.add_argument(
        "--send-input-event",
        type=_parse_input_event_uint8,
        metavar="EVENT_CODE",
        help=f"Send a physical input event code (0-{_MAX_INPUT_EVENT_CODE}) to the node",
    )
    outer.add_argument(
        "--input-kb-char",
        type=_parse_firmware_keyboard_char,
        help="Single keyboard character paired with --send-input-event",
    )
    outer.add_argument(
        "--input-touch-x",
        type=_parse_input_touch_x,
        help=f"Touch X coordinate (0-{_MAX_INPUT_TOUCH_X}) paired with --send-input-event",
    )
    outer.add_argument(
        "--input-touch-y",
        type=_parse_input_touch_y,
        help=f"Touch Y coordinate (0-{_MAX_INPUT_TOUCH_Y}) paired with --send-input-event",
    )
    outer.add_argument(
        "--request-connection-status",
        action="store_true",
        help="Report the node's WiFi, Ethernet, Bluetooth, and serial status",
    )
    outer.add_argument(
        "--get-ui-config",
        action="store_true",
        help="Print the node's device UI configuration as YAML",
    )
    outer.add_argument(
        "--store-ui-config",
        metavar="FILE",
        help="Store a device UI configuration from YAML (as printed by --get-ui-config)",
    )

    return parser


def parse_cli_args(
    parser: argparse.ArgumentParser,
    *,
    version: str,
    argcomplete_module: _ArgcompleteModule | None = None,
    argv: Sequence[str] | None = None,
) -> argparse.Namespace:
    """Register all Meshtastic argument groups and parse the command line.

    Registers help, connection, selection, import/export, configuration,
    position, channel, local action, remote action, remote admin, and
    miscellaneous argument groups on ``parser``, optionally enables shell
    autocompletion via ``argcomplete_module``, and parses arguments.

    Parameters
    ----------
    parser : argparse.ArgumentParser
        The parser to configure with Meshtastic-specific argument groups.
    version : str
        Version string reported when ``--version`` is requested.
    argcomplete_module : _ArgcompleteModule | None
        Optional module exposing an ``autocomplete(parser)`` callable (the
        ``argcomplete`` package). When provided, autocompletion is enabled.
    argv : Sequence[str] | None
        Argument vector to parse. When ``None`` (the default), ``sys.argv[1:]``
        is used. Pass an explicit sequence to make parsing deterministic in
        tests and embedded callers.

    Returns
    -------
    argparse.Namespace
        The parsed arguments.
    """
    # The "Help" group includes the help option and other informational stuff about the CLI itself
    outerHelpGroup = parser.add_argument_group("Help")
    helpGroup = outerHelpGroup.add_mutually_exclusive_group()
    helpGroup.add_argument(
        "-h", "--help", action="help", help="show this help message and exit"
    )

    helpGroup.add_argument("--version", action="version", version=version)

    helpGroup.add_argument(
        "--support",
        action="store_true",
        help="Show support info (useful when troubleshooting an issue)",
    )

    # Connection arguments to indicate a device to connect to
    parser = addConnectionArgs(parser)

    # Selection arguments to denote nodes and channels to use
    parser = addSelectionArgs(parser)

    # Arguments concerning viewing and setting configuration
    parser = addImportExportArgs(parser)
    parser = addConfigArgs(parser)
    parser = addPositionConfigArgs(parser)
    parser = addChannelConfigArgs(parser)

    # Arguments for sending or requesting things from the local device
    parser = addLocalActionArgs(parser)

    # Arguments for sending or requesting things from the mesh
    parser = addRemoteActionArgs(parser)
    parser = addRemoteAdminArgs(parser)

    # All the rest of the arguments
    group = parser.add_argument_group("Miscellaneous arguments")

    group.add_argument(
        "--seriallog",
        help="Log device serial output to either 'none' or a filename to append to.  Defaults to '%(const)s' if no filename specified.",
        nargs="?",
        const="stdout",
        default=None,
        metavar="LOG_DESTINATION",
    )

    group.add_argument(
        "--ack",
        help="Use in combination with compatible actions (e.g. --sendtext) to wait for an acknowledgment.",
        action="store_true",
    )

    group.add_argument(
        "--timeout",
        help="How long to wait for replies. Default %(default)ss.",
        default=300.0,
        type=float,
        metavar="SECONDS",
    )

    group.add_argument(
        "--no-nodes",
        help="Request that the node not send node info to the client. "
        "Will break things that depend on the nodedb, but will speed up startup. Requires 2.3.11+ firmware.",
        action="store_true",
    )

    group.add_argument(
        "--debug",
        help="Show detailed debug log messages (connection diagnostics, config streaming, retries)",
        action="store_true",
    )

    group.add_argument(
        "--debuglib",
        help="Show debug log messages for the meshtastic library only (not dependencies)",
        action="store_true",
    )

    group.add_argument(
        "--quiet",
        help="Suppress non-essential output; show only warnings and errors",
        action="store_true",
    )

    group.add_argument(
        "--test",
        help="Run stress test against all connected Meshtastic devices",
        action="store_true",
    )

    group.add_argument(
        "--wait-to-disconnect",
        help="How many seconds to wait before disconnecting from the device.",
        const="5",
        nargs="?",
        action="store",
        metavar="SECONDS",
    )

    group.add_argument(
        "--noproto",
        help="Don't start the API, just function as a dumb serial terminal.",
        action="store_true",
    )

    group.add_argument(
        "--listen",
        help="Just stay open and listen to the protobuf stream. Enables debug logging.",
        action="store_true",
    )

    group.add_argument(
        "--no-time",
        help="Deprecated. Retained for backwards compatibility in scripts, but is a no-op.",
        action="store_true",
    )

    power_group = parser.add_argument_group(
        "Power Testing", "Options for power testing/logging."
    )

    power_supply_group = power_group.add_mutually_exclusive_group()

    power_supply_group.add_argument(
        "--power-riden",
        help="Talk to a Riden power-supply. You must specify the device path, i.e. /dev/ttyUSBxxx",
    )

    power_supply_group.add_argument(
        "--power-ppk2-meter",
        help="Talk to a Nordic Power Profiler Kit 2 (in meter mode)",
        action="store_true",
    )

    power_supply_group.add_argument(
        "--power-ppk2-supply",
        help="Talk to a Nordic Power Profiler Kit 2 (in supply mode)",
        action="store_true",
    )

    power_supply_group.add_argument(
        "--power-sim",
        help="Use a simulated power meter (for development)",
        action="store_true",
    )

    power_group.add_argument(
        "--power-voltage",
        help="Set the specified voltage on the power-supply. Be VERY careful, you can burn things up.",
    )

    power_group.add_argument(
        "--power-stress",
        help="Exercise device power states for power-consumption profiling",
        action="store_true",
    )

    power_group.add_argument(
        "--power-wait",
        help="Prompt the user to wait for device reset before looking for device serial ports (some boards kill power to USB serial port)",
        action="store_true",
    )

    power_group.add_argument(
        "--slog",
        help="Store structured-logs (slogs) for this run, optionally you can specify a destination directory",
        nargs="?",
        default=None,
        const="default",
    )

    remoteHardwareArgs = parser.add_argument_group(
        "Remote Hardware", "Arguments related to the Remote Hardware module"
    )

    remoteHardwareArgs.add_argument(
        "--gpio-wrb", nargs=2, help="Set a particular GPIO # to 1 or 0", action="append"
    )

    remoteHardwareArgs.add_argument(
        "--gpio-rd", help="Read from a GPIO mask (ex: '0x10')"
    )

    remoteHardwareArgs.add_argument(
        "--gpio-watch", help="Start watching a GPIO mask for changes (ex: '0x10')"
    )

    have_tunnel = platform.system() == "Linux"
    if have_tunnel:
        tunnelArgs = parser.add_argument_group(
            "Tunnel", "Arguments related to establishing a tunnel device over the mesh."
        )
        tunnelArgs.add_argument(
            "--tunnel",
            action="store_true",
            help="Create a TUN tunnel device for forwarding IP packets over the mesh",
        )
        tunnelArgs.add_argument(
            "--subnet",
            dest="tunnel_net",
            help="Sets the local-end subnet address for the TUN IP bridge. (ex: 10.115' which is the default)",
            default=None,
        )

    parser.set_defaults(deprecated=None)

    if argcomplete_module is not None:
        autocomplete = argcomplete_module.autocomplete
        autocomplete(parser)
    return parser.parse_args(argv if argv is not None else sys.argv[1:])
