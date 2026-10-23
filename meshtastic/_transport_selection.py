"""Explicit local transport selection, shared by clients and proxy endpoints."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import sys

import ifaddr


def _validate_ble_adapter(adapter: str | None) -> None:
    if adapter is None:
        return
    if sys.platform != "linux":
        raise ValueError("Explicit BLE adapter selection requires Linux/BlueZ")
    if not re.fullmatch(r"hci[0-9]+", adapter):
        raise ValueError("BLE adapter must be a BlueZ controller name, e.g. hci0")


async def _list_ble_adapters() -> list[tuple[str, str, str, bool]]:
    if sys.platform != "linux":
        return []
    # Linux Bleak already depends on dbus-fast; other platforms do not need it.
    # pylint: disable=import-outside-toplevel
    from dbus_fast import Message, MessageType
    from dbus_fast.aio import MessageBus
    from dbus_fast.constants import BusType
    from dbus_fast.errors import DBusError

    bus = MessageBus(bus_type=BusType.SYSTEM)
    try:
        await bus.connect()
        reply = await bus.call(
            Message(
                destination="org.bluez",
                path="/",
                interface="org.freedesktop.DBus.ObjectManager",
                member="GetManagedObjects",
            )
        )
        if reply is None or reply.message_type == MessageType.ERROR:
            raise OSError("Cannot list Bluetooth controllers")
        result = []
        for path, interfaces in reply.body[0].items():
            properties = interfaces.get("org.bluez.Adapter1")
            if properties is not None:
                result.append(
                    (
                        str(path.rsplit("/", 1)[-1]),
                        str(properties["Alias"].value),
                        str(properties["Address"].value),
                        bool(properties["Powered"].value),
                    )
                )
        return sorted(result)
    except DBusError as exc:
        raise OSError(f"Cannot list Bluetooth controllers: {exc}") from exc
    finally:
        bus.disconnect()


def _list_network_interfaces() -> list[tuple[str, str, list[str]]]:
    """Return interface identifiers, display names, and scoped local IPs."""
    result = []
    for adapter in ifaddr.get_adapters(include_unconfigured=True):
        addresses = []
        for address in adapter.ips:
            if isinstance(address.ip, str):
                value = address.ip
            else:
                value, _flow, scope = address.ip
                if scope:
                    value = f"{value}%{scope}"
            if not ipaddress.ip_address(value).is_unspecified:
                addresses.append(value)
        result.append((adapter.name, adapter.nice_name, addresses))
    return sorted(result)


def _network_addresses(interface: str) -> list[str]:
    """Resolve a NIC name/display name or an explicit source IP address."""
    try:
        address = ipaddress.ip_address(interface)
    except ValueError:
        pass
    else:
        if address.is_unspecified or address.is_multicast:
            raise ValueError("Select a specific local IP address")
        return [interface]
    try:
        interfaces = _list_network_interfaces()
    except OSError as exc:
        raise ValueError(
            "Cannot enumerate network interfaces; select a local source IP instead"
        ) from exc
    matches = [
        addresses
        for name, display_name, addresses in interfaces
        if interface in (name, display_name)
    ]
    if not matches:
        raise ValueError(
            f"Unknown network interface {interface!r}; use --list-interfaces"
        )
    if len(matches) != 1:
        raise ValueError(
            f"Ambiguous network interface {interface!r}; use its identifier or IP"
        )
    if not matches[0]:
        raise ValueError(f"Network interface {interface!r} has no IP addresses")
    return sorted(matches[0], key=lambda value: (":" in value, value))


def _listener_address(host: str, interface: str | None) -> str:
    """Choose one local listener address, preferring the URI's IP family."""
    if interface is None:
        return host
    addresses = _network_addresses(interface)
    prefer_ipv6 = ":" in host
    return next(
        (address for address in addresses if (":" in address) == prefer_ipv6),
        addresses[0],
    )


def _connect_tcp(
    host: str, port: int, interface: str, timeout: float | None
) -> socket.socket:
    """Connect using an address of the selected local NIC, matching IP families."""
    sources = _network_addresses(interface)
    error: OSError | None = None
    for family, kind, protocol, _name, destination in socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    ):
        for source in sources:
            if (":" in source) != (family == socket.AF_INET6):
                continue
            sock = None
            try:
                sock = socket.socket(family, kind, protocol)
                if timeout is not None:
                    sock.settimeout(timeout)
                local = socket.getaddrinfo(
                    source, 0, family, socket.SOCK_STREAM, flags=socket.AI_NUMERICHOST
                )[0][4]
                sock.bind(local)
                sock.connect(destination)
                return sock
            except OSError as exc:
                error = exc
                if sock is not None:
                    sock.close()
            except BaseException:
                if sock is not None:
                    sock.close()
                raise
    if error is not None:
        raise error
    raise OSError(f"Selected interface has no address compatible with {host!r}")


async def _open_tcp_connection(
    host: str, port: int, interface: str | None
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    if interface is None:
        return await asyncio.open_connection(host, port)
    sources = _network_addresses(interface)
    try:
        destination_family = ipaddress.ip_address(host).version
    except ValueError:
        destination_family = None  # Hostnames may resolve to either family.
    if destination_family is not None:
        sources = [
            source
            for source in sources
            if (6 if ":" in source else 4) == destination_family
        ]
        if not sources:
            raise OSError(f"Selected interface has no address compatible with {host!r}")
    error: OSError | None = None
    for source in sources:
        try:
            return await asyncio.open_connection(
                host,
                port,
                local_addr=(source, 0),
                family=socket.AF_INET6 if ":" in source else socket.AF_INET,
            )
        except OSError as exc:
            error = exc
    assert error is not None
    raise error
