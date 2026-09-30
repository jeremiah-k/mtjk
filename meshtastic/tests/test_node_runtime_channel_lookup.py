"""Unit tests for the _NodeChannelLookupRuntime class."""

# pylint: disable=redefined-outer-name

import pytest

from meshtastic.node_runtime.channel_lookup_runtime import _NodeChannelLookupRuntime
from meshtastic.node_runtime.channel_state import _NodeChannelState
from meshtastic.protobuf import channel_pb2


@pytest.fixture
def channel_state() -> _NodeChannelState:
    """Provide isolated owned channel state for lookup tests."""
    return _NodeChannelState()


@pytest.fixture
def lookup(channel_state: _NodeChannelState) -> _NodeChannelLookupRuntime:
    """Provide a lookup runtime bound to owned channel state."""
    return _NodeChannelLookupRuntime(channel_state)


def _make_channel(
    index: int,
    role: channel_pb2.Channel.Role.ValueType,
    name: str = "",
) -> channel_pb2.Channel:
    """Create a Channel protobuf with the given index, role, and optional name.

    Parameters
    ----------
    index : int
        Channel index.
    role : channel_pb2.Channel.Role.ValueType
        Channel role (PRIMARY, SECONDARY, DISABLED).
    name : str
        Optional channel settings name.

    Returns
    -------
    channel_pb2.Channel
        A configured Channel instance.
    """
    channel = channel_pb2.Channel(index=index, role=role)
    if name:
        channel.settings.name = name
    return channel


@pytest.mark.unit
def test_get_channel_by_index_valid(
    lookup: _NodeChannelLookupRuntime, channel_state: _NodeChannelState
) -> None:
    """get_channel_by_index returns the live channel for a valid index."""
    primary = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, name="primary")
    secondary = _make_channel(1, channel_pb2.Channel.Role.SECONDARY, name="secondary")
    channel_state.channels = [primary, secondary]

    result = lookup._get_channel_by_index(0)

    assert result is not None
    assert result is primary
    assert result.index == 0

    result = lookup._get_channel_by_index(1)

    assert result is not None
    assert result is secondary
    assert result.index == 1


@pytest.mark.unit
def test_get_channel_by_index_out_of_range(
    lookup: _NodeChannelLookupRuntime, channel_state: _NodeChannelState
) -> None:
    """get_channel_by_index returns None for out-of-range indices."""
    primary = _make_channel(0, channel_pb2.Channel.Role.PRIMARY)
    channel_state.channels = [primary]

    assert lookup._get_channel_by_index(-1) is None
    assert lookup._get_channel_by_index(1) is None
    assert lookup._get_channel_by_index(100) is None


@pytest.mark.unit
def test_get_channel_by_index_none_channels(
    lookup: _NodeChannelLookupRuntime, channel_state: _NodeChannelState
) -> None:
    """get_channel_by_index returns None when channels is None."""
    channel_state.channels = None

    assert lookup._get_channel_by_index(0) is None


@pytest.mark.unit
def test_get_channel_copy_by_index_returns_copy(
    lookup: _NodeChannelLookupRuntime, channel_state: _NodeChannelState
) -> None:
    """get_channel_copy_by_index returns a defensive copy, not the live reference."""
    primary = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, name="primary")
    channel_state.channels = [primary]

    copy = lookup._get_channel_copy_by_index(0)

    assert copy is not None
    assert copy is not primary
    assert copy.index == primary.index
    assert copy.settings.name == primary.settings.name


@pytest.mark.unit
def test_get_channel_copy_by_index_modification_isolated(
    lookup: _NodeChannelLookupRuntime, channel_state: _NodeChannelState
) -> None:
    """Modifying the copy does not affect the original channel."""
    primary = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, name="original")
    channel_state.channels = [primary]

    copy = lookup._get_channel_copy_by_index(0)
    assert copy is not None

    copy.settings.name = "modified"
    copy.role = channel_pb2.Channel.Role.DISABLED

    assert primary.settings.name == "original"
    assert primary.role == channel_pb2.Channel.Role.PRIMARY


@pytest.mark.unit
def test_get_channel_copy_by_index_out_of_range(
    lookup: _NodeChannelLookupRuntime, channel_state: _NodeChannelState
) -> None:
    """get_channel_copy_by_index returns None for out-of-range indices."""
    primary = _make_channel(0, channel_pb2.Channel.Role.PRIMARY)
    channel_state.channels = [primary]

    assert lookup._get_channel_copy_by_index(-1) is None
    assert lookup._get_channel_copy_by_index(1) is None


@pytest.mark.unit
def test_get_channel_by_name_exists(
    lookup: _NodeChannelLookupRuntime, channel_state: _NodeChannelState
) -> None:
    """get_channel_by_name returns the live channel when name matches."""
    primary = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, name="main")
    secondary = _make_channel(1, channel_pb2.Channel.Role.SECONDARY, name="admin")
    channel_state.channels = [primary, secondary]

    result = lookup._get_channel_by_name("main")

    assert result is not None
    assert result is primary
    assert result.settings.name == "main"

    result = lookup._get_channel_by_name("admin")

    assert result is not None
    assert result is secondary


@pytest.mark.unit
def test_get_channel_by_name_not_found(
    lookup: _NodeChannelLookupRuntime, channel_state: _NodeChannelState
) -> None:
    """get_channel_by_name returns None when no channel has the given name."""
    primary = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, name="main")
    channel_state.channels = [primary]

    assert lookup._get_channel_by_name("nonexistent") is None
    assert lookup._get_channel_by_name("Admin") is None  # case-sensitive


@pytest.mark.unit
def test_get_channel_by_name_none_channels(
    lookup: _NodeChannelLookupRuntime, channel_state: _NodeChannelState
) -> None:
    """get_channel_by_name returns None when channels is None."""
    channel_state.channels = None

    assert lookup._get_channel_by_name("any") is None


@pytest.mark.unit
def test_get_channel_by_name_empty_name(
    lookup: _NodeChannelLookupRuntime, channel_state: _NodeChannelState
) -> None:
    """get_channel_by_name with empty name matches channels with no settings.name."""
    primary = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, name="")
    secondary = _make_channel(1, channel_pb2.Channel.Role.SECONDARY, name="named")
    channel_state.channels = [primary, secondary]

    result = lookup._get_channel_by_name("")

    assert result is not None
    assert result is primary


@pytest.mark.unit
def test_get_channel_copy_by_name_returns_copy(
    lookup: _NodeChannelLookupRuntime, channel_state: _NodeChannelState
) -> None:
    """get_channel_copy_by_name returns a defensive copy found by name."""
    primary = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, name="main")
    channel_state.channels = [primary]

    copy = lookup._get_channel_copy_by_name("main")

    assert copy is not None
    assert copy is not primary
    assert copy.settings.name == "main"


@pytest.mark.unit
def test_get_channel_copy_by_name_not_found(
    lookup: _NodeChannelLookupRuntime, channel_state: _NodeChannelState
) -> None:
    """get_channel_copy_by_name returns None when name is not found."""
    primary = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, name="main")
    channel_state.channels = [primary]

    assert lookup._get_channel_copy_by_name("nonexistent") is None


@pytest.mark.unit
def test_get_disabled_channel_exists(
    lookup: _NodeChannelLookupRuntime, channel_state: _NodeChannelState
) -> None:
    """get_disabled_channel returns the first disabled channel."""
    primary = _make_channel(0, channel_pb2.Channel.Role.PRIMARY)
    secondary = _make_channel(1, channel_pb2.Channel.Role.SECONDARY)
    disabled1 = _make_channel(2, channel_pb2.Channel.Role.DISABLED)
    disabled2 = _make_channel(3, channel_pb2.Channel.Role.DISABLED)
    channel_state.channels = [primary, secondary, disabled1, disabled2]

    result = lookup._get_disabled_channel()

    assert result is not None
    assert result is disabled1
    assert result.role == channel_pb2.Channel.Role.DISABLED


@pytest.mark.unit
def test_get_disabled_channel_none_exist(
    lookup: _NodeChannelLookupRuntime, channel_state: _NodeChannelState
) -> None:
    """get_disabled_channel returns None when no disabled channel exists."""
    primary = _make_channel(0, channel_pb2.Channel.Role.PRIMARY)
    secondary = _make_channel(1, channel_pb2.Channel.Role.SECONDARY)
    channel_state.channels = [primary, secondary]

    assert lookup._get_disabled_channel() is None


@pytest.mark.unit
def test_get_disabled_channel_none_channels(
    lookup: _NodeChannelLookupRuntime, channel_state: _NodeChannelState
) -> None:
    """get_disabled_channel returns None when channels is None."""
    channel_state.channels = None

    assert lookup._get_disabled_channel() is None


@pytest.mark.unit
def test_get_disabled_channel_empty_list(
    lookup: _NodeChannelLookupRuntime, channel_state: _NodeChannelState
) -> None:
    """get_disabled_channel returns None when channels is empty."""
    channel_state.channels = []

    assert lookup._get_disabled_channel() is None


@pytest.mark.unit
def test_get_disabled_channel_copy_returns_copy(
    lookup: _NodeChannelLookupRuntime, channel_state: _NodeChannelState
) -> None:
    """get_disabled_channel_copy returns a defensive copy of the disabled channel."""
    disabled = _make_channel(0, channel_pb2.Channel.Role.DISABLED)
    channel_state.channels = [disabled]

    copy = lookup._get_disabled_channel_copy()

    assert copy is not None
    assert copy is not disabled
    assert copy.role == channel_pb2.Channel.Role.DISABLED


@pytest.mark.unit
def test_get_disabled_channel_copy_modification_isolated(
    lookup: _NodeChannelLookupRuntime, channel_state: _NodeChannelState
) -> None:
    """Modifying the disabled channel copy does not affect the original."""
    disabled = _make_channel(0, channel_pb2.Channel.Role.DISABLED)
    channel_state.channels = [disabled]

    copy = lookup._get_disabled_channel_copy()
    assert copy is not None

    copy.role = channel_pb2.Channel.Role.PRIMARY

    assert disabled.role == channel_pb2.Channel.Role.DISABLED


@pytest.mark.unit
def test_get_disabled_channel_copy_none_channels(
    lookup: _NodeChannelLookupRuntime, channel_state: _NodeChannelState
) -> None:
    """get_disabled_channel_copy returns None when channels is None."""
    channel_state.channels = None

    assert lookup._get_disabled_channel_copy() is None


@pytest.mark.unit
def test_get_disabled_channel_copy_none_exist(
    lookup: _NodeChannelLookupRuntime, channel_state: _NodeChannelState
) -> None:
    """get_disabled_channel_copy returns None when no disabled channel exists."""
    primary = _make_channel(0, channel_pb2.Channel.Role.PRIMARY)
    secondary = _make_channel(1, channel_pb2.Channel.Role.SECONDARY)
    channel_state.channels = [primary, secondary]

    assert lookup._get_disabled_channel_copy() is None


@pytest.mark.unit
def test_get_named_admin_channel_index_found(
    lookup: _NodeChannelLookupRuntime, channel_state: _NodeChannelState
) -> None:
    """get_named_admin_channel_index returns the index of a named admin channel."""
    primary = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, name="primary")
    admin = _make_channel(1, channel_pb2.Channel.Role.SECONDARY, name="admin")
    disabled = _make_channel(2, channel_pb2.Channel.Role.DISABLED)
    channel_state.channels = [primary, admin, disabled]

    result = lookup._get_named_admin_channel_index()

    assert result == 1


@pytest.mark.unit
def test_get_named_admin_channel_index_case_insensitive(
    lookup: _NodeChannelLookupRuntime, channel_state: _NodeChannelState
) -> None:
    """get_named_admin_channel_index matches 'admin' case-insensitively."""
    primary = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, name="primary")
    admin = _make_channel(2, channel_pb2.Channel.Role.SECONDARY, name="ADMIN")
    channel_state.channels = [primary, admin]

    result = lookup._get_named_admin_channel_index()

    assert result == 2


@pytest.mark.unit
def test_get_named_admin_channel_index_ignores_disabled(
    lookup: _NodeChannelLookupRuntime, channel_state: _NodeChannelState
) -> None:
    """get_named_admin_channel_index ignores DISABLED channels even if named admin."""
    primary = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, name="primary")
    disabled_admin = _make_channel(1, channel_pb2.Channel.Role.DISABLED, name="admin")
    channel_state.channels = [primary, disabled_admin]

    result = lookup._get_named_admin_channel_index()

    assert result is None


@pytest.mark.unit
def test_get_named_admin_channel_index_not_found(
    lookup: _NodeChannelLookupRuntime, channel_state: _NodeChannelState
) -> None:
    """get_named_admin_channel_index returns None when no admin channel exists."""
    primary = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, name="primary")
    secondary = _make_channel(1, channel_pb2.Channel.Role.SECONDARY, name="other")
    channel_state.channels = [primary, secondary]

    assert lookup._get_named_admin_channel_index() is None


@pytest.mark.unit
def test_get_named_admin_channel_index_none_channels(
    lookup: _NodeChannelLookupRuntime, channel_state: _NodeChannelState
) -> None:
    """get_named_admin_channel_index returns None when channels is None."""
    channel_state.channels = None

    assert lookup._get_named_admin_channel_index() is None


@pytest.mark.unit
def test_get_admin_channel_index_returns_named_index(
    lookup: _NodeChannelLookupRuntime, channel_state: _NodeChannelState
) -> None:
    """get_admin_channel_index returns the named admin index when present."""
    primary = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, name="primary")
    admin = _make_channel(3, channel_pb2.Channel.Role.SECONDARY, name="admin")
    channel_state.channels = [primary, admin]

    result = lookup._get_admin_channel_index()

    assert result == 3


@pytest.mark.unit
def test_get_admin_channel_index_defaults_to_zero(
    lookup: _NodeChannelLookupRuntime, channel_state: _NodeChannelState
) -> None:
    """get_admin_channel_index returns 0 when no named admin channel exists."""
    primary = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, name="primary")
    secondary = _make_channel(1, channel_pb2.Channel.Role.SECONDARY, name="other")
    channel_state.channels = [primary, secondary]

    result = lookup._get_admin_channel_index()

    assert result == 0


@pytest.mark.unit
def test_get_admin_channel_index_empty_channels(
    lookup: _NodeChannelLookupRuntime, channel_state: _NodeChannelState
) -> None:
    """get_admin_channel_index returns 0 when channels is empty."""
    channel_state.channels = []

    result = lookup._get_admin_channel_index()

    assert result == 0


@pytest.mark.unit
def test_named_admin_index_from_channels_finds_admin_channel(
    lookup: _NodeChannelLookupRuntime,
) -> None:
    """The named-admin rule finds the first enabled admin-named channel in a list."""
    channels = [
        _make_channel(0, channel_pb2.Channel.Role.PRIMARY, name="primary"),
        _make_channel(1, channel_pb2.Channel.Role.SECONDARY, name="admin"),
        _make_channel(2, channel_pb2.Channel.Role.DISABLED, name="admin"),
    ]

    result = lookup._named_admin_index_from_channels(channels)

    assert result == 1


@pytest.mark.unit
def test_named_admin_index_from_channels_returns_zero_when_not_found(
    lookup: _NodeChannelLookupRuntime,
) -> None:
    """The named-admin rule falls back to channel zero for arbitrary lists."""
    channels = [
        _make_channel(0, channel_pb2.Channel.Role.PRIMARY, name="primary"),
        _make_channel(1, channel_pb2.Channel.Role.SECONDARY, name="trailchat"),
    ]

    result = lookup._named_admin_index_from_channels(channels)

    assert result == 0
