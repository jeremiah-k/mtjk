"""Tests for BLE shutdown lifecycle runtime behavior.

Covers unsubscribe_all legacy fallback, bounded-thread TOCTOU fixes,
and shutdown client teardown paths.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.lifecycle_shutdown_runtime import (
    BLEShutdownLifecycleCoordinator,
)

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

    def __exit__(self, *_args: object) -> None:
        self._depth -= 1
        self._lock.release()


class _ImmediateThread:
    """Run thread targets inline for deterministic unit tests."""

    def __init__(self, *, target: Any, **_kwargs: Any) -> None:
        self._target = target

    def start(self) -> None:
        self._target()

    def is_alive(self) -> bool:
        return False

    def join(self, timeout: float | None = None) -> None:
        pass


class TestShutdownClientUnsubscribeFallback:
    """Test legacy unsubscribe_all fallback in _shutdown_client."""

    def test_legacy_unsubscribe_all_with_finite_timeout_is_skipped(self) -> None:
        """When timeout is finite, do not retry legacy unsubscribe inline."""
        iface = MagicMock()
        iface.client = None
        iface._management_inflight = 0
        iface._management_target_gate.return_value.__enter__ = MagicMock(
            return_value=MagicMock()
        )
        iface._management_target_gate.return_value.__exit__ = MagicMock(
            return_value=None
        )

        coordinator = BLEShutdownLifecycleCoordinator(iface)

        called_with: list[tuple[Any, ...]] = []

        def legacy_unsubscribe(client: Any) -> None:
            called_with.append((client,))

        notification_manager = MagicMock()
        notification_manager._unsubscribe_all = None
        notification_manager.unsubscribe_all = legacy_unsubscribe
        iface._notification_manager = notification_manager

        with patch.object(
            coordinator,
            "_detach_client_for_shutdown",
            return_value=(MagicMock(spec=BLEClient), False),
        ):
            coordinator._shutdown_client(
                management_wait_timed_out=False,
                unsubscribe_timeout=5.0,
                bounded_close_timeout_active=True,
            )

        assert called_with == []

    def test_legacy_unsubscribe_all_without_timeout_is_called(self) -> None:
        """When no timeout is active, retry legacy unsubscribe without it."""
        iface = MagicMock()
        iface.client = None
        iface._management_inflight = 0
        iface._management_target_gate.return_value.__enter__ = MagicMock(
            return_value=MagicMock()
        )
        iface._management_target_gate.return_value.__exit__ = MagicMock(
            return_value=None
        )

        coordinator = BLEShutdownLifecycleCoordinator(iface)

        called_with: list[tuple[Any, ...]] = []

        def legacy_unsubscribe(client: Any) -> None:
            called_with.append((client,))

        notification_manager = MagicMock()
        notification_manager._unsubscribe_all = None
        notification_manager.unsubscribe_all = legacy_unsubscribe
        iface._notification_manager = notification_manager

        with patch.object(
            coordinator,
            "_detach_client_for_shutdown",
            return_value=(MagicMock(spec=BLEClient), False),
        ):
            coordinator._shutdown_client(
                management_wait_timed_out=False,
                unsubscribe_timeout=None,
                client_disconnect_timeout=None,
                disconnect_notification_wait_timeout=None,
            )

        assert len(called_with) == 1
        assert len(called_with[0]) == 1

    def test_unsubscribe_all_unrelated_typeerror_propagates(self) -> None:
        """Unrelated TypeError from unsubscribe_all should propagate through safe-cleanup."""
        iface = MagicMock()
        iface.client = None
        iface._management_inflight = 0
        iface._management_target_gate.return_value.__enter__ = MagicMock(
            return_value=MagicMock()
        )
        iface._management_target_gate.return_value.__exit__ = MagicMock(
            return_value=None
        )

        coordinator = BLEShutdownLifecycleCoordinator(iface)

        def bad_unsubscribe(client: Any, timeout: float | None = None) -> None:
            raise TypeError("unexpected type problem")

        notification_manager = MagicMock()
        notification_manager._unsubscribe_all = None
        notification_manager.unsubscribe_all = bad_unsubscribe
        iface._notification_manager = notification_manager

        with patch.object(
            coordinator,
            "_detach_client_for_shutdown",
            return_value=(MagicMock(spec=BLEClient), False),
        ):
            with pytest.raises(TypeError, match="unexpected type problem"):
                coordinator._shutdown_client(
                    management_wait_timed_out=False,
                    unsubscribe_timeout=5.0,
                    bounded_close_timeout_active=True,
                )


class TestBoundedThreadTOCTOU:
    """Test bounded cleanup thread TOCTOU race fixes."""

    def test_concurrent_cleanup_cannot_spawn_duplicate_threads(self) -> None:
        """Two concurrent close calls must not both start cleanup threads."""
        iface = MagicMock()
        iface._closed = True
        iface._management_inflight = 0
        iface._management_idle_condition.wait.return_value = True

        blocker = threading.Event()

        # Use an explicitly declared function so structural hook lookup resolves it.
        def slow_cleanup() -> None:
            blocker.wait()

        iface.thread_coordinator = SimpleNamespace(cleanup=slow_cleanup)

        coordinator = BLEShutdownLifecycleCoordinator(iface)
        started_threads: list[threading.Thread] = []
        original_thread = threading.Thread

        def tracking_thread(*, target: Any, **kwargs: Any) -> threading.Thread:
            t = original_thread(target=target, **kwargs)
            started_threads.append(t)
            return t

        with patch("threading.Thread", side_effect=tracking_thread):
            # First call should start a thread
            coordinator._cleanup_thread_coordinator(timeout=1.0)
            # Second concurrent call should skip because previous is still alive
            coordinator._cleanup_thread_coordinator(timeout=1.0)

        # Only one thread should have been started (the second may be constructed
        # but must not be started because the check happens under the same lock).
        started_count = sum(1 for t in started_threads if t.ident is not None)
        assert started_count == 1
        blocker.set()

    def test_cleanup_missing_thread_coordinator_is_best_effort(self) -> None:
        """Missing optional coordinator state must not escape shutdown cleanup."""
        coordinator = BLEShutdownLifecycleCoordinator(SimpleNamespace())  # type: ignore[arg-type]

        coordinator._cleanup_thread_coordinator()

    def test_start_failure_clears_stored_ref(self) -> None:
        """If thread.start() fails, the stored ref must be cleared under lock."""
        iface = MagicMock()
        iface._closed = True
        iface._management_inflight = 0
        iface._management_idle_condition.wait.return_value = True

        def real_cleanup() -> None:
            pass

        iface.thread_coordinator = SimpleNamespace(cleanup=real_cleanup)

        coordinator = BLEShutdownLifecycleCoordinator(iface)

        class FailingThread(threading.Thread):
            def start(self) -> None:
                raise RuntimeError("start failed")

        with patch("threading.Thread", side_effect=FailingThread):
            coordinator._cleanup_thread_coordinator(timeout=1.0)

        # The ref should be cleared
        assert coordinator._bounded_cleanup_thread is None

    def test_existing_skip_warning_behavior_intact(self) -> None:
        """When a previous bounded thread is alive, warn and skip without starting."""
        iface = MagicMock()
        iface._closed = True
        iface._management_inflight = 0
        iface._management_idle_condition.wait.return_value = True

        def real_cleanup() -> None:
            pass

        iface.thread_coordinator = SimpleNamespace(cleanup=real_cleanup)

        coordinator = BLEShutdownLifecycleCoordinator(iface)

        # Pretend a previous thread is still alive
        previous = MagicMock()
        previous.is_alive.return_value = True
        coordinator._bounded_cleanup_thread = previous

        coordinator._cleanup_thread_coordinator(timeout=1.0)

        # No new thread should be started; previous.join is NOT called in the new design,
        # but the warning behavior is preserved (skip + warn).
        previous.join.assert_not_called()

    def test_mesh_close_start_failure_clears_ref(self) -> None:
        """MeshInterface.close thread start failure clears the stored ref."""
        iface = MagicMock()
        iface._closed = True
        iface._management_inflight = 0
        iface._management_idle_condition.wait.return_value = True

        coordinator = BLEShutdownLifecycleCoordinator(iface)

        class FailingThread(threading.Thread):
            def start(self) -> None:
                raise RuntimeError("start failed")

        with patch("threading.Thread", side_effect=FailingThread):
            coordinator._close_mesh_interface(timeout=1.0)

        assert coordinator._bounded_mesh_close_thread is None

    def test_mesh_close_skip_when_previous_alive(self) -> None:
        """MeshInterface.close should skip when a previous bounded thread is alive."""
        iface = MagicMock()
        iface._closed = True
        iface._management_inflight = 0
        iface._management_idle_condition.wait.return_value = True

        coordinator = BLEShutdownLifecycleCoordinator(iface)

        previous = MagicMock()
        previous.is_alive.return_value = True
        coordinator._bounded_mesh_close_thread = previous

        coordinator._close_mesh_interface(timeout=1.0)

        previous.join.assert_not_called()


class TestShutdownStateProbeLocking:
    """Test shutdown state-manager probes respect the shared lock boundary."""

    def test_current_state_probe_runs_outside_session_lock(self) -> None:
        """Shutdown must not call state-manager current-state hooks under session lock."""
        from meshtastic.interfaces.ble.session_state import BLESessionState
        from meshtastic.interfaces.ble.state import ConnectionState

        lock = _TrackingRLock()
        session = BLESessionState(lock=lock)
        iface = MagicMock()
        iface._management_lock = threading.RLock()
        iface._management_inflight = 0
        probe_lock_states: list[bool] = []

        def _current_state() -> ConnectionState:
            probe_lock_states.append(lock.held)
            return ConnectionState.DISCONNECTED

        coordinator = BLEShutdownLifecycleCoordinator(iface, session_state=session)

        result = coordinator._await_management_shutdown(  # noqa: SLF001
            management_shutdown_wait_timeout=0.1,
            management_wait_poll_seconds=0.01,
            current_state_getter=_current_state,
            is_closing_getter=lambda: False,
            transition_to_state=lambda _state: True,
            reset_to_disconnected=lambda: True,
        )

        assert result is False
        assert probe_lock_states == [False]


class TestReceiveThreadSessionLocking:
    """Test shutdown receive-thread state capture synchronization."""

    def test_receive_thread_reference_is_read_under_session_lock(self) -> None:
        """Shutdown should snapshot the current receive thread while holding its owner lock."""

        class _ObservedSession:
            def __init__(self) -> None:
                self.lock = _TrackingRLock()
                self._receive_thread: object | None = None
                self.receive_start_pending = False
                self.receive_start_pending_since = None
                self.receive_thread_reads = 0

            @property
            def receive_thread(self) -> object | None:
                assert self.lock.held is True
                self.receive_thread_reads += 1
                return self._receive_thread

            @receive_thread.setter
            def receive_thread(self, value: object | None) -> None:
                assert self.lock.held is True
                self._receive_thread = value

        session = _ObservedSession()
        coordinator = BLEShutdownLifecycleCoordinator(
            MagicMock(), session_state=session  # type: ignore[arg-type]
        )

        coordinator._shutdown_receive_thread(
            wake_waiting_threads=lambda *_events: None,
            join_thread=lambda *_args, **_kwargs: None,
        )

        assert session.receive_thread_reads == 1

    def test_receive_thread_liveness_probes_run_outside_session_lock(self) -> None:
        """Thread-like liveness callbacks must not execute under shared state lock."""

        class _ObservedSession:
            def __init__(self) -> None:
                self.lock = _TrackingRLock()
                self._receive_thread: object | None = None
                self.receive_start_pending = True
                self.receive_start_pending_since = 1.0

            @property
            def receive_thread(self) -> object | None:
                assert self.lock.held is True
                return self._receive_thread

            @receive_thread.setter
            def receive_thread(self, value: object | None) -> None:
                assert self.lock.held is True
                self._receive_thread = value

        session = _ObservedSession()
        probe_lock_states: list[bool] = []
        probe_results = iter((True, True, False))

        class _ThreadLike:
            ident = 42
            name = "ObservedReceive"

            def is_alive(self) -> bool:
                probe_lock_states.append(session.lock.held)
                return next(probe_results)

        with session.lock:
            session.receive_thread = _ThreadLike()
        coordinator = BLEShutdownLifecycleCoordinator(
            MagicMock(), session_state=session  # type: ignore[arg-type]
        )

        coordinator._shutdown_receive_thread(
            wake_waiting_threads=lambda *_events: None,
            join_thread=lambda *_args, **_kwargs: None,
        )

        assert probe_lock_states == [False, False, False]
        with session.lock:
            assert session.receive_thread is None
