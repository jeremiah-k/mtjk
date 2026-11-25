"""Management command helpers for BLE interface orchestration."""

import inspect
import math
import numbers
import re
import subprocess as subprocess_stdlib
import threading
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar, cast

from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.compat_adapter import (
    _get_declared_callable,
    _get_declared_lock,
    _get_declared_member,
    _load_runtime_module,
)
from meshtastic.interfaces.ble.connection import ConnectionOrchestrator
from meshtastic.interfaces.ble.constants import (
    ERROR_CONNECTION_SUPPRESSED,
    ERROR_INVALID_CONNECT_TIMEOUT,
    ERROR_MANAGEMENT_ADDRESS_EMPTY,
    ERROR_MANAGEMENT_ADDRESS_REQUIRED,
    ERROR_MANAGEMENT_AWAIT_TIMEOUT_INVALID,
    ERROR_MANAGEMENT_TARGET_CHANGED,
    ERROR_TRUST_BLUETOOTHCTL_MISSING,
    ERROR_TRUST_COMMAND_FAILED,
    ERROR_TRUST_COMMAND_TIMEOUT,
    ERROR_TRUST_INVALID_TIMEOUT,
    ERROR_TRUST_LINUX_ONLY,
    logger,
)
from meshtastic.interfaces.ble.discovery import _looks_like_ble_address
from meshtastic.interfaces.ble.gating import (
    _addr_key,
    _addr_lock_context,
)
from meshtastic.interfaces.ble.lifecycle_primitives import _client_is_connected_compat
from meshtastic.interfaces.ble.management_state import (
    _BLEManagementStatePort,
    _management_state_for,
)
from meshtastic.interfaces.ble.ports import _BLESessionStatePort, _LockPort
from meshtastic.interfaces.ble.session_state import _session_state_for
from meshtastic.interfaces.ble.utils import (
    _call_factory_with_optional_kwarg,
    sanitize_address,
)

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.interface import BLEInterface


BLUETOOTHCTL_TRUST_TIMEOUT_SECONDS: float = 10.0
TRUST_COMMAND_OUTPUT_MAX_CHARS: int = 200
TRUST_HEX_BLOB_RE: re.Pattern[str] = re.compile(r"\b[0-9A-Fa-f]{16,}\b")
TRUST_TOKEN_RE: re.Pattern[str] = re.compile(r"\b[A-Za-z0-9+/=_-]{40,}\b")
_DISCOVERY_FACTORY_LOG_KWARG: str = "log_if_no_address"
_HEX_MAC_COLON_RE: re.Pattern[str] = re.compile(
    r"^[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}$"
)
_HEX_MAC_NO_SEPARATOR_RE: re.Pattern[str] = re.compile(r"^[0-9A-Fa-f]{12}$")
HEX_DIGITS: str = "0123456789abcdefABCDEF"

T = TypeVar("T")


@dataclass(frozen=True)
class _ManagementStartContext:
    """Captured state from management-operation startup validation."""

    expected_implicit_binding: str | None
    target_address: str | None
    use_existing_client_without_resolved_address: bool


def _create_management_client(
    ble_client_factory: Callable[..., BLEClient], target_address: str
) -> BLEClient:
    """Create a temporary management client with optional-kwarg compatibility.

    Parameters
    ----------
    ble_client_factory : Callable[..., BLEClient]
        Factory used to create temporary management clients.
    target_address : str
        Address passed to the factory for client creation.

    Returns
    -------
    BLEClient
        Newly created management client.

    Raises
    ------
    TypeError
        Propagated when factory invocation fails for reasons other than
        rejecting ``log_if_no_address``.
    """

    def _log_kwarg_rejected(exc: TypeError) -> None:
        logger.debug(
            "Management client factory rejected log_if_no_address kwarg; retrying without it: %s",
            exc,
            exc_info=True,
        )

    client = _call_factory_with_optional_kwarg(
        ble_client_factory,
        args=(target_address,),
        optional_kwarg=_DISCOVERY_FACTORY_LOG_KWARG,
        optional_value=False,
        on_kwarg_rejected=_log_kwarg_rejected,
    )
    if client is None:
        factory_name = getattr(ble_client_factory, "__name__", repr(ble_client_factory))
        raise TypeError(
            f"Management client factory {factory_name} returned None for {target_address!r}"
        )
    return client


def _is_blank_or_malformed_address_like(address: str | None) -> bool:
    """Return whether an input is blank or malformed address-like text.

    Parameters
    ----------
    address : str | None
        Explicit management-target input provided by the caller.

    Returns
    -------
    bool
        ``True`` when ``address`` is blank or appears address-like but is
        malformed; otherwise ``False``.
    """
    # Validation tree summary:
    # - ``None`` -> False; blank/whitespace -> True.
    # - Colon-separated MAC text is validated via ``_HEX_MAC_COLON_RE``.
    # - Other forms are normalized by ``sanitize_address`` then checked against
    #   ``_HEX_MAC_NO_SEPARATOR_RE``.
    # - No-colon hex-only input must be 12 chars and match
    #   ``_HEX_MAC_NO_SEPARATOR_RE`` to be treated as valid.
    if address is None:
        return False
    stripped_address = address.strip()
    if not stripped_address:
        return True
    if _HEX_MAC_COLON_RE.fullmatch(stripped_address):
        return False

    normalized_address = sanitize_address(stripped_address)
    normalized_is_mac = (
        normalized_address is not None
        and _HEX_MAC_NO_SEPARATOR_RE.fullmatch(normalized_address) is not None
    )
    if normalized_is_mac:
        return False

    stripped_is_plain_hex = all(char in HEX_DIGITS for char in stripped_address)
    if ":" not in stripped_address and stripped_is_plain_hex:
        normalized_missing_but_plain_mac = (
            normalized_address is None
            and _HEX_MAC_NO_SEPARATOR_RE.fullmatch(stripped_address) is not None
        )
        return normalized_missing_but_plain_mac or _is_no_colon_hex_malformed(
            stripped_address
        )

    # Treat colon-containing identifiers as malformed only when they look
    # like hex/MAC text but fail strict MAC validation.
    return all(char == ":" or char in HEX_DIGITS for char in stripped_address)


def _is_no_colon_hex_malformed(stripped_address: str) -> bool:
    """Return whether no-colon, hex-only text should be treated as malformed.

    Parameters
    ----------
    stripped_address : str
        Non-empty, whitespace-trimmed address-like text.

    Returns
    -------
    bool
        ``True`` when the value is a 12-character hex-only identifier that
        fails strict no-separator MAC validation; otherwise ``False``.

    Notes
    -----
    Shorter/longer no-colon hex identifiers are treated as non-address text for
    compatibility and are therefore not considered malformed by this probe.
    """
    if ":" in stripped_address:
        return False
    if not all(char in HEX_DIGITS for char in stripped_address):
        return False
    if len(stripped_address) != 12:
        return False
    return _HEX_MAC_NO_SEPARATOR_RE.fullmatch(stripped_address) is None


def _same_management_binding(left: str | None, right: str | None) -> bool:
    """Return whether two management bindings represent the same target."""
    normalized_left = sanitize_address(left)
    normalized_right = sanitize_address(right)
    if normalized_left is None and normalized_right is None:
        return (left or "").strip() == (right or "").strip()
    return normalized_left == normalized_right


def _normalized_mac_to_colon_address(normalized_address: str) -> str:
    """Convert a normalized 12-hex BLE address into colon-delimited form."""
    if len(normalized_address) != 12:
        return normalized_address
    return ":".join(
        normalized_address[index : index + 2] for index in range(0, 12, 2)
    ).lower()


def _coerce_concrete_ble_address(address: str | None) -> str | None:
    """Return a concrete colon-delimited BLE address when normalization succeeds."""
    if address is None:
        return None
    normalized = sanitize_address(address)
    if normalized is None:
        return address
    return _normalized_mac_to_colon_address(normalized)


def _synthetic_gate_address_for_client(client: BLEClient) -> str:
    """Return a deterministic pseudo-address used for fallback gate serialization."""
    token = f"{id(client) & 0xFFFFFFFFFFFF:012x}"
    return ":".join(token[index : index + 2] for index in range(0, len(token), 2))


class BLEManagementCommandHandler:
    """Instance-bound management command collaborator for BLEInterface."""

    def __init__(
        self,
        iface: "BLEInterface",
        *,
        ble_client_factory: Callable[..., BLEClient],
        connected_elsewhere: Callable[[str | None, object | None], bool],
        session_state: _BLESessionStatePort | None = None,
        management_state: _BLEManagementStatePort | None = None,
        connect_lock: _LockPort | None = None,
        client_closer: Callable[..., None] | None = None,
    ) -> None:
        """Create a bound management collaborator.

        Parameters
        ----------
        iface : BLEInterface
            Interface instance whose management lifecycle is orchestrated.
        ble_client_factory : Callable[..., BLEClient]
            Factory for temporary management clients.
        connected_elsewhere : Callable[[str | None, object | None], bool]
            Cross-interface ownership probe.
        session_state : _BLESessionStatePort | None, optional
            Shared BLE lifecycle state. When omitted, resolve the interface-owned
            state or use the legacy adapter.
        management_state : _BLEManagementStatePort | None, optional
            Shared management-operation accounting state. When omitted, resolve
            the interface-owned state or use the legacy adapter.
        connect_lock : _LockPort | None, optional
            Explicit connect-serialization lock. Compatibility resolution falls
            back to a declared interface lock or an isolated reentrant lock.
        client_closer : Callable[..., None] | None, optional
            Explicit client-close capability for temporary management clients.
            When omitted, compatibility resolution falls back to the interface.
        """
        self._iface = iface
        self._ble_client_factory = ble_client_factory
        self._connected_elsewhere = connected_elsewhere
        self._session = _session_state_for(iface, session_state)
        self._management = _management_state_for(iface, management_state)
        self._connect_lock = (
            connect_lock
            or _get_declared_lock(iface, "_connect_lock")
            or cast(_LockPort, threading.RLock())
        )
        self._client_closer = client_closer
        self._iface_override_dispatch_guard = threading.local()

    def _close_client(self, client: BLEClient) -> None:
        """Close a management client through the injected/fallback capability."""
        closer = self._client_closer or _get_declared_callable(
            self._iface, "_client_manager_safe_close_client"
        )
        if closer is None:
            raise AttributeError(
                "BLE management collaborator is missing client cleanup capability"
            )
        closer(client)

    def _active_iface_override_dispatches(self) -> set[str]:
        """Return per-thread set of in-flight iface override dispatch names."""
        active = getattr(self._iface_override_dispatch_guard, "value", None)
        if active is None:
            active = set()
            self._iface_override_dispatch_guard.value = active
        return cast(set[str], active)

    def _call_iface_override(
        self,
        method_name: str,
        fallback: Callable[..., T],
        *args: object,
        **kwargs: object,
    ) -> T:
        """Call iface override when present, else fallback.

        Uses normal attribute lookup so subclass/class-level overrides are
        honored, while still ignoring the default BLEInterface wrapper methods
        unless an explicit instance override is installed. A per-thread
        reentrancy guard prevents override paths that call ``super()`` from
        recursively re-entering this dispatcher.
        """
        active_dispatches = self._active_iface_override_dispatches()
        if method_name in active_dispatches:
            return fallback(*args, **kwargs)

        override = _get_declared_callable(self._iface, method_name)
        if override is not None:

            def _invoke_override() -> T:
                active_dispatches.add(method_name)
                try:
                    return cast(T, override(*args, **kwargs))
                finally:
                    active_dispatches.discard(method_name)

            instance_dict = _get_declared_member(self._iface, "__dict__", {})
            if isinstance(instance_dict, dict) and method_name in instance_dict:
                return _invoke_override()
            try:
                # Loaded by string: the interface module imports this module, so
                # a module-level import would be circular.
                interface_module = _load_runtime_module(
                    "meshtastic.interfaces.ble.interface"
                )
                ble_interface_type = interface_module.BLEInterface
                base_method = inspect.getattr_static(
                    ble_interface_type, method_name, None
                )
                class_method = inspect.getattr_static(
                    type(self._iface), method_name, None
                )
                if class_method is not None and class_method is not base_method:
                    return _invoke_override()
            except Exception:  # noqa: BLE001 - lookup fallback
                logger.debug(
                    "Override lookup fallback suppressed exception for %s",
                    method_name,
                    exc_info=True,
                )
        return fallback(*args, **kwargs)

    def resolve_target_address_for_management(self, address: str | None) -> str:
        """Resolve a management target to a concrete BLE address."""
        iface = self._iface
        requested_identifier = address if address is not None else iface.address
        if _is_blank_or_malformed_address_like(address):
            raise iface.BLEError(ERROR_MANAGEMENT_ADDRESS_EMPTY)
        if (
            address is None
            and requested_identifier is not None
            and _is_blank_or_malformed_address_like(requested_identifier)
        ):
            raise iface.BLEError(ERROR_MANAGEMENT_ADDRESS_EMPTY)
        if address is None:
            with self._session.lock:
                current_client = iface.client
            if self._is_client_connected(current_client):
                current_address = iface._extract_client_address(current_client)
                if current_address:
                    return (
                        _coerce_concrete_ble_address(current_address) or current_address
                    )
            if requested_identifier is None:
                raise iface.BLEError(ERROR_MANAGEMENT_ADDRESS_REQUIRED)
        if requested_identifier is None:
            raise iface.BLEError(ERROR_MANAGEMENT_ADDRESS_REQUIRED)
        normalized_request = sanitize_address(requested_identifier)
        if normalized_request is not None:
            existing_client = iface._get_existing_client_if_valid(normalized_request)
            existing_client_address = iface._extract_client_address(existing_client)
            if existing_client_address:
                return (
                    _coerce_concrete_ble_address(existing_client_address)
                    or existing_client_address
                )
            if _looks_like_ble_address(normalized_request):
                return _normalized_mac_to_colon_address(normalized_request)
        discovered_address = iface.findDevice(requested_identifier).address
        return _coerce_concrete_ble_address(discovered_address) or discovered_address

    @staticmethod
    def management_target_gate(target_address: str) -> ExitStack:
        """Return a context manager that serializes management work by address."""
        stack = ExitStack()
        target_key = _addr_key(target_address)
        if target_key is not None:
            addr_lock = stack.enter_context(_addr_lock_context(target_key))
            stack.enter_context(addr_lock)
        return stack

    def get_management_client_if_available(
        self, address: str | None
    ) -> BLEClient | None:
        """Return active/reusable management client when available."""
        with self._session.lock:
            return self.get_management_client_if_available_locked(address)

    def get_management_client_if_available_locked(
        self, address: str | None
    ) -> BLEClient | None:
        """Return active/reusable management client when state lock is already held."""
        iface = self._iface
        requested_identifier = address if address is not None else iface.address
        current_client = (
            iface.client
            if address is None and not getattr(iface, "_client_publish_pending", False)
            else None
        )
        if self._is_client_connected(current_client):
            return current_client
        normalized_request = sanitize_address(requested_identifier)
        if address is not None and normalized_request is None:
            return None
        return iface._get_existing_client_if_valid(normalized_request)

    def get_management_client_for_target(
        self,
        target_address: str,
        *,
        prefer_current_client: bool,
    ) -> BLEClient | None:
        """Return reusable management client matching ``target_address``."""
        with self._session.lock:
            return self.get_management_client_for_target_locked(
                target_address,
                prefer_current_client=prefer_current_client,
            )

    def get_management_client_for_target_locked(
        self,
        target_address: str,
        *,
        prefer_current_client: bool,
    ) -> BLEClient | None:
        """Return reusable management client matching ``target_address``.

        Caller must already hold ``_state_lock``.
        """
        iface = self._iface
        current_client = (
            iface.client
            if prefer_current_client
            and not getattr(iface, "_client_publish_pending", False)
            else None
        )
        if self._is_client_connected(current_client):
            current_address = iface._extract_client_address(current_client)
            if _same_management_binding(current_address, target_address):
                return current_client
        target_lookup = sanitize_address(target_address) or target_address
        return iface._get_existing_client_if_valid(target_lookup)

    @staticmethod
    def _is_client_connected(client: BLEClient | None) -> bool:
        """Return whether a candidate client appears currently connected."""
        try:
            return _client_is_connected_compat(client)
        except AttributeError:
            return False

    def get_current_implicit_management_binding_locked(self) -> str | None:
        """Return current implicit management binding while holding state lock."""
        iface = self._iface
        current_client = iface.client
        if self._is_client_connected(current_client):
            client_address = iface._extract_client_address(current_client)
            if client_address is not None:
                return client_address
        return iface.address

    def get_current_implicit_management_address_locked(self) -> str | None:
        """Return current implicit management target when already concrete."""
        current_binding = self.get_current_implicit_management_binding_locked()
        if current_binding and _looks_like_ble_address(current_binding):
            return current_binding
        return None

    def revalidate_implicit_management_target(
        self,
        expected_target_address: str,
        *,
        expected_binding: str | None = None,
    ) -> None:
        """Abort when implicit management target changed while waiting on gate."""
        iface = self._iface
        with self._session.lock:
            current_binding = self.get_current_implicit_management_binding_locked()
        if current_binding is None:
            raise iface.BLEError(ERROR_MANAGEMENT_TARGET_CHANGED)

        if _looks_like_ble_address(current_binding):
            current_target_address = current_binding
        else:
            if expected_binding is None or not _same_management_binding(
                current_binding,
                expected_binding,
            ):
                raise iface.BLEError(ERROR_MANAGEMENT_TARGET_CHANGED)
            current_target_address = expected_target_address

        if not _same_management_binding(
            current_target_address, expected_target_address
        ):
            raise iface.BLEError(ERROR_MANAGEMENT_TARGET_CHANGED)

    def begin_management_operation_locked(self) -> None:
        """Record a management operation while holding ``_management_lock``."""
        self._management._begin_locked()

    def finish_management_operation(self) -> None:
        """Mark completion of an in-flight management operation."""
        if not self._management.finish():
            logger.warning(
                "Management operation accounting underflow detected during finish(); resetting inflight count to zero."
            )

    def start_management_phase(self, address: str | None) -> _ManagementStartContext:
        """Begin management operation and capture startup context."""
        iface = self._iface
        expected_implicit_binding: str | None = None
        target_address: str | None = None
        use_existing_client_without_resolved_address = False
        operation_started = False
        try:
            with self._connect_lock, self._management.lock, self._session.lock:
                iface._validate_management_preconditions()
                if address is None:
                    expected_implicit_binding = (
                        self.get_current_implicit_management_binding_locked()
                    )
                self.begin_management_operation_locked()
                operation_started = True
                existing_client = self._call_iface_override(
                    "_get_management_client_if_available_locked",
                    self.get_management_client_if_available_locked,
                    address,
                )
                target_address = iface._extract_client_address(existing_client)
                use_existing_client_without_resolved_address = (
                    existing_client is not None and target_address is None
                )
        except BaseException:
            if operation_started:
                self.finish_management_operation()
            raise
        return _ManagementStartContext(
            expected_implicit_binding=expected_implicit_binding,
            target_address=target_address,
            use_existing_client_without_resolved_address=use_existing_client_without_resolved_address,
        )

    # COMPAT_STABLE_SHIM: historical helper alias retained for compatibility targets.
    def _start_management_phase(self, address: str | None) -> _ManagementStartContext:
        """Compatibility alias for `start_management_phase`."""
        return self.start_management_phase(address)

    def _resolve_target_from_existing_client(
        self,
        address: str | None,
        start_context: _ManagementStartContext,
    ) -> tuple[str | None, BLEClient | None]:
        """Resolve target when startup detected a connected client without address."""
        iface = self._iface
        refreshed_existing_client: BLEClient | None = None
        current_binding: str | None = None
        target_candidate: str | None = None
        use_refreshed_existing_client = True

        if address is None:
            with self._connect_lock, self._management.lock, self._session.lock:
                iface._validate_management_preconditions()
                refreshed_existing_client = self._call_iface_override(
                    "_get_management_client_if_available_locked",
                    self.get_management_client_if_available_locked,
                    address,
                )
                current_binding = self._call_iface_override(
                    "_get_current_implicit_management_binding_locked",
                    self.get_current_implicit_management_binding_locked,
                )
                if not _same_management_binding(
                    current_binding,
                    start_context.expected_implicit_binding,
                ):
                    raise iface.BLEError(ERROR_MANAGEMENT_TARGET_CHANGED)
                if refreshed_existing_client is not None:
                    target_candidate = iface._extract_client_address(
                        refreshed_existing_client
                    )
                else:
                    use_refreshed_existing_client = False
        else:
            with self._connect_lock, self._management.lock, self._session.lock:
                iface._validate_management_preconditions()
                refreshed_existing_client = self._call_iface_override(
                    "_get_management_client_if_available_locked",
                    self.get_management_client_if_available_locked,
                    address,
                )
                if refreshed_existing_client is not None:
                    target_candidate = iface._extract_client_address(
                        refreshed_existing_client
                    )
                    if target_candidate is None:
                        # Cannot safely gate an unknown-address client; force
                        # reacquisition by resolved concrete target.
                        use_refreshed_existing_client = False
                else:
                    use_refreshed_existing_client = False
            if target_candidate is None:
                target_candidate = self._call_iface_override(
                    "_resolve_target_address_for_management",
                    self.resolve_target_address_for_management,
                    address,
                )

        if (
            target_candidate is None
            and current_binding is not None
            and not use_refreshed_existing_client
        ):
            target_candidate = self._call_iface_override(
                "_resolve_target_address_for_management",
                self.resolve_target_address_for_management,
                current_binding,
            )

        target_address: str | None = None
        if target_candidate is not None:
            candidate = target_candidate.strip() or None
            if candidate is not None and _looks_like_ble_address(candidate):
                target_address = candidate
        return (
            target_address,
            refreshed_existing_client if use_refreshed_existing_client else None,
        )

    def resolve_management_target(
        self,
        address: str | None,
        start_context: _ManagementStartContext,
    ) -> tuple[str | None, BLEClient | None]:
        """Resolve management target address and optional existing client."""
        target_address = start_context.target_address
        if (
            target_address is None
            and not start_context.use_existing_client_without_resolved_address
        ):
            target_address = self._call_iface_override(
                "_resolve_target_address_for_management",
                self.resolve_target_address_for_management,
                address,
            )

        if start_context.use_existing_client_without_resolved_address:
            return self._resolve_target_from_existing_client(address, start_context)

        return target_address, None

    # COMPAT_STABLE_SHIM: historical helper alias retained for compatibility targets.
    def _resolve_management_target(
        self,
        address: str | None,
        start_context: _ManagementStartContext,
    ) -> tuple[str | None, BLEClient | None]:
        """Compatibility alias for `resolve_management_target`."""
        return self.resolve_management_target(address, start_context)

    def acquire_client_for_target(
        self,
        *,
        address: str | None,
        target_address: str,
        expected_implicit_binding: str | None,
    ) -> tuple[BLEClient, BLEClient | None]:
        """Acquire active or temporary client for resolved management target."""
        iface = self._iface
        client_to_use: BLEClient | None = None
        target_key: str | None = None
        temporary_client: BLEClient | None = None

        with self._connect_lock, self._management.lock, self._session.lock:
            iface._validate_management_preconditions()
            if address is None:
                self._call_iface_override(
                    "_revalidate_implicit_management_target",
                    self.revalidate_implicit_management_target,
                    target_address,
                    expected_binding=expected_implicit_binding,
                )
            client_to_use = self._call_iface_override(
                "_get_management_client_for_target_locked",
                self.get_management_client_for_target_locked,
                target_address,
                prefer_current_client=address is None,
            )
            if client_to_use is None:
                target_key = _addr_key(target_address)

        if client_to_use is None:
            if target_key is not None and self._connected_elsewhere(target_key, iface):
                raise iface.BLEError(ERROR_CONNECTION_SUPPRESSED)
            temporary_client = _create_management_client(
                self._ble_client_factory, target_address
            )
            client_to_use = temporary_client

        return client_to_use, temporary_client

    # COMPAT_STABLE_SHIM: historical helper alias retained for compatibility targets.
    def _acquire_client_for_target(
        self,
        *,
        address: str | None,
        target_address: str,
        expected_implicit_binding: str | None,
    ) -> tuple[BLEClient, BLEClient | None]:
        """Compatibility alias for `acquire_client_for_target`."""
        return self.acquire_client_for_target(
            address=address,
            target_address=target_address,
            expected_implicit_binding=expected_implicit_binding,
        )

    def execute_with_client(
        self,
        *,
        client_to_use: BLEClient,
        temporary_client: BLEClient | None,
        command: Callable[[BLEClient], T],
    ) -> T:
        """Execute management command and close temporary client on exit."""
        try:
            return command(client_to_use)
        finally:
            if temporary_client is not None:
                try:
                    self._close_client(temporary_client)
                except (
                    Exception
                ):  # noqa: BLE001 - best-effort cleanup must not mask command outcome
                    logger.debug(
                        "Failed to close temporary management client.",
                        exc_info=True,
                    )

    # COMPAT_STABLE_SHIM: historical helper alias retained for compatibility targets.
    def _execute_with_client(
        self,
        *,
        client_to_use: BLEClient,
        temporary_client: BLEClient | None,
        command: Callable[[BLEClient], T],
    ) -> T:
        """Compatibility alias for `execute_with_client`."""
        return self.execute_with_client(
            client_to_use=client_to_use,
            temporary_client=temporary_client,
            command=command,
        )

    def execute_management_command(
        self,
        address: str | None,
        command: Callable[[BLEClient], T],
    ) -> T:
        """Run management command using active, existing, or temporary BLE client.

        Parameters
        ----------
        address : str | None
            Explicit management target address. ``None`` means use current
            implicit management binding.
        command : Callable[[BLEClient], T]
            Management callable executed against the selected client.

        Returns
        -------
        T
            Result returned by ``command``.
        """
        iface = self._iface
        if _is_blank_or_malformed_address_like(address):
            raise iface.BLEError(ERROR_MANAGEMENT_ADDRESS_EMPTY)

        management_started = False
        try:
            start_context = self._start_management_phase(address)
            management_started = True
            target_address, refreshed_existing_client = self._resolve_management_target(
                address,
                start_context,
            )

            if target_address is None:
                if refreshed_existing_client is not None:
                    with self._connect_lock, self._management.lock, self._session.lock:
                        iface._validate_management_preconditions()
                        if (
                            address is None
                            and start_context.expected_implicit_binding is not None
                        ):
                            current_binding = self._call_iface_override(
                                "_get_current_implicit_management_binding_locked",
                                self.get_current_implicit_management_binding_locked,
                            )
                            if not _same_management_binding(
                                current_binding,
                                start_context.expected_implicit_binding,
                            ):
                                raise iface.BLEError(ERROR_MANAGEMENT_TARGET_CHANGED)
                        client_still_connected = self._is_client_connected(
                            refreshed_existing_client
                        )
                    if client_still_connected:
                        gate_target = start_context.expected_implicit_binding
                        if gate_target is None or _addr_key(gate_target) is None:
                            gate_target = iface._extract_client_address(
                                refreshed_existing_client
                            )
                        if gate_target is None or _addr_key(gate_target) is None:
                            gate_target = _synthetic_gate_address_for_client(
                                refreshed_existing_client
                            )
                        with self._call_iface_override(
                            "_management_target_gate",
                            self.management_target_gate,
                            gate_target,
                        ):
                            return command(refreshed_existing_client)
                    raise iface.BLEError(ERROR_MANAGEMENT_TARGET_CHANGED)
                raise iface.BLEError(ERROR_MANAGEMENT_ADDRESS_REQUIRED)

            with self._call_iface_override(
                "_management_target_gate",
                self.management_target_gate,
                target_address,
            ):
                if refreshed_existing_client is not None:
                    with self._connect_lock, self._management.lock, self._session.lock:
                        iface._validate_management_preconditions()
                        if address is None:
                            self._call_iface_override(
                                "_revalidate_implicit_management_target",
                                self.revalidate_implicit_management_target,
                                target_address,
                                expected_binding=start_context.expected_implicit_binding,
                            )
                    if self._is_client_connected(refreshed_existing_client):
                        return command(refreshed_existing_client)
                client_to_use, temporary_client = self._acquire_client_for_target(
                    address=address,
                    target_address=target_address,
                    expected_implicit_binding=start_context.expected_implicit_binding,
                )
                return self._execute_with_client(
                    client_to_use=client_to_use,
                    temporary_client=temporary_client,
                    command=command,
                )
        finally:
            if management_started:
                self.finish_management_operation()

    def validate_management_await_timeout(self, await_timeout: object) -> float:
        """Validate bounded await timeout for management operations."""
        iface = self._iface
        if (
            await_timeout is None
            or isinstance(await_timeout, bool)
            or not isinstance(await_timeout, numbers.Real)
            or not math.isfinite(await_timeout)
            or await_timeout <= 0
        ):
            raise iface.BLEError(ERROR_MANAGEMENT_AWAIT_TIMEOUT_INVALID)
        return float(await_timeout)

    def validate_trust_timeout(self, timeout: object) -> float:
        """Validate bounded timeout for trust command."""
        iface = self._iface
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, numbers.Real)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise iface.BLEError(ERROR_TRUST_INVALID_TIMEOUT)
        return float(timeout)

    def validate_connect_timeout_override(
        self,
        connect_timeout: object,
        *,
        pair_on_connect: bool,
    ) -> None:
        """Validate optional connect-timeout override."""
        iface = self._iface
        if connect_timeout is None:
            return
        if isinstance(connect_timeout, bool) or not isinstance(
            connect_timeout, numbers.Real
        ):
            exc = ValueError(
                "connect_timeout must be a finite positive number of seconds."
            )
            raise iface.BLEError(ERROR_INVALID_CONNECT_TIMEOUT.format(exc=exc)) from exc
        try:
            ConnectionOrchestrator._resolve_connect_timeout(
                pair_on_connect=pair_on_connect,
                connect_timeout=float(connect_timeout),
            )
        except (TypeError, ValueError) as exc:
            raise iface.BLEError(ERROR_INVALID_CONNECT_TIMEOUT.format(exc=exc)) from exc

    def pair(
        self,
        address: str | None = None,
        *,
        await_timeout: float,
        kwargs: dict[str, object],
    ) -> None:
        """Pair with BLE device using active or temporary client."""
        if "await_timeout" in kwargs:
            raise self._iface.BLEError(
                "pair kwargs must not include 'await_timeout'; pass await_timeout via the explicit pair() argument."
            )
        validated_timeout = self.validate_management_await_timeout(await_timeout)
        self.execute_management_command(
            address,
            lambda client: client.pair(await_timeout=validated_timeout, **kwargs),
        )

    def unpair(
        self,
        address: str | None = None,
        *,
        await_timeout: float,
    ) -> None:
        """Unpair BLE device using active or temporary client."""
        validated_timeout = self.validate_management_await_timeout(await_timeout)
        self.execute_management_command(
            address,
            lambda client: client.unpair(await_timeout=validated_timeout),
        )

    def run_bluetoothctl_trust_command(
        self,
        bluetoothctl_path: str,
        canonical_address: str,
        validated_timeout: float | None = None,
        *,
        timeout: float | None = None,
        subprocess_module: object,
        trust_hex_blob_re: re.Pattern[str],
        trust_token_re: re.Pattern[str],
        trust_command_output_max_chars: int,
    ) -> None:
        """Run bluetoothctl trust command and map failures to BLEError."""
        iface = self._iface
        command_timeout = timeout if timeout is not None else validated_timeout
        if command_timeout is None:
            raise TypeError(
                "run_bluetoothctl_trust_command requires timeout or validated_timeout"
            )

        def _sanitize_trust_command_output(output: str) -> str:
            sanitized = " ".join(output.splitlines())
            sanitized = "".join(ch if ch.isprintable() else "?" for ch in sanitized)
            sanitized = trust_hex_blob_re.sub("[redacted-hex]", sanitized)
            sanitized = trust_token_re.sub("[redacted-token]", sanitized)
            sanitized = " ".join(sanitized.split())
            max_output_chars = max(trust_command_output_max_chars, 0)
            if len(sanitized) > max_output_chars:
                if max_output_chars <= 3:
                    return sanitized[:max_output_chars]
                max_prefix = max_output_chars - 3
                return f"{sanitized[:max_prefix]}..."
            return sanitized

        logger.debug(
            "Running bluetoothctl trust command: %s (timeout=%.1fs)",
            [bluetoothctl_path, "trust", canonical_address],
            command_timeout,
        )
        timeout_exc = getattr(subprocess_module, "TimeoutExpired", None)
        timeout_exc_type = (
            timeout_exc
            if isinstance(timeout_exc, type) and issubclass(timeout_exc, BaseException)
            else subprocess_stdlib.TimeoutExpired
        )
        try:
            # Safety: command is executed without a shell; `canonical_address` is
            # normalized to strict MAC-octet format and `bluetoothctl_path`
            # comes from shutil.which("bluetoothctl").
            result = subprocess_module.run(  # type: ignore[attr-defined]  # noqa: S603
                [bluetoothctl_path, "trust", canonical_address],
                capture_output=True,
                text=True,
                check=False,
                timeout=command_timeout,
            )
        except (
            Exception
        ) as exc:  # noqa: BLE001 - preserve injected module compatibility
            if isinstance(exc, timeout_exc_type):
                raise iface.BLEError(
                    ERROR_TRUST_COMMAND_TIMEOUT.format(
                        timeout=command_timeout, address=canonical_address
                    )
                ) from exc
            detail = _sanitize_trust_command_output(str(exc))
            raise iface.BLEError(
                ERROR_TRUST_COMMAND_FAILED.format(
                    address=canonical_address,
                    detail=(
                        f"{bluetoothctl_path}: {detail}"
                        if detail
                        else f"{bluetoothctl_path}: unable to execute bluetoothctl"
                    ),
                )
            ) from exc
        if result.returncode != 0:
            stderr_output = (
                _sanitize_trust_command_output(result.stderr) if result.stderr else ""
            )
            stdout_output = (
                _sanitize_trust_command_output(result.stdout) if result.stdout else ""
            )
            detail_parts = []
            if stderr_output:
                detail_parts.append(f"stderr: {stderr_output}")
            if stdout_output:
                detail_parts.append(f"stdout: {stdout_output}")
            detail = " | ".join(detail_parts) or f"exit code {result.returncode}"
            raise iface.BLEError(
                ERROR_TRUST_COMMAND_FAILED.format(
                    address=canonical_address, detail=detail
                )
            )
        logger.info("Trusted BLE device via bluetoothctl: %s", canonical_address)

    def trust(
        self,
        address: str | None = None,
        *,
        timeout: float = BLUETOOTHCTL_TRUST_TIMEOUT_SECONDS,
        sys_module: object,
        shutil_module: object,
        subprocess_module: object,
        trust_hex_blob_re: re.Pattern[str],
        trust_token_re: re.Pattern[str],
        trust_command_output_max_chars: int,
    ) -> None:
        """Mark BLE device as trusted via Linux bluetoothctl."""
        iface = self._iface
        if _is_blank_or_malformed_address_like(address):
            raise iface.BLEError(ERROR_MANAGEMENT_ADDRESS_EMPTY)

        validated_timeout = self.validate_trust_timeout(timeout)
        if not sys_module.platform.startswith("linux"):  # type: ignore[attr-defined]
            raise iface.BLEError(ERROR_TRUST_LINUX_ONLY)
        bluetoothctl_path = shutil_module.which("bluetoothctl")  # type: ignore[attr-defined]
        if bluetoothctl_path is None:
            raise iface.BLEError(ERROR_TRUST_BLUETOOTHCTL_MISSING)

        management_started = False
        try:
            start_context = self._start_management_phase(address)
            management_started = True
            target_address, _ = self._resolve_management_target(
                address,
                start_context,
            )
            if target_address is None:
                implicit_binding = start_context.expected_implicit_binding
                if address is None and implicit_binding is not None:
                    target_address = self._call_iface_override(
                        "_resolve_target_address_for_management",
                        self.resolve_target_address_for_management,
                        implicit_binding,
                    )
                if target_address is None:
                    raise iface.BLEError(ERROR_MANAGEMENT_ADDRESS_REQUIRED)
            canonical_address = iface._format_bluetoothctl_address(target_address)
            with self._call_iface_override(
                "_management_target_gate",
                self.management_target_gate,
                target_address,
            ):
                with self._connect_lock, self._management.lock, self._session.lock:
                    iface._validate_management_preconditions()
                    if address is None:
                        self._call_iface_override(
                            "_revalidate_implicit_management_target",
                            self.revalidate_implicit_management_target,
                            target_address,
                            expected_binding=start_context.expected_implicit_binding,
                        )
                self.run_bluetoothctl_trust_command(
                    bluetoothctl_path,
                    canonical_address,
                    validated_timeout,
                    subprocess_module=subprocess_module,
                    trust_hex_blob_re=trust_hex_blob_re,
                    trust_token_re=trust_token_re,
                    trust_command_output_max_chars=trust_command_output_max_chars,
                )
        finally:
            if management_started:
                self.finish_management_operation()
