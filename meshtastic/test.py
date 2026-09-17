"""Send and receive test messages between two serially connected radios."""

import io
import logging
import sys
import threading
import time
from contextlib import ExitStack, suppress
from typing import Any, NoReturn

from pubsub import pub

import meshtastic.util
from meshtastic._core_constants import BROADCAST_NUM
from meshtastic.protobuf import portnums_pb2
from meshtastic.serial_interface import SerialInterface
from meshtastic.tcp_interface import TCPInterface


class DotMap(dict[str, Any]):
    """Attribute-access mapping used by the test helpers.

    This is mtjk's canonical implementation. It historically served as the
    fallback when the third-party ``dotmap`` package was absent; that
    dependency was removed in favor of this in-tree behavior-compatible
    subset (dependency health audit 2026-09-16).
    """

    def __getattr__(self, key: str) -> Any:
        """Provide attribute-style access to dictionary keys.

        If the key exists and its value is a dict, return a DotMap wrapping that dict.
        If the key exists and its value is not a dict, return the value unchanged.
        If the key is missing, create and store an empty DotMap.

        Parameters
        ----------
        key : str
            Attribute name to retrieve as a dictionary key.

        Returns
        -------
        Any
            The value stored under `key`, or a `DotMap` for nested dicts or missing keys.

        Raises
        ------
        AttributeError
            If the key is a double-underscore (dunder) attribute name.
        """
        # Guard dunder names to avoid interfering with copy, pickle, etc.
        if key.startswith("__") and key.endswith("__"):
            raise AttributeError(key)
        try:
            value = self[key]
        except KeyError:
            # Match real DotMap's permissive behavior by auto-vivifying and persisting children.
            child = DotMap()
            self[key] = child
            return child
        if isinstance(value, dict) and not isinstance(value, DotMap):
            wrapped = DotMap(value)
            self[key] = wrapped
            return wrapped
        return value

    def __setattr__(self, key: str, value: Any) -> None:
        """Set an item in the mapping using attribute-style access.

        Dunder (double-underscore) attribute names are delegated to object attribute
        assignment to avoid interfering with Python internals; other names create or update
        mapping entries.

        Parameters
        ----------
        key : str
            Attribute name to store under.
        value : Any
            Value to assign.
        """
        # Guard dunder names to avoid interfering with Python internals.
        # Note: This asymmetry with __getattr__ (which raises) is intentional
        # to match real DotMap behavior for compatibility.
        if key.startswith("__") and key.endswith("__"):
            object.__setattr__(self, key, value)
            return
        self[key] = value

    def __delattr__(self, key: str) -> None:
        """Delete a mapping entry using attribute-style access.

        Parameters
        ----------
        key : str
            Name of the key to remove.

        Raises
        ------
        AttributeError
            If the key does not exist.
        """
        # Guard dunder names to match __setattr__ behavior.
        if key.startswith("__") and key.endswith("__"):
            object.__delattr__(self, key)
            return
        try:
            del self[key]
        except KeyError:
            raise AttributeError(key) from None


# COMPAT_STABLE_SHIM: historical name for the in-tree DotMap implementation.
_FallbackDotMap = DotMap

TEXT_MESSAGE_APP_PORTNUM = "TEXT_MESSAGE_APP"
WAIT_LOOP_MAX_SECONDS = 60

# The interfaces we are using for our tests.
interfaces: list[Any] = []

# A list of all packets we received while the current test was running.
receivedPackets: list[Any] | None = None
packet_received_event = threading.Event()

testNumber: int = 0

sendingInterface: Any = None
expected_from_node: int | None = None
expected_to_node: int | None = None
expected_text: str | None = None
expected_binary_payload: bytes | None = None
guards_lock = threading.Lock()

logger = logging.getLogger(__name__)


def _normalize_portnum(portnum: Any) -> str | None:
    """Normalize ``portnum`` for comparisons in test helpers.

    Parameters
    ----------
    portnum : Any
        Port value to normalize. Supported inputs are enum/int-like values,
        strings, and ``None``.

    Returns
    -------
    str | None
        For enum/int-like inputs, returns the canonical protobuf enum-name
        string when resolvable.
        For string inputs, returns the original string representation unchanged.
        Returns ``None`` when ``portnum`` is ``None`` or when an integer value
        is not a known ``PortNum`` enum member.
    """
    if isinstance(portnum, int):
        with suppress(ValueError):
            return portnums_pb2.PortNum.Name(portnum)  # type: ignore[arg-type]
        return None
    return str(portnum) if portnum is not None else None


def _normalize_node_id(node_id: Any) -> int | None:
    """Normalize a node ID value to integer when coercible.

    Parameters
    ----------
    node_id : Any
        Node identifier value to normalize.

    Returns
    -------
    int | None
        The integer value when coercible, otherwise ``None``.
    """
    if node_id is None:
        return None
    with suppress(TypeError, ValueError):
        return int(node_id)
    return None


def onReceive(packet: dict[str, Any], interface: Any) -> None:
    """Handle an incoming packet and record matching test messages.

    If the packet did not originate from the current sendingInterface, convert it to a DotMap.
    If the packet's decoded.portnum equals "TEXT_MESSAGE_APP" and expected sender/receiver
    constraints match, validate text and/or binary payload expectations
    (`expected_text`, `expected_binary_payload`) and append matching packets to
    the module-level receivedPackets list.

    Parameters
    ----------
    packet : dict
        Raw packet data as received.
    interface : Any
        Interface object that delivered the packet.
    """
    with guards_lock:
        if sendingInterface is interface:
            return
        decoded = packet.get("decoded", {})
        if isinstance(decoded, dict):
            portnum = decoded.get("portnum")
        else:
            portnum = getattr(decoded, "portnum", None)
        if _normalize_portnum(portnum) == TEXT_MESSAGE_APP_PORTNUM:
            pkt_from = _normalize_node_id(packet.get("from"))
            if expected_from_node is not None and pkt_from != expected_from_node:
                return
            pkt_to = _normalize_node_id(packet.get("to"))
            if expected_to_node is not None and pkt_to != expected_to_node:
                return
            decoded_text = (
                decoded.get("text")
                if isinstance(decoded, dict)
                else getattr(decoded, "text", None)
            )
            decoded_payload = (
                decoded.get("payload")
                if isinstance(decoded, dict)
                else getattr(decoded, "payload", None)
            )
            if expected_text is not None and decoded_text != expected_text:
                return
            if expected_binary_payload is not None:
                payload_bytes = (
                    decoded_payload
                    if isinstance(decoded_payload, bytes)
                    else (
                        bytes(decoded_payload)
                        if isinstance(decoded_payload, bytearray)
                        else None
                    )
                )
                if payload_bytes != expected_binary_payload:
                    return
            # We only care about matching test packets on TEXT_MESSAGE_APP.
            if receivedPackets is not None:
                receivedPackets.append(DotMap(packet))
                packet_received_event.set()


def onNode(node: Any) -> None:
    """Log that a node database entry changed.

    Parameters
    ----------
    node : Any
        The node database entry or a payload describing the change.
    """
    logger.info("Node changed: %s", node)


def subscribeToNodeUpdates() -> None:
    """Subscribe to node update notifications.

    Registers only `onNode` on the ``meshtastic.node`` topic.
    For full test wiring (including ``onReceive`` and ``onConnection``),
    use ``testAll()`` which subscribes those callbacks explicitly.
    """

    pub.subscribe(onNode, "meshtastic.node")


# COMPAT_STABLE_SHIM: historical helper name.
def subscribe() -> None:
    """Compatibility alias for subscribeToNodeUpdates().

    Returns
    -------
    None
        This function returns no value.

    See Also
    --------
    subscribeToNodeUpdates
        Canonical helper for subscribing to node-update notifications.
    """
    subscribeToNodeUpdates()


def testSend(
    fromInterface: Any,
    toInterface: Any,
    isBroadcast: bool = False,
    asBinary: bool = False,
    wantAck: bool = False,
) -> bool:
    """Send a single test packet from one interface to another.

    Parameters
    ----------
    fromInterface : Any
        Interface used to send the packet.
    toInterface : Any
        Interface targeted to receive the packet (ignored for broadcasts).
    isBroadcast : bool
        If True, send to the broadcast address. (Default value = False)
    asBinary : bool
        If True, send the payload as binary data. (Default value = False)
    wantAck : bool
        If True, request an acknowledgment from the recipient. (Default value = False)

    Returns
    -------
    bool
        `True` if a response packet was received within 60 seconds, `False` otherwise.
    """
    # pylint: disable=W0603
    global receivedPackets
    global expected_from_node
    global expected_to_node
    global expected_text
    global expected_binary_payload
    fromNode = fromInterface.myInfo.my_node_num

    if isBroadcast:
        toNode = BROADCAST_NUM
    else:
        toNode = toInterface.myInfo.my_node_num

    logger.debug(
        "Sending test wantAck=%s packet from %s to %s", wantAck, fromNode, toNode
    )
    # pylint: disable=W0603
    global sendingInterface
    local_test_id: int
    with guards_lock:
        local_test_id = testNumber
        receivedPackets = []
        packet_received_event.clear()
        sendingInterface = fromInterface
        expected_from_node = _normalize_node_id(fromNode)
        expected_to_node = _normalize_node_id(toNode)
        expected_text = None if asBinary else f"Test {local_test_id}"
        expected_binary_payload = (
            f"Binary {local_test_id}".encode("utf-8") if asBinary else None
        )
    try:
        try:
            if not asBinary:
                fromInterface.sendText(f"Test {local_test_id}", toNode, wantAck=wantAck)
            else:
                fromInterface.sendData(
                    (f"Binary {local_test_id}").encode("utf-8"),
                    toNode,
                    portNum=portnums_pb2.PortNum.TEXT_MESSAGE_APP,
                    wantAck=wantAck,
                )
        except Exception:  # noqa: BLE001 - convert send failures into test failures
            logger.exception("Send failed for test %s", local_test_id)
            return False
        if not packet_received_event.wait(timeout=WAIT_LOOP_MAX_SECONDS):
            return False
        with guards_lock:
            packet_count = len(receivedPackets) if receivedPackets is not None else 0
        return packet_count >= 1
    finally:
        with guards_lock:
            expected_from_node = None
            expected_to_node = None
            expected_text = None
            expected_binary_payload = None
            sendingInterface = None
            receivedPackets = None
            packet_received_event.clear()


def runTests(numTests: int = 50, wantAck: bool = False, maxFailures: int = 0) -> bool:
    """Execute a series of send/receive test iterations and evaluate overall success.

    Parameters
    ----------
    numTests : int
        Number of test iterations to run. (Default value = 50)
    wantAck : bool
        If True, request acknowledgments for sent test packets. (Default value = False)
    maxFailures : int
        Maximum allowed failed tests before overall result is considered a failure. (Default value = 0)

    Returns
    -------
    bool
        `True` if the number of failed tests is less than or equal to `maxFailures`, `False` otherwise.
    """
    # pylint: disable=W0603
    global testNumber
    logger.info("Running %s tests with wantAck=%s", numTests, wantAck)
    if len(interfaces) < 2:
        logger.error("runTests requires two initialized interfaces.")
        return False
    numFail: int = 0
    numSuccess: int = 0
    for _ in range(numTests):
        with guards_lock:
            testNumber = testNumber + 1
        # Use unicast when ACKs are requested so ACK behavior is exercised.
        isBroadcast: bool = not wantAck
        # asBinary=(i % 2 == 0)
        success = testSend(
            interfaces[0], interfaces[1], isBroadcast, asBinary=False, wantAck=wantAck
        )
        if not success:
            numFail = numFail + 1
            logger.error(
                "Test %d failed, expected packet not received (%d failures so far)",
                testNumber,
                numFail,
            )
        else:
            numSuccess = numSuccess + 1
            logger.info(
                "Test %d succeeded %d successes %d failures so far",
                testNumber,
                numSuccess,
                numFail,
            )

        time.sleep(1)

    if numFail > maxFailures:
        logger.error("Too many failures! Test failed!")
        return False
    return True


def testThread(numTests: int = 50) -> bool:
    """Run a two-stage test sequence across discovered devices.

    First stage runs `numTests` with acknowledgments required; if that stage succeeds, a second
    stage runs `numTests` without acknowledgments and allows up to one failure.

    Parameters
    ----------
    numTests : int
        Number of tests to run in each stage. (Default value = 50)

    Returns
    -------
    bool
        True if the overall test sequence succeeded (both stages passed as
        required), False otherwise.
    """
    logger.info("Found devices, starting tests...")
    result: bool = runTests(numTests, wantAck=True)
    if result:
        # Run another test
        # Allow a few dropped packets
        result = runTests(numTests, wantAck=False, maxFailures=1)
    return result


def onConnection(interface: Any = None, topic: Any = pub.AUTO_TOPIC) -> None:
    """Log that a connection's state changed using the topic name.

    Parameters
    ----------
    interface : Any
        The interface whose connection state changed. (Default value = None)
    topic : Any
        The connection topic object; if it provides a `getName()` method that name is
        used, otherwise `str(topic)` is used. (Default value = pub.AUTO_TOPIC)
    """
    _ = interface
    topic_name = topic.getName() if hasattr(topic, "getName") else str(topic)
    logger.info("Connection changed: %s", topic_name)


def openDebugLog(portName: str) -> io.TextIOWrapper:
    r"""Create a per-port debug log file and return its open file handle.

    Parameters
    ----------
    portName : str
        Serial port name used to derive the filename; '/' and '\\' characters
        will be replaced with '_'.

    Returns
    -------
    io.TextIOWrapper
        An open text file for writing the debug log.
        Caller must close the returned handle (for example via
        ``ExitStack.enter_context`` as done in ``testAll``).
    """
    safe_port_name = portName.replace("/", "_").replace("\\", "_")
    debugname = f"log{safe_port_name}"
    logger.info("Writing serial debugging to %s", debugname)
    return open(debugname, "w+", buffering=1, encoding="utf8")


def testAll(numTests: int = 5) -> bool:
    """Discover connected Meshtastic devices, open up to two usable serial interfaces, run integration tests, and close interfaces.

    Parameters
    ----------
    numTests : int
        Number of test iterations to run in the test thread. (Default value = 5)

    Returns
    -------
    bool
        `True` if the test sequence completed within configured failure tolerances, `False` otherwise.
    """
    # pylint: disable=W0603
    global interfaces
    interfaces = []

    ports: list[str] = meshtastic.util.findPorts(True)
    if len(ports) < 2:
        logger.error("Must have at least two devices connected to USB.")
        return False

    def _safe_unsubscribe(listener: Any, topic: str) -> None:
        """Best-effort unsubscribe helper used during teardown."""
        with suppress(Exception):
            pub.unsubscribe(listener, topic)

    try:
        with ExitStack() as stack:
            pub.subscribe(onConnection, "meshtastic.connection")
            stack.callback(_safe_unsubscribe, onConnection, "meshtastic.connection")
            pub.subscribe(onReceive, "meshtastic.receive")
            stack.callback(_safe_unsubscribe, onReceive, "meshtastic.receive")

            # Build interfaces incrementally to ensure cleanup on failure.
            for port in ports:
                debug_log = None
                try:
                    debug_log = openDebugLog(port)
                    iface = SerialInterface(port, debugOut=debug_log, connectNow=True)
                except Exception:  # noqa: BLE001 - keep scanning remaining ports
                    logger.exception("Skipping unusable port: %s", port)
                    if debug_log is not None:
                        with suppress(Exception):
                            debug_log.close()
                    continue
                stack.callback(debug_log.close)
                stack.callback(iface.close)
                interfaces.append(iface)
                if len(interfaces) == 2:
                    break

            if len(interfaces) < 2:
                logger.error("Unable to open two usable Meshtastic interfaces.")
                return False

            logger.info("Ports opened, starting test")
            return testThread(numTests)
    finally:
        interfaces = []


def testSimulator() -> NoReturn:
    """Run a short integration check against a Meshtastic simulator on localhost.

    Connects to the simulator over TCP, requests node information and a simulator
    shutdown, and then exits the process; exits with status code 0 on success
    and 1 on error.

    Returns
    -------
    NoReturn
        This function does not return normally; it calls sys.exit() on success or failure.
    """
    logging.basicConfig(level=logging.DEBUG)
    logger.info("Connecting to simulator on localhost!")
    try:
        with TCPInterface("localhost") as iface:
            iface.showInfo()
            iface.localNode.showInfo()
            iface.localNode.exitSimulator()
        logger.info("Integration test successful!")
    except Exception:  # intentional catch-all for test exit-signaling
        logger.exception("Error while testing simulator")
        sys.exit(1)
    sys.exit(0)
