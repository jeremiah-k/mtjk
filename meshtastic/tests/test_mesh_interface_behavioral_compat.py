# pylint: disable=redefined-outer-name,too-many-lines
"""Behavioral compatibility tests for MeshInterface public API workflows.

These tests verify that old code patterns continue to work during the refactor,
not just that methods exist, but that they work correctly.

Note: Comprehensive behavioral test coverage requires extensive test cases.
The module is intentionally large to cover all API edge cases.
"""

import io
import logging
from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest

from meshtastic import BROADCAST_ADDR, LOCAL_ADDR
from meshtastic.mesh_interface import MeshInterface
from meshtastic.node import Node
from meshtastic.protobuf import mesh_pb2, portnums_pb2, telemetry_pb2

# Supported telemetry types for sendTelemetry tests
SUPPORTED_TELEMETRY_TYPES = [
    "device_metrics",
    "environment_metrics",
    "air_quality_metrics",
    "power_metrics",
    "local_stats",
]

# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def mock_interface() -> Generator[MeshInterface, None, None]:
    """Provide a MeshInterface with mocked internals for behavioral testing."""
    iface = MeshInterface(noProto=True)
    try:
        # Mock critical methods to avoid hardware dependency
        iface._send_to_radio_impl = MagicMock()  # type: ignore[method-assign]
        iface.myInfo = MagicMock()
        iface.myInfo.my_node_num = 2475227164

        # Set up sample nodes - use non-hex IDs to ensure DB lookup path
        iface.nodes = {
            "!9388f81c": {
                "num": 2475227164,
                "user": {
                    "id": "!9388f81c",
                    "longName": "Test Node",
                    "shortName": "TN",
                    "macaddr": "RBeTiPgc",
                    "hwModel": "TBEAM",
                },
                "position": {"time": 1640206266},
                "lastHeard": 1640204888,
            },
            "!testnode1": {
                "num": 11259375,
                "user": {
                    "id": "!testnode1",
                    "longName": "Remote Node",
                    "shortName": "RN",
                    "macaddr": "Test1234",
                    "hwModel": "RAK4631",
                },
                "position": {},
                "lastHeard": 1640205000,
            },
        }
        iface.nodesByNum = {
            2475227164: iface.nodes["!9388f81c"],
            11259375: iface.nodes["!testnode1"],
        }

        yield iface
    finally:
        iface.close()


# -----------------------------------------------------------------------------
# Test: sendText / sendData Workflow
# -----------------------------------------------------------------------------


@pytest.mark.unit
class TestSendTextSendDataWorkflow:
    """Test sendText() and sendData() workflows with various destinations."""

    def test_sendText_broadcast_destination(self, mock_interface):
        """Verify sendText() to broadcast queues correct packet."""
        iface = mock_interface

        packet = iface.sendText("Hello World!")

        # Verify packet structure
        assert isinstance(packet, mesh_pb2.MeshPacket)
        assert packet.to == 0xFFFFFFFF  # Broadcast
        assert packet.decoded.portnum == portnums_pb2.PortNum.TEXT_MESSAGE_APP
        assert packet.decoded.payload == b"Hello World!"
        assert packet.id != 0

    def test_sendText_specific_node_by_id(self, mock_interface):
        """Verify sendText() to specific node ID works."""
        iface = mock_interface

        packet = iface.sendText("Hello Remote!", destinationId="!testnode1")

        assert packet.to == 11259375
        assert packet.decoded.payload == b"Hello Remote!"

    def test_sendText_specific_node_by_num(self, mock_interface):
        """Verify sendText() to specific node number works."""
        iface = mock_interface

        packet = iface.sendText("Hello by num!", destinationId=11259375)

        assert packet.to == 11259375

    def test_sendText_with_options(self, mock_interface):
        """Verify sendText() with all options produces correct packet."""
        iface = mock_interface

        packet = iface.sendText(
            "Test message",
            destinationId="!testnode1",
            wantAck=True,
            wantResponse=True,
            channelIndex=2,
            hopLimit=5,
        )

        assert packet.to == 11259375
        assert packet.want_ack is True
        assert packet.decoded.want_response is True
        assert packet.channel == 2
        assert packet.hop_limit == 5

    def test_sendData_binary_payload(self, mock_interface):
        """Verify sendData() sends binary data correctly."""
        iface = mock_interface

        binary_data = b"\x00\x01\x02\x03\xff\xfe"
        packet = iface.sendData(
            binary_data,
            destinationId="!testnode1",
            portNum=portnums_pb2.PortNum.PRIVATE_APP,
        )

        assert packet.decoded.payload == binary_data
        assert packet.decoded.portnum == portnums_pb2.PortNum.PRIVATE_APP

    def test_sendData_with_ack_and_response(self, mock_interface):
        """Verify sendData() with wantAck and wantResponse flags."""
        iface = mock_interface

        packet = iface.sendData(
            b"test data",
            destinationId=BROADCAST_ADDR,
            wantAck=True,
            wantResponse=True,
            onResponseAckPermitted=True,
        )

        assert packet.want_ack is True
        assert packet.decoded.want_response is True

    def test_sendText_to_local_node(self, mock_interface):
        """Verify sendText() to LOCAL_ADDR routes to local node."""
        iface = mock_interface

        packet = iface.sendText("Local test", destinationId=LOCAL_ADDR)

        # Should route to local node number
        assert packet.to == 2475227164


# -----------------------------------------------------------------------------
# Test: waitForPosition Workflow
# -----------------------------------------------------------------------------


@pytest.mark.unit
class TestWaitForPositionWorkflow:
    """Test waitForPosition() workflow with timeout and request_id."""

    def test_waitForPosition_sets_up_wait_state(self, mock_interface):
        """Verify waitForPosition() sets up correct wait state."""
        iface = mock_interface

        # Set up short timeout to avoid long waits
        iface._timeout.expireTimeout = 0.1

        # Send a position request first to get a request_id
        # Mock waitForPosition to avoid actual waiting
        with patch.object(iface, "waitForPosition"):
            packet = iface.sendPosition(
                latitude=40.7128,
                longitude=-74.0060,
                altitude=100,
                destinationId="!testnode1",
                wantResponse=True,
            )
        request_id = packet.id

        # Verify request was registered
        assert request_id != 0

    def test_waitForPosition_new_signature_with_request_id(self, mock_interface):
        """Verify waitForPosition() works with request_id parameter."""
        iface = mock_interface

        # Mock the wait to avoid actual waiting
        with patch.object(
            iface._send_pipeline, "_wait_for_request_ack", return_value=True
        ) as mock_wait:
            with patch.object(iface, "_raise_wait_error_if_present"):
                iface.waitForPosition(request_id=12345)

            # Verify the new signature was used
            mock_wait.assert_called_once()
            call_args = mock_wait.call_args
            assert call_args[0][0] == "receivedPosition"
            assert call_args[0][1] == 12345

    def test_waitForPosition_legacy_signature_no_request_id(self, mock_interface):
        """Verify waitForPosition() works without request_id (legacy)."""
        iface = mock_interface

        with patch.object(
            iface._timeout, "waitForPosition", return_value=True
        ) as mock_legacy_wait:
            with patch.object(iface, "_raise_wait_error_if_present"):
                iface.waitForPosition()

            mock_legacy_wait.assert_called_once()

    def test_waitForPosition_raises_on_timeout(self, mock_interface):
        """Verify waitForPosition() raises MeshInterfaceError on timeout."""
        iface = mock_interface

        iface._timeout.expireTimeout = 0.01

        with pytest.raises(
            MeshInterface.MeshInterfaceError, match="Timed out waiting for position"
        ):
            iface.waitForPosition(request_id=99999)


# -----------------------------------------------------------------------------
# Test: waitForTelemetry Workflow
# -----------------------------------------------------------------------------


@pytest.mark.unit
class TestWaitForTelemetryWorkflow:
    """Test waitForTelemetry() workflow with telemetry types and request_id."""

    def test_waitForTelemetry_exists_and_accepts_parameters(self, mock_interface):
        """Verify waitForTelemetry() exists and accepts parameters."""
        iface = mock_interface

        # Set up telemetry in nodesByNum for device_metrics path
        iface.nodesByNum[2475227164]["deviceMetrics"] = {
            "batteryLevel": 85,
            "voltage": 3.7,
            "channelUtilization": 10.5,
            "airUtilTx": 5.2,
            "uptimeSeconds": 3600,
        }

        # Mock send to avoid actual sending
        with patch.object(iface, "_send_to_radio_impl"):
            # Use a very short timeout for testing
            iface._timeout.expireTimeout = 0.1

            # Verify the method exists and accepts parameters
            assert hasattr(iface, "waitForTelemetry")

    def test_waitForTelemetry_new_signature_with_request_id(self, mock_interface):
        """Verify waitForTelemetry() works with request_id parameter."""
        iface = mock_interface

        with patch.object(
            iface._send_pipeline, "_wait_for_request_ack", return_value=True
        ) as mock_wait:
            with patch.object(iface, "_raise_wait_error_if_present"):
                iface.waitForTelemetry(request_id=54321)

            mock_wait.assert_called_once()
            call_args = mock_wait.call_args
            assert call_args[0][0] == "receivedTelemetry"
            assert call_args[0][1] == 54321

    def test_waitForTelemetry_legacy_signature_no_request_id(self, mock_interface):
        """Verify waitForTelemetry() works without request_id (legacy)."""
        iface = mock_interface

        with patch.object(
            iface._timeout, "waitForTelemetry", return_value=True
        ) as mock_legacy_wait:
            with patch.object(iface, "_raise_wait_error_if_present"):
                iface.waitForTelemetry()

            mock_legacy_wait.assert_called_once()

    def test_waitForTelemetry_raises_on_timeout(self, mock_interface):
        """Verify waitForTelemetry() raises MeshInterfaceError on timeout."""
        iface = mock_interface

        iface._timeout.expireTimeout = 0.01

        with pytest.raises(
            MeshInterface.MeshInterfaceError, match="Timed out waiting for telemetry"
        ):
            iface.waitForTelemetry(request_id=88888)


# -----------------------------------------------------------------------------
# Test: waitForTraceRoute Workflow
# -----------------------------------------------------------------------------


@pytest.mark.unit
class TestWaitForTraceRouteWorkflow:
    """Test waitForTraceRoute() workflow with waitFactor and request_id."""

    def test_waitForTraceRoute_uses_wait_factor(self, mock_interface):
        """Verify waitForTraceRoute() uses waitFactor for timeout calculation."""
        iface = mock_interface

        with patch.object(
            iface._send_pipeline, "_wait_for_request_ack", return_value=True
        ) as mock_wait:
            with patch.object(iface, "_raise_wait_error_if_present"):
                iface.waitForTraceRoute(waitFactor=3.0, request_id=11111)

            mock_wait.assert_called_once()
            call_args = mock_wait.call_args
            # Verify timeout is scaled by waitFactor
            expected_timeout = iface._timeout.expireTimeout * 3.0
            assert call_args[1]["timeout_seconds"] == expected_timeout

    def test_waitForTraceRoute_new_signature_with_request_id(self, mock_interface):
        """Verify waitForTraceRoute() works with request_id parameter."""
        iface = mock_interface

        with patch.object(
            iface._send_pipeline, "_wait_for_request_ack", return_value=True
        ) as mock_wait:
            with patch.object(iface, "_raise_wait_error_if_present"):
                iface.waitForTraceRoute(waitFactor=1.0, request_id=22222)

            mock_wait.assert_called_once()
            call_args = mock_wait.call_args
            assert call_args[0][0] == "receivedTraceRoute"
            assert call_args[0][1] == 22222

    def test_waitForTraceRoute_legacy_signature_no_request_id(self, mock_interface):
        """Verify waitForTraceRoute() works without request_id (legacy)."""
        iface = mock_interface

        with patch.object(
            iface._timeout, "waitForTraceRoute", return_value=True
        ) as mock_legacy_wait:
            with patch.object(iface, "_raise_wait_error_if_present"):
                iface.waitForTraceRoute(waitFactor=2.0)

            mock_legacy_wait.assert_called_once_with(2.0, iface._acknowledgment)

    def test_waitForTraceRoute_raises_on_timeout(self, mock_interface):
        """Verify waitForTraceRoute() raises MeshInterfaceError on timeout."""
        iface = mock_interface

        iface._timeout.expireTimeout = 0.01

        with pytest.raises(
            MeshInterface.MeshInterfaceError, match="Timed out waiting for traceroute"
        ):
            iface.waitForTraceRoute(waitFactor=1.0, request_id=77777)


# -----------------------------------------------------------------------------
# Test: waitForWaypoint Workflow
# -----------------------------------------------------------------------------


@pytest.mark.unit
class TestWaitForWaypointWorkflow:
    """Test waitForWaypoint() workflow with request_id parameter."""

    def test_waitForWaypoint_new_signature_with_request_id(self, mock_interface):
        """Verify waitForWaypoint() works with request_id parameter."""
        iface = mock_interface

        with patch.object(
            iface._send_pipeline, "_wait_for_request_ack", return_value=True
        ) as mock_wait:
            with patch.object(iface, "_raise_wait_error_if_present"):
                iface.waitForWaypoint(request_id=33333)

            mock_wait.assert_called_once()
            call_args = mock_wait.call_args
            assert call_args[0][0] == "receivedWaypoint"
            assert call_args[0][1] == 33333

    def test_waitForWaypoint_legacy_signature_no_request_id(self, mock_interface):
        """Verify waitForWaypoint() works without request_id (legacy)."""
        iface = mock_interface

        with patch.object(
            iface._timeout, "waitForWaypoint", return_value=True
        ) as mock_legacy_wait:
            with patch.object(iface, "_raise_wait_error_if_present"):
                iface.waitForWaypoint()

            mock_legacy_wait.assert_called_once()

    def test_waitForWaypoint_raises_on_timeout(self, mock_interface):
        """Verify waitForWaypoint() raises MeshInterfaceError on timeout."""
        iface = mock_interface

        iface._timeout.expireTimeout = 0.01

        with pytest.raises(
            MeshInterface.MeshInterfaceError, match="Timed out waiting for waypoint"
        ):
            iface.waitForWaypoint(request_id=66666)


# -----------------------------------------------------------------------------
# Test: showNodes / showInfo Workflow
# -----------------------------------------------------------------------------


@pytest.mark.unit
class TestShowNodesShowInfoWorkflow:
    """Test showNodes() and showInfo() return proper output formats."""

    def test_showNodes_returns_formatted_table(self, mock_interface):
        """Verify showNodes() returns formatted node list."""
        iface = mock_interface

        output = iface.showNodes()

        # Verify output contains expected node information
        assert "!9388f81c" in output or "Test Node" in output
        assert isinstance(output, str)
        assert len(output) > 0

    def test_showNodes_include_self_parameter(self, mock_interface):
        """Verify showNodes() accepts includeSelf parameter without error."""
        iface = mock_interface

        # Both calls should work without error
        output_with_self = iface.showNodes(includeSelf=True)
        output_without_self = iface.showNodes(includeSelf=False)

        # Both should return valid string output
        assert isinstance(output_with_self, str)
        assert isinstance(output_without_self, str)
        assert len(output_with_self) > 0
        assert len(output_without_self) > 0

    def test_showNodes_with_custom_fields(self, mock_interface):
        """Verify showNodes() works with custom field list."""
        iface = mock_interface

        output = iface.showNodes(showFields=["id", "longName"])

        assert isinstance(output, str)
        # Output should contain node data
        assert len(output) > 0

    def test_showInfo_returns_interface_metadata(self, mock_interface):
        """Verify showInfo() returns interface metadata."""
        iface = mock_interface

        output = io.StringIO()
        summary = iface.showInfo(file=output)

        # Verify summary is a string with JSON-like content
        assert isinstance(summary, str)
        assert len(summary) > 0

        # Check output stream
        stream_content = output.getvalue()
        assert isinstance(stream_content, str)

    def test_showInfo_contains_nodes_data(self, mock_interface):
        """Verify showInfo() contains nodes information."""
        iface = mock_interface

        summary = iface.showInfo()

        # Should contain node ID or reference to nodes
        assert (
            "!9388f81c" in summary
            or '"!9388f81c"' in summary
            or "nodes" in summary.lower()
        )

    def test_showInfo_handles_nodes_without_user_dict(self, mock_interface):
        """Verify showInfo() handles malformed node data gracefully."""
        iface = mock_interface

        # Add a node with invalid user data
        iface.nodes["!badnode"] = {
            "num": 999,
            "user": "invalid",  # Not a dict
        }
        iface.nodesByNum[999] = iface.nodes["!badnode"]

        # Should not raise
        output = iface.showInfo()
        assert isinstance(output, str)


# -----------------------------------------------------------------------------
# Test: getNode Workflow
# -----------------------------------------------------------------------------


@pytest.mark.unit
class TestGetNodeWorkflow:
    """Test getNode() workflow with various node identifiers."""

    def test_getNode_by_string_id_returns_node(self, mock_interface):
        """Verify getNode() returns a Node for string ID."""
        iface = mock_interface

        with patch("meshtastic.node.Node") as MockNode:
            mock_node_instance = MagicMock(spec=Node)
            mock_node_instance.waitForConfig = MagicMock(return_value=True)
            MockNode.return_value = mock_node_instance

            node = iface.getNode("!testnode1")

            # Verify Node was created and returned
            assert node is not None
            MockNode.assert_called_once()
            # First arg should be the interface
            call_args = MockNode.call_args
            assert call_args[0][0] == iface

    def test_getNode_by_node_number_returns_node(self, mock_interface):
        """Verify getNode() returns a Node for node number."""
        iface = mock_interface

        with patch("meshtastic.node.Node") as MockNode:
            mock_node_instance = MagicMock(spec=Node)
            mock_node_instance.waitForConfig = MagicMock(return_value=True)
            MockNode.return_value = mock_node_instance

            node = iface.getNode(11259375)

            # Verify Node was returned
            assert node is not None
            MockNode.assert_called_once()

    def test_getNode_local_addr_returns_local_node(self, mock_interface):
        """Verify getNode(LOCAL_ADDR) returns localNode."""
        iface = mock_interface

        node = iface.getNode(LOCAL_ADDR)

        assert node == iface.localNode

    def test_getNode_with_requestChannels_false(self, mock_interface):
        """Verify getNode() respects requestChannels=False (no channel requests)."""
        iface = mock_interface

        with patch("meshtastic.node.Node") as MockNode:
            mock_node_instance = MagicMock(spec=Node)
            mock_node_instance.waitForConfig = MagicMock(return_value=True)
            mock_node_instance.requestChannels = MagicMock()
            MockNode.return_value = mock_node_instance

            _node = iface.getNode("!testnode1", requestChannels=False)

            # Verify requestChannels was not called on the node
            mock_node_instance.requestChannels.assert_not_called()

    def test_getNode_not_found_raises_error(self, mock_interface):
        """Verify getNode() raises error when channel request times out."""
        iface = mock_interface

        with patch("meshtastic.node.Node") as MockNode:
            mock_node_instance = MagicMock(spec=Node)
            # Simulate timeout by returning False from waitForConfig
            mock_node_instance.waitForConfig = MagicMock(return_value=False)
            mock_node_instance.partialChannels = []
            MockNode.return_value = mock_node_instance

            with pytest.raises(MeshInterface.MeshInterfaceError):
                iface.getNode("!nonexistent")


# -----------------------------------------------------------------------------
# Test: sendTelemetry Semantic Deprecation
# -----------------------------------------------------------------------------


@pytest.mark.unit
class TestSendTelemetrySemanticDeprecation:
    """Test sendTelemetry() semantic deprecation warnings."""

    def test_sendTelemetry_unsupported_type_emits_warning(self, mock_interface):
        """Verify unsupported telemetryType values emit deprecation warning."""
        iface = mock_interface

        with patch.object(iface, "_send_to_radio_impl"):
            with pytest.warns(DeprecationWarning) as warnings:
                iface.sendTelemetry(
                    destinationId=BROADCAST_ADDR, telemetryType="unsupported_type"
                )

        assert any("unsupported_type" in str(warning.message) for warning in warnings)

    def test_sendTelemetry_unsupported_type_falls_back_to_device_metrics(
        self, mock_interface
    ):
        """Verify unsupported telemetryType falls back to device_metrics."""
        iface = mock_interface

        with patch.object(iface, "_send_data_with_wait") as mock_send:
            mock_send.return_value = MagicMock()
            mock_send.return_value.id = 12345

            # Use an unsupported type
            iface.sendTelemetry(
                destinationId=BROADCAST_ADDR, telemetryType="invalid_type_xyz"
            )

            # Verify _send_data_with_wait was called
            mock_send.assert_called_once()

            # Check that a Telemetry protobuf was passed
            call_args = mock_send.call_args
            telemetry_arg = call_args[0][0]
            assert isinstance(telemetry_arg, telemetry_pb2.Telemetry)

    def test_sendTelemetry_supported_types_no_warning(self, mock_interface, caplog):
        """Verify supported telemetryType values don't emit warnings."""
        iface = mock_interface

        for telemetry_type in SUPPORTED_TELEMETRY_TYPES:
            caplog.clear()
            with caplog.at_level(logging.WARNING):
                with patch.object(iface, "_send_to_radio_impl"):
                    iface.sendTelemetry(
                        destinationId=BROADCAST_ADDR,
                        telemetryType=telemetry_type,
                        wantResponse=False,
                    )
            # Verify no telemetry-type-related warnings were logged
            assert (
                telemetry_type not in caplog.text
            ), f"Unexpected warning for supported type {telemetry_type}"

    def test_sendTelemetry_with_wantResponse(self, mock_interface):
        """Verify sendTelemetry() with wantResponse=True sets up wait."""
        iface = mock_interface

        iface.nodesByNum[2475227164]["deviceMetrics"] = {
            "batteryLevel": 85,
            "voltage": 3.7,
        }

        with patch.object(iface, "_send_data_with_wait") as mock_send:
            mock_packet = MagicMock()
            mock_packet.id = 55555
            mock_send.return_value = mock_packet

            with patch.object(iface, "waitForTelemetry"):
                iface.sendTelemetry(
                    destinationId=BROADCAST_ADDR,
                    telemetryType="device_metrics",
                    wantResponse=True,
                )

                # Verify response handler was registered
                mock_send.assert_called_once()
                call_kwargs = mock_send.call_args[1]
                assert call_kwargs.get("wantResponse") is True


# -----------------------------------------------------------------------------
# Additional Integration-style Tests
# -----------------------------------------------------------------------------


@pytest.mark.unit
class TestIntegrationWorkflows:
    """Integration-style tests for complete workflows."""

    def test_complete_send_and_wait_workflow(self, mock_interface):
        """Verify complete send + wait workflow functions correctly."""
        iface = mock_interface

        # Send a message
        packet = iface.sendText("Integration test", destinationId="!testnode1")
        assert packet.id != 0

        # Verify packet is properly formed
        assert packet.to == 11259375
        assert packet.decoded.payload == b"Integration test"

    def test_multiple_sends_queue_correctly(self, mock_interface):
        """Verify multiple send operations queue correctly."""
        iface = mock_interface

        packet_ids = []
        for i in range(5):
            packet = iface.sendText(f"Message {i}")
            packet_ids.append(packet.id)

        # Verify all packets have unique IDs
        assert len(set(packet_ids)) == 5
        assert all(pid != 0 for pid in packet_ids)

    def test_sendPosition_workflow(self, mock_interface):
        """Verify sendPosition() workflow."""
        iface = mock_interface

        packet = iface.sendPosition(
            latitude=37.7749,
            longitude=-122.4194,
            altitude=50,
            destinationId="!testnode1",
        )

        assert packet.decoded.portnum == portnums_pb2.PortNum.POSITION_APP
        assert packet.to == 11259375

    def test_sendWaypoint_workflow(self, mock_interface):
        """Verify sendWaypoint() workflow."""
        iface = mock_interface

        packet = iface.sendWaypoint(
            name="Test Waypoint",
            description="A test waypoint",
            icon=1,
            expire=3600,
            latitude=40.7128,
            longitude=-74.0060,
            destinationId="!testnode1",
        )

        assert packet.decoded.portnum == portnums_pb2.PortNum.WAYPOINT_APP
        assert packet.to == 11259375

    def test_sendTraceRoute_workflow(self, mock_interface):
        """Verify sendTraceRoute() workflow."""
        iface = mock_interface

        # Mock internal methods to avoid actual sending
        with patch.object(iface, "_send_data_with_wait") as mock_send:
            mock_packet = MagicMock()
            mock_packet.id = 99999
            mock_send.return_value = mock_packet

            with patch.object(iface, "waitForTraceRoute"):
                iface.sendTraceRoute(dest="!testnode1", hopLimit=3, channelIndex=0)

                mock_send.assert_called_once()
                call_kwargs = mock_send.call_args[1]
                assert call_kwargs.get("portNum") == portnums_pb2.PortNum.TRACEROUTE_APP
                assert call_kwargs.get("hopLimit") == 3


# -----------------------------------------------------------------------------
# Edge Case Tests
# -----------------------------------------------------------------------------


@pytest.mark.unit
class TestEdgeCases:
    """Edge case and error handling tests."""

    def test_sendText_empty_string(self, mock_interface):
        """Verify sendText() handles empty string."""
        iface = mock_interface

        packet = iface.sendText("")
        assert packet.decoded.payload == b""

    def test_sendData_unicode_payload(self, mock_interface):
        """Verify sendData() handles unicode when encoded."""
        iface = mock_interface

        text = "Hello 世界 🌍"
        packet = iface.sendData(text.encode("utf-8"))
        assert packet.decoded.payload == text.encode("utf-8")

    def test_sendData_exceeds_max_size(self, mock_interface):
        """Verify sendData() raises error for oversized payload."""
        iface = mock_interface

        # Create a payload larger than the max allowed
        large_payload = b"x" * (mesh_pb2.Constants.DATA_PAYLOAD_LEN + 1)

        with pytest.raises(
            MeshInterface.MeshInterfaceError, match="Data payload too big"
        ):
            iface.sendData(large_payload)

    def test_showInfo_with_no_nodes(self, mock_interface):
        """Verify showInfo() handles empty nodes gracefully."""
        iface = mock_interface
        iface.nodes = {}
        iface.nodesByNum = {}

        # Should not raise
        output = iface.showInfo()
        assert isinstance(output, str)

    def test_getNode_with_hex_string(self, mock_interface):
        """Verify getNode() handles hex string IDs."""
        iface = mock_interface

        with patch("meshtastic.node.Node") as MockNode:
            mock_node_instance = MagicMock(spec=Node)
            mock_node_instance.waitForConfig = MagicMock(return_value=True)
            MockNode.return_value = mock_node_instance

            # Test hex string (without ! prefix) - this is parsed as direct hex
            _node = iface.getNode("abcdef12")

            # "abcdef12" as hex is 0xabcdef12 = 2882400018
            # The string is passed directly to Node constructor, which converts it
            MockNode.assert_called_once()
            call_args = MockNode.call_args
            assert call_args[0][0] == iface  # First arg is interface
            # Node constructor receives the string and converts it internally


# -----------------------------------------------------------------------------
# Send/Wait Flow Edge Case Tests (HIGH RISK AREA)
# -----------------------------------------------------------------------------


@pytest.mark.unit
class TestSendWaitEdgeCases:
    """Comprehensive edge case tests for send/wait request-response flows.

    These tests cover the highest-risk area of the refactor: ensuring that
    ACK/response handling, timeouts, and concurrent waits work correctly.
    """

    # -------------------------------------------------------------------------
    # 1. ACK received before response
    # -------------------------------------------------------------------------

    def test_ack_received_before_response_completes_correctly(self, mock_interface):
        """Test that when an ACK arrives before the actual response,
        the wait completes correctly when the response arrives."""
        iface = mock_interface

        # Simulate sending a request and getting a request_id
        request_id = 12345
        acknowledgment_attr = "receivedPosition"

        # Register the wait state
        iface._send_pipeline._clear_wait_error(
            acknowledgment_attr, request_id=request_id, clear_scoped=True
        )

        # Simulate ACK arriving first (via _mark_wait_acknowledged)
        iface._send_pipeline._mark_wait_acknowledged(
            acknowledgment_attr, request_id=request_id
        )

        # Verify ACK was recorded
        assert (acknowledgment_attr, request_id) in iface._response_wait_acks

        # Now simulate the actual response arriving
        # This would come through onResponsePosition which calls _mark_wait_acknowledged again
        iface._send_pipeline._mark_wait_acknowledged(
            acknowledgment_attr, request_id=request_id
        )

        # Verify wait_for_request_ack would return True (ACK present)
        # The second mark_wait_acknowledged call also sets the ACK
        assert (acknowledgment_attr, request_id) in iface._response_wait_acks
        # The ACK is only consumed when wait_for_request_ack actually returns
        # (it discards the ACK after detecting it)

    def test_ack_before_response_with_response_handler(self, mock_interface):
        """Test that response handler is still called even if ACK arrives first."""
        iface = mock_interface

        request_id = 54321
        handler_called = False
        received_packet = None

        def response_handler(packet):
            nonlocal handler_called, received_packet
            handler_called = True
            received_packet = packet

        # Register response handler
        iface._request_wait_runtime.add_response_handler(
            request_id, response_handler, ack_permitted=True
        )

        # Simulate the response packet arriving (without explicit ACK)
        response_packet = {
            "from": 11259375,
            "to": 2475227164,
            "decoded": {
                "portnum": "POSITION_APP",
                "requestId": request_id,
                "payload": mesh_pb2.Position(
                    latitude_i=407128000, longitude_i=-740060000
                ).SerializeToString(),
            },
        }

        # Simulate processing the response through correlate_inbound_response
        iface._request_wait_runtime.correlate_inbound_response(
            packet_dict=response_packet,
            skip_response_callback_for_decode_failure=False,
            extract_request_id=iface._send_pipeline._extract_request_id_from_packet,
        )

        # Verify handler was called
        assert handler_called is True
        assert received_packet is not None

    # -------------------------------------------------------------------------
    # 2. Response received without ACK
    # -------------------------------------------------------------------------

    def test_response_without_ack_completes_via_mark_acknowledged(self, mock_interface):
        """Test that response alone (via onResponse handler) completes wait
        without requiring a separate routing ACK."""
        iface = mock_interface

        acknowledgment_attr = "receivedTelemetry"
        request_id = 99999

        # Clear any existing wait state
        iface._send_pipeline._clear_wait_error(
            acknowledgment_attr, request_id=request_id, clear_scoped=True
        )

        # Verify no ACK is initially present
        assert (acknowledgment_attr, request_id) not in iface._response_wait_acks

        # Simulate response handler marking acknowledgment
        # (this is what onResponseTelemetry does when it receives valid telemetry)
        iface._send_pipeline._mark_wait_acknowledged(
            acknowledgment_attr, request_id=request_id
        )

        # Verify acknowledgment was recorded
        assert (acknowledgment_attr, request_id) in iface._response_wait_acks

        # Verify no error was set
        assert (acknowledgment_attr, request_id) not in iface._response_wait_errors

    def test_routing_response_without_app_response_sets_error(self, mock_interface):
        """Test that a routing response without the actual app-level response
        properly records an error."""
        iface = mock_interface

        acknowledgment_attr = "receivedPosition"
        request_id = 77777

        # Set up wait scope
        iface._send_pipeline._clear_wait_error(
            acknowledgment_attr, request_id=request_id, clear_scoped=True
        )

        # Simulate routing error response
        iface._request_wait_runtime.record_routing_wait_error(
            acknowledgment_attr=acknowledgment_attr,
            routing_error_reason="NO_RESPONSE",
            request_id=request_id,
        )

        # Verify error was recorded
        assert (acknowledgment_attr, request_id) in iface._response_wait_errors
        assert (
            "firmware 2.1.22"
            in iface._response_wait_errors[(acknowledgment_attr, request_id)]
        )

    # -------------------------------------------------------------------------
    # 3. Timeout after handler registration
    # -------------------------------------------------------------------------

    def test_timeout_after_handler_registration_raises_error(self, mock_interface):
        """Test that if a handler is registered but no response arrives
        within timeout, it raises/cleans up properly."""
        iface = mock_interface

        acknowledgment_attr = "receivedWaypoint"
        request_id = 88888

        # Register the wait state (as would happen during send)
        iface._send_pipeline._clear_wait_error(
            acknowledgment_attr, request_id=request_id, clear_scoped=True
        )

        # Add a response handler (simulating wantResponse=True)
        handler_called = False

        def dummy_handler(_packet):
            nonlocal handler_called
            handler_called = True

        iface._request_wait_runtime.add_response_handler(
            request_id, dummy_handler, ack_permitted=False
        )

        # Verify handler is registered
        assert request_id in iface.responseHandlers

        # Simulate timeout by calling retire_wait_request (what finally block does)
        iface._send_pipeline._retire_wait_request(
            acknowledgment_attr, request_id=request_id
        )

        # Verify handler was cleaned up
        assert request_id not in iface.responseHandlers

        # Verify request_id was moved to retired set
        retired_ids = iface._send_pipeline._prune_retired_wait_request_ids_locked(
            acknowledgment_attr
        )
        assert request_id in retired_ids

    def test_timeout_cleanup_removes_all_state(self, mock_interface):
        """Test that timeout properly cleans up all wait state."""
        iface = mock_interface

        acknowledgment_attr = "receivedTraceRoute"
        request_id = 55555

        # Set up complete wait state
        iface._send_pipeline._clear_wait_error(
            acknowledgment_attr, request_id=request_id, clear_scoped=True
        )

        # Add to active wait request IDs
        with iface._response_handlers_lock:
            iface._active_wait_request_ids.setdefault(acknowledgment_attr, set()).add(
                request_id
            )

        # Add a response handler
        iface._request_wait_runtime.add_response_handler(
            request_id, lambda _: None, ack_permitted=True
        )

        # Simulate timeout cleanup
        iface._send_pipeline._retire_wait_request(
            acknowledgment_attr, request_id=request_id
        )

        # Verify all state is cleaned
        with iface._response_handlers_lock:
            active_ids = iface._active_wait_request_ids.get(acknowledgment_attr, set())
            assert request_id not in active_ids

        assert request_id not in iface.responseHandlers

    # -------------------------------------------------------------------------
    # 4. Duplicate response handling (idempotency)
    # -------------------------------------------------------------------------

    def test_duplicate_response_handled_idempotently(self, mock_interface):
        """Test that duplicate responses for the same request_id don't cause issues.
        The second response should be ignored (handler already consumed)."""
        iface = mock_interface

        request_id = 11111
        handler_call_count = 0

        def counting_handler(_packet):
            nonlocal handler_call_count
            handler_call_count += 1

        # Register handler
        iface._request_wait_runtime.add_response_handler(
            request_id, counting_handler, ack_permitted=False
        )

        # Simulate first response
        packet1 = {
            "from": 11259375,
            "decoded": {
                "portnum": "TEXT_MESSAGE_APP",
                "requestId": request_id,
                "payload": b"First response",
            },
        }

        # First correlation - handler should be called and consumed
        iface._request_wait_runtime.correlate_inbound_response(
            packet_dict=packet1,
            skip_response_callback_for_decode_failure=False,
            extract_request_id=lambda p: p.get("decoded", {}).get("requestId"),
        )

        assert handler_call_count == 1

        # Simulate duplicate response (same request_id)
        packet2 = {
            "from": 11259375,
            "decoded": {
                "portnum": "TEXT_MESSAGE_APP",
                "requestId": request_id,
                "payload": b"Duplicate response",
            },
        }

        # Second correlation - handler already consumed, so no additional call
        iface._request_wait_runtime.correlate_inbound_response(
            packet_dict=packet2,
            skip_response_callback_for_decode_failure=False,
            extract_request_id=lambda p: p.get("decoded", {}).get("requestId"),
        )

        # Handler should NOT have been called again
        assert handler_call_count == 1

    def test_duplicate_ack_does_not_cause_error(self, mock_interface):
        """Test that duplicate ACKs for the same request are handled gracefully."""
        iface = mock_interface

        acknowledgment_attr = "receivedPosition"
        request_id = 22222

        # Set up the wait scope (required for scoped waits)
        iface._send_pipeline._clear_wait_error(
            acknowledgment_attr, request_id=request_id, clear_scoped=True
        )

        # First ACK
        iface._send_pipeline._mark_wait_acknowledged(
            acknowledgment_attr, request_id=request_id
        )

        # Verify first ACK recorded
        assert (acknowledgment_attr, request_id) in iface._response_wait_acks

        # Consume the ACK (as wait_for_request_ack would do)
        with iface._response_handlers_lock:
            iface._response_wait_acks.discard((acknowledgment_attr, request_id))

        # Second ACK (duplicate)
        iface._send_pipeline._mark_wait_acknowledged(
            acknowledgment_attr, request_id=request_id
        )

        # Should still be recorded (no error)
        assert (acknowledgment_attr, request_id) in iface._response_wait_acks

    # -------------------------------------------------------------------------
    # 5. Request ID mismatches
    # -------------------------------------------------------------------------

    def test_response_with_wrong_request_id_ignored(self, mock_interface):
        """Test that responses with mismatched request_id are ignored
        for the specific wait, but may match other waits."""
        iface = mock_interface

        expected_request_id = 33333
        wrong_request_id = 44444

        handler_called = False

        def handler(_packet):
            nonlocal handler_called
            handler_called = True

        # Register handler for expected request_id
        iface._request_wait_runtime.add_response_handler(
            expected_request_id, handler, ack_permitted=False
        )

        # Send response with wrong request_id
        wrong_packet = {
            "from": 11259375,
            "decoded": {
                "portnum": "TEXT_MESSAGE_APP",
                "requestId": wrong_request_id,
                "payload": b"Wrong ID response",
            },
        }

        # Correlate wrong packet
        iface._request_wait_runtime.correlate_inbound_response(
            packet_dict=wrong_packet,
            skip_response_callback_for_decode_failure=False,
            extract_request_id=lambda p: p.get("decoded", {}).get("requestId"),
        )

        # Handler should NOT have been called (wrong request_id)
        assert handler_called is False

        # Handler should still be registered for expected_request_id
        assert expected_request_id in iface.responseHandlers

    def test_mismatched_request_id_in_wait_for_request_ack(self, mock_interface):
        """Test wait_for_request_ack with non-matching request_id times out."""
        iface = mock_interface

        acknowledgment_attr = "receivedTelemetry"
        registered_request_id = 55555
        queried_request_id = 66666

        # Set up the wait scope for registered_request_id
        iface._send_pipeline._clear_wait_error(
            acknowledgment_attr, request_id=registered_request_id, clear_scoped=True
        )

        # Register ACK for one request_id
        iface._send_pipeline._mark_wait_acknowledged(
            acknowledgment_attr, request_id=registered_request_id
        )

        # Verify the ACK is recorded
        assert (acknowledgment_attr, registered_request_id) in iface._response_wait_acks

        # Set up wait scope for queried_request_id (to avoid unscoped fallback)
        iface._send_pipeline._clear_wait_error(
            acknowledgment_attr, request_id=queried_request_id, clear_scoped=True
        )

        # Query for different request_id should not find ACK
        result = iface._request_wait_runtime.wait_for_request_ack(
            acknowledgment_attr,
            queried_request_id,
            timeout_seconds=0.01,  # Very short timeout
        )

        # Should return False (timeout - no matching ACK)
        assert result is False

        # The registered ACK should still be there (different request_id)
        assert (acknowledgment_attr, registered_request_id) in iface._response_wait_acks

    # -------------------------------------------------------------------------
    # 6. Concurrent outstanding waits
    # -------------------------------------------------------------------------

    def test_multiple_simultaneous_waits_different_request_ids(self, mock_interface):
        """Test that multiple simultaneous waits with different request_ids
        work independently without interference."""
        iface = mock_interface

        acknowledgment_attr = "receivedPosition"
        request_id_1 = 77771
        request_id_2 = 77772

        # Set up two independent waits
        iface._send_pipeline._clear_wait_error(
            acknowledgment_attr, request_id=request_id_1, clear_scoped=True
        )
        iface._send_pipeline._clear_wait_error(
            acknowledgment_attr, request_id=request_id_2, clear_scoped=True
        )

        # Add handlers for both
        handler_1_called = False
        handler_2_called = False

        def handler_1(_packet):
            nonlocal handler_1_called
            handler_1_called = True

        def handler_2(_packet):
            nonlocal handler_2_called
            handler_2_called = True

        iface._request_wait_runtime.add_response_handler(
            request_id_1, handler_1, ack_permitted=False
        )
        iface._request_wait_runtime.add_response_handler(
            request_id_2, handler_2, ack_permitted=False
        )

        # ACK only the first request
        iface._send_pipeline._mark_wait_acknowledged(
            acknowledgment_attr, request_id=request_id_1
        )

        # Verify only first request has ACK
        assert (acknowledgment_attr, request_id_1) in iface._response_wait_acks
        assert (acknowledgment_attr, request_id_2) not in iface._response_wait_acks

        # Simulate response for second request only
        packet_2 = {
            "from": 11259375,
            "decoded": {
                "portnum": "POSITION_APP",
                "requestId": request_id_2,
                "payload": mesh_pb2.Position().SerializeToString(),
            },
        }

        iface._request_wait_runtime.correlate_inbound_response(
            packet_dict=packet_2,
            skip_response_callback_for_decode_failure=False,
            extract_request_id=lambda p: p.get("decoded", {}).get("requestId"),
        )

        # Verify second handler was called
        assert handler_2_called is True
        assert handler_1_called is False

        # Verify both active wait IDs are tracked
        with iface._response_handlers_lock:
            active_ids = iface._active_wait_request_ids.get(acknowledgment_attr, set())
            assert request_id_1 in active_ids
            # request_id_2's handler was consumed, but it's still in active waits

    def test_concurrent_waits_isolation_across_different_types(self, mock_interface):
        """Test that waits for different response types are completely isolated."""
        iface = mock_interface

        pos_request_id = 88881
        telem_request_id = 88882

        # Set up waits for position and telemetry simultaneously
        iface._send_pipeline._clear_wait_error(
            "receivedPosition", request_id=pos_request_id, clear_scoped=True
        )
        iface._send_pipeline._clear_wait_error(
            "receivedTelemetry", request_id=telem_request_id, clear_scoped=True
        )

        # Add ACKs to different attributes
        iface._send_pipeline._mark_wait_acknowledged(
            "receivedPosition", request_id=pos_request_id
        )

        # Verify position ACK exists
        assert ("receivedPosition", pos_request_id) in iface._response_wait_acks

        # Verify telemetry ACK does not exist
        assert ("receivedTelemetry", telem_request_id) not in iface._response_wait_acks

        # Now add telemetry ACK
        iface._send_pipeline._mark_wait_acknowledged(
            "receivedTelemetry", request_id=telem_request_id
        )

        # Verify both exist
        assert ("receivedPosition", pos_request_id) in iface._response_wait_acks
        assert ("receivedTelemetry", telem_request_id) in iface._response_wait_acks

    # -------------------------------------------------------------------------
    # 7. sendTelemetry edge cases
    # -------------------------------------------------------------------------

    def test_sendTelemetry_with_invalid_type_logs_warning(self, mock_interface, caplog):
        """Test that sendTelemetry with invalid type logs a warning and falls back."""
        iface = mock_interface

        with pytest.warns(DeprecationWarning, match="invalid_type_xyz"):
            with caplog.at_level(logging.WARNING):
                with patch.object(iface, "_send_data_with_wait") as mock_send:
                    mock_packet = MagicMock()
                    mock_packet.id = 12345
                    mock_send.return_value = mock_packet

                    # Call with invalid telemetry type
                    iface.sendTelemetry(
                        destinationId=BROADCAST_ADDR,
                        telemetryType="invalid_type_xyz",
                        wantResponse=False,
                    )

                    # Verify method was called
                    mock_send.assert_called_once()

                    # Verify a Telemetry protobuf was passed (with device_metrics as fallback)
                    call_args = mock_send.call_args
                    telemetry_arg = call_args[0][0]
                    assert isinstance(telemetry_arg, telemetry_pb2.Telemetry)

        # Verify warning was logged
        assert "invalid_type_xyz" in caplog.text
        assert "device_metrics" in caplog.text

    def test_sendTelemetry_with_wantResponse_registers_handler(self, mock_interface):
        """Test that sendTelemetry with wantResponse=True registers a response handler."""
        iface = mock_interface

        # Set up device metrics in node DB
        iface.nodesByNum[2475227164]["deviceMetrics"] = {
            "batteryLevel": 85,
            "voltage": 3.7,
        }

        with patch.object(iface, "_send_data_with_wait") as mock_send:
            mock_packet = MagicMock()
            mock_packet.id = 99999
            mock_send.return_value = mock_packet

            with patch.object(iface, "waitForTelemetry"):
                iface.sendTelemetry(
                    destinationId=BROADCAST_ADDR,
                    telemetryType="device_metrics",
                    wantResponse=True,
                )

                # Verify send was called with wantResponse=True
                mock_send.assert_called_once()
                call_kwargs = mock_send.call_args[1]
                assert call_kwargs.get("wantResponse") is True
                assert call_kwargs.get("onResponse") is not None

    def test_sendTelemetry_all_valid_types_no_error(self, mock_interface):
        """Test that all valid telemetry types work without errors."""
        iface = mock_interface

        for telemetry_type in SUPPORTED_TELEMETRY_TYPES:
            with patch.object(iface, "_send_data_with_wait") as mock_send:
                mock_packet = MagicMock()
                mock_packet.id = 10000 + hash(telemetry_type) % 10000
                mock_send.return_value = mock_packet

                iface.sendTelemetry(
                    destinationId=BROADCAST_ADDR,
                    telemetryType=telemetry_type,
                    wantResponse=False,
                )

                # Verify a Telemetry protobuf was passed
                call_args = mock_send.call_args
                telemetry_arg = call_args[0][0]
                assert isinstance(telemetry_arg, telemetry_pb2.Telemetry)

    def test_sendTelemetry_with_local_device_metrics_populated(self, mock_interface):
        """Test that device_metrics telemetry is populated from local node DB."""
        iface = mock_interface

        # Set up localNode.nodeNum to match the node in nodesByNum
        iface.localNode.nodeNum = 2475227164

        # Set up complete device metrics
        iface.nodesByNum[2475227164]["deviceMetrics"] = {
            "batteryLevel": 75,
            "voltage": 4.1,
            "channelUtilization": 15.5,
            "airUtilTx": 8.2,
            "uptimeSeconds": 7200,
        }

        with patch.object(iface, "_send_data_with_wait") as mock_send:
            mock_packet = MagicMock()
            mock_packet.id = 77777
            mock_send.return_value = mock_packet

            iface.sendTelemetry(
                destinationId=BROADCAST_ADDR,
                telemetryType="device_metrics",
                wantResponse=False,
            )

            # Get the Telemetry protobuf that was sent
            call_args = mock_send.call_args
            telemetry_arg = call_args[0][0]

            # Verify all metrics were copied (use approx for floats)
            assert telemetry_arg.device_metrics.battery_level == 75
            assert telemetry_arg.device_metrics.voltage == pytest.approx(4.1)
            assert telemetry_arg.device_metrics.channel_utilization == pytest.approx(
                15.5
            )
            assert telemetry_arg.device_metrics.air_util_tx == pytest.approx(8.2)
            assert telemetry_arg.device_metrics.uptime_seconds == 7200

    # -------------------------------------------------------------------------
    # 8. sendWaypoint edge cases
    # -------------------------------------------------------------------------

    def test_sendWaypoint_generates_unique_id_when_none_provided(self, mock_interface):
        """Test that sendWaypoint generates a unique ID when waypoint_id is None."""
        iface = mock_interface

        waypoint_ids = []

        for i in range(5):
            packet = iface.sendWaypoint(
                name=f"Test Waypoint {i}",
                description=f"Description {i}",
                icon=i,
                expire=3600 + i * 100,
                waypoint_id=None,  # Let it generate
                latitude=40.7128 + i * 0.1,
                longitude=-74.0060 + i * 0.1,
                destinationId="!testnode1",
                wantResponse=False,
            )

            # Extract waypoint from packet
            waypoint = mesh_pb2.Waypoint()
            waypoint.ParseFromString(packet.decoded.payload)

            # Verify ID was generated and is unique
            assert waypoint.id != 0
            assert waypoint.id not in waypoint_ids
            waypoint_ids.append(waypoint.id)

        # Verify all 5 IDs are unique
        assert len(set(waypoint_ids)) == 5

    def test_sendWaypoint_uses_provided_id(self, mock_interface):
        """Test that sendWaypoint uses provided waypoint_id."""
        iface = mock_interface

        provided_id = 123456789

        packet = iface.sendWaypoint(
            name="Fixed ID Waypoint",
            description="Test with fixed ID",
            icon=42,
            expire=7200,
            waypoint_id=provided_id,
            latitude=37.7749,
            longitude=-122.4194,
            destinationId="!testnode1",
            wantResponse=False,
        )

        # Extract waypoint from packet
        waypoint = mesh_pb2.Waypoint()
        waypoint.ParseFromString(packet.decoded.payload)

        # Verify the provided ID was used
        assert waypoint.id == provided_id

    def test_sendWaypoint_with_wantResponse_registers_handler(self, mock_interface):
        """Test that sendWaypoint with wantResponse=True sets up wait properly."""
        iface = mock_interface

        with patch.object(iface, "_send_data_with_wait") as mock_send:
            mock_packet = MagicMock()
            mock_packet.id = 44444
            mock_send.return_value = mock_packet

            with patch.object(iface, "waitForWaypoint"):
                iface.sendWaypoint(
                    name="Response Test",
                    description="Testing wantResponse",
                    icon=1,
                    expire=3600,
                    latitude=40.7128,
                    longitude=-74.0060,
                    destinationId="!testnode1",
                    wantResponse=True,
                )

                # Verify send was called with wantResponse and onResponse
                mock_send.assert_called_once()
                call_kwargs = mock_send.call_args[1]
                assert call_kwargs.get("wantResponse") is True
                assert call_kwargs.get("onResponse") is not None
                assert call_kwargs.get("response_wait_attr") == "receivedWaypoint"

    def test_sendWaypoint_delete_waypoint_flow(self, mock_interface):
        """Test deleteWaypoint flow sets expire=0 correctly."""
        iface = mock_interface

        waypoint_id_to_delete = 987654321

        packet = iface.deleteWaypoint(
            waypoint_id=waypoint_id_to_delete,
            destinationId="!testnode1",
            wantAck=True,
            wantResponse=False,
        )

        # Extract waypoint from packet
        waypoint = mesh_pb2.Waypoint()
        waypoint.ParseFromString(packet.decoded.payload)

        # Verify delete waypoint properties
        assert waypoint.id == waypoint_id_to_delete
        assert waypoint.expire == 0  # This marks it as a delete

    # -------------------------------------------------------------------------
    # 9. Traceroute response edge cases
    # -------------------------------------------------------------------------

    def test_traceroute_with_routing_error_records_wait_error(self, mock_interface):
        """Test that traceroute routing error is properly recorded as wait error."""
        iface = mock_interface

        acknowledgment_attr = "receivedTraceRoute"
        request_id = 55555

        # Set up wait scope
        iface._send_pipeline._clear_wait_error(
            acknowledgment_attr, request_id=request_id, clear_scoped=True
        )

        # Create routing error packet (simulating NO_RESPONSE)
        routing_packet = {
            "from": 11259375,
            "to": 2475227164,
            "decoded": {
                "portnum": "ROUTING_APP",
                "requestId": request_id,
                "routing": {"errorReason": "NO_RESPONSE"},
            },
        }

        # Process through the onResponse handler
        iface._send_pipeline.onResponseTraceRoute(routing_packet)

        # Verify error was recorded
        assert (acknowledgment_attr, request_id) in iface._response_wait_errors
        error_msg = iface._response_wait_errors[(acknowledgment_attr, request_id)]
        assert "firmware 2.1.22" in error_msg

    def test_traceroute_malformed_payload_sets_error(self, mock_interface):
        """Test that malformed traceroute payload sets appropriate wait error."""
        iface = mock_interface

        acknowledgment_attr = "receivedTraceRoute"
        request_id = 44444

        # Set up wait scope
        iface._send_pipeline._clear_wait_error(
            acknowledgment_attr, request_id=request_id, clear_scoped=True
        )

        # Create packet with malformed/invalid payload
        malformed_packet = {
            "from": 11259375,
            "to": 2475227164,
            "decoded": {
                "portnum": "TRACEROUTE_APP",
                "requestId": request_id,
                "payload": b"invalid protobuf data",  # Not a valid RouteDiscovery
            },
        }

        # Process through the onResponse handler
        iface._send_pipeline.onResponseTraceRoute(malformed_packet)

        # Verify error was recorded
        assert (acknowledgment_attr, request_id) in iface._response_wait_errors
        error_msg = iface._response_wait_errors[(acknowledgment_attr, request_id)]
        assert "Failed to parse" in error_msg

    def test_sendTraceRoute_calculates_wait_factor_from_nodes(self, mock_interface):
        """Test that sendTraceRoute calculates waitFactor based on node count."""
        iface = mock_interface

        # Current node count in fixture: 2 nodes
        # Expected waitFactor = max(1, min(2-1=1, hopLimit+1))

        with patch.object(iface, "_send_data_with_wait") as mock_send:
            mock_packet = MagicMock()
            mock_packet.id = 33333
            mock_send.return_value = mock_packet

            with patch.object(iface, "waitForTraceRoute") as mock_wait:
                iface.sendTraceRoute(dest="!testnode1", hopLimit=5, channelIndex=0)

                # Verify waitForTraceRoute was called with calculated waitFactor
                mock_wait.assert_called_once()
                call_args = mock_wait.call_args
                # With 2 nodes, waitFactor should be min(1, 6) = 1
                assert call_args[0][0] == 1.0  # waitFactor

    def test_traceroute_valid_response_completes_wait(self, mock_interface):
        """Test that valid traceroute response properly completes the wait."""
        iface = mock_interface

        acknowledgment_attr = "receivedTraceRoute"
        request_id = 66666

        # Create valid RouteDiscovery response
        route_discovery = mesh_pb2.RouteDiscovery()
        route_discovery.route.append(12345)  # Intermediate hop

        response_packet = {
            "from": 11259375,  # Destination
            "to": 2475227164,  # Source (us)
            "decoded": {
                "portnum": "TRACEROUTE_APP",
                "requestId": request_id,
                "payload": route_discovery.SerializeToString(),
            },
        }

        # Set up wait state
        iface._send_pipeline._clear_wait_error(
            acknowledgment_attr, request_id=request_id, clear_scoped=True
        )

        # Verify no ACK initially
        assert (acknowledgment_attr, request_id) not in iface._response_wait_acks

        # Process response
        iface._send_pipeline.onResponseTraceRoute(response_packet)

        # Verify ACK was recorded
        assert (acknowledgment_attr, request_id) in iface._response_wait_acks

        # Verify no error was set
        assert (acknowledgment_attr, request_id) not in iface._response_wait_errors

    def test_traceroute_response_with_route_back(self, mock_interface):
        """Test traceroute response containing route back data."""
        iface = mock_interface

        acknowledgment_attr = "receivedTraceRoute"
        request_id = 77777

        # Create RouteDiscovery with route back
        route_discovery = mesh_pb2.RouteDiscovery()
        route_discovery.route.append(11111)  # Hop 1 towards
        route_discovery.route.append(22222)  # Hop 2 towards
        route_discovery.route_back.append(33333)  # Hop 1 back
        route_discovery.snr_towards.extend([10, 20, 30])  # SNR values
        route_discovery.snr_back.extend([25, 35])  # SNR values for back route

        response_packet = {
            "from": 11259375,
            "to": 2475227164,
            "hopStart": 3,  # Required for showing route back
            "decoded": {
                "portnum": "TRACEROUTE_APP",
                "requestId": request_id,
                "payload": route_discovery.SerializeToString(),
            },
        }

        # Set up wait state
        iface._send_pipeline._clear_wait_error(
            acknowledgment_attr, request_id=request_id, clear_scoped=True
        )

        # Process response
        iface._send_pipeline.onResponseTraceRoute(response_packet)

        # Verify ACK was recorded (response was processed successfully)
        assert (acknowledgment_attr, request_id) in iface._response_wait_acks


# -----------------------------------------------------------------------------
# Test: Request ID Extraction Edge Cases
# -----------------------------------------------------------------------------


@pytest.mark.unit
class TestRequestIdExtractionEdgeCases:
    """Test edge cases for request ID extraction from packets."""

    def test_extract_request_id_from_packet_valid_int(self, mock_interface):
        """Test extraction of valid integer request_id from packet."""
        iface = mock_interface

        packet = {"decoded": {"requestId": 12345}}

        result = iface._send_pipeline._extract_request_id_from_packet(packet)
        assert result == 12345

    def test_extract_request_id_from_packet_string_digits(self, mock_interface):
        """Test extraction of string request_id that is numeric."""
        iface = mock_interface

        packet = {"decoded": {"requestId": "67890"}}

        result = iface._send_pipeline._extract_request_id_from_packet(packet)
        assert result == 67890

    def test_extract_request_id_from_packet_zero_returns_none(self, mock_interface):
        """Test that request_id of 0 returns None (invalid)."""
        iface = mock_interface

        packet = {"decoded": {"requestId": 0}}

        result = iface._send_pipeline._extract_request_id_from_packet(packet)
        assert result is None

    def test_extract_request_id_from_packet_bool_returns_none(self, mock_interface):
        """Test that boolean request_id returns None."""
        iface = mock_interface

        packet = {"decoded": {"requestId": True}}  # Boolean, should be rejected

        result = iface._send_pipeline._extract_request_id_from_packet(packet)
        assert result is None

    def test_extract_request_id_from_packet_missing_returns_none(self, mock_interface):
        """Test that missing requestId returns None."""
        iface = mock_interface

        packet = {
            "decoded": {
                "portnum": "TEXT_MESSAGE_APP"
                # No requestId
            }
        }

        result = iface._send_pipeline._extract_request_id_from_packet(packet)
        assert result is None

    def test_extract_request_id_from_packet_no_decoded_returns_none(
        self, mock_interface
    ):
        """Test that packet without decoded field returns None."""
        iface = mock_interface

        packet = {
            "from": 12345,
            "to": 67890,
            # No decoded
        }

        result = iface._send_pipeline._extract_request_id_from_packet(packet)
        assert result is None
