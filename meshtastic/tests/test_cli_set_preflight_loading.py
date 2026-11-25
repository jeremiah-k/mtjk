"""Regression tests for loading --set config sections before atomic preflight."""

from types import SimpleNamespace
from typing import NoReturn
from unittest.mock import MagicMock

import pytest
from google.protobuf.descriptor import FieldDescriptor

import meshtastic.__main__ as main_module
import meshtastic.cli.set_actions as cli_set_actions
from meshtastic.cli.set_actions import (
    _ensure_set_sections_loaded,
    _normalize_set_entries,
    _resolve_set_target,
)
from meshtastic.node import Node
from meshtastic.protobuf import localonly_pb2
from meshtastic.tcp_interface import TCPInterface


def _refusing_exit(message: str, return_value: int = 1) -> NoReturn:
    """Fail the test if a section load refuses instead of exiting the runner."""
    raise AssertionError(f"unexpected cli_exit: {message!r} ({return_value})")


def _config_node() -> MagicMock:
    """Return a node mock with real protobuf config wrappers."""
    node = MagicMock(autospec=Node)
    node.localConfig = localonly_pb2.LocalConfig()
    node.moduleConfig = localonly_pb2.LocalModuleConfig()
    return node


@pytest.mark.unit
def test_resolve_set_target_uses_one_root_ownership_contract() -> None:
    """Root resolution should identify local, module, and unknown sections."""
    node = _config_node()
    configs = (node.localConfig, node.moduleConfig)

    local_target = _resolve_set_target(configs, "bluetooth.enabled")
    module_target = _resolve_set_target(configs, "external_notification.enabled")

    assert local_target is not None
    assert local_target[0] is node.localConfig
    assert local_target[1].name == "bluetooth"
    assert module_target is not None
    assert module_target[0] is node.moduleConfig
    assert module_target[1].name == "external_notification"
    assert _resolve_set_target(configs, "not_a_section.value") is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("wrapper_name", "section_name", "pref_name"),
    (
        ("localConfig", "bluetooth", "bluetooth.enabled"),
        ("moduleConfig", "mqtt", "mqtt.enabled"),
    ),
)
def test_loaded_default_section_is_not_requested_again(
    wrapper_name: str,
    section_name: str,
    pref_name: str,
) -> None:
    """Message presence, not populated scalar fields, marks a section as loaded."""
    node = _config_node()
    wrapper = getattr(node, wrapper_name)
    section = getattr(wrapper, section_name)
    section.SetInParent()
    assert wrapper.HasField(section_name)
    assert section.ListFields() == []

    _ensure_set_sections_loaded(
        node,
        _normalize_set_entries(((pref_name, "true"),)),
        cli_exit=_refusing_exit,
    )

    node.requestConfig.assert_not_called()


@pytest.mark.unit
def test_missing_section_is_requested_once_for_multiple_entries() -> None:
    """Multiple settings in one missing section should trigger one device read."""
    node = _config_node()

    _ensure_set_sections_loaded(
        node,
        _normalize_set_entries(
            (
                ("lora.hop_limit", "3"),
                ("lora.tx_power", "10"),
            )
        ),
        cli_exit=_refusing_exit,
    )

    node.requestConfig.assert_called_once()
    assert node.requestConfig.call_args.args[0].name == "lora"


@pytest.mark.unit
def test_unknown_nested_field_does_not_trigger_device_read() -> None:
    """Schema-invalid settings should fail locally without unnecessary requests."""
    node = _config_node()

    _ensure_set_sections_loaded(
        node,
        _normalize_set_entries((("lora.not_a_field", "1"),)),
        cli_exit=_refusing_exit,
    )

    node.requestConfig.assert_not_called()


@pytest.mark.unit
def test_request_can_populate_section_before_preflight_snapshot() -> None:
    """A synchronous remote-style request should populate state before copying."""
    node = _config_node()

    def populate_lora(config_type: FieldDescriptor) -> None:
        assert config_type.name == "lora"
        node.localConfig.lora.ignore_incoming.append(123)

    node.requestConfig.side_effect = populate_lora
    _ensure_set_sections_loaded(
        node,
        _normalize_set_entries((("lora.ignore_incoming", "456"),)),
        cli_exit=_refusing_exit,
    )

    assert list(node.localConfig.lora.ignore_incoming) == [123]


@pytest.mark.unit
def test_handle_set_loads_sections_before_invoking_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The command must snapshot only after requestConfig has updated live state."""
    node = _config_node()
    interface = MagicMock(autospec=TCPInterface)
    interface.getNode.return_value = node
    args = SimpleNamespace(
        dest=None,
        set=(("lora.ignore_incoming", "456"),),
    )
    observed_preflight_values: list[list[int]] = []

    def populate_lora(config_type: FieldDescriptor) -> None:
        assert config_type.name == "lora"
        node.localConfig.lora.ignore_incoming.append(123)

    def inspect_preflight(
        target_node: MagicMock,
        _entries: list[tuple[str, object]],
        *,
        cli_exit: object = None,
        cli_print: object = None,
    ) -> bool:
        """Record the current LoRa ignore-incoming values observed during preflight.

        Parameters
        ----------
        target_node : MagicMock
            Node whose configuration is inspected.
        _entries : list[tuple[str, object]]
            Settings being preflighted.

        Returns
        -------
        bool
            ``False`` to reject the preflight.
        """
        observed_preflight_values.append(
            list(target_node.localConfig.lora.ignore_incoming)
        )
        return False

    node.requestConfig.side_effect = populate_lora
    monkeypatch.setattr(cli_set_actions, "_preflight_set_entries", inspect_preflight)

    main_module._handle_set_command(interface, args, {})

    assert observed_preflight_values == [[123]]
    node.writeConfig.assert_not_called()
