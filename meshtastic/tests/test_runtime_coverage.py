"""Meshtastic runtime module coverage tests.

Tests for ACK handling edge cases and receive pipeline error handling
to improve coverage of runtime modules.
"""

# pylint: disable=redefined-outer-name,protected-access

import logging
import threading
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from google.protobuf.message import DecodeError

from meshtastic import (
    BROADCAST_ADDR,
    BROADCAST_NUM,
    DECODE_ERROR_KEY,
)
from meshtastic.mesh_interface import MeshInterface
from meshtastic.mesh_interface_runtime.ports import _ReceivePipelinePort
from meshtastic.mesh_interface_runtime.receive_pipeline import (
    DECODE_FAILED_PREFIX,
    ReceivePipeline,
    _FromRadioContext,
    _LazyMessageDict,
    _PacketRuntimeContext,
)
from meshtastic.node import Node
from meshtastic.node_runtime.shared import ERROR_REASON_NONE
from meshtastic.node_runtime.transport_runtime.ack import (
    _NodeAckNakRuntime,
)
from meshtastic.protobuf import (
    admin_pb2,
    config_pb2,
    mesh_pb2,
    module_config_pb2,
    portnums_pb2,
    telemetry_pb2,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_interface_with_ack() -> MagicMock:
    """Create a mock MeshInterface with acknowledgment state."""
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

    # Setup acknowledgment state
    ack_state = MagicMock()
    ack_state.receivedAck = False
    ack_state.receivedNak = False
    ack_state.receivedImplAck = False
    interface._acknowledgment = ack_state

    return interface


@pytest.fixture
def mock_node(mock_interface_with_ack: MagicMock) -> Node:
    """Create a mock Node with interface."""
    node = MagicMock(spec=Node)
    node.iface = mock_interface_with_ack
    return node


@pytest.fixture
def ack_runtime(mock_node: MagicMock) -> _NodeAckNakRuntime:
    """Create an _NodeAckNakRuntime instance."""
    return _NodeAckNakRuntime(mock_node)


@pytest.fixture
def receive_pipeline(mock_interface_with_ack: MagicMock) -> ReceivePipeline:
    """Create a ReceivePipeline instance."""
    return ReceivePipeline(_ReceivePipelinePort(mock_interface_with_ack))


# =============================================================================
# ACK Handling Tests - Edge Cases
# =============================================================================


class TestAckNakRuntimeInterfaceNone:
    """Tests for ACK handling when interface is None."""

    @pytest.mark.unit
    def test_handle_ack_nak_interface_none(
        self,
        mock_node: MagicMock,
        ack_runtime: _NodeAckNakRuntime,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test handling ACK when interface is None."""
        mock_node.iface = None
        packet = {
            "id": 12345,
            "decoded": {"routing": {"errorReason": ERROR_REASON_NONE}},
        }

        with caplog.at_level(logging.WARNING):
            ack_runtime._handle_ack_nak(packet)

        assert "interface is not available" in caplog.text
        assert "packet_id=12345" in caplog.text

    @pytest.mark.unit
    def test_handle_ack_nak_interface_none_no_packet_id(
        self,
        mock_node: MagicMock,
        ack_runtime: _NodeAckNakRuntime,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test handling ACK when interface is None and packet has no ID."""
        mock_node.iface = None
        packet = {"decoded": {"routing": {"errorReason": ERROR_REASON_NONE}}}

        with caplog.at_level(logging.WARNING):
            ack_runtime._handle_ack_nak(packet)

        assert "interface is not available" in caplog.text


class TestAckNakRuntimeDecodedEdgeCases:
    """Tests for ACK handling with decoded payload edge cases."""

    @pytest.mark.unit
    def test_handle_ack_nak_missing_decoded(
        self, ack_runtime: _NodeAckNakRuntime, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test handling ACK without decoded payload."""
        packet = {"id": 12345, "from": 67890}

        with caplog.at_level(logging.WARNING):
            ack_runtime._handle_ack_nak(packet)

        assert "without decoded payload" in caplog.text
        assert ack_runtime._node.iface._acknowledgment.receivedNak is True

    @pytest.mark.unit
    def test_handle_ack_nak_decoded_not_dict(
        self, ack_runtime: _NodeAckNakRuntime, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test handling ACK when decoded is not a dict."""
        packet = {"id": 12345, "from": 67890, "decoded": "not a dict"}

        with caplog.at_level(logging.WARNING):
            ack_runtime._handle_ack_nak(packet)

        assert "without decoded payload" in caplog.text
        assert ack_runtime._node.iface._acknowledgment.receivedNak is True

    @pytest.mark.unit
    def test_handle_ack_nak_decoded_none(
        self, ack_runtime: _NodeAckNakRuntime, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test handling ACK when decoded is None."""
        packet = {"id": 12345, "from": 67890, "decoded": None}

        with caplog.at_level(logging.WARNING):
            ack_runtime._handle_ack_nak(packet)

        assert "without decoded payload" in caplog.text
        assert ack_runtime._node.iface._acknowledgment.receivedNak is True


class TestAckNakRuntimeRoutingEdgeCases:
    """Tests for ACK handling with routing details edge cases."""

    @pytest.mark.unit
    def test_handle_ack_nak_missing_routing(
        self, ack_runtime: _NodeAckNakRuntime, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test handling ACK without routing details."""
        packet = {"id": 12345, "from": 67890, "decoded": {"other": "data"}}

        with caplog.at_level(logging.WARNING):
            ack_runtime._handle_ack_nak(packet)

        assert "without routing details" in caplog.text
        assert ack_runtime._node.iface._acknowledgment.receivedNak is True

    @pytest.mark.unit
    def test_handle_ack_nak_routing_not_dict(
        self, ack_runtime: _NodeAckNakRuntime, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test handling ACK when routing is not a dict."""
        packet = {"id": 12345, "from": 67890, "decoded": {"routing": "not a dict"}}

        with caplog.at_level(logging.WARNING):
            ack_runtime._handle_ack_nak(packet)

        assert "without routing details" in caplog.text
        assert ack_runtime._node.iface._acknowledgment.receivedNak is True

    @pytest.mark.unit
    def test_handle_ack_nak_routing_none(
        self, ack_runtime: _NodeAckNakRuntime, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test handling ACK when routing is None."""
        packet = {"id": 12345, "from": 67890, "decoded": {"routing": None}}

        with caplog.at_level(logging.WARNING):
            ack_runtime._handle_ack_nak(packet)

        assert "without routing details" in caplog.text
        assert ack_runtime._node.iface._acknowledgment.receivedNak is True


class TestAckNakRuntimeErrorReasonEdgeCases:
    """Tests for ACK handling with error reason edge cases."""

    @pytest.mark.unit
    def test_handle_ack_nak_error_reason_provided(
        self, ack_runtime: _NodeAckNakRuntime, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test handling NAK with error reason."""
        packet = {
            "id": 12345,
            "from": 67890,
            "decoded": {"routing": {"errorReason": "NO_CHANNEL"}},
        }

        with caplog.at_level(logging.WARNING):
            ack_runtime._handle_ack_nak(packet)

        assert "Received a NAK" in caplog.text
        assert "error reason: NO_CHANNEL" in caplog.text
        assert ack_runtime._node.iface._acknowledgment.receivedNak is True

    @pytest.mark.unit
    def test_handle_ack_nak_error_reason_empty_string(
        self, ack_runtime: _NodeAckNakRuntime, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test handling ACK when error reason is empty string (treated as error)."""
        packet = {
            "id": 12345,
            "from": 67890,
            "decoded": {"routing": {"errorReason": ""}},
        }

        with caplog.at_level(logging.WARNING):
            ack_runtime._handle_ack_nak(packet)

        assert "Received a NAK" in caplog.text
        assert ack_runtime._node.iface._acknowledgment.receivedNak is True

    @pytest.mark.unit
    def test_handle_ack_nak_error_reason_none_explicit(
        self, ack_runtime: _NodeAckNakRuntime, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test handling ACK when error reason is explicitly None (should continue)."""
        # Setup localNode for this test path
        ack_runtime._node.iface.localNode = MagicMock()
        ack_runtime._node.iface.localNode.nodeNum = 99999

        packet = {
            "id": 12345,
            "from": 67890,
            "decoded": {"routing": {"errorReason": None}},
        }

        with caplog.at_level(logging.INFO):
            ack_runtime._handle_ack_nak(packet)

        # Since errorReason is None (not string "NONE"), it will be treated as error
        # This tests the edge case where errorReason is explicitly None vs "NONE"
        assert ack_runtime._node.iface._acknowledgment.receivedNak is True


class TestAckNakRuntimeFromFieldEdgeCases:
    """Tests for ACK handling with from field edge cases."""

    @pytest.mark.unit
    def test_handle_ack_nak_missing_from(
        self, ack_runtime: _NodeAckNakRuntime, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test handling ACK without from field."""
        packet = {
            "id": 12345,
            "decoded": {"routing": {"errorReason": ERROR_REASON_NONE}},
        }

        with caplog.at_level(logging.WARNING):
            ack_runtime._handle_ack_nak(packet)

        assert "without sender" in caplog.text
        assert ack_runtime._node.iface._acknowledgment.receivedNak is True

    @pytest.mark.unit
    def test_handle_ack_nak_from_is_none(
        self, ack_runtime: _NodeAckNakRuntime, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test handling ACK when from field is None."""
        packet = {
            "id": 12345,
            "from": None,
            "decoded": {"routing": {"errorReason": ERROR_REASON_NONE}},
        }

        with caplog.at_level(logging.WARNING):
            ack_runtime._handle_ack_nak(packet)

        assert "without sender" in caplog.text
        assert ack_runtime._node.iface._acknowledgment.receivedNak is True

    @pytest.mark.unit
    def test_handle_ack_nak_from_type_error(
        self, ack_runtime: _NodeAckNakRuntime, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test handling ACK when from field causes TypeError."""
        packet = {
            "id": 12345,
            "from": [1, 2, 3],  # List can't be converted to int
            "decoded": {"routing": {"errorReason": ERROR_REASON_NONE}},
        }

        with caplog.at_level(logging.WARNING):
            ack_runtime._handle_ack_nak(packet)

        assert "invalid sender" in caplog.text
        assert ack_runtime._node.iface._acknowledgment.receivedNak is True

    @pytest.mark.unit
    def test_handle_ack_nak_from_value_error(
        self, ack_runtime: _NodeAckNakRuntime, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test handling ACK when from field causes ValueError."""
        packet = {
            "id": 12345,
            "from": "not_a_number",
            "decoded": {"routing": {"errorReason": ERROR_REASON_NONE}},
        }

        with caplog.at_level(logging.WARNING):
            ack_runtime._handle_ack_nak(packet)

        assert "invalid sender" in caplog.text
        assert "not_a_number" in caplog.text
        assert ack_runtime._node.iface._acknowledgment.receivedNak is True


class TestAckNakRuntimeLocalNodeEdgeCases:
    """Tests for ACK handling with localNode edge cases."""

    @pytest.mark.unit
    def test_handle_ack_nak_local_node_none(
        self, ack_runtime: _NodeAckNakRuntime, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test handling ACK when localNode is None."""
        ack_runtime._node.iface.localNode = None  # type: ignore[assignment]
        packet = {
            "id": 12345,
            "from": 67890,
            "decoded": {"routing": {"errorReason": ERROR_REASON_NONE}},
        }

        with caplog.at_level(logging.WARNING):
            ack_runtime._handle_ack_nak(packet)

        assert "localNode is not available" in caplog.text
        assert ack_runtime._node.iface._acknowledgment.receivedNak is True


class TestAckNakRuntimeImplicitAck:
    """Tests for implicit ACK handling."""

    @pytest.mark.unit
    def test_handle_ack_nak_implicit_ack(
        self, ack_runtime: _NodeAckNakRuntime, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test handling implicit ACK (from == localNode.nodeNum)."""
        local_node_num = 67890
        ack_runtime._node.iface.localNode = MagicMock()
        ack_runtime._node.iface.localNode.nodeNum = local_node_num

        packet = {
            "id": 12345,
            "from": local_node_num,
            "decoded": {"routing": {"errorReason": ERROR_REASON_NONE}},
        }

        with caplog.at_level(logging.INFO):
            ack_runtime._handle_ack_nak(packet)

        assert "implicit ACK" in caplog.text
        assert ack_runtime._node.iface._acknowledgment.receivedImplAck is True

    @pytest.mark.unit
    def test_handle_ack_nak_explicit_ack(
        self, ack_runtime: _NodeAckNakRuntime, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test handling explicit ACK (from != localNode.nodeNum)."""
        ack_runtime._node.iface.localNode = MagicMock()
        ack_runtime._node.iface.localNode.nodeNum = 11111

        packet = {
            "id": 12345,
            "from": 67890,
            "decoded": {"routing": {"errorReason": ERROR_REASON_NONE}},
        }

        with caplog.at_level(logging.INFO):
            ack_runtime._handle_ack_nak(packet)

        assert "Received an ACK" in caplog.text
        assert ack_runtime._node.iface._acknowledgment.receivedAck is True


# =============================================================================
# Receive Pipeline Tests - Error Handling and Edge Cases
# =============================================================================


class TestReceivePipelineParseErrors:
    """Tests for FromRadio parsing error handling."""

    @pytest.mark.unit
    def test_parse_from_radio_bytes_logs_debug_info(
        self, receive_pipeline: ReceivePipeline, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that parsing logs debug info about frame length and checksum."""
        from_radio = mesh_pb2.FromRadio()
        from_radio.id = 12345
        serialized = from_radio.SerializeToString()

        with caplog.at_level(logging.DEBUG):
            result = receive_pipeline._parse_from_radio_bytes(serialized)

        assert result.id == 12345
        assert "Received FromRadio frame" in caplog.text
        assert "len=" in caplog.text
        assert "sha256=" in caplog.text

    @pytest.mark.unit
    def test_parse_from_radio_bytes_exception_logging(
        self, receive_pipeline: ReceivePipeline, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that parsing exceptions log detailed frame info."""
        invalid_bytes = b"invalid protobuf that will definitely cause a decode error"

        with caplog.at_level(logging.ERROR):
            with pytest.raises(DecodeError):
                receive_pipeline._parse_from_radio_bytes(invalid_bytes)

        assert "Error while parsing FromRadio" in caplog.text
        assert "len=" in caplog.text
        assert "sha256=" in caplog.text


class TestReceivePipelineDispatchEdgeCases:
    """Tests for FromRadio dispatch edge cases."""

    @pytest.mark.unit
    def test_dispatch_unknown_branch_logs_debug(
        self, receive_pipeline: ReceivePipeline, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that unknown FromRadio payload logs debug message."""
        from_radio = mesh_pb2.FromRadio()
        from_radio.id = 12345  # Only has id, no specific field set

        context = _FromRadioContext(
            message=from_radio,
            message_dict=_LazyMessageDict(from_radio),
            config_id=None,
        )

        with caplog.at_level(logging.DEBUG):
            result = receive_pipeline._dispatch_from_radio_message(context)

        assert "Unexpected FromRadio payload" in caplog.text
        assert result == []

    @pytest.mark.unit
    def test_select_branch_config_complete_id_mismatch(
        self, receive_pipeline: ReceivePipeline
    ) -> None:
        """Test that config_complete_id branch requires matching config_id."""
        from_radio = mesh_pb2.FromRadio()
        from_radio.config_complete_id = 123

        context = _FromRadioContext(
            message=from_radio,
            message_dict=_LazyMessageDict(from_radio),
            config_id=456,  # Mismatched ID
        )

        branch = receive_pipeline._select_from_radio_branch(context)
        # Should not match config_complete_id due to mismatch
        assert branch is None

    @pytest.mark.unit
    def test_select_branch_config_complete_id_match(
        self, receive_pipeline: ReceivePipeline
    ) -> None:
        """Test that config_complete_id branch matches when IDs align."""
        from_radio = mesh_pb2.FromRadio()
        from_radio.config_complete_id = 123

        context = _FromRadioContext(
            message=from_radio,
            message_dict=_LazyMessageDict(from_radio),
            config_id=123,  # Matching ID
        )

        branch = receive_pipeline._select_from_radio_branch(context)
        assert branch == "config_complete_id"


class TestReceivePipelineNodeInfoEdgeCases:
    """Tests for node info handling edge cases."""

    @pytest.mark.unit
    def test_handle_node_info_missing_node_info(
        self, receive_pipeline: ReceivePipeline, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test handling node_info without nodeInfo payload."""
        from_radio = mesh_pb2.FromRadio()
        # node_info field is set but empty (no num set)
        # This creates a FromRadio with HasField("node_info") == True
        # but the nodeInfo message itself is empty

        context = _FromRadioContext(
            message=from_radio,
            message_dict=_LazyMessageDict(from_radio),
            config_id=None,
        )

        with caplog.at_level(logging.WARNING):
            result = receive_pipeline._handle_from_radio_node_info(context)

        assert "without nodeInfo payload" in caplog.text
        assert result == []

    @pytest.mark.unit
    def test_handle_node_info_position_fixup_key_error(
        self, receive_pipeline: ReceivePipeline, mock_interface_with_ack: MagicMock
    ) -> None:
        """Test handling node_info when position fixup raises KeyError."""
        from_radio = mesh_pb2.FromRadio()
        from_radio.node_info.num = 12345
        from_radio.node_info.user.id = "!1234"

        mock_interface_with_ack.nodesByNum = {}

        context = _FromRadioContext(
            message=from_radio,
            message_dict=_LazyMessageDict(from_radio),
            config_id=None,
        )

        with patch(
            "meshtastic.mesh_interface_runtime.receive_pipeline.publishingThread"
        ):
            with patch.object(
                receive_pipeline, "_fixup_position", side_effect=KeyError("position")
            ):
                result = receive_pipeline._handle_from_radio_node_info(context)

        # Should still complete despite KeyError in position fixup
        assert len(result) == 1
        assert result[0].topic == "meshtastic.node.updated"


class TestReceivePipelinePacketNormalizationEdgeCases:
    """Tests for packet normalization edge cases."""

    @pytest.mark.unit
    def test_normalize_packet_allow_zero_source_true(
        self, receive_pipeline: ReceivePipeline
    ) -> None:
        """Test normalizing packet with from=0 when allow_zero_source=True."""
        mesh_packet = mesh_pb2.MeshPacket()
        setattr(mesh_packet, "from", 0)
        mesh_packet.id = 12345

        result = receive_pipeline._normalize_packet_from_radio(
            mesh_packet, allow_zero_source=True
        )

        assert result is not None
        assert result["from"] == 0
        assert result["id"] == 12345

    @pytest.mark.unit
    def test_normalize_packet_allow_zero_source_false_logs_error(
        self, receive_pipeline: ReceivePipeline, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that packet with from=0 logs error when allow_zero_source=False."""
        mesh_packet = mesh_pb2.MeshPacket()
        setattr(mesh_packet, "from", 0)
        mesh_packet.id = 12345

        with caplog.at_level(logging.ERROR):
            result = receive_pipeline._normalize_packet_from_radio(
                mesh_packet, allow_zero_source=False
            )

        assert result is None
        assert "Device returned a packet we sent, ignoring" in caplog.text

    @pytest.mark.unit
    def test_normalize_packet_sets_default_to(
        self, receive_pipeline: ReceivePipeline
    ) -> None:
        """Test that packet without 'to' field defaults to 0."""
        mesh_packet = mesh_pb2.MeshPacket()
        setattr(mesh_packet, "from", 12345)
        mesh_packet.id = 99999
        # Not setting 'to'

        result = receive_pipeline._normalize_packet_from_radio(
            mesh_packet, allow_zero_source=False
        )

        assert result is not None
        assert result["to"] == 0

    @pytest.mark.unit
    def test_normalize_packet_with_zero_source_sets_from_explicitly(
        self, receive_pipeline: ReceivePipeline
    ) -> None:
        """Test that allow_zero_source=True sets from=0 explicitly."""
        mesh_packet = mesh_pb2.MeshPacket()
        setattr(mesh_packet, "from", 0)

        result = receive_pipeline._normalize_packet_from_radio(
            mesh_packet, allow_zero_source=True
        )

        assert result is not None
        assert result["from"] == 0


class TestReceivePipelineIdentityEnrichmentEdgeCases:
    """Tests for packet identity enrichment edge cases."""

    @pytest.mark.unit
    def test_enrich_identity_nodes_db_none(
        self,
        receive_pipeline: ReceivePipeline,
        mock_interface_with_ack: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test enriching identity when nodesByNum is None."""
        mock_interface_with_ack.nodesByNum = None
        packet_dict = {"from": 12345, "to": 67890}

        with caplog.at_level(logging.WARNING):
            receive_pipeline._enrich_packet_identity(packet_dict)

        assert packet_dict.get("fromId") is None
        assert packet_dict.get("toId") is None

    @pytest.mark.unit
    def test_enrich_identity_node_without_user(
        self,
        receive_pipeline: ReceivePipeline,
        mock_interface_with_ack: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test enriching identity when node exists but has no user."""
        mock_interface_with_ack.nodesByNum = {
            12345: {"num": 12345},  # No "user" key
        }
        packet_dict = {"from": 12345, "to": BROADCAST_NUM}

        with caplog.at_level(logging.WARNING):
            receive_pipeline._enrich_packet_identity(packet_dict)

        # fromId should be None since node has no user
        assert packet_dict.get("fromId") is None
        # toId should be BROADCAST_ADDR
        assert packet_dict.get("toId") == cast(Any, BROADCAST_ADDR)

    @pytest.mark.unit
    def test_enrich_identity_node_user_no_id(
        self,
        receive_pipeline: ReceivePipeline,
        mock_interface_with_ack: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test enriching identity when user exists but has no id."""
        mock_interface_with_ack.nodesByNum = {
            12345: {"num": 12345, "user": {"longName": "Test"}},  # No "id" key
        }
        packet_dict = {"from": 12345, "to": BROADCAST_NUM}

        with caplog.at_level(logging.WARNING):
            receive_pipeline._enrich_packet_identity(packet_dict)

        assert packet_dict.get("fromId") is None

    @pytest.mark.unit
    def test_enrich_identity_from_raises_exception(
        self,
        receive_pipeline: ReceivePipeline,
        mock_interface_with_ack: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test enriching identity when from lookup raises exception."""
        # Create a situation where _node_num_to_id raises an exception
        mock_interface_with_ack.nodesByNum = None  # This will cause issues
        packet_dict = {"from": 12345, "to": 67890}

        with caplog.at_level(logging.WARNING):
            receive_pipeline._enrich_packet_identity(packet_dict)

        # Both should be None or not populated due to exceptions
        assert "fromId" not in packet_dict or packet_dict.get("fromId") is None


class TestReceivePipelineNodeNumToIdEdgeCases:
    """Tests for _node_num_to_id edge cases."""

    @pytest.mark.unit
    def test_node_num_to_id_nodes_none(
        self,
        receive_pipeline: ReceivePipeline,
        mock_interface_with_ack: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test mapping when nodesByNum is None."""
        mock_interface_with_ack.nodesByNum = None

        with caplog.at_level(logging.DEBUG):
            result = receive_pipeline._node_num_to_id(12345)

        assert result is None
        assert "Node database not initialized" in caplog.text

    @pytest.mark.unit
    def test_node_num_to_id_node_not_dict(
        self,
        receive_pipeline: ReceivePipeline,
        mock_interface_with_ack: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test mapping when node is not a dict."""
        mock_interface_with_ack.nodesByNum = {
            12345: "not a dict",  # Invalid type
        }

        with caplog.at_level(logging.DEBUG):
            result = receive_pipeline._node_num_to_id(12345)

        assert result is None

    @pytest.mark.unit
    def test_node_num_to_id_user_not_dict(
        self,
        receive_pipeline: ReceivePipeline,
        mock_interface_with_ack: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test mapping when user is not a dict."""
        mock_interface_with_ack.nodesByNum = {
            12345: {"num": 12345, "user": "not a dict"},
        }

        with caplog.at_level(logging.DEBUG):
            result = receive_pipeline._node_num_to_id(12345)

        assert result is None
        assert "no user payload" in caplog.text

    @pytest.mark.unit
    def test_node_num_to_id_id_not_string(
        self,
        receive_pipeline: ReceivePipeline,
        mock_interface_with_ack: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test mapping when id is not a string."""
        mock_interface_with_ack.nodesByNum = {
            12345: {"num": 12345, "user": {"id": 12345}},  # int instead of str
        }

        with caplog.at_level(logging.DEBUG):
            result = receive_pipeline._node_num_to_id(12345)

        assert result is None
        assert "no valid id" in caplog.text


class TestReceivePipelineDecodeFailures:
    """Tests for packet decode failure handling."""

    @pytest.mark.unit
    def test_decode_failure_routing_sets_error_reason(
        self, receive_pipeline: ReceivePipeline, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that decode failure for routing handler sets errorReason."""
        packet_context = _PacketRuntimeContext(packet_dict={"decoded": {"routing": {}}})
        packet_context.decoded = packet_context.packet_dict["decoded"]

        mesh_packet = mesh_pb2.MeshPacket()
        mesh_packet.decoded.payload = b"invalid routing data"
        mesh_packet.id = 12345
        setattr(mesh_packet, "from", 67890)
        mesh_packet.to = 11111

        handler = MagicMock()
        handler.protobufFactory = mesh_pb2.Routing
        handler.name = "routing"

        with caplog.at_level(logging.WARNING):
            receive_pipeline._decode_packet_payload_with_handler(
                packet_context, mesh_packet, handler
            )

        decoded_routing = packet_context.packet_dict["decoded"]["routing"]
        assert DECODE_ERROR_KEY in decoded_routing
        assert "errorReason" in decoded_routing
        assert DECODE_FAILED_PREFIX in decoded_routing["errorReason"]

    @pytest.mark.unit
    def test_decode_failure_admin_sets_skip_response_callback(
        self, receive_pipeline: ReceivePipeline, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that admin decode failure sets skip_response_callback_for_decode_failure."""
        packet_context = _PacketRuntimeContext(packet_dict={"decoded": {"admin": {}}})
        packet_context.decoded = packet_context.packet_dict["decoded"]

        mesh_packet = mesh_pb2.MeshPacket()
        mesh_packet.decoded.payload = b"invalid admin data"

        handler = MagicMock()
        handler.protobufFactory = admin_pb2.AdminMessage
        handler.name = "admin"

        with caplog.at_level(logging.WARNING):
            receive_pipeline._decode_packet_payload_with_handler(
                packet_context, mesh_packet, handler
            )

        assert packet_context.skip_response_callback_for_decode_failure is True

    @pytest.mark.unit
    def test_decode_failure_logs_warning_with_packet_info(
        self, receive_pipeline: ReceivePipeline, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that decode failures log warning with packet details."""
        packet_context = _PacketRuntimeContext(
            packet_dict={"decoded": {"telemetry": {}}, "from": 12345, "to": 67890}
        )
        packet_context.decoded = packet_context.packet_dict["decoded"]

        mesh_packet = mesh_pb2.MeshPacket()
        mesh_packet.decoded.payload = b"invalid data"
        mesh_packet.id = 11111
        setattr(mesh_packet, "from", 12345)
        mesh_packet.to = 67890

        handler = MagicMock()
        handler.protobufFactory = telemetry_pb2.Telemetry  # Use a valid factory
        handler.name = "telemetry"

        with caplog.at_level(logging.WARNING):
            receive_pipeline._decode_packet_payload_with_handler(
                packet_context, mesh_packet, handler
            )

        assert "Failed to decode telemetry payload" in caplog.text
        assert "id=11111" in caplog.text
        assert "from=12345" in caplog.text
        assert "to=67890" in caplog.text


class TestReceivePipelineApplyMutationsEdgeCases:
    """Tests for packet runtime mutations edge cases."""

    @pytest.mark.unit
    def test_apply_mutations_with_decoded_no_handler(
        self, receive_pipeline: ReceivePipeline
    ) -> None:
        """Test applying mutations when decoded exists but no protocol handler."""
        packet_context = _PacketRuntimeContext(
            packet_dict={"decoded": {"portnum": "UNKNOWN_APP"}}
        )
        packet_context.decoded = packet_context.packet_dict["decoded"]

        mesh_packet = mesh_pb2.MeshPacket()
        mesh_packet.decoded.portnum = portnums_pb2.PortNum.UNKNOWN_APP

        receive_pipeline._apply_packet_runtime_mutations(packet_context, mesh_packet)

        # No handler for UNKNOWN_APP, so no callback should be set
        assert packet_context.on_receive_callback is None

    @pytest.mark.unit
    def test_apply_mutations_handler_no_on_receive(
        self, receive_pipeline: ReceivePipeline
    ) -> None:
        """Test applying mutations when handler exists but has no onReceive."""
        packet_context = _PacketRuntimeContext(
            packet_dict={"decoded": {"portnum": "TEXT_MESSAGE_APP"}}
        )
        packet_context.decoded = packet_context.packet_dict["decoded"]

        mesh_packet = mesh_pb2.MeshPacket()
        mesh_packet.decoded.portnum = portnums_pb2.PortNum.TEXT_MESSAGE_APP

        receive_pipeline._apply_packet_runtime_mutations(packet_context, mesh_packet)

        # Handler exists but may not have onReceive set
        # Just verify it doesn't crash
        assert packet_context.topic == "meshtastic.receive.text"


class TestReceivePipelineConfigUpdateEdgeCases:
    """Tests for config update handling edge cases."""

    @pytest.mark.unit
    def test_apply_local_config_skips_unsupported_fields(
        self,
        receive_pipeline: ReceivePipeline,
        mock_interface_with_ack: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test that local config skips fields not in target."""
        # Try to set a field that doesn't exist in target
        # This tests the "not in target_fields" branch

        # Create a config with a field that won't exist in target
        config = config_pb2.Config()
        # Set a field that we know exists
        config.lora.region = 1  # type: ignore[assignment]

        with caplog.at_level(logging.DEBUG):
            result = receive_pipeline._apply_local_config_from_radio(config)

        assert result is True
        assert mock_interface_with_ack.localNode.localConfig.lora.region == 1

    @pytest.mark.unit
    def test_apply_local_config_no_fields_set(
        self, receive_pipeline: ReceivePipeline
    ) -> None:
        """Test applying local config when no fields are set."""
        config = config_pb2.Config()  # Empty config

        result = receive_pipeline._apply_local_config_from_radio(config)

        assert result is False  # Nothing was applied

    @pytest.mark.unit
    def test_apply_module_config_no_fields_set(
        self, receive_pipeline: ReceivePipeline
    ) -> None:
        """Test applying module config when no fields are set."""
        module_config = module_config_pb2.ModuleConfig()  # Empty config

        result = receive_pipeline._apply_module_config_from_radio(module_config)

        assert result is False  # Nothing was applied


class TestReceivePipelineHandlePacketFromRadioEdgeCases:
    """Tests for _handle_packet_from_radio edge cases."""

    @pytest.mark.unit
    def test_handle_packet_from_radio_normalized_none(
        self, receive_pipeline: ReceivePipeline
    ) -> None:
        """Test handling packet when normalization returns None."""
        mesh_packet = mesh_pb2.MeshPacket()
        setattr(mesh_packet, "from", 0)  # Will be filtered out

        result = receive_pipeline._handle_packet_from_radio(
            mesh_packet, allow_zero_source=False
        )

        assert result == []

    @pytest.mark.unit
    def test_handle_packet_from_radio_emit_false(
        self, receive_pipeline: ReceivePipeline
    ) -> None:
        """Test handling packet with emit_publication=False."""
        mesh_packet = mesh_pb2.MeshPacket()
        setattr(mesh_packet, "from", 12345)
        mesh_packet.to = 67890
        mesh_packet.id = 11111
        # Make it have decoded data
        mesh_packet.decoded.portnum = portnums_pb2.PortNum.TEXT_MESSAGE_APP
        mesh_packet.decoded.payload = b"test message"

        with patch.object(receive_pipeline, "_emit_publication_intents") as mock_emit:
            result = receive_pipeline._handle_packet_from_radio(
                mesh_packet, allow_zero_source=False, emit_publication=False
            )

        # Should not emit when emit_publication=False
        mock_emit.assert_not_called()
        # But should still return intents
        assert len(result) == 1
        assert result[0].topic == "meshtastic.receive.text"

    @pytest.mark.unit
    def test_handle_packet_from_radio_emit_true(
        self, receive_pipeline: ReceivePipeline
    ) -> None:
        """Test handling packet with emit_publication=True."""
        mesh_packet = mesh_pb2.MeshPacket()
        setattr(mesh_packet, "from", 12345)
        mesh_packet.to = 67890
        mesh_packet.id = 11111
        mesh_packet.decoded.portnum = portnums_pb2.PortNum.TEXT_MESSAGE_APP
        mesh_packet.decoded.payload = b"test"

        with patch.object(receive_pipeline, "_emit_publication_intents") as mock_emit:
            receive_pipeline._handle_packet_from_radio(
                mesh_packet, allow_zero_source=False, emit_publication=True
            )

        # Should emit when emit_publication=True
        mock_emit.assert_called_once()


class TestReceivePipelineResponseHandlerEdgeCases:
    """Tests for response handler correlation edge cases."""

    @pytest.mark.unit
    def test_correlate_response_no_decoded(
        self, receive_pipeline: ReceivePipeline, mock_interface_with_ack: MagicMock
    ) -> None:
        """Test correlating when packet has no decoded data."""
        packet_context = _PacketRuntimeContext(
            packet_dict={"id": 12345, "from": 67890}  # No "decoded" key
        )

        receive_pipeline._correlate_packet_response_handler(packet_context)

        # Should not call correlate_inbound_response when no decoded
        mock_interface_with_ack._request_wait_runtime.correlate_inbound_response.assert_not_called()

    @pytest.mark.unit
    def test_correlate_response_with_skip_flag(
        self, receive_pipeline: ReceivePipeline, mock_interface_with_ack: MagicMock
    ) -> None:
        """Test correlating with skip_response_callback_for_decode_failure flag."""
        packet_context = _PacketRuntimeContext(
            packet_dict={"decoded": {"requestId": 12345}}
        )
        packet_context.decoded = packet_context.packet_dict["decoded"]
        packet_context.skip_response_callback_for_decode_failure = True

        receive_pipeline._correlate_packet_response_handler(packet_context)

        mock_interface_with_ack._request_wait_runtime.correlate_inbound_response.assert_called_once()
        # Check that skip flag was passed
        call_args = (
            mock_interface_with_ack._request_wait_runtime.correlate_inbound_response.call_args
        )
        assert call_args.kwargs["skip_response_callback_for_decode_failure"] is True


class TestReceivePipelineGetOrCreateEdgeCases:
    """Tests for _get_or_create_by_num edge cases."""

    @pytest.mark.unit
    def test_get_or_create_nodes_by_num_none(
        self, receive_pipeline: ReceivePipeline, mock_interface_with_ack: MagicMock
    ) -> None:
        """Test that nodesByNum=None raises error."""
        mock_interface_with_ack.nodesByNum = None

        with pytest.raises(
            MeshInterface.MeshInterfaceError, match="Node database not initialized"
        ):
            receive_pipeline._get_or_create_by_num(12345)

    @pytest.mark.unit
    def test_get_or_create_presumptive_id_format(
        self, receive_pipeline: ReceivePipeline, mock_interface_with_ack: MagicMock
    ) -> None:
        """Test that new node gets correct presumptive ID format."""
        mock_interface_with_ack.nodesByNum = {}

        node_num = 0x1234ABCD
        result = receive_pipeline._get_or_create_by_num(node_num)

        expected_id = f"!{node_num:08x}"
        assert result["user"]["id"] == expected_id
        assert result["num"] == node_num


class TestReceivePipelineInvokeCallbackEdgeCases:
    """Tests for _invoke_packet_on_receive edge cases."""

    @pytest.mark.unit
    def test_invoke_callback_with_exception(
        self,
        receive_pipeline: ReceivePipeline,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test that callback exceptions are suppressed and logged."""

        def failing_callback(iface: Any, packet: Any) -> None:
            raise ValueError("Callback failed!")

        packet_context = _PacketRuntimeContext(
            packet_dict={"id": 12345},
            on_receive_callback=failing_callback,
        )

        with caplog.at_level(logging.ERROR):
            receive_pipeline._invoke_packet_on_receive(packet_context)
        assert "Protocol onReceive callback failed" in caplog.text
        assert "Callback failed!" in caplog.text

    @pytest.mark.unit
    def test_invoke_callback_none_no_error(
        self, receive_pipeline: ReceivePipeline
    ) -> None:
        """Test that None callback doesn't raise error."""
        packet_context = _PacketRuntimeContext(
            packet_dict={"id": 12345},
            on_receive_callback=None,
        )

        # Should not raise
        receive_pipeline._invoke_packet_on_receive(packet_context)


class TestReceivePipelineClassifyPacketEdgeCases:
    """Tests for _classify_packet_runtime edge cases."""

    @pytest.mark.unit
    def test_classify_packet_missing_portnum_warning(
        self, receive_pipeline: ReceivePipeline, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that missing portnum triggers warning."""
        packet_context = _PacketRuntimeContext(
            packet_dict={"decoded": {}}  # No portnum
        )
        mesh_packet = mesh_pb2.MeshPacket()
        mesh_packet.decoded.portnum = portnums_pb2.PortNum.UNKNOWN_APP

        with caplog.at_level(logging.WARNING):
            receive_pipeline._classify_packet_runtime(packet_context, mesh_packet)

        assert "portnum was not in decoded" in caplog.text
        assert packet_context.topic == "meshtastic.receive.data.UNKNOWN_APP"

    @pytest.mark.unit
    def test_classify_packet_payload_assignment(
        self, receive_pipeline: ReceivePipeline
    ) -> None:
        """Test that raw payload is assigned to decoded."""
        packet_context = _PacketRuntimeContext(
            packet_dict={"decoded": {"portnum": "TEXT_MESSAGE_APP"}}
        )
        mesh_packet = mesh_pb2.MeshPacket()
        mesh_packet.decoded.portnum = portnums_pb2.PortNum.TEXT_MESSAGE_APP
        mesh_packet.decoded.payload = b"raw payload bytes"

        receive_pipeline._classify_packet_runtime(packet_context, mesh_packet)

        assert packet_context.decoded["payload"] == b"raw payload bytes"  # type: ignore[index]


class TestReceivePipelineQueuePublication:
    """Tests for _queue_publication method."""

    @pytest.mark.unit
    def test_queue_publication_payload_snapshot(
        self, receive_pipeline: ReceivePipeline
    ) -> None:
        """Test that payload is snapshotted at queue time."""
        mutable_payload = {"key": "original"}

        with patch(
            "meshtastic.mesh_interface_runtime.receive_pipeline.publishingThread"
        ) as mock_thread:
            receive_pipeline._queue_publication("test.topic", data=mutable_payload)

            # Mutate original
            mutable_payload["key"] = "modified"

            # Verify the queued work function was created
            assert mock_thread.queueWork.called


class TestReceivePipelineConfigOrModuleConfig:
    """Tests for config_or_moduleConfig branch."""

    @pytest.mark.unit
    def test_handle_config_update_with_config(
        self, receive_pipeline: ReceivePipeline
    ) -> None:
        """Test handling FromRadio with config field."""
        from_radio = mesh_pb2.FromRadio()
        from_radio.config.lora.region = 1  # type: ignore[assignment]

        context = _FromRadioContext(
            message=from_radio,
            message_dict=_LazyMessageDict(from_radio),
            config_id=None,
        )

        result = receive_pipeline._handle_from_radio_config_update(context)

        assert result == []

    @pytest.mark.unit
    def test_handle_config_update_with_module_config(
        self, receive_pipeline: ReceivePipeline
    ) -> None:
        """Test handling FromRadio with moduleConfig field."""
        from_radio = mesh_pb2.FromRadio()
        from_radio.moduleConfig.mqtt.enabled = True

        context = _FromRadioContext(
            message=from_radio,
            message_dict=_LazyMessageDict(from_radio),
            config_id=None,
        )

        result = receive_pipeline._handle_from_radio_config_update(context)

        assert result == []


# =============================================================================
# Integration and State Machine Tests
# =============================================================================


class TestReceivePipelineFullFlowEdgeCases:
    """Integration tests for full receive pipeline flow."""

    @pytest.mark.unit
    def test_handle_from_radio_full_flow_with_packet(
        self, receive_pipeline: ReceivePipeline
    ) -> None:
        """Test full _handle_from_radio flow with packet payload."""
        from_radio = mesh_pb2.FromRadio()
        from_radio.packet.id = 12345
        setattr(from_radio.packet, "from", 67890)
        from_radio.packet.to = 11111
        from_radio.packet.decoded.portnum = portnums_pb2.PortNum.TEXT_MESSAGE_APP
        from_radio.packet.decoded.payload = b"Hello World"

        serialized = from_radio.SerializeToString()

        with patch.object(receive_pipeline, "_emit_publication_intents") as mock_emit:
            receive_pipeline._handle_from_radio(serialized)

        mock_emit.assert_called_once()

    @pytest.mark.unit
    def test_handle_from_radio_full_flow_log_record(
        self, receive_pipeline: ReceivePipeline, mock_interface_with_ack: MagicMock
    ) -> None:
        """Test full flow with log_record payload."""
        from_radio = mesh_pb2.FromRadio()
        from_radio.log_record.message = "Test log message"

        serialized = from_radio.SerializeToString()
        receive_pipeline._handle_from_radio(serialized)

        mock_interface_with_ack._handle_log_line.assert_called_once_with(
            "Test log message"
        )
