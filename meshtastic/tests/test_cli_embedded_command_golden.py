"""Golden pins for the embedded command surface.

The embedded-command contract is versioned (``docs/guides/embedded-commands.md``):
the accepted option spellings, the ``--help`` rendering of that same surface, and
the refusal of unsupported options must not drift. The fixtures in
``cli_embedded_command_golden.json`` were captured from the implemented parser;
these tests fail if capability discovery, help output, or unsupported-option
handling changes without a deliberate contract update.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from meshtastic.commands import CommandResult, executeCommand, getCommandCapabilities
from meshtastic.mesh_interface import MeshInterface

pytestmark = pytest.mark.unit

_GOLDEN_FILE = Path(__file__).parent / "cli_embedded_command_golden.json"


def _golden() -> dict[str, Any]:
    """Load the captured embedded-command fixtures."""
    with _GOLDEN_FILE.open(encoding="utf-8") as handle:
        return cast("dict[str, Any]", json.load(handle))


@pytest.fixture
def offline_interface() -> MeshInterface:
    """Provide a protocol-free interface; surface commands never send packets."""
    return MeshInterface(noProto=True)


def test_capabilities_match_golden_surface() -> None:
    """Capability discovery returns the pinned contract version and options."""
    golden = _golden()
    capabilities = getCommandCapabilities()
    assert capabilities.apiVersion == golden["apiVersion"]
    assert list(capabilities.supportedOptions) == golden["supportedOptions"]


def test_help_output_matches_golden_rendering(
    offline_interface: MeshInterface,
) -> None:
    """Embedded --help renders the pinned usage text byte for byte."""
    golden = _golden()
    result = executeCommand(offline_interface, ["--help"])
    assert result.exitCode == golden["help"]["exitCode"]
    assert result.truncated == golden["help"]["truncated"]
    assert result.output == golden["help"]["output"]


def test_unsupported_option_matches_golden_refusal(
    offline_interface: MeshInterface,
) -> None:
    """Unsupported options are refused with the pinned status and message."""
    golden = _golden()
    argv = golden["unsupportedOption"]["argv"]
    result: CommandResult = executeCommand(offline_interface, argv)
    assert result.exitCode == golden["unsupportedOption"]["exitCode"]
    assert result.output == golden["unsupportedOption"]["output"]
    error = result.error
    assert error is not None
    assert type(error).__name__ == golden["unsupportedOption"]["errorType"]
