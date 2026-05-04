"""Meshtastic unit tests for mesh_interface_runtime/receive_pipeline.py."""

# pylint: disable=redefined-outer-name,protected-access

import logging
import threading
from unittest.mock import MagicMock, patch

import pytest

from meshtastic.mesh_interface import MeshInterface
from meshtastic.mesh_interface_runtime.receive_pipeline import (
    DECODE_FAILED_PREFIX,
    LOCAL_CONFIG_FROM_RADIO_FIELDS,
    MODULE_CONFIG_FROM_RADIO_FIELDS,
    ReceivePipeline,
    _FromRadioContext,
    _LazyMessageDict,
    _PacketRuntimeContext,
    _PublicationIntent,
)
from meshtastic.protobuf import (
    channel_pb2,
    config_pb2,
    mesh_pb2,
    module_config_pb2,
    portnums_pb2,
)


@pytest.fixture
def mock_interface() -> MagicMock:
    """Create a minimal mock MeshInterface for receive pipeline tests."""
    interface = MagicMock()
    interface._node_db_lock = threading.RLock()
    interface._request_wait_runtime = MagicMock()
    interface._queue_send_runtime = MagicMock()
    interface.configId = 123
    interface.localNode = MagicMock()
    interface.localNode.localConfig = config_pb2.Config()
    interface.localNode.moduleConfig = module_config_pb2.ModuleConfig()
    interface.myInfo = None
    interface.metadata = None
    interface.nodes = {}
    interface.nodesByNum = {}
    interface._localChannels = []
    interface.MeshInterfaceError = MeshInterface.MeshInterfaceError
    return interface


@pytest.fixture
def receive_pipeline(mock_interface: MagicMock) -> ReceivePipeline:
    """Create a ReceivePipeline instance with mocked interface."""
    return ReceivePipeline(mock_interface)


class TestModuleLevelConstants:
    """Tests for module-level constants (line 26 and related)."""

    @pytest.mark.unit
    def test_from_radio_branches_defined(self) -> None:
        """Test that _FROM_RADIO_BRANCHES constant is defined with correct structure."""
        from meshtastic.mesh_interface_runtime.receive_pipeline import (  # pylint: disable=import-outside-toplevel
            _FROM_RADIO_BRANCHES,
        )

        assert isinstance(_FROM_RADIO_BRANCHES, tuple)
        assert len(_FROM_RADIO_BRANCHES) > 0

        # Each branch should be a tuple of (predicate, branch_name)
        for branch in _FROM_RADIO_BRANCHES:
            assert isinstance(branch, tuple)
            assert len(branch) == 2
            predicate, branch_name = branch
            assert callable(predicate)
            assert isinstance(branch_name, str)

    @pytest.mark.unit
    def test_local_config_fields_defined(self) -> None:
        """Test that LOCAL_CONFIG_FROM_RADIO_FIELDS is defined."""
        assert isinstance(LOCAL_CONFIG_FROM_RADIO_FIELDS, tuple)
        assert len(LOCAL_CONFIG_FROM_RADIO_FIELDS) > 0
        assert "device" in LOCAL_CONFIG_FROM_RADIO_FIELDS
        assert "lora" in LOCAL_CONFIG_FROM_RADIO_FIELDS

    @pytest.mark.unit
    def test_module_config_fields_defined(self) -> None:
        """Test that MODULE_CONFIG_FROM_RADIO_FIELDS is defined."""
        assert isinstance(MODULE_CONFIG_FROM_RADIO_FIELDS, tuple)
        assert len(MODULE_CONFIG_FROM_RADIO_FIELDS) > 0
        assert "mqtt" in MODULE_CONFIG_FROM_RADIO_FIELDS

    @pytest.mark.unit
    def test_decode_failed_prefix(self) -> None:
        """Test DECODE_FAILED_PREFIX constant."""
        assert DECODE_FAILED_PREFIX == "decode-failed: "


class TestMeshInterfaceError:
    """Tests for MeshInterfaceError class (lines 164-169)."""

    @pytest.mark.unit
    def test_error_message_stored(self) -> None:
        """Test that error message is stored in the exception."""
        error = MeshInterface.MeshInterfaceError("Test error message")
        assert error.message == "Test error message"
        assert str(error) == "Test error message"

    @pytest.mark.unit
    def test_error_can_be_raised(self) -> None:
        """Test that the error can be raised and caught."""
        with pytest.raises(MeshInterface.MeshInterfaceError, match="Test error"):
            raise MeshInterface.MeshInterfaceError("Test error")


class TestPublicationIntent:
    """Tests for _PublicationIntent dataclass (lines 97-102)."""

    @pytest.mark.unit
    def test_publication_intent_creation(self) -> None:
        """Test creating a _PublicationIntent."""
        intent = _PublicationIntent(topic="test.topic", payload={"key": "value"})
        assert intent.topic == "test.topic"
        assert intent.payload == {"key": "value"}


class TestLazyMessageDict:
    """Tests for _LazyMessageDict dataclass (lines 105-120)."""

    @pytest.mark.unit
    def test_lazy_message_dict_caches_result(self) -> None:
        """Test that _LazyMessageDict caches the converted dict."""
        message = mesh_pb2.MyNodeInfo()
        message.my_node_num = 12345

        lazy_dict = _LazyMessageDict(message=message)

        # First call should compute
        result1 = lazy_dict.get()
        assert result1["myNodeNum"] == 12345

        # Second call should return cached value
        result2 = lazy_dict.get()
        assert result2 is result1  # Same object (cached)


class TestFromRadioContext:
    """Tests for _FromRadioContext dataclass (lines 123-130)."""

    @pytest.mark.unit
    def test_context_creation(self) -> None:
        """Test creating a _FromRadioContext."""
        message = mesh_pb2.FromRadio()
        message.id = 123
        lazy_dict = _LazyMessageDict(message=message)

        context = _FromRadioContext(
            message=message, message_dict=lazy_dict, config_id=456
        )

        assert context.message is message
        assert context.message_dict is lazy_dict
        assert context.config_id == 456


class TestPacketRuntimeContext:
    """Tests for _PacketRuntimeContext dataclass (lines 132-140)."""

    @pytest.mark.unit
    def test_packet_runtime_context_defaults(self) -> None:
        """Test _PacketRuntimeContext default values."""
        packet_dict = {"key": "value"}
        context = _PacketRuntimeContext(packet_dict=packet_dict)

        assert context.packet_dict is packet_dict
        assert context.topic == "meshtastic.receive"
        assert context.decoded is None
        assert context.skip_response_callback_for_decode_failure is False
        assert context.on_receive_callback is None


class TestReceivePipelineInit:
    """Tests for ReceivePipeline initialization (lines 172-191)."""

    @pytest.mark.unit
    def test_receive_pipeline_init(self, mock_interface: MagicMock) -> None:
        """Test ReceivePipeline initialization."""
        pipeline = ReceivePipeline(mock_interface)

        assert pipeline._interface is mock_interface
        assert pipeline._from_radio_dispatch_map_cache is None


class TestReceivePipelineProperties:
    """Tests for ReceivePipeline properties (lines 192-236)."""

    @pytest.mark.unit
    def test_node_db_lock_property(
        self, receive_pipeline: ReceivePipeline, mock_interface: MagicMock
    ) -> None:
        """Test _node_db_lock property returns interface lock."""
        assert receive_pipeline._node_db_lock is mock_interface._node_db_lock

    @pytest.mark.unit
    def test_request_wait_runtime_property(
        self, receive_pipeline: ReceivePipeline, mock_interface: MagicMock
    ) -> None:
        """Test _request_wait_runtime property."""
        assert (
            receive_pipeline._request_wait_runtime
            is mock_interface._request_wait_runtime
        )

    @pytest.mark.unit
    def test_queue_send_runtime_property(
        self, receive_pipeline: ReceivePipeline, mock_interface: MagicMock
    ) -> None:
        """Test _queue_send_runtime property."""
        assert (
            receive_pipeline._queue_send_runtime is mock_interface._queue_send_runtime
        )

    @pytest.mark.unit
    def test_config_id_property(
        self, receive_pipeline: ReceivePipeline, mock_interface: MagicMock
    ) -> None:
        """Test configId property."""
        assert receive_pipeline.configId == 123
        mock_interface.configId = 456
        assert receive_pipeline.configId == 456

    @pytest.mark.unit
    def test_local_node_property(
        self, receive_pipeline: ReceivePipeline, mock_interface: MagicMock
    ) -> None:
        """Test localNode property."""
        assert receive_pipeline.localNode is mock_interface.localNode

    @pytest.mark.unit
    def test_my_info_property(
        self, receive_pipeline: ReceivePipeline, mock_interface: MagicMock
    ) -> None:
        """Test myInfo property."""
        my_info = mesh_pb2.MyNodeInfo()
        mock_interface.myInfo = my_info
        assert receive_pipeline.myInfo is my_info

    @pytest.mark.unit
    def test_metadata_property(
        self, receive_pipeline: ReceivePipeline, mock_interface: MagicMock
    ) -> None:
        """Test metadata property."""
        metadata = mesh_pb2.DeviceMetadata()
        mock_interface.metadata = metadata
        assert receive_pipeline.metadata is metadata

    @pytest.mark.unit
    def test_nodes_property(
        self, receive_pipeline: ReceivePipeline, mock_interface: MagicMock
    ) -> None:
        """Test nodes property."""
        nodes = {"!1234": {"num": 1234}}
        mock_interface.nodes = nodes
        assert receive_pipeline.nodes is nodes

    @pytest.mark.unit
    def test_nodes_by_num_property(
        self, receive_pipeline: ReceivePipeline, mock_interface: MagicMock
    ) -> None:
        """Test nodesByNum property."""
        nodes_by_num = {1234: {"num": 1234}}
        mock_interface.nodesByNum = nodes_by_num
        assert receive_pipeline.nodesByNum is nodes_by_num


class TestParseFromRadioBytes:
    """Tests for _parse_from_radio_bytes method (lines 244-263)."""

    @pytest.mark.unit
    def test_parse_valid_from_radio(self, receive_pipeline: ReceivePipeline) -> None:
        """Test parsing valid FromRadio bytes."""
        from_radio = mesh_pb2.FromRadio()
        from_radio.id = 12345
        from_radio.my_info.my_node_num = 67890

        serialized = from_radio.SerializeToString()
        result = receive_pipeline._parse_from_radio_bytes(serialized)

        assert result.id == 12345
        assert result.my_info.my_node_num == 67890

    @pytest.mark.unit
    def test_parse_invalid_from_radio_logs_error(
        self, receive_pipeline: ReceivePipeline, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that parsing invalid bytes logs an error and raises."""
        invalid_bytes = b"invalid protobuf data"

        from google.protobuf.message import (  # pylint: disable=import-outside-toplevel
            DecodeError,
        )

        with caplog.at_level(logging.ERROR):
            with pytest.raises(DecodeError):
                receive_pipeline._parse_from_radio_bytes(invalid_bytes)

        assert "Error while parsing FromRadio" in caplog.text


class TestNormalizeFromRadioMessage:
    """Tests for _normalize_from_radio_message method (lines 265-276)."""

    @pytest.mark.unit
    def test_normalize_from_radio(self, receive_pipeline: ReceivePipeline) -> None:
        """Test normalizing FromRadio message."""
        from_radio = mesh_pb2.FromRadio()
        from_radio.id = 12345

        result = receive_pipeline._normalize_from_radio_message(from_radio)

        assert isinstance(result, _FromRadioContext)
        assert result.message is from_radio
        assert result.config_id == 123  # From mock_interface.configId
        assert isinstance(result.message_dict, _LazyMessageDict)


class TestSelectFromRadioBranch:
    """Tests for _select_from_radio_branch method (lines 289-295)."""

    @pytest.mark.unit
    def test_select_my_info_branch(self, receive_pipeline: ReceivePipeline) -> None:
        """Test selecting my_info branch."""
        from_radio = mesh_pb2.FromRadio()
        from_radio.my_info.my_node_num = 12345

        context = _FromRadioContext(
            message=from_radio,
            message_dict=_LazyMessageDict(from_radio),
            config_id=None,
        )

        branch = receive_pipeline._select_from_radio_branch(context)
        assert branch == "my_info"

    @pytest.mark.unit
    def test_select_packet_branch(self, receive_pipeline: ReceivePipeline) -> None:
        """Test selecting packet branch."""
        from_radio = mesh_pb2.FromRadio()
        from_radio.packet.id = 12345

        context = _FromRadioContext(
            message=from_radio,
            message_dict=_LazyMessageDict(from_radio),
            config_id=None,
        )

        branch = receive_pipeline._select_from_radio_branch(context)
        assert branch == "packet"

    @pytest.mark.unit
    def test_select_unknown_branch_returns_none(
        self, receive_pipeline: ReceivePipeline
    ) -> None:
        """Test that unknown FromRadio returns None."""
        from_radio = mesh_pb2.FromRadio()
        from_radio.id = 12345  # Only has id, no specific field

        context = _FromRadioContext(
            message=from_radio,
            message_dict=_LazyMessageDict(from_radio),
            config_id=None,
        )

        branch = receive_pipeline._select_from_radio_branch(context)
        assert branch is None


class TestFromRadioDispatchMap:
    """Tests for _from_radio_dispatch_map method (lines 297-317)."""

    @pytest.mark.unit
    def test_dispatch_map_caching(self, receive_pipeline: ReceivePipeline) -> None:
        """Test that dispatch map is cached."""
        map1 = receive_pipeline._from_radio_dispatch_map()
        map2 = receive_pipeline._from_radio_dispatch_map()

        assert map1 is map2  # Same cached object

    @pytest.mark.unit
    def test_dispatch_map_has_all_branches(
        self, receive_pipeline: ReceivePipeline
    ) -> None:
        """Test that dispatch map contains all expected branches."""
        dispatch_map = receive_pipeline._from_radio_dispatch_map()

        expected_branches = [
            "my_info",
            "metadata",
            "node_info",
            "config_complete_id",
            "channel",
            "packet",
            "log_record",
            "queueStatus",
            "clientNotification",
            "mqttClientProxyMessage",
            "xmodemPacket",
            "rebooted",
            "config_or_moduleConfig",
        ]

        for branch in expected_branches:
            assert branch in dispatch_map
            assert callable(dispatch_map[branch])


class TestHandleFromRadioMyInfo:
    """Tests for _handle_from_radio_my_info method (lines 319-330)."""

    @pytest.mark.unit
    def test_handle_my_info(
        self, receive_pipeline: ReceivePipeline, mock_interface: MagicMock
    ) -> None:
        """Test handling my_info FromRadio message."""
        from_radio = mesh_pb2.FromRadio()
        from_radio.my_info.my_node_num = 12345

        context = _FromRadioContext(
            message=from_radio,
            message_dict=_LazyMessageDict(from_radio),
            config_id=None,
        )

        result = receive_pipeline._handle_from_radio_my_info(context)

        assert result == []  # No publication intents
        assert mock_interface.myInfo.my_node_num == 12345
        assert mock_interface.localNode.nodeNum == 12345


class TestHandleFromRadioMetadata:
    """Tests for _handle_from_radio_metadata method (lines 332-342)."""

    @pytest.mark.unit
    def test_handle_metadata(
        self, receive_pipeline: ReceivePipeline, mock_interface: MagicMock
    ) -> None:
        """Test handling metadata FromRadio message."""
        from_radio = mesh_pb2.FromRadio()
        from_radio.metadata.firmware_version = "2.0.0"

        context = _FromRadioContext(
            message=from_radio,
            message_dict=_LazyMessageDict(from_radio),
            config_id=None,
        )

        result = receive_pipeline._handle_from_radio_metadata(context)

        assert result == []  # No publication intents
        assert mock_interface.metadata.firmware_version == "2.0.0"


class TestHandleFromRadioNodeInfo:
    """Tests for _handle_from_radio_node_info method (lines 344-365)."""

    @pytest.mark.unit
    def test_handle_node_info_new_node(
        self, receive_pipeline: ReceivePipeline, mock_interface: MagicMock
    ) -> None:
        """Test handling node_info for a new node."""
        from_radio = mesh_pb2.FromRadio()
        from_radio.node_info.num = 12345
        from_radio.node_info.user.id = "!1234"
        from_radio.node_info.user.long_name = "Test Node"

        mock_interface.nodesByNum = {}

        context = _FromRadioContext(
            message=from_radio,
            message_dict=_LazyMessageDict(from_radio),
            config_id=None,
        )

        with patch(
            "meshtastic.mesh_interface_runtime.receive_pipeline.publishingThread"
        ):
            result = receive_pipeline._handle_from_radio_node_info(context)

        assert len(result) == 1
        assert result[0].topic == "meshtastic.node.updated"
        assert 12345 in mock_interface.nodesByNum


class TestHandleFromRadioConfigCompleteId:
    """Tests for _handle_from_radio_config_complete_id method (lines 367-373)."""

    @pytest.mark.unit
    def test_handle_config_complete_id(self, receive_pipeline: ReceivePipeline) -> None:
        """Test handling config_complete_id FromRadio message."""
        from_radio = mesh_pb2.FromRadio()
        from_radio.config_complete_id = 123

        context = _FromRadioContext(
            message=from_radio, message_dict=_LazyMessageDict(from_radio), config_id=123
        )

        result = receive_pipeline._handle_from_radio_config_complete_id(context)

        assert result == []  # No publication intents


class TestHandleFromRadioChannel:
    """Tests for _handle_from_radio_channel method (lines 375-380)."""

    @pytest.mark.unit
    def test_handle_channel(
        self, receive_pipeline: ReceivePipeline, mock_interface: MagicMock
    ) -> None:
        """Test handling channel FromRadio message."""
        from_radio = mesh_pb2.FromRadio()
        from_radio.channel.index = 0
        from_radio.channel.settings.channel_num = 1

        context = _FromRadioContext(
            message=from_radio,
            message_dict=_LazyMessageDict(from_radio),
            config_id=None,
        )

        result = receive_pipeline._handle_from_radio_channel(context)

        assert result == []  # No publication intents
        assert len(mock_interface._localChannels) == 1


class TestHandleFromRadioPacket:
    """Tests for _handle_from_radio_packet method (lines 382-389)."""

    @pytest.mark.unit
    def test_handle_packet(self, receive_pipeline: ReceivePipeline) -> None:
        """Test handling packet FromRadio message."""
        from_radio = mesh_pb2.FromRadio()
        from_radio.packet.id = 12345
        # Use setattr for 'from' field since it's a reserved keyword
        setattr(from_radio.packet, "from", 67890)

        context = _FromRadioContext(
            message=from_radio,
            message_dict=_LazyMessageDict(from_radio),
            config_id=None,
        )

        with patch.object(receive_pipeline, "_handle_packet_from_radio") as mock_handle:
            mock_handle.return_value = []
            result = receive_pipeline._handle_from_radio_packet(context)

        assert result == []
        mock_handle.assert_called_once()


class TestHandleFromRadioLogRecord:
    """Tests for _handle_from_radio_log_record method (lines 391-396)."""

    @pytest.mark.unit
    def test_handle_log_record(
        self, receive_pipeline: ReceivePipeline, mock_interface: MagicMock
    ) -> None:
        """Test handling log_record FromRadio message."""
        from_radio = mesh_pb2.FromRadio()
        from_radio.log_record.message = "Test log message"

        context = _FromRadioContext(
            message=from_radio,
            message_dict=_LazyMessageDict(from_radio),
            config_id=None,
        )

        result = receive_pipeline._handle_from_radio_log_record(context)

        assert result == []  # No publication intents
        mock_interface._handle_log_line.assert_called_once_with("Test log message")


class TestHandleFromRadioQueueStatus:
    """Tests for _handle_from_radio_queue_status method (lines 398-403)."""

    @pytest.mark.unit
    def test_handle_queue_status(
        self, receive_pipeline: ReceivePipeline, mock_interface: MagicMock
    ) -> None:
        """Test handling queueStatus FromRadio message."""
        from_radio = mesh_pb2.FromRadio()
        from_radio.queueStatus.free = 5

        context = _FromRadioContext(
            message=from_radio,
            message_dict=_LazyMessageDict(from_radio),
            config_id=None,
        )

        result = receive_pipeline._handle_from_radio_queue_status(context)

        assert result == []  # No publication intents
        mock_interface._queue_send_runtime._handle_queue_status_from_radio.assert_called_once()


class TestHandleFromRadioClientNotification:
    """Tests for _handle_from_radio_client_notification method (lines 405-414)."""

    @pytest.mark.unit
    def test_handle_client_notification(
        self, receive_pipeline: ReceivePipeline
    ) -> None:
        """Test handling clientNotification FromRadio message."""
        from_radio = mesh_pb2.FromRadio()
        from_radio.clientNotification.message = "Test notification"

        context = _FromRadioContext(
            message=from_radio,
            message_dict=_LazyMessageDict(from_radio),
            config_id=None,
        )

        result = receive_pipeline._handle_from_radio_client_notification(context)

        assert len(result) == 1
        assert result[0].topic == "meshtastic.clientNotification"


class TestHandleFromRadioMqttClientProxyMessage:
    """Tests for _handle_from_radio_mqtt_client_proxy_message method (lines 416-425)."""

    @pytest.mark.unit
    def test_handle_mqtt_client_proxy_message(
        self, receive_pipeline: ReceivePipeline
    ) -> None:
        """Test handling mqttClientProxyMessage FromRadio message."""
        from_radio = mesh_pb2.FromRadio()
        from_radio.mqttClientProxyMessage.topic = "test/topic"
        from_radio.mqttClientProxyMessage.data = b"test data"

        context = _FromRadioContext(
            message=from_radio,
            message_dict=_LazyMessageDict(from_radio),
            config_id=None,
        )

        result = receive_pipeline._handle_from_radio_mqtt_client_proxy_message(context)

        assert len(result) == 1
        assert result[0].topic == "meshtastic.mqttclientproxymessage"


class TestHandleFromRadioXmodemPacket:
    """Tests for _handle_from_radio_xmodem_packet method (lines 427-436)."""

    @pytest.mark.unit
    def test_handle_xmodem_packet(self, receive_pipeline: ReceivePipeline) -> None:
        """Test handling xmodemPacket FromRadio message."""
        from_radio = mesh_pb2.FromRadio()
        from_radio.xmodemPacket.buffer = b"xmodem data"

        context = _FromRadioContext(
            message=from_radio,
            message_dict=_LazyMessageDict(from_radio),
            config_id=None,
        )

        result = receive_pipeline._handle_from_radio_xmodem_packet(context)

        assert len(result) == 1
        assert result[0].topic == "meshtastic.xmodempacket"


class TestHandleFromRadioRebooted:
    """Tests for _handle_from_radio_rebooted method (lines 438-444)."""

    @pytest.mark.unit
    def test_handle_rebooted(
        self, receive_pipeline: ReceivePipeline, mock_interface: MagicMock
    ) -> None:
        """Test handling rebooted FromRadio message."""
        from_radio = mesh_pb2.FromRadio()
        from_radio.rebooted = True

        context = _FromRadioContext(
            message=from_radio,
            message_dict=_LazyMessageDict(from_radio),
            config_id=None,
        )

        result = receive_pipeline._handle_from_radio_rebooted(context)

        assert result == []  # No publication intents
        mock_interface._disconnected.assert_called_once()
        mock_interface._start_config.assert_called_once()


class TestApplyConfigFromRadio:
    """Tests for _apply_config_from_radio method (lines 453-458)."""

    @pytest.mark.unit
    def test_apply_local_config_from_radio(
        self, receive_pipeline: ReceivePipeline, mock_interface: MagicMock
    ) -> None:
        """Test applying local config from radio."""
        from_radio = mesh_pb2.FromRadio()
        # Set region as integer (1 = US)
        from_radio.config.lora.region = 1  # type: ignore[assignment]

        result = receive_pipeline._apply_local_config_from_radio(from_radio.config)

        assert result is True
        assert mock_interface.localNode.localConfig.lora.region == 1

    @pytest.mark.unit
    def test_apply_module_config_from_radio(
        self, receive_pipeline: ReceivePipeline, mock_interface: MagicMock
    ) -> None:
        """Test applying module config from radio."""
        from_radio = mesh_pb2.FromRadio()
        from_radio.moduleConfig.mqtt.enabled = True

        result = receive_pipeline._apply_module_config_from_radio(
            from_radio.moduleConfig
        )

        assert result is True
        assert mock_interface.localNode.moduleConfig.mqtt.enabled is True


class TestPublicationIntentMethods:
    """Tests for _publication_intent and _emit_publication_intents methods (lines 503-519)."""

    @pytest.mark.unit
    def test_publication_intent_creation(
        self, receive_pipeline: ReceivePipeline
    ) -> None:
        """Test creating a publication intent."""
        intent = receive_pipeline._publication_intent("test.topic", key="value")

        assert intent.topic == "test.topic"
        assert intent.payload == {"key": "value"}

    @pytest.mark.unit
    def test_emit_publication_intents(self, receive_pipeline: ReceivePipeline) -> None:
        """Test emitting publication intents."""
        intents = [
            _PublicationIntent(topic="topic1", payload={"key": "value1"}),
            _PublicationIntent(topic="topic2", payload={"key": "value2"}),
        ]

        with patch.object(receive_pipeline, "_queue_publication") as mock_queue:
            receive_pipeline._emit_publication_intents(intents)

            assert mock_queue.call_count == 2


class TestFixupPosition:
    """Tests for _fixup_position method (lines 521-527)."""

    @pytest.mark.unit
    def test_fixup_position_with_integer_coords(
        self, receive_pipeline: ReceivePipeline
    ) -> None:
        """Test converting integer micro-degrees to float degrees."""
        position = {
            "latitudeI": 374560000,  # 37.456 degrees * 1e7
            "longitudeI": -1222345000,  # -122.2345 degrees * 1e7
        }

        result = receive_pipeline._fixup_position(position)

        assert result["latitude"] == pytest.approx(37.456)
        assert result["longitude"] == pytest.approx(-122.2345)

    @pytest.mark.unit
    def test_fixup_position_no_coords(self, receive_pipeline: ReceivePipeline) -> None:
        """Test that position without integer coords is unchanged."""
        position = {"latitude": 37.456, "longitude": -122.2345}

        result = receive_pipeline._fixup_position(position)

        assert result == position


class TestGetOrCreateByNum:
    """Tests for _get_or_create_by_num method (lines 529-553)."""

    @pytest.mark.unit
    def test_get_existing_node(
        self, receive_pipeline: ReceivePipeline, mock_interface: MagicMock
    ) -> None:
        """Test getting an existing node by number."""
        existing_node = {"num": 12345, "user": {"id": "!1234"}}
        mock_interface.nodesByNum = {12345: existing_node}

        result = receive_pipeline._get_or_create_by_num(12345)

        assert result is existing_node

    @pytest.mark.unit
    def test_create_new_node(
        self, receive_pipeline: ReceivePipeline, mock_interface: MagicMock
    ) -> None:
        """Test creating a new node when not found."""
        mock_interface.nodesByNum = {}

        result = receive_pipeline._get_or_create_by_num(12345)

        assert result["num"] == 12345
        assert "user" in result
        assert result["user"]["id"] == "!00003039"  # 12345 in hex, padded

    @pytest.mark.unit
    def test_get_or_create_broadcast_raises(
        self, receive_pipeline: ReceivePipeline
    ) -> None:
        """Test that creating a broadcast node raises an error."""
        from meshtastic import BROADCAST_NUM  # pylint: disable=import-outside-toplevel

        with pytest.raises(MeshInterface.MeshInterfaceError, match="broadcast"):
            receive_pipeline._get_or_create_by_num(BROADCAST_NUM)


class TestHandleChannel:
    """Tests for _handle_channel method (lines 555-558)."""

    @pytest.mark.unit
    def test_handle_channel(
        self, receive_pipeline: ReceivePipeline, mock_interface: MagicMock
    ) -> None:
        """Test handling a channel."""
        channel = channel_pb2.Channel()
        channel.index = 0
        channel.settings.channel_num = 1

        receive_pipeline._handle_channel(channel)

        assert len(mock_interface._localChannels) == 1


class TestHandleLogRecord:
    """Tests for _handle_log_record method (lines 560-562)."""

    @pytest.mark.unit
    def test_handle_log_record(
        self, receive_pipeline: ReceivePipeline, mock_interface: MagicMock
    ) -> None:
        """Test handling a log record."""
        record = mesh_pb2.LogRecord()
        record.message = "Test log"

        receive_pipeline._handle_log_record(record)

        mock_interface._handle_log_line.assert_called_once_with("Test log")


class TestHandleConfigComplete:
    """Tests for _handle_config_complete method (lines 564-569)."""

    @pytest.mark.unit
    def test_handle_config_complete(
        self, receive_pipeline: ReceivePipeline, mock_interface: MagicMock
    ) -> None:
        """Test handling config complete."""
        channel = channel_pb2.Channel()
        channel.index = 0
        mock_interface._localChannels.append(channel)

        receive_pipeline._handle_config_complete()

        mock_interface.localNode.setChannels.assert_called_once()
        mock_interface._connected.assert_called_once()


class TestHandleQueueStatusFromRadio:
    """Tests for _handle_queue_status_from_radio method (lines 571-576)."""

    @pytest.mark.unit
    def test_handle_queue_status(
        self, receive_pipeline: ReceivePipeline, mock_interface: MagicMock
    ) -> None:
        """Test handling queue status from radio."""
        queue_status = mesh_pb2.QueueStatus()
        queue_status.free = 5

        receive_pipeline._handle_queue_status_from_radio(queue_status)

        mock_interface._queue_send_runtime._handle_queue_status_from_radio.assert_called_once_with(
            queue_status
        )


class TestNormalizePacketFromRadio:
    """Tests for _normalize_packet_from_radio method (lines 612-635)."""

    @pytest.mark.unit
    def test_normalize_valid_packet(self, receive_pipeline: ReceivePipeline) -> None:
        """Test normalizing a valid packet."""
        mesh_packet = mesh_pb2.MeshPacket()
        mesh_packet.id = 12345
        setattr(mesh_packet, "from", 67890)
        mesh_packet.to = 11111
        mesh_packet.decoded.payload = b"test data"

        result = receive_pipeline._normalize_packet_from_radio(
            mesh_packet, allow_zero_source=False
        )

        assert result is not None
        assert result["id"] == 12345
        assert result["from"] == 67890
        assert result["to"] == 11111
        assert "raw" in result

    @pytest.mark.unit
    def test_normalize_packet_from_zero_returns_none(
        self, receive_pipeline: ReceivePipeline
    ) -> None:
        """Test that packet with from=0 returns None (loopback)."""
        mesh_packet = mesh_pb2.MeshPacket()
        setattr(mesh_packet, "from", 0)

        result = receive_pipeline._normalize_packet_from_radio(
            mesh_packet, allow_zero_source=False
        )

        assert result is None

    @pytest.mark.unit
    def test_normalize_packet_default_to(
        self, receive_pipeline: ReceivePipeline
    ) -> None:
        """Test that packet without 'to' field defaults to 0."""
        mesh_packet = mesh_pb2.MeshPacket()
        setattr(mesh_packet, "from", 12345)

        result = receive_pipeline._normalize_packet_from_radio(
            mesh_packet, allow_zero_source=False
        )

        assert result is not None
        assert result["to"] == 0


class TestEnrichPacketIdentity:
    """Tests for _enrich_packet_identity method (lines 637-646)."""

    @pytest.mark.unit
    def test_enrich_packet_identity(
        self, receive_pipeline: ReceivePipeline, mock_interface: MagicMock
    ) -> None:
        """Test enriching packet identity with fromId and toId."""
        mock_interface.nodesByNum = {
            12345: {"user": {"id": "!1234"}},
            67890: {"user": {"id": "!6789"}},
        }

        packet_dict = {"from": 12345, "to": 67890}

        receive_pipeline._enrich_packet_identity(packet_dict)

        assert packet_dict["fromId"] == "!1234"
        assert packet_dict["toId"] == "!6789"

    @pytest.mark.unit
    def test_enrich_packet_identity_missing_node(
        self,
        receive_pipeline: ReceivePipeline,
        mock_interface: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test enriching packet when nodes are missing."""
        mock_interface.nodesByNum = {}

        packet_dict = {"from": 12345, "to": 67890}

        with caplog.at_level(logging.WARNING):
            receive_pipeline._enrich_packet_identity(packet_dict)

        # When nodes are missing, fromId/toId should be set to None, not absent
        assert packet_dict["fromId"] is None
        assert packet_dict["toId"] is None


class TestNodeNumToId:
    """Tests for _node_num_to_id method (lines 648-674)."""

    @pytest.mark.unit
    def test_node_num_to_id_broadcast_dest(
        self, receive_pipeline: ReceivePipeline
    ) -> None:
        """Test mapping broadcast num to broadcast address (dest mode)."""
        from meshtastic import (  # pylint: disable=import-outside-toplevel
            BROADCAST_ADDR,
            BROADCAST_NUM,
        )

        result = receive_pipeline._node_num_to_id(BROADCAST_NUM, isDest=True)

        assert result == BROADCAST_ADDR

    @pytest.mark.unit
    def test_node_num_to_id_broadcast_source(
        self, receive_pipeline: ReceivePipeline
    ) -> None:
        """Test mapping broadcast num to 'Unknown' (source mode)."""
        from meshtastic import BROADCAST_NUM  # pylint: disable=import-outside-toplevel

        result = receive_pipeline._node_num_to_id(BROADCAST_NUM, isDest=False)

        assert result == "Unknown"

    @pytest.mark.unit
    def test_node_num_to_id_valid(
        self, receive_pipeline: ReceivePipeline, mock_interface: MagicMock
    ) -> None:
        """Test mapping valid node num to id."""
        mock_interface.nodesByNum = {
            12345: {"user": {"id": "!1234"}},
        }

        result = receive_pipeline._node_num_to_id(12345)

        assert result == "!1234"

    @pytest.mark.unit
    def test_node_num_to_id_not_found(
        self, receive_pipeline: ReceivePipeline, mock_interface: MagicMock
    ) -> None:
        """Test mapping non-existent node num."""
        mock_interface.nodesByNum = {}

        result = receive_pipeline._node_num_to_id(12345)

        assert result is None


class TestClassifyPacketRuntime:
    """Tests for _classify_packet_runtime method (lines 676-699)."""

    @pytest.mark.unit
    def test_classify_packet_with_decoded(
        self, receive_pipeline: ReceivePipeline
    ) -> None:
        """Test classifying packet with decoded payload."""
        packet_context = _PacketRuntimeContext(
            packet_dict={"decoded": {"portnum": "TEXT_MESSAGE_APP"}}
        )
        mesh_packet = mesh_pb2.MeshPacket()
        mesh_packet.decoded.portnum = portnums_pb2.PortNum.TEXT_MESSAGE_APP
        mesh_packet.decoded.payload = b"hello"

        receive_pipeline._classify_packet_runtime(packet_context, mesh_packet)

        assert packet_context.decoded is not None
        assert packet_context.topic == "meshtastic.receive.data.TEXT_MESSAGE_APP"

    @pytest.mark.unit
    def test_classify_packet_without_decoded(
        self, receive_pipeline: ReceivePipeline
    ) -> None:
        """Test classifying packet without decoded payload."""
        packet_context = _PacketRuntimeContext(packet_dict={"raw": "data"})
        mesh_packet = mesh_pb2.MeshPacket()

        receive_pipeline._classify_packet_runtime(packet_context, mesh_packet)

        assert packet_context.decoded is None
        assert packet_context.topic == "meshtastic.receive"


class TestApplyPacketRuntimeMutations:
    """Tests for _apply_packet_runtime_mutations method (lines 701-719)."""

    @pytest.mark.unit
    def test_apply_mutations_no_decoded(
        self, receive_pipeline: ReceivePipeline
    ) -> None:
        """Test applying mutations when no decoded payload."""
        packet_context = _PacketRuntimeContext(packet_dict={"raw": "data"})
        mesh_packet = mesh_pb2.MeshPacket()

        receive_pipeline._apply_packet_runtime_mutations(packet_context, mesh_packet)

        assert packet_context.on_receive_callback is None


class TestDecodePacketPayloadWithHandler:
    """Tests for _decode_packet_payload_with_handler method (lines 721-757)."""

    @pytest.mark.unit
    def test_decode_with_no_factory(self, receive_pipeline: ReceivePipeline) -> None:
        """Test decoding when handler has no protobuf factory."""
        packet_context = _PacketRuntimeContext(
            packet_dict={"decoded": {"test_handler": {}}}
        )
        packet_context.decoded = packet_context.packet_dict["decoded"]
        mesh_packet = mesh_pb2.MeshPacket()
        handler = MagicMock()
        handler.protobufFactory = None
        handler.name = "test_handler"

        receive_pipeline._decode_packet_payload_with_handler(
            packet_context, mesh_packet, handler
        )

        # Should not modify packet_context when no factory

    @pytest.mark.unit
    def test_decode_with_factory_success(
        self, receive_pipeline: ReceivePipeline
    ) -> None:
        """Test successful decoding with protobuf factory."""
        from meshtastic.protobuf import (  # pylint: disable=import-outside-toplevel
            telemetry_pb2,
        )

        packet_context = _PacketRuntimeContext(
            packet_dict={"decoded": {"telemetry": {}}}
        )
        packet_context.decoded = packet_context.packet_dict["decoded"]

        mesh_packet = mesh_pb2.MeshPacket()
        telemetry = telemetry_pb2.Telemetry()
        telemetry.device_metrics.battery_level = 85
        mesh_packet.decoded.payload = telemetry.SerializeToString()

        handler = MagicMock()
        handler.protobufFactory = telemetry_pb2.Telemetry
        handler.name = "telemetry"

        receive_pipeline._decode_packet_payload_with_handler(
            packet_context, mesh_packet, handler
        )

        assert "telemetry" in packet_context.packet_dict["decoded"]
        assert "raw" in packet_context.packet_dict["decoded"]["telemetry"]

    @pytest.mark.unit
    def test_decode_failure(
        self, receive_pipeline: ReceivePipeline, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test handling decode failure."""
        packet_context = _PacketRuntimeContext(packet_dict={"decoded": {"routing": {}}})
        packet_context.decoded = packet_context.packet_dict["decoded"]

        mesh_packet = mesh_pb2.MeshPacket()
        mesh_packet.decoded.payload = b"invalid data"
        mesh_packet.id = 12345

        handler = MagicMock()
        handler.protobufFactory = mesh_pb2.Routing
        handler.name = "routing"

        with caplog.at_level(logging.WARNING):
            receive_pipeline._decode_packet_payload_with_handler(
                packet_context, mesh_packet, handler
            )

        assert "Failed to decode routing payload" in caplog.text
        assert DECODE_FAILED_PREFIX in str(
            packet_context.packet_dict["decoded"]["routing"]
        )


class TestInvokePacketOnReceive:
    """Tests for _invoke_packet_on_receive method (lines 759-763)."""

    @pytest.mark.unit
    def test_invoke_callback(self, receive_pipeline: ReceivePipeline) -> None:
        """Test invoking onReceive callback."""
        callback = MagicMock()
        packet_context = _PacketRuntimeContext(
            packet_dict={"key": "value"}, on_receive_callback=callback
        )

        receive_pipeline._invoke_packet_on_receive(packet_context)

        callback.assert_called_once_with(
            receive_pipeline._interface, packet_context.packet_dict
        )

    @pytest.mark.unit
    def test_invoke_no_callback(self, receive_pipeline: ReceivePipeline) -> None:
        """Test that no error when callback is None."""
        packet_context = _PacketRuntimeContext(
            packet_dict={"key": "value"}, on_receive_callback=None
        )

        # Should not raise
        receive_pipeline._invoke_packet_on_receive(packet_context)


class TestCorrelatePacketResponseHandler:
    """Tests for _correlate_packet_response_handler method (lines 765-776)."""

    @pytest.mark.unit
    def test_correlate_response(
        self, receive_pipeline: ReceivePipeline, mock_interface: MagicMock
    ) -> None:
        """Test correlating packet response."""
        packet_context = _PacketRuntimeContext(
            packet_dict={"decoded": {"requestId": 12345}}
        )
        packet_context.decoded = packet_context.packet_dict["decoded"]

        receive_pipeline._correlate_packet_response_handler(packet_context)

        mock_interface._request_wait_runtime.correlate_inbound_response.assert_called_once()

    @pytest.mark.unit
    def test_correlate_no_decoded(
        self, receive_pipeline: ReceivePipeline, mock_interface: MagicMock
    ) -> None:
        """Test correlating when no decoded payload."""
        packet_context = _PacketRuntimeContext(packet_dict={"raw": "data"})

        receive_pipeline._correlate_packet_response_handler(packet_context)

        mock_interface._request_wait_runtime.correlate_inbound_response.assert_not_called()
