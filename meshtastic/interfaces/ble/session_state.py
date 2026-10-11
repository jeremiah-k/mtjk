"""Owned mutable lifecycle state for a BLE interface session."""

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from meshtastic._core_constants import LAST_DISCONNECT_SOURCE_TYPE_ERROR
from meshtastic.interfaces.ble.compat_adapter import (
    _get_declared_callable,
    _get_declared_lock,
    _get_declared_member,
)
from meshtastic.interfaces.ble.constants import logger
from meshtastic.interfaces.ble.coordination import ThreadLike
from meshtastic.interfaces.ble.ports import _BLESessionStatePort, _LockPort

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.client import BLEClient


_MISSING_SESSION_FIELD = object()
LEGACY_SESSION_STATE_CACHE_ERROR = (
    "Legacy BLE lifecycle collaborators without an instance __dict__ must pass "
    "session_state explicitly."
)


@dataclass(slots=True)
class BLESessionState:  # pylint: disable=too-many-instance-attributes
    """Own mutable BLE lifecycle flags and per-session bookkeeping.

    Synchronization policy
    ----------------------
    Interface-owned construction shares ``lock`` with ``BLEStateManager`` when
    that manager exposes its declared lock. Partial compatibility objects that
    have no state-manager lock receive one stable independent session lock and
    emit a diagnostic recording the degraded cross-owner atomicity guarantee.
    Callers that perform check-and-act sequences across these fields and
    connection-state transitions must hold the shared lock when one exists.
    Simple fields intentionally remain plain attributes so existing lifecycle
    code can migrate incrementally without introducing nested-lock behavior.
    """

    lock: _LockPort
    closed: bool = False
    disconnect_notified: bool = False
    client_publish_pending: bool = False
    connected_publish_inflight_client: "BLEClient | None" = None
    client_replacement_pending: bool = False
    last_disconnect_source: str | None = ""
    connection_alias_key: str | None = None
    prior_publish_was_reconnect: bool = False
    last_connect_pair_override: bool | None = None
    last_connect_timeout_override: float | None = None
    publishing_thread_override: object | None = None
    ever_connected: bool = False
    connection_session_epoch: int = 0
    receive_recovery_attempts: int = 0
    last_recovery_time: float = 0.0
    read_retry_count: int = 0
    last_empty_read_warning: float = 0.0
    suppressed_empty_read_warnings: int = 0
    want_receive: bool = True
    receive_start_pending: bool = False
    receive_start_pending_since: float | None = None
    receive_thread: ThreadLike | None = None

    def _reset_read_retry_count(self) -> None:
        """Reset only the transient read retry counter under the session lock."""
        with self.lock:
            self.read_retry_count = 0

    def _reset_receive_retry_state(self) -> None:
        """Reset transient read retry and warning counters under the session lock."""
        with self.lock:
            self.read_retry_count = 0
            self.last_empty_read_warning = 0.0
            self.suppressed_empty_read_warnings = 0

    def _reset_recovery_state(self) -> None:
        """Reset receive-recovery attempt bookkeeping under the session lock."""
        with self.lock:
            self.receive_recovery_attempts = 0
            self.last_recovery_time = 0.0


class _BLESessionStateCompatMixin:
    """Preserve historical private lifecycle attributes over owned session state."""

    _state_manager: Any
    _session_state: _BLESessionStatePort

    def _promote_legacy_session_state(
        self, state: "_LegacyBLESessionStateAdapter"
    ) -> BLESessionState:
        """Promote a cached legacy adapter to one canonical owned state.

        Parameters
        ----------
        state : _LegacyBLESessionStateAdapter
            Cached compatibility adapter currently occupying the owner slot.

        Returns
        -------
        BLESessionState
            Canonical session owner installed for this interface.
        """
        instance_dict = self.__dict__
        promotion_lock = state.lock
        with promotion_lock:
            current = instance_dict.get("_session_state")
            if isinstance(current, BLESessionState):
                return current
            if current is state:
                promoted = BLESessionState(lock=promotion_lock)
                for field_name, legacy_name in state._FIELD_MAP.items():
                    if field_name == "lock":
                        continue
                    setattr(
                        promoted,
                        field_name,
                        instance_dict.get(
                            legacy_name,
                            state._FIELD_DEFAULTS[field_name],
                        ),
                    )
                instance_dict["_session_state"] = promoted
                for legacy_name in state._FIELD_MAP.values():
                    instance_dict.pop(legacy_name, None)
                return promoted
        return self._get_session_state()

    def _get_session_state(self) -> BLESessionState:
        """Return owned lifecycle state, lazily supporting partial interfaces."""
        instance_dict = self.__dict__
        state = instance_dict.get("_session_state")
        if isinstance(state, BLESessionState):
            return state
        if isinstance(state, _LegacyBLESessionStateAdapter):
            return self._promote_legacy_session_state(state)
        state_manager = self.__dict__.get("_state_manager")
        lock = _get_declared_lock(state_manager, "lock")
        if lock is None:
            logger.debug(
                "No declared BLE state-manager lock; using an independent "
                "session lock. Session-state and connection-state updates "
                "are not mutually atomic."
            )
            lock = cast(_LockPort, threading.RLock())
        candidate = BLESessionState(lock=lock)
        winner = instance_dict.setdefault("_session_state", candidate)
        if isinstance(winner, BLESessionState):
            return winner
        if isinstance(winner, _LegacyBLESessionStateAdapter):
            return self._promote_legacy_session_state(winner)
        # Preserve the historical lazy-initialization behavior for partial test
        # doubles that pre-populate this private slot with an unrelated value,
        # while still converging concurrent callers on one replacement owner.
        if instance_dict.get("_session_state") is winner:
            instance_dict["_session_state"] = candidate
            return candidate
        return self._get_session_state()

    @property
    def _state_lock(self) -> _LockPort:
        """Compatibility view of the owned lifecycle lock."""
        return self._get_session_state().lock

    @_state_lock.setter
    def _state_lock(self, value: _LockPort) -> None:
        self._get_session_state().lock = value

    @property
    def _closed(self) -> bool:
        """Compatibility view of the owned session closed flag."""
        return self._get_session_state().closed

    @_closed.setter
    def _closed(self, value: bool) -> None:
        self._get_session_state().closed = value

    @property
    def _disconnect_notified(self) -> bool:
        """Compatibility view of the disconnect-notified flag."""
        return self._get_session_state().disconnect_notified

    @_disconnect_notified.setter
    def _disconnect_notified(self, value: bool) -> None:
        self._get_session_state().disconnect_notified = value

    @property
    def _client_publish_pending(self) -> bool:
        """Compatibility view of the client-publication pending flag."""
        return self._get_session_state().client_publish_pending

    @_client_publish_pending.setter
    def _client_publish_pending(self, value: bool) -> None:
        self._get_session_state().client_publish_pending = value

    @property
    def _connected_publish_inflight_client(self) -> "BLEClient | None":
        """Compatibility view of the client currently being published."""
        return self._get_session_state().connected_publish_inflight_client

    @_connected_publish_inflight_client.setter
    def _connected_publish_inflight_client(self, value: "BLEClient | None") -> None:
        self._get_session_state().connected_publish_inflight_client = value

    @property
    def _client_replacement_pending(self) -> bool:
        """Compatibility view of the client-replacement pending flag."""
        return self._get_session_state().client_replacement_pending

    @_client_replacement_pending.setter
    def _client_replacement_pending(self, value: bool) -> None:
        self._get_session_state().client_replacement_pending = value

    @property
    def _last_disconnect_source(self) -> str | None:
        """Compatibility view of the latest disconnect source."""
        return self._get_session_state().last_disconnect_source

    @_last_disconnect_source.setter
    def _last_disconnect_source(self, value: str | None) -> None:
        if value is not None and not isinstance(value, str):
            raise TypeError(
                LAST_DISCONNECT_SOURCE_TYPE_ERROR.format(type_name=type(value).__name__)
            )
        self._get_session_state().last_disconnect_source = value

    @property
    def _connection_alias_key(self) -> str | None:
        """Compatibility view of the active connection alias key."""
        return self._get_session_state().connection_alias_key

    @_connection_alias_key.setter
    def _connection_alias_key(self, value: str | None) -> None:
        self._get_session_state().connection_alias_key = value

    @property
    def _prior_publish_was_reconnect(self) -> bool:
        """Compatibility view of whether the prior publication was a reconnect."""
        return self._get_session_state().prior_publish_was_reconnect

    @_prior_publish_was_reconnect.setter
    def _prior_publish_was_reconnect(self, value: bool) -> None:
        self._get_session_state().prior_publish_was_reconnect = value

    @property
    def _last_connect_pair_override(self) -> bool | None:
        """Compatibility view of the last pairing override."""
        return self._get_session_state().last_connect_pair_override

    @_last_connect_pair_override.setter
    def _last_connect_pair_override(self, value: bool | None) -> None:
        self._get_session_state().last_connect_pair_override = value

    @property
    def _last_connect_timeout_override(self) -> float | None:
        """Compatibility view of the last connect-timeout override."""
        return self._get_session_state().last_connect_timeout_override

    @_last_connect_timeout_override.setter
    def _last_connect_timeout_override(self, value: float | None) -> None:
        self._get_session_state().last_connect_timeout_override = value

    @property
    def _publishing_thread_override(self) -> object | None:
        """Compatibility view of the publishing-thread override."""
        return self._get_session_state().publishing_thread_override

    @_publishing_thread_override.setter
    def _publishing_thread_override(self, value: object | None) -> None:
        self._get_session_state().publishing_thread_override = value

    @property
    def _ever_connected(self) -> bool:
        """Compatibility view of whether this session has ever connected."""
        return self._get_session_state().ever_connected

    @_ever_connected.setter
    def _ever_connected(self, value: bool) -> None:
        self._get_session_state().ever_connected = value

    @property
    def _connection_session_epoch(self) -> int:
        """Compatibility view of the connection-session epoch."""
        return self._get_session_state().connection_session_epoch

    @_connection_session_epoch.setter
    def _connection_session_epoch(self, value: int) -> None:
        self._get_session_state().connection_session_epoch = value

    @property
    def _receive_recovery_attempts(self) -> int:
        """Compatibility view of receive-recovery attempts."""
        return self._get_session_state().receive_recovery_attempts

    @_receive_recovery_attempts.setter
    def _receive_recovery_attempts(self, value: int) -> None:
        self._get_session_state().receive_recovery_attempts = value

    @property
    def _last_recovery_time(self) -> float:
        """Compatibility view of the last receive-recovery timestamp."""
        return self._get_session_state().last_recovery_time

    @_last_recovery_time.setter
    def _last_recovery_time(self, value: float) -> None:
        self._get_session_state().last_recovery_time = value

    @property
    def _read_retry_count(self) -> int:
        """Compatibility view of the transient read-retry count."""
        return self._get_session_state().read_retry_count

    @_read_retry_count.setter
    def _read_retry_count(self, value: int) -> None:
        self._get_session_state().read_retry_count = value

    @property
    def _last_empty_read_warning(self) -> float:
        """Compatibility view of the last empty-read warning timestamp."""
        return self._get_session_state().last_empty_read_warning

    @_last_empty_read_warning.setter
    def _last_empty_read_warning(self, value: float) -> None:
        self._get_session_state().last_empty_read_warning = value

    @property
    def _suppressed_empty_read_warnings(self) -> int:
        """Compatibility view of suppressed empty-read warnings."""
        return self._get_session_state().suppressed_empty_read_warnings

    @_suppressed_empty_read_warnings.setter
    def _suppressed_empty_read_warnings(self, value: int) -> None:
        self._get_session_state().suppressed_empty_read_warnings = value

    @property
    def _want_receive(self) -> bool:
        """Compatibility view of receive-loop intent."""
        return self._get_session_state().want_receive

    @_want_receive.setter
    def _want_receive(self, value: bool) -> None:
        self._get_session_state().want_receive = value

    @property
    def _receive_start_pending(self) -> bool:
        """Compatibility view of the receive-start pending flag."""
        return self._get_session_state().receive_start_pending

    @_receive_start_pending.setter
    def _receive_start_pending(self, value: bool) -> None:
        self._get_session_state().receive_start_pending = value

    @property
    def _receive_start_pending_since(self) -> float | None:
        """Compatibility view of when receive-start became pending."""
        return self._get_session_state().receive_start_pending_since

    @_receive_start_pending_since.setter
    def _receive_start_pending_since(self, value: float | None) -> None:
        self._get_session_state().receive_start_pending_since = value

    @property
    def _receiveThread(self) -> ThreadLike | None:  # noqa: N802 - compatibility name
        """Compatibility view of the receive thread."""
        return self._get_session_state().receive_thread

    @_receiveThread.setter
    def _receiveThread(self, value: ThreadLike | None) -> None:  # noqa: N802
        self._get_session_state().receive_thread = value


class _LegacyBLESessionStateAdapter:
    """Adapt historical interface-private fields to the session-state port.

    Partial legacy interfaces are allowed to omit lifecycle fields that historically
    had implicit defaults. The adapter preserves those defaults, follows the
    interface's currently declared ``_state_lock`` when present, and retains one
    stable fallback lock for intervals where no declared lock exists.
    """

    __slots__ = ("_iface", "_fallback_lock")
    _FIELD_MAP = {
        "lock": "_state_lock",
        "closed": "_closed",
        "disconnect_notified": "_disconnect_notified",
        "client_publish_pending": "_client_publish_pending",
        "connected_publish_inflight_client": "_connected_publish_inflight_client",
        "client_replacement_pending": "_client_replacement_pending",
        "last_disconnect_source": "_last_disconnect_source",
        "connection_alias_key": "_connection_alias_key",
        "prior_publish_was_reconnect": "_prior_publish_was_reconnect",
        "last_connect_pair_override": "_last_connect_pair_override",
        "last_connect_timeout_override": "_last_connect_timeout_override",
        "publishing_thread_override": "_publishing_thread_override",
        "ever_connected": "_ever_connected",
        "connection_session_epoch": "_connection_session_epoch",
        "receive_recovery_attempts": "_receive_recovery_attempts",
        "last_recovery_time": "_last_recovery_time",
        "read_retry_count": "_read_retry_count",
        "last_empty_read_warning": "_last_empty_read_warning",
        "suppressed_empty_read_warnings": "_suppressed_empty_read_warnings",
        "want_receive": "_want_receive",
        "receive_start_pending": "_receive_start_pending",
        "receive_start_pending_since": "_receive_start_pending_since",
        "receive_thread": "_receiveThread",
    }
    _FIELD_DEFAULTS: dict[str, object] = {
        "closed": False,
        "disconnect_notified": False,
        "client_publish_pending": False,
        "connected_publish_inflight_client": None,
        "client_replacement_pending": False,
        "last_disconnect_source": "",
        "connection_alias_key": None,
        "prior_publish_was_reconnect": False,
        "last_connect_pair_override": None,
        "last_connect_timeout_override": None,
        "publishing_thread_override": None,
        "ever_connected": False,
        "connection_session_epoch": 0,
        "receive_recovery_attempts": 0,
        "last_recovery_time": 0.0,
        "read_retry_count": 0,
        "last_empty_read_warning": 0.0,
        "suppressed_empty_read_warnings": 0,
        "want_receive": True,
        "receive_start_pending": False,
        "receive_start_pending_since": None,
        "receive_thread": None,
    }

    def __init__(self, iface: object) -> None:
        object.__setattr__(self, "_iface", iface)
        object.__setattr__(self, "_fallback_lock", cast(_LockPort, threading.RLock()))

    def _resolve_lock(self, mapped: str) -> _LockPort:
        """Return the synchronization owner currently governing legacy state."""
        iface = self._iface
        if isinstance(iface, _BLESessionStateCompatMixin):
            instance_dict = iface.__dict__
            current_state = instance_dict.get("_session_state")
            if isinstance(current_state, BLESessionState):
                return current_state.lock
            state_manager_lock = _get_declared_lock(
                instance_dict.get("_state_manager"), "lock"
            )
            if state_manager_lock is not None:
                return state_manager_lock
            raw_legacy_lock = instance_dict.get(mapped)
            if raw_legacy_lock is not None:
                raw_lock_type = type(raw_legacy_lock)
                if (
                    _get_declared_callable(raw_lock_type, "__enter__") is not None
                    and _get_declared_callable(raw_lock_type, "__exit__") is not None
                ):
                    return cast(_LockPort, raw_legacy_lock)
            fallback_lock = self._fallback_lock
            if current_state is self:
                return fallback_lock
        declared_lock = _get_declared_lock(iface, mapped)
        return self._fallback_lock if declared_lock is None else declared_lock

    def __getattr__(self, name: str) -> Any:
        mapped = self._FIELD_MAP.get(name)
        if mapped is None:
            raise AttributeError(
                f"{type(self).__name__} does not proxy session field {name!r}"
            )
        if name == "lock":
            return self._resolve_lock(mapped)
        value = _get_declared_member(self._iface, mapped, _MISSING_SESSION_FIELD)
        if value is not _MISSING_SESSION_FIELD:
            return value
        if name in self._FIELD_DEFAULTS:
            return self._FIELD_DEFAULTS[name]
        raise AttributeError(
            f"{type(self).__name__} has no default for session field {name!r}"
        )

    def __setattr__(self, name: str, value: object) -> None:
        mapped = self._FIELD_MAP.get(name)
        if mapped is None:
            raise AttributeError(
                f"{type(self).__name__} does not proxy session field {name!r}"
            )
        setattr(self._iface, mapped, value)

    def _reset_read_retry_count(self) -> None:
        """Reset only the legacy transient read retry counter."""
        with self.lock:
            self.read_retry_count = 0

    def _reset_receive_retry_state(self) -> None:
        """Reset legacy transient read retry and warning counters."""
        with self.lock:
            self.read_retry_count = 0
            self.last_empty_read_warning = 0.0
            self.suppressed_empty_read_warnings = 0

    def _reset_recovery_state(self) -> None:
        """Reset legacy receive-recovery bookkeeping."""
        with self.lock:
            self.receive_recovery_attempts = 0
            self.last_recovery_time = 0.0


def _session_state_for(
    iface: object, explicit: _BLESessionStatePort | None = None
) -> _BLESessionStatePort:
    """Return explicit/owned session state or a cached legacy interface adapter.

    Caches the legacy adapter on collaborators that expose a real instance
    ``__dict__``. A legacy collaborator without cacheable instance storage must
    provide ``explicit`` session state; otherwise separate coordinators could
    silently create competing owners for the same lifecycle fields.
    """
    if explicit is not None:
        return explicit
    instance_dict = _get_declared_member(iface, "__dict__")
    state = (
        instance_dict.get("_session_state") if isinstance(instance_dict, dict) else None
    )
    if isinstance(state, (BLESessionState, _LegacyBLESessionStateAdapter)):
        return cast(_BLESessionStatePort, state)

    if isinstance(iface, _BLESessionStateCompatMixin):
        return iface._get_session_state()

    if not isinstance(instance_dict, dict):
        raise TypeError(LEGACY_SESSION_STATE_CACHE_ERROR)

    legacy_state = _LegacyBLESessionStateAdapter(iface)
    winner = instance_dict.setdefault("_session_state", legacy_state)
    if isinstance(winner, (BLESessionState, _LegacyBLESessionStateAdapter)):
        return cast(_BLESessionStatePort, winner)
    # Preserve the existing compatibility behavior for an unrelated private
    # slot value while ensuring the normal empty-slot race converges through
    # ``setdefault`` on one adapter owner.
    if instance_dict.get("_session_state") is winner:
        instance_dict["_session_state"] = legacy_state
        return cast(_BLESessionStatePort, legacy_state)
    return _session_state_for(iface)
