"""Exercise the public node query contract and its JSON CLI consumer."""

import argparse
import io
import json
import threading
from collections.abc import Iterator
from dataclasses import replace
from typing import Any, cast
from unittest.mock import patch

import pytest

from meshtastic import __main__ as cli_main
from meshtastic._protocol_runtime import _on_admin_receive
from meshtastic.cli.context import ActionOutcome, CliContext
from meshtastic.cli.dispatch import _dispatch_connected
from meshtastic.cli.invocation import CliInvocation, activate_invocation
from meshtastic.cli.parser import parse_cli_args
from meshtastic.mesh_interface import MeshInterface
from meshtastic.mesh_interface_runtime import node_data
from meshtastic.protobuf import admin_pb2, mesh_pb2

pytestmark = pytest.mark.unit


@pytest.fixture
def interface() -> Iterator[MeshInterface]:
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


def test_query_returns_raw_records_counts_and_capture_time(
    interface: MeshInterface, capsys: pytest.CaptureFixture[str]
) -> None:
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


def test_query_combines_filters_and_preserves_numeric_measurements(
    interface: MeshInterface,
) -> None:
    """Select matching roles and hardware without table formatting."""
    result = interface.queryNodes(roleFilter=["client"], hwModelFilter=["rak"])
    assert (result.total, result.matched, result.returned) == (4, 1, 1)
    assert result.nodes[0]["deviceMetrics"]["batteryLevel"] == 101
    assert result.nodes[0]["snr"] == -5.5


def test_snapshot_and_live_cache_are_independent_in_both_directions(
    interface: MeshInterface,
) -> None:
    """Nested dictionary and protobuf mutation must not cross the snapshot."""
    nodes_by_num = cast("dict[int, dict[str, Any]]", interface.nodesByNum)
    nodes_by_num[2]["observation"] = mesh_pb2.User(long_name="Original")
    result = interface.queryNodes(roleFilter=["client"], hwModelFilter=["rak"])
    record = result.nodes[0]
    nodes_by_num[2]["user"]["longName"] = "Changed cache"
    nodes_by_num[2]["observation"].long_name = "Changed protobuf"
    assert record["user"]["longName"] == "Bravo"
    assert record["observation"].long_name == "Original"
    record["user"]["longName"] = "Changed result"
    record["observation"].long_name = "Changed result protobuf"
    assert nodes_by_num[2]["user"]["longName"] == "Changed cache"
    assert nodes_by_num[2]["observation"].long_name == "Changed protobuf"


def test_query_uses_one_snapshot_and_releases_lock_before_filtering(
    interface: MeshInterface,
) -> None:
    """A receive update during filtering must not alter the captured records."""
    original_filter = node_data.filter_nodes
    finished = threading.Event()

    def update() -> None:
        with interface._node_db_lock:
            nodes_by_num = cast("dict[int, dict[str, Any]]", interface.nodesByNum)
            nodes_by_num[2]["user"]["longName"] = "Changed during query"
        finished.set()

    def filter_snapshot(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
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
    interface: MeshInterface, direction: str | None, expected: list[int]
) -> None:
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
def test_invalid_query_options_are_rejected(
    interface: MeshInterface, options: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        interface.queryNodes(**options)


def test_empty_cache_has_explicit_zero_counts(interface: MeshInterface) -> None:
    interface.nodesByNum = None
    result = interface.queryNodes(includeSelf=False, limit=5)
    assert result.nodes == ()
    assert (result.total, result.matched, result.returned, result.truncated) == (
        0,
        0,
        0,
        False,
    )


def test_json_document_preserves_data_and_omits_internal_payloads(
    interface: MeshInterface,
) -> None:
    node = cast("dict[int, dict[str, Any]]", interface.nodesByNum)[2]
    node.update(
        {"adminSessionPassKey": b"secret", "raw": object(), "snr": float("nan")}
    )
    node["user"]["publicKey"] = b"\x01\x02"
    node["observation"] = mesh_pb2.User(long_name="Observed")
    result = interface.queryNodes(roleFilter=["client"], hwModelFilter=["rak"])
    document = cast("dict[str, Any]", result.toDict())
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


def _dispatch_json(interface: MeshInterface, argv: list[str]) -> list[str]:
    parser = argparse.ArgumentParser(add_help=False)
    args = parse_cli_args(parser, version="test", argv=argv)
    if args.dest is None:
        args.dest = "^all"
    lines: list[str] = []
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


@pytest.mark.parametrize("use_cli", [False, True])
def test_json_omits_cached_admin_packet_bodies(
    interface: MeshInterface, use_cli: bool
) -> None:
    """Receive metadata survives JSON export without serialized admin secrets."""
    interface.nodes = {}
    admin = admin_pb2.AdminMessage(session_passkey=b"test-session-key")
    admin.get_config_response.network.wifi_psk = "test-network-password"
    payload = admin.SerializeToString()
    _on_admin_receive(
        interface,
        {
            "from": 2,
            "to": 1,
            "rxTime": 40,
            "decoded": {
                "portnum": "ADMIN_APP",
                "payload": payload,
                "admin": {"raw": admin},
            },
        },
    )
    result = interface.queryNodes(roleFilter=["client"], hwModelFilter=["rak"])
    if use_cli:
        document = json.loads(
            _dispatch_json(
                interface, ["--nodes", "--json", "--role", "client", "--hwmodel", "rak"]
            )[0]
        )
    else:
        document = result.toDict()

    record = document["nodes"][0]
    assert record["lastReceived"] == {"from": 2, "to": 1, "rxTime": 40}
    assert "adminSessionPassKey" not in record
    assert result.nodes[0]["lastReceived"]["decoded"]["payload"] == payload
    assert result.nodes[0]["adminSessionPassKey"] == b"test-session-key"
    assert admin.session_passkey == b"test-session-key"


def test_json_omits_internal_fields_inside_nested_observations(
    interface: MeshInterface,
) -> None:
    """Nested lists and dictionaries apply the same export boundary as nodes."""
    node = cast("dict[int, dict[str, Any]]", interface.nodesByNum)[2]
    node["history"] = [
        {
            "timestamp": 50,
            "payload": object(),
            "raw": object(),
            "decoded": object(),
            "adminSessionPassKey": b"test-session-key",
        }
    ]

    document: dict[str, Any] = interface.queryNodes(
        roleFilter=["client"], hwModelFilter=["rak"]
    ).toDict()

    assert document["nodes"][0]["history"] == [{"timestamp": 50}]
    assert "payload" in node["history"][0]


def test_node_exports_strip_keys_inside_list_observations(
    interface: MeshInterface,
) -> None:
    """List-wrapped cached admin observations must not bypass redaction."""
    node = cast("dict[int, dict[str, Any]]", interface.nodesByNum)[2]
    node["user"]["id"] = "!00000002"
    node["history"] = [
        {
            "rxTime": 72,
            "decoded": {"payload": b"secret-raw-packet"},
            "adminSessionPassKey": b"secret-admin-key",
            "sessionPasskey": b"secret-proto-key",
            "session_passkey": b"secret-native-key",
            "nested": [{"payload": b"second-raw-packet", "snr": -4}],
        }
    ]
    interface.nodes = {"!00000002": node}

    json_document = cast(
        "dict[str, Any]",
        interface.queryNodes(roleFilter=["client"], hwModelFilter=["rak"]).toDict(),
    )
    expected_history = [{"rxTime": 72, "nested": [{"snr": -4}]}]
    assert json_document["nodes"][0]["history"] == expected_history

    info = interface.showInfo(file=io.StringIO())
    info_nodes = json.loads(info.split("Nodes in mesh: ", 1)[1])
    assert info_nodes["!00000002"]["history"] == expected_history
    assert "secret-raw-packet" not in info
    assert "secret-admin-key" not in info
    assert "secret-proto-key" not in info
    assert node["history"][0]["adminSessionPassKey"] == b"secret-admin-key"
    assert node["history"][0]["decoded"]["payload"] == b"secret-raw-packet"


@pytest.mark.parametrize("use_info", [False, True])
def test_json_filters_session_passkeys_in_nested_protobuf(
    interface: MeshInterface, use_info: bool
) -> None:
    """A cached protobuf is normalized after applying the same secret policy."""
    node = cast("dict[int, dict[str, Any]]", interface.nodesByNum)[2]
    node["user"]["id"] = "!00000002"
    node["observation"] = admin_pb2.AdminMessage(session_passkey=b"not-for-json")
    if use_info:
        interface.nodes = {"!00000002": node}
        info = interface.showInfo(file=io.StringIO())
        exported = json.loads(info.split("Nodes in mesh: ", 1)[1])["!00000002"]
    else:
        result = cast(
            "dict[str, Any]",
            interface.queryNodes(roleFilter=["client"], hwModelFilter=["rak"]).toDict(),
        )
        exported = result["nodes"][0]

    assert exported["observation"] == {}
    assert node["observation"].session_passkey == b"not-for-json"


def test_info_omits_cached_admin_session_keys(interface: MeshInterface) -> None:
    """The human-readable node summary does not expose the active admin key."""
    node = cast("dict[int, dict[str, Any]]", interface.nodesByNum)[2]
    node["user"]["id"] = "!00000002"
    node["adminSessionPassKey"] = b"test-session-key"
    interface.nodes = {"!00000002": node}

    output = interface.showInfo(file=io.StringIO())

    assert "adminSessionPassKey" not in output
    assert "Bravo" in output
    assert node["adminSessionPassKey"] == b"test-session-key"


def test_cli_json_emits_exactly_one_document_through_the_sink(
    interface: MeshInterface, capsys: pytest.CaptureFixture[str]
) -> None:
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
def test_cli_json_rejects_other_actions_before_dispatch(
    interface: MeshInterface, extra: list[str]
) -> None:
    with patch("meshtastic.cli.device_actions._handle_device_actions") as actions:
        with pytest.raises(SystemExit):
            _dispatch_json(interface, ["--nodes", "--json", *extra])
    actions.assert_not_called()


def test_non_boolean_include_self_is_rejected(interface: MeshInterface) -> None:
    with pytest.raises(ValueError, match="includeSelf must be a boolean"):
        interface.queryNodes(includeSelf=cast("bool", "yes"))


def test_json_encodes_list_typed_observations(interface: MeshInterface) -> None:
    cast("dict[int, dict[str, Any]]", interface.nodesByNum)[5] = {
        "num": 5,
        "user": {"longName": "Listed", "role": "CLIENT"},
        "lastHeard": 40,
        "positionQueue": [1, 2.5, "x"],
    }

    document: dict[str, Any] = interface.queryNodes(includeSelf=False).toDict()

    encoded = next(node for node in document["nodes"] if node["num"] == 5)
    assert encoded["positionQueue"] == [1, 2.5, "x"]


def test_json_rejects_unsupported_observations(interface: MeshInterface) -> None:
    cast("dict[int, dict[str, Any]]", interface.nodesByNum)[6] = {
        "num": 6,
        "user": {"longName": "Opaque", "role": "CLIENT"},
        "lastHeard": 50,
        "handle": object(),
    }
    result = interface.queryNodes(includeSelf=False)

    with pytest.raises(TypeError, match="Cannot encode node value of type object"):
        result.toDict()
