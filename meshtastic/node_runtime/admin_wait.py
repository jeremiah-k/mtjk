"""Request-scoped wait helpers for remote admin operations."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from meshtastic.protobuf import admin_pb2, mesh_pb2

WAIT_ATTR_NAK = "receivedNak"

if TYPE_CHECKING:
    from meshtastic.node import Node


def _extract_request_id_from_sent_packet(
    node: "Node", request: mesh_pb2.MeshPacket | None
) -> int | None:
    """Return a positive request id from a sent packet when available."""
    if request is None:
        return None
    extract_request_id = getattr(
        node.iface,
        "_extract_request_id_from_sent_packet",
        None,
    )
    if callable(extract_request_id):
        request_id = extract_request_id(request)
        if isinstance(request_id, int) and not isinstance(request_id, bool):
            return request_id if request_id > 0 else None
        return None

    raw_request_id = getattr(request, "id", None)
    if isinstance(raw_request_id, int) and not isinstance(raw_request_id, bool):
        return raw_request_id if raw_request_id > 0 else None
    return None


def _extract_request_id_from_response(
    node: "Node", packet: dict[str, Any]
) -> int | None:
    """Return the request id carried by a decoded response packet when available."""
    extract_request_id = getattr(node.iface, "_extract_request_id_from_packet", None)
    if not callable(extract_request_id):
        return None
    request_id = extract_request_id(packet)
    if isinstance(request_id, int) and not isinstance(request_id, bool):
        return request_id if request_id > 0 else None
    return None


def _mark_admin_wait_acknowledged(node: "Node", request_id: int | None) -> None:
    """Mark a request-scoped admin wait complete while preserving legacy flags."""
    mark_wait_acknowledged = getattr(node.iface, "_mark_wait_acknowledged", None)
    if callable(mark_wait_acknowledged):
        mark_wait_acknowledged(WAIT_ATTR_NAK, request_id=request_id)


def _set_admin_wait_error(
    node: "Node",
    message: str,
    *,
    request_id: int | None,
) -> None:
    """Record an admin wait error for both scoped and legacy wait paths."""
    set_wait_error = getattr(node.iface, "_set_wait_error", None)
    if not callable(set_wait_error):
        return
    set_wait_error(WAIT_ATTR_NAK, message)
    if request_id is not None:
        set_wait_error(WAIT_ATTR_NAK, message, request_id=request_id)


def _record_admin_wait_error_for_packet(
    node: "Node", packet: dict[str, Any], message: str
) -> None:
    """Record one request-scoped admin failure from a response packet."""
    _set_admin_wait_error(
        node,
        message,
        request_id=_extract_request_id_from_response(node, packet),
    )


def _mark_admin_wait_acknowledged_for_packet(
    node: "Node", packet: dict[str, Any]
) -> None:
    """Mark one request-scoped admin wait complete from a response packet."""
    _mark_admin_wait_acknowledged(
        node,
        _extract_request_id_from_response(node, packet),
    )


def _accepts_response_wait_attr(send_admin: Callable[..., Any]) -> bool:
    """Return whether a bound admin sender accepts the private wait keyword."""
    try:
        parameters = inspect.signature(send_admin).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "responseWaitAttr"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _send_admin_with_ack_scope(
    node: "Node",
    message: admin_pb2.AdminMessage,
    *,
    scope_ack: bool,
    **kwargs: Any,
) -> mesh_pb2.MeshPacket | None:
    """Send through the real Node transport with pre-registered ACK scope.

    Instance-level test doubles and compatibility monkeypatches historically
    replace ``Node._send_admin`` with simpler callables that do not accept the
    private ``responseWaitAttr`` keyword. Preserve that seam while enabling
    request-scoped bookkeeping for the real bound method.
    """
    send_admin = node._send_admin  # noqa: SLF001
    scoped_send = _get_bound_interface_helper(node, "_send_data_with_wait")
    if (
        scope_ack
        and getattr(send_admin, "__self__", None) is node
        and scoped_send is not None
        and _accepts_response_wait_attr(send_admin)
    ):
        kwargs["responseWaitAttr"] = WAIT_ATTR_NAK
    return send_admin(message, **kwargs)


def _get_bound_interface_helper(node: "Node", name: str) -> Callable[..., Any] | None:
    """Return a real bound interface helper, excluding loose mock attributes."""
    helper: Callable[..., Any] | None = getattr(node.iface, name, None)
    if callable(helper) and getattr(helper, "__self__", None) is node.iface:
        return helper
    return None


def _scoped_request_id(node: "Node", request: mesh_pb2.MeshPacket | None) -> int | None:
    """Return the request id when scoped wait bookkeeping is active for it."""
    request_id = _extract_request_id_from_sent_packet(node, request)
    if request_id is None:
        return None
    has_active_wait = _get_bound_interface_helper(node, "_has_active_wait_request")
    if has_active_wait is None or not bool(has_active_wait(WAIT_ATTR_NAK, request_id)):
        return None
    return request_id


def _wait_for_admin_ack(node: "Node", request: mesh_pb2.MeshPacket | None) -> None:
    """Wait for the ACK/NAK that belongs to one remote admin request.

    The modern MeshInterface exposes a private request-scoped wait helper. The
    public ``waitForAckNak()`` entrypoint remains unchanged for compatibility,
    and minimal interface doubles without the scoped helper retain that legacy
    fallback.
    """
    scoped_wait = _get_bound_interface_helper(node, "_wait_for_ack_nak")
    scoped_request_id = _scoped_request_id(node, request)
    if scoped_request_id is not None and scoped_wait is not None:
        scoped_wait(scoped_request_id)
        return
    node.iface.waitForAckNak()
