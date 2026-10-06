"""Tests for firmware nanopb payload-limit enforcement on outbound sends."""

# pylint: disable=protected-access

import threading
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

from meshtastic import BROADCAST_ADDR
from meshtastic._interface_errors import MeshInterfaceError
from meshtastic.mesh_interface_runtime.ports import _SendPipelinePort
from meshtastic.mesh_interface_runtime.send_pipeline import SendPipeline
from meshtastic.payload_limits import _validate_firmware_payload_limits
from meshtastic.protobuf import (
    admin_pb2,
    config_pb2,
    interdevice_pb2,
    mesh_pb2,
    nanopb_pb2,
    portnums_pb2,
)

BEACON_NAME_MAX_SIZE = 12  # nanopb max_size; one byte reserved for the NUL
BEACON_NAME_USABLE = BEACON_NAME_MAX_SIZE - 1
BEACON_PSK_MAX_SIZE = 32
BROADCAST_TARGET_MAX_COUNT = 4


def _staged_beacon_admin_message(name: str) -> admin_pb2.AdminMessage:
    """Build the AdminMessage that writeConfig("mesh_beacon") sends."""
    message = admin_pb2.AdminMessage()
    beacon = message.set_module_config.mesh_beacon
    beacon.flags = 3
    beacon.broadcast_offer_channel.name = name
    beacon.broadcast_offer_channel.psk = bytes(range(0xC0, 0xE0))
    beacon.broadcast_offer_region = cast(
        "config_pb2.Config.LoRaConfig.RegionCode.ValueType", 1
    )
    beacon.broadcast_offer_preset = cast(
        "config_pb2.Config.LoRaConfig.ModemPreset.ValueType", 16
    )
    target = beacon.broadcast_targets.add()
    target.channel_index = 2
    target.region = cast("config_pb2.Config.LoRaConfig.RegionCode.ValueType", 1)
    return message


class TestWalkerStringLimits:
    """String fields are capped at max_size - 1 UTF-8 bytes."""

    @pytest.mark.unit
    @pytest.mark.parametrize("name", ["N" * 255, "é" * 127 + "N"])
    def test_max_length_at_utf8_boundary_passes(self, name: str) -> None:
        """max_length counts UTF-8 bytes without reserving a byte from its cap."""
        message = interdevice_pb2.DirectoryListing(filenames=[name])

        _validate_firmware_payload_limits(message, context="Outbound payload")

    @pytest.mark.unit
    @pytest.mark.parametrize("name", ["N" * 256, "é" * 128])
    def test_max_length_over_utf8_boundary_rejects(self, name: str) -> None:
        """Repeated strings enforce max_length and identify the offending entry."""
        message = interdevice_pb2.DirectoryListing(filenames=["ok", name])

        with pytest.raises(
            MeshInterfaceError,
            match="field 'filenames\\[1\\]' is 256 bytes, exceeding the firmware limit of 255",
        ):
            _validate_firmware_payload_limits(message, context="Outbound payload")

    @pytest.mark.unit
    def test_string_at_cap_passes(self) -> None:
        """A name of exactly the usable length is accepted."""
        message = _staged_beacon_admin_message("N" * BEACON_NAME_USABLE)

        _validate_firmware_payload_limits(message, context="Outbound payload")

    @pytest.mark.unit
    def test_string_over_cap_rejected_with_field_path(self) -> None:
        """An over-cap name is rejected naming the nested field and limits."""
        message = _staged_beacon_admin_message("N" * BEACON_NAME_MAX_SIZE)

        with pytest.raises(MeshInterfaceError) as excinfo:
            _validate_firmware_payload_limits(message, context="Outbound payload")

        text = str(excinfo.value)
        assert "set_module_config.mesh_beacon.broadcast_offer_channel.name" in text
        assert f"{BEACON_NAME_MAX_SIZE} bytes" in text
        assert f"limit of {BEACON_NAME_USABLE} bytes" in text
        assert "silently drop" in text

    @pytest.mark.unit
    def test_string_cap_counts_utf8_bytes_not_characters(self) -> None:
        """Multibyte characters count their encoded bytes, not one each."""
        message = admin_pb2.AdminMessage()
        message.set_channel.settings.name = "Ä" * (BEACON_NAME_USABLE // 2 + 1)

        with pytest.raises(MeshInterfaceError, match="is 12 bytes"):
            _validate_firmware_payload_limits(message, context="Outbound payload")

    @pytest.mark.unit
    def test_fields_without_declared_limits_pass(self) -> None:
        """Fields the schema leaves without nanopb limits never reject."""
        message = admin_pb2.AdminMessage()
        message.get_config_request = admin_pb2.AdminMessage.ConfigType.LORA_CONFIG

        _validate_firmware_payload_limits(message, context="Outbound payload")


class TestWalkerBytesAndCountLimits:
    """Bytes fields cap at max_size; repeated fields cap at max_count."""

    @pytest.mark.unit
    def test_bytes_over_cap_rejected(self) -> None:
        """An oversize psk is rejected naming the field and both limits."""
        message = admin_pb2.AdminMessage()
        message.set_module_config.mesh_beacon.broadcast_offer_channel.psk = bytes(
            BEACON_PSK_MAX_SIZE + 1
        )

        with pytest.raises(MeshInterfaceError) as excinfo:
            _validate_firmware_payload_limits(message, context="Outbound payload")

        text = str(excinfo.value)
        assert "broadcast_offer_channel.psk" in text
        assert f"{BEACON_PSK_MAX_SIZE + 1} bytes" in text
        assert f"limit of {BEACON_PSK_MAX_SIZE} bytes" in text

    @pytest.mark.unit
    def test_bytes_at_cap_passes(self) -> None:
        """A psk of exactly the declared size is accepted."""
        message = admin_pb2.AdminMessage()
        message.set_module_config.mesh_beacon.broadcast_offer_channel.psk = bytes(
            BEACON_PSK_MAX_SIZE
        )

        _validate_firmware_payload_limits(message, context="Outbound payload")

    @pytest.mark.unit
    def test_repeated_bytes_over_cap_names_element_index(self) -> None:
        """An over-cap element of a repeated bytes field names its index."""
        message = admin_pb2.AdminMessage()
        security = message.set_config.security
        security.admin_key.append(b"ok" * 8)
        security.admin_key.append(b"x" * 33)

        with pytest.raises(MeshInterfaceError) as excinfo:
            _validate_firmware_payload_limits(message, context="Outbound payload")

        assert "admin_key[1]" in str(excinfo.value)

    @pytest.mark.unit
    def test_repeated_message_over_count_rejected(self) -> None:
        """More broadcast targets than the firmware count cap is rejected."""
        message = admin_pb2.AdminMessage()
        beacon = message.set_module_config.mesh_beacon
        for index in range(BROADCAST_TARGET_MAX_COUNT + 1):
            target = beacon.broadcast_targets.add()
            target.channel_index = index

        with pytest.raises(MeshInterfaceError) as excinfo:
            _validate_firmware_payload_limits(message, context="Outbound payload")

        text = str(excinfo.value)
        assert "broadcast_targets" in text
        assert f"{BROADCAST_TARGET_MAX_COUNT + 1} entries" in text
        assert f"limit of {BROADCAST_TARGET_MAX_COUNT}" in text

    @pytest.mark.unit
    def test_repeated_message_at_count_passes(self) -> None:
        """Broadcast targets up to the firmware count cap are accepted."""
        message = admin_pb2.AdminMessage()
        beacon = message.set_module_config.mesh_beacon
        for index in range(BROADCAST_TARGET_MAX_COUNT):
            target = beacon.broadcast_targets.add()
            target.channel_index = index

        _validate_firmware_payload_limits(message, context="Outbound payload")


class TestWalkerFixedLengthBytes:
    """Fixed-length nanopb bytes fields require their exact wire length."""

    @pytest.mark.unit
    def test_nonempty_macaddr_with_wrong_length_rejected(self) -> None:
        """A present MAC shorter than its fixed six-byte buffer is invalid."""
        message = mesh_pb2.User(macaddr=b"12345")

        with pytest.raises(MeshInterfaceError) as excinfo:
            _validate_firmware_payload_limits(message, context="Outbound payload")

        text = str(excinfo.value)
        assert "field 'macaddr' is 5 bytes" in text
        assert "requires exactly 6 bytes" in text

    @pytest.mark.unit
    def test_macaddr_at_fixed_length_passes(self) -> None:
        """A present MAC matching the six-byte buffer is accepted."""
        message = mesh_pb2.User(macaddr=b"123456")

        _validate_firmware_payload_limits(message, context="Outbound payload")


class TestWalkerIntegerWidthLimits:
    """Integer fields honor nanopb int_size storage widths."""

    @pytest.mark.unit
    def test_unsigned_integer_over_width_rejected(self) -> None:
        """An 8-bit uint32 field rejects values the firmware cannot decode."""
        message = mesh_pb2.MeshPacket(channel=256)

        with pytest.raises(MeshInterfaceError) as excinfo:
            _validate_firmware_payload_limits(message, context="Outbound payload")

        text = str(excinfo.value)
        assert "field 'channel' has value 256" in text
        assert "8-bit unsigned range 0..255" in text

    @pytest.mark.unit
    def test_unsigned_integer_at_width_passes(self) -> None:
        """The maximum value of an 8-bit uint32 field is accepted."""
        message = mesh_pb2.MeshPacket(channel=255)

        _validate_firmware_payload_limits(message, context="Outbound payload")

    @pytest.mark.unit
    def test_repeated_signed_integer_under_width_rejected_with_index(self) -> None:
        """A narrowed repeated int32 identifies the out-of-range element."""
        message = mesh_pb2.RouteDiscovery()
        message.snr_towards.extend([-128, -129])

        with pytest.raises(MeshInterfaceError) as excinfo:
            _validate_firmware_payload_limits(message, context="Outbound payload")

        text = str(excinfo.value)
        assert "field 'snr_towards[1]' has value -129" in text
        assert "8-bit signed range -128..127" in text


def _map_payload(*, constrained: bool = True) -> Any:
    """Build an application payload with scalar and message-valued maps."""
    field_schema = descriptor_pb2.FieldDescriptorProto
    schema = descriptor_pb2.FileDescriptorProto(
        name="payload_limits_map_test.proto",
        package="payload_limits_test",
        syntax="proto3",
    )
    schema.dependency.append(nanopb_pb2.DESCRIPTOR.name)
    item = schema.message_type.add(name="Item")
    name = item.field.add(
        name="name",
        number=1,
        type=field_schema.TYPE_STRING,
        label=field_schema.LABEL_OPTIONAL,
    )
    if constrained:
        name.options.Extensions[nanopb_pb2.nanopb].max_length = 3
    payload = schema.message_type.add(name="Payload")
    for index, (field_name, key_type, value_type) in enumerate(
        [
            ("strings", field_schema.TYPE_STRING, field_schema.TYPE_STRING),
            ("messages", field_schema.TYPE_INT32, field_schema.TYPE_MESSAGE),
        ],
        start=1,
    ):
        entry_name = f"{field_name.title()}Entry"
        entry = payload.nested_type.add(name=entry_name)
        entry.options.map_entry = True
        key = entry.field.add(
            name="key", number=1, type=key_type, label=field_schema.LABEL_OPTIONAL
        )
        value = entry.field.add(
            name="value", number=2, type=value_type, label=field_schema.LABEL_OPTIONAL
        )
        if value_type == field_schema.TYPE_MESSAGE:
            value.type_name = ".payload_limits_test.Item"
        field = payload.field.add(
            name=field_name,
            number=index,
            type=field_schema.TYPE_MESSAGE,
            label=field_schema.LABEL_REPEATED,
            type_name=f".payload_limits_test.Payload.{entry_name}",
        )
        if constrained:
            field.options.Extensions[nanopb_pb2.nanopb].max_count = 2
            if key_type == field_schema.TYPE_STRING:
                key.options.Extensions[nanopb_pb2.nanopb].max_length = 3
                value.options.Extensions[nanopb_pb2.nanopb].max_length = 3
            else:
                key.options.Extensions[nanopb_pb2.nanopb].int_size = nanopb_pb2.IS_8
    pool = descriptor_pool.DescriptorPool()
    pool.AddSerializedFile(descriptor_pb2.DESCRIPTOR.serialized_pb)
    pool.AddSerializedFile(nanopb_pb2.DESCRIPTOR.serialized_pb)
    pool.Add(schema)
    descriptor = pool.FindMessageTypeByName("payload_limits_test.Payload")
    return message_factory.GetMessageClass(descriptor)()


class TestWalkerMapLimits:
    """Application maps preserve serialization and validate entry constraints."""

    @pytest.mark.unit
    def test_unconstrained_maps_pass(self) -> None:
        """Maps without nanopb options accept scalar and message values."""
        message = _map_payload(constrained=False)
        message.strings["arbitrary key"] = "arbitrary value"
        message.messages[256].name = "arbitrary nested value"

        _validate_firmware_payload_limits(message, context="Outbound payload")

    @pytest.mark.unit
    def test_map_entries_at_limits_pass(self) -> None:
        """Map counts, UTF-8 strings, and integer keys accept their boundaries."""
        message = _map_payload()
        message.strings.update({"éa": "éa", "two": "two"})
        message.messages[-128].name = "éa"
        message.messages[127].name = "two"

        _validate_firmware_payload_limits(message, context="Outbound payload")

    @pytest.mark.unit
    def test_map_count_over_limit_rejected(self) -> None:
        """Map entry counts enforce their repeated-field nanopb allocation."""
        message = _map_payload()
        message.strings.update({"one": "one", "two": "two", "tri": "tri"})

        with pytest.raises(MeshInterfaceError, match="field 'strings' has 3 entries"):
            _validate_firmware_payload_limits(message, context="Outbound payload")

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("key", "value", "expected_path"),
        [("éé", "ok", "strings['éé'].key"), ("ok", "éé", "strings['ok'].value")],
    )
    def test_scalar_map_entry_limit_identifies_key(
        self, key: str, value: str, expected_path: str
    ) -> None:
        """UTF-8 key/value failures name the owning map entry."""
        message = _map_payload()
        message.strings[key] = value

        with pytest.raises(MeshInterfaceError) as excinfo:
            _validate_firmware_payload_limits(message, context="Outbound payload")

        assert expected_path in str(excinfo.value)
        assert "is 4 bytes" in str(excinfo.value)

    @pytest.mark.unit
    def test_message_map_value_limit_identifies_key(self) -> None:
        """Nested message constraints include the map key in diagnostics."""
        message = _map_payload()
        message.messages[1].name = "long"

        with pytest.raises(MeshInterfaceError) as excinfo:
            _validate_firmware_payload_limits(message, context="Outbound payload")

        assert "messages[1].value.name" in str(excinfo.value)

    @pytest.mark.unit
    def test_integer_map_key_width_rejected(self) -> None:
        """Nanopb integer widths apply to map keys as well as ordinary fields."""
        message = _map_payload()
        message.messages[128].name = "ok"

        with pytest.raises(MeshInterfaceError) as excinfo:
            _validate_firmware_payload_limits(message, context="Outbound payload")

        assert "messages[128].key" in str(excinfo.value)
        assert "8-bit signed range -128..127" in str(excinfo.value)


@pytest.fixture
def mock_interface() -> Any:
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

    class MeshInterfaceError(Exception):
        """Custom exception for MeshInterface errors."""

        def __init__(self, message: str) -> None:
            """Initialize the error with a message."""
            self.message = message
            super().__init__(message)

    interface.MeshInterfaceError = MeshInterfaceError
    return interface


@pytest.fixture
def send_pipeline(mock_interface: Any) -> SendPipeline:
    """Create a SendPipeline instance with mocked interface."""
    return SendPipeline(_SendPipelinePort(mock_interface))


class TestSendPipelineEnforcement:
    """The send funnel rejects over-limit payloads before transmission."""

    def test_application_map_payload_reaches_queue(
        self, send_pipeline: SendPipeline
    ) -> None:
        """Typed custom application payloads with maps remain sendable."""
        message = _map_payload(constrained=False)
        message.strings["sample"] = "value"
        expected_bytes = message.SerializeToString()

        with patch.object(send_pipeline._port.facade, "_send_packet") as send_packet:
            send_pipeline._send_data_with_wait(
                message, BROADCAST_ADDR, portNum=portnums_pb2.PortNum.PRIVATE_APP
            )

        send_packet.assert_called_once()
        assert send_packet.call_args.args[0].decoded.payload == expected_bytes

    def test_over_limit_beacon_write_rejected_before_queue(
        self,
        send_pipeline: SendPipeline,
    ) -> None:
        """A beacon write with a 12-byte offer-channel name fails loudly."""
        message = _staged_beacon_admin_message("N" * BEACON_NAME_MAX_SIZE)

        with patch.object(
            send_pipeline._port.facade, "_send_packet"
        ) as mock_send_packet:
            with pytest.raises(
                MeshInterfaceError, match="broadcast_offer_channel.name"
            ):
                send_pipeline._send_data_with_wait(
                    message,
                    BROADCAST_ADDR,
                    portNum=portnums_pb2.PortNum.ADMIN_APP,
                    wantAck=True,
                )

        mock_send_packet.assert_not_called()

    def test_beacon_write_at_limit_reaches_queue(
        self,
        send_pipeline: SendPipeline,
        mock_interface: Any,
    ) -> None:
        """The identical write with an 11-byte name proceeds to transmission."""
        message = _staged_beacon_admin_message("N" * BEACON_NAME_USABLE)

        with patch.object(
            send_pipeline._port.facade, "_send_packet"
        ) as mock_send_packet:
            mock_send_packet.return_value = MagicMock()
            send_pipeline._send_data_with_wait(
                message,
                BROADCAST_ADDR,
                portNum=portnums_pb2.PortNum.ADMIN_APP,
                wantAck=True,
            )

        mock_send_packet.assert_called_once()

    def test_over_limit_channel_write_rejected(
        self,
        send_pipeline: SendPipeline,
    ) -> None:
        """A channel snapshot write with a 12-byte name fails loudly."""
        message = admin_pb2.AdminMessage()
        message.set_channel.settings.name = "N" * BEACON_NAME_MAX_SIZE

        with pytest.raises(MeshInterfaceError, match="set_channel.settings.name"):
            send_pipeline._send_data_with_wait(
                message,
                BROADCAST_ADDR,
                portNum=portnums_pb2.PortNum.ADMIN_APP,
                wantAck=True,
            )

    def test_over_limit_waypoint_payload_rejected(
        self,
        send_pipeline: SendPipeline,
    ) -> None:
        """Typed app payloads obey their own nested field caps."""
        waypoint = mesh_pb2.Waypoint()
        waypoint.name = "x" * 31

        with pytest.raises(MeshInterfaceError, match="field 'name' is 31 bytes"):
            send_pipeline._send_data_with_wait(
                waypoint,
                BROADCAST_ADDR,
                portNum=portnums_pb2.PortNum.WAYPOINT_APP,
            )

    def test_over_limit_mqtt_proxy_topic_rejected(
        self,
        send_pipeline: SendPipeline,
    ) -> None:
        """MQTT client-proxy topics beyond the firmware cap fail loudly."""
        with pytest.raises(MeshInterfaceError, match="field 'topic' is 61 bytes"):
            send_pipeline.send_mqtt_client_proxy_message("m" * 61, b"payload")

    def test_mqtt_proxy_message_at_limit_reaches_queue(
        self,
        send_pipeline: SendPipeline,
        mock_interface: Any,
    ) -> None:
        """MQTT client-proxy messages within caps are transmitted."""
        with patch.object(send_pipeline, "_send_to_radio") as mock_send_to_radio:
            send_pipeline.send_mqtt_client_proxy_message("m" * 59, b"payload")

        mock_send_to_radio.assert_called_once()
