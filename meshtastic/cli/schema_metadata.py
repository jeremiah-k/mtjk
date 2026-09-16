"""Read optional Meshtastic schema metadata from protobuf descriptors.

The upstream schema carries presentation metadata as custom protobuf options.
Python retains those options in descriptors, so the CLI can consume them
without adding the separate reflection-free registry generator used by clients
that do not ship descriptors.
"""

from __future__ import annotations

from dataclasses import dataclass

from google.protobuf.descriptor import EnumValueDescriptor, FieldDescriptor

from meshtastic.protobuf import field_metadata_pb2


@dataclass(frozen=True, slots=True)
class _SchemaMetadata:
    """Normalized view of optional field or enum-value presentation metadata."""

    diy_only: bool | None = None
    admin_only: bool | None = None
    min_value: float | None = None
    max_value: float | None = None
    unit: str | None = None
    deprecated: bool | None = None
    label: str | None = None
    description: str | None = None
    keywords: tuple[str, ...] = ()

    @property
    def _has_bounds(self) -> bool:
        """Return whether either numeric presentation bound is present."""
        return self.min_value is not None or self.max_value is not None


def _normalize_metadata(
    metadata: field_metadata_pb2.FieldMetadata, *, standard_deprecated: bool
) -> _SchemaMetadata:
    """Convert the generated proto2 message into an immutable optional-value view."""
    keywords = metadata.keywords if metadata.HasField("keywords") else None
    return _SchemaMetadata(
        diy_only=metadata.diy_only if metadata.HasField("diy_only") else None,
        admin_only=metadata.admin_only if metadata.HasField("admin_only") else None,
        min_value=metadata.min_value if metadata.HasField("min_value") else None,
        max_value=metadata.max_value if metadata.HasField("max_value") else None,
        unit=metadata.unit if metadata.HasField("unit") else None,
        deprecated=(
            True
            if standard_deprecated
            else (metadata.deprecated if metadata.HasField("deprecated") else None)
        ),
        label=metadata.label if metadata.HasField("label") else None,
        description=(
            metadata.description if metadata.HasField("description") else None
        ),
        keywords=(
            tuple(part.strip() for part in keywords.split("|") if part.strip())
            if keywords is not None
            else ()
        ),
    )


def _get_field_metadata(field: FieldDescriptor) -> _SchemaMetadata | None:
    """Return normalized metadata for ``field``, including standard deprecation."""
    options = field.GetOptions()
    standard_deprecated = bool(options.deprecated)
    if not options.HasExtension(field_metadata_pb2.field_metadata):
        return _SchemaMetadata(deprecated=True) if standard_deprecated else None
    return _normalize_metadata(
        options.Extensions[field_metadata_pb2.field_metadata],
        standard_deprecated=standard_deprecated,
    )


def _get_enum_value_metadata(value: EnumValueDescriptor) -> _SchemaMetadata | None:
    """Return normalized metadata for an enum value, including deprecation."""
    options = value.GetOptions()
    standard_deprecated = bool(options.deprecated)
    if not options.HasExtension(field_metadata_pb2.enum_value_metadata):
        return _SchemaMetadata(deprecated=True) if standard_deprecated else None
    return _normalize_metadata(
        options.Extensions[field_metadata_pb2.enum_value_metadata],
        standard_deprecated=standard_deprecated,
    )
