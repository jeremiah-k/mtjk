"""BlueZ GATT options and session isolation missing from Bless callbacks."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from meshtastic.interfaces.ble.constants import (
    FROMNUM_UUID,
    FROMRADIO_UUID,
    TORADIO_UUID,
)

_LOG = logging.getLogger(__name__)
_GATT_INTERFACE = "org.bluez.GattCharacteristic1"


def _install_advertising(server: Any) -> None:
    """Make this advertisement discoverable without changing the host's mode.

    Bless 0.3.0 omits Discoverable, inheriting the desktop adapter's setting.
    BlueZ can advertise a discoverable LE peripheral independently of BR/EDR;
    this matches firmware and avoids enabling classic desktop discoverability.
    """
    # pylint: disable=import-outside-toplevel,import-error
    from bless.backends.bluezdbus.dbus.advertisement import (  # type: ignore[import-not-found]
        BlueZLEAdvertisement,
        Type,
    )
    from dbus_next.constants import PropertyAccess  # type: ignore[import-not-found]
    from dbus_next.service import dbus_property  # type: ignore[import-not-found]

    class Advertisement(BlueZLEAdvertisement):  # type: ignore[misc]
        """Only this proxy's advertisement overrides discoverability."""

        @dbus_property(access=PropertyAccess.READ)  # type: ignore[untyped-decorator]
        def Discoverable(self) -> "b":  # type: ignore[name-defined]  # pylint: disable=invalid-name
            """Advertise general LE discoverability independently of the host."""
            return True

    app = server.app

    async def _start_advertising(adapter: Any) -> None:
        # Keep Bless's advertisement ownership/cleanup and local-name behavior.
        await app.set_name(adapter, app.app_name)
        advertisement = Advertisement(Type.PERIPHERAL, len(app.advertisements) + 1, app)
        app.advertisements.append(advertisement)
        advertisement._service_uuids.append(app.services[0].UUID)
        app.bus.export(advertisement.path, advertisement)
        manager = adapter.get_interface("org.bluez.LEAdvertisingManager1")
        await manager.call_register_advertisement(advertisement.path, {})

    app.start_advertising = _start_advertising


class _ReadSnapshot:
    """Keep one characteristic value across a central's ATT continuation reads."""

    def __init__(self) -> None:
        self._payload = b""
        self._device: str | None = None

    def _read(
        self, offset: int, device: str | None, consume: Callable[[], bytes]
    ) -> bytes:
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or not 0 <= offset <= 65535
        ):
            raise ValueError("Invalid read offset")
        # A second identified central must never consume the first central's
        # queued packets. The GATT server is intentionally single-session.
        if self._device is not None and device != self._device:
            raise ValueError("Invalid read offset")
        if offset == 0:
            self._payload = consume()
            self._device = device
        elif device != self._device or offset > len(self._payload):
            raise ValueError("Invalid read offset")
        return self._payload[offset:]


def _install_request_handler(server: Any) -> None:
    """Preserve consuming reads and one central across BlueZ GATT requests.

    Bless 0.3.0 discards read/write options before invoking our callbacks.
    Retain read offsets and session identity on the owned bus. TORADIO accepts
    complete writes only: fragments and prepared writes cannot be forwarded as
    independent radio commands. Leave all other requests to Bless.
    """
    # These are already required by Bless's Linux backend, but stay optional
    # when importing the proxy on other platforms or without BLE advertising.
    # pylint: disable=import-outside-toplevel,import-error
    from dbus_next import Message  # type: ignore[import-not-found]
    from dbus_next.constants import MessageType

    reads = {}
    for uuid in (FROMRADIO_UUID, FROMNUM_UUID):
        characteristic = server.get_characteristic(uuid).gatt
        reads[characteristic.path] = (characteristic, _ReadSnapshot())
    writable = server.get_characteristic(TORADIO_UUID).gatt
    central: str | None = None

    def _on_message(message: Any) -> Any:
        nonlocal central
        gatt_call = (
            message.message_type == MessageType.METHOD_CALL
            and message.interface == _GATT_INTERFACE
        )
        reading = (
            gatt_call
            and message.member == "ReadValue"
            and message.path in reads
            and message.signature == "a{sv}"
        )
        writing = (
            gatt_call
            and message.member == "WriteValue"
            and message.path == writable.path
            and message.signature == "aya{sv}"
        )
        if not reading and not writing:
            return None
        options = message.body[0 if reading else 1]
        offset = options["offset"].value if "offset" in options else 0
        device = options["device"].value if "device" in options else None
        if central is not None and device != central:
            error = "InvalidOffset" if reading else "NotPermitted"
            return Message.new_error(
                message, f"org.bluez.Error.{error}", "BLE proxy already has a central"
            )
        if writing:
            preparing = (
                options["prepare-authorize"].value
                if "prepare-authorize" in options
                else False
            )
            if (
                isinstance(offset, bool)
                or not isinstance(offset, int)
                or offset != 0
                or preparing
            ):
                return Message.new_error(
                    message,
                    "org.bluez.Error.NotSupported",
                    "TORADIO requires a complete, non-prepared write at offset zero",
                )
            server.write(writable, message.body[0])
            central = device
            return Message.new_method_return(message, "", [])
        characteristic, snapshot = reads[message.path]
        try:
            value = snapshot._read(
                offset, device, lambda: bytes(server.read(characteristic))
            )
        except ValueError:
            return Message.new_error(
                message, "org.bluez.Error.InvalidOffset", "Invalid read offset"
            )
        central = device
        _LOG.debug("BLE GATT read offset=%d returned %d bytes", offset, len(value))
        return Message.new_method_return(message, "ay", [value])

    server.bus.add_message_handler(_on_message)
