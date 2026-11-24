"""Transaction planning and snapshot capture for setURL transactions."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from meshtastic.node_runtime.channel_state import _NodeChannelState
from meshtastic.node_runtime.ports import _NodePort
from meshtastic.node_runtime.seturl.context import _SetUrlAdminContext
from meshtastic.node_runtime.seturl.helpers import _channels_fingerprint
from meshtastic.node_runtime.seturl.parser import _SetUrlParsedInput
from meshtastic.node_runtime.shared import _is_named_admin_channel_name
from meshtastic.protobuf import channel_pb2

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)

_ERR_CONFIG_OR_CHANNELS_NOT_LOADED = "Config or channels not loaded"


@dataclass
class _SetUrlAddOnlyPlan:
    """Planning output for addOnly transactions."""

    ignored_channel_names: list[str]
    channels_to_write: list[tuple[channel_pb2.Channel, str]]
    deferred_add_only_admin_channel: tuple[channel_pb2.Channel, str] | None
    deferred_add_only_admin_index: int | None
    original_channels_ref: list[channel_pb2.Channel]
    original_channels_fingerprint: tuple[bytes, ...]
    original_channels_by_index: dict[int, channel_pb2.Channel]


@dataclass
class _SetUrlReplacePlan:
    """Planning output for replace-all transactions."""

    max_channels: int
    replace_original_channels_ref: list[channel_pb2.Channel]
    replace_original_channels_fingerprint: tuple[bytes, ...]
    staged_channels: list[channel_pb2.Channel]
    staged_channels_by_index: dict[int, channel_pb2.Channel]
    deferred_new_named_admin_channel: channel_pb2.Channel | None
    deferred_new_named_admin_index: int | None
    deferred_previous_admin_slot_channel: channel_pb2.Channel | None


class _SetUrlAddOnlyPlanner:
    """Owns addOnly transaction planning and snapshot capture."""

    def __init__(
        self,
        node: "Node",
        port: _NodePort | None = None,
        *,
        parsed_input: _SetUrlParsedInput,
        admin_context: _SetUrlAdminContext,
        channel_state: _NodeChannelState,
    ) -> None:
        """Initialize the planner with its facade and collaborator port.

        Parameters
        ----------
        node : Node
            The facade whose public state the planner reads.
        port : _NodePort | None
            Narrow port over the facade's internals; derived from ``node``
            when omitted.
        parsed_input : _SetUrlParsedInput
            Parsed setURL input the plan stages from.
        admin_context : _SetUrlAdminContext
            Admin-channel write context for the transaction.
        channel_state : _NodeChannelState
            Shared channel-state owner.
        """
        self._node = node
        self._port = port if port is not None else _NodePort(node)
        self._channel_state = channel_state
        self._parsed_input = parsed_input
        self._admin_context = admin_context

    def build_plan(self) -> _SetUrlAddOnlyPlan:
        """Build addOnly staging plan, dedupe selection, and deferred admin handling."""
        ignored_channel_names: list[str] = []
        channels_to_write: list[tuple[channel_pb2.Channel, str]] = []
        original_channels_ref: list[channel_pb2.Channel] = []
        original_channels_by_index: dict[int, channel_pb2.Channel] = {}
        original_channels_fingerprint: tuple[bytes, ...] = ()
        with self._channel_state.lock:
            channels = self._channel_state.channels
            if channels is None:
                self._port.raise_interface_error(_ERR_CONFIG_OR_CHANNELS_NOT_LOADED)
            original_channels_ref = channels
            original_channels_fingerprint = _channels_fingerprint(channels)
            existing_names_normalized = {
                channel.settings.name.lower()
                for channel in channels
                if (
                    channel.role != channel_pb2.Channel.Role.DISABLED
                    and channel.settings
                    and channel.settings.name
                )
            }
            disabled_channels = [
                channel
                for channel in channels
                if channel.role == channel_pb2.Channel.Role.DISABLED
            ]
            pending_new_settings: list[channel_pb2.ChannelSettings] = []
            for channel_settings in self._parsed_input.channel_set.settings:
                channel_name = channel_settings.name
                normalized_name = channel_name.lower()
                if channel_name == "" or normalized_name in existing_names_normalized:
                    ignored_channel_names.append(channel_name)
                    continue
                pending_new_settings.append(channel_settings)
                existing_names_normalized.add(normalized_name)

            if len(pending_new_settings) > len(disabled_channels):
                self._port.raise_interface_error(
                    "No free channels were found for all additions "
                    f"(need {len(pending_new_settings)}, available {len(disabled_channels)})"
                )

            for disabled_channel, new_settings in zip(
                disabled_channels,
                pending_new_settings,
                strict=False,
            ):
                previous_channel = channel_pb2.Channel()
                previous_channel.CopyFrom(disabled_channel)
                original_channels_by_index[disabled_channel.index] = previous_channel

                staged_channel = channel_pb2.Channel()
                staged_channel.CopyFrom(disabled_channel)
                staged_channel.settings.CopyFrom(new_settings)
                staged_channel.role = channel_pb2.Channel.Role.SECONDARY
                channels_to_write.append((staged_channel, new_settings.name))

        deferred_add_only_admin_channel: tuple[channel_pb2.Channel, str] | None = None
        deferred_add_only_admin_index: int | None = None
        if not self._admin_context.has_admin_write_node_named_admin:
            deferred_add_only_admin_channel = next(
                (
                    candidate
                    for candidate in channels_to_write
                    if _is_named_admin_channel_name(candidate[1])
                ),
                None,
            )
        if deferred_add_only_admin_channel is not None:
            deferred_add_only_admin_index = deferred_add_only_admin_channel[0].index

        return _SetUrlAddOnlyPlan(
            ignored_channel_names=ignored_channel_names,
            channels_to_write=channels_to_write,
            deferred_add_only_admin_channel=deferred_add_only_admin_channel,
            deferred_add_only_admin_index=deferred_add_only_admin_index,
            original_channels_ref=original_channels_ref,
            original_channels_fingerprint=original_channels_fingerprint,
            original_channels_by_index=original_channels_by_index,
        )


class _SetUrlReplacePlanner:
    """Owns replace-all transaction planning and snapshot capture."""

    def __init__(
        self,
        node: "Node",
        port: _NodePort | None = None,
        *,
        parsed_input: _SetUrlParsedInput,
        admin_context: _SetUrlAdminContext,
        channel_state: _NodeChannelState,
    ) -> None:
        """Initialize the planner with its facade and collaborator port.

        Parameters
        ----------
        node : Node
            The facade whose public state the planner reads.
        port : _NodePort | None
            Narrow port over the facade's internals; derived from ``node``
            when omitted.
        parsed_input : _SetUrlParsedInput
            Parsed setURL input the plan stages from.
        admin_context : _SetUrlAdminContext
            Admin-channel write context for the transaction.
        channel_state : _NodeChannelState
            Shared channel-state owner.
        """
        self._node = node
        self._port = port if port is not None else _NodePort(node)
        self._channel_state = channel_state
        self._parsed_input = parsed_input
        self._admin_context = admin_context

    def build_plan(self) -> _SetUrlReplacePlan:
        """Build replace-all staging plan, deferred admin strategy, and snapshots."""
        replace_original_channels_fingerprint: tuple[bytes, ...] = ()
        with self._channel_state.lock:
            replace_original_channels_ref: list[channel_pb2.Channel] = []
            channels = self._channel_state.channels
            if channels is None:
                self._port.raise_interface_error(_ERR_CONFIG_OR_CHANNELS_NOT_LOADED)
            replace_original_channels_ref = channels
            max_channels = len(channels)
            replace_original_channels_fingerprint = _channels_fingerprint(channels)

        staged_channels: list[channel_pb2.Channel] = []
        for i, channel_settings in enumerate(self._parsed_input.channel_set.settings):
            if i >= max_channels:
                logger.warning(
                    "URL contains more than %d channels; extra channels are ignored.",
                    max_channels,
                )
                break
            staged_channel = channel_pb2.Channel()
            staged_channel.role = (
                channel_pb2.Channel.Role.PRIMARY
                if i == 0
                else channel_pb2.Channel.Role.SECONDARY
            )
            staged_channel.index = i
            staged_channel.settings.CopyFrom(channel_settings)
            staged_channels.append(staged_channel)

        # Full-replace semantics: any channels not present in the URL should be
        # explicitly disabled so stale secondaries/admin channels do not persist.
        for i in range(len(staged_channels), max_channels):
            disabled_channel = channel_pb2.Channel()
            disabled_channel.index = i
            disabled_channel.role = channel_pb2.Channel.Role.DISABLED
            staged_channels.append(disabled_channel)

        staged_named_admin_channels = [
            staged_channel
            for staged_channel in staged_channels
            if staged_channel.settings
            and staged_channel.settings.name
            and _is_named_admin_channel_name(staged_channel.settings.name)
        ]
        if len(staged_named_admin_channels) > 1:
            self._port.raise_interface_error(
                "URL contains multiple channels named 'admin'; only one is allowed"
            )
        deferred_new_named_admin_channel = (
            staged_named_admin_channels[0] if staged_named_admin_channels else None
        )
        deferred_new_named_admin_index = (
            deferred_new_named_admin_channel.index
            if deferred_new_named_admin_channel is not None
            else None
        )
        staged_channels_by_index = {
            staged_channel.index: staged_channel for staged_channel in staged_channels
        }

        deferred_previous_admin_slot_channel: channel_pb2.Channel | None = None
        if self._admin_context.named_admin_index_for_write is not None:
            previous_admin_slot_channel = staged_channels_by_index.get(
                self._admin_context.named_admin_index_for_write
            )
            if previous_admin_slot_channel is not None and (
                deferred_new_named_admin_channel is None
                or previous_admin_slot_channel.index
                != deferred_new_named_admin_channel.index
            ):
                deferred_previous_admin_slot_channel = previous_admin_slot_channel

        return _SetUrlReplacePlan(
            max_channels=max_channels,
            replace_original_channels_ref=replace_original_channels_ref,
            replace_original_channels_fingerprint=replace_original_channels_fingerprint,
            staged_channels=staged_channels,
            staged_channels_by_index=staged_channels_by_index,
            deferred_new_named_admin_channel=deferred_new_named_admin_channel,
            deferred_new_named_admin_index=deferred_new_named_admin_index,
            deferred_previous_admin_slot_channel=deferred_previous_admin_slot_channel,
        )
