"""Unit tests for the interpreter-exit close registry of StreamInterface."""

import atexit
import gc
import logging
import subprocess
import sys
import textwrap
import threading
import weakref
from collections.abc import Generator
from unittest.mock import MagicMock, mock_open, patch

import pytest

from .. import stream_interface as stream_interface_module
from ..serial_interface import SerialInterface
from ..stream_interface import (
    StreamInterface,
    _add_open_stream_interface,
    _close_open_stream_interfaces_at_exit,
    _open_stream_interfaces,
    _untrack_open_stream_interface,
)
from ..tcp_interface import TCPInterface


def _connected_test_interface() -> StreamInterface:
    """Build a noProto StreamInterface with a mock stream and connect it."""
    iface = StreamInterface(noProto=True, connectNow=False)
    stream = MagicMock()
    stream.read.return_value = b""
    iface.stream = stream
    iface.connect()
    return iface


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_connect_tracks_interface_and_close_discards() -> None:
    """A successful connect registers the interface; close() removes it."""
    iface = _connected_test_interface()
    try:
        assert iface in _open_stream_interfaces
    finally:
        iface.close()

    assert iface not in _open_stream_interfaces


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_cli_style_teardown_leaves_registry_empty() -> None:
    """The CLI path closes its interface, so the exit registry ends empty."""
    iface = _connected_test_interface()
    iface.close()

    assert list(_open_stream_interfaces) == []


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_never_connected_interface_is_not_tracked() -> None:
    """An interface that never connected is absent from the exit registry."""
    iface = StreamInterface(noProto=True, connectNow=False)
    try:
        assert iface not in _open_stream_interfaces
        assert list(_open_stream_interfaces) == []
    finally:
        iface.close()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_exit_hook_closes_leftover_interfaces(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The exit hook closes interfaces still registered and leaves none behind."""
    stream_one = MagicMock()
    stream_one.read.return_value = b""
    stream_two = MagicMock()
    stream_two.read.return_value = b""

    def connected_interface(stream: MagicMock) -> StreamInterface:
        iface = StreamInterface(noProto=True, connectNow=False)
        iface.stream = stream
        iface.connect()
        return iface

    iface_one = connected_interface(stream_one)
    iface_two = connected_interface(stream_two)
    try:
        with caplog.at_level(logging.WARNING, logger="meshtastic.stream_interface"):
            _close_open_stream_interfaces_at_exit()

        assert list(_open_stream_interfaces) == []
        stream_one.close.assert_called()
        stream_two.close.assert_called()
        assert "Timed out after" not in caplog.text
    finally:
        for iface in (iface_one, iface_two):
            iface.close()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_exit_hook_is_bounded_when_close_blocks(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A blocked close() cannot stall the exit hook beyond the join budget."""
    iface = _connected_test_interface()
    release_close = threading.Event()
    close_finished = threading.Event()

    def blocking_close() -> None:
        try:
            release_close.wait(timeout=5.0)
        finally:
            close_finished.set()

    monkeypatch.setattr(iface, "close", blocking_close)
    monkeypatch.setattr(stream_interface_module, "EXIT_CLOSE_JOIN_BUDGET_SECONDS", 0.2)
    try:
        with caplog.at_level(logging.WARNING, logger="meshtastic.stream_interface"):
            _close_open_stream_interfaces_at_exit()
        assert "Timed out after" in caplog.text
        assert "continuing shutdown" in caplog.text
    finally:
        release_close.set()
        try:
            assert close_finished.wait(timeout=5.0)
        finally:
            StreamInterface.close(iface)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_exit_hook_swallows_close_exceptions(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing close() is swallowed with a debug log, never propagated."""

    def failing_close() -> None:
        raise RuntimeError("close exploded")

    iface = _connected_test_interface()
    monkeypatch.setattr(iface, "close", failing_close)
    try:
        with caplog.at_level(logging.DEBUG, logger="meshtastic.stream_interface"):
            _close_open_stream_interfaces_at_exit()
        assert "Exit-time close of" in caplog.text
        assert "close exploded" in caplog.text
    finally:
        StreamInterface.close(iface)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_weakset_drops_garbage_collected_interface() -> None:
    """The registry holds weak references and cannot be leaked by an interface."""
    iface = StreamInterface(noProto=True, connectNow=False)
    _add_open_stream_interface(iface)
    assert iface in _open_stream_interfaces

    reference = weakref.ref(iface)
    del iface
    gc.collect()

    assert reference() is None
    assert list(_open_stream_interfaces) == []


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_exit_hook_registered_once_and_lazily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The atexit hook is registered exactly once, on the first tracked interface."""
    register_calls: list[object] = []
    monkeypatch.setattr(
        stream_interface_module._exit_close_hook_state, "registered", False
    )

    def spy_register(func: object) -> None:
        register_calls.append(func)

    monkeypatch.setattr(atexit, "register", spy_register)

    iface_one = StreamInterface(noProto=True, connectNow=False)
    iface_two = StreamInterface(noProto=True, connectNow=False)
    try:
        _track_open_stream_interface(iface_one)
        assert len(register_calls) == 1

        _track_open_stream_interface(iface_two)
        assert len(register_calls) == 1
    finally:
        for iface in (iface_one, iface_two):
            _untrack_open_stream_interface(iface)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_exit_hook_registration_failure_does_not_poison_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registration failure keeps the hook unclaimed and permits a later retry."""
    state = stream_interface_module._exit_close_hook_state
    monkeypatch.setattr(state, "registered", False)
    failures: list[object] = []

    def refused(_callback: object) -> None:
        failures.append(_callback)
        raise RuntimeError("interpreter is shutting down")

    monkeypatch.setattr(atexit, "register", refused)
    stream_interface_module._register_exit_close_hook_once()
    assert not state.registered
    assert len(failures) == 1

    successful: list[object] = []
    monkeypatch.setattr(atexit, "register", successful.append)
    stream_interface_module._register_exit_close_hook_once()
    assert state.registered
    assert successful == [stream_interface_module._close_open_stream_interfaces_at_exit]


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_tcp_interface_connect_tracks_and_close_discards() -> None:
    """A real TCPInterface registers on connect and its own close discards it."""
    with (
        patch("socket.socket"),
        patch("meshtastic.tcp_interface.socket.create_connection") as mock_connect,
    ):
        mock_connect.return_value = MagicMock()
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=True)
        try:
            assert iface in _open_stream_interfaces
        finally:
            iface.close()

    assert iface not in _open_stream_interfaces


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_serial_interface_connect_tracks_and_close_discards() -> None:
    """A real SerialInterface registers on connect and its own close discards it."""
    with (
        patch("os.path.exists", return_value=True),
        patch("time.sleep"),
        patch("meshtastic.serial_interface.SerialInterface._clear_hupcl_on_fd"),
        patch("meshtastic.serial_interface.SerialInterface._set_hupcl_with_termios"),
        patch("builtins.open", new_callable=mock_open, read_data="data"),
        patch("serial.Serial"),
    ):
        iface = SerialInterface(devPath="/dev/ttyUSB0", noProto=True)
        try:
            assert iface in _open_stream_interfaces
        finally:
            iface.close()

    assert iface not in _open_stream_interfaces


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_exit_hook_skips_caller_closed_tcp_interface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A TCP interface closed by the caller is not closed again by the exit hook."""
    with (
        patch("socket.socket"),
        patch("meshtastic.tcp_interface.socket.create_connection") as mock_connect,
    ):
        mock_socket = MagicMock()
        mock_connect.return_value = mock_socket
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=True)
        iface.close()
        assert iface not in _open_stream_interfaces

        close_count_after_caller = mock_socket.close.call_count
        shutdown_count_after_caller = mock_socket.shutdown.call_count
        hook_close_calls: list[TCPInterface] = []
        monkeypatch.setattr(iface, "close", lambda: hook_close_calls.append(iface))

        _close_open_stream_interfaces_at_exit()

    assert hook_close_calls == []
    assert mock_socket.close.call_count == close_count_after_caller
    assert mock_socket.shutdown.call_count == shutdown_count_after_caller


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_exit_hook_survives_worker_start_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A thread-start failure is warned about and never raises out of the hook."""
    iface = _connected_test_interface()
    try:

        def exploding_start(self: threading.Thread) -> None:
            raise RuntimeError("cannot start thread")

        monkeypatch.setattr(threading.Thread, "start", exploding_start)
        with caplog.at_level(logging.WARNING, logger="meshtastic.stream_interface"):
            _close_open_stream_interfaces_at_exit()

        assert "Failed to start" in caplog.text
        assert "cannot start thread" in caplog.text
        assert iface in _open_stream_interfaces
    finally:
        StreamInterface.close(iface)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_close_racing_in_flight_connect_leaves_registry_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connect() still in flight when close() finishes must not re-register.

    Deterministic interleaving: connect() is parked inside _start_config on
    an Event while close() runs to completion on the main thread; only then
    is connect() released, so the final tracking decision is made strictly
    after the shutdown intent was published. No timing window is involved.
    """
    iface = StreamInterface(noProto=True, connectNow=False)
    iface.stream = MagicMock()
    iface.stream.read.return_value = b""
    connect_entered_config = threading.Event()
    release_connect = threading.Event()

    def blocking_start_config() -> None:
        connect_entered_config.set()
        release_connect.wait(timeout=10.0)

    monkeypatch.setattr(iface, "_start_config", blocking_start_config)

    connect_thread = threading.Thread(target=iface.connect, daemon=True)
    connect_thread.start()
    assert connect_entered_config.wait(timeout=5.0)

    iface.close()
    release_connect.set()
    connect_thread.join(timeout=10.0)

    assert not connect_thread.is_alive()
    assert iface not in _open_stream_interfaces


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_exit_hook_snapshot_survives_concurrent_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registry mutation racing the hook's snapshot cannot break the close.

    Deterministic construction: the instrumented registry iterates the live
    underlying set (the pre-3.14 WeakSet.__iter__ shape; 3.14+ copies
    self.data and hides the race, so the test forces the unsafe form to pin
    the guarantee on every supported interpreter). The snapshot's iterator is
    suspended after its first item - underlying set iterator already live -
    until the main thread releases it, so a mutator thread's discard provably
    lands strictly between two next() calls on the unlocked implementation
    and the hook raises. The fixed implementation holds the registry leaf
    lock for the whole snapshot, so the mutator stays parked on that lock,
    the wait for it times out, and the outcome below is schedule-independent.
    """

    def connected_interface() -> tuple[StreamInterface, MagicMock]:
        iface = StreamInterface(noProto=True, connectNow=False)
        stream = MagicMock()
        stream.read.return_value = b""
        iface.stream = stream
        iface.connect()
        return iface, stream

    iface_one, stream_one = connected_interface()
    iface_two, stream_two = connected_interface()
    iter_started = threading.Event()
    release_iter = threading.Event()
    discard_landed = threading.Event()

    class SuspendedLiveIterationWeakSet(weakref.WeakSet["StreamInterface"]):
        """WeakSet iterating the live data set, parked after the first item."""

        data: set["weakref.ref[StreamInterface]"]

        def __iter__(self) -> Generator["StreamInterface", None, None]:
            def live_items() -> Generator["StreamInterface", None, None]:
                for itemref in self.data:
                    item = itemref()
                    if item is not None:
                        yield item

            inner = live_items()
            iter_started.set()
            first = next(inner)
            yield first
            release_iter.wait(timeout=10.0)
            yield from inner

    instrumented = SuspendedLiveIterationWeakSet([iface_one, iface_two])
    monkeypatch.setattr(
        stream_interface_module, "_open_stream_interfaces", instrumented
    )

    def mutate_registry() -> None:
        _untrack_open_stream_interface(iface_one)
        discard_landed.set()

    errors: list[Exception] = []

    def run_hook() -> None:
        try:
            _close_open_stream_interfaces_at_exit()
        except Exception as exc:  # captured on purpose; re-raised by asserts
            errors.append(exc)

    hook_thread = threading.Thread(target=run_hook, daemon=True)
    mutator_thread = threading.Thread(target=mutate_registry, daemon=True)
    hook_thread.start()
    assert iter_started.wait(timeout=5.0)
    mutator_thread.start()
    discard_landed.wait(timeout=0.5)
    release_iter.set()
    hook_thread.join(timeout=10.0)
    mutator_thread.join(timeout=10.0)

    assert not hook_thread.is_alive()
    assert not mutator_thread.is_alive()
    assert errors == []
    stream_one.close.assert_called()
    stream_two.close.assert_called()


@pytest.mark.unit
def test_normal_interpreter_exit_sends_disconnect_and_closes_stream() -> None:
    """Exercise the registered hook during real interpreter shutdown."""
    code = textwrap.dedent("""
        import io

        from meshtastic.protobuf import mesh_pb2
        from meshtastic.stream_interface import START1, START2, StreamInterface

        payload = mesh_pb2.ToRadio(disconnect=True).SerializeToString()
        disconnect = bytes([START1, START2, 0, len(payload)]) + payload

        class ProbeStream(io.BytesIO):
            def close(self):
                if not self.closed:
                    if self.getvalue().endswith(disconnect):
                        print('disconnect-written', flush=True)
                    print('stream-closed', flush=True)
                super().close()

        interface = StreamInterface(noProto=True, connectNow=False)
        interface.stream = ProbeStream()
        interface.connect()
        # Skip the protocol handshake, but exercise its real disconnect write.
        interface.noProto = False
        print('body-finished', flush=True)
        """)
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "body-finished",
        "disconnect-written",
        "stream-closed",
    ], result.stderr
    assert "Failed to start" not in result.stderr
    assert "Timed out after" not in result.stderr


@pytest.mark.unit
def test_import_does_not_register_exit_hook() -> None:
    """Importing the library in a fresh interpreter registers no atexit hook."""
    code = (
        "import meshtastic.stream_interface as si; "
        "assert not si._exit_close_hook_state.registered, "
        "'hook registered on import'; "
        "print('hook-not-registered')"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "hook-not-registered" in result.stdout
