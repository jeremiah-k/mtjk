"""Ownership lifetime checks for proxy cancellation, disconnect, and fork."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
import threading
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from meshtastic.interface_proxy import channels
from meshtastic.interface_proxy.channels import BLECentralChannel
from meshtastic.interfaces.ble import gating

pytestmark = pytest.mark.unit
_ADDRESS = "AA:BB:CC:DD:EE:FF"


@pytest.fixture(autouse=True)
def _isolated_ownership(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    gating._clear_all_registries()
    monkeypatch.setattr(
        channels, "_clear_stale_connection", AsyncMock(return_value=False)
    )
    yield
    gating._clear_all_registries()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    result = MagicMock()
    result.is_connected = True
    result.connect = AsyncMock()
    result.start_notify = AsyncMock()
    result.disconnect = AsyncMock()
    result.read_gatt_char = AsyncMock(return_value=b"")
    monkeypatch.setattr("bleak.BleakClient", lambda *args, **kwargs: result)
    return result


def test_cancelled_claim_retires_while_address_lock_remains_held(
    monkeypatch: pytest.MonkeyPatch, client: MagicMock
) -> None:
    attempted = threading.Event()
    retired = threading.Event()
    original_claim = channels._try_claim_ble_address

    def claim(
        address: str, owner: object, aborted: threading.Event | None = None
    ) -> bool:
        attempted.set()
        try:
            return original_claim(address, owner, aborted)
        finally:
            retired.set()

    monkeypatch.setattr(channels, "_try_claim_ble_address", claim)

    async def scenario() -> None:
        with gating._addr_lock_context(_ADDRESS) as lock:
            with lock:
                opening = asyncio.create_task(BLECentralChannel.open(_ADDRESS))
                try:
                    assert await asyncio.to_thread(attempted.wait, 2)
                    opening.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(opening, 1)
                    assert await asyncio.to_thread(retired.wait, 1)
                    assert not gating._is_currently_connected_elsewhere(_ADDRESS)
                    client.connect.assert_not_awaited()
                finally:
                    opening.cancel()

    asyncio.run(scenario())


@pytest.mark.parametrize("cause", ["notify_failure", "cancelled_open", "close"])
def test_claim_survives_repeated_cancellation_until_disconnect_finishes(
    client: MagicMock, cause: str
) -> None:
    async def scenario() -> None:
        setup_entered = asyncio.Event()
        disconnect_entered = asyncio.Event()
        release_disconnect = asyncio.Event()
        disconnect_finished = asyncio.Event()

        async def notify(*args: Any) -> None:
            setup_entered.set()
            if cause == "notify_failure":
                raise OSError("notification setup failed")
            if cause == "cancelled_open":
                await asyncio.Event().wait()

        async def disconnect() -> None:
            # Ownership must survive even when the backend's liveness flag
            # drops before its asynchronous disconnect operation completes.
            client.is_connected = False
            disconnect_entered.set()
            try:
                await release_disconnect.wait()
            finally:
                disconnect_finished.set()

        client.start_notify.side_effect = notify
        client.disconnect.side_effect = disconnect
        opening = asyncio.create_task(BLECentralChannel.open(_ADDRESS))
        retiring: asyncio.Task[Any]
        try:
            await asyncio.wait_for(setup_entered.wait(), 2)
            if cause == "close":
                channel = await opening
                retiring = asyncio.create_task(channel.close())
            else:
                retiring = opening
                if cause == "cancelled_open":
                    opening.cancel()
            await asyncio.wait_for(disconnect_entered.wait(), 2)
            retiring.cancel()
            await asyncio.sleep(0)
            retiring.cancel()
            with pytest.raises(asyncio.CancelledError):
                await retiring
            assert (
                not disconnect_finished.is_set()
            ), "waiter cancellation interrupted native cleanup"
            assert gating._is_currently_connected_elsewhere(_ADDRESS)
            with pytest.raises(RuntimeError, match="already claimed"):
                await BLECentralChannel.open(_ADDRESS)
        finally:
            release_disconnect.set()
            await asyncio.wait_for(disconnect_finished.wait(), 2)
        assert not gating._is_currently_connected_elsewhere(_ADDRESS)

    asyncio.run(scenario())


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_proxy_claim_worker_works_after_parent_has_opened_a_channel() -> None:
    code = textwrap.dedent("""
        import asyncio
        import os
        import signal
        from unittest.mock import AsyncMock, patch
        from meshtastic.interface_proxy import channels

        class Client:
            is_connected = True
            def __init__(self, *args, **kwargs):
                pass
            async def connect(self, **kwargs):
                pass
            async def start_notify(self, *args):
                pass
            async def disconnect(self):
                pass
            async def read_gatt_char(self, *args):
                return b''

        async def session(address):
            with patch('bleak.BleakClient', Client), patch.object(
                channels, '_clear_stale_connection', AsyncMock(return_value=False)
            ):
                channel = await channels.BLECentralChannel.open(address)
                await channel.close()

        asyncio.run(session('AA:BB:CC:DD:EE:01'))
        child = os.fork()
        if child == 0:
            signal.alarm(3)
            try:
                asyncio.run(session('AA:BB:CC:DD:EE:02'))
            except BaseException:
                os._exit(2)
            os._exit(0)
        _, status = os.waitpid(child, 0)
        assert os.waitstatus_to_exitcode(status) == 0, status
        print('child-claimed')
        """)
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "child-claimed"
