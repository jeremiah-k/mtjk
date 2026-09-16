"""Tests for schema-level Meshtastic protobuf metadata access."""

from __future__ import annotations

import pytest

from meshtastic.cli.schema_metadata import get_enum_value_metadata, get_field_metadata
from meshtastic.protobuf import config_pb2


@pytest.mark.unit
def test_field_metadata_reads_current_hop_limit_annotations() -> None:
    """Generated custom options remain visible through Python descriptors."""
    field = config_pb2.Config.LoRaConfig.DESCRIPTOR.fields_by_name["hop_limit"]

    metadata = get_field_metadata(field)

    assert metadata is not None
    assert metadata.min_value == 0.0
    assert metadata.max_value == 7.0
    assert metadata.label == "Hop Limit"
    assert metadata.description == (
        "How many times a message may be repeated before it stops being forwarded."
    )
    assert metadata.keywords == ("hops", "ttl", "range", "rebroadcast")


@pytest.mark.unit
def test_field_metadata_preserves_absent_optional_values() -> None:
    """Absent proto2 metadata fields remain distinguishable from explicit false."""
    field = config_pb2.Config.PositionConfig.DESCRIPTOR.fields_by_name["rx_gpio"]

    metadata = get_field_metadata(field)

    assert metadata is not None
    assert metadata.diy_only is True
    assert metadata.admin_only is None
    assert metadata.deprecated is None
    assert metadata.min_value is None
    assert metadata.max_value is None


@pytest.mark.unit
def test_enum_value_metadata_reads_current_label_annotations() -> None:
    """Enum value labels and keywords are available without a generated registry."""
    value = config_pb2.Config.LoRaConfig.ModemPreset.DESCRIPTOR.values_by_name[
        "LONG_FAST"
    ]

    metadata = get_enum_value_metadata(value)

    assert metadata is not None
    assert metadata.label == "Long Range - Fast"
    assert metadata.keywords == ("longfast", "default")


@pytest.mark.unit
def test_unannotated_field_has_no_metadata() -> None:
    """Unannotated schema fields do not synthesize metadata defaults."""
    field = config_pb2.Config.LoRaConfig.DESCRIPTOR.fields_by_name["tx_power"]

    assert get_field_metadata(field) is None
