"""Regression tests pinning typed ADMIN_APP handler lifetime.

These tests are permanent pins for the correlation contracts in
ADMIN_RESPONSE_CONTRACTS.md as they apply to typed (matcher-bound) admin
getter handlers:

1. A routing ACK leaves the typed handler pending for the correlated data
   response; the handler is consumed exactly once by a matching data packet
   and late duplicates cannot double-deliver.
2. An admin decode failure drops the typed handler, records the NAK-keyed
   wait error plus the legacy NAK latch, and is delivered to the typed
   callback as a terminal refusal instead of being logged as a contract
   mismatch.
3. A routing NAK for the request id fails a bounded typed getter fast with
   the refusal reason: the callback receives the NAK packet as the terminal
   refusal signal, the literal-keyed wait error is retired with the getter,
   and no unscoped error or legacy ``receivedNak`` latch is filed by the
   typed-NAK path.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from types import MethodType
from typing import Any
from unittest.mock import MagicMock

import pytest

from meshtastic import DECODE_ERROR_KEY
from meshtastic.admin_response import contract_for_admin_request
from meshtastic.mesh_interface import MeshInterface
from meshtastic.mesh_interface_runtime.request_wait import (
    DECODE_FAILED_PREFIX,
    UNSCOPED_WAIT_REQUEST_ID,
)
from meshtastic.node import Node
from meshtastic.node_runtime.admin_wait import WAIT_ATTR_NAK
from meshtastic.protobuf import (
    admin_pb2,
    connection_status_pb2,
    mesh_pb2,
)

# Generous bounded-wait baseline for the defect-3 getter. The NAK failure must
# surface through _wait_until's much shorter window; sitting out this full
# timeout is precisely the bug being pinned.
_GETTER_TIMEOUT_SECONDS = 15.0


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 1.0) -> None:
    """Wait until a test predicate becomes true or fail deterministically."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail("timed out waiting for test condition")


def _lora_config_contract() -> Any:
    """Build a typed contract for a remote get_config(LoRa) getter."""
    request = admin_pb2.AdminMessage(
        get_config_request=admin_pb2.AdminMessage.LORA_CONFIG
    )
    contract = contract_for_admin_request(
        request, destination=0x1234, local_node_num=0x9999
    )
    assert contract is not None
    return contract


def _config_response_packet(
    *, request_id: int, source: int, field: str
) -> dict[str, object]:
    """Craft an admin data-response packet dict with a real AdminMessage raw."""
    raw = admin_pb2.AdminMessage()
    response = raw.get_config_response
    getattr(response, field).SetInParent()
    return {
        "from": source,
        "decoded": {
            "requestId": request_id,
            "admin": {"raw": raw},
        },
    }


def _extract_request_id(packet: dict[str, Any]) -> int | None:
    """Extract the request id the way the receive pipeline does."""
    request_id = packet.get("decoded", {}).get("requestId")  # type: ignore[union-attr]
    return int(request_id) if isinstance(request_id, int) else None


def _register_typed_lora_handler(
    iface: MeshInterface, request_id: int, callback: MagicMock
) -> None:
    """Register a typed handler exactly as the production admin send does.

    ``_send_data_with_wait`` registers ``onResponseAckPermitted=False`` (see
    ``meshtastic/mesh_interface_runtime/send_pipeline.py``) and the admin
    transport attaches ``contract.matches`` for known getters (see
    ``meshtastic/node_runtime/transport_runtime/admin.py``).
    """
    iface._request_wait_runtime.add_response_handler(  # noqa: SLF001
        request_id,
        callback,
        ack_permitted=False,
        matcher=_lora_config_contract().matches,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "routing_section",
    [
        pytest.param({"errorReason": "NONE"}, id="error-reason-none"),
        pytest.param({}, id="missing-error-reason"),
    ],
)
def test_routing_ack_does_not_consume_typed_admin_handler(
    routing_section: dict[str, str],
) -> None:
    """A routing ACK must neither consume nor complete a typed getter handler.

    Pinned contract: the ACK leaves the handler pending, the data response is
    delivered to the callback exactly once, and a late duplicate data packet
    cannot double-deliver.
    """
    iface = MeshInterface(noProto=True)
    request_id = 88
    callback = MagicMock()
    _register_typed_lora_handler(iface, request_id, callback)

    ack = {
        "from": 0x1234,
        "decoded": {
            "requestId": request_id,
            "routing": dict(routing_section),
        },
    }
    iface._request_wait_runtime.correlate_inbound_response(  # noqa: SLF001
        packet_dict=ack,
        skip_response_callback_for_decode_failure=False,
        extract_request_id=_extract_request_id,
    )
    callback.assert_not_called()
    assert request_id in iface.responseHandlers

    correct = _config_response_packet(
        request_id=request_id, source=0x1234, field="lora"
    )
    iface._request_wait_runtime.correlate_inbound_response(  # noqa: SLF001
        packet_dict=correct,
        skip_response_callback_for_decode_failure=False,
        extract_request_id=_extract_request_id,
    )
    callback.assert_called_once_with(correct)
    assert request_id not in iface.responseHandlers

    # A late/duplicate data response after consumption must not double-deliver.
    iface._request_wait_runtime.correlate_inbound_response(  # noqa: SLF001
        packet_dict=correct,
        skip_response_callback_for_decode_failure=False,
        extract_request_id=_extract_request_id,
    )
    callback.assert_called_once_with(correct)


@pytest.mark.unit
def test_admin_decode_failure_drops_typed_handler_and_records_nak_wait_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Admin decode failures must drop the handler, NAK the request, and refuse.

    Pinned contract: the handler is dropped, a "Failed to decode admin payload"
    wait error is recorded under the NAK attr for the request id, the legacy
    ``receivedNak`` flag is set, and the decode-failure packet is delivered to
    the typed callback exactly once as a terminal refusal — never as the
    requested payload and never as a contract mismatch.
    """
    iface = MeshInterface(noProto=True)
    request_id = 91
    callback = MagicMock()
    _register_typed_lora_handler(iface, request_id, callback)

    broken = {
        "from": 0x1234,
        "decoded": {
            "requestId": request_id,
            "admin": {DECODE_ERROR_KEY: "decode-failed: malformed payload"},
        },
    }
    with caplog.at_level(logging.DEBUG):
        iface._request_wait_runtime.correlate_inbound_response(  # noqa: SLF001
            packet_dict=broken,
            skip_response_callback_for_decode_failure=True,
            extract_request_id=_extract_request_id,
        )

    # Terminal refusal delivery: exactly once, with the decode-failure packet.
    callback.assert_called_once_with(broken)
    assert request_id not in iface.responseHandlers
    wait_error = iface._response_wait_errors.get(  # noqa: SLF001
        (WAIT_ATTR_NAK, request_id)
    )
    assert wait_error is not None
    assert "Failed to decode admin payload" in wait_error
    assert iface._acknowledgment.receivedNak is True
    assert "did not match its contract" not in caplog.text


@pytest.mark.unit
def test_bounded_typed_getter_fails_fast_on_routing_nak(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A routing NAK for its request id must fail a bounded typed getter fast.

    Pinned observable contract: the getter stops waiting well before its
    bounded timeout and raises ``MeshInterfaceError`` naming the routing error
    reason; the literal-keyed NAK error and the response handler are retired
    with the getter; the typed-NAK path files no unscoped error and no legacy
    ``receivedNak`` latch; and a later valid data response for the same id is
    not delivered to the retired getter.
    """
    with MeshInterface(noProto=True) as iface:
        iface.myInfo = mesh_pb2.MyNodeInfo(my_node_num=1)
        iface.localNode.nodeNum = 1
        iface._get_or_create_by_num = MethodType(  # type: ignore[method-assign]  # noqa: SLF001
            lambda _self, _node_num: {},
            iface,
        )
        remote = Node(iface, 2, noProto=False, timeout=30.0)

        def _send_packet(
            packet: mesh_pb2.MeshPacket, *_args: Any, **_kwargs: Any
        ) -> mesh_pb2.MeshPacket:
            return packet

        monkeypatch.setattr(iface, "_send_packet", _send_packet)

        getter_done = threading.Event()
        outcome: dict[str, object] = {}

        def _run() -> None:
            try:
                outcome["result"] = remote._request_admin_response(  # noqa: SLF001
                    admin_pb2.AdminMessage(get_device_connection_status_request=True),
                    "get_device_connection_status_response",
                    connection_status_pb2.DeviceConnectionStatus,
                    response_timeout_seconds=_GETTER_TIMEOUT_SECONDS,
                )
            except Exception as exc:  # noqa: BLE001 - asserted below
                outcome["error"] = exc
            finally:
                getter_done.set()

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not iface.responseHandlers:
            time.sleep(0.005)
        assert (
            iface.responseHandlers
        ), "bounded getter never registered its response handler"
        request_id = next(iter(iface.responseHandlers))

        nak = {
            "from": remote.nodeNum,
            "decoded": {
                "requestId": request_id,
                "routing": {"errorReason": "NOT_AUTHORIZED"},
            },
        }
        with caplog.at_level(logging.DEBUG):
            iface._request_wait_runtime.correlate_inbound_response(  # noqa: SLF001
                packet_dict=nak,
                skip_response_callback_for_decode_failure=False,
                extract_request_id=iface._extract_request_id_from_packet,  # noqa: SLF001
            )

        # The NAK must be observed promptly: waiting out the full bounded
        # timeout (and returning a bare None) is the defect being pinned.
        _wait_until(getter_done.is_set, timeout=1.5)
        thread.join(timeout=1.0)
        assert not thread.is_alive()

        failure = outcome.get("error")
        assert isinstance(failure, MeshInterface.MeshInterfaceError)
        assert str(failure) == "Routing error on response: NOT_AUTHORIZED"

        # The literal-keyed refusal is retired with the getter, and the
        # typed-NAK path leaves no unscoped error or legacy NAK latch behind.
        assert (
            WAIT_ATTR_NAK,
            request_id,
        ) not in iface._response_wait_errors  # noqa: SLF001
        assert (
            WAIT_ATTR_NAK,
            UNSCOPED_WAIT_REQUEST_ID,
        ) not in iface._response_wait_errors  # noqa: SLF001
        assert iface._acknowledgment.receivedNak is False
        assert request_id not in iface.responseHandlers

        outcome_before_data = dict(outcome)
        raw = admin_pb2.AdminMessage()
        raw.get_device_connection_status_response.SetInParent()
        data = {
            "from": remote.nodeNum,
            "decoded": {
                "requestId": request_id,
                "admin": {"raw": raw},
            },
        }
        iface._request_wait_runtime.correlate_inbound_response(  # noqa: SLF001
            packet_dict=data,
            skip_response_callback_for_decode_failure=False,
            extract_request_id=iface._extract_request_id_from_packet,  # noqa: SLF001
        )
        assert dict(outcome) == outcome_before_data


@pytest.mark.unit
def test_bounded_typed_getter_fails_fast_on_admin_decode_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An admin decode-failure response must fail the bounded getter fast.

    Pinned contract: the decode-failure packet is delivered to the typed
    getter's callback as a terminal refusal, the getter raises
    ``MeshInterfaceError`` naming the decode failure well before its bounded
    timeout, and the literal-keyed wait error plus the response handler are
    retired with the getter.
    """
    with MeshInterface(noProto=True) as iface:
        iface.myInfo = mesh_pb2.MyNodeInfo(my_node_num=1)
        iface.localNode.nodeNum = 1
        iface._get_or_create_by_num = MethodType(  # type: ignore[method-assign]  # noqa: SLF001
            lambda _self, _node_num: {},
            iface,
        )
        remote = Node(iface, 2, noProto=False, timeout=30.0)

        def _send_packet(
            packet: mesh_pb2.MeshPacket, *_args: Any, **_kwargs: Any
        ) -> mesh_pb2.MeshPacket:
            return packet

        monkeypatch.setattr(iface, "_send_packet", _send_packet)

        getter_done = threading.Event()
        outcome: dict[str, object] = {}

        def _run() -> None:
            try:
                outcome["result"] = remote._request_admin_response(  # noqa: SLF001
                    admin_pb2.AdminMessage(get_device_connection_status_request=True),
                    "get_device_connection_status_response",
                    connection_status_pb2.DeviceConnectionStatus,
                    response_timeout_seconds=_GETTER_TIMEOUT_SECONDS,
                )
            except Exception as exc:  # noqa: BLE001 - asserted below
                outcome["error"] = exc
            finally:
                getter_done.set()

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not iface.responseHandlers:
            time.sleep(0.005)
        assert (
            iface.responseHandlers
        ), "bounded getter never registered its response handler"
        request_id = next(iter(iface.responseHandlers))

        broken = {
            "from": remote.nodeNum,
            "decoded": {
                "requestId": request_id,
                "admin": {DECODE_ERROR_KEY: "decode-failed: malformed admin"},
            },
        }
        iface._request_wait_runtime.correlate_inbound_response(  # noqa: SLF001
            packet_dict=broken,
            skip_response_callback_for_decode_failure=True,
            extract_request_id=iface._extract_request_id_from_packet,  # noqa: SLF001
        )

        _wait_until(getter_done.is_set, timeout=1.5)
        thread.join(timeout=1.0)
        assert not thread.is_alive()

        failure = outcome.get("error")
        assert isinstance(failure, MeshInterface.MeshInterfaceError)
        assert "Failed to decode admin payload" in str(failure)
        assert "decode-failed: malformed admin" in str(failure)

        # The refusal state is fully retired with the getter.
        assert (
            WAIT_ATTR_NAK,
            request_id,
        ) not in iface._response_wait_errors  # noqa: SLF001
        assert request_id not in iface.responseHandlers


@pytest.mark.unit
def test_bounded_typed_getter_fails_fast_on_routing_payload_decode_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A routing-payload decode failure must fail the getter via the NAK path.

    The receive pipeline decodes malformed ROUTING_APP payloads into a routing
    dict whose ``errorReason`` carries the decode-failed marker, so the
    classification treats it as a routing NAK: the typed handler is consumed
    and the getter fails fast naming the marker.
    """
    with MeshInterface(noProto=True) as iface:
        iface.myInfo = mesh_pb2.MyNodeInfo(my_node_num=1)
        iface.localNode.nodeNum = 1
        iface._get_or_create_by_num = MethodType(  # type: ignore[method-assign]  # noqa: SLF001
            lambda _self, _node_num: {},
            iface,
        )
        remote = Node(iface, 2, noProto=False, timeout=30.0)

        def _send_packet(
            packet: mesh_pb2.MeshPacket, *_args: Any, **_kwargs: Any
        ) -> mesh_pb2.MeshPacket:
            return packet

        monkeypatch.setattr(iface, "_send_packet", _send_packet)

        getter_done = threading.Event()
        outcome: dict[str, object] = {}

        def _run() -> None:
            try:
                outcome["result"] = remote._request_admin_response(  # noqa: SLF001
                    admin_pb2.AdminMessage(get_device_connection_status_request=True),
                    "get_device_connection_status_response",
                    connection_status_pb2.DeviceConnectionStatus,
                    response_timeout_seconds=_GETTER_TIMEOUT_SECONDS,
                )
            except Exception as exc:  # noqa: BLE001 - asserted below
                outcome["error"] = exc
            finally:
                getter_done.set()

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not iface.responseHandlers:
            time.sleep(0.005)
        assert (
            iface.responseHandlers
        ), "bounded getter never registered its response handler"
        request_id = next(iter(iface.responseHandlers))

        decode_failed_reason = f"{DECODE_FAILED_PREFIX}routing payload malformed"
        broken_routing = {
            "from": remote.nodeNum,
            "decoded": {
                "requestId": request_id,
                "routing": {
                    DECODE_ERROR_KEY: decode_failed_reason,
                    "errorReason": decode_failed_reason,
                },
            },
        }
        iface._request_wait_runtime.correlate_inbound_response(  # noqa: SLF001
            packet_dict=broken_routing,
            skip_response_callback_for_decode_failure=False,
            extract_request_id=iface._extract_request_id_from_packet,  # noqa: SLF001
        )

        _wait_until(getter_done.is_set, timeout=1.5)
        thread.join(timeout=1.0)
        assert not thread.is_alive()

        failure = outcome.get("error")
        assert isinstance(failure, MeshInterface.MeshInterfaceError)
        assert str(failure) == f"Routing error on response: {decode_failed_reason}"

        assert (
            WAIT_ATTR_NAK,
            request_id,
        ) not in iface._response_wait_errors  # noqa: SLF001
        assert request_id not in iface.responseHandlers


@pytest.mark.unit
def test_untyped_handler_still_receives_routing_nak() -> None:
    """An untyped ackPermitted=False handler still consumes a routing NAK.

    Historical delivery: without a matcher there is no typed contract to
    enforce, so the NAK consumes the handler and is delivered to the callback
    exactly as before the typed-aware branches existed.
    """
    iface = MeshInterface(noProto=True)
    request_id = 97
    callback = MagicMock()
    iface._request_wait_runtime.add_response_handler(  # noqa: SLF001
        request_id, callback, ack_permitted=False
    )

    nak = {
        "from": 0x1234,
        "decoded": {
            "requestId": request_id,
            "routing": {"errorReason": "NO_ROUTE"},
        },
    }
    iface._request_wait_runtime.correlate_inbound_response(  # noqa: SLF001
        packet_dict=nak,
        skip_response_callback_for_decode_failure=False,
        extract_request_id=_extract_request_id,
    )

    callback.assert_called_once_with(nak)
    assert request_id not in iface.responseHandlers
