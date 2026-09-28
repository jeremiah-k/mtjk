"""Regression tests for repeated-submessage ``--set`` preference values.

The generic ``--set`` path lives in :mod:`meshtastic.cli.preference_runtime`
and historically only understood scalar and repeated-scalar protobuf fields.
These tests pin the new repeated-submessage behaviour, which lets a user
populate fields such as ``module.mesh_beacon.broadcast_targets`` with a JSON
array literal in a single ``--set`` token.
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache
from typing import Any

import pytest
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

from meshtastic.__main__ import setPref
from meshtastic.protobuf import config_pb2, localonly_pb2, module_config_pb2


@pytest.fixture(autouse=True)
def _quiet_mt_config() -> Iterator[None]:
    """Force ``setPref`` to use the public CLI printer and bypass preflight."""
    # Direct ``setPref`` exercises the same diagnostic surface as ``--set``;
    # we deliberately avoid ``reset_mt_config`` because these tests don't
    # touch argparse state.
    import meshtastic.cli.preference_runtime as pr

    token = pr.CONFIGURE_PREFLIGHT_MODE.set(False)
    try:
        yield
    finally:
        pr.CONFIGURE_PREFLIGHT_MODE.reset(token)


@pytest.mark.unit
def test_json_array_builds_repeated_broadcast_targets() -> None:
    """A JSON array of objects populates ``broadcast_targets`` via ParseDict."""
    config = localonly_pb2.LocalModuleConfig()
    payload = (
        '[{"preset":"SHORT_FAST","region":"US","channel_index":1},'
        '{"preset":"MEDIUM_FAST","channel_index":2}]'
    )

    assert setPref(config, "mesh_beacon.broadcast_targets", payload) is True

    targets = config.mesh_beacon.broadcast_targets
    assert len(targets) == 2
    assert targets[0].preset == config_pb2.Config.LoRaConfig.ModemPreset.SHORT_FAST
    assert targets[0].region == config_pb2.Config.LoRaConfig.RegionCode.US
    assert targets[0].channel_index == 1
    assert targets[1].preset == config_pb2.Config.LoRaConfig.ModemPreset.MEDIUM_FAST
    assert targets[1].channel_index == 2
    # Defaults are applied for omitted fields, so region == 0 (UNSET) for the
    # second entry which did not specify one.
    assert targets[1].region == config_pb2.Config.LoRaConfig.RegionCode.Value("UNSET")


@pytest.mark.unit
def test_empty_array_clears_repeated_broadcast_targets() -> None:
    """An empty JSON array clears an already-populated repeated-submessage field."""
    config = localonly_pb2.LocalModuleConfig()
    seed = '[{"preset":"SHORT_FAST","channel_index":1}]'
    assert setPref(config, "mesh_beacon.broadcast_targets", seed) is True
    assert len(config.mesh_beacon.broadcast_targets) == 1

    assert setPref(config, "mesh_beacon.broadcast_targets", "[]") is True

    assert len(config.mesh_beacon.broadcast_targets) == 0


@pytest.mark.unit
@pytest.mark.parametrize(
    ("payload", "expected_substring"),
    (
        ("{not valid json", "Invalid value"),
        ('"just a string"', "Invalid value"),
        ('{"preset":"SHORT_FAST"}', "Invalid value"),
        ('["not an object"]', "is not an object/mapping"),
    ),
)
def test_malformed_json_uses_same_validation_surface(
    payload: str,
    expected_substring: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Bad JSON payloads report through the standard ``setPref`` diagnostic."""
    config = localonly_pb2.LocalModuleConfig()
    seed = '[{"preset":"SHORT_FAST","channel_index":7}]'
    assert setPref(config, "mesh_beacon.broadcast_targets", seed) is True
    before = config.SerializeToString()

    assert setPref(config, "mesh_beacon.broadcast_targets", payload) is False

    out, err = capsys.readouterr()
    combined = out + err
    assert expected_substring in combined
    # The repeated-submessage field must not be cleared or partially mutated.
    assert config.SerializeToString() == before


@pytest.mark.unit
def test_unknown_key_inside_element_reports_element_diagnostic(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unknown key inside an object surfaces a per-element diagnostic."""
    config = localonly_pb2.LocalModuleConfig()
    seed = '[{"preset":"SHORT_FAST","channel_index":7}]'
    assert setPref(config, "mesh_beacon.broadcast_targets", seed) is True
    before = config.SerializeToString()

    assert (
        setPref(
            config,
            "mesh_beacon.broadcast_targets",
            '[{"bogus":"foo"}]',
        )
        is False
    )

    out, err = capsys.readouterr()
    combined = out + err
    assert "element 0" in combined
    # Strict protobuf parsing must name the offending unknown key, not merely
    # identify an element index.
    assert "bogus" in combined
    assert config.SerializeToString() == before


@pytest.mark.unit
def test_repeated_scalar_field_accepts_comma_separated_string() -> None:
    """A comma-separated token for a repeated scalar field parses per-element."""
    config = localonly_pb2.LocalConfig()

    assert setPref(config, "lora.ignore_incoming", "11, 22, 33") is True

    assert list(config.lora.ignore_incoming) == [11, 22, 33]


@pytest.mark.unit
def test_repeated_scalar_ignores_empty_comma_elements() -> None:
    """Accidental duplicate separators do not synthesize invalid scalar values."""
    config = localonly_pb2.LocalConfig()

    assert setPref(config, "lora.ignore_incoming", "11,, 22, ,33") is True

    assert list(config.lora.ignore_incoming) == [11, 22, 33]


@pytest.mark.unit
def test_repeated_scalar_rejects_separator_only_value_without_clearing() -> None:
    """A malformed comma list with no values must not clear existing state."""
    config = localonly_pb2.LocalConfig()
    config.lora.ignore_incoming.extend([11, 22])
    before = config.SerializeToString()

    assert setPref(config, "lora.ignore_incoming", ", ,") is False

    assert config.SerializeToString() == before


@pytest.mark.unit
def test_repeated_scalar_list_path_still_works() -> None:
    """Passing a Python list to a repeated scalar field keeps historical semantics."""
    config = localonly_pb2.LocalConfig()

    assert setPref(config, "lora.ignore_incoming", ["4", "8"]) is True

    assert list(config.lora.ignore_incoming) == [4, 8]


@lru_cache(maxsize=1)
def _repeated_enum_config_class() -> type[Any]:
    """Build a dynamic message with one repeated enum field.

    No shipped Meshtastic protobuf declares a repeated enum field, so the
    batch path is exercised against a dynamically constructed descriptor.
    """
    file_proto = descriptor_pb2.FileDescriptorProto()
    file_proto.name = "mtjk_repeated_enum_test.proto"
    file_proto.package = "mtjk.test"
    palette = file_proto.message_type.add()
    palette.name = "Palette"
    color = palette.enum_type.add()
    color.name = "Color"
    for member in (("RED", 1), ("GREEN", 2)):
        enum_value = color.value.add()
        enum_value.name = member[0]
        enum_value.number = member[1]
    colors = palette.field.add()
    colors.name = "colors"
    colors.number = 1
    colors.label = colors.LABEL_REPEATED
    colors.type = colors.TYPE_ENUM
    colors.type_name = ".mtjk.test.Palette.Color"
    pool = descriptor_pool.DescriptorPool()
    classes = message_factory.GetMessages([file_proto], pool=pool)
    return classes["mtjk.test.Palette"]


@pytest.mark.unit
def test_repeated_enum_field_accepts_comma_separated_names() -> None:
    """A comma-separated enum name list resolves per token on repeated enums."""
    palette = _repeated_enum_config_class()()

    assert setPref(palette, "colors", "RED, GREEN") is True

    assert list(palette.colors) == [
        palette.Color.Value("RED"),
        palette.Color.Value("GREEN"),
    ]


@pytest.mark.unit
def test_repeated_enum_field_rejects_unknown_token_without_clearing() -> None:
    """An unknown token rejects the batch and leaves existing entries intact."""
    palette_class = _repeated_enum_config_class()
    palette = palette_class()
    palette.colors.extend([palette_class.Color.Value("RED")])
    before = palette.SerializeToString()

    assert setPref(palette, "colors", "RED,BOGUS") is False

    assert palette.SerializeToString() == before


@pytest.mark.unit
def test_repeated_enum_field_rejects_separator_only_value() -> None:
    """A separator-only value rejects without clearing existing entries."""
    palette_class = _repeated_enum_config_class()
    palette = palette_class()
    palette.colors.extend([palette_class.Color.Value("GREEN")])
    before = palette.SerializeToString()

    assert setPref(palette, "colors", ", ,") is False

    assert palette.SerializeToString() == before


@pytest.mark.unit
def test_repeated_message_enforces_declared_max_count(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Repeated message arrays reject values beyond the firmware max_count."""
    config = localonly_pb2.LocalModuleConfig()
    accepted = [{"preset": "SHORT_FAST", "channelIndex": index} for index in range(4)]
    rejected = accepted + [{"preset": "LONG_FAST", "channelIndex": 4}]

    assert setPref(config, "mesh_beacon.broadcast_targets", accepted) is True
    before = config.SerializeToString()
    assert setPref(config, "mesh_beacon.broadcast_targets", rejected) is False

    assert config.SerializeToString() == before
    out, err = capsys.readouterr()
    assert "5 entries exceeds the firmware limit of 4" in out
    assert err == ""


@pytest.mark.unit
def test_repeated_message_enforces_nested_string_size(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Nested message strings honor their own nanopb max_size before commit."""
    config = localonly_pb2.LocalModuleConfig()
    accepted = [{"gpioPin": 1, "name": "x" * 14, "type": "DIGITAL_READ"}]
    rejected = [{"gpioPin": 1, "name": "x" * 15, "type": "DIGITAL_READ"}]

    assert setPref(config, "remote_hardware.available_pins", accepted) is True
    before = config.SerializeToString()
    assert setPref(config, "remote_hardware.available_pins", rejected) is False

    assert config.SerializeToString() == before
    out, err = capsys.readouterr()
    assert "remote_hardware.available_pins[0].name" in out
    assert "encoded length 15 bytes exceeds the firmware limit of 14 bytes" in out
    assert err == ""


@pytest.mark.unit
def test_non_repeated_field_unchanged_by_repeated_message_support() -> None:
    """Setting a non-repeated field still routes through the scalar path."""
    config = localonly_pb2.LocalConfig()

    assert setPref(config, "lora.hop_limit", "5") is True

    assert config.lora.hop_limit == 5
    # Golden value for "the same field set with the historical contract".
    golden = localonly_pb2.LocalConfig()
    golden.lora.hop_limit = 5
    assert config.lora == golden.lora


@pytest.mark.unit
def test_broadcast_target_round_trip_against_real_protos() -> None:
    """Round-trip a BroadcastTarget list through the real ModuleConfig protobuf."""
    target = module_config_pb2.ModuleConfig.MeshBeaconConfig.BroadcastTarget
    preset_enum = config_pb2.Config.LoRaConfig.ModemPreset
    region_enum = config_pb2.Config.LoRaConfig.RegionCode
    payload = (
        "["
        '{"preset":"LONG_FAST","region":"US","channel_index":1},'
        '{"preset":"SHORT_FAST","region":"EU_433","channel_index":3}'
        "]"
    )

    config = localonly_pb2.LocalModuleConfig()

    assert setPref(config, "mesh_beacon.broadcast_targets", payload) is True

    bt = list(config.mesh_beacon.broadcast_targets)
    assert len(bt) == 2
    assert isinstance(bt[0], target)
    assert bt[0].preset == preset_enum.Value("LONG_FAST")

    assert bt[0].region == region_enum.US
    assert bt[0].channel_index == 1
    assert bt[1].preset == preset_enum.SHORT_FAST
    assert bt[1].region == region_enum.EU_433
    assert bt[1].channel_index == 3


@pytest.mark.unit
def test_repeated_message_accepts_configure_mapping_list() -> None:
    """YAML/DeviceProfile list-of-mapping values use the same strict parser."""
    config = localonly_pb2.LocalModuleConfig()
    payload = [
        {"preset": "SHORT_FAST", "region": "US", "channelIndex": 1},
        {"preset": "MEDIUM_FAST", "channelIndex": 2},
    ]

    assert setPref(config, "mesh_beacon.broadcast_targets", payload) is True

    targets = list(config.mesh_beacon.broadcast_targets)
    assert len(targets) == 2
    assert targets[0].preset == config_pb2.Config.LoRaConfig.ModemPreset.SHORT_FAST
    assert targets[0].region == config_pb2.Config.LoRaConfig.RegionCode.US
    assert targets[0].channel_index == 1
    assert targets[1].preset == config_pb2.Config.LoRaConfig.ModemPreset.MEDIUM_FAST
    assert targets[1].channel_index == 2


@pytest.mark.unit
def test_repeated_message_accepts_configure_mapping_tuple() -> None:
    """Tuple-of-mapping values take the same strict repeated-message path."""
    config = localonly_pb2.LocalModuleConfig()
    payload = (
        {"preset": "SHORT_FAST", "region": "US", "channelIndex": 1},
        {"preset": "MEDIUM_FAST", "channelIndex": 2},
    )

    assert setPref(config, "mesh_beacon.broadcast_targets", payload) is True

    targets = list(config.mesh_beacon.broadcast_targets)
    assert len(targets) == 2
    assert targets[0].preset == config_pb2.Config.LoRaConfig.ModemPreset.SHORT_FAST
    assert targets[0].region == config_pb2.Config.LoRaConfig.RegionCode.US
    assert targets[0].channel_index == 1
    assert targets[1].preset == config_pb2.Config.LoRaConfig.ModemPreset.MEDIUM_FAST
    assert targets[1].channel_index == 2


@pytest.mark.unit
def test_repeated_message_mapping_list_is_transactional_on_parse_failure() -> None:
    """An invalid YAML list element cannot partially replace existing messages."""
    config = localonly_pb2.LocalModuleConfig()
    assert (
        setPref(
            config,
            "mesh_beacon.broadcast_targets",
            '[{"preset":"SHORT_FAST","channel_index":7}]',
        )
        is True
    )
    before = config.SerializeToString()

    assert (
        setPref(
            config,
            "mesh_beacon.broadcast_targets",
            [{"preset": "LONG_FAST"}, {"notAField": 1}],
        )
        is False
    )

    assert config.SerializeToString() == before


@pytest.mark.unit
def test_repeated_message_parser_rejects_non_array_candidates_cleanly() -> None:
    """Internal parser guards distinguish scalar descriptors and non-array values."""
    from meshtastic.cli import preference_runtime

    scalar = localonly_pb2.LocalConfig().lora.DESCRIPTOR.fields_by_name["hop_limit"]
    repeated_message = (
        localonly_pb2.LocalModuleConfig().mesh_beacon.DESCRIPTOR.fields_by_name[
            "broadcast_targets"
        ]
    )

    def reporter(_message: str, **_kwargs: object) -> None:
        return None

    assert (
        preference_runtime._parse_repeated_message_value(
            scalar, [], field_path="lora.hop_limit", cli_print=reporter
        )
        is None
    )
    assert (
        preference_runtime._parse_repeated_message_value(
            repeated_message,
            7,
            field_path="mesh_beacon.broadcast_targets",
            cli_print=reporter,
        )
        is None
    )


@pytest.mark.unit
def test_repeated_message_parser_reports_bracketed_invalid_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A bracket-shaped malformed JSON array produces the precise JSON diagnostic."""
    config = localonly_pb2.LocalModuleConfig()

    assert setPref(config, "mesh_beacon.broadcast_targets", "[{bad}]") is False

    out, err = capsys.readouterr()
    assert "Invalid JSON value" in out + err


@pytest.mark.unit
def test_invalid_element_diagnostic_redacts_secret_and_avoids_nested_repr() -> None:
    """A non-mapping element is echoed without nested repr quoting, and a
    secret-bearing path never exposes the raw input in the diagnostic."""
    from meshtastic.cli import preference_runtime

    repeated_message = (
        localonly_pb2.LocalModuleConfig().mesh_beacon.DESCRIPTOR.fields_by_name[
            "broadcast_targets"
        ]
    )
    reported: list[str] = []

    def reporter(message: str, **_kwargs: object) -> None:
        reported.append(message)

    assert preference_runtime._parse_repeated_message_value(
        repeated_message,
        '["element"]',
        field_path="security.admin_key",
        cli_print=reporter,
    ) == (False, [])
    secret_message = reported[-1]
    assert "<redacted>" in secret_message
    assert '["element"]' not in secret_message

    assert preference_runtime._parse_repeated_message_value(
        repeated_message,
        '["element"]',
        field_path="mesh_beacon.broadcast_targets",
        cli_print=reporter,
    ) == (False, [])
    plain_message = reported[-1]
    assert '["element"] for mesh_beacon.broadcast_targets' in plain_message
    assert "'[\"element\"]'" not in plain_message


@pytest.mark.unit
def test_repeated_message_malformed_json_honors_fatal_policy() -> None:
    """Malformed array JSON raises under the fatal preference-value policy."""
    from meshtastic.cli import preference_runtime

    config = localonly_pb2.LocalModuleConfig()
    with (
        preference_runtime.fatal_preference_value_errors(),
        pytest.raises(
            preference_runtime.PreferenceValueError, match="Invalid JSON value"
        ),
    ):
        setPref(config, "mesh_beacon.broadcast_targets", "[{bad}]")


@pytest.mark.unit
def test_repeated_message_element_parse_error_honors_fatal_policy() -> None:
    """Invalid array elements raise under the fatal preference-value policy."""
    from meshtastic.cli import preference_runtime

    config = localonly_pb2.LocalModuleConfig()
    payload = '[{"preset":"NOT_A_PRESET","channel_index":1}]'
    with (
        preference_runtime.fatal_preference_value_errors(),
        pytest.raises(preference_runtime.PreferenceValueError, match="element 0"),
    ):
        setPref(config, "mesh_beacon.broadcast_targets", payload)


@pytest.mark.unit
def test_repeated_element_unencodable_utf8_reports_violation() -> None:
    """Repeated string elements with lone surrogates report a UTF-8 violation."""
    from meshtastic.cli.preference_runtime import _describe_repeated_limit_violation
    from meshtastic.protobuf import config_pb2
    from meshtastic.schema_metadata import _get_field_limits

    field = config_pb2.Config.NetworkConfig.DESCRIPTOR.fields_by_name["wifi_ssid"]
    limits = _get_field_limits(field)
    assert limits is not None

    violation = _describe_repeated_limit_violation(field, ["ab\ud800cd"], limits)

    assert violation == "element is not encodable as UTF-8"
