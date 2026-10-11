"""BLE shutdown, notification, and reconnect tests."""

# pylint: disable=redefined-outer-name

import logging
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable, cast
from unittest.mock import MagicMock

import pytest
from bleak.exc import BleakDBusError, BleakError

# Import meshtastic modules for use in tests
import meshtastic.interfaces.ble as ble_mod
from meshtastic.interfaces.ble import (
    FROMNUM_UUID,
    LEGACY_LOGRADIO_UUID,
    LOGRADIO_UUID,
    BLEClient,
)
from meshtastic.interfaces.ble.reconnection import ReconnectScheduler, ReconnectWorker
from meshtastic.interfaces.ble.state import BLEStateManager, ConnectionState
from tests._ble_interface_core_support import (
    SAFE_EXECUTE_LEGACY_POSITIONAL_MISMATCH_ERROR_MSG,
    SAFE_EXECUTE_UNEXPECTED_ERROR_MSG,
    _attach_close_monitor,
    _ReconnectTestNotificationManager,
    _ReconnectTestScheduler,
    pub,
)

# Import common fixtures
from tests.test_ble_interface_fixtures import DummyClient, _build_interface

pytestmark = pytest.mark.unit


def test_close_with_timeout_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """close(timeout=...) should remain idempotent across repeated calls."""
    client = DummyClient()
    iface = _build_interface(monkeypatch, client)

    iface.close(timeout=1.0)
    iface.close(timeout=0.2)
    iface.close(timeout=0.0)

    assert client.disconnect_calls == 1
    assert client.close_calls == 1


@pytest.mark.parametrize(
    ("exc_cls", "message"),
    [
        pytest.param(BleakError, "boom", id="BleakError"),
        pytest.param(RuntimeError, "boom", id="RuntimeError"),
        pytest.param(OSError, "boom", id="OSError"),
        pytest.param(OSError, "Permission denied", id="permission-denied"),
    ],
)
def test_close_handles_errors(
    monkeypatch: pytest.MonkeyPatch,
    exc_cls: type[Exception],
    message: str,
) -> None:
    """Test that close() handles various exception types gracefully."""
    # pub already imported at top as mesh_iface_module.pub

    calls: list[tuple[str, dict[str, object]]] = []

    def _capture(topic: str, **kwargs: object) -> None:
        """Record a published pubsub message for test inspection.

        Appends (topic, kwargs) to the module-level `calls` list.

        Parameters
        ----------
        topic : str
            Pubsub topic identifier.
        **kwargs
            Additional message fields to capture.
        """
        calls.append((topic, kwargs))

    monkeypatch.setattr(pub, "sendMessage", _capture)

    client = DummyClient(disconnect_exception=exc_cls(message))
    iface = _build_interface(monkeypatch, client)

    iface.close()

    assert client.disconnect_calls == 1
    assert client.close_calls == 1
    assert (
        sum(
            1
            for t, kw in calls
            if t == "meshtastic.connection.status" and kw.get("connected") is False
        )
        == 1
    )


@pytest.mark.usefixtures("clear_registry")
def test_close_clears_connecting_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """close() should clear provisional connecting state to prevent orphaned gate claims."""
    from meshtastic.interfaces.ble.gating import (
        _CONNECTING_ADDRS,
        _addr_key,
    )

    client = DummyClient()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)

    key = _addr_key(iface.address)
    assert key is not None

    iface._mark_address_keys_connecting(iface.address)
    assert key in _CONNECTING_ADDRS, "Connecting claim should be registered"

    iface.close()

    assert key not in _CONNECTING_ADDRS, "close() should clear connecting claim"


def test_close_skips_disconnect_when_interpreter_finalizing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """close() should skip client I/O during finalization without leaking resources."""
    import meshtastic.interfaces.ble.connection as connection_mod

    client = DummyClient()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    try:
        with monkeypatch.context() as finalizing:
            sys_attrs = vars(vars(connection_mod)["sys"]).copy()
            sys_attrs["is_finalizing"] = lambda: True
            finalizing.setattr(
                connection_mod,
                "sys",
                SimpleNamespace(**sys_attrs),
                raising=True,
            )

            iface.close()

            assert client.disconnect_calls == 0
            assert client.close_calls == 0
    finally:
        # The interpreter-finalizing branch intentionally skips client I/O.
        # Clean the test double after the scoped module patch is restored.
        iface._client_manager._safe_close_client(cast(Any, client))

    assert client.disconnect_calls == 1
    assert client.close_calls == 1


def test_close_closes_discovery_manager_before_receive_thread_join(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """close() should stop discovery before attempting receive-thread joins."""
    client = DummyClient()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    discovery_closed = threading.Event()
    join_called = threading.Event()
    stop_worker = threading.Event()

    class _DiscoveryManager:
        def close(self) -> None:
            discovery_closed.set()

    cast(Any, iface)._discovery_manager = _DiscoveryManager()
    receive_thread = threading.Thread(
        target=lambda: stop_worker.wait(1.0),
        name="BLEReceiveTest",
    )
    receive_thread.start()
    iface._receiveThread = receive_thread

    def _assert_join_after_discovery_close(
        _thread: threading.Thread, timeout: float | None = None
    ) -> None:
        """Assert discovery closes before join and then join the receive thread."""
        assert discovery_closed.is_set()
        join_called.set()
        stop_worker.set()
        _thread.join(timeout=timeout)

    monkeypatch.setattr(
        iface.thread_coordinator,
        "_join_thread",
        _assert_join_after_discovery_close,
    )

    iface.close()
    assert discovery_closed.is_set()
    assert join_called.is_set()
    receive_thread.join(timeout=0.5)
    assert not receive_thread.is_alive()


def test_close_clears_ble_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Closing the interface should leave no BLE* threads running."""
    # threading already imported at top

    baseline_threads = set(threading.enumerate())
    client = DummyClient()
    iface = _build_interface(monkeypatch, client)

    iface.close()

    # Poll only for BLE threads created by this interface. Other tests and
    # process-wide singleton workers are outside this test's ownership.
    max_wait_time = 1.0
    poll_interval = 0.05
    deadline = time.monotonic() + max_wait_time
    lingering: list[str] = []

    while time.monotonic() < deadline:
        lingering = [
            thread.name
            for thread in threading.enumerate()
            if thread not in baseline_threads and thread.name.startswith("BLE")
        ]

        if not lingering:
            break

        time.sleep(poll_interval)

    assert (
        not lingering
    ), f"Found lingering BLE threads after {max_wait_time}s: {lingering}"


@pytest.mark.parametrize("exc_type", [RuntimeError, OSError])
def test_receive_thread_specific_exceptions(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    exc_type: type[Exception],
) -> None:
    """Verify that the BLE receive thread treats specific exceptions as fatal: it logs a fatal error message and invokes the interface's close().

    The test injects a client whose read_gatt_char raises the given exception type,
    triggers the receive loop, and asserts that the fatal log entry is present and that close() was called.
    """
    # logging and threading already imported at top

    # Set logging level to DEBUG to capture debug messages
    caplog.set_level(logging.DEBUG)

    class ExceptionClient(DummyClient):
        """Mock client that raises specific exceptions for testing."""

        def __init__(self, exception_type: type[Exception]) -> None:
            """Create a test BLE client configured to raise the given exception from its faulting methods.

            Parameters
            ----------
            exception_type : type | Exception
                Exception class or exception instance that the client will raise when its faulting methods are invoked.
            """
            super().__init__()
            self.exception_type = exception_type

        def read_gatt_char(self, *_args: object, **_kwargs: object) -> bytes:
            """Raise the client's configured exception to simulate a failing GATT characteristic read.

            Raises
            ------
            Exception
                An instance of `self.exception_type` constructed with the message "test".
            """
            raise self.exception_type("test")

    caplog.clear()

    client = ExceptionClient(exc_type)
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    close_called = _attach_close_monitor(monkeypatch, iface)

    # Exercise the receive loop synchronously for deterministic assertions.
    iface._want_receive = True
    with iface._state_lock:
        cast(Any, iface).client = client

    iface._read_trigger.set()
    iface._receive_from_radio_impl()

    assert "Fatal error in BLE receive thread" in caplog.text
    assert (
        close_called.is_set()
    ), f"Expected close() to be called for {exc_type.__name__}"

    # Clean up
    iface._want_receive = False
    caplog.clear()
    try:
        iface.close()
    except Exception as exc:  # noqa: BLE001 - cleanup best-effort in tests
        # Log for visibility; still allow test to proceed with cleanup.
        logging.warning("Cleanup error in iface.close(): %r", exc)


def test_bleak_error_transient_retry_logic(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify that BleakError in the receive thread goes through transient retry logic.

    The interface should retry on transient BleakError before giving up and closing.

    Raises
    ------
    BleakError
    """
    caplog.set_level(logging.DEBUG)

    class BleakErrorClient(DummyClient):
        """Mock client that raises BleakError for testing retry logic."""

        def __init__(self) -> None:
            """Initialize the instance and set the read operation counter to 0."""
            super().__init__()
            self.read_count = 0

        def read_gatt_char(self, *_args: object, **_kwargs: object) -> bytes:
            """Simulate a GATT characteristic read that increments self.read_count and always fails.

            Increments self.read_count and then raises a BleakError with the message "transient error".

            Raises
            ------
            BleakError
                Always raised with message "transient error".
            """
            self.read_count += 1
            raise BleakError("transient error")

    client = BleakErrorClient()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    close_called = _attach_close_monitor(monkeypatch, iface)

    iface._want_receive = True

    with iface._state_lock:
        cast(Any, iface).client = client

    iface._read_trigger.set()
    iface._receive_from_radio_impl()

    assert "Transient BLE read error, retrying" in caplog.text
    assert "Fatal BLE read error after retries" in caplog.text
    assert client.read_count == ble_mod.BLEConfig.TRANSIENT_READ_MAX_RETRIES + 1
    assert close_called.is_set()

    # Clean up
    iface._want_receive = False
    try:
        iface.close()
    except Exception as exc:  # noqa: BLE001 - cleanup best-effort in tests
        logging.warning("Cleanup error in iface.close(): %r", exc)


def test_log_notification_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that log notifications are properly registered for both legacy and current log UUIDs."""
    # UUID constants already imported at top as ble_mod.FROMNUM_UUID, ble_mod.LEGACY_LOGRADIO_UUID, ble_mod.LOGRADIO_UUID

    class MockClientWithLogChars(DummyClient):
        """Mock client that has log characteristics."""

        def __init__(self) -> None:
            """Initialize the mock BLE client and its notification/characteristic tracking.

            Attributes
            ----------
            start_notify_calls : list
                Recorded calls to start_notify as tuples of the arguments passed.
            has_characteristic_map : dict
                Maps characteristic UUID strings to booleans indicating presence. Initially sets
                LEGACY_LOGRADIO_UUID, LOGRADIO_UUID, and FROMNUM_UUID to True.
            """
            super().__init__()
            self.start_notify_calls: list[tuple[object, object]] = []
            self.has_characteristic_map = {
                LEGACY_LOGRADIO_UUID: True,
                LOGRADIO_UUID: True,
                FROMNUM_UUID: True,
            }

        def has_characteristic(self, uuid: str) -> bool:
            """Determine whether the client exposes a GATT characteristic identified by the given UUID.

            Parameters
            ----------
            uuid : uuid.UUID or hashable
                Characteristic UUID or key used to look up the client's characteristic map.

            Returns
            -------
            bool
                `True` if the UUID is present in the client's characteristic map, `False` otherwise.
            """
            return self.has_characteristic_map.get(uuid, False)

        def start_notify(self, *_args: object, **_kwargs: object) -> None:
            """Record a notification registration by saving the characteristic UUID and its handler.

            If called with at least two positional arguments, treats the first as the characteristic UUID and the second as the notification handler, and appends the pair to self.start_notify_calls. Any additional positional or keyword arguments are accepted and ignored.
            """
            # Extract uuid and handler from args if available
            if len(_args) >= 2:
                uuid, handler = _args[0], _args[1]
                self.start_notify_calls.append((uuid, handler))

    client = MockClientWithLogChars()
    iface = _build_interface(monkeypatch, client)

    # Call _register_notifications to test log notification setup
    iface._register_notifications(cast(BLEClient, client))

    # Verify that all three notifications were registered
    registered_uuids = [call[0] for call in client.start_notify_calls]

    # Should have registered both log notifications and the critical FROMNUM notification
    assert (
        LEGACY_LOGRADIO_UUID in registered_uuids
    ), "Legacy log notification should be registered"
    assert (
        LOGRADIO_UUID in registered_uuids
    ), "Current log notification should be registered"
    assert FROMNUM_UUID in registered_uuids, "FROMNUM notification should be registered"

    # Verify handlers are correctly associated
    legacy_call = next(
        call for call in client.start_notify_calls if call[0] == LEGACY_LOGRADIO_UUID
    )
    current_call = next(
        call for call in client.start_notify_calls if call[0] == LOGRADIO_UUID
    )
    fromnum_call = next(
        call for call in client.start_notify_calls if call[0] == FROMNUM_UUID
    )

    assert callable(
        legacy_call[1]
    ), "Legacy log notification should register a callable handler"
    assert callable(
        current_call[1]
    ), "Current log notification should register a callable handler"
    assert callable(
        fromnum_call[1]
    ), "FROMNUM notification should register a callable handler"

    iface.close()


class _ClientWithCallbacks(DummyClient):
    """Reusable notification test double that captures callback and notify registrations."""

    def __init__(self) -> None:
        """Initialize callback and notification-registration capture state."""
        super().__init__()
        self.callbacks: dict[str, Callable[[Any, bytes], None]] = {}
        self.start_notify_calls: dict[str, int] = {}

    def has_characteristic(self, uuid: str) -> bool:
        """Return whether the notification UUID is supported by the double.

        Parameters
        ----------
        uuid : str
            Characteristic UUID queried by notification registration.

        Returns
        -------
        bool
            ``True`` for the three notification characteristics under test.
        """
        return uuid in {LEGACY_LOGRADIO_UUID, LOGRADIO_UUID, FROMNUM_UUID}

    def start_notify(self, *args: object, **kwargs: object) -> None:
        """Record a notification callback registration.

        Parameters
        ----------
        *args : object
            Positional notification arguments; the first two are UUID and callback.
        **kwargs : object
            Additional notification options, accepted for interface compatibility.
        """
        _ = kwargs
        if len(args) >= 2:
            uuid = str(args[0])
            self.start_notify_calls[uuid] = self.start_notify_calls.get(uuid, 0) + 1
            self.callbacks[uuid] = cast(Callable[[Any, bytes], None], args[1])


def test_register_notifications_safe_call_inline_fallback_when_safe_execute_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Notification wrappers should use inline fallback when safe_execute hooks are unconfigured."""

    client = _ClientWithCallbacks()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    errors: list[str] = []
    try:
        iface.error_handler = cast(
            Any,
            SimpleNamespace(
                safe_execute=None,
                _safe_execute=None,
            ),
        )
        monkeypatch.setattr(
            iface,
            "_log_radio_handler",
            lambda _sender, _data: (_ for _ in ()).throw(RuntimeError("handler boom")),
            raising=True,
        )
        monkeypatch.setattr(
            iface,
            "_report_notification_handler_error",
            lambda msg: errors.append(msg),
            raising=True,
        )

        iface._register_notifications(cast(BLEClient, client))
        client.callbacks[LOGRADIO_UUID]("sender", b"log")

        assert errors == ["Error in log notification handler"]
    finally:
        iface.close()


def test_register_notifications_ignores_synthesized_public_error_reporter() -> None:
    """A synthesized public reporter must not mask the declared legacy fallback."""
    from meshtastic.interfaces.ble.notifications import (
        BLENotificationDispatcher,
        NotificationManager,
    )

    client = _ClientWithCallbacks()
    legacy_errors: list[str] = []
    dynamic_errors: list[str] = []

    class _Iface:
        _connection_session_epoch = 1
        BLEError = BLEClient.BLEError

        def __init__(self) -> None:
            self.client = client

        def _report_notification_handler_error(self, message: str) -> None:
            legacy_errors.append(message)

        def __getattr__(self, name: str) -> object:
            if name == "report_notification_handler_error":
                return lambda message: dynamic_errors.append(message)
            raise AttributeError(name)

    iface = _Iface()
    dispatcher = BLENotificationDispatcher(
        notification_manager=NotificationManager(),
        error_handler_provider=lambda: None,
        trigger_read_event=lambda: None,
        registration_current_provider=lambda _client, _epoch: True,
    )
    dispatcher.register_notifications(
        iface,  # type: ignore[arg-type]
        cast(BLEClient, client),
        legacy_log_handler=lambda _sender, _data: None,
        log_handler=lambda _sender, _data: (_ for _ in ()).throw(
            RuntimeError("handler boom")
        ),
        from_num_handler=lambda _sender, _data: None,
    )

    client.callbacks[LOGRADIO_UUID]("sender", b"log")

    assert legacy_errors == ["Error in log notification handler"]
    assert dynamic_errors == []


def test_register_notifications_safe_execute_fallback_still_invokes_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Incompatible safe_execute signatures should fallback to direct handler invocation."""

    safe_execute_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def _incompatible_safe_execute(
        _func: Callable[[], None], *args: object, **kwargs: object
    ) -> None:
        safe_execute_calls.append((args, dict(kwargs)))
        if "error_msg" in kwargs:
            raise TypeError(SAFE_EXECUTE_UNEXPECTED_ERROR_MSG)
        if args:
            raise TypeError(SAFE_EXECUTE_LEGACY_POSITIONAL_MISMATCH_ERROR_MSG)
        raise AssertionError("callable-only probe should be skipped in this path")

    client = _ClientWithCallbacks()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    log_calls: list[tuple[object, bytes]] = []
    error_hook = MagicMock()
    try:
        iface.error_handler = cast(
            Any,
            SimpleNamespace(
                safe_execute=_incompatible_safe_execute,
                handle_unhandled_exception=error_hook,
            ),
        )
        monkeypatch.setattr(
            iface,
            "_log_radio_handler",
            lambda sender, data: log_calls.append((sender, data)),
            raising=True,
        )

        iface._register_notifications(cast(BLEClient, client))
        client.callbacks[LOGRADIO_UUID]("sender", b"log")

        assert log_calls == [("sender", b"log")]
        assert len(safe_execute_calls) == 2
        error_hook.assert_not_called()
    finally:
        iface.close()


def test_register_notifications_reuses_cached_wrapper_with_latest_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cached notification wrappers should dispatch to latest handlers after re-registration."""

    client = _ClientWithCallbacks()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    log_handler_calls: list[tuple[str, bytes]] = []
    legacy_handler_calls: list[tuple[str, bytes]] = []
    from_num_calls: list[tuple[str, bytes]] = []
    try:
        monkeypatch.setattr(
            iface,
            "_log_radio_handler",
            lambda _sender, payload: log_handler_calls.append(("first", payload)),
            raising=True,
        )
        monkeypatch.setattr(
            iface,
            "_legacy_log_radio_handler",
            lambda _sender, payload: legacy_handler_calls.append(("first", payload)),
            raising=True,
        )
        monkeypatch.setattr(
            iface,
            "_from_num_handler",
            lambda _sender, payload: from_num_calls.append(("first", bytes(payload))),
            raising=True,
        )
        iface._register_notifications(cast(BLEClient, client))
        assert client.start_notify_calls.get(LOGRADIO_UUID, 0) == 1
        assert client.start_notify_calls.get(LEGACY_LOGRADIO_UUID, 0) == 1
        assert client.start_notify_calls.get(FROMNUM_UUID, 0) == 1
        first_wrapper = client.callbacks[LOGRADIO_UUID]
        first_legacy_wrapper = client.callbacks[LEGACY_LOGRADIO_UUID]
        first_from_num_wrapper = client.callbacks[FROMNUM_UUID]
        first_wrapper("sender", b"one")
        first_legacy_wrapper("sender", b"legacy-one")
        first_from_num_wrapper("sender", b"fromnum-one")

        monkeypatch.setattr(
            iface,
            "_log_radio_handler",
            lambda _sender, payload: log_handler_calls.append(("second", payload)),
            raising=True,
        )
        monkeypatch.setattr(
            iface,
            "_legacy_log_radio_handler",
            lambda _sender, payload: legacy_handler_calls.append(("second", payload)),
            raising=True,
        )
        monkeypatch.setattr(
            iface,
            "_from_num_handler",
            lambda _sender, payload: from_num_calls.append(("second", bytes(payload))),
            raising=True,
        )
        iface._register_notifications(cast(BLEClient, client))
        assert client.callbacks[LOGRADIO_UUID] is first_wrapper
        assert client.callbacks[LEGACY_LOGRADIO_UUID] is first_legacy_wrapper
        assert client.callbacks[FROMNUM_UUID] is first_from_num_wrapper
        assert client.start_notify_calls.get(LOGRADIO_UUID, 0) == 1
        assert client.start_notify_calls.get(LEGACY_LOGRADIO_UUID, 0) == 1
        assert client.start_notify_calls.get(FROMNUM_UUID, 0) == 1
        client.callbacks[LOGRADIO_UUID]("sender", b"two")
        client.callbacks[LEGACY_LOGRADIO_UUID]("sender", b"legacy-two")
        client.callbacks[FROMNUM_UUID]("sender", b"fromnum-two")

        assert log_handler_calls == [("first", b"one"), ("second", b"two")]
        assert legacy_handler_calls == [
            ("first", b"legacy-one"),
            ("second", b"legacy-two"),
        ]
        assert from_num_calls == [
            ("first", b"fromnum-one"),
            ("second", b"fromnum-two"),
        ]
    finally:
        iface.close()


class _FromNumRegistrationClient(DummyClient):
    """Notification-capable client for connection-finalization ownership tests."""

    def __init__(
        self,
        address: str = "dummy",
        *,
        on_fromnum_start: Callable[["_FromNumRegistrationClient"], None] | None = None,
    ) -> None:
        super().__init__()
        self.address = address
        assert self.bleak_client is not None
        self.bleak_client.address = address
        self.on_fromnum_start = on_fromnum_start
        self.start_notify_calls: list[str] = []

    def has_characteristic(self, uuid: str) -> bool:
        """Expose only the critical FROMNUM notification characteristic."""
        return uuid == FROMNUM_UUID

    def start_notify(self, *args: object, **_kwargs: object) -> None:
        """Record FROMNUM subscription and optionally mutate lifecycle state."""
        if not args:
            return
        characteristic = cast(str, args[0])
        self.start_notify_calls.append(characteristic)
        if characteristic == FROMNUM_UUID and self.on_fromnum_start is not None:
            self.on_fromnum_start(self)


def _prepare_provisional_notification_registration(
    iface: ble_mod.BLEInterface,
    *,
    published_client: DummyClient | None,
    replacement_pending: bool = False,
) -> int:
    """Place an interface in the pre-publication CONNECTING phase."""
    with iface._state_lock:
        iface.client = cast(Any, published_client)
        iface._disconnect_notified = False
        iface._client_publish_pending = True
        iface._client_replacement_pending = replacement_pending
        iface._connection_session_epoch += 1
        iface._state_manager._reset_to_disconnected()
        assert iface._state_manager._transition_to(ConnectionState.CONNECTING)
        return iface._connection_session_epoch


def test_register_notifications_keeps_fromnum_during_provisional_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FROMNUM registration must survive normal pre-publication finalization."""
    client = _FromNumRegistrationClient()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    try:
        _prepare_provisional_notification_registration(iface, published_client=None)

        iface._register_notifications(cast(BLEClient, client))

        assert client.start_notify_calls == [FROMNUM_UUID]
        assert client.stop_notify_calls == []
        assert iface._fromnum_notify_enabled is True
        assert iface._receive_recovery_controller._should_poll_without_notify() is False
        assert (
            FROMNUM_UUID
            in iface._notification_dispatcher._started_notify_characteristics
        )
    finally:
        with iface._state_lock:
            iface.client = cast(Any, client)
            iface._client_publish_pending = False
        iface.close()


def test_register_notifications_serializes_overlapping_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One dispatcher must not overlap backend registration transactions."""
    client = _FromNumRegistrationClient()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    _prepare_provisional_notification_registration(iface, published_client=None)

    first_backend_started = threading.Event()
    second_backend_started = threading.Event()
    release_first_backend = threading.Event()
    start_barrier = threading.Barrier(3)
    start_count_lock = threading.Lock()
    start_count = 0
    failures: list[BaseException] = []
    registration_entries = 0
    registration_entry_lock = threading.Lock()
    second_worker_entered = threading.Event()
    dispatcher = iface._get_notification_dispatcher()
    original_register_notifications = dispatcher.register_notifications
    original_start_notify = client.start_notify

    def _blocking_start_notify(*args: object, **kwargs: object) -> None:
        nonlocal start_count
        original_start_notify(*args, **kwargs)
        if not args or args[0] != FROMNUM_UUID:
            return
        with start_count_lock:
            start_count += 1
            current_start = start_count
        if current_start == 1:
            first_backend_started.set()
            assert release_first_backend.wait(timeout=2.0)
        else:
            second_backend_started.set()

    client.start_notify = _blocking_start_notify  # type: ignore[method-assign]

    def _observed_register_notifications(*args: object, **kwargs: object) -> None:
        nonlocal registration_entries
        with registration_entry_lock:
            registration_entries += 1
            if registration_entries == 2:
                second_worker_entered.set()
        original_register_notifications(*args, **kwargs)  # type: ignore[arg-type]

    dispatcher.register_notifications = _observed_register_notifications  # type: ignore[method-assign]

    def _register() -> None:
        try:
            start_barrier.wait(timeout=2.0)
            iface._register_notifications(cast(BLEClient, client))
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            failures.append(exc)

    workers = [threading.Thread(target=_register) for _ in range(2)]
    try:
        for worker in workers:
            worker.start()
        start_barrier.wait(timeout=2.0)
        assert first_backend_started.wait(timeout=2.0)
        assert second_worker_entered.wait(timeout=2.0)
        assert not second_backend_started.wait(timeout=0.1)
        release_first_backend.set()
        for worker in workers:
            worker.join(timeout=2.0)

        assert all(not worker.is_alive() for worker in workers)
        assert not failures, failures
        assert client.start_notify_calls == [FROMNUM_UUID]
        assert iface._fromnum_notify_enabled is True
    finally:
        release_first_backend.set()
        for worker in workers:
            worker.join(timeout=2.0)
        with iface._state_lock:
            iface.client = cast(Any, client)
            iface._client_publish_pending = False
        iface.close()


def test_register_notifications_keeps_fromnum_during_client_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replacement client may register before replacing the published client."""
    old_client = _FromNumRegistrationClient("old")
    new_client = _FromNumRegistrationClient("new")
    iface = _build_interface(monkeypatch, old_client, start_receive_thread=False)
    try:
        _prepare_provisional_notification_registration(
            iface,
            published_client=old_client,
            replacement_pending=True,
        )

        iface._register_notifications(cast(BLEClient, new_client))

        assert new_client.start_notify_calls == [FROMNUM_UUID]
        assert new_client.stop_notify_calls == []
        assert iface._fromnum_notify_enabled is True
    finally:
        with iface._state_lock:
            iface.client = cast(Any, new_client)
            iface._client_publish_pending = False
            iface._client_replacement_pending = False
        iface.close()


def test_execute_optional_registration_tolerates_optional_start_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Optional notification failure should leave registration state converged."""

    class _OptionalFailureClient(DummyClient):
        def __init__(self) -> None:
            super().__init__()
            self.start_notify_calls: list[str] = []

        def has_characteristic(self, uuid: str) -> bool:
            return uuid == LEGACY_LOGRADIO_UUID

        def start_notify(self, *args: object, **_kwargs: object) -> None:
            characteristic = cast(str, args[0])
            self.start_notify_calls.append(characteristic)
            raise RuntimeError("optional start failed")

    client = _OptionalFailureClient()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    dispatcher = iface._notification_dispatcher
    try:
        plan = dispatcher._prepare_registration_plan(
            iface,
            cast(BLEClient, client),
            legacy_log_handler=lambda _sender, _data: None,
            log_handler=lambda _sender, _data: None,
            from_num_handler=lambda _sender, _data: None,
        )

        assert (
            dispatcher._notification_manager._get_callback(LEGACY_LOGRADIO_UUID) is None
        )
        assert dispatcher._execute_optional_registration(
            iface, cast(BLEClient, client), plan
        )
        assert client.start_notify_calls == [LEGACY_LOGRADIO_UUID]
        assert LEGACY_LOGRADIO_UUID not in dispatcher._started_notify_characteristics
        assert (
            dispatcher._notification_manager._get_callback(LEGACY_LOGRADIO_UUID) is None
        )
        assert dispatcher.fromnum_notify_enabled is False
    finally:
        iface.close()


def test_execute_optional_registration_tracks_only_after_backend_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Optional callbacks should become locally visible only after backend startup."""

    class _OptionalSuccessClient(DummyClient):
        def __init__(self) -> None:
            super().__init__()
            self.before_start_notify: Callable[[str], None] = lambda _uuid: None

        def has_characteristic(self, uuid: str) -> bool:
            return uuid == LEGACY_LOGRADIO_UUID

        def start_notify(self, *args: object, **_kwargs: object) -> None:
            self.before_start_notify(cast(str, args[0]))

    client = _OptionalSuccessClient()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    dispatcher = iface._notification_dispatcher

    def _assert_untracked(uuid: str) -> None:
        assert dispatcher._notification_manager._get_callback(uuid) is None

    client.before_start_notify = _assert_untracked
    try:
        plan = dispatcher._prepare_registration_plan(
            iface,
            cast(BLEClient, client),
            legacy_log_handler=lambda _sender, _data: None,
            log_handler=lambda _sender, _data: None,
            from_num_handler=lambda _sender, _data: None,
        )

        assert dispatcher._execute_optional_registration(
            iface, cast(BLEClient, client), plan
        )
        assert (
            dispatcher._notification_manager._get_callback(LEGACY_LOGRADIO_UUID)
            is plan.optional_starts[0].handler
        )
        assert LEGACY_LOGRADIO_UUID in dispatcher._started_notify_characteristics
    finally:
        iface.close()


def test_execute_optional_registration_stops_backend_when_tracking_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local tracking failure must not leave an optional backend notify active."""

    class _OptionalTrackingClient(DummyClient):
        def __init__(self) -> None:
            super().__init__()
            self.stop_notify_calls: list[str] = []

        def has_characteristic(self, uuid: str) -> bool:
            return uuid == LEGACY_LOGRADIO_UUID

        def stop_notify(  # type: ignore[override]
            self, characteristic: str, **_kwargs: object
        ) -> None:
            self.stop_notify_calls.append(characteristic)

    def _fail_subscribe(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("tracking failed")

    client = _OptionalTrackingClient()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    dispatcher = iface._notification_dispatcher
    try:
        plan = dispatcher._prepare_registration_plan(
            iface,
            cast(BLEClient, client),
            legacy_log_handler=lambda _sender, _data: None,
            log_handler=lambda _sender, _data: None,
            from_num_handler=lambda _sender, _data: None,
        )
        monkeypatch.setattr(
            dispatcher._notification_manager, "_subscribe", _fail_subscribe
        )

        assert dispatcher._execute_optional_registration(
            iface, cast(BLEClient, client), plan
        )
        assert client.stop_notify_calls == [LEGACY_LOGRADIO_UUID]
        assert LEGACY_LOGRADIO_UUID not in dispatcher._started_notify_characteristics
        assert (
            dispatcher._notification_manager._get_callback(LEGACY_LOGRADIO_UUID) is None
        )
    finally:
        iface.close()


def test_execute_optional_registration_retains_started_state_when_compensation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed compensation should preserve backend-started state for later cleanup."""

    class _OptionalUnstoppableClient(DummyClient):
        def has_characteristic(self, uuid: str) -> bool:
            return uuid == LEGACY_LOGRADIO_UUID

        def stop_notify(  # type: ignore[override]
            self, _characteristic: str, **_kwargs: object
        ) -> None:
            raise RuntimeError("stop failed")

    def _fail_subscribe(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("tracking failed")

    client = _OptionalUnstoppableClient()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    dispatcher = iface._notification_dispatcher
    try:
        plan = dispatcher._prepare_registration_plan(
            iface,
            cast(BLEClient, client),
            legacy_log_handler=lambda _sender, _data: None,
            log_handler=lambda _sender, _data: None,
            from_num_handler=lambda _sender, _data: None,
        )
        monkeypatch.setattr(
            dispatcher._notification_manager, "_subscribe", _fail_subscribe
        )

        assert dispatcher._execute_optional_registration(
            iface, cast(BLEClient, client), plan
        )
        assert LEGACY_LOGRADIO_UUID in dispatcher._started_notify_characteristics
        assert (
            dispatcher._notification_manager._get_callback(LEGACY_LOGRADIO_UUID) is None
        )
    finally:
        iface.close()


def test_execute_optional_registration_rolls_back_stale_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Optional registration should unwind a start when session ownership turns stale."""

    class _OptionalStaleClient(DummyClient):
        def __init__(self) -> None:
            super().__init__()
            self.start_notify_calls: list[str] = []

        def has_characteristic(self, uuid: str) -> bool:
            return uuid == LEGACY_LOGRADIO_UUID

        def start_notify(self, *args: object, **_kwargs: object) -> None:
            self.start_notify_calls.append(cast(str, args[0]))

    client = _OptionalStaleClient()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    dispatcher = iface._notification_dispatcher
    try:
        plan = dispatcher._prepare_registration_plan(
            iface,
            cast(BLEClient, client),
            legacy_log_handler=lambda _sender, _data: None,
            log_handler=lambda _sender, _data: None,
            from_num_handler=lambda _sender, _data: None,
        )
        with iface._state_lock:
            iface._connection_session_epoch += 1

        assert not dispatcher._execute_optional_registration(
            iface, cast(BLEClient, client), plan
        )
        assert client.start_notify_calls == [LEGACY_LOGRADIO_UUID]
        assert client.stop_notify_calls == [LEGACY_LOGRADIO_UUID]
        assert not dispatcher._started_notify_characteristics
        assert dispatcher.fromnum_notify_enabled is False
    finally:
        iface.close()


def test_execute_fromnum_registration_rolls_back_failed_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FROMNUM commit failure should unwind backend state and remain in polling mode."""
    client = _FromNumRegistrationClient()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    dispatcher = iface._notification_dispatcher
    try:
        _prepare_provisional_notification_registration(iface, published_client=None)
        plan = dispatcher._prepare_registration_plan(
            iface,
            cast(BLEClient, client),
            legacy_log_handler=lambda _sender, _data: None,
            log_handler=lambda _sender, _data: None,
            from_num_handler=lambda _sender, _data: None,
        )
        dispatcher._started_notify_characteristics.add(FROMNUM_UUID)
        monkeypatch.setattr(
            iface._notification_manager,
            "_subscribe",
            lambda _uuid, _callback: (_ for _ in ()).throw(
                RuntimeError("local subscription tracking failed")
            ),
        )

        dispatcher._execute_fromnum_registration(iface, cast(BLEClient, client), plan)

        assert client.stop_notify_calls == [FROMNUM_UUID]
        assert dispatcher.fromnum_notify_enabled is False
        assert not dispatcher._started_notify_characteristics
    finally:
        with iface._state_lock:
            iface.client = cast(Any, client)
        iface.close()


def test_register_notifications_rolls_back_if_session_changes_during_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session rollover during notification I/O must invalidate registration."""
    iface_holder: dict[str, ble_mod.BLEInterface] = {}

    def _roll_session(_client: _FromNumRegistrationClient) -> None:
        iface = iface_holder["iface"]
        with iface._state_lock:
            iface._client_publish_pending = False
            iface._connection_session_epoch += 1

    client = _FromNumRegistrationClient(on_fromnum_start=_roll_session)
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    iface_holder["iface"] = iface
    try:
        _prepare_provisional_notification_registration(iface, published_client=None)

        iface._register_notifications(cast(BLEClient, client))

        assert client.start_notify_calls == [FROMNUM_UUID]
        assert client.stop_notify_calls == [FROMNUM_UUID]
        assert iface._fromnum_notify_enabled is False
        assert not iface._notification_dispatcher._started_notify_characteristics
    finally:
        with iface._state_lock:
            iface.client = cast(Any, client)
        iface.close()


@pytest.mark.parametrize("characteristic", [LEGACY_LOGRADIO_UUID, LOGRADIO_UUID])
def test_register_notifications_stale_optional_subscription_stops_once(
    monkeypatch: pytest.MonkeyPatch,
    characteristic: str,
) -> None:
    """A stale optional subscription should be stopped exactly once."""
    iface_holder: dict[str, ble_mod.BLEInterface] = {}

    class _OptionalStaleClient(DummyClient):
        def __init__(self) -> None:
            super().__init__()
            self.start_notify_calls: list[str] = []

        def has_characteristic(self, uuid: str) -> bool:
            return uuid == characteristic

        def start_notify(self, *args: object, **_kwargs: object) -> None:
            if not args:
                return
            started = cast(str, args[0])
            self.start_notify_calls.append(started)
            if started == characteristic:
                iface = iface_holder["iface"]
                with iface._state_lock:
                    iface._connection_session_epoch += 1

    client = _OptionalStaleClient()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    iface_holder["iface"] = iface
    try:
        iface._register_notifications(cast(BLEClient, client))

        assert client.start_notify_calls == [characteristic]
        assert client.stop_notify_calls == [characteristic]
        assert not iface._notification_dispatcher._started_notify_characteristics
        assert iface._fromnum_notify_enabled is False
    finally:
        iface.close()


def test_register_notifications_rolls_back_if_client_disconnects_during_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disconnected provisional client must not retain its subscription."""

    def _disconnect_candidate(candidate: _FromNumRegistrationClient) -> None:
        candidate._initialized = False
        candidate.bleak_client = None

    client = _FromNumRegistrationClient(on_fromnum_start=_disconnect_candidate)
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    try:
        _prepare_provisional_notification_registration(iface, published_client=None)

        iface._register_notifications(cast(BLEClient, client))

        assert client.start_notify_calls == [FROMNUM_UUID]
        assert client.stop_notify_calls == [FROMNUM_UUID]
        assert iface._fromnum_notify_enabled is False
        assert not iface._notification_dispatcher._started_notify_characteristics
    finally:
        with iface._state_lock:
            iface.client = cast(Any, client)
        iface.close()


def test_register_notifications_retries_fromnum_notify_acquired_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_register_notifications should retry FROMNUM notify once on BlueZ 'Notify acquired'."""

    class MockClientNotifyAcquired(DummyClient):
        """Mock client that fails the first FROMNUM notify start with Notify acquired."""

        def __init__(self) -> None:
            super().__init__()
            self.fromnum_start_attempts = 0

        def has_characteristic(self, uuid: str) -> bool:
            return uuid == FROMNUM_UUID

        def start_notify(self, *args: object, **kwargs: object) -> None:
            _ = kwargs
            if args and args[0] == FROMNUM_UUID:
                self.fromnum_start_attempts += 1
                if self.fromnum_start_attempts == 1:
                    raise BleakDBusError(
                        "org.bluez.Error.Failed",
                        ["Notify acquired"],
                    )

    client = MockClientNotifyAcquired()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)

    iface._register_notifications(cast(BLEClient, client))

    assert client.fromnum_start_attempts == 2
    assert client.stop_notify_calls == [FROMNUM_UUID]
    with iface._state_lock:
        assert iface._fromnum_notify_enabled is True

    iface.close()


def test_register_notifications_falls_back_on_non_notify_acquired_dbus_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_register_notifications should fall back to polling on non-Notify acquired DBus errors."""

    class MockClientFatalFromNumNotify(DummyClient):
        """Mock client that always raises a non-recoverable DBus notify error."""

        def __init__(self) -> None:
            super().__init__()
            self.fromnum_start_attempts = 0

        def has_characteristic(self, uuid: str) -> bool:
            return uuid == FROMNUM_UUID

        def start_notify(self, *args: object, **kwargs: object) -> None:
            _ = kwargs
            if args and args[0] == FROMNUM_UUID:
                self.fromnum_start_attempts += 1
                raise BleakDBusError(
                    "org.bluez.Error.Failed",
                    ["AlreadyConnected"],
                )

    client = MockClientFatalFromNumNotify()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)

    iface._register_notifications(cast(BLEClient, client))

    assert client.fromnum_start_attempts == 1
    assert client.stop_notify_calls == []
    with iface._state_lock:
        assert iface._fromnum_notify_enabled is False

    iface.close()
    with iface._state_lock:
        assert iface._fromnum_notify_enabled is False
    assert client.stop_notify_calls == []


def test_register_notifications_falls_back_to_polling_after_repeated_notify_acquired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated FROMNUM notify-acquired errors should not fail connect; polling fallback is enabled."""

    class MockClientPersistentNotifyAcquired(DummyClient):
        """Mock client that always returns Notify acquired for FROMNUM start_notify."""

        def __init__(self) -> None:
            super().__init__()
            self.fromnum_start_attempts = 0

        def has_characteristic(self, uuid: str) -> bool:
            return uuid == FROMNUM_UUID

        def start_notify(self, *args: object, **kwargs: object) -> None:
            _ = kwargs
            if args and args[0] == FROMNUM_UUID:
                self.fromnum_start_attempts += 1
                raise BleakDBusError(
                    "org.bluez.Error.Failed",
                    ["Notify acquired"],
                )

    client = MockClientPersistentNotifyAcquired()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)

    iface._register_notifications(cast(BLEClient, client))

    expected_attempts = ble_mod.BLEConfig.SERVICE_CHARACTERISTIC_RETRY_COUNT + 1
    assert client.fromnum_start_attempts == expected_attempts
    assert len(client.stop_notify_calls) == expected_attempts
    assert all(call == FROMNUM_UUID for call in client.stop_notify_calls)
    with iface._state_lock:
        assert iface._fromnum_notify_enabled is False

    iface.close()


def test_read_from_radio_with_retries_polling_mode_does_single_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Polling fallback mode should perform a single read attempt on empty payloads."""

    class EmptyReadClient(DummyClient):
        """Mock client that records read count and always returns empty payloads."""

        def __init__(self) -> None:
            super().__init__()
            self.read_count = 0
            self.last_timeout: float | None = None

        def read_gatt_char(self, *_args: Any, **_kwargs: Any) -> bytes:
            self.read_count += 1
            timeout_value = _kwargs.get("timeout")
            self.last_timeout = cast(float | None, timeout_value)
            return b""

    client = EmptyReadClient()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)

    result = iface._read_from_radio_with_retries(
        cast(BLEClient, client),
        retry_on_empty=False,
    )

    assert result is None
    assert client.read_count == 1
    assert client.last_timeout == ble_mod.BLEConfig.RECEIVE_WAIT_TIMEOUT

    iface.close()


def test_close_unsubscribes_tracked_notifications(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """close() should best-effort stop tracked notifications before client teardown."""
    client = DummyClient()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)

    iface._register_notifications(cast(BLEClient, client))
    assert len(iface._notification_manager) > 0

    iface.close()

    assert FROMNUM_UUID in client.stop_notify_calls


def test_reconnect_scheduler_tracks_threads() -> None:
    """ReconnectScheduler should start at most one reconnect thread and respect closing state."""

    state_manager = BLEStateManager()
    shutdown_event = threading.Event()

    class StubCoordinator:
        """Thread coordinator stub used by reconnect scheduler tests."""

        def __init__(self) -> None:
            """Initialize the instance and prepare storage for items created during tests.

            Creates an empty `created` list used to record items that this helper constructs.
            """
            self.created: list[SimpleNamespace] = []

        def _create_thread(
            self,
            target: Callable[..., object],
            name: str,
            *,
            daemon: bool = True,
            args: tuple[object, ...] = (),
            kwargs: dict[str, object] | None = None,
        ) -> SimpleNamespace:
            """Create a lightweight thread-like SimpleNamespace, record it in self.created, and return it.

            Parameters
            ----------
            target : callable
                The callable intended to run when the thread is started.
            name : str
                Identifier for the thread-like object.
            daemon : bool
                Whether the thread-like object is considered a daemon. (Default value = True)
            args : tuple
                Positional arguments associated with the target. (Default value = ())
            kwargs : dict | None
                Keyword arguments associated with the target; treated as {} when None. (Default value = None)

            Returns
            -------
            SimpleNamespace
                An object with attributes `target`, `args`, `name`, `daemon`, `kwargs`, and `started`, plus an `is_alive()` callable that returns whether `started` is True.
            """
            thread = SimpleNamespace(
                target=target,
                args=args,
                name=name,
                daemon=daemon,
                kwargs=kwargs if kwargs is not None else {},
                started=False,
            )
            thread.is_alive = lambda: thread.started
            self.created.append(thread)
            return thread

        @staticmethod
        def _start_thread(thread: SimpleNamespace) -> None:
            """Mark a thread-like object's `started` attribute as True.

            Parameters
            ----------
            thread : object
                Thread-like object with a writable `started` attribute.
            """
            thread.started = True

    interface_stub = SimpleNamespace(
        _is_connection_closing=False,
        _can_initiate_connection=True,
    )
    coordinator = StubCoordinator()
    scheduler = ReconnectScheduler(
        state_manager,
        state_manager._lock,
        coordinator,  # type: ignore[arg-type]
        interface_stub,  # type: ignore[arg-type]
    )

    assert scheduler._schedule_reconnect(True, shutdown_event) is True
    assert len(coordinator.created) == 1
    assert scheduler._schedule_reconnect(True, shutdown_event) is False

    scheduler._clear_thread_reference()
    assert scheduler._reconnect_thread is None

    assert state_manager._transition_to(ConnectionState.CONNECTING) is True
    assert state_manager._transition_to(ConnectionState.CONNECTED) is True
    assert state_manager._transition_to(ConnectionState.DISCONNECTING) is True
    interface_stub._is_connection_closing = True
    assert scheduler._schedule_reconnect(True, shutdown_event) is False


def test_reconnect_worker_successful_attempt() -> None:
    """ReconnectWorker should reconnect and clear thread references on success; cleanup/resubscribe are handled by the interface layer, not the worker."""

    class StubPolicy:
        """Reconnect policy stub for successful reconnect tests."""

        def __init__(self) -> None:
            """Initialize the stub retry policy used by reconnect tests.

            Sets initial state for test assertions.

            Attributes
            ----------
            reset_called : bool
                True if reset() has been invoked.
            _attempt_count : int
                Number of connection attempts recorded.
            """
            self.reset_called = False
            self._attempt_count = 0

        def reset(self) -> None:
            """Reset the retry policy to its initial state.

            Sets the internal attempt counter to 0 and records that a reset occurred by setting `reset_called` to True.
            """
            self.reset_called = True
            self._attempt_count = 0

        def get_attempt_count(self) -> int:
            """Return the internal attempt count for ReconnectWorker tests."""
            return self._attempt_count

        def next_attempt(self) -> tuple[float, bool]:
            """Determine the delay before the next retry and whether another attempt should be made.

            Increments the internal attempt counter as a side effect.

            Returns
            -------
            tuple
                (delay_seconds, continue_retry)
                delay_seconds (float): Seconds to wait before the next attempt.
                continue_retry (bool): `True` to perform another attempt, `False` otherwise.
            """
            self._attempt_count += 1
            return 0.1, False

    class DummyInterface:
        """Minimal interface stub used by reconnect worker tests.

        Methods
        -------
        connect(address)
        """

        BLEError = RuntimeError

        def __init__(self) -> None:
            """Create a minimal stub interface for reconnect-related tests.

            Initializes lightweight test doubles and records connect invocations.

            Attributes
            ----------
            _reconnect_policy : StubPolicy
                Retry/backoff policy used by reconnect attempts.
            _notification_manager : _ReconnectTestNotificationManager
                Tracks cleanup and resubscribe requests.
            _state_manager : types.SimpleNamespace
                Exposes `is_closing` (bool) to simulate shutdown state.
            _reconnect_scheduler : _ReconnectTestScheduler
                Manages reconnect thread reference and clearing.
            auto_reconnect : bool
                Whether automatic reconnect attempts are enabled.
            _is_connection_closing : bool
                Simulates an in-progress connection close.
            _is_connection_connected : bool
                Simulates an active connection state.
            address : str
                Device address used for connect attempts.
            client : object
                Placeholder BLE client object.
            connect_calls : list
                Records addresses passed to `connect` for assertions in tests.
            """
            self._reconnect_policy = StubPolicy()
            self._notification_manager = _ReconnectTestNotificationManager()
            self._state_manager = SimpleNamespace(is_closing=False)
            self._reconnect_scheduler = _ReconnectTestScheduler()
            self.auto_reconnect = True
            self._is_connection_closing = False
            self._is_connection_connected = False
            self.address = "addr"
            self.client = object()
            self.connect_calls: list[str] = []

        def connect(self, address: str, **_kwargs: object) -> None:
            """Record that a connection was attempted for the given device address by appending it to this instance's `connect_calls` list.

            Parameters
            ----------
            address : str
                Bluetooth address or device identifier that was attempted and will be appended to `connect_calls`.
            """
            self.connect_calls.append(address)

    iface = DummyInterface()
    worker = ReconnectWorker(iface, iface._reconnect_policy)  # type: ignore[arg-type]
    worker._attempt_reconnect_loop(
        threading.Event(),
        on_exit=iface._reconnect_scheduler._clear_thread_reference,
    )

    assert iface.connect_calls == ["addr"]
    assert iface._notification_manager.cleaned == 0
    assert len(iface._notification_manager.resubscribed) == 0
    assert iface._reconnect_policy.reset_called is True
    assert iface._reconnect_scheduler.cleared is True


def test_reconnect_worker_respects_retry_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure ReconnectWorker respects the retry policy and stops after the allowed attempts when connect continuously fails.

    Simulates an interface whose connect() always raises and a LimitedPolicy that permits a single retry. Verifies that:
    - connect() is attempted the expected number of times (2 attempts),
    - no notification resubscriptions occur,
    - the retry delay from the policy (0.25) was waited once,
    - the reconnect policy was reset,
    - the reconnect scheduler cleared its thread reference.

    Raises
    ------
    BLEError
    """

    sleep_calls: list[float] = []

    # Mock shutdown_event.wait to capture the sleep delay instead of actually waiting
    def mock_wait(timeout: float | None = None) -> bool:
        """Simulate waiting for a shutdown event while recording requested timeouts.

        Records the provided timeout value into the surrounding `sleep_calls` list when not None, and always returns `False` to indicate the wait timed out (not interrupted by a shutdown/notification).

        Parameters
        ----------
        timeout : float | None
            Duration in seconds to wait; if None, no value is recorded. (Default value = None)

        Returns
        -------
        bool
            `False` to indicate a timeout (i.e., the wait was not interrupted).
        """
        if timeout is not None:
            sleep_calls.append(timeout)
        # Return False to simulate timeout (not interrupted by shutdown)
        return False

    class LimitedPolicy:
        """Reconnect policy stub with a bounded retry window."""

        def __init__(self) -> None:
            """Initialize a stub reconnect policy for tests, resetting counters and flags.

            Attributes
            ----------
            reset_called : bool
                True if reset() has been invoked.
            attempts : int
                Number of connection attempts recorded.
            """
            self.reset_called = False
            self.attempts = 0

        def reset(self) -> None:
            """Mark the retry policy as reset and clear its attempt counter.

            Sets the internal `reset_called` flag to True and resets `attempts` to 0.
            """
            self.reset_called = True
            self.attempts = 0

        def get_attempt_count(self) -> int:
            """Return the internal attempt count for ReconnectWorker tests."""
            return self.attempts

        def next_attempt(self) -> tuple[float, bool]:
            """Return the delay before the next retry and whether another retry should be attempted.

            Returns
            -------
            tuple
                (delay_seconds, continue_flag)
                delay_seconds (float): Seconds to wait before the next retry (0.25).
                continue_flag (bool): `True` if another retry should be attempted for the current policy cycle, `False` otherwise.
            """
            self.attempts += 1
            return 0.25, self.attempts < 2

    class FailingInterface:
        """Interface stub whose connect path always fails.

        Methods
        -------
        connect(*_args, **_kwargs)
        """

        BLEError = RuntimeError

        def __init__(self) -> None:
            """Initialize a minimal stub interface used by reconnect tests.

            Attributes
            ----------
            _reconnect_policy : LimitedPolicy
                Policy controlling reconnect attempts.
            _notification_manager : _ReconnectTestNotificationManager
                Manages notification cleanup and resubscription.
            _state_manager : SimpleNamespace
                Runtime state flags (contains `is_closing`).
            _reconnect_scheduler : _ReconnectTestScheduler
                Scheduler that manages reconnect threads.
            auto_reconnect : bool
                Whether automatic reconnect attempts are enabled.
            _is_connection_closing : bool
                Indicates an in-progress connection close.
            _is_connection_connected : bool
                Indicates whether the interface is currently connected.
            address : str
                Remote device address used for connection attempts.
            client : object | None
                Placeholder for the BLE client instance (initially None).
            connect_attempts : int
                Counter of connect() invocation attempts.
            """
            self._reconnect_policy = LimitedPolicy()
            self._notification_manager = _ReconnectTestNotificationManager(
                fail_on_resubscribe=True
            )
            self._state_manager = SimpleNamespace(is_closing=False)
            self._reconnect_scheduler = _ReconnectTestScheduler()
            self.auto_reconnect = True
            self._is_connection_closing = False
            self._is_connection_connected = False
            self.address = "addr"
            self.client = None
            self.connect_attempts = 0

        def connect(self, *_args: object, **_kwargs: object) -> None:
            """Simulate a failing connection attempt for tests and record the attempt.

            Increments the instance's `connect_attempts` counter and raises an error to emulate a failed connection.

            Raises
            ------
            self.BLEError
                raised with message "boom".
            BLEError
            """
            self.connect_attempts += 1
            raise self.BLEError("boom")

    iface = FailingInterface()
    worker = ReconnectWorker(iface, iface._reconnect_policy)  # type: ignore[arg-type]
    shutdown_event = threading.Event()
    monkeypatch.setattr(shutdown_event, "wait", mock_wait)

    worker._attempt_reconnect_loop(
        shutdown_event,
        on_exit=iface._reconnect_scheduler._clear_thread_reference,
    )

    assert iface.connect_attempts == 2
    assert iface._notification_manager.cleaned == 0
    assert sleep_calls == [0.25]
    assert iface._reconnect_policy.reset_called is True
    assert iface._reconnect_scheduler.cleared is True


@pytest.mark.parametrize("failure_hook", ["get_callback", "subscribe"])
def test_register_notifications_rolls_back_backend_if_local_tracking_fails(
    monkeypatch: pytest.MonkeyPatch,
    failure_hook: str,
) -> None:
    """A local FROMNUM tracking failure must unwind the active backend notify."""
    client = _FromNumRegistrationClient()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    try:
        _prepare_provisional_notification_registration(iface, published_client=None)

        if failure_hook == "get_callback":
            monkeypatch.setattr(
                iface._notification_manager,
                "_get_callback",
                lambda _uuid: (_ for _ in ()).throw(
                    RuntimeError("local callback lookup failed")
                ),
            )
        else:
            monkeypatch.setattr(
                iface._notification_manager,
                "_subscribe",
                lambda _uuid, _callback: (_ for _ in ()).throw(
                    RuntimeError("local subscription tracking failed")
                ),
            )

        iface._register_notifications(cast(BLEClient, client))

        assert client.start_notify_calls == [FROMNUM_UUID]
        assert client.stop_notify_calls == [FROMNUM_UUID]
        assert iface._fromnum_notify_enabled is False
        assert not iface._notification_dispatcher._started_notify_characteristics
    finally:
        iface.close()
