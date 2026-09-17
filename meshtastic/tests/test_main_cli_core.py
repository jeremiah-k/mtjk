"""Meshtastic unit tests for __main__.py."""

# pylint: disable=W0613,R0917

import argparse
import logging
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

import meshtastic.__main__ as main_module
from meshtastic import mt_config
from meshtastic.__main__ import (
    _normalize_pref_name,
    _parse_host_port,
    initParser,
    main,
    support_info,
)
from meshtastic._branding import PRIMARY_CLI_NAME, UPSTREAM_PRODUCT_NAME

# from ..radioconfig_pb2 import UserPreferences
# import meshtastic.config_pb2
from ..protobuf import localonly_pb2
from ..serial_interface import SerialInterface
from ..tcp_interface import TCPInterface

# from ..ble_interface import BLEInterface


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
    sys.argv = [PRIMARY_CLI_NAME, "--version"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with pytest.raises(SystemExit) as pytest_wrapped_e:
        initParser()
    assert pytest_wrapped_e.type is SystemExit
    assert pytest_wrapped_e.value.code == 0
    out, err = capsys.readouterr()
    assert out == f"{PRIMARY_CLI_NAME} {main_module.get_active_version()}\n"
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
@pytest.mark.parametrize(
    ("flag", "destination"),
    (
        pytest.param("--ch-longmod", "ch_longmod", id="long-moderate-short"),
        pytest.param("--ch-longmoderate", "ch_longmod", id="long-moderate-long"),
        pytest.param("--ch-longturbo", "ch_longturbo", id="long-turbo"),
        pytest.param("--ch-shortturbo", "ch_shortturbo", id="short-turbo"),
    ),
)
def test_main_init_parser_accepts_firmware_2_8_preset_shorthands(
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
    destination: str,
) -> None:
    """Firmware 2.8 preset shorthands should retain stable argparse destinations."""
    monkeypatch.setattr(sys, "argv", ["meshtastic", flag])

    initParser()

    assert mt_config.args is not None
    assert getattr(mt_config.args, destination) is True


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_init_parser_accepts_region_preset_capability_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["meshtastic", "--show-region-presets"])
    initParser()
    assert mt_config.args is not None
    assert mt_config.args.show_region_presets is True


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_init_parser_accepts_usb_lockdown_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meshtastic",
            "--lockdown-unlock",
            "--lockdown-passphrase-file",
            "/tmp/secret",
            "--lockdown-wait",
            "12",
        ],
    )
    initParser()
    assert mt_config.args is not None
    assert mt_config.args.lockdown_unlock is True
    assert mt_config.args.lockdown_passphrase_file == "/tmp/secret"
    assert mt_config.args.lockdown_wait == 12.0


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw_value", "expected"),
    (
        pytest.param("medium_turbo", "MEDIUM_TURBO", id="lower-snake"),
        pytest.param("long-moderate", "LONG_MODERATE", id="lower-kebab"),
        pytest.param(" TINY_FAST ", "TINY_FAST", id="whitespace"),
    ),
)
def test_parse_modem_preset_name_uses_active_protobuf_schema(
    raw_value: str,
    expected: str,
) -> None:
    """The generic preset parser should normalize every active enum value."""
    assert main_module._parse_modem_preset_name(raw_value) == expected


@pytest.mark.unit
def test_parse_modem_preset_name_rejects_unknown_value() -> None:
    """Unknown generic presets should fail with schema-derived choices."""
    with pytest.raises(argparse.ArgumentTypeError, match="Unknown modem preset"):
        main_module._parse_modem_preset_name("future-but-not-yet-in-schema")


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_main_version(capsys: pytest.CaptureFixture[str]) -> None:
    """The compatibility CLI should silently report the configured product version."""
    sys.argv = [UPSTREAM_PRODUCT_NAME, "--version"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with pytest.raises(SystemExit) as pytest_wrapped_e:
        main()
    assert pytest_wrapped_e.type is SystemExit
    assert pytest_wrapped_e.value.code == 0
    out, err = capsys.readouterr()
    assert out == f"{PRIMARY_CLI_NAME} {main_module.get_active_version()}\n"
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
@pytest.mark.usefixtures("reset_mt_config")
def test_main_rejects_repeated_configure_before_connecting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Repeated configure documents must fail instead of silently ignoring one."""
    first = tmp_path / "first.yaml"
    second = tmp_path / "second.yaml"
    first.write_text("owner: first\n", encoding="utf-8")
    second.write_text("owner: second\n", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meshtastic",
            "--configure",
            str(first),
            "--configure",
            str(second),
        ],
    )
    serial_factory = MagicMock()

    with (
        patch("meshtastic.serial_interface.SerialInterface", serial_factory),
        pytest.raises(SystemExit) as exc_info,
    ):
        main()

    assert exc_info.value.code == 2
    assert "--configure may be specified only once" in capsys.readouterr().err
    serial_factory.assert_not_called()


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

    assert main_module._is_local_destination(iface, main_module.BROADCAST_ADDR) is True
    assert main_module._is_local_destination(iface, MAIN_LOCAL_ADDR) is True
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

    def _mock_show_info() -> None:
        """Print a recognizable marker to stdout used by tests to simulate an interface's showInfo().

        This test helper prints the string "inside mocked showInfo" so tests can detect that the mocked showInfo was invoked.
        """
        print("inside mocked showInfo")

    iface.showInfo.side_effect = _mock_show_info
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

    def _mock_show_info() -> None:
        """Print a recognizable marker to stdout used by tests to simulate an interface's showInfo().

        This test helper prints the string "inside mocked showInfo" so tests can detect that the mocked showInfo was invoked.
        """
        print("inside mocked showInfo")

    iface.showInfo.side_effect = _mock_show_info
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

    def _mock_show_info() -> None:
        """Print a recognizable marker to stdout used by tests to simulate an interface's showInfo().

        This test helper prints the string "inside mocked showInfo" so tests can detect that the mocked showInfo was invoked.
        """
        print("inside mocked showInfo")

    iface.showInfo.side_effect = _mock_show_info

    # Override the time.sleep so there is no loop
    def _sleep_and_exit(amount: float) -> None:
        """Print sleep duration and terminate to break the no-proto loop in tests."""
        print(f"amount:{amount}")
        sys.exit(0)

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        with patch("time.sleep", side_effect=_sleep_and_exit):
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

    def _mock_show_info() -> None:
        """Print a recognizable marker to stdout used by tests to simulate an interface's showInfo().

        This test helper prints the string "inside mocked showInfo" so tests can detect that the mocked showInfo was invoked.
        """
        print("inside mocked showInfo")

    iface.showInfo.side_effect = _mock_show_info
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

    def _mock_show_info() -> None:
        """Print a recognizable marker to stdout used by tests to simulate an interface's showInfo().

        This test helper prints the string "inside mocked showInfo" so tests can detect that the mocked showInfo was invoked.
        """
        stream = debug_out_stream[0]
        if stream is not None:
            stream.write("inside mocked showInfo\n")
            stream.flush()
        print("inside mocked showInfo")

    iface.showInfo.side_effect = _mock_show_info
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
def test_main_qr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--qr`` should render the exact primary-channel URL returned by the node."""
    expected_url = "https://meshtastic.org/e/#deterministic-primary"
    sys.argv = ["", "--qr"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    qr_render = MagicMock(return_value="<qr-terminal>")
    monkeypatch.setattr(
        "meshtastic.cli.qr.segno",
        SimpleNamespace(make=MagicMock()),
        raising=True,
    )
    monkeypatch.setattr(
        "meshtastic.cli.qr.render_terminal_qr",
        qr_render,
        raising=True,
    )

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value.getURL.return_value = expected_url

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()

    assert re.search(r"Connected to radio", out, re.MULTILINE)
    assert f"Primary channel URL: {expected_url}" in out
    assert "<qr-terminal>" in out
    assert err == ""
    qr_render.assert_called_once_with(expected_url)
    mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_onConnected_exception(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify that running main with --qr exits with code 1 when QR code generation raises an exception.

    Raises
    ------
    Exception
        Raised by the monkeypatched QR-code function to exercise error handling.
    """
    sys.argv = ["", "--qr"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    def _throw_an_exception(_junk: Any) -> None:
        """Raise a deterministic exception used by tests.

        Raises
        ------
        Exception
            A generic Exception with the message "Fake exception.".
        """
        raise Exception("Fake exception.")  # pylint: disable=W0719

    monkeypatch.setattr(
        "meshtastic.cli.qr.segno",
        SimpleNamespace(make=MagicMock()),
        raising=True,
    )
    monkeypatch.setattr(
        "meshtastic.cli.qr.render_terminal_qr",
        _throw_an_exception,
        raising=True,
    )
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
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

    def _mock_show_nodes(includeSelf: bool, showFields: Any) -> None:
        """Print a test marker indicating a mocked node listing and its options.

        Parameters
        ----------
        includeSelf : bool
            Whether the local node would be included in the listing.
        showFields : Any
            Representation of which node fields would be shown; forwarded verbatim into the printed marker.
        """
        print(f"inside mocked showNodes: {includeSelf} {showFields}")

    iface.showNodes.side_effect = _mock_show_nodes
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"inside mocked showNodes", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()
