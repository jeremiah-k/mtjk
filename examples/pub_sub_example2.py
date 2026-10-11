"""Simple program to demo how to use meshtastic library.

To run: `python examples/pub_sub_example2.py host`.
"""

import sys
import time
from typing import Any

from pubsub import pub

import meshtastic.tcp_interface


def onReceive(packet: Any, _interface: Any) -> None:
    """Handle an incoming packet."""
    print(f"Received: {packet}")


def onConnection(interface: Any, _topic: Any = pub.AUTO_TOPIC) -> None:
    """Handle (re)connection to the radio."""
    # defaults to broadcast, specify a destination ID if you wish
    interface.sendText("hello mesh")


def main() -> None:
    """Connect to a TCP radio, print received packets, and send a greeting on connect."""
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} host", file=sys.stderr)
        sys.exit(1)

    recv_topic = "meshtastic.receive"
    conn_topic = "meshtastic.connection.established"
    pub.subscribe(onReceive, recv_topic)
    pub.subscribe(onConnection, conn_topic)
    try:
        with meshtastic.tcp_interface.TCPInterface(hostname=sys.argv[1]):
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        print("Exiting...")
    except OSError as exc:
        print(f"Error: Could not connect to {sys.argv[1]} ({exc})", file=sys.stderr)
        sys.exit(1)
    finally:
        pub.unsubscribe(onReceive, recv_topic)
        pub.unsubscribe(onConnection, conn_topic)


if __name__ == "__main__":
    main()
