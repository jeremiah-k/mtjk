"""Tests for publishing standalone tool versions from Trunk configuration."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


def _load_checker_module() -> ModuleType:
    """Load the repository quality-version checker as an isolated script module."""
    project_root = Path(__file__).resolve().parents[2]
    script_path = project_root / "bin" / "check_quality_tool_versions.py"
    spec = importlib.util.spec_from_file_location(
        "check_quality_tool_versions", script_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
def test_ruff_version_reader_accepts_surrounding_whitespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reader should accept whitespace around Trunk's Ruff list item."""
    module = _load_checker_module()
    trunk_file = tmp_path / "trunk.yaml"
    trunk_file.write_text(
        "lint:\n  enabled:\n      -   ruff@0.16.2  \n",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "TRUNK_CONFIG", trunk_file)

    assert module._ruff_version_from_trunk() == "0.16.2"


@pytest.mark.unit
@pytest.mark.parametrize(
    "contents, found_count",
    [
        ("lint:\n  enabled:\n    - ruff\n", 0),
        ("lint:\n  enabled:\n    - ruff@0.16\n", 0),
        ("lint:\n  disabled:\n    - ruff@0.16.2\n  enabled:\n    - black@1.2.3\n", 0),
        (
            "lint:\n  enabled:\n    - ruff@0.16.1\n    - ruff@0.16.2\n",
            2,
        ),
    ],
)
def test_ruff_version_reader_rejects_missing_malformed_or_duplicate_pins(
    contents: str,
    found_count: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing, malformed, and duplicate Ruff pins should fail loudly."""
    module = _load_checker_module()
    trunk_file = tmp_path / "trunk.yaml"
    trunk_file.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(module, "TRUNK_CONFIG", trunk_file)

    with pytest.raises(
        RuntimeError,
        match=rf"Expected exactly one Ruff pin .*; found {found_count}",
    ):
        module._ruff_version_from_trunk()


@pytest.mark.unit
@pytest.mark.parametrize(
    "contents, expected_message",
    [
        ("runtimes:\n  enabled: []\n", "top-level lint section"),
        ("lint:\n  disabled:\n    - ruff\n", "lint.enabled section"),
        (
            "lint:\n  rules:\n    enabled:\n      - ruff@0.16.2\n",
            "lint.enabled section",
        ),
    ],
)
def test_ruff_version_reader_requires_the_lint_enabled_section(
    contents: str,
    expected_message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins outside the canonical Trunk section must not satisfy the contract."""
    module = _load_checker_module()
    trunk_file = tmp_path / "trunk.yaml"
    trunk_file.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(module, "TRUNK_CONFIG", trunk_file)

    with pytest.raises(RuntimeError, match=expected_message):
        module._ruff_version_from_trunk()


@pytest.mark.unit
def test_ruff_version_reader_reports_unreadable_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable Trunk configuration should produce an actionable error."""
    module = _load_checker_module()
    trunk_file = tmp_path / "missing-trunk.yaml"
    monkeypatch.setattr(module, "TRUNK_CONFIG", trunk_file)

    with pytest.raises(RuntimeError, match="Unable to read Trunk configuration"):
        module._ruff_version_from_trunk()


@pytest.mark.unit
def test_quality_version_checker_writes_sanitized_github_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GitHub Actions should receive the Ruff pin from Trunk."""
    module = _load_checker_module()
    trunk_file = tmp_path / "trunk.yaml"
    trunk_file.write_text("lint:\n  enabled:\n    - ruff@0.16.1\n", encoding="utf-8")
    github_env = tmp_path / "github-env"
    monkeypatch.setattr(module, "TRUNK_CONFIG", trunk_file)

    assert module.main(["--github-env", str(github_env)]) == 0
    assert github_env.read_text(encoding="utf-8") == "RUFF_VERSION=0.16.1\n"


@pytest.mark.unit
def test_quality_version_checker_prints_only_the_requested_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Local tooling should receive only the version when explicitly requested."""
    module = _load_checker_module()
    trunk_file = tmp_path / "trunk.yaml"
    trunk_file.write_text("lint:\n  enabled:\n    - ruff@0.16.2\n", encoding="utf-8")
    monkeypatch.setattr(module, "TRUNK_CONFIG", trunk_file)

    assert module.main(["--print-ruff-version"]) == 0
    assert capsys.readouterr().out == "0.16.2\n"
