"""Transport selection must survive retries and preserve listener opt-in."""

from __future__ import annotations

import argparse
import asyncio
import errno
import socket
import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from meshtastic import _transport_selection as selection
from meshtastic.cli import bootstrap
from meshtastic.cli.parser import parse_cli_args
from meshtastic.interface_proxy.server import ProxyOptions
from meshtastic.interfaces.ble.connection import ClientManager
from meshtastic.interfaces.ble.discovery import DiscoveryManager


@pytest.fixture
def local_interfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    """Include a dual-stack NIC and a public interface for deterministic checks."""
    monkeypatch.setattr(
        selection,
        "_list_network_interfaces",
        lambda: [
            ("loop", "Loopback", ["::1", "127.0.0.1"]),
            ("wan", "Ethernet", ["192.0.2.10"]),
            ("empty", "Unconfigured", []),
        ],
    )


@pytest.mark.usefixtures("local_interfaces")
def test_interface_resolution_and_public_gate() -> None:
    """Apply exposure checks to the actual selected address, including IPv6."""
    assert selection._listener_address("0.0.0.0", "Loopback") == "127.0.0.1"
    assert selection._listener_address("::", "loop") == "::1"
    ProxyOptions(
        "serial:/ignored", "tcp:0.0.0.0:4403", bind_interface="loop"
    ).validate()
    with pytest.raises(ValueError, match="allow-public"):
        ProxyOptions(
            "serial:/ignored", "tcp:127.0.0.1:4403", bind_interface="wan"
        ).validate()
    for interface in ("missing", "empty", "0.0.0.0", "224.0.0.1"):
        with pytest.raises(ValueError):
            selection._network_addresses(interface)


def test_listener_rechecks_exposure_after_interface_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-resolution cannot change a validated loopback bind into a public bind."""
    from meshtastic.interface_proxy import server

    resolve = MagicMock(side_effect=["127.0.0.1", "192.0.2.10"])
    monkeypatch.setattr(server, "_listener_address", resolve)
    options = ProxyOptions(
        "serial:/ignored", "tcp:127.0.0.1:4403", bind_interface="moving"
    )
    options.validate()
    with pytest.raises(ValueError, match="allow-public"):
        asyncio.run(server.run_tcp_proxy(options))


def test_explicit_ip_works_without_interface_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restricted hosts can select an IP even if NIC enumeration is denied."""
    monkeypatch.setattr(
        selection, "_list_network_interfaces", MagicMock(side_effect=PermissionError())
    )
    assert selection._network_addresses("127.0.0.1") == ["127.0.0.1"]
    with pytest.raises(ValueError, match="local source IP"):
        selection._network_addresses("loop")


@pytest.mark.parametrize(
    "family,source", [(socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")]
)
def test_sync_tcp_binds_selected_source(family: int, source: str) -> None:
    """A real loopback peer observes the requested source address."""
    with socket.socket(family, socket.SOCK_STREAM) as listener:
        try:
            listener.bind((source, 0))
        except OSError:
            if family == socket.AF_INET6:
                pytest.skip("IPv6 loopback unavailable")
            raise
        listener.listen()
        listener.settimeout(2)
        client = selection._connect_tcp(source, listener.getsockname()[1], source, 2)
        try:
            peer, address = listener.accept()
            peer.close()
            assert address[0] == source
        finally:
            client.close()


def test_async_tcp_tries_compatible_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """Try the second local address when the first family cannot connect."""
    monkeypatch.setattr(
        selection, "_network_addresses", lambda _name: ["::1", "127.0.0.1"]
    )
    reader, writer = MagicMock(), MagicMock()
    connect = AsyncMock(side_effect=[OSError("no IPv6 route"), (reader, writer)])
    monkeypatch.setattr(asyncio, "open_connection", connect)
    assert asyncio.run(selection._open_tcp_connection("radio", 4403, "loop")) == (
        reader,
        writer,
    )
    assert [call.kwargs["local_addr"] for call in connect.call_args_list] == [
        ("::1", 0),
        ("127.0.0.1", 0),
    ]


def test_sync_tcp_falls_back_when_socket_family_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unsupported first socket family must not prevent a usable connection."""
    monkeypatch.setattr(
        selection, "_network_addresses", lambda _name: ["::1", "127.0.0.1"]
    )
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        MagicMock(
            side_effect=[
                [
                    (socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("::1", 4403, 0, 0)),
                    (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 4403)),
                ],
                [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0))],
            ]
        ),
    )
    connected = MagicMock()
    factory = MagicMock(
        side_effect=[OSError(errno.EAFNOSUPPORT, "IPv6 unavailable"), connected]
    )
    monkeypatch.setattr(socket, "socket", factory)
    assert selection._connect_tcp("radio", 4403, "loop", 2) is connected
    connected.bind.assert_called_once_with(("127.0.0.1", 0))
    connected.connect.assert_called_once_with(("127.0.0.1", 4403))
    connected.close.assert_not_called()


@pytest.mark.parametrize(
    "destination,sources",
    [("127.0.0.1", ["::1"]), ("::1", ["127.0.0.1"])],
)
def test_async_tcp_rejects_numeric_host_with_incompatible_source_family(
    monkeypatch: pytest.MonkeyPatch, destination: str, sources: list[str]
) -> None:
    """Fail a literal family mismatch before attempting an unrelated connection."""
    monkeypatch.setattr(selection, "_network_addresses", lambda _name: sources)
    connect = AsyncMock()
    monkeypatch.setattr(asyncio, "open_connection", connect)
    with pytest.raises(OSError, match="no address compatible"):
        asyncio.run(selection._open_tcp_connection(destination, 4403, "loop"))
    connect.assert_not_awaited()


def test_ble_adapter_survives_client_creation_and_repeated_scans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconnect clients and later discovery passes retain the same controller."""
    client_factory = MagicMock()
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.connection.BLEClient", client_factory
    )
    manager = ClientManager(
        MagicMock(), threading.RLock(), MagicMock(), MagicMock(), adapter="hci7"
    )
    for _ in range(2):
        manager._create_client("AA:BB:CC:DD:EE:FF", MagicMock())  # noqa: SLF001
    assert all(
        call.kwargs["bluez"] == {"adapter": "hci7"}
        for call in client_factory.call_args_list
    )

    class ScanClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def discover(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(kwargs)
            return {}

        def isConnected(self) -> bool:
            return True

        def close(self) -> None:
            pass

    scan_client = ScanClient()
    discovery = DiscoveryManager(
        client_factory=lambda **_kw: scan_client, adapter="hci7"
    )
    for _ in range(2):
        discovery.discover_devices("missing")
    assert len(scan_client.calls) == 2
    assert all(call["bluez"] == {"adapter": "hci7"} for call in scan_client.calls)
    discovery.close()


@pytest.mark.parametrize(
    "argv",
    [
        ["--list-interfaces", "--sendtext", "ignored"],
        ["--ble-adapter", "hci0", "--host", "radio"],
        ["--tcp-interface", "127.0.0.1", "--ble", "AA:BB:CC:DD:EE:FF"],
        ["--interface-proxy-bind-interface", "127.0.0.1"],
    ],
)
def test_cli_rejects_ignored_selectors_and_actions(argv: list[str]) -> None:
    """Refuse mixed or meaningless options before creating a transport."""
    parser = argparse.ArgumentParser(add_help=False)
    args = parse_cli_args(parser, version="test", argv=argv)
    hooks = MagicMock()
    with pytest.raises(SystemExit) as exc:
        bootstrap.run_common(args, parser, hooks, argv=["mtjk", *argv])
    assert exc.value.code == 2
    hooks.ble_interface.assert_not_called()
    hooks.tcp_interface.assert_not_called()
    hooks.serial_interface.assert_not_called()


def test_cli_lists_without_opening_transport(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Inventory is read-only and exits before normal radio startup."""
    monkeypatch.setattr(
        bootstrap,
        "_list_network_interfaces",
        lambda: [("lo", "loopback", ["127.0.0.1"])],
    )
    monkeypatch.setattr(
        bootstrap,
        "_list_ble_adapters",
        AsyncMock(return_value=[("hci0", "USB", "AA:BB:CC:DD:EE:FF", True)]),
    )
    parser = argparse.ArgumentParser(add_help=False)
    args = parse_cli_args(parser, version="test", argv=["--list-interfaces"])
    hooks = MagicMock()
    bootstrap.run_common(args, parser, hooks, argv=["mtjk", "--list-interfaces"])
    output = capsys.readouterr().out
    assert "127.0.0.1" in output and "hci0" in output
    hooks.ble_interface.assert_not_called()
    hooks.tcp_interface.assert_not_called()
    hooks.serial_interface.assert_not_called()
