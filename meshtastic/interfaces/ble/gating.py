"""Process-wide BLE connection gating utilities.

Lock Ordering Note:
    - Never hold `_REGISTRY_LOCK` while waiting to acquire a per-address lock.
    - `_REGISTRY_LOCK` is for short registry updates only.
    - Per-address locks may be held while calling `_mark_connected()` /
      `_mark_disconnected()`, which each acquire `_REGISTRY_LOCK` internally.
      This prevents registry<->address lock inversion in concurrent
      connect/disconnect paths.

Context Manager Usage:
    Prefer using _addr_lock_context() over manual _get_addr_lock()/_release_addr_lock()
    or _get_addr_lock_by_key()/_release_addr_lock_by_key() calls to ensure
    proper holder-count management:

        with _addr_lock_context(address) as lock:
            with lock:
                # Connection logic here
                # On success: _mark_connected(address) records the address in _CONNECTED_ADDRS
                # (it does not touch _LOCK_HOLDERS), which prevents _release_addr_lock_by_key
                # from removing the lock entry when the context exits.
                # On failure/exception: context manager releases holder count

    Note: _addr_lock_context() yields the per-address RLock handle but does not
    acquire it; callers must explicitly acquire it with `with lock:`.
"""

import dataclasses
import logging
import math
import time
import weakref
from collections.abc import Generator
from contextlib import contextmanager
from threading import Lock, RLock
from typing import Any

from meshtastic.interfaces.ble.constants import (
    CONNECTION_TIMEOUT,
    DISCONNECT_TIMEOUT_SECONDS,
    BLEConfig,
)
from meshtastic.interfaces.ble.utils import sanitize_address

logger = logging.getLogger("meshtastic.ble")


@dataclasses.dataclass(frozen=True)
class _InflightConnectFenceRegistration:
    """Owner identity attached to one in-flight direct connect's fence.

    ``owner_ref`` tracks weak-referenceable owners without extending their
    lifetime. ``owner_strong`` retains a non-weak-referenceable owner until
    retirement or expiry, so a recycled ``id()`` can never grant a different
    caller an exemption. Only the identical live owner is exempt.
    """

    owner_ref: weakref.ReferenceType[Any] | None
    owner_strong: object | None


class _BLEAddressRegistry:
    """Own process-wide BLE address claims and per-address lock bookkeeping.

    The registry object owns all mutable gating state.  Module-level aliases are
    retained below because a small amount of historical test/support code imports
    the collections directly.  Runtime code should treat this object as the
    ownership boundary and use the existing module-level operations rather than
    mutating the collections itself.
    """

    def __init__(self) -> None:
        """Initialize empty ownership, claim, and lock registries."""
        # Invariant: never rebind these collections after construction. The
        # module-level compatibility aliases below capture object identity, so
        # registry reset must mutate in place (for example via ``.clear()``).
        self.lock = RLock()
        self.addr_locks: dict[str, RLock] = {}
        self.connected_addrs: set[str] = set()
        self.connecting_addrs: set[str] = set()
        self.connected_owners: dict[str, weakref.ReferenceType[Any] | None] = {}
        self.connected_owner_ids: dict[str, int | None] = {}
        self.connected_marked_at: dict[str, float] = {}
        self.connecting_owners: dict[str, weakref.ReferenceType[Any] | None] = {}
        self.connecting_owner_ids: dict[str, int | None] = {}
        self.connecting_marked_at: dict[str, float] = {}
        self.lock_holders: dict[str, int] = {}
        # Addresses fenced because an interrupted connect's native disconnect
        # cleanup exceeded its bounded wait. Maps normalized address key to
        # the monotonic deadline after which the fence expires.
        self.interrupted_cleanup_fences: dict[str, float] = {}
        # Every overrunning cleanup has an independent retirement. A newer
        # cleanup finishing must not release an older still-running disconnect.
        self.interrupted_cleanup_pending: dict[str, set[float]] = {}
        # Addresses fenced while a direct connect attempt is in flight in
        # this process. Maps normalized address key to that attempt's
        # registrations keyed by retirement token (a distinct monotonic
        # deadline); each registration carries its caller's owner identity
        # for the per-caller exemption.
        self.inflight_connect_fences: dict[
            str, dict[float, _InflightConnectFenceRegistration]
        ] = {}

    def clear(self) -> None:
        """Clear every registry collection while holding the owner lock."""
        with self.lock:
            self.addr_locks.clear()
            self.connected_addrs.clear()
            self.connecting_addrs.clear()
            self.connected_marked_at.clear()
            self.connecting_marked_at.clear()
            self.connected_owner_ids.clear()
            self.connecting_owner_ids.clear()
            self.connected_owners.clear()
            self.connecting_owners.clear()
            self.lock_holders.clear()
            self.interrupted_cleanup_fences.clear()
            self.interrupted_cleanup_pending.clear()
            self.inflight_connect_fences.clear()


_DEFAULT_REGISTRY = _BLEAddressRegistry()

# Internal compatibility aliases: keep existing imported collection identities
# stable while making `_DEFAULT_REGISTRY` the single owner of mutable state.
_REGISTRY_LOCK = _DEFAULT_REGISTRY.lock
_ADDR_LOCKS = _DEFAULT_REGISTRY.addr_locks
_CONNECTED_ADDRS = _DEFAULT_REGISTRY.connected_addrs
_CONNECTING_ADDRS = _DEFAULT_REGISTRY.connecting_addrs
_CONNECTED_OWNERS = _DEFAULT_REGISTRY.connected_owners
_CONNECTED_OWNER_IDS = _DEFAULT_REGISTRY.connected_owner_ids
_CONNECTED_MARKED_AT = _DEFAULT_REGISTRY.connected_marked_at
_CONNECTING_OWNERS = _DEFAULT_REGISTRY.connecting_owners
_CONNECTING_OWNER_IDS = _DEFAULT_REGISTRY.connecting_owner_ids
_CONNECTING_MARKED_AT = _DEFAULT_REGISTRY.connecting_marked_at
_LOCK_HOLDERS = _DEFAULT_REGISTRY.lock_holders
_INTERRUPTED_CLEANUP_FENCES = _DEFAULT_REGISTRY.interrupted_cleanup_fences
_INTERRUPTED_CLEANUP_PENDING = _DEFAULT_REGISTRY.interrupted_cleanup_pending
_INFLIGHT_CONNECT_FENCES = _DEFAULT_REGISTRY.inflight_connect_fences

# BLEClient instances register themselves here so stale-link clearance can
# recognize a direct client's live link. Entries are weak: a client that goes
# away stops protecting its address. Connection state is probed outside this
# lock because isConnected() reads backend state.
_LIVE_BLE_CLIENTS: weakref.WeakSet[Any] = weakref.WeakSet()
_LIVE_BLE_CLIENTS_LOCK = Lock()


def _register_live_ble_client(client: Any) -> None:
    """Track a BLEClient instance for stale-link clearance decisions.

    Parameters
    ----------
    client : Any
        BLEClient-compatible object. The registry keeps only a weak reference,
        so the caller's lifetime controls the entry.
    """
    with _LIVE_BLE_CLIENTS_LOCK:
        _LIVE_BLE_CLIENTS.add(client)


def _live_ble_client_owns_address(addr: str | None) -> bool:
    """Return whether a live BLEClient in this process is connected to ``addr``.

    Direct ``BLEClient`` connections do not register a per-address claim, so
    stale-link clearance cannot see them in the gate registry. Probing the live
    clients closes that gap: clearance must not disconnect a link this process
    is still using.

    Parameters
    ----------
    addr : str | None
        BLE address to check, normalized through :func:`_addr_key`.

    Returns
    -------
    bool
        True when a tracked client reports connected for the address.
    """
    key = _addr_key(addr)
    if key is None:
        return False
    with _LIVE_BLE_CLIENTS_LOCK:
        clients = list(_LIVE_BLE_CLIENTS)
    for client in clients:
        try:
            is_connected = getattr(client, "isConnected", None)
            if not callable(is_connected) or not is_connected():
                continue
            if _addr_key(getattr(client, "address", None)) == key:
                return True
        except Exception:  # noqa: BLE001 - a broken probe must not block clearance
            logger.debug("Failed to probe a live BLE client for %s", key, exc_info=True)
    return False


def _register_interrupted_cleanup_fence(addr: str | None) -> float:
    """Fence an address while an interrupted connect's cleanup is pending.

    A direct ``BLEClient`` connect that is interrupted waits a bounded time
    for its native disconnect cleanup. When that wait expires, a BlueZ
    disconnect may still be pending; the fence recorded here lets both
    connect paths refuse a new link to the same address so it cannot race
    the straggler disconnect. Registration is idempotent per address and
    refreshes the deadline, so repeated overrunning cleanups extend the
    fence. Each cleanup retains its own retirement token until it finishes;
    one cleanup cannot release another's fence. The deadline backstop is read from
    :data:`BLEConfig.INTERRUPTED_CLEANUP_FENCE_MAX_SECONDS` at call time.

    Parameters
    ----------
    addr : str | None
        Address to fence; normalized via :func:`_addr_key`. None or empty
        is a no-op that returns ``0.0``.

    Returns
    -------
    float
        The monotonic deadline until which the fence refuses connects, or
        ``0.0`` when the address is unusable.
    """
    key = _addr_key(addr)
    if key is None:
        return 0.0
    max_seconds = BLEConfig.INTERRUPTED_CLEANUP_FENCE_MAX_SECONDS
    if (
        not isinstance(max_seconds, (int, float))
        or isinstance(max_seconds, bool)
        or not math.isfinite(max_seconds)
        or max_seconds <= 0
    ):
        logger.warning(
            "Invalid interrupted-cleanup fence timeout %r; using the safe default",
            max_seconds,
        )
        max_seconds = DISCONNECT_TIMEOUT_SECONDS * 6.0
    deadline = time.monotonic() + max_seconds
    with _REGISTRY_LOCK:
        pending = _INTERRUPTED_CLEANUP_PENDING.setdefault(key, set())
        # Treat the deadline as this cleanup's retirement token. Even on a
        # coarse clock, successive registrations must have distinct tokens.
        if pending:
            deadline = max(deadline, math.nextafter(max(pending), math.inf))
        pending.add(deadline)
        _INTERRUPTED_CLEANUP_FENCES[key] = max(pending)
    logger.debug(
        "Fenced %s while interrupted connect cleanup is pending (deadline %.1fs)",
        key,
        deadline,
    )
    return deadline


def _retire_interrupted_cleanup_fence(
    addr: str | None, expected_deadline: float | None = None
) -> None:
    """Release an interrupted-cleanup fence once the pending disconnect retires.

    Idempotent. When ``expected_deadline`` is provided, only that cleanup's
    registration is retired; other pending cleanups keep the address fenced.
    With no deadline, all registrations are explicitly cleared.

    Parameters
    ----------
    addr : str | None
        Address whose fence should be released; normalized via
        :func:`_addr_key`. None or empty is a no-op.
    expected_deadline : float | None
        Deadline the fence must still carry to be released. (Default
        value = None)
    """
    key = _addr_key(addr)
    if key is None:
        return
    with _REGISTRY_LOCK:
        deadline = _INTERRUPTED_CLEANUP_FENCES.get(key)
        if deadline is None:
            return
        if expected_deadline is None:
            _INTERRUPTED_CLEANUP_PENDING.pop(key, None)
            _INTERRUPTED_CLEANUP_FENCES.pop(key, None)
        else:
            pending = _INTERRUPTED_CLEANUP_PENDING.get(key)
            if pending is None or expected_deadline not in pending:
                return
            pending.remove(expected_deadline)
            if pending:
                _INTERRUPTED_CLEANUP_FENCES[key] = max(pending)
            else:
                _INTERRUPTED_CLEANUP_PENDING.pop(key, None)
                _INTERRUPTED_CLEANUP_FENCES.pop(key, None)
    logger.debug(
        "Released interrupted-cleanup fence for %s (pending disconnect retired "
        "or fence expired)",
        key,
    )


def _interrupted_cleanup_fenced(addr: str | None) -> bool:
    """Return whether an interrupted-cleanup fence currently covers ``addr``.

    An expired fence is pruned lazily here, so an address can never stay
    fenced permanently even if the retirement watcher never runs.

    Parameters
    ----------
    addr : str | None
        Address to check; normalized via :func:`_addr_key`.

    Returns
    -------
    bool
        True while a non-expired fence refuses new connects to the address.
    """
    key = _addr_key(addr)
    if key is None:
        return False
    with _REGISTRY_LOCK:
        deadline = _INTERRUPTED_CLEANUP_FENCES.get(key)
        if deadline is None:
            return False
        if time.monotonic() >= deadline:
            _INTERRUPTED_CLEANUP_FENCES.pop(key, None)
            _INTERRUPTED_CLEANUP_PENDING.pop(key, None)
            logger.debug("Interrupted-cleanup fence for %s expired by deadline", key)
            return False
        return True


def _register_inflight_connect_fence(
    addr: str | None,
    owner: object | None = None,
    *,
    minimum_seconds: float | None = None,
) -> float:
    """Fence an address while a direct connect attempt to it is in flight.

    A direct connect attempt registers this fence when it starts and retires
    it when it exits, so concurrent connects to the same address from other
    in-process callers are refused for the whole window where the in-flight
    attempt holds no connected or provisional claim yet (before
    ``_mark_connecting`` and after the per-address lock is released). The
    registering caller records its owner identity so its own later consults
    are exempt (per-caller exemption); every other caller is refused.
    Registration is idempotent per address and refreshes the deadline, and
    every registration keeps its own retirement token, so a newer attempt
    refreshing the fence is never released by an older attempt's exit.

    The deadline backstop is a safety net for paths that never reach the
    ``try/finally`` retirement, such as the owning thread dying mid-connect.
    It reads :data:`BLEConfig.INFLIGHT_CONNECT_FENCE_MAX_SECONDS` at call
    time and extends the bound when the caller has a larger effective
    connection budget. The usual default is twice ``CONNECTION_TIMEOUT``;
    an explicit longer connection timeout must not leave a live attempt
    unprotected merely because the default deadline expired.

    Parameters
    ----------
    addr : str | None
        Address to fence; normalized via :func:`_addr_key`. None or empty
        is a no-op that returns ``0.0``.
    owner : object | None
        Caller performing the in-flight connect; identity is matched by the
        consult for the per-caller exemption. (Default value = None)
    minimum_seconds : float | None
        Optional lower bound on the fence lifetime. The caller uses twice
        its effective connect timeout so an extended connect budget cannot
        outlive the default fence and admit another connection.

    Returns
    -------
    float
        The retirement token: the monotonic deadline until which the fence
        refuses other callers, or ``0.0`` when the address is unusable.
    """
    key = _addr_key(addr)
    if key is None:
        return 0.0
    max_seconds = BLEConfig.INFLIGHT_CONNECT_FENCE_MAX_SECONDS
    if (
        not isinstance(max_seconds, (int, float))
        or isinstance(max_seconds, bool)
        or not math.isfinite(max_seconds)
        or max_seconds <= 0
    ):
        logger.warning(
            "Invalid in-flight connect fence timeout %r; using the safe default",
            max_seconds,
        )
        max_seconds = CONNECTION_TIMEOUT * 2.0
    if minimum_seconds is not None and (
        isinstance(minimum_seconds, (int, float))
        and not isinstance(minimum_seconds, bool)
        and math.isfinite(minimum_seconds)
        and minimum_seconds > 0
    ):
        max_seconds = max(max_seconds, minimum_seconds)
    deadline = time.monotonic() + max_seconds
    with _REGISTRY_LOCK:
        pending = _INFLIGHT_CONNECT_FENCES.setdefault(key, {})
        # Treat the deadline as this attempt's retirement token. Even on a
        # coarse clock, successive registrations must have distinct tokens.
        if pending:
            deadline = max(deadline, math.nextafter(max(pending), math.inf))
        owner_ref = _owner_ref(owner)
        pending[deadline] = _InflightConnectFenceRegistration(
            owner_ref=owner_ref,
            owner_strong=owner if owner_ref is None else None,
        )
    logger.debug(
        "Fenced %s while a direct connect attempt is in flight (deadline %.1fs)",
        key,
        deadline,
    )
    return deadline


def _retire_inflight_connect_fence(addr: str | None, expected_deadline: float) -> None:
    """Release one in-flight direct connect registration by its token.

    Only the registration carrying ``expected_deadline`` is retired; a newer
    attempt's registration keeps the address fenced. This mirrors the
    per-caller retirement-token pattern of the interrupted-cleanup fence.
    Unlike that fence there is deliberately no retire-all form: an in-flight
    attempt must never be able to release another attempt's fence.

    Parameters
    ----------
    addr : str | None
        Address whose fence should be released; normalized via
        :func:`_addr_key`. None or empty is a no-op.
    expected_deadline : float
        Retirement token returned by the attempt's own registration.
    """
    key = _addr_key(addr)
    if key is None:
        return
    with _REGISTRY_LOCK:
        pending = _INFLIGHT_CONNECT_FENCES.get(key)
        if not pending or expected_deadline not in pending:
            return
        pending.pop(expected_deadline)
        if not pending:
            _INFLIGHT_CONNECT_FENCES.pop(key, None)
    logger.debug(
        "Released in-flight connect fence for %s (connect attempt exited)",
        key,
    )


def _inflight_connect_fenced(addr: str | None, owner: object | None = None) -> bool:
    """Return whether an in-flight direct connect fence refuses ``owner``.

    An expired registration is pruned lazily here, so an address can never
    stay fenced permanently even if its attempt never retired (the deadline
    backstop). The caller that registered the fence — matched by the same
    owner-identity mechanism as the connecting/connected claims (object
    identity, retaining non-weak-referenceable owners until retirement) —
    is exempt only when it owns every active registration.

    Parameters
    ----------
    addr : str | None
        Address to check; normalized via :func:`_addr_key`.
    owner : object | None
        Caller requesting the check; exempt when it matches any pending
        registration's owner. (Default value = None)

    Returns
    -------
    bool
        True when a non-expired fence from a different caller refuses a new
        connect to the address.
    """
    key = _addr_key(addr)
    if key is None:
        return False
    with _REGISTRY_LOCK:
        pending = _INFLIGHT_CONNECT_FENCES.get(key)
        if not pending:
            return False
        now = time.monotonic()
        for token in [t for t in pending if now >= t]:
            pending.pop(token)
            logger.debug("In-flight connect fence for %s expired by deadline", key)
        if not pending:
            _INFLIGHT_CONNECT_FENCES.pop(key, None)
            return False
        if owner is None:
            return True
        # An owner is exempt only if it owns EVERY active registration.
        # Another attempt must remain fenced even if this caller also has
        # its own registration for the same address.
        for registration in pending.values():
            current_owner = (
                registration.owner_ref()
                if registration.owner_ref is not None
                else registration.owner_strong
            )
            if current_owner is not owner:
                return True
        return False


def _clear_all_registries() -> None:
    """Reset all internal BLE gating registries to their empty initial state.

    Acquires the module-wide registry lock to perform a thread-safe clear of:
    per-address lock mapping, connected-address set, connection timestamps,
    owner id and weak-ref mappings, and per-address lock holder counts.

    Warning
    -------
    Ensure no active BLE connections are in progress before calling this
    helper, because it force-clears global ownership and lock state.
    Intended for tests and full process-level reset paths.
    """
    _DEFAULT_REGISTRY.clear()
    with _LIVE_BLE_CLIENTS_LOCK:
        _LIVE_BLE_CLIENTS.clear()


def _addr_key(addr: str | None) -> str | None:
    """Normalize a BLE address into a registry key.

    Parameters
    ----------
    addr : str | None
        Raw BLE address to normalize.

    Returns
    -------
    str | None
        The sanitized address string, or None if the input is None, empty, or contains only whitespace.
    """
    sanitized = sanitize_address(addr)
    return sanitized if sanitized else None


def _get_addr_lock(key: str | None) -> RLock:
    """Get the process-wide reentrant lock for a normalized address key.

    If `key` is None the global registry lock is returned. When a per-address lock is returned,
    its internal holder count is incremented to prevent premature cleanup; callers that obtain
    the lock object but do not acquire it must call `_release_addr_lock()` when finished.

    Parameters
    ----------
    key : str | None
        A raw or already-normalized address key; `None` selects the global registry lock.

    Returns
    -------
    RLock
        The per-address reentrant lock for `key`, or the global registry lock if `key` is None.
    """
    key = _addr_key(key)
    return _get_addr_lock_by_key(key)


def _get_addr_lock_by_key(key: str | None) -> RLock:
    """Get the process-wide reentrant lock for a normalized address key.

    If `key` is None, returns the global registry lock. For per-address locks, ensures a lock exists and increments an
    internal holder count to prevent premature cleanup.

    Parameters
    ----------
    key : str | None
        Normalized address key; `None` selects the global registry lock.

    Returns
    -------
    RLock
        The reentrant lock for `key`, or the global registry lock when `key` is `None`.
    """
    if key is None:
        # The registry lock is a process-wide singleton and is not ref-counted.
        return _REGISTRY_LOCK
    with _REGISTRY_LOCK:
        lock = _ADDR_LOCKS.get(key)
        if lock is None:
            lock = RLock()
            _ADDR_LOCKS[key] = lock
        # Increment holder count to prevent premature cleanup
        _LOCK_HOLDERS[key] = _LOCK_HOLDERS.get(key, 0) + 1
        return lock


def _maybe_remove_addr_lock_entry(key: str) -> None:
    """Remove per-address lock bookkeeping when no holders/claims remain.

    Must be called while holding `_REGISTRY_LOCK`.

    Parameters
    ----------
    key : str
        Normalized address key to evaluate.
    """
    if (
        _LOCK_HOLDERS.get(key, 0) <= 0
        and key not in _CONNECTED_ADDRS
        and key not in _CONNECTING_ADDRS
    ):
        _ADDR_LOCKS.pop(key, None)
        _LOCK_HOLDERS.pop(key, None)
        logger.debug("Cleaned up address lock for %s", key)


def _release_addr_lock(key: str | None) -> None:
    """Decrement the holder count for the per-address lock identified by key.

    If key is None, operates on the global registry lock. The input is normalized to a registry key; when the holder
    count reaches zero and the address is not marked connected, the per-address lock entry may be removed by cleanup
    logic.

    Parameters
    ----------
    key : str | None
        An address or already-normalized key identifying the per-address lock; use None for the registry.
    """
    key = _addr_key(key)
    _release_addr_lock_by_key(key)


def _release_addr_lock_by_key(key: str | None) -> None:
    """Decrement the holder count for a normalized address key and remove its lock when unused.

    If `key` is None this is a no-op. Decrements the internal holder count (never below zero); if the count reaches zero
    and the address is not marked connected, removes the per-address lock and its holder bookkeeping entry.

    Parameters
    ----------
    key : str | None
        Normalized address registry key (or None).
    """
    if key is None:
        return
    with _REGISTRY_LOCK:
        if key in _LOCK_HOLDERS:
            _LOCK_HOLDERS[key] = max(0, _LOCK_HOLDERS[key] - 1)
            _maybe_remove_addr_lock_entry(key)


def _cleanup_addr_lock(key: str | None) -> None:
    """Remove a per-address lock when there are no holders or ownership claims.

    If `key` is None this function does nothing. When `key` is provided, the registry entries for that address (the lock
    and its holder count) are removed only if the tracked holder count is less than or equal to zero; otherwise the
    entries are left intact.

    Parameters
    ----------
    key : str | None
        Normalized address key identifying the per-address lock, or `None` to indicate no action.
    """
    if key is None:
        return
    with _REGISTRY_LOCK:
        holder_count = _LOCK_HOLDERS.get(key, 0)
        if holder_count > 0 or key in _CONNECTED_ADDRS or key in _CONNECTING_ADDRS:
            logger.debug(
                "Skipping cleanup of address lock for %s (holders: %d)",
                key,
                holder_count,
            )
            return
        _maybe_remove_addr_lock_entry(key)


def _owner_ref(owner: Any | None) -> weakref.ReferenceType[Any] | None:
    """Create a weak reference to `owner` when possible.

    If `owner` is None or does not support weak references, returns None.

    Parameters
    ----------
    owner : Any | None
        Owner object for which to create a weak reference.

    Returns
    -------
    weakref.ReferenceType[Any] | None
        A weak reference to `owner`, or `None` if `owner` is `None` or not weak-referenceable.
    """
    if owner is None:
        return None
    try:
        return weakref.ref(owner)
    except TypeError:
        logger.debug(
            "Owner is not weak-referenceable; using id() fallback for gate ownership: %r",
            owner,
        )
        return None


def _owner_connected_state(owner: Any) -> bool | None:
    """Return the connection state reported by an owner via its `_is_connection_connected` attribute.

    If the owner exposes `_is_connection_connected` as a callable, it will be invoked; if it is a boolean, that value is
    used. If the attribute is missing, not a boolean, or invoking the callable raises an exception, `None` is returned.

    Parameters
    ----------
    owner : Any
        Object that may expose an `_is_connection_connected` boolean or callable.

    Returns
    -------
    bool | None
        `True` if the owner reports connected, `False` if it reports disconnected, or `None` if not available or an error occurred while reading it.
    """
    connection_state = getattr(owner, "_is_connection_connected", None)
    if callable(connection_state):
        try:
            connection_state = connection_state()
        except Exception:  # noqa: BLE001 - defensive gate-state probe
            logger.debug(
                "Failed to read owner connection state; preserving gate claim.",
                exc_info=True,
            )
            return None

    if isinstance(connection_state, bool):
        return connection_state
    return None


def _remove_connected_record_locked(key: str) -> None:
    """Remove all connected-state bookkeeping associated with the normalized address key.

    Parameters
    ----------
    key : str
        Normalized address key whose connected-state records should be removed.

    Notes
    -----
    Must be called while holding the module-level `_REGISTRY_LOCK`.
    """
    _CONNECTED_ADDRS.discard(key)
    _CONNECTED_OWNERS.pop(key, None)
    _CONNECTED_OWNER_IDS.pop(key, None)
    _CONNECTED_MARKED_AT.pop(key, None)


def _remove_connecting_record_locked(key: str) -> None:
    """Remove provisional connecting-state bookkeeping for a normalized key.

    Parameters
    ----------
    key : str
        Normalized address key to remove provisional connecting-state records
        for.

    Returns
    -------
    None
        This helper mutates global registry state in place.
    """
    _CONNECTING_ADDRS.discard(key)
    _CONNECTING_OWNERS.pop(key, None)
    _CONNECTING_OWNER_IDS.pop(key, None)
    _CONNECTING_MARKED_AT.pop(key, None)


def _prune_stale_unowned_claim_locked(key: str) -> bool:
    """Prune an unowned claim when its marker timestamp is missing or stale.

    Parameters
    ----------
    key : str
        Normalized address key to evaluate.

    Returns
    -------
    bool
        `True` when a stale claim was removed, `False` when claim remains valid.

    Notes
    -----
    Must be called while holding the reentrant `_REGISTRY_LOCK` (`RLock`).
    This function may call `_cleanup_addr_lock()`, which also acquires
    `_REGISTRY_LOCK`; nested acquisition is intentional and safe because the
    lock is reentrant.
    """
    marked_at = _CONNECTED_MARKED_AT.get(key)
    if marked_at is None or (
        time.monotonic() - marked_at > BLEConfig.CONNECTION_GATE_UNOWNED_STALE_SECONDS
    ):
        logger.debug(
            "Pruning stale unowned claim for %s (marked_at=%s, threshold=%.1fs).",
            key,
            marked_at,
            BLEConfig.CONNECTION_GATE_UNOWNED_STALE_SECONDS,
        )
        _remove_connected_record_locked(key)
        is_owned = getattr(_REGISTRY_LOCK, "_is_owned", None)
        # _is_owned() is a CPython RLock implementation detail; guard usage so
        # non-CPython runtimes simply skip this diagnostic assertion.
        if callable(is_owned):
            if not is_owned():
                raise RuntimeError(
                    "_prune_stale_unowned_claim_locked() called without owning _REGISTRY_LOCK"
                )  # Nested lock acquisition below relies on RLock ownership.
        # Intentional nested acquisition: _cleanup_addr_lock() also takes
        # _REGISTRY_LOCK, and _REGISTRY_LOCK is a reentrant RLock.
        _cleanup_addr_lock(key)
        return True
    return False


def _prune_stale_connecting_claim_locked(key: str) -> bool:
    """Prune stale provisional connecting-state for a normalized key.

    Parameters
    ----------
    key : str
        Normalized address key to evaluate for stale provisional ownership.

    Returns
    -------
    bool
        `True` when a stale provisional record was removed, otherwise `False`.
    """
    marked_at = _CONNECTING_MARKED_AT.get(key)
    if marked_at is None or (
        time.monotonic() - marked_at > BLEConfig.CONNECTION_GATE_UNOWNED_STALE_SECONDS
    ):
        logger.debug(
            "Pruning stale provisional claim for %s (marked_at=%s, threshold=%.1fs).",
            key,
            marked_at,
            BLEConfig.CONNECTION_GATE_UNOWNED_STALE_SECONDS,
        )
        _remove_connecting_record_locked(key)
        _maybe_remove_addr_lock_entry(key)
        return True
    return False


def _is_connecting_claim_elsewhere_locked(
    key: str, owner: object | None
) -> bool | None:
    """Return whether a provisional claim currently blocks `owner` for `key`.

    Must be called while holding `_REGISTRY_LOCK`.

    Parameters
    ----------
    key : str
        Normalized address key to evaluate.
    owner : object | None
        Optional owner object requesting the check.

    Returns
    -------
    bool | None
        `True` when a different owner currently holds a provisional claim,
        `False` when the key has a provisional record but should not block this
        owner, and `None` when no provisional claim exists for `key`.
    """
    if key not in _CONNECTING_ADDRS:
        return None
    owner_ref = _CONNECTING_OWNERS.get(key)
    current_owner = owner_ref() if owner_ref is not None else None
    current_owner_id = _CONNECTING_OWNER_IDS.get(key)
    if owner_ref is not None and current_owner is None:
        _remove_connecting_record_locked(key)
        _cleanup_addr_lock(key)
        return False
    if _prune_stale_connecting_claim_locked(key):
        return False
    if owner is not None and (
        current_owner is owner
        or (
            owner_ref is None
            and current_owner_id is not None
            and current_owner_id == id(owner)
        )
    ):
        return False
    return True


def _mark_connecting(addr: str | None, owner: object | None = None) -> None:
    """Record a provisional per-address ownership claim for an in-flight connect.

    Parameters
    ----------
    addr : str | None
        Address whose provisional connecting claim should be recorded.
    owner : object | None
        Optional owner identity associated with the provisional claim.

    Returns
    -------
    None

    Notes
    -----
    Stores a concrete per-address lock and updates provisional ownership maps
    (`_ADDR_LOCKS`, `_CONNECTING_ADDRS`, `_CONNECTING_OWNERS`,
    `_CONNECTING_OWNER_IDS`, `_CONNECTING_MARKED_AT`).
    """
    key = _addr_key(addr)
    if key is None:
        return
    with _REGISTRY_LOCK:
        # Keep a concrete per-address lock entry while a provisional claim is
        # active so cleanup cannot drop/recreate the lock mid-connect.
        _ADDR_LOCKS.setdefault(key, RLock())
        _CONNECTING_ADDRS.add(key)
        _CONNECTING_OWNERS[key] = _owner_ref(owner)
        _CONNECTING_OWNER_IDS[key] = id(owner) if owner is not None else None
        _CONNECTING_MARKED_AT[key] = time.monotonic()


def _clear_connecting(addr: str | None, owner: object | None = None) -> None:
    """Clear a provisional connecting claim, optionally requiring owner match.

    Parameters
    ----------
    addr : str | None
        Address whose provisional connecting claim should be cleared.
    owner : object | None
        Optional owner identity to require before removing the claim.

    Returns
    -------
    None

    Notes
    -----
    When `owner` is provided, clearing occurs only if the stored owner matches.
    """
    key = _addr_key(addr)
    if key is None:
        return
    with _REGISTRY_LOCK:
        if owner is not None:
            owner_ref = _CONNECTING_OWNERS.get(key)
            current_owner = owner_ref() if owner_ref is not None else None
            # Dead weakrefs must be pruned before any id(owner) fallback checks.
            if owner_ref is not None and current_owner is None:
                _remove_connecting_record_locked(key)
                _cleanup_addr_lock(key)
                return
            if current_owner is not None and current_owner is not owner:
                return
            if current_owner is None:
                stored_id = _CONNECTING_OWNER_IDS.get(key)
                if stored_id is None or stored_id != id(owner):
                    return
        _remove_connecting_record_locked(key)
        _cleanup_addr_lock(key)


def _clear_connecting_for_owner(owner: object | None) -> None:
    """Clear all provisional connecting claims currently attributed to ``owner``.

    Parameters
    ----------
    owner : object | None
        Owner instance whose provisional claims should be removed.

    Notes
    -----
    This is a best-effort cleanup helper for shutdown paths where the caller
    may not know every provisional key that was claimed during connect.
    """
    if owner is None:
        return
    owner_id = id(owner)
    with _REGISTRY_LOCK:
        for key in tuple(_CONNECTING_ADDRS):
            owner_ref = _CONNECTING_OWNERS.get(key)
            current_owner = owner_ref() if owner_ref is not None else None
            if owner_ref is not None and current_owner is None:
                _remove_connecting_record_locked(key)
                _maybe_remove_addr_lock_entry(key)
                continue
            stored_owner_id = _CONNECTING_OWNER_IDS.get(key)
            if current_owner is owner or (
                owner_ref is None
                and stored_owner_id is not None
                and stored_owner_id == owner_id
            ):
                _remove_connecting_record_locked(key)
                _maybe_remove_addr_lock_entry(key)


def _mark_connected(addr: str | None, owner: Any | None = None) -> None:
    """Mark a BLE address as connected and record optional owner metadata.

    If `addr` is None, empty, or whitespace-only this function does nothing. For a valid
    address it normalizes the address to an internal key and, while holding the registry
    lock, adds the key to the connected set, stores a weak reference to `owner` when
    possible, records `id(owner)` (or `None`), and records the monotonic timestamp when
    the connection was marked.

    Parameters
    ----------
    addr : str | None
        The BLE address to mark connected; may be None or empty (no-op).
    owner : Any | None
        Optional owner object associated with the connection; a weak
        reference is stored when the object supports weak references and the owner's
        `id` is recorded. (Default value = None)
    """
    key = _addr_key(addr)
    if key is None:
        return
    with _REGISTRY_LOCK:
        _remove_connecting_record_locked(key)
        _CONNECTED_ADDRS.add(key)
        _CONNECTED_OWNERS[key] = _owner_ref(owner)
        _CONNECTED_OWNER_IDS[key] = id(owner) if owner is not None else None
        _CONNECTED_MARKED_AT[key] = time.monotonic()


def _mark_disconnected(addr: str | None, owner: Any | None = None) -> None:
    """Mark an address as disconnected and clear its connection bookkeeping.

    This normalizes `addr` and is a no-op for `None` or empty addresses. If `owner` is provided, the disconnect is
    applied only when the recorded live owner matches `owner` (by identity or, when weak references are unavailable, by
    a stored owner id); otherwise the call is ignored. When applied, the function removes connected-state records for
    the address and triggers per-address lock cleanup. It does not decrement per-address lock holder counts — callers
    remain responsible for holder bookkeeping.

    Parameters
    ----------
    addr : str | None
        Address to mark disconnected; will be normalized internally.
    owner : Any | None
        Optional owner object that must match the recorded owner for the disconnect to proceed. (Default value = None)
    """
    key = _addr_key(addr)
    if key is None:
        return
    with _REGISTRY_LOCK:
        has_connected_claim = (
            key in _CONNECTED_ADDRS
            or key in _CONNECTED_OWNERS
            or key in _CONNECTED_OWNER_IDS
        )
        has_connecting_claim = (
            key in _CONNECTING_ADDRS
            or key in _CONNECTING_OWNERS
            or key in _CONNECTING_OWNER_IDS
        )

        if owner is not None and has_connected_claim:
            owner_ref = _CONNECTED_OWNERS.get(key)
            current_owner = owner_ref() if owner_ref is not None else None
            pruned_dead_connected_owner = False
            if owner_ref is not None and current_owner is None:
                _remove_connected_record_locked(key)
                pruned_dead_connected_owner = True
            if (
                not pruned_dead_connected_owner
                and current_owner is not None
                and current_owner is not owner
            ):
                logger.debug(
                    "Ignoring disconnect mark for %s from non-owner instance.",
                    key,
                )
                return
            # Also check owner ID when weakref is unavailable (non-weakrefable objects)
            if not pruned_dead_connected_owner and current_owner is None:
                stored_id = _CONNECTED_OWNER_IDS.get(key)
                # CPython can reuse ids after GC; this _mark_disconnected fallback is
                # best-effort when _CONNECTED_OWNER_IDS must compare id(owner).
                # The race window is narrow because callers still hold owner live.
                if stored_id is None or stored_id != id(owner):
                    logger.debug(
                        "Ignoring disconnect mark for %s from non-owner instance.",
                        key,
                    )
                    return
        clear_connecting_claim = True
        if owner is not None and has_connecting_claim:
            provisional_owner_ref = _CONNECTING_OWNERS.get(key)
            provisional_owner = (
                provisional_owner_ref() if provisional_owner_ref is not None else None
            )
            # Prune dead provisional weakrefs before id(owner) fallback checks.
            pruned_dead_provisional_owner = False
            if provisional_owner_ref is not None and provisional_owner is None:
                _CONNECTING_OWNERS.pop(key, None)
                _CONNECTING_OWNER_IDS.pop(key, None)
                pruned_dead_provisional_owner = True
            if not pruned_dead_provisional_owner:
                if provisional_owner is not None and provisional_owner is not owner:
                    logger.debug(
                        "Ignoring provisional disconnect mark for %s from non-owner instance.",
                        key,
                    )
                    clear_connecting_claim = False
                if provisional_owner is None:
                    provisional_owner_id = _CONNECTING_OWNER_IDS.get(key)
                    if provisional_owner_id is None or provisional_owner_id != id(
                        owner
                    ):
                        logger.debug(
                            "Ignoring provisional disconnect mark for %s from non-owner instance.",
                            key,
                        )
                        clear_connecting_claim = False
        _remove_connected_record_locked(key)
        if clear_connecting_claim:
            _remove_connecting_record_locked(key)
        _cleanup_addr_lock(key)


@contextmanager
def _addr_lock_context(addr: str | None) -> Generator[RLock, None, None]:
    """Provide a per-address reentrant lock handle while managing the lock's internal holder count.

    Parameters
    ----------
    addr : str | None
        BLE address or already-normalized address key. If None, the global registry lock is selected.

    Yields
    ------
    RLock
        The per-address reentrant lock for the normalized address key. The lock is yielded but not acquired by this context manager.
    """
    key = _addr_key(addr)
    lock = _get_addr_lock_by_key(key)
    try:
        # NOTE: We intentionally yield WITHOUT acquiring the lock.
        # The caller must explicitly acquire it with `with lock:` to ensure
        # proper lock ordering discipline is visible in the code.
        yield lock
    finally:
        _release_addr_lock_by_key(key)


# Early exits keep registry-lock cleanup and TOCTOU revalidation branches explicit.
def _is_currently_connected_elsewhere(  # pylint: disable=too-many-return-statements
    addr: str | None, owner: Any | None = None
) -> bool:
    """Return whether a BLE address is recorded as connected by a different owner.

    Normalizes the provided address via _addr_key; if normalization yields None the function returns False. While
    checking, stale records are pruned when the recorded owner has been garbage-collected, reports disconnected, or an
    unowned claim has expired.

    Parameters
    ----------
    addr : str | None
        BLE address to check; will be normalized via _addr_key.
    owner : Any | None
        Caller's owner instance; if it matches the recorded owner by identity or recorded id, the address is not
        considered connected elsewhere. (Default value = None)

    Returns
    -------
    bool
        True if the normalized address is marked connected by a different owner, False otherwise.
    """
    key = _addr_key(addr)
    if key is None:
        return False

    owner_to_check: Any | None = None
    # Intentional two-phase TOCTOU trade-off:
    # we must not hold _REGISTRY_LOCK while calling into owner state because
    # owner callbacks can touch interface locks. We therefore sample owner
    # state outside the registry lock and then re-check ownership/claim under
    # _REGISTRY_LOCK before applying that sample.
    # Phase 1: Check under registry lock.
    with _REGISTRY_LOCK:
        if key not in _CONNECTED_ADDRS:
            provisional_elsewhere = _is_connecting_claim_elsewhere_locked(key, owner)
            return False if provisional_elsewhere is None else provisional_elsewhere

        owner_ref = _CONNECTED_OWNERS.get(key)
        current_owner = owner_ref() if owner_ref is not None else None
        current_owner_id = _CONNECTED_OWNER_IDS.get(key)

        # Prune stale claims when owner object was garbage collected.
        if owner_ref is not None and current_owner is None:
            _remove_connected_record_locked(key)
            _cleanup_addr_lock(key)
            return False

        # Same owner is not "connected elsewhere".
        # Only use id() fallback when no weakref owner entry exists, so dead
        # weakrefs are pruned before CPython id() reuse can match stale ids.
        if owner is not None and (
            current_owner is owner
            or (
                owner_ref is None
                and current_owner_id is not None
                and current_owner_id == id(owner)
            )
        ):
            return False

        if current_owner is not None:
            # Evaluate owner connection state outside _REGISTRY_LOCK to avoid
            # crossing into interface state locks while holding the registry lock.
            owner_to_check = current_owner
        else:
            # Fallback for unowned claims: prune after a safety window.
            if _prune_stale_unowned_claim_locked(key):
                return False

            return True

    # owner_to_check is guaranteed non-None here: all current_owner-is-None
    # paths return from inside the first _REGISTRY_LOCK block above.
    connection_state = _owner_connected_state(owner_to_check)

    # Phase 2: Verify owner connection state; re-check under registry lock to close the TOCTOU window.
    with _REGISTRY_LOCK:
        if key not in _CONNECTED_ADDRS:
            provisional_elsewhere = _is_connecting_claim_elsewhere_locked(key, owner)
            return False if provisional_elsewhere is None else provisional_elsewhere

        owner_ref = _CONNECTED_OWNERS.get(key)
        current_owner = owner_ref() if owner_ref is not None else None
        current_owner_id = _CONNECTED_OWNER_IDS.get(key)

        if owner_ref is not None and current_owner is None:
            _remove_connected_record_locked(key)
            _cleanup_addr_lock(key)
            return False

        if owner is not None and (
            current_owner is owner
            or (
                owner_ref is None
                and current_owner_id is not None
                and current_owner_id == id(owner)
            )
        ):
            return False

        # Only apply the sampled owner state if the claim still belongs to the
        # same owner object we checked outside _REGISTRY_LOCK.
        if current_owner is owner_to_check and connection_state is False:
            _remove_connected_record_locked(key)
            _cleanup_addr_lock(key)
            return False

        if current_owner is not None:
            return True

        if _prune_stale_unowned_claim_locked(key):
            return False

        return True
