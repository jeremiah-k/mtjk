"""Validate outbound protobuf payloads against firmware nanopb buffer limits.

Device firmware decodes every ToRadio frame with nanopb, whose generated
decoders copy fields into fixed-size buffers declared by the ``max_size`` and
``max_count`` options in the protobuf ``*.options`` files. A frame whose
nested message overflows any of those buffers fails to decode and is dropped
silently: the device logs nothing, sends no acknowledgment, and the client
cannot distinguish a dropped frame from an applied one.

The generated Python descriptors retain those nanopb options, so limits are
read from the same schema the client was generated from rather than a
hand-maintained table. String fields reserve one byte of ``max_size`` for
nanopb's NUL terminator, so a string value may carry at most
``max_size - 1`` UTF-8 bytes. For strings, ``max_length`` overrides
``max_size`` and directly caps UTF-8 bytes; bytes and repeated fields are capped at
``max_size`` and ``max_count`` respectively. ``fixed_length`` bytes fields
must match their allocated size exactly when present. Integer fields with
``int_size`` are checked against the signed/unsigned range of the firmware
storage width.
"""

from __future__ import annotations

from typing import Any, NoReturn

from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.message import Message

from meshtastic._interface_errors import MeshInterfaceError
from meshtastic.protobuf import nanopb_pb2
from meshtastic.schema_metadata import _get_field_limits

_STRING = FieldDescriptor.TYPE_STRING
_BYTES = FieldDescriptor.TYPE_BYTES
_SIGNED_INTEGER_TYPES = frozenset(
    {
        FieldDescriptor.TYPE_INT32,
        FieldDescriptor.TYPE_INT64,
        FieldDescriptor.TYPE_SINT32,
        FieldDescriptor.TYPE_SINT64,
        FieldDescriptor.TYPE_SFIXED32,
        FieldDescriptor.TYPE_SFIXED64,
    }
)
_UNSIGNED_INTEGER_TYPES = frozenset(
    {
        FieldDescriptor.TYPE_UINT32,
        FieldDescriptor.TYPE_UINT64,
        FieldDescriptor.TYPE_FIXED32,
        FieldDescriptor.TYPE_FIXED64,
    }
)

_SILENT_DROP_NOTE = "the device would silently drop this message instead of applying it"


def _reject(context: str, problem: str) -> NoReturn:
    """Raise one interface error describing an over-limit payload."""
    raise MeshInterfaceError(f"{context}: {problem} {_SILENT_DROP_NOTE}")


def _check_string_element(
    element: str,
    *,
    usable_size: int,
    limit_size: int,
    field_path: str,
    context: str,
) -> None:
    """Reject one string value beyond its declared firmware storage cap."""
    encoded_length = len(element.encode("utf-8"))
    if encoded_length > usable_size:
        _reject(
            context,
            f"field '{field_path}' is {encoded_length} bytes, exceeding the "
            f"firmware limit of {usable_size} bytes (nanopb max_size "
            f"{limit_size} includes the NUL terminator) and",
        )


def _reject_over_count(
    field: FieldDescriptor,
    value: Any,
    *,
    field_path: str,
    context: str,
) -> None:
    """Reject one repeated field carrying more entries than firmware allows."""
    limits = _get_field_limits(field)
    if (
        limits is not None
        and limits.max_count is not None
        and len(value) > limits.max_count
    ):
        _reject(
            context,
            f"field '{field_path}' has {len(value)} entries, exceeding the "
            f"firmware limit of {limits.max_count} and",
        )


def _check_integer_width(
    field: FieldDescriptor,
    value: Any,
    *,
    bit_width: int,
    field_path: str,
    context: str,
) -> None:
    """Reject integer values that cannot fit nanopb's configured storage width."""
    if field.type in _UNSIGNED_INTEGER_TYPES:
        minimum = 0
        maximum = (1 << bit_width) - 1
        range_label = "unsigned"
    elif field.type in _SIGNED_INTEGER_TYPES:
        minimum = -(1 << (bit_width - 1))
        maximum = (1 << (bit_width - 1)) - 1
        range_label = "signed"
    else:
        # Enum storage is compiler/generator-version sensitive; protobuf enum
        # validity is handled by its own schema and is intentionally not inferred
        # from nanopb int_size here.
        return

    elements = value if field.is_repeated else [value]
    for index, element in enumerate(elements):
        if minimum <= element <= maximum:
            continue
        element_path = f"{field_path}[{index}]" if field.is_repeated else field_path
        _reject(
            context,
            f"field '{element_path}' has value {element}, exceeding the firmware "
            f"{bit_width}-bit {range_label} range {minimum}..{maximum} and",
        )


def _is_fixed_length_bytes(field: FieldDescriptor) -> bool:
    """Return whether nanopb stores one bytes field as an exact-size array."""
    if field.type != _BYTES:
        return False
    options = field.GetOptions().Extensions[nanopb_pb2.nanopb]
    return bool(options.HasField("fixed_length") and options.fixed_length)


def _check_field_limits(
    field: FieldDescriptor,
    value: Any,
    *,
    field_path: str,
    context: str,
) -> None:
    """Reject one set field whose value exceeds its declared nanopb limits."""
    _reject_over_count(field, value, field_path=field_path, context=context)
    limits = _get_field_limits(field)
    options = field.GetOptions().Extensions[nanopb_pb2.nanopb]
    max_size = limits.max_size if limits is not None else None
    if field.type == _STRING and options.HasField("max_length"):
        # nanopb's generator gives max_length precedence over max_size and
        # allocates one extra byte for the terminator.
        max_size = options.max_length + 1
    if limits is not None and limits.int_size is not None:
        _check_integer_width(
            field,
            value,
            bit_width=limits.int_size,
            field_path=field_path,
            context=context,
        )
    if max_size is None:
        return

    if field.type == _STRING:
        usable_size = max_size - 1
        elements = value if field.is_repeated else [value]
        for index, element in enumerate(elements):
            element_path = f"{field_path}[{index}]" if field.is_repeated else field_path
            _check_string_element(
                element,
                usable_size=usable_size,
                limit_size=max_size,
                field_path=element_path,
                context=context,
            )
    elif field.type == _BYTES:
        byte_values = value if field.is_repeated else [value]
        fixed_length = _is_fixed_length_bytes(field)
        for index, byte_value in enumerate(byte_values):
            element_path = f"{field_path}[{index}]" if field.is_repeated else field_path
            if fixed_length and len(byte_value) != max_size:
                _reject(
                    context,
                    f"field '{element_path}' is {len(byte_value)} bytes, but the "
                    f"firmware requires exactly {max_size} bytes and",
                )
            if len(byte_value) > max_size:
                _reject(
                    context,
                    f"field '{element_path}' is {len(byte_value)} bytes, "
                    f"exceeding the firmware limit of {max_size} bytes and",
                )


def _validate_map_entries(
    field: FieldDescriptor,
    value: Any,
    *,
    context: str,
    path: tuple[str, ...],
) -> None:
    """Validate map keys and values using their synthetic entry descriptors."""
    entry_descriptor = field.message_type
    assert entry_descriptor is not None
    key_field = entry_descriptor.fields_by_name["key"]
    value_field = entry_descriptor.fields_by_name["value"]
    _reject_over_count(
        field, value, field_path=".".join((*path, field.name)), context=context
    )
    for key, element in value.items():
        entry_path = (*path, f"{field.name}[{key!r}]")
        _check_field_limits(
            key_field,
            key,
            field_path=".".join((*entry_path, "key")),
            context=context,
        )
        if value_field.message_type is not None:
            _validate_message_fields(
                element, context=context, path=(*entry_path, "value")
            )
        else:
            _check_field_limits(
                value_field,
                element,
                field_path=".".join((*entry_path, "value")),
                context=context,
            )


def _validate_message_fields(
    message: Message,
    *,
    context: str,
    path: tuple[str, ...],
) -> None:
    """Walk one message's set fields, rejecting firmware-limit violations.

    Recursion covers singular and repeated nested messages; repeated elements
    are addressed as ``parent[i]`` in diagnostics so the offending entry is
    identifiable.
    """
    for field, value in message.ListFields():
        if field.message_type is not None:
            if field.message_type.GetOptions().map_entry:
                # Python map containers iterate keys rather than the synthetic
                # protobuf entry messages exposed by the field descriptor.
                _validate_map_entries(field, value, context=context, path=path)
            elif field.is_repeated:
                field_path = ".".join((*path, field.name))
                # Repeated message fields still enforce nanopb max_count
                # even though their elements need recursive traversal.
                _reject_over_count(field, value, field_path=field_path, context=context)
                for index, element in enumerate(value):
                    _validate_message_fields(
                        element,
                        context=context,
                        path=(*path, f"{field.name}[{index}]"),
                    )
            else:
                _validate_message_fields(
                    value,
                    context=context,
                    path=(*path, field.name),
                )
            continue
        _check_field_limits(
            field,
            value,
            field_path=".".join((*path, field.name)),
            context=context,
        )


def _validate_firmware_payload_limits(
    message: Message,
    *,
    context: str,
) -> None:
    """Raise ``MeshInterfaceError`` when a payload exceeds firmware nanopb limits.

    Device firmware decodes ToRadio frames with fixed-size nanopb buffers; an
    oversized nested field fails that decode and the whole frame is dropped
    with no feedback on either side. Call this before serializing a typed
    protobuf payload so the failure surfaces client-side with the offending
    field path and both the effective and declared limits.

    Parameters
    ----------
    message : Message
        Payload message about to be serialized into a ToRadio frame.
    context : str
        Short description of the send, used as the error message prefix
        (for example ``"Outbound payload"``).

    Raises
    ------
    MeshInterfaceError
        When any set string, bytes, repeated field, fixed-length bytes field,
        or explicitly narrowed integer violates the schema's nanopb options.
    """
    _validate_message_fields(message, context=context, path=())
