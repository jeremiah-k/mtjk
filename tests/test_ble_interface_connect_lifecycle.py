"""BLE connection lifecycle tests."""

# pylint: disable=redefined-outer-name

import logging
import re
import threading
from types import SimpleNamespace
from typing import Any, cast

import pytest
from bleak.exc import BleakError

# Import meshtastic modules for use in tests
from meshtastic.interfaces.ble import (
    BLEClient,
    BLEInterface,
)
from meshtastic.interfaces.ble.constants import (
    ERROR_MANAGEMENT_CONNECTING,
)
from meshtastic.interfaces.ble.state import ConnectionState
from tests._ble_interface_core_support import (
    _MAX_SPURIOUS_CONNECT_WAIT_CALLS_BEFORE_FAIL,
    _build_minimal_connect_test_interface,
    _make_establish_stub,
)

# Import common fixtures
from tests.test_ble_interface_fixtures import DummyClient, _build_interface

pytestmark = pytest.mark.unit


def test_ble_interface_connect_uses_pair_override_for_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should forward pair and timeout overrides to connection orchestration."""
    iface = _build_minimal_connect_test_interface()

    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(iface, "_get_existing_client_if_valid", lambda _req: None)
    monkeypatch.setattr(
        iface, "_raise_if_duplicate_connect", lambda _key, **_kwargs: None
    )
    monkeypatch.setattr(iface, "_finalize_connection_gates", lambda *_args: None)
    connected_callbacks: list[bool] = []
    monkeypatch.setattr(iface, "_connected", lambda: connected_callbacks.append(True))

    captured_pair_flags: list[bool] = []
    captured_timeouts: list[float | None] = []

    def _record_establish(
        _address: str | None,
        _normalized_request: str | None,
        _address_key: str | None,
        pair_on_connect: bool,
        connect_timeout: float | None,
    ) -> None:
        captured_pair_flags.append(pair_on_connect)
        captured_timeouts.append(connect_timeout)

    _establish_stub = _make_establish_stub(
        iface,
        DummyClient,
        on_call=_record_establish,
    )

    monkeypatch.setattr(iface, "_establish_and_update_client", _establish_stub)

    iface.connect(pair=True, connect_timeout=4.5)
    iface.connect(pair=False)
    iface.pair_on_connect = True
    iface.connect()

    assert captured_pair_flags == [True, False, True]
    assert captured_timeouts == [4.5, None, None]
    assert connected_callbacks == [True, True, True]


def test_connect_wraps_invalid_connect_timeout_as_ble_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should wrap invalid timeout overrides as BLEError."""
    iface = _build_minimal_connect_test_interface()
    cast(Any, iface)._connection_orchestrator = SimpleNamespace(
        _establish_connection=lambda *_args, **_kwargs: pytest.fail(
            "_establish_connection should not be called for invalid connect_timeout"
        )
    )

    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(iface, "_get_existing_client_if_valid", lambda _req: None)
    monkeypatch.setattr(
        iface, "_raise_if_duplicate_connect", lambda _key, **_kwargs: None
    )

    with pytest.raises(
        BLEInterface.BLEError,
        match=re.escape(
            "invalid connect_timeout: connect_timeout must be a finite positive number of seconds."
        ),
    ):
        iface.connect("AA:BB:CC:DD:EE:10", connect_timeout=cast(Any, 0.0))


def test_validate_connect_timeout_override_rejects_non_numeric_values() -> None:
    """_validate_connect_timeout_override should wrap non-numeric overrides as BLEError."""
    iface = _build_minimal_connect_test_interface()
    with pytest.raises(BLEInterface.BLEError, match="invalid connect_timeout"):
        iface._validate_connect_timeout_override(
            cast(object, "invalid-timeout"),
            pair_on_connect=False,
        )


def test_finish_management_operation_clamps_underflow(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """_finish_management_operation() should clamp negative accounting to zero."""
    iface = _build_minimal_connect_test_interface()
    with iface._management_lock:
        iface._management_inflight = -1
    notify_calls: list[bool] = []
    monkeypatch.setattr(
        iface._management_idle_condition,
        "notify_all",
        lambda: notify_calls.append(True),
        raising=True,
    )

    with caplog.at_level(logging.WARNING):
        iface._finish_management_operation()

    with iface._management_lock:
        assert iface._management_inflight == 0
    assert notify_calls == [True]
    assert any("underflow" in record.message.lower() for record in caplog.records)


def test_finish_management_operation_notifies_when_count_reaches_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_finish_management_operation() should notify waiters on zero transition."""
    iface = _build_minimal_connect_test_interface()
    with iface._management_lock:
        iface._management_inflight = 1
    notify_calls: list[bool] = []
    monkeypatch.setattr(
        iface._management_idle_condition,
        "notify_all",
        lambda: notify_calls.append(True),
        raising=True,
    )

    iface._finish_management_operation()

    with iface._management_lock:
        assert iface._management_inflight == 0
    assert notify_calls == [True]


def test_finish_management_operation_does_not_notify_above_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_finish_management_operation() should not notify while inflight remains positive."""
    iface = _build_minimal_connect_test_interface()
    with iface._management_lock:
        iface._management_inflight = 2
    notify_calls: list[bool] = []
    monkeypatch.setattr(
        iface._management_idle_condition,
        "notify_all",
        lambda: notify_calls.append(True),
        raising=True,
    )

    iface._finish_management_operation()

    with iface._management_lock:
        assert iface._management_inflight == 1
    assert notify_calls == []


@pytest.mark.unit
def test_connect_rejects_non_bool_pair_override() -> None:
    """connect() should fail fast when `pair` is not explicitly bool/None."""
    iface = _build_minimal_connect_test_interface()
    with pytest.raises(BLEInterface.BLEError, match="pair must be a bool"):
        iface.connect("AA:BB:CC:DD:EE:10", pair=cast(Any, "false"))


@pytest.mark.unit
def test_connect_waits_for_inflight_management_before_establishing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should wait until in-flight management operations finish."""
    iface = _build_minimal_connect_test_interface()
    iface._management_lock = threading.RLock()
    iface._management_idle_condition = threading.Condition(iface._management_lock)
    iface._management_inflight = 1

    wait_calls: list[bool] = []
    establish_calls: list[bool] = []

    def _wait_for_management(timeout: float | None = None) -> bool:
        _ = timeout
        wait_calls.append(True)
        iface._management_inflight = 0
        return True

    monkeypatch.setattr(
        iface._management_idle_condition,
        "wait",
        _wait_for_management,
        raising=True,
    )
    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(iface, "_get_existing_client_if_valid", lambda _req: None)
    monkeypatch.setattr(
        iface, "_raise_if_duplicate_connect", lambda _key, **_kwargs: None
    )
    monkeypatch.setattr(iface, "_finalize_connection_gates", lambda *_args: None)
    monkeypatch.setattr(
        iface,
        "_verify_and_publish_connected",
        lambda *_args, **_kwargs: None,
    )

    def _record_establish(
        _address: str | None,
        _normalized_request: str | None,
        _address_key: str | None,
        _pair_on_connect: bool,
        _connect_timeout: float | None,
    ) -> None:
        establish_calls.append(True)

    _establish_stub = _make_establish_stub(
        iface,
        DummyClient,
        on_call=_record_establish,
    )

    monkeypatch.setattr(iface, "_establish_and_update_client", _establish_stub)

    iface.connect("AA:BB:CC:DD:EE:10")

    assert wait_calls == [True]
    assert establish_calls == [True]


def test_connect_returns_preexisting_client_before_resolving_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should return immediately when a preexisting client already satisfies the request."""
    iface = _build_minimal_connect_test_interface()
    existing_client = cast(BLEClient, DummyClient())
    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(
        iface, "_get_existing_client_if_valid", lambda _request: existing_client
    )
    monkeypatch.setattr(
        iface,
        "_resolve_target_address_for_connect",
        lambda _identifier: pytest.fail("resolution should not run"),
    )

    assert iface.connect("AA:BB:CC:DD:EE:10") is existing_client


def test_connect_times_out_waiting_for_management_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should abort when inflight management never drains."""
    iface = _build_minimal_connect_test_interface()
    iface._management_lock = threading.RLock()
    iface._management_idle_condition = threading.Condition(iface._management_lock)
    iface._management_inflight = 1

    import meshtastic.interfaces.ble.interface as interface_mod

    monotonic_values = iter([0.0, 999.0])
    last_monotonic = 999.0

    def _monotonic() -> float:
        nonlocal last_monotonic
        last_monotonic = next(monotonic_values, last_monotonic + 1.0)
        return last_monotonic

    monkeypatch.setattr(
        interface_mod,
        "time",
        SimpleNamespace(monotonic=_monotonic),
        raising=True,
    )
    monkeypatch.setattr(
        iface._management_idle_condition,
        "wait",
        lambda timeout=None: False,
        raising=True,
    )
    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(iface, "_get_existing_client_if_valid", lambda _request: None)

    with pytest.raises(BLEInterface.BLEError, match=ERROR_MANAGEMENT_CONNECTING):
        iface.connect("AA:BB:CC:DD:EE:10")


def test_connect_times_out_on_spurious_management_wakeups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should enforce timeout even if management wait wakes spuriously."""
    iface = _build_minimal_connect_test_interface()
    iface._management_lock = threading.RLock()
    iface._management_idle_condition = threading.Condition(iface._management_lock)
    iface._management_inflight = 1
    wait_calls: list[float | None] = []
    fake_time = 0.0

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface._MANAGEMENT_CONNECT_WAIT_TIMEOUT_SECONDS",
        0.03,
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface._MANAGEMENT_CONNECT_WAIT_POLL_SECONDS",
        0.005,
    )

    def _monotonic() -> float:
        """Advance and return the deterministic monotonic test clock.

        Returns
        -------
        float
            Current monotonic time, advanced by 0.01 seconds per call.
        """
        nonlocal fake_time
        fake_time += 0.01
        return fake_time

    import meshtastic.interfaces.ble.interface as interface_mod

    monkeypatch.setattr(
        interface_mod,
        "time",
        SimpleNamespace(monotonic=_monotonic),
        raising=True,
    )

    def _spurious_wait(timeout: float | None = None) -> bool:
        """Record a bounded spurious condition wakeup.

        Parameters
        ----------
        timeout : float | None
            Timeout passed to the condition wait.

        Returns
        -------
        bool
            ``True`` to simulate a spurious wakeup.

        Raises
        ------
        AssertionError
            If connect waits beyond the configured spurious-wakeup budget.
        """
        wait_calls.append(timeout)
        if len(wait_calls) > _MAX_SPURIOUS_CONNECT_WAIT_CALLS_BEFORE_FAIL:
            raise AssertionError("connect() kept waiting past the timeout budget")
        return True

    monkeypatch.setattr(
        iface._management_idle_condition,
        "wait",
        _spurious_wait,
        raising=True,
    )
    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(iface, "_get_existing_client_if_valid", lambda _request: None)

    with pytest.raises(BLEInterface.BLEError, match=ERROR_MANAGEMENT_CONNECTING):
        iface.connect("AA:BB:CC:DD:EE:10")

    assert wait_calls


def test_connect_management_wait_timeout_resets_between_wait_cycles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should restart timeout accounting after management drains."""
    iface = _build_minimal_connect_test_interface()
    iface._management_lock = threading.RLock()
    iface._management_idle_condition = threading.Condition(iface._management_lock)
    iface._management_inflight = 1
    wait_calls: list[float | None] = []
    duplicate_checks = 0
    monotonic_values = iter([0.0, 0.1, 100.0, 100.1])
    last_time = 100.1

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface._MANAGEMENT_CONNECT_WAIT_TIMEOUT_SECONDS",
        1.0,
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface._MANAGEMENT_CONNECT_WAIT_POLL_SECONDS",
        0.1,
    )

    def _monotonic() -> float:
        """Return the next deterministic timestamp, then advance after exhaustion.

        Returns
        -------
        float
            Current monotonic test timestamp.
        """
        nonlocal last_time
        try:
            last_time = next(monotonic_values)
        except StopIteration:
            last_time += 0.1
        return last_time

    import meshtastic.interfaces.ble.interface as interface_mod

    monkeypatch.setattr(
        interface_mod,
        "time",
        SimpleNamespace(monotonic=_monotonic),
        raising=True,
    )

    def _wait_for_management(timeout: float | None = None) -> bool:
        """Drain simulated management work and record the requested timeout.

        Parameters
        ----------
        timeout : float | None
            Maximum seconds to wait, or ``None`` for an indefinite wait.

        Returns
        -------
        bool
            ``True`` to indicate the wait condition was signaled.

        Notes
        -----
        The fake records ``timeout`` and clears ``iface._management_inflight``.
        """
        wait_calls.append(timeout)
        iface._management_inflight = 0
        return True

    def _raise_if_duplicate(_key: str | None, **_kwargs: object) -> None:
        """
        Increment the duplicate-check counter and, on the second invocation, mark the interface as having one inflight management operation.

        Parameters
        ----------
            _key (str | None): Ignored; present to match the duplicate-check callback signature.
        """
        nonlocal duplicate_checks
        duplicate_checks += 1
        if duplicate_checks == 2:
            iface._management_inflight = 1

    monkeypatch.setattr(
        iface._management_idle_condition,
        "wait",
        _wait_for_management,
        raising=True,
    )
    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(iface, "_get_existing_client_if_valid", lambda _request: None)
    monkeypatch.setattr(
        iface,
        "_resolve_target_address_for_connect",
        lambda identifier: cast(str, identifier),
    )
    monkeypatch.setattr(iface, "_raise_if_duplicate_connect", _raise_if_duplicate)
    monkeypatch.setattr(iface, "_finalize_connection_gates", lambda *_args: None)
    monkeypatch.setattr(
        iface,
        "_verify_and_publish_connected",
        lambda *_args, **_kwargs: None,
    )

    _establish_stub = _make_establish_stub(iface, DummyClient)

    monkeypatch.setattr(iface, "_establish_and_update_client", _establish_stub)

    assert isinstance(iface.connect("AA:BB:CC:DD:EE:10"), DummyClient)
    assert len(wait_calls) == 2


def test_connect_retries_when_management_becomes_inflight_inside_connect_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should continue outer loop when management starts after address-gate wait."""
    iface = _build_minimal_connect_test_interface()
    iface._management_lock = threading.RLock()
    iface._management_idle_condition = threading.Condition(iface._management_lock)
    iface._management_inflight = 0
    established: list[bool] = []
    duplicate_checks = 0

    def _wait_for_management(timeout: float | None = None) -> bool:
        _ = timeout
        iface._management_inflight = 0
        return True

    def _raise_if_duplicate(_key: str | None, **_kwargs: object) -> None:
        nonlocal duplicate_checks
        duplicate_checks += 1
        if duplicate_checks == 2:
            iface._management_inflight = 1

    monkeypatch.setattr(
        iface._management_idle_condition,
        "wait",
        _wait_for_management,
        raising=True,
    )
    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(iface, "_get_existing_client_if_valid", lambda _request: None)
    monkeypatch.setattr(
        iface,
        "_resolve_target_address_for_connect",
        lambda identifier: cast(str, identifier),
    )
    monkeypatch.setattr(iface, "_raise_if_duplicate_connect", _raise_if_duplicate)
    monkeypatch.setattr(iface, "_finalize_connection_gates", lambda *_args: None)
    monkeypatch.setattr(
        iface,
        "_verify_and_publish_connected",
        lambda *_args, **_kwargs: None,
    )

    def _record_establish(
        _address: str | None,
        _normalized_request: str | None,
        _address_key: str | None,
        _pair_on_connect: bool,
        _connect_timeout: float | None,
    ) -> None:
        established.append(True)

    _establish_stub = _make_establish_stub(
        iface,
        DummyClient,
        on_call=_record_establish,
    )

    monkeypatch.setattr(iface, "_establish_and_update_client", _establish_stub)

    assert isinstance(iface.connect("AA:BB:CC:DD:EE:10"), DummyClient)
    assert established == [True]
    assert duplicate_checks >= 4


def test_connect_returns_existing_client_after_lock_recheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should return an existing client found during in-lock recheck."""
    iface = _build_minimal_connect_test_interface()
    existing_client = cast(BLEClient, DummyClient())
    lookup_count = 0

    def _lookup_existing(_request: str | None) -> BLEClient | None:
        nonlocal lookup_count
        lookup_count += 1
        if lookup_count == 1:
            return None
        return existing_client

    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(iface, "_get_existing_client_if_valid", _lookup_existing)
    monkeypatch.setattr(
        iface,
        "_resolve_target_address_for_connect",
        lambda identifier: cast(str, identifier),
    )
    monkeypatch.setattr(
        iface, "_raise_if_duplicate_connect", lambda _key, **_kwargs: None
    )

    assert iface.connect("AA:BB:CC:DD:EE:10") is existing_client


def test_connect_raises_when_establish_returns_no_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should fail fast when establishment returns no client object."""
    iface = _build_minimal_connect_test_interface()
    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(iface, "_get_existing_client_if_valid", lambda _request: None)
    monkeypatch.setattr(
        iface,
        "_resolve_target_address_for_connect",
        lambda identifier: cast(str, identifier),
    )
    monkeypatch.setattr(
        iface, "_raise_if_duplicate_connect", lambda _key, **_kwargs: None
    )
    monkeypatch.setattr(
        iface,
        "_establish_and_update_client",
        lambda *_args, **_kwargs: (cast(DummyClient, None), None, None),
    )

    with pytest.raises(BLEInterface.BLEError, match="no BLE client established"):
        iface.connect("AA:BB:CC:DD:EE:10")


def test_connect_does_not_relabel_unrelated_establish_connection_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should preserve unrelated ValueError failures from orchestration."""
    iface = _build_minimal_connect_test_interface()
    cast(Any, iface)._connection_orchestrator = SimpleNamespace(
        _establish_connection=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("boom")
        )
    )

    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(iface, "_get_existing_client_if_valid", lambda _req: None)
    monkeypatch.setattr(
        iface, "_raise_if_duplicate_connect", lambda _key, **_kwargs: None
    )

    with pytest.raises(ValueError, match="boom"):
        iface.connect("AA:BB:CC:DD:EE:10", connect_timeout=4.5)


def test_ble_interface_establish_and_update_client_discards_late_connection_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Late shutdown results should be closed and restore notification state."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    dispatcher = iface._notification_dispatcher
    manager = dispatcher._notification_manager
    connected_client = DummyClient()
    connected_client.address = "AA:BB:CC:DD:EE:FF"
    connected_client.bleak_client = SimpleNamespace(address="AA:BB:CC:DD:EE:FF")
    cleanup_calls: list[object] = []

    def prior_callback(*_args: object) -> None:
        return None

    def late_callback(*_args: object) -> None:
        return None

    prior_token = manager._subscribe("prior-characteristic", prior_callback)
    prior_counter = manager._subscription_counter
    dispatcher._registered_notification_session_epoch = 17
    dispatcher._started_notify_characteristics = {"prior-characteristic"}

    def _establish_late_client(*_args: object, **_kwargs: object) -> BLEClient:
        manager._subscribe("late-characteristic", late_callback)
        dispatcher._registered_notification_session_epoch = 99
        dispatcher._started_notify_characteristics.add("late-characteristic")
        return cast(BLEClient, connected_client)

    try:
        monkeypatch.setattr(
            iface._connection_orchestrator,
            "_establish_connection",
            _establish_late_client,
        )
        monkeypatch.setattr(
            iface._client_manager,
            "_safe_close_client",
            lambda client: cleanup_calls.append(client),
        )

        with iface._connect_lock:
            with iface._state_lock:
                iface._closed = True
            with pytest.raises(BLEInterface.BLEError, match="closing"):
                iface._establish_and_update_client(
                    "AA:BB:CC:DD:EE:FF",
                    "aabbccddeeff",
                    "aabbccddeeff",
                    pair_on_connect=False,
                )

        assert cleanup_calls == [connected_client]
        assert manager._active_subscriptions == {
            prior_token: ("prior-characteristic", prior_callback)
        }
        assert manager._characteristic_to_callback == {
            "prior-characteristic": prior_callback
        }
        assert manager._subscription_counter == prior_counter
        assert dispatcher._registered_notification_session_epoch == 17
        assert dispatcher._started_notify_characteristics == {"prior-characteristic"}
        with iface._state_lock:
            assert cast(object, iface.client) is not connected_client
    finally:
        iface.close()


def test_establish_and_update_client_preserves_newer_client_on_handoff_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed handoff must not overwrite a newer client adopted by a collaborator."""
    previous_client = DummyClient()
    previous_client.address = "11:22:33:44:55:66"
    previous_client.bleak_client = SimpleNamespace(address=previous_client.address)
    connected_client = DummyClient()
    connected_client.address = "AA:BB:CC:DD:EE:FF"
    connected_client.bleak_client = SimpleNamespace(address=connected_client.address)
    newer_client = DummyClient()
    newer_client.address = "22:33:44:55:66:77"
    newer_client.bleak_client = SimpleNamespace(address=newer_client.address)
    iface = _build_interface(monkeypatch, previous_client, start_receive_thread=False)
    cleanup_calls: list[object] = []

    def _fail_handoff(_new_client: BLEClient, _old_client: BLEClient | None) -> None:
        with iface._state_lock:
            iface.client = cast(BLEClient, newer_client)
            iface.address = newer_client.address
            iface._last_connection_request = "newer-session"
            iface._client_publish_pending = False
            iface._client_replacement_pending = False
            iface._disconnect_notified = False
            iface._connection_session_epoch = 77
        raise RuntimeError("handoff failed")

    try:
        monkeypatch.setattr(
            iface,
            "_establish_connection",
            lambda *_args, **_kwargs: cast(BLEClient, connected_client),
        )
        monkeypatch.setattr(
            iface, "_client_manager_update_client_reference", _fail_handoff
        )
        monkeypatch.setattr(
            iface,
            "_client_manager_safe_close_client",
            lambda client: cleanup_calls.append(client),
        )

        with iface._connect_lock, pytest.raises(RuntimeError, match="handoff failed"):
            iface._establish_and_update_client(
                connected_client.address,
                "aabbccddeeff",
                "aabbccddeeff",
            )

        with iface._state_lock:
            assert iface.client is cast(Any, newer_client)
            assert iface.address == newer_client.address
            assert iface._last_connection_request == "newer-session"
            assert iface._client_publish_pending is False
            assert iface._client_replacement_pending is False
            assert iface._disconnect_notified is False
            assert iface._connection_session_epoch == 77
        assert cleanup_calls == [connected_client]
    finally:
        with iface._state_lock:
            iface.client = cast(BLEClient, newer_client)
        iface.close()


def test_establish_and_update_client_sets_last_request_from_device_and_updates_previous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Established clients should refresh last request from device key and close replaced clients."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    previous_client = DummyClient()
    previous_client.address = "11:22:33:44:55:66"
    previous_client.bleak_client = SimpleNamespace(address=previous_client.address)
    connected_client = DummyClient()
    connected_client.address = "AA:BB:CC:DD:EE:FF"
    connected_client.bleak_client = SimpleNamespace(address=connected_client.address)
    updated_refs: list[tuple[BLEClient, BLEClient | None]] = []

    try:
        monkeypatch.setattr(
            iface._connection_orchestrator,
            "_establish_connection",
            lambda *_args, **_kwargs: cast(BLEClient, connected_client),
        )
        monkeypatch.setattr(
            iface._client_manager,
            "_update_client_reference",
            lambda new_client, old_client: updated_refs.append(
                (new_client, old_client)
            ),
        )

        with iface._state_lock:
            cast(Any, iface).client = previous_client
            iface._state_manager._reset_to_disconnected()
            assert (
                iface._state_manager._transition_to(ConnectionState.CONNECTING) is True
            )
            assert (
                iface._state_manager._transition_to(ConnectionState.CONNECTED) is True
            )

        with iface._connect_lock:
            result_client, _, _ = iface._establish_and_update_client(
                "AA:BB:CC:DD:EE:FF",
                None,
                "aabbccddeeff",
                pair_on_connect=False,
            )

        assert result_client is cast(Any, connected_client)
        assert updated_refs == [
            (cast(BLEClient, connected_client), cast(BLEClient, previous_client))
        ]
        assert iface._last_connection_request == iface._sanitize_address(
            connected_client.address
        )
    finally:
        iface.close()


def test_handle_disconnect_ignores_stale_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale disconnect callbacks must not clear the current active client."""
    stale_client = DummyClient()
    iface = _build_interface(monkeypatch, stale_client)

    active_client = DummyClient()
    active_client.address = "active"
    active_client.bleak_client = SimpleNamespace(address="active")
    reconnect_calls: list[bool] = []
    disconnected_calls: list[bool] = []

    monkeypatch.setattr(
        iface,
        "_schedule_auto_reconnect",
        lambda: reconnect_calls.append(True),
        raising=True,
    )
    monkeypatch.setattr(
        iface, "_disconnected", lambda: disconnected_calls.append(True), raising=True
    )

    with iface._state_lock:
        cast(Any, iface).client = active_client
        iface._disconnect_notified = False
        iface._state_manager._reset_to_disconnected()
        assert iface._state_manager._transition_to(ConnectionState.CONNECTING) is True
        assert iface._state_manager._transition_to(ConnectionState.CONNECTED) is True

    # Stale callback by BLEClient instance should be ignored.
    assert iface._handle_disconnect("stale-client", client=stale_client) is True  # type: ignore[arg-type]
    # Stale callback by bleak client identity should also be ignored.
    assert (
        iface._handle_disconnect("stale-bleak", bleak_client=stale_client.bleak_client)  # type: ignore[arg-type]
        is True
    )

    assert cast(object, iface.client) is active_client
    assert iface._disconnect_notified is False
    assert reconnect_calls == []
    assert disconnected_calls == []

    iface.close()


def test_discard_invalidated_connected_client_marks_stale_callbacks_notified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discarded clients should not trigger a second disconnect via their stale callback."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    discarded_client = DummyClient()
    discarded_client.address = "AA:BB:CC:DD:EE:FF"
    discarded_client.bleak_client = SimpleNamespace(address=discarded_client.address)
    disconnected_calls: list[bool] = []
    reconnect_calls: list[bool] = []
    callback_results: list[bool] = []

    monkeypatch.setattr(
        iface, "_disconnected", lambda: disconnected_calls.append(True), raising=True
    )
    monkeypatch.setattr(
        iface,
        "_schedule_auto_reconnect",
        lambda: reconnect_calls.append(True),
        raising=True,
    )

    def _safe_close_client(client: BLEClient) -> None:
        callback_results.append(
            iface._handle_disconnect("discarded-client", client=client)
        )

    monkeypatch.setattr(
        iface._client_manager,
        "_safe_close_client",
        _safe_close_client,
        raising=True,
    )

    with iface._state_lock:
        cast(Any, iface).client = discarded_client
        iface.address = discarded_client.address
        iface._disconnect_notified = False
        iface._state_manager._reset_to_disconnected()
        assert iface._state_manager._transition_to(ConnectionState.CONNECTING) is True
        assert iface._state_manager._transition_to(ConnectionState.CONNECTED) is True

    iface._discard_invalidated_connected_client(cast(BLEClient, discarded_client))

    assert callback_results == [True]
    assert disconnected_calls == []
    assert reconnect_calls == []
    with iface._state_lock:
        assert iface.client is None
        assert iface.address is None
        assert iface._disconnect_notified is True
        assert iface._state_manager._current_state == ConnectionState.DISCONNECTED

    iface.close()


def test_discard_invalidated_connected_client_clears_pending_when_already_detached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pending publish flag should clear even if the provisional client already detached."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    discarded_client = DummyClient()
    discarded_client.address = "AA:BB:CC:DD:EE:44"
    discarded_client.bleak_client = SimpleNamespace(address=discarded_client.address)
    closed_clients: list[BLEClient] = []

    monkeypatch.setattr(
        iface._client_manager,
        "_safe_close_client",
        lambda client: closed_clients.append(client),
        raising=True,
    )

    with iface._state_lock:
        cast(Any, iface).client = None
        iface._client_publish_pending = True
        iface._connected_publish_inflight_client = cast(BLEClient, discarded_client)
        iface._disconnect_notified = False
        iface._state_manager._reset_to_disconnected()
        assert iface._state_manager._transition_to(ConnectionState.CONNECTING) is True

    iface._discard_invalidated_connected_client(cast(BLEClient, discarded_client))

    assert closed_clients == [cast(BLEClient, discarded_client)]
    with iface._state_lock:
        assert iface.client is None
        assert iface._client_publish_pending is False
        assert iface._disconnect_notified is False

    iface.close()


def test_discard_invalidated_connected_client_emits_disconnect_for_retired_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replacement-pending stale sessions should emit one disconnect publication."""
    from meshtastic import mesh_interface as mesh_iface_module

    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    discarded_client = DummyClient()
    discarded_client.address = "AA:BB:CC:DD:EE:45"
    discarded_client.bleak_client = SimpleNamespace(address=discarded_client.address)
    closed_clients: list[BLEClient] = []
    published_topics: list[str] = []

    monkeypatch.setattr(
        iface._client_manager,
        "_safe_close_client",
        lambda client: closed_clients.append(client),
        raising=True,
    )
    monkeypatch.setattr(
        mesh_iface_module,
        "pub",
        SimpleNamespace(
            sendMessage=lambda topic, **_kwargs: published_topics.append(topic)
        ),
        raising=True,
    )
    monkeypatch.setattr(
        mesh_iface_module,
        "publishingThread",
        SimpleNamespace(queueWork=lambda callback: callback()),
        raising=True,
    )

    with iface._state_lock:
        cast(Any, iface).client = None
        iface._client_publish_pending = True
        iface._client_replacement_pending = True
        iface._connected_publish_inflight_client = cast(BLEClient, discarded_client)
        iface._disconnect_notified = False
        iface._state_manager._reset_to_disconnected()
        assert iface._state_manager._transition_to(ConnectionState.CONNECTING) is True
    with iface._heartbeat_lock:
        iface.isConnected.set()

    iface._discard_invalidated_connected_client(cast(BLEClient, discarded_client))
    assert published_topics.count("meshtastic.connection.lost") == 1

    assert closed_clients == [cast(BLEClient, discarded_client)]
    with iface._state_lock:
        assert iface.client is None
        assert iface._client_publish_pending is False
        assert iface._client_replacement_pending is False
        assert iface._disconnect_notified is True

    iface.close()
    assert published_topics.count("meshtastic.connection.lost") == 1


@pytest.mark.parametrize("is_closing", [True, False])
def test_finalize_connection_gates_cleans_up_when_client_loses_ownership_mid_finalize(
    monkeypatch: pytest.MonkeyPatch,
    is_closing: bool,
) -> None:
    """Gate finalization should clean up provisional claims when ownership disappears mid-finalize."""
    from meshtastic.interfaces.ble.lifecycle_service import BLELifecycleService

    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    connected_client = DummyClient()
    cleanup_calls: list[tuple[str | None, str | None]] = []

    # Set up interface state to simulate that we had claimed ownership
    # before ownership was lost mid-finalize
    with iface._state_lock:
        iface.client = cast(BLEClient, connected_client)
        iface._connection_alias_key = "alias-key"

    monkeypatch.setattr(
        BLELifecycleService,
        "_get_connected_client_status",
        lambda _iface, _client: (True, False),
        raising=True,
    )
    monkeypatch.setattr(
        BLELifecycleService,
        "_get_connected_client_status_locked",
        lambda _iface, _client: (False, is_closing),
        raising=True,
    )
    monkeypatch.setattr(iface, "_mark_address_keys_connected", lambda *_keys: None)
    monkeypatch.setattr(
        iface,
        "_mark_address_keys_disconnected",
        lambda *keys: cleanup_calls.append(cast(tuple[str | None, str | None], keys)),
    )

    iface._finalize_connection_gates(
        cast(BLEClient, connected_client), "device-key", "alias-key"
    )

    assert cleanup_calls == [("device-key", "alias-key")]
    assert iface._connection_alias_key is None
    iface.close()


@pytest.mark.parametrize("is_closing", [True, False])
def test_finalize_connection_gates_logs_when_result_is_already_stale(
    monkeypatch: pytest.MonkeyPatch,
    is_closing: bool,
) -> None:
    """Gate finalization should no-op when initial ownership check already reports stale result."""
    from meshtastic.interfaces.ble.lifecycle_service import BLELifecycleService

    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    connected_client = DummyClient()
    mark_connected_calls: list[tuple[str | None, str | None]] = []
    mark_disconnected_calls: list[tuple[str | None, str | None]] = []

    monkeypatch.setattr(
        BLELifecycleService,
        "_get_connected_client_status",
        lambda _iface, _client: (False, is_closing),
        raising=True,
    )
    monkeypatch.setattr(
        iface,
        "_mark_address_keys_connected",
        lambda *keys: mark_connected_calls.append(
            cast(tuple[str | None, str | None], keys)
        ),
        raising=True,
    )
    monkeypatch.setattr(
        iface,
        "_mark_address_keys_disconnected",
        lambda *keys: mark_disconnected_calls.append(
            cast(tuple[str | None, str | None], keys)
        ),
        raising=True,
    )

    iface._finalize_connection_gates(
        cast(BLEClient, connected_client), "device-key", "alias-key"
    )

    assert mark_connected_calls == []
    assert mark_disconnected_calls == []
    iface.close()


def test_is_owned_connected_client_reads_status_tuple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owned-client helper should return the first element of status tuple."""
    from meshtastic.interfaces.ble.lifecycle_service import BLELifecycleService

    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    client = cast(BLEClient, DummyClient())
    monkeypatch.setattr(
        BLELifecycleService,
        "_get_connected_client_status",
        lambda _iface, _client: (True, False),
        raising=True,
    )
    assert iface._is_owned_connected_client(client) is True
    iface.close()


def test_emit_verified_connection_side_effects_sets_reconnected_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verified publish should signal reconnected_event for reconnect publishes."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    connected_client = DummyClient()
    connected_client.address = "AA:BB:CC:DD:EE:FF"
    connected_client.bleak_client = SimpleNamespace(address=connected_client.address)
    set_events: list[str] = []
    monkeypatch.setattr(
        iface.thread_coordinator,
        "_set_event",
        lambda event_name: set_events.append(event_name),
        raising=True,
    )

    iface._prior_publish_was_reconnect = True
    iface._emit_verified_connection_side_effects(cast(BLEClient, connected_client))

    assert set_events == ["reconnected_event"]
    assert iface._prior_publish_was_reconnect is False
    iface.close()


def test_thread_event_dispatcher_resolution_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Thread-event dispatcher resolution should cover underscored and missing hooks."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    original_close = iface.close
    original_thread_coordinator = iface.thread_coordinator
    try:
        class_calls: list[str] = []

        class _ClassCoordinator:
            def _set_event(self, event_name: str) -> None:
                class_calls.append(event_name)

        class_dispatch = BLEInterface._resolve_thread_event_dispatcher(
            _ClassCoordinator()
        )
        assert callable(class_dispatch)
        class_dispatch("class-evt")
        assert class_calls == ["class-evt"]
        iface.thread_coordinator = cast(Any, _ClassCoordinator())
        iface._set_thread_event("class-evt-2")
        assert class_calls == ["class-evt", "class-evt-2"]

        instance_calls: list[str] = []

        def _instance_set_event(event_name: str) -> None:
            instance_calls.append(event_name)

        instance_coordinator = SimpleNamespace(_set_event=_instance_set_event)
        instance_dispatch = BLEInterface._resolve_thread_event_dispatcher(
            instance_coordinator
        )
        assert callable(instance_dispatch)
        instance_dispatch("instance-evt")
        assert instance_calls == ["instance-evt"]

        missing_coordinator = SimpleNamespace()
        assert (
            BLEInterface._resolve_thread_event_dispatcher(missing_coordinator) is None
        )

        debug_messages: list[str] = []
        monkeypatch.setattr(
            "meshtastic.interfaces.ble.interface.logger.debug",
            lambda message, *_args, **_kwargs: debug_messages.append(
                cast(str, message)
            ),
        )
        iface.thread_coordinator = cast(Any, missing_coordinator)
        iface._set_thread_event("missing-evt")
        assert any(
            "No callable thread event dispatcher available" in message
            for message in debug_messages
        )
    finally:
        iface.thread_coordinator = original_thread_coordinator
        original_close()


def test_connect_notification_snapshot_compatibility_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Notification snapshot hooks should remain best effort for compatibility doubles."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)

    def _raise_dispatcher_lookup() -> object:
        raise RuntimeError("dispatcher lookup failed")

    try:
        with monkeypatch.context() as patcher:
            patcher.setattr(
                iface, "_get_notification_dispatcher", _raise_dispatcher_lookup
            )
            assert iface._snapshot_connect_notifications() == (None, None)

        missing_snapshotter = SimpleNamespace()
        with monkeypatch.context() as patcher:
            patcher.setattr(
                iface, "_get_notification_dispatcher", lambda: missing_snapshotter
            )
            assert iface._snapshot_connect_notifications() == (
                missing_snapshotter,
                None,
            )

        def _raise_snapshot_error() -> object:
            raise RuntimeError("snapshot failed")

        raising_snapshotter = SimpleNamespace(
            _snapshot_session_state=_raise_snapshot_error
        )
        with (
            monkeypatch.context() as patcher,
            caplog.at_level(logging.DEBUG),
        ):
            patcher.setattr(
                iface, "_get_notification_dispatcher", lambda: raising_snapshotter
            )
            assert iface._snapshot_connect_notifications() == (
                raising_snapshotter,
                None,
            )
        assert (
            "Unable to snapshot BLE notification state before connect." in caplog.text
        )

        invalid_snapshotter = SimpleNamespace(_snapshot_session_state=lambda: object())
        with monkeypatch.context() as patcher:
            patcher.setattr(
                iface, "_get_notification_dispatcher", lambda: invalid_snapshotter
            )
            assert iface._snapshot_connect_notifications() == (
                invalid_snapshotter,
                None,
            )
    finally:
        iface.close()


def test_restore_connect_notifications_is_best_effort(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Rollback should tolerate missing or failing notification restore hooks."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    snapshot = iface._notification_dispatcher._snapshot_session_state()

    try:
        BLEInterface._restore_connect_notifications(None, snapshot)
        BLEInterface._restore_connect_notifications(SimpleNamespace(), snapshot)

        def _raise_restore_error(_snapshot: object) -> None:
            raise RuntimeError("restore failed")

        failing_dispatcher = SimpleNamespace(
            _restore_session_state=_raise_restore_error
        )
        with caplog.at_level(logging.DEBUG):
            BLEInterface._restore_connect_notifications(failing_dispatcher, snapshot)
        assert (
            "Unable to restore BLE notification state after connect rollback."
            in caplog.text
        )
    finally:
        iface.close()


def test_establish_and_update_client_restores_notification_registration_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed connection setup should restore all registration-owned notification state."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    dispatcher = iface._notification_dispatcher
    manager = dispatcher._notification_manager

    def prior_callback(*_args: object) -> None:
        return None

    def new_callback(*_args: object) -> None:
        return None

    prior_token = manager._subscribe("prior-characteristic", prior_callback)
    prior_counter = manager._subscription_counter
    dispatcher._registered_notification_session_epoch = 17
    dispatcher._started_notify_characteristics = {"prior-characteristic"}
    with iface._state_lock:
        iface._client_publish_pending = True
        iface._client_replacement_pending = True
        iface._connection_session_epoch = 41
        prior_publish_pending = iface._client_publish_pending
        prior_replacement_pending = iface._client_replacement_pending
        prior_session_epoch = iface._connection_session_epoch

    def _fail_after_registration(*_args: object, **_kwargs: object) -> BLEClient:
        manager._subscribe("new-characteristic", new_callback)
        dispatcher._registered_notification_session_epoch = 99
        dispatcher._started_notify_characteristics.add("new-characteristic")
        raise BleakError("registration failed")

    monkeypatch.setattr(iface, "_establish_connection", _fail_after_registration)

    try:
        with (
            iface._connect_lock,
            pytest.raises(BleakError, match="registration failed"),
        ):
            iface._establish_and_update_client(
                "AA:BB:CC:DD:EE:FF",
                "aabbccddeeff",
                "aabbccddeeff",
            )

        assert manager._active_subscriptions == {
            prior_token: ("prior-characteristic", prior_callback)
        }
        assert manager._characteristic_to_callback == {
            "prior-characteristic": prior_callback
        }
        assert manager._subscription_counter == prior_counter
        assert dispatcher._registered_notification_session_epoch == 17
        assert dispatcher._started_notify_characteristics == {"prior-characteristic"}
        assert iface._client_publish_pending is prior_publish_pending
        assert iface._client_replacement_pending is prior_replacement_pending
        assert iface._connection_session_epoch == prior_session_epoch
    finally:
        iface.close()
