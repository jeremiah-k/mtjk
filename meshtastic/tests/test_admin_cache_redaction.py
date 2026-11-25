"""Regression coverage for administrative passkeys in received node records."""

import base64
import copy
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from google.protobuf.json_format import MessageToDict

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


def test_admin_receive_redacts_every_cached_representation() -> None:
    """Only the cache copy changes; request handlers and publication keep the wire packet."""
    admin = admin_pb2.AdminMessage(session_passkey=_KEY)
    admin.get_device_metadata_response.firmware_version = "2.7.11"
    packet = _admin_packet(admin.SerializeToString())
    original = copy.deepcopy(packet)
    callback = Mock()

    with MeshInterface(noProto=True) as iface:
        iface.nodesByNum = {}
        iface.responseHandlers[17] = ResponseHandler(callback)
        intents = iface._handle_packet_from_radio(packet, emit_publication=False)
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
        assert (
            decoded["admin"]["getDeviceMetadataResponse"]["firmwareVersion"] == "2.7.11"
        )
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
    assert len(intents) == 1
    published = intents[0].payload["packet"]
    for delivered in (callback_packet, published):
        assert delivered["decoded"]["admin"]["raw"] == admin
        assert delivered["decoded"]["admin"]["sessionPasskey"] == base64.b64encode(
            _KEY
        ).decode("ascii")
        assert delivered["decoded"]["payload"] == original.decoded.payload
        assert delivered["raw"] == original
    assert packet == original


def test_malformed_admin_wire_payload_is_not_cached() -> None:
    """An undecodable payload may contain a key even when no AdminMessage was produced."""
    payload = admin_pb2.AdminMessage(session_passkey=_KEY).SerializeToString() + b"\x80"
    packet = _admin_packet(payload)
    original = copy.deepcopy(packet)
    with MeshInterface(noProto=True) as iface:
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


def test_admin_without_passkey_retains_payload_and_omission() -> None:
    """Non-secret admin requests keep their wire content without inventing a key."""
    admin = admin_pb2.AdminMessage(get_device_metadata_request=True)
    packet = _admin_packet(admin.SerializeToString())
    with MeshInterface(noProto=True) as iface:
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


@pytest.mark.parametrize("copy_returns_self", [True, False])
def test_legacy_object_with_no_effective_redaction_is_replaced(
    copy_returns_self: bool,
) -> None:
    """A failed or non-detached legacy object copy must not retain the key."""

    class ReadOnlyKey:
        @property
        def session_passkey(self) -> bytes:
            """Return a synthetic immutable key."""
            return _KEY

        @session_passkey.setter
        def session_passkey(self, value: bytes) -> None:
            """Simulate a setter that accepts but ignores redaction."""
            _ = value

        def __deepcopy__(self, memo: dict[int, Any]) -> "ReadOnlyKey":
            _ = memo
            return self if copy_returns_self else ReadOnlyKey()

    raw = ReadOnlyKey()
    packet: dict[str, Any] = {"decoded": {"admin": {"raw": raw}}}
    cached = _sanitize_last_received(packet)
    assert cached["decoded"]["admin"]["raw"] == {"session_passkey": REDACTED_BYTES}
    assert raw.session_passkey == _KEY


def test_legacy_object_redacts_both_passkey_spellings() -> None:
    """The native spelling cannot be redacted while leaving its camelCase alias."""
    raw = SimpleNamespace(session_passkey=_KEY, sessionPasskey=_KEY, other=42)
    cached = _sanitize_last_received({"decoded": {"admin": {"raw": raw}}})
    assert cached["decoded"]["admin"]["raw"] == SimpleNamespace(
        session_passkey=REDACTED_BYTES, sessionPasskey=REDACTED_BYTES, other=42
    )
    assert raw.session_passkey == raw.sessionPasskey == _KEY


def test_legacy_object_with_failing_attribute_access_is_not_cached() -> None:
    """A throwing property must fail closed rather than abort cache processing."""

    class UnreadableKey:
        @property
        def session_passkey(self) -> bytes:
            raise RuntimeError("unreadable property")

    raw = UnreadableKey()
    cached = _sanitize_last_received({"decoded": {"admin": {"raw": raw}}})
    assert cached["decoded"]["admin"]["raw"] == {"session_passkey": REDACTED_BYTES}
