"""Compatibility and dependency-boundary tests for core runtime primitives."""

from __future__ import annotations

import ast
import inspect
import pickle
import subprocess
import sys
import typing
from pathlib import Path

import pytest

import meshtastic
from meshtastic import _core_constants, _protocol_runtime, _publishing, _response_types

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.unit


def _meshtastic_root_references(path: Path) -> set[str]:
    """Return package-root names referenced by one Python module.

    Both ``from meshtastic import name`` and ``import meshtastic;
    meshtastic.name`` forms are included so the dependency-boundary test does
    not depend on a particular import style.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    referenced: set[str] = set()
    root_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "meshtastic":
                    root_aliases.add(alias.asname or alias.name)
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module == "meshtastic"
        ):
            referenced.update(alias.name for alias in node.names)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in root_aliases
        ):
            referenced.add(node.attr)
    return referenced


def _production_python_modules() -> tuple[Path, ...]:
    """Return production modules that must consume core primitives directly."""
    package_root = ROOT / "meshtastic"
    return tuple(
        path
        for path in sorted(package_root.rglob("*.py"))
        if path != package_root / "__init__.py"
        and "tests" not in path.parts
        and "protobuf" not in path.parts
    )


def test_public_core_constants_are_leaf_module_identities() -> None:
    """Package-root constants should remain exact re-exports of leaf primitives."""
    for name in _core_constants.__all__:
        assert getattr(meshtastic, name) is getattr(_core_constants, name), name


def test_internal_core_validation_messages_are_not_public_exports() -> None:
    """Internal validation literals must not accidentally expand package-root API."""
    assert "LAST_DISCONNECT_SOURCE_TYPE_ERROR" not in _core_constants.__all__
    assert not hasattr(meshtastic, "LAST_DISCONNECT_SOURCE_TYPE_ERROR")


def test_public_response_types_are_leaf_module_identities() -> None:
    """Historical response types should retain identity through the package facade."""
    for name in _response_types.__all__:
        assert getattr(meshtastic, name) is getattr(_response_types, name), name


def test_production_consumers_do_not_reference_moved_primitives_from_package_root() -> (
    None
):
    """Production internals should consume leaf-owned primitives directly."""
    moved_names = (
        set(_core_constants.__all__)
        | set(_response_types.__all__)
        | set(_protocol_runtime.PACKAGE_ROOT_COMPAT_EXPORTS)
    )
    for path in _production_python_modules():
        assert _meshtastic_root_references(path).isdisjoint(moved_names), path


def test_public_response_handler_metadata_and_pickle_contract_are_preserved() -> None:
    """Moving ResponseHandler must not change its historical public metadata."""
    assert meshtastic.ResponseHandler.__module__ == "meshtastic"
    assert meshtastic.ResponseHandler.__qualname__ == "ResponseHandler"
    assert meshtastic.ResponseHandler._fields == ("callback", "ackPermitted")
    assert meshtastic.ResponseHandler._field_defaults == {"ackPermitted": False}

    response_handler = meshtastic.ResponseHandler(callback=str, ackPermitted=True)
    assert pickle.loads(pickle.dumps(response_handler)) == response_handler

    signature = inspect.signature(meshtastic.ResponseHandler)
    assert tuple(signature.parameters) == ("callback", "ackPermitted")
    callback_parameter = signature.parameters["callback"]
    assert callback_parameter.default is inspect.Signature.empty
    assert callback_parameter.annotation == _response_types.ResponseCallback
    ack_parameter = signature.parameters["ackPermitted"]
    assert ack_parameter.default is False
    assert ack_parameter.annotation is bool
    assert signature.return_annotation is inspect.Signature.empty


def test_request_wait_uses_shared_decode_error_key_primitive() -> None:
    """Request waits must share the single leaf-owned decode error key."""
    from meshtastic.mesh_interface_runtime import request_wait

    assert getattr(request_wait, "DECODE_ERROR_KEY") is _core_constants.DECODE_ERROR_KEY
    assert getattr(request_wait, "DECODE_ERROR_KEY") is meshtastic.DECODE_ERROR_KEY


def test_core_owner_modules_do_not_import_package_root() -> None:
    """Core leaf modules should stay below the package facade."""
    for module in (_core_constants, _response_types):
        assert module.__file__ is not None
        path = Path(module.__file__).resolve()
        assert _meshtastic_root_references(path) == set(), path


def test_public_protocol_runtime_objects_preserve_identity() -> None:
    """Package-root protocol objects should be exact internal runtime re-exports."""
    for name in _protocol_runtime.PACKAGE_ROOT_COMPAT_EXPORTS:
        assert getattr(meshtastic, name) is getattr(_protocol_runtime, name), name


def test_receive_pipeline_imports_protocol_registry_from_internal_runtime() -> None:
    """Receive processing should not route protocol-registry access through package root."""
    path = ROOT / "meshtastic/mesh_interface_runtime/receive_pipeline.py"
    assert "protocols" not in _meshtastic_root_references(path)


def test_public_known_protocol_metadata_and_pickle_contract_are_preserved() -> None:
    """Moving KnownProtocol must not change its historical public metadata."""
    assert meshtastic.KnownProtocol.__module__ == "meshtastic"
    assert meshtastic.KnownProtocol.__qualname__ == "KnownProtocol"
    assert meshtastic.KnownProtocol._fields == ("name", "protobufFactory", "onReceive")
    assert meshtastic.KnownProtocol._field_defaults == {
        "protobufFactory": None,
        "onReceive": None,
    }

    known_protocol = meshtastic.KnownProtocol("test")
    assert pickle.loads(pickle.dumps(known_protocol)) == known_protocol

    signature = inspect.signature(meshtastic.KnownProtocol)
    assert tuple(signature.parameters) == ("name", "protobufFactory", "onReceive")
    assert signature.parameters["name"].default is inspect.Signature.empty
    assert signature.parameters["protobufFactory"].default is None
    assert signature.parameters["onReceive"].default is None
    assert signature.return_annotation is inspect.Signature.empty

    hints = typing.get_type_hints(meshtastic.KnownProtocol)
    assert hints == {
        "name": str,
        "protobufFactory": meshtastic.ProtobufFactory | None,
        "onReceive": meshtastic.OnReceive | None,
    }


def test_protocol_runtime_uses_historical_package_logger() -> None:
    """Handler extraction should not change the logger name or logger object."""
    assert _protocol_runtime.logger is meshtastic.logger


def test_protocol_owner_module_does_not_import_package_root() -> None:
    """The protocol runtime should remain below the package facade."""
    assert _protocol_runtime.__file__ is not None
    path = Path(_protocol_runtime.__file__).resolve()
    assert _meshtastic_root_references(path) == set()


def test_public_publishing_thread_is_internal_owner_identity() -> None:
    """Historical publishingThread should alias the internal singleton exactly."""
    assert meshtastic.publishingThread is _publishing.publishing_thread


def test_importing_meshtastic_does_not_start_publishing_worker() -> None:
    """Package import should not create the publishing daemon before work exists."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import meshtastic; "
                "print(meshtastic.publishingThread.thread is None); "
                "meshtastic.publishingThread.stop()"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.splitlines()[-1].strip() == "True"


def test_production_consumers_do_not_import_publishing_worker_from_package_root() -> (
    None
):
    """Production modules should depend on the internal publishing owner directly."""
    for path in _production_python_modules():
        assert "publishingThread" not in _meshtastic_root_references(path), path


def test_publishing_owner_module_does_not_import_package_root() -> None:
    """Publishing ownership should remain below the package facade."""
    assert _publishing.__file__ is not None
    path = Path(_publishing.__file__).resolve()
    assert _meshtastic_root_references(path) == set()
