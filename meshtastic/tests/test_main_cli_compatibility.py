"""Compatibility and failure-path tests for ``__main__`` CLI adapters."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import meshtastic.__main__ as main_module
import meshtastic.cli.configure_actions as cli_configure_actions
import meshtastic.cli.device_actions as cli_device_actions
from meshtastic.cli.values import is_local_destination as _is_local_destination
from meshtastic.configure_verify import _verify_channel_url_against_state
from meshtastic.protobuf import localonly_pb2


@pytest.mark.unit
@pytest.mark.parametrize(
    ("wrapper_name", "forwards_hooks", "kwargs"),
    [
        (
            "_preflight_configure_sections",
            True,
            {"config_sections": {"power": {}}, "module_config_sections": {}},
        ),
        (
            "_refresh_no_disconnect_verify_state",
            False,
            {
                "verify_channel_url": "url",
                "verify_config_fields": {"power": {}},
                "verify_module_config_fields": {},
            },
        ),
    ],
)
def test_void_configure_compat_wrappers_delegate(
    monkeypatch: pytest.MonkeyPatch,
    wrapper_name: str,
    forwards_hooks: bool,
    kwargs: dict[str, Any],
) -> None:
    """Retained ``__main__`` seams must delegate to the internal runtime."""
    delegate = MagicMock()
    hooks = object()
    monkeypatch.setattr(cli_configure_actions, wrapper_name, delegate)
    monkeypatch.setattr(main_module, "_configure_hooks", MagicMock(return_value=hooks))
    target = object()

    getattr(main_module, wrapper_name)(target, **kwargs)

    if forwards_hooks:
        delegate.assert_called_once_with(hooks, target, **kwargs)
    else:
        delegate.assert_called_once_with(target, **kwargs)


@pytest.mark.unit
def test_verify_config_sections_compat_wrapper_delegates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The verification wrapper should preserve verified-fields forwarding."""
    delegate = MagicMock(return_value=True)
    monkeypatch.setattr(cli_configure_actions, "_verify_config_sections", delegate)
    verified: list[str] = []
    proto_config = object()

    assert main_module._verify_config_sections(
        {"power": {}}, proto_config, "Config", verified
    )
    delegate.assert_called_once_with(
        {"power": {}}, proto_config, "Config", verified_fields=verified
    )


@pytest.mark.unit
def test_verify_post_reconnect_compat_wrapper_injects_url_comparator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reconnect wrapper should inject the historical URL-comparison seam."""
    expected = main_module._ConfigureReconnectResult.VERIFIED
    delegate = MagicMock(return_value=expected)
    monkeypatch.setattr(
        cli_configure_actions, "_verify_post_reconnect_config", delegate
    )
    interface = MagicMock()

    result = main_module._verify_post_reconnect_config(
        interface,
        "^local",
        verify_channel_url="url",
        verify_config_fields={"power": {}},
        verify_module_config_fields={"telemetry": {}},
    )

    assert result is expected
    assert (
        delegate.call_args.kwargs["verify_channel_url_against_state"]
        is _verify_channel_url_against_state
    )


@pytest.mark.unit
def test_nonsecret_preflight_error_preserves_detail() -> None:
    """Non-secret preference failures should retain useful exception detail."""
    assert (
        main_module._format_set_preflight_exception(
            "lora.region", ValueError("bad region")
        )
        == "lora.region: bad region"
    )


@pytest.mark.unit
@pytest.mark.parametrize("apply_result", [ValueError("apply failed"), False])
def test_set_apply_divergence_terminates_after_successful_preflight(
    monkeypatch: pytest.MonkeyPatch, apply_result: Exception | bool
) -> None:
    """Post-preflight divergence must abort rather than writing inconsistent state."""
    node = MagicMock()
    node.localConfig = localonly_pb2.LocalConfig()
    node.moduleConfig = localonly_pb2.LocalModuleConfig()
    interface = MagicMock()
    interface.getNode.return_value = node
    field = SimpleNamespace(name="power")
    config = object()
    monkeypatch.setattr(
        main_module,
        "_normalize_set_entries",
        MagicMock(return_value=[("power.ls_secs", "1")]),
    )
    monkeypatch.setattr(main_module, "_ensure_set_sections_loaded", MagicMock())
    monkeypatch.setattr(
        main_module, "_preflight_set_entries", MagicMock(return_value=True)
    )
    monkeypatch.setattr(
        main_module, "_resolve_set_target", MagicMock(return_value=(config, field))
    )
    if isinstance(apply_result, Exception):
        monkeypatch.setattr(main_module, "setPref", MagicMock(side_effect=apply_result))
    else:
        monkeypatch.setattr(
            main_module, "setPref", MagicMock(return_value=apply_result)
        )
    cli_exit = MagicMock(side_effect=SystemExit(1))
    monkeypatch.setattr(main_module, "_cli_exit", cli_exit)

    with pytest.raises(SystemExit):
        main_module._handle_set_command(
            interface, SimpleNamespace(dest="^local", set=[["power.ls_secs", "1"]]), {}
        )

    assert "apply diverged" in str(cli_exit.call_args)
    node.writeConfig.assert_not_called()


@pytest.mark.unit
def test_ota_compat_wrapper_delegates_to_private_device_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retained ``__main__`` OTA seam must delegate with historical hooks."""
    delegate = MagicMock()
    monkeypatch.setattr(cli_device_actions, "_handle_ota_update", delegate)
    interface = MagicMock()
    args = SimpleNamespace(ota_update="firmware.bin", dest="^local")
    get_node_kwargs = {"timeout": 10}

    main_module._handle_ota_update(interface, args, get_node_kwargs)

    delegate.assert_called_once_with(
        interface,
        args,
        get_node_kwargs,
        cli_exit=main_module._cli_exit,
        cli_print=main_module._cli_print,
        is_local_destination=_is_local_destination,
    )
