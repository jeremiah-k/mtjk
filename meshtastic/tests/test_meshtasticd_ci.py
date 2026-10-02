"""Stable meshtasticd integration checks for CI."""

import os
import re
import time
from pathlib import Path

import pytest
import yaml

from .cli_test_utils import _run_host_cli, _run_host_cli_ok

pytestmark = [pytest.mark.int, pytest.mark.smokevirt]


def _positive_float_from_env(name: str, default: float) -> float:
    """Read a positive float from the environment with a safe fallback."""
    raw_value = os.environ.get(name)
    if raw_value is None or raw_value == "":
        return default
    try:
        parsed = float(raw_value)
    except ValueError:
        return default
    if parsed <= 0:
        return default
    return parsed


HOST = os.environ.get("MESHTASTICD_HOST", "localhost:4401")
HOST_READY_TIMEOUT_SECONDS = _positive_float_from_env("MESH_HOST_READY_TIMEOUT", 45.0)
HOST_READY_POLL_TIMEOUT_SECONDS = _positive_float_from_env(
    "MESH_HOST_READY_POLL_TIMEOUT", 10.0
)


def _wait_for_host_ready(host: str, meshtastic_bin: str) -> None:
    """Wait for a host to return a successful ``--info`` response."""
    deadline = time.monotonic() + HOST_READY_TIMEOUT_SECONDS
    last_output = ""
    while time.monotonic() < deadline:
        returncode, output = _run_host_cli(
            host,
            "--info",
            timeout=HOST_READY_POLL_TIMEOUT_SECONDS,
            meshtastic_bin=meshtastic_bin,
        )
        if returncode == 0 and output.startswith("Connected to radio"):
            return
        last_output = output
        time.sleep(1)
    pytest.fail(
        f"{host} did not become ready in {HOST_READY_TIMEOUT_SECONDS}s."
        f"\nLast output:\n{last_output}"
    )


def _capture_host_debug_state(
    tmp_path: Path,
    host: str,
    meshtastic_bin: str,
    *,
    label: str,
) -> None:
    """Best-effort host snapshot to improve integration-failure diagnostics."""
    label_token = re.sub(r"[^A-Za-z0-9_.-]+", "_", label).strip("_") or "failure"
    try:
        info_rc, info_output = _run_host_cli(
            host,
            "--info",
            timeout=HOST_READY_POLL_TIMEOUT_SECONDS,
            meshtastic_bin=meshtastic_bin,
        )
        (tmp_path / f"debug-{label_token}-info.txt").write_text(
            f"host={host}\nreturncode={info_rc}\n\n{info_output}",
            encoding="utf-8",
        )
    except Exception as exc:
        (tmp_path / f"debug-{label_token}-info-error.txt").write_text(
            f"host={host}\nsnapshot_exception={exc!r}\n",
            encoding="utf-8",
        )
    export_path = tmp_path / f"debug-{label_token}-export.yaml"
    try:
        export_rc, export_output = _run_host_cli(
            host,
            "--export-config",
            str(export_path),
            timeout=HOST_READY_TIMEOUT_SECONDS,
            meshtastic_bin=meshtastic_bin,
        )
        if export_rc != 0:
            (tmp_path / f"debug-{label_token}-export-error.txt").write_text(
                f"host={host}\nreturncode={export_rc}\n\n{export_output}",
                encoding="utf-8",
            )
    except Exception as exc:
        (tmp_path / f"debug-{label_token}-export-error.txt").write_text(
            f"host={host}\nsnapshot_exception={exc!r}\n",
            encoding="utf-8",
        )


def test_meshtasticd_info_has_core_sections(meshtastic_bin: str) -> None:
    """`--info` should connect and return the key high-level sections."""
    output = _run_host_cli_ok(HOST, "--info", meshtastic_bin=meshtastic_bin)
    assert output.startswith("Connected to radio")
    assert re.search(r"^Owner:", output, re.MULTILINE)
    assert re.search(r"^My info:", output, re.MULTILINE)
    assert re.search(r"^Channels", output, re.MULTILINE)


def test_meshtasticd_nodes_lists_local_node(meshtastic_bin: str) -> None:
    """`--nodes` should include at least one Meshtastic node id."""
    output = _run_host_cli_ok(HOST, "--nodes", meshtastic_bin=meshtastic_bin)
    assert output.startswith("Connected to radio")
    assert re.search(r"![0-9a-fA-F]{8}", output)


def test_meshtasticd_export_and_configure_roundtrip(
    meshtastic_bin: str, tmp_path: Path
) -> None:
    """Export, mutate, configure, verify live state, then restore baseline config."""
    export_path = tmp_path / "meshtasticd-ci-export.yaml"
    mutated_path = tmp_path / "meshtasticd-ci-mutated.yaml"

    try:
        export_output = _run_host_cli_ok(
            HOST,
            "--export-config",
            str(export_path),
            meshtastic_bin=meshtastic_bin,
        )
        assert "Exported configuration to" in export_output
        assert export_path.exists()
        assert export_path.stat().st_size > 0

        exported_data = yaml.safe_load(export_path.read_text(encoding="utf-8"))
        assert isinstance(exported_data, dict)
        mutated_data = dict(exported_data)
        mutated_data["owner"] = "Meshtastic CI"
        mutated_data["owner_short"] = "MCI"
        mutated_path.write_text(
            yaml.safe_dump(mutated_data, sort_keys=False), encoding="utf-8"
        )

        original_exc: BaseException | None = None
        try:
            configure_output = _run_host_cli_ok(
                HOST,
                "--configure",
                str(mutated_path),
                meshtastic_bin=meshtastic_bin,
            )
            assert "Configuration transaction committed." in configure_output
            assert "all requested settings were verified." in configure_output

            _wait_for_host_ready(HOST, meshtastic_bin)
            info_output = _run_host_cli_ok(
                HOST,
                "--info",
                meshtastic_bin=meshtastic_bin,
            )
            assert info_output.startswith("Connected to radio")
            assert re.search(r"^Owner: Meshtastic CI\b", info_output, re.MULTILINE)
        except Exception as exc:
            original_exc = exc
            _capture_host_debug_state(
                tmp_path,
                HOST,
                meshtastic_bin,
                label="export-configure-roundtrip-failure-before-restore",
            )
            raise
        finally:
            try:
                restore_output = _run_host_cli_ok(
                    HOST,
                    "--configure",
                    str(export_path),
                    meshtastic_bin=meshtastic_bin,
                )
                assert "Configuration transaction committed." in restore_output
                assert "all requested settings were verified." in restore_output
                _wait_for_host_ready(HOST, meshtastic_bin)
            except Exception:
                if original_exc is not None:
                    raise original_exc from None
                raise
    except Exception:
        _capture_host_debug_state(
            tmp_path,
            HOST,
            meshtastic_bin,
            label="export-configure-roundtrip-failure",
        )
        raise


def test_meshtasticd_get_and_sendtext_paths(meshtastic_bin: str) -> None:
    """Read-only `--get` and private-port `--sendtext` should succeed."""
    get_output = _run_host_cli_ok(
        HOST,
        "--get",
        "lora.region",
        meshtastic_bin=meshtastic_bin,
    )
    assert re.search(r"^lora\.region:", get_output, re.MULTILINE)

    send_output = _run_host_cli_ok(
        HOST,
        "--private",
        "--sendtext",
        "hello",
        meshtastic_bin=meshtastic_bin,
    )
    assert "Sending text message" in send_output
