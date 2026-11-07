"""Fault injection for local network selection and BlueZ inventory fallbacks."""

from __future__ import annotations

import asyncio
import errno
import socket
import sys
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import ifaddr
import pytest

from meshtastic import _transport_selection as selection


@pytest.mark.parametrize("adapter", ["hci", "hci-0", "HCI0", "hci0:any", "0"])
def test_ble_adapter_rejects_invalid_linux_controller_names(
    monkeypatch: pytest.MonkeyPatch, adapter: str
) -> None:
    monkeypatch.setattr(selection, "sys", types.SimpleNamespace(platform="linux"))
    with pytest.raises(ValueError, match="BlueZ controller name"):
        selection._validate_ble_adapter(adapter)


def test_ble_adapter_rejects_non_linux_explicit_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(selection, "sys", types.SimpleNamespace(platform="darwin"))
    with pytest.raises(ValueError, match="Linux/BlueZ"):
        selection._validate_ble_adapter("hci0")
    selection._validate_ble_adapter(None)
    assert asyncio.run(selection._list_ble_adapters()) == []


def _fake_dbus(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response: Any = None,
    error: Exception | None = None,
) -> MagicMock:
    """Expose the narrow dbus-fast contract without a live system bus."""

    class DBusError(Exception):
        pass

    bus = MagicMock()
    bus.connect = AsyncMock()
    bus.call = AsyncMock(return_value=response)
    if error is not None:
        bus.call.side_effect = error
    backend: Any = types.ModuleType("dbus_fast")
    backend.Message = MagicMock
    backend.MessageType = types.SimpleNamespace(ERROR="error")
    aio: Any = types.ModuleType("dbus_fast.aio")
    aio.MessageBus = lambda **_kwargs: bus
    constants: Any = types.ModuleType("dbus_fast.constants")
    constants.BusType = types.SimpleNamespace(SYSTEM="system")
    errors: Any = types.ModuleType("dbus_fast.errors")
    errors.DBusError = DBusError
    monkeypatch.setitem(sys.modules, "dbus_fast", backend)
    monkeypatch.setitem(sys.modules, "dbus_fast.aio", aio)
    monkeypatch.setitem(sys.modules, "dbus_fast.constants", constants)
    monkeypatch.setitem(sys.modules, "dbus_fast.errors", errors)
    return bus


def test_bluez_inventory_tolerates_incomplete_adapter_properties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partly populated ObjectManager snapshot must not abort enumeration."""
    monkeypatch.setattr(selection, "sys", types.SimpleNamespace(platform="linux"))
    props = {
        "Alias": types.SimpleNamespace(value="USB dongle"),
        "Address": types.SimpleNamespace(value="AA:BB:CC:DD:EE:FF"),
        "Powered": types.SimpleNamespace(value=True),
    }
    response = types.SimpleNamespace(
        message_type="reply",
        body=[
            {
                "/org/bluez/hci8": {"org.bluez.Adapter1": {}},
                "/org/bluez/hci2": {"org.bluez.Adapter1": props},
                "/org/bluez/other": {"org.bluez.Device1": {}},
            }
        ],
    )
    bus = _fake_dbus(monkeypatch, response=response)
    assert asyncio.run(selection._list_ble_adapters()) == [
        ("hci2", "USB dongle", "AA:BB:CC:DD:EE:FF", True),
        ("hci8", "hci8", "", False),
    ]
    bus.connect.assert_awaited_once()
    bus.call.assert_awaited_once()
    bus.disconnect.assert_called_once()


@pytest.mark.parametrize("reply", [None, types.SimpleNamespace(message_type="error")])
def test_bluez_inventory_reports_failed_object_manager_call(
    monkeypatch: pytest.MonkeyPatch, reply: object
) -> None:
    monkeypatch.setattr(selection, "sys", types.SimpleNamespace(platform="linux"))
    bus = _fake_dbus(monkeypatch, response=reply)
    with pytest.raises(OSError, match="Cannot list Bluetooth"):
        asyncio.run(selection._list_ble_adapters())
    bus.disconnect.assert_called_once()


def test_bluez_inventory_wraps_dbus_error_and_disconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(selection, "sys", types.SimpleNamespace(platform="linux"))
    bus = _fake_dbus(monkeypatch, response=SimpleNamespace(message_type="reply"))
    # Raise the actual type imported inside the function, not an arbitrary exception.
    error_type = sys.modules["dbus_fast.errors"].DBusError
    bus.call.side_effect = error_type("BlueZ unavailable")
    with pytest.raises(OSError, match="BlueZ unavailable"):
        asyncio.run(selection._list_ble_adapters())
    bus.disconnect.assert_called_once()


def test_network_inventory_skips_invalid_and_unconfigured_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unrelated bad host interface data must not crash inventory or selection."""
    adapters = [
        types.SimpleNamespace(
            name="eth2",
            nice_name="My NIC",
            ips=[
                types.SimpleNamespace(ip="0.0.0.0"),
                types.SimpleNamespace(ip="bad-local-IP"),
                types.SimpleNamespace(ip="192.0.2.23"),
                types.SimpleNamespace(ip=("fe80::abcd", 0, 5)),
            ],
        ),
        types.SimpleNamespace(name="eth0", nice_name="Idle", ips=[]),
    ]
    monkeypatch.setattr(ifaddr, "get_adapters", lambda **_kwargs: adapters)
    assert selection._list_network_interfaces() == [
        ("eth0", "Idle", []),
        ("eth2", "My NIC", ["192.0.2.23", "fe80::abcd%5"]),
    ]
    assert selection._network_addresses("eth2") == ["192.0.2.23", "fe80::abcd%5"]


def test_network_source_selection_catches_empty_unknown_and_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapters = [
        ("eth0", "Shared", ["127.0.0.1"]),
        ("eth1", "Shared", ["::1"]),
        ("eth2", "Unconfigured", []),
    ]
    monkeypatch.setattr(selection, "_list_network_interfaces", lambda: adapters)
    for candidate, pattern in [
        ("missing", "Unknown network"),
        ("Shared", "Ambiguous network"),
        ("eth2", "no IP addresses"),
    ]:
        with pytest.raises(ValueError, match=pattern):
            selection._network_addresses(candidate)
    assert selection._network_addresses("::1") == ["::1"]
    for candidate in ("0.0.0.0", "ff02::1", "224.0.0.1"):
        with pytest.raises(ValueError, match="specific local IP"):
            selection._network_addresses(candidate)


def test_listener_address_prefers_requested_ip_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        selection, "_network_addresses", lambda _iface: ["192.0.2.23", "fe80::1%5"]
    )
    assert selection._listener_address("::", "nic") == "fe80::1%5"
    assert selection._listener_address("0.0.0.0", "nic") == "192.0.2.23"
    assert selection._listener_address("localhost", None) == "localhost"


def test_sync_selected_tcp_rejects_unmatched_family_without_opening_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(selection, "_network_addresses", lambda _iface: ["::1"])
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 4403))
        ],
    )
    constructor = MagicMock()
    monkeypatch.setattr(socket, "socket", constructor)
    with pytest.raises(OSError, match="no address compatible"):
        selection._connect_tcp("127.0.0.1", 4403, "loop", 1)
    constructor.assert_not_called()


def test_sync_selected_tcp_timeout_closes_unfinished_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sock = MagicMock()
    sock.connect_ex.return_value = errno.EINPROGRESS
    monkeypatch.setattr(selection, "_network_addresses", lambda _iface: ["127.0.0.1"])
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", (host, port))
        ],
    )
    monkeypatch.setattr(socket, "socket", lambda *_args: sock)

    class FakeSelector:
        def __enter__(self) -> FakeSelector:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def register(self, *_args: object) -> None:
            pass

    monkeypatch.setattr(
        selection,
        "selectors",
        types.SimpleNamespace(DefaultSelector=FakeSelector, EVENT_WRITE=2),
    )
    with pytest.raises(TimeoutError, match="timed out"):
        selection._connect_tcp("127.0.0.1", 4403, "loop", 0)
    sock.close.assert_called_once()


def test_async_selected_tcp_reports_last_source_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        selection, "_network_addresses", lambda _iface: ["::1", "127.0.0.1"]
    )
    connect = AsyncMock(side_effect=[OSError("first"), OSError("last")])
    monkeypatch.setattr(asyncio, "open_connection", connect)
    with pytest.raises(OSError, match="last"):
        asyncio.run(selection._open_tcp_connection("radio", 4403, "loop"))
    assert connect.await_count == 2


def test_async_default_tcp_keeps_original_unbound_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader, writer = MagicMock(), MagicMock()
    connect = AsyncMock(return_value=(reader, writer))
    monkeypatch.setattr(asyncio, "open_connection", connect)
    assert asyncio.run(selection._open_tcp_connection("radio", 4403, None)) == (
        reader,
        writer,
    )
    connect.assert_awaited_once_with("radio", 4403)


def test_selected_listener_cannot_proxy_back_to_same_tcp_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validate self-loop against the effective NIC address, not the URI wildcard."""
    from meshtastic.interface_proxy.server import ProxyOptions

    monkeypatch.setattr(selection, "_network_addresses", lambda _iface: ["127.0.0.1"])
    # The imported server has its own reference to the helper.
    from meshtastic.interface_proxy import server

    monkeypatch.setattr(server, "_listener_address", lambda _host, _iface: "127.0.0.1")
    with pytest.raises(ValueError, match="same endpoint"):
        ProxyOptions(
            "tcp:127.0.0.1:4403",
            "tcp:0.0.0.0:4403",
            bind_interface="lo",
        ).validate()
