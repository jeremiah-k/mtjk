"""BLE notification management."""

import contextlib
import logging
import re
import struct
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock, RLock
from typing import TYPE_CHECKING, Any, cast

from bleak.exc import BleakDBusError, BleakError

from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.compat_adapter import (
    _iter_declared_callables,
    _resolve_declared_callable,
)
from meshtastic.interfaces.ble.constants import (
    BLECLIENT_ERROR_SUBSCRIPTION_TOKEN_EXHAUSTED,
    FROMNUM_UUID,
    LEGACY_LOGRADIO_UUID,
    LOGRADIO_UUID,
    MALFORMED_NOTIFICATION_THRESHOLD,
    NOTIFICATION_START_TIMEOUT,
    BLEConfig,
)
from meshtastic.interfaces.ble.errors import DecodeError
from meshtastic.interfaces.ble.notification_registration import (
    _NotificationRegistrationPlan,
    _OptionalNotificationStart,
)
from meshtastic.interfaces.ble.utils import (
    _is_unexpected_keyword_error,
    _sleep,
)
from meshtastic.protobuf import mesh_pb2

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.interface import BLEInterface

logger = logging.getLogger("meshtastic.ble")
UNSUBSCRIBE_FAILURE_WARNING_THRESHOLD = 3
RESUBSCRIBE_FAILURE_WARNING_THRESHOLD = 3
_NOTIFY_ACQUIRED_FRAGMENT = "notify acquired"
_SAFE_EXECUTE_POSITIONAL_SIGNATURE_MISMATCH_RE = re.compile(
    r"positional.*argument|takes .* positional|missing required positional|were given|was given",
    re.IGNORECASE,
)


_NotificationCallback = Callable[[Any, Any], None]


@dataclass(frozen=True, slots=True)
class _NotificationManagerSnapshot:
    """Immutable rollback snapshot for notification subscription bookkeeping."""

    active_subscriptions: dict[int, tuple[str, _NotificationCallback]]
    characteristic_to_callback: dict[str, _NotificationCallback]
    subscription_counter: int


@dataclass(frozen=True, slots=True)
class _NotificationSessionSnapshot:
    """Immutable rollback snapshot for one dispatcher registration session."""

    registered_session_epoch: int | None
    started_notify_characteristics: frozenset[str]
    fromnum_notify_enabled: bool
    malformed_notification_count: int
    current_legacy_log_handler: _NotificationCallback | None
    current_log_handler: _NotificationCallback | None
    current_from_num_handler: _NotificationCallback | None
    safe_legacy_handler: _NotificationCallback | None
    safe_log_handler: _NotificationCallback | None
    safe_from_num_handler: _NotificationCallback | None
    manager: _NotificationManagerSnapshot


class SubscriptionTokenExhaustedError(RuntimeError):
    """Raised when notification subscription token space is exhausted."""


class NotificationManager:
    """Manage BLE notification subscriptions so we can resubscribe cleanly after reconnects."""

    _MAX_SUBSCRIPTION_TOKEN = 0x7FFFFFFF

    def __init__(self) -> None:
        """Initialize a NotificationManager and its thread-safe subscription state.

        Initializes the following internal attributes used for tracking subscriptions:
        - _active_subscriptions: maps a unique subscription token to a (characteristic, callback) pair.
        - _characteristic_to_callback: maps a characteristic UUID to the most recently registered callback.
        - _subscription_counter: monotonic counter used to allocate unique subscription tokens.
        - _lock: re-entrant lock protecting access to subscription state.

        Returns
        -------
        None
        """
        self._active_subscriptions: dict[
            int, tuple[str, Callable[[Any, Any], None]]
        ] = {}
        self._characteristic_to_callback: dict[str, Callable[[Any, Any], None]] = {}
        self._resubscribe_failures: dict[str, int] = {}
        self._subscription_counter = 0
        self._resubscribe_epoch = 0
        self._lock = RLock()

    def _snapshot_registration_state(self) -> _NotificationManagerSnapshot:
        """Capture registration-owned manager state under the manager lock.

        Returns
        -------
        _NotificationManagerSnapshot
            Subscription mappings and token counter required for rollback.

        Notes
        -----
        ``_resubscribe_epoch`` and ``_resubscribe_failures`` intentionally remain
        outside this snapshot. They describe reconnect/resubscribe history rather
        than the registration transaction being rolled back.
        """
        with self._lock:
            return _NotificationManagerSnapshot(
                active_subscriptions=dict(self._active_subscriptions),
                characteristic_to_callback=dict(self._characteristic_to_callback),
                subscription_counter=self._subscription_counter,
            )

    def _restore_registration_state(
        self, snapshot: _NotificationManagerSnapshot
    ) -> None:
        """Restore registration-owned manager state under the manager lock.

        Parameters
        ----------
        snapshot : _NotificationManagerSnapshot
            Registration-owned subscription state captured before the transaction.

        Returns
        -------
        None

        Notes
        -----
        Resubscribe epoch/failure history is preserved deliberately because it is
        lifecycle history, not transaction-local registration state.
        """
        with self._lock:
            self._active_subscriptions = dict(snapshot.active_subscriptions)
            self._characteristic_to_callback = dict(snapshot.characteristic_to_callback)
            self._subscription_counter = snapshot.subscription_counter

    def _subscribe(
        self, characteristic: str, callback: Callable[[Any, Any], None]
    ) -> int:
        """Track a notification callback for a BLE characteristic so it can be cleaned up or re-registered later.

        Parameters
        ----------
        characteristic : str
            Identifier of the BLE characteristic (for example, a UUID string or handle).
        callback : Callable[[Any, Any], None]
            Function invoked when a notification arrives; typically called as (sender, data).

        Returns
        -------
        token : int
            Unique opaque token that identifies the tracked subscription.

        Notes
        -----
        Multiple subscriptions to the same characteristic are allowed; the most recently
        registered callback for a characteristic is returned by `_get_callback()`, while
        all tracked subscriptions are retained for cleanup or resubscription attempts.
        """
        with self._lock:
            token = self._subscription_counter
            # Wrap at 2 billion to prevent unbounded growth in long-running processes
            self._subscription_counter = (
                self._subscription_counter + 1
            ) & self._MAX_SUBSCRIPTION_TOKEN
            # Detect a full cycle through the token space instead of using a fixed
            # iteration cap, which can raise prematurely under wraparound collisions.
            start_token = token
            while token in self._active_subscriptions:
                token = self._subscription_counter
                self._subscription_counter = (
                    self._subscription_counter + 1
                ) & self._MAX_SUBSCRIPTION_TOKEN
                if token == start_token:
                    raise SubscriptionTokenExhaustedError(
                        BLECLIENT_ERROR_SUBSCRIPTION_TOKEN_EXHAUSTED
                    )
            self._active_subscriptions[token] = (characteristic, callback)
            self._characteristic_to_callback[characteristic] = callback
            return token

    def _cleanup_all(self) -> None:
        """Clear all tracked BLE notification subscriptions and per-characteristic callbacks.

        Removes every active subscription entry, clears the characteristic-to-callback mapping, and resets the
        subscription token counter to zero. This operation is performed while holding the manager's internal lock.

        Returns
        -------
        None
        """
        with self._lock:
            self._resubscribe_epoch += 1
            self._active_subscriptions.clear()
            self._characteristic_to_callback.clear()
            self._resubscribe_failures.clear()
            self._subscription_counter = 0

    def _unsubscribe_all(self, client: "BLEClient", *, timeout: float | None) -> None:
        """Stop notifications for every characteristic currently tracked and ignore any errors.

        Parameters
        ----------
        client : 'BLEClient'
            BLE client used to stop notifications.
        timeout : float | None
            Per-unsubscribe timeout passed to the client's `stop_notify` method; may be None.

        Returns
        -------
        None
        """
        with self._lock:
            characteristics = list(self._characteristic_to_callback.keys())

        failure_count = 0
        for characteristic in characteristics:
            try:
                client.stop_notify(characteristic, timeout=timeout)
            except Exception as e:  # noqa: BLE001
                failure_count += 1
                logger.debug(
                    "Failed to unsubscribe %s during shutdown: %s",
                    characteristic,
                    e,
                )
        if failure_count >= UNSUBSCRIBE_FAILURE_WARNING_THRESHOLD:
            logger.warning(
                "Failed to unsubscribe %d/%d BLE characteristic notifications during shutdown.",
                failure_count,
                len(characteristics),
            )

    def _resubscribe_all(self, client: "BLEClient", *, timeout: float | None) -> None:
        """Resubscribe all tracked BLE notification callbacks on the given client.

        Uses the per-characteristic latest callback to avoid duplicate resubscription attempts when
        multiple subscriptions were registered for the same characteristic.

        Parameters
        ----------
        client : 'BLEClient'
            BLE client on which to call `start_notify` for each characteristic.
        timeout : float | None
            Per-subscription timeout to pass to the client's `start_notify` method.

        Returns
        -------
        None
        """
        with self._lock:
            # Use _characteristic_to_callback to deduplicate - it holds only the latest
            # callback per characteristic, avoiding redundant resubscription attempts
            subscriptions = list(self._characteristic_to_callback.items())
            failures = self._resubscribe_failures
            epoch = self._resubscribe_epoch
        max_failure_threshold = RESUBSCRIBE_FAILURE_WARNING_THRESHOLD

        for characteristic, callback in subscriptions:
            with self._lock:
                if epoch != self._resubscribe_epoch:
                    return
            if characteristic == FROMNUM_UUID:
                # FROMNUM startup requires dispatcher-owned retry/fallback logic
                # and fromnum_notify_enabled synchronization.
                continue
            try:
                client.start_notify(
                    characteristic,
                    callback,
                    timeout=timeout,
                )
            except Exception as e:  # noqa: BLE001
                with self._lock:
                    if epoch != self._resubscribe_epoch:
                        return
                    failure_count = failures.get(characteristic, 0) + 1
                    failures[characteristic] = failure_count
                logger.debug(
                    "Failed to resubscribe %s during reconnect: %s",
                    characteristic,
                    e,
                )
                if failure_count >= max_failure_threshold:
                    logger.warning(
                        "Repeated BLE resubscribe failures for %s (%d attempts): %s",
                        characteristic,
                        failure_count,
                        e,
                    )
            else:
                stale_epoch = False
                with self._lock:
                    stale_epoch = epoch != self._resubscribe_epoch
                    if not stale_epoch:
                        failures.pop(characteristic, None)
                if stale_epoch:
                    with contextlib.suppress(
                        Exception
                    ):  # noqa: BLE001 - stale resubscribe rollback is best effort
                        client.stop_notify(characteristic, timeout=timeout)
                    return

    def __len__(self) -> int:
        """Report the number of active BLE notification subscriptions being tracked.

        Returns
        -------
        int
            Number of active subscriptions currently tracked.
        """
        with self._lock:
            return len(self._active_subscriptions)

    def _get_callback(self, characteristic: str) -> Callable[[Any, Any], None] | None:
        """Retrieve the most recently registered callback for a BLE characteristic.

        Parameters
        ----------
        characteristic : str
            BLE characteristic identifier (e.g., UUID or handle) to look up.

        Returns
        -------
        Callable[[Any, Any], None] | None
            The most recently registered callback for the characteristic, or `None` if no callback is registered.
        """
        with self._lock:
            return self._characteristic_to_callback.get(characteristic)


class BLENotificationDispatcher:
    """Own notification callback safety, FROMNUM parsing, and registration flow."""

    def __init__(
        self,
        *,
        notification_manager: NotificationManager,
        error_handler_provider: Callable[[], object],
        trigger_read_event: Callable[[], None],
        registration_current_provider: Callable[[BLEClient, int], bool],
    ) -> None:
        """Create a notification-dispatch collaborator.

        Parameters
        ----------
        notification_manager : NotificationManager
            Subscription manager used to deduplicate callback registrations.
        error_handler_provider : Callable[[], object]
            Function returning the current interface error-handler object.
        trigger_read_event : Callable[[], None]
            Callback that wakes the receive loop after FROMNUM handling.
        registration_current_provider : Callable[[BLEClient, int], bool]
            Interface-owned lifecycle predicate that validates whether notification
            registration still belongs to the current connection attempt.
        """
        self._notification_manager = notification_manager
        self._error_handler_provider = error_handler_provider
        self._trigger_read_event = trigger_read_event
        self._registration_current_provider = registration_current_provider
        self._registration_lock = RLock()
        self._fromnum_notify_enabled = False
        self._malformed_notification_count = 0
        self._malformed_notification_lock = RLock()
        self._notification_state_lock = Lock()
        self._malformed_notification_handling_started = False
        self._current_legacy_log_handler: Callable[[Any, Any], None] | None = None
        self._current_log_handler: Callable[[Any, Any], None] | None = None
        self._current_from_num_handler: Callable[[Any, Any], None] | None = None
        self._safe_legacy_handler: Callable[[Any, Any], None] | None = None
        self._safe_log_handler: Callable[[Any, Any], None] | None = None
        self._safe_from_num_handler: Callable[[Any, Any], None] | None = None
        self._registered_notification_session_epoch: int | None = None
        self._started_notify_characteristics: set[str] = set()

    def _snapshot_session_state(self) -> _NotificationSessionSnapshot:
        """Capture registration session state atomically for connect rollback.

        Returns
        -------
        _NotificationSessionSnapshot
            Dispatcher and manager state needed to restore the registration
            session after a failed connection transaction.

        Notes
        -----
        ``_registration_lock`` is held across the snapshot. While it is held,
        ``_notification_state_lock`` and ``_malformed_notification_lock`` are
        acquired and released in that order, then the notification-manager lock
        is acquired last by ``_snapshot_registration_state``.
        """
        with self._registration_lock:
            with self._notification_state_lock:
                fromnum_notify_enabled = self._fromnum_notify_enabled
            with self._malformed_notification_lock:
                malformed_notification_count = self._malformed_notification_count
            return _NotificationSessionSnapshot(
                registered_session_epoch=self._registered_notification_session_epoch,
                started_notify_characteristics=frozenset(
                    self._started_notify_characteristics
                ),
                fromnum_notify_enabled=fromnum_notify_enabled,
                malformed_notification_count=malformed_notification_count,
                current_legacy_log_handler=self._current_legacy_log_handler,
                current_log_handler=self._current_log_handler,
                current_from_num_handler=self._current_from_num_handler,
                safe_legacy_handler=self._safe_legacy_handler,
                safe_log_handler=self._safe_log_handler,
                safe_from_num_handler=self._safe_from_num_handler,
                manager=self._notification_manager._snapshot_registration_state(),
            )

    def _restore_session_state(self, snapshot: _NotificationSessionSnapshot) -> None:
        """Restore one previously captured registration session snapshot.

        Parameters
        ----------
        snapshot : _NotificationSessionSnapshot
            Dispatcher and manager registration state to restore.

        Returns
        -------
        None

        Notes
        -----
        ``_registration_lock`` is held across restoration. While it is held,
        ``_notification_state_lock`` and ``_malformed_notification_lock`` are
        acquired and released in that order, then the notification-manager lock
        is acquired last by ``_restore_registration_state``.
        """
        with self._registration_lock:
            self._registered_notification_session_epoch = (
                snapshot.registered_session_epoch
            )
            self._started_notify_characteristics = set(
                snapshot.started_notify_characteristics
            )
            with self._notification_state_lock:
                self._fromnum_notify_enabled = snapshot.fromnum_notify_enabled
            with self._malformed_notification_lock:
                self._malformed_notification_count = (
                    snapshot.malformed_notification_count
                )
            self._current_legacy_log_handler = snapshot.current_legacy_log_handler
            self._current_log_handler = snapshot.current_log_handler
            self._current_from_num_handler = snapshot.current_from_num_handler
            self._safe_legacy_handler = snapshot.safe_legacy_handler
            self._safe_log_handler = snapshot.safe_log_handler
            self._safe_from_num_handler = snapshot.safe_from_num_handler
            self._notification_manager._restore_registration_state(snapshot.manager)

    @property
    def fromnum_notify_enabled(self) -> bool:
        """Return whether FROMNUM notifications are currently active."""
        with self._notification_state_lock:
            return self._fromnum_notify_enabled

    @fromnum_notify_enabled.setter
    def fromnum_notify_enabled(self, enabled: bool) -> None:
        """Set FROMNUM notification-active flag."""
        with self._registration_lock:
            with self._notification_state_lock:
                self._fromnum_notify_enabled = enabled

    @property
    def malformed_notification_count(self) -> int:
        """Return current malformed FROMNUM notification counter."""
        with self._malformed_notification_lock:
            return self._malformed_notification_count

    @malformed_notification_count.setter
    def malformed_notification_count(self, value: int) -> None:
        """Set malformed FROMNUM notification counter."""
        with self._malformed_notification_lock:
            self._malformed_notification_count = value

    @property
    def malformed_notification_lock(self) -> RLock:
        """Expose malformed-notification lock for compatibility callers."""
        return self._malformed_notification_lock

    @malformed_notification_lock.setter
    def malformed_notification_lock(self, lock: RLock) -> None:
        """Set malformed-notification lock for compatibility callers."""
        with self._notification_state_lock:
            if self._malformed_notification_handling_started:
                raise RuntimeError(
                    "Cannot replace malformed_notification_lock after notification handling starts."
                )
            self._malformed_notification_lock = lock

    def handle_malformed_fromnum(self, reason: str, exc_info: bool = False) -> None:
        """Track malformed FROMNUM notifications and emit threshold warnings."""
        with self._notification_state_lock:
            self._malformed_notification_handling_started = True
        with self._malformed_notification_lock:
            self._malformed_notification_count += 1
            logger.debug("%s", reason, exc_info=exc_info)
            if self._malformed_notification_count >= MALFORMED_NOTIFICATION_THRESHOLD:
                logger.warning(
                    "Received %d malformed FROMNUM notifications. Check BLE connection stability.",
                    self._malformed_notification_count,
                )
                self._malformed_notification_count = 0

    def _resolve_error_handler(self) -> object | None:
        """Resolve current error handler, suppressing provider failures.

        Returns
        -------
        object | None
            Resolved error-handler object, or ``None`` when provider lookup
            fails or returns no handler.
        """
        try:
            error_handler = self._error_handler_provider()
        except Exception:  # noqa: BLE001 - notification paths must remain best effort
            logger.debug(
                "Error resolving notification error-handler provider.",
                exc_info=True,
            )
            return None
        return error_handler

    def report_notification_handler_error(self, error_msg: str) -> None:
        """Report notification-handler failures through configured hooks."""
        error_handler = self._resolve_error_handler()
        if error_handler is None:
            logger.debug(error_msg)
            return
        report_exception: Callable[[str], Any] | None = None
        safe_execute_hook = _resolve_declared_callable(
            error_handler, "safe_execute", "_safe_execute"
        )
        if safe_execute_hook is not None:
            safe_execute_callable = safe_execute_hook

            def _report_via_safe_execute(
                message: str,
                *,
                _safe_execute_callable: Callable[..., Any] = safe_execute_callable,
            ) -> None:
                def _raise_handler_error() -> None:
                    raise RuntimeError(message)

                BLENotificationDispatcher.invoke_safe_execute_compat(
                    _safe_execute_callable,
                    _raise_handler_error,
                    error_msg=message,
                    fallback=lambda: logger.debug(message),
                )

            report_exception = _report_via_safe_execute
        if report_exception is None:
            report_exception = _resolve_declared_callable(
                error_handler,
                "handle_unhandled_exception",
                "_handle_unhandled_exception",
            )
        if report_exception is not None:
            try:
                report_exception(error_msg)
            except Exception:  # noqa: BLE001 - callback error reporting is best effort
                logger.debug(error_msg, exc_info=True)
            return
        logger.debug(error_msg)

    @staticmethod
    # Signature probing deliberately exits immediately once handler execution is known.
    def invoke_safe_execute_compat(  # pylint: disable=too-many-return-statements
        safe_execute: Callable[..., Any],
        handler_thunk: Callable[[], None],
        *,
        error_msg: str,
        fallback: Callable[[], None],
        report_handler_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        """Invoke ``safe_execute`` with compatibility signature fallbacks."""
        executed = False
        handler_failed = False
        handler_exception: BaseException | None = None
        handler_failure_reported = False

        def _tracked_handler_thunk() -> None:
            nonlocal executed, handler_failed, handler_exception
            executed = True
            try:
                handler_thunk()
            except (
                Exception
            ) as exc:  # noqa: BLE001 - callback errors are reported below
                handler_failed = True
                handler_exception = exc
                raise

        def _report_handler_failure() -> None:
            nonlocal handler_failure_reported
            if (
                handler_failure_reported
                or not handler_failed
                or handler_exception is None
                or report_handler_error is None
            ):
                return
            handler_failure_reported = True
            try:
                report_handler_error(handler_exception)
            except Exception:  # noqa: BLE001 - error reporting remains best effort
                logger.debug(
                    "Failed to report notification handler error (%s).",
                    error_msg,
                    exc_info=True,
                )

        def _fallback_if_not_executed() -> None:
            if not executed:
                fallback()

        try:
            safe_execute(_tracked_handler_thunk, error_msg=error_msg)
            if executed:
                _report_handler_failure()
                return
            _fallback_if_not_executed()
            return
        except TypeError as exc:
            if not _is_unexpected_keyword_error(exc, "error_msg"):
                logger.debug(
                    "safe_execute keyword probe raised TypeError for notification handler (%s): %s",
                    error_msg,
                    exc,
                    exc_info=True,
                )
                if executed:
                    _report_handler_failure()
                    return
                _fallback_if_not_executed()
                return
            if executed:
                logger.debug(
                    "safe_execute keyword compatibility probe raised TypeError after handler execution (%s): %s",
                    error_msg,
                    exc,
                    exc_info=True,
                )
                _report_handler_failure()
        except (
            Exception
        ) as exc:  # noqa: BLE001 - notification callbacks must stay best effort
            logger.debug(
                "safe_execute keyword probe failed for notification handler (%s): %s",
                error_msg,
                exc,
                exc_info=True,
            )
            if executed:
                _report_handler_failure()
                return
            _fallback_if_not_executed()
            return

        if executed:
            _report_handler_failure()
            return

        try:
            safe_execute(_tracked_handler_thunk, error_msg)
            if executed:
                _report_handler_failure()
                return
            _fallback_if_not_executed()
            return
        except TypeError as exc:
            if _SAFE_EXECUTE_POSITIONAL_SIGNATURE_MISMATCH_RE.search(str(exc)):
                if executed:
                    logger.debug(
                        "safe_execute positional compatibility probe raised TypeError after handler execution (%s): %s",
                        error_msg,
                        exc,
                        exc_info=True,
                    )
                    _report_handler_failure()
            else:
                logger.debug(
                    "safe_execute positional probe raised TypeError for notification handler (%s); skipping callable-only "
                    "probe to avoid duplicate handler execution.",
                    error_msg,
                    exc_info=True,
                )
                if executed:
                    _report_handler_failure()
                    return
                _fallback_if_not_executed()
                return
        except (
            Exception
        ) as exc:  # noqa: BLE001 - notification callbacks must stay best effort
            logger.debug(
                "safe_execute positional probe failed for notification handler (%s): %s; skipping callable-only probe "
                "to avoid duplicate handler execution.",
                error_msg,
                exc,
                exc_info=True,
            )
            if executed:
                _report_handler_failure()
                return
            _fallback_if_not_executed()
            return

        if executed:
            _report_handler_failure()
            return

        try:
            safe_execute(_tracked_handler_thunk)
            if executed:
                _report_handler_failure()
                return
            _fallback_if_not_executed()
            return
        except (
            Exception
        ) as exc:  # noqa: BLE001 - notification callbacks must stay best effort
            logger.debug(
                "safe_execute callable-only probe failed for notification handler (%s): %s.",
                error_msg,
                exc,
                exc_info=True,
            )
            if executed:
                _report_handler_failure()
                return
            _fallback_if_not_executed()

    def from_num_handler(self, _: Any, b: bytes | bytearray) -> None:
        """Parse FROMNUM payload, reset malformed counter, and wake read loop."""
        with self._notification_state_lock:
            self._malformed_notification_handling_started = True
        try:
            if len(b) != 4:
                self.handle_malformed_fromnum(
                    f"FROMNUM notify has unexpected length {len(b)}; ignoring"
                )
                return
            from_num = struct.unpack("<I", b)[0]
            logger.debug("FROMNUM notify: %d", from_num)
            with self._malformed_notification_lock:
                self._malformed_notification_count = 0
        except (struct.error, ValueError):
            self.handle_malformed_fromnum(
                "Malformed FROMNUM notify; ignoring", exc_info=True
            )
            return
        finally:
            try:
                self._trigger_read_event()
            except Exception:  # noqa: BLE001 - wakeup path remains best effort
                logger.debug(
                    "Error waking receive loop from FROMNUM notification.",
                    exc_info=True,
                )

    def register_notifications(
        self,
        iface: "BLEInterface",
        client: BLEClient,
        *,
        legacy_log_handler: Callable[[Any, bytes | bytearray], None],
        log_handler: Callable[[Any, bytes | bytearray], None],
        from_num_handler: Callable[[Any, bytes], None],
    ) -> None:
        """Serialize one complete notification-registration transaction."""
        with self._registration_lock:
            self._register_notifications_locked(
                iface,
                client,
                legacy_log_handler=legacy_log_handler,
                log_handler=log_handler,
                from_num_handler=from_num_handler,
            )

    def _rollback_registration_state(
        self, *, stop_client: BLEClient | None = None
    ) -> None:
        """Rollback every backend notification started by the current session."""
        if stop_client is not None:
            for characteristic in tuple(self._started_notify_characteristics):
                with contextlib.suppress(
                    Exception
                ):  # noqa: BLE001 - rollback cleanup must remain best effort
                    stop_client.stop_notify(
                        characteristic,
                        timeout=NOTIFICATION_START_TIMEOUT,
                    )
        self._started_notify_characteristics.clear()
        self.fromnum_notify_enabled = False
        self.malformed_notification_count = 0

    def _rollback_fromnum_registration_state(self) -> None:
        """Rollback only FROMNUM registration state for polling fallback."""
        self._started_notify_characteristics.discard(FROMNUM_UUID)
        self.fromnum_notify_enabled = False
        self.malformed_notification_count = 0

    def _registration_still_current(
        self, client: BLEClient, session_epoch: int
    ) -> bool:
        """Return whether registration still belongs to the active session."""
        try:
            return self._registration_current_provider(client, session_epoch) is True
        except Exception:  # noqa: BLE001 - stale registration must fail closed
            logger.debug(
                "Error validating BLE notification registration ownership.",
                exc_info=True,
            )
            return False

    def _dispatch_notification_safely(
        self,
        iface: "BLEInterface",
        handler: Callable[[Any, Any], None],
        sender: Any,
        data: Any,
        error_msg: str,
    ) -> None:
        """Invoke one notification callback through compatibility-safe error handling."""

        def _report_notification_error() -> None:
            def _try_report(hook: object | None) -> bool:
                if not callable(hook):
                    return False
                try:
                    hook(error_msg)
                except Exception:  # noqa: BLE001 - reporting must stay best effort
                    logger.debug(
                        "Notification error reporter failed; trying fallback.",
                        exc_info=True,
                    )
                    return False
                return True

            for _member_name, reporter in _iter_declared_callables(
                iface,
                "report_notification_handler_error",
                "_report_notification_handler_error",
            ):
                if _try_report(reporter):
                    return
            self.report_notification_handler_error(error_msg)

        def _invoke_handler() -> None:
            handler(sender, data)

        def _fallback_invoke_handler() -> None:
            try:
                _invoke_handler()
            except Exception:  # noqa: BLE001 - callbacks must stay best effort
                _report_notification_error()

        error_handler = self._resolve_error_handler()
        safe_execute = _resolve_declared_callable(
            error_handler, "safe_execute", "_safe_execute"
        )
        if safe_execute is None:
            _fallback_invoke_handler()
            return
        self.invoke_safe_execute_compat(
            safe_execute,
            _invoke_handler,
            error_msg=error_msg,
            fallback=_fallback_invoke_handler,
            report_handler_error=lambda _error: _report_notification_error(),
        )

    def _get_or_create_registration_handler(
        self,
        uuid: str,
        *,
        cache_attr: str,
        handler: Callable[[Any, Any], None],
        track: bool = True,
    ) -> Callable[[Any, Any], None]:
        """Return the cached safe handler or cache the supplied candidate."""
        cached_handler = getattr(self, cache_attr, None)
        if not callable(cached_handler):
            cached_handler = handler
            setattr(self, cache_attr, cached_handler)
        if track:
            active_handler = self._notification_manager._get_callback(uuid)
            if active_handler is not cached_handler:
                self._notification_manager._subscribe(uuid, cached_handler)
        return cast(Callable[[Any, Any], None], cached_handler)

    @staticmethod
    def _optional_notification_errors(
        iface: "BLEInterface",
    ) -> tuple[type[BaseException], ...]:
        """Return exception classes treated as optional-notification failures."""
        return (
            BleakError,
            BleakDBusError,
            RuntimeError,
            BLEClient.BLEError,
            iface.BLEError,
        )

    def _prepare_registration_plan(
        self,
        iface: "BLEInterface",
        client: BLEClient,
        *,
        legacy_log_handler: Callable[[Any, bytes | bytearray], None],
        log_handler: Callable[[Any, bytes | bytearray], None],
        from_num_handler: Callable[[Any, bytes], None],
    ) -> _NotificationRegistrationPlan:
        """Prepare safe handlers and optional starts without starting backend notifications."""
        self._current_legacy_log_handler = legacy_log_handler
        self._current_log_handler = log_handler
        self._current_from_num_handler = from_num_handler
        session_epoch = int(getattr(iface, "_connection_session_epoch", 0))
        if self._registered_notification_session_epoch != session_epoch:
            self._rollback_registration_state(stop_client=client)
            self._registered_notification_session_epoch = session_epoch

        def _safe_legacy_handler(sender: Any, data: bytes | bytearray) -> None:
            handler = cast(
                Callable[[Any, Any], None],
                getattr(self, "_current_legacy_log_handler", legacy_log_handler),
            )
            self._dispatch_notification_safely(
                iface, handler, sender, data, "Error in legacy log notification handler"
            )

        def _safe_log_handler(sender: Any, data: bytes | bytearray) -> None:
            handler = cast(
                Callable[[Any, Any], None],
                getattr(self, "_current_log_handler", log_handler),
            )
            self._dispatch_notification_safely(
                iface, handler, sender, data, "Error in log notification handler"
            )

        def _safe_from_num_handler(sender: Any, data: bytes) -> None:
            handler = cast(
                Callable[[Any, Any], None],
                getattr(self, "_current_from_num_handler", from_num_handler),
            )
            self._dispatch_notification_safely(
                iface, handler, sender, data, "Error in FROMNUM notification handler"
            )

        optional_starts: list[_OptionalNotificationStart] = []
        optional_errors = self._optional_notification_errors(iface)
        optional_candidates = (
            (
                LEGACY_LOGRADIO_UUID,
                "_safe_legacy_handler",
                _safe_legacy_handler,
                "legacy log",
            ),
            (LOGRADIO_UUID, "_safe_log_handler", _safe_log_handler, "log"),
        )
        for uuid, cache_attr, candidate_handler, label in optional_candidates:
            try:
                supported = client.has_characteristic(uuid)
            except optional_errors as err:
                logger.debug(
                    "Failed to inspect optional %s notifications for %s: %s",
                    label,
                    uuid,
                    err,
                )
                continue
            if not supported or uuid in self._started_notify_characteristics:
                continue
            handler = self._get_or_create_registration_handler(
                uuid, cache_attr=cache_attr, handler=candidate_handler, track=False
            )
            optional_starts.append(
                _OptionalNotificationStart(
                    characteristic=uuid, handler=handler, label=label
                )
            )

        ingress_handler = self._get_or_create_registration_handler(
            FROMNUM_UUID,
            cache_attr="_safe_from_num_handler",
            handler=_safe_from_num_handler,
            track=False,
        )
        return _NotificationRegistrationPlan(
            session_epoch=session_epoch,
            optional_starts=tuple(optional_starts),
            fromnum_handler=ingress_handler,
        )

    def _execute_optional_registration(
        self,
        iface: "BLEInterface",
        client: BLEClient,
        plan: _NotificationRegistrationPlan,
    ) -> bool:
        """Start optional notifications and stop if session ownership becomes stale."""
        optional_errors = self._optional_notification_errors(iface)
        for start in plan.optional_starts:
            try:
                client.start_notify(
                    start.characteristic,
                    start.handler,
                    timeout=NOTIFICATION_START_TIMEOUT,
                )
            except optional_errors as err:
                logger.debug(
                    "Failed to start optional %s notifications for %s: %s",
                    start.label,
                    start.characteristic,
                    err,
                )
                continue
            self._started_notify_characteristics.add(start.characteristic)
            if not self._track_optional_handler(client, start):
                continue
            if not self._registration_still_current(client, plan.session_epoch):
                self._rollback_registration_state(stop_client=client)
                return False
        return True

    def _track_optional_handler(
        self, client: BLEClient, start: _OptionalNotificationStart
    ) -> bool:
        """Track one started optional callback or compensate its backend start.

        Parameters
        ----------
        client : BLEClient
            Client whose backend notification has already started.
        start : _OptionalNotificationStart
            Optional registration descriptor containing the characteristic and callback.

        Returns
        -------
        bool
            ``True`` when local callback tracking converges with backend state;
            ``False`` when tracking fails and compensating cleanup is attempted.

        Notes
        -----
        If compensating ``stop_notify`` also fails, the characteristic remains in
        ``_started_notify_characteristics`` so later lifecycle cleanup can retry it.
        """
        try:
            active_handler = self._notification_manager._get_callback(
                start.characteristic
            )
            if active_handler is not start.handler:
                self._notification_manager._subscribe(
                    start.characteristic, start.handler
                )
        except Exception as err:  # noqa: BLE001 - local/backend state must converge
            logger.warning(
                "Unable to track optional %s notification callback for %s: %s; "
                "stopping the backend notification.",
                start.label,
                start.characteristic,
                err,
            )
            try:
                client.stop_notify(
                    start.characteristic, timeout=NOTIFICATION_START_TIMEOUT
                )
            except Exception:  # noqa: BLE001 - later cleanup must retain retry state
                logger.debug(
                    "Failed to stop untracked optional notification %s; retaining "
                    "backend-started state for later cleanup.",
                    start.characteristic,
                    exc_info=True,
                )
            else:
                self._started_notify_characteristics.discard(start.characteristic)
            return False

        return True

    def _track_fromnum_handler(
        self, client: BLEClient, handler: Callable[[Any, Any], None]
    ) -> bool:
        """Track the FROMNUM callback or rollback backend notifications on failure."""
        try:
            current_handler = self._notification_manager._get_callback(FROMNUM_UUID)
            if current_handler is handler:
                return True
            self._notification_manager._subscribe(FROMNUM_UUID, handler)
        except Exception as err:  # noqa: BLE001 - local/backend state must converge
            logger.warning(
                "Unable to track FROMNUM notification callback for %s: %s; "
                "rolling back backend notifications and falling back to polling reads.",
                FROMNUM_UUID,
                err,
            )
            self._rollback_registration_state(stop_client=client)
            return False
        return True

    def _commit_fromnum_registration(
        self, client: BLEClient, plan: _NotificationRegistrationPlan
    ) -> bool:
        """Validate and commit active FROMNUM notification state."""
        if not self._track_fromnum_handler(client, plan.fromnum_handler):
            return False
        if not self._registration_still_current(client, plan.session_epoch):
            self._rollback_registration_state(stop_client=client)
            return False
        self.fromnum_notify_enabled = True
        self._registered_notification_session_epoch = plan.session_epoch
        return True

    def _execute_fromnum_registration(
        self,
        iface: "BLEInterface",
        client: BLEClient,
        plan: _NotificationRegistrationPlan,
    ) -> None:
        """Start or validate FROMNUM notifications, falling back to polling on failure."""
        if FROMNUM_UUID in self._started_notify_characteristics:
            self._commit_fromnum_registration(client, plan)
            return

        self.fromnum_notify_enabled = False
        optional_errors = self._optional_notification_errors(iface)
        max_attempts = BLEConfig.SERVICE_CHARACTERISTIC_RETRY_COUNT + 1
        for attempt in range(max_attempts):
            try:
                client.start_notify(
                    FROMNUM_UUID,
                    plan.fromnum_handler,
                    timeout=NOTIFICATION_START_TIMEOUT,
                )
            except BleakDBusError as err:
                if not self._is_notify_acquired_error(err):
                    logger.warning(
                        "Unable to start FROMNUM notifications for %s: %s; "
                        "falling back to polling reads.",
                        FROMNUM_UUID,
                        err,
                    )
                    self._rollback_fromnum_registration_state()
                    return
                logger.debug(
                    "FROMNUM notify already acquired for %s; retrying after "
                    "best-effort stop_notify (attempt %d/%d)",
                    FROMNUM_UUID,
                    attempt + 1,
                    max_attempts,
                )
                with contextlib.suppress(*optional_errors):
                    client.stop_notify(FROMNUM_UUID, timeout=NOTIFICATION_START_TIMEOUT)
                if attempt + 1 < max_attempts:
                    _sleep(BLEConfig.SERVICE_CHARACTERISTIC_RETRY_DELAY * (attempt + 1))
                    continue
                logger.warning(
                    "Unable to start FROMNUM notifications for %s after %d attempts "
                    "due to BlueZ 'Notify acquired'; falling back to polling reads.",
                    FROMNUM_UUID,
                    max_attempts,
                )
                self._rollback_fromnum_registration_state()
                return
            except optional_errors as err:
                logger.warning(
                    "Unable to start FROMNUM notifications for %s: %s; "
                    "falling back to polling reads.",
                    FROMNUM_UUID,
                    err,
                )
                self._rollback_fromnum_registration_state()
                return

            self._started_notify_characteristics.add(FROMNUM_UUID)
            if not self._commit_fromnum_registration(client, plan):
                return
            logger.debug(
                "FROMNUM notifications active for BLE session %d; receive loop "
                "using notification-driven reads.",
                plan.session_epoch,
            )
            return

    @staticmethod
    def _is_notify_acquired_error(err: BaseException) -> bool:
        """Return whether BlueZ reports that another notify owner is active."""
        return _NOTIFY_ACQUIRED_FRAGMENT in str(err).casefold()

    def _register_notifications_locked(
        self,
        iface: "BLEInterface",
        client: BLEClient,
        *,
        legacy_log_handler: Callable[[Any, bytes | bytearray], None],
        log_handler: Callable[[Any, bytes | bytearray], None],
        from_num_handler: Callable[[Any, bytes], None],
    ) -> None:
        """Prepare and execute one notification-registration transaction."""
        plan = self._prepare_registration_plan(
            iface,
            client,
            legacy_log_handler=legacy_log_handler,
            log_handler=log_handler,
            from_num_handler=from_num_handler,
        )
        if not self._execute_optional_registration(iface, client, plan):
            return
        self._execute_fromnum_registration(iface, client, plan)

    def log_radio_handler(self, _: Any, b: bytes | bytearray) -> str | None:
        """Decode protobuf log payload and return formatted message."""
        log_record = mesh_pb2.LogRecord()
        try:
            log_record.ParseFromString(bytes(b))
            if log_record.source:
                return f"[{log_record.source}] {log_record.message}"
            return log_record.message
        except DecodeError:
            logger.warning("Malformed LogRecord received. Skipping.")
            return None

    # COMPAT_STABLE_SHIM (2.7.7): historical underscore callback entrypoint.
    def _log_radio_handler(self, _: Any, b: bytes | bytearray) -> str | None:
        """Decode protobuf log payload and return formatted message."""
        return self.log_radio_handler(_, b)

    @staticmethod
    def legacy_log_radio_handler(_: Any, b: bytes | bytearray) -> str | None:
        """Decode legacy UTF-8 log payload and return normalized message."""
        try:
            return b.decode("utf-8").replace("\n", "")
        except UnicodeDecodeError:
            logger.warning(
                "Malformed legacy LogRecord received (not valid utf-8). Skipping."
            )
            return None

    # COMPAT_STABLE_SHIM (2.7.7): historical underscore callback entrypoint.
    def _legacy_log_radio_handler(
        self, sender: Any, b: bytes | bytearray
    ) -> str | None:
        """Decode legacy UTF-8 log payload and return normalized message."""
        return self.legacy_log_radio_handler(sender, b)
