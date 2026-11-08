"""Best-effort lookup of BLE peripherals already connected at the OS level.

Used by `BLEInterface.scan` to also report radios that are connected but not
currently advertising. The lookup never gates the advertisement scan: callers
wrap it so any failure degrades to scan-only results.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from bleak.backends.device import BLEDevice

from meshtastic._transport_selection import _list_ble_adapters
from meshtastic.interfaces.ble.constants import SERVICE_UUID, BLEConfig, logger
from meshtastic.interfaces.ble.utils import sanitize_address

if TYPE_CHECKING:
    from bleak import BleakAdapter

# Fraction of the outer `BLEConfig.BLE_CONNECTED_DEVICES_TIMEOUT` budget that
# the controller queries may consume, measured from the start of the lookup.
# The remainder of the outer timeout that `BLEInterface.scan` applies covers
# the thread hop back to the scan caller and the result merge.
_CONNECTED_LOOKUP_BUDGET_FRACTION = 0.8


async def _connected_meshtastic_devices(
    *, adapter: str | None = None
) -> list[BLEDevice]:
    """Query the OS for connected BLE peripherals exposing the Meshtastic service.

    Uses bleak's `BleakAdapter.get_connected_devices()`, which asks the operating
    system about peripherals that are already connected (and have GATT services
    resolved) without starting a scan. `BleakAdapter` is imported lazily so the
    module can be imported regardless of which backend the platform provides;
    unsupported backends raise at call time and callers are expected to degrade.

    With an explicit `adapter`, only that controller is queried and errors
    propagate. Without one, every local controller reported by
    `_list_ble_adapters` is queried concurrently so radios connected via a
    non-default controller are not missed, and one stalled controller cannot
    discard the results already found elsewhere. The whole lookup runs against
    a deadline measured from the start: each controller's query gets the part
    of the `BLEConfig.BLE_CONNECTED_DEVICES_TIMEOUT` budget left after adapter
    enumeration, capped at `_CONNECTED_LOOKUP_BUDGET_FRACTION` of that budget.
    Adapter enumeration is bounded by the same deadline; a failure or timeout
    there, or an empty controller list (non-Linux hosts, missing D-Bus), falls
    back to bleak's default adapter. A controller whose query fails or times
    out is skipped at debug level. If the budget is exhausted before the
    queries (zero or negative lookup timeout, or enumeration that consumed it),
    the lookup returns what has been collected so far without querying.

    Parameters
    ----------
    adapter : str | None
        Linux/BlueZ adapter name (for example ``"hci0"``) to restrict the
        query to. `None` queries every enumerated local controller.
        (Default value = None)

    Returns
    -------
    list[BLEDevice]
        Connected peripherals whose GATT services intersect the Meshtastic
        service UUID, deduplicated by normalized address across controllers.
        The returned `BLEDevice` objects carry backend details (for example
        the BlueZ object path) and can be passed to `BleakClient` directly.
    """
    # Imported lazily: `BleakAdapter` requires a bleak version that ships the
    # connected-device API, and backend resolution happens per platform.
    from bleak import BleakAdapter  # pylint: disable=import-outside-toplevel

    loop = asyncio.get_running_loop()
    # The deadline is measured from the start of the whole lookup so that slow
    # adapter enumeration cannot leave the controller queries no time at all.
    start = loop.time()
    # Read at call time so hosts and tests can tune the budget dynamically.
    lookup_timeout = BLEConfig.BLE_CONNECTED_DEVICES_TIMEOUT
    deadline = start + lookup_timeout * _CONNECTED_LOOKUP_BUDGET_FRACTION

    if adapter is not None:
        bleak_adapter = await BleakAdapter.get(bluez={"adapter": adapter})
        return await _query_connected_devices(bleak_adapter)

    devices: list[BLEDevice] = []
    enumeration_budget = max(0.0, deadline - loop.time())
    if enumeration_budget <= 0.0:
        # A zero or negative budget leaves no time for the lookup at all; keep
        # whatever was collected (nothing) instead of querying controllers.
        logger.debug(
            "Connected-device lookup budget exhausted before controller "
            "enumeration (%.3fs of %ss used); skipping the connected-device "
            "lookup",
            loop.time() - start,
            lookup_timeout,
        )
        return devices
    try:
        adapter_names = [
            name
            for name, _alias, _address, _powered in await asyncio.wait_for(
                _list_ble_adapters(), timeout=enumeration_budget
            )
        ]
    except Exception as exc:  # noqa: BLE001 - enumeration is best-effort
        logger.debug(
            "Bluetooth adapter enumeration unavailable (%s); "
            "using the default adapter for the connected-device lookup",
            exc,
        )
        adapter_names = []
    if not adapter_names:
        bleak_adapter = await BleakAdapter.get()
        return await _query_connected_devices(bleak_adapter)

    # Each controller gets only what is left of the shared deadline, so a
    # slow enumeration shrinks the per-controller budget instead of letting
    # the queries overrun the outer timeout `scan()` applies.
    query_budget = max(0.0, deadline - loop.time())
    if query_budget <= 0.0:
        logger.debug(
            "Connected-device lookup budget exhausted after enumerating %d "
            "controller(s); skipping the per-controller queries",
            len(adapter_names),
        )
        return devices

    async def _query_adapter(adapter_name: str) -> list[BLEDevice]:
        backend = await BleakAdapter.get(bluez={"adapter": adapter_name})
        return await _query_connected_devices(backend)

    outcomes = await asyncio.gather(
        *(
            asyncio.wait_for(_query_adapter(name), timeout=query_budget)
            for name in adapter_names
        ),
        return_exceptions=True,
    )
    for adapter_name, outcome in zip(adapter_names, outcomes, strict=False):
        if isinstance(outcome, BaseException):
            logger.debug(
                "Connected-device lookup failed for adapter %s: %s",
                adapter_name,
                outcome,
                exc_info=(type(outcome), outcome, outcome.__traceback__),
            )
            continue
        devices = _merge_devices_by_address(devices, outcome)
    return devices


async def _query_connected_devices(bleak_adapter: BleakAdapter) -> list[BLEDevice]:
    """Query one resolved adapter for peripherals exposing the Meshtastic service."""
    devices = await bleak_adapter.get_connected_devices([SERVICE_UUID])
    logger.debug(
        "Connected-device lookup returned %d device(s) for the Meshtastic service",
        len(devices),
    )
    return devices


def _merge_devices_by_address(
    primary: list[BLEDevice], secondary: list[BLEDevice]
) -> list[BLEDevice]:
    """Merge two device lists, keeping the first entry for each address.

    Deduplication uses the normalized address produced by
    `sanitize_address`, so entries that differ only in separators or case
    collapse into the primary entry. Primary entries win: for addresses that
    appear in both lists, the primary `BLEDevice` object is kept, which
    preserves backend details such as the BlueZ object path that connected
    devices carry and `BleakClient` needs. Order is preserved: primary
    entries first (in order), then secondary entries whose address was not
    seen. Devices without a usable normalized address are always appended,
    since they cannot be deduplicated.

    Parameters
    ----------
    primary : list[BLEDevice]
        Devices that win on duplicate addresses (for example connected
        devices).
    secondary : list[BLEDevice]
        Devices appended when their address is not already present (for
        example scanned devices).

    Returns
    -------
    list[BLEDevice]
        Merged device list with duplicates removed by normalized address.
    """
    merged: list[BLEDevice] = []
    seen_addresses: set[str] = set()
    for device in (*primary, *secondary):
        key = sanitize_address(device.address)
        if key is not None:
            if key in seen_addresses:
                continue
            seen_addresses.add(key)
        merged.append(device)
    return merged
