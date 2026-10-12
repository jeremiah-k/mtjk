"""Tests for the firmware 2.5-2.8 admin utility CLI actions."""

from unittest.mock import MagicMock

import pytest

import meshtastic.cli.device_actions as device_actions
from meshtastic._interface_errors import MeshInterfaceError
from meshtastic.node import Node, _backup_location_value
from meshtastic.protobuf import admin_pb2, connection_status_pb2, mesh_pb2
from meshtastic.tests.cli_device_action_test_helpers import (
    device_action_context as _context,
)
from meshtastic.tests.cli_device_action_test_helpers import (
    device_action_hooks as _hooks,
)
from meshtastic.tests.cli_device_action_test_helpers import (
    device_interface_mock as _interface,
)


@pytest.mark.unit
def test_backup_location_value_accepts_names_case_insensitively() -> None:
    """flash/SD names map to BackupLocation enum values."""
    assert (
        _backup_location_value("flash") == admin_pb2.AdminMessage.BackupLocation.FLASH
    )
    assert _backup_location_value("SD") == admin_pb2.AdminMessage.BackupLocation.SD


@pytest.mark.unit
def test_backup_location_value_rejects_unknown_names() -> None:
    """Unknown locations raise the interface error naming valid choices."""
    with pytest.raises(MeshInterfaceError, match="Unknown backup location"):
        _backup_location_value("usb")


@pytest.mark.unit
def test_preference_backup_actions_call_node_methods() -> None:
    """Each backup flag drives the matching Node method with its location."""
    interface = _interface()
    prints: list[str] = []
    context = _context(
        interface,
        {"backup_preferences": "sd"},
    )
    device_actions._handle_admin_utility_actions(context, _hooks(prints))
    interface.getNode.assert_called_once_with("^local", False)
    interface.getNode.return_value.backupPreferences.assert_called_once_with("sd")
    assert any("Backing up preferences (sd)" in line for line in prints)
    assert context.outcome.close_now is True
    assert context.outcome.wait_for_ack_nak is True


@pytest.mark.unit
def test_restore_and_remove_backup_actions() -> None:
    """Restore/remove flags route through their Node methods."""
    interface = _interface()
    device_actions._handle_admin_utility_actions(
        _context(interface, {"restore_preferences": "flash"}), _hooks()
    )
    interface.getNode.return_value.restorePreferences.assert_called_once_with("flash")

    interface2 = _interface()
    device_actions._handle_admin_utility_actions(
        _context(interface2, {"remove_backup_preferences": "sd"}), _hooks()
    )
    interface2.getNode.return_value.removeBackupPreferences.assert_called_once_with(
        "sd"
    )


@pytest.mark.unit
def test_delete_file_and_input_event_actions() -> None:
    """File deletion and input events forward their arguments verbatim."""
    interface = _interface()
    device_actions._handle_admin_utility_actions(
        _context(interface, {"delete_file": "/fs/old.cfg"}), _hooks()
    )
    interface.getNode.return_value.deleteFile.assert_called_once_with("/fs/old.cfg")

    interface2 = _interface()
    device_actions._handle_admin_utility_actions(
        _context(
            interface2,
            {"send_input_event": 212, "input_kb_char": "x", "input_touch_x": 5},
        ),
        _hooks(),
    )
    interface2.getNode.return_value.sendInputEvent.assert_called_once_with(
        212, kb_char=ord("x"), touch_x=5, touch_y=0
    )


@pytest.mark.unit
def test_input_event_rejects_multiple_keyboard_characters() -> None:
    """The scalar protobuf keyboard field cannot accept a multi-character token."""
    interface = _interface()
    exits: list[tuple[str, int]] = []

    with pytest.raises(SystemExit):
        device_actions._handle_admin_utility_actions(
            _context(
                interface,
                {"send_input_event": 212, "input_kb_char": "escape"},
            ),
            _hooks(exits=exits),
        )

    assert exits == [("ERROR: --input-kb-char accepts exactly one character.", 1)]
    interface.getNode.assert_not_called()


def _node_with_admin_sender(
    *, local: bool
) -> tuple[Node, MagicMock, MagicMock, MagicMock]:
    """Build a minimal Node whose admin transport records one-shot operations."""
    node = object.__new__(Node)
    interface = MagicMock()
    interface.localNode = node if local else object()
    node.iface = interface
    ensure_session_key = MagicMock()
    node.ensureSessionKey = ensure_session_key  # type: ignore[method-assign]
    sender = MagicMock(return_value=mesh_pb2.MeshPacket(id=17))
    node._send_admin = sender  # type: ignore[method-assign]
    return node, interface, sender, ensure_session_key


@pytest.mark.unit
def test_input_event_rejects_empty_keyboard_character() -> None:
    """An explicitly supplied empty keyboard token is not silently treated as zero."""
    interface = _interface()
    exits: list[tuple[str, int]] = []

    with pytest.raises(SystemExit):
        device_actions._handle_admin_utility_actions(
            _context(interface, {"send_input_event": 212, "input_kb_char": ""}),
            _hooks(exits=exits),
        )

    assert exits == [("ERROR: --input-kb-char accepts exactly one character.", 1)]
    interface.getNode.assert_not_called()


@pytest.mark.unit
def test_send_admin_op_waits_for_remote_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remote one-shot admin operations do not report success before ACK/NAK."""
    node, _interface_mock, sender, ensure_session_key = _node_with_admin_sender(
        local=False
    )
    wait_for_ack = MagicMock()
    monkeypatch.setattr("meshtastic.node._wait_for_admin_ack", wait_for_ack)
    message = admin_pb2.AdminMessage(
        backup_preferences=admin_pb2.AdminMessage.BackupLocation.FLASH
    )

    request = node._send_admin_op(message)

    ensure_session_key.assert_called_once_with()
    sender.assert_called_once_with(message, onResponse=node.onAckNak)
    wait_for_ack.assert_called_once_with(node, request)


@pytest.mark.unit
def test_send_admin_op_keeps_local_send_nonblocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A directly connected local admin operation sends without an ACK wait."""
    node, _interface_mock, sender, _ensure_session_key = _node_with_admin_sender(
        local=True
    )
    wait_for_ack = MagicMock()
    monkeypatch.setattr("meshtastic.node._wait_for_admin_ack", wait_for_ack)
    message = admin_pb2.AdminMessage(
        backup_preferences=admin_pb2.AdminMessage.BackupLocation.FLASH
    )

    request = node._send_admin_op(message)

    sender.assert_called_once_with(message)
    wait_for_ack.assert_not_called()
    assert request is sender.return_value


@pytest.mark.unit
def test_toggle_muted_node_uses_nodedb_action_path() -> None:
    """ToggleMutedNode rides the shared node-database action table."""
    interface = _interface()
    context = _context(interface, {"toggle_muted_node": "!abcd1234"})
    device_actions._handle_node_database_actions(context)
    interface.getNode.return_value.toggleMutedNode.assert_called_once_with("!abcd1234")
    assert context.outcome.close_now is True


@pytest.mark.unit
def test_reset_nodedb_skips_final_ack_wait() -> None:
    """A node DB reset reboots the device, so no final ACK wait is enrolled.

    Firmware schedules the reboot while handling the reset request (and drops
    Bluetooth immediately), so the shared finalize-time ACK wait would time
    out instead of observing the reset's completion.
    """
    interface = _interface()
    context = _context(interface, {"reset_nodedb": True})
    device_actions._handle_node_database_actions(context)
    interface.getNode.return_value.resetNodeDb.assert_called_once_with()
    assert context.outcome.close_now is True
    assert context.outcome.wait_for_ack_nak is True
    assert context.outcome.skip_ack_wait is True


@pytest.mark.unit
def test_connection_status_prints_each_transport() -> None:
    """The status printer reports present transports with their details."""
    status = connection_status_pb2.DeviceConnectionStatus()
    status.wifi.status.is_connected = True
    status.wifi.status.ip_address = 0x2A01A8C0
    status.wifi.status.is_mqtt_connected = True
    status.wifi.ssid = "meshnet"
    status.serial.is_connected = True
    status.serial.baud = 115200

    prints: list[str] = []
    device_actions._print_device_connection_status(status, _hooks(prints))

    joined = "\n".join(prints)
    assert "wifi: connected (192.168.1.42, mqtt, ssid meshnet)" in joined
    assert "serial: connected (baud 115200)" in joined
    assert "bluetooth" not in joined


@pytest.mark.unit
def test_connection_status_missing_response_terminates() -> None:
    """A missing status response exits with the firmware-version hint."""
    interface = _interface()
    interface.getNode.return_value.requestDeviceConnectionStatus.return_value = None
    exits: list[tuple[str, int]] = []
    with pytest.raises(SystemExit):
        device_actions._handle_admin_utility_actions(
            _context(interface, {"request_connection_status": True}),
            _hooks(exits=exits),
        )
    assert exits and exits[0][1] == 1
    assert "connection-status queries" in exits[0][0]


@pytest.mark.unit
def test_ip4_to_str_formats_dotted_quad() -> None:
    """Packed IPv4 addresses render as dotted quads."""
    assert device_actions._ip4_to_str(0x2A01A8C0) == "192.168.1.42"
    assert device_actions._ip4_to_str(0x0100007F) == "127.0.0.1"


@pytest.mark.unit
def test_input_event_rejects_keyboard_code_outside_firmware_byte() -> None:
    """Firmware stores kb_char as an unsigned byte, so Unicode must not truncate."""
    interface = _interface()
    exits: list[tuple[str, int]] = []

    with pytest.raises(SystemExit):
        device_actions._handle_admin_utility_actions(
            _context(interface, {"send_input_event": 212, "input_kb_char": "€"}),
            _hooks(exits=exits),
        )

    assert exits == [
        ("ERROR: --input-kb-char must fit the firmware 8-bit keyboard field.", 1)
    ]
    interface.getNode.assert_not_called()


@pytest.mark.unit
def test_connection_status_prints_all_optional_details() -> None:
    """WiFi/Ethernet/Bluetooth status output covers every optional detail branch."""
    status = connection_status_pb2.DeviceConnectionStatus()
    status.wifi.status.is_connected = False
    status.wifi.status.ip_address = 0x0501A8C0  # 192.168.1.5 in firmware packing
    status.wifi.status.is_syslog_connected = True
    status.wifi.ssid = "meshnet"
    status.wifi.rssi = -42
    status.ethernet.status.is_connected = True
    status.ethernet.status.is_mqtt_connected = True
    status.bluetooth.is_connected = True
    status.bluetooth.rssi = -55
    status.serial.is_connected = False
    status.serial.baud = 9600

    prints: list[str] = []
    device_actions._print_device_connection_status(status, _hooks(prints))

    assert "wifi: disconnected (192.168.1.5, syslog, ssid meshnet, rssi -42)" in prints
    assert "ethernet: connected (mqtt)" in prints
    assert "bluetooth: connected (rssi -55)" in prints
    assert "serial: disconnected (baud 9600)" in prints


@pytest.mark.unit
def test_connection_status_without_nested_network_state_is_unknown() -> None:
    """Present transport sections without a nested status remain explicitly unknown."""
    status = connection_status_pb2.DeviceConnectionStatus()
    status.wifi.ssid = "meshnet"

    prints: list[str] = []
    device_actions._print_device_connection_status(status, _hooks(prints))

    assert prints == ["wifi: unknown (ssid meshnet)"]


@pytest.mark.unit
def test_node_admin_utility_methods_build_expected_admin_fields() -> None:
    """Every new one-shot Node utility constructs the firmware field it documents."""
    node = object.__new__(Node)
    sender = MagicMock(return_value=mesh_pb2.MeshPacket(id=9))
    node._send_admin_op = sender  # type: ignore[method-assign]

    assert node.backupPreferences("flash") is sender.return_value
    assert sender.call_args.args[0].backup_preferences == admin_pb2.AdminMessage.FLASH
    node.restorePreferences("sd")
    assert sender.call_args.args[0].restore_preferences == admin_pb2.AdminMessage.SD
    node.removeBackupPreferences("flash")
    assert (
        sender.call_args.args[0].remove_backup_preferences
        == admin_pb2.AdminMessage.FLASH
    )
    node.toggleMutedNode("!0000002a")
    assert sender.call_args.args[0].toggle_muted_node == 42
    node.deleteFile("/prefs/uiconfig.proto")
    assert sender.call_args.args[0].delete_file_request == "/prefs/uiconfig.proto"
    node.sendInputEvent(17, kb_char=65, touch_x=10, touch_y=20)
    event = sender.call_args.args[0].send_input_event
    assert (event.event_code, event.kb_char, event.touch_x, event.touch_y) == (
        17,
        65,
        10,
        20,
    )


@pytest.mark.unit
def test_request_connection_status_delegates_to_shared_response_helper() -> None:
    """The connection-status getter requests its named response with the supplied timeout."""
    node = object.__new__(Node)
    expected = connection_status_pb2.DeviceConnectionStatus()
    requester = MagicMock(return_value=expected)
    node._request_admin_response = requester  # type: ignore[method-assign]

    assert node.requestDeviceConnectionStatus(response_timeout_seconds=3.5) is expected

    message, field_name, response_type = requester.call_args.args
    assert message.get_device_connection_status_request is True
    assert field_name == "get_device_connection_status_response"
    assert response_type is connection_status_pb2.DeviceConnectionStatus
    assert requester.call_args.kwargs == {"response_timeout_seconds": 3.5}


@pytest.mark.unit
def test_connection_status_action_prints_successful_response() -> None:
    """The CLI action prints a received status rather than only testing the helper."""
    interface = _interface()
    status = connection_status_pb2.DeviceConnectionStatus()
    status.serial.is_connected = True
    status.serial.baud = 115200
    interface.getNode.return_value.requestDeviceConnectionStatus.return_value = status
    prints: list[str] = []

    context = _context(interface, {"request_connection_status": True})
    device_actions._handle_admin_utility_actions(context, _hooks(prints))

    assert "serial: connected (baud 115200)" in prints
    assert context.outcome.close_now is True


@pytest.mark.unit
def test_request_node_remote_hardware_pins_delegates_to_shared_response_helper() -> (
    None
):
    """The remote-hardware-pins getter requests its named response
    with the supplied timeout."""
    node = object.__new__(Node)
    expected = admin_pb2.NodeRemoteHardwarePinsResponse()
    requester = MagicMock(return_value=expected)
    node._request_admin_response = requester  # type: ignore[method-assign]

    assert node.requestNodeRemoteHardwarePins(response_timeout_seconds=3.5) is expected

    message, field_name, response_type = requester.call_args.args
    assert message.get_node_remote_hardware_pins_request is True
    assert field_name == "get_node_remote_hardware_pins_response"
    assert response_type is admin_pb2.NodeRemoteHardwarePinsResponse
    assert requester.call_args.kwargs == {"response_timeout_seconds": 3.5}


@pytest.mark.unit
def test_node_delete_file_keeps_library_level_absolute_path_guard() -> None:
    """Direct library callers still get the Node-level absolute-path validation."""
    node = object.__new__(Node)
    node._raise_interface_error = MagicMock(  # type: ignore[method-assign]
        side_effect=MeshInterfaceError("bad path")
    )
    with pytest.raises(MeshInterfaceError, match="bad path"):
        node.deleteFile("prefs/config.proto")


@pytest.mark.unit
def test_node_send_input_event_accepts_firmware_maxima() -> None:
    """Direct callers can use the exact firmware byte/short maxima."""
    node = object.__new__(Node)
    sender = MagicMock(return_value=mesh_pb2.MeshPacket(id=1))
    node._send_admin_op = sender  # type: ignore[method-assign]

    node.sendInputEvent(255, kb_char=255, touch_x=65535, touch_y=65535)

    event = sender.call_args.args[0].send_input_event
    assert (event.event_code, event.kb_char, event.touch_x, event.touch_y) == (
        255,
        255,
        65535,
        65535,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event_code", -1),
        ("event_code", 256),
        ("kb_char", -1),
        ("kb_char", 256),
        ("touch_x", -1),
        ("touch_x", 65536),
        ("touch_y", -1),
        ("touch_y", 65536),
    ],
)
def test_node_send_input_event_rejects_out_of_range_field(
    field: str, value: int
) -> None:
    """Direct callers get an interface error before any protobuf assignment."""
    node = object.__new__(Node)
    sender = MagicMock(return_value=mesh_pb2.MeshPacket(id=1))
    node._send_admin_op = sender  # type: ignore[method-assign]
    node._raise_interface_error = MagicMock(  # type: ignore[method-assign]
        side_effect=MeshInterfaceError(f"{field} out of range: {value}")
    )

    kwargs = {"event_code": 0, "kb_char": 0, "touch_x": 0, "touch_y": 0}
    kwargs[field] = value

    with pytest.raises(MeshInterfaceError, match=f"{field} out of range"):
        node.sendInputEvent(**kwargs)

    sender.assert_not_called()


@pytest.mark.unit
def test_node_delete_file_accepts_maximum_utf8_path_payload() -> None:
    """Node.deleteFile accepts an absolute path that fills the firmware byte budget."""
    node = object.__new__(Node)
    sender = MagicMock(return_value=mesh_pb2.MeshPacket(id=9))
    node._send_admin_op = sender  # type: ignore[method-assign]
    # MAX_DELETE_FILE_PATH_BYTES = 200 user bytes UTF-8. Build a path whose
    # UTF-8 byte length is exactly 200 by using 199 ASCII characters after the
    # leading slash (1 byte each).
    body = "a" * 199
    path_value = "/" + body
    assert len(path_value.encode("utf-8")) == 200

    node.deleteFile(path_value)

    assert sender.call_args.args[0].delete_file_request == path_value


@pytest.mark.unit
def test_node_delete_file_rejects_oversize_utf8_path_payload() -> None:
    """Node.deleteFile raises before send when the path overflows the firmware bound."""
    node = object.__new__(Node)
    sender = MagicMock(return_value=mesh_pb2.MeshPacket(id=9))
    node._send_admin_op = sender  # type: ignore[method-assign]
    body = "a" * 200
    path_value = "/" + body
    assert len(path_value.encode("utf-8")) == 201

    with pytest.raises(MeshInterfaceError, match="200-byte UTF-8 limit"):
        node.deleteFile(path_value)

    sender.assert_not_called()


@pytest.mark.unit
def test_node_delete_file_rejects_multibyte_path_above_byte_budget() -> None:
    """UTF-8 byte length, not character count, governs the firmware path bound."""
    node = object.__new__(Node)
    sender = MagicMock(return_value=mesh_pb2.MeshPacket(id=9))
    node._send_admin_op = sender  # type: ignore[method-assign]
    # 60 ASCII characters + 60 € characters = 120 characters but
    # 60 + 60 * 3 = 240 UTF-8 bytes, exceeding the 200-byte firmware limit.
    multibyte_segment = "€" * 60
    path_value = "/" + "a" * 60 + multibyte_segment
    assert len(path_value) < 200
    assert len(path_value.encode("utf-8")) > 200

    with pytest.raises(MeshInterfaceError, match="200-byte UTF-8 limit"):
        node.deleteFile(path_value)

    sender.assert_not_called()


@pytest.mark.unit
def test_node_delete_file_rejects_non_utf8_path_payload() -> None:
    """Direct callers get an interface error before encoding an invalid Unicode path."""
    node = object.__new__(Node)
    sender = MagicMock(return_value=mesh_pb2.MeshPacket(id=9))
    node._send_admin_op = sender  # type: ignore[method-assign]
    path_value = "/bad\udcff"

    with pytest.raises(MeshInterfaceError, match="valid UTF-8"):
        node.deleteFile(path_value)

    sender.assert_not_called()
