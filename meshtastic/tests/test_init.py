"""Meshtastic unit tests for __init__.py."""

import copy
import logging
import re
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, create_autospec

import pytest
import serial as pyserial  # type: ignore[import-untyped]

import meshtastic
from meshtastic import (
    DECODE_ERROR_KEY,
    _on_admin_receive,
    _on_node_info_receive,
    _on_position_receive,
    _on_telemetry_receive,
    _on_text_receive,
    _protocol_runtime,
    _receive_info_update,
    mt_config,
)

from ..mesh_interface import MeshInterface
from ..protobuf import admin_pb2
from ..serial_interface import SerialInterface


@pytest.mark.unit
def test_init_serial_alias_points_to_pyserial_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify meshtastic.serial resolves to the third-party pyserial module."""
    monkeypatch.delattr(meshtastic, "serial", raising=False)
    assert meshtastic.serial is pyserial


@pytest.mark.unit
def test_init_on_text_receive_with_exception(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test _on_text_receive logs error when packet is malformatted."""
    args = SimpleNamespace(camel_case=False)
    monkeypatch.setattr(mt_config, "args", args)
    iface = create_autospec(SerialInterface, instance=True)
    packet: dict[str, Any] = {}
    with caplog.at_level(logging.DEBUG):
        _on_text_receive(iface, packet)
    assert re.search(r"in _on_text_receive", caplog.text, re.MULTILINE)
    assert re.search(r"Malformatted", caplog.text, re.MULTILINE)


@pytest.mark.unit
def test_init_on_position_receive(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_on_position_receive should skip node lookup when sender is not numeric."""
    args = SimpleNamespace(camel_case=False)
    monkeypatch.setattr(mt_config, "args", args)
    iface = create_autospec(SerialInterface, instance=True)
    packet = {"from": "foo", "decoded": {"position": {}}}
    with caplog.at_level(logging.DEBUG):
        _on_position_receive(iface, packet)
    assert re.search(r"in _on_position_receive", caplog.text, re.MULTILINE)
    iface._get_or_create_by_num.assert_not_called()


@pytest.mark.unit
def test_init_on_position_receive_updates_node_position(
    iface_with_nodes: MeshInterface,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Position payloads with a valid sender should update the node's cached position."""
    args = SimpleNamespace(camel_case=False)
    monkeypatch.setattr(mt_config, "args", args)
    iface = iface_with_nodes
    packet: dict[str, Any] = {
        "from": 4808675309,
        "decoded": {"position": {"latitudeI": 123456, "longitudeI": 654321}},
    }

    _on_position_receive(iface, packet)

    node = iface._get_or_create_by_num(4808675309)
    assert "position" in node
    assert node["position"]["latitudeI"] == 123456


@pytest.mark.unit
def test_init_on_position_receive_decode_error_updates_metadata_without_position_state(
    iface_with_nodes: MeshInterface,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decode-error position payloads should update receive metadata but not overwrite position state."""
    args = SimpleNamespace(camel_case=False)
    monkeypatch.setattr(mt_config, "args", args)
    iface = iface_with_nodes
    baseline_node = iface._get_or_create_by_num(1234567890)
    with iface._node_db_lock:
        baseline_node["position"] = {"latitudeI": 111111, "longitudeI": 222222}
        baseline_position = dict(baseline_node["position"])
    packet: dict[str, Any] = {
        "from": 1234567890,
        "decoded": {"position": {DECODE_ERROR_KEY: "decode-failed: malformed"}},
    }

    _on_position_receive(iface, packet)

    node = iface._get_or_create_by_num(1234567890)
    assert node["position"] == baseline_position
    assert node["lastReceived"]["decoded"]["position"][DECODE_ERROR_KEY].startswith(
        "decode-failed:"
    )


@pytest.mark.unit
def test_init_on_node_info_receive(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test _on_node_info_receive."""
    args = SimpleNamespace(camel_case=False)
    monkeypatch.setattr(mt_config, "args", args)
    iface = iface_with_nodes
    assert iface.myInfo is not None
    iface.myInfo.my_node_num = 2475227164
    packet = {
        "from": 4808675309,
        "decoded": {
            "user": {
                "id": "bar",
            },
        },
    }
    with caplog.at_level(logging.DEBUG):
        _on_node_info_receive(iface, packet)
    assert re.search(r"in _on_node_info_receive", caplog.text, re.MULTILINE)
    node = iface._get_or_create_by_num(4808675309)
    assert node["user"]["id"] == "bar"
    assert iface.nodes is not None
    assert iface.nodes["bar"] is node
    assert node["lastReceived"]["decoded"]["user"]["id"] == "bar"


@pytest.mark.unit
def test_init_on_node_info_receive_decode_error_updates_metadata_without_user_state(
    iface_with_nodes: MeshInterface,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decode-error user payloads should update receive metadata but not overwrite user state."""
    args = SimpleNamespace(camel_case=False)
    monkeypatch.setattr(mt_config, "args", args)
    iface = iface_with_nodes
    baseline_node = iface._get_or_create_by_num(2468135790)
    with iface._node_db_lock:
        baseline_node["user"] = {
            "id": "baseline-user",
            "longName": "Baseline User",
            "shortName": "BU",
        }
        baseline_user_snapshot = copy.deepcopy(baseline_node["user"])
    packet: dict[str, Any] = {
        "from": 2468135790,
        "decoded": {"user": {DECODE_ERROR_KEY: "decode-failed: malformed"}},
    }

    _on_node_info_receive(iface, packet)

    node = iface._get_or_create_by_num(2468135790)
    assert node["user"] == baseline_user_snapshot
    assert node["lastReceived"]["decoded"]["user"][DECODE_ERROR_KEY].startswith(
        "decode-failed:"
    )


@pytest.mark.unit
def test_init_on_node_info_receive_returns_when_sender_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_on_node_info_receive should return early when packet lacks sender metadata."""
    args = SimpleNamespace(camel_case=False)
    monkeypatch.setattr(mt_config, "args", args)
    iface = create_autospec(SerialInterface, instance=True)
    packet = {"decoded": {"user": {"id": "bar"}}}

    _on_node_info_receive(iface, packet)

    iface._get_or_create_by_num.assert_not_called()


@pytest.mark.unit
def test_init_on_admin_receive_redacts_last_received(
    iface_with_nodes: MeshInterface,
) -> None:
    """A legacy object payload is replaced by a redaction marker in the cache."""
    iface = iface_with_nodes
    packet: dict[str, Any] = {
        "from": 4808675309,
        "decoded": {
            "admin": {
                "raw": SimpleNamespace(session_passkey=b"abc123"),
            }
        },
        "rxTime": 100,
        "rxSnr": 5.0,
        "hopLimit": 3,
    }

    _on_admin_receive(iface, packet)

    node = iface._get_or_create_by_num(4808675309)
    assert node["adminSessionPassKey"] == b"abc123"
    last_received_admin = node["lastReceived"]["decoded"]["admin"]
    assert isinstance(last_received_admin, dict)
    # The legacy object is never copied or inspected; only a marker is cached.
    assert last_received_admin["raw"] == {"session_passkey": b"<redacted>"}
    # Input packet should remain unchanged for callback consumers.
    assert packet["decoded"]["admin"]["raw"].session_passkey == b"abc123"


@pytest.mark.unit
def test_init_on_admin_receive_redacts_last_received_dict_raw_payload(
    iface_with_nodes: MeshInterface,
) -> None:
    """Admin packets with dict raw payload should redact session passkey only."""
    iface = iface_with_nodes
    packet: dict[str, Any] = {
        "from": 4808675309,
        "decoded": {
            "admin": {
                "raw": {
                    "session_passkey": b"abc123",
                    "get_device_metadata_response": {"firmware_version": "2.7.18"},
                }
            }
        },
        "rxTime": 100,
        "rxSnr": 5.0,
        "hopLimit": 3,
    }

    _on_admin_receive(iface, packet)

    node = iface._get_or_create_by_num(4808675309)
    last_received_admin = node["lastReceived"]["decoded"]["admin"]
    assert isinstance(last_received_admin, dict)
    assert isinstance(last_received_admin.get("raw"), dict)
    assert last_received_admin["raw"]["session_passkey"] == b"<redacted>"
    assert (
        last_received_admin["raw"]["get_device_metadata_response"]["firmware_version"]
        == "2.7.18"
    )
    # Input packet should remain unchanged for callback consumers.
    assert packet["decoded"]["admin"]["raw"]["session_passkey"] == b"abc123"


@pytest.mark.unit
def test_init_on_admin_receive_uses_sentinel_when_object_redaction_fails(
    iface_with_nodes: MeshInterface,
) -> None:
    """Failing object redaction should still guarantee a redacted sentinel payload."""

    class _UnredactableRaw:
        """Raw admin payload stand-in that refuses passkey redaction mutation."""

        def __init__(self) -> None:
            """Initialize with a non-redacted session passkey value."""
            self._session_passkey = b"abc123"

        @property
        def session_passkey(self) -> bytes:
            """Return the current session passkey bytes."""
            return self._session_passkey

        @session_passkey.setter
        def session_passkey(self, _value: bytes) -> None:
            """Reject writes to simulate an immutable field."""
            raise RuntimeError("session_passkey is read-only")

        def __delattr__(self, name: str) -> None:
            """Reject deletion of the session passkey attribute."""
            if name == "session_passkey":
                raise RuntimeError("cannot delete session_passkey")
            super().__delattr__(name)

        def __deepcopy__(self, _memo: dict[int, Any]) -> "_UnredactableRaw":
            """Return self so deepcopy cannot produce a mutable clone."""
            return self

    iface = iface_with_nodes
    packet: dict[str, Any] = {
        "from": 4808675309,
        "decoded": {"admin": {"raw": _UnredactableRaw()}},
        "rxTime": 100,
        "rxSnr": 5.0,
        "hopLimit": 3,
    }

    _on_admin_receive(iface, packet)

    node = iface._get_or_create_by_num(4808675309)
    last_received_admin = node["lastReceived"]["decoded"]["admin"]
    assert isinstance(last_received_admin, dict)
    assert last_received_admin["raw"] == {"session_passkey": b"<redacted>"}


@pytest.mark.unit
def test_init_on_admin_receive_uses_delattr_fallback_when_assignment_fails(
    iface_with_nodes: MeshInterface,
) -> None:
    """Admin redaction should keep object payloads when delattr fallback succeeds."""

    class _DelattrOnlyRaw:
        def __init__(self) -> None:
            self.session_passkey = b"abc123"

        def __setattr__(self, name: str, value: Any) -> None:
            if name == "session_passkey" and hasattr(self, "session_passkey"):
                raise RuntimeError("session_passkey is immutable")
            object.__setattr__(self, name, value)

    iface = iface_with_nodes
    packet: dict[str, Any] = {
        "from": 4808675309,
        "decoded": {"admin": {"raw": _DelattrOnlyRaw()}},
        "rxTime": 100,
        "rxSnr": 5.0,
        "hopLimit": 3,
    }

    _on_admin_receive(iface, packet)

    node = iface._get_or_create_by_num(4808675309)
    last_received_admin = node["lastReceived"]["decoded"]["admin"]
    redacted_raw = last_received_admin["raw"]
    assert not hasattr(redacted_raw, "session_passkey")


@pytest.mark.unit
def test_init_on_admin_receive_redacts_non_dict_admin_payload(
    iface_with_nodes: MeshInterface,
) -> None:
    """Non-dict admin payloads should be replaced with the redacted text sentinel."""
    iface = iface_with_nodes
    packet: dict[str, Any] = {
        "from": 4808675309,
        "decoded": {"admin": "not-a-dict"},
        "rxTime": 100,
        "rxSnr": 5.0,
        "hopLimit": 3,
    }

    _on_admin_receive(iface, packet)

    node = iface._get_or_create_by_num(4808675309)
    assert node["lastReceived"]["decoded"]["admin"] == "<redacted>"


@pytest.mark.unit
def test_init_on_telemetry_receive_updates_metrics_dict(
    iface_with_nodes: MeshInterface,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Telemetry metrics should merge into the node database under the matching key."""
    args = SimpleNamespace(camel_case=False)
    monkeypatch.setattr(mt_config, "args", args)
    iface = iface_with_nodes
    packet: dict[str, Any] = {
        "from": 4808675309,
        "decoded": {"telemetry": {"deviceMetrics": {"batteryLevel": 95}}},
    }

    _on_telemetry_receive(iface, packet)

    node = iface._get_or_create_by_num(4808675309)
    assert node["deviceMetrics"]["batteryLevel"] == 95


@pytest.mark.unit
def test_init_on_telemetry_receive_ignores_non_dict_metric_payload(
    iface_with_nodes: MeshInterface,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Telemetry update should return early when selected metric payload is not a dict."""
    args = SimpleNamespace(camel_case=False)
    monkeypatch.setattr(mt_config, "args", args)
    iface = iface_with_nodes
    packet: dict[str, Any] = {
        "from": 4808675309,
        "decoded": {"telemetry": {"deviceMetrics": 123}},
    }

    _on_telemetry_receive(iface, packet)

    node = iface._get_or_create_by_num(4808675309)
    assert "deviceMetrics" not in node


@pytest.mark.unit
def test_init_on_telemetry_receive_replaces_non_dict_existing_metrics(
    iface_with_nodes: MeshInterface,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Telemetry update should replace non-dict cached metrics before merging."""
    args = SimpleNamespace(camel_case=False)
    monkeypatch.setattr(mt_config, "args", args)
    iface = iface_with_nodes
    node = iface._get_or_create_by_num(4808675309)
    node["deviceMetrics"] = "invalid"
    packet: dict[str, Any] = {
        "from": 4808675309,
        "decoded": {"telemetry": {"deviceMetrics": {"voltage": 4.2}}},
    }

    _on_telemetry_receive(iface, packet)

    metrics = node["deviceMetrics"]
    assert isinstance(metrics, dict)
    assert metrics.get("voltage") == 4.2


@pytest.mark.unit
def test_init_on_telemetry_receive_ignores_unknown_metrics_payloads(
    iface_with_nodes: MeshInterface,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Telemetry update should return early when no known metrics object is present."""
    args = SimpleNamespace(camel_case=False)
    monkeypatch.setattr(mt_config, "args", args)
    iface = iface_with_nodes
    packet: dict[str, Any] = {
        "from": 4808675309,
        "decoded": {"telemetry": {"mysteryMetrics": {"value": 1}}},
    }

    _on_telemetry_receive(iface, packet)

    node = iface._get_or_create_by_num(4808675309)
    assert "deviceMetrics" not in node
    assert "environmentMetrics" not in node
    assert "airQualityMetrics" not in node
    assert "powerMetrics" not in node
    assert "localStats" not in node


@pytest.mark.unit
def test_init_on_admin_receive_returns_when_passkey_missing(
    iface_with_nodes: MeshInterface,
) -> None:
    """Admin handler should return without mutating passkey when payload lacks one."""
    iface = iface_with_nodes
    packet: dict[str, Any] = {
        "from": 4808675309,
        "decoded": {"admin": {"raw": {}}},
        "rxTime": 100,
    }

    _on_admin_receive(iface, packet)

    node = iface._get_or_create_by_num(4808675309)
    assert "adminSessionPassKey" not in node


@pytest.mark.unit
def test_receive_handlers_skip_packet_summary_when_debug_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receive handlers should not build packet summaries unless DEBUG is enabled."""
    iface = create_autospec(SerialInterface, instance=True)
    packet_summary = MagicMock()
    monkeypatch.setattr(_protocol_runtime, "_packet_debug_summary", packet_summary)
    monkeypatch.setattr(
        _protocol_runtime.logger,
        "isEnabledFor",
        lambda _level: False,
    )
    cases: tuple[tuple[Callable[[Any, dict[str, Any]], None], dict[str, Any]], ...] = (
        (_on_text_receive, {"decoded": {"payload": b"hello"}}),
        (_on_position_receive, {}),
        (_on_node_info_receive, {}),
        (_on_telemetry_receive, {}),
    )

    for handler, packet in cases:
        handler(iface, packet)

    packet_summary.assert_not_called()


@pytest.mark.unit
def test_init_on_telemetry_receive_does_not_merge_decode_error_metrics(
    iface_with_nodes: MeshInterface,
) -> None:
    """Partially decoded telemetry must not overwrite cached metric state."""
    iface = iface_with_nodes
    node = iface._get_or_create_by_num(4808675309)
    with iface._node_db_lock:
        node["deviceMetrics"] = {"batteryLevel": 95}
    packet: dict[str, Any] = {
        "from": 4808675309,
        "decoded": {
            "telemetry": {
                DECODE_ERROR_KEY: "decode-failed: malformed",
                "deviceMetrics": {"batteryLevel": 1},
            }
        },
    }

    _on_telemetry_receive(iface, packet)

    assert node["deviceMetrics"] == {"batteryLevel": 95}
    assert node["lastReceived"]["decoded"]["telemetry"][DECODE_ERROR_KEY].startswith(
        "decode-failed"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw_admin",
    (admin_pb2.AdminMessage(), {"session_passkey": b""}),
    ids=("protobuf", "mapping"),
)
def test_init_on_admin_receive_does_not_replace_valid_key_with_empty_passkey(
    iface_with_nodes: MeshInterface,
    raw_admin: Any,
) -> None:
    """An omitted protobuf bytes field must not erase a cached admin passkey."""
    iface = iface_with_nodes
    node = iface._get_or_create_by_num(4808675309)
    with iface._node_db_lock:
        node["adminSessionPassKey"] = b"existing-key"
    packet: dict[str, Any] = {
        "from": 4808675309,
        "decoded": {"admin": {"raw": raw_admin}},
        "rxTime": 100,
    }

    _on_admin_receive(iface, packet)

    assert node["adminSessionPassKey"] == b"existing-key"


@pytest.mark.unit
def test_init_receive_info_update_keeps_non_admin_packet_object(
    iface_with_nodes: MeshInterface,
) -> None:
    """Non-admin lastReceived cache should be a shallow copy of the packet."""
    iface = iface_with_nodes
    packet: dict[str, Any] = {
        "from": 4808675309,
        "decoded": {"text": "hello"},
        "rxTime": 101,
    }

    _receive_info_update(iface, packet)

    node = iface._get_or_create_by_num(4808675309)
    # lastReceived is always a shallow copy (not the same object) to avoid aliasing
    assert node["lastReceived"] == packet
    assert node["lastReceived"] is not packet


@pytest.mark.unit
def test_init_getattr_raises_for_unknown_attribute() -> None:
    """Verify __getattr__ raises AttributeError for unknown attributes."""
    with pytest.raises(
        AttributeError, match="module 'meshtastic' has no attribute 'nonexistent'"
    ):
        _ = meshtastic.nonexistent


@pytest.mark.unit
def test_init_getattr_caches_serial_on_first_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify __getattr__ caches serial module on first access."""
    # Isolate module-level cache mutation to this test and auto-restore afterwards.
    # `raising=False` allows this test to run whether meshtastic.serial is
    # already cached or not.
    monkeypatch.delattr(meshtastic, "serial", raising=False)

    # First access should trigger lazy load.
    serial = meshtastic.serial
    assert serial is pyserial

    # Second access should use cached value.
    serial2 = meshtastic.serial
    assert serial2 is serial
    assert serial2 is pyserial
