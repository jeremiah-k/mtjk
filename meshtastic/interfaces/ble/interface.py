"""Main BLE interface class.

Concurrency model summary
-------------------------
`BLEInterface` intentionally centralizes connection/disconnect, receive-loop, and
recovery control because these paths share state transitions and lock-sensitive
invariants.

When multiple locks are required, acquire in this order:
1. per-address locks from gating (`_addr_lock_context`)
2. `_connect_lock`
3. `_management_lock`
4. `_state_lock`
5. `_disconnect_lock`

`_REGISTRY_LOCK` is only held for short registry updates and must never be held
while waiting to acquire a per-address lock.

Threading model summary
-----------------------
- Receive thread: owns inbound packet reads and disconnect escalation.
- Event/reconnect workers: coordinate wakeups and policy-driven reconnect.
- Main thread: issues lifecycle operations (`connect()`, `close()`), which are
  idempotent and synchronized through the shared state manager lock.
"""

# BLEInterface is the public compatibility facade and composition root for extracted BLE runtimes.
# pylint: disable=too-many-lines

import atexit
import contextlib
import math
import numbers
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from threading import Event
from typing import IO, Any, NoReturn, TypeVar, cast

from bleak import BleakClient as BleakRootClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakDBusError, BleakError

import meshtastic.interfaces.ble.constants as _ble_constants
from meshtastic._publishing import publishing_thread as publishingThread
from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.compat_adapter import (
    _get_declared_callable,
    _get_declared_lock,
    _get_declared_member,
    _iter_declared_members,
    _resolve_declared_callable,
)
from meshtastic.interfaces.ble.compatibility_service import (
    BLECompatibilityEventPublisher,
)
from meshtastic.interfaces.ble.connect_transaction import (
    _BLEClientAdoption,
    _BLEConnectStateSnapshot,
)
from meshtastic.interfaces.ble.connection import (
    ClientManager,
    ConnectionOrchestrator,
    ConnectionValidator,
)
from meshtastic.interfaces.ble.constants import (
    BLECLIENT_MANAGEMENT_AWAIT_TIMEOUT,
    CONNECTION_ERROR_EMPTY_ADDRESS,
    CONNECTION_ERROR_LOST_OWNERSHIP,
    ERROR_ADDRESS_RESOLUTION_FAILED,
    ERROR_CONNECTION_FAILED,
    ERROR_CONNECTION_SUPPRESSED,
    ERROR_INTERFACE_CLOSING,
    ERROR_MANAGEMENT_CONNECTING,
    ERROR_MULTIPLE_DEVICES,
    ERROR_MULTIPLE_DEVICES_DISCOVERY,
    ERROR_NO_CLIENT_ESTABLISHED,
    ERROR_NO_PERIPHERALS_FOUND,
    ERROR_TIMEOUT,
    ERROR_TRUST_ADDRESS_NOT_RESOLVED,
    ERROR_WRITING_BLE,
    GATT_IO_TIMEOUT,
    READ_TRIGGER_EVENT,
    RECONNECTED_EVENT,
    SERVICE_UUID,
    TORADIO_UUID,
    UNREACHABLE_ADDRESSED_DEVICES_MSG,
    BLEConfig,
    logger,
)
from meshtastic.interfaces.ble.coordination import ThreadCoordinator
from meshtastic.interfaces.ble.discovery import (
    DiscoveryManager,
    _looks_like_ble_address,
    _parse_scan_response,
)
from meshtastic.interfaces.ble.errors import (
    BLEConnectionSuppressedError,
    BLEDeviceNotFoundError,
    BLEDiscoveryError,
    BLEErrorHandler,
    MeshtasticBLEError,
)
from meshtastic.interfaces.ble.failure_policy import (
    _BLEFailureDisposition,
    _log_ble_failure,
)
from meshtastic.interfaces.ble.gating import (
    _addr_key,
    _addr_lock_context,
    _clear_connecting,
    _clear_connecting_for_owner,
    _is_currently_connected_elsewhere,
    _mark_connected,
    _mark_connecting,
    _mark_disconnected,
)
from meshtastic.interfaces.ble.lifecycle_primitives import _LifecycleStateAccess
from meshtastic.interfaces.ble.lifecycle_service import BLELifecycleController
from meshtastic.interfaces.ble.management_service import (
    BLUETOOTHCTL_TRUST_TIMEOUT_SECONDS as _BLUETOOTHCTL_TRUST_TIMEOUT_SECONDS,
)
from meshtastic.interfaces.ble.management_service import BLEManagementCommandHandler
from meshtastic.interfaces.ble.management_state import (
    BLEManagementState,
    _ManagementConditionPort,
)
from meshtastic.interfaces.ble.notifications import (
    BLENotificationDispatcher,
    NotificationManager,
    _NotificationSessionSnapshot,
)
from meshtastic.interfaces.ble.policies import RetryPolicy
from meshtastic.interfaces.ble.ports import _LockPort
from meshtastic.interfaces.ble.receive_service import BLEReceiveRecoveryController
from meshtastic.interfaces.ble.reconnection import ReconnectScheduler
from meshtastic.interfaces.ble.session_state import (
    BLESessionState,
    _BLESessionStateCompatMixin,
)
from meshtastic.interfaces.ble.state import (
    BLEStateManager,
    ConnectionState,
    _is_canonical_state_manager,
)
from meshtastic.interfaces.ble.utils import (
    _is_unexpected_keyword_error,
    _sleep,
    sanitize_address,
    with_timeout,
)
from meshtastic.mesh_interface import MeshInterface
from meshtastic.protobuf import mesh_pb2

T = TypeVar("T")

MALFORMED_NOTIFICATION_THRESHOLD: int = _ble_constants.MALFORMED_NOTIFICATION_THRESHOLD
_MANAGEMENT_SHUTDOWN_WAIT_TIMEOUT_SECONDS: float = 30.0
_MANAGEMENT_CONNECT_WAIT_POLL_SECONDS: float = 0.5
_MANAGEMENT_CONNECT_WAIT_TIMEOUT_SECONDS: float = 30.0
_TRUST_COMMAND_OUTPUT_MAX_CHARS: int = 200
_TRUST_HEX_BLOB_RE = re.compile(r"\b[0-9A-Fa-f]{16,}\b")
_TRUST_TOKEN_RE = re.compile(r"\b[A-Za-z0-9+/=_-]{40,}\b")
ERROR_PAIR_ON_CONNECT_BOOL: str = "pair_on_connect must be a bool."
ERROR_PAIR_BOOL: str = "pair must be a bool when provided."
ERROR_CLOSE_TIMEOUT: str = (
    "close() timeout must be a finite non-negative number of seconds."
)
ERROR_RETRY_POLICY_MISSING_SHOULD_RETRY: str = (
    "Retry policy missing should_retry/_should_retry"
)
ERROR_RETRY_POLICY_MISSING_GET_DELAY: str = "Retry policy missing get_delay/_get_delay"
ERROR_DISCOVERY_MANAGER_MISSING_DISCOVER_DEVICES: str = (
    "Discovery manager is missing discover_devices/_discover_devices"
)
ERROR_STATE_MANAGER_MISSING_BOOL_IS_CONNECTED: str = (
    "State manager is missing is_connected/_is_connected boolean members"
)
ERROR_CONNECTION_VALIDATOR_MISSING_CHECK_EXISTING_CLIENT: str = (
    "Connection validator is missing check_existing_client/_check_existing_client"
)
ERROR_CONNECTION_ORCHESTRATOR_MISSING_ESTABLISH_CONNECTION: str = (
    "Connection orchestrator is missing establish_connection/_establish_connection"
)
ERROR_CLIENT_MANAGER_MISSING_SAFE_CLOSE_CLIENT: str = (
    "Client manager is missing safe_close_client/_safe_close_client"
)
ERROR_CLIENT_MANAGER_MISSING_UPDATE_CLIENT_REFERENCE: str = (
    "Client manager is missing update_client_reference/_update_client_reference"
)


# The facade owns runtime collaborators; mutable session state itself lives in BLESessionState.
class BLEInterface(  # pylint: disable=too-many-instance-attributes
    _BLESessionStateCompatMixin, MeshInterface
):
    """MeshInterface using BLE to connect to Meshtastic devices.

    This class provides a complete BLE interface for Meshtastic communication,
    handling connection management, packet transmission/reception, error recovery,
    and automatic reconnection. It extends MeshInterface with BLE-specific
    functionality while maintaining API compatibility.

    Notes
    -----
    - Key features:
      - Automatic connection management and recovery.
      - Thread-safe operations with centralized thread coordination.
      - Unified error handling and logging.
      - Configurable timeouts and retry behavior.
      - Support for both legacy and modern BLE characteristics.
      - Comprehensive state management.
    - Internal architecture:
      - `BLEStateManager`: Centralized connection state machine with shared locking.
      - `ThreadCoordinator`: Thread/event lifecycle and coordination utilities.
      - `BLEErrorHandler`: Standardized error handling patterns.
      - `NotificationManager`: Tracks active notifications for reconnect-safe resubscription.
      - `DiscoveryManager`: Scans for Meshtastic BLE devices with address normalization.
      - `ConnectionValidator`: Enforces connection preconditions.
      - `ClientManager`: Owns BLEClient lifecycle and cleanup operations.
      - `ConnectionOrchestrator`: Coordinates connection establishment.
      - `ReconnectScheduler` / `ReconnectWorker`: Policy-driven reconnect attempts.

    Note: This interface requires appropriate Bluetooth permissions and may
    need platform-specific setup for BLE operations.
    """

    BLEError = MeshtasticBLEError

    def __init__(
        self,
        address: str | None = None,
        noProto: bool = False,
        debugOut: IO[str] | Callable[[str], Any] | None = None,
        noNodes: bool = False,
        timeout: float = 300.0,
        *,
        auto_reconnect: bool = False,
        pair_on_connect: bool = False,
    ) -> None:
        """Initialize the BLEInterface, configure background threads, and attempt an initial connection to a Meshtastic BLE device.

        If an address or device name is provided, attempt to connect to that device; otherwise discovery may select any
        available Meshtastic device. Registers an atexit handler to ensure an orderly disconnect on process exit.

        Parameters
        ----------
        address : str | None
            BLE address or device name to connect to; if None, discovery may select any compatible device.
        auto_reconnect : bool
            If True, schedule automatic reconnection after unexpected disconnects. (Default value = False)
        pair_on_connect : bool
            If True, request BLE pairing as part of each connect attempt. This maps
            to Bleak's `BleakClient(..., pair=True)` behavior. (Default value = False)
        noProto : bool
            If True, skip protobuf protocol initialization. (Default value = False)
        debugOut : IO[str] | Callable[[str], Any] | None
            Optional stream or callable for debug output; if None, uses sys.stderr. (Default value = None)
        noNodes : bool
            If True, skip node database initialization. (Default value = False)
        timeout : float
            Connection timeout in seconds. (Default value = 300.0)

        Raises
        ------
        BLEInterface.BLEError
            If the initial connection or configuration fails.
        """

        # Thread safety and state management
        # Unified state-based lock system replacing multiple locks and boolean flags
        #
        # Lock Ordering (to prevent deadlocks):
        #     - Never hold _REGISTRY_LOCK while waiting on a per-address lock.
        #     - For interface-level coordination, acquire in this order:
        #       1. Per-address locks (_ADDR_LOCKS in gating.py, via _addr_lock_context)
        #       2. Interface connect lock (_connect_lock)
        #       3. Interface management lock (_management_lock)
        #       4. Interface state lock (_state_lock)
        #       5. Interface disconnect lock (_disconnect_lock)
        #
        # EXCEPTION: In _handle_disconnect, _disconnect_lock is acquired FIRST
        # (non-blocking) before _state_lock. This intentional inversion enables
        # early-return optimization for concurrent disconnect callbacks without
        # blocking on state_lock. Safe because _disconnect_lock uses non-blocking
        # acquire. If other code needs both locks, it acquires state_lock first,
        # then disconnect_lock, and _handle_disconnect's non-blocking acquire will
        # simply fail and return early.
        #
        # _connect_lock Purpose:
        #     Serializes connection attempts within a single interface instance.
        #     While _addr_lock_context provides process-wide serialization for the
        #     same address, _connect_lock ensures that within this interface,
        #     only one connection attempt can be in the critical section at a time.
        #     This prevents race conditions when checking existing_client and
        #     managing the connection state machine.
        self._state_manager = BLEStateManager()  # Centralized connection state
        state_lock = self._state_manager.lock
        self._session_state = BLESessionState(lock=state_lock)
        self._connect_lock = threading.RLock()  # Serializes connection attempts
        self._management_state = BLEManagementState()
        self._disconnect_lock = threading.Lock()  # Serializes disconnect handling
        self._exit_handler: Any | None = None
        self.address = address
        self._last_connection_request: str | None = sanitize_address(address)
        self.auto_reconnect = auto_reconnect
        if not isinstance(pair_on_connect, bool):
            raise self.BLEError(ERROR_PAIR_ON_CONNECT_BOOL)
        self.pair_on_connect = pair_on_connect

        # Error handling infrastructure
        self.error_handler = BLEErrorHandler()

        # Thread management infrastructure
        self.thread_coordinator = ThreadCoordinator()
        self._notification_manager = NotificationManager()
        self._notification_dispatcher = self._create_notification_dispatcher()
        self._discovery_manager: DiscoveryManager | None = DiscoveryManager()
        self._connection_validator = ConnectionValidator(
            self._state_manager, state_lock, self.BLEError
        )
        self._client_manager = ClientManager(
            self._state_manager,
            state_lock,
            self.thread_coordinator,
            self.error_handler,
        )
        self._connection_orchestrator = ConnectionOrchestrator(
            self,
            self._connection_validator,
            self._client_manager,
            self._discovery_manager,
            self._state_manager,
            state_lock,
            self.thread_coordinator,
        )
        self._reconnect_scheduler = ReconnectScheduler(
            self._state_manager,
            state_lock,
            self.thread_coordinator,
            self,
        )
        self._lifecycle_controller = BLELifecycleController(
            self,
            client_closer=self._client_manager_safe_close_client,
            management_state=self._get_management_state(),
        )
        self._receive_recovery_controller = BLEReceiveRecoveryController(
            self, session_state=self._session_state
        )
        self._compatibility_publisher = BLECompatibilityEventPublisher(
            self,
            publishing_thread_provider=self._get_publishing_thread,
        )

        # Lazily recreatable collaborator: management flows may clear it to
        # None to force re-creation through _get_management_command_handler.
        self._management_command_handler: BLEManagementCommandHandler | None = (
            BLEManagementCommandHandler(
                self,
                ble_client_factory=BLEClient,
                connected_elsewhere=self._connected_elsewhere_late_bound,
                session_state=self._session_state,
                management_state=self._get_management_state(),
                connect_lock=self._connect_lock,
                client_closer=self._client_manager_safe_close_client,
            )
        )

        # Event coordination for reconnection and read operations
        self._read_trigger = self.thread_coordinator._create_event(
            READ_TRIGGER_EVENT
        )  # Signals when data is available to read
        self._reconnected_event = self.thread_coordinator._create_event(
            RECONNECTED_EVENT
        )  # Signals when reconnection occurred
        self._shutdown_event = self.thread_coordinator._create_event("shutdown_event")
        # Whether FROMNUM notifications were successfully registered for the
        # active connection. When false, receive loop falls back to periodic
        # FROMRADIO polling.
        # Monotonic session counter used to suppress stale disconnect side effects.
        # Recovery throttling to prevent tight crash→spawn loops

        # Initialize parent interface
        super().__init__(
            debugOut=debugOut, noProto=noProto, noNodes=noNodes, timeout=timeout
        )

        # Initialize retry counter for transient read errors
        # Policies are immutable presets; cache instances to avoid churn in hot loops.
        self._empty_read_policy = RetryPolicy._empty_read()
        self._transient_read_policy = RetryPolicy._transient_error()

        self.client: BLEClient | None = None

        # Start background receive thread for inbound packet processing
        logger.debug("Threads starting")
        self._start_receive_thread(name="BLEReceive")
        logger.debug("Threads running")
        try:
            logger.debug("BLE connecting to: %s", address if address else "any")
            self.connect(address, connect_timeout=timeout)
            logger.debug("BLE connected")

            logger.debug("Mesh configure starting")
            self._start_config()
            if not self.noProto:
                self._wait_connected(timeout=timeout)
                self.waitForConfig()

            # FROMNUM notification is set in _register_notifications

            # We MUST run atexit (if we can) because otherwise (at least on linux) the BLE device is not disconnected
            # and future connection attempts will fail.  (BlueZ kinda sucks)
            # Note: the on disconnected callback will call our self.close which will make us nicely wait for threads to exit
            self._exit_handler = atexit.register(self.close)
        except (SystemExit, KeyboardInterrupt):  # pylint: disable=W0706
            self.close()
            raise
        except MeshInterface.MeshInterfaceError:
            # BLEInterface.BLEError and any other MeshInterfaceError subclass raised
            # by connect() (e.g., ERROR_CONNECTION_SUPPRESSED) need cleanup before
            # propagating.  Re-raise without wrapping to preserve the original message.
            self.close()
            raise
        except (BleakError, BLEClient.BLEError, OSError, RuntimeError) as e:
            self.close()
            raise BLEInterface.BLEError(ERROR_CONNECTION_FAILED.format(e)) from e

    def __repr__(self) -> str:
        """Compact textual representation of the BLEInterface showing its address and any active non-default flags.

        Includes the `address`, `debugOut` when set, and the boolean flags
        `noProto`, `noNodes`, `auto_reconnect`, and `pair_on_connect` only when
        they differ from their defaults.

        Returns
        -------
        str
            String representation like "BLEInterface(address='AA:BB:CC:DD:EE:FF', debugOut='...', noProto=True)".
        """
        parts = [f"address={self.address!r}"]
        if self.debugOut is not None:
            parts.append(f"debugOut={self.debugOut!r}")
        if self.noProto:
            parts.append("noProto=True")
        if self.noNodes:
            parts.append("noNodes=True")
        if self.auto_reconnect:
            parts.append("auto_reconnect=True")
        if self.pair_on_connect:
            parts.append("pair_on_connect=True")
        return f"BLEInterface({', '.join(parts)})"

    def _set_receive_wanted(self, want_receive: bool) -> None:
        """Request or clear running of the background receive loop and record that intent under the interface state lock.

        Parameters
        ----------
        want_receive : bool
            True to request the receive loop to run, False to stop it.
        """
        lifecycle_controller = self._get_lifecycle_controller()
        set_receive_wanted = _resolve_declared_callable(
            lifecycle_controller, "_set_receive_wanted", "set_receive_wanted"
        )
        if set_receive_wanted is not None:
            set_receive_wanted(want_receive=want_receive)

    def _should_run_receive_loop(self) -> bool:
        """Return whether the receive loop should run.

        Returns
        -------
        bool
            `True` if the receive loop is desired and the interface is not closed, `False` otherwise.
        """
        lifecycle_controller = self._get_lifecycle_controller()
        should_run_receive_loop = _resolve_declared_callable(
            lifecycle_controller,
            "_should_run_receive_loop",
            "should_run_receive_loop",
        )
        if should_run_receive_loop is not None:
            result = should_run_receive_loop()
            return result if isinstance(result, bool) else False
        return False

    def _start_receive_thread(self, *, name: str, reset_recovery: bool = True) -> None:
        """Create and start the background receive thread, updating `_receiveThread`.

        Parameters
        ----------
        name : str
            Thread name to assign (for diagnostics/logging).
        reset_recovery : bool
            If True, reset the recovery attempt counter after
            successful thread start; if False, preserve the counter for recovery
            backoff tracking. Defaults to True.
        """
        lifecycle_controller = self._get_lifecycle_controller()
        start_receive_thread = _resolve_declared_callable(
            lifecycle_controller, "_start_receive_thread", "start_receive_thread"
        )
        if start_receive_thread is not None:
            start_receive_thread(
                name=name,
                reset_recovery=reset_recovery,
            )

    @staticmethod
    def _sorted_address_keys(*keys: str | None) -> list[str]:
        """Return a deterministically ordered list of unique, non-empty address keys.

        Parameters
        ----------
        *keys : str | None
            One or more address key strings; `None` or empty values are ignored.

        Returns
        -------
        list[str]
            Unique, non-empty address keys sorted in deterministic order.
        """
        return sorted({key for key in keys if key})

    def _lock_ordered_address_keys(
        self, ordered_keys: list[str]
    ) -> contextlib.ExitStack:
        """Acquire per-address locks in a deterministic order and return an ExitStack that holds them.

        The returned ExitStack contains the acquired per-address lock contexts; keep the stack open (or use it as a
        context manager) for the duration the locks are required so they remain held and are released when the stack is
        closed.

        Parameters
        ----------
        ordered_keys : list[str]
            Address keys in the exact order locks should be acquired.

        Returns
        -------
        contextlib.ExitStack
            An active ExitStack with the acquired address-lock contexts.

        Raises
        ------
        RuntimeError
            If acquiring any of the address locks fails.
        """
        stack = contextlib.ExitStack()
        try:
            for key in ordered_keys:
                addr_lock = stack.enter_context(_addr_lock_context(key))
                stack.enter_context(addr_lock)
        except BaseException:
            stack.close()
            raise
        return stack

    def _mark_address_keys_connected(self, *keys: str | None) -> None:
        """Mark the given address registry keys as connected for this interface.

        Ignores None or empty strings; for each non-empty key the registry is updated to record this interface as the owner.

        Parameters
        ----------
        *keys : str | None
            One or more address registry keys to mark as connected.
        """
        ordered_keys = self._sorted_address_keys(*keys)
        if not ordered_keys:
            return
        # Do not hold _REGISTRY_LOCK while waiting on per-address locks.
        # _mark_connected() acquires _REGISTRY_LOCK internally after the
        # address locks are held, which avoids registry<->address deadlocks
        # with concurrent connect/disconnect paths.
        with self._lock_ordered_address_keys(ordered_keys):
            for key in ordered_keys:
                _mark_connected(key, owner=self)

    def _mark_address_keys_connecting(self, *keys: str | None) -> None:
        """Record provisional address claims for an in-flight connect result."""
        for key in self._sorted_address_keys(*keys):
            _mark_connecting(key, owner=self)

    def _clear_address_keys_connecting(self, *keys: str | None) -> None:
        """Clear provisional address claims for an in-flight connect result."""
        for key in self._sorted_address_keys(*keys):
            _clear_connecting(key, owner=self)

    def _mark_address_keys_disconnected(self, *keys: str | None) -> None:
        """Mark one or more address registry keys as disconnected.

        Ignores None or empty keys. Updates the global address registry so the interface is no longer recorded as the
        owner of each provided key. Registry updates use atomic owner checks without waiting for another
        interface's per-address connection reservation.

        Parameters
        ----------
        *keys : str | None
            One or more address key strings to mark disconnected; None or empty values are skipped.
        """
        ordered_keys = self._sorted_address_keys(*keys)
        if not ordered_keys:
            return
        # The registry removal is atomic and owner-checked. Waiting for a
        # per-address reservation here can deadlock cancellation of a caller
        # that never acquired that reservation or owned its connection.
        for key in ordered_keys:
            _mark_disconnected(key, owner=self)

    def _handle_disconnect(
        self,
        source: str,
        client: BLEClient | None = None,
        bleak_client: BleakRootClient | None = None,
    ) -> bool:
        """Handle BLE disconnect events via lifecycle-service delegation.

        Parameters
        ----------
        source : str
            Disconnect-source label used for logging and diagnostics.
        client : BLEClient | None
            Optional client instance associated with the disconnect callback.
        bleak_client : BleakRootClient | None
            Optional raw Bleak client instance associated with the callback.

        Returns
        -------
        bool
            ``True`` when receive-loop processing should continue, otherwise
            ``False``.

        Raises
        ------
        AttributeError
            Intentionally propagated when delegated lifecycle-service helpers
            are unavailable on compatibility doubles.
        """
        lifecycle_controller = self._get_lifecycle_controller()
        handle_disconnect = _resolve_declared_callable(
            lifecycle_controller, "_handle_disconnect", "handle_disconnect"
        )
        if handle_disconnect is not None:
            result = handle_disconnect(
                source,
                client=client,
                bleak_client=bleak_client,
            )
            return result if isinstance(result, bool) else False
        return False

    def _on_ble_disconnect(self, client: BleakRootClient) -> None:
        """Handle a Bleak client disconnect callback.

        Parameters
        ----------
        client : BleakRootClient
            The Bleak client instance that disconnected.
        """
        lifecycle_controller = self._get_lifecycle_controller()
        on_ble_disconnect = _resolve_declared_callable(
            lifecycle_controller, "_on_ble_disconnect", "on_ble_disconnect"
        )
        if on_ble_disconnect is not None:
            on_ble_disconnect(client)

    def _schedule_auto_reconnect(self) -> None:
        """Schedule repeated automatic reconnection attempts until a connection is established or shutdown begins.

        Does nothing if automatic reconnection is disabled or the interface is closing or already closed.
        """
        lifecycle_controller = self._get_lifecycle_controller()
        schedule_auto_reconnect = _resolve_declared_callable(
            lifecycle_controller, "_schedule_auto_reconnect", "schedule_auto_reconnect"
        )
        if schedule_auto_reconnect is not None:
            schedule_auto_reconnect()

    @staticmethod
    def _resolve_thread_event_dispatcher(
        coordinator: object,
    ) -> Callable[[str], None] | None:
        """Resolve a declared current/legacy set-event hook in precedence order."""
        set_event = _resolve_declared_callable(coordinator, "set_event", "_set_event")
        return cast(Callable[[str], None], set_event) if set_event is not None else None

    def _set_thread_event(self, event_name: str) -> None:
        """Set thread-coordinator event via public-first compatibility dispatch."""
        coordinator = getattr(self, "thread_coordinator", None)
        if coordinator is None:
            logger.debug(
                "Thread coordinator is missing set_event/_set_event for event %s",
                event_name,
            )
            return
        dispatch_event = self._resolve_thread_event_dispatcher(coordinator)
        if dispatch_event is not None:
            try:
                dispatch_event(event_name)
            except Exception:  # noqa: BLE001 - event wakeups must remain best effort
                logger.debug(
                    "Error setting thread event %s",
                    event_name,
                    exc_info=True,
                )
            return
        logger.debug(
            "No callable thread event dispatcher available for event %s",
            event_name,
        )

    def _handle_malformed_fromnum(self, reason: str, *, exc_info: bool = False) -> None:
        """Track malformed FROMNUM notifications through dispatcher ownership."""
        self._get_notification_dispatcher().handle_malformed_fromnum(
            reason, exc_info=exc_info
        )

    def _report_notification_handler_error(self, error_msg: str) -> None:
        """Report notification handler failures through dispatcher ownership."""
        self._get_notification_dispatcher().report_notification_handler_error(error_msg)

    @staticmethod
    def _invoke_safe_execute_compat(
        safe_execute: Callable[..., Any],
        handler_thunk: Callable[[], None],
        *,
        error_msg: str,
        fallback: Callable[[], None],
    ) -> None:
        """Invoke safe_execute compatibility probing via notification dispatcher."""
        BLENotificationDispatcher.invoke_safe_execute_compat(
            safe_execute,
            handler_thunk,
            error_msg=error_msg,
            fallback=fallback,
        )

    # COMPAT_STABLE_SHIM (2.7.7): historical internal BLE callback entrypoint.
    # Keep callable without deprecation warning.
    def _from_num_handler(self, _: object, b: bytes | bytearray) -> None:
        """Process a FROMNUM notification via notification dispatcher."""
        self._get_notification_dispatcher().from_num_handler(_, b)

    # COMPAT_STABLE_SHIM (2.7.7): historical public BLEInterface callback.
    # Keep callable without deprecation warning.
    def from_num_handler(self, sender: Any, b: bytes | bytearray) -> None:
        """Backward-compatible wrapper for the legacy FROMNUM callback name.

        Parameters
        ----------
        sender : Any
            Notification sender/handle value provided by Bleak.
        b : bytes | bytearray
            FROMNUM payload.
        """
        self._from_num_handler(sender, b)

    def _register_notifications(self, client: BLEClient) -> None:
        """Register notifications through the notification dispatcher owner."""
        self._get_notification_dispatcher().register_notifications(
            self,
            client,
            legacy_log_handler=self._legacy_log_radio_handler,
            log_handler=self._log_radio_handler,
            from_num_handler=self._from_num_handler,
        )

    # COMPAT_STABLE_SHIM (2.7.7): historical internal BLE callback entrypoint.
    # Keep callable without deprecation warning.
    def _log_radio_handler(self, _: object, b: bytes | bytearray) -> None:
        """Decode and dispatch protobuf log notification via dispatcher owner."""
        message = self._get_notification_dispatcher().log_radio_handler(_, b)
        if message is not None:
            self._handle_log_line(message)

    # COMPAT_STABLE_SHIM (2.7.7): historical public BLEInterface callback.
    # Keep callable without deprecation warning.
    async def log_radio_handler(self, sender: Any, b: bytes | bytearray) -> None:
        """Backward-compatible wrapper for the legacy log callback name.

        Historical API in 2.7.7 used an async signature; keep it unchanged.

        Parameters
        ----------
        sender : Any
            Notification sender/handle value provided by Bleak.
        b : bytes | bytearray
            Serialized mesh_pb2.LogRecord payload.
        """
        # Async signature is intentional for 2.7.7 API compatibility.
        # Keep direct in-thread dispatch (no asyncio.to_thread/run_in_executor)
        # so legacy callback ordering/side-effects remain synchronous.
        self._log_radio_handler(sender, b)

    # COMPAT_STABLE_SHIM (2.7.7): historical internal BLE callback entrypoint.
    # Keep callable without deprecation warning.
    def _legacy_log_radio_handler(self, _: object, b: bytes | bytearray) -> None:
        """Decode and dispatch legacy UTF-8 log notification via dispatcher owner."""
        message = self._get_notification_dispatcher().legacy_log_radio_handler(_, b)
        if message is not None:
            self._handle_log_line(message)

    # COMPAT_STABLE_SHIM (2.7.7): historical public BLEInterface callback.
    # Keep callable without deprecation warning.
    async def legacy_log_radio_handler(self, sender: Any, b: bytes | bytearray) -> None:
        """Backward-compatible wrapper for the legacy log callback name.

        Historical API in 2.7.7 used an async signature; keep it unchanged.

        Parameters
        ----------
        sender : Any
            Notification sender/handle value provided by Bleak.
        b : bytes | bytearray
            Raw UTF-8 legacy log payload.
        """
        self._legacy_log_radio_handler(sender, b)

    @staticmethod
    async def _with_timeout(
        awaitable: Awaitable[T], timeout: float | None, label: str
    ) -> T:
        """Await an awaitable and raise a BLEInterface.BLEError if it does not complete within the given timeout.

        Parameters
        ----------
        awaitable : Awaitable[T]
            The awaitable to await.
        timeout : float | None
            Maximum time in seconds to wait; if None, wait indefinitely.
        label : str
            Short label used in the timeout error message.

        Returns
        -------
        T
            The value produced by the awaited awaitable.

        Raises
        ------
        BLEInterface.BLEError
            If the awaitable does not finish before the timeout elapses.
        """
        return await with_timeout(
            awaitable,
            timeout,
            label,
            timeout_error_factory=(
                lambda timeout_label, timeout_seconds: BLEInterface.BLEError(
                    ERROR_TIMEOUT.format(timeout_label, timeout_seconds)
                )
            ),
        )

    @staticmethod
    def scan() -> list[BLEDevice]:
        """Scan for BLE devices advertising the Meshtastic service UUID.

        Performs a timed scan and returns discovered devices that advertise the Meshtastic service.
        Returns an empty list if no matching devices are found or if the scan fails for non-DBus reasons.

        Returns
        -------
        list[BLEDevice]
            Discovered BLEDevice instances advertising the Meshtastic service.

        Raises
        ------
        BleakDBusError
            If a DBus-level error occurs during scanning (propagated to callers).
        """
        with BLEClient(log_if_no_address=False) as client:
            logger.debug(
                "Scanning for BLE devices (takes %.0f seconds)...",
                BLEConfig.BLE_SCAN_TIMEOUT,
            )
            try:
                response = client._discover(
                    timeout=BLEConfig.BLE_SCAN_TIMEOUT,
                    return_adv=True,
                    service_uuids=[SERVICE_UUID],
                )
                return _parse_scan_response(response)
            except BleakDBusError:
                # Propagate DBus-level failures so callers can back off appropriately
                raise
            except (BleakError, RuntimeError) as e:
                logger.warning("Device scan failed: %s", e, exc_info=True)
                return []
            except (
                Exception
            ) as e:  # noqa: BLE001 # pragma: no cover - defensive last resort
                logger.warning(
                    "Unexpected error during device scan: %s", e, exc_info=True
                )
                return []

    def findDevice(self, address: str | None) -> BLEDevice:
        """Locate a Meshtastic BLEDevice by address or device name.

        Parameters
        ----------
        address : str | None
            A device address or name to match. Separators (':', '-', '_', and spaces) are ignored when matching. If
            None, any discovered Meshtastic device may be returned.

        Returns
        -------
        BLEDevice
            The matched BLE device.

        Raises
        ------
        BLEInterface.BLEError
            If no Meshtastic devices are found, if multiple matching devices are found when an `address` was provided,
            if the discovery manager is unavailable, or if a synthetic device cannot be created from the provided
            address.
        """

        target = address or getattr(self, "address", None)
        sanitized = sanitize_address(target)

        # Pass raw target to discovery; the discovery matcher handles address
        # normalization internally and uses the raw identifier for name matching
        # to correctly match device names containing separators (_, -, spaces, :)
        addressed_devices = self._discover_devices(target)

        if len(addressed_devices) == 0:
            if target:
                if not _looks_like_ble_address(target):
                    raise BLEDeviceNotFoundError(
                        ERROR_NO_PERIPHERALS_FOUND,
                        self.BLEError.DEVICE_NOT_FOUND,
                        requested_identifier=target,
                    )
                logger.warning(
                    "No peripherals found for %s via scan; attempting direct address connect",
                    target,
                )
                if not sanitized:
                    raise BLEDiscoveryError(
                        ERROR_ADDRESS_RESOLUTION_FAILED,
                        requested_identifier=target,
                    )
                # Create a synthetic BLEDevice for direct address connection.
                # This allows the connection logic to attempt direct connect without
                # verification, supporting pre-bonded devices (e.g., via bluetoothctl).
                logger.debug(
                    "Creating synthetic BLEDevice for direct connect: address=%s, sanitized=%s",
                    target,
                    sanitized,
                )
                # Use the backend-usable raw address as the BLEDevice identity.
                # `sanitized` remains for registry/discovery key normalization.
                return BLEDevice(target, target, {})
            raise BLEDiscoveryError(
                ERROR_NO_PERIPHERALS_FOUND,
                self.BLEError.DEVICE_NOT_FOUND,
            )
        if len(addressed_devices) == 1:
            return addressed_devices[0]

        def _format_device_list(devices: list[BLEDevice]) -> str:
            return "\n".join(f"- {d.name or 'Unknown'} ({d.address})" for d in devices)

        if target and len(addressed_devices) > 1:
            # Build a list of found devices for the error message
            device_list = _format_device_list(addressed_devices)
            raise BLEDiscoveryError(
                ERROR_MULTIPLE_DEVICES.format(target, device_list),
                self.BLEError.MULTIPLE_DEVICES,
                requested_identifier=target,
            )
        if len(addressed_devices) > 1:
            device_list = _format_device_list(addressed_devices)
            raise BLEDiscoveryError(
                ERROR_MULTIPLE_DEVICES_DISCOVERY.format(device_list),
                self.BLEError.MULTIPLE_DEVICES,
            )
        raise AssertionError(UNREACHABLE_ADDRESSED_DEVICES_MSG)

    # COMPAT_STABLE_SHIM: historical public BLEInterface API.
    # Keep callable without deprecation warning.
    def find_device(self, address: str | None) -> BLEDevice:
        """Compatibility wrapper for legacy snake_case callers; delegates to findDevice().

        Parameters
        ----------
        address : str | None
            Bluetooth address or device identifier to resolve; if None, discovery is used to select a device.

        Returns
        -------
        BLEDevice
            The resolved BLEDevice matching the address or discovered selection.
        """
        return self.findDevice(address)

    @staticmethod
    def _extract_client_address(client: BLEClient | None) -> str | None:
        """Return the best-known address from a BLE client instance."""
        if client is None:
            return None
        bleak_client = getattr(client, "bleak_client", None)
        bleak_address = getattr(bleak_client, "address", None)
        return cast(str | None, bleak_address or getattr(client, "address", None))

    @property
    def bleAddress(self) -> str | None:
        """Return the best-known BLE address for the active interface client."""
        client = getattr(self, "client", None)
        active_client_address = self._extract_client_address(client)
        if isinstance(active_client_address, str) and _looks_like_ble_address(
            active_client_address
        ):
            return active_client_address
        interface_address = getattr(self, "address", None)
        if isinstance(interface_address, str) and _looks_like_ble_address(
            interface_address
        ):
            return interface_address
        return None

    # COMPAT_STABLE_SHIM
    @property
    def ble_address(self) -> str | None:
        """Compatibility shim for ``bleAddress``."""
        return self.bleAddress

    def _resolve_target_address_for_management(self, address: str | None) -> str:
        """Resolve management target through the management collaborator."""
        return self._get_management_command_handler().resolve_target_address_for_management(
            address
        )

    def _resolve_target_address_for_connect(
        self, requested_identifier: str | None
    ) -> str | None:
        """Resolve a connect target to a concrete BLE address when possible.

        Parameters
        ----------
        requested_identifier : str | None
            Explicit connect identifier (address/name) or None for implicit
            target selection.

        Returns
        -------
        str | None
            Concrete BLE address when resolution is available, otherwise None.
        """
        if requested_identifier and _looks_like_ble_address(requested_identifier):
            return requested_identifier
        if getattr(self, "_discovery_manager", None) is None:
            # Minimal/unit-test interfaces can intentionally omit discovery.
            return None
        return self.findDevice(requested_identifier).address

    @classmethod
    def _format_bluetoothctl_address(cls, address: str) -> str:
        """Canonicalize a BLE address for bluetoothctl commands."""
        sanitized = sanitize_address(address)
        if sanitized is None or len(sanitized) != 12:
            raise cls.BLEError(ERROR_TRUST_ADDRESS_NOT_RESOLVED.format(address=address))
        octets = [sanitized[index : index + 2].upper() for index in range(0, 12, 2)]
        return ":".join(octets)

    def _management_target_gate(self, target_address: str) -> contextlib.ExitStack:
        """Compatibility wrapper for address-gated management execution."""
        return self._get_management_command_handler().management_target_gate(
            target_address
        )

    def _get_management_client_if_available(
        self, address: str | None
    ) -> BLEClient | None:
        """Return available management client via management collaborator."""
        handler = self._get_management_command_handler()
        return handler.get_management_client_if_available(address)

    def _get_management_client_if_available_locked(
        self, address: str | None
    ) -> BLEClient | None:
        """Return available management client while caller already holds ``_state_lock``."""
        handler = self._get_management_command_handler()
        return handler.get_management_client_if_available_locked(address)

    def _get_management_client_for_target(
        self,
        target_address: str,
        *,
        prefer_current_client: bool,
    ) -> BLEClient | None:
        """Return reusable target-matching management client via collaborator."""
        handler = self._get_management_command_handler()
        return handler.get_management_client_for_target(
            target_address,
            prefer_current_client=prefer_current_client,
        )

    def _get_management_client_for_target_locked(
        self,
        target_address: str,
        *,
        prefer_current_client: bool,
    ) -> BLEClient | None:
        """Return reusable target-matching client while ``_state_lock`` is held."""
        handler = self._get_management_command_handler()
        return handler.get_management_client_for_target_locked(
            target_address,
            prefer_current_client=prefer_current_client,
        )

    def _get_current_implicit_management_binding_locked(self) -> str | None:
        """Return implicit management binding via management collaborator.

        Caller must hold ``_state_lock``.
        """
        handler = self._get_management_command_handler()
        return handler.get_current_implicit_management_binding_locked()

    def _get_current_implicit_management_address_locked(self) -> str | None:
        """Return implicit management concrete address via collaborator.

        Caller must hold ``_state_lock``.
        """
        handler = self._get_management_command_handler()
        return handler.get_current_implicit_management_address_locked()

    def _revalidate_implicit_management_target(
        self,
        expected_target_address: str,
        *,
        expected_binding: str | None = None,
    ) -> None:
        """Revalidate implicit management target via collaborator."""
        self._get_management_command_handler().revalidate_implicit_management_target(
            expected_target_address,
            expected_binding=expected_binding,
        )

    def _get_or_create_error_handler(self) -> BLEErrorHandler:
        """Return bound error handler, creating one lazily for partial test doubles."""
        handler = getattr(self, "error_handler", None)
        if handler is None:
            handler = BLEErrorHandler()
            self.error_handler = handler
        return cast(BLEErrorHandler, handler)

    def _get_or_create_collaborator(
        self, attr_name: str, factory: Callable[[], T]
    ) -> T:
        """Return collaborator by attribute name, creating it lazily.

        Uses `_state_lock` double-checked locking when available, and degrades
        to lockless initialization for partial test doubles that bypass `__init__`.
        """
        collaborator = getattr(self, attr_name, None)
        if collaborator is not None:
            return cast(T, collaborator)
        state_lock = _get_declared_lock(self, "_state_lock")
        if state_lock is None:
            instance_dict = _get_declared_member(self, "__dict__")
            if isinstance(instance_dict, dict):
                # Partial test doubles can lack the canonical state lock. A
                # lazily shared per-instance lock preserves single construction
                # without imposing a process-wide fallback lock.
                fallback_lock = instance_dict.setdefault(
                    "_ble_collaborator_init_lock", threading.RLock()
                )
                fallback_lock_type = type(fallback_lock)
                if (
                    _get_declared_callable(fallback_lock_type, "__enter__") is None
                    or _get_declared_callable(fallback_lock_type, "__exit__") is None
                ):
                    raise TypeError(
                        "_ble_collaborator_init_lock must be a context-manager lock"
                    )
                with cast(Any, fallback_lock):
                    collaborator = instance_dict.get(attr_name)
                    if collaborator is None:
                        collaborator = factory()
                        instance_dict[attr_name] = collaborator
                return cast(T, collaborator)
            collaborator = getattr(self, attr_name, None)
            if collaborator is None:
                collaborator = factory()
                setattr(self, attr_name, collaborator)
            return cast(T, collaborator)
        with state_lock:
            collaborator = getattr(self, attr_name, None)
            if collaborator is None:
                collaborator = factory()
                setattr(self, attr_name, collaborator)
        return cast(T, collaborator)

    def _create_notification_dispatcher(self) -> BLENotificationDispatcher:
        """Build notification dispatcher with canonical collaborator wiring."""
        notification_manager = getattr(self, "_notification_manager", None)
        if notification_manager is None:
            notification_manager = NotificationManager()
            self._notification_manager = notification_manager
        return BLENotificationDispatcher(
            notification_manager=notification_manager,
            error_handler_provider=self._get_or_create_error_handler,
            trigger_read_event=lambda: self._set_thread_event(READ_TRIGGER_EVENT),
            registration_current_provider=self._is_notification_registration_current,
        )

    @staticmethod
    def _connected_elsewhere_late_bound(
        key: str | None, owner: object | None = None
    ) -> bool:
        """Late-bound gateway for interface-level ownership probes."""
        return _is_currently_connected_elsewhere(key, owner=owner)

    def _get_notification_dispatcher(self) -> BLENotificationDispatcher:
        """Return notification dispatcher collaborator, creating it lazily."""
        return self._get_or_create_collaborator(
            "_notification_dispatcher", self._create_notification_dispatcher
        )

    def _is_notification_registration_current(
        self,
        client: BLEClient,
        expected_session_epoch: int,
    ) -> bool:
        """Return whether notification setup still belongs to the active BLE session.

        Notification registration occurs during connection finalization before the
        newly connected client is published through ``self.client``. Treat that
        provisional CONNECTING phase as current when the session epoch still
        matches and publication is pending. Once the client is published, identity
        with ``self.client`` becomes the ownership check.

        Parameters
        ----------
        client : BLEClient
            Client whose notification subscriptions are being registered.
        expected_session_epoch : int
            Connection-attempt epoch captured before notification I/O begins.

        Returns
        -------
        bool
            ``True`` only while the registration belongs to the same live session.
        """
        state_manager = self._state_manager
        lifecycle_owner_is_current: bool | None
        canonical_locked_probe = _is_canonical_state_manager(
            state_manager, self._state_lock
        )
        with self._state_lock:
            if self._closed or self._connection_session_epoch != expected_session_epoch:
                return False
            if self.client is client:
                lifecycle_owner_is_current = True
            elif not self._client_publish_pending:
                lifecycle_owner_is_current = False
            elif canonical_locked_probe:
                lifecycle_owner_is_current = (
                    BLEStateManager._current_state_unlocked(state_manager)
                    == ConnectionState.CONNECTING
                )
            else:
                lifecycle_owner_is_current = None

        if lifecycle_owner_is_current is None:
            # Compatibility state probes can execute arbitrary collaborator code.
            # Run them outside the shared lock, then revalidate the complete
            # registration ownership snapshot before accepting the result.
            current_state = self._state_manager_current_state()
            with self._state_lock:
                lifecycle_owner_is_current = (
                    not self._closed
                    and self._connection_session_epoch == expected_session_epoch
                    and (
                        self.client is client
                        or (
                            self._client_publish_pending
                            and current_state == ConnectionState.CONNECTING
                        )
                    )
                )
        if not lifecycle_owner_is_current:
            return False
        return ConnectionValidator._client_is_connected(client)

    # COMPAT_STABLE_SHIM: internal bridge for historical FROMNUM notify state probes.
    @property
    def _fromnum_notify_enabled(self) -> bool:
        """Compatibility bridge exposing FROMNUM notification-active state."""
        return self._get_notification_dispatcher().fromnum_notify_enabled

    @_fromnum_notify_enabled.setter
    def _fromnum_notify_enabled(self, enabled: bool) -> None:
        """Compatibility bridge setter for FROMNUM notification-active state."""
        self._get_notification_dispatcher().fromnum_notify_enabled = bool(enabled)

    # COMPAT_STABLE_SHIM: internal bridge for historical malformed-notification counter access.
    @property
    def _malformed_notification_count(self) -> int:
        """Compatibility bridge exposing malformed FROMNUM counter."""
        return self._get_notification_dispatcher().malformed_notification_count

    @_malformed_notification_count.setter
    def _malformed_notification_count(self, count: int) -> None:
        """Compatibility bridge setter for malformed FROMNUM counter."""
        self._get_notification_dispatcher().malformed_notification_count = int(count)

    # COMPAT_STABLE_SHIM: internal bridge for historical malformed-notification lock access.
    @property
    def _malformed_notification_lock(self) -> threading.RLock:
        """Compatibility bridge exposing malformed FROMNUM lock."""
        return self._get_notification_dispatcher().malformed_notification_lock

    @_malformed_notification_lock.setter
    def _malformed_notification_lock(self, lock: threading.RLock) -> None:
        """Compatibility bridge setter for malformed FROMNUM lock."""
        self._get_notification_dispatcher().malformed_notification_lock = lock

    def _get_lifecycle_controller(self) -> BLELifecycleController:
        """Return lifecycle collaborator, creating one lazily when needed."""
        return self._get_or_create_collaborator(
            "_lifecycle_controller",
            lambda: BLELifecycleController(
                self,
                client_closer=self._client_manager_safe_close_client,
                management_state=self._get_management_state(),
            ),
        )

    def _get_receive_recovery_controller(self) -> BLEReceiveRecoveryController:
        """Return receive/recovery collaborator, creating one lazily when needed."""
        return self._get_or_create_collaborator(
            "_receive_recovery_controller", lambda: BLEReceiveRecoveryController(self)
        )

    def _get_compatibility_publisher(self) -> BLECompatibilityEventPublisher:
        """Return compatibility publisher collaborator, creating one lazily when needed."""
        return self._get_or_create_collaborator(
            "_compatibility_publisher",
            lambda: BLECompatibilityEventPublisher(
                self,
                publishing_thread_provider=self._get_publishing_thread,
            ),
        )

    def _get_management_state(self) -> BLEManagementState:
        """Return owned management state, repairing partial-object state safely."""
        state = self.__dict__.get("_management_state")
        if isinstance(state, BLEManagementState):
            return state

        repair_lock = cast(
            _LockPort,
            BLEInterface._get_or_create_collaborator(
                self,
                "_ble_management_state_init_lock",
                threading.RLock,
            ),
        )
        with repair_lock:
            state = self.__dict__.get("_management_state")
            if isinstance(state, BLEManagementState):
                return state
            state = BLEManagementState()
            self.__dict__["_management_state"] = state
            return state

    @property
    def _management_lock(self) -> _LockPort:
        """Compatibility view of the owned management lock."""
        return self._get_management_state().lock

    @_management_lock.setter
    def _management_lock(self, value: _LockPort) -> None:
        self._get_management_state()._replace_lock(value)

    @property
    def _management_idle_condition(self) -> _ManagementConditionPort:
        """Compatibility view of the owned management idle condition."""
        return self._get_management_state().condition

    @_management_idle_condition.setter
    def _management_idle_condition(self, value: _ManagementConditionPort) -> None:
        self._get_management_state()._replace_condition(value)

    @property
    def _management_inflight(self) -> int:
        """Compatibility view of in-flight management-operation count."""
        return self._get_management_state().inflight

    @_management_inflight.setter
    def _management_inflight(self, value: int) -> None:
        self._get_management_state().inflight = value

    def _get_management_command_handler(self) -> BLEManagementCommandHandler:
        """Return the bound management-command collaborator.

        Returns
        -------
        BLEManagementCommandHandler
            Lazily initialized management command collaborator.
        """
        return self._get_or_create_collaborator(
            "_management_command_handler",
            lambda: BLEManagementCommandHandler(
                self,
                ble_client_factory=BLEClient,
                connected_elsewhere=self._connected_elsewhere_late_bound,
                session_state=self._session_state,
                management_state=self._get_management_state(),
                connect_lock=self._connect_lock,
                client_closer=self._client_manager_safe_close_client,
            ),
        )

    def _execute_management_command(
        self,
        address: str | None,
        command: Callable[[BLEClient], T],
    ) -> T:
        """Run a BLE management command using an active, existing, or temporary client.

        Parameters
        ----------
        address : str | None
            Target address or identifier. If None, prefer the currently connected
            client for this interface when available.
        command : Callable[[BLEClient], T]
            Command to execute against the selected BLE client.

        Returns
        -------
        T
            Command return value.
        """
        return self._get_management_command_handler().execute_management_command(
            address,
            command,
        )

    def _begin_management_operation_locked(self) -> None:
        """Record management operation via collaborator."""
        with self._management_lock:
            self._get_management_command_handler().begin_management_operation_locked()

    def _finish_management_operation(self) -> None:
        """Mark completion of management operation via collaborator."""
        self._get_management_command_handler().finish_management_operation()

    def _validate_management_await_timeout(self, await_timeout: object) -> float:
        """Validate and return a bounded await timeout for management operations.

        Parameters
        ----------
        await_timeout : object
            Timeout value to validate; must be a finite positive real number.

        Returns
        -------
        float
            The validated timeout as a float.

        Raises
        ------
        BLEInterface.BLEError
            If `await_timeout` is None, a boolean, non-numeric, non-finite,
            zero, or negative.
        """
        return self._get_management_command_handler().validate_management_await_timeout(
            await_timeout
        )

    def _validate_trust_timeout(self, timeout: object) -> float:
        """Validate and return a bounded timeout for `trust()`.

        Parameters
        ----------
        timeout : object
            Timeout value to validate; must be a finite positive real number.

        Returns
        -------
        float
            The validated timeout as a float.

        Raises
        ------
        BLEInterface.BLEError
            If `timeout` is a boolean, non-numeric, non-finite, zero, or
            negative.
        """
        return self._get_management_command_handler().validate_trust_timeout(timeout)

    def _validate_connect_timeout_override(
        self,
        connect_timeout: object,
        *,
        pair_on_connect: bool,
    ) -> None:
        """Validate a connect timeout override before connection orchestration."""
        self._get_management_command_handler().validate_connect_timeout_override(
            connect_timeout,
            pair_on_connect=pair_on_connect,
        )

    def pair(
        self,
        address: str | None = None,
        *,
        await_timeout: float = BLECLIENT_MANAGEMENT_AWAIT_TIMEOUT,
        **kwargs: object,
    ) -> None:
        """Pair with a BLE device either through the active client or a temporary client.

        Parameters
        ----------
        address : str | None
            Target device address or identifier. If None, use the active
            connection target when available; otherwise fall back to the
            interface's bound target. Raises if no active or bound target is
            available.
        await_timeout : float
            Maximum seconds to wait for the pairing coroutine to complete.
            `BLEInterface` requires a finite positive timeout here so shutdown
            cannot block indefinitely behind a management operation. Defaults
            to `BLECLIENT_MANAGEMENT_AWAIT_TIMEOUT`.
        **kwargs : object
            Backend-specific pairing options forwarded to `BLEClient.pair()`.

        Returns
        -------
        None
            Pairing is performed for side effects and does not return a value.
        """
        self._get_management_command_handler().pair(
            address,
            await_timeout=await_timeout,
            kwargs=dict(kwargs),
        )

    def unpair(
        self,
        address: str | None = None,
        *,
        await_timeout: float = BLECLIENT_MANAGEMENT_AWAIT_TIMEOUT,
    ) -> None:
        """Unpair from a BLE device through the active or a temporary client.

        Parameters
        ----------
        address : str | None
            Target device address or identifier. If None, use the active
            connection target when available; otherwise fall back to the
            interface's bound target. Raises if no active or bound target is
            available.
        await_timeout : float
            Maximum seconds to wait for the unpair coroutine to complete.
            `BLEInterface` requires a finite positive timeout here so shutdown
            cannot block indefinitely behind a management operation. Defaults
            to `BLECLIENT_MANAGEMENT_AWAIT_TIMEOUT`.

        Returns
        -------
        None
            Unpairing is performed for side effects and does not return a value.
        """
        self._get_management_command_handler().unpair(
            address,
            await_timeout=await_timeout,
        )

    def _run_bluetoothctl_trust_command(
        self,
        bluetoothctl_path: str,
        canonical_address: str,
        validated_timeout: float,
    ) -> None:
        """Delegate execution of ``bluetoothctl trust`` to management service.

        Parameters
        ----------
        bluetoothctl_path : str
            Resolved path to the ``bluetoothctl`` binary.
        canonical_address : str
            Canonicalized device address passed to ``bluetoothctl trust``.
        validated_timeout : float
            Positive finite timeout in seconds for subprocess execution.

        Returns
        -------
        None
            Trust execution is performed for side effects only.

        Raises
        ------
        BLEError
            If command execution fails or times out.
        """
        self._get_management_command_handler().run_bluetoothctl_trust_command(
            bluetoothctl_path,
            canonical_address,
            timeout=validated_timeout,
            subprocess_module=subprocess,
            trust_hex_blob_re=_TRUST_HEX_BLOB_RE,
            trust_token_re=_TRUST_TOKEN_RE,
            trust_command_output_max_chars=_TRUST_COMMAND_OUTPUT_MAX_CHARS,
        )

    def trust(
        self,
        address: str | None = None,
        *,
        timeout: float = _BLUETOOTHCTL_TRUST_TIMEOUT_SECONDS,
    ) -> None:
        """Mark a BLE device as trusted via Linux bluetoothctl.

        Parameters
        ----------
        address : str | None
            Target device address or identifier. If None, use the active
            connection target when available; otherwise fall back to the
            interface's bound target.
        timeout : float
            Maximum seconds to wait for the `bluetoothctl` command to
            complete. Must be a finite positive timeout. Defaults to
            `_BLUETOOTHCTL_TRUST_TIMEOUT_SECONDS`.

        Raises
        ------
        BLEError
            If the interface is closing, if the target address is blank or
            unavailable, if not running on Linux, if `bluetoothctl` is not
            found, or if the trust command fails.

        Notes
        -----
        - This helper is Linux-only and requires `bluetoothctl` on PATH.
        - Pairing PIN/passkey handling remains OS-agent managed.
        """
        self._get_management_command_handler().trust(
            address,
            timeout=timeout,
            sys_module=sys,
            shutil_module=shutil,
            subprocess_module=subprocess,
            trust_hex_blob_re=_TRUST_HEX_BLOB_RE,
            trust_token_re=_TRUST_TOKEN_RE,
            trust_command_output_max_chars=_TRUST_COMMAND_OUTPUT_MAX_CHARS,
        )

    def _sanitize_address(self, address: str | None) -> str | None:
        """Provide a backward-compatible wrapper that returns a sanitized BLE address or None.

        Parameters
        ----------
        address : str | None
            BLE address to sanitize; if None, returns None.

        Returns
        -------
        str | None
            The sanitized address string, or `None` if the input was `None`.
        """
        return sanitize_address(address)

    @staticmethod
    def _compat_dispatch_callable(
        target: object,
        *,
        public_name: str,
        legacy_name: str,
        fallback_attr_name: str | None,
        error_message: str,
        args: tuple[object, ...] = (),
        kwargs: dict[str, object] | None = None,
    ) -> object:
        """Dispatch a callable through public/legacy/fallback compatibility names."""
        kwargs = {} if kwargs is None else kwargs
        names = (
            (public_name, legacy_name)
            if fallback_attr_name is None
            else (public_name, legacy_name, fallback_attr_name)
        )
        member = _resolve_declared_callable(target, *names)
        if member is None:
            raise AttributeError(error_message)
        return member(*args, **kwargs)

    @staticmethod
    def _compat_get_bool_member(
        target: object,
        *,
        public_name: str,
        legacy_name: str,
        fallback_attr_name: str | None,
        error_message: str,
    ) -> bool:
        """Resolve bool members via public/legacy/fallback compatibility names."""
        names = (
            (public_name, legacy_name)
            if fallback_attr_name is None
            else (public_name, legacy_name, fallback_attr_name)
        )
        for member_name, member in _iter_declared_members(target, *names):
            if callable(member):
                try:
                    resolved = member()
                except Exception:  # noqa: BLE001 - probe dispatch stays best effort
                    _log_ble_failure(
                        _BLEFailureDisposition.COMPATIBILITY_FALLBACK,
                        "Error probing BLE compatibility member %s()",
                        member_name,
                    )
                    continue
            else:
                resolved = member
            if isinstance(resolved, bool):
                return resolved
        raise AttributeError(error_message)

    def _discover_devices(self, address: str | None) -> list[BLEDevice]:
        # COMPAT_STABLE_SHIM: compatibility wrapper for collaborator API migration.
        """Discover devices via discovery-manager compatibility dispatch.

        Parameters
        ----------
        address : str | None
            Optional address or identifier used to filter scan results.

        Returns
        -------
        list[BLEDevice]
            Discovered devices matching the requested target filter.

        Raises
        ------
        AttributeError
            If no supported discovery entrypoint exists on the configured
            discovery manager.
        """
        discovery_manager = self._discovery_manager
        if discovery_manager is None:
            return []
        return cast(
            list[BLEDevice],
            self._compat_dispatch_callable(
                discovery_manager,
                public_name="discover_devices",
                legacy_name="_discover_devices",
                fallback_attr_name=None,
                error_message=ERROR_DISCOVERY_MANAGER_MISSING_DISCOVER_DEVICES,
                args=(address,),
            ),
        )

    def _state_manager_is_connected(self) -> bool:
        # COMPAT_STABLE_SHIM: compatibility wrapper for collaborator API migration.
        """Read connected-state flag through compatibility member dispatch.

        Returns
        -------
        bool
            Current connected-state flag from the configured state manager.

        Raises
        ------
        AttributeError
            If no supported connected-state member exists on the state manager.
        """
        return self._compat_get_bool_member(
            self._state_manager,
            public_name="is_connected",
            legacy_name="_is_connected",
            fallback_attr_name=None,
            error_message=ERROR_STATE_MANAGER_MISSING_BOOL_IS_CONNECTED,
        )

    def _state_manager_current_state(self) -> ConnectionState:
        """Read current state through lifecycle state-access compatibility dispatch."""
        return _LifecycleStateAccess(self._state_manager).current_state()

    def _state_manager_is_closing(self) -> bool:
        """Read closing-state flag through lifecycle state-access compatibility dispatch."""
        return _LifecycleStateAccess(self._state_manager).is_closing()

    def _validator_check_existing_client(
        self,
        client: BLEClient,
        normalized_request: str | None,
        last_connection_request: str | None,
    ) -> bool:
        # COMPAT_STABLE_SHIM: compatibility wrapper for collaborator API migration.
        """Check whether an existing client matches the requested connection target.

        Parameters
        ----------
        client : BLEClient
            Client candidate to validate.
        normalized_request : str | None
            Normalized requested address/identifier.
        last_connection_request : str | None
            Last stored normalized connection request.

        Returns
        -------
        bool
            ``True`` when the client is considered a compatible existing
            connection target.

        Raises
        ------
        AttributeError
            If neither supported validator compatibility method exists.
        """
        return bool(
            self._compat_dispatch_callable(
                self._connection_validator,
                public_name="check_existing_client",
                legacy_name="_check_existing_client",
                fallback_attr_name=None,
                error_message=ERROR_CONNECTION_VALIDATOR_MISSING_CHECK_EXISTING_CLIENT,
                args=(
                    client,
                    normalized_request,
                    last_connection_request,
                ),
            )
        )

    def _establish_connection(
        self,
        address: str | None,
        *,
        pair_on_connect: bool = False,
        connect_timeout: float | None = None,
    ) -> BLEClient:
        # COMPAT_STABLE_SHIM: compatibility wrapper for collaborator API migration.
        """Establish a BLE connection through orchestrator compatibility dispatch.

        Parameters
        ----------
        address : str | None
            Explicit target address/identifier or ``None`` for discovery mode.
        pair_on_connect : bool
            Whether pairing should be requested during connect.
        connect_timeout : float | None
            Optional caller-supplied connect timeout in seconds.

        Returns
        -------
        BLEClient
            Connected BLE client instance.

        Raises
        ------
        AttributeError
            If no supported orchestrator connect entrypoint is available.
        BLEError
            Propagated from orchestrator connection flow on failure.
        """
        args = (
            address,
            self.address,
            self._register_notifications,
            # Defer _connected() publication until interface ownership
            # has been committed and verified.
            lambda: None,
            self._on_ble_disconnect,
        )
        kwargs: dict[str, object] = {
            "pair_on_connect": pair_on_connect,
            "connect_timeout": connect_timeout,
            "emit_connected_side_effects": False,
        }
        try:
            result = self._compat_dispatch_callable(
                self._connection_orchestrator,
                public_name="establish_connection",
                legacy_name="_establish_connection",
                fallback_attr_name=None,
                error_message=ERROR_CONNECTION_ORCHESTRATOR_MISSING_ESTABLISH_CONNECTION,
                args=args,
                kwargs=kwargs,
            )
        except TypeError as exc:
            if not _is_unexpected_keyword_error(exc, "emit_connected_side_effects"):
                raise
            legacy_kwargs: dict[str, object] = {
                "pair_on_connect": pair_on_connect,
                "connect_timeout": connect_timeout,
            }
            result = self._compat_dispatch_callable(
                self._connection_orchestrator,
                public_name="establish_connection",
                legacy_name="_establish_connection",
                fallback_attr_name=None,
                error_message=ERROR_CONNECTION_ORCHESTRATOR_MISSING_ESTABLISH_CONNECTION,
                args=args,
                kwargs=legacy_kwargs,
            )
        return cast(BLEClient, result)

    def _client_manager_safe_close_client(
        self,
        client: BLEClient,
        *,
        disconnect_timeout: float | None = None,
    ) -> None:
        # COMPAT_STABLE_SHIM: compatibility wrapper for collaborator API migration.
        """Close a BLE client via client-manager compatibility dispatch.

        Parameters
        ----------
        client : BLEClient
            Client to close using best-effort manager helpers.

        Returns
        -------
        None
            Cleanup is performed for side effects only.

        Raises
        ------
        AttributeError
            If no supported client-close helper exists on the manager.
        """
        try:
            self._compat_dispatch_callable(
                self._client_manager,
                public_name="safe_close_client",
                legacy_name="_safe_close_client",
                fallback_attr_name=None,
                error_message=ERROR_CLIENT_MANAGER_MISSING_SAFE_CLOSE_CLIENT,
                args=(client,),
                kwargs={"disconnect_timeout": disconnect_timeout},
            )
        except TypeError as exc:
            if not _is_unexpected_keyword_error(exc, "disconnect_timeout"):
                raise
            self._compat_dispatch_callable(
                self._client_manager,
                public_name="safe_close_client",
                legacy_name="_safe_close_client",
                fallback_attr_name=None,
                error_message=ERROR_CLIENT_MANAGER_MISSING_SAFE_CLOSE_CLIENT,
                args=(client,),
            )

    def _client_manager_update_client_reference(
        self, client: BLEClient, previous_client: BLEClient
    ) -> None:
        # COMPAT_STABLE_SHIM: compatibility wrapper for collaborator API migration.
        """Update active-client reference via manager compatibility dispatch.

        Parameters
        ----------
        client : BLEClient
            Client that should become active.
        previous_client : BLEClient
            Previously active client to retire/close asynchronously.

        Returns
        -------
        None
            Update is performed for side effects only.

        Raises
        ------
        AttributeError
            If no supported update-client-reference helper is available.
        """
        self._compat_dispatch_callable(
            self._client_manager,
            public_name="update_client_reference",
            legacy_name="_update_client_reference",
            fallback_attr_name=None,
            error_message=ERROR_CLIENT_MANAGER_MISSING_UPDATE_CLIENT_REFERENCE,
            args=(client, previous_client),
        )

    @staticmethod
    def _retry_policy_should_retry(policy: object, attempt: int) -> bool:
        # COMPAT_STABLE_SHIM: compatibility wrapper for collaborator API migration.
        """Evaluate retry permission via policy compatibility dispatch.

        Parameters
        ----------
        policy : object
            Retry-policy object exposing public or underscore retry helpers.
        attempt : int
            Retry attempt index to evaluate.

        Returns
        -------
        bool
            ``True`` when another retry is permitted.

        Raises
        ------
        AttributeError
            If no supported retry helper exists on ``policy``.
        """
        return bool(
            BLEInterface._compat_dispatch_callable(
                policy,
                public_name="should_retry",
                legacy_name="_should_retry",
                fallback_attr_name=None,
                error_message=ERROR_RETRY_POLICY_MISSING_SHOULD_RETRY,
                args=(attempt,),
            )
        )

    @staticmethod
    def _retry_policy_get_delay(policy: object, attempt: int) -> float:
        # COMPAT_STABLE_SHIM: compatibility wrapper for collaborator API migration.
        """Resolve retry delay via policy compatibility dispatch.

        Parameters
        ----------
        policy : object
            Retry-policy object exposing public or underscore delay helpers.
        attempt : int
            Retry attempt index used for delay calculation.

        Returns
        -------
        float
            Retry delay in seconds.

        Raises
        ------
        AttributeError
            If no supported delay helper exists on ``policy``.
        """
        result = BLEInterface._compat_dispatch_callable(
            policy,
            public_name="get_delay",
            legacy_name="_get_delay",
            fallback_attr_name=None,
            error_message=ERROR_RETRY_POLICY_MISSING_GET_DELAY,
            args=(attempt,),
        )
        if isinstance(result, numbers.Real) and not isinstance(result, bool):
            try:
                normalized_result = float(result)
            except (TypeError, ValueError, OverflowError):
                normalized_result = None
            if (
                normalized_result is not None
                and math.isfinite(normalized_result)
                and normalized_result >= 0
            ):
                return normalized_result
        logger.debug(
            "Retry policy get_delay returned invalid value %r; defaulting to 0.0",
            result,
        )
        return 0.0

    @property
    def _connection_state(self) -> ConnectionState:
        """Get the current BLE connection state.

        Returns
        -------
        ConnectionState
            The current connection state of the interface.
        """
        with self._state_lock:
            return self._state_manager_current_state()

    @property
    def _is_connection_connected(self) -> bool:
        """Return whether the interface currently has an active BLE connection.

        Returns
        -------
        bool
            `True` if a BLE connection is active, `False` otherwise.
        """
        with self._state_lock:
            return self._state_manager_is_connected()

    @property
    def _is_connection_closing(self) -> bool:
        """Return whether the interface is shutting down or has already closed.

        Returns
        -------
        bool
            True if the interface is closing or closed, False otherwise.
        """
        with self._state_lock:
            return self._state_manager_is_closing() or self._closed

    @property
    def _can_initiate_connection(self) -> bool:
        """Return whether the interface may start a new BLE connection.

        Considers the centralized connection state and whether the interface is shutting down.

        Returns
        -------
        bool
            True if a new connection can be initiated, False otherwise.
        """
        with self._state_lock:
            return (
                self._state_manager_current_state()
                in (
                    ConnectionState.DISCONNECTED,
                    ConnectionState.RECONNECTING,
                    ConnectionState.ERROR,
                )
                and not self._closed
            )

    # ---------------------------------------------------------------------
    # Connection helper methods (extracted from connect() for readability)
    # ---------------------------------------------------------------------

    def _validate_connection_preconditions(self) -> None:
        """Raise BLEError if the BLE interface is closing or already closed to prevent initiating a new connection.

        Raises
        ------
        BLEError
            with ERROR_INTERFACE_CLOSING when the interface is closing or already closed.
        """
        if self._is_connection_closing:
            raise self.BLEError(ERROR_INTERFACE_CLOSING)

    def _validate_management_preconditions(self) -> None:
        """Raise BLEError if the interface is closing or actively connecting.

        Management operations are side-effecting and should not race a
        connection attempt that is still establishing or replacing the active
        client. We reject them while CONNECTING rather than trying to interleave
        them with connect/reconnect state transitions.

        Raises
        ------
        BLEError
            With ERROR_INTERFACE_CLOSING when shutdown has begun, or
            ERROR_MANAGEMENT_CONNECTING when a connection attempt is in progress.
        """
        self._validate_connection_preconditions()
        with self._state_lock:
            if (
                self._state_manager_current_state() == ConnectionState.CONNECTING
                or self._client_publish_pending
            ):
                raise self.BLEError(ERROR_MANAGEMENT_CONNECTING)

    def _should_suppress_duplicate_connect(self, connection_key: str | None) -> bool:
        """Return whether a connect attempt for the given connection key should be suppressed because an active connection
        for that key exists on a different interface.

        Parameters
        ----------
        connection_key : str | None
            Address or alias key to check; use None for discovery-mode connects.

        Returns
        -------
        bool
            `True` if the connection should be suppressed because the key is connected elsewhere and this interface is
            not the active connection, `False` otherwise.
        """
        with self._state_lock:
            is_self_connected = self._state_manager_is_connected()
        return bool(
            connection_key
            and _is_currently_connected_elsewhere(connection_key, owner=self)
            and not is_self_connected
        )

    def _raise_if_duplicate_connect(
        self,
        connection_key: str | None,
        *,
        requested_identifier: str | None = None,
        resolved_address: str | None = None,
    ) -> None:
        """Raise BLEError when connect should be suppressed for a duplicate address claim.

        Parameters
        ----------
        connection_key : str | None
            Address registry key for this connect attempt.
        requested_identifier : str | None
            Original requested identifier (address or device name) used for
            exception context.
        resolved_address : str | None
            Concrete BLE address resolved from the requested identifier, if
            available.

        Raises
        ------
        BLEInterface.BLEError
            If this address is currently connected elsewhere.
        """
        if self._should_suppress_duplicate_connect(connection_key):
            logger.info(
                "Suppressing duplicate connect to %s: recently connected elsewhere.",
                connection_key or "unknown",
            )
            raise BLEConnectionSuppressedError(
                ERROR_CONNECTION_SUPPRESSED,
                address=resolved_address or connection_key,
                requested_identifier=requested_identifier or connection_key,
            )

    def _get_existing_client_if_valid(
        self, normalized_request: str | None
    ) -> BLEClient | None:
        """Return the current BLE client if it represents a connected client compatible with the provided request.

        Parameters
        ----------
        normalized_request : str | None
            Sanitized connection request identifier used to validate compatibility with the current client.

        Returns
        -------
        BLEClient | None
            The existing connected client if compatible with `normalized_request`, otherwise `None`.
        """
        with self._state_lock:
            existing_client = self.client
            last_connection_request = self._last_connection_request
            is_connected = (
                self._state_manager_is_connected()
                and not self._disconnect_notified
                and not getattr(self, "_client_publish_pending", False)
            )
        if not is_connected or existing_client is None:
            return None
        if self._validator_check_existing_client(
            existing_client,
            normalized_request,
            last_connection_request,
        ):
            return existing_client
        return None

    def _snapshot_connect_notifications(
        self,
    ) -> tuple[object | None, _NotificationSessionSnapshot | None]:
        """Capture notification registration state for connect rollback.

        Returns
        -------
        tuple[object | None, _NotificationSessionSnapshot | None]
            Dispatcher and snapshot when the concrete dispatcher supports
            transactional snapshots; otherwise the available dispatcher with
            ``None`` snapshot state.

        Notes
        -----
        The connect transaction calls this helper while ``_connect_lock`` is
        held. Snapshot support remains feature-detected so compatibility test
        doubles and older collaborators can continue without this private hook.
        """
        try:
            dispatcher = self._get_notification_dispatcher()
        except Exception:  # noqa: BLE001 - rollback snapshot remains best effort
            return None, None
        snapshotter = getattr(dispatcher, "_snapshot_session_state", None)
        if not callable(snapshotter):
            return dispatcher, None
        try:
            snapshot = snapshotter()
        except Exception:  # noqa: BLE001 - rollback snapshot remains best effort
            logger.debug(
                "Unable to snapshot BLE notification state before connect.",
                exc_info=True,
            )
            return dispatcher, None
        if not isinstance(snapshot, _NotificationSessionSnapshot):
            return dispatcher, None
        return dispatcher, snapshot

    @staticmethod
    def _restore_connect_notifications(
        dispatcher: object | None,
        snapshot: _NotificationSessionSnapshot | None,
    ) -> None:
        """Best-effort restoration of notification registration state.

        Parameters
        ----------
        dispatcher : object | None
            Dispatcher captured before the connection attempt.
        snapshot : _NotificationSessionSnapshot | None
            Registration snapshot captured from ``dispatcher``.

        Returns
        -------
        None

        Notes
        -----
        The connect transaction calls this helper while ``_connect_lock`` is
        held. Missing compatibility hooks and rollback failures are intentionally
        treated as best effort so they do not mask the original connection error.
        """
        if dispatcher is None or snapshot is None:
            return
        restorer = getattr(dispatcher, "_restore_session_state", None)
        if not callable(restorer):
            return
        try:
            restorer(snapshot)
        except Exception:  # noqa: BLE001 - rollback cleanup must remain best effort
            logger.debug(
                "Unable to restore BLE notification state after connect rollback.",
                exc_info=True,
            )

    def _begin_connect_transaction(self) -> _BLEConnectStateSnapshot:
        """Capture interface state and claim a provisional connection session.

        Returns
        -------
        _BLEConnectStateSnapshot
            Interface-owned state required to roll back the provisional session.

        Notes
        -----
        The caller must hold ``_connect_lock`` for the full connection
        transaction. Individual interface fields are synchronized by
        ``_state_lock`` inside this helper.
        """
        with self._state_lock:
            snapshot = _BLEConnectStateSnapshot(
                client=self.client,
                address=self.address,
                last_connection_request=getattr(self, "_last_connection_request", None),
                client_publish_pending=bool(
                    getattr(self, "_client_publish_pending", False)
                ),
                client_replacement_pending=bool(
                    getattr(self, "_client_replacement_pending", False)
                ),
                disconnect_notified=bool(getattr(self, "_disconnect_notified", False)),
                connection_session_epoch=getattr(self, "_connection_session_epoch", 0),
            )
            self._client_replacement_pending = (
                self.client is not None and not snapshot.client_publish_pending
            )
            self._client_publish_pending = True
            self._connection_session_epoch = snapshot.connection_session_epoch + 1
            return snapshot

    def _restore_connect_provisional_state(
        self, snapshot: _BLEConnectStateSnapshot
    ) -> None:
        """Restore provisional flags changed before connection establishment.

        Parameters
        ----------
        snapshot : _BLEConnectStateSnapshot
            State captured before provisional connection ownership was claimed.

        Returns
        -------
        None

        Notes
        -----
        The caller must hold ``_connect_lock``. The individual fields are
        restored atomically under ``_state_lock``.
        """
        with self._state_lock:
            self._disconnect_notified = snapshot.disconnect_notified
            self._client_publish_pending = snapshot.client_publish_pending
            self._client_replacement_pending = snapshot.client_replacement_pending
            self._connection_session_epoch = snapshot.connection_session_epoch

    def _adopt_connect_result(
        self,
        client: BLEClient,
        normalized_request: str | None,
        snapshot: _BLEConnectStateSnapshot,
    ) -> _BLEClientAdoption:
        """Adopt a new client under the state lock or reject it during shutdown.

        Parameters
        ----------
        client : BLEClient
            Newly established client candidate.
        normalized_request : str | None
            Normalized connection request used to update reconnect identity.
        snapshot : _BLEConnectStateSnapshot
            Pre-transaction state used when shutdown invalidates the result.

        Returns
        -------
        _BLEClientAdoption
            Previous client, resolved device address, and shutdown-abort status.

        Notes
        -----
        The caller must hold ``_connect_lock``. Interface state is mutated only
        while ``_state_lock`` is held.
        """
        device_address = getattr(
            getattr(client, "bleak_client", None), "address", None
        ) or getattr(client, "address", None)
        with self._state_lock:
            if self._closed or self._state_manager_is_closing():
                self._disconnect_notified = snapshot.disconnect_notified
                self._client_publish_pending = False
                self._client_replacement_pending = False
                self._connection_session_epoch = snapshot.connection_session_epoch
                return _BLEClientAdoption(
                    previous_client=None,
                    device_address=device_address,
                    aborted_for_shutdown=True,
                )

            previous_client = self.client
            self.address = device_address
            self.client = client
            self._disconnect_notified = False
            self._client_publish_pending = True
            normalized_device_address = sanitize_address(device_address or "")
            self._last_connection_request = (
                normalized_request
                if normalized_request is not None
                else normalized_device_address
            )
            return _BLEClientAdoption(
                previous_client=previous_client,
                device_address=device_address,
                aborted_for_shutdown=False,
            )

    def _restore_connect_after_handoff_failure(
        self, snapshot: _BLEConnectStateSnapshot, client: BLEClient
    ) -> None:
        """Restore the pre-connect client state after failed reference handoff.

        Parameters
        ----------
        snapshot : _BLEConnectStateSnapshot
            State captured before the connection transaction began.
        client : BLEClient
            Newly adopted client whose handoff failed.

        Returns
        -------
        None

        Notes
        -----
        The caller must hold ``_connect_lock``. If another thread has already
        replaced ``self.client``, the helper returns without modifying that newer
        ownership state. Otherwise restoration is atomic under ``_state_lock``.
        """
        with self._state_lock:
            if self.client is not client:
                return
            self.client = snapshot.client
            self.address = snapshot.address
            self._last_connection_request = snapshot.last_connection_request
            self._client_publish_pending = snapshot.client_publish_pending
            self._client_replacement_pending = snapshot.client_replacement_pending
            self._disconnect_notified = snapshot.disconnect_notified
            self._connection_session_epoch = snapshot.connection_session_epoch

    def _establish_and_update_client(
        self,
        address: str | None,
        normalized_request: str | None,
        address_key: str | None,
        *,
        pair_on_connect: bool = False,
        connect_timeout: float | None = None,
    ) -> tuple[BLEClient, str | None, str | None]:
        """Establish, provisionally adopt, and commit one BLE client transaction.

        Notes
        -----
        Must be called while holding ``_connect_lock``. Connection state and
        notification registration are snapshotted before orchestration so every
        failure path can restore the prior session without duplicating rollback
        logic.
        """
        notification_dispatcher, notification_snapshot = (
            self._snapshot_connect_notifications()
        )
        state_snapshot = self._begin_connect_transaction()

        try:
            client = self._establish_connection(
                address,
                pair_on_connect=pair_on_connect,
                connect_timeout=connect_timeout,
            )
        except Exception:
            self._restore_connect_provisional_state(state_snapshot)
            self._restore_connect_notifications(
                notification_dispatcher, notification_snapshot
            )
            raise

        adoption = self._adopt_connect_result(
            client, normalized_request, state_snapshot
        )
        if adoption.aborted_for_shutdown:
            logger.debug(
                "Discarding late BLE connection result during shutdown: %s",
                sanitize_address(adoption.device_address or "") or "unknown",
            )
            try:
                self._client_manager_safe_close_client(client)
            except Exception:  # noqa: BLE001 - best-effort cleanup during shutdown
                logger.debug(
                    "Error closing discarded late BLE connection result",
                    exc_info=True,
                )
            self._restore_connect_notifications(
                notification_dispatcher, notification_snapshot
            )
            raise self.BLEError(ERROR_INTERFACE_CLOSING)

        previous_client = adoption.previous_client
        if previous_client and previous_client is not client:
            try:
                self._client_manager_update_client_reference(client, previous_client)
            except Exception:
                self._restore_connect_after_handoff_failure(state_snapshot, client)
                self._restore_connect_notifications(
                    notification_dispatcher, notification_snapshot
                )
                if client is not state_snapshot.client:
                    try:
                        self._client_manager_safe_close_client(client)
                    except Exception:  # noqa: BLE001 - rollback cleanup is best effort
                        logger.debug(
                            "Error closing client after failed client handoff rollback",
                            exc_info=True,
                        )
                raise

        connected_device_key = (
            _addr_key(adoption.device_address) if adoption.device_address else None
        )
        connection_alias_key = (
            address_key
            if connected_device_key
            and address_key
            and address_key != connected_device_key
            else None
        )
        self._read_retry_count = 0
        return client, connected_device_key, connection_alias_key

    def _get_connected_client_status_locked(
        self, client: BLEClient
    ) -> tuple[bool, bool]:
        """Return owned/closing status for a connected client while holding `_state_lock`."""
        is_closing = self._state_manager_is_closing() or self._closed
        is_owned = (
            not self._closed
            and self.client is client
            and self._state_manager_is_connected()
            and client.isConnected()
        )
        return is_owned, is_closing

    def _get_connected_client_status(self, client: BLEClient) -> tuple[bool, bool]:
        """Return whether this interface owns `client` and whether shutdown began."""
        with self._state_lock:
            return self._get_connected_client_status_locked(client)

    def _has_lost_gate_ownership(self, *keys: str | None) -> bool:
        """Return whether any connected address key is now owned elsewhere."""
        return any(
            key is not None and _is_currently_connected_elsewhere(key, owner=self)
            for key in keys
        )

    def _raise_for_invalidated_connect_result(
        self,
        connected_client: BLEClient,
        connected_device_key: str | None,
        connection_alias_key: str | None,
        *,
        is_closing: bool,
        lost_gate_ownership: bool,
        restore_address: str | None,
        restore_last_connection_request: str | None,
    ) -> NoReturn:
        """Clean up a stale connect result and raise the appropriate BLEError."""
        stale_keys = self._sorted_address_keys(
            connected_device_key,
            connection_alias_key,
        )
        if not lost_gate_ownership:
            with self._state_lock:
                active_client = self.client
                active_keys = (
                    set(
                        self._sorted_address_keys(
                            _addr_key(self._extract_client_address(active_client)),
                            self._connection_alias_key,
                        )
                    )
                    if active_client is not None
                    and active_client is not connected_client
                    else set()
                )
            if active_keys:
                stale_keys = [key for key in stale_keys if key not in active_keys]
        if stale_keys:
            self._mark_address_keys_disconnected(*stale_keys)
        self._discard_invalidated_connected_client(
            connected_client,
            restore_address=restore_address,
            restore_last_connection_request=restore_last_connection_request,
        )
        if is_closing:
            raise self.BLEError(ERROR_INTERFACE_CLOSING)
        raise self.BLEError(CONNECTION_ERROR_LOST_OWNERSHIP)

    def _verify_and_publish_connected(
        self,
        connected_client: BLEClient,
        connected_device_key: str | None,
        connection_alias_key: str | None,
        *,
        restore_address: str | None,
        restore_last_connection_request: str | None,
    ) -> None:
        """Publish `_connected()` only if ownership is still valid at publish time."""
        lifecycle_controller = self._get_lifecycle_controller()
        verify_and_publish_connected = _resolve_declared_callable(
            lifecycle_controller,
            "_verify_and_publish_connected",
            "verify_and_publish_connected",
        )
        if verify_and_publish_connected is not None:
            verify_and_publish_connected(
                connected_client,
                connected_device_key,
                connection_alias_key,
                restore_address=restore_address,
                restore_last_connection_request=restore_last_connection_request,
            )

    def _emit_verified_connection_side_effects(
        self, connected_client: BLEClient
    ) -> None:
        """Emit reconnect signaling/logging only after verified connect publish."""
        lifecycle_controller = self._get_lifecycle_controller()
        emit_verified_connection_side_effects = _resolve_declared_callable(
            lifecycle_controller,
            "_emit_verified_connection_side_effects",
            "emit_verified_connection_side_effects",
        )
        if emit_verified_connection_side_effects is not None:
            emit_verified_connection_side_effects(connected_client)

    def _discard_invalidated_connected_client(
        self,
        client: BLEClient,
        *,
        restore_address: str | None = None,
        restore_last_connection_request: str | None = None,
    ) -> None:
        """Best-effort cleanup for a connected client invalidated before return.

        Parameters
        ----------
        client : BLEClient
            Connected client result that should no longer be published to
            callers because ownership was lost or shutdown began.
        restore_address : str | None
            Target identifier to restore when the connection result is discarded
            before ownership is finalized.
        restore_last_connection_request : str | None
            Normalized request identifier to restore when the connection result
            is discarded before ownership is finalized.
        """
        lifecycle_controller = self._get_lifecycle_controller()
        discard_invalidated_connected_client = _resolve_declared_callable(
            lifecycle_controller,
            "_discard_invalidated_connected_client",
            "discard_invalidated_connected_client",
        )
        if discard_invalidated_connected_client is not None:
            discard_invalidated_connected_client(
                client,
                restore_address=restore_address,
                restore_last_connection_request=restore_last_connection_request,
            )

    def _finalize_connection_gates(
        self,
        connected_client: BLEClient,
        connected_device_key: str | None,
        connection_alias_key: str | None,
    ) -> None:
        """Finalize post-connection gating by marking the relevant address keys as connected or undoing those marks if the
        interface closed or the client became stale during the connection process.

        When the provided client is still the active, connected client, this records the connected device and alias keys
        and persists the alias key on the interface. If the interface closed or the client is no longer current, any
        provisional gate marks are removed.

        Parameters
        ----------
        connected_client : BLEClient
            The BLE client instance that just completed connecting.
        connected_device_key : str | None
            Deterministic registry key derived from the connected device's address, or `None` if not applicable.
        connection_alias_key : str | None
            Optional alias key used when claiming connection gates, or `None` if not used.
        """
        lifecycle_controller = self._get_lifecycle_controller()
        finalize_connection_gates = _resolve_declared_callable(
            lifecycle_controller,
            "_finalize_connection_gates",
            "finalize_connection_gates",
        )
        if finalize_connection_gates is not None:
            finalize_connection_gates(
                connected_client,
                connected_device_key,
                connection_alias_key,
            )

    def _is_owned_connected_client(self, client: BLEClient) -> bool:
        """Return whether this interface still owns the provided connected client.

        Parameters
        ----------
        client : BLEClient
            Client instance to validate against the interface's current state.

        Returns
        -------
        bool
            `True` when the interface is not closed, still references `client`,
            and the state machine reports CONNECTED.
        """
        lifecycle_controller = self._get_lifecycle_controller()
        is_owned_connected_client = _resolve_declared_callable(
            lifecycle_controller,
            "_is_owned_connected_client",
            "is_owned_connected_client",
        )
        if is_owned_connected_client is not None:
            result = is_owned_connected_client(client)
            return result if isinstance(result, bool) else False
        return False

    # ---------------------------------------------------------------------
    # Main connection method
    # ---------------------------------------------------------------------

    def connect(
        self,
        address: str | None = None,
        *,
        pair: bool | None = None,
        connect_timeout: float | None = None,
    ) -> BLEClient:
        """Connect to a Meshtastic device over BLE by explicit address or by performing device discovery.

        Attempts to establish and return a connected BLE client for the requested device. If an existing compatible
        client is already connected, that client is returned. The method uses per-address gating to suppress duplicate
        concurrent connects and updates the interface's stored address and client reference on success. On failure any
        client opened during the attempt is closed before the error is propagated.

        Parameters
        ----------
        address : str | None
            BLE address or device name to connect to; if None, discovery is used to select a device. (Default value = None)
        pair : bool | None
            If True, request pairing during connect attempts for this call. If None,
            uses `self.pair_on_connect`. (Default value = None)
        connect_timeout : float | None
            Optional timeout override forwarded to BLE client construction and
            connection attempts for this call. When `None`, the pairing-aware
            default timeout is used.

        Returns
        -------
        BLEClient
            The connected BLE client for the selected device.

        Raises
        ------
        BLEInterface.BLEError
            If the interface is closing, if connection ownership is lost before
            the result can be returned, if connection is suppressed due to a
            recent connect elsewhere, or if the connection attempt fails.
        """
        # Fail fast if interface is closing before acquiring any locks
        self._validate_connection_preconditions()

        requested_identifier: str | None = None
        normalized_request: str | None = None
        if pair is not None and not isinstance(pair, bool):
            raise self.BLEError(ERROR_PAIR_BOOL)
        pair_on_connect = self.pair_on_connect if pair is None else pair
        self._validate_connect_timeout_override(
            connect_timeout,
            pair_on_connect=pair_on_connect,
        )
        with self._state_lock:
            self._last_connect_pair_override = pair
            self._last_connect_timeout_override = connect_timeout

        connected_client: BLEClient | None = None
        connected_device_key: str | None = None
        connection_alias_key: str | None = None
        provisional_keys: list[str] = []

        try:
            management_lock = self._management_lock
            management_idle_condition = self._management_idle_condition
            management_wait_started: float | None = None
            while True:
                requested_identifier = address if address is not None else self.address
                normalized_request = sanitize_address(requested_identifier)
                if address is not None and normalized_request is None:
                    raise self.BLEError(CONNECTION_ERROR_EMPTY_ADDRESS)
                # Management commands increment `_management_inflight` under
                # `_connect_lock` and then continue work outside interface
                # locks. Wait for current operations to settle, then re-check
                # once we have `_connect_lock` to avoid the handoff race.
                with management_lock:
                    while self._management_inflight > 0:
                        self._validate_connection_preconditions()
                        if management_wait_started is None:
                            management_wait_started = time.monotonic()
                        elapsed = time.monotonic() - management_wait_started
                        if elapsed >= _MANAGEMENT_CONNECT_WAIT_TIMEOUT_SECONDS:
                            logger.warning(
                                "Timed out waiting %.1fs for %d inflight management operation(s) before connect()",
                                elapsed,
                                self._management_inflight,
                            )
                            raise self.BLEError(ERROR_MANAGEMENT_CONNECTING)
                        remaining = _MANAGEMENT_CONNECT_WAIT_TIMEOUT_SECONDS - elapsed
                        management_idle_condition.wait(
                            timeout=min(
                                _MANAGEMENT_CONNECT_WAIT_POLL_SECONDS, remaining
                            )
                        )
                    # Reset timeout accounting once we are no longer blocked by
                    # in-flight management operations so future waits start fresh.
                    management_wait_started = None

                # Recompute potentially discovery-backed target state after
                # waiting for inflight management operations so retries do not
                # reuse stale resolution results.
                requested_registry_key = (
                    _addr_key(requested_identifier) if requested_identifier else None
                )
                preexisting_client = self._get_existing_client_if_valid(
                    normalized_request
                )
                if preexisting_client:
                    logger.debug("Already connected, skipping connect call.")
                    return preexisting_client
                resolved_connect_target = self._resolve_target_address_for_connect(
                    requested_identifier
                )
                concrete_registry_key = (
                    _addr_key(resolved_connect_target)
                    if resolved_connect_target
                    else None
                )
                connect_target_identifier = (
                    resolved_connect_target
                    if resolved_connect_target is not None
                    else requested_identifier
                )
                reservation_keys = self._sorted_address_keys(
                    requested_registry_key,
                    concrete_registry_key,
                )

                with contextlib.ExitStack() as stack:
                    for reservation_key in reservation_keys:
                        self._raise_if_duplicate_connect(
                            reservation_key,
                            requested_identifier=requested_identifier,
                            resolved_address=resolved_connect_target,
                        )
                    # Acquire per-address locks without holding _REGISTRY_LOCK
                    # to avoid lock-order inversion with paths that hold
                    # address locks and then mark ownership in registry state.
                    for reservation_key in reservation_keys:
                        addr_lock = stack.enter_context(
                            _addr_lock_context(reservation_key)
                        )
                        stack.enter_context(addr_lock)
                    for reservation_key in reservation_keys:
                        # Re-check after lock acquisition to close TOCTOU windows.
                        self._raise_if_duplicate_connect(
                            reservation_key,
                            requested_identifier=requested_identifier,
                            resolved_address=resolved_connect_target,
                        )

                    with self._connect_lock:
                        with management_lock:
                            if self._management_inflight > 0:
                                continue

                        # Re-check closing state inside connect_lock for extra safety
                        self._validate_connection_preconditions()

                        # Check for existing valid client
                        existing_client = self._get_existing_client_if_valid(
                            normalized_request
                        )
                        if existing_client:
                            logger.debug("Already connected, skipping connect call.")
                            return existing_client
                        with self._state_lock:
                            if self._client_publish_pending:
                                raise self.BLEError(ERROR_MANAGEMENT_CONNECTING)

                        # Establish new connection and update state
                        (
                            connected_client,
                            connected_device_key,
                            connection_alias_key,
                        ) = self._establish_and_update_client(
                            connect_target_identifier,
                            normalized_request,
                            requested_registry_key,
                            pair_on_connect=pair_on_connect,
                            connect_timeout=connect_timeout,
                        )
                        provisional_keys = self._sorted_address_keys(
                            requested_registry_key,
                            connected_device_key,
                            connection_alias_key,
                        )
                        self._mark_address_keys_connecting(*provisional_keys)

                # Finalize after the per-address lock scope exits to avoid
                # nested lock-order inversions when gate finalization
                # reacquires address locks.
                if connected_client is None:
                    raise self.BLEError(ERROR_NO_CLIENT_ESTABLISHED)
                self._finalize_connection_gates(
                    connected_client, connected_device_key, connection_alias_key
                )
                self._verify_and_publish_connected(
                    connected_client,
                    connected_device_key,
                    connection_alias_key,
                    restore_address=requested_identifier,
                    restore_last_connection_request=normalized_request,
                )
                return connected_client
        finally:
            self._clear_address_keys_connecting(*provisional_keys)

    def _handle_read_loop_disconnect(
        self, error_message: str, previous_client: BLEClient
    ) -> bool:
        """Return whether the receive loop should continue after a BLE client disconnect.

        Parameters
        ----------
        error_message : str
            Human-readable description of the disconnection cause.
        previous_client : BLEClient
            The BLE client that disconnected and may be closed.

        Returns
        -------
        bool
            `True` if the read loop should continue to allow auto-reconnect, `False` otherwise.
        """
        return self._get_receive_recovery_controller().handle_read_loop_disconnect(
            error_message,
            previous_client,
        )

    def _receive_from_radio_impl(self) -> None:
        """Run the main receive loop that reads FROMRADIO packets and delivers them to the packet handler.

        Waits for read or reconnection events, reads payloads from the active BLE client, forwards non-empty payloads to
        _handle_from_radio, and manages recovery paths (transient retries, disconnect handling, and thread restart)
        until the interface is closing or the receive loop is stopped.

        Raises
        ------
        Exception
            For unexpected errors in the receive thread that trigger recovery.
        """
        self._get_receive_recovery_controller().receive_from_radio_impl()

    def _recover_receive_thread(self, disconnect_reason: str) -> None:
        """Handle receive-thread failure and trigger guarded recovery.

        Parameters
        ----------
        disconnect_reason : str
            Reason string passed to disconnect handling for diagnostics.
        """
        self._get_receive_recovery_controller().recover_receive_thread(
            disconnect_reason
        )

    def _read_from_radio_with_retries(
        self,
        client: BLEClient,
        *,
        retry_on_empty: bool = True,
    ) -> bytes | None:
        """Read a non-empty payload from the FROMRADIO characteristic, retrying on repeated empty reads.

        Attempts repeated reads with backoff according to the empty-read policy; if a non-empty payload is read the
        suppressed-empty-read counter is reset. If all attempts return empty, a throttled warning is emitted.

        Parameters
        ----------
        client : BLEClient
            The connected BLE client to read from.
        retry_on_empty : bool
            Whether to retry and back off on empty reads. When False, performs
            a single short-duration poll read using
            ``BLEConfig.RECEIVE_WAIT_TIMEOUT``. (Default value = True)

        Returns
        -------
        bytes | None
            The payload bytes when a non-empty read occurs, or `None` if no non-empty payload was obtained after retries.
        """
        return self._get_receive_recovery_controller().read_from_radio_with_retries(
            client,
            retry_on_empty=retry_on_empty,
        )

    def _handle_transient_read_error(
        self, error: BleakError | BLEClient.BLEError
    ) -> None:
        """Apply the transient-read retry policy for a BLE read error.

        If the policy allows another retry, increments the internal retry counter and sleeps the configured delay to
        permit a retry. If retries are exhausted, resets the counter and raises
        BLEInterface.BLEError(ERROR_READING_BLE).

        Parameters
        ----------
        error : BleakError | BLEClient.BLEError
            The transient BLE read error that triggered the retry policy.

        Raises
        ------
        BLEInterface.BLEError
            When the retry policy is exhausted and the read should be treated as persistent.
        """
        self._get_receive_recovery_controller().handle_transient_read_error(error)

    def _log_empty_read_warning(self) -> None:
        """Emit a throttled warning when repeated empty FROMRADIO BLE reads are observed.

        If the cooldown period has elapsed, log a warning that an empty read retry limit was exceeded and include how
        many warnings were suppressed during the last cooldown window; otherwise increment the suppressed-warning
        counter and log a debug message with the current suppressed count and cooldown duration.
        """
        self._get_receive_recovery_controller().log_empty_read_warning()

    def _send_to_radio_impl(self, toRadio: mesh_pb2.ToRadio) -> None:
        """Send a protobuf ToRadio message over the TORADIO BLE characteristic.

        No-op if the serialized payload is empty or the interface is closing; on success the receive loop is signaled to process any response.

        Parameters
        ----------
        toRadio : mesh_pb2.ToRadio
            Protobuf message providing SerializeToString() for the outbound payload.

        Raises
        ------
        BLEError
            If the BLE write operation fails.
        """
        b: bytes = toRadio.SerializeToString()
        if not b:
            return

        write_successful = False
        is_disconnect_msg = toRadio.WhichOneof("payload_variant") == "disconnect"
        # Grab the current client under the shared lock, but perform the blocking write outside
        with self._state_lock:
            client = self.client
            publish_pending = self._client_publish_pending

        if publish_pending and not is_disconnect_msg:
            logger.debug(
                "Skipping TORADIO write while connect publication is pending verification."
            )
            return
        if not client or (self._is_connection_closing and not is_disconnect_msg):
            logger.debug(
                "Skipping TORADIO write: no BLE client or interface is closing."
            )
            return

        logger.debug("TORADIO write: %s", b.hex())
        try:
            # Intentional synchronous write-with-response: preserve in-order send
            # semantics and apply backpressure to callers when the device stalls.
            write_started = time.monotonic()
            client.write_gatt_char(
                TORADIO_UUID, b, response=True, timeout=GATT_IO_TIMEOUT
            )
            write_duration = time.monotonic() - write_started
            if write_duration > 1.0:
                logger.debug("Slow TORADIO write completed in %.2fs", write_duration)
            write_successful = True
        except (BleakError, BLEClient.BLEError, RuntimeError, OSError) as e:
            # Log detailed error information and wrap in our interface exception
            logger.debug(
                "Error during write operation: %s",
                type(e).__name__,
                exc_info=True,
            )
            raise self.BLEError(
                ERROR_WRITING_BLE,
                self.BLEError.WRITE_ERROR,
            ) from e

        if write_successful:
            # Brief delay to allow write to propagate before triggering read
            _sleep(BLEConfig.SEND_PROPAGATION_DELAY)
            self._set_thread_event(READ_TRIGGER_EVENT)

    # COMPAT_STABLE_SHIM: historical public BLEInterface API alias.
    # Keep callable without deprecation warning.
    def disconnect(self, timeout: float | None = None) -> None:
        """Compatibility alias for callers that expect an explicit disconnect API.

        Parameters
        ----------
        timeout : float | None
            Optional total shutdown budget in seconds, forwarded to ``close()``.
        """
        self.close(timeout=timeout)

    def close(self, timeout: float | None = None) -> None:
        """Shut down the BLE interface and release associated resources.

        Parameters
        ----------
        timeout : float | None
            Optional total close-budget in seconds. When provided, the budget is
            sliced across blocking shutdown phases (management wait, receive
            thread join, ``MeshInterface.close``, disconnect-notification wait,
            and client disconnect/close). When ``None``, lifecycle defaults are
            used for each stage. (Default value = None)
        """
        if timeout is not None:
            if isinstance(timeout, bool) or not isinstance(timeout, numbers.Real):
                raise self.BLEError(ERROR_CLOSE_TIMEOUT)
            if not math.isfinite(timeout) or timeout < 0:
                raise self.BLEError(ERROR_CLOSE_TIMEOUT)
            timeout = float(timeout)
        # Clear any provisional connecting state before shutdown to prevent
        # orphaned gate claims when connection threads are forcibly terminated.
        # This clears all provisional keys owned by this interface, not just
        # the current self.address value.
        try:
            _clear_connecting_for_owner(self)
        except Exception:  # noqa: BLE001
            # Best-effort cleanup; don't let gate cleanup errors prevent shutdown
            logger.debug(
                "Failed to clear connecting gates for owner during shutdown",
                exc_info=True,
            )
            try:
                self._clear_address_keys_connecting(self.address)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "Failed to clear address connecting keys during shutdown",
                    exc_info=True,
                )
        lifecycle_controller = self._get_lifecycle_controller()
        close = _resolve_declared_callable(lifecycle_controller, "_close", "close")
        if close is not None:
            close_kwargs: dict[str, float | None] = {
                "management_shutdown_wait_timeout": _MANAGEMENT_SHUTDOWN_WAIT_TIMEOUT_SECONDS,
                "management_wait_poll_seconds": _MANAGEMENT_CONNECT_WAIT_POLL_SECONDS,
                "timeout": timeout,
            }
            try:
                close(**close_kwargs)
            except TypeError as exc:
                if not _is_unexpected_keyword_error(exc, "timeout"):
                    raise
                close_kwargs.pop("timeout", None)
                close(**close_kwargs)

    def _get_publishing_thread(self) -> object:
        """Resolve the publishing-thread adapter used for compatibility events.

        Returns
        -------
        object
            Instance override when configured, otherwise module-level
            ``publishingThread``.
        """
        if self._publishing_thread_override is not None:
            return self._publishing_thread_override
        return publishingThread

    def _wait_for_disconnect_notifications(self, timeout: float | None = None) -> None:
        """Wait up to timeout seconds for the publishing thread to flush pending publish callbacks.

        Requests a flush on the publishing thread and waits for a flush event. If the wait times out and the publishing
        thread is still alive, a debug message is logged; if the publishing thread is not running when the timeout
        elapses, the publish queue is drained synchronously on the current thread. Exceptions raised while requesting
        the flush are caught and logged by the error handler.

        Parameters
        ----------
        timeout : float | None
            Maximum seconds to wait for the publish queue to flush. If `None`, uses `DISCONNECT_TIMEOUT_SECONDS`. (Default value = None)
        """
        self._get_compatibility_publisher().wait_for_disconnect_notifications(timeout)

    def _disconnect_and_close_client(
        self, client: BLEClient, *, timeout: float | None = None
    ) -> None:
        """Ensure the given BLE client is disconnected and its resources are released.

        Parameters
        ----------
        client : BLEClient
            BLE client to disconnect and close; operation is idempotent and safe to call on already-closed clients.
        """
        lifecycle_controller = self._get_lifecycle_controller()
        disconnect_and_close_client = _resolve_declared_callable(
            lifecycle_controller,
            "_disconnect_and_close_client",
            "disconnect_and_close_client",
        )
        if disconnect_and_close_client is not None:
            try:
                disconnect_and_close_client(client, timeout=timeout)
            except TypeError as exc:
                if not _is_unexpected_keyword_error(exc, "timeout"):
                    raise
                disconnect_and_close_client(client)

    def _drain_publish_queue(self, flush_event: Event) -> None:
        """Drain and run pending publish callbacks on the current thread until the queue is empty or the provided event is set.

        Each callback is executed via the interface's error handler; exceptions raised by callbacks are caught and logged so draining continues.

        Parameters
        ----------
        flush_event : Event
            When set, stop draining immediately.
        """
        self._get_compatibility_publisher().drain_publish_queue(flush_event)

    def _publish_connection_status(self, *, connected: bool) -> None:
        """Enqueue legacy connection-status publication via compatibility service.

        Parameters
        ----------
        connected : bool
            ``True`` when connected; ``False`` when disconnected.

        Returns
        -------
        None
            Publication is best-effort and performed for side effects.
        """
        self._get_compatibility_publisher().publish_connection_status(
            connected=connected
        )

    def _disconnected(self) -> None:
        """Publish the legacy meshtastic.connection.status event indicating the interface is disconnected.

        This enqueues a publish of the connection status for backward compatibility; exceptions raised while queueing or publishing are suppressed.
        """
        super()._disconnected()
        self._publish_connection_status(connected=False)

    def _connected(self, *, expected_session_epoch: int | None = None) -> None:
        """Mark connected and publish compatibility status for the active session.

        Parameters
        ----------
        expected_session_epoch : int | None
            Optional session epoch guard. When provided, stale calls are ignored
            if the current connection session no longer matches.
        """
        if expected_session_epoch is not None:
            with self._state_lock:
                current_session_epoch = getattr(self, "_connection_session_epoch", 0)
                if current_session_epoch != expected_session_epoch:
                    logger.debug(
                        "Skipping stale _connected() call (expected epoch=%s current epoch=%s).",
                        expected_session_epoch,
                        current_session_epoch,
                    )
                    return
                super()._connected()
        else:
            super()._connected()
        if expected_session_epoch is not None:
            with self._state_lock:
                current_session_epoch = getattr(self, "_connection_session_epoch", 0)
                if current_session_epoch != expected_session_epoch:
                    logger.debug(
                        "Skipping stale connection-status publish (expected epoch=%s current epoch=%s).",
                        expected_session_epoch,
                        current_session_epoch,
                    )
                    return
        self._publish_connection_status(connected=True)
