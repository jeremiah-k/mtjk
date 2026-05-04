"""Mesh Interface class."""

# pylint: disable=C0302,R0801

import base64
import collections
import copy
import hashlib
import logging
import random
import sys
import threading
import time
import traceback
from types import TracebackType
from typing import IO, Any, Callable, TypeAlias, cast

import google.protobuf.json_format
from google.protobuf import message as protobuf_message

try:
    import print_color  # type: ignore[import-untyped]
except ImportError:
    print_color = None

from pubsub import pub

import meshtastic.node
from meshtastic import (
    BROADCAST_ADDR,
    BROADCAST_NUM,
    NODELESS_WANT_CONFIG_ID,
    ResponseHandler,
    protocols,
    publishingThread,
)
from meshtastic.mesh_interface_runtime.flows import (
    DEFAULT_TELEMETRY_TYPE,
    TelemetryType,
)
from meshtastic.mesh_interface_runtime.node_view import NodeView
from meshtastic.mesh_interface_runtime.queue_send import _QueueSendRuntime
from meshtastic.mesh_interface_runtime.receive_pipeline import (
    LOCAL_CONFIG_FROM_RADIO_FIELDS,
    MODULE_CONFIG_FROM_RADIO_FIELDS,
    ReceivePipeline,
    _FromRadioContext,
    _LazyMessageDict,
    _PacketRuntimeContext,
    _PublicationIntent,
)
from meshtastic.mesh_interface_runtime.request_wait import (
    DECODE_ERROR_KEY,
    DECODE_FAILED_PREFIX,
    RETIRED_WAIT_REQUEST_ID_TTL_SECONDS,
    _RequestWaitRuntime,
)
from meshtastic.mesh_interface_runtime.send_pipeline import (
    PayloadData,
    SendPipeline,
)
from meshtastic.mesh_interface_runtime.send_pipeline import (
    extract_request_id_from_packet as _pipeline_extract_request_id_from_packet,
)
from meshtastic.mesh_interface_runtime.send_pipeline import (
    extract_request_id_from_sent_packet as _pipeline_extract_request_id_from_sent_packet,
)
from meshtastic.protobuf import (
    channel_pb2,
    config_pb2,
    mesh_pb2,
    module_config_pb2,
    portnums_pb2,
)
from meshtastic.util import (
    Acknowledgment,
    Timeout,
    stripnl,
)

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 300
CONNECT_WAIT_POLL_SECONDS = 0.2

PACKET_ID_MASK = 0xFFFFFFFF
PACKET_ID_COUNTER_MASK = 0x3FF
PACKET_ID_RANDOM_MAX = 0x3FFFFF
PACKET_ID_RANDOM_SHIFT_BITS = 10

QUEUE_WAIT_DELAY_SECONDS = 0.5

UNKNOWN_SNR_QUARTER_DB = -128
MISSING_NODE_NUM_ERROR_TEMPLATE = "NodeId {destination_id} has no numeric 'num' in DB"
NODE_NOT_FOUND_IN_DB_ERROR_TEMPLATE = "NodeId {destination_id} not found in DB"
NODE_NOT_FOUND_DB_UNAVAILABLE_ERROR_TEMPLATE = (
    "NodeId {destination_id} not found and node DB is unavailable"
)
HEX_NODE_ID_TAIL_CHARS = frozenset("0123456789abcdefABCDEF")
NO_RESPONSE_FIRMWARE_ERROR: str = (
    "No response from node. At least firmware 2.1.22 is required on the destination node."
)

JSONValue: TypeAlias = (
    None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]
)


# Module-level cached predicates for _select_from_radio_branch to avoid
# recreating lambdas on every call. Each predicate follows the signature
# Callable[[mesh_pb2.FromRadio, _FromRadioContext], bool].
def _fr_has_my_info(fr: mesh_pb2.FromRadio, _ctx: Any) -> bool:
    return fr.HasField("my_info")


def _fr_has_metadata(fr: mesh_pb2.FromRadio, _ctx: Any) -> bool:
    return fr.HasField("metadata")


def _fr_has_node_info(fr: mesh_pb2.FromRadio, _ctx: Any) -> bool:
    return fr.HasField("node_info")


def _fr_is_config_complete_id(fr: mesh_pb2.FromRadio, ctx: Any) -> bool:
    return fr.config_complete_id != 0 and fr.config_complete_id == ctx.config_id


def _fr_has_channel(fr: mesh_pb2.FromRadio, _ctx: Any) -> bool:
    return fr.HasField("channel")


def _fr_has_packet(fr: mesh_pb2.FromRadio, _ctx: Any) -> bool:
    return fr.HasField("packet")


def _fr_has_log_record(fr: mesh_pb2.FromRadio, _ctx: Any) -> bool:
    return fr.HasField("log_record")


def _fr_has_queue_status(fr: mesh_pb2.FromRadio, _ctx: Any) -> bool:
    return fr.HasField("queueStatus")


def _fr_has_client_notification(fr: mesh_pb2.FromRadio, _ctx: Any) -> bool:
    return fr.HasField("clientNotification")


def _fr_has_mqtt_client_proxy_message(fr: mesh_pb2.FromRadio, _ctx: Any) -> bool:
    return fr.HasField("mqttClientProxyMessage")


def _fr_has_xmodem_packet(fr: mesh_pb2.FromRadio, _ctx: Any) -> bool:
    return fr.HasField("xmodemPacket")


def _fr_is_rebooted(fr: mesh_pb2.FromRadio, _ctx: Any) -> bool:
    return fr.HasField("rebooted") and fr.rebooted


def _fr_has_config_or_module_config(fr: mesh_pb2.FromRadio, _ctx: Any) -> bool:
    return fr.HasField("config") or fr.HasField("moduleConfig")


# Exported references for _select_from_radio_branch
_FR_HAS_MY_INFO = _fr_has_my_info
_FR_HAS_METADATA = _fr_has_metadata
_FR_HAS_NODE_INFO = _fr_has_node_info
_FR_IS_CONFIG_COMPLETE_ID = _fr_is_config_complete_id
_FR_HAS_CHANNEL = _fr_has_channel
_FR_HAS_PACKET = _fr_has_packet
_FR_HAS_LOG_RECORD = _fr_has_log_record
_FR_HAS_QUEUE_STATUS = _fr_has_queue_status
_FR_HAS_CLIENT_NOTIFICATION = _fr_has_client_notification
_FR_HAS_MQTT_CLIENT_PROXY_MESSAGE = _fr_has_mqtt_client_proxy_message
_FR_HAS_XMODEM_PACKET = _fr_has_xmodem_packet
_FR_IS_REBOOTED = _fr_is_rebooted
_FR_HAS_CONFIG_OR_MODULE_CONFIG = _fr_has_config_or_module_config


def _normalize_json_serializable(value: object) -> JSONValue:
    """Recursively normalize common non-JSON-native values into JSON-safe forms."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "base64:" + base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, dict):
        return {
            str(key): _normalize_json_serializable(inner_value)
            for key, inner_value in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_normalize_json_serializable(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _format_missing_node_num_error(destination_id: int | str) -> str:
    """Return a consistent error message for nodes missing numeric IDs."""
    return MISSING_NODE_NUM_ERROR_TEMPLATE.format(destination_id=destination_id)


def _format_node_not_found_in_db_error(destination_id: int | str) -> str:
    """Return a consistent error for node IDs missing from an available node DB."""
    return NODE_NOT_FOUND_IN_DB_ERROR_TEMPLATE.format(destination_id=destination_id)


def _format_node_db_unavailable_error(destination_id: int | str) -> str:
    """Return a consistent error for node IDs when node DB is unavailable."""
    return NODE_NOT_FOUND_DB_UNAVAILABLE_ERROR_TEMPLATE.format(
        destination_id=destination_id
    )


def _extract_hex_node_id_body(destination_id: str) -> str | None:
    """Return an 8-hex node-id body when ``destination_id`` matches supported forms."""
    candidate = destination_id
    if destination_id.startswith("!"):
        candidate = destination_id[1:]
    elif destination_id.startswith(("0x", "0X")):
        candidate = destination_id[2:]
    if len(candidate) != 8:
        return None
    if not all(ch in HEX_NODE_ID_TAIL_CHARS for ch in candidate):
        return None
    return candidate





def _logger_has_visible_info_handler(target_logger: logging.Logger) -> bool:
    """Return True when INFO logs are visibly emitted to stdout."""
    if target_logger.disabled:
        return False
    if target_logger.getEffectiveLevel() > logging.INFO:
        return False

    logger_to_check: logging.Logger | None = target_logger
    while logger_to_check:
        for handler in logger_to_check.handlers:
            if handler.level > logging.INFO:
                continue
            stream = getattr(handler, "stream", None)
            if stream is sys.stdout:
                return True
            console = getattr(handler, "console", None)
            console_file = getattr(console, "file", None)
            if console_file is sys.stdout:
                return True
        logger_to_check = logger_to_check.parent
    return False


def _emit_response_summary(message: str) -> None:
    """Emit short response summaries with legacy stdout fallback semantics."""
    logger.info("%s", message)
    if not _logger_has_visible_info_handler(logger):
        print(message)


class MeshInterface:  # pylint: disable=R0902
    """Interface class for meshtastic devices.

    Properties:

    isConnected
    nodes
    debugOut
    """

    class MeshInterfaceError(Exception):
        """An exception class for general mesh interface errors."""

        def __init__(self, message: str) -> None:
            """Create a MeshInterfaceError with a human-readable message.

            Parameters
            ----------
            message : str
                The error message describing the failure.
            """
            self.message = message
            super().__init__(self.message)

    def __init__(
        self,
        debugOut: IO[str] | Callable[[str], Any] | None = None,
        noProto: bool = False,
        noNodes: bool = False,
        timeout: float = 300.0,
    ) -> None:
        """Initialize the MeshInterface and configure runtime options.

        Parameters
        ----------
        debugOut : IO[str] | Callable[[str], Any] | None
            Destination for human-readable log lines; if provided the
            interface will publish device logs to this output. (Default value = None)
        noProto : bool
            If True, disable running the meshtastic protocol layer over the link
            (operate as a dumb serial client). (Default value = False)
        noNodes : bool
            If True, instruct the device not to send its node database on startup;
            only other configuration will be requested. (Default value = False)
        timeout : float
            Default timeout in seconds for operations that wait for replies.
        """
        self.debugOut = debugOut
        self.nodes: dict[str, dict[str, Any]] | None = None
        self.isConnected: threading.Event = threading.Event()
        self.noProto: bool = noProto
        self.localNode: meshtastic.node.Node = meshtastic.node.Node(
            self, -1, timeout=timeout
        )  # We fixup nodenum later
        self.myInfo: mesh_pb2.MyNodeInfo | None = None  # We don't have device info yet
        self.metadata: mesh_pb2.DeviceMetadata | None = (
            None  # We don't have device metadata yet
        )
        # ------------------------------------------------------------------
        # Locking contract for MeshInterface shared state.
        #
        # Shared mutable state is intentionally split by concern:
        # - _response_handlers_lock: responseHandlers map + response wait errors
        #   + _response_wait_acks + _active_wait_request_ids
        #   + _retired_wait_request_ids
        # - _heartbeat_lock: _closing, heartbeatTimer, _heartbeat_inflight,
        #   isConnected
        # - _packet_id_lock: currentPacketId generation
        # - _queue_lock: queue + queueStatus
        # - _node_db_lock: nodes/nodesByNum/_localChannels plus myInfo/metadata
        #   and local config/module config copies that are updated from RX thread.
        #
        # Deadlock-avoidance rule:
        # - Do not hold more than one MeshInterface lock at a time.
        # - If a future change must nest locks, establish and document a single
        #   global order in this block before introducing nested acquisition.
        #
        # Current implementation follows the no-nesting rule by acquiring one
        # lock, copying/snapshotting needed state, and doing I/O/callbacks after
        # releasing the lock.
        # ------------------------------------------------------------------
        # responseHandlers is shared by _add_response_handler (sendData path) and
        # _handle_packet_from_radio (receive thread). Use this lock to serialize
        # responseHandlers access across those call sites.
        self._response_handlers_lock = threading.RLock()
        self.responseHandlers: dict[int, ResponseHandler] = (
            {}
        )  # A map from request ID to the handler
        self._response_wait_errors: dict[tuple[str, int], str] = {}
        self._response_wait_acks: set[tuple[str, int]] = set()
        self._active_wait_request_ids: dict[str, set[int]] = {}
        self._retired_wait_request_ids: dict[str, dict[int, float]] = {}
        self.failure: BaseException | None = (
            None  # If we've encountered a fatal exception it will be kept here
        )
        self._timeout: Timeout = Timeout(maxSecs=timeout)
        self._acknowledgment: Acknowledgment = Acknowledgment()
        self.heartbeatTimer: threading.Timer | None = None
        self._heartbeat_lock = threading.RLock()
        # Track heartbeat sends that have passed the _closing gate but have not
        # finished I/O yet. close() waits on this to avoid post-close sends.
        self._heartbeat_inflight = 0
        self._heartbeat_idle_condition = threading.Condition(self._heartbeat_lock)
        self._packet_id_lock = threading.Lock()
        self._queue_lock = threading.RLock()
        # Guard node DB plus configuration state updated by the receive thread:
        # nodes/nodesByNum/_localChannels and myInfo/metadata/configId/localNode config copies.
        self._node_db_lock = threading.RLock()
        self._closing = False
        self.currentPacketId: int = random.randint(0, PACKET_ID_MASK)
        self.nodesByNum: dict[int, dict[str, Any]] | None = None
        self.noNodes: bool = noNodes
        self.configId: int | None = NODELESS_WANT_CONFIG_ID if noNodes else None
        self.gotResponse: bool = False  # used in gpio read
        self.mask: int | None = None  # legacy GPIO mask fallback (remote_hardware)
        self.queueStatus: mesh_pb2.QueueStatus | None = None
        self.queue: collections.OrderedDict[int, mesh_pb2.ToRadio | bool] = (
            collections.OrderedDict()
        )
        self._localChannels: list[channel_pb2.Channel] = []
        self._request_wait_runtime = _RequestWaitRuntime(
            lock=self._response_handlers_lock,
            get_response_handlers=lambda: self.responseHandlers,
            get_wait_errors=lambda: self._response_wait_errors,
            get_wait_acks=lambda: self._response_wait_acks,
            get_active_wait_request_ids=lambda: self._active_wait_request_ids,
            get_retired_wait_request_ids=lambda: self._retired_wait_request_ids,
            get_acknowledgment=lambda: self._acknowledgment,
            get_timeout=lambda: self._timeout,
            retired_wait_ttl_seconds=RETIRED_WAIT_REQUEST_ID_TTL_SECONDS,
        )
        self._queue_send_runtime = _QueueSendRuntime(
            lock=self._queue_lock,
            get_queue=lambda: self.queue,
            get_queue_status=lambda: self.queueStatus,
            set_queue_status=self._set_queue_status,
            queue_wait_delay_seconds=QUEUE_WAIT_DELAY_SECONDS,
        )
        self._from_radio_dispatch_map_cache: (
            dict[str, Callable[[_FromRadioContext], list[_PublicationIntent]]] | None
        ) = None
        self._receive_pipeline = ReceivePipeline(self)
        self._send_pipeline = SendPipeline(self)
        self._node_view = NodeView(self)

        # We could have just not passed in debugOut to MeshInterface, and instead told consumers to subscribe to
        # the meshtastic.log.line publish instead.  Alas though changing that now would be a breaking API change
        # for any external consumers of the library.
        if debugOut:
            pub.subscribe(MeshInterface._print_log_line, "meshtastic.log.line")

    def _set_queue_status(self, queue_status: mesh_pb2.QueueStatus | None) -> None:
        """Set the queueStatus attribute directly."""
        self.queueStatus = queue_status

    @staticmethod
    def _print_log_line(line: str, interface: Any) -> None:
        """Print one device log line to the configured debug output sink."""
        if print_color is not None and interface.debugOut == sys.stdout:
            if "DEBUG" in line:
                print_color.print(line, color=cast(Any, "cyan"))
            elif "INFO" in line:
                print_color.print(line, color="white")
            elif "WARN" in line:
                print_color.print(line, color="yellow")
            elif "ERR" in line:
                print_color.print(line, color="red")
            else:
                print_color.print(line)
        elif callable(interface.debugOut):
            interface.debugOut(line)
        elif hasattr(interface.debugOut, "write"):
            interface.debugOut.write(line + "\n")

    def _handle_log_line(self, line: str) -> None:
        """Publish a device log line after stripping a trailing newline."""
        if line.endswith("\n"):
            line = line[:-1]
        pub.sendMessage("meshtastic.log.line", line=line, interface=self)

    def _handle_log_record(self, record: mesh_pb2.LogRecord) -> None:
        """Handle a protobuf log record by forwarding its message text."""
        self._handle_log_line(record.message)

    def _prepare_for_connect(self) -> None:
        """Reset connection-lifecycle state so a new connect attempt can succeed.

        Called by transport connect implementations (e.g. StreamInterface.connect)
        before starting a fresh reader/handshake cycle.  Resets flags that were
        set by a previous close() so that the new attempt is not treated as a
        no-op or early-return path.
        """
        with self._heartbeat_lock:
            self._closing = False

    def close(self) -> None:
        """Shut down the interface and send a disconnect to the radio.

        Marks the interface as closing, cancels any scheduled heartbeat timer,
        waits for any in-flight heartbeat send to finish, then emits a
        disconnect message to the radio transport.
        """
        # Handle case where __init__ returned early before parent initialization
        heartbeat_lock = getattr(self, "_heartbeat_lock", None)
        heartbeat_idle_condition = getattr(self, "_heartbeat_idle_condition", None)
        if heartbeat_lock is not None:
            with heartbeat_lock:
                if self._closing:
                    return
                self._closing = True
                timer = self.heartbeatTimer
                self.heartbeatTimer = None
            if timer:
                timer.cancel()
            # Extra complexity is intentional: a callback can pass the _closing
            # check and begin sendHeartbeat() just before close() starts. Wait
            # until any such in-flight send completes so close() provides a
            # strong "no heartbeat after close returns" guarantee.
            if isinstance(heartbeat_idle_condition, threading.Condition):
                with heartbeat_lock:
                    while self._heartbeat_inflight > 0:
                        heartbeat_idle_condition.wait()
            if not self.noProto:
                try:
                    self._send_disconnect()
                except (OSError, MeshInterface.MeshInterfaceError):
                    logger.debug(
                        "Failed to send disconnect during close(); continuing shutdown.",
                        exc_info=True,
                    )
                except TypeError:
                    is_finalizing = getattr(sys, "is_finalizing", None)
                    if not (callable(is_finalizing) and is_finalizing()):
                        raise
                    logger.debug(
                        "Failed to send disconnect during interpreter finalization; continuing shutdown.",
                        exc_info=True,
                    )
        # debugOut is caller-owned (often shared via outer context managers);
        # do not close it here. Only clear our reference on shutdown.
        if hasattr(self, "debugOut"):
            self.debugOut = None

    def __enter__(self) -> "MeshInterface":
        """Enter a context for use with the with statement and return this MeshInterface instance.

        Returns
        -------
        'MeshInterface'
            This MeshInterface instance.
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        """Handle context-manager exit: log any exception information and close the interface.

        If an exception occurred within the with-block (exc_type is not None),
        any exception raised by close() is logged and suppressed so the original
        exception propagates. If no with-block exception occurred, close() exceptions
        are allowed to propagate normally.

        Parameters
        ----------
        exc_type : type[BaseException] | None
            The exception class if an exception was raised, otherwise None.
        exc_value : BaseException | None
            The exception instance if one was raised, otherwise None.
        trace : TracebackType | None
            The traceback object for the exception if present, otherwise None.
        """
        if exc_type is not None and exc_value is not None:
            if isinstance(exc_value, (SystemExit, KeyboardInterrupt)):
                logger.debug("Exiting (%s: %s)", exc_type.__name__, exc_value)
            else:
                logger.error(
                    "An exception of type %s with value %s has occurred",
                    exc_type,
                    exc_value,
                )
                if trace is not None:
                    logger.error("Traceback:\n%s", "".join(traceback.format_tb(trace)))
        try:
            self.close()
        except Exception:
            if exc_type is not None:
                logger.warning(
                    "close() failed while unwinding an existing exception.",
                    exc_info=True,
                )
            else:
                raise

    def showInfo(self, file: IO[str] | None = None) -> str:
        """Return a human-readable JSON summary of the mesh interface.

        Delegates to self.node_view.showInfo().
        """
        return self._node_view.showInfo(file)

    def showNodes(
        self, includeSelf: bool = True, showFields: list[str] | None = None
    ) -> str:
        """Produce a formatted table summarizing known mesh nodes.

        Delegates to self.node_view.showNodes().
        """
        return self._node_view.showNodes(includeSelf, showFields)

    def getNode(
        self,
        nodeId: str,
        requestChannels: bool = True,
        requestChannelAttempts: int = 3,
        timeout: float = 300.0,
    ) -> meshtastic.node.Node:
        """Get the Node object for the given node identifier.

        Delegates to self.node_view.getNode().
        """
        return self._node_view.getNode(
            nodeId, requestChannels, requestChannelAttempts, timeout
        )

    # pylint: disable=too-many-positional-arguments
    def sendText(
        self,
        text: str,
        destinationId: int | str = BROADCAST_ADDR,
        wantAck: bool = False,
        wantResponse: bool = False,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        channelIndex: int = 0,
        portNum: portnums_pb2.PortNum.ValueType = portnums_pb2.PortNum.TEXT_MESSAGE_APP,
        replyId: int | None = None,
        hopLimit: int | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Send UTF-8 text to a node (or broadcast) and return the transmitted MeshPacket.

        Parameters
        ----------
        text : str
            Message text to send; encoded as UTF-8 for transport.
        destinationId : int | str
            Target node as numeric node number or node ID string; use BROADCAST_ADDR
            to send to all nodes. (Default value = BROADCAST_ADDR)
        replyId : int | None
            If provided, marks this packet as a response to the given message ID. (Default value = None)
        wantAck : bool
            If True, request transport-level acknowledgment. (Default value = False)
        wantResponse : bool
            If True, request an application-level response. (Default value = False)
        onResponse : Callable[[dict[str, Any]], Any] | None
            Optional callback for the response. (Default value = None)
        channelIndex : int
            Channel index to send on. (Default value = 0)
        portNum : portnums_pb2.PortNum.ValueType
            Application port number for the text message. (Default value = portnums_pb2.PortNum.TEXT_MESSAGE_APP)
        hopLimit : int | None
            Optional hop limit override for the outgoing packet. (Default value = None)

        Returns
        -------
        mesh_pb2.MeshPacket
            The packet that was sent; its `id` field will be populated and can be used to track acknowledgments or naks.
        """
        return self._send_pipeline.sendText(
            text,
            destinationId=destinationId,
            wantAck=wantAck,
            wantResponse=wantResponse,
            onResponse=onResponse,
            channelIndex=channelIndex,
            portNum=portNum,
            replyId=replyId,
            hopLimit=hopLimit,
        )

    # pylint: disable=too-many-positional-arguments
    def sendAlert(
        self,
        text: str,
        destinationId: int | str = BROADCAST_ADDR,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        channelIndex: int = 0,
        hopLimit: int | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Send a high-priority alert text to a node, which may trigger special notifications on clients.

        Parameters
        ----------
        text : str
            Alert text to send.
        destinationId : int | str
            Node ID or node number to receive the alert (defaults to broadcast).
        onResponse : Callable[[dict[str, Any]], Any] | None
            Optional callback invoked if a response is received for this message. (Default value = None)
        channelIndex : int
            Channel index to use when sending. (Default value = 0)
        hopLimit : int | None
            Optional hop limit override for the outgoing packet. (Default value = None)

        Returns
        -------
        mesh_pb2.MeshPacket
            The sent mesh packet with its `id` populated.
        """
        return self._send_pipeline.sendAlert(
            text,
            destinationId=destinationId,
            onResponse=onResponse,
            channelIndex=channelIndex,
            hopLimit=hopLimit,
        )

    def sendMqttClientProxyMessage(self, topic: str, data: bytes) -> None:
        """Send an MQTT client-proxy message through the radio.

        Parameters
        ----------
        topic : str
            MQTT topic to forward.
        data : bytes
            MQTT payload to forward.
        """
        self._send_pipeline.sendMqttClientProxyMessage(topic, data)

    def sendData(  # pylint: disable=R0913,too-many-positional-arguments
        self,
        data: "PayloadData",
        destinationId: int | str = BROADCAST_ADDR,
        portNum: portnums_pb2.PortNum.ValueType = portnums_pb2.PortNum.PRIVATE_APP,
        wantAck: bool = False,
        wantResponse: bool = False,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        onResponseAckPermitted: bool = False,
        channelIndex: int = 0,
        hopLimit: int | None = None,
        pkiEncrypted: bool = False,
        publicKey: bytes | None = None,
        priority: mesh_pb2.MeshPacket.Priority.ValueType = mesh_pb2.MeshPacket.Priority.RELIABLE,
        replyId: int | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Send a payload to a mesh node.

        Parameters
        ----------
        data : Any
            Payload to send; protobuf messages are serialized to bytes.
        destinationId : int | str
            Destination node identifier (node id string, numeric node number,
            `BROADCAST_ADDR`, or `LOCAL_ADDR`).
        portNum : portnums_pb2.PortNum.ValueType
            Application port number for the payload.
        wantAck : bool
            Request link-layer ACK for this packet.
        wantResponse : bool
            Register a response handler for this packet.
        onResponse : Callable[[dict[str, Any]], Any] | None
            Optional callback invoked for matching responses.
        onResponseAckPermitted : bool
            Whether ACK-only responses should trigger `onResponse`.
        channelIndex : int
            Channel index used to transmit the packet.
        hopLimit : int | None
            Optional hop-limit override for the packet.
        pkiEncrypted : bool
            Whether to request PKI encryption for this payload.
        publicKey : bytes | None
            Optional destination public key used for PKI encryption.
        priority : mesh_pb2.MeshPacket.Priority.ValueType
            Mesh packet priority for retransmission behavior.
        replyId : int | None
            Optional mesh packet id to set in `reply_id`.

        Returns
        -------
        mesh_pb2.MeshPacket
            The packet object that was enqueued for transmission.

        Notes
        -----
        Request-scoped wait/error bookkeeping is intentionally implemented in
        `_send_data_with_wait()` and is not part of the public `sendData()`
        contract.
        """
        return self._send_pipeline.sendData(
            data,
            destinationId=destinationId,
            portNum=portNum,
            wantAck=wantAck,
            wantResponse=wantResponse,
            onResponse=onResponse,
            onResponseAckPermitted=onResponseAckPermitted,
            channelIndex=channelIndex,
            hopLimit=hopLimit,
            pkiEncrypted=pkiEncrypted,
            publicKey=publicKey,
            priority=priority,
            replyId=replyId,
        )

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    def _send_data_with_wait(
        self,
        data: "PayloadData",
        destinationId: int | str = BROADCAST_ADDR,
        portNum: portnums_pb2.PortNum.ValueType = portnums_pb2.PortNum.PRIVATE_APP,
        *,
        wantAck: bool = False,
        wantResponse: bool = False,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        onResponseAckPermitted: bool = False,
        channelIndex: int = 0,
        hopLimit: int | None = None,
        pkiEncrypted: bool = False,
        publicKey: bytes | None = None,
        priority: mesh_pb2.MeshPacket.Priority.ValueType = mesh_pb2.MeshPacket.Priority.RELIABLE,
        replyId: int | None = None,
        response_wait_attr: str | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Send payload data while optionally pre-registering scoped wait bookkeeping."""
        return self._send_pipeline._send_data_with_wait(
            data,
            destinationId=destinationId,
            portNum=portNum,
            wantAck=wantAck,
            wantResponse=wantResponse,
            onResponse=onResponse,
            onResponseAckPermitted=onResponseAckPermitted,
            channelIndex=channelIndex,
            hopLimit=hopLimit,
            pkiEncrypted=pkiEncrypted,
            publicKey=publicKey,
            priority=priority,
            replyId=replyId,
            response_wait_attr=response_wait_attr,
        )

    def _add_response_handler(
        self,
        requestId: int,
        callback: Callable[[dict[str, Any]], Any],
        ackPermitted: bool = False,
    ) -> None:
        """Register a response callback for a specific request identifier."""
        self._send_pipeline._add_response_handler(
            requestId,
            callback,
            ackPermitted=ackPermitted,
        )

    # pylint: disable=too-many-positional-arguments
    def _send_packet(
        self,
        meshPacket: mesh_pb2.MeshPacket,
        destinationId: int | str = BROADCAST_ADDR,
        wantAck: bool = False,
        hopLimit: int | None = None,
        pkiEncrypted: bool | None = False,
        publicKey: bytes | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Send a MeshPacket to a specific node or broadcast."""
        return self._send_pipeline._send_packet(
            meshPacket=meshPacket,
            destinationId=destinationId,
            wantAck=wantAck,
            hopLimit=hopLimit,
            pkiEncrypted=pkiEncrypted,
            publicKey=publicKey,
        )

    # pylint: disable=too-many-positional-arguments
    def _sendPacket(
        self,
        meshPacket: mesh_pb2.MeshPacket,
        destinationId: int | str = BROADCAST_ADDR,
        wantAck: bool = False,
        hopLimit: int | None = None,
        pkiEncrypted: bool | None = False,
        publicKey: bytes | None = None,
    ) -> mesh_pb2.MeshPacket:
        """COMPAT_STABLE_SHIM: Alias for `_send_packet`."""
        return self._send_packet(
            meshPacket=meshPacket,
            destinationId=destinationId,
            wantAck=wantAck,
            hopLimit=hopLimit,
            pkiEncrypted=pkiEncrypted,
            publicKey=publicKey,
        )

    # pylint: disable=too-many-positional-arguments
    def sendPosition(
        self,
        latitude: float = 0.0,
        longitude: float = 0.0,
        altitude: int = 0,
        destinationId: int | str = BROADCAST_ADDR,
        wantAck: bool = False,
        wantResponse: bool = False,
        channelIndex: int = 0,
        hopLimit: int | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Send the device's position to a specific node or to broadcast.

        Parameters
        ----------
        latitude : float
            Latitude in degrees; if 0.0 the latitude field is omitted. (Default value = 0.0)
        longitude : float
            Longitude in degrees; if 0.0 the longitude field is omitted. (Default value = 0.0)
        altitude : int
            Altitude in meters; if 0 the altitude field is omitted. (Default value = 0)
        destinationId : int | str
            Destination address or node ID; defaults to broadcast.
        wantAck : bool
            Request an acknowledgment from the recipient. (Default value = False)
        wantResponse : bool
            If True, blocks until a position response is received. (Default value = False)
        channelIndex : int
            Channel index to send the packet on. (Default value = 0)
        hopLimit : int | None
            Optional hop limit override for the outgoing packet. (Default value = None)

        Returns
        -------
        mesh_pb2.MeshPacket
            The sent packet with its `id` populated.
        """
        return self._send_pipeline.sendPosition(
            latitude=latitude,
            longitude=longitude,
            altitude=altitude,
            destinationId=destinationId,
            wantAck=wantAck,
            wantResponse=wantResponse,
            channelIndex=channelIndex,
            hopLimit=hopLimit,
        )

    # These thin wrappers intentionally mirror SendPipeline helper methods to
    # preserve historical MeshInterface compatibility entrypoints.
    @staticmethod
    def _extract_request_id_from_packet(packet: dict[str, Any]) -> int | None:
        """Return decoded requestId as an int when present and valid."""
        return _pipeline_extract_request_id_from_packet(packet)

    @staticmethod
    def _extract_request_id_from_sent_packet(packet: object) -> int | None:
        """Return sent packet id when present and positive."""
        return _pipeline_extract_request_id_from_sent_packet(packet)

    def _clear_wait_error(
        self,
        acknowledgment_attr: str,
        request_id: int | None = None,
        *,
        clear_scoped: bool = True,
    ) -> None:
        """Clear wait error state for an attribute and optional request id."""
        self._send_pipeline._clear_wait_error(
            acknowledgment_attr,
            request_id=request_id,
            clear_scoped=clear_scoped,
        )

    def _prune_retired_wait_request_ids_locked(
        self, acknowledgment_attr: str
    ) -> dict[int, float]:
        """Prune expired retired request ids for a wait attribute.

        Notes
        -----
        Must be called while holding `_response_handlers_lock`.
        """
        return self._send_pipeline._prune_retired_wait_request_ids_locked(
            acknowledgment_attr
        )

    def _set_wait_error(
        self,
        acknowledgment_attr: str,
        message: str,
        *,
        request_id: int | None = None,
    ) -> None:
        """Record a wait error and wake the matching waiter."""
        self._send_pipeline._set_wait_error(
            acknowledgment_attr,
            message,
            request_id=request_id,
        )

    def _mark_wait_acknowledged(
        self, acknowledgment_attr: str, *, request_id: int | None = None
    ) -> None:
        """Set acknowledgment flag for the matching request scope."""
        self._send_pipeline._mark_wait_acknowledged(
            acknowledgment_attr,
            request_id=request_id,
        )

    def _raise_wait_error_if_present(
        self, acknowledgment_attr: str, request_id: int | None = None
    ) -> None:
        """Raise and clear any pending wait error for the given wait scope."""
        self._send_pipeline._raise_wait_error_if_present(
            acknowledgment_attr,
            request_id=request_id,
        )

    def _retire_wait_request(
        self, acknowledgment_attr: str, request_id: int | None = None
    ) -> None:
        """Retire response handler and wait bookkeeping for a completed wait."""
        self._send_pipeline._retire_wait_request(
            acknowledgment_attr,
            request_id=request_id,
        )

    def _wait_for_request_ack(
        self,
        acknowledgment_attr: str,
        request_id: int,
        *,
        timeout_seconds: float,
    ) -> bool:
        """Wait for a request-scoped acknowledgment flag.

        Parameters
        ----------
        acknowledgment_attr : str
            Acknowledgment attribute namespace (for example, "receivedTelemetry").
        request_id : int
            Packet request id that must acknowledge before timeout.
        timeout_seconds : float
            Maximum seconds to wait.

        Returns
        -------
        bool
            `True` when the scoped acknowledgment was observed before timeout,
            otherwise `False`.
        """
        return self._send_pipeline._wait_for_request_ack(
            acknowledgment_attr,
            request_id,
            timeout_seconds=timeout_seconds,
        )

    def _record_routing_wait_error(
        self,
        *,
        acknowledgment_attr: str,
        routing_error_reason: str | None,
        request_id: int | None = None,
    ) -> None:
        """Record non-success routing responses into shared wait state."""
        self._send_pipeline._record_routing_wait_error(
            acknowledgment_attr=acknowledgment_attr,
            routing_error_reason=routing_error_reason,
            request_id=request_id,
        )

    def onResponsePosition(self, p: dict[str, Any]) -> None:
        """Process a position response packet and emit a concise human-readable summary.

        Marks the interface's position acknowledgment as received, parses the Position
        protobuf from the packet payload, and emits latitude/longitude (degrees),
        altitude (meters) when present, and precision information. When INFO logging
        is configured, the summary is logged; otherwise it is printed to stdout for
        backward compatibility. Routing replies are recorded into shared wait
        state so waiters can surface routing failures consistently.

        Parameters
        ----------
        p : dict[str, Any]
            Decoded packet dictionary expected to contain at minimum
            `decoded["portnum"]` and `decoded["payload"]`. For routing error checks
            the nested `decoded["routing"]["errorReason"]` may be present.

        """
        self._send_pipeline.onResponsePosition(p)

    def sendTraceRoute(
        self, dest: int | str, hopLimit: int, channelIndex: int = 0
    ) -> None:
        """Initiate a traceroute request toward a destination node and wait for responses.

        Sends a RouteDiscovery to the specified destination using the given channel and waits for
        traceroute responses; the waiting period is extended based on the current node count up to
        the provided hopLimit.

        Parameters
        ----------
        dest : int | str
            Destination node (numeric node number, node ID string, or broadcast/local constants).
        hopLimit : int
            Maximum number of hops to probe for the traceroute.
        channelIndex : int
            Channel index to use for transmission. (Default value = 0)

        Raises
        ------
        MeshInterfaceError
            If waiting for traceroute responses times out or the operation fails.
        """
        self._send_pipeline.sendTraceRoute(dest, hopLimit, channelIndex=channelIndex)

    def onResponseTraceRoute(self, p: dict[str, Any]) -> None:
        """Emit human-readable traceroute results from a RouteDiscovery payload.

        Parameters
        ----------
        p : dict[str, Any]
            The traceroute response packet.
        """
        self._send_pipeline.onResponseTraceRoute(p)

    # pylint: disable=too-many-positional-arguments
    def sendTelemetry(
        self,
        destinationId: int | str = BROADCAST_ADDR,
        wantResponse: bool = False,
        channelIndex: int = 0,
        telemetryType: TelemetryType | str = DEFAULT_TELEMETRY_TYPE,
        hopLimit: int | None = None,
    ) -> None:
        """Send a telemetry message to a node or broadcast and optionally wait for a telemetry response.

        Parameters
        ----------
        destinationId : int | str
            Numeric node id, node id string, or the broadcast address to receive the telemetry. (Default value = BROADCAST_ADDR)
        wantResponse : bool
            If true, register a telemetry response handler and wait for the corresponding response. (Default value = False)
        channelIndex : int
            Channel index to use for the outgoing packet. (Default value = 0)
        telemetryType : str
            Telemetry payload to send. Supported values: "environment_metrics", "air_quality_metrics",
            "power_metrics", "local_stats", and "device_metrics". When "device_metrics" is selected and local device
            metrics are available, the payload is populated from the local node's cached device metrics. (Default value = 'device_metrics')
        hopLimit : int | None
            Optional hop limit override for the outgoing packet. (Default value = None)
        """
        self._send_pipeline.sendTelemetry(
            destinationId=destinationId,
            wantResponse=wantResponse,
            channelIndex=channelIndex,
            telemetryType=telemetryType,
            hopLimit=hopLimit,
        )

    def onResponseTelemetry(self, p: dict[str, Any]) -> None:
        """Handle an incoming telemetry response: mark telemetry as received and emit human-readable telemetry values.

        Parameters
        ----------
        p : dict[str, Any]
            Decoded packet dictionary produced by _handle_packet_from_radio.
        """
        self._send_pipeline.onResponseTelemetry(p)

    def onResponseWaypoint(self, p: dict[str, Any]) -> None:
        """Handle a waypoint response or routing error contained in a received packet.

        Parameters
        ----------
        p : dict[str, Any]
            Packet dictionary containing a 'decoded' mapping.
        """
        self._send_pipeline.onResponseWaypoint(p)

    def sendWaypoint(  # pylint: disable=R0913,too-many-positional-arguments
        self,
        name: str,
        description: str,
        icon: int | str,
        expire: int,
        waypoint_id: int | None = None,
        latitude: float = 0.0,
        longitude: float = 0.0,
        destinationId: int | str = BROADCAST_ADDR,
        wantAck: bool = True,
        wantResponse: bool = False,
        channelIndex: int = 0,
        hopLimit: int | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Send a waypoint to a node or broadcast.

        Parameters
        ----------
        name : str
            Human-readable waypoint name.
        description : str
            Text description for the waypoint.
        icon : int | str
            Icon identifier (will be converted to an integer).
        expire : int
            Expiration time for the waypoint, in seconds.
        waypoint_id : int | None
            Waypoint identifier to use; if None a pseudo-random id is generated. (Default value = None)
        latitude : float
            Latitude in decimal degrees; included only when not 0.0. (Default value = 0.0)
        longitude : float
            Longitude in decimal degrees; included only when not 0.0. (Default value = 0.0)
        destinationId : int | str
            Destination node id or special address (broadcast/local). (Default value = BROADCAST_ADDR)
        wantAck : bool
            If True, request an acknowledgement for the sent packet. (Default value = True)
        wantResponse : bool
            If True, wait for and process a waypoint response before returning. (Default value = False)
        channelIndex : int
            Channel index to send the waypoint on. (Default value = 0)
        hopLimit : int | None
            Optional hop limit override for the outgoing packet. (Default value = None)

        Returns
        -------
        mesh_pb2.MeshPacket
            The MeshPacket that was sent; its `id` is populated for tracking.
        """
        return self._send_pipeline.sendWaypoint(
            name=name,
            description=description,
            icon=icon,
            expire=expire,
            waypoint_id=waypoint_id,
            latitude=latitude,
            longitude=longitude,
            destinationId=destinationId,
            wantAck=wantAck,
            wantResponse=wantResponse,
            channelIndex=channelIndex,
            hopLimit=hopLimit,
        )

    # pylint: disable=too-many-positional-arguments
    def deleteWaypoint(
        self,
        waypoint_id: int,
        destinationId: int | str = BROADCAST_ADDR,
        wantAck: bool = True,
        wantResponse: bool = False,
        channelIndex: int = 0,
        hopLimit: int | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Delete a waypoint by sending a Waypoint message with expire=0 to a destination.

        Parameters
        ----------
        waypoint_id : int
            The waypoint's identifier to delete (the waypoint id, not a packet id).
        destinationId : int | str
            Destination node numeric id or address string; defaults to broadcast.
        wantAck : bool
            Request an acknowledgement for the transmitted packet when True. (Default value = True)
        wantResponse : bool
            If True, wait for and process a waypoint response before returning. (Default value = False)
        channelIndex : int
            Channel index to send the packet on. (Default value = 0)
        hopLimit : int | None
            Optional hop limit override for the outgoing packet. (Default value = None)

        Returns
        -------
        mesh_pb2.MeshPacket
            The MeshPacket that was sent; its `id` field is populated and can be used to track acknowledgements.
        """
        return self._send_pipeline.deleteWaypoint(
            waypoint_id=waypoint_id,
            destinationId=destinationId,
            wantAck=wantAck,
            wantResponse=wantResponse,
            channelIndex=channelIndex,
            hopLimit=hopLimit,
        )

    def waitForConfig(self) -> None:
        """Block until the radio configuration and the local node's configuration are available.

        Raises
        ------
        MeshInterface.MeshInterfaceError
            If the configuration is not received before the interface timeout.
        """
        self._send_pipeline.waitForConfig()

    def waitForAckNak(self) -> None:
        """Wait until an acknowledgement (ACK) or negative acknowledgement (NAK) is received or the wait times out.

        Raises
        ------
        MeshInterface.MeshInterfaceError
            If waiting times out before an ACK/NAK is received.
        """
        self._send_pipeline.waitForAckNak()

    def waitForTraceRoute(
        self, waitFactor: float, request_id: int | None = None
    ) -> None:
        """Wait for trace route completion using the configured timeout.

        Delegates to self._send_pipeline.waitForTraceRoute().

        Parameters
        ----------
        waitFactor : float
            Multiplier applied to the base trace-route timeout to extend the wait period.
        request_id : int | None
            Optional request id used to scope wait/error handling to a specific
            traceroute request.

        Raises
        ------
        MeshInterface.MeshInterfaceError
            If the wait times out before a traceroute response is received.
        """
        self._send_pipeline.waitForTraceRoute(waitFactor, request_id=request_id)

    def waitForTelemetry(self, request_id: int | None = None) -> None:
        """Wait for a telemetry response or until the configured timeout elapses.

        Delegates to self._send_pipeline.waitForTelemetry().

        Parameters
        ----------
        request_id : int | None
            Optional request id used to scope wait/error handling to a specific
            telemetry request.

        Raises
        ------
        MeshInterface.MeshInterfaceError
            If a telemetry response is not received before the configured timeout.
        """
        self._send_pipeline.waitForTelemetry(request_id=request_id)

    def waitForPosition(self, request_id: int | None = None) -> None:
        """Block until a position acknowledgment is received.

        Delegates to self._send_pipeline.waitForPosition().

        Parameters
        ----------
        request_id : int | None
            Optional request id used to scope wait/error handling to a specific
            position request.

        Raises
        ------
        MeshInterface.MeshInterfaceError
            If waiting for the position times out.
        """
        self._send_pipeline.waitForPosition(request_id=request_id)

    def waitForWaypoint(self, request_id: int | None = None) -> None:
        """Block until a waypoint acknowledgment is received.

        Delegates to self._send_pipeline.waitForWaypoint().

        Parameters
        ----------
        request_id : int | None
            Optional request id used to scope wait/error handling to a specific
            waypoint request.

        Raises
        ------
        MeshInterface.MeshInterfaceError
            If the wait times out before a waypoint acknowledgment is received.
        """
        self._send_pipeline.waitForWaypoint(request_id=request_id)

    def getMyNodeInfo(self) -> dict[str, Any] | None:
        """Get the stored node-info dictionary for the local node.

        Delegates to self.node_view.getMyNodeInfo().
        """
        return self._node_view.getMyNodeInfo()

    def getMyUser(self) -> dict[str, Any] | None:
        """Get the user information for the local node.

        Delegates to self.node_view.getMyUser().
        """
        return self._node_view.getMyUser()

    def getLongName(self) -> str | None:
        """Get the local user's configured long name.

        Delegates to self.node_view.getLongName().
        """
        return self._node_view.getLongName()

    def getShortName(self) -> str | None:
        """Get the local node user's short name.

        Delegates to self.node_view.getShortName().
        """
        return self._node_view.getShortName()

    def getPublicKey(self) -> bytes | None:
        """Return the local node's public key if available.

        Delegates to self.node_view.getPublicKey().
        """
        return self._node_view.getPublicKey()

    def getCannedMessage(self) -> str | None:
        """Retrieve the canned (predefined) message configured for the local node.

        Delegates to self.node_view.getCannedMessage().
        """
        return self._node_view.getCannedMessage()

    def getRingtone(self) -> str | None:
        """Get the local node's ringtone name or identifier.

        Delegates to self.node_view.getRingtone().
        """
        return self._node_view.getRingtone()

    def _wait_connected(self, timeout: float = 30.0) -> None:
        """Wait until the interface is marked connected or the timeout elapses.

        Parameters
        ----------
        timeout : float
            Maximum seconds to wait for the connection. (Default value = 30.0)

        Raises
        ------
        MeshInterface.MeshInterfaceError
            If waiting timed out.
        Exception
            Re-raises a stored fatal exception if one occurred during connection.
        """
        if not self.noProto:
            deadline = time.monotonic() + timeout
            abort_check = getattr(self, "_connect_wait_should_abort", None)
            connected = False
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                if self.isConnected.wait(min(CONNECT_WAIT_POLL_SECONDS, remaining)):
                    connected = True
                    break
                if self.failure is not None:
                    raise self.failure
                if callable(abort_check):
                    abort_reason = abort_check()  # pylint: disable=not-callable
                    if abort_reason:
                        logger.warning(
                            "Connection wait aborted: %s (isConnected=%s, failure=%r, last_disconnect_source=%s)",
                            abort_reason,
                            self.isConnected.is_set(),
                            self.failure,
                            getattr(self, "_last_disconnect_source", "unknown"),
                        )
                        raise MeshInterface.MeshInterfaceError(abort_reason)
            if not connected:
                if self.failure is not None:
                    raise self.failure
                if callable(abort_check):
                    abort_reason = abort_check()  # pylint: disable=not-callable
                    if abort_reason:
                        abort_reason_str = str(abort_reason)
                        logger.warning(
                            "Connection wait timed out but abort reason detected: %s (isConnected=%s, failure=%r, last_disconnect_source=%s)",
                            abort_reason_str,
                            self.isConnected.is_set(),
                            self.failure,
                            getattr(self, "_last_disconnect_source", "unknown"),
                        )
                        raise MeshInterface.MeshInterfaceError(abort_reason_str)
                logger.error(
                    "Timed out waiting for connection completion (isConnected=%s, failure=%r, last_disconnect_source=%s)",
                    self.isConnected.is_set(),
                    self.failure,
                    getattr(self, "_last_disconnect_source", "unknown"),
                )
                raise MeshInterface.MeshInterfaceError(
                    "Timed out waiting for connection completion"
                )

        # If we failed while connecting, raise the connection to the client
        if self.failure is not None:
            raise self.failure

    def _generate_packet_id(self) -> int:
        """Generate a new 32-bit packet identifier combining a 10-bit monotonic counter with randomized upper bits.

        Returns
        -------
        packet_id : int
            New packet id where the low 10 bits are a monotonic counter and the remaining bits are randomized.

        Raises
        ------
        MeshInterface.MeshInterfaceError
            If `currentPacketId` is None and a packet id cannot be generated.
        """
        with self._packet_id_lock:
            if self.currentPacketId is None:
                raise MeshInterface.MeshInterfaceError(
                    "Not connected yet, can not generate packet"
                )
            next_packet_id = (self.currentPacketId + 1) & PACKET_ID_MASK
            next_packet_id = (
                next_packet_id & PACKET_ID_COUNTER_MASK
            )  # Keep only low 10-bit counter (clear upper 22 bits)
            random_part = (
                random.randint(0, PACKET_ID_RANDOM_MAX)
                << PACKET_ID_RANDOM_SHIFT_BITS  # noqa: S311
            ) & PACKET_ID_MASK  # generate number with 10 zeros at end
            self.currentPacketId = next_packet_id | random_part  # combine
            return self.currentPacketId

    # COMPAT_STABLE_SHIM: historical private camelCase helper used by external integrations.
    def _generatePacketId(self) -> int:
        """Backward-compatible alias for `_generate_packet_id`."""
        return self._generate_packet_id()

    def _disconnected(self) -> None:
        """Mark the interface as disconnected and publish meshtastic.connection.lost once per connection.

        Clears the internal connected flag and publishes a lost-connection
        notification only if the interface was previously connected. This keeps
        shutdown/retry paths from publishing duplicate "lost" events.
        """
        with self._heartbeat_lock:
            was_connected = self.isConnected.is_set()
            self.isConnected.clear()
        if was_connected:
            publishingThread.queueWork(
                lambda: pub.sendMessage("meshtastic.connection.lost", interface=self)
            )

    def sendHeartbeat(self) -> None:
        """Send a heartbeat message to the radio to indicate the interface is alive."""
        p = mesh_pb2.ToRadio()
        p.heartbeat.CopyFrom(mesh_pb2.Heartbeat())
        self._send_to_radio(p)

    def _start_heartbeat(self) -> None:
        """Start a recurring daemon timer that sends a heartbeat to the radio every 300 seconds.

        Schedules and runs the first heartbeat immediately and then re-schedules a daemon
        threading.Timer to invoke subsequent heartbeats at a fixed 300-second interval. The
        scheduler respects shutdown by checking self._closing and uses self._heartbeat_lock to
        avoid scheduling or storing timers after shutdown begins. Heartbeat sends are tracked
        as in-flight so close() can wait for quiescence and guarantee no post-close sends. The
        actual call to self.sendHeartbeat() is still performed outside the lock.
        """

        def callback() -> None:
            """Schedule the next heartbeat and emit one unless the interface is shutting down.

            Schedules a daemon timer to re-run this callback after a fixed interval and then calls
            self.sendHeartbeat(). If self._closing is set, no timer is scheduled and no heartbeat is sent.
            """
            interval = HEARTBEAT_INTERVAL_SECONDS
            logger.debug("Sending heartbeat, interval %s seconds", interval)
            with self._heartbeat_lock:
                # Keep timer update/start in one critical section for simpler
                # state reasoning while still honoring shutdown.
                if self._closing:
                    return
                self.heartbeatTimer = None
                timer = threading.Timer(interval, callback)
                # Heartbeat maintenance should never prevent process shutdown.
                timer.daemon = True
                self.heartbeatTimer = timer
                timer.start()
                # Mark this send as in-flight before releasing the lock so
                # close() cannot miss it when establishing shutdown quiescence.
                self._heartbeat_inflight += 1
            # sendHeartbeat() is intentionally outside the lock to avoid
            # holding the lock during I/O. close() handles timer cancellation.
            try:
                self.sendHeartbeat()
            finally:
                with self._heartbeat_lock:
                    self._heartbeat_inflight -= 1
                    if self._heartbeat_inflight == 0:
                        self._heartbeat_idle_condition.notify_all()

        callback()  # run our periodic callback now, it will make another timer if necessary

    def _connected(self) -> None:
        """Mark the interface as connected, start the heartbeat timer, and publish a connection-established event.

        If the interface is shutting down, do nothing. Otherwise set the
        internal connected event, start periodic heartbeats, and enqueue a
        "meshtastic.connection.established" publication.
        """
        start_heartbeat = False
        with self._heartbeat_lock:
            if self._closing:
                logger.debug("Skipping _connected(): interface is closing")
                return
            # (because I'm lazy) _connected might be called when remote Node
            # objects complete their config reads, don't generate redundant isConnected
            # for the local interface
            if not self.isConnected.is_set():
                self.isConnected.set()
                start_heartbeat = True
        if start_heartbeat:
            self._start_heartbeat()
        # Check _closing again before publishing to avoid race with close()
        with self._heartbeat_lock:
            # Publish once per disconnected->connected transition.
            should_publish = start_heartbeat and not self._closing
        if should_publish:
            publishingThread.queueWork(
                lambda: pub.sendMessage(
                    "meshtastic.connection.established", interface=self
                )
            )

    def _start_config(self) -> None:
        """Initialize internal node/config state and request the radio's configuration.

        Resets local state used during configuration (myInfo, nodes, nodesByNum, and local channel list),
        allocates a new non-conflicting configId when appropriate, and sends a ToRadio message
        requesting configuration using the chosen configId.
        """
        with self._node_db_lock:
            self.myInfo = None
            self.nodes = {}  # nodes keyed by ID
            self.nodesByNum = {}  # nodes keyed by nodenum
            self._localChannels = (
                []
            )  # empty until we start getting channels pushed from the device (during config)
            config_id = self.configId
            if config_id is None or not self.noNodes:
                # Keep config_complete_id zero reserved as an unset sentinel.
                config_id = random.randint(1, PACKET_ID_MASK)
                if config_id == NODELESS_WANT_CONFIG_ID:
                    config_id = config_id + 1
                self.configId = config_id

        startConfig = mesh_pb2.ToRadio()
        startConfig.want_config_id = config_id
        self._send_to_radio(startConfig)

    def _send_disconnect(self) -> None:
        """Notify the radio device that this interface is disconnecting."""
        m = mesh_pb2.ToRadio()
        m.disconnect = True
        self._send_to_radio(m)

    def _queue_has_free_space(self) -> bool:
        # We never got queueStatus, maybe the firmware is old
        """Indicate whether the cached transmit queue has free slots.

        Returns
        -------
        bool
            `True` if at least one free slot is available or the queue status is unknown, `False` otherwise.
        """
        return self._queue_send_runtime._has_free_space()

    def _queue_claim(self) -> None:
        """Decrement the cached transmit-queue free-slot counter when a packet is claimed.

        Does nothing if queue status information is not available.
        """
        self._queue_send_runtime._claim()

    def _queue_pop_for_send(self) -> tuple[int, mesh_pb2.ToRadio | bool] | None:
        """Atomically pop the next queued packet if TX queue state permits sending.

        Returns
        -------
        tuple[int, mesh_pb2.ToRadio | bool] | None
            The popped queue entry `(packet_id, payload)` when available and sendable,
            otherwise `None`.
        """
        return self._queue_send_runtime._pop_for_send()

    def _send_to_radio(self, toRadio: mesh_pb2.ToRadio) -> None:
        """Queue and transmit a ToRadio protobuf to the radio device.

        If the ToRadio has a MeshPacket in its `packet` field, that packet is enqueued and will be
        transmitted when TX queue space is available; otherwise the message is sent immediately.
        The method respects the interface's `noProto` setting (no transmission when disabled),
        may block while waiting for TX queue space, and preserves/requeues packets that remain unacknowledged.

        Parameters
        ----------
        toRadio : mesh_pb2.ToRadio
            The ToRadio protobuf to send; if it contains a `packet` field
            the contained MeshPacket will be queued for transmission.
        """
        if self.noProto:
            logger.warning(
                "Not sending packet because protocol use is disabled by noProto"
            )
            return

        self._queue_send_runtime._send_to_radio(
            toRadio,
            send_impl=self._send_to_radio_impl,
            pop_for_send=self._queue_pop_for_send,
            sleep_fn=time.sleep,
        )

    def _send_to_radio_impl(self, toRadio: mesh_pb2.ToRadio) -> None:
        """Transport hook that delivers a ToRadio protobuf to the radio device.

        Subclasses must override this method to perform the actual transmission; the base
        implementation logs an error when invoked.

        Parameters
        ----------
        toRadio : mesh_pb2.ToRadio
            Protobuf describing the action or packet to send to the radio.
        """
        logger.error("Subclass must provide toradio: %s", toRadio)

    def _handle_config_complete(self) -> None:
        """Finalize initial configuration by applying collected local channels and marking the interface as connected.

        Sets the local node's channels from the internally collected
        _localChannels and invokes _connected() to signal that configuration is
        complete and normal packet handling may begin.
        """
        # This is no longer necessary because the current protocol statemachine has already proactively sent us the locally visible channels
        # self.localNode.requestChannels()
        with self._node_db_lock:
            local_channels = list(self._localChannels)
        self.localNode.setChannels(local_channels)

        # the following should only be called after we have settings and channels
        self._connected()  # Tell everyone else we are ready to go

    def _handle_queue_status_from_radio(
        self, queueStatus: mesh_pb2.QueueStatus
    ) -> None:
        """Update internal transmit-queue state from a received QueueStatus message.

        Sets self.queueStatus and logs the reported free/total slots and packet id.
        If queueStatus.res is falsy, removes the entry for queueStatus.mesh_packet_id from
        self.queue; if no entry exists and mesh_packet_id is nonzero, records a False
        marker for that id to indicate an unexpected reply was observed.

        Parameters
        ----------
        queueStatus : mesh_pb2.QueueStatus
            An object (protobuf-like) with attributes `free`, `maxlen`,
            `res`, and `mesh_packet_id` describing the radio's transmit-queue state.
        """
        self._queue_send_runtime._handle_queue_status_from_radio(queueStatus)

    def _record_queue_status(self, queueStatus: mesh_pb2.QueueStatus) -> None:
        """Persist latest radio TX queue status under queue ownership."""
        self._queue_send_runtime._record_queue_status(queueStatus)

    def _correlate_queue_status_reply(self, queueStatus: mesh_pb2.QueueStatus) -> None:
        """Correlate queue reply IDs with local pending queue entries."""
        self._queue_send_runtime._correlate_queue_status_reply(queueStatus)
        # logger.warn("queue: " + " ".join(f'{k:08x}' for k in self.queue))

    def _handle_from_radio(self, fromRadioBytes: bytes) -> None:
        """Handle a raw FromRadio payload."""
        from_radio = self._parse_from_radio_bytes(fromRadioBytes)
        context = self._normalize_from_radio_message(from_radio)
        publication_intents = self._dispatch_from_radio_message(context)
        self._emit_publication_intents(publication_intents)

    def _parse_from_radio_bytes(self, from_radio_bytes: bytes) -> mesh_pb2.FromRadio:
        """Parse raw FromRadio bytes into protobuf form."""
        from_radio = mesh_pb2.FromRadio()
        frame_length = len(from_radio_bytes)
        frame_checksum = hashlib.sha256(from_radio_bytes).hexdigest()[:12]
        logger.debug(
            "Received FromRadio frame len=%d sha256=%s",
            frame_length,
            frame_checksum,
        )
        try:
            from_radio.ParseFromString(from_radio_bytes)
        except Exception:
            logger.exception(
                "Error while parsing FromRadio frame len=%d sha256=%s",
                frame_length,
                frame_checksum,
            )
            raise
        return from_radio

    def _normalize_from_radio_message(
        self, from_radio: mesh_pb2.FromRadio
    ) -> _FromRadioContext:
        """Normalize parsed FromRadio data for dispatch and mutation handlers."""
        logger.debug("Received from radio: %s", from_radio)
        with self._node_db_lock:
            config_id = self.configId
        return _FromRadioContext(
            message=from_radio,
            message_dict=_LazyMessageDict(from_radio),
            config_id=config_id,
        )

    def _dispatch_from_radio_message(
        self, context: _FromRadioContext
    ) -> list[_PublicationIntent]:
        """Dispatch normalized FromRadio payloads to dedicated branch handlers."""
        branch = self._select_from_radio_branch(context)
        if branch is None:
            logger.debug("Unexpected FromRadio payload")
            return []
        handler = self._from_radio_dispatch_map()[branch]
        return handler(context)

    def _select_from_radio_branch(self, context: _FromRadioContext) -> str | None:
        """Select the active FromRadio branch using the historical precedence order."""
        from_radio = context.message
        branches: list[
            tuple[
                Callable[[mesh_pb2.FromRadio, _FromRadioContext], bool],
                str,
            ]
        ] = [
            (_FR_HAS_MY_INFO, "my_info"),
            (_FR_HAS_METADATA, "metadata"),
            (_FR_HAS_NODE_INFO, "node_info"),
            (_FR_IS_CONFIG_COMPLETE_ID, "config_complete_id"),
            (_FR_HAS_CHANNEL, "channel"),
            (_FR_HAS_PACKET, "packet"),
            (_FR_HAS_LOG_RECORD, "log_record"),
            (_FR_HAS_QUEUE_STATUS, "queueStatus"),
            (_FR_HAS_CLIENT_NOTIFICATION, "clientNotification"),
            (_FR_HAS_MQTT_CLIENT_PROXY_MESSAGE, "mqttClientProxyMessage"),
            (_FR_HAS_XMODEM_PACKET, "xmodemPacket"),
            (_FR_IS_REBOOTED, "rebooted"),
            (_FR_HAS_CONFIG_OR_MODULE_CONFIG, "config_or_moduleConfig"),
        ]
        for predicate, branch_name in branches:
            if predicate(from_radio, context):
                return branch_name
        return None

    def _from_radio_dispatch_map(
        self,
    ) -> dict[str, Callable[[_FromRadioContext], list[_PublicationIntent]]]:
        """Return branch handlers for FromRadio dispatch."""
        if self._from_radio_dispatch_map_cache is None:
            self._from_radio_dispatch_map_cache = {
                "my_info": self._handle_from_radio_my_info,
                "metadata": self._handle_from_radio_metadata,
                "node_info": self._handle_from_radio_node_info,
                "config_complete_id": self._handle_from_radio_config_complete_id,
                "channel": self._handle_from_radio_channel,
                "packet": self._handle_from_radio_packet,
                "log_record": self._handle_from_radio_log_record,
                "queueStatus": self._handle_from_radio_queue_status,
                "clientNotification": self._handle_from_radio_client_notification,
                "mqttClientProxyMessage": self._handle_from_radio_mqtt_client_proxy_message,
                "xmodemPacket": self._handle_from_radio_xmodem_packet,
                "rebooted": self._handle_from_radio_rebooted,
                "config_or_moduleConfig": self._handle_from_radio_config_update,
            }
        return self._from_radio_dispatch_map_cache

    def _handle_from_radio_my_info(
        self, context: _FromRadioContext
    ) -> list[_PublicationIntent]:
        """Apply my_info updates to interface state."""
        from_radio = context.message
        with self._node_db_lock:
            my_info = mesh_pb2.MyNodeInfo()
            my_info.CopyFrom(from_radio.my_info)
            self.myInfo = my_info
            self.localNode.nodeNum = my_info.my_node_num
        logger.debug("Received myinfo: %s", stripnl(from_radio.my_info))
        return []

    def _handle_from_radio_metadata(
        self, context: _FromRadioContext
    ) -> list[_PublicationIntent]:
        """Apply metadata updates to interface state."""
        from_radio = context.message
        with self._node_db_lock:
            metadata = mesh_pb2.DeviceMetadata()
            metadata.CopyFrom(from_radio.metadata)
            self.metadata = metadata
        logger.debug("Received device metadata: %s", stripnl(from_radio.metadata))
        return []

    def _handle_from_radio_node_info(
        self, context: _FromRadioContext
    ) -> list[_PublicationIntent]:
        """Apply node_info updates and emit node-updated publication intents."""
        node_info = context.message_dict.get()["nodeInfo"]
        logger.debug("Received nodeinfo: %s", node_info)

        node = self._get_or_create_by_num(node_info["num"])
        with self._node_db_lock:
            node.update(node_info)
            try:
                node["position"] = self._fixup_position(node["position"])
            except KeyError:
                logger.debug("Node has no position key")

            # no longer necessary since we're mutating directly in nodesByNum via _get_or_create_by_num
            # self.nodesByNum[node["num"]] = node
            # Some nodes might not have user/ids assigned yet.
            # Keep nodes and nodesByNum mutation under the same lock so readers
            # never observe partially-updated node mappings.
            if "user" in node and "id" in node["user"] and self.nodes is not None:
                self.nodes[node["user"]["id"]] = node
            published_node = copy.deepcopy(node)

        return [
            self._publication_intent("meshtastic.node.updated", node=published_node),
        ]

    def _handle_from_radio_config_complete_id(
        self, context: _FromRadioContext
    ) -> list[_PublicationIntent]:
        """Handle config-complete correlation and startup completion."""
        logger.debug("Config complete ID %s", context.config_id)
        self._handle_config_complete()
        return []

    def _handle_from_radio_channel(
        self, context: _FromRadioContext
    ) -> list[_PublicationIntent]:
        """Handle incoming channel updates."""
        self._handle_channel(context.message.channel)
        return []

    def _handle_from_radio_packet(
        self, context: _FromRadioContext
    ) -> list[_PublicationIntent]:
        """Handle incoming mesh packets and return publication intents."""
        return self._handle_packet_from_radio(
            context.message.packet,
            emit_publication=False,
        )

    def _handle_from_radio_log_record(
        self, context: _FromRadioContext
    ) -> list[_PublicationIntent]:
        """Handle incoming log records."""
        self._handle_log_record(context.message.log_record)
        return []

    def _handle_from_radio_queue_status(
        self, context: _FromRadioContext
    ) -> list[_PublicationIntent]:
        """Handle inbound queue status updates/correlation."""
        self._handle_queue_status_from_radio(context.message.queueStatus)
        return []

    def _handle_from_radio_client_notification(
        self, context: _FromRadioContext
    ) -> list[_PublicationIntent]:
        """Build publication intent for client notifications."""
        return [
            self._publication_intent(
                "meshtastic.clientNotification",
                notification=context.message.clientNotification,
            ),
        ]

    def _handle_from_radio_mqtt_client_proxy_message(
        self, context: _FromRadioContext
    ) -> list[_PublicationIntent]:
        """Build publication intent for MQTT client proxy messages."""
        return [
            self._publication_intent(
                "meshtastic.mqttclientproxymessage",
                proxymessage=context.message.mqttClientProxyMessage,
            ),
        ]

    def _handle_from_radio_xmodem_packet(
        self, context: _FromRadioContext
    ) -> list[_PublicationIntent]:
        """Build publication intent for inbound XMODEM payloads."""
        return [
            self._publication_intent(
                "meshtastic.xmodempacket",
                packet=context.message.xmodemPacket,
            ),
        ]

    def _handle_from_radio_rebooted(
        self, _context: _FromRadioContext
    ) -> list[_PublicationIntent]:
        """Handle reboot notifications by disconnecting and restarting config flow."""
        # Tell clients the device went away.  Careful not to call the overridden
        # subclass version that closes the serial port
        MeshInterface._disconnected(self)
        self._start_config()  # redownload the node db etc...
        return []

    def _handle_from_radio_config_update(
        self, context: _FromRadioContext
    ) -> list[_PublicationIntent]:
        """Apply localConfig/moduleConfig updates from inbound FromRadio payloads."""
        self._apply_config_from_radio(context.message)
        return []

    def _apply_config_from_radio(self, from_radio: mesh_pb2.FromRadio) -> None:
        """Copy the active config/moduleConfig submessage into local cached config."""
        with self._node_db_lock:
            self._apply_local_config_from_radio(from_radio.config)
            self._apply_module_config_from_radio(from_radio.moduleConfig)

    def _apply_local_config_from_radio(self, config: config_pb2.Config) -> bool:
        """Apply all present localConfig fields from inbound config payload."""
        applied = False
        source_fields = config.DESCRIPTOR.fields_by_name
        target_fields = self.localNode.localConfig.DESCRIPTOR.fields_by_name
        for field_name in LOCAL_CONFIG_FROM_RADIO_FIELDS:
            if field_name not in source_fields:
                continue
            if field_name not in target_fields:
                logger.debug(
                    "Skipping unsupported localConfig field from radio update: %s",
                    field_name,
                )
                continue
            if config.HasField(field_name):  # type: ignore[arg-type]  # field_name is from known-valid LOCAL_CONFIG_FROM_RADIO_FIELDS
                getattr(self.localNode.localConfig, field_name).CopyFrom(
                    getattr(config, field_name)
                )
                applied = True
        return applied

    def _apply_module_config_from_radio(
        self, module_config: module_config_pb2.ModuleConfig
    ) -> bool:
        """Apply all present moduleConfig fields from inbound moduleConfig payload."""
        applied = False
        source_fields = module_config.DESCRIPTOR.fields_by_name
        target_fields = self.localNode.moduleConfig.DESCRIPTOR.fields_by_name
        for field_name in MODULE_CONFIG_FROM_RADIO_FIELDS:
            if field_name not in source_fields:
                continue
            if field_name not in target_fields:
                logger.debug(
                    "Skipping unsupported moduleConfig field from radio update: %s",
                    field_name,
                )
                continue
            if module_config.HasField(field_name):  # type: ignore[arg-type]  # field_name is from known-valid MODULE_CONFIG_FROM_RADIO_FIELDS
                getattr(self.localNode.moduleConfig, field_name).CopyFrom(
                    getattr(module_config, field_name)
                )
                applied = True
        return applied

    def _publication_intent(self, topic: str, **payload: Any) -> _PublicationIntent:
        """Create a publication intent for deferred emission."""
        return _PublicationIntent(topic=topic, payload=dict(payload))

    def _emit_publication_intents(self, intents: list[_PublicationIntent]) -> None:
        """Emit queued publication intents in a dedicated publication phase."""
        for intent in intents:
            self._queue_publication(intent.topic, **intent.payload)

    def _queue_publication(self, topic: str, **payload: Any) -> None:
        """Queue a pubsub emission for the publishing thread."""
        payload_snapshot = dict(payload)

        def publish_work() -> None:
            pub.sendMessage(topic, interface=self, **payload_snapshot)

        publishingThread.queueWork(publish_work)

    def _fixup_position(self, position: dict[str, Any]) -> dict[str, Any]:
        """Convert integer micro-degree coordinates in a position dict to floating-point degrees.

        If present, 'latitudeI' and 'longitudeI' are converted to 'latitude' and 'longitude'
        by multiplying by 1e-7 (micro-degrees -> degrees) and stored back into the same dict.

        Parameters
        ----------
        position : dict[str, Any]
            Position dictionary that may contain integer keys 'latitudeI' and 'longitudeI'.

        Returns
        -------
        dict[str, Any]
            The same position dictionary with 'latitude' and/or 'longitude' set to float degrees when corresponding integer fields were present.
        """
        if "latitudeI" in position:
            position["latitude"] = position["latitudeI"] * 1e-7
        if "longitudeI" in position:
            position["longitude"] = position["longitudeI"] * 1e-7
        return position

    def _node_num_to_id(self, num: int, isDest: bool = True) -> str | None:
        """Map a mesh numeric node number to its node ID string or a broadcast/unknown literal.

        If num equals the broadcast numeric constant, returns BROADCAST_ADDR when isDest is True
        or the string "Unknown" when isDest is False. Otherwise looks up and returns the stored
        user ID for that node number.

        Parameters
        ----------
        isDest : bool
            When True treat the broadcast number as a destination (return
            BROADCAST_ADDR); when False treat it as an unknown source (return "Unknown"). (Default value = True)
        num : int
            Numeric node identifier.

        Returns
        -------
        str | None
            The node ID string, BROADCAST_ADDR for broadcast destinations, "Unknown" for
            broadcast sources, or `None` if the node number is not present in the local node map.
        """
        if num == BROADCAST_NUM:
            return BROADCAST_ADDR if isDest else "Unknown"

        with self._node_db_lock:
            nodes = self.nodesByNum
            if nodes is None:
                logger.debug(
                    "Node database not initialized while resolving node id for %s", num
                )
                return None
            node = nodes.get(num)
            if not isinstance(node, dict):
                logger.debug("Node %s not found for fromId", num)
                return None
            user = node.get("user")
            if not isinstance(user, dict):
                logger.debug("Node %s has no user payload for fromId", num)
                return None
            node_id = user.get("id")
            if not isinstance(node_id, str):
                logger.debug("Node %s user payload has no valid id", num)
                return None
            return node_id

    def _get_or_create_by_num(self, nodeNum: int) -> dict[str, Any]:
        """Retrieve the node record for a numeric node ID, creating a minimal placeholder if none exists.

        Parameters
        ----------
        nodeNum : int
            Numeric node identifier.

        Returns
        -------
        dict[str, Any]
            The node info dictionary stored in self.nodesByNum for the given nodeNum.

        Raises
        ------
        MeshInterface.MeshInterfaceError
            If nodeNum is the broadcast node number or if the node database has not been initialized.
        """
        if nodeNum == BROADCAST_NUM:
            raise MeshInterface.MeshInterfaceError(
                "Can not create/find nodenum by the broadcast num"
            )

        with self._node_db_lock:
            if self.nodesByNum is None:
                raise MeshInterface.MeshInterfaceError("Node database not initialized")

            if nodeNum in self.nodesByNum:
                return self.nodesByNum[nodeNum]
            presumptive_id = f"!{nodeNum:08x}"
            n = {
                "num": nodeNum,
                "user": {
                    "id": presumptive_id,
                    "longName": f"Meshtastic {presumptive_id[-4:]}",
                    "shortName": f"{presumptive_id[-4:]}",
                    "hwModel": "UNSET",
                },
            }  # Create a minimal node db entry
            self.nodesByNum[nodeNum] = n
            return n

    def _handle_channel(self, channel: channel_pb2.Channel) -> None:
        """Record a received local channel descriptor for later configuration.

        Parameters
        ----------
        channel : channel_pb2.Channel
            Channel descriptor to append to the internal _localChannels list.
        """
        with self._node_db_lock:
            self._localChannels.append(channel)

    def _handle_packet_from_radio(
        self,
        meshPacket: mesh_pb2.MeshPacket,
        hack: bool = False,
        *,
        emit_publication: bool = True,
    ) -> list[_PublicationIntent]:
        """Process incoming MeshPacket with explicit normalize/classify/mutate/publish phases.

        The `hack` flag bypasses the normal rejection of packets with from==0; this
        compatibility path exists for tests and legacy call paths.
        """
        packet_dict = self._normalize_packet_from_radio(meshPacket, hack=hack)
        if packet_dict is None:
            return []

        packet_context = _PacketRuntimeContext(packet_dict=packet_dict)
        self._enrich_packet_identity(packet_context.packet_dict)
        self._classify_packet_runtime(packet_context, meshPacket)
        self._apply_packet_runtime_mutations(packet_context, meshPacket)
        self._invoke_packet_on_receive(packet_context)
        self._correlate_packet_response_handler(packet_context)
        published_packet = copy.deepcopy(packet_context.packet_dict)

        publication_intents = [
            self._publication_intent(
                packet_context.topic,
                packet=published_packet,
            )
        ]
        logger.debug(
            "Publishing %s: packet=%s",
            packet_context.topic,
            stripnl(published_packet),
        )
        if emit_publication:
            self._emit_publication_intents(publication_intents)
        return publication_intents

    def _normalize_packet_from_radio(
        self,
        meshPacket: mesh_pb2.MeshPacket,
        *,
        hack: bool,
    ) -> dict[str, Any] | None:
        """Convert protobuf packet into runtime dict and enforce legacy defaults."""
        if not hack and getattr(meshPacket, "from") == 0:
            packet_dict = {"raw": meshPacket, "from": 0}
            logger.error(
                "Device returned a packet we sent, ignoring: %s",
                stripnl(packet_dict),
            )
            return None

        packet_dict = _LazyMessageDict(meshPacket).get()

        # We normally decompose the payload into a dictionary so that the client
        # doesn't need to understand protobufs.  But advanced clients might
        # want the raw protobuf, so we provide it in "raw"
        packet_dict["raw"] = meshPacket
        if hack and "from" not in packet_dict and getattr(meshPacket, "from") == 0:
            packet_dict["from"] = 0

        if "to" not in packet_dict:
            packet_dict["to"] = 0
        return packet_dict

    def _enrich_packet_identity(self, packet_dict: dict[str, Any]) -> None:
        """Populate fromId/toId fields from known node-number mappings."""
        try:
            packet_dict["fromId"] = self._node_num_to_id(packet_dict["from"], False)
        except Exception as ex:
            logger.warning("Not populating fromId: %s", ex, exc_info=True)
        try:
            packet_dict["toId"] = self._node_num_to_id(packet_dict["to"])
        except Exception as ex:
            logger.warning("Not populating toId: %s", ex, exc_info=True)

    def _classify_packet_runtime(
        self,
        packet_context: _PacketRuntimeContext,
        mesh_packet: mesh_pb2.MeshPacket,
    ) -> None:
        """Classify packet topic and decoded payload view."""
        # We could provide our objects as DotMaps - which work with . notation or as dictionaries
        # asObj = DotMap(asDict)
        packet_context.topic = "meshtastic.receive"  # Generic unknown packet type

        if "decoded" not in packet_context.packet_dict:
            return

        decoded = cast(dict[str, Any], packet_context.packet_dict["decoded"])
        packet_context.decoded = decoded
        # The default MessageToDict converts byte arrays into base64 strings.
        # We don't want that - it messes up data payload.  So slam in the correct
        # byte array.
        decoded["payload"] = mesh_packet.decoded.payload

        portnum = portnums_pb2.PortNum.Name(portnums_pb2.PortNum.UNKNOWN_APP)
        # UNKNOWN_APP is the default protobuf portnum value, and therefore if not
        # set it will not be populated at all to make API usage easier, set
        # it to prevent confusion
        if "portnum" not in decoded:
            decoded["portnum"] = portnum
            logger.warning("portnum was not in decoded. Setting to:%s", portnum)
        else:
            portnum = decoded["portnum"]
        packet_context.topic = f"meshtastic.receive.data.{portnum}"

    def _apply_packet_runtime_mutations(
        self,
        packet_context: _PacketRuntimeContext,
        mesh_packet: mesh_pb2.MeshPacket,
    ) -> None:
        """Decode known payloads and run protocol-specific onReceive handlers."""
        if packet_context.decoded is None:
            return

        # decode position protobufs and update nodedb, provide decoded version
        # as "position" in the published msg move the following into a 'decoders'
        # API that clients could register?
        port_num_int = mesh_packet.decoded.portnum  # we want portnum as an int
        handler = protocols.get(port_num_int)
        if handler is None:
            return

        packet_context.topic = f"meshtastic.receive.{handler.name}"
        self._decode_packet_payload_with_handler(packet_context, mesh_packet, handler)

        # Call specialized onReceive if necessary
        if handler.onReceive is not None:
            packet_context.on_receive_callback = handler.onReceive

    def _invoke_packet_on_receive(self, packet_context: _PacketRuntimeContext) -> None:
        """Run protocol onReceive callback if one was selected during mutation."""
        if packet_context.on_receive_callback is None:
            return
        packet_context.on_receive_callback(self, packet_context.packet_dict)

    def _decode_packet_payload_with_handler(
        self,
        packet_context: _PacketRuntimeContext,
        mesh_packet: mesh_pb2.MeshPacket,
        handler: Any,
    ) -> None:
        """Decode decoded.payload using a protocol handler protobuf factory when available."""
        if handler.protobufFactory is None:
            return

        pb = handler.protobufFactory()
        try:
            pb.ParseFromString(mesh_packet.decoded.payload)
            decoded_payload = google.protobuf.json_format.MessageToDict(pb)
            packet_context.packet_dict["decoded"][handler.name] = decoded_payload
            # Also provide the protobuf raw
            packet_context.packet_dict["decoded"][handler.name]["raw"] = pb
        except (protobuf_message.DecodeError, TypeError, ValueError) as exc:
            decode_error = f"{DECODE_FAILED_PREFIX}{exc}"
            logger.warning(
                "Failed to decode %s payload for packet id=%s from=%s to=%s: %s",
                handler.name,
                getattr(mesh_packet, "id", 0),
                packet_context.packet_dict.get("from"),
                packet_context.packet_dict.get("to"),
                exc,
            )
            packet_context.packet_dict["decoded"][handler.name] = {
                DECODE_ERROR_KEY: decode_error
            }
            if handler.name == "routing":
                packet_context.packet_dict["decoded"][handler.name][
                    "errorReason"
                ] = decode_error
            if handler.name == "admin":
                # Admin callbacks frequently expect decoded.admin.raw.
                # Avoid dispatching malformed payloads through that path.
                packet_context.skip_response_callback_for_decode_failure = True

    def _correlate_packet_response_handler(
        self, packet_context: _PacketRuntimeContext
    ) -> None:
        """Correlate requestId responses with registered response handlers."""
        if packet_context.decoded is None:
            return
        self._request_wait_runtime.correlate_inbound_response(
            packet_dict=packet_context.packet_dict,
            skip_response_callback_for_decode_failure=(
                packet_context.skip_response_callback_for_decode_failure
            ),
            extract_request_id=self._extract_request_id_from_packet,
        )
