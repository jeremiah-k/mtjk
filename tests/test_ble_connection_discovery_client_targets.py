"""Targeted branch tests for BLE connection/discovery/client helpers."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from threading import Event, RLock
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from bleak.exc import BleakDBusError, BleakDeviceNotFoundError, BleakError

import meshtastic.interfaces.ble.connection as connection_mod
from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.connection import (
    ClientManager,
    ConnectionOrchestrator,
    ConnectionValidator,
)
from meshtastic.interfaces.ble.constants import DISCONNECT_TIMEOUT_SECONDS
from meshtastic.interfaces.ble.discovery import DiscoveryClientError, DiscoveryManager
from meshtastic.interfaces.ble.errors import (
    BLEAddressMismatchError,
    BLEConnectionTimeoutError,
    BLEDBusTransportError,
    BLEDeviceNotFoundError,
    BLEErrorHandler,
)
from meshtastic.interfaces.ble.management_runtime import BLEManagementCommandHandler
from meshtastic.interfaces.ble.state import BLEStateManager, ConnectionState
from tests.test_ble_interface_fixtures import DummyClient, _build_interface

pytestmark = pytest.mark.unit

TEST_BLE_ADDRESS = "AA:BB:CC:DD:EE:FF"
TEST_CONNECT_TIMEOUT_SECONDS = 5.0


class _TestBLEError(Exception):
    """Custom BLEError type for focused branch tests."""


@contextlib.contextmanager
def _make_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Any, ConnectionOrchestrator]]:
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    validator = ConnectionValidator(BLEStateManager(), RLock(), iface.BLEError)
    client_manager = ClientManager(
        BLEStateManager(),
        RLock(),
        cast(Any, SimpleNamespace()),
        BLEErrorHandler(),
    )
    discovery_manager = SimpleNamespace()
    orchestrator = ConnectionOrchestrator(
        iface,
        validator,
        client_manager,
        cast(Any, discovery_manager),
        BLEStateManager(),
        RLock(),
        cast(Any, SimpleNamespace()),
    )
    try:
        yield iface, orchestrator
    finally:
        iface.close()


def test_connection_validator_client_is_connected_member_paths() -> None:
    """ConnectionValidator._client_is_connected should handle callable/member compatibility."""
    assert ConnectionValidator._client_is_connected(None) is False

    client_callable = SimpleNamespace(isConnected=lambda: True)
    assert (
        ConnectionValidator._client_is_connected(cast(BLEClient, client_callable))
        is True
    )

    client_bool_member = SimpleNamespace(is_connected=True)
    assert (
        ConnectionValidator._client_is_connected(cast(BLEClient, client_bool_member))
        is True
    )

    client_unknown = SimpleNamespace(isConnected=lambda: "yes")
    assert (
        ConnectionValidator._client_is_connected(cast(BLEClient, client_unknown))
        is False
    )


def test_connectivity_probes_share_declared_fallback_semantics() -> None:
    """Connection and management paths should use the same declared fallback."""

    class FallbackConnectivityClient:
        def __init__(self) -> None:
            self.preferred_probe_calls = 0

        def isConnected(self) -> bool:  # noqa: N802 - compatibility spelling
            self.preferred_probe_calls += 1
            raise RuntimeError("preferred probe failed")

        def _is_connected(self) -> bool:
            return True

        def __getattr__(self, _name: str) -> object:
            return lambda: False

    client = FallbackConnectivityClient()

    assert ConnectionValidator._client_is_connected(client) is True  # type: ignore[arg-type]
    assert client.preferred_probe_calls == 1
    assert BLEManagementCommandHandler._is_client_connected(client) is True  # type: ignore[arg-type]
    assert client.preferred_probe_calls == 2


def test_connection_helpers_cover_mock_import_and_inline_safe_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connection helper utilities should handle import fallback and inline cleanup execution."""

    cleanup_calls: list[str] = []

    def _cleanup() -> None:
        cleanup_calls.append("ran")

    assert connection_mod._run_safe_cleanup(
        _cleanup,
        cleanup_name="client close",
        safe_cleanup_hook=lambda *_args, **_kwargs: False,
    )
    assert cleanup_calls == ["ran"]


def test_client_manager_thread_dispatch_and_wrapper_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ClientManager thread helpers/wrappers should cover public/legacy/missing paths."""
    manager = ClientManager(
        BLEStateManager(),
        RLock(),
        cast(Any, SimpleNamespace()),
        BLEErrorHandler(),
    )

    created = cast(Any, SimpleNamespace(name="created"))
    manager.thread_coordinator = cast(
        Any, SimpleNamespace(create_thread=lambda **_kwargs: created)
    )
    assert (
        manager._thread_create_thread(
            target=lambda: None,
            args=(),
            name="n",
            daemon=True,
        )
        is created
    )

    manager.thread_coordinator = cast(Any, SimpleNamespace())
    with pytest.raises(AttributeError):
        manager._thread_create_thread(
            target=lambda: None, args=(), name="n", daemon=True
        )

    started: list[object] = []
    manager.thread_coordinator = cast(
        Any, SimpleNamespace(_start_thread=lambda thread: started.append(thread))
    )
    manager._thread_start_thread(created)
    assert started == [created]

    manager.thread_coordinator = cast(Any, SimpleNamespace())
    with pytest.raises(AttributeError):
        manager._thread_start_thread(created)

    manager._create_client = MagicMock(return_value=DummyClient())  # type: ignore[method-assign]
    assert isinstance(
        manager.create_client(TEST_BLE_ADDRESS, lambda _client: None),
        DummyClient,
    )

    manager._connect_client = MagicMock(return_value=None)  # type: ignore[method-assign]
    manager.connect_client(cast(BLEClient, DummyClient()))
    manager._connect_client.assert_called_once()


def test_client_manager_connect_client_recovers_from_services_property_bleak_error() -> (
    None
):
    """_connect_client should force service discovery when services property access fails."""
    manager = ClientManager(
        BLEStateManager(),
        RLock(),
        cast(Any, SimpleNamespace()),
        BLEErrorHandler(),
    )

    class _BleakClientWithFailingServices:
        @property
        def services(self) -> None:
            raise BleakError("services not ready")

    class _ConnectedClient:
        def __init__(self) -> None:
            self.connect_calls: list[dict[str, object]] = []
            self.get_services_calls = 0
            self.bleak_client = _BleakClientWithFailingServices()

        def connect(self, **kwargs: object) -> None:
            self.connect_calls.append(dict(kwargs))

        def _get_services(self) -> None:
            self.get_services_calls += 1

    client = _ConnectedClient()
    manager._connect_client(cast(BLEClient, client))

    assert client.connect_calls
    assert client.get_services_calls == 1


def test_client_manager_update_reference_thread_failure_closes_inline() -> None:
    """update_client_reference should close old client inline on thread start failure."""
    lock = RLock()
    manager = ClientManager(
        BLEStateManager(), lock, cast(Any, SimpleNamespace()), BLEErrorHandler()
    )

    old_client = DummyClient()
    new_client = DummyClient()
    old_client.close_calls = 0

    manager._thread_create_thread = MagicMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(ident=1, is_alive=lambda: True)
    )
    manager._thread_start_thread = MagicMock(side_effect=RuntimeError("start failed"))  # type: ignore[method-assign]
    manager._safe_close_client = MagicMock(return_value=None)  # type: ignore[method-assign]

    manager._update_client_reference(
        cast(BLEClient, new_client), cast(BLEClient, old_client)
    )
    manager._safe_close_client.assert_called_once_with(old_client)


def test_client_manager_safe_close_client_hook_and_wrapper_paths() -> None:
    """safe-close helper should cover keyword/positional hook fallback and wrapper event path."""
    manager = ClientManager(
        BLEStateManager(),
        RLock(),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
    )
    client = DummyClient()
    client.bleak_client = SimpleNamespace()

    hook_calls: list[str] = []

    def _legacy_safe_cleanup(func: Any, cleanup_name: str) -> bool:
        hook_calls.append(cleanup_name)
        func()
        return True

    manager.error_handler = cast(
        Any, SimpleNamespace(_safe_cleanup=_legacy_safe_cleanup)
    )
    manager._safe_close_client(cast(BLEClient, client))
    assert "client close" in hook_calls

    manager.error_handler = cast(
        Any,
        SimpleNamespace(
            _safe_cleanup=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                TypeError("safe_cleanup() got an unexpected keyword argument 'func'")
            )
        ),
    )
    manager._safe_close_client(cast(BLEClient, client))

    event = Event()
    manager.safe_close_client(cast(BLEClient, client), event=event)
    assert event.is_set()


def test_connection_orchestrator_dispatch_set_event_and_kwarg_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Orchestrator dispatch helpers should cover missing/default and kwarg compatibility retries."""
    with _make_orchestrator(monkeypatch) as (_iface, orchestrator):
        assert (
            orchestrator._dispatch_public_or_underscore(
                target=SimpleNamespace(),
                public_name="missing",
                underscore_name="_missing",
                call_member=False,
                default_if_missing="default",
            )
            == "default"
        )

        orchestrator.thread_coordinator = cast(Any, SimpleNamespace())
        orchestrator._thread_set_event("reconnected_event")

        create_calls: list[dict[str, object]] = []

        def _create_client(
            _device: object, _cb: object, **kwargs: object
        ) -> DummyClient:
            create_calls.append(dict(kwargs))
            if "pair_on_connect" in kwargs:
                raise TypeError(  # noqa: TRY003 - intentional fixture message shape
                    "create_client() got an unexpected keyword argument 'pair_on_connect'"
                )
            if "connect_timeout" in kwargs:
                raise TypeError(  # noqa: TRY003 - intentional fixture message shape
                    "create_client() got an unexpected keyword argument 'connect_timeout'"
                )
            return DummyClient()

        orchestrator.client_manager = cast(
            Any, SimpleNamespace(create_client=_create_client)
        )
        created = orchestrator._client_manager_create_client(
            TEST_BLE_ADDRESS,
            lambda _client: None,
            pair_on_connect=True,
            connect_timeout=TEST_CONNECT_TIMEOUT_SECONDS,
        )
        assert isinstance(created, DummyClient)
        assert create_calls[-1] == {}
        assert any("pair_on_connect" in call for call in create_calls[:-1])
        assert any("connect_timeout" in call for call in create_calls[:-1])

        connect_calls: list[dict[str, object]] = []

        def _connect_client(_client: object, **kwargs: object) -> None:
            connect_calls.append(dict(kwargs))
            if "timeout" in kwargs:
                raise TypeError(  # noqa: TRY003 - intentional fixture message shape
                    "connect_client() got an unexpected keyword argument 'timeout'"
                )

        orchestrator.client_manager = cast(
            Any, SimpleNamespace(connect_client=_connect_client)
        )
        orchestrator._client_manager_connect_client(
            cast(BLEClient, DummyClient()), timeout=1.0
        )
        assert connect_calls == [{"timeout": 1.0}, {}]


def test_connection_orchestrator_direct_and_retry_exception_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct/retry connect helpers should close clients on exceptional paths."""
    with _make_orchestrator(monkeypatch) as (_iface, orchestrator):
        closed_clients: list[object] = []
        orchestrator._client_manager_safe_close_client = cast(  # type: ignore[method-assign]
            Any, lambda client: closed_clients.append(client)
        )

        class _SimpleClient(DummyClient):
            pass

        created_client = _SimpleClient()
        created_client.address = TEST_BLE_ADDRESS
        created_client.bleak_client = SimpleNamespace(address=TEST_BLE_ADDRESS)
        orchestrator._client_manager_create_client = cast(  # type: ignore[method-assign]
            Any, lambda *_args, **_kwargs: created_client
        )
        orchestrator._client_manager_connect_client = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        orchestrator._raise_if_interface_closing = lambda: None  # type: ignore[method-assign]
        orchestrator._finalize_connection = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            _ for _ in ()
        ).throw(
            RuntimeError("finalize failed")
        )

        with pytest.raises(RuntimeError, match="finalize failed"):
            orchestrator._attempt_direct_connect(
                target_address=TEST_BLE_ADDRESS,
                explicit_address=True,
                normalized_target="aabbccddeeff",
                on_disconnect_func=lambda _client: None,
                pair_on_connect=False,
                direct_connect_timeout=1.0,
                register_notifications_func=lambda _client: None,
                on_connected_func=lambda: None,
                emit_connected_side_effects=True,
            )
        assert created_client in closed_clients

        closed_clients.clear()
        retry_client = _SimpleClient()
        orchestrator._client_manager_create_client = cast(  # type: ignore[method-assign]
            Any, lambda *_args, **_kwargs: retry_client
        )
        orchestrator._client_manager_connect_client = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            _ for _ in ()
        ).throw(
            RuntimeError("connect failed")
        )

        with pytest.raises(RuntimeError, match="connect failed"):
            orchestrator._connect_retry_target(
                connection_target=TEST_BLE_ADDRESS,
                resolved_address=TEST_BLE_ADDRESS,
                target_address=TEST_BLE_ADDRESS,
                skip_discovery_scan=False,
                on_disconnect_func=lambda _client: None,
                pair_on_connect=False,
                retry_connect_timeout=1.0,
            )
        assert retry_client in closed_clients

        orchestrator.interface = cast(Any, SimpleNamespace())
        with pytest.raises(AttributeError):
            orchestrator._compat_find_device(TEST_BLE_ADDRESS)


def test_compat_find_device_ignores_synthesized_preferred_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Declared legacy find-device hooks should beat synthesized preferred names."""

    class _LegacyFindDeviceInterface:
        def __getattr__(self, _name: str) -> object:
            return lambda _target: (_ for _ in ()).throw(
                AssertionError("dynamic find-device hook must be ignored")
            )

        def find_device(self, target: str | None) -> object:
            return SimpleNamespace(address=target)

    with _make_orchestrator(monkeypatch) as (_iface, orchestrator):
        orchestrator.interface = cast(Any, _LegacyFindDeviceInterface())
        result = orchestrator._compat_find_device(TEST_BLE_ADDRESS)

    assert result.address == TEST_BLE_ADDRESS


def test_attempt_direct_connect_allows_discovery_for_derived_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Derived address targets should not force skip_discovery_scan direct-only mode."""
    with _make_orchestrator(monkeypatch) as (_iface, orchestrator):
        direct_client = DummyClient()
        closed_clients: list[object] = []
        orchestrator._client_manager_create_client = cast(  # type: ignore[method-assign]
            Any, lambda *_args, **_kwargs: direct_client
        )
        orchestrator._client_manager_connect_client = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            _ for _ in ()
        ).throw(
            TimeoutError("direct fail")
        )
        orchestrator._client_manager_safe_close_client = cast(  # type: ignore[method-assign]
            Any, lambda client: closed_clients.append(client)
        )

        client, skip_discovery_scan = orchestrator._attempt_direct_connect(
            target_address=TEST_BLE_ADDRESS,
            explicit_address=False,
            normalized_target="aabbccddeeff",
            on_disconnect_func=lambda _client: None,
            pair_on_connect=False,
            direct_connect_timeout=1.0,
            register_notifications_func=lambda _client: None,
            on_connected_func=lambda: None,
            emit_connected_side_effects=True,
        )

        assert client is None
        assert skip_discovery_scan is False
        assert closed_clients == [direct_client]


def test_connection_orchestrator_finalize_and_establish_error_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finalize/establish helpers should cover invalid state and cleanup-on-failure branches."""
    with _make_orchestrator(monkeypatch) as (iface, orchestrator):
        orchestrator.state_manager = cast(
            Any,
            SimpleNamespace(
                current_state=ConnectionState.DISCONNECTED,
                transition_to=lambda _state: False,
            ),
        )

        with pytest.raises(iface.BLEError):
            orchestrator._finalize_connection(
                cast(BLEClient, DummyClient()),
                TEST_BLE_ADDRESS,
                lambda _client: None,
                lambda: None,
            )

        orchestrator._validator_validate_connection_request = lambda: None  # type: ignore[method-assign]
        orchestrator._raise_if_interface_closing = lambda: None  # type: ignore[method-assign]
        orchestrator._prepare_connection_target = lambda **_kwargs: ("AA", "aa", True)  # type: ignore[method-assign]
        orchestrator._resolve_connection_timeouts = lambda **_kwargs: (1.0, 1.0)  # type: ignore[method-assign]
        orchestrator._state_transition_to = cast(  # type: ignore[method-assign]
            Any, lambda _state: True
        )
        client = DummyClient()
        orchestrator._attempt_direct_connect = lambda **_kwargs: (None, False)  # type: ignore[method-assign]
        orchestrator._resolve_retry_target = lambda **_kwargs: ("AA", "AA", 1.0)  # type: ignore[method-assign]
        orchestrator._connect_retry_target = cast(  # type: ignore[method-assign]
            Any, lambda **_kwargs: (client, "AA")
        )
        orchestrator._finalize_connection = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            _ for _ in ()
        ).throw(
            RuntimeError("boom")
        )
        closed: list[object] = []
        orchestrator._client_manager_safe_close_client = cast(  # type: ignore[method-assign]
            Any, lambda c: closed.append(c)
        )
        orchestrator._transition_failure_to_disconnected = cast(  # type: ignore[method-assign]
            Any, lambda _ctx: None
        )

        with pytest.raises(RuntimeError, match="boom"):
            orchestrator._establish_connection(
                address="AA",
                current_address=None,
                register_notifications_func=lambda _client: None,
                on_connected_func=lambda: None,
                on_disconnect_func=lambda _client: None,
            )
        assert closed == [client]


def test_discovery_manager_typeerror_and_invalid_response_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discovery manager should invalidate cached clients on TypeError/invalid responses."""

    class _BadDiscoverClient:
        def __init__(self) -> None:
            self.bleak_client = object()

        @staticmethod
        def discover(**_kwargs: object) -> dict[str, Any]:
            raise TypeError("discovery invocation failed")

        @staticmethod
        def close() -> None:
            return None

    manager = DiscoveryManager(client_factory=lambda **_kwargs: _BadDiscoverClient())
    with pytest.raises(DiscoveryClientError):
        manager._discover_devices(address=None)

    class _InvalidResponseClient:
        def __init__(self) -> None:
            self.bleak_client = object()

        @staticmethod
        def discover(**_kwargs: object) -> object:
            return object()

        @staticmethod
        def close() -> None:
            return None

    manager_invalid = DiscoveryManager(
        client_factory=lambda **_kwargs: _InvalidResponseClient()
    )
    with pytest.raises(DiscoveryClientError):
        manager_invalid._discover_devices(address=None)


def test_client_error_handler_and_connection_state_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BLEClient helper branches should cover hook resolution, cleanup fallback, and service errors."""
    client = object.__new__(BLEClient)
    client.BLEError = BLEClient.BLEError

    client.error_handler = cast(Any, None)
    assert client._resolve_error_handler_hook("safe_cleanup", "_safe_cleanup") is None

    legacy_cleanup_calls: list[str] = []

    def _legacy_cleanup(func: Any, _name: str) -> None:
        legacy_cleanup_calls.append("called")
        func()

    client.error_handler = cast(
        Any,
        SimpleNamespace(
            safe_cleanup=None,
            _safe_cleanup=_legacy_cleanup,
        ),
    )
    client._error_handler_safe_cleanup(lambda: None, "cleanup")
    assert legacy_cleanup_calls == ["called"]

    client.error_handler = cast(
        Any, SimpleNamespace(safe_execute=lambda func, **_kwargs: func())
    )
    client.bleak_client = cast(Any, SimpleNamespace(is_connected=MagicMock()))
    assert client.isConnected() is False

    client.bleak_client = cast(
        Any, SimpleNamespace(is_connected=MagicMock(return_value=MagicMock()))
    )
    assert client.isConnected() is False

    class _BleakWithBrokenServices:
        @property
        def services(self) -> object:
            raise BleakError("not discovered")

    client._require_bleak_client = cast(  # type: ignore[method-assign]
        Any, lambda _error: _BleakWithBrokenServices()
    )
    with pytest.raises(BLEClient.BLEError):
        client._get_services()


def test_connection_validator_client_wrapper_and_manager_remaining_branches() -> None:
    """Connection validator/client manager should hit remaining wrapper/start/cleanup paths."""
    validator = ConnectionValidator(BLEStateManager(), RLock(), _TestBLEError)
    assert (
        validator.check_existing_client(
            cast(
                BLEClient, SimpleNamespace(is_connected=True, address=TEST_BLE_ADDRESS)
            ),
            None,
            None,
        )
        is True
    )
    assert (
        ConnectionValidator._client_is_connected(
            cast(BLEClient, SimpleNamespace(isConnected=MagicMock()))
        )
        is False
    )

    manager = ClientManager(
        BLEStateManager(), RLock(), cast(Any, SimpleNamespace()), BLEErrorHandler()
    )
    started: list[object] = []
    marker = cast(Any, SimpleNamespace(name="public-start"))
    manager.thread_coordinator = cast(
        Any, SimpleNamespace(start_thread=lambda thread: started.append(thread))
    )
    manager._thread_start_thread(marker)
    assert started == [marker]

    manager._thread_create_thread = cast(  # type: ignore[method-assign]
        Any,
        lambda **_kwargs: SimpleNamespace(ident=1, is_alive=lambda: True),
    )
    manager._thread_start_thread = cast(  # type: ignore[method-assign]
        Any, lambda _thread: (_ for _ in ()).throw(SystemExit())
    )
    manager._safe_close_client = MagicMock()  # type: ignore[method-assign]
    with pytest.raises(SystemExit):
        manager._update_client_reference(
            cast(BLEClient, DummyClient()), cast(BLEClient, DummyClient())
        )
    manager._safe_close_client.assert_called_once()

    manager_cleanup = ClientManager(
        BLEStateManager(), RLock(), cast(Any, SimpleNamespace()), BLEErrorHandler()
    )
    safe_client = DummyClient()
    safe_client.bleak_client = SimpleNamespace()
    safe_client.disconnect = lambda **_kwargs: None  # type: ignore[method-assign]
    safe_client.close = cast(Any, lambda: None)  # type: ignore[method-assign]
    manager_cleanup.error_handler = cast(
        Any,
        SimpleNamespace(
            _safe_cleanup=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                TypeError("non-kwarg typeerror")
            )
        ),
    )
    manager_cleanup._safe_close_client(cast(BLEClient, safe_client))

    failing_disconnect_client = DummyClient()
    failing_disconnect_client.bleak_client = SimpleNamespace()
    failing_disconnect_client.disconnect = lambda **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        RuntimeError("disconnect fail")
    )
    failing_disconnect_client.close = cast(Any, lambda: None)  # type: ignore[method-assign]
    manager_cleanup.error_handler = cast(Any, SimpleNamespace())
    manager_cleanup._safe_close_client(cast(BLEClient, failing_disconnect_client))

    runtime_hook_client = DummyClient()
    runtime_hook_client.bleak_client = SimpleNamespace()
    runtime_hook_client.disconnect = lambda **_kwargs: None  # type: ignore[method-assign]
    runtime_hook_client.close = cast(Any, lambda: None)  # type: ignore[method-assign]
    manager_cleanup.error_handler = cast(
        Any,
        SimpleNamespace(
            _safe_cleanup=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("hook runtime failure")
            )
        ),
    )
    manager_cleanup._safe_close_client(cast(BLEClient, runtime_hook_client))


def test_orchestrator_dispatch_and_event_remaining_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connection orchestrator dispatch helper should cover remaining guard branches."""
    with _make_orchestrator(monkeypatch) as (_iface, orchestrator):
        assert (
            orchestrator._dispatch_public_or_underscore(
                target=SimpleNamespace(),
                public_name="public",
                underscore_name="_legacy",
                call_member=False,
                default_if_missing="fallback",
            )
            == "fallback"
        )

        validator = ConnectionValidator(BLEStateManager(), RLock(), _TestBLEError)
        with pytest.raises(AttributeError):
            orchestrator._dispatch_public_or_underscore(
                target=validator,
                public_name="missing",
                underscore_name="_missing",
                prefer_instance_type=ConnectionValidator,
                call_member=True,
            )

        validator_non_callable = ConnectionValidator(
            BLEStateManager(), RLock(), _TestBLEError
        )
        validator_non_callable.validate_connection_request = cast(Any, 123)  # type: ignore[method-assign]
        with pytest.raises(AttributeError):
            orchestrator._dispatch_public_or_underscore(
                target=validator_non_callable,
                public_name="validate_connection_request",
                underscore_name="_validate_connection_request",
                prefer_instance_type=ConnectionValidator,
                call_member=True,
            )

        called: list[str] = []
        validator_callable = ConnectionValidator(
            BLEStateManager(), RLock(), _TestBLEError
        )
        validator_callable.validate_connection_request = lambda: called.append("called")  # type: ignore[method-assign]
        assert (
            orchestrator._dispatch_public_or_underscore(
                target=validator_callable,
                public_name="validate_connection_request",
                underscore_name="_validate_connection_request",
                prefer_instance_type=ConnectionValidator,
                call_member=True,
            )
            is None
        )
        assert called == ["called"]
        assert (
            orchestrator._dispatch_public_or_underscore(
                target=validator_callable,
                public_name="BLEError",
                underscore_name="_ble_error",
                prefer_instance_type=ConnectionValidator,
                call_member=False,
            )
            is _TestBLEError
        )

        with pytest.raises(AttributeError):
            orchestrator._dispatch_public_or_underscore(
                target=SimpleNamespace(),
                public_name="missing",
                underscore_name="_missing",
                call_member=False,
            )

        orchestrator.thread_coordinator = cast(
            Any,
            SimpleNamespace(
                set_event=lambda _name: (_ for _ in ()).throw(
                    RuntimeError("event failed")
                )
            ),
        )
        orchestrator._thread_set_event("evt")


def test_orchestrator_create_connect_direct_retry_remaining_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Create/connect/direct/retry helpers should hit remaining TypeError and cleanup branches."""
    with _make_orchestrator(monkeypatch) as (_iface, orchestrator):
        orchestrator.client_manager = cast(
            Any,
            SimpleNamespace(
                create_client=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    TypeError("plain typeerror")
                )
            ),
        )
        with pytest.raises(TypeError, match="plain typeerror"):
            orchestrator._client_manager_create_client(
                TEST_BLE_ADDRESS,
                lambda _client: None,
                pair_on_connect=True,
                connect_timeout=1.0,
            )

        orchestrator.client_manager = cast(
            Any,
            SimpleNamespace(
                connect_client=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    TypeError("plain typeerror")
                )
            ),
        )
        with pytest.raises(TypeError, match="plain typeerror"):
            orchestrator._client_manager_connect_client(
                cast(BLEClient, DummyClient()), timeout=1.0
            )

        direct_client = DummyClient()
        closed_clients: list[object] = []
        orchestrator._client_manager_create_client = cast(  # type: ignore[method-assign]
            Any, lambda *_args, **_kwargs: direct_client
        )
        orchestrator._client_manager_connect_client = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            _ for _ in ()
        ).throw(
            ValueError("direct fail")
        )
        orchestrator._client_manager_safe_close_client = cast(  # type: ignore[method-assign]
            Any, lambda client: closed_clients.append(client)
        )
        with pytest.raises(ValueError, match="direct fail"):
            orchestrator._attempt_direct_connect(
                target_address=TEST_BLE_ADDRESS,
                explicit_address=True,
                normalized_target="aabbccddeeff",
                on_disconnect_func=lambda _client: None,
                pair_on_connect=False,
                direct_connect_timeout=1.0,
                register_notifications_func=lambda _client: None,
                on_connected_func=lambda: None,
                emit_connected_side_effects=True,
            )
        assert direct_client in closed_clients

        retry_client = DummyClient()
        closed_clients.clear()
        orchestrator._client_manager_create_client = cast(  # type: ignore[method-assign]
            Any, lambda *_args, **_kwargs: retry_client
        )
        orchestrator._client_manager_connect_client = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            _ for _ in ()
        ).throw(
            SystemExit()
        )
        with pytest.raises(SystemExit):
            orchestrator._connect_retry_target(
                connection_target=TEST_BLE_ADDRESS,
                resolved_address=TEST_BLE_ADDRESS,
                target_address=TEST_BLE_ADDRESS,
                skip_discovery_scan=False,
                on_disconnect_func=lambda _client: None,
                pair_on_connect=False,
                retry_connect_timeout=1.0,
            )
        assert retry_client in closed_clients

        retry_client = DummyClient()
        closed_clients.clear()
        orchestrator._client_manager_create_client = cast(  # type: ignore[method-assign]
            Any, lambda *_args, **_kwargs: retry_client
        )
        orchestrator._client_manager_connect_client = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            _ for _ in ()
        ).throw(
            BleakError("retry fail")
        )
        with pytest.raises(BleakError, match="retry fail"):
            orchestrator._connect_retry_target(
                connection_target=TEST_BLE_ADDRESS,
                resolved_address=TEST_BLE_ADDRESS,
                target_address=TEST_BLE_ADDRESS,
                skip_discovery_scan=False,
                on_disconnect_func=lambda _client: None,
                pair_on_connect=False,
                retry_connect_timeout=1.0,
            )
        assert retry_client in closed_clients

        first_retry_client = DummyClient()
        closed_clients.clear()
        orchestrator._client_manager_create_client = cast(  # type: ignore[method-assign]
            Any, lambda *_args, **_kwargs: first_retry_client
        )

        def _connect_side_effect(*_args: object, **_kwargs: object) -> None:
            raise BleakDeviceNotFoundError("device not found")

        orchestrator._client_manager_connect_client = cast(  # type: ignore[method-assign]
            Any, _connect_side_effect
        )
        orchestrator._compat_find_device = cast(  # type: ignore[method-assign]
            Any,
            lambda _target: (_ for _ in ()).throw(
                AssertionError("explicit-address retry should not scan")
            ),
        )
        with pytest.raises(BleakDeviceNotFoundError):
            orchestrator._connect_retry_target(
                connection_target=TEST_BLE_ADDRESS,
                resolved_address=TEST_BLE_ADDRESS,
                target_address=TEST_BLE_ADDRESS,
                skip_discovery_scan=True,
                on_disconnect_func=lambda _client: None,
                pair_on_connect=False,
                retry_connect_timeout=1.0,
            )
        assert closed_clients == [first_retry_client]


def test_orchestrator_attempt_direct_connect_maps_dbus_error_to_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct-connect DBus failures should normalize to BLEDBusTransportError."""
    with _make_orchestrator(monkeypatch) as (iface, orchestrator):
        direct_client = DummyClient()
        orchestrator._client_manager_create_client = cast(  # type: ignore[method-assign]
            Any, lambda *_args, **_kwargs: direct_client
        )
        orchestrator._client_manager_connect_client = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            _ for _ in ()
        ).throw(
            BleakDBusError(
                "org.bluez.Error.InProgress",
                ["Device busy"],
            )
        )
        orchestrator._should_attempt_stale_bluez_cleanup = lambda **_kwargs: False  # type: ignore[method-assign]
        orchestrator._client_manager_safe_close_client = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

        with pytest.raises(BLEDBusTransportError) as exc_info:
            orchestrator._attempt_direct_connect(
                target_address=TEST_BLE_ADDRESS,
                explicit_address=True,
                normalized_target="aabbccddeeff",
                on_disconnect_func=lambda _client: None,
                pair_on_connect=False,
                direct_connect_timeout=1.0,
                register_notifications_func=lambda _client: None,
                on_connected_func=lambda: None,
                emit_connected_side_effects=True,
            )
        assert exc_info.value.requested_identifier == TEST_BLE_ADDRESS
        assert exc_info.value.dbus_error == "org.bluez.Error.InProgress"
        assert exc_info.value.dbus_error_details == "Device busy"
        assert exc_info.value.dbus_error_body == ("Device busy",)
        assert isinstance(exc_info.value.cause, BleakDBusError)
        assert isinstance(exc_info.value, iface.BLEError)


def test_orchestrator_attempt_direct_connect_maps_dbus_eof_to_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A closed dbus-fast transport should use the typed DBus error path."""
    with _make_orchestrator(monkeypatch) as (iface, orchestrator):
        direct_client = DummyClient()
        orchestrator._client_manager_create_client = cast(  # type: ignore[method-assign]
            Any, lambda *_args, **_kwargs: direct_client
        )
        orchestrator._client_manager_connect_client = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            _ for _ in ()
        ).throw(
            EOFError()
        )
        orchestrator._should_attempt_stale_bluez_cleanup = lambda **_kwargs: False  # type: ignore[method-assign]
        orchestrator._client_manager_safe_close_client = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

        with pytest.raises(BLEDBusTransportError) as exc_info:
            orchestrator._attempt_direct_connect(
                target_address=TEST_BLE_ADDRESS,
                explicit_address=True,
                normalized_target="aabbccddeeff",
                on_disconnect_func=lambda _client: None,
                pair_on_connect=False,
                direct_connect_timeout=1.0,
                register_notifications_func=lambda _client: None,
                on_connected_func=lambda: None,
                emit_connected_side_effects=True,
            )

        assert exc_info.value.requested_identifier == TEST_BLE_ADDRESS
        assert isinstance(exc_info.value.dbus_error, str)
        assert exc_info.value.dbus_error
        assert exc_info.value.dbus_error_body == ()
        assert isinstance(exc_info.value.cause, EOFError)
        assert isinstance(exc_info.value, iface.BLEError)


def test_orchestrator_retry_connect_maps_dbus_eof_to_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry connects should classify a closed D-Bus stream consistently."""
    with _make_orchestrator(monkeypatch) as (iface, orchestrator):
        retry_client = DummyClient()
        closed_clients: list[DummyClient] = []
        orchestrator._client_manager_create_client = cast(  # type: ignore[method-assign]
            Any, lambda *_args, **_kwargs: retry_client
        )
        orchestrator._client_manager_connect_client = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            _ for _ in ()
        ).throw(
            EOFError()
        )
        orchestrator._client_manager_safe_close_client = cast(  # type: ignore[method-assign]
            Any, lambda client: closed_clients.append(client)
        )

        with pytest.raises(BLEDBusTransportError) as exc_info:
            orchestrator._connect_retry_target(
                connection_target=TEST_BLE_ADDRESS,
                resolved_address=TEST_BLE_ADDRESS,
                target_address=TEST_BLE_ADDRESS,
                skip_discovery_scan=True,
                on_disconnect_func=lambda _client: None,
                pair_on_connect=False,
                retry_connect_timeout=1.0,
            )

        assert closed_clients == [retry_client]
        assert exc_info.value.requested_identifier == TEST_BLE_ADDRESS
        assert isinstance(exc_info.value.dbus_error, str)
        assert exc_info.value.dbus_error
        assert exc_info.value.dbus_error_body == ()
        assert isinstance(exc_info.value.cause, EOFError)
        assert isinstance(exc_info.value, iface.BLEError)


def test_orchestrator_stale_cleanup_retry_maps_dbus_eof_to_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-cleanup direct retry should normalize and clean up D-Bus EOF."""
    with _make_orchestrator(monkeypatch) as (iface, orchestrator):
        direct_client = DummyClient()
        retry_client = DummyClient()
        clients = iter((direct_client, retry_client))
        closed_clients: list[DummyClient] = []
        orchestrator._client_manager_create_client = cast(  # type: ignore[method-assign]
            Any,
            lambda *_args, **_kwargs: next(clients),
        )

        def _connect_client(client: DummyClient, **_kwargs: object) -> None:
            if client is direct_client:
                raise BleakDBusError(
                    "org.bluez.Error.InProgress",
                    ["Device or resource busy"],
                )
            raise EOFError()

        orchestrator._client_manager_connect_client = cast(  # type: ignore[method-assign]
            Any, _connect_client
        )
        orchestrator._client_manager_safe_close_client = cast(  # type: ignore[method-assign]
            Any, lambda client: closed_clients.append(client)
        )
        orchestrator._should_attempt_stale_bluez_cleanup = lambda **_kwargs: True  # type: ignore[method-assign]
        orchestrator._attempt_stale_bluez_cleanup = lambda **_kwargs: True  # type: ignore[method-assign]

        with pytest.raises(BLEDBusTransportError) as exc_info:
            orchestrator._attempt_direct_connect(
                target_address=TEST_BLE_ADDRESS,
                explicit_address=True,
                normalized_target="aabbccddeeff",
                on_disconnect_func=lambda _client: None,
                pair_on_connect=False,
                direct_connect_timeout=1.0,
                register_notifications_func=lambda _client: None,
                on_connected_func=lambda: None,
                emit_connected_side_effects=True,
            )

        assert closed_clients == [direct_client, retry_client]
        assert exc_info.value.requested_identifier == TEST_BLE_ADDRESS
        assert isinstance(exc_info.value.dbus_error, str)
        assert exc_info.value.dbus_error
        assert exc_info.value.dbus_error_body == ()
        assert isinstance(exc_info.value.cause, EOFError)
        assert isinstance(exc_info.value, iface.BLEError)


def test_orchestrator_attempt_direct_connect_uses_stale_cleanup_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct-connect failure should retry once after stale BlueZ cleanup."""
    with _make_orchestrator(monkeypatch) as (_iface, orchestrator):
        direct_client = DummyClient()
        retried_client = DummyClient()
        closed_clients: list[object] = []
        orchestrator._client_manager_create_client = cast(  # type: ignore[method-assign]
            Any, lambda *_args, **_kwargs: direct_client
        )
        orchestrator._client_manager_connect_client = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            _ for _ in ()
        ).throw(
            BleakDBusError("dbus", ["busy"])
        )
        orchestrator._client_manager_safe_close_client = cast(  # type: ignore[method-assign]
            Any, lambda client: closed_clients.append(client)
        )
        cleanup_attempts = 0
        retry_attempts = 0
        orchestrator._should_attempt_stale_bluez_cleanup = lambda **_kwargs: True  # type: ignore[method-assign]

        def _cleanup_once(**_kwargs: object) -> bool:
            nonlocal cleanup_attempts
            cleanup_attempts += 1
            return True

        def _retry_once(**_kwargs: object) -> DummyClient:
            nonlocal retry_attempts
            retry_attempts += 1
            return retried_client

        orchestrator._attempt_stale_bluez_cleanup = cast(  # type: ignore[method-assign]
            Any, _cleanup_once
        )
        orchestrator._retry_direct_connect_after_cleanup = cast(  # type: ignore[method-assign]
            Any, _retry_once
        )

        connected_client, skip_discovery_scan = orchestrator._attempt_direct_connect(
            target_address=TEST_BLE_ADDRESS,
            explicit_address=True,
            normalized_target="aabbccddeeff",
            on_disconnect_func=lambda _client: None,
            pair_on_connect=False,
            direct_connect_timeout=1.0,
            register_notifications_func=lambda _client: None,
            on_connected_func=lambda: None,
            emit_connected_side_effects=True,
        )

        assert connected_client is cast(BLEClient, retried_client)
        assert skip_discovery_scan is False
        assert closed_clients == [direct_client]
        assert cleanup_attempts == 1
        assert retry_attempts == 1


def test_orchestrator_attempt_stale_cleanup_forwards_disconnect_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale-cleanup client close should reuse the cleanup disconnect timeout budget."""
    with _make_orchestrator(monkeypatch) as (_iface, orchestrator):
        cleanup_client = DummyClient()
        observed_timeout: list[float | None] = []
        connect_calls: list[tuple[object, float | None]] = []
        orchestrator._client_manager_create_client = cast(  # type: ignore[method-assign]
            Any, lambda *_args, **_kwargs: cleanup_client
        )
        orchestrator._client_manager_connect_client = cast(  # type: ignore[method-assign]
            Any, lambda client, timeout=None: connect_calls.append((client, timeout))
        )
        orchestrator._client_manager_safe_close_client = cast(  # type: ignore[method-assign]
            Any,
            lambda _client, disconnect_timeout=None: observed_timeout.append(
                disconnect_timeout
            ),
        )

        assert orchestrator._attempt_stale_bluez_cleanup(
            target_address=TEST_BLE_ADDRESS,
            on_disconnect_func=lambda _client: None,
            connect_timeout=2.0,
        )
        assert connect_calls == [(cleanup_client, 2.0)]
        assert cleanup_client.disconnect_calls == 1
        assert observed_timeout == [min(2.0, DISCONNECT_TIMEOUT_SECONDS)]


def test_orchestrator_attempt_stale_cleanup_connect_failure_is_best_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale-cleanup connect failure should return False and still safe-close."""
    with _make_orchestrator(monkeypatch) as (_iface, orchestrator):
        cleanup_client = DummyClient()
        closed_clients: list[object] = []
        orchestrator._client_manager_create_client = cast(  # type: ignore[method-assign]
            Any, lambda *_args, **_kwargs: cleanup_client
        )

        def _fail_connect(_client: object, *, timeout: float | None = None) -> None:
            del timeout
            raise TimeoutError("cleanup connect timed out")

        orchestrator._client_manager_connect_client = cast(  # type: ignore[method-assign]
            Any, _fail_connect
        )
        orchestrator._client_manager_safe_close_client = cast(  # type: ignore[method-assign]
            Any,
            lambda client, disconnect_timeout=None: closed_clients.append(client),
        )

        assert not orchestrator._attempt_stale_bluez_cleanup(
            target_address=TEST_BLE_ADDRESS,
            on_disconnect_func=lambda _client: None,
            connect_timeout=2.0,
        )
        assert cleanup_client.disconnect_calls == 0
        assert closed_clients == [cleanup_client]


def test_orchestrator_attempt_stale_cleanup_safe_close_runs_after_disconnect_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale-cleanup safe close should run even when disconnect fails."""
    with _make_orchestrator(monkeypatch) as (_iface, orchestrator):
        cleanup_client = DummyClient(
            disconnect_exception=BleakError("disconnect failed")
        )
        closed_clients: list[object] = []
        orchestrator._client_manager_create_client = cast(  # type: ignore[method-assign]
            Any, lambda *_args, **_kwargs: cleanup_client
        )
        orchestrator._client_manager_connect_client = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        orchestrator._client_manager_safe_close_client = cast(  # type: ignore[method-assign]
            Any,
            lambda client, disconnect_timeout=None: closed_clients.append(client),
        )

        assert not orchestrator._attempt_stale_bluez_cleanup(
            target_address=TEST_BLE_ADDRESS,
            on_disconnect_func=lambda _client: None,
            connect_timeout=2.0,
        )
        assert cleanup_client.disconnect_calls == 1
        assert closed_clients == [cleanup_client]


def test_orchestrator_stale_bluez_error_classifier_prefers_specific_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale BlueZ classifier should key off BlueZ-specific names/details, not generic busy wording."""
    with _make_orchestrator(monkeypatch) as (_iface, orchestrator):
        assert orchestrator._is_stale_bluez_direct_connect_error(
            BleakDBusError(
                "org.bluez.Error.AlreadyConnected",
                ["Already connected"],
            )
        )
        assert orchestrator._is_stale_bluez_direct_connect_error(
            BleakDBusError(
                "org.bluez.Error.InProgress",
                ["Operation already in progress"],
            )
        )
        assert not orchestrator._is_stale_bluez_direct_connect_error(
            RuntimeError("adapter busy waiting for unrelated operation")
        )
        assert not orchestrator._is_stale_bluez_direct_connect_error(
            RuntimeError("operation in progress without BlueZ context")
        )


def test_orchestrator_stale_bluez_error_classifier_walks_wrapped_causes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale BlueZ classifier should inspect nested cause/context chains."""
    with _make_orchestrator(monkeypatch) as (_iface, orchestrator):
        stale = BleakDBusError(
            "org.bluez.Error.AlreadyConnected",
            ["Already connected"],
        )
        mid = RuntimeError("wrapper")
        mid.__cause__ = stale
        outer = RuntimeError("outer")
        outer.__context__ = mid

        assert orchestrator._is_stale_bluez_direct_connect_error(outer)


def test_orchestrator_stale_bluez_error_classifier_rejects_non_stale_wrappers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wrapped non-BlueZ errors should not be classified as stale BlueZ state."""
    with _make_orchestrator(monkeypatch) as (_iface, orchestrator):
        inner = RuntimeError("adapter busy")
        outer = RuntimeError("operation in progress without BlueZ context")
        outer.__cause__ = inner

        assert not orchestrator._is_stale_bluez_direct_connect_error(outer)


def test_orchestrator_attempt_direct_connect_rejects_explicit_address_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit-address direct connect should fail when connected peer mismatches."""
    with _make_orchestrator(monkeypatch) as (iface, orchestrator):
        mismatch_client = DummyClient()
        mismatch_client.address = "11:22:33:44:55:66"
        mismatch_client.bleak_client = SimpleNamespace(address="11:22:33:44:55:66")
        closed_clients: list[object] = []
        orchestrator._client_manager_create_client = cast(  # type: ignore[method-assign]
            Any, lambda *_args, **_kwargs: mismatch_client
        )
        orchestrator._client_manager_connect_client = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        orchestrator._client_manager_safe_close_client = cast(  # type: ignore[method-assign]
            Any, lambda client: closed_clients.append(client)
        )
        orchestrator._finalize_connection = MagicMock()  # type: ignore[method-assign]

        with pytest.raises(BLEAddressMismatchError) as exc_info:
            orchestrator._attempt_direct_connect(
                target_address=TEST_BLE_ADDRESS,
                explicit_address=True,
                normalized_target="aabbccddeeff",
                on_disconnect_func=lambda _client: None,
                pair_on_connect=False,
                direct_connect_timeout=1.0,
                register_notifications_func=lambda _client: None,
                on_connected_func=lambda: None,
                emit_connected_side_effects=True,
            )
        assert exc_info.value.requested_identifier == TEST_BLE_ADDRESS
        assert exc_info.value.connected_address == "11:22:33:44:55:66"
        assert isinstance(exc_info.value, iface.BLEError)
        assert closed_clients == [mismatch_client]
        orchestrator._finalize_connection.assert_not_called()


def test_orchestrator_validate_explicit_address_allows_unknown_connected_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit-address verification remains permissive when address is unavailable."""
    with _make_orchestrator(monkeypatch) as (_iface, orchestrator):
        unknown_client = DummyClient()
        unknown_client.address = cast(Any, None)
        unknown_client.bleak_client = SimpleNamespace(address=None)

        orchestrator._validate_explicit_address_connection(
            client=cast(BLEClient, unknown_client),
            target_address=TEST_BLE_ADDRESS,
            explicit_address=True,
        )


def test_orchestrator_retry_target_timeout_and_discovery_typing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry connect timeouts and discovery misses should map to typed BLE errors."""
    with _make_orchestrator(monkeypatch) as (iface, orchestrator):
        retry_client = DummyClient()
        orchestrator._client_manager_create_client = cast(  # type: ignore[method-assign]
            Any, lambda *_args, **_kwargs: retry_client
        )
        orchestrator._client_manager_connect_client = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            _ for _ in ()
        ).throw(
            TimeoutError("timed out")
        )
        orchestrator._client_manager_safe_close_client = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

        with pytest.raises(BLEConnectionTimeoutError) as timeout_exc:
            orchestrator._connect_retry_target(
                connection_target=TEST_BLE_ADDRESS,
                resolved_address=TEST_BLE_ADDRESS,
                target_address=TEST_BLE_ADDRESS,
                skip_discovery_scan=True,
                on_disconnect_func=lambda _client: None,
                pair_on_connect=False,
                retry_connect_timeout=2.5,
            )
        assert timeout_exc.value.requested_identifier == TEST_BLE_ADDRESS
        assert timeout_exc.value.timeout == 2.5
        assert isinstance(timeout_exc.value, TimeoutError)
        assert isinstance(timeout_exc.value, iface.BLEError)

        orchestrator._compat_find_device = cast(  # type: ignore[method-assign]
            Any,
            lambda _target: (_ for _ in ()).throw(BleakDeviceNotFoundError("missing")),
        )
        with pytest.raises(BLEDeviceNotFoundError):
            orchestrator._resolve_retry_target(
                target_address="name-only",
                skip_discovery_scan=False,
                direct_connect_timeout=1.0,
                discovery_connect_timeout=3.0,
            )


def test_establish_connection_preserves_typed_timeout_without_rewrapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_establish_connection should not wrap BLEConnectionTimeoutError a second time."""
    with _make_orchestrator(monkeypatch) as (iface, orchestrator):
        timeout_error = BLEConnectionTimeoutError(
            "timed out",
            requested_identifier=TEST_BLE_ADDRESS,
            timeout=1.5,
        )
        orchestrator._validator_validate_connection_request = lambda: None  # type: ignore[method-assign]
        orchestrator._raise_if_interface_closing = lambda: None  # type: ignore[method-assign]
        orchestrator._prepare_connection_target = lambda **_kwargs: (  # type: ignore[method-assign]
            TEST_BLE_ADDRESS,
            "aabbccddeeff",
            True,
        )
        orchestrator._resolve_connection_timeouts = lambda **_kwargs: (1.5, 1.5)  # type: ignore[method-assign]
        orchestrator._state_transition_to = cast(  # type: ignore[method-assign]
            Any, lambda _state: True
        )
        orchestrator._attempt_direct_connect = lambda **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
            timeout_error
        )
        orchestrator._transition_failure_to_disconnected = cast(  # type: ignore[method-assign]
            Any, lambda _ctx: None
        )
        orchestrator._client_manager_safe_close_client = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

        with pytest.raises(BLEConnectionTimeoutError) as exc_info:
            orchestrator._establish_connection(
                address=TEST_BLE_ADDRESS,
                current_address=None,
                register_notifications_func=lambda _client: None,
                on_connected_func=lambda: None,
                on_disconnect_func=lambda _client: None,
            )

        assert exc_info.value is timeout_error
        assert isinstance(exc_info.value, TimeoutError)
        assert isinstance(exc_info.value, iface.BLEError)


def test_orchestrator_finalize_and_establish_remaining_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finalize/establish paths should cover remaining invalidation and cleanup branches."""
    with _make_orchestrator(monkeypatch) as (iface, orchestrator):
        connected_client = SimpleNamespace(isConnected=lambda: True)

        state_values = iter([ConnectionState.CONNECTING, ConnectionState.DISCONNECTED])
        orchestrator._state_current_state = lambda: next(state_values)  # type: ignore[method-assign]
        with pytest.raises(iface.BLEError):
            orchestrator._finalize_connection(
                cast(BLEClient, connected_client),
                TEST_BLE_ADDRESS,
                lambda _client: None,
                lambda: None,
            )

        orchestrator._state_current_state = lambda: ConnectionState.CONNECTING  # type: ignore[method-assign]
        monkeypatch.setattr(
            ConnectionValidator,
            "_client_is_connected",
            staticmethod(lambda _client: False),
        )
        with pytest.raises(iface.BLEError):
            orchestrator._finalize_connection(
                cast(BLEClient, connected_client),
                TEST_BLE_ADDRESS,
                lambda _client: None,
                lambda: None,
            )

        monkeypatch.setattr(
            ConnectionValidator,
            "_client_is_connected",
            staticmethod(lambda _client: True),
        )
        orchestrator._state_transition_to = cast(  # type: ignore[method-assign]
            Any, lambda _state: False
        )
        with pytest.raises(iface.BLEError):
            orchestrator._finalize_connection(
                cast(BLEClient, connected_client),
                TEST_BLE_ADDRESS,
                lambda _client: None,
                lambda: None,
            )

        retry_client = DummyClient()
        closed_clients: list[object] = []
        orchestrator._validator_validate_connection_request = lambda: None  # type: ignore[method-assign]
        orchestrator._raise_if_interface_closing = lambda: None  # type: ignore[method-assign]
        orchestrator._prepare_connection_target = lambda **_kwargs: ("AA", "aa", True)  # type: ignore[method-assign]
        orchestrator._resolve_connection_timeouts = lambda **_kwargs: (1.0, 1.0)  # type: ignore[method-assign]
        orchestrator._state_transition_to = cast(  # type: ignore[method-assign]
            Any, lambda _state: True
        )
        orchestrator._attempt_direct_connect = lambda **_kwargs: (None, False)  # type: ignore[method-assign]
        orchestrator._resolve_retry_target = lambda **_kwargs: ("AA", "AA", 1.0)  # type: ignore[method-assign]
        orchestrator._connect_retry_target = cast(  # type: ignore[method-assign]
            Any, lambda **_kwargs: (retry_client, "AA")
        )
        orchestrator._client_manager_safe_close_client = cast(  # type: ignore[method-assign]
            Any, lambda client: closed_clients.append(client)
        )
        orchestrator._transition_failure_to_disconnected = cast(  # type: ignore[method-assign]
            Any, lambda _ctx: None
        )
        orchestrator._finalize_connection = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            _ for _ in ()
        ).throw(
            BleakDBusError("dbus", ["err"])
        )

        with pytest.raises(BleakDBusError):
            orchestrator._establish_connection(
                address="AA",
                current_address=None,
                register_notifications_func=lambda _client: None,
                on_connected_func=lambda: None,
                on_disconnect_func=lambda _client: None,
            )
        assert closed_clients == [retry_client]

        closed_clients.clear()
        orchestrator._finalize_connection = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            _ for _ in ()
        ).throw(
            SystemExit()
        )
        with pytest.raises(SystemExit):
            orchestrator._establish_connection(
                address="AA",
                current_address=None,
                register_notifications_func=lambda _client: None,
                on_connected_func=lambda: None,
                on_disconnect_func=lambda _client: None,
            )
        assert closed_clients == [retry_client]


def test_discovery_probe_and_factory_kwarg_rejection_branches() -> None:
    """Discovery probing/factory fallback should cover probe errors and kwarg-rejection logging."""

    class _GoodDiscoveryClient:
        def __init__(self) -> None:
            self.bleak_client = object()

        @staticmethod
        def discover(**_kwargs: object) -> dict[str, Any]:
            return {}

        @staticmethod
        def close() -> None:
            return None

    manager_probe_error = DiscoveryManager(
        client_factory=lambda **_kwargs: _GoodDiscoveryClient()
    )
    manager_probe_error._client = SimpleNamespace(
        bleak_client=object(),
        isConnected=lambda: (_ for _ in ()).throw(RuntimeError("probe failure")),
        close=lambda: None,
    )
    assert manager_probe_error._discover_devices(address=None) == []

    manager_bool_member = DiscoveryManager(
        client_factory=lambda **_kwargs: _GoodDiscoveryClient()
    )
    manager_bool_member._client = SimpleNamespace(
        bleak_client=object(),
        is_connected=False,
        close=lambda: None,
    )
    assert manager_bool_member._discover_devices(address=None) == []

    def _factory_rejecting_kwarg(**kwargs: object) -> _GoodDiscoveryClient:
        if kwargs:
            raise TypeError(  # noqa: TRY003 - intentional fixture message shape
                "factory() got an unexpected keyword argument 'log_if_no_address'"
            )
        return _GoodDiscoveryClient()

    manager_kwarg_reject = DiscoveryManager(client_factory=_factory_rejecting_kwarg)
    assert manager_kwarg_reject._discover_devices(address=None) == []


def test_discovery_missing_discover_and_typeerror_rejection_branches() -> None:
    """Discovery should invalidate clients that lack discover hooks or reject expected kwargs."""

    class _FlakyDiscoverClient:
        def __init__(self) -> None:
            self._discover_reads = 0
            self.bleak_client = object()

        @property
        def discover(self) -> object | None:
            self._discover_reads += 1
            if self._discover_reads == 1:
                return lambda **_kwargs: {}
            return None

        @staticmethod
        def close() -> None:
            return None

    manager_missing = DiscoveryManager(
        client_factory=lambda **_kwargs: _FlakyDiscoverClient()
    )
    with pytest.raises(DiscoveryClientError):
        manager_missing._discover_devices(address=None)

    class _MissingDiscoverClient:
        def __init__(self) -> None:
            self.bleak_client = object()

        @staticmethod
        def close() -> None:
            return None

    class _DiscoveryLikeClient:
        def __init__(self) -> None:
            self.bleak_client = object()

        @staticmethod
        def discover(**_kwargs: object) -> dict[str, Any]:
            return {}

        @staticmethod
        def close() -> None:
            return None

    class _Line567Manager(DiscoveryManager):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._stored_client: object | None = None
            self._post_set_reads_remaining = 0
            super().__init__(*args, **kwargs)

        @property  # type: ignore[override]
        def _client(self) -> object | None:
            if self._post_set_reads_remaining > 0:
                self._post_set_reads_remaining -= 1
                return self._stored_client
            return None

        @_client.setter
        def _client(self, value: object | None) -> None:
            self._stored_client = value
            if value is None:
                self._post_set_reads_remaining = 0
            else:
                # Keep three post-assignment reads visible for validation, then hide.
                # Read sequence after factory assignment:
                # 1) None-check at line 543
                # 2) isinstance check at line 548
                # 3) _is_discovery_client_like check at line 548
                self._post_set_reads_remaining = 3

    manager_line567 = _Line567Manager(
        client_factory=lambda **_kwargs: _DiscoveryLikeClient()
    )
    with pytest.raises(DiscoveryClientError):
        manager_line567._discover_devices(address=None)

    class _UnexpectedKwargClient:
        def __init__(self) -> None:
            self.bleak_client = object()

        @staticmethod
        def discover(**_kwargs: object) -> dict[str, Any]:
            raise TypeError(  # noqa: TRY003 - intentional fixture message shape
                "discover() got an unexpected keyword argument 'return_adv'"
            )

        @staticmethod
        def close() -> None:
            return None

    manager_kwarg = DiscoveryManager(
        client_factory=lambda **_kwargs: _UnexpectedKwargClient()
    )
    with pytest.raises(DiscoveryClientError):
        manager_kwarg._discover_devices(address=None)


def test_discovery_awaitable_and_exception_handler_branches() -> None:
    """Discovery should handle unresolved awaitables and classify scan failures."""

    async def _discover_coro() -> dict[str, Any]:
        return {}

    class _AwaitableDiscoveryClient:
        def __init__(self) -> None:
            self.bleak_client = object()

        @staticmethod
        def discover(**_kwargs: object) -> Any:
            return _discover_coro()

        @staticmethod
        def close() -> None:
            return None

    manager_awaitable = DiscoveryManager(
        client_factory=lambda **_kwargs: _AwaitableDiscoveryClient()
    )
    with pytest.raises(DiscoveryClientError):
        manager_awaitable._discover_devices(address=None)

    class _AwaitBridgeDiscoveryClient:
        def __init__(self) -> None:
            self.bleak_client = object()

        @staticmethod
        def discover(**_kwargs: object) -> Any:
            return _discover_coro()

        @staticmethod
        def async_await(awaitable: Any) -> dict[str, Any]:
            # Resolve the awaitable through compatibility bridge path.
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
            return {}

        @staticmethod
        def close() -> None:
            return None

    manager_await_bridge = DiscoveryManager(
        client_factory=lambda **_kwargs: _AwaitBridgeDiscoveryClient()
    )
    assert manager_await_bridge._discover_devices(address=None) == []

    class _DbusErrorDiscoveryClient:
        def __init__(self) -> None:
            self.bleak_client = object()

        @staticmethod
        def discover(**_kwargs: object) -> dict[str, Any]:
            raise BleakDBusError("dbus", ["err"])

        @staticmethod
        def close() -> None:
            return None

    manager_dbus = DiscoveryManager(
        client_factory=lambda **_kwargs: _DbusErrorDiscoveryClient()
    )
    with pytest.raises(BleakDBusError):
        manager_dbus._discover_devices(address=None)

    class _RuntimeErrorDiscoveryClient:
        def __init__(self) -> None:
            self.bleak_client = object()

        @staticmethod
        def discover(**_kwargs: object) -> dict[str, Any]:
            raise RuntimeError("runtime failure")

        @staticmethod
        def close() -> None:
            return None

    manager_runtime = DiscoveryManager(
        client_factory=lambda **_kwargs: _RuntimeErrorDiscoveryClient()
    )
    assert manager_runtime._discover_devices(address=None) == []

    class _ValueErrorDiscoveryClient:
        def __init__(self) -> None:
            self.bleak_client = object()

        @staticmethod
        def discover(**_kwargs: object) -> dict[str, Any]:
            raise ValueError("unexpected failure")

        @staticmethod
        def close() -> None:
            return None

    manager_value = DiscoveryManager(
        client_factory=lambda **_kwargs: _ValueErrorDiscoveryClient()
    )
    assert manager_value._discover_devices(address=None) == []


def test_bleclient_remaining_hook_and_connection_branches() -> None:
    """BLEClient helper methods should cover remaining hook/connection/property branches."""
    client = object.__new__(BLEClient)
    client.BLEError = BLEClient.BLEError
    client.error_handler = cast(
        Any, SimpleNamespace(safe_cleanup=object(), _safe_cleanup=object())
    )
    assert client._resolve_error_handler_hook("safe_cleanup", "_safe_cleanup") is None

    client.error_handler = cast(
        Any,
        SimpleNamespace(
            safe_cleanup=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                TypeError("non-keyword type error")
            )
        ),
    )
    client._error_handler_safe_cleanup(lambda: None, "cleanup-typeerror")

    client.error_handler = cast(
        Any,
        SimpleNamespace(
            safe_cleanup=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("runtime cleanup hook failure")
            )
        ),
    )
    client._error_handler_safe_cleanup(lambda: None, "cleanup-runtimeerror")

    def _failing_safe_cleanup(cleanup: Any, *args: object, **kwargs: object) -> None:
        if "cleanup_name" in kwargs:
            raise TypeError(  # noqa: TRY003 - intentional fixture message shape
                "safe_cleanup() got an unexpected keyword argument 'cleanup_name'"
            )
        _ = args
        cleanup()
        raise RuntimeError("positional cleanup failure")

    client.error_handler = cast(
        Any, SimpleNamespace(safe_cleanup=_failing_safe_cleanup)
    )
    client._error_handler_safe_cleanup(
        lambda: (_ for _ in ()).throw(RuntimeError("cleanup fallback failure")),
        "cleanup-op",
    )

    client.error_handler = cast(
        Any, SimpleNamespace(safe_execute=lambda func, **_kwargs: func())
    )
    client.bleak_client = cast(Any, SimpleNamespace(is_connected=MagicMock()))
    assert client.isConnected() is False

    client.bleak_client = cast(
        Any, SimpleNamespace(is_connected=MagicMock(return_value=MagicMock()))
    )
    assert client.isConnected() is False

    class _BrokenServices:
        @property
        def services(self) -> object:
            raise BleakError("services missing")

    client._require_bleak_client = cast(  # type: ignore[method-assign]
        Any, lambda _message: _BrokenServices()
    )
    with pytest.raises(BLEClient.BLEError):
        client._get_services()


def test_discovery_cached_client_ignores_synthesized_connectivity_probe() -> None:
    """A dynamic public probe must not override a declared connected-state member."""

    class _CachedDiscoveryClient:
        bleak_client = object()
        is_connected = True

        def __init__(self) -> None:
            self.discover_calls = 0

        def discover(self, **_kwargs: object) -> dict[str, object]:
            self.discover_calls += 1
            return {}

        def __getattr__(self, name: str) -> object:
            if name == "isConnected":
                return lambda: False
            raise AttributeError(name)

    cached = _CachedDiscoveryClient()
    manager = DiscoveryManager(
        client_factory=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("connected cached client should be reused")
        )
    )
    manager._client = cached

    assert manager._discover_devices(None) == []
    assert manager._client is cached
    assert cached.discover_calls == 1
