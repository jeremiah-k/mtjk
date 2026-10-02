"""Consumer-seam regression tests for local ``--set`` apply verification.

These tests drive the real ``_handle_set_command`` through the real ``main()``
entry point against a protobuf-backed node double. The transport boundary is
simulated, never the verification outcome: the double's ``_send_admin`` parses
the verification refresh's ``get_config_request``/``get_module_config_request``
AdminMessage and applies the matching section payload from a separate
device-truth protobuf (modeling what firmware plus the typed response handler
do on a real device). A dropped write therefore leaves device truth at the old
value while the staged CLI cache keeps the requested one.
"""

# pylint: disable=W0613

from __future__ import annotations

import sys
import threading
import time as time_module
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, create_autospec, patch

import pytest

import meshtastic.__main__ as main_module
from meshtastic import mt_config
from meshtastic.__main__ import main
from meshtastic.cli import configure_actions as cli_configure_actions
from meshtastic.node import Node
from meshtastic.protobuf import admin_pb2, config_pb2, localonly_pb2
from meshtastic.serial_interface import SerialInterface
from meshtastic.util import Timeout


def _patch_seam_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Advance the verification seam's clock so bounded waits cannot stall.

    Each ``monotonic`` call advances 5s while ``sleep`` is a no-op, so a
    never-repopulating section exhausts the shared budget after a few loop
    iterations instead of sleeping in real time.
    """
    clock = {"now": 100.0}

    def _fast_monotonic() -> float:
        clock["now"] += 5.0
        return clock["now"]

    monkeypatch.setattr(
        "meshtastic.configure_verify.time",
        SimpleNamespace(
            monotonic=_fast_monotonic,
            sleep=lambda _seconds: None,
            time=time_module.time,
        ),
        raising=True,
    )


def _refresh_from_device(
    staged: Any, device: Any, section: str, *, deliver: bool
) -> None:
    """Clear one staged section and repopulate it from device truth."""
    staged.ClearField(section)
    if deliver and device.HasField(section):
        getattr(staged, section).CopyFrom(getattr(device, section))


def _build_local_set_interface(
    *,
    device_local: localonly_pb2.LocalConfig | None = None,
    device_module: localonly_pb2.LocalModuleConfig | None = None,
    drop_writes: bool = False,
    deliver_on_request: bool = True,
    no_proto: bool = False,
    delivery_delay: float | None = None,
    reboot_after_write: bool = False,
) -> tuple[MagicMock, MagicMock, list[str]]:
    """Build a protobuf-backed local node interface double with device truth.

    Parameters
    ----------
    device_local : localonly_pb2.LocalConfig | None
        Device truth for ``LocalConfig`` sections; the staged CLI cache starts
        as a copy of it.
    device_module : localonly_pb2.LocalModuleConfig | None
        Device truth for ``LocalModuleConfig`` sections.
    drop_writes : bool
        When true, ``writeConfig`` silently drops the write, leaving device
        truth at its old value while the staged cache keeps the requested one.
    deliver_on_request : bool
        When false, the refresh boundary clears the staged section without
        ever repopulating it, modeling a device that never answers.
    no_proto : bool
        Modeled ``noProto`` flag on the node double.
    delivery_delay : float | None
        When set, the refresh answer is applied after this many seconds on a
        timer thread instead of synchronously, exercising the seam's deadline
        polling loop against a late reply.
    reboot_after_write : bool
        When true, the write drops the interface link and advances the config
        generation, modeling a reboot. Tests restore the link from an explicit
        reconnect hook rather than wall-clock timing.

    Returns
    -------
    tuple[MagicMock, MagicMock, list[str]]
        Interface double, node double, and the ordered node-call log.
    """
    device_local = device_local or localonly_pb2.LocalConfig()
    device_module = device_module or localonly_pb2.LocalModuleConfig()
    staged_local = localonly_pb2.LocalConfig()
    staged_local.CopyFrom(device_local)
    staged_module = localonly_pb2.LocalModuleConfig()
    staged_module.CopyFrom(device_module)

    calls: list[str] = []
    iface = create_autospec(SerialInterface, instance=True)
    node = create_autospec(Node, instance=True)
    node.iface = iface
    node.noProto = no_proto
    node.localConfig = staged_local
    node.moduleConfig = staged_module
    node._timeout = Timeout(maxSecs=30.0)
    node.beginSettingsTransaction = MagicMock(
        side_effect=lambda: calls.append("beginSettingsTransaction")
    )
    node.commitSettingsTransaction = MagicMock(
        side_effect=lambda: calls.append("commitSettingsTransaction")
    )
    node.requestChannels = MagicMock()

    def _write_config(config_name: str) -> None:
        """Apply one staged write to device truth and optionally model a reboot."""
        calls.append(f"writeConfig:{config_name}")
        if reboot_after_write:
            iface.isConnected.clear()
            iface.configId += 1
        if drop_writes:
            return
        if config_name in staged_local.DESCRIPTOR.fields_by_name:
            _refresh_from_device(device_local, staged_local, config_name, deliver=True)
        elif config_name in staged_module.DESCRIPTOR.fields_by_name:
            _refresh_from_device(
                device_module, staged_module, config_name, deliver=True
            )

    node.writeConfig = MagicMock(side_effect=_write_config)

    def _apply_refresh(section: str, staged: Any, device: Any, callback: Any) -> None:
        _refresh_from_device(staged, device, section, deliver=deliver_on_request)
        if deliver_on_request and device.HasField(section):
            raw = admin_pb2.AdminMessage()
            variant = (
                "get_config_response"
                if staged is staged_local
                else "get_module_config_response"
            )
            getattr(getattr(raw, variant), section).CopyFrom(getattr(device, section))
            callback({"decoded": {"admin": {"raw": raw}}})

    def _device_send_admin(message: Any, **kwargs: Any) -> None:
        """Model the device answering a settings readback from device truth."""
        variant = message.WhichOneof("payload_variant")
        staged: Any
        device: Any
        if variant == "get_config_request":
            config_name = admin_pb2.AdminMessage.ConfigType.Name(
                message.get_config_request
            )
            section = config_name.removesuffix("_CONFIG").lower()
            staged, device = staged_local, device_local
        elif variant == "get_module_config_request":
            section = staged_module.DESCRIPTOR.fields[
                message.get_module_config_request
            ].name
            staged, device = staged_module, device_module
        else:
            return
        calls.append(f"readback:{section}")
        if delivery_delay is None:
            _apply_refresh(section, staged, device, kwargs["onResponse"])
            return
        timer = threading.Timer(
            delivery_delay,
            _apply_refresh,
            args=(section, staged, device, kwargs["onResponse"]),
        )
        timer.daemon = True
        timer.start()

    node._send_admin = MagicMock(side_effect=_device_send_admin)

    def _request_config(config_type: Any, *_args: Any) -> None:
        section = getattr(config_type, "name", "")
        containing = getattr(getattr(config_type, "containing_type", None), "name", "")
        calls.append(f"requestConfig:{section}")
        # The public requestConfig path models the correlated full-config
        # delivery that follows a reconnect, so it always applies device
        # truth regardless of the live refresh boundary's answer policy.
        if containing == "LocalConfig":
            _refresh_from_device(staged_local, device_local, section, deliver=True)
        elif containing == "LocalModuleConfig":
            _refresh_from_device(staged_module, device_module, section, deliver=True)

    node.requestConfig = MagicMock(side_effect=_request_config)

    def _repopulate_after_config_reload() -> None:
        """Model the post-reboot full-config delivery repopulating the cache."""
        for staged, device in (
            (staged_local, device_local),
            (staged_module, device_module),
        ):
            for field in device.DESCRIPTOR.fields:
                if field.message_type is None or not device.HasField(field.name):
                    continue
                _refresh_from_device(staged, device, field.name, deliver=True)

    if reboot_after_write:
        iface.waitForConfig = MagicMock(side_effect=_repopulate_after_config_reload)

    iface.devPath = "/dev/mock"
    iface.configId = 1
    iface.isConnected = threading.Event()
    iface.isConnected.set()
    iface.noProto = no_proto
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = node
    iface.localNode = node
    return iface, node, calls


def _run_main_set(
    argv: list[str],
    iface: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    *,
    patch_seam_clock: bool = True,
    record: list[str] | None = None,
) -> None:
    """Run ``main()`` with the supplied argv against the supplied interface."""
    if patch_seam_clock:
        _patch_seam_clock(monkeypatch)
    time_attrs = vars(main_module.time).copy()

    def _record_sleep(seconds: float) -> None:
        if record is not None:
            record.append(f"sleep:{seconds:g}")

    time_attrs["sleep"] = _record_sleep
    with monkeypatch.context() as apply_patch:
        apply_patch.setattr(sys, "argv", ["", *argv])
        apply_patch.setattr(mt_config, "args", cast(Any, ["", *argv]))
        apply_patch.setattr(
            main_module, "time", SimpleNamespace(**time_attrs), raising=True
        )
        with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
            main()


@pytest.fixture(autouse=True)
def _mock_newer_version_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent external network calls during unit tests in this module."""
    monkeypatch.setattr("meshtastic.util.check_if_newer_version", lambda: None)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_local_set_success_verifies_after_final_write(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A verified local --set proves fresh device state after the final write."""
    device_local = localonly_pb2.LocalConfig()
    device_local.power.ls_secs = 100
    iface, _node, calls = _build_local_set_interface(device_local=device_local)

    _run_main_set(["--set", "power.ls_secs", "222"], iface, monkeypatch)

    out, _err = capsys.readouterr()
    assert calls == ["writeConfig:power", "readback:power"]
    assert "Verified: fresh device state matches the requested --set values." in out


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_local_set_multi_section_verifies_once_after_commit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A multi-section --set verifies once, after commit and settle."""
    device_local = localonly_pb2.LocalConfig()
    device_local.power.ls_secs = 100
    device_local.lora.hop_limit = 3
    iface, _node, calls = _build_local_set_interface(device_local=device_local)

    _run_main_set(
        ["--set", "power.ls_secs", "222", "--set", "lora.hop_limit", "5"],
        iface,
        monkeypatch,
        record=calls,
    )

    out, _err = capsys.readouterr()
    writes = [
        index for index, call in enumerate(calls) if call.startswith("writeConfig:")
    ]
    readbacks = [
        index for index, call in enumerate(calls) if call.startswith("readback:")
    ]
    assert sorted(calls[index] for index in writes) == [
        "writeConfig:lora",
        "writeConfig:power",
    ]
    # One fresh device readback per requested section, all after the commit
    # and its settle delay.
    assert sorted(calls[index] for index in readbacks) == [
        "readback:lora",
        "readback:power",
    ]
    assert calls.count("beginSettingsTransaction") == 1
    assert calls.count("commitSettingsTransaction") == 1
    assert calls.count(f"sleep:{main_module.CONFIG_COMMIT_SETTLE_SECONDS:g}") == 1
    assert calls.index("beginSettingsTransaction") < min(writes)
    assert max(writes) < calls.index("commitSettingsTransaction")
    assert calls.index("commitSettingsTransaction") < calls.index(
        f"sleep:{main_module.CONFIG_COMMIT_SETTLE_SECONDS:g}"
    )
    assert calls.index(f"sleep:{main_module.CONFIG_COMMIT_SETTLE_SECONDS:g}") < min(
        readbacks
    )
    assert min(readbacks) > calls.index("commitSettingsTransaction")
    assert len(readbacks) == 2
    assert "Verified: fresh device state matches the requested --set values." in out


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_local_set_dropped_write_exits_nonzero_naming_field(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A silently dropped local write must fail naming the field, not succeed.

    The staged cache holds the requested value, but fresh device truth still
    reports the old value; the verification seam observes the fresh old value
    through the device-answering refresh boundary.
    """
    device_local = localonly_pb2.LocalConfig()
    device_local.power.ls_secs = 100
    iface, _node, calls = _build_local_set_interface(
        device_local=device_local, drop_writes=True
    )

    with pytest.raises(SystemExit) as exit_info:
        _run_main_set(["--set", "power.ls_secs", "222"], iface, monkeypatch)

    out, err = capsys.readouterr()
    captured = out + err
    assert exit_info.value.code == 1
    assert calls == ["writeConfig:power", "readback:power"]
    assert "power.ls_secs" in captured
    assert "different values" in captured
    assert "Verified:" not in captured


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_local_set_reload_failed_exits_nonzero_naming_section_and_budget(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A section that never repopulates fails naming the section and budget."""
    device_local = localonly_pb2.LocalConfig()
    device_local.power.ls_secs = 100
    iface, _node, calls = _build_local_set_interface(
        device_local=device_local, drop_writes=True, deliver_on_request=False
    )

    with pytest.raises(SystemExit) as exit_info:
        _run_main_set(["--set", "power.ls_secs", "222"], iface, monkeypatch)

    out, err = capsys.readouterr()
    captured = out + err
    assert exit_info.value.code == 1
    assert calls == ["writeConfig:power", "readback:power"]
    assert "power" in captured
    assert f"{main_module.LOCAL_SET_APPLY_VERIFY_SECONDS:g}" in captured
    assert "could not be verified" in captured
    assert "Verified:" not in captured


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_local_set_noproto_skips_verification(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """NoProto nodes skip verification: no reads, no verified claims, exit 0."""
    device_local = localonly_pb2.LocalConfig()
    device_local.power.ls_secs = 100
    iface, _node, calls = _build_local_set_interface(
        device_local=device_local, drop_writes=True, no_proto=True
    )

    _run_main_set(["--set", "power.ls_secs", "222"], iface, monkeypatch)

    out, err = capsys.readouterr()
    captured = out + err
    assert not any(call.startswith("readback:") for call in calls)
    assert "writeConfig:power" in calls
    assert "Verified:" not in captured


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_remote_set_skips_verification(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Remote-target --set keeps historical behavior: no verification at all."""
    device_local = localonly_pb2.LocalConfig()
    device_local.power.ls_secs = 100
    iface, remote_node, calls = _build_local_set_interface(
        device_local=device_local, drop_writes=True
    )
    local_node = create_autospec(Node, instance=True)
    local_node.localConfig = localonly_pb2.LocalConfig()
    local_node.moduleConfig = localonly_pb2.LocalModuleConfig()
    iface.localNode = local_node

    _run_main_set(
        ["--set", "power.ls_secs", "222", "--dest", "!98765432"], iface, monkeypatch
    )

    out, err = capsys.readouterr()
    captured = out + err
    assert "writeConfig:power" in calls
    assert not any(call.startswith("readback:") for call in calls)
    remote_node.requestConfig.assert_not_called()
    remote_node._send_admin.assert_not_called()
    local_node.requestConfig.assert_not_called()
    local_node._send_admin.assert_not_called()
    assert "Verified:" not in captured


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_dry_run_set_issues_no_writes_or_verification(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--dry-run alone performs no writes and never starts verification."""
    device_local = localonly_pb2.LocalConfig()
    device_local.power.ls_secs = 100
    iface, node, calls = _build_local_set_interface(device_local=device_local)

    _run_main_set(["--set", "power.ls_secs", "222", "--dry-run"], iface, monkeypatch)

    out, err = capsys.readouterr()
    captured = out + err
    assert calls == []
    node.writeConfig.assert_not_called()
    node.requestConfig.assert_not_called()
    node._send_admin.assert_not_called()
    node.beginSettingsTransaction.assert_not_called()
    assert "Writing modified preferences to device" not in captured
    assert "Verified:" not in captured


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_set_preflight_failure_performs_zero_writes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A rejected --set value aborts before any device write or read."""
    device_local = localonly_pb2.LocalConfig()
    device_local.power.ls_secs = 100
    iface, node, calls = _build_local_set_interface(device_local=device_local)

    with pytest.raises(SystemExit) as exit_info:
        _run_main_set(["--set", "power.ls_secs", "notanint"], iface, monkeypatch)

    assert exit_info.value.code == 1
    assert calls == []
    node.writeConfig.assert_not_called()
    node.requestConfig.assert_not_called()
    node._send_admin.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_local_set_false_default_value_verifies(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A requested false value verifies: 0/false is real applied state."""
    device_local = localonly_pb2.LocalConfig()
    device_local.bluetooth.enabled = True
    iface, _node, _calls = _build_local_set_interface(device_local=device_local)

    _run_main_set(["--set", "bluetooth.enabled", "false"], iface, monkeypatch)

    out, _err = capsys.readouterr()
    assert "Verified: fresh device state matches the requested --set values." in out


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_local_set_nested_repeated_and_enum_values_verify(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Nested, repeated, and enum requested values freeze and verify intact."""
    device_local = localonly_pb2.LocalConfig()
    device_local.network.ipv4_config.ip = 1
    device_local.lora.ignore_incoming.extend([1])
    device_local.bluetooth.mode = (
        config_pb2.Config.BluetoothConfig.PairingMode.FIXED_PIN
    )
    iface, _node, calls = _build_local_set_interface(device_local=device_local)

    _run_main_set(
        [
            "--set",
            "network.ipv4_config.ip",
            "2",
            "--set",
            "lora.ignore_incoming",
            "8675309",
            "--set",
            "bluetooth.mode",
            "RANDOM_PIN",
        ],
        iface,
        monkeypatch,
    )

    out, _err = capsys.readouterr()
    writes = [
        index for index, call in enumerate(calls) if call.startswith("writeConfig:")
    ]
    readbacks = [
        index for index, call in enumerate(calls) if call.startswith("readback:")
    ]
    assert sorted(calls[index] for index in writes) == [
        "writeConfig:bluetooth",
        "writeConfig:lora",
        "writeConfig:network",
    ]
    assert len(readbacks) == 3
    assert calls.count("commitSettingsTransaction") == 1
    assert max(writes) < calls.index("commitSettingsTransaction")
    assert min(readbacks) > calls.index("commitSettingsTransaction")
    assert "Verified: fresh device state matches the requested --set values." in out


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_local_set_delayed_refresh_still_verifies(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A late-but-bounded device answer is caught by the deadline poll loop.

    The refresh answer is applied on a timer thread 50ms after the send; the
    seam's real (unpatched) monotonic deadline loop must observe the section
    repopulating and still report a verified apply.
    """
    device_local = localonly_pb2.LocalConfig()
    device_local.power.ls_secs = 100
    iface, _node, calls = _build_local_set_interface(
        device_local=device_local, delivery_delay=0.05
    )

    _run_main_set(
        ["--set", "power.ls_secs", "222"],
        iface,
        monkeypatch,
        patch_seam_clock=False,
    )

    out, _err = capsys.readouterr()
    assert calls == ["writeConfig:power", "readback:power"]
    assert "Verified: fresh device state matches the requested --set values." in out


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_local_set_reboot_during_verify_recovers_verified(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A commit-triggered reboot that re-answers still exits 0 verified.

    The write succeeds and the device reboots (link drops), so the primary
    readback budget expires with RELOAD_FAILED; the reconnect fallback then
    reloads fresh state (222) and verifies, mirroring the ``--configure``
    reboot tolerance.
    """
    device_local = localonly_pb2.LocalConfig()
    device_local.power.ls_secs = 100
    iface, _node, calls = _build_local_set_interface(
        device_local=device_local,
        deliver_on_request=False,
        reboot_after_write=True,
    )
    monkeypatch.setattr(cli_configure_actions, "CONFIG_RECONNECT_WAIT_SECONDS", 0.5)
    monkeypatch.setattr(cli_configure_actions, "CONFIG_REBOOT_PROBE_SECONDS", 0.0)
    real_reconnect_verify = main_module._post_configure_reconnect_and_verify

    def _restore_link_at_reconnect_check(*args: Any, **kwargs: Any) -> Any:
        """Restore the modeled link only once the reconnect fallback owns it."""
        iface.isConnected.set()
        return real_reconnect_verify(*args, **kwargs)

    monkeypatch.setattr(
        main_module,
        "_post_configure_reconnect_and_verify",
        _restore_link_at_reconnect_check,
    )

    _run_main_set(["--set", "power.ls_secs", "222"], iface, monkeypatch)

    out, err = capsys.readouterr()
    captured = out + err
    assert "readback:power" in calls
    assert "Verified: fresh device state matches the requested --set values." in out
    assert "did not reconnect" not in captured


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_local_set_fast_reboot_generation_change_recovers_verified(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A reboot that reconnects before the readback expires still re-verifies."""
    device_local = localonly_pb2.LocalConfig()
    device_local.power.ls_secs = 100
    iface, node, calls = _build_local_set_interface(
        device_local=device_local,
        deliver_on_request=False,
        reboot_after_write=True,
    )
    original_write = node.writeConfig.side_effect

    def _write_and_reconnect(config_name: str) -> None:
        """Complete the modeled reboot before local verification starts."""
        original_write(config_name)
        iface.isConnected.set()

    node.writeConfig.side_effect = _write_and_reconnect
    monkeypatch.setattr(cli_configure_actions, "CONFIG_RECONNECT_WAIT_SECONDS", 0.5)
    monkeypatch.setattr(cli_configure_actions, "CONFIG_REBOOT_PROBE_SECONDS", 0.0)

    _run_main_set(["--set", "power.ls_secs", "222"], iface, monkeypatch)

    out, err = capsys.readouterr()
    captured = out + err
    assert "readback:power" in calls
    assert iface.configId == 2
    assert "Verified: fresh device state matches the requested --set values." in out
    assert "did not return the power configuration section" not in captured


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_local_set_reboot_never_returns_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A reboot whose link never returns exits nonzero naming the failure."""
    device_local = localonly_pb2.LocalConfig()
    device_local.power.ls_secs = 100
    iface, _node, calls = _build_local_set_interface(
        device_local=device_local,
        deliver_on_request=False,
        reboot_after_write=True,
    )
    monkeypatch.setattr(cli_configure_actions, "CONFIG_RECONNECT_WAIT_SECONDS", 0.2)

    with pytest.raises(SystemExit) as exit_info:
        _run_main_set(["--set", "power.ls_secs", "222"], iface, monkeypatch)

    out, err = capsys.readouterr()
    captured = out + err
    assert exit_info.value.code == 1
    assert "reconnect check failed" in captured
    assert "did not reconnect within the timeout" in captured
    assert "Verified:" not in captured


@pytest.mark.unit
def test_local_set_verification_returning_exit_seam_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A nonconforming returning exit seam cannot turn a mismatch into success."""
    iface = MagicMock()
    node = MagicMock()
    monkeypatch.setattr(
        main_module,
        "verify_local_config_apply",
        lambda *_args, **_kwargs: SimpleNamespace(
            status=main_module.LocalApplyStatus.MISMATCH,
            mismatched_fields=("power.ls_secs",),
            missing_sections=(),
        ),
    )
    monkeypatch.setattr(main_module, "_cli_exit", lambda *_args, **_kwargs: None)

    with pytest.raises(AssertionError, match="cli_exit returned unexpectedly"):
        main_module._verify_local_set_apply(
            iface,
            node,
            node_dest="^local",
            pre_write_config_id=None,
            config_fields={"power": {"ls_secs": 222}},
            module_config_fields={},
        )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_local_set_connected_reload_failure_skips_reconnect_fallback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A connection-alive reload failure fails immediately without reconnect."""
    device_local = localonly_pb2.LocalConfig()
    device_local.power.ls_secs = 100
    iface, _node, calls = _build_local_set_interface(
        device_local=device_local, deliver_on_request=False
    )
    reconnect_spy = MagicMock()
    monkeypatch.setattr(
        main_module, "_post_configure_reconnect_and_verify", reconnect_spy
    )

    with pytest.raises(SystemExit) as exit_info:
        _run_main_set(["--set", "power.ls_secs", "222"], iface, monkeypatch)

    out, err = capsys.readouterr()
    captured = out + err
    assert exit_info.value.code == 1
    reconnect_spy.assert_not_called()
    assert "did not return the power configuration section(s) within" in captured
    assert "reconnect" not in captured
    assert "Verified:" not in captured
