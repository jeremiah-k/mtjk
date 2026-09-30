"""Channel lookup and admin-index resolution runtime owner."""

from collections.abc import Callable, Sequence

from meshtastic.node_runtime.channel_state import (
    _named_admin_index_in_channels,
    _NodeChannelState,
)
from meshtastic.node_runtime.shared import _is_named_admin_channel_name
from meshtastic.protobuf import channel_pb2


class _NodeChannelLookupRuntime:
    """Own lock-safe channel lookup and admin-channel index resolution."""

    def __init__(self, channel_state: _NodeChannelState) -> None:
        self._channel_state = channel_state

    def _get_channel_by_index(self, channel_index: int) -> channel_pb2.Channel | None:
        """Return the historical live channel reference by index."""
        return self._channel_state.get_live_by_index(channel_index)

    def _get_channel_copy_by_index(
        self, channel_index: int
    ) -> channel_pb2.Channel | None:
        """Return a defensive channel copy by index for read-only callers."""
        return self._channel_state.get_copy_by_index(channel_index)

    def _get_channel_by_name(self, name: str) -> channel_pb2.Channel | None:
        """Return the historical live channel reference matching ``name``."""
        return self._channel_state.get_live_by_name(name)

    def _get_channel_copy_by_name(self, name: str) -> channel_pb2.Channel | None:
        """Return a defensive channel copy matching ``name``."""
        return self._channel_state.get_copy_by_name(name)

    def _get_disabled_channel(self) -> channel_pb2.Channel | None:
        """Return the historical live first disabled-channel reference."""
        return self._channel_state.get_live_disabled()

    def _get_disabled_channel_copy(self) -> channel_pb2.Channel | None:
        """Return a defensive copy of the first disabled channel."""
        return self._channel_state.get_copy_disabled()

    def _get_named_admin_channel_index(self) -> int | None:
        """Return index of explicitly named ``admin`` channel, if present."""
        predicate: Callable[[str], bool] = _is_named_admin_channel_name
        return self._channel_state.named_admin_index(is_named_admin=predicate)

    def _get_admin_channel_index(self) -> int:
        """Return named admin index when present; otherwise channel index zero."""
        named_admin_index = self._get_named_admin_channel_index()
        return 0 if named_admin_index is None else named_admin_index

    def _named_admin_index_from_channels(
        self, channel_list: Sequence[channel_pb2.Channel]
    ) -> int:
        """Return the named-admin channel index for an arbitrary channel list.

        Applies the authoritative named-admin lookup rule to a caller-provided
        list (for example a staged delete/rewrite plan) that is not the live
        cached channel state.

        Parameters
        ----------
        channel_list : Sequence[channel_pb2.Channel]
            Channel list to scan with the named-admin rule.

        Returns
        -------
        int
            Index of the first enabled channel named ``admin``, or 0 when none.
        """
        named_admin_index = _named_admin_index_in_channels(
            channel_list, is_named_admin=_is_named_admin_channel_name
        )
        return 0 if named_admin_index is None else named_admin_index
