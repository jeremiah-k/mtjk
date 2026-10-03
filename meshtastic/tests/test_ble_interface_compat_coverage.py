"""Test coverage for meshtastic/ble_interface.py compatibility layer."""

import builtins
import importlib
import sys
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

# pylint: disable=import-outside-toplevel

_BLE_IMPORT_TEST_MODULE_PREFIXES = (
    "bleak",
    "meshtastic.ble_interface",
    "meshtastic.interfaces",
    "meshtastic.interfaces.ble",
)


def _module_matches_prefix(name: str, prefixes: tuple[str, ...]) -> bool:
    """Return True when a module name is in or under one of the prefixes."""
    return any(name == prefix or name.startswith(f"{prefix}.") for prefix in prefixes)


def _snapshot_modules(prefixes: tuple[str, ...]) -> dict[str, ModuleType]:
    """Capture module objects for the provided prefixes."""
    return {
        name: module
        for name, module in sys.modules.items()
        if _module_matches_prefix(name, prefixes)
    }


def _restore_modules(
    snapshot: dict[str, ModuleType], prefixes: tuple[str, ...]
) -> None:
    """Restore captured modules and remove any modules created during the test."""
    for name in list(sys.modules.keys()):
        if _module_matches_prefix(name, prefixes):
            del sys.modules[name]
    sys.modules.update(snapshot)


class TestBleInterfaceImportFailure:
    """Test import failure paths - these must be separate to not pollute imports."""

    def test_bleak_import_failure_raises_import_error(self):
        """Test that missing bleak raises ImportError with helpful message."""
        module_snapshot = _snapshot_modules(_BLE_IMPORT_TEST_MODULE_PREFIXES)

        # Remove the ble_interface module to force reimport
        if "meshtastic.ble_interface" in sys.modules:
            del sys.modules["meshtastic.ble_interface"]
        if "meshtastic.interfaces.ble" in sys.modules:
            del sys.modules["meshtastic.interfaces.ble"]
        if "meshtastic.interfaces" in sys.modules:
            del sys.modules["meshtastic.interfaces"]

        # Mock import to raise ModuleNotFoundError for bleak
        original_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "bleak" or name.startswith("bleak."):
                exc = ModuleNotFoundError(f"No module named '{name}'")
                exc.name = name.split(".")[0] if "." in name else name
                raise exc
            return original_import(name, *args, **kwargs)

        try:
            builtins.__import__ = mock_import
            with pytest.raises(ImportError) as ctx:
                importlib.import_module("meshtastic.ble_interface")

            assert "BLE support requires the 'bleak' package" in str(ctx.value)
            assert "uv sync --locked" in str(ctx.value)
        finally:
            builtins.__import__ = original_import
            _restore_modules(module_snapshot, _BLE_IMPORT_TEST_MODULE_PREFIXES)

    def test_non_bleak_module_not_found_raises_original(self):
        """Test that missing non-bleak modules re-raise the original error."""
        # This tests lines 25-28 where non-bleak ModuleNotFoundError is re-raised
        # Need to simulate a different module failing during the bleak import process
        module_snapshot = _snapshot_modules(_BLE_IMPORT_TEST_MODULE_PREFIXES)

        if "meshtastic.ble_interface" in sys.modules:
            del sys.modules["meshtastic.ble_interface"]
        if "meshtastic.interfaces.ble" in sys.modules:
            del sys.modules["meshtastic.interfaces.ble"]
        if "meshtastic.interfaces" in sys.modules:
            del sys.modules["meshtastic.interfaces"]

        original_import = builtins.__import__
        call_count = 0

        def mock_import(name, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if name == "bleak":
                # First call to bleak succeeds
                return original_import(name, *args, **kwargs)
            elif name == "bleak.backends.device":
                # But the sub-module import fails with a different module name
                exc = ModuleNotFoundError("No module named 'some_other_dep'")
                exc.name = "some_other_dep"
                raise exc
            return original_import(name, *args, **kwargs)

        try:
            builtins.__import__ = mock_import
            # This should raise the original ModuleNotFoundError, not an ImportError
            with pytest.raises(ModuleNotFoundError) as ctx:
                importlib.import_module("meshtastic.ble_interface")

            assert "some_other_dep" in str(ctx.value)
        finally:
            builtins.__import__ = original_import
            _restore_modules(module_snapshot, _BLE_IMPORT_TEST_MODULE_PREFIXES)


class TestBleInterfaceCompatImports:
    """Test import compatibility paths when bleak is available."""

    def test_bleak_imports_available(self):
        """Test that bleak imports are available when bleak is installed."""
        # Import should succeed when bleak is available
        import meshtastic.ble_interface as ble_iface

        # Verify all expected bleak symbols are exported
        assert hasattr(ble_iface, "BleakClient")
        assert hasattr(ble_iface, "BleakScanner")
        assert hasattr(ble_iface, "BLEDevice")
        assert hasattr(ble_iface, "BleakError")
        assert hasattr(ble_iface, "BleakDBusError")


class TestBleInterfaceExports:
    """Test module-level export verification."""

    def test_all_exports_present(self):
        """Test that all expected symbols are in __all__."""
        import meshtastic.ble_interface as ble_iface

        expected_exports = [
            "BleakClient",
            "BleakScanner",
            "BLEDevice",
            "BleakError",
            "BleakDBusError",
            "BLEInterface",
            "BLEClient",
            "BLEConfig",
            "logger",
        ]

        for export in expected_exports:
            assert export in ble_iface.__all__, f"{export} should be in __all__"

    def test_uuid_constants_exported(self):
        """Test that UUID constants are exported."""
        import meshtastic.ble_interface as ble_iface

        uuid_constants = [
            "SERVICE_UUID",
            "FROMNUM_UUID",
            "TORADIO_UUID",
            "FROMRADIO_UUID",
            "LOGRADIO_UUID",
            "LEGACY_LOGRADIO_UUID",
        ]

        for const in uuid_constants:
            assert hasattr(ble_iface, const), f"{const} should be exported"
            assert const in ble_iface.__all__, f"{const} should be in __all__"

    def test_error_constants_exported(self):
        """Test that error constants are exported."""
        import meshtastic.ble_interface as ble_iface

        error_constants = [
            "ERROR_CONNECTION_FAILED",
            "ERROR_NO_PERIPHERALS_FOUND",
            "ERROR_NO_PERIPHERAL_FOUND",
            "ERROR_MULTIPLE_DEVICES",
            "ERROR_TIMEOUT",
            "ERROR_READING_BLE",
            "ERROR_WRITING_BLE",
            "BLECLIENT_ERROR_ASYNC_TIMEOUT",
        ]

        for const in error_constants:
            assert hasattr(ble_iface, const), f"{const} should be exported"
            assert const in ble_iface.__all__, f"{const} should be in __all__"

    def test_ble_classes_exported(self):
        """Test that BLE classes are properly exported."""
        import meshtastic.ble_interface as ble_iface

        assert hasattr(ble_iface, "BLEInterface")
        assert hasattr(ble_iface, "BLEClient")
        assert hasattr(ble_iface, "BLEConfig")

    def test_logger_exported(self):
        """Test that logger is exported."""
        import meshtastic.ble_interface as ble_iface

        assert hasattr(ble_iface, "logger")
        assert "logger" in ble_iface.__all__


class TestBleInterfaceShimBehavior:
    """Test shim function behavior and delegation."""

    def test_ble_symbols_same_as_interfaces_ble(self):
        """Test that ble_interface exports match meshtastic.interfaces.ble exports."""
        import meshtastic.ble_interface as ble_iface
        from meshtastic.interfaces import ble as _ble

        # Check that key symbols are the same object
        ble_all = getattr(_ble, "__all__", ())
        for symbol in ble_all:
            assert hasattr(
                ble_iface, symbol
            ), f"{symbol} must be exported by ble_interface"
            iface_obj = getattr(_ble, symbol)
            compat_obj = getattr(ble_iface, symbol)
            assert iface_obj is compat_obj, f"{symbol} should be same object"

    def test_compat_bleak_exports_in_all(self):
        """Test that compatibility bleak exports are in __all__."""
        import meshtastic.ble_interface as ble_iface

        compat_exports = [
            "BleakClient",
            "BleakScanner",
            "BLEDevice",
            "BleakError",
            "BleakDBusError",
        ]

        for export in compat_exports:
            assert (
                export in ble_iface.__all__
            ), f"Compatibility export {export} should be in __all__"


class TestBleInterfaceEdgeCases:
    """Test edge cases and error conditions."""

    def test_all_unique_no_duplicates(self):
        """Test that __all__ has no duplicates."""
        import meshtastic.ble_interface as ble_iface

        # Check for duplicates by comparing length to set
        assert len(ble_iface.__all__) == len(
            set(ble_iface.__all__)
        ), "__all__ should not have duplicates"

    def test_no_circular_import_issues(self):
        """Test that importing ble_interface doesn't cause circular import issues."""
        # Clear the module from cache and reimport
        module_name = "meshtastic.ble_interface"
        if module_name in sys.modules:
            del sys.modules[module_name]

        # This should work without error
        import meshtastic.ble_interface as ble_iface

        assert ble_iface is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
