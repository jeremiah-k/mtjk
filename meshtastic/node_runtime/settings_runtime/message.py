"""Settings request/response and admin command-family runtime owners."""

import logging
from typing import TYPE_CHECKING, Any, cast

from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.message import Message

from meshtastic.protobuf import admin_pb2

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)

_ERR_INVALID_CONFIG_NAME = "Error: No valid config with name {}"


class _NodeSettingsMessageBuilder:
    """Owns settings request/write AdminMessage construction and field mapping."""

    def __init__(self, node: "Node") -> None:
        self._node = node

    def build_request_message(
        self, config_type: int | FieldDescriptor
    ) -> admin_pb2.AdminMessage:
        """Build request-config message from int or protobuf field descriptor."""
        message = admin_pb2.AdminMessage()
        if isinstance(config_type, int):
            message.get_config_request = cast(
                "admin_pb2.AdminMessage.ConfigType.ValueType", config_type
            )
            return message

        containing_type = config_type.containing_type
        if containing_type is None:
            raise ValueError(
                f"Config descriptor {config_type.name} has no containing message"
            )

        if containing_type.name == "LocalConfig":
            message.get_config_request = admin_pb2.AdminMessage.ConfigType.Value(
                f"{config_type.name.upper()}_CONFIG"
            )
            return message

        if containing_type.name in ("ModuleConfig", "LocalModuleConfig"):
            message.get_module_config_request = cast(
                "admin_pb2.AdminMessage.ModuleConfigType.ValueType", config_type.index
            )
            return message

        raise ValueError(
            f"Unsupported config descriptor: {config_type.name} in {containing_type.name}"
        )

    @staticmethod
    def _write_entries_for(
        setter_name: str, source_config: Message
    ) -> dict[str, tuple[str, Any]]:
        """Return writable fields shared by an admin setter and local cache."""
        setter_field = admin_pb2.AdminMessage.DESCRIPTOR.fields_by_name[setter_name]
        setter_message = setter_field.message_type
        if setter_message is None:
            raise ValueError(f"Admin setter {setter_name!r} is not a message field")

        source_fields = source_config.DESCRIPTOR.fields_by_name
        return {
            field.name: (setter_name, getattr(source_config, field.name))
            for field in setter_message.fields
            if field.name in source_fields
        }

    def _write_config_dispatch(self) -> dict[str, tuple[str, Any]]:
        """Return schema-driven config-name to admin setter/source mapping."""
        dispatch = self._write_entries_for("set_config", self._node.localConfig)
        dispatch.update(
            self._write_entries_for("set_module_config", self._node.moduleConfig)
        )
        return dispatch

    def get_write_config_entry(self, config_name: str) -> tuple[str, Any] | None:
        """Return dispatch entry for one write-config section when available."""
        return self._write_config_dispatch().get(config_name)

    def build_write_message(self, config_name: str) -> admin_pb2.AdminMessage:
        """Build one set_config/set_module_config message for a config name."""
        config_entry = self.get_write_config_entry(config_name)
        if config_entry is None:
            self._node._raise_interface_error(  # noqa: SLF001
                _ERR_INVALID_CONFIG_NAME.format(config_name)
            )
            raise AssertionError("Unreachable: _raise_interface_error must raise")

        message = admin_pb2.AdminMessage()
        setter_name, source_config = config_entry
        config_setter = getattr(message, setter_name)
        getattr(config_setter, config_name).CopyFrom(source_config)
        return message

    def validate_config_name(self, config_name: str) -> None:
        """Validate config-name dispatch key without constructing a message."""
        if self.get_write_config_entry(config_name) is None:
            self._node._raise_interface_error(  # noqa: SLF001
                _ERR_INVALID_CONFIG_NAME.format(config_name)
            )
            raise AssertionError("Unreachable: _raise_interface_error must raise")
