from __future__ import annotations

import base64
from unittest.mock import MagicMock

import pytest

from meshtastic.configure_verify import (
    _is_repeated_field,
    _verify_channel_url_against_state,
    _verify_channel_url_match,
    _verify_requested_fields,
)
from meshtastic.protobuf import apponly_pb2, channel_pb2, config_pb2, localonly_pb2


@pytest.mark.unit
@pytest.mark.parametrize("use_preset", [False, True])
@pytest.mark.parametrize("coding_rate", [0, 5, 6, 8])
def test_channel_url_verification_accepts_preset_derived_values(
    use_preset: bool, coding_rate: int
) -> None:
    """Preset-derived scalars can change during a device configuration round-trip."""
    requested = apponly_pb2.ChannelSet()
    requested.settings.add(name="test", psk=b"\x01")
    requested.lora_config.use_preset = use_preset
    requested.lora_config.hop_limit = 3
    device = apponly_pb2.ChannelSet()
    device.CopyFrom(requested)
    device.lora_config.spread_factor = 11
    device.lora_config.bandwidth = 250
    device.lora_config.coding_rate = coding_rate

    def _url(channel_set: apponly_pb2.ChannelSet) -> str:
        encoded = base64.urlsafe_b64encode(channel_set.SerializeToString()).decode()
        return f"https://meshtastic.org/e/#{encoded}"

    requested_bytes = requested.SerializeToString()
    device_bytes = device.SerializeToString()
    assert _verify_channel_url_match(_url(requested), _url(device)) is (
        use_preset and coding_rate in (0, 5)
    )
    assert requested.SerializeToString() == requested_bytes
    assert device.SerializeToString() == device_bytes


@pytest.mark.unit
@pytest.mark.parametrize("use_preset", [False, True])
@pytest.mark.parametrize("coding_rate", [0, 5, 6, 8])
def test_direct_lora_readback_accepts_only_preset_default_coding_rate(
    use_preset: bool, coding_rate: int
) -> None:
    """A zero preset coding rate can materialize as five without changing the radio."""
    lora = config_pb2.Config.LoRaConfig(use_preset=use_preset, coding_rate=coding_rate)
    mismatches = _verify_requested_fields({"codingRate": 0}, lora, "lora")
    expected_match = coding_rate == 0 or (use_preset and coding_rate == 5)
    assert mismatches == ([] if expected_match else ["lora.coding_rate"])


@pytest.mark.unit
def test_channel_url_verification_ignores_dormant_preset_in_custom_mode() -> None:
    """The preset enum is irrelevant while custom modem parameters are active."""
    requested = apponly_pb2.ChannelSet()
    requested.settings.add(name="test", psk=b"\x01")
    requested.lora_config.use_preset = False
    requested.lora_config.bandwidth = 125
    requested.lora_config.spread_factor = 7
    requested.lora_config.coding_rate = 5
    requested.lora_config.modem_preset = (
        config_pb2.Config.LoRaConfig.ModemPreset.LONG_FAST
    )
    device = apponly_pb2.ChannelSet()
    device.CopyFrom(requested)
    device.lora_config.modem_preset = (
        config_pb2.Config.LoRaConfig.ModemPreset.SHORT_FAST
    )

    def _url(channel_set: apponly_pb2.ChannelSet) -> str:
        encoded = base64.urlsafe_b64encode(channel_set.SerializeToString()).decode()
        return f"https://meshtastic.org/e/#{encoded}"

    requested_bytes = requested.SerializeToString()
    device_bytes = device.SerializeToString()
    assert _verify_channel_url_match(_url(requested), _url(device)) is True
    assert requested.SerializeToString() == requested_bytes
    assert device.SerializeToString() == device_bytes


def _make_channel_url(
    settings_list: list[channel_pb2.ChannelSettings],
    *,
    lora_region: str | None = None,
    lora_hop_limit: int | None = None,
) -> str:
    cs = apponly_pb2.ChannelSet()
    for s in settings_list:
        cs.settings.add().CopyFrom(s)
    if lora_region is not None:
        cs.lora_config.region = config_pb2.Config.LoRaConfig.RegionCode.Value(
            lora_region
        )
    if lora_hop_limit is not None:
        cs.lora_config.hop_limit = lora_hop_limit
    raw = cs.SerializeToString()
    b64 = base64.b64encode(raw, altchars=b"-_").decode().rstrip("=")
    return f"https://meshtastic.org/e/#{b64}"


@pytest.mark.unit
def test_verify_fields_exact_scalar_match() -> None:
    proto = localonly_pb2.LocalConfig()
    proto.lora.hop_limit = 3
    assert _verify_requested_fields({"hop_limit": 3}, proto.lora, "lora") == []


@pytest.mark.unit
def test_verify_fields_enum_string_match() -> None:
    proto = localonly_pb2.LocalConfig()
    proto.lora.region = config_pb2.Config.LoRaConfig.RegionCode.Value("US")
    assert _verify_requested_fields({"region": "US"}, proto.lora, "lora") == []


@pytest.mark.unit
def test_verify_fields_enum_mismatch() -> None:
    proto = localonly_pb2.LocalConfig()
    proto.lora.region = config_pb2.Config.LoRaConfig.RegionCode.Value("US")
    result = _verify_requested_fields({"region": "EU_868"}, proto.lora, "lora")
    assert result == ["lora.region"]


@pytest.mark.unit
def test_verify_fields_missing_descriptor() -> None:
    proto = localonly_pb2.LocalConfig()
    result = _verify_requested_fields({"nonexistent_field": 42}, proto.lora, "lora")
    assert result == ["lora.nonexistent_field"]


@pytest.mark.unit
def test_verify_fields_nested_dict() -> None:
    proto = localonly_pb2.LocalConfig()
    proto.device.serial_enabled = True
    assert (
        _verify_requested_fields({"device": {"serial_enabled": True}}, proto, "") == []
    )


@pytest.mark.unit
def test_verify_fields_bytes_match() -> None:
    proto = localonly_pb2.LocalConfig()
    proto.security.private_key = b"\x01\x02"
    assert (
        _verify_requested_fields(
            {"private_key": "base64:AQI="}, proto.security, "security"
        )
        == []
    )


@pytest.mark.unit
def test_verify_fields_value_mismatch() -> None:
    proto = localonly_pb2.LocalConfig()
    proto.lora.hop_limit = 3
    result = _verify_requested_fields({"hop_limit": 5}, proto.lora, "lora")
    assert result == ["lora.hop_limit"]


@pytest.mark.unit
def test_verify_fields_repeated_match() -> None:
    proto = localonly_pb2.LocalConfig()
    proto.security.admin_key.extend([b"\x01", b"\x02"])
    assert (
        _verify_requested_fields(
            {"admin_key": [b"\x01", b"\x02"]}, proto.security, "security"
        )
        == []
    )


@pytest.mark.unit
def test_verify_fields_bool_coercion() -> None:
    proto = localonly_pb2.LocalConfig()
    proto.bluetooth.enabled = True
    assert (
        _verify_requested_fields({"enabled": "true"}, proto.bluetooth, "bluetooth")
        == []
    )


@pytest.mark.unit
def test_verify_fields_neighbor_info_zero_requested_matches_default_device() -> None:
    proto = localonly_pb2.LocalModuleConfig()
    proto.neighbor_info.update_interval = 21600
    assert (
        _verify_requested_fields(
            {"update_interval": 0}, proto.neighbor_info, "neighbor_info"
        )
        == []
    )


@pytest.mark.unit
def test_verify_fields_neighbor_info_default_requested_matches_zero_device() -> None:
    proto = localonly_pb2.LocalModuleConfig()
    assert (
        _verify_requested_fields(
            {"update_interval": 21600}, proto.neighbor_info, "neighbor_info"
        )
        == []
    )


@pytest.mark.unit
def test_verify_fields_neighbor_info_camel_case_section_equivalence() -> None:
    proto = localonly_pb2.LocalModuleConfig()
    proto.neighbor_info.update_interval = 21600
    assert (
        _verify_requested_fields(
            {"updateInterval": 0}, proto.neighbor_info, "neighborInfo"
        )
        == []
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("requested", "actual"),
    [
        (0, 3600),
        (7200, 0),
        (21600, 43200),
        (43200, 21600),
    ],
)
def test_verify_fields_neighbor_info_non_equivalent_values_rejected(
    requested: int, actual: int
) -> None:
    proto = localonly_pb2.LocalModuleConfig()
    proto.neighbor_info.update_interval = actual
    assert _verify_requested_fields(
        {"update_interval": requested}, proto.neighbor_info, "neighbor_info"
    ) == ["neighbor_info.update_interval"]


@pytest.mark.unit
def test_verify_fields_neighbor_info_other_fields_still_compared() -> None:
    proto = localonly_pb2.LocalModuleConfig()
    proto.neighbor_info.update_interval = 21600
    proto.neighbor_info.transmit_over_lora = True
    result = _verify_requested_fields(
        {"update_interval": 0, "transmit_over_lora": False},
        proto.neighbor_info,
        "neighbor_info",
    )
    assert result == ["neighbor_info.transmit_over_lora"]


@pytest.mark.unit
def test_verify_fields_neighbor_info_bool_zero_not_equivalent() -> None:
    proto = localonly_pb2.LocalModuleConfig()
    proto.neighbor_info.update_interval = 21600
    result = _verify_requested_fields(
        {"update_interval": False}, proto.neighbor_info, "neighbor_info"
    )
    assert result == ["neighbor_info.update_interval"]


@pytest.mark.unit
def test_verify_fields_other_update_intervals_strict() -> None:
    proto = localonly_pb2.LocalModuleConfig()
    proto.telemetry.device_update_interval = 900
    result = _verify_requested_fields(
        {"device_update_interval": 0}, proto.telemetry, "telemetry"
    )
    assert result == ["telemetry.device_update_interval"]


@pytest.mark.unit
def test_channel_url_matching_urls() -> None:
    s = channel_pb2.ChannelSettings()
    s.name = "test"
    s.psk = b"\x01\x02\x03"
    url = _make_channel_url([s])
    assert _verify_channel_url_match(url, url) is True


@pytest.mark.unit
def test_channel_url_different_psk() -> None:
    s1 = channel_pb2.ChannelSettings()
    s1.name = "test"
    s1.psk = b"\x01\x02\x03"
    s2 = channel_pb2.ChannelSettings()
    s2.name = "test"
    s2.psk = b"\xff\xff\xff"
    assert (
        _verify_channel_url_match(_make_channel_url([s1]), _make_channel_url([s2]))
        is False
    )


@pytest.mark.unit
def test_channel_url_device_has_extra_channels() -> None:
    s1 = channel_pb2.ChannelSettings()
    s1.name = "test"
    s1.psk = b"\x01\x02\x03"
    s2 = channel_pb2.ChannelSettings()
    s2.name = "extra"
    s2.psk = b"\xaa\xbb"
    assert (
        _verify_channel_url_match(_make_channel_url([s1]), _make_channel_url([s1, s2]))
        is False
    )


@pytest.mark.unit
def test_channel_url_missing_channel_in_device() -> None:
    s1 = channel_pb2.ChannelSettings()
    s1.name = "test"
    s1.psk = b"\x01\x02\x03"
    s2 = channel_pb2.ChannelSettings()
    s2.name = "extra"
    s2.psk = b"\xaa\xbb"
    assert (
        _verify_channel_url_match(_make_channel_url([s1, s2]), _make_channel_url([s1]))
        is False
    )


@pytest.mark.unit
def test_channel_url_lora_presence_mismatch() -> None:
    s = channel_pb2.ChannelSettings()
    s.name = "test"
    s.psk = b"\x01\x02\x03"
    assert (
        _verify_channel_url_match(
            _make_channel_url([s], lora_region="US"),
            _make_channel_url([s]),
        )
        is False
    )


@pytest.mark.unit
def test_channel_url_lora_config_mismatch() -> None:
    s = channel_pb2.ChannelSettings()
    s.name = "test"
    s.psk = b"\x01\x02\x03"
    assert (
        _verify_channel_url_match(
            _make_channel_url([s], lora_region="US", lora_hop_limit=3),
            _make_channel_url([s], lora_region="US", lora_hop_limit=5),
        )
        is False
    )


@pytest.mark.unit
def test_channel_url_primary_entry_order_mismatch_fails() -> None:
    requested_primary = channel_pb2.ChannelSettings()
    requested_primary.name = "primary"
    requested_primary.psk = b"\x01\x01"
    requested_secondary = channel_pb2.ChannelSettings()
    requested_secondary.name = "secondary"
    requested_secondary.psk = b"\x02\x02"
    device_primary = channel_pb2.ChannelSettings()
    device_primary.CopyFrom(requested_secondary)
    device_secondary = channel_pb2.ChannelSettings()
    device_secondary.CopyFrom(requested_primary)
    assert (
        _verify_channel_url_match(
            _make_channel_url(
                [requested_primary, requested_secondary], lora_region="US"
            ),
            _make_channel_url([device_primary, device_secondary], lora_region="US"),
        )
        is False
    )


@pytest.mark.unit
def test_channel_url_against_state_match() -> None:
    primary_settings = channel_pb2.ChannelSettings()
    primary_settings.name = "primary"
    primary_settings.psk = b"\x01\x02"
    secondary_settings = channel_pb2.ChannelSettings()
    secondary_settings.name = "secondary"
    secondary_settings.psk = b"\x03\x04"
    requested_url = _make_channel_url(
        [primary_settings, secondary_settings],
        lora_region="US",
        lora_hop_limit=5,
    )

    primary_channel = channel_pb2.Channel()
    primary_channel.role = channel_pb2.Channel.Role.PRIMARY
    primary_channel.settings.CopyFrom(primary_settings)
    secondary_channel = channel_pb2.Channel()
    secondary_channel.role = channel_pb2.Channel.Role.SECONDARY
    secondary_channel.settings.CopyFrom(secondary_settings)

    local_config = localonly_pb2.LocalConfig()
    local_config.lora.region = config_pb2.Config.LoRaConfig.RegionCode.Value("US")
    local_config.lora.hop_limit = 5

    assert (
        _verify_channel_url_against_state(
            requested_url,
            device_channels=[primary_channel, secondary_channel],
            device_lora_config=local_config.lora,
        )
        is True
    )


@pytest.mark.unit
def test_channel_url_against_state_missing_lora_is_false() -> None:
    primary_settings = channel_pb2.ChannelSettings()
    primary_settings.name = "primary"
    primary_settings.psk = b"\x01\x02"
    requested_url = _make_channel_url([primary_settings], lora_region="US")

    primary_channel = channel_pb2.Channel()
    primary_channel.role = channel_pb2.Channel.Role.PRIMARY
    primary_channel.settings.CopyFrom(primary_settings)

    assert (
        _verify_channel_url_against_state(
            requested_url,
            device_channels=[primary_channel],
            device_lora_config=None,
        )
        is False
    )


@pytest.mark.unit
def test_verify_fields_string_literal_true_stays_string() -> None:
    telemetry = localonly_pb2.LocalModuleConfig()
    telemetry.detection_sensor.name = "true"
    assert (
        _verify_requested_fields(
            {"name": "true"}, telemetry.detection_sensor, "detection_sensor"
        )
        == []
    )


@pytest.mark.unit
def test_is_repeated_field_with_is_repeated_true() -> None:
    fd = MagicMock()
    fd.is_repeated = True
    assert _is_repeated_field(fd) is True


@pytest.mark.unit
def test_is_repeated_field_with_is_repeated_false() -> None:
    fd = MagicMock()
    fd.is_repeated = False
    assert _is_repeated_field(fd) is False


@pytest.mark.unit
def test_is_repeated_field_label_fallback() -> None:
    fd = MagicMock(spec=[])
    del fd.is_repeated
    fd.label = 3
    fd.LABEL_REPEATED = 3
    assert _is_repeated_field(fd) is True


@pytest.mark.unit
def test_is_repeated_field_label_fallback_non_repeated() -> None:
    fd = MagicMock(spec=[])
    del fd.is_repeated
    fd.label = 1
    fd.LABEL_REPEATED = 3
    assert _is_repeated_field(fd) is False


@pytest.mark.unit
def test_repeated_enum_list_coercion() -> None:
    enum_val_mock = MagicMock()
    enum_val_mock.number = 1

    second_enum_val_mock = MagicMock()
    second_enum_val_mock.number = 2

    enum_type_mock = MagicMock()
    enum_type_mock.values_by_name = {
        "FIXED": enum_val_mock,
        "RANDOM": second_enum_val_mock,
    }

    field_desc = MagicMock()
    field_desc.is_repeated = True
    field_desc.enum_type = enum_type_mock

    sub_msg = MagicMock()
    sub_msg.DESCRIPTOR.fields_by_name = {"my_field": field_desc}
    sub_msg.my_field = [1, 2]

    result = _verify_requested_fields(
        {"my_field": ["FIXED", "RANDOM"]}, sub_msg, "test"
    )
    assert result == []


@pytest.mark.unit
def test_repeated_enum_list_mismatch() -> None:
    enum_val_mock = MagicMock()
    enum_val_mock.number = 1

    enum_type_mock = MagicMock()
    enum_type_mock.values_by_name = {"FIXED": enum_val_mock}

    field_desc = MagicMock()
    field_desc.is_repeated = True
    field_desc.enum_type = enum_type_mock

    sub_msg = MagicMock()
    sub_msg.DESCRIPTOR.fields_by_name = {"my_field": field_desc}
    sub_msg.my_field = [1, 2]

    result = _verify_requested_fields({"my_field": ["FIXED"]}, sub_msg, "test")
    assert result == ["test.my_field"]


@pytest.mark.unit
def test_repeated_non_enum_string_coercion() -> None:
    proto = localonly_pb2.LocalConfig()
    proto.security.admin_key.extend([b"\x01", b"\x02"])
    result = _verify_requested_fields(
        {"admin_key": ["0x01", "0x02"]}, proto.security, "security"
    )
    assert result == []


@pytest.mark.unit
def test_channel_url_invalid_requested_url() -> None:
    s = channel_pb2.ChannelSettings()
    s.name = "test"
    s.psk = b"\x01"
    assert _verify_channel_url_match("not-a-valid-url", _make_channel_url([s])) is False


@pytest.mark.unit
def test_channel_url_invalid_device_url() -> None:
    s = channel_pb2.ChannelSettings()
    s.name = "test"
    s.psk = b"\x01"
    assert _verify_channel_url_match(_make_channel_url([s]), "garbage") is False


@pytest.mark.unit
def test_verify_fields_repeated_scalar_coerced_to_list() -> None:
    proto = localonly_pb2.LocalConfig()
    proto.security.admin_key.append(b"\x01")
    result = _verify_requested_fields(
        {"admin_key": b"\x01"}, proto.security, "security"
    )
    assert result == []


@pytest.mark.unit
def test_verify_fields_repeated_list_order_mismatch() -> None:
    proto = localonly_pb2.LocalConfig()
    proto.security.admin_key.extend([b"\x01", b"\x02"])
    result = _verify_requested_fields(
        {"admin_key": [b"\x02", b"\x01"]}, proto.security, "security"
    )
    assert result == ["security.admin_key"]


@pytest.mark.unit
def test_verify_fields_non_repeated_given_list_uses_first() -> None:
    proto = localonly_pb2.LocalConfig()
    proto.lora.hop_limit = 3
    result = _verify_requested_fields({"hop_limit": [3, 5]}, proto.lora, "lora")
    assert result == []


@pytest.mark.unit
def test_verify_fields_non_repeated_given_wrong_list() -> None:
    proto = localonly_pb2.LocalConfig()
    proto.lora.hop_limit = 3
    result = _verify_requested_fields({"hop_limit": [5]}, proto.lora, "lora")
    assert result == ["lora.hop_limit"]


@pytest.mark.unit
def test_verify_fields_invalid_enum_name_treated_as_mismatch() -> None:
    proto = localonly_pb2.LocalConfig()
    proto.lora.region = config_pb2.Config.LoRaConfig.RegionCode.Value("US")
    result = _verify_requested_fields({"region": "INVALID_REGION"}, proto.lora, "lora")
    assert result == ["lora.region"]


@pytest.mark.unit
def test_channel_url_name_mismatch() -> None:
    s1 = channel_pb2.ChannelSettings()
    s1.name = "alpha"
    s1.psk = b"\x01\x02\x03"
    s2 = channel_pb2.ChannelSettings()
    s2.name = "alpha"
    s2.psk = b"\x01\x02\x03"
    s2.uplink_enabled = False
    s3 = channel_pb2.ChannelSettings()
    s3.name = "alpha"
    s3.psk = b"\x01\x02\x03"
    s3.uplink_enabled = True
    assert (
        _verify_channel_url_match(_make_channel_url([s1, s2]), _make_channel_url([s1]))
        is False
    )


@pytest.mark.unit
def test_channel_url_uplink_mismatch() -> None:
    s1 = channel_pb2.ChannelSettings()
    s1.name = "test"
    s1.psk = b"\x01"
    s1.uplink_enabled = True
    s2 = channel_pb2.ChannelSettings()
    s2.name = "test"
    s2.psk = b"\x01"
    s2.uplink_enabled = False
    assert (
        _verify_channel_url_match(_make_channel_url([s1]), _make_channel_url([s2]))
        is False
    )


@pytest.mark.unit
def test_channel_url_downlink_mismatch() -> None:
    s1 = channel_pb2.ChannelSettings()
    s1.name = "test"
    s1.psk = b"\x01"
    s1.downlink_enabled = True
    s2 = channel_pb2.ChannelSettings()
    s2.name = "test"
    s2.psk = b"\x01"
    s2.downlink_enabled = False
    assert (
        _verify_channel_url_match(_make_channel_url([s1]), _make_channel_url([s2]))
        is False
    )


@pytest.mark.unit
def test_channel_url_id_mismatch() -> None:
    s1 = channel_pb2.ChannelSettings()
    s1.name = "test"
    s1.psk = b"\x01"
    s1.id = 12345
    s2 = channel_pb2.ChannelSettings()
    s2.name = "test"
    s2.psk = b"\x01"
    s2.id = 99999
    assert (
        _verify_channel_url_match(_make_channel_url([s1]), _make_channel_url([s2]))
        is False
    )


@pytest.mark.unit
def test_channel_url_module_settings_mismatch() -> None:
    s1 = channel_pb2.ChannelSettings()
    s1.name = "test"
    s1.psk = b"\x01"
    s1.module_settings.position_precision = 10
    s2 = channel_pb2.ChannelSettings()
    s2.name = "test"
    s2.psk = b"\x01"
    s2.module_settings.position_precision = 5
    assert (
        _verify_channel_url_match(_make_channel_url([s1]), _make_channel_url([s2]))
        is False
    )


@pytest.mark.unit
def test_channel_url_module_settings_is_muted_mismatch() -> None:
    s1 = channel_pb2.ChannelSettings()
    s1.name = "test"
    s1.psk = b"\x01"
    s1.module_settings.is_muted = True
    s2 = channel_pb2.ChannelSettings()
    s2.name = "test"
    s2.psk = b"\x01"
    s2.module_settings.is_muted = False
    assert (
        _verify_channel_url_match(_make_channel_url([s1]), _make_channel_url([s2]))
        is False
    )


@pytest.mark.unit
def test_channel_url_all_fields_match() -> None:
    s1 = channel_pb2.ChannelSettings()
    s1.name = "full"
    s1.psk = b"\xaa\xbb\xcc"
    s1.id = 42
    s1.uplink_enabled = True
    s1.downlink_enabled = False
    s1.module_settings.position_precision = 8
    s1.module_settings.is_muted = True
    s2 = channel_pb2.ChannelSettings()
    s2.name = "full"
    s2.psk = b"\xaa\xbb\xcc"
    s2.id = 42
    s2.uplink_enabled = True
    s2.downlink_enabled = False
    s2.module_settings.position_precision = 8
    s2.module_settings.is_muted = True
    assert (
        _verify_channel_url_match(_make_channel_url([s1]), _make_channel_url([s2]))
        is True
    )


@pytest.mark.unit
def test_channel_url_duplicate_requested_names_returns_false() -> None:
    s1 = channel_pb2.ChannelSettings()
    s1.name = "dup"
    s1.psk = b"\x01"
    s2 = channel_pb2.ChannelSettings()
    s2.name = "dup"
    s2.psk = b"\x02"
    s3 = channel_pb2.ChannelSettings()
    s3.name = "dup"
    s3.psk = b"\x01"
    assert (
        _verify_channel_url_match(_make_channel_url([s1, s2]), _make_channel_url([s1]))
        is False
    )


@pytest.mark.unit
def test_channel_url_duplicate_device_names_returns_false() -> None:
    s1 = channel_pb2.ChannelSettings()
    s1.name = "dup"
    s1.psk = b"\x01"
    s2 = channel_pb2.ChannelSettings()
    s2.name = "dup"
    s2.psk = b"\x02"
    assert (
        _verify_channel_url_match(_make_channel_url([s1]), _make_channel_url([s1, s2]))
        is False
    )


@pytest.mark.unit
def test_channel_url_duplicate_names_both_sides_returns_false() -> None:
    s1 = channel_pb2.ChannelSettings()
    s1.name = "same"
    s1.psk = b"\x01"
    s2 = channel_pb2.ChannelSettings()
    s2.name = "same"
    s2.psk = b"\x01"
    assert (
        _verify_channel_url_match(
            _make_channel_url([s1, s2]), _make_channel_url([s1, s2])
        )
        is False
    )


@pytest.mark.unit
def test_channel_url_no_duplicates_still_works() -> None:
    s1 = channel_pb2.ChannelSettings()
    s1.name = "alpha"
    s1.psk = b"\x01"
    s2 = channel_pb2.ChannelSettings()
    s2.name = "beta"
    s2.psk = b"\x02"
    s3 = channel_pb2.ChannelSettings()
    s3.name = "alpha"
    s3.psk = b"\x01"
    s4 = channel_pb2.ChannelSettings()
    s4.name = "beta"
    s4.psk = b"\x02"
    assert (
        _verify_channel_url_match(
            _make_channel_url([s1, s2]), _make_channel_url([s3, s4])
        )
        is True
    )


@pytest.mark.unit
def test_channel_url_duplicate_requested_with_mismatch_settings_returns_false() -> None:
    s1 = channel_pb2.ChannelSettings()
    s1.name = "dup"
    s1.psk = b"\x01"
    s2 = channel_pb2.ChannelSettings()
    s2.name = "dup"
    s2.psk = b"\xff"
    s3 = channel_pb2.ChannelSettings()
    s3.name = "dup"
    s3.psk = b"\x01"
    assert (
        _verify_channel_url_match(_make_channel_url([s1, s2]), _make_channel_url([s1]))
        is False
    )
