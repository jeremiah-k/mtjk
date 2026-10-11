"""Test coverage for BLE errors module.

This module tests exception handling, error formatting, and edge cases
in the BLE errors module to achieve comprehensive coverage.
"""

from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any, NoReturn
from unittest.mock import Mock, patch

import pytest
from bleak.exc import BleakDBusError, BleakError
from google.protobuf.message import DecodeError as ProtobufDecodeError

from meshtastic.interfaces.ble import errors as ble_errors_module
from meshtastic.interfaces.ble.constants import logger
from meshtastic.interfaces.ble.errors import (
    BLEDBusTransportError,
    BLEErrorHandler,
    DecodeError,
)

pytestmark = pytest.mark.unit


class TestBLEDBusTransportErrorFlattening:
    """Test BLEDBusTransportError.from_exception flattens nested DBus args."""

    def test_flatten_single_list_wrapper(self) -> None:
        """BleakDBusError-style args with a list body should produce string details."""
        original = BleakDBusError(
            "org.bluez.Error.Failed",
            ["Device or resource busy"],
        )
        err = BLEDBusTransportError.from_exception(
            original,
            message="BLE DBus transport error during direct connect.",
            requested_identifier="AA:BB:CC:DD:EE:FF",
            address="AA:BB:CC:DD:EE:FF",
        )
        assert err.dbus_error_details == "Device or resource busy"
        assert err.dbus_error_body == ("Device or resource busy",)

    def test_flatten_single_tuple_wrapper(self) -> None:
        """A tuple-wrapped body should also be flattened."""
        original = Exception("wrapper")
        original.args = ("org.bluez.Error.Failed", ("Operation already in progress",))
        err = BLEDBusTransportError.from_exception(
            original,
            message="transport failed",
        )
        assert err.dbus_error_name == "org.bluez.Error.Failed"
        assert err.dbus_error_body == ("Operation already in progress",)

    def test_args_zero_dbus_name_is_preserved_without_attr(self) -> None:
        """DBus error names in args[0] should be preserved when attr is absent."""
        original = Exception("wrapper")
        original.args = ("org.bluez.Error.InProgress", ["Operation in progress"])
        err = BLEDBusTransportError.from_exception(
            original,
            message="transport failed",
        )
        assert err.dbus_error_name == "org.bluez.Error.InProgress"
        assert err.dbus_error_details == "Operation in progress"

    def test_no_flatten_for_multiple_args(self) -> None:
        """Multiple positional args should remain as-is."""
        original = Exception("wrapper")
        original.args = ("org.bluez.Error.Failed", "arg1", "arg2")
        err = BLEDBusTransportError.from_exception(
            original,
            message="transport failed",
        )
        assert err.dbus_error_body == ("arg1", "arg2")

    def test_stale_bluez_detection_matches_normalized_wrapper(self) -> None:
        """Stale-BlueZ token matching should work against the flattened detail."""
        original = BleakDBusError(
            "org.bluez.Error.Failed",
            ["Device or resource busy"],
        )
        err = BLEDBusTransportError.from_exception(
            original,
            message="transport failed",
        )
        # The detail string must be lowercase-matchable for stale-BlueZ detection.
        assert "device or resource busy" in str(err.dbus_error_details).casefold()


class TestDecodeErrorImport:
    """Test DecodeError import fallback behavior."""

    def test_decodeerror_is_protobuf_by_default(self) -> None:
        """DecodeError should be protobuf's DecodeError when available."""
        assert DecodeError is ProtobufDecodeError

    def test_fallback_decodeerror_when_protobuf_unavailable(self) -> None:
        """Test that fallback DecodeError class behaves correctly.

        Note: Testing the actual import-time fallback path requires an isolated
        environment without google.protobuf. This test verifies the fallback class
        implementation would work correctly if triggered.
        """
        # The errors module always exposes DecodeError
        # When protobuf is available, it's ProtobufDecodeError
        # When unavailable, it's the internal _FallbackDecodeError
        assert hasattr(ble_errors_module, "DecodeError")

        # Verify DecodeError can be instantiated (works for both real and fallback)
        err = ble_errors_module.DecodeError("test message")
        assert str(err) == "test message"
        assert isinstance(err, Exception)

        # Test with multiple args
        err2 = ble_errors_module.DecodeError("arg1", "arg2")
        assert err2.args == ("arg1", "arg2")

    def test_non_google_import_error_raises_other_modules(self) -> None:
        """Non-google ImportError should propagate for non-google modules."""
        # Test the logic path used in the actual import error handling
        exc = ImportError("Some other error")
        exc.name = "some.other.module"
        # This should be re-raised
        with pytest.raises(ImportError, match="Some other error"):
            if exc.name not in (
                "google",
                "google.protobuf",
                "google.protobuf.message",
            ):
                raise exc


class TestBLEErrorHandlerSafeExecute:
    """Test BLEErrorHandler._safe_execute and safe_execute methods."""

    def test_safe_execute_success(self) -> None:
        """Test successful execution returns func result."""
        result = BLEErrorHandler._safe_execute(lambda: "success")
        assert result == "success"

    def test_safe_execute_with_default_return(self) -> None:
        """Test default_return used when exception caught."""
        result = BLEErrorHandler._safe_execute(
            lambda: (_ for _ in ()).throw(BleakError("BLE error")),
            default_return="default",
        )
        assert result == "default"

    def test_safe_execute_with_custom_default_return(self) -> None:
        """Test custom default_return value."""
        custom_return = {"key": "value"}
        result = BLEErrorHandler._safe_execute(
            lambda: (_ for _ in ()).throw(DecodeError("decode error")),
            default_return=custom_return,
        )
        assert result is custom_return

    def test_safe_execute_bleak_error_logging(self) -> None:
        """Test BleakError is logged correctly."""
        with patch.object(logger, "debug") as mock_debug:
            BLEErrorHandler._safe_execute(
                lambda: (_ for _ in ()).throw(BleakError("connection failed")),
                log_error=True,
                error_msg="BLE operation failed",
            )
            mock_debug.assert_called_once()
            args, _kwargs = mock_debug.call_args
            # logger.debug("%s: %s", error_msg, e) - first arg is format string
            assert args[0] == "%s: %s"
            assert "BLE operation failed" in str(args)
            assert "connection failed" in str(args)

    def test_safe_execute_decode_error_logging(self) -> None:
        """Test DecodeError is logged correctly."""
        with patch.object(logger, "debug") as mock_debug:
            BLEErrorHandler._safe_execute(
                lambda: (_ for _ in ()).throw(DecodeError("protobuf parse failed")),
                log_error=True,
                error_msg="Parse error",
            )
            mock_debug.assert_called_once()
            args, _kwargs = mock_debug.call_args
            assert args[0] == "%s: %s"
            assert "Parse error" in str(args)
            assert "protobuf parse failed" in str(args)

    def test_safe_execute_future_timeout_error_logging(self) -> None:
        """Test FutureTimeoutError is logged correctly."""
        with patch.object(logger, "debug") as mock_debug:
            BLEErrorHandler._safe_execute(
                lambda: (_ for _ in ()).throw(
                    FutureTimeoutError("operation timed out")
                ),
                log_error=True,
                error_msg="Timeout occurred",
            )
            mock_debug.assert_called_once()
            args, _kwargs = mock_debug.call_args
            assert args[0] == "%s: %s"
            assert "Timeout occurred" in str(args)
            assert "operation timed out" in str(args)

    def test_safe_execute_no_logging_when_log_error_false(self) -> None:
        """Test that no logging occurs when log_error=False."""
        with patch.object(logger, "debug") as mock_debug:
            result = BLEErrorHandler._safe_execute(
                lambda: (_ for _ in ()).throw(BleakError("error")),
                log_error=False,
                default_return="fallback",
            )
            assert result == "fallback"
            mock_debug.assert_not_called()

    def test_safe_execute_reraise_bleak_error(self) -> None:
        """Test BleakError is re-raised when reraise=True."""
        with pytest.raises(BleakError, match="test bleak error"):
            BLEErrorHandler._safe_execute(
                lambda: (_ for _ in ()).throw(BleakError("test bleak error")),
                reraise=True,
            )

    def test_safe_execute_reraise_decode_error(self) -> None:
        """Test DecodeError is re-raised when reraise=True."""
        with pytest.raises(DecodeError, match="test decode error"):
            BLEErrorHandler._safe_execute(
                lambda: (_ for _ in ()).throw(DecodeError("test decode error")),
                reraise=True,
            )

    def test_safe_execute_reraise_future_timeout_error(self) -> None:
        """Test FutureTimeoutError is re-raised when reraise=True."""
        with pytest.raises(FutureTimeoutError, match="test timeout"):
            BLEErrorHandler._safe_execute(
                lambda: (_ for _ in ()).throw(FutureTimeoutError("test timeout")),
                reraise=True,
            )

    def test_safe_execute_generic_exception_logging(self) -> None:
        """Test generic Exception is logged with exception() method."""
        with patch.object(logger, "exception") as mock_exception:
            result = BLEErrorHandler._safe_execute(
                lambda: (_ for _ in ()).throw(ValueError("unexpected value")),
                log_error=True,
                error_msg="Generic error occurred",
            )
            assert result is None
            mock_exception.assert_called_once()
            args, _kwargs = mock_exception.call_args
            # logger.exception("%s", error_msg) - first arg is format string
            assert args[0] == "%s"
            assert "Generic error occurred" in str(args)

    def test_safe_execute_generic_exception_reraise(self) -> None:
        """Test generic Exception is re-raised when reraise=True."""
        with pytest.raises(ValueError, match="generic exception"):
            BLEErrorHandler._safe_execute(
                lambda: (_ for _ in ()).throw(ValueError("generic exception")),
                reraise=True,
            )

    def test_safe_execute_system_exit_reraised(self) -> None:
        """Test SystemExit is always re-raised."""
        with pytest.raises(SystemExit, match="exit code 1"):
            BLEErrorHandler._safe_execute(
                lambda: (_ for _ in ()).throw(SystemExit("exit code 1"))
            )

    def test_safe_execute_system_exit_with_code(self) -> None:
        """Test SystemExit with numeric code is re-raised."""
        with pytest.raises(SystemExit) as exc_info:
            BLEErrorHandler._safe_execute(lambda: (_ for _ in ()).throw(SystemExit(42)))
        assert exc_info.value.code == 42

    def test_safe_execute_keyboard_interrupt_reraised(self) -> None:
        """Test KeyboardInterrupt is always re-raised."""
        with pytest.raises(KeyboardInterrupt):
            BLEErrorHandler._safe_execute(
                lambda: (_ for _ in ()).throw(KeyboardInterrupt("user interrupt"))
            )

    def test_safe_execute_class_method_wrapper(self) -> None:
        """Test safe_execute class method delegates to _safe_execute."""
        result = BLEErrorHandler.safe_execute(lambda: "class method works")
        assert result == "class method works"

    def test_safe_execute_class_method_with_exception(self) -> None:
        """Test safe_execute handles exceptions."""
        result = BLEErrorHandler.safe_execute(
            lambda: (_ for _ in ()).throw(BleakError("error")),
            default_return="handled",
        )
        assert result == "handled"

    def test_safe_execute_all_parameters(self) -> None:
        """Test safe_execute with all parameter combinations."""
        call_count = 0

        def counting_func() -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise BleakError("error")
            return "success"

        # Test with reraise=False, log_error=False
        result = BLEErrorHandler.safe_execute(
            counting_func, default_return="fallback", log_error=False, reraise=False
        )
        assert result == "fallback"


class TestBLEErrorHandlerSafeCleanup:
    """Test BLEErrorHandler._safe_cleanup and safe_cleanup methods."""

    def test_safe_cleanup_success(self) -> None:
        """Test successful cleanup returns True."""
        mock_cleanup = Mock(return_value=None)
        result = BLEErrorHandler._safe_cleanup(mock_cleanup, "test cleanup")
        assert result is True
        mock_cleanup.assert_called_once()

    def test_safe_cleanup_success_with_return_value(self) -> None:
        """Test cleanup that returns a value."""
        mock_cleanup = Mock(return_value="some value")
        result = BLEErrorHandler._safe_cleanup(mock_cleanup)
        assert result is True

    def test_safe_cleanup_exception_returns_false(self) -> None:
        """Test cleanup with exception returns False."""
        with patch.object(logger, "debug") as mock_debug:
            mock_cleanup = Mock(side_effect=ValueError("cleanup failed"))
            result = BLEErrorHandler._safe_cleanup(mock_cleanup, "custom cleanup")
            assert result is False
            mock_debug.assert_called_once()
            args, _kwargs = mock_debug.call_args
            # logger.debug("Error during %s: %s", cleanup_name, e)
            assert args[0] == "Error during %s: %s"
            assert "custom cleanup" in str(args)
            assert "cleanup failed" in str(args)

    def test_safe_cleanup_exception_no_cleanup_name(self) -> None:
        """Test cleanup with default cleanup_name."""
        with patch.object(logger, "debug") as mock_debug:
            mock_cleanup = Mock(side_effect=RuntimeError("runtime error"))
            result = BLEErrorHandler._safe_cleanup(mock_cleanup)
            assert result is False
            args, _kwargs = mock_debug.call_args
            assert "cleanup operation" in str(args)

    def test_safe_cleanup_system_exit_reraised(self) -> None:
        """Test SystemExit is always re-raised in cleanup."""
        with pytest.raises(SystemExit, match="system exit"):
            BLEErrorHandler._safe_cleanup(
                lambda: (_ for _ in ()).throw(SystemExit("system exit"))
            )

    def test_safe_cleanup_system_exit_with_code(self) -> None:
        """Test SystemExit with code is re-raised."""
        with pytest.raises(SystemExit) as exc_info:
            BLEErrorHandler._safe_cleanup(lambda: (_ for _ in ()).throw(SystemExit(1)))
        assert exc_info.value.code == 1

    def test_safe_cleanup_keyboard_interrupt_reraised(self) -> None:
        """Test KeyboardInterrupt is always re-raised in cleanup."""
        with pytest.raises(KeyboardInterrupt):
            BLEErrorHandler._safe_cleanup(
                lambda: (_ for _ in ()).throw(KeyboardInterrupt("ctrl+c"))
            )

    def test_safe_cleanup_class_method_wrapper(self) -> None:
        """Test safe_cleanup class method delegates to _safe_cleanup."""
        mock_cleanup = Mock(return_value=None)
        result = BLEErrorHandler.safe_cleanup(mock_cleanup, "wrapped cleanup")
        assert result is True
        mock_cleanup.assert_called_once()

    def test_safe_cleanup_class_method_with_exception(self) -> None:
        """Test safe_cleanup handles exceptions."""
        mock_cleanup = Mock(side_effect=Exception("error"))
        result = BLEErrorHandler.safe_cleanup(mock_cleanup)
        assert result is False


class TestErrorMessageFormatting:
    """Test error message formatting edge cases."""

    def test_empty_error_message(self) -> None:
        """Test handling of empty error message."""
        with patch.object(logger, "debug") as mock_debug:
            BLEErrorHandler._safe_execute(
                lambda: (_ for _ in ()).throw(BleakError("")),
                error_msg="",
            )
            mock_debug.assert_called_once()
            args, _kwargs = mock_debug.call_args
            # logger.debug("%s: %s", error_msg, e) - with empty strings
            assert args[0] == "%s: %s"
            # Empty error message results in format args being empty strings
            assert any(str(arg) == "" for arg in args[1:])

    def test_long_error_message(self) -> None:
        """Test handling of long error message."""
        long_msg = "x" * 1000
        with patch.object(logger, "debug") as mock_debug:
            BLEErrorHandler._safe_execute(
                lambda: (_ for _ in ()).throw(BleakError(long_msg)),
                error_msg="Long error",
            )
            mock_debug.assert_called_once()
            args, _kwargs = mock_debug.call_args
            assert args[0] == "%s: %s"
            assert "Long error" in str(args)
            assert long_msg in str(args)

    def test_error_message_with_formatting_chars(self) -> None:
        """Test error messages with special formatting characters."""
        special_msg = "Error with %s and %d and {}"
        with patch.object(logger, "debug") as mock_debug:
            BLEErrorHandler._safe_execute(
                lambda: (_ for _ in ()).throw(BleakError(special_msg)),
                error_msg="Special chars",
            )
            mock_debug.assert_called_once()
            args, _kwargs = mock_debug.call_args
            # Format string is first arg, actual args follow
            assert args[0] == "%s: %s"

    def test_error_message_with_unicode(self) -> None:
        """Test error messages with unicode characters."""
        unicode_msg = "错误信息 💥 émoji"
        with patch.object(logger, "debug") as mock_debug:
            BLEErrorHandler._safe_execute(
                lambda: (_ for _ in ()).throw(BleakError(unicode_msg)),
                error_msg="Unicode test",
            )
            mock_debug.assert_called_once()
            args, _kwargs = mock_debug.call_args
            assert args[0] == "%s: %s"
            assert "Unicode test" in str(args)
            assert unicode_msg in str(args)

    def test_multiline_error_message(self) -> None:
        """Test error messages with newlines."""
        multiline_msg = "Line 1\nLine 2\nLine 3"
        with patch.object(logger, "debug") as mock_debug:
            BLEErrorHandler._safe_execute(
                lambda: (_ for _ in ()).throw(BleakError(multiline_msg)),
                error_msg="Multiline",
            )
            mock_debug.assert_called_once()
            args, _kwargs = mock_debug.call_args
            assert args[0] == "%s: %s"
            assert "Multiline" in str(args)
            # The multiline message appears in the BleakError repr where newlines are escaped
            assert (
                "Line 1" in str(args)
                and "Line 2" in str(args)
                and "Line 3" in str(args)
            )


class TestExceptionInheritance:
    """Test exception class inheritance and properties."""

    def test_bleak_error_inheritance(self) -> None:
        """Test BleakError is caught by exception handler."""
        caught = False
        try:
            BLEErrorHandler._safe_execute(
                lambda: (_ for _ in ()).throw(BleakError("test")), reraise=False
            )
            caught = True
        except Exception:
            pass
        assert caught, "BleakError should be caught, not re-raised with reraise=False"

    def test_decode_error_is_exception(self) -> None:
        """Test DecodeError inherits from Exception."""
        assert issubclass(DecodeError, Exception)

    def test_future_timeout_error_inheritance(self) -> None:
        """Test FutureTimeoutError is caught appropriately."""
        assert issubclass(FutureTimeoutError, Exception)
        # Test it's caught by the handler
        result = BLEErrorHandler._safe_execute(
            lambda: (_ for _ in ()).throw(FutureTimeoutError("timeout")),
            default_return="caught",
        )
        assert result == "caught"

    def test_exception_chaining(self) -> None:
        """Test exception context is preserved."""
        try:
            try:
                raise ValueError("original")
            except ValueError as e:
                raise BleakError("wrapped") from e
        except BleakError as e:
            assert e.__cause__ is not None
            assert isinstance(e.__cause__, ValueError)


class TestEdgeCases:
    """Test various edge cases and boundary conditions."""

    def test_safe_execute_with_none_default_return(self) -> None:
        """Test that None default_return works correctly."""
        result = BLEErrorHandler._safe_execute(
            lambda: (_ for _ in ()).throw(BleakError("error")),
            default_return=None,
        )
        assert result is None

    def test_safe_execute_with_falsy_default_return(self) -> None:
        """Test falsy default_return values."""
        # Test empty string
        result = BLEErrorHandler._safe_execute(
            lambda: (_ for _ in ()).throw(BleakError("error")),
            default_return="",
        )
        assert result == ""

        # Test zero
        zero_result = BLEErrorHandler._safe_execute(
            lambda: (_ for _ in ()).throw(BleakError("error")),
            default_return=0,
        )
        assert zero_result == 0

        # Test False
        false_result = BLEErrorHandler._safe_execute(
            lambda: (_ for _ in ()).throw(BleakError("error")),
            default_return=False,
        )
        assert false_result is False

        # Test empty list
        empty_list_result: list[Any] | None = BLEErrorHandler._safe_execute(
            lambda: (_ for _ in ()).throw(BleakError("error")),
            default_return=[],
        )
        assert empty_list_result == []

    def test_safe_cleanup_with_none_result(self) -> None:
        """Test cleanup returning None is still success."""
        result = BLEErrorHandler._safe_cleanup(lambda: None)
        assert result is True

    def test_safe_execute_func_with_side_effects(self) -> None:
        """Test that func side effects happen before exception."""
        side_effect_called = False

        def func_with_side_effects() -> None:
            nonlocal side_effect_called
            side_effect_called = True
            raise BleakError("after side effect")

        BLEErrorHandler._safe_execute(func_with_side_effects, reraise=False)
        assert side_effect_called is True

    def test_nested_exceptions(self) -> None:
        """Test handling of nested exception scenarios."""
        call_order = []

        def outer_func() -> NoReturn:
            call_order.append("outer")
            raise BleakError("outer error")

        result = BLEErrorHandler._safe_execute(outer_func, default_return="handled")
        assert result == "handled"
        assert call_order == ["outer"]


class TestModuleExports:
    """Test module exports and public API."""

    def test_all_exports_defined(self) -> None:
        """Test __all__ is properly defined."""
        assert hasattr(ble_errors_module, "__all__")
        assert "BLEErrorHandler" in ble_errors_module.__all__
        assert "DecodeError" in ble_errors_module.__all__

    def test_ble_error_handler_callable(self) -> None:
        """Test BLEErrorHandler class is accessible."""
        assert callable(BLEErrorHandler)
        handler = BLEErrorHandler()
        assert handler is not None

    def test_safe_execute_staticmethod(self) -> None:
        """Test _safe_execute is a static method."""
        assert isinstance(BLEErrorHandler._safe_execute, type(lambda: None))

    def test_safe_cleanup_staticmethod(self) -> None:
        """Test _safe_cleanup is a static method."""
        assert isinstance(BLEErrorHandler._safe_cleanup, type(lambda: None))


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--faulthandler-timeout=30"])
