"""BlueZ ATT requests must preserve the consuming Meshtastic read semantics."""

from __future__ import annotations

import sys
import types
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from meshtastic.interface_proxy._bluez import (
    _install_advertising,
    _install_request_handler,
)
from meshtastic.interface_proxy.endpoints import BLEMailbox
from meshtastic.interfaces.ble.constants import (
    FROMNUM_UUID,
    FROMRADIO_UUID,
    TORADIO_UUID,
)


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


@pytest.fixture
def bluez_server(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[MagicMock, BLEMailbox, Callable[..., Any]]:
    """Dispatch owned D-Bus requests, including Bless's fallback for writes."""
    monkeypatch.setitem(
        sys.modules,
        "dbus_next",
        types.SimpleNamespace(
            Message=types.SimpleNamespace(
                new_method_return=lambda _request, _signature, body: (
                    body[0] if body else ()
                ),
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
    server = MagicMock()
    server.get_characteristic.side_effect = lambda uuid: types.SimpleNamespace(
        gatt=types.SimpleNamespace(path=uuid)
    )
    server.read.side_effect = lambda characteristic: (
        mailbox.pop()
        if characteristic.path == FROMRADIO_UUID
        else mailbox.current_counter()
    )
    _install_request_handler(server)
    handler = server.bus.add_message_handler.call_args.args[0]

    def _request(
        offset: object = 0,
        device: str | None = "central",
        path: str = FROMRADIO_UUID,
        *,
        write: bytes | None = None,
        **options: object,
    ) -> Any:
        properties = {
            key: types.SimpleNamespace(value=value)
            for key, value in {"offset": offset, "device": device, **options}.items()
        }
        if device is None:
            del properties["device"]
        result = handler(
            types.SimpleNamespace(
                message_type="call",
                interface="org.bluez.GattCharacteristic1",
                member="ReadValue" if write is None else "WriteValue",
                path=path,
                signature="a{sv}" if write is None else "aya{sv}",
                body=[properties] if write is None else [write, properties],
            )
        )
        if result is None and write is not None:
            # This is the backend dispatch that currently drops write options.
            server.write(server.get_characteristic(path).gatt, write)
            return ()
        return result

    return server, mailbox, _request


def test_bluez_read_continuations_preserve_packet(
    bluez_server: tuple[MagicMock, BLEMailbox, Callable[..., Any]],
) -> None:
    """An offset read repeats the original packet, leaving the next one queued."""
    server, mailbox, read = bluez_server
    first, second = bytes(range(100)), b"next packet"
    mailbox.push(first)
    mailbox.push(second)

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
    assert read(False) == "org.bluez.Error.InvalidOffset"
    assert read(0.0) == "org.bluez.Error.InvalidOffset"
    assert read(0) == second
    assert read(0) == b""
    assert read(1) == "org.bluez.Error.InvalidOffset"
    assert server.read.call_count == 4  # Three new packets/empty reads plus FROMNUM.

    # Unrelated requests continue through Bless's normal dispatcher.
    handler = server.bus.add_message_handler.call_args.args[0]
    assert (
        handler(
            types.SimpleNamespace(
                message_type="signal", interface="org.bluez.GattCharacteristic1"
            )
        )
        is None
    )


@pytest.mark.parametrize("device", ["another central", None])
@pytest.mark.parametrize(
    "first_path,second_path,write",
    [
        (FROMNUM_UUID, FROMRADIO_UUID, None),
        (FROMRADIO_UUID, FROMNUM_UUID, None),
        (FROMRADIO_UUID, TORADIO_UUID, b"command"),
        (TORADIO_UUID, FROMRADIO_UUID, None),
    ],
)
def test_bluez_session_identity_covers_all_characteristics(
    bluez_server: tuple[MagicMock, BLEMailbox, Callable[..., Any]],
    device: str | None,
    first_path: str,
    second_path: str,
    write: bytes | None,
) -> None:
    """Changing characteristic or using TORADIO cannot bypass the session owner."""
    server, mailbox, request = bluez_server
    mailbox.push(b"first")
    mailbox.push(b"second")
    request(path=first_path, write=b"initial" if first_path == TORADIO_UUID else None)
    reads, writes = server.read.call_count, server.write.call_count
    error = "InvalidOffset" if write is None else "NotPermitted"
    assert (
        request(device=device, path=second_path, write=write)
        == f"org.bluez.Error.{error}"
    )
    assert server.read.call_count == reads
    assert server.write.call_count == writes
    assert request() == (b"second" if first_path == FROMRADIO_UUID else b"first")
    assert request(path=TORADIO_UUID, write=b"accepted") == ()
    server.write.assert_called_with(
        server.get_characteristic(TORADIO_UUID).gatt, b"accepted"
    )


@pytest.mark.parametrize(
    "options",
    [
        {"offset": 1},
        {"offset": -1},
        {"offset": False},
        {"offset": "invalid"},
        {"prepare-authorize": True},
    ],
)
def test_bluez_rejects_partial_or_prepared_writes(
    bluez_server: tuple[MagicMock, BLEMailbox, Callable[..., Any]],
    options: dict[str, Any],
) -> None:
    """A fragment or prepare request must never become a complete radio command."""
    server, mailbox, request = bluez_server
    assert (
        request(path=TORADIO_UUID, write=b"fragment", **options)
        == "org.bluez.Error.NotSupported"
    )
    server.write.assert_not_called()
    # A rejected request cannot claim the session or consume its queued data.
    mailbox.push(b"original")
    assert request(device="valid central") == b"original"


@pytest.mark.parametrize("write_type", [None, "request", "command", "reliable"])
def test_bluez_forwards_complete_writes_unchanged(
    bluez_server: tuple[MagicMock, BLEMailbox, Callable[..., Any]],
    write_type: str | None,
) -> None:
    """Full writes, including already-assembled long writes, retain their payload."""
    server, _mailbox, request = bluez_server
    payload = bytes(range(256)) * 2
    options = {} if write_type is None else {"type": write_type}
    assert request(path=TORADIO_UUID, write=payload, **options) == ()
    server.write.assert_called_once_with(
        server.get_characteristic(TORADIO_UUID).gatt, payload
    )


def test_bluez_anonymous_backend_keeps_best_effort_behavior(
    bluez_server: tuple[MagicMock, BLEMailbox, Callable[..., Any]],
) -> None:
    """An entirely anonymous backend still works without inventing an identity."""
    server, mailbox, request = bluez_server
    mailbox.push(b"original")
    assert request(device=None, path=TORADIO_UUID, write=b"command") == ()
    assert request(device=None) == b"original"
    assert request(device=None, path=FROMNUM_UUID) == b"\x01\x00\x00\x00"
    server.write.assert_called_once()


def test_bluez_invalid_initial_read_does_not_claim_session(
    bluez_server: tuple[MagicMock, BLEMailbox, Callable[..., Any]],
) -> None:
    """Invalid continuations cannot block another central's first valid request."""
    server, mailbox, request = bluez_server
    mailbox.push(b"original")
    assert request(1, device="invalid central") == "org.bluez.Error.InvalidOffset"
    server.read.assert_not_called()
    assert request(device="valid central") == b"original"
