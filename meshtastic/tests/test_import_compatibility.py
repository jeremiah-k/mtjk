"""Import compatibility tests for old import paths.

These tests verify that old documented import paths still work, ensuring
backward compatibility for users' existing code after the major refactor.

Reference: docs/compatibility.md for documented compatibility aliases.
"""

# pylint: disable=no-name-in-module

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import ModuleType

import pytest


@pytest.mark.unit
class TestCoreModuleImports:
    """Test core Meshtastic module imports that should remain stable."""

    def test_import_node(self) -> None:
        """Test that Node can be imported from meshtastic package."""
        from meshtastic import Node  # pylint: disable=import-outside-toplevel

        assert Node is not None
        assert isinstance(Node, type)

    def test_import_mesh_interface(self) -> None:
        """Test that MeshInterface can be imported from meshtastic.mesh_interface.

        Note: MeshInterface is not exported directly from the meshtastic package.
        Import from meshtastic.mesh_interface instead.
        """
        from meshtastic.mesh_interface import (  # pylint: disable=import-outside-toplevel
            MeshInterface,
        )

        assert MeshInterface is not None
        assert isinstance(MeshInterface, type)

    def test_import_broadcast_addr(self) -> None:
        """Test that BROADCAST_ADDR can be imported from meshtastic package."""
        from meshtastic import BROADCAST_ADDR  # pylint: disable=import-outside-toplevel

        assert BROADCAST_ADDR == "^all"

    def test_import_local_addr(self) -> None:
        """Test that LOCAL_ADDR can be imported from meshtastic package."""
        from meshtastic import LOCAL_ADDR  # pylint: disable=import-outside-toplevel

        assert LOCAL_ADDR == "^local"

    def test_util_message_to_json_import(self) -> None:
        """Test that messageToJson can be imported from meshtastic.util."""
        from meshtastic.util import (  # pylint: disable=import-outside-toplevel
            messageToJson,
        )

        assert messageToJson is not None
        assert callable(messageToJson)

    def test_util_from_psk_import(self) -> None:
        """Test that fromPSK can be imported from meshtastic.util."""
        from meshtastic.util import fromPSK  # pylint: disable=import-outside-toplevel

        assert fromPSK is not None
        assert callable(fromPSK)

    def test_util_to_node_num_import(self) -> None:
        """Test that toNodeNum can be imported from meshtastic.util."""
        from meshtastic.util import toNodeNum  # pylint: disable=import-outside-toplevel

        assert toNodeNum is not None
        assert callable(toNodeNum)


@pytest.mark.unit
class TestBLEImports:
    """Test BLE interface imports - both old and new paths."""

    def test_ble_interface_import(self) -> None:
        """Test that BLEInterface can be imported from meshtastic.ble_interface."""
        # BLEInterface is a re-export from meshtastic.interfaces.ble
        from meshtastic.ble_interface import (
            BLEInterface,  # type: ignore[attr-defined]  # pylint: disable=import-outside-toplevel
        )

        assert BLEInterface is not None
        assert isinstance(BLEInterface, type)

    def test_bleak_client_import_from_ble_interface(self) -> None:
        """Test that BleakClient can be imported from meshtastic.ble_interface.

        This is a backwards compatibility shim for code that imported Bleak
        symbols from meshtastic.ble_interface in the pre-refactor API.
        COMPAT_STABLE_SHIM
        """
        # Re-export of BleakClient for backwards compatibility
        from meshtastic.ble_interface import (
            BleakClient,  # type: ignore[attr-defined]  # pylint: disable=import-outside-toplevel
        )

        assert BleakClient is not None

    def test_bleak_scanner_import_from_ble_interface(self) -> None:
        """Test that BleakScanner can be imported from meshtastic.ble_interface.

        This is a backwards compatibility shim for code that imported Bleak
        symbols from meshtastic.ble_interface in the pre-refactor API.
        COMPAT_STABLE_SHIM
        """
        # Re-export of BleakScanner for backwards compatibility
        from meshtastic.ble_interface import (
            BleakScanner,  # type: ignore[attr-defined]  # pylint: disable=import-outside-toplevel
        )

        assert BleakScanner is not None

    def test_ble_interface_import_from_interfaces(self) -> None:
        """Test that BLEInterface can be imported from meshtastic.interfaces.ble."""
        from meshtastic.interfaces.ble import (  # pylint: disable=import-outside-toplevel
            BLEInterface,
        )

        assert BLEInterface is not None
        assert isinstance(BLEInterface, type)


@pytest.mark.unit
class TestNodeRuntimeImports:
    """Test node runtime module compatibility exports.

    The node_runtime modules contain internal implementation details.
    Only explicitly documented compatibility exports (like toNodeNum) are
    guaranteed stable. Underscore-prefixed internals may change without notice.

    Reference: docs/compatibility.md "Runtime Import Compatibility" section.
    """

    def test_to_node_num_export_from_settings_runtime(self) -> None:
        """Test that toNodeNum is exported from settings_runtime for mocking compatibility.

        This is a COMPAT_STABLE_SHIM explicitly maintained for test ecosystem
        compatibility. Documented in docs/compatibility.md.
        """
        from meshtastic.node_runtime.settings_runtime import (  # pylint: disable=import-outside-toplevel
            toNodeNum,
        )

        assert toNodeNum is not None
        assert callable(toNodeNum)


@pytest.mark.unit
class TestProtobufImports:
    """Test protobuf module imports."""

    def test_import_mesh_pb2(self) -> None:
        """Test that mesh_pb2 can be imported from meshtastic.protobuf."""
        from meshtastic.protobuf import (  # pylint: disable=import-outside-toplevel
            mesh_pb2,
        )

        assert mesh_pb2 is not None
        assert isinstance(mesh_pb2, ModuleType)

    def test_import_channel_pb2(self) -> None:
        """Test that channel_pb2 can be imported from meshtastic.protobuf."""
        from meshtastic.protobuf import (  # pylint: disable=import-outside-toplevel
            channel_pb2,
        )

        assert channel_pb2 is not None
        assert isinstance(channel_pb2, ModuleType)

    def test_import_mesh_packet_from_mesh_pb2(self) -> None:
        """Test that MeshPacket can be imported from mesh_pb2."""
        # MeshPacket is a protobuf message class, pylint has trouble with protobuf imports
        from meshtastic.protobuf.mesh_pb2 import (  # pylint: disable=import-outside-toplevel
            MeshPacket,
        )

        assert MeshPacket is not None


@pytest.mark.unit
class TestTopLevelModuleImports:
    """Test top-level module imports and key attributes."""

    def test_import_meshtastic_package(self) -> None:
        """Test that meshtastic package can be imported."""
        import meshtastic  # pylint: disable=import-outside-toplevel

        assert meshtastic is not None
        assert isinstance(meshtastic, ModuleType)

    def test_import_node_module(self) -> None:
        """Test that meshtastic.node module can be imported."""
        import meshtastic.node  # pylint: disable=import-outside-toplevel

        assert meshtastic.node is not None
        assert isinstance(meshtastic.node, ModuleType)
        assert hasattr(meshtastic.node, "Node")

    def test_import_mesh_interface_module(self) -> None:
        """Test that meshtastic.mesh_interface module can be imported."""
        import meshtastic.mesh_interface  # pylint: disable=import-outside-toplevel

        assert meshtastic.mesh_interface is not None
        assert isinstance(meshtastic.mesh_interface, ModuleType)
        assert hasattr(meshtastic.mesh_interface, "MeshInterface")

    def test_meshtastic_key_attributes(self) -> None:
        """Test that key attributes exist on the meshtastic module."""
        import meshtastic  # pylint: disable=import-outside-toplevel

        # Key constants
        assert hasattr(meshtastic, "BROADCAST_ADDR")
        assert hasattr(meshtastic, "LOCAL_ADDR")
        assert hasattr(meshtastic, "BROADCAST_NUM")
        assert hasattr(meshtastic, "OUR_APP_VERSION")

        # Key classes
        assert hasattr(meshtastic, "Node")
        assert hasattr(meshtastic, "ResponseHandler")
        assert hasattr(meshtastic, "KnownProtocol")

        # Protocols dictionary
        assert hasattr(meshtastic, "protocols")
        assert isinstance(meshtastic.protocols, dict)


@pytest.mark.unit
class TestBackwardCompatAliases:
    """Test backward compatibility aliases that are maintained but may warn."""

    @pytest.mark.usefixtures("isolated_dotdict_deprecation_state")
    def test_util_dotdict_deprecated_import(self) -> None:
        """Test that dotdict can still be imported from meshtastic.util.

        This is a COMPAT_DEPRECATE alias that emits a warn-once DeprecationWarning.
        The canonical name is DotDict.
        """
        import warnings  # pylint: disable=import-outside-toplevel

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            from meshtastic.util import (  # pylint: disable=import-outside-toplevel
                dotdict,
            )

            # Verify the class is importable
            assert dotdict is not None
            assert isinstance(dotdict, type)

            # Instantiate to trigger deprecation warning
            _ = dotdict()

            # Should have emitted a deprecation warning
            deprecation_warnings = [
                x for x in w if issubclass(x.category, DeprecationWarning)
            ]
            assert len(deprecation_warnings) >= 1

            # Verify warn-once behavior: subsequent instantiation should not warn again
            initial_count = len(deprecation_warnings)
            _ = dotdict()
            new_deprecation_warnings = [
                x for x in w if issubclass(x.category, DeprecationWarning)
            ]
            assert (
                len(new_deprecation_warnings) == initial_count
            ), "Expected warn-once behavior for dotdict deprecation"

    def test_mt_config_tunnel_instance_deprecated(self) -> None:
        """Test that tunnelInstance can be accessed from mt_config.

        This is a COMPAT_DEPRECATE alias for tunnel_instance that emits
        a warn-once DeprecationWarning on read/write/delete.
        """
        import warnings  # pylint: disable=import-outside-toplevel

        from meshtastic import mt_config  # pylint: disable=import-outside-toplevel

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            # Should be accessible (may warn)
            _ = mt_config.tunnelInstance  # Access the deprecated alias

            # Verify deprecation warning was emitted
            deprecation_warnings = [
                x for x in w if issubclass(x.category, DeprecationWarning)
            ]
            assert (
                len(deprecation_warnings) >= 1
            ), "Expected DeprecationWarning for tunnelInstance"

    def test_node_snake_case_ringtone_aliases(self) -> None:
        """Test that snake_case ringtone methods exist on Node.

        These are COMPAT_STABLE_SHIM aliases for the camelCase methods:
        - get_ringtone -> getRingtone
        - set_ringtone -> setRingtone
        """
        from meshtastic.node import Node  # pylint: disable=import-outside-toplevel

        # Verify the snake_case aliases exist
        assert hasattr(Node, "get_ringtone")
        assert hasattr(Node, "set_ringtone")

        # Verify they are callable
        assert callable(Node.get_ringtone)
        assert callable(Node.set_ringtone)

    def test_node_snake_case_canned_message_aliases(self) -> None:
        """Test that snake_case canned message methods exist on Node.

        These are COMPAT_STABLE_SHIM aliases for the camelCase methods:
        - get_canned_message -> getCannedMessage
        - set_canned_message -> setCannedMessage
        """
        from meshtastic.node import Node  # pylint: disable=import-outside-toplevel

        # Verify the snake_case aliases exist
        assert hasattr(Node, "get_canned_message")
        assert hasattr(Node, "set_canned_message")

        # Verify they are callable
        assert callable(Node.get_canned_message)
        assert callable(Node.set_canned_message)


@pytest.mark.unit
class TestUtilityCompatAliases:
    """Test utility module compatibility aliases."""

    def test_util_snake_case_message_to_json(self) -> None:
        """Test that message_to_json snake_case function exists.

        This is a COMPAT_STABLE_SHIM that provides snake_case naming.
        Note: This is a separate function, not an alias of messageToJson().
        """
        from meshtastic.util import (  # pylint: disable=import-outside-toplevel
            message_to_json,
            messageToJson,
        )

        # Both should exist and be callable
        assert message_to_json is not None
        assert callable(message_to_json)
        assert messageToJson is not None
        assert callable(messageToJson)
        # They are different functions with the same behavior
        assert message_to_json.__name__ == "message_to_json"
        assert messageToJson.__name__ == "messageToJson"

    def test_util_snake_case_to_node_num(self) -> None:
        """Test that to_node_num snake_case function exists.

        This is a COMPAT_STABLE_SHIM that provides snake_case naming.
        Note: This is a separate function, not an alias of toNodeNum().
        """
        from meshtastic.util import (  # pylint: disable=import-outside-toplevel
            to_node_num,
            toNodeNum,
        )

        # Both should exist and be callable
        assert to_node_num is not None
        assert callable(to_node_num)
        assert toNodeNum is not None
        assert callable(toNodeNum)
        # They are different functions with the same behavior
        assert to_node_num.__name__ == "to_node_num"
        assert toNodeNum.__name__ == "toNodeNum"


@pytest.mark.unit
class TestSerialModuleLazyImport:
    """Test the lazy serial module import compatibility."""

    def test_meshtastic_serial_lazy_import(self) -> None:
        """Test that meshtastic.serial provides access to the pyserial module.

        This is a COMPAT_STABLE_SHIM that provides lazy access to the
        third-party pyserial module via __getattr__.
        """
        import meshtastic  # pylint: disable=import-outside-toplevel

        # Accessing meshtastic.serial should load the pyserial module
        serial_module = meshtastic.serial
        assert serial_module is not None
        assert isinstance(serial_module, ModuleType)

        # Should have key attributes expected from pyserial
        assert hasattr(serial_module, "Serial")


@pytest.mark.unit
class TestModuleReimport:
    """Test that modules can be reimported without errors."""

    def test_reimport_meshtastic(self) -> None:
        """Test that meshtastic module can be reimported cleanly."""
        import meshtastic  # pylint: disable=import-outside-toplevel

        importlib.reload(meshtastic)

        assert meshtastic is not None
        assert hasattr(meshtastic, "Node")
        assert hasattr(meshtastic, "BROADCAST_ADDR")


def test_runtime_compatibility_manifest_exports_are_importable() -> None:
    """Every manifest-backed runtime compatibility export should remain importable."""
    project_root = Path(__file__).resolve().parents[2]
    manifest = json.loads(
        (project_root / "meshtastic" / "_runtime_compatibility.json").read_text(
            encoding="utf-8"
        )
    )

    assert manifest["schema_version"] == 1
    for entry in manifest["modules"]:
        module = importlib.import_module(entry["path"])
        for export_name in entry["exports"]:
            assert hasattr(module, export_name), (entry["path"], export_name)
