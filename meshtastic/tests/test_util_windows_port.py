"""Targeted tests for Windows COM-port detection helpers in util.py."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from meshtastic.supported_device import SupportedDevice
from meshtastic.util import detect_windows_port, detectWindowsPort


@pytest.mark.unit
@patch("platform.system", return_value="Windows")
@patch("subprocess.run")
def test_detectWindowsPort_parses_com_port_from_powershell_output(
    mock_run: MagicMock,
    _mock_system: MagicMock,
) -> None:
    """DetectWindowsPort should parse COM ports from PowerShell output on Windows."""
    mock_run.return_value = subprocess.CompletedProcess(
        args=["powershell.exe"],
        returncode=0,
        stdout=(
            "Name     : Meshtastic Serial (COM12) Extra (ignored)\n"
            "DeviceID : USB\\\\VID_303A&PID_1001\n\n"
            "Name     : Other Serial (COM7)\n"
            "DeviceID : USB\\\\VID_303A&PID_9999\n"
        ),
        stderr="",
    )
    device = SupportedDevice(
        name="x",
        for_firmware="heltec-v3",
        usb_vendor_id_in_hex="303A",
        usb_product_id_in_hex="1001",
    )

    assert detectWindowsPort(device) == {"COM12"}
    command = mock_run.call_args.args[0]
    assert command[:3] == ["powershell.exe", "-NoProfile", "-Command"]
    assert "303A" not in " ".join(command)


@pytest.mark.unit
@patch("meshtastic.util.detectWindowsPort", return_value={"COM7"})
def test_detect_windows_port_alias_delegates(
    wrapped: MagicMock,
) -> None:
    """detect_windows_port should delegate to detectWindowsPort."""
    device = SupportedDevice(name="x", for_firmware="heltec-v3")

    assert detect_windows_port(device) == {"COM7"}
    wrapped.assert_called_once_with(device)
