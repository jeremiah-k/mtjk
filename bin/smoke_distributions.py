#!/usr/bin/env python3
"""Installed-distribution artifact smoke gate.

Builds the final-candidate ``mtjk`` sdist and wheel from the current checkout
with ``python -m build`` and then proves the built artifacts behave as
installed distributions in fresh, isolated, non-editable consumer virtualenvs.
This is the release gate for the class of defect where a wheel's packaging
metadata diverges from its runtime behavior (for example an ``[analysis]``
extra that fails to declare its runtime dependencies).

The gate is fail-closed: every missing binary, missing resource, wrong module
origin, failed command, or timeout aborts the run with a nonzero exit code and
a context-rich error naming the phase, artifact, consumer profile, and
command. There are no skip paths.

Phases
------
1. ``build``       Fresh build of sdist + wheel (or validation of pre-existing
                   artifacts via ``--dist-dir`` + ``--skip-build``).
2. ``contents``    Wheel RECORD/METADATA/entry-points and sdist member checks,
                   with expectations derived at runtime from ``pyproject.toml``
                   and the repository's generated protobuf modules.
3. ``cells``       Four independent consumer cells (wheel/core, wheel/analysis,
                   sdist/core, sdist/analysis), each in a fresh non-editable
                   venv, run from a neutral working directory with
                   ``PYTHONPATH``/``PYTHONHOME`` cleared.

Local usage::

    python3 bin/smoke_distributions.py

Requires network access for dependency resolution and ``pip install build``
in the interpreter that runs this script.
"""

from __future__ import annotations

import argparse
import configparser
import csv
import dataclasses
import hashlib
import importlib.util
import io
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from collections.abc import Mapping, Sequence
from email.parser import Parser
from pathlib import Path
from types import ModuleType
from typing import Any

try:  # packaging ships with pip/build environments and is a core project dep
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.version import Version
except ImportError:  # pragma: no cover - gate interpreter without packaging
    InvalidSpecifier = None  # type: ignore[assignment,misc]
    SpecifierSet = None  # type: ignore[assignment,misc]
    Version = None  # type: ignore[assignment,misc]

_LOGGER = logging.getLogger("artifact-smoke")

# Bounded timeouts (seconds) per child-process class. Every child gets one.
TIMEOUT_BUILD = 1800
TIMEOUT_VENV_CREATE = 600
TIMEOUT_PIP_INSTALL = 2400
TIMEOUT_PROBE = 300
TIMEOUT_CLI = 300

# Grace period for reaping a timed-out child's (SIGKILLed) process group.
TIMEOUT_REAP = 30

# The extra whose dependencies are proven by the analysis consumer cells.
ANALYSIS_EXTRA = "analysis"

# Bounded length of captured child output kept in error context.
ERROR_OUTPUT_TAIL = 2000

# Core module imports proven without constructing any device.
CORE_LIBRARY_MODULES = (
    "meshtastic",
    "meshtastic.serial_interface",
    "meshtastic.tcp_interface",
    "meshtastic.ble_interface",
)

# Documented offline schema surface (see bin/smoke-standalone.sh).
SCHEMA_FIELD = "lora.hop_limit"
SCHEMA_BOUNDS = (0, 7)


class SmokeGateError(Exception):
    """Fail-closed gate failure with phase/artifact/profile/command context.

    Attributes
    ----------
    phase : str
        Gate phase that failed (for example ``build`` or ``cells``).
    message : str
        Human-readable failure description.
    artifact : str | None
        Artifact path involved in the failure, when known.
    profile : str | None
        Consumer profile involved (for example ``wheel-analysis``), when known.
    command : Sequence[str] | None
        Failing child command, when the failure came from a subprocess.
    detail : str | None
        Bounded captured output or extra context for the failure.
    """

    def __init__(
        self,
        phase: str,
        message: str,
        *,
        artifact: str | None = None,
        profile: str | None = None,
        command: Sequence[str] | None = None,
        detail: str | None = None,
    ) -> None:
        self.phase = phase
        self.message = message
        self.artifact = artifact
        self.profile = profile
        self.command = list(command) if command is not None else None
        self.detail = detail
        super().__init__(message)

    def __str__(self) -> str:
        parts = [f"phase={self.phase}"]
        if self.artifact is not None:
            parts.append(f"artifact={self.artifact}")
        if self.profile is not None:
            parts.append(f"profile={self.profile}")
        if self.command is not None:
            parts.append(f"command={subprocess.list2cmdline(self.command)}")
        text = f"{self.message} ({', '.join(parts)})"
        if self.detail:
            text = f"{text}\n{self.detail}"
        return text


@dataclasses.dataclass(frozen=True)
class CliExpectations:
    """Console-script roles derived from pyproject scripts and branding.

    No binary name is hard-coded: the primary and compatibility names come
    from ``meshtastic/_branding.py`` (read from the repo root at runtime),
    the analysis CLI is the remaining script whose target module lives under
    ``meshtastic.analysis``, and the tunnel CLI is the sole script left over.

    Attributes
    ----------
    primary : str
        The branded primary CLI binary name.
    compat : tuple[str, ...]
        Compatibility CLI binary names (upstream-name wrappers).
    tunnel : str
        The tunnel CLI binary name.
    analysis : str
        The analysis CLI binary name.
    """

    primary: str
    compat: tuple[str, ...]
    tunnel: str
    analysis: str


@dataclasses.dataclass(frozen=True)
class RepoExpectations:
    """Packaging expectations derived from the repository at runtime.

    Attributes
    ----------
    name : str
        Distribution name from ``pyproject.toml``.
    version : str
        Distribution version from ``pyproject.toml``.
    scripts : tuple[str, ...]
        Required console-script names from ``pyproject.toml``.
    script_targets : Mapping[str, str]
        Console-script name to ``module:attr[extra]`` target, verbatim from
        ``pyproject.toml``; built entry-point values must match these.
    cli : CliExpectations
        Role classification of the console scripts (primary/compat/tunnel/
        analysis binary names).
    extras : tuple[str, ...]
        Extra names that must appear in built metadata.
    analysis_extra_deps : tuple[str, ...]
        Dependencies that are BOTH declared ``optional = true`` and listed in
        the analysis extra; each must carry the ``extra == "analysis"``
        marker in built metadata.
    python_requires : str
        Required Python specifier that must be present in built metadata.
    """

    name: str
    version: str
    scripts: tuple[str, ...]
    script_targets: Mapping[str, str]
    cli: CliExpectations
    extras: tuple[str, ...]
    analysis_extra_deps: tuple[str, ...]
    python_requires: str

    @property
    def wheel_name(self) -> str:
        """Return the expected wheel filename."""
        return f"{self.name}-{self.version}-py3-none-any.whl"

    @property
    def sdist_name(self) -> str:
        """Return the expected sdist filename."""
        return f"{self.name}-{self.version}.tar.gz"


@dataclasses.dataclass(frozen=True)
class Artifacts:
    """Located build artifacts.

    Attributes
    ----------
    dist_dir : Path
        Directory containing the artifacts.
    wheel : Path
        Built (or retained) wheel path.
    sdist : Path
        Built (or retained) sdist path.
    """

    dist_dir: Path
    wheel: Path
    sdist: Path

    def summary(self) -> list[str]:
        """Return human-readable size/hash lines for the artifacts."""
        lines = []
        for artifact in (self.wheel, self.sdist):
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            lines.append(
                f"{artifact.name}: {artifact.stat().st_size} bytes sha256={digest}"
            )
        return lines


@dataclasses.dataclass(frozen=True)
class CellPlan:
    """One isolated consumer cell.

    Attributes
    ----------
    profile : str
        Cell identifier (for example ``wheel-analysis``).
    artifact : Path
        Artifact installed into the cell venv.
    extras : tuple[str, ...]
        Extras requested on the install (``[analysis]`` / ``[cli]``).
    """

    profile: str
    artifact: Path
    extras: tuple[str, ...]

    @property
    def is_core(self) -> bool:
        """Return True for core cells (the controlled ``[cli]`` extra)."""
        return self.extras == ("cli",)

    @property
    def requirement(self) -> str:
        """Return the primary pip requirement string for this cell."""
        spec = str(self.artifact)
        if self.extras and not self.is_core:
            spec = f"{spec}[{','.join(self.extras)}]"
        return spec

    @property
    def compat_requirement(self) -> str | None:
        """Return the controlled compatibility-step requirement, if any.

        Core cells additionally prove that the intentionally empty ``[cli]``
        extra is accepted by normal pip resolution.
        """
        if self.is_core:
            return f"{self.artifact}[cli]"
        return None


def _tail(text: str | None, limit: int = ERROR_OUTPUT_TAIL) -> str:
    """Return the tail of captured output, bounded for error context."""
    if not text:
        return ""
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return cleaned
    return f"...[truncated]...\n{cleaned[-limit:]}"


def sanitize_child_env(
    containment_dir: Path | None = None,
    extra_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a child environment cleared of path-poisoning variables.

    The gate must probe installed artifacts, so ``PYTHONPATH`` and
    ``PYTHONHOME`` are removed; everything else (proxies, pip cache, locale)
    is passed through untouched. Secret-bearing variables are never printed.

    When ``containment_dir`` is given (consumer cells), ``HOME`` and
    ``XDG_DATA_HOME`` are pointed at owned subdirectories of it, so no
    consumer probe can write global user data: anything platformdirs,
    pip, or ``slog.rootDir`` would persist lands inside the gate's own
    workspace, structurally. ``extra_env`` entries are applied last and
    override the containment defaults.
    """
    env = dict(os.environ)
    for poison in ("PYTHONPATH", "PYTHONHOME"):
        env.pop(poison, None)
    if containment_dir is not None:
        home = containment_dir / "home"
        xdg_data = containment_dir / "xdg-data"
        home.mkdir(parents=True, exist_ok=True)
        xdg_data.mkdir(parents=True, exist_ok=True)
        env["HOME"] = str(home)
        env["XDG_DATA_HOME"] = str(xdg_data)
    if extra_env:
        env.update({key: str(value) for key, value in extra_env.items()})
    return env


def _kill_process_group(process: subprocess.Popen[str]) -> None:
    """SIGKILL the child's whole process group so no grandchild survives.

    Every ``run_command`` child starts its own session (``start_new_session``),
    so the child's pid is also its process-group id.
    """
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        process.kill()
    except OSError:
        pass


def _stream_text(value: object) -> str:
    """Decode partial captured output from a TimeoutExpired exception."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def run_command(
    argv: Sequence[str],
    *,
    phase: str,
    cwd: Path,
    timeout: int,
    profile: str | None = None,
    artifact: str | None = None,
    containment_dir: Path | None = None,
    extra_env: Mapping[str, str] | None = None,
) -> str:
    """Run a child command to completion, returning captured stdout.

    Parameters
    ----------
    argv : Sequence[str]
        Argument vector executed directly (never via a shell).
    phase : str
        Gate phase label used in error context.
    cwd : Path
        Neutral working directory for the child.
    timeout : int
        Bounded timeout in seconds; the child's whole process group is
        SIGKILLed when exceeded (timed-out grandchildren cannot outlive
        the gate).
    profile : str | None
        Optional consumer profile for error context.
    artifact : str | None
        Optional artifact path for error context.
    containment_dir : Path | None
        Optional owned directory; when given, ``HOME``/``XDG_DATA_HOME``
        point inside it (see :func:`sanitize_child_env`).
    extra_env : Mapping[str, str] | None
        Optional extra environment entries for this child only.

    Returns
    -------
    str
        Captured stdout of the successful command.

    Raises
    ------
    SmokeGateError
        When the command exits nonzero or exceeds the timeout.
    """
    try:
        process = subprocess.Popen(
            [str(part) for part in argv],
            cwd=str(cwd),
            env=sanitize_child_env(containment_dir, extra_env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise SmokeGateError(
            phase,
            f"required binary is missing: {argv[0]}",
            artifact=artifact,
            profile=profile,
            command=argv,
        ) from exc
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill_process_group(process)
        try:
            process.wait(timeout=TIMEOUT_REAP)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
        raise SmokeGateError(
            phase,
            f"command timed out after {timeout}s (process group killed)",
            artifact=artifact,
            profile=profile,
            command=argv,
            detail=_tail(f"{_stream_text(exc.stdout)}\n{_stream_text(exc.stderr)}"),
        ) from exc
    finally:
        if process.poll() is None:
            _kill_process_group(process)
            try:
                process.wait(timeout=TIMEOUT_REAP)
            except subprocess.TimeoutExpired:
                pass
    if process.returncode != 0:
        raise SmokeGateError(
            phase,
            f"command failed with exit code {process.returncode}",
            artifact=artifact,
            profile=profile,
            command=argv,
            detail=_tail(f"{stderr}\n{stdout}"),
        )
    return stdout


def _load_branding(repo_root: Path) -> ModuleType:
    """Load ``meshtastic/_branding.py`` from the checkout, without imports.

    Parameters
    ----------
    repo_root : Path
        Repository checkout root.

    Returns
    -------
    ModuleType
        The executed branding module.

    Raises
    ------
    SmokeGateError
        When the module is missing or cannot be executed.
    """
    path = repo_root / "meshtastic" / "_branding.py"
    if not path.is_file():
        raise SmokeGateError("preflight", f"branding module is missing: {path}")
    spec = importlib.util.spec_from_file_location("_smoke_gate_branding", path)
    if spec is None or spec.loader is None:
        raise SmokeGateError("preflight", f"cannot load branding module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _derive_cli_expectations(
    scripts: tuple[str, ...],
    script_targets: Mapping[str, str],
    branding: ModuleType,
) -> CliExpectations:
    """Classify console scripts into CLI roles; fail closed on ambiguity.

    The primary and compatibility binary names come from the branding module
    (``PRIMARY_CLI_NAME`` / ``COMPATIBILITY_CLI_NAMES``); of the remaining
    scripts, the one targeting a ``meshtastic.analysis.*`` module is the
    analysis CLI and the single remaining one is the tunnel CLI.
    """
    script_set = set(scripts)
    primary = str(getattr(branding, "PRIMARY_CLI_NAME", ""))
    compat = tuple(
        str(member) for member in getattr(branding, "COMPATIBILITY_CLI_NAMES", ()) or ()
    )
    if primary not in script_set:
        raise SmokeGateError(
            "preflight",
            f"branding PRIMARY_CLI_NAME {primary!r} is not a pyproject "
            "console script",
        )
    unknown_compat = sorted(set(compat) - script_set)
    if unknown_compat:
        raise SmokeGateError(
            "preflight",
            "branding compatibility CLI names are not pyproject console "
            f"scripts: {unknown_compat}",
        )
    if primary in compat:
        raise SmokeGateError(
            "preflight",
            f"branding PRIMARY_CLI_NAME duplicates a compatibility name: "
            f"{primary!r}",
        )

    def target_module(script: str) -> str:
        return script_targets[script].split(":", 1)[0].strip()

    remaining = sorted(script_set - {primary, *compat})
    analysis = [
        script
        for script in remaining
        if target_module(script).startswith("meshtastic.analysis")
    ]
    if len(analysis) != 1:
        raise SmokeGateError(
            "preflight",
            "expected exactly one console script targeting "
            f"meshtastic.analysis.*, found: {analysis or 'none'}",
        )
    tunnel = [script for script in remaining if script != analysis[0]]
    if len(tunnel) != 1:
        raise SmokeGateError(
            "preflight",
            "expected exactly one remaining console script for the tunnel "
            f"CLI, found: {tunnel or 'none'}",
        )
    return CliExpectations(
        primary=primary, compat=compat, tunnel=tunnel[0], analysis=analysis[0]
    )


def load_repo_expectations(repo_root: Path) -> RepoExpectations:
    """Derive packaging expectations from the repository's pyproject.toml.

    Parameters
    ----------
    repo_root : Path
        Repository checkout root containing ``pyproject.toml``.

    Returns
    -------
    RepoExpectations
        Runtime-derived expectations; no version, script name, or CLI role
        is hard-coded.

    Raises
    ------
    SmokeGateError
        When required packaging declarations are missing or inconsistent
        (for example an optional dependency belonging to no extra).
    """
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.is_file():
        raise SmokeGateError("preflight", f"pyproject.toml is missing at {pyproject}")
    try:
        raw = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise SmokeGateError(
            "preflight", f"pyproject.toml is not valid TOML: {exc}"
        ) from exc
    try:
        poetry = raw["tool"]["poetry"]
        name = str(poetry["name"])
        version = str(poetry["version"])
        scripts = tuple(sorted(str(key) for key in poetry["scripts"]))
        script_targets = {
            str(key): str(value) for key, value in poetry["scripts"].items()
        }
        extras = tuple(sorted(str(key) for key in poetry["extras"]))
        extras_dependencies = {
            str(extra): tuple(str(dep) for dep in poetry["extras"][extra])
            for extra in poetry["extras"]
        }
        python_requires = str(poetry["dependencies"]["python"])
        optional_deps = tuple(
            sorted(
                str(dep)
                for dep, spec in poetry["dependencies"].items()
                if isinstance(spec, Mapping) and spec.get("optional") is True
            )
        )
    except (KeyError, TypeError) as exc:
        raise SmokeGateError(
            "preflight",
            f"pyproject.toml is missing required packaging declarations: {exc!r}",
        ) from exc
    covered = {
        _canonical_name(dep)
        for members in extras_dependencies.values()
        for dep in members
    }
    orphans = [dep for dep in optional_deps if _canonical_name(dep) not in covered]
    if orphans:
        raise SmokeGateError(
            "preflight",
            "optional dependencies belong to no extra (real packaging "
            f"defect): {orphans}",
        )
    analysis_deps = tuple(
        dep
        for dep in optional_deps
        if _canonical_name(dep)
        in {
            _canonical_name(member)
            for member in extras_dependencies.get(ANALYSIS_EXTRA, ())
        }
    )
    if not analysis_deps:
        raise SmokeGateError(
            "preflight",
            f"no optional dependencies are scoped to the {ANALYSIS_EXTRA} extra",
        )
    cli = _derive_cli_expectations(scripts, script_targets, _load_branding(repo_root))
    expectations = RepoExpectations(
        name=name,
        version=version,
        scripts=scripts,
        script_targets=script_targets,
        cli=cli,
        extras=extras,
        analysis_extra_deps=analysis_deps,
        python_requires=python_requires,
    )
    for label, value in (
        ("scripts", expectations.scripts),
        ("extras", expectations.extras),
        (f"{ANALYSIS_EXTRA}-extra dependencies", expectations.analysis_extra_deps),
    ):
        if not value:
            raise SmokeGateError("preflight", f"pyproject.toml declares no {label}")
    _LOGGER.info(
        "[preflight] expectations: dist=%s version=%s scripts=%s extras=%s "
        "analysis-extra-deps=%s cli=%s",
        expectations.name,
        expectations.version,
        ",".join(expectations.scripts),
        ",".join(expectations.extras),
        ",".join(expectations.analysis_extra_deps),
        dataclasses.asdict(cli),
    )
    return expectations


def expected_pb2_modules(repo_root: Path) -> tuple[str, ...]:
    """Return the protobuf module stems generated in this repository.

    Parameters
    ----------
    repo_root : Path
        Repository checkout root.

    Returns
    -------
    tuple[str, ...]
        Sorted ``*_pb2`` module stems (for example ``mesh_pb2``).
    """
    proto_dir = repo_root / "meshtastic" / "protobuf"
    if not proto_dir.is_dir():
        raise SmokeGateError(
            "preflight", f"generated protobuf directory is missing: {proto_dir}"
        )
    stems = tuple(
        sorted(path.name[: -len("_pb2.py")] for path in proto_dir.glob("*_pb2.py"))
    )
    if not stems:
        raise SmokeGateError(
            "preflight", f"no generated *_pb2.py modules found in {proto_dir}"
        )
    return stems


def locate_artifacts(
    dist_dir: Path, expectations: RepoExpectations, *, skip_build: bool
) -> Artifacts:
    """Locate (or prepare the directory for) the artifacts to validate.

    Parameters
    ----------
    dist_dir : Path
        Directory holding (or that will hold) the artifacts.
    expectations : RepoExpectations
        Runtime-derived expectations for artifact filenames.
    skip_build : bool
        When True the artifacts must already exist in ``dist_dir``.

    Returns
    -------
    Artifacts
        Located wheel and sdist.

    Raises
    ------
    SmokeGateError
        When the directory state is unsuitable (stale files, missing
        artifacts), so pre-existing junk can never pass as a fresh build.
    """
    if skip_build:
        if not dist_dir.is_dir():
            raise SmokeGateError("build", f"dist directory does not exist: {dist_dir}")
        wheel = dist_dir / expectations.wheel_name
        sdist = dist_dir / expectations.sdist_name
        for artifact, label in ((wheel, "wheel"), (sdist, "sdist")):
            if not artifact.is_file():
                raise SmokeGateError(
                    "build",
                    f"expected {label} artifact is missing: {artifact.name}",
                    artifact=str(artifact),
                    detail=(
                        f"directory contents: "
                        f"{sorted(p.name for p in dist_dir.iterdir())}"
                    ),
                )
        return Artifacts(dist_dir=dist_dir, wheel=wheel, sdist=sdist)
    if dist_dir.exists() and any(dist_dir.iterdir()):
        raise SmokeGateError(
            "build",
            "dist directory is not empty; stale artifacts cannot pass the "
            "gate (remove them or pass --skip-build)",
            artifact=str(dist_dir),
        )
    dist_dir.mkdir(parents=True, exist_ok=True)
    return Artifacts(
        dist_dir=dist_dir,
        wheel=dist_dir / expectations.wheel_name,
        sdist=dist_dir / expectations.sdist_name,
    )


def build_artifacts(repo_root: Path, dist_dir: Path) -> None:
    """Build the sdist and wheel with ``python -m build``.

    Parameters
    ----------
    repo_root : Path
        Repository checkout root passed as the build working directory.
    dist_dir : Path
        Fresh output directory for the built artifacts.

    Raises
    ------
    SmokeGateError
        When the build tool is unavailable or the build fails.
    """
    if importlib.util.find_spec("build") is None:
        raise SmokeGateError(
            "build",
            "the 'build' tool is not importable by this interpreter; "
            "install it with: pip install build",
        )
    _LOGGER.info("[build] running: python -m build --outdir %s", dist_dir)
    run_command(
        [sys.executable, "-m", "build", "--outdir", str(dist_dir)],
        phase="build",
        cwd=repo_root,
        timeout=TIMEOUT_BUILD,
        artifact=str(dist_dir),
    )
    _LOGGER.info("[build] build completed")


def _canonical_name(name: str) -> str:
    """PEP 503-normalize a distribution name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_name(line: str) -> str:
    """Extract the PEP 503-normalized distribution name from Requires-Dist."""
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", line)
    if match is None:
        return ""
    return _canonical_name(match.group(1))


# Tolerant marker matching: whitespace and quote style may vary between
# poetry-core versions and metadata normalizers.
_ANY_EXTRA_MARKER_RE = re.compile(r"extra\s*==")
_ANALYSIS_EXTRA_MARKER_RE = re.compile(
    rf"extra\s*==\s*['\"]{re.escape(ANALYSIS_EXTRA)}['\"]"
)


def _validate_requires_dist(
    requires_dist: Sequence[str], expectations: RepoExpectations
) -> None:
    """Require every analysis-extra dependency to carry the analysis marker.

    Only dependencies that are BOTH declared ``optional = true`` in pyproject
    AND listed in the analysis extra are held to the marker requirement, so
    optional dependencies of other extras (or of no extra, rejected earlier)
    are never conflated with it. Names are compared after PEP 503
    normalization and markers are matched tolerantly.
    """
    for dep in expectations.analysis_extra_deps:
        lines = [line for line in requires_dist if _requirement_name(line) == dep]
        if not lines:
            raise SmokeGateError(
                "contents",
                f"analysis extra dependency '{dep}' is missing from built "
                "metadata; an [analysis] install would not receive it",
            )
        unconditional = [
            line for line in lines if not _ANY_EXTRA_MARKER_RE.search(line)
        ]
        if unconditional:
            raise SmokeGateError(
                "contents",
                f"dependency '{dep}' is declared unconditionally; it must only "
                f"be required via the analysis extra",
                detail="\n".join(unconditional),
            )
        marked = [line for line in lines if _ANALYSIS_EXTRA_MARKER_RE.search(line)]
        if not marked:
            raise SmokeGateError(
                "contents",
                f"dependency '{dep}' does not carry the " 'extra == "analysis" marker',
                detail="\n".join(lines),
            )


def _expand_poetry_caret(constraint: str) -> str:
    """Expand poetry caret constraints to PEP 440 bounds, per comma part.

    For example ``^3.11`` becomes ``>=3.11,<4.0.0`` and ``^0.2.3`` becomes
    ``>=0.2.3,<0.3.0``. Non-caret parts pass through unchanged.
    """
    expanded = []
    for part in constraint.split(","):
        part = part.strip()
        match = re.fullmatch(r"\^(\d+)(?:\.(\d+))?(?:\.(\d+))?", part)
        if match is None:
            expanded.append(part)
            continue
        major = int(match.group(1))
        minor = match.group(2)
        patch = match.group(3)
        floor = part[1:]
        if minor is None:
            cap = f"{major + 1}.0.0"
        elif patch is None:
            cap = f"0.{int(minor) + 1}.0" if major == 0 else f"{major + 1}.0.0"
        elif major == 0 and int(minor) == 0:
            cap = f"0.0.{int(patch) + 1}"
        elif major == 0:
            cap = f"0.{int(minor) + 1}.0"
        else:
            cap = f"{major + 1}.0.0"
        expanded.append(f">={floor},<{cap}")
    return ",".join(expanded)


def _compare_release_versions(left: str, right: str) -> int:
    """Return -1/0/1 comparing two plain release version strings."""
    if Version is not None:
        left_v, right_v = Version(left), Version(right)
        return -1 if left_v < right_v else (1 if left_v > right_v else 0)
    left_parts = [int(piece) for piece in left.split(".")]
    right_parts = [int(piece) for piece in right.split(".")]
    width = max(len(left_parts), len(right_parts))
    left_parts += [0] * (width - len(left_parts))
    right_parts += [0] * (width - len(right_parts))
    return (left_parts > right_parts) - (left_parts < right_parts)


def _python_range_bounds(
    constraint: str, *, phase: str
) -> tuple[tuple[str, str] | None, tuple[str, str] | None]:
    """Reduce a specifier string to ((floor_op, floor), (cap_op, cap)).

    Only floor (``>=``, ``>``) and cap (``<``, ``<=``) operators are
    supported; anything else fails closed, because equivalence of arbitrary
    specifier sets cannot be decided by this reduction. Uses
    ``packaging.specifiers.SpecifierSet`` for parsing when available, with a
    plain-release regex fallback so the gate never needs a new dependency.
    """
    if SpecifierSet is not None:
        try:
            specs = [(spec.operator, spec.version) for spec in SpecifierSet(constraint)]
        except InvalidSpecifier as exc:
            raise SmokeGateError(
                phase,
                f"unparseable python version constraint {constraint!r}: {exc}",
            ) from exc
    else:
        specs = []
        for part in constraint.split(","):
            match = re.fullmatch(r"\s*(>=|<=|>|<)\s*([0-9]+(?:\.[0-9]+)*)\s*", part)
            if match is None:
                raise SmokeGateError(
                    phase,
                    f"python version constraint {constraint!r} uses a form the "
                    "fallback parser cannot read and packaging is unavailable; "
                    "cannot prove Requires-Python equivalence",
                )
            specs.append((match.group(1), match.group(2)))
    floor: tuple[str, str] | None = None
    cap: tuple[str, str] | None = None
    for op, bound in specs:
        if op not in (">=", ">", "<", "<="):
            raise SmokeGateError(
                phase,
                f"python version constraint {constraint!r} uses operator "
                f"{op!r}; only floor/cap bounds can be proven equivalent",
            )
        if op in (">=", ">"):
            if (
                floor is None
                or _compare_release_versions(bound, floor[1]) > 0
                or (_compare_release_versions(bound, floor[1]) == 0 and op == ">")
            ):
                floor = (op, bound)
        elif (
            cap is None
            or _compare_release_versions(bound, cap[1]) < 0
            or (_compare_release_versions(bound, cap[1]) == 0 and op == "<")
        ):
            cap = (op, bound)
    return floor, cap


def _validate_requires_python(
    requires_python: str, expectations: RepoExpectations
) -> None:
    """Prove built Requires-Python matches the source-derived expectation.

    The pyproject constraint uses poetry syntax (caret bounds); it is
    expanded to PEP 440, and both sides are reduced to (floor, cap) bounds
    that must agree exactly, so a metadata drift such as a silently widened
    or narrowed Python range cannot pass.
    """
    expected_floor, expected_cap = _python_range_bounds(
        _expand_poetry_caret(expectations.python_requires), phase="contents"
    )
    built_floor, built_cap = _python_range_bounds(requires_python, phase="contents")

    def bound_matches(
        built: tuple[str, str] | None, expected: tuple[str, str] | None
    ) -> bool:
        if built is None or expected is None:
            return built is None and expected is None
        return built[0] == expected[0] and (
            _compare_release_versions(built[1], expected[1]) == 0
        )

    if not (
        bound_matches(built_floor, expected_floor)
        and bound_matches(built_cap, expected_cap)
    ):
        raise SmokeGateError(
            "contents",
            "built Requires-Python does not match the source-derived expectation",
            detail=(
                f"built={requires_python!r} (floor={built_floor} cap={built_cap})"
                f" expected from pyproject="
                f"{expectations.python_requires!r} "
                f"(floor={expected_floor} cap={expected_cap})"
            ),
        )


def _validate_record(record_text: str, dist_info: str) -> None:
    """Require the wheel RECORD ledger to account for packaged markers.

    Parameters
    ----------
    record_text : str
        Raw contents of the wheel's ``RECORD`` file.
    dist_info : str
        ``*.dist-info`` directory name inside the wheel.

    Raises
    ------
    SmokeGateError
        When ``RECORD`` itself or the ``meshtastic/py.typed`` marker entry is
        missing.
    """
    if not record_text.strip():
        raise SmokeGateError("contents", "wheel RECORD is empty")
    paths = [row[0] for row in csv.reader(io.StringIO(record_text)) if row and row[0]]
    if f"{dist_info}/RECORD" not in paths:
        raise SmokeGateError(
            "contents", f"wheel RECORD does not account for {dist_info}/RECORD"
        )
    if "meshtastic/py.typed" not in paths:
        raise SmokeGateError(
            "contents",
            "wheel RECORD does not account for meshtastic/py.typed; the PEP 561 "
            "marker would be absent from installed distributions",
        )


_SCRIPT_EXTRA_SUFFIX_RE = re.compile(r"\s*\[\s*([^\]\s]+)\s*\]\s*$")


def _normalize_script_target(value: str) -> str:
    """Normalize an entry-point target for comparison.

    Collapses whitespace runs and glues the legacy ``[extra]`` suffix onto
    the callable, so pyproject's ``module:attr [extra]`` compares equal to
    the ``module:attr[extra]`` form poetry-core writes into
    entry_points.txt.
    """
    collapsed = " ".join(value.split())
    return re.sub(r"\s+\[", "[", collapsed)


def _validate_entry_points(
    entry_points_text: str, expectations: RepoExpectations
) -> None:
    """Require all expected console scripts with their declared targets.

    Every script VALUE from the built entry_points.txt is compared against
    the ``module:attr[extra]`` target derived from pyproject
    ``[tool.poetry.scripts]`` (whitespace-normalized); a script whose
    pyproject target carries the ``[analysis]`` extra marker must keep it.

    Raises
    ------
    SmokeGateError
        When any expected console script is missing or has an unexpected
        value.
    """
    parser = configparser.ConfigParser()
    # Entry-point names are case-sensitive; the default optionxform would
    # silently lowercase them and misreport mixed-case script names.
    parser.optionxform = str
    try:
        parser.read_string(entry_points_text)
    except configparser.Error as exc:
        raise SmokeGateError(
            "contents", f"wheel entry_points.txt is unparseable: {exc}"
        ) from exc
    if not parser.has_section("console_scripts"):
        raise SmokeGateError(
            "contents", "wheel entry_points.txt has no [console_scripts] section"
        )
    present = set(parser.options("console_scripts"))
    missing = sorted(set(expectations.scripts) - present)
    if missing:
        raise SmokeGateError(
            "contents",
            f"built entry points are missing console scripts: {missing}",
            detail=f"present: {sorted(present)}",
        )
    for script in expectations.scripts:
        expected_target = expectations.script_targets[script]
        observed_raw = parser.get("console_scripts", script)
        suffix = _SCRIPT_EXTRA_SUFFIX_RE.search(expected_target)
        if suffix is not None and f"[{suffix.group(1)}]" not in observed_raw:
            raise SmokeGateError(
                "contents",
                f"console script '{script}' lost its [{suffix.group(1)}] extra "
                "marker in built entry points; installs without the extra "
                "would still create the script",
                detail=f"observed={observed_raw!r} " f"expected={expected_target!r}",
            )
        observed = _normalize_script_target(observed_raw)
        expected = _normalize_script_target(expected_target)
        if observed != expected:
            raise SmokeGateError(
                "contents",
                f"console script '{script}' has an unexpected target",
                detail=f"observed={observed!r} expected={expected!r}",
            )


def validate_wheel_contents(
    wheel: Path,
    expectations: RepoExpectations,
    pb2_stems: Sequence[str],
) -> None:
    """Validate wheel RECORD, METADATA, entry points, and protobuf members.

    Raises
    ------
    SmokeGateError
        On any missing marker, metadata member, entry point, or protobuf
        module; expectations are runtime-derived.
    """
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        metadata_paths = sorted(
            name for name in names if name.endswith(".dist-info/METADATA")
        )
        if len(metadata_paths) != 1:
            raise SmokeGateError(
                "contents",
                "wheel must contain exactly one .dist-info/METADATA",
                artifact=str(wheel),
                detail=f"found: {metadata_paths}",
            )
        dist_info = metadata_paths[0][: -len("METADATA")]
        metadata_text = archive.read(metadata_paths[0]).decode("utf-8")
        record_name = f"{dist_info}RECORD"
        if record_name not in names:
            raise SmokeGateError(
                "contents", f"wheel is missing {record_name}", artifact=str(wheel)
            )
        record_text = archive.read(record_name).decode("utf-8")
        entry_points_name = f"{dist_info}entry_points.txt"
        if entry_points_name not in names:
            raise SmokeGateError(
                "contents",
                f"wheel is missing {entry_points_name}",
                artifact=str(wheel),
            )
        entry_points_text = archive.read(entry_points_name).decode("utf-8")
        wheel_paths = set(names)
        record_paths = {
            row[0] for row in csv.reader(io.StringIO(record_text)) if row and row[0]
        }
        for stem in pb2_stems:
            for member in (
                f"meshtastic/protobuf/{stem}_pb2.py",
                f"meshtastic/protobuf/{stem}_pb2.pyi",
            ):
                if member not in wheel_paths:
                    raise SmokeGateError(
                        "contents",
                        f"wheel is missing generated protobuf member {member}",
                        artifact=str(wheel),
                    )
                if member not in record_paths:
                    raise SmokeGateError(
                        "contents",
                        f"wheel RECORD does not account for {member}",
                        artifact=str(wheel),
                    )
        unpackaged = sorted(
            name
            for name in wheel_paths
            if name.startswith("meshtastic/") and name not in record_paths
        )
        if unpackaged:
            raise SmokeGateError(
                "contents",
                f"wheel RECORD does not account for {len(unpackaged)} packaged "
                "meshtastic member(s); they would be absent from the installed "
                "distribution's uninstall ledger",
                artifact=str(wheel),
                detail="\n".join(unpackaged[:20]),
            )

    _validate_record(record_text, dist_info.rstrip("/"))

    message = Parser().parsestr(metadata_text)
    requires_python = (message.get("Requires-Python") or "").strip()
    if not requires_python:
        raise SmokeGateError("contents", "wheel metadata is missing Requires-Python")
    _validate_requires_python(requires_python, expectations)
    provides = {value.strip() for value in (message.get_all("Provides-Extra") or [])}
    missing_extras = sorted(set(expectations.extras) - provides)
    if missing_extras:
        raise SmokeGateError(
            "contents",
            f"built metadata is missing Provides-Extra entries: {missing_extras}",
            detail=f"present: {sorted(provides)}",
        )
    requires_dist = [value for value in (message.get_all("Requires-Dist") or [])]
    if not requires_dist:
        raise SmokeGateError(
            "contents", "built metadata declares no Requires-Dist entries"
        )
    _validate_requires_dist(requires_dist, expectations)
    _validate_entry_points(entry_points_text, expectations)
    _LOGGER.info("[contents] wheel OK: %s", wheel.name)


def validate_sdist_contents(
    sdist: Path,
    expectations: RepoExpectations,
    pb2_stems: Sequence[str],
) -> None:
    """Validate that the sdist carries the generated protobuf sources.

    Raises
    ------
    SmokeGateError
        When the sdist lacks the py.typed marker or any ``*_pb2.py``/``*_pb2.pyi``
        member.
    """
    with tarfile.open(sdist) as archive:
        names = archive.getnames()
    required_members = ["meshtastic/py.typed"]
    for stem in pb2_stems:
        required_members.extend(
            (
                f"meshtastic/protobuf/{stem}_pb2.py",
                f"meshtastic/protobuf/{stem}_pb2.pyi",
            )
        )
    for member in required_members:
        if not any(name.endswith(member) for name in names):
            raise SmokeGateError(
                "contents",
                f"sdist is missing member {member}",
                artifact=str(sdist),
            )
    _LOGGER.info("[contents] sdist OK: %s", sdist.name)


def validate_describe_field_document(document: Mapping[str, Any]) -> None:
    """Validate the JSON document for ``--describe-field lora.hop_limit``.

    Raises
    ------
    SmokeGateError
        When the document does not carry the documented 0..7 bounds.
    """
    field = document.get("field")
    if field != SCHEMA_FIELD:
        raise SmokeGateError(
            "schema",
            f"--describe-field returned field {field!r}, expected {SCHEMA_FIELD!r}",
            detail=f"document keys: {sorted(document)}",
        )
    low, high = SCHEMA_BOUNDS
    if document.get("min_value") != low or document.get("max_value") != high:
        raise SmokeGateError(
            "schema",
            f"{SCHEMA_FIELD} bounds are not the documented {low}..{high}",
            detail=json.dumps(document, sort_keys=True)[:ERROR_OUTPUT_TAIL],
        )


def validate_list_fields_document(document: Mapping[str, Any]) -> None:
    """Validate the JSON document for ``--list-fields --json``.

    Raises
    ------
    SmokeGateError
        When config/module field descriptors are missing or no ``lora`` fields
        are present.
    """
    sections = {}
    for key in ("config_fields", "module_config_fields"):
        value = document.get(key)
        if not isinstance(value, list) or not value:
            raise SmokeGateError(
                "schema",
                f"--list-fields section {key!r} is missing or empty",
                detail=f"document keys: {sorted(document)}",
            )
        sections[key] = value
        for entry in value:
            if not isinstance(entry, Mapping) or not entry.get("field"):
                raise SmokeGateError(
                    "schema",
                    f"--list-fields section {key!r} has a malformed entry",
                    detail=repr(entry)[:ERROR_OUTPUT_TAIL],
                )
    all_fields = [
        str(entry.get("field")) for value in sections.values() for entry in value
    ]
    if not any(name.startswith("lora.") for name in all_fields):
        raise SmokeGateError(
            "schema",
            "--list-fields emitted no lora.* config fields",
            detail=f"sample fields: {sorted(all_fields)[:20]}",
        )


_INSTALL_IDENTITY_PROBE = r"""
import importlib.metadata as md
import importlib.resources
import json
import sys

dist_name = sys.argv[1]
dist = md.distribution(dist_name)
import meshtastic
import meshtastic.protobuf
py_typed = importlib.resources.files("meshtastic").joinpath("py.typed")
print(json.dumps({
    "name": dist.metadata["Name"],
    "version": dist.metadata["Version"],
    "meshtastic_file": meshtastic.__file__,
    "protobuf_file": meshtastic.protobuf.__file__,
    "py_typed_via_resources": py_typed.is_file(),
}))
"""

_LIBRARY_IMPORT_LINES = "".join(
    f"import {module}  # noqa: F401  (import-only surface)\n"
    for module in CORE_LIBRARY_MODULES
)

_LIBRARY_PROBE = (
    "import json\n\n"
    + _LIBRARY_IMPORT_LINES
    + "from meshtastic.protobuf import mesh_pb2, portnums_pb2\n"
    + r"""
facade = meshtastic.mesh_pb2
payload = b"artifact-smoke-gate"
packet = mesh_pb2.MeshPacket()
packet.to = 0x1234ABCD
packet.id = 0x0BADF00D
packet.hop_limit = 3
packet.want_ack = True
packet.decoded.portnum = portnums_pb2.PRIVATE_APP
packet.decoded.payload = payload
to_radio = mesh_pb2.ToRadio(packet=packet)
wire = to_radio.SerializeToString()
parsed = mesh_pb2.ToRadio()
consumed = parsed.ParseFromString(wire)
back = parsed.packet
print(json.dumps({
    "wire_len": consumed,
    "to": back.to == 0x1234ABCD,
    "id": back.id == 0x0BADF00D,
    "hop_limit": back.hop_limit == 3,
    "want_ack": back.want_ack is True,
    "portnum": back.decoded.portnum == portnums_pb2.PRIVATE_APP,
    "payload": back.decoded.payload == payload,
    "facade_is_canonical": facade is meshtastic.protobuf.mesh_pb2,
    "facade_mesh_packet": facade.MeshPacket is mesh_pb2.MeshPacket,
}))
"""
)

_CORE_LIGHT_PROBE = r"""
import json

failures = []
for module in ("pyarrow", "dash"):
    try:
        __import__(module)
    except ImportError:
        pass
    else:
        failures.append(module)
print(json.dumps({"unexpectedly_importable": failures}))
"""

# Production data path probe: create_dash() is the real consumer of slog
# Feather inputs (the installed analysis data path), unlike the
# --no-server CLI run which exits before any read.
_ANALYSIS_CREATE_DASH_PROBE = r"""
import json
import sys

from dash import Dash, dcc
from meshtastic.analysis.__main__ import create_dash


def _axis_len(axis):
    try:
        return 0 if axis is None else len(axis)
    except TypeError:
        return 0


app = create_dash(slog_path=sys.argv[1])
graphs = [c for c in app.layout if isinstance(c, dcc.Graph)]
points = sum(_axis_len(trace.x) for g in graphs for trace in g.figure.data)
print(json.dumps({
    "is_dash_app": isinstance(app, Dash),
    "layout_set": app.layout is not None,
    "graph_count": len(graphs),
    "trace_count": sum(len(g.figure.data) for g in graphs),
    "plotted_points": points,
}))
"""

_ANALYSIS_READ_PROBE = r"""
import json
import sys

import pandas as pd
from meshtastic.analysis.__main__ import read_pandas
import pyarrow as pa
import pyarrow.feather as feather

table = pa.table({
    "n": pa.array([1, None, 3], type=pa.int64()),
    "b": pa.array([True, None, False], type=pa.bool_()),
    "s": pa.array(["alpha", None, "gamma"], type=pa.string()),
    "f": pa.array([0.5, None, 2.25], type=pa.float64()),
})
feather.write_feather(table, sys.argv[1])
frame = read_pandas(sys.argv[1]).reset_index(drop=True)
expected = {
    "n": pd.Series([1, pd.NA, 3], dtype="Int64"),
    "b": pd.Series([True, pd.NA, False], dtype="boolean"),
    "s": pd.Series(["alpha", pd.NA, "gamma"], dtype="string[pyarrow]"),
    "f": pd.Series([0.5, pd.NA, 2.25], dtype="Float64"),
}
dtypes = {column: str(frame.dtypes[column]) for column in expected}
expected_dtypes = {"n": "Int64", "b": "boolean", "s": "string[pyarrow]", "f": "Float64"}
# ArrowDtype Series.equals mishandles <NA>; compare NA masks and dropped values.
values_ok = all(
    expected[c].isna().equals(frame[c].isna())
    and expected[c].dropna().tolist() == frame[c].dropna().tolist()
    for c in expected
)
print(json.dumps({
    "dtypes": dtypes,
    "expected_dtypes": expected_dtypes,
    "values_ok": values_ok,
}))
"""


def _write_probe(path: Path, source: str) -> None:
    """Write a probe script into an owned cell directory."""
    path.write_text(source, encoding="utf-8")


def _venv_site_packages(venv_dir: Path) -> Path:
    """Return the venv's site-packages directory (single interpreter)."""
    matches = sorted(venv_dir.glob("lib/python*/site-packages"))
    if not matches:
        raise SmokeGateError(
            "cells", f"venv has no site-packages directory: {venv_dir}"
        )
    return matches[-1]


def _assert_no_editable_install(
    site_packages: Path, repo_root: Path, expectations: RepoExpectations
) -> None:
    """Fail when the venv shows any editable/egg-link install of the project.

    Raises
    ------
    SmokeGateError
        When an editable finder, ``.pth``, or ``*.egg-link`` references the
        source tree or distribution.
    """
    offenders: list[str] = []
    for path in sorted(site_packages.rglob("*")):
        lowered = path.name.lower()
        if lowered.endswith(".egg-link") and expectations.name in lowered:
            offenders.append(str(path))
        elif lowered.endswith(".pth"):
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                content = ""
            if (
                "__editable__" in lowered
                or str(repo_root) in content
                or expectations.name in content
            ):
                offenders.append(str(path))
        elif lowered.startswith("__editable__"):
            offenders.append(str(path))
    for dist_info in sorted(site_packages.glob("*.dist-info")):
        direct_url = dist_info / "direct_url.json"
        if direct_url.is_file():
            try:
                info = json.loads(direct_url.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                info = {}
            if info.get("editable") is True:
                offenders.append(str(direct_url))
    if offenders:
        raise SmokeGateError(
            "cells",
            "venv contains an editable install of the project",
            detail="\n".join(offenders),
        )


def create_consumer_venv(
    venv_dir: Path, work_dir: Path, *, profile: str
) -> tuple[Path, Path]:
    """Create a fresh, non-editable consumer venv.

    Returns
    -------
    tuple[Path, Path]
        The venv's python binary and its site-packages directory.
    """
    run_command(
        [sys.executable, "-m", "venv", str(venv_dir)],
        phase="cells",
        cwd=work_dir,
        timeout=TIMEOUT_VENV_CREATE,
        profile=profile,
        containment_dir=work_dir,
    )
    venv_python = venv_dir / "bin" / "python"
    if not venv_python.is_file():
        raise SmokeGateError(
            "cells",
            f"venv python binary is missing: {venv_python}",
            profile=profile,
        )
    return venv_python, _venv_site_packages(venv_dir)


def install_requirement(
    venv_python: Path,
    requirement: str,
    work_dir: Path,
    *,
    profile: str,
    pip_cache_dir: Path | None = None,
) -> None:
    """pip-install one requirement into the consumer venv.

    The child runs with ``HOME``/``XDG_DATA_HOME`` contained in ``work_dir``
    and, when given, a shared gate-owned pip cache directory.
    """
    extra_env = (
        {"PIP_CACHE_DIR": str(pip_cache_dir)} if pip_cache_dir is not None else None
    )
    _LOGGER.info("[cells] %s: pip install %s", profile, requirement)
    run_command(
        [str(venv_python), "-m", "pip", "--quiet", "install", requirement],
        phase="cells",
        cwd=work_dir,
        timeout=TIMEOUT_PIP_INSTALL,
        profile=profile,
        artifact=requirement,
        containment_dir=work_dir,
        extra_env=extra_env,
    )


def run_probe_json(
    venv_python: Path,
    probe_path: Path,
    work_dir: Path,
    *,
    profile: str,
    phase: str = "cells",
    args: Sequence[str] = (),
) -> dict[str, Any]:
    """Run a probe script with the venv python and parse its JSON output.

    The probe runs in a contained environment (owned ``HOME`` and
    ``XDG_DATA_HOME``), so neither it nor the installed code it exercises
    can write global user data.
    """
    stdout = run_command(
        [str(venv_python), str(probe_path), *args],
        phase=phase,
        cwd=work_dir,
        timeout=TIMEOUT_PROBE,
        profile=profile,
        containment_dir=work_dir,
    )
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise SmokeGateError(
            phase,
            f"probe output is not valid JSON: {exc}",
            profile=profile,
            detail=_tail(stdout),
        ) from exc


def _assert_install_identity(
    observed: Mapping[str, Any],
    venv_dir: Path,
    expectations: RepoExpectations,
    *,
    profile: str,
) -> None:
    """Assert installed name/version, module origins, and resource access."""
    if str(observed.get("name", "")).lower() != expectations.name.lower():
        raise SmokeGateError(
            "cells",
            "installed distribution name does not match the artifact",
            profile=profile,
            detail=f"observed={observed.get('name')!r} "
            f"expected={expectations.name!r}",
        )
    if observed.get("version") != expectations.version:
        raise SmokeGateError(
            "cells",
            "installed distribution version does not match the artifact",
            profile=profile,
            detail=f"observed={observed.get('version')!r} "
            f"expected={expectations.version!r}",
        )
    for key in ("meshtastic_file", "protobuf_file"):
        origin = str(observed.get(key, ""))
        if not origin.startswith(str(venv_dir)) or "site-packages" not in origin:
            raise SmokeGateError(
                "cells",
                f"installed {key} is outside the consumer venv",
                profile=profile,
                detail=f"observed origin: {origin}",
            )
    if observed.get("py_typed_via_resources") is not True:
        raise SmokeGateError(
            "cells",
            "importlib.resources cannot resolve the installed py.typed marker",
            profile=profile,
        )


def _run_core_cli_surfaces(
    venv_dir: Path,
    venv_python: Path,
    work_dir: Path,
    expectations: RepoExpectations,
    *,
    profile: str,
) -> None:
    """Prove offline core CLI surfaces in an installed core cell.

    Binary names come from the runtime-derived CLI expectations (branding
    module plus pyproject scripts), never from literals.
    """
    bin_dir = venv_dir / "bin"
    cli_names = [
        expectations.cli.primary,
        *expectations.cli.compat,
        expectations.cli.tunnel,
    ]
    for name in cli_names:
        if not (bin_dir / name).is_file():
            raise SmokeGateError(
                "cells",
                f"installed console script is missing: {name}",
                profile=profile,
            )
    expected_version = f"{expectations.cli.primary} {expectations.version}"
    for name in (expectations.cli.primary, *expectations.cli.compat):
        argv = [str(bin_dir / name), "--version"]
        stdout = run_command(
            argv,
            phase="core-surfaces",
            cwd=work_dir,
            timeout=TIMEOUT_CLI,
            profile=profile,
            containment_dir=work_dir,
        )
        if stdout.strip() != expected_version:
            raise SmokeGateError(
                "core-surfaces",
                "installed CLI version output does not match the installed "
                "distribution version",
                profile=profile,
                command=argv,
                detail=f"observed={stdout.strip()!r} expected={expected_version!r}",
            )
    module_argv = [str(venv_python), "-m", "meshtastic", "--version"]
    module_stdout = run_command(
        module_argv,
        phase="core-surfaces",
        cwd=work_dir,
        timeout=TIMEOUT_CLI,
        profile=profile,
        containment_dir=work_dir,
    )
    if module_stdout.strip() != expected_version:
        raise SmokeGateError(
            "core-surfaces",
            "installed CLI version output does not match the installed "
            "distribution version",
            profile=profile,
            command=module_argv,
            detail=(
                f"observed={module_stdout.strip()!r} expected={expected_version!r}"
            ),
        )
    for name in cli_names:
        argv = [str(bin_dir / name), "--help"]
        stdout = run_command(
            argv,
            phase="core-surfaces",
            cwd=work_dir,
            timeout=TIMEOUT_CLI,
            profile=profile,
            containment_dir=work_dir,
        )
        if not stdout.strip():
            raise SmokeGateError(
                "core-surfaces",
                f"installed CLI produced empty --help output: {name}",
                profile=profile,
                command=argv,
            )
    list_fields = json.loads(
        run_command(
            [str(bin_dir / expectations.cli.primary), "--list-fields", "--json"],
            phase="schema",
            cwd=work_dir,
            timeout=TIMEOUT_CLI,
            profile=profile,
            containment_dir=work_dir,
        )
    )
    validate_list_fields_document(list_fields)
    describe = json.loads(
        run_command(
            [
                str(bin_dir / expectations.cli.primary),
                "--describe-field",
                SCHEMA_FIELD,
                "--json",
            ],
            phase="schema",
            cwd=work_dir,
            timeout=TIMEOUT_CLI,
            profile=profile,
            containment_dir=work_dir,
        )
    )
    validate_describe_field_document(describe)
    _LOGGER.info("[schema] %s: list-fields and %s bounds OK", profile, SCHEMA_FIELD)


def _run_library_surfaces(
    venv_python: Path,
    work_dir: Path,
    *,
    profile: str,
) -> None:
    """Prove library import surface, protobuf roundtrip, and facade."""
    probe_path = work_dir / f"probe-library-{profile}.py"
    _write_probe(probe_path, _LIBRARY_PROBE)
    observed = run_probe_json(venv_python, probe_path, work_dir, profile=profile)
    failed = sorted(
        key
        for key, value in observed.items()
        if key != "wire_len" and value is not True
    )
    if failed:
        raise SmokeGateError(
            "library",
            "installed library/protobuf surface checks failed",
            profile=profile,
            detail=f"failed assertions: {failed}; observed: "
            f"{json.dumps(observed, sort_keys=True)}",
        )
    if int(observed.get("wire_len", 0)) <= 0:
        raise SmokeGateError(
            "library",
            "protobuf roundtrip produced empty wire data",
            profile=profile,
        )
    _LOGGER.info(
        "[library] %s: imports, MeshPacket/ToRadio roundtrip (%s bytes), "
        "canonical namespace, and historical facade OK",
        profile,
        observed.get("wire_len"),
    )


def _run_core_light_assertion(
    venv_python: Path,
    work_dir: Path,
    *,
    profile: str,
) -> None:
    """Assert the core install is lightweight (analysis deps absent)."""
    probe_path = work_dir / f"probe-core-light-{profile}.py"
    _write_probe(probe_path, _CORE_LIGHT_PROBE)
    observed = run_probe_json(venv_python, probe_path, work_dir, profile=profile)
    unexpected = observed.get("unexpectedly_importable")
    if unexpected:
        raise SmokeGateError(
            "cells",
            "core install must not carry analysis-only dependencies",
            profile=profile,
            detail=f"unexpectedly importable: {unexpected}",
        )
    _LOGGER.info(
        "[cells] %s: core install stays lightweight (no pyarrow/dash)", profile
    )


def _run_analysis_surfaces(
    venv_dir: Path,
    venv_python: Path,
    work_dir: Path,
    repo_root: Path,
    expectations: RepoExpectations,
    *,
    profile: str,
) -> None:
    """Prove real analysis CLI and Feather reading in an analysis cell."""
    bin_dir = venv_dir / "bin"
    analysis_bin = bin_dir / expectations.cli.analysis
    if not analysis_bin.is_file():
        raise SmokeGateError(
            "cells",
            f"installed console script is missing: {expectations.cli.analysis}",
            profile=profile,
        )
    for argv in (
        [str(analysis_bin), "--help"],
        [str(venv_python), "-m", "meshtastic.analysis", "--help"],
    ):
        stdout = run_command(
            argv,
            phase="analysis-surfaces",
            cwd=work_dir,
            timeout=TIMEOUT_CLI,
            profile=profile,
            containment_dir=work_dir,
        )
        if not stdout.strip():
            raise SmokeGateError(
                "analysis-surfaces",
                "installed analysis CLI produced empty --help output",
                profile=profile,
                command=argv,
            )
    probe_path = work_dir / f"probe-analysis-read-{profile}.py"
    _write_probe(probe_path, _ANALYSIS_READ_PROBE)
    feather_path = work_dir / f"smoke-{profile}.feather"
    observed = run_probe_json(
        venv_python,
        probe_path,
        work_dir,
        profile=profile,
        args=[str(feather_path)],
    )
    if observed.get("dtypes") != observed.get("expected_dtypes"):
        raise SmokeGateError(
            "analysis-surfaces",
            "installed read_pandas did not map Arrow columns to the expected "
            "nullable dtypes",
            profile=profile,
            detail=json.dumps(observed, sort_keys=True),
        )
    if observed.get("values_ok") is not True:
        raise SmokeGateError(
            "analysis-surfaces",
            "installed read_pandas did not preserve exact Feather values "
            "including pd.NA cells",
            profile=profile,
            detail=json.dumps(observed, sort_keys=True)[:ERROR_OUTPUT_TAIL],
        )
    _LOGGER.info(
        "[analysis] %s: read_pandas dtypes=%s values_ok=%s (nullable Int64/"
        "boolean/string[pyarrow]/Float64 with pd.NA preserved)",
        profile,
        json.dumps(observed.get("dtypes"), sort_keys=True),
        observed.get("values_ok"),
    )
    slog_source = repo_root / "meshtastic" / "tests" / "slog-test-input"
    if not slog_source.is_dir():
        raise SmokeGateError(
            "analysis-surfaces",
            f"slog fixture directory is missing in the repository: {slog_source}",
        )
    slog_copy = work_dir / "slog-input"
    shutil.copytree(slog_source, slog_copy)
    # Real production data path: the installed create_dash() must consume the
    # copied slog fixture and build a populated Dash app from its Feather
    # frames (this is the code path the analysis server actually runs).
    dash_probe_path = work_dir / f"probe-create-dash-{profile}.py"
    _write_probe(dash_probe_path, _ANALYSIS_CREATE_DASH_PROBE)
    dash_observed = run_probe_json(
        venv_python,
        dash_probe_path,
        work_dir,
        profile=profile,
        args=[str(slog_copy)],
    )
    if (
        dash_observed.get("is_dash_app") is not True
        or dash_observed.get("layout_set") is not True
    ):
        raise SmokeGateError(
            "analysis-surfaces",
            "installed create_dash did not return a real Dash app for the "
            "slog fixture",
            profile=profile,
            detail=json.dumps(dash_observed, sort_keys=True),
        )
    graph_count = int(dash_observed.get("graph_count", 0))
    plotted_points = int(dash_observed.get("plotted_points", 0))
    if graph_count < 1 or plotted_points <= 0:
        raise SmokeGateError(
            "analysis-surfaces",
            "the Dash app built from the slog fixture carries no plotted data",
            profile=profile,
            detail=json.dumps(dash_observed, sort_keys=True),
        )
    _LOGGER.info(
        "[analysis] %s: create_dash(slog fixture) returned a Dash app "
        "(graphs=%s traces=%s plotted_points=%s)",
        profile,
        graph_count,
        dash_observed.get("trace_count"),
        plotted_points,
    )
    # Entrypoint/arg-parse check only: --no-server exits in main() before any
    # Feather read, so this proves CLI wiring, not the data path (the
    # create_dash probe above covers the data path).
    run_command(
        [str(analysis_bin), "--no-server", "--slog", str(slog_copy)],
        phase="analysis-surfaces",
        cwd=work_dir,
        timeout=TIMEOUT_CLI,
        profile=profile,
        containment_dir=work_dir,
    )
    _LOGGER.info(
        "[analysis] %s: --help, python -m, read_pandas Feather dtypes/values, "
        "create_dash on the slog fixture, and the --no-server --slog "
        "entrypoint check OK",
        profile,
    )


def run_cell(
    plan: CellPlan,
    work_dir: Path,
    repo_root: Path,
    expectations: RepoExpectations,
) -> None:
    """Run one isolated consumer cell end to end."""
    cell_dir = work_dir / f"cell-{plan.profile}"
    venv_dir = cell_dir / "venv"
    cell_dir.mkdir(parents=True, exist_ok=True)
    pip_cache_dir = work_dir / "pip-cache"
    venv_python, site_packages = create_consumer_venv(
        venv_dir, cell_dir, profile=plan.profile
    )
    install_requirement(
        venv_python,
        plan.requirement,
        cell_dir,
        profile=plan.profile,
        pip_cache_dir=pip_cache_dir,
    )
    if plan.is_core:
        compat = plan.compat_requirement
        if compat is None:
            raise SmokeGateError(
                "cells",
                "core cell is missing its compatibility step",
                profile=plan.profile,
            )
        install_requirement(
            venv_python,
            compat,
            cell_dir,
            profile=plan.profile,
            pip_cache_dir=pip_cache_dir,
        )
    identity = run_probe_json(
        venv_python,
        _write_identity_probe(cell_dir, plan.profile),
        cell_dir,
        profile=plan.profile,
        args=[expectations.name],
    )
    _assert_install_identity(identity, venv_dir, expectations, profile=plan.profile)
    _LOGGER.info(
        "[cells] %s: installed %s %s; meshtastic origin %s; protobuf origin %s; "
        "py.typed via importlib.resources: %s",
        plan.profile,
        identity.get("name"),
        identity.get("version"),
        identity.get("meshtastic_file"),
        identity.get("protobuf_file"),
        identity.get("py_typed_via_resources"),
    )
    _assert_no_editable_install(site_packages, repo_root, expectations)
    if plan.extras == ("cli",):
        _run_core_cli_surfaces(
            venv_dir, venv_python, cell_dir, expectations, profile=plan.profile
        )
        _run_library_surfaces(venv_python, cell_dir, profile=plan.profile)
        _run_core_light_assertion(venv_python, cell_dir, profile=plan.profile)
    else:
        _run_analysis_surfaces(
            venv_dir,
            venv_python,
            cell_dir,
            repo_root,
            expectations,
            profile=plan.profile,
        )
    _LOGGER.info("[cells] profile OK: %s", plan.profile)


def _write_identity_probe(cell_dir: Path, profile: str) -> Path:
    """Materialize the install-identity probe for one cell."""
    probe_path = cell_dir / f"probe-identity-{profile}.py"
    _write_probe(probe_path, _INSTALL_IDENTITY_PROBE)
    return probe_path


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse gate CLI arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Build sdist+wheel from this checkout and prove them as installed "
            "distributions in isolated consumer venvs (fail-closed)."
        )
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository checkout to build from (default: this script's repo).",
    )
    parser.add_argument(
        "--dist-dir",
        type=Path,
        default=None,
        help=(
            "Directory holding (or receiving) the artifacts to validate. "
            "Default: a fresh owned temporary directory."
        ),
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Validate pre-existing artifacts in --dist-dir instead of building.",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help=(
            "Parent directory for owned temporary scratch state (venvs, "
            "probes). Default: the system temporary directory."
        ),
    )
    return parser.parse_args(argv)


def run_gate(args: argparse.Namespace) -> int:
    """Execute the full gate; return the process exit code."""
    repo_root = args.repo_root.resolve()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    work_dir_parent = args.work_dir.resolve() if args.work_dir else None
    workspace = Path(tempfile.mkdtemp(prefix="artifact-smoke-", dir=work_dir_parent))
    dist_dir = args.dist_dir.resolve() if args.dist_dir else workspace / "dist"
    try:
        expectations = load_repo_expectations(repo_root)
        pb2_stems = expected_pb2_modules(repo_root)
        artifacts = locate_artifacts(dist_dir, expectations, skip_build=args.skip_build)
        if not args.skip_build:
            build_artifacts(repo_root, artifacts.dist_dir)
            artifacts = locate_artifacts(
                artifacts.dist_dir, expectations, skip_build=True
            )
        for line in artifacts.summary():
            _LOGGER.info("[artifacts] %s", line)
        validate_wheel_contents(artifacts.wheel, expectations, pb2_stems)
        validate_sdist_contents(artifacts.sdist, expectations, pb2_stems)
        cells = (
            CellPlan("wheel-core", artifacts.wheel, ("cli",)),
            CellPlan("sdist-core", artifacts.sdist, ("cli",)),
            CellPlan("wheel-analysis", artifacts.wheel, ("analysis",)),
            CellPlan("sdist-analysis", artifacts.sdist, ("analysis",)),
        )
        for plan in cells:
            run_cell(plan, workspace, repo_root, expectations)
        _LOGGER.info(
            "[gate] all phases passed: %s", ", ".join(p.profile for p in cells)
        )
        return 0
    except SmokeGateError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the artifact smoke gate."""
    return run_gate(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
