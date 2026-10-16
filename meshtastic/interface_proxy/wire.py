"""Meshtastic serial/TCP framing; BLE GATT transfers raw protobuf bytes."""

from __future__ import annotations

from meshtastic.stream_interface import MAX_TO_FROM_RADIO_SIZE, START1, START2

_HEADER = bytes((START1, START2))


class ProxyProtocolError(ValueError):
    """Malformed or oversize on-wire Meshtastic transport packet."""


def validate_payload(payload: bytes) -> bytes:
    """Validate a complete protobuf blob without inspecting or changing its content."""
    if not payload or len(payload) > MAX_TO_FROM_RADIO_SIZE:
        raise ProxyProtocolError(
            f"protobuf length {len(payload)} outside 1..{MAX_TO_FROM_RADIO_SIZE}"
        )
    return payload


def frame(payload: bytes) -> bytes:
    """Encode a single serial/TCP frame."""
    validate_payload(payload)
    return _HEADER + len(payload).to_bytes(2, "big") + payload


class FrameDecoder:
    """Incrementally decode stream frames, resynchronizing after stray bytes.

    The decoder never constructs a payload larger than the firmware stream cap.
    An invalid length raises immediately instead of buffering attacker input.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        """Consume arbitrary stream chunks and return all complete frames."""
        self._buffer.extend(data)
        packets: list[bytes] = []
        while True:
            start = self._buffer.find(_HEADER)
            if start == -1:
                self._buffer[:] = b"\x94" if self._buffer.endswith(b"\x94") else b""
                return packets
            if start:
                del self._buffer[:start]
            if len(self._buffer) < 4:
                return packets
            size = int.from_bytes(self._buffer[2:4], "big")
            if not 1 <= size <= MAX_TO_FROM_RADIO_SIZE:
                raise ProxyProtocolError(f"invalid frame length: {size}")
            if len(self._buffer) < 4 + size:
                return packets
            packets.append(bytes(self._buffer[4 : 4 + size]))
            del self._buffer[: 4 + size]
