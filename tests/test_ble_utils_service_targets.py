"""Targeted branch-coverage tests for BLE utility/service helpers."""

from __future__ import annotations

import contextlib
import importlib
import inspect
import logging
from queue import Empty, Full
from threading import Event, RLock
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

import meshtastic.interfaces.ble.compatibility_service as compatibility_mod
import meshtastic.interfaces.ble.utils as ble_utils
from meshtastic.interfaces.ble.compatibility_service import (
    BLECompatibilityEventPublisher,
    BLECompatibilityEventService,
)
from meshtastic.interfaces.ble.coordination import ThreadCoordinator, _InertThread
from meshtastic.interfaces.ble.errors import BLEErrorHandler
from meshtastic.interfaces.ble.management_runtime import (
    _create_management_client,
    _is_blank_or_malformed_address_like,
)
from meshtastic.interfaces.ble.management_service import (
    BLEManagementCommandHandler,
    BLEManagementCommandsService,
)
from meshtastic.interfaces.ble.management_state import BLEManagementState
from meshtastic.interfaces.ble.reconnection import (
    ReconnectPolicyMissingMethodError,
    ReconnectScheduler,
    ReconnectWorker,
    ThreadCoordinatorMissingMethodError,
)
from meshtastic.interfaces.ble.session_state import BLESessionState
from meshtastic.interfaces.ble.state import BLEStateManager
from meshtastic.util import DeferredExecution
from tests.test_ble_interface_fixtures import DummyClient, _build_interface

pytestmark = pytest.mark.unit


class _QueueFailure(Exception):
    """Raised by queue test doubles."""


def test_call_factory_with_optional_kwarg_handles_signature_and_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Optional-kwarg helper should handle signature probe errors and fallback retries."""
    captured_kwargs: list[dict[str, object]] = []

    def _factory_with_signature_failure(*_args: object, **kwargs: object) -> str:
        captured_kwargs.append(dict(kwargs))
        return "ok"

    monkeypatch.setattr(
        inspect,
        "signature",
        lambda _func: (_ for _ in ()).throw(TypeError("no-signature")),
        raising=True,
    )
    assert (
        ble_utils._call_factory_with_optional_kwarg(
            _factory_with_signature_failure,
            optional_kwarg="flag",
            optional_value=False,
        )
        == "ok"
    )
    assert captured_kwargs == [{"flag": False}]

    rejected_errors: list[TypeError] = []
    retry_calls: list[dict[str, object]] = []

    def _rejecting_factory(*_args: object, **kwargs: object) -> str:
        retry_calls.append(dict(kwargs))
        if kwargs:
            raise TypeError("factory() got an unexpected keyword argument 'flag'")
        return "retried"

    result = ble_utils._call_factory_with_optional_kwarg(
        _rejecting_factory,
        optional_kwarg="flag",
        optional_value=True,
        on_kwarg_rejected=rejected_errors.append,
    )
    assert result == "retried"
    assert len(retry_calls) == 2
    assert rejected_errors


def test_safe_execute_adapter_covers_kwarg_stripping_and_fallback_paths() -> None:
    """safe_execute adapter should strip unsupported kwargs and fallback correctly."""
    adapter_calls: list[dict[str, object]] = []

    def _compat_safe_execute(func: Any, **kwargs: object) -> object:
        adapter_calls.append(dict(kwargs))
        for rejected in ("default_return", "error_msg", "reraise"):
            if rejected in kwargs:
                raise TypeError(
                    f"safe_execute() got an unexpected keyword argument '{rejected}'"
                )
        return func()

    iface = SimpleNamespace(
        error_handler=SimpleNamespace(safe_execute=_compat_safe_execute)
    )
    assert (
        ble_utils._safe_execute_through_adapter(
            iface,
            lambda: "value",
            default_return="fallback",
            error_msg="adapter error",
        )
        == "value"
    )
    assert adapter_calls

    def _always_typeerror(_func: Any, **_kwargs: object) -> object:
        raise TypeError("totally different type error")

    iface_typeerror = SimpleNamespace(
        error_handler=SimpleNamespace(safe_execute=_always_typeerror)
    )
    assert (
        ble_utils._safe_execute_through_adapter(
            iface_typeerror,
            lambda: "unused",
            default_return="typed-fallback",
            error_msg="adapter type error",
            reraise=False,
        )
        == "typed-fallback"
    )
    with pytest.raises(TypeError):
        ble_utils._safe_execute_through_adapter(
            iface_typeerror,
            lambda: "unused",
            default_return="typed-fallback",
            error_msg="adapter type error",
            reraise=True,
        )

    iface_no_hook = SimpleNamespace(error_handler=SimpleNamespace())

    with pytest.raises(RuntimeError):
        ble_utils._safe_execute_through_adapter(
            iface_no_hook,
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            default_return=None,
            error_msg="fallback error",
            reraise=True,
        )

    assert (
        ble_utils._safe_execute_through_adapter(
            iface_no_hook,
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            default_return="no-hook-default",
            error_msg="fallback error",
            reraise=False,
        )
        == "no-hook-default"
    )


def test_thread_start_probe_handles_invalid_ident_and_is_alive_errors() -> None:
    """Thread probe should normalize invalid fields and swallow probe exceptions."""

    def _raise_alive() -> bool:
        raise _QueueFailure

    fake_thread = SimpleNamespace(ident="bad-ident", is_alive=_raise_alive)
    assert ble_utils._thread_start_probe(fake_thread) == (None, False)


def test_compatibility_event_service_enqueue_paths() -> None:
    """Callback enqueue helper should cover non-blocking and fallback queue paths."""

    def callback() -> None:
        return None

    dead_thread = SimpleNamespace(
        queueWork=lambda _cb: None,
        queue=SimpleNamespace(put_nowait=lambda _cb: None),
        thread=SimpleNamespace(is_alive=lambda: False),
    )
    assert (
        BLECompatibilityEventService._enqueue_publish_callback(
            dead_thread,
            callback,
            prefer_non_blocking=False,
        )
        is False
    )

    no_put_nowait = SimpleNamespace(
        queueWork=None, queue=SimpleNamespace(), thread=None
    )
    assert (
        BLECompatibilityEventService._enqueue_publish_callback(
            no_put_nowait,
            callback,
            prefer_non_blocking=True,
        )
        is False
    )

    queued_from_full: list[object] = []
    full_nonblocking = SimpleNamespace(
        queueWork=lambda cb: queued_from_full.append(cb),
        queue=SimpleNamespace(put_nowait=lambda _cb: (_ for _ in ()).throw(Full())),
        thread=None,
    )
    assert (
        BLECompatibilityEventService._enqueue_publish_callback(
            full_nonblocking,
            callback,
            prefer_non_blocking=True,
        )
        is False
    )
    assert queued_from_full == []

    queued: list[object] = []
    queue_work_only = SimpleNamespace(
        queueWork=lambda cb: queued.append(cb),
        queue=SimpleNamespace(),
        thread=None,
    )
    assert (
        BLECompatibilityEventService._enqueue_publish_callback(
            queue_work_only,
            callback,
            prefer_non_blocking=False,
        )
        is True
    )
    assert queued == [callback]

    queued_nowait: list[object] = []
    put_nowait_only = SimpleNamespace(
        queueWork=None,
        queue=SimpleNamespace(put_nowait=lambda cb: queued_nowait.append(cb)),
        thread=None,
    )
    assert (
        BLECompatibilityEventService._enqueue_publish_callback(
            put_nowait_only,
            callback,
            prefer_non_blocking=False,
        )
        is True
    )
    assert queued_nowait == [callback]


def test_nonblocking_enqueue_starts_lazy_publishing_worker() -> None:
    """The first BLE status callback must activate a lazy publishing executor."""
    completed = Event()
    publishing_thread = DeferredExecution._create_lazy("test-ble-publishing")

    try:
        assert publishing_thread.thread is None
        assert BLECompatibilityEventService._enqueue_publish_callback(
            publishing_thread,
            completed.set,
            prefer_non_blocking=True,
        )
        assert publishing_thread.thread is not None
        assert completed.wait(timeout=1.0)
    finally:
        publishing_thread.stop()
        publishing_thread.join(timeout=1.0)


def test_wait_for_disconnect_notifications_handles_timeout_and_dead_publisher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disconnect-notification wait should drain when publisher thread is not alive."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)

    class _FakeEvent:
        def __init__(self) -> None:
            self._set = False
            self.wait_calls: list[float | None] = []

        def set(self) -> None:
            self._set = True

        def wait(self, timeout: float | None = None) -> bool:
            self.wait_calls.append(timeout)
            return False

        def is_set(self) -> bool:
            return self._set

    drained: list[object] = []
    monkeypatch.setattr(compatibility_mod, "Event", _FakeEvent, raising=True)
    monkeypatch.setattr(
        BLECompatibilityEventService,
        "_safe_execute",
        staticmethod(lambda *_args, **_kwargs: True),
    )
    monkeypatch.setattr(
        BLECompatibilityEventService,
        "drain_publish_queue",
        staticmethod(lambda *_args, **_kwargs: drained.append("drained")),
    )

    publishing_thread = SimpleNamespace(
        thread=SimpleNamespace(is_alive=lambda: (_ for _ in ()).throw(RuntimeError())),
        queueWork=lambda _cb: None,
        queue=SimpleNamespace(put_nowait=lambda _cb: None),
    )
    BLECompatibilityEventService.wait_for_disconnect_notifications(
        iface,
        timeout=0.01,
        publishing_thread=publishing_thread,
    )
    assert drained == ["drained"]


def test_drain_publish_queue_fallback_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Queue drain should fallback from thread drain and stop after configured iterations."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)

    flush_event = Event()

    def _thread_drain(_event: Event) -> None:
        raise RuntimeError("thread-drain-failure")

    runnable_calls: list[str] = []

    def _runnable() -> None:
        runnable_calls.append("ran")

    callbacks = [_runnable, _runnable, _runnable]

    def _get_nowait() -> Any:
        if callbacks:
            return callbacks.pop(0)
        raise Empty

    publishing_thread = SimpleNamespace(
        thread=SimpleNamespace(_drain_publish_queue=_thread_drain),
        queue=SimpleNamespace(get_nowait=_get_nowait),
    )

    monkeypatch.setattr(compatibility_mod, "MAX_DRAIN_ITERATIONS", 2, raising=True)

    BLECompatibilityEventService.drain_publish_queue(
        iface,
        flush_event,
        publishing_thread=publishing_thread,
    )
    assert runnable_calls == ["ran", "ran"]

    BLECompatibilityEventService.drain_publish_queue(
        iface,
        flush_event,
        publishing_thread=SimpleNamespace(thread=None, queue=SimpleNamespace()),
    )


def test_publish_connection_status_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Connection-status publish should handle unavailable pub and legacy wrapper paths."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)

    import meshtastic.mesh_interface as mesh_iface_module

    monkeypatch.setattr(mesh_iface_module, "pub", None, raising=True)
    BLECompatibilityEventService.publish_connection_status(
        iface,
        connected=True,
        publishing_thread=SimpleNamespace(queueWork=lambda _cb: None, queue=None),
    )

    class _RaisingPub:
        @staticmethod
        def sendMessage(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("publish failed")

    monkeypatch.setattr(mesh_iface_module, "pub", _RaisingPub(), raising=True)
    BLECompatibilityEventService.publish_connection_status_legacy(
        iface,
        connected=False,
        publishing_thread=SimpleNamespace(queueWork=None, queue=SimpleNamespace()),
    )


def test_publish_connection_status_legacy_resolves_default_publishing_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy status-publish shim should resolve default thread when kwarg is omitted."""
    import meshtastic.mesh_interface as mesh_iface_module

    sent: list[tuple[str, object, bool]] = []

    def _send_message(topic: str, *, interface: object, connected: bool) -> None:
        sent.append((topic, interface, connected))

    monkeypatch.setattr(
        mesh_iface_module,
        "pub",
        SimpleNamespace(sendMessage=_send_message),
        raising=True,
    )

    publishing_thread = SimpleNamespace(
        queue=SimpleNamespace(put_nowait=lambda callback: callback()),
        thread=None,
    )
    iface = SimpleNamespace(_get_publishing_thread=lambda: publishing_thread)

    BLECompatibilityEventService.publish_connection_status_legacy(
        iface,  # type: ignore[arg-type]
        connected=True,
    )

    assert sent == [("meshtastic.connection.status", iface, True)]


def test_status_publish_ignores_synthesized_closing_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A synthesized closing hook must not suppress an otherwise valid publish."""
    import meshtastic.mesh_interface as mesh_iface_module
    from meshtastic.interfaces.ble import (
        compatibility_service as compatibility_service_mod,
    )

    sent: list[tuple[str, object, bool]] = []

    class _StateManager:
        _is_closing = False

        def __getattr__(self, name: str) -> object:
            if name == "is_closing":
                return lambda: True
            raise AttributeError(name)

    class _ImmediateThread:
        def __init__(self, *, target: Any, **_kwargs: object) -> None:
            self._target = target

        def start(self) -> None:
            self._target()

    iface = SimpleNamespace(_closed=False, _state_manager=_StateManager())
    monkeypatch.setattr(
        mesh_iface_module,
        "pub",
        SimpleNamespace(
            sendMessage=lambda topic, *, interface, connected: sent.append(
                (topic, interface, connected)
            )
        ),
        raising=True,
    )
    monkeypatch.setattr(
        compatibility_service_mod, "Thread", _ImmediateThread, raising=True
    )

    BLECompatibilityEventService.publish_connection_status(
        iface,  # type: ignore[arg-type]
        connected=True,
        publishing_thread=None,
    )

    assert sent == [("meshtastic.connection.status", iface, True)]


def test_status_publish_closing_probe_failure_is_best_effort(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Closing-state probe failures must not escape best-effort publication."""
    import meshtastic.mesh_interface as mesh_iface_module
    from meshtastic.interfaces.ble import (
        compatibility_service as compatibility_service_mod,
    )

    sent: list[tuple[str, object, bool]] = []

    class _RaisingStateManager:
        @property
        def is_closing(self) -> bool:
            raise RuntimeError("closing probe failed")

    class _ImmediateThread:
        def __init__(self, *, target: Any, **_kwargs: object) -> None:
            self._target = target

        def start(self) -> None:
            self._target()

    iface = SimpleNamespace(_closed=False, _state_manager=_RaisingStateManager())
    monkeypatch.setattr(
        mesh_iface_module,
        "pub",
        SimpleNamespace(
            sendMessage=lambda topic, *, interface, connected: sent.append(
                (topic, interface, connected)
            )
        ),
        raising=True,
    )
    monkeypatch.setattr(
        compatibility_service_mod, "Thread", _ImmediateThread, raising=True
    )
    caplog.set_level(logging.DEBUG, logger="meshtastic.ble")

    BLECompatibilityEventService.publish_connection_status(
        iface,  # type: ignore[arg-type]
        connected=True,
        publishing_thread=None,
    )

    assert sent == [("meshtastic.connection.status", iface, True)]
    assert any(
        getattr(record, "ble_failure_disposition", None) == "compatibility_fallback"
        and "Error probing interface closing state" in record.getMessage()
        for record in caplog.records
    )


def test_legacy_status_publish_ignores_synthesized_thread_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy status publishing should only call a declared provider hook."""
    import meshtastic.mesh_interface as mesh_iface_module

    sent: list[tuple[str, object, bool]] = []
    fallback_thread = SimpleNamespace(
        queue=SimpleNamespace(put_nowait=lambda callback: callback()),
        thread=None,
    )

    class _Iface:
        def __init__(self) -> None:
            self.synthesized_provider_calls = 0

        def __getattr__(self, name: str) -> object:
            if name == "_get_publishing_thread":

                def _synthesized_provider() -> object:
                    self.synthesized_provider_calls += 1
                    raise AssertionError("synthesized provider must be ignored")

                return _synthesized_provider
            raise AttributeError(name)

    iface = _Iface()
    monkeypatch.setattr(
        mesh_iface_module, "publishingThread", fallback_thread, raising=True
    )
    monkeypatch.setattr(
        mesh_iface_module,
        "pub",
        SimpleNamespace(
            sendMessage=lambda topic, *, interface, connected: sent.append(
                (topic, interface, connected)
            )
        ),
        raising=True,
    )

    BLECompatibilityEventService.publish_connection_status_legacy(
        iface,  # type: ignore[arg-type]
        connected=True,
    )

    assert sent == [("meshtastic.connection.status", iface, True)]
    assert iface.synthesized_provider_calls == 0


def test_coordination_inert_thread_start_is_noop() -> None:
    """Starting an inert coordinator thread should only warn and return."""
    coordinator = ThreadCoordinator()
    inert = _InertThread(name="never-start")
    coordinator._start_thread(inert)


def test_reconnection_error_types_and_hook_resolution() -> None:
    """Reconnect helper types and coordinator-hook resolution should cover missing paths."""
    assert "ReconnectPolicy missing method 'next_attempt'" in str(
        ReconnectPolicyMissingMethodError("next_attempt")
    )
    assert "Thread coordinator is missing start_thread/_start_thread" in str(
        ThreadCoordinatorMissingMethodError("start_thread/_start_thread")
    )

    scheduler = ReconnectScheduler(
        state_manager=BLEStateManager(),
        state_lock=RLock(),
        thread_coordinator=cast(
            Any,
            SimpleNamespace(
                create_thread=lambda **_kwargs: SimpleNamespace(
                    ident=None, is_alive=lambda: False
                ),
                start_thread=lambda _thread: None,
            ),
        ),
        interface=cast(
            Any,
            SimpleNamespace(
                _is_connection_closing=False, _can_initiate_connection=True
            ),
        ),
    )
    hook = scheduler._resolve_thread_coordinator_hook("create_thread", "_create_thread")
    assert callable(hook)

    scheduler.thread_coordinator = cast(Any, SimpleNamespace())
    with pytest.raises(ThreadCoordinatorMissingMethodError):
        scheduler._resolve_thread_coordinator_hook("create_thread", "_create_thread")


def test_reconnection_schedule_thread_not_started_and_policy_missing_method() -> None:
    """Reconnect scheduler should clear stale thread refs and worker should raise for missing methods."""
    scheduler = ReconnectScheduler(
        state_manager=BLEStateManager(),
        state_lock=RLock(),
        thread_coordinator=cast(
            Any,
            SimpleNamespace(
                create_thread=lambda **_kwargs: SimpleNamespace(
                    ident=None, is_alive=lambda: False
                ),
                start_thread=lambda _thread: None,
            ),
        ),
        interface=cast(
            Any,
            SimpleNamespace(
                _is_connection_closing=False, _can_initiate_connection=True
            ),
        ),
    )

    should_reconnect = True
    assert scheduler._schedule_reconnect(should_reconnect, Event()) is False

    worker = ReconnectWorker(
        interface=cast(Any, SimpleNamespace(auto_reconnect=True)),
        reconnect_policy=cast(Any, SimpleNamespace()),
    )
    with pytest.raises(ReconnectPolicyMissingMethodError):
        worker._call_policy("next_attempt")


def test_management_helpers_cover_factory_and_target_edge_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Management helper branches should handle factory fallback and target drift."""

    def _reject_kwarg(address: str, **kwargs: object) -> DummyClient:
        if kwargs:
            raise TypeError(  # noqa: TRY003 - intentional test fixture message shape
                "factory() got an unexpected keyword argument 'log_if_no_address'"
            )
        client = DummyClient()
        client.address = address
        return client

    client = _create_management_client(cast(Any, _reject_kwarg), "AA:BB:CC:DD:EE:FF")
    assert isinstance(client, DummyClient)

    with pytest.raises(TypeError, match="returned None"):
        _create_management_client(lambda _address, **_kwargs: None, "AA:BB:CC:DD:EE:FF")

    assert _is_blank_or_malformed_address_like("AA:BB:CC:DD:EE:FF") is False
    assert _is_blank_or_malformed_address_like("aabbccddeeff") is False
    assert _is_blank_or_malformed_address_like("Kitchen:Node") is False
    assert _is_blank_or_malformed_address_like("AA:BB:CC") is True
    assert _is_blank_or_malformed_address_like("AABBCCDDEEF") is False
    assert _is_blank_or_malformed_address_like("AABBCCDDEEFF00") is False

    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    try:
        iface._finish_management_operation = MagicMock()
        iface._validate_management_preconditions = MagicMock(return_value=None)
        iface._get_management_client_if_available_locked = MagicMock(
            side_effect=RuntimeError("target resolution failure")
        )

        with pytest.raises(RuntimeError, match="target resolution failure"):
            BLEManagementCommandsService._start_management_phase(iface, None)
        with iface._management_lock:
            assert iface._management_inflight == 0
    finally:
        iface.close()

    fresh_iface = _build_interface(
        monkeypatch, DummyClient(), start_receive_thread=False
    )
    try:
        start_context = SimpleNamespace(
            target_address=None,
            use_existing_client_without_resolved_address=True,
            expected_implicit_binding="AA:BB:CC:DD:EE:FF",
        )
        fresh_iface._get_management_client_if_available_locked = lambda _address: (
            DummyClient()
        )
        fresh_iface._resolve_target_address_for_management = lambda _address: None
        fresh_iface._validate_management_preconditions = lambda: None
        fresh_iface._get_current_implicit_management_binding_locked = (
            lambda: "11:22:33:44:55:66"
        )

        with pytest.raises(fresh_iface.BLEError, match="changed"):
            BLEManagementCommandsService._resolve_management_target(
                fresh_iface, None, start_context
            )
    finally:
        fresh_iface.close()


def test_management_shim_handler_resolution_preserves_minimal_iface_double(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shim handler resolution should preserve minimal iface-owned handler doubles."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    try:
        minimal_handler = SimpleNamespace(
            execute_management_command=lambda *_args, **_kwargs: "ok"
        )
        iface._get_management_command_handler = cast(  # type: ignore[method-assign]
            Any, lambda: minimal_handler
        )
        resolved = BLEManagementCommandsService._handler_for_shim(iface)
        assert resolved is cast(Any, minimal_handler)
        assert BLEManagementCommandsService._has_required_handler_entrypoint(
            minimal_handler
        )

        iface._get_management_command_handler = MagicMock(  # type: ignore[method-assign]
            side_effect=lambda: MagicMock()
        )
        fallback_handler = BLEManagementCommandsService._handler_for_shim(iface)
        assert isinstance(fallback_handler, BLEManagementCommandHandler)
        assert (
            BLEManagementCommandsService._has_required_handler_entrypoint(object())
            is False
        )
    finally:
        iface.close()


def _minimal_management_handler(
    *, client_closer: Any = None
) -> BLEManagementCommandHandler:
    """Build a handler with explicit synchronization state and no iface fallbacks."""
    iface = SimpleNamespace()
    return BLEManagementCommandHandler(
        iface,  # type: ignore[arg-type]
        ble_client_factory=cast(Any, DummyClient),
        connected_elsewhere=lambda *_args, **_kwargs: False,
        session_state=BLESessionState(RLock()),
        management_state=BLEManagementState(),
        connect_lock=RLock(),
        client_closer=client_closer,
    )


def test_management_handler_requires_client_cleanup_capability() -> None:
    """A handler without any closer should report the missing cleanup capability."""
    handler = _minimal_management_handler()

    with pytest.raises(AttributeError, match="client cleanup capability"):
        handler._close_client(cast(Any, DummyClient()))


def test_management_handler_close_failure_does_not_mask_command_result() -> None:
    """Best-effort temporary cleanup must not replace a successful command result."""

    def _failing_close(_client: object) -> None:
        raise RuntimeError("close failed")

    handler = _minimal_management_handler(client_closer=_failing_close)

    result = handler.execute_with_client(
        client_to_use=cast(Any, DummyClient()),
        temporary_client=cast(Any, DummyClient()),
        command=lambda _client: "ok",
    )

    assert result == "ok"


def test_management_handler_revalidates_refreshed_client_under_shared_locks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refreshed client should be revalidated while all coordination locks are held."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    try:
        handler = BLEManagementCommandHandler(
            iface,
            ble_client_factory=cast(Any, DummyClient),
            connected_elsewhere=lambda *_args, **_kwargs: False,
            session_state=iface._session_state,
            management_state=iface._management_state,
            connect_lock=iface._connect_lock,
            client_closer=iface._client_manager_safe_close_client,
        )
        refreshed_client = DummyClient()
        start_context = SimpleNamespace(
            expected_implicit_binding=None,
            target_address="AA:BB:CC:DD:EE:FF",
            use_existing_client_without_resolved_address=False,
        )
        preconditions = MagicMock()
        monkeypatch.setattr(iface, "_validate_management_preconditions", preconditions)
        monkeypatch.setattr(
            handler, "_start_management_phase", lambda _address: start_context
        )
        monkeypatch.setattr(
            handler,
            "_resolve_management_target",
            lambda _address, _context: ("AA:BB:CC:DD:EE:FF", refreshed_client),
        )
        monkeypatch.setattr(handler, "_is_client_connected", lambda _client: True)
        monkeypatch.setattr(
            iface,
            "_management_target_gate",
            lambda _target: contextlib.nullcontext(),
        )
        monkeypatch.setattr(handler, "finish_management_operation", lambda: None)

        result = handler.execute_management_command(
            "AA:BB:CC:DD:EE:FF",
            lambda client: client,
        )

        assert result is cast(Any, refreshed_client)
        preconditions.assert_called_once_with()
    finally:
        iface.close()


def test_management_handler_call_iface_override_respects_instance_and_subclass_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_call_iface_override should ignore base wrappers but honor real overrides."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    try:
        handler = BLEManagementCommandHandler(
            iface,
            ble_client_factory=cast(Any, DummyClient),
            connected_elsewhere=lambda *_args, **_kwargs: False,
        )
        fallback_calls: list[str] = []

        def _fallback(address: str | None) -> str:
            fallback_calls.append("fallback")
            return f"fallback:{address}"

        assert (
            handler._call_iface_override(
                "_resolve_target_address_for_management",
                _fallback,
                "mesh-node",
            )
            == "fallback:mesh-node"
        )
        assert fallback_calls == ["fallback"]

        class _SynthesizingInterface:
            def __init__(self) -> None:
                self.synthesized_override_calls = 0

            def __getattr__(self, name: str) -> object:
                if name == "_resolve_target_address_for_management":

                    def _synthesized(address: str | None) -> str:
                        self.synthesized_override_calls += 1
                        return f"synthesized:{address}"

                    return _synthesized
                raise AttributeError(name)

        synthesizing_iface = _SynthesizingInterface()
        synthesizing_handler = BLEManagementCommandHandler(
            synthesizing_iface,  # type: ignore[arg-type]
            ble_client_factory=cast(Any, DummyClient),
            connected_elsewhere=lambda *_args, **_kwargs: False,
        )
        assert (
            synthesizing_handler._call_iface_override(
                "_resolve_target_address_for_management",
                _fallback,
                "dynamic-node",
            )
            == "fallback:dynamic-node"
        )
        assert synthesizing_iface.synthesized_override_calls == 0

        iface._resolve_target_address_for_management = lambda address: (  # type: ignore[method-assign]
            f"override:{address}"
        )
        assert (
            handler._call_iface_override(
                "_resolve_target_address_for_management",
                _fallback,
                "mesh-node",
            )
            == "override:mesh-node"
        )
        assert fallback_calls == ["fallback", "fallback"]

        class _OverrideInterface(type(iface)):  # type: ignore[misc]
            def _resolve_target_address_for_management(
                self, address: str | None
            ) -> str:
                return f"subclass:{address}"

        subclass_iface = object.__new__(_OverrideInterface)
        subclass_handler = BLEManagementCommandHandler(
            subclass_iface,
            ble_client_factory=cast(Any, DummyClient),
            connected_elsewhere=lambda *_args, **_kwargs: False,
        )
        assert (
            subclass_handler._call_iface_override(
                "_resolve_target_address_for_management",
                _fallback,
                "mesh-node",
            )
            == "subclass:mesh-node"
        )
        assert fallback_calls == ["fallback", "fallback"]
    finally:
        iface.close()


def test_management_shim_default_connected_elsewhere_and_handler_like(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Management shim should cover strict handler-like positives and default ownership probe."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    original_get_management_handler = iface._get_management_command_handler
    original_direct_handler = getattr(iface, "_management_command_handler", None)
    try:
        required_methods = (
            "resolve_target_address_for_management",
            "management_target_gate",
            "get_management_client_if_available",
            "get_management_client_for_target",
            "get_current_implicit_management_binding_locked",
            "get_current_implicit_management_address_locked",
            "revalidate_implicit_management_target",
            "start_management_phase",
            "resolve_management_target",
            "acquire_client_for_target",
            "execute_with_client",
            "execute_management_command",
            "begin_management_operation_locked",
            "finish_management_operation",
            "validate_management_await_timeout",
            "validate_trust_timeout",
            "validate_connect_timeout_override",
            "pair",
            "unpair",
            "run_bluetoothctl_trust_command",
            "trust",
        )
        handler_like_candidate = SimpleNamespace(
            **{
                method_name: (lambda *_args, **_kwargs: None)
                for method_name in required_methods
            }
        )
        assert BLEManagementCommandsService._is_handler_like(handler_like_candidate)

        connected_elsewhere_calls: list[tuple[str | None, object | None]] = []
        late_bound_calls: list[tuple[str | None, object | None]] = []

        def _connected_elsewhere(key: str | None, owner: object | None = None) -> bool:
            connected_elsewhere_calls.append((key, owner))
            return True

        def _late_bound(key: str | None, owner: object | None = None) -> bool:
            late_bound_calls.append((key, owner))
            return True

        monkeypatch.setattr(
            "meshtastic.interfaces.ble.management_compat_service._is_currently_connected_elsewhere",
            _connected_elsewhere,
        )
        monkeypatch.setattr(
            iface,
            "_connected_elsewhere_late_bound",
            _late_bound,
            raising=True,
        )
        iface._get_management_command_handler = cast(  # type: ignore[method-assign]
            Any, lambda: None
        )
        resolved_direct_handler = BLEManagementCommandsService._handler_for_shim(iface)
        assert resolved_direct_handler is original_direct_handler

        iface._management_command_handler = None  # type: ignore[assignment]
        resolved_handler = BLEManagementCommandsService._handler_for_shim(iface)
        assert isinstance(resolved_handler, BLEManagementCommandHandler)
        assert resolved_handler._connected_elsewhere("device-key", iface) is True
        assert late_bound_calls == [("device-key", iface)]
        assert connected_elsewhere_calls == []
    finally:
        iface._get_management_command_handler = original_get_management_handler  # type: ignore[method-assign]
        iface._management_command_handler = cast(Any, original_direct_handler)
        iface.close()


def test_resolve_management_target_existing_client_explicit_address_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_resolve_management_target should validate explicit existing-client paths and resolver fallback."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    try:
        start_context = SimpleNamespace(
            target_address=None,
            use_existing_client_without_resolved_address=True,
            expected_implicit_binding=None,
        )
        iface._validate_management_preconditions = lambda: None  # type: ignore[method-assign]
        iface._get_management_client_if_available_locked = cast(  # type: ignore[method-assign]
            Any, lambda _address: None
        )
        resolved_addresses: list[str | None] = []

        def _resolve_target_address(address: str | None) -> str:
            resolved_addresses.append(address)
            return "AA:BB:CC:DD:EE:FF"

        iface._resolve_target_address_for_management = (  # type: ignore[method-assign]
            _resolve_target_address
        )

        target_missing_client, active_client_missing_client = (
            BLEManagementCommandsService._resolve_management_target(
                iface, "target-id", cast(Any, start_context)
            )
        )
        assert target_missing_client == "AA:BB:CC:DD:EE:FF"
        assert active_client_missing_client is None
        assert resolved_addresses == ["target-id"]

        refreshed_client = DummyClient()
        refreshed_client.address = None  # type: ignore[assignment]
        refreshed_client.bleak_client = SimpleNamespace(address=None)
        resolved_addresses.clear()

        iface._get_management_client_if_available_locked = cast(  # type: ignore[method-assign]
            Any, lambda _address: refreshed_client
        )
        iface._extract_client_address = cast(  # type: ignore[method-assign]
            Any, lambda _client: None
        )

        target, active_client = BLEManagementCommandsService._resolve_management_target(
            iface,
            "target-id",
            cast(Any, start_context),
        )
        assert target == "AA:BB:CC:DD:EE:FF"
        assert active_client is None
        assert resolved_addresses == ["target-id"]
    finally:
        iface.close()


def test_management_execute_with_client_preserves_command_outcome_on_close_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Temporary-client close failures should not mask command result."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    try:
        iface._client_manager_safe_close_client = MagicMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("close failed")
        )

        result = BLEManagementCommandsService._execute_with_client(
            iface,
            client_to_use=cast(Any, DummyClient()),
            temporary_client=cast(Any, DummyClient()),
            command=lambda _client: "ok",
        )
        assert result == "ok"
    finally:
        iface.close()


def test_management_execute_management_command_existing_client_and_trust_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Management command and trust helpers should cover missing-target and subprocess branches."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    bluetooth_iface = _build_interface(
        monkeypatch, DummyClient(), start_receive_thread=False
    )
    try:
        start_context = SimpleNamespace(
            expected_implicit_binding=None,
            target_address=None,
            use_existing_client_without_resolved_address=False,
        )
        monkeypatch.setattr(
            BLEManagementCommandHandler,
            "_start_management_phase",
            lambda _self, _address: start_context,
        )
        refreshed_client = DummyClient()
        monkeypatch.setattr(
            BLEManagementCommandHandler,
            "_resolve_management_target",
            lambda _self, _address, _ctx: (None, refreshed_client),
        )
        monkeypatch.setattr(
            BLEManagementCommandHandler,
            "_acquire_client_for_target",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()),
        )

        assert BLEManagementCommandsService._execute_management_command(
            iface,
            None,
            lambda client: client,
            ble_client_factory=cast(Any, DummyClient),
            connected_elsewhere=lambda *_args: False,
        ) is cast(Any, refreshed_client)

        bluetooth_iface._format_bluetoothctl_address = (  # type: ignore[method-assign]
            lambda address: address
        )
        bluetooth_iface._management_target_gate = (  # type: ignore[method-assign]
            cast(Any, lambda _address: contextlib.nullcontext())
        )

        class _SubprocessModule:
            TimeoutExpired = RuntimeError

            @staticmethod
            def run(*_args: object, **_kwargs: object) -> object:
                raise ValueError("unexpected non-oserror")

        with pytest.raises(bluetooth_iface.BLEError, match="trust"):
            BLEManagementCommandsService._run_bluetoothctl_trust_command(
                bluetooth_iface,
                "/usr/bin/bluetoothctl",
                "AA:BB:CC:DD:EE:FF",
                1.0,
                subprocess_module=_SubprocessModule,
                trust_hex_blob_re=importlib.import_module(
                    "meshtastic.interfaces.ble.management_service"
                ).TRUST_HEX_BLOB_RE,
                trust_token_re=importlib.import_module(
                    "meshtastic.interfaces.ble.management_service"
                ).TRUST_TOKEN_RE,
                trust_command_output_max_chars=3,
            )

        monkeypatch.setattr(
            BLEManagementCommandHandler,
            "_start_management_phase",
            lambda _self, _address: start_context,
        )
        monkeypatch.setattr(
            BLEManagementCommandHandler,
            "_resolve_management_target",
            lambda _self, _address, _ctx: (None, None),
        )

        with pytest.raises(bluetooth_iface.BLEError, match="require"):
            BLEManagementCommandsService.trust(
                bluetooth_iface,
                None,
                timeout=1.0,
                sys_module=SimpleNamespace(platform="linux"),
                shutil_module=SimpleNamespace(
                    which=lambda _name: "/usr/bin/bluetoothctl"
                ),
                subprocess_module=_SubprocessModule,
                trust_hex_blob_re=importlib.import_module(
                    "meshtastic.interfaces.ble.management_service"
                ).TRUST_HEX_BLOB_RE,
                trust_token_re=importlib.import_module(
                    "meshtastic.interfaces.ble.management_service"
                ).TRUST_TOKEN_RE,
                trust_command_output_max_chars=3,
            )
    finally:
        iface.close()
        bluetooth_iface.close()


def test_error_handler_safe_execute_reraises_unexpected_when_requested() -> None:
    """BLEErrorHandler._safe_execute should re-raise unexpected exceptions with reraise=True."""
    with pytest.raises(RuntimeError, match="boom"):
        BLEErrorHandler._safe_execute(
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            reraise=True,
            log_error=False,
        )


def test_utils_remaining_optional_kwarg_and_safe_execute_branches() -> None:
    """Utility helpers should cover remaining raise/None/error branches."""

    def _bad_factory(*_args: object, **kwargs: object) -> str:
        if kwargs:
            raise TypeError("bad factory failure")
        return "ok"

    with pytest.raises(TypeError, match="bad factory failure"):
        ble_utils._call_factory_with_optional_kwarg(
            _bad_factory,
            optional_kwarg="flag",
            optional_value=True,
        )

    iface_no_hooks = SimpleNamespace(
        error_handler=SimpleNamespace(safe_execute=None, _safe_execute=None)
    )
    assert ble_utils._resolve_safe_execute(iface_no_hooks) is None

    iface_hook_exception = SimpleNamespace(
        error_handler=SimpleNamespace(
            safe_execute=lambda _func, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("adapter failure")
            )
        )
    )
    assert (
        ble_utils._safe_execute_through_adapter(
            iface_hook_exception,
            lambda: "unused",
            default_return="fallback",
            error_msg="adapter failed",
            reraise=False,
        )
        == "fallback"
    )


def test_utils_resolve_safe_execute_legacy_hook_branch() -> None:
    """Legacy underscore safe_execute hook should be returned when public hook is absent."""
    iface = SimpleNamespace(
        error_handler=SimpleNamespace(
            safe_execute=None,
            _safe_execute=lambda func, **_kwargs: func(),
        )
    )
    resolved = ble_utils._resolve_safe_execute(iface)
    assert callable(resolved)
    assert resolved(lambda: "ok") == "ok"


def test_compatibility_enqueue_put_nowait_full_without_queuework() -> None:
    """enqueue_publish_callback should return False when only put_nowait is available and full."""
    publishing_thread = SimpleNamespace(
        queueWork=None,
        queue=SimpleNamespace(
            put_nowait=lambda _callback: (_ for _ in ()).throw(Full())
        ),
        thread=None,
    )
    assert (
        BLECompatibilityEventService._enqueue_publish_callback(
            publishing_thread,
            lambda: None,
            prefer_non_blocking=False,
        )
        is False
    )


def test_compatibility_resolve_safe_execute_wrapper() -> None:
    """Compatibility service should delegate safe_execute hook resolution to shared utils."""
    iface = SimpleNamespace(
        error_handler=SimpleNamespace(safe_execute=lambda func, **_kwargs: func())
    )
    resolved = BLECompatibilityEventService._resolve_safe_execute(cast(Any, iface))
    assert callable(resolved)
    assert resolved(lambda: "ok") == "ok"


def test_compatibility_publisher_swallows_provider_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bound compatibility publisher should treat provider failures as best effort."""
    from meshtastic import mesh_interface as mesh_iface_module

    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    try:
        publisher = BLECompatibilityEventPublisher(
            iface,
            publishing_thread_provider=lambda: (_ for _ in ()).throw(
                RuntimeError("provider boom")
            ),
        )
        wait_threads: list[object | None] = []
        drain_threads: list[object | None] = []
        publish_threads: list[object | None] = []
        publish_legacy_threads: list[object | None] = []
        monkeypatch.setattr(
            BLECompatibilityEventService,
            "wait_for_disconnect_notifications",
            staticmethod(
                lambda _iface, _timeout=None, publishing_thread=None: wait_threads.append(
                    publishing_thread
                )
            ),
        )
        monkeypatch.setattr(
            BLECompatibilityEventService,
            "drain_publish_queue",
            staticmethod(
                lambda _iface, _flush_event, publishing_thread=None: drain_threads.append(
                    publishing_thread
                )
            ),
        )
        monkeypatch.setattr(
            BLECompatibilityEventService,
            "publish_connection_status",
            staticmethod(
                lambda _iface, *, connected, publishing_thread=None: publish_threads.append(  # noqa: ARG005
                    publishing_thread
                )
            ),
        )
        monkeypatch.setattr(
            BLECompatibilityEventService,
            "publish_connection_status_legacy",
            staticmethod(
                lambda _iface, _connected, publishing_thread=None: publish_legacy_threads.append(  # noqa: ARG005
                    publishing_thread
                )
            ),
        )

        publisher.wait_for_disconnect_notifications(timeout=0.1)
        publisher.drain_publish_queue(Event())
        publisher.publish_connection_status(connected=True)
        publisher.publish_connection_status_legacy(True)

        expected_thread = vars(mesh_iface_module)["publishingThread"]
        assert wait_threads == [expected_thread]
        assert drain_threads == [expected_thread]
        assert publish_threads == [expected_thread, expected_thread]
        assert publish_legacy_threads == []
    finally:
        iface.close()


def test_compatibility_publisher_reuses_last_good_thread_on_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bound publisher should reuse last successful thread when provider later raises."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    try:
        publishing_thread = object()
        provider_calls = 0

        def _provider() -> object:
            nonlocal provider_calls
            provider_calls += 1
            if provider_calls == 1:
                return publishing_thread
            raise RuntimeError("provider boom")

        publisher = BLECompatibilityEventPublisher(
            iface,
            publishing_thread_provider=_provider,
        )
        wait_threads: list[object | None] = []
        monkeypatch.setattr(
            BLECompatibilityEventService,
            "wait_for_disconnect_notifications",
            staticmethod(
                lambda _iface, _timeout=None, publishing_thread=None: wait_threads.append(
                    publishing_thread
                )
            ),
        )

        assert publisher._resolve_publishing_thread() is publishing_thread
        publisher.wait_for_disconnect_notifications(timeout=0.1)
        assert wait_threads == [publishing_thread]
        assert provider_calls == 2
    finally:
        iface.close()


def test_compatibility_publisher_reuses_last_good_thread_on_provider_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bound publisher should reuse last successful thread when provider later returns None."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    try:
        publishing_thread = object()
        provider_calls = 0

        def _provider() -> object | None:
            nonlocal provider_calls
            provider_calls += 1
            if provider_calls == 1:
                return publishing_thread
            return None

        publisher = BLECompatibilityEventPublisher(
            iface,
            publishing_thread_provider=_provider,
        )
        wait_threads: list[object | None] = []
        monkeypatch.setattr(
            BLECompatibilityEventService,
            "wait_for_disconnect_notifications",
            staticmethod(
                lambda _iface, _timeout=None, publishing_thread=None: wait_threads.append(
                    publishing_thread
                )
            ),
        )

        assert publisher._resolve_publishing_thread() is publishing_thread
        publisher.wait_for_disconnect_notifications(timeout=0.1)
        assert wait_threads == [publishing_thread]
        assert provider_calls == 2
    finally:
        iface.close()


def test_compatibility_enqueue_returns_false_when_no_enqueue_path() -> None:
    """enqueue_publish_callback should return False when no queue path exists."""
    publishing_thread = SimpleNamespace(
        queueWork=None, queue=SimpleNamespace(), thread=None
    )
    assert (
        BLECompatibilityEventService._enqueue_publish_callback(
            publishing_thread,
            lambda: None,
            prefer_non_blocking=False,
        )
        is False
    )


def test_compatibility_service_remaining_enqueue_wait_and_drain_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compatibility service should cover remaining enqueue/wait/drain branches."""
    original_drain_publish_queue = BLECompatibilityEventService.drain_publish_queue
    original_safe_execute = BLECompatibilityEventService._safe_execute

    def callback() -> None:
        return None

    enqueue_thread = SimpleNamespace(
        queueWork=None,
        queue=SimpleNamespace(put_nowait=lambda _cb: (_ for _ in ()).throw(Full())),
        thread=SimpleNamespace(is_alive=lambda: (_ for _ in ()).throw(RuntimeError())),
    )
    assert (
        BLECompatibilityEventService._enqueue_publish_callback(
            enqueue_thread,
            callback,
            prefer_non_blocking=False,
        )
        is False
    )

    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    try:

        class _NeverSetEvent:
            def set(self) -> None:
                return None

            @staticmethod
            def wait(timeout: float | None = None) -> bool:
                _ = timeout
                return False

        monkeypatch.setattr(compatibility_mod, "Event", _NeverSetEvent, raising=True)
        monkeypatch.setattr(
            BLECompatibilityEventService,
            "_safe_execute",
            staticmethod(lambda *_args, **_kwargs: True),
        )
        drained: list[str] = []
        monkeypatch.setattr(
            BLECompatibilityEventService,
            "drain_publish_queue",
            staticmethod(lambda *_args, **_kwargs: drained.append("drained")),
        )
        BLECompatibilityEventService.wait_for_disconnect_notifications(
            iface,
            timeout=0.01,
            publishing_thread=SimpleNamespace(
                thread=SimpleNamespace(is_alive=lambda: True),
                queueWork=lambda _cb: None,
                queue=SimpleNamespace(put_nowait=lambda _cb: None),
            ),
        )
        assert drained == []
        monkeypatch.setattr(
            BLECompatibilityEventService,
            "_safe_execute",
            original_safe_execute,
        )
        monkeypatch.setattr(
            BLECompatibilityEventService,
            "drain_publish_queue",
            original_drain_publish_queue,
        )

        flush_event = Event()
        called_thread_drain: list[str] = []
        BLECompatibilityEventService.drain_publish_queue(
            iface,
            flush_event,
            publishing_thread=SimpleNamespace(
                thread=SimpleNamespace(
                    _drain_publish_queue=lambda _event: called_thread_drain.append(
                        "thread"
                    )
                ),
                queue=SimpleNamespace(
                    get_nowait=lambda: (_ for _ in ()).throw(Empty())
                ),
            ),
        )
        assert called_thread_drain == ["thread"]

        BLECompatibilityEventService.drain_publish_queue(
            iface,
            flush_event,
            publishing_thread=SimpleNamespace(
                thread=None,
                queue=SimpleNamespace(
                    get_nowait=lambda: (_ for _ in ()).throw(
                        RuntimeError("queue read failed")
                    )
                ),
            ),
        )
    finally:
        iface.close()


def test_reconnect_scheduler_start_exception_and_management_trust_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconnect start failures and trust-output truncation should hit remaining branches."""
    scheduler = ReconnectScheduler(
        state_manager=BLEStateManager(),
        state_lock=RLock(),
        thread_coordinator=cast(
            Any,
            SimpleNamespace(
                create_thread=lambda **_kwargs: SimpleNamespace(
                    ident=1, is_alive=lambda: True
                ),
                start_thread=lambda _thread: (_ for _ in ()).throw(
                    RuntimeError("start failed")
                ),
            ),
        ),
        interface=cast(
            Any,
            SimpleNamespace(
                _is_connection_closing=False, _can_initiate_connection=True
            ),
        ),
    )
    should_reconnect = True
    with pytest.raises(RuntimeError, match="start failed"):
        scheduler._schedule_reconnect(should_reconnect, Event())

    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    try:

        class _FailingSubprocess:
            TimeoutExpired = RuntimeError

            @staticmethod
            def run(*_args: object, **_kwargs: object) -> object:
                return SimpleNamespace(
                    returncode=1,
                    stderr="A very long diagnostic output that should be truncated for tests",
                    stdout="",
                )

        with pytest.raises(iface.BLEError, match="trust"):
            BLEManagementCommandsService._run_bluetoothctl_trust_command(
                iface,
                "/usr/bin/bluetoothctl",
                "AA:BB:CC:DD:EE:FF",
                1.0,
                subprocess_module=_FailingSubprocess,
                trust_hex_blob_re=importlib.import_module(
                    "meshtastic.interfaces.ble.management_service"
                ).TRUST_HEX_BLOB_RE,
                trust_token_re=importlib.import_module(
                    "meshtastic.interfaces.ble.management_service"
                ).TRUST_TOKEN_RE,
                trust_command_output_max_chars=2,
            )
    finally:
        iface.close()
