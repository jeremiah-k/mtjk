"""Node presentation and formatting utilities for display output."""

from datetime import datetime, timezone
from typing import Any, TypeAlias

JSONValue: TypeAlias = (
    None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]
)

_BATTERY_LEVEL_POWERED_SENTINELS = (0, 101)

# Time intervals for human-readable relative time formatting (name, seconds)
_TIMEAGO_INTERVALS: tuple[tuple[str, int], ...] = (
    ("year", 60 * 60 * 24 * 365),
    ("month", 60 * 60 * 24 * 30),
    ("day", 60 * 60 * 24),
    ("hour", 60 * 60),
    ("min", 60),
    ("sec", 1),
)

_COLUMN_LABEL_MAP: dict[str, str] = {
    "user.longName": "User",
    "user.id": "ID",
    "user.shortName": "AKA",
    "user.hwModel": "Hardware",
    "user.publicKey": "Pubkey",
    "user.role": "Role",
    "position.latitude": "Latitude",
    "position.longitude": "Longitude",
    "position.altitude": "Altitude",
    "deviceMetrics.batteryLevel": "Battery",
    "deviceMetrics.channelUtilization": "Channel util.",
    "deviceMetrics.airUtilTx": "Tx air util.",
    "snr": "SNR",
    "hopsAway": "Hops",
    "channel": "Channel",
    "lastHeard": "LastHeard",
    "since": "Since",
    "isFavorite": "Fav",
}


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


def _format_numeric_value(
    value: int | float | None,
    precision: int = 2,
    unit: str = "",
) -> str | None:
    """Format a numeric value as a string with fixed decimal precision and an optional unit suffix.

    Parameters
    ----------
    value : int | float | None
        Value to format; returns `None` when this is `None`.
    precision : int
        Number of digits after the decimal point. (Default value = 2)
    unit : str
        Suffix appended directly after the number (for example, "V" or " dB").

    Returns
    -------
    str | None
        `None` if `value` is `None`, otherwise the formatted string using the given precision and unit.
    """
    return f"{value:.{precision}f}{unit}" if value is not None else None


def _format_timestamp(ts: int | float | None) -> str | None:
    """Format a Unix timestamp as "YYYY-MM-DD HH:MM:SSZ" (UTC) or return None when no timestamp is available.

    Parameters
    ----------
    ts : int | float | None
        Seconds since the Unix epoch. If `ts` is `None` or `0`, no timestamp is available.

    Returns
    -------
    str | None
        Formatted timestamp string, or `None` if `ts` is `None` or `0`.
    """
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        if ts is not None and ts != 0
        else None
    )


def _format_time_ago(ts: int | float | None) -> str | None:
    """Return a short human-readable relative time string for a past Unix epoch timestamp.

    Parameters
    ----------
    ts : int | float | None
        Unix timestamp in seconds since the epoch. If `None` or `0`, no computation is performed.

    Returns
    -------
    str | None
        A concise relative time string such as "now", "5 sec ago", or "2 min ago",
        or `None` if `ts` is `None`, `0`, or represents a time in the future.
    """
    if ts is None or ts == 0:
        return None
    delta = datetime.now(tz=timezone.utc) - datetime.fromtimestamp(ts, tz=timezone.utc)
    delta_secs = int(delta.total_seconds())
    if delta_secs < 0:
        return None
    return _timeago(delta_secs)


def _get_human_readable_column_label(name: str) -> str:
    """Map an internal dotted field path to a human-readable column label.

    Parameters
    ----------
    name : str
        Dotted field path or key to convert (e.g., "user.longName", "position.latitude").

    Returns
    -------
    str
        A human-readable label for the given field when known; otherwise returns the original `name`.
    """
    return _COLUMN_LABEL_MAP.get(name, name)


# pylint: disable=too-many-return-statements
def _format_node_field(
    col_name: str,
    raw_value: Any,
    node: dict[str, Any],
) -> str | None:
    """Format a single node field value based on its column name.

    Parameters
    ----------
    col_name : str
        The column/field name (e.g., "user.shortName", "deviceMetrics.batteryLevel").
    raw_value : Any
        The raw value extracted from the node.
    node : dict[str, Any]
        The full node dictionary (used for fallback values).

    Returns
    -------
    str | None
        The formatted string value for display, or None if value is None.
    """
    # Defensive access for node["num"] to avoid KeyError on malformed dicts
    node_num = node.get("num")
    presumptive_id = "!00000000"
    if isinstance(node_num, int):
        presumptive_id = f"!{node_num:08x}"

    if col_name == "channel":
        if raw_value is None:
            return "0"
        return str(raw_value)
    elif col_name == "deviceMetrics.channelUtilization":
        return _format_numeric_value(raw_value, 2, "%")
    elif col_name == "deviceMetrics.airUtilTx":
        return _format_numeric_value(raw_value, 2, "%")
    elif col_name == "deviceMetrics.batteryLevel":
        if raw_value in _BATTERY_LEVEL_POWERED_SENTINELS:
            return "Powered"
        else:
            return _format_numeric_value(raw_value, 0, "%")
    elif col_name == "isFavorite":
        return "*" if raw_value else ""
    elif col_name == "lastHeard":
        return _format_timestamp(raw_value)
    elif col_name == "position.latitude":
        return _format_numeric_value(raw_value, 4, "°")
    elif col_name == "position.longitude":
        return _format_numeric_value(raw_value, 4, "°")
    elif col_name == "position.altitude":
        return _format_numeric_value(raw_value, 0, "m")
    elif col_name == "since":
        return _format_time_ago(raw_value)
    elif col_name == "snr":
        return _format_numeric_value(raw_value, 0, " dB")
    elif col_name == "user.shortName":
        if raw_value is not None:
            return str(raw_value)
        return f"Meshtastic {presumptive_id[-4:]}"
    elif col_name == "user.id":
        if raw_value is not None:
            return str(raw_value)
        return presumptive_id
    else:
        return str(raw_value) if raw_value is not None else None
