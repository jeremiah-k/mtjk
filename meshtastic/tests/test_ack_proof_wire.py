"""Receipt-proof metadata must survive decoding without changing routing waits.

Firmware reports pairwise receipt-proof verdicts separately from identity
signatures. Legacy packets omit the verdict, and advisory verdicts must not
replace the routing reason or complete a typed getter before its data arrives.
"""

from base64 import b64encode
from typing import Any, cast

import pytest

from meshtastic.mesh_interface import MeshInterface
from meshtastic.node import Node
from meshtastic.node_runtime.admin_wait import WAIT_ATTR_NAK
from meshtastic.protobuf import admin_pb2, config_pb2, mesh_pb2, portnums_pb2


@pytest.mark.unit
@pytest.mark.parametrize(
    "proof_status",
    [
        mesh_pb2.MeshPacket.ACK_PROOF_ABSENT,
        mesh_pb2.MeshPacket.ACK_PROOF_VALID,
        mesh_pb2.MeshPacket.ACK_PROOF_INVALID,
        mesh_pb2.MeshPacket.ACK_PROOF_NO_KEY,
        99,
    ],
)
@pytest.mark.parametrize("refused", [False, True])
def test_proof_metadata_survives_wire_decode_and_scoped_wait(
    proof_status: int, refused: bool
) -> None:
    """A proof verdict is advisory; Routing.error_reason owns ACK/NAK completion."""
    with MeshInterface(noProto=True) as iface:
        iface.myInfo = mesh_pb2.MyNodeInfo(my_node_num=1)
        iface.localNode.nodeNum = 1
        iface.nodes = {}
        iface.nodesByNum = {}
        remote = Node(iface, 2, noProto=True)
        captured: list[dict[str, Any]] = []

        def _receive(packet: dict[str, Any]) -> None:
            captured.append(packet)
            remote.onAckNak(packet)

        iface._clear_wait_error(WAIT_ATTR_NAK, request_id=101)
        iface._add_response_handler(101, _receive, ackPermitted=True)
        proof = b"receipt!" if proof_status else b""
        routing = mesh_pb2.Routing(
            error_reason=(
                mesh_pb2.Routing.NOT_AUTHORIZED if refused else mesh_pb2.Routing.NONE
            ),
            ack_proof=proof,
        )
        packet = mesh_pb2.MeshPacket(
            id=202,
            to=1,
            ack_proof_status=proof_status,  # type: ignore[arg-type]
            pki_encrypted=True,
            xeddsa_signed=False,
        )
        setattr(packet, "from", 2)
        packet.decoded.portnum = portnums_pb2.PortNum.ROUTING_APP
        packet.decoded.request_id = 101
        packet.decoded.payload = routing.SerializeToString()

        iface._handle_from_radio(mesh_pb2.FromRadio(packet=packet).SerializeToString())

        assert len(captured) == 1
        decoded = captured[0]
        assert decoded["raw"].ack_proof_status == proof_status
        if proof_status:
            expected = (
                mesh_pb2.MeshPacket.AckProofStatus.Name(
                    cast(mesh_pb2.MeshPacket.AckProofStatus.ValueType, proof_status)
                )
                if proof_status != 99
                else 99
            )
            assert decoded["ackProofStatus"] == expected
            assert (
                decoded["decoded"]["routing"]["ackProof"] == b64encode(proof).decode()
            )
        else:
            assert "ackProofStatus" not in decoded
            assert "ackProof" not in decoded["decoded"]["routing"]
        assert decoded["decoded"]["routing"]["raw"].ack_proof == proof
        assert not decoded["raw"].xeddsa_signed
        assert iface._acknowledgment.receivedAck is not refused
        assert iface._acknowledgment.receivedNak is refused
        if refused:
            with pytest.raises(
                MeshInterface.MeshInterfaceError, match="NOT_AUTHORIZED"
            ):
                iface._wait_for_ack_nak(101)
        else:
            iface._wait_for_ack_nak(101)
        assert 101 not in iface.responseHandlers
        assert not iface._active_wait_request_ids.get(WAIT_ATTR_NAK)


@pytest.mark.unit
def test_proven_ack_does_not_complete_typed_admin_response() -> None:
    """Even a verified receipt only confirms transport, not the requested payload."""
    with MeshInterface(noProto=True) as iface:
        iface.myInfo = mesh_pb2.MyNodeInfo(my_node_num=1)
        iface.localNode.nodeNum = 1
        iface.nodes = {}
        iface.nodesByNum = {}
        captured: list[dict[str, Any]] = []
        iface._add_response_handler(
            101,
            captured.append,
            matcher=lambda packet: "admin" in packet.get("decoded", {}),
        )
        packet = mesh_pb2.MeshPacket(
            id=202, to=1, ack_proof_status=mesh_pb2.MeshPacket.ACK_PROOF_VALID
        )
        setattr(packet, "from", 2)
        packet.decoded.request_id = 101
        packet.decoded.portnum = portnums_pb2.PortNum.ROUTING_APP
        packet.decoded.payload = mesh_pb2.Routing(
            error_reason=mesh_pb2.Routing.NONE, ack_proof=b"receipt!"
        ).SerializeToString()
        iface._handle_from_radio(mesh_pb2.FromRadio(packet=packet).SerializeToString())
        assert captured == []
        assert 101 in iface.responseHandlers

        packet.id = 203
        packet.ClearField("ack_proof_status")
        packet.decoded.portnum = portnums_pb2.PortNum.ADMIN_APP
        reply = admin_pb2.AdminMessage()
        reply.get_config_response.device.role = config_pb2.Config.DeviceConfig.ROUTER
        packet.decoded.payload = reply.SerializeToString()
        iface._handle_from_radio(mesh_pb2.FromRadio(packet=packet).SerializeToString())
        assert len(captured) == 1
        assert (
            captured[0]["decoded"]["admin"]["raw"].get_config_response.device.role
            == config_pb2.Config.DeviceConfig.ROUTER
        )
        assert 101 not in iface.responseHandlers


@pytest.mark.unit
def test_implicit_ack_retains_relay_and_link_metrics() -> None:
    """A locally generated relay ACK remains implicit and exposes its RF metrics."""
    with MeshInterface(noProto=True) as iface:
        iface.myInfo = mesh_pb2.MyNodeInfo(my_node_num=1)
        iface.localNode.nodeNum = 1
        iface.nodes = {}
        iface.nodesByNum = {}
        captured: list[dict[str, Any]] = []

        def _receive(packet: dict[str, Any]) -> None:
            captured.append(packet)
            iface.localNode.onAckNak(packet)

        iface._clear_wait_error(WAIT_ATTR_NAK, request_id=101)
        iface._add_response_handler(101, _receive, ackPermitted=True)
        packet = mesh_pb2.MeshPacket(
            id=202, to=1, relay_node=0x34, rx_rssi=0, rx_snr=-7.5
        )
        setattr(packet, "from", 1)
        packet.decoded.request_id = 101
        packet.decoded.portnum = portnums_pb2.PortNum.ROUTING_APP
        packet.decoded.payload = mesh_pb2.Routing(
            error_reason=mesh_pb2.Routing.NONE
        ).SerializeToString()
        iface._handle_from_radio(mesh_pb2.FromRadio(packet=packet).SerializeToString())

        assert len(captured) == 1
        assert captured[0]["relayNode"] == 0x34
        assert captured[0]["rxRssi"] == 0
        assert captured[0]["rxSnr"] == -7.5
        assert iface._acknowledgment.receivedImplAck
        assert not iface._acknowledgment.receivedAck
        iface._wait_for_ack_nak(101)
