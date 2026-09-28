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


def _make_hooks(
    *, traverse_config: Any = main_module.traverseConfig
) -> tuple[ConfigureHooks, dict[str, Any]]:
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
        traverse_config=traverse_config,
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


def _snapshot(message: Any) -> Any:
    """Return a deep copy of a protobuf config root for before/after comparison."""
    copied = type(message)()
    copied.CopyFrom(message)
    return copied


@pytest.mark.unit
def test_configure_preflight_rejects_out_of_bounds_config_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A YAML value beyond a schema bound terminates before device mutation."""
    node = _make_node()
    hooks, recordings = _make_hooks()
    local_before = _snapshot(node.localConfig)
    module_before = _snapshot(node.moduleConfig)

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
    assert node.localConfig == local_before
    assert node.moduleConfig == module_before


@pytest.mark.unit
def test_configure_preflight_rejects_out_of_bounds_module_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Module config values are bounds-checked on the same preflight path."""
    node = _make_node()
    hooks, recordings = _make_hooks()
    local_before = _snapshot(node.localConfig)
    module_before = _snapshot(node.moduleConfig)

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
    assert node.localConfig == local_before
    assert node.moduleConfig == module_before


@pytest.mark.unit
def test_configure_preflight_accepts_in_bounds_values() -> None:
    """In-bounds values are applied to the preflight copies, not the live roots."""
    node = _make_node()
    candidates: list[Any] = []

    def _recording_traverse(
        section: str, values: dict[str, Any], interface_config: Any, **kwargs: Any
    ) -> bool:
        candidates.append(interface_config)
        return main_module.traverseConfig(section, values, interface_config, **kwargs)

    hooks, recordings = _make_hooks(traverse_config=_recording_traverse)
    local_before = _snapshot(node.localConfig)
    module_before = _snapshot(node.moduleConfig)

    _preflight_configure_sections(
        hooks,
        node,
        config_sections={"lora": {"hop_limit": 3}},
        module_config_sections={"ambient_lighting": {"red": 200}},
    )

    assert recordings["exits"] == []
    # The in-bounds values must reach the protobuf copies preflight validates,
    # proving traversal and assignment actually ran rather than being skipped.
    assert len(candidates) == 2
    config_candidate, module_candidate = candidates
    assert isinstance(config_candidate, config_pb2.Config)
    assert isinstance(module_candidate, module_config_pb2.ModuleConfig)
    assert config_candidate.lora.hop_limit == 3
    assert module_candidate.ambient_lighting.red == 200
    # Preflight validates copies; compare the complete live roots so any field
    # the preflight might touch, not just the ones under test, is caught here.
    assert config_candidate is not node.localConfig
    assert module_candidate is not node.moduleConfig
    assert node.localConfig == local_before
    assert node.moduleConfig == module_before


@pytest.mark.unit
def test_configure_preflight_rejects_over_limit_string_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A YAML string beyond the firmware size limit terminates before mutation."""
    node = _make_node()
    hooks, recordings = _make_hooks()
    local_before = _snapshot(node.localConfig)
    module_before = _snapshot(node.moduleConfig)

    with pytest.raises(SystemExit):
        _preflight_configure_sections(
            hooks,
            node,
            config_sections={"network": {"wifi_ssid": "x" * 33}},
            module_config_sections={},
        )

    assert recordings["exits"], "over-limit wifi_ssid must terminate the preflight"
    output = capsys.readouterr().out
    assert "encoded length 33 bytes exceeds the firmware limit of 32 bytes" in output
    assert node.localConfig == local_before
    assert node.moduleConfig == module_before


@pytest.mark.unit
def test_configure_preflight_rejects_over_limit_repeated_list(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A YAML list beyond the firmware count limit terminates before mutation."""
    node = _make_node()
    hooks, recordings = _make_hooks()
    local_before = _snapshot(node.localConfig)
    module_before = _snapshot(node.moduleConfig)

    with pytest.raises(SystemExit):
        _preflight_configure_sections(
            hooks,
            node,
            config_sections={"lora": {"ignore_incoming": [1, 2, 3, 4]}},
            module_config_sections={},
        )

    assert recordings["exits"], "over-count ignore_incoming must terminate"
    output = capsys.readouterr().out
    assert "4 entries exceeds the firmware limit of 3" in output
    assert node.localConfig == local_before
    assert node.moduleConfig == module_before
