#!/usr/bin/env python3
"""Generate a markdown reference of schema-constrained configuration fields."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator, Sequence
from typing import Any

from google.protobuf.descriptor import Descriptor, FieldDescriptor

from meshtastic.cli.config_io import _field_json_document
from meshtastic.cli.preference_runtime import protobuf_field_type_label
from meshtastic.protobuf import localonly_pb2


def _iter_fields(
    descriptor: Descriptor, prefix: str = ""
) -> Iterator[tuple[str, FieldDescriptor]]:
    """Yield field paths in the same schema order as JSON introspection.

    Repeated-message fields are configurable as array values and therefore get
    their own row for container limits. Their element fields are also emitted so
    per-element firmware constraints (for example string ``max_size``) are not
    omitted from the generated reference.
    """
    for field in descriptor.fields:
        canonical = f"{prefix}{field.name}"
        if field.message_type is not None:
            if field.is_repeated:
                yield canonical, field
            yield from _iter_fields(field.message_type, prefix=f"{canonical}.")
            continue
        yield canonical, field


def _bounds_text(document: dict[str, Any]) -> str:
    """Render declared presentation bounds as table text."""
    minimum = document.get("min_value")
    maximum = document.get("max_value")
    if minimum is not None and maximum is not None:
        return f"{minimum:g} to {maximum:g}"
    if minimum is not None:
        return f"at least {minimum:g}"
    if maximum is not None:
        return f"at most {maximum:g}"
    return ""


def _limits_text(document: dict[str, Any], field: FieldDescriptor) -> str:
    """Render declared firmware limits as table text.

    String max_size reserves one byte for the NUL terminator, matching the
    firmware enforcement semantics.
    """
    limits = document.get("limits")
    if not limits:
        return ""
    parts = []
    if "max_size" in limits:
        capacity = limits["max_size"]
        if field.type == FieldDescriptor.TYPE_STRING:
            capacity -= 1
        parts.append(f"max {capacity} bytes")
    if "max_count" in limits:
        parts.append(f"max {limits['max_count']} entries")
    if "int_size" in limits:
        parts.append(f"{limits['int_size']}-bit")
    return ", ".join(parts)


def _flags_text(document: dict[str, Any]) -> str:
    """Render access and deprecation flags as table text."""
    parts = []
    if document.get("diy_only"):
        parts.append("DIY")
    if document.get("admin_only"):
        parts.append("admin")
    if document.get("deprecated"):
        parts.append("deprecated")
    return ", ".join(parts)


def _render_root(title: str, root_descriptor: Descriptor) -> list[str]:
    """Render one configuration root as a markdown table."""
    lines = [
        f"## {title}",
        "",
        "| Field | Type | Label | Bounds | Firmware limits | Flags |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for canonical, field in _iter_fields(root_descriptor):
        document = _field_json_document(
            field, canonical, type_label=protobuf_field_type_label
        )
        row = [
            f"`{canonical}`",
            str(document["type"]),
            str(document.get("label", "")),
            _bounds_text(document),
            _limits_text(document, field),
            _flags_text(document),
        ]
        lines.append("| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |")
    lines.append("")
    return lines


def build_reference() -> str:
    """Return the full markdown field reference document."""
    lines = [
        "# Schema-constrained configuration fields",
        "",
        "Generated from the current protobuf schemas; regenerate with",
        "`make field-reference`.",
        "",
    ]
    lines.extend(_render_root("Local config", localonly_pb2.LocalConfig.DESCRIPTOR))
    lines.extend(
        _render_root("Module config", localonly_pb2.LocalModuleConfig.DESCRIPTOR)
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Write the markdown reference to stdout or a file."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-o",
        "--output",
        help="Destination path; defaults to stdout.",
    )
    args = parser.parse_args(argv)
    reference = build_reference()
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(reference)
            handle.write("\n")
    else:
        sys.stdout.write(reference + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
