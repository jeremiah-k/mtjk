"""BLE connection management and validation."""

import logging
import math
import numbers
import re
import sys
from collections.abc import Callable
from threading import Event, RLock
from typing import TYPE_CHECKING, cast

from bleak.backends.device import BLEDevice
from bleak.exc import BleakDBusError, BleakDeviceNotFoundError, BleakError

from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.constants import (
    AWAIT_TIMEOUT_BUFFER_SECONDS,
    BLECLIENT_ERROR_ALREADY_CONNECTED,
    BLECLIENT_ERROR_CANNOT_CONNECT_WHILE_CLOSING,
    CONNECTION_ERROR_CLIENT_DISCONNECTED_DURING_FINALIZATION,
    CONNECTION_ERROR_EMPTY_ADDRESS,
    CONNECTION_ERROR_INVALIDATED_BY_CONCURRENT_DISCONNECT,
    CONNECTION_ERROR_STATE_TRANSITION_INVALIDATED,
    DISCONNECT_TIMEOUT_SECONDS,
    ERROR_INVALID_CONNECT_TIMEOUT,
    RECONNECTED_EVENT,
    BLEConfig,
)
from meshtastic.interfaces.ble.coordination import ThreadCoordinator, ThreadLike
from meshtastic.interfaces.ble.discovery import _looks_like_ble_address
from meshtastic.interfaces.ble.errors import (
    BLEAddressMismatchError,
    BLEConnectionTimeoutError,
    BLEDBusTransportError,
)
from meshtastic.interfaces.ble.errors import (
    BLEDeviceNotFoundError as MeshtasticBLEDeviceNotFoundError,
)
from meshtastic.interfaces.ble.errors import (
    BLEErrorHandler,
)
from meshtastic.interfaces.ble.state import BLEStateManager, ConnectionState
from meshtastic.interfaces.ble.utils import (
    _is_unconfigured_mock_callable,
    _is_unconfigured_mock_member,
    _is_unexpected_keyword_error,
    _thread_start_probe,
    sanitize_address,
)

if TYPE_CHECKING:
    from bleak import BleakClient as BleakRootClient

    from meshtastic.interfaces.ble.discovery import DiscoveryManager
    from meshtastic.interfaces.ble.interface import BLEInterface

logger = logging.getLogger("meshtastic.ble")
_DEVICE_NOT_FOUND_MESSAGE_RE: re.Pattern[str] = re.compile(
    r"(?:"
    r"\bcould not find (?:the )?(?:device|peripheral)\b"
    r"(?!\s+(?:service|characteristic)\b)(?:\W|$)|"
    r"\b(?:device|peripheral)\b"
    r"(?:(?!\b(?:service|characteristic)\b).){0,40}\bnot found\b"
    r")"
)
_CONNECT_TIMEOUT_INVALID_MSG: str = (
    "connect_timeout must be a finite positive number of seconds."
)
_CONNECT_TIMEOUT_FALLBACK_SECONDS: float = 10.0
_DISPATCH_MISSING: object = object()
_STALE_BLUEZ_DBUS_ERROR_NAMES: frozenset[str] = frozenset(
    {
        "org.bluez.error.alreadyconnected",
        "org.bluez.error.inprogress",
    }
)
_STALE_BLUEZ_DETAIL_TOKENS: tuple[str, ...] = (
    # These phrases appear in BlueZ/DBus transport errors when the adapter has a
    # stale session or overlapping connect operation. Keep these specific to
    # avoid false positives from generic "busy" wording in unrelated failures.
    "already connected",
    "operation already in progress",
    "device or resource busy",
)
_STALE_BLUEZ_FALLBACK_MESSAGE_TOKENS: tuple[str, ...] = (
    "org.bluez.error.alreadyconnected",
    "org.bluez.error.inprogress",
    "already connected",
    "operation already in progress",
    "device or resource busy",
)


def _is_device_not_found_error(err: Exception) -> bool:
    """Return True when an exception indicates the target BLE device was not found."""
    if isinstance(err, BleakDeviceNotFoundError):
        return True
    message = str(err).casefold()
    return bool(message) and _DEVICE_NOT_FOUND_MESSAGE_RE.search(message) is not None


def _is_mock_instance(target: object) -> bool:
    """Return whether ``target`` is a unittest mock instance.

    Parameters
    ----------
    target : object
        Candidate object inspected for mock-instance type.

    Returns
    -------
    bool
        ``True`` when ``target`` is a ``Mock`` or ``NonCallableMock``.
    """
    try:
        from unittest.mock import Mock, NonCallableMock
    except ImportError:  # pragma: no cover - stdlib import should always exist
        return False
    return isinstance(target, (Mock, NonCallableMock))


def _run_safe_cleanup(
    func: Callable[[], object],
    cleanup_name: str,
    safe_cleanup_hook: Callable[..., object] | None,
) -> bool:
    """Run cleanup through hook when available, otherwise inline best effort.

    Parameters
    ----------
    func : Callable[[], object]
        Cleanup callable to execute.
    cleanup_name : str
        Cleanup operation label used in debug logs.
    safe_cleanup_hook : Callable[..., object] | None
        Optional hook resolved from error-handler compatibility surface.

    Returns
    -------
    bool
        ``True`` when cleanup was handled/executed successfully, otherwise
        ``False``.
    """
    cleanup_ran = False
    cleanup_invoked = False

    def _tracked_cleanup() -> object:
        nonlocal cleanup_invoked, cleanup_ran
        cleanup_invoked = True
        result = func()
        cleanup_ran = True
        return result

    handled = False
    if safe_cleanup_hook is not None:
        try:
            handled = bool(
                safe_cleanup_hook(func=_tracked_cleanup, cleanup_name=cleanup_name)
            )
        except TypeError as exc:
            if _is_unexpected_keyword_error(
                exc, "func"
            ) or _is_unexpected_keyword_error(exc, "cleanup_name"):
                try:
                    handled = bool(safe_cleanup_hook(_tracked_cleanup, cleanup_name))
                except Exception:  # noqa: BLE001 - cleanup path must stay best-effort
                    logger.debug(
                        "Error running safe_cleanup hook for %s",
                        cleanup_name,
                        exc_info=True,
                    )
            else:
                logger.debug(
                    "Error running safe_cleanup hook for %s",
                    cleanup_name,
                    exc_info=True,
                )
        except Exception:  # noqa: BLE001 - cleanup path must stay best-effort
            logger.debug(
                "Error running safe_cleanup hook for %s",
                cleanup_name,
                exc_info=True,
            )
        if handled or cleanup_ran:
            return True
        if cleanup_invoked:
            return False
    try:
        _tracked_cleanup()
    except Exception:  # noqa: BLE001 - cleanup path must stay best-effort
        return False
    return True


class ConnectionValidator:
    """Encapsulate connection pre-checks and reuse logic."""

    def __init__(
        self,
        state_manager: BLEStateManager,
        state_lock: RLock,
        error_class: type[Exception],
    ) -> None:
        """Create a ConnectionValidator that enforces pre-connection checks for BLE operations.

        Parameters
        ----------
        state_manager : BLEStateManager
            Manager for BLE connection state and transitions.
        state_lock : RLock
            Reentrant lock for synchronizing access to shared BLE state.
        error_class : type[Exception]
            Exception class raised when validation fails.
        """
        self.state_manager = state_manager
        self.state_lock = state_lock
        self.BLEError = error_class

    def _validate_connection_request(self) -> None:
        """Validate that a new BLE connection may be started.

        Raises
        ------
        BLEError
            If connections are not permitted. If the interface is closing the error message will be
            "Cannot connect while interface is closing". If a connection is already established or in progress
            the error message will be "Already connected or connection in progress".
        """
        with self.state_lock:
            can_connect = self.state_manager._can_connect
            is_closing = self.state_manager._is_closing
            if not can_connect:
                if is_closing:
                    raise self.BLEError(BLECLIENT_ERROR_CANNOT_CONNECT_WHILE_CLOSING)
                raise self.BLEError(BLECLIENT_ERROR_ALREADY_CONNECTED)

    def validate_connection_request(self) -> None:
        # Internal adapter alias for collaborator migration; delegates to _validate_connection_request.
        """Validate whether a BLE connection request may proceed.

        Parameters
        ----------
        None
            This compatibility wrapper does not accept additional parameters.

        Returns
        -------
        None
            Returns ``None`` when validation passes.

        Raises
        ------
        BLEError
            Propagated from :meth:`_validate_connection_request` when the
            interface cannot accept a new connection request.

        See Also
        --------
        _validate_connection_request
            Internal validation implementation.
        """
        self._validate_connection_request()

    @staticmethod
    def _client_is_connected(client: BLEClient | None) -> bool:
        """Resolve connected state from public/legacy BLEClient compatibility members."""
        if client is None:
            return False
        for candidate_name in ("isConnected", "is_connected", "_is_connected"):
            candidate = getattr(client, candidate_name, None)
            if callable(candidate):
                if _is_unconfigured_mock_callable(candidate):
                    continue
                connected = candidate()
                if isinstance(connected, bool):
                    return connected
                continue
            if isinstance(candidate, bool) and not _is_unconfigured_mock_member(
                candidate
            ):
                return candidate
        return False

    def _check_existing_client(
        self,
        client: BLEClient | None,
        normalized_request: str | None,
        last_connection_request: str | None,
    ) -> bool:
        """Return whether the given BLE client corresponds to the requested or a known device address.

        Considers the provided normalized_request, last_connection_request, the
        client object's cached address, and the client's bleak address. If
        `normalized_request` is `None`, any connected client is treated as acceptable.

        Parameters
        ----------
        client : BLEClient | None
            The BLE client to verify.
        normalized_request : str | None
            Desired target identifier. Separator/case variants are normalized
            internally; when `None` any connected client matches.
        last_connection_request : str | None
            The last sanitized connection request to include among known targets.

        Returns
        -------
        bool
            `True` if the client is connected and its normalized address equals
            the normalized request or one of the known targets, `False` otherwise.
        """
        if not self._client_is_connected(client):
            return False
        normalized_request_key = sanitize_address(normalized_request)
        client_address = sanitize_address(getattr(client, "address", None))
        bleak_client = getattr(client, "bleak_client", None)
        bleak_address = getattr(bleak_client, "address", None)
        normalized_known_targets = {
            t
            for t in (
                sanitize_address(last_connection_request),
                client_address,
                sanitize_address(bleak_address),
            )
            if t is not None
        }
        return (
            normalized_request_key is None
            or normalized_request_key in normalized_known_targets
        )

    def check_existing_client(
        self,
        client: BLEClient | None,
        normalized_request: str | None,
        last_connection_request: str | None,
    ) -> bool:
        # Internal adapter alias for collaborator migration; delegates to _check_existing_client.
        """Check whether an existing client still matches the requested target.

        Parameters
        ----------
        client : BLEClient | None
            Client candidate to validate.
        normalized_request : str | None
            Normalized requested target identifier.
        last_connection_request : str | None
            Last normalized target requested by the interface.

        Returns
        -------
        bool
            ``True`` when the candidate is connected and matches the request
            context; otherwise ``False``.

        See Also
        --------
        _check_existing_client
            Internal compatibility-aware matching implementation.
        """
        return self._check_existing_client(
            client,
            normalized_request,
            last_connection_request,
        )


class ClientManager:
    """Helper for creating, connecting, and closing BLEClient instances."""

    def __init__(
        self,
        state_manager: BLEStateManager,
        state_lock: RLock,
        thread_coordinator: ThreadCoordinator,
        error_handler: "BLEErrorHandler",
    ) -> None:
        """Initialize a ClientManager with the managers, synchronization primitive, and error handler required to manage BLEClient lifecycle.

        Parameters
        ----------
        state_manager : BLEStateManager
            Tracks BLE connection state and the current client.
        state_lock : RLock
            Reentrant lock protecting access to shared BLE state.
        thread_coordinator : ThreadCoordinator
            Creates and manages background threads for client cleanup.
        error_handler : 'BLEErrorHandler'
            Performs safe client shutdown and handles or suppresses errors during close.
        """
        self.state_manager = state_manager
        self.state_lock = state_lock
        self.thread_coordinator = thread_coordinator
        self.error_handler = error_handler

    def _thread_create_thread(
        self,
        *,
        target: Callable[..., object],
        args: tuple[object, ...],
        name: str,
        daemon: bool,
    ) -> ThreadLike:
        """Create thread via public API with underscore fallback for test doubles."""
        create_thread = getattr(self.thread_coordinator, "create_thread", None)
        legacy_create_thread = getattr(self.thread_coordinator, "_create_thread", None)
        valid_create_thread = (
            create_thread
            if callable(create_thread)
            and not _is_unconfigured_mock_callable(create_thread)
            else None
        )
        valid_legacy_create_thread = (
            legacy_create_thread
            if callable(legacy_create_thread)
            and not _is_unconfigured_mock_callable(legacy_create_thread)
            else None
        )
        if valid_create_thread is not None:
            return cast(
                ThreadLike,
                valid_create_thread(
                    target=target,
                    args=args,
                    name=name,
                    daemon=daemon,
                ),
            )
        if valid_legacy_create_thread is not None:
            return cast(
                ThreadLike,
                valid_legacy_create_thread(
                    target=target,
                    args=args,
                    name=name,
                    daemon=daemon,
                ),
            )
        raise AttributeError(
            "Thread coordinator is missing create_thread/_create_thread"
        )

    def _thread_start_thread(self, thread: ThreadLike) -> None:
        """Start thread via public API with underscore fallback for test doubles."""
        start_thread = getattr(self.thread_coordinator, "start_thread", None)
        legacy_start_thread = getattr(self.thread_coordinator, "_start_thread", None)
        valid_start_thread = (
            start_thread
            if callable(start_thread)
            and not _is_unconfigured_mock_callable(start_thread)
            else None
        )
        valid_legacy_start_thread = (
            legacy_start_thread
            if callable(legacy_start_thread)
            and not _is_unconfigured_mock_callable(legacy_start_thread)
            else None
        )
        if valid_start_thread is not None:
            valid_start_thread(thread)
            return
        if valid_legacy_start_thread is not None:
            valid_legacy_start_thread(thread)
            return
        raise AttributeError("Thread coordinator is missing start_thread/_start_thread")

    def _create_client(
        self,
        device: BLEDevice | str,
        disconnect_callback: Callable[["BleakRootClient"], None],
        *,
        pair_on_connect: bool = False,
        connect_timeout: float | None = None,
    ) -> BLEClient:
        """Create a BLEClient bound to the given device target and register a disconnect callback.

        Parameters
        ----------
        device : BLEDevice | str
            Target BLE device object or address to bind the client to.
        disconnect_callback : 'Callable'
            Callable invoked when the client disconnects.
        pair_on_connect : bool
            If True, initialize the underlying Bleak client with pairing enabled so
            connect attempts request pairing as part of connection setup. (Default value = False)
        connect_timeout : float | None
            Timeout in seconds forwarded to the underlying Bleak client
            constructor. If None, BLEConfig.CONNECTION_TIMEOUT is used.

        Returns
        -------
        'BLEClient'
            A BLEClient instance bound to `device` with the disconnect callback configured.
        """
        return BLEClient(
            device,
            disconnected_callback=disconnect_callback,
            timeout=(
                connect_timeout
                if connect_timeout is not None
                else BLEConfig.CONNECTION_TIMEOUT
            ),
            pair=pair_on_connect,
        )

    def create_client(
        self,
        device: BLEDevice | str,
        disconnect_callback: Callable[["BleakRootClient"], None],
        *,
        pair_on_connect: bool = False,
        connect_timeout: float | None = None,
    ) -> BLEClient:
        # Internal adapter alias for collaborator migration; delegates to _create_client.
        """Create a BLE client through the compatibility wrapper surface.

        Parameters
        ----------
        device : BLEDevice | str
            Target BLE device object or address string.
        disconnect_callback : Callable[[BleakRootClient], None]
            Callback invoked when the underlying Bleak client disconnects.
        pair_on_connect : bool
            Whether the created client should request pairing on connect.
        connect_timeout : float | None
            Optional connect timeout forwarded to the BLE client constructor.

        Returns
        -------
        BLEClient
            Newly created BLE client instance.

        See Also
        --------
        _create_client
            Internal client-construction implementation.
        """
        return self._create_client(
            device,
            disconnect_callback,
            pair_on_connect=pair_on_connect,
            connect_timeout=connect_timeout,
        )

    def _connect_client(self, client: BLEClient, timeout: float | None = None) -> None:
        """Connect the provided BLEClient and ensure its GATT services are populated.

        If the client's discovered services are not available immediately after connecting, service discovery will be requested so characteristics and services are usable.

        Parameters
        ----------
        client : BLEClient
            BLEClient to connect and prepare for use.
        timeout : float | None
            Maximum seconds to wait for the connection; if None, uses BLEConfig.CONNECTION_TIMEOUT. (Default value = None)
        """
        connect_timeout = (
            timeout if timeout is not None else BLEConfig.CONNECTION_TIMEOUT
        )
        # Give the underlying BLE timeout a chance to fail first with clearer context.
        await_timeout = connect_timeout + AWAIT_TIMEOUT_BUFFER_SECONDS
        client.connect(
            await_timeout=await_timeout,
            timeout=connect_timeout,
        )
        try:
            services = getattr(client.bleak_client, "services", None)
        except BleakError as exc:
            logger.debug(
                "BLE services property raised right after connect; forcing discovery: %s",
                exc,
            )
            services = None
        get_characteristic = getattr(services, "get_characteristic", None)
        if not services or not callable(get_characteristic):
            logger.debug(
                "BLE services not available immediately after connect; getting services"
            )
            client._get_services()

    def connect_client(self, client: BLEClient, timeout: float | None = None) -> None:
        # Internal adapter alias for collaborator migration; delegates to _connect_client.
        """Connect a BLE client and ensure service readiness.

        Parameters
        ----------
        client : BLEClient
            Client instance to connect.
        timeout : float | None
            Optional connect timeout in seconds.

        Returns
        -------
        None
            Returns ``None`` when connection preparation succeeds.

        Raises
        ------
        BLEError
            Propagated from the underlying client connect/discovery flow.

        See Also
        --------
        _connect_client
            Internal connect implementation with service-readiness checks.
        """
        self._connect_client(client, timeout=timeout)

    def _update_client_reference(
        self,
        new_client: BLEClient,
        old_client: BLEClient | None,
    ) -> None:
        """Schedule asynchronous close of a previous BLE client when replacing it.

        If `old_client` is provided and is a different object than `new_client`, schedules `_safe_close_client(old_client)` to run in a background daemon thread so the caller is not blocked.

        Parameters
        ----------
        new_client : BLEClient
            The client that will become active.
        old_client : BLEClient | None
            The previous client to close if different from `new_client`.
        """
        # Compute the decision under lock, but start the thread after releasing
        # to avoid holding the lock during thread creation/start
        should_close = False
        client_to_close: BLEClient | None = None
        with self.state_lock:
            if old_client and old_client is not new_client:
                should_close = True
                client_to_close = old_client

        if should_close and client_to_close is not None:
            try:
                close_thread = self._thread_create_thread(
                    target=self._safe_close_client,
                    args=(client_to_close,),
                    name="BLEClientClose",
                    daemon=True,
                )
                self._thread_start_thread(close_thread)
                thread_ident, thread_is_alive = _thread_start_probe(close_thread)
                if thread_ident is None and not thread_is_alive:
                    logger.debug(
                        "BLEClientClose thread failed to start (ident=%s, alive=%s); closing retired client inline.",
                        thread_ident,
                        thread_is_alive,
                    )
                    self._safe_close_client(client_to_close)
            except (SystemExit, KeyboardInterrupt):  # pylint: disable=W0706
                self._safe_close_client(client_to_close)
                raise
            except Exception:
                logger.debug(
                    "Failed to schedule BLEClientClose thread; closing retired client inline.",
                    exc_info=True,
                )
                self._safe_close_client(client_to_close)

    def update_client_reference(
        self,
        new_client: BLEClient,
        old_client: BLEClient | None,
    ) -> None:
        # Internal adapter alias for collaborator migration; delegates to _update_client_reference.
        """Update active-client reference and schedule old-client cleanup.

        Parameters
        ----------
        new_client : BLEClient
            Client that should remain active.
        old_client : BLEClient | None
            Previous client to close asynchronously when different from
            ``new_client``.

        Returns
        -------
        None
            Returns ``None`` after scheduling or performing cleanup.

        See Also
        --------
        _update_client_reference
            Internal implementation handling async close scheduling.
        """
        self._update_client_reference(new_client, old_client)

    def _safe_close_client(
        self,
        client: BLEClient,
        event: Event | None = None,
        *,
        disconnect_timeout: float | None = None,
    ) -> None:
        """Attempt to disconnect and close the given BLE client, suppressing any errors and optionally signal completion.

        Parameters
        ----------
        client : BLEClient
            BLE client to disconnect and close.
        event : Event | None
            Optional Event that will be set after cleanup completes. (Default value = None)
        disconnect_timeout : float | None
            Optional maximum seconds passed to disconnect and close-time
            disconnect operations.
        """
        is_finalizing = getattr(sys, "is_finalizing", None)
        skip_disconnect = bool(is_finalizing()) if callable(is_finalizing) else False
        safe_cleanup_hook = getattr(self.error_handler, "safe_cleanup", None)
        if not callable(safe_cleanup_hook) or _is_unconfigured_mock_callable(
            safe_cleanup_hook
        ):
            safe_cleanup_hook = getattr(self.error_handler, "_safe_cleanup", None)
        if not callable(safe_cleanup_hook) or _is_unconfigured_mock_callable(
            safe_cleanup_hook
        ):
            safe_cleanup_hook = None

        try:
            if (
                not skip_disconnect
                and not getattr(client, "_closed", False)
                and getattr(client, "bleak_client", None)
            ):
                is_connected = False
                for probe_name in ("is_connected", "isConnected", "_is_connected"):
                    is_connected_probe = getattr(client, probe_name, None)
                    if callable(
                        is_connected_probe
                    ) and not _is_unconfigured_mock_callable(is_connected_probe):
                        try:
                            is_connected = bool(is_connected_probe())
                        except (
                            Exception
                        ):  # noqa: BLE001 - shutdown must remain best effort
                            logger.debug(
                                "Failed to read BLE client connected state via %s during shutdown.",
                                probe_name,
                                exc_info=True,
                            )
                        if is_connected:
                            break
                    elif isinstance(
                        is_connected_probe, bool
                    ) and not _is_unconfigured_mock_member(is_connected_probe):
                        is_connected = is_connected_probe
                        if is_connected:
                            break
                if is_connected:
                    effective_disconnect_timeout = (
                        DISCONNECT_TIMEOUT_SECONDS
                        if disconnect_timeout is None
                        else disconnect_timeout
                    )

                    def _disconnect_with_timeout() -> None:
                        try:
                            client.disconnect(
                                await_timeout=effective_disconnect_timeout
                            )
                        except TypeError as exc:
                            if not _is_unexpected_keyword_error(exc, "await_timeout"):
                                raise
                            client.disconnect()

                    _run_safe_cleanup(
                        _disconnect_with_timeout,
                        "client disconnect",
                        safe_cleanup_hook,
                    )
                else:
                    logger.debug(
                        "Skipping BLE client disconnect during shutdown: client is not connected."
                    )
            elif skip_disconnect:
                logger.debug(
                    "Skipping BLE client disconnect during interpreter finalization."
                )
            if not skip_disconnect:
                close_error: TypeError | None = None

                def _close_with_timeout() -> None:
                    nonlocal close_error
                    try:
                        client.close(timeout=disconnect_timeout)
                    except TypeError as exc:
                        if not _is_unexpected_keyword_error(exc, "timeout"):
                            close_error = exc
                            raise
                        client.close()

                _run_safe_cleanup(
                    _close_with_timeout,
                    "client close",
                    safe_cleanup_hook,
                )
                if close_error is not None:
                    raise close_error
            else:
                logger.debug(
                    "Skipping BLE client close during interpreter finalization."
                )
        finally:
            if event:
                event.set()

    def safe_close_client(
        self,
        client: BLEClient,
        event: Event | None = None,
        *,
        disconnect_timeout: float | None = None,
    ) -> None:
        # Internal adapter alias for collaborator migration; delegates to _safe_close_client.
        """Close a BLE client using best-effort shutdown semantics.

        Parameters
        ----------
        client : BLEClient
            Client instance to close.
        event : Event | None
            Optional completion event set after cleanup finishes.
        disconnect_timeout : float | None
            Optional maximum seconds passed to disconnect and close-time
            disconnect operations.

        Returns
        -------
        None
            Returns ``None`` after best-effort cleanup.

        See Also
        --------
        _safe_close_client
            Internal close implementation with guarded cleanup.
        """
        if event is None:
            try:
                self._safe_close_client(
                    client,
                    disconnect_timeout=disconnect_timeout,
                )
            except TypeError as exc:
                if not _is_unexpected_keyword_error(exc, "disconnect_timeout"):
                    raise
                self._safe_close_client(client)
            return
        try:
            self._safe_close_client(
                client,
                event=event,
                disconnect_timeout=disconnect_timeout,
            )
        except TypeError as exc:
            if not _is_unexpected_keyword_error(exc, "disconnect_timeout"):
                raise
            self._safe_close_client(client, event=event)


class ConnectionOrchestrator:
    """Coordinate discovery, validation, and notification setup for new connections."""

    def __init__(
        self,
        interface: "BLEInterface",
        validator: ConnectionValidator,
        client_manager: ClientManager,
        discovery_manager: "DiscoveryManager",
        state_manager: BLEStateManager,
        state_lock: RLock,
        thread_coordinator: ThreadCoordinator,
    ) -> None:
        """Coordinate BLE connection orchestration by wiring together the interface, validators, client lifecycle manager, discovery manager, and synchronization primitives.

        Parameters
        ----------
        interface : 'BLEInterface'
            BLE interface used for device discovery and low-level operations.
        validator : ConnectionValidator
            Performs pre-connection validation and reuse checks.
        client_manager : ClientManager
            Creates, connects, and safely closes BLE clients.
        discovery_manager : 'DiscoveryManager'
            Discovers target devices when direct connect fails or is not specified.
        state_manager : BLEStateManager
            Tracks and updates the BLE connection state machine.
        state_lock : RLock
            Reentrant lock protecting access to shared BLE state during transitions.
        thread_coordinator : ThreadCoordinator
            Schedules background tasks and signals threading events (e.g., reconnection notifications).
        """
        self.interface = interface
        self.validator = validator
        self.client_manager = client_manager
        self.discovery_manager = discovery_manager
        self.state_manager = state_manager
        self.state_lock = state_lock
        self.thread_coordinator = thread_coordinator

    def _dispatch_public_or_underscore(
        self,
        *,
        target: object,
        public_name: str,
        underscore_name: str,
        prefer_instance_type: type[object] | None = None,
        call_member: bool,
        args: tuple[object, ...] = (),
        kwargs: dict[str, object] | None = None,
        underscore_attr_type: type[object] | None = None,
        default_if_missing: object = _DISPATCH_MISSING,
    ) -> object:
        """Dispatch to public/underscore members with compatibility fallback.

        Parameters
        ----------
        target : object
            Object from which the member will be read.
        public_name : str
            Canonical public member name.
        underscore_name : str
            Compatibility underscore-prefixed member name.
        prefer_instance_type : type[object] | None
            When target matches this type, require the public member.
        call_member : bool
            Whether to call the resolved member.
        args : tuple[object, ...]
            Positional arguments used when ``call_member`` is True.
        kwargs : dict[str, object] | None
            Keyword arguments used when ``call_member`` is True.
        underscore_attr_type : type[object] | None
            Type guard used for attribute-return mode. The public member is
            checked first; underscore member is used as fallback when available.
        default_if_missing : object
            Default value returned when neither member is available.

        Returns
        -------
        object
            The called member result or resolved attribute value.

        Raises
        ------
        AttributeError
            If no supported member exists and no default is provided.
        """
        # Use the function's own default sentinel rather than module globals so
        # behavior remains stable even if this module is reloaded in tests.
        dispatch_kwdefaults = (
            type(self)._dispatch_public_or_underscore.__kwdefaults__ or {}
        )
        missing_sentinel = cast(
            object,
            dispatch_kwdefaults.get("default_if_missing", _DISPATCH_MISSING),
        )
        kwargs = {} if kwargs is None else kwargs
        public_member = getattr(target, public_name, missing_sentinel)
        underscore_member = getattr(target, underscore_name, missing_sentinel)

        if not call_member:
            if _is_unconfigured_mock_member(public_member):
                public_member = missing_sentinel
            if _is_unconfigured_mock_member(underscore_member):
                underscore_member = missing_sentinel
        else:
            if _is_unconfigured_mock_callable(public_member):
                public_member = missing_sentinel
            if _is_unconfigured_mock_callable(underscore_member):
                underscore_member = missing_sentinel

        if (
            prefer_instance_type is not None
            and isinstance(target, prefer_instance_type)
            and not _is_mock_instance(target)
        ):
            if public_member is missing_sentinel:
                raise AttributeError(
                    f"{type(target).__name__} is missing required member '{public_name}'"
                )
            if call_member:
                if not callable(public_member):
                    raise AttributeError(
                        f"{type(target).__name__}.{public_name} is not callable"
                    )
                return public_member(*args, **kwargs)
            return public_member

        if call_member:
            if callable(public_member):
                return public_member(*args, **kwargs)
            if callable(underscore_member):
                return underscore_member(*args, **kwargs)
        else:
            if public_member is not missing_sentinel:
                if underscore_attr_type is None or isinstance(
                    public_member, underscore_attr_type
                ):
                    return public_member
            if underscore_member is not missing_sentinel and (
                underscore_attr_type is None
                or isinstance(underscore_member, underscore_attr_type)
            ):
                return underscore_member

        if default_if_missing is not missing_sentinel:
            return default_if_missing

        raise AttributeError(
            f"{type(target).__name__} is missing supported members "
            f"'{public_name}'/'{underscore_name}'"
        )

    def _validator_validate_connection_request(self) -> None:
        """Call validator pre-check with underscore-compatible fallback for test doubles."""
        self._dispatch_public_or_underscore(
            target=self.validator,
            public_name="validate_connection_request",
            underscore_name="_validate_connection_request",
            prefer_instance_type=ConnectionValidator,
            call_member=True,
        )

    def _state_current_state(self) -> ConnectionState:
        """Read current state with fallback for underscore/mocked state managers."""
        state_value = self._dispatch_public_or_underscore(
            target=self.state_manager,
            public_name="current_state",
            underscore_name="_current_state",
            call_member=False,
            underscore_attr_type=ConnectionState,
        )
        return cast(ConnectionState, state_value)

    def _state_is_closing(self) -> bool:
        """Read closing-state flag with fallback for underscore/mocked state managers."""
        is_closing = self._dispatch_public_or_underscore(
            target=self.state_manager,
            public_name="is_closing",
            underscore_name="_is_closing",
            call_member=False,
            underscore_attr_type=bool,
            default_if_missing=False,
        )
        return bool(is_closing)

    def _state_transition_to(self, new_state: ConnectionState) -> bool:
        """Transition helper with fallback for underscore/mocked state managers."""
        return bool(
            self._dispatch_public_or_underscore(
                target=self.state_manager,
                public_name="transition_to",
                underscore_name="_transition_to",
                call_member=True,
                args=(new_state,),
            )
        )

    def _state_reset_to_disconnected(self) -> bool:
        """Reset helper with fallback for underscore/mocked state managers."""
        return bool(
            self._dispatch_public_or_underscore(
                target=self.state_manager,
                public_name="reset_to_disconnected",
                underscore_name="_reset_to_disconnected",
                call_member=True,
            )
        )

    def _thread_set_event(self, name: str) -> None:
        """Set thread-coordinator event with underscore-compatible fallback for test doubles."""
        missing_event_dispatch = object()
        try:
            result = self._dispatch_public_or_underscore(
                target=self.thread_coordinator,
                public_name="set_event",
                underscore_name="_set_event",
                call_member=True,
                args=(name,),
                default_if_missing=missing_event_dispatch,
            )
        except Exception:  # noqa: BLE001 - reconnect signaling is best effort
            logger.debug(
                "Failed to signal thread event %s via set_event/_set_event.",
                name,
                exc_info=True,
            )
            return
        if result is missing_event_dispatch:
            logger.debug(
                "Thread coordinator is missing set_event/_set_event; skipping %s.",
                name,
            )

    def _client_manager_create_client(
        self,
        device: BLEDevice | str,
        on_disconnect_func: Callable[["BleakRootClient"], None],
        *,
        pair_on_connect: bool,
        connect_timeout: float | None,
    ) -> BLEClient:
        """Create client via public API with underscore-compatible fallback for mocks/test doubles."""
        retry_kwargs: dict[str, object] = {
            "pair_on_connect": pair_on_connect,
            "connect_timeout": connect_timeout,
        }
        while True:
            try:
                created_client = self._dispatch_public_or_underscore(
                    target=self.client_manager,
                    public_name="create_client",
                    underscore_name="_create_client",
                    prefer_instance_type=ClientManager,
                    call_member=True,
                    args=(device, on_disconnect_func),
                    kwargs=retry_kwargs,
                )
                return cast(BLEClient, created_client)
            except TypeError as exc:
                removed_kwarg = False
                for kwarg_name in ("pair_on_connect", "connect_timeout"):
                    if kwarg_name in retry_kwargs and _is_unexpected_keyword_error(
                        exc, kwarg_name
                    ):
                        retry_kwargs.pop(kwarg_name)
                        removed_kwarg = True
                if not removed_kwarg:
                    raise

    def _client_manager_connect_client(
        self, client: BLEClient, *, timeout: float | None
    ) -> None:
        """Connect client via public API with underscore-compatible fallback for mocks/test doubles."""
        try:
            self._dispatch_public_or_underscore(
                target=self.client_manager,
                public_name="connect_client",
                underscore_name="_connect_client",
                prefer_instance_type=ClientManager,
                call_member=True,
                args=(client,),
                kwargs={"timeout": timeout},
            )
        except TypeError as exc:
            if not _is_unexpected_keyword_error(exc, "timeout"):
                raise
            self._dispatch_public_or_underscore(
                target=self.client_manager,
                public_name="connect_client",
                underscore_name="_connect_client",
                prefer_instance_type=ClientManager,
                call_member=True,
                args=(client,),
                kwargs={},
            )

    def _client_manager_safe_close_client(
        self,
        client: BLEClient,
        *,
        disconnect_timeout: float | None = None,
    ) -> None:
        """Close client via public API with underscore-compatible fallback for mocks/test doubles."""
        try:
            self._dispatch_public_or_underscore(
                target=self.client_manager,
                public_name="safe_close_client",
                underscore_name="_safe_close_client",
                prefer_instance_type=ClientManager,
                call_member=True,
                args=(client,),
                kwargs={"disconnect_timeout": disconnect_timeout},
            )
        except TypeError as exc:
            if not _is_unexpected_keyword_error(exc, "disconnect_timeout"):
                raise
            self._dispatch_public_or_underscore(
                target=self.client_manager,
                public_name="safe_close_client",
                underscore_name="_safe_close_client",
                prefer_instance_type=ClientManager,
                call_member=True,
                args=(client,),
                kwargs={},
            )

    @staticmethod
    def _get_connect_timeout(*, pair_on_connect: bool) -> float:
        """Return the appropriate connect timeout for the requested pairing mode.

        Parameters
        ----------
        pair_on_connect : bool
            When True, use the full BLE connection timeout to allow time for
            OS-mediated pairing prompts. When False, use the shorter direct
            connect timeout capped by the full connection timeout.

        Returns
        -------
        float
            Timeout in seconds for the current connect attempt.
        """
        connection_timeout = BLEConfig.CONNECTION_TIMEOUT
        if (
            isinstance(connection_timeout, bool)
            or not isinstance(connection_timeout, numbers.Real)
            or not math.isfinite(connection_timeout)
            or connection_timeout <= 0
        ):
            logger.warning(
                "Invalid BLEConfig.CONNECTION_TIMEOUT=%r; using fallback %.1fs.",
                connection_timeout,
                _CONNECT_TIMEOUT_FALLBACK_SECONDS,
            )
            safe_connection_timeout = _CONNECT_TIMEOUT_FALLBACK_SECONDS
        else:
            safe_connection_timeout = float(connection_timeout)

        direct_connect_timeout = BLEConfig.DIRECT_CONNECT_TIMEOUT_SECONDS
        if (
            isinstance(direct_connect_timeout, bool)
            or not isinstance(direct_connect_timeout, numbers.Real)
            or not math.isfinite(direct_connect_timeout)
            or direct_connect_timeout <= 0
        ):
            logger.warning(
                "Invalid BLEConfig.DIRECT_CONNECT_TIMEOUT_SECONDS=%r; using %.1fs.",
                direct_connect_timeout,
                safe_connection_timeout,
            )
            safe_direct_connect_timeout = safe_connection_timeout
        else:
            safe_direct_connect_timeout = float(direct_connect_timeout)

        if pair_on_connect:
            return safe_connection_timeout
        return min(safe_direct_connect_timeout, safe_connection_timeout)

    @classmethod
    def _resolve_connect_timeout(
        cls,
        *,
        pair_on_connect: bool,
        connect_timeout: float | None,
    ) -> float:
        """Return the effective connect timeout for the current attempt.

        Parameters
        ----------
        pair_on_connect : bool
            Whether pairing is being requested during this connect attempt.
        connect_timeout : float | None
            Optional caller-supplied timeout override. When `None`, the
            pairing-aware default from `_get_connect_timeout()` is used.

        Returns
        -------
        float
            Effective timeout for BLE client construction and connection.

        Raises
        ------
        ValueError
            If `connect_timeout` is non-finite or not strictly positive.
        """
        if connect_timeout is not None:
            if isinstance(connect_timeout, bool) or not isinstance(
                connect_timeout, numbers.Real
            ):
                raise ValueError(_CONNECT_TIMEOUT_INVALID_MSG)
            if not math.isfinite(connect_timeout) or connect_timeout <= 0:
                raise ValueError(_CONNECT_TIMEOUT_INVALID_MSG)
            return float(connect_timeout)
        return cls._get_connect_timeout(pair_on_connect=pair_on_connect)

    def _prepare_connection_target(
        self,
        *,
        address: str | None,
        current_address: str | None,
    ) -> tuple[str | None, str | None, bool]:
        """Resolve and validate the connection target address for this attempt.

        Parameters
        ----------
        address : str | None
            Explicit caller-supplied connection target.
        current_address : str | None
            Existing interface address used when ``address`` is ``None``.

        Returns
        -------
        tuple[str | None, str | None, bool]
            Tuple of ``(target_address, normalized_target, explicit_address)``
            where ``normalized_target`` is produced by
            :func:`sanitize_address` and ``explicit_address`` tracks whether the
            target came from the caller-provided ``address`` argument.

        Raises
        ------
        BLEError
            If an explicitly supplied address is blank after trimming.
        """
        explicit_address = address is not None
        target_address = address if explicit_address else current_address
        if target_address is not None:
            target_address = target_address.strip()
        # Allow None target_address for discovery mode - findDevice() handles this.
        # Only reject empty/whitespace-only strings that are explicitly provided.
        if explicit_address and not target_address:
            raise self.interface.BLEError(CONNECTION_ERROR_EMPTY_ADDRESS)
        if not explicit_address and not target_address:
            target_address = None

        normalized_target = sanitize_address(target_address)
        if target_address:
            logger.info("Attempting to connect to %s", target_address)
        else:
            logger.info("Attempting discovery-mode connection (no address specified)")
        return target_address, normalized_target, explicit_address

    def _resolve_connection_timeouts(
        self,
        *,
        pair_on_connect: bool,
        connect_timeout: float | None,
    ) -> tuple[float, float]:
        """Compute direct and discovery connect timeout budgets for the attempt.

        Parameters
        ----------
        pair_on_connect : bool
            Whether pairing is requested during connect.
        connect_timeout : float | None
            Optional explicit timeout override in seconds.

        Returns
        -------
        tuple[float, float]
            ``(direct_connect_timeout, discovery_connect_timeout)`` in seconds.

        Raises
        ------
        BLEError
            If ``connect_timeout`` is invalid.
        """
        try:
            direct_connect_timeout = self._resolve_connect_timeout(
                pair_on_connect=pair_on_connect,
                connect_timeout=connect_timeout,
            )
        except ValueError as exc:
            raise self.interface.BLEError(
                ERROR_INVALID_CONNECT_TIMEOUT.format(exc=exc)
            ) from exc

        # Preserve historical cadence: direct address attempts use the shorter
        # timeout; discovery-resolved connects use full connection time unless
        # caller explicitly overrides timeout.
        discovery_connect_timeout = (
            direct_connect_timeout
            if connect_timeout is not None or pair_on_connect
            else self._get_connect_timeout(pair_on_connect=True)
        )
        return direct_connect_timeout, discovery_connect_timeout

    @staticmethod
    def _extract_client_address(client: BLEClient) -> str | None:
        """Return the best-known connected address from a BLE client."""
        bleak_client = getattr(client, "bleak_client", None)
        bleak_address = getattr(bleak_client, "address", None)
        if isinstance(bleak_address, str) and bleak_address:
            return bleak_address
        client_address = getattr(client, "address", None)
        return client_address if isinstance(client_address, str) else None

    def _validate_explicit_address_connection(
        self,
        *,
        client: BLEClient,
        target_address: str | None,
        explicit_address: bool,
    ) -> None:
        """Enforce post-connect address verification for explicit BLE-address connects."""
        if not explicit_address or not target_address:
            return
        if not _looks_like_ble_address(target_address):
            return
        requested_key = sanitize_address(target_address)
        if requested_key is None:
            return
        connected_address = self._extract_client_address(client)
        connected_key = sanitize_address(connected_address)
        if connected_key is None:
            # Intentional compatibility policy:
            # Some backends/mocks do not expose a resolved connected peer address
            # even after a successful explicit-address connect. Treat this as
            # "cannot verify" rather than a hard mismatch to avoid disconnecting
            # valid sessions solely due to missing metadata.
            logger.debug(
                "Cannot enforce explicit-address verification for target %s because the connected peer address is unavailable; proceeding in compatibility mode.",
                requested_key,
            )
            return
        if connected_key != requested_key:
            raise BLEAddressMismatchError(
                "Connected BLE address does not match explicit target address.",
                requested_identifier=target_address,
                connected_address=connected_address,
                address=target_address,
            )

    @staticmethod
    def _is_stale_bluez_direct_connect_error(error: BaseException) -> bool:
        """Return whether direct-connect failure looks like stale BlueZ state."""
        candidates: list[BaseException] = []
        pending: list[tuple[BaseException, int]] = [(error, 0)]
        seen_ids: set[int] = set()
        while pending:
            candidate, depth = pending.pop(0)
            candidate_id = id(candidate)
            if candidate_id in seen_ids:
                continue
            seen_ids.add(candidate_id)
            candidates.append(candidate)
            if depth >= 5:
                continue
            for attr_name in ("cause", "__cause__", "__context__"):
                cause = getattr(candidate, attr_name, None)
                if isinstance(cause, BaseException):
                    pending.append((cause, depth + 1))

        for candidate in candidates:
            dbus_error_name = getattr(candidate, "dbus_error", None)
            if (
                isinstance(dbus_error_name, str)
                and dbus_error_name.casefold() in _STALE_BLUEZ_DBUS_ERROR_NAMES
            ):
                return True

            dbus_error_details = getattr(candidate, "dbus_error_details", None)
            details_text = (
                dbus_error_details.casefold()
                if isinstance(dbus_error_details, str)
                else ""
            )
            if details_text and any(
                token in details_text for token in _STALE_BLUEZ_DETAIL_TOKENS
            ):
                return True

            message = str(candidate).strip().casefold()
            if message and any(
                token in message for token in _STALE_BLUEZ_FALLBACK_MESSAGE_TOKENS
            ):
                return True
        return False

    def _should_attempt_stale_bluez_cleanup(
        self,
        *,
        target_address: str | None,
        explicit_address: bool,
        error: BaseException,
    ) -> bool:
        """Return whether stale BlueZ disconnect cleanup should be attempted."""
        if not sys.platform.startswith("linux"):
            return False
        if not explicit_address or not target_address:
            return False
        if not _looks_like_ble_address(target_address):
            return False
        return self._is_stale_bluez_direct_connect_error(error)

    def _attempt_stale_bluez_cleanup(
        self,
        *,
        target_address: str,
        on_disconnect_func: Callable[["BleakRootClient"], None],
        connect_timeout: float,
    ) -> bool:
        """Best-effort stale connection cleanup for Linux/BlueZ direct connects."""
        cleanup_client: BLEClient | None = None
        disconnect_timeout = min(connect_timeout, DISCONNECT_TIMEOUT_SECONDS)
        try:
            cleanup_client = self._client_manager_create_client(
                target_address,
                on_disconnect_func,
                pair_on_connect=False,
                connect_timeout=connect_timeout,
            )
            self._client_manager_connect_client(
                cleanup_client,
                timeout=connect_timeout,
            )
            disconnect = getattr(cleanup_client, "disconnect", None)
            if callable(disconnect):
                try:
                    disconnect(await_timeout=disconnect_timeout)
                except TypeError as exc:
                    if not _is_unexpected_keyword_error(exc, "await_timeout"):
                        raise
                    disconnect()
            logger.debug(
                "Completed stale BlueZ cleanup probe for explicit address %s",
                sanitize_address(target_address) or target_address,
            )
            return True
        except Exception:  # noqa: BLE001 - stale cleanup is best effort
            logger.debug(
                "Stale BlueZ cleanup probe failed for %s",
                sanitize_address(target_address) or target_address,
                exc_info=True,
            )
            return False
        finally:
            if cleanup_client is not None:
                self._client_manager_safe_close_client(
                    cleanup_client,
                    disconnect_timeout=disconnect_timeout,
                )

    def _retry_direct_connect_after_cleanup(
        self,
        *,
        target_address: str,
        explicit_address: bool,
        on_disconnect_func: Callable[["BleakRootClient"], None],
        pair_on_connect: bool,
        direct_connect_timeout: float,
        register_notifications_func: Callable[[BLEClient], None],
        on_connected_func: Callable[[], None],
        emit_connected_side_effects: bool,
    ) -> BLEClient:
        """Retry one explicit-address direct connect after stale cleanup."""
        self._raise_if_interface_closing()
        retry_client = self._client_manager_create_client(
            target_address,
            on_disconnect_func,
            pair_on_connect=pair_on_connect,
            connect_timeout=direct_connect_timeout,
        )
        _retry_succeeded = False
        try:
            self._raise_if_interface_closing()
            self._client_manager_connect_client(
                retry_client,
                timeout=direct_connect_timeout,
            )
            self._raise_if_interface_closing()
            self._validate_explicit_address_connection(
                client=retry_client,
                target_address=target_address,
                explicit_address=explicit_address,
            )
            self._finalize_connection(
                retry_client,
                target_address,
                register_notifications_func,
                on_connected_func,
                emit_connected_side_effects=emit_connected_side_effects,
            )
            _retry_succeeded = True
            return retry_client
        finally:
            if not _retry_succeeded:
                self._client_manager_safe_close_client(retry_client)

    def _attempt_direct_connect(
        self,
        *,
        target_address: str | None,
        explicit_address: bool,
        normalized_target: str | None,
        on_disconnect_func: Callable[["BleakRootClient"], None],
        pair_on_connect: bool,
        direct_connect_timeout: float,
        register_notifications_func: Callable[[BLEClient], None],
        on_connected_func: Callable[[], None],
        emit_connected_side_effects: bool,
    ) -> tuple[BLEClient | None, bool]:
        """Try explicit-address direct connection and return fallback guidance.

        Parameters
        ----------
        target_address : str | None
            Explicit address to connect, or ``None`` for discovery mode.
        explicit_address : bool
            Whether ``target_address`` originated from an explicitly supplied
            ``address`` argument rather than derived current-address state.
        normalized_target : str | None
            Sanitized logging form of ``target_address``.
        on_disconnect_func : Callable[[BleakRootClient], None]
            Disconnect callback passed to client creation.
        pair_on_connect : bool
            Whether pairing should be attempted during connect.
        direct_connect_timeout : float
            Timeout for direct connect attempts.
        register_notifications_func : Callable[[BLEClient], None]
            Notification registration callback used during finalization.
        on_connected_func : Callable[[], None]
            Connected callback used during finalization.
        emit_connected_side_effects : bool
            Whether finalization should emit connected side effects immediately.

        Returns
        -------
        tuple[BLEClient | None, bool]
            ``(client, skip_discovery_scan)`` where ``client`` is non-``None``
            on success. When ``client`` is ``None``, ``skip_discovery_scan``
            indicates whether retry should bypass discovery.

        Raises
        ------
        BleakDBusError
            Propagated for DBus-level failures to allow upstream backoff.
        BleakError
            Propagated for non-recoverable direct-connect errors.
        BLEError
            Propagated when interface shutdown races with connect flow.
        OSError
            Propagated for transport-level failures.
        TimeoutError
            Propagated for non-recoverable timeout failures.
        """
        if not target_address:
            return None, False

        self._raise_if_interface_closing()
        client = self._client_manager_create_client(
            target_address,
            on_disconnect_func,
            pair_on_connect=pair_on_connect,
            connect_timeout=direct_connect_timeout,
        )
        error_for_fallback: (
            BleakError | BLEClient.BLEError | OSError | TimeoutError | BleakDBusError
        ) | None = None
        try:
            self._raise_if_interface_closing()
            self._client_manager_connect_client(client, timeout=direct_connect_timeout)
        except (SystemExit, KeyboardInterrupt):  # pylint: disable=W0706
            self._client_manager_safe_close_client(client)
            raise
        except (
            BleakDBusError,
            BleakError,
            BLEClient.BLEError,
            OSError,
            TimeoutError,
        ) as direct_err:
            error_for_fallback = direct_err
            self._client_manager_safe_close_client(client)
            logger.debug(
                "Direct connect to %s failed; preparing retry path: %s",
                normalized_target,
                direct_err,
                exc_info=True,
            )
            if self._should_attempt_stale_bluez_cleanup(
                target_address=target_address,
                explicit_address=explicit_address,
                error=direct_err,
            ):
                logger.warning(
                    "Direct connect to %s failed; attempting one stale BlueZ cleanup + retry.",
                    normalized_target,
                )
                if self._attempt_stale_bluez_cleanup(
                    target_address=target_address,
                    on_disconnect_func=on_disconnect_func,
                    connect_timeout=direct_connect_timeout,
                ):
                    try:
                        retried_client = self._retry_direct_connect_after_cleanup(
                            target_address=target_address,
                            explicit_address=explicit_address,
                            on_disconnect_func=on_disconnect_func,
                            pair_on_connect=pair_on_connect,
                            direct_connect_timeout=direct_connect_timeout,
                            register_notifications_func=register_notifications_func,
                            on_connected_func=on_connected_func,
                            emit_connected_side_effects=emit_connected_side_effects,
                        )
                        return retried_client, False
                    except BLEAddressMismatchError:
                        raise
                    except BleakDBusError as retry_dbus_err:
                        logger.debug(
                            "Direct reconnect after stale BlueZ cleanup failed for %s: %s",
                            normalized_target,
                            retry_dbus_err,
                            exc_info=True,
                        )
                        raise BLEDBusTransportError.from_exception(
                            retry_dbus_err,
                            message="BLE DBus transport error during direct connect.",
                            requested_identifier=target_address,
                            address=target_address,
                        ) from retry_dbus_err
                    except (
                        BleakError,
                        BLEClient.BLEError,
                        OSError,
                        TimeoutError,
                    ) as retry_err:
                        logger.debug(
                            "Direct reconnect after stale BlueZ cleanup failed for %s: %s",
                            normalized_target,
                            retry_err,
                            exc_info=True,
                        )
                        raise
            if isinstance(error_for_fallback, BleakDBusError):
                raise BLEDBusTransportError.from_exception(
                    error_for_fallback,
                    message="BLE DBus transport error during direct connect.",
                    requested_identifier=target_address,
                    address=target_address,
                ) from error_for_fallback
            # Preserve strict direct-only retries only for caller-explicit BLE
            # addresses. Derived targets (for example current_address carried
            # into reconnect flows) are still allowed to use discovery fallback.
            skip_discovery_scan = explicit_address and _looks_like_ble_address(
                target_address
            )
            if skip_discovery_scan and _is_device_not_found_error(error_for_fallback):
                logger.debug(
                    "Direct connect reported device-not-found for %s; skipping discovery scan and retrying explicit address connect.",
                    normalized_target,
                )
            elif skip_discovery_scan:
                logger.debug(
                    "Direct connect to explicit address %s failed; retrying without discovery scan.",
                    normalized_target,
                )
            elif target_address and _looks_like_ble_address(target_address):
                logger.debug(
                    "Direct connect to derived address %s failed; allowing discovery fallback.",
                    normalized_target,
                )
            return None, skip_discovery_scan
        except Exception:
            self._client_manager_safe_close_client(client)
            raise

        try:
            self._raise_if_interface_closing()
            self._validate_explicit_address_connection(
                client=client,
                target_address=target_address,
                explicit_address=explicit_address,
            )
            self._finalize_connection(
                client,
                target_address,
                register_notifications_func,
                on_connected_func,
                emit_connected_side_effects=emit_connected_side_effects,
            )
        except BaseException:
            self._client_manager_safe_close_client(client)
            raise
        return client, False

    def _resolve_retry_target(
        self,
        *,
        target_address: str | None,
        skip_discovery_scan: bool,
        direct_connect_timeout: float,
        discovery_connect_timeout: float,
    ) -> tuple[BLEDevice | str, str, float]:
        """Resolve retry connection target from direct or discovery path.

        Parameters
        ----------
        target_address : str | None
            Original target address, if one was supplied.
        skip_discovery_scan : bool
            Whether retry should skip discovery and retry direct address connect.
        direct_connect_timeout : float
            Timeout used for direct retry attempts.
        discovery_connect_timeout : float
            Timeout used for discovery-resolved retry attempts.

        Returns
        -------
        tuple[BLEDevice | str, str, float]
            ``(connection_target, resolved_address, retry_connect_timeout)``.

        Raises
        ------
        BLEError
            If shutdown begins before target resolution completes.
        AttributeError
            If interface find-device compatibility entrypoints are unavailable.
        """
        if skip_discovery_scan and target_address is not None:
            return target_address, target_address, direct_connect_timeout

        self._raise_if_interface_closing()
        try:
            device = self._compat_find_device(target_address)
        except BleakDeviceNotFoundError as error:
            raise MeshtasticBLEDeviceNotFoundError(
                "No Meshtastic BLE device matched the requested identifier.",
                requested_identifier=target_address,
                cause=error,
            ) from error
        return device, device.address, discovery_connect_timeout

    def _connect_retry_target(
        self,
        *,
        connection_target: BLEDevice | str,
        resolved_address: str,
        target_address: str | None,
        skip_discovery_scan: bool,
        on_disconnect_func: Callable[["BleakRootClient"], None],
        pair_on_connect: bool,
        retry_connect_timeout: float,
    ) -> tuple[BLEClient, str]:
        """Connect a resolved retry target and return the connected client.

        Parameters
        ----------
        connection_target : BLEDevice | str
            Target returned by :meth:`_resolve_retry_target`.
        resolved_address : str
            Address associated with ``connection_target`` for logging/state.
        target_address : str | None
            Original explicit target address.
        skip_discovery_scan : bool
            Whether retry started in skip-discovery mode.
        on_disconnect_func : Callable[[BleakRootClient], None]
            Disconnect callback for newly created clients.
        pair_on_connect : bool
            Whether pairing should be requested during connect.
        retry_connect_timeout : float
            Timeout for the initial retry connect.

        Returns
        -------
        tuple[BLEClient, str]
            Connected client and its resolved address.

        Raises
        ------
        BleakDBusError
            Propagated for DBus-level failures.
        BleakError
            Propagated when retry connect fails without eligible fallback.
        BLEError
            Propagated when interface shutdown races with retry flow.
        OSError
            Propagated for transport failures.
        TimeoutError
            Propagated for non-recoverable timeout failures.
        """
        self._raise_if_interface_closing()
        client = self._client_manager_create_client(
            connection_target,
            on_disconnect_func,
            pair_on_connect=pair_on_connect,
            connect_timeout=retry_connect_timeout,
        )
        try:
            self._raise_if_interface_closing()
            self._client_manager_connect_client(client, timeout=retry_connect_timeout)
        except (SystemExit, KeyboardInterrupt):  # pylint: disable=W0706
            self._client_manager_safe_close_client(client)
            raise
        except BleakDBusError as dbus_err:
            self._client_manager_safe_close_client(client)
            raise BLEDBusTransportError.from_exception(
                dbus_err,
                message="BLE DBus transport error during retry connect.",
                requested_identifier=target_address or resolved_address,
                address=resolved_address,
            ) from dbus_err
        except TimeoutError as timeout_err:
            self._client_manager_safe_close_client(client)
            raise BLEConnectionTimeoutError.from_exception(
                timeout_err,
                message="BLE connection timed out during retry connect.",
                requested_identifier=target_address or resolved_address,
                timeout=retry_connect_timeout,
            ) from timeout_err
        except (
            BleakError,
            BLEClient.BLEError,
            OSError,
        ) as retry_err:
            if (
                skip_discovery_scan
                and target_address is not None
                and _is_device_not_found_error(retry_err)
            ):
                logger.debug(
                    "Direct retry also reported device-not-found for %s; explicit-address mode does not use discovery fallback.",
                    sanitize_address(target_address),
                )
            self._client_manager_safe_close_client(client)
            raise
        except Exception:
            self._client_manager_safe_close_client(client)
            raise

        return client, resolved_address

    def _compat_find_device(self, target_address: str | None) -> BLEDevice:
        """Resolve find-device lookup with historical method-name compatibility.

        Parameters
        ----------
        target_address : str | None
            Address/identifier to locate, or ``None`` for discovery-mode lookup.

        Returns
        -------
        BLEDevice
            Matching BLE device resolved by interface find-device helpers.

        Raises
        ------
        AttributeError
            If no supported find-device compatibility helper exists.
        """
        find_device = getattr(self.interface, "findDevice", None)
        if callable(find_device) and not _is_unconfigured_mock_callable(find_device):
            return cast(BLEDevice, find_device(target_address))

        legacy_find_device = getattr(self.interface, "find_device", None)
        if callable(legacy_find_device) and not _is_unconfigured_mock_callable(
            legacy_find_device
        ):
            return cast(BLEDevice, legacy_find_device(target_address))

        underscore_find_device = getattr(self.interface, "_find_device", None)
        if callable(underscore_find_device) and not _is_unconfigured_mock_callable(
            underscore_find_device
        ):
            return cast(BLEDevice, underscore_find_device(target_address))
        raise AttributeError("Interface is missing findDevice/find_device/_find_device")

    def _finalize_connection(
        self,
        client: BLEClient,
        device_address: str,
        register_notifications_func: Callable[[BLEClient], None],
        on_connected_func: Callable[[], None],
        *,
        emit_connected_side_effects: bool = True,
    ) -> None:
        """Finalize a successful BLE connection by registering notification handlers, validating the client and orchestrator state, transitioning to CONNECTED, and invoking post-connection callbacks.

        Parameters
        ----------
        client : BLEClient
            The connected BLE client instance.
        device_address : str
            Device address used for logging.
        register_notifications_func : Callable
            Callable that registers notification handlers on `client`.
        on_connected_func : Callable
            Callback invoked after the connection state transitions to CONNECTED
            when `emit_connected_side_effects` is True.
        emit_connected_side_effects : bool
            When True, emit reconnect wake signaling and success logging during
            finalization. Callers that defer "connected" publication can set this
            to False and emit those side effects later.

        Raises
        ------
        BLEInterface.BLEError
            If the orchestrator is not in CONNECTING state or if the client disconnects during finalization.
        BLEError
            If state transitions fail or the client connection is lost.
        """
        # Initial state check under lock before performing blocking I/O
        with self.state_lock:
            current_state = self._state_current_state()
            if current_state != ConnectionState.CONNECTING:
                logger.debug(
                    "Connection finalization aborted: state changed from CONNECTING to %s during connect",
                    current_state,
                )
                raise self.interface.BLEError(
                    CONNECTION_ERROR_INVALIDATED_BY_CONCURRENT_DISCONNECT
                )

        # Register notifications OUTSIDE the lock to avoid blocking state transitions
        # during BLE I/O (start_notify can take up to NOTIFICATION_START_TIMEOUT).
        # This allows disconnect/close/connect to proceed during notification setup.
        register_notifications_func(client)

        # Re-check state after registration under lock for atomic state transition
        with self.state_lock:
            if self._state_current_state() != ConnectionState.CONNECTING:
                logger.debug(
                    "Connection finalization aborted: state changed during notification registration"
                )
                raise self.interface.BLEError(
                    CONNECTION_ERROR_INVALIDATED_BY_CONCURRENT_DISCONNECT
                )

            # Post-registration check: verify client is still connected.
            # This catches disconnects that occurred during notification registration
            # which may have been ignored by _handle_disconnect due to CONNECTING state.
            if not ConnectionValidator._client_is_connected(client):
                logger.debug(
                    "Connection finalization aborted: client disconnected during notification registration"
                )
                raise self.interface.BLEError(
                    CONNECTION_ERROR_CLIENT_DISCONNECTED_DURING_FINALIZATION
                )

            if not self._state_transition_to(ConnectionState.CONNECTED):
                raise self.interface.BLEError(
                    CONNECTION_ERROR_STATE_TRANSITION_INVALIDATED
                )

        if emit_connected_side_effects:
            raw_ever_connected = getattr(self.interface, "_ever_connected", False)
            prior_ever_connected = (
                False
                if _is_unconfigured_mock_member(raw_ever_connected)
                else (
                    raw_ever_connected
                    if isinstance(raw_ever_connected, bool)
                    else False
                )
            )
            on_connected_func()
            if prior_ever_connected:
                self._thread_set_event(RECONNECTED_EVENT)
            normalized_device_address = sanitize_address(device_address)
            logger.info(
                "Connection successful to %s",
                normalized_device_address or "unknown",
            )

    def _transition_failure_to_disconnected(self, error_context: str) -> None:
        """Perform a best-effort state correction after a connection failure.

        Attempts to transition the connection state to ERROR and then to DISCONNECTED; if a transition is rejected, logs a warning and forces DISCONNECTED as a final fallback.

        Parameters
        ----------
        error_context : str
            Context string used in log messages to identify the failure context.
        """
        if not self._state_transition_to(ConnectionState.ERROR):
            logger.warning(
                "Failed state transition to %s during %s (current=%s)",
                ConnectionState.ERROR.value,
                error_context,
                self._state_current_state().value,
            )
        if not self._state_transition_to(ConnectionState.DISCONNECTED):
            logger.warning(
                "Failed state transition to %s during %s (current=%s); forcing reset",
                ConnectionState.DISCONNECTED.value,
                error_context,
                self._state_current_state().value,
            )
            self._state_reset_to_disconnected()

    def _raise_if_interface_closing(self) -> None:
        """Abort connection work when shutdown is already in progress."""
        with self.state_lock:
            is_closing = self._state_is_closing()
        raw_closed = getattr(self.interface, "_closed", False)
        is_closed = raw_closed is True
        if is_closing or is_closed:
            raise self.interface.BLEError(BLECLIENT_ERROR_CANNOT_CONNECT_WHILE_CLOSING)

    def _establish_connection(
        self,
        address: str | None,
        current_address: str | None,
        register_notifications_func: Callable[[BLEClient], None],
        on_connected_func: Callable[[], None],
        on_disconnect_func: Callable[["BleakRootClient"], None],
        *,
        pair_on_connect: bool = False,
        connect_timeout: float | None = None,
        emit_connected_side_effects: bool = True,
    ) -> BLEClient:
        """Establish a BLE connection to a device, attempting a direct connect when an explicit address is provided and falling back to discovery when needed, then finalize notification registration and lifecycle callbacks.

        Parameters
        ----------
        address : str | None
            Explicit device address to connect to; when None discovery mode is used.
        current_address : str | None
            Fallback address used when `address` is None.
        register_notifications_func : Callable
            Function called with the connected `BLEClient` to register notification handlers.
        on_connected_func : Callable
            Callback invoked after the connection has been finalized and state updated to CONNECTED.
        on_disconnect_func : Callable
            Callback passed to the `BLEClient` to be invoked when the client disconnects.
        pair_on_connect : bool
            If True, initialize per-attempt BLE clients with pairing enabled so
            connection attempts request pairing while connecting. (Default value = False)
        connect_timeout : float | None
            Optional timeout override for BLE client construction and connect
            attempts. When `None`, the pairing-aware default is used.
        emit_connected_side_effects : bool
            When True, emit reconnect wake signaling and success logging during
            finalization. Set False when connected publication is deferred until
            a later ownership verification step.

        Returns
        -------
        BLEClient
            The connected BLE client instance.

        Raises
        ------
        BLEError
            If the request is invalid (e.g., empty address) or a concurrent connection state prevents establishing a connection.
        BleakDBusError
            If a DBus-level BLE error occurs during connection.
        Exception
            If any other error occurs during the connection process.
        """
        self._validator_validate_connection_request()
        self._raise_if_interface_closing()

        target_address, normalized_target, explicit_address = (
            self._prepare_connection_target(
                address=address,
                current_address=current_address,
            )
        )
        direct_connect_timeout, discovery_connect_timeout = (
            self._resolve_connection_timeouts(
                pair_on_connect=pair_on_connect,
                connect_timeout=connect_timeout,
            )
        )

        with self.state_lock:
            if not self._state_transition_to(ConnectionState.CONNECTING):
                raise self.interface.BLEError(BLECLIENT_ERROR_ALREADY_CONNECTED)
        client: BLEClient | None = None
        skip_discovery_scan = False
        try:
            client, skip_discovery_scan = self._attempt_direct_connect(
                target_address=target_address,
                explicit_address=explicit_address,
                normalized_target=normalized_target,
                on_disconnect_func=on_disconnect_func,
                pair_on_connect=pair_on_connect,
                direct_connect_timeout=direct_connect_timeout,
                register_notifications_func=register_notifications_func,
                on_connected_func=on_connected_func,
                emit_connected_side_effects=emit_connected_side_effects,
            )
            if client is not None:
                return client

            (
                connection_target,
                resolved_address,
                retry_connect_timeout,
            ) = self._resolve_retry_target(
                target_address=target_address,
                skip_discovery_scan=skip_discovery_scan,
                direct_connect_timeout=direct_connect_timeout,
                discovery_connect_timeout=discovery_connect_timeout,
            )

            client, resolved_address = self._connect_retry_target(
                connection_target=connection_target,
                resolved_address=resolved_address,
                target_address=target_address,
                skip_discovery_scan=skip_discovery_scan,
                on_disconnect_func=on_disconnect_func,
                pair_on_connect=pair_on_connect,
                retry_connect_timeout=retry_connect_timeout,
            )

            self._raise_if_interface_closing()
            self._validate_explicit_address_connection(
                client=client,
                target_address=target_address,
                explicit_address=explicit_address,
            )
            self._finalize_connection(
                client,
                resolved_address,
                register_notifications_func,
                on_connected_func,
                emit_connected_side_effects=emit_connected_side_effects,
            )
            return client
        except BLEConnectionTimeoutError:
            if client:
                self._client_manager_safe_close_client(client)
            self._transition_failure_to_disconnected("Timeout during connect")
            raise
        except TimeoutError as timeout_err:
            if client:
                self._client_manager_safe_close_client(client)
            self._transition_failure_to_disconnected("Timeout during connect")
            raise BLEConnectionTimeoutError.from_exception(
                timeout_err,
                message="BLE connection timed out.",
                requested_identifier=target_address or current_address,
                timeout=connect_timeout,
            ) from timeout_err
        except BleakDBusError:
            if client:
                self._client_manager_safe_close_client(client)
            self._transition_failure_to_disconnected("BleakDBusError during connect")
            raise
        except (SystemExit, KeyboardInterrupt):  # pylint: disable=W0706
            # Transition first so concurrent callers immediately observe the
            # interrupted state before teardown work begins.
            self._transition_failure_to_disconnected(
                "connect interrupted by SystemExit/KeyboardInterrupt"
            )
            # Clean up client before re-raising to avoid resource leak
            if client:
                self._client_manager_safe_close_client(client)
            raise
        except Exception:
            logger.warning(
                "Failed to connect, closing BLEClient thread.", exc_info=True
            )
            if client:
                self._client_manager_safe_close_client(client)
            self._transition_failure_to_disconnected("unexpected connect failure")
            raise

    def establish_connection(
        self,
        address: str | None,
        current_address: str | None,
        register_notifications_func: Callable[[BLEClient], None],
        on_connected_func: Callable[[], None],
        on_disconnect_func: Callable[["BleakRootClient"], None],
        *,
        pair_on_connect: bool = False,
        connect_timeout: float | None = None,
        emit_connected_side_effects: bool = True,
    ) -> BLEClient:
        # Internal adapter alias for collaborator migration; delegates to _establish_connection.
        """Establish a BLE connection through the public compatibility surface.

        Parameters
        ----------
        address : str | None
            Requested target address or identifier.
        current_address : str | None
            Currently bound interface address, if any.
        register_notifications_func : Callable[[BLEClient], None]
            Callback used to register post-connect notifications.
        on_connected_func : Callable[[], None]
            Callback executed after successful connection finalization.
        on_disconnect_func : Callable[[BleakRootClient], None]
            Callback invoked when the underlying Bleak client disconnects.
        pair_on_connect : bool
            Whether pairing should be requested during connect.
        connect_timeout : float | None
            Optional connect timeout in seconds.
        emit_connected_side_effects : bool
            Whether connected side effects should be emitted during finalization.

        Returns
        -------
        BLEClient
            Connected client returned by the orchestrator flow.

        Raises
        ------
        BLEError
            Propagated from connection validation and connect/finalization
            failures.
        AttributeError
            If a required compatibility collaborator hook is unavailable.

        See Also
        --------
        _establish_connection
            Internal connection orchestration implementation.
        """
        return self._establish_connection(
            address=address,
            current_address=current_address,
            register_notifications_func=register_notifications_func,
            on_connected_func=on_connected_func,
            on_disconnect_func=on_disconnect_func,
            pair_on_connect=pair_on_connect,
            connect_timeout=connect_timeout,
            emit_connected_side_effects=emit_connected_side_effects,
        )
