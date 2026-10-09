"""Meshtastic unit tests for __main__.py."""

# pylint: disable=C0302,W0613,R0917

import argparse
import base64
import logging
import re
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, create_autospec, mock_open, patch

import pytest
import yaml

import meshtastic.__main__ as main_module
from meshtastic import mt_config
from meshtastic.__main__ import (
    main,
)

# from ..ble_interface import BLEInterface
from ..node import Node

# from ..radioconfig_pb2 import UserPreferences
# import meshtastic.config_pb2
from ..protobuf import channel_pb2, localonly_pb2
from ..protobuf.channel_pb2 import Channel  # pylint: disable=E0611
from ..serial_interface import SerialInterface
from ..util import Timeout
from ._main_legacy_support import (
    _build_configure_interface,
    _mock_send_text,
    _run_main_configure_file,
)

# from ..remote_hardware import onGPIOreceive
# from ..config_pb2 import Config

MAIN_LOCAL_ADDR: str = cast(str, main_module.__dict__["LOCAL_ADDR"])


@pytest.fixture(autouse=True)
def _mock_newer_version_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent external network calls during unit tests in this module.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Pytest monkeypatching fixture.
    """
    monkeypatch.setattr("meshtastic.util.check_if_newer_version", lambda: None)


@pytest.fixture
def mocked_serial_node() -> tuple[Any, Any]:
    """Return an autospecced serial interface and its selected-node double."""
    mocked_node = create_autospec(Node, instance=True)
    # Modeled on the noProto main-path doubles: section readiness never waits.
    mocked_node.noProto = True
    iface = create_autospec(SerialInterface, instance=True)
    iface.devPath = "/dev/mock"
    iface.__enter__.return_value = iface
    iface.__exit__.return_value = None
    iface.getNode.return_value = mocked_node
    return iface, mocked_node


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_set_owner_to_bob(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --set-owner bob."""
    sys.argv = ["", "--set-owner", "bob"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Setting device owner to bob", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_set_owner_short_to_bob(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --set-owner-short bob."""
    sys.argv = ["", "--set-owner-short", "bob"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Setting device owner short to bob", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_set_time_with_explicit_timestamp(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --set-time TIMESTAMP forwards the provided epoch value."""
    epoch = 1769686798
    sys.argv = ["", "--set-time", str(epoch)]
    mt_config.args = cast(Any, sys.argv)

    mocked_node = MagicMock()
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert err == ""

    mocked_node.setTime.assert_called_once_with(epoch)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_set_time_without_timestamp_uses_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --set-time without argument forwards 0 to trigger node-side current-time behavior."""
    sys.argv = ["", "--set-time"]
    mt_config.args = cast(Any, sys.argv)

    mocked_node = MagicMock()
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert err == ""

    mocked_node.setTime.assert_called_once_with(0)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_set_is_unmessageable_to_true(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --set-is-unmessageable true."""
    sys.argv = ["", "--set-is-unmessageable", "true"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(
            r"Setting device owner is_unmessageable to True", out, re.MULTILINE
        )
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_set_is_unmessagable_to_true(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --set-is-unmessagable true."""
    sys.argv = ["", "--set-is-unmessagable", "true"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(
            r"Setting device owner is_unmessageable to True", out, re.MULTILINE
        )
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_set_canned_messages(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --set-canned-message."""
    sys.argv = ["", "--set-canned-message", "foo"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Setting canned plugin message to foo", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_get_canned_messages(
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: Any,
) -> None:
    """Test --get-canned-message."""
    sys.argv = ["", "--get-canned-message"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = iface_with_nodes
    iface.localNode.cannedPluginMessage = "foo"
    iface.devPath = "bar"

    with caplog.at_level(logging.DEBUG):
        with patch(
            "meshtastic.serial_interface.SerialInterface", return_value=iface
        ) as mo:
            main()
            out, err = capsys.readouterr()
            assert re.search(r"Connected to radio", out, re.MULTILINE)
            assert re.search(r"canned_plugin_message:foo", out, re.MULTILINE)
            assert err == ""
            mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_set_ringtone(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify the CLI --set-ringtone option instructs the device to set the ringtone and prints confirmation.

    Sets argv to request setting the ringtone, patches the SerialInterface,
    runs main(), and asserts stdout contains "Connected to radio" and
    "Setting ringtone to foo,bar", stderr is empty, and the SerialInterface
    was instantiated.

    """
    sys.argv = ["", "--set-ringtone", "foo,bar"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Setting ringtone to foo,bar", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_get_ringtone(
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: Any,
) -> None:
    """Test --get-ringtone."""
    sys.argv = ["", "--get-ringtone"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = iface_with_nodes
    iface.devPath = "bar"

    mocked_node = MagicMock(autospec=Node)
    mocked_node.get_ringtone.return_value = "foo,bar"
    iface.localNode = mocked_node

    with caplog.at_level(logging.DEBUG):
        with patch(
            "meshtastic.serial_interface.SerialInterface", return_value=iface
        ) as mo:
            main()
            out, err = capsys.readouterr()
            assert re.search(r"Connected to radio", out, re.MULTILINE)
            assert re.search(r"ringtone:foo,bar", out, re.MULTILINE)
            assert err == ""
            mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_set_ham_to_KI123(capsys: pytest.CaptureFixture[str]) -> None:
    """``--set-ham`` should license the owner and disable channel encryption."""
    sys.argv = ["", "--set-ham", "KI123"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = create_autospec(Node, instance=True)
    iface = create_autospec(SerialInterface, instance=True)
    iface.devPath = "/dev/mock"
    iface.__enter__.return_value = iface
    iface.__exit__.return_value = None
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()

    assert re.search(r"Connected to radio", out, re.MULTILINE)
    assert re.search(r"Setting Ham ID to KI123", out, re.MULTILINE)
    assert err == ""
    mocked_node.setOwner.assert_called_once_with("KI123", is_licensed=True)
    mocked_node.turnOffEncryptionOnPrimaryChannel.assert_called_once_with()
    mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize(
    ("flag", "method_name"),
    [
        ("--reboot", "reboot"),
        ("--reboot-ota", "rebootOTA"),
        ("--shutdown", "shutdown"),
    ],
)
def test_main_rebooting_command(
    flag: str,
    method_name: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reboot and shutdown flags should invoke exactly their selected node command."""
    sys.argv = ["", flag]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = create_autospec(Node, instance=True)
    iface = create_autospec(SerialInterface, instance=True)
    iface.devPath = "/dev/mock"
    iface.__enter__.return_value = iface
    iface.__exit__.return_value = None
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()

    assert re.search(r"Connected to radio", out, re.MULTILINE)
    assert err == ""
    getattr(mocked_node, method_name).assert_called_once_with()
    mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize(
    ("args", "method_name"),
    [
        (["--reboot", "--ack"], "reboot"),
        (["--reboot-ota", "--ack"], "rebootOTA"),
        (["--enter-dfu", "--ack"], "enterDFUMode"),
        (["--shutdown", "--ack"], "shutdown"),
    ],
)
def test_rebooting_commands_with_ack_skip_wait(
    args: list[str],
    method_name: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Rebooting commands should invoke the node and skip trailing ACK waits."""
    sys.argv = ["", *args]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = create_autospec(Node, instance=True)
    iface = create_autospec(SerialInterface, instance=True)
    iface.devPath = "/dev/mock"
    iface.__enter__.return_value = iface
    iface.__exit__.return_value = None
    iface.getNode.return_value = mocked_node
    mocked_node.iface = iface

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()
        out, err = capsys.readouterr()

    assert "Connected to radio" in out
    assert "Waiting for an acknowledgment from remote node" not in out
    assert err == ""
    getattr(mocked_node, method_name).assert_called_once_with()
    iface.waitForAckNak.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_sendtext(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify that the CLI `--sendtext` command sends a message through the radio interface and reports progress.

    Runs meshtastic.main() with `--sendtext hello`, patches the SerialInterface to capture sendText calls, and asserts that:
    - the output contains connection and "Sending text message" lines,
    - the mocked sendText was invoked and its debug output appeared on stdout,
    - no stderr output was produced.

    """
    sys.argv = ["", "--sendtext", "hello"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.sendText.side_effect = _mock_send_text

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Sending text message", out, re.MULTILINE)
        assert re.search(r"inside mocked sendText", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_sendtext_with_channel(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify that invoking the CLI with.

    `--sendtext <message> --ch-index <n>` results in a sendText call for the
    specified channel and emits the expected connection and send messages.

    The test sets CLI arguments, replaces SerialInterface with a mock whose
    sendText prints identifiable lines, runs main(), and asserts that stdout
    contains "Connected to radio", a "Sending text message" line referencing
    the channel index, and the mock's output. Uses the pytest `capsys`
    fixture to capture stdout/stderr.

    Parameters
    ----------
    capsys : pytest.CaptureFixture[str]
        Pytest capture fixture for reading stdout and stderr.
    """
    sys.argv = ["", "--sendtext", "hello", "--ch-index", "1"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.sendText.side_effect = _mock_send_text

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Sending text message", out, re.MULTILINE)
        assert re.search(r"on channelIndex:1", out, re.MULTILINE)
        assert re.search(r"inside mocked sendText", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize("ch_index", ["-1", "9"])
def test_main_sendtext_with_invalid_channel(
    ch_index: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--sendtext`` should reject channel indices outside the valid range."""
    sys.argv = ["", "--sendtext", "hello", "--ch-index", ch_index]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.localNode.getChannelByChannelIndex.return_value = None
    iface.localNode.getChannelCopyByChannelIndex.return_value = None

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        with pytest.raises(SystemExit) as exc_info:
            main()

    assert exc_info.value.code == 1
    _, err = capsys.readouterr()
    assert re.search(r"is not a valid channel", err, re.MULTILINE)
    mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("meshtastic.serial_interface.SerialInterface._clear_hupcl_on_fd")
@patch("meshtastic.serial_interface.SerialInterface._set_hupcl_with_termios")
@patch("builtins.open", new_callable=mock_open, read_data="data")
@patch("serial.Serial")
@patch("meshtastic.util.findPorts", return_value=["/dev/ttyUSBfake"])
def test_main_sendtext_with_dest(
    _mock_findPorts: Any,
    _mock_serial: Any,
    _mocked_open: Any,
    _mock_hupcl: Any,
    _mock_clear_hupcl: Any,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test --sendtext with --dest."""
    sys.argv = ["", "--sendtext", "hello", "--dest", "!12345678"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with SerialInterface(noProto=True, connectNow=False) as serial_interface:
        mocked_channel = MagicMock(autospec=Channel)
        serial_interface.localNode.getChannelByChannelIndex = MagicMock(  # type: ignore[method-assign]
            return_value=mocked_channel
        )
        serial_interface.localNode.getChannelCopyByChannelIndex = MagicMock(  # type: ignore[method-assign]
            return_value=mocked_channel
        )

        with patch(
            "meshtastic.serial_interface.SerialInterface", return_value=serial_interface
        ):
            with caplog.at_level(logging.DEBUG):
                # Note: With noProto=True, the packet is not actually sent due to
                # "protocol use is disabled by noProto", so no SystemExit is raised
                main()
                out, err = capsys.readouterr()
                assert re.search(r"Connected to radio", out, re.MULTILINE)
            assert re.search(r"Not sending packet", caplog.text, re.MULTILINE)
            assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_removeposition_remote(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --remove-position with a remote dest."""
    sys.argv = ["", "--remove-position", "--dest", "!12345678"]
    mt_config.args = sys.argv  # type: ignore[assignment]
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(
            r"Removing fixed position and disabling fixed position setting",
            out,
            re.MULTILINE,
        )
        assert re.search(
            r"Waiting for an acknowledgment from remote node", out, re.MULTILINE
        )
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_removeposition_local_dest_waits_for_ack_and_uses_local_dest(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Explicit ^local destinations should still use the normal ACK wait flow."""
    sys.argv = ["", "--remove-position", "--dest", MAIN_LOCAL_ADDR]
    mt_config.args = sys.argv  # type: ignore[assignment]
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    # Keep assertion anchored to the interface-level waiter contract.
    iface.getNode.return_value.iface = iface
    waiter = iface.waitForAckNak
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()
        out, err = capsys.readouterr()
        assert "Connected to radio" in out
        assert "Removing fixed position and disabling fixed position setting" in out
        assert "Waiting for an acknowledgment from remote node" in out
        assert err == ""
    waiter.assert_called_once()
    assert any(
        (call_args.args and call_args.args[0] == MAIN_LOCAL_ADDR)
        or call_args.kwargs.get("dest") == MAIN_LOCAL_ADDR
        for call_args in iface.getNode.call_args_list
    )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_setlat_remote(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --setlat with a remote dest."""
    sys.argv = ["", "--setlat", "37.5", "--dest", "!12345678"]
    mt_config.args = sys.argv  # type: ignore[assignment]
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(
            r"Setting device position and enabling fixed position setting",
            out,
            re.MULTILINE,
        )
        assert re.search(
            r"Waiting for an acknowledgment from remote node", out, re.MULTILINE
        )
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_removeposition(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify that invoking the CLI with --remove-position connects to the radio, removes the node's fixed position, and prints confirmation.

    Asserts that "Connected to radio" and "Removing fixed position" appear on stdout, that the
    node's removeFixedPosition was invoked (observable via its printed output), stderr is empty,
    and a SerialInterface instance was created.

    """
    sys.argv = ["", "--remove-position"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)

    def _mock_remove_fixed_position() -> None:
        """Simulate removing fixed position."""
        print("inside mocked removeFixedPosition")

    mocked_node.removeFixedPosition.side_effect = _mock_remove_fixed_position

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Removing fixed position", out, re.MULTILINE)
        assert re.search(r"inside mocked removeFixedPosition", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize(
    ("flag", "value", "expected_message", "expected_call"),
    [
        ("--setlat", "37.5", "Fixing latitude", (37.5, 0.0, 0)),
        ("--setlon", "-122.1", "Fixing longitude", (0.0, -122.1, 0)),
        ("--setlat", "900000000", "Fixing latitude", (900000000, 0.0, 0)),
        (
            "--setlon",
            "-1800000000",
            "Fixing longitude",
            (0.0, -1800000000, 0),
        ),
        ("--setalt", "51", "Fixing altitude", (0.0, 0.0, 51)),
    ],
)
def test_main_set_fixed_position(
    flag: str,
    value: str,
    expected_message: str,
    expected_call: tuple[float, float, int],
    capsys: pytest.CaptureFixture[str],
    mocked_serial_node: tuple[Any, Any],
) -> None:
    """Each fixed-position flag should forward the normalized coordinates exactly."""
    sys.argv = ["", flag, value]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface, mocked_node = mocked_serial_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()

    assert re.search(r"Connected to radio", out, re.MULTILINE)
    assert expected_message in out
    assert re.search(r"Setting device position", out, re.MULTILINE)
    assert err == ""
    mocked_node.setFixedPosition.assert_called_once_with(*expected_call)
    mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_seturl(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --seturl (url used below is what is generated after a factory_reset)."""
    sys.argv = ["", "--seturl", "https://www.meshtastic.org/d/#CgUYAyIBAQ"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("meshtastic.serial_interface.SerialInterface._clear_hupcl_on_fd")
@patch("meshtastic.serial_interface.SerialInterface._set_hupcl_with_termios")
@patch("builtins.open", new_callable=mock_open, read_data="data")
@patch("serial.Serial")
@patch("meshtastic.util.findPorts", return_value=["/dev/ttyUSBfake"])
def test_main_set_valid(
    _mocked_findports: Any,
    _mocked_serial: Any,
    _mocked_open: Any,
    _mocked_hupcl: Any,
    _mock_clear_hupcl: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --set with valid field."""
    sys.argv = ["", "--set", "network.wifi_ssid", "foo"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with SerialInterface(noProto=True, connectNow=False) as serialInterface:
        anode = Node(serialInterface, 1234567890, noProto=True)
        serialInterface.localNode = anode

        with patch(
            "meshtastic.serial_interface.SerialInterface", return_value=serialInterface
        ) as mo:
            main()
            out, err = capsys.readouterr()
            assert re.search(r"Connected to radio", out, re.MULTILINE)
            assert re.search(r"Set network.wifi_ssid to <redacted>", out, re.MULTILINE)
            assert err == ""
            mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("meshtastic.serial_interface.SerialInterface._clear_hupcl_on_fd")
@patch("meshtastic.serial_interface.SerialInterface._set_hupcl_with_termios")
@patch("builtins.open", new_callable=mock_open, read_data="data")
@patch("serial.Serial")
@patch("meshtastic.util.findPorts", return_value=["/dev/ttyUSBfake"])
def test_main_set_valid_display_use_12_hour_alias(
    _mocked_findports: Any,
    _mocked_serial: Any,
    _mocked_open: Any,
    _mocked_hupcl: Any,
    _mock_clear_hupcl: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --set accepts legacy display.use_12_hour alias."""
    sys.argv = ["", "--set", "display.use_12_hour", "true"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with SerialInterface(noProto=True, connectNow=False) as serialInterface:
        anode = Node(serialInterface, 1234567890, noProto=True)
        serialInterface.localNode = anode

        with patch(
            "meshtastic.serial_interface.SerialInterface", return_value=serialInterface
        ) as mo:
            main()
            out, err = capsys.readouterr()
            assert re.search(r"Connected to radio", out, re.MULTILINE)
            assert re.search(r"Set display.use_12h_clock to true", out, re.MULTILINE)
            assert anode.localConfig.display.use_12h_clock is True
            assert err == ""
            mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("meshtastic.serial_interface.SerialInterface._clear_hupcl_on_fd")
@patch("meshtastic.serial_interface.SerialInterface._set_hupcl_with_termios")
@patch("builtins.open", new_callable=mock_open, read_data="data")
@patch("serial.Serial")
@patch("meshtastic.util.findPorts", return_value=["/dev/ttyUSBfake"])
def test_main_set_valid_wifi_psk(
    _mocked_findports: Any,
    _mocked_serial: Any,
    _mocked_open: Any,
    _mocked_hupcl: Any,
    _mock_clear_hupcl: Any,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test --set with valid field."""
    sys.argv = ["", "--set", "network.wifi_psk", "123456789"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with SerialInterface(noProto=True, connectNow=False) as serialInterface:
        anode = Node(serialInterface, 1234567890, noProto=True)
        serialInterface.localNode = anode

        with caplog.at_level(logging.INFO):
            with patch(
                "meshtastic.serial_interface.SerialInterface",
                return_value=serialInterface,
            ) as mo:
                main()
            out, err = capsys.readouterr()
            assert re.search(r"Connected to radio", out, re.MULTILINE)
            assert re.search(r"Set network\.wifi_psk to <redacted>", out, re.MULTILINE)
            assert "123456789" not in out
            assert "123456789" not in caplog.text
            assert err == ""
            mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("meshtastic.serial_interface.SerialInterface._clear_hupcl_on_fd")
@patch("meshtastic.serial_interface.SerialInterface._set_hupcl_with_termios")
@patch("builtins.open", new_callable=mock_open, read_data="data")
@patch("serial.Serial")
@patch("meshtastic.util.findPorts", return_value=["/dev/ttyUSBfake"])
def test_main_set_valid_lora_hop_limit(
    _mocked_findports: Any,
    _mocked_serial: Any,
    _mocked_open: Any,
    _mocked_hupcl: Any,
    _mock_clear_hupcl: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --set lora.hop_limit applies in a single configure write."""
    sys.argv = ["", "--set", "lora.hop_limit", "4"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with SerialInterface(noProto=True, connectNow=False) as serialInterface:
        anode = Node(serialInterface, 1234567890, noProto=True)
        serialInterface.localNode = anode

        with patch(
            "meshtastic.serial_interface.SerialInterface", return_value=serialInterface
        ):
            main()
            out, err = capsys.readouterr()
            assert re.search(r"Connected to radio", out, re.MULTILINE)
            assert re.search(r"Set lora.hop_limit to 4", out, re.MULTILINE)
            assert re.search(r"Writing lora configuration to device", out, re.MULTILINE)
            assert err == ""

    assert anode.localConfig.lora.hop_limit == 4


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("meshtastic.serial_interface.SerialInterface._clear_hupcl_on_fd")
@patch("meshtastic.serial_interface.SerialInterface._set_hupcl_with_termios")
@patch("builtins.open", new_callable=mock_open, read_data="data")
@patch("serial.Serial")
@patch("meshtastic.util.findPorts", return_value=["/dev/ttyUSBfake"])
def test_main_set_invalid_wifi_psk(
    _mocked_findports: Any,
    _mocked_serial: Any,
    _mocked_open: Any,
    _mocked_hupcl: Any,
    _mock_clear_hupcl: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --set with an invalid value (psk must be 8 or more characters)."""
    sys.argv = ["", "--set", "network.wifi_psk", "1234567"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with SerialInterface(noProto=True, connectNow=False) as serialInterface:
        anode = Node(serialInterface, 1234567890, noProto=True)
        serialInterface.localNode = anode

        with patch(
            "meshtastic.serial_interface.SerialInterface", return_value=serialInterface
        ) as mo:
            main()
            out, err = capsys.readouterr()
            assert re.search(r"Connected to radio", out, re.MULTILINE)
            assert not re.search(r"Set network.wifi_psk to 1234567", out, re.MULTILINE)
            assert re.search(
                r"Warning: network.wifi_psk must be 8 or more characters.",
                out,
                re.MULTILINE,
            )
            assert err == ""
            mo.assert_called()

        assert anode.localConfig.network.wifi_psk == ""


@pytest.fixture
def pref_node() -> SimpleNamespace:
    """Return the minimal node surface required by ``getPref`` tests."""
    return SimpleNamespace(
        localConfig=localonly_pb2.LocalConfig(),
        moduleConfig=localonly_pb2.LocalModuleConfig(),
        requestConfig=MagicMock(),
    )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_get_pref_redacts_security_private_key(
    pref_node: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """getPref() should redact secret-bearing security values in field reads."""
    private_key = bytes(range(32))
    pref_node.localConfig.security.private_key = private_key

    assert main_module.getPref(pref_node, "security.private_key") is True
    out, err = capsys.readouterr()
    assert "security.private_key: <redacted>" in out
    assert base64.b64encode(private_key).decode("utf-8") not in out
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_get_pref_redacts_security_section_values(
    pref_node: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Whole-field getPref() reads should redact secret values in each printed field."""
    private_key = bytes(range(32))
    public_key = bytes(range(32, 64))
    admin_key = bytes(range(64, 96))
    pref_node.localConfig.security.private_key = private_key
    pref_node.localConfig.security.public_key = public_key
    pref_node.localConfig.security.admin_key.append(admin_key)

    assert main_module.getPref(pref_node, "security") is True
    out, err = capsys.readouterr()
    assert "security.private_key: <redacted>" in out
    assert "security.public_key: <redacted>" in out
    assert re.search(r"security\.admin_key:.*<redacted>", out)
    assert base64.b64encode(private_key).decode("utf-8") not in out
    assert base64.b64encode(public_key).decode("utf-8") not in out
    assert base64.b64encode(admin_key).decode("utf-8") not in out
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_get_pref_allow_secrets_shows_private_key(
    pref_node: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """getPref(allow_secrets=True) should show the actual private key value."""
    private_key = bytes(range(32))
    pref_node.localConfig.security.private_key = private_key

    with caplog.at_level(logging.DEBUG):
        assert (
            main_module.getPref(pref_node, "security.private_key", allow_secrets=True)
            is True
        )
    out, err = capsys.readouterr()
    assert "security.private_key: <redacted>" not in out
    assert base64.b64encode(private_key).decode("utf-8") in out
    assert base64.b64encode(private_key).decode("utf-8") not in caplog.text
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_get_pref_allow_secrets_shows_security_section_keys(
    pref_node: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """getPref(allow_secrets=True) whole-field read should show actual key values."""
    private_key = bytes(range(32))
    public_key = bytes(range(32, 64))
    pref_node.localConfig.security.private_key = private_key
    pref_node.localConfig.security.public_key = public_key

    with caplog.at_level(logging.DEBUG):
        assert main_module.getPref(pref_node, "security", allow_secrets=True) is True
    out, err = capsys.readouterr()
    assert "<redacted>" not in out
    assert base64.b64encode(private_key).decode("utf-8") in out
    assert base64.b64encode(public_key).decode("utf-8") in out
    assert base64.b64encode(private_key).decode("utf-8") not in caplog.text
    assert base64.b64encode(public_key).decode("utf-8") not in caplog.text
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_get_pref_routes_field_reads_through_injected_sink(
    pref_node: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """getPref() should deliver field reads to an injected sink, not stdout."""
    pref_node.localConfig.lora.region = 8
    sink_lines: list[str] = []

    assert (
        main_module.getPref(pref_node, "lora.region", cli_print=sink_lines.append)
        is True
    )

    out, err = capsys.readouterr()
    assert out == ""
    assert err == ""
    assert "lora.region: 8" in sink_lines


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize(
    ("path", "root_name", "section", "field", "value", "expected"),
    [
        ("lora.region", "localConfig", "lora", "region", 8, "lora.region: 8"),
        (
            "telemetry.device_update_interval",
            "moduleConfig",
            "telemetry",
            "device_update_interval",
            60,
            "telemetry.device_update_interval: 60",
        ),
        ("lora", "localConfig", "lora", "region", 8, "lora.region: 8"),
        (
            "security.private_key",
            "localConfig",
            "security",
            "private_key",
            bytes(range(32)),
            "security.private_key: <redacted>",
        ),
    ],
)
def test_get_pref_displays_newly_requested_section(
    pref_node: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
    path: str,
    root_name: str,
    section: str,
    field: str,
    value: Any,
    expected: str,
) -> None:
    """First reads of remote sections must capture the received value."""
    root = getattr(pref_node, root_name)

    def receive_config(_descriptor: Any) -> None:
        setattr(getattr(root, section), field, value)

    pref_node.requestConfig.side_effect = receive_config
    lines: list[str] = []

    assert main_module.getPref(pref_node, path, cli_print=lines.append)

    pref_node.requestConfig.assert_called_once_with(
        root.DESCRIPTOR.fields_by_name[section]
    )
    assert lines == [expected]
    assert capsys.readouterr() == ("", "")


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_get_pref_does_not_treat_another_section_as_requested_state(
    pref_node: SimpleNamespace,
) -> None:
    """A loaded sibling section cannot vouch for a default-looking value."""
    pref_node.localConfig.device.role = 1
    pref_node.requestConfig.side_effect = lambda _: setattr(
        pref_node.localConfig.lora, "region", 8
    )
    lines: list[str] = []

    assert main_module.getPref(pref_node, "lora.region", cli_print=lines.append)

    pref_node.requestConfig.assert_called_once()
    assert lines == ["lora.region: 8"]


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_get_pref_accepts_present_default_section_without_request(
    pref_node: SimpleNamespace,
) -> None:
    """An explicitly loaded all-default section carries authoritative defaults."""
    pref_node.localConfig.lora.SetInParent()
    lines: list[str] = []

    assert main_module.getPref(pref_node, "lora.region", cli_print=lines.append)

    pref_node.requestConfig.assert_not_called()
    assert lines == ["lora.region: 0"]


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_get_pref_waits_for_async_module_response(
    pref_node: SimpleNamespace,
) -> None:
    """The asynchronous compatibility request path gets a bounded presence wait."""
    pref_node._timeout = Timeout(maxSecs=1)
    response = threading.Timer(
        0.02,
        lambda: setattr(pref_node.moduleConfig.telemetry, "device_update_interval", 60),
    )
    pref_node.requestConfig.side_effect = lambda _: response.start()
    lines: list[str] = []

    try:
        assert main_module.getPref(
            pref_node, "telemetry.device_update_interval", cli_print=lines.append
        )
    finally:
        response.cancel()
        response.join()

    assert lines == ["telemetry.device_update_interval: 60"]


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_get_pref_fails_when_request_returns_without_section(
    pref_node: SimpleNamespace,
) -> None:
    """No response must produce an interface error instead of manufactured defaults."""
    pref_node._timeout = Timeout(maxSecs=0)
    lines: list[str] = []

    with pytest.raises(main_module.MeshInterface.MeshInterfaceError, match="lora"):
        main_module.getPref(pref_node, "lora.region", cli_print=lines.append)

    assert lines == []


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_get_pref_preserves_request_failure(
    pref_node: SimpleNamespace,
) -> None:
    """Correlated routing refusals retain their typed error and emit no value."""
    pref_node.requestConfig.side_effect = main_module.MeshInterface.MeshInterfaceError(
        "Routing error on response: NOT_AUTHORIZED"
    )
    lines: list[str] = []

    with pytest.raises(
        main_module.MeshInterface.MeshInterfaceError, match="NOT_AUTHORIZED"
    ):
        main_module.getPref(pref_node, "lora.region", cli_print=lines.append)

    assert lines == []


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_get_pref_noproto_requests_without_waiting_or_displaying_defaults(
    pref_node: SimpleNamespace,
) -> None:
    """NoProto retains its skipped-request behavior without burning a timeout."""
    pref_node.noProto = True
    pref_node._timeout = MagicMock()
    lines: list[str] = []

    assert main_module.getPref(pref_node, "lora.region", cli_print=lines.append)

    pref_node.requestConfig.assert_called_once()
    pref_node._timeout.waitForSet.assert_not_called()
    assert lines == []


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_get_pref_redacts_secret_values_delivered_to_injected_sink(
    pref_node: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Redaction must apply to secret values delivered through an injected sink."""
    private_key = bytes(range(32))
    pref_node.localConfig.security.private_key = private_key
    sink_lines: list[str] = []

    assert (
        main_module.getPref(
            pref_node, "security.private_key", cli_print=sink_lines.append
        )
        is True
    )

    out, err = capsys.readouterr()
    assert out == ""
    assert err == ""
    assert "security.private_key: <redacted>" in sink_lines
    assert base64.b64encode(private_key).decode("utf-8") not in "".join(sink_lines)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_get_pref_unknown_pref_routes_choices_through_injected_sink(
    pref_node: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unknown-preference diagnostics and choice listings must reach the sink."""
    sink_lines: list[str] = []

    assert (
        main_module.getPref(
            pref_node, "bogus.no_such_field", cli_print=sink_lines.append
        )
        is False
    )

    out, err = capsys.readouterr()
    assert out == ""
    assert err == ""
    assert any("do not have an attribute no_such_field" in line for line in sink_lines)
    assert "Choices are..." in sink_lines
    assert "lora:" in sink_lines
    assert any("lora.region" in line for line in sink_lines)


@pytest.mark.unit
def test_support_info_routes_through_injected_sink(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """supportInfo() should deliver its troubleshooting report to an injected sink."""
    sink_lines: list[str] = []

    main_module.supportInfo(cli_print=sink_lines.append)

    out, err = capsys.readouterr()
    assert out == ""
    assert err == ""
    assert "CLI / python library" in "".join(sink_lines)
    assert any("Platform:" in line for line in sink_lines)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_print_set_field_choices_routes_through_injected_sink(
    pref_node: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unknown --set diagnostics and choice listings must reach the injected sink."""
    sink_lines: list[str] = []

    main_module._print_set_field_choices(
        pref_node, ["lora.bogus"], cli_print=sink_lines.append
    )

    out, err = capsys.readouterr()
    assert out == ""
    assert err == ""
    assert any("do not have an attribute lora.bogus" in line for line in sink_lines)
    assert "Choices are..." in sink_lines
    assert "lora:" in sink_lines


@pytest.mark.unit
def test_print_channel_field_choices_routes_through_injected_sink(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unknown --ch-set diagnostics and choice listings must reach the injected sink."""
    sink_lines: list[str] = []

    main_module._print_channel_field_choices(
        channel_pb2.ChannelSettings(), "bogus", cli_print=sink_lines.append
    )

    out, err = capsys.readouterr()
    assert out == ""
    assert err == ""
    assert any("does not have an attribute bogus" in line for line in sink_lines)
    assert "Choices are..." in sink_lines
    assert "psk" in "".join(sink_lines)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_on_receive_reply_routes_through_injected_sink(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--reply echo output must reach the injected sink."""
    mt_config.args = argparse.Namespace(reply=True, sendtext=None, ch_index=None)
    interface = MagicMock()
    interface.myInfo = SimpleNamespace(my_node_num=123)
    sink_lines: list[str] = []

    main_module._on_receive_with_sink(
        {
            "decoded": {"text": "hello there", "portnum": "TEXT_MESSAGE_APP"},
            "to": 123,
            "from": 456,
            "channel": 0,
            "rxSnr": 12.5,
            "hopLimit": 3,
        },
        interface,
        cli_print=sink_lines.append,
    )

    out, err = capsys.readouterr()
    assert out == ""
    assert err == ""
    assert "message: hello there" in sink_lines
    assert (
        "Received channel 0. Sending reply: got msg 'hello there' "
        "with rxSnr: 12.5 and hopLimit: 3" in sink_lines
    )
    interface.sendText.assert_called_once_with(
        "got msg 'hello there' with rxSnr: 12.5 and hopLimit: 3", channelIndex=0
    )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("meshtastic.serial_interface.SerialInterface._clear_hupcl_on_fd")
@patch("meshtastic.serial_interface.SerialInterface._set_hupcl_with_termios")
@patch("builtins.open", new_callable=mock_open, read_data="data")
@patch("serial.Serial")
@patch("meshtastic.util.findPorts", return_value=["/dev/ttyUSBfake"])
def test_main_set_valid_camel_case(
    _mocked_findports: Any,
    _mocked_serial: Any,
    _mocked_open: Any,
    _mocked_hupcl: Any,
    _mock_clear_hupcl: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --set with valid field."""
    sys.argv = ["", "--set", "network.wifi_ssid", "foo"]
    mt_config.args = sys.argv  # type: ignore[assignment]
    mt_config.camel_case = True

    with SerialInterface(noProto=True, connectNow=False) as serialInterface:
        anode = Node(serialInterface, 1234567890, noProto=True)
        serialInterface.localNode = anode

        with patch(
            "meshtastic.serial_interface.SerialInterface", return_value=serialInterface
        ) as mo:
            main()
            out, err = capsys.readouterr()
            assert re.search(r"Connected to radio", out, re.MULTILINE)
            assert re.search(r"Set network.wifiSsid to <redacted>", out, re.MULTILINE)
            assert err == ""
            mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("meshtastic.serial_interface.SerialInterface._clear_hupcl_on_fd")
@patch("meshtastic.serial_interface.SerialInterface._set_hupcl_with_termios")
@patch("builtins.open", new_callable=mock_open, read_data="data")
@patch("serial.Serial")
@patch("meshtastic.util.findPorts", return_value=["/dev/ttyUSBfake"])
def test_main_set_with_invalid(
    _mocked_findports: Any,
    _mocked_serial: Any,
    _mocked_open: Any,
    _mocked_hupcl: Any,
    _mock_clear_hupcl: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --set with invalid field."""
    sys.argv = ["", "--set", "foo", "foo"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with SerialInterface(noProto=True, connectNow=False) as serialInterface:
        anode = Node(serialInterface, 1234567890, noProto=True)
        serialInterface.localNode = anode

        with patch(
            "meshtastic.serial_interface.SerialInterface", return_value=serialInterface
        ) as mo:
            main()
            out, err = capsys.readouterr()
            assert re.search(r"Connected to radio", out, re.MULTILINE)
            assert re.search(r"do not have an attribute foo", out, re.MULTILINE)
            assert err == ""
            mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("meshtastic.serial_interface.SerialInterface._clear_hupcl_on_fd")
@patch("meshtastic.serial_interface.SerialInterface._set_hupcl_with_termios")
@patch("builtins.open", new_callable=mock_open, read_data="data")
@patch("serial.Serial")
@patch("meshtastic.util.findPorts", return_value=["/dev/ttyUSBfake"])
def test_main_set_with_module_prefix_hints_unprefixed_path(
    _mocked_findports: Any,
    _mocked_serial: Any,
    _mocked_open: Any,
    _mocked_hupcl: Any,
    _mock_clear_hupcl: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --set with a 'module.'-prefixed path points at the unprefixed path."""
    sys.argv = ["", "--set", "module.mesh_beacon.flags", "3"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with SerialInterface(noProto=True, connectNow=False) as serialInterface:
        anode = Node(serialInterface, 1234567890, noProto=True)
        serialInterface.localNode = anode

        with patch(
            "meshtastic.serial_interface.SerialInterface", return_value=serialInterface
        ) as mo:
            main()
            out, err = capsys.readouterr()
            assert re.search(
                r"do not have an attribute module\.mesh_beacon\.flags",
                out,
                re.MULTILINE,
            )
            assert re.search(r"Did you mean 'mesh_beacon\.flags'", out, re.MULTILINE)
            assert err == ""
            mo.assert_called()


@pytest.mark.unit
def test_module_prefix_hint_covers_bare_and_unrelated_segments() -> None:
    """The hint helper handles a bare 'module' segment and leaves others alone."""
    assert (
        main_module._module_prefix_strip_hint("module")
        == main_module.MODULE_PREFIX_HINT
    )
    assert main_module._module_prefix_strip_hint("module.") == (
        main_module.MODULE_PREFIX_HINT
    )
    assert main_module._module_prefix_strip_hint("device.role") is None


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("meshtastic.serial_interface.SerialInterface._clear_hupcl_on_fd")
@patch("meshtastic.serial_interface.SerialInterface._set_hupcl_with_termios")
@patch(
    "builtins.open",
    new_callable=mock_open,
    read_data="owner: TestSnake\nowner_short: TS\n",
)
@patch("serial.Serial")
@patch("meshtastic.util.findPorts", return_value=["/dev/ttyUSBfake"])
def test_main_configure_with_snake_case(
    _mocked_findports: Any,
    _mocked_serial: Any,
    _mocked_open: Any,
    _mocked_hupcl: Any,
    _mock_clear_hupcl: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --configure applies snake_case owner/owner_short keys."""
    sys.argv = ["", "--configure", "example_config.yaml"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with SerialInterface(noProto=True, connectNow=False) as serialInterface:
        anode = Node(serialInterface, 1234567890, noProto=True)
        serialInterface.localNode = anode

        with patch(
            "meshtastic.serial_interface.SerialInterface", return_value=serialInterface
        ) as mo:
            main()
            out, err = capsys.readouterr()
            assert re.search(r"Connected to radio", out, re.MULTILINE)
            assert re.search(r"Setting device owner to TestSnake", out, re.MULTILINE)
            assert re.search(r"Setting device owner short to TS", out, re.MULTILINE)
        assert re.search(
            r"Configuration applied \(no reboot expected\)", out, re.MULTILINE
        )
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("meshtastic.serial_interface.SerialInterface._clear_hupcl_on_fd")
@patch("meshtastic.serial_interface.SerialInterface._set_hupcl_with_termios")
@patch(
    "builtins.open",
    new_callable=mock_open,
    read_data="owner: TestCamel\nownerShort: TC\n",
)
@patch("serial.Serial")
@patch("meshtastic.util.findPorts", return_value=["/dev/ttyUSBfake"])
def test_main_configure_with_camel_case_keys(
    _mocked_findports: Any,
    _mocked_serial: Any,
    _mocked_open: Any,
    _mocked_hupcl: Any,
    _mock_clear_hupcl: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --configure applies camelCase owner/ownerShort keys."""
    sys.argv = ["", "--configure", "exampleConfig.yaml"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with SerialInterface(noProto=True, connectNow=False) as serialInterface:
        anode = Node(serialInterface, 1234567890, noProto=True)
        serialInterface.localNode = anode

        with patch(
            "meshtastic.serial_interface.SerialInterface", return_value=serialInterface
        ) as mo:
            main()
            out, err = capsys.readouterr()
            assert re.search(r"Connected to radio", out, re.MULTILINE)
            assert re.search(r"Setting device owner to TestCamel", out, re.MULTILINE)
            assert re.search(r"Setting device owner short to TC", out, re.MULTILINE)
        assert re.search(
            r"Configuration applied \(no reboot expected\)", out, re.MULTILINE
        )
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize(
    ("owner_key", "expected_error"),
    [
        (
            "owner",
            "ERROR: Long Name cannot be empty or contain only whitespace characters",
        ),
        (
            "owner_short",
            "ERROR: Short Name cannot be empty or contain only whitespace characters",
        ),
        (
            "ownerShort",
            "ERROR: Short Name cannot be empty or contain only whitespace characters",
        ),
    ],
)
def test_main_configure_rejects_blank_owner_fields(
    owner_key: str,
    expected_error: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --configure rejects blank owner fields and exits with a clear message."""
    config_path = tmp_path / "invalid_owner.yaml"
    config_path.write_text(yaml.safe_dump({owner_key: "   "}), encoding="utf-8")
    iface, target_node = _build_configure_interface()

    with pytest.raises(SystemExit) as excinfo:
        _run_main_configure_file(config_path, iface, monkeypatch)

    _, err = capsys.readouterr()
    assert expected_error in err
    assert excinfo.value.code == 1
    target_node.setOwner.assert_not_called()
    target_node.commitSettingsTransaction.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_skips_unknown_config_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test --configure skips unknown fields with a batched warning."""
    config_path = tmp_path / "unknown_field.yaml"
    config_path.write_text(
        yaml.safe_dump({"config": {"bluetooth": {"not_a_field": True}}}),
        encoding="utf-8",
    )
    iface, target_node = _build_configure_interface()

    _run_main_configure_file(config_path, iface, monkeypatch)

    assert "not_a_field" in caplog.text
    assert "Skipping 1 unknown field(s) from bluetooth" in caplog.text
    target_node.writeConfig.assert_called_once_with("bluetooth")
    target_node.commitSettingsTransaction.assert_called_once()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize(
    ("flag", "value", "expected"),
    [
        ("--setlat", "91.0", "latitude must be between -90 and 90"),
        ("--setlon", "181.0", "longitude must be between -180 and 180"),
        (
            "--setlat",
            "900000001",
            "latitude premultiplied integer must be between",
        ),
        (
            "--setlon",
            "1800000001",
            "longitude premultiplied integer must be between",
        ),
    ],
)
def test_main_rejects_out_of_range_fixed_coordinates(
    flag: str,
    value: str,
    expected: str,
    capsys: pytest.CaptureFixture[str],
    mocked_serial_node: tuple[Any, Any],
) -> None:
    """Direct fixed-position flags should reject impossible coordinates cleanly."""
    sys.argv = ["", flag, value]
    mt_config.args = sys.argv  # type: ignore[assignment]
    iface, mocked_node = mocked_serial_node

    with (
        patch("meshtastic.serial_interface.SerialInterface", return_value=iface),
        pytest.raises(SystemExit),
    ):
        main()

    captured = capsys.readouterr()
    assert expected in captured.err
    mocked_node.setFixedPosition.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_integer_latitude_preserves_scaled_coordinate_compatibility(
    mocked_serial_node: tuple[Any, Any],
) -> None:
    """Integer CLI latitude values remain historical 1e7-scaled coordinates."""
    sys.argv = ["", "--setlat", "375000000"]
    mt_config.args = sys.argv  # type: ignore[assignment]
    iface, mocked_node = mocked_serial_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()

    mocked_node.setFixedPosition.assert_called_once_with(375000000, 0.0, 0)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize("raw_altitude", [str(1 << 31), str(-(1 << 31) - 1)])
def test_main_rejects_altitude_outside_position_int32(
    raw_altitude: str,
    capsys: pytest.CaptureFixture[str],
    mocked_serial_node: tuple[Any, Any],
) -> None:
    """CLI altitude should fail cleanly outside either protobuf int32 bound."""
    sys.argv = ["", "--setalt", raw_altitude]
    mt_config.args = sys.argv  # type: ignore[assignment]
    iface, mocked_node = mocked_serial_node

    with (
        patch("meshtastic.serial_interface.SerialInterface", return_value=iface),
        pytest.raises(SystemExit),
    ):
        main()

    assert "signed 32-bit position field" in capsys.readouterr().err
    mocked_node.setFixedPosition.assert_not_called()


@pytest.mark.unit
def test_connected_hooks_keep_preference_values_visible_in_quiet_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Standalone --get uses forced output; ordinary CLI banners still respect --quiet."""
    print_hook = MagicMock()
    monkeypatch.setattr(main_module, "_cli_print", print_hook)

    hooks = main_module._build_connected_dispatch_hooks()
    assert hooks.services.preference_print is not None
    hooks.services.preference_print("lora.region: US")

    print_hook.assert_called_once_with("lora.region: US", force=True)
