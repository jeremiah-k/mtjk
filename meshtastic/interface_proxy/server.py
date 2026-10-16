"""Session-owned, bidirectional Meshtastic transport gateway."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from dataclasses import dataclass

from meshtastic.interface_proxy.channels import (
    PacketChannel,
    StreamChannel,
    connect_upstream,
    parse_tcp_endpoint,
)
from meshtastic.interface_proxy.endpoints import BLEPeripheralChannel, PTYChannel

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProxyOptions:
    """One upstream and one exposed downstream transport endpoint."""

    upstream: str
    downstream: str
    allow_public: bool = False
    ble_name: str = "Meshtastic_0000"

    def validate(self) -> None:
        """Reject unsupported endpoints and unsafe defaults before opening I/O."""
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
            if not self.allow_public:
                try:
                    loopback = ipaddress.ip_address(host).is_loopback
                except ValueError:
                    loopback = host.lower() == "localhost"
                if not loopback:
                    raise ValueError(
                        "non-loopback proxy listener requires --interface-proxy-allow-public"
                    )
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
    active_client = False

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
            upstream = await connect_upstream(options.upstream)
            try:
                await bridge(upstream, downstream)
            finally:
                await upstream.close()
        except Exception:
            _LOG.exception("TCP proxy session failed")
        finally:
            active_client = False
            await downstream.close()

    server = await asyncio.start_server(handle, host, port, reuse_address=True)
    async with server:
        _LOG.warning("Unauthenticated Meshtastic proxy listening on %s:%d", host, port)
        await server.serve_forever()


async def run_single_endpoint(options: ProxyOptions) -> None:
    """Expose a radio as POSIX PTY or an experimental BLE peripheral."""
    if options.downstream == "pty":
        downstream = PTYChannel.create()
        print(f"Meshtastic proxy PTY: {downstream.path}", flush=True)
    else:
        downstream = await BLEPeripheralChannel.start(options.ble_name)
        _LOG.warning("BLE peripheral requires tested OS GATT adapter, pairing, and MTU")
    try:
        if isinstance(downstream, BLEPeripheralChannel):
            # Stop advertising when the current central/radio session ends.
            # Rebinding a live GATT client to a new upstream would mix the
            # old device's config stream with the replacement's state.
            await downstream.wait_for_central()
        upstream = await connect_upstream(options.upstream)
        try:
            await bridge(upstream, downstream)
        finally:
            await upstream.close()
    finally:
        await downstream.close()


async def serve(options: ProxyOptions) -> None:
    """Validate and start the configured interface-proxy server."""
    options.validate()
    if options.downstream.startswith("tcp:"):
        await run_tcp_proxy(options)
    else:
        await run_single_endpoint(options)
