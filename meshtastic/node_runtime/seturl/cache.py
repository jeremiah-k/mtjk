"""Local cache updates, invalidation, and restoration for setURL transactions."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from meshtastic.node_runtime.channel_state import _NodeChannelState
from meshtastic.node_runtime.ports import _NodePort
from meshtastic.node_runtime.seturl.helpers import _channels_fingerprint
from meshtastic.protobuf import channel_pb2, config_pb2

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)

_ERR_CONFIG_OR_CHANNELS_NOT_LOADED = "Config or channels not loaded"


class _SetUrlCacheManager:
    """Owns local cache updates, invalidation, and restoration for setURL transactions."""

    def __init__(
        self,
        node: Node,
        port: _NodePort | None = None,
        *,
        channel_state: _NodeChannelState,
    ) -> None:
        """Initialize the cache manager with its facade and collaborator port.

        Parameters
        ----------
        node : Node
            The facade whose public state the manager reads.
            port : _NodePort | None
                Narrow port over the facade's internals; derived from ``node``
                when omitted.
        channel_state : _NodeChannelState
            Shared channel-state owner.
        """
        self._node = node
        self._port = port if port is not None else _NodePort(node)
        self._channel_state = channel_state

    def _invalidate_channel_cache_locked(self, warning_message: str) -> None:
        """Invalidate channel cache while already holding ``_channel_state.lock``."""
        self._channel_state.channels = None
        self._channel_state.partial_channels = []
        logger.warning("%s", warning_message)

    def apply_add_only_success(
        self,
        channels_to_write: list[tuple[channel_pb2.Channel, str]],
        *,
        expected_channels_fingerprint: tuple[bytes, ...] | None = None,
    ) -> None:
        """Apply addOnly channel cache updates after remote writes succeed."""
        with self._channel_state.lock:
            channels = self._channel_state.channels
            if channels is None:
                self._channel_state.partial_channels = []
                logger.warning(
                    "Channel cache unavailable after successful addOnly apply; reload channels to refresh local state."
                )
                return
            if (
                expected_channels_fingerprint is not None
                and _channels_fingerprint(channels) != expected_channels_fingerprint
            ):
                self._invalidate_channel_cache_locked(
                    "Channel cache changed during addOnly cache update; invalidating local channel cache."
                )
                return
            for staged_channel, _ in channels_to_write:
                if 0 <= staged_channel.index < len(channels):
                    channels[staged_channel.index].CopyFrom(staged_channel)
                else:
                    self._invalidate_channel_cache_locked(
                        f"Channel index {staged_channel.index} out of range during addOnly cache update; invalidating local channel cache."
                    )
                    break
            else:
                self._channel_state.partial_channels = []

    def apply_replace_channel_write(
        self,
        staged_channel: channel_pb2.Channel,
        *,
        expected_channels_fingerprint: tuple[bytes, ...] | None = None,
        expected_channels_ref: list[channel_pb2.Channel] | None = None,
    ) -> tuple[bytes, ...]:
        """Apply a replace-path write and return the next guarded fingerprint."""
        with self._channel_state.lock:
            channels = self._channel_state.channels
            if channels is None:
                self._port.raise_interface_error(_ERR_CONFIG_OR_CHANNELS_NOT_LOADED)
            channels_changed = False
            if (
                expected_channels_ref is not None
                and channels is not expected_channels_ref
            ):
                channels_changed = True
            if not channels_changed and expected_channels_fingerprint is not None:
                channels_changed = (
                    _channels_fingerprint(channels) != expected_channels_fingerprint
                )
            if channels_changed:
                self._invalidate_channel_cache_locked(
                    "Channel cache changed during replace-all cache update; invalidating local channel cache."
                )
                self._port.raise_interface_error(
                    "Channel cache changed during replace-all cache update; aborting transaction."
                )
            if not 0 <= staged_channel.index < len(channels):
                self._invalidate_channel_cache_locked(
                    "Channel index "
                    f"{staged_channel.index} out of range during replace-all cache "
                    "update; invalidating local channel cache."
                )
                self._port.raise_interface_error(
                    f"Channel index {staged_channel.index} out of range during cache update"
                )
            channels[staged_channel.index].CopyFrom(staged_channel)
            self._channel_state.partial_channels = []
            return _channels_fingerprint(channels)

    def invalidate_channel_cache(self, warning_message: str) -> None:
        """Invalidate local channel cache after partial failure."""
        with self._channel_state.lock:
            self._invalidate_channel_cache_locked(warning_message)

    def apply_lora_success(self, lora_config: config_pb2.Config.LoRaConfig) -> None:
        """Apply successful LoRa cache update."""

        def _apply() -> None:
            self._node.localConfig.lora.CopyFrom(lora_config)

        self._port.execute_with_node_db_lock(_apply)

    def clear_lora_cache_with_warning(self, warning_message: str) -> None:
        """Clear the local LoRa config cache after a partial failure.

        Removes the locally-cached LoRa configuration so that subsequent
        reads will fetch fresh state from the device.  The caller is
        responsible for logging the reason via *warning_message*.
        """

        def _clear() -> None:
            self._node.localConfig.ClearField("lora")

        self._port.execute_with_node_db_lock(_clear)
        logger.warning("%s", warning_message)
