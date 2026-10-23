"""Terminal owner cleanup does not queue behind an unrelated BLE constructor."""

import threading

import pytest

from meshtastic.interfaces.ble import gating
from meshtastic.interfaces.ble.interface import BLEInterface

pytestmark = pytest.mark.unit


def test_nonowner_disconnect_does_not_wait_for_address_reservation() -> None:
    """Atomic registry ownership checks make unrelated address-lock waits unnecessary."""
    address = "aabbccddeeff"
    owner = object()
    other = BLEInterface.__new__(BLEInterface)
    finished = threading.Event()
    gating._mark_connected(address, owner=owner)

    def disconnect() -> None:
        """Record completion only after atomic nonowner cleanup returns."""
        other._mark_address_keys_disconnected(address)
        finished.set()

    thread = threading.Thread(target=disconnect, daemon=True)
    try:
        with gating._addr_lock_context(address) as lock, lock:
            thread.start()
            assert finished.wait(
                1
            ), "Nonowner cleanup waited for another constructor's reservation"
            assert gating._is_currently_connected_elsewhere(address, owner=other)
    finally:
        thread.join(2)
        gating._mark_disconnected(address, owner=owner)
    assert not thread.is_alive()
