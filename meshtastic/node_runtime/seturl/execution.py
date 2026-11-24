"""Remote write ordering and admin-index strategy for setURL transactions."""

from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from meshtastic.node_runtime.channel_state import _NodeChannelState
from meshtastic.node_runtime.ports import _NodePort
from meshtastic.node_runtime.seturl.cache import _SetUrlCacheManager
from meshtastic.node_runtime.seturl.context import _SetUrlAdminContext
from meshtastic.node_runtime.seturl.helpers import _channels_fingerprint
from meshtastic.node_runtime.seturl.parser import _SetUrlParsedInput
from meshtastic.node_runtime.seturl.planner import (
    _SetUrlAddOnlyPlan,
    _SetUrlReplacePlan,
)
from meshtastic.protobuf import admin_pb2, channel_pb2

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)

CHANNEL_WRITE_PACE_SECONDS = 1.0
"""Delay between successive channel snapshot writes.  Real hardware needs
time to write prefs to flash and reconfigure radio state between admin
writes.  1.0 s is conservative but reliable for USB serial devices."""

LORA_WRITE_SETTLE_SECONDS = 2.0
"""Extra settle delay before/after LoRa config writes.  LoRa reconfiguration
is expensive on firmware and may trigger radio recalibration."""

DEFERRED_ADMIN_PACE_SECONDS = 1.5
"""Delay before deferred admin-channel writes.  These writes modify the
admin channel topology and need extra settle time."""


class _ReplaceAllStage(enum.Enum):
    """Stage of a replace-all write sequence.

    Tracks which step of ``execute_replace_all`` is currently in progress
    so that error reporting can identify the exact failure point and a
    future reconnect-aware wrapper can decide whether resume is safe.
    """

    CHANNEL_WRITES = "channel_writes"
    LORA_CONFIG = "lora_config"
    DEFERRED_NAMED_ADMIN = "deferred_named_admin"
    DEFERRED_PREVIOUS_ADMIN = "deferred_previous_admin"


@dataclass
class _SetUrlAddOnlyExecutionState:
    """Execution tracking state for addOnly transactions."""

    written_indices: list[int] = field(default_factory=list)
    lora_write_started: bool = False


@dataclass
class _SetUrlReplaceExecutionState:
    """Execution tracking state for replace-all transactions."""

    written_channel_indices: list[int] = field(default_factory=list)
    lora_write_started: bool = False
    stage: _ReplaceAllStage | None = None


class _SetUrlExecutionEngine:
    """Owns remote write ordering/admin-index strategy for setURL transactions."""

    def __init__(
        self,
        node: Node,
        port: _NodePort | None = None,
        *,
        channel_state: _NodeChannelState,
        cache_manager: _SetUrlCacheManager,
    ) -> None:
        """Initialize the execution engine with its facade and collaborator port.

        Parameters
        ----------
        node : Node
            The facade whose public state the engine reads.
        port : _NodePort | None
            Narrow port over the facade's internals; derived from ``node``
            when omitted.
        channel_state : _NodeChannelState
            Shared channel-state owner.
        cache_manager : _SetUrlCacheManager
            Local cache update owner for the transaction.
        """
        self._node = node
        self._port = port if port is not None else _NodePort(node)
        self._channel_state = channel_state
        self._cache_manager = cache_manager

    @staticmethod
    def _safe_channel_role_name(role: int) -> str:
        """Return a safe channel role label for logging."""
        if role in channel_pb2.Channel.Role.values():
            return channel_pb2.Channel.Role.Name(role)  # type: ignore[arg-type]
        return f"UNKNOWN({role})"

    def _write_lora_config(
        self,
        parsed_input: _SetUrlParsedInput,
        admin_context: _SetUrlAdminContext,
        state: _SetUrlAddOnlyExecutionState | _SetUrlReplaceExecutionState,
    ) -> None:
        """Write LoRa configuration to the device."""
        if not parsed_input.has_lora_update:
            return
        set_lora = admin_pb2.AdminMessage()
        set_lora.set_config.lora.CopyFrom(parsed_input.channel_set.lora_config)
        self._node.ensureSessionKey(adminIndex=admin_context.admin_index_for_write)
        request = self._port.send_admin(
            set_lora,
            admin_index=admin_context.admin_index_for_write,
        )
        if request is None:
            self._port.raise_interface_error("LoRa config update was not started")
        state.lora_write_started = True

    def _validate_replace_cache(
        self,
        expected_channels_ref: list[channel_pb2.Channel],
        expected_channels_fingerprint: tuple[bytes, ...],
    ) -> None:
        """Abort when channel state drifts from a replace transaction baseline."""
        with self._channel_state.lock:
            channels = self._channel_state.channels
            channels_changed = channels is not expected_channels_ref
            if not channels_changed:
                channels_changed = (
                    _channels_fingerprint(expected_channels_ref)
                    != expected_channels_fingerprint
                )
        if channels_changed:
            self._cache_manager.invalidate_channel_cache(
                "Channel cache changed before replace-all write; invalidating local channel cache."
            )
            self._port.raise_interface_error(
                "Channel cache changed before replace-all write; aborting transaction."
            )

    def _capture_replace_cache(
        self,
    ) -> tuple[list[channel_pb2.Channel], tuple[bytes, ...]]:
        """Capture the current cache guard for a resumed replace transaction."""
        with self._channel_state.lock:
            channels = self._channel_state.channels
            if channels is not None:
                return channels, _channels_fingerprint(channels)
        return self._port.raise_interface_error("Config or channels not loaded")

    def _write_replace_channel(
        self,
        staged_channel: channel_pb2.Channel,
        *,
        admin_index: int | None,
        state: _SetUrlReplaceExecutionState,
        expected_channels_ref: list[channel_pb2.Channel],
        expected_channels_fingerprint: tuple[bytes, ...],
    ) -> tuple[bytes, ...]:
        """Write one channel without the state lock, then commit it atomically."""
        self._validate_replace_cache(
            expected_channels_ref,
            expected_channels_fingerprint,
        )
        self._port.write_channel_snapshot(
            staged_channel,
            admin_index=admin_index,
        )
        state.written_channel_indices.append(staged_channel.index)
        return self._cache_manager.apply_replace_channel_write(
            staged_channel,
            expected_channels_ref=expected_channels_ref,
            expected_channels_fingerprint=expected_channels_fingerprint,
        )

    def execute_add_only(
        self,
        *,
        parsed_input: _SetUrlParsedInput,
        admin_context: _SetUrlAdminContext,
        plan: _SetUrlAddOnlyPlan,
        state: _SetUrlAddOnlyExecutionState,
    ) -> None:
        """Execute addOnly writes in transactional order."""
        written_count = 0
        for staged_channel, channel_name in plan.channels_to_write:
            if (
                plan.deferred_add_only_admin_channel is not None
                and staged_channel.index
                == plan.deferred_add_only_admin_channel[0].index
            ):
                continue
            if written_count > 0:
                time.sleep(CHANNEL_WRITE_PACE_SECONDS)
            logger.info("Adding new channel '%s' to device", channel_name)
            self._port.write_channel_snapshot(
                staged_channel,
                admin_index=admin_context.admin_index_for_write,
            )
            state.written_indices.append(staged_channel.index)
            written_count += 1

        if parsed_input.has_lora_update and written_count > 0:
            time.sleep(LORA_WRITE_SETTLE_SECONDS)
        self._write_lora_config(parsed_input, admin_context, state)

        if plan.deferred_add_only_admin_channel is not None:
            if written_count > 0 or state.lora_write_started:
                time.sleep(DEFERRED_ADMIN_PACE_SECONDS)
            staged_channel, channel_name = plan.deferred_add_only_admin_channel
            logger.info("Adding new channel '%s' to device", channel_name)
            self._port.write_channel_snapshot(
                staged_channel,
                admin_index=admin_context.admin_index_for_write,
            )
            state.written_indices.append(staged_channel.index)

    def execute_replace_all(
        self,
        *,
        parsed_input: _SetUrlParsedInput,
        admin_context: _SetUrlAdminContext,
        plan: _SetUrlReplacePlan,
        state: _SetUrlReplaceExecutionState,
        skip_channel_indices: set[int] | None = None,
        skip_lora: bool = False,
    ) -> None:
        """Execute replace-all writes in transactional order.

        The write sequence proceeds through four stages tracked in
        *state.stage*: channel writes, LoRa config, deferred named
        admin channel, and deferred previous admin slot.  Each stage
        is set before the first write of that stage so that an error
        handler can report exactly where the sequence stopped.
        """
        deferred_channel_indexes = {
            channel.index
            for channel in (
                plan.deferred_new_named_admin_channel,
                plan.deferred_previous_admin_slot_channel,
            )
            if channel is not None
        }
        if skip_channel_indices is None:
            cache_ref = plan.replace_original_channels_ref
            cache_fingerprint = plan.replace_original_channels_fingerprint
        else:
            cache_ref, cache_fingerprint = self._capture_replace_cache()
        state.stage = _ReplaceAllStage.CHANNEL_WRITES
        written_count = 0
        for staged_channel in plan.staged_channels:
            if staged_channel.index in deferred_channel_indexes:
                continue
            if (
                skip_channel_indices is not None
                and staged_channel.index in skip_channel_indices
            ):
                continue
            if written_count > 0:
                time.sleep(CHANNEL_WRITE_PACE_SECONDS)
            logger.debug(
                "Writing channel index=%s role=%s name=%s",
                staged_channel.index,
                self._safe_channel_role_name(staged_channel.role),
                staged_channel.settings.name if staged_channel.settings else None,
            )
            cache_fingerprint = self._write_replace_channel(
                staged_channel,
                admin_index=admin_context.admin_index_for_write,
                state=state,
                expected_channels_ref=cache_ref,
                expected_channels_fingerprint=cache_fingerprint,
            )
            written_count += 1

        state.stage = _ReplaceAllStage.LORA_CONFIG
        if not skip_lora:
            if parsed_input.has_lora_update:
                time.sleep(LORA_WRITE_SETTLE_SECONDS)
            self._write_lora_config(parsed_input, admin_context, state)
            if parsed_input.has_lora_update:
                self._cache_manager.apply_lora_success(
                    parsed_input.channel_set.lora_config
                )

        if plan.deferred_new_named_admin_channel is not None:
            if (
                skip_channel_indices is not None
                and plan.deferred_new_named_admin_channel.index in skip_channel_indices
            ):
                pass
            else:
                state.stage = _ReplaceAllStage.DEFERRED_NAMED_ADMIN
                time.sleep(DEFERRED_ADMIN_PACE_SECONDS)
                logger.debug(
                    "Writing deferred admin channel index=%s role=%s name=%s",
                    plan.deferred_new_named_admin_channel.index,
                    self._safe_channel_role_name(
                        plan.deferred_new_named_admin_channel.role
                    ),
                    (
                        plan.deferred_new_named_admin_channel.settings.name
                        if plan.deferred_new_named_admin_channel.settings
                        else None
                    ),
                )
                cache_fingerprint = self._write_replace_channel(
                    plan.deferred_new_named_admin_channel,
                    admin_index=admin_context.admin_index_for_write,
                    state=state,
                    expected_channels_ref=cache_ref,
                    expected_channels_fingerprint=cache_fingerprint,
                )

        if plan.deferred_previous_admin_slot_channel is not None:
            if (
                skip_channel_indices is not None
                and plan.deferred_previous_admin_slot_channel.index
                in skip_channel_indices
            ):
                pass
            else:
                state.stage = _ReplaceAllStage.DEFERRED_PREVIOUS_ADMIN
                time.sleep(DEFERRED_ADMIN_PACE_SECONDS)
                updated_admin_index_for_write = admin_context.admin_index_for_write
                if plan.deferred_new_named_admin_channel is not None:
                    updated_admin_index_for_write = (
                        admin_context.admin_write_node._get_admin_channel_index()  # noqa: SLF001
                    )
                logger.debug(
                    "Rewriting deferred admin slot i:%s via admin index %s",
                    plan.deferred_previous_admin_slot_channel.index,
                    updated_admin_index_for_write,
                )
                self._write_replace_channel(
                    plan.deferred_previous_admin_slot_channel,
                    admin_index=updated_admin_index_for_write,
                    state=state,
                    expected_channels_ref=cache_ref,
                    expected_channels_fingerprint=cache_fingerprint,
                )
