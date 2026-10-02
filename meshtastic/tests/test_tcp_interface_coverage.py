"""Extended test coverage for tcp_interface.py edge cases.

This module provides comprehensive coverage for TCP interface edge cases
including connection timeouts, network errors, socket closure scenarios,
and partial read/write handling.
"""

import socket
import threading
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from meshtastic.tcp_interface import TCPInterface, _is_transport_fd_state_value_error

# ============================================================================
# Connection Timeout and Refused Handling Tests
# ============================================================================


@pytest.mark.unit
def test_TCPInterface_init_exception_cleanup_closes_socket() -> None:
    """Test that socket is closed when base class init raises exception.

    Covers lines 150-158: Exception handling cleanup in __init__.
    """
    with patch("meshtastic.tcp_interface.socket.create_connection") as mock_connect:
        mock_socket = MagicMock()
        mock_connect.return_value = mock_socket

        with patch(
            "meshtastic.tcp_interface.StreamInterface.__init__"
        ) as mock_super_init:
            mock_super_init.side_effect = RuntimeError("Init failed")

            with pytest.raises(RuntimeError, match="Init failed"):
                TCPInterface(
                    hostname="localhost",
                    noProto=True,
                    connectNow=True,
                )

            # Socket should be closed during cleanup
            mock_socket.close.assert_called_once()


@pytest.mark.unit
def test_TCPInterface_repr_full() -> None:
    """Test __repr__ method with all flags set (lines 170-184)."""
    with patch("socket.socket"):
        iface = TCPInterface(
            hostname="test.meshtastic.local",
            debugOut=print,
            noProto=True,
            connectNow=False,
            portNumber=1234,
            noNodes=True,
        )
        try:
            rep = repr(iface)
            assert "TCPInterface('test.meshtastic.local'" in rep
            assert "debugOut=" in rep
            assert "noProto=True" in rep
            assert "connectNow=False" in rep
            assert "socket=None" in rep
            assert "portNumber=1234" in rep
            assert "noNodes=True" in rep
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_repr_default_port() -> None:
    """Test __repr__ excludes port when default (lines 179-180)."""
    with patch("socket.socket"):
        iface = TCPInterface(
            hostname="localhost",
            noProto=True,
            connectNow=False,
            portNumber=4403,  # DEFAULT_TCP_PORT
        )
        try:
            rep = repr(iface)
            # Port should not appear when it's the default
            assert "portNumber" not in rep
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_repr_connected() -> None:
    """Test __repr__ with connected socket (lines 177-178 not triggered)."""
    with patch("meshtastic.tcp_interface.socket.create_connection") as mock_connect:
        mock_socket = MagicMock()
        mock_connect.return_value = mock_socket
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=True)
        try:
            rep = repr(iface)
            # Should not show socket=None when connected
            assert "socket=None" not in rep
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_repr_no_special_flags() -> None:
    """Test __repr__ with minimal flags set (only connectNow is non-default)."""
    with patch("socket.socket"):
        iface = TCPInterface(
            hostname="localhost",
            noProto=False,
            connectNow=False,  # Non-default value, should appear in repr
            portNumber=4403,  # Default value, should NOT appear
            noNodes=False,
        )
        try:
            rep = repr(iface)
            # Should not show flags that are False/default
            assert "noProto=True" not in rep
            # connectNow=False is non-default so it SHOULD appear
            assert "connectNow=False" in rep
            assert "noNodes=True" not in rep
            assert "debugOut" not in rep
            # Default port should not appear
            assert "portNumber" not in rep
        finally:
            iface.close()


# ============================================================================
# Network Error Recovery Tests
# ============================================================================


@pytest.mark.unit
def test_TCPInterface_myConnect_connection_refused() -> None:
    """Test myConnect handles connection refused errors."""
    with patch("socket.socket"):
        with patch(
            "meshtastic.tcp_interface.socket.create_connection",
            side_effect=ConnectionRefusedError("Connection refused"),
        ):
            iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
            try:
                with pytest.raises(ConnectionRefusedError, match="Connection refused"):
                    iface.myConnect()
            finally:
                iface.close()


@pytest.mark.unit
def test_TCPInterface_myConnect_timeout_error() -> None:
    """Test myConnect handles socket timeout during connection."""
    with patch("socket.socket"):
        with patch(
            "meshtastic.tcp_interface.socket.create_connection",
            side_effect=socket.timeout("Connection timed out"),
        ):
            iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
            try:
                with pytest.raises(socket.timeout, match="Connection timed out"):
                    iface.myConnect()
            finally:
                iface.close()


@pytest.mark.unit
def test_TCPInterface_myConnect_os_error() -> None:
    """Test myConnect handles generic OSError (e.g., network unreachable)."""
    with patch("socket.socket"):
        with patch(
            "meshtastic.tcp_interface.socket.create_connection",
            side_effect=OSError(51, "Network is unreachable"),
        ):
            iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
            try:
                with pytest.raises(OSError, match="Network is unreachable"):
                    iface.myConnect()
            finally:
                iface.close()


@pytest.mark.unit
def test_TCPInterface_myConnect_host_resolution_failure() -> None:
    """Test myConnect handles DNS resolution failures."""
    with patch("socket.socket"):
        with patch(
            "meshtastic.tcp_interface.socket.create_connection",
            side_effect=socket.gaierror(8, "nodename nor servname provided"),
        ):
            iface = TCPInterface(
                hostname="invalid-hostname.xyz", noProto=True, connectNow=False
            )
            try:
                with pytest.raises(
                    socket.gaierror, match="nodename nor servname provided"
                ):
                    iface.myConnect()
            finally:
                iface.close()


@pytest.mark.unit
def test_TCPInterface_myConnect_settimeout_failure_cleanup(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test myConnect closes socket when settimeout fails (lines 276-278)."""
    _ = caplog  # Unused but required for potential future log assertions
    with patch("socket.socket"):
        with patch("meshtastic.tcp_interface.socket.create_connection") as mock_connect:
            mock_socket = MagicMock()
            mock_socket.settimeout.side_effect = OSError("Cannot set timeout")
            mock_connect.return_value = mock_socket

            iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
            try:
                with pytest.raises(OSError, match="Cannot set timeout"):
                    iface.myConnect()

                # Socket should be closed during cleanup
                mock_socket.close.assert_called_once()
            finally:
                iface.close()


@pytest.mark.unit
def test_TCPInterface_myConnect_shutting_down_during_connection() -> None:
    """Test myConnect when interface is shutting down during connection."""
    with patch("socket.socket"):
        with patch("meshtastic.tcp_interface.socket.create_connection") as mock_connect:
            mock_socket = MagicMock()
            mock_connect.return_value = mock_socket

            iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
            try:
                # Set shutting down flag
                iface._wantExit = True

                with pytest.raises(ConnectionError, match="interface is shutting down"):
                    iface.myConnect()

                # Socket should be closed because we're shutting down
                mock_socket.close.assert_called_once()
            finally:
                iface.close()


@pytest.mark.unit
def test_TCPInterface_myConnect_redundant_socket_discarded() -> None:
    """Test myConnect discards redundant socket when one already exists."""
    with patch("socket.socket"):
        with patch("meshtastic.tcp_interface.socket.create_connection") as mock_connect:
            existing_socket = MagicMock()
            new_socket = MagicMock()
            mock_connect.return_value = new_socket

            iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
            try:
                # Set an existing socket
                iface.socket = existing_socket

                iface.myConnect()

                # New socket should be closed as redundant
                new_socket.close.assert_called_once()
                # Original socket should remain
                assert iface.socket is existing_socket
            finally:
                iface.close()


# ============================================================================
# Socket Closure Edge Cases
# ============================================================================


@pytest.mark.unit
def test_TCPInterface_close_socket_if_current_none() -> None:
    """Test _close_socket_if_current with None socket (line 252-253)."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            result = iface._close_socket_if_current(None)
            assert result is False
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_close_socket_if_current_mismatch() -> None:
    """Test _close_socket_if_current when socket doesn't match (lines 255-259)."""
    with patch("socket.socket"):
        with patch("meshtastic.tcp_interface.socket.create_connection") as mock_connect:
            current_socket = MagicMock()
            mock_connect.return_value = current_socket

            iface = TCPInterface(hostname="localhost", noProto=True, connectNow=True)
            try:
                # Create a different socket handle
                other_socket = MagicMock()

                result = iface._close_socket_if_current(other_socket)

                # Should return False because socket changed
                assert result is False
                # Other socket should still be closed
                other_socket.close.assert_called_once()
                # Current socket should remain
                assert iface.socket is current_socket
            finally:
                iface.close()


@pytest.mark.unit
def test_TCPInterface_close_socket_if_current_match() -> None:
    """Test _close_socket_if_current when socket matches."""
    with patch("socket.socket"):
        with patch("meshtastic.tcp_interface.socket.create_connection") as mock_connect:
            current_socket = MagicMock()
            mock_connect.return_value = current_socket

            iface = TCPInterface(hostname="localhost", noProto=True, connectNow=True)
            try:
                result = iface._close_socket_if_current(current_socket)

                # Should return True and clear socket
                assert result is True
                assert iface.socket is None
                current_socket.close.assert_called_once()
            finally:
                iface.close()


@pytest.mark.unit
def test_TCPInterface_socket_shutdown_with_explicit_socket() -> None:
    """Test _socket_shutdown with explicit socket parameter."""
    with patch("socket.socket"):
        with patch("meshtastic.tcp_interface.socket.create_connection") as mock_connect:
            mock_socket = MagicMock()
            mock_connect.return_value = mock_socket

            iface = TCPInterface(hostname="localhost", noProto=True, connectNow=True)
            try:
                other_socket = MagicMock()
                iface._socket_shutdown(other_socket)

                # Should shutdown the provided socket
                other_socket.shutdown.assert_called_once_with(socket.SHUT_RDWR)
            finally:
                iface.close()


@pytest.mark.unit
def test_TCPInterface_socket_shutdown_suppresses_errors() -> None:
    """Test _socket_shutdown suppresses OSError exceptions."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            mock_socket = MagicMock()
            mock_socket.shutdown.side_effect = OSError("Shutdown failed")
            iface.socket = mock_socket

            # Should not raise
            iface._socket_shutdown()
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_close_socket_handle_none() -> None:
    """Test _close_socket_handle with None socket."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            # Should not raise with None
            iface._close_socket_handle(None)
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_close_socket_handle_exception_in_close() -> None:
    """Test _close_socket_handle suppresses exceptions in close()."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            mock_socket = MagicMock()
            mock_socket.shutdown.side_effect = OSError("Shutdown failed")
            mock_socket.close.side_effect = OSError("Close failed")

            # Should not raise despite both failing
            iface._close_socket_handle(mock_socket)
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_close_socket_handle_exception_in_shutdown() -> None:
    """Test _close_socket_handle when shutdown raises but close succeeds."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            mock_socket = MagicMock()
            mock_socket.shutdown.side_effect = ValueError("Shutdown error")

            # Should not raise
            iface._close_socket_handle(mock_socket)

            # Close should still be called
            mock_socket.close.assert_called_once()
        finally:
            iface.close()


# ============================================================================
# Connection Management Edge Cases
# ============================================================================


@pytest.mark.unit
def test_TCPInterface_connect_socket_if_needed_no_reconnect() -> None:
    """Test _connect_socket_if_needed when reconnect is not in progress (line 237)."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            iface.socket = MagicMock()

            with patch.object(iface, "myConnect") as mock_connect:
                iface._connect_socket_if_needed()
                # Should return early when socket exists
                mock_connect.assert_not_called()
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_connect_socket_if_needed_concurrent_reconnect() -> None:
    """Test _connect_socket_if_needed when another thread is reconnecting."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            iface.socket = None

            # Set reconnect in progress
            with iface._reconnect_lock:
                iface._reconnect_attempt_in_progress = True

            sleep_count = [0]

            def side_effect_sleep(_delay: float) -> None:
                sleep_count[0] += 1
                if sleep_count[0] >= 2:
                    # Clear reconnect flag after a couple sleeps
                    with iface._reconnect_lock:
                        iface._reconnect_attempt_in_progress = False

            with (
                patch(
                    "meshtastic.tcp_interface.time.sleep",
                    side_effect=side_effect_sleep,
                ),
                patch.object(iface, "myConnect") as mock_connect,
            ):
                iface._connect_socket_if_needed()
                # Should have slept and then connected
                assert sleep_count[0] >= 1
                mock_connect.assert_called_once()
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_raise_if_connect_blocked_locked_shutdown() -> None:
    """Test _raise_if_connect_blocked_locked raises when shutting down."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            iface._wantExit = True

            with pytest.raises(ConnectionError, match="interface is shutting down"):
                with iface._reconnect_lock:
                    iface._raise_if_connect_blocked_locked()
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_raise_if_connect_blocked_locked_fatal() -> None:
    """Test _raise_if_connect_blocked_locked raises on fatal disconnect."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            iface._fatal_disconnect = True

            with pytest.raises(
                ConnectionError, match="reconnect disabled after fatal disconnect"
            ):
                with iface._reconnect_lock:
                    iface._raise_if_connect_blocked_locked()
        finally:
            iface.close()


# ============================================================================
# Partial Read/Write Handling Tests
# ============================================================================


@pytest.mark.unit
def test_TCPInterface_write_bytes_multiple_partial_sends() -> None:
    """Test _write_bytes with many partial sends requiring multiple iterations."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            mock_socket = MagicMock()
            # Send 1 byte at a time for 10-byte payload
            mock_socket.send.side_effect = [1] * 10
            iface.socket = mock_socket

            with patch(
                "meshtastic.tcp_interface.select.select",
                return_value=([], [mock_socket], []),
            ) as mock_select:
                payload = b"0123456789"
                iface._write_bytes(payload)

                # Should have called select and send 10 times
                assert mock_select.call_count == 10
                assert mock_socket.send.call_count == 10

                # Verify each send got progressively smaller slice
                for i, call in enumerate(mock_socket.send.call_args_list):
                    expected_remaining = payload[i:]
                    assert call.args[0] == expected_remaining
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_read_bytes_socket_changed_during_reconnect() -> None:
    """Test _read_bytes when socket changes during read cleanup."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            original_socket = MagicMock()
            original_socket.recv.return_value = b""
            iface.socket = original_socket

            # Mock _close_socket_if_current to return False (socket changed)
            with patch.object(
                iface, "_close_socket_if_current", return_value=False
            ) as mock_close:
                with patch.object(iface, "_attempt_reconnect") as mock_reconnect:
                    data = iface._read_bytes(1)

                    assert data == b""
                    # Should not attempt reconnect when socket changed
                    mock_reconnect.assert_not_called()
                    mock_close.assert_called_once_with(original_socket)
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_read_bytes_resets_reconnect_attempts() -> None:
    """Test _read_bytes resets reconnect counters after successful read."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            mock_socket = MagicMock()
            mock_socket.recv.return_value = b"test data"
            iface.socket = mock_socket
            iface._reconnect_attempts = 5
            iface._fatal_disconnect = True  # Reset this too

            data = iface._read_bytes(10)

            assert data == b"test data"
            assert iface._reconnect_attempts == 0
            assert iface._fatal_disconnect is False
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_read_bytes_no_socket_attempts_reconnect() -> None:
    """Test _read_bytes attempts reconnect when socket is None."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            iface.socket = None
            iface._wantExit = False
            iface._fatal_disconnect = False

            with patch.object(iface, "_attempt_reconnect") as mock_reconnect:
                data = iface._read_bytes(10)

                assert data == b""
                mock_reconnect.assert_called_once()
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_read_bytes_no_socket_with_shutdown() -> None:
    """Test _read_bytes skips reconnect when socket is None and shutdown requested."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            iface.socket = None
            iface._wantExit = True

            with patch.object(iface, "_attempt_reconnect") as mock_reconnect:
                data = iface._read_bytes(10)

                assert data == b""
                mock_reconnect.assert_not_called()
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_read_bytes_data_available() -> None:
    """Test _read_bytes returns data when available."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            mock_socket = MagicMock()
            mock_socket.recv.return_value = b"hello"
            iface.socket = mock_socket

            data = iface._read_bytes(10)

            assert data == b"hello"
            mock_socket.recv.assert_called_once_with(10)
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_read_bytes_empty_data_triggers_reconnect() -> None:
    """Test _read_bytes treats empty data as disconnect and reconnects."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            mock_socket = MagicMock()
            mock_socket.recv.return_value = b""
            iface.socket = mock_socket

            with (
                patch.object(
                    iface, "_close_socket_if_current", return_value=True
                ) as mock_close,
                patch.object(iface, "_attempt_reconnect") as mock_reconnect,
            ):
                data = iface._read_bytes(10)

                assert data == b""
                mock_close.assert_called_once_with(mock_socket)
                mock_reconnect.assert_called_once()
        finally:
            iface.close()


# ============================================================================
# Helper Function Tests
# ============================================================================


@pytest.mark.unit
@pytest.mark.parametrize(
    "message,expected",
    [
        ("fd is None", True),
        ("file descriptor cannot be a negative integer", True),
        ("invalid file descriptor", True),
        ("negative file descriptor", True),
        ("bad file descriptor", True),
        ("closed file", True),
        ("some other value error", False),
        ("", False),
    ],
)
def test_is_transport_fd_state_value_error(message: str, expected: bool) -> None:
    """Test _is_transport_fd_state_value_error function."""
    result = _is_transport_fd_state_value_error(ValueError(message))
    assert result is expected


@pytest.mark.unit
def test_is_transport_fd_state_value_error_case_insensitive() -> None:
    """Test _is_transport_fd_state_value_error is case insensitive."""
    result = _is_transport_fd_state_value_error(ValueError("FD IS NONE"))
    assert result is True


# ============================================================================
# Reconnect Failure Handling Tests
# ============================================================================


@pytest.mark.unit
def test_TCPInterface_attempt_reconnect_os_error_on_connect() -> None:
    """Test _attempt_reconnect handles OSError from myConnect."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            with patch.object(
                iface, "myConnect", side_effect=OSError("Network unreachable")
            ) as mock_connect:
                result = iface._attempt_reconnect()

                assert result is False
                mock_connect.assert_called_once()
                # Should still have incremented attempt counter
                assert iface._reconnect_attempts >= 1
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_attempt_reconnect_connection_error_on_connect() -> None:
    """Test _attempt_reconnect handles ConnectionError from myConnect."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            with patch.object(
                iface,
                "myConnect",
                side_effect=ConnectionError("interface is shutting down"),
            ) as mock_connect:
                result = iface._attempt_reconnect()

                assert result is False
                mock_connect.assert_called_once()
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_attempt_reconnect_no_socket_after_connect() -> None:
    """Test _attempt_reconnect when socket not set after myConnect."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            with (
                patch.object(iface, "myConnect") as mock_connect,
                patch("meshtastic.tcp_interface.logger.warning") as mock_log,
            ):
                # myConnect succeeds but doesn't set socket
                mock_connect.return_value = None
                iface.socket = None

                result = iface._attempt_reconnect()

                assert result is False
                mock_log.assert_called_once()
                assert "returned without setting socket" in str(mock_log.call_args)
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_attempt_reconnect_wantexit_during_reconnect() -> None:
    """Test _attempt_reconnect handles _wantExit becoming True during reconnect."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            mock_socket = MagicMock()

            def connect_side_effect() -> None:
                iface.socket = mock_socket
                # Simulate close() being called during reconnect
                iface._wantExit = True

            with (
                patch.object(iface, "myConnect", side_effect=connect_side_effect),
                patch.object(iface, "_close_socket_if_current") as mock_close,
            ):
                result = iface._attempt_reconnect()

                assert result is False
                # Should close the newly connected socket
                mock_close.assert_called_once_with(mock_socket)
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_attempt_reconnect_config_exception() -> None:
    """Test _attempt_reconnect handles exception in _start_config."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            mock_socket = MagicMock()

            with (
                patch.object(iface, "myConnect") as mock_connect,
                patch.object(
                    iface, "_start_config", side_effect=RuntimeError("Config failed")
                ) as mock_config,
                patch.object(iface, "_close_socket_if_current") as mock_close,
            ):

                def connect_side_effect() -> None:
                    iface.socket = mock_socket

                mock_connect.side_effect = connect_side_effect

                result = iface._attempt_reconnect()

                assert result is False
                mock_config.assert_called_once()
                # Should close socket after config failure
                mock_close.assert_called_once_with(mock_socket)
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_attempt_reconnect_non_reader_thread_no_queue_clear() -> None:
    """Test _attempt_reconnect doesn't clear queue when not on reader thread."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            mock_socket = MagicMock()
            # Add items to queue
            mock_item1 = MagicMock()
            mock_item2 = MagicMock()
            iface.queue[1] = mock_item1
            iface.queue[2] = mock_item2
            original_queue_len = len(iface.queue)

            # Ensure we're not on the reader thread
            iface._rxThread = None  # type: ignore[assignment]

            with (
                patch.object(iface, "myConnect") as mock_connect,
                patch.object(iface, "_start_config"),
            ):

                def connect_side_effect() -> None:
                    iface.socket = mock_socket

                mock_connect.side_effect = connect_side_effect

                result = iface._attempt_reconnect()

                assert result is True
                # Queue should not be cleared when not on reader thread
                assert len(iface.queue) == original_queue_len
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_attempt_reconnect_queue_clear_notifies_pending() -> None:
    """Test _attempt_reconnect notifies pending entries when clearing queue."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            mock_socket = MagicMock()
            iface._rxThread = threading.current_thread()

            # Create mock pending entries with set_exception
            mock_pending1 = MagicMock()
            mock_pending2 = MagicMock()
            iface.queue[1] = mock_pending1
            iface.queue[2] = mock_pending2

            with (
                patch.object(iface, "myConnect") as mock_connect,
                patch.object(iface, "_start_config"),
            ):

                def connect_side_effect() -> None:
                    iface.socket = mock_socket

                mock_connect.side_effect = connect_side_effect

                result = iface._attempt_reconnect()

                assert result is True
                # Both pending entries should have been notified
                mock_pending1.set_exception.assert_called_once()
                mock_pending2.set_exception.assert_called_once()
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_attempt_reconnect_queue_clear_handles_notification_failure() -> (
    None
):
    """Test _attempt_reconnect handles exceptions when notifying pending entries."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            mock_socket = MagicMock()
            iface._rxThread = threading.current_thread()

            # Create mock pending entry that raises on set_exception
            mock_pending = MagicMock()
            mock_pending.set_exception.side_effect = RuntimeError("Notification failed")
            iface.queue[1] = mock_pending

            with (
                patch.object(iface, "myConnect") as mock_connect,
                patch.object(iface, "_start_config"),
            ):

                def connect_side_effect() -> None:
                    iface.socket = mock_socket

                mock_connect.side_effect = connect_side_effect

                # Should not raise despite notification failure
                result = iface._attempt_reconnect()

                assert result is True
                mock_pending.set_exception.assert_called_once()
        finally:
            iface.close()


# ============================================================================
# Pending Sender Notification Tests
# ============================================================================


@pytest.mark.unit
def test_TCPInterface_notify_pending_sender_failure_future_like() -> None:
    """Test _notify_pending_sender_failure with future-like object."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            mock_future = MagicMock()
            mock_future.set_exception = MagicMock()

            result = iface._notify_pending_sender_failure(
                mock_future, "test failure reason"
            )

            assert result is True
            mock_future.set_exception.assert_called_once()
            exc = mock_future.set_exception.call_args[0][0]
            assert isinstance(exc, ConnectionError)
            assert "test failure reason" in str(exc)
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_notify_pending_sender_failure_set_failed() -> None:
    """Test _notify_pending_sender_failure with set_failed pattern."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            mock_waiter = MagicMock()
            mock_waiter.set_exception = None  # No set_exception
            mock_waiter.set_failed = MagicMock()

            result = iface._notify_pending_sender_failure(mock_waiter, "custom failure")

            assert result is True
            mock_waiter.set_failed.assert_called_once()
            exc = mock_waiter.set_failed.call_args[0][0]
            assert isinstance(exc, ConnectionError)
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_notify_pending_sender_failure_signal_set() -> None:
    """Test _notify_pending_sender_failure with Event-like set() method."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            mock_event = MagicMock()
            mock_event.set_exception = None
            mock_event.set_failed = None
            mock_event.set = MagicMock()

            result = iface._notify_pending_sender_failure(mock_event, "wake up")

            assert result is True
            mock_event.set.assert_called_once()
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_notify_pending_sender_failure_unsupported() -> None:
    """Test _notify_pending_sender_failure with unsupported object."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            unsupported_obj = MagicMock()
            unsupported_obj.set_exception = None
            unsupported_obj.set_failed = None
            unsupported_obj.set = None

            result = iface._notify_pending_sender_failure(
                unsupported_obj, "some reason"
            )

            assert result is False
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_notify_pending_sender_failure_not_callable() -> None:
    """Test _notify_pending_sender_failure when methods are not callable."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            # Use a real object instead of MagicMock to avoid auto-callable attributes
            class NonCallableObj:
                """A non-callable object for testing failure notification."""

                set_exception = "not callable"

            obj = NonCallableObj()
            result = iface._notify_pending_sender_failure(obj, "reason")
            assert result is False
        finally:
            iface.close()


# ============================================================================
# Sleep Reconnect Delay Tests
# ============================================================================


@pytest.mark.unit
def test_TCPInterface_sleep_reconnect_delay_zero_delay() -> None:
    """Test _sleep_reconnect_delay with zero delay."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            with patch("meshtastic.tcp_interface.time.sleep") as mock_sleep:
                result = iface._sleep_reconnect_delay(0.0)

                assert result is True
                mock_sleep.assert_not_called()
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_sleep_reconnect_delay_small_delay() -> None:
    """Test _sleep_reconnect_delay with delay smaller than slice."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            with patch("meshtastic.tcp_interface.time.sleep") as mock_sleep:
                result = iface._sleep_reconnect_delay(0.1)

                assert result is True
                # Should have called sleep at least once with the smaller delay
                assert mock_sleep.call_count >= 1
                # First call should be with 0.1 (or remaining time)
                first_call = mock_sleep.call_args_list[0]
                assert first_call.args[0] <= 0.1
        finally:
            iface.close()


# ============================================================================
# Write Bytes Edge Cases
# ============================================================================


@pytest.mark.unit
def test_TCPInterface_write_bytes_value_error_not_fd_state() -> None:
    """Test _write_bytes propagates ValueError that's not a fd state error."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            mock_socket = MagicMock()
            iface.socket = mock_socket

            with (
                patch(
                    "meshtastic.tcp_interface.select.select",
                    side_effect=ValueError("some other value error"),
                ),
                pytest.raises(ValueError, match="some other value error"),
            ):
                iface._write_bytes(b"abc")

            # Socket should still be closed
            assert iface.socket is None
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_write_bytes_type_error_converted_to_oserror() -> None:
    """Test _write_bytes converts TypeError to OSError."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            mock_socket = MagicMock()
            iface.socket = mock_socket

            with (
                patch(
                    "meshtastic.tcp_interface.select.select",
                    side_effect=TypeError("fd is None"),
                ),
                pytest.raises(OSError, match="fd is None"),
            ):
                iface._write_bytes(b"abc")

            # Socket should be closed
            assert iface.socket is None
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_write_bytes_socket_not_closed_on_invalid_payload() -> None:
    """Test _write_bytes doesn't close socket on invalid payload type."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            mock_socket = MagicMock()
            iface.socket = mock_socket

            with pytest.raises(TypeError):
                # Pass string instead of bytes
                iface._write_bytes(cast(Any, "not bytes"))

            # Socket should NOT be closed for programmer errors
            assert iface.socket is mock_socket
            mock_socket.close.assert_not_called()
        finally:
            iface.close()


# ============================================================================
# Read Bytes Edge Cases
# ============================================================================


@pytest.mark.unit
def test_TCPInterface_read_bytes_non_fd_value_error_propagates() -> None:
    """Test _read_bytes propagates ValueError that's not a fd state error."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            mock_socket = MagicMock()
            mock_socket.recv.side_effect = ValueError("some other error")
            iface.socket = mock_socket

            with pytest.raises(ValueError, match="some other error"):
                iface._read_bytes(10)

            # Socket should NOT be closed for non-fd errors
            assert iface.socket is mock_socket
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_read_bytes_oserror_triggers_reconnect() -> None:
    """Test _read_bytes treats OSError as dead socket and reconnects."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            mock_socket = MagicMock()
            mock_socket.recv.side_effect = OSError("Connection reset")
            iface.socket = mock_socket

            with (
                patch.object(
                    iface, "_close_socket_if_current", return_value=True
                ) as mock_close,
                patch.object(iface, "_attempt_reconnect") as mock_reconnect,
            ):
                data = iface._read_bytes(10)

                assert data == b""
                mock_close.assert_called_once_with(mock_socket)
                mock_reconnect.assert_called_once()
        finally:
            iface.close()


# ============================================================================
# Reconnect Backoff Calculation Tests
# ============================================================================


@pytest.mark.unit
def test_TCPInterface_compute_reconnect_delay_zero_attempts() -> None:
    """Test _compute_reconnect_delay with zero attempts."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            # Zero attempts should give base delay (exponent = max(0, -1) = 0)
            iface._reconnect_attempts = 0
            delay = iface._compute_reconnect_delay()
            assert delay == 1.0
        finally:
            iface.close()


# ============================================================================
# Interface Close Tests
# ============================================================================


@pytest.mark.unit
def test_TCPInterface_close_no_socket() -> None:
    """Test close() when socket is None."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            iface.socket = None
            # Should not raise
            iface.close()
        finally:
            # Already closed
            pass


@pytest.mark.unit
def test_TCPInterface_close_exception_from_shared_close_propagates() -> None:
    """Test close() propagates exceptions from _shared_close."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            mock_socket = MagicMock()
            iface.socket = mock_socket

            # Patch on the instance to override method lookup
            with patch.object(
                iface, "_shared_close", side_effect=RuntimeError("Shared close failed")
            ):
                # Exception should propagate
                with pytest.raises(RuntimeError, match="Shared close failed"):
                    iface.close()

            # Cleanup errors must propagate after releasing the socket.
            mock_socket.close.assert_called_once()
            assert iface.socket is None
        finally:
            pass


# ============================================================================
# Integration/Flow Tests
# ============================================================================


@pytest.mark.unit
def test_TCPInterface_full_reconnect_flow() -> None:
    """Test full reconnect flow from read failure to successful reconnect."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            # Set up initial state
            original_socket = MagicMock()
            original_socket.recv.return_value = b""  # Simulates disconnect
            iface.socket = original_socket
            iface._reconnect_attempts = 0

            # Create new socket for reconnect
            new_socket = MagicMock()

            with (
                patch.object(iface, "myConnect") as mock_myconnect,
                patch.object(iface, "_start_config") as mock_config,
            ):

                def myconnect_side_effect() -> None:
                    iface.socket = new_socket

                mock_myconnect.side_effect = myconnect_side_effect

                # Manually trigger reconnect to test full flow
                result = iface._attempt_reconnect()

                assert result is True
                mock_myconnect.assert_called_once()
                mock_config.assert_called_once()
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_connect_successful_flow() -> None:
    """Test successful connect() flow."""
    with patch("socket.socket"):
        with patch("meshtastic.tcp_interface.socket.create_connection") as mock_connect:
            mock_socket = MagicMock()
            mock_connect.return_value = mock_socket

            iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
            try:
                iface.socket = mock_socket

                with patch.object(
                    iface, "_connect_socket_if_needed"
                ) as mock_connect_socket:
                    mock_connect_socket.return_value = None

                    with patch(
                        "meshtastic.tcp_interface.StreamInterface.connect"
                    ) as mock_super_connect:
                        iface.connect()

                        mock_connect_socket.assert_called_once()
                        mock_super_connect.assert_called_once()
            finally:
                iface.close()


@pytest.mark.unit
def test_TCPInterface_multiple_concurrent_close_attempts() -> None:
    """Test multiple threads attempting to close simultaneously."""
    with patch("socket.socket"):
        with patch("meshtastic.tcp_interface.socket.create_connection") as mock_connect:
            mock_socket = MagicMock()
            mock_connect.return_value = mock_socket

            iface = TCPInterface(hostname="localhost", noProto=True, connectNow=True)
            try:
                close_count = [0]

                def close_socket() -> None:
                    close_count[0] += 1
                    iface.close()

                # Start multiple close threads
                threads = [threading.Thread(target=close_socket) for _ in range(5)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=2.0)

                # All threads should complete without deadlocking
                assert all(not t.is_alive() for t in threads)
            finally:
                pass
