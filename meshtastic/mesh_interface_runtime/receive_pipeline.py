"""Receive pipeline for processing inbound packets from the radio."""

import copy
import hashlib
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeAlias

import google.protobuf.json_format
from google.protobuf import message as protobuf_message
from pubsub import pub

from meshtastic import (
    BROADCAST_ADDR,
    BROADCAST_NUM,
    DECODE_ERROR_KEY,
    protocols,
    publishingThread,
)
from meshtastic.protobuf import (
    channel_pb2,
    config_pb2,
    mesh_pb2,
    module_config_pb2,
    portnums_pb2,
)
from meshtastic.util import stripnl

if TYPE_CHECKING:
    from meshtastic.mesh_interface import MeshInterface

logger = logging.getLogger(__name__)

# Module-level constant for FromRadio branch selection predicates
_FROM_RADIO_BRANCHES: tuple[
    tuple[Callable[[mesh_pb2.FromRadio, "_FromRadioContext"], bool], str],
    ...,
] = (
    (lambda fr, _ctx: fr.HasField("my_info"), "my_info"),
    (lambda fr, _ctx: fr.HasField("metadata"), "metadata"),
    (lambda fr, _ctx: fr.HasField("node_info"), "node_info"),
    (
        lambda fr, ctx: fr.config_complete_id != 0
        and fr.config_complete_id == ctx.config_id,
        "config_complete_id",
    ),
    (lambda fr, _ctx: fr.HasField("channel"), "channel"),
    (lambda fr, _ctx: fr.HasField("packet"), "packet"),
    (lambda fr, _ctx: fr.HasField("log_record"), "log_record"),
    (lambda fr, _ctx: fr.HasField("queueStatus"), "queueStatus"),
    (lambda fr, _ctx: fr.HasField("clientNotification"), "clientNotification"),
    (
        lambda fr, _ctx: fr.HasField("mqttClientProxyMessage"),
        "mqttClientProxyMessage",
    ),
    (lambda fr, _ctx: fr.HasField("xmodemPacket"), "xmodemPacket"),
    (lambda fr, _ctx: fr.HasField("rebooted") and fr.rebooted, "rebooted"),
    (
        lambda fr, _ctx: fr.HasField("config") or fr.HasField("moduleConfig"),
        "config_or_moduleConfig",
    ),
)

# Module-level constants
LOCAL_CONFIG_FROM_RADIO_FIELDS: tuple[str, ...] = (
    "device",
    "position",
    "power",
    "network",
    "display",
    "lora",
    "bluetooth",
    "security",
    "sessionkey",
    "device_ui",
)
MODULE_CONFIG_FROM_RADIO_FIELDS: tuple[str, ...] = (
    "mqtt",
    "serial",
    "external_notification",
    "store_forward",
    "range_test",
    "telemetry",
    "canned_message",
    "audio",
    "remote_hardware",
    "neighbor_info",
    "detection_sensor",
    "ambient_lighting",
    "paxcounter",
    "statusmessage",
    "traffic_management",
)
DECODE_FAILED_PREFIX = "decode-failed: "

_MICRODEGREE_TO_DEGREE = 1e-7

JSONValue: TypeAlias = (
    None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]
)


@dataclass(frozen=True)
class _PublicationIntent:
    """Represents one pubsub emission requested by inbound runtime handlers."""

    topic: str
    payload: dict[str, Any]


@dataclass
class _LazyMessageDict:
    """Lazily materializes protobuf JSON dict form on first access.

    _LazyMessageDict is not thread-safe: get() lazily writes backing field _value
    without synchronization. Callers must synchronize externally for concurrent use.
    """

    message: protobuf_message.Message
    _value: dict[str, Any] | None = field(default=None, init=False, repr=False)

    def get(self) -> dict[str, Any]:
        """Return cached MessageToDict payload, computing it only once."""
        if self._value is None:
            self._value = google.protobuf.json_format.MessageToDict(self.message)
        return self._value


@dataclass(frozen=True)
class _FromRadioContext:
    """Normalized FromRadio context passed from parse/normalize to dispatch handlers."""

    message: mesh_pb2.FromRadio
    message_dict: _LazyMessageDict
    config_id: int | None


@dataclass
class _PacketRuntimeContext:
    """Mutable packet runtime state across packet handling phases."""

    packet_dict: dict[str, Any]
    topic: str = "meshtastic.receive"
    decoded: dict[str, Any] | None = None
    skip_response_callback_for_decode_failure: bool = False
    on_receive_callback: Callable[[Any, dict[str, Any]], Any] | None = None


class ReceivePipeline:
    """Receives and processes inbound packets from the radio.

    This class encapsulates all receive-related functionality, including parsing,
    normalization, dispatch, and publication of inbound packets.
    """

    def __init__(self, interface: "MeshInterface") -> None:
        """Initialize the receive pipeline with a parent MeshInterface.

        Parameters
        ----------
        interface : MeshInterface
            The parent MeshInterface instance providing access to interface state.
        """
        self._interface = interface
        self._from_radio_dispatch_map_cache: (
            dict[str, Callable[[_FromRadioContext], list[_PublicationIntent]]] | None
        ) = None

    @property
    def _node_db_lock(self) -> threading.RLock:
        """Return the node database lock from the parent interface."""
        return self._interface._node_db_lock

    @property
    def _request_wait_runtime(self) -> Any:
        """Return the request wait runtime from the parent interface."""
        return self._interface._request_wait_runtime

    @property
    def _queue_send_runtime(self) -> Any:
        """Return the queue send runtime from the parent interface."""
        return self._interface._queue_send_runtime

    @property
    def configId(self) -> int | None:
        """Return the config ID from the parent interface."""
        return self._interface.configId

    @property
    def localNode(self) -> Any:
        """Return the local node from the parent interface."""
        return self._interface.localNode

    @property
    def myInfo(self) -> mesh_pb2.MyNodeInfo | None:
        """Return the myInfo from the parent interface."""
        return self._interface.myInfo

    @property
    def metadata(self) -> mesh_pb2.DeviceMetadata | None:
        """Return the device metadata from the parent interface."""
        return self._interface.metadata

    @property
    def nodes(self) -> dict[str, dict[str, Any]] | None:
        """Return the nodes dictionary from the parent interface."""
        return self._interface.nodes

    @property
    def nodesByNum(self) -> dict[int, dict[str, Any]] | None:
        """Return the nodes by number dictionary from the parent interface."""
        return self._interface.nodesByNum

    def _handle_from_radio(self, fromRadioBytes: bytes) -> None:
        """Handle a raw FromRadio payload using parse -> normalize -> dispatch -> publish phases."""
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
        for predicate, branch_name in _FROM_RADIO_BRANCHES:
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
            self._interface.myInfo = my_info
            self._interface.localNode.nodeNum = my_info.my_node_num
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
            self._interface.metadata = metadata
        logger.debug("Received device metadata: %s", stripnl(from_radio.metadata))
        return []

    def _handle_from_radio_node_info(
        self, context: _FromRadioContext
    ) -> list[_PublicationIntent]:
        """Apply node_info updates and emit node-updated publication intents."""
        message_dict = context.message_dict.get()
        if "nodeInfo" not in message_dict:
            logger.warning("Received node_info without nodeInfo payload")
            return []
        node_info = message_dict["nodeInfo"]
        logger.debug("Received nodeinfo: %s", node_info)

        node = self._get_or_create_by_num(node_info["num"])
        with self._node_db_lock:
            node.update(node_info)
            try:
                node["position"] = self._fixup_position(node["position"])
            except KeyError:
                logger.debug("Node has no position key")

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
        self._interface._disconnected()
        self._interface._start_config()
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
            pub.sendMessage(topic, interface=self._interface, **payload_snapshot)

        publishingThread.queueWork(publish_work)

    def _fixup_position(self, position: dict[str, Any]) -> dict[str, Any]:
        """Convert integer micro-degree coordinates in a position dict to floating-point degrees."""
        if "latitudeI" in position:
            position["latitude"] = position["latitudeI"] * _MICRODEGREE_TO_DEGREE
        if "longitudeI" in position:
            position["longitude"] = position["longitudeI"] * _MICRODEGREE_TO_DEGREE
        return position

    def _get_or_create_by_num(self, nodeNum: int) -> dict[str, Any]:
        """Retrieve the node record for a numeric node ID, creating a minimal placeholder if none exists."""
        if nodeNum == BROADCAST_NUM:
            raise self._interface.MeshInterfaceError(
                "Can not create/find nodenum by the broadcast num"
            )

        with self._node_db_lock:
            if self.nodesByNum is None:
                raise self._interface.MeshInterfaceError(
                    "Node database not initialized"
                )

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
            }
            self.nodesByNum[nodeNum] = n
            return n

    def _handle_channel(self, channel: channel_pb2.Channel) -> None:
        """Record a received local channel descriptor for later configuration."""
        with self._node_db_lock:
            self._interface._localChannels.append(channel)

    def _handle_log_record(self, record: mesh_pb2.LogRecord) -> None:
        """Process a protobuf LogRecord by extracting its message text."""
        self._interface._handle_log_line(record.message)

    def _handle_config_complete(self) -> None:
        """Finalize initial configuration by applying collected local channels."""
        with self._node_db_lock:
            local_channels = list(self._interface._localChannels)
        self._interface.localNode.setChannels(local_channels)
        self._interface._connected()

    def _handle_queue_status_from_radio(
        self, queueStatus: mesh_pb2.QueueStatus
    ) -> None:
        """Update internal transmit-queue state from a received QueueStatus message."""
        self._queue_send_runtime._handle_queue_status_from_radio(queueStatus)

    def _handle_packet_from_radio(
        self,
        meshPacket: mesh_pb2.MeshPacket,
        allow_zero_source: bool = False,
        *,
        emit_publication: bool = True,
    ) -> list[_PublicationIntent]:
        """Process incoming MeshPacket with explicit normalize/classify/mutate/publish phases.

        Parameters
        ----------
        allow_zero_source : bool
            If True, process packets with from==0 (normally filtered as echoed sends).
        emit_publication : bool, optional
            Whether to emit publication intents immediately (default: True).
        """
        packet_dict = self._normalize_packet_from_radio(
            meshPacket, allow_zero_source=allow_zero_source
        )
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
        allow_zero_source: bool,
    ) -> dict[str, Any] | None:
        """Convert protobuf packet into runtime dict and enforce legacy defaults.

        Parameters
        ----------
        allow_zero_source : bool
            If True, process packets with from==0 (normally filtered as echoed sends).
        """
        if not allow_zero_source and getattr(meshPacket, "from") == 0:
            packet_dict = {"raw": meshPacket, "from": 0}
            logger.error(
                "Device returned a packet we sent, ignoring: %s",
                stripnl(packet_dict),
            )
            return None

        packet_dict = _LazyMessageDict(meshPacket).get()

        packet_dict["raw"] = meshPacket
        if (
            allow_zero_source
            and "from" not in packet_dict
            and getattr(meshPacket, "from") == 0
        ):
            packet_dict["from"] = 0

        if "to" not in packet_dict:
            packet_dict["to"] = 0
        return packet_dict

    def _enrich_packet_identity(self, packet_dict: dict[str, Any]) -> None:
        """Populate fromId/toId fields from known node-number mappings."""
        try:
            packet_dict["fromId"] = self._node_num_to_id(packet_dict["from"], False)
        except Exception as ex:
            packet_dict["fromId"] = None
            logger.warning("Not populating fromId: %s", ex, exc_info=True)
        try:
            packet_dict["toId"] = self._node_num_to_id(packet_dict["to"])
        except Exception as ex:
            packet_dict["toId"] = None
            logger.warning("Not populating toId: %s", ex, exc_info=True)

    def _node_num_to_id(self, num: int, isDest: bool = True) -> str | None:
        """Map a mesh numeric node number to its node ID string."""
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

    def _classify_packet_runtime(
        self,
        packet_context: _PacketRuntimeContext,
        mesh_packet: mesh_pb2.MeshPacket,
    ) -> None:
        """Classify packet topic and decoded payload view."""
        packet_context.topic = "meshtastic.receive"

        if "decoded" not in packet_context.packet_dict:
            return

        decoded = packet_context.packet_dict["decoded"]
        packet_context.decoded = decoded
        decoded["payload"] = mesh_packet.decoded.payload

        portnum = portnums_pb2.PortNum.Name(portnums_pb2.PortNum.UNKNOWN_APP)
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

        port_num_int = mesh_packet.decoded.portnum
        handler = protocols.get(port_num_int)
        if handler is None:
            return

        packet_context.topic = f"meshtastic.receive.{handler.name}"
        self._decode_packet_payload_with_handler(packet_context, mesh_packet, handler)

        if handler.onReceive is not None:
            packet_context.on_receive_callback = handler.onReceive

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
                packet_context.skip_response_callback_for_decode_failure = True

    def _invoke_packet_on_receive(self, packet_context: _PacketRuntimeContext) -> None:
        """Run protocol onReceive callback if one was selected during mutation."""
        if packet_context.on_receive_callback is None:
            return
        try:
            packet_context.on_receive_callback(
                self._interface, packet_context.packet_dict
            )
        except Exception:
            logger.exception(
                "Protocol onReceive callback failed: callback=%s topic=%s packet_id=%s",
                packet_context.on_receive_callback,
                packet_context.topic,
                getattr(packet_context.packet_dict.get("raw"), "id", 0),
            )

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
            extract_request_id=self._interface._extract_request_id_from_packet,
        )
