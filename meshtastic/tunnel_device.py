"""Minimal Linux TUN device adapter used by the mesh IP tunnel.

This replaces the abandoned PyTap2 dependency (upstream repository archived;
see dependency health audit 2026-09-16) with the small surface mtjk actually
needs: open ``/dev/net/tun``, attach with ``TUNSETIFF`` using
``IFF_TUN | IFF_NO_PI``, read/write raw IP packets, and configure the
interface with the modern ``ip`` command instead of the obsolete
net-tools ``ifconfig`` (which current distributions may not install).

Creating a TUN device requires ``CAP_NET_ADMIN`` (typically via root or
sudo). Run only the tunnel invocation with the necessary privileges, or use a
dedicated service/launcher that grants ``CAP_NET_ADMIN`` solely to the tunnel
process. Do not grant the capability to the shared Python interpreter: that
gives every script run by that interpreter network-administration privileges.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import platform
import struct
import subprocess
from types import ModuleType
from typing import Final

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - fcntl is Unix-only (e.g. Windows)
    _fcntl = None  # type: ignore[assignment]

# Import-optional so ``meshtastic.tunnel`` stays importable on non-Unix
# platforms; LinuxTunDevice refuses to construct when it is missing.
fcntl: ModuleType | None = _fcntl

logger = logging.getLogger(__name__)

TUN_CONTROL_DEVICE: Final[str] = "/dev/net/tun"
# ioctl request for TUNSETIFF from <linux/if_tun.h> using the asm-generic
# _IOW encoding (matches the value PyTap2 historically used). Architectures
# with different ioctl encodings (mips, powerpc, sparc, ...) would need their
# own value; constructing the device there is rejected explicitly below.
TUNSETIFF: Final[int] = 0x400454CA
# platform.machine() values known to use the asm-generic ioctl encoding:
# exact matches below, plus prefix families in _ASM_GENERIC_MACHINE_PREFIXES.
# Extend these only after checking the kernel's uapi ioctl headers for the
# architecture in question.
_ASM_GENERIC_MACHINES: Final[frozenset[str]] = frozenset(
    {
        "amd64",
        "x86_64",
        "i386",
        "i486",
        "i586",
        "i686",
        "aarch64",
        "arm64",
        "s390",
        "s390x",
        "m68k",
        "csky",
        "openrisc",
        "nios2",
        "hexagon",
    }
)
_ASM_GENERIC_MACHINE_PREFIXES: Final[tuple[str, ...]] = (
    "arm",
    "aarch64",
    "riscv",
    "loongarch",
    "sh",
)
IFF_TUN: Final[int] = 0x0001
IFF_NO_PI: Final[int] = 0x1000
IFNAMSIZ: Final[int] = 16
DEFAULT_MTU: Final[int] = 1500
# One os.read() returns a single packet; buffer for the largest IPv4 datagram.
MAX_IP_PACKET_SIZE: Final[int] = 65535


def _require_asm_generic_ioctl() -> None:
    """Reject architectures whose ioctl encoding differs from asm-generic.

    Raises
    ------
    OSError
        If ``platform.machine()`` is not known to encode ``_IOW`` the
        asm-generic way (for example mips, powerpc, or sparc).
    """
    machine = platform.machine().lower()
    if machine in _ASM_GENERIC_MACHINES or machine.startswith(
        _ASM_GENERIC_MACHINE_PREFIXES
    ):
        return
    raise OSError(
        f"LinuxTunDevice requires an architecture using the asm-generic "
        f"ioctl encoding for TUNSETIFF; {machine!r} is not supported "
        f"(mips/powerpc/sparc and friends encode _IOW differently)"
    )


def _run_ip(*args: str) -> None:
    """Run an ``ip`` subcommand, raising ``CalledProcessError`` on failure.

    Parameters
    ----------
    *args : str
        Subcommand tokens appended after ``ip`` (e.g. ``"link", "set", ...``).
    """
    command = ["ip", *args]
    logger.debug("Running interface command: %s", " ".join(command))
    subprocess.run(command, check=True)


class LinuxTunDevice:
    """Linux-only layer-3 TUN device with no packet-information header.

    Mirrors the small PyTap2 ``TapDevice`` surface the tunnel historically
    used (``up``/``ifconfig``/``read``/``write``/``close``) so it can serve
    as a drop-in replacement.

    Note that a blocking :meth:`read` is not interrupted by :meth:`close`
    called from another thread (close does not reliably wake a blocked
    reader on Linux). The tunnel keeps its reader on a daemon thread with a
    join timeout, matching the historical PyTap2-based shutdown behavior.
    """

    def __init__(self, *, name: str | None = None, mtu: int = DEFAULT_MTU) -> None:
        """Open ``/dev/net/tun`` and attach a new TUN interface.

        Parameters
        ----------
        name : str | None
            Historical PyTap2-style interface prefix. ``None`` requests
            ``tun%d``; a value such as ``"mesh"`` requests ``mesh%d`` so the
            kernel assigns the numeric suffix. (Default value = None)
        mtu : int
            Initial MTU recorded for the device. (Default value = 1500)

        Raises
        ------
        OSError
            If the fcntl module is unavailable (non-Unix platform), the
            architecture uses a non-asm-generic ioctl encoding, the TUN
            control device cannot be opened, or the ``TUNSETIFF`` ioctl is
            rejected (typically missing ``CAP_NET_ADMIN``).
        """
        if fcntl is None:  # pragma: no cover - exercised via monkeypatch
            raise OSError(
                "LinuxTunDevice requires Linux (the fcntl module is unavailable)"
            )
        _require_asm_generic_ioctl()
        self.mtu = mtu
        self.name = ""
        self._fd: int | None = None
        fd = os.open(TUN_CONTROL_DEVICE, os.O_RDWR)
        try:
            # Preserve PyTap2's naming contract: a supplied name is a prefix
            # and the kernel chooses the numeric suffix ("mesh" -> "mesh0").
            request_name = "tun%d" if name is None else f"{name}%d"
            # Full 40-byte struct ifreq: 16-byte name + 2-byte flags (at the
            # union offset) + padding, so the kernel's fixed-size copy never
            # reads past our buffer.
            request = struct.pack(
                "16sH22x",
                request_name.encode("ascii")[: IFNAMSIZ - 1],
                IFF_TUN | IFF_NO_PI,
            )
            resolved = fcntl.ioctl(fd, TUNSETIFF, request)
        except BaseException:
            os.close(fd)
            raise
        self.name = resolved[:IFNAMSIZ].split(b"\x00", 1)[0].decode("ascii")
        self._fd = fd
        logger.debug("Opened TUN interface %s (mtu=%d)", self.name, self.mtu)

    def _open_fd(self) -> int:
        """Return the device file descriptor or raise if already closed."""
        if self._fd is None:
            raise OSError("TUN device is closed")
        return self._fd

    def _require_open(self) -> None:
        """Fail fast when operating on a closed device."""
        _ = self._open_fd()

    def up(self) -> None:
        """Bring the interface link up.

        Raises
        ------
        OSError
            If the device is already closed.
        """
        self._require_open()
        _run_ip("link", "set", "dev", self.name, "up")

    def ifconfig(self, *, address: str, netmask: str, mtu: int | None = None) -> None:
        """Assign the IPv4 address/netmask and MTU to the interface.

        The dotted-decimal ``netmask`` is converted to a prefix length so the
        modern ``ip`` command can be used instead of net-tools ``ifconfig``.

        Parameters
        ----------
        address : str
            Dotted-decimal IPv4 address to assign.
        netmask : str
            Dotted-decimal netmask converted to a prefix length.
        mtu : int | None
            MTU to set; when None the device keeps its current MTU.
            (Default value = None)

        Raises
        ------
        OSError
            If the device is already closed.
        ValueError
            If address/netmask cannot be interpreted as an IPv4 interface.
        subprocess.CalledProcessError
            If an ``ip`` command fails.
        """
        self._require_open()
        interface = ipaddress.IPv4Interface(f"{address}/{netmask}")
        _run_ip("addr", "add", str(interface), "dev", self.name)
        if mtu is not None:
            # The constructor only records the desired MTU; it does not apply
            # it to the kernel. Always issue the command when requested, even
            # when the value equals the recorded constructor value.
            _run_ip("link", "set", "dev", self.name, "mtu", str(mtu))
            # Record the MTU only after the interface accepted it so a failed
            # command never desynchronizes self.mtu from the kernel state.
            self.mtu = mtu

    def read(self) -> bytes:
        """Read one raw IP packet from the TUN device.

        Returns
        -------
        bytes
            A single IP packet (``IFF_NO_PI``: no tunnel header is present).

        Raises
        ------
        OSError
            If the device is closed or the read fails.
        """
        return os.read(self._open_fd(), MAX_IP_PACKET_SIZE)

    def write(self, data: bytes) -> None:
        """Write one raw IP packet to the TUN device.

        Parameters
        ----------
        data : bytes
            Raw IP packet to inject.

        Raises
        ------
        OSError
            If the device is closed or the write fails.
        """
        os.write(self._open_fd(), data)

    def close(self) -> None:
        """Close the device file descriptor. Idempotent."""
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
            logger.debug("Closed TUN interface %s", self.name)
