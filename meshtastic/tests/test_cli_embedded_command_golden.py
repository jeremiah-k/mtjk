"""Golden pins for the embedded command surface.

The embedded-command contract is versioned (``docs/guides/embedded-commands.md``):
the accepted option spellings, the ``--help`` rendering of that same surface, and
the refusal of unsupported options must not drift. The fixtures in
``cli_embedded_command_golden.json`` were captured from the implemented parser;
these tests fail if capability discovery, help output, or unsupported-option
handling changes without a deliberate contract update.

Capabilities and unsupported-option handling are stable across interpreter
versions, but the stdlib argparse renderer is not: Python 3.13 stopped
repeating the metavar for each option string of an argument, and Python 3.14
started rendering mutually exclusive groups nested inside argument groups as
one bracketed ``[a | b]`` list instead of individually bracketed options. The
embedded parser uses both constructs, so ``help.outputByPython`` pins one
rendering per interpreter family and the help assertion selects the entry for
the running version. A new interpreter minor must be mapped in
``_HELP_RENDERING_FAMILIES`` (and captured) deliberately.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from meshtastic.commands import CommandResult, executeCommand, getCommandCapabilities
from meshtastic.mesh_interface import MeshInterface

pytestmark = pytest.mark.unit

_GOLDEN_FILE = Path(__file__).parent / "cli_embedded_command_golden.json"

# Interpreter minor version -> ``help.outputByPython`` family. Both members of
# each family render byte-identical usage text; keep in step with the argparse
# rendering changes noted in the module docstring.
_HELP_RENDERING_FAMILIES: dict[tuple[int, int], str] = {
    (3, 11): "3.11-3.12",
    (3, 12): "3.11-3.12",
    (3, 13): "3.13",
    (3, 14): "3.14-3.15",
    (3, 15): "3.14-3.15",
}


def _golden() -> dict[str, Any]:
    """Load the captured embedded-command fixtures."""
    with _GOLDEN_FILE.open(encoding="utf-8") as handle:
        return cast("dict[str, Any]", json.load(handle))


def _golden_help_output(golden: dict[str, Any]) -> str:
    """Return the pinned ``--help`` rendering for the running interpreter."""
    outputs = golden["help"]["outputByPython"]
    version = (sys.version_info.major, sys.version_info.minor)
    family = _HELP_RENDERING_FAMILIES.get(version)
    if family is None:
        pytest.fail(
            f"No golden --help rendering is mapped for Python {version[0]}.{version[1]}; "
            f"capture one, add it to help.outputByPython, and map it in "
            f"_HELP_RENDERING_FAMILIES (known families: {sorted(outputs)})"
        )
    return cast("str", outputs[family])


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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Embedded --help renders the pinned usage text byte for byte."""
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setattr("meshtastic.cli.parser.platform.system", lambda: "Linux")
    golden = _golden()
    result = executeCommand(offline_interface, ["--help"])
    assert result.exitCode == golden["help"]["exitCode"]
    assert result.truncated == golden["help"]["truncated"]
    assert result.output == _golden_help_output(golden)


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
