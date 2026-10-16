"""Hardware-free interface proxy framing, gateway and BLE GATT contracts."""

from __future__ import annotations

import asyncio
import os
import sys
import types
from enum import IntFlag
from typing import Any
from unittest.mock import MagicMock

import pytest

from meshtastic.cli import bootstrap
from meshtastic.cli import parser as cli_parser
from meshtastic.interface_proxy.channels import BLECentralChannel
from meshtastic.interface_proxy.endpoints import (
    BLEMailbox,
    BLEPeripheralChannel,
    PTYChannel,
)
from meshtastic.interface_proxy.server import ProxyOptions, bridge, run_tcp_proxy
from meshtastic.interface_proxy.wire import FrameDecoder, ProxyProtocolError, frame
from meshtastic.interfaces.ble.constants import (
    FROMNUM_UUID,
    FROMRADIO_UUID,
    SERVICE_UUID,
    TORADIO_UUID,
)


def test_fragmented_and_multiple_frames() -> None:
    """Framing survives fragmentation and multiple envelopes per chunk."""
    decoder = FrameDecoder()
    first, second = frame(b"\x08\x01"), frame(b"\x10\x03")
    assert decoder.feed(b"noise\x94") == []
    assert decoder.feed(first[1:3]) == []
    assert decoder.feed(first[3:] + second) == [b"\x08\x01", b"\x10\x03"]


def test_invalid_frame_lengths_rejected() -> None:
    """Invalid envelope lengths resynchronize instead of failing the stream."""
    for length in (0, 513, 65535):
        assert FrameDecoder().feed(b"\x94\xc3" + length.to_bytes(2, "big")) == []
    with pytest.raises(ProxyProtocolError):
        frame(b"x" * 513)


def test_decoder_resynchronizes_after_invalid_length() -> None:
    """Stream noise before a valid header cannot poison the next frame."""
    assert FrameDecoder().feed(b"\x94\xc3\xff\xff" + frame(b"\x08\x01")) == [
        b"\x08\x01"
    ]


@pytest.mark.parametrize(
    "upstream,downstream,allowed",
    [
        ("serial:/dev/ttyUSB0", "tcp:127.0.0.1:4403", True),
        ("tcp:localhost:4403", "pty", True),
        ("ble:AB:CD:EF:01:02:03", "ble-peripheral", False),
        ("serial:/dev/ttyUSB0", "tcp:0.0.0.0:4403", False),
    ],
)
def test_proxy_options(upstream: str, downstream: str, allowed: bool) -> None:
    """Validate directions and refuse unauthenticated public listeners by default."""
    options = ProxyOptions(upstream, downstream)
    if allowed:
        options.validate()
    else:
        with pytest.raises(ValueError, match="allow-public"):
            options.validate()


def test_public_bind_opt_in() -> None:
    """Allow a consciously exposed TCP bind, including IPv6 endpoints."""
    ProxyOptions("serial:/dev/null", "tcp:0.0.0.0:4403", allow_public=True).validate()
    ProxyOptions("tcp:[::1]:4403", "tcp:[::1]:4404").validate()
    ProxyOptions(
        "ble:AA:BB:CC:DD:EE:FF", "ble-peripheral", allow_public=True
    ).validate()


class MemoryChannel:
    """In-memory packet channel for bidirectional bridge tests."""

    def __init__(self) -> None:
        self.incoming: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.outgoing: asyncio.Queue[bytes] = asyncio.Queue()

    async def recv(self) -> bytes | None:
        return await self.incoming.get()

    async def send(self, payload: bytes) -> None:
        await self.outgoing.put(payload)

    async def close(self) -> None:
        pass


def test_bridge_keeps_message_bytes_in_both_directions() -> None:
    """Admin/MQTT/raw protobuf bytes pass through without reserialization."""

    async def scenario() -> None:
        upstream, downstream = MemoryChannel(), MemoryChannel()
        bridge_task = asyncio.create_task(bridge(upstream, downstream))
        radio_packet, app_packet = b"\x08\x90\x01", b"\x12\xff\x00\x99"
        await upstream.incoming.put(radio_packet)
        await downstream.incoming.put(app_packet)
        assert await asyncio.wait_for(downstream.outgoing.get(), 1) == radio_packet
        assert await asyncio.wait_for(upstream.outgoing.get(), 1) == app_packet
        await downstream.incoming.put(None)
        await asyncio.wait_for(bridge_task, 1)

    asyncio.run(scenario())


def test_tcp_proxy_forwards_and_rejects_second_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only one TCP client can claim a given radio session."""

    async def scenario() -> None:
        from meshtastic.interface_proxy import server as module

        upstream = MemoryChannel()
        claimed = asyncio.Event()

        async def fake_connect(_uri: str) -> MemoryChannel:
            claimed.set()
            return upstream

        monkeypatch.setattr(module, "connect_upstream", fake_connect)
        # Acquire an ephemeral port without requiring a hard-coded test port.
        import socket

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])
        task = asyncio.create_task(
            run_tcp_proxy(ProxyOptions("serial:/ignored", f"tcp:127.0.0.1:{port}"))
        )
        try:
            for _ in range(50):
                try:
                    reader, writer = await asyncio.open_connection("127.0.0.1", port)
                    break
                except ConnectionRefusedError:
                    await asyncio.sleep(0.01)
            else:
                pytest.fail("TCP listener did not start")
            await asyncio.wait_for(claimed.wait(), 1)
            second_reader, second_writer = await asyncio.open_connection(
                "127.0.0.1", port
            )
            assert await asyncio.wait_for(second_reader.read(1), 1) == b""
            second_writer.close()
            await second_writer.wait_closed()
            writer.write(frame(b"\x08\x03"))
            await writer.drain()
            assert await asyncio.wait_for(upstream.outgoing.get(), 1) == b"\x08\x03"
            await upstream.incoming.put(b"\x10\x06")
            result = await asyncio.wait_for(reader.readexactly(6), 1)
            assert result == frame(b"\x10\x06")
            writer.close()
            await writer.wait_closed()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(scenario())


@pytest.mark.skipif(os.name != "posix", reason="PTY is available on POSIX")
def test_pty_virtual_serial_roundtrip() -> None:
    """A real PTY slave exposes the same framed Meshtastic wire protocol."""

    async def scenario() -> None:
        channel = PTYChannel.create()
        slave = os.open(channel.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            os.write(slave, frame(b"\x08\x07"))
            assert await asyncio.wait_for(channel.recv(), 1) == b"\x08\x07"
            await channel.send(b"\x10\x08")
            assert os.read(slave, 256) == frame(b"\x10\x08")
        finally:
            os.close(slave)
            await channel.close()

    asyncio.run(scenario())


def test_ble_mailbox_preserves_fifo_and_tracks_fromnum() -> None:
    """BLE FROMRADIO reads consume queued packets with 32-bit FROMNUM counters."""
    mailbox = BLEMailbox(capacity=2)
    assert mailbox.push(b"\x08\x01") == b"\x01\x00\x00\x00"
    assert mailbox.push(b"\x10\x01") == b"\x02\x00\x00\x00"
    with pytest.raises(OverflowError):
        mailbox.push(b"\x20\x01")
    assert mailbox.pop() == b"\x08\x01"
    assert mailbox.pop() == b"\x10\x01"
    assert mailbox.pop() == b""
    mailbox.reset()
    assert mailbox.current_counter() == b"\x02\x00\x00\x00"


def test_ble_upstream_drains_buffer_after_disconnect() -> None:
    """Do not drop already-received packets when the BLE central disconnects."""

    async def scenario() -> None:
        channel = BLECentralChannel(MagicMock())
        channel._pending.put_nowait(b"\x08\x07")
        channel._disconnected.set()
        assert await channel.recv() == b"\x08\x07"
        assert await channel.recv() is None

    asyncio.run(scenario())


def test_ble_peripheral_gatt_shape_and_mailbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fake Bless enforces Meshtastic UUID, raw read/write, FROMNUM contract."""

    class Flags(IntFlag):
        read = 1
        write = 2
        write_without_response = 4
        notify = 8
        readable = 16
        writeable = 32

    async def scenario() -> None:
        class Char:
            def __init__(self, uuid: str) -> None:
                self.uuid = uuid
                self.value = bytearray()

        class FakeServer:
            def __init__(self, *, name: str) -> None:
                self.name = name
                self.characteristics: dict[str, Char] = {}
                self.service: str | None = None
                self.notified: list[tuple[str, str]] = []
                self.stopped = False

            async def add_new_service(self, uuid: str) -> None:
                self.service = uuid

            async def add_new_characteristic(
                self, _service: str, uuid: str, *_args: Any
            ) -> None:
                self.characteristics[uuid] = Char(uuid)

            def get_characteristic(self, uuid: str) -> Char:
                return self.characteristics[uuid]

            def update_value(self, service: str, uuid: str) -> None:
                self.notified.append((service, uuid))

            async def start(self) -> None:
                pass

            async def stop(self) -> None:
                self.stopped = True

            async def is_connected(self) -> bool:
                return True

        monkeypatch.setitem(
            sys.modules,
            "bless",
            types.SimpleNamespace(
                BlessServer=FakeServer,
                GATTAttributePermissions=Flags,
                GATTCharacteristicProperties=Flags,
            ),
        )
        proxy = await BLEPeripheralChannel.start("Meshtastic_0123")
        try:
            server = proxy.server
            assert server.service == SERVICE_UUID
            assert set(server.characteristics) == {
                TORADIO_UUID,
                FROMRADIO_UUID,
                FROMNUM_UUID,
            }
            server.write_request_func(
                server.get_characteristic(TORADIO_UUID), b"\x08\x07", object()
            )
            await asyncio.sleep(0)
            # The central may write before wait_for_central notices connection.
            await proxy.wait_for_central()
            assert await asyncio.wait_for(proxy.recv(), 1) == b"\x08\x07"
            await proxy.send(b"\x10\x04")
            assert (
                server.read_request_func(
                    server.get_characteristic(FROMRADIO_UUID), object()
                )
                == b"\x10\x04"
            )
            assert (
                server.read_request_func(server.get_characteristic(FROMRADIO_UUID))
                == b""
            )
            assert (
                server.read_request_func(server.get_characteristic(FROMNUM_UUID))
                == b"\x01\x00\x00\x00"
            )
            assert server.notified == [(SERVICE_UUID, FROMNUM_UUID)]
        finally:
            await proxy.close()
            assert server.stopped

    asyncio.run(scenario())


def test_proxy_cli_routes_without_normal_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI proxy mode bypasses normal MeshInterface construction and callbacks."""
    arg_parser = __import__("argparse").ArgumentParser(add_help=False)
    args = cli_parser.parse_cli_args(
        arg_parser,
        version="test",
        argv=[
            "--interface-proxy-in",
            "serial:/dev/ttyUSB0",
            "--interface-proxy-out",
            "tcp:127.0.0.1:4403",
        ],
    )
    launched: list[ProxyOptions] = []

    async def fake_serve(options: ProxyOptions) -> None:
        launched.append(options)

    from meshtastic.interface_proxy import server

    monkeypatch.setattr(server, "serve", fake_serve)
    bootstrap.run_common(
        args, arg_parser, MagicMock(), argv=["mtjk", "--interface-proxy-in"]
    )
    assert len(launched) == 1
    assert launched[0].upstream == "serial:/dev/ttyUSB0"


def test_serial_upstream_wakes_device_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raw serial bridging sends the normal 32-byte startup wake sequence."""

    async def scenario() -> None:
        import serial  # type: ignore[import-untyped]

        class FakeSerial:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                self.writes: list[bytes] = []
                self.closed = False

            def write(self, data: bytes) -> int:
                self.writes.append(data)
                return len(data)

            def close(self) -> None:
                self.closed = True

        fake = FakeSerial()
        monkeypatch.setattr(serial, "Serial", lambda *_args, **_kwargs: fake)
        from meshtastic.interface_proxy.channels import SerialChannel

        channel = await SerialChannel.open("/dev/fake")
        try:
            assert fake.writes == [bytes([0xC3]) * 32]
            await channel.send(b"\x08\x01")
            assert fake.writes[-1] == frame(b"\x08\x01")
        finally:
            await channel.close()
        assert fake.closed

    asyncio.run(scenario())


def test_proxy_cli_refuses_mixed_normal_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A proxy request must not silently ignore ordinary device mutations."""
    import argparse

    parser = argparse.ArgumentParser(add_help=False)
    args = cli_parser.parse_cli_args(
        parser,
        version="test",
        argv=[
            "--interface-proxy-in",
            "serial:/dev/ttyUSB0",
            "--interface-proxy-out",
            "pty",
            "--sendtext",
            "dangerous-mixed-command",
        ],
    )
    from meshtastic.interface_proxy import server

    launch = MagicMock()
    monkeypatch.setattr(server, "serve", launch)
    with pytest.raises(SystemExit) as exit_info:
        bootstrap.run_common(
            args, parser, MagicMock(), argv=["mtjk", "--interface-proxy-in"]
        )
    assert exit_info.value.code == 2
    launch.assert_not_called()


def test_proxy_cli_refuses_dry_run_combination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dry-run preview must never start a real blocking proxy listener."""
    import argparse

    parser = argparse.ArgumentParser(add_help=False)
    args = cli_parser.parse_cli_args(
        parser,
        version="test",
        argv=[
            "--interface-proxy-in",
            "serial:/dev/ttyUSB0",
            "--interface-proxy-out",
            "pty",
            "--dry-run",
        ],
    )
    from meshtastic.interface_proxy import server

    launch = MagicMock()
    monkeypatch.setattr(server, "serve", launch)
    with pytest.raises(SystemExit) as exit_info:
        bootstrap.run_common(
            args, parser, MagicMock(), argv=["mtjk", "--interface-proxy-in"]
        )
    assert exit_info.value.code == 2
    launch.assert_not_called()


def test_ble_mailbox_rejects_invalid_payloads() -> None:
    """BLE GATT reads must not queue invalid or unbounded protobuf blobs."""
    mailbox = BLEMailbox(capacity=1)
    for payload in (b"", b"x" * 513):
        with pytest.raises(ProxyProtocolError):
            mailbox.push(payload)
    assert mailbox.pop() == b""


def test_proxy_cli_rejects_ordinary_transport_selector() -> None:
    """Proxy endpoint URIs cannot be combined with normal --ble connection."""
    import argparse

    parser = argparse.ArgumentParser(add_help=False)
    args = cli_parser.parse_cli_args(
        parser,
        version="test",
        argv=[
            "--interface-proxy-in",
            "serial:/dev/ttyUSB0",
            "--interface-proxy-out",
            "tcp:127.0.0.1:4403",
            "--ble",
            "any",
        ],
    )
    with pytest.raises(SystemExit) as exc_info:
        bootstrap.run_common(args, parser, MagicMock())
    assert exc_info.value.code == 2


def test_proxy_cli_rejects_bare_proxy_only_options() -> None:
    """Proxy-only options without both --interface-proxy-in/out are refused."""
    import argparse

    parser = argparse.ArgumentParser(add_help=False)
    args = cli_parser.parse_cli_args(
        parser,
        version="test",
        argv=["--interface-proxy-allow-public", "--info"],
    )
    with pytest.raises(SystemExit) as exc_info:
        bootstrap.run_common(args, parser, MagicMock())
    assert exc_info.value.code == 2


def test_ble_upstream_backpressure_preserves_full_queue() -> None:
    """A slow client pauses GATT reads rather than disconnecting at 256 packets."""

    async def scenario() -> None:
        from unittest.mock import AsyncMock

        read_257 = asyncio.Event()
        read_258 = asyncio.Event()
        reads = 0

        async def read_gatt(_uuid: str) -> bytes:
            nonlocal reads
            reads += 1
            if reads == 257:
                read_257.set()
            elif reads == 258:
                read_258.set()
            return b"\x08\x01" if reads <= 257 else b""

        client = MagicMock()
        client.read_gatt_char = AsyncMock(side_effect=read_gatt)
        client.disconnect = AsyncMock()
        channel = BLECentralChannel(client)
        channel._wake.set()
        channel._receive_task = asyncio.create_task(channel._poll_radio())
        try:
            await asyncio.wait_for(read_257.wait(), 1)
            assert channel._pending.qsize() == 256
            assert not channel._disconnected.is_set()
            assert await channel.recv() == b"\x08\x01"
            # The blocked 257th write must complete before the next GATT read.
            await asyncio.wait_for(read_258.wait(), 1)
            assert channel._pending.qsize() == 256
            assert not channel._disconnected.is_set()
        finally:
            await channel.close()

    asyncio.run(scenario())


def test_ble_upstream_recv_wakes_without_polling_and_drains_on_error() -> None:
    """An awaiting recv wakes on packets/disconnect and reports invalid GATT data."""

    async def scenario() -> None:
        channel = BLECentralChannel(MagicMock())
        waiter = asyncio.create_task(channel.recv())
        await channel._pending.put(b"\x08\x02")
        assert await asyncio.wait_for(waiter, 1) == b"\x08\x02"

        # An in-flight waiter must complete immediately after clean disconnect.
        waiter = asyncio.create_task(channel.recv())
        channel._disconnected.set()
        assert await asyncio.wait_for(waiter, 1) is None

        # A GATT read error is not a normal EOF, even after buffered packets.
        failed = BLECentralChannel(MagicMock())
        await failed._pending.put(b"\x10\x02")
        failed._receive_error = ProxyProtocolError("invalid GATT length")
        failed._disconnected.set()
        assert await failed.recv() == b"\x10\x02"
        with pytest.raises(RuntimeError, match="BLE upstream receive failed") as exc:
            await failed.recv()
        assert isinstance(exc.value.__cause__, ProxyProtocolError)

    asyncio.run(scenario())


def test_ble_upstream_invalid_read_reports_failure() -> None:
    """An oversized GATT response is surfaced rather than disguised as EOF."""

    async def scenario() -> None:
        from unittest.mock import AsyncMock

        client = MagicMock()
        client.read_gatt_char = AsyncMock(return_value=b"x" * 513)
        channel = BLECentralChannel(client)
        channel._wake.set()
        await asyncio.wait_for(channel._poll_radio(), 1)
        with pytest.raises(RuntimeError, match="BLE upstream receive failed") as exc:
            await channel.recv()
        assert isinstance(exc.value.__cause__, ProxyProtocolError)

    asyncio.run(scenario())


def test_ble_peripheral_does_not_discard_writes_on_repeated_session_wait() -> None:
    """One GATT server is single-use; a second wait must never erase early writes."""

    async def scenario() -> None:
        from unittest.mock import AsyncMock

        server = MagicMock()
        server.is_connected = AsyncMock(return_value=True)
        channel = BLEPeripheralChannel(server, BLEMailbox())
        # BLE write can arrive before is_connected() reports the connection.
        channel._incoming.put_nowait(b"\x08\x07")
        await channel.wait_for_central()
        assert await channel.recv() == b"\x08\x07"

        # A second central can write before an accidental repeat wait call.
        # Without a portable identity we refuse reuse, rather than discard data
        # or silently mix the previous central's session with a new one.
        channel._incoming.put_nowait(b"\x10\x07")
        channel.mailbox.push(b"\x12\x07")
        with pytest.raises(RuntimeError, match="single-session"):
            await channel.wait_for_central()
        assert await channel.recv() == b"\x10\x07"
        assert channel.mailbox.pop() == b"\x12\x07"
        server.is_connected.assert_awaited_once()

    asyncio.run(scenario())


def test_ble_upstream_idle_timeout_keeps_connection_alive() -> None:
    """An ordinary polling timeout is not a radio disconnect."""

    async def scenario() -> None:
        from unittest.mock import AsyncMock

        client = MagicMock()
        client.read_gatt_char = AsyncMock(return_value=b"")
        client.disconnect = AsyncMock()
        channel = BLECentralChannel(client)
        channel._receive_task = asyncio.create_task(channel._poll_radio())
        try:
            await asyncio.sleep(0.46)
            assert not channel._disconnected.is_set()
            assert channel._receive_error is None
            assert client.read_gatt_char.await_count >= 1
        finally:
            await channel.close()

    asyncio.run(scenario())


def test_stream_close_suppresses_asyncio_timeout() -> None:
    """Slow close does not overwrite the underlying session outcome."""
    from unittest.mock import AsyncMock

    from meshtastic.interface_proxy.channels import StreamChannel

    async def scenario() -> None:
        writer = MagicMock()
        writer.wait_closed = AsyncMock(side_effect=asyncio.TimeoutError())
        await StreamChannel(asyncio.StreamReader(), writer).close()
        writer.close.assert_called_once()

    asyncio.run(scenario())


def test_proxy_ctrl_c_exits_without_traceback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Ctrl+C behaves like the ordinary CLI listen-loop shutdown path."""
    import argparse

    parser = argparse.ArgumentParser(add_help=False)
    args = cli_parser.parse_cli_args(
        parser,
        version="test",
        argv=[
            "--interface-proxy-in",
            "serial:/dev/ttyUSB0",
            "--interface-proxy-out",
            "tcp:127.0.0.1:4403",
        ],
    )

    def interrupt_proxy(coroutine: Any) -> None:
        # asyncio.run normally closes its coroutine during Ctrl+C teardown.
        coroutine.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(asyncio, "run", interrupt_proxy)
    with caplog.at_level("INFO", logger=bootstrap.logger.name):
        bootstrap.run_common(
            args, parser, MagicMock(), argv=["mtjk", "--interface-proxy-in"]
        )
    assert "Exiting interface proxy due to keyboard interrupt" in caplog.text
