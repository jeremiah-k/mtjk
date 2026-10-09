"""Unit tests for _NodeChannelPresentationRuntime in channel_presentation_runtime.py."""

# pylint: disable=redefined-outer-name

import re
import threading
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, create_autospec, patch

import pytest

from meshtastic.node_runtime.channel_export_runtime import _NodeChannelExportRuntime
from meshtastic.node_runtime.channel_presentation_runtime import (
    _NodeChannelPresentationRuntime,
)
from meshtastic.protobuf import channel_pb2, localonly_pb2

from ._node_channel_state_test_support import _attach_channel_state


@pytest.fixture
def mock_node() -> MagicMock:
    """Create a minimal mock Node with channels list and lock.

    Returns
    -------
    MagicMock
        A mock node with channels attribute and _channels_lock.
    """
    node = MagicMock(
        spec=[
            "channels",
            "_channels_lock",
            "iface",
            "localConfig",
            "moduleConfig",
            "_test_channel_state",
        ]
    )
    channel_state = _attach_channel_state(node)
    channel_state.channels = []
    node.iface = SimpleNamespace(_node_db_lock=threading.RLock())
    node.localConfig = None
    node.moduleConfig = None
    return node


@pytest.fixture
def presentation_runtime(mock_node: MagicMock) -> _NodeChannelPresentationRuntime:
    """Create a _NodeChannelPresentationRuntime with mocked dependencies.

    Parameters
    ----------
    mock_node : MagicMock
        The mock node fixture.

    Returns
    -------
    _NodeChannelPresentationRuntime
        The presentation runtime instance with a mocked export_runtime.
    """
    export_runtime = create_autospec(_NodeChannelExportRuntime, instance=True)
    export_runtime.get_url.return_value = "https://meshtastic.org/e/#test"
    return _NodeChannelPresentationRuntime(
        mock_node,
        channel_state=mock_node._test_channel_state,
        export_runtime=export_runtime,
    )


@pytest.mark.unit
def test_show_info_routes_output_through_injected_sink(
    mock_node: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Channel/preferences presentation must honor an injected print sink."""
    export_runtime = create_autospec(_NodeChannelExportRuntime, instance=True)
    export_runtime.get_url.return_value = "https://meshtastic.org/e/#test"
    sink: list[str] = []

    runtime = _NodeChannelPresentationRuntime(
        mock_node,
        channel_state=mock_node._test_channel_state,
        export_runtime=export_runtime,
        cli_print=sink.append,
    )

    runtime._show_info()

    out, err = capsys.readouterr()
    assert out == ""
    assert err == ""
    joined = "".join(sink)
    assert "Preferences: " in joined
    assert "Module preferences: " in joined
    assert "Channels:" in joined
    assert "Primary channel URL: https://meshtastic.org/e/#test" in joined


@pytest.mark.unit
def test_show_channels_with_no_channels(
    presentation_runtime: _NodeChannelPresentationRuntime,
    mock_node: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test show_channels prints Channels header and URLs when no channels exist."""
    mock_node.channels = []

    presentation_runtime._show_channels()

    out, _ = capsys.readouterr()
    assert "Channels:" in out
    assert "Primary channel URL: https://meshtastic.org/e/#test" in out
    # When public_url == admin_url, complete URL line should NOT be printed
    assert "Complete URL" not in out


@pytest.mark.unit
def test_show_channels_with_channels(
    presentation_runtime: _NodeChannelPresentationRuntime,
    mock_node: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test show_channels prints channel details for non-disabled channels."""
    primary = channel_pb2.Channel(index=0, role=channel_pb2.Channel.Role.PRIMARY)
    primary.settings.name = "primary"
    primary.settings.psk = b"\x01"

    disabled = channel_pb2.Channel(index=1, role=channel_pb2.Channel.Role.DISABLED)
    disabled.settings.name = "disabled"

    mock_node.channels = [primary, disabled]

    presentation_runtime._show_channels()

    out, _ = capsys.readouterr()
    assert "Channels:" in out
    assert "Index 0: PRIMARY" in out
    assert "Index 1:" not in out  # DISABLED channels are not printed
    assert "Primary channel URL:" in out


@pytest.mark.unit
def test_show_channels_uses_one_snapshot_for_display_and_both_urls(
    mock_node: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Printed channels and both exported URLs should share one state snapshot."""
    primary = channel_pb2.Channel(index=0, role=channel_pb2.Channel.Role.PRIMARY)
    primary.settings.name = "captured"
    primary.settings.psk = b"\x01"
    mock_node.channels = [primary]
    export_runtime = _NodeChannelExportRuntime(
        mock_node,
        channel_state=mock_node._test_channel_state,
    )
    presentation_runtime = _NodeChannelPresentationRuntime(
        mock_node,
        channel_state=mock_node._test_channel_state,
        export_runtime=export_runtime,
    )

    def _encode_snapshot(
        channels_snapshot: list[channel_pb2.Channel],
        *,
        include_all: bool,
    ) -> str:
        assert channels_snapshot[0].settings.name == "captured"
        return "complete-encoded" if include_all else "public-encoded"

    with (
        patch.object(
            export_runtime,
            "_build_and_encode_channel_set",
            side_effect=_encode_snapshot,
        ) as encode_snapshot,
        patch.object(export_runtime, "get_url") as compatibility_export,
    ):
        presentation_runtime._show_channels()

    assert encode_snapshot.call_count == 2
    first_snapshot = encode_snapshot.call_args_list[0].args[0]
    second_snapshot = encode_snapshot.call_args_list[1].args[0]
    assert first_snapshot is second_snapshot
    compatibility_export.assert_not_called()
    out, _ = capsys.readouterr()
    assert "Primary channel URL: https://meshtastic.org/e/#public-encoded" in out
    assert (
        "Complete URL (includes all channels): "
        "https://meshtastic.org/e/#complete-encoded" in out
    )


@pytest.mark.unit
def test_show_channels_handles_unknown_role_values(
    presentation_runtime: _NodeChannelPresentationRuntime,
    mock_node: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """show_channels should not raise when a channel has an unknown enum role."""
    channel = channel_pb2.Channel(
        index=0,
        role=cast(channel_pb2.Channel.Role.ValueType, 999),
    )
    channel.settings.name = "mystery"
    channel.settings.psk = b"\x01"
    mock_node.channels = [channel]

    presentation_runtime._show_channels()

    out, _ = capsys.readouterr()
    assert "UNKNOWN(999)" in out


@pytest.mark.unit
def test_show_channels_handles_export_errors_without_raising(
    mock_node: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    """show_channels should keep presentation output even if URL export fails."""
    export_runtime = create_autospec(_NodeChannelExportRuntime, instance=True)
    export_runtime.get_url.side_effect = RuntimeError("channels not loaded")
    presentation_runtime = _NodeChannelPresentationRuntime(
        mock_node,
        channel_state=mock_node._test_channel_state,
        export_runtime=export_runtime,
    )
    mock_node.channels = []

    presentation_runtime._show_channels()

    out, _ = capsys.readouterr()
    assert "Channels:" in out
    assert "Primary channel URL: unavailable" in out


@pytest.mark.unit
def test_show_channels_shows_complete_url_when_different_from_public(
    mock_node: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test show_channels prints complete URL when admin_url differs from public_url.

    This exercises the branch that prints complete URL when admin_url differs.
    """
    export_runtime = create_autospec(_NodeChannelExportRuntime, instance=True)

    def get_url_side_effect(*, include_all: bool = True) -> str:
        return (
            "https://meshtastic.org/e/#complete"
            if include_all
            else "https://meshtastic.org/e/#public"
        )

    export_runtime.get_url.side_effect = get_url_side_effect
    presentation_runtime = _NodeChannelPresentationRuntime(
        mock_node,
        channel_state=mock_node._test_channel_state,
        export_runtime=export_runtime,
    )

    # Add multiple secondary channels so admin_url != public_url
    primary = channel_pb2.Channel(index=0, role=channel_pb2.Channel.Role.PRIMARY)
    primary.settings.name = "primary"
    primary.settings.psk = b"\x01"

    secondary = channel_pb2.Channel(index=1, role=channel_pb2.Channel.Role.SECONDARY)
    secondary.settings.name = "secondary"
    secondary.settings.psk = b"\x02"

    mock_node.channels = [primary, secondary]

    presentation_runtime._show_channels()

    out, _ = capsys.readouterr()
    assert "Primary channel URL: https://meshtastic.org/e/#public" in out
    assert (
        "Complete URL (includes all channels): https://meshtastic.org/e/#complete"
        in out
    )


@pytest.mark.unit
def test_show_info_with_no_config(
    presentation_runtime: _NodeChannelPresentationRuntime,
    mock_node: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test show_info prints empty preferences when no config is set."""
    mock_node.localConfig = None
    mock_node.moduleConfig = None
    mock_node.channels = []

    presentation_runtime._show_info()

    out, _ = capsys.readouterr()
    assert "Preferences: " in out
    assert "Module preferences: " in out
    assert "Channels:" in out
    assert "Primary channel URL:" in out


@pytest.mark.unit
def test_show_info_with_local_config(
    presentation_runtime: _NodeChannelPresentationRuntime,
    mock_node: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test show_info prints preferences when localConfig is populated."""
    mock_node.localConfig = localonly_pb2.LocalConfig()
    mock_node.localConfig.lora.hop_limit = 3
    mock_node.moduleConfig = localonly_pb2.LocalModuleConfig()
    mock_node.channels = []

    presentation_runtime._show_info()

    out, _ = capsys.readouterr()
    assert "Preferences:" in out
    assert "Module preferences:" in out
    assert "Channels:" in out
    # Verify hop_limit value is printed (from localConfig.lora.hop_limit = 3)
    # Note: We lowercase the output for case-insensitive matching (protobuf uses camelCase: hopLimit)
    assert re.search(
        r'"hoplimit"\s*:\s*3', out.lower()
    ), f"hop_limit=3 not found in output: {out[:200]}"
