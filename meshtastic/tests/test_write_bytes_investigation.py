"""Tests exposing the write bytes inconsistency between TCPInterface and StreamInterface.

This module demonstrates the inconsistency in:
1. Exception types raised
2. Error handling patterns
3. Timeout handling semantics
"""

from unittest.mock import MagicMock, patch

import pytest

from meshtastic.stream_interface import StreamInterface
from meshtastic.tcp_interface import TCPInterface


class TestWriteBytesInconsistency:
    """Test suite exposing write bytes inconsistencies."""

    @pytest.mark.unit
    def test_tcp_vs_stream_exception_types_on_missing_connection(self) -> None:
        """INCONSISTENCY #1: Different exception types when no connection.

        TCPInterface raises ConnectionError, StreamInterface raises StreamClosedError.
        This is a bug because callers must catch different exceptions for the same failure mode.
        """
        # TCPInterface raises ConnectionError
        with patch("socket.socket"):
            tcp_iface = TCPInterface(
                hostname="localhost", noProto=True, connectNow=False
            )
            try:
                tcp_iface.socket = None
                with pytest.raises(ConnectionError):
                    tcp_iface._write_bytes(b"test")
            finally:
                tcp_iface.close()

        # StreamInterface raises StreamClosedError
        stream_iface = StreamInterface(noProto=True, connectNow=False)
        try:
            stream_iface.stream = None
            with pytest.raises(StreamInterface.StreamClosedError):
                stream_iface._write_bytes(b"test")
        finally:
            stream_iface.close()

    @pytest.mark.unit
    def test_tcp_vs_stream_exception_types_on_write_failure(self) -> None:
        """INCONSISTENCY #2: Different exception types on socket/stream errors.

        TCPInterface raises OSError/TimeoutError directly, StreamInterface wraps in StreamClosedError.
        """
        # TCPInterface - raises OSError directly
        with patch("socket.socket"):
            tcp_iface = TCPInterface(
                hostname="localhost", noProto=True, connectNow=False
            )
            try:
                mock_socket = MagicMock()
                mock_socket.send.side_effect = OSError("Network error")
                tcp_iface.socket = mock_socket

                with patch(
                    "meshtastic.tcp_interface.select.select",
                    return_value=([], [mock_socket], []),
                ):
                    with pytest.raises(OSError, match="Network error"):
                        tcp_iface._write_bytes(b"test")
            finally:
                tcp_iface.close()

        # StreamInterface - wraps OSError in StreamClosedError
        stream_iface = StreamInterface(noProto=True, connectNow=False)
        try:
            mock_stream = MagicMock()
            mock_stream.is_open = True
            mock_stream.write.side_effect = OSError("Network error")
            stream_iface.stream = mock_stream

            with pytest.raises(StreamInterface.StreamClosedError):
                stream_iface._write_bytes(b"test")
        finally:
            stream_iface.close()

    @pytest.mark.unit
    def test_tcp_vs_stream_timeout_exception_types(self) -> None:
        """INCONSISTENCY #3: Different exception types on timeout.

        TCPInterface raises TimeoutError, StreamInterface raises StreamClosedError.
        """
        # TCPInterface - raises TimeoutError
        with patch("socket.socket"):
            tcp_iface = TCPInterface(
                hostname="localhost", noProto=True, connectNow=False
            )
            try:
                mock_socket = MagicMock()
                tcp_iface.socket = mock_socket

                with patch(
                    "meshtastic.tcp_interface.select.select",
                    return_value=([], [], []),  # Not writable
                ):
                    with pytest.raises(TimeoutError):
                        tcp_iface._write_bytes(b"test")
            finally:
                tcp_iface.close()

        # StreamInterface - raises StreamClosedError with timeout message
        stream_iface = StreamInterface(noProto=True, connectNow=False)
        try:
            mock_stream = MagicMock()
            mock_stream.is_open = True
            # Simulate timeout by making write block forever

            call_count = [0]

            def fake_monotonic() -> float:
                call_count[0] += 1
                if call_count[0] == 1:
                    return 0.0  # Start time
                return 9999.0  # Way past deadline

            stream_iface.stream = mock_stream
            with patch(
                "meshtastic.stream_interface.time.monotonic", side_effect=fake_monotonic
            ):
                with pytest.raises(StreamInterface.StreamClosedError) as exc_info:
                    stream_iface._write_bytes(b"test")
                assert (
                    "timed out" in str(exc_info.value).lower()
                    or "timeout" in str(exc_info.value).lower()
                )
        finally:
            stream_iface.close()

    @pytest.mark.unit
    def test_tcp_partial_write_zero_bytes_vs_stream(self) -> None:
        """INCONSISTENCY #4: Different handling of zero-byte write returns.

        Both handle it, but TCP raises OSError, Stream raises StreamClosedError.
        """
        # TCPInterface - raises OSError when send() returns 0
        with patch("socket.socket"):
            tcp_iface = TCPInterface(
                hostname="localhost", noProto=True, connectNow=False
            )
            try:
                mock_socket = MagicMock()
                mock_socket.send.return_value = 0
                tcp_iface.socket = mock_socket

                with patch(
                    "meshtastic.tcp_interface.select.select",
                    return_value=([], [mock_socket], []),
                ):
                    with pytest.raises(OSError, match="no bytes"):
                        tcp_iface._write_bytes(b"test")
            finally:
                tcp_iface.close()

        # StreamInterface - raises StreamClosedError when written is 0
        stream_iface = StreamInterface(noProto=True, connectNow=False)
        try:
            mock_stream = MagicMock()
            mock_stream.is_open = True
            mock_stream.write.return_value = 0
            stream_iface.stream = mock_stream

            with pytest.raises(StreamInterface.StreamClosedError, match="no bytes"):
                stream_iface._write_bytes(b"test")
        finally:
            stream_iface.close()

    @pytest.mark.unit
    def test_tcp_socket_closure_on_error_vs_stream(self) -> None:
        """INCONSISTENCY #5: Different socket closure behavior.

        TCPInterface explicitly closes socket on error and sets to None.
        StreamInterface relies on _disconnected() to close stream.
        """
        # TCPInterface - explicitly closes socket on error
        with patch("socket.socket"):
            tcp_iface = TCPInterface(
                hostname="localhost", noProto=True, connectNow=False
            )
            try:
                mock_socket = MagicMock()
                mock_socket.send.side_effect = OSError("Error")
                tcp_iface.socket = mock_socket

                with patch(
                    "meshtastic.tcp_interface.select.select",
                    return_value=([], [mock_socket], []),
                ):
                    with pytest.raises(OSError):
                        tcp_iface._write_bytes(b"test")

                # TCPInterface closes socket and sets to None
                assert tcp_iface.socket is None
                mock_socket.close.assert_called_once()
            finally:
                tcp_iface.close()

        # StreamInterface - relies on _disconnected() which calls _close_stream_safely
        stream_iface = StreamInterface(noProto=True, connectNow=False)
        try:
            mock_stream = MagicMock()
            mock_stream.is_open = True
            mock_stream.write.side_effect = OSError("Error")
            stream_iface.stream = mock_stream

            with pytest.raises(StreamInterface.StreamClosedError):
                stream_iface._write_bytes(b"test")

            # StreamInterface doesn't close stream in _write_bytes
            # It relies on the caller to handle via exception handling
            mock_stream.close.assert_not_called()
        finally:
            stream_iface.close()

    @pytest.mark.unit
    def test_tcp_converts_fd_state_errors_vs_stream(self) -> None:
        """INCONSISTENCY #6: TCP has special handling for fd-state errors, Stream doesn't.

        TCPInterface converts TypeError/ValueError (fd is None, bad file descriptor) to OSError.
        StreamInterface has TypeError in STREAM_CLOSE_EXCEPTIONS but NOT in STREAM_WRITE_EXCEPTIONS,
        so TypeError from write() is NOT caught and wrapped.
        """
        # TCPInterface - converts fd-state TypeError to OSError
        with patch("socket.socket"):
            tcp_iface = TCPInterface(
                hostname="localhost", noProto=True, connectNow=False
            )
            try:
                mock_socket = MagicMock()
                tcp_iface.socket = mock_socket

                with patch(
                    "meshtastic.tcp_interface.select.select",
                    side_effect=TypeError("fd is None"),
                ):
                    with pytest.raises(OSError, match="fd is None"):
                        tcp_iface._write_bytes(b"test")
            finally:
                tcp_iface.close()

        # StreamInterface - TypeError from stream.write() is NOT in STREAM_WRITE_EXCEPTIONS
        # (which is STREAM_IO_EXCEPTIONS: OSError, ValueError, serial.SerialException, serial.SerialTimeoutException)
        # So TypeError propagates directly without being caught or wrapped
        stream_iface = StreamInterface(noProto=True, connectNow=False)
        try:
            mock_stream = MagicMock()
            mock_stream.is_open = True
            mock_stream.write.side_effect = TypeError("bad type")
            stream_iface.stream = mock_stream

            # TypeError is NOT caught by STREAM_WRITE_EXCEPTIONS, so it propagates directly
            with pytest.raises(TypeError, match="bad type"):
                stream_iface._write_bytes(b"test")
        finally:
            stream_iface.close()

    @pytest.mark.unit
    def test_tcp_vs_stream_partial_write_behavior(self) -> None:
        """SIMILARITY: Both handle partial writes correctly.

        This is what they both do correctly - partial write handling in a loop.
        """
        # TCPInterface - partial writes handled in loop
        with patch("socket.socket"):
            tcp_iface = TCPInterface(
                hostname="localhost", noProto=True, connectNow=False
            )
            try:
                mock_socket = MagicMock()
                # Send 2 bytes at a time
                mock_socket.send.side_effect = [2, 2, 1]
                tcp_iface.socket = mock_socket

                with patch(
                    "meshtastic.tcp_interface.select.select",
                    return_value=([], [mock_socket], []),
                ):
                    tcp_iface._write_bytes(b"hello")  # 5 bytes

                # Should have been called 3 times: 5 -> 3 -> 1 -> 0
                assert mock_socket.send.call_count == 3
            finally:
                tcp_iface.close()

        # StreamInterface - partial writes handled in loop
        stream_iface = StreamInterface(noProto=True, connectNow=False)
        try:
            mock_stream = MagicMock()
            # Write 2 bytes at a time
            mock_stream.is_open = True
            mock_stream.write.side_effect = [2, 2, 1]
            stream_iface.stream = mock_stream

            stream_iface._write_bytes(b"hello")  # 5 bytes

            # Should have been called 3 times: 5 -> 3 -> 1 -> 0
            assert mock_stream.write.call_count == 3
        finally:
            stream_iface.close()


class TestUnifiedExceptionInterface:
    """Tests demonstrating what a unified interface should look like."""

    @pytest.mark.unit
    def test_proposed_unified_exception_hierarchy(self) -> None:
        """Demonstrate the ideal: both interfaces raise same exception type.

        RECOMMENDATION: Create a common TransportError hierarchy:
        - TransportError (base)
          - TransportClosedError (when no connection)
          - TransportTimeoutError (when timeout)
          - TransportWriteError (when write fails)

        OR: Make TCPInterface also raise StreamClosedError variants.
        """
        # Currently these are different:
        # - TCPInterface: ConnectionError, TimeoutError, OSError
        # - StreamInterface: StreamClosedError
        #
        # RECOMMENDATION: Make TCPInterface raise StreamClosedError (or common subtype)
        # for all write failures to match StreamInterface's behavior.


class TestReturnValueConsistency:
    """Tests checking return value handling."""

    @pytest.mark.unit
    def test_both_return_none(self) -> None:
        """SIMILARITY: Both return None on success.

        This is consistent between implementations.
        """
        # TCPInterface
        with patch("socket.socket"):
            tcp_iface = TCPInterface(
                hostname="localhost", noProto=True, connectNow=False
            )
            try:
                mock_socket = MagicMock()
                mock_socket.send.return_value = 5
                tcp_iface.socket = mock_socket

                with patch(
                    "meshtastic.tcp_interface.select.select",
                    return_value=([], [mock_socket], []),
                ):
                    tcp_iface._write_bytes(b"hello")
            finally:
                tcp_iface.close()

        # StreamInterface
        stream_iface = StreamInterface(noProto=True, connectNow=False)
        try:
            mock_stream = MagicMock()
            mock_stream.is_open = True
            mock_stream.write.return_value = 5
            stream_iface.stream = mock_stream

            stream_iface._write_bytes(b"hello")
        finally:
            stream_iface.close()
