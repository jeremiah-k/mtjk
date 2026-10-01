"""Configure-path config-section acquisition and snapshot-coherence tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, NoReturn, cast
from unittest.mock import MagicMock

import pytest
from google.protobuf.descriptor import FieldDescriptor

from meshtastic.__main__ import setPref
from meshtastic.cli import configure_actions, configure_values, preference_runtime
from meshtastic.cli.configure_actions import (
    ConfigureHooks,
    ConfigureReconnectResult,
    _requested_configure_section_fields,
)
from meshtastic.cli.context import CliExit
from meshtastic.cli.preference_runtime import CONFIGURE_PREFLIGHT_MODE
from meshtastic.node import Node
from meshtastic.protobuf import localonly_pb2

CONFIG_SECTION_TIMEOUT_MESSAGE = (
    "ERROR: timed out waiting for the lora configuration section from the "
    "device; no changes were made."
)


def _cli_exit(_message: str, return_value: int = 1) -> NoReturn:
    """Raise ``SystemExit`` in place of the production CLI-exit hook."""
    raise SystemExit(return_value)


def _recording_exit(exits: list[str]) -> CliExit:
    """Build a CLI-exit hook that records messages before exiting."""

    def _exit(message: str, return_value: int = 1) -> NoReturn:
        exits.append(message)
        raise SystemExit(return_value)

    return cast(CliExit, _exit)


def _hooks(**overrides: Any) -> ConfigureHooks:
    """Build configure hooks wired to the real traversal runtime."""
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
        "preflight_mode": CONFIGURE_PREFLIGHT_MODE,
        "is_local_destination": MagicMock(return_value=True),
        "post_seturl_stability_check": MagicMock(return_value=True),
        "post_configure_reconnect_and_verify": MagicMock(),
        "channel_url_matches_current_device_state": MagicMock(return_value=False),
        "pace_configure_write": MagicMock(),
    }
    values.update(overrides)
    return ConfigureHooks(**values)


def _install_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Install a no-op configure-runtime clock that records sleep requests."""
    sleeps: list[float] = []
    monkeypatch.setattr(
        configure_actions,
        "time",
        SimpleNamespace(
            monotonic=configure_actions.time.monotonic,
            sleep=sleeps.append,
        ),
    )
    return sleeps


def _config_node() -> MagicMock:
    """Return a node mock with real protobuf config wrappers."""
    node = MagicMock(autospec=Node)
    node.localConfig = localonly_pb2.LocalConfig()
    node.moduleConfig = localonly_pb2.LocalModuleConfig()
    node.noProto = False
    node._timeout = SimpleNamespace(waitForSet=lambda probe, attrs: True)
    return node


def _prepared(
    config_sections: dict[str, dict] | None = None,
    module_config_sections: dict[str, dict] | None = None,
) -> configure_actions._PreparedConfigureDocument:
    """Build a prepared configure document carrying only section mappings."""
    return configure_actions._PreparedConfigureDocument(
        direct_values=configure_values._DirectConfigureValues(),
        config_sections=config_sections or {},
        module_config_sections=module_config_sections or {},
    )


@pytest.mark.unit
def test_requested_section_fields_resolves_both_roots_in_apply_order() -> None:
    """Snake and Camel YAML keys resolve onto their owning protobuf roots."""
    node = _config_node()
    prepared = _prepared(
        config_sections={"lora": {"hop_limit": 7}},
        module_config_sections={"externalNotification": {"enabled": True}},
    )

    section_fields = _requested_configure_section_fields(node, prepared)

    assert [(root, field.name) for root, field in section_fields] == [
        (node.localConfig, "lora"),
        (node.moduleConfig, "external_notification"),
    ]
    for _root, field in section_fields:
        assert isinstance(field, FieldDescriptor)


@pytest.mark.unit
def test_requested_section_fields_skips_unknown_sections() -> None:
    """Unknown and non-section scalar names are skipped for traversal to report."""
    node = _config_node()
    prepared = _prepared(
        config_sections={
            "lora": {"hop_limit": 7},
            "not_a_section": {"x": 1},
            # version is a real LocalConfig field but a scalar, not a section.
            "version": {"ignored": True},
        },
    )

    section_fields = _requested_configure_section_fields(node, prepared)

    assert [(root, field.name) for root, field in section_fields] == [
        (node.localConfig, "lora")
    ]


@pytest.mark.unit
def test_requested_section_fields_deduplicates_case_variants() -> None:
    """YAML key spellings that normalize to one section request it once."""
    node = _config_node()
    prepared = _prepared(
        config_sections={"lora": {"hop_limit": 7}, "Lora": {"hop_limit": 9}},
    )

    section_fields = _requested_configure_section_fields(node, prepared)

    assert [(root, field.name) for root, field in section_fields] == [
        (node.localConfig, "lora")
    ]


def _write_document(tmp_path: Any, text: str) -> Any:
    """Write one YAML configure document for apply."""
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _configure_args(path: Any, dest: str = "^local") -> SimpleNamespace:
    """Build parsed ``--configure`` arguments for direct execution."""
    return SimpleNamespace(configure=[str(path)], dest=dest)


@pytest.mark.unit
def test_configure_timeout_refuses_before_preflight_writes_and_transaction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A section that never arrives aborts with no preflight, writes, or transaction."""
    path = _write_document(tmp_path, "owner: Bob\nconfig:\n  lora:\n    hop_limit: 7\n")
    node = _config_node()
    node._timeout = SimpleNamespace(waitForSet=lambda probe, attrs: False)
    iface = MagicMock()
    exits: list[str] = []
    hooks = _hooks(
        cli_exit=_recording_exit(exits),
        post_configure_reconnect_and_verify=MagicMock(),
    )
    plan = configure_actions._prepare_configure_execution(
        hooks, iface, _configure_args(path)
    )

    with pytest.raises(SystemExit):
        configure_actions._execute_configure_plan(hooks, iface, node, plan)

    assert exits == [CONFIG_SECTION_TIMEOUT_MESSAGE]
    node.requestConfig.assert_called_once()
    assert node.requestConfig.call_args.args[0].name == "lora"
    node.setOwner.assert_not_called()
    node.setURL.assert_not_called()
    node.setFixedPosition.assert_not_called()
    node.beginSettingsTransaction.assert_not_called()
    node.writeConfig.assert_not_called()
    node.commitSettingsTransaction.assert_not_called()
    assert cast(MagicMock, hooks.cli_print).call_args_list == []


@pytest.mark.unit
def test_configure_missing_wait_machinery_refuses_before_any_write(
    tmp_path: Any,
) -> None:
    """Absent requested state cannot fall through when a node has no wait seam."""
    path = _write_document(tmp_path, "config:\n  lora:\n    hop_limit: 7\n")
    node = _config_node()
    node._timeout = SimpleNamespace()
    iface = MagicMock()
    exits: list[str] = []
    hooks = _hooks(cli_exit=_recording_exit(exits))
    plan = configure_actions._prepare_configure_execution(
        hooks, iface, _configure_args(path)
    )

    with pytest.raises(SystemExit):
        configure_actions._execute_configure_plan(hooks, iface, node, plan)

    assert exits == [CONFIG_SECTION_TIMEOUT_MESSAGE]
    node.requestConfig.assert_called_once()
    node.setOwner.assert_not_called()
    node.setURL.assert_not_called()
    node.setFixedPosition.assert_not_called()
    node.beginSettingsTransaction.assert_not_called()
    node.writeConfig.assert_not_called()
    node.commitSettingsTransaction.assert_not_called()


@pytest.mark.unit
def test_configure_apply_acquires_delayed_section_before_preflight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A synchronous requestConfig response feeds preflight and the transaction."""
    path = _write_document(tmp_path, "config:\n  lora:\n    hop_limit: 7\n")
    node = _config_node()
    lora_presence_at_traversal: list[bool] = []

    def populate_lora(config_type: FieldDescriptor) -> None:
        assert config_type.name == "lora"
        node.localConfig.lora.hop_limit = 5

    def traverse(
        section: str, section_values: Any, candidate: Any, **kwargs: Any
    ) -> Any:
        if section == "lora":
            lora_presence_at_traversal.append(node.localConfig.HasField("lora"))
        return preference_runtime.traverse_config(
            section,
            section_values,
            candidate,
            resolve_pref_fn=preference_runtime.resolve_pref,
            set_pref_fn=setPref,
            **kwargs,
        )

    node.requestConfig.side_effect = populate_lora
    hooks = _hooks(
        traverse_config=traverse,
        post_configure_reconnect_and_verify=MagicMock(
            return_value=ConfigureReconnectResult.VERIFIED
        ),
    )
    iface = MagicMock()
    sleeps = _install_clock(monkeypatch)
    plan = configure_actions._prepare_configure_execution(
        hooks, iface, _configure_args(path)
    )

    result = configure_actions._execute_configure_plan(hooks, iface, node, plan)

    assert result.settings_transaction_started is True
    # Preflight and transaction traversal both ran after requestConfig
    # populated the section on the live cached messages.
    assert lora_presence_at_traversal == [True, True]
    node.beginSettingsTransaction.assert_called_once()
    node.writeConfig.assert_called_once_with("lora")
    node.commitSettingsTransaction.assert_called_once()
    assert sleeps == [configure_actions.CONFIG_COMMIT_SETTLE_SECONDS]


@pytest.mark.unit
def test_configure_apply_skips_request_for_present_default_section() -> None:
    """A present default-valued section is never requested or waited on."""
    node = _config_node()
    node.localConfig.lora.SetInParent()
    wait_for_set = MagicMock()
    node._timeout = SimpleNamespace(waitForSet=wait_for_set)
    hooks = _hooks(
        post_configure_reconnect_and_verify=MagicMock(
            return_value=ConfigureReconnectResult.VERIFIED
        )
    )
    iface = MagicMock()
    plan = configure_actions._ConfigureExecutionPlan(
        prepared=configure_actions._PreparedConfigureDocument(
            direct_values=configure_values._DirectConfigureValues(),
            config_sections={"lora": {"hop_limit": 7}},
            module_config_sections={},
        ),
        destination="^local",
        is_local_target=True,
        has_config_writes=True,
    )

    result = configure_actions._execute_configure_plan(hooks, iface, node, plan)

    assert result.settings_transaction_started is True
    node.requestConfig.assert_not_called()
    wait_for_set.assert_not_called()
    node.beginSettingsTransaction.assert_called_once()
    node.writeConfig.assert_called_once_with("lora")
