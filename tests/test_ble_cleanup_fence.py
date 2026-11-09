"""Address-level fence for interrupted BLEClient connect cleanup.

Follow-up to the #767 stale-clearance work: when an interrupted direct
connect's bounded native cleanup wait expires, a BlueZ disconnect may still
be pending. An address-level fence must refuse new connects (interface path
and proxy claim path) until the straggler disconnect retires or the fence
deadline expires, so the new link cannot race the late teardown.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from meshtastic.interface_proxy import channels
from meshtastic.interface_proxy.channels import BLECentralChannel
from meshtastic.interfaces.ble import client as client_module
from meshtastic.interfaces.ble import gating
from meshtastic.interfaces.ble import interface as ble_interface_mod
from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.constants import BLEConfig
from meshtastic.interfaces.ble.gating import _INTERRUPTED_CLEANUP_FENCES
from meshtastic.interfaces.ble.interface import BLEInterface
from meshtastic.interfaces.ble.state import BLEStateManager

pytestmark = pytest.mark.unit

_TARGET = "AA:BB:CC:DD:EE:FF"
_SHORT_BOUND = 0.05


@pytest.fixture(autouse=True)
def _isolated_registry() -> Iterator[None]:
    """Give every test an empty ownership registry, fences included."""
    gating._clear_all_registries()
    yield
    gating._clear_all_registries()


def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    """Poll ``predicate`` until it is truthy or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class TestInterruptedCleanupFenceRegistry:
    """Register/expire/refuse lifecycle of the gating fence record."""

    def test_register_and_retire(self) -> None:
        assert not gating._interrupted_cleanup_fenced(_TARGET)
        deadline = gating._register_interrupted_cleanup_fence(_TARGET)
        assert deadline > time.monotonic()
        assert gating._interrupted_cleanup_fenced(_TARGET)
        gating._retire_interrupted_cleanup_fence(_TARGET)
        assert not gating._interrupted_cleanup_fenced(_TARGET)

    def test_retire_is_idempotent(self) -> None:
        gating._register_interrupted_cleanup_fence(_TARGET)
        gating._retire_interrupted_cleanup_fence(_TARGET)
        gating._retire_interrupted_cleanup_fence(_TARGET)
        assert not gating._interrupted_cleanup_fenced(_TARGET)

    def test_none_address_is_a_noop(self) -> None:
        assert gating._register_interrupted_cleanup_fence(None) == 0.0
        assert not gating._interrupted_cleanup_fenced(None)
        gating._retire_interrupted_cleanup_fence(None)
        assert not gating._interrupted_cleanup_fenced(None)

    def test_normalization_collapses_address_spellings(self) -> None:
        gating._register_interrupted_cleanup_fence("aa:bb-cc_dd ee:ff")
        assert gating._interrupted_cleanup_fenced("AABBCCDDEEFF")
        assert gating._interrupted_cleanup_fenced(_TARGET)

    def test_registration_refreshes_deadline(self) -> None:
        first = gating._register_interrupted_cleanup_fence(_TARGET)
        time.sleep(0.005)
        second = gating._register_interrupted_cleanup_fence(_TARGET)
        assert second > first
        assert _INTERRUPTED_CLEANUP_FENCES["aabbccddeeff"] == second

    def test_expected_deadline_guard_keeps_newer_fence(self) -> None:
        """An older watcher must not release a fence a newer cleanup refreshed."""
        stale_deadline = gating._register_interrupted_cleanup_fence(_TARGET)
        fresh_deadline = gating._register_interrupted_cleanup_fence(_TARGET)
        gating._retire_interrupted_cleanup_fence(
            _TARGET, expected_deadline=stale_deadline
        )
        assert gating._interrupted_cleanup_fenced(_TARGET)
        gating._retire_interrupted_cleanup_fence(
            _TARGET, expected_deadline=fresh_deadline
        )
        assert not gating._interrupted_cleanup_fenced(_TARGET)

    def test_expired_fence_is_pruned_lazily_on_consult(self) -> None:
        """A fence past its deadline must never wedge the address."""
        _INTERRUPTED_CLEANUP_FENCES["aabbccddeeff"] = time.monotonic() - 1.0
        assert not gating._interrupted_cleanup_fenced(_TARGET)
        assert "aabbccddeeff" not in _INTERRUPTED_CLEANUP_FENCES

    def test_clear_all_registries_clears_fences(self) -> None:
        gating._register_interrupted_cleanup_fence(_TARGET)
        gating._clear_all_registries()
        assert _INTERRUPTED_CLEANUP_FENCES == {}
        assert not gating._interrupted_cleanup_fenced(_TARGET)


def _make_connecting_client(
    address: str | None = _TARGET,
) -> tuple[BLEClient, MagicMock]:
    """Build a BLEClient whose transport is a hermetic mock backend."""
    client = BLEClient(address=address, log_if_no_address=False)
    backend = MagicMock()
    backend.is_connected = False
    backend.disconnect = AsyncMock()
    client.bleak_client = backend
    return client, backend


def _interrupt_at_wait(
    monkeypatch: pytest.MonkeyPatch, client: BLEClient, started: threading.Event
) -> None:
    """Interrupt ``client.connect()`` with a fatal signal mid-wait.

    The interrupting ``_wait_future`` stub is scoped to this call so
    ``client.close()`` in the caller's cleanup never sees it; a close-time
    disconnect hitting the stub would raise a second, uncaught interrupt
    outside the ``pytest.raises`` window.
    """

    def interrupt_wait(future: Any, timeout: float | None) -> Any:
        assert started.wait(timeout=2)
        raise KeyboardInterrupt()

    with monkeypatch.context() as scoped:
        scoped.setattr(client_module, "_wait_future", interrupt_wait)
        with pytest.raises(KeyboardInterrupt):
            client.connect()


class TestClientFenceRegistration:
    """The interrupted-cleanup path in BLEClient registers and releases fences."""

    def test_overrunning_cleanup_registers_fence_on_both_layers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        started = threading.Event()
        release_cleanup = threading.Event()

        async def connect(**kwargs: Any) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # Bleak-like: native disconnect handling outlives the
                # caller's bounded cleanup wait before the coroutine ends.
                # to_thread keeps the wait off the shared runner loop.
                await asyncio.to_thread(release_cleanup.wait)
                raise

        monkeypatch.setattr(client_module, "DISCONNECT_TIMEOUT_SECONDS", _SHORT_BOUND)
        # A present cancellation event makes the inner _async_await layer
        # wrap the coroutine too, so both registration call sites run.
        monkeypatch.setattr(client_module, "_current_event", threading.Event)
        client, backend = _make_connecting_client()
        backend.connect = connect
        try:
            _interrupt_at_wait(monkeypatch, client, started)
            assert gating._interrupted_cleanup_fenced(_TARGET)
        finally:
            release_cleanup.set()
            client.close()
        # The retirement watcher releases the fence once cleanup retires.
        assert _wait_until(lambda: not gating._interrupted_cleanup_fenced(_TARGET))

    def test_cleanup_within_bound_does_not_fence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        started = threading.Event()

        async def connect(**kwargs: Any) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                # Mirrors the shipped interrupted path: native cleanup
                # retires promptly after cancellation.
                await asyncio.sleep(0)

        monkeypatch.setattr(client_module, "DISCONNECT_TIMEOUT_SECONDS", _SHORT_BOUND)
        client, backend = _make_connecting_client()
        backend.connect = connect
        try:
            _interrupt_at_wait(monkeypatch, client, started)
            assert not gating._interrupted_cleanup_fenced(_TARGET)
        finally:
            client.close()

    def test_unretired_cleanup_expires_by_deadline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        started = threading.Event()
        release_cleanup = threading.Event()

        async def connect(**kwargs: Any) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # The straggler cleanup stays pending well past the fence
                # deadline; to_thread keeps the wait off the runner loop.
                await asyncio.to_thread(release_cleanup.wait)
                raise

        monkeypatch.setattr(client_module, "DISCONNECT_TIMEOUT_SECONDS", _SHORT_BOUND)
        monkeypatch.setattr(BLEConfig, "INTERRUPTED_CLEANUP_FENCE_MAX_SECONDS", 0.15)
        client, backend = _make_connecting_client()
        backend.connect = connect
        try:
            _interrupt_at_wait(monkeypatch, client, started)
            assert gating._interrupted_cleanup_fenced(_TARGET)
            # The straggler cleanup never retires; the deadline backstop
            # must release the fence so the address cannot wedge.
            assert _wait_until(
                lambda: not gating._interrupted_cleanup_fenced(_TARGET),
                timeout=5.0,
            )
        finally:
            release_cleanup.set()
            client.close()

    def test_client_without_address_does_not_fence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        started = threading.Event()
        release_cleanup = threading.Event()

        async def connect(**kwargs: Any) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.to_thread(release_cleanup.wait)
                raise

        monkeypatch.setattr(client_module, "DISCONNECT_TIMEOUT_SECONDS", _SHORT_BOUND)
        client, backend = _make_connecting_client(address=None)
        backend.connect = connect
        try:
            _interrupt_at_wait(monkeypatch, client, started)
            assert _INTERRUPTED_CLEANUP_FENCES == {}
        finally:
            release_cleanup.set()
            client.close()


def _stub_interface(monkeypatch: pytest.MonkeyPatch) -> BLEInterface:
    """Build a minimal interface whose connect collaborators are stubbed."""
    interface = BLEInterface.__new__(BLEInterface)
    state_manager = BLEStateManager()
    interface._state_manager = state_manager
    interface._state_lock = state_manager.lock
    interface._management_lock = threading.RLock()
    interface._management_idle_condition = threading.Condition()
    interface._management_inflight = 0
    interface._connect_lock = threading.RLock()
    interface._client_publish_pending = False
    interface._last_connect_pair_override = None
    interface._last_connect_timeout_override = None
    interface.pair_on_connect = False
    interface.address = _TARGET
    interface._adapter = None

    def _no_op(*_args: Any, **_kwargs: Any) -> None:
        """Stub for connect collaborators with no observable effect."""

    sentinel_client = object()

    def _establish(
        _address: str | None,
        _normalized: str | None,
        _key: str | None,
        *,
        pair_on_connect: bool = False,
        connect_timeout: float | None = None,
    ) -> tuple[object, str | None, None]:
        _ = pair_on_connect, connect_timeout
        return sentinel_client, "aabbccddeeff", None

    monkeypatch.setattr(interface, "_validate_connection_preconditions", _no_op)
    monkeypatch.setattr(interface, "_validate_connect_timeout_override", _no_op)
    monkeypatch.setattr(
        interface, "_get_existing_client_if_valid", lambda _request: None
    )
    monkeypatch.setattr(interface, "_raise_if_duplicate_connect", _no_op)
    monkeypatch.setattr(
        interface,
        "_resolve_target_address_for_connect",
        lambda _requested: _TARGET,
    )
    monkeypatch.setattr(interface, "_mark_address_keys_connecting", _no_op)
    monkeypatch.setattr(interface, "_clear_address_keys_connecting", _no_op)
    monkeypatch.setattr(interface, "_finalize_connection_gates", _no_op)
    monkeypatch.setattr(interface, "_verify_and_publish_connected", _no_op)
    monkeypatch.setattr(interface, "_establish_and_update_client", _establish)
    monkeypatch.setattr(
        ble_interface_mod,
        "_clear_stale_connection_before_central_connect",
        _no_op,
    )
    return interface


class TestInterfacePathRefusal:
    """The interface connect path refuses while the fence is active."""

    def test_connect_refused_while_fenced(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        interface = _stub_interface(monkeypatch)
        gating._register_interrupted_cleanup_fence(_TARGET)
        with pytest.raises(
            BLEInterface.BLEError, match="Interrupted connection cleanup"
        ):
            interface.connect(_TARGET)

    def test_refusal_is_independent_of_clearance_policy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Clearance suppression must not bypass the interrupted-cleanup fence."""
        monkeypatch.setattr(BLEConfig, "CLEAR_STALE_CONNECTIONS", False)
        interface = _stub_interface(monkeypatch)
        gating._register_interrupted_cleanup_fence(_TARGET)
        with pytest.raises(
            BLEInterface.BLEError, match="Interrupted connection cleanup"
        ):
            interface.connect(_TARGET)

    def test_connect_allowed_after_fence_retires(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        interface = _stub_interface(monkeypatch)
        deadline = gating._register_interrupted_cleanup_fence(_TARGET)
        gating._retire_interrupted_cleanup_fence(_TARGET, expected_deadline=deadline)
        client = interface.connect(_TARGET)
        assert client is not None


class TestProxyClaimPathRefusal:
    """The proxy claim path refuses while the fence is active."""

    def test_claim_refused_while_fenced(self) -> None:
        gating._register_interrupted_cleanup_fence(_TARGET)
        assert channels._try_claim_ble_address(_TARGET, object()) is False

    def test_claim_allowed_after_fence_retires(self) -> None:
        deadline = gating._register_interrupted_cleanup_fence(_TARGET)
        gating._retire_interrupted_cleanup_fence(_TARGET, expected_deadline=deadline)
        owner = object()
        try:
            assert channels._try_claim_ble_address(_TARGET, owner) is True
        finally:
            gating._clear_connecting(_TARGET, owner=owner)

    def test_open_reports_fenced_address(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A fenced open fails with the fence-specific actionable error."""
        monkeypatch.setattr("bleak.BleakClient", lambda *_args, **_kwargs: MagicMock())
        gating._register_interrupted_cleanup_fence(_TARGET)
        with pytest.raises(RuntimeError, match="is fenced"):
            asyncio.run(BLECentralChannel.open(_TARGET))
