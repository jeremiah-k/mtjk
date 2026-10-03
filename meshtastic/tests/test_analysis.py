"""Test analysis processing."""

import logging
import os
import sys
from pathlib import Path
from typing import Any, NoReturn, cast

import pytest

OPTIONAL_ANALYSIS_DEPS = {
    "dash",
    "dash_bootstrap_components",
    "matplotlib",
    "numpy",
    "pandas",
    "plotly",
    "pyarrow",
}
ADDRESS_IN_USE_ERROR = "address already in use"

try:
    import numpy as np
    import pandas as pd
    from pyarrow import feather

    # Depends upon matplotlib & other packages in uv's analysis group, not installed by default
    from meshtastic import powermon_pb2
    from meshtastic.analysis import __main__ as analysis_main
    from meshtastic.analysis.__main__ import (
        _cli_exit,
        choose_power_column,
        choosePowerColumn,
        create_argparser,
        create_dash,
        get_board_info,
        get_pmon_raises,
        getBoardInfo,
        getPmonRaises,
        main,
        read_pandas,
        to_pmon_names,
    )
    from meshtastic.protobuf import mesh_pb2

    # Import private function for testing
    _is_loopback_host = analysis_main._is_loopback_host
except ModuleNotFoundError as exc:
    missing = (exc.name or "").split(".", maxsplit=1)[0]
    if missing in OPTIONAL_ANALYSIS_DEPS:
        pytest.skip("Can't import meshtastic.analysis", allow_module_level=True)
    raise


def _single_bit_power_states() -> tuple[int, int]:
    """Return two deterministic single-bit PowerMon state values."""
    single_bits = sorted(
        state.number
        for state in powermon_pb2.PowerMon.State.DESCRIPTOR.values
        if state.number > 0 and (state.number & (state.number - 1)) == 0
    )
    if len(single_bits) < 2:
        pytest.skip("PowerMon enum must expose at least two single-bit states")
    return single_bits[0], single_bits[1]


@pytest.mark.unit
def test_analysis(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test analysis processing."""

    cur_dir = Path(__file__).resolve().parent
    slog_input_dir = str(cur_dir / "slog-test-input")

    monkeypatch.setattr(
        sys, "argv", ["fakescriptname", "--no-server", "--slog", slog_input_dir]
    )
    monkeypatch.setattr(logging.getLogger(), "propagate", True)

    with caplog.at_level(logging.DEBUG):
        main()

    assert "Exiting without running visualization server" in caplog.text


@pytest.mark.unit
def test_choose_power_column_falls_back_to_new_when_legacy_all_null() -> None:
    """choose_power_column should use corrected column when legacy values are all null."""
    frame = pd.DataFrame({"average_mW": [None, None], "average_mA": [1.2, 1.5]})
    assert choose_power_column(frame, "average_mW", "average_mA") == "average_mA"


@pytest.mark.unit
def test_main_routes_load_errors_through_cli_exit(
    monkeypatch: pytest.MonkeyPatch,
    cli_exit_capture: dict[str, Any],
) -> None:
    """main() should emit a graceful CLI error when slog loading fails."""

    def _fake_create_dash(*, slog_path: str) -> NoReturn:
        _ = slog_path
        raise ValueError("bad slog")  # noqa: TRY003

    monkeypatch.setattr(analysis_main, "create_dash", _fake_create_dash)
    monkeypatch.setattr(sys, "argv", ["fakescriptname", "--slog", os.devnull])

    with pytest.raises(SystemExit):
        main()

    assert "Error loading slog data: bad slog" in cli_exit_capture["message"]
    assert cli_exit_capture["code"] == 1


@pytest.mark.unit
def test_main_routes_server_startup_errors_through_cli_exit(
    monkeypatch: pytest.MonkeyPatch,
    cli_exit_capture: dict[str, Any],
) -> None:
    """main() should emit a graceful CLI error when Dash server startup fails."""

    class _FailingApp:
        def run(self, *, debug: bool, host: str, port: int) -> NoReturn:
            """Simulate Dash server startup failure."""
            _ = (debug, host, port)
            raise OSError(ADDRESS_IN_USE_ERROR)

    def _fake_create_dash(*, slog_path: str) -> _FailingApp:
        _ = slog_path
        return _FailingApp()

    monkeypatch.setattr(analysis_main, "create_dash", _fake_create_dash)
    monkeypatch.setattr(sys, "argv", ["fakescriptname", "--slog", os.devnull])

    with pytest.raises(SystemExit):
        main()

    assert "Error starting Dash server on 127.0.0.1:" in cli_exit_capture["message"]
    assert ADDRESS_IN_USE_ERROR in cli_exit_capture["message"]
    assert cli_exit_capture["code"] == 1


@pytest.mark.unit
def test_get_board_info_requires_board_id_column() -> None:
    """get_board_info should fail with a clear error when board_id column is missing."""
    frame = pd.DataFrame({"sw_version": ["2.5.0"]})
    with pytest.raises(ValueError, match="No board_id rows found in dslog"):
        get_board_info(frame)


@pytest.mark.unit
def test_get_board_info_requires_non_null_board_id() -> None:
    """get_board_info should fail with a clear error when board_id is all null."""
    frame = pd.DataFrame({"sw_version": ["2.5.0"], "board_id": [None]})
    with pytest.raises(ValueError, match="No board_id rows found in dslog"):
        get_board_info(frame)


@pytest.mark.unit
def test_get_board_info_rejects_non_integral_float_board_id() -> None:
    """get_board_info should reject non-integral float board_id values."""
    frame = pd.DataFrame({"sw_version": ["2.5.0"], "board_id": [1.5]})
    with pytest.raises(ValueError, match="Invalid board_id value in dslog"):
        get_board_info(frame)


@pytest.mark.unit
def test_get_board_info_rejects_decimal_string_board_id() -> None:
    """get_board_info should reject non-integer string board_id values."""
    frame = pd.DataFrame({"sw_version": ["2.5.0"], "board_id": ["1.5"]})
    with pytest.raises(ValueError, match="Invalid board_id value in dslog"):
        get_board_info(frame)


@pytest.mark.unit
def test_get_board_info_rejects_numpy_bool_board_id() -> None:
    """get_board_info should reject numpy boolean board_id values."""
    frame = pd.DataFrame({"sw_version": ["2.5.0"], "board_id": [np.bool_(True)]})
    with pytest.raises(ValueError, match="Invalid board_id value in dslog"):
        get_board_info(frame)


@pytest.mark.unit
def test_to_pmon_names_maps_valid_states() -> None:
    """to_pmon_names should convert valid power-monitor state integers to name strings."""
    bit1, bit2 = _single_bit_power_states()
    result = to_pmon_names([bit1, bit2, bit1 | bit2])
    assert isinstance(result, list)
    assert len(result) == 3
    assert result[0] == powermon_pb2.PowerMon.State.Name(cast(Any, bit1))
    assert result[1] == powermon_pb2.PowerMon.State.Name(cast(Any, bit2))
    assert result[2] == (
        f"{powermon_pb2.PowerMon.State.Name(cast(Any, bit1))}"
        f"|{powermon_pb2.PowerMon.State.Name(cast(Any, bit2))}"
    )


@pytest.mark.unit
def test_to_pmon_names_handles_invalid_values() -> None:
    """to_pmon_names should return None for invalid state values."""
    known_values = {
        state.number for state in powermon_pb2.PowerMon.State.DESCRIPTOR.values
    }
    invalid_value = next(
        (1 << i for i in range(63) if (1 << i) not in known_values),
        0xFFFFFFFF,
    )
    assert invalid_value not in known_values
    result = to_pmon_names([invalid_value, -1, "invalid"])
    assert result == [None, None, None]


@pytest.mark.unit
def test_to_pmon_names_handles_none_state() -> None:
    """to_pmon_names should return None when state name is 'None'."""
    # State 0 typically maps to 'None' in the enum
    result = to_pmon_names([0])
    assert result == [None]


@pytest.mark.unit
def test_read_pandas_preserves_nullable_dtypes(tmp_path: Path) -> None:
    """read_pandas should map Arrow types to pandas nullable dtypes."""
    # Create a test DataFrame with various types
    test_data = pd.DataFrame(
        {
            "int_col": pd.array([1, 2, None], dtype=pd.Int32Dtype()),
            "bool_col": pd.array([True, False, None], dtype=pd.BooleanDtype()),
            "str_col": pd.Series(["a", "b", None], dtype="string[pyarrow]"),
        }
    )

    # Write to feather
    test_file = tmp_path / "test.feather"
    feather.write_feather(test_data, str(test_file))

    # Read back
    result = read_pandas(str(test_file))

    # Check that nullable dtypes are preserved
    assert isinstance(result["int_col"].dtype, pd.Int32Dtype)
    assert isinstance(result["bool_col"].dtype, pd.BooleanDtype)
    assert pd.api.types.is_string_dtype(result["str_col"])


@pytest.mark.unit
def test_get_pmon_raises_requires_time_column() -> None:
    """get_pmon_raises should fail clearly when the time column is missing."""
    dslog = pd.DataFrame({"pm_mask": [1, 2, 3]})
    with pytest.raises(ValueError, match="No time column found in slog"):
        get_pmon_raises(dslog)


@pytest.mark.unit
def test_get_pmon_raises_rejects_uint64_overflow() -> None:
    """get_pmon_raises should reject values beyond representable range.

    Large integers are converted to float64 by pd.to_numeric, triggering
    the float64 exactness check before the uint64 range check.
    """
    uint64_max = np.iinfo(np.uint64).max
    overflow_val = int(uint64_max) + 1
    dslog = pd.DataFrame(
        {
            "time": [1.0, 2.0],
            "pm_mask": [0, overflow_val],
        }
    )
    with pytest.raises(
        ValueError,
        match="pm_mask contains values that cannot be exactly represented in float64",
    ):
        get_pmon_raises(dslog)


@pytest.mark.unit
def test_get_pmon_raises_rejects_large_int_as_float64_error() -> None:
    """get_pmon_raises should reject large integer values via float64 error when they exceed exact int range."""
    overflow_val = float((1 << 53) + 2)
    dslog = pd.DataFrame(
        {
            "time": [1.0, 2.0],
            "pm_mask": [0, overflow_val],
        }
    )
    with pytest.raises(
        ValueError,
        match="pm_mask contains values that cannot be exactly represented in float64",
    ):
        get_pmon_raises(dslog)


@pytest.mark.unit
def test_get_pmon_raises_rejects_negative_masks() -> None:
    """get_pmon_raises should reject negative pm_mask values."""
    dslog = pd.DataFrame(
        {
            "time": [1.0, 2.0],
            "pm_mask": [0, -1],
        }
    )
    with pytest.raises(ValueError, match="pm_mask contains negative values"):
        get_pmon_raises(dslog)


@pytest.mark.unit
def test_get_pmon_raises_rejects_float_masks_beyond_exact_int_range() -> None:
    """get_pmon_raises should reject float pm_mask values beyond 2**53."""
    dslog = pd.DataFrame(
        {
            "time": [1.0, 2.0],
            "pm_mask": [0.0, float((1 << 53) + 2)],
        }
    )
    with pytest.raises(
        ValueError,
        match="pm_mask contains values that cannot be exactly represented in float64",
    ):
        get_pmon_raises(dslog)


@pytest.mark.unit
def test_get_pmon_raises_rejects_overflowing_masks() -> None:
    """get_pmon_raises should reject overflowing float masks before uint64 cast."""
    dslog = pd.DataFrame(
        {
            "time": [1.0, 2.0],
            "pm_mask": [0, float(np.iinfo(np.uint64).max) * 2.0],
        }
    )
    with pytest.raises(
        ValueError,
        match="pm_mask contains values that cannot be exactly represented in float64",
    ):
        get_pmon_raises(dslog)


@pytest.mark.unit
def test_get_pmon_raises_handles_multibit_raise_mask() -> None:
    """get_pmon_raises should decode simultaneous multi-bit raise masks."""
    bit1, bit2 = _single_bit_power_states()
    dslog = pd.DataFrame(
        {
            "time": [1.0, 2.0],
            "pm_mask": [0, bit1 | bit2],
        }
    )

    result = get_pmon_raises(dslog)

    assert len(result) == 1
    assert result["time"].tolist() == [2.0]
    assert result["pm_raises"].tolist() == [
        (
            f"{powermon_pb2.PowerMon.State.Name(cast(Any, bit1))}"
            f"|{powermon_pb2.PowerMon.State.Name(cast(Any, bit2))}"
        )
    ]


@pytest.mark.unit
def test_is_loopback_host_recognizes_localhost() -> None:
    """_is_loopback_host should recognize common loopback addresses."""
    assert _is_loopback_host("localhost")
    assert _is_loopback_host("127.0.0.1")
    assert _is_loopback_host("::1")
    assert _is_loopback_host("[::1]")
    assert not _is_loopback_host("192.168.1.1")
    assert not _is_loopback_host("0.0.0.0")  # noqa: S104
    assert not _is_loopback_host("example.com")


@pytest.mark.unit
def test_is_loopback_host_handles_invalid_addresses() -> None:
    """_is_loopback_host should handle invalid addresses gracefully."""
    assert not _is_loopback_host("not-an-ip")
    assert not _is_loopback_host("")


@pytest.mark.unit
def test_create_argparser_returns_parser() -> None:
    """create_argparser should return an ArgumentParser with expected arguments."""
    parser = create_argparser()
    assert parser is not None

    # Test parsing with various arguments
    args = parser.parse_args(["--no-server"])
    assert args.no_server is True

    args = parser.parse_args(["--slog", "/some/path"])
    assert args.slog == "/some/path"

    args = parser.parse_args(["--port", "9000"])
    assert args.port == 9000

    args = parser.parse_args(["--bind-host", "0.0.0.0"])  # noqa: S104
    assert args.host == "0.0.0.0"  # noqa: S104

    args = parser.parse_args(["--host", "::1"])
    assert args.host == "::1"


@pytest.mark.unit
def test_create_argparser_rejects_invalid_port() -> None:
    """create_argparser should reject ports outside the valid TCP range."""
    parser = create_argparser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--port", "0"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--port", "70000"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--port", "not-a-number"])


@pytest.mark.unit
def test_choose_power_column_prefers_legacy_when_present() -> None:
    """choose_power_column should prefer legacy column when it has non-null values."""
    frame = pd.DataFrame({"average_mW": [1.0, 2.0], "average_mA": [3.0, 4.0]})
    assert choose_power_column(frame, "average_mW", "average_mA") == "average_mW"


@pytest.mark.unit
def test_choose_power_column_raises_when_neither_present() -> None:
    """choose_power_column should raise ValueError when neither column exists."""
    frame = pd.DataFrame({"other_col": [1.0, 2.0]})
    with pytest.raises(ValueError, match="Missing required power column"):
        choose_power_column(frame, "average_mW", "average_mA")


@pytest.mark.unit
def test_choose_power_column_uses_legacy_when_only_legacy_present() -> None:
    """choose_power_column should use legacy column even if all null when it's the only option."""
    frame = pd.DataFrame({"average_mW": [None, None]})
    assert choose_power_column(frame, "average_mW", "average_mA") == "average_mW"


@pytest.mark.unit
def test_analysis_camelcase_aliases_delegate() -> None:
    """CamelCase analysis helpers should delegate to snake_case implementations."""
    frame = pd.DataFrame({"average_mA": [1.0]})
    assert choosePowerColumn(frame, "average_mW", "average_mA") == "average_mA"

    known_board_id = mesh_pb2.HardwareModel.DESCRIPTOR.values[0].number
    dslog = pd.DataFrame(
        {
            "time": [1],
            "pm_mask": [0],
            "sw_version": ["2.7.8"],
            "board_id": [known_board_id],
        }
    )
    assert getPmonRaises(dslog).equals(get_pmon_raises(dslog))
    assert getBoardInfo(dslog) == get_board_info(dslog)


@pytest.mark.unit
def test_get_pmon_raises_requires_pm_mask_column() -> None:
    """get_pmon_raises should fail clearly when the pm_mask column is missing."""
    dslog = pd.DataFrame({"time": [1.0, 2.0]})
    with pytest.raises(ValueError, match="No pm_mask column found in slog"):
        get_pmon_raises(dslog)


@pytest.mark.unit
def test_get_pmon_raises_rejects_non_numeric_masks() -> None:
    """get_pmon_raises should reject non-numeric pm_mask values."""
    dslog = pd.DataFrame(
        {
            "time": [1.0, 2.0],
            "pm_mask": [0, "invalid"],
        }
    )
    with pytest.raises(ValueError, match="pm_mask contains non-numeric values"):
        get_pmon_raises(dslog)


@pytest.mark.unit
def test_get_board_info_requires_sw_version_column() -> None:
    """get_board_info should fail clearly when sw_version column is missing."""
    frame = pd.DataFrame({"board_id": [1]})
    with pytest.raises(ValueError, match="No sw_version column found in dslog"):
        get_board_info(frame)


@pytest.mark.unit
def test_get_board_info_requires_non_null_sw_version() -> None:
    """get_board_info should fail when all sw_version values are null."""
    frame = pd.DataFrame({"sw_version": [None], "board_id": [1]})
    with pytest.raises(ValueError, match="No board info rows found in dslog"):
        get_board_info(frame)


@pytest.mark.unit
def test_get_board_info_accepts_integral_float_board_id() -> None:
    """get_board_info should accept float board_id values that are exact integers."""
    known_board_id = mesh_pb2.HardwareModel.DESCRIPTOR.values[0].number
    frame = pd.DataFrame({"sw_version": ["2.5.0"], "board_id": [float(known_board_id)]})
    result = get_board_info(frame)
    assert result[0] == mesh_pb2.HardwareModel.Name(known_board_id)


@pytest.mark.unit
def test_get_board_info_accepts_string_board_id() -> None:
    """get_board_info should accept string board_id values that are valid integers."""
    known_board_id = mesh_pb2.HardwareModel.DESCRIPTOR.values[0].number
    frame = pd.DataFrame({"sw_version": ["2.5.0"], "board_id": [str(known_board_id)]})
    result = get_board_info(frame)
    assert result[0] == mesh_pb2.HardwareModel.Name(known_board_id)


@pytest.mark.unit
def test_get_board_info_rejects_invalid_string_board_id() -> None:
    """get_board_info should reject board_id values that are non-integer strings."""
    frame = pd.DataFrame({"sw_version": ["2.5.0"], "board_id": ["not_a_number"]})
    with pytest.raises(ValueError, match="Invalid board_id value in dslog"):
        get_board_info(frame)


@pytest.mark.unit
def test_get_board_info_rejects_unknown_board_id() -> None:
    """get_board_info should reject board_id values that don't map to known HardwareModel."""
    frame = pd.DataFrame({"sw_version": ["2.5.0"], "board_id": [999999999]})
    with pytest.raises(ValueError, match="Unknown board_id value in dslog"):
        get_board_info(frame)


@pytest.mark.unit
def test_create_dash_returns_dash_app() -> None:
    """create_dash should return a configured Dash application."""
    cur_dir = Path(__file__).resolve().parent
    slog_input_dir = str(cur_dir / "slog-test-input")

    app = create_dash(slog_input_dir)

    assert app is not None
    assert hasattr(app, "layout")
    assert app.layout is not None


@pytest.mark.unit
def test_create_dash_with_pmon_raises(tmp_path: Path) -> None:
    """create_dash should render when slog includes power-monitor raise bits."""
    single_bit, _ = _single_bit_power_states()
    slog_dir = tmp_path / "test_slog"
    slog_dir.mkdir()

    power_data = pd.DataFrame(
        {
            "time": pd.array([1, 2, 3], dtype=pd.Int64Dtype()),
            "average_mA": pd.array([100, 150, 200], dtype=pd.Float64Dtype()),
            "max_mA": pd.array([110, 160, 210], dtype=pd.Float64Dtype()),
            "min_mA": pd.array([90, 140, 190], dtype=pd.Float64Dtype()),
        }
    )
    feather.write_feather(power_data, str(slog_dir / "power.feather"))

    slog_data = pd.DataFrame(
        {
            "time": pd.array([1.0, 2.0, 3.0]),
            "pm_mask": pd.array([0, single_bit, single_bit], dtype="object"),
        }
    )
    feather.write_feather(slog_data, str(slog_dir / "slog.feather"))

    app = create_dash(str(slog_dir))
    assert app is not None
    assert hasattr(app, "layout")
    assert app.layout is not None


@pytest.mark.unit
def test_create_dash_with_legacy_mw_columns(tmp_path: Path) -> None:
    """create_dash should support historical mW column names."""
    slog_dir = tmp_path / "test_slog"
    slog_dir.mkdir()

    power_data = pd.DataFrame(
        {
            "time": pd.array([1, 2, 3], dtype=pd.Int64Dtype()),
            "average_mW": pd.array([1000, 1500, 2000], dtype=pd.Float64Dtype()),
            "max_mW": pd.array([1100, 1600, 2100], dtype=pd.Float64Dtype()),
            "min_mW": pd.array([900, 1400, 1900], dtype=pd.Float64Dtype()),
        }
    )
    feather.write_feather(power_data, str(slog_dir / "power.feather"))

    slog_data = pd.DataFrame(
        {
            "time": pd.array([1, 2, 3], dtype=pd.Int64Dtype()),
            "pm_mask": pd.array([0, 0, 0], dtype=pd.Int64Dtype()),
        }
    )
    feather.write_feather(slog_data, str(slog_dir / "slog.feather"))

    app = create_dash(str(slog_dir))
    assert app is not None
    assert hasattr(app, "layout")
    assert app.layout is not None


@pytest.mark.unit
def test_create_dash_fails_when_power_file_missing(tmp_path: Path) -> None:
    """create_dash should raise when power.feather is absent."""
    slog_dir = tmp_path / "test_slog"
    slog_dir.mkdir()

    slog_data = pd.DataFrame(
        {
            "time": pd.array([1, 2, 3], dtype=pd.Int64Dtype()),
            "pm_mask": pd.array([0, 0, 0], dtype=pd.Int64Dtype()),
        }
    )
    feather.write_feather(slog_data, str(slog_dir / "slog.feather"))

    with pytest.raises((FileNotFoundError, OSError)):
        create_dash(str(slog_dir))


@pytest.mark.unit
def test_create_dash_fails_when_slog_file_missing(tmp_path: Path) -> None:
    """create_dash should raise when slog.feather is absent."""
    slog_dir = tmp_path / "test_slog"
    slog_dir.mkdir()

    power_data = pd.DataFrame(
        {
            "time": pd.array([1, 2, 3], dtype=pd.Int64Dtype()),
            "average_mA": pd.array([100, 150, 200], dtype=pd.Float64Dtype()),
            "max_mA": pd.array([110, 160, 210], dtype=pd.Float64Dtype()),
            "min_mA": pd.array([90, 140, 190], dtype=pd.Float64Dtype()),
        }
    )
    feather.write_feather(power_data, str(slog_dir / "power.feather"))

    with pytest.raises((FileNotFoundError, OSError)):
        create_dash(str(slog_dir))


@pytest.mark.unit
def test_main_uses_default_slog_when_not_provided(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """main() should use default slog path when --slog is not provided."""
    parser_default_slog = create_argparser().parse_args([]).slog
    expected_slog = parser_default_slog or os.path.join(
        analysis_main.rootDir(), "latest"
    )
    captured: dict[str, str] = {}

    class _FakeApp:
        def run(self, *, debug: bool, host: str, port: int) -> None:
            _ = (debug, host, port)

    def _fake_create_dash(*, slog_path: str) -> _FakeApp:
        captured["slog_path"] = slog_path
        return _FakeApp()

    monkeypatch.setattr(analysis_main, "create_dash", _fake_create_dash)
    monkeypatch.setattr(sys, "argv", ["fakescriptname"])
    monkeypatch.setattr(logging.getLogger(), "propagate", True)

    with caplog.at_level(logging.INFO):
        main()

    assert "Running Dash visualization" in caplog.text
    assert captured["slog_path"] == expected_slog


@pytest.mark.unit
def test_main_disables_debug_for_non_loopback_host(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() should disable debug mode when host is not loopback."""
    run_kwargs: dict[str, Any] = {}

    class _FakeApp:
        def run(self, *, debug: bool, host: str, port: int) -> None:
            """Simulate Dash app run method."""
            run_kwargs.update({"debug": debug, "host": host, "port": port})

    def _fake_create_dash(*, slog_path: str) -> _FakeApp:
        _ = slog_path
        return _FakeApp()

    monkeypatch.setattr(analysis_main, "create_dash", _fake_create_dash)
    monkeypatch.setattr(
        sys,
        "argv",
        ["fakescriptname", "--slog", os.devnull, "--debug", "--host", "0.0.0.0"],
    )
    monkeypatch.setattr(logging.getLogger(), "propagate", True)

    with caplog.at_level(logging.DEBUG):
        main()

    assert "Ignoring --debug because host 0.0.0.0 is not localhost" in caplog.text
    assert run_kwargs["debug"] is False
    assert run_kwargs["host"] == "0.0.0.0"


@pytest.mark.unit
def test_cli_exit_calls_util_our_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """_cli_exit should call util.our_exit with the provided message and return code."""
    captured: dict[str, Any] = {}

    def _fake_our_exit(message: str, return_code: int = 1) -> NoReturn:
        captured["message"] = message
        captured["code"] = return_code
        raise SystemExit(return_code)

    monkeypatch.setattr(analysis_main.util, "our_exit", _fake_our_exit)

    with pytest.raises(SystemExit) as exc_info:
        _cli_exit("test error message", 42)

    assert captured["message"] == "test error message"
    assert captured["code"] == 42
    assert exc_info.value.code == 42


@pytest.mark.unit
def test_parse_port_accepts_valid_ports() -> None:
    """create_argparser should accept valid --port values."""
    parser = create_argparser()
    assert parser.parse_args(["--port", "1"]).port == 1
    assert parser.parse_args(["--port", "8051"]).port == 8051
    assert parser.parse_args(["--port", "65535"]).port == 65535
