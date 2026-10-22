"""Session-owned, bidirectional Meshtastic transport gateway."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from dataclasses import dataclass

from meshtastic._transport_selection import (
    _listener_address,
    _network_addresses,
    _validate_ble_adapter,
)
from meshtastic.interface_proxy.channels import (
    PacketChannel,
    StreamChannel,
    connect_upstream,
    parse_tcp_endpoint,
)
from meshtastic.interface_proxy.endpoints import BLEPeripheralChannel, PTYChannel

_LOG = logging.getLogger(__name__)


def _validate_tcp_listener(host: str, allow_public: bool) -> None:
    """Check the resolved bind address, including when NIC addresses change."""
    if allow_public:
        return
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host.lower() == "localhost"
    if not loopback:
        raise ValueError(
            "non-loopback proxy listener requires --interface-proxy-allow-public"
        )


@dataclass(frozen=True)
class ProxyOptions:
    """One upstream and one exposed downstream transport endpoint."""

    upstream: str
    downstream: str
    allow_public: bool = False
    ble_name: str = "Meshtastic_0000"
    ble_adapter: str | None = None
    peripheral_adapter: str | None = None
    network_interface: str | None = None
    bind_interface: str | None = None

    def validate(self) -> None:
        """Reject unsupported endpoints and unsafe defaults before opening I/O."""
        _validate_ble_adapter(self.ble_adapter)
        _validate_ble_adapter(self.peripheral_adapter)
        if self.ble_adapter is not None and not self.upstream.startswith("ble:"):
            raise ValueError("--ble-adapter requires a BLE upstream")
        if self.peripheral_adapter is not None and self.downstream != "ble-peripheral":
            raise ValueError(
                "--interface-proxy-ble-adapter requires ble-peripheral output"
            )
        if self.network_interface is not None:
            if not self.upstream.startswith("tcp:"):
                raise ValueError("--tcp-interface requires a TCP upstream")
            _network_addresses(self.network_interface)
        if self.bind_interface is not None and not self.downstream.startswith("tcp:"):
            raise ValueError("--interface-proxy-bind-interface requires TCP output")
        if not (
            self.upstream.startswith("serial:")
            or self.upstream.startswith("tcp:")
            or self.upstream.startswith("ble:")
        ):
            raise ValueError(
                "--interface-proxy-in expects serial:PATH, tcp:HOST:PORT, or ble:ADDRESS"
            )
        if self.upstream.startswith("tcp:"):
            parse_tcp_endpoint(self.upstream[4:])
        if self.upstream.startswith("serial:") and not self.upstream[7:]:
            raise ValueError("serial upstream path cannot be empty")
        if self.upstream.startswith("ble:") and not self.upstream[4:]:
            raise ValueError("BLE upstream address cannot be empty")
        if self.downstream.startswith("tcp:"):
            host, _port = parse_tcp_endpoint(self.downstream[4:])
            host = _listener_address(host, self.bind_interface)
            _validate_tcp_listener(host, self.allow_public)
        elif self.downstream == "ble-peripheral":
            if not self.allow_public:
                raise ValueError(
                    "BLE advertising exposes unauthenticated radio control; "
                    "explicit --interface-proxy-allow-public is required"
                )
        elif self.downstream != "pty":
            raise ValueError(
                "--interface-proxy-out expects tcp:HOST:PORT, pty, or ble-peripheral"
            )
        if not self.ble_name or len(self.ble_name.encode("utf-8")) > 29:
            raise ValueError("BLE advertised name must be 1..29 UTF-8 bytes")


async def bridge(upstream: PacketChannel, downstream: PacketChannel) -> None:
    """Pump both directions until either endpoint closes or reports failure.

    Only transport envelopes change. Message content (including MQTT client
    proxy variants) is never parsed, filtered, decrypted, or rewritten.
    """

    async def transfer(source: PacketChannel, destination: PacketChannel) -> None:
        while (packet := await source.recv()) is not None:
            await destination.send(packet)

    tasks = (
        asyncio.create_task(transfer(upstream, downstream), name="radio-to-client"),
        asyncio.create_task(transfer(downstream, upstream), name="client-to-radio"),
    )
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_tcp_proxy(options: ProxyOptions) -> None:
    """Expose one radio to one TCP client at a time; reject other clients."""
    host, port = parse_tcp_endpoint(options.downstream[4:])
    host = _listener_address(host, options.bind_interface)
    _validate_tcp_listener(host, options.allow_public)
    active_client = False
    sessions: set[asyncio.Task[None]] = set()

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        nonlocal active_client
        downstream = StreamChannel(reader, writer)
        # Check and claim synchronously on the event loop; no await in between.
        if active_client:
            _LOG.warning("Rejecting additional TCP proxy client")
            await downstream.close()
            return
        active_client = True
        try:
            upstream = await _open_upstream(options)
            try:
                await bridge(upstream, downstream)
            finally:
                await upstream.close()
        except (ConnectionResetError, BrokenPipeError) as exc:
            _LOG.info("TCP proxy transport disconnected: %s", exc)
        except Exception:
            _LOG.exception("TCP proxy session failed")
        finally:
            active_client = False
            await downstream.close()

    def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Register synchronously: shutdown may run before the coroutine starts.
        task = asyncio.create_task(handle(reader, writer))
        sessions.add(task)
        task.add_done_callback(sessions.discard)
        # A task cancelled before its first step never enters handle's finally.
        task.add_done_callback(lambda _task: writer.close())

    server = await asyncio.start_server(accept, host, port)
    try:
        _LOG.warning("Unauthenticated Meshtastic proxy listening on %s:%d", host, port)
        # start_server already accepts connections. Own cancellation here:
        # older asyncio serve_forever implementations wait for clients to
        # close before returning, preventing us from cancelling their sessions.
        await asyncio.Future[None]()
    finally:
        server.close()
        remaining = tuple(sessions)
        for task in remaining:
            task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)
        await server.wait_closed()


async def run_single_endpoint(options: ProxyOptions) -> None:
    """Expose a radio as POSIX PTY or an experimental BLE peripheral."""
    downstream: PTYChannel | BLEPeripheralChannel
    if options.downstream == "pty":
        downstream = PTYChannel.create()
        print(f"Meshtastic proxy PTY: {downstream.path}", flush=True)
    else:
        downstream = await BLEPeripheralChannel.start(
            options.ble_name,
            **(
                {"adapter": options.peripheral_adapter}
                if options.peripheral_adapter is not None
                else {}
            ),
        )
        _LOG.warning("BLE peripheral requires tested OS GATT adapter, pairing, and MTU")
    try:
        if isinstance(downstream, BLEPeripheralChannel):
            # Stop advertising when the current central/radio session ends.
            # Rebinding a live GATT client to a new upstream would mix the
            # old device's config stream with the replacement's state.
            await downstream.wait_for_central()
            _LOG.debug("BLE central subscribed; opening proxy upstream")
        upstream = await _open_upstream(options)
        try:
            await bridge(upstream, downstream)
        finally:
            await upstream.close()
    finally:
        await downstream.close()


async def _open_upstream(options: ProxyOptions) -> PacketChannel:
    kwargs = {}
    if options.ble_adapter is not None:
        kwargs["ble_adapter"] = options.ble_adapter
    if options.network_interface is not None:
        kwargs["network_interface"] = options.network_interface
    return await connect_upstream(options.upstream, **kwargs)


async def serve(options: ProxyOptions) -> None:
    """Validate and start the configured interface-proxy server."""
    options.validate()
    if options.downstream.startswith("tcp:"):
        await run_tcp_proxy(options)
    else:
        await run_single_endpoint(options)
