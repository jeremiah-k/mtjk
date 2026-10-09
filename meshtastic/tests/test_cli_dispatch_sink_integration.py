"""Exercise dispatch output with the real facade and protobuf-backed nodes."""

import argparse
from dataclasses import replace
from unittest.mock import patch

import pytest

from meshtastic import __main__ as cli_main
from meshtastic.cli.context import ActionOutcome, CliContext
from meshtastic.cli.dispatch import _dispatch_connected
from meshtastic.cli.invocation import CliInvocation, activate_invocation
from meshtastic.mesh_interface import MeshInterface
from meshtastic.node import Node
from meshtastic.protobuf import channel_pb2


def _dispatch(argv: list[str], interface: MeshInterface) -> list[str]:
    parser = argparse.ArgumentParser(add_help=False)
    args = cli_main.parse_cli_args(parser, version="test", argv=argv)
    lines: list[str] = []

    def output(message: str, *, force: bool = False) -> None:
        del force
        lines.append(message)

    hooks = cli_main._build_connected_dispatch_hooks()
    hooks = replace(
        hooks,
        cli_print=output,
        services=replace(hooks.services, cli_print=output, preference_print=output),
        configure=replace(hooks.configure, cli_print=output),
        channel_contact=replace(hooks.channel_contact, cli_print=output),
    )
    with activate_invocation(
        CliInvocation(args=args, parser=parser, channel_index=args.ch_index)
    ):
        _dispatch_connected(
            CliContext(
                interface, args, {}, ActionOutcome(interface_close_attempted=True)
            ),
            hooks,
        )
    return lines


@pytest.mark.unit
def test_info_dispatch_captures_real_interface_and_node_output(capsys):
    """Info includes actual preference and channel output without stdout leaks."""
    interface = MeshInterface(noProto=True)
    interface.localNode.localConfig.lora.region = 1
    with patch.object(interface, "getNode", return_value=interface.localNode):
        lines = _dispatch(["--dest", "^all", "--info", "--quiet"], interface)
    joined = "\n".join(lines)
    assert "Owner:" in joined
    assert "Preferences:" in joined
    assert "Channels:" in joined
    assert capsys.readouterr() == ("", "")


@pytest.mark.unit
def test_unknown_set_dispatch_captures_choices(capsys):
    """The facade's preflight reporter inherits the injected dispatch sink."""
    interface = MeshInterface(noProto=True)
    with patch.object(interface, "waitForAckNak"):
        lines = _dispatch(["--dest", "!12345678", "--set", "bad_field", "1"], interface)
    assert "Choices are..." in lines
    assert any("bad_field" in line for line in lines)
    assert capsys.readouterr() == ("", "")


@pytest.mark.unit
def test_unknown_channel_set_dispatch_captures_choices_and_exit(capsys):
    """Channel diagnostics and facade exits stay within the dispatch sink."""
    interface = MeshInterface(noProto=True)
    node = Node(interface, "!12345678", noProto=True)
    node.channels = [channel_pb2.Channel(index=0), channel_pb2.Channel(index=1)]
    lines: list[str] = []
    parser = argparse.ArgumentParser(add_help=False)
    args = cli_main.parse_cli_args(
        parser,
        version="test",
        argv=["--dest", "!12345678", "--ch-index", "1", "--ch-set", "bad_field", "1"],
    )
    hooks = cli_main._build_connected_dispatch_hooks()
    hooks = replace(hooks, cli_print=lines.append)
    with (
        activate_invocation(CliInvocation(args, parser, channel_index=1)),
        patch.object(interface, "getNode", return_value=node),
        pytest.raises(SystemExit),
    ):
        _dispatch_connected(CliContext(interface, args, {}), hooks)
    assert "Choices are..." in lines
    assert capsys.readouterr() == ("", "")


@pytest.mark.unit
def test_wrapped_entrypoint_output_does_not_recurse(capsys):
    """A legacy sink wrapping the facade reporter emits once and restores routing."""
    from meshtastic.cli.invocation import _activate_cli_output

    with _activate_cli_output(lambda message: cli_main._cli_print(message)):
        cli_main._cli_print("once")
    cli_main._cli_print("after")
    assert capsys.readouterr() == ("once\nafter\n", "")


@pytest.mark.unit
@pytest.mark.parametrize(
    "query_args", [["--sort", "snr"], ["--nodes", "--sort", "not_a_field"]]
)
def test_invalid_embedded_node_query_prevents_device_actions(
    query_args: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    interface = MeshInterface(noProto=True)
    with (
        patch("meshtastic.cli.device_actions._handle_device_actions") as actions,
        pytest.raises(SystemExit) as error,
    ):
        _dispatch(["--reboot", *query_args], interface)
    assert error.value.code == 1
    actions.assert_not_called()
    assert capsys.readouterr() == ("", "")
