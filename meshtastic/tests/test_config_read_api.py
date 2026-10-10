"""Exercise fresh configuration reads through real request correlation."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from meshtastic._core_constants import DECODE_ERROR_KEY
from meshtastic._deadline import _operation_deadline, _remaining_timeout
from meshtastic.admin_response import (
    _CONFIG_RESPONSE_SUBTYPE_BY_REQUEST,
    _MODULE_CONFIG_RESPONSE_SUBTYPE_BY_REQUEST,
)
from meshtastic.errors import (
    RequestError,
    RequestRejectedError,
    RequestTimeoutError,
    ResponseDecodeError,
)
from meshtastic.mesh_interface import MeshInterface
from meshtastic.node import Node
from meshtastic.protobuf import admin_pb2, config_pb2, mesh_pb2, module_config_pb2

pytestmark = pytest.mark.unit


@pytest.fixture
def client():
    with MeshInterface(noProto=True) as interface:
        interface.nodesByNum = {}
        interface.nodes = {}
        interface.localNode.nodeNum = 1
        interface.myInfo = mesh_pb2.MyNodeInfo(my_node_num=1)
        yield interface


def deliver(client, packet):
    client._request_wait_runtime.correlate_inbound_response(
        packet_dict=packet,
        skip_response_callback_for_decode_failure=(
            DECODE_ERROR_KEY in packet.get("decoded", {}).get("admin", {})
        ),
        extract_request_id=client._extract_request_id_from_packet,
    )


def response_packet(packet, *, module=False, section="lora", source=2):
    raw = admin_pb2.AdminMessage()
    response = raw.get_module_config_response if module else raw.get_config_response
    getattr(response, section).SetInParent()
    return {"from": source, "decoded": {"requestId": packet.id, "admin": {"raw": raw}}}


@pytest.mark.parametrize(
    "module, request_enum, section",
    [(False, key, name) for key, name in _CONFIG_RESPONSE_SUBTYPE_BY_REQUEST.items()]
    + [
        (True, key, name)
        for key, name in _MODULE_CONFIG_RESPONSE_SUBTYPE_BY_REQUEST.items()
    ],
)
def test_every_section_uses_named_wire_enum(
    client, monkeypatch, module, request_enum, section
):
    remote = Node(client, 2, noProto=False)
    received = []

    def send(packet, *args, **kwargs):
        request = admin_pb2.AdminMessage.FromString(packet.decoded.payload)
        received.append(request)
        deliver(client, response_packet(packet, module=module, section=section))
        return packet

    monkeypatch.setattr(client, "_send_packet", send)
    result = (remote.readModuleConfig if module else remote.readConfig)(
        section, adminIndex=0
    )
    assert isinstance(
        result, module_config_pb2.ModuleConfig if module else config_pb2.Config
    )
    assert result.WhichOneof("payload_variant") == section
    assert (
        getattr(
            received[0], "get_module_config_request" if module else "get_config_request"
        )
        == request_enum
    )
    assert client.responseHandlers == {}
    assert client._response_wait_errors == {}


def test_read_is_fresh_detached_and_does_not_replace_cache(client, monkeypatch):
    remote = Node(client, 2, noProto=False)
    remote.localConfig.lora.hop_limit = 7
    responses = []

    def send(packet, *args, **kwargs):
        response = response_packet(packet)
        raw = response["decoded"]["admin"]["raw"]
        raw.get_config_response.lora.hop_limit = len(responses) + 2
        responses.append(raw)
        deliver(client, response)
        return packet

    monkeypatch.setattr(client, "_send_packet", send)
    first = remote.readConfig("lora")
    second = remote.readConfig("lora")
    first.lora.hop_limit = 6
    assert second.lora.hop_limit == 3
    assert responses[0].get_config_response.lora.hop_limit == 2
    assert remote.localConfig.lora.hop_limit == 7


@pytest.mark.parametrize("path", ["lora.hop_limit", "lora.hopLimit"])
def test_preference_returns_raw_scalar_and_validates_aliases(client, monkeypatch, path):
    def send(packet, *args, **kwargs):
        response = response_packet(packet)
        response["decoded"]["admin"]["raw"].get_config_response.lora.hop_limit = 4
        deliver(client, response)
        return packet

    monkeypatch.setattr(client, "_send_packet", send)
    assert Node(client, 2, noProto=False).readPreference(path) == 4


def test_preference_copies_repeated_bytes_and_message_values(client, monkeypatch):
    response = None

    def send(packet, *args, **kwargs):
        nonlocal response
        response = response_packet(packet, section="security")
        security = response["decoded"]["admin"]["raw"].get_config_response.security
        security.admin_key.append(b"key")
        deliver(client, response)
        return packet

    monkeypatch.setattr(client, "_send_packet", send)
    remote = Node(client, 2, noProto=False)
    values = remote.readPreference("security.adminKey")
    values.append(b"other")
    assert list(
        response["decoded"]["admin"]["raw"].get_config_response.security.admin_key
    ) == [b"key"]
    message = remote.readPreference("security")
    message.admin_key.append(b"message copy")
    assert list(
        response["decoded"]["admin"]["raw"].get_config_response.security.admin_key
    ) == [b"key"]


@pytest.mark.parametrize(
    "options",
    [
        {"timeout": 0},
        {"timeout": -1},
        {"timeout": True},
        {"timeout": float("nan")},
        {"timeout": float("inf")},
        {"adminIndex": -1},
        {"adminIndex": True},
        {"adminIndex": 8},
    ],
)
def test_invalid_read_options_fail_before_transmission(client, monkeypatch, options):
    def send(*args, **kwargs):
        pytest.fail("invalid read transmitted a packet")

    monkeypatch.setattr(client, "_send_packet", send)
    with pytest.raises(ValueError):
        Node(client, 2, noProto=False).readConfig("lora", **options)


@pytest.mark.parametrize(
    "path",
    [
        "",
        "missing",
        "lora.noSuchField",
        "lora.hopLimit.value",
        "security.adminKey.0",
        "mqtt..enabled",
    ],
)
def test_invalid_preference_paths_fail_before_transmission(client, monkeypatch, path):
    def send(*args, **kwargs):
        pytest.fail("invalid path transmitted a packet")

    monkeypatch.setattr(client, "_send_packet", send)
    with pytest.raises(ValueError):
        Node(client, 2, noProto=False).readPreference(path)


def test_wrong_source_subtype_variant_and_ack_do_not_consume_request(
    client, monkeypatch
):
    def send(packet, *args, **kwargs):
        for response in [
            response_packet(packet, source=3),
            response_packet(packet, section="power"),
            response_packet(packet, module=True, section="mqtt"),
            {
                "from": 2,
                "decoded": {"requestId": packet.id, "routing": {"errorReason": "NONE"}},
            },
        ]:
            deliver(client, response)
            assert packet.id in client.responseHandlers
        deliver(client, response_packet(packet))
        return packet

    monkeypatch.setattr(client, "_send_packet", send)
    assert Node(client, 2, noProto=False).readConfig("lora").HasField("lora")
    assert client.responseHandlers == {}


@pytest.mark.parametrize("decode", [False, True])
def test_typed_failures_include_context_and_retire_only_owned_state(
    client, monkeypatch, decode
):
    def sentinel(packet):
        return None

    client.responseHandlers[1234] = sentinel

    def send(packet, *args, **kwargs):
        decoded = {"requestId": packet.id}
        decoded.update(
            {"admin": {DECODE_ERROR_KEY: "malformed"}}
            if decode
            else {"routing": {"errorReason": "NOT_AUTHORIZED"}}
        )
        deliver(client, {"from": 2, "decoded": decoded})
        return packet

    monkeypatch.setattr(client, "_send_packet", send)
    with pytest.raises(
        ResponseDecodeError if decode else RequestRejectedError
    ) as captured:
        Node(client, 2, noProto=False).readConfig("lora", timeout=0.1)
    error = captured.value
    assert error.nodeNum == 2 and error.requestId > 0
    assert error.operation == "get_config_request"
    if not decode:
        assert error.reason == "NOT_AUTHORIZED"
    assert client.responseHandlers == {1234: sentinel}
    assert client._response_wait_errors == {}
    assert client._acknowledgment.receivedNak is False


def test_timeout_retires_handler_and_late_response_cannot_change_cache(
    client, monkeypatch
):
    packets = []

    def send(packet, *args, **kwargs):
        packets.append(packet)
        return packet

    monkeypatch.setattr(client, "_send_packet", send)
    remote = Node(client, 2, noProto=False)
    with pytest.raises(RequestTimeoutError) as captured:
        remote.readConfig("lora", timeout=0.01)
    assert isinstance(captured.value, TimeoutError)
    assert captured.value.requestId == packets[0].id
    assert client.responseHandlers == {}
    deliver(client, response_packet(packets[0]))
    assert not remote.localConfig.HasField("lora")


def test_concurrent_reads_correlate_out_of_order(client, monkeypatch):
    barrier = threading.Barrier(2)

    def send(packet, *args, **kwargs):
        request = admin_pb2.AdminMessage.FromString(packet.decoded.payload)
        barrier.wait(timeout=2)
        section = (
            "lora"
            if request.get_config_request == admin_pb2.AdminMessage.LORA_CONFIG
            else "power"
        )
        deliver(client, response_packet(packet, section=section))
        return packet

    monkeypatch.setattr(client, "_send_packet", send)
    remote = Node(client, 2, noProto=False)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(remote.readConfig, "lora", timeout=2)
        second = pool.submit(remote.readConfig, "power", timeout=2)
        assert first.result().HasField("lora")
        assert second.result().HasField("power")
    assert client.responseHandlers == {}
    assert client._response_wait_errors == {}


def test_connection_wait_shares_budget(client):
    client.noProto = False
    try:
        start = time.monotonic()
        with pytest.raises(RequestTimeoutError):
            Node(client, 2, noProto=False).readConfig("lora", timeout=0.03)
        assert time.monotonic() - start < 0.5
        assert client.responseHandlers == {}
    finally:
        client.noProto = True


@pytest.mark.parametrize("cause", ["stored_failure", "abort_reason"])
def test_expired_connection_deadline_preserves_more_specific_failure(
    client, monkeypatch, cause
):
    """A fatal failure or abort observed at the deadline beats a generic timeout."""
    client.noProto = False
    failure = RuntimeError("transport failed during connect")

    def expire_wait(_seconds):
        if cause == "stored_failure":
            client.failure = failure
        time.sleep(0.01)
        return False

    monkeypatch.setattr(client.isConnected, "wait", expire_wait)
    if cause == "abort_reason":
        monkeypatch.setattr(
            client,
            "_connect_wait_should_abort",
            lambda: "transport closed",
            raising=False,
        )
    try:
        with _operation_deadline(0.002):
            if cause == "stored_failure":
                with pytest.raises(
                    RuntimeError, match="transport failed during connect"
                ):
                    client._wait_connected(timeout=0.1)
            else:
                with pytest.raises(
                    MeshInterface.MeshInterfaceError, match="transport closed"
                ):
                    client._wait_connected(timeout=0.1)
    finally:
        client.noProto = True
        client.failure = None


def test_full_tx_queue_shares_budget_and_removes_unsent_packet(client, monkeypatch):
    client.noProto = False
    client.isConnected.set()
    client.queueStatus = mesh_pb2.QueueStatus(free=0, maxlen=16)
    sent = []
    monkeypatch.setattr(client, "_send_to_radio_impl", sent.append)
    try:
        start = time.monotonic()
        with pytest.raises(RequestTimeoutError):
            Node(client, 2, noProto=False).readConfig("lora", timeout=0.03)
        assert time.monotonic() - start < 0.5
        assert sent == []
        assert client.queue == {}
        assert client.responseHandlers == {}
    finally:
        client.noProto = True


def test_disabled_protocol_reports_request_failure(client):
    with pytest.raises(RequestError, match="not sent"):
        client.localNode.readConfig("lora")


def test_nested_deadline_cannot_extend_budget_and_resets_context():
    with _operation_deadline(0.01):
        with _operation_deadline(30):
            assert _remaining_timeout(30) <= 0.01
    assert _remaining_timeout(30) == 30


def test_module_preference_reads_defaults_enums_and_nested_messages(
    client, monkeypatch
):
    def send(packet, *args, **kwargs):
        raw_request = admin_pb2.AdminMessage.FromString(packet.decoded.payload)
        is_module = raw_request.HasField("get_module_config_request")
        response = response_packet(
            packet, module=is_module, section="mqtt" if is_module else "lora"
        )
        raw = response["decoded"]["admin"]["raw"]
        if is_module:
            raw.get_module_config_response.mqtt.map_report_settings.publish_interval_secs = (
                123
            )
        else:
            raw.get_config_response.lora.region = config_pb2.Config.LoRaConfig.US
        deliver(client, response)
        return packet

    monkeypatch.setattr(client, "_send_packet", send)
    remote = Node(client, 2, noProto=False)
    assert remote.readPreference("mqtt.enabled") is False
    assert remote.readPreference("mqtt.mapReportSettings.publishIntervalSecs") == 123
    assert remote.readPreference("lora.region") == config_pb2.Config.LoRaConfig.US


def test_expired_queue_wait_preserves_unrelated_queued_packet(client, monkeypatch):
    client.noProto = False
    client.isConnected.set()
    client.queueStatus = mesh_pb2.QueueStatus(free=0, maxlen=16)
    unrelated = mesh_pb2.ToRadio(packet=mesh_pb2.MeshPacket(id=1234, to=3))
    client.queue[1234] = unrelated
    monkeypatch.setattr(
        client,
        "_send_to_radio_impl",
        lambda packet: pytest.fail("full queue sent a packet"),
    )
    try:
        with pytest.raises(RequestTimeoutError):
            Node(client, 2, noProto=False).readConfig("lora", timeout=0.01)
        assert client.queue == {1234: unrelated}
        assert client.responseHandlers == {}
    finally:
        client.noProto = True
