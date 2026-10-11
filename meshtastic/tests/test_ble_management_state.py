"""Tests for owned BLE management synchronization state."""

import threading
from types import SimpleNamespace, TracebackType
from unittest.mock import MagicMock

import pytest

from meshtastic.interfaces.ble.management_state import (
    BLEManagementState,
    _management_state_for,
)


@pytest.mark.unit
def test_management_state_tracks_and_notifies_idle_waiters() -> None:
    """Management state should own inflight accounting and idle notification."""
    state = BLEManagementState()
    notify_all = MagicMock(wraps=state.condition.notify_all)
    condition = SimpleNamespace(wait=state.condition.wait, notify_all=notify_all)
    state._replace_condition(condition)

    with state.lock:
        state._begin_locked()
        state._begin_locked()
    assert state.inflight == 2

    assert state.finish() is True
    assert state.inflight == 1
    notify_all.assert_not_called()

    assert state.finish() is True
    assert state.inflight == 0
    notify_all.assert_called_once_with()


@pytest.mark.unit
def test_management_state_normalizes_underflow_and_notifies() -> None:
    """Finishing without active work should reset to idle and signal waiters."""
    state = BLEManagementState()
    notify_all = MagicMock()
    condition = SimpleNamespace(
        wait=lambda *_args, **_kwargs: True,
        notify_all=notify_all,
    )
    state._replace_condition(condition)
    state.inflight = -3

    assert state.finish() is False
    assert state.inflight == 0
    notify_all.assert_called_once_with()


@pytest.mark.unit
def test_management_state_lock_replacement_rebuilds_default_condition() -> None:
    """Compatibility lock replacement should keep condition ownership coherent."""
    state = BLEManagementState()
    replacement = threading.RLock()

    state._replace_lock(replacement)

    assert state.lock is replacement
    with state.lock:
        state._begin_locked()
    assert state.inflight == 1
    assert state.finish() is True


@pytest.mark.unit
def test_management_state_for_preserves_legacy_collaborator_members() -> None:
    """Legacy partial collaborators should remain usable through an adapter."""
    lock = threading.RLock()
    condition = threading.Condition(lock)
    target = SimpleNamespace(
        _management_lock=lock,
        _management_idle_condition=condition,
        _management_inflight=0,
    )
    state = _management_state_for(target)

    with state.lock:
        state._begin_locked()
    assert target._management_inflight == 1
    assert state.finish() is True
    assert target._management_inflight == 0


class _ContextOnlyLock:
    """Expose only the lock operations promised by ``_LockPort``."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def __enter__(self) -> object:
        return self._lock.__enter__()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._lock.__exit__(exc_type, exc_value, traceback)


@pytest.mark.unit
def test_management_state_condition_supports_context_only_lock() -> None:
    """A compatibility lock without acquire/release should still support waiters."""
    state = BLEManagementState(_ContextOnlyLock())
    released = threading.Event()
    waiting = threading.Event()
    original_condition = state.condition

    def _tracked_wait(timeout: float | None = None) -> bool:
        waiting.set()
        return original_condition.wait(timeout)

    state._replace_condition(
        SimpleNamespace(
            wait=_tracked_wait,
            notify_all=original_condition.notify_all,
        )
    )

    with state.lock:
        state._begin_locked()

    def _wait_for_idle() -> None:
        with state.lock:
            while state.inflight:
                state.condition.wait(timeout=1.0)
        released.set()

    waiter = threading.Thread(target=_wait_for_idle)
    waiter.start()
    assert waiting.wait(timeout=1.0)
    assert state.finish() is True
    waiter.join(timeout=1.0)

    assert not waiter.is_alive()
    assert released.is_set()


@pytest.mark.unit
def test_context_only_condition_timeout_unregisters_waiter() -> None:
    """A timed-out compatibility wait should unregister itself before returning."""
    state = BLEManagementState(_ContextOnlyLock())

    with state.lock:
        assert state.condition.wait(timeout=0.0) is False

    # A later notification must remain safe after the timed-out waiter is gone.
    with state.lock:
        state.condition.notify_all()


@pytest.mark.unit
def test_legacy_management_state_normalizes_underflow() -> None:
    """Legacy accounting underflow should normalize to idle without raising."""
    lock = threading.RLock()
    target = SimpleNamespace(
        _management_lock=lock,
        _management_idle_condition=threading.Condition(lock),
        _management_inflight=0,
    )
    state = _management_state_for(target)

    assert state.finish() is False
    assert target._management_inflight == 0


@pytest.mark.unit
def test_legacy_management_state_adapter_is_cached_per_target() -> None:
    """Legacy collaborators should resolve one shared lock/condition adapter."""
    target = SimpleNamespace(_management_inflight=0)

    first = _management_state_for(target)
    second = _management_state_for(target)

    assert first is second
    assert first.lock is second.lock
    assert first.condition is second.condition


class _SlottedLegacyTarget:
    """Legacy target that stores synchronization members but no adapter cache."""

    __slots__ = (
        "_management_idle_condition",
        "_management_inflight",
        "_management_lock",
    )

    def __init__(self) -> None:
        self._management_inflight = 0


@pytest.mark.unit
def test_legacy_management_state_persists_fallback_primitives_on_slotted_target() -> (
    None
):
    """Independent adapters should share fallback synchronization and wakeups."""
    target = _SlottedLegacyTarget()
    first = _management_state_for(target)
    second = _management_state_for(target)
    released = threading.Event()

    assert first is second
    assert first.lock is second.lock
    assert first.condition is second.condition

    with first.lock:
        first._begin_locked()

    def _wait_with_second_adapter() -> None:
        with second.lock:
            while second.inflight:
                second.condition.wait(timeout=1.0)
        released.set()

    waiter = threading.Thread(target=_wait_with_second_adapter)
    waiter.start()
    assert first.finish() is True
    waiter.join(timeout=1.0)

    assert not waiter.is_alive()
    assert released.is_set()


class _UnwritableLegacyTarget:
    """Legacy target that cannot persist any compatibility members."""

    __slots__ = ()


@pytest.mark.unit
def test_legacy_management_state_caches_unwritable_target_and_tracks_inflight() -> None:
    """Unwritable targets should still share adapter state across collaborators."""
    target = _UnwritableLegacyTarget()

    first = _management_state_for(target)
    second = _management_state_for(target)

    assert first is second
    with first.lock:
        first._begin_locked()
    assert first.inflight == 1
    assert second.inflight == 1
    assert second.finish() is True
    assert first.inflight == 0


@pytest.mark.unit
def test_legacy_management_state_treats_boolean_inflight_as_invalid() -> None:
    """Boolean legacy counters should not masquerade as active operations."""
    target = SimpleNamespace(_management_inflight=True)
    state = _management_state_for(target)

    assert state.inflight == 0
    with state.lock:
        state._begin_locked()
    assert target._management_inflight == 1
    assert state.finish() is True


@pytest.mark.unit
def test_legacy_management_state_replaces_condition_bound_to_other_lock() -> None:
    """A declared condition must not be reused with a different management lock."""
    lock = threading.RLock()
    mismatched = threading.Condition(threading.RLock())
    target = SimpleNamespace(
        _management_lock=lock,
        _management_idle_condition=mismatched,
        _management_inflight=1,
    )

    state = _management_state_for(target)

    assert state.condition is not mismatched
    assert target._management_idle_condition is state.condition
    with state.lock:
        assert state.condition.wait(timeout=0.0) is False
    assert state.finish() is True
