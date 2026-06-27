"""Meshtastic unit tests for traffic management handling in mesh_interface.py."""

import pytest

from ..mesh_interface import MeshInterface
from ..protobuf import mesh_pb2


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handle_from_radio_with_traffic_management_module_config() -> None:
    """Test _handle_from_radio with moduleConfig.traffic_management.

    The bool toggles (``enabled``, ``rate_limit_enabled``, ...) were removed
    from the protobuf in favour of the "non-zero implies enabled" convention
    on their companion uint32 fields, so we exercise that convention here.
    """
    iface = MeshInterface(noProto=True)
    try:
        from_radio = mesh_pb2.FromRadio()
        tm = from_radio.moduleConfig.traffic_management
        tm.position_min_interval_secs = 30
        tm.rate_limit_window_secs = 60
        tm.rate_limit_max_packets = 100

        iface._handle_from_radio(from_radio.SerializeToString())

        result = iface.localNode.moduleConfig.traffic_management
        assert result.position_min_interval_secs == 30
        assert result.rate_limit_window_secs == 60
        assert result.rate_limit_max_packets == 100
    finally:
        iface.close()
