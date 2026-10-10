"""Exercise the public node query contract and its JSON CLI consumer."""

import argparse
import json
import threading
from dataclasses import replace
from unittest.mock import patch

import pytest

from meshtastic import __main__ as cli_main
from meshtastic.cli.context import ActionOutcome, CliContext
from meshtastic.cli.dispatch import _dispatch_connected
from meshtastic.cli.invocation import CliInvocation, activate_invocation
from meshtastic.mesh_interface import MeshInterface
from meshtastic.mesh_interface_runtime import node_data
from meshtastic.protobuf import mesh_pb2

pytestmark = pytest.mark.unit


@pytest.fixture
def interface():
    """Build a client cache with local, matching, and incomplete observations."""
    client = MeshInterface(noProto=True)
    client.localNode.nodeNum = 1
    client.nodesByNum = {
        1: {"num": 1, "user": {"longName": "Relay", "role": "CLIENT"}, "lastHeard": 10},
        2: {
            "num": 2,
            "user": {"longName": "Bravo", "role": "CLIENT", "hwModel": "RAK"},
            "lastHeard": 20,
            "deviceMetrics": {"batteryLevel": 101},
            "snr": -5.5,
        },
        3: {
            "num": 3,
            "user": {"longName": "Alpha", "role": "ROUTER", "hwModel": "RAK"},
            "lastHeard": 30,
        },
        4: {"num": 4},
    }
    yield client
    client.close()


def test_query_returns_raw_records_counts_and_capture_time(interface, capsys):
    """Apply selection, filtering, sorting and limiting without display effects."""
    result = interface.queryNodes(
        includeSelf=False, hwModelFilter=("rak",), sortField="name", limit=1
    )
    assert (result.total, result.matched, result.returned, result.truncated) == (
        3,
        2,
        1,
        True,
    )
    assert result.nodes[0]["user"]["longName"] == "Alpha"
    assert result.nodes[0]["lastHeard"] == 30
    assert result.capturedAt > 0
    assert capsys.readouterr() == ("", "")


def test_query_combines_filters_and_preserves_numeric_measurements(interface):
    """Select matching roles and hardware without table formatting."""
    result = interface.queryNodes(roleFilter=["client"], hwModelFilter=["rak"])
    assert (result.total, result.matched, result.returned) == (4, 1, 1)
    assert result.nodes[0]["deviceMetrics"]["batteryLevel"] == 101
    assert result.nodes[0]["snr"] == -5.5


def test_snapshot_and_live_cache_are_independent_in_both_directions(interface):
    """Nested dictionary and protobuf mutation must not cross the snapshot."""
    interface.nodesByNum[2]["observation"] = mesh_pb2.User(long_name="Original")
    result = interface.queryNodes(roleFilter=["client"], hwModelFilter=["rak"])
    record = result.nodes[0]
    interface.nodesByNum[2]["user"]["longName"] = "Changed cache"
    interface.nodesByNum[2]["observation"].long_name = "Changed protobuf"
    assert record["user"]["longName"] == "Bravo"
    assert record["observation"].long_name == "Original"
    record["user"]["longName"] = "Changed result"
    record["observation"].long_name = "Changed result protobuf"
    assert interface.nodesByNum[2]["user"]["longName"] == "Changed cache"
    assert interface.nodesByNum[2]["observation"].long_name == "Changed protobuf"


def test_query_uses_one_snapshot_and_releases_lock_before_filtering(interface):
    """A receive update during filtering must not alter the captured records."""
    original_filter = node_data.filter_nodes
    finished = threading.Event()

    def update():
        with interface._node_db_lock:
            interface.nodesByNum[2]["user"]["longName"] = "Changed during query"
        finished.set()

    def filter_snapshot(*args, **kwargs):
        worker = threading.Thread(target=update)
        worker.start()
        try:
            assert finished.wait(1), "query held the node database lock while filtering"
        finally:
            worker.join(timeout=1)
        return original_filter(*args, **kwargs)

    with patch.object(node_data, "filter_nodes", side_effect=filter_snapshot):
        result = interface.queryNodes(roleFilter=["client"], hwModelFilter=["rak"])
    assert result.nodes[0]["user"]["longName"] == "Bravo"


@pytest.mark.parametrize(
    "direction, expected", [(None, [3, 2, 1, 4]), ("asc", [1, 2, 3, 4])]
)
def test_default_sort_uses_timestamps_and_keeps_missing_values_last(
    interface, direction, expected
):
    result = interface.queryNodes(sortDirection=direction)
    assert [record["num"] for record in result.nodes] == expected


@pytest.mark.parametrize(
    "options",
    [
        {"limit": -1},
        {"limit": True},
        {"limit": 1.5},
        {"sortDirection": "up"},
        {"sortField": ""},
        {"sortField": "misspelled"},
        {"roleFilter": "client"},
        {"hwModelFilter": [1]},
    ],
)
def test_invalid_query_options_are_rejected(interface, options):
    with pytest.raises(ValueError):
        interface.queryNodes(**options)


def test_empty_cache_has_explicit_zero_counts(interface):
    interface.nodesByNum = None
    result = interface.queryNodes(includeSelf=False, limit=5)
    assert result.nodes == ()
    assert (result.total, result.matched, result.returned, result.truncated) == (
        0,
        0,
        0,
        False,
    )


def test_json_document_preserves_data_and_omits_internal_payloads(interface):
    node = interface.nodesByNum[2]
    node.update(
        {"adminSessionPassKey": b"secret", "raw": object(), "snr": float("nan")}
    )
    node["user"]["publicKey"] = b"\x01\x02"
    node["observation"] = mesh_pb2.User(long_name="Observed")
    result = interface.queryNodes(roleFilter=["client"], hwModelFilter=["rak"])
    document = result.toDict()
    record = document["nodes"][0]
    assert document["schema_version"] == 1
    assert record["user"]["publicKey"] == "base64:AQI="
    assert record["observation"] == {"longName": "Observed"}
    assert record["snr"] is None
    assert "adminSessionPassKey" not in record
    assert "raw" not in record
    json.dumps(document, allow_nan=False)
    record["user"]["longName"] = "Changed JSON"
    assert result.nodes[0]["user"]["longName"] == "Bravo"


def _dispatch_json(interface, argv):
    parser = argparse.ArgumentParser(add_help=False)
    args = cli_main.parse_cli_args(parser, version="test", argv=argv)
    if args.dest is None:
        args.dest = "^all"
    lines = []
    hooks = cli_main._build_connected_dispatch_hooks()
    hooks = replace(
        hooks,
        cli_print=lines.append,
        services=replace(
            hooks.services, cli_print=lines.append, preference_print=lines.append
        ),
    )
    with activate_invocation(CliInvocation(args, parser)):
        _dispatch_connected(
            CliContext(
                interface, args, {}, ActionOutcome(interface_close_attempted=True)
            ),
            hooks,
        )
    return lines


def test_cli_json_emits_exactly_one_document_through_the_sink(interface, capsys):
    lines = _dispatch_json(
        interface, ["--nodes", "--json", "--role", "router", "--quiet"]
    )
    assert len(lines) == 1
    document = json.loads(lines[0])
    assert document["matched"] == 1
    assert document["nodes"][0]["num"] == 3
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "extra",
    [
        ["--reboot"],
        ["--info"],
        ["--get", "lora"],
        ["--listen"],
        ["--debug"],
        ["--show-fields", "snr"],
    ],
)
def test_cli_json_rejects_other_actions_before_dispatch(interface, extra):
    with patch("meshtastic.cli.device_actions._handle_device_actions") as actions:
        with pytest.raises(SystemExit):
            _dispatch_json(interface, ["--nodes", "--json", *extra])
    actions.assert_not_called()


def test_non_boolean_include_self_is_rejected(interface):
    with pytest.raises(ValueError, match="includeSelf must be a boolean"):
        interface.queryNodes(includeSelf="yes")


def test_json_encodes_list_typed_observations(interface):
    interface.nodesByNum[5] = {
        "num": 5,
        "user": {"longName": "Listed", "role": "CLIENT"},
        "lastHeard": 40,
        "positionQueue": [1, 2.5, "x"],
    }

    document = interface.queryNodes(includeSelf=False).toDict()

    encoded = next(node for node in document["nodes"] if node["num"] == 5)
    assert encoded["positionQueue"] == [1, 2.5, "x"]


def test_json_rejects_unsupported_observations(interface):
    interface.nodesByNum[6] = {
        "num": 6,
        "user": {"longName": "Opaque", "role": "CLIENT"},
        "lastHeard": 50,
        "handle": object(),
    }
    result = interface.queryNodes(includeSelf=False)

    with pytest.raises(TypeError, match="Cannot encode node value of type object"):
        result.toDict()
