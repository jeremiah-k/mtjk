"""Shared pytest fixtures for BLE tests."""

import atexit as _atexit
import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import pytest

from meshtastic.interfaces.ble import gating

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


@pytest.fixture(autouse=True)
def _no_real_self_signals() -> Iterator[list[tuple[int, bool]]]:
    """Replace the CLI self-signal seam so no test can signal this process.

    Every CLI fatal-signal path (the TERM/HUP re-delivery after teardown, the
    second-delivery forced exit, and the SIGINT re-delivery after a Ctrl+C
    teardown) delivers through ``meshtastic._self_signal._signal_self``. The
    recorder notes each request and returns, so callers keep their
    ``SystemExit`` fallbacks and the pytest process is never signaled. Tests
    that verify the delivery itself opt back in by patching the seam with
    their own fake. The seam module is imported lazily here and is stdlib-only,
    so conftest collection imports nothing heavy and no import-order effects
    are possible.
    """
    from meshtastic import _self_signal

    requests: list[tuple[int, bool]] = []

    def _recorder(signum: int, *, reset_to_default: bool) -> None:
        requests.append((signum, reset_to_default))

    # A private patcher, NOT the shared monkeypatch fixture: requesting that
    # fixture from an autouse conftest fixture sets it up before module-level
    # autouse fixtures, and LIFO teardown would then run their teardowns while
    # the test's own monkeypatches were still active. The context below keeps
    # this guard's patch bracketed around the test without touching that
    # order: a test's shared-monkeypatch undo restores the recorder first,
    # and this guard's undo restores the real function last.
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(_self_signal, "_signal_self", _recorder)
        yield requests
