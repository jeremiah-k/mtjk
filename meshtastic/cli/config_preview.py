"""Shared private contract for ``--dry-run`` configuration previews.

This module deliberately stays small. It owns the three pieces shared by the
``--set`` and ``--configure`` preview paths so they cannot drift apart:

- the protobuf-copy plan state that chains previews of combined
  ``--set``/``--configure`` invocations in their historical execution order;
- the redaction-aware leaf renderer used for every previewed value;
- the stable wording for absent values and the final no-changes summary.

Raw secret-bearing inputs must never be stored in plan records or rendered
through any path other than :func:`render_preview_value`, which redacts based
on the preference-path classification in :mod:`meshtastic.cli.preference_runtime`.
This module is internal to the CLI package and must not be re-exported from
the public ``meshtastic`` API.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from google.protobuf.descriptor import FieldDescriptor

from meshtastic.cli.preference_runtime import REDACTED_PREF_VALUE, is_secret_pref

PREVIEW_CURRENT_NOT_SET = "not set"
PREVIEW_NO_CHANGES_MESSAGE = "Dry run complete. No changes were written to the device."


@dataclass(slots=True)
class ConfigSnapshotCopies:
    """Mutable protobuf copies that accumulate previewed operations in order.

    A dry-run preview never mutates the live cached configuration messages.
    Instead it copies ``localConfig``/``moduleConfig`` once and applies the
    previewed assignments to the copies. Combined ``--set`` plus ``--configure``
    previews pass the same copies along so the second action validates against
    the state the first action would have produced, matching the historical
    set-then-configure execution order without any device write.

    Attributes
    ----------
    local_config : Any
        Copy of the target node's ``localConfig`` message.
    module_config : Any
        Copy of the target node's ``moduleConfig`` message.
    """

    local_config: Any
    module_config: Any

    @classmethod
    def from_node(cls, node: Any) -> ConfigSnapshotCopies:
        """Snapshot a node's cached configuration messages as private copies.

        Parameters
        ----------
        node : Any
            Node exposing ``localConfig`` and ``moduleConfig`` protobuf messages.

        Returns
        -------
        ConfigSnapshotCopies
            Deep protobuf copies that are safe to mutate during previews.
        """
        local_copy = type(node.localConfig)()
        local_copy.CopyFrom(node.localConfig)
        module_copy = type(node.moduleConfig)()
        module_copy.CopyFrom(node.moduleConfig)
        return cls(local_config=local_copy, module_config=module_copy)


def preview_requested(args: Any) -> bool:
    """Return whether parsed CLI arguments request a dry-run preview.

    Parameters
    ----------
    args : Any
        Parsed CLI arguments; namespaces built without argparse defaults
        (for example focused test doubles) report no preview.

    Returns
    -------
    bool
        Whether ``dry_run`` was set on the invocation.
    """
    return bool(getattr(args, "dry_run", False))


def render_preview_value(
    pref_name: str, field: FieldDescriptor | None, value: Any
) -> str:
    """Render one previewed leaf value, redacting secret-bearing paths.

    Parameters
    ----------
    pref_name : str
        Canonical preference path used for secret classification.
    field : FieldDescriptor | None
        Descriptor of the rendered field when known; enables enum-name and
        nested-message rendering. ``None`` falls back to scalar rendering.
    value : Any
        Value read back from a preview copy or live cached message.

    Returns
    -------
    str
        Human-readable rendering safe for CLI output.
    """
    if is_secret_pref(pref_name):
        return REDACTED_PREF_VALUE
    return _render_leaf(field, value)


def _render_leaf(field: FieldDescriptor | None, value: Any) -> str:
    """Render one protobuf leaf, list, or nested message without secrets."""
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_render_leaf(field, item) for item in value) + "]"
    if hasattr(value, "DESCRIPTOR"):
        return _render_message(value)
    if field is not None and field.enum_type is not None:
        enum_value = field.enum_type.values_by_number.get(value)
        return enum_value.name if enum_value is not None else str(value)
    return str(value)


def _render_message(message: Any) -> str:
    """Render a nested protobuf message as a compact field mapping."""
    parts: list[str] = []
    for field, value in message.ListFields():
        rendered = render_preview_value(field.name, field, value)
        parts.append(f"{field.name}: {rendered}")
    return "{" + ", ".join(parts) + "}" if parts else "{}"
