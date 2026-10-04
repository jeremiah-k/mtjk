"""Tests for USB-only firmware lockdown client helpers."""

import os
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pubsub import pub

import meshtastic.lockdown as lockdown_module
from meshtastic.lockdown import (
    LOCKDOWN_STATUS_TOPIC,
    build_lockdown_auth,
    read_lockdown_passphrase_file,
    send_lockdown_auth,
    validate_lockdown_passphrase,
)
from meshtastic.mesh_interface import MeshInterface
from meshtastic.protobuf import mesh_pb2, portnums_pb2
from meshtastic.serial_interface import SerialInterface
from meshtastic.tcp_interface import TCPInterface


@pytest.mark.unit
@pytest.mark.parametrize(
    ("startup", "command", "terminal"),
    [
        (
            mesh_pb2.LockdownStatus.STATE_UNSPECIFIED,
            "unlock",
            mesh_pb2.LockdownStatus.UNLOCKED,
        ),
        (
            mesh_pb2.LockdownStatus.NEEDS_PROVISION,
            "unlock",
            mesh_pb2.LockdownStatus.UNLOCKED,
        ),
        (mesh_pb2.LockdownStatus.LOCKED, "unlock", mesh_pb2.LockdownStatus.UNLOCKED),
        (mesh_pb2.LockdownStatus.DISABLED, "unlock", mesh_pb2.LockdownStatus.UNLOCKED),
        (mesh_pb2.LockdownStatus.LOCKED, "disable", mesh_pb2.LockdownStatus.DISABLED),
        (mesh_pb2.LockdownStatus.UNLOCKED, "lock-now", mesh_pb2.LockdownStatus.LOCKED),
    ],
)
def test_send_lockdown_auth_ignores_startup_status_before_command_result(
    startup: int, command: str, terminal: int
) -> None:
    """A queued capability notification cannot complete a fresh auth transaction."""
    serial = MagicMock(spec=SerialInterface)
    serial.myInfo = mesh_pb2.MyNodeInfo(my_node_num=123)
    final_status = mesh_pb2.LockdownStatus(state=terminal)  # type: ignore[arg-type]
    timer = threading.Timer(
        0.02,
        lambda: pub.sendMessage(
            LOCKDOWN_STATUS_TOPIC, interface=serial, status=final_status
        ),
    )

    def _send(*_args: object, **_kwargs: object) -> mesh_pb2.MeshPacket:
        pub.sendMessage(
            LOCKDOWN_STATUS_TOPIC,
            interface=serial,
            status=mesh_pb2.LockdownStatus(state=startup),  # type: ignore[arg-type]
        )
        timer.start()
        return mesh_pb2.MeshPacket(id=7)

    serial.sendData.side_effect = _send
    auth = build_lockdown_auth(
        b"secret", disable=command == "disable", lock_now=command == "lock-now"
    )
    try:
        status = send_lockdown_auth(serial, auth, timeout=1.0)
        assert status == final_status
    finally:
        timer.join(timeout=1.0)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("state", "reason"),
    [
        (mesh_pb2.LockdownStatus.UNLOCK_FAILED, ""),
        (mesh_pb2.LockdownStatus.LOCKED, "storage_corrupt"),
        (999, ""),
    ],
)
def test_send_lockdown_auth_retains_refusals_and_future_states(
    state: int, reason: str
) -> None:
    """Filtering boot notifications must retain terminal failures and unknown enums."""
    serial = MagicMock(spec=SerialInterface)
    serial.myInfo = mesh_pb2.MyNodeInfo(my_node_num=123)
    refusal = mesh_pb2.LockdownStatus(state=state, lock_reason=reason)  # type: ignore[arg-type]

    def _send(*_args: object, **_kwargs: object) -> mesh_pb2.MeshPacket:
        pub.sendMessage(LOCKDOWN_STATUS_TOPIC, interface=serial, status=refusal)
        return mesh_pb2.MeshPacket(id=7)

    serial.sendData.side_effect = _send
    assert (
        send_lockdown_auth(serial, build_lockdown_auth(b"secret"), timeout=0.5)
        == refusal
    )


@pytest.mark.unit
def test_validate_lockdown_passphrase_enforces_wire_bounds() -> None:
    assert validate_lockdown_passphrase(b"secret") == b"secret"
    with pytest.raises(ValueError, match="1..32"):
        validate_lockdown_passphrase(b"")
    with pytest.raises(ValueError, match="1..32"):
        validate_lockdown_passphrase(b"x" * 33)


@pytest.mark.unit
def test_read_lockdown_passphrase_file_requires_operator_only_permissions(
    tmp_path: Path,
) -> None:
    path = tmp_path / "passphrase"
    path.write_bytes(b"secret\n")
    path.chmod(0o600)
    assert read_lockdown_passphrase_file(path) == b"secret"
    if os.name == "nt":
        return
    path.chmod(0o640)
    with pytest.raises(PermissionError, match="operator-only"):
        read_lockdown_passphrase_file(path)


@pytest.mark.unit
def test_read_lockdown_passphrase_file_strips_only_one_line_ending(
    tmp_path: Path,
) -> None:
    path = tmp_path / "passphrase-multiline"
    path.write_bytes(b"secret\n\n")
    path.chmod(0o600)
    assert read_lockdown_passphrase_file(path) == b"secret\n"


@pytest.mark.unit
def test_build_lockdown_auth_rejects_out_of_range_limits() -> None:
    auth = build_lockdown_auth(
        b"secret", boots_remaining=3, max_session_seconds=90, disable=True
    )
    assert auth.passphrase == b"secret"
    assert auth.boots_remaining == 3
    assert auth.max_session_seconds == 90
    assert auth.disable is True
    with pytest.raises(ValueError, match="0 and 255"):
        build_lockdown_auth(b"secret", boots_remaining=256)


@pytest.mark.unit
def test_send_lockdown_auth_is_serial_only_and_uses_plain_local_admin() -> None:
    tcp = MagicMock(spec=TCPInterface)
    with pytest.raises(ValueError, match="USB-serial only"):
        send_lockdown_auth(tcp, build_lockdown_auth(b"secret"), timeout=0.1)

    serial = MagicMock(spec=SerialInterface)
    serial.myInfo = mesh_pb2.MyNodeInfo(my_node_num=123)

    def _send(*_args: object, **kwargs: object) -> mesh_pb2.MeshPacket:
        assert kwargs["portNum"] == portnums_pb2.PortNum.ADMIN_APP
        assert kwargs["wantAck"] is True
        assert kwargs["pkiEncrypted"] is False
        pub.sendMessage(
            LOCKDOWN_STATUS_TOPIC,
            interface=serial,
            status=mesh_pb2.LockdownStatus(state=mesh_pb2.LockdownStatus.UNLOCKED),
        )
        return mesh_pb2.MeshPacket(id=7)

    serial.sendData.side_effect = _send
    status = send_lockdown_auth(serial, build_lockdown_auth(b"secret"), timeout=0.5)
    assert status is not None
    assert status.state == mesh_pb2.LockdownStatus.UNLOCKED


@pytest.mark.unit
def test_send_lockdown_auth_status_listener_tolerates_extra_keywords(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serial = MagicMock(spec=SerialInterface)
    serial.myInfo = mesh_pb2.MyNodeInfo(my_node_num=123)
    listeners: list[object] = []

    def _subscribe(listener: object, topic: str) -> None:
        assert topic == LOCKDOWN_STATUS_TOPIC
        listeners.append(listener)

    def _unsubscribe(listener: object, topic: str) -> None:
        assert topic == LOCKDOWN_STATUS_TOPIC
        assert listener in listeners

    monkeypatch.setattr(pub, "subscribe", _subscribe)
    monkeypatch.setattr(pub, "unsubscribe", _unsubscribe)

    def _send(*_args: object, **_kwargs: object) -> mesh_pb2.MeshPacket:
        listener = listeners[-1]
        assert callable(listener)
        listener(
            interface=serial,
            status=mesh_pb2.LockdownStatus(state=mesh_pb2.LockdownStatus.UNLOCKED),
            source="future-publisher",
        )
        return mesh_pb2.MeshPacket(id=8)

    serial.sendData.side_effect = _send
    status = send_lockdown_auth(serial, build_lockdown_auth(b"secret"), timeout=0.5)
    assert status is not None
    assert status.state == mesh_pb2.LockdownStatus.UNLOCKED


@pytest.mark.unit
def test_send_lockdown_auth_times_out_without_status() -> None:
    serial = MagicMock(spec=SerialInterface)
    serial.myInfo = mesh_pb2.MyNodeInfo(my_node_num=123)
    serial.sendData.return_value = mesh_pb2.MeshPacket(id=7)
    with pytest.raises(TimeoutError, match="LockdownStatus"):
        send_lockdown_auth(serial, build_lockdown_auth(b"secret"), timeout=0.01)


@pytest.mark.unit
def test_mesh_interface_initializes_lockdown_status() -> None:
    with MeshInterface(noProto=True) as interface:
        assert interface.lockdownStatus is None


@pytest.mark.unit
def test_read_lockdown_passphrase_file_strips_crlf(
    tmp_path: Path,
) -> None:
    path = tmp_path / "passphrase-crlf"
    path.write_bytes(b"secret\r\n")
    path.chmod(0o600)
    assert read_lockdown_passphrase_file(path) == b"secret"


@pytest.mark.unit
def test_read_lockdown_passphrase_file_skips_posix_mode_check_on_windows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "passphrase-windows"
    path.write_bytes(b"secret\n")
    path.chmod(0o666)
    monkeypatch.setattr(lockdown_module, "_HAS_POSIX_FILE_PERMISSIONS", False)
    assert read_lockdown_passphrase_file(path) == b"secret"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("kwargs", "message"),
    (
        ({"valid_until_epoch": -1}, "must not be negative"),
        ({"max_session_seconds": -1}, "must not be negative"),
        ({"boots_remaining": -1}, "between 0 and 255"),
    ),
)
def test_build_lockdown_auth_rejects_all_invalid_limits(
    kwargs: dict[str, int], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        build_lockdown_auth(b"secret", **kwargs)  # type: ignore[arg-type]


@pytest.mark.unit
def test_build_lockdown_auth_supports_lock_now_without_passphrase() -> None:
    auth = build_lockdown_auth(lock_now=True)
    assert auth.passphrase == b""
    assert auth.lock_now is True


@pytest.mark.unit
def test_send_lockdown_auth_validates_timeout_and_my_info() -> None:
    serial = MagicMock(spec=SerialInterface)
    serial.myInfo = mesh_pb2.MyNodeInfo(my_node_num=123)
    with pytest.raises(ValueError, match="timeout must be positive"):
        send_lockdown_auth(serial, build_lockdown_auth(b"secret"), timeout=0)

    serial.myInfo = None
    with pytest.raises(RuntimeError, match="my_info"):
        send_lockdown_auth(serial, build_lockdown_auth(b"secret"), timeout=0.1)


@pytest.mark.unit
def test_send_lockdown_auth_ignores_other_interface_status_and_allows_reboot() -> None:
    serial = MagicMock(spec=SerialInterface)
    serial.myInfo = mesh_pb2.MyNodeInfo(my_node_num=123)
    other = MagicMock(spec=SerialInterface)

    def _send(*_args: object, **_kwargs: object) -> mesh_pb2.MeshPacket:
        pub.sendMessage(
            LOCKDOWN_STATUS_TOPIC,
            interface=other,
            status=mesh_pb2.LockdownStatus(state=mesh_pb2.LockdownStatus.UNLOCKED),
        )
        return mesh_pb2.MeshPacket(id=9)

    serial.sendData.side_effect = _send
    assert (
        send_lockdown_auth(
            serial,
            build_lockdown_auth(lock_now=True),
            timeout=0.01,
            allow_reboot_without_status=True,
        )
        is None
    )


@pytest.mark.unit
def test_send_lockdown_auth_returns_defensive_status_copy_and_unsubscribes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serial = MagicMock(spec=SerialInterface)
    serial.myInfo = mesh_pb2.MyNodeInfo(my_node_num=123)
    listeners: list[object] = []
    unsubscribed: list[object] = []

    monkeypatch.setattr(
        pub, "subscribe", lambda listener, _topic: listeners.append(listener)
    )
    monkeypatch.setattr(
        pub, "unsubscribe", lambda listener, _topic: unsubscribed.append(listener)
    )

    source = mesh_pb2.LockdownStatus(state=mesh_pb2.LockdownStatus.UNLOCKED)

    def _send(
        payload: object, destination: int, **kwargs: object
    ) -> mesh_pb2.MeshPacket:
        assert destination == 123
        assert kwargs == {
            "portNum": portnums_pb2.PortNum.ADMIN_APP,
            "wantAck": True,
            "channelIndex": 0,
            "pkiEncrypted": False,
            "priority": mesh_pb2.MeshPacket.Priority.RELIABLE,
        }
        assert payload.lockdown_auth.passphrase == b"secret"  # type: ignore[attr-defined]
        listener = listeners[-1]
        assert callable(listener)
        listener(interface=serial, status=source)
        source.state = mesh_pb2.LockdownStatus.UNLOCK_FAILED
        return mesh_pb2.MeshPacket(id=10)

    serial.sendData.side_effect = _send
    status = send_lockdown_auth(serial, build_lockdown_auth(b"secret"), timeout=0.5)

    assert status is not None
    assert status is not source
    assert status.state == mesh_pb2.LockdownStatus.UNLOCKED
    assert unsubscribed == listeners


@pytest.mark.unit
def test_send_lockdown_auth_unsubscribes_when_send_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serial = MagicMock(spec=SerialInterface)
    serial.myInfo = mesh_pb2.MyNodeInfo(my_node_num=123)
    listener: object | None = None
    unsubscribed: list[object] = []

    def _subscribe(candidate: object, _topic: str) -> None:
        nonlocal listener
        listener = candidate

    monkeypatch.setattr(pub, "subscribe", _subscribe)
    monkeypatch.setattr(
        pub, "unsubscribe", lambda candidate, _topic: unsubscribed.append(candidate)
    )
    serial.sendData.side_effect = OSError("USB disconnected")

    with pytest.raises(OSError, match="USB disconnected"):
        send_lockdown_auth(serial, build_lockdown_auth(b"secret"), timeout=0.5)

    assert listener is not None
    assert unsubscribed == [listener]


@pytest.mark.unit
def test_read_lockdown_passphrase_file_preserves_value_without_line_ending(
    tmp_path: Path,
) -> None:
    path = tmp_path / "passphrase-no-newline"
    path.write_bytes(b"secret")
    path.chmod(0o600)
    assert read_lockdown_passphrase_file(path) == b"secret"
