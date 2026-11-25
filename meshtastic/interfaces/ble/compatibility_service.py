"""Compatibility/event publication helpers for BLE interface orchestration."""

from queue import Empty, Full
from threading import Event, Thread
from typing import TYPE_CHECKING, Any, Callable, cast

from meshtastic.interfaces.ble.compat_adapter import (
    _get_declared_callable,
    _get_declared_member,
    _load_runtime_module,
)
from meshtastic.interfaces.ble.constants import (
    DISCONNECT_TIMEOUT_SECONDS,
    MAX_DRAIN_ITERATIONS,
    logger,
)
from meshtastic.interfaces.ble.failure_policy import (
    _BLEFailureDisposition,
    _log_ble_failure,
)
from meshtastic.interfaces.ble.lifecycle_primitives import _LifecycleStateAccess
from meshtastic.interfaces.ble.utils import (
    _resolve_safe_execute as _resolve_safe_execute_hook,
)
from meshtastic.interfaces.ble.utils import _safe_execute_through_adapter
from meshtastic.util import DeferredExecution

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.interface import BLEInterface


class BLECompatibilityEventPublisher:
    """Instance-bound collaborator for compatibility event publication.

    Notes
    -----
    All publication helpers are best-effort and must not raise during
    disconnect/close paths where callers are already unwinding state.
    """

    def __init__(
        self,
        iface: "BLEInterface",
        *,
        publishing_thread_provider: Callable[[], object | None],
    ) -> None:
        """Bind publisher helpers to a specific interface instance.

        Parameters
        ----------
        iface : BLEInterface
            Interface instance whose compatibility event publication is delegated
            through this collaborator.
        publishing_thread_provider : Callable[[], object | None]
            Callable returning the current publishing-thread facade.

        Returns
        -------
        None
            Initializes bound collaborator state.
        """
        self._iface = iface
        self._publishing_thread_provider = publishing_thread_provider
        self._last_publishing_thread: object | None = None

    @staticmethod
    def _is_live_publishing_thread(publishing_thread: object | None) -> bool:
        """Return whether a facade can accept work, including worker recovery."""
        if publishing_thread is None:
            return False
        if isinstance(publishing_thread, DeferredExecution):
            # Its admission path owns worker recovery; a dead worker is not a
            # reason to discard the facade or bypass its FIFO queue.
            return publishing_thread._accepts_work()  # noqa: SLF001
        thread = getattr(publishing_thread, "thread", None)
        if thread is None:
            return True
        is_alive = getattr(thread, "is_alive", None)
        if not callable(is_alive):
            return True
        try:
            alive_result = is_alive()
        except Exception:  # noqa: BLE001 - liveness probe must stay best effort
            logger.debug(
                "Error probing publishing thread liveness.",
                exc_info=True,
            )
            return False
        return alive_result if isinstance(alive_result, bool) else False

    def _resolve_publishing_thread(
        self, *, use_cached_fallback: bool = True, for_flush: bool = False
    ) -> object | None:
        """Resolve publishing-thread facade with cached fallback on provider failure.

        Returns
        -------
        object | None
            Current publishing-thread facade when available; otherwise the last
            known-good facade or ``None`` when none has been resolved.

        Notes
        -----
        Provider failures are suppressed and logged because disconnect/shutdown
        paths must remain best effort. The mesh-interface module is loaded by
        string because BLE module import must not depend on the runtime
        publish-queue state of that module at import time.
        """
        def _available(candidate: object | None) -> bool:
            # A stopped executor still owns its terminal drain.
            return (for_flush and isinstance(candidate, DeferredExecution)) or (
                self._is_live_publishing_thread(candidate)
            )

        try:
            publishing_thread = self._publishing_thread_provider()
            if _available(publishing_thread):
                self._last_publishing_thread = publishing_thread
                return publishing_thread
            if publishing_thread is not None:
                self._last_publishing_thread = None
        except Exception:  # noqa: BLE001 - teardown path must stay best effort
            logger.debug(
                "Error resolving publishing thread for compatibility publisher.",
                exc_info=True,
            )
        if not use_cached_fallback:
            return None
        if _available(self._last_publishing_thread):
            return self._last_publishing_thread
        self._last_publishing_thread = None
        # Loaded by string: BLE module import must not depend on the runtime
        # publish-queue state of the mesh-interface module.
        mesh_iface_module = _load_runtime_module("meshtastic.mesh_interface")
        fallback_thread = getattr(mesh_iface_module, "publishingThread", None)
        if _available(fallback_thread):
            self._last_publishing_thread = fallback_thread
            return fallback_thread
        return None

    @staticmethod
    def _enqueue_publish_callback(
        publishing_thread: object,
        callback: Any,
        *,
        prefer_non_blocking: bool = False,
    ) -> bool:
        """Queue a callback on the publish thread.

        Parameters
        ----------
        publishing_thread : object
            Publishing-thread façade exposing ``queueWork`` and/or ``queue``.
        callback : Any
            Callable to enqueue for deferred execution.
        prefer_non_blocking : bool
            When ``True``, use an executor's atomic non-blocking enqueue contract
            when available, otherwise attempt ``queue.put_nowait``. If neither
            path can accept the callback, return ``False`` so callers can apply
            an inline fallback.

        Returns
        -------
        bool
            ``True`` when the callback was queued, otherwise ``False``.
        """
        if isinstance(publishing_thread, DeferredExecution):
            # The atomic path both recovers dead workers and reports rejection
            # after stop. The public queueWork() intentionally returns None.
            return publishing_thread._queue_work_non_blocking(callback)  # noqa: SLF001
        queue_work = getattr(publishing_thread, "queueWork", None)
        queue_work_non_blocking = _get_declared_callable(
            publishing_thread, "_queue_work_non_blocking"
        )
        queue = getattr(publishing_thread, "queue", None)
        put_nowait = getattr(queue, "put_nowait", None)
        can_queue_work = callable(queue_work)
        can_put_nowait = callable(put_nowait)
        queue_work_callback: Callable[[Any], object] | None = (
            cast(Callable[[Any], object], queue_work) if can_queue_work else None
        )
        queue_work_non_blocking_callback: Callable[[Any], bool] | None = (
            cast(Callable[[Any], bool], queue_work_non_blocking)
            if callable(queue_work_non_blocking)
            else None
        )
        put_nowait_callback: Callable[[Any], object] | None = (
            cast(Callable[[Any], object], put_nowait) if can_put_nowait else None
        )
        thread = getattr(publishing_thread, "thread", None)
        is_alive = getattr(thread, "is_alive", None)
        thread_is_alive: bool | None = None
        if callable(is_alive):
            try:
                alive_result = is_alive()
            except Exception:  # noqa: BLE001 - enqueue path is best effort
                alive_result = False
            thread_is_alive = alive_result if isinstance(alive_result, bool) else False
        if thread_is_alive is False:
            return False

        queued = False
        if prefer_non_blocking:
            if queue_work_non_blocking_callback is not None:
                queued = queue_work_non_blocking_callback(callback)
            elif put_nowait_callback is not None:
                try:
                    put_nowait_callback(callback)
                    queued = True
                except Full:
                    pass
        elif queue_work_callback is not None:
            queue_work_callback(callback)
            queued = True
        elif put_nowait_callback is not None:
            try:
                put_nowait_callback(callback)
                queued = True
            except Full:
                pass
        return queued

    def wait_for_disconnect_notifications(self, timeout: float | None = None) -> None:
        """Wait for queued disconnect notifications to flush.

        Parameters
        ----------
        timeout : float | None
            Maximum seconds to wait for flush completion. ``None`` uses the
            default disconnect timeout.

        Returns
        -------
        None
            The deferred executor owns its drain. For legacy queue facades,
            queueing or wait failures may trigger inline draining.
        """
        self._flush_disconnect_notifications(
            timeout,
            publishing_thread=self._resolve_publishing_thread(for_flush=True),
        )

    def _flush_disconnect_notifications(
        self,
        timeout: float | None,
        *,
        publishing_thread: object | None,
    ) -> None:
        """Flush queued disconnect notifications on a resolved publishing thread."""
        if timeout is None:
            timeout = DISCONNECT_TIMEOUT_SECONDS
        if publishing_thread is None:
            return
        flush_event = Event()
        if isinstance(publishing_thread, DeferredExecution):
            self._flush_deferred_queue(publishing_thread, flush_event, timeout)
            return

        def _queue_flush_notification() -> bool:
            return self._enqueue_publish_callback(
                publishing_thread,
                flush_event.set,
                prefer_non_blocking=True,
            )

        queued = _safe_execute_through_adapter(
            self._iface,
            _queue_flush_notification,
            default_return=False,
            error_msg="Runtime error during disconnect notification flush (possible threading issue)",
            reraise=False,
        )
        if not queued:
            self._drain_publish_queue_on_thread(
                flush_event,
                publishing_thread=publishing_thread,
            )
            return

        if not flush_event.wait(timeout=timeout):
            thread = getattr(publishing_thread, "thread", None)
            is_alive = getattr(thread, "is_alive", None)
            alive_result = False
            if callable(is_alive):
                try:
                    alive_result = is_alive()
                except Exception:  # noqa: BLE001 - disconnect fallback must continue
                    alive_result = False
            thread_is_alive = alive_result if isinstance(alive_result, bool) else False
            if thread_is_alive:
                logger.debug("Timed out waiting for publish queue flush")
            else:
                self._drain_publish_queue_on_thread(
                    flush_event,
                    publishing_thread=publishing_thread,
                )

    def drain_publish_queue(self, flush_event: Event) -> None:
        """Drain pending publish callbacks for the bound interface.

        Parameters
        ----------
        flush_event : Event
            Event used to stop direct-drain iteration.

        Returns
        -------
        None
            Drain execution is best effort.
        """
        self._drain_publish_queue_on_thread(
            flush_event,
            publishing_thread=self._resolve_publishing_thread(for_flush=True),
        )

    def _flush_deferred_queue(
        self, executor: DeferredExecution, flush_event: Event, timeout: float
    ) -> None:
        """Wait for the executor's own consumer to drain through a barrier."""
        if not _safe_execute_through_adapter(
            self._iface,
            lambda: executor._flush(flush_event, timeout),  # noqa: SLF001
            default_return=False,
            error_msg="Error flushing deferred publish queue",
            reraise=False,
        ):
            logger.debug("Deferred publish queue flush did not complete")

    def _drain_publish_queue_on_thread(
        self,
        flush_event: Event,
        *,
        publishing_thread: object | None,
    ) -> None:
        """Drain pending publish callbacks on the current thread.

        Parameters
        ----------
        flush_event : Event
            Event set by queued flush callbacks to indicate completion.
        publishing_thread : object | None
            Publishing-thread façade exposing queue-drain internals.

        Returns
        -------
        None
            Drains callbacks best-effort and suppresses callback failures.
        """
        if publishing_thread is None:
            return
        if isinstance(publishing_thread, DeferredExecution):
            self._flush_deferred_queue(
                publishing_thread, flush_event, DISCONNECT_TIMEOUT_SECONDS
            )
            return
        thread = getattr(publishing_thread, "thread", None)
        thread_drain = getattr(thread, "_drain_publish_queue", None)
        if callable(thread_drain):
            try:
                _safe_execute_through_adapter(
                    self._iface,
                    lambda: thread_drain(flush_event),
                    error_msg="Error draining publish queue via publishing thread",
                    reraise=True,
                )
                return
            except Exception:  # noqa: BLE001 - fall back to direct draining path
                logger.debug(
                    "Error draining publish queue via publishing thread; falling back to direct drain.",
                    exc_info=True,
                )

        # Do not call iface._drain_publish_queue here - it would recursively
        # call this method again. Instead, fall through to the direct queue
        # draining logic below.

        queue = getattr(publishing_thread, "queue", None)
        get_nowait = getattr(queue, "get_nowait", None)
        if queue is None or not callable(get_nowait):
            return
        iterations = 0
        while not flush_event.is_set():
            if iterations >= MAX_DRAIN_ITERATIONS:
                logger.debug(
                    "Stopping publish queue drain after %d callbacks to avoid shutdown starvation",
                    MAX_DRAIN_ITERATIONS,
                )
                break
            try:
                runnable = get_nowait()
            except Empty:
                break
            except Exception:  # noqa: BLE001 - direct drain must stay best effort
                logger.debug(
                    "Error reading deferred publish queue during direct drain",
                    exc_info=True,
                )
                break
            iterations += 1
            _safe_execute_through_adapter(
                self._iface,
                cast(Callable[[], Any], runnable),
                error_msg="Error in deferred publish callback",
                reraise=False,
            )

    def publish_connection_status(self, *, connected: bool) -> None:
        """Publish legacy connection-status event for compatibility.

        Parameters
        ----------
        connected : bool
            Connection status flag to publish.

        Returns
        -------
        None
            Publication is best-effort; failures are logged and suppressed.
        """
        self._publish_connection_status_on_thread(
            connected=connected,
            publishing_thread=self._resolve_publishing_thread(),
        )

    def _publish_connection_status_on_thread(
        self,
        *,
        connected: bool,
        publishing_thread: object | None,
    ) -> None:
        """Publish connection status through a resolved publishing thread."""
        # Loaded by string: the publish-queue state of the mesh-interface module
        # is runtime state, and BLE module import must not depend on it.
        mesh_iface_module = _load_runtime_module("meshtastic.mesh_interface")
        mesh_pub: Any = getattr(mesh_iface_module, "pub", None)
        if mesh_pub is None:
            logger.debug("Skipping connection status publish: mesh pub is unavailable")
            return

        def _publish_status() -> None:
            try:
                mesh_pub.sendMessage(
                    "meshtastic.connection.status",
                    interface=self._iface,
                    connected=connected,
                )
            except Exception:  # noqa: BLE001 - best-effort publish path
                logger.debug(
                    "Error publishing %s status via mesh_pub.sendMessage",
                    "connect" if connected else "disconnect",
                    exc_info=True,
                )

        def _is_iface_closing() -> bool:
            try:
                closed = _get_declared_member(self._iface, "_closed", False)
                if closed is True:
                    return True
                state_manager = _get_declared_member(self._iface, "_state_manager")
                if state_manager is None:
                    return False
                return _LifecycleStateAccess(state_manager).is_closing()
            except Exception:  # noqa: BLE001 - status publication is best effort
                _log_ble_failure(
                    _BLEFailureDisposition.COMPATIBILITY_FALLBACK,
                    "Error probing interface closing state during status publication",
                )
                return False

        def _publish_status_async(reason: str) -> bool:
            if _is_iface_closing():
                logger.debug(
                    "Skipping connection status publish during shutdown: %s.",
                    reason,
                )
                return False

            def _async_publish() -> None:
                try:
                    _publish_status()
                except (
                    Exception
                ):  # noqa: BLE001 - async fallback must remain best effort
                    logger.debug(
                        "Error in async fallback connection status publish.",
                        exc_info=True,
                    )

            try:
                Thread(
                    target=_async_publish,
                    name="ble-compat-status-publish",
                    daemon=True,
                ).start()
            except Exception:  # noqa: BLE001 - async fallback must remain best effort
                logger.debug(
                    "Error starting async fallback connection status publish.",
                    exc_info=True,
                )
                return False
            logger.debug(
                "Queued async fallback connection status publish: %s.",
                reason,
            )
            return True

        if publishing_thread is None:
            logger.debug(
                "Publishing thread is unavailable; using async fallback publish."
            )
            _publish_status_async("publishing thread unavailable")
            return

        try:
            queued = self._enqueue_publish_callback(
                publishing_thread,
                _publish_status,
                prefer_non_blocking=True,
            )
            if not queued:
                logger.debug(
                    "Publish queue is unavailable; using async fallback publish."
                )
                _publish_status_async("publish queue unavailable")
                return
        except Exception:  # noqa: BLE001 - best-effort queueing path
            logger.debug(
                "Error queuing connection status publish via publishingThread.queueWork",
                exc_info=True,
            )
            _publish_status_async("enqueue failure")
            return

    # COMPAT_STABLE_SHIM: retained bound alias for compatibility callers.
    def publish_connection_status_legacy(
        self, connected: bool
    ) -> None:  # noqa: FBT001 - compatibility positional bool
        """Publish status through legacy positional API retained for compatibility.

        This alias preserves historical call sites that invoke
        ``publish_connection_status_legacy(connected)`` directly on the bound
        compatibility publisher while runtime behavior remains delegated to the
        canonical ``publish_connection_status`` implementation.

        Parameters
        ----------
        connected : bool
            Connection status flag (``True`` for connected, ``False`` for
            disconnected).

        Returns
        -------
        None
            Always returns ``None``.
        """
        self.publish_connection_status(connected=connected)


# Retain the documented stable service alias without routing the publisher through it.
class BLECompatibilityEventService:
    """Service helpers for compatibility event publication paths.

    Notes
    -----
    All helpers in this service are best-effort and must not raise during
    disconnect/close paths where callers are already unwinding state.
    """

    @staticmethod
    def _resolve_safe_execute(iface: "BLEInterface") -> Callable[..., Any] | None:
        """Resolve an error-handler ``safe_execute`` hook with underscore fallback.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing an optional ``error_handler`` attribute.

        Returns
        -------
        Callable[..., Any] | None
            Resolved ``safe_execute`` callable when available and configured,
            otherwise ``None``.
        """
        return _resolve_safe_execute_hook(iface)

    @staticmethod
    def _safe_execute(
        iface: "BLEInterface",
        func: Callable[[], Any],
        *,
        default_return: Any = None,
        error_msg: str,
        reraise: bool = False,
    ) -> Any:
        """Execute a callable through resolved safe execution with best-effort fallback.

        Parameters
        ----------
        iface : BLEInterface
            Interface used to resolve ``error_handler.safe_execute`` helpers.
        func : Callable[[], Any]
            Zero-argument callable to execute.
        default_return : Any
            Value returned when execution fails and ``reraise`` is ``False``.
        error_msg : str
            Log message used when execution fails.
        reraise : bool
            When ``True``, propagate execution exceptions after logging.

        Returns
        -------
        Any
            Callable return value on success; otherwise ``default_return`` when
            failures are handled.

        Raises
        ------
        Exception
            Re-raises execution failures only when ``reraise`` is ``True``.
        """
        return _safe_execute_through_adapter(
            iface,
            func,
            default_return=default_return,
            error_msg=error_msg,
            reraise=reraise,
        )

    @staticmethod
    def _enqueue_publish_callback(
        publishing_thread: object,
        callback: Any,
        *,
        prefer_non_blocking: bool = False,
    ) -> bool:
        """Queue a callback on the publish thread.

        Parameters
        ----------
        publishing_thread : object
            Publishing-thread façade exposing ``queueWork`` and/or ``queue``.
        callback : Any
            Callable to enqueue for deferred execution.
        prefer_non_blocking : bool
            When ``True``, use an executor's atomic non-blocking enqueue contract
            when available, otherwise attempt ``queue.put_nowait``. If neither
            path can accept the callback, return ``False`` so callers can apply
            an inline fallback.

        Returns
        -------
        bool
            ``True`` when the callback was queued, otherwise ``False``.
        """
        return BLECompatibilityEventPublisher._enqueue_publish_callback(  # noqa: SLF001
            publishing_thread, callback, prefer_non_blocking=prefer_non_blocking
        )

    @staticmethod
    def wait_for_disconnect_notifications(
        iface: "BLEInterface",
        timeout: float | None = None,
        *,
        publishing_thread: object | None,
    ) -> None:
        """Wait for queued disconnect notifications to flush.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing error-handling helpers and fallback drain logic.
        timeout : float | None
            Maximum seconds to wait for flush completion. ``None`` uses the
            default disconnect timeout.
        publishing_thread : object | None
            Publishing-thread façade used to queue and drain callbacks.

        Returns
        -------
        None
            This method returns ``None`` and falls back to inline draining on
            queueing or wait failures.
        """
        publisher = BLECompatibilityEventPublisher(
            iface, publishing_thread_provider=lambda: publishing_thread
        )
        publisher._flush_disconnect_notifications(  # noqa: SLF001
            timeout, publishing_thread=publishing_thread
        )

    @staticmethod
    def drain_publish_queue(
        iface: "BLEInterface",
        flush_event: Event,
        *,
        publishing_thread: object | None,
    ) -> None:
        """Drain pending publish callbacks on the current thread.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing safe execution helpers.
        flush_event : Event
            Event set by queued flush callbacks to indicate completion.
        publishing_thread : object | None
            Publishing-thread façade exposing queue-drain internals.

        Returns
        -------
        None
            Drains callbacks best-effort and suppresses callback failures.
        """
        publisher = BLECompatibilityEventPublisher(
            iface, publishing_thread_provider=lambda: publishing_thread
        )
        publisher._drain_publish_queue_on_thread(  # noqa: SLF001
            flush_event, publishing_thread=publishing_thread
        )

    @staticmethod
    def publish_connection_status(
        iface: "BLEInterface",
        connected: bool,
        *,
        publishing_thread: object | None = None,
    ) -> None:
        """Publish legacy connection-status event for compatibility.

        Parameters
        ----------
        iface : BLEInterface
            Interface associated with the status event payload.
        connected : bool
            Connection status flag to publish.
        publishing_thread : object | None
            Publishing-thread façade used for deferred publish scheduling.

        Returns
        -------
        None
            Publication is best-effort; failures are logged and suppressed.
        """
        publisher = BLECompatibilityEventPublisher(
            iface, publishing_thread_provider=lambda: publishing_thread
        )
        publisher._publish_connection_status_on_thread(  # noqa: SLF001
            connected=connected, publishing_thread=publishing_thread
        )

    # COMPAT_STABLE_SHIM: retained for existing callers during service migration.
    @staticmethod
    def publish_connection_status_legacy(
        iface: "BLEInterface",
        connected: bool,
        *,
        publishing_thread: object | None = None,
    ) -> None:
        """Backward-compatible alias for ``publish_connection_status``.

        Parameters
        ----------
        iface : BLEInterface
            Interface whose connection status is being published.
        connected : bool
            ``True`` for connected status and ``False`` for disconnected.
        publishing_thread : object | None
            Publishing-thread facade used to enqueue/send the legacy message.
            When ``None``, this shim resolves the active publishing thread from
            ``iface._get_publishing_thread()`` or module fallback before
            delegating.

        Returns
        -------
        None
            This compatibility alias returns ``None``.
        """
        resolved_publishing_thread = publishing_thread
        if resolved_publishing_thread is None:
            get_publishing_thread = _get_declared_callable(
                iface, "_get_publishing_thread"
            )
            if get_publishing_thread is not None:
                try:
                    resolved_publishing_thread = get_publishing_thread()
                except Exception:  # noqa: BLE001 - compatibility shim is best effort
                    logger.debug(
                        "Error resolving publishing thread via iface._get_publishing_thread for legacy status publish.",
                        exc_info=True,
                    )
            if resolved_publishing_thread is None:
                mesh_iface_module = _load_runtime_module("meshtastic.mesh_interface")
                resolved_publishing_thread = getattr(
                    mesh_iface_module, "publishingThread", None
                )
        if not BLECompatibilityEventPublisher._is_live_publishing_thread(
            resolved_publishing_thread
        ):
            mesh_iface_module = _load_runtime_module("meshtastic.mesh_interface")
            fallback_thread = getattr(mesh_iface_module, "publishingThread", None)
            resolved_publishing_thread = (
                fallback_thread
                if BLECompatibilityEventPublisher._is_live_publishing_thread(
                    fallback_thread
                )
                else None
            )
        BLECompatibilityEventService.publish_connection_status(
            iface,
            connected,
            publishing_thread=resolved_publishing_thread,
        )
