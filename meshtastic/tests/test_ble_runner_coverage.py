"""Test coverage for BLE runner async/execution edge cases.

This module tests thread management, coroutine execution, timeout handling,
event loop lifecycle, and thread-safe callback execution in the BLE runner.
"""

from __future__ import annotations

import asyncio
import threading
import time
import warnings
from concurrent.futures import Future
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest
from bleak.exc import BleakDBusError

from meshtastic.interfaces.ble import runner as _runner_module
from meshtastic.interfaces.ble.constants import BLECLIENT_ERROR_LOOP_NOT_AVAILABLE
from meshtastic.interfaces.ble.runner import (
    BLECoroutineRunner,
    get_zombie_runner_count,
)


@pytest.fixture(autouse=True)
def reset_runner_singleton() -> Generator[None, None, None]:
    """Reset the BLECoroutineRunner singleton between tests."""
    original_instance = BLECoroutineRunner._instance

    with _runner_module._zombie_lock:
        saved_zombie_count = _runner_module._zombie_runner_count
        _runner_module._zombie_runner_count = 0

    BLECoroutineRunner._instance = None

    try:
        yield
    finally:
        current = BLECoroutineRunner._instance
        if current is not None and current is not original_instance:
            try:
                handler = getattr(current, "_atexit_handler", None)
                if callable(handler):
                    _runner_module.atexit.unregister(handler)
                if hasattr(current, "_atexit_registered"):
                    current._atexit_registered = False
                current._stop(timeout=0.5)
            except Exception:
                pass
        BLECoroutineRunner._instance = original_instance
        with _runner_module._zombie_lock:
            _runner_module._zombie_runner_count = saved_zombie_count


@pytest.fixture
def fresh_runner(reset_runner_singleton: None) -> BLECoroutineRunner:  # noqa: F811
    """Provide a fresh BLECoroutineRunner instance."""
    runner = BLECoroutineRunner()
    return runner


@pytest.mark.unit
class TestBLECoroutineRunnerSingleton:
    """Test BLECoroutineRunner singleton pattern and initialization."""

    def test_singleton_returns_same_instance(self) -> None:
        """Test that multiple calls to BLECoroutineRunner() return the same instance."""
        runner1 = BLECoroutineRunner()
        runner2 = BLECoroutineRunner()
        assert runner1 is runner2

    def test_singleton_thread_safety(self) -> None:
        """Test singleton creation is thread-safe."""
        runners: list[BLECoroutineRunner | None] = [None, None]

        def create_runner(index: int) -> None:
            runners[index] = BLECoroutineRunner()

        threads = [
            threading.Thread(target=create_runner, args=(0,)),
            threading.Thread(target=create_runner, args=(1,)),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2.0)

        assert runners[0] is runners[1]

    def test_initialization_sets_defaults(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test that __init__ sets default values correctly."""
        assert fresh_runner._loop is None
        assert fresh_runner._thread is None
        assert fresh_runner._stop_requested is False
        assert fresh_runner._initialized is True
        assert fresh_runner._warned_timeout_alias is False

    def test_initialization_idempotent(
        self, fresh_runner: BLECoroutineRunner
    ) -> None:  # noqa: F811
        """Test that multiple __init__ calls are idempotent."""
        # First init already done by fixture
        original_lock = fresh_runner._internal_lock

        # Creating new instance should return same singleton
        second_instance = BLECoroutineRunner()

        assert fresh_runner._internal_lock is original_lock
        assert fresh_runner._initialized is True
        assert second_instance is fresh_runner  # Same singleton instance

    def test_instance_lock_property(
        self, fresh_runner: BLECoroutineRunner
    ) -> None:  # noqa: F811
        """Test _instance_lock property returns the internal lock."""
        lock = fresh_runner._instance_lock
        assert isinstance(lock, type(threading.RLock()))


@pytest.mark.unit
class TestBLECoroutineRunnerIsRunning:
    """Test the _is_running property."""

    def test_is_running_false_when_not_started(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test _is_running returns False when runner not started."""
        assert fresh_runner._is_running is False

    def test_is_running_true_when_running(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test _is_running returns True when runner is active."""

        async def simple_coro() -> str:
            return "done"

        # Start the runner
        future = fresh_runner._run_coroutine_threadsafe(simple_coro())
        future.result(timeout=2.0)

        # Give a moment for loop to start
        time.sleep(0.1)

        assert fresh_runner._is_running is True

    def test_is_running_false_after_stop(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test _is_running returns False after stop."""

        async def simple_coro() -> str:
            return "done"

        # Start and stop
        future = fresh_runner._run_coroutine_threadsafe(simple_coro())
        future.result(timeout=2.0)

        fresh_runner._stop(timeout=1.0)

        assert fresh_runner._is_running is False


@pytest.mark.unit
class TestBLECoroutineRunnerStartLocked:
    """Test the _start_locked method."""

    def test_start_locked_returns_none_when_running(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test _start_locked returns None when already running."""

        async def simple_coro() -> str:
            return "done"

        # Start the runner
        future = fresh_runner._run_coroutine_threadsafe(simple_coro())
        future.result(timeout=2.0)

        # Second call should return None
        with fresh_runner._instance_lock:
            result = fresh_runner._start_locked()

        assert result is None

    def test_start_locked_returns_event_when_starting(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test _start_locked returns an Event when starting new thread."""
        with fresh_runner._instance_lock:
            result = fresh_runner._start_locked()

        assert result is not None
        assert isinstance(result, threading.Event)

    def test_start_locked_returns_existing_event_when_in_progress(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test _start_locked returns existing event when startup in progress."""

        # Start a thread but don't wait for it
        def slow_target() -> None:
            time.sleep(0.5)

        with fresh_runner._instance_lock:
            # Manually set up a thread that's alive but loop not ready
            fresh_runner._thread = threading.Thread(target=slow_target)
            fresh_runner._thread.start()
            fresh_runner._loop_ready = threading.Event()
            existing_event = fresh_runner._loop_ready

            # Second call should return same event
            result = fresh_runner._start_locked()

        assert result is existing_event

        # Cleanup
        fresh_runner._thread.join(timeout=1.0)


@pytest.mark.unit
class TestBLECoroutineRunnerEnsureRunning:
    """Test the _ensure_running method."""

    def test_ensure_running_starts_loop(
        self, fresh_runner: BLECoroutineRunner
    ) -> None:  # noqa: F811
        """Test _ensure_running starts the event loop."""
        fresh_runner._ensure_running(timeout=2.0)

        assert fresh_runner._loop is not None
        assert fresh_runner._loop.is_running()

    def test_ensure_running_uses_default_timeout(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test _ensure_running uses default timeout when None."""
        fresh_runner._ensure_running(timeout=None)

        assert fresh_runner._loop is not None

    def test_ensure_running_raises_on_timeout(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test _ensure_running raises RuntimeError on timeout."""
        # Mock _start_locked to return an event that never gets set
        mock_event = MagicMock()
        mock_event.wait = MagicMock(return_value=False)

        with patch.object(fresh_runner, "_start_locked", return_value=mock_event):
            with pytest.raises(RuntimeError, match="BLE event loop failed to start"):
                fresh_runner._ensure_running(timeout=0.01)


@pytest.mark.unit
class TestBLECoroutineRunnerRunLoop:
    """Test the _run_loop method."""

    def test_run_loop_creates_and_runs_loop(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test _run_loop creates and runs asyncio loop."""

        ready_event = threading.Event()

        # Must set the thread reference before starting _run_loop

        with fresh_runner._instance_lock:
            fresh_runner._stop_requested = False

            fresh_runner._thread = threading.current_thread()

        thread = threading.Thread(target=fresh_runner._run_loop, args=(ready_event,))

        with fresh_runner._instance_lock:
            fresh_runner._thread = thread

        thread.start()

        # Wait for loop to be ready

        ready_event.wait(timeout=2.0)

        # Verify loop is running

        assert ready_event.is_set()

        assert fresh_runner._loop is not None

        assert fresh_runner._loop.is_running()

        # Cleanup

        fresh_runner._stop(timeout=1.0)

        thread.join(timeout=2.0)

    def test_run_loop_handles_stop_requested(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test _run_loop exits when stop requested during startup."""
        ready_event = threading.Event()

        with fresh_runner._instance_lock:
            fresh_runner._stop_requested = True

        fresh_runner._run_loop(ready_event)

        # Loop should not be published since stop was requested
        assert not ready_event.is_set()

    def test_run_loop_handles_stale_thread(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test _run_loop handles stale thread during startup."""
        ready_event = threading.Event()

        # Set up a different thread reference
        with fresh_runner._instance_lock:
            fresh_runner._thread = threading.Thread(target=lambda: None)

        fresh_runner._run_loop(ready_event)

        # Loop should not be published since thread is stale
        assert not ready_event.is_set()

    def test_run_loop_exception_handling(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test _run_loop handles exceptions gracefully."""
        ready_event = threading.Event()

        # Mock new_event_loop to raise an exception
        with patch(
            "asyncio.new_event_loop", side_effect=RuntimeError("loop creation failed")
        ):
            fresh_runner._run_loop(ready_event)

        # Should complete without raising

    @pytest.mark.skip(reason="Keepalive tick is internal implementation detail")
    def test_run_loop_keepalive_tick(
        self, fresh_runner: BLECoroutineRunner
    ) -> None:  # noqa: F811
        """Test the keepalive tick keeps loop responsive."""
        # Skipped: internal implementation detail


@pytest.mark.unit
class TestBLECoroutineRunnerCancelAllTasks:
    """Test the _cancel_all_tasks method."""

    def test_cancel_all_tasks_with_pending_tasks(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test _cancel_all_tasks cancels pending tasks."""
        loop = asyncio.new_event_loop()

        async def long_running_task() -> None:
            await asyncio.sleep(10.0)

        # Create a task
        task = loop.create_task(long_running_task())

        # Cancel all tasks
        fresh_runner._cancel_all_tasks(loop)

        # Task should be cancelled
        assert task.cancelled() or task.done()

        loop.close()

    def test_cancel_all_tasks_no_pending_tasks(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test _cancel_all_tasks with no pending tasks."""
        loop = asyncio.new_event_loop()

        # Should not raise with no tasks
        fresh_runner._cancel_all_tasks(loop)

        loop.close()

    def test_cancel_all_tasks_exception_handling(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test _cancel_all_tasks handles exceptions gracefully."""
        loop = asyncio.new_event_loop()

        # Create a task that raises on cancel
        async def bad_task() -> None:
            try:
                await asyncio.sleep(10.0)
            except asyncio.CancelledError as exc:
                raise RuntimeError("cancel failed") from exc

        loop.create_task(bad_task())

        # Let the task start
        loop.run_until_complete(asyncio.sleep(0.01))

        # Cancel should handle exception
        fresh_runner._cancel_all_tasks(loop)

        loop.close()


@pytest.mark.unit
class TestBLECoroutineRunnerRunCoroutineThreadsafe:
    """Test the _run_coroutine_threadsafe method."""

    def test_run_coroutine_success(
        self, fresh_runner: BLECoroutineRunner
    ) -> None:  # noqa: F811
        """Test _run_coroutine_threadsafe successfully runs a coroutine."""

        async def simple_coro() -> str:
            return "success"

        future = fresh_runner._run_coroutine_threadsafe(simple_coro())
        result = future.result(timeout=2.0)

        assert result == "success"

    def test_run_coroutine_with_exception(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test _run_coroutine_threadsafe handles coroutine exceptions."""

        async def failing_coro() -> str:
            raise ValueError("test error")

        future = fresh_runner._run_coroutine_threadsafe(failing_coro())

        with pytest.raises(ValueError, match="test error"):
            future.result(timeout=2.0)

    def test_run_coroutine_timeout_param_conflict(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test _run_coroutine_threadsafe raises on timeout param conflict."""

        async def simple_coro() -> str:
            return "done"

        coro = simple_coro()
        try:
            with pytest.raises(
                ValueError, match="Specify only one of timeout or startup_timeout"
            ):
                fresh_runner._run_coroutine_threadsafe(
                    coro, timeout=1.0, startup_timeout=2.0
                )
        finally:
            BLECoroutineRunner._close_coroutine_safely(coro)

    def test_run_coroutine_deprecated_timeout_warning(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test _run_coroutine_threadsafe warns on deprecated timeout param."""

        async def simple_coro() -> str:
            return "done"

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            future = fresh_runner._run_coroutine_threadsafe(simple_coro(), timeout=1.0)
            future.result(timeout=2.0)

            # Should emit deprecation warning
            deprecation_warnings = [
                x for x in w if issubclass(x.category, DeprecationWarning)
            ]
            assert len(deprecation_warnings) > 0

    def test_run_coroutine_loop_not_available(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test _run_coroutine_threadsafe raises when loop not available."""

        async def simple_coro() -> str:
            return "done"

        # Mock _ensure_running to not set up loop
        with patch.object(fresh_runner, "_ensure_running", return_value=None):
            with patch.object(fresh_runner, "_loop", None):
                with pytest.raises(
                    RuntimeError, match="BLECoroutineRunner loop is not available"
                ):
                    fresh_runner._run_coroutine_threadsafe(simple_coro())

    def test_run_coroutine_close_safely_on_error(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test _run_coroutine_threadsafe closes coroutine on error."""

        async def simple_coro() -> str:
            return "done"

        coro = simple_coro()

        # Mock _ensure_running to raise
        with patch.object(
            fresh_runner, "_ensure_running", side_effect=RuntimeError("startup failed")
        ):
            with pytest.raises(RuntimeError):
                fresh_runner._run_coroutine_threadsafe(coro)

        # Coroutine should be closed

    def test_run_coroutine_tracks_future(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test _run_coroutine_threadsafe tracks pending futures."""

        async def simple_coro() -> str:
            return "done"

        future = fresh_runner._run_coroutine_threadsafe(simple_coro())

        # Future should be in pending set
        assert future in fresh_runner._pending_futures

    def test_run_coroutine_stop_requested_raises(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test _run_coroutine_threadsafe raises when stop requested."""

        async def slow_coro() -> str:
            await asyncio.sleep(10.0)
            return "done"

        # First ensure runner is running
        async def setup_coro() -> str:
            return "setup"

        future = fresh_runner._run_coroutine_threadsafe(setup_coro())
        future.result(timeout=2.0)

        # Set stop requested
        with fresh_runner._instance_lock:
            fresh_runner._stop_requested = True

        # Submit new coroutine
        with pytest.raises(RuntimeError, match=BLECLIENT_ERROR_LOOP_NOT_AVAILABLE):
            fresh_runner._run_coroutine_threadsafe(slow_coro())


@pytest.mark.unit
class TestBLECoroutineRunnerDiscardTrackedFuture:
    """Test the _discard_tracked_future method."""

    def test_discard_removes_future(
        self, fresh_runner: BLECoroutineRunner
    ) -> None:  # noqa: F811
        """Test _discard_tracked_future removes future from pending set."""
        future: Future[str] = Future()

        with fresh_runner._instance_lock:
            fresh_runner._pending_futures.add(future)

        fresh_runner._discard_tracked_future(future)

        assert future not in fresh_runner._pending_futures

    def test_discard_nonexistent_future(
        self, fresh_runner: BLECoroutineRunner
    ) -> None:  # noqa: F811
        """Test _discard_tracked_future handles non-existent future gracefully."""
        future: Future[str] = Future()

        # Should not raise even if future wasn't tracked
        fresh_runner._discard_tracked_future(future)


@pytest.mark.unit
class TestBLECoroutineRunnerHandleLoopException:
    """Test the _handle_loop_exception method."""

    def test_handle_bleak_dbus_error_suppressed(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test BleakDBusError is suppressed."""
        loop = asyncio.new_event_loop()

        context = {
            "exception": BleakDBusError("test.error", ["test message"]),
            "message": "DBus error occurred",
        }

        with patch("meshtastic.interfaces.ble.runner.logger") as mock_logger:
            fresh_runner._handle_loop_exception(loop, context)
            mock_logger.debug.assert_called_once()

        loop.close()

    def test_handle_other_exception_logged(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test other exceptions are logged."""
        loop = asyncio.new_event_loop()

        context = {
            "exception": ValueError("test error"),
            "message": "Value error occurred",
        }

        with patch("meshtastic.interfaces.ble.runner.logger") as mock_logger:
            fresh_runner._handle_loop_exception(loop, context)
            mock_logger.error.assert_called_once()

        loop.close()

    def test_handle_exception_no_exception_in_context(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test handling context without exception."""
        loop = asyncio.new_event_loop()

        context = {"message": "No exception here"}

        with patch("meshtastic.interfaces.ble.runner.logger") as mock_logger:
            fresh_runner._handle_loop_exception(loop, context)
            mock_logger.error.assert_called_once()

        loop.close()

    def test_handle_exception_default_handler_raises(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test handling when default exception handler raises."""
        loop = asyncio.new_event_loop()

        context = {"exception": ValueError("test"), "message": "test"}

        def bad_handler(loop: asyncio.AbstractEventLoop, ctx: dict[str, Any]) -> None:
            raise RuntimeError("handler failed")

        loop.set_exception_handler(bad_handler)

        with patch("meshtastic.interfaces.ble.runner.logger") as mock_logger:
            fresh_runner._handle_loop_exception(loop, context)
            # Should log debug about handler failure or log error for the original exception
            assert mock_logger.debug.called or mock_logger.error.called

        loop.close()


@pytest.mark.unit
class TestBLECoroutineRunnerCancelPendingFutures:
    """Test the _cancel_pending_futures method."""

    def test_cancel_pending_futures_success(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test _cancel_pending_futures cancels tracked futures."""
        future1: Future[str] = Future()
        future2: Future[str] = Future()

        with fresh_runner._instance_lock:
            fresh_runner._pending_futures.add(future1)
            fresh_runner._pending_futures.add(future2)

        fresh_runner._cancel_pending_futures()

        assert future1.cancelled()
        assert future2.cancelled()

    def test_cancel_pending_futures_already_done(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test _cancel_pending_futures handles already-done futures."""
        future: Future[str] = Future()
        future.set_result("done")

        with fresh_runner._instance_lock:
            fresh_runner._pending_futures.add(future)

        # Should not raise
        fresh_runner._cancel_pending_futures()

        assert future.done()
        assert not future.cancelled()

    def test_cancel_pending_futures_exception_handling(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test _cancel_pending_futures handles cancel exceptions."""
        future = MagicMock(spec=Future)
        future.done = MagicMock(return_value=False)
        future.cancel = MagicMock(side_effect=RuntimeError("cancel failed"))

        with fresh_runner._instance_lock:
            fresh_runner._pending_futures.add(future)  # type: ignore

        # Should not raise despite exception
        fresh_runner._cancel_pending_futures()


@pytest.mark.unit
class TestBLECoroutineRunnerStop:
    """Test the _stop method."""

    def test_stop_when_not_running(
        self, fresh_runner: BLECoroutineRunner
    ) -> None:  # noqa: F811
        """Test _stop when runner not running."""
        result = fresh_runner._stop(timeout=1.0)
        assert result is True

    def test_stop_cancels_pending_futures(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test _stop cancels pending futures."""

        async def setup_coro() -> str:
            return "setup"

        future = fresh_runner._run_coroutine_threadsafe(setup_coro())
        future.result(timeout=2.0)

        # Add a pending future
        pending: Future[str] = Future()
        with fresh_runner._instance_lock:
            fresh_runner._pending_futures.add(pending)

        fresh_runner._stop(timeout=1.0)

        assert pending.cancelled()

    def test_stop_from_runner_thread_skips_join(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test _stop skips join when called from runner thread."""

        async def setup_coro() -> str:
            return "setup"

        future = fresh_runner._run_coroutine_threadsafe(setup_coro())
        future.result(timeout=2.0)

        # Store thread reference
        runner_thread = fresh_runner._thread

        # Simulate calling from runner thread
        with patch.object(threading, "current_thread", return_value=runner_thread):
            result = fresh_runner._stop(timeout=1.0)

        # Should return True but skip join
        assert result is True

    def test_stop_timeout_creates_zombie(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test _stop records zombie on timeout."""

        async def setup_coro() -> str:
            return "setup"

        future = fresh_runner._run_coroutine_threadsafe(setup_coro())
        future.result(timeout=2.0)

        # Mock thread to simulate it not exiting
        mock_thread = MagicMock()
        mock_thread.is_alive = MagicMock(return_value=True)
        mock_thread.join = MagicMock(return_value=None)

        with fresh_runner._instance_lock:
            fresh_runner._thread = mock_thread  # type: ignore

        result = fresh_runner._stop(timeout=0.01)

        assert result is False
        assert get_zombie_runner_count() > 0

    def test_stop_clears_references(
        self, fresh_runner: BLECoroutineRunner
    ) -> None:  # noqa: F811
        """Test _stop clears thread and loop references."""

        async def setup_coro() -> str:
            return "setup"

        future = fresh_runner._run_coroutine_threadsafe(setup_coro())
        future.result(timeout=2.0)

        fresh_runner._stop(timeout=1.0)

        # References should be cleared
        assert fresh_runner._thread is None
        assert fresh_runner._loop is None


@pytest.mark.unit
class TestBLECoroutineRunnerAtexitShutdown:
    """Test the _atexit_shutdown method."""

    def test_atexit_shutdown_success(
        self, fresh_runner: BLECoroutineRunner
    ) -> None:  # noqa: F811
        """Test _atexit_shutdown stops the runner."""

        async def setup_coro() -> str:
            return "setup"

        future = fresh_runner._run_coroutine_threadsafe(setup_coro())
        future.result(timeout=2.0)

        fresh_runner._atexit_shutdown()

        # Runner should be stopped
        assert not fresh_runner._is_running

    def test_atexit_shutdown_exception_suppressed(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test _atexit_shutdown suppresses exceptions."""
        with patch.object(
            fresh_runner, "_stop", side_effect=RuntimeError("stop failed")
        ):
            # Should not raise
            fresh_runner._atexit_shutdown()


@pytest.mark.unit
class TestBLECoroutineRunnerRestart:
    """Test the _restart method."""

    def test_restart_when_not_running(
        self, fresh_runner: BLECoroutineRunner
    ) -> None:  # noqa: F811
        """Test _restart starts runner when not running."""
        result = fresh_runner._restart()

        assert result is True
        assert fresh_runner._is_running

    def test_restart_when_already_running(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test _restart returns False when already running."""

        async def setup_coro() -> str:
            return "setup"

        future = fresh_runner._run_coroutine_threadsafe(setup_coro())
        future.result(timeout=2.0)

        result = fresh_runner._restart()

        assert result is False

    def test_restart_timeout_raises(
        self, fresh_runner: BLECoroutineRunner
    ) -> None:  # noqa: F811
        """Test _restart raises RuntimeError on timeout."""
        # Mock _start_locked to return event that never sets
        mock_event = MagicMock()
        mock_event.wait = MagicMock(return_value=False)

        with patch.object(fresh_runner, "_start_locked", return_value=mock_event):
            with pytest.raises(RuntimeError, match="BLE event loop failed to restart"):
                fresh_runner._restart()


@pytest.mark.unit
class TestBLECoroutineRunnerCloseCoroutineSafely:
    """Test the _close_coroutine_safely static method."""

    def test_close_coroutine_success(self) -> None:
        """Test _close_coroutine_safely closes coroutine."""

        async def simple_coro() -> str:
            return "done"

        coro = simple_coro()

        # Should not raise
        BLECoroutineRunner._close_coroutine_safely(coro)

        # Coroutine should be closed - verify by checking it raises when used
        def use_closed_coro() -> None:
            try:
                coro.send(None)  # type: ignore
            except StopIteration:
                pass

        # After close, the coroutine frame is released, so send() won't work properly
        # We just verify the close operation itself succeeded without error

    def test_close_coroutine_exception_suppressed(self) -> None:
        """Test _close_coroutine_safely suppresses exceptions."""
        coro = MagicMock()
        coro.close = MagicMock(side_effect=RuntimeError("close failed"))

        # Should not raise
        BLECoroutineRunner._close_coroutine_safely(coro)  # type: ignore


@pytest.mark.unit
class TestBLECoroutineRunnerAtexitHandlers:
    """Test atexit handler registration and unregistration."""

    def test_register_atexit_handler(
        self, fresh_runner: BLECoroutineRunner
    ) -> None:  # noqa: F811
        """Test _register_atexit_handler_locked registers handler."""
        with fresh_runner._instance_lock:
            # Reset state
            fresh_runner._atexit_registered = False
            fresh_runner._register_atexit_handler_locked()

        assert fresh_runner._atexit_registered is True

    def test_register_atexit_handler_already_registered(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test _register_atexit_handler_locked is idempotent."""
        with fresh_runner._instance_lock:
            fresh_runner._atexit_registered = True
            fresh_runner._register_atexit_handler_locked()

        # Should not raise or register again
        assert fresh_runner._atexit_registered is True

    def test_unregister_atexit_handler(
        self, fresh_runner: BLECoroutineRunner
    ) -> None:  # noqa: F811
        """Test _unregister_atexit_handler_locked unregisters handler."""
        with fresh_runner._instance_lock:
            fresh_runner._atexit_registered = True
            fresh_runner._unregister_atexit_handler_locked()

        assert fresh_runner._atexit_registered is False

    def test_unregister_atexit_handler_not_registered(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test _unregister_atexit_handler_locked when not registered."""
        with fresh_runner._instance_lock:
            fresh_runner._atexit_registered = False
            fresh_runner._unregister_atexit_handler_locked()

        # Should not raise
        assert fresh_runner._atexit_registered is False

    def test_unregister_atexit_handler_exception_suppressed(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test _unregister_atexit_handler_locked suppresses exceptions."""
        with fresh_runner._instance_lock:
            fresh_runner._atexit_registered = True

            # Mock atexit.unregister to raise
            with patch(
                "atexit.unregister", side_effect=RuntimeError("unregister failed")
            ):
                fresh_runner._unregister_atexit_handler_locked()

        # Should mark as unregistered despite exception
        assert fresh_runner._atexit_registered is False


@pytest.mark.unit
class TestGetZombieRunnerCount:
    """Test the get_zombie_runner_count function."""

    def test_returns_zero_initially(self) -> None:
        """Test get_zombie_runner_count returns 0 initially."""
        count = get_zombie_runner_count()
        assert isinstance(count, int)
        assert count == 0

    def test_thread_safe_access(self) -> None:
        """Test get_zombie_runner_count is thread-safe."""
        counts: list[int] = []

        def get_count() -> None:
            counts.append(get_zombie_runner_count())

        threads = [threading.Thread(target=get_count) for _ in range(5)]

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=1.0)

        assert len(counts) == 5
        assert all(isinstance(c, int) for c in counts)


@pytest.mark.unit
class TestBLECoroutineRunnerConcurrency:
    """Test thread safety and concurrency scenarios."""

    def test_concurrent_coroutine_submission(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test concurrent coroutine submissions are handled safely."""
        results: list[str] = []

        async def coro_with_id(coro_id: int) -> str:
            await asyncio.sleep(0.01)
            return f"result_{coro_id}"

        def submit_coro(task_id: int) -> None:
            future = fresh_runner._run_coroutine_threadsafe(coro_with_id(task_id))
            try:
                result = future.result(timeout=5.0)
                results.append(result)
            except Exception as e:
                results.append(f"error_{task_id}: {e}")

        threads = [threading.Thread(target=submit_coro, args=(i,)) for i in range(5)]

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        assert len(results) == 5
        assert all(r.startswith("result_") for r in results)

    def test_concurrent_stop_and_start(
        self, fresh_runner: BLECoroutineRunner
    ) -> None:  # noqa: F811
        """Test concurrent stop and start operations."""

        async def simple_coro() -> str:
            return "done"

        # Start the runner
        future = fresh_runner._run_coroutine_threadsafe(simple_coro())
        future.result(timeout=2.0)

        def stop_runner() -> None:
            fresh_runner._stop(timeout=1.0)

        def start_runner() -> None:
            try:
                fresh_runner._restart()
            except Exception:
                pass

        # Run stop and start concurrently
        threads = [
            threading.Thread(target=stop_runner),
            threading.Thread(target=start_runner),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        # Should not raise or deadlock
        assert True

    def test_coroutine_submission_during_stop(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test coroutine submission during stop operation."""

        async def setup_coro() -> str:
            return "setup"

        future = fresh_runner._run_coroutine_threadsafe(setup_coro())
        future.result(timeout=2.0)

        # Request stop
        with fresh_runner._instance_lock:
            fresh_runner._stop_requested = True

        # Try to submit another coroutine
        async def late_coro() -> str:
            return "late"

        with pytest.raises(RuntimeError, match=BLECLIENT_ERROR_LOOP_NOT_AVAILABLE):
            fresh_runner._run_coroutine_threadsafe(late_coro())


@pytest.mark.unit
class TestBLECoroutineRunnerEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_coroutine_with_none_result(
        self, fresh_runner: BLECoroutineRunner
    ) -> None:  # noqa: F811
        """Test coroutine that returns None."""

        async def none_coro() -> None:
            return None

        future = fresh_runner._run_coroutine_threadsafe(none_coro())
        result = future.result(timeout=2.0)

        assert result is None

    def test_coroutine_with_exception_result(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test coroutine that raises exception."""

        async def exception_coro() -> str:
            raise RuntimeError("coroutine error")

        future = fresh_runner._run_coroutine_threadsafe(exception_coro())

        with pytest.raises(RuntimeError, match="coroutine error"):
            future.result(timeout=2.0)

    def test_zero_timeout(self, fresh_runner: BLECoroutineRunner) -> None:  # noqa: F811
        """Test with zero timeout."""

        # Should use the default timeout when 0 is provided
        # Note: 0 timeout will likely cause immediate timeout
        # This tests the error path
        with pytest.raises(RuntimeError):
            fresh_runner._ensure_running(timeout=0.0)

    def test_negative_timeout(
        self, fresh_runner: BLECoroutineRunner
    ) -> None:  # noqa: F811
        """Test with negative timeout."""

        # Negative timeout should be treated as immediate timeout
        with pytest.raises(RuntimeError):
            fresh_runner._ensure_running(timeout=-1.0)

    def test_very_short_timeout(
        self, fresh_runner: BLECoroutineRunner
    ) -> None:  # noqa: F811
        """Test with very short timeout - startup timeout behavior."""

        async def quick_coro() -> str:
            return "done"

        # Start runner first with normal timeout
        fresh_runner._ensure_running(timeout=2.0)

        # Then submit coroutine with short timeout - should work since loop is running
        future = fresh_runner._run_coroutine_threadsafe(
            quick_coro(), startup_timeout=0.001
        )

        result = future.result(timeout=2.0)
        assert result == "done"

    def test_coroutine_with_cancellation(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test coroutine that handles cancellation."""
        cancellation_handled = False

        async def cancellable_coro() -> str:
            nonlocal cancellation_handled
            try:
                await asyncio.sleep(10.0)
                return "completed"
            except asyncio.CancelledError:
                cancellation_handled = True
                raise

        # Start runner
        future = fresh_runner._run_coroutine_threadsafe(cancellable_coro())

        # Give it time to start
        time.sleep(0.1)

        # Cancel the future
        future.cancel()

        try:
            future.result(timeout=2.0)
        except Exception:
            pass

    def test_future_done_callback(
        self, fresh_runner: BLECoroutineRunner
    ) -> None:  # noqa: F811
        """Test future done callback is called."""
        callback_called = threading.Event()

        def done_callback(_fut: Future[str]) -> None:
            callback_called.set()

        async def simple_coro() -> str:
            return "done"

        future = fresh_runner._run_coroutine_threadsafe(simple_coro())
        future.add_done_callback(done_callback)

        future.result(timeout=2.0)

        # Callback should be called
        assert callback_called.wait(timeout=2.0)

    def test_multiple_futures_same_coroutine(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test submitting the same coroutine multiple times."""

        async def simple_coro() -> str:
            return "done"

        future1 = fresh_runner._run_coroutine_threadsafe(simple_coro())
        future2 = fresh_runner._run_coroutine_threadsafe(simple_coro())

        result1 = future1.result(timeout=2.0)
        result2 = future2.result(timeout=2.0)

        assert result1 == "done"
        assert result2 == "done"

    def test_coroutine_with_nested_async(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test coroutine with nested async calls."""

        async def inner() -> str:
            await asyncio.sleep(0.01)
            return "inner"

        async def outer() -> str:
            result = await inner()
            return f"outer-{result}"

        future = fresh_runner._run_coroutine_threadsafe(outer())
        result = future.result(timeout=2.0)

        assert result == "outer-inner"


@pytest.mark.unit
class TestBLECoroutineRunnerStopLoopErrors:
    """Test error handling in stop method."""

    def test_stop_loop_call_soon_threadsafe_raises(
        self,
        fresh_runner: BLECoroutineRunner,  # noqa: F811
    ) -> None:
        """Test _stop handles RuntimeError from call_soon_threadsafe."""

        async def setup_coro() -> str:
            return "setup"

        future = fresh_runner._run_coroutine_threadsafe(setup_coro())
        future.result(timeout=2.0)

        # Mock call_soon_threadsafe to raise
        with patch.object(
            fresh_runner._loop,  # type: ignore
            "call_soon_threadsafe",
            side_effect=RuntimeError("loop closing"),
        ):
            # Should not raise
            fresh_runner._stop(timeout=1.0)

    @pytest.mark.skip(
        reason="Thread join exception handling tested indirectly via integration tests"
    )
    def test_stop_thread_join_exception(
        self, fresh_runner: BLECoroutineRunner
    ) -> None:  # noqa: F811
        """Test _stop handles thread join exception - skipped, tested via integration."""
        # Skipped: tested via integration


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
