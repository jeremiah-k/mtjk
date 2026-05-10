"""Meshtastic unit tests for util.py."""

import base64
import binascii
import glob
import json
import logging
import platform
import re
import subprocess
import threading
import warnings
from collections import Counter
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
import serial.tools.list_ports  # type: ignore[import-untyped]
from hypothesis import given
from hypothesis import strategies as st

import meshtastic.util as util_module
from meshtastic.protobuf import mesh_pb2
from meshtastic.supported_device import (
    SupportedDevice,
    SupportedDeviceValidationError,
    seeed_xiao_s3,
    supported_devices,
    tdeck,
)
from meshtastic.util import (
    DEFAULT_KEY,
    Acknowledgment,
    DeferredExecution,
    DotDict,
    FixmeError,
    Timeout,
    active_ports_on_supported_devices,
    camel_to_snake,
    catchAndIgnore,
    channel_hash,
    convert_mac_addr,
    detect_supported_devices,
    detect_windows_needs_driver,
    dotdict,
    eliminate_duplicate_port,
    findPorts,
    fixme,
    flagsToList,
    fromPSK,
    fromStr,
    generate_channel_hash,
    genPSK256,
    get_devices_with_vendor_id,
    get_unique_vendor_ids,
    hexstr,
    ipstr,
    is_windows11,
    message_to_json,
    messageToJson,
    our_exit,
    pskToString,
    quoteBooleans,
    readnet_u16,
    remove_keys_from_dict,
    snake_to_camel,
    stripnl,
    toNodeNum,
    toStr,
)

_BASE64_ALLOWED_CHARS = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
)
_BASE64_INVALID_CHARS = "!\"#$%&'()*,-.:;<>?@[\\]^_`{|}~"
_HASH_BYTE_MIN = 0
_HASH_BYTE_MAX = 0xFF


class _TempPort:
    """Stub port object for serial-port discovery tests."""

    def __init__(self, device: str | None = None, vid: int | None = None) -> None:
        """Create a temporary port stub with an optional device path and USB vendor ID.

        Parameters
        ----------
        device : str | None
            Port device path (e.g., '/dev/ttyUSB0') or None to leave unset. (Default value = None)
        vid : int | None
            USB vendor ID as an integer, or None to leave unset. (Default value = None)
        """
        self.device = device
        self.vid = vid


@pytest.mark.unit
def test_util_preserves_port_discovery_monkeypatch_modules() -> None:
    """Legacy tests can still patch module objects through meshtastic.util."""
    assert util_module.glob is glob
    assert util_module.platform is platform
    assert util_module.subprocess is subprocess
    assert util_module.serial.tools.list_ports is serial.tools.list_ports


@pytest.mark.unit
def test_genPSK256() -> None:
    """Test genPSK256."""
    result = genPSK256()
    assert isinstance(result, bytes)
    assert len(result) == 32


@pytest.mark.unit
def test_fromStr() -> None:
    """Test fromStr."""
    assert fromStr("") == b""
    assert fromStr("0x12") == b"\x12"
    assert fromStr("t")
    assert fromStr("T")
    assert fromStr("true")
    assert fromStr("True")
    assert fromStr("yes")
    assert fromStr("Yes")
    assert fromStr("f") is False
    assert fromStr("F") is False
    assert fromStr("false") is False
    assert fromStr("False") is False
    assert fromStr("no") is False
    assert fromStr("No") is False
    assert fromStr("100.01") == 100.01
    assert fromStr("123") == 123
    assert fromStr("abc") == "abc"
    assert fromStr("123456789") == 123456789
    assert fromStr("base64:Zm9vIGJhciBiYXo=") == b"foo bar baz"


@pytest.mark.unitslow
def test_quoteBooleans() -> None:
    """Test quoteBooleans."""
    assert quoteBooleans("") == ""
    assert quoteBooleans("foo") == "foo"
    assert quoteBooleans("true") == "true"
    assert quoteBooleans("false") == "false"
    assert quoteBooleans(": true") == ": 'true'"
    assert quoteBooleans(": false") == ": 'false'"


@pytest.mark.unit
def test_fromPSK() -> None:
    """Test fromPSK."""
    random_psk = fromPSK("random")
    assert isinstance(random_psk, bytes)
    assert len(random_psk) == 32
    assert fromPSK("none") == b"\x00"
    assert fromPSK("default") == b"\x01"
    assert fromPSK("simple22") == b"\x17"
    # "trash" is NOT valid base64 (bad padding length), falls back to string
    assert fromPSK("trash") == "trash"
    # Raw base64 auto-detection: deterministic standard AES key lengths
    for key_length in (16, 24, 32):
        expected_bytes = bytes(range(key_length))
        raw_b64_key = base64.b64encode(expected_bytes).decode("ascii")
        assert fromPSK(raw_b64_key) == expected_bytes
    # Raw base64: short (1-byte) key is NOT an allowed PSK length, stays string
    assert fromPSK("AQ==") == "AQ=="
    # Explicit base64: prefix still works for any length (including 1-byte default)
    assert fromPSK("base64:AQ==") == b"\x01"
    assert fromPSK(f"base64:{raw_b64_key}") == expected_bytes
    # Hex still works
    assert fromPSK("0x1a") == b"\x1a"
    # Invalid base64 (spaces/special chars) falls back to string
    assert fromPSK("not valid base64!") == "not valid base64!"
    with pytest.raises(ValueError, match=r"simpleN"):
        fromPSK("simple")
    with pytest.raises(ValueError, match=r"simpleN"):
        fromPSK("simple255")


@pytest.mark.unit
def test_stripnl() -> None:
    """Test stripnl."""
    assert stripnl("") == ""
    assert stripnl("a\n") == "a"
    assert stripnl(" a \n ") == "a"
    assert stripnl("a\nb") == "a b"


@pytest.mark.unit
def test_pskToString_empty_string() -> None:
    """Test pskToString empty string."""
    assert pskToString(b"") == "unencrypted"


@pytest.mark.unit
def test_pskToString_string() -> None:
    """Test pskToString string."""
    assert pskToString(b"hunter123") == "secret"


@pytest.mark.unit
def test_pskToString_one_byte_zero_value() -> None:
    """Test pskToString one byte that is value of 0."""
    assert pskToString(bytes([0x00])) == "unencrypted"


@pytest.mark.unitslow
def test_pskToString_one_byte_non_zero_value() -> None:
    """Test pskToString one byte that is non-zero."""
    assert pskToString(bytes([0x01])) == "default"


@pytest.mark.unitslow
def test_pskToString_many_bytes() -> None:
    """Test pskToString many bytes."""
    assert pskToString(bytes([0x02, 0x01])) == "secret"


@pytest.mark.unit
def test_pskToString_simple() -> None:
    """Test pskToString simple."""
    assert pskToString(bytes([0x03])) == "simple2"


@pytest.mark.unitslow
def test_fixme() -> None:
    """Test fixme()."""
    with pytest.raises(FixmeError) as pytest_wrapped_e:
        fixme("some exception")
    assert pytest_wrapped_e.type is FixmeError


@pytest.mark.unit
def test_catchAndIgnore(caplog: pytest.LogCaptureFixture) -> None:
    """Test catchAndIgnore() does not actually throw an exception, but just logs.

    Raises
    ------
    Exception
        Raised inside the closure to exercise the retry handler.
    """

    def some_closure() -> None:
        """Raise an Exception with the message "foo".

        Raises
        ------
        Exception
            Always raised with message "foo".
        """
        raise Exception("foo")  # pylint: disable=W0719  # noqa: TRY002

    with caplog.at_level(logging.DEBUG):
        catchAndIgnore("something", some_closure)
    assert re.search(r"Exception thrown in something", caplog.text, re.MULTILINE)


@pytest.mark.unitslow
def test_remove_keys_from_dict_empty_keys_empty_dict() -> None:
    """Test when keys and dict both are empty."""
    assert not remove_keys_from_dict((), {})


@pytest.mark.unitslow
def test_remove_keys_from_dict_empty_dict() -> None:
    """Test when dict is empty."""
    assert not remove_keys_from_dict(("a",), {})


@pytest.mark.unit
def test_remove_keys_from_dict_empty_keys() -> None:
    """Test when keys is empty."""
    assert remove_keys_from_dict((), {"a": 1}) == {"a": 1}


@pytest.mark.unitslow
def test_remove_keys_from_dict() -> None:
    """Test remove_keys_from_dict()."""
    assert remove_keys_from_dict(("b",), {"a": 1, "b": 2}) == {"a": 1}


@pytest.mark.unitslow
def test_remove_keys_from_dict_multiple_keys() -> None:
    """Test remove_keys_from_dict()."""
    keys = ("a", "b")
    adict = {"a": 1, "b": 2, "c": 3}
    assert remove_keys_from_dict(keys, adict) == {"c": 3}


@pytest.mark.unit
def test_remove_keys_from_dict_nested() -> None:
    """Test remove_keys_from_dict()."""
    keys = ("b",)
    adict = {"a": {"b": 1}, "b": 2, "c": 3}
    exp = {"a": {}, "c": 3}
    assert remove_keys_from_dict(keys, adict) == exp


@pytest.mark.unitslow
def test_Timeout_not_found() -> None:
    """Test Timeout()."""
    to = Timeout(1)
    attrs = "foo"
    to.waitForSet("bar", attrs)


@pytest.mark.unitslow
def test_Timeout_found() -> None:
    """Test Timeout()."""
    to = Timeout(1)
    attrs = ()
    to.waitForSet("bar", attrs)


@pytest.mark.unitslow
def test_hexstr() -> None:
    """Test hexstr()."""
    assert hexstr(b"123") == "31:32:33"
    assert hexstr(b"") == ""


@pytest.mark.unitslow
def test_ipstr() -> None:
    """Test ipstr()."""
    assert ipstr(b"1234") == "49.50.51.52"
    assert ipstr(b"") == ""


@pytest.mark.unitslow
def test_readnet_u16() -> None:
    """Test readnet_u16()."""
    assert readnet_u16(b"123456", 2) == 13108


@pytest.mark.unitslow
@patch("serial.tools.list_ports.comports", return_value=[])
def test_findPorts_when_none_found(patch_comports: MagicMock) -> None:
    """Test findPorts()."""
    assert not findPorts()
    patch_comports.assert_called()


@pytest.mark.unitslow
@patch("serial.tools.list_ports.comports")
def test_findPorts_when_duplicate_found_and_duplicate_option_used(
    patch_comports: MagicMock,
) -> None:
    """Verify that findPorts() removes duplicate serial devices when.

    eliminate_duplicates is True.

    Sets the patched comports() to return two port-like objects representing
    the same physical device and asserts findPorts(eliminate_duplicates=True)
    returns only the deduplicated device path.

    Parameters
    ----------
    patch_comports : MagicMock
        pytest fixture that patches and returns the
        serial.tools.list_ports.comports function.
    """
    fake1 = _TempPort("/dev/cu.usbserial-1430", vid=0xFFFF)
    fake2 = _TempPort("/dev/cu.wchusbserial1430", vid=0xFFFE)
    patch_comports.return_value = [fake1, fake2]
    assert findPorts(eliminate_duplicates=True) == ["/dev/cu.wchusbserial1430"]
    patch_comports.assert_called()


@pytest.mark.unitslow
@patch("serial.tools.list_ports.comports")
def test_findPorts_when_duplicate_found_and_duplicate_option_used_ports_reversed(
    patch_comports: MagicMock,
) -> None:
    """Verifies that findPorts(eliminate_duplicates=True) returns the expected.

    single port when duplicate devices are reported in reversed order.

    Patches the comports listing to simulate two ports that should be
    considered duplicates and asserts the duplicate-elimination logic
    selects the correct remaining device.

    """
    fake1 = _TempPort("/dev/cu.usbserial-1430", vid=0xFFFF)
    fake2 = _TempPort("/dev/cu.wchusbserial1430", vid=0xFFFE)
    patch_comports.return_value = [fake2, fake1]
    assert findPorts(eliminate_duplicates=True) == ["/dev/cu.wchusbserial1430"]
    patch_comports.assert_called()


@pytest.mark.unitslow
@patch("serial.tools.list_ports.comports")
def test_findPorts_when_duplicate_found_and_duplicate_option_not_used(
    patch_comports: MagicMock,
) -> None:
    """Test findPorts()."""
    fake1 = _TempPort("/dev/cu.usbserial-1430", vid=0xFFFF)
    fake2 = _TempPort("/dev/cu.wchusbserial1430", vid=0xFFFE)
    patch_comports.return_value = [fake1, fake2]
    assert findPorts() == ["/dev/cu.usbserial-1430", "/dev/cu.wchusbserial1430"]
    patch_comports.assert_called()


@pytest.mark.unit
@patch("serial.tools.list_ports.comports")
@patch("glob.glob")
@patch("os.path.isdir", return_value=True)
@patch("platform.system", return_value="Linux")
@patch("os.path.realpath")
def test_findPorts_prefers_linux_by_id_alias_when_available(
    patch_realpath: MagicMock,
    patch_system: MagicMock,
    patch_isdir: MagicMock,
    patch_glob: MagicMock,
    patch_comports: MagicMock,
) -> None:
    """On Linux, prefer stable /dev/serial/by-id aliases over ttyACM paths."""
    by_id_alias = "/dev/serial/by-id/usb-RAK4631-if00"
    tty_device = "/dev/ttyACM1"
    fake1 = _TempPort(tty_device, vid=0x239A)
    patch_comports.return_value = [fake1]
    patch_glob.return_value = [by_id_alias]
    patch_realpath.side_effect = lambda path: (
        tty_device if path in (by_id_alias, tty_device) else path
    )

    assert findPorts() == [by_id_alias]


@pytest.mark.unit
@patch("serial.tools.list_ports.comports")
@patch("glob.glob", return_value=[])
@patch("os.path.isdir", return_value=True)
@patch("platform.system", return_value="Linux")
@patch("os.path.realpath")
def test_findPorts_keeps_tty_path_when_no_by_id_alias_matches(
    patch_realpath: MagicMock,
    patch_system: MagicMock,
    patch_isdir: MagicMock,
    patch_glob: MagicMock,
    patch_comports: MagicMock,
) -> None:
    """When no by-id alias resolves to a tty path, keep original device path."""
    tty_device = "/dev/ttyACM1"
    fake1 = _TempPort(tty_device, vid=0x239A)
    patch_comports.return_value = [fake1]
    patch_realpath.side_effect = lambda path: path

    assert findPorts() == [tty_device]


@pytest.mark.unit
@patch("subprocess.run")
@patch("platform.system", return_value="Windows")
def test_detect_supported_devices_windows_uses_safe_pnp_query(
    patch_system: MagicMock,
    patch_run: MagicMock,
) -> None:
    """Windows supported-device detection should query PnP once without VID interpolation."""
    patch_run.return_value = subprocess.CompletedProcess(
        args=["powershell.exe"],
        returncode=0,
        stdout="DeviceID : USB\\VID_303A&PID_1001\n",
        stderr="",
    )

    devices = detect_supported_devices()

    assert any(device.usb_vendor_id_in_hex == "303a" for device in devices)
    command = patch_run.call_args.args[0]
    assert command[:3] == ["powershell.exe", "-NoProfile", "-Command"]
    assert "303A" not in " ".join(command)
    patch_system.assert_called()


@pytest.mark.unit
@patch("subprocess.run")
@patch("platform.system", return_value="Windows")
def test_detect_windows_needs_driver_filters_pnp_output(
    patch_system: MagicMock,
    patch_run: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Driver detection should filter one static PnP query in Python."""
    patch_run.return_value = subprocess.CompletedProcess(
        args=["powershell.exe"],
        returncode=0,
        stdout=(
            "DeviceID : USB\\VID_303A&PID_1001\n"
            "Status   : CM_PROB_FAILED_INSTALL\n"
        ),
        stderr="",
    )
    device = SupportedDevice(
        name="x",
        for_firmware="heltec-v3",
        usb_vendor_id_in_hex="303A",
        usb_product_id_in_hex="1001",
    )

    with caplog.at_level(logging.DEBUG):
        assert detect_windows_needs_driver(device, print_reason=True) is True

    assert "CM_PROB_FAILED_INSTALL" in caplog.text
    command = patch_run.call_args.args[0]
    assert "303A" not in " ".join(command)
    patch_system.assert_called()


@pytest.mark.unit
@patch("subprocess.run")
@patch("platform.system", return_value="Windows")
def test_detect_windows_needs_driver_rejects_invalid_vendor_id(
    patch_system: MagicMock,
    patch_run: MagicMock,
) -> None:
    """Invalid VID metadata should not be interpolated into a command."""
    device = cast(
        Any,
        SimpleNamespace(usb_vendor_id_in_hex="303A'; Remove-Item C:\\"),
    )

    assert detect_windows_needs_driver(device) is False
    patch_run.assert_not_called()
    patch_system.assert_called()


@pytest.mark.unitslow
def test_convert_mac_addr() -> None:
    """Test convert_mac_addr()."""
    assert convert_mac_addr("/c0gFyhb") == "fd:cd:20:17:28:5b"
    assert convert_mac_addr("fd:cd:20:17:28:5b") == "fd:cd:20:17:28:5b"
    assert convert_mac_addr("") == ""


@pytest.mark.unit
def test_snake_to_camel() -> None:
    """Test snake_to_camel."""
    assert snake_to_camel("") == ""
    assert snake_to_camel("foo") == "foo"
    assert snake_to_camel("foo_bar") == "fooBar"
    assert snake_to_camel("fooBar") == "fooBar"


@pytest.mark.unit
def test_camel_to_snake() -> None:
    """Test camel_to_snake."""
    assert camel_to_snake("") == ""
    assert camel_to_snake("foo") == "foo"
    assert camel_to_snake("Foo") == "foo"
    assert camel_to_snake("fooBar") == "foo_bar"
    assert camel_to_snake("fooBarBaz") == "foo_bar_baz"


@pytest.mark.unit
def test_eliminate_duplicate_port() -> None:
    """Test eliminate_duplicate_port()."""
    assert not eliminate_duplicate_port([])
    assert eliminate_duplicate_port(["/dev/fake"]) == ["/dev/fake"]
    assert eliminate_duplicate_port(["/dev/fake", "/dev/fake1"]) == [
        "/dev/fake",
        "/dev/fake1",
    ]
    assert eliminate_duplicate_port(["/dev/fake", "/dev/fake1", "/dev/fake2"]) == [
        "/dev/fake",
        "/dev/fake1",
        "/dev/fake2",
    ]
    assert eliminate_duplicate_port(
        ["/dev/cu.usbserial-1430", "/dev/cu.wchusbserial1430"]
    ) == ["/dev/cu.wchusbserial1430"]
    assert eliminate_duplicate_port(
        ["/dev/cu.wchusbserial1430", "/dev/cu.usbserial-1430"]
    ) == ["/dev/cu.wchusbserial1430"]
    assert eliminate_duplicate_port(
        ["/dev/cu.usbserial-1234", "/dev/cu.wchusbserial5678"]
    ) == ["/dev/cu.usbserial-1234", "/dev/cu.wchusbserial5678"]
    assert eliminate_duplicate_port(
        ["/dev/cu.SLAB_USBtoUART", "/dev/cu.usbserial-0001"]
    ) == ["/dev/cu.usbserial-0001"]
    assert eliminate_duplicate_port(
        ["/dev/cu.usbserial-0001", "/dev/cu.SLAB_USBtoUART"]
    ) == ["/dev/cu.usbserial-0001"]
    assert eliminate_duplicate_port(
        ["/dev/cu.usbmodem11301", "/dev/cu.wchusbserial11301"]
    ) == ["/dev/cu.wchusbserial11301"]
    assert eliminate_duplicate_port(
        ["/dev/cu.wchusbserial11301", "/dev/cu.usbmodem11301"]
    ) == ["/dev/cu.wchusbserial11301"]
    assert eliminate_duplicate_port(
        ["/dev/cu.usbmodem53230051441", "/dev/cu.wchusbserial53230051441"]
    ) == ["/dev/cu.wchusbserial53230051441"]
    assert eliminate_duplicate_port(
        ["/dev/cu.wchusbserial53230051441", "/dev/cu.usbmodem53230051441"]
    ) == ["/dev/cu.wchusbserial53230051441"]
    assert eliminate_duplicate_port(
        [
            "/dev/fake",
            "/dev/cu.usbserial-1430",
            "/dev/cu.wchusbserial1430",
        ]
    ) == ["/dev/fake", "/dev/cu.wchusbserial1430"]
    assert eliminate_duplicate_port(
        [
            "/dev/cu.usbmodem11301",
            "/dev/fake",
            "/dev/cu.wchusbserial11301",
        ]
    ) == ["/dev/cu.wchusbserial11301", "/dev/fake"]


@pytest.mark.unit
@patch("platform.version", return_value="10.0.22000.194")
@patch("platform.release", return_value="10")
@patch("platform.system", return_value="Windows")
def test_is_windows11_true(
    patched_platform: MagicMock,
    patched_release: MagicMock,
    patched_version: MagicMock,
) -> None:
    """Test is_windows11()."""
    assert is_windows11() is True
    patched_platform.assert_called()
    patched_release.assert_called()
    patched_version.assert_called()


@pytest.mark.unit
@patch("platform.version", return_value="10.0.123456.1")
@patch("platform.release", return_value="10")
@patch("platform.system", return_value="Windows")
def test_is_windows11_allows_six_digit_build_numbers(
    patched_platform: MagicMock,
    patched_release: MagicMock,
    patched_version: MagicMock,
) -> None:
    """Windows build parsing should not truncate future six-digit builds."""
    assert is_windows11() is True
    patched_platform.assert_called()
    patched_release.assert_called()
    patched_version.assert_called()


@pytest.mark.unit
@patch("platform.version", return_value="10.0.a2200.foo")  # made up
@patch("platform.release", return_value="10")
@patch("platform.system", return_value="Windows")
def test_is_windows11_true2(
    patched_platform: MagicMock,
    patched_release: MagicMock,
    patched_version: MagicMock,
) -> None:
    """Test is_windows11()."""
    assert is_windows11() is False
    patched_platform.assert_called()
    patched_release.assert_called()
    patched_version.assert_called()


@pytest.mark.unit
@patch("platform.version", return_value="10.0.17763")  # windows 10 home
@patch("platform.release", return_value="10")
@patch("platform.system", return_value="Windows")
def test_is_windows11_false(
    patched_platform: MagicMock,
    patched_release: MagicMock,
    patched_version: MagicMock,
) -> None:
    """Test is_windows11()."""
    assert is_windows11() is False
    patched_platform.assert_called()
    patched_release.assert_called()
    patched_version.assert_called()


@pytest.mark.unit
@patch("platform.release", return_value="8.1")
@patch("platform.system", return_value="Windows")
def test_is_windows11_false_win8_1(
    patched_platform: MagicMock,
    patched_release: MagicMock,
) -> None:
    """Test is_windows11()."""
    assert is_windows11() is False
    patched_platform.assert_called()
    patched_release.assert_called()


@pytest.mark.unit
@patch("platform.release", return_value="2022Server")
@patch("platform.system", return_value="Windows")
def test_is_windows11_false_winserver(
    patched_platform: MagicMock,
    patched_release: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test is_windows11()."""
    with caplog.at_level(logging.ERROR):
        assert is_windows11() is False
    assert "Problem detecting Windows 11" not in caplog.text
    patched_platform.assert_called()
    patched_release.assert_called()


@pytest.mark.unit
@patch("platform.system", return_value="Linux")
def test_active_ports_on_supported_devices_empty(mock_platform: MagicMock) -> None:
    """Test active_ports_on_supported_devices()."""
    sds: set[SupportedDevice] = set()
    assert active_ports_on_supported_devices(sds) == set()
    mock_platform.assert_called()


@pytest.mark.unit
@patch("meshtastic._port_discovery.glob.glob")
@patch("platform.system", return_value="Linux")
def test_active_ports_on_supported_devices_linux(
    mock_platform: MagicMock,
    mock_glob: MagicMock,
) -> None:
    """Test active_ports_on_supported_devices()."""
    mock_glob.return_value = ["/dev/ttyUSBfake"]
    fake_device = SupportedDevice(
        name="a", for_firmware="heltec-v2.1", baseport_on_linux="ttyUSB"
    )
    fake_supported_devices = [fake_device]
    assert active_ports_on_supported_devices(fake_supported_devices) == {
        "/dev/ttyUSBfake"
    }
    mock_platform.assert_called()
    mock_glob.assert_called()


@pytest.mark.unit
@patch("meshtastic._port_discovery.glob.glob")
@patch("platform.system", return_value="Darwin")
def test_active_ports_on_supported_devices_mac(
    mock_platform: MagicMock,
    mock_glob: MagicMock,
) -> None:
    """Test active_ports_on_supported_devices()."""
    mock_glob.return_value = ["/dev/cu.usbserial-foo"]
    fake_device = SupportedDevice(
        name="a", for_firmware="heltec-v2.1", baseport_on_mac="cu.usbserial-"
    )
    fake_supported_devices = [fake_device]
    assert active_ports_on_supported_devices(fake_supported_devices) == {
        "/dev/cu.usbserial-foo"
    }
    mock_platform.assert_called()
    mock_glob.assert_called()


@pytest.mark.unit
@patch("meshtastic.util.detectWindowsPort", return_value={"COM2"})
@patch("platform.system", return_value="Windows")
def test_active_ports_on_supported_devices_win(
    mock_platform: MagicMock,
    mock_dwp: MagicMock,
) -> None:
    """Test active_ports_on_supported_devices()."""
    fake_device = SupportedDevice(name="a", for_firmware="heltec-v2.1")
    fake_supported_devices = [fake_device]
    assert active_ports_on_supported_devices(fake_supported_devices) == {"COM2"}
    mock_platform.assert_called()
    mock_dwp.assert_called()


@pytest.mark.unit
@patch("meshtastic.util.detectWindowsPort", side_effect=[{"COM2"}, {"COM3"}])
@patch("platform.system", return_value="Windows")
def test_active_ports_on_supported_devices_win_accepts_generators(
    mock_platform: MagicMock,
    mock_dwp: MagicMock,
) -> None:
    """Windows active-port discovery should not exhaust one-shot iterables."""
    fake_supported_devices = (
        SupportedDevice(name=f"device-{index}", for_firmware="heltec-v2.1")
        for index in range(2)
    )

    assert active_ports_on_supported_devices(fake_supported_devices) == {
        "COM2",
        "COM3",
    }
    assert mock_dwp.call_count == 2
    mock_platform.assert_called()


@pytest.mark.unit
@patch("subprocess.run")
@patch("platform.system", return_value="Windows")
def test_active_ports_on_supported_devices_win_default_path_queries_once(
    mock_platform: MagicMock,
    mock_run: MagicMock,
) -> None:
    """The default Windows path should query PnP once for all devices."""
    mock_run.return_value = subprocess.CompletedProcess(
        args=["powershell.exe"],
        returncode=0,
        stdout=(
            "Name     : Meshtastic Serial (COM2)\n"
            "DeviceID : USB\\VID_303A&PID_1001\n\n"
            "Name     : Meshtastic Serial (COM3)\n"
            "DeviceID : USB\\VID_303A&PID_1002\n"
        ),
        stderr="",
    )
    fake_supported_devices = [
        SupportedDevice(
            name="device-1",
            for_firmware="heltec-v3",
            usb_vendor_id_in_hex="303A",
            usb_product_id_in_hex="1001",
        ),
        SupportedDevice(
            name="device-2",
            for_firmware="heltec-v3",
            usb_vendor_id_in_hex="303A",
            usb_product_id_in_hex="1002",
        ),
    ]

    assert active_ports_on_supported_devices(fake_supported_devices) == {
        "COM2",
        "COM3",
    }
    mock_run.assert_called_once()
    mock_platform.assert_called()


@pytest.mark.unit
@patch("meshtastic._port_discovery.glob.glob")
@patch("platform.system", return_value="Darwin")
def test_active_ports_on_supported_devices_mac_no_duplicates_check(
    mock_platform: MagicMock,
    mock_glob: MagicMock,
) -> None:
    """Test active_ports_on_supported_devices()."""
    mock_glob.return_value = [
        "/dev/cu.usbmodem53230051441",
        "/dev/cu.wchusbserial53230051441",
    ]
    fake_device = SupportedDevice(
        name="a", for_firmware="tbeam", baseport_on_mac="cu.usbmodem"
    )
    fake_supported_devices = [fake_device]
    assert active_ports_on_supported_devices(fake_supported_devices, False) == {
        "/dev/cu.usbmodem53230051441",
        "/dev/cu.wchusbserial53230051441",
    }
    mock_platform.assert_called()
    mock_glob.assert_called()


@pytest.mark.unit
@patch("meshtastic._port_discovery.glob.glob")
@patch("platform.system", return_value="Darwin")
def test_active_ports_on_supported_devices_mac_duplicates_check(
    mock_platform: MagicMock,
    mock_glob: MagicMock,
) -> None:
    """Ensure duplicate mac device entries are deduplicated when duplicate checking is enabled.

    Verifies that given a mac-style device listing containing two related device paths, active_ports_on_supported_devices(...)
    returns only the non-duplicate host port when the duplicates check is enabled.

    """
    mock_glob.return_value = [
        "/dev/cu.usbmodem53230051441",
        "/dev/cu.wchusbserial53230051441",
    ]
    fake_device = SupportedDevice(
        name="a", for_firmware="tbeam", baseport_on_mac="cu.usbmodem"
    )
    fake_supported_devices = [fake_device]
    assert active_ports_on_supported_devices(fake_supported_devices, True) == {
        "/dev/cu.wchusbserial53230051441"
    }
    mock_platform.assert_called()
    mock_glob.assert_called()


@pytest.mark.unit
def test_messageToJson_shows_all() -> None:
    """Test that messageToJson prints fields that aren't included in data passed in."""
    actual = json.loads(messageToJson(mesh_pb2.MyNodeInfo()))
    # Check that expected keys are present with expected values, rather than
    # asserting exact equality, to avoid fragility when protobuf schema adds fields.
    expected = {
        "myNodeNum": 0,
        "rebootCount": 0,
        "minAppVersion": 0,
        "deviceId": "",
        "pioEnv": "",
        "nodedbCount": 0,
    }
    for key, value in expected.items():
        assert (
            actual.get(key) == value
        ), f"Key {key}: expected {value}, got {actual.get(key)}"
    # firmwareEdition presence only — value depends on proto enum default name
    assert "firmwareEdition" in actual


@pytest.mark.unit
def test_message_to_json_alias_matches_messageToJson() -> None:
    """snake_case message_to_json shim should behave exactly like messageToJson."""
    message = mesh_pb2.MyNodeInfo()
    assert message_to_json(message) == messageToJson(message)
    assert message_to_json(message, multiline=True) == messageToJson(
        message, multiline=True
    )


@pytest.mark.unit
def test_acknowledgement_reset() -> None:
    """Test that the reset method can set all fields back to False."""
    ack = Acknowledgment()
    # everything's set to False; let's set it to True to get a good test
    ack.receivedAck = True
    ack.receivedNak = True
    ack.receivedImplAck = True
    ack.receivedTraceRoute = True
    ack.receivedTelemetry = True
    ack.receivedPosition = True
    ack.receivedWaypoint = True
    ack.reset()
    assert ack.receivedAck is False
    assert ack.receivedNak is False
    assert ack.receivedImplAck is False
    assert ack.receivedTraceRoute is False
    assert ack.receivedTelemetry is False
    assert ack.receivedPosition is False
    assert ack.receivedWaypoint is False


@pytest.mark.unitslow
@given(
    a_string=st.text(
        alphabet=st.characters(
            codec="ascii",
            min_codepoint=0x5F,
            max_codepoint=0x7A,
            exclude_characters=r"`",
        )
    ).filter(
        lambda x: x != "" and x[0] != "_" and x[-1] != "_" and not re.search(r"__", x)
    )
)
def test_roundtrip_snake_to_camel_camel_to_snake(a_string: str) -> None:
    """Test that snake_to_camel and camel_to_snake roundtrip each other."""
    value0 = snake_to_camel(a_string=a_string)
    value1 = camel_to_snake(a_string=value0)
    assert a_string == value1, (a_string, value1)


@pytest.mark.unitslow
@given(st.text(alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"))))
def test_fuzz_camel_to_snake(a_string: str) -> None:
    """Test that camel_to_snake lowercases output and preserves non-uppercase characters."""
    result = camel_to_snake(a_string)
    assert result == result.lower()  # output is always lowercase
    # Use casefold for unicode-stable comparisons. Some characters (e.g., Greek
    # sigma) have context-sensitive lowercase forms, while camel_to_snake lowers
    # character-by-character.
    src_counts = Counter(
        folded_char
        for char in a_string
        if char != "_"
        for folded_char in char.casefold()
    )
    res_counts = Counter(
        folded_char for char in result if char != "_" for folded_char in char.casefold()
    )
    assert res_counts == src_counts  # no chars dropped or multiplied


@pytest.mark.unitslow
@given(st.text())
def test_fuzz_snake_to_camel(a_string: str) -> None:
    """Test that snake_to_camel satisfies core invariants."""
    result = snake_to_camel(a_string)
    assert "_" not in result
    if "_" not in a_string:
        assert result == a_string


@pytest.mark.unit
def test_snake_to_camel_examples() -> None:
    """Test fixed snake_to_camel examples."""
    assert snake_to_camel("foo_bar") == "fooBar"
    assert snake_to_camel("alreadyCamel") == "alreadyCamel"


@pytest.mark.unitslow
@given(st.text())
def test_fuzz_stripnl(s: str) -> None:
    """Test that stripnl always takes away newlines."""
    result = stripnl(s)
    assert "\n" not in result


@pytest.mark.unitslow
@given(st.binary())
def test_fuzz_pskToString(psk: bytes) -> None:
    """Test that pskToString produces sane output for any bytes."""
    result = pskToString(psk)
    if len(psk) == 0:
        assert result == "unencrypted"
    elif len(psk) == 1:
        b = psk[0]
        if b == 0:
            assert result == "unencrypted"
        elif b == 1:
            assert result == "default"
        else:
            assert result == f"simple{b - 1}"
    else:
        assert result == "secret"


@pytest.mark.unitslow
@given(
    st.text(
        alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E),
        max_size=256,
    ).filter(lambda s: not s.startswith("0x") and not s.startswith("base64:"))
)
def test_fuzz_fromStr_non_prefixed(valstr: str) -> None:
    """Test fromStr behavior for non-prefixed string inputs."""
    result = fromStr(valstr)
    if len(valstr) == 0:
        assert result == b""
    elif valstr.lower() in {"t", "true", "yes"}:
        assert result is True
    elif valstr.lower() in {"f", "false", "no"}:
        assert result is False
    else:
        try:
            int(valstr)
            assert isinstance(result, int)
        except ValueError:
            try:
                float(valstr)
                assert isinstance(result, float)
            except ValueError:
                assert isinstance(result, str)


@pytest.mark.unitslow
@given(
    st.text(
        alphabet=st.sampled_from(list("0123456789abcdefABCDEF")),
        min_size=0,
        max_size=64,
    )
)
def test_fuzz_fromStr_hex_prefixed(hex_digits: str) -> None:
    """Test that fromStr decodes 0x-prefixed hex strings, including odd lengths."""
    expected_hex = hex_digits
    if len(expected_hex) == 0:
        expected_hex = "00"
    elif len(expected_hex) % 2 == 1:
        expected_hex = "0" + expected_hex
    assert fromStr(f"0x{hex_digits}") == bytes.fromhex(expected_hex)


@pytest.mark.unitslow
@given(
    st.text(
        alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E),
        min_size=1,
        max_size=64,
    ).filter(
        lambda s: (
            re.fullmatch(r"[0-9a-fA-F]+", s) is None
            and not any(ch.isspace() for ch in s)
        )
    )
)
def test_fuzz_fromStr_hex_invalid_raises(hex_digits: str) -> None:
    """Test that fromStr raises for invalid 0x-prefixed hex strings."""
    with pytest.raises(ValueError):
        fromStr(f"0x{hex_digits}")


@pytest.mark.unitslow
@given(st.binary(max_size=256))
def test_fuzz_fromStr_base64_roundtrip(raw_value: bytes) -> None:
    """Test that fromStr round-trips valid base64-prefixed payloads."""
    encoded = base64.b64encode(raw_value).decode("ascii")
    assert fromStr(f"base64:{encoded}") == raw_value


@pytest.mark.unitslow
@given(
    st.text(
        alphabet=st.sampled_from(list(_BASE64_ALLOWED_CHARS)),
        min_size=1,
        max_size=65,
    ).filter(lambda s: len(s) % 4 == 1)
)
def test_fuzz_fromStr_base64_malformed_raises(base64_payload: str) -> None:
    """Test that fromStr raises for malformed base64 payload lengths."""
    with pytest.raises(binascii.Error):
        fromStr(f"base64:{base64_payload}")


@st.composite
def _base64_payload_with_single_invalid_char(draw: st.DrawFn) -> str:
    """Generate base64-like payloads with valid length and exactly one invalid character."""
    quad_count = draw(st.integers(min_value=1, max_value=32))
    payload_len = quad_count * 4
    chars = draw(
        st.lists(
            st.sampled_from(list(_BASE64_ALLOWED_CHARS)),
            min_size=payload_len,
            max_size=payload_len,
        )
    )
    invalid_idx = draw(st.integers(min_value=0, max_value=payload_len - 1))
    chars[invalid_idx] = draw(st.sampled_from(list(_BASE64_INVALID_CHARS)))
    return "".join(chars)


@pytest.mark.unitslow
@given(_base64_payload_with_single_invalid_char())
def test_fuzz_fromStr_base64_invalid_chars_raises(base64_payload: str) -> None:
    """Test that fromStr raises for base64 payloads containing invalid characters."""
    with pytest.raises(binascii.Error):
        fromStr(f"base64:{base64_payload}")


@pytest.mark.unit
def test_shorthex() -> None:
    """Test the shortest hex string representations."""
    result = fromStr("0x0")
    assert result == b"\x00"
    result = fromStr("0x5")
    assert result == b"\x05"
    result = fromStr("0x123")
    assert result == b"\x01#"
    result = fromStr("0xffff")
    assert result == b"\xff\xff"


@pytest.mark.unit
def test_channel_hash_basics() -> None:
    """Test the default key and LongFast with channel_hash."""
    assert channel_hash(DEFAULT_KEY) == 2
    assert channel_hash("LongFast".encode("utf-8")) == 10


@pytest.mark.unitslow
@given(st.text(min_size=1, max_size=12))
def test_channel_hash_fuzz(channel_name: str) -> None:
    """Test channel_hash with fuzzed channel names, ensuring it produces single-byte values."""
    hashed = channel_hash(channel_name.encode("utf-8"))
    assert _HASH_BYTE_MIN <= hashed <= _HASH_BYTE_MAX


@pytest.mark.unit
def test_generate_channel_hash_basics() -> None:
    """Test the default key and LongFast/MediumFast with generate_channel_hash."""
    assert generate_channel_hash("LongFast", "AQ==") == 8
    assert generate_channel_hash("LongFast", bytes([1])) == 8
    assert generate_channel_hash("LongFast", DEFAULT_KEY) == 8
    assert generate_channel_hash("MediumFast", DEFAULT_KEY) == 31


@pytest.mark.unitslow
@given(st.text(min_size=1, max_size=12))
def test_generate_channel_hash_fuzz_default_key(channel_name: str) -> None:
    """Test generate_channel_hash with fuzzed channel names and the default key, ensuring it produces single-byte values."""
    hashed = generate_channel_hash(channel_name, DEFAULT_KEY)
    assert _HASH_BYTE_MIN <= hashed <= _HASH_BYTE_MAX


@pytest.mark.unitslow
@given(st.text(min_size=1, max_size=12), st.binary(min_size=1, max_size=1))
def test_generate_channel_hash_fuzz_simple(channel_name: str, key_bytes: bytes) -> None:
    """Test generate_channel_hash with fuzzed channel names and one-byte keys, ensuring it produces single-byte values."""
    hashed = generate_channel_hash(channel_name, key_bytes)
    assert _HASH_BYTE_MIN <= hashed <= _HASH_BYTE_MAX


@pytest.mark.unitslow
@given(st.text(min_size=1, max_size=12), st.binary(min_size=16, max_size=16))
def test_generate_channel_hash_fuzz_aes128(channel_name: str, key_bytes: bytes) -> None:
    """Test generate_channel_hash with fuzzed channel names and 128-bit keys, ensuring it produces single-byte values."""
    hashed = generate_channel_hash(channel_name, key_bytes)
    assert _HASH_BYTE_MIN <= hashed <= _HASH_BYTE_MAX


@pytest.mark.unitslow
@given(st.text(min_size=1, max_size=12), st.binary(min_size=32, max_size=32))
def test_generate_channel_hash_fuzz_aes256(channel_name: str, key_bytes: bytes) -> None:
    """Test generate_channel_hash with fuzzed channel names and 256-bit keys, ensuring it produces single-byte values."""
    hashed = generate_channel_hash(channel_name, key_bytes)
    assert _HASH_BYTE_MIN <= hashed <= _HASH_BYTE_MAX


@pytest.mark.unit
def test_tdeck_vid_pid_mapping() -> None:
    """Verify T-Deck device resolves correctly from VID 303a and PID 1001."""
    tdeck_devices = [
        d
        for d in supported_devices
        if d.usb_vendor_id_in_hex == "303a" and d.usb_product_id_in_hex == "1001"
    ]
    assert (
        len(tdeck_devices) == 1
    ), "Expected exactly one T-Deck device with VID 303a and PID 1001"
    assert (
        tdeck_devices[0].name == "T-Deck"
    ), f"Expected device name 'T-Deck', got '{tdeck_devices[0].name}'"
    assert (
        tdeck_devices[0].for_firmware == "t-deck"
    ), f"Expected for_firmware 't-deck', got '{tdeck_devices[0].for_firmware}'"


@pytest.mark.unit
def test_supported_device_usb_ids_include_aliases() -> None:
    """T-Deck and Seeed Xiao should include alternate USB VID/PID modes."""
    tdeck_devices = [d for d in supported_devices if d.name == "T-Deck"]
    assert len(tdeck_devices) == 1
    assert ("303a", "1001") in tdeck_devices[0].usb_ids
    assert ("1a86", "55d4") in tdeck_devices[0].usb_ids

    xiao_devices = [d for d in supported_devices if d.name == "Seeed Xiao ESP32-S3"]
    assert len(xiao_devices) == 1
    assert ("2886", "0059") in xiao_devices[0].usb_ids
    assert ("303a", "1001") in xiao_devices[0].usb_ids


def _resolve_device_by_vid_pid(
    devices: list[SupportedDevice], vendor_id: str, product_id: str
) -> SupportedDevice | None:
    """Resolve the first supported device for a VID/PID pair with direct-match precedence."""
    normalized_pair = (
        vendor_id.strip().lower().removeprefix("0x"),
        product_id.strip().lower().removeprefix("0x"),
    )
    for device in devices:
        if (
            device.usb_vendor_id_in_hex,
            device.usb_product_id_in_hex,
        ) == normalized_pair:
            return device
    for device in devices:
        if normalized_pair in device.usb_id_aliases:
            return device
    return None


@pytest.mark.unit
def test_supported_device_vid_pid_resolution_handles_overlapping_aliases() -> None:
    """Overlapping VID/PID aliases should resolve deterministically with direct-match precedence."""
    candidate_devices = [seeed_xiao_s3, tdeck]

    assert ("303a", "1001") in seeed_xiao_s3.usb_id_aliases
    assert ("303a", "1001") in tdeck.usb_ids
    assert ("1a86", "55d4") in tdeck.usb_id_aliases

    assert _resolve_device_by_vid_pid(candidate_devices, "303a", "1001") is tdeck
    assert (
        _resolve_device_by_vid_pid(candidate_devices, "2886", "0059") is seeed_xiao_s3
    )
    assert _resolve_device_by_vid_pid(candidate_devices, "1a86", "55d4") is tdeck


@pytest.mark.unit
def test_supported_device_post_init_normalizes_usb_ids() -> None:
    """SupportedDevice should normalize USB IDs."""
    device = SupportedDevice(
        name="Test Device",
        usb_vendor_id_in_hex="ABCD",
        usb_product_id_in_hex="EF01",
        usb_id_aliases=(("303A", "1001"),),
    )
    assert device.usb_vendor_id_in_hex == "abcd"
    assert device.usb_product_id_in_hex == "ef01"
    assert device.usb_id_aliases == (("303a", "1001"),)
    assert ("abcd", "ef01") in device.usb_ids


@pytest.mark.unit
def test_supported_device_post_init_accepts_list_aliases_and_deduplicates() -> None:
    """Alias normalization should accept list pairs and remove duplicates."""
    raw_aliases: Any = (
        ("303A", "1001"),
        ["303a", "1001"],
        (" 303a ", " 1001 "),
    )
    device = SupportedDevice(
        name="Test Device",
        usb_id_aliases=cast(tuple[tuple[str, str], ...], raw_aliases),
    )
    assert device.usb_id_aliases == (("303a", "1001"),)


@pytest.mark.unit
def test_supported_device_post_init_rejects_invalid_usb_aliases() -> None:
    """Invalid alias VID/PID values should fail fast during normalization."""
    with pytest.raises(
        SupportedDeviceValidationError, match="Invalid usb_id_aliases entry"
    ):
        SupportedDevice(name="Test Device", usb_id_aliases=(("ZZZZ", "1001"),))


@pytest.mark.unit
def test_supported_device_post_init_rejects_non_container_usb_aliases() -> None:
    """usb_id_aliases should reject non-list/tuple container values via validation error."""
    with pytest.raises(SupportedDeviceValidationError, match="expected tuple/list"):
        SupportedDevice(name="Test Device", usb_id_aliases=cast(Any, 1234))


@pytest.mark.unit
def test_supported_device_post_init_rejects_invalid_primary_usb_id() -> None:
    """Invalid primary VID/PID values should fail fast during normalization."""
    with pytest.raises(
        SupportedDeviceValidationError, match="Invalid usb_vendor_id_in_hex"
    ):
        SupportedDevice(
            name="Test Device",
            usb_vendor_id_in_hex="12",
            usb_product_id_in_hex="1001",
        )


@pytest.mark.unit
def test_supported_device_post_init_rejects_partial_primary_usb_id() -> None:
    """Primary USB vendor/product IDs must be provided together."""
    with pytest.raises(
        SupportedDeviceValidationError,
        match="must be provided together",
    ):
        SupportedDevice(name="Test Device", usb_vendor_id_in_hex="303a")


@pytest.mark.unit
def test_supported_device_post_init_rejects_non_string_primary_usb_id() -> None:
    """Primary USB IDs should reject non-string values with validation errors."""
    with pytest.raises(SupportedDeviceValidationError, match="expected str or None"):
        SupportedDevice(
            name="Test Device",
            usb_vendor_id_in_hex=cast(Any, 1234),
            usb_product_id_in_hex="1001",
        )


@pytest.mark.unit
def test_vendor_lookup_uses_alias_vids() -> None:
    """Vendor-based lookup should include devices matched through alias VID/PID pairs."""
    vids = get_unique_vendor_ids()
    assert "303a" in vids
    alias_devices = get_devices_with_vendor_id("303a")
    assert any(device.name == "Seeed Xiao ESP32-S3" for device in alias_devices)


# Tests for toStr
@pytest.mark.unit
def test_toStr_bytes() -> None:
    """Test toStr with bytes input."""

    result = toStr(b"hello")
    assert result == "base64:aGVsbG8="
    assert isinstance(result, str)


@pytest.mark.unit
def test_toStr_string() -> None:
    """Test toStr with string input."""

    result = toStr("hello")
    assert result == "hello"


@pytest.mark.unit
def test_toStr_int() -> None:
    """Test toStr with int input."""

    result = toStr(123)
    assert result == "123"


@pytest.mark.unit
def test_toStr_float() -> None:
    """Test toStr with float input."""

    result = toStr(3.14)
    assert result == "3.14"


@pytest.mark.unit
def test_toStr_bool() -> None:
    """Test toStr with bool input."""

    assert toStr(True) == "True"
    assert toStr(False) == "False"


@pytest.mark.unit
def test_toStr_none() -> None:
    """Test toStr with None input."""

    result = toStr(None)
    assert result == "None"


@pytest.mark.unitslow
@given(st.binary())
def test_fuzz_toStr_bytes_roundtrip(raw_bytes: bytes) -> None:
    """Test that toStr produces valid base64 for any bytes."""

    result = toStr(raw_bytes)
    assert result.startswith("base64:")
    # Verify it's valid base64
    encoded = result[7:]  # Remove "base64:" prefix
    decoded = base64.b64decode(encoded)
    assert decoded == raw_bytes


# Tests for toNodeNum
@pytest.mark.unit
def test_toNodeNum_int() -> None:
    """Test toNodeNum with int input."""

    assert toNodeNum(123456789) == 123456789


@pytest.mark.unit
def test_toNodeNum_string_decimal() -> None:
    """Test toNodeNum with decimal string."""

    assert toNodeNum("123456789") == 123456789


@pytest.mark.unit
def test_toNodeNum_string_hex_with_prefix() -> None:
    """Test toNodeNum with hex string with 0x prefix."""

    assert toNodeNum("0x1234abcd") == 0x1234ABCD


@pytest.mark.unit
def test_toNodeNum_string_hex_without_prefix() -> None:
    """Test toNodeNum with hex string without 0x prefix (falls back to hex)."""

    # When decimal parse fails, it should try hex
    assert toNodeNum("deadbeef") == 0xDEADBEEF


@pytest.mark.unit
def test_toNodeNum_string_with_bang_prefix() -> None:
    """Test toNodeNum with ! prefix (node ID format)."""

    assert toNodeNum("!12345678") == 12345678
    assert toNodeNum("!0xabcdef") == 0xABCDEF


@pytest.mark.unit
def test_toNodeNum_string_with_bang_and_hex() -> None:
    """Test toNodeNum with !0x prefix."""

    assert toNodeNum("!0x1234") == 0x1234


@pytest.mark.unitslow
@given(st.integers(min_value=0, max_value=0xFFFFFFFF))
def test_fuzz_toNodeNum_int_roundtrip(node_num: int) -> None:
    """Test that toNodeNum preserves any valid node number."""

    result = toNodeNum(node_num)
    assert result == node_num


# Tests for flagsToList
@pytest.mark.unit
def test_flagsToList_single_flag() -> None:
    """Test flagsToList with single flag."""

    # Create a simple mock enum wrapper
    class MockEnum:
        """Minimal enum-like wrapper used by flagsToList tests."""

        @staticmethod
        def keys() -> list[str]:
            """Return enum key names."""
            return ["FLAG_A", "FLAG_B", "EXCLUDED_NONE"]

        @staticmethod
        def Value(name: str) -> int:
            """Return integer value for enum key."""
            return {"FLAG_A": 1, "FLAG_B": 2, "EXCLUDED_NONE": 0}[name]

    result = flagsToList(MockEnum, 1)  # pyright: ignore[reportArgumentType]
    assert "FLAG_A" in result


@pytest.mark.unit
def test_flagsToList_zero_flags() -> None:
    """Test flagsToList with zero flags."""

    class MockEnum:
        """Minimal enum-like wrapper used by flagsToList tests."""

        @staticmethod
        def keys() -> list[str]:
            """Return enum key names."""
            return ["FLAG_A", "FLAG_B"]

        @staticmethod
        def Value(name: str) -> int:
            """Return integer value for enum key."""
            return {"FLAG_A": 1, "FLAG_B": 2}[name]

    result = flagsToList(MockEnum, 0)  # pyright: ignore[reportArgumentType]
    assert not result


@pytest.mark.unit
def test_flagsToList_unknown_flags() -> None:
    """Test flagsToList with unknown flag bits."""

    class MockEnum:
        """Minimal enum-like wrapper used by flagsToList tests."""

        @staticmethod
        def keys() -> list[str]:
            """Return enum key names."""
            return ["FLAG_A"]

        @staticmethod
        def Value(name: str) -> int:
            """Return integer value for enum key."""
            return {"FLAG_A": 1}[name]

    # Use a value with unknown bits
    result = flagsToList(MockEnum, 0xFFFF)  # pyright: ignore[reportArgumentType]
    assert "FLAG_A" in result
    assert any("UNKNOWN_ADDITIONAL_FLAGS" in item for item in result)


# Tests for our_exit
@pytest.mark.unit
def test_our_exit_default_return_code(capsys: pytest.CaptureFixture[str]) -> None:
    """Test our_exit with default return code (1)."""

    with pytest.raises(SystemExit) as exc_info:
        our_exit("Error message")

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Error message" in captured.err


@pytest.mark.unit
def test_our_exit_custom_return_code(capsys: pytest.CaptureFixture[str]) -> None:
    """Test our_exit with custom return code."""

    with pytest.raises(SystemExit) as exc_info:
        our_exit("Error message", return_value=42)

    assert exc_info.value.code == 42
    captured = capsys.readouterr()
    assert "Error message" in captured.err


@pytest.mark.unit
def test_our_exit_zero_return_code(capsys: pytest.CaptureFixture[str]) -> None:
    """Test our_exit with return code 0 (success)."""

    with pytest.raises(SystemExit) as exc_info:
        our_exit("Success", return_value=0)

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    # Return code 0 should write to stdout
    assert "Success" in captured.out


@pytest.mark.unit
def test_our_exit_empty_message(capsys: pytest.CaptureFixture[str]) -> None:
    """Test our_exit with empty message."""

    with pytest.raises(SystemExit) as exc_info:
        our_exit("")

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    # Empty message still emits newline
    assert captured.err == "\n"


# Tests for DeferredExecution
@pytest.mark.unit
def test_deferred_execution_runs_closure() -> None:
    """Test that DeferredExecution runs closures in background thread."""

    first = threading.Event()
    second = threading.Event()
    de = DeferredExecution(name="test_thread")

    try:
        de.queueWork(first.set)
        de.queueWork(second.set)
        assert first.wait(timeout=1.0)
        assert second.wait(timeout=1.0)
    finally:
        de.stop()
        de.join(timeout=1.0)


@pytest.mark.unit
def test_deferred_execution_handles_exceptions(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that DeferredExecution logs exceptions without crashing."""

    completion = threading.Event()
    de = DeferredExecution(name="test_exception_thread")

    try:
        # Queue work that raises exception
        def bad_closure() -> None:
            raise ValueError("Test exception")

        def signal_completion() -> None:
            completion.set()

        with caplog.at_level(logging.DEBUG):
            de.queueWork(bad_closure)
            de.queueWork(signal_completion)
            assert completion.wait(timeout=1.0)

        # Should have logged the exception
        assert "Unexpected error in deferred execution" in caplog.text
    finally:
        de.stop()
        de.join(timeout=1.0)


# Tests for Timeout.waitForAckNak
@pytest.mark.unit
def test_timeout_waitForAckNak_timeout() -> None:
    """Test Timeout.waitForAckNak when timeout expires."""

    to = Timeout(0.1)  # Very short timeout
    ack = Acknowledgment()

    result = to.waitForAckNak(ack)
    assert result is False


@pytest.mark.unit
def test_timeout_waitForAckNak_received() -> None:
    """Test Timeout.waitForAckNak when ack received."""

    to = Timeout(5.0)  # Long timeout
    ack = Acknowledgment()

    # Set ack flag immediately
    ack.receivedAck = True

    result = to.waitForAckNak(ack)
    assert result is True
    # Should reset after reading
    assert ack.receivedAck is False


@pytest.mark.unit
def test_timeout_waitForAckNak_custom_attrs() -> None:
    """Test Timeout.waitForAckNak with custom attribute names."""

    to = Timeout(5.0)
    ack = Acknowledgment()

    # Set nak flag
    ack.receivedNak = True

    result = to.waitForAckNak(ack, attrs=("receivedNak",))
    assert result is True


# Tests for Timeout.waitForTelemetry
@pytest.mark.unit
def test_timeout_waitForTelemetry_timeout() -> None:
    """Test Timeout.waitForTelemetry when timeout expires."""

    to = Timeout(0.1)
    ack = Acknowledgment()

    result = to.waitForTelemetry(ack)
    assert result is False


@pytest.mark.unit
def test_timeout_waitForTelemetry_received() -> None:
    """Test Timeout.waitForTelemetry when telemetry received."""

    to = Timeout(5.0)
    ack = Acknowledgment()
    ack.receivedTelemetry = True

    result = to.waitForTelemetry(ack)
    assert result is True


# Tests for Timeout.waitForPosition
@pytest.mark.unit
def test_timeout_waitForPosition_timeout() -> None:
    """Test Timeout.waitForPosition when timeout expires."""

    to = Timeout(0.1)
    ack = Acknowledgment()

    result = to.waitForPosition(ack)
    assert result is False


@pytest.mark.unit
def test_timeout_waitForPosition_received() -> None:
    """Test Timeout.waitForPosition when position received."""

    to = Timeout(5.0)
    ack = Acknowledgment()
    ack.receivedPosition = True

    result = to.waitForPosition(ack)
    assert result is True


# Tests for Timeout.waitForWaypoint
@pytest.mark.unit
def test_timeout_waitForWaypoint_timeout() -> None:
    """Test Timeout.waitForWaypoint when timeout expires."""

    to = Timeout(0.1)
    ack = Acknowledgment()

    result = to.waitForWaypoint(ack)
    assert result is False


@pytest.mark.unit
def test_timeout_waitForWaypoint_received() -> None:
    """Test Timeout.waitForWaypoint when waypoint received."""

    to = Timeout(5.0)
    ack = Acknowledgment()
    ack.receivedWaypoint = True

    result = to.waitForWaypoint(ack)
    assert result is True


# Tests for Timeout.waitForTraceRoute
@pytest.mark.unit
def test_timeout_waitForTraceRoute_timeout() -> None:
    """Test Timeout.waitForTraceRoute when timeout expires."""

    to = Timeout(0.1)
    ack = Acknowledgment()

    result = to.waitForTraceRoute(1.0, ack)
    assert result is False


@pytest.mark.unit
def test_timeout_waitForTraceRoute_received() -> None:
    """Test Timeout.waitForTraceRoute when traceroute received."""

    to = Timeout(5.0)
    ack = Acknowledgment()
    ack.receivedTraceRoute = True

    result = to.waitForTraceRoute(1.0, ack)
    assert result is True


# Tests for DotDict
@pytest.mark.unit
def test_dotdict_getattr() -> None:
    """Test DotDict attribute access."""

    dd = DotDict({"key": "value"})
    assert dd.key == "value"  # type: ignore[attr-defined]


@pytest.mark.unit
def test_dotdict_setattr() -> None:
    """Test DotDict attribute setting."""

    dd = DotDict()
    dd.key = "value"  # type: ignore[attr-defined]
    assert dd["key"] == "value"


@pytest.mark.unit
def test_dotdict_delattr() -> None:
    """Test DotDict attribute deletion."""

    dd = DotDict({"key": "value"})
    del dd.key  # type: ignore[attr-defined]
    assert "key" not in dd


@pytest.mark.unit
def test_dotdict_missing_attr_returns_none() -> None:
    """Test DotDict returns None for missing attributes."""

    dd = DotDict()
    assert dd.nonexistent is None  # type: ignore[attr-defined]


@pytest.mark.unit
def test_dotdict_deprecated_warns() -> None:
    """Test dotdict deprecated alias warns once per process."""
    # Clear warn-once state to ensure we get a warning
    from meshtastic.util import (  # pylint: disable=import-outside-toplevel
        _warned_deprecations,
        _warned_deprecations_lock,
    )

    with _warned_deprecations_lock:
        _warned_deprecations.discard("dotdict")

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always", DeprecationWarning)
        dd = dotdict()  # pyright: ignore[reportDeprecated]
        _ = dotdict()  # pyright: ignore[reportDeprecated]
    assert isinstance(dd, dict)
    assert len(captured) == 1
    assert issubclass(captured[0].category, DeprecationWarning)
    assert "dotdict" in str(captured[0].message).lower()
