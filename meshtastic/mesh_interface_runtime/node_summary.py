"""At-a-glance summary rendering for the local node and mesh (--summary view).

This module formats already-snapshotted state; NodeView.show_summary() owns the
state snapshotting and output routing. Keeping the formatting pure over plain
data makes the sections independently testable without a live interface.
"""

import logging
from typing import Any, Protocol, cast

from tabulate import tabulate

from meshtastic.protobuf import (
    channel_pb2,
    config_pb2,
    localonly_pb2,
    mesh_pb2,
)
from meshtastic.util import pskToString

from . import node_data, node_presentation
from .node_presentation import (
    _BATTERY_LEVEL_POWERED_SENTINELS,
    _format_numeric_value,
)

logger = logging.getLogger(__name__)


class _SummaryLocalNode(Protocol):
    """Structural view of the local Node state the summary sections read."""

    nodeNum: int
    localConfig: localonly_pb2.LocalConfig
    channels: list[channel_pb2.Channel] | None


# Columns for the recent-nodes table in the Mesh section. Mirrors the default
# --nodes columns minus hardware and role, keeping the summary compact.
_SUMMARY_NODE_FIELDS: list[str] = [
    "N",
    "user.longName",
    "deviceMetrics.batteryLevel",
    "snr",
    "hopsAway",
    "since",
]

# Nodes shown in the Mesh section's recent-nodes table.
_SUMMARY_NODE_LIMIT = 5

# Interval table for _format_duration() (name, seconds).
_DURATION_INTERVALS = (
    ("year", 60 * 60 * 24 * 365),
    ("month", 60 * 60 * 24 * 30),
    ("day", 60 * 60 * 24),
    ("hour", 60 * 60),
    ("min", 60),
    ("sec", 1),
)


def _format_duration(seconds: int) -> str:
    """Produce a compact human-readable duration such as "3 hours" or "45 mins".

    Parameters
    ----------
    seconds : int
        Elapsed seconds; zero or negative values return "0 secs".

    Returns
    -------
    str
        Compact duration string without an "ago" suffix.
    """
    for name, interval_duration in _DURATION_INTERVALS:
        if seconds < interval_duration:
            continue
        count = seconds // interval_duration
        plural = "s" if count > 1 else ""
        return f"{count} {name}{plural}"

    return "0 secs"


def _region_name(region: int) -> str:
    """Return the LoRa RegionCode name or UNKNOWN(<value>) for unrecognized values.

    Parameters
    ----------
    region : int
        Numeric RegionCode value.

    Returns
    -------
    str
        The region name, or ``"UNKNOWN(<value>)"`` when not defined.
    """
    try:
        return config_pb2.Config.LoRaConfig.RegionCode.Name(
            cast("config_pb2.Config.LoRaConfig.RegionCode.ValueType", region)
        )
    except ValueError:
        return f"UNKNOWN({region})"


def _modem_preset_name(preset: int) -> str:
    """Return the LoRa ModemPreset name or UNKNOWN(<value>) for unrecognized values.

    Parameters
    ----------
    preset : int
        Numeric ModemPreset value.

    Returns
    -------
    str
        The modem preset name, or ``"UNKNOWN(<value>)"`` when not defined.
    """
    try:
        return config_pb2.Config.LoRaConfig.ModemPreset.Name(
            cast("config_pb2.Config.LoRaConfig.ModemPreset.ValueType", preset)
        )
    except ValueError:
        return f"UNKNOWN({preset})"


def _format_battery_level(metrics: dict[str, Any]) -> str | None:
    """Format battery level and voltage from a local node's deviceMetrics dict.

    Parameters
    ----------
    metrics : dict[str, Any]
        The node record's ``deviceMetrics`` dictionary.

    Returns
    -------
    str | None
        ``"Powered"`` for the powered sentinels, ``"84% (4.02 V)"`` when voltage
        is known, ``"84%"`` otherwise, or None when no battery level is known.
    """
    level = metrics.get("batteryLevel")
    if level is None:
        return None
    if level in _BATTERY_LEVEL_POWERED_SENTINELS:
        text = "Powered"
    else:
        text = f"{level:.0f}%"
    voltage = metrics.get("voltage")
    if voltage is not None:
        text += f" ({voltage:.2f} V)"
    return text


def _channel_role_name(role: int) -> str:
    """Return the channel role name or UNKNOWN(<value>) for unrecognized roles."""
    if role in channel_pb2.Channel.Role.values():
        return channel_pb2.Channel.Role.Name(
            cast("channel_pb2.Channel.Role.ValueType", role)
        )
    return f"UNKNOWN({role})"


def _format_my_node_section(
    *,
    local_num: int | None,
    long_name: str | None,
    short_name: str | None,
    my_record: dict[str, Any] | None,
    metadata: mesh_pb2.DeviceMetadata | None,
) -> str:
    """Render the "My Node" key/value table.

    Parameters
    ----------
    local_num : int | None
        Local node number, when known.
    long_name : str | None
        Local user long name.
    short_name : str | None
        Local user short name.
    my_record : dict[str, Any] | None
        Local node's record from the node database, when present.
    metadata : mesh_pb2.DeviceMetadata | None
        Device metadata received from the firmware, when present.

    Returns
    -------
    str
        Section header plus two-column table, or an empty string when no rows apply.
    """
    rows: list[tuple[str, str]] = []

    name = long_name or "Unknown"
    if short_name:
        name = f"{name} ({short_name})"
    rows.append(("Name", name))

    if local_num is not None:
        rows.append(("Node", f"!{local_num:08x}"))

    if metadata is not None and metadata.firmware_version:
        rows.append(("Firmware", metadata.firmware_version))

    user: dict[str, Any] = (my_record or {}).get("user") or {}
    hardware = user.get("hwModel")
    if hardware and hardware != "UNSET":
        rows.append(("Hardware", str(hardware)))
    role = user.get("role")
    if role:
        rows.append(("Role", str(role)))

    metrics: dict[str, Any] = (my_record or {}).get("deviceMetrics") or {}
    battery = _format_battery_level(metrics)
    if battery is not None:
        rows.append(("Battery", battery))
    uptime_seconds = metrics.get("uptimeSeconds")
    if uptime_seconds:
        rows.append(("Uptime", _format_duration(int(uptime_seconds))))

    # ble_address is newer than some deployed DeviceMetadata producers; treat a
    # missing attribute as an empty value so mocked or older metadata still renders.
    ble_address = getattr(metadata, "ble_address", "") if metadata else ""
    if ble_address:
        rows.append(("BLE address", ble_address))

    return "My Node\n" + str(tabulate(rows, tablefmt="fancy_grid"))


def _format_radio_section(
    *,
    local_node: _SummaryLocalNode,
    my_record: dict[str, Any] | None,
) -> str:
    """Render the "Radio" key/value table from the local LoRa config and metrics.

    Parameters
    ----------
    local_node : _SummaryLocalNode
        Local node carrying the cached LocalConfig.
    my_record : dict[str, Any] | None
        Local node's record from the node database, for utilization metrics.

    Returns
    -------
    str
        Section header plus two-column table, or an empty string when no LoRa
        config has been received yet.
    """
    config = local_node.localConfig
    if not config.HasField("lora"):
        return ""
    lora = config.lora

    rows: list[tuple[str, str]] = []
    if lora.region:
        rows.append(("Region", _region_name(lora.region)))
    rows.append(("Preset", _modem_preset_name(lora.modem_preset)))
    if lora.hop_limit:
        rows.append(("Hop limit", str(lora.hop_limit)))
    if lora.tx_power:
        rows.append(("TX power", f"{lora.tx_power} dBm"))

    metrics: dict[str, Any] = (my_record or {}).get("deviceMetrics") or {}
    channel_util = _format_numeric_value(metrics.get("channelUtilization"), 2, "%")
    if channel_util is not None:
        rows.append(("Channel util", channel_util))
    air_util = _format_numeric_value(metrics.get("airUtilTx"), 2, "%")
    if air_util is not None:
        rows.append(("Tx air util", air_util))

    return "Radio\n" + str(tabulate(rows, tablefmt="fancy_grid"))


def _format_mesh_section(node_records: list[dict[str, Any]]) -> str:
    """Render the "Mesh" header line and a recent-nodes table.

    Parameters
    ----------
    node_records : list[dict[str, Any]]
        Snapshot of every known node record (including the local node).

    Returns
    -------
    str
        Header line ``"Mesh: <total> known, <direct> direct"`` followed by a
        table of the most recently heard nodes.
    """
    total = len(node_records)
    header = f"Mesh: {total} known"
    if total == 0:
        return header

    direct = sum(1 for node in node_records if node.get("hopsAway") == 0)
    header += f", {direct} direct"

    ordered = node_data.sort_nodes(node_records)
    shown = ordered[:_SUMMARY_NODE_LIMIT]

    rows: list[dict[str, Any]] = []
    for index, node in enumerate(shown, start=1):
        row: dict[str, Any] = {"N": index}
        for col_name in _SUMMARY_NODE_FIELDS[1:]:
            if col_name == "since":
                raw_value: Any = node.get("lastHeard")
            elif "." in col_name:
                raw_value = node_data.extract_node_field_value(node, col_name)
            else:
                raw_value = node.get(col_name)
            label = node_presentation._get_human_readable_column_label(col_name)
            row[label] = node_presentation._format_node_field(col_name, raw_value, node)
        rows.append(row)

    table = str(tabulate(rows, headers="keys", missingval="–", tablefmt="fancy_grid"))
    section = f"{header}\n{table}"
    hidden = total - len(shown)
    if hidden > 0:
        section += f"\n… and {hidden} more not shown"
    return section


def _format_channels_section(local_node: _SummaryLocalNode) -> str:
    """Render the "Channels" table of active channels.

    Parameters
    ----------
    local_node : _SummaryLocalNode
        Local node carrying the cached channel list.

    Returns
    -------
    str
        Section header plus table, or an empty string when no channels are
        cached or every channel is disabled.
    """
    channels = local_node.channels or []
    rows: list[tuple[int, str, str, str]] = []
    for channel in channels:
        if channel.role == channel_pb2.Channel.Role.DISABLED:
            continue
        name = channel.settings.name if channel.settings else ""
        rows.append(
            (
                channel.index,
                name or "(unnamed)",
                _channel_role_name(channel.role),
                pskToString(channel.settings.psk if channel.settings else b""),
            )
        )
    if not rows:
        return ""

    table = str(
        tabulate(
            rows,
            headers=["Index", "Name", "Role", "Security"],
            tablefmt="fancy_grid",
        )
    )
    return "Channels\n" + table


def _format_summary(
    *,
    local_node: _SummaryLocalNode,
    local_num: int | None,
    long_name: str | None,
    short_name: str | None,
    node_records: list[dict[str, Any]],
    metadata: mesh_pb2.DeviceMetadata | None,
) -> str:
    """Compose the full at-a-glance summary from snapshotted state.

    Parameters
    ----------
    local_node : _SummaryLocalNode
        Local node carrying the cached LocalConfig and channel list.
    local_num : int | None
        Local node number, when known.
    long_name : str | None
        Local user long name.
    short_name : str | None
        Local user short name.
    node_records : list[dict[str, Any]]
        Snapshot of every known node record (including the local node).
    metadata : mesh_pb2.DeviceMetadata | None
        Device metadata received from the firmware, when present.

    Returns
    -------
    str
        The rendered summary with sections separated by blank lines.
    """
    my_record = next(
        (node for node in node_records if node.get("num") == local_num), None
    )
    sections = (
        _format_my_node_section(
            local_num=local_num,
            long_name=long_name,
            short_name=short_name,
            my_record=my_record,
            metadata=metadata,
        ),
        _format_radio_section(local_node=local_node, my_record=my_record),
        _format_mesh_section(node_records),
        _format_channels_section(local_node),
    )
    return "\n\n".join(section for section in sections if section)
