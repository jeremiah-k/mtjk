"""Interactive text chat demo.

Purpose: demonstrate bidirectional text chat loop.
Transport scope: Serial default, optional TCP/BLE.
Behavior: prints incoming messages and sends each typed line as text.
Expected output: incoming sender/text lines and sent messages reaching peers.
Cleanup/error handling: explicit connect errors and graceful Ctrl+C / EOF close.
"""

import argparse
from typing import Any

from pubsub import pub

import meshtastic.ble_interface
import meshtastic.serial_interface
import meshtastic.tcp_interface
from meshtastic.mesh_interface import MeshInterface


def onReceive(
    packet: dict, interface: MeshInterface
) -> None:  # pylint: disable=unused-argument
    """Handle a received packet."""
    text: str | None = packet.get("decoded", {}).get("text")
    if text:
        # Filter out local echo — user already sees their own input
        if interface.myInfo and packet.get("from") == interface.myInfo.my_node_num:
            return
        sender: str = packet.get("fromId", "unknown")
        print(f"{sender}: {text}")


def onConnection(
    interface: MeshInterface, topic: Any = pub.AUTO_TOPIC
) -> None:  # pylint: disable=unused-argument
    """Handle a connection established event."""
    print("Connected. Type a message and press Enter to send. Ctrl+C to exit.")


def main() -> int:
    """Parse args, connect to a radio, and run an interactive text chat loop."""
    parser = argparse.ArgumentParser(description="Meshtastic text chat demo")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--host", help="Connect via TCP to this hostname or IP")
    group.add_argument(
        "--ble", help="Connect via BLE to this MAC address or device name"
    )

    args = parser.parse_args()

    pub.subscribe(onReceive, "meshtastic.receive")
    pub.subscribe(onConnection, "meshtastic.connection.established")

    iface: (
        meshtastic.serial_interface.SerialInterface
        | meshtastic.ble_interface.BLEInterface
        | meshtastic.tcp_interface.TCPInterface
        | None
    ) = None

    # defaults to serial, use --host for TCP or --ble for Bluetooth
    try:
        if args.host:
            # note: timeout only applies after connection, not during the initial connect attempt
            # TCPInterface.myConnect() calls socket.create_connection() without a timeout
            iface = meshtastic.tcp_interface.TCPInterface(
                hostname=args.host, timeout=10
            )
        elif args.ble:
            iface = meshtastic.ble_interface.BLEInterface(address=args.ble, timeout=10)
        else:
            iface = meshtastic.serial_interface.SerialInterface(timeout=10)
    except KeyboardInterrupt as exc:
        raise SystemExit(0) from exc
    except Exception as e:
        print(f"Error: Could not connect. {e}")
        return 1

    assert iface is not None
    try:
        while True:
            line = input()
            if line:
                iface.sendText(line)
    except KeyboardInterrupt:
        return 0
    except EOFError:
        return 0
    finally:
        if iface:
            iface.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
