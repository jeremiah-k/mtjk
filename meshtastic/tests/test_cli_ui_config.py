"""Tests for the device UI configuration CLI surface."""

from __future__ import annotations

import argparse
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import meshtastic.cli.device_actions as device_actions
import meshtastic.cli.parser as cli_parser
from meshtastic.node import Node
from meshtastic.protobuf import admin_pb2, device_ui_pb2, mesh_pb2
from meshtastic.tests.cli_device_action_test_helpers import (
    device_action_context as _context,
)
from meshtastic.tests.cli_device_action_test_helpers import (
    device_action_hooks as _hooks,
)
from meshtastic.tests.cli_device_action_test_helpers import (
    device_interface_mock as _interface,
)


def _build_response_packet(
    *,
    config: device_ui_pb2.DeviceUIConfig | None = None,
) -> dict[str, Any]:
    """Build a decoded admin packet mirroring the real wire shape."""
    raw = admin_pb2.AdminMessage()
    if config is not None:
        raw.get_ui_config_response.CopyFrom(config)
    return {"decoded": {"admin": {"raw": raw}}}


def _stub_node_for_request(
    captured: dict[str, Any],
    iface: MagicMock,
) -> Node:
    """Create a Node double that captures ``_send_admin`` arguments."""

    node = object.__new__(Node)
    node.iface = iface
    node.ensureSessionKey = MagicMock()  # type: ignore[method-assign]

    def _fake_send_admin(
        msg: admin_pb2.AdminMessage,
        wantResponse: bool = False,
        onResponse: Any = None,
        adminIndex: int | None = None,
        responseWaitAttr: str | None = None,
    ) -> mesh_pb2.MeshPacket:
        _ = (wantResponse, adminIndex)
        captured["msg"] = msg
        captured["onResponse"] = onResponse
        captured["responseWaitAttr"] = responseWaitAttr
        return mesh_pb2.MeshPacket(id=1)

    node._send_admin = _fake_send_admin  # type: ignore[assignment]
    return node


# ---------------------------------------------------------------------------
# requestUiConfig: Node request method
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_requestUiConfig_returns_config_when_response_fires() -> None:
    """The DeviceUIConfig is returned when the response callback is invoked."""

    expected = device_ui_pb2.DeviceUIConfig(version=3, screen_brightness=85)
    captured: dict[str, Any] = {}
    iface = _interface()
    node = _stub_node_for_request(captured, iface)

    def _fire_response_later() -> None:
        # Mirror the real pipeline: the response packet arrives concurrently
        # while the bounded correlation wait is running.
        on_response = captured.get("onResponse")
        if on_response is not None:
            on_response(_build_response_packet(config=expected))

    timer = threading.Timer(0.05, _fire_response_later)
    timer.start()

    result = node.requestUiConfig(response_timeout_seconds=2.0)
    assert result == expected
    assert captured["msg"].HasField("get_ui_config_request") is True
    # Response-only requests wait on the correlated response event; they must
    # not register scoped WAIT_ATTR_NAK bookkeeping that nothing retires.
    assert captured["responseWaitAttr"] is None


@pytest.mark.unit
def test_requestUiConfig_returns_none_when_response_times_out() -> None:
    """A response that never fires surfaces as ``None`` after the bounded wait."""

    captured: dict[str, Any] = {}
    iface = _interface()
    node = _stub_node_for_request(captured, iface)

    result = node.requestUiConfig(response_timeout_seconds=0.05)
    assert result is None


@pytest.mark.unit
def test_requestUiConfig_rejects_non_positive_timeout() -> None:
    """Invalid bounded-wait values fail before sending an admin request."""
    captured: dict[str, Any] = {}
    node = _stub_node_for_request(captured, _interface())

    with pytest.raises(ValueError, match="response timeout must be positive"):
        node.requestUiConfig(response_timeout_seconds=0)

    assert captured == {}


@pytest.mark.unit
@pytest.mark.parametrize("send_finished_at, expected_wait", [(11.5, 0.5), (13.0, 0.0)])
def test_private_admin_response_deadline_includes_send_time(
    monkeypatch: pytest.MonkeyPatch, send_finished_at: float, expected_wait: float
) -> None:
    """Sending cannot grant a typed getter another full response timeout."""
    captured: dict[str, Any] = {}
    iface = _interface()
    node = _stub_node_for_request(captured, iface)
    completed = threading.Event()
    iface._request_wait_runtime = MagicMock()
    monkeypatch.setattr(
        "meshtastic.node.time", SimpleNamespace(monotonic=lambda: send_finished_at)
    )

    with (
        patch("meshtastic.node.threading.Event", return_value=completed),
        patch.object(completed, "wait", return_value=False) as wait,
    ):
        result = node._request_admin_response(
            admin_pb2.AdminMessage(get_ui_config_request=True),
            "get_ui_config_response",
            device_ui_pb2.DeviceUIConfig,
            response_timeout_seconds=12.0,
            response_deadline=12.0,
        )

    assert result is None
    wait.assert_called_once_with(timeout=expected_wait)
    iface._request_wait_runtime.retire_wait_request.assert_called_once_with(
        "receivedNak", request_id=1
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "response_received_at, accepted", [(11.0, True), (13.0, False)]
)
def test_private_admin_response_deadline_retains_only_timely_responses(
    monkeypatch: pytest.MonkeyPatch, response_received_at: float, accepted: bool
) -> None:
    """A timely synchronous response survives exhausted post-send wait budget."""
    expected = device_ui_pb2.DeviceUIConfig(version=3, screen_brightness=85)
    clock = {"now": response_received_at}
    node = _stub_node_for_request({}, _interface())
    monkeypatch.setattr(
        "meshtastic.node.time", SimpleNamespace(monotonic=lambda: clock["now"])
    )

    def send(*args: Any, **kwargs: Any) -> mesh_pb2.MeshPacket:
        kwargs["onResponse"](_build_response_packet(config=expected))
        clock["now"] = 14.0
        return mesh_pb2.MeshPacket(id=1)

    monkeypatch.setattr(node, "_send_admin", send)
    result = node._request_admin_response(
        admin_pb2.AdminMessage(get_ui_config_request=True),
        "get_ui_config_response",
        device_ui_pb2.DeviceUIConfig,
        response_timeout_seconds=12.0,
        response_deadline=12.0,
    )

    assert result == (expected if accepted else None)


# ---------------------------------------------------------------------------
# storeUiConfig: Node request method
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_storeUiConfig_copies_config_into_admin_message() -> None:
    """StoreUiConfig populates ``store_ui_config`` on the AdminMessage sent."""
    captured: dict[str, Any] = {}

    node = object.__new__(Node)
    node.iface = MagicMock()
    node.ensureSessionKey = MagicMock()  # type: ignore[method-assign]

    def _fake_send_admin_op(message: admin_pb2.AdminMessage) -> mesh_pb2.MeshPacket:
        captured["msg"] = message
        return mesh_pb2.MeshPacket(id=42)

    node._send_admin_op = _fake_send_admin_op  # type: ignore[method-assign]

    config = device_ui_pb2.DeviceUIConfig(version=7, screen_brightness=120)
    sent_packet = node.storeUiConfig(config)
    assert sent_packet is not None
    assert sent_packet.id == 42
    assert captured["msg"].store_ui_config == config


# ---------------------------------------------------------------------------
# YAML dump/load helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_yaml_round_trip_preserves_fields(tmp_path: Path) -> None:
    """Dump + load round-trip preserves the populated fields on the proto."""

    config = device_ui_pb2.DeviceUIConfig()
    config.version = 4
    config.screen_brightness = 200
    config.theme = device_ui_pb2.Theme.DARK
    config.language = device_ui_pb2.Language.ENGLISH

    dumped = device_actions._yaml_dump_ui_config(config)
    payload = tmp_path / "ui_config.yaml"
    payload.write_text(dumped, encoding="utf8")

    hooks = _hooks()
    loaded = device_actions._load_ui_config_document(str(payload), hooks)

    assert loaded.version == config.version
    assert loaded.screen_brightness == config.screen_brightness
    assert loaded.theme == config.theme
    assert loaded.language == config.language


@pytest.mark.unit
def test_load_ui_config_document_terminates_on_missing_file() -> None:
    """A missing path exits via the CLI hook with code 1."""

    exits: list[tuple[str, int]] = []
    with pytest.raises(SystemExit):
        device_actions._load_ui_config_document(
            "/no/such/path/ui_config.yaml",
            _hooks(exits=exits),
        )
    assert exits and exits[0][1] == 1
    assert "Failed to read UI config" in exits[0][0]


@pytest.mark.unit
def test_load_ui_config_document_terminates_on_non_mapping_yaml(
    tmp_path: Path,
) -> None:
    """A YAML sequence at the top level exits with code 1."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("- 1\n- 2\n", encoding="utf8")
    exits: list[tuple[str, int]] = []
    with pytest.raises(SystemExit):
        device_actions._load_ui_config_document(str(bad), _hooks(exits=exits))
    assert exits and exits[0][1] == 1
    assert "mapping" in exits[0][0]


@pytest.mark.unit
def test_load_ui_config_document_terminates_on_unknown_field(
    tmp_path: Path,
) -> None:
    """Unknown proto fields are rejected (ParseError surfaces as a CLI exit)."""

    payload = tmp_path / "ui.yaml"
    payload.write_text("version: 1\ntotally_made_up_field: 42\n", encoding="utf8")
    exits: list[tuple[str, int]] = []
    with pytest.raises(SystemExit):
        device_actions._load_ui_config_document(str(payload), _hooks(exits=exits))
    assert exits and exits[0][1] == 1
    assert "Invalid device UI configuration" in exits[0][0]


# ---------------------------------------------------------------------------
# _handle_admin_utility_actions integration
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_handle_get_ui_config_prints_yaml() -> None:
    """``--get-ui-config`` prints the dumped YAML and closes the interface."""

    expected = device_ui_pb2.DeviceUIConfig(version=2, screen_brightness=99)
    iface = _interface()
    iface.getNode.return_value.requestUiConfig.return_value = expected
    prints: list[str] = []
    context = _context(iface, {"get_ui_config": True})

    device_actions._handle_admin_utility_actions(context, _hooks(prints=prints))

    iface.getNode.assert_called_once_with("^local", False)
    joined = "\n".join(prints)
    assert "screenBrightness: 99" in joined or "screen_brightness: 99" in joined
    assert context.outcome.close_now is True


@pytest.mark.unit
def test_handle_get_ui_config_missing_response_terminates() -> None:
    """Missing response exits 1 with a capability-oriented firmware hint."""
    iface = _interface()
    iface.getNode.return_value.requestUiConfig.return_value = None
    exits: list[tuple[str, int]] = []
    with pytest.raises(SystemExit):
        device_actions._handle_admin_utility_actions(
            _context(iface, {"get_ui_config": True}),
            _hooks(exits=exits),
        )
    assert exits and exits[0][1] == 1
    assert "device UI configuration" in exits[0][0]


@pytest.mark.unit
def test_handle_store_ui_config_reads_file_and_calls_node(tmp_path: Path) -> None:
    """``--store-ui-config`` loads the YAML and forwards it to node.storeUiConfig."""

    config = device_ui_pb2.DeviceUIConfig()
    config.version = 5
    config.screen_brightness = 123
    dumped = device_actions._yaml_dump_ui_config(config)
    payload = tmp_path / "ui.yaml"
    payload.write_text(dumped, encoding="utf8")

    iface = _interface()
    context = _context(iface, {"store_ui_config": str(payload)})
    device_actions._handle_admin_utility_actions(context, _hooks())

    iface.getNode.assert_called_once_with("^local", False)
    forwarded = iface.getNode.return_value.storeUiConfig.call_args.args[0]
    assert isinstance(forwarded, device_ui_pb2.DeviceUIConfig)
    assert forwarded.version == config.version
    assert forwarded.screen_brightness == config.screen_brightness
    assert context.outcome.close_now is True
    assert context.outcome.wait_for_ack_nak is True


# ---------------------------------------------------------------------------
# Parser: --get-ui-config must live on the outer (non-mutex) group
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parser_accepts_get_ui_config_with_reboot_argument() -> None:
    """``--get-ui-config`` coexists with ``--reboot`` because it is on outer."""

    parser = argparse.ArgumentParser(prog="mtjk", add_help=False)
    cli_parser.addRemoteAdminArgs(parser)
    args = parser.parse_args(["--get-ui-config", "--reboot"])
    assert args.get_ui_config is True
    assert args.reboot is True


@pytest.mark.unit
def test_request_admin_response_ignores_malformed_has_field_and_times_out() -> None:
    """Malformed response doubles cannot consume/complete the pending admin getter."""
    captured: dict[str, Any] = {}
    node = _stub_node_for_request(captured, _interface())

    def _fire_malformed_response_later() -> None:
        raw = MagicMock()
        raw.HasField.side_effect = ValueError("bad oneof")
        callback = captured.get("onResponse")
        if callback is not None:
            callback({"decoded": {"admin": {"raw": raw}}})

    timer = threading.Timer(0.05, _fire_malformed_response_later)
    timer.start()

    assert node.requestUiConfig(response_timeout_seconds=0.5) is None


@pytest.mark.unit
def test_request_admin_response_ignores_wrong_response_variant() -> None:
    """A different admin response oneof must not satisfy the UI-config wait."""
    captured: dict[str, Any] = {}
    node = _stub_node_for_request(captured, _interface())

    def _fire_wrong_variant_later() -> None:
        raw = admin_pb2.AdminMessage()
        raw.get_device_connection_status_response.SetInParent()
        callback = captured.get("onResponse")
        if callback is not None:
            callback({"decoded": {"admin": {"raw": raw}}})

    timer = threading.Timer(0.05, _fire_wrong_variant_later)
    timer.start()

    assert node.requestUiConfig(response_timeout_seconds=0.5) is None


@pytest.mark.unit
def test_request_admin_response_returns_none_when_transport_returns_no_packet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sender that produces no request packet exits without entering ACK/response waits."""
    node = object.__new__(Node)
    wait = MagicMock()
    monkeypatch.setattr(
        "meshtastic.node._send_admin_with_ack_scope", MagicMock(return_value=None)
    )
    monkeypatch.setattr("meshtastic.node._wait_for_admin_ack", wait)

    assert node.requestUiConfig(response_timeout_seconds=1.0) is None
    wait.assert_not_called()
