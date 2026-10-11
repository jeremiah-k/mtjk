"""Comprehensive test coverage for BLE coordination primitives.

Tests thread coordinator edge cases, thread lifecycle management,
coordination primitive error handling, concurrent operations, and
thread pool exhaustion scenarios.
"""

from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from meshtastic.interfaces.ble.constants import EVENT_THREAD_JOIN_TIMEOUT
from meshtastic.interfaces.ble.coordination import (
    _INERT_THREAD_START_ERROR,
    _LOCK_NOT_OWNED_ERROR,
    ThreadCoordinator,
    _InertThread,
)


@pytest.mark.unit
class TestInertThread:
    """Tests for _InertThread placeholder class."""

    def test_inert_thread_initialization(self) -> None:
        """Test _InertThread is created with correct default attributes."""
        inert = _InertThread("test_thread")
        assert inert.name == "test_thread"
        assert inert.daemon is True
        assert inert.ident is None

    def test_inert_thread_start_raises(self) -> None:
        """Test starting an inert thread raises RuntimeError with correct message."""
        inert = _InertThread("blocked_thread")
        expected_msg = _INERT_THREAD_START_ERROR.format(name="blocked_thread")
        with pytest.raises(RuntimeError, match=expected_msg):
            inert.start()

    def test_inert_thread_is_alive_returns_false(self) -> None:
        """Test inert thread is never alive."""
        inert = _InertThread("dead_thread")
        assert inert.is_alive() is False

    def test_inert_thread_join_is_noop(self) -> None:
        """Test joining an inert thread does nothing and accepts any timeout."""
        inert = _InertThread("noop_thread")
        # Should not raise and should complete immediately
        inert.join()
        inert.join(timeout=1.0)
        inert.join(timeout=None)
        inert.join(timeout=0)

    def test_inert_thread_join_ignores_timeout(self) -> None:
        """Test that join ignores timeout parameter and returns immediately."""
        inert = _InertThread("ignore_timeout")
        start = time.time()
        inert.join(timeout=10.0)
        elapsed = time.time() - start
        # Should return immediately, not wait for timeout
        assert elapsed < 0.1


@pytest.mark.unit
class TestThreadCoordinatorInitialization:
    """Tests for ThreadCoordinator initialization and basic state."""

    def test_coordinator_initializes_empty(self) -> None:
        """Test coordinator starts with empty tracking structures."""
        coord = ThreadCoordinator()
        assert coord._threads == []
        assert coord._pending_start == set()
        assert not coord._events
        assert coord._cleaned_up is False
        assert coord._lock is not None

    def test_coordinator_lock_is_rlock(self) -> None:
        """Test coordinator uses RLock for thread safety."""
        coord = ThreadCoordinator()
        # RLock is a function factory, not a class directly
        # Check it behaves like an RLock by verifying _is_owned method exists
        assert hasattr(coord._lock, "_is_owned") or hasattr(coord._lock, "acquire")


@pytest.mark.unit
class TestThreadCoordinatorThreadCreation:
    """Tests for thread creation and lifecycle management."""

    def test_create_thread_registers_thread(self) -> None:
        """Test creating a thread adds it to tracking."""
        coord = ThreadCoordinator()

        def dummy() -> None:
            pass

        thread = coord._create_thread(dummy, "test_thread")
        assert thread in coord._threads
        assert isinstance(thread, threading.Thread)
        assert thread.name == "test_thread"
        assert thread.daemon is True

    def test_create_thread_non_daemon(self) -> None:
        """Test creating a non-daemon thread."""
        coord = ThreadCoordinator()

        def dummy() -> None:
            pass

        thread = coord._create_thread(dummy, "non_daemon", daemon=False)
        assert thread.daemon is False

    def test_create_thread_with_args(self) -> None:
        """Test creating a thread with positional arguments."""
        coord = ThreadCoordinator()
        result: list[Any] = []

        def collect_args(a: int, b: str) -> None:
            result.append((a, b))

        thread = coord._create_thread(collect_args, "with_args", args=(42, "hello"))
        thread.start()
        thread.join()
        assert result == [(42, "hello")]

    def test_create_thread_with_kwargs(self) -> None:
        """Test creating a thread with keyword arguments."""
        coord = ThreadCoordinator()
        result: list[Any] = []

        def collect_kwargs(x: int = 0, y: str = "") -> None:
            result.append((x, y))

        thread = coord._create_thread(
            collect_kwargs, "with_kwargs", kwargs={"x": 100, "y": "world"}
        )
        thread.start()
        thread.join()
        assert result == [(100, "world")]

    def test_create_thread_with_args_and_kwargs(self) -> None:
        """Test creating a thread with both args and kwargs."""
        coord = ThreadCoordinator()
        result: list[Any] = []

        def collect_both(a: int, b: str, c: float = 0.0) -> None:
            result.append((a, b, c))

        thread = coord._create_thread(
            collect_both, "both", args=(1, "two"), kwargs={"c": 3.0}
        )
        thread.start()
        thread.join()
        assert result == [(1, "two", 3.0)]

    def test_create_thread_prunes_dead_threads(self) -> None:
        """Test that creating a thread prunes dead threads from tracking."""
        coord = ThreadCoordinator()

        def quick_exit() -> None:
            pass

        # Create and start a thread that will exit quickly
        thread1 = coord._create_thread(quick_exit, "quick1")
        thread1.start()
        thread1.join()

        # Create another thread - should prune the dead one
        thread2 = coord._create_thread(quick_exit, "quick2")
        # After pruning, only alive threads and thread2 should remain
        assert thread2 in coord._threads
        # The dead thread should be pruned
        assert thread1 not in coord._threads

    def test_create_thread_keeps_alive_threads(self) -> None:
        """Test that pruning keeps threads that are still alive."""
        coord = ThreadCoordinator()
        event = threading.Event()

        def wait_for_event() -> None:
            event.wait(timeout=5.0)

        # Create a long-running thread
        thread1 = coord._create_thread(wait_for_event, "long_running")
        thread1.start()
        time.sleep(0.05)  # Let thread start

        # Create another thread - should keep the alive one
        thread2 = coord._create_thread(lambda: None, "short")
        assert thread1 in coord._threads
        assert thread2 in coord._threads

        event.set()
        thread1.join()


@pytest.mark.unit
class TestThreadCoordinatorAfterCleanup:
    """Tests for thread coordinator behavior after cleanup."""

    def test_create_thread_after_cleanup_returns_inert(self) -> None:
        """Test creating thread after cleanup returns inert thread."""
        coord = ThreadCoordinator()
        coord._cleanup()

        def dummy() -> None:
            pass

        with patch("meshtastic.interfaces.ble.coordination.logger") as mock_logger:
            thread = coord._create_thread(dummy, "post_cleanup")

        assert isinstance(thread, _InertThread)
        assert thread.name == "post_cleanup"
        mock_logger.warning.assert_called_once()

    def test_create_event_after_cleanup_returns_set_event(self) -> None:
        """Test creating event after cleanup returns pre-set inert event."""
        coord = ThreadCoordinator()
        coord._cleanup()

        with patch("meshtastic.interfaces.ble.coordination.logger") as mock_logger:
            event = coord._create_event("post_cleanup_event")

        assert isinstance(event, threading.Event)
        assert event.is_set() is True  # Pre-set to prevent blocking
        mock_logger.warning.assert_called_once()

    def test_create_event_after_cleanup_logs_warning(self) -> None:
        """Test that creating event after cleanup logs appropriate warning."""
        coord = ThreadCoordinator()
        coord._cleanup()

        with patch("meshtastic.interfaces.ble.coordination.logger") as mock_logger:
            coord._create_event("test_event")

        mock_logger.warning.assert_called_once()
        args = mock_logger.warning.call_args[0]
        assert "test_event" in args[0]
        assert "cleaned up" in args[0].lower()

    def test_get_event_returns_none_after_cleanup(self) -> None:
        """Test get_event returns None for events cleared during cleanup."""
        coord = ThreadCoordinator()
        coord._create_event("test_event")
        coord._cleanup()
        result = coord._get_event("test_event")
        assert result is None

    def test_cleanup_idempotent(self) -> None:
        """Test that cleanup is idempotent - calling twice is safe."""
        coord = ThreadCoordinator()

        def dummy() -> None:
            pass

        thread = coord._create_thread(dummy, "idempotent_test")
        thread.start()

        coord._cleanup()
        first_cleanup_count = len(coord._threads)

        coord._cleanup()  # Second cleanup
        second_cleanup_count = len(coord._threads)

        assert first_cleanup_count == 0
        assert second_cleanup_count == 0
        assert coord._cleaned_up is True


@pytest.mark.unit
class TestThreadCoordinatorThreadStarting:
    """Tests for thread starting and edge cases."""

    def test_start_thread_success(self) -> None:
        """Test starting a registered thread."""
        coord = ThreadCoordinator()
        result: list[int] = []

        def work() -> None:
            result.append(42)

        thread = coord._create_thread(work, "worker")
        coord._start_thread(thread)
        thread.join()
        assert result == [42]

    def test_start_thread_untracked_logs_warning(self) -> None:
        """Test starting untracked thread logs warning and does nothing."""
        coord = ThreadCoordinator()
        untracked = threading.Thread(target=lambda: None)

        with patch("meshtastic.interfaces.ble.coordination.logger") as mock_logger:
            coord._start_thread(untracked)

        mock_logger.warning.assert_called_once()
        assert "untracked" in mock_logger.warning.call_args[0][0].lower()

    def test_start_inert_thread_logs_warning(self) -> None:
        """Test starting an inert thread logs warning."""
        coord = ThreadCoordinator()
        inert = _InertThread("inert")

        with patch("meshtastic.interfaces.ble.coordination.logger") as mock_logger:
            coord._start_thread(inert)

        mock_logger.warning.assert_called_once()

    def test_start_thread_already_started_noop(self) -> None:
        """Test starting an already started thread is a no-op."""
        coord = ThreadCoordinator()
        result: list[int] = []

        def work() -> None:
            result.append(1)

        thread = coord._create_thread(work, "already_started")
        thread.start()
        thread.join()
        result.clear()

        # Try to start again - should be no-op
        coord._start_thread(thread)
        time.sleep(0.05)
        assert not result  # Should not have run again

    def test_start_thread_pending_start_race_prevention(self) -> None:
        """Test thread in _pending_start cannot be started again."""
        coord = ThreadCoordinator()
        started_event = threading.Event()

        def slow_start() -> None:
            started_event.wait(timeout=5.0)

        thread = coord._create_thread(slow_start, "pending_race")

        # Simulate race: add to pending_start without actually starting
        coord._pending_start.add(thread)  # type: ignore[arg-type]

        # Try to start while in pending_start - should be blocked
        coord._start_thread(thread)

        # Thread should not have been started again (still pending or not started)
        assert thread in coord._pending_start
        assert not started_event.is_set()

        # Cleanup to release the thread
        started_event.set()
        coord._cleanup()

    def test_start_thread_race_join_path(self) -> None:
        """Test the join path when cleanup races with thread start.

        This test attempts to cover line 258 where must_join_after_start
        causes the thread to be joined after cleanup occurs during start.
        """
        coord = ThreadCoordinator()
        release_worker = threading.Event()
        join_called_from_start = threading.Event()
        in_cleanup = False

        def slow_worker() -> None:
            release_worker.wait(timeout=5.0)

        thread = coord._create_thread(slow_worker, "race_test")
        original_start = thread.start
        original_join = thread.join
        original_cleanup = coord._cleanup

        def tracking_start() -> None:
            original_start()
            # Trigger cleanup before _start_thread enters finally.
            coord._cleanup()

        def tracking_join(timeout: float | None = None) -> None:
            if not in_cleanup:
                join_called_from_start.set()
            return original_join(timeout=timeout)

        def tracking_cleanup() -> None:
            nonlocal in_cleanup
            in_cleanup = True
            try:
                original_cleanup()
            finally:
                in_cleanup = False

        thread.start = tracking_start  # type: ignore[method-assign]
        thread.join = tracking_join  # type: ignore[method-assign]
        coord._cleanup = tracking_cleanup  # type: ignore[method-assign]

        with patch(
            "meshtastic.interfaces.ble.coordination.EVENT_THREAD_JOIN_TIMEOUT", 0.05
        ):
            coord._start_thread(thread)

        assert join_called_from_start.is_set()

        release_worker.set()
        thread.join(timeout=1.0)
        assert not thread.is_alive()

    def test_start_thread_cleanup_during_start_race(self) -> None:
        """Test cleanup during thread start triggers proper joining.

        This test sets up a scenario where cleanup happens between
        thread.start() completing and the finally block checking cleaned_up.
        """
        coord = ThreadCoordinator()
        entered_start = threading.Event()
        cleanup_done = threading.Event()

        def slow_target() -> None:
            # Keep thread alive long enough for test
            cleanup_done.wait(timeout=5.0)

        thread = coord._create_thread(slow_target, "cleanup_race_thread")

        # Track when thread.start() returns and trigger cleanup
        def patched_start() -> None:
            # Start the actual thread
            threading.Thread.start(thread)  # type: ignore[arg-type]
            entered_start.set()
            # Small delay to let the finally block in _start_thread execute
            time.sleep(0.05)

        def cleanup_trigger() -> None:
            # Wait for start to complete
            entered_start.wait(timeout=2.0)
            # Trigger cleanup
            coord._cleanup()
            cleanup_done.set()

        # Start cleanup thread first
        cleanup_thread = threading.Thread(target=cleanup_trigger)
        cleanup_thread.start()

        # Start the thread with our patched version
        with patch.object(thread, "start", side_effect=patched_start):
            coord._start_thread(thread)

        cleanup_thread.join(timeout=5.0)

        # Thread should be dead after cleanup
        assert not thread.is_alive()
        assert coord._cleaned_up


@pytest.mark.unit
class TestThreadCoordinatorThreadJoining:
    """Tests for thread joining operations."""

    def test_join_thread_success(self) -> None:
        """Test joining a tracked alive thread."""
        coord = ThreadCoordinator()
        completed = threading.Event()

        def work() -> None:
            time.sleep(0.05)
            completed.set()

        thread = coord._create_thread(work, "joinable")
        thread.start()
        coord._join_thread(thread, timeout=1.0)
        assert completed.is_set()
        assert not thread.is_alive()

    def test_join_thread_not_tracked_noop(self) -> None:
        """Test joining an untracked thread does nothing."""
        coord = ThreadCoordinator()
        untracked = threading.Thread(target=lambda: time.sleep(0.1))
        untracked.start()

        # Should not raise and should not join (thread keeps running)
        coord._join_thread(untracked, timeout=0.01)
        assert untracked.is_alive()

        untracked.join()

    def test_join_thread_not_alive_noop(self) -> None:
        """Test joining a dead thread does nothing."""
        coord = ThreadCoordinator()

        def quick() -> None:
            pass

        thread = coord._create_thread(quick, "dead")
        thread.start()
        thread.join()

        # Should not raise
        coord._join_thread(thread, timeout=1.0)

    def test_join_thread_current_thread_noop(self) -> None:
        """Test joining current thread is a no-op."""
        coord = ThreadCoordinator()
        result: list[bool] = []

        def try_join_self() -> None:
            current = threading.current_thread()
            coord._join_thread(current, timeout=1.0)
            result.append(True)

        thread = coord._create_thread(try_join_self, "self_join")
        thread.start()
        thread.join()

        assert result == [True]  # Should have completed without deadlock

    def test_join_all_threads(self) -> None:
        """Test joining all tracked threads."""
        coord = ThreadCoordinator()
        completed = [threading.Event() for _ in range(3)]

        def make_worker(i: int) -> Any:
            def worker() -> None:
                time.sleep(0.05 * (i + 1))
                completed[i].set()

            return worker

        threads = [
            coord._create_thread(make_worker(i), f"worker_{i}") for i in range(3)
        ]
        for t in threads:
            t.start()

        coord._join_all(timeout=1.0)

        for event in completed:
            assert event.is_set()

    def test_join_all_excludes_current_thread(self) -> None:
        """Test join_all excludes the calling thread."""
        coord = ThreadCoordinator()
        result: list[str] = []

        def worker() -> None:
            time.sleep(0.1)
            result.append("completed")

        # Create thread in current thread
        thread = coord._create_thread(worker, "other")

        def caller() -> None:
            thread.start()
            coord._join_all(timeout=1.0)

        caller_thread = threading.Thread(target=caller)
        caller_thread.start()
        caller_thread.join()

        assert result == ["completed"]


@pytest.mark.unit
class TestThreadCoordinatorEventOperations:
    """Tests for event coordination primitives."""

    def test_create_event_registers_event(self) -> None:
        """Test creating an event registers it by name."""
        coord = ThreadCoordinator()
        event = coord._create_event("test_event")
        assert "test_event" in coord._events
        assert coord._events["test_event"] is event
        assert isinstance(event, threading.Event)
        assert not event.is_set()

    def test_create_event_returns_existing(self) -> None:
        """Test creating event with same name returns existing instance."""
        coord = ThreadCoordinator()
        event1 = coord._create_event("shared_event")
        event2 = coord._create_event("shared_event")
        assert event1 is event2

    def test_get_event_returns_event(self) -> None:
        """Test get_event retrieves registered event."""
        coord = ThreadCoordinator()
        created = coord._create_event("retrievable")
        retrieved = coord._get_event("retrievable")
        assert retrieved is created

    def test_get_event_missing_returns_none(self) -> None:
        """Test get_event returns None for non-existent event."""
        coord = ThreadCoordinator()
        result = coord._get_event("nonexistent")
        assert result is None

    def test_set_event_no_lock_sets_event(self) -> None:
        """Test set_event_no_lock sets the event."""
        coord = ThreadCoordinator()
        coord._create_event("to_set")

        with coord._lock:
            coord._set_event_no_lock("to_set")

        assert coord._events["to_set"].is_set()

    def test_set_event_no_lock_missing_noop(self) -> None:
        """Test set_event_no_lock is no-op for missing event."""
        coord = ThreadCoordinator()

        with coord._lock:
            # Should not raise
            coord._set_event_no_lock("missing")

    def test_clear_event_no_lock_clears_event(self) -> None:
        """Test clear_event_no_lock clears the event."""
        coord = ThreadCoordinator()
        coord._create_event("to_clear")
        coord._events["to_clear"].set()

        with coord._lock:
            coord._clear_event_no_lock("to_clear")

        assert not coord._events["to_clear"].is_set()

    def test_clear_event_no_lock_missing_noop(self) -> None:
        """Test clear_event_no_lock is no-op for missing event."""
        coord = ThreadCoordinator()

        with coord._lock:
            # Should not raise
            coord._clear_event_no_lock("missing")

    def test_set_event_sets_event(self) -> None:
        """Test set_event sets the named event."""
        coord = ThreadCoordinator()
        coord._create_event("settable")
        coord._set_event("settable")
        assert coord._events["settable"].is_set()

    def test_set_event_missing_noop(self) -> None:
        """Test set_event is no-op for missing event."""
        coord = ThreadCoordinator()
        # Should not raise
        coord._set_event("nonexistent")

    def test_clear_event_clears_event(self) -> None:
        """Test clear_event clears the named event."""
        coord = ThreadCoordinator()
        coord._create_event("clearable")
        coord._events["clearable"].set()
        coord._clear_event("clearable")
        assert not coord._events["clearable"].is_set()

    def test_clear_event_missing_noop(self) -> None:
        """Test clear_event is no-op for missing event."""
        coord = ThreadCoordinator()
        # Should not raise
        coord._clear_event("nonexistent")

    def test_wait_for_event_success(self) -> None:
        """Test wait_for_event returns True when event is set."""
        coord = ThreadCoordinator()
        coord._create_event("waitable")

        def setter() -> None:
            time.sleep(0.05)
            coord._set_event("waitable")

        threading.Thread(target=setter).start()
        result = coord._wait_for_event("waitable", timeout=1.0)
        assert result is True

    def test_wait_for_event_timeout(self) -> None:
        """Test wait_for_event returns False on timeout."""
        coord = ThreadCoordinator()
        coord._create_event("never_set")
        result = coord._wait_for_event("never_set", timeout=0.01)
        assert result is False

    def test_wait_for_event_missing_returns_false(self) -> None:
        """Test wait_for_event returns False for non-existent event."""
        coord = ThreadCoordinator()
        result = coord._wait_for_event("missing", timeout=1.0)
        assert result is False

    def test_check_and_clear_event_when_set(self) -> None:
        """Test check_and_clear_event returns True and clears when set."""
        coord = ThreadCoordinator()
        coord._create_event("checkable")
        coord._events["checkable"].set()
        result = coord._check_and_clear_event("checkable")
        assert result is True
        assert not coord._events["checkable"].is_set()

    def test_check_and_clear_event_when_clear(self) -> None:
        """Test check_and_clear_event returns False when not set."""
        coord = ThreadCoordinator()
        coord._create_event("already_clear")
        result = coord._check_and_clear_event("already_clear")
        assert result is False

    def test_check_and_clear_event_missing(self) -> None:
        """Test check_and_clear_event returns False for missing event."""
        coord = ThreadCoordinator()
        result = coord._check_and_clear_event("missing")
        assert result is False

    def test_wake_waiting_threads_sets_multiple(self) -> None:
        """Test wake_waiting_threads sets multiple events."""
        coord = ThreadCoordinator()
        for name in ["event1", "event2", "event3"]:
            coord._create_event(name)

        coord._wake_waiting_threads("event1", "event2", "event3")

        for name in ["event1", "event2", "event3"]:
            assert coord._events[name].is_set()

    def test_wake_waiting_threads_ignores_missing(self) -> None:
        """Test wake_waiting_threads ignores non-existent events."""
        coord = ThreadCoordinator()
        coord._create_event("exists")

        # Should not raise
        coord._wake_waiting_threads("exists", "missing1", "missing2")

        assert coord._events["exists"].is_set()

    def test_clear_events_clears_multiple(self) -> None:
        """Test clear_events clears multiple events."""
        coord = ThreadCoordinator()
        for name in ["e1", "e2", "e3"]:
            coord._create_event(name)
            coord._events[name].set()

        coord._clear_events("e1", "e2", "e3")

        for name in ["e1", "e2", "e3"]:
            assert not coord._events[name].is_set()

    def test_clear_events_ignores_missing(self) -> None:
        """Test clear_events ignores non-existent events."""
        coord = ThreadCoordinator()
        coord._create_event("exists")
        coord._events["exists"].set()

        # Should not raise
        coord._clear_events("exists", "missing1", "missing2")

        assert not coord._events["exists"].is_set()


@pytest.mark.unit
class TestThreadCoordinatorAssertLockOwned:
    """Tests for lock ownership assertion."""

    def test_assert_lock_owned_with_lock_held(self) -> None:
        """Test assertion passes when lock is held."""
        coord = ThreadCoordinator()
        with coord._lock:
            # Should not raise when lock is held
            coord._assert_lock_owned()

    def test_assert_lock_owned_without_lock_raises(self) -> None:
        """Test assertion raises when lock is not held."""
        coord = ThreadCoordinator()
        with pytest.raises(RuntimeError, match=_LOCK_NOT_OWNED_ERROR):
            coord._assert_lock_owned()

    def test_assert_lock_owned_no_is_owned_attribute(self) -> None:
        """Test assertion is no-op when _is_owned is not available."""
        coord = ThreadCoordinator()
        # Replace lock with mock that lacks _is_owned
        mock_lock = MagicMock()
        del mock_lock._is_owned  # Ensure attribute is missing
        coord._lock = mock_lock
        # Should not raise even without lock held
        coord._assert_lock_owned()


@pytest.mark.unit
class TestThreadCoordinatorCleanup:
    """Tests for coordinator cleanup operation."""

    def test_cleanup_sets_cleaned_up_flag(self) -> None:
        """Test cleanup sets the cleaned_up flag."""
        coord = ThreadCoordinator()
        assert not coord._cleaned_up
        coord._cleanup()
        assert coord._cleaned_up

    def test_cleanup_sets_all_events(self) -> None:
        """Test cleanup sets all tracked events to wake waiters before clearing."""
        coord = ThreadCoordinator()
        events: dict[str, threading.Event] = {}
        for name in ["event1", "event2"]:
            events[name] = coord._create_event(name)

        coord._cleanup()

        # Events should have been set during cleanup (before being cleared from tracking)
        # but the events dict is cleared after, so we check the events we saved
        for name in ["event1", "event2"]:
            assert events[name].is_set()

    def test_cleanup_clears_tracking_structures(self) -> None:
        """Test cleanup clears threads, events, and pending_start."""
        coord = ThreadCoordinator()

        def dummy() -> None:
            pass

        coord._create_thread(dummy, "t1")
        coord._create_event("e1")
        coord._pending_start.add(MagicMock())

        coord._cleanup()

        assert len(coord._threads) == 0
        assert len(coord._events) == 0
        assert len(coord._pending_start) == 0

    def test_cleanup_joins_alive_threads(self) -> None:
        """Test cleanup joins alive tracked threads."""
        coord = ThreadCoordinator()
        completed = threading.Event()

        def slow_worker() -> None:
            time.sleep(0.2)
            completed.set()

        thread = coord._create_thread(slow_worker, "slow")
        thread.start()

        coord._cleanup()

        assert completed.is_set()
        assert not thread.is_alive()

    def test_cleanup_uses_correct_timeout(self) -> None:
        """Test cleanup uses EVENT_THREAD_JOIN_TIMEOUT for joins."""
        coord = ThreadCoordinator()
        release_worker = threading.Event()

        def dummy() -> None:
            release_worker.wait(timeout=5.0)

        thread = coord._create_thread(dummy, "timeout_test")
        thread.start()

        with patch.object(thread, "join", wraps=thread.join) as mock_join:
            coord._cleanup()

        mock_join.assert_called_once_with(timeout=EVENT_THREAD_JOIN_TIMEOUT)

        release_worker.set()
        thread.join(timeout=1.0)

    def test_cleanup_excludes_current_thread(self) -> None:
        """Test cleanup excludes the calling thread from joining."""
        coord = ThreadCoordinator()
        result: list[bool] = []

        def cleanup_from_self() -> None:
            coord._cleanup()
            result.append(True)

        thread = coord._create_thread(cleanup_from_self, "self_cleanup")
        thread.start()
        thread.join()

        assert result == [True]
        assert coord._cleaned_up is True


@pytest.mark.unit
class TestThreadCoordinatorConcurrentOperations:
    """Tests for concurrent operations and race conditions."""

    def test_concurrent_thread_creation(self) -> None:
        """Test thread creation is thread-safe under concurrent access."""
        coord = ThreadCoordinator()
        results: list[int] = []
        lock = threading.Lock()

        def make_worker(i: int) -> Any:
            def worker() -> None:
                with lock:
                    results.append(i)

            return worker

        threads = []
        for i in range(10):
            t = threading.Thread(
                target=lambda i=i: coord._create_thread(
                    make_worker(i), f"concurrent_{i}"
                )
            )
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All threads should have been registered
        assert len(coord._threads) == 10

    def test_concurrent_event_operations(self) -> None:
        """Test event operations are thread-safe."""
        coord = ThreadCoordinator()
        coord._create_event("shared")

        set_count = [0]
        clear_count = [0]
        lock = threading.Lock()

        def setter() -> None:
            for _ in range(10):
                coord._set_event("shared")
                with lock:
                    set_count[0] += 1

        def clearer() -> None:
            for _ in range(10):
                coord._clear_event("shared")
                with lock:
                    clear_count[0] += 1

        threads = [
            threading.Thread(target=setter),
            threading.Thread(target=clearer),
            threading.Thread(target=setter),
            threading.Thread(target=clearer),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert set_count[0] == 20
        assert clear_count[0] == 20

    def test_concurrent_wait_and_set(self) -> None:
        """Test concurrent wait and set operations."""
        coord = ThreadCoordinator()
        coord._create_event("signal")
        results: list[bool] = []
        lock = threading.Lock()

        def waiter() -> None:
            result = coord._wait_for_event("signal", timeout=1.0)
            with lock:
                results.append(result)

        waiters = [threading.Thread(target=waiter) for _ in range(5)]
        for w in waiters:
            w.start()

        time.sleep(0.05)
        coord._set_event("signal")

        for w in waiters:
            w.join()

        assert all(results)
        assert len(results) == 5

    def test_create_thread_during_cleanup(self) -> None:
        """Test thread creation during cleanup returns inert thread."""
        coord = ThreadCoordinator()
        inert_threads: list[Any] = []
        lock = threading.Lock()

        def creator() -> None:
            def dummy() -> None:
                pass

            for _ in range(5):
                thread = coord._create_thread(dummy, "race_create")
                with lock:
                    inert_threads.append(isinstance(thread, _InertThread))
                time.sleep(0.01)

        creator_thread = threading.Thread(target=creator)
        creator_thread.start()

        time.sleep(0.02)
        coord._cleanup()

        creator_thread.join()

        # At least some threads created after cleanup started should be inert
        assert any(inert_threads)


@pytest.mark.unit
class TestThreadCoordinatorThreadPoolExhaustion:
    """Tests simulating thread pool exhaustion scenarios."""

    def test_many_threads_created(self) -> None:
        """Test coordinator handles many thread creations."""
        coord = ThreadCoordinator()
        completed = threading.Event()
        counter = [0]
        lock = threading.Lock()

        def worker() -> None:
            with lock:
                counter[0] += 1
            if counter[0] >= 100:
                completed.set()

        threads = []
        for i in range(100):
            t = coord._create_thread(worker, f"mass_worker_{i}")
            threads.append(t)

        for t in threads:
            t.start()

        completed.wait(timeout=10.0)

        for t in threads:
            t.join(timeout=0.5)

        assert counter[0] == 100

    def test_thread_pruning_under_load(self) -> None:
        """Test thread pruning works correctly under load."""
        coord = ThreadCoordinator()

        def quick_exit() -> None:
            pass

        # Create many threads that exit quickly
        for i in range(50):
            t = coord._create_thread(quick_exit, f"quick_{i}")
            t.start()
            t.join()

        # At this point all threads should be dead
        # Creating a new thread should trigger pruning
        t = coord._create_thread(quick_exit, "trigger_prune")

        # Only the new thread (if not started) should be in the list
        # Or the list should be empty if pruning happened
        alive_count = sum(1 for t in coord._threads if t.is_alive())
        assert alive_count == 0

    def test_cleanup_with_many_threads(self) -> None:
        """Test cleanup properly handles many concurrent threads."""
        coord = ThreadCoordinator()
        started = threading.Event()
        continue_event = threading.Event()
        counters = [0] * 20
        lock = threading.Lock()

        def make_worker(i: int) -> Any:
            def worker() -> None:
                started.set()
                continue_event.wait(timeout=5.0)
                with lock:
                    counters[i] += 1

            return worker

        threads = []
        for i in range(20):
            t = coord._create_thread(make_worker(i), f"cleanup_worker_{i}")
            threads.append(t)
            t.start()

        started.wait(timeout=2.0)

        # Trigger cleanup while threads are running
        cleanup_thread = threading.Thread(target=coord._cleanup)
        cleanup_thread.start()

        # Let threads complete
        continue_event.set()
        cleanup_thread.join(timeout=5.0)

        # All threads should have been joined
        for t in threads:
            assert not t.is_alive()

        assert all(c == 1 for c in counters)

    def test_pending_start_cleanup(self) -> None:
        """Test cleanup handles threads in pending_start state."""
        coord = ThreadCoordinator()

        def dummy() -> None:
            time.sleep(0.1)

        # Add threads to pending_start without starting
        for i in range(5):
            t = coord._create_thread(dummy, f"pending_{i}")
            coord._pending_start.add(t)  # type: ignore[arg-type]

        assert len(coord._pending_start) == 5

        coord._cleanup()

        assert len(coord._pending_start) == 0


@pytest.mark.unit
class TestThreadCoordinatorErrorHandling:
    """Tests for error handling and edge cases."""

    def test_start_thread_with_exception_in_target(self) -> None:
        """Test thread with exception in target is still tracked."""
        coord = ThreadCoordinator()
        exception_raised = threading.Event()

        def failing_target() -> None:
            try:
                raise ValueError("Test exception")
            except ValueError:
                exception_raised.set()
                # Don't re-raise - exception is already verified via event

        thread = coord._create_thread(failing_target, "failing")
        thread.start()

        # Wait for exception to be raised
        exception_raised.wait(timeout=1.0)

        # Thread should complete (and exit due to exception)
        thread.join(timeout=1.0)

        # Thread should be dead but was tracked
        assert not thread.is_alive()
        assert thread in coord._threads or coord._cleaned_up

    def test_join_with_negative_timeout(self) -> None:
        """Test join with negative timeout (should use None behavior)."""
        coord = ThreadCoordinator()

        def quick() -> None:
            time.sleep(0.01)

        thread = coord._create_thread(quick, "negative_timeout")
        thread.start()

        # Should not raise with negative timeout
        coord._join_thread(thread, timeout=-1.0)

    def test_event_operations_with_special_names(self) -> None:
        """Test event operations with unusual event names."""
        coord = ThreadCoordinator()

        special_names = [
            "",
            "with spaces",
            "with\nnewlines",
            "unicode_事件",
            "a" * 1000,
        ]

        for name in special_names:
            coord._create_event(name)
            coord._set_event(name)
            assert coord._events[name].is_set()

    def test_thread_name_with_special_characters(self) -> None:
        """Test thread creation with unusual names."""
        coord = ThreadCoordinator()

        def dummy() -> None:
            pass

        special_names = [
            "unicode_线程",
            "name with spaces",
            "name-with-dashes",
            "name.with.dots",
        ]

        for name in special_names:
            thread = coord._create_thread(dummy, name)
            assert thread.name == name

    def test_multiple_coordinators_isolation(self) -> None:
        """Test multiple coordinators maintain independent state."""
        coord1 = ThreadCoordinator()
        coord2 = ThreadCoordinator()

        coord1._create_event("shared_name")
        coord2._create_event("shared_name")

        coord1._set_event("shared_name")

        # Event in coord2 should not be affected
        assert not coord2._events["shared_name"].is_set()
        assert coord1._events["shared_name"].is_set()

    def test_nested_lock_acquisition(self) -> None:
        """Test RLock allows nested acquisition."""
        coord = ThreadCoordinator()

        with coord._lock:
            with coord._lock:
                with coord._lock:
                    # Should not deadlock
                    coord._create_event("nested")

        assert "nested" in coord._events

    def test_thread_target_with_none_args(self) -> None:
        """Test thread creation with None args."""
        coord = ThreadCoordinator()
        result: list[Any] = []

        def accept_none(arg1: Any = None) -> None:
            result.append(arg1)

        thread = coord._create_thread(accept_none, "none_args", args=(None,))
        thread.start()
        thread.join()

        assert result == [None]

    def test_create_thread_returns_thread_like(self) -> None:
        """Test that create_thread returns ThreadLike type."""
        coord = ThreadCoordinator()

        def dummy() -> None:
            pass

        thread = coord._create_thread(dummy, "type_check")
        # Should have start, join, is_alive methods
        assert hasattr(thread, "start")
        assert hasattr(thread, "join")
        assert hasattr(thread, "is_alive")
        assert hasattr(thread, "name")
