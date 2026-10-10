"""Typed values and internal resolution for fresh configuration reads."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TypeAlias, cast

from google.protobuf.descriptor import Descriptor, FieldDescriptor
from google.protobuf.message import Message

from meshtastic.admin_response import (
    _CONFIG_RESPONSE_SUBTYPE_BY_REQUEST,
    _MODULE_CONFIG_RESPONSE_SUBTYPE_BY_REQUEST,
)
from meshtastic.protobuf import config_pb2, module_config_pb2

__all__ = ("PreferenceValue",)

PreferenceValue: TypeAlias = (
    bool
    | int
    | float
    | str
    | bytes
    | Message
    | list["PreferenceValue"]
    | dict[str | int | bool, "PreferenceValue"]
)


def _resolve_field(descriptor: Descriptor, name: str) -> FieldDescriptor:
    """Accept exact protobuf field names and their JSON camelCase spellings."""
    for field in descriptor.fields:
        if name in (field.name, field.json_name):
            return field
    raise ValueError(f"Unknown {descriptor.name} field: {name}")


def _config_request(section: str, *, module: bool) -> int:
    """Resolve a section using named wire enums rather than descriptor order."""
    descriptor = (
        module_config_pb2.ModuleConfig.DESCRIPTOR
        if module
        else config_pb2.Config.DESCRIPTOR
    )
    field = _resolve_field(descriptor, section)
    mapping = (
        _MODULE_CONFIG_RESPONSE_SUBTYPE_BY_REQUEST
        if module
        else _CONFIG_RESPONSE_SUBTYPE_BY_REQUEST
    )
    for request, response_section in mapping.items():
        if response_section == field.name:
            return request
    raise ValueError(f"Configuration section is not readable: {section}")


def _preference_path(path: str) -> tuple[bool, list[FieldDescriptor]]:
    """Validate the complete path before issuing any request."""
    if not isinstance(path, str) or not path:
        raise ValueError("preference path must be a non-empty string")
    parts = path.split(".")
    module = parts[0] not in {
        spelling
        for field in config_pb2.Config.DESCRIPTOR.fields
        for spelling in (field.name, field.json_name)
    }
    descriptor = (
        module_config_pb2.ModuleConfig.DESCRIPTOR
        if module
        else config_pb2.Config.DESCRIPTOR
    )
    fields = []
    for index, part in enumerate(parts):
        field = _resolve_field(descriptor, part)
        fields.append(field)
        if index < len(parts) - 1:
            if field.message_type is None or field.is_repeated:
                raise ValueError(f"Cannot traverse preference field: {part}")
            descriptor = field.message_type
    _config_request(fields[0].name, module=module)
    return module, fields


def _copy_value(value: object, field: FieldDescriptor) -> PreferenceValue:
    """Detach protobuf messages, repeated values, and maps from their parent."""
    if field.is_repeated:
        if field.message_type and field.message_type.GetOptions().map_entry:
            value_field = field.message_type.fields_by_name["value"]
            items = cast(Mapping[str | int | bool, object], value)
            return {key: _copy_value(item, value_field) for key, item in items.items()}
        # Repeated elements have the same scalar/message kind as their field.
        return [_copy_element(item) for item in cast(Iterable[object], value)]
    return _copy_element(value)


def _copy_element(value: object) -> PreferenceValue:
    """Copy a protobuf element or retain an immutable scalar."""
    if isinstance(value, Message):
        result = type(value)()
        result.CopyFrom(value)
        return result
    if isinstance(value, (bool, int, float, str, bytes)):
        return value
    raise TypeError(f"Unsupported preference value: {type(value).__name__}")
