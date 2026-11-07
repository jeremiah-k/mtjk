"""Failure-path and ownership regressions for the experimental proxy transports."""

from __future__ import annotations

import asyncio
import errno
import socket
import sys
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from meshtastic.interface_proxy import channels, endpoints, server
from meshtastic.interface_proxy.channels import BLECentralChannel, SerialChannel
from meshtastic.interface_proxy.endpoints import (
    BLEMailbox,
    BLEPeripheralChannel,
    PTYChannel,
)
from meshtastic.interface_proxy.server import ProxyOptions
from meshtastic.interface_proxy.wire import frame


@pytest.mark.parametrize(
    ("upstream", "listener"),
    [
        ("tcp:127.0.0.1:4403", "tcp:localhost:4403"),
        ("tcp:localhost:4403", "tcp:127.0.0.1:4403"),
        ("tcp:[::1]:4403", "tcp:[::1]:4403"),
        ("tcp:LOCALHOST:4403", "tcp:[::1]:4403"),
    ],
)
def test_tcp_proxy_refuses_connecting_to_its_own_listener(
    upstream: str, listener: str
) -> None:
    """A same-port self-loop must fail before binding any listener."""
    with pytest.raises(ValueError, match="same endpoint"):
        asyncio.run(server.run_tcp_proxy(ProxyOptions(upstream, listener)))


@pytest.mark.parametrize(
    ("upstream", "listener"),
    [
        ("tcp:127.0.0.1:4403", "tcp:0.0.0.0:4403"),
        ("tcp:localhost:4403", "tcp:0.0.0.0:4403"),
        ("tcp:[::1]:4403", "tcp:[::]:4403"),
    ],
)
def test_public_wildcard_listener_cannot_proxy_to_own_loopback(
    upstream: str, listener: str
) -> None:
    with pytest.raises(ValueError, match="same endpoint"):
        asyncio.run(
            server.run_tcp_proxy(ProxyOptions(upstream, listener, allow_public=True))
        )


def test_tcp_proxy_permits_distinct_upstream_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Self-loop prevention does not reject another radio on the same host."""
    started = asyncio.Event()

    async def start_server(_handler: Any, _host: str, _port: int) -> Any:
        started.set()
        return types.SimpleNamespace(close=lambda: None, wait_closed=AsyncMock())

    async def scenario() -> None:
        monkeypatch.setattr(asyncio, "start_server", start_server)
        task = asyncio.create_task(
            server.run_tcp_proxy(
                ProxyOptions("tcp:localhost:4404", "tcp:127.0.0.1:4403")
            )
        )
        try:
            await asyncio.wait_for(started.wait(), 1)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(scenario())


def test_keepalive_uses_available_socket_options() -> None:
    """Enable keepalive and bounded liveness probes without requiring one OS."""
    sock = MagicMock()
    writer = MagicMock()
    writer.get_extra_info.return_value = sock
    server._enable_tcp_keepalive(writer)
    writer.get_extra_info.assert_called_once_with("socket")
    assert (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1) in [
        call.args for call in sock.setsockopt.call_args_list
    ]
    expected = [
        (socket.IPPROTO_TCP, option, setting)
        for name, setting in (
            ("TCP_KEEPIDLE", 30),
            ("TCP_KEEPINTVL", 10),
            ("TCP_KEEPCNT", 3),
        )
        if (option := getattr(socket, name, None)) is not None
    ]
    for args in expected:
        assert args in [call.args for call in sock.setsockopt.call_args_list]


def test_keepalive_skips_missing_or_rejected_socket() -> None:
    writer = MagicMock()
    writer.get_extra_info.return_value = None
    server._enable_tcp_keepalive(writer)
    writer.get_extra_info.return_value = sock = MagicMock()
    sock.setsockopt.side_effect = OSError(errno.ENOPROTOOPT, "no option")
    server._enable_tcp_keepalive(writer)
    assert sock.setsockopt.call_count == 1


def test_keepalive_probe_configuration_is_best_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sock = MagicMock()
    writer = MagicMock()
    writer.get_extra_info.return_value = sock
    monkeypatch.setattr(socket, "TCP_KEEPIDLE", 4, raising=False)

    def set_option(_level: int, option: int, _value: int) -> None:
        if option == 4:
            raise OSError(errno.ENOPROTOOPT, "unsupported")

    sock.setsockopt.side_effect = set_option
    server._enable_tcp_keepalive(writer)
    assert sock.setsockopt.call_count >= 2


@pytest.mark.parametrize(
    ("upstream", "downstream", "pattern"),
    [
        ("udp:radio", "pty", "--interface-proxy-in"),
        ("serial:", "pty", "serial upstream path"),
        ("ble:", "pty", "BLE upstream address"),
        ("serial:/dev/ttyS0", "invalid", "--interface-proxy-out"),
        ("serial:/dev/ttyS0", "ble-peripheral", "allow-public"),
        ("serial:/dev/ttyS0", "tcp:127.0.0.1:4403", "BLE advertised name"),
    ],
)
def test_bad_proxy_options_fail_fast(
    upstream: str, downstream: str, pattern: str
) -> None:
    opts = ProxyOptions(
        upstream, downstream, ble_name="" if "name" in pattern else "ok"
    )
    with pytest.raises(ValueError, match=pattern):
        opts.validate()


def test_bridge_propagates_send_failure() -> None:
    """The first failed direction cancels the waiting opposite direction."""

    class Channel:
        def __init__(self, *, fail: bool = False) -> None:
            self.events: asyncio.Queue[bytes | None] = asyncio.Queue()
            self.fail = fail
            self.sent: list[bytes] = []

        async def recv(self) -> bytes | None:
            return await self.events.get()

        async def send(self, payload: bytes) -> None:
            if self.fail:
                raise BrokenPipeError("lost downstream")
            self.sent.append(payload)

        async def close(self) -> None:
            pass

    async def scenario() -> None:
        source, sink = Channel(), Channel(fail=True)
        await source.events.put(b"\x08\x02")
        with pytest.raises(BrokenPipeError):
            await asyncio.wait_for(server.bridge(source, sink), 1)
        assert not sink.sent

    asyncio.run(scenario())


@pytest.mark.parametrize("endpoint", ["pty", "ble-peripheral"])
def test_single_endpoint_closes_both_sides_on_bridge_failure(
    monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    """A failing bridge cannot orphan an upstream or PTY/GATT endpoint."""
    upstream = MagicMock()
    upstream.close = AsyncMock()
    downstream = MagicMock()
    downstream.path = "/dev/pts/fake"
    downstream.close = AsyncMock()
    downstream.wait_for_central = AsyncMock()
    if endpoint == "pty":
        monkeypatch.setattr(endpoints.PTYChannel, "create", lambda: downstream)
    else:
        # A real instance is necessary for the server's isinstance() check.
        async def make_ble() -> BLEPeripheralChannel:
            obj = BLEPeripheralChannel(MagicMock(), BLEMailbox())
            monkeypatch.setattr(obj, "close", downstream.close)
            monkeypatch.setattr(obj, "wait_for_central", downstream.wait_for_central)
            return obj

        monkeypatch.setattr(
            endpoints.BLEPeripheralChannel, "start", lambda _name: make_ble()
        )
    monkeypatch.setattr(server, "connect_upstream", AsyncMock(return_value=upstream))
    monkeypatch.setattr(
        server, "bridge", AsyncMock(side_effect=OSError("bridge broke"))
    )

    async def scenario() -> None:
        with pytest.raises(OSError, match="bridge broke"):
            await server.run_single_endpoint(ProxyOptions("serial:/ignored", endpoint))

    asyncio.run(scenario())
    upstream.close.assert_awaited_once()
    downstream.close.assert_awaited_once()
    if endpoint == "ble-peripheral":
        downstream.wait_for_central.assert_awaited_once()


def test_single_pty_closes_on_upstream_connect_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downstream = MagicMock()
    downstream.path = "/dev/pts/fake"
    downstream.close = AsyncMock()
    monkeypatch.setattr(endpoints.PTYChannel, "create", lambda: downstream)
    monkeypatch.setattr(
        server, "connect_upstream", AsyncMock(side_effect=OSError("no radio"))
    )
    with pytest.raises(OSError, match="no radio"):
        asyncio.run(server.run_single_endpoint(ProxyOptions("serial:/missing", "pty")))
    downstream.close.assert_awaited_once()


@pytest.mark.parametrize("kind", ["serial", "tcp", "ble"])
def test_connect_upstream_routes_exactly_one_transport(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    serial_result, ble_result = MagicMock(), MagicMock()
    serial_open = AsyncMock(return_value=serial_result)
    ble_open = AsyncMock(return_value=ble_result)
    tcp_open = AsyncMock(return_value=(MagicMock(), MagicMock()))
    monkeypatch.setattr(channels.SerialChannel, "open", serial_open)
    monkeypatch.setattr(channels.BLECentralChannel, "open", ble_open)
    monkeypatch.setattr(asyncio, "open_connection", tcp_open)
    uri = {
        "serial": "serial:/dev/ttyUSB1",
        "tcp": "tcp:127.0.0.1:4403",
        "ble": "ble:UUID",
    }[kind]
    result = asyncio.run(channels.connect_upstream(uri))
    if kind == "serial":
        assert result is serial_result
        serial_open.assert_awaited_once_with("/dev/ttyUSB1")
    elif kind == "tcp":
        assert isinstance(result, channels.StreamChannel)
        tcp_open.assert_awaited_once_with("127.0.0.1", 4403)
    else:
        assert result is ble_result
        ble_open.assert_awaited_once_with("UUID")


@pytest.mark.parametrize("uri", ["", "serial:", "ble:", "udp:localhost:3"])
def test_invalid_upstream_uri_fails_before_transport_open(uri: str) -> None:
    with pytest.raises(ValueError, match="proxy input"):
        asyncio.run(channels.connect_upstream(uri))


def test_serial_recv_ignores_idle_and_preserves_frame() -> None:
    port = MagicMock()
    port.read.side_effect = [b"", frame(b"\x08\x01")]
    channel = SerialChannel(port)
    assert asyncio.run(asyncio.wait_for(channel.recv(), 1)) == b"\x08\x01"
    assert port.read.call_count == 2


def test_serial_send_rejects_short_write() -> None:
    port = MagicMock()
    port.write.return_value = 2
    channel = SerialChannel(port)
    with pytest.raises(OSError, match="short write"):
        asyncio.run(channel.send(b"\x08\x01"))


def test_serial_close_releases_port() -> None:
    port = MagicMock()
    asyncio.run(SerialChannel(port).close())
    port.close.assert_called_once()


def test_pty_send_retries_backpressure_and_partial_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = frame(b"\x08\x01")
    writes: list[bytes] = []

    def write(_fd: int, payload: bytes) -> int:
        writes.append(payload)
        if len(writes) == 1:
            raise BlockingIOError(errno.EAGAIN, "full PTY")
        if len(writes) == 2:
            return 1
        return len(payload)

    monkeypatch.setattr(endpoints, "os", types.SimpleNamespace(write=write))
    asyncio.run(PTYChannel(999, "fake").send(b"\x08\x01"))
    assert writes == [data, data, data[1:]]


@pytest.mark.parametrize("write_error", [0, OSError(errno.EACCES, "denied")])
def test_pty_send_rejects_no_progress_or_permission_error(
    monkeypatch: pytest.MonkeyPatch, write_error: object
) -> None:
    write = MagicMock()
    if isinstance(write_error, OSError):
        write.side_effect = write_error
    else:
        write.return_value = write_error
    monkeypatch.setattr(endpoints, "os", types.SimpleNamespace(write=write))
    with pytest.raises(OSError):
        asyncio.run(PTYChannel(999, "fake").send(b"\x08\x01"))


def test_pty_send_has_bounded_no_reader_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A permanently blocked PTY cannot stall the bridge forever."""
    loop_clock = iter((0.0, 6.0))
    monkeypatch.setattr(
        endpoints,
        "asyncio",
        types.SimpleNamespace(
            get_running_loop=lambda: types.SimpleNamespace(
                time=lambda: next(loop_clock)
            ),
            sleep=asyncio.sleep,
        ),
    )
    monkeypatch.setattr(
        "os.write",
        MagicMock(side_effect=BlockingIOError(errno.EAGAIN, "full")),
    )
    with pytest.raises(TimeoutError, match="PTY slave"):
        asyncio.run(PTYChannel(999, "fake").send(b"\x08\x01"))


def test_mailbox_counter_wrap_does_not_break_fromnum() -> None:
    box = BLEMailbox(capacity=1)
    box._counter = (1 << 32) - 1
    assert box.push(b"\x08\x01") == b"\x00\x00\x00\x00"
    assert box.pop() == b"\x08\x01"
    assert box.current_counter() == b"\x00\x00\x00\x00"


def test_ble_peripheral_recv_reports_disconnected_central() -> None:
    async def scenario() -> None:
        server_fake = MagicMock()
        server_fake.is_connected = AsyncMock(return_value=False)
        proxy = BLEPeripheralChannel(server_fake, BLEMailbox())
        assert await proxy.recv() is None

    asyncio.run(scenario())


def test_ble_peripheral_recv_wakes_for_delayed_packet() -> None:
    async def scenario() -> None:
        server_fake = MagicMock()
        server_fake.is_connected = AsyncMock(return_value=True)
        proxy = BLEPeripheralChannel(server_fake, BLEMailbox())
        task = asyncio.create_task(proxy.recv())
        await asyncio.sleep(0.01)
        proxy._incoming.put_nowait(b"\x08\x04")
        assert await asyncio.wait_for(task, 1) == b"\x08\x04"

    asyncio.run(scenario())


def test_ble_peripheral_requires_optional_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "bless", None)
    with pytest.raises(RuntimeError, match="optional 'bless'"):
        asyncio.run(BLEPeripheralChannel.start("radio"))


def test_ble_central_send_preserves_raw_proto_and_rejects_empty() -> None:
    async def scenario() -> None:
        client = MagicMock()
        client.write_gatt_char = AsyncMock()
        channel = BLECentralChannel(client)
        await channel.send(b"\x08\x02")
        client.write_gatt_char.assert_awaited_once()
        with pytest.raises(ValueError):
            await channel.send(b"")

    asyncio.run(scenario())


def test_tcp_proxy_listener_refuses_same_upstream_during_validation() -> None:
    with pytest.raises(ValueError, match="same endpoint"):
        ProxyOptions("tcp:localhost:4403", "tcp:127.0.0.1:4403").validate()


def test_proxy_dispatch_validates_then_routes_to_correct_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tcp = AsyncMock()
    single = AsyncMock()
    monkeypatch.setattr(server, "run_tcp_proxy", tcp)
    monkeypatch.setattr(server, "run_single_endpoint", single)
    options_tcp = ProxyOptions("serial:/dev/ttyUSB0", "tcp:127.0.0.1:4403")
    options_pty = ProxyOptions("serial:/dev/ttyUSB0", "pty")

    async def scenario() -> None:
        await server.serve(options_tcp)
        await server.serve(options_pty)

    asyncio.run(scenario())
    tcp.assert_awaited_once_with(options_tcp)
    single.assert_awaited_once_with(options_pty)


def test_proxy_options_rejects_oversize_ble_name() -> None:
    with pytest.raises(ValueError, match="BLE advertised name"):
        ProxyOptions("serial:/dev/null", "pty", ble_name="x" * 30).validate()


def test_bridge_propagates_upstream_reader_failure() -> None:
    async def scenario() -> None:
        upstream = MagicMock()
        downstream = MagicMock()
        upstream.recv = AsyncMock(side_effect=OSError("radio read failed"))

        async def await_forever() -> None:
            await asyncio.Future()

        downstream.recv = AsyncMock(side_effect=await_forever)
        with pytest.raises(OSError, match="radio read failed"):
            await asyncio.wait_for(server.bridge(upstream, downstream), 1)

    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform == "win32", reason="PTY requires POSIX add_reader")
def test_pty_wait_readable_wakes_on_fd_event() -> None:
    import os

    async def scenario() -> None:
        rfd, wfd = os.pipe()
        try:
            channel = PTYChannel(rfd, "pipe")
            task = asyncio.create_task(channel._wait_readable())
            await asyncio.sleep(0)
            os.write(wfd, b"x")
            await asyncio.wait_for(task, 1)
        finally:
            os.close(rfd)
            os.close(wfd)

    asyncio.run(scenario())


def test_pty_error_after_observed_slave_is_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = PTYChannel(1234, "fake")
    channel._slave_seen = True
    monkeypatch.setattr(
        endpoints,
        "os",
        types.SimpleNamespace(read=MagicMock(side_effect=OSError(errno.EIO, "hangup"))),
    )
    assert asyncio.run(channel.recv()) is None


def test_pty_initial_eio_waits_for_client(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = PTYChannel(1234, "fake")
    monkeypatch.setattr(
        endpoints,
        "os",
        types.SimpleNamespace(
            read=MagicMock(
                side_effect=[OSError(errno.EIO, "unopened"), frame(b"\x08\x03")]
            )
        ),
    )
    assert asyncio.run(channel.recv()) == b"\x08\x03"
    assert channel._slave_seen


def test_pty_create_error_closes_both_fds(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    if os.name != "posix":
        pytest.skip("PTY requires POSIX")
    # Preserve the real descriptor allocation so closed FDs can be checked.
    opened: list[int] = []
    original_openpty = os.openpty

    def allocate() -> tuple[int, int]:
        pair = original_openpty()
        opened.extend(pair)
        return pair

    monkeypatch.setattr(
        endpoints,
        "os",
        types.SimpleNamespace(name=os.name, openpty=allocate, close=os.close),
    )
    monkeypatch.setitem(
        sys.modules,
        "tty",
        types.SimpleNamespace(setraw=MagicMock(side_effect=OSError("setraw failed"))),
    )
    with pytest.raises(OSError, match="setraw failed"):
        PTYChannel.create()
    assert len(opened) == 2
    for fd in opened:
        with pytest.raises(OSError) as exc:
            os.fstat(fd)
        assert exc.value.errno == errno.EBADF


def test_pty_unavailable_platform_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(endpoints, "os", types.SimpleNamespace(name="nt"))
    with pytest.raises(OSError, match="only on POSIX"):
        PTYChannel.create()


def test_ble_peripheral_alias_restore_failure_still_releases_bus() -> None:
    async def scenario() -> None:
        backend = MagicMock()
        backend.stop = AsyncMock()
        channel = BLEPeripheralChannel(backend, BLEMailbox())
        channel._adapter_alias = "original"
        channel._adapter_properties = MagicMock()
        channel._adapter_properties.call_set = AsyncMock(
            side_effect=OSError("no adapter")
        )
        await channel.close()
        backend.bus.disconnect.assert_called_once()

    asyncio.run(scenario())


def test_single_pty_success_closes_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    downstream, upstream = MagicMock(), MagicMock()
    downstream.path = "/dev/pts/fake"
    downstream.close = AsyncMock()
    upstream.close = AsyncMock()
    monkeypatch.setattr(endpoints.PTYChannel, "create", lambda: downstream)
    monkeypatch.setattr(server, "connect_upstream", AsyncMock(return_value=upstream))
    bridge = AsyncMock()
    monkeypatch.setattr(server, "bridge", bridge)
    asyncio.run(server.run_single_endpoint(ProxyOptions("serial:/fake", "pty")))
    bridge.assert_awaited_once_with(upstream, downstream)
    downstream.close.assert_awaited_once()
    upstream.close.assert_awaited_once()
