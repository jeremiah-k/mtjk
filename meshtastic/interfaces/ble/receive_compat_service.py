"""Receive compatibility shim service for BLE."""

import contextlib
import inspect
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from bleak.exc import BleakError

from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.compat_adapter import (
    _get_declared_callable,
    _get_declared_lock,
    _get_declared_member,
    _load_runtime_module,
)

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.coordination import ThreadCoordinator
    from meshtastic.interfaces.ble.interface import BLEInterface
    from meshtastic.interfaces.ble.receive_service import BLEReceiveRecoveryController


# COMPAT_STABLE_SHIM: historical receive service entrypoints retained for compatibility.
class BLEReceiveRecoveryService:
    """Service helpers for BLE receive-loop and recovery behavior."""

    @staticmethod
    def _controller_class() -> type["BLEReceiveRecoveryController"]:
        """Resolve the receive controller class lazily across the compat cycle."""
        receive_module = _load_runtime_module(
            "meshtastic.interfaces.ble.receive_service"
        )
        return cast(
            type["BLEReceiveRecoveryController"],
            receive_module.BLEReceiveRecoveryController,
        )

    @staticmethod
    def _is_controller_like(
        candidate: object, required_method: str | None = None
    ) -> bool:
        """Return whether candidate exposes the receive-controller API shape.

        Parameters
        ----------
        candidate : object
            Object to check.
        required_method : str | None
            If provided, only check for this specific method. If None, check
            for the full compatibility surface.

        Returns
        -------
        bool
            True if candidate has the required method(s).
        """
        if required_method is not None:
            return _get_declared_callable(candidate, required_method) is not None
        # Full compatibility surface check
        required_methods = (
            "handle_read_loop_disconnect",
            "_wait_for_read_trigger",
            "_snapshot_client_state",
            "_process_client_state",
            "_reset_recovery_after_stability",
            "_read_and_handle_payload",
            "_handle_payload_read",
            "_run_receive_cycle",
            "receive_from_radio_impl",
            "recover_receive_thread",
            "read_from_radio_with_retries",
            "handle_transient_read_error",
            "log_empty_read_warning",
            "_should_run_receive_loop",
            "_set_receive_wanted",
            "_handle_disconnect",
            "_start_receive_thread",
        )
        for method_name in required_methods:
            if _get_declared_callable(candidate, method_name) is None:
                return False
        return True

    @staticmethod
    def _resolve_controller_candidate(
        candidate: object,
        required_method: str | None = None,
        *,
        iface: "BLEInterface | None" = None,
    ) -> "BLEReceiveRecoveryController | None":
        """Resolve concrete controller instances from eager or lazy candidates."""
        if candidate is None:
            return None
        resolved = candidate
        should_invoke_factory = callable(resolved) and (
            inspect.isclass(resolved)
            or not BLEReceiveRecoveryService._is_controller_like(
                resolved, required_method
            )
        )
        if should_invoke_factory:
            with contextlib.suppress(
                Exception
            ):  # noqa: BLE001 - factory probe is best effort
                if inspect.isclass(resolved):
                    resolved_factory = cast(Callable[..., Any], resolved)
                    if iface is not None:
                        resolved = resolved_factory(iface)
                    else:
                        resolved = resolved_factory()
                else:
                    resolved = cast(Callable[..., Any], resolved)()
        if inspect.isclass(resolved):
            return None
        if resolved is not None and BLEReceiveRecoveryService._is_controller_like(
            resolved, required_method
        ):
            return cast("BLEReceiveRecoveryController", resolved)
        return None

    @staticmethod
    def _controller_for_shim(
        iface: "BLEInterface", required_method: str | None = None
    ) -> "BLEReceiveRecoveryController":
        """Return iface-bound receive controller, falling back to cached construction.

        Parameters
        ----------
        iface : BLEInterface
            Interface to resolve or attach the receive-recovery controller.
        required_method : str | None
            Required method name if specific method is needed by caller.

        Returns
        -------
        BLEReceiveRecoveryController
            Controller bound to ``iface``.
        """
        controller_cls = BLEReceiveRecoveryService._controller_class()
        get_controller = _get_declared_callable(
            iface, "_get_receive_recovery_controller"
        )
        if get_controller is not None:
            with contextlib.suppress(
                Exception
            ):  # noqa: BLE001 - shim resolution stays best effort
                resolved = get_controller()
                resolved_controller = (
                    BLEReceiveRecoveryService._resolve_controller_candidate(
                        resolved, required_method, iface=iface
                    )
                )
                if resolved_controller is not None:
                    return resolved_controller
        cached = _get_declared_member(iface, "_receive_recovery_controller")
        cached_controller = BLEReceiveRecoveryService._resolve_controller_candidate(
            cached,
            required_method,
            iface=iface,
        )
        if cached_controller is not None:
            return cached_controller
        controller = controller_cls(iface)
        state_lock = _get_declared_lock(iface, "_state_lock")
        if state_lock is None:
            with contextlib.suppress(
                Exception
            ):  # noqa: BLE001 - best-effort cache attach
                iface._receive_recovery_controller = controller
            return controller
        with (
            contextlib.suppress(Exception),
            state_lock,
        ):  # noqa: BLE001 - best-effort cache attach
            cached = _get_declared_member(iface, "_receive_recovery_controller")
            cached_controller = BLEReceiveRecoveryService._resolve_controller_candidate(
                cached,
                required_method,
                iface=iface,
            )
            if cached_controller is not None:
                return cached_controller
            iface._receive_recovery_controller = controller
        return controller

    @staticmethod
    def _handle_read_loop_disconnect(
        iface: "BLEInterface", error_message: str, previous_client: BLEClient
    ) -> bool:
        """Handle a read-loop disconnect and return continue/stop decision.

        Parameters
        ----------
        iface : BLEInterface
            Interface whose disconnect handling should be invoked.
        error_message : str
            Disconnect context string included in the source label.
        previous_client : BLEClient
            Client instance that triggered the disconnect path.

        Returns
        -------
        bool
            ``True`` when receive-loop processing should continue, otherwise
            ``False``.
        """
        return BLEReceiveRecoveryService._controller_for_shim(
            iface, "handle_read_loop_disconnect"
        ).handle_read_loop_disconnect(
            error_message,
            previous_client,
        )

    @staticmethod
    def _should_poll_without_notify(iface: "BLEInterface") -> bool:
        """Return whether fallback polling is allowed without notify callbacks.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing notification-configuration state.

        Returns
        -------
        bool
            ``True`` when polling without notify callbacks should proceed,
            otherwise ``False``.
        """
        notify_enabled = _get_declared_member(iface, "_fromnum_notify_enabled", False)
        return not (notify_enabled if isinstance(notify_enabled, bool) else False)

    @staticmethod
    def _coordinator_wait_for_event(
        coordinator: "ThreadCoordinator", event_name: str, timeout: float | None
    ) -> bool:
        """Wait for a coordinator event using compatibility dispatch.

        Parameters
        ----------
        coordinator : ThreadCoordinator
            Coordinator instance exposing public or underscore wait helpers.
        event_name : str
            Event name to wait on.
        timeout : float | None
            Maximum wait time in seconds. ``None`` waits indefinitely when the
            coordinator exposes ``wait_for_event``/``_wait_for_event``; when no
            wait hook is available, fallback behavior sleeps briefly using
            ``_sleep(COORDINATOR_WAIT_FALLBACK_SLEEP_SEC)`` and returns.

        Returns
        -------
        bool
            ``True`` when the event was signaled, otherwise ``False``.
        """
        return (
            BLEReceiveRecoveryService._controller_class()._coordinator_wait_for_event(
                coordinator,
                event_name,
                timeout=timeout,
            )
        )

    @staticmethod
    def _coordinator_check_and_clear_event(
        coordinator: "ThreadCoordinator", event_name: str
    ) -> bool:
        """Check and clear a coordinator event using compatibility dispatch.

        Parameters
        ----------
        coordinator : ThreadCoordinator
            Coordinator instance exposing public or underscore clear helpers.
        event_name : str
            Event name to check and clear.

        Returns
        -------
        bool
            ``True`` when the event was set before clearing, otherwise
            ``False``.
        """
        return BLEReceiveRecoveryService._controller_class()._coordinator_check_and_clear_event(
            coordinator,
            event_name,
        )

    @staticmethod
    def _coordinator_clear_event(
        coordinator: "ThreadCoordinator", event_name: str
    ) -> None:
        """Clear a coordinator event using compatibility dispatch.

        Parameters
        ----------
        coordinator : ThreadCoordinator
            Coordinator instance exposing public or underscore clear helpers.
        event_name : str
            Event name to clear.

        Returns
        -------
        None
            Returns ``None`` after best-effort event clearing.
        """
        BLEReceiveRecoveryService._controller_class()._coordinator_clear_event(
            coordinator,
            event_name,
        )

    @staticmethod
    def _wait_for_read_trigger(
        iface: "BLEInterface",
        *,
        coordinator: "ThreadCoordinator",
        wait_timeout: float,
    ) -> tuple[bool, bool]:
        """Wait for read trigger and compute fallback poll mode.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing reconnect and notification state.
        coordinator : ThreadCoordinator
            Coordinator used for event waits and clear operations.
        wait_timeout : float
            Maximum wait time for read-trigger and reconnect events.

        Returns
        -------
        tuple[bool, bool]
            ``(proceed, poll_without_notify)`` where ``proceed`` indicates
            whether the caller should continue the loop iteration.
        """
        return BLEReceiveRecoveryService._controller_for_shim(
            iface,
            "_wait_for_read_trigger",
        )._wait_for_read_trigger(
            coordinator=coordinator,
            wait_timeout=wait_timeout,
            wait_for_event=BLEReceiveRecoveryService._coordinator_wait_for_event,
            check_and_clear_event=BLEReceiveRecoveryService._coordinator_check_and_clear_event,
            clear_event=BLEReceiveRecoveryService._coordinator_clear_event,
        )

    @staticmethod
    def _snapshot_client_state(
        iface: "BLEInterface",
    ) -> tuple[BLEClient | None, bool, bool, bool]:
        """Snapshot client and gating flags needed by the read loop.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing state guarded by ``_state_lock``.

        Returns
        -------
        tuple[BLEClient | None, bool, bool, bool]
            ``(client, is_connecting, publish_pending, is_closing)``.
        """
        return BLEReceiveRecoveryService._controller_for_shim(
            iface,
            "_snapshot_client_state",
        )._snapshot_client_state()

    @staticmethod
    def _process_client_state(
        iface: "BLEInterface",
        *,
        coordinator: "ThreadCoordinator",
        wait_timeout: float,
        client: BLEClient | None,
        is_connecting: bool,
        publish_pending: bool,
        is_closing: bool,
    ) -> bool:
        """Process current client state and decide whether to break loop.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing reconnect and lifecycle helpers.
        coordinator : ThreadCoordinator
            Coordinator used for reconnect wait signaling.
        wait_timeout : float
            Wait timeout passed to coordinator event waits.
        client : BLEClient | None
            Current active BLE client snapshot.
        is_connecting : bool
            Whether connection establishment is currently in progress.
        publish_pending : bool
            Whether connect publication is pending verification.
        is_closing : bool
            Whether the interface is closing.

        Returns
        -------
        bool
            ``True`` when the caller should break out of the inner receive
            loop, otherwise ``False``.
        """
        return BLEReceiveRecoveryService._controller_for_shim(
            iface,
            "_process_client_state",
        )._process_client_state(
            coordinator=coordinator,
            wait_timeout=wait_timeout,
            client=client,
            is_connecting=is_connecting,
            publish_pending=publish_pending,
            is_closing=is_closing,
            wait_for_event=BLEReceiveRecoveryService._coordinator_wait_for_event,
        )

    @staticmethod
    def _reset_recovery_after_stability(iface: "BLEInterface") -> None:
        """Reset recovery-attempt counter after sustained stable reads.

        Parameters
        ----------
        iface : BLEInterface
            Interface maintaining receive-recovery counters.

        Returns
        -------
        None
            Returns ``None`` after best-effort counter reset.
        """
        BLEReceiveRecoveryService._controller_for_shim(
            iface,
            "_reset_recovery_after_stability",
        )._reset_recovery_after_stability()

    @staticmethod
    def _read_and_handle_payload(
        iface: "BLEInterface",
        client: BLEClient,
        *,
        poll_without_notify: bool,
    ) -> bool:
        """Read a payload iteration and dispatch packet handling.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing read/retry helpers and packet handlers.
        client : BLEClient
            Active BLE client used for characteristic reads.
        poll_without_notify : bool
            Whether the current cycle is using fallback polling mode.

        Returns
        -------
        bool
            ``True`` to continue the inner loop, ``False`` to stop.
        """
        return BLEReceiveRecoveryService._controller_for_shim(
            iface,
            "_read_and_handle_payload",
        )._read_and_handle_payload(
            client,
            poll_without_notify=poll_without_notify,
        )

    @staticmethod
    def _handle_payload_read(
        iface: "BLEInterface",
        client: BLEClient,
        *,
        poll_without_notify: bool,
    ) -> tuple[bool, bool]:
        """Handle one payload-read attempt and classify loop control flow.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing payload handlers and retry/disconnect helpers.
        client : BLEClient
            Active BLE client used for current read iteration.
        poll_without_notify : bool
            Whether the current cycle is running in polling fallback mode.

        Returns
        -------
        tuple[bool, bool]
            ``(break_inner_loop, stop_receive_loop)``.

        Raises
        ------
        SystemExit
            Propagated when process termination is requested.
        KeyboardInterrupt
            Propagated when process termination is requested.
        """
        return BLEReceiveRecoveryService._controller_for_shim(
            iface,
            "_handle_payload_read",
        )._handle_payload_read(
            client,
            poll_without_notify=poll_without_notify,
        )

    @staticmethod
    def _run_receive_cycle(
        iface: "BLEInterface",
        *,
        coordinator: "ThreadCoordinator",
        wait_timeout: float,
    ) -> bool:
        """Run receive-loop orchestration until shutdown or fatal stop.

        Parameters
        ----------
        iface : BLEInterface
            Interface whose receive loop is being executed.
        coordinator : ThreadCoordinator
            Coordinator used for trigger/reconnect event waits.
        wait_timeout : float
            Wait timeout passed to coordinator waits for each cycle.

        Returns
        -------
        bool
            ``True`` when the loop exited naturally, ``False`` when a fatal
            read path requested immediate receive-loop stop.
        """
        return BLEReceiveRecoveryService._controller_for_shim(
            iface,
            "_run_receive_cycle",
        )._run_receive_cycle(
            coordinator=coordinator,
            wait_timeout=wait_timeout,
        )

    @staticmethod
    def _receive_from_radio_impl(iface: "BLEInterface") -> None:
        """Run the BLE receive loop and dispatch decoded radio payloads.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing receive-loop state and handlers.

        Returns
        -------
        None
            Returns ``None``. Fatal receive-loop failures trigger recovery or
            close side effects before exiting.
        """
        BLEReceiveRecoveryService._controller_for_shim(
            iface,
            "receive_from_radio_impl",
        ).receive_from_radio_impl()

    @staticmethod
    def _recover_receive_thread(iface: "BLEInterface", disconnect_reason: str) -> None:
        """Handle receive-thread crash and guarded restart behavior.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing disconnect and receive-thread lifecycle helpers.
        disconnect_reason : str
            Reason label passed into disconnect handling.

        Returns
        -------
        None
            Returns ``None`` after scheduling recovery or stopping receive.
        """
        BLEReceiveRecoveryService._controller_for_shim(
            iface,
            "recover_receive_thread",
        ).recover_receive_thread(disconnect_reason)

    @staticmethod
    def _read_from_radio_with_retries(
        iface: "BLEInterface",
        client: BLEClient,
        *,
        retry_on_empty: bool = True,
    ) -> bytes | None:
        """Read non-empty payload from FROMRADIO with retry policy.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing retry policies and warning state.
        client : BLEClient
            Active BLE client used for characteristic reads.
        retry_on_empty : bool
            When ``True``, apply empty-read retry/backoff behavior.

        Returns
        -------
        bytes | None
            Non-empty payload bytes when available, otherwise ``None``.
        """
        return BLEReceiveRecoveryService._controller_for_shim(
            iface,
            "read_from_radio_with_retries",
        ).read_from_radio_with_retries(
            client,
            retry_on_empty=retry_on_empty,
        )

    @staticmethod
    def _handle_transient_read_error(
        iface: "BLEInterface", error: BleakError | BLEClient.BLEError
    ) -> None:
        """Apply transient-read retry policy and raise on exhaustion.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing transient retry counters and policy helpers.
        error : BleakError | BLEClient.BLEError
            Error that triggered retry-policy evaluation.

        Returns
        -------
        None
            Returns ``None`` when retry is scheduled.

        Raises
        ------
        BLEError
            If transient retry attempts are exhausted.
        """
        BLEReceiveRecoveryService._controller_for_shim(
            iface,
            "handle_transient_read_error",
        ).handle_transient_read_error(error)

    @staticmethod
    def _log_empty_read_warning(iface: "BLEInterface") -> None:
        """Emit throttled warning on repeated empty FROMRADIO reads.

        Parameters
        ----------
        iface : BLEInterface
            Interface storing empty-read warning cooldown state.

        Returns
        -------
        None
            Returns ``None`` after logging or suppressing warning output.
        """
        BLEReceiveRecoveryService._controller_for_shim(
            iface,
            "log_empty_read_warning",
        ).log_empty_read_warning()
