"""Three-node native simradio mesh behavior smoke tests."""

from __future__ import annotations

from typing import Any

import pytest

from .simradio_harness import SimMesh
from .simradio_helpers import subscribe_texts, subscribe_traceroutes

pytestmark = [pytest.mark.simradio, pytest.mark.simradio_mesh, pytest.mark.smokevirt]


def _decoded_port(packet: dict[str, Any]) -> str | None:
    decoded = packet.get("decoded")
    return decoded.get("portnum") if isinstance(decoded, dict) else None


def test_simradio_mesh_node_databases_converge(firmware_mesh: SimMesh) -> None:
    """Every node should retain all three participants after fixture convergence."""
    counts = firmware_mesh.node_db_counts()
    assert len(counts) == firmware_mesh.node_count
    assert all(count >= firmware_mesh.node_count for count in counts), counts


def test_simradio_mesh_broadcast_crosses_relay(firmware_mesh: SimMesh) -> None:
    """A broadcast from A should reach direct neighbor B and relayed node C."""
    text = "simradio-broadcast-a-to-c"
    with (
        subscribe_texts(firmware_mesh.get_iface(1)) as collector_b,
        subscribe_texts(firmware_mesh.get_iface(2)) as collector_c,
    ):
        firmware_mesh.send_text(0, text, wantAck=False)
        assert collector_b.wait_for_text(text), "B did not receive A's broadcast"
        assert collector_c.wait_for_text(
            text
        ), "C did not receive A's relayed broadcast"


def test_simradio_mesh_direct_message_stops_at_destination(
    firmware_mesh: SimMesh,
) -> None:
    """A direct A→B message should arrive at B without leaking to C."""
    text = "simradio-direct-a-to-b"
    destination_b = firmware_mesh.get_node(1).node_num
    with (
        subscribe_texts(firmware_mesh.get_iface(1)) as collector_b,
        subscribe_texts(firmware_mesh.get_iface(2)) as collector_c,
    ):
        firmware_mesh.send_text(
            0,
            text,
            destinationId=destination_b,
            wantAck=False,
        )
        assert collector_b.wait_for_text(text), "B did not receive its direct message"
        collector_c.assert_no_text(text)


def test_simradio_mesh_direct_message_relays_to_non_neighbor(
    firmware_mesh: SimMesh,
) -> None:
    """A direct A→C message should traverse B in the chain topology."""
    text = "simradio-direct-a-via-b-to-c"
    destination_c = firmware_mesh.get_node(2).node_num
    with subscribe_texts(firmware_mesh.get_iface(2)) as collector_c:
        firmware_mesh.send_text(
            0,
            text,
            destinationId=destination_c,
            wantAck=False,
        )
        assert collector_c.wait_for_text(text), "C did not receive A's relayed DM"


def test_simradio_mesh_hop_limit_zero_reaches_neighbor_only(
    firmware_mesh: SimMesh,
) -> None:
    """hopLimit=0 should reach B over the simulated link but never relay to C."""
    text = "simradio-hop-zero"
    with (
        subscribe_texts(firmware_mesh.get_iface(1)) as collector_b,
        subscribe_texts(firmware_mesh.get_iface(2)) as collector_c,
    ):
        firmware_mesh.send_text(
            0,
            text,
            wantAck=False,
            hopLimit=0,
        )
        assert collector_b.wait_for_text(text), "B did not hear hopLimit=0 packet"
        collector_c.assert_no_text(text)


def test_simradio_mesh_hop_limit_one_allows_single_relay(
    firmware_mesh: SimMesh,
) -> None:
    """hopLimit=1 should permit the one relay required for A→B→C."""
    text = "simradio-hop-one"
    with subscribe_texts(firmware_mesh.get_iface(2)) as collector_c:
        firmware_mesh.send_text(
            0,
            text,
            wantAck=False,
            hopLimit=1,
        )
        assert collector_c.wait_for_text(text), "C did not receive one-hop relay"


def test_simradio_mesh_traceroute_reports_forward_and_return_relay(
    firmware_mesh: SimMesh,
) -> None:
    """A→C traceroute should report B in both route directions."""
    source_a = firmware_mesh.get_node(0).node_num
    relay_b = firmware_mesh.get_node(1).node_num
    destination_c = firmware_mesh.get_node(2).node_num
    with (
        subscribe_traceroutes(firmware_mesh.get_iface(0)) as collector_a,
        subscribe_traceroutes(firmware_mesh.get_iface(2)) as collector_c,
    ):
        firmware_mesh.send_trace_route(0, dest=destination_c, hopLimit=3)
        response = collector_a.wait_for_packet(
            lambda packet: (
                _decoded_port(packet) == "TRACEROUTE_APP"
                and packet.get("from") == destination_c
            )
        )
        assert response is not None, "A did not receive C's traceroute response"
        decoded = response.get("decoded")
        assert isinstance(decoded, dict)
        route = decoded.get("traceroute")
        assert isinstance(route, dict)
        assert route.get("route") == [relay_b]
        assert route.get("routeBack") == [relay_b]

        request = collector_c.wait_for_packet(
            lambda packet: (
                _decoded_port(packet) == "TRACEROUTE_APP"
                and packet.get("from") == source_a
            )
        )
        assert request is not None, "C did not observe A's traceroute request"
