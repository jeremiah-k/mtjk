"""# A library for the Meshtastic Client API.

Primary interfaces: SerialInterface, TCPInterface, BLEInterface

Install with pip: "[pip3 install mtjk](https://pypi.org/project/mtjk/)"

Source code on [github](https://github.com/jeremiah-k/mtjk)

notable properties of interface classes:

- `nodes` - The database of received nodes.  Includes always up-to-date location and username information for each
node in the mesh.  This is a read-only datastructure.
- `nodesByNum` - like "nodes" but keyed by nodeNum instead of nodeId. As such, includes "unknown" nodes which haven't seen a User packet yet
- `myInfo` & `metadata` - Contain read-only information about the local radio device (software version, hardware version, etc)
- `localNode` - Pointer to a node object for the local node

notable properties of nodes:

- `localConfig` - Current radio settings, can be written to the radio with the `writeConfig` method.
- `moduleConfig` - Current module settings, can be written to the radio with the `writeConfig` method.
- `channels` - The node's channels, keyed by index.

# Published PubSub topics

We use a [publish-subscribe](https://pypubsub.readthedocs.io/en/v4.0.3/) model to communicate asynchronous events.  Available
topics:

- `meshtastic.connection.established` - published once we've successfully connected to the radio and downloaded the node DB
- `meshtastic.connection.lost` - published once we've lost our link to the radio
- `meshtastic.receive.text(packet)` - delivers a received packet as a dictionary, if you only care about a particular
type of packet, you should subscribe to the full topic name.  If you want to see all packets, simply subscribe to "meshtastic.receive".
- `meshtastic.receive.position(packet)`
- `meshtastic.receive.user(packet)`
- `meshtastic.receive.data.portnum(packet)` (where portnum is an integer or well known PortNum enum)
- `meshtastic.node.updated(node = NodeInfo)` - published when a node in the DB changes (appears, location changed, username changed, etc...)
- `meshtastic.log.line(line)` - a raw unparsed log line from the radio
- `meshtastic.clientNotification(notification, interface) - a ClientNotification sent from the radio

We receive position, user, or data packets from the mesh.  You probably only care about `meshtastic.receive.data`.  The first argument for
that publish will be the packet.  Text or binary data packets (from `sendData` or `sendText`) will both arrive this way.  If you print packet
you'll see the fields in the dictionary.  `decoded.data.payload` will contain the raw bytes that were sent.  If the packet was sent with
`sendText`, `decoded.data.text` will **also** be populated with the decoded string.  For ASCII these two strings will be the same, but for
unicode scripts they can be different.

# Example Usage
```
import meshtastic
import meshtastic.serial_interface
from pubsub import pub

def onReceive(packet, interface): # called when a packet arrives
    print(f"Received: {packet}")

def onConnection(interface, topic=pub.AUTO_TOPIC): # called when we (re)connect to the radio
    # defaults to broadcast, specify a destination ID if you wish
    interface.sendText("hello mesh")

pub.subscribe(onReceive, "meshtastic.receive")
pub.subscribe(onConnection, "meshtastic.connection.established")
# By default will try to find a meshtastic device, otherwise provide a device path like /dev/ttyUSB0
interface = meshtastic.serial_interface.SerialInterface()

```
"""

# ruff: noqa: F401

import logging
from importlib import import_module as _import_module
from typing import Any as _Any
from typing import Callable as _Callable
from typing import cast as _cast

from google.protobuf.json_format import MessageToJson

from meshtastic.node import Node
from meshtastic.util import (
    DeferredExecution,
    Timeout,
    catchAndIgnore,
    fixme,
    stripnl,
)

from . import _protocol_runtime, _publishing, util
from ._core_constants import BROADCAST_ADDR as _BROADCAST_ADDR
from ._core_constants import BROADCAST_NUM as _BROADCAST_NUM
from ._core_constants import DECODE_ERROR_KEY as _DECODE_ERROR_KEY
from ._core_constants import LOCAL_ADDR as _LOCAL_ADDR
from ._core_constants import NODELESS_WANT_CONFIG_ID as _NODELESS_WANT_CONFIG_ID
from ._core_constants import OUR_APP_VERSION as _OUR_APP_VERSION
from ._response_types import ResponseCallback as _ResponseCallback
from ._response_types import ResponseHandler as _ResponseHandler
from .protobuf import (
    admin_pb2,
    apponly_pb2,
    channel_pb2,
    config_pb2,
    mesh_pb2,
    mqtt_pb2,
    paxcount_pb2,
    portnums_pb2,
    powermon_pb2,
    remote_hardware_pb2,
    storeforward_pb2,
    telemetry_pb2,
)

pub = _cast(_Any, _import_module("pubsub.pub"))

# Keep this module aligned with historical master behavior by intentionally not
# defining __all__. Public names remain available as module attributes.


def __getattr__(name: str) -> _Any:
    """Provide lazy access to legacy module attributes.

    When the attribute "serial" is requested, import the third-party pyserial
    module, cache it on the module globals as "serial", and return it. For any
    other attribute, raise AttributeError.

    Parameters
    ----------
    name : str
        Attribute name being accessed.

    Returns
    -------
    _Any
        The resolved module object for the requested legacy attribute
        (e.g., the third-party pyserial module for "serial").

    Raises
    ------
    AttributeError
        If the requested attribute is not provided by this lazy loader.
    """
    # COMPAT_STABLE_SHIM: preserve historical `meshtastic.serial` module access.
    if name == "serial":
        # Keep historical `meshtastic.serial` access to the third-party
        # pyserial module as exposed on master.
        serial_module = _import_module("serial")
        # Cache in module namespace so subsequent accesses bypass __getattr__
        globals()["serial"] = serial_module
        return serial_module
    raise AttributeError(  # noqa: TRY003
        f"module {__name__!r} has no attribute {name!r}"
    )


# Note: To follow PEP224, comments should be after the module variable.

LOCAL_ADDR = _LOCAL_ADDR
"""A special ID that means the local node"""

BROADCAST_NUM: int = _BROADCAST_NUM
"""if using 8 bit nodenums this will be shortened on the target"""

BROADCAST_ADDR = _BROADCAST_ADDR
"""A special ID that means broadcast"""

OUR_APP_VERSION: int = _OUR_APP_VERSION
"""The numeric buildnumber (shared with android apps) specifying the
   level of device code we are guaranteed to understand

   format is Mmmss (where M is 1+the numeric major number. i.e. 20120 means 1.1.20
"""

NODELESS_WANT_CONFIG_ID = _NODELESS_WANT_CONFIG_ID
"""A special thing to pass for want_config_id that instructs nodes to skip sending nodeinfos other than its own."""

# COMPAT_STABLE_SHIM: preserve the historical package-root publishingThread
# singleton while internal publishers depend on its private owner directly.
publishingThread = _publishing.publishing_thread
"""Process-wide deferred publisher worker.

`DeferredExecution.queueWork()` is thread-safe (backed by Queue) and all
callbacks are serialized on the worker thread.
"""

logger = logging.getLogger(__name__)

# COMPAT_STABLE_SHIM: preserve historical package-root runtime names after moving
# their implementations to internal owner modules. These aliases must remain
# available without deprecation warnings.
REDACTED_TEXT = _protocol_runtime.REDACTED_TEXT
REDACTED_BYTES = _protocol_runtime.REDACTED_BYTES
DECODE_ERROR_KEY = _DECODE_ERROR_KEY

ResponseCallback = _ResponseCallback
ResponseHandler = _ResponseHandler
ProtobufFactory = _protocol_runtime.ProtobufFactory
OnReceive = _protocol_runtime.OnReceive
KnownProtocol = _protocol_runtime.KnownProtocol

_packet_debug_summary = _protocol_runtime._packet_debug_summary
_sanitize_last_received = _protocol_runtime._sanitize_last_received
_on_text_receive = _protocol_runtime._on_text_receive
_on_position_receive = _protocol_runtime._on_position_receive
_on_node_info_receive = _protocol_runtime._on_node_info_receive
_on_node_status_receive = _protocol_runtime._on_node_status_receive
_on_telemetry_receive = _protocol_runtime._on_telemetry_receive
_receive_info_update = _protocol_runtime._receive_info_update
_on_admin_receive = _protocol_runtime._on_admin_receive

# Well known message payloads can register decoders for automatic protobuf parsing.
protocols = _protocol_runtime.protocols
