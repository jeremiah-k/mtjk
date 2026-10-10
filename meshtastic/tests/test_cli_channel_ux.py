"""Focused CLI validation tests for channel and node-list options."""

import argparse
import sys
from unittest.mock import MagicMock, create_autospec, patch

import pytest

from meshtastic.__main__ import main
from meshtastic.cli.channel_contact_actions import (
    ChannelContactHooks,
    _handle_channel_delete,
)
from meshtastic.cli.context import ActionOutcome, CliContext
from meshtastic.mesh_interface import MeshInterface
from meshtastic.tcp_interface import TCPInterface


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_ch_add_with_index_explains_how_to_target_channels(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meshtastic",
            "--host",
            "meshtastic.local",
            "--ch-add",
            "test",
            "--ch-index",
            "1",
        ],
    )
    interface = MagicMock(autospec=TCPInterface)
    interface.__enter__ = MagicMock(return_value=interface)
    interface.__exit__ = MagicMock(return_value=None)

    with patch("meshtastic.tcp_interface.TCPInterface", return_value=interface):
        with pytest.raises(SystemExit) as exc_info:
            main()

    assert exc_info.value.code == 1
    _out, err = capsys.readouterr()
    assert "--ch-add chooses the next free channel index automatically" in err
    assert "remove --ch-index and retry" in err


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_nodes_show_fields_rejects_unknown_field_with_choices(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meshtastic",
            "--host",
            "meshtastic.local",
            "--nodes",
            "--show-fields",
            "user.id,channel_aeros index",
        ],
    )
    interface = MagicMock(autospec=TCPInterface)
    interface.__enter__ = MagicMock(return_value=interface)
    interface.__exit__ = MagicMock(return_value=None)
    interface.nodesByNum = {
        1: {"num": 1, "user": {"id": "!00000001", "longName": "Node"}, "channel": 0}
    }

    with patch("meshtastic.tcp_interface.TCPInterface", return_value=interface):
        with pytest.raises(SystemExit) as exc_info:
            main()

    assert exc_info.value.code == 1
    interface.showNodes.assert_not_called()
    _out, err = capsys.readouterr()
    assert "Unknown --show-fields value(s): channel_aeros index" in err
    assert "user.id" in err
    assert "channel" in err


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_nodes_show_fields_accepts_schema_fields_absent_from_node_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meshtastic",
            "--host",
            "meshtastic.local",
            "--nodes",
            "--show-fields",
            "user.id,environmentMetrics.temperature",
        ],
    )
    interface = MagicMock(autospec=TCPInterface)
    interface.__enter__ = MagicMock(return_value=interface)
    interface.__exit__ = MagicMock(return_value=None)
    interface.nodesByNum = {1: {"num": 1, "user": {"id": "!00000001"}, "channel": 0}}

    with patch("meshtastic.tcp_interface.TCPInterface", return_value=interface):
        main()

    interface.showNodes.assert_called_once_with(
        True,
        ["user.id", "environmentMetrics.temperature"],
        roleFilter=None,
        hwModelFilter=None,
        sortField=None,
        sortDirection=None,
        limit=0,
    )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize("nodes_by_num", [None, {}])
def test_nodes_show_fields_rejects_unknown_field_without_node_database(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    nodes_by_num: object,
) -> None:
    """Schema validation must still run before any nodes have been synchronized."""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meshtastic",
            "--host",
            "meshtastic.local",
            "--nodes",
            "--show-fields",
            "definitely.notAField",
        ],
    )
    interface = MagicMock(autospec=TCPInterface)
    interface.__enter__ = MagicMock(return_value=interface)
    interface.__exit__ = MagicMock(return_value=None)
    interface.nodesByNum = nodes_by_num

    with patch("meshtastic.tcp_interface.TCPInterface", return_value=interface):
        with pytest.raises(SystemExit) as exc_info:
            main()

    assert exc_info.value.code == 1
    interface.showNodes.assert_not_called()
    _out, err = capsys.readouterr()
    assert "Unknown --show-fields value(s): definitely.notAField" in err
    assert "Available fields:\n" in err
    assert "user.id" in err


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_nodes_show_fields_accepts_schema_field_without_node_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Known schema fields should remain usable before NodeDB population."""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meshtastic",
            "--host",
            "meshtastic.local",
            "--nodes",
            "--show-fields",
            "environmentMetrics.temperature",
        ],
    )
    interface = MagicMock(autospec=TCPInterface)
    interface.__enter__ = MagicMock(return_value=interface)
    interface.__exit__ = MagicMock(return_value=None)
    interface.nodesByNum = {}

    with patch("meshtastic.tcp_interface.TCPInterface", return_value=interface):
        main()

    interface.showNodes.assert_called_once_with(
        True,
        ["environmentMetrics.temperature"],
        roleFilter=None,
        hwModelFilter=None,
        sortField=None,
        sortDirection=None,
        limit=0,
    )


@pytest.mark.unit
def test_channel_delete_fails_closed_if_exit_seam_returns() -> None:
    """A missing channel index must never fall through to deletion after exit."""
    interface = create_autospec(MeshInterface, instance=True)
    context = CliContext(
        interface=interface,
        args=argparse.Namespace(ch_del=True, dest="^local"),
        get_node_kwargs={},
        outcome=ActionOutcome(),
    )
    cli_exit = MagicMock()
    hooks = ChannelContactHooks(
        cli_exit=cli_exit,
        cli_print=MagicMock(),
        get_channel_index=MagicMock(return_value=None),
        set_channel_index=MagicMock(),
        resolve_pref=MagicMock(),
        set_pref=MagicMock(),
        fatal_preference_value_errors=MagicMock(),
        preference_value_error=ValueError,
        print_channel_field_choices=MagicMock(),
        is_local_destination=MagicMock(),
        modem_preset_shorthands=(),
    )

    with pytest.raises(AssertionError, match="cli_exit returned unexpectedly"):
        _handle_channel_delete(context, hooks)

    cli_exit.assert_called_once_with(
        "Warning: Need to specify '--ch-index' for '--ch-del'.", 1
    )
    interface.getNode.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize(
    "query_args",
    [
        ["--role", "client"],
        ["--hwmodel", "rak"],
        ["--sort", "name"],
        ["--limit", "1"],
        ["--nodes", "--sort", "snr:sideways"],
        ["--nodes", "--sort", ""],
        ["--nodes", "--limit", "-1"],
        ["--nodes", "--role", ",,"],
    ],
)
def test_invalid_node_queries_fail_before_transport_or_reboot(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    query_args: list[str],
) -> None:
    monkeypatch.setattr(
        sys, "argv", ["meshtastic", "--host", "radio", "--reboot", *query_args]
    )
    with patch("meshtastic.tcp_interface.TCPInterface") as transport:
        with pytest.raises(SystemExit) as error:
            main()
    assert error.value.code == 2
    transport.assert_not_called()
    assert "error:" in capsys.readouterr().err
