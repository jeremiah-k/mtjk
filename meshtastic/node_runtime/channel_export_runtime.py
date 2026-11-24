"""Channel export/hash and primary-channel mutation runtime owner."""

import base64
import logging
from typing import TYPE_CHECKING, Any

from meshtastic.node_runtime.channel_state import _NodeChannelState
from meshtastic.node_runtime.ports import _NodePort
from meshtastic.protobuf import apponly_pb2, channel_pb2, localonly_pb2
from meshtastic.util import fromPSK, generate_channel_hash

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)

LORA_TIMEOUT_MSG = "Timeout waiting for LoRa config"


class _NodeChannelExportRuntime:
    """Owns channel URL export, channel-hash export, and primary PSK mutation."""

    def __init__(
        self,
        node: "Node",
        port: _NodePort | None = None,
        *,
        channel_state: _NodeChannelState,
    ) -> None:
        """Initialize the runtime with its facade and collaborator port.

        Parameters
        ----------
        node : Node
            The facade whose public state the runtime reads.
        port : _NodePort | None
            Narrow port over the facade's internals; derived from ``node``
            when omitted.
        channel_state : _NodeChannelState
            Shared channel-state owner.
        """
        self._node = node
        self._port = port if port is not None else _NodePort(node)
        self._channel_state = channel_state

    def _snapshot_channels(self) -> list[channel_pb2.Channel]:
        """Return detached channel snapshots captured under the channel lock."""
        return self._channel_state.snapshot_channels()

    def _snapshot_local_config(self) -> localonly_pb2.LocalConfig:
        """Return detached localConfig snapshot for consistent field checks/copies."""
        config_snapshot = localonly_pb2.LocalConfig()
        node_db_lock = getattr(self._node, "_node_db_lock", None)
        if node_db_lock is None:
            local_config = getattr(self._node, "localConfig", None)
            if local_config is not None:
                config_snapshot.CopyFrom(local_config)
            return config_snapshot
        with node_db_lock:
            local_config = getattr(self._node, "localConfig", None)
            if local_config is not None:
                config_snapshot.CopyFrom(local_config)
        return config_snapshot

    def _build_and_encode_channel_set(
        self, channels_snapshot: list[channel_pb2.Channel], *, include_all: bool
    ) -> str:
        """Build channel_set from snapshot, ensure LoRa config, serialize and encode.

        Returns the URL-safe base64 encoded string (rstrip'd of '=').
        """
        channel_set = apponly_pb2.ChannelSet()
        if not channels_snapshot:
            self._port.raise_interface_error("Error: No channels have been read")
        primary_channel = next(
            (
                channel
                for channel in channels_snapshot
                if channel.role == channel_pb2.Channel.Role.PRIMARY
            ),
            None,
        )
        if primary_channel is None:
            self._port.raise_interface_error("Error: No primary channel found")
        channel_set.settings.append(primary_channel.settings)
        if include_all:
            for channel in channels_snapshot:
                if channel.role == channel_pb2.Channel.Role.SECONDARY:
                    channel_set.settings.append(channel.settings)

        local_config_snapshot = self._snapshot_local_config()
        if not local_config_snapshot.HasField("lora"):
            self._node.requestConfig(
                local_config_snapshot.DESCRIPTOR.fields_by_name["lora"]
            )
            wait_for_config = getattr(self._node, "waitForConfig", None)
            if callable(wait_for_config):
                result = wait_for_config(attribute="lora")
                if not result:
                    raise RuntimeError(LORA_TIMEOUT_MSG)
            local_config_snapshot = self._snapshot_local_config()
            if not local_config_snapshot.HasField("lora"):
                self._port.raise_interface_error(
                    "LoRa config must be loaded before exporting a channel URL"
                )
        channel_set.lora_config.CopyFrom(local_config_snapshot.lora)
        serialized_channel_set = channel_set.SerializeToString()
        encoded = base64.urlsafe_b64encode(serialized_channel_set).decode("ascii")
        return encoded.rstrip("=")

    def get_url(self, *, include_all: bool = True) -> str:
        """Build a channel URL while preserving LoRa export semantics."""
        channels_snapshot = self._snapshot_channels()
        encoded = self._build_and_encode_channel_set(
            channels_snapshot, include_all=include_all
        )
        return f"https://meshtastic.org/e/#{encoded}"

    def _get_url_from_snapshot(
        self,
        channels_snapshot: list[channel_pb2.Channel],
        *,
        include_all: bool = True,
    ) -> str:
        """Build channel URL export from a pre-captured channel snapshot.

        This method allows callers to use the same snapshot for multiple
        operations, ensuring consistency between display and export.
        """
        encoded = self._build_and_encode_channel_set(
            channels_snapshot, include_all=include_all
        )
        return f"https://meshtastic.org/e/#{encoded}"

    def get_channels_with_hash(self) -> list[dict[str, Any]]:
        """Return index/role/name/hash descriptors for the current channels."""
        result: list[dict[str, Any]] = []
        channels_snapshot = self._snapshot_channels()
        if channels_snapshot:
            for channel in channels_snapshot:
                settings = getattr(channel, "settings", None)
                name = getattr(settings, "name", "")
                psk = getattr(settings, "psk", b"")
                has_name = bool(name)
                has_psk = bool(psk)
                hash_value = (
                    generate_channel_hash(name, psk) if has_name and has_psk else None
                )
                role_name = (
                    channel_pb2.Channel.Role.Name(channel.role)
                    if channel.role in channel_pb2.Channel.Role.values()
                    else f"UNKNOWN({channel.role})"
                )
                result.append(
                    {
                        "index": channel.index,
                        "role": role_name,
                        "name": name if has_name else "",
                        "hash": hash_value,
                    }
                )
        return result

    def _turn_off_encryption_on_primary_channel(self) -> None:
        """Disable primary-channel encryption and persist updated channel state."""
        with self._channel_state.mutation_lock:
            self._turn_off_encryption_on_primary_channel_locked()

    def _turn_off_encryption_on_primary_channel_locked(self) -> None:
        """Persist primary-channel encryption state under the mutation lock."""
        primary_snapshot: channel_pb2.Channel | None = None
        original_channels_ref: list[channel_pb2.Channel] | None = None
        original_primary_slot: channel_pb2.Channel | None = None
        original_primary_snapshot: bytes | None = None
        with self._channel_state.lock:
            channels = self._channel_state.channels
            if not channels:
                self._port.raise_interface_error("Error: No channels have been read")
            original_channels_ref = channels
            for channel in channels:
                if channel.role == channel_pb2.Channel.Role.PRIMARY:
                    original_primary_slot = channel
                    original_primary_snapshot = channel.SerializeToString()
                    primary_snapshot = channel_pb2.Channel()
                    primary_snapshot.CopyFrom(channel)
                    primary_snapshot.settings.psk = fromPSK("none")
                    break
            if primary_snapshot is None:
                self._port.raise_interface_error("Error: No primary channel found")
        logger.info("Writing modified channels to device")
        self._port.write_channel_snapshot(primary_snapshot)
        with self._channel_state.lock:
            channels = self._channel_state.channels
            if not channels:
                logger.warning(
                    "Primary channel write succeeded but local channel cache is unavailable; reload channels to refresh local state."
                )
                self._channel_state.partial_channels = []
                return
            if channels is not original_channels_ref:
                logger.warning(
                    "Primary channel write succeeded but channel cache changed concurrently; invalidating local channel cache."
                )
                self._channel_state.channels = None
                self._channel_state.partial_channels = []
                return
            for channel in channels:
                if channel.index == primary_snapshot.index:
                    if channel is not original_primary_slot:
                        logger.warning(
                            "Primary channel write succeeded but primary slot object changed concurrently; invalidating local channel cache."
                        )
                        self._channel_state.channels = None
                        self._channel_state.partial_channels = []
                        return
                    if (
                        original_primary_snapshot is not None
                        and channel.SerializeToString() != original_primary_snapshot
                    ):
                        logger.warning(
                            "Primary channel write succeeded but primary slot content changed concurrently; invalidating local channel cache."
                        )
                        self._channel_state.channels = None
                        self._channel_state.partial_channels = []
                        return
                    channel.CopyFrom(primary_snapshot)
                    self._channel_state.partial_channels = []
                    return
            logger.warning(
                "Primary channel write succeeded but local channel index %s is unavailable; invalidating local channel cache.",
                primary_snapshot.index,
            )
            self._channel_state.channels = None
            self._channel_state.partial_channels = []
