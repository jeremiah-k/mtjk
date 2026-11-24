"""Regression coverage retained for the documented stable receive service."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.receive_compat_service import BLEReceiveRecoveryService
from meshtastic.interfaces.ble.receive_service import BLEReceiveRecoveryController

pytestmark = pytest.mark.unit

# pylint: disable=attribute-defined-outside-init


def configured_mock(return_value: Any = None, side_effect: Any = None) -> MagicMock:
    """Create a configured MagicMock that won't be detected as unconfigured."""
    mock = MagicMock()
    if side_effect is not None:
        mock.side_effect = side_effect
    else:
        mock.return_value = return_value
    return mock


class _DynamicAttributeProxy:
    """Test double that synthesizes undeclared attributes dynamically."""

    def __getattr__(self, _name: str) -> _DynamicAttributeProxy:
        return self

    def __call__(self, *_args: Any, **_kwargs: Any) -> _DynamicAttributeProxy:
        return self


class ControllerLike:
    """Mock controller class for testing receive service compatibility.

    Attributes are declared for typing only and attached at runtime by
    ``create_configured_controller_mock``.
    """

    handle_read_loop_disconnect: MagicMock
    _wait_for_read_trigger: MagicMock
    _snapshot_client_state: MagicMock
    _process_client_state: MagicMock
    _reset_recovery_after_stability: MagicMock
    _read_and_handle_payload: MagicMock
    _handle_payload_read: MagicMock
    _run_receive_cycle: MagicMock
    receive_from_radio_impl: MagicMock
    recover_receive_thread: MagicMock
    read_from_radio_with_retries: MagicMock
    handle_transient_read_error: MagicMock
    log_empty_read_warning: MagicMock
    _should_run_receive_loop: MagicMock
    _set_receive_wanted: MagicMock
    _handle_disconnect: MagicMock
    _start_receive_thread: MagicMock


def create_configured_controller_mock() -> ControllerLike:
    """Create a properly configured controller mock that passes _is_controller_like check."""

    controller = ControllerLike()
    controller.handle_read_loop_disconnect = configured_mock()
    controller._wait_for_read_trigger = configured_mock()
    controller._snapshot_client_state = configured_mock()
    controller._process_client_state = configured_mock()
    controller._reset_recovery_after_stability = configured_mock()
    controller._read_and_handle_payload = configured_mock()
    controller._handle_payload_read = configured_mock()
    controller._run_receive_cycle = configured_mock()
    controller.receive_from_radio_impl = configured_mock()
    controller.recover_receive_thread = configured_mock()
    controller.read_from_radio_with_retries = configured_mock()
    controller.handle_transient_read_error = configured_mock()
    controller.log_empty_read_warning = configured_mock()
    controller._should_run_receive_loop = configured_mock()
    controller._set_receive_wanted = configured_mock()
    controller._handle_disconnect = configured_mock()
    controller._start_receive_thread = configured_mock()
    return controller


class TestBLEReceiveRecoveryServiceControllerDetection:
    """Test controller detection methods for receive compatibility service."""

    def test_controller_class_resolution(self) -> None:
        """Test controller class resolution."""
        result = BLEReceiveRecoveryService._controller_class()
        assert result is BLEReceiveRecoveryController

    def test_is_controller_like_with_required_method(self) -> None:
        """Test checking for a specific required method."""
        controller = create_configured_controller_mock()

        result = BLEReceiveRecoveryService._is_controller_like(
            controller, required_method="handle_read_loop_disconnect"
        )
        assert result is True

    def test_is_controller_like_dynamic_proxy(self) -> None:
        """Dynamic-only controller members should not satisfy the contract."""
        mock_controller = _DynamicAttributeProxy()

        result = BLEReceiveRecoveryService._is_controller_like(mock_controller)
        assert result is False

    def test_is_controller_like_full_api(self) -> None:
        """Test checking for full controller API surface."""
        controller = create_configured_controller_mock()

        result = BLEReceiveRecoveryService._is_controller_like(controller)
        assert result is True

    def test_is_controller_like_missing_method(self) -> None:
        """Test controller missing one method returns False."""

        class PartialController:
            """Partial controller with limited methods for testing."""

            handle_read_loop_disconnect: MagicMock

        controller = PartialController()
        controller.handle_read_loop_disconnect = configured_mock()
        # Missing other required methods

        result = BLEReceiveRecoveryService._is_controller_like(controller)
        assert result is False

    def test_resolve_controller_candidate_direct_instance(self) -> None:
        """Test resolving direct controller instance."""
        controller = create_configured_controller_mock()

        result = BLEReceiveRecoveryService._resolve_controller_candidate(
            controller, iface=MagicMock()
        )
        # Returns the controller if it's controller-like
        assert result is not None

    def test_resolve_controller_candidate_from_factory(self) -> None:
        """Test resolving controller from factory callable."""
        controller = create_configured_controller_mock()

        # Factory that works with or without iface argument
        def factory(iface: Any = None) -> Any:
            return controller

        mock_iface = MagicMock()
        result = BLEReceiveRecoveryService._resolve_controller_candidate(
            factory, iface=mock_iface
        )
        assert result is cast(Any, controller)

    def test_resolve_controller_candidate_none(self) -> None:
        """Test resolving None returns None."""
        result = BLEReceiveRecoveryService._resolve_controller_candidate(None)
        assert result is None

    def test_resolve_controller_candidate_dynamic_proxy(self) -> None:
        """Dynamic-only controller candidates should resolve to None."""
        mock_controller = _DynamicAttributeProxy()

        result = BLEReceiveRecoveryService._resolve_controller_candidate(
            mock_controller
        )
        assert result is None


class TestBLEReceiveRecoveryServiceControllerForShim:
    """Test controller resolution for receive compatibility service."""

    def test_controller_for_shim_getter_method(self) -> None:
        """Test using _get_receive_recovery_controller getter method."""
        iface = MagicMock()
        controller = create_configured_controller_mock()

        iface._get_receive_recovery_controller = configured_mock(
            return_value=controller
        )

        result = BLEReceiveRecoveryService._controller_for_shim(iface)
        assert result is not None

    def test_controller_for_shim_cached_controller(self) -> None:
        """Test using cached controller from iface."""
        iface = MagicMock()
        controller = create_configured_controller_mock()

        iface._get_receive_recovery_controller = _DynamicAttributeProxy()
        iface._receive_recovery_controller = controller

        result = BLEReceiveRecoveryService._controller_for_shim(iface)
        assert result is not None

    def test_controller_for_shim_create_new_controller(self) -> None:
        """Test creating new controller when no cached controller exists."""
        iface = MagicMock()
        iface._get_receive_recovery_controller = _DynamicAttributeProxy()
        iface._receive_recovery_controller = _DynamicAttributeProxy()
        iface._state_lock = None

        result = BLEReceiveRecoveryService._controller_for_shim(iface)
        assert isinstance(result, BLEReceiveRecoveryController)

    def test_controller_for_shim_getter_raises_exception(self) -> None:
        """Test fallback when getter raises exception."""
        iface = MagicMock()
        controller = create_configured_controller_mock()

        iface._get_receive_recovery_controller = configured_mock(
            side_effect=AttributeError("test error")
        )
        iface._receive_recovery_controller = controller

        result = BLEReceiveRecoveryService._controller_for_shim(iface)
        assert result is not None

    def test_controller_for_shim_with_state_lock(self) -> None:
        """Test controller creation with state lock."""
        iface = MagicMock()
        iface._get_receive_recovery_controller = _DynamicAttributeProxy()
        iface._receive_recovery_controller = _DynamicAttributeProxy()
        mock_lock = MagicMock()
        mock_lock.__enter__ = MagicMock(return_value=None)
        mock_lock.__exit__ = MagicMock(return_value=None)
        iface._state_lock = mock_lock

        result = BLEReceiveRecoveryService._controller_for_shim(iface)
        assert isinstance(result, BLEReceiveRecoveryController)


class TestBLEReceiveRecoveryServiceOperations:
    """Test receive/recovery operations through compatibility service."""

    def test_handle_read_loop_disconnect(self) -> None:
        """Test handling read loop disconnect through compatibility service."""
        iface = MagicMock()
        controller = create_configured_controller_mock()
        controller.handle_read_loop_disconnect = configured_mock(return_value=True)
        iface._receive_recovery_controller = controller
        iface._get_receive_recovery_controller = _DynamicAttributeProxy()

        mock_client = MagicMock(spec=BLEClient)
        result = BLEReceiveRecoveryService._handle_read_loop_disconnect(
            iface, "test disconnect", mock_client
        )
        assert result is True

    def test_should_poll_without_notify_true(self) -> None:
        """Test should poll without notify returns True when notify disabled."""
        iface = MagicMock()
        iface._fromnum_notify_enabled = False
        mock_lock = MagicMock()
        mock_lock.__enter__ = MagicMock(return_value=None)
        mock_lock.__exit__ = MagicMock(return_value=None)
        iface._state_lock = mock_lock

        result = BLEReceiveRecoveryService._should_poll_without_notify(iface)
        assert result is True

    def test_should_poll_without_notify_false(self) -> None:
        """Test should poll without notify returns False when notify enabled."""
        iface = MagicMock()
        iface._fromnum_notify_enabled = True
        mock_lock = MagicMock()
        mock_lock.__enter__ = MagicMock(return_value=None)
        mock_lock.__exit__ = MagicMock(return_value=None)
        iface._state_lock = mock_lock

        result = BLEReceiveRecoveryService._should_poll_without_notify(iface)
        assert result is False

    def test_wait_for_read_trigger(self) -> None:
        """Test waiting for read trigger through compatibility service."""
        iface = MagicMock()
        controller = create_configured_controller_mock()
        controller._wait_for_read_trigger = configured_mock(return_value=(True, False))
        iface._receive_recovery_controller = controller
        iface._get_receive_recovery_controller = _DynamicAttributeProxy()

        coordinator = MagicMock()
        result = BLEReceiveRecoveryService._wait_for_read_trigger(
            iface, coordinator=coordinator, wait_timeout=1.0
        )
        assert result == (True, False)

    def test_snapshot_client_state(self) -> None:
        """Test snapshotting client state through compatibility service."""
        iface = MagicMock()
        controller = create_configured_controller_mock()
        expected_result = (None, False, False, False)
        controller._snapshot_client_state = configured_mock(
            return_value=expected_result
        )
        iface._receive_recovery_controller = controller
        iface._get_receive_recovery_controller = _DynamicAttributeProxy()

        result = BLEReceiveRecoveryService._snapshot_client_state(iface)
        assert result == expected_result

    def test_process_client_state(self) -> None:
        """Test processing client state through compatibility service."""
        iface = MagicMock()
        controller = create_configured_controller_mock()
        controller._process_client_state = configured_mock(return_value=False)
        iface._receive_recovery_controller = controller
        iface._get_receive_recovery_controller = _DynamicAttributeProxy()

        coordinator = MagicMock()
        mock_client = MagicMock(spec=BLEClient)
        result = BLEReceiveRecoveryService._process_client_state(
            iface,
            coordinator=coordinator,
            wait_timeout=1.0,
            client=mock_client,
            is_connecting=False,
            publish_pending=False,
            is_closing=False,
        )
        assert result is False

    def test_reset_recovery_after_stability(self) -> None:
        """Test resetting recovery after stability through compatibility service."""
        iface = MagicMock()
        controller = create_configured_controller_mock()
        controller._reset_recovery_after_stability = configured_mock()
        iface._receive_recovery_controller = controller
        iface._get_receive_recovery_controller = _DynamicAttributeProxy()

        # Should not raise
        BLEReceiveRecoveryService._reset_recovery_after_stability(iface)
        controller._reset_recovery_after_stability.assert_called_once()

    def test_read_and_handle_payload(self) -> None:
        """Test reading and handling payload through compatibility service."""
        iface = MagicMock()
        controller = create_configured_controller_mock()
        controller._read_and_handle_payload = configured_mock(return_value=True)
        iface._receive_recovery_controller = controller
        iface._get_receive_recovery_controller = _DynamicAttributeProxy()

        mock_client = MagicMock(spec=BLEClient)
        result = BLEReceiveRecoveryService._read_and_handle_payload(
            iface, mock_client, poll_without_notify=False
        )
        assert result is True

    def test_handle_payload_read(self) -> None:
        """Test handling payload read through compatibility service."""
        iface = MagicMock()
        controller = create_configured_controller_mock()
        controller._handle_payload_read = configured_mock(return_value=(False, False))
        iface._receive_recovery_controller = controller
        iface._get_receive_recovery_controller = _DynamicAttributeProxy()

        mock_client = MagicMock(spec=BLEClient)
        result = BLEReceiveRecoveryService._handle_payload_read(
            iface, mock_client, poll_without_notify=False
        )
        assert result == (False, False)

    def test_run_receive_cycle(self) -> None:
        """Test running receive cycle through compatibility service."""
        iface = MagicMock()
        controller = create_configured_controller_mock()
        controller._run_receive_cycle = configured_mock(return_value=True)
        iface._receive_recovery_controller = controller
        iface._get_receive_recovery_controller = _DynamicAttributeProxy()

        coordinator = MagicMock()
        result = BLEReceiveRecoveryService._run_receive_cycle(
            iface, coordinator=coordinator, wait_timeout=1.0
        )
        assert result is True

    def test_receive_from_radio_impl(self) -> None:
        """Test receive from radio impl through compatibility service."""
        iface = MagicMock()
        controller = create_configured_controller_mock()
        controller.receive_from_radio_impl = configured_mock()
        iface._receive_recovery_controller = controller
        iface._get_receive_recovery_controller = _DynamicAttributeProxy()

        # Should not raise
        BLEReceiveRecoveryService._receive_from_radio_impl(iface)
        controller.receive_from_radio_impl.assert_called_once()

    def test_recover_receive_thread(self) -> None:
        """Test recovering receive thread through compatibility service."""
        iface = MagicMock()
        controller = create_configured_controller_mock()
        controller.recover_receive_thread = configured_mock()
        iface._receive_recovery_controller = controller
        iface._get_receive_recovery_controller = _DynamicAttributeProxy()

        # Should not raise
        BLEReceiveRecoveryService._recover_receive_thread(iface, "test disconnect")
        controller.recover_receive_thread.assert_called_once_with("test disconnect")

    def test_read_from_radio_with_retries(self) -> None:
        """Test reading from radio with retries through compatibility service."""
        iface = MagicMock()
        controller = create_configured_controller_mock()
        expected_data = b"test_data"
        controller.read_from_radio_with_retries = configured_mock(
            return_value=expected_data
        )
        iface._receive_recovery_controller = controller
        iface._get_receive_recovery_controller = _DynamicAttributeProxy()

        mock_client = MagicMock(spec=BLEClient)
        result = BLEReceiveRecoveryService._read_from_radio_with_retries(
            iface, mock_client, retry_on_empty=True
        )
        assert result == expected_data

    def test_handle_transient_read_error(self) -> None:
        """Test handling transient read error through compatibility service."""
        iface = MagicMock()
        controller = create_configured_controller_mock()
        controller.handle_transient_read_error = configured_mock()
        iface._receive_recovery_controller = controller
        iface._get_receive_recovery_controller = _DynamicAttributeProxy()

        mock_error = MagicMock()
        # Should not raise
        BLEReceiveRecoveryService._handle_transient_read_error(iface, mock_error)
        controller.handle_transient_read_error.assert_called_once_with(mock_error)

    def test_log_empty_read_warning(self) -> None:
        """Test logging empty read warning through compatibility service."""
        iface = MagicMock()
        controller = create_configured_controller_mock()
        controller.log_empty_read_warning = configured_mock()
        iface._receive_recovery_controller = controller
        iface._get_receive_recovery_controller = _DynamicAttributeProxy()

        # Should not raise
        BLEReceiveRecoveryService._log_empty_read_warning(iface)
        controller.log_empty_read_warning.assert_called_once()


class TestBLEReceiveRecoveryServiceCoordinatorOperations:
    """Test coordinator operations through receive compatibility service."""

    def test_coordinator_wait_for_event(self) -> None:
        """Test coordinator wait for event through compatibility service."""
        coordinator = MagicMock()

        # Mock the controller class method
        with patch.object(
            BLEReceiveRecoveryController,
            "_coordinator_wait_for_event",
            return_value=True,
        ) as mock_wait:
            result = BLEReceiveRecoveryService._coordinator_wait_for_event(
                coordinator, "test_event", timeout=1.0
            )
            assert result is True
            mock_wait.assert_called_once_with(coordinator, "test_event", timeout=1.0)

    def test_coordinator_check_and_clear_event(self) -> None:
        """Test coordinator check and clear event through compatibility service."""
        coordinator = MagicMock()

        with patch.object(
            BLEReceiveRecoveryController,
            "_coordinator_check_and_clear_event",
            return_value=True,
        ) as mock_check:
            result = BLEReceiveRecoveryService._coordinator_check_and_clear_event(
                coordinator, "test_event"
            )
            assert result is True
            mock_check.assert_called_once_with(coordinator, "test_event")

    def test_coordinator_clear_event(self) -> None:
        """Test coordinator clear event through compatibility service."""
        coordinator = MagicMock()

        with patch.object(
            BLEReceiveRecoveryController, "_coordinator_clear_event"
        ) as mock_clear:
            BLEReceiveRecoveryService._coordinator_clear_event(
                coordinator, "test_event"
            )
            mock_clear.assert_called_once_with(coordinator, "test_event")


class TestBLEReceiveRecoveryServiceEdgeCases:
    """Test edge cases and error handling for receive compatibility service."""

    def test_should_poll_without_notify_mock_value(self) -> None:
        """Test handling mock value for notify enabled flag."""
        iface = MagicMock()
        # A non-bool dynamic proxy must not be treated as notification-enabled.
        iface._fromnum_notify_enabled = _DynamicAttributeProxy()
        mock_lock = MagicMock()
        mock_lock.__enter__ = MagicMock(return_value=None)
        mock_lock.__exit__ = MagicMock(return_value=None)
        iface._state_lock = mock_lock

        result = BLEReceiveRecoveryService._should_poll_without_notify(iface)
        assert result is True  # Should return True when notify is effectively disabled

    def test_resolve_controller_candidate_factory_raises(self) -> None:
        """Test handling factory that raises exception."""

        def failing_factory(iface: Any) -> None:
            raise RuntimeError("test error")

        mock_iface = MagicMock()
        result = BLEReceiveRecoveryService._resolve_controller_candidate(
            failing_factory, iface=mock_iface
        )
        assert result is None

    def test_controller_for_shim_class_cannot_be_resolved(self) -> None:
        """Test when controller class resolution returns a class (not instance)."""

        # This tests the edge case where resolved is still a class after factory probe
        def class_factory(iface: Any) -> type[BLEReceiveRecoveryController]:
            # Return the class itself instead of instance
            return BLEReceiveRecoveryController

        mock_iface = MagicMock()
        result = BLEReceiveRecoveryService._resolve_controller_candidate(
            class_factory, iface=mock_iface
        )
        # Should return None when the result is still a class
        assert result is None

    def test_resolve_controller_candidate_with_required_method(self) -> None:
        """Test resolving controller with specific required method check."""
        controller = create_configured_controller_mock()

        result = BLEReceiveRecoveryService._resolve_controller_candidate(
            controller, required_method="handle_read_loop_disconnect"
        )
        assert result is not None


# =============================================================================
# Integration Tests
# =============================================================================
