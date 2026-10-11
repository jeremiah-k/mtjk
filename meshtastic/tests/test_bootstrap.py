"""Focused CLI bootstrap coverage for OTA node-loading policy."""

import argparse
import contextlib
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import meshtastic.__main__ as main_module
from meshtastic.ble_interface import BLEInterface
from meshtastic.cli import bootstrap
from meshtastic.cli.session_resources import CliSessionResources


def _make_args(**overrides: Any) -> argparse.Namespace:
    """Build a preconnect argument namespace with OTA-relevant defaults."""
    defaults: dict[str, Any] = {
        "quiet": False,
        "debug": False,
        "listen": False,
        "debuglib": False,
        "contact_verified": False,
        "contact_ignore": False,
        "contact_qr": None,
        "configure": None,
        "set_owner": None,
        "set_owner_short": None,
        "set_ham": None,
        "ota_update": None,
        "reboot_ota": False,
        "no_nodes": False,
        "ch_index": None,
        "dest": None,
        "seriallog": None,
        "noproto": False,
        "port": None,
        "ble": None,
        "ble_auto_reconnect": False,
        "host": None,
        "timeout": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _session() -> CliSessionResources:
    """Build an invocation session over a fresh exit stack."""
    return CliSessionResources(ExitStack())


def _recording_factory(
    interface_obj: Any, calls: list[tuple[tuple[object, ...], dict[str, Any]]]
) -> Callable[..., Any]:
    """Build a transport factory that records arguments and yields a double."""

    @contextlib.contextmanager
    def factory(*args: Any, **kwargs: Any) -> Iterator[Any]:
        calls.append((args, kwargs))
        yield interface_obj

    return factory


class _FakeBLETransport:
    """Callable BLE seam mirroring the BLEInterface class surface."""

    BLEError = BLEInterface.BLEError

    def __init__(self, interface_obj: Any) -> None:
        self._interface_obj = interface_obj
        self.calls: list[tuple[tuple[object, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        @contextlib.contextmanager
        def entered() -> Iterator[Any]:
            self.calls.append((args, kwargs))
            yield self._interface_obj

        return entered()


@pytest.mark.unit
def test_ota_preflight_forces_no_nodes_before_transport_open(tmp_path: Path) -> None:
    """Verify OTA preflight disables NodeDB loading before transport open.

    Parameters
    ----------
    tmp_path : Path
        Temporary directory used to create a firmware image accepted by preflight.
    """
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"firmware")
    args = _make_args(ota_update=str(firmware))
    hooks = MagicMock(autospec=bootstrap.BootstrapHooks)

    bootstrap._validate_and_normalize_args(args, MagicMock(), hooks)

    assert args.no_nodes is True


@pytest.mark.unit
def test_reboot_ota_preflight_forces_no_nodes() -> None:
    """Verify rebootOTA preflight disables NodeDB loading like the OTA path."""
    args = _make_args(reboot_ota=True)
    hooks = MagicMock(autospec=bootstrap.BootstrapHooks)

    bootstrap._validate_and_normalize_args(args, MagicMock(), hooks)

    assert args.no_nodes is True


@pytest.mark.unit
def test_plain_invocation_keeps_node_loading_enabled() -> None:
    """Verify preflight leaves NodeDB loading on when no OTA action is requested."""
    args = _make_args()
    hooks = MagicMock(autospec=bootstrap.BootstrapHooks)

    bootstrap._validate_and_normalize_args(args, MagicMock(), hooks)

    assert args.no_nodes is False


@pytest.mark.unit
def test_open_serial_transport_returns_serial_client_when_devpath_present() -> None:
    """A serial client with a resolved device path is returned without fallback."""
    serial_client = MagicMock(devPath="/dev/ttyUSB0")
    tcp_calls: list[tuple[tuple[object, ...], dict[str, Any]]] = []
    hooks = replace(
        main_module._build_bootstrap_hooks(),
        serial_interface=_recording_factory(serial_client, []),
        tcp_interface=_recording_factory(MagicMock(), tcp_calls),
    )

    result = bootstrap._open_serial_transport(
        _make_args(port="/dev/ttyUSB0"), hooks, _session(), None
    )

    assert result is serial_client
    assert tcp_calls == []


@pytest.mark.unit
def test_open_serial_transport_falls_back_to_localhost_tcp_without_devpath() -> None:
    """A serial client lacking devPath falls back to the localhost TCP listener."""
    serial_client = MagicMock(spec=["close"])  # deliberately without devPath
    tcp_client = MagicMock()
    tcp_calls: list[tuple[tuple[object, ...], dict[str, Any]]] = []
    hooks = replace(
        main_module._build_bootstrap_hooks(),
        serial_interface=_recording_factory(serial_client, []),
        tcp_interface=_recording_factory(tcp_client, tcp_calls),
    )

    result = bootstrap._open_serial_transport(
        _make_args(port="/dev/null"), hooks, _session(), None
    )

    assert result is tcp_client
    assert tcp_calls, "fallback must open a TCP interface"
    tcp_args, tcp_kwargs = tcp_calls[0]
    assert tcp_args[0] == "localhost"
    assert tcp_kwargs["timeout"] is None


@pytest.mark.unit
def test_open_transport_ble_selection_returns_ble_client() -> None:
    """BLE selection enters the BLE seam and returns its client verbatim."""
    ble_client = MagicMock()
    ble_transport = _FakeBLETransport(ble_client)
    hooks = replace(
        main_module._build_bootstrap_hooks(),
        ble_interface=ble_transport,
    )

    result = bootstrap._open_transport(
        _make_args(ble="AA:BB:CC:DD:EE:FF"), hooks, _session(), None
    )

    assert result is ble_client
    assert ble_transport.calls, "BLE seam must be entered"
    ble_args, ble_kwargs = ble_transport.calls[0]
    assert ble_args[0] == "AA:BB:CC:DD:EE:FF"
    assert ble_kwargs["auto_reconnect"] is False
