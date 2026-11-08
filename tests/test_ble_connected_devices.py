"""Tests for the best-effort OS-level connected-device lookup in BLE scans.

All tests are hermetic: `bleak.BleakAdapter` and the scan client are replaced
with test doubles, so no D-Bus, Bluetooth, or network I/O happens.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Coroutine
from concurrent.futures import Future
from types import SimpleNamespace
from typing import Any, Literal

import pytest
from bleak.backends.device import BLEDevice
from bleak.exc import BleakDBusError, BleakError

from meshtastic.cancellation import ConnectionCancelledError
from meshtastic.interfaces.ble.connected_devices import (
    _connected_meshtastic_devices,
    _merge_devices_by_address,
)
from meshtastic.interfaces.ble.constants import (
    BLE_CONNECTED_DEVICES_TIMEOUT,
    SERVICE_UUID,
    BLEConfig,
)
from meshtastic.interfaces.ble.interface import BLEInterface

pytestmark = pytest.mark.unit

_CONNECTED_DEVICE_PATH = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_01"


def _scan_response(*devices: BLEDevice) -> dict[str, tuple[BLEDevice, Any]]:
    """Build a discover(return_adv=True) payload advertising the service."""
    response: dict[str, tuple[BLEDevice, Any]] = {}
    for device in devices:
        response[device.address] = (
            device,
            SimpleNamespace(service_uuids=[SERVICE_UUID]),
        )
    return response


def _create_ble_device(
    address: str,
    name: str,
    details: dict[str, Any] | None = None,
) -> BLEDevice:
    """Construct a BLEDevice for testing."""
    return BLEDevice(
        address=address, name=name, details=details if details is not None else {}
    )


class _FakeConnectedAdapter:
    """Backend test double that records queries and returns a canned result."""

    def __init__(self, state: dict[str, Any], adapter_name: str | None) -> None:
        self._state = state
        self._adapter_name = adapter_name

    async def get_connected_devices(self, service_uuids: list[str]) -> list[BLEDevice]:
        """Record the requested service UUIDs and return the canned result."""
        self._state["service_uuids"].append(list(service_uuids))
        per_adapter = self._state["per_adapter"]
        if self._adapter_name in per_adapter:
            outcome = per_adapter[self._adapter_name]
            if isinstance(outcome, Exception):
                raise outcome
            return list(outcome)
        error = self._state.get("error")
        if error is not None:
            raise error
        return list(self._state["result"])


def _install_fake_bleak_adapter(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: list[BLEDevice] | None = None,
    error: Exception | None = None,
    enumerated_adapters: list[str] | None = None,
    enumeration_error: Exception | None = None,
    per_adapter: dict[str, list[BLEDevice] | Exception] | None = None,
) -> dict[str, Any]:
    """Replace `bleak.BleakAdapter` and adapter enumeration with test doubles.

    Returns a state dict recording calls. `enumerated_adapters` feeds the fake
    `_list_ble_adapters` (default: nothing enumerated, which drives the
    default-adapter fallback); `enumeration_error` makes enumeration raise;
    `per_adapter` maps a BlueZ adapter name to a canned result or exception.
    """
    state: dict[str, Any] = {
        "get_kwargs": [],
        "service_uuids": [],
        "result": list(result or []),
        "error": error,
        "per_adapter": dict(per_adapter or {}),
    }

    class FakeBleakAdapter:
        """Stands in for `bleak.BleakAdapter` (async keyword-only `get`)."""

        @classmethod
        async def get(
            cls,
            *,
            bluez: dict[str, str] | None = None,
            backend: Any = None,
        ) -> _FakeConnectedAdapter:
            """Record construction kwargs and hand back the fake backend."""
            state["get_kwargs"].append({"bluez": bluez, "backend": backend})
            adapter_name = bluez.get("adapter") if bluez is not None else None
            return _FakeConnectedAdapter(state, adapter_name)

    async def fake_list_ble_adapters() -> list[tuple[str, str, str, bool]]:
        """Stand in for `_list_ble_adapters` with canned controller names."""
        if enumeration_error is not None:
            raise enumeration_error
        return [(name, name, "", True) for name in (enumerated_adapters or [])]

    monkeypatch.setattr("bleak.BleakAdapter", FakeBleakAdapter)
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.connected_devices._list_ble_adapters",
        fake_list_ble_adapters,
    )
    return state


class _FakeScanClient:
    """Context-manager BLEClient stub mirroring the discovery test fakes."""

    def __init__(
        self,
        discover_result: dict[str, Any],
        *,
        discover_error: Exception | None = None,
    ) -> None:
        self.discover_result = discover_result
        self.discover_error = discover_error
        self.discover_calls: list[dict[str, Any]] = []
        self.await_calls: list[tuple[Any, float | None]] = []

    def __enter__(self) -> "_FakeScanClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Literal[False]:
        return False

    def _async_await(self, coro: Any, timeout: float | None = None) -> Any:
        """Record the awaited coroutine and run it to completion inline."""
        self.await_calls.append((coro, timeout))
        return asyncio.run(coro)

    def _discover(self, **kwargs: Any) -> dict[str, Any]:
        """Record discovery kwargs and return the canned result or raise."""
        self.discover_calls.append(kwargs)
        if self.discover_error is not None:
            raise self.discover_error
        return self.discover_result


def _install_scan_client(
    monkeypatch: pytest.MonkeyPatch,
    client: _FakeScanClient,
) -> None:
    """Route `BLEInterface.scan` through the given fake client."""
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.BLEClient",
        lambda **_kwargs: client,
    )


# ============================================================================
# _connected_meshtastic_devices helper
# ============================================================================


def test_connected_meshtastic_devices_queries_meshtastic_service_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lookup should filter on the Meshtastic service UUID."""
    device = _create_ble_device("AA:BB:CC:DD:EE:01", "Mesh Node")
    state = _install_fake_bleak_adapter(monkeypatch, result=[device])

    devices = asyncio.run(_connected_meshtastic_devices())

    assert state["service_uuids"] == [["6ba1b218-15a8-461f-9fa8-5dcae273eafd"]]
    assert devices == [device]


def test_connected_meshtastic_devices_queries_every_enumerated_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an adapter name every enumerated controller should be queried."""
    device_a = _create_ble_device("AA:BB:CC:DD:EE:01", "Mesh One")
    device_b = _create_ble_device("AA:BB:CC:DD:EE:02", "Mesh Two")
    state = _install_fake_bleak_adapter(
        monkeypatch,
        enumerated_adapters=["hci0", "hci1"],
        per_adapter={"hci0": [device_a], "hci1": [device_b]},
    )

    devices = asyncio.run(_connected_meshtastic_devices())

    assert state["get_kwargs"] == [
        {"bluez": {"adapter": "hci0"}, "backend": None},
        {"bluez": {"adapter": "hci1"}, "backend": None},
    ]
    assert devices == [device_a, device_b]


def test_connected_meshtastic_devices_dedupes_across_adapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A radio visible on two controllers should be reported once."""
    connected = _create_ble_device(
        "AA:BB:CC:DD:EE:01",
        "Mesh Node",
        details={"path": _CONNECTED_DEVICE_PATH},
    )
    duplicate = _create_ble_device(
        "AA:BB:CC:DD:EE:01",
        "Mesh Node",
        details={"path": "/org/bluez/hci1/dev_AA_BB_CC_DD_EE_01"},
    )
    _install_fake_bleak_adapter(
        monkeypatch,
        enumerated_adapters=["hci0", "hci1"],
        per_adapter={"hci0": [connected], "hci1": [duplicate]},
    )

    devices = asyncio.run(_connected_meshtastic_devices())

    assert devices == [connected]
    assert devices[0] is connected
    assert devices[0].details["path"] == _CONNECTED_DEVICE_PATH


def test_connected_meshtastic_devices_continues_when_one_adapter_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One failing controller must not discard another controller's result."""
    device = _create_ble_device("AA:BB:CC:DD:EE:02", "Mesh Two")
    _install_fake_bleak_adapter(
        monkeypatch,
        enumerated_adapters=["hci0", "hci1"],
        per_adapter={"hci0": OSError("hci0 wedged"), "hci1": [device]},
    )

    devices = asyncio.run(_connected_meshtastic_devices())

    assert devices == [device]


def test_connected_meshtastic_devices_isolates_stalled_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hung BlueZ controller cannot discard another controller's results."""
    device = _create_ble_device("AA:BB:CC:DD:EE:17", "Connected radio")
    _install_fake_bleak_adapter(
        monkeypatch,
        enumerated_adapters=["hci0", "hci1"],
        per_adapter={"hci0": [device]},
    )
    from meshtastic.interfaces.ble import connected_devices as connected_module
    from meshtastic.interfaces.ble.constants import BLEConfig

    original_query: Any = connected_module._query_connected_devices
    seen_cancel = asyncio.Event()

    async def query(adapter: _FakeConnectedAdapter) -> list[BLEDevice]:
        if adapter._adapter_name == "hci1":
            try:
                await asyncio.sleep(3600)
            finally:
                seen_cancel.set()
            return []
        found: list[BLEDevice] = await original_query(adapter)
        return found

    monkeypatch.setattr(connected_module, "_query_connected_devices", query)
    monkeypatch.setattr(BLEConfig, "BLE_CONNECTED_DEVICES_TIMEOUT", 0.12)
    devices = asyncio.run(_connected_meshtastic_devices())
    assert devices == [device]
    assert seen_cancel.is_set()


def test_connected_meshtastic_devices_preserves_adapter_order_under_races(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A faster second adapter must not reorder results or replace first details."""
    first = _create_ble_device("AA:BB:CC:DD:EE:10", "First", {"path": "hci0"})
    duplicate = _create_ble_device("AA:BB:CC:DD:EE:10", "Duplicate", {"path": "hci1"})
    other = _create_ble_device("AA:BB:CC:DD:EE:11", "Second")
    _install_fake_bleak_adapter(
        monkeypatch,
        enumerated_adapters=["hci0", "hci1"],
    )
    from meshtastic.interfaces.ble import connected_devices as connected_module

    async def query(adapter: _FakeConnectedAdapter) -> list[BLEDevice]:
        if adapter._adapter_name == "hci0":
            await asyncio.sleep(0.03)
            return [first]
        return [duplicate, other]

    monkeypatch.setattr(connected_module, "_query_connected_devices", query)
    assert asyncio.run(_connected_meshtastic_devices()) == [first, other]


def test_connected_meshtastic_devices_shrinks_query_budget_after_slow_enumeration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Time spent enumerating is deducted from the controllers' shared budget."""
    device = _create_ble_device("AA:BB:CC:DD:EE:21", "Mesh Fast")
    state = _install_fake_bleak_adapter(
        monkeypatch,
        enumerated_adapters=["hci0", "hci1"],
    )
    from meshtastic.interfaces.ble import connected_devices as connected_module
    from meshtastic.interfaces.ble.constants import BLEConfig

    hci1_interrupted = asyncio.Event()
    hci1_completed = asyncio.Event()

    async def query(adapter: _FakeConnectedAdapter) -> list[BLEDevice]:
        if adapter._adapter_name == "hci1":
            try:
                # Finishes too late when the query keeps only the budget left
                # after enumeration; a full unshrunk share would let it finish.
                await asyncio.sleep(0.18)
            finally:
                hci1_interrupted.set()
            hci1_completed.set()
            return []
        return [device]

    monkeypatch.setattr(connected_module, "_query_connected_devices", query)

    async def slow_enumeration() -> list[tuple[str, str, str, bool]]:
        await asyncio.sleep(0.05)
        return [("hci0", "hci0", "", True), ("hci1", "hci1", "", True)]

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.connected_devices._list_ble_adapters",
        slow_enumeration,
    )
    monkeypatch.setattr(BLEConfig, "BLE_CONNECTED_DEVICES_TIMEOUT", 0.25)

    devices = asyncio.run(_connected_meshtastic_devices())

    assert devices == [device]
    assert hci1_interrupted.is_set()
    assert not hci1_completed.is_set()
    assert state["get_kwargs"] == [
        {"bluez": {"adapter": "hci0"}, "backend": None},
        {"bluez": {"adapter": "hci1"}, "backend": None},
    ]


def test_connected_meshtastic_devices_falls_back_when_enumeration_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enumeration stalling past the deadline falls back to the default adapter."""
    device = _create_ble_device("AA:BB:CC:DD:EE:22", "Mesh Default")
    state = _install_fake_bleak_adapter(
        monkeypatch,
        enumerated_adapters=["hci0", "hci1"],
        result=[device],
    )
    from meshtastic.interfaces.ble.constants import BLEConfig

    async def stalled_enumeration() -> list[tuple[str, str, str, bool]]:
        await asyncio.sleep(3600)
        return [("hci0", "hci0", "", True)]

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.connected_devices._list_ble_adapters",
        stalled_enumeration,
    )
    monkeypatch.setattr(BLEConfig, "BLE_CONNECTED_DEVICES_TIMEOUT", 0.2)

    devices = asyncio.run(_connected_meshtastic_devices())

    assert devices == [device]
    assert state["get_kwargs"] == [{"bluez": None, "backend": None}]


@pytest.mark.parametrize("lookup_timeout", [0.0, -1.0])
def test_connected_meshtastic_devices_skips_lookup_without_budget(
    monkeypatch: pytest.MonkeyPatch, lookup_timeout: float
) -> None:
    """A zero or negative budget skips the queries and returns what was found."""
    state = _install_fake_bleak_adapter(
        monkeypatch, enumerated_adapters=["hci0", "hci1"]
    )
    from meshtastic.interfaces.ble.constants import BLEConfig

    monkeypatch.setattr(BLEConfig, "BLE_CONNECTED_DEVICES_TIMEOUT", lookup_timeout)

    devices = asyncio.run(_connected_meshtastic_devices())

    assert devices == []
    assert state["get_kwargs"] == []
    assert state["service_uuids"] == []


def test_connected_meshtastic_devices_skips_queries_when_enumeration_uses_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enumeration that consumes the whole deadline leaves the queries unrun."""
    state = _install_fake_bleak_adapter(
        monkeypatch, enumerated_adapters=["hci0", "hci1"]
    )
    from meshtastic.interfaces.ble.constants import BLEConfig

    real_wait_for = asyncio.wait_for
    wait_for_calls = {"count": 0}

    async def fake_wait_for(coro: Any, timeout: float | None = None) -> Any:
        if wait_for_calls["count"] == 0:
            # Simulate enumeration succeeding just as the deadline passes.
            wait_for_calls["count"] += 1
            await asyncio.sleep(0.06)
            return await coro
        return await real_wait_for(coro, timeout=timeout)

    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
    monkeypatch.setattr(BLEConfig, "BLE_CONNECTED_DEVICES_TIMEOUT", 0.05)

    devices = asyncio.run(_connected_meshtastic_devices())

    assert devices == []
    assert wait_for_calls["count"] == 1
    assert state["get_kwargs"] == []
    assert state["service_uuids"] == []


def test_connected_meshtastic_devices_falls_back_when_enumeration_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No enumerated controllers should fall back to the default adapter."""
    device = _create_ble_device("AA:BB:CC:DD:EE:01", "Mesh Node")
    state = _install_fake_bleak_adapter(
        monkeypatch, enumerated_adapters=[], result=[device]
    )

    devices = asyncio.run(_connected_meshtastic_devices())

    assert state["get_kwargs"] == [{"bluez": None, "backend": None}]
    assert devices == [device]


def test_connected_meshtastic_devices_falls_back_when_enumeration_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enumeration failure (non-Linux, missing D-Bus) falls back the same way."""
    device = _create_ble_device("AA:BB:CC:DD:EE:01", "Mesh Node")
    state = _install_fake_bleak_adapter(
        monkeypatch, enumeration_error=OSError("D-Bus unavailable"), result=[device]
    )

    devices = asyncio.run(_connected_meshtastic_devices())

    assert state["get_kwargs"] == [{"bluez": None, "backend": None}]
    assert devices == [device]


def test_connected_meshtastic_devices_passes_bluez_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A named adapter should be forwarded as `bluez={"adapter": ...}`."""
    state = _install_fake_bleak_adapter(monkeypatch)

    asyncio.run(_connected_meshtastic_devices(adapter="hci0"))

    assert state["get_kwargs"] == [{"bluez": {"adapter": "hci0"}, "backend": None}]


def test_connected_meshtastic_devices_propagates_backend_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit-adapter backend failure raises; degradation is the caller's job."""
    _install_fake_bleak_adapter(
        monkeypatch, error=NotImplementedError("backend unsupported")
    )

    with pytest.raises(NotImplementedError):
        asyncio.run(_connected_meshtastic_devices(adapter="hci0"))


# ============================================================================
# _merge_devices_by_address helper
# ============================================================================


def test_merge_keeps_connected_object_for_duplicated_address() -> None:
    """Primary entries win duplicates and keep object identity (BlueZ path)."""
    connected = _create_ble_device(
        "AA:BB:CC:DD:EE:01",
        "Mesh Node",
        details={"path": _CONNECTED_DEVICE_PATH},
    )
    scanned = _create_ble_device("aabbccddee01", "Mesh Node")

    merged = _merge_devices_by_address([connected], [scanned])

    assert merged == [connected]
    assert merged[0] is connected
    assert merged[0].details["path"] == _CONNECTED_DEVICE_PATH


def test_merge_keeps_order_and_appends_secondary_only_entries() -> None:
    """Primary order comes first, then unseen secondary entries in order."""
    connected_1 = _create_ble_device("AA:BB:CC:DD:EE:01", "Mesh One")
    connected_2 = _create_ble_device("AA:BB:CC:DD:EE:02", "Mesh Two")
    scanned_2 = _create_ble_device("AA:BB:CC:DD:EE:02", "Mesh Two scan")
    scanned_3 = _create_ble_device("AA:BB:CC:DD:EE:03", "Mesh Three")

    merged = _merge_devices_by_address(
        [connected_1, connected_2], [scanned_3, scanned_2]
    )

    assert merged == [connected_1, connected_2, scanned_3]


def test_merge_normalizes_separators_and_case_for_dedupe() -> None:
    """Addresses that differ only in separators or case are the same entry."""
    connected = _create_ble_device("AA:BB:CC:DD:EE:04", "Mesh Node")
    scanned_variants = [
        _create_ble_device("aabbccddee04", "lowercase"),
        _create_ble_device("AA-BB-CC-DD-EE-04", "dashes"),
        _create_ble_device(" aa:bb:cc:dd:ee:04 ", "spaces"),
    ]

    merged = _merge_devices_by_address([connected], scanned_variants)

    assert merged == [connected]


def test_merge_appends_devices_without_usable_address() -> None:
    """Devices without a normalizable address are kept, not deduplicated."""
    unnamed_1 = _create_ble_device("", "No Address One")
    unnamed_2 = _create_ble_device("", "No Address Two")

    merged = _merge_devices_by_address([unnamed_1], [unnamed_2])

    assert merged == [unnamed_1, unnamed_2]


def test_merge_with_empty_inputs_returns_empty_list() -> None:
    """Merging nothing yields nothing."""
    assert _merge_devices_by_address([], []) == []


# ============================================================================
# BLEInterface.scan wiring
# ============================================================================


def test_scan_reports_connected_devices_ahead_of_scanned_devices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scan should merge OS-connected devices ahead of scan results."""
    connected = _create_ble_device(
        "AA:BB:CC:DD:EE:01",
        "Mesh Node",
        details={"path": _CONNECTED_DEVICE_PATH},
    )
    scanned_same = _create_ble_device("aabbccddee01", "Mesh Node (scan)")
    scanned_other = _create_ble_device("AA:BB:CC:DD:EE:02", "Mesh Two")
    state = _install_fake_bleak_adapter(monkeypatch, result=[connected])
    scan_response = _scan_response(scanned_same, scanned_other)
    client = _FakeScanClient(scan_response)
    _install_scan_client(monkeypatch, client)

    result = BLEInterface.scan()

    assert result == [connected, scanned_other]
    assert result[0] is connected
    assert state["get_kwargs"] == [{"bluez": None, "backend": None}]
    assert client.discover_calls, "advertisement scan must still run"
    assert len(client.await_calls) == 1
    assert client.await_calls[0][1] == BLE_CONNECTED_DEVICES_TIMEOUT


def test_scan_passes_bluez_adapter_to_connected_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A named adapter should reach both the connected lookup and the scan."""
    monkeypatch.setattr("meshtastic._transport_selection.sys.platform", "linux")
    state = _install_fake_bleak_adapter(monkeypatch)
    client = _FakeScanClient({})
    _install_scan_client(monkeypatch, client)

    BLEInterface.scan(adapter="hci0")

    assert state["get_kwargs"] == [{"bluez": {"adapter": "hci0"}, "backend": None}]
    assert client.discover_calls
    assert client.discover_calls[0]["bluez"] == {"adapter": "hci0"}


def test_scan_propagates_cancellation_from_connected_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lookup's best-effort handlers never swallow construction cancellation."""
    client = _FakeScanClient({})

    def _cancelled(coro: Coroutine[Any, Any, Any], timeout: float | None = None) -> Any:
        coro.close()
        raise ConnectionCancelledError()

    monkeypatch.setattr(client, "_async_await", _cancelled)
    _install_scan_client(monkeypatch, client)

    with pytest.raises(ConnectionCancelledError):
        BLEInterface.scan()
    assert client.discover_calls == []


def test_scan_propagates_cancellation_from_advertisement_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A canceled advertisement scan propagates instead of degrading to results."""
    _install_fake_bleak_adapter(monkeypatch)
    client = _FakeScanClient({}, discover_error=ConnectionCancelledError())
    _install_scan_client(monkeypatch, client)

    with pytest.raises(ConnectionCancelledError):
        BLEInterface.scan()


@pytest.mark.parametrize("lookup_timeout", [0.25, 7.5])
def test_scan_reads_configured_lookup_timeout(
    monkeypatch: pytest.MonkeyPatch, lookup_timeout: float
) -> None:
    """The outer wait uses the same tunable budget as the controller queries."""
    _install_fake_bleak_adapter(monkeypatch)
    client = _FakeScanClient({})
    _install_scan_client(monkeypatch, client)
    monkeypatch.setattr(BLEConfig, "BLE_CONNECTED_DEVICES_TIMEOUT", lookup_timeout)

    BLEInterface.scan()

    assert client.await_calls[0][1] == lookup_timeout


@pytest.mark.parametrize("adapter", [None, "hci0"])
def test_scan_timeout_leaves_submitted_cleanup_on_runner(
    monkeypatch: pytest.MonkeyPatch, adapter: str | None
) -> None:
    """A real lookup timeout still scans and lets async cancellation finish."""
    from meshtastic.interfaces.ble import interface as interface_module
    from meshtastic.interfaces.ble.client import BLEClient

    monkeypatch.setattr("meshtastic._transport_selection.sys.platform", "linux")
    monkeypatch.setattr(BLEConfig, "BLE_CONNECTED_DEVICES_TIMEOUT", 0.05)
    cleanup_entered = threading.Event()
    cleanup_finished = threading.Event()
    lookup_started = threading.Event()
    release_cleanup = asyncio.Event()
    lookup_loops: list[asyncio.AbstractEventLoop] = []

    async def stalled_lookup(*, adapter: str | None = None) -> list[BLEDevice]:
        lookup_loops.append(asyncio.get_running_loop())
        lookup_started.set()
        try:
            await asyncio.sleep(3600)
        finally:
            cleanup_entered.set()
            await release_cleanup.wait()
            cleanup_finished.set()
        return []

    client = BLEClient(log_if_no_address=False)
    original_await = client._async_await
    original_submit = client._async_run

    def submit_lookup(coro: Coroutine[Any, Any, Any]) -> Future[Any]:
        future = original_submit(coro)
        # Begin the short timeout only after the task is running, independent
        # of runner startup or scheduler load.
        assert lookup_started.wait(timeout=5.0)
        return future

    def await_lookup(
        coro: Coroutine[Any, Any, Any], timeout: float | None = None
    ) -> Any:
        try:
            return original_await(coro, timeout=timeout)
        except BLEClient.BLEError:
            # Park cancellation inside the lookup's async cleanup before the
            # caller handles the timeout, making caller-thread close() observable.
            assert cleanup_entered.wait(timeout=2.0)
            raise

    scanned = _create_ble_device("AA:BB:CC:DD:EE:02", "Mesh Scan")
    discovery_calls: list[dict[str, Any]] = []

    def discover(**kwargs: Any) -> dict[str, tuple[BLEDevice, Any]]:
        discovery_calls.append(kwargs)
        lookup_loops[0].call_soon_threadsafe(release_cleanup.set)
        assert cleanup_finished.wait(timeout=2.0)
        return _scan_response(scanned)

    monkeypatch.setattr(
        interface_module, "_connected_meshtastic_devices", stalled_lookup
    )
    monkeypatch.setattr(interface_module, "BLEClient", lambda **_kwargs: client)
    monkeypatch.setattr(client, "_async_await", await_lookup)
    monkeypatch.setattr(client, "_async_run", submit_lookup)
    monkeypatch.setattr(client, "_discover", discover)
    try:
        assert BLEInterface.scan(adapter=adapter) == [scanned]
        assert discovery_calls
        assert cleanup_finished.is_set()
    finally:
        if lookup_loops:
            lookup_loops[0].call_soon_threadsafe(release_cleanup.set)
        client.close()


def test_scan_returns_scan_only_results_when_connected_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed connected lookup must degrade without skipping the scan."""
    _install_fake_bleak_adapter(monkeypatch, error=BleakError("org.bluez.Error.Failed"))
    scanned = _create_ble_device("AA:BB:CC:DD:EE:02", "Mesh Two")
    scan_response = _scan_response(scanned)
    client = _FakeScanClient(scan_response)
    _install_scan_client(monkeypatch, client)

    with caplog.at_level(logging.DEBUG, logger="meshtastic.ble"):
        result = BLEInterface.scan()

    assert result == [scanned]
    assert client.discover_calls, "scan must still run after lookup failure"
    assert "Connected-device lookup unavailable" in caplog.text


def test_scan_degrades_on_unsupported_connected_backend(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """NotImplementedError from an unsupported backend degrades to scan-only."""
    _install_fake_bleak_adapter(
        monkeypatch, error=NotImplementedError("backend unsupported")
    )
    scanned = _create_ble_device("AA:BB:CC:DD:EE:03", "Mesh Three")
    client = _FakeScanClient(_scan_response(scanned))
    _install_scan_client(monkeypatch, client)

    with caplog.at_level(logging.DEBUG, logger="meshtastic.ble"):
        result = BLEInterface.scan()

    assert result == [scanned]
    assert "Connected-device lookup unavailable" in caplog.text


def test_scan_degrades_on_os_error_from_connected_lookup(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """OSError (for example a missing system bus) degrades to scan-only."""
    _install_fake_bleak_adapter(
        monkeypatch, error=OSError("D-Bus system bus not available")
    )
    scanned = _create_ble_device("AA:BB:CC:DD:EE:04", "Mesh Four")
    client = _FakeScanClient(_scan_response(scanned))
    _install_scan_client(monkeypatch, client)

    with caplog.at_level(logging.DEBUG, logger="meshtastic.ble"):
        result = BLEInterface.scan()

    assert result == [scanned]
    assert "Connected-device lookup unavailable" in caplog.text


def test_scan_degrades_when_bleak_adapter_import_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A bleak without `BleakAdapter` degrades to scan-only via ImportError."""
    monkeypatch.delattr("bleak.BleakAdapter")
    scanned = _create_ble_device("AA:BB:CC:DD:EE:05", "Mesh Five")
    client = _FakeScanClient(_scan_response(scanned))
    _install_scan_client(monkeypatch, client)

    with caplog.at_level(logging.DEBUG, logger="meshtastic.ble"):
        result = BLEInterface.scan()

    assert result == [scanned]
    assert "Connected-device lookup unavailable" in caplog.text


def test_scan_still_scans_after_unexpected_connected_lookup_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unexpected lookup error degrades to scan-only at debug level."""
    _install_fake_bleak_adapter(monkeypatch, error=ValueError("unexpected"))
    scanned = _create_ble_device("AA:BB:CC:DD:EE:06", "Mesh Six")
    client = _FakeScanClient(_scan_response(scanned))
    _install_scan_client(monkeypatch, client)

    with caplog.at_level(logging.DEBUG, logger="meshtastic.ble"):
        result = BLEInterface.scan()

    assert result == [scanned]
    assert client.discover_calls, "scan must still run after lookup failure"
    assert "Unexpected error during connected-device lookup" in caplog.text


@pytest.mark.parametrize(
    "scan_error",
    [
        BleakError("scanner failed"),
        RuntimeError("scan unavailable"),
        ValueError("unexpected"),
    ],
)
def test_scan_preserves_connected_radios_when_advertising_scan_fails(
    monkeypatch: pytest.MonkeyPatch, scan_error: Exception
) -> None:
    """OS-connected devices remain discoverable even when scanning fails."""
    connected = _create_ble_device(
        "AA:BB:CC:DD:EE:01", "Mesh Node", details={"path": _CONNECTED_DEVICE_PATH}
    )
    _install_fake_bleak_adapter(monkeypatch, result=[connected])
    client = _FakeScanClient({}, discover_error=scan_error)
    _install_scan_client(monkeypatch, client)

    assert BLEInterface.scan() == [connected]
    assert client.discover_calls, "advertisement scan should still be attempted"


def test_scan_propagates_dbus_error_from_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The connected lookup must not swallow scan-level BleakDBusError."""
    _install_fake_bleak_adapter(monkeypatch)
    client = _FakeScanClient(
        {}, discover_error=BleakDBusError("org.bluez.Error.Failed", ["Test"])
    )
    _install_scan_client(monkeypatch, client)

    with pytest.raises(BleakDBusError):
        BLEInterface.scan()
