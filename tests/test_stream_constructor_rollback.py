"""Failed stream construction retires native resources before propagating."""

import socket
from typing import Any
from unittest.mock import MagicMock

import pytest

from meshtastic.mesh_interface import MeshInterface
from meshtastic.tcp_interface import TCPInterface

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("phase", ["connected", "configured"])
@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit, RuntimeError])
def test_tcp_constructor_failure_closes_socket_and_reader(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    error_type: type[BaseException],
) -> None:
    """Both constructor waits release their socket and reader on every failure."""
    sock, peer = socket.socketpair()
    instances: list[TCPInterface] = []
    failure = error_type("construction interrupted")

    def connect(interface: TCPInterface) -> None:
        interface.socket = sock
        instances.append(interface)

    def wait_connected(_interface: MeshInterface, *_args: Any, **_kwargs: Any) -> None:
        if phase == "connected":
            raise failure

    def wait_configured(_interface: MeshInterface, *_args: Any, **_kwargs: Any) -> None:
        if phase == "configured":
            raise failure

    monkeypatch.setattr(TCPInterface, "myConnect", connect)
    monkeypatch.setattr(MeshInterface, "_start_config", lambda _self: None)
    monkeypatch.setattr(MeshInterface, "_wait_connected", wait_connected)
    monkeypatch.setattr(MeshInterface, "waitForConfig", wait_configured)
    try:
        with pytest.raises(error_type) as error:
            TCPInterface("127.0.0.1")

        assert error.value is failure
        assert len(instances) == 1
        assert instances[0].socket is None
        assert sock.fileno() == -1
        assert not instances[0]._rxThread.is_alive()
        assert instances[0]._wantExit
    finally:
        for instance in instances:
            instance.close()
        sock.close()
        peer.close()


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
def test_tcp_native_socket_setup_interrupt_closes_unadopted_handle(
    monkeypatch: pytest.MonkeyPatch, error_type: type[BaseException]
) -> None:
    """Interruptions before socket adoption close the handle without base init."""
    native = MagicMock()
    failure = error_type("native socket setup interrupted")
    native.settimeout.side_effect = failure
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_kw: native)

    with pytest.raises(error_type) as error:
        TCPInterface("127.0.0.1")

    assert error.value is failure
    native.close.assert_called_once()


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
def test_tcp_socket_adoption_lock_interrupt_closes_unadopted_handle(
    monkeypatch: pytest.MonkeyPatch, error_type: type[BaseException]
) -> None:
    """Waiting for socket adoption cannot retain an unowned native handle."""
    native = MagicMock()
    failure = error_type("socket adoption interrupted")

    class InterruptedLock:
        def __enter__(self) -> None:
            raise failure

        def __exit__(self, *_args: object) -> None:
            pass

    interface = TCPInterface("127.0.0.1", noProto=True, connectNow=False)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(socket, "create_connection", lambda *_a, **_kw: native)
            patch.setattr(interface, "_reconnect_lock", InterruptedLock())
            with pytest.raises(error_type) as error:
                interface.myConnect()
        assert error.value is failure
        assert interface.socket is None
        native.close.assert_called_once()
    finally:
        interface.close()


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
def test_tcp_interrupt_after_socket_adoption_retires_partial_constructor(
    monkeypatch: pytest.MonkeyPatch, error_type: type[BaseException]
) -> None:
    """The concrete constructor owns sockets acquired before shared base setup."""
    native = MagicMock()
    failure = error_type("adopted socket interrupted")
    instances: list[TCPInterface] = []

    def connect(interface: TCPInterface) -> None:
        interface.socket = native
        instances.append(interface)
        raise failure

    monkeypatch.setattr(TCPInterface, "myConnect", connect)
    with pytest.raises(error_type) as error:
        TCPInterface("127.0.0.1")

    assert error.value is failure
    assert instances[0].socket is None
    native.close.assert_called_once()
