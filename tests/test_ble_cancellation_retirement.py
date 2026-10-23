"""Construction cancellation must retire native cleanup before closing its bus."""

import asyncio
import threading
from types import CoroutineType
from typing import Any

import pytest

from meshtastic.cancellation import ConnectionCancelledError, _cancellable_init
from meshtastic.interfaces.ble.client import BLEClient


@pytest.mark.parametrize("exceed_budget", (False, True))
@pytest.mark.parametrize("cancel_by_timeout", (False, True))
def test_cancelled_ble_operation_finishes_cleanup_before_client_close(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    exceed_budget: bool,
    cancel_by_timeout: bool,
) -> None:
    """A cancelled future is not acknowledgement of native coroutine retirement."""
    if exceed_budget:
        monkeypatch.setattr(
            "meshtastic.interfaces.ble.client.DISCONNECT_TIMEOUT_SECONDS", 0.05
        )
    entered = threading.Event()
    cleaning = threading.Event()
    finished = threading.Event()
    retired = threading.Event()
    release: list[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = []
    disconnect_order: list[bool] = []
    outcomes: list[BaseException] = []

    class NativeClient:
        """Model BlueZ cleanup that still owns its D-Bus connection while awaiting."""

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def disconnect(self) -> None:
            """Record whether the outstanding connect released its bus first."""
            disconnect_order.append(finished.is_set())

    async def pending() -> None:
        """Keep cancellation cleanup pending until the test releases ownership."""
        gate = asyncio.Event()
        release.append((asyncio.get_running_loop(), gate))
        entered.set()
        try:
            await asyncio.Future[None]()
        finally:
            cleaning.set()
            await gate.wait()
            finished.set()

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.client.BleakRootClient", NativeClient
    )
    client = BLEClient("AA:BB:CC:DD:EE:FF")
    event = threading.Event()

    @_cancellable_init
    def construct(*, cancelEvent: threading.Event) -> None:
        try:
            client._async_await(pending(), timeout=0.1 if cancel_by_timeout else 5)
        finally:
            client.close()

    def worker() -> None:
        try:
            construct(cancelEvent=event)
        except BaseException as exc:
            outcomes.append(exc)
        finally:
            retired.set()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        assert entered.wait(2)
        if not cancel_by_timeout:
            event.set()
        assert cleaning.wait(2)
        if exceed_budget:
            assert retired.wait(2)
            assert "BLE construction cancellation cleanup exceeded" in caplog.text
        else:
            assert not retired.wait(
                0.1
            ), "constructor closed before native cleanup finished"
            assert disconnect_order == []
    finally:
        for loop, gate in release:
            loop.call_soon_threadsafe(gate.set)
        thread.join(3)
        client.close()
    assert not thread.is_alive()
    expected = BLEClient.BLEError if cancel_by_timeout else ConnectionCancelledError
    assert len(outcomes) == 1 and isinstance(outcomes[0], expected)
    assert disconnect_order == [not exceed_budget]


def test_cancel_before_ble_coroutine_starts_closes_unawaited_native_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued operation can be abandoned without leaking its native coroutine."""
    blocked = threading.Event()
    unblock = threading.Event()
    submitted = threading.Event()
    retired = threading.Event()
    entered = threading.Event()
    event = threading.Event()
    outcomes: list[BaseException] = []

    class NativeClient:
        """Avoid system Bluetooth while exercising the real shared runner."""

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def disconnect(self) -> None:
            """Provide ordinary client cleanup after the runner is released."""

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.client.BleakRootClient", NativeClient
    )
    client = BLEClient("AA:BB:CC:DD:EE:FF")

    def block_runner() -> None:
        blocked.set()
        assert unblock.wait(3)

    async def pending() -> None:
        entered.set()

    async def occupy_runner() -> None:
        block_runner()

    blocker = client._async_run(occupy_runner())
    assert blocked.wait(2)
    original_run = client._async_run

    def observe_submission(coro: Any) -> Any:
        future = original_run(coro)
        submitted.set()
        return future

    monkeypatch.setattr(client, "_async_run", observe_submission)
    native = pending()

    @_cancellable_init
    def construct(*, cancelEvent: threading.Event) -> None:
        client._async_await(native, timeout=5)

    def worker() -> None:
        try:
            construct(cancelEvent=event)
        except BaseException as exc:
            outcomes.append(exc)
        finally:
            retired.set()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        assert submitted.wait(2)
        event.set()
        assert retired.wait(2)
        assert isinstance(native, CoroutineType)
        assert native.cr_frame is None
        assert not entered.is_set()
    finally:
        unblock.set()
        thread.join(3)
        blocker.result(timeout=3)
        client.close()
    assert len(outcomes) == 1 and isinstance(outcomes[0], ConnectionCancelledError)


def test_rejected_ble_submission_closes_the_unstarted_native_coroutine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Submission failure must release both the wrapper and its native operation."""
    client = BLEClient.__new__(BLEClient)
    # Reuse real state initialization without constructing a native client.
    client._initialize_runtime_state("AA:BB:CC:DD:EE:FF")

    def reject(_coro: Any) -> Any:
        raise RuntimeError("runner stopped")

    monkeypatch.setattr(client._runner, "_run_coroutine_threadsafe", reject)

    async def pending() -> None:
        pytest.fail("rejected native coroutine was executed")

    native = pending()

    @_cancellable_init
    def construct(*, cancelEvent: threading.Event) -> None:
        client._async_await(native, timeout=5)

    with pytest.raises(BLEClient.BLEError, match="runner stopped"):
        construct(cancelEvent=threading.Event())
    assert isinstance(native, CoroutineType)
    assert native.cr_frame is None
    client.close()
