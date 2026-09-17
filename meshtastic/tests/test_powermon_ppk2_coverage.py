"""Extended test coverage for PPK2 power monitor hardware interface.

This module tests device detection, serial communication error handling,
measurement reading edge cases, initialization failures, timeout handling,
and hardware state transitions.
"""

import math
import threading
from typing import Any, Generator, Type
from unittest.mock import MagicMock, patch

import pytest

try:
    from meshtastic.powermon.constants import (
        MICROAMPS_PER_MILLIAMP,
    )
    from meshtastic.powermon.power_supply import PowerError
    from meshtastic.powermon.ppk2 import (
        INITIAL_POLL_TIMEOUT_S,
        READ_ERROR_RETRY_DELAY_S,
        STABILIZATION_DELAY_S,
        SUBSEQUENT_POLL_TIMEOUT_S,
        PPK2PowerSupply,
    )
except ImportError:
    pytest.skip("Can't import PPK2 modules", allow_module_level=True)


@pytest.fixture
def mock_ppk2_api_class() -> Generator[type[Any], None, None]:
    """Provide a mock PPK2_API class for constructor tests."""

    class MockPPK2API:
        """Mock implementation of PPK2_API for testing."""

        _list_devices_result: list[Any] | None = []

        def __init__(self, port_name: str):
            """Initialize mock API with port name."""
            self.port_name = port_name
            self.ser = MagicMock()
            self.modifiers_called = False

        def get_modifiers(self) -> None:
            """Mock get_modifiers method."""
            self.modifiers_called = True

        def get_data(self) -> bytes:
            """Mock get_data method."""
            return b""

        def get_samples(self, _data: bytes) -> tuple[list[int], int]:
            """Mock get_samples method."""
            return ([], 0)

        def start_measuring(self) -> None:
            """Mock start_measuring method."""

        def stop_measuring(self) -> None:
            """Mock stop_measuring method."""

        def use_source_meter(self) -> None:
            """Mock use_source_meter method."""

        def use_ampere_meter(self) -> None:
            """Mock use_ampere_meter method."""

        def set_source_voltage(self, _mv: int) -> None:
            """Mock set_source_voltage method."""

        def toggle_DUT_power(self, _state: str) -> None:
            """Mock toggle_DUT_power method."""

        def close(self) -> None:
            """Mock close method."""

        @classmethod
        def list_devices(cls) -> list[Any] | None:
            """Mock list_devices classmethod."""
            return cls._list_devices_result

    yield MockPPK2API


# =============================================================================
# Device Detection and Selection Tests
# =============================================================================


@pytest.mark.unit
def test_constructor_no_devices_found_raises_error(
    monkeypatch: pytest.MonkeyPatch,
    mock_ppk2_api_class: type[Any],  # noqa
) -> None:
    """PPK2PowerSupply should raise PowerError when no devices found."""

    mock_ppk2_api_class._list_devices_result = []

    monkeypatch.setattr(
        "meshtastic.powermon.ppk2.ppk2_api.PPK2_API",
        mock_ppk2_api_class,
    )

    with pytest.raises(PowerError, match="No PPK2 devices found"):
        PPK2PowerSupply(portName=None)


@pytest.mark.unit
def test_constructor_multiple_devices_raises_error(
    monkeypatch: pytest.MonkeyPatch,
    mock_ppk2_api_class: type[Any],  # noqa
) -> None:
    """PPK2PowerSupply should raise PowerError when multiple devices found."""

    mock_ppk2_api_class._list_devices_result = ["/dev/ttyUSB0", "/dev/ttyUSB1"]

    monkeypatch.setattr(
        "meshtastic.powermon.ppk2.ppk2_api.PPK2_API",
        mock_ppk2_api_class,
    )

    with pytest.raises(PowerError, match="Multiple PPK2 devices found"):
        PPK2PowerSupply(portName=None)


@pytest.mark.unit
def test_constructor_auto_selects_single_device(
    monkeypatch: pytest.MonkeyPatch,
    mock_ppk2_api_class: type[Any],  # noqa
) -> None:
    """PPK2PowerSupply should auto-select when exactly one device found."""

    mock_ppk2_api_class._list_devices_result = ["/dev/ttyACM0"]

    monkeypatch.setattr(
        "meshtastic.powermon.ppk2.ppk2_api.PPK2_API",
        mock_ppk2_api_class,
    )

    ppk = PPK2PowerSupply(portName=None)
    assert ppk.r.port_name == "/dev/ttyACM0"  # type: ignore[attr-defined]
    ppk.close()


@pytest.mark.unit
def test_constructor_with_explicit_port_name(
    monkeypatch: pytest.MonkeyPatch,
    mock_ppk2_api_class: type[Any],  # noqa
) -> None:
    """PPK2PowerSupply should use provided portName without auto-discovery."""

    monkeypatch.setattr(
        "meshtastic.powermon.ppk2.ppk2_api.PPK2_API",
        mock_ppk2_api_class,
    )

    ppk = PPK2PowerSupply(portName="/dev/ttyUSB0")
    assert ppk.r.port_name == "/dev/ttyUSB0"  # type: ignore[attr-defined]
    ppk.close()


@pytest.mark.unit
def test_constructor_skips_auto_discovery_with_port_name(
    monkeypatch: pytest.MonkeyPatch,  # noqa: ARG001
    mock_ppk2_api_class: type[Any],  # noqa
) -> None:
    """PPK2PowerSupply should not call list_devices when portName is provided."""

    mock_ppk2_api_class._list_devices_result = []
    list_devices_called = [False]

    original_list_devices = mock_ppk2_api_class.list_devices

    def mock_list_devices(_cls: type[Any]) -> list[str]:
        list_devices_called[0] = True
        return original_list_devices()

    mock_ppk2_api_class.list_devices = classmethod(mock_list_devices)

    monkeypatch.setattr(
        "meshtastic.powermon.ppk2.ppk2_api.PPK2_API",
        mock_ppk2_api_class,
    )

    ppk = PPK2PowerSupply(portName="COM3")
    assert not list_devices_called[0]
    ppk.close()


# =============================================================================
# list_devices() Shape Contract Tests (ppk2-api 0.9.2 vs unreleased master)
# =============================================================================


@pytest.mark.unit
@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("/dev/ttyACM0", "/dev/ttyACM0"),
        ("", None),
        (("/dev/ttyACM0", "PPK2-serial-1"), "/dev/ttyACM0"),
        (["/dev/ttyACM0", "PPK2-serial-1"], "/dev/ttyACM0"),
        (("/dev/ttyACM0",), "/dev/ttyACM0"),
        (("", "PPK2-serial-1"), None),
        ((), None),
        ((123, "PPK2-serial-1"), None),
        (None, None),
        (123, None),
    ],
)
def test_ppk2_port_from_entry_normalizes_supported_shapes(
    entry: Any, expected: str | None
) -> None:
    """Both the 0.9.2 string shape and upstream's tuple shape must resolve."""
    from meshtastic.powermon.ppk2 import (  # pylint: disable=import-outside-toplevel
        _ppk2_port_from_entry,
    )

    assert _ppk2_port_from_entry(entry) == expected


@pytest.mark.unit
def test_constructor_treats_none_discovery_result_as_no_devices(
    monkeypatch: pytest.MonkeyPatch,
    mock_ppk2_api_class: type[Any],  # noqa
) -> None:
    """A defensive None result should retain the historical no-device error."""
    mock_ppk2_api_class._list_devices_result = None
    monkeypatch.setattr(
        "meshtastic.powermon.ppk2.ppk2_api.PPK2_API",
        mock_ppk2_api_class,
    )

    with pytest.raises(PowerError, match="No PPK2 devices found"):
        PPK2PowerSupply(portName=None)


@pytest.mark.unit
def test_constructor_deduplicates_same_normalized_port(
    monkeypatch: pytest.MonkeyPatch,
    mock_ppk2_api_class: type[Any],  # noqa
) -> None:
    """Duplicate old/new-shape entries for one port should represent one device."""
    mock_ppk2_api_class._list_devices_result = [
        "/dev/ttyACM0",
        ("/dev/ttyACM0", "PPK2-serial-1"),
    ]
    monkeypatch.setattr(
        "meshtastic.powermon.ppk2.ppk2_api.PPK2_API",
        mock_ppk2_api_class,
    )

    ppk = PPK2PowerSupply(portName=None)
    assert ppk.r.port_name == "/dev/ttyACM0"  # type: ignore[attr-defined]
    ppk.close()


@pytest.mark.unit
def test_constructor_auto_selects_single_tuple_device(
    monkeypatch: pytest.MonkeyPatch,
    mock_ppk2_api_class: type[Any],  # noqa
) -> None:
    """A single (port, serial) tuple entry should auto-select its port."""

    mock_ppk2_api_class._list_devices_result = [("/dev/ttyACM0", "PPK2-serial-1")]

    monkeypatch.setattr(
        "meshtastic.powermon.ppk2.ppk2_api.PPK2_API",
        mock_ppk2_api_class,
    )

    ppk = PPK2PowerSupply(portName=None)
    assert ppk.r.port_name == "/dev/ttyACM0"  # type: ignore[attr-defined]
    ppk.close()


@pytest.mark.unit
def test_constructor_multiple_tuple_devices_raises_error(
    monkeypatch: pytest.MonkeyPatch,
    mock_ppk2_api_class: type[Any],  # noqa
) -> None:
    """Multiple tuple entries should raise the same ambiguity error as strings."""

    mock_ppk2_api_class._list_devices_result = [
        ("/dev/ttyUSB0", "PPK2-serial-1"),
        ("/dev/ttyUSB1", "PPK2-serial-2"),
    ]

    monkeypatch.setattr(
        "meshtastic.powermon.ppk2.ppk2_api.PPK2_API",
        mock_ppk2_api_class,
    )

    with pytest.raises(PowerError, match="Multiple PPK2 devices found"):
        PPK2PowerSupply(portName=None)


@pytest.mark.unit
def test_constructor_ignores_unusable_entries(
    monkeypatch: pytest.MonkeyPatch,
    mock_ppk2_api_class: type[Any],  # noqa
) -> None:
    """Entries matching neither supported shape should not become port names."""

    mock_ppk2_api_class._list_devices_result = [(123, "junk"), None, 42]

    monkeypatch.setattr(
        "meshtastic.powermon.ppk2.ppk2_api.PPK2_API",
        mock_ppk2_api_class,
    )

    with pytest.raises(PowerError, match="No PPK2 devices found"):
        PPK2PowerSupply(portName=None)


@pytest.mark.unit
def test_constructor_mixed_shapes_select_single_valid_port(
    monkeypatch: pytest.MonkeyPatch,
    mock_ppk2_api_class: type[Any],  # noqa
) -> None:
    """A mix of old and new shapes should still count devices consistently."""

    mock_ppk2_api_class._list_devices_result = [
        (123, "junk"),
        ("/dev/ttyACM0", "PPK2-serial-1"),
    ]

    monkeypatch.setattr(
        "meshtastic.powermon.ppk2.ppk2_api.PPK2_API",
        mock_ppk2_api_class,
    )

    ppk = PPK2PowerSupply(portName=None)
    assert ppk.r.port_name == "/dev/ttyACM0"  # type: ignore[attr-defined]
    ppk.close()


# =============================================================================
# Serial Communication Error Handling Tests
# =============================================================================


@pytest.mark.unit
def test_measurement_loop_handles_empty_data(
    monkeypatch: pytest.MonkeyPatch,  # noqa: ARG001
    ppk2_stub: "PPK2PowerSupply",  # noqa: ARG001
) -> None:
    """Measurement loop should handle empty data reads gracefully."""

    ppk = ppk2_stub
    ppk.r = MagicMock()
    ppk.r.get_data.return_value = b""
    ppk.r.get_samples.return_value = ([], 0)

    ppk.measuring = True

    # Run one iteration
    with ppk._want_measurement:
        ppk._want_measurement.wait(timeout=0.1)

    ppk.measuring = False

    # No samples should be recorded
    assert ppk.current_num_samples == 0


@pytest.mark.unit
def test_measurement_loop_handles_exception_in_get_data(
    monkeypatch: pytest.MonkeyPatch,
    ppk2_stub: "PPK2PowerSupply",
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Measurement loop should log and retry on get_data() exception."""

    ppk = ppk2_stub
    ppk.r = MagicMock()

    call_count = 0

    def side_effect() -> bytes:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise SerialException("Serial read error")
        ppk.measuring = False
        return b""

    ppk.r.get_data.side_effect = side_effect

    ppk.measuring = True

    # Fast-forward sleep
    monkeypatch.setattr("meshtastic.powermon.ppk2.time.sleep", lambda _: None)

    # Run one iteration (should catch exception and continue)
    ppk._measurement_loop()

    # Should have logged the error
    assert "PPK2 read loop error" in caplog.text
    assert "Serial read error" in caplog.text


@pytest.mark.unit
def test_measurement_loop_handles_exception_in_get_samples(
    monkeypatch: pytest.MonkeyPatch,
    ppk2_stub: "PPK2PowerSupply",
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Measurement loop should handle exception in get_samples()."""

    ppk = ppk2_stub
    ppk.r = MagicMock()
    ppk.r.get_data.return_value = b"some_data"

    call_count = 0

    def samples_side_effect(data: bytes) -> tuple[list[int], int]:  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ValueError("Invalid sample data")
        ppk.measuring = False
        return ([], 0)

    ppk.r.get_samples.side_effect = samples_side_effect

    ppk.measuring = True
    monkeypatch.setattr("meshtastic.powermon.ppk2.time.sleep", lambda _: None)

    # Run one iteration
    ppk._measurement_loop()

    # Should have logged the error
    assert "PPK2 read loop error" in caplog.text
    assert "Invalid sample data" in caplog.text


@pytest.mark.unit
def test_measurement_loop_stops_on_measuring_false_after_exception(
    monkeypatch: pytest.MonkeyPatch,
    ppk2_stub: "PPK2PowerSupply",
) -> None:
    """Measurement loop should exit when measuring becomes False."""

    ppk = ppk2_stub
    ppk.r = MagicMock()

    call_count = 0

    def raise_then_exit() -> bytes:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise SerialException("Serial error")
        # Simulate measuring becoming False
        ppk.measuring = False
        return b""

    ppk.r.get_data.side_effect = raise_then_exit
    ppk.measuring = True

    # Fast-forward sleep
    monkeypatch.setattr("meshtastic.powermon.ppk2.time.sleep", lambda _: None)

    # Run loop (should exit cleanly)
    ppk._measurement_loop()

    assert call_count >= 1


@pytest.mark.unit
def test_measurement_loop_exits_when_measuring_false_during_error(
    ppk2_stub: "PPK2PowerSupply",
) -> None:
    """Measurement loop should not log error if measuring is False during exception."""

    ppk = ppk2_stub
    ppk.r = MagicMock()

    def raise_then_check_measuring() -> bytes:
        ppk.measuring = False
        raise SerialException("Serial error")

    ppk.r.get_data.side_effect = raise_then_check_measuring
    ppk.measuring = True

    # Should exit cleanly without logging
    ppk._measurement_loop()

    # Test passes if we get here without exception


# =============================================================================
# Measurement Reading Edge Cases
# =============================================================================


@pytest.mark.unit
def test_measurement_loop_processes_single_sample(
    monkeypatch: pytest.MonkeyPatch,  # noqa: ARG001
    ppk2_stub: "PPK2PowerSupply",  # noqa: ARG001
) -> None:
    """Measurement loop should correctly process single sample."""

    ppk = ppk2_stub
    ppk.r = MagicMock()

    call_count = 0

    def get_data_side_effect() -> bytes:
        nonlocal call_count
        call_count += 1
        # Return data on first call, then set measuring False for next iteration
        if call_count == 1:
            return b"data"
        else:
            ppk.measuring = False
            return b""

    ppk.r.get_data.side_effect = get_data_side_effect
    ppk.r.get_samples.return_value = ([5000], 0)  # 5000 microamps

    ppk.measuring = True

    # Run loop
    ppk._measurement_loop()

    # Verify sample was recorded
    assert ppk.current_num_samples == 1
    assert ppk.current_sum == 5000
    assert ppk.current_min == 5000
    assert ppk.current_max == 5000


@pytest.mark.unit
def test_measurement_loop_processes_multiple_samples(
    monkeypatch: pytest.MonkeyPatch,  # noqa: ARG001
    ppk2_stub: "PPK2PowerSupply",  # noqa: ARG001
) -> None:
    """Measurement loop should correctly aggregate multiple samples."""

    ppk = ppk2_stub
    ppk.r = MagicMock()

    call_count = 0

    def get_data_side_effect() -> bytes:
        nonlocal call_count
        call_count += 1
        # Return data on first call only
        if call_count == 1:
            return b"data"
        else:
            ppk.measuring = False
            return b""

    ppk.r.get_data.side_effect = get_data_side_effect
    # Multiple samples: min=1000, max=9000, sum=25000, count=5
    ppk.r.get_samples.return_value = ([1000, 3000, 5000, 7000, 9000], 0)

    ppk.measuring = True

    ppk._measurement_loop()

    # Verify aggregation
    assert ppk.current_num_samples == 5
    assert ppk.current_sum == 25000
    assert ppk.current_min == 1000
    assert ppk.current_max == 9000


@pytest.mark.unit
def test_measurement_loop_aggregates_across_multiple_reads(
    monkeypatch: pytest.MonkeyPatch,  # noqa: ARG001
    ppk2_stub: "PPK2PowerSupply",  # noqa: ARG001
) -> None:
    """Measurement loop should aggregate samples across multiple data reads."""

    ppk = ppk2_stub
    ppk.r = MagicMock()

    call_count = 0

    def get_samples_side_effect(data: bytes) -> tuple[list[int], int]:  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ([1000, 2000], 0)
        elif call_count == 2:
            return ([3000, 4000], 0)
        ppk.measuring = False
        return ([], 0)

    ppk.r.get_data.return_value = b"data"
    ppk.r.get_samples.side_effect = get_samples_side_effect

    ppk.measuring = True

    # Run loop for a few iterations
    ppk._measurement_loop()

    # Verify aggregation across reads
    assert ppk.current_num_samples == 4
    assert ppk.current_sum == 10000
    assert ppk.current_min == 1000
    assert ppk.current_max == 4000


@pytest.mark.unit
def test_measurement_loop_updates_min_max_correctly(
    monkeypatch: pytest.MonkeyPatch,  # noqa: ARG001
    ppk2_stub: "PPK2PowerSupply",  # noqa: ARG001
) -> None:
    """Measurement loop should correctly update min/max across batches."""

    ppk = ppk2_stub
    ppk.r = MagicMock()

    call_count = 0

    def get_samples_side_effect(data: bytes) -> tuple[list[int], int]:  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ([5000, 6000, 7000], 0)  # min=5000, max=7000
        elif call_count == 2:
            return ([3000, 8000], 0)  # new min=3000, new max=8000
        ppk.measuring = False
        return ([], 0)

    ppk.r.get_data.return_value = b"data"
    ppk.r.get_samples.side_effect = get_samples_side_effect

    ppk.measuring = True

    ppk._measurement_loop()

    # Verify min/max were updated correctly
    assert ppk.current_min == 3000
    assert ppk.current_max == 8000
    assert ppk.current_num_samples == 5


@pytest.mark.unit
def test_measurement_loop_tracks_data_read_statistics(
    monkeypatch: pytest.MonkeyPatch,  # noqa: ARG001
    ppk2_stub: "PPK2PowerSupply",  # noqa: ARG001
) -> None:
    """Measurement loop should track data length statistics."""

    ppk = ppk2_stub
    ppk.r = MagicMock()

    call_count = 0

    def get_data_side_effect() -> bytes:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return b"a" * 100
        elif call_count == 2:
            return b"b" * 200
        else:
            ppk.measuring = False
            return b""

    ppk.r.get_data.side_effect = get_data_side_effect
    ppk.r.get_samples.return_value = ([1000], 0)

    ppk.measuring = True

    ppk._measurement_loop()

    # Verify tracking statistics - 3 reads total (100 + 200 + 0 empty bytes)
    # The empty read still counts in statistics
    assert ppk.num_data_reads == 3
    assert ppk.total_data_len == 300
    assert ppk.max_data_len == 200


@pytest.mark.unit
def test_get_average_returns_nan_with_no_samples(
    ppk2_stub: "PPK2PowerSupply",
) -> None:
    """GetAverageCurrentMA should return NaN when no samples exist."""

    ppk = ppk2_stub
    ppk.current_num_samples = 0
    ppk.current_average = math.nan

    result = ppk.getAverageCurrentMA()
    assert math.isnan(result)


@pytest.mark.unit
def test_get_average_calculates_correctly_with_samples(
    ppk2_stub: "PPK2PowerSupply",
) -> None:
    """GetAverageCurrentMA should calculate correct average with samples."""

    ppk = ppk2_stub
    ppk.current_num_samples = 4
    ppk.current_sum = 10000  # 10000 microamps = 10mA
    ppk.current_average = math.nan

    result = ppk.getAverageCurrentMA()
    expected = (10000 / 4) / MICROAMPS_PER_MILLIAMP  # 2500 microamps = 2.5mA
    assert result == pytest.approx(expected)


@pytest.mark.unit
def test_get_average_uses_cached_value_with_no_new_samples(
    ppk2_stub: "PPK2PowerSupply",
) -> None:
    """GetAverageCurrentMA should return cached average when no new samples."""

    ppk = ppk2_stub
    ppk.current_num_samples = 0
    ppk.current_average = 5.0  # Previously calculated

    result = ppk.getAverageCurrentMA()
    assert result == pytest.approx(5.0 / MICROAMPS_PER_MILLIAMP)


@pytest.mark.unit
def test_get_min_returns_last_reported_with_no_samples(
    ppk2_stub: "PPK2PowerSupply",
) -> None:
    """GetMinCurrentMA should return last_reported_min when no new samples."""

    ppk = ppk2_stub
    ppk.current_num_samples = 0
    ppk.last_reported_min = 3000.0  # 3000 microamps

    result = ppk.getMinCurrentMA()
    assert result == pytest.approx(3000.0 / MICROAMPS_PER_MILLIAMP)


@pytest.mark.unit
def test_get_max_returns_last_reported_with_no_samples(
    ppk2_stub: "PPK2PowerSupply",
) -> None:
    """GetMaxCurrentMA should return last_reported_max when no new samples."""

    ppk = ppk2_stub
    ppk.current_num_samples = 0
    ppk.last_reported_max = 7000.0  # 7000 microamps

    result = ppk.getMaxCurrentMA()
    assert result == pytest.approx(7000.0 / MICROAMPS_PER_MILLIAMP)


# =============================================================================
# Device Initialization Failures
# =============================================================================


@pytest.mark.unit
def test_constructor_api_init_failure_raises_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PPK2PowerSupply should propagate API initialization errors."""

    def mock_api_init(port_name: str):
        raise SerialException("Failed to open port")

    monkeypatch.setattr(
        "meshtastic.powermon.ppk2.ppk2_api.PPK2_API",
        mock_api_init,
    )

    with pytest.raises(SerialException, match="Failed to open port"):
        PPK2PowerSupply(portName="/dev/ttyACM0")


@pytest.mark.unit
def test_constructor_get_modifiers_failure_raises_error(
    monkeypatch: pytest.MonkeyPatch,  # noqa: ARG001
    mock_ppk2_api_class: Type[Any],  # noqa
) -> None:
    """PPK2PowerSupply should propagate get_modifiers() errors."""

    class FailingAPI(mock_ppk2_api_class):
        def get_modifiers(self) -> None:
            raise RuntimeError("Modifiers read failed")

    monkeypatch.setattr(
        "meshtastic.powermon.ppk2.ppk2_api.PPK2_API",
        FailingAPI,
    )

    with pytest.raises(RuntimeError, match="Modifiers read failed"):
        PPK2PowerSupply(portName="/dev/ttyACM0")


@pytest.mark.unit
def test_close_handles_missing_close_method(
    ppk2_stub: "PPK2PowerSupply",
) -> None:
    """close() should handle case where API has no close() method."""

    ppk = ppk2_stub
    ppk.r = MagicMock()
    # Remove close method
    delattr(ppk.r, "close")
    ppk.r.ser = MagicMock()
    ppk.measurement_thread = MagicMock()
    ppk.measurement_thread.is_alive.return_value = False

    # Should not raise
    ppk.close()

    # stop_measuring should still be called
    ppk.r.stop_measuring.assert_called_once()


@pytest.mark.unit
def test_close_handles_missing_ser_attribute(
    ppk2_stub: "PPK2PowerSupply",
) -> None:
    """close() should handle case where API has no ser attribute."""

    ppk = ppk2_stub
    ppk.r = MagicMock()
    # Remove ser attribute
    delattr(ppk.r, "ser")
    ppk.measurement_thread = MagicMock()
    ppk.measurement_thread.is_alive.return_value = False

    # Should not raise
    ppk.close()

    # API close should still be called
    ppk.r.close.assert_called_once()


@pytest.mark.unit
def test_close_handles_serial_close_exception(
    ppk2_stub: "PPK2PowerSupply",
) -> None:
    """close() should suppress exceptions during serial close."""

    ppk = ppk2_stub
    ppk.r = MagicMock()
    ppk.r.ser = MagicMock()
    ppk.r.ser.close.side_effect = SerialException("Serial close failed")
    ppk.measurement_thread = MagicMock()
    ppk.measurement_thread.is_alive.return_value = False

    # Should not raise
    ppk.close()


@pytest.mark.unit
def test_close_handles_stop_measuring_exception(
    ppk2_stub: "PPK2PowerSupply",
) -> None:
    """close() should suppress exceptions during stop_measuring."""

    ppk = ppk2_stub
    ppk.r = MagicMock()
    ppk.r.stop_measuring.side_effect = RuntimeError("Stop failed")
    ppk.r.ser = MagicMock()
    ppk.measurement_thread = MagicMock()
    ppk.measurement_thread.is_alive.return_value = False

    # Should not raise
    ppk.close()


@pytest.mark.unit
def test_close_handles_thread_join_timeout(
    monkeypatch: pytest.MonkeyPatch,  # noqa: ARG001
    ppk2_stub: "PPK2PowerSupply",
    caplog: pytest.LogCaptureFixture,
) -> None:
    """close() should log warning when thread join times out."""

    ppk = ppk2_stub
    ppk.r = MagicMock()
    ppk.measurement_thread = MagicMock()
    ppk.measurement_thread.is_alive.return_value = True
    ppk.measurement_thread.join.return_value = None

    ppk.measuring = True
    ppk.close()

    # Should have logged warning
    assert "did not stop within timeout" in caplog.text


# =============================================================================
# Command/Response Timeout Handling
# =============================================================================


@pytest.mark.unit
def test_measurement_loop_uses_initial_timeout_on_first_read(
    monkeypatch: pytest.MonkeyPatch, ppk2_stub: "PPK2PowerSupply"
) -> None:
    """Measurement loop should use INITIAL_POLL_TIMEOUT_S on first read."""

    ppk = ppk2_stub
    ppk.r = MagicMock()

    call_count = 0
    timeouts_used: list[float] = []

    def get_data_side_effect() -> bytes:
        nonlocal call_count
        call_count += 1
        if call_count > 1:
            ppk.measuring = False
        return b"data"

    ppk.r.get_data.side_effect = get_data_side_effect
    ppk.r.get_samples.return_value = ([1000], 0)

    def mock_wait(_self, timeout: float | None = None) -> bool:
        timeouts_used.append(timeout if timeout is not None else 0)
        return True

    monkeypatch.setattr(threading.Condition, "wait", mock_wait)

    ppk.measuring = True
    ppk.num_data_reads = 0

    # Run the measurement loop
    ppk._measurement_loop()

    # The timeout should be INITIAL_POLL_TIMEOUT_S on first call
    assert any(t == INITIAL_POLL_TIMEOUT_S for t in timeouts_used)


@pytest.mark.unit
def test_measurement_loop_uses_subsequent_timeout_on_later_reads(
    monkeypatch: pytest.MonkeyPatch, ppk2_stub: "PPK2PowerSupply"
) -> None:
    """Measurement loop should use SUBSEQUENT_POLL_TIMEOUT_S after first read."""

    ppk = ppk2_stub
    ppk.r = MagicMock()

    call_count = 0

    def get_data_side_effect() -> bytes:
        nonlocal call_count
        call_count += 1
        if call_count > 2:
            ppk.measuring = False
        return b"data"

    ppk.r.get_data.side_effect = get_data_side_effect
    ppk.r.get_samples.return_value = ([1000], 0)

    timeouts_used: list[float] = []

    def mock_wait(_self, timeout: float | None = None) -> bool:
        timeouts_used.append(timeout if timeout is not None else 0)
        return True

    monkeypatch.setattr(threading.Condition, "wait", mock_wait)

    ppk.measuring = True
    ppk.num_data_reads = 1  # Simulate previous reads

    ppk._measurement_loop()

    # After first read, should use SUBSEQUENT_POLL_TIMEOUT_S
    assert any(t == SUBSEQUENT_POLL_TIMEOUT_S for t in timeouts_used)


@pytest.mark.unit
def test_measurement_loop_sleeps_after_read_error(
    monkeypatch: pytest.MonkeyPatch, ppk2_stub: "PPK2PowerSupply"
) -> None:
    """Measurement loop should sleep READ_ERROR_RETRY_DELAY_S after error."""

    ppk = ppk2_stub
    ppk.r = MagicMock()
    ppk.r.get_data.side_effect = SerialException("Serial error")

    sleep_called = False
    sleep_duration = 0.0

    def mock_sleep(duration: float) -> None:
        nonlocal sleep_called, sleep_duration
        sleep_called = True
        sleep_duration = duration
        ppk.measuring = False  # Stop after one iteration

    monkeypatch.setattr("meshtastic.powermon.ppk2.time.sleep", mock_sleep)

    ppk.measuring = True
    ppk._measurement_loop()

    assert sleep_called
    assert sleep_duration == READ_ERROR_RETRY_DELAY_S


# =============================================================================
# Hardware State Transition Tests
# =============================================================================


@pytest.mark.unit
def test_setIsSupply_validates_voltage_before_supply_mode(
    ppk2_stub: "PPK2PowerSupply",
) -> None:
    """SetIsSupply should raise PowerError when voltage < MIN_SUPPLY_VOLTAGE_V in supply mode."""

    ppk = ppk2_stub
    ppk.v = 0.5  # Below minimum
    ppk.r = MagicMock()

    with pytest.raises(PowerError, match="Supply voltage must be set to at least"):
        ppk.setIsSupply(is_supply=True)


@pytest.mark.unit
def test_setIsSupply_allows_zero_voltage_in_meter_mode(
    monkeypatch: pytest.MonkeyPatch, ppk2_stub: "PPK2PowerSupply"
) -> None:
    """SetIsSupply should allow zero voltage in amp meter mode."""

    ppk = ppk2_stub
    ppk.v = 0.0
    ppk.r = MagicMock()
    ppk.measurement_thread = MagicMock()
    ppk.measurement_thread.is_alive.return_value = True
    ppk.resetMeasurements = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr("meshtastic.powermon.ppk2.time.sleep", lambda _: None)

    # Should not raise
    ppk.setIsSupply(is_supply=False)


@pytest.mark.unit
def test_setIsSupply_sets_voltage_before_mode(
    monkeypatch: pytest.MonkeyPatch, ppk2_stub: "PPK2PowerSupply"
) -> None:
    """SetIsSupply should call set_source_voltage before setting mode."""

    ppk = ppk2_stub
    ppk.v = 3.3
    ppk.r = MagicMock()
    ppk.measurement_thread = MagicMock()
    ppk.measurement_thread.is_alive.return_value = True
    ppk.resetMeasurements = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr("meshtastic.powermon.ppk2.time.sleep", lambda _: None)

    ppk.setIsSupply(is_supply=True)

    # Voltage should be set in mV (3.3V = 3300mV)
    ppk.r.set_source_voltage.assert_called_once_with(3300)


@pytest.mark.unit
def test_setIsSupply_starts_measuring_when_thread_not_alive(
    monkeypatch: pytest.MonkeyPatch, ppk2_stub: "PPK2PowerSupply"
) -> None:
    """SetIsSupply should start measuring when thread is not alive."""

    ppk = ppk2_stub
    ppk.v = 3.3
    ppk.r = MagicMock()
    ppk.measurement_thread = MagicMock()
    ppk.measurement_thread.is_alive.return_value = False
    ppk.measurement_thread.ident = None
    ppk.resetMeasurements = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr("meshtastic.powermon.ppk2.time.sleep", lambda _: None)

    ppk.setIsSupply(is_supply=False)

    # start_measuring should be called
    ppk.r.start_measuring.assert_called_once()


@pytest.mark.unit
def test_setIsSupply_uses_source_meter_for_supply_mode(
    monkeypatch: pytest.MonkeyPatch, ppk2_stub: "PPK2PowerSupply"
) -> None:
    """SetIsSupply should use source meter mode when is_supply=True."""

    ppk = ppk2_stub
    ppk.v = 3.3
    ppk.r = MagicMock()
    ppk.measurement_thread = MagicMock()
    ppk.measurement_thread.is_alive.return_value = True
    ppk.resetMeasurements = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr("meshtastic.powermon.ppk2.time.sleep", lambda _: None)

    ppk.setIsSupply(is_supply=True)

    ppk.r.use_source_meter.assert_called_once()
    ppk.r.use_ampere_meter.assert_not_called()


@pytest.mark.unit
def test_setIsSupply_uses_ampere_meter_for_meter_mode(
    monkeypatch: pytest.MonkeyPatch, ppk2_stub: "PPK2PowerSupply"
) -> None:
    """SetIsSupply should use ampere meter mode when is_supply=False."""

    ppk = ppk2_stub
    ppk.v = 3.3
    ppk.r = MagicMock()
    ppk.measurement_thread = MagicMock()
    ppk.measurement_thread.is_alive.return_value = True
    ppk.resetMeasurements = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr("meshtastic.powermon.ppk2.time.sleep", lambda _: None)

    ppk.setIsSupply(is_supply=False)

    ppk.r.use_ampere_meter.assert_called_once()
    ppk.r.use_source_meter.assert_not_called()


@pytest.mark.unit
def test_setIsSupply_creates_new_thread_when_previous_started(
    monkeypatch: pytest.MonkeyPatch, ppk2_stub: "PPK2PowerSupply"
) -> None:
    """SetIsSupply should create new thread when previous thread was already started."""

    ppk = ppk2_stub
    ppk.v = 3.3
    ppk.r = MagicMock()
    old_thread = MagicMock()
    old_thread.is_alive.return_value = False
    old_thread.ident = 123  # Previously started
    ppk.measurement_thread = old_thread
    ppk.resetMeasurements = MagicMock()  # type: ignore[method-assign]

    new_thread = MagicMock()
    new_thread.ident = None
    new_thread.is_alive.return_value = False

    def mock_thread(**kwargs):  # noqa: ARG001
        return new_thread

    monkeypatch.setattr("meshtastic.powermon.ppk2.threading.Thread", mock_thread)
    monkeypatch.setattr("meshtastic.powermon.ppk2.time.sleep", lambda _: None)

    ppk.setIsSupply(is_supply=False)

    # Should have created a new thread
    assert ppk.measurement_thread is new_thread


@pytest.mark.unit
def test_setIsSupply_sets_measuring_flag_true(
    monkeypatch: pytest.MonkeyPatch, ppk2_stub: "PPK2PowerSupply"
) -> None:
    """SetIsSupply should set measuring=True after initialization."""

    ppk = ppk2_stub
    ppk.v = 3.3
    ppk.r = MagicMock()
    ppk.measurement_thread = MagicMock()
    ppk.measurement_thread.is_alive.return_value = True
    ppk.resetMeasurements = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr("meshtastic.powermon.ppk2.time.sleep", lambda _: None)

    ppk.measuring = False
    ppk.setIsSupply(is_supply=False)

    assert ppk.measuring is True


@pytest.mark.unit
def test_setIsSupply_resets_measurements_twice_with_stabilization(
    monkeypatch: pytest.MonkeyPatch, ppk2_stub: "PPK2PowerSupply"
) -> None:
    """SetIsSupply should reset measurements twice with stabilization delay."""

    ppk = ppk2_stub
    ppk.v = 3.3
    ppk.r = MagicMock()
    ppk.measurement_thread = MagicMock()
    ppk.measurement_thread.is_alive.return_value = True

    reset_calls: list[int] = []
    sleep_calls: list[float] = []

    def mock_reset() -> None:
        reset_calls.append(1)

    def mock_sleep(duration: float) -> None:
        sleep_calls.append(duration)

    ppk.resetMeasurements = mock_reset  # type: ignore[method-assign]
    monkeypatch.setattr("meshtastic.powermon.ppk2.time.sleep", mock_sleep)

    ppk.setIsSupply(is_supply=False)

    # Should reset twice
    assert len(reset_calls) == 2
    # Should sleep stabilization delay
    assert STABILIZATION_DELAY_S in sleep_calls


@pytest.mark.unit
def test_setIsSupply_handles_thread_ident_none_but_alive(
    monkeypatch: pytest.MonkeyPatch, ppk2_stub: "PPK2PowerSupply"
) -> None:
    """SetIsSupply should start thread when ident is None and thread not alive."""

    ppk = ppk2_stub
    ppk.v = 3.3
    ppk.r = MagicMock()
    mock_thread = MagicMock()
    mock_thread.is_alive.return_value = False
    mock_thread.ident = None  # Never started
    ppk.measurement_thread = mock_thread
    ppk.resetMeasurements = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr("meshtastic.powermon.ppk2.time.sleep", lambda _: None)

    ppk.setIsSupply(is_supply=False)

    # Original thread should be started (ident is None)
    mock_thread.start.assert_called_once()


@pytest.mark.unit
def test_setIsSupply_handles_did_start_measuring_false_case(
    monkeypatch: pytest.MonkeyPatch, ppk2_stub: "PPK2PowerSupply"
) -> None:
    """SetIsSupply should call start_measuring if thread dies between checks."""

    ppk = ppk2_stub
    ppk.v = 3.3
    ppk.r = MagicMock()
    # Thread is alive first check, not alive second check
    mock_thread = MagicMock()
    mock_thread.is_alive.side_effect = [True, False]
    mock_thread.ident = None
    ppk.measurement_thread = mock_thread
    ppk.resetMeasurements = MagicMock()  # type: ignore[method-assign]

    new_thread = MagicMock()
    new_thread.ident = None
    new_thread.is_alive.return_value = False

    monkeypatch.setattr(
        "meshtastic.powermon.ppk2.threading.Thread", lambda **_: new_thread
    )
    monkeypatch.setattr("meshtastic.powermon.ppk2.time.sleep", lambda _: None)

    ppk.setIsSupply(is_supply=False)

    # Should call start_measuring once because thread died and did_start_measuring is False
    ppk.r.start_measuring.assert_called_once()


@pytest.mark.unit
def test_setIsSupply_does_not_restart_measuring_if_already_started(
    monkeypatch: pytest.MonkeyPatch, ppk2_stub: "PPK2PowerSupply"
) -> None:
    """SetIsSupply should not call start_measuring twice if did_start_measuring is True."""

    ppk = ppk2_stub
    ppk.v = 3.3
    ppk.r = MagicMock()
    mock_thread = MagicMock()
    mock_thread.is_alive.return_value = False
    mock_thread.ident = None
    ppk.measurement_thread = mock_thread
    ppk.resetMeasurements = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr("meshtastic.powermon.ppk2.time.sleep", lambda _: None)

    ppk.setIsSupply(is_supply=False)

    # Should only start once (in the initial check)
    ppk.r.start_measuring.assert_called_once()


@pytest.mark.unit
def test_setIsSupply_calls_toggle_DUT_power_not_implemented(
    ppk2_stub: "PPK2PowerSupply",
) -> None:
    """PowerOn and powerOff should call toggle_DUT_power with correct state."""

    ppk = ppk2_stub
    ppk.r = MagicMock()

    ppk.powerOn()
    ppk.r.toggle_DUT_power.assert_called_once_with("ON")

    ppk.r.reset_mock()

    ppk.powerOff()
    ppk.r.toggle_DUT_power.assert_called_once_with("OFF")


@pytest.mark.unit
def test_setIsSupply_uses_lock_for_thread_safety(
    monkeypatch: pytest.MonkeyPatch, ppk2_stub: "PPK2PowerSupply"
) -> None:
    """SetIsSupply should use _measurement_state_lock for thread safety."""

    ppk = ppk2_stub
    ppk.v = 3.3
    ppk.r = MagicMock()
    ppk.measurement_thread = MagicMock()
    ppk.measurement_thread.is_alive.return_value = True
    ppk.resetMeasurements = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr("meshtastic.powermon.ppk2.time.sleep", lambda _: None)

    lock_acquired = False

    # Create a wrapper lock that tracks acquisition
    original_lock = ppk._measurement_state_lock

    class TrackingLock:
        def __init__(self, real_lock):
            self._real_lock = real_lock

        def __enter__(self):
            nonlocal lock_acquired
            lock_acquired = True
            return self._real_lock.__enter__()

        def __exit__(self, *args):
            return self._real_lock.__exit__(*args)

        def acquire(self, blocking=True, timeout=-1):
            nonlocal lock_acquired
            lock_acquired = True
            return self._real_lock.acquire(blocking, timeout)

        def release(self):
            return self._real_lock.release()

    ppk._measurement_state_lock = TrackingLock(original_lock)  # type: ignore[assignment]

    ppk.setIsSupply(is_supply=False)

    assert lock_acquired


# =============================================================================
# Reset Measurements Tests
# =============================================================================


@pytest.mark.unit
def test_resetMeasurements_notifies_want_measurement(
    monkeypatch: pytest.MonkeyPatch,
    ppk2_stub: "PPK2PowerSupply",
) -> None:
    """ResetMeasurements should notify _want_measurement condition."""

    ppk = ppk2_stub

    notified = False

    def mock_notify(_n: int = 1) -> None:
        nonlocal notified
        notified = True

    monkeypatch.setattr(ppk._want_measurement, "notify", mock_notify)

    ppk.resetMeasurements()

    assert notified


@pytest.mark.unit
def test_resetMeasurements_preserves_last_reported_with_samples(
    ppk2_stub: "PPK2PowerSupply",
) -> None:
    """ResetMeasurements should update last_reported when samples exist."""

    ppk = ppk2_stub
    ppk.current_num_samples = 5
    ppk.current_min = 2000
    ppk.current_max = 8000
    ppk.last_reported_min = 1000
    ppk.last_reported_max = 9000

    ppk.resetMeasurements()

    # Should update last_reported to current values
    assert ppk.last_reported_min == 2000
    assert ppk.last_reported_max == 8000


@pytest.mark.unit
def test_resetMeasurements_preserves_last_reported_without_samples(
    ppk2_stub: "PPK2PowerSupply",
) -> None:
    """ResetMeasurements should preserve last_reported when no samples exist."""

    ppk = ppk2_stub
    ppk.current_num_samples = 0
    ppk.current_min = 2000
    ppk.current_max = 8000
    ppk.last_reported_min = 1000
    ppk.last_reported_max = 9000

    ppk.resetMeasurements()

    # Should keep existing last_reported values
    assert ppk.last_reported_min == 1000
    assert ppk.last_reported_max == 9000


# =============================================================================
# Close Method Tests
# =============================================================================


@pytest.mark.unit
def test_close_sets_measuring_false(
    ppk2_stub: "PPK2PowerSupply",
) -> None:
    """close() should set measuring=False."""

    ppk = ppk2_stub
    ppk.r = MagicMock()
    ppk.measuring = True
    ppk.measurement_thread = MagicMock()
    ppk.measurement_thread.is_alive.return_value = False

    ppk.close()

    assert ppk.measuring is False


@pytest.mark.unit
def test_close_notifies_all_want_measurement(
    monkeypatch: pytest.MonkeyPatch,
    ppk2_stub: "PPK2PowerSupply",
) -> None:
    """close() should notify_all on _want_measurement."""

    ppk = ppk2_stub
    ppk.r = MagicMock()
    ppk.measuring = True
    ppk.measurement_thread = MagicMock()
    ppk.measurement_thread.is_alive.return_value = False

    notified_all = False

    def mock_notify_all() -> None:
        nonlocal notified_all
        notified_all = True

    monkeypatch.setattr(ppk._want_measurement, "notify_all", mock_notify_all)

    ppk.close()

    assert notified_all


# =============================================================================
# Measurement Loop Break Conditions
# =============================================================================


@pytest.mark.unit
def test_measurement_loop_breaks_when_measuring_false_before_wait(
    ppk2_stub: "PPK2PowerSupply",
) -> None:
    """Measurement loop should break if measuring becomes False before wait."""

    ppk = ppk2_stub
    ppk.r = MagicMock()
    ppk.measuring = False  # Already False before loop starts

    # Run loop - should exit immediately
    ppk._measurement_loop()

    # Should not have called get_data
    ppk.r.get_data.assert_not_called()


@pytest.mark.unit
def test_measurement_loop_breaks_after_wait_when_measuring_false(
    ppk2_stub: "PPK2PowerSupply",
) -> None:
    """Measurement loop should break after wait if measuring becomes False."""

    ppk = ppk2_stub
    ppk.r = MagicMock()

    # Set measuring to False during wait

    def mock_wait(_self, timeout: float | None = None) -> bool:
        ppk.measuring = False
        return True

    with patch.object(threading.Condition, "wait", mock_wait):
        ppk.measuring = True
        ppk._measurement_loop()

    # Loop should have exited after one iteration


@pytest.mark.unit
def test_measurement_loop_handles_notify_during_reset(
    ppk2_stub: "PPK2PowerSupply",
) -> None:
    """Measurement loop should respond to notify from resetMeasurements."""

    ppk = ppk2_stub
    ppk.r = MagicMock()
    ppk.r.get_data.return_value = b"data"
    ppk.r.get_samples.return_value = ([1000], 0)

    iteration_count = 0

    def mock_wait(_self, timeout: float | None = None) -> bool:  # noqa: ARG001
        nonlocal iteration_count
        iteration_count += 1
        if iteration_count >= 2:
            ppk.measuring = False
        return True

    with patch.object(threading.Condition, "wait", mock_wait):
        ppk.measuring = True
        ppk._measurement_loop()

    # Should have processed data at least once
    assert iteration_count >= 1


# Custom exception class for serial errors
class SerialException(Exception):
    """Simulated serial communication exception."""


# =============================================================================
# Additional Edge Cases
# =============================================================================


@pytest.mark.unit
def test_get_min_current_mA_returns_nan_initially(
    ppk2_stub: "PPK2PowerSupply",
) -> None:
    """get_min_current_mA should return NaN when no samples ever recorded."""

    ppk = ppk2_stub
    ppk.current_num_samples = 0
    ppk.last_reported_min = math.nan

    result = ppk.get_min_current_mA()
    assert math.isnan(result)


@pytest.mark.unit
def test_get_max_current_mA_returns_nan_initially(
    ppk2_stub: "PPK2PowerSupply",
) -> None:
    """get_max_current_mA should return NaN when no samples ever recorded."""

    ppk = ppk2_stub
    ppk.current_num_samples = 0
    ppk.last_reported_max = math.nan

    result = ppk.get_max_current_mA()
    assert math.isnan(result)


@pytest.mark.unit
def test_reset_measurements_compatibility_shim_calls_canonical(
    ppk2_stub: "PPK2PowerSupply",
) -> None:
    """reset_measurements() compatibility shim should call resetMeasurements()."""

    ppk = ppk2_stub
    ppk.resetMeasurements = MagicMock()  # type: ignore[method-assign]

    ppk.reset_measurements()

    ppk.resetMeasurements.assert_called_once()


@pytest.mark.unit
def test_get_average_current_mA_returns_correct_value(
    ppk2_stub: "PPK2PowerSupply",
) -> None:
    """get_average_current_mA should return correctly converted value."""

    ppk = ppk2_stub
    ppk.current_num_samples = 2
    ppk.current_sum = 5000  # 5000 microamps total = 2500 microamps avg
    ppk.current_average = math.nan

    result = ppk.get_average_current_mA()
    expected = 2500 / MICROAMPS_PER_MILLIAMP  # 2.5 mA
    assert result == pytest.approx(expected)
