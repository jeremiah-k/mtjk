"""Tests for schema-level Meshtastic protobuf metadata access."""

from __future__ import annotations

import pytest
from google.protobuf import descriptor_pb2, descriptor_pool

from meshtastic.protobuf import config_pb2, localonly_pb2
from meshtastic.schema_metadata import (
    _format_numeric_bound,
    _get_enum_value_metadata,
    _get_field_metadata,
    _resolve_enum_value_name,
    getEnumValueMetadata,
    getFieldLimits,
    getFieldMetadata,
)


@pytest.mark.unit
def test_field_metadata_reads_current_hop_limit_annotations() -> None:
    """Generated custom options remain visible through Python descriptors."""
    field = config_pb2.Config.LoRaConfig.DESCRIPTOR.fields_by_name["hop_limit"]

    metadata = _get_field_metadata(field)

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

    metadata = _get_field_metadata(field)

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

    metadata = _get_enum_value_metadata(value)

    assert metadata is not None
    assert metadata.label == "Long Range - Fast"
    assert metadata.keywords == ("longfast", "default")


@pytest.mark.unit
def test_unannotated_field_has_no_metadata() -> None:
    """Unannotated schema fields do not synthesize metadata defaults."""
    field = config_pb2.Config.LoRaConfig.DESCRIPTOR.fields_by_name["frequency_offset"]

    assert _get_field_metadata(field) is None


@pytest.mark.unit
def test_expanded_schema_annotations_reach_normalized_metadata() -> None:
    """Later upstream annotation expansions flow through unchanged plumbing."""
    field = config_pb2.Config.LoRaConfig.DESCRIPTOR.fields_by_name["tx_power"]

    metadata = _get_field_metadata(field)

    assert metadata is not None
    assert metadata.label == "Transmit Power"
    assert metadata.unit == "dBm"
    assert metadata.min_value == 0.0
    assert metadata.max_value == 30.0
    assert metadata.keywords == ("tx", "power", "dbm", "output", "gain")


@pytest.mark.unit
def test_standard_field_deprecation_is_preserved_without_custom_metadata() -> None:
    """Standard FieldOptions deprecation is part of normalized metadata."""
    field = localonly_pb2.LocalConfig().device.DESCRIPTOR.fields_by_name[
        "serial_enabled"
    ]

    metadata = _get_field_metadata(field)

    assert metadata is not None
    assert metadata.deprecated is True


@pytest.mark.unit
def test_standard_enum_value_deprecation_is_preserved_without_custom_metadata() -> None:
    """Standard EnumValueOptions deprecation is retained for enum descriptions."""
    field = localonly_pb2.LocalConfig().device.DESCRIPTOR.fields_by_name["role"]
    value = field.enum_type.values_by_name["ROUTER_CLIENT"]

    metadata = _get_enum_value_metadata(value)

    assert metadata is not None
    assert metadata.deprecated is True


@pytest.mark.unit
def test_get_field_metadata_resolves_config_path() -> None:
    """Public lookup resolves config paths to their declared metadata."""
    metadata = getFieldMetadata("lora.hop_limit")

    assert metadata is not None
    assert metadata.min_value == 0.0
    assert metadata.max_value == 7.0
    assert metadata.label == "Hop Limit"


@pytest.mark.unit
def test_get_field_metadata_normalizes_camel_case_segments() -> None:
    """CamelCase segments resolve to the same metadata as snake_case ones."""
    assert getFieldMetadata("lora.hopLimit") == getFieldMetadata("lora.hop_limit")


@pytest.mark.unit
def test_get_field_metadata_resolves_module_config_path() -> None:
    """Public lookup covers module_config sections without a root hint."""
    metadata = getFieldMetadata("ambient_lighting.red")

    assert metadata is not None
    assert metadata.max_value == 255.0


@pytest.mark.unit
def test_get_field_metadata_returns_none_for_unknown_paths() -> None:
    """Unknown sections, unknown fields, and section-only paths return None."""
    assert getFieldMetadata("nosuch.hop_limit") is None
    assert getFieldMetadata("lora.nosuch") is None
    assert getFieldMetadata("lora") is None


@pytest.mark.unit
def test_get_field_metadata_returns_none_for_unannotated_field() -> None:
    """Fields that declare no metadata resolve to None on the public path."""
    assert getFieldMetadata("power.sds_secs") is None


@pytest.mark.unit
def test_field_metadata_has_bounds_property() -> None:
    """The hasBounds property reflects either declared numeric bound."""
    bounded = getFieldMetadata("lora.hop_limit")
    unbounded = getFieldMetadata("position.rx_gpio")

    assert bounded is not None and bounded.hasBounds is True
    assert unbounded is not None and unbounded.hasBounds is False


@pytest.mark.unit
def test_field_metadata_instances_are_immutable() -> None:
    """Public metadata objects cannot be mutated after construction."""
    metadata = getFieldMetadata("lora.hop_limit")

    assert metadata is not None
    with pytest.raises(AttributeError):
        metadata.min_value = 1.0  # type: ignore[misc]


@pytest.mark.unit
def test_get_field_metadata_resolves_nested_field_paths() -> None:
    """Public lookup walks nested messages inside a section."""
    metadata = getFieldMetadata("mqtt.map_report_settings.publish_interval_secs")

    assert metadata is not None
    assert metadata.unit == "s"
    assert metadata.label == "Map Publish Interval"
    assert getFieldMetadata("device_ui.node_filter.node_name") is None


@pytest.mark.unit
def test_get_field_metadata_rejects_scalar_intermediate_segments() -> None:
    """Paths continuing past a leaf field or unknown middle segment return None."""
    assert getFieldMetadata("lora.hop_limit.deeper") is None
    assert getFieldMetadata("lora.nosuch.deeper") is None


@pytest.mark.unit
def test_enum_value_without_metadata_returns_none() -> None:
    """Enum values carrying no annotation options produce no metadata."""
    value = config_pb2.Config.DeviceConfig.BuzzerMode.DESCRIPTOR.values_by_name[
        "ALL_ENABLED"
    ]

    assert _get_enum_value_metadata(value) is None


@pytest.mark.unit
def test_format_numeric_bound_drops_unneeded_decimals() -> None:
    """Bound formatting keeps decimals only when present."""
    assert _format_numeric_bound(7.0) == "7"
    assert _format_numeric_bound(0.5) == "0.5"


@pytest.mark.unit
def test_enum_name_resolution_is_truly_case_insensitive() -> None:
    """Public case-insensitive semantics do not depend on uppercase proto style."""
    file_proto = descriptor_pb2.FileDescriptorProto(
        name="schema_metadata_casefold.proto", package="schema_metadata_test"
    )
    enum_proto = file_proto.enum_type.add(name="MixedCaseEnum")
    enum_proto.value.add(name="Mixed_Name", number=0)
    enum_descriptor = (
        descriptor_pool.DescriptorPool()
        .Add(file_proto)
        .enum_types_by_name["MixedCaseEnum"]
    )

    resolved = _resolve_enum_value_name(enum_descriptor, "mixed_name")

    assert resolved is not None
    assert resolved.name == "Mixed_Name"


@pytest.mark.unit
def test_get_field_limits_returns_declared_max_size() -> None:
    """String size limits are readable from the nanopb options."""
    limits = getFieldLimits("network.ntp_server")

    assert limits is not None
    assert limits.max_size == 33
    assert limits.max_count is None
    assert limits.int_size is None


@pytest.mark.unit
def test_get_field_limits_returns_declared_max_count() -> None:
    """Repeated-field element counts are readable from the nanopb options."""
    limits = getFieldLimits("lora.ignore_incoming")

    assert limits is not None
    assert limits.max_count == 3
    assert limits.max_size is None


@pytest.mark.unit
def test_get_field_limits_returns_declared_int_size() -> None:
    """Integer width declarations surface as bit widths."""
    limits = getFieldLimits("device.buzzer_mode")

    assert limits is not None
    assert limits.int_size == 8


@pytest.mark.unit
def test_get_field_limits_returns_none_without_declarations() -> None:
    """Fields with no nanopb limits and unknown paths return None."""
    assert getFieldLimits("power.sds_secs") is None
    assert getFieldLimits("nosuch.field") is None


@pytest.mark.unit
def test_get_enum_value_metadata_resolves_by_name() -> None:
    """Enum value metadata resolves through the field path."""
    metadata = getEnumValueMetadata("lora.modem_preset", "LONG_FAST")

    assert metadata is not None
    assert metadata.label == "Long Range - Fast"
    assert metadata.keywords == ("longfast", "default")


@pytest.mark.unit
def test_get_enum_value_metadata_normalizes_value_name_case() -> None:
    """Lower-case value names resolve to the same metadata."""
    assert getEnumValueMetadata(
        "lora.modem_preset", "long_fast"
    ) == getEnumValueMetadata("lora.modem_preset", "LONG_FAST")


@pytest.mark.unit
def test_get_enum_value_metadata_returns_none_for_unknown_targets() -> None:
    """Unknown values, non-enum fields, and unknown paths return None."""
    assert getEnumValueMetadata("lora.modem_preset", "NO_SUCH_MODE") is None
    assert getEnumValueMetadata("network.ntp_server", "ANY") is None
    assert getEnumValueMetadata("nosuch.field", "ANY") is None


@pytest.mark.unit
def test_field_limits_instances_are_immutable() -> None:
    """Public limit objects cannot be mutated after construction."""
    limits = getFieldLimits("network.ntp_server")

    assert limits is not None
    with pytest.raises(AttributeError):
        limits.max_size = 1  # type: ignore[misc]
