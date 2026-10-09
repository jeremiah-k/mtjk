"""Node data processing utilities for filtering, sorting, and extraction."""

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


def get_default_show_fields() -> list[str]:
    """Return the default list of fields to display in showNodes output."""
    return DEFAULT_SHOW_FIELDS.copy()


def filter_nodes(
    nodes: list[dict[str, Any]],
    include_self: bool,
    local_node_num: int,
) -> list[dict[str, Any]]:
    """Filter nodes based on include_self option.

    Parameters
    ----------
    nodes : list[dict[str, Any]]
        List of node dictionaries.
    include_self : bool
        If False, filter out the local node.
    local_node_num : int
        The local node's number for comparison.

    Returns
    -------
    list[dict[str, Any]]
        Filtered list of nodes.
    """
    if include_self:
        return list(nodes)
    return [node for node in nodes if node.get("num") != local_node_num]


def sort_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort nodes by lastHeard timestamp in descending order.

    Parameters
    ----------
    nodes : list[dict[str, Any]]
        List of node dictionaries.

    Returns
    -------
    list[dict[str, Any]]
        Sorted list of nodes (newest first).
    """
    return sorted(
        nodes,
        key=lambda r: r.get("lastHeard") or 0,
        reverse=True,
    )


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
