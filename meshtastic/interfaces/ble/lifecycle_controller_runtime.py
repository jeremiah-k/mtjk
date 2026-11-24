"""Top-level lifecycle controller runtime ownership for BLE."""

import inspect
from collections.abc import Callable
from typing import TYPE_CHECKING, TypeVar, cast

from bleak import BleakClient as BleakRootClient

from meshtastic.interfaces.ble.compat_adapter import _get_declared_callable
from meshtastic.interfaces.ble.lifecycle_disconnect_runtime import (
    BLEDisconnectLifecycleCoordinator,
)
from meshtastic.interfaces.ble.lifecycle_ownership_runtime import (
    BLEConnectionOwnershipLifecycleCoordinator,
)
from meshtastic.interfaces.ble.lifecycle_primitives import (
    _LifecycleErrorAccess,
    _LifecycleStateAccess,
    _OwnershipSnapshot,
)
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
        iface = self._iface
        state_access = _LifecycleStateAccess(iface)
        error_access = _LifecycleErrorAccess(iface)

        def _resolve_hook(attr_name: str, fallback: _HookT) -> _HookT:
            """Resolve a declared interface hook, falling back to the owner callable."""
            raw_hook = _get_declared_callable(iface, attr_name)
            return fallback if raw_hook is None else cast(_HookT, raw_hook)

        is_closing = _resolve_hook("_state_manager_is_closing", state_access.is_closing)
        reset_to_disconnected = _resolve_hook(
            "_state_manager_reset_to_disconnected",
            state_access.reset_to_disconnected,
        )
        current_state = _resolve_hook(
            "_state_manager_current_state",
            state_access.current_state,
        )
        transition_to_state = _resolve_hook(
            "_state_manager_transition_to",
            state_access.transition_to,
        )
        safe_cleanup = _resolve_hook(
            "_error_handler_safe_cleanup",
            error_access.safe_cleanup,
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

    def _finalize_connection_gates(
        self,
        connected_client: "BLEClient",
        connected_device_key: str | None,
        connection_alias_key: str | None,
    ) -> None:
        """Finalize gate ownership after successful connect."""
        self._connection_ownership._finalize_connection_gates(
            connected_client,
            connected_device_key,
            connection_alias_key,
        )

    def _is_owned_connected_client(self, client: "BLEClient") -> bool:
        """Return whether the bound interface still owns the provided client."""
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
