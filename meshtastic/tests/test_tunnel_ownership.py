"""Regression tests for the single-active-tunnel ownership contract."""

import threading
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import pytest
from pubsub import pub

from meshtastic import mt_config
from meshtastic import tunnel as tunnel_module
from meshtastic.protobuf import mesh_pb2
from meshtastic.tunnel import TUNNEL_TOPIC, Tunnel, onTunnelReceive

pytestmark = pytest.mark.unit
_WAIT = 5.0
_PACKET: dict[str, Any] = {"from": 2, "decoded": {"payload": b"\x45" + b"\0" * 19}}


def _interface(*, no_proto: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        myInfo=mesh_pb2.MyNodeInfo(my_node_num=1),
        nodes={},
        noProto=no_proto,
        sendData=Mock(),
    )


@pytest.fixture(autouse=True)
def _tunnel_environment(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Keep all tests hardware-independent and close the live owner on failure."""
    monkeypatch.setattr("meshtastic.tunnel.platform.system", lambda: "Linux")
    assert mt_config.tunnel_instance is None
    yield
    if mt_config.tunnel_instance is not None:
        mt_config.tunnel_instance.close()


def test_callback_routes_only_to_the_bound_interface() -> None:
    """Identical-valued interfaces are still distinct packet sources."""
    iface = _interface()
    other = _interface()
    other.sendData = iface.sendData
    assert iface == other and iface is not other
    tunnel = Tunnel(iface)
    try:
        iface.noProto = False
        device = MagicMock()
        tunnel.tun = device
        with patch.object(tunnel, "_should_filter_packet", return_value=False):
            onTunnelReceive(_PACKET, other)
            device.write.assert_not_called()
            onTunnelReceive(_PACKET, iface)
            device.write.assert_called_once_with(_PACKET["decoded"]["payload"])
    finally:
        tunnel.close()


def test_second_tunnel_rejected_without_mutating_subscription() -> None:
    """A duplicate cannot create resources or replace the active singleton."""
    iface = _interface()
    with (
        patch("meshtastic.tunnel.pub.subscribe") as subscribe,
        patch("meshtastic.tunnel.pub.unsubscribe") as unsubscribe,
        patch("meshtastic.tunnel.LinuxTunDevice") as device,
    ):
        tunnel = Tunnel(iface)
        try:
            with pytest.raises(Tunnel.TunnelError, match="Only one active Tunnel"):
                Tunnel(_interface(no_proto=False))
            assert mt_config.tunnel_instance is tunnel
            subscribe.assert_called_once_with(onTunnelReceive, TUNNEL_TOPIC)
            unsubscribe.assert_not_called()
            device.assert_not_called()
        finally:
            tunnel.close()
        unsubscribe.assert_called_once_with(onTunnelReceive, TUNNEL_TOPIC)


def test_failed_construction_releases_slot_and_device() -> None:
    """Partial TUN configuration failure leaves no owner or subscription."""
    with (
        patch("meshtastic.tunnel.LinuxTunDevice") as factory,
        patch("meshtastic.tunnel.pub.subscribe") as subscribe,
        patch("meshtastic.tunnel.pub.unsubscribe") as unsubscribe,
    ):
        factory.return_value.ifconfig.side_effect = OSError("configuration failed")
        with pytest.raises(OSError, match="configuration failed"):
            Tunnel(_interface(no_proto=False))
        factory.return_value.close.assert_called_once_with()
        assert mt_config.tunnel_instance is None
        subscribe.assert_not_called()
        unsubscribe.assert_not_called()
        next_tunnel = Tunnel(_interface())
        next_tunnel.close()
        subscribe.assert_called_once_with(onTunnelReceive, TUNNEL_TOPIC)
        unsubscribe.assert_called_once_with(onTunnelReceive, TUNNEL_TOPIC)


def test_partial_subscription_failure_rolls_back() -> None:
    """An exception during pubsub registration cannot strand the singleton."""
    with (
        patch(
            "meshtastic.tunnel.pub.subscribe",
            side_effect=RuntimeError("subscribe failed"),
        ),
        patch("meshtastic.tunnel.pub.unsubscribe") as unsubscribe,
    ):
        with pytest.raises(RuntimeError, match="subscribe failed"):
            Tunnel(_interface())
        assert mt_config.tunnel_instance is None
        unsubscribe.assert_called_once_with(onTunnelReceive, TUNNEL_TOPIC)
    tunnel = Tunnel(_interface())
    tunnel.close()


def test_reader_start_failure_rolls_back_subscription_and_device() -> None:
    """Failure to start the worker cannot leave a half-constructed owner."""
    with (
        patch("meshtastic.tunnel.LinuxTunDevice") as factory,
        patch("meshtastic.tunnel.pub.subscribe") as subscribe,
        patch("meshtastic.tunnel.pub.unsubscribe") as unsubscribe,
        patch(
            "meshtastic.tunnel.threading.Thread.start",
            side_effect=RuntimeError("thread start failed"),
        ),
    ):
        with pytest.raises(RuntimeError, match="thread start failed"):
            Tunnel(_interface(no_proto=False))
        assert mt_config.tunnel_instance is None
        subscribe.assert_called_once_with(onTunnelReceive, TUNNEL_TOPIC)
        unsubscribe.assert_called_once_with(onTunnelReceive, TUNNEL_TOPIC)
        factory.return_value.close.assert_called_once_with()
    tunnel = Tunnel(_interface())
    tunnel.close()


def test_construction_reserves_slot_before_tun_setup_finishes() -> None:
    """The public singleton is unpublished while construction owns the slot."""
    configuring = threading.Event()
    release = threading.Event()
    device = MagicMock()

    def configure(**_kwargs: object) -> None:
        configuring.set()
        assert release.wait(_WAIT)
        raise OSError("configuration failed")

    device.ifconfig.side_effect = configure
    with (
        patch("meshtastic.tunnel.LinuxTunDevice", return_value=device),
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        future = pool.submit(Tunnel, _interface(no_proto=False))
        try:
            assert configuring.wait(_WAIT)
            assert mt_config.tunnel_instance is None
            with pytest.raises(Tunnel.TunnelError, match="Only one active Tunnel"):
                Tunnel(_interface())
        finally:
            release.set()
        with pytest.raises(OSError, match="configuration failed"):
            future.result(_WAIT)
    tunnel = Tunnel(_interface())
    tunnel.close()


def test_close_during_construction_leaves_slot_reusable() -> None:
    """A tunnel retired mid-construction must never become the active singleton."""
    configuring = threading.Event()
    release = threading.Event()
    device = MagicMock()

    def configure(**_kwargs: object) -> None:
        configuring.set()
        assert release.wait(_WAIT)

    device.ifconfig.side_effect = configure
    with (
        patch("meshtastic.tunnel.LinuxTunDevice", return_value=device),
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        future = pool.submit(Tunnel, _interface(no_proto=False))
        try:
            assert configuring.wait(_WAIT)
            constructing = getattr(tunnel_module, "_tunnel_owner", None)
            if constructing is not None:
                constructing.close()
            assert mt_config.tunnel_instance is None
        finally:
            release.set()
        with pytest.raises(Tunnel.TunnelError, match="closed during initialization"):
            future.result(_WAIT)
    assert mt_config.tunnel_instance is None
    tunnel = Tunnel(_interface())
    tunnel.close()


def test_close_reserves_slot_and_stale_close_cannot_unsubscribe_replacement() -> None:
    """No replacement can begin during native cleanup; old close stays harmless."""
    closing = threading.Event()
    release = threading.Event()
    old = Tunnel(_interface())
    old_device = MagicMock()
    old.tun = old_device

    def block_close() -> None:
        closing.set()
        assert release.wait(_WAIT)

    old_device.close.side_effect = block_close
    with (
        patch(
            "meshtastic.tunnel.pub.unsubscribe", wraps=pub.unsubscribe
        ) as unsubscribe,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        future = pool.submit(old.close)
        try:
            assert closing.wait(_WAIT)
            assert mt_config.tunnel_instance is None
            with pytest.raises(Tunnel.TunnelError, match="Only one active Tunnel"):
                Tunnel(_interface())
            old.close()
            old_device.close.assert_called_once_with()
        finally:
            release.set()
        future.result(_WAIT)
        replacement = Tunnel(_interface())
        try:
            old.close()
            assert mt_config.tunnel_instance is replacement
            unsubscribe.assert_called_once_with(onTunnelReceive, TUNNEL_TOPIC)
        finally:
            replacement.close()
        assert unsubscribe.call_count == 2


def test_inflight_old_callback_does_not_write_after_close() -> None:
    """A callback already filtering an old packet cannot write to the next owner."""
    filtering = threading.Event()
    release = threading.Event()
    old_iface = _interface()
    old = Tunnel(old_iface)
    old_iface.noProto = False
    old_device = MagicMock()
    old.tun = old_device

    def filter_packet(_packet: bytes) -> bool:
        filtering.set()
        assert release.wait(_WAIT)
        return False

    with (
        patch.object(old, "_should_filter_packet", side_effect=filter_packet),
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        future = pool.submit(onTunnelReceive, _PACKET, old_iface)
        try:
            assert filtering.wait(_WAIT)
            old.close()
            next_iface = _interface()
            replacement = Tunnel(next_iface)
            try:
                next_iface.noProto = False
                new_device = MagicMock()
                replacement.tun = new_device
                release.set()
                future.result(_WAIT)
                old_device.write.assert_not_called()
                new_device.write.assert_not_called()
            finally:
                replacement.close()
        finally:
            release.set()
            old.close()


def test_retired_reader_discards_late_native_read() -> None:
    """A blocked native read may finish after close but must not forward its packet."""
    reading = threading.Event()
    release = threading.Event()
    device = MagicMock()
    iface = _interface(no_proto=False)

    def read() -> bytes:
        reading.set()
        assert release.wait(_WAIT)
        return b"\x45" + b"\0" * 15 + b"\x0a\x73\xff\xff"

    device.read.side_effect = read
    with (
        patch("meshtastic.tunnel.LinuxTunDevice", return_value=device),
        patch("meshtastic.tunnel.RX_THREAD_JOIN_TIMEOUT", 0.01),
    ):
        tunnel = Tunnel(iface)
        reader = tunnel._rx_thread
        assert reader is not None
        try:
            assert reading.wait(_WAIT)
            tunnel.close()
            replacement = Tunnel(_interface())
            try:
                release.set()
                reader.join(_WAIT)
                assert not reader.is_alive()
                iface.sendData.assert_not_called()
            finally:
                replacement.close()
        finally:
            release.set()
            tunnel.close()
            reader.join(_WAIT)


def test_pubsub_subscription_survives_stale_close() -> None:
    """Exercise actual topic registration/dispatch in the normal dependency environment."""
    old = Tunnel(_interface())
    old.close()
    iface = _interface()
    tunnel = Tunnel(iface)
    try:
        old.close()
        iface.noProto = False
        device = MagicMock()
        tunnel.tun = device
        with patch.object(tunnel, "_should_filter_packet", return_value=False):
            pub.sendMessage(TUNNEL_TOPIC, packet=_PACKET, interface=_interface())
            device.write.assert_not_called()
            pub.sendMessage(TUNNEL_TOPIC, packet=_PACKET, interface=iface)
            device.write.assert_called_once_with(_PACKET["decoded"]["payload"])
    finally:
        tunnel.close()
