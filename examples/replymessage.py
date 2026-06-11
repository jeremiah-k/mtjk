"""Auto-reply to received text messages.

Purpose: demonstrate receive callback + generated reply flow.
Transport scope: Serial default, optional TCP/BLE.
Behavior: listens for text, prints message metadata, sends one reply per text message.
Expected output: "Connected..." plus message/reply lines while running.
Cleanup/error handling: clear connect failures and graceful Ctrl+C close.
"""

import argparse
import time
from typing import Any

from pubsub import pub

import meshtastic.ble_interface
import meshtastic.serial_interface
import meshtastic.tcp_interface
from meshtastic.mesh_interface import MeshInterface

# Type alias for the union of supported interface types
Interface = (
    meshtastic.serial_interface.SerialInterface
    | meshtastic.ble_interface.BLEInterface
    | meshtastic.tcp_interface.TCPInterface
)


def onReceive(packet: dict[str, Any], interface: MeshInterface) -> None:
    """Reply to every received packet with some info."""
    text: str | None = packet.get("decoded", {}).get("text")
    if text:
        # Prevent infinite loop: ignore own messages and auto-reply echoes
        if interface.myInfo and packet.get("from") == interface.myInfo.my_node_num:
            return
        if text.startswith("got msg '"):
            return
        rx_snr: Any = packet.get("rxSnr", "unknown")
        hop_limit: Any = packet.get("hopLimit", "unknown")
        print(f"message: {text}")
        reply: str = f"got msg '{text}' with rxSnr: {rx_snr} and hopLimit: {hop_limit}"
        print("Sending reply: ", reply)
        interface.sendText(reply, channelIndex=packet.get("channel", 0))


def onConnection(  # pylint: disable=unused-argument
    interface: MeshInterface, topic: Any = pub.AUTO_TOPIC
) -> None:
    """Handle a connection established event."""
    print("Connected. Will auto-reply to all messages while running.")


def main() -> int:
    """Parse args, connect to a radio, and auto-reply to received messages."""
    parser = argparse.ArgumentParser(description="Meshtastic Auto-Reply Feature Demo")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--host", help="Connect via TCP to this hostname or IP")
    group.add_argument("--ble", help="Connect via BLE to this MAC address")

    args = parser.parse_args()

    pub.subscribe(onReceive, "meshtastic.receive")
    pub.subscribe(onConnection, "meshtastic.connection.established")

    iface: Interface | None = None

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
    except Exception as exc:
        print(f"Error: Could not connect. {exc}")
        return 1

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return 0
    finally:
        if iface:
            iface.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
