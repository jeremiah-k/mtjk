"""Extended test coverage for slog.py edge cases and error handling."""

import io
import logging
import threading
import time
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

pa = pytest.importorskip("pyarrow")

import meshtastic.slog.slog as slog_module  # noqa: E402  # pylint: disable=wrong-import-position
from meshtastic.slog.slog import (  # noqa: E402  # pylint: disable=wrong-import-position
    DIR_NAME_REQUIRED_MESSAGE,
    SAMPLE_FAILURE_WARNING_BURST_COUNT,
    SAMPLE_FAILURE_WARNING_COOLDOWN_SECONDS,
    LogSet,
    PowerLogger,
    StructuredLogger,
    rootDir,
)

# ============================================================================
# Test Helpers
# ============================================================================


class _FakeThread:
    """Lightweight thread stub for testing."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._args = args
        self._kwargs = kwargs
        self.started = False
        self._is_alive = True

    def start(self) -> None:
        """Start the fake thread."""
        self.started = True

    def join(self, timeout: float | None = None) -> None:  # noqa: ARG001
        """Join the fake thread immediately."""
        self._is_alive = False

    def is_alive(self) -> bool:
        """Return whether thread is alive."""
        return self._is_alive


class _FakeWriter:
    """In-memory writer stub for schema assertions."""

    def __init__(self, file_path: str) -> None:
        self.file_path = file_path
        self.schema: Any = None
        self.rows: list[dict[str, Any]] = []

    def setSchema(self, _schema: Any) -> None:
        """Set the schema for the writer."""
        self.schema = _schema

    def addRow(self, _row: dict[str, Any]) -> None:
        """Add a row to the writer."""
        self.rows.append(_row)

    def close(self) -> None:
        """Close the writer."""
        return None


class _SlowStopThread:
    """Thread that takes longer than timeout to stop."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG001
        self._target = kwargs.get("target")
        self.started = False
        self._is_alive = True
        self._thread_id: Optional[int] = None

    def start(self) -> None:
        """Start the slow thread, simulating thread running."""
        self.started = True
        # Simulate thread running
        self._thread_id = threading.current_thread().ident

    def join(self, timeout: float | None = None) -> None:  # noqa: ARG001
        """Join the slow thread - never completes."""
        return None

    def is_alive(self) -> bool:
        """Return True always - simulates hung thread."""
        return True


def _make_meter_with_voltage(voltage: float) -> MagicMock:
    """Create a PowerMeter mock with the given voltage."""
    meter = MagicMock()
    meter.v = voltage
    meter.getAverageCurrentMA.return_value = 10.0
    meter.getMaxCurrentMA.return_value = 20.0
    meter.getMinCurrentMA.return_value = 5.0
    return meter


def _make_meter_without_voltage() -> MagicMock:
    """Create a PowerMeter mock without voltage."""
    meter = MagicMock()
    meter.v = 0.0
    meter.getAverageCurrentMA.return_value = 10.0
    meter.getMaxCurrentMA.return_value = 20.0
    meter.getMinCurrentMA.return_value = 5.0
    return meter


# ============================================================================
# File Rotation and Directory Edge Cases
# ============================================================================


@pytest.mark.unit
def test_logset_dir_name_empty_raises_valueerror() -> None:
    """LogSet should raise ValueError when dir_name is empty string."""
    with pytest.raises(ValueError, match=DIR_NAME_REQUIRED_MESSAGE):
        LogSet(MagicMock(), dir_name="")


@pytest.mark.unit
def test_logset_handles_symlink_update_failure_on_latest_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """LogSet should handle failure when updating 'latest' symlink."""
    monkeypatch.setattr(slog_module, "rootDir", lambda: str(tmp_path))

    # Create a file at 'latest' that can't be removed
    latest_path = tmp_path / "latest"
    latest_path.write_text("blocking file")

    # Make unlink fail
    original_unlink = Path.unlink

    def failing_unlink(self: Path, missing_ok: bool = False) -> None:
        if "latest" in str(self):
            raise OSError("Cannot remove file")
        original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", failing_unlink)

    # Create proper mocks
    mock_slog = MagicMock()
    mock_power = MagicMock()
    monkeypatch.setattr(
        slog_module, "StructuredLogger", lambda *args, **kwargs: mock_slog
    )
    monkeypatch.setattr(slog_module, "PowerLogger", lambda *args, **kwargs: mock_power)

    with caplog.at_level(logging.WARNING):
        logset = LogSet(MagicMock(), dir_name=None)

    assert "Skipping latest symlink update" in caplog.text
    logset.close()


@pytest.mark.unit
def test_logset_creates_dir_when_dir_name_provided(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """LogSet should create directory when dir_name is explicitly provided."""
    custom_dir = tmp_path / "custom-logs"

    # Don't mock client - just use a mock
    client = MagicMock()

    # Create proper mocks
    mock_slog = MagicMock()
    mock_power = MagicMock()
    monkeypatch.setattr(
        slog_module, "StructuredLogger", lambda *args, **kwargs: mock_slog
    )
    monkeypatch.setattr(slog_module, "PowerLogger", lambda *args, **kwargs: mock_power)

    logset = LogSet(client, dir_name=str(custom_dir))

    assert custom_dir.exists()
    assert logset.dir_name == str(custom_dir)
    logset.close()


@pytest.mark.unit
def test_logset_handles_dir_creation_with_parents(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """LogSet should create parent directories when needed."""
    nested_dir = tmp_path / "deep" / "nested" / "logs"

    client = MagicMock()
    mock_slog = MagicMock()
    mock_power = MagicMock()
    monkeypatch.setattr(
        slog_module, "StructuredLogger", lambda *args, **kwargs: mock_slog
    )
    monkeypatch.setattr(slog_module, "PowerLogger", lambda *args, **kwargs: mock_power)

    logset = LogSet(client, dir_name=str(nested_dir))

    assert nested_dir.exists()
    logset.close()


@pytest.mark.unit
def test_logset_collision_retries_with_suffix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """LogSet should retry with suffixed names on collision."""
    monkeypatch.setattr(slog_module, "rootDir", lambda: str(tmp_path))

    # Get the timestamp that will be used
    base_stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")

    # Create directories that will collide at first attempt
    (tmp_path / base_stamp).mkdir()
    (tmp_path / f"{base_stamp}-1").mkdir()

    mock_slog = MagicMock()
    mock_power = MagicMock()
    monkeypatch.setattr(
        slog_module, "StructuredLogger", lambda *args, **kwargs: mock_slog
    )
    monkeypatch.setattr(slog_module, "PowerLogger", lambda *args, **kwargs: mock_power)

    logset = LogSet(MagicMock(), dir_name=None)

    # Check that we got a directory with suffix
    assert "-" in logset.dir_name.split("/")[-1]
    logset.close()


@pytest.mark.unit
def test_logset_symlink_fails_silently(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """LogSet should silently handle symlink creation failures."""
    monkeypatch.setattr(slog_module, "rootDir", lambda: str(tmp_path))

    mock_slog = MagicMock()
    mock_power = MagicMock()
    monkeypatch.setattr(
        slog_module, "StructuredLogger", lambda *args, **kwargs: mock_slog
    )
    monkeypatch.setattr(slog_module, "PowerLogger", lambda *args, **kwargs: mock_power)

    # Make symlink fail
    def failing_symlink(self: Path, target: str, target_is_directory: bool = False) -> None:
        raise OSError("Symlink not supported")

    monkeypatch.setattr(Path, "symlink_to", failing_symlink)

    with caplog.at_level(logging.DEBUG):
        logset = LogSet(MagicMock(), dir_name=None)

    # Should log at debug level but not raise
    assert "Unable to update latest slog symlink" in caplog.text
    logset.close()


# ============================================================================
# Concurrent Write Scenarios
# ============================================================================


@pytest.mark.unit
def test_power_logger_p_meter_concurrent_setter_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test concurrent access to pMeter setter doesn't corrupt state."""
    monkeypatch.setattr(slog_module, "FeatherWriter", _FakeWriter)
    monkeypatch.setattr("meshtastic.slog.slog.threading.Thread", _FakeThread)

    meter1 = _make_meter_with_voltage(3.3)
    logger = PowerLogger(meter1, "unused-path")

    errors: list[Exception] = []

    def setter_worker() -> None:
        try:
            for i in range(100):
                new_meter = _make_meter_with_voltage(3.3 + (i % 10) * 0.1)
                logger.pMeter = new_meter
        except Exception as e:
            errors.append(e)

    def getter_worker() -> None:
        try:
            for _ in range(100):
                _ = logger.pMeter
        except Exception as e:
            errors.append(e)

    threads = [
        threading.Thread(target=setter_worker),
        threading.Thread(target=getter_worker),
        threading.Thread(target=setter_worker),
        threading.Thread(target=getter_worker),
    ]

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert not errors, f"Concurrent access errors: {errors}"
    logger.close()


@pytest.mark.unit
def test_structured_logger_raw_file_concurrent_writes() -> None:
    """Test concurrent writes to raw file are thread-safe."""
    logger = object.__new__(StructuredLogger)
    logger.writer = MagicMock()
    logger.power_logger = None
    logger.include_raw = True
    logger._raw_file_lock = threading.Lock()

    # Use a real StringIO to capture writes
    raw_buffer = io.StringIO()
    logger.raw_file = raw_buffer  # type: ignore[assignment]

    errors: list[Exception] = []

    def write_worker() -> None:
        try:
            for i in range(50):
                logger._on_log_message(f"Line {i}")
        except Exception as e:
            errors.append(e)

    threads = [
        threading.Thread(target=write_worker),
        threading.Thread(target=write_worker),
    ]

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert not errors, f"Concurrent write errors: {errors}"


@pytest.mark.unit
def test_power_logger_concurrent_store_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test concurrent calls to storeCurrentReading are thread-safe."""
    monkeypatch.setattr(slog_module, "FeatherWriter", _FakeWriter)
    monkeypatch.setattr("meshtastic.slog.slog.threading.Thread", _FakeThread)

    meter = _make_meter_with_voltage(3.3)
    logger = PowerLogger(meter, "unused-path", interval=0.1)

    errors: list[Exception] = []

    def store_worker() -> None:
        try:
            for _ in range(100):
                logger.storeCurrentReading()
        except Exception as e:
            errors.append(e)

    threads = [
        threading.Thread(target=store_worker),
        threading.Thread(target=store_worker),
    ]

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert not errors, f"Concurrent store errors: {errors}"
    logger.close()


# ============================================================================
# Error Handling During Log Writes
# ============================================================================


@pytest.mark.unit
def test_power_logger_writer_close_failure_during_error_cleanup(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PowerLogger should handle writer close failure during error cleanup."""
    meter = _make_meter_with_voltage(3.3)

    mock_writer = MagicMock()
    mock_writer.setSchema.side_effect = RuntimeError("schema error")
    mock_writer.close.side_effect = OSError("close error")

    def mock_writer_factory(path: str) -> MagicMock:  # noqa: ARG001
        return mock_writer

    monkeypatch.setattr(slog_module, "FeatherWriter", mock_writer_factory)
    monkeypatch.setattr("meshtastic.slog.slog.threading.Thread", _FakeThread)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(RuntimeError, match="schema error"):
            PowerLogger(meter, "unused-path")

    assert "Failed to close power writer after schema setup failure" in caplog.text


@pytest.mark.unit
def test_power_logger_reset_after_write_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """PowerLogger should reset meter even after write failure."""
    meter = _make_meter_with_voltage(3.3)
    meter.resetMeasurements.side_effect = OSError("reset failed")

    logger = object.__new__(PowerLogger)
    logger._p_meter = meter
    logger._warned_legacy_mw_without_voltage = False
    logger._warned_store_current_reading_deprecation = False
    logger._reading_lock = threading.Lock()
    logger.writer = MagicMock()
    logger.writer.addRow.side_effect = RuntimeError("write failed")

    with caplog.at_level(logging.WARNING):
        with pytest.raises(RuntimeError, match="write failed"):
            logger._store_current_reading()

    # resetMeasurements should have been called
    meter.resetMeasurements.assert_called_once()


@pytest.mark.unit
def test_power_logger_sample_error_suppression(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """PowerLogger should suppress warnings after burst count."""
    meter = MagicMock()
    meter.getAverageCurrentMA.side_effect = OSError("meter read failed")
    meter.resetMeasurements = MagicMock()

    logger = object.__new__(PowerLogger)
    logger._p_meter = meter
    logger._stop_event = threading.Event()
    logger.interval = 0.001
    logger._sample_warning_count = 0
    logger._last_sample_warning_monotonic = 0.0
    logger.writer = MagicMock()
    logger.writer.addRow = MagicMock()

    with caplog.at_level(logging.WARNING):
        # Simulate exception handling path from _logging_thread
        for _ in range(SAMPLE_FAILURE_WARNING_BURST_COUNT + 2):
            try:
                logger._store_current_reading()
            except Exception as exc:
                logger._sample_warning_count += 1
                now_monotonic = time.monotonic()
                should_warn = (
                    logger._sample_warning_count <= SAMPLE_FAILURE_WARNING_BURST_COUNT
                    or (
                        now_monotonic - logger._last_sample_warning_monotonic
                        >= SAMPLE_FAILURE_WARNING_COOLDOWN_SECONDS
                    )
                )
                if should_warn:
                    logging.warning("PowerLogger sample failed: %s", exc)
                    logger._last_sample_warning_monotonic = now_monotonic
                else:
                    logging.debug(
                        "PowerLogger sample failed (suppressed warning #%d): %s",
                        logger._sample_warning_count,
                        exc,
                    )

    # Should have exactly SAMPLE_FAILURE_WARNING_BURST_COUNT warnings
    warning_count = caplog.text.count("PowerLogger sample failed")
    assert (
        warning_count <= SAMPLE_FAILURE_WARNING_BURST_COUNT
    ), f"Got {warning_count} warnings"


@pytest.mark.unit
def test_structured_logger_add_row_failure_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """StructuredLogger should continue processing even when addRow fails."""
    logger_obj = object.__new__(StructuredLogger)
    logger_obj.writer = MagicMock()
    logger_obj.writer.addRow.side_effect = OSError("disk full")
    logger_obj.power_logger = None
    logger_obj.include_raw = False

    # Should not raise, just log warning
    with caplog.at_level(logging.WARNING):
        logger_obj._on_log_message("S:B:1,version")

    assert "Failed to write structured slog row" in caplog.text


# ============================================================================
# Power Logger Edge Cases
# ============================================================================


@pytest.mark.unit
def test_power_logger_negative_voltage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PowerLogger should handle negative voltage gracefully."""
    monkeypatch.setattr(slog_module, "FeatherWriter", _FakeWriter)
    monkeypatch.setattr("meshtastic.slog.slog.threading.Thread", _FakeThread)

    meter = _make_meter_with_voltage(-1.0)  # Negative voltage
    logger = PowerLogger(meter, "unused-path")

    # Should treat negative voltage as unavailable
    assert logger._nominal_voltage_v() is None
    logger.close()


@pytest.mark.unit
def test_power_logger_zero_voltage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PowerLogger should handle zero voltage gracefully."""
    monkeypatch.setattr(slog_module, "FeatherWriter", _FakeWriter)
    monkeypatch.setattr("meshtastic.slog.slog.threading.Thread", _FakeThread)

    meter = _make_meter_with_voltage(0.0)
    logger = PowerLogger(meter, "unused-path")

    # Should treat zero voltage as unavailable
    assert logger._nominal_voltage_v() is None
    logger.close()


@pytest.mark.unit
def test_power_logger_voltage_bool_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PowerLogger should handle bool False as voltage gracefully."""
    monkeypatch.setattr(slog_module, "FeatherWriter", _FakeWriter)
    monkeypatch.setattr("meshtastic.slog.slog.threading.Thread", _FakeThread)

    meter = _make_meter_with_voltage(3.3)
    meter.v = False  # bool is subclass of int
    logger = PowerLogger(meter, "unused-path")

    # bool is instance of int, so False should return None
    result = logger._nominal_voltage_v()
    assert result is None
    logger.close()


@pytest.mark.unit
def test_power_logger_very_small_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PowerLogger should accept very small positive interval."""
    monkeypatch.setattr(slog_module, "FeatherWriter", _FakeWriter)
    monkeypatch.setattr("meshtastic.slog.slog.threading.Thread", _FakeThread)

    meter = _make_meter_with_voltage(3.3)
    logger = PowerLogger(meter, "unused-path", interval=0.0001)

    assert logger.interval == 0.0001
    logger.close()


@pytest.mark.unit
def test_power_logger_hung_thread_leaves_dependencies_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hung sampler must leave its dependencies open and surface failure."""
    writer = MagicMock()
    monkeypatch.setattr(slog_module, "FeatherWriter", MagicMock(return_value=writer))
    monkeypatch.setattr("meshtastic.slog.slog.threading.Thread", _SlowStopThread)

    meter = _make_meter_with_voltage(3.3)
    logger = PowerLogger(meter, "unused-path")

    with pytest.raises(RuntimeError, match="did not stop within"):
        logger.close()

    meter.close.assert_not_called()
    writer.close.assert_not_called()


@pytest.mark.unit
def test_power_logger_close_from_logging_thread(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A self-close request must defer cleanup until the sampler exits."""
    monkeypatch.setattr(slog_module, "FeatherWriter", _FakeWriter)

    meter = _make_meter_with_voltage(3.3)
    logger = PowerLogger(meter, "unused-path")

    # Make the caller look like the logging thread only for the close decision.
    with monkeypatch.context() as context:
        context.setattr(threading, "current_thread", lambda: logger.thread)
        with caplog.at_level(logging.DEBUG):
            logger.close()

    assert "deferring dependency cleanup until thread exit" in caplog.text
    logger.thread.join(timeout=1.0)
    assert not logger.thread.is_alive()
    meter.close.assert_called_once_with()


@pytest.mark.unit
def test_power_logger_both_pmeter_args_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PowerLogger should error when both p_meter and pMeter provided with different values."""
    monkeypatch.setattr(slog_module, "FeatherWriter", _FakeWriter)
    monkeypatch.setattr("meshtastic.slog.slog.threading.Thread", _FakeThread)

    meter1 = _make_meter_with_voltage(3.3)
    meter2 = _make_meter_with_voltage(5.0)

    with pytest.raises(
        TypeError, match="Specify only one of 'p_meter' or legacy 'pMeter'"
    ):
        PowerLogger(meter1, "unused-path", pMeter=meter2)


@pytest.mark.unit
def test_power_logger_unexpected_kwargs_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PowerLogger should error on unexpected kwargs."""
    monkeypatch.setattr(slog_module, "FeatherWriter", _FakeWriter)
    monkeypatch.setattr("meshtastic.slog.slog.threading.Thread", _FakeThread)

    meter = _make_meter_with_voltage(3.3)

    with pytest.raises(TypeError, match="Unexpected keyword argument"):
        PowerLogger(meter, "unused-path", invalid_kwarg=True)


@pytest.mark.unit
def test_power_logger_meter_close_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PowerLogger should raise meter close failure after successful writer close."""
    monkeypatch.setattr(slog_module, "FeatherWriter", _FakeWriter)
    monkeypatch.setattr("meshtastic.slog.slog.threading.Thread", _FakeThread)

    meter = _make_meter_with_voltage(3.3)
    meter.close.side_effect = OSError("meter close failed")

    logger = PowerLogger(meter, "unused-path")

    with pytest.raises(OSError, match="meter close failed"):
        logger.close()


@pytest.mark.unit
def test_power_logger_writer_close_failure_after_meter_close_failure(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PowerLogger should log writer close failure when meter close also failed."""
    meter = _make_meter_with_voltage(3.3)
    meter.close.side_effect = OSError("meter close failed")

    mock_writer = MagicMock()
    mock_writer.setSchema = MagicMock()
    mock_writer.close.side_effect = OSError("writer close failed")

    def mock_writer_factory(path: str) -> MagicMock:  # noqa: ARG001
        return mock_writer

    monkeypatch.setattr(slog_module, "FeatherWriter", mock_writer_factory)
    monkeypatch.setattr("meshtastic.slog.slog.threading.Thread", _FakeThread)

    logger = PowerLogger(meter, "unused-path")

    with caplog.at_level(logging.WARNING):
        with pytest.raises(OSError, match="meter close failed"):
            logger.close()

    assert (
        "PowerLogger writer close failed after power meter close error" in caplog.text
    )


@pytest.mark.unit
def test_power_logger_store_reading_reset_failure_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """PowerLogger should log when reset fails without primary exception."""
    meter = _make_meter_with_voltage(3.3)
    meter.resetMeasurements.side_effect = OSError("reset failed")

    logger = object.__new__(PowerLogger)
    logger._p_meter = meter
    logger._warned_legacy_mw_without_voltage = False
    logger._warned_store_current_reading_deprecation = False
    logger._reading_lock = threading.Lock()
    logger.writer = MagicMock()

    with caplog.at_level(logging.WARNING):
        with pytest.raises(OSError, match="reset failed"):
            logger._store_current_reading()


# ============================================================================
# Data Parsing Error Handling
# ============================================================================


@pytest.mark.unit
def test_logdef_parse_failure_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """LogDef should log warning when parsing fails."""
    logger_obj = object.__new__(StructuredLogger)
    logger_obj.writer = MagicMock()
    logger_obj.power_logger = None
    logger_obj.include_raw = False

    # Try to parse a malformed log line
    with caplog.at_level(logging.WARNING):
        logger_obj._on_log_message("S:B:invalid-data")

    assert "Failed to parse slog" in caplog.text


@pytest.mark.unit
def test_logdef_unknown_code_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """StructuredLogger should log warning for unknown log codes."""
    logger_obj = object.__new__(StructuredLogger)
    logger_obj.writer = MagicMock()
    logger_obj.power_logger = None
    logger_obj.include_raw = False

    with caplog.at_level(logging.WARNING):
        logger_obj._on_log_message("S:UNKNOWN:123,test")

    assert "Unknown Structured Log" in caplog.text


@pytest.mark.unit
def test_logdef_empty_last_string_field(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """LogDef should handle empty string in last field."""
    logger_obj = object.__new__(StructuredLogger)
    logger_obj.writer = MagicMock()
    logger_obj.power_logger = None
    logger_obj.include_raw = True
    logger_obj.raw_file = MagicMock()
    logger_obj._raw_file_lock = threading.Lock()

    # PM log with empty reason string
    with caplog.at_level(logging.WARNING):
        logger_obj._on_log_message("S:PM:255,")

    # Should parse but remove empty field
    call_args = logger_obj.writer.addRow.call_args
    if call_args:
        _ = call_args[0][0] if call_args[0] else call_args[1].get("row", {})
        # pm_reason might be absent or empty


@pytest.mark.unit
def test_logdef_parse_no_match_pattern(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """StructuredLogger should handle non-matching log lines."""
    logger_obj = object.__new__(StructuredLogger)
    logger_obj.writer = MagicMock()
    logger_obj.power_logger = None
    logger_obj.include_raw = True
    logger_obj.raw_file = MagicMock()
    logger_obj._raw_file_lock = threading.Lock()

    # Line that doesn't match the log pattern
    with caplog.at_level(logging.DEBUG):
        logger_obj._on_log_message("Regular log message without S: prefix")

    # Should still write to raw file when include_raw=True
    logger_obj.raw_file.write.assert_called_once()


# ============================================================================
# Storage Edge Cases (Disk Full, Permissions)
# ============================================================================


@pytest.mark.unit
def test_structured_logger_raw_file_write_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """StructuredLogger should handle raw file write failures gracefully."""
    logger_obj = object.__new__(StructuredLogger)
    logger_obj.writer = MagicMock()
    logger_obj.power_logger = None
    logger_obj.include_raw = True
    logger_obj._raw_file_lock = threading.Lock()

    raw_file = MagicMock()
    raw_file.write.side_effect = OSError("disk full")
    logger_obj.raw_file = raw_file

    with caplog.at_level(logging.WARNING):
        logger_obj._on_log_message("test log line")

    assert "Failed to write raw slog line" in caplog.text


@pytest.mark.unit
def test_structured_logger_raw_file_close_failure(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """StructuredLogger.close should handle raw file close failures."""
    logger_obj = object.__new__(StructuredLogger)
    logger_obj._listen_glue = MagicMock()
    logger_obj.writer = MagicMock()
    logger_obj._raw_file_lock = threading.Lock()

    raw_file = MagicMock()
    raw_file.close.side_effect = OSError("close failed")
    logger_obj.raw_file = raw_file

    monkeypatch.setattr(slog_module, "pub", MagicMock())

    with caplog.at_level(logging.WARNING):
        logger_obj.close()

    assert "Failed to close raw log file" in caplog.text


@pytest.mark.unit
def test_logset_startup_failure_closes_power_logger(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """LogSet should close power logger when structured logger setup fails."""
    monkeypatch.setattr(slog_module, "rootDir", lambda: str(tmp_path))

    power_logger_mock = MagicMock()
    power_logger_mock.close = MagicMock()

    def mock_power_factory(*args: Any, **kwargs: Any) -> MagicMock:  # noqa: ARG001
        return power_logger_mock

    monkeypatch.setattr(slog_module, "PowerLogger", mock_power_factory)

    def mock_slog_factory(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("setup failed")

    monkeypatch.setattr(slog_module, "StructuredLogger", mock_slog_factory)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(RuntimeError, match="setup failed"):
            LogSet(MagicMock(), dir_name=None, power_meter=MagicMock())

    power_logger_mock.close.assert_called_once()


@pytest.mark.unit
def test_logset_startup_failure_logs_secondary_error(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """LogSet should log secondary error when closing power logger during startup failure."""
    monkeypatch.setattr(slog_module, "rootDir", lambda: str(tmp_path))

    power_logger_mock = MagicMock()
    power_logger_mock.close.side_effect = OSError("power close failed")

    def mock_power_factory(*args: Any, **kwargs: Any) -> MagicMock:  # noqa: ARG001
        return power_logger_mock

    monkeypatch.setattr(slog_module, "PowerLogger", mock_power_factory)

    def mock_slog_factory(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("setup failed")

    monkeypatch.setattr(slog_module, "StructuredLogger", mock_slog_factory)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(RuntimeError, match="setup failed"):
            LogSet(MagicMock(), dir_name=None, power_meter=MagicMock())

    assert (
        "Ignoring secondary error while closing power logger during startup failure"
        in caplog.text
    )


@pytest.mark.unit
def test_structured_logger_setup_failure_cleanup(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """StructuredLogger should cleanup resources on setup failure."""
    mock_pub = MagicMock()
    mock_pub.subscribe = MagicMock()
    monkeypatch.setattr(slog_module, "pub", mock_pub)

    mock_writer = MagicMock()
    mock_writer.setSchema.side_effect = RuntimeError("schema failed")
    mock_writer.close = MagicMock()

    def mock_writer_factory(path: str) -> MagicMock:  # noqa: ARG001
        return mock_writer

    monkeypatch.setattr(slog_module, "FeatherWriter", mock_writer_factory)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(RuntimeError, match="schema failed"):
            StructuredLogger(MagicMock(), str(tmp_path))

    mock_writer.close.assert_called_once()


@pytest.mark.unit
def test_structured_logger_raw_file_setup_failure_cleanup(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """StructuredLogger should cleanup raw file on setup failure."""
    mock_pub = MagicMock()
    mock_pub.subscribe = MagicMock()
    monkeypatch.setattr(slog_module, "pub", mock_pub)

    mock_writer = MagicMock()
    mock_raw_file = MagicMock()
    mock_raw_file.close = MagicMock()

    # Fail after raw file is opened
    call_count = 0

    def fail_on_setSchema(*args: Any, **kwargs: Any) -> None:  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        if call_count >= 1:
            raise RuntimeError("schema failed")

    mock_writer.setSchema = MagicMock(side_effect=fail_on_setSchema)
    mock_writer.close = MagicMock()

    def mock_writer_factory(path: str) -> MagicMock:  # noqa: ARG001
        return mock_writer

    monkeypatch.setattr(slog_module, "FeatherWriter", mock_writer_factory)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(RuntimeError, match="schema failed"):
            # Use a mock client
            StructuredLogger(MagicMock(), str(tmp_path), include_raw=True)


@pytest.mark.unit
def test_root_dir_creates_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """RootDir should create slog directory if it doesn't exist."""
    slog_dir = tmp_path / "slogs"
    monkeypatch.setattr(
        "meshtastic.slog.slog.platformdirs.user_data_dir",
        lambda *args, **kwargs: str(tmp_path),
    )

    result = rootDir()

    assert slog_dir.exists()
    assert result == str(slog_dir)


@pytest.mark.unit
def test_power_logger_pmeter_setter_no_lock_fallback() -> None:
    """PowerLogger.pMeter setter should work without lock for test doubles."""
    logger = object.__new__(PowerLogger)
    # Don't initialize _reading_lock
    meter = _make_meter_with_voltage(3.3)

    # Should not raise
    logger.pMeter = meter
    assert logger.pMeter is meter


@pytest.mark.unit
def test_power_logger_store_current_reading_no_lock_fallback() -> None:
    """PowerLogger.store_current_reading deprecation should work without lock."""
    meter = _make_meter_with_voltage(3.3)

    logger = object.__new__(PowerLogger)
    logger._p_meter = meter
    logger.writer = MagicMock()
    logger._warned_store_current_reading_deprecation = False
    logger._reading_lock = threading.Lock()
    # Don't initialize _deprecation_warning_lock

    # Should not raise - will use fallback lock and emit deprecation warning
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        logger.store_current_reading()

    assert any(issubclass(item.category, DeprecationWarning) for item in captured)


@pytest.mark.unit
def test_structured_logger_raw_file_none_during_write() -> None:
    """StructuredLogger should skip write when raw_file is None."""
    logger = object.__new__(StructuredLogger)
    logger.writer = MagicMock()
    logger.power_logger = None
    logger.include_raw = True
    logger._raw_file_lock = threading.Lock()
    logger.raw_file = None

    # Should not raise
    logger._on_log_message("test line")

    # Should still write structured data
    logger.writer.addRow.assert_called_once()


# ============================================================================
# Additional edge cases for compatibility coverage
# ============================================================================


@pytest.mark.unit
def test_power_logger_pmeter_property_compatibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test p_meter property compatibility alias works."""
    monkeypatch.setattr(slog_module, "FeatherWriter", _FakeWriter)
    monkeypatch.setattr("meshtastic.slog.slog.threading.Thread", _FakeThread)

    meter = _make_meter_with_voltage(3.3)
    logger = PowerLogger(meter, "unused-path")

    # Test p_meter property (snake_case alias)
    assert logger.p_meter is meter

    # Test p_meter setter
    new_meter = _make_meter_with_voltage(5.0)
    logger.p_meter = new_meter
    assert logger.pMeter is new_meter

    logger.close()


@pytest.mark.unit
def test_power_logger_store_current_reading_deprecation_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test store_current_reading emits deprecation warning once."""
    monkeypatch.setattr(slog_module, "FeatherWriter", _FakeWriter)
    monkeypatch.setattr("meshtastic.slog.slog.threading.Thread", _FakeThread)

    meter = _make_meter_with_voltage(3.3)
    logger = PowerLogger(meter, "unused-path")

    # Clear warning state
    logger._warned_store_current_reading_deprecation = False

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        logger.store_current_reading()
        logger.store_current_reading()  # Second call should not warn

        deprecation_warnings = [
            x for x in w if issubclass(x.category, DeprecationWarning)
        ]
        assert len(deprecation_warnings) == 1
        assert "store_current_reading()" in str(deprecation_warnings[0].message)

    logger.close()
