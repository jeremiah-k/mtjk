"""Meshtastic unit tests for mesh_interface_runtime/node_view.py."""

# pylint: disable=redefined-outer-name,protected-access

import sys
import threading
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

from meshtastic import BROADCAST_ADDR, BROADCAST_NUM, LOCAL_ADDR
from meshtastic.mesh_interface import MeshInterface
from meshtastic.mesh_interface_runtime.node_view import (
    NodeView,
    _normalize_json_serializable,
    _timeago,
)
from meshtastic.mesh_interface_runtime.ports import _NodeViewPort
from meshtastic.protobuf import mesh_pb2


@pytest.fixture
def mock_interface() -> MagicMock:
    """Create a minimal mock MeshInterface for node view tests."""
    # Create a spec-based mock that includes MeshInterfaceError
    interface = MagicMock(spec=MeshInterface)
    interface._node_db_lock = threading.RLock()
    interface.localNode = MagicMock()
    interface.localNode.nodeNum = 12345
    interface.nodes = {}
    interface.nodesByNum = {}
    interface.myInfo = None
    interface.metadata = None
    interface.debugOut = sys.stdout

    return interface


@pytest.fixture
def node_view(mock_interface: MagicMock) -> NodeView:
    """Create a NodeView instance with mocked interface."""
    return NodeView(_NodeViewPort(mock_interface))


class TestTimeAgo:
    """Tests for _timeago function."""

    @pytest.mark.unit
    def test_timeago_now(self) -> None:
        """Test timeago for zero/negative seconds returns 'now'."""
        assert _timeago(0) == "now"
        assert _timeago(-1) == "now"

    @pytest.mark.unit
    def test_timeago_seconds(self) -> None:
        """Test timeago for seconds."""
        assert _timeago(1) == "1 sec ago"
        assert _timeago(30) == "30 secs ago"
        assert _timeago(59) == "59 secs ago"

    @pytest.mark.unit
    def test_timeago_minutes(self) -> None:
        """Test timeago for minutes."""
        assert _timeago(60) == "1 min ago"
        assert _timeago(90) == "1 min ago"
        assert _timeago(120) == "2 mins ago"

    @pytest.mark.unit
    def test_timeago_hours(self) -> None:
        """Test timeago for hours."""
        assert _timeago(3600) == "1 hour ago"
        assert _timeago(7200) == "2 hours ago"

    @pytest.mark.unit
    def test_timeago_days(self) -> None:
        """Test timeago for days."""
        assert _timeago(86400) == "1 day ago"
        assert _timeago(172800) == "2 days ago"


class TestNormalizeJsonSerializableNodeView:
    """Tests for _normalize_json_serializable function in node_view."""

    @pytest.mark.unit
    def test_normalize_bytes_node_view(self) -> None:
        """Test that bytes are base64-encoded."""
        result = _normalize_json_serializable(b"hello")
        assert result == "base64:aGVsbG8="

    @pytest.mark.unit
    def test_normalize_dict_node_view(self) -> None:
        """Test that dict values are recursively normalized."""
        data = {"key": b"value", "num": 42}
        result = _normalize_json_serializable(data)
        assert result == {"key": "base64:dmFsdWU=", "num": 42}

    @pytest.mark.unit
    def test_normalize_list_node_view(self) -> None:
        """Test that list items are recursively normalized."""
        data = [b"item1", "item2", 42]
        result = _normalize_json_serializable(data)
        assert result == ["base64:aXRlbTE=", "item2", 42]

    @pytest.mark.unit
    def test_normalize_primitives_node_view(self) -> None:
        """Test that primitives are returned as-is."""
        assert _normalize_json_serializable(None) is None
        assert _normalize_json_serializable(True) is True
        assert _normalize_json_serializable(42) == 42
        assert _normalize_json_serializable("hello") == "hello"


class TestNodeViewInit:
    """Tests for NodeView initialization."""

    @pytest.mark.unit
    def test_node_view_init(self, mock_interface: MagicMock) -> None:
        """Test NodeView initialization."""
        view = NodeView(_NodeViewPort(mock_interface))

        assert view._port.facade is mock_interface


class TestNodeViewProperties:
    """Tests for NodeView properties."""

    @pytest.mark.unit
    def test_node_db_lock_property(
        self, node_view: NodeView, mock_interface: MagicMock
    ) -> None:
        """Test _node_db_lock property returns interface lock."""
        assert node_view._node_db_lock is mock_interface._node_db_lock

    @pytest.mark.unit
    def test_local_node_property(
        self, node_view: NodeView, mock_interface: MagicMock
    ) -> None:
        """Test localNode property."""
        assert node_view.local_node is mock_interface.localNode

    @pytest.mark.unit
    def test_nodes_property(
        self, node_view: NodeView, mock_interface: MagicMock
    ) -> None:
        """Test nodes property."""
        nodes = {"!1234": {"num": 1234}}
        mock_interface.nodes = nodes
        assert node_view.nodes is nodes

    @pytest.mark.unit
    def test_nodes_by_num_property(
        self, node_view: NodeView, mock_interface: MagicMock
    ) -> None:
        """Test nodesByNum property."""
        nodes_by_num = {1234: {"num": 1234}}
        mock_interface.nodesByNum = nodes_by_num
        assert node_view.nodes_by_num is nodes_by_num

    @pytest.mark.unit
    def test_my_info_property(
        self, node_view: NodeView, mock_interface: MagicMock
    ) -> None:
        """Test myInfo property."""
        my_info = mesh_pb2.MyNodeInfo()
        mock_interface.myInfo = my_info
        assert node_view.my_info is my_info

    @pytest.mark.unit
    def test_metadata_property(
        self, node_view: NodeView, mock_interface: MagicMock
    ) -> None:
        """Test metadata property."""
        metadata = mesh_pb2.DeviceMetadata()
        mock_interface.metadata = metadata
        assert node_view.metadata is metadata


class TestPrintLogLine:
    """Tests for _print_log_line method."""

    @pytest.mark.unit
    def test_print_log_line_stdout(
        self,
        node_view: NodeView,
        mock_interface: MagicMock,
    ) -> None:
        """Test printing log line to stdout."""
        mock_interface.debugOut = sys.stdout

        with patch(
            "meshtastic.mesh_interface_runtime.node_view.print_color"
        ) as mock_print_color:
            node_view._print_log_line("Test message")
            mock_print_color.print.assert_called_once_with("Test message")

    @pytest.mark.unit
    def test_print_log_line_callable(
        self, node_view: NodeView, mock_interface: MagicMock
    ) -> None:
        """Test printing log line to callable."""
        output_lines: list[str] = []
        mock_interface.debugOut = output_lines.append

        node_view._print_log_line("Test message")

        assert output_lines == ["Test message"]

    @pytest.mark.unit
    def test_print_log_line_file_like(
        self, node_view: NodeView, mock_interface: MagicMock
    ) -> None:
        """Test printing log line to file-like object."""
        output = StringIO()
        mock_interface.debugOut = output

        node_view._print_log_line("Test message")

        assert output.getvalue() == "Test message\n"


class TestHandleLogLine:
    """Tests for _handle_log_line method."""

    @pytest.mark.unit
    def test_handle_log_line_publishes(
        self, node_view: NodeView, mock_interface: MagicMock
    ) -> None:
        """Test that _handle_log_line publishes to pubsub."""
        with patch("meshtastic.mesh_interface_runtime.node_view.pub") as mock_pub:
            node_view._handle_log_line("Test log message")

        mock_pub.sendMessage.assert_called_once()
        call_args = mock_pub.sendMessage.call_args
        assert call_args[0][0] == "meshtastic.log.line"
        assert call_args[1]["line"] == "Test log message"
        assert call_args[1]["interface"] is mock_interface

    @pytest.mark.unit
    def test_handle_log_line_strips_trailing_newline(self, node_view: NodeView) -> None:
        """Test that trailing newline is stripped."""
        with patch("meshtastic.mesh_interface_runtime.node_view.pub") as mock_pub:
            node_view._handle_log_line("Test message\n")

        assert mock_pub.sendMessage.call_args[1]["line"] == "Test message"


class TestHandleLogRecord:
    """Tests for _handle_log_record method."""

    @pytest.mark.unit
    def test_handle_log_record(self, node_view: NodeView) -> None:
        """Test handling log record."""
        record = mesh_pb2.LogRecord()
        record.message = "Log record message"

        with patch.object(node_view, "_handle_log_line") as mock_handle:
            node_view._handle_log_record(record)

        mock_handle.assert_called_once_with("Log record message")


class TestShowInfo:
    """Tests for showInfo method."""

    @pytest.mark.unit
    def test_show_info_basic(
        self, node_view: NodeView, mock_interface: MagicMock
    ) -> None:
        """Test basic showInfo functionality."""
        mock_interface.myInfo = mesh_pb2.MyNodeInfo()
        mock_interface.myInfo.my_node_num = 12345
        mock_interface.nodes = {}

        output = StringIO()
        result = node_view.show_info(file=output)

        assert "Owner:" in result
        assert "Nodes in mesh:" in result

    @pytest.mark.unit
    def test_show_info_with_nodes(
        self, node_view: NodeView, mock_interface: MagicMock
    ) -> None:
        """Test showInfo with nodes."""
        mock_interface.myInfo = mesh_pb2.MyNodeInfo()
        mock_interface.nodes = {
            "!1234": {
                "num": 1234,
                "user": {
                    "id": "!1234",
                    "longName": "Test Node",
                    "macaddr": "1234567890ab",
                },
            }
        }

        output = StringIO()
        result = node_view.show_info(file=output)

        assert "!1234" in result
        assert "Test Node" in result


class TestBuildTableData:
    """Tests for _build_table_data method."""

    @pytest.mark.unit
    def test_build_table_data_simple(self, node_view: NodeView) -> None:
        """Test building table data with simple fields."""
        nodes = [
            {"num": 1234, "user": {"id": "!1234", "longName": "Test Node"}},
            {"num": 5678, "user": {"id": "!5678", "longName": "Another Node"}},
        ]
        fields = ["num", "user.longName"]

        with patch(
            "meshtastic.mesh_interface_runtime.node_view.node_data"
        ) as mock_node_data:
            with patch(
                "meshtastic.mesh_interface_runtime.node_view.node_presentation"
            ) as mock_presentation:
                mock_node_data.extract_node_field_value.return_value = "extracted_value"
                mock_presentation._format_node_field.return_value = "formatted_value"
                mock_presentation._get_human_readable_column_label.return_value = (
                    "Human Label"
                )

                result = node_view._build_table_data(nodes, fields)

        assert len(result) == 2
        assert all(isinstance(row, dict) for row in result)


class TestRenderNodeTable:
    """Tests for _render_node_table method."""

    @pytest.mark.unit
    def test_render_node_table(self, node_view: NodeView) -> None:
        """Test rendering node table."""
        rows = [
            {"N": 1, "Name": "Node 1"},
            {"N": 2, "Name": "Node 2"},
        ]

        with patch(
            "meshtastic.mesh_interface_runtime.node_view.tabulate"
        ) as mock_tabulate:
            mock_tabulate.return_value = "Rendered Table"
            result = node_view._render_node_table(rows)

        assert result == "Rendered Table"
        mock_tabulate.assert_called_once()


class TestShowNodes:
    """Tests for showNodes method."""

    @pytest.mark.unit
    def test_show_nodes_empty(
        self, node_view: NodeView, mock_interface: MagicMock
    ) -> None:
        """Test showNodes with empty node list."""
        mock_interface.nodesByNum = {}

        with patch.object(node_view, "_render_node_table") as mock_render:
            mock_render.return_value = "Empty Table"
            result = node_view.show_nodes()

        assert result == "Nodes: 0\nEmpty Table"

    @pytest.mark.unit
    def test_show_nodes_with_data(
        self, node_view: NodeView, mock_interface: MagicMock
    ) -> None:
        """Test showNodes with nodes."""
        mock_interface.nodesByNum = {
            1234: {"num": 1234, "user": {"id": "!1234", "longName": "Test Node"}},
        }

        with patch.object(node_view, "_build_table_data") as mock_build:
            with patch.object(node_view, "_render_node_table") as mock_render:
                with patch(
                    "meshtastic.mesh_interface_runtime.node_view.node_data"
                ) as mock_node_data:
                    mock_build.return_value = [{"N": 1, "Name": "Test Node"}]
                    mock_render.return_value = "Rendered Table"
                    mock_node_data.filter_nodes.return_value = [
                        mock_interface.nodesByNum[1234]
                    ]
                    mock_node_data.sort_nodes.return_value = [
                        mock_interface.nodesByNum[1234]
                    ]
                    mock_node_data.get_default_show_fields.return_value = ["N", "Name"]

                    result = node_view.show_nodes()

        assert result == "Nodes: 1\nRendered Table"


class TestGetNode:
    """Tests for getNode method."""

    @pytest.mark.unit
    def test_get_node_local_addr(
        self, node_view: NodeView, mock_interface: MagicMock
    ) -> None:
        """Test getting node for local address."""
        result = node_view.get_node(LOCAL_ADDR)

        assert result is mock_interface.localNode

    @pytest.mark.unit
    def test_get_node_broadcast_addr(
        self, node_view: NodeView, mock_interface: MagicMock
    ) -> None:
        """Test getting node for broadcast address."""
        result = node_view.get_node(BROADCAST_ADDR)

        assert result is mock_interface.localNode

    @pytest.mark.unit
    def test_get_node_remote(self, node_view: NodeView) -> None:
        """Test getting remote node."""
        with patch(
            "meshtastic.mesh_interface_runtime.node_view.meshtastic.node.Node"
        ) as mock_node_class:
            mock_node = MagicMock()
            mock_node_class.return_value = mock_node
            mock_node.waitForConfig.return_value = True

            result = node_view.get_node("!1234abcd", requestChannels=False)

        assert result is mock_node


class TestGetMyNodeInfo:
    """Tests for getMyNodeInfo method."""

    @pytest.mark.unit
    def test_get_my_node_info_success(
        self, node_view: NodeView, mock_interface: MagicMock
    ) -> None:
        """Test getting my node info."""
        my_info = mesh_pb2.MyNodeInfo()
        my_info.my_node_num = 12345
        mock_interface.myInfo = my_info
        mock_interface.nodesByNum = {12345: {"num": 12345, "user": {"id": "!1234"}}}

        result = node_view.get_my_node_info()

        assert result is not None
        assert result["num"] == 12345

    @pytest.mark.unit
    def test_get_my_node_info_no_my_info(
        self, node_view: NodeView, mock_interface: MagicMock
    ) -> None:
        """Test getting my node info when myInfo is None."""
        mock_interface.myInfo = None

        result = node_view.get_my_node_info()

        assert result is None


class TestGetMyUser:
    """Tests for getMyUser method."""

    @pytest.mark.unit
    def test_get_my_user_success(
        self, node_view: NodeView, mock_interface: MagicMock
    ) -> None:
        """Test getting my user info."""
        mock_interface.myInfo = mesh_pb2.MyNodeInfo()
        mock_interface.myInfo.my_node_num = 12345
        mock_interface.nodesByNum = {
            12345: {"num": 12345, "user": {"id": "!1234", "longName": "Test User"}}
        }

        result = node_view.get_my_user()

        assert result is not None
        assert result["longName"] == "Test User"

    @pytest.mark.unit
    def test_get_my_user_none(
        self, node_view: NodeView, mock_interface: MagicMock
    ) -> None:
        """Test getting my user when no node info."""
        mock_interface.myInfo = None

        result = node_view.get_my_user()

        assert result is None


class TestGetLongName:
    """Tests for getLongName method."""

    @pytest.mark.unit
    def test_get_long_name_success(
        self, node_view: NodeView, mock_interface: MagicMock
    ) -> None:
        """Test getting long name."""
        mock_interface.myInfo = mesh_pb2.MyNodeInfo()
        mock_interface.myInfo.my_node_num = 12345
        mock_interface.nodesByNum = {
            12345: {
                "num": 12345,
                "user": {"id": "!1234", "longName": "Test User Long Name"},
            }
        }

        result = node_view.get_long_name()

        assert result == "Test User Long Name"

    @pytest.mark.unit
    def test_get_long_name_none(
        self, node_view: NodeView, mock_interface: MagicMock
    ) -> None:
        """Test getting long name when no user info."""
        mock_interface.myInfo = None

        result = node_view.get_long_name()

        assert result is None


class TestGetShortName:
    """Tests for getShortName method."""

    @pytest.mark.unit
    def test_get_short_name_success(
        self, node_view: NodeView, mock_interface: MagicMock
    ) -> None:
        """Test getting short name."""
        mock_interface.myInfo = mesh_pb2.MyNodeInfo()
        mock_interface.myInfo.my_node_num = 12345
        mock_interface.nodesByNum = {
            12345: {"num": 12345, "user": {"id": "!1234", "shortName": "TU"}}
        }

        result = node_view.get_short_name()

        assert result == "TU"

    @pytest.mark.unit
    def test_get_short_name_none(
        self, node_view: NodeView, mock_interface: MagicMock
    ) -> None:
        """Test getting short name when no user info."""
        mock_interface.myInfo = None

        result = node_view.get_short_name()

        assert result is None


class TestGetPublicKey:
    """Tests for getPublicKey method."""

    @pytest.mark.unit
    def test_get_public_key_success(
        self, node_view: NodeView, mock_interface: MagicMock
    ) -> None:
        """Test getting public key."""
        mock_interface.myInfo = mesh_pb2.MyNodeInfo()
        mock_interface.myInfo.my_node_num = 12345
        public_key = b"test_public_key"
        mock_interface.nodesByNum = {
            12345: {"num": 12345, "user": {"id": "!1234", "publicKey": public_key}}
        }

        result = node_view.get_public_key()

        assert result == public_key

    @pytest.mark.unit
    def test_get_public_key_none(
        self, node_view: NodeView, mock_interface: MagicMock
    ) -> None:
        """Test getting public key when no user info."""
        mock_interface.myInfo = None

        result = node_view.get_public_key()

        assert result is None


class TestGetCannedMessage:
    """Tests for getCannedMessage method."""

    @pytest.mark.unit
    def test_get_canned_message_delegates(
        self, node_view: NodeView, mock_interface: MagicMock
    ) -> None:
        """Test that getCannedMessage delegates to localNode."""
        mock_interface.localNode.get_canned_message.return_value = "Canned message text"

        result = node_view.get_canned_message()

        assert result == "Canned message text"
        mock_interface.localNode.get_canned_message.assert_called_once()


class TestGetRingtone:
    """Tests for getRingtone method."""

    @pytest.mark.unit
    def test_get_ringtone_delegates(
        self, node_view: NodeView, mock_interface: MagicMock
    ) -> None:
        """Test that getRingtone delegates to localNode."""
        mock_interface.localNode.get_ringtone.return_value = "Ringtone melody"

        result = node_view.get_ringtone()

        assert result == "Ringtone melody"
        mock_interface.localNode.get_ringtone.assert_called_once()


class TestFixupPosition:
    """Tests for _fixup_position method."""

    @pytest.mark.unit
    def test_fixup_position_converts_integer_coords(self, node_view: NodeView) -> None:
        """Test converting integer micro-degree coordinates to float degrees."""
        position = {
            "latitudeI": 374560000,
            "longitudeI": -1222345000,
        }

        result = node_view._fixup_position(position)

        assert result["latitude"] == pytest.approx(37.456)
        assert result["longitude"] == pytest.approx(-122.2345)

    @pytest.mark.unit
    def test_fixup_position_no_coords(self, node_view: NodeView) -> None:
        """Test that position without integer coords is unchanged."""
        position = {"latitude": 37.456, "longitude": -122.2345}

        result = node_view._fixup_position(position)

        assert result == position


class TestNodeNumToId:
    """Tests for _node_num_to_id method."""

    @pytest.mark.unit
    def test_node_num_to_id_broadcast_dest(self, node_view: NodeView) -> None:
        """Test mapping broadcast num to broadcast address (dest mode)."""
        result = node_view._node_num_to_id(BROADCAST_NUM, isDest=True)

        assert result == BROADCAST_ADDR

    @pytest.mark.unit
    def test_node_num_to_id_broadcast_source(self, node_view: NodeView) -> None:
        """Test mapping broadcast num to 'Unknown' (source mode)."""
        result = node_view._node_num_to_id(BROADCAST_NUM, isDest=False)

        assert result == "Unknown"

    @pytest.mark.unit
    def test_node_num_to_id_valid(
        self, node_view: NodeView, mock_interface: MagicMock
    ) -> None:
        """Test mapping valid node num to id."""
        mock_interface.nodesByNum = {
            12345: {"user": {"id": "!1234"}},
        }

        result = node_view._node_num_to_id(12345)

        assert result == "!1234"

    @pytest.mark.unit
    def test_node_num_to_id_not_found(
        self, node_view: NodeView, mock_interface: MagicMock
    ) -> None:
        """Test mapping non-existent node num."""
        mock_interface.nodesByNum = {}

        result = node_view._node_num_to_id(12345)

        assert result is None

    @pytest.mark.unit
    def test_node_num_to_id_nodes_none(
        self, node_view: NodeView, mock_interface: MagicMock
    ) -> None:
        """Test mapping when nodesByNum is None."""
        mock_interface.nodesByNum = None

        result = node_view._node_num_to_id(12345)

        assert result is None


class TestGetOrCreateByNum:
    """Tests for _get_or_create_by_num method."""

    @pytest.mark.unit
    def test_get_existing_node(
        self, node_view: NodeView, mock_interface: MagicMock
    ) -> None:
        """Test getting an existing node by number."""
        existing_node = {"num": 12345, "user": {"id": "!1234"}}
        mock_interface.nodesByNum = {12345: existing_node}

        result = node_view._get_or_create_by_num(12345)

        assert result is existing_node

    @pytest.mark.unit
    def test_create_new_node(
        self, node_view: NodeView, mock_interface: MagicMock
    ) -> None:
        """Test creating a new node when not found."""
        mock_interface.nodesByNum = {}

        result = node_view._get_or_create_by_num(12345)

        assert result["num"] == 12345
        assert "user" in result
        assert result["user"]["id"] == "!00003039"

    @pytest.mark.unit
    def test_get_or_create_broadcast_raises(self, node_view: NodeView) -> None:
        """Test that creating a broadcast node raises an error."""
        with pytest.raises(MeshInterface.MeshInterfaceError, match="broadcast"):
            node_view._get_or_create_by_num(BROADCAST_NUM)

    @pytest.mark.unit
    def test_get_or_create_nodes_none_raises(
        self, node_view: NodeView, mock_interface: MagicMock
    ) -> None:
        """Test that getting/creating node when nodesByNum is None raises error."""
        mock_interface.nodesByNum = None

        with pytest.raises(MeshInterface.MeshInterfaceError, match="not initialized"):
            node_view._get_or_create_by_num(12345)
