"""Tests for meshtastic.interfaces.ble package initialization.

Tests the import guard and error handling when bleak is not available.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest


def _run_isolated_python(source: str) -> None:
    """Run an import-state assertion in a fresh interpreter process.

    Parameters
    ----------
    source : str
        Python source executed by a child interpreter.
    """
    subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        check=True,
        text=True,
    )


@pytest.mark.unit
class TestBLEPackageInit:
    """Test BLE package initialization and import guard."""

    def test_ble_init_imports_successfully(self) -> None:
        """Test that ble package imports successfully when bleak is available."""
        from meshtastic.interfaces import ble

        assert ble is not None
        assert hasattr(ble, "BLEInterface")
        assert hasattr(ble, "BLEClient")
        assert hasattr(ble, "SERVICE_UUID")

    def test_ble_package_defers_interface_import_until_attribute_access(self) -> None:
        """Importing the BLE package alone should not import the implementation module."""
        _run_isolated_python("""
            import importlib
            import sys

            ble = importlib.import_module("meshtastic.interfaces.ble")
            assert "meshtastic.interfaces.ble.interface" not in sys.modules
            assert "BLEInterface" in dir(ble)
            resolved = ble.BLEInterface
            assert "meshtastic.interfaces.ble.interface" in sys.modules
            assert ble.BLEInterface is resolved
            """)

    def test_ble_init_raises_import_error_when_bleak_missing(self) -> None:
        """Missing bleak should produce the package's actionable import error."""
        _run_isolated_python("""
            import builtins
            import importlib

            original_import = builtins.__import__
            def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
                if name == "bleak":
                    raise ModuleNotFoundError("No module named 'bleak'", name="bleak")
                return original_import(name, globals, locals, fromlist, level)

            builtins.__import__ = guarded_import
            try:
                importlib.import_module("meshtastic.interfaces.ble")
            except ImportError as exc:
                message = str(exc).lower()
                assert "bleak" in message
                assert "uv sync --locked" in message
            else:
                raise AssertionError("expected BLE package import to fail without bleak")
            """)

    def test_ble_init_reraises_non_bleak_module_not_found(self) -> None:
        """Non-bleak dependency failures must not be rewritten as bleak guidance."""
        _run_isolated_python("""
            import builtins
            import importlib

            original_import = builtins.__import__
            def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
                if name == "bleak":
                    raise ModuleNotFoundError(
                        "No module named 'some_other_module'",
                        name="some_other_module",
                    )
                return original_import(name, globals, locals, fromlist, level)

            builtins.__import__ = guarded_import
            try:
                importlib.import_module("meshtastic.interfaces.ble")
            except ModuleNotFoundError as exc:
                assert exc.name == "some_other_module"
            else:
                raise AssertionError("expected non-bleak ModuleNotFoundError")
            """)

    def test_ble_unknown_lazy_export_raises_attribute_error(self) -> None:
        """Unknown package attributes should retain normal module semantics."""
        from meshtastic.interfaces import ble

        unknown_name = "NoSuchBLEExport"
        with pytest.raises(AttributeError, match=unknown_name):
            getattr(ble, unknown_name)

    def test_ble_all_exports(self) -> None:
        """Test that __all__ exports are accessible."""
        from meshtastic.interfaces import ble

        for name in ble.__all__:
            assert hasattr(ble, name), f"Missing export: {name}"


@pytest.mark.unit
class TestBLEPackageConstants:
    """Test BLE package exports are accessible."""

    def test_uuid_constants_exported(self) -> None:
        """Test that UUID constants are exported."""
        from meshtastic.interfaces.ble import (
            FROMNUM_UUID,
            FROMRADIO_UUID,
            LEGACY_LOGRADIO_UUID,
            LOGRADIO_UUID,
            SERVICE_UUID,
            TORADIO_UUID,
        )

        assert SERVICE_UUID is not None
        assert TORADIO_UUID is not None
        assert FROMRADIO_UUID is not None
        assert FROMNUM_UUID is not None
        assert LOGRADIO_UUID is not None
        assert LEGACY_LOGRADIO_UUID is not None

    def test_error_constants_exported(self) -> None:
        """Test that error message constants are exported."""
        from meshtastic.interfaces.ble import (
            BLECLIENT_ERROR_ASYNC_TIMEOUT,
            ERROR_CONNECTION_FAILED,
            ERROR_MULTIPLE_DEVICES,
            ERROR_NO_PERIPHERAL_FOUND,
            ERROR_NO_PERIPHERALS_FOUND,
            ERROR_READING_BLE,
            ERROR_TIMEOUT,
            ERROR_WRITING_BLE,
        )

        assert ERROR_TIMEOUT is not None
        assert ERROR_MULTIPLE_DEVICES is not None
        assert ERROR_READING_BLE is not None
        assert ERROR_NO_PERIPHERAL_FOUND is not None
        assert ERROR_WRITING_BLE is not None
        assert ERROR_CONNECTION_FAILED is not None
        assert ERROR_NO_PERIPHERALS_FOUND is not None
        assert BLECLIENT_ERROR_ASYNC_TIMEOUT is not None

    def test_classes_exported(self) -> None:
        """Test that main classes are exported."""
        from meshtastic.interfaces.ble import BLEClient, BLEConfig, BLEInterface

        assert BLEInterface is not None
        assert BLEClient is not None
        assert BLEConfig is not None

    def test_logger_exported(self) -> None:
        """Test that logger is exported for backward compatibility."""
        from meshtastic.interfaces.ble import logger

        assert logger is not None
