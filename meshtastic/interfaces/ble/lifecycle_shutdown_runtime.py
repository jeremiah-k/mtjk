"""Shutdown lifecycle coordinator runtime ownership for BLE."""

import atexit
import contextlib
import inspect
import math
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from meshtastic.interfaces.ble.compat_adapter import (
    _get_declared_callable,
    _get_declared_member,
)
from meshtastic.interfaces.ble.constants import (
    DISCONNECT_TIMEOUT_SECONDS,
    NOTIFICATION_START_TIMEOUT,
    READ_TRIGGER_EVENT,
    RECEIVE_THREAD_JOIN_TIMEOUT,
    RECONNECTED_EVENT,
    logger,
)
from meshtastic.interfaces.ble.failure_policy import (
    _BLEFailureDisposition,
    _log_ble_failure,
)
from meshtastic.interfaces.ble.gating import _addr_key
from meshtastic.interfaces.ble.lifecycle_primitives import (
    _LifecycleErrorAccess,
    _LifecycleStateAccess,
    _LifecycleThreadAccess,
)
from meshtastic.interfaces.ble.management_state import (
    _BLEManagementStatePort,
    _management_state_for,
)
from meshtastic.interfaces.ble.ports import _BLESessionStatePort
from meshtastic.interfaces.ble.session_state import _session_state_for
from meshtastic.interfaces.ble.state import ConnectionState
from meshtastic.interfaces.ble.utils import (
    _is_unexpected_keyword_error,
    _thread_start_probe,
)
from meshtastic.mesh_interface import MeshInterface

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.client import BLEClient
    from meshtastic.interfaces.ble.interface import BLEInterface


SHUTDOWN_ALREADY_CLOSED_MSG = (
    "BLEInterface.close called on already closed interface; ignoring"
)


class BLEShutdownLifecycleCoordinator:
    """Own interface shutdown orchestration and terminal cleanup behavior.

    Parameters
    ----------
    iface : BLEInterface
        Interface instance whose shutdown flow and terminal cleanup are managed
        by this collaborator.
    """

    def __init__(
        self,
        iface: "BLEInterface",
        *,
        session_state: _BLESessionStatePort | None = None,
        management_state: _BLEManagementStatePort | None = None,
    ) -> None:
        """Bind shutdown ownership to a specific interface.

        Parameters
        ----------
        iface : BLEInterface
            Interface instance providing state/thread/error collaborators.
        session_state : _BLESessionStatePort | None
            Optional shared lifecycle state. When ``None``, resolve the
            interface-owned state or use the legacy adapter.
        management_state : _BLEManagementStatePort | None, optional
            Shared management-operation state. When omitted, resolve the
            interface-owned state or use the legacy adapter.

        Returns
        -------
        None
            Initializes bound shutdown-collaborator state.
        """
        self._iface = iface
        self._session = _session_state_for(iface, session_state)
        self._management = _management_state_for(iface, management_state)
        self._state_access = _LifecycleStateAccess(iface)
        self._thread_access = _LifecycleThreadAccess(iface)
        self._error_access = _LifecycleErrorAccess(iface)
        self._bounded_cleanup_thread: threading.Thread | None = None
        self._bounded_mesh_close_thread: threading.Thread | None = None
        self._bounded_thread_lock = threading.Lock()

    def is_connection_closing(self) -> bool:
        """Return whether this interface is closing or already closed.

        Returns
        -------
        bool
            ``True`` when the interface is in closing/closed state; otherwise
            ``False``.
        """
        if self._state_access.is_closing():
            return True
        with self._session.lock:
            return self._session.closed

    def _cleanup_thread_coordinator(
        self,
        *,
        timeout: float | None = None,
    ) -> None:
        """Run thread-coordinator cleanup via public/legacy compatibility hooks.

        Parameters
        ----------
        timeout : float | None, optional
            Optional maximum seconds to wait for cleanup completion. When
            ``None``, cleanup runs inline without an additional wait budget.

        Returns
        -------
        None
            Cleanup is best effort and intentionally suppresses hook failures.
        """
        iface = self._iface
        coordinator = _get_declared_member(iface, "thread_coordinator")
        if coordinator is None:
            logger.debug("Thread coordinator is unavailable; skipping cleanup")
            return
        cleanup_hook = _get_declared_callable(coordinator, "cleanup")
        if cleanup_hook is not None:
            hook_name = "cleanup"
        else:
            legacy_cleanup = _get_declared_callable(coordinator, "_cleanup")
            if legacy_cleanup is not None:
                cleanup_hook = legacy_cleanup
                hook_name = "_cleanup"
            else:
                logger.debug("Thread coordinator is missing cleanup/_cleanup")
                return

        if timeout is None:
            try:
                cleanup_hook()
            except Exception:  # noqa: BLE001 - shutdown cleanup is best effort
                _log_ble_failure(
                    _BLEFailureDisposition.BEST_EFFORT,
                    "Error running thread coordinator %s()",
                    hook_name,
                )
            return

        if timeout <= 0:
            logger.warning(
                "Skipping thread coordinator %s(): close timeout budget exhausted.",
                hook_name,
            )
            return

        def _run_cleanup() -> None:
            try:
                cleanup_hook()
            except Exception:  # noqa: BLE001 - shutdown cleanup is best effort
                _log_ble_failure(
                    _BLEFailureDisposition.BEST_EFFORT,
                    "Error running thread coordinator %s()",
                    hook_name,
                )
            finally:
                with self._bounded_thread_lock:
                    if self._bounded_cleanup_thread is threading.current_thread():
                        self._bounded_cleanup_thread = None

        cleanup_thread = threading.Thread(
            target=_run_cleanup,
            name="BLEThreadCoordinatorCleanup",
            daemon=True,
        )
        try:
            with self._bounded_thread_lock:
                previous_thread = self._bounded_cleanup_thread
                if previous_thread is not None and previous_thread.is_alive():
                    logger.warning(
                        "Skipping thread coordinator %s(): previous bounded cleanup thread is still running.",
                        hook_name,
                    )
                    return
                self._bounded_cleanup_thread = cleanup_thread
                cleanup_thread.start()
        except Exception:  # noqa: BLE001 - shutdown cleanup is best effort
            with self._bounded_thread_lock:
                if self._bounded_cleanup_thread is cleanup_thread:
                    self._bounded_cleanup_thread = None
            logger.warning(
                "Failed to start thread coordinator cleanup thread; skipping bounded cleanup stage to preserve timeout contract.",
                exc_info=True,
            )
            return

        cleanup_thread.join(timeout=timeout)
        if cleanup_thread.is_alive():
            logger.warning(
                "Timed out waiting %.3fs for thread coordinator %s() cleanup.",
                timeout,
                hook_name,
            )

    def _await_management_shutdown(
        self,
        *,
        management_shutdown_wait_timeout: float,
        management_wait_poll_seconds: float,
        current_state_getter: Callable[[], ConnectionState] | None = None,
        is_closing_getter: Callable[[], bool] | None = None,
        transition_to_state: Callable[[ConnectionState], bool] | None = None,
        reset_to_disconnected: Callable[[], bool] | None = None,
    ) -> bool | None:
        """Mark interface closed and wait for in-flight management operations.

        Parameters
        ----------
        management_shutdown_wait_timeout : float
            Maximum seconds to wait for management inflight count to reach zero.
        management_wait_poll_seconds : float
            Poll interval used while waiting on management idle condition.
        current_state_getter : Callable[[], ConnectionState] | None, optional
            Optional state probe used for transition validation.
        is_closing_getter : Callable[[], bool] | None, optional
            Optional closing-state probe used during shutdown entry.
        transition_to_state : Callable[[ConnectionState], bool] | None, optional
            Optional transition helper override.
        reset_to_disconnected : Callable[[], bool] | None, optional
            Optional reset helper override.

        Returns
        -------
        bool | None
            ``None`` when already closed; otherwise ``True`` when management wait
            timed out, ``False`` when inflight management reached idle.
        """
        get_current_state = current_state_getter or self._state_access.current_state
        get_is_closing = is_closing_getter or self._state_access.is_closing
        do_transition_to = transition_to_state or self._state_access.transition_to
        do_reset_to_disconnected = (
            reset_to_disconnected or self._state_access.reset_to_disconnected
        )
        management_wait_timed_out = False
        should_transition_to_disconnecting = False
        disconnect_alias_key: str | None = None
        management_wait_started = time.monotonic()
        with self._session.lock:
            if self._session.closed:
                logger.debug(SHUTDOWN_ALREADY_CLOSED_MSG)
                return None
        # Compatibility/state-manager probes may execute collaborator code. They
        # do not participate in the atomic closed-state claim below, so evaluate
        # closing state without interface/session locks, then revalidate closed
        # state after acquiring the normal shutdown lock order.
        was_closing = get_is_closing()
        with self._management.lock:
            with self._session.lock:
                if self._session.closed:
                    logger.debug(SHUTDOWN_ALREADY_CLOSED_MSG)
                    return None
                self._session.closed = True
                if was_closing:
                    logger.debug(
                        "BLEInterface.close called while another shutdown is in progress; continuing with cleanup"
                    )
                disconnect_alias_key = self._session.connection_alias_key
            # State-manager compatibility access may dispatch through a
            # collaborator hook. Preserve the management-before-state lock
            # ordering, but never execute that hook while the session lock is
            # held.
            current_state = get_current_state()
            should_transition_to_disconnecting = current_state not in (
                ConnectionState.DISCONNECTED,
                ConnectionState.DISCONNECTING,
            )
            if should_transition_to_disconnecting and not do_transition_to(
                ConnectionState.DISCONNECTING
            ):
                current_state = get_current_state()
                logger.error(
                    "Failed state transition to %s during shutdown (alias=%s current=%s); forcing reset.",
                    ConnectionState.DISCONNECTING.value,
                    disconnect_alias_key,
                    getattr(current_state, "value", current_state),
                )
                if not do_reset_to_disconnected() and not do_transition_to(
                    ConnectionState.DISCONNECTED
                ):
                    fallback_state = get_current_state()
                    logger.error(
                        "Failed forced transition to %s during shutdown fallback (alias=%s current=%s).",
                        ConnectionState.DISCONNECTED.value,
                        disconnect_alias_key,
                        getattr(fallback_state, "value", fallback_state),
                    )
            while self._management.inflight > 0:
                elapsed = time.monotonic() - management_wait_started
                if elapsed >= management_shutdown_wait_timeout:
                    management_wait_timed_out = True
                    logger.warning(
                        "Timed out waiting %.1fs for %d inflight management operation(s) during shutdown",
                        elapsed,
                        self._management.inflight,
                    )
                    break
                remaining = management_shutdown_wait_timeout - elapsed
                self._management.condition.wait(
                    timeout=min(management_wait_poll_seconds, remaining)
                )
        return management_wait_timed_out

    def _shutdown_discovery(
        self,
        *,
        safe_cleanup: Callable[[Callable[[], object], str], None] | None = None,
    ) -> None:
        """Close discovery resources and clear receive-loop intent.

        Parameters
        ----------
        safe_cleanup : Callable[[Callable[[], object], str], None] | None, optional
            Optional safe-cleanup wrapper used for discovery close.

        Returns
        -------
        None
            Always returns ``None``.
        """
        iface = self._iface
        run_safe_cleanup = safe_cleanup or self._error_access.safe_cleanup
        discovery_manager = iface._discovery_manager
        iface._discovery_manager = None
        if discovery_manager is not None:
            run_safe_cleanup(discovery_manager.close, "discovery manager close")
        iface._set_receive_wanted(want_receive=False)

    def _shutdown_receive_thread(
        self,
        *,
        wake_waiting_threads: Callable[..., None] | None = None,
        join_thread: Callable[..., None] | None = None,
        join_timeout: float = RECEIVE_THREAD_JOIN_TIMEOUT,
    ) -> None:  # noqa: PLR0912,PLR0915
        """Wake and join receive thread, then clear cached thread reference.

        Parameters
        ----------
        wake_waiting_threads : Callable[..., None] | None, optional
            Optional wake helper used to release receive-loop waiters.
        join_thread : Callable[..., None] | None, optional
            Optional thread-join helper used for compatibility/test injection.
        join_timeout : float
            Maximum seconds to wait for the receive thread to exit.

        Returns
        -------
        None
            Always returns ``None``.
        """
        wake_waiters = wake_waiting_threads or self._thread_access.wake_waiting_threads
        if join_thread is None:
            join_runtime_thread = self._thread_access.join_thread
        else:
            join_accepts_timeout_kwarg = True
            try:
                parameters = inspect.signature(join_thread).parameters
            except (TypeError, ValueError):
                join_accepts_timeout_kwarg = True
            else:
                timeout_parameter = parameters.get("timeout")
                if timeout_parameter is not None:
                    join_accepts_timeout_kwarg = timeout_parameter.kind in (
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        inspect.Parameter.KEYWORD_ONLY,
                    )
                else:
                    join_accepts_timeout_kwarg = any(
                        parameter.kind is inspect.Parameter.VAR_KEYWORD
                        for parameter in parameters.values()
                    )

            def join_runtime_thread(thread: object, *, timeout: float | None) -> None:
                if join_accepts_timeout_kwarg:
                    join_thread(thread, timeout=timeout)
                    return
                join_thread(thread, timeout)

        try:
            wake_waiters(READ_TRIGGER_EVENT, RECONNECTED_EVENT)
        except Exception:  # noqa: BLE001 - close must remain best effort
            _log_ble_failure(
                _BLEFailureDisposition.BEST_EFFORT,
                "Error waking BLE receive-thread waiters during close",
            )
        with self._session.lock:
            receive_thread = self._session.receive_thread
        if receive_thread is None:
            return
        thread_ident, thread_is_alive = _thread_start_probe(receive_thread)

        def _explicitly_not_alive(thread_like: object) -> bool:
            is_alive_probe = getattr(thread_like, "is_alive", None)
            if not callable(is_alive_probe):
                return False
            try:
                return is_alive_probe() is False
            except Exception:  # noqa: BLE001 - shutdown probe is best effort
                _log_ble_failure(
                    _BLEFailureDisposition.BEST_EFFORT,
                    "Unable to probe BLE receive-thread liveness during close",
                )
                return False

        start_failure_confirmed = False
        if thread_ident is None and not thread_is_alive:
            started_event = getattr(receive_thread, "_started", None)
            is_started = getattr(started_event, "is_set", None)
            if callable(is_started):
                try:
                    start_failure_confirmed = not bool(is_started())
                except Exception:  # noqa: BLE001 - shutdown probe is best effort
                    _log_ble_failure(
                        _BLEFailureDisposition.BEST_EFFORT,
                        "Unable to probe BLE receive-thread start state during close",
                    )
                    start_failure_confirmed = False
            elif isinstance(receive_thread, threading.Thread):
                start_failure_confirmed = True
            elif _explicitly_not_alive(receive_thread):
                start_failure_confirmed = True
            if start_failure_confirmed:
                with self._session.lock:
                    if self._session.receive_thread is receive_thread:
                        self._session.receive_thread = None
                        self._session.receive_start_pending = False
                        self._session.receive_start_pending_since = None
                logger.debug(
                    "Skipping receive thread join during close: worker never started."
                )
                return
        current_ident = threading.get_ident()
        if (
            thread_ident is not None and thread_ident == current_ident
        ) or receive_thread is threading.current_thread():
            logger.debug("close() called from receive thread; skipping self-join")
        else:
            try:
                join_runtime_thread(
                    receive_thread,
                    timeout=join_timeout,
                )
            except Exception:  # noqa: BLE001 - close must remain best effort
                _log_ble_failure(
                    _BLEFailureDisposition.BEST_EFFORT,
                    "Error joining BLE receive thread during close",
                )
            post_join_ident, post_join_is_alive = _thread_start_probe(receive_thread)
            if post_join_is_alive:
                logger.warning(
                    "BLE receive thread did not exit within %.1fs",
                    join_timeout,
                )
            thread_ident = post_join_ident
            thread_is_alive = post_join_is_alive
        # Thread-like liveness probes can execute collaborator code. Keep them
        # outside the shared session lock and reserve the critical section for
        # the identity-checked compare-and-clear below.
        probe_confirms_stopped = not thread_is_alive
        if not probe_confirms_stopped:
            probe_confirms_stopped = _explicitly_not_alive(receive_thread)
        with self._session.lock:
            if self._session.receive_thread is receive_thread and (
                start_failure_confirmed or probe_confirms_stopped
            ):
                self._session.receive_thread = None
                self._session.receive_start_pending = False
                self._session.receive_start_pending_since = None

    def _close_mesh_interface(
        self,
        *,
        safe_execute: Callable[[Callable[[], object]], object | None] | None = None,
        timeout: float | None = None,
    ) -> None:
        """Run ``MeshInterface.close`` through guarded error-handler execution.

        Parameters
        ----------
        safe_execute : Callable[[Callable[[], object]], object | None] | None, optional
            Optional safe-execute wrapper used for close invocation.
        timeout : float | None, optional
            Optional maximum seconds to wait for ``MeshInterface.close``. When
            ``None``, close runs inline without an additional wait budget.

        Returns
        -------
        None
            Always returns ``None``.
        """
        iface = self._iface
        run_safe_execute = safe_execute or (
            lambda func: self._error_access.safe_execute(
                func,
                error_msg="Error closing mesh interface",
            )
        )
        if timeout is None:
            run_safe_execute(lambda: MeshInterface.close(iface))
            return

        if timeout <= 0:
            logger.warning(
                "Skipping MeshInterface.close(): close timeout budget exhausted."
            )
            return

        def _run_mesh_close() -> None:
            try:
                run_safe_execute(lambda: MeshInterface.close(iface))
            finally:
                with self._bounded_thread_lock:
                    if self._bounded_mesh_close_thread is threading.current_thread():
                        self._bounded_mesh_close_thread = None

        close_thread = threading.Thread(
            target=_run_mesh_close,
            name="BLEMeshInterfaceClose",
            daemon=True,
        )
        try:
            with self._bounded_thread_lock:
                previous_thread = self._bounded_mesh_close_thread
                if previous_thread is not None and previous_thread.is_alive():
                    logger.warning(
                        "Skipping MeshInterface.close(): previous bounded close thread is still running."
                    )
                    return
                self._bounded_mesh_close_thread = close_thread
                close_thread.start()
        except Exception:  # noqa: BLE001 - close must remain best effort
            with self._bounded_thread_lock:
                if self._bounded_mesh_close_thread is close_thread:
                    self._bounded_mesh_close_thread = None
            logger.warning(
                "Failed to start MeshInterface.close thread; skipping bounded close stage to preserve timeout contract.",
                exc_info=True,
            )
            return

        close_thread.join(timeout=timeout)
        if close_thread.is_alive():
            logger.warning(
                "Timed out waiting %.3fs for MeshInterface.close(); continuing BLE shutdown.",
                timeout,
            )

    def _unregister_exit_handler(self) -> None:
        """Unregister process exit handler when present."""
        iface = self._iface
        if iface._exit_handler:
            atexit.unregister(iface._exit_handler)
            iface._exit_handler = None

    def _detach_client_for_shutdown(self) -> tuple["BLEClient | None", bool]:
        """Detach active client reference and return detached client plus publish state.

        Returns
        -------
        tuple[BLEClient | None, bool]
            Detached client plus the publish-pending flag captured at detach.
        """
        iface = self._iface
        with self._session.lock:
            client = iface.client
            publish_pending = self._session.client_publish_pending
            if client is not None:
                iface.client = None
        return client, publish_pending

    def _consume_disconnect_notification_state(self) -> bool:
        """Consume publish flags and decide disconnect notification emission."""
        notify = False
        with self._session.lock:
            if self._session.client_publish_pending:
                replacement_pending = self._session.client_replacement_pending
                self._session.client_publish_pending = False
                self._session.client_replacement_pending = False
                if replacement_pending and not self._session.disconnect_notified:
                    self._session.disconnect_notified = True
                    notify = True
                else:
                    self._session.disconnect_notified = True
            elif self._session.client_replacement_pending:
                self._session.client_replacement_pending = False
                if not self._session.disconnect_notified:
                    self._session.disconnect_notified = True
                    notify = True
            elif not self._session.disconnect_notified:
                self._session.disconnect_notified = True
                notify = self._session.ever_connected is True
        return notify

    def _shutdown_client(
        self,
        *,
        management_wait_timed_out: bool,
        detach_client_for_shutdown: (
            Callable[[], tuple["BLEClient | None", bool]] | None
        ) = None,
        safe_cleanup: Callable[[Callable[[], object], str], None] | None = None,
        consume_disconnect_notification_state: Callable[[], bool] | None = None,
        unsubscribe_timeout: float | None = NOTIFICATION_START_TIMEOUT,
        disconnect_notification_wait_timeout: float | None = DISCONNECT_TIMEOUT_SECONDS,
        client_disconnect_timeout: float | None = DISCONNECT_TIMEOUT_SECONDS,
        bounded_close_timeout_active: bool = False,
    ) -> None:
        """Shutdown active client resources and notification publication state.

        Parameters
        ----------
        management_wait_timed_out : bool
            Whether shutdown already timed out waiting for management idle.
        detach_client_for_shutdown : Callable[[], tuple[BLEClient | None, bool]] | None, optional
            Optional detach helper override.
        safe_cleanup : Callable[[Callable[[], object], str], None] | None, optional
            Optional safe-cleanup wrapper override.
        consume_disconnect_notification_state : Callable[[], bool] | None, optional
            Optional publish-state consumer override.
        unsubscribe_timeout : float | None
            Maximum seconds to wait for notification unsubscribe cleanup.
        disconnect_notification_wait_timeout : float | None
            Maximum seconds to wait for disconnect notification publication.
        client_disconnect_timeout : float | None
            Maximum seconds to wait for BLE client disconnect/close.
        bounded_close_timeout_active : bool
            Whether these stage timeouts were derived from a caller-provided
            finite ``close(timeout=...)`` budget. Legacy no-timeout fallbacks
            are skipped only when this is true.

        Returns
        -------
        None
            Always returns ``None``.
        """
        iface = self._iface
        detach_client = detach_client_for_shutdown or self._detach_client_for_shutdown
        run_safe_cleanup = safe_cleanup or self._error_access.safe_cleanup
        consume_disconnect_state = (
            consume_disconnect_notification_state
            or self._consume_disconnect_notification_state
        )
        client, publish_pending = detach_client()
        client_address = iface._extract_client_address(client)
        notification_manager = iface._notification_manager

        def _has_finite_timeout(timeout: float | None) -> bool:
            return timeout is not None and math.isfinite(timeout)

        def _resolve_notification_cleanup(
            method_name: str,
        ) -> Callable[..., object] | None:
            candidate_names = [method_name]
            if method_name == "_unsubscribe_all":
                candidate_names.append("unsubscribe_all")
            elif method_name == "_cleanup_all":
                candidate_names.append("cleanup_all")
            for candidate_name in candidate_names:
                method = _get_declared_callable(notification_manager, candidate_name)
                if callable(method):
                    return cast(Callable[..., object], method)
            return None

        if client is not None:
            gate_context = (
                iface._management_target_gate(client_address)
                if client_address is not None
                and not management_wait_timed_out
                and not publish_pending
                else contextlib.nullcontext()
            )
            with gate_context:
                unsubscribe_all = _resolve_notification_cleanup("_unsubscribe_all")
                if unsubscribe_all is not None:
                    unsubscribe_error: TypeError | None = None

                    def _unsubscribe_with_timeout() -> None:
                        nonlocal unsubscribe_error
                        try:
                            unsubscribe_all(client, timeout=unsubscribe_timeout)
                        except TypeError as exc:
                            if not _is_unexpected_keyword_error(exc, "timeout"):
                                unsubscribe_error = exc
                                raise
                            if bounded_close_timeout_active and _has_finite_timeout(
                                unsubscribe_timeout
                            ):
                                logger.warning(
                                    "Skipping legacy notification unsubscribe_all() fallback: close timeout budget is active and the "
                                    "callable does not accept timeout."
                                )
                                return
                            unsubscribe_all(client)

                    run_safe_cleanup(
                        _unsubscribe_with_timeout,
                        "notification unsubscribe_all",
                    )
                    if unsubscribe_error is not None:
                        raise unsubscribe_error
                else:
                    logger.debug("Notification manager is missing _unsubscribe_all")

                disconnect_error: TypeError | None = None

                def _disconnect_client_with_timeout() -> None:
                    nonlocal disconnect_error
                    try:
                        iface._disconnect_and_close_client(
                            client, timeout=client_disconnect_timeout
                        )
                    except TypeError as exc:
                        if not _is_unexpected_keyword_error(exc, "timeout"):
                            disconnect_error = exc
                            raise
                        if bounded_close_timeout_active and _has_finite_timeout(
                            client_disconnect_timeout
                        ):
                            logger.warning(
                                "Skipping legacy BLE client disconnect/close fallback: close timeout budget is active and the "
                                "callable does not accept timeout."
                            )
                            return
                        iface._disconnect_and_close_client(client)

                run_safe_cleanup(
                    _disconnect_client_with_timeout,
                    "BLE client disconnect/close",
                )
                if disconnect_error is not None:
                    raise disconnect_error
        cleanup_all = _resolve_notification_cleanup("_cleanup_all")
        if cleanup_all is not None:
            run_safe_cleanup(cleanup_all, "notification manager cleanup")
        else:
            logger.debug("Notification manager is missing _cleanup_all")

        if consume_disconnect_state():
            iface._disconnected()
            try:
                iface._wait_for_disconnect_notifications(
                    timeout=disconnect_notification_wait_timeout
                )
            except TypeError as exc:
                if not _is_unexpected_keyword_error(exc, "timeout"):
                    raise
                if bounded_close_timeout_active and _has_finite_timeout(
                    disconnect_notification_wait_timeout
                ):
                    logger.warning(
                        "Skipping legacy disconnect notification wait fallback: close timeout budget is active and the "
                        "callable does not accept timeout."
                    )
                    return
                iface._wait_for_disconnect_notifications()

    def _finalize_close_state(
        self,
        *,
        current_state_getter: Callable[[], ConnectionState] | None = None,
        transition_to_state: Callable[[ConnectionState], bool] | None = None,
        reset_to_disconnected: Callable[[], bool] | None = None,
    ) -> None:
        """Persist terminal disconnected state and clear address registry claims.

        Parameters
        ----------
        current_state_getter : Callable[[], ConnectionState] | None, optional
            Optional state probe override.
        transition_to_state : Callable[[ConnectionState], bool] | None, optional
            Optional transition helper override.
        reset_to_disconnected : Callable[[], bool] | None, optional
            Optional reset helper override.

        Returns
        -------
        None
            Always returns ``None``.
        """
        iface = self._iface
        get_current_state = current_state_getter or self._state_access.current_state
        do_transition_to = transition_to_state or self._state_access.transition_to
        do_reset_to_disconnected = (
            reset_to_disconnected or self._state_access.reset_to_disconnected
        )
        alias_key: str | None = None
        with self._session.lock:
            alias_key = self._session.connection_alias_key
            self._session.connection_alias_key = None
        # Record final state as DISCONNECTED for observers; instance remains closed.
        if not do_transition_to(ConnectionState.DISCONNECTED):
            current_state = get_current_state()
            logger.error(
                "Failed state transition to %s during close finalization (alias=%s current=%s); forcing reset.",
                ConnectionState.DISCONNECTED.value,
                alias_key,
                getattr(current_state, "value", current_state),
            )
            if not do_reset_to_disconnected() and not do_transition_to(
                ConnectionState.DISCONNECTED
            ):
                fallback_state = get_current_state()
                logger.error(
                    "Failed forced transition to %s during close finalization (alias=%s current=%s).",
                    ConnectionState.DISCONNECTED.value,
                    alias_key,
                    getattr(fallback_state, "value", fallback_state),
                )
        close_key = _addr_key(iface.address)
        iface._mark_address_keys_disconnected(close_key, alias_key)

    def close(
        self,
        *,
        management_shutdown_wait_timeout: float,
        management_wait_poll_seconds: float,
        timeout: float | None = None,
    ) -> None:
        """Shut down BLE resources within an optional total timeout budget.

        When ``timeout`` is provided, remaining-budget slices are applied to
        each blocking shutdown phase (management wait, receive-thread join,
        ``MeshInterface.close``, disconnect-notification waits, and client
        disconnect/close). Best-effort final state cleanup still runs after
        teardown stages complete or time out.
        """
        deadline: float | None = None
        if timeout is not None:
            deadline = time.monotonic() + max(0.0, timeout)

        def _remaining_timeout(default_timeout: float) -> float:
            if deadline is None:
                return default_timeout
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return 0.0
            return min(default_timeout, remaining)

        def _remaining_optional_timeout(
            default_timeout: float | None,
        ) -> float | None:
            if deadline is None:
                return default_timeout
            remaining = max(0.0, deadline - time.monotonic())
            if default_timeout is None:
                return remaining
            return min(default_timeout, remaining)

        management_wait_timed_out = self._await_management_shutdown(
            management_shutdown_wait_timeout=_remaining_timeout(
                management_shutdown_wait_timeout
            ),
            management_wait_poll_seconds=management_wait_poll_seconds,
        )
        if management_wait_timed_out is None:
            # A previous bounded cleanup may have timed out while a worker
            # stayed alive. A repeated close still needs to revisit it.
            self._cleanup_thread_coordinator(
                timeout=_remaining_optional_timeout(None),
            )
            return

        try:
            iface = self._iface
            if iface._shutdown_event is not None:
                iface._shutdown_event.set()
            self._shutdown_discovery()
            self._shutdown_receive_thread(
                join_timeout=_remaining_timeout(RECEIVE_THREAD_JOIN_TIMEOUT)
            )
            self._close_mesh_interface(
                timeout=_remaining_optional_timeout(None),
            )
            self._unregister_exit_handler()
            self._shutdown_client(
                management_wait_timed_out=management_wait_timed_out,
                unsubscribe_timeout=_remaining_optional_timeout(
                    NOTIFICATION_START_TIMEOUT
                ),
                disconnect_notification_wait_timeout=_remaining_optional_timeout(
                    DISCONNECT_TIMEOUT_SECONDS
                ),
                client_disconnect_timeout=_remaining_optional_timeout(
                    DISCONNECT_TIMEOUT_SECONDS
                ),
                bounded_close_timeout_active=timeout is not None,
            )
        finally:
            try:
                self._cleanup_thread_coordinator(
                    timeout=_remaining_optional_timeout(None),
                )
            finally:
                self._finalize_close_state()
