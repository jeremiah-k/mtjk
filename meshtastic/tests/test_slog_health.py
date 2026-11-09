"""Behavioral tests for structured logging health state."""

import threading
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest

import meshtastic.slog as slog_package
from meshtastic.slog.health import (
    SlogHealthSnapshot,
    _merge_health_snapshots,
    _SlogHealthTracker,
)
from meshtastic.slog.slog import (
    POWER_SAMPLE_HEALTH_COMPONENT,
    POWER_SHUTDOWN_HEALTH_COMPONENT,
    STRUCTURED_POWER_HEALTH_COMPONENT,
    STRUCTURED_SHUTDOWN_HEALTH_COMPONENT,
    STRUCTURED_WRITER_HEALTH_COMPONENT,
    LogSet,
    PowerLogger,
    StructuredLogger,
    _PowerLoggerShutdownError,
)


@pytest.mark.unit
def test_health_tracker_retains_history_after_component_recovers() -> None:
    """A recovered component should clear degradation without losing failure history."""
    tracker = _SlogHealthTracker()

    tracker._record_failure("writer", RuntimeError("disk full"))
    failed = tracker._snapshot()
    tracker._record_success("writer")
    recovered = tracker._snapshot()

    assert failed == SlogHealthSnapshot(
        degraded=True,
        degraded_components=("writer",),
        failure_counts=(("writer", 1),),
        active_errors=(("writer", "disk full"),),
    )
    assert recovered == SlogHealthSnapshot(
        degraded=False,
        failure_counts=(("writer", 1),),
    )


@pytest.mark.unit
def test_merge_health_snapshots_combines_current_and_historical_state() -> None:
    """Aggregate health should preserve independent component counters and errors."""
    merged = _merge_health_snapshots(
        SlogHealthSnapshot(
            degraded=True,
            degraded_components=("power.sample",),
            failure_counts=(("power.sample", 2),),
            active_errors=(("power.sample", "meter failed"),),
        ),
        SlogHealthSnapshot(
            degraded=False,
            failure_counts=(("structured.writer", 1),),
        ),
    )

    assert merged.degraded
    assert merged.degraded_components == ("power.sample",)
    assert dict(merged.failure_counts) == {
        "power.sample": 2,
        "structured.writer": 1,
    }
    assert dict(merged.active_errors) == {"power.sample": "meter failed"}


@pytest.mark.unit
def test_power_logger_health_marks_failure_and_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PowerLogger should expose current degradation and cumulative sample failures."""
    power_logger = object.__new__(PowerLogger)
    outcomes: list[BaseException | None] = [RuntimeError("sample failed"), None]

    def _store_impl(_self: PowerLogger, _now: object | None = None) -> None:
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome

    monkeypatch.setattr(PowerLogger, "_store_current_reading_impl", _store_impl)

    with pytest.raises(RuntimeError, match="sample failed"):
        power_logger.storeCurrentReading()
    failed = power_logger.getHealth()

    power_logger.storeCurrentReading()
    recovered = power_logger.getHealth()

    assert failed.degraded_components == (POWER_SAMPLE_HEALTH_COMPONENT,)
    assert dict(failed.failure_counts) == {POWER_SAMPLE_HEALTH_COMPONENT: 1}
    assert not recovered.degraded
    assert dict(recovered.failure_counts) == {POWER_SAMPLE_HEALTH_COMPONENT: 1}


@pytest.mark.unit
def test_power_logger_join_timeout_records_shutdown_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker that outlives the bounded join should degrade shutdown health."""
    power_logger = object.__new__(PowerLogger)
    monkeypatch.setattr(
        PowerLogger, "_take_closed_dependency_error", lambda self: (False, None)
    )
    power_logger.is_logging = True
    power_logger._deferred_dependency_close = False
    power_logger._dependency_close_lock = threading.Lock()
    power_logger._worker_exited_loop = False
    power_logger._worker_should_close_dependencies = False
    power_logger._stop_event = MagicMock()
    power_logger.thread = MagicMock()
    power_logger.thread.is_alive.return_value = True

    with pytest.raises(_PowerLoggerShutdownError):
        power_logger.close()

    # The timed-out caller hands dependency teardown ownership to the worker.
    assert power_logger._worker_should_close_dependencies is True

    health = power_logger.getHealth()
    assert health.degraded_components == (POWER_SHUTDOWN_HEALTH_COMPONENT,)
    assert dict(health.failure_counts) == {POWER_SHUTDOWN_HEALTH_COMPONENT: 1}


@pytest.mark.unit
def test_structured_logger_health_tracks_writer_and_power_recovery() -> None:
    """Best-effort writer/power failures should remain visible until each path recovers."""
    structured_logger = object.__new__(StructuredLogger)
    structured_logger.include_raw = False
    structured_logger.writer = MagicMock()
    structured_logger.writer.addRow.side_effect = [RuntimeError("writer failed"), None]
    power_logger = MagicMock()
    power_logger.storeCurrentReading.side_effect = [RuntimeError("power failed"), None]
    structured_logger.power_logger = power_logger

    structured_logger._on_log_message("S:B:1,2.0")
    failed = structured_logger.getHealth()
    structured_logger._on_log_message("S:B:1,2.0")
    recovered = structured_logger.getHealth()

    assert set(failed.degraded_components) == {
        STRUCTURED_POWER_HEALTH_COMPONENT,
        STRUCTURED_WRITER_HEALTH_COMPONENT,
    }
    assert dict(failed.failure_counts) == {
        STRUCTURED_POWER_HEALTH_COMPONENT: 1,
        STRUCTURED_WRITER_HEALTH_COMPONENT: 1,
    }
    assert not recovered.degraded
    assert dict(recovered.failure_counts) == dict(failed.failure_counts)


@pytest.mark.unit
def test_log_set_health_survives_logger_reference_cleanup() -> None:
    """LogSet should preserve its final health snapshot after owned loggers are cleared."""
    log_set = object.__new__(LogSet)
    log_set._closed_health = SlogHealthSnapshot()
    log_set.slog_logger = cast(
        StructuredLogger,
        SimpleNamespace(
            getHealth=lambda: SlogHealthSnapshot(
                degraded=True,
                degraded_components=("structured.writer",),
                failure_counts=(("structured.writer", 3),),
                active_errors=(("structured.writer", "disk full"),),
            )
        ),
    )
    log_set.power_logger = cast(
        PowerLogger,
        SimpleNamespace(
            getHealth=lambda: SlogHealthSnapshot(
                degraded=False,
                failure_counts=(("power.sample", 1),),
            )
        ),
    )

    final_health = log_set.getHealth()
    log_set._closed_health = final_health
    log_set.slog_logger = None
    log_set.power_logger = None

    assert log_set.getHealth() == final_health


@pytest.mark.unit
def test_slog_health_snapshot_is_exported_from_package() -> None:
    """The promoted health type should be importable from ``meshtastic.slog``."""
    assert "SlogHealthSnapshot" in slog_package.__all__
    assert slog_package.SlogHealthSnapshot is SlogHealthSnapshot


@pytest.mark.unit
def test_power_logger_health_reports_dependency_shutdown_failure() -> None:
    """Power dependency teardown failures should remain observable after close."""
    power_logger = object.__new__(PowerLogger)
    power_logger._p_meter = MagicMock()
    power_logger._p_meter.close.side_effect = RuntimeError("meter close failed")
    power_logger.writer = MagicMock()
    power_logger._dependency_close_lock = threading.Lock()
    power_logger._dependencies_closed = False

    with pytest.raises(RuntimeError, match="meter close failed"):
        power_logger._close_dependencies()

    health = power_logger.getHealth()
    assert health.degraded_components == (POWER_SHUTDOWN_HEALTH_COMPONENT,)
    assert dict(health.failure_counts) == {POWER_SHUTDOWN_HEALTH_COMPONENT: 1}


@pytest.mark.unit
def test_structured_logger_health_reports_swallowed_raw_close_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Best-effort raw-file teardown failures should be visible in health state."""
    structured_logger = object.__new__(StructuredLogger)
    structured_logger._listen_glue = MagicMock()
    structured_logger.writer = MagicMock()
    structured_logger._raw_file_lock = threading.Lock()
    structured_logger.raw_file = MagicMock()
    structured_logger.raw_file.close.side_effect = RuntimeError("raw close failed")
    monkeypatch.setattr("meshtastic.slog.slog.pub.unsubscribe", MagicMock())

    structured_logger.close()

    health = structured_logger.getHealth()
    assert health.degraded_components == (STRUCTURED_SHUTDOWN_HEALTH_COMPONENT,)
    assert dict(health.active_errors) == {
        STRUCTURED_SHUTDOWN_HEALTH_COMPONENT: "raw close failed"
    }


@pytest.mark.unit
def test_log_set_close_preserves_shutdown_degradation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Aggregate health should include failures raised while owned loggers close."""
    log_set = object.__new__(LogSet)
    log_set.dir_name = "unused"
    log_set._closed_health = SlogHealthSnapshot()
    log_set.atexit_handler = MagicMock()
    slog_health = SlogHealthSnapshot(
        degraded=True,
        degraded_components=(STRUCTURED_SHUTDOWN_HEALTH_COMPONENT,),
        failure_counts=((STRUCTURED_SHUTDOWN_HEALTH_COMPONENT, 1),),
        active_errors=((STRUCTURED_SHUTDOWN_HEALTH_COMPONENT, "close failed"),),
    )
    log_set.slog_logger = cast(
        StructuredLogger,
        SimpleNamespace(
            close=MagicMock(side_effect=RuntimeError("close failed")),
            getHealth=lambda: slog_health,
        ),
    )
    log_set.power_logger = None
    monkeypatch.setattr("meshtastic.slog.slog.atexit.unregister", MagicMock())

    with pytest.raises(RuntimeError, match="close failed"):
        log_set.close()

    assert log_set.getHealth() == slog_health
    assert log_set.slog_logger is None
