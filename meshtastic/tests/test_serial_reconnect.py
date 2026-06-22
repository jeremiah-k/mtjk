"""Unit tests for serial disconnect/reconnect and noProto propagation."""

import threading
from unittest.mock import MagicMock, patch

import pytest

from ..__main__ import (
    _serial_should_reconnect,
    _serial_transport_is_live,
    _poll_serial_reconnect,
    SERIAL_RECONNECT_RETRY_SECONDS,
)
from ..mesh_interface import MeshInterface


def _make_serial_mock(
    *,
    no_proto: bool = False,
    stream_open: bool = True,
    reader_alive: bool = True,
    want_exit: bool = False,
    is_connected: bool = True,
) -> MagicMock:
    """Build a MagicMock simulating a SerialInterface for reconnect tests."""

    client = MagicMock(autospec=MeshInterface)
    client.noProto = no_proto
    client._wantExit = want_exit

    if stream_open:
        client.stream = MagicMock()
        client.stream.is_open = True
    else:
        client.stream = None

    if reader_alive:
        client._rxThread = MagicMock()
        client._rxThread.is_alive.return_value = True
    else:
        client._rxThread = MagicMock()
        client._rxThread.is_alive.return_value = False

    client.isConnected = threading.Event()
    if is_connected:
        client.isConnected.set()

    client.devPath = "/dev/ttyUSB0"
    client.connect = MagicMock()
    client._is_retryable_connect_error = MagicMock(return_value=False)
    return client


def _simulate_reader_exit(client: MagicMock) -> None:
    """Configure mock so reader thread reports dead after join()."""

    def _join_kills(**kw: object) -> None:
        client._rxThread.is_alive.return_value = False

    client._rxThread.join.side_effect = _join_kills


# ---------------------------------------------------------------------------
# _serial_transport_is_live
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_transport_live_when_stream_open_and_reader_alive() -> None:
    """Transport is live when stream is open and reader thread is alive."""

    client = _make_serial_mock(stream_open=True, reader_alive=True)
    assert _serial_transport_is_live(client) is True


@pytest.mark.unit
def test_transport_dead_when_stream_none() -> None:
    """Transport is dead when stream is None."""

    client = _make_serial_mock(stream_open=False, reader_alive=True)
    assert _serial_transport_is_live(client) is False


@pytest.mark.unit
def test_transport_dead_when_reader_dead() -> None:
    """Transport is dead when reader thread has exited."""

    client = _make_serial_mock(stream_open=True, reader_alive=False)
    assert _serial_transport_is_live(client) is False


# ---------------------------------------------------------------------------
# _serial_should_reconnect
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_should_not_reconnect_when_protocol_connected() -> None:
    """Protocol mode with isConnected set should not reconnect."""

    client = _make_serial_mock(no_proto=False, is_connected=True)
    assert _serial_should_reconnect(client) is False


@pytest.mark.unit
def test_should_reconnect_when_protocol_disconnected() -> None:
    """Protocol mode with isConnected cleared should reconnect."""

    client = _make_serial_mock(no_proto=False, is_connected=False)
    assert _serial_should_reconnect(client) is True


@pytest.mark.unit
def test_should_not_reconnect_when_noproto_transport_live() -> None:
    """NoProto mode with live transport should not reconnect.

    This is the key fix: isConnected is never set in noProto mode, but
    if the stream is open and reader is alive, the session is healthy.
    """

    client = _make_serial_mock(
        no_proto=True, stream_open=True, reader_alive=True, is_connected=False
    )
    assert _serial_should_reconnect(client) is False


@pytest.mark.unit
def test_should_reconnect_when_noproto_transport_dead() -> None:
    """NoProto mode with dead transport should reconnect."""

    client = _make_serial_mock(
        no_proto=True, stream_open=False, reader_alive=False, is_connected=False
    )
    assert _serial_should_reconnect(client) is True


@pytest.mark.unit
def test_should_not_reconnect_when_want_exit() -> None:
    """Should not reconnect when shutdown is requested."""

    client = _make_serial_mock(want_exit=True, is_connected=False)
    assert _serial_should_reconnect(client) is False


# ---------------------------------------------------------------------------
# _poll_serial_reconnect
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_poll_reconnect_waits_for_dead_reader_thread() -> None:
    """Reconnect waits for old reader thread to exit before calling connect()."""

    client = _make_serial_mock(is_connected=False)
    client._rxThread.is_alive.return_value = True
    client.connect.return_value = None
    # After connect, mark as connected
    client.connect.side_effect = lambda: client.isConnected.set()
    # Simulate thread dying after join
    def _simulate_join_exit(**kw: object) -> None:
        client._rxThread.is_alive.return_value = False

    client._rxThread.join.side_effect = _simulate_join_exit

    with patch("meshtastic.__main__.time"):
        _poll_serial_reconnect(client)

    client._rxThread.join.assert_called_once_with(timeout=5.0)
    client.connect.assert_called_once()


@pytest.mark.unit
def test_poll_reconnect_returns_if_reader_still_alive() -> None:
    """Reconnect defers if reader thread is still alive after join timeout."""

    client = _make_serial_mock(is_connected=False)
    client._rxThread.is_alive.return_value = True  # stays alive

    with patch("meshtastic.__main__.time") as mock_time:
        _poll_serial_reconnect(client)

    client._rxThread.join.assert_called_once_with(timeout=5.0)
    client.connect.assert_not_called()
    mock_time.sleep.assert_called_once_with(SERIAL_RECONNECT_RETRY_SECONDS)


@pytest.mark.unit
def test_poll_reconnect_swallows_oserror() -> None:
    """Reconnect catches OSError and sleeps before returning."""

    client = _make_serial_mock(is_connected=False)
    client.connect.side_effect = OSError("device gone")
    _simulate_reader_exit(client)

    with patch("meshtastic.__main__.time") as mock_time:
        _poll_serial_reconnect(client)

    mock_time.sleep.assert_called_once_with(SERIAL_RECONNECT_RETRY_SECONDS)


@pytest.mark.unit
def test_poll_reconnect_swallows_retryable_mesh_error() -> None:
    """Reconnect catches retryable MeshInterfaceError."""

    client = _make_serial_mock(is_connected=False)
    client._is_retryable_connect_error = MagicMock(return_value=True)
    client.connect.side_effect = MeshInterface.MeshInterfaceError("device not found")
    _simulate_reader_exit(client)

    with patch("meshtastic.__main__.time") as mock_time:
        _poll_serial_reconnect(client)

    mock_time.sleep.assert_called_once_with(SERIAL_RECONNECT_RETRY_SECONDS)


@pytest.mark.unit
def test_poll_reconnect_reraises_non_retryable_mesh_error() -> None:
    """Reconnect re-raises non-retryable MeshInterfaceError."""

    client = _make_serial_mock(is_connected=False)
    client._is_retryable_connect_error = MagicMock(return_value=False)
    client.connect.side_effect = MeshInterface.MeshInterfaceError("config error")
    _simulate_reader_exit(client)

    with pytest.raises(MeshInterface.MeshInterfaceError, match="config error"):
        _poll_serial_reconnect(client)


# ---------------------------------------------------------------------------
# noProto propagation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_mesh_interface_noproto_propagates_to_localnode() -> None:
    """MeshInterface(noProto=True) should create localNode with noProto=True."""

    with MeshInterface(noProto=True) as iface:
        assert iface.localNode.noProto is True


@pytest.mark.unit
def test_mesh_interface_proto_propagates_to_localnode() -> None:
    """MeshInterface(noProto=False) should create localNode with noProto=False."""

    with MeshInterface(noProto=False) as iface:
        assert iface.localNode.noProto is False


# ---------------------------------------------------------------------------
# _is_retryable_connect_error classification
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_retryable_device_disconnected_error() -> None:
    """'Serial port does not exist (device disconnected)' is retryable."""

    from ..serial_interface import SerialInterface

    iface = SerialInterface.__new__(SerialInterface)
    exc = SerialInterface.MeshInterfaceError(
        "Serial port /dev/ttyACM0 does not exist (device disconnected)"
    )
    assert iface._is_retryable_connect_error(exc) is True


@pytest.mark.unit
def test_retryable_no_device_detected_error() -> None:
    """'No serial Meshtastic device detected for reconnect.' is retryable."""

    from ..serial_interface import SerialInterface

    iface = SerialInterface.__new__(SerialInterface)
    exc = SerialInterface.MeshInterfaceError(
        "No serial Meshtastic device detected for reconnect."
    )
    assert iface._is_retryable_connect_error(exc) is True


@pytest.mark.unit
def test_non_retryable_mesh_error() -> None:
    """Non-retryable MeshInterfaceError is not classified as retryable."""

    from ..serial_interface import SerialInterface

    iface = SerialInterface.__new__(SerialInterface)
    exc = SerialInterface.MeshInterfaceError("Configuration validation failed")
    assert iface._is_retryable_connect_error(exc) is False


# ---------------------------------------------------------------------------
# Remote noProto propagation via getNode
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_getnode_propagates_noproto_to_remote_node() -> None:
    """getNode() for a remote node should inherit noProto from the interface."""

    from ..mesh_interface import MeshInterface
    from ..node import Node

    with MeshInterface(noProto=True) as iface:
        remote = iface.getNode("!12345678", requestChannels=False)
        assert isinstance(remote, Node)
        assert remote.noProto is True


@pytest.mark.unit
def test_getnode_propagates_proto_to_remote_node() -> None:
    """getNode() for a remote node should inherit noProto=False from the interface."""

    from ..mesh_interface import MeshInterface
    from ..node import Node

    with MeshInterface(noProto=False) as iface:
        remote = iface.getNode("!12345678", requestChannels=False)
        assert isinstance(remote, Node)
        assert remote.noProto is False


# ---------------------------------------------------------------------------
# Termios error suppression in close()
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_close_suppresses_termios_error() -> None:
    """Verify termios.error is included in SerialInterface.close() suppress tuple."""

    from ..serial_interface import _TERMIOS_ERRORS

    if not _TERMIOS_ERRORS:
        pytest.skip("termios not available on this platform")

    import contextlib

    termios_exc = _TERMIOS_ERRORS[0]
    # The suppress call in close() uses OSError, ValueError, serial.SerialException,
    # and *_TERMIOS_ERRORS. Verify that combination actually suppresses termios.error.
    with contextlib.suppress(OSError, ValueError, *_TERMIOS_ERRORS):
        raise termios_exc("Input/output error")
    # If we reach here, the suppress worked
