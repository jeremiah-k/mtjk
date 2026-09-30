"""Policy tests for the bootstrap ``--dry-run`` invocation gate.

All parsed namespaces are built through the real parser
(:func:`meshtastic.cli.parser.parse_cli_args`) so the gate is exercised against
authentic argparse defaults, including the parameter flags with truthy
defaults (``--lockdown-wait``, ``--key-verify-wait``) that must never be
mistaken for requested actions.
"""

from __future__ import annotations

import argparse
import platform
from unittest.mock import MagicMock

import pytest

from meshtastic._core_constants import BROADCAST_ADDR
from meshtastic.cli import bootstrap
from meshtastic.cli.parser import parse_cli_args
from meshtastic.mesh_interface import MeshInterface


def _parse(argv: list[str]) -> tuple[argparse.ArgumentParser, argparse.Namespace]:
    """Parse ``argv`` with the real CLI parser and return (parser, namespace)."""
    parser = argparse.ArgumentParser(add_help=False)
    args = parse_cli_args(parser, version="t", argv=argv)
    return parser, args


def _make_hooks() -> bootstrap.BootstrapHooks:
    """Build real ``BootstrapHooks`` with sentinel transport factories.

    The serial/TCP/BLE interface factories are mocks whose side effect raises
    if ever called, proving that a ``--dry-run`` invocation is refused before
    any transport construction; tests assert ``assert_not_called()`` on them.
    Every non-transport seam is a :class:`~unittest.mock.MagicMock`.
    """
    _transport_error = AssertionError(
        "transport factory reached; --dry-run must exit before transport setup"
    )

    def _sentinel_factory() -> MagicMock:
        return MagicMock(side_effect=_transport_error)

    return bootstrap.BootstrapHooks(
        cli_exit=MagicMock(),
        support_info=MagicMock(),
        print_available_config_fields=MagicMock(),
        describe_config_field=MagicMock(return_value=True),
        create_power_meter=MagicMock(),
        get_power_meter=MagicMock(return_value=None),
        release_power_meter=MagicMock(),
        set_logfile=MagicMock(),
        clear_session_logfile=MagicMock(),
        subscribe=MagicMock(),
        unsubscribe_receive=MagicMock(),
        on_connected=MagicMock(),
        parse_host_port=MagicMock(return_value=("127.0.0.1", 4403)),
        listen_loop_poll_once=MagicMock(return_value=True),
        set_channel_index=MagicMock(),
        ble_interface=_sentinel_factory(),
        tcp_interface=_sentinel_factory(),
        default_tcp_port=4403,
        serial_interface=_sentinel_factory(),
        mesh_interface_error=MeshInterface.MeshInterfaceError,
        test_module=None,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("flag_argv", "label"),
    [
        # Mutating device actions.
        (["--reboot"], "--reboot"),
        (["--set-owner", "Alice"], "--set-owner"),
        (["--ch-longfast"], "--ch-longfast"),
        (["--ch-preset", "long_fast"], "--ch-preset"),
        (["--seturl", "https://meshtastic.org/e/#CgMSAQE"], "--seturl"),
        (["--begin-edit"], "--begin-edit"),
        # Long-running / blocking loops.
        (["--listen"], "--listen"),
        (["--noproto"], "--noproto"),
        (["--wait-to-disconnect", "3"], "--wait-to-disconnect"),
        (["--ack"], "--ack"),
        # Pre-connect exit paths.
        (["--support"], "--support"),
        (["--list-fields"], "--list-fields"),
        (["--describe-field", "power.ls_secs"], "--describe-field"),
        (["--test"], "--test"),
        # Hardware access.
        (["--power-sim"], "--power-sim"),
        (["--power-voltage", "5"], "--power-voltage"),
        # Read-only device queries.
        (["--get", "power.ls_secs"], "--get"),
        (["--info"], "--info"),
        (["--nodes"], "--nodes"),
        (["--qr"], "--qr"),
        # File-producing output.
        (["--export-config"], "--export-config"),
        (["--slog"], "--slog"),
    ],
)
def test_dry_run_rejects_each_action_category(
    capsys: pytest.CaptureFixture[str], flag_argv: list[str], label: str
) -> None:
    """Each rejected action flag exits 2 naming only the flag(s) provided."""
    if flag_argv[0] == "--tunnel" and platform.system() != "Linux":
        pytest.skip("--tunnel only exists on Linux")
    argv = ["--dry-run", "--set", "a", "1", *flag_argv]
    parser, args = _parse(argv)
    hooks = _make_hooks()
    with pytest.raises(SystemExit) as exc_info:
        bootstrap.run_common(args, parser, hooks, argv=argv)
    assert exc_info.value.code == 2
    stderr = capsys.readouterr().err
    message = stderr.strip().splitlines()[-1]
    assert "--dry-run only supports --set and --configure; remove:" in message
    removal_list = message.split("remove:", 1)[1].strip()
    assert removal_list == label


@pytest.mark.unit
@pytest.mark.skipif(
    platform.system() != "Linux", reason="--tunnel only exists on Linux"
)
def test_dry_run_rejects_tunnel(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The Linux-only ``--tunnel`` flag is refused with ``--dry-run``."""
    argv = ["--dry-run", "--set", "a", "1", "--tunnel"]
    parser, args = _parse(argv)
    hooks = _make_hooks()
    with pytest.raises(SystemExit) as exc_info:
        bootstrap.run_common(args, parser, hooks, argv=argv)
    assert exc_info.value.code == 2
    stderr = capsys.readouterr().err
    removal_list = stderr.strip().splitlines()[-1].split("remove:", 1)[1].strip()
    assert removal_list == "--tunnel"


@pytest.mark.unit
def test_dry_run_without_set_or_configure_is_rejected(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--dry-run`` alone exits 2 requiring ``--set`` or ``--configure``."""
    argv = ["--dry-run"]
    parser, args = _parse(argv)
    hooks = _make_hooks()
    with pytest.raises(SystemExit) as exc_info:
        bootstrap.run_common(args, parser, hooks, argv=argv)
    assert exc_info.value.code == 2
    stderr = capsys.readouterr().err
    assert "--dry-run requires --set or --configure" in stderr


@pytest.mark.unit
@pytest.mark.parametrize(
    "preview_argv",
    [
        ["--set", "power.ls_secs", "300"],
        ["--configure", "doc.yaml"],
        ["--set", "a", "1", "--configure", "doc.yaml"],
    ],
)
def test_dry_run_allows_valid_preview_invocations(preview_argv: list[str]) -> None:
    """Plain preview batches pass the gate and keep their normalized defaults."""
    for extra, expected_dest, expected_seriallog in (
        ([], BROADCAST_ADDR, "none"),
        (
            [
                "--host",
                "127.0.0.1",
                "--dest",
                "!1234abcd",
                "--timeout",
                "60",
                "--no-nodes",
                "--debug",
                "--seriallog",
                "stdout",
            ],
            "!1234abcd",
            "stdout",
        ),
    ):
        argv = ["--dry-run", *preview_argv, *extra]
        parser, args = _parse(argv)
        hooks = _make_hooks()
        bootstrap._validate_and_normalize_args(args, parser, hooks)
        assert args.dest == expected_dest
        assert args.seriallog == expected_seriallog


@pytest.mark.unit
def test_dry_run_passes_despite_truthy_parameter_defaults() -> None:
    """Regression: truthy parser defaults are not mistaken for requested actions.

    ``--lockdown-wait`` (default 20.0) and ``--key-verify-wait`` (default
    timeout) are truthy when merely parsed; the gate must still pass an
    invocation that only carries ``--dry-run`` and ``--set``, because
    default-carrying parameters are not requested actions.
    """
    argv = ["--dry-run", "--set", "a", "1"]
    parser, args = _parse(argv)
    assert args.key_verify_nonce == 0
    hooks = _make_hooks()
    bootstrap._validate_and_normalize_args(args, parser, hooks)


@pytest.mark.unit
def test_rejected_dry_run_never_reaches_transport_construction() -> None:
    """A rejected ``--dry-run`` exits before any transport factory is built."""
    argv = ["--dry-run", "--set", "a", "1", "--host", "127.0.0.1", "--reboot"]
    parser, args = _parse(argv)
    hooks = _make_hooks()
    with pytest.raises(SystemExit) as exc_info:
        bootstrap.run_common(args, parser, hooks, argv=argv)
    assert exc_info.value.code == 2
    for factory in (hooks.serial_interface, hooks.tcp_interface, hooks.ble_interface):
        factory.assert_not_called()


# Parser flag dests that are inert for a preview: connection selection, global
# output/query formatting, and the parameter flags that only qualify other
# (denied) actions but never request one on their own. Everything else the
# parser registers must appear in ``_DRY_RUN_CONFLICTING_ACTIONS``.
_DRY_RUN_INERT_DESTS = frozenset(
    {
        "set",
        "configure",
        "dry_run",
        "dest",
        "host",
        "port",
        "ble",
        "ble_auto_reconnect",
        "timeout",
        "no_nodes",
        "debug",
        "debuglib",
        "quiet",
        "seriallog",
        "json",
        "channel_fetch_attempts",
        "export_format",
        "no_time",
        "private",
        "deprecated",
        "help",
        "version",
        "key_verify_nonce",
        "key_verify_security_number",
        "key_verify_wait",
        "lockdown_boots",
        "lockdown_max_session_seconds",
        "lockdown_valid_until",
        "lockdown_wait",
    }
)


@pytest.mark.unit
def test_dry_run_denylist_covers_every_registered_parser_flag() -> None:
    """Every registered flag dest is inert for previews or denylisted.

    The gate refuses only the denylisted actions and fails open for anything
    unlisted, so a future parser flag that performs a device action would be
    silently accepted under ``--dry-run`` unless the denylist names it. This
    guard walks the real parser's registered dests (never flag labels, which
    can alias) and fails here on parser↔table drift instead of failing open at
    runtime.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parse_cli_args(parser, version="t", argv=[])
    flag_dests = {action.dest for action in parser._actions if action.option_strings}
    denylisted = {dest for dest, _label in bootstrap._DRY_RUN_CONFLICTING_ACTIONS}

    uncovered = sorted(flag_dests - denylisted - _DRY_RUN_INERT_DESTS)
    assert not uncovered, (
        "parser flags missing from both the --dry-run denylist and the inert "
        f"set (the gate would fail open for them): {uncovered}"
    )
