"""Shared helpers for decomposed legacy ``meshtastic.__main__`` tests."""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, create_autospec, patch

import pytest
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

import meshtastic.__main__ as main_module
from meshtastic import mt_config
from meshtastic.__main__ import main
from meshtastic.node import Node
from meshtastic.protobuf import localonly_pb2
from meshtastic.serial_interface import SerialInterface


def _get_config_field(config: Any, dotted_path: str) -> Any:
    """Walk a dotted ``section.field`` path on a protobuf Config message."""
    value = config
    for part in dotted_path.split("."):
        value = getattr(value, part)
    return value


def _patch_fast_monotonic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Advance monotonic time quickly in reconnect/config verification tests."""
    value = [0.0]

    def _fast() -> float:
        value[0] += 100.0
        return value[0]

    time_attrs = vars(main_module.time).copy()
    time_attrs["monotonic"] = _fast
    monkeypatch.setattr(
        main_module, "time", SimpleNamespace(**time_attrs), raising=True
    )


def _mock_send_text(
    text: str,
    dest: Any,
    wantAck: bool = False,
    wantResponse: bool = False,
    onResponse: Callable[..., Any] | None = None,
    channelIndex: int = 0,
    portNum: int = 0,
) -> None:
    """Print mocked sendText arguments for historical CLI assertions."""
    _ = onResponse
    print("inside mocked sendText")
    print(f"{text} {dest} {wantAck} {wantResponse} {channelIndex} {portNum}")


def _build_export_interface(
    local_config: localonly_pb2.LocalConfig,
    module_config: localonly_pb2.LocalModuleConfig,
) -> MagicMock:
    """Build a minimal interface mock compatible with ``export_config``."""
    iface = create_autospec(SerialInterface, instance=True)
    iface.devPath = "/dev/mock"
    iface.isConnected = threading.Event()
    iface.isConnected.set()
    iface.localNode = create_autospec(Node, instance=True)
    iface.localNode.localConfig = local_config
    iface.localNode.moduleConfig = module_config
    iface.localNode.getURL.return_value = "https://meshtastic.org/e/#Cgo"
    iface.getLongName.return_value = "Roundtrip Node"
    iface.getShortName.return_value = "RT"
    iface.getMyNodeInfo.return_value = {}
    iface.getCannedMessage.return_value = ""
    iface.getRingtone.return_value = ""
    return iface


def _build_configure_interface(
    target_local: localonly_pb2.LocalConfig | None = None,
    target_module: localonly_pb2.LocalModuleConfig | None = None,
) -> tuple[MagicMock, MagicMock]:
    """Build a minimal interface mock compatible with ``--configure`` operations."""
    if target_local is None:
        target_local = localonly_pb2.LocalConfig()
    if target_module is None:
        target_module = localonly_pb2.LocalModuleConfig()

    device_local = localonly_pb2.LocalConfig()
    device_local.CopyFrom(target_local)
    device_module = localonly_pb2.LocalModuleConfig()
    device_module.CopyFrom(target_module)

    target_node = create_autospec(Node, instance=True)
    # Modeled on the noProto main-path doubles: section readiness never waits.
    target_node.noProto = True
    target_node.localConfig = target_local
    target_node.moduleConfig = target_module
    target_node.beginSettingsTransaction = MagicMock()
    target_node.commitSettingsTransaction = MagicMock()
    target_node.setOwner = MagicMock()
    target_node.setURL = MagicMock()
    target_node.set_canned_message = MagicMock()
    target_node.set_ringtone = MagicMock()
    target_node.channels = []
    target_node.partialChannels = []
    target_node.requestChannels = MagicMock()

    def _write_config_side_effect(config_name: str) -> None:
        local_field = target_local.DESCRIPTOR.fields_by_name.get(config_name)
        if local_field is not None:
            device_local.ClearField(config_name)  # type: ignore[arg-type]
            if target_local.HasField(config_name):  # type: ignore[arg-type]
                getattr(device_local, config_name).CopyFrom(
                    getattr(target_local, config_name)
                )
            return
        module_field = target_module.DESCRIPTOR.fields_by_name.get(config_name)
        if module_field is not None:
            device_module.ClearField(config_name)  # type: ignore[arg-type]
            if target_module.HasField(config_name):  # type: ignore[arg-type]
                getattr(device_module, config_name).CopyFrom(
                    getattr(target_module, config_name)
                )

    target_node.writeConfig = MagicMock(side_effect=_write_config_side_effect)

    def _request_config_side_effect(config_type: object, *_args: object) -> None:
        field_name = getattr(config_type, "name", None)
        containing_type = getattr(config_type, "containing_type", None)
        containing_name = getattr(containing_type, "name", None)
        if not isinstance(field_name, str):
            return
        if containing_name == "LocalConfig":
            target_local.ClearField(field_name)  # type: ignore[arg-type]
            if device_local.HasField(field_name):  # type: ignore[arg-type]
                getattr(target_local, field_name).CopyFrom(
                    getattr(device_local, field_name)
                )
            return
        if containing_name == "LocalModuleConfig":
            target_module.ClearField(field_name)  # type: ignore[arg-type]
            if device_module.HasField(field_name):  # type: ignore[arg-type]
                getattr(target_module, field_name).CopyFrom(
                    getattr(device_module, field_name)
                )

    target_node.requestConfig = MagicMock(side_effect=_request_config_side_effect)
    target_node.setFixedPosition = MagicMock()

    iface = create_autospec(SerialInterface, instance=True)
    iface.devPath = "/dev/mock"
    iface.isConnected = threading.Event()
    iface.isConnected.set()
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = target_node
    iface.localNode = target_node
    return iface, target_node


def _run_main_configure_file(
    config_path: Path,
    iface: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run ``main`` for ``--configure`` against a supplied interface mock."""
    argv = ["", "--configure", str(config_path)]
    time_attrs = vars(main_module.time).copy()
    time_attrs["sleep"] = lambda _seconds: None
    with monkeypatch.context() as configure_patch:
        configure_patch.setattr(sys, "argv", argv)
        configure_patch.setattr(mt_config, "args", cast(Any, argv))
        configure_patch.setattr(
            main_module,
            "time",
            SimpleNamespace(**time_attrs),
            raising=True,
        )
        with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
            main()


def _make_fake_tcp_interface(
    *,
    get_node: Callable[..., Any] | None = None,
    on_close: Callable[[], None] | None = None,
) -> type[object]:
    """Return a configurable TCPInterface test double with context-manager behavior."""

    class _FakeTCPInterface:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.hostname = "localhost"
            if get_node is not None:
                self.getNode = get_node

        def __enter__(self) -> "_FakeTCPInterface":
            return self

        def __exit__(self, *_args: object) -> None:
            self.close()

        def close(self) -> None:
            if on_close is not None:
                on_close()

    return _FakeTCPInterface


def _build_nested_bytes_test_message() -> Any:
    """Build a dynamic protobuf covering nested/repeated/map bytes fields."""
    file_proto = descriptor_pb2.FileDescriptorProto(
        name="nested_bytes_test.proto", package="mtjk.tests.nested", syntax="proto3"
    )
    child = file_proto.message_type.add(name="Child")
    child.field.add(
        name="payload",
        number=1,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_BYTES,
    )
    container = file_proto.message_type.add(name="Container")
    child_map_entry = container.nested_type.add(name="ChildMapEntry")
    child_map_entry.options.map_entry = True
    child_map_entry.field.add(
        name="key",
        number=1,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
    )
    child_map_entry.field.add(
        name="value",
        number=2,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE,
        type_name=".mtjk.tests.nested.Child",
    )
    container.field.add(
        name="child_map",
        number=1,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE,
        type_name=".mtjk.tests.nested.Container.ChildMapEntry",
    )
    container.field.add(
        name="children",
        number=2,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE,
        type_name=".mtjk.tests.nested.Child",
    )
    container.field.add(
        name="child",
        number=3,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE,
        type_name=".mtjk.tests.nested.Child",
    )
    container.field.add(
        name="blobs",
        number=4,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_BYTES,
    )
    container.field.add(
        name="scalar_blob",
        number=5,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_BYTES,
    )
    pool = descriptor_pool.DescriptorPool()
    pool.Add(file_proto)
    message_class = message_factory.GetMessageClass(
        pool.FindMessageTypeByName("mtjk.tests.nested.Container")
    )
    return message_class()
