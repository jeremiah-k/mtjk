"""BLE reconnection logic and scheduling."""

import logging
import math
from collections.abc import Callable
from threading import TIMEOUT_MAX, Event, RLock
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from bleak.exc import BleakDBusError, BleakDeviceNotFoundError, BleakError

from meshtastic.interfaces.ble.compat_adapter import _resolve_declared_callable
from meshtastic.interfaces.ble.constants import DBUS_ERROR_RECONNECT_DELAY, BLEConfig
from meshtastic.interfaces.ble.coordination import ThreadCoordinator, ThreadLike
from meshtastic.interfaces.ble.errors import BLEDBusTransportError
from meshtastic.interfaces.ble.failure_policy import (
    _BLEFailureDisposition,
    _log_ble_failure,
)
from meshtastic.interfaces.ble.gating import (
    _addr_key,
    _is_currently_connected_elsewhere,
)
from meshtastic.interfaces.ble.policies import ReconnectPolicy
from meshtastic.interfaces.ble.state import BLEStateManager
from meshtastic.interfaces.ble.utils import (
    _thread_start_probe,
)

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.interface import BLEInterface

logger = logging.getLogger("meshtastic.ble")


class ReconnectPolicyMissingMethodError(AttributeError):
    """Raised when a required reconnect-policy method is missing."""

    def __init__(self, method_name: str) -> None:
        """Initialize with the missing method name."""
        self.method_name = method_name
        super().__init__(f"ReconnectPolicy missing method '{method_name}'")


class ThreadCoordinatorMissingMethodError(AttributeError):
    """Raised when required thread-coordinator methods are unavailable."""

    def __init__(self, methods: str) -> None:
        """Initialize with the missing thread-coordinator method names."""
        super().__init__(f"Thread coordinator is missing {methods}")


class NextAttempt(NamedTuple):
    """Validated reconnect scheduling decision from policy.next_attempt()."""

    delay: float
    should_retry: bool


class ReconnectScheduler:
    """Manage lifecycle of the reconnect worker thread."""

    def __init__(
        self,
        state_manager: BLEStateManager,
        state_lock: RLock,
        thread_coordinator: ThreadCoordinator,
        interface: "BLEInterface",
    ) -> None:
        """Manage scheduling of a background reconnect worker for a BLE interface.

        Parameters
        ----------
        state_manager : BLEStateManager
            Observes BLE lifecycle state used to decide whether reconnects may
            be scheduled (e.g., whether the interface is closing or a
            connection is active).
        state_lock : RLock
            Re-entrant lock protecting shared BLE state and the scheduler's internal thread reference.
        thread_coordinator : ThreadCoordinator
            Factory/manager used to create and start the worker thread that performs reconnect attempts.
        interface : 'BLEInterface'
            The BLE interface whose connection the scheduler will attempt to restore.
        """
        self.state_manager = state_manager
        self.state_lock = state_lock
        self.thread_coordinator = thread_coordinator
        self.interface = interface
        self._reconnect_policy = ReconnectPolicy(
            initial_delay=BLEConfig.AUTO_RECONNECT_INITIAL_DELAY,
            max_delay=BLEConfig.AUTO_RECONNECT_MAX_DELAY,
            backoff=BLEConfig.AUTO_RECONNECT_BACKOFF,
            jitter_ratio=BLEConfig.AUTO_RECONNECT_JITTER_RATIO,
            max_retries=None,
        )
        self._reconnect_worker = ReconnectWorker(interface, self._reconnect_policy)
        self._reconnect_thread: ThreadLike | None = None

    def _resolve_thread_coordinator_hook(
        self,
        public_name: str,
        legacy_name: str,
    ) -> Callable[..., object]:
        """Resolve a coordinator hook with public-first, underscore fallback.

        Parameters
        ----------
        public_name : str
            Public hook name to try first on ``thread_coordinator``.
        legacy_name : str
            Underscored fallback hook name to try when public hook is missing.

        Returns
        -------
        Callable[..., object]
            Resolved coordinator hook callable.

        Raises
        ------
        ThreadCoordinatorMissingMethodError
            If neither hook is available and callable.
        """
        hook = _resolve_declared_callable(
            self.thread_coordinator, public_name, legacy_name
        )
        if hook is not None:
            return cast(Callable[..., object], hook)
        raise ThreadCoordinatorMissingMethodError(f"{public_name}/{legacy_name}")

    def _thread_create_thread(
        self,
        *,
        target: Callable[..., object],
        args: tuple[object, ...],
        kwargs: dict[str, object],
        name: str,
        daemon: bool,
    ) -> ThreadLike:
        """Create a thread via public coordinator API with underscore fallback.

        Parameters
        ----------
        target : Callable[..., object]
            Worker entrypoint for the created thread.
        args : tuple[object, ...]
            Positional arguments passed to ``target``.
        kwargs : dict[str, object]
            Keyword arguments passed to ``target``.
        name : str
            Thread name for diagnostics and debug output.
        daemon : bool
            Whether the thread should run as a daemon.

        Returns
        -------
        ThreadLike
            Created thread-like object.

        Raises
        ------
        ThreadCoordinatorMissingMethodError
            If neither create hook is available.
        Exception
            Any exception raised by the coordinator's create hook.
        """
        create_thread = self._resolve_thread_coordinator_hook(
            "create_thread", "_create_thread"
        )
        return cast(
            ThreadLike,
            create_thread(
                target=target,
                args=args,
                kwargs=kwargs,
                name=name,
                daemon=daemon,
            ),
        )

    def _thread_start_thread(self, thread: ThreadLike) -> None:
        """Start a thread via public coordinator API with underscore fallback.

        Parameters
        ----------
        thread : ThreadLike
            Thread-like object to start.

        Returns
        -------
        None

        Raises
        ------
        ThreadCoordinatorMissingMethodError
            If neither start hook is available.
        Exception
            Any exception raised by the coordinator's start hook.
        """
        start_thread = cast(
            Callable[[ThreadLike], None],
            self._resolve_thread_coordinator_hook("start_thread", "_start_thread"),
        )
        start_thread(thread)

    def _schedule_reconnect(self, auto_reconnect: bool, shutdown_event: Event) -> bool:
        """Schedule a background BLE reconnect worker when auto-reconnect is enabled and no worker is already running.

        Parameters
        ----------
        auto_reconnect : bool
            Whether automatic reconnection is enabled; scheduling is skipped when False.
        shutdown_event : Event
            Event the worker observes to stop retrying early during shutdown.

        Returns
        -------
        bool
            True if a new reconnect worker thread was created and started, False otherwise.
        """
        if not auto_reconnect:
            return False

        with self.state_lock:
            is_closing = self.interface._is_connection_closing
            can_initiate_connection = self.interface._can_initiate_connection
            if is_closing or not can_initiate_connection:
                logger.debug(
                    "Skipping auto-reconnect scheduling because interface is closing or connection already in progress."
                )
                return False
            if self._reconnect_thread is not None:
                logger.debug(
                    "Auto-reconnect already in progress; skipping new attempt."
                )
                return False

            # A replaced worker's late on_exit (or start-failure cleanup) must
            # not clear the reference to its successor. Bind completion to the
            # exact thread that owns this scheduling generation; the binding is
            # assigned before the worker can start (create does not start it).
            thread: ThreadLike | None = None

            def on_worker_exit() -> None:
                if thread is not None:
                    self._clear_thread_reference(thread)

            thread = self._thread_create_thread(
                target=self._reconnect_worker._attempt_reconnect_loop,
                args=(shutdown_event,),
                kwargs={"on_exit": on_worker_exit},
                name="BLEAutoReconnect",
                daemon=True,
            )
            # Set the thread reference before starting to prevent race conditions
            self._reconnect_thread = thread
        # Start outside the lock: the reference is already set, so concurrent
        # schedulers will see a non-None _reconnect_thread and exit early.
        try:
            self._thread_start_thread(thread)
            thread_ident, thread_is_alive = _thread_start_probe(thread)
            if thread_ident is None and not thread_is_alive:
                logger.debug(
                    "Auto-reconnect thread did not start; clearing stale thread reference."
                )
                self._clear_thread_reference(thread)
                return False
        except Exception:
            self._clear_thread_reference(thread)
            raise
        return True

    def _clear_thread_reference(self, owner: ThreadLike | None = None) -> None:
        """Clear the reconnect worker reference when its ownership still matches.

        With ``owner`` unset (legacy form) this clears unconditionally. With
        ``owner`` set, the reference is cleared only when it still points at
        that exact worker thread, so completion reported by a replaced
        generation cannot discard its successor.

        This operation acquires the scheduler's state_lock and sets the internal
        reconnect thread reference to None to record that no background
        reconnect worker is active.

        Parameters
        ----------
        owner : ThreadLike | None
            Worker thread whose completion is being reported; ``None`` keeps
            the legacy unconditional clear. (Default value = None)

        Returns
        -------
        None
        """
        with self.state_lock:
            if owner is None or self._reconnect_thread is owner:
                self._reconnect_thread = None


class ReconnectWorker:
    """Perform blocking reconnect attempts with policy-driven backoff."""

    def __init__(
        self, interface: "BLEInterface", reconnect_policy: ReconnectPolicy
    ) -> None:
        """Initialize the ReconnectWorker bound to a BLE interface and a reconnect policy.

        Parameters
        ----------
        interface : 'BLEInterface'
            Interface used to initiate connection attempts and to query or modify connection state.
        reconnect_policy : ReconnectPolicy
            Backoff and retry policy that controls reconnect timing and attempt state.
        """
        self.interface = interface
        self.reconnect_policy = reconnect_policy

    def _call_policy(self, method_name: str, *args: Any) -> Any:
        """Call one canonical reconnect-policy method.

        Parameters
        ----------
        method_name : str
            Name of the canonical internal method to call on the policy.
        *args
            Arguments to pass to the policy method.

        Returns
        -------
        Any
            The return value from the policy method.
        """
        candidate_method = _resolve_declared_callable(
            self.reconnect_policy, method_name
        )
        if candidate_method is not None:
            return candidate_method(*args)
        raise ReconnectPolicyMissingMethodError(method_name)

    def _should_abort_reconnect(
        self, auto_reconnect: bool | None = None, context: str = ""
    ) -> bool:
        """Return whether the reconnect process should be aborted based on interface state and live auto-reconnect setting.

        Parameters
        ----------
        auto_reconnect : bool | None
            Backward-compatible placeholder. Current behavior reads
            ``self.interface.auto_reconnect`` on each call so runtime flag
            changes are observed immediately. (Default value = None)
        context : str
            Optional context string included in debug messages. (Default value = '')

        Returns
        -------
        bool
            `True` if reconnection should be aborted, `False` otherwise.
        """
        _ = auto_reconnect  # retained for compatibility with existing call sites/tests
        if self.interface._is_connection_closing:
            logger.debug(
                "Auto-reconnect aborted%s: interface is closing.",
                f" ({context})" if context else "",
            )
            return True
        if not bool(getattr(self.interface, "auto_reconnect", False)):
            logger.debug(
                "Auto-reconnect aborted%s: auto-reconnect disabled.",
                f" ({context})" if context else "",
            )
            return True
        return False

    def _validate_next_attempt(self, value: Any) -> NextAttempt | None:
        """Validate reconnect-policy next_attempt() output.

        Parameters
        ----------
        value : Any
            Raw value returned by reconnect_policy.next_attempt().

        Returns
        -------
        NextAttempt | None
            (delay, should_retry) on valid input, otherwise None.
        """
        if not isinstance(value, tuple) or len(value) != 2:
            logger.error(
                "Reconnect policy next_attempt returned invalid value: %r",
                value,
            )
            return None
        sleep_delay, should_retry = value
        if (
            not isinstance(sleep_delay, (int, float))
            or isinstance(sleep_delay, bool)
            or not isinstance(should_retry, bool)
        ):
            logger.error(
                "Reconnect policy next_attempt returned invalid value: %r",
                value,
            )
            return None
        sleep_delay = float(sleep_delay)
        if not math.isfinite(sleep_delay):
            logger.error(
                "Reconnect policy next_attempt returned non-finite delay: %r",
                value,
            )
            return None
        if sleep_delay > TIMEOUT_MAX:
            logger.warning(
                "Reconnect policy next_attempt delay %r exceeded threading.TIMEOUT_MAX (%s); capping to maximum.",
                sleep_delay,
                TIMEOUT_MAX,
            )
            sleep_delay = TIMEOUT_MAX
        if sleep_delay < 0.0:
            logger.error(
                "Reconnect policy next_attempt returned negative delay: %r",
                value,
            )
            return None
        return NextAttempt(delay=sleep_delay, should_retry=should_retry)

    def _get_validated_next_attempt(self) -> NextAttempt | None:
        """Call and validate reconnect policy `next_attempt()` output."""
        try:
            next_attempt = self._call_policy("next_attempt")
        except ReconnectPolicyMissingMethodError as err:
            logger.exception(
                "Reconnect policy missing required method '%s'",
                err.method_name,
            )
            return None
        return self._validate_next_attempt(next_attempt)

    def _handle_connected_elsewhere_gate(
        self, interface: "BLEInterface", attempt_num: int, shutdown_event: Event
    ) -> bool | None:
        """Handle reconnect gate when the device address is currently connected elsewhere.

        Returns
        -------
        bool | None
            `True` when handled and caller should continue loop, `False` when
            reconnect loop should abort, `None` when gate does not apply.
        """
        device_addr = _addr_key(getattr(interface, "address", None))
        if not device_addr or not _is_currently_connected_elsewhere(
            device_addr, owner=interface
        ):
            return None

        logger.info(
            "Skipping reconnect attempt %d: address %s already connected elsewhere",
            attempt_num,
            device_addr,
        )
        validated_next_attempt = self._get_validated_next_attempt()
        if validated_next_attempt is None:
            return False
        if not validated_next_attempt.should_retry:
            logger.info("Auto-reconnect reached maximum retry limit.")
            return False
        # Keep a minimum delay for the connected-elsewhere gate while
        # still honoring policy-provided backoff.
        sleep_delay = max(
            validated_next_attempt.delay,
            BLEConfig.AUTO_RECONNECT_INITIAL_DELAY,
        )
        if shutdown_event.wait(timeout=sleep_delay):
            return False
        return True

    def _attempt_reconnect_loop(  # pylint: disable=R0911
        self,
        shutdown_event: Event,
        *,
        on_exit: Callable[[], None] | None = None,
    ) -> None:
        """Run the reconnect loop that attempts to restore the bound BLE interface using the configured backoff policy.

        Attempts reconnects until a connection succeeds, the reconnect policy
        stops further retries, the provided shutdown_event is set, or
        auto-reconnect is disabled. Between failed attempts the loop waits
        according to the policy; certain BLE/DBus errors may increase the
        delay. The optional on_exit callback is invoked unconditionally when the
        loop ends.

        Parameters
        ----------
        shutdown_event : Event
            Event that stops the loop when set.
        on_exit : Callable[[], None] | None
            Optional callback called once when the loop finishes (successful connect, abort, or exception). (Default value = None)
        """
        try:
            self._call_policy("reset")
            interface = self.interface
            while not shutdown_event.is_set():
                override_delay: float | None = None
                if self._should_abort_reconnect(context="loop start"):
                    return
                attempt_num = 0
                try:
                    attempt_count = self._call_policy("get_attempt_count")
                    if attempt_count is None:
                        logger.error(
                            "Reconnect policy get_attempt_count() returned None for address %r; aborting reconnect loop.",
                            interface.address,
                        )
                        return
                    if isinstance(attempt_count, bool) or not isinstance(
                        attempt_count, int
                    ):
                        logger.error(
                            "Reconnect policy get_attempt_count() returned non-int value: %r",
                            attempt_count,
                        )
                        return
                    attempt_num = attempt_count + 1
                    if interface._is_connection_connected:
                        return
                    gate_result = self._handle_connected_elsewhere_gate(
                        interface, attempt_num, shutdown_event
                    )
                    if gate_result is False:
                        return
                    if gate_result is True:
                        continue
                    logger.info(
                        "Attempting BLE auto-reconnect (attempt %d).",
                        attempt_num,
                    )
                    if not interface.address:
                        logger.warning(
                            "Cannot attempt auto-reconnect: interface address is not set."
                        )
                        return
                    interface.connect(interface.address)
                except ReconnectPolicyMissingMethodError as err:
                    logger.exception(
                        "Reconnect policy missing required method '%s'",
                        err.method_name,
                    )
                    return
                except BLEDBusTransportError:
                    if self._should_abort_reconnect(context="DBusError"):
                        return
                    logger.warning(
                        "DBus error during auto-reconnect attempt %d",
                        attempt_num,
                        exc_info=True,
                    )
                    override_delay = DBUS_ERROR_RECONNECT_DELAY
                except interface.BLEError as err:
                    if self._should_abort_reconnect(context="BLEError"):
                        return
                    logger.warning(
                        "Auto-reconnect attempt %d failed: %s",
                        attempt_num,
                        err,
                    )
                except BleakDBusError:
                    if self._should_abort_reconnect(context="DBusError"):
                        return
                    # Safety net for raw BleakDBusError paths that have not
                    # been normalized to BLEDBusTransportError yet.
                    logger.warning(
                        "DBus error during auto-reconnect attempt %d",
                        attempt_num,
                        exc_info=True,
                    )
                    # Use longer delay for DBus errors to allow system Bluetooth stack to recover
                    override_delay = DBUS_ERROR_RECONNECT_DELAY
                    # State transition to ERROR and DISCONNECTED is already handled by
                    # the connection orchestrator, so we don't need to do it here
                except BleakError as err:
                    if self._should_abort_reconnect(context="BleakError"):
                        return
                    logger.warning(
                        "Auto-reconnect attempt %d failed with BLE error: %s",
                        attempt_num,
                        err,
                    )
                    # Give the adapter a respite before retrying and avoid thrashing scans.
                    delay_hint = (
                        DBUS_ERROR_RECONNECT_DELAY
                        if isinstance(err, BleakDeviceNotFoundError)
                        else BLEConfig.AUTO_RECONNECT_INITIAL_DELAY
                    )
                    override_delay = delay_hint
                except Exception:
                    if self._should_abort_reconnect(context="unexpected error"):
                        return
                    _log_ble_failure(
                        _BLEFailureDisposition.RETRYABLE,
                        "Unexpected error during auto-reconnect attempt %d",
                        attempt_num,
                        level=logging.ERROR,
                    )
                else:
                    logger.info(
                        "BLE auto-reconnect succeeded after %d attempts.",
                        attempt_num,
                    )
                    return

                if self._should_abort_reconnect(context="pre-sleep"):
                    return
                validated_next_attempt = self._get_validated_next_attempt()
                if validated_next_attempt is None:
                    return
                sleep_delay = validated_next_attempt.delay
                should_retry = validated_next_attempt.should_retry
                if override_delay is not None:
                    sleep_delay = max(sleep_delay, override_delay)
                if not should_retry:
                    logger.info("Auto-reconnect reached maximum retry limit.")
                    return
                logger.debug(
                    "Waiting %.2f seconds before next reconnect attempt.",
                    sleep_delay,
                )
                # Allow prompt shutdown without waiting the full backoff.
                if shutdown_event.wait(timeout=sleep_delay):
                    logger.debug("Reconnect wait interrupted by shutdown signal.")
                    return
        except ReconnectPolicyMissingMethodError as err:
            policy_name = type(self.reconnect_policy).__name__
            logger.exception(
                "Reconnect policy missing required method '%s'; aborting reconnect loop (policy_name=%s, policy=%r)",
                err.method_name,
                policy_name,
                self.reconnect_policy,
            )
        except Exception:
            _log_ble_failure(
                _BLEFailureDisposition.TERMINAL,
                "Unexpected error during reconnect loop setup; aborting reconnect",
                level=logging.ERROR,
            )
        finally:
            if on_exit is not None:
                try:
                    on_exit()
                except Exception:
                    _log_ble_failure(
                        _BLEFailureDisposition.BEST_EFFORT,
                        "Reconnect loop exit callback failed",
                        level=logging.ERROR,
                    )
