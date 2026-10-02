"""Exercise simulator runner selection without starting Docker or a device."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("targets", "override", "expected"),
    [
        (None, None, "int"),
        ("meshtastic/tests/test_meshtasticd_tcp_interface_ci.py", None, "int"),
        ("meshtastic/tests/test_meshtasticd_ci.py", None, "int"),
        (
            "meshtastic/tests/test_smokevirt.py",
            None,
            "smokevirt and not smoke1_destructive",
        ),
        (None, "int or unit", "int or unit"),
    ],
)
def test_runner_selects_integration_tests(
    tmp_path: Path, targets: str | None, override: str | None, expected: str
) -> None:
    """Capture the actual pytest invocation from the standalone runner."""
    capture = tmp_path / "pytest-args.json"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text("#!/bin/sh\nexit 0\n")
    docker.chmod(0o755)
    poetry = fake_bin / "poetry"
    poetry.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "if args[:2] == ['run', 'pytest']:\n"
        "    Path(os.environ['RUNNER_CAPTURE']).write_text(json.dumps(args[2:]))\n"
        "elif args[:3] == ['run', 'python', '-']:\n"
        "    print('localhost\\t4401')\n"
        "elif args[:3] == ['run', 'python', '-c']:\n"
        "    print('mtjk')\n"
    )
    poetry.chmod(0o755)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("MESHTASTICD_", "SMOKEVIRT_", "READY_LOG_FILE"))
    }
    env.update(
        PATH=f"{fake_bin}:{os.environ['PATH']}",
        RUNNER_CAPTURE=str(capture),
        READY_LOG_FILE=str(tmp_path / "ready.log"),
    )
    if targets is not None:
        env["MESHTASTICD_PYTEST_TARGETS"] = targets
    if override is not None:
        env["MESHTASTICD_PYTEST_MARK_EXPR"] = override
    result = subprocess.run(
        ["bash", str(ROOT / "bin/run-smokevirt-with-meshtasticd.sh")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    args = json.loads(capture.read_text())
    assert args[:2] == ["-m", expected]
    assert args[2:] == (
        targets.split()
        if targets is not None
        else [
            "meshtastic/tests/test_meshtasticd_ci.py",
            "meshtastic/tests/test_meshtasticd_tcp_interface_ci.py",
        ]
    )
