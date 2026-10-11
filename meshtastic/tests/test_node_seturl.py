"""Unit tests for Node channel URL parsing, planning, writes, and failure handling."""

import base64
import logging
import re
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pytest import LogCaptureFixture

from ..mesh_interface import MeshInterface
from ..node import MAX_CHANNELS, Node
from ..protobuf import (
    admin_pb2,
    apponly_pb2,
    mesh_pb2,
)
from ..protobuf.channel_pb2 import Channel  # pylint: disable=E0611
from ._node_legacy_support import (
    _DropChannelsOnEnterCountLock,
    _encode_channel_set_to_url,
    _get_mock_call_arg,
    _make_recording_send_admin,
)

CHANNEL_LIMIT = MAX_CHANNELS


@pytest.mark.unit
def test_setURL_rejects_missing_fragment_and_empty_fragment_data(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """SetURL should fail fast for malformed fragment inputs once channels are loaded."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.channels = [Channel(index=0, role=Channel.Role.DISABLED)]

    with pytest.raises(MeshInterface.MeshInterfaceError, match="Invalid URL"):
        anode.setURL("https://meshtastic.org/e/not-a-fragment")

    with pytest.raises(MeshInterface.MeshInterfaceError, match="no channel data found"):
        anode.setURL("https://meshtastic.org/e/#")


@pytest.mark.unit
def test_setURL_add_only_adds_unique_named_channels(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """setURL(addOnly=True) should ignore existing/empty names and add only new ones."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    primary = Channel(index=0, role=Channel.Role.PRIMARY)
    primary.settings.name = "existing"
    disabled = Channel(index=1, role=Channel.Role.DISABLED)
    anode.channels = [primary, disabled]
    anode.localConfig.lora.hop_limit = 3
    anode._send_admin = MagicMock(return_value=mesh_pb2.MeshPacket())  # type: ignore[method-assign]

    channel_set = apponly_pb2.ChannelSet()
    existing = channel_set.settings.add()
    existing.name = "existing"
    existing.psk = b"\x01"
    empty = channel_set.settings.add()
    empty.name = ""
    empty.psk = b"\x02"
    new_channel = channel_set.settings.add()
    new_channel.name = "new-ch"
    new_channel.psk = b"\x03"
    channel_set.lora_config.hop_limit = 9
    url = _encode_channel_set_to_url(channel_set)

    anode.setURL(url, addOnly=True)

    assert anode.channels is not None
    assert anode.channels[1].settings.name == "new-ch"
    assert anode.channels[1].role == Channel.Role.SECONDARY
    assert anode.localConfig.lora.hop_limit == 9
    send_calls = anode._send_admin.call_args_list
    assert len(send_calls) == 2
    assert _get_mock_call_arg(send_calls[0], name="adminIndex", positional_index=3) == 0
    assert send_calls[0].args[0].HasField("set_channel")
    assert send_calls[0].args[0].set_channel.index == 1
    assert _get_mock_call_arg(send_calls[1], name="adminIndex", positional_index=3) == 0
    assert send_calls[1].args[0].HasField("set_config")
    assert send_calls[1].args[0].set_config.lora.hop_limit == 9


@pytest.mark.unit
def test_setURL_add_only_treats_names_as_case_insensitive_duplicates(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """setURL(addOnly=True) should ignore case-variant duplicate channel names."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    primary = Channel(index=0, role=Channel.Role.PRIMARY)
    primary.settings.name = "primary"
    disabled1 = Channel(index=1, role=Channel.Role.DISABLED)
    disabled2 = Channel(index=2, role=Channel.Role.DISABLED)
    anode.channels = [primary, disabled1, disabled2]
    anode._send_admin = MagicMock(return_value=mesh_pb2.MeshPacket())  # type: ignore[method-assign]

    channel_set = apponly_pb2.ChannelSet()
    first = channel_set.settings.add()
    first.name = "Admin"
    first.psk = b"\x01"
    second = channel_set.settings.add()
    second.name = "admin"
    second.psk = b"\x02"
    url = _encode_channel_set_to_url(channel_set)

    anode.setURL(url, addOnly=True)

    assert anode.channels is not None
    assert anode.channels[1].settings.name == "Admin"
    assert anode.channels[2].role == Channel.Role.DISABLED
    send_calls = anode._send_admin.call_args_list
    assert len(send_calls) == 1
    assert send_calls[0].args[0].HasField("set_channel")
    assert send_calls[0].args[0].set_channel.settings.name == "Admin"


@pytest.mark.unit
def test_setURL_add_only_raises_when_no_disabled_slot_available(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """setURL(addOnly=True) should fail if no DISABLED channels remain."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.channels = [Channel(index=i, role=Channel.Role.SECONDARY) for i in range(2)]
    anode.localConfig.lora.hop_limit = 3

    channel_set = apponly_pb2.ChannelSet()
    new_channel = channel_set.settings.add()
    new_channel.name = "new-ch"
    new_channel.psk = b"\x01"
    url = _encode_channel_set_to_url(channel_set)

    with pytest.raises(
        MeshInterface.MeshInterfaceError, match="No free channels were found"
    ):
        anode.setURL(url, addOnly=True)


@pytest.mark.unit
def test_setURL_add_only_channel_only_url_skips_lora_snapshot_and_write(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """setURL(addOnly=True) should allow channel-only URLs without requiring cached LoRa."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    primary = Channel(index=0, role=Channel.Role.PRIMARY)
    primary.settings.name = "primary"
    disabled = Channel(index=1, role=Channel.Role.DISABLED)
    anode.channels = [primary, disabled]
    anode._send_admin = MagicMock(return_value=mesh_pb2.MeshPacket())  # type: ignore[method-assign]

    channel_set = apponly_pb2.ChannelSet()
    added = channel_set.settings.add()
    added.name = "new-ch"
    added.psk = b"\x03"
    url = _encode_channel_set_to_url(channel_set)

    anode.setURL(url, addOnly=True)

    assert anode.channels is not None
    assert anode.channels[1].settings.name == "new-ch"
    assert anode.channels[1].role == Channel.Role.SECONDARY
    assert anode.localConfig.HasField("lora") is False
    send_calls = anode._send_admin.call_args_list
    assert len(send_calls) == 1
    assert send_calls[0].args[0].HasField("set_channel")


@pytest.mark.unit
def test_setURL_add_only_defers_first_named_admin_write_until_end(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """setURL(addOnly=True) should defer a first named-admin write until other writes finish."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    primary = Channel(index=0, role=Channel.Role.PRIMARY)
    primary.settings.name = "primary"
    disabled1 = Channel(index=1, role=Channel.Role.DISABLED)
    disabled2 = Channel(index=2, role=Channel.Role.DISABLED)
    anode.channels = [primary, disabled1, disabled2]
    anode.localConfig.lora.hop_limit = 3

    operations: list[tuple[str, int | None]] = []
    anode._send_admin = _make_recording_send_admin(operations)  # type: ignore[method-assign,assignment]

    channel_set = apponly_pb2.ChannelSet()
    first = channel_set.settings.add()
    first.name = "admin"
    first.psk = b"\x03"
    second = channel_set.settings.add()
    second.name = "new-b"
    second.psk = b"\x04"
    channel_set.lora_config.hop_limit = 9
    url = _encode_channel_set_to_url(channel_set)

    anode.setURL(url, addOnly=True)

    assert operations == [
        ("channel:2", 0),
        ("lora", 0),
        ("channel:1", 0),
    ]


@pytest.mark.unit
def test_setURL_add_only_is_transactional_when_slots_are_insufficient(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """setURL(addOnly=True) should not partially mutate channels when it fails for capacity."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)

    primary = Channel(index=0, role=Channel.Role.PRIMARY)
    primary.settings.name = "primary"
    primary.settings.psk = b"\x01"
    secondary = Channel(index=1, role=Channel.Role.SECONDARY)
    secondary.settings.name = "existing"
    secondary.settings.psk = b"\x02"
    disabled = Channel(index=2, role=Channel.Role.DISABLED)
    anode.channels = [primary, secondary, disabled]
    anode.localConfig.lora.hop_limit = 3
    anode._send_admin = MagicMock(return_value=mesh_pb2.MeshPacket())  # type: ignore[method-assign]

    before_snapshot = [channel.SerializeToString() for channel in anode.channels]
    before_lora = anode.localConfig.lora.SerializeToString()

    channel_set = apponly_pb2.ChannelSet()
    first = channel_set.settings.add()
    first.name = "new-a"
    first.psk = b"\x03"
    second = channel_set.settings.add()
    second.name = "new-b"
    second.psk = b"\x04"
    channel_set.lora_config.hop_limit = 9
    url = _encode_channel_set_to_url(channel_set)

    with pytest.raises(
        MeshInterface.MeshInterfaceError, match="No free channels were found"
    ):
        anode.setURL(url, addOnly=True)

    assert anode.channels is not None
    after_snapshot = [channel.SerializeToString() for channel in anode.channels]
    assert after_snapshot == before_snapshot
    after_lora = anode.localConfig.lora.SerializeToString()
    assert after_lora == before_lora
    anode._send_admin.assert_not_called()


@pytest.mark.unit
def test_setURL_add_only_uses_snapshotted_admin_index_and_fails_fast_on_write_failure(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """setURL(addOnly=True) should keep using pre-mutation admin path and invalidate caches on failure."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)

    primary = Channel(index=0, role=Channel.Role.PRIMARY)
    primary.settings.name = "primary"
    primary.settings.psk = b"\x01"
    disabled1 = Channel(index=1, role=Channel.Role.DISABLED)
    disabled2 = Channel(index=2, role=Channel.Role.DISABLED)
    anode.channels = [primary, disabled1, disabled2]
    anode.localConfig.lora.hop_limit = 3
    ensure_session_key_spy = MagicMock(wraps=anode.ensureSessionKey)
    anode.ensureSessionKey = ensure_session_key_spy  # type: ignore[method-assign]

    staged_writes: list[tuple[int, str, int | None]] = []

    def _send_admin_with_staged_write_failure(
        msg: admin_pb2.AdminMessage,
        wantResponse: bool = False,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        adminIndex: int | None = None,
    ) -> mesh_pb2.MeshPacket:
        _ = (wantResponse, onResponse)
        if msg.HasField("set_channel"):
            if msg.set_channel.role == Channel.Role.SECONDARY:
                staged_writes.append(
                    (msg.set_channel.index, msg.set_channel.settings.name, adminIndex)
                )
                if len(staged_writes) == 2:
                    raise RuntimeError("write failed during addOnly batch")
        return mesh_pb2.MeshPacket()

    anode._send_admin = _send_admin_with_staged_write_failure  # type: ignore[method-assign,assignment]

    channel_set = apponly_pb2.ChannelSet()
    first = channel_set.settings.add()
    first.name = "admin"
    first.psk = b"\x03"
    second = channel_set.settings.add()
    second.name = "new-b"
    second.psk = b"\x04"
    url = _encode_channel_set_to_url(channel_set)

    with pytest.raises(RuntimeError, match="write failed during addOnly batch"):
        anode.setURL(url, addOnly=True)

    assert ensure_session_key_spy.call_args_list
    assert all(
        _get_mock_call_arg(call, name="adminIndex", positional_index=0) == 0
        for call in ensure_session_key_spy.call_args_list
    )

    assert staged_writes == [(2, "new-b", 0), (1, "admin", 0)]

    # Fail-fast: local channel cache is invalidated after partial write failure.
    assert anode.channels is None


@pytest.mark.unit
def test_setURL_add_only_deferred_admin_failure_fails_fast(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """Deferred addOnly admin-write failures should fail-fast and invalidate channel cache."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.iface.localNode = anode

    primary = Channel(index=0, role=Channel.Role.PRIMARY)
    primary.settings.name = "primary"
    disabled1 = Channel(index=1, role=Channel.Role.DISABLED)
    disabled2 = Channel(index=2, role=Channel.Role.DISABLED)
    anode.channels = [primary, disabled1, disabled2]

    deferred_failure_seen = {"seen": False}
    staged_writes: list[int] = []
    admin_indexes: list[int | None] = []

    def _send_admin_with_deferred_admin_failure(
        msg: admin_pb2.AdminMessage,
        wantResponse: bool = False,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        adminIndex: int | None = None,
    ) -> mesh_pb2.MeshPacket:
        _ = (wantResponse, onResponse)
        if msg.HasField("set_channel"):
            staged_writes.append(msg.set_channel.index)
            admin_indexes.append(adminIndex)
            if (
                msg.set_channel.role == Channel.Role.SECONDARY
                and msg.set_channel.settings.name == "admin"
                and not deferred_failure_seen["seen"]
            ):
                deferred_failure_seen["seen"] = True
                raise RuntimeError("deferred admin write failed")
        return mesh_pb2.MeshPacket()

    anode._send_admin = _send_admin_with_deferred_admin_failure  # type: ignore[method-assign,assignment]

    channel_set = apponly_pb2.ChannelSet()
    first = channel_set.settings.add()
    first.name = "admin"
    first.psk = b"\x03"
    second = channel_set.settings.add()
    second.name = "new-b"
    second.psk = b"\x04"
    url = _encode_channel_set_to_url(channel_set)

    with pytest.raises(RuntimeError, match="deferred admin write failed"):
        anode.setURL(url, addOnly=True)

    assert anode.channels is None
    assert deferred_failure_seen["seen"]
    assert staged_writes == [2, 1]
    assert admin_indexes == [0, 0]


@pytest.mark.unit
def test_setURL_add_only_skips_lora_clear_when_forward_write_never_started(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """setURL(addOnly=True) should skip LoRa cache clear when forward write fails before being marked started."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)

    primary = Channel(index=0, role=Channel.Role.PRIMARY)
    primary.settings.name = "primary"
    primary.settings.psk = b"\x01"
    disabled = Channel(index=1, role=Channel.Role.DISABLED)
    anode.channels = [primary, disabled]
    anode.localConfig.lora.hop_limit = 3
    ensure_session_key_spy = MagicMock(wraps=anode.ensureSessionKey)
    anode.ensureSessionKey = ensure_session_key_spy  # type: ignore[method-assign]

    failed_lora_send = {"seen": False}
    staged_channel_writes: list[int] = []
    admin_indexes: list[int | None] = []

    def _send_admin_with_lora_failure(
        msg: admin_pb2.AdminMessage,
        wantResponse: bool = False,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        adminIndex: int | None = None,
    ) -> mesh_pb2.MeshPacket:
        _ = (wantResponse, onResponse)
        admin_indexes.append(adminIndex)
        if (
            msg.HasField("set_channel")
            and msg.set_channel.role == Channel.Role.SECONDARY
        ):
            staged_channel_writes.append(msg.set_channel.index)
        if msg.HasField("set_config") and msg.set_config.HasField("lora"):
            if (
                not failed_lora_send["seen"]
                and adminIndex == 0
                and msg.set_config.lora.hop_limit == 9
            ):
                failed_lora_send["seen"] = True
                raise OSError("LoRa write failed")
        return mesh_pb2.MeshPacket()

    anode._send_admin = _send_admin_with_lora_failure  # type: ignore[method-assign,assignment]

    channel_set = apponly_pb2.ChannelSet()
    added = channel_set.settings.add()
    added.name = "admin"
    added.psk = b"\x03"
    channel_set.lora_config.hop_limit = 9
    url = _encode_channel_set_to_url(channel_set)

    with pytest.raises(OSError, match="LoRa write failed"):
        anode.setURL(url, addOnly=True)

    assert ensure_session_key_spy.call_args_list
    assert all(
        _get_mock_call_arg(call, name="adminIndex", positional_index=0) == 0
        for call in ensure_session_key_spy.call_args_list
    )
    assert anode.channels is None
    assert not staged_channel_writes
    assert admin_indexes == [0]
    assert anode.localConfig.lora.hop_limit == 3


@pytest.mark.unit
def test_setURL_replace_pins_admin_index_for_channel_and_lora_writes(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """setURL(addOnly=False) should pin admin path from pre-rewrite channel state."""
    iface = autospec_local_node_iface(MeshInterface)
    iface.localNode._get_admin_channel_index.return_value = 1
    iface.localNode._get_named_admin_channel_index = MagicMock(return_value=1)
    iface._get_or_create_by_num.return_value = {"adminSessionPassKey": b"secret"}
    anode = Node(iface, "!12345678", noProto=False)
    anode.localConfig.lora.hop_limit = 2

    primary = Channel(index=0, role=Channel.Role.PRIMARY)
    primary.settings.name = "primary"
    legacy_admin = Channel(index=1, role=Channel.Role.SECONDARY)
    legacy_admin.settings.name = "admin"
    anode.channels = [primary, legacy_admin]
    ensure_session_key_spy = MagicMock(wraps=anode.ensureSessionKey)
    anode.ensureSessionKey = ensure_session_key_spy  # type: ignore[method-assign]

    sent_messages: list[admin_pb2.AdminMessage] = []
    admin_indexes: list[int | None] = []

    def _capture_admin_index(
        msg: admin_pb2.AdminMessage,
        wantResponse: bool = False,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        adminIndex: int | None = None,
    ) -> mesh_pb2.MeshPacket:
        _ = (wantResponse, onResponse)
        sent_messages.append(msg)
        admin_indexes.append(adminIndex)
        return mesh_pb2.MeshPacket()

    anode._send_admin = _capture_admin_index  # type: ignore[method-assign,assignment]

    channel_set = apponly_pb2.ChannelSet()
    first = channel_set.settings.add()
    first.name = "new-primary"
    first.psk = b"\x11"
    second = channel_set.settings.add()
    second.name = "new-secondary"
    second.psk = b"\x12"
    channel_set.lora_config.hop_limit = 9
    url = _encode_channel_set_to_url(channel_set)

    anode.setURL(url, addOnly=False)

    assert ensure_session_key_spy.call_args_list
    assert all(
        _get_mock_call_arg(call, name="adminIndex", positional_index=0) == 1
        for call in ensure_session_key_spy.call_args_list
    )
    assert len(sent_messages) == 3
    assert admin_indexes == [1, 1, 1]
    assert sent_messages[0].HasField("set_channel")
    assert sent_messages[0].set_channel.index == 0
    assert sent_messages[1].HasField("set_config")
    assert sent_messages[1].set_config.HasField("lora")
    assert sent_messages[1].set_config.lora.hop_limit == 9
    assert sent_messages[2].HasField("set_channel")
    assert sent_messages[2].set_channel.index == 1
    assert anode.localConfig.lora.hop_limit == 9


@pytest.mark.unit
def test_setURL_replace_defers_first_named_admin_write_until_end(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """setURL(addOnly=False) should defer first named-admin write until after other writes."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    primary = Channel(index=0, role=Channel.Role.PRIMARY)
    primary.settings.name = "primary"
    secondary = Channel(index=1, role=Channel.Role.SECONDARY)
    secondary.settings.name = "secondary"
    anode.channels = [primary, secondary]
    anode.localConfig.lora.hop_limit = 3

    operations: list[tuple[str, int | None]] = []
    anode._send_admin = _make_recording_send_admin(operations)  # type: ignore[method-assign,assignment]

    channel_set = apponly_pb2.ChannelSet()
    first = channel_set.settings.add()
    first.name = "admin"
    first.psk = b"\x05"
    second = channel_set.settings.add()
    second.name = "new-b"
    second.psk = b"\x06"
    channel_set.lora_config.hop_limit = 11
    url = _encode_channel_set_to_url(channel_set)

    anode.setURL(url, addOnly=False)

    assert operations == [
        ("channel:1", 0),
        ("lora", 0),
        ("channel:0", 0),
    ]


@pytest.mark.unit
def test_setURL_replace_rejects_multiple_named_admin_channels(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """setURL(addOnly=False) should reject URLs that stage multiple admin channels."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.channels = [
        Channel(index=0, role=Channel.Role.DISABLED),
        Channel(index=1, role=Channel.Role.DISABLED),
        Channel(index=2, role=Channel.Role.DISABLED),
    ]

    channel_set = apponly_pb2.ChannelSet()
    first = channel_set.settings.add()
    first.name = "admin"
    first.psk = b"\x01"
    second = channel_set.settings.add()
    second.name = "AdMiN"
    second.psk = b"\x02"
    url = _encode_channel_set_to_url(channel_set)

    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match="multiple channels named 'admin'",
    ):
        anode.setURL(url, addOnly=False)


@pytest.mark.unit
def test_setURL_replace_when_admin_slot_moves_defers_old_slot_cleanup(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """setURL(addOnly=False) should apply moved named-admin channel before rewriting prior admin slot."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.iface.localNode = anode

    primary = Channel(index=0, role=Channel.Role.PRIMARY)
    primary.settings.name = "primary"
    old_admin = Channel(index=1, role=Channel.Role.SECONDARY)
    old_admin.settings.name = "admin"
    third = Channel(index=2, role=Channel.Role.SECONDARY)
    third.settings.name = "third"
    anode.channels = [primary, old_admin, third]
    anode.localConfig.lora.hop_limit = 3

    operations: list[tuple[str, int | None]] = []
    anode._send_admin = _make_recording_send_admin(operations)  # type: ignore[method-assign,assignment]

    channel_set = apponly_pb2.ChannelSet()
    moved_admin = channel_set.settings.add()
    moved_admin.name = "admin"
    moved_admin.psk = b"\x21"
    replacement_for_old_admin = channel_set.settings.add()
    replacement_for_old_admin.name = "secondary-new"
    replacement_for_old_admin.psk = b"\x22"
    replacement_third = channel_set.settings.add()
    replacement_third.name = "third-new"
    replacement_third.psk = b"\x23"
    channel_set.lora_config.hop_limit = 7
    url = _encode_channel_set_to_url(channel_set)

    anode.setURL(url, addOnly=False)

    assert operations == [
        ("channel:2", 1),
        ("lora", 1),
        ("channel:0", 1),
        ("channel:1", 0),
    ]


@pytest.mark.unit
def test_setURL_replace_fails_fast_invalidate_cache_after_deferred_failure(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """Replace-all should fail-fast and invalidate channel cache after deferred admin failure."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.iface.localNode = anode

    primary = Channel(index=0, role=Channel.Role.PRIMARY)
    primary.settings.name = "primary"
    old_admin = Channel(index=1, role=Channel.Role.SECONDARY)
    old_admin.settings.name = "admin"
    third = Channel(index=2, role=Channel.Role.SECONDARY)
    third.settings.name = "third"
    anode.channels = [primary, old_admin, third]

    deferred_failure_seen = {"seen": False}
    forward_writes: list[int] = []

    def _send_admin_with_deferred_admin_failure(
        msg: admin_pb2.AdminMessage,
        wantResponse: bool = False,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        adminIndex: int | None = None,
    ) -> mesh_pb2.MeshPacket:
        _ = (wantResponse, onResponse)
        if msg.HasField("set_channel"):
            if (
                msg.set_channel.index == 0
                and msg.set_channel.settings.name == "admin"
                and not deferred_failure_seen["seen"]
            ):
                deferred_failure_seen["seen"] = True
                raise RuntimeError("deferred admin write failed")
            forward_writes.append(msg.set_channel.index)
        return mesh_pb2.MeshPacket()

    anode._send_admin = _send_admin_with_deferred_admin_failure  # type: ignore[method-assign,assignment]

    channel_set = apponly_pb2.ChannelSet()
    moved_admin = channel_set.settings.add()
    moved_admin.name = "admin"
    moved_admin.psk = b"\x21"
    replacement_for_old_admin = channel_set.settings.add()
    replacement_for_old_admin.name = "secondary-new"
    replacement_for_old_admin.psk = b"\x22"
    replacement_third = channel_set.settings.add()
    replacement_third.name = "third-new"
    replacement_third.psk = b"\x23"
    url = _encode_channel_set_to_url(channel_set)

    with pytest.raises(RuntimeError, match="deferred admin write failed"):
        anode.setURL(url, addOnly=False)

    assert deferred_failure_seen["seen"] is True
    assert forward_writes == [2]
    assert anode.channels is None


@pytest.mark.unit
def test_setURL_replace_all_invalidates_cache_on_failure(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """setURL(addOnly=False) should invalidate channel cache on replace failure mid-flight."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.iface.localNode = anode

    primary = Channel(index=0, role=Channel.Role.PRIMARY)
    primary.settings.name = "primary"
    secondary = Channel(index=1, role=Channel.Role.SECONDARY)
    secondary.settings.name = "existing"
    disabled = Channel(index=2, role=Channel.Role.DISABLED)
    anode.channels = [primary, secondary, disabled]

    failed_stage_write = {"seen": False}

    def _send_admin_with_midflight_failure(
        msg: admin_pb2.AdminMessage,
        wantResponse: bool = False,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        adminIndex: int | None = None,
    ) -> mesh_pb2.MeshPacket:
        _ = (wantResponse, onResponse, adminIndex)
        if (
            msg.HasField("set_channel")
            and msg.set_channel.index == 1
            and msg.set_channel.settings.name == "new-secondary"
            and not failed_stage_write["seen"]
        ):
            failed_stage_write["seen"] = True
            raise RuntimeError("replace write failed")
        return mesh_pb2.MeshPacket()

    anode._send_admin = _send_admin_with_midflight_failure  # type: ignore[method-assign,assignment]

    channel_set = apponly_pb2.ChannelSet()
    first = channel_set.settings.add()
    first.name = "new-primary"
    first.psk = b"\x31"
    second = channel_set.settings.add()
    second.name = "new-secondary"
    second.psk = b"\x32"
    url = _encode_channel_set_to_url(channel_set)

    with pytest.raises(RuntimeError, match="replace write failed"):
        anode.setURL(url, addOnly=False)

    assert anode.channels is None


@pytest.mark.unit
def test_setURL_replace_skips_lora_clear_when_forward_write_never_started(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """setURL(addOnly=False) should skip LoRa cache clear when forward write fails before being marked started."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.iface.localNode = anode

    primary = Channel(index=0, role=Channel.Role.PRIMARY)
    primary.settings.name = "primary"
    secondary = Channel(index=1, role=Channel.Role.SECONDARY)
    secondary.settings.name = "existing"
    anode.channels = [primary, secondary]
    anode.localConfig.lora.hop_limit = 3

    failed_lora_send = {"seen": False}

    def _send_admin_with_lora_failure(
        msg: admin_pb2.AdminMessage,
        wantResponse: bool = False,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        adminIndex: int | None = None,
    ) -> mesh_pb2.MeshPacket:
        _ = (wantResponse, onResponse, adminIndex)
        if msg.HasField("set_config") and msg.set_config.HasField("lora"):
            if not failed_lora_send["seen"] and msg.set_config.lora.hop_limit == 9:
                failed_lora_send["seen"] = True
                raise OSError("LoRa replace write failed")
        return mesh_pb2.MeshPacket()

    anode._send_admin = _send_admin_with_lora_failure  # type: ignore[method-assign,assignment]

    channel_set = apponly_pb2.ChannelSet()
    first = channel_set.settings.add()
    first.name = "new-primary"
    first.psk = b"\x41"
    second = channel_set.settings.add()
    second.name = "new-secondary"
    second.psk = b"\x42"
    channel_set.lora_config.hop_limit = 9
    url = _encode_channel_set_to_url(channel_set)

    with pytest.raises(OSError, match="LoRa replace write failed"):
        anode.setURL(url, addOnly=False)

    assert anode.channels is None
    assert anode.localConfig.lora.hop_limit == 3


@pytest.mark.unit
def test_setURL_replace_channel_only_url_skips_lora_write_and_cache_update(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """setURL(addOnly=False) should not write LoRa when URL omits lora_config."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.channels = [
        Channel(index=0, role=Channel.Role.DISABLED),
        Channel(index=1, role=Channel.Role.DISABLED),
    ]
    anode.localConfig.lora.hop_limit = 3
    anode._send_admin = MagicMock(return_value=mesh_pb2.MeshPacket())  # type: ignore[method-assign]

    channel_set = apponly_pb2.ChannelSet()
    first = channel_set.settings.add()
    first.name = "new-primary"
    first.psk = b"\x11"
    second = channel_set.settings.add()
    second.name = "new-secondary"
    second.psk = b"\x12"
    url = _encode_channel_set_to_url(channel_set)

    anode.setURL(url, addOnly=False)

    send_calls = anode._send_admin.call_args_list
    assert len(send_calls) == 2
    assert all(call.args[0].HasField("set_channel") for call in send_calls)
    assert anode.localConfig.lora.hop_limit == 3


@pytest.mark.unit
def test_setURL_replace_disables_channels_omitted_from_url(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """setURL(addOnly=False) should disable stale channels not present in the replacement URL."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.iface.localNode = anode

    primary = Channel(index=0, role=Channel.Role.PRIMARY)
    primary.settings.name = "primary"
    admin_secondary = Channel(index=1, role=Channel.Role.SECONDARY)
    admin_secondary.settings.name = "admin"
    stale_secondary_a = Channel(index=2, role=Channel.Role.SECONDARY)
    stale_secondary_a.settings.name = "stale-a"
    stale_secondary_b = Channel(index=3, role=Channel.Role.SECONDARY)
    stale_secondary_b.settings.name = "stale-b"
    anode.channels = [primary, admin_secondary, stale_secondary_a, stale_secondary_b]
    anode._send_admin = MagicMock(return_value=mesh_pb2.MeshPacket())  # type: ignore[method-assign]

    channel_set = apponly_pb2.ChannelSet()
    first = channel_set.settings.add()
    first.name = "new-primary"
    first.psk = b"\x11"
    url = _encode_channel_set_to_url(channel_set)

    anode.setURL(url, addOnly=False)

    assert anode.channels is not None
    assert anode.channels[0].settings.name == "new-primary"
    for channel_index in (1, 2, 3):
        assert anode.channels[channel_index].role == Channel.Role.DISABLED
        assert anode.channels[channel_index].settings.name == ""

    channel_writes = [
        call.args[0].set_channel
        for call in anode._send_admin.call_args_list
        if call.args[0].HasField("set_channel")
    ]
    assert {channel.index for channel in channel_writes} == {0, 1, 2, 3}
    assert {
        channel.index
        for channel in channel_writes
        if channel.role == Channel.Role.DISABLED
    } == {1, 2, 3}


@pytest.mark.unit
def test_setURL_replace_rejects_channel_cache_loss_before_writes(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """Replace-all should reject channel-cache loss before starting device writes."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.channels = [Channel(index=0, role=Channel.Role.DISABLED)]
    # Drop the cache when execution validates the plan against current state.
    channels_lock = _DropChannelsOnEnterCountLock(anode, trigger_enter=3)
    anode._channels_lock = channels_lock

    channel_set = apponly_pb2.ChannelSet()
    setting = channel_set.settings.add()
    setting.name = "primary"
    setting.psk = b"\x01"
    url = _encode_channel_set_to_url(channel_set)

    with (
        patch.object(anode, "_write_channel_snapshot") as write_channel,
        pytest.raises(
            MeshInterface.MeshInterfaceError,
            match="Channel cache changed before replace-all write; aborting transaction",
        ),
    ):
        anode.setURL(url, addOnly=False)

    write_channel.assert_not_called()
    assert anode.channels is None


@pytest.mark.unit
def test_setURL_reports_decode_and_parse_errors(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """SetURL should surface base64 decode and protobuf parse failures."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.channels = [Channel(index=0, role=Channel.Role.DISABLED)]

    with pytest.raises(MeshInterface.MeshInterfaceError, match="Invalid URL"):
        anode.setURL("https://meshtastic.org/e/#_")

    bad_proto = base64.urlsafe_b64encode(b"\x00\x01").decode("ascii").rstrip("=")
    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match="Unable to parse channel settings from URL",
    ):
        anode.setURL(f"https://meshtastic.org/e/#{bad_proto}")


@pytest.mark.unit
def test_setURL_reports_empty_settings_when_channels_loaded(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """SetURL should reject URLs that decode to an empty ChannelSet."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.channels = [Channel(index=0, role=Channel.Role.DISABLED)]
    channel_set = apponly_pb2.ChannelSet()
    channel_set.lora_config.tx_enabled = True
    with pytest.raises(
        MeshInterface.MeshInterfaceError, match="There were no settings"
    ):
        anode.setURL(_encode_channel_set_to_url(channel_set))


@pytest.mark.unit
def test_setURL_add_only_rechecks_channels_before_addition(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """SetURL(addOnly=True) should fail if channels disappear before add loop mutation."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.channels = [Channel(index=0, role=Channel.Role.DISABLED)]
    anode.localConfig.lora.hop_limit = 3
    # The second acquisition is the add-only mutation recheck after planning.
    channels_lock = _DropChannelsOnEnterCountLock(anode, trigger_enter=2)
    anode._channels_lock = channels_lock

    channel_set = apponly_pb2.ChannelSet()
    setting = channel_set.settings.add()
    setting.name = "new-channel"
    setting.psk = b"\x01"
    with pytest.raises(
        MeshInterface.MeshInterfaceError, match="Config or channels not loaded"
    ):
        anode.setURL(_encode_channel_set_to_url(channel_set), addOnly=True)
    assert channels_lock.enters == 2


@pytest.mark.unit
def test_setURL_replace_rechecks_channels_before_length_calculation(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """SetURL replace path should fail if channels disappear before max-channel snapshot."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.channels = [Channel(index=0, role=Channel.Role.DISABLED)]
    # The second acquisition is the replace-path length/snapshot recheck.
    channels_lock = _DropChannelsOnEnterCountLock(anode, trigger_enter=2)
    anode._channels_lock = channels_lock

    channel_set = apponly_pb2.ChannelSet()
    setting = channel_set.settings.add()
    setting.name = "primary"
    setting.psk = b"\x01"
    with pytest.raises(
        MeshInterface.MeshInterfaceError, match="Config or channels not loaded"
    ):
        anode.setURL(_encode_channel_set_to_url(channel_set))
    assert channels_lock.enters == 2


@pytest.mark.unit
def test_setURL_raises_when_channels_not_loaded(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """Test setURL raises when config/channels are not loaded."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    with pytest.raises(
        MeshInterface.MeshInterfaceError, match="Config or channels not loaded"
    ):
        anode.setURL("")


@pytest.mark.unit
def test_setURL_ignores_channels_over_device_limit(
    caplog: LogCaptureFixture,
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """Test that setURL ignores channels beyond the fixed device channel limit."""
    iface = autospec_local_node_iface(MeshInterface)
    anode = Node(iface, "!12345678", noProto=True)
    anode.channels = [
        Channel(index=i, role=Channel.Role.DISABLED) for i in range(CHANNEL_LIMIT)
    ]
    anode.localConfig.lora.hop_limit = 2
    # Mock I/O operations to prevent actual device communication
    anode._write_channel_snapshot = MagicMock()  # type: ignore[method-assign]
    anode._send_admin = MagicMock(return_value=MagicMock())  # type: ignore[method-assign]

    channel_set = apponly_pb2.ChannelSet()
    for i in range(CHANNEL_LIMIT + 1):
        settings = channel_set.settings.add()
        settings.name = f"ch{i}"
        settings.psk = b"\x01"
    channel_set.lora_config.hop_limit = 7

    url = _encode_channel_set_to_url(channel_set)

    with caplog.at_level(logging.WARNING):
        anode.setURL(url)

    assert re.search(
        rf"URL contains more than {CHANNEL_LIMIT} channels",
        caplog.text,
        re.MULTILINE,
    )
    assert len(anode.channels) == CHANNEL_LIMIT
    assert anode.channels[0].settings.name == "ch0"
    assert anode.channels[CHANNEL_LIMIT - 1].settings.name == f"ch{CHANNEL_LIMIT - 1}"
    assert anode.localConfig.lora.hop_limit == 7
