"""Tests for the BLE interface module - Advanced functionality."""

import logging
import threading
import time
import types
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import ExitStack, contextmanager
from queue import Queue
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from bleak.exc import BleakError

# Import meshtastic modules for use in tests
import meshtastic.interfaces.ble.interface as ble_mod
import meshtastic.mesh_interface as mesh_iface_module
from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.constants import (
    FROMNUM_UUID,
    FROMRADIO_UUID,
    LEGACY_LOGRADIO_UUID,
    LOGRADIO_UUID,
    BLEConfig,
)
from meshtastic.interfaces.ble.interface import BLEInterface
from meshtastic.protobuf import mesh_pb2

# Import common fixtures
from tests.test_ble_interface_fixtures import DummyClient, _build_interface

pytestmark = pytest.mark.unit


def _get_connect_stub_calls(iface: BLEInterface) -> list[str | None]:
    """Retrieve the test-only list of addresses recorded for BLEInterface.connect calls.

    Parameters
    ----------
    iface : BLEInterface
        Interface instance that may expose a `_connect_stub_calls` attribute used by tests.

    Returns
    -------
    list[str | None]
        Recorded addresses for each connect call; `None` represents a connect attempt without an address. Returns an empty list if the attribute is not present.
    """
    connect_calls, _ = _snapshot_connect_stub_state(iface)
    return connect_calls


def _get_connect_stub_kwargs(iface: BLEInterface) -> list[dict[str, object]]:
    """Retrieve the test-only list of keyword args recorded for connect calls.

    Parameters
    ----------
    iface : BLEInterface
        Interface instance that may expose a `_connect_stub_kwargs` attribute used by tests.

    Returns
    -------
    list[dict[str, object]]
        Recorded keyword-argument dictionaries for each connect call. Returns an empty list if the attribute is not present.
    """
    _, connect_kwargs = _snapshot_connect_stub_state(iface)
    return connect_kwargs


def _snapshot_connect_stub_state(
    iface: BLEInterface,
) -> tuple[list[str | None], list[dict[str, object]]]:
    """Return a lock-consistent snapshot of recorded connect addresses and kwargs."""
    with iface._state_lock:
        return (
            list(cast(list[str | None], getattr(iface, "_connect_stub_calls", []))),
            list(
                cast(
                    list[dict[str, object]],
                    getattr(iface, "_connect_stub_kwargs", []),
                )
            ),
        )


def _get_stress_test_clients(iface: BLEInterface) -> list[object]:
    """Retrieve the stress-test clients created by the patched connect stub.

    Parameters
    ----------
    iface : BLEInterface
        Interface instance that may expose a `_stress_test_clients` attribute used by tests.

    Returns
    -------
    list[object]
        Created stress-test client objects in creation order. Returns an empty list if the attribute is not present.
    """
    with iface._state_lock:
        return list(cast(list[object], getattr(iface, "_stress_test_clients", [])))


def _get_stress_test_attempts(iface: BLEInterface) -> list[object]:
    """Retrieve reconnect attempts recorded before connect completion.

    Parameters
    ----------
    iface : BLEInterface
        Interface instance that may expose a `_stress_test_attempts` attribute used by tests.

    Returns
    -------
    list[object]
        Attempt objects recorded before connect completion. Returns an empty list if the attribute is not present.
    """
    with iface._state_lock:
        return list(cast(list[object], getattr(iface, "_stress_test_attempts", [])))


def _latest_successful_stress_attempt(
    attempts: Sequence[object],
    successful_clients: Sequence[object],
) -> object | None:
    """Return the highest-order reconnect attempt that completed successfully.

    Parameters
    ----------
    attempts : Sequence[object]
        Reconnect attempt objects in creation order.
    successful_clients : Sequence[object]
        Successfully connected client objects to match against the attempt list.

    Returns
    -------
    object | None
        The latest successful reconnect attempt object, or `None` when no attempt completed successfully.
    """
    successful_ids = {id(client) for client in successful_clients}
    for attempt in reversed(attempts):
        if id(attempt) in successful_ids:
            return attempt
    return None


def _make_fake_future(exception_to_raise: Exception) -> Any:
    """Create a fake Future object that raises the provided exception from result()."""

    class _FakeFuture:
        def __init__(self) -> None:
            self.cancelled = False
            self.coro = None
            self.callbacks: list[Callable[..., Any]] = []

        def result(self, _timeout: float | None = None) -> Any:
            raise exception_to_raise

        def cancel(self) -> None:
            self.cancelled = True

        def add_done_callback(self, callback: Callable[..., Any]) -> None:
            self.callbacks.append(callback)

    return _FakeFuture()


def _bind_coro_to_future(fake_future: Any) -> Callable[[Any], Any]:
    """Build a fake _async_run callback that records the coroutine on fake_future."""

    def _fake_async_run(coro: Any) -> Any:
        fake_future.coro = coro
        return fake_future

    return _fake_async_run


def test_log_notification_registration_missing_characteristics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that log notification registration handles missing characteristics gracefully."""
    # UUID constants already imported at top as ble_mod.FROMNUM_UUID, ble_mod.LEGACY_LOGRADIO_UUID, ble_mod.LOGRADIO_UUID

    class MockClientWithoutLogChars(DummyClient):
        """Mock client that doesn't have log characteristics."""

        def __init__(self) -> None:
            """Create a mock BLE client that exposes only the FROMNUM characteristic.

            Attributes
            ----------
            start_notify_calls : list
                Recorded (uuid, handler) tuples passed to start_notify.
            has_characteristic_map : dict
                Mapping of characteristic UUID to bool; contains only `FROMNUM_UUID: True`.
            """
            super().__init__()
            self.start_notify_calls: list[tuple[object, object]] = []
            self.has_characteristic_map = {
                FROMNUM_UUID: True,  # Only have the critical one
            }

        def has_characteristic(self, uuid: str) -> bool:
            """Return whether the client reports a characteristic with the given UUID.

            Parameters
            ----------
            uuid : str
                Characteristic UUID to check in the client's characteristic map.

            Returns
            -------
            bool
                `True` if the UUID is present, `False` otherwise.
            """
            return self.has_characteristic_map.get(uuid, False)

        def start_notify(self, *_args: Any, **_kwargs: Any) -> None:
            """Record a notification registration by appending a (uuid, handler) tuple to self.start_notify_calls.

            If called with at least two positional arguments, the first is treated as the characteristic UUID and the second as the notification handler; any additional positional or keyword arguments are ignored.
            """
            # Extract uuid and handler from args if available
            if len(_args) >= 2:
                uuid, handler = _args[0], _args[1]
                self.start_notify_calls.append((uuid, handler))

    client = MockClientWithoutLogChars()
    iface = _build_interface(monkeypatch, client)

    # Call _register_notifications - should not fail even without log characteristics
    iface._register_notifications(client)  # type: ignore[arg-type]

    # Verify that only FROMNUM notification was registered
    registered_uuids = [call[0] for call in client.start_notify_calls]

    assert (
        FROMNUM_UUID in registered_uuids
    ), "FROMNUM notification should still be registered"
    assert (
        LEGACY_LOGRADIO_UUID not in registered_uuids
    ), "Legacy log notification should not be registered when characteristic is missing"
    assert (
        LOGRADIO_UUID not in registered_uuids
    ), "Current log notification should not be registered when characteristic is missing"

    iface.close()


def test_receive_loop_handles_decode_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that the receive loop handles DecodeError gracefully without closing."""
    # logging, threading, and time already imported at top
    # FROMRADIO_UUID already imported at top as ble_mod.FROMRADIO_UUID

    caplog.set_level(logging.WARNING)

    class MockClient(DummyClient):
        """Mock client that returns invalid protobuf data to trigger DecodeError."""

        def read_gatt_char(self, *_args: object, **_kwargs: object) -> bytes:
            """Return raw GATT characteristic bytes for this test client.

            When the requested UUID equals ble_mod.FROMRADIO_UUID, returns malformed protobuf bytes to simulate a decode error; otherwise returns empty bytes.

            Returns
            -------
            bytes
                Malformed protobuf bytes for `ble_mod.FROMRADIO_UUID`, empty bytes otherwise.
            """
            # Extract uuid from args if available
            if _args and _args[0] == FROMRADIO_UUID:
                return b"invalid-protobuf-data"
            return b""

    client = MockClient()
    iface = _build_interface(monkeypatch, client)

    close_called = threading.Event()
    original_close = iface.close

    def mock_close() -> None:
        """Signal that the mock close was invoked and then call the original close callable.

        Sets the `close_called` event to notify waiters and then invokes `original_close`.
        """
        close_called.set()
        original_close()

    monkeypatch.setattr(iface, "close", mock_close)

    # Start the receive thread
    iface._want_receive = True

    # Set up the client
    with iface._state_lock:
        cast(Any, iface).client = client

    # Trigger the receive loop to process the bad data
    iface._read_trigger.set()

    # Give the thread time to run: poll for expected log
    for _ in range(50):
        if "Failed to parse FromRadio packet, discarding:" in caplog.text:
            break
        time.sleep(0.02)

    # Assert that the graceful handling occurred
    assert "Failed to parse FromRadio packet, discarding:" in caplog.text
    assert not close_called.is_set(), "close() should not be called for a DecodeError"

    # Clean up
    iface._want_receive = False
    iface.close()


def test_auto_reconnect_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify BLEInterface schedules an auto-reconnect after a BLE disconnect and preserves connection state and receive-thread behavior.

    This test simulates a disconnection with auto_reconnect enabled and asserts that:
    - close() is not called during auto-reconnect.
    - A disconnect event (connected=False) is published.
    - At least one reconnect attempt occurs and the original client is restored.
    - A connected=True event is published when configuration completes.
    - The internal disconnect notification flag is reset.
    - The receive thread remains active and _want_receive remains True.

    Uses test fixtures for monkeypatching and logging; does not document those fixtures.
    """
    # time and meshtastic.mesh_interface already imported at top

    # Track published events
    published_events: list[tuple[Any, dict[str, Any]]] = []

    def _capture_events(topic: Any, **kwargs: Any) -> None:
        """Record a pub/sub event for test inspection.

        Parameters
        ----------
        topic : Any
            The event topic name.
        **kwargs : Any
            Key/value pairs comprising the event payload.
        """
        published_events.append((topic, kwargs))

    # Create a fresh pub mock for this test
    fresh_pub = types.SimpleNamespace(
        subscribe=lambda *_args, **_kwargs: None,
        sendMessage=_capture_events,
        AUTO_TOPIC=None,
    )
    monkeypatch.setattr(mesh_iface_module, "pub", fresh_pub)
    monkeypatch.setattr(
        "meshtastic.mesh_interface.publishingThread.queueWork",
        lambda callback: callback() if callback else None,
    )
    monkeypatch.setattr(
        "meshtastic.mesh_interface.publishingThread.queue.put_nowait",
        lambda callback: callback() if callback else None,
    )

    # Create a client that can simulate disconnection
    client = DummyClient()

    # Build interface with auto_reconnect=True
    iface = _build_interface(monkeypatch, client)
    iface.auto_reconnect = True
    assert _get_connect_stub_calls(iface) == [
        "dummy"
    ], "Initial connect should occur on instantiation"

    # Track if close() was called
    close_called: list[bool] = []
    original_close = iface.close

    def _track_close() -> None:
        """Mark that close() was invoked and delegate to the preserved original close function.

        Returns
        -------
            The value returned by the preserved original close function.
        """
        close_called.append(True)
        return original_close()

    monkeypatch.setattr(iface, "close", _track_close)

    try:
        # Simulate disconnection by calling _on_ble_disconnect directly
        # This simulates what happens when bleak calls disconnected callback
        disconnect_client = iface.client
        if disconnect_client and hasattr(disconnect_client, "bleak_client"):
            iface._on_ble_disconnect(disconnect_client.bleak_client)  # type: ignore[arg-type]

        # Allow time for auto-reconnect thread to run.
        reconnect_poll_iterations = max(
            1,
            int(BLEConfig.AUTO_RECONNECT_INITIAL_DELAY / 0.01),
        )
        for _ in range(reconnect_poll_iterations):
            if (
                len(_get_connect_stub_calls(iface)) >= 2
                and cast(object, iface.client) is client
            ):
                break
            time.sleep(0.01)

        # Assertions
        # 1. BLEInterface.close() should NOT be called when auto_reconnect=True
        assert (
            len(close_called) == 0
        ), "close() should not be called when auto_reconnect=True"

        # 2. Connection status event should be published with connected=False
        disconnect_events: list[tuple[Any, dict[str, Any]]] = []
        disconnect_deadline = time.monotonic() + 1.0
        first_disconnect_seen_at: float | None = None
        while time.monotonic() < disconnect_deadline:
            disconnect_events = [
                (topic, kw)
                for topic, kw in published_events
                if topic == "meshtastic.connection.status"
                and kw.get("connected") is False
            ]
            if disconnect_events and first_disconnect_seen_at is None:
                first_disconnect_seen_at = time.monotonic()
            if (
                first_disconnect_seen_at is not None
                and time.monotonic() - first_disconnect_seen_at >= 0.05
            ):
                break
            time.sleep(0.005)
        assert (
            len(disconnect_events) == 1
        ), f"Expected exactly one disconnect event, got {len(disconnect_events)}"

        # 3. Auto-reconnect should have attempted a reconnect and restored the client
        assert (
            len(_get_connect_stub_calls(iface)) >= 2
        ), f"Expected at least 2 connect calls, got {len(_get_connect_stub_calls(iface))}"
        assert (
            cast(object, iface.client) is client
        ), "client should be restored after successful auto-reconnect"

        # Simulate config completion to publish connected=True and verify it was emitted
        iface.isConnected.clear()
        monkeypatch.setattr(iface, "_start_heartbeat", lambda: None)
        iface._connected()
        reconnect_events: list[tuple[Any, dict[str, Any]]] = []
        reconnect_deadline = time.monotonic() + 1.0
        first_reconnect_seen_at: float | None = None
        while time.monotonic() < reconnect_deadline:
            reconnect_events = [
                (topic, kw)
                for topic, kw in published_events
                if topic == "meshtastic.connection.status"
                and kw.get("connected") is True
            ]
            if reconnect_events and first_reconnect_seen_at is None:
                first_reconnect_seen_at = time.monotonic()
            if (
                first_reconnect_seen_at is not None
                and time.monotonic() - first_reconnect_seen_at >= 0.05
            ):
                break
            time.sleep(0.005)
        assert (
            len(reconnect_events) == 1
        ), f"Expected exactly one reconnect event, got {len(reconnect_events)}"

        # 4. Ensure disconnect flag resets for future disconnect handling
        assert (
            not iface._disconnect_notified
        ), "_disconnect_notified should reset after auto-reconnect"

        # 5. Receive thread should remain alive, waiting for new connection
        # Check that _want_receive is still True and receive thread is alive
        assert (
            iface._want_receive is True
        ), "_want_receive should remain True for auto_reconnect"
        assert iface._receiveThread is not None, "receive thread should still exist"
        # Wait up to ~1s for receive thread to be alive
        for _ in range(100):
            t = getattr(iface, "_receiveThread", None)
            if t and t.is_alive():
                break
            time.sleep(0.01)
        assert iface._receiveThread is not None, "receive thread should still exist"
        assert iface._receiveThread.is_alive(), "receive thread should remain alive"
    finally:
        iface.auto_reconnect = False
        original_close()


def test_send_to_radio_specific_exceptions(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Verify that _send_to_radio_impl wraps write failures for specific exception types as BLEInterface.BLEError and logs the underlying error.

    This test exercises three failure modes (BleakError, RuntimeError, OSError) by using a mock client that raises the configured exception from write_gatt_char. For each case it asserts that BLEInterface.BLEError is raised and that the log contains the name of the original exception.
    """
    # logging already imported at top
    # BleakError already imported at top as ble_mod.BleakError, BLEInterface

    # Set logging level to DEBUG to capture debug messages
    caplog.set_level(logging.DEBUG)

    class ExceptionClient(DummyClient):
        """Mock client that raises exceptions during write operations."""

        def __init__(self, exception_type: type[Exception]) -> None:
            """Create a test client that raises a specified exception type for BLE operations.

            Parameters
            ----------
            exception_type : type[Exception]
                Exception class that the client's BLE methods will raise when invoked.
            """
            super().__init__()
            self.exception_type = exception_type

        def write_gatt_char(self, *_args: Any, **_kwargs: Any) -> None:
            """Simulate a failing GATT characteristic write.

            Raises
            ------
            Exception
                An instance of `self.exception_type` with the message "write failed".
            """
            raise self.exception_type("write failed")

    # Test BleakError specifically
    client = ExceptionClient(BleakError)
    iface = _build_interface(monkeypatch, client)

    # Create a mock ToRadio message with actual data to ensure it's not empty
    # mesh_pb2 already imported at top

    to_radio = mesh_pb2.ToRadio()
    to_radio.packet.decoded.payload = b"test_data"

    # This should raise BLEInterface.BLEError
    with pytest.raises(BLEInterface.BLEError) as exc_info:
        iface._send_to_radio_impl(to_radio)

    assert "Error writing BLE" in str(exc_info.value)
    assert (
        "Error during write operation: BleakError" in caplog.text
        or "Error during write operation: _StubBleakError" in caplog.text
    )

    # Clear caplog for next test
    caplog.clear()
    iface.close()

    # Test RuntimeError
    client2 = ExceptionClient(RuntimeError)
    iface2 = _build_interface(monkeypatch, client2)

    with pytest.raises(BLEInterface.BLEError) as exc_info:
        iface2._send_to_radio_impl(to_radio)

    assert "Error writing BLE" in str(exc_info.value)
    assert "Error during write operation: RuntimeError" in caplog.text

    # Clear caplog for next test
    caplog.clear()
    iface2.close()

    # Test OSError
    client3 = ExceptionClient(OSError)
    iface3 = _build_interface(monkeypatch, client3)

    with pytest.raises(BLEInterface.BLEError) as exc_info:
        iface3._send_to_radio_impl(to_radio)

    assert "Error writing BLE" in str(exc_info.value)
    assert "Error during write operation: OSError" in caplog.text

    iface3.close()


@pytest.mark.unitslow
def test_rapid_connect_disconnect_stress_test(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test rapid connect/disconnect cycles to validate thread-safety and reconnect logic."""
    # logging, threading, and time already imported at top
    # MagicMock, patch already imported at top

    # BLEClient and BLEInterface already imported at top as ble_mod.BLEClient, ble_mod.BLEInterface

    # Set logging level to DEBUG to capture all messages
    caplog.set_level(logging.DEBUG)

    # Mock device for testing
    mock_device = MagicMock()
    mock_device.address = "00:11:22:33:44:55"
    mock_device.name = "Test Device"

    class BaseMockBleakClient:
        """Shared BLE client stub behavior for stress-test doubles."""

        def __init__(
            self,
            address: str = "00:11:22:33:44:55",
            *,
            is_connected_result: bool = True,
        ) -> None:
            """Initialize the shared BLE test stub state."""
            self.connect_count = 0
            self.disconnect_count = 0
            self.address = address
            self.is_connected_result = is_connected_result
            self._should_fail_connect = False

        def connect(self, *_args: object, **_kwargs: object) -> "BaseMockBleakClient":
            """Simulate a BLE client connection for tests.

            Increments the client's connect_count and returns the client instance. If the client is configured to fail, raises a RuntimeError.

            Returns
            -------
            self
                The mock client instance with connect_count incremented.

            Raises
            ------
            RuntimeError
                If the client is configured to fail connecting.
            """
            if self._should_fail_connect:
                raise RuntimeError("connect fail")  # noqa: TRY003
            self.connect_count += 1
            return self

        def is_connected(self) -> bool:
            """Report whether the mock client is configured as connected."""
            return self.is_connected_result

        def disconnect(self, *_args: object, **_kwargs: object) -> None:
            """Record a disconnect attempt on the mock client.

            Increments the `disconnect_count` attribute by 1. Any positional or keyword arguments are accepted for call-site compatibility and ignored.
            """
            self.disconnect_count += 1

        def start_notify(self, *_args: object, **_kwargs: object) -> None:
            """Accept any positional and keyword arguments and perform no action.

            This stub is a no-op placeholder that intentionally ignores all inputs.
            """

        def stopNotify(self, *_args: object, **_kwargs: object) -> None:
            """Compatibility shim that accepts any arguments and performs no action.

            Provided for API compatibility with BLE client implementations; accepts any positional and keyword arguments and does nothing.
            """

    class MockBleakRootClient(BaseMockBleakClient):
        """Mock BleakRootClient that tracks operations for stress testing."""

    class StressTestClient(BLEClient):
        """Mock client that simulates rapid connect/disconnect cycles."""

        def __init__(self) -> None:  # pylint: disable=super-init-not-called
            # Don't call super().__init__() to avoid creating real event loop
            """Create a mock BLE root client that simulates a Bleak client and connection state for tests.

            Initializes attributes used by tests to emulate connect/disconnect behavior:

            Attributes
            ----------
            bleak_client : MockBleakRootClient
                Mock underlying Bleak client used to emulate BLE operations.
            _should_fail_connect : bool
                If True, simulated connect attempts will fail.
                Placeholder for an event loop to suppress test warnings.
                Placeholder for an event thread to suppress test warnings.
            """
            self.bleak_client = MockBleakRootClient()  # type: ignore[assignment]
            self._should_fail_connect = False
            # Add mock event loop attributes to prevent warnings
            self._eventLoop = None
            self._eventThread = None

        def _delegate_to_bleak(
            self, method_name: str, *args: object, **kwargs: object
        ) -> object:
            """Invoke a method on the backing mock bleak client."""
            bleak_client = cast(Any, self.bleak_client)
            method = getattr(bleak_client, method_name)
            return method(*args, **kwargs)

        def connect(self, *_args: object, **_kwargs: object) -> object:
            """Delegate connection behavior to the shared bleak client stub."""
            bleak_client = cast(Any, self.bleak_client)
            bleak_client._should_fail_connect = self._should_fail_connect
            return self._delegate_to_bleak("connect", *_args, **_kwargs)

        @property
        def is_connected(self) -> bool:  # type: ignore[override]
            """Delegate connection-state queries to the shared bleak client stub."""
            return cast(bool, self._delegate_to_bleak("is_connected"))

        def isConnected(self) -> bool:
            """Compatibility wrapper for the promoted camelCase BLE API."""
            return self.is_connected

        def disconnect(self, *_args: object, **_kwargs: object) -> None:
            """Delegate disconnect behavior to the shared bleak client stub."""
            self._delegate_to_bleak("disconnect", *_args, **_kwargs)

        def start_notify(self, *_args: object, **_kwargs: object) -> None:
            """Delegate notify-start behavior to the shared bleak client stub."""
            self._delegate_to_bleak("start_notify", *_args, **_kwargs)

        def stopNotify(self, *_args: object, **_kwargs: object) -> None:
            """Delegate notify-stop behavior to the shared bleak client stub."""
            self._delegate_to_bleak("stopNotify", *_args, **_kwargs)

        def close(self, timeout: float | None = None) -> None:
            """No-op close method used in tests to avoid interacting with the event loop.

            This intentionally performs no action so that calling `close()` on a mock client does not trigger
            event-loop side effects or errors during unit tests.
            """

    @contextmanager
    def create_interface_with_auto_reconnect() -> (
        Iterator[tuple[BLEInterface, "StressTestClient"]]
    ):
        """Create and yield a BLEInterface configured for stress testing with auto-reconnect enabled.

        Patches BLEInterface.connect so the interface receives a StressTestClient.
        The interface is created with an explicit address to exercise reconnect
        scheduling against a concrete target address. Discovery is bypassed in
        this helper because connect is patched; BLEInterface.scan is intentionally
        left unpatched. Yields a tuple
        `(iface, client)`. On generator exit the interface is closed and all
        patches are undone.

        Yields
        ------
        tuple[BLEInterface, 'StressTestClient']
            (iface, client) where `iface` is the configured BLEInterface and
            `client` is the attached StressTestClient.
        """

        connect_calls: list[str | None] = []
        connect_kwargs: list[dict[str, object]] = []
        attempted_clients: list[StressTestClient] = []
        created_clients: list[StressTestClient] = []

        stack = ExitStack()

        def _patched_connect(
            self: BLEInterface,
            address: str | None = None,
            **kwargs: object,
        ) -> "StressTestClient":
            """Attach a fresh StressTestClient to this BLEInterface for testing and record the connection address.

            Records the attempted connection address in the test's connect_calls
            list, creates a new StressTestClient for this connect attempt, sets
            the interface's client to that fresh instance, clears the
            interface's _disconnect_notified flag, and signals a
            _reconnected_event if present.

            Parameters
            ----------
            address : str | None
                Address used for the connection; appended to the test's connect_calls list. (Default value = None)

            Returns
            -------
            'StressTestClient'
                The client instance attached to the interface.
            """
            new_client = StressTestClient()
            new_client._should_fail_connect = cast(
                bool,
                getattr(self, "_stress_test_should_fail_connect", False),
            )
            with self._state_lock:
                effective_pair = cast(
                    bool | None,
                    kwargs.get("pair", self._last_connect_pair_override),
                )
                effective_connect_timeout = cast(
                    float | None,
                    kwargs.get("connect_timeout", self._last_connect_timeout_override),
                )
                connect_calls.append(address)
                connect_kwargs.append(
                    {
                        "pair": effective_pair,
                        "connect_timeout": effective_connect_timeout,
                    }
                )
                attempted_clients.append(new_client)
            new_client.connect(
                pair=effective_pair,
                connect_timeout=effective_connect_timeout,
            )
            with self._state_lock:
                if (
                    self._state_manager._current_state
                    == cast(Any, ble_mod).ConnectionState.DISCONNECTED
                ):
                    self._state_manager._transition_to(
                        cast(Any, ble_mod).ConnectionState.CONNECTING
                    )
                transitioned = self._state_manager._transition_to(
                    cast(Any, ble_mod).ConnectionState.CONNECTED
                )
                self._last_connect_pair_override = effective_pair
                self._last_connect_timeout_override = effective_connect_timeout
                if transitioned:
                    cast(Any, self).client = new_client
                    created_clients.append(new_client)
                    self._client_publish_pending = False
                    self._disconnect_notified = False
                    if hasattr(self, "_reconnected_event"):
                        self._reconnected_event.set()
            return new_client

        stack.enter_context(patch.object(BLEInterface, "connect", _patched_connect))

        iface = None
        try:
            iface = BLEInterface(
                address=mock_device.address,
                noProto=True,
                auto_reconnect=True,
            )
            cast(Any, iface)._connect_stub_calls = connect_calls
            cast(Any, iface)._connect_stub_kwargs = connect_kwargs
            cast(Any, iface)._stress_test_attempts = attempted_clients
            cast(Any, iface)._stress_test_clients = created_clients
            client = cast("StressTestClient", iface.client)
            yield iface, client
        finally:
            try:
                if iface is not None:
                    try:
                        iface.close()
                    except Exception as e:  # noqa: BLE001 - teardown logging only
                        logging.debug(
                            "Error closing stress-test interface during cleanup: %s: %s",
                            type(e).__name__,
                            e,
                        )
            finally:
                stack.close()

    def _wait_for_latest_stress_client(
        iface: BLEInterface,
        *,
        baseline_attempts: Sequence["StressTestClient"],
        baseline_clients: Sequence["StressTestClient"],
        timeout: float = 2.0,
    ) -> tuple[object | None, list["StressTestClient"], list["StressTestClient"]]:
        """Wait until latest successful reconnect attempt owns iface.client.

        Returns
        -------
        tuple[object | None, list[StressTestClient], list[StressTestClient]]
            The client observed under ``_state_lock`` (captured atomically to
            avoid a TOCTOU race on the caller side), the list of attempted
            clients, and the list of successfully created clients.
        """

        def _snapshot_stress_state() -> (
            tuple[object | None, list["StressTestClient"], list["StressTestClient"]]
        ):
            with iface._state_lock:
                current_client = cast(object | None, iface.client)
                attempts = list(
                    cast(
                        list["StressTestClient"],
                        getattr(iface, "_stress_test_attempts", []),
                    )
                )
                clients = list(
                    cast(
                        list["StressTestClient"],
                        getattr(iface, "_stress_test_clients", []),
                    )
                )
            return current_client, attempts, clients

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            observed_client, observed_attempts, observed_clients = (
                _snapshot_stress_state()
            )
            latest_successful_attempt = _latest_successful_stress_attempt(
                observed_attempts, observed_clients
            )
            if (
                latest_successful_attempt is not None
                and observed_client is latest_successful_attempt
                and all(
                    latest_successful_attempt is not baseline_attempt
                    for baseline_attempt in baseline_attempts
                )
                and all(
                    latest_successful_attempt is not baseline_client
                    for baseline_client in baseline_clients
                )
            ):
                return observed_client, observed_attempts, observed_clients
            time.sleep(0.01)
        final_client, final_attempts, final_clients = _snapshot_stress_state()
        return final_client, final_attempts, final_clients

    # Test 1: Rapid disconnect callbacks
    with create_interface_with_auto_reconnect() as (iface, client):
        iface.connect(mock_device.address, pair=True, connect_timeout=3.5)
        baseline_connect_snapshot, _ = _snapshot_connect_stub_state(iface)
        baseline_connects = len(baseline_connect_snapshot)
        baseline_attempts = list(
            cast(list["StressTestClient"], _get_stress_test_attempts(iface))
        )
        baseline_clients = len(_get_stress_test_clients(iface))
        baseline_client_objects = list(
            cast(list["StressTestClient"], _get_stress_test_clients(iface))
        )
        worker_errors: Queue[Exception] = Queue()

        def simulate_rapid_disconnects() -> None:
            """Simulate a burst of rapid BLE disconnect events against the test interface.

            Triggers ten disconnect callbacks roughly 0.01 seconds apart to exercise the interface's reconnect and disconnect handling during tests.
            """
            for _ in range(10):
                current_client = cast("StressTestClient", iface.client)
                if current_client is None or current_client.bleak_client is None:
                    time.sleep(0.01)
                    continue
                bleak_client = cast(Any, current_client.bleak_client)
                bleak_client.is_connected_result = False
                try:
                    iface._on_ble_disconnect(bleak_client)
                except Exception as exc:  # noqa: BLE001 - test collects worker failures
                    worker_errors.put(exc)
                    return
                time.sleep(0.01)  # Very short delay between disconnects

        # Start rapid disconnect simulation in a separate thread
        disconnect_thread = threading.Thread(target=simulate_rapid_disconnects)
        disconnect_thread.start()
        disconnect_thread.join()

        collected_worker_errors: list[Exception] = []
        while not worker_errors.empty():
            collected_worker_errors.append(worker_errors.get_nowait())
        assert collected_worker_errors == []

        # Verify that the interface handled rapid disconnects gracefully
        assert client.bleak_client is not None
        # Test reaching this point without crashing indicates success
        connect_call_snapshot, stub_kwargs = _snapshot_connect_stub_state(iface)
        assert (
            len(connect_call_snapshot) >= baseline_connects + 1
        ), "Auto-reconnect should continue scheduling during rapid disconnects"
        reconnect_kwargs = stub_kwargs[baseline_connects:]
        assert reconnect_kwargs
        assert all(
            kwargs == {"pair": True, "connect_timeout": 3.5}
            for kwargs in reconnect_kwargs
        )
        observed_client, attempted_clients, republished_clients = (
            _wait_for_latest_stress_client(
                iface,
                baseline_attempts=baseline_attempts,
                baseline_clients=baseline_client_objects,
            )
        )
        assert len(republished_clients) >= baseline_clients + 1
        latest_successful_attempt = _latest_successful_stress_attempt(
            attempted_clients,
            republished_clients,
        )
        assert latest_successful_attempt is not None
        assert observed_client is latest_successful_attempt

    # Test 2: Concurrent connect/disconnect operations
    with create_interface_with_auto_reconnect() as (iface2, client2):
        iface2.connect(mock_device.address, pair=False, connect_timeout=7.0)
        baseline_connect_snapshot, _ = _snapshot_connect_stub_state(iface2)
        baseline_connects = len(baseline_connect_snapshot)
        baseline_attempts = list(
            cast(list["StressTestClient"], _get_stress_test_attempts(iface2))
        )
        baseline_clients = len(_get_stress_test_clients(iface2))
        baseline_client_objects = list(
            cast(list["StressTestClient"], _get_stress_test_clients(iface2))
        )
        concurrent_worker_errors: Queue[Exception] = Queue()

        def _stress_test_disconnects() -> None:
            """Perform a burst of simulated BLE disconnects on iface2 to exercise auto-reconnect and disconnect handling.

            Performs five disconnect attempts spaced 5 milliseconds apart and
            records worker failures for assertion on the main thread.
            """
            for _ in range(5):
                try:
                    current_client = cast("StressTestClient", iface2.client)
                    if current_client is None or current_client.bleak_client is None:
                        time.sleep(0.005)
                        continue
                    bleak_client = cast(Any, current_client.bleak_client)
                    bleak_client.is_connected_result = False
                    iface2._on_ble_disconnect(bleak_client)
                    time.sleep(0.005)
                except Exception as exc:  # noqa: BLE001 - test collects worker failures
                    concurrent_worker_errors.put(exc)

        # Start multiple threads for concurrent operations
        threads: list[threading.Thread] = []
        for _ in range(3):
            thread = threading.Thread(target=_stress_test_disconnects, daemon=True)
            threads.append(thread)
            thread.start()

        # Wait for all threads to complete
        for thread in threads:
            thread.join()

        collected_concurrent_worker_errors: list[Exception] = []
        while not concurrent_worker_errors.empty():
            collected_concurrent_worker_errors.append(
                concurrent_worker_errors.get_nowait()
            )
        assert collected_concurrent_worker_errors == []
        assert client2.bleak_client is not None
        assert cast(Any, client2.bleak_client).disconnect_count >= 0
        connect_call_snapshot, stub_kwargs = _snapshot_connect_stub_state(iface2)
        assert len(connect_call_snapshot) >= baseline_connects + 1
        reconnect_kwargs = stub_kwargs[baseline_connects:]
        assert reconnect_kwargs
        assert all(
            kwargs == {"pair": False, "connect_timeout": 7.0}
            for kwargs in reconnect_kwargs
        )
        observed_client2, attempted_clients, republished_clients = (
            _wait_for_latest_stress_client(
                iface2,
                baseline_attempts=baseline_attempts,
                baseline_clients=baseline_client_objects,
            )
        )
        assert len(republished_clients) >= baseline_clients + 1
        latest_successful_attempt = _latest_successful_stress_attempt(
            attempted_clients,
            republished_clients,
        )
        assert latest_successful_attempt is not None
        assert observed_client2 is latest_successful_attempt

    # Test 3: Stress test with connection failures
    with create_interface_with_auto_reconnect() as (iface3, _client3):
        cast(Any, iface3)._stress_test_should_fail_connect = True
        failure_errors: list[Exception] = []
        baseline_connect_count = len(_get_connect_stub_calls(iface3))

        for _ in range(5):
            try:
                current_client = cast("StressTestClient", iface3.client)
                if current_client is None or current_client.bleak_client is None:
                    time.sleep(0.01)
                    continue
                bleak_client = cast(Any, current_client.bleak_client)
                bleak_client.is_connected_result = False
                iface3._on_ble_disconnect(bleak_client)
                time.sleep(0.01)
            except (
                Exception
            ) as exc:  # noqa: BLE001 - test records failure-path behavior
                failure_errors.append(exc)

        assert failure_errors == []
        reconnect_deadline = time.monotonic() + 1.0
        while len(_get_connect_stub_calls(iface3)) <= baseline_connect_count:
            if time.monotonic() >= reconnect_deadline:
                break
            time.sleep(0.01)
        assert (
            len(_get_connect_stub_calls(iface3)) >= baseline_connect_count + 1
        ), "Failure path should still schedule reconnect attempts"

    # Verify no critical errors in logs
    critical = [r for r in caplog.records if r.levelno >= logging.CRITICAL]
    assert (
        not critical
    ), f"Critical errors found in logs: {[r.message for r in critical]}"


def test_ble_client_is_connected_exception_handling(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that BLEClient.isConnected handles exceptions gracefully."""
    # logging already imported at top
    # BLEClient already imported at top as ble_mod.BLEClient

    # Set logging level to DEBUG to capture debug messages
    caplog.set_level(logging.DEBUG)

    class ExceptionBleakClient:
        """Mock bleak client that raises exceptions during connection checks."""

        def __init__(self, exception_type: type[Exception]) -> None:
            """Test BLE client constructor storing an exception type used to simulate failures in BLE operations.

            Parameters
            ----------
            exception_type : type[Exception]
                Exception class that this test client will raise from its BLE method stubs.
            """
            self.exception_type = exception_type

        def is_connected(self) -> bool:
            """Simulate a failing connection-state check by raising the configured exception.

            Raises
            ------
            Exception
                An instance of `self.exception_type` with message "conn check failed".
            """
            raise self.exception_type("conn check failed")  # noqa: TRY003

    # Create BLEClient with a mock bleak client that raises exceptions
    ble_client = BLEClient(log_if_no_address=False)
    try:
        ble_client.bleak_client = cast(Any, ExceptionBleakClient(AttributeError))

        # Should return False and log debug message when AttributeError occurs
        result = ble_client.isConnected()
        assert result is False
        assert "Unable to read bleak connection state" in caplog.text

        # Clear caplog
        caplog.clear()

        # Test TypeError
        ble_client.bleak_client = cast(Any, ExceptionBleakClient(TypeError))
        result = ble_client.isConnected()
        assert result is False
        assert "Unable to read bleak connection state" in caplog.text

        # Clear caplog
        caplog.clear()

        # Test RuntimeError
        ble_client.bleak_client = cast(Any, ExceptionBleakClient(RuntimeError))
        result = ble_client.isConnected()
        assert result is False
        assert "Unable to read bleak connection state" in caplog.text
    finally:
        ble_client.close()


def test_ble_client_async_timeout_maps_to_ble_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BLEClient._async_await should wrap FutureTimeoutError in BLEInterface.BLEError."""

    # BLEClient and BLEInterface already imported at top as ble_mod.BLEClient, ble_mod.BLEInterface

    client = BLEClient(
        log_if_no_address=False
    )  # address=None keeps underlying bleak client unset
    fake_future = _make_fake_future(FutureTimeoutError())
    monkeypatch.setattr(client, "_async_run", _bind_coro_to_future(fake_future))

    async def _test_coro() -> None:
        """Run a no-op coroutine used for tests."""

    # BLEClient._async_await raises BLEClient.BLEError for timeouts
    with pytest.raises(BLEClient.BLEError) as excinfo:
        client._async_await(_test_coro(), timeout=0.01)

    assert "Async operation timed out" in str(excinfo.value)
    assert fake_future.cancelled is True

    client.close()
    coro_obj = getattr(fake_future, "coro", None)
    if isinstance(coro_obj, types.CoroutineType):
        coro_obj.close()


def test_ble_client_async_runtime_error_maps_to_ble_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BLEClient._async_await should surface RuntimeError as a non-timeout BLE error."""
    client = BLEClient(log_if_no_address=False)
    fake_future = _make_fake_future(RuntimeError("loop is closed"))
    monkeypatch.setattr(client, "_async_run", _bind_coro_to_future(fake_future))

    async def _test_coro() -> None:
        """Run a no-op coroutine used for tests."""

    # BLEClient._async_await raises BLEClient.BLEError for runtime errors
    with pytest.raises(BLEClient.BLEError) as excinfo:
        client._async_await(_test_coro(), timeout=0.01)

    assert "Async operation failed: loop is closed" in str(excinfo.value)
    assert fake_future.cancelled is True
    client.close()
    coro_obj = getattr(fake_future, "coro", None)
    if isinstance(coro_obj, types.CoroutineType):
        coro_obj.close()


def test_wait_for_disconnect_notifications_exceptions(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that _wait_for_disconnect_notifications handles exceptions gracefully."""
    # logging already imported at top

    # Set logging level to DEBUG to capture debug messages
    caplog.set_level(logging.DEBUG)

    # Also ensure the logger is configured to capture the actual module logger
    logger = logging.getLogger("meshtastic.ble")
    logger.setLevel(logging.DEBUG)

    client = DummyClient()
    iface = _build_interface(monkeypatch, client)

    class MockPublishingThread:
        """Mock publishingThread that raises RuntimeError in queue.put_nowait."""

        class _FailingQueue:
            @staticmethod
            def put_nowait(_callback: object) -> None:
                raise RuntimeError("thread error in put_nowait")  # noqa: TRY003

        queue = _FailingQueue()

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.publishingThread",
        MockPublishingThread(),
    )

    # Should handle RuntimeError gracefully
    iface._wait_for_disconnect_notifications()
    assert "disconnect notification flush" in caplog.text

    # Clear caplog
    caplog.clear()

    class MockPublishingThread2:
        """Mock publishingThread that raises ValueError in queue.put_nowait."""

        class _FailingQueue:
            @staticmethod
            def put_nowait(_callback: object) -> None:
                raise ValueError("invalid state")  # noqa: TRY003

        queue = _FailingQueue()

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.publishingThread",
        MockPublishingThread2(),
    )

    # Should handle ValueError gracefully
    iface._wait_for_disconnect_notifications()
    assert "disconnect notification flush" in caplog.text

    iface.close()


def test_drain_publish_queue_exceptions(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that _drain_publish_queue handles exceptions gracefully."""
    # logging, threading, and Queue already imported at top

    # Set logging level to DEBUG to capture debug messages
    caplog.set_level(logging.DEBUG)

    client = DummyClient()
    iface = _build_interface(monkeypatch, client)

    # Create a mock queue with a runnable that raises exceptions
    class ExceptionRunnable:
        """Mock runnable that raises ValueError when called."""

        def __call__(self) -> None:
            """Call the mock callback; this implementation always raises a ValueError.

            Raises
            ------
            ValueError
                Always raised to indicate the mock callback failed during execution.
            """
            raise ValueError("callback failed")  # noqa: TRY003

    mock_queue: Queue[Callable[[], object]] = Queue()
    mock_queue.put(ExceptionRunnable())

    # Mock publishingThread with the queue
    # meshtastic.ble_interface already imported at top as ble_mod

    class MockPublishingThread:
        """Mock publishingThread with a predefined queue."""

        def __init__(self) -> None:
            """Initialize the mock publishing thread with a provided external queue.

            Store the external `mock_queue` on `self.queue` so tests can inject, control, and inspect deferred publish callbacks.
            """
            self.queue = mock_queue

        def queueWork(self, _callback: Callable[[], object] | None) -> object | None:
            """Execute a callback immediately to simulate scheduling work.

            Parameters
            ----------
            _callback : callable | None
                Callable to execute; if `None`, no action is taken.

            Returns
            -------
                The value returned by `_callback` if provided, otherwise `None`.
            """
            if _callback:
                return _callback()
            return None

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.publishingThread",
        MockPublishingThread(),
    )

    # Should handle ValueError gracefully
    flush_event = threading.Event()
    iface._drain_publish_queue(flush_event)
    assert "Error in deferred publish callback" in caplog.text

    iface.close()
