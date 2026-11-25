"""BLE connection concurrency and ownership tests."""

# pylint: disable=redefined-outer-name

import contextlib
import threading
from collections.abc import Iterator
from queue import Queue
from types import SimpleNamespace, TracebackType
from typing import Any, Literal, cast

import pytest
from bleak.backends.device import BLEDevice

# Import meshtastic modules for use in tests
from meshtastic.interfaces.ble import (
    BLEClient,
    BLEInterface,
)
from meshtastic.interfaces.ble.constants import (
    CONNECTION_ERROR_LOST_OWNERSHIP,
    ERROR_INTERFACE_CLOSING,
)
from meshtastic.interfaces.ble.state import ConnectionState
from tests._ble_interface_core_support import (
    _build_minimal_connect_test_interface,
    _make_establish_stub,
)

# Import common fixtures
from tests.test_ble_interface_fixtures import DummyClient

pytestmark = pytest.mark.unit


def test_concurrent_connect_and_disconnect_do_not_deadlock(
    monkeypatch: pytest.MonkeyPatch, clear_registry: None
) -> None:
    """Concurrent connect/disconnect should complete without deadlocking under address-lock contention.

    This test forces connect() to hold the per-address lock while _handle_disconnect()
    runs, then releases connect to ensure both operations complete.

    Raises
    ------
    AssertionError
    """
    _ = clear_registry
    import meshtastic.interfaces.ble.interface as ble_iface_mod

    target_address = "AA:BB:CC:DD:EE:01"
    initial_client = DummyClient()
    initial_client.address = target_address
    initial_client.bleak_client = SimpleNamespace(address=target_address)

    connected_client = DummyClient()
    connected_client.address = target_address
    connected_client.bleak_client = SimpleNamespace(address=target_address)

    real_connect = BLEInterface.connect

    def _init_connect_stub(
        iface: BLEInterface,
        _address: str | None = None,
        *,
        connect_timeout: float | None = None,
    ) -> DummyClient:
        """Prepare the given BLEInterface for tests by installing and returning a pre-existing DummyClient and marking the interface as connected.

        Parameters
        ----------
        iface : BLEInterface
            The interface whose client and connection state will be configured.
        _address : str | None
            Ignored; present for compatibility with call sites that pass an address.

        Returns
        -------
        DummyClient
            The dummy client instance that was attached to the interface.
        """
        _ = (_address, connect_timeout)
        with iface._state_lock:
            iface.client = initial_client  # type: ignore[assignment]
            iface._disconnect_notified = False
            iface._state_manager._reset_to_disconnected()
            assert iface._state_manager._transition_to(ConnectionState.CONNECTING)
            assert iface._state_manager._transition_to(ConnectionState.CONNECTED)
        return initial_client

    monkeypatch.setattr(BLEInterface, "connect", _init_connect_stub, raising=True)
    monkeypatch.setattr(
        BLEInterface,
        "_start_receive_thread",
        lambda _self, *, name: None,
        raising=True,
    )
    monkeypatch.setattr(BLEInterface, "_start_config", lambda _self: None, raising=True)

    iface = BLEInterface(address=target_address, noProto=True, auto_reconnect=False)
    monkeypatch.setattr(BLEInterface, "connect", real_connect, raising=True)

    with iface._state_lock:
        iface.client = None
        iface._disconnect_notified = False
        iface._connection_alias_key = None
        iface._state_manager._reset_to_disconnected()

    connect_waiting = threading.Event()
    allow_connect = threading.Event()
    establish_called = threading.Event()
    thread_errors: "Queue[tuple[str, Exception]]" = Queue()

    def _gate_check_stub(_addr_key: str | None, owner: Any | None = None) -> bool:
        """Block test caller until the test releases a connection gate and record that the gate was reached.

        Parameters
        ----------
        _addr_key : str | None
            Address key that must be provided (asserted non-None); used to identify the gated connection.
        owner : Any | None
            Ignored; present to match the gate-check signature. (Default value = None)

        Returns
        -------
        bool
            `False` always.

        Raises
        ------
        AssertionError
            If `_addr_key` is None or if waiting for the test to release the gate times out (12 seconds).
        """
        _ = owner
        assert _addr_key is not None
        connect_waiting.set()
        if not allow_connect.wait(timeout=12.0):
            raise AssertionError("Timed out waiting to release connect gate check")
        return False

    def _establish_connection_stub(*_args: Any, **_kwargs: Any) -> DummyClient:
        """Simulate a successful connection for tests by transitioning the interface state to CONNECTING then CONNECTED.

        Also sets the `establish_called` event to signal completion.

        Parameters
        ----------
        *_args : Any
        **_kwargs : Any

        Returns
        -------
        connected_client : DummyClient
            A DummyClient instance representing the established connection.
        """
        with iface._state_lock:
            assert iface._state_manager._transition_to(ConnectionState.CONNECTING)
            assert iface._state_manager._transition_to(ConnectionState.CONNECTED)
        establish_called.set()
        return connected_client

    monkeypatch.setattr(
        ble_iface_mod,
        "_is_currently_connected_elsewhere",
        _gate_check_stub,
        raising=True,
    )
    monkeypatch.setattr(
        iface._connection_orchestrator,
        "_establish_connection",
        _establish_connection_stub,
        raising=True,
    )
    monkeypatch.setattr(iface, "_register_notifications", lambda _client: None)
    monkeypatch.setattr(iface, "_connected", lambda: None)
    monkeypatch.setattr(iface, "_disconnected", lambda: None)

    def _connect_worker() -> None:
        """Invoke the interface's connect routine for the configured target address and capture any exception raised.

        If an exception occurs, record a tuple ("connect", exc) into the `thread_errors` queue for later inspection by tests.
        """
        try:
            iface.connect(target_address)
        except Exception as exc:  # noqa: BLE001 - test captures thread errors
            thread_errors.put(("connect", exc))

    def _disconnect_worker() -> None:
        """Invoke the interface's disconnect handler in a thread and capture any exception for test inspection.

        Calls iface._handle_disconnect("concurrency-test"). If an exception is raised, places a ("disconnect", exception) tuple into the thread_errors queue so test code can observe thread failures.
        """
        try:
            iface._handle_disconnect("concurrency-test")
        except Exception as exc:  # noqa: BLE001 - test captures thread errors
            thread_errors.put(("disconnect", exc))

    connect_thread = threading.Thread(target=_connect_worker, daemon=True)
    disconnect_thread = threading.Thread(target=_disconnect_worker, daemon=True)
    try:
        connect_thread.start()
        assert connect_waiting.wait(timeout=12.0), "connect() did not reach gate check"

        disconnect_thread.start()
        allow_connect.set()

        connect_thread.join(timeout=12.0)
        disconnect_thread.join(timeout=12.0)

        assert (
            establish_called.is_set()
        ), "connect() did not run connection establishment"
        assert not connect_thread.is_alive(), "connect() thread appears deadlocked"
        assert not disconnect_thread.is_alive(), "disconnect thread appears deadlocked"

        if not thread_errors.empty():
            where, exc = thread_errors.get_nowait()
            pytest.fail(f"{where} thread raised {type(exc).__name__}: {exc}")
    finally:
        allow_connect.set()
        if connect_thread.is_alive():
            connect_thread.join(timeout=1.0)
        if disconnect_thread.is_alive():
            disconnect_thread.join(timeout=1.0)
        iface.close()


def test_ble_interface_init_forwards_constructor_timeout_to_initial_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """__init__() should pass its timeout through to the eager initial connect()."""
    captured_timeouts: list[float | None] = []
    initial_client = DummyClient()
    initial_client.address = "AA:BB:CC:DD:EE:09"
    initial_client.bleak_client = SimpleNamespace(address=initial_client.address)

    def _init_connect_stub(
        iface: BLEInterface,
        _address: str | None = None,
        *,
        connect_timeout: float | None = None,
    ) -> DummyClient:
        _ = _address
        captured_timeouts.append(connect_timeout)
        with iface._state_lock:
            iface.client = initial_client  # type: ignore[assignment]
            iface._disconnect_notified = False
            iface._state_manager._reset_to_disconnected()
            assert iface._state_manager._transition_to(ConnectionState.CONNECTING)
            assert iface._state_manager._transition_to(ConnectionState.CONNECTED)
        return initial_client

    monkeypatch.setattr(BLEInterface, "connect", _init_connect_stub, raising=True)
    monkeypatch.setattr(
        BLEInterface,
        "_start_receive_thread",
        lambda _self, *, name: None,
        raising=True,
    )
    monkeypatch.setattr(BLEInterface, "_start_config", lambda _self: None, raising=True)

    iface = BLEInterface(
        address=initial_client.address,
        noProto=True,
        auto_reconnect=False,
        timeout=17.5,
    )

    assert captured_timeouts == [17.5]
    iface.close()


def test_connect_finalizes_gates_after_address_lock_scope(
    monkeypatch: pytest.MonkeyPatch,
    clear_registry: None,
) -> None:
    """connect() should finalize address gates only after per-address lock scope exits."""
    _ = clear_registry
    import meshtastic.interfaces.ble.interface as ble_iface_mod

    target_address = "AA:BB:CC:DD:EE:02"
    real_connect = BLEInterface.connect

    def _init_connect_stub(
        iface: BLEInterface,
        _address: str | None = None,
        *,
        connect_timeout: float | None = None,
    ) -> DummyClient:
        _ = (_address, connect_timeout)
        initial_client = DummyClient()
        initial_client.address = target_address
        initial_client.bleak_client = SimpleNamespace(address=target_address)
        with iface._state_lock:
            iface.client = initial_client  # type: ignore[assignment]
            iface._disconnect_notified = False
            iface._state_manager._reset_to_disconnected()
            assert iface._state_manager._transition_to(ConnectionState.CONNECTING)
            assert iface._state_manager._transition_to(ConnectionState.CONNECTED)
        return initial_client

    monkeypatch.setattr(BLEInterface, "connect", _init_connect_stub, raising=True)
    monkeypatch.setattr(
        BLEInterface,
        "_start_receive_thread",
        lambda _self, *, name: None,
        raising=True,
    )
    monkeypatch.setattr(BLEInterface, "_start_config", lambda _self: None, raising=True)

    iface = BLEInterface(address=target_address, noProto=True, auto_reconnect=False)
    monkeypatch.setattr(BLEInterface, "connect", real_connect, raising=True)

    with iface._state_lock:
        iface.client = None
        iface._disconnect_notified = False
        iface._connection_alias_key = None
        iface._state_manager._reset_to_disconnected()

    address_lock_held = False

    class _FakeAddressLock:
        def __enter__(self) -> "_FakeAddressLock":
            nonlocal address_lock_held
            address_lock_held = True
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> Literal[False]:
            _ = (exc_type, exc, tb)
            nonlocal address_lock_held
            address_lock_held = False
            return False

    @contextlib.contextmanager
    def _fake_addr_lock_context(_addr: str | None) -> Iterator[_FakeAddressLock]:
        with _FakeAddressLock() as lock:
            yield lock

    connected_client = DummyClient()
    connected_client.address = target_address
    connected_client.bleak_client = SimpleNamespace(address=target_address)

    finalized_lock_states: list[bool] = []

    def _finalize_stub(
        _client: BLEClient, _device_key: str | None, _alias_key: str | None
    ) -> None:
        finalized_lock_states.append(address_lock_held)

    monkeypatch.setattr(
        ble_iface_mod, "_addr_lock_context", _fake_addr_lock_context, raising=True
    )
    monkeypatch.setattr(
        iface,
        "_raise_if_duplicate_connect",
        lambda _connection_key, **_kwargs: None,
        raising=True,
    )
    monkeypatch.setattr(
        iface, "_get_existing_client_if_valid", lambda _request: None, raising=True
    )

    _establish_stub = _make_establish_stub(
        iface,
        lambda: connected_client,
        connected_address=target_address,
        device_key="device-key",
    )

    monkeypatch.setattr(
        iface,
        "_establish_and_update_client",
        _establish_stub,
        raising=True,
    )
    monkeypatch.setattr(
        iface, "_finalize_connection_gates", _finalize_stub, raising=True
    )

    result = iface.connect(target_address)

    assert cast(object, result) is connected_client
    assert finalized_lock_states == [False]
    iface.close()


def test_connect_marks_provisional_claims_before_gate_release(
    monkeypatch: pytest.MonkeyPatch,
    clear_registry: None,
) -> None:
    """connect() should publish provisional ownership before releasing the address gate."""
    _ = clear_registry
    from meshtastic.interfaces.ble.gating import _is_currently_connected_elsewhere

    target_identifier = "mesh-node"
    device_key = "aabbccddee30"
    iface = _build_minimal_connect_test_interface()
    connected_client = DummyClient()
    connected_client.address = "AA:BB:CC:DD:EE:30"
    connected_client.bleak_client = SimpleNamespace(address=connected_client.address)
    observed_claims: list[bool] = []

    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(
        iface,
        "_raise_if_duplicate_connect",
        lambda _connection_key, **_kwargs: None,
        raising=True,
    )
    monkeypatch.setattr(
        iface, "_get_existing_client_if_valid", lambda _request: None, raising=True
    )
    monkeypatch.setattr(iface, "_connected", lambda: None, raising=True)

    _establish_stub = _make_establish_stub(
        iface,
        lambda: connected_client,
        connected_address=connected_client.address,
        device_key=device_key,
        alias_key=target_identifier,
    )

    def _finalize_stub(
        _self: BLEInterface,
        _client: BLEClient,
        _device_key: str | None,
        _alias_key: str | None,
    ) -> None:
        observed_claims.append(
            _is_currently_connected_elsewhere(device_key, owner=object())
        )

    monkeypatch.setattr(
        iface,
        "_establish_and_update_client",
        _establish_stub,
        raising=True,
    )
    monkeypatch.setattr(BLEInterface, "_finalize_connection_gates", _finalize_stub)

    iface.connect(target_identifier)

    assert observed_claims == [True]
    assert _is_currently_connected_elsewhere(device_key, owner=object()) is False


def test_connect_name_target_reserves_requested_and_resolved_keys(
    monkeypatch: pytest.MonkeyPatch,
    clear_registry: None,
) -> None:
    """Name-based connect should reserve both alias and resolved concrete keys."""
    _ = clear_registry
    from meshtastic.interfaces.ble.gating import _addr_key

    iface = _build_minimal_connect_test_interface()
    target_identifier = "mesh-node"
    resolved_address = "AA:BB:CC:DD:EE:31"
    connected_client = DummyClient()
    connected_client.address = resolved_address
    connected_client.bleak_client = SimpleNamespace(address=resolved_address)
    cast(Any, iface)._discovery_manager = object()
    duplicate_checks: list[str] = []
    addr_lock_keys: list[str | None] = []
    established_args: list[tuple[str | None, str | None, str | None]] = []

    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(
        iface, "_get_existing_client_if_valid", lambda _request: None, raising=True
    )
    monkeypatch.setattr(
        iface,
        "findDevice",
        lambda _identifier: BLEDevice(
            address=resolved_address, name="Mesh", details={}
        ),
        raising=True,
    )

    def _record_duplicate_check(_key: str | None, **_kwargs: object) -> None:
        if _key is not None:
            duplicate_checks.append(_key)

    monkeypatch.setattr(
        iface, "_raise_if_duplicate_connect", _record_duplicate_check, raising=True
    )
    monkeypatch.setattr(iface, "_finalize_connection_gates", lambda *_args: None)
    monkeypatch.setattr(iface, "_connected", lambda: None, raising=True)
    monkeypatch.setattr(
        iface,
        "_emit_verified_connection_side_effects",
        lambda _client: None,
        raising=True,
    )

    @contextlib.contextmanager
    def _record_addr_lock_context(_addr: str | None) -> Iterator[threading.RLock]:
        addr_lock_keys.append(_addr)
        lock = threading.RLock()
        yield lock

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface._addr_lock_context",
        _record_addr_lock_context,
    )

    def _record_establish(
        address: str | None,
        normalized_request: str | None,
        address_key: str | None,
        _pair_on_connect: bool,
        _connect_timeout: float | None,
    ) -> None:
        established_args.append((address, normalized_request, address_key))

    _establish_stub = _make_establish_stub(
        iface,
        lambda: connected_client,
        connected_address=resolved_address,
        device_key=_addr_key(resolved_address),
        alias_key=_addr_key(target_identifier),
        on_call=_record_establish,
    )

    monkeypatch.setattr(
        iface,
        "_establish_and_update_client",
        _establish_stub,
        raising=True,
    )

    result = iface.connect(target_identifier)

    assert cast(object, result) is connected_client
    requested_key = _addr_key(target_identifier)
    resolved_key = _addr_key(resolved_address)
    assert requested_key is not None and resolved_key is not None
    assert duplicate_checks.count(requested_key) >= 2
    assert duplicate_checks.count(resolved_key) >= 2
    assert requested_key in addr_lock_keys
    assert resolved_key in addr_lock_keys
    assert established_args == [
        (
            resolved_address,
            iface._sanitize_address(target_identifier),
            requested_key,
        )
    ]


def test_connect_raises_when_client_becomes_stale_after_gate_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should not return a client that lost ownership after finalization."""
    target_address = "AA:BB:CC:DD:EE:03"
    replacement_address = "AA:BB:CC:DD:EE:04"
    iface = _build_minimal_connect_test_interface()
    connected_callbacks: list[bool] = []
    connected_client = DummyClient()
    connected_client.address = target_address
    connected_client.bleak_client = SimpleNamespace(address=target_address)
    finalized_clients: list[BLEClient] = []
    closed_clients: list[BLEClient] = []
    released_claims: list[tuple[str | None, ...]] = []

    monkeypatch.setattr(iface, "_connected", lambda: connected_callbacks.append(True))
    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(
        iface,
        "_raise_if_duplicate_connect",
        lambda _connection_key, **_kwargs: None,
        raising=True,
    )
    monkeypatch.setattr(
        iface, "_get_existing_client_if_valid", lambda _request: None, raising=True
    )
    monkeypatch.setattr(
        iface._client_manager,
        "_safe_close_client",
        lambda client: closed_clients.append(client),
        raising=True,
    )
    monkeypatch.setattr(
        iface,
        "_mark_address_keys_disconnected",
        lambda *keys: released_claims.append(keys),
        raising=True,
    )

    _establish_stub = _make_establish_stub(
        iface,
        lambda: connected_client,
        connected_address=target_address,
        device_key="device-key",
    )

    def _finalize_stub(
        _self: BLEInterface,
        _client: BLEClient,
        _device_key: str | None,
        _alias_key: str | None,
    ) -> None:
        finalized_clients.append(_client)
        replacement_client = DummyClient()
        replacement_client.address = replacement_address
        replacement_client.bleak_client = SimpleNamespace(address=replacement_address)
        with iface._state_lock:
            cast(Any, iface).client = replacement_client
            iface.address = replacement_address
            iface._state_manager._reset_to_disconnected()
            assert iface._state_manager._transition_to(ConnectionState.CONNECTING)
            assert iface._state_manager._transition_to(ConnectionState.CONNECTED)

    monkeypatch.setattr(
        iface,
        "_establish_and_update_client",
        _establish_stub,
        raising=True,
    )
    monkeypatch.setattr(BLEInterface, "_finalize_connection_gates", _finalize_stub)

    with pytest.raises(BLEInterface.BLEError, match=CONNECTION_ERROR_LOST_OWNERSHIP):
        iface.connect(target_address)

    assert finalized_clients == [cast(BLEClient, connected_client)]
    assert closed_clients == [cast(BLEClient, connected_client)]
    assert released_claims == [("device-key",)]
    assert connected_callbacks == []
    assert cast(object, iface.client) is not connected_client
    assert iface.address != target_address


def test_connect_preserves_reclaimed_keys_for_newer_client_after_gate_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should not release keys a newer client on this interface already reclaimed."""
    target_address = "AA:BB:CC:DD:EE:03"
    device_key = "aabbccddee03"
    alias_key = "mesh-node"
    iface = _build_minimal_connect_test_interface()
    connected_client = DummyClient()
    connected_client.address = target_address
    connected_client.bleak_client = SimpleNamespace(address=target_address)
    finalized_clients: list[BLEClient] = []
    closed_clients: list[BLEClient] = []
    released_claims: list[tuple[str | None, ...]] = []

    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(
        iface,
        "_raise_if_duplicate_connect",
        lambda _connection_key, **_kwargs: None,
        raising=True,
    )
    monkeypatch.setattr(
        iface, "_get_existing_client_if_valid", lambda _request: None, raising=True
    )
    monkeypatch.setattr(
        iface._client_manager,
        "_safe_close_client",
        lambda client: closed_clients.append(client),
        raising=True,
    )
    monkeypatch.setattr(
        iface,
        "_mark_address_keys_disconnected",
        lambda *keys: released_claims.append(keys),
        raising=True,
    )

    _establish_stub = _make_establish_stub(
        iface,
        lambda: connected_client,
        connected_address=target_address,
        device_key=device_key,
        alias_key=alias_key,
        connection_alias_key=alias_key,
    )

    def _finalize_stub(
        _self: BLEInterface,
        _client: BLEClient,
        _device_key: str | None,
        _alias_key: str | None,
    ) -> None:
        finalized_clients.append(_client)
        replacement_client = DummyClient()
        replacement_client.address = target_address
        replacement_client.bleak_client = SimpleNamespace(address=target_address)
        with iface._state_lock:
            cast(Any, iface).client = replacement_client
            iface.address = target_address
            iface._connection_alias_key = alias_key
            iface._state_manager._reset_to_disconnected()
            assert iface._state_manager._transition_to(ConnectionState.CONNECTING)
            assert iface._state_manager._transition_to(ConnectionState.CONNECTED)

    monkeypatch.setattr(
        iface,
        "_establish_and_update_client",
        _establish_stub,
        raising=True,
    )
    monkeypatch.setattr(BLEInterface, "_finalize_connection_gates", _finalize_stub)

    with pytest.raises(BLEInterface.BLEError, match=CONNECTION_ERROR_LOST_OWNERSHIP):
        iface.connect(target_address)

    assert finalized_clients == [cast(BLEClient, connected_client)]
    assert closed_clients == [cast(BLEClient, connected_client)]
    assert released_claims == []
    assert cast(Any, iface).client is not connected_client


def test_connect_raises_when_registry_ownership_is_lost_after_gate_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should fail if process-wide address ownership moves elsewhere."""
    target_address = "AA:BB:CC:DD:EE:0A"
    iface = _build_minimal_connect_test_interface()
    connected_callbacks: list[bool] = []
    connected_client = DummyClient()
    connected_client.address = target_address
    connected_client.bleak_client = SimpleNamespace(address=target_address)
    finalized_clients: list[BLEClient] = []
    closed_clients: list[BLEClient] = []
    released_claims: list[tuple[str | None, ...]] = []

    monkeypatch.setattr(iface, "_connected", lambda: connected_callbacks.append(True))
    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(
        iface,
        "_raise_if_duplicate_connect",
        lambda _connection_key, **_kwargs: None,
        raising=True,
    )
    monkeypatch.setattr(
        iface, "_get_existing_client_if_valid", lambda _request: None, raising=True
    )
    monkeypatch.setattr(
        iface._client_manager,
        "_safe_close_client",
        lambda client: closed_clients.append(client),
        raising=True,
    )
    monkeypatch.setattr(
        iface,
        "_mark_address_keys_disconnected",
        lambda *keys: released_claims.append(keys),
        raising=True,
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface._is_currently_connected_elsewhere",
        lambda key, owner=None: key == "device-key" and owner is iface,
    )

    _establish_stub = _make_establish_stub(
        iface,
        lambda: connected_client,
        connected_address=target_address,
        device_key="device-key",
    )

    def _finalize_stub(
        _self: BLEInterface,
        _client: BLEClient,
        _device_key: str | None,
        _alias_key: str | None,
    ) -> None:
        finalized_clients.append(_client)

    monkeypatch.setattr(
        iface,
        "_establish_and_update_client",
        _establish_stub,
        raising=True,
    )
    monkeypatch.setattr(BLEInterface, "_finalize_connection_gates", _finalize_stub)

    with pytest.raises(BLEInterface.BLEError, match=CONNECTION_ERROR_LOST_OWNERSHIP):
        iface.connect(target_address)

    assert finalized_clients == [cast(BLEClient, connected_client)]
    assert closed_clients == [cast(BLEClient, connected_client)]
    assert released_claims == [("device-key",)]
    assert connected_callbacks == []
    assert iface.client is None
    assert iface.address == target_address
    assert iface._last_connection_request == iface._sanitize_address(target_address)


def test_connect_restores_requested_identifier_after_name_target_loses_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A discarded name-based connect should restore the caller's requested identifier."""
    target_identifier = "mesh-node"
    target_address = "AA:BB:CC:DD:EE:11"
    iface = _build_minimal_connect_test_interface()
    connected_client = DummyClient()
    connected_client.address = target_address
    connected_client.bleak_client = SimpleNamespace(address=target_address)
    closed_clients: list[BLEClient] = []
    released_claims: list[tuple[str | None, ...]] = []

    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(
        iface,
        "_raise_if_duplicate_connect",
        lambda _connection_key, **_kwargs: None,
        raising=True,
    )
    monkeypatch.setattr(
        iface, "_get_existing_client_if_valid", lambda _request: None, raising=True
    )
    monkeypatch.setattr(
        iface._client_manager,
        "_safe_close_client",
        lambda client: closed_clients.append(client),
        raising=True,
    )
    monkeypatch.setattr(
        iface,
        "_mark_address_keys_disconnected",
        lambda *keys: released_claims.append(keys),
        raising=True,
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface._is_currently_connected_elsewhere",
        lambda key, owner=None: key == "device-key" and owner is iface,
    )

    _establish_stub = _make_establish_stub(
        iface,
        lambda: connected_client,
        connected_address=target_address,
        device_key="device-key",
    )

    monkeypatch.setattr(
        iface,
        "_establish_and_update_client",
        _establish_stub,
        raising=True,
    )
    monkeypatch.setattr(BLEInterface, "_finalize_connection_gates", lambda *_args: None)

    with pytest.raises(BLEInterface.BLEError, match=CONNECTION_ERROR_LOST_OWNERSHIP):
        iface.connect(target_identifier)

    assert closed_clients == [cast(BLEClient, connected_client)]
    assert released_claims == [("device-key",)]
    assert iface.address == target_identifier
    assert iface._last_connection_request == iface._sanitize_address(target_identifier)
    with iface._state_lock:
        assert iface._get_current_implicit_management_address_locked() is None


def test_connect_rechecks_ownership_before_publishing_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should discard a client that becomes stale after the first check."""
    from meshtastic.interfaces.ble.lifecycle_ownership_runtime import (
        BLEConnectionOwnershipLifecycleCoordinator,
    )

    target_address = "AA:BB:CC:DD:EE:12"
    iface = _build_minimal_connect_test_interface()
    connected_client = DummyClient()
    connected_client.address = target_address
    connected_client.bleak_client = SimpleNamespace(address=target_address)
    connected_callbacks: list[bool] = []
    closed_clients: list[BLEClient] = []
    released_claims: list[tuple[str | None, ...]] = []
    status_checks = iter([(True, False), (False, False)])

    def _next_status(_iface: BLEInterface, _client: BLEClient) -> tuple[bool, bool]:
        return next(status_checks, (False, False))

    monkeypatch.setattr(iface, "_connected", lambda: connected_callbacks.append(True))
    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(
        iface,
        "_raise_if_duplicate_connect",
        lambda _connection_key, **_kwargs: None,
        raising=True,
    )
    monkeypatch.setattr(
        iface, "_get_existing_client_if_valid", lambda _request: None, raising=True
    )
    monkeypatch.setattr(
        iface._client_manager,
        "_safe_close_client",
        lambda client: closed_clients.append(client),
        raising=True,
    )
    monkeypatch.setattr(
        iface,
        "_mark_address_keys_disconnected",
        lambda *keys: released_claims.append(keys),
        raising=True,
    )
    monkeypatch.setattr(
        BLEConnectionOwnershipLifecycleCoordinator,
        "_get_connected_client_status_locked",
        lambda self, client: _next_status(cast("BLEInterface", self), client),
        raising=True,
    )
    monkeypatch.setattr(
        iface, "_has_lost_gate_ownership", lambda *_keys: True, raising=True
    )

    _establish_stub = _make_establish_stub(
        iface,
        lambda: connected_client,
        connected_address=target_address,
        device_key="device-key",
    )

    monkeypatch.setattr(
        iface,
        "_establish_and_update_client",
        _establish_stub,
        raising=True,
    )
    monkeypatch.setattr(BLEInterface, "_finalize_connection_gates", lambda *_args: None)

    with pytest.raises(BLEInterface.BLEError, match=CONNECTION_ERROR_LOST_OWNERSHIP):
        iface.connect(target_address)

    assert connected_callbacks == []
    assert released_claims == [("device-key",)]
    assert closed_clients == [cast(BLEClient, connected_client)]
    assert iface.client is None
    assert iface.address == target_address


def test_connect_raises_when_shutdown_wins_after_gate_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should surface shutdown when close() wins after gate finalization."""
    target_address = "AA:BB:CC:DD:EE:05"
    iface = _build_minimal_connect_test_interface()
    connected_callbacks: list[bool] = []
    connected_client = DummyClient()
    connected_client.address = target_address
    connected_client.bleak_client = SimpleNamespace(address=target_address)
    finalized_clients: list[BLEClient] = []
    closed_clients: list[BLEClient] = []
    released_claims: list[tuple[str | None, ...]] = []

    monkeypatch.setattr(iface, "_connected", lambda: connected_callbacks.append(True))
    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(
        iface,
        "_raise_if_duplicate_connect",
        lambda _connection_key, **_kwargs: None,
        raising=True,
    )
    monkeypatch.setattr(
        iface, "_get_existing_client_if_valid", lambda _request: None, raising=True
    )
    monkeypatch.setattr(
        iface._client_manager,
        "_safe_close_client",
        lambda client: closed_clients.append(client),
        raising=True,
    )
    monkeypatch.setattr(
        iface,
        "_mark_address_keys_disconnected",
        lambda *keys: released_claims.append(keys),
        raising=True,
    )

    _establish_stub = _make_establish_stub(
        iface,
        lambda: connected_client,
        connected_address=target_address,
        device_key="device-key",
    )

    def _finalize_stub(
        _self: BLEInterface,
        _client: BLEClient,
        _device_key: str | None,
        _alias_key: str | None,
    ) -> None:
        finalized_clients.append(_client)
        with iface._state_lock:
            iface._closed = True

    monkeypatch.setattr(
        iface,
        "_establish_and_update_client",
        _establish_stub,
        raising=True,
    )
    monkeypatch.setattr(BLEInterface, "_finalize_connection_gates", _finalize_stub)

    with pytest.raises(BLEInterface.BLEError, match=ERROR_INTERFACE_CLOSING):
        iface.connect(target_address)

    assert finalized_clients == [cast(BLEClient, connected_client)]
    assert closed_clients == [cast(BLEClient, connected_client)]
    assert released_claims == [("device-key",)]
    assert connected_callbacks == []
    assert cast(object, iface.client) is not connected_client
    assert iface.address == target_address
