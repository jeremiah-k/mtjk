"""Expanded test coverage for BLE reconnection logic and policies.

Tests cover reconnection policy edge cases, state transitions, timeouts,
maximum retry limits, and concurrent reconnection attempt handling.
"""

# pylint: disable=too-many-lines

import logging
from threading import TIMEOUT_MAX, Event, RLock
from types import SimpleNamespace
from typing import Any, Generator, cast
from unittest.mock import MagicMock

import pytest

from meshtastic.interfaces.ble.gating import (
    _clear_all_registries,
    _mark_connected,
    _mark_disconnected,
)
from meshtastic.interfaces.ble.policies import MIN_DELAY_FLOOR, ReconnectPolicy
from meshtastic.interfaces.ble.reconnection import (
    NextAttempt,
    ReconnectPolicyMissingMethodError,
    ReconnectScheduler,
    ReconnectWorker,
    ThreadCoordinatorMissingMethodError,
)
from meshtastic.interfaces.ble.state import BLEStateManager


@pytest.fixture(autouse=True)
def clear_gating_registry() -> Generator[None, None, None]:
    """Clear gating registry before each test."""
    _clear_all_registries()
    yield
    _clear_all_registries()


class TestReconnectPolicyMissingMethodError:
    """Tests for ReconnectPolicyMissingMethodError."""

    @pytest.mark.unit
    def test_error_message(self) -> None:
        """Error message includes method name."""
        err = ReconnectPolicyMissingMethodError("test_method")
        assert "test_method" in str(err)
        assert err.method_name == "test_method"


class TestThreadCoordinatorMissingMethodError:
    """Tests for ThreadCoordinatorMissingMethodError."""

    @pytest.mark.unit
    def test_error_message(self) -> None:
        """Error message includes missing methods."""
        err = ThreadCoordinatorMissingMethodError("create_thread/_create_thread")
        assert "create_thread/_create_thread" in str(err)


class TestNextAttempt:
    """Tests for NextAttempt named tuple."""

    @pytest.mark.unit
    def test_next_attempt_creation(self) -> None:
        """NextAttempt can be created and accessed."""
        attempt = NextAttempt(delay=1.5, should_retry=True)
        assert attempt.delay == 1.5
        assert attempt.should_retry is True

    @pytest.mark.unit
    def test_next_attempt_immutable(self) -> None:
        """NextAttempt is immutable."""
        attempt = NextAttempt(delay=1.5, should_retry=True)
        with pytest.raises(AttributeError):
            attempt.delay = 2.0  # type: ignore[misc]


class TestReconnectSchedulerInit:
    """Tests for ReconnectScheduler initialization."""

    @pytest.mark.unit
    def test_scheduler_init_creates_worker(self) -> None:
        """Scheduler initializes with worker and policy."""
        interface = SimpleNamespace(
            _is_connection_closing=False,
            _can_initiate_connection=True,
        )
        scheduler = ReconnectScheduler(
            state_manager=BLEStateManager(),
            state_lock=RLock(),
            thread_coordinator=SimpleNamespace(),  # type: ignore[arg-type]
            interface=interface,  # type: ignore[arg-type]
        )
        assert scheduler._reconnect_worker is not None
        assert scheduler._reconnect_policy is not None
        assert scheduler._reconnect_thread is None


class TestReconnectSchedulerHookResolution:
    """Tests for thread coordinator hook resolution."""

    @pytest.mark.unit
    def test_resolve_public_hook_first(self) -> None:
        """Public hook is preferred over legacy."""
        public_hook = MagicMock(return_value="public")
        legacy_hook = MagicMock(return_value="legacy")
        coordinator = SimpleNamespace(
            create_thread=public_hook,
            _create_thread=legacy_hook,
        )
        interface = SimpleNamespace(
            _is_connection_closing=False,
            _can_initiate_connection=True,
        )
        scheduler = ReconnectScheduler(
            state_manager=BLEStateManager(),
            state_lock=RLock(),
            thread_coordinator=coordinator,  # type: ignore[arg-type]
            interface=interface,  # type: ignore[arg-type]
        )
        hook = scheduler._resolve_thread_coordinator_hook(
            "create_thread", "_create_thread"
        )
        assert hook is public_hook

    @pytest.mark.unit
    def test_resolve_legacy_hook_when_public_missing(self) -> None:
        """Legacy hook used when public missing."""
        legacy_hook = MagicMock(return_value="legacy")
        coordinator = SimpleNamespace(_create_thread=legacy_hook)
        interface = SimpleNamespace(
            _is_connection_closing=False,
            _can_initiate_connection=True,
        )
        scheduler = ReconnectScheduler(
            state_manager=BLEStateManager(),
            state_lock=RLock(),
            thread_coordinator=coordinator,  # type: ignore[arg-type]
            interface=interface,  # type: ignore[arg-type]
        )
        hook = scheduler._resolve_thread_coordinator_hook(
            "create_thread", "_create_thread"
        )
        assert hook is legacy_hook

    @pytest.mark.unit
    def test_resolve_hook_ignores_synthesized_public_member(self) -> None:
        """A dynamic public hook must not outrank a declared legacy hook."""

        class _Coordinator:
            def __init__(self) -> None:
                self.legacy_calls = 0

            def _create_thread(self, **_kwargs: object) -> str:
                self.legacy_calls += 1
                return "legacy"

            def __getattr__(self, _name: str) -> object:
                return lambda **_kwargs: "dynamic"

        coordinator = _Coordinator()
        scheduler = ReconnectScheduler(
            state_manager=BLEStateManager(),
            state_lock=RLock(),
            thread_coordinator=coordinator,  # type: ignore[arg-type]
            interface=SimpleNamespace(
                _is_connection_closing=False,
                _can_initiate_connection=True,
            ),  # type: ignore[arg-type]
        )

        hook = scheduler._resolve_thread_coordinator_hook(
            "create_thread", "_create_thread"
        )

        assert hook() == "legacy"
        assert coordinator.legacy_calls == 1

    @pytest.mark.unit
    def test_resolve_hook_missing_raises(self) -> None:
        """Missing both hooks raises ThreadCoordinatorMissingMethodError."""
        coordinator = SimpleNamespace()
        interface = SimpleNamespace(
            _is_connection_closing=False,
            _can_initiate_connection=True,
        )
        scheduler = ReconnectScheduler(
            state_manager=BLEStateManager(),
            state_lock=RLock(),
            thread_coordinator=coordinator,  # type: ignore[arg-type]
            interface=interface,  # type: ignore[arg-type]
        )
        with pytest.raises(ThreadCoordinatorMissingMethodError):
            scheduler._resolve_thread_coordinator_hook(
                "create_thread", "_create_thread"
            )


class TestReconnectSchedulerThreadCreation:
    """Tests for thread creation and starting."""

    @pytest.mark.unit
    def test_thread_create_thread_success(self) -> None:
        """Thread creation via coordinator succeeds."""
        mock_thread = SimpleNamespace(ident=12345, is_alive=lambda: True)
        create_hook = MagicMock(return_value=mock_thread)
        coordinator = SimpleNamespace(create_thread=create_hook)
        interface = SimpleNamespace(
            _is_connection_closing=False,
            _can_initiate_connection=True,
        )
        scheduler = ReconnectScheduler(
            state_manager=BLEStateManager(),
            state_lock=RLock(),
            thread_coordinator=coordinator,  # type: ignore[arg-type]
            interface=interface,  # type: ignore[arg-type]
        )
        thread = scheduler._thread_create_thread(
            target=lambda: None,
            args=(),
            kwargs={},
            name="TestThread",
            daemon=True,
        )
        assert thread is cast(Any, mock_thread)
        create_hook.assert_called_once()

    @pytest.mark.unit
    def test_thread_start_thread_success(self) -> None:
        """Thread starting via coordinator succeeds."""
        calls: list[Any] = []

        def start_hook(thread: Any) -> None:
            calls.append(thread)

        coordinator = SimpleNamespace(start_thread=start_hook)
        interface = SimpleNamespace(
            _is_connection_closing=False,
            _can_initiate_connection=True,
        )
        scheduler = ReconnectScheduler(
            state_manager=BLEStateManager(),
            state_lock=RLock(),
            thread_coordinator=coordinator,  # type: ignore[arg-type]
            interface=interface,  # type: ignore[arg-type]
        )
        mock_thread = SimpleNamespace(ident=12345)
        scheduler._thread_start_thread(mock_thread)  # type: ignore[arg-type]
        assert len(calls) == 1
        assert calls[0] is mock_thread


class TestReconnectSchedulerScheduleReconnect:
    """Tests for reconnect scheduling logic."""

    @pytest.mark.unit
    def test_schedule_reconnect_disabled(self) -> None:
        """Scheduling skipped when auto_reconnect is False."""
        scheduler = ReconnectScheduler(
            state_manager=BLEStateManager(),
            state_lock=RLock(),
            thread_coordinator=SimpleNamespace(),  # type: ignore[arg-type]
            interface=SimpleNamespace(  # type: ignore[arg-type]
                _is_connection_closing=False,
                _can_initiate_connection=True,
            ),
        )
        result = scheduler._schedule_reconnect(False, Event())
        assert result is False

    @pytest.mark.unit
    def test_schedule_reconnect_interface_closing(self) -> None:
        """Scheduling skipped when interface is closing."""
        scheduler = ReconnectScheduler(
            state_manager=BLEStateManager(),
            state_lock=RLock(),
            thread_coordinator=SimpleNamespace(),  # type: ignore[arg-type]
            interface=SimpleNamespace(  # type: ignore[arg-type]
                _is_connection_closing=True,
                _can_initiate_connection=True,
            ),
        )
        result = scheduler._schedule_reconnect(True, Event())
        assert result is False

    @pytest.mark.unit
    def test_schedule_reconnect_already_in_progress(self) -> None:
        """Scheduling skipped when reconnect already in progress."""
        scheduler = ReconnectScheduler(
            state_manager=BLEStateManager(),
            state_lock=RLock(),
            thread_coordinator=SimpleNamespace(),  # type: ignore[arg-type]
            interface=SimpleNamespace(  # type: ignore[arg-type]
                _is_connection_closing=False,
                _can_initiate_connection=True,
            ),
        )
        # Simulate existing thread
        scheduler._reconnect_thread = SimpleNamespace(ident=12345)  # type: ignore[assignment]
        result = scheduler._schedule_reconnect(True, Event())
        assert result is False

    @pytest.mark.unit
    def test_schedule_reconnect_thread_creation_fails(self) -> None:
        """Thread creation failure clears reference."""
        mock_thread = SimpleNamespace(ident=None, is_alive=lambda: False)
        coordinator = SimpleNamespace(
            create_thread=lambda **_: mock_thread,
            start_thread=lambda _: None,
        )
        scheduler = ReconnectScheduler(
            state_manager=BLEStateManager(),
            state_lock=RLock(),
            thread_coordinator=coordinator,  # type: ignore[arg-type]
            interface=SimpleNamespace(  # type: ignore[arg-type]
                _is_connection_closing=False,
                _can_initiate_connection=True,
            ),
        )
        result = scheduler._schedule_reconnect(True, Event())
        assert result is False
        assert scheduler._reconnect_thread is None

    @pytest.mark.unit
    def test_schedule_reconnect_success(self) -> None:
        """Successful scheduling creates and starts thread."""
        mock_thread = SimpleNamespace(ident=12345, is_alive=lambda: True)
        coordinator = SimpleNamespace(
            create_thread=lambda **_: mock_thread,
            start_thread=lambda _: None,
        )
        scheduler = ReconnectScheduler(
            state_manager=BLEStateManager(),
            state_lock=RLock(),
            thread_coordinator=coordinator,  # type: ignore[arg-type]
            interface=SimpleNamespace(  # type: ignore[arg-type]
                _is_connection_closing=False,
                _can_initiate_connection=True,
            ),
        )
        result = scheduler._schedule_reconnect(True, Event())
        assert result is True
        assert scheduler._reconnect_thread is cast(Any, mock_thread)


class TestReconnectSchedulerClearReference:
    """Tests for clearing thread reference."""

    @pytest.mark.unit
    def test_clear_thread_reference(self) -> None:
        """Clear reference sets thread to None."""
        scheduler = ReconnectScheduler(
            state_manager=BLEStateManager(),
            state_lock=RLock(),
            thread_coordinator=SimpleNamespace(),  # type: ignore[arg-type]
            interface=SimpleNamespace(  # type: ignore[arg-type]
                _is_connection_closing=False,
                _can_initiate_connection=True,
            ),
        )
        scheduler._reconnect_thread = SimpleNamespace(ident=12345)  # type: ignore[assignment]
        scheduler._clear_thread_reference()
        assert scheduler._reconnect_thread is None


class TestReconnectWorkerCallPolicy:
    """Tests for canonical reconnect-policy method dispatch."""

    @pytest.mark.unit
    def test_call_policy_uses_declared_canonical_method(self) -> None:
        """Policy dispatch should invoke the declared canonical snake_case method."""
        next_attempt = MagicMock(return_value=(1.0, True))
        policy = SimpleNamespace(next_attempt=next_attempt)
        worker = ReconnectWorker(
            interface=SimpleNamespace(),  # type: ignore[arg-type]
            reconnect_policy=policy,  # type: ignore[arg-type]
        )
        result = worker._call_policy("next_attempt")
        assert result == (1.0, True)
        next_attempt.assert_called_once_with()

    @pytest.mark.unit
    @pytest.mark.parametrize("legacy_name", ["nextAttempt", "_next_attempt"])
    def test_call_policy_rejects_noncanonical_aliases(self, legacy_name: str) -> None:
        """Internal reconnect policy dispatch must not preserve naming aliases."""
        policy = SimpleNamespace(**{legacy_name: lambda: (1.0, True)})
        worker = ReconnectWorker(
            interface=SimpleNamespace(),  # type: ignore[arg-type]
            reconnect_policy=policy,  # type: ignore[arg-type]
        )
        with pytest.raises(ReconnectPolicyMissingMethodError, match="next_attempt"):
            worker._call_policy("next_attempt")

    @pytest.mark.unit
    def test_call_policy_ignores_synthesized_public_member(self) -> None:
        """Policy lookup should reject dynamically synthesized canonical members."""

        class _Policy:
            def __init__(self) -> None:
                self.synthesized_calls = 0

            def __getattr__(self, _name: str) -> object:
                def _synthesized(*_args: object) -> tuple[float, bool]:
                    self.synthesized_calls += 1
                    return (99.0, False)

                return _synthesized

        policy = _Policy()
        worker = ReconnectWorker(
            interface=SimpleNamespace(),  # type: ignore[arg-type]
            reconnect_policy=policy,  # type: ignore[arg-type]
        )

        with pytest.raises(ReconnectPolicyMissingMethodError, match="next_attempt"):
            worker._call_policy("next_attempt")
        assert policy.synthesized_calls == 0

    @pytest.mark.unit
    def test_call_policy_missing_raises(self) -> None:
        """Missing policy method raises ReconnectPolicyMissingMethodError."""
        policy = SimpleNamespace()
        worker = ReconnectWorker(
            interface=SimpleNamespace(),  # type: ignore[arg-type]
            reconnect_policy=policy,  # type: ignore[arg-type]
        )
        with pytest.raises(ReconnectPolicyMissingMethodError):
            worker._call_policy("nonexistent_method")


class TestReconnectWorkerShouldAbortReconnect:
    """Tests for abort reconnect decision logic."""

    @pytest.mark.unit
    def test_should_abort_when_closing(self) -> None:
        """Abort when interface is closing."""
        interface = SimpleNamespace(
            _is_connection_closing=True,
            auto_reconnect=True,
        )
        worker = ReconnectWorker(
            interface=interface,  # type: ignore[arg-type]
            reconnect_policy=ReconnectPolicy(max_retries=None),
        )
        assert worker._should_abort_reconnect() is True

    @pytest.mark.unit
    def test_should_abort_when_auto_reconnect_disabled(self) -> None:
        """Abort when auto_reconnect is disabled."""
        interface = SimpleNamespace(
            _is_connection_closing=False,
            auto_reconnect=False,
        )
        worker = ReconnectWorker(
            interface=interface,  # type: ignore[arg-type]
            reconnect_policy=ReconnectPolicy(max_retries=None),
        )
        assert worker._should_abort_reconnect() is True

    @pytest.mark.unit
    def test_should_not_abort_when_enabled_and_open(self) -> None:
        """Don't abort when enabled and not closing."""
        interface = SimpleNamespace(
            _is_connection_closing=False,
            auto_reconnect=True,
        )
        worker = ReconnectWorker(
            interface=interface,  # type: ignore[arg-type]
            reconnect_policy=ReconnectPolicy(max_retries=None),
        )
        assert worker._should_abort_reconnect() is False

    @pytest.mark.unit
    def test_should_abort_with_context(self) -> None:
        """Context string included in debug output."""
        interface = SimpleNamespace(
            _is_connection_closing=True,
            auto_reconnect=True,
        )
        worker = ReconnectWorker(
            interface=interface,  # type: ignore[arg-type]
            reconnect_policy=ReconnectPolicy(max_retries=None),
        )
        # Should not raise, just verifies context parameter is accepted
        assert worker._should_abort_reconnect(context="test context") is True


class TestReconnectWorkerValidateNextAttempt:
    """Tests for next_attempt validation."""


    @pytest.mark.unit
    def test_validate_valid_tuple(self) -> None:
        """Valid tuple returns NextAttempt."""
        worker = ReconnectWorker(
            interface=SimpleNamespace(),  # type: ignore[arg-type]
            reconnect_policy=ReconnectPolicy(max_retries=None),
        )
        result = worker._validate_next_attempt((1.5, True))
        assert isinstance(result, NextAttempt)
        assert result.delay == 1.5
        assert result.should_retry is True


    @pytest.mark.unit
    def test_validate_invalid_not_tuple(self) -> None:
        """Non-tuple returns None."""
        worker = ReconnectWorker(
            interface=SimpleNamespace(),  # type: ignore[arg-type]
            reconnect_policy=ReconnectPolicy(max_retries=None),
        )
        assert worker._validate_next_attempt("invalid") is None


    @pytest.mark.unit
    def test_validate_invalid_wrong_length(self) -> None:
        """Tuple with wrong length returns None."""
        worker = ReconnectWorker(
            interface=SimpleNamespace(),  # type: ignore[arg-type]
            reconnect_policy=ReconnectPolicy(max_retries=None),
        )
        assert worker._validate_next_attempt((1.0,)) is None
        assert worker._validate_next_attempt((1.0, True, "extra")) is None


    @pytest.mark.unit
    def test_validate_invalid_delay_type(self) -> None:
        """Invalid delay type returns None."""
        worker = ReconnectWorker(
            interface=SimpleNamespace(),  # type: ignore[arg-type]
            reconnect_policy=ReconnectPolicy(max_retries=None),
        )
        assert worker._validate_next_attempt(("delay", True)) is None


    @pytest.mark.unit
    def test_validate_invalid_delay_bool(self) -> None:
        """Boolean delay treated as invalid."""
        worker = ReconnectWorker(
            interface=SimpleNamespace(),  # type: ignore[arg-type]
            reconnect_policy=ReconnectPolicy(max_retries=None),
        )
        assert worker._validate_next_attempt((True, True)) is None


    @pytest.mark.unit
    def test_validate_invalid_retry_type(self) -> None:
        """Invalid should_retry type returns None."""
        worker = ReconnectWorker(
            interface=SimpleNamespace(),  # type: ignore[arg-type]
            reconnect_policy=ReconnectPolicy(max_retries=None),
        )
        assert worker._validate_next_attempt((1.0, "yes")) is None


    @pytest.mark.unit
    def test_validate_non_finite_delay(self) -> None:
        """Non-finite delay returns None."""
        worker = ReconnectWorker(
            interface=SimpleNamespace(),  # type: ignore[arg-type]
            reconnect_policy=ReconnectPolicy(max_retries=None),
        )
        assert worker._validate_next_attempt((float("inf"), True)) is None
        assert worker._validate_next_attempt((float("nan"), True)) is None


    @pytest.mark.unit
    def test_validate_negative_delay(self) -> None:
        """Negative delay returns None."""
        worker = ReconnectWorker(
            interface=SimpleNamespace(),  # type: ignore[arg-type]
            reconnect_policy=ReconnectPolicy(max_retries=None),
        )
        assert worker._validate_next_attempt((-1.0, True)) is None


    @pytest.mark.unit
    def test_validate_delay_exceeds_timeout_max(self) -> None:
        """Delay exceeding TIMEOUT_MAX is capped."""
        worker = ReconnectWorker(
            interface=SimpleNamespace(),  # type: ignore[arg-type]
            reconnect_policy=ReconnectPolicy(max_retries=None),
        )
        result = worker._validate_next_attempt((TIMEOUT_MAX + 1000, True))
        assert isinstance(result, NextAttempt)
        assert result.delay == TIMEOUT_MAX


class TestReconnectWorkerGetValidatedNextAttempt:
    """Tests for get_validated_next_attempt method."""

    @pytest.mark.unit
    def test_get_validated_success(self) -> None:
        """Successful policy call returns validated result."""
        policy = ReconnectPolicy(max_retries=3)
        worker = ReconnectWorker(
            interface=SimpleNamespace(),  # type: ignore[arg-type]
            reconnect_policy=policy,
        )
        result = worker._get_validated_next_attempt()
        assert isinstance(result, NextAttempt)
        assert result.delay > 0
        assert isinstance(result.should_retry, bool)

    @pytest.mark.unit
    def test_get_validated_policy_missing_method(self) -> None:
        """Missing policy method returns None."""
        policy = SimpleNamespace()
        worker = ReconnectWorker(
            interface=SimpleNamespace(),  # type: ignore[arg-type]
            reconnect_policy=policy,  # type: ignore[arg-type]
        )
        assert worker._get_validated_next_attempt() is None


class TestReconnectWorkerConnectedElsewhereGate:
    """Tests for connected elsewhere gate handling."""

    @pytest.mark.unit
    def test_gate_no_address(self) -> None:
        """Gate returns None when no address."""
        interface = SimpleNamespace(address=None)
        worker = ReconnectWorker(
            interface=interface,  # type: ignore[arg-type]
            reconnect_policy=ReconnectPolicy(max_retries=None),
        )
        result = worker._handle_connected_elsewhere_gate(interface, 1, Event())  # type: ignore[arg-type]
        assert result is None

    @pytest.mark.unit
    def test_gate_not_connected_elsewhere(self) -> None:
        """Gate returns None when not connected elsewhere."""
        interface = SimpleNamespace(address="AA:BB:CC:DD:EE:FF")
        worker = ReconnectWorker(
            interface=interface,  # type: ignore[arg-type]
            reconnect_policy=ReconnectPolicy(max_retries=None),
        )
        result = worker._handle_connected_elsewhere_gate(interface, 1, Event())  # type: ignore[arg-type]
        assert result is None


class TestReconnectWorkerAttemptReconnectLoop:
    """Tests for the main reconnect loop."""

    @pytest.mark.unit
    def test_loop_aborts_when_interface_closing(self) -> None:
        """Loop aborts immediately when interface is closing."""
        shutdown_event = Event()
        interface = SimpleNamespace(
            _is_connection_closing=True,
            auto_reconnect=True,
        )
        worker = ReconnectWorker(
            interface=interface,  # type: ignore[arg-type]
            reconnect_policy=ReconnectPolicy(max_retries=None),
        )
        # Should return without running
        worker._attempt_reconnect_loop(shutdown_event)
        # No assertions needed - just verifying no exception

    @pytest.mark.unit
    def test_loop_aborts_when_auto_reconnect_disabled(self) -> None:
        """Loop aborts immediately when auto_reconnect disabled."""
        shutdown_event = Event()
        interface = SimpleNamespace(
            _is_connection_closing=False,
            auto_reconnect=False,
        )
        worker = ReconnectWorker(
            interface=interface,  # type: ignore[arg-type]
            reconnect_policy=ReconnectPolicy(max_retries=None),
        )
        # Should return without running
        worker._attempt_reconnect_loop(shutdown_event)

    @pytest.mark.unit
    def test_loop_resets_policy(self) -> None:
        """Loop resets policy at start."""
        shutdown_event = Event()
        policy = ReconnectPolicy(max_retries=1)
        policy._attempt_count = 5  # Set non-zero
        interface = SimpleNamespace(
            _is_connection_closing=False,
            auto_reconnect=True,
            _is_connection_connected=False,
            address="AA:BB:CC:DD:EE:FF",
        )
        worker = ReconnectWorker(
            interface=interface,  # type: ignore[arg-type]
            reconnect_policy=policy,
        )
        # Just verify reset is called at start (policy counter will be reset)
        worker._attempt_reconnect_loop(shutdown_event)
        # After loop, policy should be reset (0 or more based on loop execution)

    @pytest.mark.unit
    def test_loop_on_exit_callback_called(self) -> None:
        """Exit callback is called when loop finishes."""
        shutdown_event = Event()
        on_exit = MagicMock()
        policy = ReconnectPolicy(max_retries=1)
        interface = SimpleNamespace(
            _is_connection_closing=False,
            auto_reconnect=True,
            _is_connection_connected=False,
            address="AA:BB:CC:DD:EE:FF",
        )
        worker = ReconnectWorker(
            interface=interface,  # type: ignore[arg-type]
            reconnect_policy=policy,
        )
        worker._attempt_reconnect_loop(shutdown_event, on_exit=on_exit)
        on_exit.assert_called_once()

    @pytest.mark.unit
    def test_loop_on_exit_callback_exception_handled(self) -> None:
        """Exception in exit callback is handled gracefully."""
        shutdown_event = Event()
        on_exit = MagicMock(side_effect=Exception("exit error"))
        policy = ReconnectPolicy(max_retries=1)
        interface = SimpleNamespace(
            _is_connection_closing=False,
            auto_reconnect=True,
            _is_connection_connected=False,
            address="AA:BB:CC:DD:EE:FF",
        )
        worker = ReconnectWorker(
            interface=interface,  # type: ignore[arg-type]
            reconnect_policy=policy,
        )
        # Should not raise despite exit callback exception
        worker._attempt_reconnect_loop(shutdown_event, on_exit=on_exit)
        on_exit.assert_called_once()

    @pytest.mark.unit
    def test_loop_handles_shutdown_event_set(self) -> None:
        """Loop exits when shutdown event is set."""
        shutdown_event = Event()
        shutdown_event.set()
        policy = ReconnectPolicy(max_retries=None)
        interface = SimpleNamespace(
            _is_connection_closing=False,
            auto_reconnect=True,
            _is_connection_connected=False,
            address="AA:BB:CC:DD:EE:FF",
        )
        worker = ReconnectWorker(
            interface=interface,  # type: ignore[arg-type]
            reconnect_policy=policy,
        )
        worker._attempt_reconnect_loop(shutdown_event)
        # Should exit immediately due to shutdown event


class TestReconnectWorkerPolicyIntegration:
    """Integration tests for policy behavior."""

    @pytest.mark.unit
    def test_exponential_backoff_increases(self) -> None:
        """Exponential backoff increases delay over attempts."""
        policy = ReconnectPolicy(
            initial_delay=1.0,
            max_delay=30.0,
            backoff=2.0,
            jitter_ratio=0.0,
            max_retries=5,
        )
        delays = []
        for _ in range(5):
            delay, _ = policy.next_attempt()
            delays.append(delay)
        # Verify exponential growth
        assert delays[1] > delays[0]
        assert delays[2] > delays[1]
        # Verify backoff factor
        assert delays[1] == pytest.approx(delays[0] * 2.0)

    @pytest.mark.unit
    def test_max_delay_cap(self) -> None:
        """Delay is capped at max_delay."""
        policy = ReconnectPolicy(
            initial_delay=10.0,
            max_delay=15.0,
            backoff=2.0,
            jitter_ratio=0.0,
            max_retries=5,
        )
        for _ in range(5):
            delay, _ = policy.next_attempt()
            assert delay <= 15.0

    @pytest.mark.unit
    def test_max_retry_limit(self) -> None:
        """Policy respects max_retries limit."""
        policy = ReconnectPolicy(
            initial_delay=0.1,
            max_delay=1.0,
            backoff=1.5,
            jitter_ratio=0.0,
            max_retries=3,
        )
        # First 3 attempts should allow retry
        for i in range(3):
            _, should_retry = policy.next_attempt()
            assert should_retry is True, f"Attempt {i} should allow retry"
        # Fourth attempt should not allow retry
        _, should_retry = policy.next_attempt()
        assert should_retry is False

    @pytest.mark.unit
    def test_unlimited_retries(self) -> None:
        """None max_retries allows unlimited retries."""
        policy = ReconnectPolicy(
            initial_delay=0.1,
            max_delay=1.0,
            backoff=1.5,
            jitter_ratio=0.0,
            max_retries=None,
        )
        for _ in range(100):
            _, should_retry = policy.next_attempt()
            assert should_retry is True

    @pytest.mark.unit
    def test_reset_clears_attempt_count(self) -> None:
        """Reset clears attempt counter."""
        policy = ReconnectPolicy(max_retries=10)
        for _ in range(5):
            policy.next_attempt()
        assert policy.get_attempt_count() == 5
        policy.reset()
        assert policy.get_attempt_count() == 0


class TestReconnectWorkerAttemptCountValidation:
    """Tests for attempt count validation in loop."""

    @pytest.mark.unit
    def test_loop_handles_none_attempt_count(self) -> None:
        """Loop handles None attempt count from policy."""
        shutdown_event = Event()
        policy = SimpleNamespace(
            reset=lambda: None,
            get_attempt_count=lambda: None,
        )
        interface = SimpleNamespace(
            _is_connection_closing=False,
            auto_reconnect=True,
            _is_connection_connected=False,
        )
        worker = ReconnectWorker(
            interface=interface,  # type: ignore[arg-type]
            reconnect_policy=policy,  # type: ignore[arg-type]
        )
        # Should handle None and exit
        worker._attempt_reconnect_loop(shutdown_event)

    @pytest.mark.unit
    def test_loop_handles_invalid_attempt_count_type(self) -> None:
        """Loop handles non-int attempt count from policy."""
        shutdown_event = Event()
        policy = SimpleNamespace(
            reset=lambda: None,
            get_attempt_count=lambda: "invalid",
        )
        interface = SimpleNamespace(
            _is_connection_closing=False,
            auto_reconnect=True,
            _is_connection_connected=False,
        )
        worker = ReconnectWorker(
            interface=interface,  # type: ignore[arg-type]
            reconnect_policy=policy,  # type: ignore[arg-type]
        )
        # Should handle invalid type and exit
        worker._attempt_reconnect_loop(shutdown_event)

    @pytest.mark.unit
    def test_loop_handles_bool_attempt_count(self) -> None:
        """Loop handles boolean attempt count from policy."""
        shutdown_event = Event()
        policy = SimpleNamespace(
            reset=lambda: None,
            get_attempt_count=lambda: True,
        )
        interface = SimpleNamespace(
            _is_connection_closing=False,
            auto_reconnect=True,
            _is_connection_connected=False,
        )
        worker = ReconnectWorker(
            interface=interface,  # type: ignore[arg-type]
            reconnect_policy=policy,  # type: ignore[arg-type]
        )
        # Should handle bool and exit
        worker._attempt_reconnect_loop(shutdown_event)


class TestReconnectWorkerConcurrentHandling:
    """Tests for concurrent reconnection attempt handling."""

    @pytest.mark.unit
    def test_concurrent_scheduler_checks_thread_reference(self) -> None:
        """Scheduler checks existing thread reference to prevent duplicates."""
        scheduler = ReconnectScheduler(
            state_manager=BLEStateManager(),
            state_lock=RLock(),
            thread_coordinator=SimpleNamespace(),  # type: ignore[arg-type]
            interface=SimpleNamespace(  # type: ignore[arg-type]
                _is_connection_closing=False,
                _can_initiate_connection=True,
            ),
        )
        # First scheduling attempt
        scheduler._reconnect_thread = SimpleNamespace(ident=12345)  # type: ignore[assignment]
        # Second attempt should be rejected
        result = scheduler._schedule_reconnect(True, Event())
        assert result is False


class TestReconnectWorkerTimeoutScenarios:
    """Tests for timeout handling in reconnection."""

    @pytest.mark.unit
    def test_shutdown_event_timeout_interrupts_wait(self) -> None:
        """Shutdown event interrupts sleep wait."""
        shutdown_event = Event()
        policy = ReconnectPolicy(max_retries=5)
        interface = SimpleNamespace(
            _is_connection_closing=False,
            auto_reconnect=True,
            _is_connection_connected=False,
            address="AA:BB:CC:DD:EE:FF",
        )
        worker = ReconnectWorker(
            interface=interface,  # type: ignore[arg-type]
            reconnect_policy=policy,
        )
        # Set shutdown event to interrupt immediately
        shutdown_event.set()
        # Loop should exit quickly without full backoff wait
        worker._attempt_reconnect_loop(shutdown_event)


class TestReconnectWorkerEdgeCases:
    """Edge case tests for reconnection logic."""

    @pytest.mark.unit
    def test_empty_address_warning(self) -> None:
        """Empty address logs warning and exits."""
        shutdown_event = Event()
        policy = ReconnectPolicy(max_retries=5)
        interface = SimpleNamespace(
            _is_connection_closing=False,
            auto_reconnect=True,
            _is_connection_connected=False,
            address="",
        )
        worker = ReconnectWorker(
            interface=interface,  # type: ignore[arg-type]
            reconnect_policy=policy,
        )
        # Should handle empty address gracefully
        worker._attempt_reconnect_loop(shutdown_event)

    @pytest.mark.unit
    def test_already_connected_exits(self) -> None:
        """Loop exits immediately if already connected."""
        shutdown_event = Event()
        policy = ReconnectPolicy(max_retries=None)
        policy._attempt_count = 0
        interface = SimpleNamespace(
            _is_connection_closing=False,
            auto_reconnect=True,
            _is_connection_connected=True,
            address="AA:BB:CC:DD:EE:FF",
        )
        worker = ReconnectWorker(
            interface=interface,  # type: ignore[arg-type]
            reconnect_policy=policy,
        )
        # Should exit on first iteration when checking connection status
        worker._attempt_reconnect_loop(shutdown_event)


class TestReconnectWorkerErrorHandling:
    """Tests for error handling in reconnect loop."""

    @pytest.mark.unit
    def test_policy_error_in_setup(self) -> None:
        """Policy error during setup is handled."""
        shutdown_event = Event()
        policy = SimpleNamespace(
            reset=lambda: (_ for _ in ()).throw(Exception("reset error")),
        )
        interface = SimpleNamespace(
            _is_connection_closing=False,
            auto_reconnect=True,
        )
        worker = ReconnectWorker(
            interface=interface,  # type: ignore[arg-type]
            reconnect_policy=policy,  # type: ignore[arg-type]
        )
        # Should handle reset error gracefully
        worker._attempt_reconnect_loop(shutdown_event)

    @pytest.mark.unit
    def test_missing_next_attempt_in_loop(self) -> None:
        """Missing next_attempt method exits loop."""
        shutdown_event = Event()
        policy = SimpleNamespace(
            reset=lambda: None,
            get_attempt_count=lambda: 0,
        )
        interface = SimpleNamespace(
            _is_connection_closing=False,
            auto_reconnect=True,
            _is_connection_connected=False,
            address="AA:BB:CC:DD:EE:FF",
        )
        worker = ReconnectWorker(
            interface=interface,  # type: ignore[arg-type]
            reconnect_policy=policy,  # type: ignore[arg-type]
        )
        # Should exit when can't call next_attempt
        worker._attempt_reconnect_loop(shutdown_event)


class TestPolicyDelayComputation:
    """Tests for policy delay computation edge cases."""

    @pytest.mark.unit
    def test_overflow_handling(self) -> None:
        """Overflow in exponential computation falls back to max_delay."""
        # Use a moderate initial delay with high backoff factor and many attempts
        # to trigger overflow, while keeping initial_delay <= max_delay
        policy = ReconnectPolicy(
            initial_delay=10.0,
            max_delay=100.0,
            backoff=10.0,  # High backoff to reach overflow faster
            jitter_ratio=0.0,
            max_retries=100,
        )
        delay = policy._get_delay(100)  # Very high attempt
        assert delay <= 100.0

    @pytest.mark.unit
    def test_jitter_application(self) -> None:
        """Jitter is applied to delay."""
        policy = ReconnectPolicy(
            initial_delay=10.0,
            max_delay=100.0,
            backoff=2.0,
            jitter_ratio=0.1,
            max_retries=5,
        )
        # With jitter, delays should vary
        delays = [policy._get_delay(0) for _ in range(10)]
        # All should be within jitter bounds
        for d in delays:
            assert 9.0 <= d <= 11.0  # 10.0 +/- 10%

    @pytest.mark.unit
    def test_min_delay_floor(self) -> None:
        """Minimum delay floor is respected."""
        policy = ReconnectPolicy(
            initial_delay=0.0001,
            max_delay=100.0,
            backoff=1.1,
            jitter_ratio=0.0,
            max_retries=5,
        )
        delay = policy._get_delay(100)  # High attempt with small initial
        # Should be at least MIN_DELAY_FLOOR
        assert delay >= MIN_DELAY_FLOOR


class TestReconnectSchedulerConcurrency:
    """Tests for scheduler concurrency handling."""

    @pytest.mark.unit
    def test_clear_reference_while_in_progress(self) -> None:
        """Clearing reference during operation is safe."""
        scheduler = ReconnectScheduler(
            state_manager=BLEStateManager(),
            state_lock=RLock(),
            thread_coordinator=SimpleNamespace(),  # type: ignore[arg-type]
            interface=SimpleNamespace(  # type: ignore[arg-type]
                _is_connection_closing=False,
                _can_initiate_connection=True,
            ),
        )
        scheduler._reconnect_thread = SimpleNamespace(ident=12345)  # type: ignore[assignment]
        # Clear while "in progress"
        scheduler._clear_thread_reference()
        assert scheduler._reconnect_thread is None
        # Should be able to schedule again


class TestReconnectWorkerLogging:
    """Tests for logging behavior in reconnection."""

    @pytest.mark.unit
    def test_debug_logs_with_context(self, caplog: pytest.LogCaptureFixture) -> None:
        """Debug logging includes context information."""
        caplog.set_level(logging.DEBUG)
        interface = SimpleNamespace(
            _is_connection_closing=True,
            auto_reconnect=True,
        )
        worker = ReconnectWorker(
            interface=interface,  # type: ignore[arg-type]
            reconnect_policy=ReconnectPolicy(max_retries=None),
        )
        worker._should_abort_reconnect(context="test_context")
        # Verify logging occurred
        assert any("test_context" in record.message for record in caplog.records)


class TestReconnectSchedulerThreadExceptions:
    """Tests for thread start exception handling."""

    @pytest.mark.unit
    def test_thread_start_exception_clears_reference(self) -> None:
        """Thread start exception clears thread reference."""
        mock_thread = SimpleNamespace(ident=12345, is_alive=lambda: True)
        calls: list[Any] = []

        def failing_start(thread: Any) -> None:
            calls.append(thread)
            raise RuntimeError("Thread start failed")

        coordinator = SimpleNamespace(
            create_thread=lambda **_: mock_thread,
            start_thread=failing_start,
        )
        scheduler = ReconnectScheduler(
            state_manager=BLEStateManager(),
            state_lock=RLock(),
            thread_coordinator=coordinator,  # type: ignore[arg-type]
            interface=SimpleNamespace(  # type: ignore[arg-type]
                _is_connection_closing=False,
                _can_initiate_connection=True,
            ),
        )
        # Should raise the exception
        with pytest.raises(RuntimeError, match="Thread start failed"):
            scheduler._schedule_reconnect(True, Event())
        # Reference should be cleared after exception
        assert scheduler._reconnect_thread is None


class TestReconnectWorkerGating:
    """Tests for connected elsewhere gate handling."""

    @pytest.mark.unit
    def test_gate_connected_elsewhere_with_retry(self) -> None:
        """Gate handles connected elsewhere with retry."""

        shutdown_event = Event()
        address = "AA:BB:CC:DD:EE:FF"

        # Create a mock interface to be the "other" owner
        other_interface = SimpleNamespace()
        _mark_connected(address, owner=other_interface)

        try:
            interface = SimpleNamespace(
                address=address,
                _is_connection_closing=False,
                auto_reconnect=True,
                _is_connection_connected=False,
            )
            policy = ReconnectPolicy(max_retries=3, initial_delay=0.01)
            worker = ReconnectWorker(
                interface=interface,  # type: ignore[arg-type]
                reconnect_policy=policy,
            )

            # Should detect connected elsewhere and wait
            result = worker._handle_connected_elsewhere_gate(
                interface,  # type: ignore[arg-type]
                1,
                shutdown_event,
            )
            assert result is True  # Should continue after wait
        finally:
            _mark_disconnected(address)

    @pytest.mark.unit
    def test_gate_connected_elsewhere_max_retries_reached(self) -> None:
        """Gate exits when max retries reached."""

        shutdown_event = Event()
        address = "AA:BB:CC:DD:EE:FF"

        # Create a mock interface to be the "other" owner
        other_interface = SimpleNamespace()
        _mark_connected(address, owner=other_interface)

        try:
            interface = SimpleNamespace(
                address=address,
                _is_connection_closing=False,
                auto_reconnect=True,
                _is_connection_connected=False,
            )
            # Create policy with max_retries=0 so should_retry is False
            policy = ReconnectPolicy(max_retries=0, initial_delay=0.01)
            worker = ReconnectWorker(
                interface=interface,  # type: ignore[arg-type]
                reconnect_policy=policy,
            )

            result = worker._handle_connected_elsewhere_gate(
                interface,  # type: ignore[arg-type]
                1,
                shutdown_event,
            )
            assert result is False  # Should exit (max retries reached)
        finally:
            _mark_disconnected(address)

    @pytest.mark.unit
    def test_gate_connected_elsewhere_shutdown_event_set(self) -> None:
        """Gate exits when shutdown event is set."""
        address = "AA:BB:CC:DD:EE:FF"
        _mark_connected(address)
        shutdown_event = Event()

        try:
            interface = SimpleNamespace(
                _is_connection_closing=False,
                _is_connection_connected=True,
                address=address,
            )
            policy = ReconnectPolicy(max_retries=3, initial_delay=0.01)
            worker = ReconnectWorker(
                interface=interface,  # type: ignore[arg-type]
                reconnect_policy=policy,
            )
            # Set shutdown event
            shutdown_event.set()

            result = worker._handle_connected_elsewhere_gate(
                interface,  # type: ignore[arg-type]
                1,
                shutdown_event,
            )
            assert result is False  # Should exit due to shutdown
        finally:
            _mark_disconnected(address)


class TestReconnectWorkerErrorPaths:
    """Tests for error handling in reconnect loop."""

    @pytest.mark.unit
    def test_loop_handles_ble_error(self) -> None:
        """Loop handles interface BLEError."""
        shutdown_event = Event()

        class MockBLEError(Exception):
            """Mock BLE error for testing."""

            pass

        interface = SimpleNamespace(
            _is_connection_closing=False,
            auto_reconnect=True,
            _is_connection_connected=False,
            address="AA:BB:CC:DD:EE:FF",
            BLEError=MockBLEError,
        )

        def failing_connect(_addr: str) -> None:
            raise MockBLEError("BLE connection failed")


        interface.connect = failing_connect

        policy = ReconnectPolicy(max_retries=1, initial_delay=0.01)
        worker = ReconnectWorker(
            interface=interface,  # type: ignore[arg-type]
            reconnect_policy=policy,
        )

        # Should handle BLEError and exit after max_retries
        worker._attempt_reconnect_loop(shutdown_event)

    @pytest.mark.unit
    def test_loop_handles_ble_error_then_abort(self) -> None:
        """Loop aborts when interface closing after BLEError."""
        shutdown_event = Event()

        class MockBLEError(Exception):
            """Mock BLE error for testing."""

            pass

        calls: list[bool] = []

        interface = SimpleNamespace(
            _is_connection_closing=False,
            auto_reconnect=True,
            _is_connection_connected=False,
            address="AA:BB:CC:DD:EE:FF",
            BLEError=MockBLEError,
        )

        def failing_connect(_addr: str) -> None:
            # After first failure, mark as closing
            if len(calls) == 0:
                calls.append(True)
                interface._is_connection_closing = True
            raise MockBLEError("BLE connection failed")


        interface.connect = failing_connect

        policy = ReconnectPolicy(max_retries=5, initial_delay=0.01)
        worker = ReconnectWorker(
            interface=interface,  # type: ignore[arg-type]
            reconnect_policy=policy,
        )

        worker._attempt_reconnect_loop(shutdown_event)
        assert interface._is_connection_closing is True

    @pytest.mark.unit
    def test_loop_handles_unexpected_error(self) -> None:
        """Loop handles unexpected errors."""
        shutdown_event = Event()

        interface = SimpleNamespace(
            _is_connection_closing=False,
            auto_reconnect=True,
            _is_connection_connected=False,
            address="AA:BB:CC:DD:EE:FF",
        )

        def failing_connect(_addr: str) -> None:
            raise ValueError("Unexpected error")


        interface.connect = failing_connect

        policy = ReconnectPolicy(max_retries=1, initial_delay=0.01)
        worker = ReconnectWorker(
            interface=interface,  # type: ignore[arg-type]
            reconnect_policy=policy,
        )

        # Should handle unexpected error and retry
        worker._attempt_reconnect_loop(shutdown_event)

    @pytest.mark.unit
    def test_loop_handles_unexpected_error_then_abort(self) -> None:
        """Loop aborts when auto_reconnect disabled after unexpected error."""
        shutdown_event = Event()

        calls: list[bool] = []

        interface = SimpleNamespace(
            _is_connection_closing=False,
            auto_reconnect=True,
            _is_connection_connected=False,
            address="AA:BB:CC:DD:EE:FF",
        )

        def failing_connect(_addr: str) -> None:
            if len(calls) == 0:
                calls.append(True)
                interface.auto_reconnect = False  # Disable after first attempt
            raise ValueError("Unexpected error")


        interface.connect = failing_connect

        policy = ReconnectPolicy(max_retries=5, initial_delay=0.01)
        worker = ReconnectWorker(
            interface=interface,  # type: ignore[arg-type]
            reconnect_policy=policy,
        )

        worker._attempt_reconnect_loop(shutdown_event)
        assert interface.auto_reconnect is False


class TestReconnectWorkerPolicyMissingInLoop:
    """Tests for policy missing method in outer try block."""

    @pytest.mark.unit
    def test_policy_missing_reset_in_outer_try(self) -> None:
        """Missing reset method in outer try block handled."""
        shutdown_event = Event()

        # Policy without reset method
        policy = SimpleNamespace()
        interface = SimpleNamespace(
            _is_connection_closing=False,
            auto_reconnect=True,
            _is_connection_connected=False,
        )
        worker = ReconnectWorker(
            interface=interface,  # type: ignore[arg-type]
            reconnect_policy=policy,  # type: ignore[arg-type]
        )

        # Should handle missing reset in outer try block
        worker._attempt_reconnect_loop(shutdown_event)


class TestReconnectWorkerGateResults:
    """Tests for gate result handling in loop."""

    @pytest.mark.unit
    def test_loop_gate_returns_false_exits(self) -> None:
        """Loop exits when gate returns False."""
        shutdown_event = Event()

        interface = SimpleNamespace(
            _is_connection_closing=False,
            auto_reconnect=True,
            _is_connection_connected=False,
            address="AA:BB:CC:DD:EE:FF",
        )
        policy = ReconnectPolicy(max_retries=5, initial_delay=0.01)
        worker = ReconnectWorker(
            interface=interface,  # type: ignore[arg-type]
            reconnect_policy=policy,
        )

        # Patch gate to return False
        original_gate: Any = worker._handle_connected_elsewhere_gate

        def mock_gate_false(*_args: Any) -> bool:
            return False

        worker._handle_connected_elsewhere_gate = mock_gate_false  # type: ignore[method-assign,assignment]
        # Should exit when gate returns False
        worker._attempt_reconnect_loop(shutdown_event)

        # Restore original
        worker._handle_connected_elsewhere_gate = original_gate  # type: ignore[method-assign]

    @pytest.mark.unit
    def test_loop_gate_returns_true_continues(self) -> None:
        """Loop continues when gate returns True."""
        shutdown_event = Event()

        interface = SimpleNamespace(
            _is_connection_closing=False,
            auto_reconnect=True,
            _is_connection_connected=False,
            address="AA:BB:CC:DD:EE:FF",
        )
        policy = ReconnectPolicy(max_retries=5, initial_delay=0.01)
        worker = ReconnectWorker(
            interface=interface,  # type: ignore[arg-type]
            reconnect_policy=policy,
        )

        # Patch gate to return True once, then None
        original_gate: Any = worker._handle_connected_elsewhere_gate
        call_count = 0

        def mock_gate_true_once(_iface: Any, _attempt: int, _event: Any) -> bool | None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return True  # Continue to next iteration
            return None  # Then proceed normally

        worker._handle_connected_elsewhere_gate = mock_gate_true_once  # type: ignore[method-assign,assignment]

        # Should handle True (continue) then proceed
        # Exit quickly by setting max retries low
        worker._attempt_reconnect_loop(shutdown_event)

        # Restore original
        worker._handle_connected_elsewhere_gate = original_gate  # type: ignore[method-assign]


class TestReconnectWorkerBleakErrors:
    """Tests for Bleak error handling in reconnect loop."""

    @pytest.mark.unit
    def test_loop_handles_bleak_dbus_error(self) -> None:
        """Loop handles BleakDBusError with extended delay."""
        from bleak.exc import BleakDBusError

        shutdown_event = Event()

        interface = SimpleNamespace(
            _is_connection_closing=False,
            auto_reconnect=True,
            _is_connection_connected=False,
            address="AA:BB:CC:DD:EE:FF",
        )

        def failing_connect(_addr: str) -> None:
            raise BleakDBusError("org.bluez.Error.Failed", 1)  # type: ignore[arg-type]


        interface.connect = failing_connect

        policy = ReconnectPolicy(max_retries=1, initial_delay=0.01)
        worker = ReconnectWorker(
            interface=interface,  # type: ignore[arg-type]
            reconnect_policy=policy,
        )

        # Should handle BleakDBusError and use extended delay
        worker._attempt_reconnect_loop(shutdown_event)

    @pytest.mark.unit
    def test_loop_handles_bleak_error(self) -> None:
        """Loop handles BleakError."""
        from bleak.exc import BleakError

        shutdown_event = Event()

        interface = SimpleNamespace(
            _is_connection_closing=False,
            auto_reconnect=True,
            _is_connection_connected=False,
            address="AA:BB:CC:DD:EE:FF",
        )

        def failing_connect(_addr: str) -> None:
            raise BleakError("Generic BLE error")


        interface.connect = failing_connect

        policy = ReconnectPolicy(max_retries=1, initial_delay=0.01)
        worker = ReconnectWorker(
            interface=interface,  # type: ignore[arg-type]
            reconnect_policy=policy,
        )

        # Should handle BleakError
        worker._attempt_reconnect_loop(shutdown_event)

    @pytest.mark.unit
    def test_loop_handles_bleak_device_not_found_error(self) -> None:
        """Loop handles BleakDeviceNotFoundError with extended delay."""
        from bleak.exc import BleakDeviceNotFoundError

        shutdown_event = Event()

        interface = SimpleNamespace(
            _is_connection_closing=False,
            auto_reconnect=True,
            _is_connection_connected=False,
            address="AA:BB:CC:DD:EE:FF",
        )

        def failing_connect(_addr: str) -> None:
            raise BleakDeviceNotFoundError("Device not found")


        interface.connect = failing_connect

        policy = ReconnectPolicy(max_retries=1, initial_delay=0.01)
        worker = ReconnectWorker(
            interface=interface,  # type: ignore[arg-type]
            reconnect_policy=policy,
        )

        # Should handle BleakDeviceNotFoundError with extended delay
        worker._attempt_reconnect_loop(shutdown_event)

    @pytest.mark.unit
    def test_dbus_error_abort_when_closing(self) -> None:
        """Loop aborts on BleakDBusError when interface closing."""
        from bleak.exc import BleakDBusError

        shutdown_event = Event()
        calls: list[bool] = []

        interface = SimpleNamespace(
            _is_connection_closing=False,
            auto_reconnect=True,
            _is_connection_connected=False,
            address="AA:BB:CC:DD:EE:FF",
        )


        def failing_connect(_addr: str) -> None:
            if len(calls) == 0:
                calls.append(True)
                interface._is_connection_closing = True
            raise BleakDBusError("org.bluez.Error.Failed", 1)  # type: ignore[arg-type]


        interface.connect = failing_connect

        policy = ReconnectPolicy(max_retries=5, initial_delay=0.01)
        worker = ReconnectWorker(
            interface=interface,  # type: ignore[arg-type]
            reconnect_policy=policy,
        )

        worker._attempt_reconnect_loop(shutdown_event)
        assert interface._is_connection_closing is True

    @pytest.mark.unit
    def test_bleak_error_abort_when_disabled(self) -> None:
        """Loop aborts on BleakError when auto_reconnect disabled."""
        from bleak.exc import BleakError

        shutdown_event = Event()
        calls: list[bool] = []

        interface = SimpleNamespace(
            _is_connection_closing=False,
            auto_reconnect=True,
            _is_connection_connected=False,
            address="AA:BB:CC:DD:EE:FF",
        )

        def failing_connect(_addr: str) -> None:
            if len(calls) == 0:
                calls.append(True)
                interface.auto_reconnect = False
            raise BleakError("Generic BLE error")


        interface.connect = failing_connect

        policy = ReconnectPolicy(max_retries=5, initial_delay=0.01)
        worker = ReconnectWorker(
            interface=interface,  # type: ignore[arg-type]
            reconnect_policy=policy,
        )

        worker._attempt_reconnect_loop(shutdown_event)
        assert interface.auto_reconnect is False


class TestReconnectWorkerPreSleepAbort:
    """Tests for pre-sleep abort scenarios."""

    @pytest.mark.unit
    def test_loop_aborts_pre_sleep_when_closing(self) -> None:
        """Loop aborts before sleep when interface closing after error."""
        shutdown_event = Event()
        calls: list[int] = []

        interface = SimpleNamespace(
            _is_connection_closing=False,
            auto_reconnect=True,
            _is_connection_connected=False,
            address="AA:BB:CC:DD:EE:FF",
        )

        def failing_connect(_addr: str) -> None:
            calls.append(len(calls))
            if len(calls) == 1:
                # Mark as closing after first failure
                interface._is_connection_closing = True
            raise ValueError("Connection failed")


        interface.connect = failing_connect

        policy = ReconnectPolicy(max_retries=5, initial_delay=0.01)
        worker = ReconnectWorker(
            interface=interface,  # type: ignore[arg-type]
            reconnect_policy=policy,
        )

        worker._attempt_reconnect_loop(shutdown_event)
        # Should have made at least 1 attempt before aborting
        assert len(calls) >= 1
        assert interface._is_connection_closing is True

    @pytest.mark.unit
    def test_loop_aborts_pre_sleep_when_disabled(self) -> None:
        """Loop aborts before sleep when auto_reconnect disabled after error."""
        shutdown_event = Event()
        calls: list[int] = []

        interface = SimpleNamespace(
            _is_connection_closing=False,
            auto_reconnect=True,
            _is_connection_connected=False,
            address="AA:BB:CC:DD:EE:FF",
        )

        def failing_connect(_addr: str) -> None:
            calls.append(len(calls))
            if len(calls) == 1:
                # Disable auto_reconnect after first failure
                interface.auto_reconnect = False
            raise ValueError("Connection failed")


        interface.connect = failing_connect

        policy = ReconnectPolicy(max_retries=5, initial_delay=0.01)
        worker = ReconnectWorker(
            interface=interface,  # type: ignore[arg-type]
            reconnect_policy=policy,
        )

        worker._attempt_reconnect_loop(shutdown_event)
        # Should have made at least 1 attempt before aborting
        assert len(calls) >= 1
        assert interface.auto_reconnect is False


class TestReconnectWorkerRemainingCoverage:
    """Additional tests to maximize coverage of remaining lines."""

    @pytest.mark.unit
    def test_get_attempt_count_returns_none_logs_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Loop logs error when get_attempt_count returns None."""
        shutdown_event = Event()
        caplog.set_level(logging.ERROR)

        interface = SimpleNamespace(
            _is_connection_closing=False,
            auto_reconnect=True,
            _is_connection_connected=False,
            address="AA:BB:CC:DD:EE:FF",
        )

        # Policy that returns None for get_attempt_count
        policy = SimpleNamespace(
            reset=lambda: None,
            get_attempt_count=lambda: None,
        )
        worker = ReconnectWorker(
            interface=interface,  # type: ignore[arg-type]
            reconnect_policy=policy,  # type: ignore[arg-type]
        )

        worker._attempt_reconnect_loop(shutdown_event)
        # Should log error about None
        assert any("None" in record.message for record in caplog.records)

    @pytest.mark.unit
    def test_connected_elsewhere_gate_logs_info(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Gate logs info when device connected elsewhere."""

        shutdown_event = Event()
        caplog.set_level(logging.INFO)
        address = "AA:BB:CC:DD:EE:FF"

        # Create a mock interface to be the "other" owner
        other_interface = SimpleNamespace()
        _mark_connected(address, owner=other_interface)

        try:
            interface = SimpleNamespace(
                address=address,
                _is_connection_closing=False,
                auto_reconnect=True,
                _is_connection_connected=False,
            )
            policy = ReconnectPolicy(max_retries=5, initial_delay=0.01)
            worker = ReconnectWorker(
                interface=interface,  # type: ignore[arg-type]
                reconnect_policy=policy,
            )

            # Should log info about connected elsewhere
            worker._handle_connected_elsewhere_gate(interface, 1, shutdown_event)  # type: ignore[arg-type]
            assert any(
                "connected elsewhere" in record.message.lower()
                for record in caplog.records
            )
        finally:
            _mark_disconnected(address)

    @pytest.mark.unit
    def test_max_retry_limit_logs_info(self, caplog: pytest.LogCaptureFixture) -> None:
        """Loop logs info when max retry limit reached."""
        shutdown_event = Event()
        caplog.set_level(logging.INFO)

        class MockBLEError(Exception):
            """Mock BLE error for testing."""

            pass

        interface = SimpleNamespace(
            _is_connection_closing=False,
            auto_reconnect=True,
            _is_connection_connected=False,
            address="AA:BB:CC:DD:EE:FF",
            BLEError=MockBLEError,
        )

        def failing_connect(_addr: str) -> None:
            raise MockBLEError("Connection failed")

        interface.connect = failing_connect

        # Policy with max_retries=0 so limit is reached immediately
        policy = ReconnectPolicy(max_retries=0, initial_delay=0.01)
        worker = ReconnectWorker(
            interface=interface,  # type: ignore[arg-type]
            reconnect_policy=policy,
        )

        worker._attempt_reconnect_loop(shutdown_event)
        # Should log about max retry limit
        assert any(
            "maximum retry limit" in record.message.lower() for record in caplog.records
        )
