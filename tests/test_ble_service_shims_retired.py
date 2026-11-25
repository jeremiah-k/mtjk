"""Pin that the retired BLE compat-service shim surface stays retired.

The BLE runtime enters its collaborators through the controllers,
coordinators, and publisher that own each behavior. These tests fail if a
retired static-method shim class or its hosting module reappears under
``meshtastic/interfaces/ble``, or if production BLE code starts comparing
methods against import-time snapshots of themselves.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

RETIRED_CLASS_NAMES = (
    "BLELifecycleService",
    "BLEManagementCommandsService",
)

RETIRED_MODULE_NAMES = (
    "lifecycle_service.py",
    "lifecycle_compat_service.py",
    "lifecycle_compat_wiring.py",
    "management_compat_service.py",
    "management_service.py",
)

SNAPSHOT_ASSIGN_RE = re.compile(
    r"^_ORIGINAL_[A-Z0-9_]+\s*=\s*\w+\.\w+",
    re.MULTILINE,
)

BLE_PACKAGE_DIR = (
    Path(__file__).resolve().parent.parent / "meshtastic" / "interfaces" / "ble"
)


def _ble_python_sources() -> list[Path]:
    """Return every Python source file in the BLE implementation package."""
    return sorted(BLE_PACKAGE_DIR.rglob("*.py"))


@pytest.mark.unit
def test_retired_service_class_names_do_not_reappear() -> None:
    """No BLE implementation module may reference a retired shim class name."""
    offenders: list[str] = []
    for path in _ble_python_sources():
        text = path.read_text()
        for class_name in RETIRED_CLASS_NAMES:
            if re.search(rf"\b{class_name}\b", text):
                offenders.append(f"{path.name}: {class_name}")
    assert offenders == []


@pytest.mark.unit
def test_retired_shim_hosting_modules_do_not_reappear() -> None:
    """No shim-hosting module may exist in the BLE implementation package."""
    present = {path.name for path in BLE_PACKAGE_DIR.glob("*.py")}
    offenders = [name for name in RETIRED_MODULE_NAMES if name in present]
    assert offenders == []


@pytest.mark.unit
def test_no_import_time_method_snapshots_in_ble_runtime() -> None:
    """BLE runtime code must not capture method baselines for self-comparison."""
    offenders: list[str] = []
    for path in _ble_python_sources():
        text = path.read_text()
        if SNAPSHOT_ASSIGN_RE.search(text):
            offenders.append(path.name)
    assert offenders == []


@pytest.mark.unit
def test_documented_stable_service_aliases_are_still_importable() -> None:
    """Stable compatibility promises survive the internal service retirement."""
    from meshtastic.interfaces.ble.compatibility_service import (
        BLECompatibilityEventService,
    )
    from meshtastic.interfaces.ble.receive_compat_service import (
        BLEReceiveRecoveryService as original,
    )
    from meshtastic.interfaces.ble.receive_service import BLEReceiveRecoveryService

    assert BLEReceiveRecoveryService is original
    assert callable(BLECompatibilityEventService.publish_connection_status_legacy)


@pytest.mark.unit
def test_runtime_does_not_route_through_retained_service_shims() -> None:
    """Keep the compatibility surfaces isolated from runtime orchestration."""
    compatibility_modules = {
        "compatibility_service.py",
        "receive_service.py",
        "receive_compat_service.py",
    }
    for path in _ble_python_sources():
        if path.name not in compatibility_modules:
            text = path.read_text(encoding="utf-8")
            assert "BLEReceiveRecoveryService" not in text, path
            assert "BLECompatibilityEventService" not in text, path


@pytest.mark.unit
def test_legacy_status_service_delegates_to_same_static_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

    from meshtastic.interfaces.ble.compatibility_service import (
        BLECompatibilityEventService,
    )

    iface = MagicMock()
    publishing_thread = MagicMock()
    publishing_thread.thread.is_alive.return_value = True
    publish = MagicMock()
    monkeypatch.setattr(
        BLECompatibilityEventService, "publish_connection_status", publish
    )
    BLECompatibilityEventService.publish_connection_status_legacy(
        iface, True, publishing_thread=publishing_thread
    )
    publish.assert_called_once_with(iface, True, publishing_thread=publishing_thread)


@pytest.mark.unit
def test_legacy_status_service_publishes_on_supplied_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retained shim publishes the legacy topic on the caller's queue."""
    from queue import Queue
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from meshtastic import mesh_interface
    from meshtastic.interfaces.ble.compatibility_service import (
        BLECompatibilityEventService,
    )

    iface = MagicMock()
    publishing_thread = SimpleNamespace(queue=Queue())
    publish = MagicMock()
    monkeypatch.setattr(mesh_interface, "pub", SimpleNamespace(sendMessage=publish))

    BLECompatibilityEventService.publish_connection_status_legacy(
        iface, True, publishing_thread=publishing_thread
    )

    publish.assert_not_called()
    callback = publishing_thread.queue.get_nowait()
    callback()
    publish.assert_called_once_with(
        "meshtastic.connection.status", interface=iface, connected=True
    )


@pytest.mark.unit
def test_retained_queue_helpers_delegate_using_supplied_thread() -> None:
    from threading import Event
    from unittest.mock import MagicMock, patch

    from meshtastic.interfaces.ble.compatibility_service import (
        BLECompatibilityEventPublisher,
        BLECompatibilityEventService,
    )

    iface, thread, callback = MagicMock(), object(), MagicMock()
    event = Event()
    with (
        patch.object(
            BLECompatibilityEventPublisher,
            "_enqueue_publish_callback",
            return_value=True,
        ) as enqueue,
        patch.object(
            BLECompatibilityEventPublisher, "_flush_disconnect_notifications"
        ) as flush,
        patch.object(
            BLECompatibilityEventPublisher, "_drain_publish_queue_on_thread"
        ) as drain,
    ):
        assert BLECompatibilityEventService._enqueue_publish_callback(
            thread, callback, prefer_non_blocking=True
        )
        BLECompatibilityEventService.wait_for_disconnect_notifications(
            iface, 0.5, publishing_thread=thread
        )
        BLECompatibilityEventService.drain_publish_queue(
            iface, event, publishing_thread=thread
        )
    enqueue.assert_called_once_with(thread, callback, prefer_non_blocking=True)
    flush.assert_called_once_with(0.5, publishing_thread=thread)
    drain.assert_called_once_with(event, publishing_thread=thread)
