"""Edge coverage tests for tunnel initialization branches.

This module intentionally lives under ``tests/`` (not ``meshtastic/tests``)
to mirror the historical split of tunnel coverage; it verifies Tunnel wiring
against a fake LinuxTunDevice when protocol handling is enabled.
"""

from types import SimpleNamespace

import pytest

from meshtastic import mt_config
from meshtastic import tunnel as tunnel_module


@pytest.mark.unit
def test_tunnel_initialization_creates_tun_device_when_proto_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tunnel should create/configure the TUN device when noProto is disabled."""
    tun_events: list[tuple[object, ...]] = []

    class _FakeTunDevice:
        def __init__(self, *, name: str) -> None:
            tun_events.append(("init", name))

        def up(self) -> None:
            tun_events.append(("up",))

        def ifconfig(self, *, address: str, netmask: str, mtu: int) -> None:
            tun_events.append(("ifconfig", address, netmask, mtu))

        def close(self) -> None:
            tun_events.append(("close",))

    class _FakeThread:
        def start(self) -> None:
            tun_events.append(("thread-start",))

        def join(self, timeout: float | None = None) -> None:
            _ = timeout

        def is_alive(self) -> bool:
            return False

    monkeypatch.setattr(tunnel_module, "LinuxTunDevice", _FakeTunDevice)
    monkeypatch.setattr(tunnel_module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        tunnel_module.threading,
        "Thread",
        lambda *_args, **_kwargs: _FakeThread(),
    )
    monkeypatch.setattr(tunnel_module.pub, "subscribe", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        tunnel_module.pub,
        "unsubscribe",
        lambda *_args, **_kwargs: None,
    )

    iface = SimpleNamespace(
        myInfo=SimpleNamespace(my_node_num=2475227164),
        nodes={},
        noProto=False,
        sendData=lambda *_args, **_kwargs: None,
    )

    tunnel = None
    try:
        tunnel = tunnel_module.Tunnel(iface)
        assert ("init", "mesh") in tun_events
        assert ("up",) in tun_events
        assert (
            "ifconfig",
            "10.115.248.28",
            "255.255.0.0",
            tunnel_module.TUN_MTU,
        ) in tun_events
        assert ("thread-start",) in tun_events
    finally:
        if tunnel is not None:
            tunnel.close()
        mt_config.reset()
