"""Binary Meshtastic packet channels for stream and BLE central transports."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ParamSpec, Protocol, TypeVar, cast

from meshtastic._transport_selection import _open_tcp_connection
from meshtastic.interface_proxy.wire import FrameDecoder, frame, validate_payload
from meshtastic.interfaces.ble.constants import (
    FROMNUM_UUID,
    FROMRADIO_UUID,
    TORADIO_UUID,
)
from meshtastic.stream_interface import DEVICE_WAKE_DELAY, START2, WAKE_BYTE_COUNT

if TYPE_CHECKING:
    from bleak import BleakClient

_LOG = logging.getLogger(__name__)
_READ_CHUNK = 4096
_P = ParamSpec("_P")
_T = TypeVar("_T")


async def _serial_io(
    operation: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs
) -> _T:
    """Finish bounded serial I/O before cancellation allows the port to close.

    Cancelling to_thread only cancels its awaiter. Closing/reopening the port
    while its worker still reads or writes can race descriptor reuse.
    """
    worker = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await worker
        raise


class PacketChannel(Protocol):
    """One-direction-at-a-time byte-preserving protobuf channel."""

    async def recv(self) -> bytes | None:
        """Get the next complete protobuf message, or None for EOF."""

    async def send(self, payload: bytes) -> None:
        """Send one complete protobuf message."""

    async def close(self) -> None:
        """Release the channel's transport resources."""


class StreamChannel:
    """Framed stream reader/writer, backed by TCP or a POSIX file descriptor."""

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.reader = reader
        self.writer = writer
        self._decoder = FrameDecoder()
        self._pending: list[bytes] = []

    async def recv(self) -> bytes | None:
        """Read one packet, preserving other packets in the same stream chunk."""
        while not self._pending:
            chunk = await self.reader.read(_READ_CHUNK)
            if not chunk:
                return None
            self._pending.extend(self._decoder.feed(chunk))
        return self._pending.pop(0)

    async def send(self, payload: bytes) -> None:
        """Write one complete packet and apply asyncio transport backpressure."""
        self.writer.write(frame(payload))
        await self.writer.drain()

    async def close(self) -> None:
        """Close the stream and wait for shutdown where possible."""
        self.writer.close()
        with contextlib.suppress(OSError, ConnectionError, asyncio.TimeoutError):
            await asyncio.wait_for(self.writer.wait_closed(), 2)


class _SerialPort(Protocol):
    """Operations needed from an opened pyserial port."""

    def read(self, size: int) -> bytes:
        """Read up to ``size`` bytes or return empty bytes on timeout."""

    def write(self, data: bytes) -> int:
        """Return the number of bytes written."""

    def close(self) -> None:
        """Release the underlying file handle."""


class SerialChannel:
    """PySerial link with bounded blocking reads and explicit teardown."""

    def __init__(self, serial_port: _SerialPort) -> None:
        self._port = serial_port
        self._decoder = FrameDecoder()
        self._pending: list[bytes] = []

    @classmethod
    async def open(cls, path: str) -> SerialChannel:
        """Open one serial port at 115200 baud without initializing MeshInterface."""
        import serial  # type: ignore[import-untyped]  # pylint: disable=import-outside-toplevel  # optional at import time for pure framing tests

        def _open_port() -> _SerialPort:
            # Set control lines before opening, as SerialInterface does. The
            # default asserted RTS can hold USB radios in reset/bootloader mode.
            port = serial.Serial(
                port=None, baudrate=115200, timeout=0.2, write_timeout=5
            )
            try:
                port.port = path
                port.dtr = True  # nRF52 USB CDC requires DTR for output.
                port.rts = False
                if sys.platform != "win32":
                    port.exclusive = True
                port.open()
            except BaseException:
                port.close()
                raise
            return cast(_SerialPort, port)

        opening = asyncio.create_task(asyncio.to_thread(_open_port))
        try:
            port = await asyncio.shield(opening)
        except asyncio.CancelledError:
            # Cancelling to_thread cannot stop its worker. Reclaim the handle
            # after acquisition rather than losing it when the await is cancelled.
            with contextlib.suppress(Exception):
                port = await opening
                await asyncio.to_thread(port.close)
            raise
        try:
            # Match SerialInterface's wake/resynchronization behavior.
            await _serial_io(port.write, bytes([START2] * WAKE_BYTE_COUNT))
            await asyncio.sleep(DEVICE_WAKE_DELAY)
        except BaseException:
            await asyncio.to_thread(port.close)
            raise
        return cls(port)

    async def recv(self) -> bytes | None:
        """Read framed bytes; a serial idle timeout is not EOF."""
        while not self._pending:
            chunk = await _serial_io(self._port.read, _READ_CHUNK)
            if not chunk:
                await asyncio.sleep(0)
                continue
            self._pending.extend(self._decoder.feed(bytes(chunk)))
        return self._pending.pop(0)

    async def send(self, payload: bytes) -> None:
        """Send exactly one frame, rejecting partial/zero-progress writes."""
        data = frame(payload)
        written = await _serial_io(self._port.write, data)
        if written != len(data):
            raise OSError(f"serial proxy short write: {written}/{len(data)}")

    async def close(self) -> None:
        """Close the serial port; the bounded read timeout unblocks recv."""
        await asyncio.to_thread(self._port.close)


class BLECentralChannel:
    """Bleak central link to a real Meshtastic BLE radio.

    FROMNUM indicates queued data; FROMRADIO reads consume it. A slow polling
    fallback handles firmware/backend notification loss. GATT payloads are raw.
    """

    def __init__(self, client: BleakClient) -> None:
        self._client = client
        self._wake = asyncio.Event()
        self._pending: asyncio.Queue[bytes] = asyncio.Queue(maxsize=256)
        self._transport_disconnected = asyncio.Event()
        self._disconnected = asyncio.Event()
        self._receive_error: Exception | None = None
        self._receive_task: asyncio.Task[None] | None = None

    @classmethod
    async def open(
        cls, address: str, *, adapter: str | None = None
    ) -> BLECentralChannel:
        """Connect to a BLE radio using the existing Bleak dependency."""
        from bleak import BleakClient  # pylint: disable=import-outside-toplevel

        loop = asyncio.get_running_loop()
        link: BLECentralChannel | None = None

        def _disconnected(_client: object) -> None:
            # Bleak backends may invoke callbacks from a non-event-loop thread,
            # including while connect() is still in progress. Never touch an
            # asyncio.Event directly from that callback.
            if link is not None:
                with contextlib.suppress(RuntimeError):  # event loop already closed
                    loop.call_soon_threadsafe(link._transport_disconnected.set)
                    loop.call_soon_threadsafe(link._wake.set)

        transport_options: dict[str, Any] = {}
        if adapter is not None:
            transport_options["bluez"] = {"adapter": adapter}
        client = BleakClient(
            address,
            disconnected_callback=_disconnected,
            **transport_options,
        )
        link = cls(client)

        def _notified(_char: object, _value: bytearray) -> None:
            with contextlib.suppress(RuntimeError):  # late notification on shutdown
                loop.call_soon_threadsafe(link._wake.set)

        try:
            await client.connect(timeout=20)
            await client.start_notify(FROMNUM_UUID, _notified)
            link._wake.set()  # drain anything queued before subscription
            link._receive_task = asyncio.create_task(link._poll_radio())
        except BaseException:
            with contextlib.suppress(Exception):
                await client.disconnect()
            raise
        return link

    async def _poll_radio(self) -> None:
        try:
            while not self._transport_disconnected.is_set():
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=0.4)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()
                # Multiple packets can accumulate for a single FROMNUM change.
                while not self._transport_disconnected.is_set():
                    payload = bytes(await self._client.read_gatt_char(FROMRADIO_UUID))
                    if not payload:
                        break
                    validate_payload(payload)
                    # Stop fetching GATT values while the downstream is slow;
                    # a full queue is backpressure, not a radio disconnect.
                    await self._pending.put(payload)
        except Exception as exc:
            self._receive_error = exc
            _LOG.exception("BLE upstream read stopped")
        finally:
            # Signal EOF only after a packet blocked on queue space is enqueued.
            self._disconnected.set()

    async def recv(self) -> bytes | None:
        """Wait for a packet or disconnect, draining buffered data first.

        A GATT read failure must not be disguised as a normal radio EOF.
        """
        if not self._pending.empty():
            return self._pending.get_nowait()
        if self._disconnected.is_set():
            self._raise_receive_error()
            return None

        packet_task = asyncio.create_task(self._pending.get())
        disconnect_task = asyncio.create_task(self._disconnected.wait())
        try:
            await asyncio.wait(
                (packet_task, disconnect_task), return_when=asyncio.FIRST_COMPLETED
            )
            if packet_task.done():
                return packet_task.result()
            # A packet and disconnect can become ready in the same iteration.
            if not self._pending.empty():
                return self._pending.get_nowait()
            self._raise_receive_error()
            return None
        finally:
            for task in (packet_task, disconnect_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(packet_task, disconnect_task, return_exceptions=True)

    def _raise_receive_error(self) -> None:
        """Distinguish an invalid/failed GATT read from a clean disconnect."""
        if self._receive_error is not None:
            raise RuntimeError("BLE upstream receive failed") from self._receive_error

    async def send(self, payload: bytes) -> None:
        """Write one unframed protobuf to TORADIO."""
        await self._client.write_gatt_char(
            TORADIO_UUID, validate_payload(payload), response=True
        )

    async def close(self) -> None:
        """Cancel receive and disconnect the GATT client."""
        if self._receive_task is not None:
            self._receive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._receive_task
        await self._client.disconnect()


async def connect_upstream(
    uri: str, *, ble_adapter: str | None = None, network_interface: str | None = None
) -> PacketChannel:
    """Connect to a serial, TCP, or BLE upstream radio endpoint."""
    if uri.startswith("serial:") and uri[7:]:
        return await SerialChannel.open(uri[7:])
    if uri.startswith("tcp:"):
        host, port = parse_tcp_endpoint(uri[4:])
        reader, writer = await _open_tcp_connection(host, port, network_interface)
        return StreamChannel(reader, writer)
    if uri.startswith("ble:") and uri[4:]:
        return await BLECentralChannel.open(
            uri[4:], **({"adapter": ble_adapter} if ble_adapter is not None else {})
        )
    raise ValueError("proxy input must be serial:PATH, tcp:HOST:PORT, or ble:ADDRESS")


def parse_tcp_endpoint(text: str) -> tuple[str, int]:
    """Parse TCP endpoints, allowing bracketed IPv6 without colon ambiguity."""
    if text.startswith("["):
        closing = text.find("]")
        if closing == -1 or text[closing + 1 : closing + 2] != ":":
            raise ValueError("IPv6 TCP endpoints require [HOST]:PORT")
        host, port_text = text[1:closing], text[closing + 2 :]
    else:
        if text.count(":") != 1:
            raise ValueError("TCP endpoint must be HOST:PORT (IPv6 uses [HOST]:PORT)")
        host, port_text = text.rsplit(":", 1)
    if not host or not port_text.isdecimal():
        raise ValueError("TCP endpoint requires host and numeric port")
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise ValueError("TCP port must be in range 1..65535")
    return host, port
