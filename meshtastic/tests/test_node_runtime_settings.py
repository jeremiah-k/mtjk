"""Meshtastic unit tests for node_runtime/settings_runtime.py."""

# pylint: disable=redefined-outer-name

import logging
import threading
from types import MethodType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from google.protobuf.descriptor import FieldDescriptor

from .._interface_errors import MeshInterfaceError
from ..node_runtime.settings_runtime import (
    _NodeAdminCommandRuntime,
    _NodeOwnerProfileRuntime,
    _NodeSettingsMessageBuilder,
    _NodeSettingsResponseRuntime,
    _NodeSettingsRuntime,
)
from ..node_runtime.shared import (
    EMPTY_LONG_NAME_MSG,
    EMPTY_SHORT_NAME_MSG,
    MAX_LONG_NAME_LEN,
    MAX_SHORT_NAME_LEN,
)
from ..protobuf import admin_pb2, config_pb2, localonly_pb2
from ..util import Acknowledgment


class _TestInterfaceError(Exception):
    """Sentinel exception raised by interface-error test doubles."""


def _raise_test_error(msg: str) -> None:
    """Raise an exception with the provided message."""
    raise _TestInterfaceError(msg)


@pytest.fixture
def mock_iface() -> MagicMock:
    """Create a minimal mock interface for settings runtime tests."""
    iface = MagicMock()
    iface._acknowledgment = Acknowledgment()
    iface.localNode = None  # Will be set by individual tests
    iface.waitForAckNak = MagicMock()
    return iface


@pytest.fixture
def mock_node_for_settings(mock_iface: MagicMock) -> MagicMock:
    """Create a minimal mock Node for settings message builder tests."""
    node = MagicMock(
        spec=[
            "iface",
            "localConfig",
            "moduleConfig",
            "_send_admin",
            "_raise_interface_error",
            "onResponseRequestSettings",
            "onAckNak",
            "ensureSessionKey",
        ]
    )
    node.iface = mock_iface
    node.localConfig = localonly_pb2.LocalConfig()
    node.moduleConfig = localonly_pb2.LocalModuleConfig()
    node._send_admin = MagicMock(return_value=MagicMock())
    node._raise_interface_error = MagicMock(side_effect=_raise_test_error)
    node.onResponseRequestSettings = MagicMock()
    node.onAckNak = MagicMock()
    node.ensureSessionKey = MagicMock()
    return node


@pytest.fixture
def mock_local_node(
    mock_node_for_settings: MagicMock, mock_iface: MagicMock
) -> MagicMock:
    """Create a mock local Node (node == iface.localNode)."""
    mock_iface.localNode = mock_node_for_settings
    return mock_node_for_settings


@pytest.fixture
def mock_remote_node(
    mock_node_for_settings: MagicMock, mock_iface: MagicMock
) -> MagicMock:
    """Create a mock remote Node (node != iface.localNode)."""
    mock_iface.localNode = MagicMock()  # Different from mock_node_for_settings
    return mock_node_for_settings


class TestNodeSettingsMessageBuilder:
    """Tests for _NodeSettingsMessageBuilder."""

    @pytest.mark.unit
    @pytest.mark.parametrize("config_type_int", [0, 123])
    def test_build_request_message_with_int_sets_get_config_request(
        self, mock_node_for_settings: MagicMock, config_type_int: int
    ) -> None:
        """build_request_message with int config_type sets get_config_request."""
        builder = _NodeSettingsMessageBuilder(mock_node_for_settings)
        result = builder.build_request_message(config_type_int)

        assert result.get_config_request == config_type_int

    @pytest.mark.unit
    def test_build_request_message_rejects_descriptor_without_containing_message(
        self, mock_node_for_settings: MagicMock
    ) -> None:
        """An unbound field descriptor fails with a configuration diagnostic."""
        builder = _NodeSettingsMessageBuilder(mock_node_for_settings)
        field = MagicMock(spec=FieldDescriptor)
        field.name = "device"
        field.containing_type = None

        with pytest.raises(ValueError, match="device has no containing message"):
            builder.build_request_message(field)

    @pytest.mark.unit
    def test_build_request_message_with_local_config_field_descriptor(
        self, mock_node_for_settings: MagicMock
    ) -> None:
        """build_request_message with LocalConfig FieldDescriptor sets get_config_request."""
        builder = _NodeSettingsMessageBuilder(mock_node_for_settings)
        # Get a field descriptor from LocalConfig
        device_field = localonly_pb2.LocalConfig.DESCRIPTOR.fields_by_name["device"]

        result = builder.build_request_message(device_field)

        assert (
            result.get_config_request == admin_pb2.AdminMessage.ConfigType.DEVICE_CONFIG
        )

    @pytest.mark.unit
    def test_build_request_message_with_module_config_field_descriptor(
        self, mock_node_for_settings: MagicMock
    ) -> None:
        """build_request_message with ModuleConfig FieldDescriptor sets get_module_config_request."""
        builder = _NodeSettingsMessageBuilder(mock_node_for_settings)
        # Get a field descriptor from LocalModuleConfig
        mqtt_field = localonly_pb2.LocalModuleConfig.DESCRIPTOR.fields_by_name["mqtt"]

        result = builder.build_request_message(mqtt_field)

        # Module config uses index
        assert result.get_module_config_request == mqtt_field.index

    @pytest.mark.unit
    def test_get_write_config_entry_returns_entry_for_valid_config(
        self, mock_node_for_settings: MagicMock
    ) -> None:
        """get_write_config_entry returns dispatch entry for valid config name."""
        builder = _NodeSettingsMessageBuilder(mock_node_for_settings)

        entry = builder.get_write_config_entry("device")

        assert entry is not None
        assert entry[0] == "set_config"
        assert entry[1] == mock_node_for_settings.localConfig.device

    @pytest.mark.unit
    def test_get_write_config_entry_returns_none_for_invalid_config(
        self, mock_node_for_settings: MagicMock
    ) -> None:
        """get_write_config_entry returns None for invalid config name."""
        builder = _NodeSettingsMessageBuilder(mock_node_for_settings)

        entry = builder.get_write_config_entry("nonexistent")

        assert entry is None

    @pytest.mark.unit
    def test_get_write_config_entry_returns_module_entry(
        self, mock_node_for_settings: MagicMock
    ) -> None:
        """get_write_config_entry returns module config entry for module config names."""
        builder = _NodeSettingsMessageBuilder(mock_node_for_settings)

        entry = builder.get_write_config_entry("mqtt")

        assert entry is not None
        assert entry[0] == "set_module_config"
        assert entry[1] == mock_node_for_settings.moduleConfig.mqtt

    @pytest.mark.unit
    @pytest.mark.skipif(
        not hasattr(localonly_pb2.LocalConfig(), "sessionkey"),
        reason="sessionkey field not present in current protobuf schema",
    )
    def test_get_write_config_entry_includes_optional_sessionkey(
        self, mock_node_for_settings: MagicMock
    ) -> None:
        """get_write_config_entry includes sessionkey if present in localConfig."""
        builder = _NodeSettingsMessageBuilder(mock_node_for_settings)
        mock_node_for_settings.localConfig.sessionkey.CopyFrom(
            localonly_pb2.LocalConfig.SessionkeyConfig()  # type: ignore[attr-defined]
        )

        entry = builder.get_write_config_entry("sessionkey")

        assert entry is not None
        assert entry[0] == "set_config"

    @pytest.mark.unit
    @pytest.mark.skipif(
        not hasattr(localonly_pb2.LocalConfig, "DeviceUIConfig"),
        reason="DeviceUIConfig not present in protobuf schema",
    )
    def test_get_write_config_entry_includes_optional_device_ui(
        self, mock_node_for_settings: MagicMock
    ) -> None:
        """get_write_config_entry includes device_ui if present in localConfig."""
        builder = _NodeSettingsMessageBuilder(mock_node_for_settings)
        mock_node_for_settings.localConfig.device_ui.CopyFrom(
            localonly_pb2.LocalConfig.DeviceUIConfig()  # type: ignore[attr-defined]
        )

        entry = builder.get_write_config_entry("device_ui")

        assert entry is not None
        assert entry[0] == "set_config"

    @pytest.mark.unit
    def test_build_write_message_with_valid_config_builds_message(
        self, mock_node_for_settings: MagicMock
    ) -> None:
        """build_write_message with valid config builds set_config message."""
        builder = _NodeSettingsMessageBuilder(mock_node_for_settings)
        # Set some config values
        mock_node_for_settings.localConfig.device.role = (
            config_pb2.Config.DeviceConfig.Role.CLIENT
        )

        result = builder.build_write_message("device")

        assert result.HasField("set_config")
        assert result.set_config.HasField("device")

    @pytest.mark.unit
    def test_build_write_message_with_invalid_config_raises_error(
        self, mock_node_for_settings: MagicMock
    ) -> None:
        """build_write_message with invalid config raises interface error."""
        builder = _NodeSettingsMessageBuilder(mock_node_for_settings)

        with pytest.raises(
            _TestInterfaceError, match="No valid config with name nonexistent"
        ):
            builder.build_write_message("nonexistent")

    @pytest.mark.unit
    def test_validate_config_name_valid_name_does_not_raise(
        self, mock_node_for_settings: MagicMock
    ) -> None:
        """validate_config_name with valid name does not raise."""
        builder = _NodeSettingsMessageBuilder(mock_node_for_settings)

        # Should not raise
        builder.validate_config_name("device")

    @pytest.mark.unit
    def test_validate_config_name_invalid_name_raises_error(
        self, mock_node_for_settings: MagicMock
    ) -> None:
        """validate_config_name with invalid name raises interface error."""
        builder = _NodeSettingsMessageBuilder(mock_node_for_settings)

        with pytest.raises(
            _TestInterfaceError, match="No valid config with name invalid"
        ):
            builder.validate_config_name("invalid")


class TestNodeSettingsRuntime:
    """Tests for _NodeSettingsRuntime."""

    @pytest.mark.unit
    def test_request_config_local_node_sends_with_response_callback(
        self, mock_local_node: MagicMock
    ) -> None:
        """request_config on local node registers the settings response handler.

        Without the registered handler the device's config response is never
        correlated for the local node, so a cleared section stays at protobuf
        defaults and later comparisons read manufactured values.
        """
        iface = mock_local_node.iface
        scoped_waits: list[int] = []
        iface._extract_request_id_from_sent_packet = MagicMock(return_value=7)
        iface._has_active_wait_request = MethodType(
            lambda self, acknowledgment_attr, request_id: False, iface
        )
        iface._wait_for_ack_nak = MethodType(
            lambda self, request_id: scoped_waits.append(request_id), iface
        )
        mock_local_node._send_admin = MagicMock(return_value=SimpleNamespace(id=7))
        builder = _NodeSettingsMessageBuilder(mock_local_node)
        runtime = _NodeSettingsRuntime(mock_local_node, message_builder=builder)

        runtime.request_config(admin_pb2.AdminMessage.ConfigType.DEVICE_CONFIG)

        mock_local_node._send_admin.assert_called_once()
        call_kwargs = mock_local_node._send_admin.call_args[1]
        assert call_kwargs["wantResponse"] is True
        assert call_kwargs["onResponse"] is mock_local_node.onResponseRequestSettings
        assert scoped_waits == []
        iface.waitForAckNak.assert_not_called()

    @pytest.mark.unit
    def test_request_config_local_node_waits_via_scoped_bookkeeping(
        self, mock_local_node: MagicMock
    ) -> None:
        """When scoped ACK bookkeeping is registered, the local settings
        request waits on the request-scoped helper instead of the legacy
        interface ACK wait."""
        scoped_waits: list[int] = []
        iface = mock_local_node.iface
        iface._wait_for_ack_nak = MethodType(
            lambda self, request_id: scoped_waits.append(request_id), iface
        )
        iface._has_active_wait_request = MethodType(
            lambda self, acknowledgment_attr, request_id: True, iface
        )
        iface._extract_request_id_from_sent_packet = MagicMock(return_value=7)
        mock_local_node._send_admin = MagicMock(return_value=SimpleNamespace(id=7))

        builder = _NodeSettingsMessageBuilder(mock_local_node)
        runtime = _NodeSettingsRuntime(mock_local_node, message_builder=builder)

        runtime.request_config(admin_pb2.AdminMessage.ConfigType.DEVICE_CONFIG)

        assert scoped_waits == [7]
        iface.waitForAckNak.assert_not_called()

    @pytest.mark.unit
    def test_request_config_local_node_applies_response_to_local_config(
        self, mock_local_node: MagicMock
    ) -> None:
        """A local get_config response must repopulate the cleared section.

        Regression test for the real-device configure failure where the
        no-disconnect verification refresh cleared requested sections and
        fired local requestConfig calls whose responses were dropped, leaving
        network.wifi_enabled/wifi_ssid/wifi_psk reading as defaults.
        """
        response_runtime = _NodeSettingsResponseRuntime(mock_local_node)
        mock_local_node.onResponseRequestSettings.side_effect = (
            response_runtime.handle_settings_response
        )

        def deliver_response(_message: Any, **kwargs: Any) -> MagicMock:
            on_response = kwargs.get("onResponse")
            assert on_response is not None
            raw = admin_pb2.AdminMessage()
            raw.get_config_response.device.CopyFrom(mock_local_node.localConfig.device)
            raw.get_config_response.device.node_info_broadcast_secs = 10800
            on_response(
                {
                    "decoded": {
                        "admin": {
                            "getConfigResponse": {"device": {}},
                            "raw": raw,
                        }
                    }
                }
            )
            return MagicMock()

        mock_local_node._send_admin.side_effect = deliver_response

        builder = _NodeSettingsMessageBuilder(mock_local_node)
        runtime = _NodeSettingsRuntime(mock_local_node, message_builder=builder)
        runtime.request_config(admin_pb2.AdminMessage.ConfigType.DEVICE_CONFIG)

        assert mock_local_node.localConfig.device.node_info_broadcast_secs == 10800

    @pytest.mark.unit
    def test_request_config_remote_node_sends_with_callback(
        self, mock_remote_node: MagicMock
    ) -> None:
        """request_config on remote node sends with onResponse callback and waits for ACK."""
        builder = _NodeSettingsMessageBuilder(mock_remote_node)
        runtime = _NodeSettingsRuntime(mock_remote_node, message_builder=builder)

        runtime.request_config(admin_pb2.AdminMessage.ConfigType.DEVICE_CONFIG)

        mock_remote_node._send_admin.assert_called_once()
        call_kwargs = mock_remote_node._send_admin.call_args[1]
        assert call_kwargs["wantResponse"] is True
        assert call_kwargs["onResponse"] == mock_remote_node.onResponseRequestSettings
        mock_remote_node.iface.waitForAckNak.assert_called_once()

    @pytest.mark.unit
    def test_request_config_remote_node_raises_error_when_send_returns_none(
        self, mock_remote_node: MagicMock
    ) -> None:
        """request_config should raise error when _send_admin returns None."""
        mock_remote_node._send_admin.return_value = None
        builder = _NodeSettingsMessageBuilder(mock_remote_node)
        runtime = _NodeSettingsRuntime(mock_remote_node, message_builder=builder)

        with pytest.raises(
            _TestInterfaceError, match="requestConfig failed: admin message not started"
        ):
            runtime.request_config(admin_pb2.AdminMessage.ConfigType.DEVICE_CONFIG)

        mock_remote_node._send_admin.assert_called_once()
        mock_remote_node.iface.waitForAckNak.assert_not_called()

    @pytest.mark.unit
    def test_request_config_with_admin_index_passes_through(
        self, mock_local_node: MagicMock
    ) -> None:
        """request_config passes admin_index to _send_admin."""
        builder = _NodeSettingsMessageBuilder(mock_local_node)
        runtime = _NodeSettingsRuntime(mock_local_node, message_builder=builder)

        runtime.request_config(
            admin_pb2.AdminMessage.ConfigType.DEVICE_CONFIG, admin_index=2
        )

        call_kwargs = mock_local_node._send_admin.call_args[1]
        assert call_kwargs["adminIndex"] == 2

    @pytest.mark.unit
    def test_validate_write_configs_loaded_raises_for_invalid_config(
        self, mock_local_node: MagicMock
    ) -> None:
        """_validate_write_configs_loaded raises for invalid config name."""
        builder = _NodeSettingsMessageBuilder(mock_local_node)
        runtime = _NodeSettingsRuntime(mock_local_node, message_builder=builder)

        with pytest.raises(
            _TestInterfaceError, match="No valid config with name invalid"
        ):
            runtime._validate_write_configs_loaded("invalid")

    @pytest.mark.unit
    def test_validate_write_configs_loaded_passes_with_populated_config(
        self, mock_local_node: MagicMock
    ) -> None:
        """_validate_write_configs_loaded passes when source config has fields."""
        builder = _NodeSettingsMessageBuilder(mock_local_node)
        runtime = _NodeSettingsRuntime(mock_local_node, message_builder=builder)
        # Set a field in device config
        mock_local_node.localConfig.device.role = (
            config_pb2.Config.DeviceConfig.Role.CLIENT
        )

        # Should not raise
        runtime._validate_write_configs_loaded("device")

    @pytest.mark.unit
    def test_validate_write_configs_loaded_passes_with_other_config_populated(
        self, mock_local_node: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """_validate_write_configs_loaded passes when other config has fields (compatibility)."""
        builder = _NodeSettingsMessageBuilder(mock_local_node)
        runtime = _NodeSettingsRuntime(mock_local_node, message_builder=builder)
        # Set a field in a different config (not device)
        mock_local_node.localConfig.lora.region = (
            config_pb2.Config.LoRaConfig.RegionCode.US
        )

        with caplog.at_level(logging.DEBUG):
            runtime._validate_write_configs_loaded("device")

        # Should log compatibility message
        assert "Writing device with empty payload" in caplog.text

    @pytest.mark.unit
    def test_validate_write_configs_loaded_raises_with_no_configs(
        self, mock_local_node: MagicMock
    ) -> None:
        """_validate_write_configs_loaded raises when no configs have been read."""
        builder = _NodeSettingsMessageBuilder(mock_local_node)
        runtime = _NodeSettingsRuntime(mock_local_node, message_builder=builder)
        # localConfig and moduleConfig are empty

        with pytest.raises(_TestInterfaceError, match="No config has been read"):
            runtime._validate_write_configs_loaded("device")

    @pytest.mark.unit
    def test_write_config_local_node_sends_without_callback(
        self, mock_local_node: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """write_config on local node sends without onResponse callback."""
        builder = _NodeSettingsMessageBuilder(mock_local_node)
        runtime = _NodeSettingsRuntime(mock_local_node, message_builder=builder)
        mock_local_node.localConfig.device.role = (
            config_pb2.Config.DeviceConfig.Role.CLIENT
        )

        with caplog.at_level(logging.DEBUG):
            runtime.write_config("device")

        assert "Config write completed: device" in caplog.text
        mock_local_node._send_admin.assert_called_once()
        call_kwargs = mock_local_node._send_admin.call_args[1]
        assert call_kwargs["onResponse"] is None

    @pytest.mark.unit
    def test_write_config_remote_node_sends_with_callback_and_waits(
        self, mock_remote_node: MagicMock
    ) -> None:
        """write_config on remote node sends with onResponse callback and waits for ACK."""
        builder = _NodeSettingsMessageBuilder(mock_remote_node)
        runtime = _NodeSettingsRuntime(mock_remote_node, message_builder=builder)
        mock_remote_node.localConfig.device.role = (
            config_pb2.Config.DeviceConfig.Role.CLIENT
        )

        runtime.write_config("device")

        mock_remote_node._send_admin.assert_called_once()
        call_kwargs = mock_remote_node._send_admin.call_args[1]
        assert call_kwargs["onResponse"] == mock_remote_node.onAckNak
        mock_remote_node.iface.waitForAckNak.assert_called_once()

    @pytest.mark.unit
    def test_write_config_remote_node_raises_error_when_send_returns_none(
        self, mock_remote_node: MagicMock
    ) -> None:
        """write_config should raise error when _send_admin returns None."""
        builder = _NodeSettingsMessageBuilder(mock_remote_node)
        runtime = _NodeSettingsRuntime(mock_remote_node, message_builder=builder)
        mock_remote_node.localConfig.device.role = (
            config_pb2.Config.DeviceConfig.Role.CLIENT
        )
        mock_remote_node._send_admin = MagicMock(return_value=None)

        with pytest.raises(
            _TestInterfaceError, match="writeConfig failed: admin message not started"
        ):
            runtime.write_config("device")

        mock_remote_node.iface.waitForAckNak.assert_not_called()


class TestNodeSettingsResponseRuntime:
    """Tests for _NodeSettingsResponseRuntime."""

    @pytest.fixture
    def mock_node_for_response(self, mock_iface: MagicMock) -> MagicMock:
        """Create a mock Node for response tests with config objects."""
        node = MagicMock(
            spec=[
                "iface",
                "localConfig",
                "moduleConfig",
            ]
        )
        node.iface = mock_iface
        node.localConfig = localonly_pb2.LocalConfig()
        node.moduleConfig = localonly_pb2.LocalModuleConfig()
        return node

    @pytest.mark.unit
    def test_handle_settings_response_missing_decoded_returns_early(
        self, mock_node_for_response: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """handle_settings_response with missing decoded returns early."""
        runtime = _NodeSettingsResponseRuntime(mock_node_for_response)
        packet: dict[str, Any] = {}

        with caplog.at_level(logging.WARNING):
            runtime.handle_settings_response(packet)

        assert "malformed settings response (missing decoded)" in caplog.text
        assert mock_node_for_response.iface._acknowledgment.receivedNak is True

    @pytest.mark.unit
    def test_handle_settings_response_routing_error_sets_nak(
        self, mock_node_for_response: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """handle_settings_response with routing error sets Nak."""
        runtime = _NodeSettingsResponseRuntime(mock_node_for_response)
        packet: dict[str, Any] = {
            "decoded": {
                "routing": {"errorReason": "NO_RESPONSE"},
            }
        }

        with caplog.at_level(logging.ERROR):
            runtime.handle_settings_response(packet)

        assert "Error on response: NO_RESPONSE" in caplog.text
        assert mock_node_for_response.iface._acknowledgment.receivedNak is True

    @pytest.mark.unit
    def test_handle_settings_response_routing_no_error_processes_admin_payload(
        self, mock_node_for_response: MagicMock
    ) -> None:
        """handle_settings_response with routing NONE should continue to admin payload handling."""
        runtime = _NodeSettingsResponseRuntime(mock_node_for_response)
        raw = admin_pb2.AdminMessage()
        raw.get_config_response.device.role = config_pb2.Config.DeviceConfig.Role.CLIENT
        packet: dict[str, Any] = {
            "decoded": {
                "routing": {"errorReason": "NONE"},
                "admin": {
                    "getConfigResponse": {"device": {}},
                    "raw": raw,
                },
            }
        }

        runtime.handle_settings_response(packet)

        assert mock_node_for_response.iface._acknowledgment.receivedNak is False
        assert mock_node_for_response.iface._acknowledgment.receivedAck is True

    @pytest.mark.unit
    def test_handle_settings_response_missing_admin_returns_early(
        self, mock_node_for_response: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """handle_settings_response with missing admin returns early."""
        runtime = _NodeSettingsResponseRuntime(mock_node_for_response)
        packet: dict[str, Any] = {"decoded": {}}

        with caplog.at_level(logging.WARNING):
            runtime.handle_settings_response(packet)

        assert "malformed settings response (missing admin)" in caplog.text
        assert mock_node_for_response.iface._acknowledgment.receivedNak is True

    @pytest.mark.unit
    def test_handle_settings_response_empty_config_response_logs_warning(
        self, mock_node_for_response: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """handle_settings_response with empty config response logs warning (lines 223-224)."""
        runtime = _NodeSettingsResponseRuntime(mock_node_for_response)
        packet: dict[str, Any] = {
            "decoded": {"admin": {"getConfigResponse": {}}}  # Empty response
        }

        with caplog.at_level(logging.WARNING):
            runtime.handle_settings_response(packet)

        assert "Received empty config response from node" in caplog.text
        assert mock_node_for_response.iface._acknowledgment.receivedNak is True

    @pytest.mark.unit
    def test_handle_settings_response_empty_module_config_response_logs_warning(
        self, mock_node_for_response: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """handle_settings_response with empty module config response logs warning (lines 247-259)."""
        runtime = _NodeSettingsResponseRuntime(mock_node_for_response)
        packet: dict[str, Any] = {
            "decoded": {"admin": {"getModuleConfigResponse": {}}}  # Empty response
        }

        with caplog.at_level(logging.WARNING):
            runtime.handle_settings_response(packet)

        assert "Received empty module config response from node" in caplog.text
        assert mock_node_for_response.iface._acknowledgment.receivedNak is True

    @pytest.mark.unit
    def test_handle_settings_response_unknown_local_config_field_logs_warning(
        self, mock_node_for_response: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """_resolve_local_config_target with unknown field logs warning."""
        runtime = _NodeSettingsResponseRuntime(mock_node_for_response)
        admin_message: dict[str, Any] = {"getConfigResponse": {"UnknownField": {}}}

        with caplog.at_level(logging.WARNING):
            result = runtime._resolve_local_config_target(admin_message)

        assert result is None
        assert "Ignoring unknown LocalConfig field" in caplog.text

    @pytest.mark.unit
    def test_handle_settings_response_unknown_module_config_field_logs_warning(
        self, mock_node_for_response: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """_resolve_module_config_target with unknown field logs warning (lines 247-259)."""
        runtime = _NodeSettingsResponseRuntime(mock_node_for_response)
        admin_message: dict[str, Any] = {
            "getModuleConfigResponse": {"UnknownField": {}}
        }

        with caplog.at_level(logging.WARNING):
            result = runtime._resolve_module_config_target(admin_message)

        assert result is None
        assert "Ignoring unknown ModuleConfig field" in caplog.text

    @pytest.mark.unit
    def test_resolve_config_target_returns_local_target(
        self, mock_node_for_response: MagicMock
    ) -> None:
        """_resolve_config_target returns local target when present (line 276)."""
        runtime = _NodeSettingsResponseRuntime(mock_node_for_response)
        # Create a valid local config response
        raw = admin_pb2.AdminMessage()
        raw.get_config_response.device.role = config_pb2.Config.DeviceConfig.Role.CLIENT
        admin_message: dict[str, Any] = {
            "getConfigResponse": {"device": {}},
            "raw": raw,
        }

        result = runtime._resolve_config_target(admin_message)

        assert result is not None
        assert result[0] == "get_config_response"
        assert result[1] == "device"

    @pytest.mark.unit
    def test_resolve_config_target_returns_module_target(
        self, mock_node_for_response: MagicMock
    ) -> None:
        """_resolve_config_target returns module target when present (line 276)."""
        runtime = _NodeSettingsResponseRuntime(mock_node_for_response)
        # Create a valid module config response
        raw = admin_pb2.AdminMessage()
        raw.get_module_config_response.mqtt.enabled = True
        admin_message: dict[str, Any] = {
            "getModuleConfigResponse": {"mqtt": {}},
            "raw": raw,
        }

        result = runtime._resolve_config_target(admin_message)

        assert result is not None
        assert result[0] == "get_module_config_response"
        assert result[1] == "mqtt"

    @pytest.mark.unit
    def test_handle_settings_response_missing_admin_raw_logs_warning(
        self, mock_node_for_response: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """handle_settings_response with missing admin.raw logs warning (lines 316-320)."""
        runtime = _NodeSettingsResponseRuntime(mock_node_for_response)
        packet: dict[str, Any] = {
            "decoded": {
                "admin": {
                    "getConfigResponse": {"device": {}}
                    # Missing "raw"
                }
            }
        }

        with caplog.at_level(logging.WARNING):
            runtime.handle_settings_response(packet)

        assert "malformed settings response (invalid admin.raw)" in caplog.text
        assert mock_node_for_response.iface._acknowledgment.receivedNak is True

    @pytest.mark.unit
    def test_handle_settings_response_valid_sets_ack(
        self, mock_node_for_response: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """handle_settings_response with valid response sets ACK and copies config."""
        runtime = _NodeSettingsResponseRuntime(mock_node_for_response)

        raw = admin_pb2.AdminMessage()
        raw.get_config_response.device.role = config_pb2.Config.DeviceConfig.Role.CLIENT

        packet: dict[str, Any] = {
            "decoded": {
                "admin": {
                    "getConfigResponse": {"device": {}},
                    "raw": raw,
                }
            }
        }

        with caplog.at_level(logging.INFO):
            runtime.handle_settings_response(packet)

        assert mock_node_for_response.iface._acknowledgment.receivedAck is True
        assert "Received settings block: device" in caplog.text
        # Verify config was copied
        assert (
            mock_node_for_response.localConfig.device.role
            == config_pb2.Config.DeviceConfig.Role.CLIENT
        )

    @pytest.mark.unit
    def test_handle_settings_response_session_key_variant_acks_without_applying(
        self, mock_node_for_response: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A SESSIONKEY_CONFIG response completes the wait without a fabricated NAK.

        The device answers with a ``Config`` payload variant, which maps to no
        ``LocalConfig`` field; the response must be acknowledged and must not
        fail ``ensureSessionKey`` with a "recognized config field" error.
        """
        runtime = _NodeSettingsResponseRuntime(mock_node_for_response)
        # A stale NAK from an earlier failed exchange must not survive the
        # ack: legacy unscoped waiters read one coherent outcome.
        mock_node_for_response.iface._acknowledgment.receivedNak = True

        raw = admin_pb2.AdminMessage()
        raw.get_config_response.sessionkey.SetInParent()

        packet: dict[str, Any] = {
            "decoded": {
                "admin": {
                    "getConfigResponse": {"sessionkey": {}},
                    "raw": raw,
                }
            }
        }

        with caplog.at_level(logging.INFO):
            runtime.handle_settings_response(packet)

        assert mock_node_for_response.iface._acknowledgment.receivedAck is True
        assert mock_node_for_response.iface._acknowledgment.receivedNak is False
        assert len(mock_node_for_response.localConfig.ListFields()) == 0
        assert "Received session-key config response" in caplog.text


class TestNodeAdminCommandRuntime:
    """Tests for _NodeAdminCommandRuntime."""

    @pytest.fixture
    def mock_node_for_admin(self, mock_iface: MagicMock) -> MagicMock:
        """Create a mock Node for admin command tests."""
        node = MagicMock(
            spec=[
                "iface",
                "_send_admin",
                "ensureSessionKey",
                "onAckNak",
                "_raise_interface_error",
                "_get_factory_reset_request_value",
            ]
        )
        node.iface = mock_iface
        node._send_admin = MagicMock(return_value=MagicMock())
        node.ensureSessionKey = MagicMock()
        node.onAckNak = MagicMock()
        node._raise_interface_error = MagicMock(side_effect=_raise_test_error)
        node._get_factory_reset_request_value = MagicMock(return_value=1)
        return node

    @pytest.fixture
    def mock_local_node_for_admin(
        self, mock_node_for_admin: MagicMock, mock_iface: MagicMock
    ) -> MagicMock:
        """Create a mock local Node for admin tests."""
        mock_iface.localNode = mock_node_for_admin
        return mock_node_for_admin

    @pytest.mark.unit
    def test_reboot_sends_command_with_seconds(
        self, mock_local_node_for_admin: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Reboot sends reboot_seconds command."""
        runtime = _NodeAdminCommandRuntime(mock_local_node_for_admin)

        with caplog.at_level(logging.INFO):
            result = runtime.reboot(10)

        assert result is not None
        assert "Telling node to reboot in 10 seconds" in caplog.text
        mock_local_node_for_admin.ensureSessionKey.assert_called_once()
        # Local node does not wait for ACK (callback is None for local node)
        mock_local_node_for_admin.iface.waitForAckNak.assert_not_called()

    @pytest.mark.unit
    def test_factory_reset_full_sends_factory_reset_device(
        self, mock_local_node_for_admin: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """factory_reset with full=True sends factory_reset_device command."""
        runtime = _NodeAdminCommandRuntime(mock_local_node_for_admin)

        with caplog.at_level(logging.INFO):
            result = runtime.factory_reset(full=True)

        assert result is not None
        assert "factory reset (full device reset)" in caplog.text
        mock_local_node_for_admin._get_factory_reset_request_value.assert_called_once()
        mock_local_node_for_admin._send_admin.assert_called_once()
        assert (
            mock_local_node_for_admin._send_admin.call_args.kwargs["onResponse"]
            is mock_local_node_for_admin.onAckNak
        )
        assert (
            mock_local_node_for_admin._send_admin.call_args.kwargs["responseWaitAttr"]
            == "receivedNak"
        )
        mock_local_node_for_admin.iface.waitForAckNak.assert_not_called()

    @pytest.mark.unit
    def test_factory_reset_config_sends_factory_reset_config(
        self, mock_local_node_for_admin: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """factory_reset with full=False sends factory_reset_config command."""
        runtime = _NodeAdminCommandRuntime(mock_local_node_for_admin)

        with caplog.at_level(logging.INFO):
            result = runtime.factory_reset(full=False)

        assert result is not None
        assert "factory reset (config reset)" in caplog.text
        mock_local_node_for_admin._get_factory_reset_request_value.assert_called_once()
        mock_local_node_for_admin._send_admin.assert_called_once()
        assert (
            mock_local_node_for_admin._send_admin.call_args.kwargs["onResponse"]
            is mock_local_node_for_admin.onAckNak
        )
        assert (
            mock_local_node_for_admin._send_admin.call_args.kwargs["responseWaitAttr"]
            == "receivedNak"
        )
        mock_local_node_for_admin.iface.waitForAckNak.assert_not_called()

    @pytest.mark.unit
    def test_begin_settings_transaction_sends_command(
        self, mock_local_node_for_admin: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """begin_settings_transaction sends begin_edit_settings command (lines 391-416)."""
        runtime = _NodeAdminCommandRuntime(mock_local_node_for_admin)

        with caplog.at_level(logging.INFO):
            result = runtime.begin_settings_transaction()

        assert result is not None
        assert "open a transaction to edit settings" in caplog.text
        mock_local_node_for_admin.ensureSessionKey.assert_called_once()
        # Local node does not wait for ACK (callback is None for local node)
        mock_local_node_for_admin.iface.waitForAckNak.assert_not_called()

    @pytest.mark.unit
    def test_commit_settings_transaction_sends_command(
        self, mock_local_node_for_admin: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """commit_settings_transaction sends commit_edit_settings command (lines 391-416)."""
        runtime = _NodeAdminCommandRuntime(mock_local_node_for_admin)

        with caplog.at_level(logging.INFO):
            result = runtime.commit_settings_transaction()

        assert result is not None
        assert "commit open transaction" in caplog.text
        mock_local_node_for_admin.ensureSessionKey.assert_called_once()
        # Local node does not wait for ACK (callback is None for local node)
        mock_local_node_for_admin.iface.waitForAckNak.assert_not_called()

    @pytest.mark.unit
    def test_reboot_remote_node_waits_for_ack(
        self,
        mock_node_for_admin: MagicMock,
        mock_iface: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Reboot on remote node waits for ACK/NAK."""
        mock_iface.localNode = MagicMock()  # Different from mock_node_for_admin
        runtime = _NodeAdminCommandRuntime(mock_node_for_admin)

        with caplog.at_level(logging.INFO):
            result = runtime.reboot(10)

        assert result is not None
        mock_node_for_admin.ensureSessionKey.assert_called_once()
        # Remote node waits for ACK
        mock_node_for_admin.iface.waitForAckNak.assert_called_once()

    @pytest.mark.unit
    def test_begin_settings_transaction_remote_node_waits_for_ack(
        self,
        mock_node_for_admin: MagicMock,
        mock_iface: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """begin_settings_transaction on remote node waits for ACK/NAK."""
        mock_iface.localNode = MagicMock()  # Different from mock_node_for_admin
        runtime = _NodeAdminCommandRuntime(mock_node_for_admin)

        with caplog.at_level(logging.INFO):
            result = runtime.begin_settings_transaction()

        assert result is not None
        mock_node_for_admin.ensureSessionKey.assert_called_once()
        # Remote node waits for ACK
        mock_node_for_admin.iface.waitForAckNak.assert_called_once()

    @pytest.mark.unit
    def test_commit_settings_transaction_remote_node_waits_for_ack(
        self,
        mock_node_for_admin: MagicMock,
        mock_iface: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """commit_settings_transaction on remote node waits for ACK/NAK."""
        mock_iface.localNode = MagicMock()  # Different from mock_node_for_admin
        runtime = _NodeAdminCommandRuntime(mock_node_for_admin)

        with caplog.at_level(logging.INFO):
            result = runtime.commit_settings_transaction()

        assert result is not None
        mock_node_for_admin.ensureSessionKey.assert_called_once()
        # Remote node waits for ACK
        mock_node_for_admin.iface.waitForAckNak.assert_called_once()

    @pytest.mark.unit
    def test_resolve_ota_hash_no_hash_raises_type_error(self) -> None:
        """_resolve_ota_hash with no hash raises TypeError (lines 441-479)."""
        with pytest.raises(TypeError, match="missing required argument"):
            _NodeAdminCommandRuntime._resolve_ota_hash(
                ota_file_hash=None,
                ota_hash=None,
                legacy_hash=None,
            )

    @pytest.mark.unit
    def test_resolve_ota_hash_conflicting_hashes_raises_value_error(self) -> None:
        """_resolve_ota_hash with conflicting hashes raises ValueError (lines 441-479)."""
        with pytest.raises(ValueError, match="Conflicting OTA hash arguments"):
            _NodeAdminCommandRuntime._resolve_ota_hash(
                ota_file_hash=b"hash1",
                ota_hash=b"hash2",
                legacy_hash=None,
            )

    @pytest.mark.unit
    def test_resolve_ota_hash_non_bytes_raises_type_error(self) -> None:
        """_resolve_ota_hash with non-bytes hash raises TypeError (lines 441-479)."""
        with pytest.raises(TypeError, match="must be bytes"):
            _NodeAdminCommandRuntime._resolve_ota_hash(
                ota_file_hash="not_bytes",  # type: ignore[arg-type]
                ota_hash=None,
                legacy_hash=None,
            )

    @pytest.mark.unit
    def test_resolve_ota_hash_returns_valid_hash(self) -> None:
        """_resolve_ota_hash returns the provided hash bytes."""
        result = _NodeAdminCommandRuntime._resolve_ota_hash(
            ota_file_hash=b"valid_hash",
            ota_hash=None,
            legacy_hash=None,
        )

        assert result == b"valid_hash"

    @pytest.mark.unit
    def test_resolve_ota_hash_uses_ota_hash(self) -> None:
        """_resolve_ota_hash uses ota_hash when ota_file_hash is None."""
        result = _NodeAdminCommandRuntime._resolve_ota_hash(
            ota_file_hash=None,
            ota_hash=b"ota_hash_value",
            legacy_hash=None,
        )

        assert result == b"ota_hash_value"

    @pytest.mark.unit
    def test_resolve_ota_hash_uses_legacy_hash(self) -> None:
        """_resolve_ota_hash uses legacy hash when others are None."""
        result = _NodeAdminCommandRuntime._resolve_ota_hash(
            ota_file_hash=None,
            ota_hash=None,
            legacy_hash=b"legacy_hash_value",
        )

        assert result == b"legacy_hash_value"

    @pytest.mark.unit
    def test_start_ota_remote_node_raises_error(
        self, mock_node_for_admin: MagicMock, mock_iface: MagicMock
    ) -> None:
        """start_OTA on remote node raises error."""
        mock_iface.localNode = MagicMock()  # Different from mock_node_for_admin
        runtime = _NodeAdminCommandRuntime(mock_node_for_admin)

        with pytest.raises(
            _TestInterfaceError, match="startOTA only possible on local node"
        ):
            runtime.start_ota(
                mode=admin_pb2.OTAMode.OTA_WIFI,
                ota_file_hash=b"hash",
            )

    @pytest.mark.unit
    def test_start_ota_no_mode_raises_type_error(
        self, mock_local_node_for_admin: MagicMock
    ) -> None:
        """start_OTA without mode raises TypeError."""
        runtime = _NodeAdminCommandRuntime(mock_local_node_for_admin)

        with pytest.raises(TypeError, match=r"missing required argument.*mode"):
            runtime.start_ota(
                mode=None,
                ota_file_hash=b"hash",
            )

    @pytest.mark.unit
    def test_start_ota_no_hash_raises_type_error(
        self, mock_local_node_for_admin: MagicMock
    ) -> None:
        """start_OTA without hash raises TypeError."""
        runtime = _NodeAdminCommandRuntime(mock_local_node_for_admin)

        with pytest.raises(
            TypeError, match=r"missing required argument.*ota_file_hash"
        ):
            runtime.start_ota(
                mode=admin_pb2.OTAMode.OTA_WIFI,
                ota_file_hash=None,
            )

    @pytest.mark.unit
    def test_start_ota_conflicting_modes_raises_value_error(
        self, mock_local_node_for_admin: MagicMock
    ) -> None:
        """start_OTA with conflicting modes raises ValueError."""
        runtime = _NodeAdminCommandRuntime(mock_local_node_for_admin)

        with pytest.raises(ValueError, match="Conflicting OTA mode arguments"):
            runtime.start_ota(
                mode=admin_pb2.OTAMode.OTA_WIFI,
                ota_mode=admin_pb2.OTAMode.OTA_BLE,
                ota_file_hash=b"hash",
            )

    @pytest.mark.unit
    def test_start_ota_unexpected_kwargs_raises_type_error(
        self, mock_local_node_for_admin: MagicMock
    ) -> None:
        """start_OTA with unexpected kwargs raises TypeError."""
        runtime = _NodeAdminCommandRuntime(mock_local_node_for_admin)

        with pytest.raises(TypeError, match="unexpected keyword argument"):
            runtime.start_ota(
                mode=admin_pb2.OTAMode.OTA_WIFI,
                ota_file_hash=b"hash",
                unknown_arg="value",
            )

    @pytest.mark.unit
    def test_start_ota_valid_sends_ota_request(
        self, mock_local_node_for_admin: MagicMock
    ) -> None:
        """start_OTA with valid mode and hash sends OTA request (lines 441-479)."""
        runtime = _NodeAdminCommandRuntime(mock_local_node_for_admin)

        result = runtime.start_ota(
            mode=admin_pb2.OTAMode.OTA_WIFI,
            ota_file_hash=b"test_hash_bytes",
        )

        assert result is not None
        mock_local_node_for_admin._send_admin.assert_called_once()
        # Verify the message has the right fields set
        call_args = mock_local_node_for_admin._send_admin.call_args
        message = call_args[0][0]
        assert message.ota_request.reboot_ota_mode == admin_pb2.OTAMode.OTA_WIFI
        assert message.ota_request.ota_hash == b"test_hash_bytes"

    @pytest.mark.unit
    def test_start_ota_uses_legacy_hash_kwarg(
        self, mock_local_node_for_admin: MagicMock
    ) -> None:
        """start_OTA uses legacy 'hash' kwarg from extra_kwargs."""
        runtime = _NodeAdminCommandRuntime(mock_local_node_for_admin)

        result = runtime.start_ota(
            mode=admin_pb2.OTAMode.OTA_WIFI,
            ota_file_hash=None,
            hash=b"legacy_hash",
        )

        assert result is not None
        call_args = mock_local_node_for_admin._send_admin.call_args
        message = call_args[0][0]
        assert message.ota_request.ota_hash == b"legacy_hash"

    @pytest.mark.unit
    def test_set_ignored_with_int_node_id(
        self, mock_local_node_for_admin: MagicMock
    ) -> None:
        """set_ignored with int node_id converts to node_num (lines 578-581)."""
        runtime = _NodeAdminCommandRuntime(mock_local_node_for_admin)

        with patch(
            "meshtastic.node_runtime.settings_runtime.admin.toNodeNum",
            return_value=12345,
        ) as mock_to_node_num:
            result = runtime.set_ignored(0x12345)

        mock_to_node_num.assert_called_once_with(0x12345)
        assert result is not None
        call_args = mock_local_node_for_admin._send_admin.call_args
        message = call_args[0][0]
        assert message.set_ignored_node == 12345

    @pytest.mark.unit
    def test_set_ignored_with_str_node_id(
        self, mock_local_node_for_admin: MagicMock
    ) -> None:
        """set_ignored with str node_id converts to node_num (lines 578-581)."""
        runtime = _NodeAdminCommandRuntime(mock_local_node_for_admin)

        with patch(
            "meshtastic.node_runtime.settings_runtime.admin.toNodeNum",
            return_value=0x9388F81C,
        ) as mock_to_node_num:
            result = runtime.set_ignored("!9388f81c")

        mock_to_node_num.assert_called_once_with("!9388f81c")
        assert result is not None

    @pytest.mark.unit
    def test_send_node_id_command_converts_node_id(
        self, mock_local_node_for_admin: MagicMock
    ) -> None:
        """_send_node_id_command converts node_id to node_num (line 533)."""
        runtime = _NodeAdminCommandRuntime(mock_local_node_for_admin)

        set_field = MagicMock()

        with patch(
            "meshtastic.node_runtime.settings_runtime.admin.toNodeNum", return_value=999
        ) as mock_to_node_num:
            runtime._send_node_id_command(
                node_id="!abc123",
                set_field=set_field,
            )

        mock_to_node_num.assert_called_once_with("!abc123")
        set_field.assert_called_once()
        # Verify the message was created and set_field was called with message and node_num
        call_args = set_field.call_args
        assert isinstance(call_args[0][0], admin_pb2.AdminMessage)
        assert call_args[0][1] == 999

    @pytest.mark.unit
    def test_send_command_ensures_session_key_when_requested(
        self, mock_local_node_for_admin: MagicMock
    ) -> None:
        """_send_command ensures session key when ensure_session_key=True."""
        runtime = _NodeAdminCommandRuntime(mock_local_node_for_admin)
        message = admin_pb2.AdminMessage()

        runtime._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=False,
        )

        mock_local_node_for_admin.ensureSessionKey.assert_called_once()

    @pytest.mark.unit
    def test_send_command_uses_remote_callback_for_remote_node(
        self, mock_node_for_admin: MagicMock, mock_iface: MagicMock
    ) -> None:
        """_send_command uses remote callback when node is not local."""
        mock_iface.localNode = MagicMock()  # Different from mock_node_for_admin
        runtime = _NodeAdminCommandRuntime(mock_node_for_admin)
        message = admin_pb2.AdminMessage()

        runtime._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=True,
        )

        mock_node_for_admin.iface.waitForAckNak.assert_called_once()


class TestNodeOwnerProfileRuntime:
    """Tests for _NodeOwnerProfileRuntime."""

    @pytest.fixture
    def mock_node_for_owner(self, mock_iface: MagicMock) -> MagicMock:
        """Create a mock Node for owner profile tests."""
        node = MagicMock(
            spec=[
                "iface",
                "nodeNum",
                "_raise_interface_error",
            ]
        )
        node.iface = mock_iface
        node.nodeNum = 0x12345678
        node._raise_interface_error = MagicMock(side_effect=_raise_test_error)
        return node

    @pytest.fixture
    def mock_runtime_for_owner(
        self, mock_node_for_owner: MagicMock
    ) -> _NodeOwnerProfileRuntime:
        """Create a _NodeOwnerProfileRuntime with mocked admin_command_runtime."""
        admin_runtime = MagicMock()
        admin_runtime.send_owner_message = MagicMock(return_value=MagicMock())
        return _NodeOwnerProfileRuntime(
            mock_node_for_owner,
            admin_command_runtime=admin_runtime,
        )

    @pytest.mark.unit
    def test_set_owner_truncates_long_name(
        self,
        mock_runtime_for_owner: _NodeOwnerProfileRuntime,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """set_owner truncates long_name to MAX_LONG_NAME_LEN characters (lines 618-619)."""
        long_name = "A" * 50  # Longer than MAX_LONG_NAME_LEN (40)

        with caplog.at_level(logging.WARNING):
            mock_runtime_for_owner.set_owner(long_name=long_name)

        assert "Long name is longer than" in caplog.text
        assert "truncating" in caplog.text
        # Verify the message was sent with truncated name
        call_args = (
            mock_runtime_for_owner._admin_command_runtime.send_owner_message.call_args  # type: ignore[attr-defined]
        )
        message = call_args[0][0]
        assert len(message.set_owner.long_name) == MAX_LONG_NAME_LEN
        assert message.set_owner.long_name == "A" * MAX_LONG_NAME_LEN

    @pytest.mark.unit
    def test_set_owner_truncates_short_name(
        self,
        mock_runtime_for_owner: _NodeOwnerProfileRuntime,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """set_owner truncates short_name to MAX_SHORT_NAME_LEN characters (lines 618-619)."""
        short_name = "LongShort"  # Longer than MAX_SHORT_NAME_LEN (4)

        with caplog.at_level(logging.WARNING):
            mock_runtime_for_owner.set_owner(short_name=short_name)

        assert "Short name is longer than" in caplog.text
        assert "truncating" in caplog.text
        # Verify the message was sent with truncated name
        call_args = (
            mock_runtime_for_owner._admin_command_runtime.send_owner_message.call_args  # type: ignore[attr-defined]
        )
        message = call_args[0][0]
        assert len(message.set_owner.short_name) == MAX_SHORT_NAME_LEN
        assert message.set_owner.short_name == "Long"

    @pytest.mark.unit
    def test_set_owner_empty_long_name_raises_error(
        self, mock_runtime_for_owner: _NodeOwnerProfileRuntime
    ) -> None:
        """set_owner with empty long_name raises error."""
        with pytest.raises(_TestInterfaceError, match=EMPTY_LONG_NAME_MSG):
            mock_runtime_for_owner.set_owner(long_name="")

    @pytest.mark.unit
    def test_set_owner_whitespace_long_name_raises_error(
        self, mock_runtime_for_owner: _NodeOwnerProfileRuntime
    ) -> None:
        """set_owner with whitespace-only long_name raises error."""
        with pytest.raises(_TestInterfaceError, match=EMPTY_LONG_NAME_MSG):
            mock_runtime_for_owner.set_owner(long_name="   ")

    @pytest.mark.unit
    def test_set_owner_empty_short_name_raises_error(
        self, mock_runtime_for_owner: _NodeOwnerProfileRuntime
    ) -> None:
        """set_owner with empty short_name raises error."""
        with pytest.raises(_TestInterfaceError, match=EMPTY_SHORT_NAME_MSG):
            mock_runtime_for_owner.set_owner(short_name="")

    @pytest.mark.unit
    def test_set_owner_whitespace_short_name_raises_error(
        self, mock_runtime_for_owner: _NodeOwnerProfileRuntime
    ) -> None:
        """set_owner with whitespace-only short_name raises error."""
        with pytest.raises(_TestInterfaceError, match=EMPTY_SHORT_NAME_MSG):
            mock_runtime_for_owner.set_owner(short_name="   ")

    @pytest.mark.unit
    def test_set_owner_strips_whitespace(
        self, mock_runtime_for_owner: _NodeOwnerProfileRuntime
    ) -> None:
        """set_owner strips whitespace from names before validation."""
        mock_runtime_for_owner.set_owner(long_name="  ValidName  ", short_name=" AB ")

        call_args = (
            mock_runtime_for_owner._admin_command_runtime.send_owner_message.call_args  # type: ignore[attr-defined]
        )
        message = call_args[0][0]
        assert message.set_owner.long_name == "ValidName"
        assert message.set_owner.short_name == "AB"

    @pytest.mark.unit
    def test_set_owner_sets_is_licensed(
        self, mock_runtime_for_owner: _NodeOwnerProfileRuntime
    ) -> None:
        """set_owner sets is_licensed flag."""
        mock_runtime_for_owner.set_owner(long_name="Test", is_licensed=True)

        call_args = (
            mock_runtime_for_owner._admin_command_runtime.send_owner_message.call_args  # type: ignore[attr-defined]
        )
        message = call_args[0][0]
        assert message.set_owner.is_licensed is True

    @pytest.mark.unit
    def test_set_owner_sets_is_unmessagable(
        self, mock_runtime_for_owner: _NodeOwnerProfileRuntime
    ) -> None:
        """set_owner sets is_unmessagable flag when provided."""
        mock_runtime_for_owner.set_owner(long_name="Test", is_unmessagable=True)

        call_args = (
            mock_runtime_for_owner._admin_command_runtime.send_owner_message.call_args  # type: ignore[attr-defined]
        )
        message = call_args[0][0]
        assert message.set_owner.is_unmessagable is True

    @pytest.mark.unit
    def test_set_owner_no_unmessagable_when_not_provided(
        self, mock_runtime_for_owner: _NodeOwnerProfileRuntime
    ) -> None:
        """set_owner does not set is_unmessagable when None."""
        mock_runtime_for_owner.set_owner(long_name="Test", is_unmessagable=None)

        call_args = (
            mock_runtime_for_owner._admin_command_runtime.send_owner_message.call_args  # type: ignore[attr-defined]
        )
        message = call_args[0][0]
        assert not message.set_owner.HasField("is_unmessagable")

    @pytest.mark.unit
    def test_set_owner_with_all_params(
        self,
        mock_runtime_for_owner: _NodeOwnerProfileRuntime,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """set_owner with all parameters sets all fields."""
        with caplog.at_level(logging.DEBUG):
            mock_runtime_for_owner.set_owner(
                long_name="TestUser",
                short_name="TEST",
                is_licensed=True,
                is_unmessagable=False,
            )

        call_args = (
            mock_runtime_for_owner._admin_command_runtime.send_owner_message.call_args  # type: ignore[attr-defined]
        )
        message = call_args[0][0]
        assert message.set_owner.long_name == "TestUser"
        assert message.set_owner.short_name == "TEST"
        assert message.set_owner.is_licensed is True
        assert message.set_owner.HasField("is_unmessagable")
        assert message.set_owner.is_unmessagable is False
        # Check debug logs (PII redacted - only boolean indicators)
        assert "p.set_owner.long_name_set:True" in caplog.text
        assert "p.set_owner.short_name_set:True" in caplog.text

    @pytest.mark.unit
    def test_set_owner_only_long_name(
        self, mock_runtime_for_owner: _NodeOwnerProfileRuntime
    ) -> None:
        """set_owner with only long_name sets only that field."""
        mock_runtime_for_owner.set_owner(long_name="OnlyLong")

        call_args = (
            mock_runtime_for_owner._admin_command_runtime.send_owner_message.call_args  # type: ignore[attr-defined]
        )
        message = call_args[0][0]
        assert message.set_owner.long_name == "OnlyLong"
        # short_name should be empty (not set)
        assert message.set_owner.short_name == ""

    @pytest.mark.unit
    def test_set_owner_only_short_name(
        self, mock_runtime_for_owner: _NodeOwnerProfileRuntime
    ) -> None:
        """set_owner with only short_name sets only that field."""
        mock_runtime_for_owner.set_owner(short_name="ABC")

        call_args = (
            mock_runtime_for_owner._admin_command_runtime.send_owner_message.call_args  # type: ignore[attr-defined]
        )
        message = call_args[0][0]
        assert message.set_owner.short_name == "ABC"
        # long_name should be empty (not set)
        assert message.set_owner.long_name == ""


class TestWriteConfigVerification:
    """Read-back verification for writeConfig."""

    @staticmethod
    def _verified_node() -> Any:
        """Create a local-node double with a controllable read-back response."""
        node = MagicMock(
            spec=[
                "iface",
                "localConfig",
                "moduleConfig",
                "_send_admin",
                "_raise_interface_error",
                "onResponseRequestSettings",
                "onAckNak",
                "ensureSessionKey",
                "requestConfig",
                "noProto",
            ]
        )
        node.iface = MagicMock()
        node.iface.localNode = node
        node.localConfig = localonly_pb2.LocalConfig()
        node.moduleConfig = localonly_pb2.LocalModuleConfig()
        node._send_admin = MagicMock(return_value=MagicMock())
        node._raise_interface_error = MagicMock(side_effect=_raise_test_error)
        node.onResponseRequestSettings = _NodeSettingsResponseRuntime(
            node
        ).handle_settings_response
        node.onAckNak = MagicMock()
        node.ensureSessionKey = MagicMock()
        node.requestConfig = MagicMock()
        node.noProto = False
        return node

    @staticmethod
    def _beacon_runtime(node: Any) -> _NodeSettingsRuntime:
        """Create the settings runtime under test for one node double."""
        return _NodeSettingsRuntime(
            node,
            message_builder=_NodeSettingsMessageBuilder(node),
        )

    @pytest.mark.unit
    @pytest.mark.parametrize("verify", [False, True])
    @pytest.mark.parametrize("remote", [False, True])
    def test_invalid_write_rejected_before_session_requests_or_cache_changes(
        self, verify: bool, remote: bool
    ) -> None:
        """Preflight invalid config before session preparation can touch transport/state."""
        node = self._verified_node()
        if remote:
            node.iface.localNode = MagicMock()
        node.moduleConfig.mesh_beacon.broadcast_offer_channel.name = "N" * 12
        before = node.moduleConfig.SerializeToString()
        node.ensureSessionKey.side_effect = lambda: setattr(
            node.moduleConfig.mqtt, "enabled", True
        )

        with pytest.raises(MeshInterfaceError, match="broadcast_offer_channel.name"):
            self._beacon_runtime(node).write_config("mesh_beacon", verify=verify)

        node.ensureSessionKey.assert_not_called()
        node._send_admin.assert_not_called()
        node.requestConfig.assert_not_called()
        node.iface.waitForAckNak.assert_not_called()
        assert node.moduleConfig.SerializeToString() == before

    @staticmethod
    def _device_reports(
        node: Any,
        flags_value: int | None,
        *,
        on_response: Any,
        delay_seconds: float = 0.0,
    ) -> threading.Timer | None:
        """Schedule the device response a verification read-back would get.

        The request-specific callback receives a real admin payload and applies
        it through the ordinary settings response handler.
        """

        def _apply_and_deliver() -> None:
            raw = admin_pb2.AdminMessage()
            if flags_value is not None:
                raw.get_module_config_response.mesh_beacon.flags = flags_value
            on_response(
                {
                    "decoded": {
                        "admin": {
                            "raw": raw,
                            "getModuleConfigResponse": {
                                "meshBeacon": {"flags": flags_value}
                            },
                        }
                    }
                }
            )

        if delay_seconds > 0:
            timer = threading.Timer(delay_seconds, _apply_and_deliver)
            timer.daemon = True
            timer.start()
            return timer
        _apply_and_deliver()
        return None

    @classmethod
    def _verify_beacon(cls, node: Any, *, verify_timeout: float) -> None:
        """Run one verified beacon write under patched wait budgets."""
        runtime = cls._beacon_runtime(node)
        with (
            patch.object(runtime, "request_config", node.requestConfig),
            patch(
                (
                    "meshtastic.node_runtime.settings_runtime.config_runtime."
                    "CONFIG_VERIFY_TIMEOUT_SECONDS"
                ),
                verify_timeout,
            ),
            patch(
                (
                    "meshtastic.node_runtime.settings_runtime.config_runtime."
                    "CONFIG_VERIFY_POLL_INTERVAL_SECONDS"
                ),
                0.01,
            ),
        ):
            runtime.write_config("mesh_beacon", verify=True)

    @pytest.mark.unit
    def test_verify_requests_section_and_passes_when_device_reports_staged_values(
        self,
    ) -> None:
        """The section is re-requested and staged values pass verification."""
        node = self._verified_node()
        node.moduleConfig.mesh_beacon.flags = 7
        node.requestConfig.side_effect = (
            lambda _descriptor, **kwargs: self._device_reports(node, 7, **kwargs)
        )
        self._verify_beacon(node, verify_timeout=1.0)

        node.requestConfig.assert_called_once()
        requested_descriptor = node.requestConfig.call_args[0][0]
        assert requested_descriptor.containing_type.name == "LocalModuleConfig"
        assert requested_descriptor.name == "mesh_beacon"

    @pytest.mark.unit
    def test_verify_waits_for_a_delayed_matching_response(self) -> None:
        """Verification passes only once a delayed response repopulates."""
        node = self._verified_node()
        node.moduleConfig.mesh_beacon.flags = 7
        timer: threading.Timer | None = None

        def _deliver(_descriptor: Any, **kwargs: Any) -> None:
            nonlocal timer
            timer = self._device_reports(node, 7, delay_seconds=0.05, **kwargs)

        node.requestConfig.side_effect = _deliver
        try:
            self._verify_beacon(node, verify_timeout=1.0)
        finally:
            if timer is not None:
                timer.cancel()
                timer.join()

        assert node.moduleConfig.mesh_beacon.flags == 7

    @pytest.mark.unit
    def test_verify_fails_when_device_reports_different_values(self) -> None:
        """A dropped write is detected because read-back reports other values."""
        node = self._verified_node()
        node.moduleConfig.mesh_beacon.flags = 7
        # The device never applied the write; its read-back reports defaults.
        node.requestConfig.side_effect = (
            lambda _descriptor, **kwargs: self._device_reports(node, 0, **kwargs)
        )
        self._verify_failing_beacon(node)

        # The device-reported values stay in the cache after a mismatch.
        assert node.moduleConfig.mesh_beacon.flags == 0

    @pytest.mark.unit
    def test_verify_fails_when_read_back_never_arrives(self) -> None:
        """A read-back with no response fails and restores the staged values."""
        node = self._verified_node()
        node.moduleConfig.mesh_beacon.flags = 7
        node.requestConfig.side_effect = (
            lambda _descriptor, **kwargs: self._device_reports(node, None, **kwargs)
        )
        self._verify_failing_beacon(node)

        assert node.moduleConfig.mesh_beacon.flags == 7

    @pytest.mark.unit
    def test_verify_fails_for_all_default_staged_section_without_device_response(
        self,
    ) -> None:
        """An unrelated response cannot satisfy an all-default staged section.

        A cleared section and an all-default section both serialize empty, so
        byte equality alone would accept verification before the device ever
        answers; the correlated-response gate must reject this.
        """
        node = self._verified_node()
        # Some other staged section satisfies the loaded-state guard while
        # the verified mesh_beacon section itself stays all-default.
        node.moduleConfig.mqtt.enabled = True
        node.requestConfig.side_effect = (
            lambda _descriptor, **kwargs: self._device_reports(node, None, **kwargs)
        )
        self._verify_failing_beacon(node)

    @classmethod
    def _verify_failing_beacon(cls, node: Any) -> None:
        """Assert one verified beacon write fails under short wait budgets."""
        with pytest.raises(_TestInterfaceError, match="verification"):
            cls._verify_beacon(node, verify_timeout=0.2)

    @pytest.mark.unit
    def test_verify_restores_sent_values_when_request_config_raises(self) -> None:
        """A failed read-back request cannot leave the staged cache cleared."""
        node = self._verified_node()
        node.moduleConfig.mesh_beacon.flags = 7
        node.requestConfig.side_effect = _TestInterfaceError("read-back failed")

        with pytest.raises(_TestInterfaceError, match="read-back failed"):
            self._verify_beacon(node, verify_timeout=1.0)

        assert node.moduleConfig.mesh_beacon.flags == 7

    @pytest.mark.unit
    def test_verify_preserves_device_values_when_request_config_raises_after_response(
        self,
    ) -> None:
        """A response received before request failure remains authoritative."""
        node = self._verified_node()
        node.moduleConfig.mesh_beacon.flags = 7

        def _report_then_fail(_descriptor: Any, **kwargs: Any) -> None:
            self._device_reports(node, 4, **kwargs)
            raise _TestInterfaceError("read-back failed")

        node.requestConfig.side_effect = _report_then_fail
        with pytest.raises(_TestInterfaceError, match="read-back failed"):
            self._verify_beacon(node, verify_timeout=1.0)

        assert node.moduleConfig.mesh_beacon.flags == 4

    @pytest.mark.unit
    def test_verify_restores_sent_values_when_request_fails_after_empty_response(
        self,
    ) -> None:
        """A response without section data cannot block failure restoration."""
        node = self._verified_node()
        node.moduleConfig.mesh_beacon.flags = 7

        def _deliver_empty_then_fail(_descriptor: Any, **kwargs: Any) -> None:
            self._device_reports(node, None, **kwargs)
            raise _TestInterfaceError("read-back failed")

        node.requestConfig.side_effect = _deliver_empty_then_fail
        with pytest.raises(_TestInterfaceError, match="read-back failed"):
            self._verify_beacon(node, verify_timeout=1.0)

        assert node.moduleConfig.mesh_beacon.flags == 7

    @pytest.mark.unit
    def test_verify_fails_promptly_on_routing_nak(self) -> None:
        """A correlated routing NAK fails verification with its cause."""
        node = self._verified_node()
        node.moduleConfig.mesh_beacon.flags = 7

        def _deliver_nak(_descriptor: Any, on_response: Any, **_kwargs: Any) -> None:
            on_response({"decoded": {"routing": {"errorReason": "NO_RESPONSE"}}})

        node.requestConfig.side_effect = _deliver_nak

        with pytest.raises(_TestInterfaceError, match="routing error NO_RESPONSE"):
            self._verify_beacon(node, verify_timeout=5.0)

        assert node.moduleConfig.mesh_beacon.flags == 7

    @pytest.mark.unit
    def test_verify_compares_exact_section_snapshot_that_was_sent(self) -> None:
        """A cache mutation after send cannot change the verification target."""
        node = self._verified_node()
        node.moduleConfig.mesh_beacon.flags = 7

        def _mutate_cache_after_send(_message: Any, **_kwargs: Any) -> Any:
            node.moduleConfig.mesh_beacon.flags = 9
            return MagicMock()

        node._send_admin.side_effect = _mutate_cache_after_send
        node.requestConfig.side_effect = (
            lambda _descriptor, **kwargs: self._device_reports(node, 7, **kwargs)
        )

        self._verify_beacon(node, verify_timeout=1.0)

        sent_message = node._send_admin.call_args.args[0]
        assert sent_message.set_module_config.mesh_beacon.flags == 7
        assert node.moduleConfig.mesh_beacon.flags == 7

    @pytest.mark.unit
    def test_verify_skipped_under_noproto(self) -> None:
        """NoProto sends nothing, so verification is skipped without error."""
        node = self._verified_node()
        node.noProto = True
        node.moduleConfig.mesh_beacon.flags = 7
        node.moduleConfig.mesh_beacon.broadcast_offer_channel.name = "N" * 12
        runtime = self._beacon_runtime(node)

        runtime.write_config("mesh_beacon", verify=True)

        node.requestConfig.assert_not_called()

    @pytest.mark.unit
    @pytest.mark.parametrize("reported_flags,later_flags", [(4, 7), (7, 4)])
    def test_verify_uses_correlated_payload_after_later_cache_update(
        self, reported_flags: int, later_flags: int
    ) -> None:
        """Later cache updates cannot change the correlated response verdict."""
        node = self._verified_node()
        node.moduleConfig.mesh_beacon.flags = 7

        def _respond_then_overwrite(_descriptor: Any, **kwargs: Any) -> None:
            self._device_reports(node, reported_flags, **kwargs)
            node.moduleConfig.mesh_beacon.flags = later_flags

        node.requestConfig.side_effect = _respond_then_overwrite
        if reported_flags == 7:
            self._verify_beacon(node, verify_timeout=1.0)
        else:
            with pytest.raises(_TestInterfaceError, match="different values"):
                self._verify_beacon(node, verify_timeout=1.0)
        assert node.moduleConfig.mesh_beacon.flags == later_flags

    @pytest.mark.unit
    def test_verify_interrupted_poll_restores_sent_values(self) -> None:
        """Cancellation after requesting preserves intent when no reply arrived."""
        node = self._verified_node()
        node.moduleConfig.mesh_beacon.flags = 7
        with (
            patch(
                "meshtastic.node_runtime.settings_runtime.config_runtime.time.sleep",
                side_effect=KeyboardInterrupt,
            ),
            pytest.raises(KeyboardInterrupt),
        ):
            self._verify_beacon(node, verify_timeout=1.0)
        assert node.moduleConfig.mesh_beacon.flags == 7

    @pytest.mark.unit
    def test_overlapping_verifications_keep_request_callbacks_independent(self) -> None:
        """Two section reads complete independently without replacing Node methods."""
        node = self._verified_node()
        node.moduleConfig.mesh_beacon.flags = 7
        node.moduleConfig.mqtt.enabled = True
        staged = {
            name: getattr(node.moduleConfig, name).SerializeToString()
            for name in ("mesh_beacon", "mqtt")
        }
        runtime = self._beacon_runtime(node)
        original_handler = node.onResponseRequestSettings
        callbacks: dict[str, Any] = {}
        requested = {name: threading.Event() for name in staged}
        finished = {name: threading.Event() for name in staged}
        errors: list[BaseException] = []

        def _send(message: Any, **kwargs: Any) -> Any:
            index = message.get_module_config_request
            name = node.moduleConfig.DESCRIPTOR.fields[index].name
            callbacks[name] = kwargs["onResponse"]
            requested[name].set()
            return MagicMock()

        def _verify(name: str) -> None:
            try:
                runtime._verify_written_config(name, staged_bytes=staged[name])
            except BaseException as error:
                errors.append(error)
            finally:
                finished[name].set()

        node._send_admin.side_effect = _send
        threads = [threading.Thread(target=_verify, args=(name,)) for name in staged]
        with (
            patch(
                "meshtastic.node_runtime.settings_runtime.config_runtime.CONFIG_VERIFY_TIMEOUT_SECONDS",
                2.0,
            ),
            patch(
                "meshtastic.node_runtime.settings_runtime.config_runtime.CONFIG_VERIFY_POLL_INTERVAL_SECONDS",
                0.01,
            ),
        ):
            try:
                for thread in threads:
                    thread.start()
                assert all(event.wait(1.0) for event in requested.values())
                assert node.onResponseRequestSettings is original_handler
                self._device_reports(node, 7, on_response=callbacks["mesh_beacon"])
                assert finished["mesh_beacon"].wait(1.0)
                assert not finished["mqtt"].is_set()
                raw = admin_pb2.AdminMessage()
                raw.get_module_config_response.mqtt.enabled = True
                callbacks["mqtt"](
                    {
                        "decoded": {
                            "admin": {
                                "raw": raw,
                                "getModuleConfigResponse": {"mqtt": {"enabled": True}},
                            }
                        }
                    }
                )
                assert finished["mqtt"].wait(1.0)
            finally:
                for thread in threads:
                    thread.join(timeout=3.0)
        assert not errors
        assert node.onResponseRequestSettings is original_handler

    @pytest.mark.unit
    def test_verify_rejects_other_section_despite_matching_cached_values(self) -> None:
        """A correlated response for another section cannot certify stale cache."""
        node = self._verified_node()
        node.moduleConfig.mesh_beacon.flags = 7

        def _wrong_section(_descriptor: Any, **kwargs: Any) -> None:
            # Another outstanding read supplied matching cache state, while
            # this request's own response contains only a different section.
            node.moduleConfig.mesh_beacon.flags = 7
            raw = admin_pb2.AdminMessage()
            raw.get_module_config_response.mqtt.enabled = True
            kwargs["on_response"](
                {
                    "decoded": {
                        "admin": {
                            "raw": raw,
                            "getModuleConfigResponse": {"mqtt": {"enabled": True}},
                        }
                    }
                }
            )

        node.requestConfig.side_effect = _wrong_section
        self._verify_failing_beacon(node)
        assert node.moduleConfig.mesh_beacon.flags == 7
