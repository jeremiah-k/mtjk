"""Helpers for invoking CLI commands in tests with bounded execution time."""

import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import NoReturn

import pytest

from meshtastic._branding import PRIMARY_CLI_NAME

EMPTY_SHELL_COMMAND_ERROR = "Empty command passed to CLI shell helper"
EMPTY_ARGV_COMMAND_ERROR = "Empty command list passed to CLI argv helper"
HOST_CLI_REQUEST_TIMEOUT_SECONDS = 5
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")


def _fail_masked_timeout(command_name: str, timeout_s: int | float) -> NoReturn:
    """Fail a test with a redacted timeout message for a CLI command."""
    masked = f"{command_name!r} +REDACTED_ARGS"
    pytest.fail(f"CLI command timed out after {timeout_s}s: {masked}")


def _shell_executable_for_timeout(command: str) -> str:
    """Return a safe executable token for timeout messages without leaking env assignments."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = ["<COMMAND_PARSING_FAILED>"]
    for token in tokens:
        if not _ENV_ASSIGNMENT_RE.fullmatch(token):
            return token
    return "<shell-command>"


def _quote_shell_path(path: str | Path) -> str:
    """Quote a filesystem path for shell command usage in runCliWithTimeout.

    Parameters
    ----------
    path : str | Path
        Filesystem path to quote.

    Returns
    -------
    str
        Shell-escaped path suitable for the current platform.
    """
    path_str = str(path)
    if os.name == "nt":
        escaped = path_str.replace('"', '""')
        return f'"{escaped}"'
    return shlex.quote(path_str)


def runCliWithTimeout(command: str, timeout: int | float = 120) -> tuple[int, str]:
    """Run a shell CLI command and return (exit_code, combined_output).

    Parameters
    ----------
    command : str
        Full shell command to execute. This helper intentionally accepts
        a command string (rather than argv tokens) for compatibility with
        existing test call sites. Prefer `runCliWithTimeout`; the
        `run_cli_with_timeout` name remains as a compatibility alias. Use
        `_quote_shell_path` for path quoting when constructing commands that may
        include spaces.
    timeout : int | float
        Maximum time to allow command execution before failing the test.
        (Default value = 120)

    Returns
    -------
    tuple[int, str]
        Exit code and combined stdout/stderr output.

    Raises
    ------
    ValueError
        If command is empty or whitespace-only.
    """
    if not command.strip():
        raise ValueError(EMPTY_SHELL_COMMAND_ERROR)
    try:
        # Intentional shell=True for legacy string-based test commands.
        # For argv + shell=False patterns, use run_cli_argv_with_timeout.
        result = subprocess.run(  # noqa: S602
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        executable = _shell_executable_for_timeout(command)
        _fail_masked_timeout(executable, timeout)
    return result.returncode, result.stdout


def runCliArgvWithTimeout(
    cmd: list[str], timeout: int | float = 30
) -> subprocess.CompletedProcess[str]:
    """Run a CLI command using argv list and return CompletedProcess.

    Parameters
    ----------
    cmd : list[str]
        Command and arguments as a list (e.g., ["meshtastic", "--info"]).
    timeout : int | float
        Maximum time to allow command execution before failing the test.
        (Default value = 30)

    Returns
    -------
    subprocess.CompletedProcess[str]
        CompletedProcess with returncode, stdout, and stderr attributes.

    Raises
    ------
    ValueError
        If cmd is empty.
    """
    if not cmd:
        raise ValueError(EMPTY_ARGV_COMMAND_ERROR)
    try:
        return subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        cmd_name = cmd[0]
        timeout_value = e.timeout if e.timeout is not None else timeout
        _fail_masked_timeout(cmd_name, timeout_value)


# COMPAT_STABLE_SHIM: historical snake_case helper names used by tests.
def run_cli_with_timeout(command: str, timeout: int | float = 120) -> tuple[int, str]:
    """Compatibility alias for runCliWithTimeout()."""
    return runCliWithTimeout(command, timeout=timeout)


# COMPAT_STABLE_SHIM: historical snake_case helper names used by tests.
def run_cli_argv_with_timeout(
    cmd: list[str], timeout: int | float = 30
) -> subprocess.CompletedProcess[str]:
    """Compatibility alias for runCliArgvWithTimeout()."""
    return runCliArgvWithTimeout(cmd, timeout=timeout)


def _host_cli_argv(
    host: str,
    args: tuple[str, ...],
    *,
    timeout: int | float,
    meshtastic_bin: str,
) -> list[str]:
    """Build the exact host CLI argv, including the bounded request timeout."""
    has_request_timeout = any(
        arg == "--timeout" or arg.startswith("--timeout=") for arg in args
    )
    request_timeout_args = (
        []
        if has_request_timeout
        else ["--timeout", str(min(HOST_CLI_REQUEST_TIMEOUT_SECONDS, timeout))]
    )
    return [meshtastic_bin, "--host", host, *request_timeout_args, *args]


def _run_host_cli(
    host: str,
    *args: str,
    timeout: int | float = 30,
    meshtastic_bin: str = PRIMARY_CLI_NAME,
) -> tuple[int, str]:
    """Run the configured CLI command against a host and return (code, output).

    Parameters
    ----------
    host : str
        Target host address for the --host argument.
    *args : str
        Additional CLI arguments to pass after --host.
    timeout : int | float
        Maximum time to allow command execution before failing the test.
        (Default value = 30)
    meshtastic_bin : str
        Path or name of the selected CLI executable.
        (Default value = PRIMARY_CLI_NAME)

    Returns
    -------
    tuple[int, str]
        Exit code and combined stdout/stderr output.
    """
    result = run_cli_argv_with_timeout(
        _host_cli_argv(host, args, timeout=timeout, meshtastic_bin=meshtastic_bin),
        timeout=timeout,
    )
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def _run_host_cli_ok(
    host: str,
    *args: str,
    timeout: int | float = 30,
    meshtastic_bin: str = PRIMARY_CLI_NAME,
) -> str:
    """Run the configured host CLI command and assert success with useful context.

    Parameters
    ----------
    host : str
        Target host address for the --host argument.
    *args : str
        Additional CLI arguments to pass after --host.
    timeout : int | float
        Maximum time to allow command execution before failing the test.
        (Default value = 30)
    meshtastic_bin : str
        Path or name of the selected CLI executable.
        (Default value = PRIMARY_CLI_NAME)

    Returns
    -------
    str
        Combined stdout/stderr output from the command.

    Raises
    ------
    AssertionError
        If the command returns a non-zero exit code.
    """
    returncode, output = _run_host_cli(
        host,
        *args,
        timeout=timeout,
        meshtastic_bin=meshtastic_bin,
    )
    rendered_command = " ".join(
        shlex.quote(arg)
        for arg in _host_cli_argv(
            host, args, timeout=timeout, meshtastic_bin=meshtastic_bin
        )
    )
    assert returncode == 0, (
        f"Command failed (rc={returncode}) on {host}\n"
        f"Command: {rendered_command}\n"
        f"Output:\n{output}"
    )
    return output
