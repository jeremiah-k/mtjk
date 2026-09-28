"""Public access to Meshtastic protobuf schema field metadata.

The schema carries presentation metadata as custom protobuf options. Python
retains those options in descriptors, so they can be read without a separate
reflection-free registry. The CLI consumes this module to validate and
describe configuration fields; it is also the public surface for external
tooling that needs the same constraint data (bounds, units, labels, and
deprecation) without importing CLI internals.

Public entry points follow the package camelCase convention; the descriptor
level helpers stay private for CLI-internal use.
"""

from __future__ import annotations

from dataclasses import dataclass

from google.protobuf.descriptor import (
    Descriptor,
    EnumValueDescriptor,
    FieldDescriptor,
)

import meshtastic.util
from meshtastic.protobuf import (
    config_pb2,
    field_metadata_pb2,
    module_config_pb2,
)


@dataclass(frozen=True, slots=True)
class FieldMetadata:
    """Immutable view of declared presentation metadata for one schema field.

    Attributes
    ----------
    diy_only : bool | None
        Whether the field is intended for DIY-node configuration only.
    admin_only : bool | None
        Whether writes require an admin relationship with the node.
    min_value : float | None
        Declared lower presentation bound, when present.
    max_value : float | None
        Declared upper presentation bound, when present.
    unit : str | None
        Declared unit of measurement, when present.
    deprecated : bool | None
        Whether the field is marked deprecated in the schema.
    label : str | None
        Human-readable short label, when present.
    description : str | None
        Human-readable description, when present.
    keywords : tuple[str, ...]
        Search keywords declared for the field.
    """

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
    def hasBounds(self) -> bool:
        """Return whether either numeric presentation bound is present."""
        return self.min_value is not None or self.max_value is not None


def getFieldMetadata(path: str) -> FieldMetadata | None:
    """Return declared metadata for one configuration field path.

    Parameters
    ----------
    path : str
        Dotted field path relative to the configuration roots, using the
        same section and field names as ``--set`` and ``--configure``
        (for example ``"lora.hop_limit"`` or ``"ambient_lighting.red"``).
        camelCase segments are accepted and normalized. The section is
        resolved against both the ``config`` and ``module_config`` roots.

    Returns
    -------
    FieldMetadata | None
        The declared metadata, or ``None`` when the path does not resolve
        to a known field or the field declares no metadata.
    """
    segments = [meshtastic.util.camel_to_snake(part) for part in path.split(".")]
    if len(segments) < 2:
        return None
    section, field_path = segments[0], segments[1:]
    for root in _configuration_roots():
        field = _resolve_field_path(root, section, field_path)
        if field is not None:
            return _get_field_metadata(field)
    return None


def _configuration_roots() -> tuple[Descriptor, ...]:
    """Return the configuration root descriptors searched by path lookups."""
    return (
        config_pb2.Config.DESCRIPTOR,
        module_config_pb2.ModuleConfig.DESCRIPTOR,
    )


def _resolve_field_path(
    root: Descriptor, section: str, field_path: list[str]
) -> FieldDescriptor | None:
    """Resolve one section and nested field path inside a config root.

    Parameters
    ----------
    root : Descriptor
        Configuration root descriptor to search.
    section : str
        Snake_case section name within the root.
    field_path : list[str]
        Remaining snake_case segments; the last one names the field.

    Returns
    -------
    FieldDescriptor | None
        The resolved field, or ``None`` when any segment is unknown or a
        non-final segment is not a nested message.
    """
    current: FieldDescriptor | None = root.fields_by_name.get(section)
    if current is None or current.message_type is None:
        return None
    for name in field_path[:-1]:
        assert current is not None
        nested = current.message_type.fields_by_name.get(name)
        if nested is None or nested.message_type is None:
            return None
        current = nested
    return current.message_type.fields_by_name.get(field_path[-1])


def _normalize_metadata(
    metadata: field_metadata_pb2.FieldMetadata, *, standard_deprecated: bool
) -> FieldMetadata:
    """Convert the generated proto2 message into an immutable optional-value view."""
    keywords = metadata.keywords if metadata.HasField("keywords") else None
    return FieldMetadata(
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


def _format_numeric_bound(value: float) -> str:
    """Format a metadata numeric bound without unnecessary decimal noise."""
    return str(int(value)) if value.is_integer() else f"{value:g}"


def _get_field_metadata(field: FieldDescriptor) -> FieldMetadata | None:
    """Return normalized metadata for ``field``, including standard deprecation."""
    options = field.GetOptions()
    standard_deprecated = bool(options.deprecated)
    if not options.HasExtension(field_metadata_pb2.field_metadata):
        return FieldMetadata(deprecated=True) if standard_deprecated else None
    return _normalize_metadata(
        options.Extensions[field_metadata_pb2.field_metadata],
        standard_deprecated=standard_deprecated,
    )


def _get_enum_value_metadata(value: EnumValueDescriptor) -> FieldMetadata | None:
    """Return normalized metadata for an enum value, including deprecation."""
    options = value.GetOptions()
    standard_deprecated = bool(options.deprecated)
    if not options.HasExtension(field_metadata_pb2.enum_value_metadata):
        return FieldMetadata(deprecated=True) if standard_deprecated else None
    return _normalize_metadata(
        options.Extensions[field_metadata_pb2.enum_value_metadata],
        standard_deprecated=standard_deprecated,
    )
