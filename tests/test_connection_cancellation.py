"""Caller-owned events retire construction without reaching into partial objects."""

from __future__ import annotations

import asyncio
import errno
import socket
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from meshtastic.cancellation import ConnectionCancelledError, _current_event
from meshtastic.interfaces.ble.interface import BLEInterface
from meshtastic.mesh_interface import MeshInterface
from meshtastic.serial_interface import SerialInterface
from meshtastic.stream_interface import StreamInterface
from meshtastic.tcp_interface import TCPInterface

pytestmark = pytest.mark.unit


def _cancel_construction(
    construct: Callable[[threading.Event], object], entered: threading.Event
) -> ConnectionCancelledError:
    """Cancel after a witnessed blocking phase and join the actual SDK caller."""
    event = threading.Event()
    errors: list[BaseException] = []

    def run() -> None:
        try:
            construct(event)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert entered.wait(3), "construction never reached the blocking phase"
    assert _current_event() is None
    event.set()
    thread.join(5)
    assert not thread.is_alive(), "SDK constructor ignored cancellation"
    assert len(errors) == 1 and isinstance(errors[0], ConnectionCancelledError), errors
    return errors[0]


@pytest.mark.parametrize(
    "factory",
    [
        lambda event: MeshInterface(cancelEvent=event),
        lambda event: StreamInterface(cancelEvent=event),
        lambda event: TCPInterface("invalid.test", cancelEvent=event),
        lambda event: SerialInterface("/not-a-device", cancelEvent=event),
        lambda event: BLEInterface("AA:BB:CC:DD:EE:FF", cancelEvent=event),
    ],
)
def test_pre_cancelled_constructor_never_opens_transport(
    factory: Callable[[threading.Event], object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancelled request is rejected before DNS, serial discovery or BLE setup."""
    event = threading.Event()
    event.set()
    forbidden = MagicMock(side_effect=AssertionError("Pre-cancelled transport opened"))
    monkeypatch.setattr("socket.getaddrinfo", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    monkeypatch.setattr("meshtastic.serial_interface.serial.Serial", forbidden)
    monkeypatch.setattr(
        "meshtastic.serial_interface.serial.tools.list_ports.comports", forbidden
    )
    monkeypatch.setattr("meshtastic.interfaces.ble.client.BleakRootClient", forbidden)
    with pytest.raises(ConnectionCancelledError):
        factory(event)
    # The constructor wrapper can convert a backend exception to cancellation;
    # assert the call history as well, rather than relying on that exception.
    forbidden.assert_not_called()
    assert _current_event() is None


def test_tcp_socket_connect_is_interruptible(monkeypatch: pytest.MonkeyPatch) -> None:
    """An in-progress socket cannot retain the whole configured connect timeout."""
    entered = threading.Event()
    sock = MagicMock()
    sock.connect_ex.return_value = errno.EINPROGRESS
    selector = MagicMock()

    def select(timeout: float) -> list[object]:
        entered.set()
        threading.Event().wait(timeout)
        return []

    selector.select.side_effect = select
    selector.__enter__.return_value = selector
    monkeypatch.setattr(
        "meshtastic.cancellation._socket.getaddrinfo",
        lambda *_args: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 12345))
        ],
    )
    monkeypatch.setattr("meshtastic.cancellation._socket.socket", lambda *_args: sock)
    monkeypatch.setattr(
        "meshtastic.cancellation._selectors.DefaultSelector", lambda: selector
    )
    _cancel_construction(
        lambda event: TCPInterface("127.0.0.1", connectTimeout=60, cancelEvent=event),
        entered,
    )
    sock.close.assert_called_once()
    assert not sock.setblocking.call_args_list[-1].args[0]


def test_tcp_configuration_wait_releases_socket_and_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real loopback I/O is retired when a peer accepts but never sends config."""
    entered = threading.Event()
    instances: list[TCPInterface] = []
    original = TCPInterface.myConnect
    wait_connected = MeshInterface._wait_connected
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5)

    def connect(self: TCPInterface) -> None:
        original(self)
        instances.append(self)

    def wait(self: MeshInterface, timeout: float = 30.0) -> None:
        entered.set()
        wait_connected(self, timeout)

    monkeypatch.setattr(MeshInterface, "_wait_connected", wait)
    monkeypatch.setattr(TCPInterface, "myConnect", connect)
    monkeypatch.setattr(MeshInterface, "_start_config", lambda self: None)
    try:
        _cancel_construction(
            lambda event: TCPInterface(
                "127.0.0.1",
                portNumber=listener.getsockname()[1],
                timeout=60,
                cancelEvent=event,
            ),
            entered,
        )
        assert len(instances) == 1
        assert instances[0].socket is None
        assert not instances[0]._rxThread.is_alive()
    finally:
        listener.close()
        for instance in instances:
            instance.close()


@pytest.mark.parametrize("discovery", [False, True])
def test_ble_native_future_is_cancelled_and_cleanup_runs(
    monkeypatch: pytest.MonkeyPatch, discovery: bool
) -> None:
    """Pending connect/discovery coroutines are cancelled and their client closes."""
    from meshtastic.interfaces.ble.client import BLEClient

    entered, cancelled, disconnected = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    clients: list[BLEClient] = []

    async def pending(**_kwargs: Any) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    class NativeClient:
        address = "AA:BB:CC:DD:EE:FF"
        is_connected = False

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            """Create a transport-shaped client without using system Bluetooth."""

        async def disconnect(self) -> None:
            """Witness native cleanup after the construction event is set."""
            disconnected.set()

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.client.BleakRootClient", NativeClient
    )

    def connect(self: BLEInterface, *_args: Any, **_kwargs: Any) -> BLEClient:
        client = BLEClient("AA:BB:CC:DD:EE:FF")
        clients.append(client)
        self.client = client
        if discovery:
            client.discover(timeout=60)
        else:
            client._async_await(pending(), timeout=60)
        return client

    monkeypatch.setattr(
        BLEInterface, "_start_receive_thread", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(BLEInterface, "connect", connect)
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.client.BleakScanner.discover", pending
    )
    _cancel_construction(
        lambda event: BLEInterface("AA:BB:CC:DD:EE:FF", timeout=60, cancelEvent=event),
        entered,
    )
    assert cancelled.wait(2)
    assert disconnected.is_set()
    assert len(clients) == 1 and clients[0]._closed
    assert not clients[0]._pending_futures


def test_cancel_during_serial_open_releases_native_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation after driver open cannot strand its exclusive port lease."""
    path = tmp_path / "serial"
    path.touch()
    event = threading.Event()
    stream = MagicMock()
    stream.open.side_effect = event.set
    monkeypatch.setattr(
        "meshtastic.serial_interface.serial.Serial", lambda **kwargs: stream
    )
    with pytest.raises(ConnectionCancelledError):
        SerialInterface(str(path), cancelEvent=event)
    stream.close.assert_called_once()


def test_event_is_not_retained_by_a_connected_interface() -> None:
    """Cancelling a completed construction cannot poison later SDK operations."""
    event = threading.Event()
    interface = MeshInterface(noProto=True, cancelEvent=event)
    try:
        event.set()
        interface._wait_connected()
        assert _current_event() is None
    finally:
        interface.close()


def test_invalid_event_is_rejected_before_transport_open() -> None:
    """An arbitrary truthy object is not treated as a cancellation signal."""
    with pytest.raises(TypeError, match="threading.Event"):
        TCPInterface("invalid.test", cancelEvent=object())  # type: ignore[arg-type]


def test_cancel_while_waiting_for_shared_ble_address_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second constructor can cancel without waiting for an unrelated owner."""
    from collections.abc import Iterator
    from contextlib import contextmanager

    from meshtastic.cancellation import _acquire_lock
    from meshtastic.interfaces.ble import gating

    entered = threading.Event()

    @contextmanager
    def witnessed(lock: Any) -> Iterator[None]:
        entered.set()
        with _acquire_lock(lock):
            yield

    monkeypatch.setattr("meshtastic.interfaces.ble.interface._acquire_lock", witnessed)
    monkeypatch.setattr(
        BLEInterface, "_start_receive_thread", lambda *args, **kwargs: None
    )
    native = MagicMock(side_effect=AssertionError("address reservation was bypassed"))
    monkeypatch.setattr(BLEInterface, "_establish_and_update_client", native)
    with gating._addr_lock_context("aabbccddeeff") as lock, lock:
        _cancel_construction(
            lambda event: BLEInterface("AA:BB:CC:DD:EE:FF", cancelEvent=event), entered
        )


def test_async_host_can_exit_after_cancelling_constructor_thread() -> None:
    """Cancelling to_thread plus its SDK event permits default-executor shutdown."""
    import subprocess
    import sys

    script = """
import asyncio, socket, threading
from unittest.mock import patch
from meshtastic.mesh_interface import MeshInterface
from meshtastic.tcp_interface import TCPInterface
entered = threading.Event()
cancel = threading.Event()
instances = []
listener = socket.socket()
listener.bind(("127.0.0.1", 0))
listener.listen(1)
wait_connected = MeshInterface._wait_connected

def wait(self, timeout=30):
    instances.append(self)
    entered.set()
    return wait_connected(self, timeout)

async def run():
    task = asyncio.create_task(asyncio.to_thread(
        TCPInterface, "127.0.0.1", portNumber=listener.getsockname()[1],
        timeout=60, cancelEvent=cancel))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            cancel.set()
    finally:
        cancel.set()

try:
    with patch.object(MeshInterface, "_wait_connected", wait), patch.object(MeshInterface, "_start_config", lambda self: None):
        asyncio.run(run())
    assert len(instances) == 1
    assert instances[0].socket is None
    assert not instances[0]._rxThread.is_alive()
finally:
    listener.close()
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "interface",
    [MeshInterface, StreamInterface, TCPInterface, SerialInterface, BLEInterface],
)
def test_constructor_cancellation_is_discoverable_by_embedding_hosts(
    interface: type[MeshInterface],
) -> None:
    """Signature inspection can detect support without constructing a transport."""
    import inspect

    parameter = inspect.signature(interface).parameters["cancelEvent"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is None


def test_cancellation_error_retains_type_and_message_across_process_serialization() -> (
    None
):
    """Standard executor exception transport preserves the cancellation outcome."""
    import pickle

    error = ConnectionCancelledError()
    restored = pickle.loads(pickle.dumps(error))
    assert type(restored) is ConnectionCancelledError
    assert restored.args == error.args
    assert restored.message == error.message
    assert isinstance(restored, MeshInterface.MeshInterfaceError)
