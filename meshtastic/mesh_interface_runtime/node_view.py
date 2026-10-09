"""NodeView - node accessor and presentation methods for MeshInterface."""

import base64
import copy
import json
import logging
import sys
import threading
from typing import IO, Any, TypeAlias

try:
    import print_color  # type: ignore[import-untyped]
except ImportError:
    print_color = None

from pubsub import pub
from tabulate import tabulate

import meshtastic.node
from meshtastic._core_constants import BROADCAST_ADDR, BROADCAST_NUM, LOCAL_ADDR
from meshtastic.protobuf import mesh_pb2
from meshtastic.util import (
    convert_mac_addr,
    messageToJson,
    remove_keys_from_dict,
)

from . import node_data, node_presentation
from .ports import _NodeViewPort

JSONValue: TypeAlias = (
    None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]
)

logger = logging.getLogger(__name__)

# Time intervals for _timeago() function (name, seconds)
_TIMEAGO_INTERVALS = (
    ("year", 60 * 60 * 24 * 365),
    ("month", 60 * 60 * 24 * 30),
    ("day", 60 * 60 * 24),
    ("hour", 60 * 60),
    ("min", 60),
    ("sec", 1),
)


def _timeago(delta_secs: int) -> str:
    """Produce a short human-readable relative time string for a past interval.

    Parameters
    ----------
    delta_secs : int
        Number of seconds elapsed in the past; zero or negative values are treated as "now".

    Returns
    -------
    str
        A compact relative time string such as "now", "30 sec ago", "1 hour ago", or "2 days ago".
    """
    for name, interval_duration in _TIMEAGO_INTERVALS:
        if delta_secs < interval_duration:
            continue
        x = delta_secs // interval_duration
        plur = "s" if x > 1 else ""
        return f"{x} {name}{plur} ago"

    return "now"


def _normalize_json_serializable(value: object) -> JSONValue:
    """Recursively normalize common non-JSON-native values into JSON-safe forms."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "base64:" + base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, dict):
        return {
            str(key): _normalize_json_serializable(inner_value)
            for key, inner_value in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_normalize_json_serializable(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


class NodeView:
    """Node accessor and presentation methods for MeshInterface.

    This class provides node lookup, node DB helpers, and presentation/display
    methods (show_info, show_nodes). Shared interface state is accessed through a
    narrow internal capability port.
    """

    def __init__(self, port: _NodeViewPort) -> None:
        """Initialize the node view with its interface capability port.

        Parameters
        ----------
        port : _NodeViewPort
            Narrow access to node state and facade compatibility seams.
        """
        self._port = port

    @property
    def _node_db_lock(self) -> threading.RLock:
        return self._port.node_db_lock

    @property
    def local_node(self) -> meshtastic.node.Node:
        """Return the local node for this interface."""
        return self._port.local_node

    @property
    def nodes(self) -> dict[str, dict[str, Any]] | None:
        """Return the node info dictionary, or None if not initialized."""
        return self._port.nodes

    @property
    def nodes_by_num(self) -> dict[int, dict[str, Any]] | None:
        """Return the node-number-to-info dictionary, or None if not initialized."""
        return self._port.nodes_by_num

    @property
    def my_info(self) -> mesh_pb2.MyNodeInfo | None:
        """Return the MyNodeInfo for this interface, or None."""
        return self._port.my_info

    @property
    def metadata(self) -> mesh_pb2.DeviceMetadata | None:
        """Return device metadata, or None if not yet received."""
        return self._port.metadata

    def _print_log_line(self, line: str) -> None:
        """Print a formatted device log line to the configured debug output.

        Parameters
        ----------
        line : str
            The raw log text to print.
        """
        debug_out = self._port.debug_out
        if print_color is not None and debug_out == sys.stdout:
            if "DEBUG" in line:
                print_color.print(line, color="cyan")
            elif "INFO" in line:
                print_color.print(line, color="white")
            elif "WARN" in line:
                print_color.print(line, color="yellow")
            elif "ERR" in line:
                print_color.print(line, color="red")
            else:
                print_color.print(line)
        elif callable(debug_out):
            debug_out(line)
        elif debug_out is not None and hasattr(debug_out, "write"):
            debug_out.write(line + "\n")

    def _handle_log_line(self, line: str) -> None:
        """Publish a device log line to the "meshtastic.log.line" topic, normalizing any trailing newline.

        Parameters
        ----------
        line : str
            Log text received from the device; a trailing newline (if present) is removed before publishing.
        """
        if line.endswith("\n"):
            line = line[:-1]

        pub.sendMessage("meshtastic.log.line", line=line, interface=self._port.facade)

    def _handle_log_record(self, record: mesh_pb2.LogRecord) -> None:
        """Process a protobuf LogRecord by extracting its message text and handling it as a device log line.

        Parameters
        ----------
        record : mesh_pb2.LogRecord
            Protobuf log record containing the `message` field to be processed.
        """
        self._handle_log_line(record.message)

    def show_info(self, file: IO[str] | None = None) -> str:
        """Return a human-readable JSON summary of the mesh interface including owner, local node info, metadata, and known nodes.

        The summary omits internal node fields (`raw`, `decoded`, `payload`) and normalizes stored MAC addresses
        to a human-readable form before formatting.

        Parameters
        ----------
        file : IO[str] | None
            File-like object to which the summary is written; defaults to sys.stdout.

        Returns
        -------
        summary : str
            The formatted summary text that was written to `file`.
        """
        owner = f"Owner: {self.get_long_name()} ({self.get_short_name()})"
        with self._node_db_lock:
            my_info = self.my_info
            metadata_info = self.metadata
            nodes_snapshot = (
                [copy.deepcopy(node) for node in self.nodes.values()]
                if self.nodes
                else []
            )
        myinfo = ""
        if my_info:
            myinfo = f"\nMy info: {messageToJson(my_info)}"
        metadata = ""
        if metadata_info:
            metadata = f"\nMetadata: {messageToJson(metadata_info)}"
        mesh = "\n\nNodes in mesh: "
        nodes: dict[str, JSONValue] = {}
        for n in nodes_snapshot:
            keys_to_remove = ("raw", "decoded", "payload")
            n2 = remove_keys_from_dict(keys_to_remove, n)

            user = n2.get("user")
            if not isinstance(user, dict):
                continue

            val = user.get("macaddr")
            if isinstance(val, str):
                try:
                    user["macaddr"] = convert_mac_addr(val)
                except (TypeError, ValueError):
                    logger.debug(
                        "Skipping malformed macaddr for node %s",
                        user.get("id"),
                    )

            node_id = user.get("id")
            if node_id is not None:
                nodes[str(node_id)] = _normalize_json_serializable(n2)
        infos = owner + myinfo + metadata + mesh + str(json.dumps(nodes, indent=2))
        if file is None:
            file = sys.stdout
        print(infos, file=file)
        return infos

    def _build_table_data(
        self,
        nodes: list[dict[str, Any]],
        fields: list[str],
    ) -> list[dict[str, Any]]:
        """Build table data rows from nodes and field specifications.

        Parameters
        ----------
        nodes : list[dict[str, Any]]
            List of node dictionaries.
        fields : list[str]
            List of field paths to include.

        Returns
        -------
        list[dict[str, Any]]
            List of row dictionaries with formatted values.
        """
        rows: list[dict[str, Any]] = []
        for node in nodes:
            fields_data: dict[str, Any] = {}
            for col_name in fields:
                if "." in col_name:
                    raw_value = node_data.extract_node_field_value(node, col_name)
                elif col_name == "since":
                    raw_value = node.get("lastHeard")
                else:
                    raw_value = node.get(col_name)

                formatted_value = node_presentation._format_node_field(
                    col_name, raw_value, node
                )
                fields_data[col_name] = formatted_value

            filtered_data = {
                node_presentation._get_human_readable_column_label(k): v
                for k, v in fields_data.items()
                if k in fields
            }
            rows.append(filtered_data)

        return rows

    @staticmethod
    def _render_node_table(rows: list[dict[str, Any]]) -> str:
        """Render a formatted table from row data.

        Parameters
        ----------
        rows : list[dict[str, Any]]
            List of row dictionaries.

        Returns
        -------
        str
            The rendered table string using tabulate.
        """
        return str(
            tabulate(rows, headers="keys", missingval="–", tablefmt="fancy_grid")
        )

    def show_nodes(
        self,
        includeSelf: bool = True,
        showFields: list[str] | None = None,
        *,
        roleFilter: list[str] | None = None,
        hwModelFilter: list[str] | None = None,
        sortField: str | None = None,
        sortDirection: str | None = None,
        limit: int = 0,
    ) -> str:
        """Produce a formatted table summarizing known mesh nodes.

        Parameters
        ----------
        includeSelf : bool
            If False, omit the local node from the output. (Default value = True)
        showFields : list[str] | None
            Ordered list of node fields to
            include (dotted paths for nested fields). If omitted or empty,
            a sensible default set of fields is used; the row-number column
            "N" is always included.
        roleFilter : list[str] | None
            Case-insensitive substrings; a node is listed when ``user.role``
            contains any of them (comma values mean any-of).
        hwModelFilter : list[str] | None
            Case-insensitive substrings matched against ``user.hwModel`` the
            same way. Role and hwmodel filters combine with AND.
        sortField : str | None
            Dotted field path or friendly alias to sort by; None keeps the
            historical lastHeard-descending order.
        sortDirection : str | None
            "asc" or "desc"; None picks the natural default for the field
            (numeric fields high-to-low, text fields A-to-Z). Missing values
            always sort last.
        limit : int
            Show at most this many nodes; 0 lists every node. (Default value = 0)

        Returns
        -------
        table : str
            The rendered node-count line, table, and truncation footer
            (also printed to stdout) with columns mapped to human-readable
            headings.
        """
        # Determine fields to show
        if not showFields:
            fields = node_data.get_default_show_fields()
        else:
            fields = ["N", *showFields] if "N" not in showFields else list(showFields)

        # Get node data under lock
        with self._node_db_lock:
            nodes_snapshot = (
                list(self.nodes_by_num.values()) if self.nodes_by_num else []
            )
            local_node_num = self.local_node.nodeNum

        if nodes_snapshot:
            node_count = len(nodes_snapshot)

            # Log only count and trimmed nodeNum list for privacy/safety
            def _get_num(n: Any) -> int | None:
                return getattr(n, "nodeNum", None) or (
                    n.get("num") if isinstance(n, dict) else None
                )

            sample_nums = [_get_num(n) for n in nodes_snapshot[:3]]
            logger.debug(
                "Node database: %d nodes, sample nodeNums: %s%s",
                node_count,
                sample_nums,
                "..." if node_count > 3 else "",
            )

        # Filter nodes
        total = sum(
            includeSelf or node.get("num") != local_node_num for node in nodes_snapshot
        )
        filtered_nodes = node_data.filter_nodes(
            nodes_snapshot,
            includeSelf,
            local_node_num,
            role_patterns=roleFilter,
            hwmodel_patterns=hwModelFilter,
        )
        has_field_filters = bool(roleFilter) or bool(hwModelFilter)

        # Sort nodes (lastHeard-descending unless a field is requested)
        sorted_nodes = node_data.sort_nodes(
            filtered_nodes, field=sortField, direction=sortDirection
        )

        # Apply the listing cap
        shown_nodes = sorted_nodes if limit <= 0 else sorted_nodes[:limit]

        # Build table data with field extraction and formatting
        rows = self._build_table_data(shown_nodes, fields)

        # Add row numbers
        for i, row in enumerate(rows):
            row["N"] = i + 1

        # Node-count header mirrors the MMRelay !nodes grammar
        if has_field_filters:
            if len(shown_nodes) < len(sorted_nodes):
                header = f"Nodes: {len(shown_nodes)} of {len(sorted_nodes)} matching"
            else:
                header = f"Nodes: {len(sorted_nodes)} matching"
            if len(sorted_nodes) < total:
                header += f" (of {total} known)"
        elif len(shown_nodes) < total:
            header = f"Nodes: {len(shown_nodes)} of {total}"
        else:
            header = f"Nodes: {total}"

        # Render and output table
        table = self._render_node_table(rows)
        output = header + "\n" + table
        hidden = len(sorted_nodes) - len(shown_nodes)
        if hidden > 0:
            output += f"\n… and {hidden} more not shown"
        print(output)
        return output

    def get_node(
        self,
        nodeId: str,
        requestChannels: bool = True,
        requestChannelAttempts: int = 3,
        timeout: float = 300.0,
    ) -> meshtastic.node.Node:
        """Get the Node object for the given node identifier.

        If nodeId is the local or broadcast address, return the already-initialized local node.
        If requestChannels is True, request channel information from the remote node and retry up to
        requestChannelAttempts until channel data is received or the operation times out.
        Raises MeshInterfaceError if channel retrieval repeatedly fails.

        Parameters
        ----------
        nodeId : str
            Node identifier (hex/node-id string, or LOCAL_ADDR/BROADCAST_ADDR).
        requestChannels : bool
            If True, request channel settings from the remote node. (Default value = True)
        requestChannelAttempts : int
            Number of attempts to retrieve channel info before giving up. (Default value = 3)
        timeout : float
            Timeout in seconds passed to the Node constructor and used while waiting for responses. (Default value = 300.0)

        Returns
        -------
        meshtastic.node.Node
            The Node object corresponding to nodeId.

        Raises
        ------
        MeshInterfaceError
            If channel retrieval repeatedly fails or times out.
        """
        if nodeId in (LOCAL_ADDR, BROADCAST_ADDR):
            return self.local_node
        n = meshtastic.node.Node(
            self._port.facade,
            nodeId,
            timeout=timeout,
            noProto=self._port.no_proto,
        )
        if requestChannels:
            logger.debug("About to requestChannels")
            n.requestChannels()
            retries_left = requestChannelAttempts
            last_index: int = 0
            while retries_left > 0:
                retries_left -= 1
                if not n.waitForConfig():
                    new_index: int = len(n.partialChannels) if n.partialChannels else 0
                    if new_index != last_index:
                        retries_left = requestChannelAttempts - 1
                    if retries_left <= 0:
                        raise self._port.error_type(
                            "Error: Timed out waiting for channels, giving up"
                        )
                    logger.warning(
                        "Timed out trying to retrieve channel info, retrying"
                    )
                    n.requestChannels(startingIndex=new_index)
                    last_index = new_index
                else:
                    break
        return n

    def get_my_node_info(self) -> dict[str, Any] | None:
        """Get the stored node-info dictionary for the local node.

        Returns
        -------
        dict[str, Any] | None
            The local node's node-info entry from `nodes_by_num`, or `None` if `my_info`
            or `nodes_by_num` is unset or the local node entry is missing.
        """
        with self._node_db_lock:
            if self.my_info is None or self.nodes_by_num is None:
                return None
            return self.nodes_by_num.get(self.my_info.my_node_num)

    def get_my_user(self) -> dict[str, Any] | None:
        """Get the user information for the local node.

        Returns
        -------
        user : dict[str, Any] | None
            The local node's `user` dictionary, or `None` if no local node info or no `user` field is present.
        """
        nodeInfo = self.get_my_node_info()
        if nodeInfo is not None:
            return nodeInfo.get("user")
        return None

    def get_long_name(self) -> str | None:
        """Get the local user's configured long name.

        Returns
        -------
        str | None
            The long name string if configured, `None` otherwise.
        """
        user = self.get_my_user()
        if user is not None:
            return user.get("longName")
        return None

    def get_short_name(self) -> str | None:
        """Get the local node user's short name.

        Returns
        -------
        str | None
            The user's `shortName` if present, `None` otherwise.
        """
        user = self.get_my_user()
        if user is not None:
            return user.get("shortName")
        return None

    def get_public_key(self) -> bytes | None:
        """Return the local node's public key if available.

        Returns
        -------
        bytes | None
            The local node's public key bytes if present, `None` otherwise.
        """
        user = self.get_my_user()
        if user is not None:
            return user.get("publicKey")
        return None

    def get_canned_message(self) -> str | None:
        """Retrieve the canned (predefined) message configured for the local node.

        Returns
        -------
        str | None
            The canned message text, or `None` if there is no local node or no canned message configured.
        """
        node = self.local_node
        if node is not None:
            return node.get_canned_message()
        return None

    def get_ringtone(self) -> str | None:
        """Get the local node's ringtone name or identifier.

        Returns
        -------
        str | None
            The ringtone name or identifier as a string, or None if the local node or ringtone is unavailable.
        """
        node = self.local_node
        if node is not None:
            return node.get_ringtone()
        return None

    def _fixup_position(self, position: dict[str, Any]) -> dict[str, Any]:
        """Convert integer micro-degree coordinates in a position dict to floating-point degrees.

        If present, 'latitudeI' and 'longitudeI' are converted to 'latitude' and 'longitude'
        by multiplying by 1e-7 (micro-degrees -> degrees) and stored back into the same dict.

        Parameters
        ----------
        position : dict[str, Any]
            Position dictionary that may contain integer keys 'latitudeI' and 'longitudeI'.

        Returns
        -------
        dict[str, Any]
            The same position dictionary with 'latitude' and/or 'longitude' set to float degrees when corresponding integer fields were present.
        """
        if "latitudeI" in position:
            position["latitude"] = position["latitudeI"] * 1e-7
        if "longitudeI" in position:
            position["longitude"] = position["longitudeI"] * 1e-7
        return position

    def _node_num_to_id(self, num: int, isDest: bool = True) -> str | None:
        """Map a mesh numeric node number to its node ID string or a broadcast/unknown literal.

        If num equals the broadcast numeric constant, returns BROADCAST_ADDR when isDest is True
        or the string "Unknown" when isDest is False. Otherwise looks up and returns the stored
        user ID for that node number.

        Parameters
        ----------
        num : int
            Numeric node identifier.
        isDest : bool
            When True treat the broadcast number as a destination (return
            BROADCAST_ADDR); when False treat it as an unknown source (return "Unknown"). (Default value = True)

        Returns
        -------
        str | None
            The node ID string, BROADCAST_ADDR for broadcast destinations, "Unknown" for
            broadcast sources, or `None` if the node number is not present in the local node map.
        """
        if num == BROADCAST_NUM:
            return BROADCAST_ADDR if isDest else "Unknown"

        with self._node_db_lock:
            nodes = self.nodes_by_num
            if nodes is None:
                logger.debug(
                    "Node database not initialized while resolving node id for %s", num
                )
                return None
            node = nodes.get(num)
            if not isinstance(node, dict):
                logger.debug("Node %s not found for fromId", num)
                return None
            user = node.get("user")
            if not isinstance(user, dict):
                logger.debug("Node %s has no user payload for fromId", num)
                return None
            node_id = user.get("id")
            if not isinstance(node_id, str):
                logger.debug("Node %s user payload has no valid id", num)
                return None
            return node_id

    def _get_or_create_by_num(self, nodeNum: int) -> dict[str, Any]:
        """Retrieve the node record for a numeric node ID, creating a minimal placeholder if none exists.

        Parameters
        ----------
        nodeNum : int
            Numeric node identifier.

        Returns
        -------
        dict[str, Any]
            The node info dictionary stored in self.nodes_by_num for the given nodeNum.

        Raises
        ------
        MeshInterface.MeshInterfaceError
            If nodeNum is the broadcast node number or if the node database has not been initialized.
        """
        if nodeNum == BROADCAST_NUM:
            raise self._port.error_type(
                "Can not create/find nodenum by the broadcast num"
            )

        with self._node_db_lock:
            if self.nodes_by_num is None:
                raise self._port.error_type("Node database not initialized")

            if nodeNum in self.nodes_by_num:
                return self.nodes_by_num[nodeNum]
            presumptive_id = f"!{nodeNum:08x}"
            n = {
                "num": nodeNum,
                "user": {
                    "id": presumptive_id,
                    "longName": f"Meshtastic {presumptive_id[-4:]}",
                    "shortName": f"{presumptive_id[-4:]}",
                    "hwModel": "UNSET",
                },
            }
            self.nodes_by_num[nodeNum] = n
            return n
