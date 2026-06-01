"""Private serial and USB port discovery helpers."""

from __future__ import annotations

import glob
import logging
import os
import platform
import re
import subprocess
from collections.abc import Callable, Iterable

import serial.tools.list_ports  # type: ignore[import-untyped]

from meshtastic.supported_device import (
    USB_ID_HEX_RE,
    SupportedDevice,
    supported_devices,
)

logger = logging.getLogger(__name__)

_LINUX_SERIAL_BY_ID_DIR = "/dev/serial/by-id"
_POWERSHELL_TIMEOUT_SECONDS = 10
_COM_PORT_RE = re.compile(r"\(COM(\d+)\)")
_PNP_DEVICE_BLOCK_RE = re.compile(r"(?:\r?\n){2,}")


def _normalize_usb_hex_id(raw_value: object, *, field_name: str) -> str | None:
    """Return a validated uppercase USB VID/PID, or None for unusable values."""
    if not isinstance(raw_value, str):
        return None
    normalized = raw_value.strip().lower().removeprefix("0x")
    if not USB_ID_HEX_RE.fullmatch(normalized):
        logger.debug("Ignoring invalid USB %s: %r", field_name, raw_value)
        return None
    return normalized.upper()


def _run_powershell(command: str) -> str:
    """Run a PowerShell command without a shell and return stdout."""
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                f"[Console]::OutputEncoding = [Text.UTF8Encoding]::UTF8; {command}",
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=_POWERSHELL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        logger.debug(
            "PowerShell command timed out after %s seconds",
            _POWERSHELL_TIMEOUT_SECONDS,
        )
        return ""
    except OSError:
        logger.debug("Unable to run PowerShell command", exc_info=True)
        return ""
    if completed.returncode != 0:
        logger.debug("PowerShell command failed: %s", completed.stderr.strip())
    return completed.stdout


def _windows_pnp_device_output(*, present_only: bool) -> str:
    """Return formatted Windows PnP device output for Python-side filtering."""
    present_only_arg = " -PresentOnly" if present_only else ""
    return _run_powershell(f"Get-PnpDevice{present_only_arg} | Format-List")


def _iter_pnp_device_blocks(output: str) -> Iterable[str]:
    """Yield non-empty Format-List device blocks from PowerShell output."""
    for block in _PNP_DEVICE_BLOCK_RE.split(output):
        block = block.strip()
        if block:
            yield block


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
        sp_output_upper = _windows_pnp_device_output(present_only=True).upper()
        for vid in _get_unique_vendor_ids():
            normalized_vid = _normalize_usb_hex_id(vid, field_name="vendor_id")
            if normalized_vid is None:
                continue
            if f"VID_{normalized_vid}" in sp_output_upper:
                possible_devices.update(_get_devices_with_vendor_id(vid))
    elif system == "Darwin":
        _, sp_output = subprocess.getstatusoutput("system_profiler SPUSBDataType")
        for vid in _get_unique_vendor_ids():
            if re.search(f"Vendor ID: 0x{vid}", sp_output, re.MULTILINE):
                possible_devices.update(_get_devices_with_vendor_id(vid))
    return possible_devices


def _detect_windows_needs_driver(
    sd: SupportedDevice | None,
    *,
    log_reason: bool = False,
) -> bool:
    """Return whether Windows reports a failed driver install for a supported device."""
    if not sd or platform.system() != "Windows":
        return False
    usb_ids = getattr(sd, "usb_ids", None)
    if (
        not usb_ids
        and getattr(sd, "usb_vendor_id_in_hex", None) is not None
        and getattr(sd, "usb_product_id_in_hex", None) is not None
    ):
        usb_ids = ((sd.usb_vendor_id_in_hex, sd.usb_product_id_in_hex),)
    if not usb_ids:
        return False

    device_blocks = tuple(
        _iter_pnp_device_blocks(
            _windows_pnp_device_output(present_only=False),
        )
    )
    matching_blocks: list[str] = []
    for vendor_id, product_id in usb_ids or ():
        normalized_vendor_id = _normalize_usb_hex_id(
            vendor_id,
            field_name="vendor_id",
        )
        normalized_product_id = _normalize_usb_hex_id(
            product_id,
            field_name="product_id",
        )
        if normalized_vendor_id is None or normalized_product_id is None:
            continue
        for block in device_blocks:
            block_upper = block.upper()
            if (
                f"VID_{normalized_vendor_id}" in block_upper
                and f"PID_{normalized_product_id}" in block_upper
            ):
                matching_blocks.append(block)

    needs_driver = any("CM_PROB_FAILED_INSTALL" in block for block in matching_blocks)
    if needs_driver and log_reason:
        logger.debug("\n\n".join(matching_blocks))
    return needs_driver


def _preferred_duplicate_port(first_port: str, second_port: str) -> str | None:
    """Return the preferred representative when two paths name one serial port."""
    sorted_ports = sorted((first_port, second_port))
    first_sorted, second_sorted = sorted_ports
    if "usbserial" in first_sorted and "wchusbserial" in second_sorted:
        first = first_sorted.replace("usbserial-", "")
        second = second_sorted.replace("wchusbserial", "")
        if first == second:
            return second_sorted
    elif "usbmodem" in first_sorted and "wchusbserial" in second_sorted:
        first = first_sorted.replace("usbmodem", "")
        second = second_sorted.replace("wchusbserial", "")
        if first == second:
            return second_sorted
    elif "SLAB_USBtoUART" in first_sorted and "usbserial" in second_sorted:
        return second_sorted
    return None


def _eliminate_duplicate_port(ports: list[str]) -> list[str]:
    """Collapse likely duplicate serial device paths to one representative."""
    deduped: list[str] = []
    for port in ports:
        for index, existing_port in enumerate(deduped):
            preferred_port = _preferred_duplicate_port(existing_port, port)
            if preferred_port is None:
                continue
            deduped[index] = preferred_port
            break
        else:
            deduped.append(port)
    return deduped


def _is_windows11() -> bool:
    """Return whether the host appears to be Windows 11."""
    if platform.system() != "Windows":
        return False
    try:
        if float(platform.release()) < 10.0:
            return False
        version_parts = platform.version().split(".")
        if len(version_parts) < 3:
            return False
        patch = version_parts[2]
        return int(patch) >= 22000
    except ValueError:
        return False
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
            and device.usb_vendor_id_in_hex.lower().removeprefix("0x") == normalized_vid
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
    detect_windows_port_from_output_fn: (
        Callable[
            [SupportedDevice | None, str],
            set[str],
        ]
        | None
    ) = None,
) -> set[str]:
    """Collect active serial ports for supported devices on the current platform."""
    ports: set[str] = set()
    baseports: set[str] = set()
    system = platform.system()
    sds_list = list(sds)

    for device in sds_list:
        if system == "Linux" and device.baseport_on_linux is not None:
            baseports.add(device.baseport_on_linux)
        elif system == "Darwin" and device.baseport_on_mac is not None:
            baseports.add(device.baseport_on_mac)

    if system in ("Linux", "Darwin"):
        for base_port in baseports:
            ports |= _discover_unix_ports(base_port)
    elif system == "Windows":
        if detect_windows_port_from_output_fn is None:
            for device in sds_list:
                ports.update(detect_windows_port_fn(device))
        else:
            sp_output = _windows_pnp_device_output(present_only=True)
            for device in sds_list:
                ports.update(detect_windows_port_from_output_fn(device, sp_output))

    if eliminate_duplicates:
        port_list = eliminate_duplicate_port_fn(list(ports))
        ports = set(port_list)
    return ports


def _detect_windows_port(sd: SupportedDevice | None) -> set[str]:
    """Detect Windows COM ports associated with a supported USB device."""
    if not sd or platform.system() != "Windows":
        return set()
    return _detect_windows_port_from_output(
        sd,
        _windows_pnp_device_output(present_only=True),
    )


def _detect_windows_port_from_output(
    sd: SupportedDevice | None,
    sp_output: str,
) -> set[str]:
    """Extract COM ports for one supported device from PnP Format-List output."""
    ports: set[str] = set()
    if not sd:
        return ports

    usb_ids = sd.usb_ids
    if (
        not usb_ids
        and sd.usb_vendor_id_in_hex is not None
        and sd.usb_product_id_in_hex is not None
    ):
        usb_ids = ((sd.usb_vendor_id_in_hex, sd.usb_product_id_in_hex),)
    device_blocks = tuple(_iter_pnp_device_blocks(sp_output))
    for vendor_id, product_id in usb_ids or ():
        normalized_vendor_id = _normalize_usb_hex_id(
            vendor_id,
            field_name="vendor_id",
        )
        if normalized_vendor_id is None:
            continue
        normalized_product_id = _normalize_usb_hex_id(
            product_id,
            field_name="product_id",
        )
        if product_id is not None and normalized_product_id is None:
            continue
        for device_block in device_blocks:
            device_block_upper = device_block.upper()
            if f"VID_{normalized_vendor_id}" not in device_block_upper:
                continue
            if (
                normalized_product_id is not None
                and f"PID_{normalized_product_id}" not in device_block_upper
            ):
                continue
            for com_suffix in _COM_PORT_RE.findall(device_block):
                ports.add(f"COM{com_suffix}")
    return ports
