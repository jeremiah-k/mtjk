"""Minimal Linux TUN device adapter used by the mesh IP tunnel.

This replaces the abandoned PyTap2 dependency (upstream repository archived;
see dependency health audit 2026-09-16) with the small surface mtjk actually
needs: open ``/dev/net/tun``, attach with ``TUNSETIFF`` using
``IFF_TUN | IFF_NO_PI``, read/write raw IP packets, and configure the
interface with the modern ``ip`` command instead of the obsolete
net-tools ``ifconfig`` (which current distributions may not install).

Creating a TUN device requires ``CAP_NET_ADMIN`` (or root). Grant it to the
interpreter with e.g. ``sudo setcap cap_net_admin+eip $(which python3)``.
"""

from __future__ import annotations

import fcntl
import ipaddress
import logging
import os
import struct
import subprocess
from typing import Final

logger = logging.getLogger(__name__)

TUN_CONTROL_DEVICE: Final[str] = "/dev/net/tun"
# ioctl request for TUNSETIFF from <linux/if_tun.h>, asm-generic encoding
# (x86, arm64, riscv; matches the value PyTap2 historically used). Some other
# architectures encode _IOW differently and would need their own value.
TUNSETIFF: Final[int] = 0x400454CA
IFF_TUN: Final[int] = 0x0001
IFF_NO_PI: Final[int] = 0x1000
IFNAMSIZ: Final[int] = 16
DEFAULT_MTU: Final[int] = 1500
# One os.read() returns a single packet; buffer for the largest IPv4 datagram.
MAX_IP_PACKET_SIZE: Final[int] = 65535


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
    """

    def __init__(self, *, name: str = "mesh", mtu: int = DEFAULT_MTU) -> None:
        """Open ``/dev/net/tun`` and attach a new TUN interface.

        Parameters
        ----------
        name : str
            Requested interface name. If empty, the kernel assigns the next
            free ``tunN`` name. (Default value = "mesh")
        mtu : int
            Initial MTU recorded for the device. (Default value = 1500)

        Raises
        ------
        OSError
            If the TUN control device cannot be opened or the ``TUNSETIFF``
            ioctl is rejected (typically missing ``CAP_NET_ADMIN``).
        """
        self.mtu = mtu
        self.name = name
        self._fd: int | None = None
        fd = os.open(TUN_CONTROL_DEVICE, os.O_RDWR)
        try:
            # Full 40-byte struct ifreq: 16-byte name + 2-byte flags (at the
            # union offset) + padding, so the kernel's fixed-size copy never
            # reads past our buffer.
            request = struct.pack(
                "16sH22x", name.encode("ascii")[: IFNAMSIZ - 1], IFF_TUN | IFF_NO_PI
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
        if mtu is not None and mtu != self.mtu:
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
