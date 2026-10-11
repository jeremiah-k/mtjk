"""Additional edge case tests for BLE client functionality."""

import asyncio
import re
import threading
from collections.abc import Awaitable, Callable, Coroutine
from functools import partial
from types import SimpleNamespace
from typing import Any, NoReturn, cast

import pytest

pytest.importorskip("bleak")

from bleak.exc import BleakError

from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.constants import (
    BLECLIENT_ERROR_ASYNC_OPERATION_FAILED,
    BLECLIENT_ERROR_ASYNC_TIMEOUT,
    BLECLIENT_ERROR_CANCELLED,
    BLECLIENT_ERROR_CANNOT_CONNECT_NOT_INITIALIZED,
    BLECLIENT_ERROR_CANNOT_DISCONNECT_NOT_INITIALIZED,
    BLECLIENT_ERROR_CANNOT_GET_SERVICES_NOT_INITIALIZED,
    BLECLIENT_ERROR_CANNOT_PAIR_NOT_INITIALIZED,
    BLECLIENT_ERROR_CANNOT_PAIR_UNSUPPORTED,
    BLECLIENT_ERROR_CANNOT_READ_NOT_INITIALIZED,
    BLECLIENT_ERROR_CANNOT_START_NOTIFY_NOT_INITIALIZED,
    BLECLIENT_ERROR_CANNOT_STOP_NOTIFY_NOT_INITIALIZED,
    BLECLIENT_ERROR_CANNOT_UNPAIR_NOT_INITIALIZED,
    BLECLIENT_ERROR_CANNOT_UNPAIR_UNSUPPORTED,
    BLECLIENT_ERROR_CANNOT_WRITE_NOT_INITIALIZED,
    BLECLIENT_ERROR_RUNNER_THREAD_WAIT,
    BLECLIENT_MANAGEMENT_AWAIT_TIMEOUT,
    ERROR_MANAGEMENT_AWAIT_TIMEOUT_INVALID,
    SERVICE_CHARACTERISTIC_RETRY_COUNT,
)

TRANSIENT_SERVICES_LOOKUP_FAILURE = "transient services lookup failure"


def _make_run_awaitable(
    captured_timeout: list[float | None] | None = None,
) -> Callable[[Coroutine[Any, Any, object], float | None], object]:
    """Create a simple `_async_await` stand-in that runs the awaitable locally."""

    def _run_awaitable(
        awaitable: Coroutine[Any, Any, object],
        timeout: float | None = None,
    ) -> object:
        if captured_timeout is not None:
            captured_timeout.append(timeout)
        return asyncio.run(awaitable)

    return _run_awaitable


def _raise_wrapped_not_implemented(
    awaitable: Awaitable[object],
    timeout: float | None = None,
) -> NoReturn:
    """Simulate `_async_await` wrapping NotImplementedError in BLEError."""
    _ = timeout
    close_awaitable = getattr(awaitable, "close", None)
    if callable(close_awaitable):
        close_awaitable()
    try:
        raise NotImplementedError("backend unsupported")
    except NotImplementedError as exc:
        raise BLEClient.BLEError(
            BLECLIENT_ERROR_ASYNC_OPERATION_FAILED.format(exc)
        ) from exc


@pytest.mark.unit
def test_bleclient_discovery_mode_without_address(ble_client: BLEClient) -> None:
    """BLEClient should support discovery-only mode when initialized without an address."""
    assert ble_client.address is None
    assert ble_client.bleak_client is None
    assert not ble_client.isConnected()


@pytest.mark.unit
def test_bleclient_isConnected_handles_missing_bleak_client(
    ble_client: BLEClient,
) -> None:
    """IsConnected should return False when bleak_client is None."""
    assert not ble_client.isConnected()


@pytest.mark.unit
def test_bleclient_is_connected_alias(ble_client: BLEClient) -> None:
    """is_connected should be an alias for isConnected."""
    assert ble_client.is_connected() == ble_client.isConnected()


@pytest.mark.unit
def test_bleclient_close_is_idempotent(ble_client: BLEClient) -> None:
    """close() should be idempotent and safe to call multiple times."""
    ble_client.close()
    ble_client.close()  # Should not raise
    ble_client.close()  # Should not raise


@pytest.mark.unit
def test_bleclient_context_manager() -> None:
    """BLEClient should work as a context manager."""
    with BLEClient(address=None) as client:
        assert client is not None
    assert not client.isConnected()


@pytest.mark.unit
def test_bleclient_error_class_exists() -> None:
    """BLEClient should have a BLEError exception class."""
    assert hasattr(BLEClient, "BLEError")
    assert issubclass(BLEClient.BLEError, Exception)


@pytest.mark.unit
def test_bleclient_operations_require_initialized_client(ble_client: BLEClient) -> None:
    """BLEClient operations should raise BLEError when bleak_client is not initialized."""
    with pytest.raises(
        BLEClient.BLEError, match=BLECLIENT_ERROR_CANNOT_CONNECT_NOT_INITIALIZED
    ):
        ble_client.connect()

    with pytest.raises(
        BLEClient.BLEError, match=BLECLIENT_ERROR_CANNOT_DISCONNECT_NOT_INITIALIZED
    ):
        ble_client.disconnect()

    with pytest.raises(
        BLEClient.BLEError, match=BLECLIENT_ERROR_CANNOT_READ_NOT_INITIALIZED
    ):
        ble_client.read_gatt_char("uuid")

    with pytest.raises(
        BLEClient.BLEError, match=BLECLIENT_ERROR_CANNOT_WRITE_NOT_INITIALIZED
    ):
        ble_client.write_gatt_char("uuid", b"data")

    with pytest.raises(
        BLEClient.BLEError, match=BLECLIENT_ERROR_CANNOT_PAIR_NOT_INITIALIZED
    ):
        ble_client.pair()

    with pytest.raises(
        BLEClient.BLEError, match=BLECLIENT_ERROR_CANNOT_UNPAIR_NOT_INITIALIZED
    ):
        ble_client.unpair()

    with pytest.raises(
        BLEClient.BLEError, match=BLECLIENT_ERROR_CANNOT_GET_SERVICES_NOT_INITIALIZED
    ):
        ble_client._get_services()

    with pytest.raises(
        BLEClient.BLEError, match=BLECLIENT_ERROR_CANNOT_START_NOTIFY_NOT_INITIALIZED
    ):
        ble_client.start_notify("uuid", lambda *_args: None)

    with pytest.raises(
        BLEClient.BLEError,
        match=BLECLIENT_ERROR_CANNOT_STOP_NOTIFY_NOT_INITIALIZED,
    ):
        ble_client.stopNotify("uuid")


@pytest.mark.unit
def test_bleclient_has_characteristic_returns_false_without_client(
    ble_client: BLEClient,
) -> None:
    """has_characteristic should return False when bleak_client is None."""
    assert not ble_client.has_characteristic("some-uuid")


@pytest.mark.unit
def test_bleclient_stop_notify_alias(ble_client: BLEClient) -> None:
    """stop_notify should be an alias for stopNotify."""
    # Both should raise the same error
    with pytest.raises(
        BLEClient.BLEError,
        match=BLECLIENT_ERROR_CANNOT_STOP_NOTIFY_NOT_INITIALIZED,
    ):
        ble_client.stopNotify("uuid")
    with pytest.raises(
        BLEClient.BLEError,
        match=BLECLIENT_ERROR_CANNOT_STOP_NOTIFY_NOT_INITIALIZED,
    ):
        ble_client.stop_notify("uuid")


@pytest.mark.unit
def test_bleclient_init_with_address_syncs_from_bleak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BLEClient should create bleak client and sync canonical address from backend."""

    class _BleakStub:
        def __init__(self, _address: object, **_kwargs: object) -> None:
            self.address = "AA:BB:CC:DD:EE:FF"

    monkeypatch.setattr("meshtastic.interfaces.ble.client.BleakRootClient", _BleakStub)

    client = BLEClient("11:22:33:44:55:66")
    assert client.bleak_client is not None
    assert client.address == "AA:BB:CC:DD:EE:FF"
    client.close()


@pytest.mark.unit
def test_bleclient_init_translates_adapter_kwarg_to_bluez(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BLEClient should map deprecated adapter kwarg to bluez adapter args."""

    captured_kwargs: list[dict[str, Any]] = []

    class _BleakStub:
        def __init__(self, _address: object, **kwargs: object) -> None:
            captured_kwargs.append(dict(kwargs))
            self.address = "AA:BB:CC:DD:EE:FF"

    monkeypatch.setattr("meshtastic.interfaces.ble.client.BleakRootClient", _BleakStub)

    client = BLEClient("11:22:33:44:55:66", adapter="hci0")
    assert client.bleak_client is not None
    assert captured_kwargs == [{"bluez": {"adapter": "hci0"}}]
    client.close()


@pytest.mark.unit
def test_bleclient_discover_translates_adapter_kwarg_to_bluez(
    monkeypatch: pytest.MonkeyPatch,
    ble_client: BLEClient,
) -> None:
    """BLEClient discover() should map deprecated adapter kwarg to bluez args."""
    captured_kwargs: list[dict[str, Any]] = []

    async def _discover_stub(**kwargs: Any) -> list[Any]:
        captured_kwargs.append(dict(kwargs))
        return []

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.client.BleakScanner.discover",
        _discover_stub,
    )
    monkeypatch.setattr(ble_client, "_async_await", _make_run_awaitable())

    assert ble_client.discover(adapter="hci0") == []
    assert captured_kwargs == [{"bluez": {"adapter": "hci0"}}]


@pytest.mark.unit
def test_bleclient_init_prefers_explicit_bluez_adapter_over_adapter_kwarg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BLEClient should preserve explicit bluez adapter when adapter kwarg is also set."""
    captured_kwargs: list[dict[str, Any]] = []

    class _BleakStub:
        def __init__(self, _address: object, **kwargs: object) -> None:
            captured_kwargs.append(dict(kwargs))
            self.address = "AA:BB:CC:DD:EE:FF"

    monkeypatch.setattr("meshtastic.interfaces.ble.client.BleakRootClient", _BleakStub)

    client = BLEClient(
        "11:22:33:44:55:66",
        adapter="hci0",
        bluez={"adapter": "hci1"},
    )
    assert client.bleak_client is not None
    assert captured_kwargs == [{"bluez": {"adapter": "hci1"}}]
    client.close()


@pytest.mark.unit
def test_bleclient_sync_address_noops_without_bleak_client(
    ble_client: BLEClient,
) -> None:
    """Address sync should return early when the backend client is not initialized."""
    ble_client.address = "11:22:33:44:55:66"
    ble_client.bleak_client = None
    ble_client._sync_address_from_bleak()
    assert ble_client.address == "11:22:33:44:55:66"


@pytest.mark.unit
def test_bleclient_management_call_reraises_non_notimplemented_bleerror() -> None:
    """_run_management_call should re-raise BLEError unless caused by NotImplementedError."""
    client = BLEClient(address=None)
    client.bleak_client = cast(Any, SimpleNamespace())

    async def _noop_operation() -> None:
        return None

    def _raise_bleerror(
        coro: Coroutine[Any, Any, object], timeout: float | None = None
    ) -> NoReturn:
        _ = timeout
        close_awaitable = getattr(coro, "close", None)
        if callable(close_awaitable):
            close_awaitable()
        try:
            raise ValueError("backend failure")
        except ValueError as exc:
            raise BLEClient.BLEError("wrapped failure") from exc

    client._async_await = _raise_bleerror  # type: ignore[method-assign]

    with pytest.raises(BLEClient.BLEError, match="wrapped failure"):
        client._run_management_call(
            lambda: _noop_operation(),
            await_timeout=BLECLIENT_MANAGEMENT_AWAIT_TIMEOUT,
            not_initialized_error=BLECLIENT_ERROR_CANNOT_PAIR_NOT_INITIALIZED,
            unsupported_error=BLECLIENT_ERROR_CANNOT_PAIR_UNSUPPORTED,
        )
    client.close()


@pytest.mark.unit
def test_bleclient_operations_fail_when_closed(ble_client: BLEClient) -> None:
    """Operations should fail with clear error when client is closed."""
    ble_client.close()

    # Create a simple coroutine to test
    async def dummy_coro() -> str:
        return "result"

    with pytest.raises(
        BLEClient.BLEError, match="Cannot schedule operation: BLE client is closed"
    ):
        ble_client._async_await(dummy_coro())


@pytest.mark.unit
def test_bleclient_async_await_static_method() -> None:
    """BLEClient should have _with_timeout method."""
    assert hasattr(BLEClient, "_with_timeout")
    assert callable(BLEClient._with_timeout)


@pytest.mark.unit
def test_bleclient_error_constant() -> None:
    """Verify BLECLIENT_ERROR_ASYNC_TIMEOUT constant is properly defined."""
    assert BLECLIENT_ERROR_ASYNC_TIMEOUT == "Async operation timed out"


@pytest.mark.unit
def test_bleclient_discover_method_exists(ble_client: BLEClient) -> None:
    """BLEClient should have discover and _discover methods."""
    assert hasattr(ble_client, "discover")
    assert hasattr(ble_client, "_discover")
    assert hasattr(ble_client, "find_device")
    assert hasattr(ble_client, "findDevice")
    assert callable(ble_client.discover)
    assert callable(ble_client._discover)
    assert callable(ble_client.find_device)
    assert callable(ble_client.findDevice)


@pytest.mark.unit
def test_bleclient_with_timeout_delegates_to_utils_wrapper() -> None:
    """_with_timeout should execute through the shared with_timeout utility."""

    async def _done() -> str:
        return "ok"

    assert (
        asyncio.run(BLEClient._with_timeout(_done(), timeout=1.0, label="test")) == "ok"
    )


@pytest.mark.unit
def test_bleclient_has_characteristic_retries_and_skips_final_sleep(
    monkeypatch: pytest.MonkeyPatch,
    ble_client: BLEClient,
) -> None:
    """has_characteristic should retry BleakError reads and avoid sleeping after final retry."""

    class _Services:
        def get_characteristic(self, _specifier: str) -> object:
            """Raise a transient lookup error to trigger retry logic."""
            raise BleakError(TRANSIENT_SERVICES_LOOKUP_FAILURE)

    services = _Services()
    sleep_calls: list[float] = []
    ble_client.bleak_client = type("_BleakClient", (), {"services": services})()
    monkeypatch.setattr(ble_client, "_get_services", lambda: services)
    monkeypatch.setattr(
        ble_client.error_handler,
        "_safe_execute",
        lambda operation, **_kwargs: operation(),
    )

    def _record_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.client.time.sleep",
        _record_sleep,
    )

    assert ble_client.has_characteristic("0000") is False
    assert len(sleep_calls) == SERVICE_CHARACTERISTIC_RETRY_COUNT - 1


@pytest.mark.unit
def test_bleclient_async_await_maps_asyncio_cancelled_to_cancelled_error(
    monkeypatch: pytest.MonkeyPatch,
    ble_client: BLEClient,
) -> None:
    """_async_await should map asyncio cancellation to BLE cancelled error."""

    class _CancelledFuture:
        def result(self, _timeout: float | None = None) -> None:
            """Raise asyncio.CancelledError to simulate cancelled future result retrieval."""
            raise asyncio.CancelledError()

    async def _dummy_coro() -> None:
        pass

    def _fake_async_run(coro: Any) -> _CancelledFuture:
        coro.close()
        return _CancelledFuture()

    monkeypatch.setattr(ble_client, "_async_run", _fake_async_run)
    with pytest.raises(BLEClient.BLEError, match=BLECLIENT_ERROR_CANCELLED):
        ble_client._async_await(_dummy_coro())


@pytest.mark.unit
def test_bleclient_async_await_rejects_wait_from_runner_thread(
    monkeypatch: pytest.MonkeyPatch,
    ble_client: BLEClient,
) -> None:
    """_async_await should fail fast when invoked from the runner thread."""

    class _FutureLike:
        def cancel(self) -> None:
            """Support best-effort cancellation in guarded path."""

    async def _dummy_coro() -> None:
        pass

    def _fake_async_run(coro: Any) -> _FutureLike:
        coro.close()
        return _FutureLike()

    monkeypatch.setattr(ble_client, "_async_run", _fake_async_run)

    fake_runner = type("_Runner", (), {"_thread": threading.current_thread()})()
    monkeypatch.setattr(ble_client, "_runner", fake_runner)

    with pytest.raises(BLEClient.BLEError, match=BLECLIENT_ERROR_RUNNER_THREAD_WAIT):
        ble_client._async_await(_dummy_coro())


@pytest.mark.unit
def test_bleclient_find_device_aliases_delegate_to_expected_targets(
    monkeypatch: pytest.MonkeyPatch,
    ble_client: BLEClient,
) -> None:
    """find_device and findDevice aliases should delegate to findDevice/discover."""
    discover_calls: list[dict[str, Any]] = []
    find_device_calls: list[dict[str, Any]] = []

    def _discover_stub(**kwargs: Any) -> list[str]:
        discover_calls.append(kwargs)
        return ["discovered"]

    def _find_device_stub(**kwargs: Any) -> list[str]:
        find_device_calls.append(kwargs)
        return ["found"]

    monkeypatch.setattr(ble_client, "discover", _discover_stub)
    assert ble_client.findDevice(address="AA:BB") == ["discovered"]
    assert discover_calls == [{"address": "AA:BB"}]

    monkeypatch.setattr(ble_client, "findDevice", _find_device_stub)
    assert ble_client.find_device(timeout=1.5) == ["found"]
    assert find_device_calls == [{"timeout": 1.5}]


@pytest.mark.unit
def test_bleclient_discover_delegates_to_private_discover(
    monkeypatch: pytest.MonkeyPatch,
    ble_client: BLEClient,
) -> None:
    """discover() should delegate through the private _discover implementation."""
    discover_calls: list[dict[str, Any]] = []

    def _discover_stub(**kwargs: Any) -> list[str]:
        discover_calls.append(kwargs)
        return ["discovered_from__"]

    monkeypatch.setattr(ble_client, "_discover", _discover_stub)
    assert ble_client.discover(address="AA:BB") == ["discovered_from__"]
    assert discover_calls == [{"address": "AA:BB"}]


@pytest.mark.unit
def test_bleclient_has_characteristic_returns_false_when_services_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
    ble_client: BLEClient,
) -> None:
    """has_characteristic should return False when services cannot be resolved."""

    class _BleakClientWithBrokenServices:
        @property
        def services(self) -> Any:
            """Simulate a services property that fails at access time."""
            raise BleakError("services unavailable")

    ble_client.bleak_client = cast(Any, _BleakClientWithBrokenServices())
    monkeypatch.setattr(ble_client, "_get_services", lambda: None)
    monkeypatch.setattr(
        ble_client.error_handler,
        "_safe_execute",
        lambda operation, **_kwargs: operation(),
    )

    assert ble_client.has_characteristic("0000") is False


@pytest.mark.unit
def test_bleclient_unpair_raises_when_backend_does_not_support_unpair(
    ble_client: BLEClient,
) -> None:
    """unpair() should fail with a clear message when backend lacks unpair support."""
    ble_client.bleak_client = type("_NoUnpairClient", (), {"unpair": None})()
    with pytest.raises(
        BLEClient.BLEError,
        match=BLECLIENT_ERROR_CANNOT_UNPAIR_UNSUPPORTED,
    ):
        ble_client.unpair()


@pytest.mark.unit
def test_bleclient_unpair_delegates_to_backend(
    monkeypatch: pytest.MonkeyPatch,
    ble_client: BLEClient,
) -> None:
    """unpair() should invoke backend unpair through _async_await."""
    backend_calls: list[bool] = []
    captured_timeout: list[float | None] = []

    class _Backend:
        def __init__(self) -> None:
            self.address = "AA:BB:CC:DD:EE:01"

        async def unpair(self) -> None:
            backend_calls.append(True)
            self.address = "AA:BB:CC:DD:EE:02"

    ble_client.bleak_client = cast(Any, _Backend())
    ble_client.address = "AA:BB:CC:DD:EE:01"
    monkeypatch.setattr(
        ble_client,
        "_async_await",
        _make_run_awaitable(captured_timeout),
    )

    ble_client.unpair(await_timeout=12.5)
    assert backend_calls == [True]
    assert captured_timeout == [12.5]
    assert ble_client.address == "AA:BB:CC:DD:EE:02"


@pytest.mark.unit
def test_bleclient_unpair_translates_not_implemented_error(
    monkeypatch: pytest.MonkeyPatch,
    ble_client: BLEClient,
) -> None:
    """unpair() should wrap backend NotImplementedError as unsupported."""

    class _Backend:
        async def unpair(self) -> None:
            raise NotImplementedError

    ble_client.bleak_client = cast(Any, _Backend())
    monkeypatch.setattr(ble_client, "_async_await", _make_run_awaitable())

    with pytest.raises(
        BLEClient.BLEError, match=BLECLIENT_ERROR_CANNOT_UNPAIR_UNSUPPORTED
    ) as exc_info:
        ble_client.unpair()

    assert isinstance(exc_info.value.__cause__, NotImplementedError)


@pytest.mark.unit
def test_bleclient_unpair_translates_wrapped_not_implemented_error(
    monkeypatch: pytest.MonkeyPatch,
    ble_client: BLEClient,
) -> None:
    """unpair() should preserve the unsupported contract when _async_await wraps NotImplementedError."""

    class _Backend:
        async def unpair(self) -> None:
            raise NotImplementedError("backend unsupported")

    ble_client.bleak_client = cast(Any, _Backend())
    monkeypatch.setattr(ble_client, "_async_await", _raise_wrapped_not_implemented)

    with pytest.raises(
        BLEClient.BLEError, match=BLECLIENT_ERROR_CANNOT_UNPAIR_UNSUPPORTED
    ) as exc_info:
        ble_client.unpair()

    assert isinstance(exc_info.value.__cause__, NotImplementedError)


@pytest.mark.unit
def test_bleclient_pair_rejects_backends_without_pair_support(
    ble_client: BLEClient,
) -> None:
    """pair() should report unsupported backends that do not expose pair()."""
    ble_client.bleak_client = cast(Any, object())

    with pytest.raises(
        BLEClient.BLEError,
        match=BLECLIENT_ERROR_CANNOT_PAIR_UNSUPPORTED,
    ):
        ble_client.pair(confirm=True)


@pytest.mark.unit
def test_bleclient_pair_delegates_to_backend(
    monkeypatch: pytest.MonkeyPatch,
    ble_client: BLEClient,
) -> None:
    """pair() should invoke backend pair through _async_await and return None."""
    backend_calls: list[dict[str, object]] = []
    captured_timeout: list[float | None] = []

    class _Backend:
        def __init__(self) -> None:
            self.address = "AA:BB:CC:DD:EE:0A"

        async def pair(self, **kwargs: object) -> None:
            backend_calls.append(kwargs)
            self.address = "AA:BB:CC:DD:EE:0B"

    ble_client.bleak_client = cast(Any, _Backend())
    ble_client.address = "AA:BB:CC:DD:EE:0A"
    monkeypatch.setattr(
        ble_client,
        "_async_await",
        _make_run_awaitable(captured_timeout),
    )

    ble_client.pair(confirm=True, await_timeout=9.0)
    assert backend_calls == [{"confirm": True}]
    assert captured_timeout == [9.0]
    assert ble_client.address == "AA:BB:CC:DD:EE:0B"


@pytest.mark.unit
def test_bleclient_connect_refreshes_cached_address_from_backend(
    monkeypatch: pytest.MonkeyPatch,
    ble_client: BLEClient,
) -> None:
    """connect() should refresh BLEClient.address from backend-resolved address."""
    captured_timeout: list[float | None] = []

    class _Backend:
        def __init__(self) -> None:
            self.address = "AA:BB:CC:DD:EE:10"

        async def connect(self, **_kwargs: object) -> None:
            self.address = "AA:BB:CC:DD:EE:11"

    ble_client.bleak_client = cast(Any, _Backend())
    ble_client.address = "AA:BB:CC:DD:EE:10"
    monkeypatch.setattr(
        ble_client,
        "_async_await",
        _make_run_awaitable(captured_timeout),
    )

    ble_client.connect(await_timeout=7.5)

    assert captured_timeout == [7.5]
    assert ble_client.address == "AA:BB:CC:DD:EE:11"


@pytest.mark.unit
def test_bleclient_pair_uses_bounded_default_await_timeout(
    monkeypatch: pytest.MonkeyPatch,
    ble_client: BLEClient,
) -> None:
    """pair() should use the named bounded await timeout by default."""
    captured_timeout: list[float | None] = []

    class _Backend:
        async def pair(self, **_kwargs: object) -> None:
            pass

    ble_client.bleak_client = cast(Any, _Backend())
    monkeypatch.setattr(
        ble_client,
        "_async_await",
        _make_run_awaitable(captured_timeout),
    )

    ble_client.pair()
    assert captured_timeout == [BLECLIENT_MANAGEMENT_AWAIT_TIMEOUT]


@pytest.mark.unit
def test_bleclient_pair_translates_not_implemented_error(
    monkeypatch: pytest.MonkeyPatch,
    ble_client: BLEClient,
) -> None:
    """pair() should wrap backend NotImplementedError as unsupported."""

    class _Backend:
        async def pair(self, **_kwargs: object) -> None:
            raise NotImplementedError

    ble_client.bleak_client = cast(Any, _Backend())
    monkeypatch.setattr(ble_client, "_async_await", _make_run_awaitable())

    with pytest.raises(
        BLEClient.BLEError, match=BLECLIENT_ERROR_CANNOT_PAIR_UNSUPPORTED
    ) as exc_info:
        ble_client.pair(confirm=True)

    assert isinstance(exc_info.value.__cause__, NotImplementedError)


@pytest.mark.unit
def test_bleclient_pair_translates_wrapped_not_implemented_error(
    monkeypatch: pytest.MonkeyPatch,
    ble_client: BLEClient,
) -> None:
    """pair() should preserve the unsupported contract when _async_await wraps NotImplementedError."""

    class _Backend:
        async def pair(self, **_kwargs: object) -> None:
            raise NotImplementedError("backend unsupported")

    ble_client.bleak_client = cast(Any, _Backend())
    monkeypatch.setattr(ble_client, "_async_await", _raise_wrapped_not_implemented)

    with pytest.raises(
        BLEClient.BLEError, match=BLECLIENT_ERROR_CANNOT_PAIR_UNSUPPORTED
    ) as exc_info:
        ble_client.pair(confirm=True)

    assert isinstance(exc_info.value.__cause__, NotImplementedError)


@pytest.mark.unit
@pytest.mark.parametrize("method_name", ["pair", "unpair"])
@pytest.mark.parametrize(
    "invalid_timeout",
    [None, 0.0, -1.0, float("nan"), float("inf"), float("-inf"), True],
)
def test_bleclient_management_rejects_invalid_await_timeout(
    ble_client: BLEClient,
    method_name: str,
    invalid_timeout: object,
) -> None:
    """pair()/unpair() should require a finite positive await timeout."""
    backend_calls = {"pair": 0, "unpair": 0}

    class _Backend:
        async def pair(self, **_kwargs: object) -> None:
            backend_calls["pair"] += 1

        async def unpair(self) -> None:
            backend_calls["unpair"] += 1

    ble_client.bleak_client = cast(Any, _Backend())
    invoke: Callable[[], None]
    if method_name == "pair":
        invoke = partial(
            ble_client.pair,
            confirm=True,
            await_timeout=cast(Any, invalid_timeout),
        )
    else:
        invoke = partial(ble_client.unpair, await_timeout=cast(Any, invalid_timeout))

    with pytest.raises(
        BLEClient.BLEError,
        match=re.escape(ERROR_MANAGEMENT_AWAIT_TIMEOUT_INVALID),
    ):
        invoke()

    assert backend_calls == {"pair": 0, "unpair": 0}
