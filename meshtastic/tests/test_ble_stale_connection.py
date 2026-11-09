"""Hermetic BlueZ stale-connection clearance tests (no live D-Bus or radio)."""

from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
import types
from collections.abc import Generator
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meshtastic.interface_proxy import channels as proxy_channels_mod
from meshtastic.interface_proxy.channels import BLECentralChannel
from meshtastic.interfaces.ble import interface as ble_interface_mod
from meshtastic.interfaces.ble import stale_connection
from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.constants import (
    AWAIT_TIMEOUT_BUFFER_SECONDS,
    BLEConfig,
)
from meshtastic.interfaces.ble.errors import BLEConnectionSuppressedError
from meshtastic.interfaces.ble.gating import (
    _addr_lock_context,
    _clear_all_registries,
    _is_currently_connected_elsewhere,
)
from meshtastic.interfaces.ble.interface import BLEInterface
from meshtastic.interfaces.ble.state import BLEStateManager, ConnectionState

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _linux_clearance_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the module's platform probe to Linux for hermetic D-Bus unit cases.

    The shipped ``BLEConfig.CLEAR_STALE_CONNECTIONS`` default is deliberately
    left observable: tests that exercise the toggle patch it themselves.
    """
    monkeypatch.setattr(
        stale_connection, "sys", types.SimpleNamespace(platform="linux")
    )
    monkeypatch.setattr(
        proxy_channels_mod, "_clear_stale_connection", AsyncMock(return_value=False)
    )


_TARGET = "AA:BB:CC:DD:EE:FF"
_DEVICE_PATH_HCI0 = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
_DEVICE_PATH_HCI1 = "/org/bluez/hci1/dev_AA_BB_CC_DD_EE_FF"


def _device_entry(address: str, connected: bool) -> dict[str, dict[str, Any]]:
    """Build one ``org.bluez.Device1`` ObjectManager snapshot entry."""
    return {
        "org.bluez.Device1": {
            "Address": types.SimpleNamespace(value=address),
            "Connected": types.SimpleNamespace(value=connected),
        }
    }


def _managed_objects_reply(devices: dict[str, dict[str, dict[str, Any]]]) -> Any:
    """Build a GetManagedObjects reply-shaped object for the fake bus."""
    return types.SimpleNamespace(
        message_type="reply",
        body=[devices],
    )


def _fake_dbus(  # pylint: disable=too-many-arguments
    monkeypatch: pytest.MonkeyPatch,
    *,
    replies: list[Any] | None = None,
    call_error: Exception | None = None,
    hang_call: bool = False,
    bus_connect_error: Exception | None = None,
    bus_constructor_error: Exception | None = None,
) -> tuple[MagicMock, MagicMock]:
    """Expose the narrow dbus-fast contract without a live system bus.

    Returns the ``Message`` constructor mock (whose ``call_args_list`` records
    the destination/path/interface/member of every message) and the fake bus
    (whose ``call`` records each request).
    """
    bus = MagicMock()
    bus.connect = AsyncMock()
    if bus_connect_error is not None:
        bus.connect.side_effect = bus_connect_error
    if hang_call:

        async def _hang(*_args: Any, **_kwargs: Any) -> Any:
            await asyncio.sleep(3600)

        bus.call = AsyncMock(side_effect=_hang)
    elif call_error is not None:
        bus.call = AsyncMock(side_effect=call_error)
    else:
        bus.call = AsyncMock(side_effect=list(replies or []))
    message = MagicMock()
    backend: Any = types.ModuleType("dbus_fast")
    backend.Message = message
    backend.MessageType = types.SimpleNamespace(ERROR="error")
    aio: Any = types.ModuleType("dbus_fast.aio")
    aio.MessageBus = (
        MagicMock(side_effect=bus_constructor_error)
        if bus_constructor_error is not None
        else MagicMock(return_value=bus)
    )
    constants: Any = types.ModuleType("dbus_fast.constants")
    constants.BusType = types.SimpleNamespace(SYSTEM="system")
    errors: Any = types.ModuleType("dbus_fast.errors")

    class DBusError(Exception):
        """Stand-in for ``dbus_fast.errors.DBusError``."""

    errors.DBusError = DBusError
    monkeypatch.setitem(sys.modules, "dbus_fast", backend)
    monkeypatch.setitem(sys.modules, "dbus_fast.aio", aio)
    monkeypatch.setitem(sys.modules, "dbus_fast.constants", constants)
    monkeypatch.setitem(sys.modules, "dbus_fast.errors", errors)
    return message, bus


def _assert_disconnect_message(message: MagicMock, expected_path: str) -> None:
    """Assert the second D-Bus message is a Device1.Disconnect on ``expected_path``."""
    assert message.call_args_list[1].kwargs["destination"] == "org.bluez"
    assert message.call_args_list[1].kwargs["path"] == expected_path
    assert message.call_args_list[1].kwargs["interface"] == "org.bluez.Device1"
    assert message.call_args_list[1].kwargs["member"] == "Disconnect"


def test_clear_stale_connection_disconnects_connected_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A BlueZ-connected target gets Device1.Disconnect on its object path."""
    reply = _managed_objects_reply({_DEVICE_PATH_HCI0: _device_entry(_TARGET, True)})
    message, bus = _fake_dbus(
        monkeypatch,
        replies=[reply, types.SimpleNamespace(message_type="reply", body=[])],
    )
    assert asyncio.run(stale_connection._clear_stale_connection(_TARGET)) is True
    _assert_disconnect_message(message, _DEVICE_PATH_HCI0)
    bus.disconnect.assert_called_once()


def test_clear_stale_connection_skips_disconnect_when_not_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A target BlueZ reports as not connected is never sent a Disconnect."""
    reply = _managed_objects_reply({_DEVICE_PATH_HCI0: _device_entry(_TARGET, False)})
    _message, bus = _fake_dbus(monkeypatch, replies=[reply])
    assert asyncio.run(stale_connection._clear_stale_connection(_TARGET)) is False
    bus.call.assert_awaited_once()
    bus.disconnect.assert_called_once()


def test_clear_stale_connection_prefers_requested_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit adapter selects that controller's device instance."""
    reply = _managed_objects_reply(
        {
            _DEVICE_PATH_HCI0: _device_entry(_TARGET, True),
            _DEVICE_PATH_HCI1: _device_entry(_TARGET, True),
        }
    )
    message, _bus = _fake_dbus(
        monkeypatch,
        replies=[reply, types.SimpleNamespace(message_type="reply", body=[])],
    )
    assert (
        asyncio.run(stale_connection._clear_stale_connection(_TARGET, adapter="hci1"))
        is True
    )
    _assert_disconnect_message(message, _DEVICE_PATH_HCI1)


def test_clear_stale_connection_without_adapter_is_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same address on two adapters always picks the sorted-first path."""
    reply = _managed_objects_reply(
        {
            _DEVICE_PATH_HCI1: _device_entry(_TARGET, True),
            _DEVICE_PATH_HCI0: _device_entry(_TARGET, True),
        }
    )
    message, _bus = _fake_dbus(
        monkeypatch,
        replies=[reply, types.SimpleNamespace(message_type="reply", body=[])],
    )
    assert asyncio.run(stale_connection._clear_stale_connection(_TARGET)) is True
    _assert_disconnect_message(message, _DEVICE_PATH_HCI0)


def test_clear_stale_connection_swallows_dbus_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A D-Bus error is logged at debug and reported as False, never raised."""
    _message, bus = _fake_dbus(monkeypatch)
    # Raise the actual type imported inside the function, not an arbitrary exception.
    error = sys.modules["dbus_fast.errors"].DBusError("BlueZ unavailable")
    bus.call = AsyncMock(side_effect=error)
    with caplog.at_level("DEBUG", logger="meshtastic.ble"):
        assert asyncio.run(stale_connection._clear_stale_connection(_TARGET)) is False
    assert any(record.levelname == "DEBUG" for record in caplog.records)


def test_clear_stale_connection_swallows_missing_bus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unavailable system bus is a best-effort False, never an error."""
    _message, _bus = _fake_dbus(
        monkeypatch, bus_constructor_error=OSError("no system bus")
    )
    assert asyncio.run(stale_connection._clear_stale_connection(_TARGET)) is False


def test_clear_stale_connection_swallows_bus_connect_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed bus connect is a best-effort False, never an error."""
    _message, _bus = _fake_dbus(monkeypatch, bus_connect_error=OSError("blocked"))
    assert asyncio.run(stale_connection._clear_stale_connection(_TARGET)) is False


def test_clear_stale_connection_times_out_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wedged D-Bus call is bounded by the disconnect timeout constant."""
    _message, _bus = _fake_dbus(monkeypatch, hang_call=True)
    monkeypatch.setattr(BLEConfig, "STALE_CONNECTION_DISCONNECT_TIMEOUT", 0.05)
    wait_for = asyncio.wait_for
    observed_timeouts: list[float | None] = []

    async def record_wait(coro: Any, timeout: float | None = None) -> Any:
        observed_timeouts.append(timeout)
        return await wait_for(coro, timeout=timeout)

    monkeypatch.setattr(asyncio, "wait_for", record_wait)
    assert asyncio.run(stale_connection._clear_stale_connection(_TARGET)) is False
    assert observed_timeouts == [0.05]


def test_clear_stale_connection_noop_off_linux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-Linux hosts never touch D-Bus and simply report False."""
    monkeypatch.setattr(
        stale_connection, "sys", types.SimpleNamespace(platform="darwin")
    )
    message, _bus = _fake_dbus(monkeypatch)
    assert asyncio.run(stale_connection._clear_stale_connection(_TARGET)) is False
    message.assert_not_called()


def test_clear_stale_connection_toggle_disables_bus_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With CLEAR_STALE_CONNECTIONS disabled, no bus is ever opened."""
    monkeypatch.setattr(BLEConfig, "CLEAR_STALE_CONNECTIONS", False)
    message, bus = _fake_dbus(monkeypatch)
    assert asyncio.run(stale_connection._clear_stale_connection(_TARGET)) is False
    message.assert_not_called()
    bus.connect.assert_not_called()


def test_clear_stale_connection_sync_bridge_runs_on_shared_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sync bridge clears a stale link through the shared runner loop."""
    reply = _managed_objects_reply({_DEVICE_PATH_HCI0: _device_entry(_TARGET, True)})
    _message, _bus = _fake_dbus(
        monkeypatch,
        replies=[reply, types.SimpleNamespace(message_type="reply", body=[])],
    )
    assert (
        stale_connection._clear_stale_connection_before_central_connect(_TARGET) is True
    )


def test_timed_out_bridge_cannot_disconnect_a_later_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expired jobs cannot issue Device1.Disconnect when the runner unblocks."""
    import concurrent.futures

    import meshtastic.interfaces.ble.runner as runner_module

    reply = _managed_objects_reply({_DEVICE_PATH_HCI0: _device_entry(_TARGET, True)})
    message, bus = _fake_dbus(monkeypatch, replies=[reply])
    scheduled: list[Any] = []
    cancelled: list[bool] = []

    class FakeFuture:
        def result(self, timeout: float) -> bool:
            raise concurrent.futures.TimeoutError()

        def cancel(self) -> bool:
            cancelled.append(True)
            return True

    class FakeRunner:
        _thread = None

        def _run_coroutine_threadsafe(
            self, coroutine: Any, *, startup_timeout: float
        ) -> FakeFuture:
            scheduled.append(coroutine)
            return FakeFuture()

    monkeypatch.setattr(runner_module, "BLECoroutineRunner", FakeRunner)
    assert (
        stale_connection._clear_stale_connection_before_central_connect(_TARGET)
        is False
    )
    assert cancelled == [True]
    assert len(scheduled) == 1
    assert asyncio.run(scheduled[0]) is False
    assert bus.call.await_count == 1
    assert not any(
        call.kwargs.get("member") == "Disconnect" for call in message.call_args_list
    )


def test_bridge_keyboard_interrupt_fences_job_and_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KeyboardInterrupt during the wait fences the job and keeps propagating."""
    reply = _managed_objects_reply({_DEVICE_PATH_HCI0: _device_entry(_TARGET, True)})
    message, bus = _fake_dbus(monkeypatch, replies=[reply])
    import meshtastic.interfaces.ble.runner as runner_module

    scheduled: list[Any] = []
    cancelled: list[bool] = []

    class FakeFuture:
        def result(self, timeout: float) -> bool:
            raise KeyboardInterrupt()

        def cancel(self) -> bool:
            cancelled.append(True)
            return True

    class FakeRunner:
        _thread = None

        def _run_coroutine_threadsafe(
            self, coroutine: Any, *, startup_timeout: float
        ) -> FakeFuture:
            scheduled.append(coroutine)
            return FakeFuture()

    monkeypatch.setattr(runner_module, "BLECoroutineRunner", FakeRunner)
    with pytest.raises(KeyboardInterrupt):
        stale_connection._clear_stale_connection_before_central_connect(_TARGET)
    assert cancelled == [True]
    assert len(scheduled) == 1
    assert asyncio.run(scheduled[0]) is False
    assert bus.call.await_count == 1
    assert not any(
        call.kwargs.get("member") == "Disconnect" for call in message.call_args_list
    )


def test_bridge_system_exit_fences_job_and_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SystemExit(143) during the wait fences the job and keeps propagating."""
    reply = _managed_objects_reply({_DEVICE_PATH_HCI0: _device_entry(_TARGET, True)})
    message, bus = _fake_dbus(monkeypatch, replies=[reply])
    import meshtastic.interfaces.ble.runner as runner_module

    scheduled: list[Any] = []
    cancelled: list[bool] = []

    class FakeFuture:
        def result(self, timeout: float) -> bool:
            raise SystemExit(143)

        def cancel(self) -> bool:
            cancelled.append(True)
            return True

    class FakeRunner:
        _thread = None

        def _run_coroutine_threadsafe(
            self, coroutine: Any, *, startup_timeout: float
        ) -> FakeFuture:
            scheduled.append(coroutine)
            return FakeFuture()

    monkeypatch.setattr(runner_module, "BLECoroutineRunner", FakeRunner)
    with pytest.raises(SystemExit) as excinfo:
        stale_connection._clear_stale_connection_before_central_connect(_TARGET)
    assert excinfo.value.code == 143
    assert cancelled == [True]
    assert len(scheduled) == 1
    assert asyncio.run(scheduled[0]) is False
    assert bus.call.await_count == 1
    assert not any(
        call.kwargs.get("member") == "Disconnect" for call in message.call_args_list
    )


def test_runner_construction_failure_never_creates_or_leaks_coroutine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed shared-runner setup must not allocate a disposable coroutine."""
    import meshtastic.interfaces.ble.runner as runner_module

    created: list[str] = []

    def _create(*_args: Any, **_kwargs: Any) -> Any:
        created.append("created")
        return None

    def _bad_runner() -> Any:
        raise RuntimeError("runner unavailable")

    monkeypatch.setattr(stale_connection, "_clear_stale_connection", _create)
    monkeypatch.setattr(runner_module, "BLECoroutineRunner", _bad_runner)
    assert (
        stale_connection._clear_stale_connection_before_central_connect(_TARGET)
        is False
    )
    assert created == []


def test_sync_bridge_reads_live_timeout_and_swallows_failed_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed cancellation cannot escape the best-effort bridge."""
    import concurrent.futures

    import meshtastic.interfaces.ble.runner as runner_module

    observed: list[float] = []
    pending: list[Any] = []

    class FakeFuture:
        def result(self, timeout: float) -> bool:
            observed.append(timeout)
            raise concurrent.futures.TimeoutError()

        def cancel(self) -> bool:
            raise RuntimeError("runner is closing")

    class FakeRunner:
        _thread = None

        def _run_coroutine_threadsafe(
            self, coroutine: Any, *, startup_timeout: float
        ) -> FakeFuture:
            pending.append(coroutine)
            return FakeFuture()

    monkeypatch.setattr(runner_module, "BLECoroutineRunner", FakeRunner)
    monkeypatch.setattr(BLEConfig, "STALE_CONNECTION_DISCONNECT_TIMEOUT", 0.015)
    assert (
        stale_connection._clear_stale_connection_before_central_connect(_TARGET)
        is False
    )
    assert observed == [pytest.approx(0.015 + AWAIT_TIMEOUT_BUFFER_SECONDS)]
    # No real executor owns this fake; close the allocated coroutine explicitly.
    pending.pop().close()


def _no_op(*_args: Any, **_kwargs: Any) -> None:
    """Stub for connect collaborators with no observable effect."""


def _ordering_interface(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[BLEInterface, list[str]]:
    """Build a minimal interface whose connect collaborators are stubbed.

    Returns the interface and a shared call-order log; the patched module-level
    clearance bridge appends ``"clear"`` and the patched establish step appends
    ``"establish"``.
    """
    interface = BLEInterface.__new__(BLEInterface)
    state_manager = BLEStateManager()
    interface._state_manager = state_manager
    interface._state_lock = state_manager.lock
    interface._management_lock = threading.RLock()
    interface._management_idle_condition = threading.Condition()
    interface._management_inflight = 0
    interface._connect_lock = threading.RLock()
    interface._client_publish_pending = False
    interface._last_connect_pair_override = None
    interface._last_connect_timeout_override = None
    interface.pair_on_connect = False
    interface.address = _TARGET
    interface._adapter = None

    order: list[str] = []

    monkeypatch.setattr(interface, "_validate_connection_preconditions", _no_op)
    monkeypatch.setattr(interface, "_validate_connect_timeout_override", _no_op)
    monkeypatch.setattr(
        interface, "_get_existing_client_if_valid", lambda _request: None
    )
    monkeypatch.setattr(interface, "_raise_if_duplicate_connect", _no_op)
    monkeypatch.setattr(
        interface,
        "_resolve_target_address_for_connect",
        lambda _requested: _TARGET,
    )
    monkeypatch.setattr(interface, "_mark_address_keys_connecting", _no_op)
    monkeypatch.setattr(interface, "_clear_address_keys_connecting", _no_op)
    monkeypatch.setattr(interface, "_finalize_connection_gates", _no_op)
    monkeypatch.setattr(interface, "_verify_and_publish_connected", _no_op)

    sentinel_client = object()

    def _establish(
        _address: str | None,
        _normalized: str | None,
        _key: str | None,
        *,
        pair_on_connect: bool = False,
        connect_timeout: float | None = None,
    ) -> tuple[object, str | None, None]:
        _ = pair_on_connect, connect_timeout
        order.append("establish")
        return sentinel_client, "aabbccddeeff", None

    monkeypatch.setattr(interface, "_establish_and_update_client", _establish)

    def _record_clear(address: str | None, *, adapter: str | None = None) -> bool:
        _ = adapter
        order.append("clear")
        return False

    monkeypatch.setattr(
        ble_interface_mod,
        "_clear_stale_connection_before_central_connect",
        _record_clear,
    )
    return interface, order


def test_connect_clears_stale_link_before_client_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() runs the clearance before establishing the Bleak client."""
    interface, order = _ordering_interface(monkeypatch)
    monkeypatch.setattr(
        ble_interface_mod,
        "_is_currently_connected_elsewhere",
        lambda _addr, owner=None: False,
    )
    client = interface.connect(_TARGET)
    assert client is not None
    assert order == ["clear", "establish"]


def test_connect_clearance_failure_does_not_block_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clearance failure does not prevent the connect from establishing."""
    interface, order = _ordering_interface(monkeypatch)
    monkeypatch.setattr(
        ble_interface_mod,
        "_is_currently_connected_elsewhere",
        lambda _addr, owner=None: False,
    )

    def _explode(_address: str | None, *, adapter: str | None = None) -> bool:
        raise RuntimeError("clearance exploded")

    monkeypatch.setattr(
        ble_interface_mod,
        "_clear_stale_connection_before_central_connect",
        _explode,
    )
    client = interface.connect(_TARGET)
    assert client is not None
    assert order == ["establish"]


def test_connect_skips_clearance_for_discovery_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Implicit discovery never clears even after resolving a concrete address."""
    interface, order = _ordering_interface(monkeypatch)
    interface.address = None
    monkeypatch.setattr(
        ble_interface_mod,
        "_is_currently_connected_elsewhere",
        lambda _addr, owner=None: False,
    )

    def _forbidden(_address: str | None, *, adapter: str | None = None) -> bool:
        order.append("clear")
        return False

    monkeypatch.setattr(
        ble_interface_mod,
        "_clear_stale_connection_before_central_connect",
        _forbidden,
    )
    monkeypatch.setattr(
        interface, "_resolve_target_address_for_connect", lambda _requested: _TARGET
    )
    client = interface.connect(None)
    assert client is not None
    assert order == ["establish"]


def test_connect_skips_clearance_when_connected_elsewhere_in_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An in-process owner's live link is ours, not stale: no clearance."""
    interface, order = _ordering_interface(monkeypatch)
    monkeypatch.setattr(
        ble_interface_mod,
        "_is_currently_connected_elsewhere",
        lambda _addr, owner=None: True,
    )

    def _forbidden(_address: str | None, *, adapter: str | None = None) -> bool:
        order.append("clear")
        return False

    monkeypatch.setattr(
        ble_interface_mod,
        "_clear_stale_connection_before_central_connect",
        _forbidden,
    )
    client = interface.connect(_TARGET)
    assert client is not None
    assert order == ["establish"]


def test_connect_skips_clearance_while_suppressed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connect inside the suppression block never runs the clearance hook."""
    interface, order = _ordering_interface(monkeypatch)
    monkeypatch.setattr(
        ble_interface_mod,
        "_is_currently_connected_elsewhere",
        lambda _addr, owner=None: False,
    )
    with stale_connection._suppress_stale_clearance():
        client = interface.connect(_TARGET)
    assert client is not None
    assert order == ["establish"]


def test_suppression_resets_after_block_and_on_raise() -> None:
    """The suppression flag is scoped to the block, even when the body raises."""
    with stale_connection._suppress_stale_clearance():
        assert stale_connection._stale_clearance_suppressed() is True
    assert stale_connection._stale_clearance_suppressed() is False

    def _raise_inside() -> None:
        with stale_connection._suppress_stale_clearance():
            raise RuntimeError("connect failed")

    with pytest.raises(RuntimeError, match="connect failed"):
        _raise_inside()
    assert stale_connection._stale_clearance_suppressed() is False


def test_connect_preserves_own_live_link_when_identifier_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A healthy client owned by this interface is never forcibly disconnected."""
    interface, order = _ordering_interface(monkeypatch)
    cast(Any, interface).client = types.SimpleNamespace(
        bleak_client=types.SimpleNamespace(address=_TARGET),
        isConnected=lambda: True,
    )
    monkeypatch.setattr(
        ble_interface_mod,
        "_is_currently_connected_elsewhere",
        lambda _addr, owner=None: False,
    )
    client = interface.connect(_TARGET)
    assert client is not None
    assert order == ["establish"]


def test_clear_stale_connections_ships_enabled_by_default() -> None:
    """CLEAR_STALE_CONNECTIONS ships enabled; auto-reconnect is fenced separately."""
    assert BLEConfig.CLEAR_STALE_CONNECTIONS is True


def test_scan_does_not_clear_stale_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--ble-scan is read-only listing and must never disconnect anything."""
    called: list[str] = []

    def _forbidden_coroutine(_address: str, *, adapter: str | None = None) -> Any:
        called.append("coroutine")

        async def _never() -> bool:
            return False

        return _never()

    def _forbidden_bridge(_address: str | None, *, adapter: str | None = None) -> bool:
        called.append("bridge")
        return False

    monkeypatch.setattr(
        stale_connection, "_clear_stale_connection", _forbidden_coroutine
    )
    monkeypatch.setattr(
        stale_connection,
        "_clear_stale_connection_before_central_connect",
        _forbidden_bridge,
    )
    monkeypatch.setattr(
        ble_interface_mod,
        "_clear_stale_connection_before_central_connect",
        _forbidden_bridge,
    )
    with patch.object(BLEClient, "_discover", return_value={}, create=True):
        assert BLEInterface.scan() == []
    assert called == []


def test_clearance_targets_only_requested_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A different connected device sorted first must never be disconnected."""
    reply = _managed_objects_reply(
        {
            "/org/bluez/hci0/dev_00_00_00_00_00_00": _device_entry(
                "00:00:00:00:00:00", True
            ),
            _DEVICE_PATH_HCI0: _device_entry(_TARGET, True),
        }
    )
    message, _bus = _fake_dbus(
        monkeypatch,
        replies=[reply, types.SimpleNamespace(message_type="reply", body=[])],
    )
    assert asyncio.run(stale_connection._clear_stale_connection(_TARGET)) is True
    _assert_disconnect_message(message, _DEVICE_PATH_HCI0)


def test_clearance_hooks_are_limited_to_central_paths() -> None:
    """Only the central connect paths may reference the clearance helpers.

    Pins that read-only listing (scan) and the BLE peripheral output path
    never gain a clearance call: disconnecting from those paths would tear
    down our own central link.
    """
    package_root = Path(__file__).resolve().parents[1]
    referencing: set[Path] = set()
    for path in package_root.rglob("*.py"):
        relative = path.relative_to(package_root)
        if relative.parts[0] == "tests":
            continue
        if "_clear_stale_connection" in path.read_text(encoding="utf-8"):
            referencing.add(relative)
    assert referencing == {
        Path("interfaces/ble/stale_connection.py"),
        Path("interfaces/ble/interface.py"),
        Path("interface_proxy/channels.py"),
    }


@pytest.fixture
def clear_ble_registry() -> Generator[None, None, None]:
    """Reset the shared BLE ownership registry around registry-gated tests."""
    _clear_all_registries()
    yield
    _clear_all_registries()


# Upper bound for deterministic event handoffs in the registry-race tests; a
# wait this long only fires when the wiring itself is broken.
_REGISTRY_RACE_GATE_TIMEOUT_SECONDS = 10.0


def _registry_gated_interface(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[BLEInterface, list[str]]:
    """Build a minimal interface whose registry gating runs for real.

    Unlike :func:`_ordering_interface`, the duplicate-connect gate and the
    connecting/connected registry marks are not stubbed: the connect() call
    races the real shared ownership registry. The establish step, the gate
    finalization, and the clearance bridge are stubbed; the clearance stub
    appends ``"clear"`` and establish appends ``"establish"`` to the shared
    order log.
    """
    interface = BLEInterface.__new__(BLEInterface)
    state_manager = BLEStateManager()
    interface._state_manager = state_manager
    interface._state_lock = state_manager.lock
    interface._management_lock = threading.RLock()
    interface._management_idle_condition = threading.Condition()
    interface._management_inflight = 0
    interface._connect_lock = threading.RLock()
    interface._client_publish_pending = False
    interface._last_connect_pair_override = None
    interface._last_connect_timeout_override = None
    interface.pair_on_connect = False
    interface.address = _TARGET
    interface._adapter = None
    interface._closed = False

    order: list[str] = []

    monkeypatch.setattr(interface, "_validate_connection_preconditions", _no_op)
    monkeypatch.setattr(interface, "_validate_connect_timeout_override", _no_op)
    monkeypatch.setattr(
        interface, "_get_existing_client_if_valid", lambda _request: None
    )
    monkeypatch.setattr(
        interface,
        "_resolve_target_address_for_connect",
        lambda _requested: _TARGET,
    )
    monkeypatch.setattr(interface, "_verify_and_publish_connected", _no_op)

    sentinel_client = object()

    def _establish(
        _address: str | None,
        _normalized: str | None,
        _key: str | None,
        *,
        pair_on_connect: bool = False,
        connect_timeout: float | None = None,
    ) -> tuple[object, str | None, None]:
        _ = pair_on_connect, connect_timeout
        order.append("establish")
        return sentinel_client, "aabbccddeeff", None

    monkeypatch.setattr(interface, "_establish_and_update_client", _establish)

    def _finalize_gates(
        _client: object,
        device_key: str | None,
        alias_key: str | None,
    ) -> None:
        order.append("finalize")
        # Mirror the real finalize outcome: the state machine reports
        # CONNECTED so the interface's registry liveness probe agrees with
        # the connected claim recorded here.
        state_manager.transition_to(ConnectionState.CONNECTING)
        state_manager.transition_to(ConnectionState.CONNECTED)
        interface._mark_address_keys_connected(device_key)
        if alias_key is not None:
            interface._mark_address_keys_connected(alias_key)

    monkeypatch.setattr(interface, "_finalize_connection_gates", _finalize_gates)

    def _record_clear(address: str | None, *, adapter: str | None = None) -> bool:
        _ = adapter
        order.append("clear")
        return False

    monkeypatch.setattr(
        ble_interface_mod,
        "_clear_stale_connection_before_central_connect",
        _record_clear,
    )
    return interface, order


def _proxy_bleak_client(connect: Any = None) -> MagicMock:
    """Build a fake connected BleakClient for BLECentralChannel.open."""
    client = MagicMock()
    client.is_connected = True
    client.connect = connect if connect is not None else AsyncMock()
    client.start_notify = AsyncMock()
    client.read_gatt_char = AsyncMock(return_value=b"")
    client.disconnect = AsyncMock()
    return client


def test_proxy_open_loses_registry_race_to_connecting_interface(
    monkeypatch: pytest.MonkeyPatch, clear_ble_registry: None
) -> None:
    """A proxy open during an interface connect refuses without clearing."""
    interface, order = _registry_gated_interface(monkeypatch)

    establish_entered = threading.Event()
    release_establish = threading.Event()
    claim_attempted = threading.Event()
    original_context = _addr_lock_context

    @contextlib.contextmanager
    def observed_context(address: str) -> Generator[Any, None, None]:
        with original_context(address) as lock:

            class ObservedLock:
                def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
                    claim_attempted.set()
                    return lock.acquire(blocking, timeout)

                def release(self) -> None:
                    lock.release()

                def __enter__(self) -> "ObservedLock":
                    self.acquire()
                    return self

                def __exit__(self, *args: Any) -> None:
                    self.release()

            yield ObservedLock()

    monkeypatch.setattr(proxy_channels_mod, "_addr_lock_context", observed_context)

    def _gated_establish(
        _address: str | None,
        _normalized: str | None,
        _key: str | None,
        *,
        pair_on_connect: bool = False,
        connect_timeout: float | None = None,
    ) -> tuple[object, str | None, None]:
        _ = pair_on_connect, connect_timeout
        order.append("establish")
        establish_entered.set()
        assert release_establish.wait(timeout=_REGISTRY_RACE_GATE_TIMEOUT_SECONDS)
        return object(), "aabbccddeeff", None

    monkeypatch.setattr(interface, "_establish_and_update_client", _gated_establish)

    proxy_clears: list[str] = []

    async def _record_proxy_clear(address: str, *, adapter: str | None = None) -> bool:
        proxy_clears.append(address)
        return False

    proxy_client = _proxy_bleak_client()
    monkeypatch.setitem(
        sys.modules,
        "bleak",
        types.SimpleNamespace(BleakClient=lambda *a, **kw: proxy_client),
    )
    monkeypatch.setattr(
        proxy_channels_mod, "_clear_stale_connection", _record_proxy_clear
    )

    outcome: dict[str, Any] = {}

    def _run_interface_connect() -> None:
        try:
            outcome["client"] = interface.connect(_TARGET)
        except BaseException as exc:  # noqa: BLE001 - captured for assertions
            outcome["error"] = exc

    worker = threading.Thread(target=_run_interface_connect)
    worker.start()
    # The interface holds the per-address lock inside its critical section.
    assert establish_entered.wait(timeout=_REGISTRY_RACE_GATE_TIMEOUT_SECONDS)

    async def scenario() -> None:
        open_task = asyncio.create_task(BLECentralChannel.open(_TARGET))
        # Let the interface finish establishing while the proxy claim waits
        # on the per-address lock; the proxy must then lose.
        try:
            assert await asyncio.to_thread(
                claim_attempted.wait, _REGISTRY_RACE_GATE_TIMEOUT_SECONDS
            )
        finally:
            release_establish.set()
        with pytest.raises(RuntimeError, match=_TARGET):
            await open_task

    try:
        asyncio.run(scenario())
    finally:
        release_establish.set()
        worker.join(timeout=_REGISTRY_RACE_GATE_TIMEOUT_SECONDS)
    assert not worker.is_alive()
    assert "error" not in outcome
    # The loser never cleared the winner's link and never connected.
    assert proxy_clears == []
    assert proxy_client.connect.await_count == 0
    # The interface ran its own clearance, then won the address.
    assert order == ["clear", "establish", "finalize"]
    assert _is_currently_connected_elsewhere(_TARGET)
    assert not _is_currently_connected_elsewhere(_TARGET, owner=interface)


def test_proxy_open_wins_registry_race_and_interface_refuses_without_clearing(
    monkeypatch: pytest.MonkeyPatch, clear_ble_registry: None
) -> None:
    """An interface connect during a proxy open refuses without clearing."""
    interface, order = _registry_gated_interface(monkeypatch)
    interface_clears: list[str] = []

    def _record_interface_clear(
        address: str | None, *, adapter: str | None = None
    ) -> bool:
        interface_clears.append(address or "")
        return False

    monkeypatch.setattr(
        ble_interface_mod,
        "_clear_stale_connection_before_central_connect",
        _record_interface_clear,
    )

    async def scenario() -> None:
        connect_entered = asyncio.Event()
        release_connect = asyncio.Event()

        async def slow_connect(*, timeout: float) -> None:
            connect_entered.set()
            await release_connect.wait()

        proxy_client = _proxy_bleak_client(connect=slow_connect)
        monkeypatch.setitem(
            sys.modules,
            "bleak",
            types.SimpleNamespace(BleakClient=lambda *a, **kw: proxy_client),
        )
        open_task = asyncio.create_task(BLECentralChannel.open(_TARGET))
        await connect_entered.wait()
        # The proxy holds a connecting claim spanning its connect attempt.
        assert _is_currently_connected_elsewhere(_TARGET)

        outcome: dict[str, Any] = {}

        def _run_interface_connect() -> None:
            try:
                outcome["client"] = interface.connect(_TARGET)
            except BaseException as exc:  # noqa: BLE001 - captured for assertions
                outcome["error"] = exc

        worker = threading.Thread(target=_run_interface_connect)
        worker.start()
        await asyncio.get_running_loop().run_in_executor(
            None, worker.join, _REGISTRY_RACE_GATE_TIMEOUT_SECONDS
        )
        assert not worker.is_alive()
        assert isinstance(outcome.get("error"), BLEConnectionSuppressedError)
        # The loser never cleared the winner's claim or link.
        assert interface_clears == []
        assert order == []

        release_connect.set()
        channel = await open_task
        try:
            assert _is_currently_connected_elsewhere(_TARGET)
            assert not _is_currently_connected_elsewhere(_TARGET, owner=channel)
        finally:
            await channel.close()

    asyncio.run(scenario())


def test_construct_cancellation_fences_clearance_and_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from concurrent.futures import Future

    from meshtastic.cancellation import ConnectionCancelledError, _cancellable_init
    from meshtastic.interfaces.ble import runner as runner_module

    cancel_event = threading.Event()
    scheduled: list[Any] = []
    reply = _managed_objects_reply({_DEVICE_PATH_HCI0: _device_entry(_TARGET, True)})
    message, _bus = _fake_dbus(monkeypatch, replies=[reply])

    class Pending(Future[bool]):
        def result(self, timeout: float | None = None) -> bool:
            cancel_event.set()
            return super().result(timeout=0.001)

    pending = Pending()

    class Runner:
        _thread = None

        def _run_coroutine_threadsafe(
            self, coro: Any, *, startup_timeout: float
        ) -> Future[bool]:
            scheduled.append(coro)
            return pending

    @_cancellable_init
    def construct(*, cancelEvent: threading.Event) -> bool:
        return stale_connection._clear_stale_connection_before_central_connect(_TARGET)

    monkeypatch.setattr(runner_module, "BLECoroutineRunner", Runner)
    with pytest.raises(ConnectionCancelledError):
        construct(cancelEvent=cancel_event)
    assert pending.cancelled()
    assert asyncio.run(scheduled[0]) is False
    assert not any(
        call.kwargs.get("member") == "Disconnect" for call in message.call_args_list
    )


def test_interface_clearance_does_not_swallow_construction_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meshtastic.cancellation import ConnectionCancelledError

    interface, order = _ordering_interface(monkeypatch)

    def cancelled(*args: Any, **kwargs: Any) -> bool:
        raise ConnectionCancelledError()

    monkeypatch.setattr(
        ble_interface_mod, "_clear_stale_connection_before_central_connect", cancelled
    )
    with pytest.raises(ConnectionCancelledError):
        interface.connect(_TARGET)
    assert order == []


@pytest.mark.parametrize("elsewhere", [False, True])
def test_connected_interface_cannot_bypass_another_targets_owner(
    monkeypatch: pytest.MonkeyPatch, elsewhere: bool
) -> None:
    interface, _order = _ordering_interface(monkeypatch)
    monkeypatch.setattr(interface, "_state_manager_is_connected", lambda: True)
    monkeypatch.setattr(
        ble_interface_mod,
        "_is_currently_connected_elsewhere",
        lambda *args, **kwargs: elsewhere,
    )
    assert interface._should_suppress_duplicate_connect(_TARGET) is elsewhere
