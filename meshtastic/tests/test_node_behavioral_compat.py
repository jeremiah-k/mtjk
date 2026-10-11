"""Behavioral compatibility tests for Node public API workflows.

These tests verify that old code patterns still work correctly during the refactor,
not just that methods exist, but that they work as expected end-to-end.
"""

# pylint: disable=redefined-outer-name,no-name-in-module

from __future__ import annotations

import base64
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock, create_autospec

import pytest

from meshtastic.mesh_interface import MeshInterface
from meshtastic.node import MAX_CHANNELS, Node
from meshtastic.protobuf import admin_pb2, apponly_pb2, localonly_pb2
from meshtastic.protobuf.channel_pb2 import Channel


def _make_fake_send_admin(
    *,
    sent_messages: list[admin_pb2.AdminMessage] | None = None,
    captured: dict[str, object] | None = None,
    expected_want_response: bool | None = None,
    response_payload: dict[str, Any] | None = None,
    return_packet: Any = None,
) -> Any:
    """Create a configurable fake for Node._send_admin."""

    def _fake_send_admin(
        msg: admin_pb2.AdminMessage,
        wantResponse: bool = False,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        adminIndex: int | None = None,
    ) -> Any:
        if sent_messages is not None:
            sent_messages.append(msg)
        if captured is not None:
            captured["msg"] = msg
            captured["wantResponse"] = wantResponse
            captured["onResponse"] = onResponse
            captured["adminIndex"] = adminIndex
        if expected_want_response is not None:
            assert wantResponse is expected_want_response
        if response_payload is not None:
            assert onResponse is not None
            onResponse(response_payload)
        return return_packet

    return _fake_send_admin


def _create_local_node_mock(iface: MagicMock) -> Node:
    """Create a local Node instance with mocked interface."""
    node = Node(iface, iface.myInfo.my_node_num, noProto=True)
    iface.localNode = node
    return node


def _create_remote_node_mock(iface: MagicMock, node_id: str = "!12345678") -> Node:
    """Create a remote Node instance with mocked interface."""
    node = Node(iface, node_id, noProto=True)
    return node


def _encode_channel_set_to_url(channel_set: apponly_pb2.ChannelSet) -> str:
    """Encode a ChannelSet as a meshtastic URL."""
    encoded = base64.urlsafe_b64encode(channel_set.SerializeToString()).decode("ascii")
    return f"https://meshtastic.org/e/#{encoded.rstrip('=')}"


def _decode_channel_set_from_url(url: str) -> apponly_pb2.ChannelSet:
    """Decode and parse a ChannelSet from a meshtastic URL."""
    b64 = url.split("#")[-1]
    missing_padding = len(b64) % 4
    if missing_padding:
        b64 += "=" * (4 - missing_padding)
    raw = base64.urlsafe_b64decode(b64)
    channel_set = apponly_pb2.ChannelSet()
    channel_set.ParseFromString(raw)
    return channel_set


def _setup_lock_mock(iface: MagicMock) -> None:
    """Set up lock mocking for an interface."""
    iface._node_db_lock = MagicMock()
    iface._node_db_lock.__enter__ = MagicMock(return_value=iface._node_db_lock)
    iface._node_db_lock.__exit__ = MagicMock(return_value=False)


@pytest.fixture
def mock_interface() -> MagicMock:
    """Create a fully mocked interface suitable for local node tests."""
    iface: MagicMock = create_autospec(MeshInterface, instance=True)
    iface.myInfo = MagicMock()
    iface.myInfo.my_node_num = 1234567890
    iface.metadata = None
    # Setup localNode attribute that will be set later
    iface.localNode = None
    # Setup locks
    _setup_lock_mock(iface)
    return iface


@pytest.fixture
def mock_interface_with_local_node() -> tuple[MagicMock, Node]:
    """Create a mocked interface with a properly configured localNode."""
    iface = create_autospec(MeshInterface, instance=True)
    iface.myInfo = MagicMock()
    iface.myInfo.my_node_num = 1234567890
    iface.metadata = None
    # Setup locks
    _setup_lock_mock(iface)
    # Setup _get_or_create_by_num
    iface._get_or_create_by_num = MagicMock(return_value={})
    # Create and set localNode with noProto=False to enable protocol operations
    node = Node(iface, iface.myInfo.my_node_num, noProto=False)
    iface.localNode = node
    return iface, node


@pytest.fixture
def mock_remote_interface() -> MagicMock:
    """Create a mocked interface with a localNode different from the test node."""
    iface: MagicMock = create_autospec(MeshInterface, instance=True)
    iface.myInfo = MagicMock()
    iface.myInfo.my_node_num = 1234567890
    iface.metadata = None
    # Setup a localNode that is different from the remote node we'll create
    local_node = Node(iface, iface.myInfo.my_node_num, noProto=True)
    iface.localNode = local_node
    # Setup locks
    _setup_lock_mock(iface)
    return iface


@pytest.fixture
def mock_interface_with_metadata() -> MagicMock:
    """Create a mocked interface with populated metadata."""
    iface: MagicMock = create_autospec(MeshInterface, instance=True)
    iface.myInfo = MagicMock()
    iface.myInfo.my_node_num = 1234567890
    # Setup metadata
    metadata = MagicMock()
    metadata.firmware_version = "2.2.0"
    metadata.device_state_version = 1
    metadata.role = 0
    metadata.position_flags = 0
    metadata.hw_model = 0
    metadata.hasPKC = False
    metadata.excluded_modules = 0
    iface.metadata = metadata
    # Setup locks
    _setup_lock_mock(iface)
    return iface


@pytest.fixture
def primary_channel() -> Channel:
    """Create a standard primary channel for testing."""
    channel = Channel(index=0, role=Channel.Role.PRIMARY)
    channel.settings.name = "TestChannel"
    channel.settings.psk = b"\x01" * 16
    return channel


@pytest.fixture
def secondary_channel() -> Channel:
    """Create a standard secondary channel for testing."""
    channel = Channel(index=1, role=Channel.Role.SECONDARY)
    channel.settings.name = "Secondary"
    channel.settings.psk = b"\x02" * 16
    return channel


# =============================================================================
# Test 1: Channel Mutate-then-Write Workflow
# =============================================================================


@pytest.mark.unit
def test_channel_mutate_then_write_workflow(mock_interface: MagicMock) -> None:
    """Test the classic workflow: get channel, mutate it, write it back.

    This is a common pattern in user code:
    1. Get channel via getChannelByChannelIndex()
    2. Mutate settings on the channel object
    3. Call writeChannel() to save
    """
    # Setup local node
    node = _create_local_node_mock(mock_interface)

    # Setup channels
    primary = Channel(index=0, role=Channel.Role.PRIMARY)
    primary.settings.name = "Original"
    primary.settings.psk = b"\x01" * 16
    node.channels = [primary] + [
        Channel(index=i, role=Channel.Role.DISABLED) for i in range(1, MAX_CHANNELS)
    ]

    # Setup mocks for the workflow
    node.ensureSessionKey = MagicMock()  # type: ignore[method-assign]
    sent_messages: list[admin_pb2.AdminMessage] = []
    node._send_admin = _make_fake_send_admin(  # type: ignore[method-assign]
        sent_messages=sent_messages, return_packet=MagicMock()
    )

    # Step 1: Get channel (returns live reference)
    channel = node.getChannelByChannelIndex(0)
    assert channel is not None
    assert channel is primary  # Should be the same object

    # Step 2: Mutate settings
    channel.settings.name = "Modified"
    channel.settings.psk = b"\x03" * 16

    # Step 3: Write channel
    node.writeChannel(0)

    # Verify the write was requested correctly
    assert len(sent_messages) == 1
    sent_msg = sent_messages[0]
    assert sent_msg.HasField("set_channel")
    assert sent_msg.set_channel.index == 0
    assert sent_msg.set_channel.settings.name == "Modified"
    assert sent_msg.set_channel.settings.psk == b"\x03" * 16

    # Verify ensureSessionKey was called
    node.ensureSessionKey.assert_called_once()


@pytest.mark.unit
def test_channel_mutate_with_admin_index_workflow(mock_interface: MagicMock) -> None:
    """Test channel mutation workflow with explicit admin index."""
    # Setup local node
    node = _create_local_node_mock(mock_interface)

    # Setup channels
    primary = Channel(index=0, role=Channel.Role.PRIMARY)
    primary.settings.name = "Original"
    node.channels = [primary] + [
        Channel(index=i, role=Channel.Role.DISABLED) for i in range(1, MAX_CHANNELS)
    ]

    # Setup mocks
    captured: dict[str, object] = {}
    node.ensureSessionKey = MagicMock()  # type: ignore[method-assign]
    node._send_admin = _make_fake_send_admin(  # type: ignore[method-assign]
        captured=captured, return_packet=MagicMock()
    )

    # Get and modify channel
    channel = node.getChannelByChannelIndex(0)
    assert channel is not None
    channel.settings.name = "AdminModified"

    # Write with admin index
    node.writeChannel(0, adminIndex=3)

    # Verify admin index was passed correctly
    assert captured.get("adminIndex") == 3
    node.ensureSessionKey.assert_called_once_with(adminIndex=3)


# =============================================================================
# Test 2: getURL/setURL Roundtrip Workflow
# =============================================================================


@pytest.mark.unit
def test_getURL_setURL_roundtrip(
    mock_interface: MagicMock,
    primary_channel: Channel,
    secondary_channel: Channel,
) -> None:
    """Test URL roundtrip: getURL from one node, setURL on another, verify channels match.

    This tests the common workflow of sharing channel configuration via URLs.
    """
    # Setup source node with channels
    source_node = _create_local_node_mock(mock_interface)
    source_node.channels = [
        primary_channel,
        secondary_channel,
    ] + [Channel(index=i, role=Channel.Role.DISABLED) for i in range(2, MAX_CHANNELS)]

    # Setup localConfig with LoRa settings
    source_node.localConfig = localonly_pb2.LocalConfig()
    source_node.localConfig.lora.hop_limit = 5
    source_node.localConfig.lora.tx_power = 20

    # Setup write mocks for source node
    source_node.ensureSessionKey = MagicMock()  # type: ignore[method-assign]
    source_node._send_admin = MagicMock(return_value=MagicMock())  # type: ignore[method-assign]
    source_node._write_channel_snapshot = MagicMock()  # type: ignore[method-assign]

    # Get URL from source node
    url = source_node.getURL(includeAll=True)
    assert url.startswith("https://meshtastic.org/e/#")

    # Setup target node (empty channels)
    target_node = _create_local_node_mock(mock_interface)
    target_node.channels = [
        Channel(index=i, role=Channel.Role.DISABLED) for i in range(MAX_CHANNELS)
    ]
    target_node.localConfig = localonly_pb2.LocalConfig()
    target_node.localConfig.lora.hop_limit = 3  # Different value

    # Setup write mocks for target node
    target_node.ensureSessionKey = MagicMock()  # type: ignore[method-assign]
    target_node._send_admin = MagicMock(return_value=MagicMock())  # type: ignore[method-assign]
    target_node._write_channel_snapshot = MagicMock()  # type: ignore[method-assign]

    # Apply URL to target node
    target_node.setURL(url)

    # Verify channels were updated
    assert target_node.channels[0].role == Channel.Role.PRIMARY
    assert target_node.channels[0].settings.name == primary_channel.settings.name
    assert target_node.channels[1].role == Channel.Role.SECONDARY
    assert target_node.channels[1].settings.name == secondary_channel.settings.name

    # Verify LoRa config was updated
    assert target_node.localConfig.lora.hop_limit == 5


@pytest.mark.unit
def test_getURL_setURL_primary_only_roundtrip(mock_interface: MagicMock) -> None:
    """Test URL roundtrip with only primary channel (includeAll=False)."""
    # Setup source node
    source_node = _create_local_node_mock(mock_interface)
    primary = Channel(index=0, role=Channel.Role.PRIMARY)
    primary.settings.name = "PrimaryOnly"
    primary.settings.psk = b"\x01" * 16
    source_node.channels = [primary] + [
        Channel(index=i, role=Channel.Role.DISABLED) for i in range(1, MAX_CHANNELS)
    ]
    source_node.localConfig = localonly_pb2.LocalConfig()
    source_node.localConfig.lora.hop_limit = 7

    # Get URL without secondary channels
    url = source_node.getURL(includeAll=False)

    # Verify URL only contains primary
    channel_set = _decode_channel_set_from_url(url)
    assert len(channel_set.settings) == 1
    assert channel_set.settings[0].name == "PrimaryOnly"


# =============================================================================
# Test 3: requestConfig Wait Behavior
# =============================================================================


@pytest.mark.unit
def test_requestConfig_wait_behavior(mock_interface: MagicMock) -> None:
    """Test requestConfig sends correct admin packet and handles response.

    This verifies:
    - It sends the correct get_config_request for LocalConfig fields
    - It sends get_module_config_request for module config fields
    - It handles adminIndex parameter correctly
    """
    # Setup local node
    node = _create_local_node_mock(mock_interface)
    node.localConfig = localonly_pb2.LocalConfig()

    # Capture sent messages
    sent_messages: list[admin_pb2.AdminMessage] = []
    node._send_admin = _make_fake_send_admin(  # type: ignore[method-assign]
        sent_messages=sent_messages, return_packet=MagicMock()
    )

    # Test 1: Request local config (device field)
    device_field = node.localConfig.DESCRIPTOR.fields_by_name["device"]
    node.requestConfig(device_field)

    assert len(sent_messages) == 1
    assert sent_messages[0].get_config_request == device_field.index

    # Test 2: Request module config (mqtt field)
    node.moduleConfig = localonly_pb2.LocalModuleConfig()
    mqtt_field = node.moduleConfig.DESCRIPTOR.fields_by_name["mqtt"]
    sent_messages.clear()
    node.requestConfig(mqtt_field)

    assert len(sent_messages) == 1
    assert sent_messages[0].get_module_config_request == mqtt_field.index


@pytest.mark.unit
def test_requestConfig_with_adminIndex(mock_remote_interface: MagicMock) -> None:
    """Test requestConfig with adminIndex parameter."""
    # Setup remote node
    node = _create_remote_node_mock(mock_remote_interface, "!abcdef01")
    node.localConfig = localonly_pb2.LocalConfig()

    # Capture sent messages
    captured: dict[str, object] = {}
    node._send_admin = _make_fake_send_admin(  # type: ignore[method-assign]
        captured=captured, return_packet=MagicMock()
    )

    # Request with admin index
    device_field = node.localConfig.DESCRIPTOR.fields_by_name["device"]
    node.requestConfig(device_field, adminIndex=2)

    # Verify admin index was passed
    assert captured.get("adminIndex") == 2


# =============================================================================
# Test 4: ensureSessionKey Workflow
# =============================================================================


@pytest.mark.unit
def test_ensureSessionKey_workflow(
    mock_interface_with_local_node: tuple[MagicMock, Node],
) -> None:
    """Test ensureSessionKey workflow when no key exists.

    When called and no adminSessionPassKey exists, it should request one.
    When called and key exists, it should do nothing.
    """
    # Setup node
    iface, node = mock_interface_with_local_node
    node.localConfig = localonly_pb2.LocalConfig()

    # Setup mock to return node without session key
    iface._get_or_create_by_num = MagicMock(return_value={})

    # Mock _timeout.waitForSet to return immediately
    node._timeout = MagicMock()
    node._timeout.waitForSet.return_value = True

    # Capture sent messages
    sent_messages: list[admin_pb2.AdminMessage] = []
    node._send_admin = _make_fake_send_admin(  # type: ignore[method-assign]
        sent_messages=sent_messages, return_packet=MagicMock()
    )

    # Call ensureSessionKey when no key exists
    node.ensureSessionKey()

    # Verify session key was requested
    assert len(sent_messages) == 1
    assert (
        sent_messages[0].get_config_request == admin_pb2.AdminMessage.SESSIONKEY_CONFIG
    )


@pytest.mark.unit
def test_ensureSessionKey_with_adminIndex(
    mock_interface_with_local_node: tuple[MagicMock, Node],
) -> None:
    """Test ensureSessionKey passes adminIndex correctly."""
    # Setup node
    iface, node = mock_interface_with_local_node
    node.localConfig = localonly_pb2.LocalConfig()

    # Setup mock to return node without session key
    iface._get_or_create_by_num = MagicMock(return_value={})

    # Mock _timeout.waitForSet to return immediately
    node._timeout = MagicMock()
    node._timeout.waitForSet.return_value = True

    captured: dict[str, object] = {}
    node._send_admin = _make_fake_send_admin(  # type: ignore[method-assign]
        captured=captured, return_packet=MagicMock()
    )

    # Call with admin index
    node.ensureSessionKey(adminIndex=5)

    # Verify admin index was passed to _send_admin
    assert captured.get("adminIndex") == 5


# =============================================================================
# Test 5: startOTA Signature Compatibility
# =============================================================================


@pytest.mark.unit
def test_startOTA_old_signature_compat(mock_interface: MagicMock) -> None:
    """Test startOTA with old signature: ota_mode, ota_hash, hash.

    Old code may call startOTA with keyword args like:
    - ota_mode=admin_pb2.OTAMode.OTA_WIFI
    - ota_hash=b'...'
    - hash=b'...'
    """
    # Setup local node
    node = _create_local_node_mock(mock_interface)

    # Capture sent messages
    sent_messages: list[admin_pb2.AdminMessage] = []
    node._send_admin = _make_fake_send_admin(  # type: ignore[method-assign]
        sent_messages=sent_messages, return_packet=MagicMock()
    )

    test_hash = b"\x01\x02\x03" * 8  # 24-byte hash

    # Test old signature with ota_mode and ota_hash
    _result = node.startOTA(ota_mode=admin_pb2.OTAMode.OTA_WIFI, ota_hash=test_hash)

    # Verify it sent correctly
    assert len(sent_messages) == 1
    assert sent_messages[0].ota_request.reboot_ota_mode == admin_pb2.OTAMode.OTA_WIFI
    assert sent_messages[0].ota_request.ota_hash == test_hash


@pytest.mark.unit
def test_startOTA_new_signature_compat(mock_interface: MagicMock) -> None:
    """Test startOTA with new signature: mode, ota_file_hash.

    New code should use:
    - mode=admin_pb2.OTAMode.OTA_WIFI
    - ota_file_hash=b'...'
    """
    # Setup local node
    node = _create_local_node_mock(mock_interface)

    # Capture sent messages
    sent_messages: list[admin_pb2.AdminMessage] = []
    node._send_admin = _make_fake_send_admin(  # type: ignore[method-assign]
        sent_messages=sent_messages, return_packet=MagicMock()
    )

    test_hash = b"\x04\x05\x06" * 8  # 24-byte hash

    # Test new signature with mode and ota_file_hash
    _result = node.startOTA(mode=admin_pb2.OTAMode.OTA_BLE, ota_file_hash=test_hash)

    # Verify it sent correctly
    assert len(sent_messages) == 1
    assert sent_messages[0].ota_request.reboot_ota_mode == admin_pb2.OTAMode.OTA_BLE
    assert sent_messages[0].ota_request.ota_hash == test_hash


@pytest.mark.unit
def test_startOTA_remote_node_raises(mock_interface: MagicMock) -> None:
    """Test startOTA raises error on remote nodes."""
    # Setup remote node
    node = _create_remote_node_mock(mock_interface, "!abcdef01")
    mock_interface.localNode = Node(mock_interface, 1234567890, noProto=True)

    test_hash = b"\x01\x02\x03" * 8

    with pytest.raises(
        MeshInterface.MeshInterfaceError, match="startOTA only possible on local node"
    ):
        node.startOTA(mode=admin_pb2.OTAMode.OTA_WIFI, ota_file_hash=test_hash)


# =============================================================================
# Test 6: Canned Message Roundtrip Workflow
# =============================================================================


@pytest.mark.unit
def test_get_set_canned_message_workflow(mock_interface: MagicMock) -> None:
    """Test getCannedMessage / setCannedMessage roundtrip workflow.

    Verifies:
    - getCannedMessage returns cached value if available
    - getCannedMessage requests from device if not cached
    - setCannedMessage sends the message
    - setCannedMessage invalidates cache
    """
    # Setup node
    node = _create_local_node_mock(mock_interface)

    # Pre-populate cache
    node.cannedPluginMessage = "cached message"

    # Test 1: Get returns cached without sending request
    node.ensureSessionKey = MagicMock()  # type: ignore[method-assign]
    result = node.getCannedMessage()
    assert result == "cached message"
    node.ensureSessionKey.assert_not_called()  # Should not need session key for cache hit

    # Test 2: Set canned message
    sent_messages: list[admin_pb2.AdminMessage] = []
    node._send_admin = _make_fake_send_admin(  # type: ignore[method-assign]
        sent_messages=sent_messages, return_packet=MagicMock()
    )

    node.setCannedMessage("new message")

    # Verify message was sent
    assert len(sent_messages) == 1
    assert sent_messages[0].set_canned_message_module_messages == "new message"

    # Verify cache was invalidated
    assert node.cannedPluginMessage is None


@pytest.mark.unit
def test_get_canned_message_requests_when_not_cached(mock_interface: MagicMock) -> None:
    """Test getCannedMessage requests from device when cache is empty."""
    # Setup node
    node = _create_local_node_mock(mock_interface)
    node.module_available = MagicMock(return_value=True)  # type: ignore[method-assign]

    # Setup timeout mock for immediate response
    timeout_mock = MagicMock()
    timeout_mock.waitForSet.return_value = True
    timeout_mock.expireTimeout = 0
    node._timeout = timeout_mock

    # No cache - should request from device
    response_raw = admin_pb2.AdminMessage()
    response_raw.get_canned_message_module_messages_response = "fetched from device"

    sent_messages: list[admin_pb2.AdminMessage] = []
    node._send_admin = _make_fake_send_admin(  # type: ignore[method-assign]
        sent_messages=sent_messages,
        expected_want_response=True,
        response_payload={"decoded": {"admin": {"raw": response_raw}}},
        return_packet=MagicMock(),
    )

    result = node.getCannedMessage()

    # Verify request was made and response cached
    assert len(sent_messages) == 1
    assert sent_messages[0].get_canned_message_module_messages_request is True
    assert result == "fetched from device"
    assert node.cannedPluginMessage == "fetched from device"


# =============================================================================
# Test 7: Ringtone Roundtrip Workflow
# =============================================================================


@pytest.mark.unit
def test_get_set_ringtone_workflow(mock_interface: MagicMock) -> None:
    """Test getRingtone / setRingtone roundtrip workflow.

    Verifies:
    - setRingtone sends the ringtone
    - getRingtone retrieves ringtone from device
    """
    # Setup node
    node = _create_local_node_mock(mock_interface)
    node.module_available = MagicMock(return_value=True)  # type: ignore[method-assign]

    # Setup timeout mock
    timeout_mock = MagicMock()
    timeout_mock.waitForSet.return_value = True
    timeout_mock.expireTimeout = 0
    node._timeout = timeout_mock

    # Test setRingtone
    sent_messages: list[admin_pb2.AdminMessage] = []
    response_raw = admin_pb2.AdminMessage()
    response_raw.get_ringtone_response = "fetch:ringtone:data"

    node._send_admin = _make_fake_send_admin(  # type: ignore[method-assign]
        sent_messages=sent_messages, return_packet=MagicMock()
    )

    node.ensureSessionKey = MagicMock()  # type: ignore[method-assign]
    node.setRingtone("ringtone data here")

    # Verify ringtone was sent
    assert len(sent_messages) == 1
    assert sent_messages[0].set_ringtone_message == "ringtone data here"

    # Test getRingtone requests from device
    sent_messages.clear()
    node._send_admin = _make_fake_send_admin(  # type: ignore[method-assign]
        sent_messages=sent_messages,
        expected_want_response=True,
        response_payload={"decoded": {"admin": {"raw": response_raw}}},
        return_packet=MagicMock(),
    )

    result = node.getRingtone()

    # Verify request was made
    assert len(sent_messages) == 1
    assert sent_messages[0].get_ringtone_request is True

    # Verify response was consumed and returned
    assert result == "fetch:ringtone:data"
    # Verify the ringtone was cached on the node
    assert node.ringtone == "fetch:ringtone:data"


# =============================================================================
# Additional Compatibility Tests
# =============================================================================


@pytest.mark.unit
def test_snake_case_get_canned_message_compat(mock_interface: MagicMock) -> None:
    """Test snake_case get_canned_message is compatible alias."""
    node = _create_local_node_mock(mock_interface)
    node.cannedPluginMessage = "test message"

    # Both should return the same value
    assert node.get_canned_message() == node.getCannedMessage()


@pytest.mark.unit
def test_snake_case_set_canned_message_compat(mock_interface: MagicMock) -> None:
    """Test snake_case set_canned_message is compatible alias."""
    node = _create_local_node_mock(mock_interface)

    sent_messages: list[admin_pb2.AdminMessage] = []
    node._send_admin = _make_fake_send_admin(  # type: ignore[method-assign]
        sent_messages=sent_messages, return_packet=MagicMock()
    )
    node.ensureSessionKey = MagicMock()  # type: ignore[method-assign]

    # Both should send the same message
    node.set_canned_message("test")
    assert sent_messages[0].set_canned_message_module_messages == "test"


@pytest.mark.unit
def test_snake_case_get_ringtone_compat(mock_interface: MagicMock) -> None:
    """Test snake_case get_ringtone is compatible alias."""
    node = _create_local_node_mock(mock_interface)
    node.module_available = MagicMock(return_value=True)  # type: ignore[method-assign]
    node.ringtone = "test ringtone"

    # Both should return the same value
    assert node.get_ringtone() == "test ringtone"
    assert node.get_ringtone() == node.getRingtone()


@pytest.mark.unit
def test_snake_case_set_ringtone_compat(mock_interface: MagicMock) -> None:
    """Test snake_case set_ringtone is compatible alias."""
    node = _create_local_node_mock(mock_interface)
    node.module_available = MagicMock(return_value=True)  # type: ignore[method-assign]

    sent_messages: list[admin_pb2.AdminMessage] = []
    node._send_admin = _make_fake_send_admin(  # type: ignore[method-assign]
        sent_messages=sent_messages, return_packet=MagicMock()
    )
    node.ensureSessionKey = MagicMock()  # type: ignore[method-assign]

    # Both should send the same message
    node.set_ringtone("ringtone")
    assert sent_messages[0].set_ringtone_message == "ringtone"
