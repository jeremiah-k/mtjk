# ruff: noqa: RUF022  # __all__ is intentionally grouped, not sorted

"""Backwards compatibility layer for BLE interface.

This module provides a stable public API for the BLE interface.
Internal implementation classes are available from meshtastic.interfaces.ble
but should not be considered part of the stable public API.
"""

# Historical module-level imports retained for compatibility with code that
# imported Bleak symbols from meshtastic.ble_interface in the pre-refactor API.
# COMPAT_STABLE_SHIM
try:
    from bleak import (  # noqa: F401  # pylint: disable=unused-import
        BleakClient,
        BleakScanner,
    )
    from bleak.backends.device import (  # noqa: F401  # pylint: disable=unused-import
        BLEDevice,
    )
    from bleak.exc import (  # noqa: F401  # pylint: disable=unused-import
        BleakDBusError,
        BleakError,
        BleakGATTProtocolError,
    )
except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
    if exc.name != "bleak":
        raise
    raise ImportError(  # noqa: TRY003
        "BLE support requires the 'bleak' package, but it is missing. "
        "Your Meshtastic installation appears incomplete; reinstall dependencies "
        "with `poetry install` (or `pipx install mtjk`)."
    ) from exc

# Public API re-export: import the canonical BLE facade once and mirror its
# declared public surface from __all__ into this shim namespace.
from meshtastic.interfaces import ble as _ble
from meshtastic.interfaces.ble import (  # noqa: F401  # pylint: disable=unused-import
    BLECLIENT_ERROR_ASYNC_TIMEOUT,
    ERROR_CONNECTION_FAILED,
    ERROR_MULTIPLE_DEVICES,
    ERROR_NO_PERIPHERAL_FOUND,
    ERROR_NO_PERIPHERALS_FOUND,
    ERROR_READING_BLE,
    ERROR_TIMEOUT,
    ERROR_WRITING_BLE,
    FROMNUM_UUID,
    FROMRADIO_UUID,
    LEGACY_LOGRADIO_UUID,
    LOGRADIO_UUID,
    SERVICE_UUID,
    TORADIO_UUID,
    BLEAddressMismatchError,
    BLEClient,
    BLEConfig,
    BLEConnectionSuppressedError,
    BLEConnectionTimeoutError,
    BLEDBusTransportError,
    BLEDeviceNotFoundError,
    BLEDiscoveryError,
    BLEInterface,
    MeshtasticBLEError,
    logger,
    sanitize_address,
)

_BLE_PUBLIC_ALL = tuple(getattr(_ble, "__all__", ()))
for _ble_public_symbol in _BLE_PUBLIC_ALL:
    globals().setdefault(_ble_public_symbol, getattr(_ble, _ble_public_symbol))
if _BLE_PUBLIC_ALL:
    del _ble_public_symbol

# Retained module-level Bleak compatibility exports from pre-refactor API.
_COMPAT_BLEAK_EXPORTS = (
    "BleakClient",
    "BleakScanner",
    "BLEDevice",
    "BleakError",
    "BleakDBusError",
    "BleakGATTProtocolError",
)

# Stable public API delegates to canonical facade exports plus retained Bleak
# compatibility names for `from meshtastic.ble_interface import *`.
__all__ = list(dict.fromkeys([*_BLE_PUBLIC_ALL, *_COMPAT_BLEAK_EXPORTS]))
