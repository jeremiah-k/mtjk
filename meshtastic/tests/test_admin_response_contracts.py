"""Tests for typed ADMIN_APP response correlation contracts."""

from unittest.mock import MagicMock

import pytest

from meshtastic.admin_response import contract_for_admin_request
from meshtastic.mesh_interface import MeshInterface
from meshtastic.protobuf import admin_pb2


def _config_response_packet(
    *, request_id: int, source: int, field: str
) -> dict[str, object]:
    raw = admin_pb2.AdminMessage()
    response = raw.get_config_response
    getattr(response, field).SetInParent()
    return {
        "from": source,
        "decoded": {
            "requestId": request_id,
            "admin": {"raw": raw},
        },
    }


@pytest.mark.unit
def test_contract_for_admin_request_binds_source_variant_and_subtype() -> None:
    request = admin_pb2.AdminMessage(
        get_config_request=admin_pb2.AdminMessage.LORA_CONFIG
    )
    contract = contract_for_admin_request(
        request, destination=0x1234, local_node_num=0x9999
    )
    assert contract is not None
    assert contract.response_variant == "get_config_response"
    assert contract.response_subtype == "lora"
    assert contract.expected_sources == frozenset({0x1234})
    assert contract.matches_source({"from": 0x1234})
    assert not contract.matches_source({"from": 0x5678})
    assert not contract.matches_source({"from": "0x1234"})
    assert contract.matches(
        _config_response_packet(request_id=1, source=0x1234, field="lora")
    )
    assert not contract.matches(
        _config_response_packet(request_id=1, source=0x1234, field="position")
    )
    assert not contract.matches(
        _config_response_packet(request_id=1, source=0x5678, field="lora")
    )


@pytest.mark.unit
def test_local_contract_accepts_zero_or_local_source() -> None:
    request = admin_pb2.AdminMessage(get_device_metadata_request=True)
    contract = contract_for_admin_request(
        request, destination=0x1234, local_node_num=0x1234
    )
    assert contract is not None
    assert contract.expected_sources == frozenset({0, 0x1234})
    assert contract.matches_source({"from": 0})
    assert contract.matches_source({"from": 0x1234})


@pytest.mark.unit
@pytest.mark.parametrize(
    ("source", "decoded", "allowed"),
    [
        (0x1234, {"routing": {"errorReason": "NOT_AUTHORIZED"}}, True),
        (0x9999, {"routing": {"errorReason": "PKI_FAILED"}}, True),
        (0x9999, {"routing": {"errorReason": "NONE"}}, False),
        (0x9999, {"routing": {"errorReason": 0}}, False),
        (0x9999, {"routing": {"errorReason": "decode failed"}}, False),
        (
            0x9999,
            {"routing": {"errorReason": "PKI_FAILED", "error": "bad bytes"}},
            False,
        ),
        (0x9999, {"routing": {}}, False),
        (0x9999, {"admin": {"decode_error": "invalid payload"}}, False),
        (0x9999, {}, False),
        (0x5678, {"routing": {"errorReason": "PKI_FAILED"}}, False),
        (0, {"routing": {"errorReason": "PKI_FAILED"}}, False),
    ],
)
def test_remote_contract_distinguishes_origin_rejection_from_peer_feedback(
    source: int, decoded: dict[str, object], allowed: bool
) -> None:
    request = admin_pb2.AdminMessage(get_device_metadata_request=True)
    contract = contract_for_admin_request(
        request, destination=0x1234, local_node_num=0x9999
    )
    assert contract is not None
    packet = {"from": source, "decoded": decoded}
    assert contract._matches_feedback(packet) is allowed
    assert contract.matches_source(packet) is (source == 0x1234)


@pytest.mark.unit
def test_remote_contract_without_local_identity_rejects_other_sources() -> None:
    contract = contract_for_admin_request(
        admin_pb2.AdminMessage(get_device_metadata_request=True),
        destination=0x1234,
        local_node_num=None,
    )
    assert contract is not None
    assert not contract._matches_feedback(
        {"from": 0, "decoded": {"routing": {"errorReason": "PKI_FAILED"}}}
    )


@pytest.mark.unit
def test_mismatched_response_does_not_consume_handler() -> None:
    iface = MeshInterface(noProto=True)
    request_id = 77
    callback = MagicMock()
    request = admin_pb2.AdminMessage(
        get_config_request=admin_pb2.AdminMessage.LORA_CONFIG
    )
    contract = contract_for_admin_request(
        request, destination=0x1234, local_node_num=0x9999
    )
    assert contract is not None
    iface._request_wait_runtime.add_response_handler(
        request_id,
        callback,
        ack_permitted=True,
        matcher=contract.matches,
    )

    wrong = _config_response_packet(
        request_id=request_id, source=0x1234, field="position"
    )
    iface._request_wait_runtime.correlate_inbound_response(
        packet_dict=wrong,
        skip_response_callback_for_decode_failure=False,
        extract_request_id=lambda packet: int(packet["decoded"]["requestId"]),  # type: ignore[index]
    )
    assert callback.call_count == 0
    assert request_id in iface.responseHandlers

    correct = _config_response_packet(
        request_id=request_id, source=0x1234, field="lora"
    )
    iface._request_wait_runtime.correlate_inbound_response(
        packet_dict=correct,
        skip_response_callback_for_decode_failure=False,
        extract_request_id=lambda packet: int(packet["decoded"]["requestId"]),  # type: ignore[index]
    )
    callback.assert_called_once_with(correct)
    assert request_id not in iface.responseHandlers


@pytest.mark.unit
@pytest.mark.parametrize("reason", ["NONE", 0])
def test_routing_ack_remains_eligible_before_typed_payload(reason: str | int) -> None:
    iface = MeshInterface(noProto=True)
    callback = MagicMock()
    iface._request_wait_runtime.add_response_handler(
        88,
        callback,
        ack_permitted=True,
        matcher=lambda _packet: False,
    )
    ack = {
        "from": 1,
        "decoded": {
            "requestId": 88,
            "routing": {"errorReason": reason},
        },
    }
    iface._request_wait_runtime.correlate_inbound_response(
        packet_dict=ack,
        skip_response_callback_for_decode_failure=False,
        extract_request_id=lambda packet: int(packet["decoded"]["requestId"]),  # type: ignore[index]
    )
    callback.assert_called_once_with(ack)


@pytest.mark.unit
def test_contract_maps_named_config_and_module_enum_values() -> None:
    device_ui = contract_for_admin_request(
        admin_pb2.AdminMessage(
            get_config_request=admin_pb2.AdminMessage.DEVICEUI_CONFIG
        ),
        destination=1,
        local_node_num=None,
    )
    mesh_beacon = contract_for_admin_request(
        admin_pb2.AdminMessage(
            get_module_config_request=admin_pb2.AdminMessage.MESHBEACON_CONFIG
        ),
        destination=1,
        local_node_num=None,
    )
    assert device_ui is not None
    assert device_ui.response_subtype == "device_ui"
    assert mesh_beacon is not None
    assert mesh_beacon.response_subtype == "mesh_beacon"


@pytest.mark.unit
def test_matcher_exception_does_not_consume_handler(
    caplog: pytest.LogCaptureFixture,
) -> None:
    iface = MeshInterface(noProto=True)
    callback = MagicMock()

    def broken_matcher(_packet: dict[str, object]) -> bool:
        raise ValueError("bad matcher")

    iface._request_wait_runtime.add_response_handler(
        99,
        callback,
        ack_permitted=True,
        matcher=broken_matcher,
    )
    packet = _config_response_packet(request_id=99, source=1, field="lora")
    iface._request_wait_runtime.correlate_inbound_response(
        packet_dict=packet,
        skip_response_callback_for_decode_failure=False,
        extract_request_id=lambda value: int(value["decoded"]["requestId"]),  # type: ignore[index]
    )
    callback.assert_not_called()
    assert 99 in iface.responseHandlers
    assert "Response matcher failed for requestId 99" in caplog.text
