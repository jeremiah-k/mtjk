"""Unit tests for --add-contact and --contact-qr CLI flags.

Tests cover the CLI integration of the contact URL feature:
- ``--add-contact <url>`` invokes ``Node.addContactURL()``
- ``--contact-qr <node_id>`` invokes ``localNode.getContactURL()`` with correct flags
- ``--contact-verified`` and ``--contact-ignore`` modify the generated URL
- Modifier flags without ``--contact-qr`` exit with an error
- ``--contact-qr`` with ``--dest`` still uses localNode (not the destination node)
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

from ..__main__ import main
from ..node import Node
from ..serial_interface import SerialInterface


def _make_cli_mocks() -> tuple[MagicMock, MagicMock, MagicMock]:
    """Create a consistent (iface, local_node, remote_node) pair for CLI tests.

    The iface mock is configured with ``__enter__``/``__exit__`` returning
    itself, matching how ``common()`` uses ``stack.enter_context()``.
    Returns (iface, localNode_mock, getNode_mock) so tests can assert which
    node was used.
    """

    local_node = MagicMock(autospec=Node)
    remote_node = MagicMock(autospec=Node)
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.localNode = local_node
    iface.getNode.return_value = remote_node
    local_node.iface = iface
    remote_node.iface = iface
    return iface, local_node, remote_node


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_add_contact_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test --add-contact with a shareable URL."""

    url = (
        "https://meshtastic.org/v/#CKqkvZgIElEKCSE4MzBmNTIyYRIQUm9hZHJ1bm5lciBSaWRnZRoE"
        "UktTTiIGAAAAAAAAKAk4AkIgRxo_Fw_ergQIhRqBbrHasLYy3gU-Ay8hrhu4OVnIPQc="
    )
    monkeypatch.setattr(sys, "argv", ["", "--add-contact", url])
    iface, _local_node, remote_node = _make_cli_mocks()
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()

    remote_node.addContactURL.assert_called_once_with(url)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_contact_qr_uses_localnode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test --contact-qr invokes localNode.getContactURL(), not getNode()."""

    monkeypatch.setattr(sys, "argv", ["", "--contact-qr", "!830f522a"])
    iface, local_node, remote_node = _make_cli_mocks()
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()

    local_node.getContactURL.assert_called_once_with(
        "!830f522a", should_ignore=False, manually_verified=False
    )
    # Verify getNode was NOT called for contact QR generation
    iface.getNode.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_contact_qr_ignores_dest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test --contact-qr with --dest still uses localNode, not the destination."""

    monkeypatch.setattr(
        sys, "argv", ["", "--contact-qr", "!830f522a", "--dest", "!12345678"]
    )
    iface, local_node, _remote_node = _make_cli_mocks()
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()

    local_node.getContactURL.assert_called_once_with(
        "!830f522a", should_ignore=False, manually_verified=False
    )
    iface.getNode.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_contact_qr_with_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test --contact-qr with --contact-verified and --contact-ignore."""

    monkeypatch.setattr(
        sys,
        "argv",
        ["", "--contact-qr", "!830f522a", "--contact-verified", "--contact-ignore"],
    )
    iface, local_node, _remote_node = _make_cli_mocks()
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()

    local_node.getContactURL.assert_called_once_with(
        "!830f522a", should_ignore=True, manually_verified=True
    )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_contact_verified_without_qr_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--contact-verified without --contact-qr should exit with error."""

    monkeypatch.setattr(sys, "argv", ["", "--contact-verified"])
    iface, _local_node, _remote_node = _make_cli_mocks()
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        with pytest.raises(SystemExit):
            main()

    err = capsys.readouterr().err
    assert "require --contact-qr" in err


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_contact_ignore_without_qr_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--contact-ignore without --contact-qr should exit with error."""

    monkeypatch.setattr(sys, "argv", ["", "--contact-ignore"])
    iface, _local_node, _remote_node = _make_cli_mocks()
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        with pytest.raises(SystemExit):
            main()

    err = capsys.readouterr().err
    assert "require --contact-qr" in err
