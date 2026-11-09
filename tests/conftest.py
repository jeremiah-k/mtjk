"""Shared pytest fixtures for BLE tests."""

import atexit as _atexit
import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import pytest

from meshtastic.interfaces.ble import gating

# Registers the autouse _no_real_self_signals fixture for this test root.
from meshtastic.tests._self_signal_fixture import _no_real_self_signals  # noqa: F401

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.notifications import NotificationManager

# tests/tuntest.py is an interactive manual utility and not part of pytest runs.
collect_ignore = ["tuntest.py"]


@pytest.fixture
def clear_registry() -> Iterator[None]:
    """Reset BLE gating global registries before and after each test."""
    gating._clear_all_registries()
    yield
    gating._clear_all_registries()


@pytest.fixture
def ble_client() -> Iterator[Any]:
    """Create and clean up a discovery-mode BLEClient for edge-case tests."""
    from meshtastic.interfaces.ble.client import BLEClient

    client = BLEClient(address=None, log_if_no_address=False)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def notification_manager() -> "NotificationManager":
    """Create a NotificationManager test instance."""
    from meshtastic.interfaces.ble.notifications import NotificationManager

    return NotificationManager()


@pytest.fixture(scope="session", autouse=True)
def _stop_ble_runner_at_session_end() -> Iterator[None]:
    """Ensure BLECoroutineRunner is stopped at the end of the test session.

    This prevents the runner's background thread from hanging during pytest exit.
    """
    yield
    # Stop the BLECoroutineRunner singleton at session end
    try:
        from meshtastic.interfaces.ble.runner import BLECoroutineRunner
    except ImportError:
        # BLE extras may not be installed in this test environment.
        return

    try:
        from meshtastic.interfaces.ble import runner as _runner_module
    except ImportError:
        return

    try:
        runner = BLECoroutineRunner()
        stop_runner = getattr(runner, "_stop", None)
        if callable(stop_runner):
            stop_runner(timeout=2.0)
        handler = getattr(runner, "_atexit_handler", None)
        if callable(handler):
            _atexit.unregister(handler)
        runner._atexit_registered = False
    except Exception as exc:  # noqa: BLE001 - teardown must not raise
        logger.debug(
            "Failed to stop BLECoroutineRunner during test cleanup: %s",
            exc,
        )

    with _runner_module._zombie_lock:
        _runner_module._zombie_runner_count = 0
