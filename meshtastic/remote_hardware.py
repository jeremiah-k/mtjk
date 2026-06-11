"""Remote hardware."""

import logging
import threading
from typing import TYPE_CHECKING, Any, Callable, Protocol, cast

from pubsub import pub

from meshtastic.protobuf import mesh_pb2, portnums_pb2, remote_hardware_pb2

if TYPE_CHECKING:
    from meshtastic.mesh_interface import MeshInterface

logger = logging.getLogger(__name__)

GPIO_CHANNEL_NAME = "gpio"
REMOTE_HARDWARE_TOPIC = "meshtastic.receive.remotehw"
NO_GPIO_CHANNEL_ERROR = (
    f"No channel named '{GPIO_CHANNEL_NAME}' was found. "
    f"On the sending and receiving nodes create a channel named '{GPIO_CHANNEL_NAME}'.\n"
    f"For example, run '--ch-add {GPIO_CHANNEL_NAME}' on one device, then '--seturl' on\n"
    "the other devices using the url from the device where the channel was added."
)
MISSING_DEST_NODE_ID_ERROR = (
    "Must use a destination node ID for this operation (use --dest). "
    "Special aliases (for example '^all') are not valid here."
)
INVALID_GPIO_MASK_ERROR = "mask must be a non-negative int"
INVALID_GPIO_VALS_ERROR = "vals must be a non-negative int"
INVALID_GPIO_VALS_MASK_ERROR = "vals contains bits outside mask"
WATCH_MASKS_ATTR = "_remote_hardware_watch_masks"
WATCH_MASKS_LOCK_ATTR = "_remote_hardware_watch_masks_lock"
WATCH_MASKS_INIT_LOCK = threading.Lock()
REMOTE_HARDWARE_SUBSCRIBE_LOCK = threading.Lock()

_MESH_INTERFACE_ERROR_LOCK = threading.Lock()
_MESH_INTERFACE_ERROR: type[Exception] | None = None


class LockLike(Protocol):
    """Minimal lock protocol used for per-interface watch-mask synchronization."""

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        """Acquire the lock."""

    def release(self) -> None:
        """Release the lock."""

    def __enter__(self) -> bool:
        """Enter context manager and acquire lock."""

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None:
        """Exit context manager and release lock."""


def _get_mesh_interface_error() -> type[Exception]:
    """Resolve MeshInterfaceError lazily to avoid module-import cycles.

    Returns
    -------
    MeshInterfaceError : type[Exception]
        The cached MeshInterface.MeshInterfaceError exception class.
    """
    global _MESH_INTERFACE_ERROR  # pylint: disable=global-statement
    if _MESH_INTERFACE_ERROR is None:
        with _MESH_INTERFACE_ERROR_LOCK:
            if _MESH_INTERFACE_ERROR is None:
                from meshtastic.mesh_interface import (  # pylint: disable=import-outside-toplevel
                    MeshInterface,
                )

                _MESH_INTERFACE_ERROR = MeshInterface.MeshInterfaceError
    return _MESH_INTERFACE_ERROR


def _normalize_node_key(nodeid: Any) -> str | None:
    """Normalize a node identifier to a stable string key for internal mask tracking.

    Parameters
    ----------
    nodeid : Any
        Node identifier candidate (numeric node number, node ID string, or None).

    Returns
    -------
    str | None
        Normalized key prefixed with ``num:`` or ``id:``, or ``None`` for
        unsupported/empty values.
    """
    key: str | None = None
    if nodeid is None or isinstance(nodeid, bool):
        return key

    if isinstance(nodeid, int):
        key = f"num:{nodeid}"
    elif isinstance(nodeid, str):
        stripped = nodeid.strip()
        if stripped:
            if stripped.startswith("!"):
                key = f"id:{stripped.lower()}"
            else:
                parsed = _parse_node_number(stripped)
                key = (
                    f"num:{parsed}" if parsed is not None else f"id:{stripped.lower()}"
                )
    else:
        try:
            key = f"num:{int(nodeid)}"
        except (TypeError, ValueError):
            key = None

    return key


def _parse_node_number(text: str) -> int | None:
    """Parse node number text from prefixed base forms or plain decimal."""
    normalized = text.strip().lower()
    if not normalized:
        return None
    try:
        return int(normalized, 0)
    except ValueError:
        # Fall back to explicit decimal parsing so plain numeric strings with
        # leading zeros (for example "00123") are still accepted.
        signless = normalized[1:] if normalized[:1] in "+-" else normalized
        if signless.isdigit():
            return int(normalized, 10)
        return None


def _get_watch_masks(interface: "MeshInterface") -> dict[str, int]:
    """Return per-interface watch masks, creating storage if needed.

    This helper mutates ``WATCH_MASKS_ATTR`` on ``interface`` when missing and
    is not thread-safe by itself. Callers of ``_get_watch_masks`` must hold the
    lock returned by ``_get_watch_masks_lock(interface)`` when reading or
    mutating the returned dictionary.
    """
    watch_masks = getattr(interface, WATCH_MASKS_ATTR, None)
    if isinstance(watch_masks, dict):
        return watch_masks
    watch_masks = {}
    setattr(interface, WATCH_MASKS_ATTR, watch_masks)
    return watch_masks


def _get_watch_masks_lock(interface: "MeshInterface") -> LockLike:
    """Return the per-interface lock guarding watch-mask state."""
    lock = getattr(interface, WATCH_MASKS_LOCK_ATTR, None)
    if lock is None:
        with WATCH_MASKS_INIT_LOCK:
            lock = getattr(interface, WATCH_MASKS_LOCK_ATTR, None)
            if lock is None:
                lock = threading.Lock()
                setattr(interface, WATCH_MASKS_LOCK_ATTR, lock)
    return cast(LockLike, lock)


def onGPIOReceive(packet: Any, interface: "MeshInterface") -> None:
    """Handle an incoming remote hardware (GPIO) response packet, log its summary, and mark the interface as having received a response.

    Extracts `gpioValue` from packet["decoded"]["remotehw"] (defaults to 0 if
    absent), determines an active mask from packet `gpioMask` or from
    RemoteHardwareClient watch-mask state keyed by sender node (with a final
    legacy fallback to ``interface.mask``), computes the masked GPIO value,
    logs the hardware type and computed value, and sets
    `interface.gotResponse` to True.

    Parameters
    ----------
    packet : Any
        Decoded message dictionary containing a "remotehw" mapping with optional keys:
        - "gpioValue" (int): GPIO value reported by the remote device (may be
          omitted; treated as 0).
        - "gpioMask" (int): Mask provided by the remote device.
        - "type" (int|enum): Hardware message type.
        Non-dict payloads are treated as malformed and only mark `gotResponse`.
    interface : 'MeshInterface'
        MeshInterface instance that may contain per-node watch mask state and
        whose `gotResponse` attribute will be set to True.
    """
    if not isinstance(packet, dict):
        logger.warning(
            "Malformed remote hardware packet: packet is not a dict (packet=%r)",
            packet,
        )
        interface.gotResponse = True
        return

    logger.debug("packet:%s interface:%s", packet, interface)
    gpioValue = 0
    decoded = packet.get("decoded", {})
    if not isinstance(decoded, dict):
        logger.warning(
            "Malformed remote hardware packet: decoded is not a dict (decoded=%r packet=%r)",
            decoded,
            packet,
        )
        interface.gotResponse = True
        return
    hw = decoded.get("remotehw", {})
    if not isinstance(hw, dict):
        logger.warning(
            "Malformed remote hardware packet: remotehw is not a dict (remotehw=%r packet=%r)",
            hw,
            packet,
        )
        interface.gotResponse = True
        return
    if "gpioValue" in hw:
        gpioValue = hw["gpioValue"]
    # Note: proto3 omits zero-valued fields; gpioValue defaults to 0
    # See https://developers.google.com/protocol-buffers/docs/proto3#default

    raw_mask = hw.get("gpioMask")
    if raw_mask is None:
        sender_from = packet.get("from")
        sender_from_id = packet.get("fromId")
        with _get_watch_masks_lock(interface):
            watch_masks = _get_watch_masks(interface)
            for lookup_value in (sender_from, sender_from_id):
                key = _normalize_node_key(lookup_value)
                if key is not None and key in watch_masks:
                    raw_mask = watch_masks[key]
                    break
            sender_missing = (
                sender_from is None
                or (isinstance(sender_from, str) and not sender_from.strip())
            ) and (
                sender_from_id is None
                or (isinstance(sender_from_id, str) and not sender_from_id.strip())
            )
            if raw_mask is None and sender_missing and len(watch_masks) == 1:
                # Legacy fallback for packets that omit sender identity.
                raw_mask = next(iter(watch_masks.values()))
    if raw_mask is None:
        raw_mask = getattr(interface, "mask", None)
    if raw_mask is None:
        raw_mask = 0
    try:
        mask = int(raw_mask)
        gpio_value_int = int(gpioValue)
    except (TypeError, ValueError):
        logger.warning(
            "Could not convert gpioValue=%r or mask=%r to int.",
            gpioValue,
            raw_mask,
        )
        mask = 0
        gpio_value_int = 0
    logger.debug("mask:%s", mask)
    value = gpio_value_int & mask
    logger.info(
        "Received RemoteHardware type=%s, gpio_value=%s value=%s",
        hw.get("type", remote_hardware_pb2.HardwareMessage.Type.UNSET),
        gpioValue,
        value,
    )
    interface.gotResponse = True


# COMPAT_STABLE_SHIM: alias for onGPIOReceive
def onGpioReceive(packet: Any, interface: "MeshInterface") -> None:
    """Backward-compatible alias for onGPIOReceive."""
    onGPIOReceive(packet, interface)


# COMPAT_STABLE_SHIM: alias for onGPIOReceive
def onGPIOreceive(packet: Any, interface: "MeshInterface") -> None:
    """Backward-compatible alias for onGPIOReceive."""
    onGPIOReceive(packet, interface)


class RemoteHardwareClient:
    """Client code to control and monitor simple hardware built into Meshtastic devices.

    It is intended to be both a useful API/service and example code for how you can
    connect to your own custom meshtastic services.
    """

    def __init__(self, iface: "MeshInterface") -> None:
        """Create a RemoteHardwareClient bound to a MeshInterface and subscribe to remote GPIO responses.

        Parameters
        ----------
        iface : 'MeshInterface'
            An already-open MeshInterface instance to use for sending/receiving remote hardware messages.

        Raises
        ------
        MeshInterface.MeshInterfaceError
            If the local node has no channel named "gpio".
        """
        self.iface = iface
        if hasattr(type(iface.localNode), "getChannelCopyByName"):
            ch = iface.localNode.getChannelCopyByName(GPIO_CHANNEL_NAME)
        else:
            ch = iface.localNode.getChannelByName(GPIO_CHANNEL_NAME)
        if ch is None:
            mesh_interface_error = _get_mesh_interface_error()
            raise mesh_interface_error(NO_GPIO_CHANNEL_ERROR)
        self.channelIndex = ch.index
        with _get_watch_masks_lock(self.iface):
            _get_watch_masks(self.iface)

        with REMOTE_HARDWARE_SUBSCRIBE_LOCK:
            already_subscribed = False
            try:
                already_subscribed = pub.isSubscribed(
                    onGPIOReceive, REMOTE_HARDWARE_TOPIC
                )
            except pub.TopicNameError:
                # Topic may not exist yet; subscribe below to create/register it.
                already_subscribed = False
            except (TypeError, ValueError) as ex:
                logger.warning(
                    "Unable to inspect remote hardware topic subscription: %s", ex
                )
                already_subscribed = False
            if not already_subscribed:
                pub.subscribe(onGPIOReceive, REMOTE_HARDWARE_TOPIC)

    @staticmethod
    def _normalize_dest_nodeid(nodeid: int | str | None) -> int | str:
        """Validate and normalize destination node IDs for remote hardware commands."""
        # Reject None, empty/whitespace-only string, integer values <= 0, and
        # string values that parse as non-positive numbers (for example, "0", "-1").
        is_invalid_bool = isinstance(nodeid, bool)
        is_invalid_str = isinstance(nodeid, str) and nodeid.strip() == ""
        is_invalid_int = isinstance(nodeid, int) and not is_invalid_bool and nodeid <= 0
        is_non_positive_numeric_str = False
        is_special_alias = False
        if isinstance(nodeid, str) and not is_invalid_str:
            normalized_nodeid = nodeid.strip().lower()
            is_special_alias = normalized_nodeid.startswith("^")
            parsed_nodeid = _parse_node_number(normalized_nodeid)
            is_non_positive_numeric_str = (
                parsed_nodeid is not None and parsed_nodeid <= 0
            )
        has_invalid_dest_nodeid = (
            nodeid is None
            or is_invalid_bool
            or is_invalid_str
            or is_invalid_int
            or is_non_positive_numeric_str
            or is_special_alias
        )
        if has_invalid_dest_nodeid:
            mesh_interface_error = _get_mesh_interface_error()
            raise mesh_interface_error(MISSING_DEST_NODE_ID_ERROR)
        if isinstance(nodeid, str):
            return nodeid.strip()
        if isinstance(nodeid, int):
            return nodeid
        mesh_interface_error = _get_mesh_interface_error()
        raise mesh_interface_error(
            f"{MISSING_DEST_NODE_ID_ERROR} (got {type(nodeid).__name__}: {nodeid!r})"
        )

    def _send_hardware(
        self,
        nodeid: int | str | None,
        r: remote_hardware_pb2.HardwareMessage,
        wantResponse: bool = False,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Send a HardwareMessage to a remote node over the configured GPIO channel.

        Parameters
        ----------
        nodeid : int | str | None
            Destination node ID.
        r : remote_hardware_pb2.HardwareMessage
            The hardware message payload to send.
        wantResponse : bool
            If True, request and wait for a device response. (Default value = False)
        onResponse : Callable[[dict[str, Any]], Any] | None
            Optional callback invoked when a response is received. (Default value = None)

        Returns
        -------
        mesh_pb2.MeshPacket
            The packet returned by the underlying sendData call.

        Raises
        ------
        MeshInterface.MeshInterfaceError
            If no destination node ID is provided.
        """
        dest_nodeid = self._normalize_dest_nodeid(nodeid)
        return self.iface.sendData(
            r,
            dest_nodeid,
            portnums_pb2.REMOTE_HARDWARE_APP,
            wantAck=True,
            channelIndex=self.channelIndex,
            wantResponse=wantResponse,
            onResponse=onResponse,
        )

    @staticmethod
    def _validate_non_negative_int(value: Any, error_message: str) -> int:
        """Validate integer GPIO arguments and return normalized int."""
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            mesh_interface_error = _get_mesh_interface_error()
            raise mesh_interface_error(error_message)
        return value

    def writeGPIOs(
        self, nodeid: int | str, mask: int, vals: int
    ) -> mesh_pb2.MeshPacket:
        """Set specified GPIO pins on a remote device according to the provided mask and values.

        Parameters
        ----------
        nodeid : int | str
            Destination node identifier.
        mask : int
            Bitmask where bits set to 1 indicate GPIO pins to modify.
        vals : int
            Bit pattern to write to the masked GPIO pins; bits corresponding to 1s in `mask` will be applied.

        Returns
        -------
        mesh_pb2.MeshPacket
            Result of the underlying send operation.
        """
        mask = self._validate_non_negative_int(mask, INVALID_GPIO_MASK_ERROR)
        vals = self._validate_non_negative_int(vals, INVALID_GPIO_VALS_ERROR)
        if vals & ~mask:
            mesh_interface_error = _get_mesh_interface_error()
            raise mesh_interface_error(INVALID_GPIO_VALS_MASK_ERROR)
        logger.debug("writeGPIOs nodeid:%s mask:%s vals:%s", nodeid, mask, vals)
        r = remote_hardware_pb2.HardwareMessage()
        r.type = remote_hardware_pb2.HardwareMessage.Type.WRITE_GPIOS
        r.gpio_mask = mask
        r.gpio_value = vals
        return self._send_hardware(nodeid, r)

    def readGPIOs(
        self,
        nodeid: int | str,
        mask: int,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Request the device to read the specified GPIO bits.

        Parameters
        ----------
        nodeid : int | str
            Destination node identifier or address to send the request to.
        mask : int
            Bitmask indicating which GPIO pins to read.
        onResponse : Callable[[dict[str, Any]], Any] | None
            Optional callback invoked with the response packet when a response arrives. (Default value = None)

        Returns
        -------
        mesh_pb2.MeshPacket
            The result returned by the underlying `MeshInterface.sendData` call.
            Callback return values are ignored.
        """
        mask = self._validate_non_negative_int(mask, INVALID_GPIO_MASK_ERROR)
        logger.debug("readGPIOs nodeid:%s mask:%s", nodeid, mask)
        r = remote_hardware_pb2.HardwareMessage()
        r.type = remote_hardware_pb2.HardwareMessage.Type.READ_GPIOS
        r.gpio_mask = mask
        return self._send_hardware(nodeid, r, wantResponse=True, onResponse=onResponse)

    def watchGPIOs(self, nodeid: int | str, mask: int) -> mesh_pb2.MeshPacket:
        """Start monitoring the specified GPIO bits on a remote device for changes.

        Parameters
        ----------
        nodeid : int | str
            Destination node identifier for the target device.
        mask : int
            Bitmask selecting which GPIO pins to monitor (bit i corresponds to GPIO i).

        Returns
        -------
        mesh_pb2.MeshPacket
            Result of sending the watch request.
        """
        mask = self._validate_non_negative_int(mask, INVALID_GPIO_MASK_ERROR)
        logger.debug("watchGPIOs nodeid:%s mask:%s", nodeid, mask)
        r = remote_hardware_pb2.HardwareMessage()
        r.type = remote_hardware_pb2.HardwareMessage.Type.WATCH_GPIOS
        r.gpio_mask = mask
        result = self._send_hardware(nodeid, r)
        node_key = _normalize_node_key(nodeid)
        if node_key is not None:
            with _get_watch_masks_lock(self.iface):
                _get_watch_masks(self.iface)[node_key] = int(mask)
        return result
