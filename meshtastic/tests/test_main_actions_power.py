"""Meshtastic unit tests for __main__.py."""

# pylint: disable=C0302,W0613,R0917

import logging
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import IO, Any, cast
from unittest.mock import MagicMock, call, mock_open, patch

import pytest
import yaml

import meshtastic.__main__ as main_module
from meshtastic import mt_config
from meshtastic.__main__ import (
    _create_power_meter,
    main,
    onNode,
    printConfig,
    tunnelMain,
)

# from ..ble_interface import BLEInterface
from ..node import Node

# from ..radioconfig_pb2 import UserPreferences
# import meshtastic.config_pb2
from ..ota import OTAError, OTATransportError
from ..protobuf.channel_pb2 import Channel  # pylint: disable=E0611
from ..serial_interface import SerialInterface
from ..tcp_interface import TCPInterface
from ._main_legacy_support import (
    _build_configure_interface,
    _make_fake_tcp_interface,
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
        patch("os.path.isfile", return_value=True),
        pytest.raises(SystemExit) as excinfo,
    ):
        main()

    _, err = capsys.readouterr()
    assert "OTA update currently requires a TCP connection" in err
    assert excinfo.value.code == 1


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
        patch("os.path.isfile", return_value=True),
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
        patch("os.path.isfile", return_value=True),
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
        patch("os.path.isfile", return_value=True),
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
        patch("os.path.isfile", return_value=True),
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
        patch("os.path.isfile", return_value=True),
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
        patch("os.path.isfile", return_value=True),
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
    """_create_power_meter should fail fast if parsed CLI arguments are unavailable."""
    monkeypatch.setattr(main_module, "meter", None)
    monkeypatch.setattr(mt_config, "args", None)

    with pytest.raises(
        RuntimeError,
        match="CLI arguments must be initialized before calling _create_power_meter",
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
    monkeypatch.setattr(main_module, "have_powermon", True)
    monkeypatch.setattr(main_module, "RidenPowerSupply", object)
    monkeypatch.setattr(main_module, "PPK2PowerSupply", object)
    monkeypatch.setattr(main_module, "SimPowerSupply", lambda: fake_meter)
    monkeypatch.setattr(mt_config, "args", args)
    sleep_mock = MagicMock()
    time_attrs = vars(main_module.time).copy()
    time_attrs["sleep"] = sleep_mock
    monkeypatch.setattr(
        main_module, "time", SimpleNamespace(**time_attrs), raising=True
    )

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

    def mock_showInfo(file: IO[str] | None = None) -> str:
        print("inside mocked showInfo", file=file)
        return "inside mocked showInfo"

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

    def mock_showInfo(file: IO[str] | None = None) -> str:
        print("inside mocked showInfo", file=file)
        return "inside mocked showInfo"

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

    def mock_showInfo(file: IO[str] | None = None) -> str:
        print("inside mocked showInfo", file=file)
        return "inside mocked showInfo"

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
def test_create_power_meter_validates_ppk2_voltage_before_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PPK2 voltage validation should fail before opening the hardware backend."""
    args = SimpleNamespace(
        power_voltage=None,
        power_riden=None,
        power_ppk2_supply=False,
        power_ppk2_meter=True,
        power_sim=False,
        power_wait=False,
    )
    constructor = MagicMock()
    monkeypatch.setattr(main_module, "meter", None)
    monkeypatch.setattr(main_module, "have_powermon", True)
    monkeypatch.setattr(main_module, "RidenPowerSupply", object)
    monkeypatch.setattr(main_module, "PPK2PowerSupply", constructor)
    monkeypatch.setattr(main_module, "SimPowerSupply", object)
    monkeypatch.setattr(mt_config, "args", args)

    with pytest.raises(SystemExit):
        _create_power_meter()

    constructor.assert_not_called()


@pytest.mark.unit
def test_create_power_meter_rejects_nan_voltage_before_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-finite voltage input must not reach a hardware backend."""
    constructor = MagicMock()
    args = SimpleNamespace(
        power_voltage="nan",
        power_riden="/dev/ttyUSB0",
        power_ppk2_supply=False,
        power_ppk2_meter=False,
        power_sim=False,
        power_wait=False,
    )
    monkeypatch.setattr(main_module, "meter", None)
    monkeypatch.setattr(main_module, "have_powermon", True)
    monkeypatch.setattr(main_module, "RidenPowerSupply", constructor)
    monkeypatch.setattr(main_module, "PPK2PowerSupply", object)
    monkeypatch.setattr(main_module, "SimPowerSupply", object)
    monkeypatch.setattr(mt_config, "args", args)

    with pytest.raises(SystemExit):
        _create_power_meter()

    constructor.assert_not_called()


@pytest.mark.unit
def test_create_power_meter_closes_partial_backend_and_preserves_previous_global(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configuration failures should close the new meter without replacing the old one."""
    previous = MagicMock()
    candidate = MagicMock()
    candidate.setVoltage.side_effect = RuntimeError("configure failed")
    args = SimpleNamespace(
        power_voltage="3.3",
        power_riden=None,
        power_ppk2_supply=False,
        power_ppk2_meter=False,
        power_sim=True,
        power_wait=False,
    )
    monkeypatch.setattr(main_module, "meter", previous)
    monkeypatch.setattr(main_module, "have_powermon", True)
    monkeypatch.setattr(main_module, "RidenPowerSupply", object)
    monkeypatch.setattr(main_module, "PPK2PowerSupply", object)
    monkeypatch.setattr(
        main_module, "SimPowerSupply", MagicMock(return_value=candidate)
    )
    monkeypatch.setattr(mt_config, "args", args)

    with pytest.raises(RuntimeError, match="configure failed"):
        _create_power_meter()

    candidate.close.assert_called_once_with()
    previous.close.assert_not_called()
    assert main_module.meter is previous


@pytest.mark.unit
def test_create_power_meter_replaces_and_closes_previous_meter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful meter replacement should release the previous backend transport."""
    previous = MagicMock()
    replacement = MagicMock()
    args = SimpleNamespace(
        power_voltage=None,
        power_riden=None,
        power_ppk2_supply=False,
        power_ppk2_meter=False,
        power_sim=True,
        power_wait=False,
    )
    monkeypatch.setattr(main_module, "meter", previous)
    monkeypatch.setattr(main_module, "have_powermon", True)
    monkeypatch.setattr(main_module, "RidenPowerSupply", object)
    monkeypatch.setattr(main_module, "PPK2PowerSupply", object)
    monkeypatch.setattr(
        main_module, "SimPowerSupply", MagicMock(return_value=replacement)
    )
    monkeypatch.setattr(mt_config, "args", args)

    _create_power_meter()

    assert main_module.meter is replacement
    previous.close.assert_called_once_with()


@pytest.mark.unit
def test_release_session_power_meter_closes_and_clears_matching_global(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invocation meter cleanup should release hardware and clear the legacy alias."""
    active_meter = MagicMock()
    monkeypatch.setattr(main_module, "meter", active_meter)

    main_module._release_session_power_meter(active_meter)

    active_meter.close.assert_called_once_with()
    assert main_module.meter is None


@pytest.mark.unit
def test_release_session_power_meter_does_not_clear_replacement_global(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Late cleanup must not erase a meter installed by a newer invocation."""
    old_meter = MagicMock()
    replacement = MagicMock()
    monkeypatch.setattr(main_module, "meter", replacement)

    main_module._release_session_power_meter(old_meter)

    old_meter.close.assert_called_once_with()
    assert main_module.meter is replacement
