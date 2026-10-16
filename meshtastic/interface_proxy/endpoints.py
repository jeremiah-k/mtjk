"""TCP, POSIX PTY and experimental BLE GATT downstream endpoints."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import errno
import logging
import os
import threading
from typing import Any

from meshtastic.interface_proxy.wire import FrameDecoder, frame, validate_payload
from meshtastic.interfaces.ble.constants import (
    FROMNUM_UUID,
    FROMRADIO_UUID,
    SERVICE_UUID,
    TORADIO_UUID,
)

_LOG = logging.getLogger(__name__)


class PTYChannel:
    """A Linux/macOS POSIX pseudo-terminal master, without extra dependencies.

    The OS allocates a slave device (such as /dev/pts/4). PTYs cannot prevent
    multiple unrelated processes from opening the slave; the operator owns
    client exclusivity. Do not use for concurrent Meshtastic clients.
    """

    def __init__(self, fd: int, path: str) -> None:
        self.fd = fd
        self.path = path
        self._decoder = FrameDecoder()
        self._pending: collections.deque[bytes] = collections.deque()
        self._slave_seen = False

    @classmethod
    def create(cls) -> PTYChannel:
        """Allocate a new raw PTY pair and return the master endpoint."""
        if os.name != "posix":
            raise OSError("virtual serial PTY is supported only on POSIX hosts")
        import tty  # pylint: disable=import-outside-toplevel

        master, slave = os.openpty()
        try:
            tty.setraw(slave)
            path = os.ttyname(slave)
            os.set_blocking(master, False)
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        return cls(master, path)

    async def _wait_readable(self) -> None:
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[None] = loop.create_future()

        def _on_readable() -> None:
            if not ready.done():
                ready.set_result(None)

        loop.add_reader(self.fd, _on_readable)
        try:
            await ready
        finally:
            loop.remove_reader(self.fd)

    async def recv(self) -> bytes | None:
        """Wait for the initial slave, then report EOF when that client closes."""
        while not self._pending:
            try:
                chunk = os.read(self.fd, 4096)
            except BlockingIOError:
                self._slave_seen = True
                await self._wait_readable()
                continue
            except OSError as exc:
                if exc.errno == errno.EIO:
                    if self._slave_seen:
                        return None
                    await asyncio.sleep(0.1)
                    continue
                raise
            if not chunk:
                if self._slave_seen:
                    return None
                await asyncio.sleep(0.1)
                continue
            self._slave_seen = True
            self._pending.extend(self._decoder.feed(chunk))
        return self._pending.popleft()

    async def send(self, payload: bytes) -> None:
        """Send one framed packet, bounded if the slave is not open."""
        data = frame(payload)
        offset = 0
        deadline = asyncio.get_running_loop().time() + 5
        while offset < len(data):
            try:
                written = os.write(self.fd, data[offset:])
                if written == 0:
                    raise OSError("PTY write made no progress")
                offset += written
            except (BlockingIOError, OSError) as exc:
                if isinstance(exc, OSError) and exc.errno not in (
                    errno.EAGAIN,
                    errno.EWOULDBLOCK,
                    errno.EIO,
                ):
                    raise
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError(
                        "PTY slave is absent or not consuming data"
                    ) from exc
                await asyncio.sleep(0.025)

    async def close(self) -> None:
        """Close the master, invalidating the ephemeral virtual serial path."""
        os.close(self.fd)


class BLEMailbox:
    """Bounded raw protobuf queue and FROMNUM counter for a GATT read endpoint.

    The GATT callback may arrive from another thread; a lock protects the queue.
    Overflow is terminal rather than silently dropping message data.
    """

    def __init__(self, capacity: int = 128) -> None:
        self._pending: collections.deque[bytes] = collections.deque()
        self._lock = threading.Lock()
        self._capacity = capacity
        self._counter = 0

    def push(self, payload: bytes) -> bytes:
        """Queue one FROMRADIO value and return the new FROMNUM notification."""
        validate_payload(payload)
        with self._lock:
            if len(self._pending) >= self._capacity:
                raise OverflowError("BLE peripheral unread FROMRADIO queue is full")
            self._pending.append(payload)
            self._counter = (self._counter + 1) % (1 << 32)
            return self._counter.to_bytes(4, "little")

    def pop(self) -> bytes:
        """Consume exactly one GATT FROMRADIO read, or return empty bytes."""
        with self._lock:
            return self._pending.popleft() if self._pending else b""

    def current_counter(self) -> bytes:
        """Return the last FROMNUM value atomically for a GATT read."""
        with self._lock:
            return self._counter.to_bytes(4, "little")

    def reset(self) -> None:
        """Drop a disconnected central's stale messages before the next session."""
        with self._lock:
            self._pending.clear()


class BLEPeripheralChannel:
    """Experimental Meshtastic BLE GATT server backed by optional Bless.

    This is an opt-in proof of concept: backends vary in bonding, MTU/long-read,
    advertising name support and connection identity. No multi-central support.
    """

    def __init__(self, server: Any, mailbox: BLEMailbox) -> None:
        self.server = server
        self.mailbox = mailbox
        self._incoming: asyncio.Queue[bytes] = asyncio.Queue(maxsize=128)
        self._loop = asyncio.get_running_loop()
        self._fatal: BaseException | None = None
        self._session_started = False

    @classmethod
    async def start(cls, name: str) -> BLEPeripheralChannel:
        """Start advertising the standard Meshtastic GATT service."""
        try:
            # Deferred optional dependency; the disable covers the import block below.
            # pylint: disable=import-outside-toplevel
            from bless import BlessServer  # type: ignore[import-not-found]
            from bless import GATTAttributePermissions as Perm
            from bless import GATTCharacteristicProperties as Prop
        except ImportError as exc:
            raise RuntimeError(
                "BLE advertising requires optional 'bless' (install: pip install bless)"
            ) from exc

        server = BlessServer(name=name)
        channel = cls(server, BLEMailbox())

        def on_read(characteristic: Any, *_args: Any, **_kwargs: Any) -> bytearray:
            uuid = str(characteristic.uuid).lower()
            if uuid == FROMRADIO_UUID:
                return bytearray(channel.mailbox.pop())
            if uuid == FROMNUM_UUID:
                return bytearray(channel.mailbox.current_counter())
            return bytearray()

        def enqueue(raw: bytes) -> None:
            try:
                channel._incoming.put_nowait(raw)
            except asyncio.QueueFull:
                channel._fatal = OverflowError("BLE peripheral TORADIO queue overflow")

        def on_write(
            characteristic: Any, value: Any, *_args: Any, **_kwargs: Any
        ) -> None:
            if str(characteristic.uuid).lower() != TORADIO_UUID:
                return
            try:
                payload = validate_payload(bytes(value))
            except ValueError as exc:
                channel._loop.call_soon_threadsafe(setattr, channel, "_fatal", exc)
                return
            channel._loop.call_soon_threadsafe(enqueue, payload)

        server.read_request_func = on_read
        server.write_request_func = on_write
        try:
            await server.add_new_service(SERVICE_UUID)
            await server.add_new_characteristic(
                SERVICE_UUID,
                TORADIO_UUID,
                Prop.write | Prop.write_without_response,
                bytearray(),
                Perm.writeable,  # bless 0.3.0 spells this permission "writeable"
            )
            await server.add_new_characteristic(
                SERVICE_UUID,
                FROMRADIO_UUID,
                Prop.read,
                bytearray(),
                Perm.readable,
            )
            await server.add_new_characteristic(
                SERVICE_UUID,
                FROMNUM_UUID,
                Prop.read | Prop.notify,
                bytearray(b"\x00\x00\x00\x00"),
                Perm.readable,
            )
            await server.start()
        except BaseException:
            with contextlib.suppress(Exception):
                await server.stop()
            raise
        _LOG.warning("Experimental BLE GATT peripheral advertising as %s", name)
        return channel

    async def wait_for_central(self) -> None:
        """Wait for the sole central session, preserving any early TORADIO writes.

        The gateway closes its GATT server after this session. Bless does not
        expose a portable central identity, so reusing one server for another
        central could mix state or discard writes at a reconnect boundary.
        """
        if self._session_started:
            raise RuntimeError(
                "BLE peripheral is single-session; start a new GATT server"
            )
        self._session_started = True
        while not await self.server.is_connected():
            await asyncio.sleep(0.25)

    async def recv(self) -> bytes | None:
        """Receive one unframed TORADIO write from a central."""
        while True:
            if self._fatal is not None:
                raise RuntimeError("BLE peripheral session failed") from self._fatal
            if not self._incoming.empty():
                return self._incoming.get_nowait()
            if not await self.server.is_connected():
                return None
            await asyncio.sleep(0.05)

    async def send(self, payload: bytes) -> None:
        """Queue a FROMRADIO read and notify FROMNUM subscribers."""
        counter = self.mailbox.push(payload)
        characteristic = self.server.get_characteristic(FROMNUM_UUID)
        characteristic.value = bytearray(counter)
        self.server.update_value(SERVICE_UUID, FROMNUM_UUID)

    async def close(self) -> None:
        """Unregister GATT services and stop advertising."""
        await self.server.stop()
