"""Configure-path config-section acquisition and snapshot-coherence tests."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from google.protobuf.descriptor import FieldDescriptor

from meshtastic.cli import configure_actions, configure_values
from meshtastic.cli.configure_actions import _requested_configure_section_fields
from meshtastic.node import Node
from meshtastic.protobuf import localonly_pb2


def _config_node() -> MagicMock:
    """Return a node mock with real protobuf config wrappers."""
    node = MagicMock(autospec=Node)
    node.localConfig = localonly_pb2.LocalConfig()
    node.moduleConfig = localonly_pb2.LocalModuleConfig()
    return node


def _prepared(
    config_sections: dict[str, dict] | None = None,
    module_config_sections: dict[str, dict] | None = None,
) -> configure_actions._PreparedConfigureDocument:
    """Build a prepared configure document carrying only section mappings."""
    return configure_actions._PreparedConfigureDocument(
        direct_values=configure_values._DirectConfigureValues(),
        config_sections=config_sections or {},
        module_config_sections=module_config_sections or {},
    )


@pytest.mark.unit
def test_requested_section_fields_resolves_both_roots_in_apply_order() -> None:
    """Snake and Camel YAML keys resolve onto their owning protobuf roots."""
    node = _config_node()
    prepared = _prepared(
        config_sections={"lora": {"hop_limit": 7}},
        module_config_sections={"externalNotification": {"enabled": True}},
    )

    section_fields = _requested_configure_section_fields(node, prepared)

    assert [(root, field.name) for root, field in section_fields] == [
        (node.localConfig, "lora"),
        (node.moduleConfig, "external_notification"),
    ]
    for _root, field in section_fields:
        assert isinstance(field, FieldDescriptor)


@pytest.mark.unit
def test_requested_section_fields_skips_unknown_sections() -> None:
    """Unknown and non-section scalar names are skipped for traversal to report."""
    node = _config_node()
    prepared = _prepared(
        config_sections={
            "lora": {"hop_limit": 7},
            "not_a_section": {"x": 1},
            # version is a real LocalConfig field but a scalar, not a section.
            "version": {"ignored": True},
        },
    )

    section_fields = _requested_configure_section_fields(node, prepared)

    assert [(root, field.name) for root, field in section_fields] == [
        (node.localConfig, "lora")
    ]


@pytest.mark.unit
def test_requested_section_fields_deduplicates_case_variants() -> None:
    """YAML key spellings that normalize to one section request it once."""
    node = _config_node()
    prepared = _prepared(
        config_sections={"lora": {"hop_limit": 7}, "Lora": {"hop_limit": 9}},
    )

    section_fields = _requested_configure_section_fields(node, prepared)

    assert [(root, field.name) for root, field in section_fields] == [
        (node.localConfig, "lora")
    ]
