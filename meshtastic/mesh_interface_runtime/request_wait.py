"""Request wait runtime for managing async response handling."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from meshtastic._core_constants import DECODE_ERROR_KEY
from meshtastic._response_types import ResponseHandler
from meshtastic.protobuf import portnums_pb2
from meshtastic.util import Acknowledgment, Timeout

logger = logging.getLogger(__name__)

UNSCOPED_WAIT_REQUEST_ID: int = -1
WAIT_ATTR_POSITION: str = "receivedPosition"
WAIT_ATTR_TELEMETRY: str = "receivedTelemetry"
WAIT_ATTR_TRACEROUTE: str = "receivedTraceRoute"
WAIT_ATTR_WAYPOINT: str = "receivedWaypoint"
WAIT_ATTR_NAK: str = "receivedNak"

LEGACY_UNSCOPED_WAIT_ATTR_BY_PORTNUM: dict[int, str] = {
    portnums_pb2.PortNum.POSITION_APP: WAIT_ATTR_POSITION,
    portnums_pb2.PortNum.TRACEROUTE_APP: WAIT_ATTR_TRACEROUTE,
    portnums_pb2.PortNum.TELEMETRY_APP: WAIT_ATTR_TELEMETRY,
    portnums_pb2.PortNum.WAYPOINT_APP: WAIT_ATTR_WAYPOINT,
}

NO_RESPONSE_FIRMWARE_ERROR: str = (
    "No response from node. At least firmware 2.1.22 is required on the destination node."
)
RESPONSE_WAIT_REQID_ERROR: str = (
    "Internal error: response wait requires a positive packet id."
)

DECODE_FAILED_PREFIX: str = "decode-failed: "
# Placeholder for legacy unscoped wait attribute mapping (currently unused)
RETIRED_WAIT_REQUEST_ID_TTL_SECONDS: float = 60.0
# Generic asynchronous callbacks are bounded independently of short ACK waits.
# Active scoped waits are exempt from this TTL until their normal retirement path.
RESPONSE_HANDLER_TTL_SECONDS: float = 3600.0

# Inbound response packets are classified once per correlation so typed
# (matcher-bound) handlers can treat routing feedback separately from the
# correlated data payload they were registered for.
RESPONSE_CLASS_ROUTING_ACK: str = "routing_ack"
RESPONSE_CLASS_ROUTING_NAK: str = "routing_nak"
RESPONSE_CLASS_DATA: str = "data"


class _RequestWaitRuntime:
    """Owns request/wait bookkeeping, scoped wait semantics, and response correlation.

    This class manages the lifecycle of request/response pairs including:
    - Response handler registration and cleanup
    - Scoped and unscoped wait state tracking
    - Acknowledgment and error correlation
    - Request ID retirement and pruning

    Parameters
    ----------
    lock : threading.RLock
        Shared lock for synchronizing access to wait state.
    get_response_handlers : Callable[[], dict[int, ResponseHandler]]
        Factory that returns the shared response handlers dictionary.
    get_wait_errors : Callable[[], dict[tuple[str, int], str]]
        Factory that returns the shared wait errors dictionary.
    get_wait_acks : Callable[[], set[tuple[str, int]]]
        Factory that returns the shared wait acknowledgments set.
    get_active_wait_request_ids : Callable[[], dict[str, set[int]]]
        Factory that returns the shared active wait request IDs dictionary.
    get_retired_wait_request_ids : Callable[[], dict[str, dict[int, float]]]
        Factory that returns the shared retired wait request IDs dictionary.
    get_acknowledgment : Callable[[], Acknowledgment]
        Factory that returns the shared acknowledgment state object.
    get_timeout : Callable[[], Timeout]
        Factory that returns the shared timeout configuration object.
    retired_wait_ttl_seconds : float
        Time-to-live in seconds for retired request IDs before pruning.
    response_handler_ttl_seconds : float
        Time-to-live in seconds for managed response callbacks before pruning.
    """

    def __init__(
        self,
        *,
        lock: threading.RLock,
        get_response_handlers: Callable[[], dict[int, ResponseHandler]],
        get_wait_errors: Callable[[], dict[tuple[str, int], str]],
        get_wait_acks: Callable[[], set[tuple[str, int]]],
        get_active_wait_request_ids: Callable[[], dict[str, set[int]]],
        get_retired_wait_request_ids: Callable[[], dict[str, dict[int, float]]],
        get_acknowledgment: Callable[[], Acknowledgment],
        get_timeout: Callable[[], Timeout],
        retired_wait_ttl_seconds: float,
        response_handler_ttl_seconds: float = RESPONSE_HANDLER_TTL_SECONDS,
    ) -> None:
        self._lock = lock
        self._get_response_handlers = get_response_handlers
        self._get_wait_errors = get_wait_errors
        self._get_wait_acks = get_wait_acks
        self._get_active_wait_request_ids = get_active_wait_request_ids
        self._get_retired_wait_request_ids = get_retired_wait_request_ids
        self._get_acknowledgment = get_acknowledgment
        self._get_timeout = get_timeout
        self._retired_wait_ttl_seconds = retired_wait_ttl_seconds
        self._response_handler_ttl_seconds = response_handler_ttl_seconds
        self._ack_nak_handlers: dict[int, bool] = {}
        self._response_matchers: dict[int, Callable[[dict[str, Any]], bool]] = {}
        self._response_feedback_matchers: dict[
            int, Callable[[dict[str, Any]], bool]
        ] = {}
        self._response_handler_registered_at: dict[int, float] = {}
        self._managed_response_handlers: dict[int, ResponseHandler] = {}

    def mark_ack_nak_handler(self, request_id: int, *, flag: bool = True) -> None:
        """Mark or unmark a request_id as an ACK/NAK handler."""
        with self._lock:
            if flag:
                self._ack_nak_handlers[request_id] = True
            else:
                self._ack_nak_handlers.pop(request_id, None)

    def add_response_handler(
        self,
        request_id: int,
        callback: Callable[[dict[str, Any]], Any],
        *,
        ack_permitted: bool,
        is_ack_nak_handler: bool = False,
        matcher: Callable[[dict[str, Any]], bool] | None = None,
        feedback_matcher: Callable[[dict[str, Any]], bool] | None = None,
        reject_if_registered: bool = False,
    ) -> bool:
        """Register a managed response callback for a request id.

        Returns ``False`` without changing state when ``reject_if_registered``
        is true and the request id already belongs to a live handler.
        """
        now = time.monotonic()
        with self._lock:
            self._prune_stale_response_handlers_locked(now=now)
            response_handlers = self._get_response_handlers()
            if reject_if_registered and request_id in response_handlers:
                return False
            response_handler = ResponseHandler(
                callback=callback,
                ackPermitted=ack_permitted,
            )
            response_handlers[request_id] = response_handler
            self._managed_response_handlers[request_id] = response_handler
            self._response_handler_registered_at[request_id] = now
            if is_ack_nak_handler:
                self._ack_nak_handlers[request_id] = True
            else:
                self._ack_nak_handlers.pop(request_id, None)
            if matcher is not None:
                self._response_matchers[request_id] = matcher
            else:
                self._response_matchers.pop(request_id, None)
            if feedback_matcher is not None:
                self._response_feedback_matchers[request_id] = feedback_matcher
            else:
                self._response_feedback_matchers.pop(request_id, None)
        return True

    def drop_response_handler(self, request_id: int) -> None:
        """Remove a response callback registration if present."""
        with self._lock:
            self._remove_response_handler_locked(request_id)

    def clear_response_handlers(self) -> None:
        """Remove all response callbacks and associated managed metadata."""
        with self._lock:
            self._get_response_handlers().clear()
            self._ack_nak_handlers.clear()
            self._response_matchers.clear()
            self._response_feedback_matchers.clear()
            self._response_handler_registered_at.clear()
            self._managed_response_handlers.clear()

    def prune_stale_response_handlers(self, *, now: float | None = None) -> list[int]:
        """Remove expired managed callbacks that are not part of an active wait."""
        prune_time = time.monotonic() if now is None else now
        with self._lock:
            return self._prune_stale_response_handlers_locked(now=prune_time)

    def _prune_stale_response_handlers_locked(self, *, now: float) -> list[int]:
        """Prune expired managed callbacks while the response-state lock is held."""
        response_handlers = self._get_response_handlers()
        for request_id, managed_handler in list(
            self._managed_response_handlers.items()
        ):
            if response_handlers.get(request_id) is not managed_handler:
                self._clear_managed_response_metadata_locked(request_id)

        cutoff = now - self._response_handler_ttl_seconds
        active_request_ids = {
            request_id
            for request_ids in self._get_active_wait_request_ids().values()
            for request_id in request_ids
        }
        stale_request_ids = [
            request_id
            for request_id, registered_at in self._response_handler_registered_at.items()
            if registered_at <= cutoff and request_id not in active_request_ids
        ]
        for request_id in stale_request_ids:
            self._remove_response_handler_locked(request_id)
        if stale_request_ids:
            logger.debug(
                "Pruned %d stale response handler(s): %s",
                len(stale_request_ids),
                stale_request_ids,
            )
        return stale_request_ids

    def _response_handler_is_stale_locked(self, request_id: int, *, now: float) -> bool:
        """Return whether one managed callback is expired and not actively awaited."""
        registered_at = self._response_handler_registered_at.get(request_id)
        if registered_at is None:
            return False
        active_request_ids = self._get_active_wait_request_ids()
        if any(request_id in ids for ids in active_request_ids.values()):
            return False
        return now - registered_at > self._response_handler_ttl_seconds

    def _clear_managed_response_metadata_locked(self, request_id: int) -> None:
        """Forget runtime-owned metadata without touching a legacy replacement."""
        self._ack_nak_handlers.pop(request_id, None)
        self._response_matchers.pop(request_id, None)
        self._response_feedback_matchers.pop(request_id, None)
        self._response_handler_registered_at.pop(request_id, None)
        self._managed_response_handlers.pop(request_id, None)

    def _remove_response_handler_locked(
        self, request_id: int
    ) -> ResponseHandler | None:
        """Remove one response registration while the response-state lock is held."""
        response_handler = self._get_response_handlers().pop(request_id, None)
        self._clear_managed_response_metadata_locked(request_id)
        return response_handler

    def clear_wait_error(
        self,
        acknowledgment_attr: str,
        request_id: int | None = None,
        *,
        clear_scoped: bool = True,
    ) -> None:
        """Clear scoped/unscoped wait state."""
        with self._lock:
            wait_errors = self._get_wait_errors()
            wait_acks = self._get_wait_acks()
            active_wait_request_ids = self._get_active_wait_request_ids()
            if request_id is None:
                if clear_scoped:
                    for key in list(wait_errors):
                        if key[0] == acknowledgment_attr:
                            wait_errors.pop(key, None)
                    for key in list(wait_acks):
                        if key[0] == acknowledgment_attr:
                            wait_acks.discard(key)
                    active_wait_request_ids.pop(acknowledgment_attr, None)
                else:
                    wait_errors.pop(
                        (acknowledgment_attr, UNSCOPED_WAIT_REQUEST_ID), None
                    )
                    wait_acks.discard(
                        (acknowledgment_attr, UNSCOPED_WAIT_REQUEST_ID),
                    )
                self.prune_retired_wait_request_ids_locked(acknowledgment_attr)
            else:
                active_ids = active_wait_request_ids.setdefault(
                    acknowledgment_attr, set()
                )
                wait_errors.pop((acknowledgment_attr, UNSCOPED_WAIT_REQUEST_ID), None)
                wait_acks.discard((acknowledgment_attr, UNSCOPED_WAIT_REQUEST_ID))
                active_ids.add(request_id)
                retired_ids = self.prune_retired_wait_request_ids_locked(
                    acknowledgment_attr
                )
                retired_ids.pop(request_id, None)
                wait_errors.pop((acknowledgment_attr, request_id), None)
                wait_acks.discard((acknowledgment_attr, request_id))
        if request_id is None:
            setattr(self._get_acknowledgment(), acknowledgment_attr, False)

    def prune_retired_wait_request_ids_locked(
        self,
        acknowledgment_attr: str,
    ) -> dict[int, float]:
        """Prune expired retired ids. Caller must hold the response-handler lock."""
        retired_wait_request_ids = self._get_retired_wait_request_ids()
        retired_ids = retired_wait_request_ids.get(acknowledgment_attr)
        if not retired_ids:
            return {}
        now = time.monotonic()
        for retired_id, retired_at in list(retired_ids.items()):
            if now - retired_at > self._retired_wait_ttl_seconds:
                retired_ids.pop(retired_id, None)
        if not retired_ids:
            retired_wait_request_ids.pop(acknowledgment_attr, None)
            return {}
        return retired_ids

    def _resolve_wait_outcome_locked(
        self,
        acknowledgment_attr: str,
        request_id: int | None,
        *,
        description: str,
    ) -> tuple[int | None, bool]:
        """Resolve the wait slot an ack/error belongs to; caller must hold the lock.

        Parameters
        ----------
        acknowledgment_attr : str
            Acknowledgment attribute scope the outcome belongs to.
        request_id : int | None
            Request-scoped id, or None for a legacy unscoped outcome.
        description : str
            Human-readable outcome label used in stale/retired debug logs.

        Returns
        -------
        tuple[int | None, bool]
            The resolved request id (None when the outcome must be ignored)
            and whether the legacy unscoped acknowledgment flag should be set.
        """
        active_request_ids_for_attr = self._get_active_wait_request_ids().get(
            acknowledgment_attr
        )
        has_request_scope = active_request_ids_for_attr is not None
        active_request_ids = active_request_ids_for_attr or set()
        if request_id is not None:
            if request_id in active_request_ids:
                resolved_request_id = request_id
            elif has_request_scope:
                logger.debug(
                    "Ignoring stale %s for %s request_id=%s (active=%s)",
                    description,
                    acknowledgment_attr,
                    request_id,
                    sorted(active_request_ids),
                )
                return None, False
            else:
                retired_request_ids = self.prune_retired_wait_request_ids_locked(
                    acknowledgment_attr
                )
                if request_id in retired_request_ids:
                    logger.debug(
                        "Ignoring retired scoped %s for %s request_id=%s",
                        description,
                        acknowledgment_attr,
                        request_id,
                    )
                    return None, False
                resolved_request_id = UNSCOPED_WAIT_REQUEST_ID
        elif has_request_scope:
            logger.debug(
                "Ignoring stale unscoped %s for %s while scoped waits are active: %s",
                description,
                acknowledgment_attr,
                sorted(active_request_ids),
            )
            return None, False
        else:
            resolved_request_id = UNSCOPED_WAIT_REQUEST_ID
        return resolved_request_id, not has_request_scope

    def set_wait_error(
        self,
        acknowledgment_attr: str,
        message: str,
        *,
        request_id: int | None = None,
    ) -> None:
        """Record wait errors using scoped/unscoped compatibility rules."""
        with self._lock:
            resolved_request_id, set_legacy_ack_flag = (
                self._resolve_wait_outcome_locked(
                    acknowledgment_attr,
                    request_id,
                    description="wait error",
                )
            )
            if resolved_request_id is None:
                return
            self._get_wait_errors()[
                (acknowledgment_attr, resolved_request_id)
            ] = message
        if set_legacy_ack_flag:
            setattr(self._get_acknowledgment(), acknowledgment_attr, True)

    def mark_wait_acknowledged(
        self,
        acknowledgment_attr: str,
        *,
        request_id: int | None = None,
    ) -> None:
        """Mark wait acknowledgments using scoped/unscoped compatibility rules."""
        with self._lock:
            resolved_request_id, set_legacy_ack_flag = (
                self._resolve_wait_outcome_locked(
                    acknowledgment_attr,
                    request_id,
                    description="acknowledgement",
                )
            )
            if resolved_request_id is None:
                return
            self._get_wait_acks().add((acknowledgment_attr, resolved_request_id))
        if set_legacy_ack_flag:
            setattr(self._get_acknowledgment(), acknowledgment_attr, True)

    def raise_wait_error_if_present(
        self,
        acknowledgment_attr: str,
        *,
        request_id: int | None,
        error_factory: Callable[[str], Exception],
    ) -> None:
        """Raise and consume the pending wait error for a wait scope."""
        with self._lock:
            wait_errors = self._get_wait_errors()
            active_wait_request_ids = self._get_active_wait_request_ids()
            resolved_request_id = (
                request_id if request_id is not None else UNSCOPED_WAIT_REQUEST_ID
            )
            error_message = wait_errors.pop(
                (acknowledgment_attr, resolved_request_id), None
            )
            if (
                error_message is None
                and request_id is not None
                and acknowledgment_attr not in active_wait_request_ids
            ):
                error_message = wait_errors.pop(
                    (acknowledgment_attr, UNSCOPED_WAIT_REQUEST_ID),
                    None,
                )
        if error_message is not None:
            raise error_factory(error_message)

    def retire_wait_request(
        self,
        acknowledgment_attr: str,
        *,
        request_id: int | None,
    ) -> None:
        """Retire response handlers and scoped wait state after completion/timeout."""
        with self._lock:
            wait_errors = self._get_wait_errors()
            wait_acks = self._get_wait_acks()
            active_wait_request_ids = self._get_active_wait_request_ids()
            retired_wait_request_ids = self._get_retired_wait_request_ids()

            active_request_ids = active_wait_request_ids.get(acknowledgment_attr, set())
            if request_id is not None:
                if request_id in active_request_ids:
                    active_request_ids.discard(request_id)
                    retired_request_ids = retired_wait_request_ids.setdefault(
                        acknowledgment_attr, {}
                    )
                    retired_request_ids[request_id] = time.monotonic()
                    if not active_request_ids:
                        active_wait_request_ids.pop(acknowledgment_attr, None)
                        wait_errors.pop(
                            (acknowledgment_attr, UNSCOPED_WAIT_REQUEST_ID), None
                        )
                        wait_acks.discard(
                            (acknowledgment_attr, UNSCOPED_WAIT_REQUEST_ID)
                        )
                    else:
                        active_wait_request_ids[acknowledgment_attr] = (
                            active_request_ids
                        )
                self._remove_response_handler_locked(request_id)
                wait_errors.pop((acknowledgment_attr, request_id), None)
                wait_acks.discard((acknowledgment_attr, request_id))
            else:
                if acknowledgment_attr in active_wait_request_ids:
                    for active_request_id in active_request_ids:
                        self._remove_response_handler_locked(active_request_id)
                        wait_errors.pop((acknowledgment_attr, active_request_id), None)
                        wait_acks.discard((acknowledgment_attr, active_request_id))
                    active_wait_request_ids.pop(acknowledgment_attr, None)
                    # Clean up scoped waits (request_id=None)
                    wait_errors.pop(
                        (acknowledgment_attr, UNSCOPED_WAIT_REQUEST_ID), None
                    )
                    wait_acks.discard((acknowledgment_attr, UNSCOPED_WAIT_REQUEST_ID))
                self.prune_retired_wait_request_ids_locked(acknowledgment_attr)
        if request_id is None:
            setattr(self._get_acknowledgment(), acknowledgment_attr, False)

    def has_active_wait_request(
        self, acknowledgment_attr: str, request_id: int
    ) -> bool:
        """Return whether one request id is active for an acknowledgment scope."""
        with self._lock:
            active_request_ids = self._get_active_wait_request_ids().get(
                acknowledgment_attr
            )
            return active_request_ids is not None and request_id in active_request_ids

    def wait_for_request_ack(
        self,
        acknowledgment_attr: str,
        request_id: int,
        *,
        timeout_seconds: float,
    ) -> bool:
        """Poll request-scoped wait state until ACK/error or timeout."""
        deadline = time.monotonic() + timeout_seconds
        timeout = self._get_timeout()
        sleep_interval = max(0.01, float(getattr(timeout, "sleepInterval", 0.1)))
        while time.monotonic() < deadline:
            with self._lock:
                wait_errors = self._get_wait_errors()
                wait_acks = self._get_wait_acks()
                key = (acknowledgment_attr, request_id)
                if key in wait_errors:
                    return True
                if key in wait_acks:
                    wait_acks.discard(key)
                    return True
            time.sleep(sleep_interval)
        return False

    def record_routing_wait_error(
        self,
        *,
        acknowledgment_attr: str,
        routing_error_reason: str | None,
        request_id: int | None = None,
    ) -> None:
        """Map routing errors into shared wait-error state."""
        if routing_error_reason is None or routing_error_reason == "NONE":
            return
        if routing_error_reason == "NO_RESPONSE":
            message = NO_RESPONSE_FIRMWARE_ERROR
        else:
            message = f"Routing error on response: {routing_error_reason}"
        self.set_wait_error(
            acknowledgment_attr,
            message,
            request_id=request_id,
        )

    def record_admin_nak_wait_error(self, *, request_id: int, message: str) -> None:
        """Record a NAK-class admin failure under its literal request id.

        This writes ONLY the ``(WAIT_ATTR_NAK, request_id)`` wait-error key.
        Typed admin getters deliberately do not open request-scoped ACK/NAK
        waits, so they must neither file legacy unscoped errors nor touch the
        global ``receivedNak`` flag; the bounded getter that raises against
        the refusal retires the key through ``retire_wait_request`` when it
        completes. Typed admin decode failures use this same literal-keyed
        policy; untyped decode failures keep the historical scoped/unscoped
        resolution and legacy flag in :meth:`_apply_admin_decode_failure_wait_state`.
        """
        with self._lock:
            self._get_wait_errors()[(WAIT_ATTR_NAK, request_id)] = message

    def correlate_inbound_response(
        self,
        *,
        packet_dict: dict[str, Any],
        skip_response_callback_for_decode_failure: bool,
        extract_request_id: Callable[[dict[str, Any]], int | None],
    ) -> None:
        """Correlate inbound response packets with callbacks and wait-state updates."""
        request_id = extract_request_id(packet_dict)
        if request_id is None:
            return
        logger.debug("Got a response for requestId %s", request_id)

        decoded = packet_dict.get("decoded")
        routing = decoded.get("routing") if isinstance(decoded, dict) else None
        if routing is None:
            response_class = RESPONSE_CLASS_DATA
        elif "errorReason" not in routing or routing["errorReason"] == "NONE":
            response_class = RESPONSE_CLASS_ROUTING_ACK
        else:
            response_class = RESPONSE_CLASS_ROUTING_NAK
        response_handler, dropped_due_to_decode_failure, dropped_handler_was_typed = (
            self._select_response_handler_for_packet(
                request_id=request_id,
                response_class=response_class,
                skip_response_callback_for_decode_failure=(
                    skip_response_callback_for_decode_failure
                ),
                packet_dict=packet_dict,
            )
        )
        if dropped_due_to_decode_failure:
            self._apply_admin_decode_failure_wait_state(
                request_id=request_id,
                packet_dict=packet_dict,
                typed_handler=dropped_handler_was_typed,
            )
        self._invoke_response_callback(
            request_id=request_id,
            response_handler=response_handler,
            packet_dict=packet_dict,
        )

    def _select_response_handler_for_packet(
        self,
        *,
        request_id: int,
        response_class: str,
        skip_response_callback_for_decode_failure: bool,
        packet_dict: dict[str, Any],
    ) -> tuple[ResponseHandler | None, bool, bool]:
        """Select/pop a response handler from shared state for one packet."""
        is_ack = response_class == RESPONSE_CLASS_ROUTING_ACK
        response_handler: ResponseHandler | None = None
        dropped_due_to_decode_failure = False
        dropped_handler_was_typed = False
        with self._lock:
            response_handlers = self._get_response_handlers()
            candidate = response_handlers.get(request_id, None)
            if candidate is not None:
                managed_handler = self._managed_response_handlers.get(request_id)
                if managed_handler is not None and candidate is not managed_handler:
                    self._clear_managed_response_metadata_locked(request_id)
                elif (
                    managed_handler is candidate
                    and self._response_handler_is_stale_locked(
                        request_id, now=time.monotonic()
                    )
                ):
                    self._remove_response_handler_locked(request_id)
                    return None, False, False
                matcher = self._response_matchers.get(request_id)
                feedback_matcher = self._response_feedback_matchers.get(request_id)
                if feedback_matcher is not None and (
                    response_class != RESPONSE_CLASS_DATA
                    or skip_response_callback_for_decode_failure
                ):
                    try:
                        matches_feedback_source = feedback_matcher(packet_dict)
                    except Exception:  # pylint: disable=broad-exception-caught
                        logger.exception(
                            "Response feedback matcher failed for requestId %s; "
                            "ignoring packet",
                            request_id,
                        )
                        return None, False, False
                    if not matches_feedback_source:
                        logger.warning(
                            "Ignoring routing/decode feedback for requestId %s that "
                            "did not match its source contract",
                            request_id,
                        )
                        return None, False, False
                is_ack_nak_handler = (
                    self._ack_nak_handlers.get(request_id, False)
                    or not candidate.ackPermitted
                )
                if skip_response_callback_for_decode_failure and (
                    matcher is not None or not is_ack_nak_handler
                ):
                    # Admin decode failures are handled separately from data
                    # responses: drop the handler instead of evaluating the
                    # decode-error payload against the typed contract. This
                    # must stay reachable for typed (matcher-bound) handlers.
                    removed_handler = self._remove_response_handler_locked(request_id)
                    dropped_due_to_decode_failure = True
                    if matcher is not None:
                        dropped_handler_was_typed = True
                        # Typed handlers receive the decode failure as a
                        # terminal refusal (mirroring routing-NAK delivery)
                        # so bounded getters fail fast instead of waiting.
                        response_handler = removed_handler
                elif (
                    matcher is not None
                    and response_class == RESPONSE_CLASS_ROUTING_ACK
                    and not candidate.ackPermitted
                    and not self._ack_nak_handlers.get(request_id, False)
                ):
                    # A routing ACK is transport feedback, not the requested
                    # protobuf: a typed handler that is not explicitly
                    # ACK-permitted stays pending for the data response. The
                    # explicit components matter here because admin sends
                    # always derive ``is_ack_nak_handler`` as True.
                    logger.debug(
                        "Ignoring routing ACK for typed requestId %s; "
                        "waiting for the correlated data response",
                        request_id,
                    )
                elif (
                    matcher is not None and response_class == RESPONSE_CLASS_ROUTING_NAK
                ):
                    # A routing NAK is not the requested protobuf either, so
                    # the matcher is skipped; consuming the handler and
                    # delivering the NAK lets bounded getters fail fast with
                    # the routing reason instead of waiting out their timeout.
                    response_handler = self._remove_response_handler_locked(request_id)
                else:
                    if response_class == RESPONSE_CLASS_DATA and matcher is not None:
                        try:
                            matches_contract = matcher(packet_dict)
                        except Exception:  # pylint: disable=broad-exception-caught
                            logger.exception(
                                "Response matcher failed for requestId %s; ignoring packet",
                                request_id,
                            )
                            return None, False, False
                        if not matches_contract:
                            logger.warning(
                                "Ignoring response for requestId %s that "
                                "did not match its contract",
                                request_id,
                            )
                            return None, False, False
                    if (not is_ack) or is_ack_nak_handler or candidate.ackPermitted:
                        response_handler = self._remove_response_handler_locked(
                            request_id
                        )
        return (
            response_handler,
            dropped_due_to_decode_failure,
            dropped_handler_was_typed,
        )

    def _apply_admin_decode_failure_wait_state(
        self,
        *,
        request_id: int,
        packet_dict: dict[str, Any],
        typed_handler: bool,
    ) -> None:
        """Convert admin decode failures into typed or legacy wait-error state."""
        logger.warning(
            "Dropping response callback for requestId %s due to admin decode failure.",
            request_id,
        )
        decoded = packet_dict.get("decoded")
        admin_decoded_payload = (
            decoded.get("admin", {}) if isinstance(decoded, dict) else {}
        )
        if isinstance(admin_decoded_payload, dict):
            admin_decode_error = admin_decoded_payload.get(
                DECODE_ERROR_KEY,
                f"{DECODE_FAILED_PREFIX}unknown error",
            )
        else:
            admin_decode_error = f"{DECODE_FAILED_PREFIX}unknown error"
        message = f"Failed to decode admin payload: {admin_decode_error}"
        if typed_handler:
            self.record_admin_nak_wait_error(request_id=request_id, message=message)
            return

        # Untyped decode failures preserve the historical wait-error routing and
        # legacy NAK latch. They have no bounded typed getter that would retire a
        # literal request-keyed error, so do not create one here.
        self.set_wait_error(WAIT_ATTR_NAK, message, request_id=request_id)
        setattr(self._get_acknowledgment(), WAIT_ATTR_NAK, True)

    @staticmethod
    def _invoke_response_callback(
        *,
        request_id: int,
        response_handler: ResponseHandler | None,
        packet_dict: dict[str, Any],
    ) -> None:
        """Invoke one response callback with error isolation."""
        if response_handler is None:
            return
        logger.debug("Calling response handler for requestId %s", request_id)
        try:
            response_handler.callback(packet_dict)
        except Exception:
            logger.exception(
                "Error in response handler for requestId %s",
                request_id,
            )
