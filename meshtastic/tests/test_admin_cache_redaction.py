"""Regression coverage for administrative passkeys in received node records."""

import base64
import copy
import threading
from typing import Any
from unittest.mock import Mock

import pytest
from google.protobuf.json_format import MessageToDict
from pubsub import pub

from meshtastic import DECODE_ERROR_KEY, ResponseHandler
from meshtastic._protocol_runtime import (
    REDACTED_BYTES,
    REDACTED_TEXT,
    _sanitize_last_received,
)
from meshtastic.mesh_interface import MeshInterface
from meshtastic.protobuf import admin_pb2, mesh_pb2, portnums_pb2

pytestmark = pytest.mark.unit

_SENDER = 0x12345678
_KEY = b"synthetic-session-passkey"


def _admin_packet(payload: bytes) -> mesh_pb2.MeshPacket:
    packet = mesh_pb2.MeshPacket(id=42, to=0x87654321, rx_time=123)
    setattr(packet, "from", _SENDER)
    packet.decoded.portnum = portnums_pb2.ADMIN_APP
    packet.decoded.payload = payload
    packet.decoded.request_id = 17
    return packet


def test_admin_receive_redacts_every_cached_representation(
    mock_interface: MeshInterface,
) -> None:
    """Only the cache copy changes; request handlers and publication keep the wire packet."""
    admin = admin_pb2.AdminMessage(session_passkey=_KEY)
    admin.get_device_metadata_response.firmware_version = "2.7.11"
    packet = _admin_packet(admin.SerializeToString())
    original = copy.deepcopy(packet)
    callback = Mock()

    iface = mock_interface
    iface.nodesByNum = {}
    received: list[dict[str, Any]] = []
    published = threading.Event()

    def receive(packet: dict[str, Any], interface: MeshInterface) -> None:
        if interface is iface:
            received.append(packet)
            published.set()

    iface.responseHandlers[17] = ResponseHandler(callback)
    pub.subscribe(receive, "meshtastic.receive.admin")
    try:
        iface._handle_packet_from_radio(packet)
        assert published.wait(5)
    finally:
        pub.unsubscribe(receive, "meshtastic.receive.admin")
    node = iface._get_or_create_by_num(_SENDER)
    cached = node["lastReceived"]
    decoded = cached["decoded"]
    redacted_admin = decoded["admin"]["raw"]
    expected_admin = copy.deepcopy(admin)
    expected_admin.session_passkey = REDACTED_BYTES
    expected_packet = copy.deepcopy(original)
    expected_packet.decoded.payload = expected_admin.SerializeToString()

    assert node["adminSessionPassKey"] == _KEY
    assert redacted_admin == expected_admin
    assert decoded["admin"]["sessionPasskey"] == base64.b64encode(
        REDACTED_BYTES
    ).decode("ascii")
    assert decoded["admin"]["getDeviceMetadataResponse"]["firmwareVersion"] == "2.7.11"
    assert admin_pb2.AdminMessage.FromString(decoded["payload"]) == expected_admin
    assert cached["raw"] == expected_packet
    assert cached["raw"] is not packet
    assert _KEY not in cached["raw"].SerializeToString()
    assert node["lastHeard"] == 123

    detached = iface.queryNodes()
    assert detached.nodes[0]["lastReceived"]["raw"] == expected_packet
    records = detached.toDict()["nodes"]
    assert isinstance(records, list)
    record = records[0]
    assert isinstance(record, dict)
    assert "adminSessionPassKey" not in record
    last_received = record["lastReceived"]
    assert isinstance(last_received, dict)
    assert "raw" not in last_received
    assert "decoded" not in last_received

    callback.assert_called_once()
    assert callback.call_args is not None
    callback_packet = callback.call_args.args[0]
    assert len(received) == 1
    for delivered in (callback_packet, received[0]):
        assert delivered["decoded"]["admin"]["raw"] == admin
        assert delivered["decoded"]["admin"]["sessionPasskey"] == base64.b64encode(
            _KEY
        ).decode("ascii")
        assert delivered["decoded"]["payload"] == original.decoded.payload
        assert delivered["raw"] == original
    assert packet == original


def test_malformed_admin_wire_payload_is_not_cached(
    mock_interface: MeshInterface,
) -> None:
    """An undecodable payload may contain a key even when no AdminMessage was produced."""
    payload = admin_pb2.AdminMessage(session_passkey=_KEY).SerializeToString() + b"\x80"
    packet = _admin_packet(payload)
    original = copy.deepcopy(packet)
    iface = mock_interface
    iface.nodesByNum = {}
    node = iface._get_or_create_by_num(_SENDER)
    node["adminSessionPassKey"] = b"previous-key"
    callback = Mock()
    iface.responseHandlers[17] = ResponseHandler(callback)
    intents = iface._handle_packet_from_radio(packet, emit_publication=False)
    cached = node["lastReceived"]
    assert cached["decoded"]["admin"][DECODE_ERROR_KEY].startswith("decode-failed:")
    assert cached["decoded"]["payload"] == b""
    assert cached["raw"].decoded.payload == b""
    assert cached["raw"].id == original.id
    assert node["adminSessionPassKey"] == b"previous-key"
    # The legacy untyped response handler still receives the decode-error
    # packet. Sanitizing the cache must not change correlation semantics.
    callback.assert_called_once()
    assert callback.call_args is not None
    assert callback.call_args.args[0]["decoded"]["payload"] == payload
    assert packet == original
    assert intents[0].payload["packet"]["decoded"]["payload"] == payload


def test_admin_without_passkey_retains_payload_and_omission(
    mock_interface: MeshInterface,
) -> None:
    """Non-secret admin requests keep their wire content without inventing a key."""
    admin = admin_pb2.AdminMessage(get_device_metadata_request=True)
    packet = _admin_packet(admin.SerializeToString())
    iface = mock_interface
    iface.nodesByNum = {}
    iface._handle_packet_from_radio(packet, emit_publication=False)
    node = iface._get_or_create_by_num(_SENDER)
    cached = node["lastReceived"]
    assert "sessionPasskey" not in cached["decoded"]["admin"]
    assert cached["decoded"]["admin"]["raw"] == admin
    assert cached["raw"] == packet
    assert "adminSessionPassKey" not in node


@pytest.mark.parametrize("key_name", ["sessionPasskey", "session_passkey"])
def test_legacy_mapping_redaction_preserves_nonsecret_fields(key_name: str) -> None:
    """Legacy mapping payloads redact both spellings and discard opaque wire bytes."""
    raw = {key_name: _KEY, "other": 42}
    packet: dict[str, Any] = {
        "decoded": {"admin": {key_name: "secret", "raw": raw}, "payload": _KEY},
        "raw": _KEY,
    }
    cached = _sanitize_last_received(packet)
    assert cached["decoded"]["admin"][key_name] == REDACTED_TEXT
    assert cached["decoded"]["admin"]["raw"] == {key_name: REDACTED_BYTES, "other": 42}
    assert cached["decoded"]["payload"] == b""
    assert cached["raw"] == REDACTED_TEXT
    assert raw[key_name] == _KEY
    assert packet["decoded"]["admin"][key_name] == "secret"


def test_admin_port_without_decoded_admin_view_is_redacted() -> None:
    """An incomplete decoded view does not bypass the admin port classification."""
    raw = _admin_packet(
        admin_pb2.AdminMessage(session_passkey=_KEY).SerializeToString()
    )
    packet: dict[str, Any] = {
        "decoded": {"portnum": "ADMIN_APP", "payload": raw.decoded.payload},
        "raw": raw,
    }
    cached = _sanitize_last_received(packet)
    assert cached["decoded"]["payload"] == b""
    assert cached["raw"].decoded.payload == b""
    assert _KEY in raw.decoded.payload
    cached = _sanitize_last_received({"raw": raw})
    assert cached["raw"].decoded.payload == b""


def test_non_admin_packet_keeps_existing_shallow_copy_contract() -> None:
    """No wire or nested-object changes are introduced for non-admin packets."""
    raw = mesh_pb2.MeshPacket()
    raw.decoded.portnum = portnums_pb2.TEXT_MESSAGE_APP
    raw.decoded.payload = _KEY
    packet: dict[str, Any] = MessageToDict(raw)
    packet["raw"] = raw
    packet["decoded"]["payload"] = raw.decoded.payload
    cached = _sanitize_last_received(packet)
    assert cached == packet
    assert cached is not packet
    assert cached["raw"] is raw
    assert cached["decoded"] is packet["decoded"]


def test_legacy_object_with_shared_passkey_storage_is_not_mutated() -> None:
    """A separate wrapper may still share its credential storage with the live packet."""

    class LegacyAdmin:
        def __init__(self, keys: list[bytes]) -> None:
            self._keys = keys

        @property
        def session_passkey(self) -> bytes:
            return self._keys[0]

        @session_passkey.setter
        def session_passkey(self, value: bytes) -> None:
            self._keys[0] = value

        def __deepcopy__(self, _memo: dict[int, Any]) -> "LegacyAdmin":
            return LegacyAdmin(self._keys)

    raw = LegacyAdmin([_KEY])
    packet: dict[str, Any] = {
        "decoded": {"admin": {"raw": raw}, "payload": _KEY},
    }
    cached = _sanitize_last_received(packet)
    assert cached["decoded"]["admin"]["raw"] == {"session_passkey": REDACTED_BYTES}
    assert cached["decoded"]["payload"] == b""
    assert raw.session_passkey == _KEY


def test_legacy_object_is_not_inspected_to_redact_it() -> None:
    """An unsupported object's accessors must never run during cache sanitization."""
    key_access = Mock(side_effect=RuntimeError("unreadable property"))

    class UnreadableKey:
        @property
        def session_passkey(self) -> bytes:
            key_access()
            raise AssertionError("unexpected key access")

    raw = UnreadableKey()
    cached = _sanitize_last_received({"decoded": {"admin": {"raw": raw}}})
    assert cached["decoded"]["admin"]["raw"] == {"session_passkey": REDACTED_BYTES}
    key_access.assert_not_called()
