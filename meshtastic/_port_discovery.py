"""Private serial and USB port discovery helpers."""

from __future__ import annotations

import glob
import logging
import os
import platform
import re
import subprocess
from collections.abc import Callable, Iterable
from typing import Any

import serial.tools.list_ports  # type: ignore[import-untyped]

from meshtastic.supported_device import SupportedDevice, supported_devices

logger = logging.getLogger(__name__)

_LINUX_SERIAL_BY_ID_DIR = "/dev/serial/by-id"


def _linux_by_id_aliases() -> dict[str, str]:
    """Map Linux tty realpaths to stable /dev/serial/by-id aliases."""
    if platform.system() != "Linux":
        return {}
    if not os.path.isdir(_LINUX_SERIAL_BY_ID_DIR):
        return {}
    aliases: dict[str, str] = {}
    for alias in glob.glob(f"{_LINUX_SERIAL_BY_ID_DIR}/*"):
        try:
            resolved = os.path.realpath(alias)
        except OSError:
            continue
        if resolved:
            aliases[resolved] = alias
    return aliases


def _find_ports(
    *,
    eliminate_duplicates: bool,
    blacklist_vids: set[int],
    whitelist_vids: set[int],
    eliminate_duplicate_port_fn: Callable[[list[str]], list[str]],
) -> list[str]:
    """Return sorted serial device paths likely to be Meshtastic radios."""
    all_ports = serial.tools.list_ports.comports()

    ports: list[str] = [
        port.device
        for port in all_ports
        if port.vid is not None and port.vid in whitelist_vids
    ]

    if not ports:
        ports = [
            port.device
            for port in all_ports
            if port.vid is not None and port.vid not in blacklist_vids
        ]

    alias_by_realpath = _linux_by_id_aliases()
    if alias_by_realpath:
        mapped_ports: list[str] = []
        for device in ports:
            try:
                resolved_device = os.path.realpath(device)
            except OSError:
                resolved_device = device
            mapped_ports.append(alias_by_realpath.get(resolved_device, device))
        ports = list(dict.fromkeys(mapped_ports))

    ports.sort()
    if eliminate_duplicates:
        ports = eliminate_duplicate_port_fn(ports)
    return ports


def _detect_supported_devices() -> set[SupportedDevice]:
    """Detect supported USB devices attached to the host."""
    system = platform.system()
    possible_devices: set[SupportedDevice] = set()
    if system == "Linux":
        _, lsusb_output = subprocess.getstatusoutput("lsusb")
        for vid in _get_unique_vendor_ids():
            if re.search(f" {vid}:", lsusb_output, re.MULTILINE):
                possible_devices.update(_get_devices_with_vendor_id(vid))
    elif system == "Windows":
        _, sp_output = subprocess.getstatusoutput(
            'powershell.exe "[Console]::OutputEncoding = [Text.UTF8Encoding]::UTF8;'
            'Get-PnpDevice -PresentOnly | Format-List"'
        )
        for vid in _get_unique_vendor_ids():
            if re.search(f"DeviceID.*{vid.upper()}&", sp_output, re.MULTILINE):
                possible_devices.update(_get_devices_with_vendor_id(vid))
    elif system == "Darwin":
        _, sp_output = subprocess.getstatusoutput("system_profiler SPUSBDataType")
        for vid in _get_unique_vendor_ids():
            if re.search(f"Vendor ID: 0x{vid}", sp_output, re.MULTILINE):
                possible_devices.update(_get_devices_with_vendor_id(vid))
    return possible_devices


def _detect_windows_needs_driver(sd: Any, print_reason: bool = False) -> bool:
    """Return whether Windows reports a failed driver install for a supported device."""
    if not sd or platform.system() != "Windows":
        return False

    command = (
        'powershell.exe "[Console]::OutputEncoding = [Text.UTF8Encoding]::UTF8; '
        "Get-PnpDevice | Where-Object{ ($_.DeviceId -like "
        f"'*{sd.usb_vendor_id_in_hex.upper()}*'"
        ')} | Format-List"'
    )
    _, sp_output = subprocess.getstatusoutput(command)
    needs_driver = bool(re.search("CM_PROB_FAILED_INSTALL", sp_output, re.MULTILINE))
    if needs_driver and print_reason:
        logger.debug(sp_output)
    return needs_driver


def _eliminate_duplicate_port(ports: list[str]) -> list[str]:
    """Collapse likely duplicate serial device paths to one representative."""
    if len(ports) != 2:
        return ports

    sorted_ports = sorted(ports)
    first_port, second_port = sorted_ports
    if "usbserial" in first_port and "wchusbserial" in second_port:
        first = first_port.replace("usbserial-", "")
        second = second_port.replace("wchusbserial", "")
        if first == second:
            return [second_port]
    elif "usbmodem" in first_port and "wchusbserial" in second_port:
        first = first_port.replace("usbmodem", "")
        second = second_port.replace("wchusbserial", "")
        if first == second:
            return [second_port]
    elif "SLAB_USBtoUART" in first_port and "usbserial" in second_port:
        return [second_port]
    return ports


def _is_windows11() -> bool:
    """Return whether the host appears to be Windows 11."""
    if platform.system() != "Windows":
        return False
    try:
        if float(platform.release()) < 10.0:
            return False
        patch = platform.version().split(".")[2][:5]
        return int(patch) >= 22000
    except Exception:
        logger.exception("Problem detecting Windows 11")
    return False


def _get_unique_vendor_ids() -> set[str]:
    """Collect normalized USB vendor IDs from supported-device metadata."""
    vids: set[str] = set()
    for device in supported_devices:
        usb_ids = device.usb_ids
        if usb_ids:
            vids.update(vendor_id for vendor_id, _ in usb_ids)
        elif device.usb_vendor_id_in_hex:
            vids.add(device.usb_vendor_id_in_hex)
    return vids


def _get_devices_with_vendor_id(vid: str) -> set[SupportedDevice]:
    """Return supported devices that match a USB vendor ID."""
    normalized_vid = vid.strip().lower().removeprefix("0x")
    matching_devices: set[SupportedDevice] = set()
    for device in supported_devices:
        if any(
            isinstance(vendor_id, str)
            and vendor_id.lower().removeprefix("0x") == normalized_vid
            for vendor_id, _ in device.usb_ids or ()
        ):
            matching_devices.add(device)
            continue
        if (
            isinstance(device.usb_vendor_id_in_hex, str)
            and device.usb_vendor_id_in_hex.lower().removeprefix("0x")
            == normalized_vid
        ):
            matching_devices.add(device)
    return matching_devices


def _discover_unix_ports(base_port: str) -> set[str]:
    """Discover Unix serial-device paths matching one base-port prefix."""
    return set(glob.glob(f"/dev/{base_port}*"))


def _active_ports_on_supported_devices(
    sds: Iterable[SupportedDevice],
    *,
    eliminate_duplicates: bool,
    detect_windows_port_fn: Callable[[SupportedDevice | None], set[str]],
    eliminate_duplicate_port_fn: Callable[[list[str]], list[str]],
) -> set[str]:
    """Collect active serial ports for supported devices on the current platform."""
    ports: set[str] = set()
    baseports: set[str] = set()
    system = platform.system()

    for device in sds:
        if system == "Linux" and device.baseport_on_linux is not None:
            baseports.add(device.baseport_on_linux)
        elif system == "Darwin" and device.baseport_on_mac is not None:
            baseports.add(device.baseport_on_mac)

    if system in ("Linux", "Darwin"):
        for base_port in baseports:
            ports |= _discover_unix_ports(base_port)
    elif system == "Windows":
        for device in sds:
            ports.update(detect_windows_port_fn(device))

    if eliminate_duplicates:
        port_list = eliminate_duplicate_port_fn(list(ports))
        port_list.sort()
        ports = set(port_list)
    return ports


def _detect_windows_port(sd: SupportedDevice | None) -> set[str]:
    """Detect Windows COM ports associated with a supported USB device."""
    ports: set[str] = set()
    if not sd or platform.system() != "Windows":
        return ports

    usb_ids = sd.usb_ids
    if (
        not usb_ids
        and sd.usb_vendor_id_in_hex is not None
        and sd.usb_product_id_in_hex is not None
    ):
        usb_ids = ((sd.usb_vendor_id_in_hex, sd.usb_product_id_in_hex),)
    for vendor_id, product_id in usb_ids or ():
        filters = [f"($_.DeviceId -like '*{vendor_id.upper()}*')"]
        if product_id:
            filters.append(f"($_.DeviceId -like '*{product_id.upper()}*')")
        where_clause = " -and ".join(filters)
        command = (
            'powershell.exe "[Console]::OutputEncoding = [Text.UTF8Encoding]::UTF8;'
            f"Get-PnpDevice -PresentOnly | Where-Object{{ {where_clause} }} | "
            'Format-List"'
        )
        _, sp_output = subprocess.getstatusoutput(command)
        for com_suffix in re.compile(r"\(COM(.*)\)").findall(sp_output):
            ports.add(f"COM{com_suffix}")
    return ports
