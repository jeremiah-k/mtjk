"""BLE interface management and compatibility core tests."""

# pylint: disable=redefined-outer-name

import contextlib
import re
import threading
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, Callable, cast

import pytest

# Import meshtastic modules for use in tests
import meshtastic.interfaces.ble as ble_mod
import meshtastic.interfaces.ble.interface as ble_interface_mod
from meshtastic.interfaces.ble import (
    BLEClient,
    BLEInterface,
)
from meshtastic.interfaces.ble.constants import (
    BLECLIENT_ERROR_CANNOT_PAIR_NOT_INITIALIZED,
    BLECLIENT_ERROR_CANNOT_UNPAIR_NOT_INITIALIZED,
    ERROR_CONNECTION_SUPPRESSED,
    ERROR_MANAGEMENT_ADDRESS_EMPTY,
    ERROR_MANAGEMENT_ADDRESS_REQUIRED,
    ERROR_MANAGEMENT_AWAIT_TIMEOUT_INVALID,
    ERROR_MANAGEMENT_TARGET_CHANGED,
)
from meshtastic.interfaces.ble.management_state import BLEManagementState
from meshtastic.interfaces.ble.state import BLEStateManager, ConnectionState
from tests._ble_interface_core_support import (
    _capture_management_wait_event,
    _clear_management_handler,
    _create_ble_device,
)

# Import common fixtures
from tests.test_ble_interface_fixtures import DummyClient, _build_interface

pytestmark = pytest.mark.unit


def test_get_management_state_replaces_invalid_partial_object_state() -> None:
    """Partial objects should normalize foreign private state to the owned state type."""
    iface = object.__new__(BLEInterface)
    foreign_state = SimpleNamespace(inflight=7)
    iface.__dict__["_management_state"] = foreign_state

    resolved = iface._get_management_state()

    assert resolved is iface.__dict__["_management_state"]
    assert resolved is not foreign_state
    assert resolved.inflight == 0


def test_get_management_state_repairs_invalid_state_once_under_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent partial-object repair should converge on one management state."""
    iface = object.__new__(BLEInterface)
    iface.__dict__["_management_state"] = object()
    real_state_type = BLEManagementState
    first_constructor_entered = threading.Event()
    second_constructor_entered = threading.Event()
    release_first_constructor = threading.Event()
    constructor_count = 0
    constructor_count_lock = threading.Lock()

    real_state_base: Any = real_state_type

    class _BlockingManagementState(real_state_base):  # type: ignore[misc]
        def __init__(self) -> None:
            nonlocal constructor_count
            with constructor_count_lock:
                constructor_count += 1
                invocation = constructor_count
            if invocation == 1:
                first_constructor_entered.set()
                assert release_first_constructor.wait(timeout=1.0)
            else:
                second_constructor_entered.set()
            super().__init__()

    monkeypatch.setattr(
        ble_interface_mod,
        "BLEManagementState",
        _BlockingManagementState,
    )
    resolved: list[object] = []

    def _resolve() -> None:
        resolved.append(iface._get_management_state())

    first = threading.Thread(target=_resolve)
    second = threading.Thread(target=_resolve)
    first.start()
    assert first_constructor_entered.wait(timeout=1.0)
    second.start()

    # The second resolver must block on the shared partial-object init lock,
    # rather than constructing a competing state while the first is in flight.
    assert not second_constructor_entered.wait(timeout=0.05)
    release_first_constructor.set()
    first.join(timeout=1.0)
    second.join(timeout=1.0)

    assert not first.is_alive()
    assert not second.is_alive()
    assert constructor_count == 1
    assert len(resolved) == 2
    assert resolved[0] is resolved[1]
    assert resolved[0] is iface.__dict__["_management_state"]


def test_find_device_returns_single_scan_result() -> None:
    """FindDevice should return the lone scanned device."""
    # BLEDevice and BLEInterface already imported at top as ble_mod.BLEDevice, ble_mod.BLEInterface

    # Intentional constructor bypass: inject a controlled _discovery_manager
    # without running BLEInterface.__init__ side effects.
    iface = object.__new__(ble_mod.BLEInterface)
    scanned_device = _create_ble_device(address="11:22:33:44:55:66", name="Test Device")
    iface._discovery_manager = SimpleNamespace(  # type: ignore[assignment]
        _discover_devices=lambda _address: [scanned_device]
    )

    result = ble_mod.BLEInterface.findDevice(iface, None)

    assert result is scanned_device


def test_find_device_multiple_scan_results_without_address_raises() -> None:
    """Discovery-mode findDevice should reject ambiguous multi-device scans."""
    iface = object.__new__(ble_mod.BLEInterface)
    devices = [
        _create_ble_device(address="11:22:33:44:55:66", name="Meshtastic-A"),
        _create_ble_device(address="22:33:44:55:66:77", name="Meshtastic-B"),
    ]
    iface._discovery_manager = SimpleNamespace(  # type: ignore[assignment]
        _discover_devices=lambda _address: devices
    )

    with pytest.raises(BLEInterface.BLEError) as excinfo:
        ble_mod.BLEInterface.findDevice(iface, None)

    assert "Multiple Meshtastic BLE peripherals found." in str(excinfo.value)


def test_ble_package_all_uses_stable_surface() -> None:
    """`meshtastic.interfaces.ble.__all__` should expose the stable facade only."""
    assert "BLEInterface" in ble_mod.__all__
    assert "BLEClient" in ble_mod.__all__
    assert "ConnectionValidator" not in ble_mod.__all__
    assert "ThreadCoordinator" not in ble_mod.__all__


def test_ble_package_and_legacy_facade_exports_match() -> None:
    """Legacy BLE facade should include canonical exports plus retained Bleak compat names."""
    import meshtastic.ble_interface as legacy_ble_mod

    canonical_exports = set(ble_mod.__all__)
    legacy_exports = set(legacy_ble_mod.__all__)
    compat_bleak_exports = {
        "BleakClient",
        "BleakScanner",
        "BLEDevice",
        "BleakError",
        "BleakDBusError",
    }

    assert canonical_exports.issubset(legacy_exports)
    assert compat_bleak_exports.issubset(legacy_exports)
    assert canonical_exports.isdisjoint(compat_bleak_exports)


def test_state_manager_closing_only_for_disconnect() -> None:
    """is_closing should be true only while disconnecting."""
    state_manager = BLEStateManager()
    assert state_manager._is_closing is False
    # DISCONNECTED -> DISCONNECTING is not allowed (semantically incorrect:
    # you can't "begin disconnecting" from an already-disconnected state).
    # The proper path is through a connected/active state first.
    assert state_manager._transition_to(ConnectionState.CONNECTING) is True
    assert state_manager._is_closing is False
    assert state_manager._transition_to(ConnectionState.DISCONNECTING) is True
    assert state_manager._is_closing is True
    assert state_manager._transition_to(ConnectionState.DISCONNECTED) is True
    assert state_manager._is_closing is False
    # ERROR state should also not be "closing"
    assert state_manager._transition_to(ConnectionState.ERROR) is True
    assert state_manager._is_closing is False


def test_state_manager_allows_error_to_disconnecting_shutdown() -> None:
    """State manager should support ERROR -> DISCONNECTING for deterministic close paths."""
    state_manager = BLEStateManager()

    assert state_manager._transition_to(ConnectionState.CONNECTING) is True
    assert state_manager._transition_to(ConnectionState.ERROR) is True
    assert state_manager._transition_to(ConnectionState.DISCONNECTING) is True
    assert state_manager._is_closing is True
    assert state_manager._transition_to(ConnectionState.DISCONNECTED) is True
    assert state_manager._is_closing is False


def test_ble_interface_defaults_auto_reconnect_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BLEInterface should default auto_reconnect to False.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
    """
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    assert iface.auto_reconnect is False
    assert iface.pair_on_connect is False
    iface.close()


def test_ble_interface_init_rejects_non_bool_pair_on_connect() -> None:
    """Constructor should reject non-bool pair_on_connect values."""
    with pytest.raises(BLEInterface.BLEError, match="pair_on_connect must be a bool"):
        BLEInterface(
            address=None,
            noProto=True,
            pair_on_connect=cast(Any, "false"),
        )


def test_ble_interface_repr_includes_non_default_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """repr() should include non-default flags and debug output."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)

    def _debug_sink(_line: str) -> None:
        return None

    iface.debugOut = _debug_sink
    iface.noProto = True
    iface.noNodes = True
    iface.auto_reconnect = True
    iface.pair_on_connect = True

    rendered = repr(iface)

    assert "address='dummy'" in rendered
    assert "debugOut=" in rendered
    assert "noProto=True" in rendered
    assert "noNodes=True" in rendered
    assert "auto_reconnect=True" in rendered
    assert "pair_on_connect=True" in rendered
    iface.close()


def test_build_interface_connect_stub_records_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared test connect stub should retain keyword arguments for assertions."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)

    iface.connect("AA:BB:CC:DD:EE:FF", pair=True, connect_timeout=4.5)

    assert cast(Any, iface)._connect_stub_calls[-1] == "AA:BB:CC:DD:EE:FF"
    assert cast(Any, iface)._connect_stub_kwargs[-1] == {
        "pair": True,
        "connect_timeout": 4.5,
    }
    iface.close()


def test_ble_interface_extract_client_address_prefers_bleak_and_falls_back() -> None:
    """_extract_client_address should prefer bleak_client.address and then client.address."""
    assert (
        BLEInterface._extract_client_address(
            cast(
                BLEClient,
                SimpleNamespace(
                    bleak_client=SimpleNamespace(address="AA:BB:CC:DD:EE:FF"),
                    address="11:22:33:44:55:66",
                ),
            )
        )
        == "AA:BB:CC:DD:EE:FF"
    )
    assert (
        BLEInterface._extract_client_address(
            cast(
                BLEClient,
                SimpleNamespace(
                    bleak_client=SimpleNamespace(address=None),
                    address="11:22:33:44:55:66",
                ),
            )
        )
        == "11:22:33:44:55:66"
    )
    assert BLEInterface._extract_client_address(None) is None


def test_get_management_command_handler_preserves_injected_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Management handler getter should preserve injected collaborators."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    try:
        injected_handler = SimpleNamespace(name="injected-handler")
        iface._management_command_handler = cast(Any, injected_handler)
        assert iface._get_management_command_handler() is cast(Any, injected_handler)
    finally:
        _clear_management_handler(iface)
        iface.close()


def test_ble_interface_pair_prefers_active_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pair() should delegate to the active matching client when connected."""
    client = DummyClient()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    monkeypatch.setattr(
        iface,
        "findDevice",
        lambda _address: pytest.fail(
            "Unexpected findDevice call during active-client pair reuse test"
        ),
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.BLEClient",
        lambda *_args, **_kwargs: pytest.fail(
            "Unexpected temporary BLEClient created during active-client pair reuse test"
        ),
    )
    _clear_management_handler(iface)

    iface.pair(confirm=True, await_timeout=12.5)
    assert client.pair_calls == 1
    assert client.pair_kwargs == [{"confirm": True}]
    assert client.pair_await_timeouts == [12.5]
    iface.close()


def test_ble_interface_pair_prefers_active_client_without_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pair() should reuse an active client even when it cannot expose an address."""
    client = DummyClient()
    client.address = cast(Any, None)
    client.bleak_client = SimpleNamespace(address=None)
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    with iface._state_lock:
        iface.address = "mesh-node"
    monkeypatch.setattr(
        iface,
        "findDevice",
        lambda _address: pytest.fail(
            "Unexpected findDevice call during active-client address-less pair reuse test"
        ),
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.BLEClient",
        lambda *_args, **_kwargs: pytest.fail(
            "Unexpected temporary BLEClient created during active-client address-less pair reuse test"
        ),
    )
    _clear_management_handler(iface)

    iface.pair(confirm=True, await_timeout=9.5)
    assert client.pair_calls == 1
    assert client.pair_kwargs == [{"confirm": True}]
    assert client.pair_await_timeouts == [9.5]
    iface.close()


def test_ble_interface_unpair_prefers_active_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """unpair() should delegate and run disconnect cleanup when the backend drops."""
    client = DummyClient()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    monkeypatch.setattr(
        iface,
        "findDevice",
        lambda _address: pytest.fail(
            "Unexpected findDevice call during active-client unpair reuse test"
        ),
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.BLEClient",
        lambda *_args, **_kwargs: pytest.fail(
            "Unexpected temporary BLEClient created during active-client unpair reuse test"
        ),
    )
    _clear_management_handler(iface)

    def _on_unpair() -> None:
        iface._handle_disconnect("test-unpair", client=cast(BLEClient, client))

    client.on_unpair = _on_unpair

    iface.unpair(await_timeout=8.0)
    assert client.unpair_calls == 1
    assert client.unpair_await_timeouts == [8.0]
    assert iface.client is None
    assert iface._state_manager._is_connected is False
    iface.close()


def test_ble_interface_pair_uses_existing_client_when_request_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pair() should reuse a matching existing client before creating a temporary one."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface._state_manager._reset_to_disconnected()

    existing_client = DummyClient()
    existing_client.address = "AA:BB:CC:DD:EE:FF"
    existing_client.bleak_client = SimpleNamespace(address="AA:BB:CC:DD:EE:FF")

    monkeypatch.setattr(
        iface,
        "_get_existing_client_if_valid",
        lambda _request: cast(BLEClient, existing_client),
    )
    monkeypatch.setattr(
        iface,
        "findDevice",
        lambda _address: pytest.fail(
            "Unexpected findDevice call during existing-client pair reuse test"
        ),
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.BLEClient",
        lambda *_args, **_kwargs: pytest.fail(
            "Unexpected temporary BLEClient created during existing-client pair reuse test"
        ),
    )
    _clear_management_handler(iface)

    iface.pair("mesh-node", confirm=True, await_timeout=7.0)

    assert existing_client.pair_calls == 1
    assert existing_client.pair_kwargs == [{"confirm": True}]
    assert existing_client.pair_await_timeouts == [7.0]
    iface.close()


@pytest.mark.parametrize(
    "factory_mode",
    ["with_optional_kwargs", "without_optional_kwargs"],
)
def test_ble_interface_pair_uses_temporary_client_when_disconnected(
    monkeypatch: pytest.MonkeyPatch, factory_mode: str
) -> None:
    """pair() should use temporary BLEClient factories with or without kwargs.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Fixture used to patch BLE client construction and cleanup hooks.
    factory_mode : str
        Factory signature variant under test:
        ``"with_optional_kwargs"`` or ``"without_optional_kwargs"``.

    Returns
    -------
    None
    """
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface._state_manager._reset_to_disconnected()
    monkeypatch.setattr(
        iface,
        "findDevice",
        lambda _address: _create_ble_device("AA:BB:CC:DD:EE:FF", "Meshtastic"),
    )

    pair_kwargs: list[dict[str, object]] = []
    pair_await_timeouts: list[float | None] = []

    def _pair(*, await_timeout: float | None = None, **kwargs: object) -> None:
        pair_await_timeouts.append(await_timeout)
        pair_kwargs.append(dict(kwargs))

    temp_client = SimpleNamespace(
        pair=_pair,
        bleak_client=SimpleNamespace(address="AA:BB:CC:DD:EE:FF"),
    )
    cleanup_calls: list[Any] = []

    def _temp_client_factory_with_kwargs(
        _address: str, **_kwargs: object
    ) -> SimpleNamespace:
        return temp_client

    def _temp_client_factory_without_kwargs(_address: str) -> SimpleNamespace:
        return temp_client

    temp_client_factory: Callable[..., SimpleNamespace]
    if factory_mode == "with_optional_kwargs":
        temp_client_factory = _temp_client_factory_with_kwargs
    elif factory_mode == "without_optional_kwargs":
        temp_client_factory = _temp_client_factory_without_kwargs
    else:
        pytest.fail(f"Unexpected factory_mode: {factory_mode}")

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.BLEClient",
        temp_client_factory,
    )
    _clear_management_handler(iface)
    monkeypatch.setattr(
        iface._client_manager,
        "_safe_close_client",
        lambda client: cleanup_calls.append(client),
    )

    iface.pair("mesh-node", confirm=True, await_timeout=7.0)
    assert pair_kwargs == [{"confirm": True}]
    assert pair_await_timeouts == [7.0]
    assert cleanup_calls == [temp_client]
    iface.close()


def test_ble_interface_close_waits_for_temporary_pair_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """close() should wait for temporary-client pair() work to finish."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface.address = None
        iface._state_manager._reset_to_disconnected()
    monkeypatch.setattr(
        iface,
        "findDevice",
        lambda _address: _create_ble_device("AA:BB:CC:DD:EE:FF", "Meshtastic"),
    )

    pair_started = threading.Event()
    allow_pair_return = threading.Event()
    pair_errors: list[Exception] = []
    close_errors: list[Exception] = []
    cleanup_calls: list[Any] = []

    def _blocking_pair(*, await_timeout: float | None = None, **kwargs: object) -> None:
        _ = (await_timeout, kwargs)
        pair_started.set()
        assert allow_pair_return.wait(timeout=1.0)

    temp_client = SimpleNamespace(
        pair=_blocking_pair,
        bleak_client=SimpleNamespace(address="AA:BB:CC:DD:EE:FF"),
    )

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.BLEClient",
        lambda _address, **_kwargs: temp_client,
    )
    _clear_management_handler(iface)
    monkeypatch.setattr(
        iface._client_manager,
        "_safe_close_client",
        lambda client: cleanup_calls.append(client),
    )
    management_wait_entered = _capture_management_wait_event(monkeypatch, iface)

    def _run_pair() -> None:
        try:
            iface.pair("mesh-node", confirm=True, await_timeout=7.0)
        except (
            Exception
        ) as exc:  # pragma: no cover - failure captured below  # noqa: BLE001 - test captures thread errors
            pair_errors.append(exc)

    close_done = threading.Event()

    def _run_close() -> None:
        try:
            iface.close()
        except (
            Exception
        ) as exc:  # pragma: no cover - failure captured below  # noqa: BLE001 - test captures thread errors
            close_errors.append(exc)
        finally:
            close_done.set()

    pair_thread = threading.Thread(target=_run_pair, daemon=True)
    pair_thread.start()
    assert pair_started.wait(timeout=1.0)

    close_thread = threading.Thread(target=_run_close, daemon=True)
    close_thread.start()
    assert management_wait_entered.wait(timeout=1.0)
    with iface._state_lock:
        assert iface._closed is True
    assert close_done.is_set() is False

    allow_pair_return.set()
    pair_thread.join(timeout=2.0)
    close_thread.join(timeout=2.0)

    assert not pair_thread.is_alive()
    assert not close_thread.is_alive()
    assert pair_errors == []
    assert close_errors == []
    assert cleanup_calls == [temp_client]


def test_ble_interface_unpair_uses_temporary_client_when_disconnected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """unpair() should create and clean up a temporary BLEClient when disconnected."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface._state_manager._reset_to_disconnected()
    monkeypatch.setattr(
        iface,
        "findDevice",
        lambda _address: _create_ble_device("AA:BB:CC:DD:EE:FF", "Meshtastic"),
    )

    unpair_await_timeouts: list[float | None] = []

    def _unpair(*, await_timeout: float | None = None) -> None:
        unpair_await_timeouts.append(await_timeout)

    temp_client = SimpleNamespace(
        unpair=_unpair,
        bleak_client=SimpleNamespace(address="AA:BB:CC:DD:EE:FF"),
    )
    cleanup_calls: list[Any] = []

    def _temp_client_factory(_address: str, **_kwargs: object) -> SimpleNamespace:
        return temp_client

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.BLEClient",
        _temp_client_factory,
    )
    _clear_management_handler(iface)
    monkeypatch.setattr(
        iface._client_manager,
        "_safe_close_client",
        lambda client: cleanup_calls.append(client),
    )

    iface.unpair("mesh-node", await_timeout=7.0)

    assert unpair_await_timeouts == [7.0]
    assert cleanup_calls == [temp_client]
    iface.close()


@pytest.mark.parametrize("method_name", ["pair", "unpair"])
def test_ble_interface_management_rejects_temp_client_when_target_owned_elsewhere(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    """Disconnected management ops should not open a temp client for another interface's target."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface._state_manager._reset_to_disconnected()
    monkeypatch.setattr(
        iface,
        "findDevice",
        lambda _address: _create_ble_device("AA:BB:CC:DD:EE:FF", "Meshtastic"),
    )
    lock_states: list[tuple[bool, bool]] = []

    def _connected_elsewhere_probe(
        key: str | None, owner: object | None = None
    ) -> bool:
        probe_done = threading.Event()
        probe_result: list[tuple[bool, bool]] = []

        def _probe_lock_ownership() -> None:
            connect_lock = cast(Any, iface._connect_lock)
            connect_lock_was_free = connect_lock.acquire(blocking=False)
            if connect_lock_was_free:
                connect_lock.release()
            management_lock = cast(Any, iface._management_lock)
            management_lock_was_free = management_lock.acquire(blocking=False)
            if management_lock_was_free:
                management_lock.release()
            probe_result.append((connect_lock_was_free, management_lock_was_free))
            probe_done.set()

        probe_thread = threading.Thread(target=_probe_lock_ownership, daemon=True)
        probe_thread.start()
        assert probe_done.wait(timeout=1.0)
        probe_thread.join(timeout=1.0)
        assert probe_result
        lock_states.append(probe_result[0])
        return key == "aabbccddeeff" and owner is iface

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface._is_currently_connected_elsewhere",
        _connected_elsewhere_probe,
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.BLEClient",
        lambda *_args, **_kwargs: pytest.fail(
            "Temporary BLEClient should not be created when target is owned elsewhere"
        ),
    )
    _clear_management_handler(iface)

    if method_name == "pair":
        with pytest.raises(BLEInterface.BLEError, match=ERROR_CONNECTION_SUPPRESSED):
            iface.pair("mesh-node", confirm=True, await_timeout=7.0)
    else:
        with pytest.raises(BLEInterface.BLEError, match=ERROR_CONNECTION_SUPPRESSED):
            iface.unpair("mesh-node", await_timeout=7.0)

    assert lock_states
    assert all(state == (True, True) for state in lock_states)
    iface.close()


@pytest.mark.parametrize("method_name", ["pair", "unpair"])
def test_ble_interface_management_revalidates_implicit_target_after_gate_handoff(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    """Implicit management ops should abort if the interface target changes at the gate."""
    current_address = "AA:BB:CC:DD:EE:FF"
    replacement_address = "11:22:33:44:55:66"
    client = DummyClient()
    client.address = current_address
    client.bleak_client = SimpleNamespace(address=current_address)
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)

    replacement_client = DummyClient()
    replacement_client.address = replacement_address
    replacement_client.bleak_client = SimpleNamespace(address=replacement_address)
    command_calls: list[str] = []

    def _record_pair(*, await_timeout: float = 0.0, **kwargs: object) -> None:
        _ = (await_timeout, kwargs)
        command_calls.append("pair")

    def _record_unpair(*, await_timeout: float = 0.0, **kwargs: object) -> None:
        _ = (await_timeout, kwargs)
        command_calls.append("unpair")

    monkeypatch.setattr(client, "pair", _record_pair)
    monkeypatch.setattr(client, "unpair", _record_unpair)
    monkeypatch.setattr(replacement_client, "pair", _record_pair)
    monkeypatch.setattr(replacement_client, "unpair", _record_unpair)

    @contextlib.contextmanager
    def _swap_target_gate(_target_address: str) -> Iterator[None]:
        with iface._state_lock:
            cast(Any, iface).client = replacement_client
            iface.address = replacement_address
            iface._state_manager._reset_to_disconnected()
            assert iface._state_manager._transition_to(ConnectionState.CONNECTING)
            assert iface._state_manager._transition_to(ConnectionState.CONNECTED)
        yield

    _clear_management_handler(iface)
    monkeypatch.setattr(iface, "_management_target_gate", _swap_target_gate)

    if method_name == "pair":
        with pytest.raises(
            BLEInterface.BLEError, match=ERROR_MANAGEMENT_TARGET_CHANGED
        ):
            iface.pair(confirm=True, await_timeout=7.0)
    else:
        with pytest.raises(
            BLEInterface.BLEError, match=ERROR_MANAGEMENT_TARGET_CHANGED
        ):
            iface.unpair(await_timeout=7.0)

    assert command_calls == []
    iface.close()


@pytest.mark.parametrize("method_name", ["pair", "unpair"])
def test_ble_interface_management_aborts_when_implicit_target_disappears_at_gate(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    """Implicit management ops should abort if the bound target disappears at the gate."""
    current_address = "AA:BB:CC:DD:EE:FF"
    client = DummyClient()
    client.address = current_address
    client.bleak_client = SimpleNamespace(address=current_address)
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    command_calls: list[str] = []

    monkeypatch.setattr(client, "pair", lambda **_kwargs: command_calls.append("pair"))
    monkeypatch.setattr(
        client, "unpair", lambda **_kwargs: command_calls.append("unpair")
    )

    @contextlib.contextmanager
    def _clear_target_gate(_target_address: str) -> Iterator[None]:
        with iface._state_lock:
            cast(Any, iface).client = None
            iface.address = None
            iface._state_manager._reset_to_disconnected()
        yield

    _clear_management_handler(iface)
    monkeypatch.setattr(iface, "_management_target_gate", _clear_target_gate)

    if method_name == "pair":
        with pytest.raises(
            BLEInterface.BLEError, match=ERROR_MANAGEMENT_TARGET_CHANGED
        ):
            iface.pair(confirm=True, await_timeout=7.0)
    else:
        with pytest.raises(
            BLEInterface.BLEError, match=ERROR_MANAGEMENT_TARGET_CHANGED
        ):
            iface.unpair(await_timeout=7.0)

    assert command_calls == []
    iface.close()


def test_get_current_implicit_management_address_locked_returns_concrete_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Implicit management address helper should return concrete BLE address bindings."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface.address = "AA:BB:CC:DD:EE:FF"
        assert iface._get_current_implicit_management_address_locked() == iface.address
    iface.close()


def test_revalidate_implicit_management_target_rejects_binding_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Implicit target revalidation should fail when the binding changes while waiting."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface.address = "mesh-node"

    with pytest.raises(BLEInterface.BLEError, match=ERROR_MANAGEMENT_TARGET_CHANGED):
        iface._revalidate_implicit_management_target(
            "AA:BB:CC:DD:EE:FF",
            expected_binding="different-node",
        )

    iface.close()


def test_execute_management_command_falls_back_when_existing_client_disappears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Management command path should resolve fallback target when existing client disappears."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    addressless_client = cast(
        BLEClient,
        SimpleNamespace(
            isConnected=lambda: True,
            bleak_client=None,
            address=None,
        ),
    )
    call_count = 0

    def _get_management_client(_address: str | None) -> BLEClient | None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return addressless_client
        return None

    monkeypatch.setattr(
        iface,
        "_get_management_client_if_available_locked",
        _get_management_client,
    )
    monkeypatch.setattr(
        iface,
        "_resolve_target_address_for_management",
        lambda _address: None,
    )

    with pytest.raises(BLEInterface.BLEError, match=ERROR_MANAGEMENT_ADDRESS_REQUIRED):
        iface._execute_management_command(None, lambda _client: None)

    iface.close()


def test_execute_management_command_requires_resolved_target_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Management command path should fail when no target address can be resolved."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    monkeypatch.setattr(
        iface, "_get_management_client_if_available_locked", lambda _address: None
    )
    monkeypatch.setattr(
        iface, "_resolve_target_address_for_management", lambda _address: None
    )

    with pytest.raises(BLEInterface.BLEError, match=ERROR_MANAGEMENT_ADDRESS_REQUIRED):
        iface._execute_management_command("mesh-node", lambda _client: None)

    iface.close()


@pytest.mark.parametrize("method_name", ["pair", "unpair"])
def test_ble_interface_management_rejects_blank_explicit_target(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    """pair()/unpair() should reject blank explicit targets before any resolution."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)

    with pytest.raises(BLEInterface.BLEError, match=ERROR_MANAGEMENT_ADDRESS_EMPTY):
        getattr(iface, method_name)("   ")

    iface.close()


@pytest.mark.parametrize("method_name", ["pair", "unpair"])
@pytest.mark.parametrize(
    "invalid_timeout",
    [None, 0.0, -1.0, float("nan"), float("inf"), True],
)
def test_ble_interface_management_rejects_unbounded_or_invalid_await_timeout(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    invalid_timeout: object,
) -> None:
    """pair()/unpair() should require a finite positive await_timeout."""
    client = DummyClient()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)

    with pytest.raises(
        BLEInterface.BLEError,
        match=re.escape(ERROR_MANAGEMENT_AWAIT_TIMEOUT_INVALID),
    ):
        getattr(iface, method_name)(await_timeout=invalid_timeout)

    assert client.pair_calls == 0
    assert client.unpair_calls == 0
    iface.close()


@pytest.mark.parametrize(
    ("method_name", "expected_error"),
    [
        ("pair", BLECLIENT_ERROR_CANNOT_PAIR_NOT_INITIALIZED),
        ("unpair", BLECLIENT_ERROR_CANNOT_UNPAIR_NOT_INITIALIZED),
    ],
)
def test_dummy_client_management_rejects_cleared_backend(
    method_name: str,
    expected_error: str,
) -> None:
    """DummyClient should mirror BLEClient management failures after backend teardown."""
    client = DummyClient()
    client.bleak_client = cast(Any, None)

    if method_name == "pair":
        with pytest.raises(BLEClient.BLEError, match=re.escape(expected_error)):
            client.pair(confirm=True)
    else:
        with pytest.raises(BLEClient.BLEError, match=re.escape(expected_error)):
            client.unpair()


def test_ble_client_management_ignores_synthesized_optional_method() -> None:
    """Optional management operations should require a declared backend method."""

    class _DynamicBleakClient:
        def __getattr__(self, _name: str) -> object:
            async def _dynamic(**_kwargs: object) -> None:
                return None

            return _dynamic

    client = BLEClient.__new__(BLEClient)
    client.bleak_client = _DynamicBleakClient()  # type: ignore[assignment]

    with pytest.raises(BLEClient.BLEError) as exc_info:
        client._run_optional_management_method(  # noqa: SLF001
            method_name="pair",
            await_timeout=1.0,
            not_initialized_error=BLECLIENT_ERROR_CANNOT_PAIR_NOT_INITIALIZED,
            unsupported_error="pair unsupported",
        )
    assert str(exc_info.value) == "pair unsupported"
