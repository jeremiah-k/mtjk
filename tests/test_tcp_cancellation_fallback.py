"""Cancellable TCP connections try every usable resolved address."""

import errno
import socket
from unittest.mock import MagicMock, call

import pytest

from meshtastic.cancellation import _connect_socket

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("failure_phase", ["socket", "connect"])
def test_tcp_connect_tries_ipv4_after_unusable_ipv6(
    monkeypatch: pytest.MonkeyPatch, failure_phase: str
) -> None:
    """An unsupported family or failed handshake cannot hide a usable address."""
    ipv6_target = ("::1", 4403, 0, 0)
    ipv4_target = ("127.0.0.1", 4403)
    monkeypatch.setattr(
        "meshtastic.cancellation._socket.getaddrinfo",
        lambda *_args: [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ipv6_target),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ipv4_target),
        ],
    )
    failed = MagicMock()
    failed.connect_ex.return_value = errno.ECONNREFUSED
    connected = MagicMock()
    connected.connect_ex.return_value = 0
    factory = MagicMock(
        side_effect=[
            (
                OSError(errno.EAFNOSUPPORT, "IPv6 unavailable")
                if failure_phase == "socket"
                else failed
            ),
            connected,
        ]
    )
    monkeypatch.setattr("meshtastic.cancellation._socket.socket", factory)

    assert _connect_socket(("localhost", 4403), 1) is connected

    assert factory.call_args_list == [
        call(socket.AF_INET6, socket.SOCK_STREAM, 6),
        call(socket.AF_INET, socket.SOCK_STREAM, 6),
    ]
    connected.connect_ex.assert_called_once_with(ipv4_target)
    connected.close.assert_not_called()
    if failure_phase == "connect":
        failed.connect_ex.assert_called_once_with(ipv6_target)
        failed.close.assert_called_once()


def test_tcp_connect_reports_last_error_when_no_socket_can_be_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exhausting address families preserves the final native socket error."""
    monkeypatch.setattr(
        "meshtastic.cancellation._socket.getaddrinfo",
        lambda *_args: [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 4403, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 4403)),
        ],
    )
    last_error = OSError(errno.EMFILE, "No file descriptors available")
    factory = MagicMock(
        side_effect=[OSError(errno.EAFNOSUPPORT, "IPv6 unavailable"), last_error]
    )
    monkeypatch.setattr("meshtastic.cancellation._socket.socket", factory)

    with pytest.raises(OSError) as error:
        _connect_socket(("localhost", 4403), 1)

    assert error.value is last_error
    assert factory.call_count == 2
