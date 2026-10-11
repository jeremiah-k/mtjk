"""Top-level lifecycle controller runtime ownership for BLE."""

import inspect
from collections.abc import Callable
from typing import TYPE_CHECKING, TypeVar, cast

from bleak import BleakClient as BleakRootClient

from meshtastic.interfaces.ble.compat_adapter import (
    _get_declared_callable,
    _load_runtime_module,
)
from meshtastic.interfaces.ble.lifecycle_compat_service import (
    _ORIGINAL_FINALIZE_CONNECTION_GATES,
    _ORIGINAL_GET_CONNECTED_CLIENT_STATUS,
    _ORIGINAL_GET_CONNECTED_CLIENT_STATUS_LOCKED,
    _ORIGINAL_IS_OWNED_CONNECTED_CLIENT,
    _ORIGINAL_VERIFY_OWNERSHIP_SNAPSHOT,
)
from meshtastic.interfaces.ble.lifecycle_disconnect_runtime import (
    BLEDisconnectLifecycleCoordinator,
)
from meshtastic.interfaces.ble.lifecycle_ownership_runtime import (
    BLEConnectionOwnershipLifecycleCoordinator,
)
from meshtastic.interfaces.ble.lifecycle_primitives import _OwnershipSnapshot
from meshtastic.interfaces.ble.lifecycle_receive_runtime import (
    BLEReceiveLifecycleCoordinator,
)
from meshtastic.interfaces.ble.lifecycle_shutdown_runtime import (
    BLEShutdownLifecycleCoordinator,
)
from meshtastic.interfaces.ble.management_state import (
    _BLEManagementStatePort,
    _management_state_for,
)
from meshtastic.interfaces.ble.session_state import _session_state_for
from meshtastic.interfaces.ble.state import ConnectionState

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.client import BLEClient
    from meshtastic.interfaces.ble.interface import BLEInterface
    from meshtastic.interfaces.ble.lifecycle_compat_service import BLELifecycleService

_HookT = TypeVar("_HookT", bound=Callable[..., object])


def _callable_accepts_timeout_kwarg(func: Callable[..., object]) -> bool:
    """Return whether ``func`` can receive a ``timeout`` keyword."""
    try:
        parameters = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return True
    timeout_parameter = parameters.get("timeout")
    if timeout_parameter is not None:
        return timeout_parameter.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


class BLELifecycleController:
    """Instance-bound collaborator for BLE lifecycle responsibilities."""

    def __init__(
        self,
        iface: "BLEInterface",
        *,
        client_closer: Callable[..., None] | None = None,
        management_state: _BLEManagementStatePort | None = None,
    ) -> None:
        """Bind lifecycle orchestration helpers to a specific interface.

        Parameters
        ----------
        iface : BLEInterface
            Interface instance whose lifecycle collaborators are owned by this
            controller.
        client_closer : Callable[..., None] | None, optional
            Explicit client-close capability used by the disconnect coordinator.
            When omitted, compatibility resolution falls back to the bound
            interface.
        management_state : _BLEManagementStatePort | None, optional
            Shared management-operation state. When omitted, resolve the
            interface-owned state or use the legacy adapter.

        Returns
        -------
        None
            Initializes bound controller collaborators.
        """
        self._iface = iface
        session = _session_state_for(iface)
        management = _management_state_for(iface, management_state)
        self._receive = BLEReceiveLifecycleCoordinator(iface, session_state=session)
        self._disconnect = BLEDisconnectLifecycleCoordinator(
            iface,
            session_state=session,
            client_closer=client_closer,
        )
        self._connection_ownership = BLEConnectionOwnershipLifecycleCoordinator(
            iface, session_state=session
        )
        self._shutdown = BLEShutdownLifecycleCoordinator(
            iface,
            session_state=session,
            management_state=management,
        )

    def _set_receive_wanted(self, *, want_receive: bool) -> None:
        """Request or clear the receive loop on the bound interface."""
        self._receive.set_receive_wanted(want_receive=want_receive)

    def _should_run_receive_loop(self) -> bool:
        """Return whether receive loop should continue for the bound interface."""
        return self._receive.should_run_receive_loop()

    def _start_receive_thread(self, *, name: str, reset_recovery: bool = True) -> None:
        """Start receive thread for the bound interface."""
        self._receive.start_receive_thread(
            name=name,
            reset_recovery=reset_recovery,
        )

    def _handle_disconnect(
        self,
        source: str,
        *,
        client: "BLEClient | None" = None,
        bleak_client: BleakRootClient | None = None,
    ) -> bool:
        """Handle disconnect sequence for the bound interface."""
        return self._disconnect.handle_disconnect(
            source,
            client=client,
            bleak_client=bleak_client,
        )

    def _on_ble_disconnect(self, client: BleakRootClient) -> None:
        """Handle Bleak disconnect callback for the bound interface."""
        self._disconnect.on_ble_disconnect(client)

    def _schedule_auto_reconnect(self) -> None:
        """Schedule reconnect worker for the bound interface."""
        self._disconnect.schedule_auto_reconnect()

    def _verify_and_publish_connected(
        self,
        connected_client: "BLEClient",
        connected_device_key: str | None,
        connection_alias_key: str | None,
        *,
        restore_address: str | None,
        restore_last_connection_request: str | None,
        verify_ownership_snapshot: (
            Callable[["BLEClient", str | None, str | None], _OwnershipSnapshot] | None
        ) = None,
        get_connected_client_status_locked: (
            Callable[["BLEClient"], tuple[bool, bool]] | None
        ) = None,
    ) -> None:
        """Verify ownership and publish connected side effects."""
        if self._uses_compat_connection_status_overrides():
            lifecycle_service_mod = _load_runtime_module(
                "meshtastic.interfaces.ble.lifecycle_service"
            )

            if (
                verify_ownership_snapshot is None
                and get_connected_client_status_locked is None
            ):

                def _compat_verify_snapshot(
                    client: "BLEClient",
                    device_key: str | None,
                    alias_key: str | None,
                ) -> _OwnershipSnapshot:
                    service = cast(
                        "type[BLELifecycleService]",
                        lifecycle_service_mod.BLELifecycleService,
                    )
                    return service._verify_ownership_snapshot(
                        self._iface,
                        client,
                        device_key,
                        alias_key,
                    )

                verify_ownership_snapshot = _compat_verify_snapshot
            if get_connected_client_status_locked is None:

                def _compat_get_status_locked(
                    client: "BLEClient",
                ) -> tuple[bool, bool]:
                    service = cast(
                        "type[BLELifecycleService]",
                        lifecycle_service_mod.BLELifecycleService,
                    )
                    return service._get_connected_client_status_locked(
                        self._iface,
                        client,
                    )

                get_connected_client_status_locked = _compat_get_status_locked
        self._connection_ownership._verify_and_publish_connected(
            connected_client,
            connected_device_key,
            connection_alias_key,
            restore_address=restore_address,
            restore_last_connection_request=restore_last_connection_request,
            verify_ownership_snapshot=verify_ownership_snapshot,
            get_connected_client_status_locked=get_connected_client_status_locked,
        )

    def _emit_verified_connection_side_effects(
        self, connected_client: "BLEClient"
    ) -> None:
        """Emit verified-connection side effects for the bound interface."""
        self._connection_ownership._emit_verified_connection_side_effects(
            connected_client
        )

    def _discard_invalidated_connected_client(
        self,
        client: "BLEClient",
        *,
        restore_address: str | None = None,
        restore_last_connection_request: str | None = None,
    ) -> None:
        """Discard stale connect result for the bound interface."""
        lifecycle_service_mod = _load_runtime_module(
            "meshtastic.interfaces.ble.lifecycle_service"
        )

        iface = self._iface

        def _resolve_hook(
            attr_name: str,
            fallback_factory: Callable[[], _HookT],
        ) -> _HookT:
            raw_hook = _get_declared_callable(iface, attr_name)
            return fallback_factory() if raw_hook is None else cast(_HookT, raw_hook)

        def _fallback_is_closing() -> Callable[[], bool]:
            def is_closing() -> bool:
                service = cast(
                    "type[BLELifecycleService]",
                    lifecycle_service_mod.BLELifecycleService,
                )
                return service._state_manager_is_closing(iface)

            return is_closing

        def _fallback_reset_to_disconnected() -> Callable[[], bool]:
            def reset_to_disconnected() -> bool:
                service = cast(
                    "type[BLELifecycleService]",
                    lifecycle_service_mod.BLELifecycleService,
                )
                return service._state_manager_reset_to_disconnected(iface)

            return reset_to_disconnected

        def _fallback_current_state() -> Callable[[], ConnectionState]:
            def current_state() -> ConnectionState:
                service = cast(
                    "type[BLELifecycleService]",
                    lifecycle_service_mod.BLELifecycleService,
                )
                return service._state_manager_current_state(iface)

            return current_state

        def _fallback_transition_to_state() -> Callable[[ConnectionState], bool]:
            def transition_to_state(state: ConnectionState) -> bool:
                service = cast(
                    "type[BLELifecycleService]",
                    lifecycle_service_mod.BLELifecycleService,
                )
                return service._state_manager_transition_to(
                    iface,
                    state,
                )

            return transition_to_state

        def _fallback_safe_cleanup() -> Callable[[Callable[[], object], str], None]:
            def safe_cleanup(
                cleanup: Callable[[], object], operation_name: str
            ) -> None:
                lifecycle_service_mod.BLELifecycleService._error_handler_safe_cleanup(  # noqa: E501
                    iface,
                    cleanup,
                    operation_name,
                )

            return safe_cleanup

        is_closing = _resolve_hook("_state_manager_is_closing", _fallback_is_closing)
        reset_to_disconnected = _resolve_hook(
            "_state_manager_reset_to_disconnected",
            _fallback_reset_to_disconnected,
        )
        current_state = _resolve_hook(
            "_state_manager_current_state",
            _fallback_current_state,
        )
        transition_to_state = _resolve_hook(
            "_state_manager_transition_to",
            _fallback_transition_to_state,
        )
        safe_cleanup = _resolve_hook(
            "_error_handler_safe_cleanup",
            _fallback_safe_cleanup,
        )

        self._connection_ownership._discard_invalidated_connected_client(
            client,
            restore_address=restore_address,
            restore_last_connection_request=restore_last_connection_request,
            is_closing_getter=is_closing,
            reset_to_disconnected=reset_to_disconnected,
            current_state_getter=current_state,
            transition_to_disconnected=lambda: transition_to_state(
                ConnectionState.DISCONNECTED
            ),
            safe_cleanup=safe_cleanup,
        )

    def _uses_compat_connection_status_overrides(self) -> bool:
        """Return whether lifecycle service status helpers were monkeypatched."""
        lifecycle_service_mod = _load_runtime_module(
            "meshtastic.interfaces.ble.lifecycle_service"
        )

        service_get_status = (
            lifecycle_service_mod.BLELifecycleService._get_connected_client_status
        )
        service_get_status_locked = (
            lifecycle_service_mod.BLELifecycleService._get_connected_client_status_locked
        )
        service_verify_snapshot = (
            lifecycle_service_mod.BLELifecycleService._verify_ownership_snapshot
        )
        service_finalize_connection_gates = (
            lifecycle_service_mod.BLELifecycleService._finalize_connection_gates
        )
        service_is_owned_connected_client = (
            lifecycle_service_mod.BLELifecycleService._is_owned_connected_client
        )
        return (
            service_get_status is not _ORIGINAL_GET_CONNECTED_CLIENT_STATUS
            or service_get_status_locked
            is not _ORIGINAL_GET_CONNECTED_CLIENT_STATUS_LOCKED
            or service_verify_snapshot is not _ORIGINAL_VERIFY_OWNERSHIP_SNAPSHOT
            or service_finalize_connection_gates
            is not _ORIGINAL_FINALIZE_CONNECTION_GATES
            or service_is_owned_connected_client
            is not _ORIGINAL_IS_OWNED_CONNECTED_CLIENT
        )

    def _finalize_connection_gates(
        self,
        connected_client: "BLEClient",
        connected_device_key: str | None,
        connection_alias_key: str | None,
    ) -> None:
        """Finalize gate ownership after successful connect."""
        if self._uses_compat_connection_status_overrides():
            lifecycle_service_mod = _load_runtime_module(
                "meshtastic.interfaces.ble.lifecycle_service"
            )

            lifecycle_service_mod.BLELifecycleService._finalize_connection_gates(
                self._iface,
                connected_client,
                connected_device_key,
                connection_alias_key,
            )
            return
        self._connection_ownership._finalize_connection_gates(
            connected_client,
            connected_device_key,
            connection_alias_key,
        )

    def _is_owned_connected_client(self, client: "BLEClient") -> bool:
        """Return whether the bound interface still owns the provided client."""
        if self._uses_compat_connection_status_overrides():
            lifecycle_service_mod = _load_runtime_module(
                "meshtastic.interfaces.ble.lifecycle_service"
            )

            service = cast(
                "type[BLELifecycleService]",
                lifecycle_service_mod.BLELifecycleService,
            )
            return service._is_owned_connected_client(self._iface, client)
        return self._connection_ownership._is_owned_connected_client(client)

    def _has_ever_connected_session(self) -> bool:
        """Return whether this interface has published at least one connection."""
        return self._connection_ownership._has_ever_connected_session()

    def _is_connection_closing(self) -> bool:
        """Return whether shutdown is in progress for the bound interface."""
        return self._shutdown.is_connection_closing()

    def _close(
        self,
        *,
        management_shutdown_wait_timeout: float,
        management_wait_poll_seconds: float,
        timeout: float | None = None,
    ) -> None:
        """Close the bound interface and lifecycle resources."""
        shutdown_close = self._shutdown.close
        if _callable_accepts_timeout_kwarg(shutdown_close):
            shutdown_close(
                management_shutdown_wait_timeout=management_shutdown_wait_timeout,
                management_wait_poll_seconds=management_wait_poll_seconds,
                timeout=timeout,
            )
        else:
            shutdown_close(
                management_shutdown_wait_timeout=management_shutdown_wait_timeout,
                management_wait_poll_seconds=management_wait_poll_seconds,
            )

    def _disconnect_and_close_client(
        self,
        client: "BLEClient",
        *,
        timeout: float | None = None,
    ) -> None:
        """Disconnect and close the provided client for the bound interface."""
        disconnect_and_close = self._disconnect.disconnect_and_close_client
        if _callable_accepts_timeout_kwarg(disconnect_and_close):
            disconnect_and_close(client, timeout=timeout)
        else:
            disconnect_and_close(client)
