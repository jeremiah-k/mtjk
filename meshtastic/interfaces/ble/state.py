"""BLE connection state management.

Lock Ordering Note:
    - Never hold _REGISTRY_LOCK while waiting to acquire per-address locks.
    - _REGISTRY_LOCK is reserved for short registry updates in gating.py.
    - Interface-level lock order is:
      1. Per-address locks (_ADDR_LOCKS in gating.py, via _addr_lock_context)
      2. Interface connect lock (_connect_lock)
      3. Interface disconnect lock (_disconnect_lock) for non-blocking pre-check
      4. Interface state lock (_state_lock)

    Note: The disconnect path acquires _disconnect_lock in non-blocking mode
    first for early-return/concurrent-callback handling, then acquires
    _state_lock for transition logic.
"""

from enum import Enum
from threading import RLock
from typing import ClassVar, TypeGuard, cast

from meshtastic.interfaces.ble.constants import logger


class ConnectionState(Enum):
    """Enum for managing BLE connection states."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTING = "disconnecting"
    # Reserved reconnect lifecycle state for compatibility/observability.
    RECONNECTING = "reconnecting"
    ERROR = "error"


class BLEStateManager:
    """Thread-safe state management for BLE connections.

    Replaces multiple locks and boolean flags with a single state machine
    for clearer connection management and reduced complexity.
    """

    _VALID_TRANSITIONS: ClassVar[dict[ConnectionState, frozenset[ConnectionState]]] = {
        ConnectionState.DISCONNECTED: frozenset(
            {
                ConnectionState.CONNECTING,
                ConnectionState.ERROR,
                # Note: DISCONNECTING is intentionally NOT allowed from DISCONNECTED.
                # A disconnected interface cannot "begin disconnecting" - it's already disconnected.
                # close() explicitly checks for DISCONNECTED state and skips the transition.
            }
        ),
        ConnectionState.CONNECTING: frozenset(
            {
                ConnectionState.CONNECTED,
                ConnectionState.DISCONNECTING,
                ConnectionState.ERROR,
                ConnectionState.DISCONNECTED,
            }
        ),
        ConnectionState.CONNECTED: frozenset(
            {
                ConnectionState.DISCONNECTING,
                ConnectionState.RECONNECTING,
                ConnectionState.DISCONNECTED,
                ConnectionState.ERROR,
            }
        ),
        ConnectionState.DISCONNECTING: frozenset(
            {
                ConnectionState.DISCONNECTED,
                ConnectionState.ERROR,
            }
        ),
        ConnectionState.RECONNECTING: frozenset(
            {
                ConnectionState.CONNECTED,
                ConnectionState.DISCONNECTING,
                ConnectionState.CONNECTING,
                ConnectionState.ERROR,
                ConnectionState.DISCONNECTED,
            }
        ),
        ConnectionState.ERROR: frozenset(
            {
                ConnectionState.DISCONNECTED,
                ConnectionState.DISCONNECTING,
                ConnectionState.CONNECTING,
                ConnectionState.RECONNECTING,
            }
        ),
    }

    def __init__(self) -> None:
        """Initialize the BLEStateManager by creating its reentrant lock and setting the initial connection state.

        Creates an RLock for serializing state transitions and sets the internal state to ConnectionState.DISCONNECTED.

        Returns
        -------
        None
        """
        self._state_lock = RLock()  # Single reentrant lock for all state changes
        self._state = ConnectionState.DISCONNECTED

    @property
    def lock(self) -> RLock:
        """Access the shared RLock that serializes BLE state transitions.

        Returns
        -------
        RLock
            The reentrant lock used to protect state changes.
        """
        return self._state_lock

    # COMPAT_STABLE_SHIM: alias for lock property
    @property
    def _lock(self) -> RLock:
        """Backward-compatible alias for :attr:`lock`."""
        return self.lock

    @property
    def _current_state(self) -> ConnectionState:
        """Get the current BLE connection state.

        This property reads the manager's state while holding the internal reentrant lock to provide a consistent, thread-safe view.

        Returns
        -------
        ConnectionState
            The current connection state.
        """
        with self._state_lock:
            return self._state

    def _current_state_unlocked(self) -> ConnectionState:
        """Return current state when the caller already owns ``lock``.

        This internal primitive exists for lifecycle code that must update
        connection state and session ownership atomically under the one shared
        state-manager/session lock. Callers must already hold :attr:`lock`.

        Returns
        -------
        ConnectionState
            Current connection state without acquiring the lock again.
        """
        return self._state

    @property
    # COMPAT_STABLE_SHIM: public-first alias for `_current_state`.
    def current_state(self) -> ConnectionState:
        """Public-first state snapshot accessor.

        Returns
        -------
        ConnectionState
            Current connection state.
        """
        return self._current_state

    @property
    def _is_connected(self) -> bool:
        """Whether the BLE interface is currently in the CONNECTED state.

        This is a point-in-time snapshot and is not an atomic check+act
        primitive. Callers that need atomic transitions must hold
        `_state_lock` or use `_transition_if_in(...)`.

        Returns
        -------
        bool
            True if the current connection state is ConnectionState.CONNECTED, False otherwise.
        """
        return self._current_state == ConnectionState.CONNECTED

    @property
    # COMPAT_STABLE_SHIM: public-first alias for `_is_connected`.
    def is_connected(self) -> bool:
        """Public-first connected-state snapshot accessor.

        Returns
        -------
        bool
            ``True`` when the current state is ``ConnectionState.CONNECTED``.
        """
        return self._is_connected

    @property
    def _is_closing(self) -> bool:
        """Internal property: Indicates whether the BLE interface is in the DISCONNECTING state.

        This is a point-in-time snapshot and is not an atomic check+act
        primitive. Callers that need atomic transitions must hold
        `_state_lock` or use `_transition_if_in(...)`.

        Returns
        -------
        bool
            True if the current connection state is DISCONNECTING, False otherwise.
        """
        return self._current_state == ConnectionState.DISCONNECTING

    @property
    # COMPAT_STABLE_SHIM: public-first alias for `_is_closing`.
    def is_closing(self) -> bool:
        """Public-first closing-state snapshot accessor.

        Returns
        -------
        bool
            ``True`` when the current state is ``ConnectionState.DISCONNECTING``.
        """
        return self._is_closing

    @property
    def _can_connect(self) -> bool:
        """Whether a new BLE connection may be initiated.

        This is a point-in-time snapshot and is not an atomic check+act
        primitive. Callers that need atomic transitions must hold
        `_state_lock` or use `_transition_if_in(...)`.

        Returns
        -------
        bool
            True if the current state is DISCONNECTED or ERROR, False otherwise.
        """
        return self._current_state in (
            ConnectionState.DISCONNECTED,
            ConnectionState.ERROR,
        )

    @property
    # COMPAT_STABLE_SHIM: public-first alias for `_can_connect`.
    def can_connect(self) -> bool:
        """Public-first connect-capability snapshot accessor.

        Returns
        -------
        bool
            ``True`` when the current state allows initiating a connection.
        """
        return self._can_connect

    @property
    def _is_connecting(self) -> bool:
        """Whether the BLE interface is in a connecting state.

        This is a point-in-time snapshot and is not an atomic check+act
        primitive. Callers that need atomic transitions must hold
        `_state_lock` or use `_transition_if_in(...)`.

        Returns
        -------
        bool
            True if the current state is CONNECTING or RECONNECTING, False otherwise.
        """
        return self._current_state in (
            ConnectionState.CONNECTING,
            ConnectionState.RECONNECTING,
        )

    # COMPAT_STABLE_SHIM: public alias for `_is_connecting`.
    @property
    def is_connecting(self) -> bool:
        """Public-first compatibility alias for ``_is_connecting``.

        Returns
        -------
        bool
            ``True`` when the manager is in CONNECTING or RECONNECTING.
        """
        return self._is_connecting

    @property
    def _is_active(self) -> bool:
        """Return whether the BLE interface currently has an active or pending connection.

        This is a point-in-time snapshot and is not an atomic check+act
        primitive. Callers that need atomic transitions must hold
        `_state_lock` or use `_transition_if_in(...)`.

        Returns
        -------
        bool
            `True` if the current state is CONNECTING, RECONNECTING, or CONNECTED, `False` otherwise.
        """
        return self._current_state in (
            ConnectionState.CONNECTING,
            ConnectionState.RECONNECTING,
            ConnectionState.CONNECTED,
        )

    @property
    # COMPAT_STABLE_SHIM: public-first alias for `_is_active`.
    def is_active(self) -> bool:
        """Public-first active-state snapshot accessor.

        Returns
        -------
        bool
            ``True`` when the connection state is active or currently in-flight.
        """
        return self._is_active

    def _transition_to_unlocked(self, new_state: ConnectionState) -> bool:
        """Transition helper that assumes `_state_lock` is already held."""
        if new_state == self._state:
            # No-op transition: already in target state, this is valid
            logger.debug("State transition no-op: already in %s", self._state.value)
            return True
        if self._is_valid_transition(self._state, new_state):
            old_state = self._state
            self._state = new_state
            logger.debug("State transition: %s → %s", old_state.value, new_state.value)
            return True
        logger.warning(
            "Invalid state transition: %s → %s",
            self._state.value,
            new_state.value,
        )
        return False

    def _transition_to(self, new_state: ConnectionState) -> bool:
        """Attempt to change the manager's BLE connection state to the given target state.

        Validates the requested transition against the state machine and treats a transition to the current state as a valid no-op.

        Parameters
        ----------
        new_state : ConnectionState
            Target connection state to transition to.

        Returns
        -------
        bool
            `True` if the state was changed or already equals the target state, `False` if the transition is invalid.
        """
        with self._state_lock:
            return self._transition_to_unlocked(new_state)

    # COMPAT_STABLE_SHIM: public-first alias for `_transition_to`.
    def transition_to(self, new_state: ConnectionState) -> bool:
        """Public-first wrapper for state transitions.

        Parameters
        ----------
        new_state : ConnectionState
            Target state to transition to.

        Returns
        -------
        bool
            ``True`` when the transition succeeds (or is a valid no-op).
        """
        return self._transition_to(new_state)

    def _is_valid_transition(
        self, from_state: ConnectionState, to_state: ConnectionState
    ) -> bool:
        """Return whether transitioning from one ConnectionState to another is permitted by the state machine.

        Parameters
        ----------
        from_state : ConnectionState
            Current connection state.
        to_state : ConnectionState
            Proposed next connection state.

        Returns
        -------
        bool
            True if the transition from `from_state` to `to_state` is allowed, False otherwise.
        """
        return to_state in self._VALID_TRANSITIONS.get(from_state, frozenset())

    def _transition_if_in(
        self, expected_states: set[ConnectionState], new_state: ConnectionState
    ) -> bool:
        """Atomically transition to `new_state` only if the current state is one of `expected_states`.

        Performs the check and potential transition while holding the internal state lock to avoid time-of-check-to-time-of-use races.

        Parameters
        ----------
        expected_states : set[ConnectionState]
            Allowed current states for the transition.
        new_state : ConnectionState
            Target state to transition to.

        Returns
        -------
        bool
            `True` if the current state was in `expected_states` and the transition to `new_state` succeeded, `False` otherwise.
        """
        with self._state_lock:
            if self._state not in expected_states:
                logger.debug(
                    "State check failed: expected one of %s, got %s",
                    sorted(s.value for s in expected_states),
                    self._state.value,
                )
                return False
            return self._transition_to_unlocked(new_state)

    def _reset_to_disconnected(self) -> bool:
        """Force the connection state to DISCONNECTED for recovery or cleanup.

        Acquires the internal state lock and attempts a transition to DISCONNECTED; this will be a no-op if the state is already DISCONNECTED.

        Returns
        -------
        bool
            `True` if the transition succeeded or was a no-op (state is DISCONNECTED after the call), `False` otherwise.
        """
        with self._state_lock:
            return self._reset_to_disconnected_unlocked()

    def _reset_to_disconnected_unlocked(self) -> bool:
        """Reset to DISCONNECTED when the caller already owns ``lock``.

        Returns
        -------
        bool
            ``True`` when DISCONNECTED is reached, including a valid no-op.
        """
        # Prefer validated transition semantics; this is a no-op when already
        # disconnected and remains resilient to future transition-map edits.
        return self._transition_to_unlocked(ConnectionState.DISCONNECTED)

    # COMPAT_STABLE_SHIM: public-first alias for `_reset_to_disconnected`.
    def reset_to_disconnected(self) -> bool:
        """Public-first wrapper that forces DISCONNECTED state.

        Returns
        -------
        bool
            ``True`` when DISCONNECTED is reached (including no-op).
        """
        return self._reset_to_disconnected()


def _is_canonical_state_manager(
    candidate: object,
    lock: object,
) -> TypeGuard[BLEStateManager]:
    """Return whether ``candidate`` is the exact state owner for ``lock``.

    Compatibility subclasses and test doubles intentionally take slower probe
    paths; only the concrete manager may use lock-coupled unlocked access.
    """
    if type(candidate) is not BLEStateManager:  # pylint: disable=unidiomatic-typecheck
        return False
    return candidate.lock is lock
