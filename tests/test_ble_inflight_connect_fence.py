"""Address-level fence for in-flight direct BLE connect attempts.

Deferred follow-up to #772: while a direct connect attempt is running it
holds no connected or provisional claim for part of its window, so a
concurrent connect to the same address could start and race it. Each direct
connect attempt now registers an address-level fence when it starts and
retires it when it exits (register-at-entry / retire-at-exit), the
attempt's own caller is exempt from its own fence (per-caller exemption),
and the two #772 consult sites refuse other callers while the fence is
active: ``BLEInterface.connect()`` raises BLEError next to its duplicate
checks regardless of clearance policy, and the proxy claim path refuses
under the per-address lock with a fence-specific error.
"""

from __future__ import annotations

import asyncio
import threading
import time
import weakref
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest

from meshtastic.interface_proxy import channels
from meshtastic.interface_proxy.channels import BLECentralChannel
from meshtastic.interfaces.ble import gating
from meshtastic.interfaces.ble import interface as ble_interface_mod
from meshtastic.interfaces.ble.constants import BLEConfig
from meshtastic.interfaces.ble.gating import _INFLIGHT_CONNECT_FENCES
from meshtastic.interfaces.ble.interface import BLEInterface
from meshtastic.interfaces.ble.state import BLEStateManager

pytestmark = pytest.mark.unit

_TARGET = "AA:BB:CC:DD:EE:FF"


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


def _registration(owner: object) -> gating._InflightConnectFenceRegistration:
    """Build a fence registration carrying ``owner``'s identity."""
    return gating._InflightConnectFenceRegistration(
        owner_ref=weakref.ref(owner),
        owner_id=id(owner),
    )


class _Owner:
    """Weakrefable stand-in owner, like the real interface/channel owners."""

    _next_id = 0

    def __init__(self) -> None:
        """Give this owner a distinguishable identity for debugging."""
        _Owner._next_id += 1
        self.name = f"owner-{_Owner._next_id}"


class TestInflightConnectFenceRegistry:
    """Register/retire/exempt lifecycle of the gating in-flight fence record."""

    def test_register_and_retire(self) -> None:
        """A registered fence refuses other callers until it is retired."""
        assert not gating._inflight_connect_fenced(_TARGET)
        token = gating._register_inflight_connect_fence(_TARGET, owner=_Owner())
        assert token > time.monotonic()
        assert gating._inflight_connect_fenced(_TARGET)
        gating._retire_inflight_connect_fence(_TARGET, expected_deadline=token)
        assert not gating._inflight_connect_fenced(_TARGET)

    def test_retire_is_idempotent(self) -> None:
        """Retiring an already-retired token keeps the address unfenced."""
        token = gating._register_inflight_connect_fence(_TARGET, owner=_Owner())
        gating._retire_inflight_connect_fence(_TARGET, expected_deadline=token)
        gating._retire_inflight_connect_fence(_TARGET, expected_deadline=token)
        assert not gating._inflight_connect_fenced(_TARGET)

    def test_none_address_is_a_noop(self) -> None:
        """Registering, consulting, and retiring None never fences anything."""
        assert gating._register_inflight_connect_fence(None) == 0.0
        assert not gating._inflight_connect_fenced(None)
        gating._retire_inflight_connect_fence(None, expected_deadline=1.0)
        assert not gating._inflight_connect_fenced(None)

    def test_normalization_collapses_address_spellings(self) -> None:
        """All address spellings normalize to the same fence key."""
        gating._register_inflight_connect_fence("aa:bb-cc_dd ee:ff", owner=object())
        assert gating._inflight_connect_fenced("AABBCCDDEEFF")
        assert gating._inflight_connect_fenced(_TARGET)

    def test_registration_is_idempotent_and_accumulates_tokens(self) -> None:
        """Repeated registration refreshes without corrupting prior tokens."""
        owner = object()
        first = gating._register_inflight_connect_fence(_TARGET, owner=owner)
        second = gating._register_inflight_connect_fence(_TARGET, owner=owner)
        assert second >= first
        gating._retire_inflight_connect_fence(_TARGET, expected_deadline=first)
        assert gating._inflight_connect_fenced(_TARGET)
        gating._retire_inflight_connect_fence(_TARGET, expected_deadline=second)
        assert not gating._inflight_connect_fenced(_TARGET)

    def test_same_tick_registrations_have_distinct_retirement_tokens(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Coarse clocks cannot let one exit retire another attempt's fence."""
        monkeypatch.setattr(gating, "time", _FrozenClock(1000.0))
        older = gating._register_inflight_connect_fence(_TARGET, owner=_Owner())
        newer = gating._register_inflight_connect_fence(_TARGET, owner=_Owner())
        assert newer > older
        gating._retire_inflight_connect_fence(_TARGET, expected_deadline=newer)
        assert gating._inflight_connect_fenced(_TARGET)
        gating._retire_inflight_connect_fence(_TARGET, expected_deadline=older)
        assert not gating._inflight_connect_fenced(_TARGET)

    def test_older_exit_cannot_release_newer_refresh(self) -> None:
        """An older attempt's exit must not release a newer attempt's fence."""
        older_owner = object()
        newer_owner = object()
        older = gating._register_inflight_connect_fence(_TARGET, owner=older_owner)
        newer = gating._register_inflight_connect_fence(_TARGET, owner=newer_owner)
        gating._retire_inflight_connect_fence(_TARGET, expected_deadline=older)
        # The newer attempt keeps the address fenced; its owner stays exempt.
        assert gating._inflight_connect_fenced(_TARGET, owner=older_owner)
        assert not gating._inflight_connect_fenced(_TARGET, owner=newer_owner)
        gating._retire_inflight_connect_fence(_TARGET, expected_deadline=newer)
        assert not gating._inflight_connect_fenced(_TARGET, owner=newer_owner)

    def test_registering_owner_is_exempt_from_its_own_fence(self) -> None:
        """The caller performing the in-flight connect is never self-fenced."""
        owner = object()
        gating._register_inflight_connect_fence(_TARGET, owner=owner)
        assert not gating._inflight_connect_fenced(_TARGET, owner=owner)
        assert gating._inflight_connect_fenced(_TARGET, owner=object())
        assert gating._inflight_connect_fenced(_TARGET)

    def test_unowned_consult_is_refused_while_fenced(self) -> None:
        """A consult without an owner identity is refused like any outsider."""
        gating._register_inflight_connect_fence(_TARGET, owner=_Owner())
        assert gating._inflight_connect_fenced(_TARGET, owner=None)

    def test_dead_owner_ref_grants_no_exemption(self) -> None:
        """A garbage-collected owner's identity cannot exempt a new caller."""
        owner = _Owner()
        gating._register_inflight_connect_fence(_TARGET, owner=owner)
        del owner
        # The dead weakref must not fall back to an id() match for a new
        # object; the fence stays up until its deadline backstop.
        assert gating._inflight_connect_fenced(_TARGET, owner=_Owner())

    def test_expired_fence_is_pruned_lazily_on_consult(self) -> None:
        """A fence past its deadline must never wedge the address."""
        _INFLIGHT_CONNECT_FENCES["aabbccddeeff"] = {
            time.monotonic() - 1.0: _registration(_Owner())
        }
        assert not gating._inflight_connect_fenced(_TARGET)
        assert "aabbccddeeff" not in _INFLIGHT_CONNECT_FENCES

    def test_expired_tokens_pruned_while_newer_remains(self) -> None:
        """Pruning expired tokens must keep a newer live registration."""
        now = time.monotonic()
        stale_owner = _Owner()
        live_owner = _Owner()
        live_token = now + 60.0
        _INFLIGHT_CONNECT_FENCES["aabbccddeeff"] = {
            now - 1.0: _registration(stale_owner),
            live_token: _registration(live_owner),
        }
        assert gating._inflight_connect_fenced(_TARGET, owner=stale_owner)
        assert not gating._inflight_connect_fenced(_TARGET, owner=live_owner)
        # Only the expired token was pruned; the live one remains.
        assert list(_INFLIGHT_CONNECT_FENCES["aabbccddeeff"]) == [live_token]
        gating._retire_inflight_connect_fence(_TARGET, expected_deadline=live_token)
        assert _INFLIGHT_CONNECT_FENCES == {}

    def test_invalid_fence_config_keeps_inflight_protection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bad embedder setting must not disable fencing."""
        monkeypatch.setattr(BLEConfig, "INFLIGHT_CONNECT_FENCE_MAX_SECONDS", None)
        token = gating._register_inflight_connect_fence(_TARGET, owner=_Owner())
        assert token > time.monotonic()
        assert gating._inflight_connect_fenced(_TARGET)

    def test_deadline_backstop_releases_never_retired_fence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An attempt that never retires is released by the deadline backstop."""
        monkeypatch.setattr(BLEConfig, "INFLIGHT_CONNECT_FENCE_MAX_SECONDS", 0.05)
        gating._register_inflight_connect_fence(_TARGET, owner=_Owner())
        assert gating._inflight_connect_fenced(_TARGET)
        assert _wait_until(lambda: not gating._inflight_connect_fenced(_TARGET))
        assert _INFLIGHT_CONNECT_FENCES == {}

    def test_clear_all_registries_clears_inflight_fences(self) -> None:
        """Clearing the gate registries drops every in-flight fence record."""
        gating._register_inflight_connect_fence(_TARGET, owner=_Owner())
        gating._clear_all_registries()
        assert _INFLIGHT_CONNECT_FENCES == {}
        assert not gating._inflight_connect_fenced(_TARGET)


class _FrozenClock:
    """Stand-in for the gating module's ``time`` with a controllable clock."""

    def __init__(self, now: float) -> None:
        """Pin the clock at ``now``."""
        self.now = now

    def monotonic(self) -> float:
        """Return the pinned instant."""
        return self.now


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

    def _establish(
        _address: str | None,
        _normalized: str | None,
        _key: str | None,
        *,
        pair_on_connect: bool = False,
        connect_timeout: float | None = None,
    ) -> tuple[object, str | None, None]:
        """Return the stub client without touching the transport."""
        _ = pair_on_connect, connect_timeout
        return object(), "aabbccddeeff", None

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


def _failing_establish(
    _address: str | None,
    _normalized: str | None,
    _key: str | None,
    *,
    failure: BaseException,
    pair_on_connect: bool = False,
    connect_timeout: float | None = None,
) -> tuple[object, str | None, None]:
    """Raise ``failure`` after the attempt's fence has registered."""
    _ = pair_on_connect, connect_timeout
    raise failure


class TestConnectAttemptFenceLifecycle:
    """A direct connect attempt fences at entry and retires at every exit."""

    def test_connect_registers_fence_while_in_flight(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The address is fenced for others while the attempt's work runs."""
        interface = _stub_interface(monkeypatch)
        observed: dict[str, bool] = {}

        def _establish(
            _address: str | None,
            _normalized: str | None,
            _key: str | None,
            *,
            pair_on_connect: bool = False,
            connect_timeout: float | None = None,
        ) -> tuple[object, str | None, None]:
            """Record the fence state seen by the guarded connect work."""
            _ = pair_on_connect, connect_timeout
            observed["fenced_for_others"] = gating._inflight_connect_fenced(_TARGET)
            observed["self_exempt"] = not gating._inflight_connect_fenced(
                _TARGET, owner=interface
            )
            return object(), "aabbccddeeff", None

        monkeypatch.setattr(interface, "_establish_and_update_client", _establish)
        client = interface.connect(_TARGET)
        assert client is not None
        assert observed["fenced_for_others"] is True
        assert observed["self_exempt"] is True

    def test_success_path_retires_fence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A succeeded connect retires its fence at exit."""
        interface = _stub_interface(monkeypatch)
        client = interface.connect(_TARGET)
        assert client is not None
        assert _INFLIGHT_CONNECT_FENCES == {}

    def test_failure_path_retires_fence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failed connect (BLEError) retires its fence at exit."""
        interface = _stub_interface(monkeypatch)
        monkeypatch.setattr(
            interface,
            "_establish_and_update_client",
            lambda *a, **kw: _failing_establish(
                *a,
                failure=BLEInterface.BLEError("establish failed"),
                **kw,
            ),
        )
        with pytest.raises(BLEInterface.BLEError, match="establish failed"):
            interface.connect(_TARGET)
        assert _INFLIGHT_CONNECT_FENCES == {}

    def test_exception_path_retires_fence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unexpected exception retires its fence at exit."""
        interface = _stub_interface(monkeypatch)
        monkeypatch.setattr(
            interface,
            "_establish_and_update_client",
            lambda *a, **kw: _failing_establish(
                *a,
                failure=RuntimeError("transport exploded"),
                **kw,
            ),
        )
        with pytest.raises(RuntimeError, match="transport exploded"):
            interface.connect(_TARGET)
        assert _INFLIGHT_CONNECT_FENCES == {}

    def test_failed_fence_registration_never_masks_connect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fence registration is best effort and must not fail the connect."""
        interface = _stub_interface(monkeypatch)
        monkeypatch.setattr(
            ble_interface_mod,
            "_register_inflight_connect_fence",
            MagicMock(side_effect=TypeError("bad address")),
        )
        client = interface.connect(_TARGET)
        assert client is not None
        assert _INFLIGHT_CONNECT_FENCES == {}

    def test_failed_retirement_still_lets_connect_exit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fence retirement is best effort and must not break the exit path."""
        interface = _stub_interface(monkeypatch)
        monkeypatch.setattr(
            ble_interface_mod,
            "_retire_inflight_connect_fence",
            MagicMock(side_effect=TypeError("bad address")),
        )
        client = interface.connect(_TARGET)
        assert client is not None


class TestInterfacePathRefusal:
    """The interface connect path refuses while another attempt is in flight."""

    def test_connect_refused_while_in_flight(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second interface connect raises while the fence is active."""
        interface = _stub_interface(monkeypatch)
        gating._register_inflight_connect_fence(_TARGET, owner=_Owner())
        with pytest.raises(BLEInterface.BLEError, match="already in flight"):
            interface.connect(_TARGET)

    def test_refusal_is_independent_of_clearance_policy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Clearance suppression must not bypass the in-flight fence."""
        monkeypatch.setattr(BLEConfig, "CLEAR_STALE_CONNECTIONS", False)
        interface = _stub_interface(monkeypatch)
        gating._register_inflight_connect_fence(_TARGET, owner=_Owner())
        with pytest.raises(BLEInterface.BLEError, match="already in flight"):
            interface.connect(_TARGET)

    def test_connect_allowed_after_fence_retires(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The interface connect succeeds once the fence retires."""
        interface = _stub_interface(monkeypatch)
        token = gating._register_inflight_connect_fence(_TARGET, owner=_Owner())
        gating._retire_inflight_connect_fence(_TARGET, expected_deadline=token)
        client = interface.connect(_TARGET)
        assert client is not None

    def test_connect_not_refused_by_its_own_fence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An attempt is exempt from a fence carrying its own identity.

        This pins the no-self-fence contract the auto-reconnect path relies
        on: the reconnect worker calls ``connect()`` on the same interface
        that registered a fence, so the per-caller exemption must admit it.
        """
        interface = _stub_interface(monkeypatch)
        stale = gating._register_inflight_connect_fence(_TARGET, owner=interface)
        client = interface.connect(_TARGET)
        assert client is not None
        # The attempt retired only its own token; the pre-existing one stays.
        assert not gating._inflight_connect_fenced(_TARGET, owner=interface)
        gating._retire_inflight_connect_fence(_TARGET, expected_deadline=stale)
        assert _INFLIGHT_CONNECT_FENCES == {}


class TestProxyClaimPathRefusal:
    """The proxy claim path refuses while an in-flight fence is active."""

    def test_claim_refused_while_in_flight(self) -> None:
        """The proxy claim path refuses while the fence is active."""
        gating._register_inflight_connect_fence(_TARGET, owner=_Owner())
        assert channels._try_claim_ble_address(_TARGET, object()) is False

    def test_claim_exempt_for_registering_owner(self) -> None:
        """The fence's own caller is not refused by its own fence."""
        owner = object()
        token = gating._register_inflight_connect_fence(_TARGET, owner=owner)
        try:
            assert channels._try_claim_ble_address(_TARGET, owner) is True
        finally:
            gating._clear_connecting(_TARGET, owner=owner)
            gating._retire_inflight_connect_fence(_TARGET, expected_deadline=token)

    def test_claim_allowed_after_fence_retires(self) -> None:
        """The proxy claim succeeds once the fence retires."""
        token = gating._register_inflight_connect_fence(_TARGET, owner=_Owner())
        gating._retire_inflight_connect_fence(_TARGET, expected_deadline=token)
        owner = object()
        try:
            assert channels._try_claim_ble_address(_TARGET, owner) is True
        finally:
            gating._clear_connecting(_TARGET, owner=owner)

    def test_open_reports_inflight_fenced_address(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An in-flight-fenced open fails with the fence-specific error."""
        monkeypatch.setattr("bleak.BleakClient", lambda *_args, **_kwargs: MagicMock())
        gating._register_inflight_connect_fence(_TARGET, owner=_Owner())
        with pytest.raises(RuntimeError, match="already in flight"):
            asyncio.run(BLECentralChannel.open(_TARGET))


class TestRacingSecondCaller:
    """Behavioral race pin: a second caller racing an in-flight connect."""

    def test_second_caller_refused_while_in_flight_then_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Refuse a racing caller during the attempt, admit it after exit."""
        interface = _stub_interface(monkeypatch)
        entered_finalize = threading.Event()
        release_finalize = threading.Event()

        def _blocking_finalize(*_args: Any, **_kwargs: Any) -> None:
            """Hold the attempt in flight after its address locks released."""
            entered_finalize.set()
            assert release_finalize.wait(timeout=5)

        monkeypatch.setattr(interface, "_finalize_connection_gates", _blocking_finalize)
        result: dict[str, object] = {}
        attempt = threading.Thread(
            target=lambda: result.update(client=interface.connect(_TARGET)),
            name="in-flight-connect-attempt",
        )
        attempt.start()
        assert entered_finalize.wait(timeout=5)

        # In flight: the fence refuses a proxy claim from another caller...
        assert gating._inflight_connect_fenced(_TARGET)
        assert channels._try_claim_ble_address(_TARGET, object()) is False
        # ...and a second interface connect racing the same address.
        racer = _stub_interface(monkeypatch)
        with pytest.raises(BLEInterface.BLEError, match="already in flight"):
            racer.connect(_TARGET)

        release_finalize.set()
        attempt.join(timeout=5)
        assert not attempt.is_alive()
        assert result["client"] is not None

        # After exit: the fence retired, so both consult sites admit callers.
        assert not gating._inflight_connect_fenced(_TARGET)
        assert _INFLIGHT_CONNECT_FENCES == {}
        owner = object()
        try:
            assert channels._try_claim_ble_address(_TARGET, owner) is True
        finally:
            gating._clear_connecting(_TARGET, owner=owner)
