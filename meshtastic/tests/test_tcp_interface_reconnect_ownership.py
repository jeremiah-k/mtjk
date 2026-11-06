"""Reconnect socket-ownership regressions for TCPInterface.

These tests pin the reconnect state machine guarantees around socket
ownership: a reconnect attempt must never report success for a socket it no
longer owns, a waiting ``connect()`` must respect shutdown/fatal intent
immediately, and an already-active socket keeps the historical silent-return
path in ``_connect_socket_if_needed()``.
"""

from unittest.mock import MagicMock, patch

import pytest

from ..tcp_interface import TCPInterface

pytestmark = pytest.mark.unit


def _make_reconnect_test_interface() -> TCPInterface:
    """Build a TCPInterface without touching the network or reader thread."""
    with patch("socket.socket"):
        return TCPInterface(hostname="localhost", noProto=True, connectNow=False)


def test_waiting_connect_raises_immediately_on_shutdown() -> None:
    """A connect() waiting on an in-flight reconnect fails fast on shutdown."""
    iface = _make_reconnect_test_interface()
    try:
        with iface._reconnect_attempt_lock:
            with iface._reconnect_lock:
                iface._reconnect_attempt_in_progress = True
                iface._wantExit = True

        with (
            patch(
                "meshtastic.tcp_interface.time.sleep",
                side_effect=AssertionError("must not poll during shutdown"),
            ),
            patch.object(iface, "myConnect") as mock_connect,
        ):
            with pytest.raises(ConnectionError, match="interface is shutting down"):
                iface._connect_socket_if_needed()
        mock_connect.assert_not_called()
    finally:
        iface.close()


def test_waiting_connect_raises_immediately_on_fatal_disconnect() -> None:
    """A connect() waiting on an in-flight reconnect fails fast after fatal."""
    iface = _make_reconnect_test_interface()
    try:
        with iface._reconnect_attempt_lock:
            with iface._reconnect_lock:
                iface._reconnect_attempt_in_progress = True
                iface._fatal_disconnect = True

        with (
            patch(
                "meshtastic.tcp_interface.time.sleep",
                side_effect=AssertionError("must not poll after fatal disconnect"),
            ),
            patch.object(iface, "myConnect") as mock_connect,
        ):
            with pytest.raises(
                ConnectionError, match="reconnect disabled after fatal disconnect"
            ):
                iface._connect_socket_if_needed()
        mock_connect.assert_not_called()
    finally:
        iface.close()


def test_active_socket_returns_silently_during_shutdown() -> None:
    """An active socket keeps the historical silent-return path during shutdown.

    The shutdown/fatal guard applies to opening a new socket, not to callers
    that already own a live one; StreamInterface.connect() coordinates with
    close() for that case.
    """
    iface = _make_reconnect_test_interface()
    try:
        active_socket = MagicMock()
        iface.socket = active_socket
        iface._wantExit = True

        with (
            patch(
                "meshtastic.tcp_interface.time.sleep",
                side_effect=AssertionError("must not poll with an active socket"),
            ),
            patch.object(iface, "myConnect") as mock_connect,
        ):
            iface._connect_socket_if_needed()
        mock_connect.assert_not_called()
        assert iface.socket is active_socket
    finally:
        iface.close()


def test_active_socket_returns_silently_after_fatal_disconnect() -> None:
    """An active socket returns silently instead of raising after fatal disconnect."""
    iface = _make_reconnect_test_interface()
    try:
        active_socket = MagicMock()
        iface.socket = active_socket
        iface._fatal_disconnect = True

        with (
            patch(
                "meshtastic.tcp_interface.time.sleep",
                side_effect=AssertionError("must not poll with an active socket"),
            ),
            patch.object(iface, "myConnect") as mock_connect,
        ):
            iface._connect_socket_if_needed()
        mock_connect.assert_not_called()
        assert iface.socket is active_socket
    finally:
        iface.close()


def test_close_during_config_cannot_report_success() -> None:
    """A close() that wins during _start_config() must not report reconnect success."""
    iface = _make_reconnect_test_interface()
    try:
        reconnect_socket = MagicMock()

        def _fake_my_connect() -> None:
            iface.socket = reconnect_socket

        def _fake_start_config() -> None:
            # Simulate close() winning while _start_config() runs: shutdown
            # intent is set and the socket is torn down.
            iface._wantExit = True
            iface.socket = None

        with (
            patch.object(iface, "myConnect", side_effect=_fake_my_connect),
            patch.object(iface, "_start_config", side_effect=_fake_start_config),
        ):
            assert iface._attempt_reconnect() is False
        assert iface._reconnect_attempts == 1
        assert iface._reconnect_attempt_in_progress is False
        assert iface.socket is None
    finally:
        iface.close()


def test_socket_replacement_during_config_cannot_report_success() -> None:
    """A socket handed over during _start_config() must not report success."""
    iface = _make_reconnect_test_interface()
    try:
        original_socket = MagicMock()
        replacement_socket = MagicMock()

        def _fake_my_connect() -> None:
            iface.socket = original_socket

        def _fake_start_config() -> None:
            iface.socket = replacement_socket

        with (
            patch.object(iface, "myConnect", side_effect=_fake_my_connect),
            patch.object(iface, "_start_config", side_effect=_fake_start_config),
        ):
            assert iface._attempt_reconnect() is False
        assert iface.socket is replacement_socket
        assert iface._reconnect_attempts == 1
    finally:
        iface.close()


def test_fatal_transition_during_config_is_not_cleared() -> None:
    """A fatal transition during config must not be cleared by reconnect success."""
    iface = _make_reconnect_test_interface()
    try:
        reconnect_socket = MagicMock()

        def _fake_my_connect() -> None:
            iface.socket = reconnect_socket

        def _fake_start_config() -> None:
            iface._fatal_disconnect = True

        with (
            patch.object(iface, "myConnect", side_effect=_fake_my_connect),
            patch.object(iface, "_start_config", side_effect=_fake_start_config),
        ):
            assert iface._attempt_reconnect() is False
        assert iface._fatal_disconnect is True
        assert iface._reconnect_attempts == 1
    finally:
        iface.close()


def test_successful_reconnect_resets_retry_counter() -> None:
    """A reconnect that keeps socket ownership resets the retry counter."""
    iface = _make_reconnect_test_interface()
    try:
        reconnect_socket = MagicMock()

        def _fake_my_connect() -> None:
            iface.socket = reconnect_socket

        with (
            patch.object(iface, "myConnect", side_effect=_fake_my_connect),
            patch.object(iface, "_start_config") as mock_start_config,
        ):
            assert iface._attempt_reconnect() is True
        mock_start_config.assert_called_once()
        assert iface._reconnect_attempts == 0
        assert iface.socket is reconnect_socket
        assert iface._reconnect_attempt_in_progress is False
    finally:
        iface.close()
