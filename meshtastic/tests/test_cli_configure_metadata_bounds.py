"""Coverage proving schema metadata bounds are enforced on the configure path.

`--set` has explicit coverage for schema-declared bounds; the `--configure`
preflight reaches the same assignment path through ``traverse_config`` and
``set_pref``. These tests pin that wiring so a refactor cannot silently drop
bounds enforcement for YAML-driven configuration without failing here.
"""

from types import SimpleNamespace
from typing import Any, NoReturn
from unittest.mock import MagicMock

import pytest

import meshtastic.__main__ as main_module
from meshtastic.cli.configure_actions import (
    ConfigureHooks,
    _preflight_configure_sections,
)
from meshtastic.cli.preference_runtime import CONFIGURE_PREFLIGHT_MODE
from meshtastic.protobuf import config_pb2, module_config_pb2


def _make_hooks(target_node: Any) -> tuple[ConfigureHooks, dict[str, Any]]:
    """Build real configure hooks around recording exit/print callables."""
    exits: list[tuple[str, int]] = []
    printed: list[str] = []
    recordings = {"exits": exits, "printed": printed}

    def _cli_exit(message: str, return_value: int = 1) -> NoReturn:
        exits.append((message, return_value))
        # Production exit handlers are non-returning; the runtime fails closed
        # when an injected handler returns, so mirror that contract here.
        raise SystemExit(return_value)

    hooks = ConfigureHooks(
        cli_exit=_cli_exit,
        cli_print=printed.append,
        traverse_config=main_module.traverseConfig,
        preflight_mode=CONFIGURE_PREFLIGHT_MODE,
        is_local_destination=MagicMock(return_value=True),
        post_seturl_stability_check=MagicMock(return_value=True),
        post_configure_reconnect_and_verify=MagicMock(),
        channel_url_matches_current_device_state=MagicMock(return_value=False),
        pace_configure_write=MagicMock(),
    )
    return hooks, recordings


def _make_node() -> Any:
    """Build a target node exposing fresh protobuf config roots."""
    return SimpleNamespace(
        localConfig=config_pb2.Config(),
        moduleConfig=module_config_pb2.ModuleConfig(),
    )


@pytest.mark.unit
def test_configure_preflight_rejects_out_of_bounds_config_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A YAML value beyond a schema bound terminates before device mutation."""
    node = _make_node()
    hooks, recordings = _make_hooks(node)

    with pytest.raises(SystemExit):
        _preflight_configure_sections(
            hooks,
            node,
            config_sections={"lora": {"hop_limit": 99}},
            module_config_sections={},
        )

    exits = recordings["exits"]
    assert exits, "out-of-bounds hop_limit must terminate the preflight"
    message, code = exits[0]
    assert code == 1
    assert "lora.hop_limit" in message
    assert "Failed to apply" in message
    output = capsys.readouterr().out
    assert "Invalid value 99 for lora.hop_limit" in output
    assert "between 0 and 7" in output
    assert node.localConfig.lora.hop_limit == 0


@pytest.mark.unit
def test_configure_preflight_rejects_out_of_bounds_module_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Module config values are bounds-checked on the same preflight path."""
    node = _make_node()
    hooks, recordings = _make_hooks(node)

    with pytest.raises(SystemExit):
        _preflight_configure_sections(
            hooks,
            node,
            config_sections={},
            module_config_sections={"ambient_lighting": {"red": 999}},
        )

    assert recordings["exits"], "out-of-bounds ambient_lighting.red must terminate"
    output = capsys.readouterr().out
    assert "between 0 and 255" in output
    assert node.moduleConfig.ambient_lighting.red == 0


@pytest.mark.unit
def test_configure_preflight_accepts_in_bounds_values() -> None:
    """In-bounds values pass preflight without terminating the CLI."""
    node = _make_node()
    hooks, recordings = _make_hooks(node)

    _preflight_configure_sections(
        hooks,
        node,
        config_sections={"lora": {"hop_limit": 3}},
        module_config_sections={"ambient_lighting": {"red": 200}},
    )

    assert recordings["exits"] == []
    # Preflight validates protobuf copies; the live config roots stay untouched
    # until the configure plan applies them after the transaction opens.
    assert node.localConfig.lora.hop_limit == 0
    assert node.moduleConfig.ambient_lighting.red == 0
