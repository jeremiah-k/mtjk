"""BlueZ ATT requests must preserve the consuming Meshtastic read semantics."""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from meshtastic.interface_proxy._bluez import (
    _install_advertising,
    _install_read_handler,
)
from meshtastic.interface_proxy.endpoints import BLEMailbox
from meshtastic.interfaces.ble.constants import FROMNUM_UUID, FROMRADIO_UUID


def test_bluez_advertisement_is_discoverable_and_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Request LE discoverability locally, retaining Bless's cleanup inventory."""
    import asyncio

    class Advertisement:
        """Small backend fake exposing the advertisement lifecycle seam."""

        def __init__(self, _kind: str, index: int, _app: Any) -> None:
            self.path = f"/advertisement{index}"
            self._service_uuids: list[str] = []

    module = types.SimpleNamespace(
        BlueZLEAdvertisement=Advertisement,
        Type=types.SimpleNamespace(PERIPHERAL="peripheral"),
    )
    monkeypatch.setitem(
        sys.modules, "bless.backends.bluezdbus.dbus.advertisement", module
    )
    monkeypatch.setitem(
        sys.modules,
        "dbus_next.constants",
        types.SimpleNamespace(PropertyAccess=types.SimpleNamespace(READ="read")),
    )
    monkeypatch.setitem(
        sys.modules,
        "dbus_next.service",
        types.SimpleNamespace(dbus_property=lambda **_kw: property),
    )
    server = MagicMock()
    server.app.advertisements = []
    server.app.services = [types.SimpleNamespace(UUID="meshtastic-service")]
    server.app.set_name = AsyncMock()
    adapter = MagicMock()
    manager = adapter.get_interface.return_value
    manager.call_register_advertisement = AsyncMock()
    _install_advertising(server)
    asyncio.run(server.app.start_advertising(adapter))
    advertisement = server.app.advertisements[0]
    assert advertisement.Discoverable is True
    assert advertisement._service_uuids == ["meshtastic-service"]
    server.app.bus.export.assert_called_once_with(advertisement.path, advertisement)
    manager.call_register_advertisement.assert_awaited_once_with(advertisement.path, {})
    # The dependency's class and the adapter's global discoverability stay intact.
    assert not hasattr(Advertisement, "Discoverable")
    assert adapter.method_calls == [
        ("get_interface", ("org.bluez.LEAdvertisingManager1",), {}),
    ]


def test_bluez_read_continuations_preserve_packet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An offset read repeats the original packet, leaving the next one queued."""
    monkeypatch.setitem(
        sys.modules,
        "dbus_next",
        types.SimpleNamespace(
            Message=types.SimpleNamespace(
                new_method_return=lambda _request, _signature, body: body[0],
                new_error=lambda _request, name, _text: name,
            )
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "dbus_next.constants",
        types.SimpleNamespace(MessageType=types.SimpleNamespace(METHOD_CALL="call")),
    )
    mailbox = BLEMailbox()
    first, second = bytes(range(100)), b"next packet"
    mailbox.push(first)
    mailbox.push(second)
    server = MagicMock()
    server.get_characteristic.side_effect = lambda uuid: types.SimpleNamespace(
        gatt=types.SimpleNamespace(path=uuid)
    )
    server.read.side_effect = lambda characteristic: (
        mailbox.pop()
        if characteristic.path == FROMRADIO_UUID
        else mailbox.current_counter()
    )
    _install_read_handler(server)
    handler = server.bus.add_message_handler.call_args.args[0]

    def read(
        offset: object, device: str | None = "central", path: str = FROMRADIO_UUID
    ) -> Any:
        return handler(
            types.SimpleNamespace(
                message_type="call",
                interface="org.bluez.GattCharacteristic1",
                member="ReadValue",
                path=path,
                signature="a{sv}",
                body=[
                    {
                        "offset": types.SimpleNamespace(value=offset),
                        "device": types.SimpleNamespace(value=device),
                    }
                ],
            )
        )

    assert read(0) == first
    assert read(0, path=FROMNUM_UUID) == b"\x02\x00\x00\x00"
    assert read(22) == first[22:]
    assert read(len(first)) == b""
    assert read(101) == "org.bluez.Error.InvalidOffset"
    assert read(22, device="another central") == "org.bluez.Error.InvalidOffset"
    # Even a fresh offset-zero read may not steal another central's packet.
    assert read(0, device="another central") == "org.bluez.Error.InvalidOffset"
    assert read(0, device=None) == "org.bluez.Error.InvalidOffset"
    assert read(-1) == "org.bluez.Error.InvalidOffset"
    assert read(65536) == "org.bluez.Error.InvalidOffset"
    assert read("invalid") == "org.bluez.Error.InvalidOffset"
    assert read(0) == second
    assert read(0) == b""
    assert read(1) == "org.bluez.Error.InvalidOffset"
    assert server.read.call_count == 4  # Three new packets/empty reads plus FROMNUM.

    # Unrelated requests continue through Bless's normal dispatcher.
    assert (
        handler(
            types.SimpleNamespace(
                message_type="signal", interface="org.bluez.GattCharacteristic1"
            )
        )
        is None
    )
