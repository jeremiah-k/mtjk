# Fresh configuration reads

Use the synchronous read methods when an application needs a correlated device
response rather than the cached localConfig or moduleConfig objects:

```python
from meshtastic.errors import RequestRejectedError, RequestTimeoutError

node = interface.getNode("!12345678", requestChannels=False)
try:
    config = node.readConfig("lora", timeout=10)
    mqtt = node.readModuleConfig("mqtt", timeout=10)
    hop_limit = node.readPreference("lora.hopLimit", timeout=10)
except RequestRejectedError as error:
    print(error.reason, error.nodeNum, error.requestId)
except RequestTimeoutError:
    print("Device did not respond within the operation budget")
```

Each call sends a fresh request. Config and ModuleConfig results contain the
requested section and belong to the caller; reads do not replace the node's
cached settings. readPreference accepts protobuf snake_case and JSON camelCase
paths. It returns raw protobuf scalars, numeric enums, copied messages, lists,
or maps. A section-only path returns its message. Repeated fields and maps
cannot be traversed by index or key; absent scalar fields return protobuf
defaults. Invalid paths fail before transmission.

A finite positive timeout covers library connection, TX queue, and response
waits through one monotonic budget. Transport I/O retains its transport's own
timeout and cannot be interrupted by this budget. Concurrent reads correlate
by packet ID, source node, response variant, and section. Routing ACKs do not
complete reads. Timeout, rejection, and decode failures retire only their own
response state; errors expose nodeNum, requestId when available, and operation.
All request failures derive from RequestError, which is also a
MeshInterface.MeshInterfaceError. RequestTimeoutError is also a TimeoutError.
Use adminIndex=0 to force channel zero, or omit it to select the configured
admin channel.

## Choosing the read path

These APIs request fresh responses; a cached protobuf section is not evidence that a particular request completed. Use `queryNodes` for cached observations instead. Embedded callers who want CLI-style text and validation can use `executeCommand`, but its `CommandResult.output` is not a typed configuration document.

A timeout applies to waits managed by the library, not to every possible blocking operation in a transport backend. A timed-out write or request can still have reached a device; do not assume rollback.
