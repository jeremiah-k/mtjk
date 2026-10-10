"""Detached node query results for applications and JSON consumers."""

from __future__ import annotations

import base64
import math
from dataclasses import dataclass
from typing import Any, TypeAlias

from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message

__all__ = ("NodeQueryResult",)

JSONValue: TypeAlias = (
    None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]
)
_INTERNAL_NODE_FIELDS = frozenset({"raw", "decoded", "payload", "adminSessionPassKey"})


def _json_value(value: Any) -> JSONValue:
    """Normalize cached node values without formatting measurements for display."""
    if isinstance(value, Message):
        return _json_value(MessageToDict(value))
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "base64:" + base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(f"Cannot encode node value of type {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class NodeQueryResult:
    """A detached, point-in-time view of the client's cached node database.

    Attributes
    ----------
    nodes : tuple[dict[str, Any], ...]
        Full cached node records in query order. Nested dictionaries and
        protobufs belong to this result; modifying them cannot change the
        client's database. Records retain their existing camelCase keys.
    total : int
        Known nodes after the includeSelf selection, before field filters.
    matched : int
        Matching nodes before the limit is applied.
    capturedAt : float
        Unix time in seconds when the database was copied under its lock.

    Notes
    -----
    This result describes cached observations, not a live radio poll. Its
    container is frozen; its detached node dictionaries remain editable.
    """

    nodes: tuple[dict[str, Any], ...]
    total: int
    matched: int
    capturedAt: float

    @property
    def returned(self) -> int:
        """Return the number of records included after limiting."""
        return len(self.nodes)

    @property
    def truncated(self) -> bool:
        """Return whether matching nodes were omitted by the limit."""
        return self.returned < self.matched

    def toDict(self) -> dict[str, JSONValue]:
        """Return an independent JSON-compatible document with schema_version 1.

        Measurements and Unix timestamps keep their raw numeric values.
        Byte values use ``base64:`` followed by standard Base64; protobufs
        follow protobuf JSON conventions. Non-finite floats become null.
        Internal raw/decoded packet payloads and administrative session keys
        are omitted from the JSON document.
        Unsupported cached objects raise TypeError rather than producing an
        unstable object representation.
        """
        return {
            "schema_version": 1,
            "captured_at": self.capturedAt,
            "total": self.total,
            "matched": self.matched,
            "returned": self.returned,
            "truncated": self.truncated,
            "nodes": [
                _json_value(
                    {
                        key: value
                        for key, value in node.items()
                        if key not in _INTERNAL_NODE_FIELDS
                    }
                )
                for node in self.nodes
            ],
        }
