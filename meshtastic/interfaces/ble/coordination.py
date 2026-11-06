"""Thread coordination utilities for BLE operations."""

import logging
from threading import Event, RLock, Thread, current_thread
from typing import Any, Callable

from meshtastic.interfaces.ble.constants import EVENT_THREAD_JOIN_TIMEOUT

logger = logging.getLogger("meshtastic.ble")

_INERT_THREAD_START_ERROR = (
    "Cannot start inert thread '{name}': coordinator has been cleaned up"
)
_EVENT_CREATE_AFTER_CLEANUP_ERROR = (
    "Cannot create event '{name}': coordinator has been cleaned up"
)
_LOCK_NOT_OWNED_ERROR = "Expected ThreadCoordinator._lock to be held"


class _InertThread:
    """A thread-like object that cannot be started.

    Returned by create_thread after cleanup to maintain API contract while
    preventing orphaned thread creation.
    """

    def __init__(self, name: str) -> None:
        """Initialize an inert thread placeholder that mimics a Thread but cannot be started.

        Parameters
        ----------
        name : str
            Human-readable thread name stored on the instance as the `name` attribute.
        """
        self.name = name
        self.daemon = True
        self.ident = None

    def start(self) -> None:
        """Prevent starting this inert thread returned after coordinator cleanup.

        Raises
        ------
        RuntimeError
            Indicates the inert thread cannot be started.
        """
        raise RuntimeError(_INERT_THREAD_START_ERROR.format(name=self.name))

    def is_alive(self) -> bool:
        """Return whether the inert thread is alive.

        Returns
        -------
        bool
            `False` — inert threads are never alive.
        """
        return False

    def join(self, timeout: float | None = None) -> None:
        """No-op join for an inert thread; joining has no effect.

        Parameters
        ----------
        timeout : float | None
            Ignored. Included for API compatibility. (Default value = None)
        """
        _ = timeout


# Type alias for objects that behave like Thread (Thread or _InertThread)
ThreadLike = Thread | _InertThread


class ThreadCoordinator:
    """Simplified thread management for BLE operations.

    This class provides centralized thread and event management for BLE interface
    operations. It ensures proper cleanup, prevents resource leaks, and provides
    a consistent API for thread coordination patterns.

    Features:
        - Thread lifecycle management (create, start, join, cleanup)
        - Event coordination for thread synchronization
        - Automatic resource cleanup on shutdown
        - Thread-safe operations with RLock
        - Helper methods for common coordination patterns
    """

    def __init__(self) -> None:
        """Create a ThreadCoordinator that centralizes thread and event management.

        Initializes internal coordinator state:
        - a reentrant lock protecting coordinator state
        - an empty list of tracked threads
        - an empty set of threads pending start
        - an empty mapping of event names to Event objects
        - a boolean flag indicating cleanup has not been performed
        """
        self._lock = RLock()
        self._threads: list[Thread] = []
        self._pending_start: set[Thread] = set()
        # Native launch can be interrupted before Thread.ident is published.
        # An indeterminate owner is retained rather than replaced/discarded.
        self._indeterminate_start: set[Thread] = set()
        # A separate handoff set distinguishes truly executing start() calls
        # from legacy/test helpers that reserve a pending thread directly.
        self._starting_now: set[Thread] = set()
        self._events: dict[str, Event] = {}
        self._cleaned_up: bool = False

    def _create_thread(
        self,
        target: Callable[..., Any],
        name: str,
        *,
        daemon: bool = True,
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
    ) -> ThreadLike:
        """Create and register a thread configured with the given target and arguments without starting it.

        If the coordinator has already been cleaned up, returns an inert thread that cannot be started and logs a
        warning. The returned thread is tracked for lifecycle management but is not started by this call.

        Parameters
        ----------
        target : Callable[..., Any]
            Callable to be executed by the thread.
        name : str
            Human-readable name for the thread.
        daemon : bool
            Whether the thread should be a daemon thread. (Default value = True)
        args : tuple[Any, ...]
            Positional arguments to pass to the target. (Default value = ())
        kwargs : dict[str, Any] | None
            Keyword arguments to pass to the target. (Default value = None)

        Returns
        -------
        ThreadLike
            The created and registered thread (not started). After cleanup, an `_InertThread` is returned whose `start()` raises `RuntimeError`.
        """
        with self._lock:
            # Prevent thread creation after cleanup to avoid orphaned threads
            if self._cleaned_up:
                logger.warning(
                    "Cannot create thread '%s': coordinator has been cleaned up", name
                )
                # Return an inert thread that cannot be started
                return _InertThread(name)
            # Prune dead threads to prevent unbounded growth in long-running processes.
            # Keep threads that are alive OR not yet started (ident is None).
            self._threads = [
                t for t in self._threads if t.is_alive() or t.ident is None
            ]
            thread = Thread(
                target=target, name=name, daemon=daemon, args=args, kwargs=kwargs
            )
            self._threads.append(thread)
            return thread

    def _create_event(self, name: str) -> Event:
        """Register or retrieve a named Event for coordinator synchronization.

        If an Event with the same name already exists, returns the existing instance without replacing it. This
        operation is thread-safe and acquires the coordinator's lock.

        Parameters
        ----------
        name : str
            Name to register the Event under.

        Returns
        -------
        Event
            The Event instance registered under `name`, or the existing instance if one was already registered.

        Notes
        -----
        After cleanup has started this returns a pre-set inert Event so callers
        do not block indefinitely on new waits.
        """
        with self._lock:
            if self._cleaned_up:
                warning_message = _EVENT_CREATE_AFTER_CLEANUP_ERROR.format(name=name)
                logger.warning(warning_message)
                inert_event = Event()
                inert_event.set()
                return inert_event
            if name in self._events:
                logger.debug(
                    "Event already exists: %s, returning existing instance", name
                )
                return self._events[name]
            event = Event()
            self._events[name] = event
            return event

    def _get_event(self, name: str) -> Event | None:
        """Retrieve the Event registered under the given name.

        Parameters
        ----------
        name : str
            Name of the event to retrieve.

        Returns
        -------
        Event | None
            Event if an event with the given name is registered, `None` otherwise.
        """
        with self._lock:
            return self._events.get(name)

    def _start_thread(self, thread: ThreadLike) -> None:
        """Start a tracked thread that has not yet been started.

        If the thread is not registered with the coordinator, logs a warning and does nothing. The thread is started
        outside the coordinator lock; if cleanup occurred while the thread was starting, the coordinator will join the
        newly started thread to ensure it does not remain detached from shutdown.

        Parameters
        ----------
        thread : ThreadLike
            Thread previously registered with this coordinator via _create_thread.
        """
        if isinstance(thread, _InertThread):
            message = _INERT_THREAD_START_ERROR.format(name=thread.name)
            logger.warning(message)
            return
        should_start = False
        must_join_after_start = False
        with self._lock:
            if self._cleaned_up:
                logger.debug(
                    "Refusing thread start after coordinator cleanup: %s", thread.name
                )
                return
            if thread not in self._threads:
                logger.warning(
                    "Cannot start untracked thread %r (ident=%s); coordinator may be cleaned up or a create_thread/cleanup race occurred",
                    thread,
                    thread.ident,
                )
                return
            # thread.ident is None only if the thread has never been started
            # This prevents RuntimeError from calling start() on a completed thread
            # Also check _pending_start to prevent TOCTOU race between checking ident and calling start()
            if (
                isinstance(thread, Thread)
                and thread.ident is None
                and thread not in self._pending_start
            ):
                self._pending_start.add(thread)
                self._starting_now.add(thread)
                should_start = True
        # Start outside the lock to prevent deadlocks if the new thread
        # immediately calls back into the coordinator
        if should_start:
            start_failed = False
            known_prelaunch_failure = False
            try:
                thread.start()
            except BaseException as exc:
                start_failed = True
                known_prelaunch_failure = isinstance(exc, (OSError, RuntimeError))
                raise
            finally:
                with self._lock:
                    if isinstance(thread, Thread):
                        self._pending_start.discard(thread)
                        self._starting_now.discard(thread)
                    if (
                        start_failed
                        and thread.ident is None
                        and known_prelaunch_failure
                    ):
                        # A rejected native start has no runnable owner and
                        # must not remain a permanent 'pending' registration.
                        # Guard remove(): a concurrent cleanup may already have
                        # retired the failed registration.
                        if thread in self._threads:
                            self._threads.remove(thread)
                    elif start_failed:
                        self._indeterminate_start.add(thread)
                    # cleanup() may have run after we released the lock but before
                    # thread.start(); if so, join this now-started thread below so
                    # we don't leave it detached from coordinator shutdown.
                    must_join_after_start = self._cleaned_up
            if (
                must_join_after_start
                and thread.is_alive()
                and thread is not current_thread()
            ):
                thread.join(timeout=EVENT_THREAD_JOIN_TIMEOUT)
            if must_join_after_start:
                with self._lock:
                    if not thread.is_alive() and thread in self._threads:
                        self._threads.remove(thread)

    def _join_thread(self, thread: ThreadLike, timeout: float | None = None) -> None:
        """Join a tracked thread if it is alive and not the current thread.

        Parameters
        ----------
        thread : ThreadLike
            Thread previously registered with this coordinator; no action is taken if the thread is not tracked, is not
            alive, or is the calling thread.
        timeout : float | None
            Maximum seconds to wait for the thread to finish; use None to wait indefinitely. (Default value = None)
        """
        with self._lock:
            should_join = (
                thread in self._threads
                and thread.is_alive()
                and thread is not current_thread()
            )
        if should_join:
            thread.join(timeout=timeout)

    def _join_all(self, timeout: float | None = None) -> None:
        """Join all tracked alive threads except the calling thread, applying the given timeout to each join.

        Parameters
        ----------
        timeout : float | None
            Per-thread join timeout in seconds. If `None`, wait indefinitely for each thread. (Default value = None)
        """
        with self._lock:
            current = current_thread()
            threads_to_join = [
                thread
                for thread in self._threads
                if thread.is_alive() and thread is not current
            ]
        for thread in threads_to_join:
            thread.join(timeout=timeout)

    def _assert_lock_owned(self) -> None:
        """Best-effort debug assertion that the coordinator lock is currently held.

        Note
        ----
        This uses CPython's private ``RLock._is_owned()`` probe when available.
        On runtimes without that attribute, this check becomes a no-op.
        """
        is_owned = getattr(self._lock, "_is_owned", None)
        if callable(is_owned) and not is_owned():
            raise RuntimeError(_LOCK_NOT_OWNED_ERROR)

    def _set_event_no_lock(self, name: str) -> None:
        """Set the named tracked event without acquiring the coordinator lock.

        Caller must hold self._lock.

        If no event with the given name is registered, this is a no-op.

        Parameters
        ----------
        name : str
            Name of the event to set.
        """
        if __debug__:
            self._assert_lock_owned()
        event = self._events.get(name)
        if event is not None:
            event.set()

    def _clear_event_no_lock(self, name: str) -> None:
        """Clear the tracked event named `name` without acquiring the coordinator lock.

        Caller must hold self._lock.

        If no event is registered under `name`, this is a no-op. This method clears the event so waiting threads will block until it is set again.

        Parameters
        ----------
        name : str
            The name of the event to clear.
        """
        if __debug__:
            self._assert_lock_owned()
        event = self._events.get(name)
        if event is not None:
            event.clear()

    def _set_event(self, name: str) -> None:
        """Set the coordinator's named event, waking any threads waiting on it.

        If no event is registered under the given name, this is a no-op.

        Parameters
        ----------
        name : str
            The registered event name to set.
        """
        with self._lock:
            self._set_event_no_lock(name)

    def _clear_event(self, name: str) -> None:
        """Clear the tracked event with the given name.

        If an event with `name` exists, clear its flag so waiting threads will block until it is set again; otherwise do nothing.

        Parameters
        ----------
        name : str
            Name of the tracked event to clear.
        """
        with self._lock:
            self._clear_event_no_lock(name)

    def _wait_for_event(self, name: str, timeout: float | None = None) -> bool:
        """Wait for the named tracked event to be set or until the timeout elapses.

        If no event is registered under `name`, returns `False` immediately.

        Parameters
        ----------
        name : str
            Name of the tracked event to wait for.
        timeout : float | None
            Maximum time in seconds to wait; `None` means wait indefinitely. (Default value = None)

        Returns
        -------
        bool
            `True` if the event was set before the timeout, `False` otherwise.
        """
        event = self._get_event(name)
        if event is not None:
            return event.wait(timeout=timeout)
        return False

    def _check_and_clear_event(self, name: str) -> bool:
        """Clear the named tracked event if it exists and is currently set.

        Parameters
        ----------
        name : str
            Name of the event to retrieve.

        Returns
        -------
        bool
            `True` if the named event existed and was set (and was cleared), `False` otherwise.
        """
        with self._lock:
            event = self._events.get(name)
            if event is not None and event.is_set():
                event.clear()
                return True
            return False

    def _wake_waiting_threads(self, *event_names: str) -> None:
        """Set the named coordinator events to wake any threads waiting on them.

        Parameters
        ----------
        event_names : str
            One or more event names to set; names not registered with the coordinator are ignored.
        """
        with self._lock:
            for name in event_names:
                self._set_event_no_lock(name)

    def _clear_events(self, *event_names: str) -> None:
        """Clear the specified named tracked events.

        Ignores names that are not registered.

        Parameters
        ----------
        event_names : str
            One or more event names to clear.
        """
        with self._lock:
            for name in event_names:
                self._clear_event_no_lock(name)

    def _cleanup(self) -> None:
        """Signal shutdown and join tracked threads with bounded retries.

        A timed-out thread remains owned by this coordinator until a later
        cleanup observes its exit. Threads in the create/start handoff also
        stay tracked, so a startup racing shutdown cannot become orphaned.
        """
        with self._lock:
            if not self._cleaned_up:
                self._cleaned_up = True
                for event in self._events.values():
                    event.set()
                # Existing waiters hold their Event references. Do not allow
                # new named-event registration after shutdown.
                self._events.clear()
            current = current_thread()
            threads_to_join = [
                thread
                for thread in self._threads
                if thread.is_alive() and thread is not current
            ]

        # Joining under the lock would prevent worker shutdown callbacks.
        for thread in threads_to_join:
            thread.join(timeout=EVENT_THREAD_JOIN_TIMEOUT)

        with self._lock:
            # Retain live workers and threads whose start() is still in flight
            # after the lock-free handoff in _start_thread(). A later cleanup
            # can join the former; the starter retires the latter on completion.
            # An interrupted native start can remain ident-less briefly.
            # Only retire its indeterminate marker after a verifiable exit.
            self._indeterminate_start = {
                thread
                for thread in self._indeterminate_start
                if thread.ident is None or thread.is_alive()
            }
            self._threads = [
                thread
                for thread in self._threads
                if thread.is_alive()
                or thread in self._starting_now
                or thread in self._indeterminate_start
            ]
            self._pending_start.intersection_update(self._starting_now)
