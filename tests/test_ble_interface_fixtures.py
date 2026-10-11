"""Common fixtures for BLE interface tests."""

import logging
import math
import sys
import types
from collections.abc import Callable, Generator
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from meshtastic.interfaces.ble.constants import (
    BLECLIENT_ERROR_CANNOT_PAIR_NOT_INITIALIZED,
    BLECLIENT_ERROR_CANNOT_UNPAIR_NOT_INITIALIZED,
    BLECLIENT_MANAGEMENT_AWAIT_TIMEOUT,
    ERROR_MANAGEMENT_AWAIT_TIMEOUT_INVALID,
)

if TYPE_CHECKING:  # pragma: no cover - import only for typing
    from meshtastic.interfaces.ble.interface import BLEInterface


def _get_ble_module() -> types.ModuleType:
    """Import and return the meshtastic.interfaces.ble.interface module.

    Returns
    -------
    types.ModuleType
        The meshtastic.interfaces.ble.interface module.
    """
    import meshtastic.interfaces.ble.interface as ble_mod

    return ble_mod


def _validate_await_timeout(await_timeout: float) -> float:
    """Validate a BLE management await timeout and return it as a float."""
    if (
        isinstance(await_timeout, bool)
        or not isinstance(await_timeout, (int, float))
        or not math.isfinite(await_timeout)
        or await_timeout <= 0
    ):
        raise _get_ble_module().BLEClient.BLEError(
            ERROR_MANAGEMENT_AWAIT_TIMEOUT_INVALID
        )
    return float(await_timeout)


@pytest.fixture
def mock_serial(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Inject a minimal fake `serial` package into sys.modules for tests.

    The injected package provides:
    - serial.tools.list_ports.comports() -> []
    - SerialException and SerialTimeoutException mapped to Exception
    - entries for "serial", "serial.tools", and "serial.tools.list_ports" in sys.modules

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch

    Returns
    -------
    types.ModuleType
        The mocked `serial` module object that was inserted into sys.modules.
    """
    serial_module = types.ModuleType("serial")

    # Create tools submodule
    tools_module = types.ModuleType("serial.tools")
    list_ports_module = types.ModuleType("serial.tools.list_ports")
    cast(Any, list_ports_module).comports = lambda *_args, **_kwargs: []
    cast(Any, tools_module).list_ports = list_ports_module
    cast(Any, serial_module).tools = tools_module

    # Add exception classes
    cast(Any, serial_module).SerialException = Exception
    cast(Any, serial_module).SerialTimeoutException = Exception

    # Mock the modules
    monkeypatch.setitem(sys.modules, "serial", serial_module)
    monkeypatch.setitem(sys.modules, "serial.tools", tools_module)
    monkeypatch.setitem(sys.modules, "serial.tools.list_ports", list_ports_module)

    return serial_module


@pytest.fixture
def mock_pubsub(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Injects a fake `pubsub` module into sys.modules for tests.

    The injected module exposes a `pub` attribute (a SimpleNamespace) with
    `subscribe` and `sendMessage` no-op callables and `AUTO_TOPIC` set to None.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Fixture used to inject the mock module.

    Returns
    -------
    types.ModuleType
        The injected `pubsub` module.
    """
    pubsub_module = types.ModuleType("pubsub")
    cast(Any, pubsub_module).pub = SimpleNamespace(
        subscribe=lambda *_args, **_kwargs: None,
        sendMessage=lambda *_args, **_kwargs: None,
        AUTO_TOPIC=None,
    )

    monkeypatch.setitem(sys.modules, "pubsub", pubsub_module)
    return pubsub_module


@pytest.fixture
def mock_publishing_thread(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Provide a synchronous test stub for the publishingThread module and install it into sys.modules.

    The stub exposes a queueWork(callback) callable that invokes the provided callback immediately if it is truthy. The mocked module is registered under both "publishingThread" and "meshtastic.publishingThread".

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Fixture used to inject the mock module.

    Returns
    -------
    types.ModuleType
        The mocked publishingThread module inserted into sys.modules.
    """
    publishing_thread_module = types.ModuleType("publishingThread")

    def queueWork(callback: Callable[[], Any] | None) -> None:
        """Execute the provided callback immediately.

        Parameters
        ----------
        callback : callable | None
            Callable to invoke; no action is taken if `callback` is `None` or otherwise falsy.
        """
        if callback:
            callback()

    cast(Any, publishing_thread_module).queueWork = queueWork

    monkeypatch.setitem(sys.modules, "publishingThread", publishing_thread_module)
    monkeypatch.setitem(
        sys.modules,
        "meshtastic.publishingThread",
        publishing_thread_module,
    )
    return publishing_thread_module


@pytest.fixture
def mock_tabulate(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Install a minimal fake `tabulate` module into sys.modules for tests.

    The fake module exposes a `tabulate(*args, **kwargs)` function that always returns an empty string.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Fixture used to inject the mock module.

    Returns
    -------
    types.ModuleType
        The fake `tabulate` module inserted into `sys.modules`.
    """
    tabulate_module = types.ModuleType("tabulate")
    cast(Any, tabulate_module).tabulate = lambda *_args, **_kwargs: ""

    monkeypatch.setitem(sys.modules, "tabulate", tabulate_module)
    return tabulate_module


@pytest.fixture
def mock_bleak(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Create and install a minimal fake `bleak` package into sys.modules for tests.

    The injected module supplies lightweight test doubles compatible with production imports:
    `BleakClient` (async connect/disconnect/read/write/start_notify, sync is_connected),
    `BleakScanner.discover()` (async, returns an empty list),
    `BLEDevice` (simple type with `address` and `name`),
    and `bleak.backends.device.BLEDevice` for compatibility.

    Returns
    -------
    types.ModuleType
        The fake `bleak` module object inserted into `sys.modules`.
    """
    bleak_module = types.ModuleType("bleak")
    cast(Any, bleak_module).__version__ = "2.1.1"

    class _StubBleakClient:
        """Minimal BleakClient test double.

        Methods
        -------
        connect(**_kwargs)
        disconnect(**_kwargs)
        start_notify(*_args, **_kwargs)
        read_gatt_char(*_args, **_kwargs)
        write_gatt_char(*_args, **_kwargs)
        is_connected()
        """

        def __init__(self, address: object | None = None, **_kwargs: object) -> None:
            """Initialize a minimal test BLE client with an associated address and a lightweight services shim.

            Parameters
            ----------
            address : object | None
                BLE device address or BLEDevice-like object associated with
                this client, or None. If the value exposes an `address`
                attribute, that address string is used. (Default value = None)
                Additional keyword arguments are accepted and ignored.

            Attributes
            ----------
            services : types.SimpleNamespace
                Exposes get_characteristic(specifier) which always returns None.
            """
            self.address = cast(str | None, getattr(address, "address", address))
            self.services = SimpleNamespace(get_characteristic=lambda _specifier: None)
            self._connected = False

        async def connect(self, **_kwargs: object) -> None:
            """Stub connect used in tests that ignores any keyword arguments.

            This no-op implementation accepts arbitrary keyword arguments and performs no action.

            Parameters
            ----------
            _kwargs : dict
                Arbitrary keyword arguments that are ignored.

            Returns
            -------
            None
                Always returns None.
            """
            self._connected = True
            return None

        async def disconnect(self, **_kwargs: object) -> None:
            """No-op disconnect that ignores any provided keyword arguments."""
            self._connected = False
            return None

        async def start_notify(self, *_args: object, **_kwargs: object) -> None:
            """Compatibility shim for the bleak start_notify API.

            Accepts and ignores positional and keyword arguments typically passed to `start_notify` (char_specifier, callback, *args, **kwargs) and performs no action.
            """
            return None

        async def stop_notify(self, *_args: object, **_kwargs: object) -> None:
            """Compatibility shim for the bleak stop_notify API."""
            return None

        async def read_gatt_char(self, *_args: object, **_kwargs: object) -> bytes:
            """Provide an empty value for any GATT characteristic read.

            Returns
            -------
            bytes
                `b""` (empty bytes).
            """
            return b""

        async def write_gatt_char(self, *_args: object, **_kwargs: object) -> None:
            """Accept any positional and keyword arguments and perform no action."""
            return None

        async def pair(self, **_kwargs: object) -> None:
            """No-op pairing hook for compatibility with BLEClient.pair()."""
            return None

        async def unpair(self, **_kwargs: object) -> None:
            """No-op unpair hook for compatibility with BLEClient.unpair()."""
            return None

        @property
        def is_connected(self) -> bool:
            """Report whether the client is connected.

            Returns
            -------
            bool
                Current connection state of this test double.
            """
            return self._connected

    async def _stub_discover(**_kwargs: object) -> list[Any]:
        """Simulate BLE device discovery for tests when no devices are present.

        Parameters
        ----------
        **_kwargs : dict
            Ignored keyword arguments kept for API compatibility.

        Returns
        -------
        list
            Empty list of discovered devices.
        """
        return []

    class _StubBLEDevice:
        """Minimal BLEDevice test double."""

        def __init__(
            self,
            address: str | None = None,
            name: str | None = None,
            details: object | None = None,
            **_kwargs: object,
        ) -> None:
            """Create a minimal BLE device representation with optional metadata.

            Parameters
            ----------
            address : str | None
                The device's BLE address, or None if unknown. (Default value = None)
            name : str | None
                The device's human-readable name, or None if unknown. (Default value = None)
            details : object | None
                Optional backend-specific details payload. (Default value = None)
            **_kwargs : object
                Additional constructor arguments accepted for API compatibility.
            """
            self.address = address
            self.name = name
            self.details = details

    class _StubBleakScanner:
        """Minimal BleakScanner test double."""

        def __init__(self) -> None:
            """Initialize a BleakScanner stub that reports no BLE devices."""

        @staticmethod
        async def discover(**_kwargs: object) -> list[Any]:
            """Simulate BLE device discovery.

            Returns
            -------
            list
                Empty list of discovered BLE devices.
            """
            return await _stub_discover(**_kwargs)

    cast(Any, bleak_module).BleakClient = _StubBleakClient
    cast(Any, bleak_module).BleakScanner = _StubBleakScanner
    cast(Any, bleak_module).BLEDevice = _StubBLEDevice
    # Mark as package so submodules work
    cast(Any, bleak_module).__path__ = []

    # Provide bleak.backends.device.BLEDevice for production imports
    bleak_backends_module = types.ModuleType("bleak.backends")
    cast(Any, bleak_backends_module).__path__ = []
    bleak_backends_device_module = types.ModuleType("bleak.backends.device")
    cast(Any, bleak_backends_device_module).BLEDevice = _StubBLEDevice
    cast(Any, bleak_backends_module).device = bleak_backends_device_module
    cast(Any, bleak_module).backends = bleak_backends_module

    monkeypatch.setitem(sys.modules, "bleak", bleak_module)
    monkeypatch.setitem(sys.modules, "bleak.backends", bleak_backends_module)
    monkeypatch.setitem(
        sys.modules, "bleak.backends.device", bleak_backends_device_module
    )
    return bleak_module


@pytest.fixture
def mock_bleak_exc(
    monkeypatch: pytest.MonkeyPatch, mock_bleak: types.ModuleType
) -> types.ModuleType:  # pylint: disable=redefined-outer-name
    """Create and register a minimal `bleak.exc` submodule exposing BLE error types.

    The created module is attached to the provided `mock_bleak` as its `exc` attribute and inserted into `sys.modules` under the name `"bleak.exc"`.

    Returns
    -------
        The created `bleak.exc` module.
    """
    bleak_exc_module: types.ModuleType = types.ModuleType("bleak.exc")

    class _StubBleakError(Exception):
        """Stub BleakError type for tests."""

    class _StubBleakDBusError(_StubBleakError):
        """Stub BleakDBusError type for tests."""

    class _StubBleakDeviceNotFoundError(_StubBleakError):
        """Stub BleakDeviceNotFoundError type for tests."""

    class _StubBleakGATTProtocolError(_StubBleakError):
        """Stub BleakGATTProtocolError type for tests."""

    bleak_exc_module_any = cast(Any, bleak_exc_module)
    bleak_exc_module_any.BleakError = _StubBleakError
    bleak_exc_module_any.BleakDBusError = _StubBleakDBusError
    bleak_exc_module_any.BleakDeviceNotFoundError = _StubBleakDeviceNotFoundError
    bleak_exc_module_any.BleakGATTProtocolError = _StubBleakGATTProtocolError

    # Attach to parent module
    cast(Any, mock_bleak).exc = bleak_exc_module

    monkeypatch.setitem(sys.modules, "bleak.exc", bleak_exc_module)
    return bleak_exc_module


class DummyClient:
    """Dummy client for testing BLE interface functionality."""

    def __init__(
        self,
        disconnect_exception: Exception | None = None,
        *,
        on_unpair: Callable[[], None] | None = None,
        initialized: bool = True,
    ) -> None:
        """Create a test-double BLE client used by unit tests.

        Parameters
        ----------
        disconnect_exception : Exception | None
            Exception to raise when disconnect() is called; pass None to disable raising.
        on_unpair : Callable[[], None] | None
            Optional hook invoked after recording an unpair() call. Tests can
            use this to simulate disconnect cleanup triggered by backend
            unpair semantics.
        initialized : bool
            If False, initialize without a backing bleak_client so tests can
            exercise "not initialized" pair/unpair paths.

        Attributes
        ----------
        disconnect_calls : int
            Number of times disconnect() has been invoked.
        close_calls : int
            Number of times close() has been invoked.
        pair_calls : int
            Number of times pair() has been invoked.
        unpair_calls : int
            Number of times unpair() has been invoked.
        pair_kwargs : list[dict[str, object]]
            Backend keyword arguments captured for each pair() invocation.
        pair_await_timeouts : list[float | None]
            Timeout arguments captured for each pair() invocation.
        unpair_await_timeouts : list[float | None]
            Timeout arguments captured for each unpair() invocation.
        unpair_kwargs : list[dict[str, object]]
            Placeholder kwargs entries for each unpair() invocation. Values are
            always empty dicts because production `BLEClient.unpair()` does not
            accept backend-specific kwargs.
        address : str
            Client address identifier, set to "dummy".
        disconnect_exception : Exception | None
            Stored exception raised by disconnect(), if any. (Default value = None)
        on_unpair : Callable[[], None] | None
            Optional callback invoked after unpair() records its call.
        services : types.SimpleNamespace
            Provides get_characteristic(specifier) -> None for characteristic lookups.
        bleak_client : types.SimpleNamespace
            Minimal mock of a bleak client with an address attribute used for identity checks.
        """
        self.disconnect_calls = 0
        self.close_calls = 0
        self.pair_calls = 0
        self.unpair_calls = 0
        self.pair_kwargs: list[dict[str, object]] = []
        self.unpair_kwargs: list[dict[str, object]] = []
        self.pair_await_timeouts: list[float | None] = []
        self.unpair_await_timeouts: list[float | None] = []
        self.stop_notify_calls: list[Any] = []
        self.address = "dummy"
        self.disconnect_exception = disconnect_exception
        self.on_unpair = on_unpair
        self.services = SimpleNamespace(get_characteristic=lambda _specifier: None)
        self._initialized = initialized
        # The bleak_client should be a separate object to correctly test identity checks
        self.bleak_client = (
            SimpleNamespace(address=self.address) if initialized else None
        )

    def has_characteristic(self, _specifier: Any) -> bool:
        """Report whether this mock client exposes a BLE characteristic matching the given specifier.

        Parameters
        ----------
        _specifier
            Identifier of the characteristic to check (for example, a UUID string or characteristic object).

        Returns
        -------
        bool
            True if the client exposes a characteristic matching `_specifier`, False otherwise. This mock always returns False.
        """
        return False

    def start_notify(self, *_args: object, **_kwargs: object) -> None:
        """Simulate subscribing to a BLE characteristic notification for tests.

        Accepts any arguments and performs no action.
        """
        return None

    def stopNotify(self, *args: object, **_kwargs: object) -> None:
        """Simulate unsubscribing from BLE notifications during tests.

        When a positional characteristic argument is provided, appends the first positional argument to the instance's stop_notify_calls list for later inspection.
        """
        if args:
            self.stop_notify_calls.append(args[0])
        return None

    def stop_notify(self, *args: object, **kwargs: object) -> None:
        """Backward-compatible snake_case alias for stopNotify."""
        return self.stopNotify(*args, **kwargs)

    def read_gatt_char(self, *_args: object, **_kwargs: object) -> bytes:
        """Provide a fixed empty-bytes response for any GATT characteristic read.

        Returns
        -------
        bytes
            Empty bytes object (`b''`).
        """
        return b""

    def isConnected(self) -> bool:
        """Indicate whether the mock BLE client is currently connected.

        Returns
        -------
        bool
            True if the client is connected, False otherwise.
        """
        return self._initialized and self.bleak_client is not None

    def is_connected(self) -> bool:
        """Backward-compatible snake_case alias for isConnected.

        Returns
        -------
        bool
            Connection state reported by isConnected().
        """
        return self.isConnected()

    def disconnect(self, *_args: object, **_kwargs: object) -> None:
        """Record a disconnect invocation and optionally raise a configured exception.

        Increments the instance's `disconnect_calls` counter. If `disconnect_exception` is set, that exception is raised instead of returning normally.

        Raises
        ------
        Exception
            The exception instance stored in `self.disconnect_exception`, if present.
        """
        self.disconnect_calls += 1
        self._initialized = False
        self.bleak_client = None
        if self.disconnect_exception:
            raise self.disconnect_exception

    def pair(
        self,
        *,
        await_timeout: float = BLECLIENT_MANAGEMENT_AWAIT_TIMEOUT,
        **_kwargs: object,
    ) -> None:
        """Record a pair invocation."""
        await_timeout = _validate_await_timeout(await_timeout)
        if not self._initialized or self.bleak_client is None:
            raise _get_ble_module().BLEClient.BLEError(
                BLECLIENT_ERROR_CANNOT_PAIR_NOT_INITIALIZED
            )
        self.pair_calls += 1
        self.pair_await_timeouts.append(await_timeout)
        self.pair_kwargs.append(dict(_kwargs))

    def unpair(
        self,
        *,
        await_timeout: float = BLECLIENT_MANAGEMENT_AWAIT_TIMEOUT,
    ) -> None:
        """Record an unpair invocation."""
        await_timeout = _validate_await_timeout(await_timeout)
        if not self._initialized or self.bleak_client is None:
            raise _get_ble_module().BLEClient.BLEError(
                BLECLIENT_ERROR_CANNOT_UNPAIR_NOT_INITIALIZED
            )
        self.unpair_calls += 1
        self.unpair_await_timeouts.append(await_timeout)
        self.unpair_kwargs.append({})
        if self.on_unpair is not None:
            self.on_unpair()

    def close(self, timeout: float | None = None) -> None:
        """Release the transport and record that the client was closed.

        This mirrors ``BLEClient.close()`` ownership: when a transport exists,
        close performs a best-effort disconnect before clearing client state.

        Parameters
        ----------
        timeout : float | None
            Optional close-time disconnect timeout. The synchronous test double
            accepts but does not enforce it.
        """
        self.close_calls += 1
        if self.bleak_client is not None:
            try:
                self.disconnect(await_timeout=timeout)
            except Exception:  # noqa: BLE001 - mirror best-effort production close
                pass
        self._initialized = False
        self.bleak_client = None

    def _get_services(self) -> SimpleNamespace:
        """Stub for _get_services."""
        return self.services


@pytest.fixture
def stub_atexit(
    monkeypatch: pytest.MonkeyPatch,
    mock_serial: types.ModuleType,  # pylint: disable=redefined-outer-name
    mock_pubsub: types.ModuleType,  # pylint: disable=redefined-outer-name
    mock_tabulate: types.ModuleType,  # pylint: disable=redefined-outer-name
    mock_bleak: types.ModuleType,  # pylint: disable=redefined-outer-name
    mock_bleak_exc: types.ModuleType,  # pylint: disable=redefined-outer-name
    mock_publishing_thread: types.ModuleType,  # pylint: disable=redefined-outer-name
) -> Generator[None, None, None]:
    """Replace the BLE interface module's atexit.register/unregister with test stubs that record callbacks and invoke them at fixture teardown.

    The fixture patches meshtastic.interfaces.ble.interface.atexit.register and .unregister so that registered callables are appended to an internal list and later executed when the fixture tears down. Any exceptions raised by those callbacks are caught and logged at debug level; execution continues for remaining callbacks.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Fixture used to apply the atexit monkeypatches.
    mock_serial, mock_pubsub, mock_tabulate, mock_bleak, mock_bleak_exc, mock_publishing_thread : types.ModuleType
        Ordering-only fixture dependencies; values are intentionally unused.
    """
    registered: list[Callable[[], object]] = []
    # Consume fixture arguments to document ordering intent and silence Ruff (ARG001).
    _ = (
        mock_serial,
        mock_pubsub,
        mock_tabulate,
        mock_bleak,
        mock_bleak_exc,
        mock_publishing_thread,
    )

    def fake_register(func: Callable[[], object]) -> Callable[[], object]:
        """Register a callable to be invoked later during teardown.

        Parameters
        ----------
        func : callable
            The callable to register; may also be used as a decorator.

        Returns
        -------
        callable
            The same callable that was registered.
        """
        registered.append(func)
        return func

    def fake_unregister(func: Callable[[], object]) -> None:
        """Unregisters all callbacks identical to the given function from the module-level registration list.

        Removes every entry whose identity matches `func` (comparison is by `is`, not by equality).

        Parameters
        ----------
        func : Any
            The callable to remove from the `registered` list; all entries that are the same object as `func` will be removed.
        """
        registered[:] = [f for f in registered if f is not func]

    ble_mod = _get_ble_module()
    monkeypatch.setattr(ble_mod.atexit, "register", fake_register, raising=True)
    monkeypatch.setattr(ble_mod.atexit, "unregister", fake_unregister, raising=True)
    yield
    # run any registered functions manually to avoid surprising global state
    for func in list(registered):
        try:
            func()
        except Exception as e:  # noqa: BLE001 - teardown should log but continue
            logging.debug(
                "atexit callback %r raised during teardown: %s: %s",
                func,
                type(e).__name__,
                e,
            )


def _build_interface(
    monkeypatch: pytest.MonkeyPatch,
    client: DummyClient,
    *,
    start_receive_thread: bool = True,
) -> "BLEInterface":
    """Create a BLEInterface configured for tests whose `connect` is stubbed to return the supplied client and whose `_start_config` is a no-op.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        pytest monkeypatch fixture used to patch BLEInterface methods.
    client : DummyClient
        Fake or mock BLE client instance that the stubbed `connect` will return.
    start_receive_thread : bool
        If False, patches BLEInterface to skip starting
        the background receive thread so tests can invoke receive paths
        synchronously. (Default value = True)

    Returns
    -------
    'BLEInterface'
        A BLEInterface instance configured for tests. The instance has `connect`
        replaced to attach and return `client`, `_start_config` replaced with a
        no-op, `_connect_stub_calls` recording addresses passed to the stubbed
        `connect`, and `_connect_stub_kwargs` recording keyword arguments.
    """
    ble_mod = _get_ble_module()
    base_interface_class: Any = ble_mod.BLEInterface

    class _TestBLEInterface(base_interface_class):  # type: ignore[misc]
        """Per-build BLEInterface subclass to isolate monkeypatched methods."""

    connect_calls: list[str | None] = []
    connect_call_kwargs: list[dict[str, object]] = []

    def _stub_connect(
        _self: "BLEInterface",
        _address: str | None = None,
        *args: object,
        **kwargs: object,
    ) -> "DummyClient":
        """Attach the prepared test client to a BLEInterface-like instance and record the connection attempt.

        Appends `_address` to the `connect_calls` list, sets `_self.client` to the prepared test client, clears `_self._disconnect_notified`, and triggers `_self._reconnected_event.set()` if `_reconnected_event` exists.

        Parameters
        ----------
        _self : BLEInterface
            BLEInterface-like instance being patched.
        _address : str | None
            Address passed to connect; recorded in the module-level `connect_calls`.
            Additional positional arguments (ignored).
            Additional keyword arguments (ignored).

        Returns
        -------
        'DummyClient'
            The preconfigured test client instance attached to the interface.
        """
        _ = args
        connect_calls.append(_address)
        connect_call_kwargs.append(dict(kwargs))
        requested_identifier = _address if _address is not None else _self.address
        # Rehydrate reused test clients so reconnect paths mirror production:
        # disconnect()/close() clear these fields.
        client._initialized = True
        updated_address = False
        if (
            _address is not None
            and _address != _self.address
            and ble_mod._looks_like_ble_address(_address)
        ):
            client.address = _address
            updated_address = True
        if client.bleak_client is None:
            client.bleak_client = SimpleNamespace(address=client.address)
        elif updated_address:
            client.bleak_client.address = client.address
        with _self._state_lock:
            was_disconnected = _self._disconnect_notified
            _self.address = client.address
            _self._last_connection_request = ble_mod.sanitize_address(
                requested_identifier
            )
            _self.client = cast(Any, client)
            _self._disconnect_notified = False
            _self._client_publish_pending = False
            _self._state_manager._transition_to(ble_mod.ConnectionState.CONNECTED)
            _self._ever_connected = True
            if was_disconnected and hasattr(_self, "_reconnected_event"):
                _self._reconnected_event.set()
        return client

    def _stub_start_config(_self: "BLEInterface") -> None:
        """Stub replacement for BLEInterface._start_config that performs no startup configuration.

        Used in tests to prevent side effects during BLEInterface initialization.

        Returns
        -------
        None
        """
        return None

    def _stub_start_receive_thread(
        _self: "BLEInterface",
        *,
        name: str,
        reset_recovery: bool = True,
    ) -> None:
        """No-op replacement for BLEInterface._start_receive_thread.

        Accepts the same parameters as the production method but performs no action; parameters are kept for signature compatibility.

        Parameters
        ----------
        name : str
            Thread name (unused; kept for signature compatibility).
        reset_recovery : bool
            Whether recovery counters should be reset. Unused in tests.

        Returns
        -------
        None
        """
        _ = (_self, name, reset_recovery)
        return None

    monkeypatch.setattr(_TestBLEInterface, "connect", _stub_connect)
    monkeypatch.setattr(_TestBLEInterface, "_start_config", _stub_start_config)
    if not start_receive_thread:
        monkeypatch.setattr(
            _TestBLEInterface, "_start_receive_thread", _stub_start_receive_thread
        )
    iface = _TestBLEInterface(address="dummy", noProto=True)
    cast(Any, iface)._connect_stub_calls = connect_calls
    cast(Any, iface)._connect_stub_kwargs = connect_call_kwargs
    return iface


@pytest.fixture
def build_interface(
    monkeypatch: pytest.MonkeyPatch,
    stub_atexit: None,
) -> Callable[..., "BLEInterface"]:
    """Return a fixture-backed factory for creating configured BLEInterface instances."""
    _ = stub_atexit
    return lambda client, **kwargs: _build_interface(monkeypatch, client, **kwargs)
