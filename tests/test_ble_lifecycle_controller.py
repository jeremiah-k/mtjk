"""Tests for BLELifecycleController lifecycle orchestration.

This module tests the BLELifecycleController class from the
lifecycle_controller_runtime module, focusing on initialization,
delegation to sub-controllers, and fallback hook resolution.
"""

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from meshtastic.interfaces.ble.lifecycle_controller_runtime import (
    BLELifecycleController,
)
from meshtastic.interfaces.ble.state import ConnectionState

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_ownership_with_capture() -> tuple[SimpleNamespace, dict[str, object]]:
    """Fixture providing mock ownership coordinator with capture dict."""
    captured: dict[str, object] = {}

    def _discard_invalidated_connected_client(*_args: object, **kwargs: object) -> None:
        for key in (
            "is_closing_getter",
            "reset_to_disconnected",
            "current_state_getter",
            "transition_to_disconnected",
            "safe_cleanup",
        ):
            getter = kwargs.get(key)
            if callable(getter):
                if key == "safe_cleanup":
                    captured[key] = getter(
                        lambda: captured.__setitem__("cleanup_called", True),
                        "test cleanup",
                    )
                else:
                    captured[key.replace("_getter", "")] = getter()

    mock_ownership = SimpleNamespace(
        _discard_invalidated_connected_client=_discard_invalidated_connected_client
    )
    return mock_ownership, captured


class TestBLELifecycleControllerInit:
    """Test cases for BLELifecycleController initialization."""

    def test_init_creates_collaborators(self) -> None:
        """BLELifecycleController.__init__ should create all sub-controllers."""
        mock_iface = SimpleNamespace()

        controller = BLELifecycleController(cast(Any, mock_iface))

        assert controller._iface is cast(Any, mock_iface)
        assert controller._receive is not None
        assert controller._disconnect is not None
        assert controller._connection_ownership is not None
        assert controller._shutdown is not None

    def test_init_binds_interface_reference(self) -> None:
        """BLELifecycleController should store reference to the bound interface."""
        mock_iface = SimpleNamespace(some_attr="test_value")

        controller = BLELifecycleController(cast(Any, mock_iface))

        assert controller._iface is cast(Any, mock_iface)
        assert cast(Any, controller._iface).some_attr == "test_value"


class TestBLELifecycleControllerReceiveDelegation:
    """Test cases for receive-related delegation methods."""

    def test_set_receive_wanted_delegates_to_receive_coordinator(self) -> None:
        """_set_receive_wanted should delegate to the _receive coordinator."""
        calls: list[tuple[str, bool]] = []
        mock_receive = SimpleNamespace(
            set_receive_wanted=lambda *, want_receive: calls.append(
                ("set_receive_wanted", want_receive)
            )
        )
        mock_iface = SimpleNamespace()
        controller = BLELifecycleController(cast(Any, mock_iface))
        controller._receive = mock_receive  # type: ignore[assignment]

        controller._set_receive_wanted(want_receive=True)

        assert calls == [("set_receive_wanted", True)]

    def test_set_receive_wanted_passes_false(self) -> None:
        """_set_receive_wanted should pass False correctly."""
        calls: list[tuple[str, bool]] = []
        mock_receive = SimpleNamespace(
            set_receive_wanted=lambda *, want_receive: calls.append(
                ("set_receive_wanted", want_receive)
            )
        )
        mock_iface = SimpleNamespace()
        controller = BLELifecycleController(cast(Any, mock_iface))
        controller._receive = mock_receive  # type: ignore[assignment]

        controller._set_receive_wanted(want_receive=False)

        assert calls == [("set_receive_wanted", False)]

    def test_should_run_receive_loop_delegates_to_receive_coordinator(self) -> None:
        """_should_run_receive_loop should delegate to the _receive coordinator."""
        mock_receive = SimpleNamespace(should_run_receive_loop=lambda: True)
        mock_iface = SimpleNamespace()
        controller = BLELifecycleController(cast(Any, mock_iface))
        controller._receive = mock_receive  # type: ignore[assignment]

        result = controller._should_run_receive_loop()

        assert result is True

    def test_should_run_receive_loop_returns_false(self) -> None:
        """_should_run_receive_loop should return False when coordinator says so."""
        mock_receive = SimpleNamespace(should_run_receive_loop=lambda: False)
        mock_iface = SimpleNamespace()
        controller = BLELifecycleController(cast(Any, mock_iface))
        controller._receive = mock_receive  # type: ignore[assignment]

        result = controller._should_run_receive_loop()

        assert result is False

    def test_start_receive_thread_delegates_to_receive_coordinator(self) -> None:
        """_start_receive_thread should delegate with correct parameters."""
        calls: list[tuple[str, bool]] = []
        mock_receive = SimpleNamespace(
            start_receive_thread=lambda *, name, reset_recovery: calls.append(
                (name, reset_recovery)
            )
        )
        mock_iface = SimpleNamespace()
        controller = BLELifecycleController(cast(Any, mock_iface))
        controller._receive = mock_receive  # type: ignore[assignment]

        controller._start_receive_thread(name="test-thread", reset_recovery=False)

        assert calls == [("test-thread", False)]

    def test_start_receive_thread_uses_default_reset_recovery(self) -> None:
        """_start_receive_thread should use default reset_recovery=True."""
        calls: list[tuple[str, bool]] = []
        mock_receive = SimpleNamespace(
            start_receive_thread=lambda *, name, reset_recovery: calls.append(
                (name, reset_recovery)
            )
        )
        mock_iface = SimpleNamespace()
        controller = BLELifecycleController(cast(Any, mock_iface))
        controller._receive = mock_receive  # type: ignore[assignment]

        controller._start_receive_thread(name="test-thread")

        assert calls == [("test-thread", True)]


class TestBLELifecycleControllerDisconnectDelegation:
    """Test cases for disconnect-related delegation methods."""

    def test_handle_disconnect_delegates_to_disconnect_coordinator(self) -> None:
        """_handle_disconnect should delegate to the _disconnect coordinator."""
        calls: list[dict[str, object]] = []

        def _handle_disconnect(
            source: str, *, client: object, bleak_client: object
        ) -> bool:
            calls.append(
                {"source": source, "client": client, "bleak_client": bleak_client}
            )
            return True

        mock_disconnect = SimpleNamespace(handle_disconnect=_handle_disconnect)
        mock_iface = SimpleNamespace()
        controller = BLELifecycleController(cast(Any, mock_iface))
        controller._disconnect = mock_disconnect  # type: ignore[assignment]

        mock_client = SimpleNamespace()
        mock_bleak = SimpleNamespace()
        result = controller._handle_disconnect(
            "test-source",
            client=cast(Any, mock_client),
            bleak_client=cast(Any, mock_bleak),
        )

        assert result is True
        assert calls == [
            {"source": "test-source", "client": mock_client, "bleak_client": mock_bleak}
        ]

    def test_handle_disconnect_returns_bool(self) -> None:
        """_handle_disconnect should return the boolean result from coordinator."""
        mock_disconnect = SimpleNamespace(handle_disconnect=lambda *_a, **_kw: False)
        mock_iface = SimpleNamespace()
        controller = BLELifecycleController(cast(Any, mock_iface))
        controller._disconnect = mock_disconnect  # type: ignore[assignment]

        result = controller._handle_disconnect("source")

        assert result is False

    def test_on_ble_disconnect_delegates_to_disconnect_coordinator(self) -> None:
        """_on_ble_disconnect should delegate to the _disconnect coordinator."""
        calls: list[object] = []
        mock_disconnect = SimpleNamespace(on_ble_disconnect=calls.append)
        mock_iface = SimpleNamespace()
        controller = BLELifecycleController(cast(Any, mock_iface))
        controller._disconnect = mock_disconnect  # type: ignore[assignment]

        mock_bleak = SimpleNamespace()
        controller._on_ble_disconnect(cast(Any, mock_bleak))

        assert calls == [mock_bleak]

    def test_schedule_auto_reconnect_delegates_to_disconnect_coordinator(self) -> None:
        """_schedule_auto_reconnect should delegate to the _disconnect coordinator."""
        calls: list[str] = []
        mock_disconnect = SimpleNamespace(
            schedule_auto_reconnect=lambda: calls.append("scheduled")
        )
        mock_iface = SimpleNamespace()
        controller = BLELifecycleController(cast(Any, mock_iface))
        controller._disconnect = mock_disconnect  # type: ignore[assignment]

        controller._schedule_auto_reconnect()

        assert calls == ["scheduled"]


class TestBLELifecycleControllerConnectionOwnershipDelegation:
    """Test cases for connection ownership delegation methods."""

    def test_emit_verified_connection_side_effects_delegates(self) -> None:
        """_emit_verified_connection_side_effects should delegate to _connection_ownership."""
        calls: list[object] = []
        mock_ownership = SimpleNamespace(
            _emit_verified_connection_side_effects=calls.append
        )
        mock_iface = SimpleNamespace()
        controller = BLELifecycleController(cast(Any, mock_iface))
        controller._connection_ownership = mock_ownership  # type: ignore[assignment]

        mock_client = SimpleNamespace()
        controller._emit_verified_connection_side_effects(cast(Any, mock_client))

        assert calls == [mock_client]


class TestBLELifecycleControllerHookResolution:
    """Test cases for _resolve_hook and fallback helper resolution.

    These tests exercise the hook resolution logic in _discard_invalidated_connected_client,
    specifically lines 177-184 (_resolve_hook), 186-194 (_fallback_is_closing),
    196-202 (_fallback_reset_to_disconnected), 204-210 (_fallback_current_state),
    and 212-219 (_fallback_transition_to_state).
    """

    def test_resolve_hook_with_callable_hook_returns_hook(self) -> None:
        """_resolve_hook should return the hook when it's a configured callable."""
        # Create a configured mock using public APIs.
        configured_hook = MagicMock(return_value=True)

        resolved_is_closing: list[bool] = []

        def _discard_invalidated_connected_client(
            *_args: object,
            **kwargs: object,
        ) -> None:
            is_closing_getter = kwargs.get("is_closing_getter")
            if callable(is_closing_getter):
                resolved_is_closing.append(is_closing_getter())

        mock_iface = SimpleNamespace(_state_manager_is_closing=configured_hook)
        mock_ownership = SimpleNamespace(
            _discard_invalidated_connected_client=_discard_invalidated_connected_client
        )

        controller = BLELifecycleController(cast(Any, mock_iface))
        controller._connection_ownership = mock_ownership  # type: ignore[assignment]

        # Call _discard_invalidated_connected_client which uses _resolve_hook internally
        mock_client = SimpleNamespace(address="test-addr")
        controller._discard_invalidated_connected_client(
            cast(Any, mock_client),
            restore_address=None,
            restore_last_connection_request=None,
        )

        configured_hook.assert_called_once_with()
        assert resolved_is_closing == [True]

    def test_resolve_hook_with_missing_hook_returns_fallback(self) -> None:
        """_resolve_hook should return fallback when hook attribute is missing."""
        mock_iface = SimpleNamespace()  # No _state_manager_is_closing attribute
        resolved_is_closing: list[bool] = []

        def _discard_invalidated_connected_client(
            *_args: object,
            **kwargs: object,
        ) -> None:
            is_closing_getter = kwargs.get("is_closing_getter")
            if callable(is_closing_getter):
                resolved_is_closing.append(is_closing_getter())

        mock_ownership = SimpleNamespace(
            _discard_invalidated_connected_client=_discard_invalidated_connected_client
        )

        controller = BLELifecycleController(cast(Any, mock_iface))
        controller._connection_ownership = mock_ownership  # type: ignore[assignment]

        mock_client = SimpleNamespace(address="test-addr")
        with patch(
            "meshtastic.interfaces.ble.lifecycle_service.BLELifecycleService._state_manager_is_closing",
            return_value=False,
        ) as state_manager_is_closing:
            # This should use fallback since no hook exists.
            controller._discard_invalidated_connected_client(
                cast(Any, mock_client),
                restore_address=None,
                restore_last_connection_request=None,
            )

        state_manager_is_closing.assert_called_once_with(mock_iface)
        assert resolved_is_closing == [False]

    def test_resolve_hook_with_non_callable_returns_fallback(self) -> None:
        """_resolve_hook should return fallback when hook is not callable."""
        mock_iface = SimpleNamespace(_state_manager_is_closing="not-a-callable")
        resolved_is_closing: list[bool] = []

        def _discard_invalidated_connected_client(
            *_args: object,
            **kwargs: object,
        ) -> None:
            is_closing_getter = kwargs.get("is_closing_getter")
            if callable(is_closing_getter):
                resolved_is_closing.append(is_closing_getter())

        mock_ownership = SimpleNamespace(
            _discard_invalidated_connected_client=_discard_invalidated_connected_client
        )

        controller = BLELifecycleController(cast(Any, mock_iface))
        controller._connection_ownership = mock_ownership  # type: ignore[assignment]

        mock_client = SimpleNamespace(address="test-addr")
        with patch(
            "meshtastic.interfaces.ble.lifecycle_service.BLELifecycleService._state_manager_is_closing",
            return_value=True,
        ) as state_manager_is_closing:
            # This should use fallback since hook is not callable.
            controller._discard_invalidated_connected_client(
                cast(Any, mock_client),
                restore_address=None,
                restore_last_connection_request=None,
            )

        state_manager_is_closing.assert_called_once_with(mock_iface)
        assert resolved_is_closing == [True]

    def test_fallback_is_closing_calls_lifecycle_service(self) -> None:
        """_fallback_is_closing should call BLELifecycleService._state_manager_is_closing."""
        mock_iface = SimpleNamespace()
        resolved_is_closing: list[bool] = []

        def _discard_invalidated_connected_client(
            *_args: object,
            **kwargs: object,
        ) -> None:
            is_closing_getter = kwargs.get("is_closing_getter")
            if callable(is_closing_getter):
                resolved_is_closing.append(is_closing_getter())

        mock_ownership = SimpleNamespace(
            _discard_invalidated_connected_client=_discard_invalidated_connected_client
        )

        controller = BLELifecycleController(cast(Any, mock_iface))
        controller._connection_ownership = mock_ownership  # type: ignore[assignment]

        with patch(
            "meshtastic.interfaces.ble.lifecycle_service.BLELifecycleService._state_manager_is_closing",
            return_value=True,
        ) as state_manager_is_closing:
            mock_client = SimpleNamespace(address="test-addr")
            controller._discard_invalidated_connected_client(
                cast(Any, mock_client),
                restore_address=None,
                restore_last_connection_request=None,
            )

        state_manager_is_closing.assert_called_once_with(mock_iface)
        assert resolved_is_closing == [True]

    def test_fallback_current_state_calls_lifecycle_service(
        self,
        mock_ownership_with_capture: tuple[SimpleNamespace, dict[str, object]],
    ) -> None:
        """_fallback_current_state should call BLELifecycleService._state_manager_current_state."""
        mock_iface = SimpleNamespace()
        mock_ownership, captured = mock_ownership_with_capture

        controller = BLELifecycleController(cast(Any, mock_iface))
        controller._connection_ownership = mock_ownership  # type: ignore[assignment]

        with (
            patch(
                "meshtastic.interfaces.ble.lifecycle_service.BLELifecycleService._state_manager_current_state",
                return_value=ConnectionState.DISCONNECTED,
            ) as state_manager_current_state,
            patch(
                "meshtastic.interfaces.ble.lifecycle_service.BLELifecycleService._state_manager_is_closing",
                return_value=False,
            ) as state_manager_is_closing,
            patch(
                "meshtastic.interfaces.ble.lifecycle_service.BLELifecycleService._state_manager_reset_to_disconnected",
                return_value=True,
            ) as state_manager_reset_to_disconnected,
            patch(
                "meshtastic.interfaces.ble.lifecycle_service.BLELifecycleService._state_manager_transition_to",
                return_value=True,
            ) as state_manager_transition_to,
            patch(
                "meshtastic.interfaces.ble.lifecycle_service.BLELifecycleService._error_handler_safe_cleanup",
                side_effect=lambda _iface, cleanup, _name: cleanup() or None,
            ) as safe_cleanup_handler,
        ):
            mock_client = SimpleNamespace(address="test-addr")
            controller._discard_invalidated_connected_client(
                cast(Any, mock_client),
                restore_address=None,
                restore_last_connection_request=None,
            )

        state_manager_current_state.assert_called_once_with(mock_iface)
        state_manager_is_closing.assert_called_once_with(mock_iface)
        state_manager_reset_to_disconnected.assert_called_once_with(mock_iface)
        state_manager_transition_to.assert_called_once_with(
            mock_iface,
            ConnectionState.DISCONNECTED,
        )
        safe_cleanup_handler.assert_called_once()
        assert captured["current_state"] == ConnectionState.DISCONNECTED
        assert captured["transition_to_disconnected"] is True
        assert captured["cleanup_called"] is True
        assert captured["safe_cleanup"] is None

    def test_fallback_transition_to_state_calls_lifecycle_service(
        self,
        mock_ownership_with_capture: tuple[SimpleNamespace, dict[str, object]],
    ) -> None:
        """_fallback_transition_to_state should call BLELifecycleService._state_manager_transition_to."""
        mock_iface = SimpleNamespace()
        mock_ownership, captured = mock_ownership_with_capture

        controller = BLELifecycleController(cast(Any, mock_iface))
        controller._connection_ownership = mock_ownership  # type: ignore[assignment]

        with (
            patch(
                "meshtastic.interfaces.ble.lifecycle_service.BLELifecycleService._state_manager_transition_to",
                side_effect=lambda _iface, state: state == ConnectionState.DISCONNECTED,
            ) as state_manager_transition_to,
            patch(
                "meshtastic.interfaces.ble.lifecycle_service.BLELifecycleService._state_manager_is_closing",
                return_value=False,
            ) as state_manager_is_closing,
            patch(
                "meshtastic.interfaces.ble.lifecycle_service.BLELifecycleService._state_manager_reset_to_disconnected",
                return_value=True,
            ) as state_manager_reset_to_disconnected,
            patch(
                "meshtastic.interfaces.ble.lifecycle_service.BLELifecycleService._state_manager_current_state",
                return_value=ConnectionState.CONNECTED,
            ) as state_manager_current_state,
            patch(
                "meshtastic.interfaces.ble.lifecycle_service.BLELifecycleService._error_handler_safe_cleanup",
                side_effect=lambda _iface, cleanup, _name: cleanup() or None,
            ) as safe_cleanup_handler,
        ):
            mock_client = SimpleNamespace(address="test-addr")
            controller._discard_invalidated_connected_client(
                cast(Any, mock_client),
                restore_address=None,
                restore_last_connection_request=None,
            )

        state_manager_transition_to.assert_called_once_with(
            mock_iface,
            ConnectionState.DISCONNECTED,
        )
        state_manager_is_closing.assert_called_once_with(mock_iface)
        state_manager_reset_to_disconnected.assert_called_once_with(mock_iface)
        state_manager_current_state.assert_called_once_with(mock_iface)
        safe_cleanup_handler.assert_called_once()
        assert captured["transition_to_disconnected"] is True
        assert captured["cleanup_called"] is True
        assert captured["safe_cleanup"] is None

    def test_discard_invalidated_connected_client_passes_restore_params(self) -> None:
        """_discard_invalidated_connected_client should pass restore parameters."""
        captured_kwargs: dict[str, object] = {}
        mock_iface = SimpleNamespace()

        def capture_kwargs(
            _client: object,
            *,
            restore_address: str | None,
            restore_last_connection_request: str | None,
            **kwargs: object,
        ) -> None:
            captured_kwargs["restore_address"] = restore_address
            captured_kwargs["restore_last_connection_request"] = (
                restore_last_connection_request
            )
            captured_kwargs["hooks"] = {
                k: v for k, v in kwargs.items() if "getter" in k or "cleanup" in k
            }

        mock_ownership = SimpleNamespace(
            _discard_invalidated_connected_client=capture_kwargs
        )

        controller = BLELifecycleController(cast(Any, mock_iface))
        controller._connection_ownership = mock_ownership  # type: ignore[assignment]

        mock_client = SimpleNamespace(address="test-addr")
        controller._discard_invalidated_connected_client(
            cast(Any, mock_client),
            restore_address="AA:BB:CC:DD:EE:FF",
            restore_last_connection_request="aa:bb:cc:dd:ee:ff",
        )

        assert captured_kwargs["restore_address"] == "AA:BB:CC:DD:EE:FF"
        assert captured_kwargs["restore_last_connection_request"] == "aa:bb:cc:dd:ee:ff"


class TestBLELifecycleControllerShutdownDelegation:
    """Test cases for shutdown-related delegation methods."""

    def test_is_connection_closing_delegates_to_shutdown_coordinator(self) -> None:
        """_is_connection_closing should delegate to the _shutdown coordinator."""
        mock_shutdown = SimpleNamespace(is_connection_closing=lambda: True)
        mock_iface = SimpleNamespace()
        controller = BLELifecycleController(cast(Any, mock_iface))
        controller._shutdown = mock_shutdown  # type: ignore[assignment]

        result = controller._is_connection_closing()

        assert result is True

    def test_close_delegates_to_shutdown_coordinator(self) -> None:
        """_close should delegate to the _shutdown coordinator with correct params."""
        calls: list[dict[str, float]] = []
        mock_shutdown = SimpleNamespace(
            close=lambda *, management_shutdown_wait_timeout, management_wait_poll_seconds: calls.append(
                {
                    "timeout": management_shutdown_wait_timeout,
                    "poll": management_wait_poll_seconds,
                }
            )
        )
        mock_iface = SimpleNamespace()
        controller = BLELifecycleController(cast(Any, mock_iface))
        controller._shutdown = mock_shutdown  # type: ignore[assignment]

        controller._close(
            management_shutdown_wait_timeout=5.0,
            management_wait_poll_seconds=0.5,
        )

        assert calls == [{"timeout": 5.0, "poll": 0.5}]

    def test_close_forwards_optional_total_timeout(self) -> None:
        """_close should forward timeout budget to shutdown coordinator when provided."""
        calls: list[dict[str, float | None]] = []

        def _capture_close(
            *,
            management_shutdown_wait_timeout: float,
            management_wait_poll_seconds: float,
            timeout: float | None = None,
        ) -> None:
            calls.append(
                {
                    "timeout": management_shutdown_wait_timeout,
                    "poll": management_wait_poll_seconds,
                    "total": timeout,
                }
            )

        mock_shutdown = SimpleNamespace(close=_capture_close)
        mock_iface = SimpleNamespace()
        controller = BLELifecycleController(cast(Any, mock_iface))
        controller._shutdown = mock_shutdown  # type: ignore[assignment]

        controller._close(
            management_shutdown_wait_timeout=5.0,
            management_wait_poll_seconds=0.5,
            timeout=1.25,
        )

        assert calls == [{"timeout": 5.0, "poll": 0.5, "total": 1.25}]


class TestBLELifecycleControllerClientManagement:
    """Test cases for client management methods."""

    def test_disconnect_and_close_client_delegates_to_disconnect_coordinator(
        self,
    ) -> None:
        """_disconnect_and_close_client should delegate to _disconnect coordinator."""
        calls: list[tuple[object, float | None]] = []

        def _capture_disconnect(
            client: object, *, timeout: float | None = None
        ) -> None:
            calls.append((client, timeout))

        mock_disconnect = SimpleNamespace(
            disconnect_and_close_client=_capture_disconnect
        )
        mock_iface = SimpleNamespace()
        controller = BLELifecycleController(cast(Any, mock_iface))
        controller._disconnect = mock_disconnect  # type: ignore[assignment]

        mock_client = SimpleNamespace()
        controller._disconnect_and_close_client(cast(Any, mock_client))

        assert calls == [(mock_client, None)]

    def test_close_without_timeout_parameter_invoked_once(self) -> None:
        """_close should omit timeout when the shutdown callable does not accept it."""
        calls: list[dict[str, object]] = []

        def _close_without_timeout(
            *,
            management_shutdown_wait_timeout: float,
            management_wait_poll_seconds: float,
        ) -> None:
            calls.append(
                {
                    "management_shutdown_wait_timeout": management_shutdown_wait_timeout,
                    "management_wait_poll_seconds": management_wait_poll_seconds,
                }
            )

        mock_shutdown = SimpleNamespace(close=_close_without_timeout)
        mock_iface = SimpleNamespace()
        controller = BLELifecycleController(cast(Any, mock_iface))
        controller._shutdown = mock_shutdown  # type: ignore[assignment]

        controller._close(
            management_shutdown_wait_timeout=5.0,
            management_wait_poll_seconds=0.5,
            timeout=1.25,
        )

        assert calls == [
            {
                "management_shutdown_wait_timeout": 5.0,
                "management_wait_poll_seconds": 0.5,
            }
        ]

    def test_close_propagates_unrelated_type_error(self) -> None:
        """_close should NOT swallow unrelated TypeError during shutdown."""

        def _raise_unrelated(
            *,
            management_shutdown_wait_timeout: float,
            management_wait_poll_seconds: float,
            timeout: float | None = None,
        ) -> None:
            del management_shutdown_wait_timeout, management_wait_poll_seconds, timeout
            raise TypeError("takes 1 positional argument but 2 were given")

        mock_shutdown = SimpleNamespace(close=_raise_unrelated)
        mock_iface = SimpleNamespace()
        controller = BLELifecycleController(cast(Any, mock_iface))
        controller._shutdown = mock_shutdown  # type: ignore[assignment]

        with pytest.raises(TypeError, match="takes 1 positional argument"):
            controller._close(
                management_shutdown_wait_timeout=5.0,
                management_wait_poll_seconds=0.5,
                timeout=1.25,
            )

    def test_disconnect_and_close_client_without_timeout_parameter_invoked_once(
        self,
    ) -> None:
        """_disconnect_and_close_client should omit timeout when unsupported."""
        calls: list[tuple[object, ...]] = []

        def _disconnect_without_timeout(client: object) -> None:
            calls.append((client,))

        mock_disconnect = SimpleNamespace(
            disconnect_and_close_client=_disconnect_without_timeout
        )
        mock_iface = SimpleNamespace()
        controller = BLELifecycleController(cast(Any, mock_iface))
        controller._disconnect = mock_disconnect  # type: ignore[assignment]

        mock_client = SimpleNamespace()
        controller._disconnect_and_close_client(cast(Any, mock_client), timeout=3.0)

        assert calls == [(mock_client,)]

    def test_disconnect_and_close_client_propagates_unrelated_type_error(self) -> None:
        """_disconnect_and_close_client should NOT swallow unrelated TypeError."""

        def _raise_unrelated(client: object, *, timeout: float | None = None) -> None:
            del client, timeout
            raise TypeError("takes 1 positional argument but 2 were given")

        mock_disconnect = SimpleNamespace(disconnect_and_close_client=_raise_unrelated)
        mock_iface = SimpleNamespace()
        controller = BLELifecycleController(cast(Any, mock_iface))
        controller._disconnect = mock_disconnect  # type: ignore[assignment]

        mock_client = SimpleNamespace()
        with pytest.raises(TypeError, match="takes 1 positional argument"):
            controller._disconnect_and_close_client(cast(Any, mock_client), timeout=3.0)

    def test_has_ever_connected_session_delegates_to_ownership_coordinator(
        self,
    ) -> None:
        """_has_ever_connected_session should delegate to _connection_ownership coordinator."""
        mock_ownership = SimpleNamespace(_has_ever_connected_session=lambda: True)
        mock_iface = SimpleNamespace()
        controller = BLELifecycleController(cast(Any, mock_iface))
        controller._connection_ownership = mock_ownership  # type: ignore[assignment]

        result = controller._has_ever_connected_session()

        assert result is True

    def test_is_owned_connected_client_delegates_to_ownership_coordinator(self) -> None:
        """_is_owned_connected_client should delegate to _connection_ownership coordinator."""
        calls: list[object] = []

        def _is_owned_connected_client(client: object) -> bool:
            calls.append(client)
            return True

        mock_ownership = SimpleNamespace(
            _is_owned_connected_client=_is_owned_connected_client
        )
        mock_iface = SimpleNamespace()
        controller = BLELifecycleController(cast(Any, mock_iface))
        controller._connection_ownership = mock_ownership  # type: ignore[assignment]

        mock_client = SimpleNamespace()
        result = controller._is_owned_connected_client(cast(Any, mock_client))

        assert result is True
        assert calls == [mock_client]
