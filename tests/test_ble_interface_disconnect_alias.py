from typing import Any
from unittest.mock import MagicMock

import pytest

from meshtastic.interfaces.ble.interface import BLEInterface


def test_disconnect_delegates_to_close() -> None:
    iface: Any = object.__new__(BLEInterface)
    iface.close = MagicMock()

    BLEInterface.disconnect(iface)

    iface.close.assert_called_once_with(timeout=None)


def test_disconnect_forwards_timeout_to_close() -> None:
    iface: Any = object.__new__(BLEInterface)
    iface.close = MagicMock()

    BLEInterface.disconnect(iface, timeout=1.25)

    iface.close.assert_called_once_with(timeout=1.25)


def test_disconnect_propagates_close_errors() -> None:
    iface: Any = object.__new__(BLEInterface)
    iface.close = MagicMock(side_effect=RuntimeError("close failed"))

    with pytest.raises(RuntimeError, match="close failed"):
        BLEInterface.disconnect(iface)
