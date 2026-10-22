"""BlueZ ATT requests must preserve the consuming Meshtastic read semantics."""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import MagicMock

import pytest

from meshtastic.interface_proxy._bluez import _install_read_handler
from meshtastic.interface_proxy.endpoints import BLEMailbox
from meshtastic.interfaces.ble.constants import FROMNUM_UUID, FROMRADIO_UUID


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

    def read(offset: int, device: str = "central", path: str = FROMRADIO_UUID) -> Any:
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
