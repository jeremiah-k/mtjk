"""Regression tests for packet-id collision avoidance in `_send_data_with_wait`.

Response correlation keys live response handlers by request id alone
(``MeshInterface.responseHandlers`` and the request-wait runtime side tables),
so when a send will register a response handler, a freshly generated packet id
must not reuse an id that still has a registered callback/matcher. These tests
pin that the send pipeline regenerates ids that are already live (and zero ids,
as before) while keeping the bounded retry budget and its historical failure
mode.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import meshtastic.mesh_interface as mesh_interface_module
from meshtastic.mesh_interface import MeshInterface

# Distinct 22-bit random parts used to craft deterministic draws. A generated
# id is (counter & 0x3FF) | (randint_draw << 10), so with currentPacketId
# starting at 0 the n-th draw has counter n.
_RANDOM_PART_1 = 0x155555
_RANDOM_PART_2 = 0x2AAAAB
_RANDOM_PART_3 = 0x3C3C3C

_COLLISION_ID_1 = (_RANDOM_PART_1 << 10) | 1
_COLLISION_ID_2 = (_RANDOM_PART_2 << 10) | 2
_FRESH_ID_3 = (_RANDOM_PART_3 << 10) | 3


def _install_randint_draws(
    monkeypatch: pytest.MonkeyPatch,
    draws: list[int],
    *,
    fallback: int = 0,
) -> None:
    """Make `_generate_packet_id` consume deterministic random parts in order."""
    draws = list(draws)

    def _fake_randint(low: int, high: int) -> int:
        assert low == 0
        assert high == mesh_interface_module.PACKET_ID_RANDOM_MAX
        return draws.pop(0) if draws else fallback

    monkeypatch.setattr(
        mesh_interface_module, "random", SimpleNamespace(randint=_fake_randint)
    )


def _install_capture_send(
    monkeypatch: pytest.MonkeyPatch, iface: MeshInterface, sent: list[Any]
) -> None:
    """Capture outgoing packets at the interface send seam."""

    def _send_packet(packet: Any, *_args: Any, **_kwargs: Any) -> Any:
        sent.append(packet)
        return packet

    monkeypatch.setattr(iface, "_send_packet", _send_packet)  # noqa: SLF001


@pytest.mark.unit
def test_send_data_with_wait_avoids_live_response_handler_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live handler's request id must be regenerated, not silently reused.

    The first two draws reproduce ids that already carry live response
    handlers; the send must land on the fresh third id while the pre-existing
    registrations (callback object and typed matcher) stay intact.
    """
    with MeshInterface(noProto=True) as iface:
        runtime = iface._request_wait_runtime  # noqa: SLF001
        callback_1 = MagicMock(name="callback-1")
        callback_2 = MagicMock(name="callback-2")
        matcher_1 = MagicMock(name="matcher-1")
        matcher_2 = MagicMock(name="matcher-2")
        runtime.add_response_handler(  # noqa: SLF001
            _COLLISION_ID_1, callback_1, ack_permitted=False, matcher=matcher_1
        )
        runtime.add_response_handler(  # noqa: SLF001
            _COLLISION_ID_2, callback_2, ack_permitted=False, matcher=matcher_2
        )
        handler_1_before = iface.responseHandlers[_COLLISION_ID_1]
        handler_2_before = iface.responseHandlers[_COLLISION_ID_2]

        iface.currentPacketId = 0  # noqa: SLF001
        _install_randint_draws(
            monkeypatch, [_RANDOM_PART_1, _RANDOM_PART_2, _RANDOM_PART_3]
        )
        sent: list[Any] = []
        _install_capture_send(monkeypatch, iface, sent)

        new_callback = MagicMock(name="callback-new")
        new_matcher = MagicMock(name="matcher-new")
        packet = iface._send_pipeline._send_data_with_wait(  # noqa: SLF001
            b"ping", onResponse=new_callback, responseMatcher=new_matcher
        )

        # The send proceeded with the first non-colliding id.
        assert sent == [packet]
        assert packet.id == _FRESH_ID_3
        assert packet.id not in (_COLLISION_ID_1, _COLLISION_ID_2)

        # The new handler is registered under the fresh id.
        assert iface.responseHandlers[packet.id].callback is new_callback
        assert runtime._response_matchers[packet.id] is new_matcher  # noqa: SLF001

        # The pre-existing registrations for the colliding ids are intact.
        assert iface.responseHandlers[_COLLISION_ID_1] is handler_1_before
        assert iface.responseHandlers[_COLLISION_ID_2] is handler_2_before
        assert iface.responseHandlers[_COLLISION_ID_1].callback is callback_1
        assert iface.responseHandlers[_COLLISION_ID_2].callback is callback_2
        assert runtime._response_matchers[_COLLISION_ID_1] is matcher_1  # noqa: SLF001
        assert runtime._response_matchers[_COLLISION_ID_2] is matcher_2  # noqa: SLF001


@pytest.mark.unit
def test_send_data_with_wait_still_retries_zero_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A zero draw is regenerated exactly as before the collision avoidance."""
    with MeshInterface(noProto=True) as iface:
        iface.currentPacketId = 1023  # noqa: SLF001
        # Draw 1: counter (1023 + 1) & 0x3FF == 0 with random part 0 -> id 0.
        # Draw 2: counter 1 with random part 777 -> accepted fresh id.
        _install_randint_draws(monkeypatch, [0, 777])
        sent: list[Any] = []
        _install_capture_send(monkeypatch, iface, sent)

        packet = iface._send_pipeline._send_data_with_wait(b"ping")  # noqa: SLF001

        assert sent == [packet]
        assert packet.id == (777 << 10) | 1
        assert packet.id != 0


@pytest.mark.unit
def test_send_data_with_wait_proceeds_after_retry_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When every draw collides, the send proceeds with a warning, not an error."""
    with MeshInterface(noProto=True) as iface:
        runtime = iface._request_wait_runtime  # noqa: SLF001
        constant_random_part = 999
        # With currentPacketId 0 the eleven draws have counters 1..11; all are
        # pre-registered so every draw collides and the retry budget drains.
        for counter in range(1, 12):
            runtime.add_response_handler(  # noqa: SLF001
                (constant_random_part << 10) | counter,
                MagicMock(),
                ack_permitted=False,
            )

        iface.currentPacketId = 0  # noqa: SLF001
        _install_randint_draws(monkeypatch, [], fallback=constant_random_part)
        sent: list[Any] = []
        _install_capture_send(monkeypatch, iface, sent)

        with caplog.at_level(logging.WARNING):
            packet = iface._send_pipeline._send_data_with_wait(  # noqa: SLF001
                b"ping", onResponse=MagicMock()
            )

        assert sent == [packet]
        assert packet.id == (constant_random_part << 10) | 11
        assert "still collides with a live response handler" in caplog.text


@pytest.mark.unit
def test_send_data_with_wait_still_fails_after_all_zero_draws(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exhausting the retries on zero ids keeps the historical error."""
    with MeshInterface(noProto=True) as iface:
        runtime = iface._request_wait_runtime  # noqa: SLF001
        # Counters for the eleven draws are (1013 + n) & 0x3FF for n = 1..11,
        # i.e. 1014..1023 followed by 0. Register the first ten so every
        # nonzero draw collides and the final draw produces id 0 again.
        for counter in range(1014, 1024):
            runtime.add_response_handler(  # noqa: SLF001
                counter, MagicMock(), ack_permitted=False
            )

        iface.currentPacketId = 1013  # noqa: SLF001
        _install_randint_draws(monkeypatch, [], fallback=0)

        with pytest.raises(
            MeshInterface.MeshInterfaceError,
            match="Failed to generate non-zero packet ID",
        ):
            iface._send_pipeline._send_data_with_wait(  # noqa: SLF001
                b"ping", onResponse=MagicMock()
            )
