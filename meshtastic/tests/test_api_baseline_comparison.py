"""API baseline comparison tests for the Meshtastic Python library.

These tests generate and compare the current API surface against frozen baselines
captured from master/develop. This ensures that public API changes are intentional
and tracked, preventing accidental breaking changes.

To update baselines after intentional API changes:
    poetry run pytest meshtastic/tests/test_api_baseline_comparison.py -v --update-baselines

The baselines are stored in:
    meshtastic/tests/api_baselines/api_baseline.json
"""

# pylint: disable=redefined-outer-name
# Fixture names (current_baseline, stored_baseline) appear to shadow when used as
# test parameters - this is standard pytest fixture injection pattern.

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

pytestmark = pytest.mark.unit

# =============================================================================
# Configuration
# =============================================================================

# Path to the baseline file
BASELINE_DIR = Path(__file__).parent / "api_baselines"
BASELINE_FILE = BASELINE_DIR / "api_baseline.json"

# Allow override via environment variable for CI comparisons
BASELINE_FILE_MASTER = BASELINE_DIR / "api_baseline_master.json"


def get_baseline_file() -> Path:
    """Get the baseline file path.

    By default prefer api_baseline.json for local/update workflows.
    Set MESHTASTIC_API_BASELINE_SOURCE=master to opt into api_baseline_master.json.
    """
    if (
        os.environ.get("MESHTASTIC_API_BASELINE_SOURCE", "").lower() == "master"
        and BASELINE_FILE_MASTER.exists()
    ):
        return BASELINE_FILE_MASTER
    if BASELINE_FILE.exists():
        return BASELINE_FILE
    if BASELINE_FILE_MASTER.exists():
        return BASELINE_FILE_MASTER
    return BASELINE_FILE


# =============================================================================
# Baseline Generation Utilities
# =============================================================================


_EXTRACTED_SURFACE_CACHE: dict[str, Any] | None = None
_IS_BREAKING_SIGNATURE_CHANGE_FN: Callable[[str, str, str, str], bool] | None = None
_COMPARE_EXPORTS_FN = Callable[[list[str], list[str]], tuple[list[str], list[str]]]


def _is_breaking_signature_change_for_baseline(
    stored_signature: str,
    current_signature: str,
    class_name: str,
    method_name: str,
) -> bool:
    """Load compare_api_surfaces helper and classify signature deltas."""
    global _IS_BREAKING_SIGNATURE_CHANGE_FN  # pylint: disable=global-statement
    if _IS_BREAKING_SIGNATURE_CHANGE_FN is None:
        project_root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(project_root / "bin"))
        try:
            from compare_api_surfaces import (  # type: ignore[import-not-found]  # pylint: disable=import-error,import-outside-toplevel
                _is_breaking_signature_change,
            )
        finally:
            sys.path.pop(0)
        _IS_BREAKING_SIGNATURE_CHANGE_FN = cast(
            Callable[[str, str, str, str], bool],
            _is_breaking_signature_change,
        )
    return bool(
        _IS_BREAKING_SIGNATURE_CHANGE_FN(
            stored_signature,
            current_signature,
            class_name,
            method_name,
        )
    )


def _extract_api_surface() -> dict[str, Any]:
    """Return API surface from the canonical extractor script."""
    global _EXTRACTED_SURFACE_CACHE  # pylint: disable=global-statement

    if _EXTRACTED_SURFACE_CACHE is not None:
        return cast(dict[str, Any], _EXTRACTED_SURFACE_CACHE)

    project_root = Path(__file__).resolve().parents[2]
    script_path = project_root / "bin" / "extract_api_surface.py"
    package_dir = project_root / "meshtastic"
    completed = subprocess.run(
        [sys.executable, str(script_path), str(package_dir)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    _EXTRACTED_SURFACE_CACHE = json.loads(completed.stdout)
    return cast(dict[str, Any], _EXTRACTED_SURFACE_CACHE)


def capture_node_methods() -> dict[str, str]:
    """Capture public method signatures from Node class."""
    return dict(sorted(_extract_api_surface().get("node_methods", {}).items()))


def capture_mesh_interface_methods() -> dict[str, str]:
    """Capture public method signatures from MeshInterface class."""
    return dict(
        sorted(_extract_api_surface().get("mesh_interface_methods", {}).items())
    )


def capture_top_level_exports() -> list[str]:
    """Capture top-level exports from main meshtastic package."""
    return sorted(_extract_api_surface().get("top_level_exports", []))


def capture_legacy_import_paths() -> list[str]:
    """Capture legacy import compatibility paths from extractor output."""
    return sorted(_extract_api_surface().get("legacy_import_paths", []))


def generate_baseline() -> dict[str, Any]:
    """Generate a complete API baseline from current code.

    Returns a dictionary containing all captured API surfaces.
    """
    return {
        "node_methods": capture_node_methods(),
        "mesh_interface_methods": capture_mesh_interface_methods(),
        "top_level_exports": capture_top_level_exports(),
        "legacy_import_paths": capture_legacy_import_paths(),
    }


# =============================================================================
# Baseline Persistence
# =============================================================================


def load_baseline() -> dict[str, Any] | None:
    """Load the stored baseline from disk.

    Returns None if no baseline exists.
    Checks for master baseline first (used in CI comparisons),
    falls back to standard baseline file.
    """
    baseline_file = get_baseline_file()
    if not baseline_file.exists():
        return None

    with open(baseline_file, encoding="utf-8") as f:
        return json.load(f)


def save_baseline(baseline_data: dict[str, Any]) -> None:
    """Save a baseline to disk."""
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    target = get_baseline_file()
    with open(target, "w", encoding="utf-8") as f:
        json.dump(baseline_data, f, indent=2, sort_keys=True)


# =============================================================================
# Comparison Logic
# =============================================================================


def compare_method_baselines(
    current: dict[str, str],
    stored: dict[str, str],
    _class_name: str,
) -> list[str]:
    """Compare current methods against baseline and report differences.

    Returns a list of human-readable difference descriptions.
    """
    differences = []

    current_keys = set(current.keys())
    stored_keys = set(stored.keys())

    # Find added methods
    added = current_keys - stored_keys
    if added:
        differences.append(f"ADDED methods: {sorted(added)}")

    # Find removed methods
    removed = stored_keys - current_keys
    if removed:
        differences.append(f"REMOVED methods: {sorted(removed)}")

    # Find changed signatures
    common = current_keys & stored_keys
    changed = []
    for key in sorted(common):
        if current[key] != stored[key]:
            if not _is_breaking_signature_change_for_baseline(
                stored[key], current[key], _class_name, key
            ):
                continue
            changed.append(f"  {key}:")
            changed.append(f"    stored: {stored[key]}")
            changed.append(f"    current:  {current[key]}")

    if changed:
        differences.append("CHANGED signatures:")
        differences.extend(changed)

    return differences


def compare_list_baselines(
    current: list[str],
    stored_list: list[str],
    name: str,
) -> list[str]:
    """Compare two lists and report differences."""
    differences = []

    current_set = set(current)
    stored_set = set(stored_list)

    added = current_set - stored_set
    if added:
        differences.append(f"ADDED {name}: {sorted(added)}")

    removed = stored_set - current_set
    if removed:
        differences.append(f"REMOVED {name}: {sorted(removed)}")

    return differences


# =============================================================================
# Pytest Fixtures and Hooks
# =============================================================================


@pytest.fixture
def current_baseline() -> dict[str, Any]:
    """Fixture providing the current API baseline."""
    return generate_baseline()


@pytest.fixture
def stored_baseline() -> dict[str, Any] | None:
    """Fixture providing the stored baseline, or None if not exists."""
    return load_baseline()


def _should_update_baselines(pytestconfig: pytest.Config) -> bool:
    """Safely check if --update-baselines flag is set."""
    try:
        return pytestconfig.getoption("--update-baselines", default=False)
    except (ValueError, AttributeError):
        return False


# =============================================================================
# Test Cases
# =============================================================================


class TestNodeAPIAgainstBaseline:
    """Tests to verify Node public API matches baseline."""

    def test_node_api_against_baseline(
        self, current_baseline, stored_baseline, pytestconfig
    ):
        """Verify Node public API methods match the stored baseline.

        This test fails if:
        - Methods are added (new API surface)
        - Methods are removed (breaking change)
        - Method signatures change (breaking change)

        Use --update-baselines to accept intentional changes.
        """
        # Generate current baseline if none exists
        if stored_baseline is None:
            if _should_update_baselines(pytestconfig):
                save_baseline(current_baseline)
                pytest.skip("Created initial baseline - re-run tests")
            else:
                pytest.fail(
                    "No baseline exists. Run with --update-baselines to create one."
                )

        # Compare current against stored
        differences = compare_method_baselines(
            current_baseline["node_methods"],
            stored_baseline.get("node_methods", {}),
            "Node",
        )

        if _should_update_baselines(pytestconfig):
            # Update baseline with current state
            stored_baseline["node_methods"] = current_baseline["node_methods"]
            save_baseline(stored_baseline)
            pytest.skip("Updated baseline with current Node API")

        if differences:
            msg = "Node API differs from baseline:\n" + "\n".join(
                f"  {d}" for d in differences
            )
            msg += "\n\nTo accept these changes, run: poetry run pytest meshtastic/tests/test_api_baseline_comparison.py -v --update-baselines"
            pytest.fail(msg)

    def test_node_critical_methods_present(self, current_baseline):
        """Verify critical Node methods are always present regardless of baseline.

        This is a safety net to ensure core functionality isn't broken.
        """
        critical_methods = {
            "setURL",
            "writeChannel",
            "writeConfig",
            "setOwner",
            "getChannelByChannelIndex",
            "deleteChannel",
            "requestConfig",
            "reboot",
            "shutdown",
            "factoryReset",
            "ensureSessionKey",
        }

        current_methods = set(current_baseline["node_methods"].keys())
        missing = critical_methods - current_methods

        if missing:
            pytest.fail(f"Critical Node methods missing: {sorted(missing)}")


class TestMeshInterfaceAPIAgainstBaseline:
    """Tests to verify MeshInterface public API matches baseline."""

    def test_mesh_interface_api_against_baseline(
        self, current_baseline, stored_baseline, pytestconfig
    ):
        """Verify MeshInterface public API methods match the stored baseline."""
        if stored_baseline is None:
            if _should_update_baselines(pytestconfig):
                save_baseline(current_baseline)
                pytest.skip("Created initial baseline - re-run tests")
            else:
                pytest.fail(
                    "No baseline exists. Run with --update-baselines to create one."
                )

        differences = compare_method_baselines(
            current_baseline["mesh_interface_methods"],
            stored_baseline.get("mesh_interface_methods", {}),
            "MeshInterface",
        )

        if _should_update_baselines(pytestconfig):
            stored_baseline["mesh_interface_methods"] = current_baseline[
                "mesh_interface_methods"
            ]
            save_baseline(stored_baseline)
            pytest.skip("Updated baseline with current MeshInterface API")

        if differences:
            msg = "MeshInterface API differs from baseline:\n" + "\n".join(
                f"  {d}" for d in differences
            )
            msg += "\n\nTo accept these changes, run: poetry run pytest meshtastic/tests/test_api_baseline_comparison.py -v --update-baselines"
            pytest.fail(msg)

    def test_mesh_interface_critical_methods_present(self, current_baseline):
        """Verify critical MeshInterface methods are always present."""
        critical_methods = {
            "sendText",
            "sendData",
            "sendPosition",
            "sendTelemetry",
            "sendTraceRoute",
            "requestTraceRoute",
            "sendWaypoint",
            "getNode",
            "showNodes",
            "showInfo",
            "close",
        }

        current_methods = set(current_baseline["mesh_interface_methods"].keys())
        missing = critical_methods - current_methods

        if missing:
            pytest.fail(f"Critical MeshInterface methods missing: {sorted(missing)}")


class TestTopLevelExportsAgainstBaseline:
    """Tests to verify main package exports match baseline."""

    def test_top_level_exports_against_baseline(
        self, current_baseline, stored_baseline, pytestconfig
    ):
        """Verify top-level exports from meshtastic package match baseline."""
        if stored_baseline is None:
            if _should_update_baselines(pytestconfig):
                save_baseline(current_baseline)
                pytest.skip("Created initial baseline - re-run tests")
            else:
                pytest.fail(
                    "No baseline exists. Run with --update-baselines to create one."
                )

        differences = compare_list_baselines(
            current_baseline["top_level_exports"],
            stored_baseline.get("top_level_exports", []),
            "exports",
        )

        if _should_update_baselines(pytestconfig):
            stored_baseline["top_level_exports"] = current_baseline["top_level_exports"]
            save_baseline(stored_baseline)
            pytest.skip("Updated baseline with current top-level exports")

        if differences:
            msg = "Top-level exports differ from baseline:\n" + "\n".join(
                f"  {d}" for d in differences
            )
            msg += "\n\nTo accept these changes, run: poetry run pytest meshtastic/tests/test_api_baseline_comparison.py -v --update-baselines"
            pytest.fail(msg)

    def test_essential_exports_present(self, current_baseline):
        """Verify essential exports are always present."""
        essential_exports = {
            "Node",
            "BROADCAST_ADDR",
            "BROADCAST_NUM",
            "LOCAL_ADDR",
        }

        current_exports = set(current_baseline["top_level_exports"])
        missing = essential_exports - current_exports

        if missing:
            pytest.fail(f"Essential exports missing: {sorted(missing)}")


class TestLegacyImportPathsAgainstBaseline:
    """Tests to verify documented runtime compatibility import paths."""

    def test_legacy_import_paths_against_baseline(
        self, current_baseline, stored_baseline, pytestconfig
    ):
        """Verify manifest-backed runtime compatibility import paths remain stable.

        Internal runtime modules that are absent from the manifest are free to move
        without creating an API-baseline failure.
        """
        if stored_baseline is None:
            if _should_update_baselines(pytestconfig):
                save_baseline(current_baseline)
                pytest.skip("Created initial baseline - re-run tests")
            else:
                pytest.fail(
                    "No baseline exists. Run with --update-baselines to create one."
                )

        differences = compare_list_baselines(
            current_baseline["legacy_import_paths"],
            stored_baseline.get("legacy_import_paths", []),
            "import paths",
        )

        if _should_update_baselines(pytestconfig):
            stored_baseline["legacy_import_paths"] = current_baseline[
                "legacy_import_paths"
            ]
            save_baseline(stored_baseline)
            pytest.skip("Updated baseline with current import paths")

        if differences:
            msg = "Legacy import paths differ from baseline:\n" + "\n".join(
                f"  {d}" for d in differences
            )
            msg += "\n\nTo accept these changes, run: poetry run pytest meshtastic/tests/test_api_baseline_comparison.py -v --update-baselines"
            pytest.fail(msg)


class TestSignatureAliasComparison:
    """Tests for signature comparison with approved aliases."""

    def test_startota_alias_not_breaking(self):
        """Verify startOTA canonical signature remains non-breaking vs master."""
        base_sig = "(self, ota_mode:admin_pb2.OTAMode.ValueType, ota_file_hash:bytes)"
        pr_sig = (
            "(self, mode:admin_pb2.OTAMode.ValueType | None=None, "
            "ota_file_hash:bytes | None=None, *, ota_mode:admin_pb2.OTAMode.ValueType | None=None, "
            "ota_hash:bytes | None=None, **kwargs)"
        )

        assert not _is_breaking_signature_change_for_baseline(
            base_sig, pr_sig, "Node", "startOTA"
        )

    def test_startota_drift_from_develop_is_breaking(self):
        """Verify switching positional order away from develop is breaking."""
        base_sig = (
            "(self, mode:admin_pb2.OTAMode.ValueType | None=None, "
            "ota_file_hash:bytes | None=None, *, ota_mode:admin_pb2.OTAMode.ValueType | None=None, "
            "ota_hash:bytes | None=None, **kwargs)"
        )
        pr_sig = (
            "(self, ota_mode:admin_pb2.OTAMode.ValueType | None=None, "
            "ota_file_hash:bytes | None=None, *, mode:admin_pb2.OTAMode.ValueType | None=None, "
            "ota_hash:bytes | None=None, **kwargs)"
        )

        assert _is_breaking_signature_change_for_baseline(
            base_sig, pr_sig, "Node", "startOTA"
        )

    def test_startota_missing_param_is_breaking(self):
        """Verify removing a required param is still breaking."""
        base_sig = "(self, ota_mode:admin_pb2.OTAMode.ValueType, ota_file_hash:bytes)"
        pr_sig = "(self, ota_mode:admin_pb2.OTAMode.ValueType | None=None)"

        assert _is_breaking_signature_change_for_baseline(
            base_sig, pr_sig, "Node", "startOTA"
        )

    def test_startota_new_required_kwonly_is_breaking(self):
        """Verify adding a new required keyword-only param is breaking."""
        base_sig = "(self, ota_mode:admin_pb2.OTAMode.ValueType, ota_file_hash:bytes)"
        pr_sig = "(self, ota_mode:admin_pb2.OTAMode.ValueType, ota_file_hash:bytes, *, new_required:int)"

        assert _is_breaking_signature_change_for_baseline(
            base_sig, pr_sig, "Node", "startOTA"
        )


class TestCompatibilityGatePolicy:
    """Guard the export policy that prevents established root exports from vanishing silently."""

    @staticmethod
    def _compare_exports() -> "_COMPARE_EXPORTS_FN":
        """Load the canonical comparator export policy from bin."""
        project_root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(project_root / "bin"))
        try:
            from compare_api_surfaces import (  # type: ignore[import-not-found]  # pylint: disable=import-error,import-outside-toplevel
                compare_exports,
            )
        finally:
            sys.path.pop(0)
        return compare_exports

    def test_removing_established_root_export_is_breaking(self) -> None:
        """Established root exports (e.g. fixme) must fail the gate when dropped."""
        compare_exports = self._compare_exports()

        base = ["Node", "fixme", "stripnl"]
        current = ["Node", "stripnl"]

        blocking, informational = compare_exports(base, current)
        assert "REMOVED exports: ['fixme']" in blocking
        assert not any("fixme" in line for line in informational)

    def test_removing_documented_lazy_alias_is_breaking(self) -> None:
        """A documented lazy alias (meshtastic.serial) must not be noise-filtered."""
        compare_exports = self._compare_exports()

        base = ["Node", "serial"]
        current = ["Node"]

        blocking, _ = compare_exports(base, current)
        assert "REMOVED exports: ['serial']" in blocking

    def test_dependency_leak_removals_remain_informational(self) -> None:
        """Stdlib/third-party namespace leaks stay non-breaking when dropped."""
        compare_exports = self._compare_exports()

        base = ["Node", "fixme", "os", "sys", "tabulate", "datetime"]
        current = ["Node", "fixme"]

        blocking, informational = compare_exports(base, current)
        assert blocking == []
        assert any(
            "REMOVED (noise/implementation detail)" in line for line in informational
        )
        assert not any("fixme" in line for line in informational)

    def test_extractor_captures_lazy_getattr_alias(self, tmp_path: Any) -> None:
        """The extractor must see __getattr__ lazy aliases without importing."""
        project_root = Path(__file__).resolve().parents[2]
        script_path = project_root / "bin" / "extract_api_surface.py"
        pkg_dir = tmp_path / "lazy_pkg"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text(
            "VALUE = 1\n"
            "\n"
            "\n"
            "def __getattr__(name):\n"
            '    if name == "serial":\n'
            "        import serial as serial_module\n"
            "        return serial_module\n"
            "    raise AttributeError(name)\n",
            encoding="utf-8",
        )

        completed = subprocess.run(
            [sys.executable, str(script_path), str(pkg_dir)],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        surface = json.loads(completed.stdout)

        assert "VALUE" in surface["top_level_exports"]
        assert "serial" in surface["top_level_exports"]

    def test_extractor_ignores_rejected_or_fallthrough_getattr_names(
        self, tmp_path: Any
    ) -> None:
        """Only lazy branches that can return values are exports."""
        project_root = Path(__file__).resolve().parents[2]
        script_path = project_root / "bin" / "extract_api_surface.py"
        pkg_dir = tmp_path / "lazy_pkg"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text(
            "def __getattr__(attribute):\n"
            '    if attribute == "serial":\n'
            "        raise AttributeError(attribute)\n"
            '    if attribute == "fallthrough":\n'
            "        pass\n"
            '    if attribute == "unreachable":\n'
            "        raise AttributeError(attribute)\n"
            "        return object()\n"
            '    if "served" == attribute:\n'
            "        return object()\n"
            "    def nested_helper():\n"
            '        if attribute == "nested_only":\n'
            "            return object()\n"
            "    raise AttributeError(attribute)\n",
            encoding="utf-8",
        )

        completed = subprocess.run(
            [sys.executable, str(script_path), str(pkg_dir)],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        surface = json.loads(completed.stdout)

        assert "served" in surface["top_level_exports"]
        assert "serial" not in surface["top_level_exports"]
        assert "fallthrough" not in surface["top_level_exports"]
        assert "unreachable" not in surface["top_level_exports"]
        assert "nested_only" not in surface["top_level_exports"]


class TestUpstreamBaselineRefreshWorkflow:
    """Guard recurring upstream-baseline PR lifecycle semantics."""

    def test_refresh_reuses_only_open_pr_and_updates_metadata(self) -> None:
        """A prior closed PR must not suppress the next scheduled refresh PR."""
        project_root = Path(__file__).resolve().parents[2]
        workflow = (
            project_root / ".github" / "workflows" / "sync-upstream-master.yml"
        ).read_text(encoding="utf-8")

        assert "gh pr list" in workflow
        assert "--state open" in workflow
        assert "gh api --method PATCH" in workflow
        assert "pulls/${open_pr_number}" in workflow
        assert '-f title="${pr_title}"' in workflow
        assert '-f body="${pr_body}"' in workflow


class TestBaselineGeneration:
    """Tests for baseline generation utilities."""

    def test_baseline_generation_succeeds(self):
        """Verify baseline generation works without errors."""
        api_baseline = generate_baseline()

        assert "node_methods" in api_baseline
        assert "mesh_interface_methods" in api_baseline
        assert "top_level_exports" in api_baseline
        assert "legacy_import_paths" in api_baseline

        # Verify we got actual data
        assert len(api_baseline["node_methods"]) > 0
        assert len(api_baseline["mesh_interface_methods"]) > 0
        assert len(api_baseline["top_level_exports"]) > 0

    def test_signature_capture_includes_parameters(self):
        """Verify signature capture includes parameter information."""
        methods = capture_node_methods()

        # Check a known method has signature info
        assert "setOwner" in methods
        sig = methods["setOwner"]
        # Should include parameter names
        assert "self" in sig
        assert "long_name" in sig

    def test_baseline_json_serializable(self):
        """Verify generated baseline can be serialized to JSON."""
        api_baseline = generate_baseline()

        # Should not raise
        json_str = json.dumps(api_baseline, indent=2, sort_keys=True)

        # Should be parseable
        parsed = json.loads(json_str)
        assert parsed == api_baseline


# =============================================================================
# Main Entry Point for Manual Baseline Generation
# =============================================================================

if __name__ == "__main__":
    # Allow running this file directly to generate baselines
    print("Generating API baseline...")
    baseline = generate_baseline()

    target = get_baseline_file()
    save_baseline(baseline)

    print(f"Baseline saved to: {target}")
    print(f"Node methods: {len(baseline['node_methods'])}")
    print(f"MeshInterface methods: {len(baseline['mesh_interface_methods'])}")
    print(f"Top-level exports: {len(baseline['top_level_exports'])}")
    print(f"Legacy import paths: {len(baseline['legacy_import_paths'])}")
    print(
        "\nRun tests with: poetry run pytest meshtastic/tests/test_api_baseline_comparison.py -v"
    )
