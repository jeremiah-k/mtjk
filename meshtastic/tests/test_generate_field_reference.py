"""Tests for the markdown field reference generator script."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "bin" / "generate_field_reference.py"
)


def _load_reference_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "generate_field_reference", _SCRIPT_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    with _argv_patched():
        spec.loader.exec_module(mod)
    return mod


class _argv_patched:
    """Patch argv while the script module executes."""

    def __enter__(self) -> None:
        self._original = sys.argv
        sys.argv = ["generate_field_reference.py"]

    def __exit__(self, *_args: object) -> None:
        sys.argv = self._original


@pytest.mark.unit
def test_reference_renders_bounds_limits_and_flags() -> None:
    """The markdown reference carries bounds, NUL-adjusted limits, and flags."""
    mod = _load_reference_module()

    reference = mod.build_reference()

    assert "## Local config" in reference
    assert "## Module config" in reference
    assert "`lora.hop_limit`" in reference
    assert "`network.wifi_ssid`" in reference
    ssid_row = [line for line in reference.splitlines() if "wifi_ssid" in line][0]
    assert "max 32 bytes" in ssid_row
    deprecated_row = [
        line for line in reference.splitlines() if "serial_enabled" in line
    ][0]
    assert "deprecated" in deprecated_row
    ignore_row = [line for line in reference.splitlines() if "ignore_incoming" in line][
        0
    ]
    assert "max 3 entries" in ignore_row
