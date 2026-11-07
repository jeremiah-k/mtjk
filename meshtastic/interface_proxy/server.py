"""Session-owned, bidirectional Meshtastic transport gateway."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from dataclasses import dataclass

from meshtastic.interface_proxy.channels import (
    PacketChannel,
    StreamChannel,
    connect_upstream,
    parse_tcp_endpoint,
)
from meshtastic.interface_proxy.endpoints import BLEPeripheralChannel, PTYChannel

_LOG = logging.getLogger(__name__)


def _safe_listener_host(host: str, allow_public: bool) -> str:
    """Bind literal loopback for an opt-in-free ``localhost`` listener.

    System resolver overrides can map ``localhost`` to a routable address.
    Avoid passing the name to start_server when radio control is unauthenticated.
    Explicit IPv6 loopback (``[::1]``) remains available in the URI.
    """
    if not allow_public and host.lower() == "localhost":
        return "127.0.0.1"
    return host


def _enable_tcp_keepalive(writer: asyncio.StreamWriter) -> None:
    """Detect abandoned single-owner sessions without changing the wire protocol.

    Socket keepalive is best-effort: optional probe controls are not available
    on every OS, and some transports expose no socket through the writer.
    """
    sock = writer.get_extra_info("socket")
    if sock is None:
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        _LOG.debug("Could not enable TCP keepalive for proxy client", exc_info=True)
        return
    idle = getattr(socket, "TCP_KEEPIDLE", None)
    if idle is None:
        idle = getattr(socket, "TCP_KEEPALIVE", None)
    for option, value in (
        (idle, 30),
        (getattr(socket, "TCP_KEEPINTVL", None), 10),
        (getattr(socket, "TCP_KEEPCNT", None), 3),
    ):
        if option is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, option, value)
            except OSError:
                _LOG.debug("TCP keepalive probe option %s unavailable", option)


def _reject_self_proxy(upstream: str, listen_host: str, listen_port: int) -> None:
    """Reject a listener that would connect upstream to its own TCP port.

    Loopback hostname aliases require special treatment because a caller may
    request ``localhost`` while the safe listener binds numeric loopback.
    """
    if not upstream.startswith("tcp:"):
        return
    source_host, source_port = parse_tcp_endpoint(upstream[4:])
    if source_port != listen_port:
        return

    def _hosts(host: str) -> set[str]:
        if host.lower() == "localhost":
            return {"127.0.0.1", "::1"}
        try:
            return {str(ipaddress.ip_address(host))}
        except ValueError:
            return {host.casefold()}

    if _hosts(source_host) & _hosts(listen_host):
        raise ValueError("Proxy TCP input and output cannot be the same endpoint")


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
            host, port = parse_tcp_endpoint(self.downstream[4:])
            _reject_self_proxy(
                self.upstream, _safe_listener_host(host, self.allow_public), port
            )
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

    async def _transfer(source: PacketChannel, destination: PacketChannel) -> None:
        while (packet := await source.recv()) is not None:
            await destination.send(packet)

    tasks = (
        asyncio.create_task(_transfer(upstream, downstream), name="radio-to-client"),
        asyncio.create_task(_transfer(downstream, upstream), name="client-to-radio"),
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
    # This entrypoint is also callable without serve(); enforce the exposure
    # gate and never let the system resolver widen a localhost-only listener.
    options.validate()
    host, port = parse_tcp_endpoint(options.downstream[4:])
    host = _safe_listener_host(host, options.allow_public)
    _reject_self_proxy(options.upstream, host, port)
    active_client = False
    sessions: set[asyncio.Task[None]] = set()

    async def _handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        nonlocal active_client
        downstream = StreamChannel(reader, writer)
        # An absent FIN/RST after Wi-Fi loss must not monopolize the only
        # upstream radio indefinitely. Keepalive setup performs no awaits.
        _enable_tcp_keepalive(writer)
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
        except (ConnectionResetError, BrokenPipeError) as exc:
            _LOG.info("TCP proxy transport disconnected: %s", exc)
        except Exception:
            _LOG.exception("TCP proxy session failed")
        finally:
            active_client = False
            await downstream.close()

    def _accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Register synchronously: shutdown may run before the coroutine starts.
        task = asyncio.create_task(_handle(reader, writer))
        sessions.add(task)
        task.add_done_callback(sessions.discard)
        # A task cancelled before its first step never enters _handle's finally.
        task.add_done_callback(lambda _task: writer.close())

    server = await asyncio.start_server(_accept, host, port)
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
        downstream = await BLEPeripheralChannel.start(options.ble_name)
        _LOG.warning("BLE peripheral requires tested OS GATT adapter, pairing, and MTU")
    try:
        if isinstance(downstream, BLEPeripheralChannel):
            # Stop advertising when the current central/radio session ends.
            # Rebinding a live GATT client to a new upstream would mix the
            # old device's config stream with the replacement's state.
            await downstream.wait_for_central()
            _LOG.debug("BLE central subscribed; opening proxy upstream")
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
