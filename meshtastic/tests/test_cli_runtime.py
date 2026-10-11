"""Focused tests for CLI serial reconnect helpers."""

import time
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest

import meshtastic.__main__ as main_module
import meshtastic.cli.runtime as runtime
from meshtastic.mesh_interface import MeshInterface


def _mesh_client(**attributes: object) -> MeshInterface:
    """Build a minimal duck-typed client for private runtime helpers."""
    return cast(MeshInterface, SimpleNamespace(**attributes))


@pytest.mark.unit
def test_main_runtime_compatibility_exports_reference_runtime_module() -> None:
    """Legacy ``__main__`` imports remain aliases, not duplicate runtime state."""
    assert (
        main_module.MAIN_LOOP_IDLE_SLEEP_SECONDS == runtime.MAIN_LOOP_IDLE_SLEEP_SECONDS
    )
    assert (
        main_module.SERIAL_RECONNECT_RETRY_SECONDS
        == runtime.SERIAL_RECONNECT_RETRY_SECONDS
    )
    assert (
        main_module.SERIAL_LISTEN_CONNECTED_SLEEP_SECONDS
        == runtime.SERIAL_LISTEN_CONNECTED_SLEEP_SECONDS
    )
    assert (
        main_module.SERIAL_RX_THREAD_JOIN_TIMEOUT_SECONDS
        == runtime.SERIAL_RX_THREAD_JOIN_TIMEOUT_SECONDS
    )
    assert (
        main_module._is_serial_reconnect_client is runtime._is_serial_reconnect_client
    )
    assert main_module._serial_transport_is_live is runtime._serial_transport_is_live
    assert main_module._serial_should_reconnect is runtime._serial_should_reconnect
    assert main_module._poll_serial_reconnect is runtime._poll_serial_reconnect
    assert main_module._listen_loop_poll_once is runtime._listen_loop_poll_once


@pytest.mark.unit
def test_serial_transport_live_requires_open_stream_and_reader() -> None:
    reader = MagicMock()
    reader.is_alive.return_value = True
    client = _mesh_client(
        stream=SimpleNamespace(is_open=True),
        _rxThread=reader,
    )

    assert runtime._serial_transport_is_live(client)

    client = _mesh_client(
        stream=SimpleNamespace(is_open=False),
        _rxThread=reader,
    )
    assert not runtime._serial_transport_is_live(client)

    reader.is_alive.return_value = False
    client = _mesh_client(
        stream=SimpleNamespace(is_open=True),
        _rxThread=reader,
    )
    assert not runtime._serial_transport_is_live(client)


@pytest.mark.unit
def test_serial_should_reconnect_requires_lost_connection() -> None:
    connected = MagicMock()
    client = _mesh_client(
        noProto=False,
        _wantExit=False,
        isConnected=connected,
    )

    connected.is_set.return_value = False
    assert runtime._serial_should_reconnect(client)

    connected.is_set.return_value = True
    assert not runtime._serial_should_reconnect(client)


@pytest.mark.unit
def test_serial_should_reconnect_noproto_uses_transport_liveness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport_live = MagicMock(return_value=False)
    monkeypatch.setattr(runtime, "_serial_transport_is_live", transport_live)
    client = _mesh_client(noProto=True, _wantExit=False)

    assert runtime._serial_should_reconnect(client)

    transport_live.return_value = True
    assert not runtime._serial_should_reconnect(client)
    assert transport_live.call_count == 2


@pytest.mark.unit
def test_serial_should_not_reconnect_when_exit_requested() -> None:
    connected = MagicMock()
    connected.is_set.return_value = False
    client = _mesh_client(
        noProto=False,
        _wantExit=True,
        isConnected=connected,
    )

    assert not runtime._serial_should_reconnect(client)
    connected.is_set.assert_not_called()


@pytest.mark.unit
def test_listen_loop_poll_once_sleeps_long_when_non_serial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _mesh_client()
    sleep = MagicMock()
    monkeypatch.setattr(time, "sleep", sleep)

    assert runtime._listen_loop_poll_once(client) is False
    sleep.assert_called_once_with(runtime.MAIN_LOOP_IDLE_SLEEP_SECONDS)


@pytest.mark.unit
def test_listen_loop_poll_once_reconnects_serial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _mesh_client()
    monkeypatch.setattr(runtime, "_is_serial_reconnect_client", lambda _client: True)
    monkeypatch.setattr(runtime, "_serial_should_reconnect", lambda _client: True)
    poll = MagicMock()
    monkeypatch.setattr(runtime, "_poll_serial_reconnect", poll)

    assert runtime._listen_loop_poll_once(client)
    poll.assert_called_once_with(client)
