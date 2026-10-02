"""Regression checks for device waits inside CLI subprocess budgets."""

import subprocess
from unittest.mock import MagicMock

import pytest

from meshtastic.tests import cli_test_utils


@pytest.mark.unit
@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("--export-config", "config.yaml"), ["--timeout", "5"]),
        (("--timeout", "2", "--info"), []),
        (("--timeout=2", "--info"), []),
    ],
)
def test_host_cli_bounds_device_waits(
    monkeypatch: pytest.MonkeyPatch,
    args: tuple[str, ...],
    expected: list[str],
) -> None:
    """Preserve explicit timeouts while bounding default device reads."""
    run = MagicMock(
        return_value=subprocess.CompletedProcess([], 0, stdout="ok", stderr="")
    )
    monkeypatch.setattr(cli_test_utils, "run_cli_argv_with_timeout", run)
    assert cli_test_utils._run_host_cli("localhost:4403", *args) == (0, "ok")
    run.assert_called_once_with(
        [cli_test_utils.PRIMARY_CLI_NAME, "--host", "localhost:4403", *expected, *args],
        timeout=30,
    )
