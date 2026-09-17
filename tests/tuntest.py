"""Manual TUN packet-read utility used for ad-hoc interactive debugging.

This script intentionally lives in tests/ for historical reasons, but it is not a
pytest-driven unit test.
"""

# Uses the in-tree LinuxTunDevice adapter (replaces the abandoned PyTap2 package).
# make sure to "sudo setcap cap_net_admin+eip /usr/bin/python3.10" so python can
# access tun device without being root
# sudo ip link delete tun0

# TODO: set MTU correctly (issue #9001)
# TODO: select local ip address based on nodeid (issue #9002)
# TODO: print known node ids as IP addresses (issue #9003)

import logging
import threading

from meshtastic.tunnel_device import LinuxTunDevice

# A set of chatty UDP services we should never accidentally
# forward to our slow network.
UDP_BLACKLIST: set[int] = {
    1900,  # SSDP
    5353,  # multicast DNS
}

# A set of TCP services to block.
TCP_BLACKLIST: set[int] = set()

# A set of protocols we ignore.
PROTOCOL_BLACKLIST: set[int] = {
    0x02,  # IGMP
    0x80,  # Service-Specific Connection-Oriented Protocol in a Multilink and Connectionless Environment
}


def _ipstr(barray: bytes | bytearray) -> str:
    """Render IPv4 bytes as dotted-decimal text.

    Parameters
    ----------
    barray : bytes | bytearray
        IPv4 address bytes.

    Returns
    -------
    str
        Dotted-decimal IPv4 string.
    """
    return ".".join(str(x) for x in barray)


def _readnet_u16(p: bytes | bytearray | memoryview, offset: int) -> int:
    """Read a network-order unsigned 16-bit integer.

    Parameters
    ----------
    p : bytes | bytearray | memoryview
        Packet buffer.
    offset : int
        Start offset for the 16-bit field.

    Returns
    -------
    int
        Parsed unsigned 16-bit value.
    """
    if not isinstance(offset, int):
        raise ValueError(f"offset must be int, got {type(offset).__name__}")
    if offset < 0 or offset + 1 >= len(p):
        raise ValueError(
            f"Cannot read u16 at offset {offset}: packet length is {len(p)}"
        )
    return p[offset] * 256 + p[offset + 1]


def _internet_checksum(payload: bytes) -> int:
    """Compute RFC 1071 Internet checksum for the provided payload.

    Parameters
    ----------
    payload : bytes
        Payload bytes to checksum.

    Returns
    -------
    int
        16-bit one's-complement checksum value.
    """
    if len(payload) % 2:
        payload += b"\x00"
    checksum = 0
    for i in range(0, len(payload), 2):
        checksum += (payload[i] << 8) + payload[i + 1]
        checksum = (checksum & 0xFFFF) + (checksum >> 16)
    return (~checksum) & 0xFFFF


def _readtest(tap: LinuxTunDevice) -> None:
    """Read packets from a TUN device and log/filter protocol details.

    Parameters
    ----------
    tap : LinuxTunDevice
        The TUN device to read packets from.
    """
    while True:
        p = bytes(tap.read())
        if len(p) < 20:
            logging.debug(
                "Dropping malformed IPv4 packet: too short (%d bytes)", len(p)
            )
            continue

        protocol = p[8 + 1]
        srcaddr = p[12:16]
        destaddr = p[16:20]
        version = p[0] >> 4
        ip_header_len = (p[0] & 0x0F) * 4
        if version != 4 or ip_header_len < 20 or len(p) < ip_header_len:
            logging.debug(
                "Dropping malformed IPv4 packet: version=%d, header length=%d",
                version,
                ip_header_len,
            )
            continue
        ignore = False  # Assume we will be forwarding the packet
        if protocol in PROTOCOL_BLACKLIST:
            ignore = True
            logging.debug("Ignoring blacklisted protocol 0x%02x", protocol)
        elif protocol == 0x01:  # ICMP
            icmp_offset = ip_header_len
            if len(p) <= icmp_offset:
                logging.debug("Ignoring malformed ICMP packet: missing type byte")
                continue
            icmp_type = p[icmp_offset]
            if icmp_type == 0x08:  # echo request -> echo reply
                if len(p) < icmp_offset + 8:
                    logging.debug(
                        "Ignoring malformed ICMP echo request: too short for header"
                    )
                    continue
                logging.debug("Generating fake ping reply")
                icmp_reply = bytearray(p[icmp_offset:])
                icmp_reply[0] = 0x00  # Echo reply type
                icmp_reply[2:4] = b"\x00\x00"
                checksum = _internet_checksum(bytes(icmp_reply))
                icmp_reply[2] = (checksum >> 8) & 0xFF
                icmp_reply[3] = checksum & 0xFF
                ip_header = bytearray(p[:ip_header_len])
                ip_header[10:12] = b"\x00\x00"
                ip_header[12:16] = p[16:20]
                ip_header[16:20] = p[12:16]
                ip_checksum = _internet_checksum(bytes(ip_header))
                ip_header[10] = (ip_checksum >> 8) & 0xFF
                ip_header[11] = ip_checksum & 0xFF
                pingback = bytes(ip_header) + bytes(icmp_reply)
                try:
                    tap.write(pingback)
                except OSError as ex:
                    logging.debug("Ignoring transient TUN write error: %s", ex)
                ignore = True  # Don't forward the original request.
            else:
                logging.debug("Ignoring ICMP type %d (not echo request)", icmp_type)
                ignore = True  # Don't forward unsupported ICMP types.
        elif protocol == 0x11:  # UDP
            if len(p) < ip_header_len + 4:
                logging.debug("Ignoring malformed UDP packet: too short for ports")
                continue
            srcport = _readnet_u16(p, ip_header_len)
            destport = _readnet_u16(p, ip_header_len + 2)
            logging.debug("udp srcport=%d, destport=%d", srcport, destport)
            if destport in UDP_BLACKLIST:
                ignore = True
                logging.debug("ignoring blacklisted UDP port %d", destport)
        elif protocol == 0x06:  # TCP
            if len(p) < ip_header_len + 4:
                logging.debug("Ignoring malformed TCP packet: too short for ports")
                continue
            srcport = _readnet_u16(p, ip_header_len)
            destport = _readnet_u16(p, ip_header_len + 2)
            logging.debug("tcp srcport=%d, destport=%d", srcport, destport)
            if destport in TCP_BLACKLIST:
                ignore = True
                logging.debug("ignoring blacklisted TCP port %d", destport)
        else:
            logging.warning(
                "unexpected protocol 0x%02x, src=%s, dest=%s",
                protocol,
                _ipstr(srcaddr),
                _ipstr(destaddr),
            )

        if not ignore:
            logging.debug(
                "Packet eligible for forwarding byte_len=%d src=%s, dest=%s",
                len(p),
                _ipstr(srcaddr),
                _ipstr(destaddr),
            )


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    tun = LinuxTunDevice(mtu=200)
    try:
        # tun.create()
        tun.up()
        # mtu is passed explicitly: the constructor value is only recorded,
        # it is not applied to the interface by itself.
        tun.ifconfig(address="10.115.1.2", netmask="255.255.0.0", mtu=200)

        reader_thread = threading.Thread(target=_readtest, args=(tun,), daemon=True)
        reader_thread.start()
        input("press return key to quit!")
    finally:
        tun.close()
