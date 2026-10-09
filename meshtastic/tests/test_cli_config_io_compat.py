"""Compatibility seams for the extracted CLI configuration I/O runtime."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import meshtastic.__main__ as main_module


@pytest.mark.unit
def test_export_config_facade_injects_current_main_module_shims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy monkeypatches on ``meshtastic.__main__`` must still affect export."""
    prefix = MagicMock()
    materialize = MagicMock()
    message_to_dict = MagicMock()
    config_defaults = {("device", "serial_enabled")}
    module_defaults = {("mqtt", "enabled")}
    monkeypatch.setattr(main_module, "_prefix_base64_bytes_fields", prefix)
    monkeypatch.setattr(main_module, "_set_missing_flags_false", materialize)
    monkeypatch.setattr(main_module, "MessageToDict", message_to_dict)
    monkeypatch.setattr(main_module, "CONFIG_TRUE_DEFAULTS", config_defaults)
    monkeypatch.setattr(main_module, "MODULE_TRUE_DEFAULTS", module_defaults)

    iface = MagicMock()
    with patch.object(
        main_module.cli_config_io,
        "export_config",
        return_value="exported",
    ) as runtime_export:
        result = main_module.exportConfig(iface)

    assert result == "exported"
    runtime_export.assert_called_once_with(
        iface,
        camel_case=main_module.mt_config.camel_case,
        message_to_dict=message_to_dict,
        prefix_base64_bytes_fields_fn=prefix,
        set_missing_flags_false_fn=materialize,
        config_true_defaults=config_defaults,
        module_true_defaults=module_defaults,
    )


@pytest.mark.unit
def test_print_available_fields_facade_uses_current_localonly_factories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replacing the legacy localonly module must still affect field discovery."""
    local_factory = MagicMock()
    module_factory = MagicMock()
    monkeypatch.setattr(
        main_module,
        "localonly_pb2",
        SimpleNamespace(
            LocalConfig=local_factory,
            LocalModuleConfig=module_factory,
        ),
    )

    with patch.object(
        main_module.cli_config_io,
        "print_available_config_fields",
    ) as runtime_print:
        main_module.printAvailableConfigFields()

    kwargs = runtime_print.call_args.kwargs
    assert kwargs["local_config_factory"] is local_factory
    assert kwargs["module_config_factory"] is module_factory


@pytest.mark.unit
def test_print_config_facade_uses_current_camel_case_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy ``printConfig`` should delegate with the current CLI casing mode."""
    config = MagicMock()
    runtime_print = MagicMock()
    monkeypatch.setattr(main_module.cli_config_io, "print_config", runtime_print)
    monkeypatch.setattr(main_module.mt_config, "camel_case", True)

    main_module.printConfig(config)

    runtime_print.assert_called_once_with(config, camel_case=True, cli_print=print)


@pytest.mark.unit
def test_is_repeated_field_supports_legacy_descriptor_labels() -> None:
    """Legacy protobuf descriptors should fall back to label comparison."""
    repeated = SimpleNamespace(label=3, LABEL_REPEATED=3)
    singular = SimpleNamespace(label=1, LABEL_REPEATED=3)
    missing_label = SimpleNamespace(LABEL_REPEATED=3)

    for descriptor, expected in (
        (repeated, True),
        (singular, False),
        (missing_label, False),
    ):
        assert main_module.cli_config_io.is_repeated_field(descriptor) is expected
        assert main_module._is_repeated_field(descriptor) is expected
