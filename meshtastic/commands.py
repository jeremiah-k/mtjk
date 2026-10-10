"""Execute finite CLI actions on an application-owned mesh connection."""

from __future__ import annotations

import argparse
import math
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import NoReturn

import meshtastic.util
from meshtastic._command_scope import _command_scope
from meshtastic._core_constants import BROADCAST_ADDR
from meshtastic._deadline import (
    _operation_deadline,
    _remaining_timeout,
    _validate_timeout,
)
from meshtastic.cli.context import CliContext
from meshtastic.cli.dispatch import _dispatch_connected
from meshtastic.cli.invocation import CliInvocation, activate_invocation
from meshtastic.cli.parser import parse_cli_args
from meshtastic.configuration import _preference_path
from meshtastic.mesh_interface import MeshInterface
from meshtastic.node import Node
from meshtastic.version import get_active_version

__all__ = (
    "CommandCapabilities",
    "CommandResult",
    "executeCommand",
    "getCommandCapabilities",
)

DEFAULT_COMMAND_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024
_ACTIONS = frozenset(
    {
        "get",
        "set",
        "begin_edit",
        "commit_edit",
        "get_canned_message",
        "set_canned_message",
        "get_ringtone",
        "set_ringtone",
        "ch_vlongslow",
        "ch_longslow",
        "ch_longmod",
        "ch_longfast",
        "ch_longturbo",
        "ch_medslow",
        "ch_medfast",
        "ch_shortslow",
        "ch_shortfast",
        "ch_shortturbo",
        "ch_preset",
        "set_owner",
        "set_owner_short",
        "set_ham",
        "set_is_unmessageable",
        "setalt",
        "setlat",
        "setlon",
        "remove_position",
        "pos_fields",
        "ch_add",
        "ch_del",
        "ch_set",
        "ch_enable",
        "ch_disable",
        "qr",
        "qr_all",
        "contact_qr",
        "info",
        "show_region_presets",
        "nodes",
        "sendtext",
        "traceroute",
        "request_telemetry",
        "request_position",
        "reboot",
        "reboot_ota",
        "enter_dfu",
        "shutdown",
        "remove_node",
        "set_favorite_node",
        "remove_favorite_node",
        "set_ignored_node",
        "remove_ignored_node",
        "reset_nodedb",
        "set_time",
        "backup_preferences",
        "restore_preferences",
        "remove_backup_preferences",
        "toggle_muted_node",
        "delete_file",
        "send_input_event",
        "request_connection_status",
        "get_ui_config",
    }
)
_OPTIONS = frozenset(
    {
        "dest",
        "ch_index",
        "channel_fetch_attempts",
        "contact_verified",
        "contact_ignore",
        "show_fields",
        "role",
        "hwmodel",
        "sort",
        "limit",
        "private",
        "reply",
        "ack",
        "quiet",
        "input_kb_char",
        "input_touch_x",
        "input_touch_y",
        "help",
        "version",
    }
)


@dataclass(frozen=True, slots=True)
class CommandCapabilities:
    """Discover the maintained embedded-command surface.

    Attributes
    ----------
    apiVersion : int
        Contract version, starting at 1. Additive options do not change it.
    supportedOptions : tuple[str, ...]
        Accepted option spellings, including aliases. Long-running services,
        transport selection, file-based configuration, and interactive flows
        belong to the standalone CLI and are excluded.
    """

    apiVersion: int
    supportedOptions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Captured outcome of one embedded invocation.

    Attributes
    ----------
    exitCode : int
        Zero on success, 2 for argument errors, 1 for execution failures.
    output : str
        Captured text, including requested results under --quiet.
    error : Exception | None
        Original failure, including typed request exceptions, when available.
    truncated : bool
        Whether the UTF-8 output byte budget omitted text.
    """

    exitCode: int
    output: str
    error: Exception | None = None
    truncated: bool = False

    @property
    def succeeded(self) -> bool:
        """Return whether execution completed with status zero."""
        return self.exitCode == 0


class _CommandExit(Exception):
    """Convert argparse and CLI termination into a returned status."""

    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(message)
        self.status = status


class _OutputCapture:
    """Bound retained and streamed output without redirecting process streams."""

    def __init__(self, limit: int, output: Callable[[str], None] | None) -> None:
        self._remaining = limit
        self._output = output
        self._parts: list[str] = []
        self.truncated = False
        self._lock = threading.RLock()
        self._error: Exception | None = None

    def _write(self, message: str) -> None:
        with self._lock:
            encoded = (message + "\n").encode("utf-8")
            retained = encoded[: self._remaining].decode("utf-8", errors="ignore")
            self.truncated |= len(encoded) > self._remaining
            self._remaining = max(0, self._remaining - len(encoded))
            if retained:
                self._parts.append(retained)
            if retained and self._output is not None:
                try:
                    self._output(retained)
                except Exception as exc:
                    self._error = exc
                    self._output = None
                    raise

    def _result(self, status: int, error: Exception | None = None) -> CommandResult:
        with self._lock:
            error = self._error or error
            return CommandResult(
                1 if error is not None and status == 0 else status,
                "".join(self._parts),
                error,
                self.truncated,
            )


class _CommandParser(argparse.ArgumentParser):
    """Keep parsing, help and version output inside the invocation."""

    def __init__(self, output: Callable[[str], None]) -> None:
        super().__init__(prog="executeCommand", add_help=False, allow_abbrev=False)
        self._output = output

    def error(self, message: str) -> NoReturn:
        raise _CommandExit(2, message)

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        raise _CommandExit(status, message or "")

    def _print_message(self, message: str, file: object = None) -> None:
        if message:
            self._output(message.rstrip("\n"))


def _parser(output: Callable[[str], None]) -> tuple[_CommandParser, argparse.Namespace]:
    """Use the CLI's argument definitions while refusing unsupported options."""
    parser = _CommandParser(output)
    defaults = parse_cli_args(parser, version=get_active_version(), argv=[])
    # argparse has no public action-removal API. Remove actions from both parse
    # and help indexes so capability discovery, help and validation agree.
    for action in tuple(parser._actions):
        if action.dest in _ACTIONS | _OPTIONS:
            continue
        parser._remove_action(action)
        for option in action.option_strings:
            parser._option_string_actions.pop(option, None)
        for group in parser._action_groups:
            if action in group._group_actions:
                group._group_actions.remove(action)
    return parser, defaults


def getCommandCapabilities() -> CommandCapabilities:
    """Return accepted option spellings without opening a transport."""
    parser, _ = _parser(lambda _message: None)
    return CommandCapabilities(1, tuple(sorted(parser._option_string_actions)))


def _fresh_get_pref(
    node: Node,
    comp_name: str,
    *,
    allow_secrets: bool = False,
    cli_print: Callable[[str], None] = print,
) -> bool:
    """Reuse CLI formatting and redaction over a fresh, detached section."""
    # Load the standalone action bindings only when an invocation needs them.
    import meshtastic.__main__ as entrypoint  # pylint: disable=import-outside-toplevel

    comp_name = entrypoint._normalize_pref_name(comp_name)
    try:
        module, fields = _preference_path(comp_name)
    except ValueError as exc:
        raise _CommandExit(2, str(exc)) from exc
    if len(fields) > 2:
        value = node.readPreference(comp_name, timeout=_remaining_timeout(math.inf))
        rendered = meshtastic.util.toStr(value)
        if not allow_secrets:
            rendered = entrypoint._redact_pref_value(
                ".".join(field.name for field in fields), rendered
            )
        cli_print(f"{comp_name}: {rendered}")
        return True
    copied = Node(node.iface, node.nodeNum, noProto=node.noProto)
    section = fields[0].name
    if module:
        response = node.readModuleConfig(section, timeout=_remaining_timeout(math.inf))
        getattr(copied.moduleConfig, section).CopyFrom(getattr(response, section))
    else:
        response_config = node.readConfig(section, timeout=_remaining_timeout(math.inf))
        getattr(copied.localConfig, section).CopyFrom(getattr(response_config, section))
    return entrypoint.getPref(
        copied, comp_name, allow_secrets=allow_secrets, cli_print=cli_print
    )


def _execute(
    interface: MeshInterface,
    args: argparse.Namespace,
    invocation: CliInvocation,
    capture: _OutputCapture,
) -> None:
    """Bind the standalone action implementation to embedded ownership rules."""
    # Load the standalone action bindings only when an invocation needs them.
    import meshtastic.__main__ as entrypoint  # pylint: disable=import-outside-toplevel

    def report(message: str) -> None:
        if not args.quiet:
            capture._write(message)

    def terminate(message: str, return_value: int = 1) -> NoReturn:
        raise _CommandExit(return_value, message)

    def select_channel(value: int) -> None:
        invocation.channel_index = value

    invocation.exit_handler = terminate
    hooks = entrypoint._build_connected_dispatch_hooks()
    hooks = replace(
        hooks,
        cli_print=capture._write,
        device=replace(
            hooks.device,
            cli_print=(
                capture._write
                if args.get_ui_config or args.request_connection_status
                else report
            ),
            cli_exit=terminate,
        ),
        channel_contact=replace(
            hooks.channel_contact,
            cli_print=report,
            cli_exit=terminate,
            set_channel_index=select_channel,
        ),
        configure=replace(hooks.configure, cli_print=report, cli_exit=terminate),
        services=replace(
            hooks.services,
            cli_print=report,
            preference_print=capture._write,
            cli_exit=terminate,
            newer_version=lambda: None,
            get_pref=_fresh_get_pref,
        ),
    )
    context = CliContext(
        interface,
        args,
        {
            "requestChannelAttempts": args.channel_fetch_attempts,
            "timeout": _remaining_timeout(math.inf),
        },
        owns_interface=False,
        invocation=invocation,
    )
    _dispatch_connected(context, hooks)


def executeCommand(
    interface: MeshInterface,
    argv: Sequence[str],
    *,
    timeout: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    maxOutputBytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    output: Callable[[str], None] | None = None,
) -> CommandResult:
    """Execute supported CLI arguments on an existing connection.

    Parameters
    ----------
    interface : MeshInterface
        Caller-owned connection. Execution never opens or closes a transport.
    argv : Sequence[str]
        CLI tokens, without a program name. No shell parsing is performed.
    timeout : float
        Finite positive budget, including waiting for another command on this
        interface. Library-managed waits share the budget; blocking transport
        I/O retains the backend's own timeout.
    maxOutputBytes : int
        Positive UTF-8 byte limit shared by returned and streamed output.
    output : Callable[[str], None] | None
        Optional synchronous sink receiving retained text chunks, with newlines.

    Returns
    -------
    CommandResult
        Status, captured text, original failure and truncation state. Argument
        and execution errors are returned. KeyboardInterrupt and other control
        flow exceptions propagate after request cleanup.

    Raises
    ------
    ValueError
        If timeout or maxOutputBytes is invalid.

    Notes
    -----
    Calls on one interface serialize; separate interfaces can run concurrently.
    Only command-owned response registrations are retired on exit. Device
    changes are not rolled back on timeout. Supported options are discoverable
    through getCommandCapabilities and --help. Authorization and destination
    policy belong to the embedding application.
    """
    if (
        isinstance(maxOutputBytes, bool)
        or not isinstance(maxOutputBytes, int)
        or maxOutputBytes <= 0
    ):
        raise ValueError("maxOutputBytes must be a positive integer")
    _validate_timeout(timeout)
    capture = _OutputCapture(maxOutputBytes, output)
    try:
        with _operation_deadline(timeout):
            if (
                isinstance(argv, (str, bytes))
                or not isinstance(argv, Sequence)
                or not all(isinstance(token, str) for token in argv)
            ):
                raise _CommandExit(2, "argv must be a sequence of strings")
            parser, defaults = _parser(capture._write)
            args = parser.parse_args(list(argv), namespace=defaults)
            if not any(
                getattr(args, name) != parser.get_default(name) for name in _ACTIONS
            ):
                raise _CommandExit(2, "Specify at least one supported command")
            args.dest = args.dest or BROADCAST_ADDR
            invocation = CliInvocation(args, parser, args.ch_index)
            with (
                _command_scope(interface, capture._write),
                activate_invocation(invocation),
            ):
                _execute(interface, args, invocation, capture)
        return capture._result(0)
    except _CommandExit as exc:
        if str(exc):
            try:
                capture._write(str(exc))
            except Exception as sink_error:
                return capture._result(1, sink_error)
        error: Exception | None = None
        if exc.status:
            error = ValueError(str(exc)) if exc.status == 2 else RuntimeError(str(exc))
        return capture._result(exc.status, error)
    except Exception as exc:
        return capture._result(1, exc)
