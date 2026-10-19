"""A successful empty GATT response is queue state, not link failure."""

import logging
import threading
from typing import Any, cast

import pytest

from meshtastic.interfaces.ble.receive_service import BLEReceiveRecoveryController
from meshtastic.interfaces.ble.session_state import BLESessionState


class _Coordinator:
    def wait_for_event(self, _name: str, *, timeout: float) -> bool:
        return True

    def clear_event(self, _name: str) -> None:
        pass


class _Interface:
    _fromnum_notify_enabled = True
    _is_connection_closing = False
    auto_reconnect = False
    _empty_read_policy = object()

    def __init__(self, responses: list[bytes | Exception]) -> None:
        self.responses = responses
        self.reads = 0
        self.stopped = False
        self.client = self
        self.handled: list[bytes] = []

    def _should_run_receive_loop(self) -> bool:
        return not self.stopped

    def _retry_policy_get_delay(self, _policy: object, _attempt: int) -> float:
        return 0

    def read_gatt_char(self, _uuid: str, *, timeout: float) -> bytes:
        self.reads += 1
        response = self.responses.pop(0) if self.responses else b""
        if isinstance(response, Exception):
            raise response
        if not response:
            self.stopped = True
        return response

    def _handle_from_radio(self, payload: bytes) -> None:
        self.handled.append(payload)

    def close(self) -> None:
        self.stopped = True


def _controller(iface: _Interface) -> BLEReceiveRecoveryController:
    return BLEReceiveRecoveryController(
        cast(Any, iface),
        session_state=BLESessionState(lock=threading.RLock(), ever_connected=True),
    )


def test_write_triggered_empty_queue_is_diagnostic(
    caplog: pytest.LogCaptureFixture,
) -> None:
    iface = _Interface([])
    with caplog.at_level(logging.DEBUG):
        assert _controller(iface).read_from_radio_with_retries(cast(Any, iface)) is None
    assert not [
        record for record in caplog.records if record.levelno >= logging.WARNING
    ]
    assert any("empty" in record.getMessage().lower() for record in caplog.records)


def test_queue_drain_empty_tail_is_diagnostic(caplog: pytest.LogCaptureFixture) -> None:
    iface = _Interface([b"packet", b""])
    with caplog.at_level(logging.DEBUG):
        assert _controller(iface)._run_receive_cycle(
            coordinator=cast(Any, _Coordinator()), wait_timeout=0.01
        )
    assert iface.handled == [b"packet"]
    assert iface.reads > 2
    assert not [
        record for record in caplog.records if record.levelno >= logging.WARNING
    ]


def test_first_read_still_retries_a_late_payload() -> None:
    iface = _Interface([b"", b"late payload"])
    assert (
        _controller(iface).read_from_radio_with_retries(cast(Any, iface))
        == b"late payload"
    )
    assert iface.reads == 2


def test_actual_read_error_still_reaches_error_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    iface = _Interface([OSError("GATT read failed")])
    with caplog.at_level(logging.ERROR):
        should_break, fatal = _controller(iface)._handle_payload_read(
            cast(Any, iface), poll_without_notify=False
        )
    assert should_break and fatal
    assert any(
        record.levelno == logging.ERROR and "GATT read failed" in record.getMessage()
        for record in caplog.records
    )
