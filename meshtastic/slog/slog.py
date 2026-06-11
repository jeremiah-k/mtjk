"""Classes for logging power consumption of Meshtastic devices."""

from __future__ import annotations

import atexit
import io
import logging
import os
import re
import threading
import time
import warnings
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeAlias

import parse  # type: ignore[import-untyped,import-not-found]
import platformdirs
import pyarrow as pa
from pubsub import pub

from meshtastic.mesh_interface import MeshInterface
from meshtastic.powermon import PowerMeter

from .arrow import FeatherWriter

logger = logging.getLogger(__name__)
_warned_deprecations: set[str] = set()
_warned_deprecations_lock: threading.Lock = threading.Lock()
LOG_DIR_COLLISION_MAX_RETRIES = 100


class SlogDirectoryCollisionError(FileExistsError):
    """Raised when slog cannot create a unique run directory."""

    def __init__(self, app_dir: str, attempts: int) -> None:
        super().__init__(
            f"Unable to create unique slog run directory under '{app_dir}' "
            f"after {attempts} attempts"
        )


# PyArrow typing stubs vary across versions (generic vs non-generic DataType).
# Keep the runtime alias stable while using a broad static alias for mypy
# compatibility across both stub families.
if TYPE_CHECKING:
    ArrowDataType = pa.DataType
    ArrowDataTypeLike: TypeAlias = Any
else:
    ArrowDataType = pa.DataType
    ArrowDataTypeLike = pa.DataType


def _root_dir_impl() -> str:
    """Create (if needed) and return the application's slog root directory path."""
    app_name = "meshtastic"
    app_author = "meshtastic"
    app_dir = platformdirs.user_data_dir(app_name, app_author)
    dir_name = Path(app_dir, "slogs")
    dir_name.mkdir(exist_ok=True, parents=True)
    return str(dir_name)


# COMPAT_DEPRECATE: snake_case alias for rootDir (warns once)
def root_dir() -> str:
    """Return the application's slog root directory path, creating the directory if it does not exist.

    The directory is named "slogs" and is created under the per-user application data directory for the Meshtastic app.

    Returns
    -------
    str
        Filesystem path to the "slogs" directory.
    """
    should_warn = False
    with _warned_deprecations_lock:
        if "root_dir" not in _warned_deprecations:
            _warned_deprecations.add("root_dir")
            should_warn = True
    if should_warn:
        warnings.warn(
            "root_dir() is deprecated; use rootDir() instead.",
            DeprecationWarning,
            stacklevel=2,
        )
    return _root_dir_impl()


def rootDir() -> str:
    """Preferred camelCase public alias for the slog root directory helper."""
    return _root_dir_impl()


@dataclass(init=False)
class LogDef:
    """Log definition."""

    code: str  # i.e. PM or B or whatever... see meshtastic slog documentation
    fields: list[
        tuple[str, ArrowDataTypeLike]
    ]  # A list of field names and their arrow types
    format: parse.Parser  # A format string that can be used to parse the arguments

    def __init__(self, code: str, fields: list[tuple[str, ArrowDataTypeLike]]) -> None:
        """Create a LogDef for the given code and fields and compile a parser for those fields.

        Parameters
        ----------
        code : str
            Short log code (e.g., "B", "PM", "PS").
        fields : list[tuple[str, ArrowDataTypeLike]]
            Ordered (name, type) pairs
            describing each field. String and integer Arrow fields are supported.
            Unsupported field types raise ValueError.
        """
        self.code = code
        self.fields = fields

        fmt = ""
        for idx, (field_name, field_type) in enumerate(fields):
            if idx != 0:
                fmt += ","

            if pa.types.is_string(field_type) or pa.types.is_large_string(field_type):
                suffix = ""
            elif pa.types.is_integer(field_type):
                suffix = ":d"
            else:
                raise ValueError(
                    f"Unsupported LogDef field type for '{field_name}': {field_type!r}"
                )
            fmt += "{" + field_name + suffix + "}"
        self.format = parse.compile(
            fmt
        )  # We include a catchall matcher at the end - to ignore stuff we don't understand


"""A dictionary mapping from logdef code to logdef"""
log_defs = {
    d.code: d
    for d in [
        LogDef("B", [("board_id", pa.uint32()), ("sw_version", pa.string())]),
        LogDef("PM", [("pm_mask", pa.uint64()), ("pm_reason", pa.string())]),
        LogDef("PS", [("ps_state", pa.uint32())]),
    ]
}
log_regex = re.compile(".*S:([0-9A-Za-z]+):(.*)")
POWER_LOG_SCHEMA_METADATA: dict[bytes | str, bytes | str] = {
    b"deprecated_fields": (
        b"Legacy *_mW fields are deprecated aliases. Prefer *_mA for current. "
        b"When nominal voltage is available, *_mW stores converted power in mW."
    ),
}
POWER_LOGGER_JOIN_TIMEOUT = 1.0
INTERVAL_REQUIRED_MESSAGE = "interval must be > 0 seconds"
DIR_NAME_REQUIRED_MESSAGE = "dir_name must be a non-empty path when provided"
SAMPLE_FAILURE_WARNING_COOLDOWN_SECONDS = 5.0
SAMPLE_FAILURE_WARNING_BURST_COUNT = 3


class PowerLogger:
    """Logs current watts reading periodically using PowerMeter and ArrowWriter."""

    def __init__(
        self,
        p_meter: PowerMeter | None = None,
        file_path: str | None = None,
        interval: float = 0.002,
        **compat_kwargs: Any,
    ) -> None:
        """Create a PowerLogger that records periodic power readings from a PowerMeter into a Feather file and starts its background logging thread.

        Parameters
        ----------
        p_meter : PowerMeter | None
            Source of power measurements; its snapshot and reset methods will be used.
        file_path : str | None
            Path to the output Feather file where readings will be written.
        interval : float
            Time in seconds between automatic samples. Must be > 0 (default 0.002).
        **compat_kwargs : Any
            Legacy keyword compatibility:
            - `pMeter`: historical constructor name for `p_meter`.

        Raises
        ------
        ValueError
            If interval is not a positive number.
        """
        legacy_p_meter = compat_kwargs.pop("pMeter", None)
        if compat_kwargs:
            unexpected = ", ".join(sorted(compat_kwargs.keys()))
            raise TypeError(f"Unexpected keyword argument(s): {unexpected}")
        if (
            p_meter is not None
            and legacy_p_meter is not None
            and p_meter is not legacy_p_meter
        ):
            raise TypeError("Specify only one of 'p_meter' or legacy 'pMeter'")
        if p_meter is None:
            p_meter = legacy_p_meter
        if p_meter is None:
            raise TypeError("PowerLogger requires a PowerMeter instance")
        if file_path is None:
            raise TypeError("PowerLogger requires file_path")
        if interval <= 0:
            raise ValueError(INTERVAL_REQUIRED_MESSAGE)
        self._p_meter = p_meter
        self.writer = FeatherWriter(file_path)
        power_schema_fields: list[pa.Field[Any]] = [
            pa.field("time", pa.timestamp("us")),
            pa.field("average_mA", pa.float64()),
            pa.field("max_mA", pa.float64()),
            pa.field("min_mA", pa.float64()),
            pa.field("average_mW", pa.float64()),
            pa.field("max_mW", pa.float64()),
            pa.field("min_mW", pa.float64()),
        ]
        try:
            self.writer.setSchema(
                pa.schema(
                    power_schema_fields,
                    metadata=POWER_LOG_SCHEMA_METADATA,
                )
            )
        except Exception:
            try:
                self.writer.close()
            except Exception as close_exc:  # noqa: BLE001 - preserve setup failure
                logger.warning(
                    "Failed to close power writer after schema setup failure: %s",
                    close_exc,
                    exc_info=True,
                )
            raise
        self.interval = interval
        self._warned_legacy_mw_without_voltage = False
        self._warned_store_current_reading_deprecation = False
        self._deprecation_warning_lock = threading.Lock()
        self._reading_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._sample_warning_count = 0
        self._last_sample_warning_monotonic = 0.0
        self.is_logging = True
        self.thread = threading.Thread(
            target=self._logging_thread, name="PowerLogger", daemon=True
        )
        self.thread.start()

    @property
    def pMeter(self) -> PowerMeter:
        """Public access to the underlying PowerMeter."""
        return self._p_meter

    @pMeter.setter
    def pMeter(self, value: PowerMeter) -> None:
        """Set the underlying PowerMeter."""
        reading_lock = getattr(self, "_reading_lock", None)
        if reading_lock is None:
            # Defensive path for unusual construction/testing flows where the
            # lock is not initialized yet.
            self._p_meter = value
            return
        with reading_lock:
            self._p_meter = value

    # COMPAT_STABLE_SHIM: alias for pMeter property
    @property
    def p_meter(self) -> PowerMeter:
        """Legacy compatibility alias for pMeter."""
        return self.pMeter

    # COMPAT_STABLE_SHIM: alias for pMeter setter
    @p_meter.setter
    def p_meter(self, value: PowerMeter) -> None:
        """Legacy compatibility alias for pMeter setter."""
        self.pMeter = value

    def _nominal_voltage_v(self, meter: PowerMeter | None = None) -> float | None:
        """Return nominal supply voltage in volts when available on the power meter."""
        source_meter = self._p_meter if meter is None else meter
        raw_v = getattr(source_meter, "v", None)
        if (
            isinstance(raw_v, (int, float))
            and not isinstance(raw_v, bool)
            and raw_v > 0
        ):
            return float(raw_v)
        return None

    def _store_current_reading(self, now: datetime | None = None) -> None:
        """Capture a snapshot of current power measurements and append it to the writer.

        If `now` is provided it is used as the timestamp; otherwise the current system time is used.
        The recorded row contains `time`, `average_mA`, `max_mA`, and `min_mA`.
        Legacy `*_mW` aliases are retained for compatibility. When a nominal
        voltage is available on the meter (`p_meter.v`), those aliases are
        converted to milliwatts via ``mW = mA * V``. If no voltage is available,
        aliases fall back to the legacy behavior (same numeric value as mA)
        and a one-time warning is emitted. After sampling, the PowerMeter's
        measurements are reset and the row is written via the writer.

        Parameters
        ----------
        now : datetime | None
            Optional timestamp to use for the recorded row. (Default value = None)
        """
        with self._reading_lock:
            meter = self._p_meter
            if now is None:
                now = datetime.now()
            primary_exc: Exception | None = None
            try:
                average_mA = meter.getAverageCurrentMA()
                max_mA = meter.getMaxCurrentMA()
                min_mA = meter.getMinCurrentMA()
                nominal_voltage = self._nominal_voltage_v(meter)
                if nominal_voltage is None:
                    average_mW = average_mA
                    max_mW = max_mA
                    min_mW = min_mA
                    if not self._warned_legacy_mw_without_voltage:
                        logger.warning(
                            "Power meter does not expose nominal voltage; storing legacy *_mW aliases with mA-equivalent values."
                        )
                        self._warned_legacy_mw_without_voltage = True
                else:
                    average_mW = average_mA * nominal_voltage
                    max_mW = max_mA * nominal_voltage
                    min_mW = min_mA * nominal_voltage
                d = {
                    "time": now,
                    "average_mA": average_mA,
                    "max_mA": max_mA,
                    "min_mA": min_mA,
                    # Historical field names kept as aliases to avoid schema breakage.
                    # Prefer *_mA for current values in new consumers.
                    "average_mW": average_mW,
                    "max_mW": max_mW,
                    "min_mW": min_mW,
                }
                self.writer.addRow(d)
            except Exception as exc:
                primary_exc = exc
                raise
            finally:
                try:
                    meter.resetMeasurements()
                except Exception as reset_exc:  # noqa: BLE001 - preserve primary error
                    if primary_exc is not None:
                        logger.warning(
                            "Failed to reset power meter after sample/write error: %s",
                            reset_exc,
                            exc_info=True,
                        )
                    else:
                        raise

    def storeCurrentReading(self, now: datetime | None = None) -> None:
        """Preferred camelCase public API; see `_store_current_reading`."""
        self._store_current_reading(now)

    # COMPAT_DEPRECATE: snake_case alias for storeCurrentReading (warns once)
    def store_current_reading(self, now: datetime | None = None) -> None:
        """Use `storeCurrentReading()` instead."""
        deprecation_warning_lock = getattr(self, "_deprecation_warning_lock", None)
        if deprecation_warning_lock is None:
            # Fall back to a shared lock for unusually-constructed test doubles.
            deprecation_warning_lock = _warned_deprecations_lock
            self._deprecation_warning_lock = deprecation_warning_lock
        with deprecation_warning_lock:
            if not self._warned_store_current_reading_deprecation:
                warnings.warn(
                    "store_current_reading() is deprecated; use storeCurrentReading() instead.",
                    DeprecationWarning,
                    stacklevel=2,
                )
                self._warned_store_current_reading_deprecation = True
        self._store_current_reading(now)

    def _logging_thread(self) -> None:
        """Background thread for logging periodic current readings."""
        while not self._stop_event.is_set():
            try:
                self._store_current_reading()
                self._sample_warning_count = 0
                self._last_sample_warning_monotonic = 0.0
            except Exception as exc:  # noqa: BLE001 - keep sampler alive
                self._sample_warning_count += 1
                now_monotonic = time.monotonic()
                should_warn = (
                    self._sample_warning_count <= SAMPLE_FAILURE_WARNING_BURST_COUNT
                    or (
                        now_monotonic - self._last_sample_warning_monotonic
                        >= SAMPLE_FAILURE_WARNING_COOLDOWN_SECONDS
                    )
                )
                if should_warn:
                    logger.warning("PowerLogger sample failed: %s", exc, exc_info=True)
                    self._last_sample_warning_monotonic = now_monotonic
                else:
                    logger.debug(
                        "PowerLogger sample failed (suppressed warning #%d): %s",
                        self._sample_warning_count,
                        exc,
                    )
            self._stop_event.wait(self.interval)

    def close(self) -> None:
        """Close the PowerLogger and stop logging."""
        if self.is_logging:
            self.is_logging = False
            self._stop_event.set()
            if threading.current_thread() is not self.thread:
                self.thread.join(timeout=POWER_LOGGER_JOIN_TIMEOUT)
                if self.thread.is_alive():
                    logger.warning(
                        "PowerLogger background thread did not stop within %.1fs; continuing teardown.",
                        POWER_LOGGER_JOIN_TIMEOUT,
                    )
            else:
                logger.debug(
                    "PowerLogger.close() called from logging thread; skipping self-join."
                )
            meter_close_exc: Exception | None = None
            try:
                self._p_meter.close()
            except Exception as exc:  # noqa: BLE001 - preserve primary close failure
                meter_close_exc = exc
            try:
                self.writer.close()
            except Exception as writer_close_exc:  # noqa: BLE001 - secondary cleanup
                if meter_close_exc is not None:
                    logger.warning(
                        "PowerLogger writer close failed after power meter close error: %s",
                        writer_close_exc,
                        exc_info=True,
                    )
                else:
                    raise
            if meter_close_exc is not None:
                raise meter_close_exc


# FIXME move these defs somewhere else
TOPIC_MESHTASTIC_LOG_LINE = "meshtastic.log.line"


class StructuredLogger:
    """Sniffs device logs for structured log messages, extracts those into apache arrow format.

    Also writes the raw log messages to raw.txt.
    """

    def __init__(
        self,
        client: MeshInterface,
        dir_path: str,
        power_logger: PowerLogger | None = None,
        include_raw: bool = True,
    ) -> None:
        """Create a StructuredLogger that monitors device logs and writes structured entries to an Arrow writer.

        Parameters
        ----------
        client : MeshInterface
            Source of device log lines to monitor.
        dir_path : str
            Filesystem directory where the slog Arrow dataset and optional raw.txt are created.
        power_logger : PowerLogger | None
            If provided, used to record a power sample with each structured log entry. (Default value = None)
        include_raw : bool
            If True, include a "raw" string field in the schema and write raw log lines to raw.txt. (Default value = True)

        Raises
        ------
        Exception
            If any setup step fails (e.g., file creation, schema setting, subscription).
        """
        self.client = client
        self.power_logger = power_logger

        # Setup the arrow writer (and its schema)
        self.writer = FeatherWriter(os.path.join(dir_path, "slog"))
        all_fields: list[tuple[str, ArrowDataTypeLike]] = [
            field for logdef in log_defs.values() for field in logdef.fields
        ]

        self.include_raw = include_raw
        if self.include_raw:
            all_fields.append(("raw", pa.string()))

        # Use timestamp as the first column
        all_fields.insert(0, ("time", pa.timestamp("us")))

        self._raw_file_lock = threading.Lock()
        self.raw_file: io.TextIOWrapper | None = None

        # We need a closure here because the subscription API is very strict about exact arg matching
        def listen_glue(
            line: str,
            interface: MeshInterface,  # pylint: disable=unused-argument  # noqa: ARG001
        ) -> None:
            """Glue function to connect pubsub events to the StructuredLogger.

            Parameters
            ----------
            line : str
                The log line received from the device.
            interface : MeshInterface
                The interface that generated the log line (unused).
            """
            self._on_log_message(line)

        self._listen_glue = (
            listen_glue  # we must save this so it doesn't get garbage collected
        )
        try:
            # pass in our name->type tuples as pa.fields
            self.writer.setSchema(
                pa.schema([pa.field(name, typ) for name, typ in all_fields])
            )
            if self.include_raw:
                self.raw_file = open(  # pylint: disable=consider-using-with
                    os.path.join(dir_path, "raw.txt"), "w", encoding="utf8"
                )
            pub.subscribe(self._listen_glue, TOPIC_MESHTASTIC_LOG_LINE)
        except Exception:
            # If setup fails at any step, close file handles before re-raising.
            try:
                if self.raw_file:
                    self.raw_file.close()
            except Exception as raw_close_exc:  # noqa: BLE001 - preserve setup error
                logger.warning(
                    "Failed to close raw slog file after setup failure: %s",
                    raw_close_exc,
                    exc_info=True,
                )
            try:
                self.writer.close()
            except Exception as writer_close_exc:  # noqa: BLE001 - preserve setup error
                logger.warning(
                    "Failed to close slog writer after setup failure: %s",
                    writer_close_exc,
                    exc_info=True,
                )
            raise

    def close(self) -> None:
        """Shut down the StructuredLogger and release its resources.

        Unsubscribes the log listener, closes the Arrow writer, and safely closes and clears the
        raw log file reference while holding the internal lock so concurrent writers cannot race
        with shutdown.
        """
        unsubscribe_exc: Exception | None = None
        writer_close_exc: Exception | None = None
        try:
            pub.unsubscribe(self._listen_glue, TOPIC_MESHTASTIC_LOG_LINE)
        except Exception as exc:  # noqa: BLE001 - preserve as primary error
            unsubscribe_exc = exc
        try:
            self.writer.close()
        except Exception as exc:  # noqa: BLE001 - capture secondary close failure
            writer_close_exc = exc
        with self._raw_file_lock:
            f = self.raw_file
            self.raw_file = None  # mark that we are shutting down
        if f:
            try:
                f.close()  # Close the raw.txt file
            except Exception as exc:  # noqa: BLE001 - best-effort cleanup
                logger.warning("Failed to close raw log file: %s", exc, exc_info=True)
        if unsubscribe_exc is not None:
            if writer_close_exc is not None:
                logger.warning(
                    "Writer close also failed after unsubscribe error: %s",
                    writer_close_exc,
                    exc_info=True,
                )
            raise unsubscribe_exc
        if writer_close_exc is not None:
            raise writer_close_exc

    def _on_log_message(self, line: str) -> None:
        """Process a single raw log line, extract any structured slog fields, and persist the resulting record.

        Parses the input line for a structured slog. If parsing yields fields, adds a "time"
        timestamp and writes the record to the configured Arrow writer. If raw logging is enabled,
        includes the original raw line in the record and appends it to the raw log file. If a power
        logger is present, records a power measurement using the exact same timestamp as the
        written slog record. Unknown or unparsable structured slog lines are logged as warnings.

        Parameters
        ----------
        line : str
            The raw log line to process.
        """

        di = {}  # the dictionary of the fields we found to log

        m = log_regex.match(line)
        if m:
            src = m.group(1)
            args = m.group(2)
            logger.debug("SLog %s, args: %s", src, args)

            d = log_defs.get(src)
            if d:
                last_field = d.fields[-1]
                last_is_str = last_field[1] == pa.string()
                if last_is_str:
                    args += " "
                    # append a space so that if the last arg is an empty str
                    # it will still be accepted as a match for a str

                r = d.format.parse(args)  # get the values with the correct types
                if r:
                    di = r.named  # pyright: ignore[reportAttributeAccessIssue]
                    if last_is_str:
                        di[last_field[0]] = di[
                            last_field[0]
                        ].strip()  # remove the trailing space we added
                        if di[last_field[0]] == "":
                            # If the last field is an empty string, remove it
                            del di[last_field[0]]
                else:
                    logger.warning("Failed to parse slog %s with %s", line, d.format)
            else:
                logger.warning("Unknown Structured Log: %s", line)

        # Store our structured log record
        if di or self.include_raw:
            has_structured_data = bool(di)
            now = datetime.now()
            di["time"] = now
            if self.include_raw:
                di["raw"] = line
            try:
                self.writer.addRow(di)
            except Exception as exc:  # noqa: BLE001 - best-effort logging path
                logger.warning(
                    "Failed to write structured slog row: %s", exc, exc_info=True
                )

            # If we have a sibling power logger, make sure we have a power measurement with the EXACT same timestamp
            if self.power_logger and has_structured_data:
                # Intentional best-effort behavior: keep power samples flowing even
                # when addRow fails so timing correlation remains usable (see
                # test_on_log_message_keeps_raw_and_power_on_add_row_failure).
                try:
                    self.power_logger.storeCurrentReading(now)
                except Exception as exc:  # noqa: BLE001 - best-effort logging path
                    logger.warning(
                        "Failed to write corresponding power sample: %s",
                        exc,
                        exc_info=True,
                    )

        # Only acquire lock and write if raw logging is enabled
        if self.include_raw:
            with self._raw_file_lock:
                if self.raw_file:
                    try:
                        self.raw_file.write(line + "\n")  # Write the raw log
                    except Exception as exc:  # noqa: BLE001 - keep callbacks resilient
                        logger.warning(
                            "Failed to write raw slog line: %s",
                            exc,
                            exc_info=True,
                        )

    # COMPAT_STABLE_SHIM: historical internal helper name used by legacy integrations.
    def _onLogMessage(self, line: str) -> None:  # pylint: disable=invalid-name
        """Compatibility alias for _on_log_message()."""
        self._on_log_message(line)


class LogSet:
    """A complete set of meshtastic log/metadata for a particular run."""

    def __init__(
        self,
        client: MeshInterface,
        dir_name: str | None = None,
        power_meter: PowerMeter | None = None,
    ) -> None:
        """Create a LogSet: prepare a directory for slog files, start structured slogging, and optionally start power logging.

        If dir_name is not provided, a timestamped directory is created under the slog root and a
        "latest" symlink is updated to point to it. A StructuredLogger is created and bound to the
        provided client; if power_meter is supplied, a PowerLogger is created that writes to a
        "power" subdirectory. An atexit handler pointing to this instance's close() is registered
        for later teardown.

        Parameters
        ----------
        client : MeshInterface
            MeshInterface client whose log lines will be
            monitored and recorded.
        dir_name : str | None
            Path for storing logs; when omitted, a new
            timestamped directory is created under the slog root and "latest"
            is updated to point to it. (Default value = None)
        power_meter : PowerMeter | None
            When provided, a PowerLogger is
            started to record power samples alongside slog entries. (Default value = None)

        Raises
        ------
        Exception
            If structured logger initialization fails.
        """

        if dir_name is None:
            app_dir = rootDir()
            base_stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            app_time_dir = Path(app_dir, base_stamp)
            for attempt in range(LOG_DIR_COLLISION_MAX_RETRIES):
                candidate = (
                    Path(app_dir, base_stamp)
                    if attempt == 0
                    else Path(app_dir, f"{base_stamp}-{attempt}")
                )
                try:
                    candidate.mkdir(exist_ok=False)
                    app_time_dir = candidate
                    break
                except FileExistsError:
                    continue
            else:
                raise SlogDirectoryCollisionError(
                    app_dir,
                    LOG_DIR_COLLISION_MAX_RETRIES,
                )
            dir_name = str(app_time_dir)

            # Also make a 'latest' directory that always points to the most recent logs
            latest_dir = Path(app_dir, "latest")
            can_update_latest_link = True
            if latest_dir.is_symlink() or latest_dir.exists():
                try:
                    if latest_dir.is_symlink() or latest_dir.is_file():
                        latest_dir.unlink()
                    elif latest_dir.is_dir():
                        latest_dir.rmdir()
                except OSError as ex:
                    logger.warning(
                        "Skipping latest symlink update because existing path %s could not be removed non-destructively: %s",
                        latest_dir,
                        ex,
                    )
                    can_update_latest_link = False

            # symlink might fail on some platforms, if it does fail silently
            try:
                if can_update_latest_link:
                    latest_dir.symlink_to(dir_name, target_is_directory=True)
            except OSError as ex:
                logger.debug("Unable to update latest slog symlink: %s", ex)
        elif dir_name == "":
            raise ValueError(DIR_NAME_REQUIRED_MESSAGE)
        else:
            Path(dir_name).mkdir(exist_ok=True, parents=True)

        self.dir_name = dir_name

        logger.info("Writing slogs to %s", dir_name)

        self.power_logger: PowerLogger | None = None
        self.slog_logger: StructuredLogger | None = None

        if power_meter is not None:
            self.power_logger = PowerLogger(
                power_meter, os.path.join(self.dir_name, "power")
            )

        try:
            self.slog_logger = StructuredLogger(
                client, self.dir_name, power_logger=self.power_logger
            )
        except Exception:
            if self.power_logger:
                try:
                    self.power_logger.close()
                except Exception as close_exc:  # noqa: BLE001 - preserve original
                    logger.warning(
                        "Ignoring secondary error while closing power logger during startup failure: %s",
                        close_exc,
                    )
                finally:
                    self.power_logger = None
            raise

        # Store a lambda so we can find it again to unregister
        self.atexit_handler = lambda: self.close()  # pylint: disable=unnecessary-lambda
        atexit.register(self.atexit_handler)

    def close(self) -> None:
        """Shuts down the log set and releases associated resources.

        If a structured logger is present, unregisters the atexit handler, closes the
        structured logger and the optional power logger, and clears the internal slog
        logger reference.
        """

        if self.slog_logger:
            logger.info("Closing slogs in %s", self.dir_name)
            atexit.unregister(
                self.atexit_handler
            )  # docs say it will silently ignore if not found
            slog_close_exc: Exception | None = None
            power_close_exc: Exception | None = None
            try:
                self.slog_logger.close()
            except Exception as exc:  # noqa: BLE001 - preserve chaining below
                slog_close_exc = exc
            finally:
                self.slog_logger = None
            try:
                if self.power_logger:
                    self.power_logger.close()
            except Exception as exc:  # noqa: BLE001 - preserve chaining below
                power_close_exc = exc
            finally:
                self.power_logger = None

            if slog_close_exc is not None:
                if power_close_exc is not None:
                    # Python 3.10 has no ExceptionGroup, so preserve the
                    # primary error and log the secondary one.
                    logger.warning(
                        "Power logger close also failed: %s",
                        power_close_exc,
                        exc_info=True,
                    )
                raise slog_close_exc
            if power_close_exc is not None:
                raise power_close_exc
