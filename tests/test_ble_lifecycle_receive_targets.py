"""Targeted branch tests for BLE lifecycle and receive services."""

from __future__ import annotations

import contextlib
import importlib
import itertools
import logging
import threading
import time
from types import SimpleNamespace
from typing import Any, Protocol, cast
from unittest.mock import MagicMock

import pytest
from bleak.exc import BleakDBusError, BleakError

try:
    from bleak.exc import BleakGATTProtocolError
except ImportError:  # pragma: no cover - compatibility with older local envs
    BleakGATTProtocolError = BleakError  # type: ignore[assignment, misc]

from meshtastic.interfaces.ble.constants import ERROR_INTERFACE_CLOSING
from meshtastic.interfaces.ble.lifecycle_primitives import (
    _DisconnectPlan,
    _OwnershipSnapshot,
)
from meshtastic.interfaces.ble.lifecycle_service import BLELifecycleService
from meshtastic.interfaces.ble.lifecycle_shutdown_runtime import (
    BLEShutdownLifecycleCoordinator,
)
from meshtastic.interfaces.ble.receive_service import BLEReceiveRecoveryService
from meshtastic.interfaces.ble.state import ConnectionState
from tests.test_ble_interface_fixtures import DummyClient, _build_interface

pytestmark = pytest.mark.unit


class _FatalReceiveError(RuntimeError):
    """Used by receive-loop tests."""


class _StartFailingThread:
    """Thread stub that raises from start() for bounded-shutdown tests."""

    def __init__(self, *, target: object, name: str, daemon: bool) -> None:
        self.target = target
        self.name = name
        self.daemon = daemon

    def start(self) -> None:
        raise RuntimeError("thread start failure")

    def join(self, timeout: float | None = None) -> None:
        del timeout

    def is_alive(self) -> bool:
        return False


class _IfaceWithStateManager(Protocol):
    _state_manager: object


def _make_iface(monkeypatch: pytest.MonkeyPatch) -> Any:
    return _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)


def _make_gatt_protocol_error() -> BaseException:
    """Create a BleakGATTProtocolError across constructor signature variants.

    Returns
    -------
    BaseException
        BleakGATTProtocolError instance compatible with multiple constructor
        signatures, or a ``BleakError("gatt")`` fallback.
    """
    for args in (("gatt",), (1,), ("gatt", 1), ()):
        try:
            return BleakGATTProtocolError(*args)
        except TypeError:
            continue
    return BleakError("gatt")


def _reset_state_manager(iface: _IfaceWithStateManager) -> None:
    iface._state_manager = importlib.import_module(
        "meshtastic.interfaces.ble.state"
    ).BLEStateManager()


def test_lifecycle_error_handler_cleanup_and_execute_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lifecycle error-handler adapters should cover compatibility fallback branches."""
    iface = _make_iface(monkeypatch)
    try:
        legacy_cleanup_calls: list[str] = []

        def _legacy_cleanup(func: Any, _operation_name: str) -> None:
            func()
            legacy_cleanup_calls.append("ran")
            raise RuntimeError("hook post-processing failure")

        iface.error_handler = SimpleNamespace(
            safe_cleanup=None,
            _safe_cleanup=_legacy_cleanup,
        )

        cleanup_calls: list[str] = []
        BLELifecycleService._error_handler_safe_cleanup(
            iface,
            lambda: cleanup_calls.append("cleanup"),
            "cleanup-op",
        )
        assert cleanup_calls == ["cleanup"]
        assert legacy_cleanup_calls == ["ran"]

        iface.error_handler = SimpleNamespace()
        BLELifecycleService._error_handler_safe_cleanup(
            iface,
            lambda: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
            "cleanup-op",
        )

        def _keyword_incompatible_safe_execute(
            func: Any, *args: object, **kwargs: object
        ) -> object:
            if "error_msg" in kwargs:
                raise TypeError(  # noqa: TRY003 - intentional fixture message shape
                    "safe_execute() got an unexpected keyword argument 'error_msg'"
                )
            if args:
                raise TypeError(  # noqa: TRY003 - intentional fixture message shape
                    "takes 1 positional argument but 2 positional arguments were given"
                )
            return func()

        iface.error_handler = SimpleNamespace(
            safe_execute=_keyword_incompatible_safe_execute
        )
        assert (
            BLELifecycleService._error_handler_safe_execute(
                iface,
                lambda: "ok",
                error_msg="execute-error",
            )
            == "ok"
        )

        def _typeerror_after_running(func: Any, **_kwargs: object) -> object:
            func()
            raise TypeError("non-keyword type error")

        iface.error_handler = SimpleNamespace(safe_execute=_typeerror_after_running)
        assert (
            BLELifecycleService._error_handler_safe_execute(
                iface,
                lambda: "ran",
                error_msg="execute-error",
            )
            is None
        )

        iface.error_handler = SimpleNamespace()
        assert (
            BLELifecycleService._error_handler_safe_execute(
                iface,
                lambda: (_ for _ in ()).throw(RuntimeError("fallback failed")),
                error_msg="fallback",
            )
            is None
        )
    finally:
        iface.close()


def test_lifecycle_thread_dispatch_helpers_cover_legacy_and_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lifecycle thread dispatch helpers should exercise public/legacy/missing paths."""
    iface = _make_iface(monkeypatch)
    try:
        created_thread = SimpleNamespace(name="created-thread")
        iface.thread_coordinator = SimpleNamespace(
            create_thread=lambda **_kwargs: created_thread,
            _create_thread=None,
        )
        assert BLELifecycleService._thread_create_thread(
            iface,
            target=lambda: None,
            name="name",
            daemon=True,
        ) is cast(Any, created_thread)

        iface.thread_coordinator = SimpleNamespace()
        with pytest.raises(AttributeError):
            BLELifecycleService._thread_create_thread(
                iface,
                target=lambda: None,
                name="name",
                daemon=True,
            )

        started: list[object] = []
        iface.thread_coordinator = SimpleNamespace(
            _start_thread=lambda thread: started.append(thread),
        )
        marker_thread = SimpleNamespace(name="legacy-start")
        BLELifecycleService._thread_start_thread(iface, marker_thread)
        assert started == [marker_thread]

        iface.thread_coordinator = SimpleNamespace()
        with pytest.raises(AttributeError):
            BLELifecycleService._thread_start_thread(iface, marker_thread)

        join_calls: list[float | None] = []
        iface.thread_coordinator = SimpleNamespace()
        BLELifecycleService._thread_join_thread(
            iface,
            SimpleNamespace(join=lambda timeout=None: join_calls.append(timeout)),
            timeout=1.5,
        )
        assert join_calls == [1.5]

        iface.thread_coordinator = SimpleNamespace(
            _set_event=lambda event_name: started.append(event_name),
            _clear_events=lambda *names: started.extend(names),
            _wake_waiting_threads=lambda *names: started.extend(names),
        )
        BLELifecycleService._thread_set_event(iface, "event-a")
        BLELifecycleService._thread_clear_events(iface, "event-b")
        BLELifecycleService._thread_wake_waiting_threads(iface, "event-c")
        assert "event-a" in started
        assert "event-b" in started
        assert "event-c" in started

        iface.thread_coordinator = SimpleNamespace()
        BLELifecycleService._thread_set_event(iface, "missing")
        BLELifecycleService._thread_clear_events(iface, "missing")
        BLELifecycleService._thread_wake_waiting_threads(iface, "missing")
    finally:
        iface.close()


def test_lifecycle_start_receive_and_schedule_auto_reconnect_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receive-thread start and reconnect scheduling should cover skip/error paths."""
    iface = _make_iface(monkeypatch)
    try:
        with iface._state_lock:
            iface._want_receive = True
            iface._closed = False
            iface._receiveThread = SimpleNamespace(
                name="existing",
                ident=None,
                is_alive=lambda: False,
            )

        BLELifecycleService._start_receive_thread(iface, name="skip-existing")
        with iface._state_lock:
            iface._receiveThread = None

        new_thread = SimpleNamespace(
            name="new-thread", ident=None, is_alive=lambda: False
        )
        monkeypatch.setattr(
            BLELifecycleService,
            "_thread_create_thread",
            staticmethod(lambda *_args, **_kwargs: new_thread),
        )
        monkeypatch.setattr(
            BLELifecycleService,
            "_thread_start_thread",
            staticmethod(lambda *_args, **_kwargs: (_ for _ in ()).throw(SystemExit())),
        )
        with pytest.raises(SystemExit):
            BLELifecycleService._start_receive_thread(iface, name="failing-start")
        assert iface._receiveThread is None

        iface.auto_reconnect = False
        BLELifecycleService._schedule_auto_reconnect(iface)

        iface.auto_reconnect = True
        with iface._state_lock:
            iface._closed = True
        BLELifecycleService._schedule_auto_reconnect(iface)

        with iface._state_lock:
            iface._closed = False
        monkeypatch.setattr(
            BLELifecycleService,
            "_state_manager_is_closing",
            staticmethod(lambda _iface: True),
        )
        BLELifecycleService._schedule_auto_reconnect(iface)

        monkeypatch.setattr(
            BLELifecycleService,
            "_state_manager_is_closing",
            staticmethod(lambda _iface: False),
        )
        iface._reconnect_scheduler = SimpleNamespace()
        with pytest.raises(AttributeError):
            BLELifecycleService._schedule_auto_reconnect(iface)
    finally:
        iface.close()


def test_lifecycle_receive_shim_preserves_method_scoped_collaborators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receive shim should reuse collaborators that only expose the requested method."""
    iface = _make_iface(monkeypatch)
    original_get_lifecycle_controller = iface._get_lifecycle_controller
    try:
        wanted_calls: list[bool] = []
        should_run_calls: list[str] = []
        start_calls: list[tuple[str, bool]] = []

        set_only = SimpleNamespace(
            set_receive_wanted=lambda *, want_receive: wanted_calls.append(want_receive)
        )

        def _should_run_receive_loop() -> bool:
            should_run_calls.append("run")
            return True

        should_only = SimpleNamespace(should_run_receive_loop=_should_run_receive_loop)
        start_only = SimpleNamespace(
            start_receive_thread=lambda *, name, reset_recovery: start_calls.append(
                (name, reset_recovery)
            )
        )

        iface._get_lifecycle_controller = lambda: set_only
        BLELifecycleService._set_receive_wanted(iface, want_receive=True)

        iface._get_lifecycle_controller = lambda: should_only
        assert BLELifecycleService._should_run_receive_loop(iface) is True

        iface._get_lifecycle_controller = lambda: start_only
        BLELifecycleService._start_receive_thread(
            iface,
            name="ScopedStart",
            reset_recovery=False,
        )

        assert wanted_calls == [True]
        assert should_run_calls == ["run"]
        assert start_calls == [("ScopedStart", False)]
    finally:
        iface._get_lifecycle_controller = original_get_lifecycle_controller
        iface.close()


def test_lifecycle_compute_disconnect_keys_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disconnect key computation should cover reconnect and fallback-key branches."""
    from meshtastic.interfaces.ble.gating import _addr_key

    iface = _make_iface(monkeypatch)
    try:
        iface.address = "AA:AA:AA:AA:AA:AA"
        iface._sorted_address_keys = lambda device_key, alias_key: [
            device_key,
            alias_key,
        ]

        reconnect_plan = BLELifecycleService._compute_disconnect_keys(
            iface,
            previous_client=None,
            alias_key="alias",
            should_reconnect=True,
            address="11:22:33:44:55:66",
        )
        assert reconnect_plan[0][0] == _addr_key("11:22:33:44:55:66")
        assert reconnect_plan[1] is True

        unknown_previous = DummyClient()
        unknown_previous.address = "unknown"
        cast(Any, unknown_previous.bleak_client).address = "unknown"
        fallback_plan = BLELifecycleService._compute_disconnect_keys(
            iface,
            previous_client=cast(Any, unknown_previous),
            alias_key="alias",
            should_reconnect=False,
            address="22:33:44:55:66:77",
        )
        assert fallback_plan[0][0] == _addr_key("22:33:44:55:66:77")
    finally:
        iface.close()


def test_lifecycle_resolve_disconnect_target_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disconnect target planning should enforce early-return/state gates."""
    iface = _make_iface(monkeypatch)
    try:
        with iface._state_lock:
            iface.client = DummyClient()
            iface._disconnect_notified = True
            iface._state_manager._transition_to(ConnectionState.CONNECTING)
        assert BLELifecycleService._resolve_disconnect_target(
            iface, "src", None, None
        ).early_return

        owned_connecting_client = DummyClient()
        with iface._state_lock:
            iface.client = owned_connecting_client
            iface._disconnect_notified = False
            iface._client_publish_pending = False
            iface._client_replacement_pending = True
            iface._state_manager._reset_to_disconnected()
            iface._state_manager._transition_to(ConnectionState.CONNECTING)
        owned_connecting_plan = BLELifecycleService._resolve_disconnect_target(
            iface,
            "src",
            cast(Any, owned_connecting_client),
            None,
        )
        assert owned_connecting_plan.early_return is None

        with iface._state_lock:
            iface._state_manager._reset_to_disconnected()
            iface._disconnect_notified = True
        assert BLELifecycleService._resolve_disconnect_target(
            iface, "src", None, None
        ).early_return

        previous = DummyClient()
        previous.address = "AA:BB:CC:DD:EE:FF"
        with iface._state_lock:
            iface.client = previous
            iface._disconnect_notified = False
            iface.auto_reconnect = False

        monkeypatch.setattr(
            BLELifecycleService,
            "_state_manager_transition_to",
            staticmethod(lambda *_args, **_kwargs: False),
        )
        monkeypatch.setattr(
            BLELifecycleService,
            "_state_manager_reset_to_disconnected",
            staticmethod(lambda *_args, **_kwargs: False),
        )
        detailed_plan = BLELifecycleService._resolve_disconnect_target(
            iface, "src", None, None
        )
        assert detailed_plan.early_return is None
    finally:
        iface.close()


def test_lifecycle_execute_disconnect_side_effects_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disconnect side effects should honor stale/provisional plan behavior."""
    iface = _make_iface(monkeypatch)
    try:
        iface.client = DummyClient()
        iface.client.address = "11:22:33:44:55:66"
        iface._sorted_address_keys = lambda *_args: ["current"]
        disconnected_keys: list[str] = []
        iface._mark_address_keys_disconnected = lambda *keys: disconnected_keys.extend(
            [key for key in keys if key is not None]
        )
        closed_previous: list[str] = []
        monkeypatch.setattr(
            BLELifecycleService,
            "_close_previous_client_async",
            staticmethod(
                lambda _iface, _previous_client: closed_previous.append(
                    "closed-previous"
                )
            ),
        )
        stale_plan = _DisconnectPlan(
            early_return=None,
            previous_client=cast(Any, DummyClient()),
            client_at_start=cast(Any, DummyClient()),
            address="addr",
            disconnect_keys=("stale-key",),
            should_reconnect=False,
            should_schedule_reconnect=False,
            was_publish_pending=False,
            was_replacement_pending=False,
        )
        assert BLELifecycleService._execute_disconnect_side_effects(
            iface, plan=stale_plan, source="stale"
        )
        assert disconnected_keys == ["stale-key"]
        assert closed_previous == ["closed-previous"]

        iface.client = None
        iface._connection_session_epoch = 3
        disconnected_keys.clear()
        closed_previous.clear()
        stale_epoch_plan = _DisconnectPlan(
            early_return=None,
            previous_client=cast(Any, DummyClient()),
            client_at_start=cast(Any, DummyClient()),
            session_epoch=2,
            address="addr",
            disconnect_keys=("stale-epoch-key",),
            should_reconnect=False,
            should_schedule_reconnect=False,
            was_publish_pending=False,
            was_replacement_pending=False,
        )
        assert BLELifecycleService._execute_disconnect_side_effects(
            iface, plan=stale_epoch_plan, source="stale-epoch"
        )
        assert disconnected_keys == ["stale-epoch-key"]
        assert closed_previous == ["closed-previous"]
        iface._connection_session_epoch = 0

        iface._disconnected = MagicMock()
        provisional_plan = _DisconnectPlan(
            early_return=None,
            previous_client=None,
            client_at_start=None,
            address="addr",
            disconnect_keys=(),
            should_reconnect=False,
            should_schedule_reconnect=False,
            was_publish_pending=True,
            was_replacement_pending=False,
        )
        iface.client = None
        assert (
            BLELifecycleService._execute_disconnect_side_effects(
                iface,
                plan=provisional_plan,
                source="provisional",
            )
            is False
        )
        iface._disconnected.assert_not_called()
    finally:
        iface.close()


def test_lifecycle_handle_disconnect_lock_busy_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Busy disconnect lock path should report whether reconnect should continue."""
    iface = _make_iface(monkeypatch)
    try:
        iface._disconnect_lock.acquire()
        with iface._state_lock:
            iface.auto_reconnect = True
            iface._want_receive = False
            iface.client = None
            iface._client_publish_pending = False
            iface._client_replacement_pending = False
            iface._closed = False
        assert BLELifecycleService._handle_disconnect(iface, "busy") is True
        with iface._state_lock:
            iface.auto_reconnect = False
        assert BLELifecycleService._handle_disconnect(iface, "busy") is False
    finally:
        if iface._disconnect_lock.locked():
            iface._disconnect_lock.release()
        iface.close()


def test_lifecycle_state_manager_helper_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """State-manager compatibility helpers should cover callable/member/missing paths."""
    iface = _make_iface(monkeypatch)
    try:
        current_state_public = MagicMock(return_value=ConnectionState.CONNECTED)
        transition_to_public = MagicMock(return_value=True)
        reset_to_disconnected_public = MagicMock(return_value=True)
        iface._state_manager = SimpleNamespace(
            is_connected=True,
            _is_connected=False,
            current_state=current_state_public,
            _current_state=ConnectionState.DISCONNECTED,
            transition_to=transition_to_public,
            _transition_to=lambda _state: False,
            reset_to_disconnected=reset_to_disconnected_public,
            _reset_to_disconnected=lambda: False,
            is_closing=True,
            _is_closing=False,
        )
        assert BLELifecycleService._state_manager_is_connected(iface) is True
        assert (
            BLELifecycleService._state_manager_current_state(iface)
            == ConnectionState.CONNECTED
        )
        assert BLELifecycleService._state_manager_transition_to(
            iface, ConnectionState.CONNECTED
        )
        assert BLELifecycleService._state_manager_reset_to_disconnected(iface)
        assert BLELifecycleService._state_manager_is_closing(iface) is True
        current_state_public.assert_called_once_with()
        transition_to_public.assert_called_once_with(ConnectionState.CONNECTED)
        reset_to_disconnected_public.assert_called_once_with()

        iface._state_manager = SimpleNamespace()
        with pytest.raises(AttributeError):
            BLELifecycleService._state_manager_is_connected(iface)
        with pytest.raises(AttributeError):
            BLELifecycleService._state_manager_current_state(iface)
        with pytest.raises(AttributeError):
            BLELifecycleService._state_manager_transition_to(
                iface, ConnectionState.CONNECTED
            )
        with pytest.raises(AttributeError):
            BLELifecycleService._state_manager_reset_to_disconnected(iface)

        bad_client = SimpleNamespace(
            isConnected=lambda: "not-bool",
            is_connected=MagicMock(),
            _is_connected=MagicMock(),
        )
        with pytest.raises(AttributeError):
            BLELifecycleService._client_is_connected(cast(Any, bad_client))
    finally:
        _reset_state_manager(iface)
        iface.close()


def test_lifecycle_invalidation_and_publish_verification_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalidation helpers should cover ownership-loss and verify/publish races."""
    iface = _make_iface(monkeypatch)
    try:
        monkeypatch.setattr(
            "meshtastic.interfaces.ble.lifecycle_service._is_currently_connected_elsewhere",
            lambda key, owner: bool(key == "key2"),
        )
        assert (
            BLELifecycleService._has_lost_gate_ownership(iface, "key1", "key2") is True
        )

        iface.client = DummyClient()
        iface._connection_alias_key = "alias"
        iface._sorted_address_keys = lambda *_keys: ["k1", "k2"]
        iface._mark_address_keys_disconnected = MagicMock()
        iface._discard_invalidated_connected_client = MagicMock()
        with pytest.raises(iface.BLEError, match=ERROR_INTERFACE_CLOSING):
            BLELifecycleService._raise_for_invalidated_connect_result(
                iface,
                cast(Any, DummyClient()),
                "device-key",
                "alias-key",
                is_closing=True,
                lost_gate_ownership=False,
                restore_address=None,
                restore_last_connection_request=None,
            )

        iface._ever_connected = MagicMock()
        assert BLELifecycleService._ever_connected_flag(iface) is False

        snapshots = [
            _OwnershipSnapshot(
                still_owned=True,
                is_closing=False,
                lost_gate_ownership=False,
                prior_ever_connected=False,
            ),
            _OwnershipSnapshot(
                still_owned=True,
                is_closing=False,
                lost_gate_ownership=False,
                prior_ever_connected=False,
            ),
            _OwnershipSnapshot(
                still_owned=False,
                is_closing=False,
                lost_gate_ownership=False,
                prior_ever_connected=False,
            ),
        ]
        monkeypatch.setattr(
            BLELifecycleService,
            "_verify_ownership_snapshot",
            staticmethod(lambda *_args, **_kwargs: snapshots.pop(0)),
        )
        monkeypatch.setattr(
            BLELifecycleService,
            "_get_connected_client_status_locked",
            staticmethod(lambda *_args, **_kwargs: (True, False)),
        )
        iface._raise_for_invalidated_connect_result = MagicMock(
            side_effect=iface.BLEError("invalidated")
        )
        with pytest.raises(iface.BLEError, match="invalidated"):
            BLELifecycleService._verify_and_publish_connected(
                iface,
                cast(Any, DummyClient()),
                "key",
                "alias",
                restore_address=None,
                restore_last_connection_request=None,
            )
    finally:
        iface.close()


def test_lifecycle_finalize_and_cleanup_thread_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Thread-coordinator cleanup and finalize-connection gates should cover fallback branches."""
    iface = _make_iface(monkeypatch)
    try:
        iface.thread_coordinator = SimpleNamespace(
            cleanup=lambda: (_ for _ in ()).throw(RuntimeError("cleanup"))
        )
        BLELifecycleService._cleanup_thread_coordinator(iface)
        iface.thread_coordinator = SimpleNamespace(
            _cleanup=lambda: (_ for _ in ()).throw(RuntimeError("legacy-cleanup"))
        )
        BLELifecycleService._cleanup_thread_coordinator(iface)
        iface.thread_coordinator = SimpleNamespace()
        BLELifecycleService._cleanup_thread_coordinator(iface)

        iface._connection_alias_key = "alias"
        iface._mark_address_keys_connected = MagicMock()
        iface._mark_address_keys_disconnected = MagicMock()
        statuses = [(True, False), (True, False), (False, False)]
        monkeypatch.setattr(
            BLELifecycleService,
            "_get_connected_client_status",
            staticmethod(lambda *_args, **_kwargs: statuses[0]),
        )
        monkeypatch.setattr(
            BLELifecycleService,
            "_get_connected_client_status_locked",
            staticmethod(lambda *_args, **_kwargs: statuses.pop(0)),
        )
        BLELifecycleService._finalize_connection_gates(
            iface,
            cast(Any, DummyClient()),
            "dev",
            "alias",
        )
    finally:
        iface.close()


def test_lifecycle_controller_detects_verify_snapshot_compat_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lifecycle controller should detect verify-snapshot monkeypatch overrides."""
    iface = _make_iface(monkeypatch)
    try:
        controller = iface._get_lifecycle_controller()
        assert controller._uses_compat_connection_status_overrides() is False
        monkeypatch.setattr(
            BLELifecycleService,
            "_verify_ownership_snapshot",
            staticmethod(
                lambda *_args, **_kwargs: _OwnershipSnapshot(
                    still_owned=True,
                    is_closing=False,
                    lost_gate_ownership=False,
                    prior_ever_connected=False,
                )
            ),
        )
        assert controller._uses_compat_connection_status_overrides() is True
    finally:
        iface.close()


def test_lifecycle_controller_keeps_injected_status_getter_when_snapshot_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compat controller path should not inject snapshot fallback over explicit status getter."""
    iface = _make_iface(monkeypatch)
    try:
        controller = iface._get_lifecycle_controller()
        monkeypatch.setattr(
            BLELifecycleService,
            "_verify_ownership_snapshot",
            staticmethod(
                lambda *_args, **_kwargs: _OwnershipSnapshot(
                    still_owned=True,
                    is_closing=False,
                    lost_gate_ownership=False,
                    prior_ever_connected=False,
                )
            ),
        )
        captured_calls: list[tuple[object | None, object | None]] = []

        def _capture_verify_and_publish_connected(
            _connected_client: DummyClient,
            _connected_device_key: str | None,
            _connection_alias_key: str | None,
            *,
            restore_address: str | None,
            restore_last_connection_request: str | None,
            verify_ownership_snapshot: object | None = None,
            get_connected_client_status_locked: object | None = None,
        ) -> None:
            _ = restore_address
            _ = restore_last_connection_request
            captured_calls.append(
                (verify_ownership_snapshot, get_connected_client_status_locked)
            )

        monkeypatch.setattr(
            controller._connection_ownership,
            "_verify_and_publish_connected",
            _capture_verify_and_publish_connected,
            raising=True,
        )

        def status_getter(_client: object) -> tuple[bool, bool]:
            return True, False

        controller._verify_and_publish_connected(
            DummyClient(),
            "dev",
            "alias",
            restore_address=None,
            restore_last_connection_request=None,
            get_connected_client_status_locked=status_getter,
        )
        assert len(captured_calls) == 1
        verify_snapshot, status_locked = captured_calls[0]
        assert verify_snapshot is None
        assert status_locked is status_getter
    finally:
        iface.close()


def test_lifecycle_shutdown_and_finalize_close_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown-management, receive-thread, and final-close helpers should cover fallback paths."""
    iface = _make_iface(monkeypatch)
    try:
        with iface._management_lock:
            iface._management_inflight = 0
        with iface._state_lock:
            iface._closed = False
        monkeypatch.setattr(
            BLELifecycleService,
            "_state_manager_current_state",
            staticmethod(lambda _iface: ConnectionState.CONNECTED),
        )
        monkeypatch.setattr(
            BLELifecycleService,
            "_state_manager_transition_to",
            staticmethod(lambda *_args, **_kwargs: False),
        )
        monkeypatch.setattr(
            BLELifecycleService,
            "_state_manager_reset_to_disconnected",
            staticmethod(lambda *_args, **_kwargs: False),
        )
        assert (
            BLELifecycleService._await_management_shutdown(
                iface,
                management_shutdown_wait_timeout=0.01,
                management_wait_poll_seconds=0.01,
            )
            is False
        )

        never_started = SimpleNamespace(
            name="never", ident=None, is_alive=lambda: False
        )
        iface._receiveThread = never_started
        BLELifecycleService._shutdown_receive_thread(iface)
        assert iface._receiveThread is None

        class _AliveThread:
            ident = 1

            @staticmethod
            def is_alive() -> bool:
                return True

            @staticmethod
            def join(timeout: float | None = None) -> None:
                _ = timeout

        iface._receiveThread = _AliveThread()
        BLELifecycleService._shutdown_receive_thread(iface)

        with iface._state_lock:
            iface._client_publish_pending = False
            iface._client_replacement_pending = True
            iface._disconnect_notified = False
        assert BLELifecycleService._consume_disconnect_notification_state(iface) is True

        unsubscribe_calls: list[str] = []
        cleanup_calls: list[str] = []
        iface._notification_manager = SimpleNamespace(
            unsubscribe_all=lambda *_args, **_kwargs: unsubscribe_calls.append(
                "unsubscribe"
            ),
            cleanup_all=lambda: cleanup_calls.append("cleanup"),
        )
        monkeypatch.setattr(
            BLELifecycleService,
            "_detach_client_for_shutdown",
            staticmethod(lambda _iface: (DummyClient(), False)),
        )
        monkeypatch.setattr(
            BLELifecycleService,
            "_error_handler_safe_cleanup",
            staticmethod(lambda _iface, func, _name: func()),
        )
        iface._management_target_gate = lambda _address: contextlib.nullcontext()
        iface._disconnect_and_close_client = lambda _client: None
        iface._wait_for_disconnect_notifications = lambda: None
        iface._disconnected = lambda: None
        BLELifecycleService._shutdown_client(iface, management_wait_timed_out=False)
        assert unsubscribe_calls == ["unsubscribe"]
        assert cleanup_calls == ["cleanup"]

        with iface._state_lock:
            iface._connection_alias_key = "alias"
        monkeypatch.setattr(
            BLELifecycleService,
            "_state_manager_transition_to",
            staticmethod(lambda *_args, **_kwargs: False),
        )
        monkeypatch.setattr(
            BLELifecycleService,
            "_state_manager_reset_to_disconnected",
            staticmethod(lambda *_args, **_kwargs: False),
        )
        monkeypatch.setattr(
            BLELifecycleService,
            "_state_manager_current_state",
            staticmethod(lambda _iface: ConnectionState.CONNECTING),
        )
        BLELifecycleService._finalize_close_state(iface)
    finally:
        _reset_state_manager(iface)
        iface.close()


def test_shutdown_close_timeout_budget_rolls_forward_to_later_stages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Earlier shutdown work should reduce remaining timeout budgets for later blocking stages."""
    iface = _make_iface(monkeypatch)
    coordinator = BLEShutdownLifecycleCoordinator(iface)
    now = {"value": 100.0}
    captured: dict[str, float | None] = {}

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.lifecycle_shutdown_runtime.time.monotonic",
        lambda: now["value"],
    )

    def _await_management_shutdown(**kwargs: object) -> bool:
        captured["management_timeout"] = cast(
            float, kwargs["management_shutdown_wait_timeout"]
        )
        now["value"] += 0.045
        return False

    def _shutdown_receive_thread(*, join_timeout: float) -> None:
        captured["join_timeout"] = join_timeout
        now["value"] += 0.003

    def _close_mesh_interface(
        *,
        timeout: float | None = None,
        safe_execute: object | None = None,
    ) -> None:
        _ = safe_execute
        captured["mesh_close_timeout"] = timeout
        now["value"] += 0.001

    def _shutdown_client(**kwargs: object) -> None:
        captured["client_disconnect_timeout"] = cast(
            float | None, kwargs["client_disconnect_timeout"]
        )
        captured["disconnect_notification_wait_timeout"] = cast(
            float | None, kwargs["disconnect_notification_wait_timeout"]
        )
        captured["unsubscribe_timeout"] = cast(
            float | None, kwargs["unsubscribe_timeout"]
        )

    cast(Any, coordinator)._await_management_shutdown = _await_management_shutdown
    cast(Any, coordinator)._shutdown_discovery = lambda: None
    cast(Any, coordinator)._shutdown_receive_thread = _shutdown_receive_thread
    cast(Any, coordinator)._close_mesh_interface = _close_mesh_interface
    cast(Any, coordinator)._unregister_exit_handler = lambda: None
    cast(Any, coordinator)._shutdown_client = _shutdown_client
    cast(Any, coordinator)._cleanup_thread_coordinator = (
        lambda *, timeout=None: captured.__setitem__("cleanup_timeout", timeout)
    )
    cast(Any, coordinator)._finalize_close_state = lambda: None

    try:
        coordinator.close(
            management_shutdown_wait_timeout=0.2,
            management_wait_poll_seconds=0.01,
            timeout=0.05,
        )

        assert captured["management_timeout"] == pytest.approx(0.05)
        assert captured["join_timeout"] == pytest.approx(0.005)
        assert captured["mesh_close_timeout"] == pytest.approx(0.002)
        assert captured["client_disconnect_timeout"] == pytest.approx(0.001)
        assert captured["disconnect_notification_wait_timeout"] == pytest.approx(0.001)
        assert captured["unsubscribe_timeout"] == pytest.approx(0.001)
        assert captured["cleanup_timeout"] == pytest.approx(0.001)
    finally:
        iface.close()


def test_close_mesh_interface_timeout_limits_blocking_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_close_mesh_interface should stop waiting once its per-stage timeout is exhausted."""
    iface = _make_iface(monkeypatch)
    coordinator = BLEShutdownLifecycleCoordinator(iface)
    release_close = threading.Event()
    close_called = threading.Event()

    def _blocking_mesh_close(_iface: object) -> None:
        close_called.set()
        release_close.wait(timeout=0.5)

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.lifecycle_shutdown_runtime.MeshInterface.close",
        _blocking_mesh_close,
    )

    try:
        started = time.monotonic()
        try:
            coordinator._close_mesh_interface(timeout=0.01)
        finally:
            release_close.set()
        elapsed = time.monotonic() - started

        assert close_called.is_set()
        assert elapsed < 0.5
    finally:
        iface.close()


def test_lifecycle_shutdown_receive_thread_skips_self_join_by_ident(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown receive-thread join should skip when probed ident matches current thread."""
    iface = _make_iface(monkeypatch)
    try:
        current_ident = threading.get_ident()
        join_calls: list[float | None] = []
        iface._receiveThread = SimpleNamespace(
            ident=current_ident,
            is_alive=lambda: True,
            join=lambda timeout=None: join_calls.append(timeout),
        )
        BLELifecycleService._shutdown_receive_thread(iface)
        assert join_calls == []
    finally:
        iface.close()


def test_receive_controller_hook_resolution_uses_declared_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receive hook resolution should ignore synthesized members and use declared ones."""
    iface = _make_iface(monkeypatch)
    original_close = iface.close

    class _DynamicHookOwner:
        public_hook = staticmethod(lambda: "declared")

        def __getattr__(self, _name: str) -> object:
            return lambda: "dynamic"

    try:
        controller = iface._get_receive_recovery_controller()
        hook = controller._resolve_private_public_callable(
            _DynamicHookOwner(),
            private_name="private_hook",
            public_name="public_hook",
        )
        assert hook is not None
        assert hook() == "declared"
    finally:
        original_close()


def test_receive_controller_filters_missing_lifecycle_controllers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receive controller should treat missing lifecycle controllers as absent."""
    iface = _make_iface(monkeypatch)
    original_close = iface.close
    original_get_lifecycle_controller = iface._get_lifecycle_controller
    try:
        controller = iface._get_receive_recovery_controller()
        monkeypatch.setattr(
            controller,
            "_resolve_iface_receive_hook_override",
            lambda _name: None,
            raising=True,
        )
        iface._get_lifecycle_controller = lambda: None
        assert controller._get_lifecycle_controller() is None
    finally:
        iface._get_lifecycle_controller = original_get_lifecycle_controller
        original_close()


def test_receive_controller_delegates_to_lifecycle_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receive controller should delegate lifecycle-owned operations."""
    iface = _make_iface(monkeypatch)
    original_close = iface.close
    original_get_lifecycle_controller = iface._get_lifecycle_controller
    try:
        controller = iface._get_receive_recovery_controller()
        monkeypatch.setattr(
            controller,
            "_resolve_iface_receive_hook_override",
            lambda _name: None,
            raising=True,
        )
        lifecycle_calls: list[tuple[str, object]] = []

        def _handle_disconnect(reason: str, *, client: object | None = None) -> bool:
            _ = client
            lifecycle_calls.append(("disconnect", reason))
            return True

        iface._get_lifecycle_controller = lambda: SimpleNamespace(
            has_ever_connected_session=lambda: True,
            is_connection_closing=lambda: True,
            should_run_receive_loop=lambda: True,
            set_receive_wanted=lambda *, want_receive: lifecycle_calls.append(
                ("set", want_receive)
            ),
            handle_disconnect=_handle_disconnect,
            start_receive_thread=lambda *, name, reset_recovery: lifecycle_calls.append(
                ("start", (name, reset_recovery))
            ),
            close=lambda **kwargs: lifecycle_calls.append(
                (
                    "close",
                    (
                        kwargs["management_shutdown_wait_timeout"],
                        kwargs["management_wait_poll_seconds"],
                    ),
                )
            ),
        )
        assert controller._has_ever_connected_session() is True
        assert controller._is_connection_closing() is True
        assert controller._should_run_receive_loop() is True
        controller._set_receive_wanted(want_receive=True)
        assert controller._handle_disconnect("reason", client=None) is True
        controller._start_receive_thread(name="rx", reset_recovery=False)
        controller._close_after_fatal_read()
        assert ("set", True) in lifecycle_calls
        assert ("disconnect", "reason") in lifecycle_calls
        assert ("start", ("rx", False)) in lifecycle_calls
        assert any(call[0] == "close" for call in lifecycle_calls)
    finally:
        iface._get_lifecycle_controller = original_get_lifecycle_controller
        original_close()


def test_receive_controller_close_without_usable_hook_skips_iface_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receive controller should avoid recursive default `iface.close()` fallback."""
    iface = _make_iface(monkeypatch)
    original_close = iface.close
    original_get_lifecycle_controller = iface._get_lifecycle_controller
    try:
        controller = iface._get_receive_recovery_controller()
        monkeypatch.setattr(
            controller,
            "_resolve_iface_receive_hook_override",
            lambda _name: None,
            raising=True,
        )
        iface._get_lifecycle_controller = lambda: SimpleNamespace(
            should_run_receive_loop=MagicMock(),
            set_receive_wanted=MagicMock(),
            handle_disconnect=MagicMock(),
            start_receive_thread=MagicMock(),
            close=MagicMock(),
        )
        close_calls: list[str] = []
        monkeypatch.setattr(
            iface,
            "close",
            lambda: close_calls.append("iface-close"),
            raising=True,
        )
        assert controller._should_run_receive_loop() is False
        controller._set_receive_wanted(want_receive=False)
        assert controller._handle_disconnect("ignored", client=None) is False
        controller._start_receive_thread(name="ignored", reset_recovery=True)
        controller._close_after_fatal_read()
        assert close_calls == []

        iface._get_lifecycle_controller = lambda: None
        controller._close_after_fatal_read()
        assert close_calls == []
    finally:
        iface._get_lifecycle_controller = original_get_lifecycle_controller
        original_close()


def test_receive_controller_propagates_disconnect_hook_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disconnect hook failures should be logged and re-raised."""
    iface = _make_iface(monkeypatch)
    original_close = iface.close
    try:
        controller = iface._get_receive_recovery_controller()

        def _raising_override(*_args: object, **_kwargs: object) -> bool:
            raise RuntimeError("disconnect boom")

        monkeypatch.setattr(
            controller,
            "_resolve_iface_receive_hook_override",
            lambda method_name: (
                _raising_override if method_name == "_handle_disconnect" else None
            ),
            raising=True,
        )

        with pytest.raises(RuntimeError, match="disconnect boom"):
            controller._handle_disconnect("reason", client=None)
    finally:
        original_close()


def test_receive_controller_propagates_read_loop_disconnect_override_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read-loop disconnect override failures should be logged and re-raised."""
    iface = _make_iface(monkeypatch)
    original_close = iface.close
    try:
        controller = iface._get_receive_recovery_controller()

        def _raising_override(*_args: object, **_kwargs: object) -> bool:
            raise RuntimeError("read-loop boom")

        monkeypatch.setattr(
            controller,
            "_resolve_iface_receive_hook_override",
            lambda method_name: (
                _raising_override
                if method_name == "_handle_read_loop_disconnect"
                else None
            ),
            raising=True,
        )

        with pytest.raises(RuntimeError, match="read-loop boom"):
            controller.handle_read_loop_disconnect("reason", DummyClient())
    finally:
        original_close()


def test_receive_controller_propagates_should_run_override_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """should_run override failures should be logged and re-raised."""
    iface = _make_iface(monkeypatch)
    original_close = iface.close
    try:
        controller = iface._get_receive_recovery_controller()

        def _raising_override(*_args: object, **_kwargs: object) -> bool:
            raise RuntimeError("should-run boom")

        monkeypatch.setattr(
            controller,
            "_resolve_iface_receive_hook_override",
            lambda method_name: (
                _raising_override if method_name == "_should_run_receive_loop" else None
            ),
            raising=True,
        )

        with pytest.raises(RuntimeError, match="should-run boom"):
            controller._should_run_receive_loop()
    finally:
        original_close()


def test_receive_controller_propagates_lifecycle_should_run_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lifecycle should_run failures should be logged and re-raised."""
    iface = _make_iface(monkeypatch)
    original_close = iface.close
    original_get_lifecycle_controller = iface._get_lifecycle_controller
    try:
        controller = iface._get_receive_recovery_controller()
        monkeypatch.setattr(
            controller,
            "_resolve_iface_receive_hook_override",
            lambda _name: None,
            raising=True,
        )
        iface._get_lifecycle_controller = lambda: SimpleNamespace(
            _should_run_receive_loop=lambda: (_ for _ in ()).throw(
                RuntimeError("lifecycle should-run boom")
            )
        )

        with pytest.raises(RuntimeError, match="lifecycle should-run boom"):
            controller._should_run_receive_loop()
    finally:
        iface._get_lifecycle_controller = original_get_lifecycle_controller
        original_close()


def test_receive_controller_has_ever_connected_session_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receive controller should expose compatibility `_ever_connected` fallback behavior."""
    iface = _make_iface(monkeypatch)
    original_get_lifecycle_controller = iface._get_lifecycle_controller
    try:
        controller = iface._get_receive_recovery_controller()
        monkeypatch.setattr(
            controller,
            "_resolve_iface_receive_hook_override",
            lambda _name: None,
            raising=True,
        )
        iface._get_lifecycle_controller = lambda: None
        iface._ever_connected = MagicMock()
        assert controller._has_ever_connected_session() is False
        iface._ever_connected = True
        assert controller._has_ever_connected_session() is True
    finally:
        iface._get_lifecycle_controller = original_get_lifecycle_controller
        _reset_state_manager(iface)
        iface.close()


def test_receive_controller_is_connection_closing_fallback_branches() -> None:
    """Receive controller should cover state-manager and legacy closing fallbacks."""
    receive_service_mod = importlib.import_module(
        "meshtastic.interfaces.ble.receive_service"
    )
    fallback_iface = SimpleNamespace(
        _state_lock=threading.RLock(),
        _state_manager=None,
        _closed=False,
        _is_connection_closing=MagicMock(),
        _get_lifecycle_controller=lambda: None,
    )
    fallback_controller = receive_service_mod.BLEReceiveRecoveryController(
        fallback_iface
    )
    assert fallback_controller._is_connection_closing() is False

    fallback_iface._is_connection_closing = lambda: True
    assert fallback_controller._is_connection_closing() is True

    state_manager = SimpleNamespace()
    fallback_iface._state_manager = state_manager

    state_manager.is_closing = MagicMock()
    assert fallback_controller._is_connection_closing() is False

    state_manager.is_closing = lambda: True
    assert fallback_controller._is_connection_closing() is True

    def _raise_is_closing() -> bool:
        raise RuntimeError("closing probe failed")

    state_manager.is_closing = _raise_is_closing
    assert fallback_controller._is_connection_closing() is False

    state_manager.is_closing = True
    assert fallback_controller._is_connection_closing() is True

    state_manager.is_closing = "invalid"
    state_manager._is_closing = MagicMock()
    assert fallback_controller._is_connection_closing() is False

    state_manager._is_closing = lambda: True
    assert fallback_controller._is_connection_closing() is True

    def _raise_legacy_is_closing() -> bool:
        raise RuntimeError("legacy closing probe failed")

    state_manager._is_closing = _raise_legacy_is_closing
    assert fallback_controller._is_connection_closing() is False

    state_manager._is_closing = True
    assert fallback_controller._is_connection_closing() is True

    state_manager._is_closing = object()
    assert fallback_controller._is_connection_closing() is False


def test_receive_controller_disconnect_and_override_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receive controller should cover no-lifecycle disconnect and hook-resolution branches."""
    iface = _make_iface(monkeypatch)
    original_get_lifecycle_controller = iface._get_lifecycle_controller
    try:
        controller = iface._get_receive_recovery_controller()
        monkeypatch.setattr(
            controller,
            "_resolve_iface_receive_hook_override",
            lambda _name: None,
            raising=True,
        )
        iface._get_lifecycle_controller = lambda: None
        assert controller._handle_disconnect("no-lifecycle", client=None) is False
        assert (
            controller._is_default_iface_receive_hook("unknown_method", lambda: None)
            is False
        )
        receive_service_mod = importlib.import_module(
            "meshtastic.interfaces.ble.receive_service"
        )
        fallback_iface = SimpleNamespace(
            _state_lock=threading.RLock(),
            _state_manager=None,
            _closed=False,
            _is_connection_closing=False,
            _get_lifecycle_controller=lambda: None,
        )
        fallback_controller = receive_service_mod.BLEReceiveRecoveryController(
            fallback_iface
        )
        assert (
            fallback_controller._resolve_iface_receive_hook_override("_unknown") is None
        )
    finally:
        iface._get_lifecycle_controller = original_get_lifecycle_controller
        _reset_state_manager(iface)
        iface.close()


def test_receive_controller_handle_payload_read_break_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receive controller should break inner loop after a successful payload read."""
    iface = _make_iface(monkeypatch)
    try:
        controller = iface._get_receive_recovery_controller()
        monkeypatch.setattr(
            controller,
            "_read_and_handle_payload",
            lambda *_args, **_kwargs: True,
            raising=True,
        )
        assert controller._handle_payload_read(
            DummyClient(),
            poll_without_notify=False,
        ) == (False, False)
    finally:
        _reset_state_manager(iface)
        iface.close()


def test_receive_controller_transient_error_override_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transient read errors should flow through override hook resolution."""
    iface = _make_iface(monkeypatch)
    try:
        controller = iface._get_receive_recovery_controller()
        override_errors: list[BaseException] = []
        monkeypatch.setattr(
            controller,
            "_resolve_iface_receive_hook_override",
            lambda method_name: (
                lambda error: (
                    override_errors.append(error)
                    if method_name == "_handle_transient_read_error"
                    else None
                )
            ),
            raising=True,
        )
        controller.handle_transient_read_error(BleakError("transient"))
        assert len(override_errors) == 1
        assert isinstance(override_errors[0], BleakError)
    finally:
        _reset_state_manager(iface)
        iface.close()


def test_receive_compat_controller_for_shim_accepts_injected_controller_double(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receive shim should reuse injected doubles only when required method is present."""
    iface = _make_iface(monkeypatch)
    try:
        injected_controller = SimpleNamespace(
            handle_read_loop_disconnect=lambda *_args, **_kwargs: True
        )
        iface._get_receive_recovery_controller = lambda: injected_controller
        assert (
            BLEReceiveRecoveryService._controller_for_shim(
                iface, "handle_read_loop_disconnect"
            )
            is injected_controller
        )
        assert (
            BLEReceiveRecoveryService._controller_for_shim(iface)
            is not injected_controller
        )
    finally:
        iface.close()


def test_receive_compat_controller_for_shim_rejects_dynamic_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receive shim should ignore dynamically synthesized controller placeholders."""
    iface = _make_iface(monkeypatch)
    try:
        iface._get_receive_recovery_controller = lambda: MagicMock()
        resolved = BLEReceiveRecoveryService._controller_for_shim(iface)
        receive_service_mod = importlib.import_module(
            "meshtastic.interfaces.ble.receive_service"
        )
        assert isinstance(resolved, receive_service_mod.BLEReceiveRecoveryController)
    finally:
        iface.close()


def test_receive_service_branch_targets_disconnect_and_wait_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receive service helpers should cover disconnect, wait, state, and process branches."""
    iface = _make_iface(monkeypatch)
    try:
        iface._read_retry_count = 3
        iface._handle_disconnect = lambda *_args, **_kwargs: False
        set_receive_wanted_calls: list[bool] = []
        iface._set_receive_wanted = (
            lambda *, want_receive: set_receive_wanted_calls.append(want_receive)
        )
        assert (
            BLEReceiveRecoveryService._handle_read_loop_disconnect(
                iface,
                "disconnect",
                DummyClient(),
            )
            is False
        )
        assert iface._read_retry_count == 0
        assert set_receive_wanted_calls == [False]

        wait_calls: list[float] = []
        monkeypatch.setattr(
            "meshtastic.interfaces.ble.receive_service._sleep",
            lambda delay: wait_calls.append(delay),
        )
        assert (
            BLEReceiveRecoveryService._coordinator_wait_for_event(
                SimpleNamespace(),
                "evt",
                timeout=None,
            )
            is False
        )
        assert wait_calls

        coordinator = SimpleNamespace(
            _wait_for_event=lambda _event, timeout=None: True,
            _check_and_clear_event=lambda _event: True,
            _clear_event=lambda _event: None,
        )
        assert BLEReceiveRecoveryService._coordinator_wait_for_event(
            coordinator, "evt", timeout=0.1
        )
        assert BLEReceiveRecoveryService._coordinator_check_and_clear_event(
            coordinator, "evt"
        )
        BLEReceiveRecoveryService._coordinator_clear_event(coordinator, "evt")

        iface._fromnum_notify_enabled = False
        with iface._state_lock:
            iface._closed = False
        monkeypatch.setattr(
            BLEReceiveRecoveryService,
            "_coordinator_wait_for_event",
            staticmethod(lambda *_args, **_kwargs: False),
        )
        monkeypatch.setattr(
            BLEReceiveRecoveryService,
            "_coordinator_check_and_clear_event",
            staticmethod(lambda *_args, **_kwargs: True),
        )
        proceed, poll_without_notify = BLEReceiveRecoveryService._wait_for_read_trigger(
            iface,
            coordinator=SimpleNamespace(),
            wait_timeout=0.1,
        )
        assert proceed is True
        assert poll_without_notify is True

        monkeypatch.setattr(
            BLEReceiveRecoveryService,
            "_coordinator_wait_for_event",
            staticmethod(lambda *_args, **_kwargs: True),
        )
        cleared: list[str] = []
        monkeypatch.setattr(
            BLEReceiveRecoveryService,
            "_coordinator_clear_event",
            staticmethod(lambda _coordinator, event_name: cleared.append(event_name)),
        )
        assert BLEReceiveRecoveryService._wait_for_read_trigger(
            iface,
            coordinator=SimpleNamespace(),
            wait_timeout=0.1,
        ) == (True, False)
        assert cleared

        iface._state_manager = SimpleNamespace(
            is_connecting=None,
            _is_connecting=True,
        )
        client, is_connecting, publish_pending, is_closing = (
            BLEReceiveRecoveryService._snapshot_client_state(iface)
        )
        assert client is iface.client
        assert is_connecting is True
        assert isinstance(publish_pending, bool)
        assert isinstance(is_closing, bool)
        _reset_state_manager(iface)

        iface.auto_reconnect = True
        with iface._state_lock:
            iface.client = None
            iface._closed = True
        iface._set_receive_wanted = MagicMock()
        assert BLEReceiveRecoveryService._process_client_state(
            iface,
            coordinator=SimpleNamespace(),
            wait_timeout=0.1,
            client=None,
            is_connecting=False,
            publish_pending=False,
            is_closing=True,
        )
    finally:
        _reset_state_manager(iface)
        iface.close()


def test_receive_service_branch_targets_payload_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receive service helpers should cover payload read and error branches."""
    iface = _make_iface(monkeypatch)
    try:
        iface._read_from_radio_with_retries = lambda *_args, **_kwargs: b""
        assert (
            BLEReceiveRecoveryService._read_and_handle_payload(
                iface,
                DummyClient(),
                poll_without_notify=False,
            )
            is False
        )

        iface._read_from_radio_with_retries = lambda *_args, **_kwargs: b"payload"
        iface._handle_from_radio = lambda _payload: (_ for _ in ()).throw(
            importlib.import_module("meshtastic.interfaces.ble.errors").DecodeError(
                "bad"
            )
        )
        assert BLEReceiveRecoveryService._read_and_handle_payload(
            iface,
            DummyClient(),
            poll_without_notify=False,
        )

        controller = iface._get_receive_recovery_controller()
        monkeypatch.setattr(
            controller,
            "_read_and_handle_payload",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                BleakDBusError("err", ["dbus"])
            ),
        )
        iface._handle_read_loop_disconnect = lambda *_args, **_kwargs: True
        assert BLEReceiveRecoveryService._handle_payload_read(
            iface,
            DummyClient(),
            poll_without_notify=False,
        ) == (True, False)

        monkeypatch.setattr(
            controller,
            "_read_and_handle_payload",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                _make_gatt_protocol_error()
            ),
        )
        iface._handle_read_loop_disconnect = lambda *_args, **_kwargs: True
        assert BLEReceiveRecoveryService._handle_payload_read(
            iface,
            DummyClient(),
            poll_without_notify=False,
        ) == (True, False)

        monkeypatch.setattr(
            controller,
            "_read_and_handle_payload",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(BleakError("transient")),
        )
        iface._handle_transient_read_error = lambda _err: (_ for _ in ()).throw(
            iface.BLEError("fatal")
        )
        original_close = iface.close
        iface.close = MagicMock()
        assert BLEReceiveRecoveryService._handle_payload_read(
            iface,
            DummyClient(),
            poll_without_notify=False,
        ) == (True, True)
        iface.close = original_close
    finally:
        iface.close()


def test_receive_service_branch_targets_cycle_and_recovery_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receive service helpers should cover cycle and recovery branches."""
    iface = _make_iface(monkeypatch)
    try:
        controller = iface._get_receive_recovery_controller()
        monkeypatch.setattr(
            controller,
            "_handle_payload_read",
            lambda *_args, **_kwargs: (True, True),
        )
        monkeypatch.setattr(
            controller,
            "_wait_for_read_trigger",
            lambda *_args, **_kwargs: (True, False),
        )
        monkeypatch.setattr(
            controller,
            "_snapshot_client_state",
            lambda *_args, **_kwargs: (DummyClient(), False, False, False),
        )
        monkeypatch.setattr(
            controller,
            "_process_client_state",
            lambda *_args, **_kwargs: False,
        )
        iface._should_run_receive_loop = MagicMock(side_effect=[True, True, False])
        assert (
            BLEReceiveRecoveryService._run_receive_cycle(
                iface,
                coordinator=SimpleNamespace(),
                wait_timeout=0.1,
            )
            is False
        )

        monkeypatch.setattr(
            controller,
            "_run_receive_cycle",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                _FatalReceiveError("fatal")
            ),
        )
        recovered_reasons: list[str] = []
        monkeypatch.setattr(
            controller,
            "recover_receive_thread",
            lambda reason: recovered_reasons.append(reason),
        )
        BLEReceiveRecoveryService._receive_from_radio_impl(iface)
        assert recovered_reasons == ["receive_thread_fatal"]

        with iface._state_lock:
            iface._closed = False
        iface._handle_disconnect = lambda *_args, **_kwargs: True
        iface._shutdown_event = SimpleNamespace(wait=lambda timeout=None: True)
        with iface._state_lock:
            iface._receive_recovery_attempts = 10
            iface._last_recovery_time = 0.0
        iface._should_run_receive_loop = lambda: False
        BLEReceiveRecoveryService._recover_receive_thread(iface, "reason")
    finally:
        _reset_state_manager(iface)
        iface._shutdown_event = threading.Event()
        iface.close()


def test_receive_service_branch_targets_retry_and_warning_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receive service helpers should cover retry and empty-read warning branches."""
    iface = _make_iface(monkeypatch)
    try:
        read_client = DummyClient()
        monkeypatch.setattr(
            iface,
            "_read_from_radio_with_retries",
            None,
            raising=False,
        )
        payloads = [b"", b"data"]
        cast(Any, read_client).read_gatt_char = lambda *_args, **_kwargs: payloads.pop(
            0
        )
        delays: list[float] = []
        monkeypatch.setattr(
            "meshtastic.interfaces.ble.receive_service._sleep",
            lambda delay: delays.append(delay),
        )
        iface._retry_policy_get_delay = lambda _policy, _attempt: 0.01
        iface._empty_read_policy = object()
        assert BLEReceiveRecoveryService._read_from_radio_with_retries(
            iface, read_client
        )
        assert delays

        monkeypatch.setattr(
            iface,
            "_handle_transient_read_error",
            None,
            raising=False,
        )
        iface._transient_read_policy = object()
        iface._retry_policy_should_retry = lambda _policy, count: count < 1
        iface._retry_policy_get_delay = lambda _policy, _attempt: 0.01
        iface._read_retry_count = 0
        BLEReceiveRecoveryService._handle_transient_read_error(
            iface, BleakError("transient")
        )
        iface._retry_policy_should_retry = lambda _policy, _count: False
        with pytest.raises(iface.BLEError):
            BLEReceiveRecoveryService._handle_transient_read_error(
                iface, BleakError("fatal")
            )

        iface._last_empty_read_warning = 0.0
        iface._suppressed_empty_read_warnings = 2
        BLEReceiveRecoveryService._log_empty_read_warning(iface)
        iface._last_empty_read_warning = 10**9
        BLEReceiveRecoveryService._log_empty_read_warning(iface)
    finally:
        iface.close()


def test_receive_controller_honors_class_level_disconnect_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Class-level disconnect hook overrides should be honored by receive controller."""
    iface = _make_iface(monkeypatch)
    try:
        override_calls: list[tuple[str, DummyClient]] = []

        def _class_override(
            _self: object, error_message: str, previous_client: DummyClient
        ) -> bool:
            override_calls.append((error_message, previous_client))
            return False

        monkeypatch.setattr(
            type(iface),
            "_handle_read_loop_disconnect",
            _class_override,
            raising=True,
        )

        previous_client = DummyClient()
        assert (
            BLEReceiveRecoveryService._handle_read_loop_disconnect(
                iface, "override-test", previous_client
            )
            is False
        )
        assert override_calls == [("override-test", previous_client)]
    finally:
        iface.close()


@pytest.mark.parametrize("raw_ever_connected", [MagicMock(), "invalid-state"])
def test_wait_for_read_trigger_normalizes_non_bool_ever_connected(
    monkeypatch: pytest.MonkeyPatch,
    raw_ever_connected: object,
) -> None:
    """_wait_for_read_trigger should treat non-bool _ever_connected values as False."""
    iface = _make_iface(monkeypatch)
    try:
        with iface._state_lock:
            iface._ever_connected = raw_ever_connected
            iface._fromnum_notify_enabled = False
            iface._closed = False

        reconnected_checks: list[str] = []
        monkeypatch.setattr(
            BLEReceiveRecoveryService,
            "_coordinator_wait_for_event",
            staticmethod(lambda *_args, **_kwargs: False),
        )

        def _check_and_clear_event(*_args: object, **_kwargs: object) -> bool:
            reconnected_checks.append("checked")
            return True

        monkeypatch.setattr(
            BLEReceiveRecoveryService,
            "_coordinator_check_and_clear_event",
            staticmethod(_check_and_clear_event),
        )
        monkeypatch.setattr(
            BLEReceiveRecoveryService,
            "_should_poll_without_notify",
            staticmethod(lambda _iface: True),
        )

        proceed, poll_without_notify = BLEReceiveRecoveryService._wait_for_read_trigger(
            iface,
            coordinator=SimpleNamespace(),
            wait_timeout=0.1,
        )
        assert (proceed, poll_without_notify) == (True, True)
        assert reconnected_checks == []
    finally:
        iface.close()


def test_lifecycle_remaining_error_handler_branch_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lifecycle error-handler compatibility fallbacks should stay best effort."""
    iface = _make_iface(monkeypatch)
    try:
        iface.error_handler = SimpleNamespace(
            safe_cleanup=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                TypeError("non-keyword type error")
            )
        )
        BLELifecycleService._error_handler_safe_cleanup(iface, lambda: None, "cleanup")

        def _safe_execute_non_positional(
            func: Any, *args: object, **kwargs: object
        ) -> object:
            if "error_msg" in kwargs:
                raise TypeError(  # noqa: TRY003 - intentional fixture message shape
                    "safe_execute() got an unexpected keyword argument 'error_msg'"
                )
            if args:
                raise TypeError("totally different positional failure")
            return func()

        iface.error_handler = SimpleNamespace(safe_execute=_safe_execute_non_positional)
        assert (
            BLELifecycleService._error_handler_safe_execute(
                iface,
                lambda: "fallback-none",
                error_msg="msg",
            )
            == "fallback-none"
        )

        def _safe_execute_positional_then_fail(
            func: Any, *args: object, **kwargs: object
        ) -> object:
            if "error_msg" in kwargs:
                raise TypeError(  # noqa: TRY003 - intentional fixture message shape
                    "safe_execute() got an unexpected keyword argument 'error_msg'"
                )
            if args:
                raise TypeError(  # noqa: TRY003 - intentional fixture message shape
                    "takes 1 positional argument but 2 positional arguments were given"
                )
            raise RuntimeError("hook failed")

        iface.error_handler = SimpleNamespace(
            safe_execute=_safe_execute_positional_then_fail
        )
        assert (
            BLELifecycleService._error_handler_safe_execute(
                iface,
                lambda: "unused",
                error_msg="msg",
            )
            == "unused"
        )
    finally:
        iface.close()


def test_lifecycle_remaining_thread_and_disconnect_target_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lifecycle thread dispatch and disconnect-target helpers should cover edge branches."""
    iface = _make_iface(monkeypatch)
    try:
        start_calls: list[object] = []
        join_calls: list[float | None] = []
        set_calls: list[str] = []
        clear_calls: list[str] = []
        wake_calls: list[str] = []
        iface.thread_coordinator = SimpleNamespace(
            start_thread=lambda thread: start_calls.append(thread),
            join_thread=lambda _thread, timeout=None: join_calls.append(timeout),
            set_event=lambda name: set_calls.append(name),
            clear_events=lambda *names: clear_calls.extend(names),
            wake_waiting_threads=lambda *names: wake_calls.extend(names),
        )
        thread_marker = SimpleNamespace(name="thread-marker")
        BLELifecycleService._thread_start_thread(iface, thread_marker)
        BLELifecycleService._thread_join_thread(iface, thread_marker, timeout=0.25)
        BLELifecycleService._thread_set_event(iface, "set-me")
        BLELifecycleService._thread_clear_events(iface, "clear-me")
        BLELifecycleService._thread_wake_waiting_threads(iface, "wake-me")
        assert start_calls == [thread_marker]
        assert join_calls == [0.25]
        assert set_calls == ["set-me"]
        assert clear_calls == ["clear-me"]
        assert wake_calls == ["wake-me"]

        fallback_set_calls: list[str] = []
        iface.thread_coordinator = SimpleNamespace(
            wake_waiting_threads=lambda *_names: (_ for _ in ()).throw(
                RuntimeError("wake failed")
            ),
            set_event=lambda name: fallback_set_calls.append(name),
        )
        BLELifecycleService._thread_wake_waiting_threads(iface, "wake-a", "wake-b")
        assert fallback_set_calls == ["wake-a", "wake-b"]

        iface.thread_coordinator = SimpleNamespace()
        fallback_join_calls: list[float | None] = []
        BLELifecycleService._thread_join_thread(
            iface,
            SimpleNamespace(
                join=lambda timeout=None: fallback_join_calls.append(timeout)
            ),
            timeout=0.1,
        )
        assert fallback_join_calls == [0.1]

        with iface._state_lock:
            iface.client = DummyClient()
            iface._closed = True
            iface._state_manager._reset_to_disconnected()
        assert (
            BLELifecycleService._resolve_disconnect_target(
                iface, "src", None, None
            ).early_return
            is False
        )

        with iface._state_lock:
            iface.client = None
            iface._closed = False
            iface._client_publish_pending = True
            iface._client_replacement_pending = False
            iface._disconnect_notified = False
            iface._state_manager._reset_to_disconnected()
        bleak_client = SimpleNamespace(address="AA:BB:CC:DD:EE:FF")
        plan = BLELifecycleService._resolve_disconnect_target(
            iface, "src", None, cast(Any, bleak_client)
        )
        assert plan.address == "AA:BB:CC:DD:EE:FF"
    finally:
        iface.close()


def test_lifecycle_remaining_close_previous_client_async_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Previous-client async close should cover start success/failure branches."""
    iface = _make_iface(monkeypatch)
    try:
        BLELifecycleService._close_previous_client_async(iface, None)
        previous_client = DummyClient()
        previous_client.address = "AA:BB:CC:DD:EE:FF"

        monkeypatch.setattr(
            BLELifecycleService,
            "_thread_create_thread",
            staticmethod(
                lambda *_args, **_kwargs: SimpleNamespace(
                    ident=None, is_alive=lambda: False
                )
            ),
        )
        monkeypatch.setattr(
            BLELifecycleService,
            "_thread_start_thread",
            staticmethod(lambda *_args, **_kwargs: None),
        )
        BLELifecycleService._close_previous_client_async(
            iface, cast(Any, previous_client)
        )

        monkeypatch.setattr(
            BLELifecycleService,
            "_thread_start_thread",
            staticmethod(lambda *_args, **_kwargs: (_ for _ in ()).throw(SystemExit())),
        )
        with pytest.raises(SystemExit):
            BLELifecycleService._close_previous_client_async(
                iface, cast(Any, previous_client)
            )

        monkeypatch.setattr(
            BLELifecycleService,
            "_thread_start_thread",
            staticmethod(
                lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("fail"))
            ),
        )
        BLELifecycleService._close_previous_client_async(
            iface, cast(Any, previous_client)
        )
    finally:
        iface.close()


def test_lifecycle_remaining_invalidation_and_state_helper_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalidation and state helper branches should remain deterministic."""
    iface = _make_iface(monkeypatch)
    try:
        iface.client = DummyClient()
        iface._client_publish_pending = False
        iface._connection_alias_key = "alias"
        with monkeypatch.context() as m:
            m.setattr(
                BLELifecycleService,
                "_state_manager_is_closing",
                staticmethod(lambda _iface: False),
            )
            m.setattr(
                BLELifecycleService,
                "_state_manager_reset_to_disconnected",
                staticmethod(lambda *_args, **_kwargs: False),
            )
            m.setattr(
                BLELifecycleService,
                "_state_manager_transition_to",
                staticmethod(lambda *_args, **_kwargs: False),
            )
            m.setattr(
                BLELifecycleService,
                "_state_manager_current_state",
                staticmethod(lambda _iface: ConnectionState.CONNECTING),
            )
            BLELifecycleService._discard_invalidated_connected_client(
                iface,
                iface.client,
                restore_address="AA",
                restore_last_connection_request="AA",
            )

        iface._state_manager = SimpleNamespace(
            is_connected=True,
            current_state=lambda: ConnectionState.CONNECTED,
            _current_state=lambda: ConnectionState.DISCONNECTED,
            is_closing=True,
        )
        assert BLELifecycleService._state_manager_is_connected(iface) is True
        assert (
            BLELifecycleService._state_manager_current_state(iface)
            == ConnectionState.CONNECTED
        )
        assert BLELifecycleService._state_manager_is_closing(iface) is True

        iface._state_manager = SimpleNamespace(current_state=ConnectionState.CONNECTED)
        assert (
            BLELifecycleService._state_manager_current_state(iface)
            == ConnectionState.CONNECTED
        )

        iface._state_manager = SimpleNamespace(
            _current_state=lambda: ConnectionState.CONNECTED
        )
        assert (
            BLELifecycleService._state_manager_current_state(iface)
            == ConnectionState.CONNECTED
        )

        client_member = SimpleNamespace(_is_connected=True)
        assert (
            BLELifecycleService._client_is_connected(cast(Any, client_member)) is True
        )

        iface._sorted_address_keys = lambda *_keys: ["key-a", "key-b"]
        iface._mark_address_keys_disconnected = MagicMock()
        iface._discard_invalidated_connected_client = MagicMock()
        with pytest.raises(iface.BLEError):
            BLELifecycleService._raise_for_invalidated_connect_result(
                iface,
                cast(Any, DummyClient()),
                "dev",
                "alias",
                is_closing=False,
                lost_gate_ownership=False,
                restore_address=None,
                restore_last_connection_request=None,
            )
    finally:
        _reset_state_manager(iface)
        iface.close()


def test_lifecycle_remaining_verify_finalize_and_shutdown_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify/publish, gate-finalize, and shutdown helpers should cover remaining branches."""
    iface = _make_iface(monkeypatch)
    try:
        monkeypatch.setattr(
            BLELifecycleService,
            "_verify_ownership_snapshot",
            staticmethod(
                lambda *_args, **_kwargs: _OwnershipSnapshot(
                    still_owned=True,
                    is_closing=False,
                    lost_gate_ownership=False,
                    prior_ever_connected=False,
                )
            ),
        )
        status_values = [(True, False), (True, False), (False, False)]
        monkeypatch.setattr(
            BLELifecycleService,
            "_get_connected_client_status_locked",
            staticmethod(lambda *_args, **_kwargs: status_values.pop(0)),
        )
        iface._client_publish_pending = False
        iface._client_replacement_pending = True
        iface._disconnect_notified = True
        iface.client = DummyClient()
        iface._connected = lambda: None
        disconnected_calls: list[str] = []
        iface._disconnected = lambda: disconnected_calls.append("disconnected")
        iface._emit_verified_connection_side_effects = lambda _client: None
        BLELifecycleService._verify_and_publish_connected(
            iface,
            iface.client,
            "dev",
            "alias",
            restore_address=None,
            restore_last_connection_request=None,
        )
        assert disconnected_calls == ["disconnected"]

        monkeypatch.setattr(
            BLELifecycleService,
            "_verify_ownership_snapshot",
            staticmethod(
                lambda *_args, **_kwargs: _OwnershipSnapshot(
                    still_owned=False,
                    is_closing=False,
                    lost_gate_ownership=False,
                    prior_ever_connected=False,
                )
            ),
        )
        iface._raise_for_invalidated_connect_result = MagicMock(
            side_effect=iface.BLEError("invalid")
        )
        with pytest.raises(iface.BLEError, match="invalid"):
            BLELifecycleService._verify_and_publish_connected(
                iface,
                cast(Any, DummyClient()),
                "dev",
                "alias",
                restore_address=None,
                restore_last_connection_request=None,
            )

        gate_statuses = [(True, False), (False, False)]
        monkeypatch.setattr(
            BLELifecycleService,
            "_get_connected_client_status",
            staticmethod(lambda *_args, **_kwargs: gate_statuses[0]),
        )
        monkeypatch.setattr(
            BLELifecycleService,
            "_get_connected_client_status_locked",
            staticmethod(lambda *_args, **_kwargs: gate_statuses.pop(0)),
        )
        iface._mark_address_keys_connected = lambda *_keys: None
        disconnected_gate_keys: list[str] = []
        iface._mark_address_keys_disconnected = (
            lambda *keys: disconnected_gate_keys.extend(
                [k for k in keys if k is not None]
            )
        )
        BLELifecycleService._finalize_connection_gates(
            iface,
            cast(Any, DummyClient()),
            "dev",
            "alias",
        )
        assert disconnected_gate_keys == []

        iface._receiveThread = threading.current_thread()
        BLELifecycleService._shutdown_receive_thread(iface)

        iface._notification_manager = SimpleNamespace()
        monkeypatch.setattr(
            BLELifecycleService,
            "_detach_client_for_shutdown",
            staticmethod(lambda _iface: (DummyClient(), False)),
        )
        monkeypatch.setattr(
            BLELifecycleService,
            "_error_handler_safe_cleanup",
            staticmethod(lambda _iface, func, _name: func()),
        )
        iface._management_target_gate = lambda _address: contextlib.nullcontext()
        iface._disconnect_and_close_client = lambda _client: None
        iface._wait_for_disconnect_notifications = lambda: None
        iface._disconnected = lambda: None
        BLELifecycleService._shutdown_client(iface, management_wait_timed_out=False)
    finally:
        _reset_state_manager(iface)
        iface.close()


def test_receive_service_remaining_coordinator_and_snapshot_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receive coordinator/snapshot helpers should cover remaining compatibility branches."""
    iface = _make_iface(monkeypatch)
    try:
        wait_calls: list[float] = []
        monkeypatch.setattr(
            "meshtastic.interfaces.ble.receive_service._sleep",
            lambda delay: wait_calls.append(delay),
        )
        assert (
            BLEReceiveRecoveryService._coordinator_wait_for_event(
                SimpleNamespace(wait_for_event=lambda _event, timeout=None: True),
                "evt",
                timeout=0.5,
            )
            is True
        )
        BLEReceiveRecoveryService._coordinator_wait_for_event(
            SimpleNamespace(), "evt", timeout=0.5
        )
        assert wait_calls

        assert (
            BLEReceiveRecoveryService._coordinator_check_and_clear_event(
                SimpleNamespace(check_and_clear_event=lambda _event: True),
                "evt",
            )
            is True
        )
        assert (
            BLEReceiveRecoveryService._coordinator_check_and_clear_event(
                SimpleNamespace(_check_and_clear_event=lambda _event: True),
                "evt",
            )
            is True
        )
        BLEReceiveRecoveryService._coordinator_clear_event(
            SimpleNamespace(clear_events=lambda *_events: None),
            "evt",
        )
        BLEReceiveRecoveryService._coordinator_clear_event(
            SimpleNamespace(clear_event=lambda _event: None),
            "evt",
        )

        iface._state_manager = SimpleNamespace(is_connecting=lambda: True)
        assert BLEReceiveRecoveryService._snapshot_client_state(iface)[1] is True
        iface._state_manager = SimpleNamespace(
            is_connecting=lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        assert BLEReceiveRecoveryService._snapshot_client_state(iface)[1] is False
        iface._state_manager = SimpleNamespace(_is_connecting=lambda: True)
        assert BLEReceiveRecoveryService._snapshot_client_state(iface)[1] is True
        iface._state_manager = SimpleNamespace(
            _is_connecting=lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        assert BLEReceiveRecoveryService._snapshot_client_state(iface)[1] is False
        iface._state_manager = SimpleNamespace(_is_connecting=False)
        assert BLEReceiveRecoveryService._snapshot_client_state(iface)[1] is False
        _reset_state_manager(iface)

        assert BLEReceiveRecoveryService._process_client_state(
            iface,
            coordinator=SimpleNamespace(),
            wait_timeout=0.1,
            client=None,
            is_connecting=False,
            publish_pending=False,
            is_closing=False,
        )

        with iface._state_lock:
            iface._receive_recovery_attempts = 1
            iface._last_recovery_time = 0.0
        BLEReceiveRecoveryService._reset_recovery_after_stability(iface)
    finally:
        _reset_state_manager(iface)
        iface.close()


def test_receive_service_remaining_payload_read_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receive payload-read helpers should cover normal and exceptional branches."""
    iface = _make_iface(monkeypatch)
    try:
        controller = iface._get_receive_recovery_controller()
        iface._read_from_radio_with_retries = lambda *_args, **_kwargs: b"payload"
        iface._handle_from_radio = lambda _payload: None
        assert BLEReceiveRecoveryService._read_and_handle_payload(
            iface,
            DummyClient(),
            poll_without_notify=False,
        )

        monkeypatch.setattr(
            controller,
            "_read_and_handle_payload",
            lambda *_args, **_kwargs: False,
        )
        assert BLEReceiveRecoveryService._handle_payload_read(
            iface,
            DummyClient(),
            poll_without_notify=False,
        ) == (True, False)

        monkeypatch.setattr(
            controller,
            "_read_and_handle_payload",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                BleakDBusError("err", ["dbus"])
            ),
        )
        iface._handle_read_loop_disconnect = lambda *_args, **_kwargs: False
        assert BLEReceiveRecoveryService._handle_payload_read(
            iface,
            DummyClient(),
            poll_without_notify=False,
        ) == (True, True)

        monkeypatch.setattr(
            controller,
            "_read_and_handle_payload",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(SystemExit()),
        )
        with pytest.raises(SystemExit):
            BLEReceiveRecoveryService._handle_payload_read(
                iface,
                DummyClient(),
                poll_without_notify=False,
            )

        monkeypatch.setattr(
            controller,
            "_read_and_handle_payload",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("unexpected")),
        )
        iface._handle_read_loop_disconnect = lambda *_args, **_kwargs: False
        assert BLEReceiveRecoveryService._handle_payload_read(
            iface,
            DummyClient(),
            poll_without_notify=False,
        ) == (True, True)
    finally:
        iface.close()


def test_receive_service_remaining_cycle_and_impl_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receive-cycle and receive-loop implementation should cover remaining branches."""
    iface = _make_iface(monkeypatch)
    try:
        controller = iface._get_receive_recovery_controller()
        monkeypatch.setattr(
            controller,
            "_wait_for_read_trigger",
            lambda *_args, **_kwargs: (False, False),
        )
        iface._should_run_receive_loop = MagicMock(side_effect=[True, False])
        assert (
            BLEReceiveRecoveryService._run_receive_cycle(
                iface,
                coordinator=SimpleNamespace(),
                wait_timeout=0.1,
            )
            is True
        )

        monkeypatch.setattr(
            controller,
            "_wait_for_read_trigger",
            lambda *_args, **_kwargs: (True, False),
        )
        monkeypatch.setattr(
            controller,
            "_snapshot_client_state",
            lambda *_args, **_kwargs: (None, False, False, False),
        )
        iface._should_run_receive_loop = MagicMock(side_effect=[True, True, False])
        assert (
            BLEReceiveRecoveryService._run_receive_cycle(
                iface,
                coordinator=SimpleNamespace(),
                wait_timeout=0.1,
            )
            is True
        )

        monkeypatch.setattr(
            controller,
            "_run_receive_cycle",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(SystemExit()),
        )
        with pytest.raises(SystemExit):
            BLEReceiveRecoveryService._receive_from_radio_impl(iface)

        monkeypatch.setattr(
            controller,
            "_run_receive_cycle",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("fatal")),
        )
        recovered_reasons: list[str] = []
        monkeypatch.setattr(
            controller,
            "recover_receive_thread",
            lambda reason: recovered_reasons.append(reason),
        )
        BLEReceiveRecoveryService._receive_from_radio_impl(iface)
        assert recovered_reasons == ["receive_thread_fatal"]
    finally:
        iface.close()


def test_receive_service_remaining_recovery_and_empty_read_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receive recovery and empty-read warning paths should hit remaining branches."""
    iface = _make_iface(monkeypatch)
    try:
        _reset_state_manager(iface)
        with iface._state_lock:
            iface._closed = True
        BLEReceiveRecoveryService._recover_receive_thread(iface, "reason")

        with iface._state_lock:
            iface._receive_recovery_attempts = 10
            iface._last_recovery_time = 0.0
            iface._closed = False
        iface._handle_disconnect = lambda *_args, **_kwargs: True
        iface._shutdown_event = SimpleNamespace(wait=lambda timeout=None: False)
        iface._should_run_receive_loop = lambda: True
        iface._start_receive_thread = lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("restart failed")
        )
        with pytest.raises(RuntimeError, match="restart failed"):
            BLEReceiveRecoveryService._recover_receive_thread(iface, "reason")

        client = DummyClient()
        cast(Any, client).read_gatt_char = lambda *_args, **_kwargs: b""
        iface._retry_policy_get_delay = lambda _policy, _attempt: 0.01
        iface._empty_read_policy = object()
        empty_read_warning_calls: list[str] = []
        iface._log_empty_read_warning = lambda: empty_read_warning_calls.append(
            "warned"
        )
        assert (
            BLEReceiveRecoveryService._read_from_radio_with_retries(iface, client)
            is None
        )
        assert empty_read_warning_calls == ["warned"]
    finally:
        _reset_state_manager(iface)
        iface._shutdown_event = threading.Event()
        iface.close()


def test_lifecycle_remaining_error_handler_execute_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover remaining lifecycle error-handler execute-hook branches."""
    iface = _make_iface(monkeypatch)
    try:

        def _hook_non_positional_with_func_run(
            func: Any, *args: object, **kwargs: object
        ) -> object:
            if "error_msg" in kwargs:
                raise TypeError(  # noqa: TRY003 - intentional fixture message shape
                    "safe_execute() got an unexpected keyword argument 'error_msg'"
                )
            if args:
                func()
                raise TypeError("non positional-compatible failure")
            return func()

        iface.error_handler = SimpleNamespace(
            safe_execute=_hook_non_positional_with_func_run
        )
        assert (
            BLELifecycleService._error_handler_safe_execute(
                iface,
                lambda: "unused",
                error_msg="line-234",
            )
            is None
        )

        def _hook_positional_then_runtime(
            func: Any, *args: object, **kwargs: object
        ) -> object:
            if "error_msg" in kwargs:
                raise TypeError(  # noqa: TRY003 - intentional fixture message shape
                    "safe_execute() got an unexpected keyword argument 'error_msg'"
                )
            if args:
                raise TypeError(  # noqa: TRY003 - intentional fixture message shape
                    "takes 1 positional argument but 2 positional arguments were given"
                )
            func()
            raise RuntimeError("post-run failure")

        iface.error_handler = SimpleNamespace(
            safe_execute=_hook_positional_then_runtime
        )
        assert (
            BLELifecycleService._error_handler_safe_execute(
                iface,
                lambda: "unused",
                error_msg="line-241",
            )
            is None
        )

        def _hook_runtime_on_positional(
            func: Any, *args: object, **kwargs: object
        ) -> object:
            if "error_msg" in kwargs:
                raise TypeError(  # noqa: TRY003 - intentional fixture message shape
                    "safe_execute() got an unexpected keyword argument 'error_msg'"
                )
            if args:
                raise RuntimeError("positional runtime failure")
            return func()

        iface.error_handler = SimpleNamespace(safe_execute=_hook_runtime_on_positional)
        assert (
            BLELifecycleService._error_handler_safe_execute(
                iface,
                lambda: "unused",
                error_msg="line-243",
            )
            == "unused"
        )

        def _hook_runtime_on_positional_with_func(
            func: Any, *args: object, **kwargs: object
        ) -> object:
            if "error_msg" in kwargs:
                raise TypeError(  # noqa: TRY003 - intentional fixture message shape
                    "safe_execute() got an unexpected keyword argument 'error_msg'"
                )
            if args:
                func()
                raise RuntimeError("positional runtime failure with func")
            return func()

        iface.error_handler = SimpleNamespace(
            safe_execute=_hook_runtime_on_positional_with_func
        )
        assert (
            BLELifecycleService._error_handler_safe_execute(
                iface,
                lambda: "unused",
                error_msg="line-245",
            )
            is None
        )

        def _hook_runtime_on_keyword(func: Any, **_kwargs: object) -> object:
            func()
            raise RuntimeError("keyword runtime failure")

        iface.error_handler = SimpleNamespace(safe_execute=_hook_runtime_on_keyword)
        assert (
            BLELifecycleService._error_handler_safe_execute(
                iface,
                lambda: "unused",
                error_msg="line-248",
            )
            is None
        )
    finally:
        iface.close()


def test_lifecycle_remaining_invalidation_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover remaining lifecycle invalidation and finalize-gate branches."""
    iface = _make_iface(monkeypatch)
    try:
        tracked_client = DummyClient()
        with iface._state_lock:
            iface.client = tracked_client
            iface._client_publish_pending = False
            iface._client_replacement_pending = False
            iface._last_connection_request = "AA:BB"
        with monkeypatch.context() as m:
            m.setattr(
                BLELifecycleService,
                "_state_manager_is_closing",
                staticmethod(lambda _iface: True),
            )
            BLELifecycleService._discard_invalidated_connected_client(
                iface,
                cast(Any, tracked_client),
                restore_address="AA:BB",
                restore_last_connection_request="AA:BB",
            )
        assert iface._last_connection_request is None

        with iface._state_lock:
            iface.client = None
            iface._client_publish_pending = True
            iface._client_replacement_pending = False
            iface._last_connection_request = "pending"
            pending_client = DummyClient()
            iface._connected_publish_inflight_client = pending_client
        with monkeypatch.context() as m:
            m.setattr(
                BLELifecycleService,
                "_state_manager_is_closing",
                staticmethod(lambda _iface: True),
            )
            BLELifecycleService._discard_invalidated_connected_client(
                iface,
                cast(Any, pending_client),
                restore_address="CC:DD",
                restore_last_connection_request="CC:DD",
            )
        assert iface._last_connection_request is None

        disconnected_keys: list[str] = []
        iface._sorted_address_keys = lambda *_keys: ["stale-key"]
        iface._mark_address_keys_disconnected = lambda *keys: disconnected_keys.extend(
            [k for k in keys if k is not None]
        )
        iface._discard_invalidated_connected_client = lambda *_args, **_kwargs: None
        with pytest.raises(iface.BLEError):
            BLELifecycleService._raise_for_invalidated_connect_result(
                iface,
                cast(Any, DummyClient()),
                "device-key",
                "alias-key",
                is_closing=False,
                lost_gate_ownership=True,
                restore_address=None,
                restore_last_connection_request=None,
            )
        assert disconnected_keys == ["stale-key"]

        iface._raise_for_invalidated_connect_result = MagicMock(
            side_effect=iface.BLEError("invalidated")
        )
        monkeypatch.setattr(
            BLELifecycleService,
            "_verify_ownership_snapshot",
            staticmethod(
                lambda *_args, **_kwargs: _OwnershipSnapshot(
                    still_owned=True,
                    is_closing=False,
                    lost_gate_ownership=False,
                    prior_ever_connected=False,
                )
            ),
        )
        monkeypatch.setattr(
            BLELifecycleService,
            "_get_connected_client_status_locked",
            staticmethod(lambda *_args, **_kwargs: (False, False)),
        )
        with pytest.raises(iface.BLEError, match="invalidated"):
            BLELifecycleService._verify_and_publish_connected(
                iface,
                cast(Any, DummyClient()),
                "device-key",
                "alias-key",
                restore_address=None,
                restore_last_connection_request=None,
            )

        cleanup_keys: list[str] = []
        status_values = [(True, False), (False, True)]
        monkeypatch.setattr(
            BLELifecycleService,
            "_get_connected_client_status",
            staticmethod(lambda *_args, **_kwargs: (True, False)),
        )
        monkeypatch.setattr(
            BLELifecycleService,
            "_get_connected_client_status_locked",
            staticmethod(lambda *_args, **_kwargs: status_values.pop(0)),
        )
        iface._mark_address_keys_connected = lambda *_keys: None
        iface._mark_address_keys_disconnected = lambda *keys: cleanup_keys.extend(
            [k for k in keys if k is not None]
        )
        BLELifecycleService._finalize_connection_gates(
            iface,
            cast(Any, DummyClient()),
            "closing-device",
            "closing-alias",
        )
        assert cleanup_keys == ["closing-device", "closing-alias"]
    finally:
        iface.close()


def test_receive_remaining_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover remaining receive-service branch paths."""
    iface = _make_iface(monkeypatch)
    try:
        controller = iface._get_receive_recovery_controller()
        assert (
            BLEReceiveRecoveryService._coordinator_check_and_clear_event(
                SimpleNamespace(), "evt"
            )
            is False
        )

        iface._state_manager = SimpleNamespace()
        _, is_connecting, _, _ = BLEReceiveRecoveryService._snapshot_client_state(iface)
        assert is_connecting is False
        _reset_state_manager(iface)

        monkeypatch.setattr(
            controller,
            "_read_and_handle_payload",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("unexpected")),
        )
        iface._handle_read_loop_disconnect = lambda *_args, **_kwargs: True
        assert BLEReceiveRecoveryService._handle_payload_read(
            iface,
            DummyClient(),
            poll_without_notify=False,
        ) == (True, False)
        iface._handle_read_loop_disconnect = lambda *_args, **_kwargs: False
        assert BLEReceiveRecoveryService._handle_payload_read(
            iface,
            DummyClient(),
            poll_without_notify=False,
        ) == (True, True)

        monkeypatch.setattr(
            controller,
            "_wait_for_read_trigger",
            lambda *_args, **_kwargs: (True, False),
        )
        monkeypatch.setattr(
            controller,
            "_snapshot_client_state",
            lambda *_args, **_kwargs: (None, False, False, False),
        )
        monkeypatch.setattr(
            controller,
            "_process_client_state",
            lambda *_args, **_kwargs: False,
        )
        iface._should_run_receive_loop = MagicMock(side_effect=[True, True, False])
        assert BLEReceiveRecoveryService._run_receive_cycle(
            iface,
            coordinator=SimpleNamespace(),
            wait_timeout=0.1,
        )

        monkeypatch.setattr(
            controller,
            "_snapshot_client_state",
            lambda *_args, **_kwargs: (DummyClient(), False, False, False),
        )
        monkeypatch.setattr(
            controller,
            "_handle_payload_read",
            lambda *_args, **_kwargs: (True, False),
        )
        iface._should_run_receive_loop = MagicMock(side_effect=[True, True, False])
        assert BLEReceiveRecoveryService._run_receive_cycle(
            iface,
            coordinator=SimpleNamespace(),
            wait_timeout=0.1,
        )

        recovered: list[str] = []
        with monkeypatch.context() as m:
            m.setattr(
                controller,
                "_run_receive_cycle",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("fatal")),
            )
            m.setattr(
                controller,
                "recover_receive_thread",
                lambda reason: recovered.append(reason),
            )
            BLEReceiveRecoveryService._receive_from_radio_impl(iface)
        assert recovered == ["receive_thread_fatal"]

        with monkeypatch.context() as m:
            monotonic_values = itertools.chain([100.0, 100.1], itertools.repeat(100.1))
            m.setattr(
                "meshtastic.interfaces.ble.receive_service.time.monotonic",
                lambda: next(monotonic_values),
            )
            with iface._state_lock:
                iface.client = DummyClient()
                iface._closed = False
                iface._receive_recovery_attempts = 3
                iface._last_recovery_time = 99.0
            iface._handle_disconnect = lambda *_args, **_kwargs: True
            iface._shutdown_event = SimpleNamespace(
                wait=lambda timeout=None: False,
                set=lambda: None,
            )
            iface._should_run_receive_loop = lambda: True
            started: list[dict[str, object]] = []
            iface._start_receive_thread = lambda **kwargs: started.append(kwargs)
            BLEReceiveRecoveryService._recover_receive_thread(iface, "recover")
        assert started == [{"name": "BLEReceiveRecovery", "reset_recovery": False}]
        iface._shutdown_event = threading.Event()
    finally:
        iface.close()


def test_shutdown_disconnect_client_skips_legacy_fallback_with_finite_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_shutdown_client should not call unbounded legacy disconnect under a finite budget."""
    iface = _make_iface(monkeypatch)
    coordinator = BLEShutdownLifecycleCoordinator(iface)
    calls: list[tuple[object, dict[str, object]]] = []

    def _disconnect_and_close_client(
        client: object, *, timeout: float | None = None
    ) -> None:
        if timeout is not None:
            raise TypeError(
                "_disconnect_and_close_client() got an unexpected keyword argument 'timeout'"
            )
        calls.append((client, {"timeout": timeout}))

    iface._disconnect_and_close_client = _disconnect_and_close_client
    monkeypatch.setattr(
        coordinator,
        "_await_management_shutdown",
        lambda **kwargs: True,
    )
    try:
        coordinator._shutdown_client(
            management_wait_timed_out=True,
            client_disconnect_timeout=1.0,
            disconnect_notification_wait_timeout=0.5,
            unsubscribe_timeout=0.1,
            bounded_close_timeout_active=True,
        )
        assert calls == []
    finally:
        iface.close()


def test_shutdown_disconnect_client_legacy_fallback_without_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_shutdown_client should permit legacy disconnect fallback without a timeout budget."""
    iface = _make_iface(monkeypatch)
    coordinator = BLEShutdownLifecycleCoordinator(iface)
    calls: list[tuple[object, dict[str, object]]] = []

    def _disconnect_and_close_client(
        client: object, *, timeout: float | None = None
    ) -> None:
        if timeout is not None:
            raise TypeError(
                "_disconnect_and_close_client() got an unexpected keyword argument 'timeout'"
            )
        calls.append((client, {"timeout": timeout}))

    iface._disconnect_and_close_client = _disconnect_and_close_client
    monkeypatch.setattr(
        coordinator,
        "_await_management_shutdown",
        lambda **kwargs: True,
    )
    try:
        coordinator._shutdown_client(
            management_wait_timed_out=True,
            client_disconnect_timeout=None,
            disconnect_notification_wait_timeout=None,
            unsubscribe_timeout=None,
        )
        assert len(calls) == 1
        assert calls[0][1]["timeout"] is None
    finally:
        iface.close()


def test_shutdown_disconnect_client_propagates_unrelated_type_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_shutdown_client should NOT swallow unrelated TypeError from disconnect."""
    iface = _make_iface(monkeypatch)
    coordinator = BLEShutdownLifecycleCoordinator(iface)

    def _disconnect_and_close_client(
        _client: object, *, timeout: float | None = None
    ) -> None:
        del timeout
        raise TypeError("disconnect broke for another reason")

    iface._disconnect_and_close_client = _disconnect_and_close_client
    monkeypatch.setattr(
        coordinator,
        "_await_management_shutdown",
        lambda **kwargs: True,
    )
    try:
        with pytest.raises(TypeError, match="disconnect broke"):
            coordinator._shutdown_client(
                management_wait_timed_out=True,
                client_disconnect_timeout=1.0,
                disconnect_notification_wait_timeout=0.5,
                unsubscribe_timeout=0.1,
                bounded_close_timeout_active=True,
            )
    finally:
        iface.close()


def test_shutdown_wait_for_disconnect_notifications_skips_legacy_fallback_with_finite_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_shutdown_client should not call unbounded legacy wait under a finite budget."""
    iface = _make_iface(monkeypatch)
    coordinator = BLEShutdownLifecycleCoordinator(iface)
    calls: list[dict[str, object]] = []

    def _wait_for_disconnect_notifications(*, timeout: float | None = None) -> None:
        if timeout is not None:
            raise TypeError(
                "_wait_for_disconnect_notifications() got an unexpected keyword argument 'timeout'"
            )
        calls.append({"timeout": timeout})

    iface._wait_for_disconnect_notifications = _wait_for_disconnect_notifications
    monkeypatch.setattr(
        coordinator,
        "_await_management_shutdown",
        lambda **kwargs: True,
    )
    try:
        coordinator._shutdown_client(
            management_wait_timed_out=True,
            client_disconnect_timeout=1.0,
            disconnect_notification_wait_timeout=0.5,
            unsubscribe_timeout=0.1,
            bounded_close_timeout_active=True,
        )
        assert calls == []
    finally:
        iface.close()


def test_shutdown_wait_for_disconnect_notifications_legacy_fallback_without_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_shutdown_client should permit legacy wait fallback without a timeout budget."""
    iface = _make_iface(monkeypatch)
    coordinator = BLEShutdownLifecycleCoordinator(iface)
    calls: list[dict[str, object]] = []

    def _wait_for_disconnect_notifications(*, timeout: float | None = None) -> None:
        if timeout is not None:
            raise TypeError(
                "_wait_for_disconnect_notifications() got an unexpected keyword argument 'timeout'"
            )
        calls.append({"timeout": timeout})

    iface._wait_for_disconnect_notifications = _wait_for_disconnect_notifications
    monkeypatch.setattr(
        coordinator,
        "_await_management_shutdown",
        lambda **kwargs: True,
    )
    try:
        coordinator._shutdown_client(
            management_wait_timed_out=True,
            client_disconnect_timeout=None,
            disconnect_notification_wait_timeout=None,
            unsubscribe_timeout=None,
        )
        assert len(calls) == 1
        assert calls[0]["timeout"] is None
    finally:
        iface.close()


def test_shutdown_wait_for_disconnect_notifications_propagates_unrelated_type_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_shutdown_client should NOT swallow unrelated TypeError from wait."""
    iface = _make_iface(monkeypatch)
    coordinator = BLEShutdownLifecycleCoordinator(iface)

    def _raise_unrelated(*, timeout: float | None = None) -> None:
        del timeout
        raise TypeError("takes 1 positional argument but 2 were given")

    iface._wait_for_disconnect_notifications = _raise_unrelated
    monkeypatch.setattr(
        coordinator,
        "_await_management_shutdown",
        lambda **kwargs: True,
    )
    try:
        with pytest.raises(TypeError, match="takes 1 positional argument"):
            coordinator._shutdown_client(
                management_wait_timed_out=True,
                client_disconnect_timeout=1.0,
                disconnect_notification_wait_timeout=0.5,
                unsubscribe_timeout=0.1,
                bounded_close_timeout_active=True,
            )
    finally:
        iface.close()


def test_shutdown_cleanup_thread_skips_stage_when_start_fails_with_timeout(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """_cleanup_thread_coordinator should skip bounded stage when thread start fails."""
    iface = _make_iface(monkeypatch)
    coordinator = BLEShutdownLifecycleCoordinator(iface)
    cleanup_calls: list[str] = []

    iface.thread_coordinator = SimpleNamespace(
        cleanup=lambda: cleanup_calls.append("cleanup")
    )

    original_thread = threading.Thread

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.lifecycle_shutdown_runtime.threading.Thread",
        _StartFailingThread,
    )

    try:
        with caplog.at_level(logging.WARNING):
            coordinator._cleanup_thread_coordinator(timeout=0.5)
        assert cleanup_calls == []
        assert "Failed to start thread coordinator cleanup thread" in caplog.text
        assert "skipping bounded cleanup stage" in caplog.text
    finally:
        monkeypatch.setattr(
            "meshtastic.interfaces.ble.lifecycle_shutdown_runtime.threading.Thread",
            original_thread,
        )
        iface.close()


def test_shutdown_mesh_close_skips_stage_when_start_fails_with_timeout(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """_close_mesh_interface should skip bounded stage when thread start fails."""
    iface = _make_iface(monkeypatch)
    coordinator = BLEShutdownLifecycleCoordinator(iface)
    close_calls: list[str] = []

    def _track_mesh_close(_iface: object) -> None:
        close_calls.append("mesh-close")

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.lifecycle_shutdown_runtime.MeshInterface.close",
        _track_mesh_close,
    )

    original_thread = threading.Thread

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.lifecycle_shutdown_runtime.threading.Thread",
        _StartFailingThread,
    )

    try:
        with caplog.at_level(logging.WARNING):
            coordinator._close_mesh_interface(timeout=0.5)
        assert close_calls == []
        assert "Failed to start MeshInterface.close thread" in caplog.text
        assert "skipping bounded close stage" in caplog.text
    finally:
        monkeypatch.setattr(
            "meshtastic.interfaces.ble.lifecycle_shutdown_runtime.threading.Thread",
            original_thread,
        )
        iface.close()


def test_shutdown_cleanup_thread_not_duplicated_when_previous_alive(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stuck bounded cleanup thread should suppress later cleanup spawns."""
    iface = _make_iface(monkeypatch)
    coordinator = BLEShutdownLifecycleCoordinator(iface)
    iface.thread_coordinator = SimpleNamespace(cleanup=lambda: None)
    started_threads: list[object] = []

    class _AliveThread:
        def __init__(self, *, target: object, name: str, daemon: bool) -> None:
            self.target = target
            self.name = name
            self.daemon = daemon
            self.started = False

        def start(self) -> None:
            self.started = True
            started_threads.append(self)

        def join(self, timeout: float | None = None) -> None:
            del timeout

        def is_alive(self) -> bool:
            return True

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.lifecycle_shutdown_runtime.threading.Thread",
        _AliveThread,
    )

    try:
        coordinator._cleanup_thread_coordinator(timeout=0.01)
        with caplog.at_level(logging.WARNING):
            coordinator._cleanup_thread_coordinator(timeout=0.01)

        assert len(started_threads) == 1
        assert "previous bounded cleanup thread is still running" in caplog.text
    finally:
        iface.close()


def test_shutdown_mesh_close_thread_not_duplicated_when_previous_alive(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stuck bounded MeshInterface.close thread should suppress later spawns."""
    iface = _make_iface(monkeypatch)
    coordinator = BLEShutdownLifecycleCoordinator(iface)
    started_threads: list[object] = []

    class _AliveThread:
        def __init__(self, *, target: object, name: str, daemon: bool) -> None:
            self.target = target
            self.name = name
            self.daemon = daemon
            self.started = False

        def start(self) -> None:
            self.started = True
            started_threads.append(self)

        def join(self, timeout: float | None = None) -> None:
            del timeout

        def is_alive(self) -> bool:
            return True

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.lifecycle_shutdown_runtime.threading.Thread",
        _AliveThread,
    )

    try:
        coordinator._close_mesh_interface(timeout=0.01)
        with caplog.at_level(logging.WARNING):
            coordinator._close_mesh_interface(timeout=0.01)

        assert len(started_threads) == 1
        assert "previous bounded close thread is still running" in caplog.text
    finally:
        iface.close()


def test_shutdown_bounded_thread_references_clear_after_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bounded cleanup and mesh-close thread references should clear on completion."""
    iface = _make_iface(monkeypatch)
    coordinator = BLEShutdownLifecycleCoordinator(iface)
    iface.thread_coordinator = SimpleNamespace(cleanup=lambda: None)
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.lifecycle_shutdown_runtime.MeshInterface.close",
        lambda _iface: None,
    )

    try:
        coordinator._cleanup_thread_coordinator(timeout=1.0)
        coordinator._close_mesh_interface(timeout=1.0)

        assert coordinator._bounded_cleanup_thread is None
        assert coordinator._bounded_mesh_close_thread is None
    finally:
        iface.close()
