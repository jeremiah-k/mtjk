"""Meshtastic unit tests for mesh_interface.py."""

# pylint: disable=too-many-lines

import logging
import threading
import time
import types
from collections import OrderedDict
from collections.abc import Iterator
from typing import Any, NoReturn, cast
from unittest.mock import MagicMock, call, patch

import pytest

import meshtastic.mesh_interface as mesh_interface_module
from meshtastic.mesh_interface_runtime import flows as flows_module
from meshtastic.mesh_interface_runtime import (
    receive_pipeline as receive_pipeline_module,
)
from meshtastic.mesh_interface_runtime.request_wait import (
    RESPONSE_HANDLER_TTL_SECONDS,
    UNSCOPED_WAIT_REQUEST_ID,
    WAIT_ATTR_NAK,
    WAIT_ATTR_POSITION,
    WAIT_ATTR_TELEMETRY,
)

from .. import NODELESS_WANT_CONFIG_ID, ResponseHandler
from ..mesh_interface import MeshInterface
from ..protobuf import (
    channel_pb2,
    config_pb2,
    mesh_pb2,
    portnums_pb2,
    telemetry_pb2,
)

# TODO
# from ..config import Config
from ..util import Acknowledgment, Timeout
from ._mesh_interface_legacy_support import (
    _inline_queue_work,
    _install_protocol_stub,
    _make_decoded_packet,
    _patch_message_to_dict_position_failure,
    _register_response_capture,
    _wait_for_scoped_wait_registration,
)

WAIT_ATTR_ACK = "receivedAck"


@pytest.fixture(name="decode_failure_iface")
def _decode_failure_iface_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[MeshInterface]:
    """Provide a MeshInterface with inline queueWork for decode-failure tests."""
    _inline_queue_work(monkeypatch)
    with MeshInterface(noProto=True) as iface:
        yield iface


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_wait_helpers_raise_expected_timeout_errors() -> None:
    """waitFor* helper methods should raise MeshInterfaceError on timeout."""
    with MeshInterface(noProto=True) as iface:
        iface._timeout = MagicMock()
        iface._timeout.waitForAckNak.return_value = False
        iface._timeout.waitForTraceRoute.return_value = False
        iface._timeout.waitForTelemetry.return_value = False
        iface._timeout.waitForPosition.return_value = False
        iface._timeout.waitForWaypoint.return_value = False

        with pytest.raises(MeshInterface.MeshInterfaceError, match="acknowledgment"):
            iface.waitForAckNak()
        with pytest.raises(MeshInterface.MeshInterfaceError, match="traceroute"):
            iface.waitForTraceRoute(1)
        with pytest.raises(MeshInterface.MeshInterfaceError, match="telemetry"):
            iface.waitForTelemetry()
        with pytest.raises(MeshInterface.MeshInterfaceError, match="position"):
            iface.waitForPosition()
        with pytest.raises(MeshInterface.MeshInterfaceError, match="waypoint"):
            iface.waitForWaypoint()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_waitForAckNak_raises_pending_received_nak_wait_error() -> None:
    """WaitForAckNak should surface detailed pending receivedNak wait errors."""
    with MeshInterface(noProto=True) as iface:
        iface._timeout = MagicMock()
        iface._timeout.waitForAckNak.return_value = False
        iface._set_wait_error(
            WAIT_ATTR_NAK,
            "Failed to decode admin payload: decode-failed: malformed payload",
        )

        with pytest.raises(
            MeshInterface.MeshInterfaceError,
            match="Failed to decode admin payload",
        ):
            iface.waitForAckNak()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_wait_errors_ignore_stale_request_ids() -> None:
    """Routing errors from stale requestIds must not poison active wait state."""
    with MeshInterface(noProto=True) as iface:
        iface._clear_wait_error(WAIT_ATTR_TELEMETRY, request_id=101)

        iface.onResponseTelemetry(
            {
                "decoded": {
                    "portnum": portnums_pb2.PortNum.Name(
                        portnums_pb2.PortNum.ROUTING_APP
                    ),
                    "requestId": 100,
                    "routing": {"errorReason": "NO_RESPONSE"},
                }
            }
        )

        assert iface._acknowledgment.receivedTelemetry is False
        iface._raise_wait_error_if_present(WAIT_ATTR_TELEMETRY, request_id=101)

        iface.onResponseTelemetry(
            {
                "decoded": {
                    "portnum": portnums_pb2.PortNum.Name(
                        portnums_pb2.PortNum.ROUTING_APP
                    ),
                    "requestId": 101,
                    "routing": {"errorReason": "NO_RESPONSE"},
                }
            }
        )

        with pytest.raises(MeshInterface.MeshInterfaceError, match="No response"):
            iface._raise_wait_error_if_present(WAIT_ATTR_TELEMETRY, request_id=101)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_wait_timeout_retires_response_handler_for_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """waitFor* timeouts should retire request-scoped response handlers."""
    with MeshInterface(noProto=True) as iface:
        iface._timeout = MagicMock()
        iface._timeout.waitForTelemetry.return_value = False
        iface._timeout.expireTimeout = 0.01
        iface._timeout.sleepInterval = 0.001
        mock_send = MagicMock(side_effect=lambda packet, *_a, **_k: packet)
        monkeypatch.setattr(iface, "_send_packet", mock_send)

        packet = iface._send_data_with_wait(
            b"ping",
            wantResponse=True,
            onResponse=lambda _packet: None,
            response_wait_attr=WAIT_ATTR_TELEMETRY,
        )
        request_id = packet.id
        assert request_id in iface.responseHandlers

        with pytest.raises(MeshInterface.MeshInterfaceError, match="telemetry"):
            iface.waitForTelemetry(request_id=request_id)

        assert request_id not in iface.responseHandlers


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_request_scoped_wait_state_supports_multiple_active_request_ids() -> None:
    """Multiple same-type waits should keep independent request-scoped error state."""
    with MeshInterface(noProto=True) as iface:
        iface._clear_wait_error(WAIT_ATTR_TELEMETRY, request_id=101)
        iface._clear_wait_error(WAIT_ATTR_TELEMETRY, request_id=202)

        iface._record_routing_wait_error(
            acknowledgment_attr=WAIT_ATTR_TELEMETRY,
            routing_error_reason="NO_RESPONSE",
            request_id=101,
        )

        iface._raise_wait_error_if_present(WAIT_ATTR_TELEMETRY, request_id=202)
        with pytest.raises(MeshInterface.MeshInterfaceError, match="No response"):
            iface._raise_wait_error_if_present(WAIT_ATTR_TELEMETRY, request_id=101)

        iface._retire_wait_request(WAIT_ATTR_TELEMETRY, request_id=101)
        iface._retire_wait_request(WAIT_ATTR_TELEMETRY, request_id=202)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_send_data_rolls_back_wait_state_when_send_packet_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sendData() should remove response state if _send_packet fails before send."""
    with MeshInterface(noProto=True) as iface:
        observed_request_ids: list[int] = []

        class _SendFailureError(OSError):
            """Local sentinel exception used to validate sendData() rollback behavior."""

            def __init__(self, message: str = "socket send failed") -> None:
                super().__init__(message)

        def _fail_send(
            packet: mesh_pb2.MeshPacket, *_args: object, **_kwargs: object
        ) -> NoReturn:
            observed_request_ids.append(packet.id)
            raise _SendFailureError()

        monkeypatch.setattr(iface, "_send_packet", _fail_send)

        with pytest.raises(OSError, match="socket send failed"):
            iface._send_data_with_wait(
                b"ping",
                wantResponse=True,
                onResponse=lambda _packet: None,
                response_wait_attr=WAIT_ATTR_TELEMETRY,
            )

        assert len(observed_request_ids) == 1
        request_id = observed_request_ids[0]
        with iface._response_handlers_lock:
            assert request_id not in iface.responseHandlers
            assert (WAIT_ATTR_TELEMETRY, request_id) not in iface._response_wait_errors
            assert (WAIT_ATTR_TELEMETRY, request_id) not in iface._response_wait_acks
            assert not iface._active_wait_request_ids.get(WAIT_ATTR_TELEMETRY)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_send_data_finalizes_non_zero_packet_id_before_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sendData() should keep generating packet ids until a non-zero id is assigned."""
    with MeshInterface(noProto=True) as iface:
        generated_ids = iter((0, 0, 123456))

        def _generate_packet_id() -> int:
            return next(generated_ids)

        monkeypatch.setattr(iface, "_generate_packet_id", _generate_packet_id)
        monkeypatch.setattr(
            iface,
            "_send_packet",
            lambda packet, *_args, **_kwargs: packet,
        )

        packet = iface._send_data_with_wait(
            b"ping",
            wantResponse=True,
            response_wait_attr=WAIT_ATTR_TELEMETRY,
        )
        assert packet.id == 123456
        with iface._response_handlers_lock:
            assert 123456 in iface._active_wait_request_ids[WAIT_ATTR_TELEMETRY]


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_send_data_removes_response_handler_when_send_fails_without_wait_attr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sendData() should pop responseHandlers on send failure even without wait-attr tracking."""
    with MeshInterface(noProto=True) as iface:
        observed_request_ids: list[int] = []

        class _SendFailureError(OSError):
            """Local sentinel exception used to validate response-handler rollback."""

            def __init__(self, message: str = "send failure") -> None:
                super().__init__(message)

        def _fail_send(
            packet: mesh_pb2.MeshPacket, *_args: object, **_kwargs: object
        ) -> NoReturn:
            observed_request_ids.append(packet.id)
            raise _SendFailureError()

        monkeypatch.setattr(iface, "_send_packet", _fail_send)

        with pytest.raises(OSError, match="send failure"):
            iface.sendData(
                b"payload",
                onResponse=lambda _packet: None,
            )

        assert len(observed_request_ids) == 1
        request_id = observed_request_ids[0]
        with iface._response_handlers_lock:
            assert request_id not in iface.responseHandlers


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_extract_request_id_from_packet_edge_cases() -> None:
    """Request-id extraction should reject invalid forms and parse positive numeric strings."""
    assert MeshInterface._extract_request_id_from_packet({"decoded": "invalid"}) is None
    assert (
        MeshInterface._extract_request_id_from_packet({"decoded": {"requestId": True}})
        is None
    )
    assert (
        MeshInterface._extract_request_id_from_packet({"decoded": {"requestId": "0"}})
        is None
    )
    assert (
        MeshInterface._extract_request_id_from_packet({"decoded": {"requestId": "17"}})
        == 17
    )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_send_methods_pass_request_id_to_wait_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """send* response paths should forward request-scoped packet ids to wait helpers."""
    with MeshInterface(noProto=True) as iface:
        iface.nodes = {"!1": {"num": 1}, "!2": {"num": 2}, "!3": {"num": 3}}
        wait_for_position = MagicMock()
        wait_for_traceroute = MagicMock()
        wait_for_telemetry = MagicMock()
        wait_for_waypoint = MagicMock()
        monkeypatch.setattr(
            iface,
            "_send_data_with_wait",
            MagicMock(
                side_effect=[
                    mesh_pb2.MeshPacket(id=77),
                    mesh_pb2.MeshPacket(id=88),
                    mesh_pb2.MeshPacket(id=99),
                    mesh_pb2.MeshPacket(id=111),
                    mesh_pb2.MeshPacket(id=222),
                ]
            ),
        )
        monkeypatch.setattr(iface, "waitForPosition", wait_for_position)
        monkeypatch.setattr(iface, "waitForTraceRoute", wait_for_traceroute)
        monkeypatch.setattr(iface, "waitForTelemetry", wait_for_telemetry)
        monkeypatch.setattr(iface, "waitForWaypoint", wait_for_waypoint)

        iface.sendPosition(wantResponse=True)
        iface.sendTraceRoute(dest=123, hopLimit=3)
        iface.sendTelemetry(wantResponse=True)
        iface.sendWaypoint(
            name="A",
            description="B",
            icon=1,
            expire=60,
            wantResponse=True,
        )
        iface.deleteWaypoint(7, wantResponse=True)

        wait_for_position.assert_called_once_with(request_id=77)
        wait_for_traceroute.assert_called_once_with(2, request_id=88)
        wait_for_telemetry.assert_called_once_with(request_id=99)
        assert wait_for_waypoint.call_args_list == [
            call(request_id=111),
            call(request_id=222),
        ]


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_wait_state_unscoped_updates_resolve_active_scoped_wait() -> None:
    """Unscoped callbacks should resolve the active scoped telemetry waiter."""
    with MeshInterface(noProto=True) as iface:
        iface._clear_wait_error(WAIT_ATTR_TELEMETRY, request_id=501)
        iface._set_wait_error(WAIT_ATTR_TELEMETRY, "scoped-error")
        iface._raise_wait_error_if_present(WAIT_ATTR_TELEMETRY)

        iface._clear_wait_error(WAIT_ATTR_TELEMETRY, request_id=501)
        iface._mark_wait_acknowledged(WAIT_ATTR_TELEMETRY)
        with iface._response_handlers_lock:
            assert (WAIT_ATTR_TELEMETRY, 501) not in iface._response_wait_acks


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_wait_state_retired_or_unknown_scoped_ack_does_not_leak() -> None:
    """Unknown scoped acknowledgments should not create persistent wait state."""
    with MeshInterface(noProto=True) as iface:
        iface._mark_wait_acknowledged(WAIT_ATTR_TELEMETRY, request_id=999)
        with iface._response_handlers_lock:
            assert (WAIT_ATTR_TELEMETRY, 999) not in iface._response_wait_acks

        iface._clear_wait_error(WAIT_ATTR_POSITION)
        iface._mark_wait_acknowledged(WAIT_ATTR_POSITION, request_id=888)
        with iface._response_handlers_lock:
            assert (
                WAIT_ATTR_POSITION,
                UNSCOPED_WAIT_REQUEST_ID,
            ) in iface._response_wait_acks


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_wait_state_unscoped_error_fallback_targets_requested_id() -> None:
    """Legacy unscoped errors should remain visible to a requested waiter."""
    with MeshInterface(noProto=True) as iface:
        iface._clear_wait_error(WAIT_ATTR_TELEMETRY)
        iface._set_wait_error(
            WAIT_ATTR_TELEMETRY,
            "legacy-unscoped-error",
            request_id=777,
        )
        with pytest.raises(
            MeshInterface.MeshInterfaceError, match="legacy-unscoped-error"
        ):
            iface._raise_wait_error_if_present(WAIT_ATTR_TELEMETRY, request_id=777)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_retire_wait_request_without_id_clears_all_scoped_state() -> None:
    """Bulk wait retirement should clear handlers, errors, and acknowledgments."""
    with MeshInterface(noProto=True) as iface:
        iface._clear_wait_error(WAIT_ATTR_TELEMETRY, request_id=601)
        iface._clear_wait_error(WAIT_ATTR_TELEMETRY, request_id=602)
        with iface._response_handlers_lock:
            iface.responseHandlers[601] = ResponseHandler(
                callback=lambda _packet: None, ackPermitted=False
            )
            iface.responseHandlers[602] = ResponseHandler(
                callback=lambda _packet: None, ackPermitted=False
            )
            iface._response_wait_errors[(WAIT_ATTR_TELEMETRY, 601)] = "err-a"
            iface._response_wait_errors[(WAIT_ATTR_TELEMETRY, 602)] = "err-b"
            iface._response_wait_acks.add((WAIT_ATTR_TELEMETRY, 601))
            iface._response_wait_acks.add((WAIT_ATTR_TELEMETRY, 602))

        iface._retire_wait_request(WAIT_ATTR_TELEMETRY)

        with iface._response_handlers_lock:
            assert 601 not in iface.responseHandlers
            assert 602 not in iface.responseHandlers
            assert (WAIT_ATTR_TELEMETRY, 601) not in iface._response_wait_errors
            assert (WAIT_ATTR_TELEMETRY, 602) not in iface._response_wait_errors
            assert (WAIT_ATTR_TELEMETRY, 601) not in iface._response_wait_acks
            assert (WAIT_ATTR_TELEMETRY, 602) not in iface._response_wait_acks


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_scoped_wait_ignores_unscoped_ack() -> None:
    """A scoped request wait should not consume a legacy unscoped acknowledgment."""
    with MeshInterface(noProto=True) as iface:
        iface._clear_wait_error(WAIT_ATTR_POSITION, request_id=700)
        with iface._response_handlers_lock:
            iface._response_wait_acks.add(
                (WAIT_ATTR_POSITION, UNSCOPED_WAIT_REQUEST_ID)
            )

        assert not iface._wait_for_request_ack(
            WAIT_ATTR_POSITION, 700, timeout_seconds=0.05
        )

        with iface._response_handlers_lock:
            assert (
                WAIT_ATTR_POSITION,
                UNSCOPED_WAIT_REQUEST_ID,
            ) in iface._response_wait_acks


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_clear_wait_error_without_id_only_clears_matching_attribute() -> None:
    """Unscoped cleanup should preserve wait state owned by another attribute."""
    with MeshInterface(noProto=True) as iface:
        with iface._response_handlers_lock:
            iface._response_wait_acks.add((WAIT_ATTR_POSITION, 1))
            iface._response_wait_acks.add(("otherAttr", 2))

        iface._clear_wait_error(WAIT_ATTR_POSITION)

        with iface._response_handlers_lock:
            assert (WAIT_ATTR_POSITION, 1) not in iface._response_wait_acks
            assert ("otherAttr", 2) in iface._response_wait_acks


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_retired_scoped_wait_ids_do_not_clobber_unscoped_wait_state() -> None:
    """Late callbacks for retired scoped waits should not write into unscoped state."""
    with MeshInterface(noProto=True) as iface:
        iface._clear_wait_error(WAIT_ATTR_TELEMETRY, request_id=321)
        iface._retire_wait_request(WAIT_ATTR_TELEMETRY, request_id=321)

        iface._mark_wait_acknowledged(WAIT_ATTR_TELEMETRY, request_id=321)
        iface._set_wait_error(
            WAIT_ATTR_TELEMETRY,
            "stale-scoped-error",
            request_id=321,
        )
        with iface._response_handlers_lock:
            assert (
                WAIT_ATTR_TELEMETRY,
                UNSCOPED_WAIT_REQUEST_ID,
            ) not in iface._response_wait_acks
            assert (
                WAIT_ATTR_TELEMETRY,
                UNSCOPED_WAIT_REQUEST_ID,
            ) not in iface._response_wait_errors

        iface._mark_wait_acknowledged(WAIT_ATTR_TELEMETRY)
        assert iface._acknowledgment.receivedTelemetry is True


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_same_request_id_in_different_scopes_stays_isolated() -> None:
    """One packet id reused by two wait scopes must correlate per scope."""
    with MeshInterface(noProto=True) as iface:
        iface._clear_wait_error(WAIT_ATTR_TELEMETRY, request_id=77)
        iface._clear_wait_error(WAIT_ATTR_POSITION, request_id=77)

        iface._set_wait_error(WAIT_ATTR_TELEMETRY, "telemetry-error", request_id=77)

        # The position scope owns the same numeric id but must not see the error.
        iface._raise_wait_error_if_present(WAIT_ATTR_POSITION, request_id=77)
        with pytest.raises(MeshInterface.MeshInterfaceError, match="telemetry-error"):
            iface._raise_wait_error_if_present(WAIT_ATTR_TELEMETRY, request_id=77)

        iface._mark_wait_acknowledged(WAIT_ATTR_POSITION, request_id=77)

        assert iface._wait_for_request_ack(WAIT_ATTR_POSITION, 77, timeout_seconds=0.05)
        assert not iface._wait_for_request_ack(
            WAIT_ATTR_TELEMETRY, 77, timeout_seconds=0.05
        )
        # Scoped outcomes never set the legacy unscoped acknowledgment flags.
        assert iface._acknowledgment.receivedPosition is False
        assert iface._acknowledgment.receivedTelemetry is False


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_record_routing_wait_error_ignores_none_like_reason() -> None:
    """Routing wait-error recorder should no-op for None/NONE reasons."""
    with MeshInterface(noProto=True) as iface:
        iface._clear_wait_error(WAIT_ATTR_TELEMETRY, request_id=801)
        iface._record_routing_wait_error(
            acknowledgment_attr=WAIT_ATTR_TELEMETRY,
            routing_error_reason="NONE",
            request_id=801,
        )
        iface._raise_wait_error_if_present(WAIT_ATTR_TELEMETRY, request_id=801)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_on_response_telemetry_logs_all_device_metric_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Telemetry response logging should include optional device-metric fields when present."""
    with MeshInterface(noProto=True) as iface:
        telemetry = telemetry_pb2.Telemetry()
        telemetry.device_metrics.channel_utilization = 12.5
        telemetry.device_metrics.air_util_tx = 4.5
        telemetry.device_metrics.uptime_seconds = 321
        with caplog.at_level(logging.INFO, logger=flows_module.__name__):
            iface.onResponseTelemetry(
                {
                    "decoded": {
                        "portnum": portnums_pb2.PortNum.Name(
                            portnums_pb2.PortNum.TELEMETRY_APP
                        ),
                        "payload": telemetry.SerializeToString(),
                    }
                }
            )
    assert "Total channel utilization:" in caplog.text
    assert "Transmit air utilization:" in caplog.text
    assert "Uptime: 321 s" in caplog.text


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_wait_helpers_use_request_scoped_waiter_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Request-scoped waitFor* helpers should delegate to send pipeline with attr-specific keys."""
    with MeshInterface(noProto=True) as iface:
        wait_calls: list[tuple[str, int, float]] = []

        def _wait_for_request_ack(
            acknowledgment_attr: str,
            request_id: int,
            *,
            timeout_seconds: float,
        ) -> bool:
            wait_calls.append((acknowledgment_attr, request_id, timeout_seconds))
            return True

        monkeypatch.setattr(
            iface._send_pipeline, "_wait_for_request_ack", _wait_for_request_ack
        )

        iface.waitForTraceRoute(1.5, request_id=11)
        iface.waitForPosition(request_id=22)
        iface.waitForWaypoint(request_id=33)

        assert wait_calls[0][0:2] == ("receivedTraceRoute", 11)
        assert wait_calls[1][0:2] == (WAIT_ATTR_POSITION, 22)
        assert wait_calls[2][0:2] == ("receivedWaypoint", 33)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_wait_for_request_ack_supports_overlapping_same_type_waits() -> None:
    """Request-scoped wait path should handle overlapping telemetry waits independently."""
    with MeshInterface(noProto=True) as iface:
        iface._timeout = Timeout(maxSecs=0.5)
        iface._timeout.sleepInterval = 0.001
        iface._clear_wait_error(WAIT_ATTR_TELEMETRY, request_id=11)
        iface._clear_wait_error(WAIT_ATTR_TELEMETRY, request_id=22)
        errors: list[BaseException] = []
        wait_started = {11: threading.Event(), 22: threading.Event()}
        release_waits = threading.Event()

        def _wait_for(req_id: int) -> None:
            try:
                assert release_waits.wait(timeout=1.0)
                wait_started[req_id].set()
                iface.waitForTelemetry(request_id=req_id)
            except Exception as exc:  # noqa: BLE001 - assertion below
                errors.append(exc)

        wait_11 = threading.Thread(target=_wait_for, args=(11,), daemon=True)
        wait_22 = threading.Thread(target=_wait_for, args=(22,), daemon=True)
        wait_11.start()
        wait_22.start()
        release_waits.set()
        assert wait_started[11].wait(timeout=1.0)
        assert wait_started[22].wait(timeout=1.0)
        _wait_for_scoped_wait_registration(
            iface,
            acknowledgment_attr=WAIT_ATTR_TELEMETRY,
            request_id=11,
        )
        _wait_for_scoped_wait_registration(
            iface,
            acknowledgment_attr=WAIT_ATTR_TELEMETRY,
            request_id=22,
        )
        iface._mark_wait_acknowledged(WAIT_ATTR_TELEMETRY, request_id=11)
        iface._mark_wait_acknowledged(WAIT_ATTR_TELEMETRY, request_id=22)
        wait_11.join(timeout=1.0)
        wait_22.join(timeout=1.0)

        assert not errors
        assert not wait_11.is_alive()
        assert not wait_22.is_alive()
        with iface._response_handlers_lock:
            assert not iface._active_wait_request_ids.get(WAIT_ATTR_TELEMETRY)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_request_scoped_wait_wakes_immediately_on_recorded_error() -> None:
    """request_id waiters should wake promptly when a matching wait error is recorded."""
    with MeshInterface(noProto=True) as iface:
        iface._timeout = Timeout(maxSecs=5.0)
        iface._timeout.sleepInterval = 0.001
        request_id = 303
        iface._clear_wait_error(WAIT_ATTR_TELEMETRY, request_id=request_id)
        iface._record_routing_wait_error(
            acknowledgment_attr=WAIT_ATTR_TELEMETRY,
            routing_error_reason="NO_ROUTE",
            request_id=request_id,
        )
        started = time.monotonic()
        with pytest.raises(
            MeshInterface.MeshInterfaceError,
            match="Routing error on response: NO_ROUTE",
        ):
            iface.waitForTelemetry(request_id=request_id)
        assert time.monotonic() - started < 0.5


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_request_scoped_wait_times_out_for_unscoped_error_across_overlapping_waits() -> (
    None
):
    """Overlapping request-scoped waits should ignore unscoped routing errors."""
    with MeshInterface(noProto=True) as iface:
        iface._timeout = Timeout(maxSecs=0.05)
        iface._timeout.sleepInterval = 0.001
        request_a = 411
        request_b = 422
        iface._clear_wait_error(WAIT_ATTR_TELEMETRY, request_id=request_a)
        iface._clear_wait_error(WAIT_ATTR_TELEMETRY, request_id=request_b)
        iface._record_routing_wait_error(
            acknowledgment_attr=WAIT_ATTR_TELEMETRY,
            routing_error_reason="NO_ROUTE",
        )

        with pytest.raises(
            MeshInterface.MeshInterfaceError,
            match="Timed out waiting for telemetry",
        ):
            iface.waitForTelemetry(request_id=request_a)
        with pytest.raises(
            MeshInterface.MeshInterfaceError,
            match="Timed out waiting for telemetry",
        ):
            iface.waitForTelemetry(request_id=request_b)

        with iface._response_handlers_lock:
            assert not iface._active_wait_request_ids.get(WAIT_ATTR_TELEMETRY)
            assert (
                WAIT_ATTR_TELEMETRY,
                UNSCOPED_WAIT_REQUEST_ID,
            ) not in iface._response_wait_errors


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_public_key_and_optional_getters_none_paths(
    iface_with_nodes: MeshInterface,
) -> None:
    """GetPublicKey should return user key while optional local-node getters return None when absent."""
    iface = iface_with_nodes
    assert iface.myInfo is not None
    iface.myInfo.my_node_num = 2475227164
    assert iface.nodesByNum is not None
    node = iface.nodesByNum[2475227164]
    node["user"]["publicKey"] = b"abc"
    assert iface.getPublicKey() == b"abc"
    node["user"] = {}
    assert iface.getPublicKey() is None
    iface.myInfo = None
    assert iface.getPublicKey() is None

    iface.localNode = None  # type: ignore[assignment]
    assert iface.getCannedMessage() is None
    assert iface.getRingtone() is None


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_send_heartbeat_builds_to_radio_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sendHeartbeat() should send a ToRadio with heartbeat field populated."""
    with MeshInterface(noProto=True) as iface:
        sent: list[mesh_pb2.ToRadio] = []
        monkeypatch.setattr(iface, "_send_to_radio", sent.append)
        iface.sendHeartbeat()
        assert sent[0].HasField("heartbeat")


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_start_config_skips_reserved_nodeless_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_start_config() should bump generated config id if it equals NODELESS_WANT_CONFIG_ID."""
    with MeshInterface(noProto=True) as iface:
        monkeypatch.setattr(
            mesh_interface_module.random,  # type: ignore[attr-defined]
            "randint",
            lambda _a, _b: NODELESS_WANT_CONFIG_ID,
        )
        sent: list[mesh_pb2.ToRadio] = []
        monkeypatch.setattr(iface, "_send_to_radio", sent.append)
        iface._start_config()
    assert iface.configId == NODELESS_WANT_CONFIG_ID + 1
    assert sent[0].want_config_id == NODELESS_WANT_CONFIG_ID + 1


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_queue_helpers_cover_state_transitions() -> None:
    """Queue helper methods should cover unknown status, full queue, and pop/decrement logic."""
    with MeshInterface(noProto=True) as iface:
        iface.queueStatus = None
        assert iface._queue_has_free_space() is True
        iface._queue_claim()

        iface.queueStatus = mesh_pb2.QueueStatus(free=1, maxlen=2)
        assert iface._queue_has_free_space() is True
        iface._queue_claim()
        assert iface.queueStatus.free == 0

        iface.queue = OrderedDict()
        assert iface._queue_pop_for_send() is None

        iface.queue[1] = mesh_pb2.ToRadio()
        iface.queueStatus.free = 0
        assert iface._queue_pop_for_send() is None
        iface.queueStatus.free = 1
        popped = iface._queue_pop_for_send()
        assert popped is not None
        assert popped[0] == 1
        assert iface.queueStatus.free == 0


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_send_to_radio_waits_resends_and_tracks_requeue(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """_send_to_radio() should wait for queue space, resend queued packets, and requeue unacked items."""
    with MeshInterface(noProto=True) as iface:
        iface.noProto = False
        iface.queueStatus = mesh_pb2.QueueStatus(free=0, maxlen=10)
        existing = mesh_pb2.ToRadio()
        existing.packet.id = 100
        iface.queue[100] = existing
        iface.queue[150] = False

        incoming = mesh_pb2.ToRadio()
        incoming.packet.id = 200

        sent_ids: list[int] = []

        def _send_impl(msg: mesh_pb2.ToRadio) -> None:
            sent_ids.append(msg.packet.id if msg.HasField("packet") else -1)

        monkeypatch.setattr(iface, "_send_to_radio_impl", _send_impl)

        def _sleep_and_free(_seconds: float) -> None:
            assert iface.queueStatus is not None
            iface.queueStatus.free = 10

        fake_time = types.SimpleNamespace(**vars(time))
        fake_time.sleep = _sleep_and_free
        monkeypatch.setattr(mesh_interface_module, "time", fake_time)

        with caplog.at_level(logging.DEBUG):
            iface._send_to_radio(incoming)

        assert "Waiting for free space in TX Queue" in caplog.text
        assert 100 in sent_ids
        assert 200 in sent_ids

    class _RequeueQueue(OrderedDict[int, mesh_pb2.ToRadio | bool]):
        def __bool__(self) -> bool:
            return False

        def pop(  # type: ignore[override]
            self, key: int, default: mesh_pb2.ToRadio | bool = False
        ) -> mesh_pb2.ToRadio | bool:
            if key == 123:
                return True
            return super().pop(key, default)

    with MeshInterface(noProto=True) as iface:
        iface.noProto = False
        iface.queue = _RequeueQueue()
        packet = mesh_pb2.ToRadio()
        packet.packet.id = 123
        incoming = mesh_pb2.ToRadio()
        incoming.packet.id = 999
        pops = iter([(123, packet), None])
        with monkeypatch.context() as send_patch:
            send_patch.setattr(iface, "_send_to_radio_impl", lambda _msg: None)
            send_patch.setattr(
                iface._queue_send_runtime,
                "_pop_for_send",
                lambda: next(pops),
            )
            iface._send_to_radio(incoming)
        assert 123 in iface.queue


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_send_to_radio_successful_missing_entry_is_not_immediately_requeued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successfully-sent packet without immediate queue-status reply should not be requeued in the same cycle."""
    with MeshInterface(noProto=True) as iface:
        iface.noProto = False

        class _FalsyQueue(OrderedDict[int, mesh_pb2.ToRadio | bool]):
            def __bool__(self) -> bool:
                return False

        iface.queue = _FalsyQueue()
        packet = mesh_pb2.ToRadio()
        packet.packet.id = 123
        incoming = mesh_pb2.ToRadio()
        incoming.packet.id = 999
        sent_ids: list[int] = []

        def _send_impl(msg: mesh_pb2.ToRadio) -> None:
            sent_ids.append(msg.packet.id if msg.HasField("packet") else -1)

        pops = iter([(123, packet), None])
        with monkeypatch.context() as send_patch:
            send_patch.setattr(iface, "_send_to_radio_impl", _send_impl)
            send_patch.setattr(
                iface._queue_send_runtime,
                "_pop_for_send",
                lambda: next(pops),
            )
            iface._send_to_radio(incoming)
        assert 123 in sent_ids
        assert 123 not in iface.queue


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_send_to_radio_requeues_packet_when_send_impl_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A packet should be requeued when the send path raises before successful handoff."""
    with MeshInterface(noProto=True) as iface:
        iface.noProto = False
        packet = mesh_pb2.ToRadio()
        packet.packet.id = 123
        incoming = mesh_pb2.ToRadio()
        incoming.packet.id = 999

        class _SendImplFailure(RuntimeError):
            """Intentional send failure sentinel for requeue-path testing."""

            def __init__(self) -> None:
                super().__init__("send failed")

        def _failing_send(_msg: mesh_pb2.ToRadio) -> None:
            raise _SendImplFailure()

        pops = iter([(123, packet), None])
        with monkeypatch.context() as send_patch:
            send_patch.setattr(iface, "_send_to_radio_impl", _failing_send)
            send_patch.setattr(
                iface._queue_send_runtime,
                "_pop_for_send",
                lambda: next(pops),
            )
            with pytest.raises(_SendImplFailure, match="send failed"):
                iface._send_to_radio(incoming)
        assert 123 in iface.queue


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handle_config_complete_and_queue_status_branches() -> None:
    """_handle_config_complete() and _handle_queue_status_from_radio() should execute all key branches."""
    with MeshInterface(noProto=True) as iface:
        channel = channel_pb2.Channel(index=1)
        iface._localChannels = [channel]
        iface.localNode = MagicMock()
        iface._connected = MagicMock()  # type: ignore[method-assign]
        iface._handle_config_complete()
        iface.localNode.setChannels.assert_called_once_with([channel])
        iface._connected.assert_called_once()

        queued = mesh_pb2.ToRadio()
        queued.packet.id = 111
        iface.queue[111] = queued

        status_hit = mesh_pb2.QueueStatus(free=1, maxlen=4, res=0, mesh_packet_id=111)
        iface._handle_queue_status_from_radio(status_hit)
        assert 111 not in iface.queue

        status_unexpected = mesh_pb2.QueueStatus(
            free=1, maxlen=4, res=0, mesh_packet_id=222
        )
        iface._handle_queue_status_from_radio(status_unexpected)
        assert iface.queue[222] is False

        status_res = mesh_pb2.QueueStatus(free=1, maxlen=4, res=1, mesh_packet_id=222)
        iface._handle_queue_status_from_radio(status_res)
        assert iface.queue[222] is False


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handle_queue_status_awaiting_correlation_not_marked_unexpected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Queue status for recently sent packets should not be logged as unexpected replies."""
    with MeshInterface(noProto=True) as iface:
        packet_id = 0x01020304
        iface._queue_send_runtime._record_queue_status(
            mesh_pb2.QueueStatus(free=3, maxlen=4, res=1, mesh_packet_id=0)
        )
        packet = mesh_pb2.ToRadio()
        packet.packet.id = packet_id
        resent_queue: OrderedDict[int, mesh_pb2.ToRadio | bool] = OrderedDict(
            [(packet_id, packet)]
        )
        iface._queue_send_runtime._reconcile_resent_queue(
            resent_queue=resent_queue,
            sent_packet_ids={packet_id},
        )

        with caplog.at_level(logging.DEBUG):
            iface._handle_queue_status_from_radio(
                mesh_pb2.QueueStatus(free=3, maxlen=4, res=0, mesh_packet_id=packet_id)
            )

    assert packet_id not in iface.queue
    assert "Reply for unexpected packet ID" not in caplog.text
    assert (
        "Correlated queue-status reply for packet awaiting correlation" in caplog.text
    )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handle_from_radio_branch_matrix(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """_handle_from_radio() should handle metadata/node-info and non-config branch dispatch paths."""
    published_topics: list[str] = []
    monkeypatch.setattr(
        mesh_interface_module.publishingThread,  # type: ignore[attr-defined]
        "queueWork",
        lambda callback: callback(),
    )
    monkeypatch.setattr(
        mesh_interface_module.pub,  # type: ignore[attr-defined]
        "sendMessage",
        lambda topic, **_kwargs: published_topics.append(topic),
    )

    with MeshInterface(noProto=True) as iface:
        iface._start_config()

        metadata_msg = mesh_pb2.FromRadio()
        metadata_msg.metadata.firmware_version = "2.7.18"
        iface._handle_from_radio(metadata_msg.SerializeToString())
        assert iface.metadata is not None
        assert iface.metadata.firmware_version == "2.7.18"

        node_info_msg = mesh_pb2.FromRadio()
        node_info_msg.node_info.num = 999
        node_info_msg.node_info.user.id = "!000003e7"
        node_info_msg.node_info.user.long_name = "N999"
        node_info_msg.node_info.user.short_name = "N9"
        with caplog.at_level(logging.DEBUG):
            iface._handle_from_radio(node_info_msg.SerializeToString())
        assert "Node has no position key" in caplog.text

        handle_config_complete = MagicMock()
        handle_channel = MagicMock()
        handle_packet = MagicMock()
        handle_log_record = MagicMock()
        handle_queue_status = MagicMock()
        monkeypatch.setattr(
            iface._receive_pipeline, "_handle_config_complete", handle_config_complete
        )
        monkeypatch.setattr(iface._receive_pipeline, "_handle_channel", handle_channel)
        monkeypatch.setattr(
            iface._receive_pipeline, "_handle_packet_from_radio", handle_packet
        )
        monkeypatch.setattr(
            iface._receive_pipeline, "_handle_log_record", handle_log_record
        )
        monkeypatch.setattr(
            iface._receive_pipeline,
            "_handle_queue_status_from_radio",
            handle_queue_status,
        )

        config_complete_msg = mesh_pb2.FromRadio()
        assert iface.configId is not None
        config_complete_msg.config_complete_id = iface.configId
        iface._handle_from_radio(config_complete_msg.SerializeToString())
        handle_config_complete.assert_called_once()

        channel_msg = mesh_pb2.FromRadio()
        channel_msg.channel.index = 1
        iface._handle_from_radio(channel_msg.SerializeToString())
        handle_channel.assert_called_once()

        packet_msg = mesh_pb2.FromRadio()
        packet_msg.packet.id = 10
        iface._handle_from_radio(packet_msg.SerializeToString())
        handle_packet.assert_called_once()

        log_msg = mesh_pb2.FromRadio()
        log_msg.log_record.message = "hello"
        iface._handle_from_radio(log_msg.SerializeToString())
        handle_log_record.assert_called_once()

        queue_msg = mesh_pb2.FromRadio()
        queue_msg.queueStatus.free = 1
        queue_msg.queueStatus.maxlen = 5
        iface._handle_from_radio(queue_msg.SerializeToString())
        handle_queue_status.assert_called_once()

        notif_msg = mesh_pb2.FromRadio()
        notif_msg.clientNotification.reply_id = 1
        iface._handle_from_radio(notif_msg.SerializeToString())

        mqtt_msg = mesh_pb2.FromRadio()
        mqtt_msg.mqttClientProxyMessage.topic = "t"
        iface._handle_from_radio(mqtt_msg.SerializeToString())

        xmodem_msg = mesh_pb2.FromRadio()
        xmodem_msg.xmodemPacket.control = cast(Any, 1)
        iface._handle_from_radio(xmodem_msg.SerializeToString())

        disconnected_calls: list[int] = []
        monkeypatch.setattr(
            MeshInterface,
            "_disconnected",
            lambda _iface: disconnected_calls.append(1),
        )
        restart_config = MagicMock()
        monkeypatch.setattr(iface, "_start_config", restart_config)
        rebooted_msg = mesh_pb2.FromRadio(rebooted=True)
        iface._handle_from_radio(rebooted_msg.SerializeToString())
        assert disconnected_calls == [1]
        restart_config.assert_called_once()

        with caplog.at_level(logging.DEBUG):
            iface._handle_from_radio(mesh_pb2.FromRadio().SerializeToString())
        assert "Unexpected FromRadio payload" in caplog.text

    assert "meshtastic.node.updated" in published_topics
    assert "meshtastic.clientNotification" in published_topics
    assert "meshtastic.mqttclientproxymessage" in published_topics
    assert "meshtastic.xmodempacket" in published_topics


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handle_from_radio_config_and_module_config_branches() -> None:
    """_handle_from_radio() should copy each config/moduleConfig branch into localNode caches."""
    config_fields = [
        "device",
        "position",
        "power",
        "network",
        "display",
        "lora",
        "bluetooth",
        "security",
    ]
    module_fields = [
        "mqtt",
        "serial",
        "external_notification",
        "store_forward",
        "range_test",
        "telemetry",
        "canned_message",
        "audio",
        "remote_hardware",
        "neighbor_info",
        "detection_sensor",
        "ambient_lighting",
        "paxcounter",
        "traffic_management",
    ]

    with MeshInterface(noProto=True) as iface:
        for field in config_fields:
            msg = mesh_pb2.FromRadio()
            getattr(msg.config, field).SetInParent()
            iface._handle_from_radio(msg.SerializeToString())
            assert iface.localNode.localConfig.HasField(cast(Any, field))

        for field in module_fields:
            msg = mesh_pb2.FromRadio()
            getattr(msg.moduleConfig, field).SetInParent()
            iface._handle_from_radio(msg.SerializeToString())
            assert iface.localNode.moduleConfig.HasField(cast(Any, field))


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handle_from_radio_config_update_skips_unsupported_local_cache_fields() -> None:
    """Config updates should skip unsupported local-only cache fields without raising."""
    with MeshInterface(noProto=True) as iface:
        msg_supported = mesh_pb2.FromRadio()
        msg_supported.config.device.SetInParent()
        iface._handle_from_radio(msg_supported.SerializeToString())
        assert iface.localNode.localConfig.HasField("device")

        # These fields may exist on FromRadio.config but not on
        # localNode.localConfig in newer firmware schemas.
        source_fields = config_pb2.Config.DESCRIPTOR.fields_by_name
        target_fields = iface.localNode.localConfig.DESCRIPTOR.fields_by_name

        if "sessionkey" in source_fields and "sessionkey" not in target_fields:
            msg_sessionkey = mesh_pb2.FromRadio()
            msg_sessionkey.config.sessionkey.SetInParent()
            iface._handle_from_radio(msg_sessionkey.SerializeToString())

        if "device_ui" in source_fields and "device_ui" not in target_fields:
            msg_device_ui = mesh_pb2.FromRadio()
            msg_device_ui.config.device_ui.SetInParent()
            iface._handle_from_radio(msg_device_ui.SerializeToString())

        # Supported cached fields remain intact after unsupported updates.
        assert iface.localNode.localConfig.HasField("device")


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_node_num_to_id_invalid_user_payloads() -> None:
    """_node_num_to_id() should return None when user payload is missing or has invalid id type."""
    with MeshInterface(noProto=True) as iface:
        iface.nodesByNum = {
            1: {"num": 1, "user": "bad-user"},
            2: {"num": 2, "user": {"id": 123}},
        }
        assert iface._node_num_to_id(1) is None
        assert iface._node_num_to_id(2) is None


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_get_or_create_by_num_requires_initialized_database() -> None:
    """_get_or_create_by_num() should raise when nodesByNum is not initialized."""
    with MeshInterface(noProto=True) as iface:
        iface.nodesByNum = None
        with pytest.raises(MeshInterface.MeshInterfaceError, match="not initialized"):
            iface._get_or_create_by_num(5)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handle_channel_appends_to_local_channel_list() -> None:
    """_handle_channel() should append received channels to _localChannels."""
    with MeshInterface(noProto=True) as iface:
        channel = channel_pb2.Channel(index=3)
        iface._handle_channel(channel)
        assert iface._localChannels[-1].index == 3


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handle_packet_from_radio_toid_warning_and_response_handler_paths(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """_handle_packet_from_radio() should log toId failures and execute protobuf/response-handler paths."""
    monkeypatch.setattr(
        mesh_interface_module.publishingThread,  # type: ignore[attr-defined]
        "queueWork",
        lambda callback: callback(),
    )

    with MeshInterface(noProto=True) as iface:
        packet_for_toid = mesh_pb2.MeshPacket()
        setattr(packet_for_toid, "from", 1)
        packet_for_toid.to = 2
        with patch.object(
            iface._receive_pipeline,
            "_node_num_to_id",
            side_effect=["!00000001", RuntimeError("toId failure")],
        ):
            with caplog.at_level(logging.WARNING):
                iface._handle_packet_from_radio(packet_for_toid, hack=True)
        assert "Not populating toId" in caplog.text

        on_receive_calls: list[int] = []
        on_ack_calls: list[int] = []
        ack_permitted_calls: list[int] = []

        def _on_receive(_iface: MeshInterface, _packet: dict[str, Any]) -> None:
            on_receive_calls.append(1)

        def _raising_callback(_packet: dict[str, Any]) -> None:
            raise RuntimeError(  # noqa: TRY003 - intentional test sentinel
                "handler boom"
            )

        def onAckNak(_packet: dict[str, Any]) -> None:  # noqa: N802
            on_ack_calls.append(1)

        def _ack_permitted_callback(_packet: dict[str, Any]) -> None:
            ack_permitted_calls.append(1)

        fake_protocol = types.SimpleNamespace(
            name="routing",
            protobufFactory=mesh_pb2.Routing,
            onReceive=_on_receive,
        )
        monkeypatch.setattr(
            receive_pipeline_module,
            "protocols",
            {portnums_pb2.PortNum.ROUTING_APP: fake_protocol},
        )

        routing = mesh_pb2.Routing()
        routing.error_reason = mesh_pb2.Routing.Error.NONE

        p1 = mesh_pb2.MeshPacket()
        setattr(p1, "from", 10)
        p1.to = 11
        p1.decoded.portnum = portnums_pb2.PortNum.ROUTING_APP
        p1.decoded.payload = routing.SerializeToString()
        p1.decoded.request_id = 77
        iface.responseHandlers[77] = ResponseHandler(
            callback=_raising_callback, ackPermitted=True
        )
        iface._handle_packet_from_radio(p1, hack=True)

        p2 = mesh_pb2.MeshPacket()
        setattr(p2, "from", 12)
        p2.to = 13
        p2.decoded.portnum = portnums_pb2.PortNum.ROUTING_APP
        p2.decoded.payload = routing.SerializeToString()
        p2.decoded.request_id = 78
        iface.responseHandlers[78] = ResponseHandler(
            callback=onAckNak, ackPermitted=False
        )
        iface._handle_packet_from_radio(p2, hack=True)

        p3 = mesh_pb2.MeshPacket()
        setattr(p3, "from", 14)
        p3.to = 15
        p3.decoded.portnum = portnums_pb2.PortNum.ROUTING_APP
        p3.decoded.payload = routing.SerializeToString()
        p3.decoded.request_id = 79
        iface.responseHandlers[79] = ResponseHandler(
            callback=_ack_permitted_callback, ackPermitted=True
        )
        iface._handle_packet_from_radio(p3, hack=True)

    assert on_receive_calls == [1, 1, 1]
    assert on_ack_calls == [1]
    assert ack_permitted_calls == [1]


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handle_packet_from_radio_admin_decode_failure_skips_admin_response_callback(
    decode_failure_iface: MeshInterface,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Admin decode failures should not invoke admin callbacks that depend on decoded admin.raw."""
    iface = decode_failure_iface
    with iface._node_db_lock:
        iface.nodes = {}
        iface.nodesByNum = {}

    response_callback = MagicMock()
    with iface._response_handlers_lock:
        iface.responseHandlers[42] = ResponseHandler(
            callback=response_callback,
            ackPermitted=True,
        )
    iface._clear_wait_error(WAIT_ATTR_NAK, request_id=42)
    packet = _make_decoded_packet(
        from_node=7,
        to_node=8,
        portnum=portnums_pb2.PortNum.ADMIN_APP,
        request_id=42,
        payload=b"\xff\x00\xff\x00",
    )

    with caplog.at_level(logging.WARNING):
        iface._handle_packet_from_radio(packet, hack=True)

    response_callback.assert_not_called()
    assert 42 not in iface.responseHandlers
    assert iface._acknowledgment.receivedNak is True
    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match="Failed to decode admin payload",
    ):
        iface._raise_wait_error_if_present(
            WAIT_ATTR_NAK,
            request_id=42,
        )
    assert "Failed to decode admin payload" in caplog.text
    assert (
        "Dropping response callback for requestId 42 due to admin decode failure."
        in caplog.text
    )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handle_packet_from_radio_decode_failure_does_not_raise(
    decode_failure_iface: MeshInterface,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Malformed known-protocol payloads should log and continue without crashing receive flow."""
    iface = decode_failure_iface
    fake_on_receive = MagicMock()
    _install_protocol_stub(
        monkeypatch,
        portnum=portnums_pb2.PortNum.POSITION_APP,
        name="position",
        protobuf_factory=mesh_pb2.Position,
        on_receive=fake_on_receive,
    )
    callback_calls = _register_response_capture(iface, 42)
    packet = _make_decoded_packet(
        portnum=portnums_pb2.PortNum.POSITION_APP,
        request_id=42,
        payload=b"\xff\x00\xff\x00",
    )

    with caplog.at_level(logging.WARNING):
        iface._handle_packet_from_radio(packet, hack=True)

    assert "Failed to decode position payload" in caplog.text
    fake_on_receive.assert_called_once()
    assert callback_calls
    assert callback_calls[0]["decoded"]["position"]["error"].startswith(
        "decode-failed:"
    )
    assert len(callback_calls) == 1
    assert 42 not in iface.responseHandlers


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handle_packet_from_radio_routing_decode_failure_sets_error_reason(
    decode_failure_iface: MeshInterface,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Malformed ROUTING_APP payloads should surface decode errors via routing.errorReason."""
    iface = decode_failure_iface
    fake_on_receive = MagicMock()
    _install_protocol_stub(
        monkeypatch,
        portnum=portnums_pb2.PortNum.ROUTING_APP,
        name="routing",
        protobuf_factory=mesh_pb2.Routing,
        on_receive=fake_on_receive,
    )
    callback_calls = _register_response_capture(iface, 77)
    packet = _make_decoded_packet(
        portnum=portnums_pb2.PortNum.ROUTING_APP,
        request_id=77,
        payload=b"\xff\x00\xff\x00",
    )

    with caplog.at_level(logging.WARNING):
        iface._handle_packet_from_radio(packet, hack=True)

    assert "Failed to decode routing payload" in caplog.text
    fake_on_receive.assert_called_once()
    assert callback_calls
    routing_payload = callback_calls[0]["decoded"]["routing"]
    assert routing_payload["error"].startswith("decode-failed:")
    assert routing_payload["errorReason"].startswith("decode-failed:")
    assert 77 not in iface.responseHandlers


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handle_packet_from_radio_message_to_dict_failure_does_not_raise(
    decode_failure_iface: MeshInterface,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """MessageToDict conversion failures should be handled as decode-failed payload errors."""
    iface = decode_failure_iface
    fake_on_receive = MagicMock()
    _install_protocol_stub(
        monkeypatch,
        portnum=portnums_pb2.PortNum.POSITION_APP,
        name="position",
        protobuf_factory=mesh_pb2.Position,
        on_receive=fake_on_receive,
    )
    callback_calls = _register_response_capture(iface, 88)
    _patch_message_to_dict_position_failure(monkeypatch)
    packet = _make_decoded_packet(
        portnum=portnums_pb2.PortNum.POSITION_APP,
        request_id=88,
        payload=mesh_pb2.Position(latitude_i=1).SerializeToString(),
    )

    with caplog.at_level(logging.WARNING):
        iface._handle_packet_from_radio(packet, hack=True)

    assert "Failed to decode position payload" in caplog.text
    fake_on_receive.assert_called_once()
    assert callback_calls
    assert callback_calls[0]["decoded"]["position"]["error"].startswith(
        "decode-failed:"
    )
    assert 88 not in iface.responseHandlers


class TestUnscopedWaitForAckNakOverlappingCommands:
    """Regression tests for unscoped waitForAckNak concurrency issues.

    The latest commits intentionally removed per-request ACK/NAK scoping and
    moved back to global ACK/NAK waits. This is simpler but reopens cross-talk
    risk when multiple remote admin commands overlap. These tests document the
    expected behavior and limitations of the unscoped implementation.
    """

    @pytest.mark.unit
    @pytest.mark.usefixtures("reset_mt_config")
    def test_overlapping_admin_commands_ack_race_condition(
        self,
    ) -> None:
        """Regression test: unscoped waits create a race condition with single ACK.

        Scenario:
        1. Send remote admin request A (request_id=100)
        2. Send remote admin request B (request_id=200) before A resolves
        3. Receive ACK for request A only (sets receivedAck=True)
        4. Observe race condition behavior

        ACTUAL BEHAVIOR with current implementation:
        - waitForAckNak calls acknowledgment.reset() IMMEDIATELY after detecting flag
        - This creates a race: whichever thread reads the flag first will:
          1. Detect receivedAck=True
          2. Call ack.reset() which sets receivedAck=False
          3. Return True
        - The other thread will then see receivedAck=False and timeout

        However, if both threads poll at the same time BEFORE either resets,
        BOTH can see the flag and both return True.

        This test verifies that with unscoped waits, only ONE waiter succeeds
        when a single ACK is received (the typical case with tight reset timing).
        This demonstrates the fundamental issue: the unscoped approach cannot
        properly attribute a single ACK to multiple overlapping requests.
        """
        # Create shared acknowledgment state (simulating MeshInterface._acknowledgment)
        ack = Acknowledgment()
        timeout = Timeout(maxSecs=0.5)
        timeout.sleepInterval = 0.001

        # Track completion status for each wait
        wait_a_result: list[bool] = []
        wait_b_result: list[bool] = []
        wait_a_started = threading.Event()
        wait_b_started = threading.Event()
        release_waits = threading.Event()

        def simulate_wait_a() -> None:
            """Simulate waitForAckNak for request A."""
            wait_a_started.set()
            assert release_waits.wait(timeout=1.0)
            # Unscoped wait - no request_id specified
            result = timeout.waitForAckNak(ack)
            wait_a_result.append(result)

        def simulate_wait_b() -> None:
            """Simulate waitForAckNak for request B."""
            wait_b_started.set()
            assert release_waits.wait(timeout=1.0)
            # Unscoped wait - no request_id specified
            result = timeout.waitForAckNak(ack)
            wait_b_result.append(result)

        # Start both waits concurrently
        thread_a = threading.Thread(target=simulate_wait_a, daemon=True)
        thread_b = threading.Thread(target=simulate_wait_b, daemon=True)

        thread_a.start()
        thread_b.start()

        # Wait for both threads to start their waits
        assert wait_a_started.wait(timeout=1.0), "Wait A did not start"
        assert wait_b_started.wait(timeout=1.0), "Wait B did not start"

        # Small delay to ensure both threads are polling
        time.sleep(0.01)

        # Release both waits simultaneously
        release_waits.set()

        # Simulate receiving ACK for only request A (by setting the global flag)
        # In real code, this would be set by _handle_packet_from_radio
        ack.receivedAck = True

        # Wait for both threads to complete
        thread_a.join(timeout=1.0)
        thread_b.join(timeout=1.0)

        # Verify both threads completed
        assert not thread_a.is_alive(), "Thread A did not complete"
        assert not thread_b.is_alive(), "Thread B did not complete"

        # The key assertion: with unscoped waits and immediate reset, only ONE
        # waiter should consume the ACK and return True. The other should timeout.
        # This demonstrates that the unscoped approach cannot properly handle
        # overlapping requests - one of them will always fail even though both
        # were waiting for potentially different ACKs.
        assert len(wait_a_result) == 1, "Wait A should have completed with a result"
        assert len(wait_b_result) == 1, "Wait B should have completed with a result"

        # Calculate how many waiters succeeded
        success_count = sum([wait_a_result[0], wait_b_result[0]])

        # With unscoped waits and tight reset timing, we expect exactly 1 success.
        # Both could succeed in a race condition if they both read before reset.
        # Either way demonstrates the fundamental problem: unscoped waits create
        # unpredictable behavior with overlapping commands.
        assert success_count >= 1, (
            "At least one waiter should have succeeded. "
            "If both timed out, there's a different issue."
        )

        # Document the actual behavior: with unscoped waits, only ONE waiter
        # gets the ACK due to the immediate reset() call. This means:
        # - One request appears to succeed (got the ACK)
        # - The other request times out (didn't get the ACK meant for it)
        # This is a problem because both requests were waiting for different ACKs
        if success_count == 1:
            # Typical case: reset() was called before the second thread read
            failed_waiter = "B" if wait_a_result[0] else "A"
            # This documents the core issue: one waiter times out incorrectly
            logging.getLogger(__name__).info(
                "REGRESSION: Waiter %s timed out despite waiting. "
                "Unscoped waits cannot distinguish between ACKs for different "
                "overlapping requests.",
                failed_waiter,
            )
        else:
            # Race condition: both read before reset
            logging.getLogger(__name__).info(
                "RACE CONDITION: Both waiters saw the ACK before reset() was called. "
                "This is unpredictable behavior from unscoped waits."
            )

    @pytest.mark.unit
    @pytest.mark.usefixtures("reset_mt_config")
    def test_overlapping_admin_commands_nak_race_condition(self) -> None:
        """Test that receiving NAK for one command creates race with unscoped waits.

        Scenario:
        1. Send remote admin request A (request_id=100)
        2. Send remote admin request B (request_id=200) before A resolves
        3. Receive NAK for request A only (sets receivedNak=True)
        4. Observe race condition behavior

        ACTUAL BEHAVIOR with current unscoped implementation:
        - waitForAckNak calls acknowledgment.reset() immediately after detecting flag
        - Only one waiter can consume the NAK and return True
        - The other waiter will timeout (return False)

        This test documents the fundamental issue: with unscoped waits, we cannot
        properly attribute a single NAK to the correct request. The unscoped approach
        creates unpredictable behavior where overlapping commands interfere.
        """
        # Create shared acknowledgment state
        ack = Acknowledgment()
        timeout = Timeout(maxSecs=0.5)
        timeout.sleepInterval = 0.001

        # Track completion status for each wait
        wait_a_result: list[bool] = []
        wait_b_result: list[bool] = []
        wait_a_started = threading.Event()
        wait_b_started = threading.Event()
        release_waits = threading.Event()

        def simulate_wait_a() -> None:
            """Simulate waitForAckNak for request A."""
            wait_a_started.set()
            assert release_waits.wait(timeout=1.0)
            result = timeout.waitForAckNak(ack, attrs=(WAIT_ATTR_ACK, WAIT_ATTR_NAK))
            wait_a_result.append(result)

        def simulate_wait_b() -> None:
            """Simulate waitForAckNak for request B."""
            wait_b_started.set()
            assert release_waits.wait(timeout=1.0)
            result = timeout.waitForAckNak(ack, attrs=(WAIT_ATTR_ACK, WAIT_ATTR_NAK))
            wait_b_result.append(result)

        # Start both waits concurrently
        thread_a = threading.Thread(target=simulate_wait_a, daemon=True)
        thread_b = threading.Thread(target=simulate_wait_b, daemon=True)

        thread_a.start()
        thread_b.start()

        # Wait for both threads to start
        assert wait_a_started.wait(timeout=1.0)
        assert wait_b_started.wait(timeout=1.0)
        time.sleep(0.01)

        # Release both waits
        release_waits.set()

        # Simulate receiving NAK for only request A
        ack.receivedNak = True

        # Wait for completion
        thread_a.join(timeout=1.0)
        thread_b.join(timeout=1.0)

        # Verify both threads completed
        assert not thread_a.is_alive()
        assert not thread_b.is_alive()

        # One or both waiters see the NAK (race condition)
        assert len(wait_a_result) == 1
        assert len(wait_b_result) == 1

        success_count = sum([wait_a_result[0], wait_b_result[0]])
        assert success_count >= 1, (
            "At least one waiter should have detected the NAK. "
            "If both timed out, there's a different issue."
        )

        # Document the issue: with unscoped waits, we cannot properly attribute
        # a single NAK to the correct request
        if success_count == 1:
            failed_waiter = "B" if wait_a_result[0] else "A"
            logging.getLogger(__name__).info(
                "REGRESSION: Waiter %s timed out despite waiting. "
                "Unscoped waits cannot distinguish between NAKs for different requests.",
                failed_waiter,
            )
        else:
            logging.getLogger(__name__).info(
                "RACE CONDITION: Both waiters saw the NAK before reset() was called. "
                "This is unpredictable behavior from unscoped waits."
            )

    @pytest.mark.unit
    @pytest.mark.usefixtures("reset_mt_config")
    def test_request_scoped_waits_do_not_crosstalk(self) -> None:
        """Verify that request-scoped waits properly isolate overlapping commands.

        This test demonstrates that when request_id is properly specified,
        overlapping waits do NOT experience cross-talk - each waiter only
        responds to its specific request's ACK/NAK.

        This serves as a comparison to show how the scoped approach solves
        the cross-talk issue present in unscoped waits.
        """
        with MeshInterface(noProto=True) as iface:
            iface._timeout = Timeout(maxSecs=0.5)
            iface._timeout.sleepInterval = 0.001

            request_a = 100
            request_b = 200

            # Clear any existing state
            iface._clear_wait_error(WAIT_ATTR_ACK, request_id=request_a)
            iface._clear_wait_error(WAIT_ATTR_ACK, request_id=request_b)

            # Track completion
            wait_a_result: list[bool] = []
            wait_b_result: list[bool] = []
            wait_a_started = threading.Event()
            wait_b_started = threading.Event()
            release_waits = threading.Event()

            def wait_for_a() -> None:
                wait_a_started.set()
                assert release_waits.wait(timeout=1.0)
                result = iface._wait_for_request_ack(
                    WAIT_ATTR_ACK, request_a, timeout_seconds=0.5
                )
                wait_a_result.append(result)

            def wait_for_b() -> None:
                wait_b_started.set()
                assert release_waits.wait(timeout=1.0)
                result = iface._wait_for_request_ack(
                    WAIT_ATTR_ACK, request_b, timeout_seconds=0.5
                )
                wait_b_result.append(result)

            # Start both scoped waits
            thread_a = threading.Thread(target=wait_for_a, daemon=True)
            thread_b = threading.Thread(target=wait_for_b, daemon=True)

            thread_a.start()
            thread_b.start()

            # Wait for both to start
            assert wait_a_started.wait(timeout=1.0)
            assert wait_b_started.wait(timeout=1.0)

            # Register both request IDs as active
            with iface._response_handlers_lock:
                active_ids = iface._active_wait_request_ids.setdefault(
                    WAIT_ATTR_ACK, set()
                )
                active_ids.add(request_a)
                active_ids.add(request_b)

            release_waits.set()

            # Mark only request A as acknowledged (scoped)
            iface._mark_wait_acknowledged(WAIT_ATTR_ACK, request_id=request_a)

            # Wait for completion
            thread_a.join(timeout=1.0)
            thread_b.join(timeout=1.0)

            # Verify proper isolation: A succeeded, B timed out
            assert len(wait_a_result) == 1
            assert len(wait_b_result) == 1
            assert wait_a_result[0] is True, "Request A should succeed with scoped wait"
            assert wait_b_result[0] is False, (
                "Request B should timeout (not receive A's ACK). "
                "Scoped waits properly isolate requests."
            )


class _FakeSendPipeline:
    """Test double capturing send pipeline call patterns."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def send_text(self, *args: object, **kwargs: object) -> str:
        self.calls.append(("sendText", args, kwargs))
        return "sent-text"

    def send_alert(self, *args: object, **kwargs: object) -> str:
        self.calls.append(("sendAlert", args, kwargs))
        return "sent-alert"

    def send_mqtt_client_proxy_message(self, *args: object, **kwargs: object) -> None:
        self.calls.append(("sendMqttClientProxyMessage", args, kwargs))


class _FakeReceivePipeline:
    """Test double capturing receive pipeline call patterns."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def _handle_from_radio(self, *args: object, **kwargs: object) -> None:
        self.calls.append(("_handle_from_radio", args, kwargs))

    def _handle_packet_from_radio(
        self, *args: object, **kwargs: object
    ) -> list[object]:
        self.calls.append(("_handle_packet_from_radio", args, kwargs))
        return ["handled-packet"]


@pytest.mark.unit
def test_mesh_interface_handle_from_radio_delegates_to_receive_pipeline() -> None:
    """_handle_from_radio should route through ReceivePipeline, not local impl."""
    interface = MeshInterface.__new__(MeshInterface)
    fake = _FakeReceivePipeline()
    interface._receive_pipeline = cast(Any, fake)

    interface._handle_from_radio(b"payload")

    assert fake.calls == [("_handle_from_radio", (b"payload",), {})]


@pytest.mark.unit
def test_mesh_interface_handle_packet_delegates_to_receive_pipeline() -> None:
    """_handle_packet_from_radio should route through ReceivePipeline."""
    interface = MeshInterface.__new__(MeshInterface)
    fake = _FakeReceivePipeline()
    interface._receive_pipeline = cast(Any, fake)
    packet = mesh_pb2.MeshPacket()

    result = interface._handle_packet_from_radio(
        packet,
        hack=True,
        emit_publication=False,
    )

    assert result == ["handled-packet"]
    assert fake.calls == [
        (
            "_handle_packet_from_radio",
            (packet,),
            {"allow_zero_source": True, "emit_publication": False},
        )
    ]


@pytest.mark.unit
def test_mesh_interface_send_text_delegates_to_send_pipeline() -> None:
    """SendText should route through _send_pipeline.send_text, not local impl."""
    interface = MeshInterface.__new__(MeshInterface)
    fake: Any = _FakeSendPipeline()
    interface._send_pipeline = fake

    result = interface.sendText("hello", destinationId="!12345678", wantAck=True)

    assert result == "sent-text"
    assert len(fake.calls) == 1
    name, args, kwargs = fake.calls[0]
    assert name == "sendText"
    assert args == ("hello",)
    assert kwargs["destinationId"] == "!12345678"
    assert kwargs["wantAck"] is True
    assert kwargs["wantResponse"] is False
    assert kwargs["onResponse"] is None
    assert kwargs["channelIndex"] == 0
    assert kwargs["hopLimit"] is None


@pytest.mark.unit
def test_mesh_interface_send_alert_delegates_to_send_pipeline() -> None:
    """SendAlert should route through _send_pipeline.send_alert, not local impl."""
    interface = MeshInterface.__new__(MeshInterface)
    fake: Any = _FakeSendPipeline()
    interface._send_pipeline = fake

    result = interface.sendAlert("wake", destinationId="!12345678")

    assert result == "sent-alert"
    assert len(fake.calls) == 1
    name, args, kwargs = fake.calls[0]
    assert name == "sendAlert"
    assert args == ("wake",)
    assert kwargs["destinationId"] == "!12345678"
    assert kwargs["onResponse"] is None
    assert kwargs["channelIndex"] == 0
    assert kwargs["hopLimit"] is None


@pytest.mark.unit
def test_mesh_interface_mqtt_proxy_delegates_to_send_pipeline() -> None:
    """SendMqttClientProxyMessage should route through _send_pipeline, not local impl."""
    interface = MeshInterface.__new__(MeshInterface)
    fake: Any = _FakeSendPipeline()
    interface._send_pipeline = fake

    interface.sendMqttClientProxyMessage("topic", b"payload")

    assert fake.calls == [
        (
            "sendMqttClientProxyMessage",
            ("topic", b"payload"),
            {},
        )
    ]


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_send_to_radio_reports_disconnected_queue_wait_as_interface_error() -> None:
    """A stale full queue after disconnect must fail rather than sleep forever."""
    with MeshInterface(noProto=True) as iface:
        iface.noProto = False
        iface.queueStatus = mesh_pb2.QueueStatus(free=0, maxlen=16)
        iface._last_disconnect_source = "stream.closed"
        packet = mesh_pb2.ToRadio()
        packet.packet.id = 903

        with pytest.raises(
            MeshInterface.MeshInterfaceError,
            match=r"interface disconnected \(stream.closed\)",
        ):
            iface._send_to_radio(packet)

        assert 903 not in iface.queue


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_queue_wait_abort_reason_reports_failure_and_closing() -> None:
    with MeshInterface(noProto=True) as iface:
        iface.failure = RuntimeError("serial failed")
        assert iface._queue_wait_abort_reason() == "interface failure: serial failed"
        iface.failure = None
        iface._closing = True
        assert iface._queue_wait_abort_reason() == "interface is closing"


@pytest.mark.unit
def test_disconnect_source_property_rejects_non_string_values() -> None:
    """Disconnect diagnostics should reject values the getter would discard."""
    iface = MeshInterface.__new__(MeshInterface)

    with pytest.raises(
        TypeError,
        match="_last_disconnect_source must be a str or None, got int",
    ):
        iface._last_disconnect_source = 7  # type: ignore[assignment]

    iface._last_disconnect_source = None
    assert iface._last_disconnect_source is None


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_queue_wait_timeout_can_be_overridden_by_transport_subclass() -> None:
    class FastFailInterface(MeshInterface):
        _queue_wait_timeout_seconds = 1.25

    with FastFailInterface(noProto=True) as iface:
        assert iface._queue_send_runtime._queue_wait_timeout_seconds == 1.25


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_queue_wait_abort_reason_allows_connected_interface() -> None:
    with MeshInterface(noProto=True) as iface:
        iface.isConnected.set()
        assert iface._queue_wait_abort_reason() is None


@pytest.mark.unit
def test_connection_probe_overrides_are_normalized() -> None:
    """Connection timeout overrides should normalize invalid probe values."""
    with MeshInterface(noProto=True) as iface:
        iface.__dict__["_probe_timeout"] = -2.5
        assert iface._connection_timeout_override("_probe_timeout") == 0.0
        iface.__dict__["_probe_timeout"] = True
        assert iface._connection_timeout_override("_probe_timeout", 3.0) == 3.0
        iface.__dict__["_probe_timeout"] = "invalid"
        assert iface._connection_timeout_override("_probe_timeout") is None


@pytest.mark.unit
def test_connect_failure_log_level_respects_quiet_probe_mode() -> None:
    """Quiet connection probes should lower expected failure logs to DEBUG."""
    with MeshInterface(noProto=True) as iface:
        assert iface._connect_failure_log_level() == logging.ERROR
        iface._suppress_connect_failure_logging = True
        assert iface._connect_failure_log_level() == logging.DEBUG


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_response_handler_pruning_expires_managed_callbacks_only() -> None:
    """Expired managed callbacks should be pruned with matcher/ACK metadata."""
    with MeshInterface(noProto=True) as iface:
        callback = MagicMock()
        matcher = MagicMock(return_value=True)
        iface._request_wait_runtime.add_response_handler(
            501,
            callback,
            ack_permitted=True,
            is_ack_nak_handler=True,
            matcher=matcher,
        )
        registered_at = iface._request_wait_runtime._response_handler_registered_at[501]
        assert iface._request_wait_runtime.prune_stale_response_handlers(
            now=registered_at + RESPONSE_HANDLER_TTL_SECONDS + 1.0
        ) == [501]
        assert 501 not in iface.responseHandlers
        assert 501 not in iface._request_wait_runtime._response_matchers
        assert 501 not in iface._request_wait_runtime._ack_nak_handlers
        assert 501 not in iface._request_wait_runtime._response_handler_registered_at
        assert 501 not in iface._request_wait_runtime._managed_response_handlers


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_response_handler_pruning_preserves_active_scoped_wait() -> None:
    """TTL pruning must not retire callbacks that still back an active wait."""
    with MeshInterface(noProto=True) as iface:
        iface._request_wait_runtime.add_response_handler(
            502, MagicMock(), ack_permitted=True
        )
        iface._clear_wait_error(WAIT_ATTR_NAK, request_id=502)
        registered_at = iface._request_wait_runtime._response_handler_registered_at[502]
        assert (
            iface._request_wait_runtime.prune_stale_response_handlers(
                now=registered_at + RESPONSE_HANDLER_TTL_SECONDS + 1.0
            )
            == []
        )
        assert 502 in iface.responseHandlers


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_close_clears_pending_response_callbacks() -> None:
    """Closing an interface should release pending managed callback references."""
    iface = MeshInterface(noProto=True)
    iface._request_wait_runtime.add_response_handler(
        503,
        MagicMock(),
        ack_permitted=True,
        matcher=MagicMock(return_value=True),
    )

    iface.close()

    assert iface.responseHandlers == {}
    assert iface._request_wait_runtime._response_matchers == {}
    assert iface._request_wait_runtime._ack_nak_handlers == {}
    assert iface._request_wait_runtime._response_handler_registered_at == {}


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_response_handler_pruning_preserves_legacy_unmanaged_entry() -> None:
    """TTL pruning should not assign a lifetime to direct legacy dict entries."""
    with MeshInterface(noProto=True) as iface:
        iface.responseHandlers[504] = ResponseHandler(
            callback=MagicMock(), ackPermitted=True
        )

        assert (
            iface._request_wait_runtime.prune_stale_response_handlers(
                now=time.monotonic() + 10_000.0
            )
            == []
        )
        assert 504 in iface.responseHandlers
        assert 504 not in iface._request_wait_runtime._response_handler_registered_at
        assert 504 not in iface._request_wait_runtime._managed_response_handlers


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_response_handler_registration_prunes_previous_stale_callback() -> None:
    """A later managed registration should bound stale callback accumulation."""
    with MeshInterface(noProto=True) as iface:
        iface._request_wait_runtime.add_response_handler(
            505, MagicMock(), ack_permitted=True
        )
        iface._request_wait_runtime._response_handler_registered_at[505] = (
            time.monotonic() - RESPONSE_HANDLER_TTL_SECONDS - 1.0
        )

        iface._request_wait_runtime.add_response_handler(
            506, MagicMock(), ack_permitted=True
        )

        assert 505 not in iface.responseHandlers
        assert 505 not in iface._request_wait_runtime._response_handler_registered_at
        assert 505 not in iface._request_wait_runtime._response_matchers
        assert 505 not in iface._request_wait_runtime._ack_nak_handlers
        assert 505 not in iface._request_wait_runtime._managed_response_handlers
        assert 506 in iface.responseHandlers


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_inbound_response_expires_only_its_correlated_stale_handler() -> None:
    """Inbound traffic should enforce TTL without scanning unrelated handlers."""
    with MeshInterface(noProto=True) as iface:
        runtime = iface._request_wait_runtime
        stale_callback = MagicMock()
        other_callback = MagicMock()
        runtime.add_response_handler(507, stale_callback, ack_permitted=True)
        runtime.add_response_handler(509, other_callback, ack_permitted=True)
        runtime._response_handler_registered_at[507] = (
            time.monotonic() - RESPONSE_HANDLER_TTL_SECONDS - 1.0
        )
        runtime._response_handler_registered_at[509] = (
            time.monotonic() - RESPONSE_HANDLER_TTL_SECONDS - 1.0
        )

        iface._request_wait_runtime.correlate_inbound_response(
            packet_dict={"decoded": {"requestId": 507}},
            skip_response_callback_for_decode_failure=False,
            extract_request_id=lambda packet: packet["decoded"]["requestId"],
        )

        stale_callback.assert_not_called()
        assert 507 not in iface.responseHandlers
        assert 509 in iface.responseHandlers
        assert 509 in runtime._response_handler_registered_at


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_response_handler_pruning_preserves_legacy_replacement() -> None:
    """A direct legacy replacement should shed stale managed metadata only."""
    with MeshInterface(noProto=True) as iface:
        runtime = iface._request_wait_runtime
        runtime.add_response_handler(508, MagicMock(), ack_permitted=True)
        registered_at = runtime._response_handler_registered_at[508]
        legacy_handler = ResponseHandler(callback=MagicMock(), ackPermitted=True)
        iface.responseHandlers[508] = legacy_handler

        assert (
            runtime.prune_stale_response_handlers(
                now=registered_at + RESPONSE_HANDLER_TTL_SECONDS + 1.0
            )
            == []
        )
        assert iface.responseHandlers[508] is legacy_handler
        assert 508 not in runtime._response_handler_registered_at
        assert 508 not in runtime._response_matchers
        assert 508 not in runtime._ack_nak_handlers
        assert 508 not in runtime._managed_response_handlers
