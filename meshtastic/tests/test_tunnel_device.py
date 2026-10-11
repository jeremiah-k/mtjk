"""Unit tests for the Linux TUN device adapter (meshtastic.tunnel_device)."""

from __future__ import annotations

import os
import platform
import struct
import subprocess
from typing import Any, Final

import pytest

from meshtastic import tunnel_device
from meshtastic.tunnel_device import (
    IFF_NO_PI,
    IFF_TUN,
    IFNAMSIZ,
    TUN_CONTROL_DEVICE,
    TUNSETIFF,
    LinuxTunDevice,
)

_FAKE_FD = 42
_PROBE_INTERFACE_NAME: Final[str] = "mtjkprobe%d"


def _resolved_name_buffer(name: str) -> bytes:
    """Build the ioctl return buffer with the kernel-resolved interface name."""
    return name.encode("ascii").ljust(IFNAMSIZ, b"\x00") + b"\x00\x01"


def _tun_device_available() -> bool:
    """Report whether this process can attach a real TUN interface.

    Replays the exact ``LinuxTunDevice`` construction sequence: open
    ``/dev/net/tun`` read/write, then issue the ``TUNSETIFF`` ioctl with
    ``IFF_TUN | IFF_NO_PI`` and a disposable test-scoped interface request.
    Closing the descriptor destroys the transient interface, so a passing
    probe leaves no kernel state behind.

    Returns
    -------
    bool
        ``True`` when the control device can be opened and the attach ioctl
        is accepted (typically root or ``CAP_NET_ADMIN``). ``False`` on any
        ``OSError``, covering the missing device node (``FileNotFoundError``)
        and the denied open or ioctl (``PermissionError``) seen in containers,
        agent sandboxes, and CI runners without TUN privileges.
    """
    fcntl_module = tunnel_device.fcntl
    if fcntl_module is None:
        return False
    try:
        fd = os.open(TUN_CONTROL_DEVICE, os.O_RDWR)
    except OSError:
        return False
    try:
        request = struct.pack(
            "16sH22x",
            _PROBE_INTERFACE_NAME.encode("ascii")[: IFNAMSIZ - 1],
            IFF_TUN | IFF_NO_PI,
        )
        fcntl_module.ioctl(fd, TUNSETIFF, request)
    except OSError:
        return False
    finally:
        os.close(fd)
    return True


@pytest.fixture
def fake_kernel_device(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[Any], list[Any]]:
    """Provide a fake /dev/net/tun with ioctl returning a fixed interface name."""
    ioctl_calls: list[Any] = []
    closed_fds: list[int] = []

    def fake_ioctl(_fd: int, request: int, ifr: bytes) -> bytes:
        ioctl_calls.append((request, ifr))
        return _resolved_name_buffer("mesh0")

    monkeypatch.setattr(os, "open", lambda *_args, **_kwargs: _FAKE_FD)
    monkeypatch.setattr(tunnel_device.fcntl, "ioctl", fake_ioctl)
    monkeypatch.setattr(os, "close", lambda fd: closed_fds.append(fd))
    return ioctl_calls, closed_fds


@pytest.fixture
def ip_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> list[list[str]]:
    """Capture ip subcommands instead of executing them."""
    commands: list[list[str]] = []

    def fake_run(command: list[str], check: bool = True) -> None:  # noqa: ARG001
        commands.append(command)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return commands


@pytest.mark.unit
def test_open_requests_tun_device_without_packet_info(
    fake_kernel_device: tuple[list[Any], list[Any]],
) -> None:
    """TUNSETIFF should request a layer-3 TUN device with no packet header."""
    ioctl_calls, _ = fake_kernel_device

    device = LinuxTunDevice(name="mesh")

    assert device.name == "mesh0"
    assert len(ioctl_calls) == 1
    request, ifr = ioctl_calls[0]
    assert request == TUNSETIFF
    # The request must be a full 40-byte struct ifreq (16-byte name + flags +
    # zero padding) so the kernel's fixed-size copy stays in bounds.
    assert len(ifr) == 40
    name_bytes, flags = struct.unpack("16sH", ifr[:18])
    assert name_bytes.rstrip(b"\x00") == b"mesh%d"
    assert flags == IFF_TUN | IFF_NO_PI
    assert ifr[18:] == b"\x00" * 22


@pytest.mark.unit
def test_open_without_name_requests_historical_tun_pattern(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default constructor should preserve PyTap2's ``tun%d`` request."""
    requests: list[bytes] = []
    monkeypatch.setattr(os, "open", lambda *_a, **_k: _FAKE_FD)

    def fake_ioctl(_fd: int, _request: int, ifr: bytes) -> bytes:
        requests.append(ifr)
        return _resolved_name_buffer("tun0")

    monkeypatch.setattr(tunnel_device.fcntl, "ioctl", fake_ioctl)

    device = LinuxTunDevice()

    assert device.name == "tun0"
    assert struct.unpack("16sH", requests[0][:18])[0].rstrip(b"\x00") == b"tun%d"


@pytest.mark.unit
def test_open_failure_closes_control_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected TUNSETIFF must not leak the opened control device."""
    closed_fds: list[int] = []
    monkeypatch.setattr(os, "open", lambda *_a, **_k: _FAKE_FD)
    monkeypatch.setattr(
        tunnel_device.fcntl,
        "ioctl",
        lambda *_args: (_ for _ in ()).throw(OSError("EPERM")),
    )
    monkeypatch.setattr(os, "close", lambda fd: closed_fds.append(fd))

    with pytest.raises(OSError):
        LinuxTunDevice(name="mesh")

    assert closed_fds == [_FAKE_FD]


@pytest.mark.unit
@pytest.mark.parametrize(
    "machine",
    ["x86_64", "amd64", "i686", "aarch64", "armv7l", "riscv64", "loongarch64", "sh4"],
)
def test_asm_generic_architectures_are_accepted(
    monkeypatch: pytest.MonkeyPatch, machine: str
) -> None:
    """Architectures using the asm-generic ioctl encoding should pass the guard."""
    monkeypatch.setattr(platform, "machine", lambda: machine)
    tunnel_device._require_asm_generic_ioctl()


@pytest.mark.unit
@pytest.mark.parametrize(
    "machine", ["mips64", "ppc64le", "powerpc", "sparc64", "parisc64", "alpha"]
)
def test_other_architectures_are_rejected(
    monkeypatch: pytest.MonkeyPatch, machine: str
) -> None:
    """Architectures with different ioctl encodings must fail clearly."""
    monkeypatch.setattr(platform, "machine", lambda: machine)

    with pytest.raises(OSError, match="asm-generic"):
        LinuxTunDevice(name="mesh")


@pytest.mark.unit
def test_constructor_rejects_missing_fcntl(monkeypatch: pytest.MonkeyPatch) -> None:
    """On platforms without fcntl, construction must fail with a clear error."""
    monkeypatch.setattr(tunnel_device, "fcntl", None)

    with pytest.raises(OSError, match="fcntl"):
        LinuxTunDevice(name="mesh")


@pytest.mark.unit
def test_up_uses_modern_ip_link_command(
    fake_kernel_device: tuple[list[Any], list[Any]],
    ip_commands: list[list[str]],
) -> None:
    """Bringing the device up should use ip link, not net-tools ifconfig."""
    device = LinuxTunDevice(name="mesh")

    device.up()

    assert ip_commands == [["ip", "link", "set", "dev", "mesh0", "up"]]


@pytest.mark.unit
def test_ifconfig_converts_netmask_to_prefix_length(
    fake_kernel_device: tuple[list[Any], list[Any]],
    ip_commands: list[list[str]],
) -> None:
    """Calling ifconfig should assign the address with a prefix length and set the MTU."""
    device = LinuxTunDevice(name="mesh")

    device.ifconfig(address="10.115.248.28", netmask="255.255.0.0", mtu=233)

    assert ip_commands == [
        ["ip", "addr", "add", "10.115.248.28/16", "dev", "mesh0"],
        ["ip", "link", "set", "dev", "mesh0", "mtu", "233"],
    ]
    assert device.mtu == 233


@pytest.mark.unit
def test_ifconfig_applies_mtu_even_when_it_matches_constructor_value(
    fake_kernel_device: tuple[list[Any], list[Any]],
    ip_commands: list[list[str]],
) -> None:
    """A constructor MTU is only recorded; ifconfig must still apply it to Linux."""
    device = LinuxTunDevice(name="mesh", mtu=200)

    device.ifconfig(address="10.115.1.2", netmask="255.255.0.0", mtu=200)

    assert ip_commands == [
        ["ip", "addr", "add", "10.115.1.2/16", "dev", "mesh0"],
        ["ip", "link", "set", "dev", "mesh0", "mtu", "200"],
    ]
    assert device.mtu == 200


@pytest.mark.unit
def test_ifconfig_without_mtu_keeps_current_mtu(
    fake_kernel_device: tuple[list[Any], list[Any]],
    ip_commands: list[list[str]],
) -> None:
    """Omitting the MTU should only assign the address."""
    device = LinuxTunDevice(name="mesh", mtu=200)

    device.ifconfig(address="10.115.1.2", netmask="255.255.0.0")

    assert ip_commands == [["ip", "addr", "add", "10.115.1.2/16", "dev", "mesh0"]]
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

    monkeypatch.setattr(subprocess, "run", failing_run)
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

    monkeypatch.setattr(os, "read", fake_read)
    monkeypatch.setattr(os, "write", fake_write)
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


@pytest.mark.unit
def test_probe_reports_false_without_fcntl_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Platforms without fcntl must read as unavailable without opening anything."""
    monkeypatch.setattr(tunnel_device, "fcntl", None)

    assert _tun_device_available() is False


@pytest.mark.unit
def test_probe_reports_false_when_control_device_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing /dev/net/tun must read as unavailable and close nothing."""
    closed_fds: list[int] = []
    monkeypatch.setattr(os, "close", closed_fds.append)

    def missing_device(*_args: Any, **_kwargs: Any) -> int:
        raise FileNotFoundError(2, "No such file or directory", TUN_CONTROL_DEVICE)

    monkeypatch.setattr(os, "open", missing_device)

    assert _tun_device_available() is False
    assert closed_fds == []


@pytest.mark.unit
def test_probe_reports_false_when_tunsetiff_is_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A denied TUNSETIFF (no CAP_NET_ADMIN) must read as unavailable."""
    closed_fds: list[int] = []
    monkeypatch.setattr(os, "open", lambda *_args, **_kwargs: _FAKE_FD)
    monkeypatch.setattr(os, "close", closed_fds.append)

    def denied_ioctl(_fd: int, _request: int, _ifr: bytes) -> bytes:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(tunnel_device.fcntl, "ioctl", denied_ioctl)

    assert _tun_device_available() is False
    assert closed_fds == [_FAKE_FD]


@pytest.mark.unit
def test_probe_reports_true_when_tunsetiff_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An accepted TUNSETIFF must read as available and close the descriptor."""
    closed_fds: list[int] = []
    ioctl_requests: list[bytes] = []
    monkeypatch.setattr(os, "open", lambda *_args, **_kwargs: _FAKE_FD)

    def resolving_ioctl(_fd: int, _request: int, ifr: bytes) -> bytes:
        ioctl_requests.append(ifr)
        return _resolved_name_buffer("mtjkprobe0")

    monkeypatch.setattr(tunnel_device.fcntl, "ioctl", resolving_ioctl)
    monkeypatch.setattr(os, "close", closed_fds.append)

    assert _tun_device_available() is True
    assert closed_fds == [_FAKE_FD]
    assert len(ioctl_requests) == 1
    name_bytes, flags = struct.unpack("16sH", ioctl_requests[0][:18])
    assert name_bytes.rstrip(b"\x00") == b"mtjkprobe%d"
    assert flags == IFF_TUN | IFF_NO_PI


@pytest.mark.unit
def test_real_kernel_attaches_and_releases_tun_interface() -> None:
    """Attach and release a real TUN only when the test is executed.

    Probe at execution time rather than at module import or collection, which
    must remain side-effect-free even on privileged hosts.
    """
    if not _tun_device_available():
        pytest.skip("requires a usable TUN device (/dev/net/tun with CAP_NET_ADMIN)")
    device = LinuxTunDevice(name="mtjkprobe")
    try:
        assert device.name.startswith("mtjkprobe")
    finally:
        device.close()

    with pytest.raises(OSError, match="closed"):
        device.read()
