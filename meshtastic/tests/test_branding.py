"""Contracts for fork-adjustable product and CLI branding."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from meshtastic import _branding as branding

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.unit
def test_product_name_is_single_runtime_branding_switch() -> None:
    """Distribution, primary CLI, and display branding should share one source."""
    assert branding.DISTRIBUTION_NAME == branding.PRODUCT_NAME
    assert branding.PRIMARY_CLI_NAME == branding.PRODUCT_NAME
    expected_display_name = (
        branding.UPSTREAM_DISPLAY_NAME
        if branding.PRODUCT_NAME == branding.UPSTREAM_PRODUCT_NAME
        else f"{branding.UPSTREAM_DISPLAY_NAME} ({branding.PRODUCT_NAME} fork)"
    )
    assert branding.PROJECT_DISPLAY_NAME == expected_display_name
    expected_repository = (
        branding.UPSTREAM_REPOSITORY_URL
        if branding.PRODUCT_NAME == branding.UPSTREAM_PRODUCT_NAME
        else branding.FORK_REPOSITORY_URL
    )
    assert branding.PROJECT_REPOSITORY_URL == expected_repository
    assert branding.PROJECT_ISSUE_URL == f"{expected_repository}/issues"

    if branding.PRODUCT_NAME == branding.UPSTREAM_PRODUCT_NAME:
        assert branding.COMPATIBILITY_CLI_NAMES == ()
    else:
        assert branding.COMPATIBILITY_CLI_NAMES == (branding.UPSTREAM_PRODUCT_NAME,)


@pytest.mark.unit
def test_format_cli_version_includes_product_identity() -> None:
    """Version output should identify the distribution, not print a bare version."""
    assert branding._format_cli_version("2.7.11.post5") == (
        f"{branding.PRIMARY_CLI_NAME} 2.7.11.post5"
    )


@pytest.mark.unit
def test_packaging_metadata_tracks_branding_contract() -> None:
    """Static install-time metadata must stay synchronized with runtime branding."""
    pyproject = tomllib.loads(
        (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    project = pyproject["project"]
    scripts = project["scripts"]

    assert project["name"] == branding.DISTRIBUTION_NAME
    expected_cli_names = {
        branding.PRIMARY_CLI_NAME,
        *branding.COMPATIBILITY_CLI_NAMES,
    }
    actual_cli_names = {
        name for name, target in scripts.items() if target == "meshtastic.__main__:main"
    }
    assert actual_cli_names == expected_cli_names
