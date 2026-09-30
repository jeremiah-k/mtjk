"""Dry-run preview tests for the ``--configure`` document path."""

from __future__ import annotations

import argparse
import contextvars
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn, cast
from unittest.mock import MagicMock, create_autospec

import pytest

import meshtastic.__main__ as main_module
from meshtastic.__main__ import _preview_set_command, setPref
from meshtastic.cli import configure_actions, preference_runtime
from meshtastic.cli.config_preview import (
    PREVIEW_NO_CHANGES_MESSAGE,
    ConfigSnapshotCopies,
)
from meshtastic.cli.configure_actions import (
    CONFIGURE_PREVIEW_HEADER,
    ConfigureActionHooks,
    ConfigureHooks,
)
from meshtastic.cli.context import ActionOutcome, CliContext, CliExit
from meshtastic.mesh_interface import MeshInterface
from meshtastic.protobuf import clientonly_pb2, localonly_pb2

_PREFLIGHT_MODE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "configure_preview_preflight", default=False
)


def _cli_exit(_message: str, return_value: int = 1) -> NoReturn:
    """Raise ``SystemExit`` in place of the production CLI-exit hook.

    Parameters
    ----------
    _message : str
        Ignored user-facing message.
    return_value : int
        Exit status carried by the raised exception.
    """
    raise SystemExit(return_value)


def _recording_exit(exits: list[str]) -> CliExit:
    """Build a CLI-exit hook that records messages before exiting.

    Parameters
    ----------
    exits : list[str]
        List that receives each failure message.

    Returns
    -------
    CliExit
        Non-returning exit hook appending to *exits*.
    """

    def _exit(message: str, return_value: int = 1) -> NoReturn:
        """Record *message* and raise ``SystemExit``."""
        exits.append(message)
        raise SystemExit(return_value)

    return cast(CliExit, _exit)


def _hooks(**overrides: Any) -> ConfigureHooks:
    """Build configure hooks wired to the real traversal runtime.

    Parameters
    ----------
    **overrides : Any
        Hook values that should replace the preview-test defaults.

    Returns
    -------
    ConfigureHooks
        Fully populated configure-runtime dependency seams using the same
        traversal implementation as production dispatch.
    """
    values: dict[str, Any] = {
        "cli_exit": _cli_exit,
        "cli_print": MagicMock(),
        "traverse_config": lambda section, section_values, candidate, **kwargs: (
            preference_runtime.traverse_config(
                section,
                section_values,
                candidate,
                resolve_pref_fn=preference_runtime.resolve_pref,
                set_pref_fn=setPref,
                **kwargs,
            )
        ),
        "preflight_mode": _PREFLIGHT_MODE,
        "is_local_destination": MagicMock(return_value=True),
        "post_seturl_stability_check": MagicMock(return_value=True),
        "post_configure_reconnect_and_verify": MagicMock(),
        "channel_url_matches_current_device_state": MagicMock(return_value=False),
        "pace_configure_write": MagicMock(),
    }
    values.update(overrides)
    return ConfigureHooks(**values)


def _action_hooks(**overrides: Any) -> ConfigureActionHooks:
    """Build configure-action hooks with deterministic defaults.

    Parameters
    ----------
    **overrides : Any
        Hook values that should replace the preview-test defaults.

    Returns
    -------
    ConfigureActionHooks
        Fully populated configure-action dependency seams with the optional
        preview seams left at their fail-closed ``None`` defaults.
    """
    values: dict[str, Any] = {
        "handle_set_command": MagicMock(),
        "handle_configure_command": MagicMock(return_value=(False, False)),
        "export_config": MagicMock(return_value="yaml"),
        "export_profile": MagicMock(return_value=b""),
        "cli_exit": cast(CliExit, _cli_exit),
        "cli_print": MagicMock(),
        "is_local_destination": MagicMock(return_value=True),
    }
    values.update(overrides)
    return ConfigureActionHooks(**values)


def _target_node(**overrides: Any) -> Any:
    """Build a target-node double exposing real cached protobuf messages.

    Parameters
    ----------
    **overrides : Any
        Attribute overrides applied to the node double.

    Returns
    -------
    Any
        Node double whose ``localConfig`` starts with ``lora.hop_limit`` set
        to ``3`` so preview current-value rendering has a real before-state.
    """
    node = MagicMock()
    node.localConfig = localonly_pb2.LocalConfig()
    node.moduleConfig = localonly_pb2.LocalModuleConfig()
    node.localConfig.lora.hop_limit = 3
    for name, value in overrides.items():
        setattr(node, name, value)
    return node


def _interface(node: Any) -> MagicMock:
    """Build a specced interface resolving to *node*.

    Parameters
    ----------
    node : Any
        Target node returned by ``getNode``.

    Returns
    -------
    MagicMock
        Autospecced ``MeshInterface`` double with a set connection event.
    """
    iface = create_autospec(MeshInterface, instance=True)
    iface.isConnected = threading.Event()
    iface.isConnected.set()
    iface.getNode.return_value = node
    return iface


def _install_clock(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sleep: Callable[[float], None] | None = None,
) -> list[float]:
    """Install a no-op configure-runtime clock that records sleep requests.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Fixture used to swap the configure-runtime clock.
    sleep : Callable[[float], None] | None
        Replacement sleep function; defaults to recording durations.

    Returns
    -------
    list[float]
        Sleep durations requested by the code under test.
    """
    current_time = configure_actions.time
    sleeps: list[float] = []
    monkeypatch.setattr(
        configure_actions,
        "time",
        SimpleNamespace(
            monotonic=current_time.monotonic,
            sleep=sleeps.append if sleep is None else sleep,
        ),
    )
    return sleeps


def _write_document(tmp_path: Path, text: str) -> Path:
    """Write one YAML configure document for preview.

    Parameters
    ----------
    tmp_path : Path
        Pytest temporary directory.
    text : str
        YAML document body.

    Returns
    -------
    Path
        Written document path.
    """
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _configure_args(path: Path | str, dest: str = "^local") -> argparse.Namespace:
    """Build parsed ``--configure`` arguments for direct preview calls.

    Parameters
    ----------
    path : Path | str
        Configure document path.
    dest : str
        Destination value.

    Returns
    -------
    argparse.Namespace
        Namespace without a ``dry_run`` attribute, matching focused doubles.
    """
    return argparse.Namespace(configure=[str(path)], dest=dest)


def _printed_lines(hooks: ConfigureHooks | ConfigureActionHooks) -> list[str]:
    """Return every positional string printed through the hook reporter.

    Parameters
    ----------
    hooks : ConfigureHooks | ConfigureActionHooks
        Hooks whose ``cli_print`` mock is inspected.

    Returns
    -------
    list[str]
        Printed lines in call order.
    """
    cli_print = cast(MagicMock, hooks.cli_print)
    return [c.args[0] for c in cli_print.call_args_list if c.args]


def _assert_no_mutations(node: Any, sleeps: list[float]) -> None:
    """Assert that a preview run issued no device writes and no sleeps.

    Parameters
    ----------
    node : Any
        Target node double whose mutating seams are inspected.
    sleeps : list[float]
        Recorded sleep durations.
    """
    node.setOwner.assert_not_called()
    node.setURL.assert_not_called()
    node.setFixedPosition.assert_not_called()
    node.set_canned_message.assert_not_called()
    node.set_ringtone.assert_not_called()
    node.beginSettingsTransaction.assert_not_called()
    node.writeConfig.assert_not_called()
    node.commitSettingsTransaction.assert_not_called()
    assert sleeps == []


@pytest.mark.unit
def test_preview_configure_renders_section_leaf_with_current_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A YAML section leaf previews its requested value and the live current."""
    path = _write_document(tmp_path, "config:\n  lora:\n    hop_limit: 7\n")
    node = _target_node()
    iface = _interface(node)
    hooks = _hooks()
    sleeps = _install_clock(monkeypatch)

    configure_actions._preview_configure_command(
        hooks, iface, _configure_args(path), {}
    )

    assert _printed_lines(hooks) == [
        CONFIGURE_PREVIEW_HEADER,
        "Would set lora.hop_limit = 7 (current: 3)",
        "Would begin settings transaction",
        "Would write config section lora to device",
        "Would commit settings transaction",
    ]
    _assert_no_mutations(node, sleeps)
    iface.getNode.assert_called_once_with("^local", False)


@pytest.mark.unit
def test_preview_configure_renders_enum_by_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Enum leaves render as names for both requested and current values."""
    path = _write_document(tmp_path, "config:\n  lora:\n    region: US\n")
    node = _target_node()
    iface = _interface(node)
    hooks = _hooks()
    sleeps = _install_clock(monkeypatch)

    configure_actions._preview_configure_command(
        hooks, iface, _configure_args(path), {}
    )

    assert "Would set lora.region = US (current: UNSET)" in _printed_lines(hooks)
    _assert_no_mutations(node, sleeps)


@pytest.mark.unit
def test_preview_configure_accepts_binary_device_profile_document(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Serialized DeviceProfile documents preview through the profile decoder."""
    profile = clientonly_pb2.DeviceProfile()
    profile.config.lora.hop_limit = 7
    path = tmp_path / "node.cfg"
    path.write_bytes(profile.SerializeToString())
    node = _target_node()
    iface = _interface(node)
    hooks = _hooks()
    sleeps = _install_clock(monkeypatch)

    configure_actions._preview_configure_command(
        hooks, iface, _configure_args(path), {}
    )

    # The decoded profile re-adds true-default lora flags in set-iteration
    # order, so compare that group without relying on its ordering.
    lines = _printed_lines(hooks)
    assert lines[0] == CONFIGURE_PREVIEW_HEADER
    assert lines[-3:] == [
        "Would begin settings transaction",
        "Would write config section lora to device",
        "Would commit settings transaction",
    ]
    assert set(lines[1:-3]) == {
        "Would set lora.hop_limit = 7 (current: 3)",
        "Would set lora.sx126x_rx_boosted_gain = false (current: false)",
        "Would set lora.use_preset = false (current: false)",
        "Would set lora.tx_enabled = false (current: false)",
    }
    _assert_no_mutations(node, sleeps)


@pytest.mark.unit
def test_preview_configure_renders_direct_values_without_mutating(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Direct values preview in apply order with no device writes or sleeps."""
    path = _write_document(
        tmp_path,
        "owner: Alice\n"
        "owner_short: AL\n"
        "is_licensed: true\n"
        "is_unmessagable: true\n"
        "location:\n"
        "  lat: 1.5\n"
        "  lon: 2.5\n"
        "  alt: 100\n"
        "canned_messages: Hi there\n"
        'ringtone: ":d=16,d=32"\n',
    )
    node = _target_node()
    iface = _interface(node)
    hooks = _hooks()
    sleeps = _install_clock(monkeypatch)

    configure_actions._preview_configure_command(
        hooks, iface, _configure_args(path), {}
    )

    assert _printed_lines(hooks) == [
        CONFIGURE_PREVIEW_HEADER,
        "Would set owner profile (long_name: Alice, short_name: AL, "
        "is_licensed: true, is_unmessagable: true)",
        "Would fix altitude at 100 meters",
        "Would fix latitude at 1.5 degrees",
        "Would fix longitude at 2.5 degrees",
        "Would set device position",
        "Would set canned message messages to Hi there",
        "Would set ringtone to :d=16,d=32",
    ]
    _assert_no_mutations(node, sleeps)


@pytest.mark.unit
def test_preview_configure_owner_names_without_flags_stay_direct(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Owner names without profile flags preview as the two direct writes."""
    path = _write_document(tmp_path, "owner: Alice\nowner_short: AL\n")
    node = _target_node()
    iface = _interface(node)
    hooks = _hooks()
    sleeps = _install_clock(monkeypatch)

    configure_actions._preview_configure_command(
        hooks, iface, _configure_args(path), {}
    )

    assert _printed_lines(hooks) == [
        CONFIGURE_PREVIEW_HEADER,
        "Would set device owner to Alice",
        "Would set device owner short to AL",
    ]
    _assert_no_mutations(node, sleeps)


@pytest.mark.unit
def test_preview_configure_resolves_flag_only_owner_state_read_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Flag-only documents preview the merged owner write from current state."""
    path = _write_document(tmp_path, "is_unmessagable: true\n")
    node = _target_node()
    node.nodeNum = 123
    node.iface = MagicMock()
    node.iface.nodesByNum = {123: {"user": {"isLicensed": True, "longName": "Owner"}}}
    iface = _interface(node)
    hooks = _hooks()
    sleeps = _install_clock(monkeypatch)

    configure_actions._preview_configure_command(
        hooks, iface, _configure_args(path), {}
    )

    assert _printed_lines(hooks) == [
        CONFIGURE_PREVIEW_HEADER,
        "Would set owner profile (long_name: Owner, short_name: not set, "
        "is_licensed: true, is_unmessagable: true)",
    ]
    _assert_no_mutations(node, sleeps)


@pytest.mark.unit
def test_preview_configure_refuses_flag_only_document_without_owner_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unresolvable owner state fails the preview exactly like an apply."""
    path = _write_document(tmp_path, "is_unmessagable: true\n")
    node = _target_node()
    node.nodeNum = 123
    node.iface = MagicMock()
    node.iface.nodesByNum = {}
    iface = _interface(node)
    exits: list[str] = []
    hooks = _hooks(cli_exit=_recording_exit(exits))
    sleeps = _install_clock(monkeypatch)

    with pytest.raises(SystemExit):
        configure_actions._preview_configure_command(
            hooks, iface, _configure_args(path), {}
        )

    assert any("preserve the current owner profile" in message for message in exits)
    _assert_no_mutations(node, sleeps)


@pytest.mark.unit
def test_preview_configure_redacts_channel_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A requested channel URL preview shows only the redacted placeholder."""
    path = _write_document(
        tmp_path, "channel_url: https://meshtastic.org/e/#TopSecretPayload\n"
    )
    node = _target_node()
    iface = _interface(node)
    hooks = _hooks(
        channel_url_matches_current_device_state=MagicMock(return_value=False)
    )
    sleeps = _install_clock(monkeypatch)

    configure_actions._preview_configure_command(
        hooks, iface, _configure_args(path), {}
    )

    assert _printed_lines(hooks) == [
        CONFIGURE_PREVIEW_HEADER,
        "Would set channel url to <redacted>",
    ]
    assert "TopSecretPayload" not in "\n".join(_printed_lines(hooks))
    _assert_no_mutations(node, sleeps)


@pytest.mark.unit
def test_preview_configure_reports_matching_channel_url_skip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An already-matching channel URL reports the skip line without the URL."""
    path = _write_document(
        tmp_path, "channel_url: https://meshtastic.org/e/#TopSecretPayload\n"
    )
    node = _target_node()
    iface = _interface(node)
    hooks = _hooks(
        channel_url_matches_current_device_state=MagicMock(return_value=True)
    )
    sleeps = _install_clock(monkeypatch)

    configure_actions._preview_configure_command(
        hooks, iface, _configure_args(path), {}
    )

    assert _printed_lines(hooks) == [
        CONFIGURE_PREVIEW_HEADER,
        "Channel url already matches device state; skipping apply.",
    ]
    assert "TopSecretPayload" not in "\n".join(_printed_lines(hooks))
    _assert_no_mutations(node, sleeps)


@pytest.mark.unit
def test_preview_configure_fails_late_invalid_section_before_reporting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A structurally invalid section fails before any operation is reported."""
    path = _write_document(tmp_path, "config:\n  lora:\n    region: NOT_A_REGION\n")
    node = _target_node()
    iface = _interface(node)
    exits: list[str] = []
    hooks = _hooks(cli_exit=_recording_exit(exits))
    sleeps = _install_clock(monkeypatch)

    with pytest.raises(SystemExit):
        configure_actions._preview_configure_command(
            hooks, iface, _configure_args(path), {}
        )

    assert any(
        "Failed to apply config section 'lora' due to structural errors." in message
        and "Invalid field: lora.region" in message
        for message in exits
    )
    printed = _printed_lines(hooks)
    assert not any("Would" in line for line in printed)
    assert not any("Dry run:" in line for line in printed)
    assert CONFIGURE_PREVIEW_HEADER not in printed
    _assert_no_mutations(node, sleeps)


@pytest.mark.unit
def test_preview_configure_skips_unknown_fields_without_reporting_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unknown leaf fields are skipped by traversal and never previewed."""
    path = _write_document(
        tmp_path, "config:\n  lora:\n    definitely_not_a_field: 5\n"
    )
    node = _target_node()
    iface = _interface(node)
    hooks = _hooks()
    sleeps = _install_clock(monkeypatch)

    configure_actions._preview_configure_command(
        hooks, iface, _configure_args(path), {}
    )

    assert not any("definitely_not_a_field" in line for line in _printed_lines(hooks))
    _assert_no_mutations(node, sleeps)


@pytest.mark.unit
def test_preview_configure_rejects_remote_channel_url_with_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Remote channel_url plus section writes are rejected before node access."""
    path = _write_document(
        tmp_path,
        "channel_url: https://meshtastic.org/e/#remote\n"
        "config:\n"
        "  bluetooth:\n"
        "    enabled: true\n",
    )
    node = _target_node()
    iface = _interface(node)
    exits: list[str] = []
    hooks = _hooks(
        cli_exit=_recording_exit(exits),
        is_local_destination=MagicMock(return_value=False),
    )
    sleeps = _install_clock(monkeypatch)

    with pytest.raises(SystemExit):
        configure_actions._preview_configure_command(
            hooks, iface, _configure_args(path, dest="!87654321"), {}
        )

    assert any("separate operations" in message for message in exits)
    iface.getNode.assert_not_called()
    _assert_no_mutations(node, sleeps)


@pytest.mark.unit
def test_preview_configure_prints_transaction_lifecycle_without_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Section previews are followed by transaction lines, never real writes."""
    path = _write_document(
        tmp_path,
        "config:\n"
        "  lora:\n"
        "    hop_limit: 7\n"
        "module_config:\n"
        "  mqtt:\n"
        "    enabled: true\n",
    )
    node = _target_node()
    node.moduleConfig.mqtt.enabled = False
    iface = _interface(node)
    hooks = _hooks()
    sleeps = _install_clock(monkeypatch)

    configure_actions._preview_configure_command(
        hooks, iface, _configure_args(path), {}
    )

    assert _printed_lines(hooks) == [
        CONFIGURE_PREVIEW_HEADER,
        "Would set lora.hop_limit = 7 (current: 3)",
        "Would set mqtt.enabled = true (current: false)",
        "Would begin settings transaction",
        "Would write config section lora to device",
        "Would write config section mqtt to device",
        "Would commit settings transaction",
    ]
    _assert_no_mutations(node, sleeps)


@pytest.mark.unit
def test_configure_apply_preflight_uses_copy_only_shared_validation() -> None:
    """Normal configure preflight validates through copies without live mutation."""
    node = _target_node()
    original_hop_limit = node.localConfig.lora.hop_limit
    hooks = _hooks()

    configure_actions._preflight_configure_sections(
        hooks,
        node,
        config_sections={"lora": {"hop_limit": 7}},
        module_config_sections={},
    )

    assert node.localConfig.lora.hop_limit == original_hop_limit
    cast(MagicMock, hooks.cli_print).assert_not_called()


@pytest.mark.unit
def test_preview_configure_nested_leaf_reports_unloaded_current_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Nested section leaves flatten canonically and report unloaded live state."""
    path = _write_document(
        tmp_path,
        "module_config:\n"
        "  mqtt:\n"
        "    map_report_settings:\n"
        "      publish_interval_secs: 120\n",
    )
    node = _target_node()
    node.moduleConfig.ClearField("mqtt")
    iface = _interface(node)
    hooks = _hooks()
    sleeps = _install_clock(monkeypatch)

    configure_actions._preview_configure_command(
        hooks, iface, _configure_args(path), {}
    )

    assert (
        "Would set mqtt.map_report_settings.publish_interval_secs = 120 "
        "(current: not set)"
    ) in _printed_lines(hooks)
    _assert_no_mutations(node, sleeps)


@pytest.mark.unit
def test_preview_configure_redacts_secret_section_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Section previews redact path-classified secrets on new and current sides."""
    path = _write_document(
        tmp_path,
        "module_config:\n"
        "  mqtt:\n"
        "    username: new-preview-user\n"
        "    password: new-preview-password\n",
    )
    node = _target_node()
    node.moduleConfig.mqtt.username = "old-preview-user"
    node.moduleConfig.mqtt.password = "old-preview-password"
    iface = _interface(node)
    hooks = _hooks()
    sleeps = _install_clock(monkeypatch)

    configure_actions._preview_configure_command(
        hooks, iface, _configure_args(path), {}
    )

    output = "\n".join(_printed_lines(hooks))
    assert "Would set mqtt.username = <redacted> (current: <redacted>)" in output
    assert "Would set mqtt.password = <redacted> (current: <redacted>)" in output
    for secret in (
        "new-preview-user",
        "new-preview-password",
        "old-preview-user",
        "old-preview-password",
    ):
        assert secret not in output
    _assert_no_mutations(node, sleeps)


@pytest.mark.unit
def test_preview_configure_validates_against_chained_snapshot_with_live_current(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A chained snapshot provides the preview state; live messages stay current."""
    path = _write_document(tmp_path, "config:\n  lora:\n    hop_limit: 6\n")
    snapshot = ConfigSnapshotCopies(
        local_config=localonly_pb2.LocalConfig(),
        module_config=localonly_pb2.LocalModuleConfig(),
    )
    snapshot.local_config.lora.hop_limit = 5
    node = _target_node()
    iface = _interface(node)
    hooks = _hooks()
    sleeps = _install_clock(monkeypatch)

    configure_actions._preview_configure_command(
        hooks, iface, _configure_args(path), {}, snapshot
    )

    assert _printed_lines(hooks) == [
        CONFIGURE_PREVIEW_HEADER,
        "Would set lora.hop_limit = 6 (current: 3)",
        "Would begin settings transaction",
        "Would write config section lora to device",
        "Would commit settings transaction",
    ]
    _assert_no_mutations(node, sleeps)


@pytest.mark.unit
def test_snapshot_absorb_missing_sections_gains_new_sections_only() -> None:
    """Absorb fills only sections the node delivered; staged ones survive."""
    node = _target_node()
    node.localConfig.bluetooth.enabled = True
    node.moduleConfig.mqtt.enabled = True
    snapshot = ConfigSnapshotCopies(
        local_config=localonly_pb2.LocalConfig(),
        module_config=localonly_pb2.LocalModuleConfig(),
    )
    snapshot.local_config.lora.hop_limit = 5

    snapshot.absorb_missing_sections(node)

    assert snapshot.local_config.lora.hop_limit == 5
    assert snapshot.local_config.bluetooth.enabled is True
    assert snapshot.module_config.mqtt.enabled is True


@pytest.mark.unit
def test_snapshot_absorb_ignores_sections_absent_on_node() -> None:
    """Sections the node has not delivered stay absent from the copies."""
    node = _target_node()
    node.localConfig.ClearField("bluetooth")
    snapshot = ConfigSnapshotCopies(
        local_config=localonly_pb2.LocalConfig(),
        module_config=localonly_pb2.LocalModuleConfig(),
    )
    snapshot.local_config.lora.hop_limit = 5

    snapshot.absorb_missing_sections(node)

    assert snapshot.local_config.lora.hop_limit == 5
    assert not snapshot.local_config.HasField("bluetooth")


@pytest.mark.unit
def test_main_configure_preview_wrapper_forwards_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The historical __main__ preview seam forwards the chained snapshot."""
    hooks = MagicMock()
    preview = MagicMock()
    iface = _interface(_target_node())
    args = argparse.Namespace(configure=["config.yaml"], dest="^local")
    snapshot = ConfigSnapshotCopies(
        local_config=localonly_pb2.LocalConfig(),
        module_config=localonly_pb2.LocalModuleConfig(),
    )
    monkeypatch.setattr(main_module, "_configure_hooks", lambda: hooks)
    monkeypatch.setattr(
        main_module.cli_configure_actions, "_preview_configure_command", preview
    )

    main_module._run_configure_preview(iface, args, {"timeout": 1}, snapshot)

    preview.assert_called_once_with(
        hooks, iface, args, {"timeout": 1}, snapshot=snapshot
    )


@pytest.mark.unit
def test_handle_configure_actions_dry_run_chains_set_snapshot_into_configure() -> None:
    """Combined dry runs pass the --set snapshot into the configure preview."""
    iface = _interface(_target_node())
    snapshot = ConfigSnapshotCopies(
        local_config=localonly_pb2.LocalConfig(),
        module_config=localonly_pb2.LocalModuleConfig(),
    )
    preview_set = MagicMock(return_value=snapshot)
    preview_configure = MagicMock()
    hooks = _action_hooks(
        handle_set_command=MagicMock(),
        handle_configure_command=MagicMock(),
        export_config=MagicMock(),
        preview_set_command=preview_set,
        preview_configure_command=preview_configure,
    )
    args = argparse.Namespace(
        set=[["lora.hop_limit", "3"]],
        configure=["config.yaml"],
        export_config=None,
        dest="^local",
        dry_run=True,
    )
    context = CliContext(
        interface=iface, args=args, get_node_kwargs={}, outcome=ActionOutcome()
    )

    configure_actions._handle_configure_actions(context, hooks)

    preview_set.assert_called_once()
    preview_configure.assert_called_once()
    assert preview_configure.call_args.args[3] is snapshot
    assert _printed_lines(hooks) == [PREVIEW_NO_CHANGES_MESSAGE]
    assert cast(MagicMock, hooks.cli_print).call_args.kwargs == {"force": True}
    assert context.outcome.close_now is True
    assert context.outcome.wait_for_ack_nak is False
    assert context.outcome.skip_ack_wait is False
    cast(MagicMock, hooks.handle_set_command).assert_not_called()
    cast(MagicMock, hooks.handle_configure_command).assert_not_called()
    cast(MagicMock, hooks.export_config).assert_not_called()


@pytest.mark.unit
def test_handle_configure_actions_dry_run_set_only_prints_summary_once() -> None:
    """A --set-only dry run previews once and still prints the shared summary."""
    iface = _interface(_target_node())
    preview_set = MagicMock(
        return_value=ConfigSnapshotCopies(
            local_config=localonly_pb2.LocalConfig(),
            module_config=localonly_pb2.LocalModuleConfig(),
        )
    )
    hooks = _action_hooks(
        handle_set_command=MagicMock(),
        preview_set_command=preview_set,
        export_config=MagicMock(),
    )
    args = argparse.Namespace(
        set=[["lora.hop_limit", "3"]],
        configure=None,
        export_config=None,
        dest="^local",
        dry_run=True,
    )
    context = CliContext(
        interface=iface, args=args, get_node_kwargs={}, outcome=ActionOutcome()
    )

    configure_actions._handle_configure_actions(context, hooks)

    preview_set.assert_called_once_with(iface, args, {})
    cast(MagicMock, hooks.cli_print).assert_called_once_with(
        PREVIEW_NO_CHANGES_MESSAGE, force=True
    )
    assert context.outcome.close_now is True
    assert context.outcome.wait_for_ack_nak is False
    assert context.outcome.skip_ack_wait is False
    cast(MagicMock, hooks.handle_set_command).assert_not_called()


@pytest.mark.unit
def test_handle_configure_actions_dry_run_missing_configure_hook_fails_closed() -> None:
    """A missing configure preview seam fails closed without mutating."""
    iface = _interface(_target_node())
    exits: list[str] = []
    hooks = _action_hooks(cli_exit=_recording_exit(exits))
    args = argparse.Namespace(
        set=None,
        configure=["config.yaml"],
        export_config=None,
        dest="^local",
        dry_run=True,
    )
    context = CliContext(
        interface=iface, args=args, get_node_kwargs={}, outcome=ActionOutcome()
    )

    with pytest.raises(SystemExit):
        configure_actions._handle_configure_actions(context, hooks)

    assert any("--dry-run preview for --configure" in message for message in exits)
    cast(MagicMock, hooks.handle_configure_command).assert_not_called()
    cast(MagicMock, hooks.export_config).assert_not_called()
    assert _printed_lines(hooks) == []


@pytest.mark.unit
def test_handle_configure_actions_dry_run_missing_set_hook_fails_closed() -> None:
    """A missing set preview seam fails closed without mutating."""
    iface = _interface(_target_node())
    exits: list[str] = []
    hooks = _action_hooks(cli_exit=_recording_exit(exits))
    args = argparse.Namespace(
        set=[["lora.hop_limit", "3"]],
        configure=None,
        export_config=None,
        dest="^local",
        dry_run=True,
    )
    context = CliContext(
        interface=iface, args=args, get_node_kwargs={}, outcome=ActionOutcome()
    )

    with pytest.raises(SystemExit):
        configure_actions._handle_configure_actions(context, hooks)

    assert any("--dry-run preview for --set" in message for message in exits)
    cast(MagicMock, hooks.handle_set_command).assert_not_called()
    assert _printed_lines(hooks) == []


@pytest.mark.unit
def test_handle_configure_actions_dry_run_missing_configure_keeps_set_output() -> None:
    """Fail-closed ordering keeps set preview output and prints no summary."""
    iface = _interface(_target_node())
    exits: list[str] = []
    snapshot = ConfigSnapshotCopies(
        local_config=localonly_pb2.LocalConfig(),
        module_config=localonly_pb2.LocalModuleConfig(),
    )

    def _preview_set(*_args: Any, **_kwargs: Any) -> ConfigSnapshotCopies:
        hooks.cli_print("Would set lora.hop_limit = 4 (current: 3)")
        return snapshot

    hooks = _action_hooks(
        cli_exit=_recording_exit(exits), preview_set_command=_preview_set
    )
    args = argparse.Namespace(
        set=[["lora.hop_limit", "4"]],
        configure=["config.yaml"],
        export_config=None,
        dest="^local",
        dry_run=True,
    )
    context = CliContext(
        interface=iface, args=args, get_node_kwargs={}, outcome=ActionOutcome()
    )

    with pytest.raises(SystemExit):
        configure_actions._handle_configure_actions(context, hooks)

    assert any("--dry-run preview for --configure" in message for message in exits)
    assert _printed_lines(hooks) == ["Would set lora.hop_limit = 4 (current: 3)"]
    assert PREVIEW_NO_CHANGES_MESSAGE not in _printed_lines(hooks)
    cast(MagicMock, hooks.handle_set_command).assert_not_called()
    cast(MagicMock, hooks.handle_configure_command).assert_not_called()
    cast(MagicMock, hooks.export_config).assert_not_called()


@pytest.mark.unit
def test_handle_configure_actions_dry_run_deduplicates_duplicate_set_field(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A duplicated --set field previews once with the last value assigned."""
    node = _target_node()
    node.localConfig.power.SetInParent()
    node.localConfig.power.ls_secs = 60
    iface = _interface(node)
    hooks = _action_hooks(preview_set_command=_preview_set_command)
    args = argparse.Namespace(
        set=[["power.ls_secs", "300"], ["power.ls_secs", "600"]],
        configure=None,
        export_config=None,
        dest="^local",
        dry_run=True,
    )
    context = CliContext(
        interface=iface, args=args, get_node_kwargs={}, outcome=ActionOutcome()
    )

    configure_actions._handle_configure_actions(context, hooks)

    out, _err = capsys.readouterr()
    would_set = [
        line for line in out.splitlines() if line.startswith("Would set power.ls_secs")
    ]
    assert would_set == ["Would set power.ls_secs = 600 (current: 60)"]
    cast(MagicMock, hooks.cli_print).assert_called_once_with(
        PREVIEW_NO_CHANGES_MESSAGE, force=True
    )
    cast(MagicMock, hooks.handle_set_command).assert_not_called()


@pytest.mark.unit
def test_preview_set_dry_run_rejects_superseded_invalid_duplicate(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A superseded invalid duplicate rejects the preview batch like the real one."""
    node = _target_node()
    node.localConfig.power.SetInParent()
    node.localConfig.power.ls_secs = 60
    iface = _interface(node)
    hooks = _action_hooks(preview_set_command=_preview_set_command)
    args = argparse.Namespace(
        set=[["power.ls_secs", "banana"], ["power.ls_secs", "300"]],
        configure=None,
        export_config=None,
        dest="^local",
        dry_run=True,
    )
    context = CliContext(
        interface=iface, args=args, get_node_kwargs={}, outcome=ActionOutcome()
    )

    with pytest.raises(SystemExit) as exit_info:
        configure_actions._handle_configure_actions(context, hooks)

    assert exit_info.value.code == 1
    out, _err = capsys.readouterr()
    assert "Would set" not in out
    assert PREVIEW_NO_CHANGES_MESSAGE not in out
    cast(MagicMock, hooks.handle_set_command).assert_not_called()


@pytest.mark.unit
def test_handle_configure_actions_without_dry_run_keeps_legacy_dispatch() -> None:
    """Namespaces without a dry_run attribute keep the historical dispatch."""
    iface = _interface(_target_node())
    configure_result = configure_actions._ConfigureCommandResult(
        False, False, request_sent=False
    )
    hooks = _action_hooks(
        handle_configure_command=MagicMock(return_value=configure_result),
        export_config=MagicMock(),
    )
    args = argparse.Namespace(
        set=None, configure=["config.yaml"], export_config=None, dest="^local"
    )
    context = CliContext(
        interface=iface, args=args, get_node_kwargs={}, outcome=ActionOutcome()
    )

    configure_actions._handle_configure_actions(context, hooks)

    cast(MagicMock, hooks.handle_configure_command).assert_called_once_with(
        iface, args, {}
    )
    assert context.outcome.close_now is True
    assert context.outcome.wait_for_ack_nak is False
    assert context.outcome.skip_ack_wait is False
    assert PREVIEW_NO_CHANGES_MESSAGE not in "\n".join(_printed_lines(hooks))
