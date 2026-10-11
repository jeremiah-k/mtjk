"""Tests for owned BLE session-state compatibility views."""

import logging
import threading
import time
from collections.abc import Callable
from types import SimpleNamespace, TracebackType
from typing import Any, cast

import pytest
from bleak.exc import BleakError

from meshtastic.interfaces.ble.coordination import ThreadLike
from meshtastic.interfaces.ble.interface import BLEInterface
from meshtastic.interfaces.ble.lifecycle_controller_runtime import (
    BLELifecycleController,
)
from meshtastic.interfaces.ble.ports import _BLESessionStatePort, _LockPort
from meshtastic.interfaces.ble.session_state import (
    LEGACY_SESSION_STATE_CACHE_ERROR,
    BLESessionState,
    _LegacyBLESessionStateAdapter,
    _session_state_for,
)
from meshtastic.interfaces.ble.state import BLEStateManager, ConnectionState

pytestmark = pytest.mark.unit


class _TrackingRLock:
    """Reentrant lock test double exposing aggregate ownership state."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._depth = 0

    @property
    def held(self) -> bool:
        """Return whether at least one reentrant acquisition is active."""
        return self._depth > 0

    def __enter__(self) -> "_TrackingRLock":
        self._lock.acquire()
        self._depth += 1
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self._depth -= 1
        self._lock.release()


def _bare_interface() -> BLEInterface:
    """Create an uninitialized interface with only owned state collaborators."""
    iface = BLEInterface.__new__(BLEInterface)
    state_manager = BLEStateManager()
    iface._state_manager = state_manager
    iface._session_state = BLESessionState(lock=state_manager.lock)
    return iface


def test_interface_lifecycle_fields_delegate_to_session_state() -> None:
    """Historical private fields should remain writable views of owned state."""
    iface = _bare_interface()

    iface._closed = True
    iface._disconnect_notified = True
    iface._client_publish_pending = True
    iface._last_disconnect_source = "test"
    iface._connection_session_epoch = 7
    iface._want_receive = False
    iface._receive_start_pending = True
    iface._receive_start_pending_since = 12.5

    assert iface._state_lock is iface._state_manager.lock
    assert iface._session_state.closed is True
    assert iface._session_state.disconnect_notified is True
    assert iface._session_state.client_publish_pending is True
    assert iface._session_state.last_disconnect_source == "test"
    assert iface._session_state.connection_session_epoch == 7
    assert iface._session_state.want_receive is False
    assert iface._session_state.receive_start_pending is True
    assert iface._session_state.receive_start_pending_since == 12.5

    with pytest.raises(
        TypeError,
        match="_last_disconnect_source must be a str or None, got int",
    ):
        iface._last_disconnect_source = 7  # type: ignore[assignment]


def test_session_state_retry_reset_helpers() -> None:
    """Retry/recovery bookkeeping resets should be explicit and complete."""
    state_manager = BLEStateManager()
    state = BLESessionState(lock=state_manager.lock)
    state.read_retry_count = 3
    state.last_empty_read_warning = 9.5
    state.suppressed_empty_read_warnings = 4
    state.receive_recovery_attempts = 2
    state.last_recovery_time = 11.0

    state._reset_receive_retry_state()
    state._reset_recovery_state()

    assert state.read_retry_count == 0
    assert state.last_empty_read_warning == 0.0
    assert state.suppressed_empty_read_warnings == 0
    assert state.receive_recovery_attempts == 0
    assert state.last_recovery_time == 0.0


def test_lazy_session_state_creation_converges_across_racing_callers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent lazy access must return one canonical session-state owner."""
    import meshtastic.interfaces.ble.session_state as session_state_module

    iface = BLEInterface.__new__(BLEInterface)
    rendezvous = threading.Barrier(2)
    original_get_declared_lock: Callable[[object | None, str], _LockPort | None] = vars(
        session_state_module
    )["_get_declared_lock"]

    def _racing_get_declared_lock(target: object, name: str) -> object | None:
        if target is None and name == "lock":
            rendezvous.wait(timeout=2.0)
            return None
        return original_get_declared_lock(target, name)

    monkeypatch.setattr(
        session_state_module,
        "_get_declared_lock",
        _racing_get_declared_lock,
    )
    states: list[BLESessionState] = []
    failures: list[BaseException] = []

    def _resolve_state() -> None:
        try:
            states.append(iface._get_session_state())  # noqa: SLF001
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            failures.append(exc)

    workers = [threading.Thread(target=_resolve_state) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=2.0)

    assert all(not worker.is_alive() for worker in workers)
    assert not failures, failures
    assert len(states) == 2
    assert states[0] is states[1]
    assert iface.__dict__["_session_state"] is states[0]


def test_legacy_session_state_resolution_is_atomic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Racing legacy coordinators must converge on one cached adapter owner."""
    import meshtastic.interfaces.ble.session_state as session_state_module

    iface = SimpleNamespace()
    rendezvous = threading.Barrier(2)

    class _RacingAdapter(_LegacyBLESessionStateAdapter):
        def __init__(self, target: object) -> None:
            super().__init__(target)
            rendezvous.wait(timeout=2.0)

    monkeypatch.setattr(
        session_state_module,
        "_LegacyBLESessionStateAdapter",
        _RacingAdapter,
    )
    states: list[object] = []
    failures: list[BaseException] = []

    def _resolve_state() -> None:
        try:
            states.append(session_state_module._session_state_for(iface))
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            failures.append(exc)

    workers = [threading.Thread(target=_resolve_state) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=2.0)

    assert all(not worker.is_alive() for worker in workers)
    assert not failures, failures
    assert len(states) == 2
    assert states[0] is states[1]
    assert iface.__dict__["_session_state"] is states[0]


def test_legacy_adapter_field_map_matches_session_state_protocol() -> None:
    """The cast-backed legacy adapter must cover every protocol data field."""
    protocol_fields = {
        field_name
        for protocol_base in _BLESessionStatePort.__mro__
        for field_name in getattr(protocol_base, "__annotations__", {})
    }

    assert (
        set(_LegacyBLESessionStateAdapter._FIELD_MAP) == protocol_fields
    )  # noqa: SLF001
    assert set(_LegacyBLESessionStateAdapter._FIELD_DEFAULTS) == (  # noqa: SLF001
        protocol_fields - {"lock"}
    )


def test_legacy_adapter_rejects_unknown_fields_with_context() -> None:
    """Unsupported adapter fields should fail with an actionable message."""
    state = _session_state_for(SimpleNamespace())

    with pytest.raises(AttributeError, match="does not proxy session field 'unknown'"):
        _ = state.unknown  # type: ignore[attr-defined]
    with pytest.raises(AttributeError, match="does not proxy session field 'unknown'"):
        state.unknown = True  # type: ignore[attr-defined]


def test_lifecycle_controller_shares_owned_session_state() -> None:
    """Lifecycle coordinators should share the interface's single state owner."""
    iface = _bare_interface()

    controller = BLELifecycleController(iface)

    assert controller._receive._session is iface._session_state
    assert controller._disconnect._session is iface._session_state
    assert controller._connection_ownership._session is iface._session_state
    assert controller._shutdown._session is iface._session_state


def test_lazy_session_state_ignores_dynamically_synthesized_lock() -> None:
    """Lazy state creation should only reuse an explicitly declared state lock."""

    class DynamicStateManager:
        def __getattr__(self, _name: str) -> object:
            return self

        def acquire(self) -> None:
            raise AssertionError("dynamic lock should not be used")

        def release(self) -> None:
            raise AssertionError("dynamic lock should not be used")

    iface = BLEInterface.__new__(BLEInterface)
    dynamic_manager = DynamicStateManager()
    iface._state_manager = dynamic_manager

    state = iface._get_session_state()
    session_lock = cast(Any, state.lock)

    assert session_lock is not dynamic_manager
    assert session_lock.acquire(blocking=False)
    session_lock.release()


def test_legacy_session_state_adapter_implements_reset_contract() -> None:
    """Legacy interfaces should satisfy the complete session-state reset port."""
    legacy = SimpleNamespace(
        _state_lock=threading.RLock(),
        _read_retry_count=5,
        _last_empty_read_warning=7.5,
        _suppressed_empty_read_warnings=3,
        _receive_recovery_attempts=4,
        _last_recovery_time=12.0,
    )

    state = _session_state_for(legacy)
    assert _session_state_for(legacy) is state
    assert state.lock is legacy._state_lock

    state._reset_read_retry_count()
    assert legacy._read_retry_count == 0

    legacy._read_retry_count = 2
    state._reset_receive_retry_state()
    assert legacy._read_retry_count == 0
    assert legacy._last_empty_read_warning == 0.0
    assert legacy._suppressed_empty_read_warnings == 0

    state._reset_recovery_state()
    assert legacy._receive_recovery_attempts == 0
    assert legacy._last_recovery_time == 0.0


def test_legacy_session_state_adapter_tracks_replaced_declared_lock() -> None:
    """Legacy adapters should follow the interface's current declared state lock."""
    first_lock = threading.RLock()
    replacement_lock = threading.RLock()
    legacy = SimpleNamespace(_state_lock=first_lock)

    state = _session_state_for(legacy)
    assert state.lock is first_lock

    legacy._state_lock = replacement_lock

    assert state.lock is replacement_lock


def test_partial_interface_resolves_mixin_session_owner_directly() -> None:
    """Partial BLE interfaces should not create a competing legacy adapter."""
    iface = BLEInterface.__new__(BLEInterface)

    state = _session_state_for(iface)

    assert isinstance(state, BLESessionState)
    assert state is iface._get_session_state()
    assert state.lock is iface._state_lock


def test_mixin_promotion_preserves_legacy_values_and_state_manager_lock() -> None:
    """Adapter promotion must preserve legacy values and canonical lock ownership."""
    iface = BLEInterface.__new__(BLEInterface)
    state_manager = BLEStateManager()
    iface.__dict__["_state_manager"] = state_manager
    iface.__dict__["_closed"] = True
    iface.__dict__["_connection_session_epoch"] = 17
    iface.__dict__["_want_receive"] = False
    iface.__dict__["_last_disconnect_source"] = "legacy-callback"
    adapter = _LegacyBLESessionStateAdapter(iface)
    iface.__dict__["_session_state"] = adapter

    assert adapter.lock is state_manager.lock
    state = iface._get_session_state()

    assert isinstance(state, BLESessionState)
    assert state.lock is state_manager.lock
    assert iface._state_lock is state_manager.lock
    assert state.closed is True
    assert state.connection_session_epoch == 17
    assert state.want_receive is False
    assert state.last_disconnect_source == "legacy-callback"
    assert "_closed" not in iface.__dict__
    assert "_connection_session_epoch" not in iface.__dict__
    assert "_want_receive" not in iface.__dict__
    assert "_last_disconnect_source" not in iface.__dict__

    replacement_lock = threading.RLock()
    iface._state_lock = replacement_lock

    assert state.lock is replacement_lock
    assert adapter.lock is replacement_lock


def test_mixin_promotion_preserves_raw_legacy_lock_without_state_manager() -> None:
    """A pre-migration raw state lock must survive adapter promotion."""
    iface = BLEInterface.__new__(BLEInterface)
    legacy_lock = threading.RLock()
    iface.__dict__["_state_lock"] = legacy_lock
    iface.__dict__["_closed"] = True
    adapter = _LegacyBLESessionStateAdapter(iface)
    iface.__dict__["_session_state"] = adapter

    assert adapter.lock is legacy_lock
    state = iface._get_session_state()

    assert state.lock is legacy_lock
    assert state.closed is True
    assert "_state_lock" not in iface.__dict__
    assert "_closed" not in iface.__dict__


def test_mixin_promotion_without_declared_lock_reuses_adapter_fallback() -> None:
    """Promotion without a declared owner lock must keep the adapter's one lock."""
    iface = BLEInterface.__new__(BLEInterface)
    adapter = _LegacyBLESessionStateAdapter(iface)
    iface.__dict__["_session_state"] = adapter

    fallback_lock = adapter.lock
    state = iface._get_session_state()

    assert isinstance(state, BLESessionState)
    assert state.lock is fallback_lock
    assert adapter.lock is fallback_lock


def test_uncacheable_legacy_collaborator_requires_explicit_session() -> None:
    """Slot-only legacy collaborators must not create competing implicit owners."""

    class _SlotLegacy:
        __slots__ = ("_closed", "_receiveThread")

        def __init__(self) -> None:
            self._closed = False
            self._receiveThread = None

    legacy = _SlotLegacy()
    explicit = BLESessionState(lock=threading.RLock())

    with pytest.raises(TypeError) as exc_info:
        _session_state_for(legacy)

    assert str(exc_info.value) == LEGACY_SESSION_STATE_CACHE_ERROR
    assert _session_state_for(legacy, explicit) is explicit


def test_disconnect_reconnect_scheduler_missing_on_partial_interface() -> None:
    """Partial interfaces should fail with the stable missing-scheduler contract."""
    from threading import Event, RLock

    from meshtastic.interfaces.ble.lifecycle_disconnect_runtime import (
        BLEDisconnectLifecycleCoordinator,
    )
    from meshtastic.interfaces.ble.lifecycle_primitives import (
        RECONNECT_SCHEDULER_MISSING_MSG,
    )

    state = BLESessionState(lock=RLock())
    iface = cast(
        BLEInterface, SimpleNamespace(auto_reconnect=True, _shutdown_event=Event())
    )
    coordinator = BLEDisconnectLifecycleCoordinator(iface, session_state=state)

    with pytest.raises(AttributeError) as exc_info:
        coordinator.schedule_auto_reconnect(is_closing_getter=lambda: False)

    assert str(exc_info.value) == RECONNECT_SCHEDULER_MISSING_MSG


def test_receive_lifecycle_uses_explicit_session_pending_markers() -> None:
    """Explicit session state must govern stale/pending receive-start decisions."""
    from meshtastic.interfaces.ble.lifecycle_receive_runtime import (
        BLEReceiveLifecycleCoordinator,
    )

    state_manager = BLEStateManager()
    state = BLESessionState(lock=state_manager.lock)
    pending_thread = cast(
        ThreadLike,
        SimpleNamespace(name="PendingReceive", ident=None, is_alive=lambda: False),
    )
    state.receive_thread = pending_thread
    state.receive_start_pending = True
    state.receive_start_pending_since = 10**12  # safely in the future for this probe

    # Deliberately contradictory legacy fields prove the coordinator reads the
    # explicit session owner rather than compatibility fields on the interface.
    iface = cast(
        BLEInterface,
        SimpleNamespace(
            _receive_start_pending=False,
            _receive_start_pending_since=None,
        ),
    )
    coordinator = BLEReceiveLifecycleCoordinator(iface, session_state=state)

    def _unexpected_create(**_kwargs: object) -> ThreadLike:
        raise AssertionError("pending session state should suppress thread creation")

    created, recovery_attempts = (
        coordinator._check_receive_start_conditions(  # noqa: SLF001
            name="PendingReceive",
            reset_recovery=False,
            create_runtime_thread=_unexpected_create,
        )
    )

    assert created is None
    assert recovery_attempts is None
    assert state.receive_start_pending is True


def test_receive_controller_reads_ever_connected_from_explicit_session() -> None:
    """Receive reconnect detection should use the shared session owner."""
    from meshtastic.interfaces.ble.receive_service import BLEReceiveRecoveryController

    state_manager = BLEStateManager()
    state = BLESessionState(lock=state_manager.lock, ever_connected=True)
    iface = cast(BLEInterface, SimpleNamespace(_ever_connected=False))

    controller = BLEReceiveRecoveryController(iface, session_state=state)

    assert controller._has_ever_connected_session() is True  # noqa: SLF001


def test_receive_controller_invalid_lifecycle_results_fall_back_to_session() -> None:
    """Non-bool lifecycle probes must not override authoritative session state."""
    from meshtastic.interfaces.ble.receive_service import BLEReceiveRecoveryController

    state_manager = BLEStateManager()
    state = BLESessionState(lock=state_manager.lock, ever_connected=True, closed=True)
    lifecycle = SimpleNamespace(
        _has_ever_connected_session=lambda: None,
        _is_connection_closing=lambda: object(),
    )
    iface = cast(
        BLEInterface,
        SimpleNamespace(
            _get_lifecycle_controller=lambda: lifecycle,
            _state_manager=None,
            _is_connection_closing=False,
        ),
    )
    controller = BLEReceiveRecoveryController(iface, session_state=state)

    assert controller._has_ever_connected_session() is True  # noqa: SLF001
    assert controller._is_connection_closing() is True  # noqa: SLF001


@pytest.mark.parametrize(
    "first_decision",
    [True, False],
    ids=["stale-retry", "stale-terminal"],
)
def test_transient_retry_revalidates_concurrent_counter_reset(
    monkeypatch: pytest.MonkeyPatch,
    first_decision: bool,
) -> None:
    """Concurrent retry resets must invalidate either stale policy decision."""
    import meshtastic.interfaces.ble.receive_service as receive_service_module
    from meshtastic.interfaces.ble.receive_service import BLEReceiveRecoveryController

    state_manager = BLEStateManager()
    state = BLESessionState(lock=state_manager.lock, read_retry_count=3)
    policy_attempts: list[int] = []
    delay_attempts: list[int] = []

    def _should_retry(_policy: object, attempt: int) -> bool:
        policy_attempts.append(attempt)
        if attempt == 3:
            state._reset_read_retry_count()
            return first_decision
        return True

    def _get_delay(_policy: object, attempt: int) -> float:
        delay_attempts.append(attempt)
        return 0.0

    iface = cast(
        BLEInterface,
        SimpleNamespace(
            _transient_read_policy=object(),
            _retry_policy_should_retry=_should_retry,
            _retry_policy_get_delay=_get_delay,
            BLEError=RuntimeError,
        ),
    )
    controller = BLEReceiveRecoveryController(iface, session_state=state)
    monkeypatch.setattr(receive_service_module, "_sleep", lambda _delay: None)

    controller.handle_transient_read_error(BleakError("transient"))

    assert policy_attempts == [3, 0]
    assert state.read_retry_count == 1
    assert delay_attempts == [0]


def test_receive_lifecycle_schedules_inconclusive_restart_outside_session_lock() -> (
    None
):
    """Deferred restart scheduling must not begin while the session lock is held."""
    from meshtastic.interfaces.ble.lifecycle_receive_runtime import (
        BLEReceiveLifecycleCoordinator,
    )

    lock = _TrackingRLock()
    state = BLESessionState(lock=lock)
    existing = cast(
        ThreadLike,
        SimpleNamespace(name="InconclusiveReceive", ident=None, is_alive=lambda: False),
    )
    state.receive_thread = existing
    iface = cast(BLEInterface, SimpleNamespace(_receive_from_radio_impl=lambda: None))
    coordinator = BLEReceiveLifecycleCoordinator(iface, session_state=state)
    scheduled: list[tuple[object, bool]] = []

    def _capture_schedule(**kwargs: object) -> None:
        assert lock.held is False
        scheduled.append(
            (kwargs["existing_thread"], bool(kwargs["enforce_pending_timeout"]))
        )

    coordinator._schedule_deferred_receive_restart = (  # type: ignore[method-assign]
        _capture_schedule
    )

    created, recovery_attempts = (
        coordinator._check_receive_start_conditions(  # noqa: SLF001
            name="InconclusiveReceive",
            reset_recovery=False,
            create_runtime_thread=lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("inconclusive liveness must defer thread creation")
            ),
        )
    )

    assert created is None
    assert recovery_attempts is None
    assert scheduled == [(existing, True)]
    assert state.receive_start_pending is True


def test_receive_lifecycle_schedules_falsy_deferred_thread_by_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A falsy thread-like proxy must still be selected as the deferred target."""
    from meshtastic.interfaces.ble.lifecycle_receive_runtime import (
        BLEReceiveLifecycleCoordinator,
    )

    class _FalsyCurrentThread:
        name = "FalsyCurrentReceive"
        ident = threading.get_ident()

        def __bool__(self) -> bool:
            return False

        @staticmethod
        def is_alive() -> bool:
            return True

    state = BLESessionState(lock=threading.RLock())
    existing = cast(ThreadLike, _FalsyCurrentThread())
    state.receive_thread = existing
    state.receive_start_pending = True
    state.receive_start_pending_since = time.monotonic() - 10.0
    iface = cast(BLEInterface, SimpleNamespace(_receive_from_radio_impl=lambda: None))
    coordinator = BLEReceiveLifecycleCoordinator(iface, session_state=state)
    scheduled: list[object] = []

    def _capture_schedule(**kwargs: object) -> None:
        scheduled.append(kwargs["existing_thread"])

    monkeypatch.setattr(
        coordinator, "_schedule_deferred_receive_restart", _capture_schedule
    )

    created, recovery_attempts = (
        coordinator._check_receive_start_conditions(  # noqa: SLF001
            name="FalsyCurrentReceive",
            reset_recovery=False,
            create_runtime_thread=lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("timed-out current receive must defer thread creation")
            ),
        )
    )

    assert created is None
    assert recovery_attempts is None
    assert scheduled == [existing]


def test_receive_lifecycle_thread_probes_run_outside_session_lock() -> None:
    """Receive-start thread probes should not execute arbitrary code under state lock."""
    from meshtastic.interfaces.ble.lifecycle_receive_runtime import (
        BLEReceiveLifecycleCoordinator,
    )

    lock = _TrackingRLock()
    state = BLESessionState(lock=lock)
    probe_lock_states: list[bool] = []

    class _ExistingThread:
        name = "PendingReceive"
        ident = None

        def is_alive(self) -> bool:
            probe_lock_states.append(lock.held)
            return False

    existing = _ExistingThread()
    state.receive_thread = cast(ThreadLike, existing)
    state.receive_start_pending = True
    state.receive_start_pending_since = 0.0
    iface = cast(BLEInterface, SimpleNamespace(_receive_from_radio_impl=lambda: None))
    coordinator = BLEReceiveLifecycleCoordinator(iface, session_state=state)
    staged = cast(
        ThreadLike,
        SimpleNamespace(name="ReplacementReceive", ident=None, is_alive=lambda: False),
    )

    created, recovery_attempts = (
        coordinator._check_receive_start_conditions(  # noqa: SLF001
            name="ReplacementReceive",
            reset_recovery=False,
            create_runtime_thread=lambda **_kwargs: staged,
        )
    )

    assert created is staged
    assert recovery_attempts is None
    assert probe_lock_states == [False, False]


def test_receive_lifecycle_thread_diagnostics_run_outside_session_lock() -> None:
    """Thread-like name and repr access must not execute while state is locked."""
    from meshtastic.interfaces.ble.lifecycle_receive_runtime import (
        BLEReceiveLifecycleCoordinator,
    )

    lock = _TrackingRLock()
    state = BLESessionState(lock=lock)
    diagnostic_lock_states: list[tuple[str, bool]] = []

    class _NamelessThread:
        ident = 42

        def is_alive(self) -> bool:
            return True

        def __getattribute__(self, name: str) -> object:
            if name == "name":
                diagnostic_lock_states.append(("name", lock.held))
                raise AttributeError(name)
            return object.__getattribute__(self, name)

        def __repr__(self) -> str:
            diagnostic_lock_states.append(("repr", lock.held))
            return "<NamelessReceive>"

    existing = _NamelessThread()
    state.receive_thread = cast(ThreadLike, existing)
    coordinator = BLEReceiveLifecycleCoordinator(
        cast(BLEInterface, SimpleNamespace(_receive_from_radio_impl=lambda: None)),
        session_state=state,
    )

    result = coordinator._check_receive_start_conditions(  # noqa: SLF001
        name="Receive",
        reset_recovery=False,
        create_runtime_thread=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("live existing thread must prevent replacement")
        ),
    )

    assert result == (None, None)
    assert diagnostic_lock_states == [("name", False), ("repr", False)]


def test_receive_recovery_reset_probes_thread_outside_session_lock() -> None:
    """Recovery-reset liveness checks must not run while session state is locked."""
    from meshtastic.interfaces.ble.lifecycle_receive_runtime import (
        BLEReceiveLifecycleCoordinator,
    )

    lock = _TrackingRLock()
    state = BLESessionState(lock=lock, receive_recovery_attempts=2)
    probe_lock_states: list[bool] = []

    class _Thread:
        ident = 42
        name = "Receive"

        def is_alive(self) -> bool:
            probe_lock_states.append(lock.held)
            return True

    thread = cast(ThreadLike, _Thread())
    state.receive_thread = thread
    coordinator = BLEReceiveLifecycleCoordinator(
        cast(BLEInterface, SimpleNamespace()), session_state=state
    )

    coordinator._maybe_reset_receive_recovery(  # noqa: SLF001
        thread=thread,
        recovery_attempts_before_start=2,
    )

    assert probe_lock_states == [False]
    assert state.receive_recovery_attempts == 0


def test_disconnect_plan_reads_epoch_from_explicit_session_state() -> None:
    """Disconnect staleness must use the coordinator's owned session epoch."""
    from meshtastic.interfaces.ble.lifecycle_disconnect_runtime import (
        BLEDisconnectLifecycleCoordinator,
    )

    state_manager = BLEStateManager()
    state = BLESessionState(
        lock=state_manager.lock,
        client_publish_pending=True,
        connection_session_epoch=9,
    )
    iface = cast(
        BLEInterface,
        SimpleNamespace(
            auto_reconnect=False,
            client=None,
            address="AA:BB:CC:DD:EE:FF",
            _connection_session_epoch=1,
            _shutdown_event=threading.Event(),
            _extract_client_address=lambda _client: None,
            _sorted_address_keys=lambda *keys: [key for key in keys if key],
        ),
    )
    coordinator = BLEDisconnectLifecycleCoordinator(iface, session_state=state)

    plan = coordinator._resolve_disconnect_target(  # noqa: SLF001
        "explicit-session",
        client=None,
        bleak_client=None,
        current_state_getter=lambda: ConnectionState.CONNECTED,
        is_closing_getter=lambda: False,
        transition_to_disconnected=lambda: True,
        reset_to_disconnected=lambda: True,
    )

    assert plan.session_epoch == 9
    assert iface._connection_session_epoch == 1


def test_disconnect_source_write_uses_session_lock() -> None:
    """Disconnect diagnostics should obey the shared session ownership boundary."""
    from meshtastic.interfaces.ble.lifecycle_disconnect_runtime import (
        BLEDisconnectLifecycleCoordinator,
    )
    from meshtastic.interfaces.ble.lifecycle_primitives import _DisconnectPlan

    class _Session:
        def __init__(self) -> None:
            self.lock = _TrackingRLock()
            self.connection_session_epoch = 5
            self._last_disconnect_source: str | None = None

        @property
        def last_disconnect_source(self) -> str | None:
            return self._last_disconnect_source

        @last_disconnect_source.setter
        def last_disconnect_source(self, value: str | None) -> None:
            assert self.lock.held is True
            self._last_disconnect_source = value

    session = _Session()
    iface = cast(BLEInterface, SimpleNamespace(client=None))
    coordinator = BLEDisconnectLifecycleCoordinator(
        iface,
        session_state=cast(_BLESessionStatePort, session),
    )
    plan = _DisconnectPlan(
        early_return=False,
        session_epoch=5,
        address="AA:BB:CC:DD:EE:FF",
        was_publish_pending=True,
    )

    should_reconnect = coordinator._execute_disconnect_side_effects(  # noqa: SLF001
        plan=plan,
        source="test",
        close_previous_client_async=lambda _client: None,
        clear_events=lambda *_events: None,
    )

    assert should_reconnect is False
    assert session.last_disconnect_source == "ble.test"


def test_ownership_cleanup_reads_explicit_session_state() -> None:
    """Publish cleanup must not consult contradictory interface compatibility fields."""
    from meshtastic.interfaces.ble.lifecycle_ownership_runtime import (
        BLEConnectionOwnershipLifecycleCoordinator,
    )

    client: Any = object()
    state_manager = BLEStateManager()
    state = BLESessionState(
        lock=state_manager.lock,
        client_publish_pending=True,
        connected_publish_inflight_client=client,
        connection_session_epoch=9,
    )
    iface = cast(
        BLEInterface,
        SimpleNamespace(
            client=None,
            address="legacy-address",
            _last_connection_request="legacy-request",
            _client_publish_pending=False,
            _connected_publish_inflight_client=None,
            _connection_session_epoch=1,
        ),
    )
    coordinator = BLEConnectionOwnershipLifecycleCoordinator(iface, session_state=state)

    coordinator._discard_invalidated_connected_client(  # noqa: SLF001
        client,
        restore_address="AA:BB:CC:DD:EE:FF",
        restore_last_connection_request="restored",
        is_closing_getter=lambda: False,
        reset_to_disconnected=lambda: True,
        current_state_getter=lambda: ConnectionState.DISCONNECTED,
        transition_to_disconnected=lambda: True,
        safe_cleanup=lambda _cleanup, _name: None,
    )

    assert state.client_publish_pending is False
    assert state.connected_publish_inflight_client is None
    assert state.connection_session_epoch == 9
    assert iface._client_publish_pending is False
    assert iface._connected_publish_inflight_client is None
    assert iface._connection_session_epoch == 1


def test_receive_start_resnapshot_loop_is_bounded_under_continuous_churn() -> None:
    """Repeated snapshot invalidation must bail out instead of spinning forever."""
    from meshtastic.interfaces.ble.lifecycle_receive_runtime import (
        RECEIVE_START_SNAPSHOT_MAX_ATTEMPTS,
        BLEReceiveLifecycleCoordinator,
    )

    state_manager = BLEStateManager()
    state = BLESessionState(lock=state_manager.lock, want_receive=True)
    probe_count = 0

    class _ChurningThread:
        name = "ChurningReceive"
        ident = None

        def is_alive(self) -> bool:
            nonlocal probe_count
            probe_count += 1
            with state.lock:
                state.receive_thread = cast(ThreadLike, _ChurningThread())
            return False

    state.receive_thread = cast(ThreadLike, _ChurningThread())
    coordinator = BLEReceiveLifecycleCoordinator(
        cast(BLEInterface, SimpleNamespace()), session_state=state
    )
    created_threads: list[object] = []

    def _unexpected_create(**_kwargs: object) -> ThreadLike:
        raise AssertionError("receive thread creation must not happen under churn")

    result = coordinator._check_receive_start_conditions(  # noqa: SLF001
        name="Receive",
        reset_recovery=False,
        create_runtime_thread=_unexpected_create,
    )

    assert result == (None, None)
    assert probe_count == RECEIVE_START_SNAPSHOT_MAX_ATTEMPTS
    assert created_threads == []


def test_shutdown_closing_probe_runs_outside_session_lock() -> None:
    """Closing compatibility probes must not execute under shared lifecycle state."""
    from meshtastic.interfaces.ble.lifecycle_shutdown_runtime import (
        BLEShutdownLifecycleCoordinator,
    )

    lock = _TrackingRLock()
    state = BLESessionState(lock=lock)
    probe_lock_states: list[bool] = []

    def _is_closing() -> bool:
        probe_lock_states.append(lock.held)
        return False

    iface = cast(
        BLEInterface,
        SimpleNamespace(_state_manager=SimpleNamespace(is_closing=_is_closing)),
    )
    coordinator = BLEShutdownLifecycleCoordinator(iface, session_state=state)

    assert coordinator.is_connection_closing() is False
    assert probe_lock_states == [False]


def test_ownership_invalidation_closing_probe_runs_outside_session_lock() -> None:
    """Invalidation compatibility probes must not execute under session state lock."""
    from meshtastic.interfaces.ble.lifecycle_ownership_runtime import (
        BLEConnectionOwnershipLifecycleCoordinator,
    )

    lock = _TrackingRLock()
    client: Any = object()
    state = BLESessionState(
        lock=lock,
        client_publish_pending=True,
        connected_publish_inflight_client=client,
    )
    iface = cast(
        BLEInterface,
        SimpleNamespace(
            client=None,
            address="legacy-address",
            _last_connection_request="legacy-request",
        ),
    )
    coordinator = BLEConnectionOwnershipLifecycleCoordinator(iface, session_state=state)
    probe_lock_states: list[bool] = []

    def _is_closing() -> bool:
        probe_lock_states.append(lock.held)
        return False

    coordinator._discard_invalidated_connected_client(  # noqa: SLF001
        client,
        restore_address="AA:BB:CC:DD:EE:FF",
        restore_last_connection_request="restored",
        is_closing_getter=_is_closing,
        reset_to_disconnected=lambda: True,
        current_state_getter=lambda: ConnectionState.DISCONNECTED,
        transition_to_disconnected=lambda: True,
        safe_cleanup=lambda _cleanup, _name: None,
    )

    assert probe_lock_states == [False]


def test_ownership_status_probes_run_outside_session_lock() -> None:
    """Ownership compatibility/client probes must not run under shared state lock."""
    from meshtastic.interfaces.ble.lifecycle_ownership_runtime import (
        BLEConnectionOwnershipLifecycleCoordinator,
    )

    lock = _TrackingRLock()
    client: Any = object()
    state = BLESessionState(lock=lock)
    iface = cast(
        BLEInterface, SimpleNamespace(client=client, _state_manager=SimpleNamespace())
    )
    coordinator = BLEConnectionOwnershipLifecycleCoordinator(iface, session_state=state)
    probe_lock_states: list[tuple[str, bool]] = []

    def _probe(name: str) -> bool:
        probe_lock_states.append((name, lock.held))
        return True

    assert coordinator._get_connected_client_status(  # noqa: SLF001
        client,
        is_closing_getter=lambda: not _probe("closing"),
        state_connected_getter=lambda: _probe("state"),
        client_connected_getter=lambda _client: _probe("client"),
    ) == (True, False)
    assert probe_lock_states == [
        ("closing", False),
        ("state", False),
        ("client", False),
    ]


def test_ownership_status_provider_revalidates_session_epoch() -> None:
    """A status result cannot authorize publication after session generation changes."""
    from meshtastic.interfaces.ble.lifecycle_ownership_runtime import (
        BLEConnectionOwnershipLifecycleCoordinator,
    )

    lock = _TrackingRLock()
    client: Any = object()
    state = BLESessionState(lock=lock, connection_session_epoch=4)
    iface = cast(
        BLEInterface,
        SimpleNamespace(
            client=client,
            _state_manager=SimpleNamespace(),
            _has_lost_gate_ownership=lambda *_keys: False,
        ),
    )
    coordinator = BLEConnectionOwnershipLifecycleCoordinator(iface, session_state=state)

    def _racing_status(_client: object) -> tuple[bool, bool]:
        assert lock.held is False
        with lock:
            state.connection_session_epoch += 1
        return True, False

    snapshot = coordinator._verify_ownership_snapshot(  # noqa: SLF001
        client,
        "device-key",
        "alias-key",
        get_connected_client_status_locked=_racing_status,
    )

    assert snapshot.still_owned is False
    assert snapshot.is_closing is False


def test_receive_snapshot_compatibility_probes_run_outside_session_lock() -> None:
    """Receive snapshot compatibility callbacks must run before session locking."""
    from meshtastic.interfaces.ble.receive_service import BLEReceiveRecoveryController

    lock = _TrackingRLock()
    state = BLESessionState(lock=lock)
    probe_lock_states: list[tuple[str, bool]] = []

    def _is_connecting() -> bool:
        probe_lock_states.append(("connecting", lock.held))
        return True

    def _is_closing() -> bool:
        probe_lock_states.append(("closing", lock.held))
        return False

    iface = cast(
        BLEInterface,
        SimpleNamespace(
            client=None,
            _state_manager=SimpleNamespace(
                is_connecting=_is_connecting,
                is_closing=_is_closing,
            ),
        ),
    )
    controller = BLEReceiveRecoveryController(iface, session_state=state)

    assert controller._snapshot_client_state() == (
        None,
        True,
        False,
        False,
    )  # noqa: SLF001
    assert probe_lock_states == [("connecting", False), ("closing", False)]


def test_disconnect_planning_runs_address_helpers_outside_session_lock() -> None:
    """Address/registry planning must not extend the disconnect state critical section."""
    from meshtastic.interfaces.ble.lifecycle_disconnect_runtime import (
        BLEDisconnectLifecycleCoordinator,
    )

    lock = _TrackingRLock()
    state = BLESessionState(lock=lock)
    previous_client: Any = SimpleNamespace(address="AA:BB:CC:DD:EE:FF")
    helper_lock_states: list[tuple[str, bool]] = []

    def _extract_client_address(_client: object) -> str:
        helper_lock_states.append(("extract", lock.held))
        return "AA:BB:CC:DD:EE:FF"

    def _sorted_address_keys(*keys: str | None) -> list[str]:
        helper_lock_states.append(("sort", lock.held))
        return [key for key in keys if key]

    iface = cast(
        BLEInterface,
        SimpleNamespace(
            auto_reconnect=False,
            client=previous_client,
            address="AA:BB:CC:DD:EE:FF",
            _extract_client_address=_extract_client_address,
            _sorted_address_keys=_sorted_address_keys,
        ),
    )
    coordinator = BLEDisconnectLifecycleCoordinator(iface, session_state=state)

    plan = coordinator._resolve_disconnect_target(  # noqa: SLF001
        "test",
        client=previous_client,
        bleak_client=None,
        current_state_getter=lambda: ConnectionState.CONNECTED,
        is_closing_getter=lambda: False,
        transition_to_disconnected=lambda: True,
        reset_to_disconnected=lambda: True,
    )

    assert plan.early_return is None
    assert helper_lock_states
    assert all(held is False for _operation, held in helper_lock_states)


def test_disconnect_compatibility_state_callbacks_run_outside_session_lock() -> None:
    """Legacy state probes and mutations must not execute under session ownership."""
    from meshtastic.interfaces.ble.lifecycle_disconnect_runtime import (
        BLEDisconnectLifecycleCoordinator,
    )

    lock = _TrackingRLock()
    state = BLESessionState(lock=lock)
    previous_client: Any = SimpleNamespace(address="AA:BB:CC:DD:EE:FF")
    state.connection_alias_key = "AA:BB:CC:DD:EE:FF"
    callback_lock_states: list[tuple[str, bool]] = []
    current_states = iter((ConnectionState.CONNECTED, ConnectionState.ERROR))

    def _current_state() -> ConnectionState:
        callback_lock_states.append(("current", lock.held))
        return next(current_states)

    def _transition() -> bool:
        callback_lock_states.append(("transition", lock.held))
        return False

    def _reset() -> bool:
        callback_lock_states.append(("reset", lock.held))
        return False

    iface = cast(
        BLEInterface,
        SimpleNamespace(
            auto_reconnect=False,
            client=previous_client,
            address="AA:BB:CC:DD:EE:FF",
            _extract_client_address=lambda _client: "AA:BB:CC:DD:EE:FF",
            _sorted_address_keys=lambda *keys: [key for key in keys if key],
        ),
    )
    coordinator = BLEDisconnectLifecycleCoordinator(iface, session_state=state)

    plan = coordinator._resolve_disconnect_target(  # noqa: SLF001
        "test",
        client=previous_client,
        bleak_client=None,
        current_state_getter=_current_state,
        is_closing_getter=lambda: False,
        transition_to_disconnected=_transition,
        reset_to_disconnected=_reset,
    )

    assert plan.early_return is None
    assert callback_lock_states == [
        ("current", False),
        ("transition", False),
        ("reset", False),
        ("current", False),
    ]


def test_disconnect_canonical_state_manager_keeps_state_and_session_atomic() -> None:
    """Owned state-manager transitions should use lock-safe canonical primitives."""
    from meshtastic.interfaces.ble.lifecycle_disconnect_runtime import (
        BLEDisconnectLifecycleCoordinator,
    )

    state_manager = BLEStateManager()
    assert state_manager.transition_to(ConnectionState.CONNECTING)
    assert state_manager.transition_to(ConnectionState.CONNECTED)
    state = BLESessionState(lock=state_manager.lock)
    previous_client: Any = SimpleNamespace(address="AA:BB:CC:DD:EE:FF")
    iface = cast(
        BLEInterface,
        SimpleNamespace(
            auto_reconnect=False,
            client=previous_client,
            address="AA:BB:CC:DD:EE:FF",
            _state_manager=state_manager,
            _extract_client_address=lambda _client: "AA:BB:CC:DD:EE:FF",
            _sorted_address_keys=lambda *keys: [key for key in keys if key],
        ),
    )
    coordinator = BLEDisconnectLifecycleCoordinator(iface, session_state=state)
    coordinator._state_access.current_state = lambda: (_ for _ in ()).throw(  # type: ignore[method-assign]  # noqa: SLF001
        AssertionError("canonical state read must not use compatibility dispatch")
    )

    def _fail_transition(_state: ConnectionState) -> bool:
        raise AssertionError("canonical transition must not use compatibility dispatch")

    coordinator._state_access.transition_to = cast(  # type: ignore[method-assign]  # noqa: SLF001
        Any, _fail_transition
    )

    plan = coordinator._resolve_disconnect_target(  # noqa: SLF001
        "test",
        client=previous_client,
        bleak_client=None,
    )

    assert plan.early_return is None
    assert iface.client is None
    assert state.disconnect_notified is True
    assert state_manager.current_state == ConnectionState.DISCONNECTED


def test_transient_retry_revalidation_is_bounded_under_continuous_churn(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A pathological policy/state race must not spin the receive thread forever."""
    import meshtastic.interfaces.ble.receive_service as receive_service_module
    from meshtastic.interfaces.ble.receive_service import (
        TRANSIENT_RETRY_POLICY_REVALIDATION_MAX_ATTEMPTS,
        BLEReceiveRecoveryController,
    )

    state_manager = BLEStateManager()
    state = BLESessionState(lock=state_manager.lock)
    policy_attempts: list[int] = []
    delay_attempts: list[int] = []

    def _should_retry(_policy: object, attempt: int) -> bool:
        policy_attempts.append(attempt)
        with state.lock:
            state.read_retry_count += 1
        return True

    def _get_delay(_policy: object, attempt: int) -> float:
        delay_attempts.append(attempt)
        return 0.0

    iface = cast(
        BLEInterface,
        SimpleNamespace(
            _transient_read_policy=object(),
            _retry_policy_should_retry=_should_retry,
            _retry_policy_get_delay=_get_delay,
            BLEError=RuntimeError,
        ),
    )
    controller = BLEReceiveRecoveryController(iface, session_state=state)
    monkeypatch.setattr(receive_service_module, "_sleep", lambda _delay: None)
    caplog.set_level(
        logging.WARNING, logger="meshtastic.interfaces.ble.receive_service"
    )

    controller.handle_transient_read_error(BleakError("transient"))

    assert len(policy_attempts) == TRANSIENT_RETRY_POLICY_REVALIDATION_MAX_ATTEMPTS
    assert delay_attempts == []
    assert "decision superseded by concurrent retry-state changes" in caplog.text


def test_disconnect_reconnect_closing_probe_runs_outside_session_lock() -> None:
    """Reconnect closing probes must not execute under shared lifecycle state."""
    from meshtastic.interfaces.ble.lifecycle_disconnect_runtime import (
        BLEDisconnectLifecycleCoordinator,
    )

    lock = _TrackingRLock()
    state = BLESessionState(lock=lock)
    probe_lock_states: list[bool] = []

    def _is_closing() -> bool:
        probe_lock_states.append(lock.held)
        return True

    iface = cast(
        BLEInterface,
        SimpleNamespace(
            auto_reconnect=True,
            _shutdown_event=threading.Event(),
            _reconnect_scheduler=SimpleNamespace(
                schedule_reconnect=lambda *_args: None
            ),
        ),
    )
    coordinator = BLEDisconnectLifecycleCoordinator(iface, session_state=state)

    coordinator.schedule_auto_reconnect(is_closing_getter=_is_closing)

    assert probe_lock_states == [False]


def test_shutdown_start_does_not_probe_closing_after_terminal_close() -> None:
    """Repeated shutdown must short-circuit before compatibility probe execution."""
    from meshtastic.interfaces.ble.lifecycle_shutdown_runtime import (
        BLEShutdownLifecycleCoordinator,
    )

    state_manager = BLEStateManager()
    state = BLESessionState(lock=state_manager.lock, closed=True)
    management_lock = threading.RLock()
    iface = cast(
        BLEInterface,
        SimpleNamespace(
            _management_lock=management_lock,
            _management_idle_condition=threading.Condition(management_lock),
            _management_inflight=0,
        ),
    )
    coordinator = BLEShutdownLifecycleCoordinator(iface, session_state=state)

    result = coordinator._await_management_shutdown(  # noqa: SLF001
        management_shutdown_wait_timeout=0.01,
        management_wait_poll_seconds=0.001,
        current_state_getter=lambda: ConnectionState.DISCONNECTED,
        is_closing_getter=lambda: (_ for _ in ()).throw(
            AssertionError("closing probe must not run after terminal close")
        ),
        transition_to_state=lambda _state: True,
        reset_to_disconnected=lambda: True,
    )

    assert result is None


def test_disconnect_client_closer_falls_back_when_timeout_keyword_is_unsupported() -> (
    None
):
    """Legacy injected closers should still be called without timeout keywords."""
    from meshtastic.interfaces.ble.lifecycle_disconnect_runtime import (
        BLEDisconnectLifecycleCoordinator,
    )

    calls: list[object] = []

    def _legacy_closer(client: object) -> None:
        calls.append(client)

    client: Any = object()
    coordinator = BLEDisconnectLifecycleCoordinator(
        cast(BLEInterface, SimpleNamespace()),
        client_closer=_legacy_closer,
    )

    coordinator.disconnect_and_close_client(client, timeout=2.0)

    assert calls == [client]


def test_disconnect_client_closer_propagates_internal_type_error() -> None:
    """Timeout compatibility fallback must not hide errors raised inside a closer."""
    from meshtastic.interfaces.ble.lifecycle_disconnect_runtime import (
        BLEDisconnectLifecycleCoordinator,
    )

    def _broken_closer(
        _client: object, *, disconnect_timeout: float | None = None
    ) -> None:
        _ = disconnect_timeout
        raise TypeError("closer implementation failed")

    coordinator = BLEDisconnectLifecycleCoordinator(
        cast(BLEInterface, SimpleNamespace()),
        client_closer=_broken_closer,
    )

    with pytest.raises(TypeError, match="closer implementation failed"):
        coordinator.disconnect_and_close_client(cast(Any, object()), timeout=2.0)


def test_disconnect_client_closer_requires_cleanup_capability() -> None:
    """A coordinator without any closer should report the missing capability."""
    from meshtastic.interfaces.ble.lifecycle_disconnect_runtime import (
        BLEDisconnectLifecycleCoordinator,
    )

    coordinator = BLEDisconnectLifecycleCoordinator(
        cast(BLEInterface, SimpleNamespace()),
    )

    with pytest.raises(AttributeError, match="client cleanup capability"):
        coordinator.disconnect_and_close_client(cast(Any, object()))
