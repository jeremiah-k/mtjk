"""Unit tests for power logging behavior in slog.py."""

import importlib.util
import logging
import sys
import threading
import typing
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

pa = pytest.importorskip("pyarrow")

import meshtastic.slog as slog_package  # noqa: E402  # pylint: disable=wrong-import-position
from meshtastic.slog.slog import (  # noqa: E402  # pylint: disable=wrong-import-position
    POWER_LOG_SCHEMA_METADATA,
    LogDef,
    LogSet,
    PowerLogger,
    StructuredLogger,
    root_dir,
    rootDir,
)

try:
    from meshtastic.slog import slog as slog_module
except ImportError:
    pytest.skip("Can't import meshtastic.slog", allow_module_level=True)


class _FakeThread:
    """Lightweight thread stub used to avoid background work in tests."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._args = args
        self._kwargs = kwargs
        self.started = False

    def start(self) -> None:
        """Record that start() was called."""
        self.started = True

    def join(self, timeout: float | None = None) -> None:
        """No-op join for compatibility with the production thread API."""

    def is_alive(self) -> bool:
        """Return False to match threading.Thread API."""
        return False


class _FakeWriter:
    """In-memory writer stub for schema assertions."""

    def __init__(self, file_path: str) -> None:
        self.file_path = file_path
        self.schema: Any = None

    def set_schema(self, schema: Any) -> None:
        """Capture the schema configured by PowerLogger."""
        self.schema = schema

    def setSchema(self, schema: Any) -> None:
        """Primary camelCase API used by production code; delegates to set_schema for schema capture."""
        self.set_schema(schema)

    def close(self) -> None:
        """No-op close used by tests."""


def _extract_row_from_add_row_call(writer: MagicMock) -> dict[str, Any]:
    """Return the row payload passed to writer.addRow(...)."""
    call = writer.addRow.call_args
    assert call is not None
    return _extract_row_from_call(call)


def _extract_row_from_call(call: Any) -> dict[str, Any]:
    """Return row payload from a single writer.addRow call object."""
    row = call.kwargs.get("row")
    if row is None:
        row = call.kwargs.get("row_dict")
    if row is None:
        assert call.args, "Expected addRow to include row payload"
        row = call.args[0]
    assert isinstance(row, dict), "Expected addRow row payload to be a dict"
    return typing.cast(dict[str, Any], row)


@pytest.mark.unit
def test_power_logger_sets_schema_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """PowerLogger should stamp compatibility metadata on its writer schema."""
    captured: dict[str, _FakeWriter] = {}

    def _fake_writer_factory(file_path: str) -> _FakeWriter:
        writer = _FakeWriter(file_path)
        captured["writer"] = writer
        return writer

    monkeypatch.setattr("meshtastic.slog.slog.FeatherWriter", _fake_writer_factory)
    monkeypatch.setattr("meshtastic.slog.slog.threading.Thread", _FakeThread)

    meter = MagicMock()
    logger = PowerLogger(meter, "unused-path")
    writer = captured["writer"]
    assert writer.schema is not None
    assert writer.schema.metadata == POWER_LOG_SCHEMA_METADATA
    assert writer.schema.names == [
        "time",
        "average_mA",
        "max_mA",
        "min_mA",
        "average_mW",
        "max_mW",
        "min_mW",
    ]
    logger.close()


@pytest.mark.unit
def test_power_logger_accepts_legacy_pmeter_constructor_keyword(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy `pMeter=` constructor keyword should remain accepted for compatibility."""
    monkeypatch.setattr("meshtastic.slog.slog.FeatherWriter", _FakeWriter)
    monkeypatch.setattr("meshtastic.slog.slog.threading.Thread", _FakeThread)

    meter = MagicMock()
    logger = PowerLogger(pMeter=meter, file_path="unused-path")
    assert logger.pMeter is meter
    logger.close()


@pytest.mark.unit
def test_root_dir_legacy_alias_warns_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Legacy root_dir() alias should warn once and return the same path as rootDir()."""
    monkeypatch.setattr(
        "meshtastic.slog.slog.platformdirs.user_data_dir",
        lambda *_args, **_kwargs: str(tmp_path),
    )
    slog_module._warned_deprecations.clear()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        legacy_path_first = root_dir()
        legacy_path_second = root_dir()
    preferred_path = rootDir()

    assert legacy_path_first == preferred_path
    assert legacy_path_second == preferred_path
    deprecations = [
        warning
        for warning in caught
        if issubclass(warning.category, DeprecationWarning)
    ]
    assert len(deprecations) == 1
    assert "root_dir()" in str(deprecations[0].message)


@pytest.mark.unit
def test_slog_directory_collision_error_includes_path_and_attempts() -> None:
    """SlogDirectoryCollisionError should include app dir and retry count context."""
    error = slog_module.SlogDirectoryCollisionError("/tmp/slog-root", 7)
    assert "under '/tmp/slog-root'" in str(error)
    assert "after 7 attempts" in str(error)


@pytest.mark.unit
def test_slog_arrow_data_type_type_checking_alias_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the Arrow type alias without replacing live slog class identities."""

    class _SubscriptableDataType:
        """Minimal stand-in that supports class subscription syntax."""

        @classmethod
        def __class_getitem__(cls, _item: Any) -> type["_SubscriptableDataType"]:
            return cls

    spec = importlib.util.spec_from_file_location(
        "meshtastic.slog._type_alias_probe", slog_module.__file__
    )
    assert spec is not None and spec.loader is not None
    probe = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, probe)
    monkeypatch.setattr(typing, "TYPE_CHECKING", True)
    monkeypatch.setattr(pa, "DataType", _SubscriptableDataType)
    spec.loader.exec_module(probe)
    assert probe.ArrowDataType is _SubscriptableDataType


@pytest.mark.unit
def test_slog_public_exports_remain_available() -> None:
    """Slog package should keep expected historical public exports."""
    expected_exports = {"LogSet", "rootDir", "root_dir"}
    assert expected_exports.issubset(set(slog_package.__all__))
    for export_name in expected_exports:
        assert hasattr(slog_package, export_name)


@pytest.mark.unit
def test_logdef_rejects_unsupported_arrow_field_types() -> None:
    """LogDef should fail fast when asked to format unsupported Arrow data types."""
    with pytest.raises(ValueError, match="Unsupported LogDef field type"):
        LogDef("X", [("voltage", pa.float64())])


@pytest.mark.unit
@pytest.mark.parametrize("interval", [0, -0.001, -1.0])
def test_power_logger_rejects_non_positive_interval(
    interval: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PowerLogger should reject interval <= 0 to prevent hot-loop threads."""
    monkeypatch.setattr("meshtastic.slog.slog.FeatherWriter", _FakeWriter)
    monkeypatch.setattr("meshtastic.slog.slog.threading.Thread", _FakeThread)

    meter = MagicMock()
    with pytest.raises(ValueError, match="interval must be > 0 seconds"):
        PowerLogger(meter, "unused-path", interval=interval)


@pytest.mark.unit
def test_power_logger_pmeter_setter_uses_initialized_reading_lock() -> None:
    """PowerLogger.pMeter setter should update through the lock path when initialized."""
    power_logger = object.__new__(PowerLogger)
    power_logger._reading_lock = threading.Lock()
    original_meter = MagicMock()
    replacement_meter = MagicMock()
    power_logger._p_meter = original_meter

    power_logger.pMeter = replacement_meter

    assert power_logger.pMeter is replacement_meter


@pytest.mark.unit
def test_store_current_reading_converts_legacy_aliases_when_voltage_present() -> None:
    """StoreCurrentReading should convert legacy *_mW aliases using nominal voltage."""
    meter = MagicMock()
    meter.v = 3.3
    meter.getAverageCurrentMA.return_value = 10.0
    meter.getMaxCurrentMA.return_value = 20.0
    meter.getMinCurrentMA.return_value = 5.0
    writer = MagicMock()

    power_logger = object.__new__(PowerLogger)
    power_logger.pMeter = meter
    power_logger.writer = writer
    power_logger._warned_legacy_mw_without_voltage = False
    power_logger._warned_store_current_reading_deprecation = False
    power_logger._reading_lock = threading.Lock()

    now = datetime(2026, 1, 1, 12, 0, 0)
    power_logger.storeCurrentReading(now=now)

    row = _extract_row_from_add_row_call(writer)
    assert row["time"] == now
    assert row["average_mA"] == 10.0
    assert row["max_mA"] == 20.0
    assert row["min_mA"] == 5.0
    assert row["average_mW"] == pytest.approx(33.0)
    assert row["max_mW"] == pytest.approx(66.0)
    assert row["min_mW"] == pytest.approx(16.5)
    meter.resetMeasurements.assert_called_once()


@pytest.mark.unit
def test_store_current_reading_warns_once_when_voltage_unavailable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """store_current_reading should preserve legacy behavior and warn once without voltage."""
    meter = MagicMock()
    meter.v = 0.0
    meter.getAverageCurrentMA.return_value = 11.0
    meter.getMaxCurrentMA.return_value = 21.0
    meter.getMinCurrentMA.return_value = 6.0
    writer = MagicMock()

    power_logger = object.__new__(PowerLogger)
    power_logger.pMeter = meter
    power_logger.writer = writer
    power_logger._warned_legacy_mw_without_voltage = False
    power_logger._warned_store_current_reading_deprecation = False
    power_logger._reading_lock = threading.Lock()

    with caplog.at_level(logging.WARNING):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", DeprecationWarning)
            power_logger.store_current_reading(now=datetime(2026, 1, 1, 12, 0, 0))
            power_logger.store_current_reading(now=datetime(2026, 1, 1, 12, 0, 1))
        deprecations = [
            warning
            for warning in caught
            if issubclass(warning.category, DeprecationWarning)
        ]
        assert len(deprecations) == 1
        assert "store_current_reading()" in str(deprecations[0].message)

    rows = [_extract_row_from_call(call) for call in writer.addRow.call_args_list]
    assert len(rows) == 2
    for row in rows:
        assert row["average_mW"] == row["average_mA"]
        assert row["max_mW"] == row["max_mA"]
        assert row["min_mW"] == row["min_mA"]
    assert (
        caplog.text.count("Power meter does not expose nominal voltage") == 1
    ), caplog.text


@pytest.mark.unit
def test_on_log_message_keeps_raw_and_power_on_add_row_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """StructuredLogger should preserve raw/power side-effects when addRow fails."""
    logger = object.__new__(StructuredLogger)
    logger.writer = MagicMock()
    logger.writer.addRow.side_effect = RuntimeError("writer down")
    logger.power_logger = MagicMock()
    logger.include_raw = True
    raw_file = MagicMock()
    logger.raw_file = raw_file
    logger._raw_file_lock = threading.Lock()

    line = "prefix S:B:1,2.5.0"
    with caplog.at_level(logging.WARNING):
        logger._on_log_message(line)

    assert "Failed to write structured slog row" in caplog.text
    raw_file.write.assert_called_once_with(f"{line}\n")
    logger.power_logger.storeCurrentReading.assert_called_once()


@pytest.mark.unit
def test_on_log_message_legacy_alias_delegates() -> None:
    """Legacy internal `_onLogMessage` alias should delegate to `_on_log_message`."""
    logger = object.__new__(StructuredLogger)
    logger._on_log_message = MagicMock()  # type: ignore[method-assign]

    logger._onLogMessage("sample line")

    logger._on_log_message.assert_called_once_with("sample line")


@pytest.mark.unit
def test_on_log_message_keeps_running_when_raw_write_raises_non_oserror(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """StructuredLogger should swallow non-OSError raw write failures in callback path."""
    logger = object.__new__(StructuredLogger)
    logger.writer = MagicMock()
    logger.power_logger = None
    logger.include_raw = True
    logger._raw_file_lock = threading.Lock()
    logger.raw_file = MagicMock()
    logger.raw_file.write.side_effect = RuntimeError("raw stream unavailable")

    with caplog.at_level(logging.WARNING):
        logger._on_log_message("plain log line")

    assert "Failed to write raw slog line" in caplog.text
    logger.writer.addRow.assert_called_once()


@pytest.mark.unit
def test_log_set_close_preserves_primary_exception(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """LogSet.close should raise slog close failure and log secondary power-close failure."""
    log_set = object.__new__(LogSet)
    log_set.dir_name = "tmp"
    log_set.atexit_handler = lambda: None
    log_set.slog_logger = MagicMock()
    log_set.power_logger = MagicMock()

    slog_error = ValueError("slog close failed")
    power_error = RuntimeError("power close failed")
    log_set.slog_logger.close.side_effect = slog_error
    log_set.power_logger.close.side_effect = power_error
    monkeypatch.setattr("meshtastic.slog.slog.atexit.unregister", lambda _: None)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(ValueError, match="slog close failed") as exc_info:
            log_set.close()

    assert exc_info.value.__cause__ is None
    assert "Power logger close also failed" in caplog.text
    assert log_set.slog_logger is None
    assert log_set.power_logger is None


@pytest.mark.unit
def test_structured_logger_close_raises_unsubscribe_and_logs_secondary_failures(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """StructuredLogger.close should raise unsubscribe error and log writer/raw close failures."""
    logger_obj = object.__new__(StructuredLogger)
    logger_obj._listen_glue = MagicMock()
    logger_obj.writer = MagicMock()
    logger_obj.writer.close.side_effect = RuntimeError("writer close failed")
    logger_obj._raw_file_lock = threading.Lock()
    logger_obj.raw_file = MagicMock()
    logger_obj.raw_file.close.side_effect = OSError("raw close failed")

    monkeypatch.setattr(
        "meshtastic.slog.slog.pub.unsubscribe",
        MagicMock(side_effect=ValueError("unsubscribe failed")),
    )

    with caplog.at_level(logging.WARNING):
        with pytest.raises(ValueError, match="unsubscribe failed"):
            logger_obj.close()

    assert "Failed to close raw log file" in caplog.text
    assert "Writer close also failed after unsubscribe error" in caplog.text


@pytest.mark.unit
def test_structured_logger_close_raises_writer_error_when_unsubscribe_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """StructuredLogger.close should raise writer close failures when unsubscribe succeeds."""
    logger_obj = object.__new__(StructuredLogger)
    logger_obj._listen_glue = MagicMock()
    logger_obj.writer = MagicMock()
    logger_obj.writer.close.side_effect = RuntimeError("writer close failed")
    logger_obj._raw_file_lock = threading.Lock()
    logger_obj.raw_file = MagicMock()

    monkeypatch.setattr("meshtastic.slog.slog.pub.unsubscribe", MagicMock())

    with pytest.raises(RuntimeError, match="writer close failed"):
        logger_obj.close()


@pytest.mark.unit
def test_log_set_init_raises_collision_error_after_retry_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """LogSet should raise SlogDirectoryCollisionError when unique dir retries are exhausted."""
    monkeypatch.setattr(slog_module, "LOG_DIR_COLLISION_MAX_RETRIES", 3)
    monkeypatch.setattr(slog_module, "rootDir", lambda: str(tmp_path))

    mkdir_mock = MagicMock(side_effect=FileExistsError("collision"))
    monkeypatch.setattr("meshtastic.slog.slog.Path.mkdir", mkdir_mock)

    with pytest.raises(
        slog_module.SlogDirectoryCollisionError, match="after 3 attempts"
    ):
        LogSet(MagicMock(), dir_name=None)

    assert mkdir_mock.call_count == 3


@pytest.mark.unit
def test_log_set_close_raises_power_close_error_when_slog_close_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LogSet.close should raise power logger close errors when slog close succeeds."""
    log_set = object.__new__(LogSet)
    log_set.dir_name = "tmp"
    log_set.atexit_handler = lambda: None
    log_set.slog_logger = MagicMock()
    log_set.power_logger = MagicMock()
    log_set.power_logger.close.side_effect = RuntimeError("power close failed")

    monkeypatch.setattr("meshtastic.slog.slog.atexit.unregister", lambda _: None)

    with pytest.raises(RuntimeError, match="power close failed"):
        log_set.close()

    assert log_set.slog_logger is None
    assert log_set.power_logger is None


@pytest.mark.unit
def test_power_logger_worker_finally_closes_dependencies_after_deferred_close() -> None:
    """A worker-owned deferred close must release dependencies only after sampling exits."""
    meter = MagicMock()
    writer = MagicMock()
    logger_obj = object.__new__(PowerLogger)
    logger_obj._p_meter = meter
    logger_obj.writer = writer
    logger_obj.interval = 0.001
    logger_obj._stop_event = threading.Event()
    logger_obj._sample_warning_count = 0
    logger_obj._last_sample_warning_monotonic = 0.0
    logger_obj._dependency_close_lock = threading.Lock()
    logger_obj._dependencies_closed = False
    logger_obj._deferred_dependency_close = True
    logger_obj._background_close_error = None
    store_current_reading = MagicMock()
    logger_obj._store_current_reading = (  # type: ignore[method-assign]
        store_current_reading
    )
    logger_obj._stop_event.set()

    logger_obj._logging_thread()

    store_current_reading.assert_not_called()
    meter.close.assert_called_once_with()
    writer.close.assert_called_once_with()
    assert logger_obj._dependencies_closed is True


@pytest.mark.unit
def test_power_logger_close_is_idempotent_after_dependencies_close() -> None:
    """Repeated close calls must not repeat meter/writer teardown."""
    meter = MagicMock()
    writer = MagicMock()
    logger_obj = object.__new__(PowerLogger)
    logger_obj._p_meter = meter
    logger_obj.writer = writer
    logger_obj._stop_event = threading.Event()
    logger_obj._dependency_close_lock = threading.Lock()
    logger_obj._dependencies_closed = False
    logger_obj._deferred_dependency_close = False
    logger_obj._background_close_error = None
    logger_obj.is_logging = True
    logger_obj.thread = MagicMock()
    logger_obj.thread.is_alive.return_value = False

    logger_obj.close()
    logger_obj.close()

    meter.close.assert_called_once_with()
    writer.close.assert_called_once_with()


@pytest.mark.unit
def test_power_logger_close_reports_deferred_worker_cleanup_error_once() -> None:
    """A worker-side dependency failure must reach exactly one close caller."""
    meter = MagicMock()
    meter.close.side_effect = RuntimeError("meter close failed")
    writer = MagicMock()
    logger_obj = object.__new__(PowerLogger)
    logger_obj._p_meter = meter
    logger_obj.writer = writer
    logger_obj._dependency_close_lock = threading.Lock()
    logger_obj._dependencies_closed = False
    logger_obj._background_close_error = None

    logger_obj._close_dependencies_after_worker_exit()

    with pytest.raises(RuntimeError, match="meter close failed"):
        logger_obj.close()
    logger_obj.close()

    meter.close.assert_called_once_with()
    writer.close.assert_called_once_with()
