"""Unit tests for the Linux TUN device adapter (meshtastic.tunnel_device)."""

from __future__ import annotations

import struct
import subprocess
from typing import Any

import pytest

from meshtastic import tunnel_device
from meshtastic.tunnel_device import (
    IFF_NO_PI,
    IFF_TUN,
    IFNAMSIZ,
    TUNSETIFF,
    LinuxTunDevice,
)

_FAKE_FD = 42


def _resolved_name_buffer(name: str) -> bytes:
    """Build the ioctl return buffer with the kernel-resolved interface name."""
    return name.encode("ascii").ljust(IFNAMSIZ, b"\x00") + b"\x00\x01"


@pytest.fixture
def fake_kernel_device(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[Any], list[Any]]:
    """Provide a fake /dev/net/tun with ioctl returning a fixed interface name."""
    ioctl_calls: list[Any] = []
    closed_fds: list[int] = []

    def fake_ioctl(_fd: int, request: int, ifr: bytes) -> bytes:
        ioctl_calls.append((request, ifr))
        return _resolved_name_buffer("mesh")

    monkeypatch.setattr(tunnel_device.os, "open", lambda *_args, **_kwargs: _FAKE_FD)
    monkeypatch.setattr(tunnel_device.fcntl, "ioctl", fake_ioctl)
    monkeypatch.setattr(tunnel_device.os, "close", lambda fd: closed_fds.append(fd))
    return ioctl_calls, closed_fds


@pytest.fixture
def ip_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> list[list[str]]:
    """Capture ip subcommands instead of executing them."""
    commands: list[list[str]] = []

    def fake_run(command: list[str], check: bool = True) -> None:  # noqa: ARG001
        commands.append(command)

    monkeypatch.setattr(tunnel_device.subprocess, "run", fake_run)
    return commands


@pytest.mark.unit
def test_open_requests_tun_device_without_packet_info(
    fake_kernel_device: tuple[list[Any], list[Any]],
) -> None:
    """TUNSETIFF should request a layer-3 TUN device with no packet header."""
    ioctl_calls, _ = fake_kernel_device

    device = LinuxTunDevice(name="mesh")

    assert device.name == "mesh"
    assert len(ioctl_calls) == 1
    request, ifr = ioctl_calls[0]
    assert request == TUNSETIFF
    # The request must be a full 40-byte struct ifreq (16-byte name + flags +
    # zero padding) so the kernel's fixed-size copy stays in bounds.
    assert len(ifr) == 40
    name_bytes, flags = struct.unpack("16sH", ifr[:18])
    assert name_bytes.rstrip(b"\x00") == b"mesh"
    assert flags == IFF_TUN | IFF_NO_PI
    assert ifr[18:] == b"\x00" * 22


@pytest.mark.unit
def test_open_resolves_kernel_assigned_name(
    fake_kernel_device: tuple[list[Any], list[Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty requested name should adopt the kernel-assigned interface name."""
    monkeypatch.setattr(
        tunnel_device.fcntl,
        "ioctl",
        lambda *_args: _resolved_name_buffer("tun0"),
    )

    device = LinuxTunDevice(name="")

    assert device.name == "tun0"


@pytest.mark.unit
def test_open_failure_closes_control_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected TUNSETIFF must not leak the opened control device."""
    closed_fds: list[int] = []
    monkeypatch.setattr(tunnel_device.os, "open", lambda *_a, **_k: _FAKE_FD)
    monkeypatch.setattr(
        tunnel_device.fcntl,
        "ioctl",
        lambda *_args: (_ for _ in ()).throw(OSError("EPERM")),
    )
    monkeypatch.setattr(tunnel_device.os, "close", lambda fd: closed_fds.append(fd))

    with pytest.raises(OSError):
        LinuxTunDevice(name="mesh")

    assert closed_fds == [_FAKE_FD]


@pytest.mark.unit
def test_up_uses_modern_ip_link_command(
    fake_kernel_device: tuple[list[Any], list[Any]],
    ip_commands: list[list[str]],
) -> None:
    """Bringing the device up should use ip link, not net-tools ifconfig."""
    device = LinuxTunDevice(name="mesh")

    device.up()

    assert ip_commands == [["ip", "link", "set", "dev", "mesh", "up"]]


@pytest.mark.unit
def test_ifconfig_converts_netmask_to_prefix_length(
    fake_kernel_device: tuple[list[Any], list[Any]],
    ip_commands: list[list[str]],
) -> None:
    """Calling ifconfig should assign the address with a prefix length and set the MTU."""
    device = LinuxTunDevice(name="mesh")

    device.ifconfig(address="10.115.248.28", netmask="255.255.0.0", mtu=233)

    assert ip_commands == [
        ["ip", "addr", "add", "10.115.248.28/16", "dev", "mesh"],
        ["ip", "link", "set", "dev", "mesh", "mtu", "233"],
    ]
    assert device.mtu == 233


@pytest.mark.unit
def test_ifconfig_without_mtu_keeps_current_mtu(
    fake_kernel_device: tuple[list[Any], list[Any]],
    ip_commands: list[list[str]],
) -> None:
    """Omitting the MTU should only assign the address."""
    device = LinuxTunDevice(name="mesh", mtu=200)

    device.ifconfig(address="10.115.1.2", netmask="255.255.0.0")

    assert ip_commands == [["ip", "addr", "add", "10.115.1.2/16", "dev", "mesh"]]
    assert device.mtu == 200


@pytest.mark.unit
def test_ifconfig_rejects_invalid_address(
    fake_kernel_device: tuple[list[Any], list[Any]],
    ip_commands: list[list[str]],
) -> None:
    """Malformed address/netmask input should fail before shelling out."""
    device = LinuxTunDevice(name="mesh")

    with pytest.raises(ValueError):
        device.ifconfig(address="not-an-address", netmask="255.255.0.0")

    assert ip_commands == []


@pytest.mark.unit
def test_ip_command_failure_propagates(
    fake_kernel_device: tuple[list[Any], list[Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failures from the ip command should surface to the caller."""

    def failing_run(command: list[str], check: bool = True) -> None:
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(tunnel_device.subprocess, "run", failing_run)
    device = LinuxTunDevice(name="mesh")

    with pytest.raises(subprocess.CalledProcessError):
        device.up()


@pytest.mark.unit
def test_read_and_write_use_file_descriptor(
    fake_kernel_device: tuple[list[Any], list[Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """read/write should map directly onto os.read/os.write of the TUN fd."""
    reads: list[tuple[int, int]] = []
    writes: list[tuple[int, bytes]] = []

    def fake_read(fd: int, size: int) -> bytes:
        reads.append((fd, size))
        return b"\x45\x00packet"

    def fake_write(fd: int, data: bytes) -> int:
        writes.append((fd, data))
        return len(data)

    monkeypatch.setattr(tunnel_device.os, "read", fake_read)
    monkeypatch.setattr(tunnel_device.os, "write", fake_write)
    device = LinuxTunDevice(name="mesh")

    packet = device.read()
    device.write(b"\x45\x00outgoing")

    assert packet == b"\x45\x00packet"
    assert reads == [(_FAKE_FD, tunnel_device.MAX_IP_PACKET_SIZE)]
    assert writes == [(_FAKE_FD, b"\x45\x00outgoing")]


@pytest.mark.unit
def test_close_is_idempotent_and_guards_operations(
    fake_kernel_device: tuple[list[Any], list[Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing twice is safe; I/O after close raises OSError."""
    ioctl_calls, closed_fds = fake_kernel_device
    device = LinuxTunDevice(name="mesh")

    device.close()
    device.close()

    assert closed_fds == [_FAKE_FD]
    with pytest.raises(OSError, match="closed"):
        device.read()
    with pytest.raises(OSError, match="closed"):
        device.write(b"x")
    with pytest.raises(OSError, match="closed"):
        device.up()
    with pytest.raises(OSError, match="closed"):
        device.ifconfig(address="10.115.1.2", netmask="255.255.0.0")
