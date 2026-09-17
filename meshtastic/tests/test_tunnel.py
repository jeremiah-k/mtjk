"""Meshtastic unit tests for tunnel.py."""

import argparse
import logging
import re
import sys
from collections.abc import Generator
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from meshtastic import mt_config

from ..mesh_interface import MeshInterface
from ..protobuf import mesh_pb2, portnums_pb2
from ..tcp_interface import TCPInterface
from ..tunnel import TUN_MTU, Tunnel, onTunnelReceive

pytestmark = pytest.mark.usefixtures("platform_socket_mocks")

# Custom logging level for trace-level logs
LOG_TRACE = 5


@pytest.fixture(autouse=True)
def reset_tunnel_mt_config_state() -> Generator[None, None, None]:
    """Reset mt_config module state before and after each tunnel test."""

    def _close_active_tunnel() -> None:
        tunnel = mt_config.tunnel_instance
        if tunnel is not None:
            tunnel.close()

    _close_active_tunnel()
    mt_config.reset()
    yield
    _close_active_tunnel()
    mt_config.reset()


@contextmanager
def _managed_tunnel(iface: MeshInterface) -> Generator[Tunnel, None, None]:
    """Yield a Tunnel and ensure close() is always called."""
    tun = Tunnel(iface)
    try:
        yield tun
    finally:
        tun.close()


@pytest.mark.unit
def test_Tunnel_on_non_linux_system(
    platform_socket_mocks: tuple[MagicMock, MagicMock],
) -> None:
    """Test that we cannot instantiate a Tunnel on a non Linux system."""
    mock_platform_system, _ = platform_socket_mocks
    mock_platform_system.return_value = "notLinux"
    iface = TCPInterface(hostname="localhost", noProto=True)
    try:
        with pytest.raises(Tunnel.TunnelError) as pytest_wrapped_e:
            Tunnel(iface)
    finally:
        iface.close()
    assert issubclass(pytest_wrapped_e.type, Tunnel.TunnelError)


@pytest.mark.unit
def test_tunnel_mtu_tracks_mesh_data_payload_contract() -> None:
    """Tunnel MTU should use the generated maximum Data.payload size."""
    assert TUN_MTU == mesh_pb2.Constants.DATA_PAYLOAD_LEN == 233


@pytest.mark.unit
def test_tunnel_sends_full_mtu_payload_without_extra_framing(
    iface_with_nodes: MeshInterface,
) -> None:
    """A maximum-MTU IP packet should be passed unchanged as Data.payload."""
    iface = iface_with_nodes
    iface.noProto = True
    iface.sendData = MagicMock()  # type: ignore[method-assign]
    payload = bytes(TUN_MTU)

    with _managed_tunnel(iface) as tun:
        tun._send_packet(b"\x00\x00\xff\xff", payload)

    iface.sendData.assert_called_once_with(
        payload,
        "^all",
        portnums_pb2.IP_TUNNEL_APP,
        wantAck=False,
    )


@pytest.mark.unit
def test_Tunnel_without_interface() -> None:
    """Test that we can not instantiate a Tunnel without a valid interface."""
    with pytest.raises(Tunnel.TunnelError) as pytest_wrapped_e:
        Tunnel(None)
    assert pytest_wrapped_e.type == Tunnel.TunnelError


@pytest.mark.unitslow
def test_Tunnel_with_interface(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
) -> None:
    """Test that Tunnel initializes with a valid interface and registers itself."""
    iface = iface_with_nodes
    assert iface.myInfo is not None
    iface.myInfo.my_node_num = 2475227164
    with caplog.at_level(logging.WARNING):
        with _managed_tunnel(iface) as tun:
            assert tun == mt_config.tunnel_instance
    assert re.search(r"Not creating a TUN device", caplog.text, re.MULTILINE)
    assert re.search(r"Not starting TUN reader", caplog.text, re.MULTILINE)


@pytest.mark.unit
def test_tunnel_creates_tun_device_when_proto_enabled(
    platform_socket_mocks: tuple[MagicMock, MagicMock],
    iface_with_nodes: MeshInterface,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tunnel should create and configure the TUN device when protocol handling is enabled."""
    mock_platform_system, _ = platform_socket_mocks
    mock_platform_system.return_value = "Linux"
    iface = iface_with_nodes
    assert iface.myInfo is not None
    iface.myInfo.my_node_num = 2475227164
    iface.noProto = False
    events: list[tuple[object, ...]] = []

    class _FakeTunDevice:
        def __init__(self, *, name: str) -> None:
            """Record TUN device construction."""
            events.append(("init", name))

        def up(self) -> None:
            """Record interface bring-up."""
            events.append(("up",))

        def ifconfig(self, *, address: str, netmask: str, mtu: int) -> None:
            """Record interface configuration arguments."""
            events.append(("ifconfig", address, netmask, mtu))

        def close(self) -> None:
            """Record device close calls."""
            events.append(("close",))

    class _FakeThread:
        def start(self) -> None:
            """Record thread start without launching target code."""
            events.append(("thread-start",))

        def join(self, timeout: float | None = None) -> None:
            """Accept join calls from tunnel shutdown."""
            _ = timeout

        def is_alive(self) -> bool:
            """Report the fake thread as already stopped."""
            return False

    monkeypatch.setattr("meshtastic.tunnel.LinuxTunDevice", _FakeTunDevice)
    monkeypatch.setattr(
        "meshtastic.tunnel.threading.Thread",
        lambda *_args, **_kwargs: _FakeThread(),
    )
    monkeypatch.setattr(
        "meshtastic.tunnel.pub.unsubscribe",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "meshtastic.tunnel.pub.subscribe", lambda *_args, **_kwargs: None
    )

    with _managed_tunnel(iface) as tun:
        assert tun.tun is not None

    assert ("init", "mesh") in events
    assert ("up",) in events
    assert ("ifconfig", "10.115.248.28", "255.255.0.0", TUN_MTU) in events
    assert ("thread-start",) in events
    assert ("close",) in events


@pytest.mark.unitslow
def test_onTunnelReceive_from_ourselves(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test onTunnelReceive."""
    iface = iface_with_nodes
    assert iface.myInfo is not None
    iface.myInfo.my_node_num = 2475227164
    monkeypatch.setattr(sys, "argv", [""])
    monkeypatch.setattr(mt_config, "args", argparse.Namespace())
    packet = {"decoded": {"payload": "foo"}, "from": 2475227164}
    with caplog.at_level(logging.DEBUG):
        with _managed_tunnel(iface):
            onTunnelReceive(packet, iface)
    assert re.search(r"in onTunnelReceive", caplog.text, re.MULTILINE)
    assert re.search(r"Ignoring message we sent", caplog.text, re.MULTILINE)


@pytest.mark.unit
def test_onTunnelReceive_from_someone_else(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test onTunnelReceive."""
    iface = iface_with_nodes
    assert iface.myInfo is not None
    iface.myInfo.my_node_num = 2475227164
    monkeypatch.setattr(sys, "argv", [""])
    monkeypatch.setattr(mt_config, "args", argparse.Namespace())
    packet = {"decoded": {"payload": "foo"}, "from": 123}
    with caplog.at_level(logging.DEBUG):
        with _managed_tunnel(iface):
            onTunnelReceive(packet, iface)
    assert re.search(r"in onTunnelReceive", caplog.text, re.MULTILINE)


@pytest.mark.unit
def test_onTunnelReceive_ignores_packet_when_myinfo_unavailable(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
) -> None:
    """Tunnel receive should safely ignore packets until iface.myInfo is available."""
    iface = iface_with_nodes
    packet = {"decoded": {"payload": b"abc"}, "from": 123}

    with caplog.at_level(logging.DEBUG):
        with _managed_tunnel(iface) as tun:
            iface.myInfo = None
            tun.onReceive(packet)

    assert "Ignoring tunnel packet because iface.myInfo is unavailable" in caplog.text


@pytest.mark.unit
def test_onTunnelReceive_ignores_invalid_decoded_or_payload(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
) -> None:
    """Tunnel receive should reject malformed decoded/payload packet shapes."""
    iface = iface_with_nodes
    assert iface.myInfo is not None
    iface.myInfo.my_node_num = 2475227164

    with caplog.at_level(logging.DEBUG):
        with _managed_tunnel(iface) as tun:
            tun.onReceive({"decoded": "not-a-dict", "from": 123})
            tun.onReceive({"decoded": {"payload": "not-bytes"}, "from": 123})

    assert "missing/invalid decoded field" in caplog.text
    assert "missing/invalid payload field" in caplog.text


@pytest.mark.unit
def test_onTunnelReceive_tun_write_oserror_is_swallowed(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
) -> None:
    """Tunnel receive should log and continue if TUN write fails during shutdown races."""
    iface = iface_with_nodes
    assert iface.myInfo is not None
    iface.myInfo.my_node_num = 2475227164

    with caplog.at_level(logging.DEBUG):
        with _managed_tunnel(iface) as tun:
            tun.iface.noProto = False
            tun.tun = MagicMock()
            tun.tun.write.side_effect = OSError("device closed")
            tun._should_filter_packet = MagicMock(return_value=False)  # type: ignore[method-assign]
            tun.onReceive({"decoded": {"payload": b"\x45" * 20}, "from": 123})

    assert "TUN write skipped: device closed during shutdown" in caplog.text


@pytest.mark.unitslow
def test_should_filter_packet_random(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
) -> None:
    """_should_filter_packet should allow unrecognized protocols by default."""
    iface = iface_with_nodes
    iface.noProto = True
    # random packet
    packet = b"1234567890123456789012345678901234567890"
    with caplog.at_level(logging.DEBUG):
        with _managed_tunnel(iface) as tun:
            ignore = tun._should_filter_packet(packet)
            assert not ignore


@pytest.mark.unit
def test_should_filter_packet_short_header(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
) -> None:
    """Packets shorter than an IPv4 header should be dropped safely."""
    iface = iface_with_nodes
    iface.noProto = True
    packet = b"\x00" * 10
    with caplog.at_level(logging.DEBUG):
        with _managed_tunnel(iface) as tun:
            ignore = tun._should_filter_packet(packet)
            assert ignore
    assert re.search(r"Ignoring short IP packet", caplog.text, re.MULTILINE)


@pytest.mark.unitslow
def test_should_filter_packet_in_blacklist(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
) -> None:
    """_should_filter_packet should drop packets with blacklisted protocols."""
    iface = iface_with_nodes
    iface.noProto = True
    # faked IGMP
    packet = b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    with caplog.at_level(logging.DEBUG):
        with _managed_tunnel(iface) as tun:
            ignore = tun._should_filter_packet(packet)
            assert ignore


@pytest.mark.unitslow
def test_should_filter_packet_icmp(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
) -> None:
    """_should_filter_packet should allow ICMP packets and log forwarding details."""
    iface = iface_with_nodes
    iface.noProto = True
    # faked ICMP
    packet = b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    with caplog.at_level(logging.DEBUG):
        with _managed_tunnel(iface) as tun:
            ignore = tun._should_filter_packet(packet)
            assert re.search(r"forwarding ICMP message", caplog.text, re.MULTILINE)
            assert not ignore


@pytest.mark.unit
def test_should_filter_packet_udp(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
) -> None:
    """_should_filter_packet should allow non-blacklisted UDP packets."""
    iface = iface_with_nodes
    iface.noProto = True
    # faked UDP
    packet = b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x11\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    with caplog.at_level(logging.DEBUG):
        with _managed_tunnel(iface) as tun:
            ignore = tun._should_filter_packet(packet)
            assert re.search(r"forwarding udp", caplog.text, re.MULTILINE)
            assert not ignore


@pytest.mark.unitslow
def test_should_filter_packet_udp_blacklisted(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
) -> None:
    """_should_filter_packet should drop UDP packets targeting blocked ports."""
    iface = iface_with_nodes
    iface.noProto = True
    # faked UDP
    packet = b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x11\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x07\x6c\x07\x6c\x00\x00\x00"
    with caplog.at_level(LOG_TRACE):
        with _managed_tunnel(iface) as tun:
            ignore = tun._should_filter_packet(packet)
            assert re.search(r"ignoring blacklisted UDP", caplog.text, re.MULTILINE)
            assert ignore


@pytest.mark.unit
def test_should_filter_packet_tcp(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
) -> None:
    """_should_filter_packet should allow non-blacklisted TCP packets."""
    iface = iface_with_nodes
    iface.noProto = True
    # faked TCP
    packet = b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x06\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    with caplog.at_level(logging.DEBUG):
        with _managed_tunnel(iface) as tun:
            ignore = tun._should_filter_packet(packet)
            assert re.search(r"forwarding tcp", caplog.text, re.MULTILINE)
            assert not ignore


@pytest.mark.unitslow
def test_should_filter_packet_tcp_blacklisted(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
) -> None:
    """_should_filter_packet should drop TCP packets targeting blocked ports."""
    iface = iface_with_nodes
    iface.noProto = True
    # faked TCP
    packet = b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x06\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x17\x0c\x17\x0c\x00\x00\x00"
    with caplog.at_level(LOG_TRACE):
        with _managed_tunnel(iface) as tun:
            ignore = tun._should_filter_packet(packet)
            assert re.search(r"ignoring blacklisted TCP", caplog.text, re.MULTILINE)
            assert ignore


@pytest.mark.unitslow
def test_ip_to_node_id_none(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
) -> None:
    """_ip_to_node_id should return None for all-zero addresses."""
    iface = iface_with_nodes
    iface.noProto = True
    with caplog.at_level(logging.DEBUG):
        with _managed_tunnel(iface) as tun:
            nodeid = tun._ip_to_node_id(b"\x00\x00\x00\x00")
            assert nodeid is None


@pytest.mark.unitslow
def test_ip_to_node_id_all(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
) -> None:
    """_ip_to_node_id should map broadcast-style suffixes to '^all'."""
    iface = iface_with_nodes
    iface.noProto = True
    with caplog.at_level(logging.DEBUG):
        with _managed_tunnel(iface) as tun:
            nodeid = tun._ip_to_node_id(b"\x00\x00\xff\xff")
            assert nodeid == "^all"


@pytest.mark.unit
def test_tun_reader_forwards_unfiltered_packets(
    iface_with_nodes: MeshInterface,
) -> None:
    """_tun_reader should forward packets when filtering says they are allowed."""
    iface = iface_with_nodes
    iface.noProto = True
    with _managed_tunnel(iface) as tun:
        packet = b"\x45\x00\x00\x28\x00\x00\x00\x00\x40\x11\x00\x00\x0a\x73\x01\x01\x0a\x73\x02\x02"
        tap = MagicMock()
        tap.read.return_value = packet
        tun.tun = tap
        tun._should_filter_packet = MagicMock(return_value=False)  # type: ignore[method-assign]

        def _capture_and_stop(dest_addr: bytes, payload: bytes) -> None:
            assert payload == packet
            assert dest_addr == packet[16:20]
            tun._stop_event.set()

        tun._send_packet = MagicMock(side_effect=_capture_and_stop)  # type: ignore[method-assign]
        tun._tun_reader()
        tun._send_packet.assert_called_once()


@pytest.mark.unit
def test_tun_reader_stops_on_read_failure(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
) -> None:
    """_tun_reader should log and stop when tap.read raises unexpectedly."""
    iface = iface_with_nodes
    iface.noProto = True
    with _managed_tunnel(iface) as tun:
        tap = MagicMock()
        tap.read.side_effect = OSError("read failed")
        tun.tun = tap
        with caplog.at_level(logging.ERROR):
            tun._tun_reader()
        assert "TUN reader terminating due to read failure" in caplog.text


@pytest.mark.unit
def test_tun_reader_exits_when_tun_is_unavailable(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
) -> None:
    """_tun_reader should exit immediately when no active TUN device is present."""
    iface = iface_with_nodes
    iface.noProto = True
    with _managed_tunnel(iface) as tun:
        tun.tun = None
        with caplog.at_level(logging.DEBUG):
            tun._tun_reader()
        assert "TUN reader exiting: no active TUN device" in caplog.text


@pytest.mark.unit
def test_tun_reader_stops_quietly_when_stop_requested_during_read_error(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
) -> None:
    """_tun_reader should break without error logging when stop was already requested."""
    iface = iface_with_nodes
    iface.noProto = True
    with _managed_tunnel(iface) as tun:
        tap = MagicMock()
        tap.read.side_effect = OSError("read failed while stopping")
        tun.tun = tap
        tun._stop_event.set()
        with caplog.at_level(logging.ERROR):
            tun._tun_reader()
        assert "TUN reader terminating due to read failure" not in caplog.text


@pytest.mark.unit
def test_ip_to_node_id_returns_none_when_nodes_unavailable(
    iface_with_nodes: MeshInterface,
) -> None:
    """_ip_to_node_id should return None when iface.nodes is missing/empty."""
    iface = iface_with_nodes
    iface.noProto = True
    iface.nodes = None
    with _managed_tunnel(iface) as tun:
        assert tun._ip_to_node_id(b"\x00\x00\x12\x34") is None


@pytest.mark.unit
def test_ip_to_node_id_short_destination_is_ignored(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
) -> None:
    """_ip_to_node_id should ignore short destination addresses."""
    iface = iface_with_nodes
    iface.noProto = True
    with _managed_tunnel(iface) as tun:
        with caplog.at_level(logging.DEBUG):
            assert tun._ip_to_node_id(b"\x00\x01\x02") is None
        assert "Ignoring short destination address" in caplog.text


@pytest.mark.unit
def test_ip_to_node_id_returns_matching_user_id(
    iface_with_nodes: MeshInterface,
) -> None:
    """_ip_to_node_id should return user.id for matching node-number low bits."""
    iface = iface_with_nodes
    iface.noProto = True
    iface.nodes = {
        "!candidate": {"num": 0xABCD1234, "user": {"id": "!candidate"}},
        "!ignored": {"num": 0xABCD1111, "user": {"id": "!ignored"}},
    }
    with _managed_tunnel(iface) as tun:
        assert tun._ip_to_node_id(b"\x00\x00\x12\x34") == "!candidate"


@pytest.mark.unit
def test_ip_to_node_id_skips_nodes_without_integer_num(
    iface_with_nodes: MeshInterface,
) -> None:
    """_ip_to_node_id should ignore node entries that do not expose integer num values."""
    iface = iface_with_nodes
    iface.noProto = True
    with _managed_tunnel(iface) as tun:
        iface.nodes = {"!bad": {"num": "not-an-int", "user": {"id": "!bad"}}}
        assert tun._ip_to_node_id(b"\x00\x00\x12\x34") is None


@pytest.mark.unit
def test_should_filter_packet_alias_delegates(
    iface_with_nodes: MeshInterface,
) -> None:
    """_shouldFilterPacket should delegate to _should_filter_packet."""
    iface = iface_with_nodes
    iface.noProto = True
    with _managed_tunnel(iface) as tun:
        tun._should_filter_packet = MagicMock(return_value=True)  # type: ignore[method-assign]
        assert tun._shouldFilterPacket(b"\x00" * 20) is True
        tun._should_filter_packet.assert_called_once()
