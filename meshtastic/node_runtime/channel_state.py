"""Owned channel cache state for :class:`meshtastic.node.Node`."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from types import TracebackType
from typing import Any, Protocol

from meshtastic.node_runtime.shared import MAX_CHANNELS
from meshtastic.protobuf import channel_pb2

logger = logging.getLogger(__name__)


class _ChannelLock(Protocol):
    """Minimal context-manager contract required by channel state."""

    def __enter__(self) -> Any:
        """Acquire the lock and return its context value."""
        # pylint: disable-next=unnecessary-ellipsis
        ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Any:
        """Release the lock after the protected operation."""
        # pylint: disable-next=unnecessary-ellipsis
        ...


class _NodeChannelState:
    """Own channel cache data and its synchronization boundary.

    ``Node.channels`` and ``Node.partialChannels`` remain compatibility-facing
    live attributes through properties on ``Node``. Internal channel runtimes
    should operate through this owner so the cache data and lock have one
    explicit home.
    """

    def __init__(self) -> None:
        self._lock: _ChannelLock = threading.RLock()
        self._mutation_lock: _ChannelLock = threading.Lock()
        self._channels: list[channel_pb2.Channel] | None = None
        self._partial_channels: list[channel_pb2.Channel] = []

    @property
    def lock(self) -> _ChannelLock:
        """Return the lock guarding cached and partial channel state."""
        return self._lock

    def _replace_lock(self, lock: _ChannelLock) -> None:
        """Replace the lock for legacy tests that instrument ``Node._channels_lock``."""
        self._lock = lock

    @property
    def mutation_lock(self) -> _ChannelLock:
        """Return the lock serializing multi-step channel mutations and device I/O."""
        return self._mutation_lock

    def _replace_mutation_lock(self, lock: _ChannelLock) -> None:
        """Replace the mutation lock for transaction-boundary instrumentation."""
        self._mutation_lock = lock

    @property
    def channels(self) -> list[channel_pb2.Channel] | None:
        """Return the live cached channel list without acquiring the owner lock."""
        return self._channels

    @channels.setter
    def channels(self, value: list[channel_pb2.Channel] | None) -> None:
        """Replace the live cached channel list without acquiring the owner lock."""
        self._channels = value

    @property
    def partial_channels(self) -> list[channel_pb2.Channel]:
        """Return the live partial-download channel list without acquiring the lock."""
        return self._partial_channels

    @partial_channels.setter
    def partial_channels(self, value: list[channel_pb2.Channel]) -> None:
        """Replace partial-download channel state without acquiring the owner lock."""
        self._partial_channels = value

    def has_channels(self) -> bool:
        """Return whether a complete channel cache is currently installed."""
        with self._lock:
            return self._channels is not None

    def invalidate(self) -> None:
        """Clear complete and partial channel caches atomically."""
        with self._lock:
            self._channels = None
            self._partial_channels = []

    def snapshot_channels(self) -> list[channel_pb2.Channel]:
        """Return detached copies of all currently cached channels."""
        with self._lock:
            return [self._copy_channel(channel) for channel in self._channels or []]

    def replace_with_copies(
        self,
        channels: Sequence[channel_pb2.Channel],
        *,
        normalize: bool = True,
    ) -> None:
        """Replace cached channels with defensive copies under the owner lock."""
        copied_channels = [self._copy_channel(channel) for channel in channels]
        with self._lock:
            self._channels = copied_channels
            if normalize:
                self._normalize_locked()

    def reset_for_download(self) -> None:
        """Clear complete and partial cache before a fresh channel download."""
        self.invalidate()

    def append_partial_if_new(self, channel: channel_pb2.Channel) -> bool:
        """Append a channel response when its index is not already staged."""
        with self._lock:
            if any(
                existing.index == channel.index for existing in self._partial_channels
            ):
                return False
            self._partial_channels.append(channel)
            return True

    def install_partial_channels(self) -> None:
        """Install the accumulated partial list as the complete normalized cache."""
        with self._lock:
            self._channels = list(self._partial_channels)
            self._normalize_locked()

    def normalize(self) -> None:
        """Normalize cached channel count and indexes under the owner lock."""
        with self._lock:
            self._normalize_locked()

    def _normalize_locked(self) -> None:
        """Normalize cached channels while the caller holds :attr:`lock`."""
        channels = self._channels
        if channels is None:
            return
        if len(channels) > MAX_CHANNELS:
            logger.warning(
                "Truncating channel list from %d to %d entries",
                len(channels),
                MAX_CHANNELS,
            )
            del channels[MAX_CHANNELS:]
        if len(channels) < MAX_CHANNELS:
            self._fill_locked()
            return
        for index, channel in enumerate(channels):
            channel.index = index

    def fill(self) -> None:
        """Append disabled channels to the device channel limit under the lock."""
        with self._lock:
            self._fill_locked()

    def _fill_locked(self) -> None:
        """Append disabled channels while the caller holds :attr:`lock`."""
        channels = self._channels
        if channels is None:
            return
        for index, channel in enumerate(channels):
            channel.index = index
        for index in range(len(channels), MAX_CHANNELS):
            channel = channel_pb2.Channel()
            channel.role = channel_pb2.Channel.Role.DISABLED
            channel.index = index
            channels.append(channel)

    def get_live_by_index(self, channel_index: int) -> channel_pb2.Channel | None:
        """Return the historical live channel reference for ``channel_index``."""
        with self._lock:
            channels = self._channels
            if channels and 0 <= channel_index < len(channels):
                return channels[channel_index]
            return None

    def get_copy_by_index(self, channel_index: int) -> channel_pb2.Channel | None:
        """Return a detached channel copy for ``channel_index`` when available."""
        with self._lock:
            channels = self._channels
            if channels and 0 <= channel_index < len(channels):
                return self._copy_channel(channels[channel_index])
            return None

    def get_live_by_name(self, name: str) -> channel_pb2.Channel | None:
        """Return the historical live channel reference matching ``name``."""
        with self._lock:
            for channel in self._channels or []:
                if channel.settings and channel.settings.name == name:
                    return channel
            return None

    def get_copy_by_name(self, name: str) -> channel_pb2.Channel | None:
        """Return a detached channel copy matching ``name`` when available."""
        with self._lock:
            for channel in self._channels or []:
                if channel.settings and channel.settings.name == name:
                    return self._copy_channel(channel)
            return None

    def get_live_disabled(self) -> channel_pb2.Channel | None:
        """Return the historical live first disabled-channel reference."""
        with self._lock:
            for channel in self._channels or []:
                if channel.role == channel_pb2.Channel.Role.DISABLED:
                    return channel
            return None

    def get_copy_disabled(self) -> channel_pb2.Channel | None:
        """Return a detached copy of the first disabled channel when available."""
        with self._lock:
            for channel in self._channels or []:
                if channel.role == channel_pb2.Channel.Role.DISABLED:
                    return self._copy_channel(channel)
            return None

    def named_admin_index(self, *, is_named_admin: Callable[[str], bool]) -> int | None:
        """Return the index of the enabled channel accepted by ``is_named_admin``."""
        with self._lock:
            channels = self._channels
            if channels is None:
                return None
            return _named_admin_index_in_channels(
                channels, is_named_admin=is_named_admin
            )

    @staticmethod
    def _copy_channel(channel: channel_pb2.Channel) -> channel_pb2.Channel:
        """Return a defensive protobuf copy of ``channel``."""
        copied = channel_pb2.Channel()
        copied.CopyFrom(channel)
        return copied


def _named_admin_index_in_channels(
    channels: Sequence[channel_pb2.Channel],
    *,
    is_named_admin: Callable[[str], bool],
) -> int | None:
    """Return the first enabled channel index accepted by ``is_named_admin``.

    Parameters
    ----------
    channels : Sequence[channel_pb2.Channel]
        Channel list to scan, such as the live cache or a staged rewrite list.
    is_named_admin : Callable[[str], bool]
        Predicate identifying a channel name as the named admin channel.

    Returns
    -------
    int | None
        Index of the first enabled matching channel, or None when absent.
    """
    for channel in channels:
        if (
            channel.role != channel_pb2.Channel.Role.DISABLED
            and channel.settings
            and is_named_admin(channel.settings.name)
        ):
            return channel.index
    return None
