"""Meshtastic unit tests for mesh_interface_runtime/node_summary.py."""

# pylint: disable=redefined-outer-name,protected-access

import sys
import threading
from typing import cast
from unittest.mock import MagicMock

import pytest

from meshtastic.mesh_interface import MeshInterface
from meshtastic.mesh_interface_runtime.node_summary import (
    _format_battery_level,
    _format_channels_section,
    _format_duration,
    _format_mesh_section,
    _format_my_node_section,
    _format_radio_section,
    _format_summary,
)
from meshtastic.mesh_interface_runtime.node_view import NodeView
from meshtastic.mesh_interface_runtime.ports import _NodeViewPort
from meshtastic.protobuf import channel_pb2, config_pb2, mesh_pb2
from meshtastic.protobuf.localonly_pb2 import LocalConfig


class _FakeLocalNode:
    """Minimal stand-in for the local Node carrying config and channels."""

    def __init__(
        self,
        node_num: int = 12345,
        local_config: LocalConfig | None = None,
        channels: list[channel_pb2.Channel] | None = None,
    ) -> None:
        self.nodeNum = node_num
        self.localConfig = local_config if local_config is not None else LocalConfig()
        self.channels = channels


def _metadata(
    firmware_version: str = "", ble_address: str = ""
) -> mesh_pb2.DeviceMetadata:
    """Build a DeviceMetadata with the given firmware version and BLE address."""
    metadata = mesh_pb2.DeviceMetadata()
    metadata.firmware_version = firmware_version
    metadata.ble_address = ble_address
    return metadata


def _local_record(battery: int | None = 84) -> dict[str, object]:
    """Build a local node database record with optional device metrics."""
    record: dict[str, object] = {
        "num": 12345,
        "user": {
            "longName": "Jeremiah's T-Deck",
            "shortName": "JK",
            "hwModel": "TDECK",
            "role": "CLIENT",
        },
    }
    if battery is not None:
        record["deviceMetrics"] = {"batteryLevel": battery}
    return record


class TestFormatDuration:
    """Tests for _format_duration()."""

    @pytest.mark.unit
    def test_duration_zero_and_negative(self) -> None:
        """Zero and negative durations render as '0 secs'."""
        assert _format_duration(0) == "0 secs"
        assert _format_duration(-3) == "0 secs"

    @pytest.mark.unit
    def test_duration_units(self) -> None:
        """Durations render compact unit strings without an 'ago' suffix."""
        assert _format_duration(45) == "45 secs"
        assert _format_duration(60) == "1 min"
        assert _format_duration(120) == "2 mins"
        assert _format_duration(3600) == "1 hour"
        assert _format_duration(112320) == "1 day"
        assert _format_duration(172800) == "2 days"


class TestFormatBatteryLevel:
    """Tests for _format_battery_level()."""

    @pytest.mark.unit
    def test_battery_missing(self) -> None:
        """No battery level yields None."""
        assert _format_battery_level({}) is None

    @pytest.mark.unit
    def test_battery_powered_sentinels(self) -> None:
        """Levels 0 and 101 render as Powered."""
        assert _format_battery_level({"batteryLevel": 0}) == "Powered"
        assert _format_battery_level({"batteryLevel": 101}) == "Powered"

    @pytest.mark.unit
    def test_battery_percent_with_voltage(self) -> None:
        """Percent rendering includes voltage when known."""
        metrics = {"batteryLevel": 84, "voltage": 4.024}
        assert _format_battery_level(metrics) == "84% (4.02 V)"

    @pytest.mark.unit
    def test_battery_percent_without_voltage(self) -> None:
        """Percent rendering omits voltage when unknown."""
        assert _format_battery_level({"batteryLevel": 41}) == "41%"


class TestMyNodeSection:
    """Tests for _format_my_node_section()."""

    @pytest.mark.unit
    def test_full_section(self) -> None:
        """All known rows render, including the BLE address."""
        section = _format_my_node_section(
            local_num=12345,
            long_name="Jeremiah's T-Deck",
            short_name="JK",
            my_record=_local_record(),
            metadata=_metadata("2.8.18.abc", "AA:BB:CC:DD:EE:FF"),
        )
        assert section.startswith("My Node\n")
        assert "Jeremiah's T-Deck (JK)" in section
        assert "!00003039" in section
        assert "2.8.18.abc" in section
        assert "TDECK" in section
        assert "CLIENT" in section
        assert "84%" in section
        assert "AA:BB:CC:DD:EE:FF" in section

    @pytest.mark.unit
    def test_ble_row_omitted_when_empty(self) -> None:
        """An empty ble_address omits the row entirely."""
        section = _format_my_node_section(
            local_num=12345,
            long_name="Bob",
            short_name=None,
            my_record=_local_record(),
            metadata=_metadata("2.8.18", ""),
        )
        assert "BLE address" not in section
        assert "Bob" in section

    @pytest.mark.unit
    def test_no_metadata_and_unset_hardware(self) -> None:
        """No metadata drops firmware, and UNSET hardware drops the row."""
        record = _local_record()
        record["user"] = {"longName": "Bob", "hwModel": "UNSET"}
        section = _format_my_node_section(
            local_num=None,
            long_name="Bob",
            short_name=None,
            my_record=record,
            metadata=None,
        )
        assert "Firmware" not in section
        assert "Hardware" not in section
        assert "!" not in section

    @pytest.mark.unit
    def test_uptime_rendered(self) -> None:
        """Uptime seconds render through _format_duration."""
        record = _local_record()
        record["deviceMetrics"] = {"batteryLevel": 84, "uptimeSeconds": 7200}
        section = _format_my_node_section(
            local_num=12345,
            long_name="Bob",
            short_name=None,
            my_record=record,
            metadata=None,
        )
        assert "2 hours" in section


class TestRadioSection:
    """Tests for _format_radio_section()."""

    @pytest.mark.unit
    def test_full_section(self) -> None:
        """Region through utilization rows render from config and metrics."""
        config = LocalConfig()
        config.lora.region = config_pb2.Config.LoRaConfig.RegionCode.US
        config.lora.modem_preset = config_pb2.Config.LoRaConfig.ModemPreset.LONG_FAST
        config.lora.hop_limit = 3
        config.lora.tx_power = 30
        node = _FakeLocalNode(local_config=config)
        record = _local_record()
        record["deviceMetrics"] = {
            "batteryLevel": 84,
            "channelUtilization": 21.4,
            "airUtilTx": 4.1,
        }
        section = _format_radio_section(local_node=node, my_record=record)
        assert section.startswith("Radio\n")
        assert "US" in section
        assert "LONG_FAST" in section
        assert "3" in section
        assert "30 dBm" in section
        assert "21.40%" in section
        assert "4.10%" in section

    @pytest.mark.unit
    def test_skipped_without_lora_config(self) -> None:
        """No received LoRa config yields an empty section."""
        section = _format_radio_section(local_node=_FakeLocalNode(), my_record=None)
        assert section == ""

    @pytest.mark.unit
    def test_unset_region_and_tx_power_omitted(self) -> None:
        """Zero region and zero tx power rows are omitted."""
        config = LocalConfig()
        config.lora.modem_preset = config_pb2.Config.LoRaConfig.ModemPreset.MEDIUM_SLOW
        section = _format_radio_section(
            local_node=_FakeLocalNode(local_config=config), my_record=None
        )
        assert "Region" not in section
        assert "TX power" not in section
        assert "MEDIUM_SLOW" in section


class TestMeshSection:
    """Tests for _format_mesh_section()."""

    @pytest.mark.unit
    def test_empty_mesh(self) -> None:
        """An empty node database renders the header only."""
        assert _format_mesh_section([]) == "Mesh: 0 known"

    @pytest.mark.unit
    def test_counts_and_truncation(self) -> None:
        """Header counts include direct peers and the footer hides extras."""
        records = [
            {
                "num": index,
                "user": {"longName": f"node{index}"},
                "hopsAway": 0 if index < 3 else 1,
                "lastHeard": 1000 - index,
            }
            for index in range(7)
        ]
        section = _format_mesh_section(records)
        assert section.startswith("Mesh: 7 known, 3 direct")
        assert "node0" in section
        assert "node6" not in section
        assert "… and 2 more not shown" in section

    @pytest.mark.unit
    def test_no_footer_when_all_shown(self) -> None:
        """Fewer nodes than the limit omit the truncation footer."""
        records = [
            {"num": 1, "user": {"longName": "solo"}, "lastHeard": 10},
        ]
        section = _format_mesh_section(records)
        assert "more not shown" not in section
        assert "solo" in section


class TestChannelsSection:
    """Tests for _format_channels_section()."""

    @pytest.mark.unit
    def test_full_section(self) -> None:
        """Active channels render with role and PSK labels."""
        primary = channel_pb2.Channel(index=0, role=channel_pb2.Channel.Role.PRIMARY)
        primary.settings.name = "primary"
        primary.settings.psk = b"\x01" * 32
        secondary = channel_pb2.Channel(
            index=1, role=channel_pb2.Channel.Role.SECONDARY
        )
        disabled = channel_pb2.Channel(index=2, role=channel_pb2.Channel.Role.DISABLED)
        disabled.settings.name = "ghost"
        node = _FakeLocalNode(channels=[primary, secondary, disabled])
        section = _format_channels_section(node)
        assert section.startswith("Channels\n")
        assert "primary" in section
        assert "PRIMARY" in section
        assert "secret" in section
        assert "(unnamed)" in section
        assert "unencrypted" in section
        assert "ghost" not in section

    @pytest.mark.unit
    def test_no_channels(self) -> None:
        """None or empty channel lists yield an empty section."""
        assert _format_channels_section(_FakeLocalNode(channels=None)) == ""
        assert _format_channels_section(_FakeLocalNode(channels=[])) == ""


class TestFormatSummary:
    """Tests for _format_summary() composition."""

    @pytest.mark.unit
    def test_sections_joined_with_blank_lines(self) -> None:
        """Populated sections appear in order separated by blank lines."""
        config = LocalConfig()
        config.lora.region = config_pb2.Config.LoRaConfig.RegionCode.US
        config.lora.modem_preset = config_pb2.Config.LoRaConfig.ModemPreset.LONG_FAST
        node = _FakeLocalNode(local_config=config)
        summary = _format_summary(
            local_node=node,
            local_num=12345,
            long_name="Jeremiah's T-Deck",
            short_name="JK",
            node_records=[_local_record()],
            metadata=_metadata("2.8.18.abc", "AA:BB:CC:DD:EE:FF"),
        )
        assert summary.index("My Node") < summary.index("Radio")
        assert summary.index("Radio") < summary.index("Mesh: 1 known")
        assert "\n\n" in summary

    @pytest.mark.unit
    def test_minimal_state(self) -> None:
        """Missing optional state drops sections without crashing."""
        summary = _format_summary(
            local_node=_FakeLocalNode(),
            local_num=42,
            long_name=None,
            short_name=None,
            node_records=[],
            metadata=None,
        )
        assert summary.startswith("My Node\n")
        assert "Mesh: 0 known" in summary
        assert "Radio" not in summary
        assert "Channels" not in summary


class TestNodeViewShowSummary:
    """Tests for NodeView.show_summary() state snapshotting and output."""

    @pytest.fixture
    def summary_view(self) -> NodeView:
        """Create a NodeView over a mock interface with summary-ready state."""
        interface = MagicMock(spec=MeshInterface)
        interface._node_db_lock = threading.RLock()
        interface.debugOut = sys.stdout
        interface.metadata = None
        my_info = mesh_pb2.MyNodeInfo()
        my_info.my_node_num = 12345
        interface.myInfo = my_info
        interface.nodesByNum = {12345: _local_record()}
        interface.nodes = {}
        interface.localNode = _FakeLocalNode()
        return NodeView(_NodeViewPort(interface))

    @pytest.mark.unit
    def test_show_summary_prints_and_returns(
        self, summary_view: NodeView, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """show_summary prints the formatted summary and returns it."""
        output = summary_view.show_summary()
        captured = capsys.readouterr().out
        assert captured == output + "\n"
        assert "My Node" in output
        assert "!00003039" in output
        assert "Mesh: 1 known" in output

    @pytest.mark.unit
    def test_show_summary_without_my_info_uses_local_node_num(
        self, summary_view: NodeView, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A missing MyNodeInfo falls back to the local node number."""
        mock_interface = cast(MagicMock, summary_view._port._interface)  # noqa: SLF001
        mock_interface.myInfo = None
        mock_interface.localNode = _FakeLocalNode(node_num=999)
        output = summary_view.show_summary()
        capsys.readouterr()
        assert "!000003e7" in output
