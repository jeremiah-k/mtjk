"""Request ownership and serialization for embedded command invocations."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary

from meshtastic._core_constants import DECODE_ERROR_KEY
from meshtastic._deadline import _current_deadline, _remaining_timeout
from meshtastic.admin_response import _routing_rejection_reason
from meshtastic.errors import RequestRejectedError
from meshtastic.protobuf import mesh_pb2

if TYPE_CHECKING:
    from meshtastic.mesh_interface import MeshInterface

_LOCKS: WeakKeyDictionary[MeshInterface, threading.Lock] = WeakKeyDictionary()
_LOCKS_GUARD = threading.Lock()
_CURRENT_SCOPE: ContextVar[_CommandScope | None] = ContextVar(
    "meshtastic_command", default=None
)


class _CommandScope:
    """Retire only request state allocated by the active command thread."""

    def __init__(self, interface: MeshInterface, output: Callable[[str], None]) -> None:
        self._interface = interface
        self._output = output
        self._deadline = _current_deadline()
        self._requests: dict[int, str | None] = {}
        self._sent_packets: dict[int, mesh_pb2.MeshPacket] = {}
        self._rejection: RequestRejectedError | None = None
        self._closed = False
        self._lock = threading.Lock()

    def _track(
        self,
        request_id: int,
        wait_attribute: str | None,
        *,
        packet: mesh_pb2.MeshPacket | None = None,
    ) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("Command has completed")
            if wait_attribute is not None:
                self._requests[request_id] = wait_attribute
            else:
                self._requests.setdefault(request_id, None)
            if packet is not None:
                self._sent_packets[request_id] = packet

    def _register_handler(self, request_id: int, register: Callable[[], bool]) -> bool:
        """Serialize response registration with closure, including callback rearms."""
        with self._lock:
            if self._closed or not register():
                return False
            self._requests.setdefault(request_id, None)
            return True

    def _bind_callback(
        self, callback: Callable[[dict[str, Any]], Any]
    ) -> Callable[[dict[str, Any]], Any]:
        """Retain invocation output when a response arrives on a reader thread."""
        context = copy_context()

        def invoke(packet: dict[str, Any]) -> Any:
            if self._closed:
                return None
            self._record_routing_rejection(packet)
            return context.run(callback, packet)

        return invoke

    def _record_routing_rejection(self, packet: dict[str, Any]) -> None:
        """Retain admitted routing feedback without changing legacy callbacks."""
        decoded = packet.get("decoded")
        if not isinstance(decoded, dict):
            return
        request_id = decoded.get("requestId")
        if not isinstance(request_id, int):
            return
        reason = _routing_rejection_reason(decoded)
        if reason is None:
            return
        with self._lock:
            if (
                self._closed
                or self._rejection is not None
                or request_id not in self._requests
                or (self._deadline is not None and time.monotonic() >= self._deadline)
            ):
                return
            sent = self._sent_packets.get(request_id)
            if sent is None:
                return
            source = packet.get("from")
            local_node = getattr(self._interface.localNode, "nodeNum", None)
            if not isinstance(source, int) or source not in (
                sent.to,
                local_node if isinstance(local_node, int) else None,
            ):
                return
            self._rejection = RequestRejectedError(
                reason,
                nodeNum=sent.to if sent is not None else None,
                requestId=request_id,
                operation="command",
            )

    def _raise_if_rejected(self) -> None:
        with self._lock:
            rejection = self._rejection
        if rejection is not None:
            raise rejection

    def _on_ack(self, packet: dict[str, Any]) -> None:
        """Complete an owned ACK without changing legacy shared error latches."""
        decoded = packet.get("decoded", {})
        request_id = decoded.get("requestId")
        admin = decoded.get("admin", {})
        if DECODE_ERROR_KEY in admin:
            self._interface._set_wait_error(
                "receivedNak",
                f"Failed to decode admin payload: {admin[DECODE_ERROR_KEY]}",
                request_id=request_id,
            )
            return
        reason = decoded.get("routing", {}).get("errorReason", "NONE")
        if reason not in ("NONE", 0, None):
            self._interface._set_wait_error(
                "receivedNak",
                f"Routing error on response: {reason}",
                request_id=request_id,
            )
        self._interface._mark_wait_acknowledged("receivedNak", request_id=request_id)

    def _wait_for_acks(self) -> None:
        with self._lock:
            requests = tuple(self._requests.items())
        for request_id, attribute in requests:
            if attribute is None or not self._interface._has_active_wait_request(
                attribute, request_id
            ):
                continue
            completed = self._interface._wait_for_request_ack(
                attribute, request_id, timeout_seconds=_remaining_timeout(math.inf)
            )
            self._interface._raise_wait_error_if_present(
                attribute, request_id=request_id
            )
            if not completed:
                raise TimeoutError("Timed out waiting for command acknowledgment")

    def _cleanup(self) -> None:
        with self._lock:
            self._closed = True
            requests = tuple(self._requests.items())
        for request_id, attribute in requests:
            self._interface._retire_wait_request(
                attribute or "receivedNak", request_id=request_id
            )


def _get_command_scope(interface: MeshInterface) -> _CommandScope | None:
    """Return command ownership only for the selected interface in this context."""
    scope = _CURRENT_SCOPE.get()
    return scope if scope is not None and scope._interface is interface else None


@contextmanager
def _command_scope(
    interface: MeshInterface, output: Callable[[str], None]
) -> Iterator[_CommandScope]:
    """Serialize commands on one interface within the caller's timeout budget."""
    if _CURRENT_SCOPE.get() is not None:
        raise RuntimeError("Nested command execution is not supported")
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(interface, threading.Lock())
    if not lock.acquire(
        timeout=min(_remaining_timeout(math.inf), threading.TIMEOUT_MAX)
    ):
        raise TimeoutError("Timed out waiting for another command to finish")
    scope = _CommandScope(interface, output)
    token = _CURRENT_SCOPE.set(scope)
    try:
        _remaining_timeout(math.inf)
        try:
            yield scope
        except Exception as error:
            if not isinstance(error, RequestRejectedError):
                scope._raise_if_rejected()
            raise
        else:
            scope._raise_if_rejected()
    finally:
        try:
            scope._cleanup()
        finally:
            _CURRENT_SCOPE.reset(token)
            lock.release()


def _get_command_scope_for_runtime(runtime: object) -> _CommandScope | None:
    """Match response state without acquiring another interface lock."""
    scope = _CURRENT_SCOPE.get()
    return (
        scope
        if scope is not None and scope._interface._request_wait_runtime is runtime
        else None
    )


def _get_command_output() -> Callable[[str], None] | None:
    """Return a response reporter only while its owning command is active."""
    scope = _CURRENT_SCOPE.get()
    return scope._output if scope is not None and not scope._closed else None
