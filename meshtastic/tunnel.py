"""Code for IP tunnel over a mesh.

# The tunnel uses the in-tree LinuxTunDevice adapter (meshtastic/tunnel_device.py),
# which replaced the abandoned PyTap2 dependency.
# run the tunnel with root/sudo, or via a launcher granting CAP_NET_ADMIN only
# to the tunnel process -- do not setcap the shared Python interpreter
# sudo ip link delete mesh0
# sudo bin/run.sh --port /dev/ttyUSB0 --setch-shortfast
# sudo bin/run.sh --port /dev/ttyUSB0 --tunnel --debug
# ssh -Y root@192.168.10.151 (or dietpi), default password p
# ncat -e /bin/cat -k -u -l 1235
# ncat -u 10.115.64.152 1235
# ping -c 1 -W 20 10.115.64.152
# ping -i 30 -W 30 10.115.64.152

"""

import logging
import platform
import threading
from contextlib import suppress
from typing import Any

from pubsub import pub

from meshtastic import mt_config
from meshtastic.protobuf import mesh_pb2, portnums_pb2
from meshtastic.tunnel_device import LinuxTunDevice
from meshtastic.util import ipstr, readnet_u16

logger = logging.getLogger(__name__)
TRACE_LEVEL = 5
logging.addLevelName(TRACE_LEVEL, "TRACE")
TUNNEL_TOPIC = "meshtastic.receive.data.IP_TUNNEL_APP"

# Reserve ownership through construction and teardown, not just while the
# public singleton is available for receive dispatch.
_tunnel_lock = threading.Lock()
_tunnel_owner: "Tunnel | None" = None

# IP Protocol numbers (RFC 790)
IP_PROTOCOL_ICMP = 0x01
IP_PROTOCOL_IGMP = 0x02
IP_PROTOCOL_TCP = 0x06
IP_PROTOCOL_UDP = 0x11
IP_PROTOCOL_SCCOPMCE = 0x80  # Service-Specific Connection-Oriented Protocol

# Bitmask for IP address octet extraction
IP_OCTET_MASK = 0xFF

# Tunnel node mapping constants
OCTET_MULTIPLIER = 256
NODE_NUM_MASK = 0xFFFF
RX_THREAD_JOIN_TIMEOUT = 2.0
MIN_IPV4_HEADER_LEN = 20
MIN_ICMP_HEADER_LEN = 4
MIN_TRANSPORT_HEADER_LEN = 4
IP_PROTOCOL_OFFSET = 9
IP_SRC_ADDR_OFFSET = 12
IP_DEST_ADDR_OFFSET = 16
TUN_MTU: int = mesh_pb2.Constants.DATA_PAYLOAD_LEN


def onTunnelReceive(packet: dict[str, Any], interface: Any) -> None:
    """Handle received tunneled messages from mesh.

    Parameters
    ----------
    packet : dict
        Mesh packet containing the tunneled data payload.
    interface : Any
        Interface object that received the packet.
    """
    logger.debug("in onTunnelReceive()")
    with _tunnel_lock:
        tunnel_instance = _tunnel_owner
        if tunnel_instance is not None and (
            tunnel_instance._initializing or tunnel_instance._stop_event.is_set()
        ):
            tunnel_instance = None
    if tunnel_instance is None:
        logger.warning("Received tunnel packet but no active tunnel instance is set.")
        return
    if interface is not tunnel_instance.iface:
        logger.debug("Ignoring tunnel packet from a different interface")
        return
    tunnel_instance.onReceive(packet)


class Tunnel:
    """A TUN based IP tunnel over meshtastic (one active tunnel per process)."""

    LOG_TRACE = TRACE_LEVEL
    # Default packet filters (copied into instance-level compatibility attributes).
    UDP_BLACKLIST_DEFAULT: frozenset[int] = frozenset(
        {
            1900,  # SSDP
            5353,  # multicast DNS
            9001,  # Yggdrasil multicast discovery
            64512,  # cjdns beacon
        }
    )
    TCP_BLACKLIST_DEFAULT: frozenset[int] = frozenset(
        {
            5900,  # VNC (note: currently used for testing coverage).
        }
    )
    PROTOCOL_BLACKLIST_DEFAULT: frozenset[int] = frozenset(
        {
            IP_PROTOCOL_IGMP,
            IP_PROTOCOL_SCCOPMCE,
        }
    )

    class TunnelError(Exception):
        """An exception class for general tunnel errors."""

        def __init__(self, message: str) -> None:
            """Initialize the TunnelError with a human-readable message.

            Parameters
            ----------
            message : str
                Description of the tunnel-related error.
            """
            self.message = message
            super().__init__(self.message)

    class NonLinuxError(TunnelError):
        """Raised when Tunnel is instantiated on a non-Linux system."""

        def __init__(self) -> None:
            super().__init__("Tunnel() can only be instantiated on a Linux system")

    class UninitializedInterfaceError(TunnelError):
        """Raised when the interface does not yet have myInfo initialized."""

        def __init__(self) -> None:
            super().__init__("Tunnel() requires iface.myInfo to be initialized")

    def __init__(
        self,
        iface: Any,
        subnet: str = "10.115",
        netmask: str = "255.255.0.0",
    ) -> None:
        """Initialize a Tunnel bound to a mesh interface and subnet.

        Creates and configures tunnel state, registers this instance as the global
        mt_config.tunnel_instance, and conditionally creates and brings up a TUN
        device (LinuxTunDevice) and a background reader thread unless the mesh
        interface has noProto enabled.

        Parameters
        ----------
        iface : Any
            An already-open MeshInterface instance providing .myInfo, .nodes,
            .node numbers, .noProto, and .sendData behavior.
        subnet : str
            Subnet prefix used to form tunnel IPs (default "10.115").
        netmask : str
            Netmask to assign to the TUN device (default "255.255.0.0").

        Raises
        ------
        Tunnel.TunnelError
            If iface, subnet, or netmask is missing, if the process is not
            running on Linux, if another tunnel owns the process slot, or if
            the tunnel is closed while initialization is still running.
        """

        if not iface:
            raise Tunnel.TunnelError("Tunnel() must have an interface")

        if not subnet:
            raise Tunnel.TunnelError("Tunnel() must have a subnet")

        if not netmask:
            raise Tunnel.TunnelError("Tunnel() must have a netmask")

        self.iface = iface
        self.subnetPrefix = subnet
        self._subscribed = False
        self._stop_event = threading.Event()
        self._rx_thread: threading.Thread | None = None
        self._initializing = True
        self._ready_event = threading.Event()
        self._closing = False
        self._cleanup_complete = False
        self._active_sends = 0
        self.tun: LinuxTunDevice | None = None

        if platform.system() != "Linux":
            raise Tunnel.NonLinuxError()

        my_info = self.iface.myInfo
        if my_info is None:
            raise Tunnel.UninitializedInterfaceError()

        # Per-instance copies preserve historical mutability of these attributes.
        self.UDP_BLACKLIST: set[int] = set(self.UDP_BLACKLIST_DEFAULT)
        self.TCP_BLACKLIST: set[int] = set(self.TCP_BLACKLIST_DEFAULT)
        self.PROTOCOL_BLACKLIST: set[int] = set(self.PROTOCOL_BLACKLIST_DEFAULT)

        # COMPAT_STABLE_SHIM: legacy instance attribute aliases.
        self.udpBlacklist = self.UDP_BLACKLIST
        self.tcpBlacklist = self.TCP_BLACKLIST
        self.protocolBlacklist = self.PROTOCOL_BLACKLIST

        logger.info("Starting IP to mesh tunnel. Mesh members:")

        global _tunnel_owner  # pylint: disable=global-statement
        try:
            with _tunnel_lock:
                if _tunnel_owner is not None:
                    raise Tunnel.TunnelError(
                        "Only one active Tunnel is supported per process"
                    )
                _tunnel_owner = self
            myAddr = self._node_num_to_ip(my_info.my_node_num)

            if self.iface.nodes:
                for node in self.iface.nodes.values():
                    nodeId = node["user"]["id"]
                    ip = self._node_num_to_ip(node["num"])
                    logger.info("Node %s has IP address %s", nodeId, ip)

            # Data.payload is the tunneled IP packet itself. Keep the TUN MTU
            # pinned to the generated firmware/protobuf payload contract.
            if self.iface.noProto:
                logger.warning(
                    "Not creating a TUN device because it is disabled by noProto"
                )
            else:
                logger.debug("creating TUN device with MTU=%d", TUN_MTU)
                logger.info(
                    "Creating TUN device; CAP_NET_ADMIN or root is typically required."
                )
                self._check_setup_cancelled()
                self.tun = LinuxTunDevice(name="mesh")
                self._check_setup_cancelled()
                self.tun.up()
                self._check_setup_cancelled()
                self.tun.ifconfig(address=myAddr, netmask=netmask, mtu=TUN_MTU)

            self._check_setup_cancelled()

            # The reservation prevents another constructor or teardown from
            # touching this subscription. Keep pubsub hooks outside the lock.
            # Mark intent first in case subscribe partially succeeds then raises.
            self._subscribed = True
            pub.subscribe(onTunnelReceive, TUNNEL_TOPIC)
            self._check_setup_cancelled()

            if self.iface.noProto:
                logger.warning(
                    "Not starting TUN reader because it is disabled by noProto"
                )
            else:
                logger.debug("starting TUN reader, our IP address is %s", myAddr)
                self._rx_thread = threading.Thread(
                    target=self._run_reader, args=(), daemon=True
                )
                self._rx_thread.start()

            with _tunnel_lock:
                # Publish only a fully initialized owner. A concurrent close()
                # may retire this instance during initialization; publishing a
                # retired tunnel would wedge the process slot permanently.
                self._check_setup_cancelled()
                mt_config.tunnel_instance = self
                self._initializing = False
                self._ready_event.set()
        except BaseException:
            self._retire()
            raise

    def _check_setup_cancelled(self) -> None:
        """Stop setup at resource boundaries; the constructor owns rollback."""
        if self._stop_event.is_set():
            raise Tunnel.TunnelError("Tunnel closed during initialization")

    def onReceive(self, packet: dict[str, Any]) -> None:
        """Handle an incoming mesh packet and forward its payload into the TUN device when appropriate.

        Ignores packets originating from the local node. If protocol handling is enabled (iface.noProto is False)
        and the packet is not filtered by _should_filter_packet, writes packet["decoded"]["payload"] to the TUN device.

        Parameters
        ----------
        packet : dict
            Mesh packet; expected to contain a "from" node number and a "decoded" dict with a "payload" bytes object.
        """
        if self._stop_event.is_set():
            return
        if not self.iface or not getattr(self.iface, "myInfo", None):
            logger.debug("Ignoring tunnel packet because iface.myInfo is unavailable")
            return

        my_info = self.iface.myInfo
        if packet.get("from") == my_info.my_node_num:
            logger.debug("Ignoring message we sent")
            return
        decoded = packet.get("decoded")
        if not isinstance(decoded, dict):
            logger.debug("Ignoring tunnel packet with missing/invalid decoded field")
            return
        p = decoded.get("payload")
        if not isinstance(p, (bytes, bytearray)):
            logger.debug("Ignoring tunnel packet with missing/invalid payload field")
            return
        payload = bytes(p)
        logger.debug(
            "Received mesh tunnel message type=%s len=%d",
            type(payload),
            len(payload),
        )
        # we don't really need to check for filtering here (sender should have checked),
        # but this provides useful debug printing on types of packets received
        if not self.iface.noProto:
            device = self.tun
            if device is not None and not self._should_filter_packet(payload):
                try:
                    if not self._stop_event.is_set():
                        device.write(payload)
                except BlockingIOError:
                    logger.debug("TUN write skipped: kernel queue is full")
                except OSError:
                    if self._stop_event.is_set():
                        logger.debug("TUN write skipped: device closed during shutdown")
                    else:
                        logger.warning("TUN write failed", exc_info=True)

    def _should_filter_packet(self, p: bytes) -> bool:
        """Decides whether an IPv4 packet should be ignored based on its protocol and port blacklists.

        Parameters
        ----------
        p : bytes
            Raw IPv4 packet bytes beginning at the IP header.

        Returns
        -------
        bool
            `True` if the packet should be ignored (filtered), `False` otherwise.
        """
        if len(p) < MIN_IPV4_HEADER_LEN:
            logger.debug("Ignoring short IP packet (len=%d)", len(p))
            return True

        protocol = p[IP_PROTOCOL_OFFSET]
        src_addr = p[IP_SRC_ADDR_OFFSET : IP_SRC_ADDR_OFFSET + 4]
        dest_addr = p[IP_DEST_ADDR_OFFSET : IP_DEST_ADDR_OFFSET + 4]
        subheader = MIN_IPV4_HEADER_LEN
        ignore = False  # Assume we will be forwarding the packet
        if protocol in self.PROTOCOL_BLACKLIST:
            ignore = True
            logger.log(self.LOG_TRACE, "Ignoring blacklisted protocol 0x%02x", protocol)
        elif protocol == IP_PROTOCOL_ICMP:
            if len(p) < MIN_IPV4_HEADER_LEN + MIN_ICMP_HEADER_LEN:
                logger.debug("Ignoring short ICMP packet (len=%d)", len(p))
                return True
            icmpType = p[MIN_IPV4_HEADER_LEN]
            icmpCode = p[MIN_IPV4_HEADER_LEN + 1]
            checksum = p[MIN_IPV4_HEADER_LEN + 2 : MIN_IPV4_HEADER_LEN + 4]
            # pylint: disable=line-too-long
            logger.debug(
                "forwarding ICMP message src=%s, dest=%s, type=%d, code=%d, checksum=%s",
                ipstr(src_addr),
                ipstr(dest_addr),
                icmpType,
                icmpCode,
                checksum.hex(),
            )
            # reply to pings (swap src and dest but keep rest of packet unchanged)
            # pingback = p[:12]+p[16:20]+p[12:16]+p[20:]
            # tap.write(pingback)
        elif protocol == IP_PROTOCOL_UDP:
            if len(p) < MIN_IPV4_HEADER_LEN + MIN_TRANSPORT_HEADER_LEN:
                logger.debug("Ignoring short UDP packet (len=%d)", len(p))
                return True
            srcport = readnet_u16(p, subheader)
            destport = readnet_u16(p, subheader + 2)
            if destport in self.UDP_BLACKLIST:
                ignore = True
                logger.log(self.LOG_TRACE, "ignoring blacklisted UDP port %s", destport)
            else:
                logger.debug(
                    "forwarding udp srcport=%s, destport=%s", srcport, destport
                )
        elif protocol == IP_PROTOCOL_TCP:
            if len(p) < MIN_IPV4_HEADER_LEN + MIN_TRANSPORT_HEADER_LEN:
                logger.debug("Ignoring short TCP packet (len=%d)", len(p))
                return True
            srcport = readnet_u16(p, subheader)
            destport = readnet_u16(p, subheader + 2)
            if destport in self.TCP_BLACKLIST:
                ignore = True
                logger.log(self.LOG_TRACE, "ignoring blacklisted TCP port %s", destport)
            else:
                logger.debug(
                    "forwarding tcp srcport=%s, destport=%s", srcport, destport
                )
        else:
            logger.warning(
                "forwarding unexpected protocol 0x%02x, src=%s, dest=%s",
                protocol,
                ipstr(src_addr),
                ipstr(dest_addr),
            )

        return ignore

    def _run_reader(self) -> None:
        """Wait for publication, then retire the reader's ownership on exit."""
        try:
            self._ready_event.wait()
            if not self._stop_event.is_set():
                self._tun_reader()
        finally:
            with _tunnel_lock:
                self._rx_thread = None
                self._release_if_drained_locked()

    def _tun_reader(self) -> None:
        """Background thread that reads IP packets from the TUN device and forwards them to the mesh.

        Continuously reads packets from the TUN device, checks if they should be filtered,
        and sends non-filtered packets to the appropriate mesh node based on destination IP.
        """
        tap = self.tun
        if tap is None:
            logger.debug("TUN reader exiting: no active TUN device")
            return
        logger.debug("TUN reader running")
        while not self._stop_event.is_set():
            try:
                p = tap.read()
            except OSError:
                if self._stop_event.is_set():
                    break
                logger.exception("TUN reader terminating due to read failure")
                break
            # A retired reader must discard any packet returned during shutdown.
            if self._stop_event.is_set():
                break
            # logger.debug(f"IP packet received on TUN interface, type={type(p)}")
            dest_addr = p[IP_DEST_ADDR_OFFSET : IP_DEST_ADDR_OFFSET + 4]

            if not self._should_filter_packet(p) and not self._stop_event.is_set():
                try:
                    self._send_packet(dest_addr, p)
                except Exception:
                    logger.warning(
                        "TUN packet could not be sent to the mesh", exc_info=True
                    )

    def _ip_to_node_id(self, ip_addr: bytes) -> str | None:
        """Convert a 4-byte IP address to the corresponding mesh node ID.

        Uses the last 16 bits of the IP address to match against the low 16 bits
        of known node numbers in the mesh.

        Parameters
        ----------
        ip_addr : bytes
            4-byte IPv4 address in network byte order.

        Returns
        -------
        str | None
            The mesh node ID string if a matching node is found, "^all" for
            broadcast address 255.255, or None if no matching node exists.
        """
        if len(ip_addr) < 4:
            logger.debug("Ignoring short destination address (len=%d)", len(ip_addr))
            return None
        ip_bits = ip_addr[2] * OCTET_MULTIPLIER + ip_addr[3]

        if ip_bits == NODE_NUM_MASK:
            return "^all"

        if not self.iface.nodes:
            return None

        for node in self.iface.nodes.values():
            node_num = node.get("num") if isinstance(node, dict) else None
            if not isinstance(node_num, int):
                continue
            node_num &= NODE_NUM_MASK
            # logger.debug(f"Considering nodenum 0x{node_num:x} for ipBits 0x{ip_bits:x}")
            if node_num == ip_bits:
                user = node.get("user") if isinstance(node, dict) else None
                if isinstance(user, dict):
                    node_id = user.get("id")
                    if isinstance(node_id, str):
                        return node_id
        return None

    def _node_num_to_ip(self, node_num: int) -> str:
        """Construct an IPv4 address in the tunnel subnet for a given node number.

        Parameters
        ----------
        node_num : int
            Node number; the low 16 bits are used to form the final two octets of the returned address.

        Returns
        -------
        str
            IPv4 address string in the form "<subnetPrefix>.<high octet>.<low octet>".
        """
        return f"{self.subnetPrefix}.{(node_num >> 8) & IP_OCTET_MASK}.{node_num & IP_OCTET_MASK}"

    def _send_packet(self, dest_addr: bytes, p: bytes) -> None:
        """Forward an IP packet to the corresponding mesh node or drop it if no node mapping exists.

        Parameters
        ----------
        dest_addr : bytes
            4-byte IPv4 address in network byte order identifying the packet's destination.
        p : bytes
            Raw IP packet bytes to be forwarded.
        """
        nodeId = self._ip_to_node_id(dest_addr)
        if nodeId is not None:
            logger.debug(
                "Forwarding packet bytelen=%d dest=%s, destNode=%s",
                len(p),
                ipstr(dest_addr),
                nodeId,
            )
            # Admit the send under the same lock that retires the owner. Do
            # not hold that lock across transport I/O (which may reenter close).
            with _tunnel_lock:
                if (
                    _tunnel_owner is not self
                    or self._initializing
                    or self._stop_event.is_set()
                ):
                    return
                self._active_sends += 1
            try:
                self.iface.sendData(
                    p, nodeId, portnums_pb2.IP_TUNNEL_APP, wantAck=False
                )
            finally:
                with _tunnel_lock:
                    self._active_sends -= 1
                    self._release_if_drained_locked()
        else:
            logger.warning(
                "Dropping packet because no node found for destIP=%s",
                ipstr(dest_addr),
            )

    def _release_if_drained_locked(self) -> None:
        """Release only after cleanup, reader exit and admitted sends finish."""
        global _tunnel_owner  # pylint: disable=global-statement
        if (
            _tunnel_owner is self
            and self._cleanup_complete
            and not self._active_sends
            and (self._rx_thread is None or not self._rx_thread.is_alive())
        ):
            self._rx_thread = None
            _tunnel_owner = None

    def close(self) -> None:
        """Cancel setup or retire an active tunnel.

        Setup owns its rollback until publication. A timed-out reader or an
        admitted transport send retains the process slot until it drains;
        closing never permits overlapping tunnel owners.
        """
        with _tunnel_lock:
            if _tunnel_owner is not self:
                return
            self._stop_event.set()
            if self._initializing:
                return
        self._retire()

    def _retire(self) -> None:
        """Clean up the reserved instance, including failed construction."""
        with _tunnel_lock:
            if _tunnel_owner is not self or self._closing:
                return
            self._closing = True
            self._stop_event.set()
            self._ready_event.set()
            # The public singleton is a compatibility mirror, not ownership.
            # Even a caller overwriting it cannot orphan our subscription.
            mt_config.tunnel_instance = None
            owns_subscription = self._subscribed
            self._subscribed = False
            device = self.tun
            self.tun = None
            thread = self._rx_thread
        try:
            try:
                if owns_subscription:
                    with suppress(Exception):
                        pub.unsubscribe(onTunnelReceive, TUNNEL_TOPIC)
            finally:
                # Preserve interruptions, but never skip native resource cleanup.
                try:
                    if device is not None:
                        try:
                            device.close()
                        except Exception:
                            logger.exception("Error closing TUN device")
                finally:
                    if (
                        thread is not None
                        and thread is not threading.current_thread()
                        and thread.is_alive()
                    ):
                        thread.join(timeout=RX_THREAD_JOIN_TIMEOUT)
                        if thread.is_alive():
                            logger.warning(
                                "TUN reader still active; retaining tunnel ownership"
                            )
        finally:
            with _tunnel_lock:
                self._cleanup_complete = True
                self._release_if_drained_locked()

    # COMPAT_STABLE_SHIM: backward-compatible aliases for existing callers/tests.
    def _shouldFilterPacket(self, p: bytes) -> bool:
        """Compatibility wrapper for _should_filter_packet."""
        return self._should_filter_packet(p)

    def _ipToNodeId(self, ipAddr: bytes) -> str | None:
        """Compatibility wrapper for _ip_to_node_id."""
        return self._ip_to_node_id(ipAddr)

    def _nodeNumToIp(self, nodeNum: int) -> str:
        """Compatibility wrapper for _node_num_to_ip."""
        return self._node_num_to_ip(nodeNum)

    def sendPacket(self, destAddr: bytes, p: bytes) -> None:
        """Compatibility wrapper for _send_packet."""
        self._send_packet(destAddr, p)
