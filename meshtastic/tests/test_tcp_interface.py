"""Meshtastic unit tests for tcp_interface.py."""

import re
import socket
import threading
from typing import Any, cast
from unittest.mock import MagicMock, call, patch

import pytest

from ..protobuf import config_pb2
from ..tcp_interface import TCPInterface


@pytest.mark.unit
@pytest.mark.parametrize("reader_alive", [False, True])
def test_close_half_closes_before_bounded_reader_drain(reader_alive: bool) -> None:
    """Keep the socket open for the final write before forcing reader exit."""
    iface = TCPInterface("localhost", noProto=True, connectNow=False)
    sock = MagicMock()
    reader = MagicMock()
    reader.is_alive.return_value = reader_alive
    iface.socket = sock
    iface._rxThread = reader
    events = MagicMock()
    events.attach_mock(sock, "socket")
    events.attach_mock(reader, "reader")

    iface.close()

    expected = [call.socket.shutdown(socket.SHUT_WR)]
    if reader_alive:
        expected.append(call.reader.join(timeout=0.25))
    expected.extend([call.socket.shutdown(socket.SHUT_RDWR), call.socket.close()])
    actual = [
        event
        for event in events.mock_calls
        if event[0] in ("socket.shutdown", "socket.close", "reader.join")
    ]
    assert actual[: len(expected)] == expected
    assert iface.socket is None


@pytest.mark.unit
def test_close_from_reader_does_not_join_itself() -> None:
    """A callback may close its own transport without a thread join error."""
    iface = TCPInterface("localhost", noProto=True, connectNow=False)
    sock = MagicMock()
    iface.socket = sock
    iface._rxThread = threading.current_thread()
    iface.close()
    assert sock.shutdown.call_args_list == [
        call(socket.SHUT_WR),
        call(socket.SHUT_RDWR),
    ]


@pytest.mark.unit
def test_TCPInterface(capsys: pytest.CaptureFixture[str]) -> None:
    """Test that we can instantiate a TCPInterface."""
    with patch("socket.socket") as mock_socket:
        iface = TCPInterface(hostname="localhost", noProto=True)
        try:
            iface.localNode.localConfig.lora.CopyFrom(config_pb2.Config.LoRaConfig())
            iface.myConnect()
            iface.showInfo()
            iface.localNode.showInfo()
            out, err = capsys.readouterr()
            assert re.search(r"Owner: None \(None\)", out, re.MULTILINE)
            assert re.search(r"Nodes", out, re.MULTILINE)
            assert re.search(r"Preferences", out, re.MULTILINE)
            assert re.search(r"Channels", out, re.MULTILINE)
            assert re.search(r"Primary channel URL", out, re.MULTILINE)
            assert err == ""
            assert mock_socket.called
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_exception() -> None:
    """Verify TCPInterface.close() handles exceptions from _socket_shutdown.

    Ensures shutdown exceptions are suppressed so close() completes.
    """

    def throw_an_exception(*_args: object) -> None:
        """Raise a ValueError with the message "Fake exception.".

        Raises
        ------
        ValueError
            Always raised with the message "Fake exception.".
        """
        raise ValueError("Fake exception.")

    with patch(
        "meshtastic.tcp_interface.TCPInterface._socket_shutdown"
    ) as mock_shutdown:
        mock_shutdown.side_effect = throw_an_exception
        with patch("socket.socket") as mock_socket:
            iface = TCPInterface(hostname="localhost", noProto=True)
            iface.myConnect()
            iface.close()
            assert mock_socket.called
            assert mock_shutdown.called


@pytest.mark.unit
def test_TCPInterface_myConnect_clears_socket_timeout() -> None:
    """MyConnect should restore blocking mode after connect-timeout use."""
    with patch("meshtastic.tcp_interface.socket.create_connection") as mock_connect:
        connected_socket = MagicMock()
        mock_connect.return_value = connected_socket
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            iface.myConnect()
            connected_socket.settimeout.assert_called_once_with(None)
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_without_connecting() -> None:
    """Test that we can instantiate a TCPInterface with connectNow as false."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            assert iface.socket is None
        finally:
            iface.close()


@pytest.mark.unit
@pytest.mark.parametrize(
    "connect_timeout",
    [
        pytest.param(0.0, id="zero"),
        pytest.param(-1.0, id="negative"),
        pytest.param(True, id="bool"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="inf"),
        pytest.param("invalid-timeout", id="string"),
    ],
)
def test_TCPInterface_rejects_non_positive_connect_timeout(
    connect_timeout: object,
) -> None:
    """Constructor should fail fast for invalid connectTimeout values."""
    with pytest.raises(
        ValueError,
        match=r"connectTimeout must be a positive finite number",
    ):
        TCPInterface(
            hostname="localhost",
            noProto=True,
            connectNow=False,
            connectTimeout=cast(float | None, connect_timeout),
        )


@pytest.mark.unit
def test_TCPInterface_accepts_none_connect_timeout() -> None:
    """None connectTimeout should defer to socket's default timeout behavior."""
    with patch("meshtastic.tcp_interface.socket.create_connection") as mock_connect:
        connected_socket = MagicMock()
        mock_connect.return_value = connected_socket
        iface = TCPInterface(
            hostname="localhost",
            noProto=True,
            connectNow=False,
            connectTimeout=None,
        )
        try:
            iface.myConnect()
            mock_connect.assert_called_once_with(("localhost", 4403))
            connected_socket.settimeout.assert_called_once_with(None)
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_write_uses_partial_send_loop() -> None:
    """_write_bytes should loop on send() until the full payload is transmitted."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            mock_socket = MagicMock()
            mock_socket.send.side_effect = [1, 2]
            iface.socket = mock_socket
            with patch(
                "meshtastic.tcp_interface.select.select",
                return_value=([], [mock_socket], []),
            ):
                iface._write_bytes(b"abc")
            assert mock_socket.send.call_args_list == [call(b"abc"), call(b"bc")]
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_write_raises_when_socket_missing() -> None:
    """_write_bytes should fail fast when no TCP socket is connected."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            iface.socket = None
            with pytest.raises(ConnectionError, match="closed or not connected"):
                iface._write_bytes(b"abc")
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_write_reraises_socket_errors() -> None:
    """_write_bytes should propagate socket write failures after cleanup."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            mock_socket = MagicMock()
            mock_socket.send.side_effect = OSError("boom")
            iface.socket = mock_socket

            with (
                patch(
                    "meshtastic.tcp_interface.select.select",
                    return_value=([], [mock_socket], []),
                ),
                pytest.raises(OSError, match="boom"),
            ):
                iface._write_bytes(b"abc")

            assert iface.socket is None
        finally:
            iface.close()


@pytest.mark.unit
@pytest.mark.parametrize("select_error", [ValueError, TypeError])
def test_TCPInterface_write_normalizes_select_error_to_oserror(
    select_error: type[Exception],
) -> None:
    """_write_bytes should normalize select readiness errors into OSError after cleanup."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            mock_socket = MagicMock()
            iface.socket = mock_socket

            with (
                patch(
                    "meshtastic.tcp_interface.select.select",
                    side_effect=select_error("bad file descriptor"),
                ),
                pytest.raises(OSError, match="bad file descriptor"),
            ):
                iface._write_bytes(b"abc")

            mock_socket.close.assert_called_once()
            assert iface.socket is None
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_write_propagates_invalid_payload_type() -> None:
    """_write_bytes should not treat caller payload type errors as socket failures."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            mock_socket = MagicMock()
            iface.socket = mock_socket

            with (
                patch("meshtastic.tcp_interface.select.select") as select_mock,
                pytest.raises(TypeError, match="a bytes-like object is required"),
            ):
                iface._write_bytes(cast(Any, "abc"))

            select_mock.assert_not_called()
            mock_socket.close.assert_not_called()
            assert iface.socket is mock_socket
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_write_times_out_when_socket_not_writable() -> None:
    """_write_bytes should timeout when the socket never becomes writable."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            mock_socket = MagicMock()
            iface.socket = mock_socket
            with (
                patch(
                    "meshtastic.tcp_interface.select.select", return_value=([], [], [])
                ),
                pytest.raises(
                    TimeoutError, match="timed out waiting for socket readiness"
                ),
            ):
                iface._write_bytes(b"abc")
            assert iface.socket is None
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_read_empty_does_not_reconnect_when_closing() -> None:
    """Test that _read_bytes avoids reconnect attempts during intentional shutdown."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            mock_socket = MagicMock()
            mock_socket.recv.return_value = b""
            iface.socket = mock_socket
            iface._wantExit = True

            with (
                patch.object(iface, "myConnect") as mock_connect,
                patch.object(iface, "_start_config") as mock_start_config,
                patch("meshtastic.tcp_interface.time.sleep") as mock_sleep,
            ):
                data = iface._read_bytes(1)

            assert data == b""
            mock_connect.assert_not_called()
            mock_start_config.assert_not_called()
            mock_sleep.assert_not_called()
            mock_socket.close.assert_called_once()
            assert iface.socket is None
        finally:
            iface.close()


@pytest.mark.unit
@pytest.mark.parametrize("recv_error", [OSError, ConnectionResetError])
def test_TCPInterface_read_error_triggers_reconnect_cleanup(
    recv_error: type[OSError],
) -> None:
    """_read_bytes should treat recv errors as a dead socket and attempt reconnect."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            mock_socket = MagicMock()
            mock_socket.recv.side_effect = recv_error("bad file descriptor")
            iface.socket = mock_socket

            with patch.object(iface, "_attempt_reconnect") as reconnect_mock:
                data = iface._read_bytes(1)

            assert data == b""
            reconnect_mock.assert_called_once_with()
            mock_socket.close.assert_called_once()
            assert iface.socket is None
        finally:
            iface.close()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("recv_error_type", "message"),
    [
        pytest.param(TypeError, "fd is None", id="type-error"),
        pytest.param(
            ValueError,
            "file descriptor cannot be a negative integer (-1)",
            id="value-error",
        ),
    ],
)
def test_TCPInterface_read_fd_race_errors_trigger_reconnect_cleanup(
    recv_error_type: type[Exception],
    message: str,
) -> None:
    """Valid read requests should treat fd-state race errors as dead sockets."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            mock_socket = MagicMock()
            mock_socket.recv.side_effect = recv_error_type(message)
            iface.socket = mock_socket

            with patch.object(iface, "_attempt_reconnect") as reconnect_mock:
                data = iface._read_bytes(1)

            assert data == b""
            reconnect_mock.assert_called_once_with()
            mock_socket.close.assert_called_once()
            assert iface.socket is None
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_read_propagates_invalid_length_type() -> None:
    """_read_bytes should reject caller length-type errors before socket recv."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            mock_socket = MagicMock()
            iface.socket = mock_socket

            with (
                patch.object(iface, "_attempt_reconnect") as reconnect_mock,
                pytest.raises(ValueError, match="length must be a positive int"),
            ):
                iface._read_bytes(cast(Any, "1"))

            reconnect_mock.assert_not_called()
            mock_socket.recv.assert_not_called()
            mock_socket.close.assert_not_called()
            assert iface.socket is mock_socket
        finally:
            iface.close()


@pytest.mark.unit
@pytest.mark.parametrize("invalid_length", [0, -1])
def test_TCPInterface_read_rejects_non_positive_length(invalid_length: int) -> None:
    """_read_bytes should fail fast for zero/negative lengths."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            mock_socket = MagicMock()
            iface.socket = mock_socket

            with (
                patch.object(iface, "_attempt_reconnect") as reconnect_mock,
                pytest.raises(ValueError, match="length must be a positive int"),
            ):
                iface._read_bytes(invalid_length)

            reconnect_mock.assert_not_called()
            mock_socket.recv.assert_not_called()
            mock_socket.close.assert_not_called()
            assert iface.socket is mock_socket
        finally:
            iface.close()


@pytest.mark.unit
@pytest.mark.parametrize("invalid_length", [0, -1, True])
def test_TCPInterface_read_rejects_invalid_length_when_socket_missing(
    invalid_length: object,
) -> None:
    """Invalid lengths should raise before reconnect logic even when socket is missing."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            iface.socket = None
            with (
                patch.object(iface, "_attempt_reconnect") as reconnect_mock,
                pytest.raises(ValueError, match="length must be a positive int"),
            ):
                iface._read_bytes(cast(Any, invalid_length))

            reconnect_mock.assert_not_called()
            assert iface.socket is None
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_connect_fails_fast_when_shutting_down() -> None:
    """connect() should fail fast when shutdown/fatal flags block a new connect."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        iface.socket = None
        iface._wantExit = True
        try:
            with (
                patch.object(iface, "myConnect") as mock_connect,
                patch("meshtastic.tcp_interface.StreamInterface.connect") as mock_super,
            ):
                with pytest.raises(
                    ConnectionError,
                    match=r"Cannot connect to localhost: interface is shutting down",
                ):
                    iface.connect()
            mock_connect.assert_not_called()
            mock_super.assert_not_called()
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_connect_fails_fast_after_fatal_disconnect() -> None:
    """connect() should fail fast after reconnect is disabled by fatal disconnect."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        iface.socket = None
        iface._wantExit = False
        iface._fatal_disconnect = True
        try:
            with (
                patch.object(iface, "myConnect") as mock_connect,
                patch("meshtastic.tcp_interface.StreamInterface.connect") as mock_super,
            ):
                with pytest.raises(
                    ConnectionError,
                    match=r"Cannot connect to localhost: reconnect disabled after fatal disconnect",
                ):
                    iface.connect()
            mock_connect.assert_not_called()
            mock_super.assert_not_called()
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_connect_socket_waits_for_inflight_reconnect_then_connects() -> (
    None
):
    """_connect_socket_if_needed should poll while reconnect is in-flight and then connect."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            iface.socket = None
            with iface._reconnect_attempt_lock:
                with iface._reconnect_lock:
                    iface._reconnect_attempt_in_progress = True

            def _finish_reconnect_slice(_delay: float) -> None:
                with iface._reconnect_attempt_lock:
                    with iface._reconnect_lock:
                        iface._reconnect_attempt_in_progress = False

            with (
                patch(
                    "meshtastic.tcp_interface.time.sleep",
                    side_effect=_finish_reconnect_slice,
                ) as sleep_mock,
                patch.object(iface, "myConnect") as my_connect_mock,
            ):
                iface._connect_socket_if_needed()

            sleep_mock.assert_called_once_with(iface.DEFAULT_RECONNECT_SLEEP_SLICE)
            my_connect_mock.assert_called_once_with()
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_notify_pending_sender_failure_sets_event() -> None:
    """_notify_pending_sender_failure should signal Event-like waiters."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            pending_event = threading.Event()
            assert (
                iface._notify_pending_sender_failure(
                    pending_event, "connect failed during test"
                )
                is True
            )
            assert pending_event.is_set()
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_attempt_reconnect_reader_thread_clears_queue() -> None:
    """Ensure reader-thread reconnect clears queued packets before _start_config().

    This locks in the deadlock-avoidance behavior documented in _attempt_reconnect:
    when reconnect runs on the reader thread, pending packets are dropped so
    _start_config() cannot block waiting on queue progress that depends on the same
    thread.
    """
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            iface._rxThread = threading.current_thread()
            iface.queue[1] = True
            iface.queue[2] = True
            mock_socket = MagicMock()

            with (
                patch.object(iface, "myConnect") as mock_connect,
                patch.object(iface, "_start_config") as mock_start_config,
            ):

                def _connect_side_effect() -> None:
                    iface.socket = mock_socket

                def _start_config_side_effect() -> None:
                    assert len(iface.queue) == 0

                mock_connect.side_effect = _connect_side_effect
                mock_start_config.side_effect = _start_config_side_effect

                assert iface._attempt_reconnect() is True

            mock_connect.assert_called_once()
            mock_start_config.assert_called_once()
            assert len(iface.queue) == 0
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_attempt_reconnect_does_not_wait_connected() -> None:
    """Reconnect should run startup without calling _wait_connected().

    _attempt_reconnect() is used from the background reader thread path, so it
    must not introduce a wait on protocol responses that are processed by that
    same thread.
    """
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            mock_socket = MagicMock()
            with (
                patch.object(iface, "myConnect") as mock_connect,
                patch.object(iface, "_start_config") as mock_start_config,
                patch.object(iface, "_wait_connected") as mock_wait_connected,
            ):

                def _connect_side_effect() -> None:
                    iface.socket = mock_socket

                mock_connect.side_effect = _connect_side_effect

                assert iface._attempt_reconnect() is True

            mock_connect.assert_called_once()
            mock_start_config.assert_called_once()
            mock_wait_connected.assert_not_called()
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_compute_reconnect_delay_exponential_backoff() -> None:
    """Test _compute_reconnect_delay implements exponential backoff correctly."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            # Test defaults: base=1.0, backoff=1.6, max=30.0
            # Attempt 1: exponent = max(0, 1-1) = 0, delay = 1.0 * 1.6^0 = 1.0
            iface._reconnect_attempts = 1
            assert iface._compute_reconnect_delay() == 1.0

            # Attempt 2: exponent = max(0, 2-1) = 1, delay = 1.0 * 1.6^1 = 1.6
            iface._reconnect_attempts = 2
            assert iface._compute_reconnect_delay() == 1.6

            # Attempt 3: exponent = 2, delay = 1.0 * 1.6^2 = 2.56
            iface._reconnect_attempts = 3
            assert abs(iface._compute_reconnect_delay() - 2.56) < 0.001

            # Attempt 4: exponent = 3, delay = 1.0 * 1.6^3 = 4.096
            iface._reconnect_attempts = 4
            assert abs(iface._compute_reconnect_delay() - 4.096) < 0.001

            # Attempt 8: should cap at max_delay (30.0)
            iface._reconnect_attempts = 8
            # Without cap: 1.0 * 1.6^7 = 26.8435456
            # With cap: min(30.0, 26.8435456) = 26.8435456
            expected = 1.0 * (1.6**7)
            assert abs(iface._compute_reconnect_delay() - expected) < 0.001

            # Attempt 10: would exceed max_delay without cap
            iface._reconnect_attempts = 10
            # Without cap: 1.0 * 1.6^9 = 68.719476736
            # With cap: min(30.0, 68.719476736) = 30.0
            assert iface._compute_reconnect_delay() == 30.0
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_compute_reconnect_delay_custom_parameters() -> None:
    """Test _compute_reconnect_delay with custom backoff parameters."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            # Customize parameters
            iface._reconnect_base_delay = 2.0
            iface._reconnect_backoff = 2.0
            iface._reconnect_max_delay = 60.0

            # Attempt 1: exponent = 0, delay = 2.0 * 2.0^0 = 2.0
            iface._reconnect_attempts = 1
            assert iface._compute_reconnect_delay() == 2.0

            # Attempt 2: exponent = 1, delay = 2.0 * 2.0^1 = 4.0
            iface._reconnect_attempts = 2
            assert iface._compute_reconnect_delay() == 4.0

            # Attempt 3: exponent = 2, delay = 2.0 * 2.0^2 = 8.0
            iface._reconnect_attempts = 3
            assert iface._compute_reconnect_delay() == 8.0

            # Attempt 5: exponent = 4, delay = 2.0 * 2.0^4 = 32.0
            iface._reconnect_attempts = 5
            assert iface._compute_reconnect_delay() == 32.0

            # Attempt 6: should cap at max_delay (60.0)
            iface._reconnect_attempts = 6
            # Without cap: 2.0 * 2.0^5 = 64.0
            # With cap: min(60.0, 64.0) = 60.0
            assert iface._compute_reconnect_delay() == 60.0
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_write_times_out_before_select_when_deadline_elapsed() -> None:
    """_write_bytes should raise TimeoutError when no write budget remains."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            mock_socket = MagicMock()
            iface.socket = mock_socket
            with (
                patch(
                    "meshtastic.tcp_interface.time.monotonic",
                    side_effect=[0.0, 9999.0],
                ),
                pytest.raises(
                    TimeoutError, match="timed out waiting for socket readiness"
                ),
            ):
                iface._write_bytes(b"abc")
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_write_raises_when_send_returns_zero_bytes() -> None:
    """_write_bytes should treat zero-byte sends as a socket write failure."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            mock_socket = MagicMock()
            mock_socket.send.return_value = 0
            iface.socket = mock_socket

            with (
                patch(
                    "meshtastic.tcp_interface.select.select",
                    return_value=([], [mock_socket], []),
                ),
                pytest.raises(OSError, match="TCP write returned no bytes"),
            ):
                iface._write_bytes(b"abc")

            assert iface.socket is None
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_attempt_reconnect_max_attempts_triggers_fatal() -> None:
    """Test that exceeding max reconnect attempts triggers fatal disconnect."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            # Set up to trigger max attempts (already at max)
            iface._reconnect_attempts = iface._max_reconnect_attempts
            assert iface._fatal_disconnect is False

            result = iface._attempt_reconnect()

            # Should return False and mark fatal disconnect
            assert result is False
            assert iface._fatal_disconnect is True
            assert iface._wantExit is True
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_sleep_reconnect_delay_interrupted_by_shutdown() -> None:
    """Test that _sleep_reconnect_delay returns False when shutdown is requested."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            iface._wantExit = False
            iface._fatal_disconnect = False

            sleep_calls = {"count": 0}

            def _trigger_shutdown_after_first_sleep(_delay: float) -> None:
                sleep_calls["count"] += 1
                if sleep_calls["count"] == 1:
                    iface._wantExit = True

            # Should return False when interrupted
            with patch(
                "meshtastic.tcp_interface.time.sleep",
                side_effect=_trigger_shutdown_after_first_sleep,
            ):
                result = iface._sleep_reconnect_delay(10.0)

            assert result is False
            assert sleep_calls["count"] == 1
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_sleep_reconnect_delay_interrupted_by_fatal_disconnect() -> None:
    """Test that _sleep_reconnect_delay returns False on fatal disconnect."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            iface._wantExit = False
            iface._fatal_disconnect = False

            sleep_calls = {"count": 0}

            def _trigger_fatal_after_first_sleep(_delay: float) -> None:
                sleep_calls["count"] += 1
                if sleep_calls["count"] == 1:
                    iface._fatal_disconnect = True

            # Should return False when interrupted
            with patch(
                "meshtastic.tcp_interface.time.sleep",
                side_effect=_trigger_fatal_after_first_sleep,
            ):
                result = iface._sleep_reconnect_delay(10.0)

            assert result is False
            assert sleep_calls["count"] == 1
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_sleep_reconnect_delay_completes_full_delay() -> None:
    """Test that _sleep_reconnect_delay returns True when delay completes."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            iface._wantExit = False
            iface._fatal_disconnect = False

            monotonic_time = {"value": 0.0}

            def _fake_monotonic() -> float:
                value = monotonic_time["value"]
                monotonic_time["value"] += 0.1
                return value

            with (
                patch("meshtastic.tcp_interface.time.sleep") as mock_sleep,
                patch(
                    "meshtastic.tcp_interface.time.monotonic",
                    side_effect=_fake_monotonic,
                ),
            ):
                result = iface._sleep_reconnect_delay(0.3)

            assert mock_sleep.call_count == 2
            assert result is True
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_attempt_reconnect_already_in_progress() -> None:
    """Test that _attempt_reconnect returns False if already in progress."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            # Mark reconnect as in progress
            iface._reconnect_attempt_in_progress = True

            result = iface._attempt_reconnect()

            # Should return False without attempting
            assert result is False
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_attempt_reconnect_aborted_by_shutdown() -> None:
    """Test that _attempt_reconnect returns False when shutdown is requested."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            iface._wantExit = True
            iface._reconnect_attempt_in_progress = False

            result = iface._attempt_reconnect()

            # Should return False without attempting
            assert result is False
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_attempt_reconnect_aborted_by_fatal_disconnect() -> None:
    """Test that _attempt_reconnect returns False on fatal disconnect."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            iface._wantExit = False
            iface._fatal_disconnect = True
            iface._reconnect_attempt_in_progress = False

            result = iface._attempt_reconnect()

            # Should return False without attempting
            assert result is False
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_on_fatal_disconnect_idempotent() -> None:
    """Test that _on_fatal_disconnect is idempotent."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            iface._fatal_disconnect = False
            iface._wantExit = False
            iface._reconnect_attempts = 5

            # First call should set flags
            iface._on_fatal_disconnect("test reason")
            assert iface._fatal_disconnect is True
            assert iface._wantExit is True

            # Second call should be no-op
            iface._on_fatal_disconnect("another reason")
            assert iface._fatal_disconnect is True
            assert iface._wantExit is True
        finally:
            iface.close()
