"""Singleton asyncio runner for BLE operations.

This module provides a singleton `BLECoroutineRunner` that manages a single,
long-lived background thread running an asyncio event loop for all BLE
operations.

Notes
-----
- Reduces resource usage by avoiding per-client thread and loop creation.
- Prevents zombie-thread accumulation.
- Simplifies cleanup and shutdown.
- Provides consistent event-loop management across all BLE clients.
"""

import asyncio
import atexit
import contextlib
import logging
import threading
import warnings
import weakref
from concurrent.futures import Future
from typing import Any, Callable, Coroutine, TypeVar

from bleak.exc import BleakDBusError

from meshtastic.interfaces.ble.constants import (
    BLECLIENT_ERROR_LOOP_NOT_AVAILABLE,
    BLECLIENT_ERROR_LOOP_RESTART_FAILED,
    BLECLIENT_ERROR_LOOP_START_FAILED,
    BLECLIENT_ERROR_TIMEOUT_PARAM_CONFLICT,
    BLEConfig,
)

T = TypeVar("T")

logger = logging.getLogger("meshtastic.ble")

# Track zombie runners for diagnostics (shouldn't happen with singleton, but useful for monitoring)
_zombie_lock = threading.Lock()
_zombie_runner_count = 0


def get_zombie_runner_count() -> int:
    """Return the number of recorded zombie runner threads that failed to stop cleanly.

    This is an internal diagnostics helper in `meshtastic.interfaces.ble.runner`
    and is intentionally not part of the historical BLE compatibility alias
    surface exposed via `meshtastic.ble_interface` (no camelCase alias by
    policy; see AGENTS compatibility inventory).

    Returns
    -------
    int
        The count of zombie runner threads recorded for diagnostics.
    """
    with _zombie_lock:
        return _zombie_runner_count


class BLECoroutineRunner:
    """Singleton runner for executing coroutines in a dedicated event loop thread.

    This avoids creating a new thread and event loop for every BLEClient instance,
    reducing resource usage and avoiding potential deadlocks between different
    event loops.

    Notes
    -----
    - This class is thread-safe. The singleton pattern uses a lock to ensure
      only one instance exists, and all public methods are safe to call from
      any thread.
    - Lifecycle:
      - The runner is created on first access (lazy initialization).
      - The background thread starts on first coroutine submission.
      - The runner registers an atexit handler for graceful shutdown.
      - If the loop crashes, it is automatically restarted on next use.

    """

    # PEP 526 type hints for instance state used by static analyzers/IDEs;
    # concrete initialization happens in __new__/__init__.
    _instance: "BLECoroutineRunner | None" = None
    _singleton_lock = threading.Lock()
    _initialized: bool
    _internal_lock: threading.RLock
    _loop: asyncio.AbstractEventLoop | None
    _thread: threading.Thread | None
    _loop_ready: threading.Event
    _stop_requested: bool
    _warned_timeout_alias: bool
    _pending_futures: weakref.WeakSet[Future[Any]]
    _atexit_handler: Callable[[], None]
    _atexit_registered: bool

    def __new__(cls) -> "BLECoroutineRunner":
        """Return the singleton BLECoroutineRunner instance, creating it on first access.

        On first creation, the new instance's `_initialized` attribute is set to False.

        Parameters
        ----------
        cls : type
            The BLECoroutineRunner class being instantiated.

        Returns
        -------
        BLECoroutineRunner
            The singleton runner instance.
        """
        with cls._singleton_lock:
            if cls._instance is None:
                cls._instance = super(BLECoroutineRunner, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self) -> None:
        """Initialize per-instance state for the singleton BLECoroutineRunner.

        Sets up the per-instance reentrant lock, lifecycle flags and placeholders for the background
        thread and asyncio loop, initializes a WeakSet to track pending futures, and registers
        an atexit handler to ensure the runner is shut down when the process exits.

        Returns
        -------
        None
        """
        # The singleton lock is uncontended after first initialization, so always
        # acquiring it has negligible performance impact while ensuring correctness.
        with self._singleton_lock:
            # Fast path: already initialized - check inside lock for thread safety
            if getattr(self, "_initialized", False):
                return

            # Use RLock to allow re-entrant locking in instance methods.
            # Create lock inside singleton lock to ensure only one is ever created.
            if not hasattr(self, "_internal_lock"):
                self._internal_lock = threading.RLock()

            self._loop = None
            self._thread = None
            self._loop_ready = threading.Event()
            self._stop_requested = False
            self._initialized = True
            self._warned_timeout_alias = False

            # Track pending futures for cleanup
            self._pending_futures = weakref.WeakSet()

            self._atexit_handler = self._atexit_shutdown
            self._atexit_registered = False
            self._register_atexit_handler_locked()

    @property
    def _instance_lock(self) -> "threading.RLock":
        """Reentrant lock that protects the runner instance's internal state.

        Returns
        -------
        'threading.RLock'
            The instance's reentrant lock used to synchronize access to internal attributes.
        """
        # Lock is always initialized in __init__, but keep property for access
        return self._internal_lock

    def _register_atexit_handler_locked(self) -> None:
        """Register the runner's process-exit shutdown handler when not already registered.

        Must be called while holding `_instance_lock` or `_singleton_lock`.

        Returns
        -------
        None
        """
        if self._atexit_registered:
            return
        atexit.register(self._atexit_handler)
        self._atexit_registered = True

    def _unregister_atexit_handler_locked(self) -> None:
        """Attempt to unregister the runner's process-exit shutdown handler if it is currently registered.

        This is a best-effort operation that suppresses errors during unregistration. Must be called while holding
        `_instance_lock` or `_singleton_lock`.

        Returns
        -------
        None
        """
        if not self._atexit_registered:
            return
        with contextlib.suppress(Exception):
            atexit.unregister(self._atexit_handler)
        self._atexit_registered = False

    @property
    def _is_running(self) -> bool:
        """Return whether the runner's background thread and event loop are both active.

        This is an advisory/best-effort snapshot: `_thread` and `_loop` are read
        under `_instance_lock`, but `is_alive()` / `is_running()` are evaluated
        after releasing the lock and can become stale immediately.

        Returns
        -------
        bool
            True if the background thread exists and is alive and the event loop exists and is running, False otherwise.
        """
        with self._instance_lock:
            thread = self._thread
            loop = self._loop
        return (
            thread is not None
            and thread.is_alive()
            and loop is not None
            and loop.is_running()
        )

    def _ensure_running(self, timeout: float | None = None) -> None:
        """Ensure the runner's background asyncio event loop is started and ready.

        Block until the background loop signals readiness or the timeout elapses. If `timeout` is `None`,
        `BLEConfig.RUNNER_LOOP_READY_TIMEOUT_SECONDS` is used.

        Parameters
        ----------
        timeout : float | None
            Maximum seconds to wait for the loop to become ready; if `None`, the configured default is used.

        Raises
        ------
        RuntimeError
            If the event loop fails to become ready within the given timeout.
        """
        if timeout is None:
            timeout = BLEConfig.RUNNER_LOOP_READY_TIMEOUT_SECONDS
        with self._instance_lock:
            ready_event = self._start_locked()
        if ready_event is not None and not ready_event.wait(timeout=timeout):
            logger.error(
                "BLECoroutineRunner loop failed to start within %.1fs",
                timeout,
            )
            raise RuntimeError(BLECLIENT_ERROR_LOOP_START_FAILED)

    def _start_locked(self) -> threading.Event | None:
        """Ensure the background event loop thread is started and provide a readiness event when a new startup is initiated.

        Must be called while holding the instance lock (`_instance_lock`). If the runner is already running, this
        returns `None`. If a startup is already in progress on another thread, returns that startup's readiness
        `threading.Event`. When this call starts a new thread, it creates and starts a daemon thread and returns a fresh
        `threading.Event` that will be set when the loop becomes ready.

        Returns
        -------
        threading.Event | None
            An event that will be set when the runner's loop is ready, or `None` if the runner is already running.
        """
        # Check if already running
        if (
            self._thread is not None
            and self._thread.is_alive()
            and self._loop is not None
            and self._loop.is_running()
        ):
            return None
        # If a thread exists but loop is not yet published, another caller is already
        # starting the runner. Reuse its readiness event.
        if self._thread is not None and self._thread.is_alive():
            return self._loop_ready

        self._register_atexit_handler_locked()

        # Reset stop flag for restart
        self._stop_requested = False
        # Use a fresh ready-event per start cycle to avoid cross-thread interference
        # if a stale runner thread exits while a new one is starting.
        self._loop_ready = threading.Event()
        ready_event = self._loop_ready

        # Create and start the thread
        self._thread = threading.Thread(
            target=self._run_loop,
            args=(ready_event,),
            name="BLECoroutineRunner",
            daemon=True,
        )
        try:
            self._thread.start()
        except BaseException:
            # A failed thread start never published a functioning runner. Do
            # not leave an unstarted thread installed as the active owner.
            self._thread = None
            self._loop = None
            if not self._pending_futures:
                self._unregister_atexit_handler_locked()
            raise
        return ready_event

    def _run_loop(self, ready_event: threading.Event) -> None:
        """Run the background asyncio event loop for this runner and manage its lifecycle.

        Creates and runs a new event loop on this thread, sets `ready_event` when the loop is ready, keeps the loop
        responsive to cross-thread callbacks, and on shutdown cancels remaining tasks and closes the loop.

        Parameters
        ----------
        ready_event : threading.Event
            Event that will be set when the loop is ready and cleared after shutdown to signal the thread's lifecycle.

        Returns
        -------
        None
        """
        loop: asyncio.AbstractEventLoop | None = None
        keepalive_handle: asyncio.TimerHandle | None = None
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.set_exception_handler(self._handle_loop_exception)

            # Publish this loop only if this thread is still the active runner thread
            # and stop hasn't been requested.
            with self._instance_lock:
                if (
                    self._thread is not threading.current_thread()
                    or self._stop_requested
                ):
                    logger.debug(
                        "Discarding stale BLE runner thread during startup race or stop requested."
                    )
                    return
                self._loop = loop

            def _runner_keepalive_tick() -> None:
                """Keep the event loop responsive by scheduling a periodic no-op callback.

                This prevents the loop from sleeping indefinitely between I/O events so callbacks submitted from other
                threads are observed promptly on platforms where the loop's low-level wakeup signaling may not occur.
                `_runner_keepalive_tick` captures `keepalive_handle` and self-reschedules,
                but self-terminates when `_stop_requested` is set, when the active runner
                thread no longer matches, or when the loop closes, so no explicit
                cancellation path is required across rapid `_run_loop` restarts.

                Returns
                -------
                None
                """
                nonlocal keepalive_handle
                if self._stop_requested:
                    return
                if self._thread is not threading.current_thread():
                    return
                if loop.is_closed():
                    return
                keepalive_handle = loop.call_later(
                    BLEConfig.RUNNER_IDLE_WAKE_INTERVAL_SECONDS,
                    _runner_keepalive_tick,
                )

            # Signal that the loop is ready
            loop.call_soon(ready_event.set)
            # Periodic keepalive to avoid starvation of cross-thread callbacks.
            keepalive_handle = loop.call_later(
                BLEConfig.RUNNER_IDLE_WAKE_INTERVAL_SECONDS,
                _runner_keepalive_tick,
            )

            # Run forever until stopped
            loop.run_forever()

        except (
            Exception
        ) as e:  # noqa: BLE001 - runner loop must not crash caller threads
            logger.error("Error in BLECoroutineRunner loop: %s", e, exc_info=True)
        finally:
            # Clean shutdown: cancel all pending tasks
            if loop:
                if keepalive_handle is not None and not keepalive_handle.cancelled():
                    with contextlib.suppress(Exception):
                        keepalive_handle.cancel()
                self._cancel_all_tasks(loop)
                loop.close()

            ready_event.clear()
            with self._instance_lock:
                if self._thread is threading.current_thread():
                    if self._loop is loop:
                        self._loop = None
                    self._thread = None
                    # Unregister atexit handler if the loop reference was cleared.
                    if self._loop is None:
                        self._unregister_atexit_handler_locked()

    def _cancel_all_tasks(self, loop: asyncio.AbstractEventLoop) -> None:
        """Cancel all non-completed tasks on the given event loop and wait for their cancellation to finish.

        Exceptions raised while cancelling or awaiting tasks are suppressed and logged at debug level.

        Parameters
        ----------
        loop : asyncio.AbstractEventLoop
            Event loop whose pending tasks should be cancelled.

        Returns
        -------
        None
        """
        try:
            tasks = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in tasks:
                task.cancel()
            if tasks:
                # Use wait_for with timeout to prevent hanging indefinitely
                # on tasks that don't respond to cancellation
                async def _cancel_with_timeout() -> None:
                    """Wait for tracked pending tasks to finish cancellation up to the configured shutdown timeout.

                    Gathers all pending tasks and awaits their completion (exceptions are collected) for up to
                    BLEConfig.RUNNER_SHUTDOWN_TIMEOUT_SECONDS. If the wait times out, a debug-level message is logged.
                    """
                    try:
                        await asyncio.wait_for(
                            asyncio.gather(*tasks, return_exceptions=True),
                            timeout=BLEConfig.RUNNER_SHUTDOWN_TIMEOUT_SECONDS,
                        )
                    except (asyncio.TimeoutError, TimeoutError):
                        logger.debug("Timeout waiting for tasks to cancel")

                loop.run_until_complete(_cancel_with_timeout())
        except Exception as e:  # noqa: BLE001 - task cancellation is best-effort
            logger.debug("Exception during task cancellation: %s", e)

    @staticmethod
    def _close_coroutine_safely(coro: Coroutine[Any, Any, Any]) -> None:
        """Best-effort close to suppress never-awaited coroutine warnings.

        Parameters
        ----------
        coro : Coroutine[Any, Any, Any]
            Coroutine object to close.
        """
        with contextlib.suppress(Exception):
            coro.close()

    def _run_coroutine_threadsafe(
        self,
        coro: Coroutine[Any, Any, T],
        timeout: float | None = None,
        *,
        startup_timeout: float | None = None,
    ) -> Future[T]:
        """Submit a coroutine to the shared BLE runner event loop and return a Future for its result.

        Parameters
        ----------
        coro : Coroutine[Any, Any, T]
            Coroutine to execute on the runner loop.
        timeout : float | None
            Deprecated alias for `startup_timeout`; if provided a DeprecationWarning is emitted. (Default value = None)
        startup_timeout : float | None
            Maximum seconds to wait for the runner loop to become ready before submission. If omitted, the runner's default startup timeout is used.

        Returns
        -------
        Future[T]
            Future that will resolve to the coroutine's result.

        Raises
        ------
        ValueError
            If both `timeout` and `startup_timeout` are provided.
        RuntimeError
            If the runner loop cannot be started or is not available.
        """
        if timeout is not None and startup_timeout is not None:
            raise ValueError(BLECLIENT_ERROR_TIMEOUT_PARAM_CONFLICT)
        if timeout is not None and startup_timeout is None:
            # COMPAT_DEPRECATE: `timeout` remains as a deprecated alias for
            # `startup_timeout` during the migration window.
            with self._instance_lock:
                should_warn_timeout_alias = not self._warned_timeout_alias
                if should_warn_timeout_alias:
                    self._warned_timeout_alias = True
            if should_warn_timeout_alias:
                warnings.warn(
                    "run_coroutine_threadsafe(timeout=...) is deprecated; "
                    "use startup_timeout=<seconds> instead.",
                    DeprecationWarning,
                    stacklevel=2,
                )

        effective_startup_timeout = (
            startup_timeout if startup_timeout is not None else timeout
        )
        if effective_startup_timeout is None:
            effective_startup_timeout = BLEConfig.RUNNER_LOOP_READY_TIMEOUT_SECONDS

        try:
            self._ensure_running(timeout=effective_startup_timeout)
        except Exception:  # noqa: BLE001 - ensure coroutine is closed before re-raise
            self._close_coroutine_safely(coro)
            raise

        # Submission and future registration are one atomic ownership handoff.
        # Otherwise stop() can cancel its snapshot between submission and
        # tracking, leaving a future queued on a loop that is shutting down.
        with self._instance_lock:
            loop = self._loop
            if self._stop_requested or loop is None or not loop.is_running():
                logger.debug("Runner unavailable; rejecting coroutine submission.")
                self._close_coroutine_safely(coro)
                raise RuntimeError(BLECLIENT_ERROR_LOOP_NOT_AVAILABLE)
            try:
                future = asyncio.run_coroutine_threadsafe(coro, loop)
            except Exception:  # noqa: BLE001 - close rejected coroutine
                self._close_coroutine_safely(coro)
                raise
            self._pending_futures.add(future)
        # Remove completed futures promptly instead of waiting for GC.
        future.add_done_callback(self._discard_tracked_future)
        return future

    def _discard_tracked_future(self, future: Future[Any]) -> None:
        """Remove a completed Future from the runner's tracked pending futures.

        Parameters
        ----------
        future : Future
            The completed future to remove from the runner's internal `_pending_futures` set.

        Returns
        -------
        None
        """
        with self._instance_lock:
            self._pending_futures.discard(future)

    def _handle_loop_exception(
        self, loop: asyncio.AbstractEventLoop, context: dict[str, Any]
    ) -> None:
        """Handle exceptions raised in the runner's asyncio event loop.

        Suppresses BleakDBusError exceptions (logged at debug) that commonly
        occur during disconnects, logs other exceptions with their context at
        error level, and delegates to the loop's default exception handler. If
        the default handler raises, that error is logged at debug level.

        Parameters
        ----------
        loop : asyncio.AbstractEventLoop
            The event loop where the exception occurred.
        context : dict[str, Any]
            The context mapping provided by asyncio containing exception and message information.

        Returns
        -------
        None
        """
        exception = context.get("exception")
        if exception and isinstance(exception, BleakDBusError):
            # Suppress DBus errors that happen as unretrieved task exceptions,
            # especially "Operation failed with ATT error: 0x0e" which happens on disconnect.
            logger.debug("Suppressing BleakDBusError in BLE event loop: %s", exception)
            return

        # Log the full context for other exceptions
        message = context.get("message", "Unknown error")
        if exception is not None and isinstance(exception, BaseException):
            exception_info: bool | tuple[type[BaseException], BaseException, Any] = (
                type(exception),
                exception,
                exception.__traceback__,
            )
        else:
            exception_info = False
        logger.error("BLE event loop error: %s", message, exc_info=exception_info)

        # Use default handler for additional processing
        try:
            loop.default_exception_handler(context)
        except (
            Exception
        ) as e:  # noqa: BLE001 - loop default handler failures are non-fatal
            logger.debug("Exception in default exception handler: %s", e)

    def _cancel_pending_futures(self) -> None:
        """Cancel all tracked futures that have not completed.

        Silently attempts to cancel each future the runner is tracking; futures that are already done are left unchanged
        and exceptions raised while cancelling individual futures are caught and suppressed.

        Returns
        -------
        None
        """
        with self._instance_lock:
            for future in list(self._pending_futures):
                if not future.done():
                    try:
                        future.cancel()
                    except Exception as e:  # noqa: BLE001 - cancellation is best-effort
                        logger.debug("Exception cancelling future: %s", e)

    def _stop(self, timeout: float = BLEConfig.RUNNER_SHUTDOWN_TIMEOUT_SECONDS) -> bool:
        """Stop the runner's background event loop thread and perform cleanup.

        Requests shutdown of the background asyncio loop, cancels any tracked pending futures, and waits up to `timeout`
        seconds for the runner thread to exit. If called from the runner thread, joining is skipped to avoid deadlock.
        If the thread fails to exit within `timeout`, it is recorded as a zombie for diagnostics. Final internal
        references and the atexit handler are cleared only if they still refer to the stopped thread/loop to avoid
        interfering with concurrent restarts.

        Parameters
        ----------
        timeout : float
            Maximum number of seconds to wait for the background thread to join.
            (Default value = BLEConfig.RUNNER_SHUTDOWN_TIMEOUT_SECONDS)

        Returns
        -------
        bool
            `True` if the background thread exited (or was not running), `False` if the thread did not exit within
            `timeout` and was recorded as a zombie.
        """
        # Capture state and schedule stop under lock, but join OUTSIDE the lock
        # to avoid deadlock if the runner thread needs _instance_lock
        with self._instance_lock:
            self._stop_requested = True
            self._cancel_pending_futures()

            # Capture thread and loop references for join/cleanup outside lock
            thread = self._thread
            loop = self._loop

        # Stop the loop outside the lock
        if loop and loop.is_running():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except (RuntimeError, AttributeError, TypeError):
                logger.debug(
                    "Unable to stop BLE event loop thread-safely; loop may already be closing."
                )

        # Avoid self-join deadlock if stop() is called from the runner thread.
        if thread is threading.current_thread():
            logger.debug("stop() called from runner thread; skipping join.")
        # Join OUTSIDE the lock to avoid deadlocking with runner-thread code
        # that might need _instance_lock (e.g., run_coroutine_threadsafe)
        elif thread and thread.is_alive():
            thread.join(timeout=timeout)

            # Re-acquire lock to check result and update state
            with self._instance_lock:
                if thread.is_alive():
                    # Internal diagnostics/tests intentionally expose this module counter.
                    global _zombie_runner_count  # pylint: disable=global-statement
                    with _zombie_lock:
                        _zombie_runner_count += 1
                        current_count = _zombie_runner_count

                    logger.warning(
                        "BLECoroutineRunner thread did not exit within %.1fs (zombie count: %d)",
                        timeout,
                        current_count,
                    )
                    if current_count >= BLEConfig.RUNNER_ZOMBIE_WARN_THRESHOLD:
                        logger.warning(
                            "Multiple zombie BLE runner threads detected (%d). "
                            "Consider restarting the process to recover resources.",
                            current_count,
                        )
                    return False

        # Final cleanup under lock - only clear if no concurrent _ensure_running replaced them
        with self._instance_lock:
            thread_matches = self._thread is thread
            thread_exited = thread is None or not thread.is_alive()
            # Only clear _thread if it still references the original thread we stopped
            # AND the thread has actually exited (not just because we're the current thread)
            if thread_matches and thread_exited:
                self._thread = None
            # Only clear _loop if it still references the original loop and
            # the associated thread has actually exited.
            if self._loop is loop and thread_exited:
                self._loop = None
            # Keep process-exit cleanup registered if a concurrent restart already
            # installed a new active runner.
            if self._thread is None and self._loop is None:
                self._unregister_atexit_handler_locked()
        return True

    def _atexit_shutdown(self) -> None:
        """Stop the runner during process exit, suppressing and logging any exceptions.

        If shutdown raises an exception, it is logged at debug level and not propagated.

        Returns
        -------
        None
        """
        try:
            # Keep a short timeout at process exit so interpreter shutdown is not delayed
            # by BLE thread teardown best-effort cleanup.
            self._stop(timeout=BLEConfig.RUNNER_ATEXIT_SHUTDOWN_TIMEOUT_SECONDS)
        except Exception as e:  # noqa: BLE001 - process-exit cleanup must not raise
            logger.debug("Exception during atexit shutdown: %s", e)

    def _restart(self) -> bool:
        """Restart the singleton runner if it is not currently running.

        Returns
        -------
        bool
            True if the runner was restarted and the event loop became ready, False if the runner was already running.

        Raises
        ------
        RuntimeError
            If the event loop fails to become ready within the configured timeout.
        """
        with self._instance_lock:
            # _is_running uses the reentrant instance lock.
            if self._is_running:
                return False
            previous_thread = self._thread

        # A loop may have stopped running while its thread is still cancelling
        # BLE tasks and closing native resources. Never replace that live owner
        # with another thread: wait outside the lock so cleanup can finish.
        if previous_thread is not None and previous_thread.is_alive():
            if previous_thread is threading.current_thread():
                raise RuntimeError(BLECLIENT_ERROR_LOOP_RESTART_FAILED)
            previous_thread.join(timeout=BLEConfig.RUNNER_SHUTDOWN_TIMEOUT_SECONDS)
            if previous_thread.is_alive():
                raise RuntimeError(BLECLIENT_ERROR_LOOP_RESTART_FAILED)

        with self._instance_lock:
            if self._is_running:
                return False
            # A different caller may already have installed a new owner. Let
            # _start_locked() join its readiness instead of discarding it.
            if self._thread is previous_thread and (
                previous_thread is None or not previous_thread.is_alive()
            ):
                self._thread = None
                self._loop = None
            ready_event = self._start_locked()
        if ready_event is not None and not ready_event.wait(
            timeout=BLEConfig.RUNNER_LOOP_READY_TIMEOUT_SECONDS
        ):
            logger.error(
                "BLECoroutineRunner restart timed out waiting for loop readiness after %.1fs",
                BLEConfig.RUNNER_LOOP_READY_TIMEOUT_SECONDS,
            )
            raise RuntimeError(BLECLIENT_ERROR_LOOP_RESTART_FAILED)
        return True
