"""Unit tests for --add-contact and --contact-qr CLI flags.

Tests cover the CLI integration of the contact URL feature:
- ``--add-contact <url>`` invokes ``Node.addContactURL()``
- ``--contact-qr <node_id>`` invokes ``Node.getContactURL()`` with correct flags
- ``--contact-verified`` and ``--contact-ignore`` modify the generated URL
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

from .. import mt_config
from ..__main__ import main
from ..node import Node
from ..serial_interface import SerialInterface


def _make_cli_mocks() -> tuple[MagicMock, MagicMock]:
    """Create a consistent (iface, mocked_node) pair for CLI tests.

    The iface mock is configured with ``__enter__``/``__exit__`` returning
    itself, matching how ``common()`` uses ``stack.enter_context()``.
    """

    mocked_node = MagicMock(autospec=Node)
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node
    mocked_node.iface = iface
    return iface, mocked_node


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_add_contact_url() -> None:
    """Test --add-contact with a shareable URL."""

    url = (
        "https://meshtastic.org/v/#CKqkvZgIElEKCSE4MzBmNTIyYRIQUm9hZHJ1bm5lciBSaWRnZRoE"
        "UktTTiIGAAAAAAAAKAk4AkIgRxo_Fw_ergQIhRqBbrHasLYy3gU-Ay8hrhu4OVnIPQc="
    )
    sys.argv = ["", "--add-contact", url]
    mt_config.args = sys.argv  # type: ignore[assignment]  # type: ignore[assignment]
    iface, mocked_node = _make_cli_mocks()
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()

    mocked_node.addContactURL.assert_called_once_with(url)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_contact_qr() -> None:
    """Test --contact-qr with a node ID."""

    sys.argv = ["", "--contact-qr", "!830f522a"]
    mt_config.args = sys.argv  # type: ignore[assignment]
    iface, mocked_node = _make_cli_mocks()
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()

    mocked_node.getContactURL.assert_called_once_with(
        "!830f522a", should_ignore=False, manually_verified=False
    )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_contact_qr_with_flags() -> None:
    """Test --contact-qr with --contact-verified and --contact-ignore."""

    sys.argv = [
        "",
        "--contact-qr",
        "!830f522a",
        "--contact-verified",
        "--contact-ignore",
    ]
    mt_config.args = sys.argv  # type: ignore[assignment]
    iface, mocked_node = _make_cli_mocks()
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()

    mocked_node.getContactURL.assert_called_once_with(
        "!830f522a", should_ignore=True, manually_verified=True
    )
