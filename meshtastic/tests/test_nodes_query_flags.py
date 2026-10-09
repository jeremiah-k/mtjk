"""Meshtastic unit tests for --nodes query flags (role, hwmodel, sort, limit)."""

# pylint: disable=redefined-outer-name

from typing import Any
from unittest.mock import MagicMock

import pytest

from ..mesh_interface import MeshInterface
from ..mesh_interface_runtime import node_data


def _node(num: int, **kwargs: Any) -> dict[str, Any]:
    node: dict[str, Any] = {"num": num, "lastHeard": 1600000000 + num}
    node.update(kwargs)
    return node


@pytest.fixture
def query_nodes() -> list[dict[str, Any]]:
    return [
        _node(
            1,
            user={"longName": "Zed Alpha", "hwModel": "TBEAM", "role": "ROUTER"},
            snr=-8.0,
        ),
        _node(
            2,
            user={"longName": "Amy Brown", "hwModel": "RAK4631", "role": "CLIENT_MUTE"},
            snr=12.5,
        ),
        _node(3, user={"longName": "Mid Carter", "hwModel": "RAK4631"}, snr=5.0),
    ]


@pytest.mark.unit
def test_filter_nodes_by_role_substring_case_insensitive(
    query_nodes: list[dict[str, Any]],
) -> None:
    filtered = node_data.filter_nodes(
        query_nodes, True, 0, role_patterns=["client_mute"]
    )
    assert [node["num"] for node in filtered] == [2]

    filtered = node_data.filter_nodes(query_nodes, True, 0, role_patterns=["client"])
    assert [node["num"] for node in filtered] == [2]


@pytest.mark.unit
def test_filter_nodes_comma_values_are_any_of(
    query_nodes: list[dict[str, Any]],
) -> None:
    filtered = node_data.filter_nodes(
        query_nodes, True, 0, role_patterns=["router", "client"]
    )
    assert [node["num"] for node in filtered] == [1, 2]


@pytest.mark.unit
def test_filter_nodes_role_and_hwmodel_combine_with_and(
    query_nodes: list[dict[str, Any]],
) -> None:
    filtered = node_data.filter_nodes(
        query_nodes, True, 0, role_patterns=["client"], hwmodel_patterns=["rak"]
    )
    assert [node["num"] for node in filtered] == [2]


@pytest.mark.unit
def test_filter_nodes_missing_field_never_matches(
    query_nodes: list[dict[str, Any]],
) -> None:
    filtered = node_data.filter_nodes(query_nodes, True, 0, hwmodel_patterns=[""])
    assert filtered == query_nodes

    filtered = node_data.filter_nodes(query_nodes, True, 0, role_patterns=["router"])
    # Node 3 has no role and must not match even broad patterns
    assert 3 not in [node["num"] for node in filtered]


@pytest.mark.unit
def test_sort_nodes_default_stays_newest_first(
    query_nodes: list[dict[str, Any]],
) -> None:
    assert [node["num"] for node in node_data.sort_nodes(query_nodes)] == [3, 2, 1]


@pytest.mark.unit
def test_sort_nodes_numeric_field_defaults_high_to_low(
    query_nodes: list[dict[str, Any]],
) -> None:
    assert [node["num"] for node in node_data.sort_nodes(query_nodes, "snr")] == [
        2,
        3,
        1,
    ]


@pytest.mark.unit
def test_sort_nodes_text_field_defaults_a_to_z(
    query_nodes: list[dict[str, Any]],
) -> None:
    by_name = node_data.sort_nodes(query_nodes, "name")
    assert [node["user"]["longName"] for node in by_name] == [
        "Amy Brown",
        "Mid Carter",
        "Zed Alpha",
    ]


@pytest.mark.unit
def test_sort_nodes_explicit_direction_overrides_default(
    query_nodes: list[dict[str, Any]],
) -> None:
    assert [
        node["num"] for node in node_data.sort_nodes(query_nodes, "snr", "asc")
    ] == [1, 3, 2]


@pytest.mark.unit
def test_sort_nodes_missing_values_sort_last_in_both_directions() -> None:
    nodes = [
        _node(1, snr=1.0),
        _node(2),  # no snr
        _node(3, snr=9.0),
    ]
    assert [node["num"] for node in node_data.sort_nodes(nodes, "snr")] == [3, 1, 2]
    assert [node["num"] for node in node_data.sort_nodes(nodes, "snr", "asc")] == [
        1,
        3,
        2,
    ]


@pytest.mark.unit
def test_sort_nodes_alias_resolution() -> None:
    assert node_data._resolve_field_alias("HWModel") == "user.hwModel"
    assert node_data._resolve_field_alias("since") == "lastHeard"
    assert node_data._resolve_field_alias("user.hwModel") == "user.hwModel"
    assert node_data._resolve_field_alias("name") == "user.longName"


@pytest.mark.unit
def test_default_show_fields_are_compact() -> None:
    fields = node_data.get_default_show_fields()
    assert fields == [
        "N",
        "user.longName",
        "user.hwModel",
        "user.role",
        "deviceMetrics.batteryLevel",
        "snr",
        "hopsAway",
        "since",
    ]


@pytest.fixture
def query_nodes_iface() -> MeshInterface:
    nodes_by_num = {
        1: _node(
            1,
            user={"longName": "Zed Alpha", "hwModel": "TBEAM", "role": "ROUTER"},
            snr=-8.0,
        ),
        2: _node(
            2,
            user={
                "longName": "Amy Brown",
                "hwModel": "RAK4631",
                "role": "CLIENT_MUTE",
            },
            snr=12.5,
        ),
        3: _node(3, user={"longName": "Mid Carter", "hwModel": "RAK4631"}, snr=5.0),
    }
    iface = MeshInterface(noProto=True)
    iface.nodesByNum = nodes_by_num
    iface.nodes = {f"!{num:08x}": node for num, node in nodes_by_num.items()}
    iface.myInfo = MagicMock()
    iface.myInfo.my_node_num = 1
    iface.localNode = MagicMock()
    iface.localNode.nodeNum = 1
    return iface


@pytest.mark.unit
def test_show_nodes_prints_count_header(
    capsys: pytest.CaptureFixture[str], query_nodes_iface: MeshInterface
) -> None:
    output = query_nodes_iface.showNodes()
    out, err = capsys.readouterr()
    assert output.startswith("Nodes: 3\n")
    assert out.startswith("Nodes: 3\n")
    assert err == ""


@pytest.mark.unit
def test_show_nodes_role_filter_and_header(
    capsys: pytest.CaptureFixture[str], query_nodes_iface: MeshInterface
) -> None:
    output = query_nodes_iface.showNodes(roleFilter=["client_mute"])
    assert output.startswith("Nodes: 1 matching (of 3 known)\n")
    assert "Amy Brown" in output
    assert "Zed Alpha" not in output


@pytest.mark.unit
def test_show_nodes_limit_reports_truncation(
    capsys: pytest.CaptureFixture[str], query_nodes_iface: MeshInterface
) -> None:
    output = query_nodes_iface.showNodes(limit=1)
    assert output.startswith("Nodes: 1 of 3\n")
    assert output.endswith("… and 2 more not shown")
    assert "Mid Carter" in output  # newest first
    assert "Amy Brown" not in output


@pytest.mark.unit
def test_show_nodes_sort_and_limit_combine(
    capsys: pytest.CaptureFixture[str], query_nodes_iface: MeshInterface
) -> None:
    output = query_nodes_iface.showNodes(sortField="snr", limit=1)
    assert output.startswith("Nodes: 1 of 3\n")
    assert "Amy Brown" in output  # best SNR first


@pytest.mark.unit
def test_show_nodes_hwmodel_filter_case_insensitive(
    capsys: pytest.CaptureFixture[str], query_nodes_iface: MeshInterface
) -> None:
    output = query_nodes_iface.showNodes(hwModelFilter=["rak"])
    assert output.startswith("Nodes: 2 matching (of 3 known)\n")
    assert "TBEAM" not in output


@pytest.mark.unit
def test_show_nodes_filter_with_limit_reports_truncated_match_count(
    capsys: pytest.CaptureFixture[str], query_nodes_iface: MeshInterface
) -> None:
    """A filtered listing capped by --limit reports shown-of-matching-of-known."""
    output = query_nodes_iface.showNodes(hwModelFilter=["rak"], limit=1)
    first_line = output.split("\n")[0]
    assert first_line == "Nodes: 1 of 2 matching (of 3 known)"
    assert output.endswith("… and 1 more not shown")


@pytest.mark.unit
def test_show_nodes_filter_matching_all_nodes_omits_known_suffix(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A filter matching every node leaves no 'of N known' suffix after truncation."""
    iface = MeshInterface(noProto=True)
    iface.nodesByNum = {
        i: {
            "num": i,
            "user": {"longName": f"Node {i}", "hwModel": "RAK4631"},
            "lastHeard": 1600000000 + i,
        }
        for i in (1, 2, 3)
    }
    iface.nodes = {f"!{i:08x}": node for i, node in iface.nodesByNum.items()}
    iface.myInfo = MagicMock()
    iface.myInfo.my_node_num = 1
    iface.localNode = MagicMock()
    iface.localNode.nodeNum = 1

    output = iface.showNodes(hwModelFilter=["rak"], limit=2)

    assert output.split("\n")[0] == "Nodes: 2 of 3 matching"
    assert output.endswith("… and 1 more not shown")


@pytest.mark.unit
@pytest.mark.parametrize(
    "kwargs", [{"roleFilter": [" CLIENT_MUTE "]}, {"hwModelFilter": [" RAK "]}]
)
def test_public_filters_normalize_patterns(
    query_nodes_iface: MeshInterface, kwargs: dict[str, Any]
) -> None:
    output = query_nodes_iface.showNodes(**kwargs)
    assert "Amy Brown" in output
    assert "Zed Alpha" not in output


@pytest.mark.unit
@pytest.mark.parametrize(
    "direction, expected", [(None, [2, 1, 3]), ("asc", [1, 2, 3]), ("desc", [2, 1, 3])]
)
def test_numeric_telemetry_paths_use_numeric_default_direction(
    direction: str | None, expected: list[int]
) -> None:
    nodes = [
        _node(1, environmentMetrics={"temperature": 20.0}),
        _node(2, environmentMetrics={"temperature": 30.0}),
        _node(3),
    ]
    assert [
        node["num"]
        for node in node_data.sort_nodes(
            nodes, "environmentMetrics.temperature", direction
        )
    ] == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "direction, expected", [("asc", [1, 2, 3]), ("desc", [2, 1, 3])]
)
@pytest.mark.parametrize("invalid", ["garbage", "NaN", float("inf"), True])
def test_invalid_numeric_values_sort_after_valid_nodes(
    direction: str, expected: list[int], invalid: Any
) -> None:
    nodes = [_node(1, snr=1.0), _node(2, snr=9.0), _node(3, snr=invalid)]
    assert [
        node["num"] for node in node_data.sort_nodes(nodes, "snr", direction)
    ] == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "direction, expected", [(None, [2, 1, 3]), ("asc", [1, 2, 3]), ("desc", [2, 1, 3])]
)
def test_inferred_numeric_sort_keeps_invalid_values_last(
    direction: str | None, expected: list[int]
) -> None:
    nodes = [
        _node(1, environmentMetrics={"temperature": 9.0}),
        _node(2, environmentMetrics={"temperature": "10"}),
        _node(3, environmentMetrics={"temperature": "garbage"}),
    ]
    assert [
        node["num"]
        for node in node_data.sort_nodes(
            nodes, "environmentMetrics.temperature", direction
        )
    ] == expected
