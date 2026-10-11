"""Simple program to demo how to use meshtastic library.

To run: `python examples/pub_sub_example.py host`.
"""

import sys
import threading
from typing import Any

from pubsub import pub

import meshtastic.tcp_interface

_CONNECTED = threading.Event()
CONNECTION_WAIT_TIMEOUT_SEC = 30


def onConnection(interface: Any, _topic: Any = pub.AUTO_TOPIC) -> None:
    """Handle (re)connection to the radio."""
    print(interface.myInfo)
    _CONNECTED.set()


def main() -> None:
    """Connect to a TCP radio and print local node info on connection."""
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} host", file=sys.stderr)
        sys.exit(1)
    hostname = sys.argv[1]

    _CONNECTED.clear()
    topic = "meshtastic.connection.established"
    pub.subscribe(onConnection, topic)
    try:
        # Wait until the connection callback runs, then exit.
        with meshtastic.tcp_interface.TCPInterface(hostname=hostname):
            if not _CONNECTED.wait(timeout=CONNECTION_WAIT_TIMEOUT_SEC):
                print(
                    "Error: Timed out waiting for connection callback",
                    file=sys.stderr,
                )
                sys.exit(1)
    except OSError as exc:
        print(f"Error: Could not connect to {hostname} ({exc})", file=sys.stderr)
        sys.exit(1)
    finally:
        pub.unsubscribe(onConnection, topic)


if __name__ == "__main__":
    main()
