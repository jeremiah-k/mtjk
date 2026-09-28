"""Focused CLI bootstrap coverage for OTA node-loading policy."""

import argparse
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from meshtastic.cli import bootstrap


def _make_args(**overrides: Any) -> argparse.Namespace:
    """Build a preconnect argument namespace with OTA-relevant defaults."""
    defaults: dict[str, Any] = {
        "quiet": False,
        "debug": False,
        "listen": False,
        "debuglib": False,
        "contact_verified": False,
        "contact_ignore": False,
        "contact_qr": None,
        "configure": None,
        "set_owner": None,
        "set_owner_short": None,
        "set_ham": None,
        "ota_update": None,
        "reboot_ota": False,
        "no_nodes": False,
        "ch_index": None,
        "dest": None,
        "seriallog": None,
        "noproto": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


@pytest.mark.unit
def test_ota_preflight_forces_no_nodes_before_transport_open(tmp_path: Path) -> None:
    """Verify OTA preflight disables NodeDB loading before transport open.

    Parameters
    ----------
    tmp_path : Path
        Temporary directory used to create a firmware image accepted by preflight.
    """
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"firmware")
    args = _make_args(ota_update=str(firmware))
    hooks = MagicMock(autospec=bootstrap.BootstrapHooks)

    bootstrap._validate_and_normalize_args(args, MagicMock(), hooks)

    assert args.no_nodes is True


@pytest.mark.unit
def test_reboot_ota_preflight_forces_no_nodes() -> None:
    """Verify rebootOTA preflight disables NodeDB loading like the OTA path."""
    args = _make_args(reboot_ota=True)
    hooks = MagicMock(autospec=bootstrap.BootstrapHooks)

    bootstrap._validate_and_normalize_args(args, MagicMock(), hooks)

    assert args.no_nodes is True


@pytest.mark.unit
def test_plain_invocation_keeps_node_loading_enabled() -> None:
    """Verify preflight leaves NodeDB loading on when no OTA action is requested."""
    args = _make_args()
    hooks = MagicMock(autospec=bootstrap.BootstrapHooks)

    bootstrap._validate_and_normalize_args(args, MagicMock(), hooks)

    assert args.no_nodes is False
