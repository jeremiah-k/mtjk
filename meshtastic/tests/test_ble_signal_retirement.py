"""A fatal CLI signal must retire native BLE connection work before exit."""

from __future__ import annotations

import asyncio
import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from meshtastic.cancellation import _wait_future
from meshtastic.interfaces.ble import client as client_module
from meshtastic.interfaces.ble.client import BLEClient

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("interruption", [SystemExit, KeyboardInterrupt])
@pytest.mark.parametrize("phase", ["wait", "registration"])
def test_interrupted_connect_waits_for_native_cleanup(
    monkeypatch: pytest.MonkeyPatch, interruption: type[BaseException], phase: str
) -> None:
    started = threading.Event()
    retired = threading.Event()

    async def connect(**kwargs: Any) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            retired.set()

    def interrupt_wait(future: Any, timeout: float | None) -> Any:
        assert started.wait(timeout=2)
        raise interruption()

    client = BLEClient(log_if_no_address=False)
    backend = MagicMock()
    backend.is_connected = False
    backend.connect = connect
    backend.disconnect = AsyncMock()
    client.bleak_client = backend
    original_wait = _wait_future
    original_submit = client._async_run
    original_register = client._with_pending_futures
    submitted: list[Any] = []

    def submit(coro: Any) -> Any:
        future = original_submit(coro)
        submitted.append(future)
        return future

    def interrupt_registration(operation: Any) -> None:
        monkeypatch.setattr(client, "_with_pending_futures", original_register)
        assert started.wait(timeout=2)
        raise interruption()

    monkeypatch.setattr(client, "_async_run", submit)
    if phase == "wait":
        monkeypatch.setattr(client_module, "_wait_future", interrupt_wait)
    else:
        monkeypatch.setattr(client, "_with_pending_futures", interrupt_registration)
    try:
        with pytest.raises(interruption):
            client.connect()
        assert retired.is_set(), "signal unwound before native connect retired"
    finally:
        monkeypatch.setattr(client_module, "_wait_future", original_wait)
        for future in submitted:
            future.cancel()
        assert retired.wait(timeout=2)
        client.close()
