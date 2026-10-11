"""BLE receive runtime and restart tests."""

# pylint: disable=redefined-outer-name

import contextlib
import math
import threading
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, Callable, cast
from unittest.mock import MagicMock

import pytest
from bleak.exc import BleakError

# Import meshtastic modules for use in tests
from meshtastic.interfaces.ble import (
    BLEInterface,
)
from tests._ble_interface_core_support import (
    START_FAILED_MSG,
)

# Import common fixtures
from tests.test_ble_interface_fixtures import DummyClient, _build_interface

pytestmark = pytest.mark.unit


def test_transient_read_retry_uses_zero_based_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transient read retries should pass a zero-based attempt index to policy delay."""
    iface = _build_interface(monkeypatch, DummyClient())
    delay_attempts: list[int] = []

    class StubTransientPolicy:
        """Retry policy stub that records delay attempt indexes."""

        def _should_retry(self, attempt: int) -> bool:
            """Decide whether to perform another retry based on the zero-based attempt index.

            Parameters
            ----------
            attempt : int
                Zero-based retry attempt index (0 for the first attempt).

            Returns
            -------
            bool
                `True` if `attempt` is less than 1, `False` otherwise.
            """
            return attempt < 1

        def _get_delay(self, attempt: int) -> float:
            """Record the retry attempt index and return a zero-second retry delay.

            Appends the zero-based `attempt` index to the surrounding test's `delay_attempts` list.

            Parameters
            ----------
            attempt : int
                Zero-based retry attempt index to record.

            Returns
            -------
            float
                Delay in seconds (always 0.0).
            """
            delay_attempts.append(attempt)
            return 0.0

    iface._transient_read_policy = StubTransientPolicy()  # type: ignore[assignment]
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface._sleep", lambda _delay: None
    )

    iface._read_retry_count = 0
    iface._handle_transient_read_error(BleakError("transient"))

    assert iface._read_retry_count == 1
    assert delay_attempts == [0]

    iface.close()


@pytest.mark.parametrize(
    "invalid_delay",
    [
        True,
        -1.0,
        math.nan,
        math.inf,
        -math.inf,
        pytest.param("1.0", id="string"),
        pytest.param(object(), id="opaque-object"),
    ],
)
def test_retry_policy_get_delay_rejects_invalid_numeric_outputs(
    invalid_delay: object,
) -> None:
    """Retry delay helper should clamp invalid bool/non-finite/negative/non-numeric results to 0.0."""

    class InvalidDelayPolicy:
        def get_delay(self, _attempt: int) -> object:
            return invalid_delay

    assert BLEInterface._retry_policy_get_delay(InvalidDelayPolicy(), attempt=0) == 0.0


def test_receive_recovery_backoff_reaches_configured_cap_for_non_power_of_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify recovery backoff reaches configured max for non-power-of-two caps.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Fixture used to patch recovery timing constants.

    Returns
    -------
    None
    """
    import meshtastic.interfaces.ble.receive_service as receive_service_mod

    iface = SimpleNamespace(
        _closed=False,
        _state_manager=SimpleNamespace(is_closing=False),
    )
    iface._is_connection_closing = False
    iface._state_lock = threading.RLock()
    iface.client = None
    iface._handle_disconnect = lambda *_args, **_kwargs: True
    iface._set_receive_wanted = lambda *_args, **_kwargs: None
    iface._receive_recovery_attempts = 4
    iface._last_recovery_time = 100.0
    iface._read_retry_count = 7
    iface._should_run_receive_loop = lambda: True
    iface._start_receive_thread = MagicMock()

    wait_calls: list[float | None] = []

    class _ShutdownEvent:
        def __init__(self) -> None:
            self._set = False

        def is_set(self) -> bool:
            return self._set

        def wait(self, timeout: float | None = None) -> bool:
            wait_calls.append(timeout)
            self._set = True
            return True

    iface._shutdown_event = _ShutdownEvent()

    monkeypatch.setattr(
        receive_service_mod,
        "RECEIVE_RECOVERY_RAPID_FAILURE_THRESHOLD",
        0,
        raising=True,
    )
    monkeypatch.setattr(
        receive_service_mod,
        "RECEIVE_RECOVERY_MAX_BACKOFF_SEC",
        30.0,
        raising=True,
    )
    monkeypatch.setattr(
        receive_service_mod,
        "time",
        SimpleNamespace(monotonic=lambda: 100.0),
        raising=True,
    )

    receive_service_mod.BLEReceiveRecoveryService._recover_receive_thread(
        iface, "receive_thread_fatal"
    )

    assert wait_calls == [30.0]
    assert iface._read_retry_count == 7
    iface._start_receive_thread.assert_not_called()


def test_receive_loop_outer_catch_routes_to_disconnect_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outer receive-loop exceptions should use normal disconnect handling.

    Raises
    ------
    RuntimeError
    """
    client = DummyClient()
    iface = _build_interface(monkeypatch, client)
    disconnect_calls: list[tuple[str, Any | None, Any | None]] = []

    def raising_wait_for_event(_name: str, timeout: float | None = None) -> bool:
        """Simulate a fatal receive-loop failure by always raising a RuntimeError.

        Parameters
        ----------
        _name : str
            Event name (unused in this stub).
        timeout : float | None
            Timeout value (unused in this stub).

        Raises
        ------
        RuntimeError
            Always raised to emulate an unexpected fatal error in the receive loop.
        """
        _ = timeout
        raise RuntimeError("fatal receive loop failure")

    def fake_handle_disconnect(
        source: str,
        client: Any | None = None,
        bleak_client: Any | None = None,
    ) -> bool:
        """Record the disconnect invocation and stop the receive loop.

        Parameters
        ----------
        source : str
        client : Any | None
        bleak_client : Any | None

        Returns
        -------
        bool
            `False` indicating the handler did not handle the disconnect.
        """
        disconnect_calls.append((source, client, bleak_client))
        iface._want_receive = False
        return False

    monkeypatch.setattr(
        iface.thread_coordinator,
        "_wait_for_event",
        raising_wait_for_event,
        raising=True,
    )
    monkeypatch.setattr(
        iface, "_handle_disconnect", fake_handle_disconnect, raising=True
    )

    iface._want_receive = True
    iface._receive_from_radio_impl()

    assert disconnect_calls
    source, disconnected_client, disconnected_bleak = disconnect_calls[0]
    assert source == "receive_thread_fatal"
    assert disconnected_client is client
    assert disconnected_bleak is None

    iface.close()


def test_receive_loop_waits_while_publish_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receive loop should pause reads while connect publication is pending."""
    client = DummyClient()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    wait_events: list[str] = []

    def _wait_for_event(event_name: str, timeout: float | None = None) -> bool:
        _ = timeout
        wait_events.append(event_name)
        if event_name == "read_trigger":
            return True
        if event_name == "reconnected_event":
            iface._want_receive = False
            return False
        return False

    monkeypatch.setattr(
        iface.thread_coordinator,
        "_wait_for_event",
        _wait_for_event,
        raising=True,
    )
    monkeypatch.setattr(
        iface.thread_coordinator,
        "_clear_event",
        lambda _event_name: None,
        raising=True,
    )
    monkeypatch.setattr(
        iface,
        "_read_from_radio_with_retries",
        lambda *_args, **_kwargs: pytest.fail(
            "read should be skipped while publish pending"
        ),
        raising=True,
    )

    with iface._state_lock:
        iface.client = cast(Any, client)
        iface._client_publish_pending = True
    iface._want_receive = True

    iface._receive_from_radio_impl()

    assert "reconnected_event" in wait_events
    iface.close()


def test_receive_loop_waits_for_reconnect_when_client_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receive loop should wait on reconnected_event when client is missing and auto-reconnect is enabled."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    wait_events: list[str] = []

    def _wait_for_event(event_name: str, timeout: float | None = None) -> bool:
        _ = timeout
        wait_events.append(event_name)
        if event_name == "read_trigger":
            return True
        if event_name == "reconnected_event":
            iface._want_receive = False
            return False
        return False

    monkeypatch.setattr(
        iface.thread_coordinator,
        "_wait_for_event",
        _wait_for_event,
        raising=True,
    )
    monkeypatch.setattr(
        iface.thread_coordinator,
        "_clear_event",
        lambda _event_name: None,
        raising=True,
    )

    with iface._state_lock:
        iface.client = None
        iface._client_publish_pending = False
    iface.auto_reconnect = True
    iface._want_receive = True

    iface._receive_from_radio_impl()

    assert wait_events.count("reconnected_event") >= 1
    iface.close()


def test_start_receive_thread_skips_when_interface_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receive thread creation should run while open and stop after close()."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    try:
        monkeypatch.setattr(
            type(iface),
            "_start_receive_thread",
            BLEInterface._start_receive_thread,
            raising=True,
        )
        create_calls: list[dict[str, object]] = []
        thread_like = SimpleNamespace(
            name="BLEReceivePositiveControl",
            ident=123,
            is_alive=lambda: True,
        )

        def _create_thread(**kwargs: object) -> object:
            create_calls.append(dict(kwargs))
            return thread_like

        monkeypatch.setattr(
            iface.thread_coordinator,
            "_create_thread",
            _create_thread,
            raising=True,
        )
        monkeypatch.setattr(
            iface.thread_coordinator,
            "_start_thread",
            lambda _thread: None,
            raising=True,
        )
        with iface._state_lock:
            iface._want_receive = True

        iface._start_receive_thread(name="BLEReceiveWhileOpen")
        assert [call["name"] for call in create_calls] == ["BLEReceiveWhileOpen"]

        iface.close()
        create_calls.clear()
        iface._start_receive_thread(name="BLEReceiveAfterClose")
        assert create_calls == []
    finally:
        iface.close()


@pytest.mark.parametrize(
    "invoke_start",
    ["service", "facade"],
)
def test_start_receive_thread_clears_cached_thread_when_start_fails(
    monkeypatch: pytest.MonkeyPatch,
    invoke_start: str,
) -> None:
    """Verify start failures clear cached receive-thread references for service/facade paths."""
    from meshtastic.interfaces.ble.interface import BLEInterface
    from meshtastic.interfaces.ble.lifecycle_service import BLELifecycleService

    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    try:
        if invoke_start == "facade":
            monkeypatch.setattr(
                type(iface),
                "_start_receive_thread",
                BLEInterface._start_receive_thread,
                raising=True,
            )

        with iface._state_lock:
            iface._want_receive = True
        thread_like = SimpleNamespace(
            name="BLEReceiveStartFailure",
            ident=None,
            is_alive=lambda: False,
        )
        monkeypatch.setattr(
            iface.thread_coordinator,
            "_create_thread",
            lambda **_kwargs: thread_like,
            raising=True,
        )

        def _raise_start_failure(_thread: object) -> None:
            assert _thread is thread_like
            assert iface._receiveThread is cast(Any, thread_like)
            raise RuntimeError(START_FAILED_MSG)

        monkeypatch.setattr(
            iface.thread_coordinator,
            "_start_thread",
            _raise_start_failure,
            raising=True,
        )

        if invoke_start == "facade":

            def start_receive(*, name: str) -> None:
                iface._start_receive_thread(name=name)

        else:

            def start_receive(*, name: str) -> None:
                BLELifecycleService._start_receive_thread(iface, name=name)

        with pytest.raises(RuntimeError, match=START_FAILED_MSG):
            start_receive(name="BLEReceiveStartFailure")

        assert iface._receiveThread is None
    finally:
        iface.close()


@contextlib.contextmanager
def _snapshot_receive_start_state(iface: BLEInterface) -> Iterator[None]:
    """Snapshot and restore receive-start thread/pending flags for tests."""
    with iface._state_lock:
        original_want_receive = iface._want_receive
        original_receive_recovery_attempts = iface._receive_recovery_attempts
        original_receive_thread = iface._receiveThread
        original_receive_start_pending = iface._receive_start_pending
        original_receive_start_pending_since = iface._receive_start_pending_since
    try:
        yield
    finally:
        with iface._state_lock:
            iface._want_receive = original_want_receive
            iface._receive_recovery_attempts = original_receive_recovery_attempts
            iface._receiveThread = original_receive_thread
            iface._receive_start_pending = original_receive_start_pending
            iface._receive_start_pending_since = original_receive_start_pending_since


def _patch_receive_start_monotonic(
    monkeypatch: pytest.MonkeyPatch,
    *,
    initial: float,
) -> SimpleNamespace:
    """Install a module-local, explicitly settable monotonic test clock.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Fixture used to replace the lifecycle runtime's ``time`` module reference.
    initial : float
        Initial timestamp returned by the fake clock.

    Returns
    -------
    SimpleNamespace
        Mutable clock state. Assign to ``value`` before each operation whose
        timing boundary matters.
    """
    import meshtastic.interfaces.ble.lifecycle_receive_runtime as lifecycle_receive_runtime

    clock = SimpleNamespace(value=initial)
    real_time = vars(lifecycle_receive_runtime)["time"]

    def _monotonic() -> float:
        return float(clock.value)

    monkeypatch.setattr(
        lifecycle_receive_runtime,
        "time",
        SimpleNamespace(monotonic=_monotonic, sleep=real_time.sleep),
        raising=True,
    )
    return clock


def _patch_receive_start_threads(
    monkeypatch: pytest.MonkeyPatch,
    *,
    iface: BLEInterface,
    created_threads: list[object],
    on_start: Callable[[object], None] | None = None,
) -> tuple[list[dict[str, object]], list[object]]:
    """Patch thread coordinator create/start hooks for receive-start tests."""
    thread_queue = list(created_threads)
    create_calls: list[dict[str, object]] = []

    def _create_thread(**kwargs: object) -> object:
        create_calls.append(dict(kwargs))
        if not thread_queue:
            raise ValueError(
                "thread_queue is empty in _create_thread - test setup/behavior mismatch"
            )
        return thread_queue.pop(0)

    monkeypatch.setattr(
        iface.thread_coordinator,
        "_create_thread",
        _create_thread,
        raising=True,
    )

    start_calls: list[object] = []

    def _start_thread(thread: object) -> None:
        start_calls.append(thread)
        if on_start is not None:
            on_start(thread)

    monkeypatch.setattr(
        iface.thread_coordinator,
        "_start_thread",
        _start_thread,
        raising=True,
    )
    return create_calls, start_calls


def _run_receive_pending_marker_scenario(
    monkeypatch: pytest.MonkeyPatch,
    *,
    iface: BLEInterface,
    start_receive: Callable[[str], None],
    prefix: str,
    t0: float,
) -> None:
    """Run pending-marker restart scenario for service/facade receive starts."""
    from meshtastic.interfaces.ble.lifecycle_receive_runtime import (
        RECEIVE_START_PENDING_TIMEOUT_SECONDS,
        BLEReceiveLifecycleCoordinator,
    )

    deferred_schedule_calls: list[dict[str, object]] = []

    def _spy_schedule_deferred_receive_restart(
        _self: BLEReceiveLifecycleCoordinator, **kwargs: object
    ) -> None:
        deferred_schedule_calls.append(dict(kwargs))

    monkeypatch.setattr(
        BLEReceiveLifecycleCoordinator,
        "_schedule_deferred_receive_restart",
        _spy_schedule_deferred_receive_restart,
        raising=True,
    )

    thread_one = SimpleNamespace(
        name=f"{prefix}One",
        ident=None,
        is_alive=lambda: False,
    )
    thread_two = SimpleNamespace(
        name=f"{prefix}Two",
        ident=42,
        is_alive=lambda: True,
    )
    create_calls, start_calls = _patch_receive_start_threads(
        monkeypatch,
        iface=iface,
        created_threads=[thread_one, thread_two],
    )

    timeout = RECEIVE_START_PENDING_TIMEOUT_SECONDS
    small_delta = max(min(timeout / 10.0, 0.01), 1e-6)
    clock = _patch_receive_start_monotonic(monkeypatch, initial=t0)

    clock.value = t0
    start_receive(f"{prefix}One")
    assert [call["name"] for call in create_calls] == [f"{prefix}One"]
    assert all(
        getattr(call["target"], "__name__", None) == "_receive_from_radio_impl"
        for call in create_calls
    )
    assert all(call["daemon"] is True for call in create_calls)
    assert start_calls == [thread_one]
    assert iface._receiveThread is cast(Any, thread_one)
    assert iface._receive_start_pending is True

    clock.value = t0 + small_delta
    start_receive(f"{prefix}Skip")
    assert [call["name"] for call in create_calls] == [f"{prefix}One"]
    assert all(
        getattr(call["target"], "__name__", None) == "_receive_from_radio_impl"
        for call in create_calls
    )
    assert all(call["daemon"] is True for call in create_calls)
    assert start_calls == [thread_one]
    assert iface._receiveThread is cast(Any, thread_one)
    assert iface._receive_start_pending is True

    clock.value = t0 + timeout - small_delta
    start_receive(f"{prefix}StillWithinTimeout")
    assert [call["name"] for call in create_calls] == [f"{prefix}One"]
    assert all(
        getattr(call["target"], "__name__", None) == "_receive_from_radio_impl"
        for call in create_calls
    )
    assert all(call["daemon"] is True for call in create_calls)
    assert start_calls == [thread_one]
    assert iface._receiveThread is cast(Any, thread_one)
    assert iface._receive_start_pending is True

    clock.value = t0 + timeout + small_delta
    start_receive(f"{prefix}Two")
    assert [call["name"] for call in create_calls] == [f"{prefix}One", f"{prefix}Two"]
    assert all(
        getattr(call["target"], "__name__", None) == "_receive_from_radio_impl"
        for call in create_calls
    )
    assert all(call["daemon"] is True for call in create_calls)
    assert start_calls == [thread_one, thread_two]
    assert iface._receiveThread is cast(Any, thread_two)
    assert iface._receive_start_pending is False
    assert len(deferred_schedule_calls) == 1


def _run_receive_current_thread_deferral_scenario(
    monkeypatch: pytest.MonkeyPatch,
    *,
    iface: BLEInterface,
    start_receive: Callable[[str], None],
    deferred_name: str,
    t0: float,
) -> None:
    """Run current-thread deferral scenario for service/facade receive starts."""
    from meshtastic.interfaces.ble.lifecycle_receive_runtime import (
        BLEReceiveLifecycleCoordinator,
    )

    deferred_schedule_calls: list[dict[str, object]] = []

    def _spy_schedule_deferred_receive_restart(
        _self: BLEReceiveLifecycleCoordinator, **kwargs: object
    ) -> None:
        deferred_schedule_calls.append(dict(kwargs))

    monkeypatch.setattr(
        BLEReceiveLifecycleCoordinator,
        "_schedule_deferred_receive_restart",
        _spy_schedule_deferred_receive_restart,
        raising=True,
    )

    with iface._state_lock:
        iface._want_receive = True
        iface._receiveThread = threading.current_thread()
        iface._receive_start_pending = False
        iface._receive_start_pending_since = None

    deferred_thread = SimpleNamespace(
        name=deferred_name,
        ident=123,
        is_alive=lambda: True,
    )
    create_calls, start_calls = _patch_receive_start_threads(
        monkeypatch,
        iface=iface,
        created_threads=[deferred_thread],
    )
    clock = _patch_receive_start_monotonic(monkeypatch, initial=t0)
    clock.value = t0

    start_receive(deferred_name)

    assert create_calls == []
    assert start_calls == []
    assert iface._receiveThread is threading.current_thread()
    assert iface._receive_start_pending is True
    assert iface._receive_start_pending_since == t0
    assert len(deferred_schedule_calls) == 1


def test_start_receive_thread_retains_cached_thread_when_start_noops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify no-op thread starts retain cached receive-thread placeholders.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Fixture used to patch thread start behavior.

    Returns
    -------
    None
    """
    from meshtastic.interfaces.ble.lifecycle_service import BLELifecycleService

    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    try:
        with _snapshot_receive_start_state(iface):
            with iface._state_lock:
                iface._want_receive = True
            thread_like = SimpleNamespace(
                name="BLEReceiveStartNoop",
                ident=None,
                is_alive=lambda: False,
            )

            def _assert_cached_thread(thread: object) -> None:
                assert thread is thread_like
                assert iface._receiveThread is cast(Any, thread_like)

            _, start_calls = _patch_receive_start_threads(
                monkeypatch,
                iface=iface,
                created_threads=[thread_like],
                on_start=_assert_cached_thread,
            )

            BLELifecycleService._start_receive_thread(iface, name="BLEReceiveStartNoop")

            assert start_calls == [thread_like]
            assert iface._receiveThread is cast(Any, thread_like)
    finally:
        iface.close()


def test_start_receive_thread_pending_marker_allows_later_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify inconclusive start markers allow restart after pending-timeout expiry."""
    from meshtastic.interfaces.ble.lifecycle_service import BLELifecycleService

    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    try:
        with _snapshot_receive_start_state(iface):
            with iface._state_lock:
                iface._want_receive = True
            _run_receive_pending_marker_scenario(
                monkeypatch,
                iface=iface,
                start_receive=lambda name: BLELifecycleService._start_receive_thread(
                    iface, name=name
                ),
                prefix="BLEReceivePending",
                t0=100.0,
            )
    finally:
        iface.close()


def test_start_receive_thread_from_current_thread_defers_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restart requests from the active receive thread should defer replacement."""
    from meshtastic.interfaces.ble.lifecycle_service import BLELifecycleService

    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    try:
        with _snapshot_receive_start_state(iface):
            _run_receive_current_thread_deferral_scenario(
                monkeypatch,
                iface=iface,
                start_receive=lambda name: BLELifecycleService._start_receive_thread(
                    iface, name=name
                ),
                deferred_name="BLEReceiveDeferred",
                t0=200.0,
            )
    finally:
        iface.close()


def test_start_receive_thread_pending_marker_via_interface_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`BLEInterface._start_receive_thread` should preserve pending-marker behavior."""
    from meshtastic.interfaces.ble.interface import BLEInterface

    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    try:
        monkeypatch.setattr(
            type(iface),
            "_start_receive_thread",
            BLEInterface._start_receive_thread,
            raising=True,
        )
        with _snapshot_receive_start_state(iface):
            with iface._state_lock:
                iface._want_receive = True
            _run_receive_pending_marker_scenario(
                monkeypatch,
                iface=iface,
                start_receive=lambda name: iface._start_receive_thread(name=name),
                prefix="BLEReceivePendingFacade",
                t0=310.0,
            )
    finally:
        iface.close()


def test_start_receive_thread_current_thread_defers_via_interface_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`BLEInterface._start_receive_thread` should defer current-thread restarts."""
    from meshtastic.interfaces.ble.interface import BLEInterface

    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    try:
        monkeypatch.setattr(
            type(iface),
            "_start_receive_thread",
            BLEInterface._start_receive_thread,
            raising=True,
        )
        with _snapshot_receive_start_state(iface):
            _run_receive_current_thread_deferral_scenario(
                monkeypatch,
                iface=iface,
                start_receive=lambda name: iface._start_receive_thread(name=name),
                deferred_name="BLEReceiveDeferredFacade",
                t0=410.0,
            )
    finally:
        iface.close()


def test_start_receive_thread_ident_only_probe_keeps_pending_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ident-only start probes should not clear pending marker or recovery attempts."""
    from meshtastic.interfaces.ble.lifecycle_service import BLELifecycleService

    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    try:
        with _snapshot_receive_start_state(iface):
            with iface._state_lock:
                iface._want_receive = True
                iface._receive_recovery_attempts = 4
            t0 = 500.0
            clock = _patch_receive_start_monotonic(monkeypatch, initial=t0)
            clock.value = t0
            thread_like = SimpleNamespace(
                name="BLEReceiveIdentOnly",
                ident=101,
                is_alive=lambda: False,
            )
            _, start_calls = _patch_receive_start_threads(
                monkeypatch,
                iface=iface,
                created_threads=[thread_like],
            )

            BLELifecycleService._start_receive_thread(iface, name="BLEReceiveIdentOnly")

            assert iface._receiveThread is cast(Any, thread_like)
            assert iface._receive_start_pending is True
            assert iface._receive_start_pending_since == t0
            assert iface._receive_recovery_attempts == 4
            assert start_calls == [thread_like]
    finally:
        iface.close()
