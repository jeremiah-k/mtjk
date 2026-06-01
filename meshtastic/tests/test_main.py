"""Meshtastic unit tests for __main__.py."""

# pylint: disable=C0302,W0613,R0917

import base64
import importlib.util
import logging
import platform
import re
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, cast
from unittest.mock import MagicMock, call, mock_open, patch

import pytest
import yaml

import meshtastic.__main__ as main_module
from meshtastic import mt_config
from meshtastic.__main__ import (
    _create_power_meter,
    _normalize_pref_name,
    _parse_host_port,
    _prefix_base64_key,
    _set_missing_flags_false,
    export_config,
    initParser,
    main,
    onConnection,
    onNode,
    onReceive,
    printConfig,
    support_info,
    traverseConfig,
    tunnelMain,
)

# from ..ble_interface import BLEInterface
from ..node import Node

# from ..radioconfig_pb2 import UserPreferences
# import meshtastic.config_pb2
from ..ota import OTAError, OTATransportError
from ..protobuf import config_pb2, localonly_pb2
from ..protobuf.channel_pb2 import Channel  # pylint: disable=E0611
from ..serial_interface import SerialInterface
from ..tcp_interface import TCPInterface

# from ..remote_hardware import onGPIOreceive
# from ..config_pb2 import Config

SDS_DISABLED_SENTINEL: int = 4_294_967_295
MAIN_LOCAL_ADDR: str = cast(str, main_module.__dict__["LOCAL_ADDR"])


def _mock_sendText_helper(
    text: str,
    dest: Any,
    wantAck: bool = False,
    wantResponse: bool = False,
    onResponse: Callable[..., Any] | None = None,
    channelIndex: int = 0,
    portNum: int = 0,
) -> None:
    """Shared helper for mocking sendText; prints parameters to stdout for test assertions.

    Parameters
    ----------
    text : str
        The text message content to send.
    dest : Any
        Destination node ID or address.
    wantAck : bool
        Whether to request acknowledgement. (Default value = False)
    wantResponse : bool
        Whether to request a response. (Default value = False)
    onResponse : Callable[..., Any] | None
        Optional response callback. (Default value = None)
    channelIndex : int
        Channel index to send on. (Default value = 0)
    portNum : int
        Port number for the message. (Default value = 0)
    """
    _ = onResponse  # Mark as intentionally unused
    print("inside mocked sendText")
    print(f"{text} {dest} {wantAck} {wantResponse} {channelIndex} {portNum}")


@pytest.fixture(autouse=True)
def _mock_newer_version_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent external network calls during unit tests in this module.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Pytest monkeypatching fixture.
    """
    monkeypatch.setattr("meshtastic.util.check_if_newer_version", lambda: None)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_init_parser_no_args(capsys: pytest.CaptureFixture[str]) -> None:
    """Test no arguments."""
    sys.argv = [""]
    mt_config.args = sys.argv  # type: ignore[assignment]
    initParser()
    out, err = capsys.readouterr()
    assert out == ""
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_init_parser_version(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --version."""
    sys.argv = ["", "--version"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with pytest.raises(SystemExit) as pytest_wrapped_e:
        initParser()
    assert pytest_wrapped_e.type is SystemExit
    assert pytest_wrapped_e.value.code == 0
    out, err = capsys.readouterr()
    assert re.match(r"[0-9]+\.[0-9]+[\.a][0-9]", out)
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_init_parser_help_mentions_list_fields(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --help mentions dynamic config field discovery."""
    sys.argv = ["", "--help"]
    with pytest.raises(SystemExit) as pytest_wrapped_e:
        initParser()
    assert pytest_wrapped_e.type is SystemExit
    assert pytest_wrapped_e.value.code == 0
    out, err = capsys.readouterr()
    assert "--list-fields" in out
    assert "protobuf schemas" in out
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_main_version(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --version."""
    sys.argv = ["", "--version"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with pytest.raises(SystemExit) as pytest_wrapped_e:
        main()
    assert pytest_wrapped_e.type is SystemExit
    assert pytest_wrapped_e.value.code == 0
    out, err = capsys.readouterr()
    assert re.match(r"[0-9]+\.[0-9]+[\.a][0-9]", out)
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_list_fields_prints_known_fields_and_alias(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --list-fields prints dynamic protobuf fields and compatibility aliases."""
    sys.argv = ["", "--list-fields"]
    main()
    out, err = capsys.readouterr()
    assert "Local config fields:" in out
    assert "bluetooth.enabled" in out
    assert "bluetooth.mode" in out
    assert "bluetooth.fixed_pin" in out
    assert "display.units" in out
    assert "display.use_12h_clock" in out
    assert "display.use_12_hour -> display.use_12h_clock" in out
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_list_fields_includes_all_descriptor_fields(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --list-fields includes every top-level protobuf config field."""
    sys.argv = ["", "--list-fields"]
    main()
    out, err = capsys.readouterr()

    expected: list[str] = []
    for message in (localonly_pb2.LocalConfig(), localonly_pb2.LocalModuleConfig()):
        for section in message.DESCRIPTOR.fields:
            if section.name == "version":
                continue
            if section.message_type is None:
                continue
            for field in section.message_type.fields:
                expected.append(f"{section.name}.{field.name}")

    missing = [field for field in expected if field not in out]
    assert missing == []
    assert err == ""


@pytest.mark.unit
def test_support_info_alias_delegates_to_supportInfo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """support_info should delegate to supportInfo for compatibility."""
    support_info_mock = MagicMock()
    monkeypatch.setattr(main_module, "supportInfo", support_info_mock)

    support_info()

    support_info_mock.assert_called_once_with()


@pytest.mark.unit
def test_normalize_pref_name_display_alias() -> None:
    """Test legacy display field aliases normalize to canonical names."""
    assert _normalize_pref_name("display.use_12_hour") == "display.use_12h_clock"
    assert _normalize_pref_name("display.use12Hour") == "display.use_12h_clock"
    assert _normalize_pref_name("display.use12hClock") == "display.use_12h_clock"
    assert _normalize_pref_name("display.use12HClock") == "display.use_12h_clock"


@pytest.mark.unit
def test_parse_host_port_with_explicit_port() -> None:
    """Test _parse_host_port parses host:port values."""
    hostname, port = _parse_host_port("hostname.example:4403", default_port=4403)
    assert hostname == "hostname.example"
    assert port == 4403


@pytest.mark.unit
def test_parse_host_port_with_bracketed_ipv6_port() -> None:
    """Test _parse_host_port parses bracketed IPv6 addresses with port."""
    hostname, port = _parse_host_port("[2001:db8::1]:4403", default_port=4403)
    assert hostname == "2001:db8::1"
    assert port == 4403


@pytest.mark.unit
def test_parse_host_port_rejects_non_numeric_port() -> None:
    """Test _parse_host_port rejects non-numeric host:port values."""
    with pytest.raises(SystemExit) as exc_info:
        _parse_host_port("hostname.example:notaport", default_port=4403)
    assert exc_info.value.code == 1


@pytest.mark.unit
def test_parse_host_port_rejects_missing_hostname() -> None:
    """Test _parse_host_port rejects host:port values with missing host."""
    with pytest.raises(SystemExit) as exc_info:
        _parse_host_port(":4403", default_port=4403)
    assert exc_info.value.code == 1


@pytest.mark.unit
def test_parse_host_port_rejects_empty_bracketed_ipv6_hostname() -> None:
    """Test _parse_host_port rejects bracketed IPv6 forms with an empty host."""
    with pytest.raises(SystemExit) as exc_info:
        _parse_host_port("[]:4403", default_port=4403)
    assert exc_info.value.code == 1


@pytest.mark.unit
def test_is_local_destination_accepts_hex_node_id_forms() -> None:
    iface = MagicMock()
    iface.myInfo = SimpleNamespace(my_node_num=int("25d6e474", 16))

    assert main_module._is_local_destination(iface, "!25d6e474") is True
    assert main_module._is_local_destination(iface, "0x25D6E474") is True
    assert main_module._is_local_destination(iface, str(int("25d6e474", 16))) is True
    assert main_module._is_local_destination(iface, "!ffffffff") is False


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_host_argument_passes_parsed_port_to_tcp_interface() -> None:
    """Test --host host:port passes parsed host and port to TCPInterface."""
    sys.argv = ["", "--host", "hostname.example:4403", "--set-time", "1"]
    mt_config.args = cast(Any, sys.argv)
    mocked_node = MagicMock()
    iface = MagicMock(autospec=TCPInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.tcp_interface.TCPInterface", return_value=iface) as ctor:
        main()

    mocked_node.setTime.assert_called_once_with(1)
    ctor.assert_called_once()
    args, kwargs = ctor.call_args
    assert args[0] == "hostname.example"
    assert kwargs["portNumber"] == 4403


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_main_no_args(capsys: pytest.CaptureFixture[str]) -> None:
    """Test with no args."""
    sys.argv = [""]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with pytest.raises(SystemExit) as pytest_wrapped_e:
        main()
    assert pytest_wrapped_e.type is SystemExit
    assert pytest_wrapped_e.value.code == 1
    _, err = capsys.readouterr()
    assert re.search(r"usage:", err, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_support(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify that the CLI --support option prints system information and exits with code 0.

    Asserts that stdout contains "System", "Platform", "Machine", and "Executable", and that no stderr was produced.

    """
    sys.argv = ["", "--support"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with pytest.raises(SystemExit) as pytest_wrapped_e:
        main()
    assert pytest_wrapped_e.type is SystemExit
    assert pytest_wrapped_e.value.code == 0
    out, err = capsys.readouterr()
    assert re.search(r"System", out, re.MULTILINE)
    assert re.search(r"Platform", out, re.MULTILINE)
    assert re.search(r"Machine", out, re.MULTILINE)
    assert re.search(r"Executable", out, re.MULTILINE)
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("meshtastic.tcp_interface.TCPInterface", side_effect=OSError("no tcp"))
@patch("meshtastic.util.findPorts", return_value=[])
def test_main_ch_index_no_devices(
    patched_find_ports: Any, _patched_tcp: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify CLI handles --ch-index 1 when no devices are available.

    Asserts that the global channel_index is set to 1, main() exits with SystemExit
    code 1, stderr contains "No Meshtastic device detected", and the port discovery
    function was invoked.

    """
    sys.argv = ["", "--ch-index", "1"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with pytest.raises(SystemExit) as pytest_wrapped_e:
        main()
    assert mt_config.channel_index == 1
    assert pytest_wrapped_e.type is SystemExit
    assert pytest_wrapped_e.value.code == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert re.search(
        r"No Meshtastic device detected and no TCP listener on localhost",
        err,
        re.MULTILINE,
    )
    patched_find_ports.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("meshtastic.util.findPorts", return_value=[])
def test_main_test_no_ports(
    patched_find_ports: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test --test with no hardware."""
    sys.argv = ["", "--test"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with pytest.raises(SystemExit) as pytest_wrapped_e:
        main()
    assert pytest_wrapped_e.type is SystemExit
    assert pytest_wrapped_e.value.code == 1
    patched_find_ports.assert_called()
    _, err = capsys.readouterr()
    # testAll() returns False when not enough ports, CLI reports test failure
    assert re.search(
        r"Test was not successful",
        err,
        re.MULTILINE,
    )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("meshtastic.util.findPorts", return_value=["/dev/ttyFake1"])
def test_main_test_one_port(
    patched_find_ports: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test --test with one fake port."""
    sys.argv = ["", "--test"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with pytest.raises(SystemExit) as pytest_wrapped_e:
        main()
    assert pytest_wrapped_e.type is SystemExit
    assert pytest_wrapped_e.value.code == 1
    patched_find_ports.assert_called()
    _, err = capsys.readouterr()
    # testAll() returns False when not enough ports, CLI reports test failure
    assert re.search(
        r"Test was not successful",
        err,
        re.MULTILINE,
    )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("meshtastic.test.testAll", return_value=True)
def test_main_test_two_ports_success(
    patched_test_all: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test --test two fake ports and testAll() is a simulated success."""
    sys.argv = ["", "--test"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with pytest.raises(SystemExit) as pytest_wrapped_e:
        main()
    assert pytest_wrapped_e.type is SystemExit
    assert pytest_wrapped_e.value.code == 0
    patched_test_all.assert_called()
    out, err = capsys.readouterr()
    assert re.search(r"Test was a success.", out, re.MULTILINE)
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("meshtastic.test.testAll", return_value=False)
def test_main_test_two_ports_fails(
    patched_test_all: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test --test two fake ports and testAll() is a simulated failure."""
    sys.argv = ["", "--test"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with pytest.raises(SystemExit) as pytest_wrapped_e:
        main()
    assert pytest_wrapped_e.type is SystemExit
    assert pytest_wrapped_e.value.code == 1
    patched_test_all.assert_called()
    out, err = capsys.readouterr()
    # Error messages go to stderr
    assert re.search(r"Test was not successful.", err, re.MULTILINE)
    assert out == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_info(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    """Tests that invoking the CLI with `--info` connects to a radio and calls SerialInterface.showInfo.

    Patches SerialInterface with a mock that prints a recognizable marker from showInfo, then
    asserts stdout contains "Connected to radio" and the marker, stderr is empty, and the
    SerialInterface constructor was invoked.

    """
    sys.argv = ["", "--info"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)

    def mock_showInfo() -> None:
        """Print a recognizable marker to stdout used by tests to simulate an interface's showInfo().

        This test helper prints the string "inside mocked showInfo" so tests can detect that the mocked showInfo was invoked.
        """
        print("inside mocked showInfo")

    iface.showInfo.side_effect = mock_showInfo
    with caplog.at_level(logging.DEBUG):
        with patch(
            "meshtastic.serial_interface.SerialInterface", return_value=iface
        ) as mo:
            main()
            out, err = capsys.readouterr()
            assert re.search(r"Connected to radio", out, re.MULTILINE)
            assert re.search(r"inside mocked showInfo", out, re.MULTILINE)
            assert err == ""
            mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("os.getlogin")
def test_main_info_with_permission_error(
    patched_getlogin: Any,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify that invoking the CLI with --info exits with code 1 and prints a permission-related.

    message when the serial interface cannot be opened due to a PermissionError.

    Asserts that a SystemExit with code 1 is raised, the current user lookup was attempted,
    stderr contains guidance matching "Need to add yourself", and stdout is empty.

    """
    sys.argv = ["", "--info"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    patched_getlogin.return_value = "me"

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            with patch(
                "meshtastic.serial_interface.SerialInterface", return_value=iface
            ) as mo:
                mo.side_effect = PermissionError("bla bla")
                main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        patched_getlogin.assert_called()
        # Error messages go to stderr
        assert re.search(r"Need to add yourself", err, re.MULTILINE)
        assert out == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_info_with_tcp_interface(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --info."""
    sys.argv = ["", "--info", "--host", "meshtastic.local"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=TCPInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)

    def mock_showInfo() -> None:
        """Print a recognizable marker to stdout used by tests to simulate an interface's showInfo().

        This test helper prints the string "inside mocked showInfo" so tests can detect that the mocked showInfo was invoked.
        """
        print("inside mocked showInfo")

    iface.showInfo.side_effect = mock_showInfo
    with patch("meshtastic.tcp_interface.TCPInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"inside mocked showInfo", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_no_proto(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --noproto (using --info for output)."""
    sys.argv = ["", "--info", "--noproto"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)

    def mock_showInfo() -> None:
        """Print a recognizable marker to stdout used by tests to simulate an interface's showInfo().

        This test helper prints the string "inside mocked showInfo" so tests can detect that the mocked showInfo was invoked.
        """
        print("inside mocked showInfo")

    iface.showInfo.side_effect = mock_showInfo

    # Override the time.sleep so there is no loop
    def my_sleep(amount: float) -> None:
        """Print sleep duration and terminate to break the no-proto loop in tests."""
        print(f"amount:{amount}")
        sys.exit(0)

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        with patch("time.sleep", side_effect=my_sleep):
            with pytest.raises(SystemExit) as pytest_wrapped_e:
                main()
            assert pytest_wrapped_e.type is SystemExit
            assert pytest_wrapped_e.value.code == 0
            out, err = capsys.readouterr()
            assert re.search(r"Connected to radio", out, re.MULTILINE)
            assert re.search(r"inside mocked showInfo", out, re.MULTILINE)
            assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_info_with_seriallog_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify that running the CLI with --info and --seriallog stdout prints connection and info output.

    Asserts that stdout contains "Connected to radio" and the output produced by showInfo, and that nothing is written to stderr.

    """
    sys.argv = ["", "--info", "--seriallog", "stdout"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)

    def mock_showInfo() -> None:
        """Print a recognizable marker to stdout used by tests to simulate an interface's showInfo().

        This test helper prints the string "inside mocked showInfo" so tests can detect that the mocked showInfo was invoked.
        """
        print("inside mocked showInfo")

    iface.showInfo.side_effect = mock_showInfo
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"inside mocked showInfo", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_info_with_seriallog_output_txt(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Test --info."""
    output_file = tmp_path / "output.txt"
    sys.argv = ["", "--info", "--seriallog", str(output_file)]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    debug_out_stream: list[Any] = [None]

    def _serial_interface_factory(*_args: Any, **kwargs: Any) -> SerialInterface:
        """Capture debugOut argument and return mocked interface."""
        debug_out = kwargs.get("debugOut")
        if debug_out is None:
            debug_out = next(
                (
                    arg
                    for arg in _args
                    if hasattr(arg, "write") and hasattr(arg, "flush")
                ),
                None,
            )
        debug_out_stream[0] = (
            debug_out
            if hasattr(debug_out, "write") and hasattr(debug_out, "flush")
            else None
        )
        return iface

    def mock_showInfo() -> None:
        """Print a recognizable marker to stdout used by tests to simulate an interface's showInfo().

        This test helper prints the string "inside mocked showInfo" so tests can detect that the mocked showInfo was invoked.
        """
        stream = debug_out_stream[0]
        if stream is not None:
            stream.write("inside mocked showInfo\n")
            stream.flush()
        print("inside mocked showInfo")

    iface.showInfo.side_effect = mock_showInfo
    with patch(
        "meshtastic.serial_interface.SerialInterface",
        side_effect=_serial_interface_factory,
    ) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"inside mocked showInfo", out, re.MULTILINE)
        assert output_file.exists()
        assert "inside mocked showInfo" in output_file.read_text(encoding="utf-8")
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_qr(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --qr."""
    sys.argv = ["", "--qr"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    # TODO: could mock/check url
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Primary channel URL", out, re.MULTILINE)
        if importlib.util.find_spec("pyqrcode") is None:
            assert re.search(
                r"Install pyqrcode to view a QR code printed to terminal.",
                out,
                re.MULTILINE,
            )
        else:
            # if a qr code is generated it will have lots of these
            assert re.search(r"\[7m", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_onConnected_exception(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify that running main with --qr exits with code 1 when QR code generation raises an exception.

    Raises
    ------
    Exception
        Raised by the monkeypatched QR-code function to exercise error handling.
    """
    sys.argv = ["", "--qr"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    def throw_an_exception(_junk: Any) -> None:
        """Raise a deterministic exception used by tests.

        Raises
        ------
        Exception
            A generic Exception with the message "Fake exception.".
        """
        raise Exception("Fake exception.")  # pylint: disable=W0719

    pytest.importorskip("pyqrcode")

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        with patch("pyqrcode.create", side_effect=throw_an_exception):
            with pytest.raises(SystemExit) as pytest_wrapped_e:
                main()
            _ = capsys.readouterr()  # consume output to avoid polluting test output
            assert pytest_wrapped_e.type is SystemExit
            assert pytest_wrapped_e.value.code == 1


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_nodes(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify the CLI --nodes option connects to a radio and prints the node list.

    Asserts that the output contains a "Connected to radio" message, that the mocked
    showNodes output is printed, no stderr is produced, and SerialInterface was instantiated.

    """
    sys.argv = ["", "--nodes"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)

    def mock_showNodes(includeSelf: bool, showFields: Any) -> None:
        """Print a test marker indicating a mocked node listing and its options.

        Parameters
        ----------
        includeSelf : bool
            Whether the local node would be included in the listing.
        showFields : Any
            Representation of which node fields would be shown; forwarded verbatim into the printed marker.
        """
        print(f"inside mocked showNodes: {includeSelf} {showFields}")

    iface.showNodes.side_effect = mock_showNodes
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"inside mocked showNodes", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


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
    """Test --set-ham KI123."""
    sys.argv = ["", "--set-ham", "KI123"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)

    def mock_turn_off_encryption_on_primary_channel() -> None:
        """Simulate disabling encryption on the primary channel."""
        print("inside mocked turnOffEncryptionOnPrimaryChannel")

    def mock_setOwner(name: str, is_licensed: bool) -> None:
        """Simulate setOwner and print received parameters."""
        print(f"inside mocked setOwner name:{name} is_licensed:{is_licensed}")

    mocked_node.turnOffEncryptionOnPrimaryChannel.side_effect = (
        mock_turn_off_encryption_on_primary_channel
    )
    mocked_node.setOwner.side_effect = mock_setOwner

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Setting Ham ID to KI123", out, re.MULTILINE)
        assert re.search(r"inside mocked setOwner", out, re.MULTILINE)
        assert re.search(
            r"inside mocked turnOffEncryptionOnPrimaryChannel", out, re.MULTILINE
        )
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_reboot(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --reboot."""
    sys.argv = ["", "--reboot"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)

    def mock_reboot() -> None:
        """Simulate node reboot command."""
        print("inside mocked reboot")

    mocked_node.reboot.side_effect = mock_reboot

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"inside mocked reboot", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_reboot_ota(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --reboot-ota."""
    sys.argv = ["", "--reboot-ota"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)

    def mock_reboot_ota() -> None:
        """Simulate node reboot OTA command."""
        print("inside mocked rebootOTA")

    mocked_node.rebootOTA.side_effect = mock_reboot_ota

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"inside mocked rebootOTA", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize(
    ("args", "method_name", "marker"),
    [
        (["--reboot", "--ack"], "reboot", "inside mocked reboot"),
        (["--reboot-ota", "--ack"], "rebootOTA", "inside mocked rebootOTA"),
        (["--enter-dfu", "--ack"], "enterDFUMode", "inside mocked enterDFU"),
        (["--shutdown", "--ack"], "shutdown", "inside mocked shutdown"),
    ],
)
def test_rebooting_commands_with_ack_skip_wait(
    args: list[str],
    method_name: str,
    marker: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Rebooting commands should skip trailing ACK waits to avoid hangs."""
    sys.argv = ["", *args]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)
    getattr(mocked_node, method_name).side_effect = lambda: print(marker)

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node
    mocked_node.iface = iface

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()
        out, err = capsys.readouterr()
        assert "Connected to radio" in out
        assert marker in out
        assert "Waiting for an acknowledgment from remote node" not in out
        assert err == ""

    iface.waitForAckNak.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_shutdown(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --shutdown."""
    sys.argv = ["", "--shutdown"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)

    def mock_shutdown() -> None:
        """Simulate node shutdown command."""
        print("inside mocked shutdown")

    mocked_node.shutdown.side_effect = mock_shutdown

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"inside mocked shutdown", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


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
    iface.sendText.side_effect = _mock_sendText_helper

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
    iface.sendText.side_effect = _mock_sendText_helper

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
def test_main_sendtext_with_invalid_channel(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test --sendtext."""
    sys.argv = ["", "--sendtext", "hello", "--ch-index", "-1"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.localNode.getChannelByChannelIndex.return_value = None
    iface.localNode.getChannelCopyByChannelIndex.return_value = None

    with caplog.at_level(logging.DEBUG):
        with patch(
            "meshtastic.serial_interface.SerialInterface", return_value=iface
        ) as mo:
            with pytest.raises(SystemExit) as pytest_wrapped_e:
                main()
            assert pytest_wrapped_e.type is SystemExit
            assert pytest_wrapped_e.value.code == 1
            _, err = capsys.readouterr()
            # Error messages go to stderr
            assert re.search(r"is not a valid channel", err, re.MULTILINE)
            mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_sendtext_with_invalid_channel_nine(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test --sendtext."""
    sys.argv = ["", "--sendtext", "hello", "--ch-index", "9"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.localNode.getChannelByChannelIndex.return_value = None
    iface.localNode.getChannelCopyByChannelIndex.return_value = None

    with caplog.at_level(logging.DEBUG):
        with patch(
            "meshtastic.serial_interface.SerialInterface", return_value=iface
        ) as mo:
            with pytest.raises(SystemExit) as pytest_wrapped_e:
                main()
            assert pytest_wrapped_e.type is SystemExit
            assert pytest_wrapped_e.value.code == 1
            _, err = capsys.readouterr()
            # Error messages go to stderr
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

    def mock_removeFixedPosition() -> None:
        """Simulate removing fixed position."""
        print("inside mocked removeFixedPosition")

    mocked_node.removeFixedPosition.side_effect = mock_removeFixedPosition

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
def test_main_setlat(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --setlat."""
    sys.argv = ["", "--setlat", "37.5"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)

    def mock_setFixedPosition(lat: Any, lon: Any, alt: Any) -> None:
        """Simulate setting fixed position and print provided coordinates."""
        print("inside mocked setFixedPosition")
        print(f"{lat} {lon} {alt}")

    mocked_node.setFixedPosition.side_effect = mock_setFixedPosition

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Fixing latitude", out, re.MULTILINE)
        assert re.search(r"Setting device position", out, re.MULTILINE)
        assert re.search(r"inside mocked setFixedPosition", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_setlon(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --setlon."""
    sys.argv = ["", "--setlon", "-122.1"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)

    def mock_setFixedPosition(lat: Any, lon: Any, alt: Any) -> None:
        """Simulate setting fixed position and print provided coordinates."""
        print("inside mocked setFixedPosition")
        print(f"{lat} {lon} {alt}")

    mocked_node.setFixedPosition.side_effect = mock_setFixedPosition

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Fixing longitude", out, re.MULTILINE)
        assert re.search(r"Setting device position", out, re.MULTILINE)
        assert re.search(r"inside mocked setFixedPosition", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_setalt(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --setalt."""
    sys.argv = ["", "--setalt", "51"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)

    def mock_setFixedPosition(lat: Any, lon: Any, alt: Any) -> None:
        """Simulate setting fixed position and print provided coordinates."""
        print("inside mocked setFixedPosition")
        print(f"{lat} {lon} {alt}")

    mocked_node.setFixedPosition.side_effect = mock_setFixedPosition

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Fixing altitude", out, re.MULTILINE)
        assert re.search(r"Setting device position", out, re.MULTILINE)
        assert re.search(r"inside mocked setFixedPosition", out, re.MULTILINE)
        assert err == ""
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
            assert re.search(r"Set network.wifi_ssid to foo", out, re.MULTILINE)
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


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_get_pref_redacts_security_private_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """getPref() should redact secret-bearing security values in field reads."""
    node = SimpleNamespace(
        localConfig=localonly_pb2.LocalConfig(),
        moduleConfig=localonly_pb2.LocalModuleConfig(),
        requestConfig=MagicMock(),
    )
    private_key = bytes(range(32))
    node.localConfig.security.private_key = private_key

    assert main_module.getPref(node, "security.private_key") is True
    out, err = capsys.readouterr()
    assert "security.private_key: <redacted>" in out
    assert base64.b64encode(private_key).decode("utf-8") not in out
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_get_pref_redacts_security_section_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Whole-field getPref() reads should redact secret values in each printed field."""
    node = SimpleNamespace(
        localConfig=localonly_pb2.LocalConfig(),
        moduleConfig=localonly_pb2.LocalModuleConfig(),
        requestConfig=MagicMock(),
    )
    private_key = bytes(range(32))
    public_key = bytes(range(32, 64))
    admin_key = bytes(range(64, 96))
    node.localConfig.security.private_key = private_key
    node.localConfig.security.public_key = public_key
    node.localConfig.security.admin_key.append(admin_key)

    assert main_module.getPref(node, "security") is True
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
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """getPref(allow_secrets=True) should show the actual private key value."""
    node = SimpleNamespace(
        localConfig=localonly_pb2.LocalConfig(),
        moduleConfig=localonly_pb2.LocalModuleConfig(),
        requestConfig=MagicMock(),
    )
    private_key = bytes(range(32))
    node.localConfig.security.private_key = private_key

    with caplog.at_level(logging.DEBUG):
        assert (
            main_module.getPref(node, "security.private_key", allow_secrets=True)
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
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """getPref(allow_secrets=True) whole-field read should show actual key values."""
    node = SimpleNamespace(
        localConfig=localonly_pb2.LocalConfig(),
        moduleConfig=localonly_pb2.LocalModuleConfig(),
        requestConfig=MagicMock(),
    )
    private_key = bytes(range(32))
    public_key = bytes(range(32, 64))
    node.localConfig.security.private_key = private_key
    node.localConfig.security.public_key = public_key

    with caplog.at_level(logging.DEBUG):
        assert main_module.getPref(node, "security", allow_secrets=True) is True
    out, err = capsys.readouterr()
    assert "<redacted>" not in out
    assert base64.b64encode(private_key).decode("utf-8") in out
    assert base64.b64encode(public_key).decode("utf-8") in out
    assert base64.b64encode(private_key).decode("utf-8") not in caplog.text
    assert base64.b64encode(public_key).decode("utf-8") not in caplog.text
    assert err == ""


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
            assert re.search(r"Set network.wifiSsid to foo", out, re.MULTILINE)
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
def test_main_configure_rejects_invalid_enum_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --configure fails fast when enum values are invalid."""
    config_path = tmp_path / "invalid_enum.yaml"
    config_path.write_text(
        yaml.safe_dump({"config": {"bluetooth": {"mode": "NOT_A_MODE"}}}),
        encoding="utf-8",
    )
    iface, target_node = _build_configure_interface()

    with pytest.raises(SystemExit) as excinfo:
        _run_main_configure_file(config_path, iface, monkeypatch)

    out, err = capsys.readouterr()
    assert "does not have an enum called NOT_A_MODE" in out
    assert "Failed to apply config section 'bluetooth'" in err
    assert excinfo.value.code == 1
    target_node.writeConfig.assert_not_called()
    target_node.commitSettingsTransaction.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_rejects_invalid_security_base64(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --configure exits when base64-encoded security keys are malformed."""
    config_path = tmp_path / "invalid_base64.yaml"
    config_path.write_text(
        yaml.safe_dump({"config": {"security": {"privateKey": "base64:A"}}}),
        encoding="utf-8",
    )
    iface, target_node = _build_configure_interface()

    with pytest.raises(SystemExit) as excinfo:
        _run_main_configure_file(config_path, iface, monkeypatch)

    _, err = capsys.readouterr()
    assert "Failed to apply config section 'security'" in err
    assert excinfo.value.code == 1
    target_node.commitSettingsTransaction.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_applies_mixed_case_and_security_encodings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test --configure accepts mixed key casing and supported security key encodings."""
    private_key = bytes(range(32))
    public_key = bytes(range(32, 64))
    admin_key_1 = bytes(range(64, 96))
    admin_key_2 = bytes(range(96, 128))

    config_path = tmp_path / "mixed_case.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "config": {
                    "bluetooth": {
                        "enabled": True,
                        "mode": "NO_PIN",
                        "fixedPin": 777777,
                    },
                    "display": {
                        "units": "IMPERIAL",
                        "use12HClock": True,
                        "screenOnSecs": 66,
                    },
                    "power": {
                        "lsSecs": 222,
                        "waitBluetoothSecs": 77,
                        "minWakeSecs": 11,
                        "sdsSecs": SDS_DISABLED_SENTINEL,
                    },
                    "security": {
                        "privateKey": f"base64:{base64.b64encode(private_key).decode()}",
                        "public_key": "0x" + public_key.hex(),
                        "adminKey": [
                            f"base64:{base64.b64encode(admin_key_1).decode()}",
                            "0x" + admin_key_2.hex(),
                        ],
                    },
                },
                "module_config": {
                    "telemetry": {
                        "deviceUpdateInterval": 321,
                        "environment_display_fahrenheit": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    target_local = localonly_pb2.LocalConfig()
    target_module = localonly_pb2.LocalModuleConfig()
    iface, target_node = _build_configure_interface(target_local, target_module)
    _run_main_configure_file(config_path, iface, monkeypatch)

    assert target_local.bluetooth.enabled is True
    assert target_local.bluetooth.mode == config_pb2.Config.BluetoothConfig.NO_PIN
    assert target_local.bluetooth.fixed_pin == 777777
    assert target_local.display.units == config_pb2.Config.DisplayConfig.IMPERIAL
    assert target_local.display.use_12h_clock is True
    assert target_local.display.screen_on_secs == 66
    assert target_local.power.ls_secs == 222
    assert target_local.power.wait_bluetooth_secs == 77
    assert target_local.power.min_wake_secs == 11
    assert target_local.power.sds_secs == SDS_DISABLED_SENTINEL
    assert target_local.security.private_key == private_key
    assert target_local.security.public_key == public_key
    assert list(target_local.security.admin_key) == [admin_key_1, admin_key_2]
    assert target_module.telemetry.device_update_interval == 321
    assert target_module.telemetry.environment_display_fahrenheit is True

    write_sections = [call.args[0] for call in target_node.writeConfig.call_args_list]
    for required in ("bluetooth", "display", "power", "security", "telemetry"):
        assert required in write_sections


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_applies_power_snake_case_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test --configure applies canonical snake_case power keys directly."""
    config_path = tmp_path / "power-snake-case.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "config": {
                    "power": {
                        "ls_secs": 222,
                        "wait_bluetooth_secs": 77,
                        "min_wake_secs": 11,
                        "sds_secs": SDS_DISABLED_SENTINEL,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    target_local = localonly_pb2.LocalConfig()
    iface, target_node = _build_configure_interface(
        target_local, localonly_pb2.LocalModuleConfig()
    )
    _run_main_configure_file(config_path, iface, monkeypatch)

    assert target_local.power.ls_secs == 222
    assert target_local.power.wait_bluetooth_secs == 77
    assert target_local.power.min_wake_secs == 11
    assert target_local.power.sds_secs == SDS_DISABLED_SENTINEL
    target_node.writeConfig.assert_called_once_with("power")
    target_node.commitSettingsTransaction.assert_called_once_with()
    assert target_node.method_calls.index(call.writeConfig("power")) < (
        target_node.method_calls.index(call.commitSettingsTransaction())
    )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize(
    "alias_key",
    ["use_12_hour", "use12Hour", "use12hClock", "use12HClock"],
)
def test_main_configure_accepts_display_use_12h_alias_spellings(
    alias_key: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test --configure accepts all known alias spellings for display.use_12h_clock."""
    config_path = tmp_path / f"display_alias_{alias_key}.yaml"
    config_path.write_text(
        yaml.safe_dump({"config": {"display": {alias_key: True}}}),
        encoding="utf-8",
    )
    target_local = localonly_pb2.LocalConfig()
    iface, _ = _build_configure_interface(
        target_local, localonly_pb2.LocalModuleConfig()
    )
    _run_main_configure_file(config_path, iface, monkeypatch)
    assert target_local.display.use_12h_clock is True


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_rejects_empty_config_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --configure rejects an empty config mapping."""
    config_path = tmp_path / "empty_config.yaml"
    config_path.write_text(yaml.safe_dump({"config": {}}), encoding="utf-8")
    iface, target_node = _build_configure_interface()

    with pytest.raises(SystemExit) as excinfo:
        _run_main_configure_file(config_path, iface, monkeypatch)

    _, err = capsys.readouterr()
    assert "config" in err
    assert excinfo.value.code == 1
    target_node.beginSettingsTransaction.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_rejects_empty_module_config_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --configure rejects an empty module_config mapping."""
    config_path = tmp_path / "empty_module_config.yaml"
    config_path.write_text(yaml.safe_dump({"module_config": {}}), encoding="utf-8")
    iface, target_node = _build_configure_interface()

    with pytest.raises(SystemExit) as excinfo:
        _run_main_configure_file(config_path, iface, monkeypatch)

    _, err = capsys.readouterr()
    assert "module_config" in err
    assert excinfo.value.code == 1
    target_node.beginSettingsTransaction.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_rejects_non_dict_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --configure rejects a non-dict config value."""
    config_path = tmp_path / "non_dict_config.yaml"
    config_path.write_text(yaml.safe_dump({"config": "invalid"}), encoding="utf-8")
    iface, target_node = _build_configure_interface()

    with pytest.raises(SystemExit) as excinfo:
        _run_main_configure_file(config_path, iface, monkeypatch)

    _, err = capsys.readouterr()
    assert "config" in err
    assert excinfo.value.code == 1
    target_node.beginSettingsTransaction.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_rejects_non_dict_module_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --configure rejects a non-dict module_config value."""
    config_path = tmp_path / "non_dict_module_config.yaml"
    config_path.write_text(yaml.safe_dump({"module_config": 42}), encoding="utf-8")
    iface, target_node = _build_configure_interface()

    with pytest.raises(SystemExit) as excinfo:
        _run_main_configure_file(config_path, iface, monkeypatch)

    _, err = capsys.readouterr()
    assert "module_config" in err
    assert excinfo.value.code == 1
    target_node.beginSettingsTransaction.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize(
    ("top_key", "section_name", "section_value"),
    [
        ("config", "lora", 1),
        ("module_config", "mqtt", 1),
    ],
)
def test_main_configure_rejects_invalid_subsection_payloads(
    top_key: str,
    section_name: str,
    section_value: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --configure rejects non-mapping subsection payloads."""
    config_path = tmp_path / f"invalid_{top_key}_{section_name}.yaml"
    config_path.write_text(
        yaml.safe_dump({top_key: {section_name: section_value}}),
        encoding="utf-8",
    )
    iface, target_node = _build_configure_interface()

    with pytest.raises(SystemExit) as excinfo:
        _run_main_configure_file(config_path, iface, monkeypatch)

    _, err = capsys.readouterr()
    assert f"{top_key}.{section_name}" in err
    assert "non-empty mapping" in err
    assert excinfo.value.code == 1
    target_node.beginSettingsTransaction.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_rejects_malformed_yaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --configure exits cleanly on malformed YAML input."""
    config_path = tmp_path / "malformed_config.yaml"
    config_path.write_text("config:\n  lora: [\n", encoding="utf-8")
    iface, target_node = _build_configure_interface()

    with pytest.raises(SystemExit) as excinfo:
        _run_main_configure_file(config_path, iface, monkeypatch)

    _, err = capsys.readouterr()
    assert "Failed to parse YAML configuration" in err
    assert excinfo.value.code == 1
    target_node.beginSettingsTransaction.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_add_valid(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-add with valid channel name, and that channel name does not already exist."""
    sys.argv = ["", "--ch-add", "testing"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_channel = MagicMock(autospec=Channel)
    # TODO: figure out how to get it to print the channel name instead of MagicMock

    mocked_node = MagicMock(autospec=Node)
    # set it up so we do not already have a channel named this
    mocked_node.getChannelByName.return_value = False
    # set it up so we have free channels
    mocked_node.getDisabledChannel.return_value = mocked_channel

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Writing modified channels to device", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_add_invalid_name_too_long(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-add with invalid channel name, name too long."""
    sys.argv = ["", "--ch-add", "testingtestingtesting"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_channel = MagicMock(autospec=Channel)
    # TODO: figure out how to get it to print the channel name instead of MagicMock

    mocked_node = MagicMock(autospec=Node)
    # set it up so we do not already have a channel named this
    mocked_node.getChannelByName.return_value = False
    # set it up so we have free channels
    mocked_node.getDisabledChannel.return_value = mocked_channel

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        combined = out + err
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(
            r"Warning: Channel name must be shorter", combined, re.MULTILINE
        )
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_add_but_name_already_exists(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --ch-add with a channel name that already exists."""
    sys.argv = ["", "--ch-add", "testing"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)
    # set it up so we do not already have a channel named this
    mocked_node.getChannelByName.return_value = True

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        combined = out + err
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Warning: This node already has", combined, re.MULTILINE)
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_add_but_no_more_channels(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-add with but there are no more channels."""
    sys.argv = ["", "--ch-add", "testing"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)
    # set it up so we do not already have a channel named this
    mocked_node.getChannelByName.return_value = False
    # set it up so we have free channels
    mocked_node.getDisabledChannel.return_value = None

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        combined = out + err
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(
            r"Warning: No free channels were found", combined, re.MULTILINE
        )
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_del(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-del with valid secondary channel to be deleted."""
    sys.argv = ["", "--ch-del", "--ch-index", "1"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Deleting channel", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_del_no_ch_index_specified(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-del without a valid ch-index."""
    sys.argv = ["", "--ch-del"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        combined = out + err
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Warning: Need to specify", combined, re.MULTILINE)
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_del_primary_channel(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-del on ch-index=0."""
    sys.argv = ["", "--ch-del", "--ch-index", "0"]
    mt_config.args = sys.argv  # type: ignore[assignment]
    mt_config.channel_index = 1

    mocked_node = MagicMock(autospec=Node)

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        combined = out + err
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(
            r"Warning: Cannot delete primary channel", combined, re.MULTILINE
        )
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_enable_valid_secondary_channel(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --ch-enable with --ch-index."""
    sys.argv = ["", "--ch-enable", "--ch-index", "1"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Writing modified channels", out, re.MULTILINE)
        assert err == ""
        assert mt_config.channel_index == 1
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_disable_valid_secondary_channel(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --ch-disable with --ch-index."""
    sys.argv = ["", "--ch-disable", "--ch-index", "1"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Writing modified channels", out, re.MULTILINE)
        assert err == ""
        assert mt_config.channel_index == 1
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_enable_without_a_ch_index(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-enable without --ch-index."""
    sys.argv = ["", "--ch-enable"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        combined = out + err
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Warning: Need to specify", combined, re.MULTILINE)
        assert mt_config.channel_index is None
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_enable_primary_channel(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-enable with --ch-index = 0."""
    sys.argv = ["", "--ch-enable", "--ch-index", "0"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        combined = out + err
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(
            r"Warning: Cannot enable/disable PRIMARY", combined, re.MULTILINE
        )
        assert mt_config.channel_index == 0
        mo.assert_called()


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_ch_range_options(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test changing the various range options."""
#    range_options = ['--ch-vlongslow', '--ch-longslow', '--ch-longfast', '--ch-midslow',
#                     '--ch-midfast', '--ch-shortslow', '--ch-shortfast']
#    for range_option in range_options:
#        sys.argv = ['', f"{range_option}" ]
#        mt_config.args = sys.argv  # type: ignore[assignment]
#
#        mocked_node = MagicMock(autospec=Node)
#
#        iface = MagicMock(autospec=SerialInterface)
#        iface.getNode.return_value = mocked_node
#
#        with patch('meshtastic.serial_interface.SerialInterface', return_value=iface) as mo:
#            main()
#            out, err = capsys.readouterr()
#            assert re.search(r'Connected to radio', out, re.MULTILINE)
#            assert re.search(r'Writing modified channels', out, re.MULTILINE)
#            assert err == ''
#            mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_longfast_on_non_primary_channel(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify that invoking the CLI with --ch-longfast and a non-primary.

    --ch-index exits with code 1 and prints a warning that the modem preset
    cannot be set for a non-primary channel while still showing
    "Connected to radio".

    """
    sys.argv = ["", "--ch-longfast", "--ch-index", "1"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        combined = out + err
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(
            r"Warning: Cannot set modem preset for non-primary channel",
            combined,
            re.MULTILINE,
        )
        mo.assert_called()


# PositionFlags:
# Misc info that might be helpful (this info will grow stale, just
# a snapshot of the values.) The radioconfig_pb2.PositionFlags.Name and bit values are:
# POS_UNDEFINED 0
# POS_ALTITUDE 1
# POS_ALT_MSL 2
# POS_GEO_SEP 4
# POS_DOP 8
# POS_HVDOP 16
# POS_BATTERY 32
# POS_SATINVIEW 64
# POS_SEQ_NOS 128
# POS_TIMESTAMP 256

# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_pos_fields_no_args(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test --pos-fields no args (which shows settings)"""
#    sys.argv = ['', '--pos-fields']
#    mt_config.args = sys.argv  # type: ignore[assignment]
#
#    pos_flags = MagicMock(autospec=meshtastic.radioconfig_pb2.PositionFlags)
#
#    with patch('meshtastic.serial_interface.SerialInterface') as mo:
#        mo().getNode().radioConfig.preferences.position_flags = 35
#        with patch('meshtastic.radioconfig_pb2.PositionFlags', return_value=pos_flags) as mrc:
#
#            mrc.values.return_value = [0, 1, 2, 4, 8, 16, 32, 64, 128, 256]
#            # Note: When you use side_effect and a list, each call will use a value from the front of the list then
#            # remove that value from the list. If there are three values in the list, we expect it to be called
#            # three times.
#            mrc.Name.side_effect = ['POS_ALTITUDE', 'POS_ALT_MSL', 'POS_BATTERY']
#
#            main()
#
#            mrc.Name.assert_called()
#            mrc.values.assert_called()
#            mo.assert_called()
#
#            out, err = capsys.readouterr()
#            assert re.search(r'Connected to radio', out, re.MULTILINE)
#            assert re.search(r'POS_ALTITUDE POS_ALT_MSL POS_BATTERY', out, re.MULTILINE)
#            assert err == ''


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_pos_fields_arg_of_zero(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test --pos-fields an arg of 0 (which shows list)"""
#    sys.argv = ['', '--pos-fields', '0']
#    mt_config.args = sys.argv  # type: ignore[assignment]
#
#    pos_flags = MagicMock(autospec=meshtastic.radioconfig_pb2.PositionFlags)
#
#    with patch('meshtastic.serial_interface.SerialInterface') as mo:
#        with patch('meshtastic.radioconfig_pb2.PositionFlags', return_value=pos_flags) as mrc:
#
#            def throw_value_error_exception(exc):
#                raise ValueError()
#            mrc.Value.side_effect = throw_value_error_exception
#            mrc.keys.return_value = [ 'POS_UNDEFINED', 'POS_ALTITUDE', 'POS_ALT_MSL',
#                                      'POS_GEO_SEP', 'POS_DOP', 'POS_HVDOP', 'POS_BATTERY',
#                                      'POS_SATINVIEW', 'POS_SEQ_NOS', 'POS_TIMESTAMP']
#
#            main()
#
#            mrc.Value.assert_called()
#            mrc.keys.assert_called()
#            mo.assert_called()
#
#            out, err = capsys.readouterr()
#            assert re.search(r'Connected to radio', out, re.MULTILINE)
#            assert re.search(r'ERROR: supported position fields are:', out, re.MULTILINE)
#            assert re.search(r"['POS_UNDEFINED', 'POS_ALTITUDE', 'POS_ALT_MSL', 'POS_GEO_SEP',"\
#                              "'POS_DOP', 'POS_HVDOP', 'POS_BATTERY', 'POS_SATINVIEW', 'POS_SEQ_NOS',"\
#                              "'POS_TIMESTAMP']", out, re.MULTILINE)
#            assert err == ''


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_pos_fields_valid_values(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test --pos-fields with valid values"""
#    sys.argv = ['', '--pos-fields', 'POS_GEO_SEP', 'POS_ALT_MSL']
#    mt_config.args = sys.argv  # type: ignore[assignment]
#
#    pos_flags = MagicMock(autospec=meshtastic.radioconfig_pb2.PositionFlags)
#
#    with patch('meshtastic.serial_interface.SerialInterface') as mo:
#        with patch('meshtastic.radioconfig_pb2.PositionFlags', return_value=pos_flags) as mrc:
#
#            mrc.Value.side_effect = [ 4, 2 ]
#
#            main()
#
#            mrc.Value.assert_called()
#            mo.assert_called()
#
#            out, err = capsys.readouterr()
#            assert re.search(r'Connected to radio', out, re.MULTILINE)
#            assert re.search(r'Setting position fields to 6', out, re.MULTILINE)
#            assert re.search(r'Set position_flags to 6', out, re.MULTILINE)
#            assert re.search(r'Writing modified preferences to device', out, re.MULTILINE)
#            assert err == ''


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_get_with_valid_values(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test --get with valid values (with string, number, boolean)"""
#    sys.argv = ['', '--get', 'ls_secs', '--get', 'wifi_ssid', '--get', 'fixed_position']
#    mt_config.args = sys.argv  # type: ignore[assignment]
#
#    with patch('meshtastic.serial_interface.SerialInterface') as mo:
#
#        mo().getNode().radioConfig.preferences.wifi_ssid = 'foo'
#        mo().getNode().radioConfig.preferences.ls_secs = 300
#        mo().getNode().radioConfig.preferences.fixed_position = False
#
#        main()
#
#        mo.assert_called()
#
#        out, err = capsys.readouterr()
#        assert re.search(r'Connected to radio', out, re.MULTILINE)
#        assert re.search(r'ls_secs: 300', out, re.MULTILINE)
#        assert re.search(r'wifi_ssid: foo', out, re.MULTILINE)
#        assert re.search(r'fixed_position: False', out, re.MULTILINE)
#        assert err == ''


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_get_with_valid_values_camel(capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture) -> None:
#    """Test --get with valid values (with string, number, boolean)"""
#    sys.argv = ["", "--get", "lsSecs", "--get", "wifiSsid", "--get", "fixedPosition"]
#    mt_config.args = sys.argv  # type: ignore[assignment]
#    mt_config.camel_case = True
#
#    with caplog.at_level(logging.DEBUG):
#        with patch("meshtastic.serial_interface.SerialInterface") as mo:
#            mo().getNode().radioConfig.preferences.wifi_ssid = "foo"
#            mo().getNode().radioConfig.preferences.ls_secs = 300
#            mo().getNode().radioConfig.preferences.fixed_position = False
#
#            main()
#
#            mo.assert_called()
#
#            out, err = capsys.readouterr()
#            assert re.search(r"Connected to radio", out, re.MULTILINE)
#            assert re.search(r"lsSecs: 300", out, re.MULTILINE)
#            assert re.search(r"wifiSsid: foo", out, re.MULTILINE)
#            assert re.search(r"fixedPosition: False", out, re.MULTILINE)
#            assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_get_with_invalid(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --get with invalid field."""
    sys.argv = ["", "--get", "foo"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_user_prefs = MagicMock()
    mocked_user_prefs.DESCRIPTOR.fields_by_name.get.return_value = None

    mocked_node = MagicMock(autospec=Node)
    mocked_node.localConfig = mocked_user_prefs
    mocked_node.moduleConfig = mocked_user_prefs

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"do not have an attribute foo", out, re.MULTILINE)
        assert re.search(r"Choices are...", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_onReceive_empty(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test onReceive with empty packet - should handle gracefully without error."""
    args = MagicMock()
    mt_config.args = args
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    # Need 'decoded' to be truthy so the code path reaches packet.get("to")
    packet: dict[str, Any] = {"decoded": {}}
    with caplog.at_level(logging.DEBUG):
        onReceive(packet, iface)
    assert re.search(r"in onReceive", caplog.text, re.MULTILINE)
    out, err = capsys.readouterr()
    # Should not print any warnings - packet.get("to") returns None gracefully
    assert out == ""
    assert err == ""


#    TODO: use this captured position app message (might want/need in the future)
#    packet = {
#            'to': 4294967295,
#            'decoded': {
#                'portnum': 'POSITION_APP',
#                'payload': "M69\306a"
#                },
#            'id': 334776976,
#            'hop_limit': 3
#            }


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_onReceive_with_sendtext(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test onReceive with sendtext.

    The entire point of this test is to make sure the interface.close() call
    is made in onReceive().

    """
    sys.argv = ["", "--sendtext", "hello"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    # Note: 'TEXT_MESSAGE_APP' value is 1
    packet = {
        "to": 4294967295,
        "decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": "hello"},
        "id": 334776977,
        "hop_limit": 3,
        "want_ack": True,
    }

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.myInfo.my_node_num = 4294967295

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        with caplog.at_level(logging.DEBUG):
            main()
            onReceive(packet, iface)
        assert re.search(r"in onReceive", caplog.text, re.MULTILINE)
        mo.assert_called()
        out, err = capsys.readouterr()
        assert re.search(r"Sending text message hello to", out, re.MULTILINE)
        assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_onReceive_with_text(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test onReceive with text."""
    args = MagicMock()
    args.sendtext.return_value = "foo"
    args.reply = True
    args.ch_index = None
    mt_config.args = args

    # Note: 'TEXT_MESSAGE_APP' value is 1
    # Note: Some of this is faked below.
    packet = {
        "to": 4294967295,
        "decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": "hello", "text": "faked"},
        "id": 334776977,
        "hop_limit": 3,
        "want_ack": True,
        "rxSnr": 6.0,
        "hopLimit": 3,
        "raw": "faked",
        "fromId": "!28b5465c",
        "toId": "^all",
    }

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.myInfo.my_node_num = 4294967295

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        with caplog.at_level(logging.DEBUG):
            onReceive(packet, iface)
        assert re.search(r"in onReceive", caplog.text, re.MULTILINE)
        out, err = capsys.readouterr()
        assert re.search(r"Sending reply", out, re.MULTILINE)
        assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_onReceive_reply_uses_rx_channel(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reply is sent on the same channel the message was received on."""
    args = MagicMock()
    args.sendtext.return_value = ""
    args.reply = True
    args.ch_index = None
    mt_config.args = args

    packet = {
        "to": 4294967295,
        "from": 999,
        "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hello"},
        "channel": 3,
        "rxSnr": 6.0,
        "hopLimit": 3,
    }

    iface = MagicMock(autospec=SerialInterface)
    iface.myInfo.my_node_num = 4294967295

    onReceive(packet, iface)

    iface.sendText.assert_called_once()
    call_kwargs = iface.sendText.call_args
    assert call_kwargs[1].get("channelIndex") == 3 or (
        len(call_kwargs[0]) > 1 and call_kwargs[0][1] == 3
    )
    out, err = capsys.readouterr()
    assert "Received channel 3" in out
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_onReceive_ch_index_filter_mismatch(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With --ch-index set, messages on a different channel are ignored."""
    args = MagicMock()
    args.sendtext.return_value = ""
    args.reply = True
    args.ch_index = 1
    mt_config.args = args

    packet = {
        "to": 4294967295,
        "from": 999,
        "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hello"},
        "channel": 5,
        "rxSnr": 6.0,
        "hopLimit": 3,
    }

    iface = MagicMock(autospec=SerialInterface)
    iface.myInfo.my_node_num = 4294967295

    onReceive(packet, iface)

    iface.sendText.assert_not_called()
    out, err = capsys.readouterr()
    assert "Ignored message on channel 5" in out
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_onReceive_own_packet_no_reply(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Messages from our own node number are not replied to (prevent loop)."""
    args = MagicMock()
    args.sendtext.return_value = ""
    args.reply = True
    args.ch_index = None
    mt_config.args = args

    my_node = 4294967295
    packet = {
        "to": 4294967295,
        "from": my_node,
        "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "my own msg"},
        "channel": 0,
        "rxSnr": 6.0,
        "hopLimit": 3,
    }

    iface = MagicMock(autospec=SerialInterface)
    iface.myInfo.my_node_num = my_node

    onReceive(packet, iface)

    iface.sendText.assert_not_called()
    out, err = capsys.readouterr()
    assert "Sending reply" not in out
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_onReceive_auto_reply_echo_no_reply(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Auto-reply echoes (starting with 'got msg ') are not replied to (prevent loop)."""
    args = MagicMock()
    args.sendtext.return_value = ""
    args.reply = True
    args.ch_index = None
    mt_config.args = args

    packet = {
        "to": 4294967295,
        "from": 999,
        "decoded": {
            "portnum": "TEXT_MESSAGE_APP",
            "text": "got msg 'hello' with rxSnr: 6.0 and hopLimit: 3",
        },
        "channel": 0,
        "rxSnr": 6.0,
        "hopLimit": 3,
    }

    iface = MagicMock(autospec=SerialInterface)
    iface.myInfo.my_node_num = 4294967295

    onReceive(packet, iface)

    iface.sendText.assert_not_called()
    out, err = capsys.readouterr()
    assert "Sending reply" not in out
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_onConnection(capsys: pytest.CaptureFixture[str]) -> None:
    """Test onConnection."""
    sys.argv = [""]
    mt_config.args = sys.argv  # type: ignore[assignment]
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)

    class TempTopic:
        """temp class for topic."""

        def getName(self) -> str:
            """Get a fake topic name.

            Returns
            -------
            str
                The fixed fake topic name `'foo'`.
            """
            return "foo"

    mytopic = TempTopic()
    onConnection(iface, mytopic)
    out, err = capsys.readouterr()
    assert re.search(r"Connection changed: foo", out, re.MULTILINE)
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_onConnection_with_non_topic(capsys: pytest.CaptureFixture[str]) -> None:
    """Test onConnection with non-topic objects."""
    sys.argv = [""]
    mt_config.args = sys.argv  # type: ignore[assignment]
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    onConnection(iface, topic="raw-topic")
    out, err = capsys.readouterr()
    assert re.search(r"Connection changed: raw-topic", out, re.MULTILINE)
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_export_config(capsys: pytest.CaptureFixture[str]) -> None:
    """Test export_config() function directly."""
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        mo.getLongName.return_value = "foo"
        mo.getShortName.return_value = "oof"
        mo.localNode.getURL.return_value = "bar"
        mo.getCannedMessage.return_value = "foo|bar"
        mo.getRingtone.return_value = "24:d=32,o=5"
        mo.getMyNodeInfo().get.return_value = {
            "latitudeI": 1100000000,
            "longitudeI": 1200000000,
            "altitude": 100,
            "batteryLevel": 34,
            "latitude": 110.0,
            "longitude": 120.0,
        }
        mo.localNode.radioConfig.preferences = """phone_timeout_secs: 900
ls_secs: 300
position_broadcast_smart: true
fixed_position: true
position_flags: 35"""
        export_config(mo)
    out = export_config(mo)
    _, err = capsys.readouterr()

    # ensure we do not output this line
    assert not re.search(r"Connected to radio", out, re.MULTILINE)

    assert re.search(r"owner: foo", out, re.MULTILINE)
    assert re.search(r"owner_short: oof", out, re.MULTILINE)
    assert re.search(r"channel_url: bar", out, re.MULTILINE)
    assert re.search(r"location:", out, re.MULTILINE)
    assert re.search(r"lat: 110.0", out, re.MULTILINE)
    assert re.search(r"lon: 120.0", out, re.MULTILINE)
    assert re.search(r"alt: 100", out, re.MULTILINE)
    # TODO: rework above config to test the following
    # assert re.search(r"user_prefs:", out, re.MULTILINE)
    # assert re.search(r"phone_timeout_secs: 900", out, re.MULTILINE)
    # assert re.search(r"ls_secs: 300", out, re.MULTILINE)
    # assert re.search(r"position_broadcast_smart: 'true'", out, re.MULTILINE)
    # assert re.search(r"fixed_position: 'true'", out, re.MULTILINE)
    # assert re.search(r"position_flags: 35", out, re.MULTILINE)
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_export_config_omits_empty_optional_fields() -> None:
    """Test export_config omits optional top-level fields when values are empty/missing."""
    iface = _build_export_interface(
        localonly_pb2.LocalConfig(), localonly_pb2.LocalModuleConfig()
    )
    iface.getLongName.return_value = ""
    iface.getShortName.return_value = ""
    iface.localNode.getURL.return_value = ""
    iface.getCannedMessage.return_value = ""
    iface.getRingtone.return_value = ""
    iface.getMyNodeInfo.return_value = {}

    exported = yaml.safe_load(export_config(iface))

    assert "owner" not in exported
    assert "owner_short" not in exported
    assert "channel_url" not in exported
    assert "canned_messages" not in exported
    assert "ringtone" not in exported
    assert "location" not in exported


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_export_config_sets_missing_true_default_flags_false() -> None:
    """Test export_config explicitly writes known true-default flags as false when missing."""
    source_local = localonly_pb2.LocalConfig()
    source_module = localonly_pb2.LocalModuleConfig()
    source_local.display.units = config_pb2.Config.DisplayConfig.IMPERIAL
    source_module.telemetry.device_update_interval = 1

    exported = yaml.safe_load(
        export_config(_build_export_interface(source_local, source_module))
    )
    config = exported["config"]
    module_config = exported["module_config"]

    assert config["bluetooth"]["enabled"] is False
    assert config["lora"]["sx126xRxBoostedGain"] is False
    assert config["lora"]["txEnabled"] is False
    assert config["lora"]["usePreset"] is False
    assert config["position"]["positionBroadcastSmartEnabled"] is False
    assert config["security"]["serialEnabled"] is False
    assert module_config["mqtt"]["encryptionEnabled"] is False


def _build_export_interface(
    local_config: localonly_pb2.LocalConfig,
    module_config: localonly_pb2.LocalModuleConfig,
) -> MagicMock:
    """Build a minimal interface mock compatible with export_config().

    Parameters
    ----------
    local_config : localonly_pb2.LocalConfig
        Local device configuration to attach to the mocked interface.
    module_config : localonly_pb2.LocalModuleConfig
        Module configuration to attach to the mocked interface.

    Returns
    -------
    MagicMock
        A MagicMock instance wired up with localConfig, moduleConfig, and helper return values.
    """
    iface = MagicMock(autospec=SerialInterface)
    iface.localNode = MagicMock()
    iface.localNode.localConfig = local_config
    iface.localNode.moduleConfig = module_config
    iface.localNode.getURL.return_value = "https://meshtastic.org/e/#Cgo"
    iface.getLongName.return_value = "Roundtrip Node"
    iface.getShortName.return_value = "RT"
    iface.getMyNodeInfo.return_value = {}
    iface.getCannedMessage.return_value = ""
    iface.getRingtone.return_value = ""
    return iface


def _build_configure_interface(
    target_local: localonly_pb2.LocalConfig | None = None,
    target_module: localonly_pb2.LocalModuleConfig | None = None,
) -> tuple[MagicMock, MagicMock]:
    """Build a minimal interface mock compatible with --configure operations.

    Parameters
    ----------
    target_local : localonly_pb2.LocalConfig | None
        LocalConfig message to mutate during configure; when None, a fresh message is created.
    target_module : localonly_pb2.LocalModuleConfig | None
        LocalModuleConfig message to mutate during configure; when None, a fresh message is created.

    Returns
    -------
    tuple[MagicMock, MagicMock]
        Tuple of ``(iface, target_node)`` where ``iface`` is a SerialInterface-like mock
        and ``target_node`` is the node mock returned by ``iface.getNode()``.
    """
    if target_local is None:
        target_local = localonly_pb2.LocalConfig()
    if target_module is None:
        target_module = localonly_pb2.LocalModuleConfig()

    device_local = localonly_pb2.LocalConfig()
    device_local.CopyFrom(target_local)
    device_module = localonly_pb2.LocalModuleConfig()
    device_module.CopyFrom(target_module)

    target_node = MagicMock()
    target_node.localConfig = target_local
    target_node.moduleConfig = target_module
    target_node.beginSettingsTransaction = MagicMock()
    target_node.commitSettingsTransaction = MagicMock()
    target_node.setOwner = MagicMock()
    target_node.setURL = MagicMock()
    target_node.set_canned_message = MagicMock()
    target_node.set_ringtone = MagicMock()
    target_node.channels = []
    target_node.partialChannels = []
    target_node.requestChannels = MagicMock()

    def _write_config_side_effect(config_name: str) -> None:
        local_field = target_local.DESCRIPTOR.fields_by_name.get(config_name)
        if local_field is not None:
            device_local.ClearField(config_name)  # type: ignore[arg-type]
            if target_local.HasField(config_name):  # type: ignore[arg-type]
                getattr(device_local, config_name).CopyFrom(
                    getattr(target_local, config_name)
                )
            return
        module_field = target_module.DESCRIPTOR.fields_by_name.get(config_name)
        if module_field is not None:
            device_module.ClearField(config_name)  # type: ignore[arg-type]
            if target_module.HasField(config_name):  # type: ignore[arg-type]
                getattr(device_module, config_name).CopyFrom(
                    getattr(target_module, config_name)
                )

    target_node.writeConfig = MagicMock(side_effect=_write_config_side_effect)

    def _request_config_side_effect(config_type: object, *_args: object) -> None:
        field_name = getattr(config_type, "name", None)
        containing_type = getattr(config_type, "containing_type", None)
        containing_name = getattr(containing_type, "name", None)
        if not isinstance(field_name, str):
            return
        if containing_name == "LocalConfig":
            target_local.ClearField(field_name)  # type: ignore[arg-type]
            if device_local.HasField(field_name):  # type: ignore[arg-type]
                getattr(target_local, field_name).CopyFrom(
                    getattr(device_local, field_name)
                )
            return
        if containing_name == "LocalModuleConfig":
            target_module.ClearField(field_name)  # type: ignore[arg-type]
            if device_module.HasField(field_name):  # type: ignore[arg-type]
                getattr(target_module, field_name).CopyFrom(
                    getattr(device_module, field_name)
                )

    target_node.requestConfig = MagicMock(side_effect=_request_config_side_effect)
    target_node.setFixedPosition = MagicMock()

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = target_node
    iface.localNode = target_node
    return iface, target_node


def _run_main_configure_file(
    config_path: Path,
    iface: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run main() for --configure against a supplied YAML file and interface mock.

    Parameters
    ----------
    config_path : Path
        Path to the YAML configuration file consumed by ``--configure``.
    iface : MagicMock
        Mocked SerialInterface object returned by the patched constructor.
    monkeypatch : pytest.MonkeyPatch
        Fixture used to patch ``time.sleep`` for deterministic tests.
    """
    monkeypatch.setattr("time.sleep", lambda _: None)
    sys.argv = ["", "--configure", str(config_path)]
    mt_config.args = cast(Any, sys.argv)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_export_config_configure_round_trip_security_keys(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ensure export->configure->export preserves security keys and structure."""
    source_local = localonly_pb2.LocalConfig()
    source_module = localonly_pb2.LocalModuleConfig()
    source_local.bluetooth.enabled = True
    source_local.security.serial_enabled = True
    source_local.security.private_key = b"\x01" * 32
    source_local.security.public_key = b"\x02" * 32
    source_local.security.admin_key.extend([b"\x03" * 32, b"\x04" * 32])
    source_module.mqtt.address = "mqtt.meshtastic.org"

    exported_yaml = export_config(_build_export_interface(source_local, source_module))
    exported = yaml.safe_load(exported_yaml)
    security = exported["config"]["security"]
    assert security["privateKey"].startswith("base64:")
    assert security["publicKey"].startswith("base64:")
    assert all(
        isinstance(item, str) and item.startswith("base64:")
        for item in security["adminKey"]
    )
    assert "base64:base64:" not in security["privateKey"]
    assert "base64:base64:" not in security["publicKey"]

    restored_local = localonly_pb2.LocalConfig()
    restored_module = localonly_pb2.LocalModuleConfig()
    for section, values in exported["config"].items():
        traverseConfig(section, values, restored_local)
    for section, values in exported["module_config"].items():
        traverseConfig(section, values, restored_module)

    assert restored_local.security.private_key == source_local.security.private_key
    assert restored_local.security.public_key == source_local.security.public_key
    assert list(restored_local.security.admin_key) == list(
        source_local.security.admin_key
    )

    exported_round_trip = yaml.safe_load(
        export_config(_build_export_interface(restored_local, restored_module))
    )
    assert exported_round_trip == exported
    _, err = capsys.readouterr()
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_export_config_and_configure_round_trip_nonstandard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Round-trip --export-config/--configure with nonstandard fully-configured settings."""
    source_local = localonly_pb2.LocalConfig()
    source_module = localonly_pb2.LocalModuleConfig()

    source_local.bluetooth.enabled = True
    source_local.bluetooth.mode = config_pb2.Config.BluetoothConfig.NO_PIN
    source_local.bluetooth.fixed_pin = 654321
    source_local.display.units = config_pb2.Config.DisplayConfig.IMPERIAL
    source_local.display.use_12h_clock = True
    source_local.display.screen_on_secs = 45
    source_local.power.ls_secs = 111
    source_local.security.serial_enabled = True
    source_local.security.private_key = b"\xaa" * 32
    source_local.security.public_key = b"\xbb" * 32
    source_local.security.admin_key.extend([b"\xcc" * 32, b"\xdd" * 32])

    source_module.telemetry.device_update_interval = 321
    source_module.telemetry.environment_display_fahrenheit = True
    source_module.remote_hardware.enabled = True

    export_iface = _build_export_interface(source_local, source_module)
    export_iface.__enter__ = MagicMock(return_value=export_iface)
    export_iface.__exit__ = MagicMock(return_value=None)
    export_iface.getCannedMessage.return_value = "Alpha|Bravo|Charlie"
    export_iface.getRingtone.return_value = "24:d=16,o=5,b=100:c"
    export_iface.localNode.getURL.return_value = "https://meshtastic.org/e/#CgYSAQABAA"
    export_iface.getMyNodeInfo.return_value = {
        "position": {"latitude": 12.345, "longitude": -98.765, "altitude": 432}
    }

    export_path = tmp_path / "roundtrip_config.yaml"
    sys.argv = ["", "--export-config", str(export_path)]
    mt_config.args = cast(Any, sys.argv)
    with patch(
        "meshtastic.serial_interface.SerialInterface", return_value=export_iface
    ):
        main()

    exported = yaml.safe_load(export_path.read_text(encoding="utf-8"))
    assert exported["owner"] == "Roundtrip Node"
    assert exported["owner_short"] == "RT"
    assert exported["channel_url"] == "https://meshtastic.org/e/#CgYSAQABAA"
    assert exported["canned_messages"] == "Alpha|Bravo|Charlie"
    assert exported["ringtone"] == "24:d=16,o=5,b=100:c"
    assert exported["location"] == {"lat": 12.345, "lon": -98.765, "alt": 432}
    bluetooth_cfg = exported["config"]["bluetooth"]
    display_cfg = exported["config"]["display"]
    power_cfg = exported["config"]["power"]
    telemetry_cfg = exported["module_config"]["telemetry"]
    assert bluetooth_cfg["mode"] == "NO_PIN"
    assert bluetooth_cfg.get("fixed_pin", bluetooth_cfg.get("fixedPin")) == 654321
    assert display_cfg["units"] == "IMPERIAL"
    assert display_cfg.get("use_12h_clock", display_cfg.get("use12hClock")) is True
    assert power_cfg.get("ls_secs", power_cfg.get("lsSecs")) == 111
    assert exported["config"]["security"]["privateKey"].startswith("base64:")
    assert exported["config"]["security"]["publicKey"].startswith("base64:")
    assert all(
        isinstance(v, str) and v.startswith("base64:")
        for v in exported["config"]["security"]["adminKey"]
    )
    assert (
        telemetry_cfg.get(
            "device_update_interval", telemetry_cfg.get("deviceUpdateInterval")
        )
        == 321
    )
    assert (
        telemetry_cfg.get(
            "environment_display_fahrenheit",
            telemetry_cfg.get("environmentDisplayFahrenheit"),
        )
        is True
    )
    assert exported["module_config"]["remote_hardware"]["enabled"] is True

    target_local = localonly_pb2.LocalConfig()
    target_module = localonly_pb2.LocalModuleConfig()
    device_local = localonly_pb2.LocalConfig()
    device_module = localonly_pb2.LocalModuleConfig()
    target_node = MagicMock()
    target_node.localConfig = target_local
    target_node.moduleConfig = target_module
    target_node.beginSettingsTransaction = MagicMock()
    target_node.commitSettingsTransaction = MagicMock()
    target_node.setOwner = MagicMock()
    target_node.setURL = MagicMock()
    target_node.set_canned_message = MagicMock()
    target_node.set_ringtone = MagicMock()
    target_node.channels = []
    target_node.partialChannels = []
    target_node.requestChannels = MagicMock()

    def _write_config_side_effect(config_name: str) -> None:
        local_field = target_local.DESCRIPTOR.fields_by_name.get(config_name)
        if local_field is not None:
            device_local.ClearField(config_name)  # type: ignore[arg-type]
            if target_local.HasField(config_name):  # type: ignore[arg-type]
                getattr(device_local, config_name).CopyFrom(
                    getattr(target_local, config_name)
                )
            return
        module_field = target_module.DESCRIPTOR.fields_by_name.get(config_name)
        if module_field is not None:
            device_module.ClearField(config_name)  # type: ignore[arg-type]
            if target_module.HasField(config_name):  # type: ignore[arg-type]
                getattr(device_module, config_name).CopyFrom(
                    getattr(target_module, config_name)
                )

    target_node.writeConfig = MagicMock(side_effect=_write_config_side_effect)

    def _request_config_side_effect(config_type: object, *_args: object) -> None:
        field_name = getattr(config_type, "name", None)
        containing_type = getattr(config_type, "containing_type", None)
        containing_name = getattr(containing_type, "name", None)
        if not isinstance(field_name, str):
            return
        if containing_name == "LocalConfig":
            target_local.ClearField(field_name)  # type: ignore[arg-type]
            if device_local.HasField(field_name):  # type: ignore[arg-type]
                getattr(target_local, field_name).CopyFrom(
                    getattr(device_local, field_name)
                )
            return
        if containing_name == "LocalModuleConfig":
            target_module.ClearField(field_name)  # type: ignore[arg-type]
            if device_module.HasField(field_name):  # type: ignore[arg-type]
                getattr(target_module, field_name).CopyFrom(
                    getattr(device_module, field_name)
                )

    target_node.requestConfig = MagicMock(side_effect=_request_config_side_effect)
    target_node.getURL = MagicMock(return_value="https://meshtastic.org/e/#CgYSAQABAA")
    target_node.setFixedPosition = MagicMock()

    configure_iface = MagicMock(autospec=SerialInterface)
    configure_iface.__enter__ = MagicMock(return_value=configure_iface)
    configure_iface.__exit__ = MagicMock(return_value=None)
    configure_iface.getNode.return_value = target_node
    configure_iface.localNode = target_node

    monkeypatch.setattr("time.sleep", lambda _: None)
    _patch_fast_monotonic(monkeypatch)
    monkeypatch.setattr(
        "meshtastic.__main__._post_seturl_stability_check",
        lambda *a, **k: True,
    )
    configure_iface.waitForConfig = MagicMock()
    sys.argv = ["", "--configure", str(export_path)]
    mt_config.args = cast(Any, sys.argv)
    with patch(
        "meshtastic.serial_interface.SerialInterface", return_value=configure_iface
    ):
        main()

    target_node.beginSettingsTransaction.assert_called_once()
    target_node.commitSettingsTransaction.assert_called_once()
    assert target_node.setOwner.call_count == 2
    target_node.setURL.assert_called_once_with("https://meshtastic.org/e/#CgYSAQABAA")
    target_node.set_canned_message.assert_called_once_with("Alpha|Bravo|Charlie")
    target_node.set_ringtone.assert_called_once_with("24:d=16,o=5,b=100:c")
    target_node.setFixedPosition.assert_called_once_with(12.345, -98.765, 432)

    assert target_local.bluetooth.enabled is True
    assert target_local.bluetooth.mode == config_pb2.Config.BluetoothConfig.NO_PIN
    assert target_local.bluetooth.fixed_pin == 654321
    assert target_local.display.units == config_pb2.Config.DisplayConfig.IMPERIAL
    assert target_local.display.use_12h_clock is True
    assert target_local.display.screen_on_secs == 45
    assert target_local.power.ls_secs == 111
    assert target_local.security.serial_enabled is True
    assert target_local.security.private_key == source_local.security.private_key
    assert target_local.security.public_key == source_local.security.public_key
    assert list(target_local.security.admin_key) == list(
        source_local.security.admin_key
    )

    assert target_module.telemetry.device_update_interval == 321
    assert target_module.telemetry.environment_display_fahrenheit is True
    assert target_module.remote_hardware.enabled is True

    write_sections = [c.args[0] for c in target_node.writeConfig.call_args_list]
    for required in (
        "bluetooth",
        "display",
        "power",
        "security",
        "telemetry",
        "remote_hardware",
    ):
        assert required in write_sections

    out, err = capsys.readouterr()
    assert re.search(r"Exported configuration to", out, re.MULTILINE)
    assert re.search(r"Configuration transaction committed", out, re.MULTILINE)
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_export_config_round_trip_with_camel_case_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test export->traverse round trip when mt_config.camel_case is enabled."""
    source_local = localonly_pb2.LocalConfig()
    source_module = localonly_pb2.LocalModuleConfig()
    source_local.display.use_12h_clock = True
    source_local.power.ls_secs = 123
    source_local.security.serial_enabled = True
    source_module.telemetry.device_update_interval = 77

    monkeypatch.setattr(mt_config, "camel_case", True)
    exported = yaml.safe_load(
        export_config(_build_export_interface(source_local, source_module))
    )

    assert "channelUrl" in exported
    assert exported["config"]["display"]["use12hClock"] is True
    assert exported["config"]["power"]["lsSecs"] == 123
    assert exported["config"]["security"]["serialEnabled"] is True
    assert exported["module_config"]["telemetry"]["deviceUpdateInterval"] == 77

    restored_local = localonly_pb2.LocalConfig()
    restored_module = localonly_pb2.LocalModuleConfig()
    for section, values in exported["config"].items():
        assert traverseConfig(section, values, restored_local)
    for section, values in exported["module_config"].items():
        assert traverseConfig(section, values, restored_module)

    assert restored_local.display.use_12h_clock is True
    assert restored_local.power.ls_secs == 123
    assert restored_local.security.serial_enabled is True
    assert restored_module.telemetry.device_update_interval == 77


@pytest.mark.unit
def test_prefix_base64_key_skips_existing_prefixes() -> None:
    """Ensure _prefix_base64_key does not double-prefix already-normalized values."""
    security = {
        "privateKey": "base64:abc123==",
        "adminKey": ["base64:def456==", "ghi789==", 7],
    }
    normalized_key_map = {
        "privateKey": "privateKey",
        "adminKey": "adminKey",
    }
    _prefix_base64_key(security, normalized_key_map, "privateKey")
    _prefix_base64_key(security, normalized_key_map, "adminKey")

    assert security["privateKey"] == "base64:abc123=="
    assert security["adminKey"] == ["base64:def456==", "base64:ghi789==", 7]


# TODO
# recursion depth exceeded error
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_export_config_use_camel(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test export_config() function directly"""
#    mt_config.camel_case = True
#    iface = MagicMock(autospec=SerialInterface)
#    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
#        mo.getLongName.return_value = "foo"
#        mo.localNode.getURL.return_value = "bar"
#        mo.getMyNodeInfo().get.return_value = {
#            "latitudeI": 1100000000,
#            "longitudeI": 1200000000,
#            "altitude": 100,
#            "batteryLevel": 34,
#            "latitude": 110.0,
#            "longitude": 120.0,
#        }
#        mo.localNode.radioConfig.preferences = """phone_timeout_secs: 900
# ls_secs: 300
# position_broadcast_smart: true
# fixed_position: true
# position_flags: 35"""
#        export_config(mo)
#    out, err = capsys.readouterr()
#
#    # ensure we do not output this line
#    assert not re.search(r"Connected to radio", out, re.MULTILINE)
#
#    assert re.search(r"owner: foo", out, re.MULTILINE)
#    assert re.search(r"channelUrl: bar", out, re.MULTILINE)
#    assert re.search(r"location:", out, re.MULTILINE)
#    assert re.search(r"lat: 110.0", out, re.MULTILINE)
#    assert re.search(r"lon: 120.0", out, re.MULTILINE)
#    assert re.search(r"alt: 100", out, re.MULTILINE)
#    assert re.search(r"userPrefs:", out, re.MULTILINE)
#    assert re.search(r"phoneTimeoutSecs: 900", out, re.MULTILINE)
#    assert re.search(r"lsSecs: 300", out, re.MULTILINE)
#    # TODO: should True be capitalized here?
#    assert re.search(r"positionBroadcastSmart: 'True'", out, re.MULTILINE)
#    assert re.search(r"fixedPosition: 'True'", out, re.MULTILINE)
#    assert re.search(r"positionFlags: 35", out, re.MULTILINE)
#    assert err == ""


# TODO
# maximum recursion depth error
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_export_config_called_from_main(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test --export-config"""
#    sys.argv = ["", "--export-config"]
#    mt_config.args = sys.argv  # type: ignore[assignment]
#
#    iface = MagicMock(autospec=SerialInterface)
#    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
#        main()
#        out, err = capsys.readouterr()
#        assert not re.search(r"Connected to radio", out, re.MULTILINE)
#        assert re.search(r"# start of Meshtastic configure yaml", out, re.MULTILINE)
#        assert err == ""
#        mo.assert_called()


@pytest.mark.unit
def test_set_missing_flags_false() -> None:
    """Test _set_missing_flags_false() function."""
    config = {"bluetooth": {"enabled": True}, "lora": {"txEnabled": True}}

    false_defaults: set[tuple[str, ...]] = {
        ("bluetooth", "enabled"),
        ("lora", "sx126xRxBoostedGain"),
        ("lora", "txEnabled"),
        ("lora", "usePreset"),
        ("position", "positionBroadcastSmartEnabled"),
        ("security", "serialEnabled"),
        ("mqtt", "encryptionEnabled"),
    }

    _set_missing_flags_false(config, false_defaults)

    # Preserved
    assert config["bluetooth"]["enabled"] is True
    assert config["lora"]["txEnabled"] is True

    # Added
    assert config["lora"]["usePreset"] is False
    assert config["lora"]["sx126xRxBoostedGain"] is False
    assert config["position"]["positionBroadcastSmartEnabled"] is False
    assert config["security"]["serialEnabled"] is False
    assert config["mqtt"]["encryptionEnabled"] is False


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_gpio_rd_no_gpio_channel(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --gpio_rd with no named gpio channel."""
    sys.argv = ["", "--gpio-rd", "0x10", "--dest", "!foo"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.localNode.getChannelByName.return_value = None
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        # Error messages go to stderr, stdout contains "Connected to radio"
        assert re.search(r"No channel named 'gpio'", err)
        assert "Connected to radio" in out


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_gpio_rd_no_dest(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --gpio_rd with a named gpio channel but no dest was specified."""
    sys.argv = ["", "--gpio-rd", "0x2000"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    channel = Channel(index=2, role=Channel.Role.SECONDARY)
    channel.settings.psk = b"\x8a\x94y\x0e\xc6\xc9\x1e5\x91\x12@\xa60\xa8\xb43\x87\x00\xf2K\x0e\xe7\x7fAz\xcd\xf5\xb0\x900\xa84"
    channel.settings.name = "gpio"

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.localNode.getChannelByName.return_value = channel
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        combined = out + err
        assert re.search(r"Warning: Must use a destination node ID", combined)


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# @patch('time.sleep')
# def test_main_gpio_rd(caplog, capsys):
#    """Test --gpio_rd with a named gpio channel"""
#    # Note: On the Heltec v2.1, there is a GPIO pin GPIO 13 that does not have a
#    # red arrow (meaning ok to use for our purposes)
#    # See https://resource.heltec.cn/download/WiFi_LoRa_32/WIFI_LoRa_32_V2.pdf
#    # To find out the mask for GPIO 13, let us assign n as 13.
#    # 1. Find the 2^n or 2^13 (8192)
#    # 2. Convert 8192 decimal to hex (0x2000)
#    # You can use python:
#    # >>> print(hex(2**13))
#    # 0x2000
#    sys.argv = ['', '--gpio-rd', '0x1000', '--dest', '!1234']
#    mt_config.args = sys.argv  # type: ignore[assignment]
#
#    channel = Channel(index=1, role=1)
#    channel.settings.modem_config = 3
#    channel.settings.psk = b'\x01'
#
#    packet = {
#
#            'from': 682968668,
#            'to': 682968612,
#            'channel': 1,
#            'decoded': {
#                'portnum': 'REMOTE_HARDWARE_APP',
#                'payload': b'\x08\x05\x18\x80 ',
#                'requestId': 1629980484,
#                'remotehw': {
#                    'typ': 'READ_GPIOS_REPLY',
#                    'gpioValue': '4096',
#                    'raw': 'faked',
#                    'id': 1693085229,
#                    'rxTime': 1640294262,
#                    'rxSnr': 4.75,
#                    'hopLimit': 3,
#                    'wantAck': True,
#                    }
#                }
#            }
#
#    iface = MagicMock(autospec=SerialInterface)
#    iface.localNode.getChannelByName.return_value = channel
#    with patch('meshtastic.serial_interface.SerialInterface', return_value=iface) as mo:
#        with caplog.at_level(logging.DEBUG):
#            main()
#            onGPIOreceive(packet, mo)
#    out, err = capsys.readouterr()
#    assert re.search(r'Connected to radio', out, re.MULTILINE)
#    assert re.search(r'Reading GPIO mask 0x1000 ', out, re.MULTILINE)
#    assert re.search(r'Received RemoteHardware typ=READ_GPIOS_REPLY, gpio_value=4096', out, re.MULTILINE)
#    assert err == ''


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# @patch('time.sleep')
# def test_main_gpio_rd_with_no_gpioMask(caplog, capsys):
#    """Test --gpio_rd with a named gpio channel"""
#    sys.argv = ['', '--gpio-rd', '0x1000', '--dest', '!1234']
#    mt_config.args = sys.argv  # type: ignore[assignment]
#
#    channel = Channel(index=1, role=1)
#    channel.settings.modem_config = 3
#    channel.settings.psk = b'\x01'
#
#    # Note: Intentionally do not have gpioValue in response as that is the
#    # default value
#    packet = {
#            'from': 682968668,
#            'to': 682968612,
#            'channel': 1,
#            'decoded': {
#                'portnum': 'REMOTE_HARDWARE_APP',
#                'payload': b'\x08\x05\x18\x80 ',
#                'requestId': 1629980484,
#                'remotehw': {
#                    'typ': 'READ_GPIOS_REPLY',
#                    'raw': 'faked',
#                    'id': 1693085229,
#                    'rxTime': 1640294262,
#                    'rxSnr': 4.75,
#                    'hopLimit': 3,
#                    'wantAck': True,
#                    }
#                }
#            }
#
#    iface = MagicMock(autospec=SerialInterface)
#    iface.localNode.getChannelByName.return_value = channel
#    with patch('meshtastic.serial_interface.SerialInterface', return_value=iface) as mo:
#        with caplog.at_level(logging.DEBUG):
#            main()
#            onGPIOreceive(packet, mo)
#    out, err = capsys.readouterr()
#    assert re.search(r'Connected to radio', out, re.MULTILINE)
#    assert re.search(r'Reading GPIO mask 0x1000 ', out, re.MULTILINE)
#    assert re.search(r'Received RemoteHardware typ=READ_GPIOS_REPLY, gpio_value=0', out, re.MULTILINE)
#    assert err == ''


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_gpio_watch(caplog, capsys):
#    """Test --gpio_watch with a named gpio channel"""
#    sys.argv = ['', '--gpio-watch', '0x1000', '--dest', '!1234']
#    mt_config.args = sys.argv  # type: ignore[assignment]
#
#    def my_sleep(amount: float) -> None:
#        print(f'{amount}')
#        sys.exit(3)
#
#    channel = Channel(index=1, role=1)
#    channel.settings.modem_config = 3
#    channel.settings.psk = b'\x01'
#
#    packet = {
#
#            'from': 682968668,
#            'to': 682968612,
#            'channel': 1,
#            'decoded': {
#                'portnum': 'REMOTE_HARDWARE_APP',
#                'payload': b'\x08\x05\x18\x80 ',
#                'requestId': 1629980484,
#                'remotehw': {
#                    'typ': 'READ_GPIOS_REPLY',
#                    'gpioValue': '4096',
#                    'raw': 'faked',
#                    'id': 1693085229,
#                    'rxTime': 1640294262,
#                    'rxSnr': 4.75,
#                    'hopLimit': 3,
#                    'wantAck': True,
#                    }
#                }
#            }
#
#    with patch('time.sleep', side_effect=my_sleep):
#        with pytest.raises(SystemExit) as pytest_wrapped_e:
#            iface = MagicMock(autospec=SerialInterface)
#            iface.localNode.getChannelByName.return_value = channel
#            with patch('meshtastic.serial_interface.SerialInterface', return_value=iface) as mo:
#                with caplog.at_level(logging.DEBUG):
#                    main()
#                    onGPIOreceive(packet, mo)
#        assert pytest_wrapped_e.type is SystemExit
#        assert pytest_wrapped_e.value.code == 3
#        out, err = capsys.readouterr()
#        assert re.search(r'Connected to radio', out, re.MULTILINE)
#        assert re.search(r'Watching GPIO mask 0x1000 ', out, re.MULTILINE)
#        assert err == ''


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_gpio_wrb(caplog, capsys):
#    """Test --gpio_wrb with a named gpio channel"""
#    sys.argv = ['', '--gpio-wrb', '4', '1', '--dest', '!1234']
#    mt_config.args = sys.argv  # type: ignore[assignment]
#
#    channel = Channel(index=1, role=1)
#    channel.settings.modem_config = 3
#    channel.settings.psk = b'\x01'
#
#    packet = {
#
#            'from': 682968668,
#            'to': 682968612,
#            'channel': 1,
#            'decoded': {
#                'portnum': 'REMOTE_HARDWARE_APP',
#                'payload': b'\x08\x05\x18\x80 ',
#                'requestId': 1629980484,
#                'remotehw': {
#                    'typ': 'READ_GPIOS_REPLY',
#                    'gpioValue': '16',
#                    'raw': 'faked',
#                    'id': 1693085229,
#                    'rxTime': 1640294262,
#                    'rxSnr': 4.75,
#                    'hopLimit': 3,
#                    'wantAck': True,
#                    }
#                }
#            }
#
#
#    iface = MagicMock(autospec=SerialInterface)
#    iface.localNode.getChannelByName.return_value = channel
#    with patch('meshtastic.serial_interface.SerialInterface', return_value=iface) as mo:
#        with caplog.at_level(logging.DEBUG):
#            main()
#            onGPIOreceive(packet, mo)
#    out, err = capsys.readouterr()
#    assert re.search(r'Connected to radio', out, re.MULTILINE)
#    assert re.search(r'Writing GPIO mask 0x10 with value 0x10 to !1234', out, re.MULTILINE)
#    assert re.search(r'Received RemoteHardware typ=READ_GPIOS_REPLY, gpio_value=16 value=0', out, re.MULTILINE)
#    assert err == ''


# TODO
# need to restructure these for nested configs
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_getPref_valid_field(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test getPref() with a valid field"""
#    prefs = MagicMock()
#    prefs.DESCRIPTOR.fields_by_name.get.return_value = "ls_secs"
#    prefs.wifi_ssid = "foo"
#    prefs.ls_secs = 300
#    prefs.fixed_position = False
#
#    getPref(prefs, "ls_secs")
#    out, err = capsys.readouterr()
#    assert re.search(r"ls_secs: 300", out, re.MULTILINE)
#    assert err == ""
#
#
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_getPref_valid_field_camel(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test getPref() with a valid field"""
#    mt_config.camel_case = True
#    prefs = MagicMock()
#    prefs.DESCRIPTOR.fields_by_name.get.return_value = "ls_secs"
#    prefs.wifi_ssid = "foo"
#    prefs.ls_secs = 300
#    prefs.fixed_position = False
#
#    getPref(prefs, "ls_secs")
#    out, err = capsys.readouterr()
#    assert re.search(r"lsSecs: 300", out, re.MULTILINE)
#    assert err == ""
#
#
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_getPref_valid_field_string(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test getPref() with a valid field and value as a string"""
#    prefs = MagicMock()
#    prefs.DESCRIPTOR.fields_by_name.get.return_value = "wifi_ssid"
#    prefs.wifi_ssid = "foo"
#    prefs.ls_secs = 300
#    prefs.fixed_position = False
#
#    getPref(prefs, "wifi_ssid")
#    out, err = capsys.readouterr()
#    assert re.search(r"wifi_ssid: foo", out, re.MULTILINE)
#    assert err == ""
#
#
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_getPref_valid_field_string_camel(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test getPref() with a valid field and value as a string"""
#    mt_config.camel_case = True
#    prefs = MagicMock()
#    prefs.DESCRIPTOR.fields_by_name.get.return_value = "wifi_ssid"
#    prefs.wifi_ssid = "foo"
#    prefs.ls_secs = 300
#    prefs.fixed_position = False
#
#    getPref(prefs, "wifi_ssid")
#    out, err = capsys.readouterr()
#    assert re.search(r"wifiSsid: foo", out, re.MULTILINE)
#    assert err == ""
#
#
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_getPref_valid_field_bool(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test getPref() with a valid field and value as a bool"""
#    prefs = MagicMock()
#    prefs.DESCRIPTOR.fields_by_name.get.return_value = "fixed_position"
#    prefs.wifi_ssid = "foo"
#    prefs.ls_secs = 300
#    prefs.fixed_position = False
#
#    getPref(prefs, "fixed_position")
#    out, err = capsys.readouterr()
#    assert re.search(r"fixed_position: False", out, re.MULTILINE)
#    assert err == ""
#
#
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_getPref_valid_field_bool_camel(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test getPref() with a valid field and value as a bool"""
#    mt_config.camel_case = True
#    prefs = MagicMock()
#    prefs.DESCRIPTOR.fields_by_name.get.return_value = "fixed_position"
#    prefs.wifi_ssid = "foo"
#    prefs.ls_secs = 300
#    prefs.fixed_position = False
#
#    getPref(prefs, "fixed_position")
#    out, err = capsys.readouterr()
#    assert re.search(r"fixedPosition: False", out, re.MULTILINE)
#    assert err == ""
#
#
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_getPref_invalid_field(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test getPref() with an invalid field"""
#
#    class Field:
#        """Simple class for testing."""
#
#        def __init__(self, name):
#            """constructor"""
#            self.name = name
#
#    prefs = MagicMock()
#    prefs.DESCRIPTOR.fields_by_name.get.return_value = None
#
#    # Note: This is a subset of the real fields
#    ls_secs_field = Field("ls_secs")
#    is_router = Field("is_router")
#    fixed_position = Field("fixed_position")
#
#    fields = [ls_secs_field, is_router, fixed_position]
#    prefs.DESCRIPTOR.fields = fields
#
#    getPref(prefs, "foo")
#
#    out, err = capsys.readouterr()
#    assert re.search(r"does not have an attribute called foo", out, re.MULTILINE)
#    # ensure they are sorted
#    assert re.search(r"fixed_position\s+is_router\s+ls_secs", out, re.MULTILINE)
#    assert err == ""
#
#
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_getPref_invalid_field_camel(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test getPref() with an invalid field"""
#    mt_config.camel_case = True
#
#    class Field:
#        """Simple class for testing."""
#
#        def __init__(self, name):
#            """constructor"""
#            self.name = name
#
#    prefs = MagicMock()
#    prefs.DESCRIPTOR.fields_by_name.get.return_value = None
#
#    # Note: This is a subset of the real fields
#    ls_secs_field = Field("ls_secs")
#    is_router = Field("is_router")
#    fixed_position = Field("fixed_position")
#
#    fields = [ls_secs_field, is_router, fixed_position]
#    prefs.DESCRIPTOR.fields = fields
#
#    getPref(prefs, "foo")
#
#    out, err = capsys.readouterr()
#    assert re.search(r"does not have an attribute called foo", out, re.MULTILINE)
#    # ensure they are sorted
#    assert re.search(r"fixedPosition\s+isRouter\s+lsSecs", out, re.MULTILINE)
#    assert err == ""
#
#
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_setPref_valid_field_int_as_string(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test setPref() with a valid field"""
#
#    class Field:
#        """Simple class for testing."""
#
#        def __init__(self, name, enum_type):
#            """constructor"""
#            self.name = name
#            self.enum_type = enum_type
#
#    ls_secs_field = Field("ls_secs", "int")
#    prefs = MagicMock()
#    prefs.DESCRIPTOR.fields_by_name.get.return_value = ls_secs_field
#
#    setPref(prefs, "ls_secs", "300")
#    out, err = capsys.readouterr()
#    assert re.search(r"Set ls_secs to 300", out, re.MULTILINE)
#    assert err == ""


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_setPref_valid_field_invalid_enum(capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture) -> None:
#    """Test setPref() with a valid field but invalid enum value"""
#
#    radioConfig = RadioConfig()
#    prefs = radioConfig.preferences
#
#    with caplog.at_level(logging.DEBUG):
#        setPref(prefs, 'charge_current', 'foo')
#        out, err = capsys.readouterr()
#        assert re.search(r'charge_current does not have an enum called foo', out, re.MULTILINE)
#        assert re.search(r'Choices in sorted order are', out, re.MULTILINE)
#        assert re.search(r'MA100', out, re.MULTILINE)
#        assert re.search(r'MA280', out, re.MULTILINE)
#        assert err == ''


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_setPref_valid_field_invalid_enum_where_enums_are_camel_cased_values(
#    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
# ) -> None:
#    """Test setPref() with a valid field but invalid enum value"""
#
#    radioConfig = RadioConfig()
#    prefs = radioConfig.preferences
#
#    with caplog.at_level(logging.DEBUG):
#        setPref(prefs, 'region', 'foo')
#        out, err = capsys.readouterr()
#        assert re.search(r'region does not have an enum called foo', out, re.MULTILINE)
#        assert re.search(r'Choices in sorted order are', out, re.MULTILINE)
#        assert re.search(r'ANZ', out, re.MULTILINE)
#        assert re.search(r'CN', out, re.MULTILINE)
#        assert err == ''


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_setPref_valid_field_invalid_enum_camel(capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture) -> None:
#    """Test setPref() with a valid field but invalid enum value"""
#    mt_config.camel_case = True
#
#    radioConfig = RadioConfig()
#    prefs = radioConfig.preferences
#
#    with caplog.at_level(logging.DEBUG):
#        setPref(prefs, 'charge_current', 'foo')
#        out, err = capsys.readouterr()
#        assert re.search(r'chargeCurrent does not have an enum called foo', out, re.MULTILINE)
#        assert err == ''


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_setPref_valid_field_valid_enum(capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture) -> None:
#    """Test setPref() with a valid field and valid enum value"""
#
#    # charge_current
#    # some valid values:   MA100 MA1000 MA1080
#
#    radioConfig = RadioConfig()
#    prefs = radioConfig.preferences
#
#    with caplog.at_level(logging.DEBUG):
#        setPref(prefs, 'charge_current', 'MA100')
#        out, err = capsys.readouterr()
#        assert re.search(r'Set charge_current to MA100', out, re.MULTILINE)
#        assert err == ''


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_setPref_valid_field_valid_enum_camel(capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture) -> None:
#    """Test setPref() with a valid field and valid enum value"""
#    mt_config.camel_case = True
#
#    # charge_current
#    # some valid values:   MA100 MA1000 MA1080
#
#    radioConfig = RadioConfig()
#    prefs = radioConfig.preferences
#
#    with caplog.at_level(logging.DEBUG):
#        setPref(prefs, 'charge_current', 'MA100')
#        out, err = capsys.readouterr()
#        assert re.search(r'Set chargeCurrent to MA100', out, re.MULTILINE)
#        assert err == ''

# TODO
# need to update for nested configs
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_setPref_invalid_field(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test setPref() with a invalid field"""
#
#    class Field:
#        """Simple class for testing."""
#
#        def __init__(self, name):
#            """constructor"""
#            self.name = name
#
#    prefs = MagicMock()
#    prefs.DESCRIPTOR.fields_by_name.get.return_value = None
#
#    # Note: This is a subset of the real fields
#    ls_secs_field = Field("ls_secs")
#    is_router = Field("is_router")
#    fixed_position = Field("fixed_position")
#
#    fields = [ls_secs_field, is_router, fixed_position]
#    prefs.DESCRIPTOR.fields = fields
#
#    setPref(prefs, "foo", "300")
#    out, err = capsys.readouterr()
#    assert re.search(r"does not have an attribute called foo", out, re.MULTILINE)
#    # ensure they are sorted
#    assert re.search(r"fixed_position\s+is_router\s+ls_secs", out, re.MULTILINE)
#    assert err == ""
#
#
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_setPref_invalid_field_camel(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test setPref() with a invalid field"""
#    mt_config.camel_case = True
#
#    class Field:
#        """Simple class for testing."""
#
#        def __init__(self, name):
#            """constructor"""
#            self.name = name
#
#    prefs = MagicMock()
#    prefs.DESCRIPTOR.fields_by_name.get.return_value = None
#
#    # Note: This is a subset of the real fields
#    ls_secs_field = Field("ls_secs")
#    is_router = Field("is_router")
#    fixed_position = Field("fixed_position")
#
#    fields = [ls_secs_field, is_router, fixed_position]
#    prefs.DESCRIPTOR.fields = fields
#
#    setPref(prefs, "foo", "300")
#    out, err = capsys.readouterr()
#    assert re.search(r"does not have an attribute called foo", out, re.MULTILINE)
#    # ensure they are sorted
#    assert re.search(r"fixedPosition\s+isRouter\s+lsSecs", out, re.MULTILINE)
#    assert err == ""
#
#
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_setPref_ignore_incoming_123(capsys):
#    """Test setPref() with ignore_incoming"""
#
#    class Field:
#        """Simple class for testing."""
#
#        def __init__(self, name, enum_type):
#            """constructor"""
#            self.name = name
#            self.enum_type = enum_type
#
#    ignore_incoming_field = Field("ignore_incoming", "list")
#    prefs = MagicMock()
#    prefs.DESCRIPTOR.fields_by_name.get.return_value = ignore_incoming_field
#
#    setPref(prefs, "ignore_incoming", "123")
#    out, err = capsys.readouterr()
#    assert re.search(r"Adding '123' to the ignore_incoming list", out, re.MULTILINE)
#    assert re.search(r"Set ignore_incoming to 123", out, re.MULTILINE)
#    assert err == ""
#
#
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_setPref_ignore_incoming_0(capsys):
#    """Test setPref() with ignore_incoming"""
#
#    class Field:
#        """Simple class for testing."""
#
#        def __init__(self, name, enum_type):
#            """constructor"""
#            self.name = name
#            self.enum_type = enum_type
#
#    ignore_incoming_field = Field("ignore_incoming", "list")
#    prefs = MagicMock()
#    prefs.DESCRIPTOR.fields_by_name.get.return_value = ignore_incoming_field
#
#    setPref(prefs, "ignore_incoming", "0")
#    out, err = capsys.readouterr()
#    assert re.search(r"Clearing ignore_incoming list", out, re.MULTILINE)
#    assert re.search(r"Set ignore_incoming to 0", out, re.MULTILINE)
#    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_set_psk_no_ch_index(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify that invoking the CLI with `--ch-set psk` but without a `--ch-index` prints a warning and exits with code 1.

    Asserts that the tool reports a successful connection, emits a warning that `--ch-index` must
    be specified, produces no stderr output, and raises SystemExit with code 1.

    """
    sys.argv = ["", "--ch-set", "psk", "foo", "--host", "meshtastic.local"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=TCPInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.tcp_interface.TCPInterface", return_value=iface) as mo:
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        out, err = capsys.readouterr()
        combined = out + err
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(
            r"Warning: Need to specify '--ch-index'", combined, re.MULTILINE
        )
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_set_psk_with_ch_index(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-set psk."""
    sys.argv = [
        "",
        "--ch-set",
        "psk",
        "none",
        "--host",
        "meshtastic.local",
        "--ch-index",
        "0",
    ]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=TCPInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.tcp_interface.TCPInterface", return_value=iface) as mo:
        main()
    out, err = capsys.readouterr()
    assert re.search(r"Connected to radio", out, re.MULTILINE)
    assert re.search(r"Writing modified channels to device", out, re.MULTILINE)
    assert err == ""
    mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize(
    "psk_value",
    [
        pytest.param(
            "HR8D2KziD3IfvpHlwHAfCAh4JP/I7dsHwKdVllfKoD0=",
            id="base64_raw",
        ),
        pytest.param(
            "base64:HR8D2KziD3IfvpHlwHAfCAh4JP/I7dsHwKdVllfKoD0=",
            id="base64_prefix",
        ),
        pytest.param(
            "0x1a1a",
            id="hex",
        ),
    ],
)
def test_main_ch_set_psk_with_supported_encodings(
    psk_value: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --ch-set psk with raw base64, base64: prefix, and hex encodings."""
    sys.argv = [
        "",
        "--ch-set",
        "psk",
        psk_value,
        "--host",
        "meshtastic.local",
        "--ch-index",
        "1",
    ]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=TCPInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.tcp_interface.TCPInterface", return_value=iface) as mo:
        main()
    out, err = capsys.readouterr()
    assert re.search(r"Connected to radio", out, re.MULTILINE)
    assert re.search(r"Writing modified channels to device", out, re.MULTILINE)
    assert err == ""
    mo.assert_called()


# TODO
# doesn't work properly with nested/module config stuff
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_ch_set_name_with_ch_index(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test --ch-set setting other than psk"""
#    sys.argv = [
#        "",
#        "--ch-set",
#        "name",
#        "foo",
#        "--host",
#        "meshtastic.local",
#        "--ch-index",
#        "0",
#    ]
#    mt_config.args = sys.argv  # type: ignore[assignment]
#
#    iface = MagicMock(autospec=TCPInterface)
#    with patch("meshtastic.tcp_interface.TCPInterface", return_value=iface) as mo:
#        main()
#    out, err = capsys.readouterr()
#    assert re.search(r"Connected to radio", out, re.MULTILINE)
#    assert re.search(r"Set name to foo", out, re.MULTILINE)
#    assert re.search(r"Writing modified channels to device", out, re.MULTILINE)
#    assert err == ""
#    mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_onNode(capsys: pytest.CaptureFixture[str]) -> None:
    """Test onNode."""
    onNode("foo")
    out, err = capsys.readouterr()
    assert re.search(r"Node changed", out, re.MULTILINE)
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_tunnel_no_args(capsys: pytest.CaptureFixture[str]) -> None:
    """Test tunnel no arguments."""
    sys.argv = [""]
    mt_config.args = sys.argv  # type: ignore[assignment]
    with pytest.raises(SystemExit) as pytest_wrapped_e:
        tunnelMain()
    assert pytest_wrapped_e.type is SystemExit
    assert pytest_wrapped_e.value.code == 1
    _, err = capsys.readouterr()
    assert re.search(r"usage: ", err, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("meshtastic.util.findPorts", return_value=[])
@patch("platform.system")
def test_tunnel_tunnel_arg_with_no_devices(
    mock_platform_system: Any,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test tunnel with tunnel arg (act like we are on a linux system)."""
    a_mock = MagicMock()
    a_mock.return_value = "Linux"
    mock_platform_system.side_effect = a_mock
    sys.argv = ["", "--tunnel"]
    mt_config.args = sys.argv  # type: ignore[assignment]
    print(f"platform.system():{platform.system()}")
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            tunnelMain()
        mock_platform_system.assert_called()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        _out, err = capsys.readouterr()
        assert re.search(
            r"No Meshtastic device detected and no TCP listener on localhost",
            err,
            re.MULTILINE,
        )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("meshtastic.util.findPorts", return_value=[])
@patch("platform.system")
def test_tunnel_subnet_arg_with_no_devices(
    mock_platform_system: Any,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test tunnel with subnet arg (act like we are on a linux system)."""
    a_mock = MagicMock()
    a_mock.return_value = "Linux"
    mock_platform_system.side_effect = a_mock
    sys.argv = ["", "--subnet", "foo"]
    mt_config.args = sys.argv  # type: ignore[assignment]
    print(f"platform.system():{platform.system()}")
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            tunnelMain()
        mock_platform_system.assert_called()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        _out, err = capsys.readouterr()
        assert re.search(
            r"No Meshtastic device detected and no TCP listener on localhost",
            err,
            re.MULTILINE,
        )


@pytest.mark.skipif(sys.platform == "win32", reason="on windows is no fcntl module")
@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("platform.system")
@patch("meshtastic.serial_interface.SerialInterface._clear_hupcl_on_fd")
@patch("meshtastic.serial_interface.SerialInterface._set_hupcl_with_termios")
@patch("builtins.open", new_callable=mock_open, read_data="data")
@patch("serial.Serial")
@patch("meshtastic.util.findPorts", return_value=["/dev/ttyUSBfake"])
def test_tunnel_tunnel_arg(
    _mocked_findPorts: Any,
    _mocked_serial: Any,
    _mocked_open: Any,
    _mock_hupcl: Any,
    _mock_clear_hupcl: Any,
    mock_platform_system: Any,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test tunnel with tunnel arg (act like we are on a linux system)."""

    # Override the time.sleep so there is no loop
    def my_sleep(amount: float) -> None:
        """Simulate a sleep in tests by printing the provided value and terminating the process.

        Prints `amount` to stdout and then exits the process with exit code 3.

        Parameters
        ----------
        amount : float
            The value (typically a sleep duration) to print before exiting.
        """
        print(f"{amount}")
        sys.exit(3)

    a_mock = MagicMock()
    a_mock.return_value = "Linux"
    mock_platform_system.side_effect = a_mock
    sys.argv = ["", "--tunnel"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with SerialInterface(noProto=True, connectNow=False) as serialInterface:
        with (
            caplog.at_level(logging.DEBUG),
            patch(
                "meshtastic.serial_interface.SerialInterface",
                return_value=serialInterface,
            ),
            patch("time.sleep", side_effect=my_sleep),
        ):
            with pytest.raises(SystemExit) as pytest_wrapped_e:
                tunnelMain()
            assert pytest_wrapped_e.type is SystemExit
            assert pytest_wrapped_e.value.code == 3
        mock_platform_system.assert_called()
        assert re.search(r"Not starting Tunnel", caplog.text, re.MULTILINE)
    out, err = capsys.readouterr()
    assert re.search(r"Connected to radio", out, re.MULTILINE)
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_set_favorite_node() -> None:
    """Test --set-favorite-node node."""
    sys.argv = ["", "--set-favorite-node", "!12345678"]
    mt_config.args = sys.argv  # type: ignore[assignment]
    mocked_node = MagicMock(autospec=Node)
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()

    mocked_node.setFavorite.assert_called_once_with("!12345678")


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_remove_favorite_node() -> None:
    """Test --remove-favorite-node node."""
    sys.argv = ["", "--remove-favorite-node", "!12345678"]
    mt_config.args = sys.argv  # type: ignore[assignment]
    mocked_node = MagicMock(autospec=Node)
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node
    mocked_node.iface = iface
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()

    mocked_node.removeFavorite.assert_called_once_with("!12345678")


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_set_ignored_node() -> None:
    """Test --set-ignored-node node."""
    sys.argv = ["", "--set-ignored-node", "!12345678"]
    mt_config.args = sys.argv  # type: ignore[assignment]
    mocked_node = MagicMock(autospec=Node)
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()

    mocked_node.setIgnored.assert_called_once_with("!12345678")


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_remove_ignored_node() -> None:
    """Test --remove-ignored-node node."""
    sys.argv = ["", "--remove-ignored-node", "!12345678"]
    mt_config.args = sys.argv  # type: ignore[assignment]
    mocked_node = MagicMock(autospec=Node)
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node
    mocked_node.iface = iface
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()

    mocked_node.removeIgnored.assert_called_once_with("!12345678")


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_set_owner_whitespace_only(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test --set-owner with whitespace-only name."""
    monkeypatch.setattr(sys, "argv", ["", "--set-owner", "   "])
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        with pytest.raises(SystemExit) as excinfo:
            main()

    _, err = capsys.readouterr()
    # Error messages go to stderr
    assert (
        "ERROR: Long Name cannot be empty or contain only whitespace characters" in err
    )
    assert excinfo.value.code == 1


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_set_owner_empty_string(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --set-owner with empty string."""
    sys.argv = ["", "--set-owner", ""]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        with pytest.raises(SystemExit) as excinfo:
            main()

    _, err = capsys.readouterr()
    # Error messages go to stderr
    assert (
        "ERROR: Long Name cannot be empty or contain only whitespace characters" in err
    )
    assert excinfo.value.code == 1


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_set_owner_short_whitespace_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --set-owner-short with whitespace-only name."""
    sys.argv = ["", "--set-owner-short", "   "]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        with pytest.raises(SystemExit) as excinfo:
            main()

    _, err = capsys.readouterr()
    # Error messages go to stderr
    assert (
        "ERROR: Short Name cannot be empty or contain only whitespace characters" in err
    )
    assert excinfo.value.code == 1


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_set_owner_short_empty_string(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --set-owner-short with empty string."""
    sys.argv = ["", "--set-owner-short", ""]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        with pytest.raises(SystemExit) as excinfo:
            main()

    _, err = capsys.readouterr()
    # Error messages go to stderr
    assert (
        "ERROR: Short Name cannot be empty or contain only whitespace characters" in err
    )
    assert excinfo.value.code == 1


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_set_ham_whitespace_only(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify that invoking the CLI with --set-ham and a whitespace-only callsign prints an appropriate error and exits with code 1.

    Asserts the error message "ERROR: Ham radio callsign cannot be empty or contain only
    whitespace characters" appears on stderr and that the process exits with code 1.

    """
    sys.argv = ["", "--set-ham", "   "]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        with pytest.raises(SystemExit) as excinfo:
            main()

    _, err = capsys.readouterr()
    # Error messages go to stderr
    assert (
        "ERROR: Ham radio callsign cannot be empty or contain only whitespace characters"
        in err
    )
    assert excinfo.value.code == 1


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_set_ham_empty_string(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --set-ham with empty string."""
    sys.argv = ["", "--set-ham", ""]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        with pytest.raises(SystemExit) as excinfo:
            main()

    _, err = capsys.readouterr()
    # Error messages go to stderr
    assert (
        "ERROR: Ham radio callsign cannot be empty or contain only whitespace characters"
        in err
    )
    assert excinfo.value.code == 1


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ota_update_requires_tcp_interface(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--ota-update should fail fast when not using a TCP interface."""
    sys.argv = ["", "--ota-update", "firmware.bin"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)

    with (
        patch("meshtastic.serial_interface.SerialInterface", return_value=iface),
        pytest.raises(SystemExit) as excinfo,
    ):
        main()

    _, err = capsys.readouterr()
    assert "OTA update currently requires a TCP connection" in err
    assert excinfo.value.code == 1


def _make_fake_tcp_interface(
    *,
    get_node: Callable[..., Any] | None = None,
    on_close: Callable[[], None] | None = None,
) -> type[object]:
    """Return a configurable TCPInterface test double with context-manager behavior."""

    class _FakeTCPInterface:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.hostname = "localhost"
            if get_node is not None:
                self.getNode = get_node

        def __enter__(self) -> "_FakeTCPInterface":
            return self

        def __exit__(self, *_args: object) -> None:
            self.close()

        def close(self) -> None:
            """Provide TCPInterface-compatible cleanup hook for test patches."""
            if on_close is not None:
                on_close()

    return _FakeTCPInterface


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ota_update_retries_then_exits(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--ota-update should retry OTA failures and exit after the final failed attempt."""
    sys.argv = ["", "--host", "localhost", "--ota-update", "firmware.bin"]
    mt_config.args = cast(Any, sys.argv)

    node = MagicMock(autospec=Node)
    get_node = MagicMock(return_value=node)

    ota = MagicMock()
    ota.hash_bytes.return_value = b"\x01\x02"
    ota.update.side_effect = OTATransportError("boom")

    with (
        patch(
            "meshtastic.tcp_interface.TCPInterface",
            _make_fake_tcp_interface(get_node=get_node),
        ),
        patch("meshtastic.ota.ESP32WiFiOTA", return_value=ota),
        patch("meshtastic.__main__.time.sleep") as sleep_mock,
        pytest.raises(SystemExit) as excinfo,
    ):
        main()

    _, err = capsys.readouterr()
    assert "OTA update failed: boom" in err
    assert excinfo.value.code == 1
    assert ota.update.call_count == main_module.OTA_MAX_RETRIES
    assert any(
        (
            call_args.args
            and call_args.args[0] == MAIN_LOCAL_ADDR
            and call_args.kwargs.get("requestChannels") is False
        )
        or call_args.kwargs.get("dest") == MAIN_LOCAL_ADDR
        for call_args in get_node.call_args_list
    )
    assert sleep_mock.call_args_list == [
        call(main_module.OTA_REBOOT_WAIT_SECONDS),
        *[call(main_module.OTA_RETRY_DELAY_SECONDS)]
        * (main_module.OTA_MAX_RETRIES - 1),
    ]


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ota_update_fails_fast_on_non_transport_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--ota-update should not retry deterministic OTA errors."""
    sys.argv = ["", "--host", "localhost", "--ota-update", "firmware.bin"]
    mt_config.args = cast(Any, sys.argv)

    node = MagicMock(autospec=Node)
    get_node = MagicMock(return_value=node)

    ota = MagicMock()
    ota.hash_bytes.return_value = b"\x01\x02"
    ota.update.side_effect = OTAError("deterministic")

    with (
        patch(
            "meshtastic.tcp_interface.TCPInterface",
            _make_fake_tcp_interface(get_node=get_node),
        ),
        patch("meshtastic.ota.ESP32WiFiOTA", return_value=ota),
        patch("meshtastic.__main__.time.sleep") as sleep_mock,
        pytest.raises(SystemExit) as excinfo,
    ):
        main()

    _, err = capsys.readouterr()
    assert "OTA update failed: deterministic" in err
    assert excinfo.value.code == 1
    ota.update.assert_called_once()
    assert any(
        (
            call_args.args
            and call_args.args[0] == MAIN_LOCAL_ADDR
            and call_args.kwargs.get("requestChannels") is False
        )
        or call_args.kwargs.get("dest") == MAIN_LOCAL_ADDR
        for call_args in get_node.call_args_list
    )
    assert sleep_mock.call_args_list == [call(main_module.OTA_REBOOT_WAIT_SECONDS)]


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ota_update_constructor_error_exits_gracefully(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--ota-update should exit gracefully when ESP32WiFiOTA constructor raises OTAError.

    This tests the error handling for constructor failures (invalid destination,
    missing firmware file, empty firmware, etc.) that occur before update() is called.
    """
    sys.argv = ["", "--host", "localhost", "--ota-update", "firmware.bin"]
    mt_config.args = cast(Any, sys.argv)

    node = MagicMock(autospec=Node)
    get_node = MagicMock(return_value=node)

    with (
        patch(
            "meshtastic.tcp_interface.TCPInterface",
            _make_fake_tcp_interface(get_node=get_node),
        ),
        patch(
            "meshtastic.ota.ESP32WiFiOTA",
            side_effect=OTAError(
                "Invalid OTA destination 'bad:port': malformed address"
            ),
        ) as ota_ctor_mock,
        patch("meshtastic.__main__.time.sleep") as sleep_mock,
        pytest.raises(SystemExit) as excinfo,
    ):
        main()

    _, err = capsys.readouterr()
    assert (
        "OTA update failed: Invalid OTA destination 'bad:port': malformed address"
        in err
    )
    assert excinfo.value.code == 1
    # Constructor was called with firmware path and hostname
    ota_ctor_mock.assert_called_once_with("firmware.bin", "localhost")
    # Should not reach sleep/retry logic since constructor failed
    assert sleep_mock.call_args_list == []
    # Should not call update or startOTA since constructor failed
    node.startOTA.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ota_update_succeeds_and_prints_completion(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--ota-update should break on first successful update and print completion."""
    sys.argv = ["", "--host", "localhost", "--ota-update", "firmware.bin"]
    mt_config.args = cast(Any, sys.argv)

    node = MagicMock(autospec=Node)
    get_node = MagicMock(return_value=node)

    ota = MagicMock()
    ota.hash_bytes.return_value = b"\x01\x02"
    ota.update.return_value = None

    with (
        patch(
            "meshtastic.tcp_interface.TCPInterface",
            _make_fake_tcp_interface(get_node=get_node),
        ),
        patch("meshtastic.ota.ESP32WiFiOTA", return_value=ota),
        patch("meshtastic.__main__.time.sleep") as sleep_mock,
    ):
        main()

    out, err = capsys.readouterr()
    assert "OTA update completed successfully!" in out
    assert err == ""
    assert ota.update.call_count == 1
    assert any(
        (
            call_args.args
            and call_args.args[0] == MAIN_LOCAL_ADDR
            and call_args.kwargs.get("requestChannels") is False
        )
        or call_args.kwargs.get("dest") == MAIN_LOCAL_ADDR
        for call_args in get_node.call_args_list
    )
    node.startOTA.assert_called_once_with(
        mode=main_module.admin_pb2.OTAMode.OTA_WIFI,
        ota_file_hash=ota.hash_bytes.return_value,
    )
    assert sleep_mock.call_args_list == [call(main_module.OTA_REBOOT_WAIT_SECONDS)]


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ota_update_rejects_remote_dest(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--ota-update should fail fast when --dest targets a non-local node."""
    sys.argv = [
        "",
        "--host",
        "localhost",
        "--dest",
        "!abcd1234",
        "--ota-update",
        "firmware.bin",
    ]
    mt_config.args = cast(Any, sys.argv)

    with (
        patch("meshtastic.tcp_interface.TCPInterface", _make_fake_tcp_interface()),
        patch("meshtastic.ota.ESP32WiFiOTA") as ota_cls,
        pytest.raises(SystemExit) as excinfo,
    ):
        main()

    _, err = capsys.readouterr()
    assert (
        "OTA update only supports the directly connected local node; omit --dest or use --dest ^local."
        in err
    )
    assert excinfo.value.code == 1
    ota_cls.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ota_update_allows_explicit_local_dest(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--ota-update should allow explicit local destination targeting."""
    sys.argv = [
        "",
        "--host",
        "localhost",
        "--dest",
        MAIN_LOCAL_ADDR,
        "--ota-update",
        "firmware.bin",
    ]
    mt_config.args = cast(Any, sys.argv)

    local_node = MagicMock(autospec=Node)
    other_node = MagicMock(autospec=Node)

    def _get_node(dest: object, *args: object, **kwargs: object) -> object:
        request_channels = (
            kwargs.get("requestChannels", True)
            if "requestChannels" in kwargs
            else (args[0] if args else True)
        )
        if dest == MAIN_LOCAL_ADDR and request_channels is False:
            return local_node
        return other_node

    get_node = MagicMock(side_effect=_get_node)

    ota = MagicMock()
    ota.hash_bytes.return_value = b"\x01\x02"
    ota.update.return_value = None

    with (
        patch(
            "meshtastic.tcp_interface.TCPInterface",
            _make_fake_tcp_interface(get_node=get_node),
        ),
        patch("meshtastic.ota.ESP32WiFiOTA", return_value=ota),
        patch("meshtastic.__main__.time.sleep"),
    ):
        main()

    out, err = capsys.readouterr()
    assert "OTA update completed successfully!" in out
    assert err == ""
    local_node.startOTA.assert_called_once_with(
        mode=main_module.admin_pb2.OTAMode.OTA_WIFI,
        ota_file_hash=ota.hash_bytes.return_value,
    )
    other_node.startOTA.assert_not_called()
    ota.update.assert_called_once()
    assert any(
        recorded_call.args[:2] == (MAIN_LOCAL_ADDR, False)
        or (
            recorded_call.args[:1] == (MAIN_LOCAL_ADDR,)
            and recorded_call.kwargs.get("requestChannels") is False
        )
        for recorded_call in get_node.call_args_list
    )


@pytest.mark.unit
def test_create_power_meter_requires_initialized_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_create_power_meter should fail fast if mt_config.args is uninitialized."""
    monkeypatch.setattr(main_module, "meter", None)
    monkeypatch.setattr(mt_config, "args", None)

    with pytest.raises(
        RuntimeError,
        match="mt_config.args must be initialized before calling _create_power_meter",
    ):
        _create_power_meter()


@pytest.mark.unit
def test_create_power_meter_exits_when_powermon_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """_create_power_meter should exit with a clear error when powermon is unavailable."""
    args = SimpleNamespace(
        power_voltage=None,
        power_riden=None,
        power_ppk2_supply=False,
        power_ppk2_meter=False,
        power_sim=True,
        power_wait=False,
    )

    monkeypatch.setattr(main_module, "meter", None)
    monkeypatch.setattr(main_module, "have_powermon", False)
    monkeypatch.setattr(main_module, "powermon_exception", ImportError("boom"))
    monkeypatch.setattr(mt_config, "args", args)

    with pytest.raises(SystemExit) as excinfo:
        _create_power_meter()

    _out, err = capsys.readouterr()
    assert excinfo.value.code == 1
    assert "The powermon module could not be loaded." in err


@pytest.mark.unit
def test_create_power_meter_sleeps_after_power_on_when_not_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_create_power_meter should sleep for boot delay when power_wait is disabled."""
    fake_meter = MagicMock()
    args = SimpleNamespace(
        power_voltage="3.3",
        power_riden=None,
        power_ppk2_supply=False,
        power_ppk2_meter=False,
        power_sim=True,
        power_wait=False,
    )

    monkeypatch.setattr(main_module, "meter", None)
    monkeypatch.setattr(main_module, "SimPowerSupply", lambda: fake_meter)
    monkeypatch.setattr(mt_config, "args", args)
    sleep_mock = MagicMock()
    monkeypatch.setattr(main_module.time, "sleep", sleep_mock)  # type: ignore[attr-defined]

    _create_power_meter()

    fake_meter.setVoltage.assert_called_once_with(3.3)
    fake_meter.powerOn.assert_called_once_with()
    sleep_mock.assert_called_once_with(main_module.POWER_ON_BOOT_DELAY_SECONDS)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_exits_when_power_flag_requested_without_powermon(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Main should fail fast when a power meter flag is used without powermon."""
    monkeypatch.setattr(main_module, "have_powermon", False)
    monkeypatch.setattr(main_module, "powermon_exception", ImportError("boom"))
    monkeypatch.setattr(sys, "argv", ["", "--power-sim"])

    with pytest.raises(SystemExit) as excinfo:
        main()

    _out, err = capsys.readouterr()
    assert excinfo.value.code == 1
    assert "The powermon module could not be loaded." in err


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_serial_oserror_includes_original_error_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Serial OSError startup failures should include the original exception text."""
    sys.argv = ["", "--port", "/dev/ttyUSB999", "--set-time", "1"]
    mt_config.args = cast(Any, sys.argv)

    with (
        patch(
            "meshtastic.serial_interface.SerialInterface",
            side_effect=OSError("device busy"),
        ),
        pytest.raises(SystemExit) as excinfo,
    ):
        main()

    _out, err = capsys.readouterr()
    assert "OS Error:" in err
    assert "Original error: device busy" in err
    assert excinfo.value.code == 1


@pytest.mark.unit
def test_printConfig_skips_non_message_sections(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """PrintConfig should skip sections that have no message descriptor."""
    config = SimpleNamespace(
        DESCRIPTOR=SimpleNamespace(
            fields=[SimpleNamespace(name="telemetry")],
            fields_by_name={"telemetry": SimpleNamespace(message_type=None)},
        )
    )

    printConfig(config)

    out, err = capsys.readouterr()
    assert out == ""
    assert err == ""


def _patch_fast_monotonic(monkeypatch: pytest.MonkeyPatch) -> None:
    _val = [0.0]

    def _fast():
        _val[0] += 100.0
        return _val[0]

    monkeypatch.setattr(main_module.time, "monotonic", _fast)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_phase3_verified_with_matching_config_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "phase3_verified.yaml"
    config_path.write_text(
        yaml.safe_dump({"config": {"power": {"ls_secs": 222}}}),
        encoding="utf-8",
    )
    target_local = localonly_pb2.LocalConfig()
    iface, target_node = _build_configure_interface(
        target_local, localonly_pb2.LocalModuleConfig()
    )
    iface.isConnected = threading.Event()
    iface.isConnected.set()
    iface.waitForConfig = MagicMock()
    target_node.requestConfig = MagicMock(
        side_effect=lambda field_desc: (
            setattr(target_local.power, "ls_secs", 222)
            if getattr(field_desc, "name", "") == "power"
            else None
        )
    )
    _patch_fast_monotonic(monkeypatch)
    _run_main_configure_file(config_path, iface, monkeypatch)
    out, _ = capsys.readouterr()
    assert "All settings verified" in out


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_phase1_direct_write_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "phase1_order.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "owner": "OrderTest",
                "owner_short": "OT",
                "location": {"lat": 1.0, "lon": 2.0, "alt": 3.0},
                "canned_messages": "A|B|C",
                "ringtone": "24:d=16,o=5,b=100:c",
                "channel_url": "https://meshtastic.org/e/#CgYSAQABAA",
            }
        ),
        encoding="utf-8",
    )
    iface, target_node = _build_configure_interface()
    _patch_fast_monotonic(monkeypatch)
    _run_main_configure_file(config_path, iface, monkeypatch)

    phase1_methods = (
        "setOwner",
        "setFixedPosition",
        "set_canned_message",
        "set_ringtone",
        "setURL",
    )
    method_names = [c[0] for c in target_node.method_calls]
    relevant = [m for m in method_names if m in phase1_methods]
    expected = [
        "setOwner",
        "setOwner",
        "setFixedPosition",
        "set_canned_message",
        "set_ringtone",
        "setURL",
    ]
    assert relevant == expected


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_owner_values_use_normalized_strings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "owner_normalized.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "owner": "  Normalized Owner  ",
                "owner_short": "  NO  ",
            }
        ),
        encoding="utf-8",
    )
    iface, target_node = _build_configure_interface()
    _run_main_configure_file(config_path, iface, monkeypatch)

    assert target_node.setOwner.call_args_list == [
        call(long_name="Normalized Owner"),
        call(long_name=None, short_name="NO"),
    ]


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_channel_url_is_terminal_phase1_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "terminal_seturl.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "owner": "TerminalTest",
                "location": {"lat": 10.0, "lon": 20.0, "alt": 30.0},
                "channel_url": "https://meshtastic.org/e/#CgYSAQABAA",
            }
        ),
        encoding="utf-8",
    )
    iface, target_node = _build_configure_interface()
    _patch_fast_monotonic(monkeypatch)
    _run_main_configure_file(config_path, iface, monkeypatch)

    method_names = [c[0] for c in target_node.method_calls]
    seturl_indices = [i for i, m in enumerate(method_names) if m == "setURL"]
    assert len(seturl_indices) == 1
    seturl_idx = seturl_indices[0]
    after_seturl = method_names[seturl_idx + 1 :]
    for method in (
        "setFixedPosition",
        "set_canned_message",
        "set_ringtone",
        "setOwner",
    ):
        assert method not in after_seturl


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_seturl_unstable_aborts_before_phase2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "unstable_seturl.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "channel_url": "https://meshtastic.org/e/#CgYSAQABAA",
                "config": {"power": {"ls_secs": 222}},
            }
        ),
        encoding="utf-8",
    )
    target_local = localonly_pb2.LocalConfig()
    iface, target_node = _build_configure_interface(
        target_local, localonly_pb2.LocalModuleConfig()
    )
    iface.isConnected = threading.Event()
    iface.isConnected.set()
    iface.waitForConfig = MagicMock()
    monkeypatch.setattr(
        "meshtastic.__main__._post_seturl_stability_check",
        lambda *a, **k: False,
    )
    _patch_fast_monotonic(monkeypatch)
    with pytest.raises(SystemExit):
        _run_main_configure_file(config_path, iface, monkeypatch)

    target_node.beginSettingsTransaction.assert_not_called()
    _, err = capsys.readouterr()
    assert "transport did not stabilize" in err


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_seturl_stable_proceeds_to_phase2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "stable_seturl.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "channel_url": "https://meshtastic.org/e/#CgYSAQABAA",
                "config": {"power": {"ls_secs": 222}},
            }
        ),
        encoding="utf-8",
    )
    target_local = localonly_pb2.LocalConfig()
    iface, target_node = _build_configure_interface(
        target_local, localonly_pb2.LocalModuleConfig()
    )
    iface.isConnected = threading.Event()
    iface.isConnected.set()
    iface.waitForConfig = MagicMock()
    monkeypatch.setattr(
        "meshtastic.__main__._post_seturl_stability_check",
        lambda *a, **k: True,
    )
    _patch_fast_monotonic(monkeypatch)
    _run_main_configure_file(config_path, iface, monkeypatch)

    target_node.beginSettingsTransaction.assert_called_once()
    target_node.commitSettingsTransaction.assert_called_once()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_rejects_channel_url_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "alias_channel_url.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "channel_url": "https://meshtastic.org/e/#CgYSAQABAA",
                "channelUrl": "https://meshtastic.org/e/#CgYSAQABAA",
            }
        ),
        encoding="utf-8",
    )
    iface, target_node = _build_configure_interface()
    with pytest.raises(SystemExit):
        _run_main_configure_file(config_path, iface, monkeypatch)

    _, err = capsys.readouterr()
    assert "channel_url" in err
    assert "channelUrl" in err
    target_node.beginSettingsTransaction.assert_not_called()
    target_node.setURL.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_rejects_owner_short_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "alias_owner_short.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "owner_short": "OT",
                "ownerShort": "OT",
            }
        ),
        encoding="utf-8",
    )
    iface, target_node = _build_configure_interface()
    with pytest.raises(SystemExit):
        _run_main_configure_file(config_path, iface, monkeypatch)

    _, err = capsys.readouterr()
    assert "owner_short" in err
    assert "ownerShort" in err
    target_node.beginSettingsTransaction.assert_not_called()
    target_node.setOwner.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_phase3_no_reconnect_needed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "phase3_no_reboot.yaml"
    config_path.write_text(
        yaml.safe_dump({"owner": "TestUser"}),
        encoding="utf-8",
    )
    iface, target_node = _build_configure_interface()
    _run_main_configure_file(config_path, iface, monkeypatch)
    out, _ = capsys.readouterr()
    assert "no reboot expected" in out


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_channel_url_only_reports_possible_reconnect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "phase1_channel_url_only.yaml"
    config_path.write_text(
        yaml.safe_dump({"channel_url": "https://meshtastic.org/e/#CgcSAQE6AggN"}),
        encoding="utf-8",
    )
    iface, target_node = _build_configure_interface()
    _run_main_configure_file(config_path, iface, monkeypatch)
    out, _ = capsys.readouterr()
    assert "Phase 1: Applying direct configuration" in out
    assert (
        "Configuration applied. Channel URL updates may still trigger reconnect/reboot."
        in out
    )
    assert "Configuration applied (no reboot expected)." not in out
    target_node.setURL.assert_called_once()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_channel_url_skip_when_already_matching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "phase1_channel_url_skip.yaml"
    config_path.write_text(
        yaml.safe_dump({"channel_url": "https://meshtastic.org/e/#CgcSAQE6AggN"}),
        encoding="utf-8",
    )
    iface, target_node = _build_configure_interface()
    monkeypatch.setattr(
        "meshtastic.__main__._channel_url_matches_current_device_state",
        lambda *a, **k: True,
    )
    _run_main_configure_file(config_path, iface, monkeypatch)
    out, _ = capsys.readouterr()
    assert "Channel url already matches device state; skipping apply." in out
    assert "Configuration applied (no reboot expected)." in out
    target_node.setURL.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_phase3_channel_url_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ..protobuf import apponly_pb2, channel_pb2

    config_path = tmp_path / "phase3_channel_url.yaml"
    channel_settings = channel_pb2.ChannelSettings()
    channel_settings.psk = b"\x01"
    channel_settings.name = "test"
    cs = apponly_pb2.ChannelSet()
    cs.settings.add().CopyFrom(channel_settings)
    cs.lora_config.region = config_pb2.Config.LoRaConfig.RegionCode.Value("US")
    cs.lora_config.hop_limit = 3
    raw = cs.SerializeToString()
    b64 = base64.b64encode(raw, altchars=b"-_").decode().rstrip("=")
    test_url = f"https://meshtastic.org/e/#{b64}"
    config_path.write_text(
        yaml.safe_dump(
            {
                "channel_url": test_url,
                "config": {"power": {"ls_secs": 222}},
            }
        ),
        encoding="utf-8",
    )
    target_local = localonly_pb2.LocalConfig()
    target_local.lora.region = config_pb2.Config.LoRaConfig.RegionCode.Value("US")
    target_local.lora.hop_limit = 3
    iface, target_node = _build_configure_interface(
        target_local, localonly_pb2.LocalModuleConfig()
    )
    primary_channel = channel_pb2.Channel()
    primary_channel.role = channel_pb2.Channel.Role.PRIMARY
    primary_channel.settings.CopyFrom(channel_settings)

    def _request_channels_side_effect(*_args: object) -> None:
        target_node.channels = [primary_channel]

    target_node.channels = [primary_channel]
    iface.isConnected = threading.Event()
    iface.isConnected.set()
    iface.waitForConfig = MagicMock()
    target_node.requestConfig = MagicMock(
        side_effect=lambda field_desc: (
            setattr(target_local.power, "ls_secs", 222)
            if getattr(field_desc, "name", "") == "power"
            else None
        )
    )
    target_node.requestChannels = MagicMock(side_effect=_request_channels_side_effect)
    monkeypatch.setattr(
        "meshtastic.__main__._verify_channel_url_against_state",
        lambda *a, **k: False,
    )
    monkeypatch.setattr(
        "meshtastic.__main__._post_seturl_stability_check",
        lambda *a, **k: True,
    )
    _patch_fast_monotonic(monkeypatch)
    _run_main_configure_file(config_path, iface, monkeypatch)
    out, _ = capsys.readouterr()
    assert "Could not fully verify" in out


@pytest.mark.unit
def test_post_seturl_stability_check_triggers_reconnect_when_disconnected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = threading.Event()
    iface = SimpleNamespace(
        isConnected=event,
        waitForConfig=MagicMock(),
    )
    iface.connect = MagicMock(side_effect=event.set)
    monkeypatch.setattr("time.sleep", lambda _: None)

    assert (
        main_module._post_seturl_stability_check(cast(Any, iface), timeout=2.0) is True
    )
    iface.connect.assert_called()
    iface.waitForConfig.assert_called_once()


@pytest.mark.unit
def test_post_factory_reset_ready_probe_closes_and_probes_reconnect() -> None:
    iface = cast(Any, object.__new__(SerialInterface))
    iface.connect = MagicMock()
    iface.close = MagicMock()

    main_module._post_factory_reset_ready_probe(cast(Any, iface))

    iface.connect.assert_called_once()
    assert iface.close.call_count >= 2


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_quiet_flag_parsed_by_argparse(monkeypatch: pytest.MonkeyPatch) -> None:
    """--quiet flag is recognized by the argument parser."""
    monkeypatch.setattr(sys, "argv", ["meshtastic", "--quiet"])
    mt_config.args = sys.argv  # type: ignore[assignment]
    main_module.initParser()
    assert mt_config.args is not None
    assert mt_config.args.quiet is True


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_quiet_suppresses_connect_banner(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """--quiet suppresses the 'Connected to radio' banner."""
    monkeypatch.setattr(sys, "argv", ["", "--info", "--quiet"])
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface._stable_path = None

    def mock_showInfo() -> None:
        print("inside mocked showInfo")

    iface.showInfo.side_effect = mock_showInfo
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()
        out, err = capsys.readouterr()
        assert "Connected to radio" not in out
        assert "inside mocked showInfo" in out
        assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_quiet_still_allows_warnings_and_errors(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--quiet does not suppress warnings/errors from _cli_exit."""
    monkeypatch.setattr(sys, "argv", ["", "--set-owner", "   ", "--quiet"])
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        assert "Connected to radio" not in out
        assert (
            "ERROR: Long Name cannot be empty or contain only whitespace characters"
            in err
        )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_stable_path_banner_omitted_when_already_by_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Stable-path banner suffix is omitted when devPath is already the by-id path."""
    sys.argv = ["", "--info"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.devPath = "/dev/serial/by-id/usb-foo-device"
    iface._stable_path = "/dev/serial/by-id/usb-foo-device"

    def mock_showInfo() -> None:
        print("inside mocked showInfo")

    iface.showInfo.side_effect = mock_showInfo
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()
        out, err = capsys.readouterr()
        assert "Connected to radio on usb-foo-device" in out
        assert "(stable:" not in out
        assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_stable_path_banner_shown_when_different(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Stable-path suffix shown when devPath differs from by-id alias.

    Even when both paths resolve to the same device via realpath (the normal
    Linux /dev/ttyUSB* + /dev/serial/by-id/* case), the stable alias must
    appear so users can copy-paste it for future connections.
    """
    sys.argv = ["", "--info"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.devPath = "/dev/ttyUSB0"
    iface._stable_path = "/dev/serial/by-id/usb-foo-device"

    def mock_showInfo() -> None:
        print("inside mocked showInfo")

    iface.showInfo.side_effect = mock_showInfo

    def fake_realpath(p: str, **_kwargs: object) -> str:
        if p in ("/dev/ttyUSB0", "/dev/serial/by-id/usb-foo-device"):
            return "/dev/bus/usb/001/002"
        return p

    with (
        patch("meshtastic.serial_interface.SerialInterface", return_value=iface),
        patch("os.path.realpath", side_effect=fake_realpath),
    ):
        main()
        out, err = capsys.readouterr()
        assert (
            "Connected to radio on ttyUSB0 (stable: /dev/serial/by-id/usb-foo-device)"
            in out
        )
        assert err == ""


@pytest.mark.unit
def test_flatten_leaf_paths_flat_dict() -> None:
    """_flatten_leaf_paths handles a flat dict."""
    result = main_module._flatten_leaf_paths(
        "lora", {"hop_limit": 3, "tx_enabled": True}
    )
    assert sorted(result) == ["lora.hop_limit", "lora.tx_enabled"]


@pytest.mark.unit
def test_flatten_leaf_paths_nested_dict() -> None:
    """_flatten_leaf_paths recursively flattens nested dicts."""
    result = main_module._flatten_leaf_paths(
        "display", {"screen_on_secs": 60, "nested": {"foo": 1, "bar": 2}}
    )
    assert sorted(result) == [
        "display.nested.bar",
        "display.nested.foo",
        "display.screen_on_secs",
    ]


@pytest.mark.unit
def test_flatten_leaf_paths_empty_nested_dict() -> None:
    """_flatten_leaf_paths treats an empty nested dict as a leaf."""
    result = main_module._flatten_leaf_paths("lora", {"hop_limit": 3, "empty": {}})
    assert sorted(result) == ["lora.empty", "lora.hop_limit"]
