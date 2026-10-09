"""Node data processing utilities for filtering, sorting, and extraction."""

import math
from typing import Any

from google.protobuf.descriptor import Descriptor

from meshtastic.protobuf import mesh_pb2, telemetry_pb2


def extract_node_field_value(node_dict: dict[str, Any], field_path: str) -> Any:
    """Retrieve a nested value from a dictionary using a dotted key path.

    Parameters
    ----------
    node_dict : dict[str, Any]
        Dictionary to traverse.
    field_path : str
        Dotted path (e.g., "a.b.c"). Non-dotted paths are treated
        as a single-level lookup on node_dict.

    Returns
    -------
    Any
        The value found at the given path, or `None` if any intermediate
        key is missing or an intermediate value is not a dictionary.
    """
    if not isinstance(node_dict, dict):
        return None
    if "." not in field_path:
        return node_dict.get(field_path)
    keys = field_path.split(".")
    value: Any = node_dict
    for key in keys:
        if isinstance(value, dict):
            value = value.get(key)
        else:
            return None
    return value


DEFAULT_SHOW_FIELDS: list[str] = [
    "N",
    "user.longName",
    "user.hwModel",
    "user.role",
    "deviceMetrics.batteryLevel",
    "snr",
    "hopsAway",
    "since",
]

# Friendly single-word aliases accepted where a node field is requested
# (--sort); canonical dotted paths are always accepted as-is.
FIELD_ALIASES: dict[str, str] = {
    "name": "user.longName",
    "id": "user.id",
    "aka": "user.shortName",
    "hwmodel": "user.hwModel",
    "role": "user.role",
    "battery": "deviceMetrics.batteryLevel",
    "snr": "snr",
    "hops": "hopsAway",
    "channel": "channel",
    "favorite": "isFavorite",
    "last_seen": "lastHeard",
    "lastheard": "lastHeard",
    "since": "lastHeard",
}

# Paths whose values compare numerically when sorting; every other field
# sorts as case-insensitive text. Numeric fields default high-to-low
# (newest or best first), text fields default A-to-Z.
NUMERIC_FIELD_PATHS: frozenset[str] = frozenset(
    {
        "num",
        "snr",
        "hopsAway",
        "lastHeard",
        "channel",
        "deviceMetrics.batteryLevel",
        "deviceMetrics.voltage",
        "deviceMetrics.channelUtilization",
        "deviceMetrics.airUtilTx",
        "deviceMetrics.uptimeSeconds",
        "position.latitude",
        "position.longitude",
        "position.altitude",
    }
)


def _resolve_field_alias(token: str) -> str:
    """Map a friendly field alias to its canonical dotted path.

    Parameters
    ----------
    token : str
        Field name as typed (aliases are matched case-insensitively; dotted
        paths keep their spelling, since protobuf keys are camelCase).

    Returns
    -------
    str
        The canonical dotted field path.
    """
    return FIELD_ALIASES.get(token.lower(), token)


def get_default_show_fields() -> list[str]:
    """Return the default list of fields to display in showNodes output."""
    return DEFAULT_SHOW_FIELDS.copy()


def _matches_any_substring(value: Any, patterns: list[str]) -> bool:
    """Check a stored value against case-insensitive substring patterns."""
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return False
    return any(pattern in str(value).casefold() for pattern in patterns)


def filter_nodes(
    nodes: list[dict[str, Any]],
    include_self: bool,
    local_node_num: int,
    role_patterns: list[str] | None = None,
    hwmodel_patterns: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Filter nodes based on include_self option and optional field patterns.

    Parameters
    ----------
    nodes : list[dict[str, Any]]
        List of node dictionaries.
    include_self : bool
        If False, filter out the local node.
    local_node_num : int
        The local node's number for comparison.
    role_patterns : list[str] | None
        Case-insensitive substrings; a node matches when ``user.role``
        contains any of them (comma values mean any-of).
    hwmodel_patterns : list[str] | None
        Case-insensitive substrings matched against ``user.hwModel`` the
        same way. Role and hwmodel filters combine with AND.

    Returns
    -------
    list[dict[str, Any]]
        Filtered list of nodes.
    """
    if include_self:
        result = list(nodes)
    else:
        result = [node for node in nodes if node.get("num") != local_node_num]
    role_patterns = [
        pattern.strip().casefold() for pattern in role_patterns or [] if pattern.strip()
    ]
    hwmodel_patterns = [
        pattern.strip().casefold()
        for pattern in hwmodel_patterns or []
        if pattern.strip()
    ]
    if role_patterns:
        result = [
            node
            for node in result
            if _matches_any_substring(
                extract_node_field_value(node, "user.role"), role_patterns
            )
        ]
    if hwmodel_patterns:
        result = [
            node
            for node in result
            if _matches_any_substring(
                extract_node_field_value(node, "user.hwModel"), hwmodel_patterns
            )
        ]
    return result


def _field_sort_key(
    path: str, node: dict[str, Any], *, numeric: bool = False
) -> tuple[int, int, Any] | None:
    """Sort key for one node; None places the node after every valued node.

    Keys compare within numeric (0, 0, number) and text (0, 1, casefolded)
    buckets so a stray string value in a numeric field cannot crash the sort.
    """
    value = extract_node_field_value(node, path)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    numeric = numeric or path in NUMERIC_FIELD_PATHS
    if isinstance(value, bool) and numeric:
        return None
    if numeric:
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return (0, 0, number) if math.isfinite(number) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return (0, 1, str(value).casefold())
    number = float(value)
    return (0, 0, number) if math.isfinite(number) else None


def sort_nodes(
    nodes: list[dict[str, Any]],
    field: str | None = None,
    direction: str | None = None,
) -> list[dict[str, Any]]:
    """Sort nodes by a field, newest first by default.

    Parameters
    ----------
    nodes : list[dict[str, Any]]
        List of node dictionaries.
    field : str | None
        Dotted field path or friendly alias to sort by; None keeps the
        historical lastHeard-descending order.
    direction : str | None
        "asc" or "desc"; None picks the natural default for the field
        (numeric fields high-to-low, text fields A-to-Z). Missing values
        always sort last regardless of direction.

    Returns
    -------
    list[dict[str, Any]]
        Sorted list of nodes.
    """
    if field is None:
        return sorted(
            nodes,
            key=lambda r: r.get("lastHeard") or 0,
            reverse=True,
        )
    path = _resolve_field_alias(field)
    numeric = path in NUMERIC_FIELD_PATHS or any(
        isinstance(value := extract_node_field_value(node, path), (int, float))
        and not isinstance(value, bool)
        for node in nodes
    )
    keyed: list[tuple[tuple[int, int, Any], dict[str, Any]]] = []
    missing: list[dict[str, Any]] = []
    for node in nodes:
        key = _field_sort_key(path, node, numeric=numeric)
        if key is None:
            missing.append(node)
        else:
            keyed.append((key, node))
    if direction is None:
        direction = "desc" if numeric else "asc"
    keyed.sort(key=lambda pair: pair[0], reverse=direction == "desc")
    return [node for _key, node in keyed] + missing


def _descriptor_field_paths(descriptor: Descriptor, prefix: str = "") -> set[str]:
    """Return JSON-style dotted paths reachable from one protobuf descriptor."""
    paths: set[str] = set()
    for field in descriptor.fields:
        path = f"{prefix}.{field.json_name}" if prefix else field.json_name
        paths.add(path)
        if (
            field.message_type is not None
            and not field.message_type.GetOptions().map_entry
        ):
            paths.update(_descriptor_field_paths(field.message_type, path))
    return paths


def get_known_field_paths(nodes: list[dict[str, Any]] | None = None) -> list[str]:
    """Return known CLI node-table field paths from schema plus observed node data."""
    paths: set[str] = set(DEFAULT_SHOW_FIELDS)
    paths.update(_descriptor_field_paths(mesh_pb2.NodeInfo.DESCRIPTOR))

    for telemetry_field in telemetry_pb2.Telemetry.DESCRIPTOR.fields:
        if telemetry_field.message_type is None:
            continue
        paths.add(telemetry_field.json_name)
        paths.update(
            _descriptor_field_paths(
                telemetry_field.message_type,
                telemetry_field.json_name,
            )
        )

    # These are synthesized by presentation/runtime logic rather than represented
    # directly in NodeInfo's protobuf descriptor.
    paths.update({"N", "since", "position.latitude", "position.longitude"})

    def _walk_observed(value: Any, prefix: str = "") -> None:
        if not isinstance(value, dict):
            return
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            paths.add(path)
            _walk_observed(child, path)

    for node in nodes or []:
        _walk_observed(node)

    return sorted(paths)
