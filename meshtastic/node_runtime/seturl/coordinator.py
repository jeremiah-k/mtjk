"""Transaction coordination for setURL transactions."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from meshtastic.node_runtime.channel_state import _NodeChannelState
from meshtastic.node_runtime.ports import _NodePort
from meshtastic.node_runtime.seturl.cache import _SetUrlCacheManager
from meshtastic.node_runtime.seturl.context import _SetUrlAdminContext
from meshtastic.node_runtime.seturl.execution import (
    _ReplaceAllStage,
    _SetUrlAddOnlyExecutionState,
    _SetUrlExecutionEngine,
    _SetUrlReplaceExecutionState,
)
from meshtastic.node_runtime.seturl.helpers import _compute_remaining_channel_writes
from meshtastic.node_runtime.seturl.parser import _SetUrlParsedInput
from meshtastic.node_runtime.seturl.planner import (
    _SetUrlAddOnlyPlanner,
    _SetUrlReplacePlan,
    _SetUrlReplacePlanner,
)

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)

MAX_REPLACE_ALL_RESUME_ATTEMPTS = 3
"""Maximum number of reconnect/resume cycles for replace-all."""

RESUME_RECONNECT_TIMEOUT_SECONDS = 15.0
"""Timeout for waiting for transport reconnect during resume."""

RESUME_STABLE_CONNECTED_SECONDS = 1.5
"""Minimum time transport must remain connected before config reload attempt.

After detecting isConnected, we wait this duration and verify the flag is
still set.  This catches serial flapping where the port opens briefly then
drops again (common after LoRa recalibration triggers device reboot)."""

RESUME_CONFIG_RELOAD_TIMEOUT_SECONDS = 30.0
"""Bounded timeout for config reload during resume.

Replaces the unbounded iface.waitForConfig() (which uses the default 300s
timeout) with a shorter deadline appropriate for reconnect recovery."""

RESUME_RECONNECT_SUB_ATTEMPTS = 3
"""Maximum reconnect sub-attempts within a single resume cycle.

Allows recovery from: connect succeeds -> stream drops -> reconnect succeeds
-> config reload works.  Each sub-attempt includes stability window check."""

LORA_CONFIG_RECONNECT_SETTLE_SECONDS = 1.0
"""Extra settle delay before reconnect after lora_config-stage failure.

LoRa recalibration is expensive on firmware and may trigger radio
recalibration or device reboot.  This delay gives the device time to
settle before we attempt serial reconnect."""


class _SetUrlTransactionCoordinator:
    """Coordinates setURL transaction planning, execution, and cache policy with fail-fast semantics."""

    def __init__(
        self,
        node: "Node",
        port: _NodePort | None = None,
        *,
        parsed_input: _SetUrlParsedInput,
        channel_state: _NodeChannelState,
    ) -> None:
        """Initialize the setURL transaction coordinator.

        Parameters
        ----------
        node : Node
            The node instance to coordinate transactions for.
        port : _NodePort | None
            Narrow port over the facade's internals; derived from ``node``
            when omitted.
        parsed_input : _SetUrlParsedInput
            Parsed setURL input containing channel set and flags.
        """
        self._node = node
        self._port = port if port is not None else _NodePort(node)
        self._channel_state = channel_state
        self._parsed_input = parsed_input
        self._admin_context = self._resolve_admin_context()
        self._cache_manager = _SetUrlCacheManager(
            node, self._port, channel_state=self._channel_state
        )
        self._execution_engine = _SetUrlExecutionEngine(
            node,
            self._port,
            channel_state=self._channel_state,
            cache_manager=self._cache_manager,
        )

    def _resolve_admin_context(self) -> _SetUrlAdminContext:
        """Capture admin-channel write context before staging any transaction writes."""
        admin_write_node = self._node.iface.localNode
        if admin_write_node is None:
            self._port.raise_interface_error("Interface localNode not initialized")
        admin_index_for_write = (
            admin_write_node._get_admin_channel_index()
        )  # noqa: SLF001
        named_admin_index_for_write = (
            admin_write_node._get_named_admin_channel_index()  # noqa: SLF001
        )
        return _SetUrlAdminContext(
            admin_write_node=admin_write_node,
            admin_index_for_write=admin_index_for_write,
            named_admin_index_for_write=named_admin_index_for_write,
            has_admin_write_node_named_admin=(named_admin_index_for_write is not None),
        )

    def _trigger_transport_reconnect(self, iface: object) -> bool:
        """Best-effort reconnect trigger for transports that do not auto-recover."""
        attempt_reconnect = getattr(iface, "_attempt_reconnect", None)
        if callable(attempt_reconnect):
            try:
                reconnect_triggered = bool(attempt_reconnect())
                if reconnect_triggered:
                    is_connected = self._get_is_connected_event(iface)
                    if self._event_is_set(is_connected):
                        logger.info("Transport reconnect hook reported success.")
                        return True
                    logger.info(
                        "Transport reconnect hook triggered; waiting for connection event."
                    )
            except Exception:
                logger.debug("Transport reconnect hook failed.", exc_info=True)

        connect = getattr(iface, "connect", None)
        if callable(connect):
            try:
                logger.info("Triggering interface reconnect via connect()...")
                connect()
                is_connected = getattr(iface, "isConnected", None)
                if is_connected is not None and hasattr(is_connected, "is_set"):
                    return bool(is_connected.is_set())
                return False
            except Exception:
                logger.debug(
                    "Interface connect() reconnect attempt failed.", exc_info=True
                )

        return False

    def _get_is_connected_event(self, iface: Any) -> Any | None:
        """Safely retrieve isConnected event with defensive guards.

        Returns the isConnected attribute only if it exists and has the
        expected Event-like interface (is_set and wait methods).

        Parameters
        ----------
        iface : object
            The mesh interface to check.

        Returns
        -------
        object | None
            The isConnected event if valid, None otherwise.
        """
        is_connected = getattr(iface, "isConnected", None)
        if is_connected is None:
            return None
        if not hasattr(is_connected, "is_set") or not callable(
            getattr(is_connected, "is_set", None)
        ):
            return None
        if not hasattr(is_connected, "wait") or not callable(
            getattr(is_connected, "wait", None)
        ):
            return None
        return is_connected

    def _event_is_set(self, event: Any | None) -> bool:
        """Safely check if an event is set.

        Parameters
        ----------
        event : Any | None
            The event to check.

        Returns
        -------
        bool
            True if event is set, False if event is None or not Event-like.
        """
        if event is None:
            return False
        if not hasattr(event, "is_set") or not callable(getattr(event, "is_set", None)):
            return False
        try:
            return bool(event.is_set())
        except Exception:
            return False

    def _event_wait(self, event: Any | None, timeout_seconds: float) -> bool:
        """Safely wait on an event with timeout.

        Parameters
        ----------
        event : Any | None
            The event to wait on.
        timeout_seconds : float
            Timeout in seconds.

        Returns
        -------
        bool
            True if event was set, False if timeout or event not Event-like.
        """
        if event is None:
            return False
        if not hasattr(event, "wait") or not callable(getattr(event, "wait", None)):
            return False
        try:
            return bool(event.wait(timeout_seconds))
        except Exception:
            return False

    def _bounded_config_reload(
        self,
        iface: object,
        deadline: float,
    ) -> bool:
        """Poll for config availability with bounded timeout and liveness check.

        Unlike ``iface.waitForConfig()`` which uses the default 300s timeout,
        this method polls with a caller-supplied deadline and checks
        ``isConnected`` during the poll.  If the transport drops during
        config reload, the method returns ``False`` immediately rather than
        blocking until timeout.

        Parameters
        ----------
        iface : object
            The mesh interface (duck-typed; must have ``isConnected`` Event
            and ``myInfo`` attribute).
        deadline : float
            ``time.monotonic()`` deadline for the poll loop.

        Returns
        -------
        bool
            ``True`` if config appears available, ``False`` if timeout or
            transport dropped.
        """
        is_connected = self._get_is_connected_event(iface)
        while time.monotonic() < deadline:
            if not self._event_is_set(is_connected):
                logger.info("Transport dropped during config reload; aborting poll.")
                return False
            if (
                getattr(iface, "myInfo", None) is not None
                and self._channel_state.has_channels()
            ):
                return True
            time.sleep(0.1)
        logger.info(
            "Config reload timed out after %.1fs.",
            RESUME_CONFIG_RELOAD_TIMEOUT_SECONDS,
        )
        return False

    def _reconnect_and_compute_remaining(
        self,
        plan: _SetUrlReplacePlan,
        failure_stage: _ReplaceAllStage | None = None,
    ) -> tuple[set[int], bool] | None:
        """Reconnect with stability check, reload device state, and compute remaining writes.

        This method implements a bounded reconnect/reload state machine:

        1.  If ``failure_stage`` is ``LORA_CONFIG``, wait a short settle
            delay before attempting reconnect (LoRa recalibration causes
            serial instability).
        2.  Attempt up to ``RESUME_RECONNECT_SUB_ATTEMPTS`` reconnect
            cycles.  Each cycle:
            a. Trigger reconnect if not connected.
            b. Wait for ``isConnected`` with timeout.
            c. **Stability window**: wait ``RESUME_STABLE_CONNECTED_SECONDS``
               and verify ``isConnected`` is still set (catches flapping).
            d. **Bounded config reload**: poll for config with
               ``RESUME_CONFIG_RELOAD_TIMEOUT_SECONDS`` deadline, checking
               ``isConnected`` liveness during the poll.
        3.  If config loads, compute remaining channel/LoRa writes.
        4.  If the reconnect sub-loop exhausts, return ``None``.

        Parameters
        ----------
        plan : _SetUrlReplacePlan
            The replace-all plan with staged channels.
        failure_stage : _ReplaceAllStage | None
            The stage at which the previous attempt failed, used for
            stage-aware settle delays.

        Returns
        -------
        tuple[set[int], bool] | None
            ``(remaining_channel_indices, lora_still_needed)`` if reconnect
            and config reload succeeded, or ``None`` if reconnect failed.
        """
        try:
            return self._reconnect_and_compute_remaining_impl(plan, failure_stage)
        except Exception:
            logger.debug(
                "Reconnect/compute-remaining failed; ignoring diagnostic error.",
                exc_info=True,
            )
            return None

    def _reconnect_and_compute_remaining_impl(
        self,
        plan: _SetUrlReplacePlan,
        failure_stage: _ReplaceAllStage | None = None,
    ) -> tuple[set[int], bool] | None:
        """Reconnect, reload state, and identify replace-all work still pending.

        The caller owns the channel-mutation lock for the complete replace
        transaction. This helper may wait for transport recovery and config
        reloads, but it never acquires the channel-state lock across those
        waits; state comparison is performed only after the device is stable.

        Returns ``None`` when bounded reconnect or config reload cannot
        establish a trustworthy device state.
        """
        if failure_stage == _ReplaceAllStage.LORA_CONFIG:
            logger.info(
                "LoRa-stage failure; settling %.1fs before reconnect.",
                LORA_CONFIG_RECONNECT_SETTLE_SECONDS,
            )
            time.sleep(LORA_CONFIG_RECONNECT_SETTLE_SECONDS)

        iface = self._node.iface

        # Defensively get isConnected event with full guards
        is_connected_event = self._get_is_connected_event(iface)

        for sub_attempt in range(RESUME_RECONNECT_SUB_ATTEMPTS):
            connected = self._event_is_set(is_connected_event)
            if not connected:
                connected = self._trigger_transport_reconnect(iface)
            if not connected:
                logger.info(
                    "Reconnect sub-attempt %d/%d: waiting for transport "
                    "(timeout=%.1fs)...",
                    sub_attempt + 1,
                    RESUME_RECONNECT_SUB_ATTEMPTS,
                    RESUME_RECONNECT_TIMEOUT_SECONDS,
                )
                connected = self._event_wait(
                    is_connected_event, RESUME_RECONNECT_TIMEOUT_SECONDS
                )
            if not connected:
                logger.warning(
                    "Reconnect sub-attempt %d/%d: transport did not reconnect.",
                    sub_attempt + 1,
                    RESUME_RECONNECT_SUB_ATTEMPTS,
                )
                continue

            logger.info(
                "Reconnect sub-attempt %d/%d: transport connected; "
                "verifying stability (%.1fs window)...",
                sub_attempt + 1,
                RESUME_RECONNECT_SUB_ATTEMPTS,
                RESUME_STABLE_CONNECTED_SECONDS,
            )
            time.sleep(RESUME_STABLE_CONNECTED_SECONDS)
            if not self._event_is_set(is_connected_event):
                logger.warning(
                    "Reconnect sub-attempt %d/%d: transport flapped during "
                    "stability window; retrying reconnect.",
                    sub_attempt + 1,
                    RESUME_RECONNECT_SUB_ATTEMPTS,
                )
                continue

            logger.info(
                "Transport stable; reloading device config (timeout=%.1fs)...",
                RESUME_CONFIG_RELOAD_TIMEOUT_SECONDS,
            )
            config_deadline = time.monotonic() + RESUME_CONFIG_RELOAD_TIMEOUT_SECONDS
            config_loaded = self._bounded_config_reload(iface, config_deadline)
            if not config_loaded:
                if not self._event_is_set(is_connected_event):
                    logger.warning(
                        "Reconnect sub-attempt %d/%d: transport dropped "
                        "during config reload; retrying reconnect.",
                        sub_attempt + 1,
                        RESUME_RECONNECT_SUB_ATTEMPTS,
                    )
                    continue
                logger.warning(
                    "Reconnect sub-attempt %d/%d: config reload timed out "
                    "after stable reconnect.",
                    sub_attempt + 1,
                    RESUME_RECONNECT_SUB_ATTEMPTS,
                )
                continue

            with self._channel_state.lock:
                actual_channels = self._channel_state.channels
                if actual_channels is None:
                    logger.warning(
                        "Device channels not available after reconnect; cannot resume."
                    )
                    return None
                remaining_indices = _compute_remaining_channel_writes(
                    actual_channels, plan.staged_channels_by_index
                )
            lora_needed = False
            if self._parsed_input.has_lora_update:
                actual_lora = self._node.localConfig.lora
                lora_needed = (
                    actual_lora.SerializeToString()
                    != self._parsed_input.channel_set.lora_config.SerializeToString()
                )
            if not remaining_indices and not lora_needed:
                logger.info(
                    "Device state already matches desired configuration after "
                    "reconnect; no further writes needed."
                )
                return (set(), False)
            logger.info(
                "After reconnect: %d channel(s) still differ from desired state, "
                "LoRa update %s.",
                len(remaining_indices),
                "still needed" if lora_needed else "already applied",
            )
            if remaining_indices:
                diff_names: list[str] = []
                for idx in sorted(remaining_indices):
                    desired_ch = plan.staged_channels_by_index.get(idx)
                    channel_name = (
                        desired_ch.settings.name
                        if desired_ch and desired_ch.settings
                        else ""
                    )
                    name = channel_name if channel_name else f"index {idx}"
                    diff_names.append(name)
                logger.info(
                    "Channels still needing writes: %s",
                    ", ".join(diff_names),
                )
            return (remaining_indices, lora_needed)

        logger.error(
            "Replace-all resume failed after %d reconnect sub-attempt(s); "
            "transport did not stabilize.",
            RESUME_RECONNECT_SUB_ATTEMPTS,
        )
        return None

    def _apply_add_only(self) -> None:
        """Execute the addOnly setURL transaction pipeline."""
        with self._channel_state.mutation_lock:
            self._admin_context = self._resolve_admin_context()
            self._apply_add_only_locked()

    def _apply_add_only_locked(self) -> None:
        """Execute addOnly while holding the per-node channel-mutation lock."""
        planner = _SetUrlAddOnlyPlanner(
            self._node,
            self._port,
            parsed_input=self._parsed_input,
            admin_context=self._admin_context,
            channel_state=self._channel_state,
        )
        plan = planner.build_plan()
        for ignored_name in plan.ignored_channel_names:
            logger.info(
                'Ignoring existing or empty channel "%s" from add URL',
                ignored_name,
            )
        if not plan.channels_to_write and not self._parsed_input.has_lora_update:
            return
        self._node.ensureSessionKey(
            adminIndex=self._admin_context.admin_index_for_write
        )
        execution_state = _SetUrlAddOnlyExecutionState()
        try:
            self._execution_engine.execute_add_only(
                parsed_input=self._parsed_input,
                admin_context=self._admin_context,
                plan=plan,
                state=execution_state,
            )
        except Exception:
            logger.error(
                "setURL addOnly failed after writing %d channel(s); "
                "device may be partially configured. "
                "Local caches invalidated — reconnect and reload before further operations.",
                len(execution_state.written_indices),
            )
            self._cache_manager.invalidate_channel_cache(
                "Channel cache invalidated after setURL addOnly partial failure."
            )
            if execution_state.lora_write_started:
                self._cache_manager.clear_lora_cache_with_warning(
                    "LoRa config cache cleared after setURL addOnly partial failure; "
                    "reload config before using localConfig.lora."
                )
            raise
        self._cache_manager.apply_add_only_success(
            plan.channels_to_write,
            expected_channels_fingerprint=plan.original_channels_fingerprint,
        )
        if self._parsed_input.has_lora_update:
            self._cache_manager.apply_lora_success(
                self._parsed_input.channel_set.lora_config
            )

    def _apply_replace_all(self) -> None:
        """Execute the replace-all setURL transaction pipeline with bounded resume.

        Plans the write sequence, delegates execution to the engine, and
        invalidates local caches on failure.  If a write failure occurs
        (typically from transport disconnect during a large channel set
        replacement over TCP), the coordinator attempts to reconnect,
        reload actual device state, compute remaining writes, and continue
        only the channels/LoRa config that still differ from desired state.

        Resume is bounded by ``MAX_REPLACE_ALL_RESUME_ATTEMPTS`` total
        attempts (initial + resume retries).  If convergence is not
        achieved within the bound, the final error is raised with
        precise reporting of the failure stage.
        """
        with self._channel_state.mutation_lock:
            self._admin_context = self._resolve_admin_context()
            self._apply_replace_all_locked()

    def _apply_replace_all_locked(self) -> None:
        """Execute replace-all and recovery under the channel-mutation lock."""
        planner = _SetUrlReplacePlanner(
            self._node,
            self._port,
            parsed_input=self._parsed_input,
            admin_context=self._admin_context,
            channel_state=self._channel_state,
        )
        plan = planner.build_plan()
        skip_channel_indices: set[int] = set()
        skip_lora = False
        last_exception: Exception | None = None
        execution_state = _SetUrlReplaceExecutionState()
        attempts_used = 0
        for attempt in range(MAX_REPLACE_ALL_RESUME_ATTEMPTS):
            attempts_used = attempt + 1
            execution_state = _SetUrlReplaceExecutionState()
            resume_skip_channel_indices = skip_channel_indices if attempt > 0 else None
            try:
                self._execution_engine.execute_replace_all(
                    parsed_input=self._parsed_input,
                    admin_context=self._admin_context,
                    plan=plan,
                    state=execution_state,
                    skip_channel_indices=resume_skip_channel_indices,
                    skip_lora=skip_lora,
                )
                return
            except Exception as exc:
                last_exception = exc
                _stage_label = (
                    execution_state.stage.value
                    if execution_state.stage is not None
                    else "unknown"
                )
                logger.warning(
                    "setURL replace-all attempt %d/%d failed at stage '%s'; "
                    "%d channel writes completed before failure.",
                    attempt + 1,
                    MAX_REPLACE_ALL_RESUME_ATTEMPTS,
                    _stage_label,
                    len(execution_state.written_channel_indices),
                )
                if attempt + 1 >= MAX_REPLACE_ALL_RESUME_ATTEMPTS:
                    break
                self._cache_manager.invalidate_channel_cache(
                    "Channel cache invalidated after setURL replace-all partial failure."
                )
                if execution_state.lora_write_started:
                    self._cache_manager.clear_lora_cache_with_warning(
                        "LoRa config cache cleared after setURL replace-all partial failure."
                    )
                resume_result = self._reconnect_and_compute_remaining(
                    plan, failure_stage=execution_state.stage
                )
                if resume_result is None:
                    logger.error(
                        "Could not reconnect after replace-all failure; "
                        "cannot attempt resume."
                    )
                    break
                remaining_indices, lora_needed = resume_result
                if not remaining_indices and not lora_needed:
                    logger.info(
                        "Replace-all resumed successfully: device converged to "
                        "desired state after reconnect (attempt %d).",
                        attempt + 1,
                    )
                    return
                skip_channel_indices = {
                    idx
                    for idx in plan.staged_channels_by_index
                    if idx not in remaining_indices
                }
                skip_lora = not lora_needed
                self._admin_context = self._resolve_admin_context()
                logger.info(
                    "Resuming replace-all (attempt %d/%d): %d channel(s) and %s"
                    "LoRa write remaining.",
                    attempt + 2,
                    MAX_REPLACE_ALL_RESUME_ATTEMPTS,
                    len(remaining_indices),
                    "" if lora_needed else "no ",
                )
        _stage_label = (
            execution_state.stage.value
            if execution_state.stage is not None
            else "unknown"
        )
        logger.error(
            "setURL replace-all failed at stage '%s' after %d attempt(s); "
            "device may be partially configured. Local caches "
            "invalidated — reconnect and reload before further operations.",
            _stage_label,
            attempts_used,
        )
        self._cache_manager.invalidate_channel_cache(
            "Channel cache invalidated after setURL replace-all final failure."
        )
        if execution_state.lora_write_started:
            self._cache_manager.clear_lora_cache_with_warning(
                "LoRa config cache cleared after setURL replace-all final failure; "
                "reload config before using localConfig.lora."
            )
        if last_exception is not None:
            raise last_exception
