"""Failure-path regression tests for the installed-distribution smoke gate.

These tests exercise the checker's decision logic against synthetic artifacts
built in ``tmp_path`` (real zip/tar METADATA and RECORD content) plus a
minimal synthetic checkout for preflight derivation. They are fast, offline,
and spawn no subprocesses or virtualenvs.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import io
import json
import sys
import tarfile
import zipfile
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def _load_smoke_module() -> ModuleType:
    """Load ``bin/smoke_distributions.py`` as a module for direct calls."""
    path = Path(__file__).resolve().parents[2] / "bin" / "smoke_distributions.py"
    spec = importlib.util.spec_from_file_location("smoke_distributions", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load the smoke gate module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["smoke_distributions"] = module
    spec.loader.exec_module(module)
    return module


smoke = _load_smoke_module()

SmokeGateError = smoke.SmokeGateError
RepoExpectations = smoke.RepoExpectations
CliExpectations = smoke.CliExpectations

pytestmark = pytest.mark.unit

EXPECTATIONS = RepoExpectations(
    name="mtjk",
    version="9.9.9",
    scripts=("mtjk", "meshtastic", "mesh-tunnel", "mesh-analysis"),
    script_targets={
        "mtjk": "meshtastic.__main__:main",
        "meshtastic": "meshtastic.__main__:main",
        "mesh-tunnel": "meshtastic.__main__:tunnelMain",
        "mesh-analysis": "meshtastic.analysis.__main__:main [analysis]",
    },
    cli=CliExpectations(
        primary="mtjk",
        compat=("meshtastic",),
        tunnel="mesh-tunnel",
        analysis="mesh-analysis",
    ),
    extras=("analysis", "cli"),
    analysis_extra_deps=("pyarrow",),
    python_requires=">=3.11,<3.15",
)

PB2_STEMS = ("mesh", "portnums")

VALID_METADATA = """\
Metadata-Version: 2.3
Name: mtjk
Version: 9.9.9
Summary: synthetic gate fixture
Requires-Python: >=3.11,<3.15
Provides-Extra: analysis
Provides-Extra: cli
Requires-Dist: pyserial (>=3.5)
Requires-Dist: pyarrow (>=25.0.0) ; extra == "analysis"
"""

VALID_ENTRY_POINTS = """\
[console_scripts]
mtjk = meshtastic.__main__:main
meshtastic = meshtastic.__main__:main
mesh-tunnel = meshtastic.__main__:tunnelMain
mesh-analysis = meshtastic.analysis.__main__:main [analysis]
"""


def _wheel_files(
    *,
    metadata: str,
    entry_points: str,
    include_py_typed: bool,
    pb2_stems: tuple[str, ...] = PB2_STEMS,
    omit_pb2_stub: str | None = None,
    omit_record_entry: str | None = None,
) -> dict[str, str]:
    """Build the file map zipped into a synthetic wheel."""
    dist_info = "mtjk-9.9.9.dist-info"
    files: dict[str, str] = {
        "meshtastic/__init__.py": "",
        "meshtastic/_runtime_compatibility.json": "{}\n",
        f"{dist_info}/METADATA": metadata,
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: synthetic\nRoot-Is-Purelib: true\n"
        ),
        f"{dist_info}/entry_points.txt": entry_points,
    }
    record_entries = [
        "meshtastic/__init__.py,,",
        "meshtastic/_runtime_compatibility.json,,",
        f"{dist_info}/METADATA,,",
        f"{dist_info}/WHEEL,,",
        f"{dist_info}/entry_points.txt,,",
    ]
    if include_py_typed:
        files["meshtastic/py.typed"] = ""
        record_entries.append("meshtastic/py.typed,,")
    for stem in pb2_stems:
        files[f"meshtastic/protobuf/{stem}_pb2.py"] = ""
        record_entries.append(f"meshtastic/protobuf/{stem}_pb2.py,,")
        if omit_pb2_stub != stem:
            files[f"meshtastic/protobuf/{stem}_pb2.pyi"] = ""
            record_entries.append(f"meshtastic/protobuf/{stem}_pb2.pyi,,")
    if omit_record_entry is not None:
        record_entries = [
            entry for entry in record_entries if not entry.startswith(omit_record_entry)
        ]
    record_entries.append(f"{dist_info}/RECORD,,")
    files[f"{dist_info}/RECORD"] = "\n".join(record_entries) + "\n"
    return files


def _write_wheel(target: Path, files: dict[str, str]) -> Path:
    """Zip a synthetic wheel together and return its path."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return target


def _write_sdist(target: Path, pb2_stems: tuple[str, ...] = PB2_STEMS) -> Path:
    """Tar a synthetic sdist with the packaged protobuf members."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(target, "w:gz") as archive:
        members = ["meshtastic/py.typed", "meshtastic/__init__.py"]
        members.extend(f"meshtastic/protobuf/{stem}_pb2.py" for stem in pb2_stems)
        members.extend(f"meshtastic/protobuf/{stem}_pb2.pyi" for stem in pb2_stems)
        for name in members:
            data = io.BytesIO(b"")
            info = tarfile.TarInfo(name=f"mtjk-9.9.9/{name}")
            info.size = 0
            archive.addfile(info, data)
    return target


@pytest.fixture(name="write_valid_wheel")
def fixture_write_valid_wheel(tmp_path: Path) -> Callable[..., Path]:
    """Return a builder writing a valid synthetic wheel, with mutations."""

    def _build(**mutations: Any) -> Path:
        files = _wheel_files(
            metadata=mutations.get("metadata", VALID_METADATA),
            entry_points=mutations.get("entry_points", VALID_ENTRY_POINTS),
            include_py_typed=mutations.get("include_py_typed", True),
            pb2_stems=mutations.get("pb2_stems", PB2_STEMS),
            omit_pb2_stub=mutations.get("omit_pb2_stub"),
            omit_record_entry=mutations.get("omit_record_entry"),
        )
        for omitted in mutations.get("omit_files", []):
            files.pop(omitted, None)
        return _write_wheel(tmp_path / "dist" / "mtjk-9.9.9-py3-none-any.whl", files)

    return _build


def _write_synthetic_repo(
    repo_root: Path,
    *,
    dependencies: str,
    extras: str,
) -> Path:
    """Create a minimal checkout (pyproject + branding) for preflight tests."""
    (repo_root / "pyproject.toml").write_text(
        "[tool.poetry]\n"
        'name = "mtjk"\n'
        'version = "9.9.9"\n'
        "\n"
        "[tool.poetry.dependencies]\n"
        'python = "^3.11,<3.15"\n'
        f"{dependencies}\n"
        "\n"
        "[tool.poetry.extras]\n"
        f"{extras}\n"
        "\n"
        "[tool.poetry.scripts]\n"
        'mtjk = "meshtastic.__main__:main"\n'
        'meshtastic = "meshtastic.__main__:main"\n'
        'mesh-tunnel = "meshtastic.__main__:tunnelMain"\n'
        'mesh-analysis = "meshtastic.analysis.__main__:main [analysis]"\n',
        encoding="utf-8",
    )
    branding = repo_root / "meshtastic" / "_branding.py"
    branding.parent.mkdir(parents=True, exist_ok=True)
    branding.write_text(
        'PRIMARY_CLI_NAME = "mtjk"\n' 'COMPATIBILITY_CLI_NAMES = ("meshtastic",)\n',
        encoding="utf-8",
    )
    return repo_root


def test_valid_synthetic_artifacts_pass(
    write_valid_wheel: Callable[..., Path], tmp_path: Path
) -> None:
    """A complete synthetic wheel and sdist pass the contents validation."""
    wheel = write_valid_wheel()
    smoke.validate_wheel_contents(wheel, EXPECTATIONS, PB2_STEMS)
    sdist = _write_sdist(tmp_path / "dist" / "mtjk-9.9.9.tar.gz")
    smoke.validate_sdist_contents(sdist, EXPECTATIONS, PB2_STEMS)


def test_unconditional_analysis_dependency_fails(
    write_valid_wheel: Callable[..., Path],
) -> None:
    """An unconditional pyarrow Requires-Dist must fail the gate."""
    wheel = write_valid_wheel(
        metadata=VALID_METADATA.replace(
            'Requires-Dist: pyarrow (>=25.0.0) ; extra == "analysis"',
            "Requires-Dist: pyarrow (>=25.0.0)",
        )
    )
    with pytest.raises(SmokeGateError, match=r"unconditionally"):
        smoke.validate_wheel_contents(wheel, EXPECTATIONS, PB2_STEMS)


def test_missing_analysis_dependency_marker_fails(
    write_valid_wheel: Callable[..., Path],
) -> None:
    """A pyarrow requirement without the analysis marker must fail."""
    wheel = write_valid_wheel(
        metadata=VALID_METADATA.replace(
            'Requires-Dist: pyarrow (>=25.0.0) ; extra == "analysis"',
            'Requires-Dist: pyarrow (>=25.0.0) ; extra == "cli"',
        )
    )
    with pytest.raises(SmokeGateError, match=r"extra == \"analysis\" marker"):
        smoke.validate_wheel_contents(wheel, EXPECTATIONS, PB2_STEMS)


def test_record_missing_py_typed_fails(write_valid_wheel: Callable[..., Path]) -> None:
    """A wheel without the py.typed marker must fail the gate."""
    wheel = write_valid_wheel(include_py_typed=False)
    with pytest.raises(SmokeGateError, match=r"py\.typed"):
        smoke.validate_wheel_contents(wheel, EXPECTATIONS, PB2_STEMS)


def test_record_unaccounted_member_fails(
    write_valid_wheel: Callable[..., Path],
) -> None:
    """A packaged meshtastic member absent from RECORD must fail the gate."""
    wheel = write_valid_wheel(
        omit_record_entry="meshtastic/_runtime_compatibility.json"
    )
    with pytest.raises(SmokeGateError, match=r"RECORD does not account for 1"):
        smoke.validate_wheel_contents(wheel, EXPECTATIONS, PB2_STEMS)


def test_missing_console_script_fails(write_valid_wheel: Callable[..., Path]) -> None:
    """A wheel missing the mesh-analysis entry point must fail."""
    wheel = write_valid_wheel(
        entry_points=VALID_ENTRY_POINTS.replace(
            "mesh-analysis = meshtastic.analysis.__main__:main [analysis]\n", ""
        )
    )
    with pytest.raises(SmokeGateError, match=r"mesh-analysis"):
        smoke.validate_wheel_contents(wheel, EXPECTATIONS, PB2_STEMS)


def test_console_script_value_mismatch_fails(
    write_valid_wheel: Callable[..., Path],
) -> None:
    """A console script pointing at the wrong callable must fail the gate."""
    wheel = write_valid_wheel(
        entry_points=VALID_ENTRY_POINTS.replace(
            "mtjk = meshtastic.__main__:main", "mtjk = meshtastic.__main__:mai"
        )
    )
    with pytest.raises(SmokeGateError, match=r"unexpected target"):
        smoke.validate_wheel_contents(wheel, EXPECTATIONS, PB2_STEMS)


def test_console_script_missing_analysis_extra_marker_fails(
    write_valid_wheel: Callable[..., Path],
) -> None:
    """A mesh-analysis script that lost its [analysis] marker must fail."""
    wheel = write_valid_wheel(
        entry_points=VALID_ENTRY_POINTS.replace(
            "mesh-analysis = meshtastic.analysis.__main__:main [analysis]",
            "mesh-analysis = meshtastic.analysis.__main__:main",
        )
    )
    with pytest.raises(SmokeGateError, match=r"lost its \[analysis\] extra marker"):
        smoke.validate_wheel_contents(wheel, EXPECTATIONS, PB2_STEMS)


def test_console_script_value_whitespace_tolerated(
    write_valid_wheel: Callable[..., Path],
) -> None:
    """Entry-point targets differing only in whitespace must still pass."""
    wheel = write_valid_wheel(
        entry_points=VALID_ENTRY_POINTS.replace(
            "mesh-analysis = meshtastic.analysis.__main__:main [analysis]",
            "mesh-analysis =   meshtastic.analysis.__main__:main   [analysis]",
        )
    )
    smoke.validate_wheel_contents(wheel, EXPECTATIONS, PB2_STEMS)


def test_missing_pb2_stub_member_fails(write_valid_wheel: Callable[..., Path]) -> None:
    """A wheel missing a generated *_pb2.pyi member must fail."""
    wheel = write_valid_wheel(omit_pb2_stub="mesh")
    with pytest.raises(SmokeGateError, match=r"mesh_pb2\.pyi"):
        smoke.validate_wheel_contents(wheel, EXPECTATIONS, PB2_STEMS)


def test_missing_requires_python_fails(write_valid_wheel: Callable[..., Path]) -> None:
    """Built metadata without Requires-Python must fail."""
    wheel = write_valid_wheel(
        metadata=VALID_METADATA.replace("Requires-Python: >=3.11,<3.15\n", "")
    )
    with pytest.raises(SmokeGateError, match=r"Requires-Python"):
        smoke.validate_wheel_contents(wheel, EXPECTATIONS, PB2_STEMS)


def test_wrong_requires_python_fails(write_valid_wheel: Callable[..., Path]) -> None:
    """Built Requires-Python that drifts from the source must fail."""
    floor_drift = write_valid_wheel(
        metadata=VALID_METADATA.replace(
            "Requires-Python: >=3.11,<3.15", "Requires-Python: >=3.12,<3.15"
        )
    )
    with pytest.raises(SmokeGateError, match=r"Requires-Python does not match"):
        smoke.validate_wheel_contents(floor_drift, EXPECTATIONS, PB2_STEMS)
    cap_drift = write_valid_wheel(
        metadata=VALID_METADATA.replace(
            "Requires-Python: >=3.11,<3.15", "Requires-Python: >=3.11,<4.0"
        )
    )
    with pytest.raises(SmokeGateError, match=r"Requires-Python does not match"):
        smoke.validate_wheel_contents(cap_drift, EXPECTATIONS, PB2_STEMS)


def test_missing_provides_extra_fails(write_valid_wheel: Callable[..., Path]) -> None:
    """Built metadata without Provides-Extra: analysis must fail."""
    wheel = write_valid_wheel(
        metadata=VALID_METADATA.replace("Provides-Extra: analysis\n", "")
    )
    with pytest.raises(SmokeGateError, match=r"Provides-Extra.*analysis"):
        smoke.validate_wheel_contents(wheel, EXPECTATIONS, PB2_STEMS)


def test_sdist_missing_pb2_member_fails(tmp_path: Path) -> None:
    """An sdist missing a generated protobuf member must fail."""
    sdist = tmp_path / "dist" / "mtjk-9.9.9.tar.gz"
    sdist.parent.mkdir(parents=True)
    with tarfile.open(sdist, "w:gz") as archive:
        info = tarfile.TarInfo(name="mtjk-9.9.9/meshtastic/py.typed")
        info.size = 0
        archive.addfile(info, io.BytesIO(b""))
    with pytest.raises(SmokeGateError, match=r"mesh_pb2"):
        smoke.validate_sdist_contents(sdist, EXPECTATIONS, PB2_STEMS)


def test_stale_dist_dir_rejected_in_build_mode(tmp_path: Path) -> None:
    """locate_artifacts must reject a non-empty dist dir when building."""
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "mtjk-9.9.9-py3-none-any.whl").write_bytes(b"stale")
    with pytest.raises(SmokeGateError, match=r"not empty"):
        smoke.locate_artifacts(dist_dir, EXPECTATIONS, skip_build=False)


def test_load_repo_expectations_derives_cli_roles(tmp_path: Path) -> None:
    """Preflight derives script targets, analysis deps, and CLI roles."""
    repo_root = _write_synthetic_repo(
        tmp_path,
        dependencies='pyarrow = { version = "*", optional = true }\n',
        extras='cli = []\nanalysis = ["pyarrow"]\n',
    )
    expectations = smoke.load_repo_expectations(repo_root)
    assert expectations.scripts == (
        "mesh-analysis",
        "mesh-tunnel",
        "meshtastic",
        "mtjk",
    )
    assert expectations.script_targets["mesh-analysis"] == (
        "meshtastic.analysis.__main__:main [analysis]"
    )
    assert expectations.cli == CliExpectations(
        primary="mtjk",
        compat=("meshtastic",),
        tunnel="mesh-tunnel",
        analysis="mesh-analysis",
    )
    assert expectations.analysis_extra_deps == ("pyarrow",)


def test_optional_dependency_without_extra_fails(tmp_path: Path) -> None:
    """An optional dependency belonging to no extra must fail preflight."""
    repo_root = _write_synthetic_repo(
        tmp_path,
        dependencies=(
            'pyarrow = { version = "*", optional = true }\n'
            'orphan-dep = { version = "*", optional = true }\n'
        ),
        extras='cli = []\nanalysis = ["pyarrow"]\n',
    )
    with pytest.raises(SmokeGateError, match=r"belong to no extra"):
        smoke.load_repo_expectations(repo_root)


def test_describe_field_document_bounds() -> None:
    """The documented lora.hop_limit bounds (0..7) must be enforced."""
    smoke.validate_describe_field_document(
        {"field": "lora.hop_limit", "type": "uint32", "min_value": 0, "max_value": 7}
    )
    with pytest.raises(SmokeGateError, match=r"bounds"):
        smoke.validate_describe_field_document(
            {"field": "lora.hop_limit", "type": "uint32", "max_value": 7}
        )
    with pytest.raises(SmokeGateError, match=r"bounds"):
        smoke.validate_describe_field_document(
            {
                "field": "lora.hop_limit",
                "type": "uint32",
                "min_value": 0,
                "max_value": 6,
            }
        )
    with pytest.raises(SmokeGateError, match=r"expected"):
        smoke.validate_describe_field_document(
            {"field": "lora.freq_override", "type": "uint32"}
        )


def test_list_fields_document_requires_real_descriptors() -> None:
    """Empty or lora-less field listings must fail."""
    smoke.validate_list_fields_document(
        {
            "config_fields": [{"field": "lora.hop_limit", "type": "uint32"}],
            "module_config_fields": [{"field": "mux.frequency", "type": "float"}],
            "aliases": {},
        }
    )
    with pytest.raises(SmokeGateError, match=r"missing or empty"):
        smoke.validate_list_fields_document(
            {"config_fields": [], "module_config_fields": [], "aliases": {}}
        )
    with pytest.raises(SmokeGateError, match=r"lora"):
        smoke.validate_list_fields_document(
            {
                "config_fields": [{"field": "power.ls_secs", "type": "uint32"}],
                "module_config_fields": [{"field": "mux.frequency", "type": "float"}],
                "aliases": {},
            }
        )


def test_requirement_name_parsing() -> None:
    """Requires-Dist name extraction handles marker suffixes and casing."""
    assert (
        smoke._requirement_name('pyarrow (>=25.0.0) ; extra == "analysis"') == "pyarrow"
    )
    assert smoke._requirement_name("dash-bootstrap-components (<5)") == (
        "dash-bootstrap-components"
    )
    assert (
        smoke._requirement_name('PyArrow (>=25.0.0) ; extra == "analysis"') == "pyarrow"
    )
    assert smoke._requirement_name("Pandas_Stubs (<5)") == "pandas-stubs"
    assert smoke._requirement_name("") == ""


def test_analysis_dependency_metadata_comparison_is_pep503_normalized(
    write_valid_wheel: Callable[..., Path],
) -> None:
    """Raw pyproject dependency keys compare canonically to Requires-Dist names."""
    expectations = dataclasses.replace(
        EXPECTATIONS, analysis_extra_deps=("Pandas_Stubs",)
    )
    wheel = write_valid_wheel(
        metadata=VALID_METADATA.replace(
            'Requires-Dist: pyarrow (>=25.0.0) ; extra == "analysis"',
            'Requires-Dist: pandas-stubs (>=2.3.3) ; extra == "analysis"',
        )
    )
    smoke.validate_wheel_contents(wheel, expectations, PB2_STEMS)


@pytest.mark.parametrize("stdout", ["not-json", "[]"])
def test_parse_json_object_reports_context(stdout: str) -> None:
    """Malformed or non-object JSON is converted to a contextual gate error."""
    command = ["mtjk", "--list-fields", "--json"]
    with pytest.raises(SmokeGateError) as exc_info:
        smoke._parse_json_object(
            stdout,
            phase="schema",
            profile="wheel-core",
            command=command,
        )
    message = str(exc_info.value)
    assert "phase=schema" in message
    assert "profile=wheel-core" in message
    assert "command=mtjk --list-fields --json" in message
    assert stdout in message


def test_run_probe_json_requires_an_object_and_keeps_command_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Probe JSON shares the same object-only contextual parse contract."""
    monkeypatch.setattr(smoke, "run_command", lambda *args, **kwargs: "[]")
    python = tmp_path / "venv" / "bin" / "python"
    probe = tmp_path / "probe.py"
    with pytest.raises(SmokeGateError) as exc_info:
        smoke.run_probe_json(
            python,
            probe,
            tmp_path,
            profile="wheel-core",
            phase="library",
            args=["one"],
        )
    message = str(exc_info.value)
    assert "phase=library" in message
    assert "profile=wheel-core" in message
    assert str(python) in message
    assert str(probe) in message


@pytest.mark.parametrize("broken_surface", ["list", "describe"])
def test_core_schema_surfaces_wrap_non_object_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, broken_surface: str
) -> None:
    """Both schema CLI JSON surfaces stay inside the contextual error contract."""
    venv_dir = tmp_path / "venv"
    bin_dir = venv_dir / "bin"
    bin_dir.mkdir(parents=True)
    for name in (
        EXPECTATIONS.cli.primary,
        *EXPECTATIONS.cli.compat,
        EXPECTATIONS.cli.tunnel,
    ):
        (bin_dir / name).write_text("", encoding="utf-8")

    expected_version = f"{EXPECTATIONS.cli.primary} {EXPECTATIONS.version}"
    list_document = {
        "config_fields": [{"field": "lora.hop_limit", "type": "uint32"}],
        "module_config_fields": [{"field": "mux.frequency", "type": "float"}],
    }
    describe_document = {
        "field": "lora.hop_limit",
        "type": "uint32",
        "min_value": 0,
        "max_value": 7,
    }

    def _run_command(argv: list[str], **_kwargs: Any) -> str:
        if argv[-2:] == ["--list-fields", "--json"]:
            return "[]" if broken_surface == "list" else json.dumps(list_document)
        if "--describe-field" in argv:
            return (
                "[]" if broken_surface == "describe" else json.dumps(describe_document)
            )
        if argv[-1] == "--version":
            return expected_version
        if argv[-1] == "--help":
            return "help"
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(smoke, "run_command", _run_command)
    with pytest.raises(SmokeGateError) as exc_info:
        smoke._run_core_cli_surfaces(
            venv_dir,
            venv_dir / "bin" / "python",
            tmp_path,
            EXPECTATIONS,
            profile="wheel-core",
        )
    message = str(exc_info.value)
    assert "phase=schema" in message
    assert "profile=wheel-core" in message
    expected_flag = "--list-fields" if broken_surface == "list" else "--describe-field"
    assert expected_flag in message


def test_core_light_assertion_checks_every_analysis_distribution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Core cells probe all runtime-derived analysis extra distributions."""
    expectations = dataclasses.replace(
        EXPECTATIONS,
        analysis_extra_deps=(
            "dash",
            "dash-bootstrap-components",
            "parse",
            "platformdirs",
            "plotly",
            "pandas",
            "pandas-stubs",
            "pyarrow",
        ),
    )
    observed_args: list[str] = []

    def _run_probe_json(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        observed_args.extend(kwargs["args"])
        return {"unexpectedly_installed": []}

    monkeypatch.setattr(smoke, "run_probe_json", _run_probe_json)
    smoke._run_core_light_assertion(
        tmp_path / "python",
        tmp_path,
        expectations,
        profile="wheel-core",
    )
    assert observed_args == list(expectations.analysis_extra_deps)


def test_install_identity_rejects_string_prefix_outside_venv(tmp_path: Path) -> None:
    """A sibling path sharing the venv string prefix is not inside the venv."""
    venv_dir = tmp_path / "venv"
    outside = tmp_path / "venv-foreign" / "site-packages" / "meshtastic"
    observed = {
        "name": EXPECTATIONS.name,
        "version": EXPECTATIONS.version,
        "meshtastic_file": str(outside / "__init__.py"),
        "protobuf_file": str(outside / "protobuf" / "__init__.py"),
        "py_typed_via_resources": True,
    }
    with pytest.raises(SmokeGateError, match=r"outside the consumer venv"):
        smoke._assert_install_identity(
            observed, venv_dir, EXPECTATIONS, profile="wheel-core"
        )
