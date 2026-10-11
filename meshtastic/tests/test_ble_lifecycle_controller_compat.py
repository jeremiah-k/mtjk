"""Compat delegation seams of the BLE lifecycle controller runtime.

Pins that ``BLELifecycleController`` falls back to the lifecycle service's
monkeypatchable status helpers when callers rely on legacy override points.
"""

from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from meshtastic.interfaces.ble.interface import BLEInterface
from meshtastic.interfaces.ble.lifecycle_compat_service import BLELifecycleService
from meshtastic.interfaces.ble.lifecycle_controller_runtime import (
    BLELifecycleController,
)


@pytest.mark.unit
def test_verify_and_publish_compat_status_shims_delegate_to_patched_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Patched service status helpers receive the controller's bound interface."""
    status_sentinel = (False, True)
    snapshot_sentinel = object()
    seen: dict[str, Any] = {}

    def _fake_status_locked(iface: object, _client: object) -> tuple[bool, bool]:
        seen["status_iface"] = iface
        return status_sentinel

    def _fake_verify_snapshot(
        iface: object, _client: object, _device_key: object, _alias_key: object
    ) -> object:
        seen["snapshot_iface"] = iface
        return snapshot_sentinel

    monkeypatch.setattr(
        BLELifecycleService,
        "_get_connected_client_status_locked",
        staticmethod(_fake_status_locked),
    )
    monkeypatch.setattr(
        BLELifecycleService,
        "_verify_ownership_snapshot",
        staticmethod(_fake_verify_snapshot),
    )

    iface = MagicMock()
    controller = BLELifecycleController(cast("BLEInterface", iface))
    client = MagicMock()

    class _OwnershipDouble:
        """Record the compat shims by invoking them like the real coordinator."""

        def _verify_and_publish_connected(  # pylint: disable=too-many-arguments
            self,
            connected_client: Any,
            connected_device_key: str | None,
            connection_alias_key: str | None,
            *,
            restore_address: str | None,
            restore_last_connection_request: str | None,
            verify_ownership_snapshot: Any,
            get_connected_client_status_locked: Any,
        ) -> None:
            del (
                connected_device_key,
                connection_alias_key,
                restore_address,
                restore_last_connection_request,
            )
            seen["status"] = get_connected_client_status_locked(connected_client)
            seen["snapshot"] = verify_ownership_snapshot(
                connected_client, "device-key", "alias-key"
            )

    controller._connection_ownership = _OwnershipDouble()  # type: ignore[assignment]

    controller._verify_and_publish_connected(
        client,
        "device-key",
        "alias-key",
        restore_address=None,
        restore_last_connection_request=None,
    )

    assert seen["status"] == status_sentinel
    assert seen["status_iface"] is iface
    assert seen["snapshot"] is snapshot_sentinel
    assert seen["snapshot_iface"] is iface
