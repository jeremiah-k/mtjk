"""BLE discovery and compatibility helper tests."""

# pylint: disable=redefined-outer-name

import asyncio
import logging
import threading
from types import SimpleNamespace
from typing import Any, Callable, cast
from unittest.mock import MagicMock

import pytest

# Import meshtastic modules for use in tests
import meshtastic.interfaces.ble as ble_mod
import meshtastic.interfaces.ble.discovery as discovery_mod
from meshtastic.interfaces.ble import (
    SERVICE_UUID,
    BLEClient,
    BLEInterface,
)
from meshtastic.interfaces.ble.connection import ConnectionValidator
from meshtastic.interfaces.ble.discovery import (
    DiscoveryClientError,
    DiscoveryManager,
    _close_discovery_client_best_effort,
    _filter_devices_for_target_identifier,
    _looks_like_ble_address,
    _parse_scan_response,
)
from meshtastic.interfaces.ble.notifications import (
    BLENotificationDispatcher,
    NotificationManager,
)
from meshtastic.interfaces.ble.state import BLEStateManager, ConnectionState
from tests._ble_interface_core_support import (
    SAFE_EXECUTE_CALLABLE_ONLY_ERROR_MSG,
    SAFE_EXECUTE_DIFFERENT_TYPE_ERROR_MSG,
    SAFE_EXECUTE_HANDLER_TYPE_ERROR_MSG,
    SAFE_EXECUTE_KEYWORD_CALL_FAILED_MSG,
    SAFE_EXECUTE_POSITIONAL_MISMATCH_ERROR_MSG,
    SAFE_EXECUTE_UNEXPECTED_ERROR_MSG,
    _create_ble_device,
    _FakeDiscoveryClient,
    _ImmediateThread,
    _make_send_message_recorder,
)

# Import common fixtures
from tests.test_ble_interface_fixtures import DummyClient, _build_interface

pytestmark = pytest.mark.unit


def test_find_device_multiple_matches_raises() -> None:
    """Providing an address that matches multiple devices should raise BLEError."""
    # BLEDevice and BLEInterface already imported at top as ble_mod.BLEDevice, ble_mod.BLEInterface

    # Intentional constructor bypass for isolated findDevice() behavior.
    iface = object.__new__(ble_mod.BLEInterface)
    devices = [
        _create_ble_device(address="AA:BB:CC:DD:EE:FF", name="Meshtastic-1"),
        _create_ble_device(address="AA-BB-CC-DD-EE-FF", name="Meshtastic-2"),
    ]
    iface._discovery_manager = SimpleNamespace(_discover_devices=lambda _addr: devices)  # type: ignore[assignment]

    with pytest.raises(BLEInterface.BLEError) as excinfo:
        BLEInterface.findDevice(iface, "aa bb cc dd ee ff")

    assert "Multiple Meshtastic BLE peripherals found matching" in str(excinfo.value)
    assert excinfo.value.kind == BLEInterface.BLEError.MULTIPLE_DEVICES


def test_find_device_direct_connect_preserves_raw_address() -> None:
    """Direct-connect fallback should keep the raw BLE address format."""
    iface = object.__new__(ble_mod.BLEInterface)
    iface._discovery_manager = SimpleNamespace(_discover_devices=lambda _addr: [])  # type: ignore[assignment]

    address = "AA:BB:CC:DD:EE:FF"
    direct_device = BLEInterface.findDevice(iface, address)

    assert direct_device.address == address
    assert direct_device.name == address


def test_find_device_direct_connect_without_discovery_manager() -> None:
    """Verify direct-connect fallback works when discovery manager is missing.

    Returns
    -------
    None
    """
    iface = object.__new__(ble_mod.BLEInterface)
    iface._discovery_manager = None

    address = "AA:BB:CC:DD:EE:FF"
    direct_device = BLEInterface.findDevice(iface, address)

    assert direct_device.address == address
    assert direct_device.name == address


def test_handle_malformed_fromnum_warns_at_threshold_and_resets_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed FROMNUM counter should emit warning at threshold then reset."""
    from meshtastic.interfaces.ble.interface import MALFORMED_NOTIFICATION_THRESHOLD

    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    warning_calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.logger.warning",
        lambda *args, **_kwargs: warning_calls.append(args),
    )
    try:
        with iface._malformed_notification_lock:
            iface._malformed_notification_count = MALFORMED_NOTIFICATION_THRESHOLD - 1

        iface._handle_malformed_fromnum("bad notification")

        assert len(warning_calls) == 1
        with iface._malformed_notification_lock:
            assert iface._malformed_notification_count == 0

        iface._handle_malformed_fromnum("bad notification")
        assert len(warning_calls) == 1
    finally:
        iface.close()


def test_report_notification_handler_error_covers_hook_and_fallback_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Notification error reporting should handle hook failures and missing hooks."""
    iface = object.__new__(BLEInterface)
    debug_calls: list[str] = []
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.logger.debug",
        lambda message, *_args, **_kwargs: debug_calls.append(cast(str, message)),
    )

    raising_hook = MagicMock(side_effect=RuntimeError("hook failed"))
    iface.error_handler = cast(
        Any, SimpleNamespace(handle_unhandled_exception=raising_hook)
    )
    iface._report_notification_handler_error("hook-error")

    # Unconfigured child mock should be treated as unavailable and fall back to logger.debug.
    error_handler = MagicMock()
    iface.error_handler = error_handler
    iface._report_notification_handler_error("missing-hook")

    raising_hook.assert_called_once_with("hook-error")
    error_handler.handle_unhandled_exception.assert_not_called()
    assert "hook-error" in debug_calls


def test_invoke_safe_execute_compat_skips_callable_only_after_positional_failure() -> (
    None
):
    """Positional safe_execute failures should not trigger a second handler invocation."""

    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    handler_runs: list[str] = []
    fallbacks: list[str] = []

    def _handler_thunk() -> None:
        handler_runs.append("run")
        raise RuntimeError("handler failed")

    def _fallback() -> None:
        fallbacks.append("fallback")

    def _legacy_safe_execute(
        func: Callable[[], None], *args: object, **kwargs: object
    ) -> None:
        calls.append((args, dict(kwargs)))
        if "error_msg" in kwargs:
            raise TypeError(SAFE_EXECUTE_UNEXPECTED_ERROR_MSG)
        func()

    BLEInterface._invoke_safe_execute_compat(
        _legacy_safe_execute,
        _handler_thunk,
        error_msg="notification error",
        fallback=_fallback,
    )

    assert handler_runs == ["run"]
    assert fallbacks == []
    assert len(calls) == 2


def test_invoke_safe_execute_compat_tries_callable_only_after_positional_signature_error() -> (
    None
):
    """Positional signature mismatch should continue to callable-only compatibility probe."""

    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    handler_runs: list[str] = []
    fallbacks: list[str] = []

    def _handler_thunk() -> None:
        handler_runs.append("run")

    def _fallback() -> None:
        fallbacks.append("fallback")

    def _legacy_safe_execute(
        func: Callable[[], None], *args: object, **kwargs: object
    ) -> None:
        calls.append((args, dict(kwargs)))
        if "error_msg" in kwargs:
            raise TypeError(SAFE_EXECUTE_UNEXPECTED_ERROR_MSG)
        if args:
            raise TypeError(SAFE_EXECUTE_POSITIONAL_MISMATCH_ERROR_MSG)
        func()

    BLEInterface._invoke_safe_execute_compat(
        _legacy_safe_execute,
        _handler_thunk,
        error_msg="notification error",
        fallback=_fallback,
    )

    assert handler_runs == ["run"]
    assert fallbacks == []
    assert len(calls) == 3


def test_invoke_safe_execute_compat_reports_handler_failure_after_execution() -> None:
    """Handler exceptions should be reported when compat safe_execute re-raises."""
    dispatcher = BLENotificationDispatcher(
        notification_manager=NotificationManager(),
        error_handler_provider=lambda: SimpleNamespace(),
        trigger_read_event=lambda: None,
        registration_current_provider=lambda _client, _epoch: True,
    )
    reported_errors: list[BaseException] = []
    fallback_runs: list[str] = []
    execution_order: list[str] = []

    def _handler() -> None:
        execution_order.append("handler")
        raise RuntimeError("handler boom")

    def _safe_execute(
        func: Callable[[], None], *_args: object, **_kwargs: object
    ) -> None:
        func()

    def _report_handler_error(exc: BaseException) -> None:
        execution_order.append("report")
        reported_errors.append(exc)

    dispatcher.invoke_safe_execute_compat(
        _safe_execute,
        _handler,
        error_msg="notification error",
        fallback=lambda: fallback_runs.append("fallback"),
        report_handler_error=_report_handler_error,
    )

    assert execution_order == ["handler", "report"]
    assert fallback_runs == []
    assert len(reported_errors) == 1
    assert isinstance(reported_errors[0], RuntimeError)


def test_invoke_safe_execute_compat_covers_keyword_positional_and_callable_only_paths() -> (
    None
):
    """safe_execute compatibility helper should cover success/fallback branches."""

    def _run_scenario(
        safe_execute: Callable[..., object],
        *,
        expect_handler_runs: int,
        expect_fallback_runs: int,
    ) -> None:
        handler_runs: list[str] = []
        fallback_runs: list[str] = []

        def _handler() -> None:
            handler_runs.append("run")

        def _fallback() -> None:
            fallback_runs.append("fallback")

        BLEInterface._invoke_safe_execute_compat(
            safe_execute,
            _handler,
            error_msg="notification error",
            fallback=_fallback,
        )

        assert len(handler_runs) == expect_handler_runs
        assert len(fallback_runs) == expect_fallback_runs

    _run_scenario(
        lambda func, *_args, **_kwargs: func(),
        expect_handler_runs=1,
        expect_fallback_runs=0,
    )

    _run_scenario(
        lambda _func, *_args, **_kwargs: None,
        expect_handler_runs=0,
        expect_fallback_runs=1,
    )

    def _keyword_typeerror(
        _func: Callable[[], None], *args: object, **kwargs: object
    ) -> None:
        _ = args
        _ = kwargs
        raise TypeError(SAFE_EXECUTE_DIFFERENT_TYPE_ERROR_MSG)

    _run_scenario(
        _keyword_typeerror,
        expect_handler_runs=0,
        expect_fallback_runs=1,
    )

    def _keyword_exception(
        _func: Callable[[], None], *args: object, **kwargs: object
    ) -> None:
        _ = args
        _ = kwargs
        raise RuntimeError(SAFE_EXECUTE_KEYWORD_CALL_FAILED_MSG)

    _run_scenario(
        _keyword_exception,
        expect_handler_runs=0,
        expect_fallback_runs=1,
    )

    def _positional_success(
        func: Callable[[], None], *args: object, **kwargs: object
    ) -> None:
        if "error_msg" in kwargs:
            raise TypeError(SAFE_EXECUTE_UNEXPECTED_ERROR_MSG)
        if args:
            func()
            return
        raise AssertionError("callable-only path should not execute")

    _run_scenario(
        _positional_success,
        expect_handler_runs=1,
        expect_fallback_runs=0,
    )

    def _non_signature_positional_typeerror(
        _func: Callable[[], None], *args: object, **kwargs: object
    ) -> None:
        if "error_msg" in kwargs:
            raise TypeError(SAFE_EXECUTE_UNEXPECTED_ERROR_MSG)
        if args:
            raise TypeError(SAFE_EXECUTE_HANDLER_TYPE_ERROR_MSG)
        raise AssertionError("callable-only path should not execute")

    _run_scenario(
        _non_signature_positional_typeerror,
        expect_handler_runs=0,
        expect_fallback_runs=1,
    )

    def _callable_only_exception(
        _func: Callable[[], None], *args: object, **kwargs: object
    ) -> None:
        if "error_msg" in kwargs:
            raise TypeError(SAFE_EXECUTE_UNEXPECTED_ERROR_MSG)
        if args:
            raise TypeError(SAFE_EXECUTE_POSITIONAL_MISMATCH_ERROR_MSG)
        raise RuntimeError(SAFE_EXECUTE_CALLABLE_ONLY_ERROR_MSG)

    _run_scenario(
        _callable_only_exception,
        expect_handler_runs=0,
        expect_fallback_runs=1,
    )


def test_wait_for_disconnect_notifications_skips_unconfigured_queuework(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify disconnect flush falls back to drain when queueWork is unconfigured.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Fixture used to patch publish queue draining behavior.

    Returns
    -------
    None
    """
    from meshtastic.interfaces.ble.compatibility_service import (
        BLECompatibilityEventService,
    )

    iface = SimpleNamespace(
        error_handler=SimpleNamespace(safe_execute=lambda func, **_kwargs: func())
    )
    publishing_thread = MagicMock()
    queue_work = publishing_thread.queueWork
    drained: list[bool] = []
    monkeypatch.setattr(
        BLECompatibilityEventService,
        "drain_publish_queue",
        lambda *_args, **_kwargs: drained.append(True),
        raising=True,
    )

    BLECompatibilityEventService.wait_for_disconnect_notifications(
        cast(Any, iface),
        timeout=0.01,
        publishing_thread=publishing_thread,
    )

    assert drained == [True]
    queue_work.assert_not_called()


def test_publish_connection_status_runs_async_fallback_when_queuework_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify status publish falls back asynchronously when queueWork/queue are unavailable.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Fixture used to patch publish transport.
    caplog : pytest.LogCaptureFixture
        Fixture used to assert debug log path.

    Returns
    -------
    None
    """
    from meshtastic import mesh_interface as mesh_iface_module
    from meshtastic.interfaces.ble import (
        compatibility_service as compatibility_service_mod,
    )
    from meshtastic.interfaces.ble.compatibility_service import (
        BLECompatibilityEventService,
    )

    sent: list[tuple[str, object, bool]] = []

    monkeypatch.setattr(
        mesh_iface_module,
        "pub",
        SimpleNamespace(sendMessage=_make_send_message_recorder(sent)),
        raising=True,
    )

    monkeypatch.setattr(
        compatibility_service_mod,
        "Thread",
        _ImmediateThread,
        raising=True,
    )

    iface = SimpleNamespace(
        _closed=False,
        _state_manager=SimpleNamespace(is_closing=False),
    )
    publishing_thread = SimpleNamespace()

    with caplog.at_level(logging.DEBUG):
        BLECompatibilityEventService.publish_connection_status(
            cast(Any, iface),
            connected=True,
            publishing_thread=publishing_thread,
        )

    assert sent == [("meshtastic.connection.status", iface, True)]
    assert "publish queue is unavailable" in caplog.text.lower()


def test_publish_connection_status_runs_async_fallback_when_enqueue_raises(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify status publish falls back asynchronously when enqueue probe raises.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Fixture used to patch queueWork failure behavior.
    caplog : pytest.LogCaptureFixture
        Fixture used to assert debug logging on enqueue failure.

    Returns
    -------
    None
    """
    from meshtastic import mesh_interface as mesh_iface_module
    from meshtastic.interfaces.ble import (
        compatibility_service as compatibility_service_mod,
    )
    from meshtastic.interfaces.ble.compatibility_service import (
        BLECompatibilityEventService,
    )

    sent: list[tuple[str, object, bool]] = []

    monkeypatch.setattr(
        mesh_iface_module,
        "pub",
        SimpleNamespace(sendMessage=_make_send_message_recorder(sent)),
        raising=True,
    )

    monkeypatch.setattr(
        compatibility_service_mod,
        "Thread",
        _ImmediateThread,
        raising=True,
    )

    iface = SimpleNamespace(
        _closed=False,
        _state_manager=SimpleNamespace(is_closing=False),
    )
    publishing_thread = SimpleNamespace()

    monkeypatch.setattr(
        BLECompatibilityEventService,
        "_enqueue_publish_callback",
        staticmethod(
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("enqueue failure")
            )
        ),
    )
    with caplog.at_level(logging.DEBUG):
        BLECompatibilityEventService.publish_connection_status(
            cast(Any, iface),
            connected=False,
            publishing_thread=publishing_thread,
        )

    assert sent == [("meshtastic.connection.status", iface, False)]
    assert "Error queuing connection status publish" in caplog.text


def test_publish_connection_status_runs_async_fallback_when_publishing_thread_missing_while_active(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Status publish should use async fallback when publishing thread is missing."""
    from meshtastic import mesh_interface as mesh_iface_module
    from meshtastic.interfaces.ble import (
        compatibility_service as compatibility_service_mod,
    )
    from meshtastic.interfaces.ble.compatibility_service import (
        BLECompatibilityEventService,
    )

    sent: list[tuple[str, object, bool]] = []

    monkeypatch.setattr(
        mesh_iface_module,
        "pub",
        SimpleNamespace(sendMessage=_make_send_message_recorder(sent)),
        raising=True,
    )

    monkeypatch.setattr(
        compatibility_service_mod,
        "Thread",
        _ImmediateThread,
        raising=True,
    )

    iface = SimpleNamespace(
        _closed=False, _state_manager=SimpleNamespace(is_closing=False)
    )

    with caplog.at_level(logging.DEBUG):
        BLECompatibilityEventService.publish_connection_status(
            cast(Any, iface),
            connected=True,
            publishing_thread=None,
        )

    assert sent == [("meshtastic.connection.status", iface, True)]
    assert "publishing thread is unavailable" in caplog.text.lower()


def test_publish_connection_status_skips_when_publishing_thread_missing_during_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Status publish should skip when publishing thread is missing during shutdown."""
    from meshtastic import mesh_interface as mesh_iface_module
    from meshtastic.interfaces.ble.compatibility_service import (
        BLECompatibilityEventService,
    )

    sent: list[tuple[str, object, bool]] = []

    monkeypatch.setattr(
        mesh_iface_module,
        "pub",
        SimpleNamespace(sendMessage=_make_send_message_recorder(sent)),
        raising=True,
    )

    iface = SimpleNamespace(
        _closed=True, _state_manager=SimpleNamespace(is_closing=True)
    )

    with caplog.at_level(logging.DEBUG):
        BLECompatibilityEventService.publish_connection_status(
            cast(Any, iface),
            connected=False,
            publishing_thread=None,
        )

    assert sent == []
    assert "publishing thread is unavailable" in caplog.text.lower()


def test_get_publishing_thread_prefers_instance_override() -> None:
    """_get_publishing_thread should return an instance override when configured."""
    iface = object.__new__(BLEInterface)
    override_thread = SimpleNamespace(name="override-thread")
    iface._publishing_thread_override = override_thread

    assert iface._get_publishing_thread() is override_thread


def test_get_publishing_thread_falls_back_to_module_publishing_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_get_publishing_thread should return module-level publishingThread when override is unset."""
    iface = object.__new__(BLEInterface)
    iface._publishing_thread_override = None
    module_thread = SimpleNamespace(name="module-thread")
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.publishingThread",
        module_thread,
    )

    assert iface._get_publishing_thread() is module_thread


def test_discovery_manager_filters_meshtastic_devices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DiscoveryManager should return only devices advertising the Meshtastic service UUID."""

    filtered_device = _create_ble_device("AA:BB:CC:DD:EE:FF", "Filtered")
    other_device = _create_ble_device("11:22:33:44:55:66", "Other")

    discover_result = {
        "filtered": (
            filtered_device,
            SimpleNamespace(service_uuids=[SERVICE_UUID]),
        ),
        "other": (
            other_device,
            SimpleNamespace(service_uuids=["some-other-service"]),
        ),
    }
    manager = DiscoveryManager(
        client_factory=lambda **_kwargs: _FakeDiscoveryClient(discover_result)
    )

    devices = manager._discover_devices(address=None)

    assert len(devices) == 1
    assert devices[0].address == filtered_device.address


def test_discovery_manager_filters_targeted_scan_to_whitelist_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Targeted discovery should keep only exact address/name matches."""
    target_device = _create_ble_device("AA:BB:CC:DD:EE:FF", "Target")
    other_meshtastic_device = _create_ble_device("11:22:33:44:55:66", "Other")

    discover_result = {
        "target": (
            target_device,
            SimpleNamespace(service_uuids=[]),
        ),
        "other": (
            other_meshtastic_device,
            SimpleNamespace(service_uuids=[SERVICE_UUID]),
        ),
    }
    manager = DiscoveryManager(
        client_factory=lambda **_kwargs: _FakeDiscoveryClient(discover_result)
    )
    devices = manager._discover_devices(address="AA:BB:CC:DD:EE:FF")

    assert devices == [target_device]


def test_discovery_manager_rejects_non_callable_discover_method() -> None:
    """DiscoveryManager should reject clients missing callable discover entrypoints.

    Returns
    -------
    None
    """

    class InvalidDiscoveryClient:
        _discover = None

    manager = DiscoveryManager(
        client_factory=lambda **_kwargs: InvalidDiscoveryClient()
    )

    with pytest.raises(DiscoveryClientError, match=r"discover|_discover"):
        manager._discover_devices(address=None)

    assert manager._client is None


def test_discovery_manager_accepts_discover_underscore_only_factory() -> None:
    """Verify DiscoveryManager accepts clients exposing only ``_discover``.

    Returns
    -------
    None
    """
    filtered_device = _create_ble_device("AA:BB:CC:DD:EE:FF", "Filtered")
    discover_result = {
        "filtered": (
            filtered_device,
            SimpleNamespace(service_uuids=[SERVICE_UUID]),
        ),
    }

    class UnderscoreDiscoveryClient:
        def __getattr__(self, name: str) -> object:
            if name == "discover":
                return lambda **_kwargs: (_ for _ in ()).throw(
                    AssertionError("dynamic discover hook must be ignored")
                )
            raise AttributeError(name)

        @staticmethod
        def _discover(**_kwargs: object) -> dict[str, Any]:
            return discover_result

    manager = DiscoveryManager(
        client_factory=lambda **_kwargs: UnderscoreDiscoveryClient()
    )
    devices = manager._discover_devices(address=None)

    assert devices == [filtered_device]


def test_discovery_manager_prefers_configured_underscore_discover_when_public_missing() -> (
    None
):
    """Verify discovery prefers configured ``_discover`` when ``discover`` is absent.

    Returns
    -------
    None
    """
    filtered_device = _create_ble_device("AA:BB:CC:DD:EE:FF", "Filtered")
    discover_result = {
        "filtered": (
            filtered_device,
            SimpleNamespace(service_uuids=[SERVICE_UUID]),
        ),
    }
    client = MagicMock()
    client.discover = None
    client._discover = MagicMock(return_value=discover_result)
    manager = DiscoveryManager(client_factory=lambda **_kwargs: client)

    devices = manager._discover_devices(address=None)

    assert devices == [filtered_device]
    client._discover.assert_called_once()

    client._discover.reset_mock()
    devices = manager.discover_devices(address=None)
    assert devices == [filtered_device]
    client._discover.assert_called_once()


def test_discovery_manager_ignores_synthesized_await_bridge() -> None:
    """Declared legacy await bridges should beat synthesized preferred members."""
    filtered_device = _create_ble_device("AA:BB:CC:DD:EE:FF", "Filtered")
    discover_result = {
        "filtered": (
            filtered_device,
            SimpleNamespace(service_uuids=[SERVICE_UUID]),
        ),
    }

    async def _discover() -> dict[str, Any]:
        return discover_result

    class _LegacyAwaitBridgeClient:
        def __getattr__(self, name: str) -> object:
            if name == "async_await":
                return lambda _awaitable: (_ for _ in ()).throw(
                    AssertionError("dynamic await bridge must be ignored")
                )
            raise AttributeError(name)

        @staticmethod
        def discover(**_kwargs: object) -> Any:
            return _discover()

        @staticmethod
        def _async_await(awaitable: Any) -> dict[str, Any]:
            return asyncio.run(awaitable)

    manager = DiscoveryManager(
        client_factory=lambda **_kwargs: _LegacyAwaitBridgeClient()
    )

    assert manager._discover_devices(address=None) == [filtered_device]


def test_discovery_manager_discards_cached_client_on_non_kwarg_typeerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-kwarg TypeError from discover should invalidate the cached discovery client."""

    class _TypeErrorDiscoveryClient:
        class _DiscoveryTypeError(TypeError):
            """Raised by this test stub to emulate discovery call failures."""

        def __init__(self) -> None:
            self.bleak_client = object()

        @staticmethod
        def _discover(**_kwargs: object) -> dict[str, Any]:
            raise _TypeErrorDiscoveryClient._DiscoveryTypeError

    client = _TypeErrorDiscoveryClient()
    manager = DiscoveryManager(client_factory=lambda **_kwargs: client)
    manager._client = cast(Any, client)

    closed_clients: list[object] = []
    monkeypatch.setattr(
        discovery_mod,
        "_close_discovery_client_best_effort",
        lambda stale_client: closed_clients.append(stale_client),
        raising=True,
    )

    with pytest.raises(DiscoveryClientError, match="invalid type"):
        manager._discover_devices(address=None)

    assert manager._client is None
    assert client in closed_clients


def test_discovery_manager_supports_factory_without_log_if_no_address_kwarg() -> None:
    """DiscoveryManager should call factories without log_if_no_address using signature-based fallback."""
    filtered_device = _create_ble_device("AA:BB:CC:DD:EE:FF", "Filtered")
    discover_result = {
        "filtered": (
            filtered_device,
            SimpleNamespace(service_uuids=[SERVICE_UUID]),
        ),
    }

    factory_calls = 0

    def _factory_without_kwargs() -> _FakeDiscoveryClient:
        nonlocal factory_calls
        factory_calls += 1
        return _FakeDiscoveryClient(discover_result)

    manager = DiscoveryManager(client_factory=_factory_without_kwargs)
    devices = manager._discover_devices(address=None)

    assert devices == [filtered_device]
    assert factory_calls == 1


def test_discovery_manager_uses_default_bleclient_when_ble_module_missing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """DiscoveryManager should fall back to default BLEClient when module resolution fails."""
    filtered_device = _create_ble_device("AA:BB:CC:DD:EE:FF", "Filtered")
    discover_result = {
        "filtered": (
            filtered_device,
            SimpleNamespace(service_uuids=[SERVICE_UUID]),
        ),
    }

    class _DefaultClient:
        def __init__(self, **_kwargs: Any) -> None:
            self.bleak_client = None

        @staticmethod
        def _discover(**_kwargs: object) -> dict[str, Any]:
            return discover_result

    monkeypatch.setattr(discovery_mod, "resolve_ble_module", lambda: None)
    monkeypatch.setattr(discovery_mod, "BLEClient", _DefaultClient)
    manager = DiscoveryManager()

    with caplog.at_level(logging.DEBUG):
        devices = manager._discover_devices(address=None)

    assert devices == [filtered_device]
    assert "No BLE module found; using default BLEClient" in caplog.text


def test_discovery_manager_deduplicates_stale_client_cleanup_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Duplicate stale-client references should be closed only once."""

    class _ManagerWithStickySecondNone(DiscoveryManager):
        """DiscoveryManager test double that preserves _client on second None assignment."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._none_assignments = 0
            super().__init__(*args, **kwargs)

        def __setattr__(self, name: str, value: Any) -> None:
            if name == "_client" and value is None and "_client" in self.__dict__:
                self._none_assignments += 1
                if self._none_assignments >= 2:
                    return
            super().__setattr__(name, value)

    class _InvalidDiscoveryClient:
        def __init__(self) -> None:
            self.bleak_client = object()

        @staticmethod
        def isConnected() -> bool:
            return False

    invalid_client = _InvalidDiscoveryClient()
    manager = _ManagerWithStickySecondNone(client_factory=lambda: invalid_client)
    manager._client = cast(Any, invalid_client)
    closed: list[int] = []
    monkeypatch.setattr(
        discovery_mod,
        "_close_discovery_client_best_effort",
        lambda stale_client: closed.append(id(stale_client)),
    )

    with pytest.raises(DiscoveryClientError, match="invalid type"):
        manager._discover_devices(address=None)

    assert closed == [id(invalid_client)]


def test_close_discovery_client_best_effort_closes_coroutine_when_task_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Best-effort cleanup should close the coroutine when create_task fails."""

    class _AwaitableClose:
        def __init__(self) -> None:
            self.closed = False

        def __await__(self) -> Any:
            """Return an empty awaitable iterator that completes immediately."""
            return iter(())

        def close(self) -> None:
            """Track explicit coroutine close calls."""
            self.closed = True

    awaitable = _AwaitableClose()

    class _Client:
        def close(self) -> Any:
            """Return the awaitable close result used by this test."""
            return awaitable

    class _Loop:
        @staticmethod
        def create_task(_task: Any) -> None:
            """Simulate loop task scheduling failure."""
            raise RuntimeError("cannot schedule task")

    def _get_running_loop() -> _Loop:
        """Return the fake running event loop."""
        return _Loop()

    def _await_close_result_passthrough(awaitable: Any) -> Any:
        """Keep awaitable unchanged for deterministic unit-test behavior."""
        return awaitable

    def _wait_for_passthrough(
        awaitable: Any, _timeout: float | None = None, **_kwargs: Any
    ) -> Any:
        """Bypass timeout wrapping to keep this branch deterministic."""
        return awaitable

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.discovery.asyncio.get_running_loop",
        _get_running_loop,
    )
    monkeypatch.setattr(
        discovery_mod, "_await_close_result", _await_close_result_passthrough
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.discovery.asyncio.wait_for",
        _wait_for_passthrough,
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.discovery.inspect.iscoroutine",
        lambda value: isinstance(value, _AwaitableClose),
    )

    assert awaitable.closed is False
    _close_discovery_client_best_effort(_Client())

    assert awaitable.closed is True


def test_finalize_discovery_close_task_discards_task_and_logs_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """_finalize_discovery_close_task should drop retained tasks and log non-cancel exceptions."""

    class _Task:
        def exception(self) -> Exception:
            """Return a deterministic task failure for log assertion."""
            return RuntimeError("close task failed")

    task = _Task()
    with discovery_mod._PENDING_DISCOVERY_CLOSE_TASKS_LOCK:
        discovery_mod._PENDING_DISCOVERY_CLOSE_TASKS.add(cast(Any, task))

    try:
        with caplog.at_level(logging.DEBUG):
            discovery_mod._finalize_discovery_close_task(task)  # type: ignore[arg-type]

        with discovery_mod._PENDING_DISCOVERY_CLOSE_TASKS_LOCK:
            assert cast(Any, task) not in discovery_mod._PENDING_DISCOVERY_CLOSE_TASKS
        assert (
            "Async close/disconnect failed for discarded discovery client."
            in caplog.text
        )
    finally:
        with discovery_mod._PENDING_DISCOVERY_CLOSE_TASKS_LOCK:
            discovery_mod._PENDING_DISCOVERY_CLOSE_TASKS.discard(cast(Any, task))


def test_close_discovery_client_best_effort_tracks_pending_task_on_running_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Best-effort async close should retain task until done callback executes."""

    class _AwaitableClose:
        def __await__(self) -> Any:
            """Return an empty awaitable iterator that completes immediately."""
            return iter(())

    class _Client:
        def close(self) -> Any:
            """Return an awaitable close result for scheduling."""
            return _AwaitableClose()

    class _Task:
        def __init__(self) -> None:
            self._callbacks: list[Callable[[Any], None]] = []

        def add_done_callback(self, callback: Callable[[Any], None]) -> None:
            """Store done callbacks for explicit invocation by the test."""
            self._callbacks.append(callback)

        def exception(self) -> None:
            """Expose successful task completion."""
            return None

    task = _Task()

    class _Loop:
        @staticmethod
        def create_task(_awaitable: Any) -> _Task:
            """Return the retained task used for callback assertions."""
            return task

    def _get_running_loop() -> _Loop:
        """Return the fake running event loop."""
        return _Loop()

    def _await_close_result_passthrough(awaitable: Any) -> Any:
        """Keep awaitable unchanged for deterministic unit-test behavior."""
        return awaitable

    def _wait_for_passthrough(
        awaitable: Any, _timeout: float | None = None, **_kwargs: Any
    ) -> Any:
        """Bypass timeout wrapping to keep this branch deterministic."""
        return awaitable

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.discovery.asyncio.get_running_loop",
        _get_running_loop,
    )
    monkeypatch.setattr(
        discovery_mod, "_await_close_result", _await_close_result_passthrough
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.discovery.asyncio.wait_for",
        _wait_for_passthrough,
    )

    _close_discovery_client_best_effort(_Client())

    try:
        with discovery_mod._PENDING_DISCOVERY_CLOSE_TASKS_LOCK:
            assert cast(Any, task) in discovery_mod._PENDING_DISCOVERY_CLOSE_TASKS
        assert len(task._callbacks) == 1

        task._callbacks[0](task)
        with discovery_mod._PENDING_DISCOVERY_CLOSE_TASKS_LOCK:
            assert cast(Any, task) not in discovery_mod._PENDING_DISCOVERY_CLOSE_TASKS
    finally:
        with discovery_mod._PENDING_DISCOVERY_CLOSE_TASKS_LOCK:
            discovery_mod._PENDING_DISCOVERY_CLOSE_TASKS.discard(cast(Any, task))


def test_discovery_manager_raises_when_factory_returns_none() -> None:
    """DiscoveryManager should raise DiscoveryClientError for None-returning factories."""
    manager = DiscoveryManager(client_factory=lambda: None)

    with pytest.raises(DiscoveryClientError, match="returned None"):
        manager._discover_devices(address=None)


def test_parse_scan_response_prefers_exact_name_before_normalized_match() -> None:
    """Targeted scan should prefer an exact name match over normalized-name candidates."""
    exact_name_device = _create_ble_device("AA:BB:CC:DD:EE:FF", "My Device")
    normalized_only_device = _create_ble_device("11:22:33:44:55:66", "my device")

    response = {
        "exact": (exact_name_device, SimpleNamespace(service_uuids=[])),
        "normalized": (normalized_only_device, SimpleNamespace(service_uuids=[])),
    }

    devices = _parse_scan_response(response, whitelist_address="My Device")

    assert devices == [exact_name_device]


def test_parse_scan_response_skips_malformed_tuple_payloads() -> None:
    """Malformed discover tuple entries should be ignored, preserving only valid BLEDevice entries."""
    valid_device = _create_ble_device("AA:BB:CC:DD:EE:FF", "Valid")
    response = {
        "valid": (valid_device, SimpleNamespace(service_uuids=[SERVICE_UUID])),
        "invalid_device": (
            "not-a-device",
            SimpleNamespace(service_uuids=[SERVICE_UUID]),
        ),
        "invalid_adv": (valid_device, object()),
    }

    devices = _parse_scan_response(response, whitelist_address=None)

    assert devices == [valid_device]


def test_looks_like_ble_address_accepts_mac_and_uuid_shapes() -> None:
    """Address-shape detection should support MAC-style and UUID-style identifiers."""
    assert _looks_like_ble_address("AA:BB:CC:DD:EE:FF")
    assert _looks_like_ble_address("aabbccddeeff")
    assert _looks_like_ble_address("00112233445566778899aabbccddeeff")
    assert _looks_like_ble_address("00112233-4455-6677-8899-aabbccddeeff")
    assert not _looks_like_ble_address("Meshtastic Device")


def test_filter_devices_rejects_ambiguous_normalized_name_matches() -> None:
    """Name matching should reject ambiguous normalized-name collisions."""
    devices = [
        _create_ble_device("AA:BB:CC:DD:EE:FF", "My Device"),
        _create_ble_device("11:22:33:44:55:66", "my device"),
    ]

    matches = _filter_devices_for_target_identifier(devices, "MY DEVICE")

    assert matches == []


def test_ble_interface_with_timeout_wrapper_returns_result() -> None:
    """BLEInterface._with_timeout should delegate to with_timeout and return the awaited value."""

    async def _ready() -> str:
        return "ok"

    assert (
        asyncio.run(BLEInterface._with_timeout(_ready(), timeout=1.0, label="ble-op"))
        == "ok"
    )


def test_ble_interface_sanitize_address_wrapper_delegates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_sanitize_address should delegate to sanitize_address helper."""
    iface = object.__new__(BLEInterface)
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.sanitize_address",
        lambda address: "normalized" if address else None,
    )

    assert iface._sanitize_address("AA-BB-CC-DD-EE-FF") == "normalized"


def test_discovery_manager_destructor_does_not_close_client() -> None:
    """DiscoveryManager.__del__ should avoid active client close I/O during GC."""

    class StubDiscoveryClient:
        """Discovery client stub used for destructor behavior checks.

        Methods
        -------
        close()
        """

        def __init__(self) -> None:
            """Initialize the test stub and reset its close-call counter.

            Sets the `close_calls` attribute to 0; tests increment this counter when the stub's `close()` is invoked to verify that discovery clients are not closed unexpectedly.
            """
            self.close_calls = 0

        def close(self) -> None:
            """Record that the client's close method was invoked by incrementing an internal call counter.

            This method exists for tests to track how many times close() was called on the object by incrementing the `close_calls` attribute.
            """
            self.close_calls += 1

    manager = DiscoveryManager()
    client = StubDiscoveryClient()
    manager._client = cast(BLEClient, client)

    manager.__del__()

    assert client.close_calls == 0
    assert manager._client is None


def test_discovery_manager_close_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """close() should clear and clean up the persistent discovery client only once."""
    manager = DiscoveryManager()
    client = object()
    close_calls: list[int] = []
    manager._client = cast(BLEClient, client)

    monkeypatch.setattr(
        discovery_mod,
        "_close_discovery_client_best_effort",
        lambda stale_client: close_calls.append(id(stale_client)),
    )

    manager.close()
    manager.close()

    assert close_calls == [id(client)]
    assert manager._client is None


def test_discovery_manager_destructor_tolerates_unusable_lock() -> None:
    """DiscoveryManager.__del__ should fall back when _client_lock is not lock-like."""
    manager = object.__new__(DiscoveryManager)
    cast(Any, manager)._client_lock = object()
    cast(Any, manager)._client = object()

    manager.__del__()

    assert cast(Any, manager)._client is None


def test_connection_validator_enforces_state() -> None:
    """ConnectionValidator should block connections when interface is closing or already connecting."""

    state_manager = BLEStateManager()
    validator = ConnectionValidator(
        state_manager, state_manager._lock, BLEInterface.BLEError
    )

    validator._validate_connection_request()

    assert state_manager._transition_to(ConnectionState.CONNECTING) is True
    assert state_manager._transition_to(ConnectionState.CONNECTED) is True
    assert state_manager._transition_to(ConnectionState.DISCONNECTING) is True
    with pytest.raises(BLEInterface.BLEError) as excinfo:
        validator._validate_connection_request()
    assert "closing" in str(excinfo.value)

    assert state_manager._transition_to(ConnectionState.DISCONNECTED) is True
    assert state_manager._transition_to(ConnectionState.CONNECTING) is True
    with pytest.raises(BLEInterface.BLEError) as excinfo:
        validator._validate_connection_request()
    assert "connection in progress" in str(excinfo.value)


def test_connection_validator_existing_client_checks() -> None:
    """check_existing_client should allow reuse only when the requested identifier matches."""

    state_manager = BLEStateManager()
    validator = ConnectionValidator(
        state_manager, state_manager._lock, BLEInterface.BLEError
    )
    client = DummyClient()
    cast(Any, client).isConnected = lambda: True

    ble_like = cast(BLEClient, client)
    assert validator._check_existing_client(ble_like, None, None) is True
    assert validator._check_existing_client(ble_like, "dummy", "dummy") is True
    assert (
        validator._check_existing_client(client, "something-else", None) is False  # type: ignore[arg-type]
    )


def test_get_existing_client_if_valid_uses_last_request_snapshot() -> None:
    """_get_existing_client_if_valid should validate against a lock-protected request snapshot."""

    iface = object.__new__(BLEInterface)
    cast(Any, iface)._state_lock = threading.RLock()
    cast(Any, iface)._last_connection_request = "old-request"
    cast(Any, iface)._state_manager = SimpleNamespace(_is_connected=True)
    cast(Any, iface)._disconnect_notified = False

    class _Client:
        def isConnected(self) -> bool:
            cast(Any, iface)._last_connection_request = "new-request"
            return True

    class _Validator:
        def __init__(self) -> None:
            self.seen_last_request: str | None = None

        def _check_existing_client(
            self,
            _client: Any,
            _normalized_request: str | None,
            last_request: str | None,
        ) -> bool:
            self.seen_last_request = last_request
            return last_request == "old-request"

    client = _Client()
    validator = _Validator()
    cast(Any, iface).client = client
    cast(Any, iface)._connection_validator = validator

    result = BLEInterface._get_existing_client_if_valid(iface, normalized_request="any")

    assert cast(object, result) is client
    assert validator.seen_last_request == "old-request"


def test_close_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that close() is idempotent and only calls disconnect once."""
    client = DummyClient()
    iface = _build_interface(monkeypatch, client)

    iface.close()
    iface.close()
    iface.close()  # Call multiple times to ensure idempotency

    assert client.disconnect_calls == 1
    assert client.close_calls == 1
