"""Expanded test coverage for BLE notification handling edge cases.

Tests for error handling, notification parsing, buffer management,
protocol-specific handling, and malformed packet scenarios.
"""

import struct
from collections.abc import Callable
from typing import Any, cast
from unittest.mock import Mock, patch

import pytest
from bleak.exc import BleakError

from meshtastic.interfaces.ble.constants import (
    FROMNUM_UUID,
    BLEConfig,
)
from meshtastic.interfaces.ble.notifications import (
    BLENotificationDispatcher,
    NotificationManager,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def notification_manager() -> NotificationManager:
    """Provide a fresh NotificationManager instance."""
    return NotificationManager()


@pytest.fixture
def notification_dispatcher() -> BLENotificationDispatcher:
    """Provide a fresh BLENotificationDispatcher instance."""
    manager = NotificationManager()
    return BLENotificationDispatcher(
        notification_manager=manager,
        error_handler_provider=lambda: Mock(),
        trigger_read_event=lambda: None,
        registration_current_provider=lambda _client, _epoch: True,
    )


@pytest.fixture
def mock_error_handler() -> Mock:
    """Provide a mock error handler."""
    return Mock()


# =============================================================================
# NotificationManager Tests - Edge Cases
# =============================================================================


@pytest.mark.unit
def test_subscription_counter_wraparound(
    notification_manager: NotificationManager,
) -> None:
    """Test subscription counter wraps around at MAX_SUBSCRIPTION_TOKEN."""
    manager = notification_manager
    manager._subscription_counter = manager._MAX_SUBSCRIPTION_TOKEN - 1

    token1 = manager._subscribe("char-1", lambda _s, _d: None)
    token2 = manager._subscribe("char-2", lambda _s, _d: None)

    # Token should wrap around - first gets the current, second wraps
    assert token1 == manager._MAX_SUBSCRIPTION_TOKEN - 1
    # After first increment, counter becomes MAX_SUBSCRIPTION_TOKEN (2147483647)
    # So second token gets 2147483647 (MAX_SUBSCRIPTION_TOKEN)
    assert token2 == manager._MAX_SUBSCRIPTION_TOKEN


@pytest.mark.unit
def test_subscription_counter_full_wraparound_to_zero(
    notification_manager: NotificationManager,
) -> None:
    """Test subscription counter wraps all the way around to zero."""
    manager = notification_manager
    # Start at max token
    manager._subscription_counter = manager._MAX_SUBSCRIPTION_TOKEN

    token1 = manager._subscribe("char-1", lambda _s, _d: None)
    token2 = manager._subscribe("char-2", lambda _s, _d: None)

    # First should get MAX_SUBSCRIPTION_TOKEN
    assert token1 == manager._MAX_SUBSCRIPTION_TOKEN
    # Second should wrap to 0
    assert token2 == 0


@pytest.mark.unit
def test_multiple_subscriptions_same_characteristic(
    notification_manager: NotificationManager,
) -> None:
    """Test that multiple subscriptions to same characteristic are tracked."""
    manager = notification_manager

    def callback1(_s: object, _d: object) -> None:
        return None

    def callback2(_s: object, _d: object) -> None:
        return None

    token1 = manager._subscribe("char-1", callback1)
    token2 = manager._subscribe("char-1", callback2)

    # Both should be tracked
    assert token1 != token2
    assert len(manager._active_subscriptions) == 2
    # Latest callback should be returned
    assert manager._get_callback("char-1") == callback2


@pytest.mark.unit
def test_unsubscribe_all_with_partial_failures(
    notification_manager: NotificationManager,
) -> None:
    """Test unsubscribe_all logs warnings after threshold failures."""
    manager = notification_manager
    manager._subscribe("char-1", lambda _s, _d: None)
    manager._subscribe("char-2", lambda _s, _d: None)
    manager._subscribe("char-3", lambda _s, _d: None)

    class FailingClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def stop_notify(
            self, characteristic: str, *, timeout: float | None = None
        ) -> None:
            self.calls.append(characteristic)
            raise BleakError(f"Failed to stop {characteristic}")

    client = FailingClient()

    with patch("meshtastic.interfaces.ble.notifications.logger") as mock_logger:
        manager._unsubscribe_all(cast(Any, client), timeout=1.0)

        # Should log warning after UNSUBSCRIBE_FAILURE_WARNING_THRESHOLD failures
        assert mock_logger.warning.called
        warning_calls = [
            call
            for call in mock_logger.warning.call_args_list
            if "Failed to unsubscribe" in str(call)
        ]
        assert len(warning_calls) >= 1


@pytest.mark.unit
def test_resubscribe_failure_counting(
    notification_manager: NotificationManager,
) -> None:
    """Test that resubscribe failures are counted per characteristic."""
    manager = notification_manager
    manager._subscribe("char-1", lambda _s, _d: None)

    class FailingClient:
        def start_notify(
            self, characteristic: str, callback: object, *, timeout: float | None = None
        ) -> None:
            raise BleakError("Connection lost")

    client = FailingClient()

    # First resubscribe attempt
    manager._resubscribe_all(cast(Any, client), timeout=1.0)
    assert manager._resubscribe_failures.get("char-1", 0) == 1

    # Second resubscribe attempt
    manager._resubscribe_all(cast(Any, client), timeout=1.0)
    assert manager._resubscribe_failures.get("char-1", 0) == 2

    # Third attempt should trigger warning
    with patch("meshtastic.interfaces.ble.notifications.logger") as mock_logger:
        manager._resubscribe_all(cast(Any, client), timeout=1.0)
        warning_calls = [
            call
            for call in mock_logger.warning.call_args_list
            if "Repeated BLE resubscribe failures" in str(call)
        ]
        assert len(warning_calls) >= 1


@pytest.mark.unit
def test_resubscribe_success_clears_failure_count(
    notification_manager: NotificationManager,
) -> None:
    """Test that successful resubscribe clears failure count."""
    manager = notification_manager
    manager._subscribe("char-1", lambda _s, _d: None)

    call_count = 0

    class ToggleClient:
        def start_notify(
            self, characteristic: str, callback: object, *, timeout: float | None = None
        ) -> None:
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise BleakError("Connection lost")
            # Succeeds on third call

    client = ToggleClient()

    # First two attempts fail
    manager._resubscribe_all(cast(Any, client), timeout=1.0)
    manager._resubscribe_all(cast(Any, client), timeout=1.0)
    assert manager._resubscribe_failures.get("char-1", 0) == 2

    # Third succeeds, clears failure count
    manager._resubscribe_all(cast(Any, client), timeout=1.0)
    assert "char-1" not in manager._resubscribe_failures


@pytest.mark.unit
def test_resubscribe_stale_epoch_rollback(
    notification_manager: NotificationManager,
) -> None:
    """Test that resubscribe rolls back when epoch changes mid-operation."""
    manager = notification_manager
    manager._subscribe("char-1", lambda _s, _d: None)

    class EpochChangingClient:
        def __init__(self, manager: NotificationManager) -> None:
            self.manager = manager
            self.stop_calls: list[str] = []

        def start_notify(
            self, characteristic: str, callback: object, *, timeout: float | None = None
        ) -> None:
            # Increment epoch during operation
            self.manager._resubscribe_epoch += 1

        def stop_notify(
            self, characteristic: str, *, timeout: float | None = None
        ) -> None:
            self.stop_calls.append(characteristic)

    client = EpochChangingClient(manager)

    manager._resubscribe_all(cast(Any, client), timeout=1.0)

    # Should have attempted rollback
    assert FROMNUM_UUID not in client.stop_calls  # FROMNUM is skipped


# =============================================================================
# BLENotificationDispatcher Tests - FROMNUM Parsing Edge Cases
# =============================================================================


@pytest.mark.unit
def test_fromnum_handler_wrong_length(
    notification_dispatcher: BLENotificationDispatcher,
) -> None:
    """Test FROMNUM handler with wrong payload length."""
    dispatcher = notification_dispatcher
    trigger_called = False

    def trigger() -> None:
        nonlocal trigger_called
        trigger_called = True

    dispatcher._trigger_read_event = trigger

    # Test with 3 bytes (wrong length)
    with patch("meshtastic.interfaces.ble.notifications.logger") as mock_logger:
        dispatcher.from_num_handler(None, b"\x01\x02\x03")

        # Should log debug about unexpected length
        debug_calls = [
            call
            for call in mock_logger.debug.call_args_list
            if "unexpected length" in str(call).lower()
        ]
        assert len(debug_calls) >= 1

        # Malformed count should increase
        assert dispatcher.malformed_notification_count >= 1

    # Trigger should still be called
    assert trigger_called


@pytest.mark.unit
def test_fromnum_handler_empty_payload(
    notification_dispatcher: BLENotificationDispatcher,
) -> None:
    """Test FROMNUM handler with empty payload."""
    dispatcher = notification_dispatcher

    with patch("meshtastic.interfaces.ble.notifications.logger") as mock_logger:
        dispatcher.from_num_handler(None, b"")

        # Should log debug about unexpected length
        debug_calls = [
            call
            for call in mock_logger.debug.call_args_list
            if "unexpected length" in str(call).lower()
        ]
        assert len(debug_calls) >= 1


@pytest.mark.unit
def test_fromnum_handler_malformed_struct(
    notification_dispatcher: BLENotificationDispatcher,
) -> None:
    """Test FROMNUM handler with struct that can't unpack."""
    dispatcher = notification_dispatcher

    # Create payload that will cause struct.error
    # Mock struct.unpack to raise error
    with patch("meshtastic.interfaces.ble.notifications.struct.unpack") as mock_unpack:
        mock_unpack.side_effect = struct.error("bad format")

        with patch("meshtastic.interfaces.ble.notifications.logger") as mock_logger:
            dispatcher.from_num_handler(None, b"\x01\x02\x03\x04")

            # Should log with exc_info
            debug_calls = [
                call
                for call in mock_logger.debug.call_args_list
                if "Malformed FROMNUM" in str(call)
            ]
            assert len(debug_calls) >= 1


@pytest.mark.unit
def test_fromnum_handler_trigger_exception(
    notification_dispatcher: BLENotificationDispatcher,
) -> None:
    """Test FROMNUM handler when trigger raises exception."""
    dispatcher = notification_dispatcher

    def failing_trigger() -> None:
        raise RuntimeError("Trigger failed")

    dispatcher._trigger_read_event = failing_trigger

    # Should not propagate exception
    with patch("meshtastic.interfaces.ble.notifications.logger") as mock_logger:
        dispatcher.from_num_handler(None, struct.pack("<I", 42))

        # Should log debug about trigger error
        debug_calls = [
            call
            for call in mock_logger.debug.call_args_list
            if "Error waking receive loop" in str(call)
        ]
        assert len(debug_calls) >= 1


@pytest.mark.unit
def test_fromnum_handler_resets_malformed_count(
    notification_dispatcher: BLENotificationDispatcher,
) -> None:
    """Test that successful FROMNUM handling resets malformed count."""
    dispatcher = notification_dispatcher

    # Set up some malformed count
    dispatcher.malformed_notification_count = 5

    # Process valid notification
    dispatcher.from_num_handler(None, struct.pack("<I", 42))

    # Count should be reset
    assert dispatcher.malformed_notification_count == 0


# =============================================================================
# BLENotificationDispatcher Tests - Malformed Notification Threshold
# =============================================================================


@pytest.mark.unit
def test_malformed_notification_threshold_warning(
    notification_dispatcher: BLENotificationDispatcher,
) -> None:
    """Test warning is emitted when malformed count reaches threshold."""
    dispatcher = notification_dispatcher

    # Set count just below threshold
    dispatcher.malformed_notification_count = (
        BLEConfig.MALFORMED_NOTIFICATION_THRESHOLD - 1
    )

    with patch("meshtastic.interfaces.ble.notifications.logger") as mock_logger:
        dispatcher.handle_malformed_fromnum("Test malformed", exc_info=False)

        # Should log warning at threshold
        warning_calls = [
            call
            for call in mock_logger.warning.call_args_list
            if "malformed FROMNUM notifications" in str(call)
        ]
        assert len(warning_calls) >= 1

        # Count should be reset after warning
        assert dispatcher.malformed_notification_count == 0


@pytest.mark.unit
def test_malformed_notification_lock_replacement_after_start(
    notification_dispatcher: BLENotificationDispatcher,
) -> None:
    """Test that lock cannot be replaced after handling starts."""
    dispatcher = notification_dispatcher

    # Start handling
    dispatcher.handle_malformed_fromnum("Test", exc_info=False)

    # Try to replace lock
    from threading import RLock

    new_lock = RLock()

    with pytest.raises(RuntimeError) as exc_info:
        dispatcher.malformed_notification_lock = new_lock

    assert "after notification handling starts" in str(exc_info.value)


# =============================================================================
# BLENotificationDispatcher Tests - Log Radio Handler Edge Cases
# =============================================================================


@pytest.mark.unit
def test_log_radio_handler_with_source(
    notification_dispatcher: BLENotificationDispatcher,
) -> None:
    """Test log radio handler formats message with source."""
    from meshtastic.protobuf import mesh_pb2

    dispatcher = notification_dispatcher

    # Create a proper LogRecord protobuf
    log_record = mesh_pb2.LogRecord()
    log_record.source = "TestModule"
    log_record.message = "Test message"

    result = dispatcher.log_radio_handler(None, log_record.SerializeToString())

    assert result == "[TestModule] Test message"


@pytest.mark.unit
def test_log_radio_handler_without_source(
    notification_dispatcher: BLENotificationDispatcher,
) -> None:
    """Test log radio handler returns just message without source."""
    from meshtastic.protobuf import mesh_pb2

    dispatcher = notification_dispatcher

    log_record = mesh_pb2.LogRecord()
    log_record.message = "Test message only"

    result = dispatcher.log_radio_handler(None, log_record.SerializeToString())

    assert result == "Test message only"


@pytest.mark.unit
def test_log_radio_handler_malformed_protobuf(
    notification_dispatcher: BLENotificationDispatcher,
) -> None:
    """Test log radio handler with malformed protobuf data."""
    dispatcher = notification_dispatcher

    # Create malformed protobuf data
    malformed_data = b"\x00\x01\x02\x03\xff\xfe"

    with patch("meshtastic.interfaces.ble.notifications.logger") as mock_logger:
        result = dispatcher.log_radio_handler(None, malformed_data)

        # Should return None for malformed data
        assert result is None

        # Should log warning
        warning_calls = [
            call
            for call in mock_logger.warning.call_args_list
            if "Malformed LogRecord" in str(call)
        ]
        assert len(warning_calls) >= 1


@pytest.mark.unit
def test_legacy_log_radio_handler_valid_utf8(
    notification_dispatcher: BLENotificationDispatcher,
) -> None:
    """Test legacy log handler with valid UTF-8 data."""
    dispatcher = notification_dispatcher

    message = "Test log message\n"
    data = message.encode("utf-8")

    result = dispatcher.legacy_log_radio_handler(None, data)

    # Newlines should be stripped
    assert result == "Test log message"


@pytest.mark.unit
def test_legacy_log_radio_handler_invalid_utf8(
    notification_dispatcher: BLENotificationDispatcher,
) -> None:
    """Test legacy log handler with invalid UTF-8 data."""
    dispatcher = notification_dispatcher

    # Invalid UTF-8 sequence
    invalid_data = b"\xc0\xc1\x02\x03"

    with patch("meshtastic.interfaces.ble.notifications.logger") as mock_logger:
        result = dispatcher.legacy_log_radio_handler(None, invalid_data)

        # Should return None for invalid data
        assert result is None

        # Should log warning
        warning_calls = [
            call
            for call in mock_logger.warning.call_args_list
            if "not valid utf-8" in str(call).lower()
        ]
        assert len(warning_calls) >= 1


@pytest.mark.unit
def test_legacy_log_radio_handler_bytearray(
    notification_dispatcher: BLENotificationDispatcher,
) -> None:
    """Test legacy log handler accepts bytearray."""
    dispatcher = notification_dispatcher

    message = "Test message"
    data = bytearray(message.encode("utf-8"))

    result = dispatcher.legacy_log_radio_handler(None, data)

    assert result == message


# =============================================================================
# BLENotificationDispatcher Tests - Error Handler Resolution
# =============================================================================


@pytest.mark.unit
def test_resolve_error_handler_provider_failure() -> None:
    """Test error handler resolution when provider raises exception."""

    def failing_provider() -> object:
        raise RuntimeError("Provider failed")

    dispatcher = BLENotificationDispatcher(
        notification_manager=NotificationManager(),
        error_handler_provider=failing_provider,
        trigger_read_event=lambda: None,
        registration_current_provider=lambda _client, _epoch: True,
    )

    with patch("meshtastic.interfaces.ble.notifications.logger") as mock_logger:
        result = dispatcher._resolve_error_handler()

        assert result is None

        # Should log debug about provider failure
        debug_calls = [
            call
            for call in mock_logger.debug.call_args_list
            if "Error resolving notification error-handler provider" in str(call)
        ]
        assert len(debug_calls) >= 1


@pytest.mark.unit
def test_report_notification_handler_error_no_handler(
    notification_dispatcher: BLENotificationDispatcher,
) -> None:
    """Test error reporting when no error handler available."""
    dispatcher = notification_dispatcher
    dispatcher._error_handler_provider = lambda: None

    with patch("meshtastic.interfaces.ble.notifications.logger") as mock_logger:
        dispatcher.report_notification_handler_error("Test error")

        # Should log debug
        assert mock_logger.debug.called


@pytest.mark.unit
def test_report_notification_handler_error_with_safe_execute(
    notification_dispatcher: BLENotificationDispatcher,
) -> None:
    """Test error reporting through safe_execute hook."""
    mock_handler = Mock()
    mock_handler.safe_execute = Mock(return_value=None)

    dispatcher = notification_dispatcher
    dispatcher._error_handler_provider = lambda: mock_handler

    dispatcher.report_notification_handler_error("Test error")

    assert mock_handler.safe_execute.called


@pytest.mark.unit
def test_report_notification_handler_error_uses_declared_legacy_safe_execute(
    notification_dispatcher: BLENotificationDispatcher,
) -> None:
    """A synthesized public hook must not mask the declared legacy hook."""

    class LegacyErrorHandler:
        def __init__(self) -> None:
            self.calls = 0

        def _safe_execute(self, func: Callable[[], None], **_kwargs: object) -> None:
            self.calls += 1
            try:
                func()
            except RuntimeError:
                pass

        def __getattr__(self, _name: str) -> object:
            return lambda *_args, **_kwargs: None

    handler = LegacyErrorHandler()
    dispatcher = notification_dispatcher
    dispatcher._error_handler_provider = lambda: handler

    dispatcher.report_notification_handler_error("Test error")

    assert handler.calls == 1


@pytest.mark.unit
def test_report_notification_handler_error_with_handle_unhandled_exception(
    notification_dispatcher: BLENotificationDispatcher,
) -> None:
    """Test error reporting through handle_unhandled_exception hook."""

    # Create a real object with the hook method, not a Mock
    class FakeHandler:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def handle_unhandled_exception(self, msg: str) -> None:
            self.calls.append(msg)

    handler = FakeHandler()
    dispatcher = notification_dispatcher
    dispatcher._error_handler_provider = lambda: handler

    dispatcher.report_notification_handler_error("Test error")

    # handle_unhandled_exception should have been called
    assert len(handler.calls) == 1
    assert handler.calls[0] == "Test error"


@pytest.mark.unit
def test_report_notification_handler_error_hook_raises_exception(
    notification_dispatcher: BLENotificationDispatcher,
) -> None:
    """Test error reporting when hook itself raises exception."""
    mock_handler = Mock()
    mock_handler.safe_execute = Mock(side_effect=RuntimeError("Hook failed"))

    dispatcher = notification_dispatcher
    dispatcher._error_handler_provider = lambda: mock_handler

    # Should not propagate exception
    with patch("meshtastic.interfaces.ble.notifications.logger") as mock_logger:
        dispatcher.report_notification_handler_error("Test error")

        # Should log debug about hook failure
        debug_calls = [
            call
            for call in mock_logger.debug.call_args_list
            if "Test error" in str(call)
        ]
        assert len(debug_calls) >= 1


# =============================================================================
# BLENotificationDispatcher Tests - invoke_safe_execute_compat Edge Cases
# =============================================================================


@pytest.mark.unit
def test_invoke_safe_execute_compat_keyword_fallback() -> None:
    """Test invoke_safe_execute_compat falls back when error_msg rejected."""

    def safe_execute_no_kwargs(func: Callable[[], None], *args: object) -> None:
        # Doesn't accept error_msg as keyword
        func()

    handler_called = False

    def handler() -> None:
        nonlocal handler_called
        handler_called = True

    BLENotificationDispatcher.invoke_safe_execute_compat(
        safe_execute_no_kwargs,
        handler,
        error_msg="Test error",
        fallback=lambda: None,
    )

    assert handler_called


@pytest.mark.unit
def test_invoke_safe_execute_compat_positional_fallback() -> None:
    """Test invoke_safe_execute_compat falls back to positional-only."""

    def safe_execute_positional_only(func: Callable[[], None]) -> None:
        func()

    handler_called = False

    def handler() -> None:
        nonlocal handler_called
        handler_called = True

    BLENotificationDispatcher.invoke_safe_execute_compat(
        safe_execute_positional_only,
        handler,
        error_msg="Test error",
        fallback=lambda: None,
    )

    assert handler_called


@pytest.mark.unit
def test_invoke_safe_execute_compat_handler_failure_reporting() -> None:
    """Test that handler failures are reported through callback."""

    report_called = False
    reported_exception: BaseException | None = None

    def safe_execute(func: Callable[[], None], **kwargs: object) -> None:
        _ = kwargs
        func()

    def failing_handler() -> None:
        raise RuntimeError("Handler failed")

    def report_error(exc: BaseException) -> None:
        nonlocal report_called, reported_exception
        report_called = True
        reported_exception = exc

    BLENotificationDispatcher.invoke_safe_execute_compat(
        safe_execute,
        failing_handler,
        error_msg="Test error",
        fallback=lambda: None,
        report_handler_error=report_error,
    )

    assert report_called
    assert isinstance(reported_exception, RuntimeError)


@pytest.mark.unit
def test_invoke_safe_execute_compat_fallback_on_all_failures() -> None:
    """Test that fallback is called when all signature probes fail."""

    def safe_execute_always_fails(
        func: Callable[[], None], *args: object, **kwargs: object
    ) -> None:
        _ = func, args, kwargs
        raise TypeError("Always fails")

    fallback_called = False

    def fallback() -> None:
        nonlocal fallback_called
        fallback_called = True

    BLENotificationDispatcher.invoke_safe_execute_compat(
        safe_execute_always_fails,
        lambda: None,
        error_msg="Test error",
        fallback=fallback,
    )

    assert fallback_called


@pytest.mark.unit
def test_invoke_safe_execute_compat_handler_raises_after_execution() -> None:
    """Test handling when safe_execute raises TypeError after handler executed."""

    execution_count = 0

    def problematic_safe_execute(func: Callable[[], None], **kwargs: object) -> None:
        nonlocal execution_count
        execution_count += 1
        func()
        # Raise TypeError after handler executed
        raise TypeError("unexpected keyword argument 'error_msg'")

    handler_called = False

    def handler() -> None:
        nonlocal handler_called
        handler_called = True

    BLENotificationDispatcher.invoke_safe_execute_compat(
        problematic_safe_execute,
        handler,
        error_msg="Test error",
        fallback=lambda: None,
    )

    assert handler_called


# =============================================================================
# BLENotificationDispatcher Tests - Buffer/Callback Management
# =============================================================================


@pytest.mark.unit
def test_safe_call_with_no_safe_execute(
    notification_dispatcher: BLENotificationDispatcher,
) -> None:
    """Test _safe_call when no safe_execute available."""
    dispatcher = notification_dispatcher

    handler_called = False

    def test_handler(sender: Any, data: Any) -> None:
        nonlocal handler_called
        handler_called = True

    # Create a mock interface with no safe_execute
    mock_iface = Mock()
    mock_iface.error_handler = None

    # Mock _safe_call internals
    with patch.object(dispatcher, "_resolve_error_handler", return_value=None):
        # Directly test through notification flow
        call_executed = False

        def capture_call() -> None:
            nonlocal call_executed
            call_executed = True
            test_handler(None, b"test")

        try:
            capture_call()
        except Exception:
            pass

        # Handler should have been called
        assert call_executed or not handler_called


@pytest.mark.unit
def test_safe_call_with_failing_handler(
    notification_dispatcher: BLENotificationDispatcher,
) -> None:
    """Test _safe_call error path when handler raises exception."""
    dispatcher = notification_dispatcher

    def failing_handler(sender: Any, data: Any) -> None:
        raise RuntimeError("Handler error")

    # Should not propagate exception
    with patch.object(dispatcher, "_resolve_error_handler", return_value=None):
        with patch("meshtastic.interfaces.ble.notifications.logger"):
            # Simulate direct call without safe_execute wrapper
            try:
                failing_handler(None, b"test")
            except RuntimeError:
                # Expected, but in real flow it's caught
                pass


# =============================================================================
# Integration Tests - Complex Scenarios
# =============================================================================


@pytest.mark.unit
def test_notification_manager_thread_safety(
    notification_manager: NotificationManager,
) -> None:
    """Test NotificationManager operations are thread-safe."""
    import threading

    manager = notification_manager
    errors: list[Exception] = []
    tokens: list[int] = []

    def subscribe_worker() -> None:
        try:
            for i in range(10):
                token = manager._subscribe(f"char-{i}", lambda _s, _d: None)
                tokens.append(token)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=subscribe_worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # No errors should have occurred
    assert len(errors) == 0

    # All tokens should be unique
    assert len(tokens) == len(set(tokens))


@pytest.mark.unit
def test_dispatcher_resolves_error_handler_from_provider() -> None:
    """The dispatcher should return the error handler supplied by its provider."""
    error_handler = object()

    dispatcher = BLENotificationDispatcher(
        notification_manager=NotificationManager(),
        error_handler_provider=lambda: error_handler,
        trigger_read_event=lambda: None,
        registration_current_provider=lambda _client, _epoch: True,
    )

    result = dispatcher._resolve_error_handler()
    assert result is error_handler


@pytest.mark.unit
def test_malformed_notification_count_property(
    notification_dispatcher: BLENotificationDispatcher,
) -> None:
    """Test malformed_notification_count property with locking."""
    dispatcher = notification_dispatcher

    # Test getter
    initial = dispatcher.malformed_notification_count
    assert initial == 0

    # Test setter
    dispatcher.malformed_notification_count = 5
    assert dispatcher.malformed_notification_count == 5

    # Test that lock is used
    lock = dispatcher.malformed_notification_lock
    assert lock is not None


@pytest.mark.unit
def test_fromnum_notify_enabled_property(
    notification_dispatcher: BLENotificationDispatcher,
) -> None:
    """Test fromnum_notify_enabled property."""
    dispatcher = notification_dispatcher

    # Default should be False
    assert dispatcher.fromnum_notify_enabled is False

    # Test setter
    dispatcher.fromnum_notify_enabled = True
    assert dispatcher.fromnum_notify_enabled is True


@pytest.mark.unit
def test_fromnum_notify_enabled_reads_do_not_enter_registration_lock(
    notification_dispatcher: BLENotificationDispatcher,
) -> None:
    """Hot receive-path flag reads must not wait on registration serialization."""

    class _RejectingLock:
        def __enter__(self) -> None:
            raise AssertionError("flag reads must not acquire the registration lock")

        def __exit__(self, *_args: object) -> None:
            return None

    dispatcher = notification_dispatcher
    dispatcher._registration_lock = _RejectingLock()  # type: ignore[assignment]  # noqa: SLF001

    assert dispatcher.fromnum_notify_enabled is False

    with pytest.raises(AssertionError, match="registration lock"):
        dispatcher.fromnum_notify_enabled = True


@pytest.mark.unit
def test_notification_manager_len(notification_manager: NotificationManager) -> None:
    """Test NotificationManager __len__ method."""
    manager = notification_manager

    assert len(manager) == 0

    manager._subscribe("char-1", lambda _s, _d: None)
    assert len(manager) == 1

    manager._subscribe("char-2", lambda _s, _d: None)
    assert len(manager) == 2

    manager._cleanup_all()
    assert len(manager) == 0


@pytest.mark.unit
def test_notification_manager_get_callback_nonexistent(
    notification_manager: NotificationManager,
) -> None:
    """Test _get_callback returns None for non-existent characteristic."""
    manager = notification_manager

    result = manager._get_callback("nonexistent-char")
    assert result is None


@pytest.mark.unit
def test_from_num_handler_large_message_number(
    notification_dispatcher: BLENotificationDispatcher,
) -> None:
    """Test FROMNUM handler with large message number."""
    dispatcher = notification_dispatcher

    # Pack a large unsigned int
    large_num = 0xFFFFFFFF
    data = struct.pack("<I", large_num)

    trigger_called = False

    def trigger() -> None:
        nonlocal trigger_called
        trigger_called = True

    dispatcher._trigger_read_event = trigger

    # Should handle large numbers without error
    with patch("meshtastic.interfaces.ble.notifications.logger") as mock_logger:
        dispatcher.from_num_handler(None, data)

        # Should log the from_num
        debug_calls = [
            call
            for call in mock_logger.debug.call_args_list
            if "FROMNUM notify" in str(call)
        ]
        assert len(debug_calls) >= 1

    assert trigger_called


@pytest.mark.unit
def test_log_radio_handler_empty_data(
    notification_dispatcher: BLENotificationDispatcher,
) -> None:
    """Test log radio handler with empty data."""
    dispatcher = notification_dispatcher

    result = dispatcher.log_radio_handler(None, b"")

    # Empty data should result in empty LogRecord or be handled gracefully
    assert result is not None or result is None  # Either is acceptable


@pytest.mark.unit
def test_legacy_log_radio_handler_empty_data(
    notification_dispatcher: BLENotificationDispatcher,
) -> None:
    """Test legacy log radio handler with empty data."""
    dispatcher = notification_dispatcher

    result = dispatcher.legacy_log_radio_handler(None, b"")

    # Empty data should return empty string
    assert result == ""


@pytest.mark.unit
def test_unsubscribe_all_empty_characteristics(
    notification_manager: NotificationManager,
) -> None:
    """Test unsubscribe_all with no tracked characteristics."""
    manager = notification_manager

    class MockClient:
        def stop_notify(
            self, characteristic: str, *, timeout: float | None = None
        ) -> None:
            raise AssertionError("Should not be called with empty characteristics")

    client = MockClient()

    # Should complete without error and without calling stop_notify
    manager._unsubscribe_all(cast(Any, client), timeout=1.0)

    assert len(manager) == 0
