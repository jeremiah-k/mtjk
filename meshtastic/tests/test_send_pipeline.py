"""Meshtastic unit tests for mesh_interface_runtime/send_pipeline.py."""

# pylint: disable=redefined-outer-name,protected-access

import logging
import threading
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from meshtastic import BROADCAST_ADDR, BROADCAST_NUM, LOCAL_ADDR
from meshtastic.mesh_interface_runtime.request_wait import (
    WAIT_ATTR_NAK,
    WAIT_ATTR_POSITION,
    WAIT_ATTR_TELEMETRY,
    WAIT_ATTR_TRACEROUTE,
    WAIT_ATTR_WAYPOINT,
)
from meshtastic.mesh_interface_runtime.send_pipeline import (
    HEX_NODE_ID_TAIL_CHARS,
    LEGACY_UNSCOPED_WAIT_ATTR_BY_PORTNUM,
    LORA_CONFIG_WAIT_SECONDS,
    MISSING_NODE_NUM_ERROR_TEMPLATE,
    NODE_NOT_FOUND_DB_UNAVAILABLE_ERROR_TEMPLATE,
    NODE_NOT_FOUND_IN_DB_ERROR_TEMPLATE,
    PACKET_ID_COUNTER_MASK,
    PACKET_ID_MASK,
    PACKET_ID_RANDOM_MAX,
    PACKET_ID_RANDOM_SHIFT_BITS,
    QUEUE_WAIT_DELAY_SECONDS,
    SendPipeline,
    _emit_response_summary,
    _extract_hex_node_id_body,
    _format_missing_node_num_error,
    _format_node_db_unavailable_error,
    _format_node_not_found_in_db_error,
)
from meshtastic.protobuf import mesh_pb2, portnums_pb2

# Line 36: TYPE_CHECKING for MeshInterface import
# This is tested implicitly by the fact that SendPipeline works with mocked interfaces


@pytest.fixture
def mock_interface() -> MagicMock:
    """Create a minimal mock MeshInterface for send pipeline tests."""
    interface = MagicMock()
    interface._node_db_lock = threading.RLock()
    interface._request_wait_runtime = MagicMock()
    interface._queue_send_runtime = MagicMock()
    interface.localNode = MagicMock()
    interface.myInfo = MagicMock()
    interface.myInfo.my_node_num = 12345
    interface.nodes = {}
    interface.nodesByNum = {}
    interface.configId = 123
    interface.noProto = False
    interface._acknowledgment = MagicMock()
    interface._timeout = MagicMock()
    interface._timeout.expireTimeout = 300.0
    interface._generate_packet_id = MagicMock(return_value=12345)
    interface._wait_connected = MagicMock()
    interface._queue_pop_for_send = MagicMock()

    # Create MeshInterfaceError dynamically
    class MeshInterfaceError(Exception):
        """Custom exception for MeshInterface errors."""

        def __init__(self, message: str) -> None:
            """Initialize the error with a message."""
            self.message = message
            super().__init__(message)

    interface.MeshInterfaceError = MeshInterfaceError
    return interface


@pytest.fixture
def send_pipeline(mock_interface: MagicMock) -> SendPipeline:
    """Create a SendPipeline instance with mocked interface."""
    return SendPipeline(mock_interface)


class TestModuleLevelConstants:
    """Tests for module-level constants."""

    @pytest.mark.unit
    def test_packet_id_constants(self) -> None:
        """Test packet ID generation constants (lines 40-43)."""
        assert PACKET_ID_MASK == 0xFFFFFFFF
        assert PACKET_ID_COUNTER_MASK == 0x3FF
        assert PACKET_ID_RANDOM_MAX == 0x3FFFFF
        assert PACKET_ID_RANDOM_SHIFT_BITS == 10

    @pytest.mark.unit
    def test_queue_wait_delay(self) -> None:
        """Test queue wait delay constant (line 45)."""
        assert QUEUE_WAIT_DELAY_SECONDS == 0.5

    @pytest.mark.unit
    def test_legacy_wait_attr_mapping(self) -> None:
        """Test legacy unscoped wait attribute mapping (lines 47-52)."""
        assert isinstance(LEGACY_UNSCOPED_WAIT_ATTR_BY_PORTNUM, dict)
        assert (
            LEGACY_UNSCOPED_WAIT_ATTR_BY_PORTNUM[portnums_pb2.PortNum.POSITION_APP]
            == WAIT_ATTR_POSITION
        )
        assert (
            LEGACY_UNSCOPED_WAIT_ATTR_BY_PORTNUM[portnums_pb2.PortNum.TRACEROUTE_APP]
            == WAIT_ATTR_TRACEROUTE
        )
        assert (
            LEGACY_UNSCOPED_WAIT_ATTR_BY_PORTNUM[portnums_pb2.PortNum.TELEMETRY_APP]
            == WAIT_ATTR_TELEMETRY
        )
        assert (
            LEGACY_UNSCOPED_WAIT_ATTR_BY_PORTNUM[portnums_pb2.PortNum.WAYPOINT_APP]
            == WAIT_ATTR_WAYPOINT
        )

    @pytest.mark.unit
    def test_hex_node_id_chars(self) -> None:
        """Test hex node ID character set (line 54)."""
        assert isinstance(HEX_NODE_ID_TAIL_CHARS, frozenset)
        assert "0" in HEX_NODE_ID_TAIL_CHARS
        assert "f" in HEX_NODE_ID_TAIL_CHARS
        assert "A" in HEX_NODE_ID_TAIL_CHARS
        assert "F" in HEX_NODE_ID_TAIL_CHARS
        assert "g" not in HEX_NODE_ID_TAIL_CHARS

    @pytest.mark.unit
    def test_error_templates(self) -> None:
        """Test error message templates (lines 55-59)."""
        assert "{destination_id}" in MISSING_NODE_NUM_ERROR_TEMPLATE
        assert "{destination_id}" in NODE_NOT_FOUND_IN_DB_ERROR_TEMPLATE
        assert "{destination_id}" in NODE_NOT_FOUND_DB_UNAVAILABLE_ERROR_TEMPLATE


class TestSerializablePayloadProtocol:
    """Tests for _SerializablePayload Protocol (lines 62-67)."""

    @pytest.mark.unit
    def test_protocol_with_serialize_method(self) -> None:
        """Test that objects with SerializeToString satisfy the protocol."""

        class MockPayload:
            """Mock payload for protocol testing."""

            def SerializeToString(self) -> bytes:
                """Serialize the payload."""
                return b"serialized"

        # This should work without error
        payload = MockPayload()
        assert payload.SerializeToString() == b"serialized"


class TestFormatMissingNodeNumError:
    """Tests for _format_missing_node_num_error function (lines 73-75)."""

    @pytest.mark.unit
    def test_format_missing_node_num_error(self) -> None:
        """Test formatting missing node num error message."""
        result = _format_missing_node_num_error("!1234abcd")
        assert "!1234abcd" in result
        assert "num" in result.lower()


class TestFormatNodeNotFoundInDbError:
    """Tests for _format_node_not_found_in_db_error function (lines 78-80)."""

    @pytest.mark.unit
    def test_format_node_not_found_in_db_error(self) -> None:
        """Test formatting node not found in DB error message."""
        result = _format_node_not_found_in_db_error("!1234abcd")
        assert "!1234abcd" in result
        assert "not found" in result.lower()


class TestFormatNodeDbUnavailableError:
    """Tests for _format_node_db_unavailable_error function (lines 83-87)."""

    @pytest.mark.unit
    def test_format_node_db_unavailable_error(self) -> None:
        """Test formatting node DB unavailable error message."""
        result = _format_node_db_unavailable_error("!1234abcd")
        assert "!1234abcd" in result
        assert "unavailable" in result.lower()


class TestExtractHexNodeIdBody:
    """Tests for _extract_hex_node_id_body function (lines 90-101)."""

    @pytest.mark.unit
    def test_extract_hex_node_id_with_bang_prefix(self) -> None:
        """Test extracting hex body with ! prefix."""
        result = _extract_hex_node_id_body("!1234abcd")
        assert result == "1234abcd"

    @pytest.mark.unit
    def test_extract_hex_node_id_with_0x_prefix(self) -> None:
        """Test extracting hex body with 0x prefix."""
        result = _extract_hex_node_id_body("0x1234abcd")
        assert result == "1234abcd"

    @pytest.mark.unit
    def test_extract_hex_node_id_with_0X_prefix(self) -> None:
        """Test extracting hex body with 0X prefix."""
        result = _extract_hex_node_id_body("0X1234ABCD")
        assert result == "1234ABCD"

    @pytest.mark.unit
    def test_extract_hex_node_id_wrong_length(self) -> None:
        """Test extracting hex body with wrong length."""
        result = _extract_hex_node_id_body("!1234abc")
        assert result is None

    @pytest.mark.unit
    def test_extract_hex_node_id_invalid_chars(self) -> None:
        """Test extracting hex body with invalid characters."""
        result = _extract_hex_node_id_body("!1234gbcz")
        assert result is None

    @pytest.mark.unit
    def test_extract_hex_node_id_valid_8chars(self) -> None:
        """Test extracting valid 8-char hex body."""
        result = _extract_hex_node_id_body("1234abcd")
        assert result == "1234abcd"


class TestEmitResponseSummary:
    """Tests for _emit_response_summary function (lines 104-106)."""

    @pytest.mark.unit
    def test_emit_response_summary_logs_info(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that _emit_response_summary logs at INFO level."""
        with caplog.at_level(logging.INFO):
            _emit_response_summary("Test response summary")

        assert "Test response summary" in caplog.text


class TestSendPipelineInit:
    """Tests for SendPipeline initialization (lines 109-124)."""

    @pytest.mark.unit
    def test_send_pipeline_init(self, mock_interface: MagicMock) -> None:
        """Test SendPipeline initialization."""
        pipeline = SendPipeline(mock_interface)

        assert pipeline._interface is mock_interface


class TestSendPipelineProperties:
    """Tests for SendPipeline properties (lines 126-179)."""

    @pytest.mark.unit
    def test_node_db_lock_property(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test _node_db_lock property returns interface lock."""
        assert send_pipeline._node_db_lock is mock_interface._node_db_lock

    @pytest.mark.unit
    def test_request_wait_runtime_property(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test _request_wait_runtime property (lines 132-134)."""
        assert (
            send_pipeline._request_wait_runtime is mock_interface._request_wait_runtime
        )

    @pytest.mark.unit
    def test_queue_send_runtime_property(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test _queue_send_runtime property (lines 137-139)."""
        assert send_pipeline._queue_send_runtime is mock_interface._queue_send_runtime

    @pytest.mark.unit
    def test_local_node_property(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test localNode property."""
        assert send_pipeline.localNode is mock_interface.localNode

    @pytest.mark.unit
    def test_my_info_property(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test myInfo property."""
        assert send_pipeline.myInfo is mock_interface.myInfo

    @pytest.mark.unit
    def test_nodes_property(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test nodes property."""
        assert send_pipeline.nodes is mock_interface.nodes

    @pytest.mark.unit
    def test_nodes_by_num_property(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test nodesByNum property."""
        assert send_pipeline.nodesByNum is mock_interface.nodesByNum

    @pytest.mark.unit
    def test_config_id_property(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test configId property (lines 162-164)."""
        assert send_pipeline.configId == 123
        mock_interface.configId = 456
        assert send_pipeline.configId == 456

    @pytest.mark.unit
    def test_no_proto_property(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test noProto property (lines 167-169)."""
        mock_interface.noProto = True
        assert send_pipeline.noProto is True
        mock_interface.noProto = False
        assert send_pipeline.noProto is False

    @pytest.mark.unit
    def test_acknowledgment_property(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test _acknowledgment property."""
        assert send_pipeline._acknowledgment is mock_interface._acknowledgment

    @pytest.mark.unit
    def test_timeout_property(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test _timeout property (lines 177-179)."""
        assert send_pipeline._timeout is mock_interface._timeout


class TestSendText:
    """Tests for sendText method (lines 181-204)."""

    @pytest.mark.unit
    def test_send_text_calls_send_data(self, send_pipeline: SendPipeline) -> None:
        """Test that sendText calls sendData with encoded text."""
        with patch.object(send_pipeline, "sendData") as mock_send_data:
            mock_send_data.return_value = MagicMock()
            send_pipeline.sendText(
                text="Hello, World!",
                destinationId="!1234abcd",
                wantAck=True,
                wantResponse=False,
                channelIndex=0,
            )

        mock_send_data.assert_called_once()
        call_args = mock_send_data.call_args
        assert call_args[0][0] == b"Hello, World!"  # Text encoded to bytes
        assert call_args[0][1] == "!1234abcd"  # destinationId is second positional arg


class TestSendAlert:
    """Tests for sendAlert method (lines 206-225)."""

    @pytest.mark.unit
    def test_send_alert_calls_send_data(self, send_pipeline: SendPipeline) -> None:
        """Test that sendAlert calls sendData with ALERT_APP port."""
        with patch.object(send_pipeline, "sendData") as mock_send_data:
            mock_send_data.return_value = MagicMock()
            send_pipeline.sendAlert(
                text="Alert message",
                destinationId=BROADCAST_ADDR,
                channelIndex=0,
            )

        mock_send_data.assert_called_once()
        call_args = mock_send_data.call_args
        assert call_args[0][0] == b"Alert message"
        assert call_args[1]["portNum"] == portnums_pb2.PortNum.ALERT_APP
        assert call_args[1]["priority"] == mesh_pb2.MeshPacket.Priority.ALERT


class TestSendMqttClientProxyMessage:
    """Tests for sendMqttClientProxyMessage method (lines 227-234)."""

    @pytest.mark.unit
    def test_send_mqtt_client_proxy_message(self, send_pipeline: SendPipeline) -> None:
        """Test sending MQTT client proxy message."""
        with patch.object(send_pipeline, "_send_to_radio") as mock_send:
            send_pipeline.sendMqttClientProxyMessage("test/topic", b"test data")

        mock_send.assert_called_once()
        call_args = mock_send.call_args[0][0]
        assert call_args.mqttClientProxyMessage.topic == "test/topic"
        assert call_args.mqttClientProxyMessage.data == b"test data"


class TestSendData:
    """Tests for sendData method (lines 236-275)."""

    @pytest.mark.unit
    def test_send_data_with_legacy_wait_attr(self, send_pipeline: SendPipeline) -> None:
        """Test that sendData clears legacy wait attributes."""
        with patch.object(send_pipeline, "_clear_wait_error") as mock_clear:
            with patch.object(send_pipeline, "_send_data_with_wait") as mock_send:
                mock_send.return_value = MagicMock()
                send_pipeline.sendData(
                    b"test data",
                    destinationId=BROADCAST_ADDR,
                    portNum=portnums_pb2.PortNum.POSITION_APP,
                )

        mock_clear.assert_called_once_with(
            WAIT_ATTR_POSITION, request_id=None, clear_scoped=False
        )

    @pytest.mark.unit
    def test_send_data_no_legacy_wait_attr(self, send_pipeline: SendPipeline) -> None:
        """Test that sendData works without legacy wait attributes."""
        with patch.object(send_pipeline, "_clear_wait_error") as mock_clear:
            with patch.object(send_pipeline, "_send_data_with_wait") as mock_send:
                mock_send.return_value = MagicMock()
                send_pipeline.sendData(
                    b"test data",
                    destinationId=BROADCAST_ADDR,
                    portNum=portnums_pb2.PortNum.TEXT_MESSAGE_APP,
                )

        mock_clear.assert_not_called()


class TestSendDataWithWait:
    """Tests for _send_data_with_wait method."""

    @pytest.mark.unit
    def test_send_data_with_wait_serializes_protobuf(
        self, send_pipeline: SendPipeline
    ) -> None:
        """Test that _send_data_with_wait serializes protobuf payloads."""

        class MockProtobuf:
            """Mock protobuf message for testing."""

            def SerializeToString(self) -> bytes:
                """Serialize the protobuf message."""
                return b"serialized protobuf"

        with patch.object(send_pipeline._interface, "_send_packet") as mock_send_packet:
            mock_send_packet.return_value = MagicMock()
            send_pipeline._send_data_with_wait(
                MockProtobuf(),
                destinationId=BROADCAST_ADDR,
                portNum=portnums_pb2.PortNum.PRIVATE_APP,
            )

        mock_send_packet.assert_called_once()

    @pytest.mark.unit
    def test_send_data_with_wait_payload_too_big(
        self, send_pipeline: SendPipeline
    ) -> None:
        """Test that _send_data_with_wait raises error for oversized payload."""
        big_data = b"x" * (mesh_pb2.Constants.DATA_PAYLOAD_LEN + 1)

        with pytest.raises(
            send_pipeline._interface.MeshInterfaceError, match="too big"
        ):
            send_pipeline._send_data_with_wait(
                big_data,
                destinationId=BROADCAST_ADDR,
                portNum=portnums_pb2.PortNum.PRIVATE_APP,
            )

    @pytest.mark.unit
    def test_send_data_with_wait_unknown_port(
        self, send_pipeline: SendPipeline
    ) -> None:
        """Test that _send_data_with_wait raises error for unknown port."""
        with pytest.raises(Exception, match="non-zero port"):
            send_pipeline._send_data_with_wait(
                b"data",
                destinationId=BROADCAST_ADDR,
                portNum=portnums_pb2.PortNum.UNKNOWN_APP,
            )

    @pytest.mark.unit
    def test_send_data_with_wait_registers_response_handler(
        self, send_pipeline: SendPipeline
    ) -> None:
        """Test that _send_data_with_wait registers response handler."""
        callback = MagicMock()

        with patch.object(send_pipeline._interface, "_send_packet") as mock_send_packet:
            with patch.object(
                send_pipeline, "_add_response_handler"
            ) as mock_add_handler:
                mock_send_packet.return_value = MagicMock()
                send_pipeline._send_data_with_wait(
                    b"data",
                    destinationId=BROADCAST_ADDR,
                    portNum=portnums_pb2.PortNum.PRIVATE_APP,
                    onResponse=callback,
                )

        mock_add_handler.assert_called_once()


class TestExtractRequestIdFromPacket:
    """Tests for _extract_request_id_from_packet method (lines 361-374)."""

    @pytest.mark.unit
    def test_extract_request_id_from_valid_packet(
        self, send_pipeline: SendPipeline
    ) -> None:
        """Test extracting request ID from valid packet."""
        packet = {"decoded": {"requestId": 12345}}
        result = send_pipeline._extract_request_id_from_packet(packet)
        assert result == 12345

    @pytest.mark.unit
    def test_extract_request_id_no_decoded(self, send_pipeline: SendPipeline) -> None:
        """Test extracting request ID when no decoded payload."""
        packet = {"raw": "data"}
        result = send_pipeline._extract_request_id_from_packet(packet)
        assert result is None

    @pytest.mark.unit
    def test_extract_request_id_from_string(self, send_pipeline: SendPipeline) -> None:
        """Test extracting request ID from string value."""
        packet = {"decoded": {"requestId": "12345"}}
        result = send_pipeline._extract_request_id_from_packet(packet)
        assert result == 12345

    @pytest.mark.unit
    def test_extract_request_id_zero_value(self, send_pipeline: SendPipeline) -> None:
        """Test that zero request ID returns None."""
        packet = {"decoded": {"requestId": 0}}
        result = send_pipeline._extract_request_id_from_packet(packet)
        assert result is None

    @pytest.mark.unit
    def test_extract_request_id_boolean_false(
        self, send_pipeline: SendPipeline
    ) -> None:
        """Test that boolean False request ID returns None."""
        packet = {"decoded": {"requestId": False}}
        result = send_pipeline._extract_request_id_from_packet(packet)
        assert result is None


class TestExtractRequestIdFromSentPacket:
    """Tests for _extract_request_id_from_sent_packet method (lines 376-381)."""

    @pytest.mark.unit
    def test_extract_from_sent_packet_valid(self, send_pipeline: SendPipeline) -> None:
        """Test extracting ID from sent packet with valid ID."""
        packet = MagicMock()
        packet.id = 12345
        result = send_pipeline._extract_request_id_from_sent_packet(packet)
        assert result == 12345

    @pytest.mark.unit
    def test_extract_from_sent_packet_zero(self, send_pipeline: SendPipeline) -> None:
        """Test that zero ID returns None."""
        packet = MagicMock()
        packet.id = 0
        result = send_pipeline._extract_request_id_from_sent_packet(packet)
        assert result is None

    @pytest.mark.unit
    def test_extract_from_sent_packet_negative(
        self, send_pipeline: SendPipeline
    ) -> None:
        """Test that negative ID returns None."""
        packet = MagicMock()
        packet.id = -1
        result = send_pipeline._extract_request_id_from_sent_packet(packet)
        assert result is None

    @pytest.mark.unit
    def test_extract_from_sent_packet_no_id(self, send_pipeline: SendPipeline) -> None:
        """Test extracting from packet without id attribute."""
        packet = MagicMock()
        packet.id = None
        result = send_pipeline._extract_request_id_from_sent_packet(packet)
        assert result is None


class TestWaitErrorMethods:
    """Tests for wait error management methods (lines 383-445)."""

    @pytest.mark.unit
    def test_clear_wait_error_delegates(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test _clear_wait_error delegates to request wait runtime (lines 383-395)."""
        send_pipeline._clear_wait_error("test_attr", request_id=12345)

        mock_interface._request_wait_runtime.clear_wait_error.assert_called_once_with(
            "test_attr",
            request_id=12345,
            clear_scoped=True,
        )

    @pytest.mark.unit
    def test_prune_retired_wait_request_ids(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test _prune_retired_wait_request_ids_locked delegates (lines 397-403)."""
        mock_interface._request_wait_runtime.prune_retired_wait_request_ids_locked.return_value = {
            12345: 123456.0
        }

        result = send_pipeline._prune_retired_wait_request_ids_locked("test_attr")

        assert 12345 in result

    @pytest.mark.unit
    def test_set_wait_error_delegates(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test _set_wait_error delegates to request wait runtime (lines 405-417)."""
        send_pipeline._set_wait_error("test_attr", "error message", request_id=12345)

        mock_interface._request_wait_runtime.set_wait_error.assert_called_once_with(
            "test_attr",
            "error message",
            request_id=12345,
        )

    @pytest.mark.unit
    def test_mark_wait_acknowledged_delegates(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test _mark_wait_acknowledged delegates to request wait runtime (lines 419-426)."""
        send_pipeline._mark_wait_acknowledged("test_attr", request_id=12345)

        mock_interface._request_wait_runtime.mark_wait_acknowledged.assert_called_once_with(
            "test_attr",
            request_id=12345,
        )

    @pytest.mark.unit
    def test_raise_wait_error_if_present_delegates(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test _raise_wait_error_if_present delegates to request wait runtime (lines 428-436)."""
        send_pipeline._raise_wait_error_if_present("test_attr", request_id=12345)

        mock_interface._request_wait_runtime.raise_wait_error_if_present.assert_called_once()

    @pytest.mark.unit
    def test_retire_wait_request_delegates(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test _retire_wait_request delegates to request wait runtime (lines 438-445)."""
        send_pipeline._retire_wait_request("test_attr", request_id=12345)

        mock_interface._request_wait_runtime.retire_wait_request.assert_called_once_with(
            "test_attr",
            request_id=12345,
        )


class TestWaitForRequestAck:
    """Tests for _wait_for_request_ack method (lines 447-459)."""

    @pytest.mark.unit
    def test_wait_for_request_ack_delegates(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test _wait_for_request_ack delegates to request wait runtime."""
        mock_interface._request_wait_runtime.wait_for_request_ack.return_value = True

        result = send_pipeline._wait_for_request_ack(
            WAIT_ATTR_POSITION,
            12345,
            timeout_seconds=30.0,
        )

        assert result is True
        mock_interface._request_wait_runtime.wait_for_request_ack.assert_called_once_with(
            WAIT_ATTR_POSITION,
            12345,
            timeout_seconds=30.0,
        )


class TestRecordRoutingWaitError:
    """Tests for _record_routing_wait_error method (lines 461-473)."""

    @pytest.mark.unit
    def test_record_routing_wait_error_delegates(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test _record_routing_wait_error delegates to request wait runtime."""
        send_pipeline._record_routing_wait_error(
            acknowledgment_attr=WAIT_ATTR_NAK,
            routing_error_reason="NO_RESPONSE",
            request_id=12345,
        )

        mock_interface._request_wait_runtime.record_routing_wait_error.assert_called_once()


class TestOnResponsePosition:
    """Tests for onResponsePosition method (lines 475-477)."""

    @pytest.mark.unit
    def test_on_response_position_delegates(self, send_pipeline: SendPipeline) -> None:
        """Test onResponsePosition delegates to flow function."""
        packet = {"decoded": {"position": {"latitude": 37.456}}}

        with patch(
            "meshtastic.mesh_interface_runtime.send_pipeline._on_response_position"
        ) as mock_flow:
            send_pipeline.onResponsePosition(packet)

        mock_flow.assert_called_once_with(send_pipeline._interface, packet)


class TestSendPosition:
    """Tests for sendPosition method (lines 479-501)."""

    @pytest.mark.unit
    def test_send_position_delegates(self, send_pipeline: SendPipeline) -> None:
        """Test sendPosition delegates to flow function."""
        with patch(
            "meshtastic.mesh_interface_runtime.send_pipeline.sendPosition"
        ) as mock_flow:
            mock_flow.return_value = MagicMock()
            send_pipeline.sendPosition(
                latitude=37.456,
                longitude=-122.2345,
                altitude=100,
                destinationId=BROADCAST_ADDR,
            )

        mock_flow.assert_called_once()
        call_kwargs = mock_flow.call_args[1]
        assert call_kwargs["latitude"] == 37.456
        assert call_kwargs["longitude"] == -122.2345
        assert call_kwargs["altitude"] == 100


class TestOnResponseTraceRoute:
    """Tests for onResponseTraceRoute method (lines 503-505)."""

    @pytest.mark.unit
    def test_on_response_traceroute_delegates(
        self, send_pipeline: SendPipeline
    ) -> None:
        """Test onResponseTraceRoute delegates to flow function."""
        packet: dict[str, Any] = {"decoded": {"traceroute": {}}}

        with patch(
            "meshtastic.mesh_interface_runtime.send_pipeline._on_response_traceroute"
        ) as mock_flow:
            send_pipeline.onResponseTraceRoute(packet)

        mock_flow.assert_called_once_with(send_pipeline._interface, packet)


class TestSendTraceRoute:
    """Tests for sendTraceRoute method (lines 507-511)."""

    @pytest.mark.unit
    def test_send_traceroute_delegates(self, send_pipeline: SendPipeline) -> None:
        """Test sendTraceRoute delegates to flow function."""
        with patch(
            "meshtastic.mesh_interface_runtime.send_pipeline.sendTraceroute"
        ) as mock_flow:
            send_pipeline.sendTraceRoute("!1234abcd", hopLimit=3, channelIndex=0)

        mock_flow.assert_called_once_with(
            send_pipeline._interface, "!1234abcd", 3, channelIndex=0
        )


class TestSendTelemetry:
    """Tests for sendTelemetry method (lines 513-529)."""

    @pytest.mark.unit
    def test_send_telemetry_delegates(self, send_pipeline: SendPipeline) -> None:
        """Test sendTelemetry delegates to flow function."""
        with patch(
            "meshtastic.mesh_interface_runtime.send_pipeline.sendTelemetry"
        ) as mock_flow:
            send_pipeline.sendTelemetry(
                destinationId=BROADCAST_ADDR,
                wantResponse=True,
                channelIndex=0,
            )

        mock_flow.assert_called_once()


class TestOnResponseTelemetry:
    """Tests for onResponseTelemetry method (lines 531-533)."""

    @pytest.mark.unit
    def test_on_response_telemetry_delegates(self, send_pipeline: SendPipeline) -> None:
        """Test onResponseTelemetry delegates to flow function."""
        packet: dict[str, Any] = {"decoded": {"telemetry": {}}}

        with patch(
            "meshtastic.mesh_interface_runtime.send_pipeline._on_response_telemetry"
        ) as mock_flow:
            send_pipeline.onResponseTelemetry(packet)

        mock_flow.assert_called_once_with(send_pipeline._interface, packet)


class TestOnResponseWaypoint:
    """Tests for onResponseWaypoint method (lines 535-537)."""

    @pytest.mark.unit
    def test_on_response_waypoint_delegates(self, send_pipeline: SendPipeline) -> None:
        """Test onResponseWaypoint delegates to flow function."""
        packet: dict[str, Any] = {"decoded": {"waypoint": {}}}

        with patch(
            "meshtastic.mesh_interface_runtime.send_pipeline._on_response_waypoint"
        ) as mock_flow:
            send_pipeline.onResponseWaypoint(packet)

        mock_flow.assert_called_once_with(send_pipeline._interface, packet)


class TestSendWaypoint:
    """Tests for sendWaypoint method (lines 539-569)."""

    @pytest.mark.unit
    def test_send_waypoint_delegates(self, send_pipeline: SendPipeline) -> None:
        """Test sendWaypoint delegates to flow function."""
        with patch(
            "meshtastic.mesh_interface_runtime.send_pipeline.sendWaypoint"
        ) as mock_flow:
            mock_flow.return_value = MagicMock()
            send_pipeline.sendWaypoint(
                name="Test Waypoint",
                description="Test Description",
                icon=1,
                expire=1234567890,
                latitude=37.456,
                longitude=-122.2345,
            )

        mock_flow.assert_called_once()


class TestDeleteWaypoint:
    """Tests for deleteWaypoint method (lines 571-589)."""

    @pytest.mark.unit
    def test_delete_waypoint_delegates(self, send_pipeline: SendPipeline) -> None:
        """Test deleteWaypoint delegates to flow function."""
        with patch(
            "meshtastic.mesh_interface_runtime.send_pipeline.deleteWaypoint"
        ) as mock_flow:
            mock_flow.return_value = MagicMock()
            send_pipeline.deleteWaypoint(
                waypoint_id=12345,
                destinationId=BROADCAST_ADDR,
            )

        mock_flow.assert_called_once()


class TestAddResponseHandler:
    """Tests for _add_response_handler method (lines 591-602)."""

    @pytest.mark.unit
    def test_add_response_handler_delegates(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test _add_response_handler delegates to request wait runtime."""
        callback = MagicMock()

        send_pipeline._add_response_handler(12345, callback, ackPermitted=True)

        mock_interface._request_wait_runtime.add_response_handler.assert_called_once_with(
            12345,
            callback,
            ack_permitted=True,
        )


class TestSendPacket:
    """Tests for _send_packet method (lines 604-697)."""

    @pytest.mark.unit
    def test_send_packet_broadcast(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test sending packet to broadcast address."""
        _ = mock_interface  # Required fixture, explicitly marked as used
        mesh_packet = mesh_pb2.MeshPacket()
        mesh_packet.id = 12345

        with patch.object(send_pipeline, "_send_to_radio"):
            result = send_pipeline._send_packet(
                mesh_packet,
                destinationId=BROADCAST_ADDR,
                wantAck=True,
            )

        assert result.to == BROADCAST_NUM
        assert result.want_ack is True

    @pytest.mark.unit
    def test_send_packet_local_addr(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test sending packet to local address."""
        mock_interface.myInfo.my_node_num = 12345
        mesh_packet = mesh_pb2.MeshPacket()

        with patch.object(send_pipeline, "_send_to_radio"):
            result = send_pipeline._send_packet(
                mesh_packet,
                destinationId=LOCAL_ADDR,
            )

        assert result.to == 12345

    @pytest.mark.unit
    def test_send_packet_int_destination(self, send_pipeline: SendPipeline) -> None:
        """Test sending packet to integer destination."""
        mesh_packet = mesh_pb2.MeshPacket()

        with patch.object(send_pipeline, "_send_to_radio"):
            result = send_pipeline._send_packet(
                mesh_packet,
                destinationId=67890,
            )

        assert result.to == 67890

    @pytest.mark.unit
    def test_send_packet_hex_string_destination(
        self, send_pipeline: SendPipeline
    ) -> None:
        """Test sending packet to hex string destination."""
        mesh_packet = mesh_pb2.MeshPacket()

        with patch.object(send_pipeline, "_send_to_radio"):
            result = send_pipeline._send_packet(
                mesh_packet,
                destinationId="!000109bf",  # hex for 68031
            )

        assert result.to == 0x109BF

    @pytest.mark.unit
    def test_send_packet_node_lookup_destination(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test sending packet to node ID that requires lookup."""
        # Use a node ID that doesn't match hex pattern (9 chars instead of 8)
        mock_interface.nodes = {"!abcdef123": {"num": 67890}}
        mesh_packet = mesh_pb2.MeshPacket()

        with patch.object(send_pipeline, "_send_to_radio"):
            result = send_pipeline._send_packet(
                mesh_packet,
                destinationId="!abcdef123",
            )

        assert result.to == 67890

    @pytest.mark.unit
    def test_send_packet_none_destination_raises(
        self, send_pipeline: SendPipeline
    ) -> None:
        """Test that None destination raises error."""
        mesh_packet = mesh_pb2.MeshPacket()

        with pytest.raises(Exception, match="Invalid destinationId:"):
            send_pipeline._send_packet(mesh_packet, destinationId=None)  # type: ignore[arg-type]

    @pytest.mark.unit
    def test_send_packet_node_not_found_raises(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test that unknown node ID raises error when DB available."""
        mock_interface.nodes = {}
        mesh_packet = mesh_pb2.MeshPacket()

        # Use a node ID that doesn't match the hex pattern (not 8 hex chars)
        with pytest.raises(Exception, match="not found"):
            send_pipeline._send_packet(mesh_packet, destinationId="!abc123")

    @pytest.mark.unit
    def test_send_packet_with_hop_limit(self, send_pipeline: SendPipeline) -> None:
        """Test sending packet with custom hop limit."""
        mesh_packet = mesh_pb2.MeshPacket()

        with patch.object(send_pipeline, "_send_to_radio"):
            result = send_pipeline._send_packet(
                mesh_packet,
                destinationId=BROADCAST_ADDR,
                hopLimit=5,
            )

        assert result.hop_limit == 5

    @pytest.mark.unit
    def test_send_packet_pki_encrypted(self, send_pipeline: SendPipeline) -> None:
        """Test sending PKI encrypted packet."""
        mesh_packet = mesh_pb2.MeshPacket()

        with patch.object(send_pipeline, "_send_to_radio"):
            result = send_pipeline._send_packet(
                mesh_packet,
                destinationId=BROADCAST_ADDR,
                pkiEncrypted=True,
            )

        assert result.pki_encrypted is True

    @pytest.mark.unit
    def test_send_packet_with_public_key(self, send_pipeline: SendPipeline) -> None:
        """Test sending packet with public key."""
        mesh_packet = mesh_pb2.MeshPacket()
        public_key = b"public_key_bytes"

        with patch.object(send_pipeline, "_send_to_radio"):
            result = send_pipeline._send_packet(
                mesh_packet,
                destinationId=BROADCAST_ADDR,
                publicKey=public_key,
            )

        assert result.public_key == public_key

    @pytest.mark.unit
    def test_send_packet_no_proto_skips_send(
        self,
        send_pipeline: SendPipeline,
        mock_interface: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test that sending is skipped when noProto is True."""
        mock_interface.noProto = True
        mesh_packet = mesh_pb2.MeshPacket()

        with caplog.at_level(logging.WARNING):
            with patch.object(send_pipeline, "_send_to_radio") as mock_send_to_radio:
                send_pipeline._send_packet(
                    mesh_packet,
                    destinationId=BROADCAST_ADDR,
                )

        mock_send_to_radio.assert_not_called()
        assert "noProto" in caplog.text


class TestSendPacketAlias:
    """Tests for _sendPacket alias (lines 699-716)."""

    @pytest.mark.unit
    def test_send_packet_alias(self, send_pipeline: SendPipeline) -> None:
        """Test that _sendPacket is an alias for _send_packet."""
        mesh_packet = mesh_pb2.MeshPacket()

        with patch.object(send_pipeline, "_send_packet") as mock_send:
            send_pipeline._sendPacket(
                mesh_packet,
                destinationId=BROADCAST_ADDR,
                wantAck=True,
                hopLimit=3,
            )

        mock_send.assert_called_once()


class TestWaitForConfig:
    """Tests for waitForConfig method (lines 718-727)."""

    @pytest.mark.unit
    def test_wait_for_config_success(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test successful wait for config including dedicated lora wait."""
        mock_interface._timeout.waitForSet.return_value = True
        mock_interface.localNode.waitForConfig.return_value = True
        mock_interface.localNode._channel_request_runtime._timeout_for_field.return_value = (
            True
        )

        send_pipeline.waitForConfig()

        mock_interface.localNode._channel_request_runtime._timeout_for_field.assert_called_once_with(
            "lora", LORA_CONFIG_WAIT_SECONDS
        )

    @pytest.mark.unit
    def test_wait_for_config_timeout_raises(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test that timeout raises error."""
        mock_interface._timeout.waitForSet.return_value = False
        mock_interface.localNode.waitForConfig.return_value = True

        with pytest.raises(Exception, match="Timed out"):
            send_pipeline.waitForConfig()

    @pytest.mark.unit
    def test_wait_for_config_lora_wait_failure_raises(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test failure path when the lora wait returns false and raises timeout error."""
        mock_interface._timeout.waitForSet.return_value = True
        mock_interface.localNode.waitForConfig.return_value = True
        mock_interface.localNode._channel_request_runtime._timeout_for_field.return_value = (
            False
        )

        with pytest.raises(
            mock_interface.MeshInterfaceError,
            match="Timed out waiting for interface config",
        ):
            send_pipeline.waitForConfig()

    @pytest.mark.unit
    def test_wait_for_config_lora_field_absent_graceful_success(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Graceful success when lora is absent from the protobuf descriptor.

        Replaces the mock _channel_request_runtime with a real
        _NodeChannelRequestRuntime wired to a mock localConfig whose DESCRIPTOR
        lacks the 'lora' field. This exercises the actual early-return path in
        _timeout_for_field rather than just stubbing it to True.
        """
        from meshtastic.node_runtime.channel_request_runtime import (
            _NodeChannelRequestRuntime,
        )

        mock_node = MagicMock()
        desc = MagicMock()
        desc.fields_by_name = {"bluetooth": MagicMock(), "device": MagicMock()}
        mock_node.localConfig = MagicMock()
        mock_node.localConfig.DESCRIPTOR = desc

        mock_norm = MagicMock()
        real_runtime = _NodeChannelRequestRuntime(
            mock_node, normalization_runtime=mock_norm
        )

        mock_interface._timeout.waitForSet.return_value = True
        mock_interface.localNode.waitForConfig.return_value = True
        mock_interface.localNode._channel_request_runtime = real_runtime

        send_pipeline.waitForConfig()


class TestWaitForAckNak:
    """Tests for waitForAckNak method (lines 729-736)."""

    @pytest.mark.unit
    def test_wait_for_ack_nak_success(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test successful wait for ACK/NAK."""
        mock_interface._timeout.waitForAckNak.return_value = True
        mock_interface._request_wait_runtime.raise_wait_error_if_present.side_effect = (
            None
        )

        # Should not raise
        send_pipeline.waitForAckNak()

    @pytest.mark.unit
    def test_wait_for_ack_nak_timeout_raises(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test that timeout raises error."""
        mock_interface._timeout.waitForAckNak.return_value = False
        mock_interface._request_wait_runtime.raise_wait_error_if_present.side_effect = (
            None
        )

        with pytest.raises(Exception, match="Timed out"):
            send_pipeline.waitForAckNak()


class TestWaitForTraceRoute:
    """Tests for waitForTraceRoute method (lines 738-761)."""

    @pytest.mark.unit
    def test_wait_for_traceroute_success(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test successful wait for traceroute."""
        mock_interface._timeout.waitForTraceRoute.return_value = True
        mock_interface._request_wait_runtime.raise_wait_error_if_present.side_effect = (
            None
        )
        mock_interface._request_wait_runtime.retire_wait_request.side_effect = None

        # Should not raise
        send_pipeline.waitForTraceRoute(waitFactor=1.0)

    @pytest.mark.unit
    def test_wait_for_traceroute_with_request_id(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test wait for traceroute with request ID."""
        mock_interface._request_wait_runtime.wait_for_request_ack.return_value = True
        mock_interface._request_wait_runtime.raise_wait_error_if_present.side_effect = (
            None
        )
        mock_interface._request_wait_runtime.retire_wait_request.side_effect = None

        # Should not raise
        send_pipeline.waitForTraceRoute(waitFactor=1.0, request_id=12345)


class TestWaitForTelemetry:
    """Tests for waitForTelemetry method (lines 763-782)."""

    @pytest.mark.unit
    def test_wait_for_telemetry_success(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test successful wait for telemetry."""
        mock_interface._timeout.waitForTelemetry.return_value = True
        mock_interface._request_wait_runtime.raise_wait_error_if_present.side_effect = (
            None
        )
        mock_interface._request_wait_runtime.retire_wait_request.side_effect = None

        # Should not raise
        send_pipeline.waitForTelemetry()

    @pytest.mark.unit
    def test_wait_for_telemetry_with_request_id(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test wait for telemetry with request ID."""
        mock_interface._request_wait_runtime.wait_for_request_ack.return_value = True
        mock_interface._request_wait_runtime.raise_wait_error_if_present.side_effect = (
            None
        )
        mock_interface._request_wait_runtime.retire_wait_request.side_effect = None

        # Should not raise
        send_pipeline.waitForTelemetry(request_id=12345)


class TestWaitForPosition:
    """Tests for waitForPosition method (lines 784-801)."""

    @pytest.mark.unit
    def test_wait_for_position_success(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test successful wait for position."""
        mock_interface._timeout.waitForPosition.return_value = True
        mock_interface._request_wait_runtime.raise_wait_error_if_present.side_effect = (
            None
        )
        mock_interface._request_wait_runtime.retire_wait_request.side_effect = None

        # Should not raise
        send_pipeline.waitForPosition()

    @pytest.mark.unit
    def test_wait_for_position_with_request_id(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test wait for position with request ID."""
        mock_interface._request_wait_runtime.wait_for_request_ack.return_value = True
        mock_interface._request_wait_runtime.raise_wait_error_if_present.side_effect = (
            None
        )
        mock_interface._request_wait_runtime.retire_wait_request.side_effect = None

        # Should not raise
        send_pipeline.waitForPosition(request_id=12345)


class TestWaitForWaypoint:
    """Tests for waitForWaypoint method (lines 803-820)."""

    @pytest.mark.unit
    def test_wait_for_waypoint_success(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test successful wait for waypoint."""
        mock_interface._timeout.waitForWaypoint.return_value = True
        mock_interface._request_wait_runtime.raise_wait_error_if_present.side_effect = (
            None
        )
        mock_interface._request_wait_runtime.retire_wait_request.side_effect = None

        # Should not raise
        send_pipeline.waitForWaypoint()

    @pytest.mark.unit
    def test_wait_for_waypoint_with_request_id(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test wait for waypoint with request ID."""
        mock_interface._request_wait_runtime.wait_for_request_ack.return_value = True
        mock_interface._request_wait_runtime.raise_wait_error_if_present.side_effect = (
            None
        )
        mock_interface._request_wait_runtime.retire_wait_request.side_effect = None

        # Should not raise
        send_pipeline.waitForWaypoint(request_id=12345)


class TestSendToRadio:
    """Tests for _send_to_radio method (lines 822-835)."""

    @pytest.mark.unit
    def test_send_to_radio_delegates(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test _send_to_radio delegates to queue send runtime."""
        to_radio = mesh_pb2.ToRadio()
        to_radio.packet.id = 12345

        send_pipeline._send_to_radio(to_radio)

        mock_interface._queue_send_runtime._send_to_radio.assert_called_once()

    @pytest.mark.unit
    def test_send_to_radio_no_proto_skips(
        self,
        send_pipeline: SendPipeline,
        mock_interface: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test that sending is skipped when noProto is True."""
        mock_interface.noProto = True
        to_radio = mesh_pb2.ToRadio()

        with caplog.at_level(logging.WARNING):
            send_pipeline._send_to_radio(to_radio)

        mock_interface._queue_send_runtime._send_to_radio.assert_not_called()
        assert "noProto" in caplog.text


class TestSendToRadioImpl:
    """Tests for _send_to_radio_impl method (lines 837-839)."""

    @pytest.mark.unit
    def test_send_to_radio_impl_delegates(
        self, send_pipeline: SendPipeline, mock_interface: MagicMock
    ) -> None:
        """Test _send_to_radio_impl delegates to interface."""
        to_radio = mesh_pb2.ToRadio()

        send_pipeline._send_to_radio_impl(to_radio)

        mock_interface._send_to_radio_impl.assert_called_once_with(to_radio)


class TestSendDisconnect:
    """Tests for _send_disconnect method (lines 841-845)."""

    @pytest.mark.unit
    def test_send_disconnect(self, send_pipeline: SendPipeline) -> None:
        """Test sending disconnect message."""
        with patch.object(send_pipeline, "_send_to_radio") as mock_send:
            send_pipeline._send_disconnect()

        mock_send.assert_called_once()
        call_args = mock_send.call_args[0][0]
        assert call_args.disconnect is True


class TestSendHeartbeat:
    """Tests for sendHeartbeat method (lines 847-851)."""

    @pytest.mark.unit
    def test_send_heartbeat(self, send_pipeline: SendPipeline) -> None:
        """Test sending heartbeat message."""
        with patch.object(send_pipeline, "_send_to_radio") as mock_send:
            send_pipeline.sendHeartbeat()

        mock_send.assert_called_once()
        # call_args[0][0] would be the ToRadio message with heartbeat field set
        # Heartbeat should have heartbeat field set
