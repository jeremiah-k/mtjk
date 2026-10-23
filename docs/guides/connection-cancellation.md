# Cancelling interface construction

SerialInterface, TCPInterface, BLEInterface and the shared interface constructors
accept an optional keyword-only `cancelEvent`, a caller-owned `threading.Event`.
This lets an embedding application interrupt setup before it has an interface
object to close. Existing calls without the event retain their timeout behavior.

```python
import threading
from concurrent.futures import ThreadPoolExecutor

from meshtastic.ble_interface import BLEInterface
from meshtastic.cancellation import ConnectionCancelledError

cancel = threading.Event()
with ThreadPoolExecutor(max_workers=1) as executor:
    pending = executor.submit(
        BLEInterface,
        address="AA:BB:CC:DD:EE:FF",
        timeout=60,
        cancelEvent=cancel,
    )
    # Another application task can request cancellation when shutting down.
    cancel.set()
    try:
        interface = pending.result()
    except ConnectionCancelledError:
        pass
    else:
        # Setup may have finished before the request. Its result is then owned
        # by the caller and must be closed normally.
        interface.close()
```

A pre-set event rejects construction before transport setup. During construction,
the event interrupts BLE discovery and coroutine waits, TCP connection attempts,
protocol/configuration waits, serial settling and retry delays, and contended
connection reservations. Partial resources use the interface's normal rollback
path. Native cleanup remains usable after cancellation. ConnectionCancelledError
is a MeshInterface.MeshInterfaceError subclass, so callers can distinguish
operator cancellation without treating it as a transport outage or retrying it.

Cancellation is scoped to one synchronous constructor invocation, including
nested SDK setup. Its caller may set the shared `threading.Event` from
another thread; only the constructing invocation observes it. The event is
not retained by the connected interface. Setting it after construction does
not disconnect an established session or cancel unrelated operations. To close an established interface, call
close(). Keep the event set until the constructor has finished; do not clear it
while setup is in progress.

For asyncio hosts, cancelling the coroutine awaiting asyncio.to_thread() does not
cancel its native thread. Set cancelEvent as well, and retain ownership of the
construction result until it either raises ConnectionCancelledError or returns
an interface that the host must close. The SDK does not require a cancellation
watcher thread and does not terminate Python threads forcibly.

## Boundaries

Cancellation is cooperative. Native coroutine waits and socket readiness are
checked in short intervals, and cleanup retains the SDK's existing shutdown
budgets. Operating-system DNS resolution and serial-driver open calls cannot be
preempted by this event; cancellation is checked after they return and partial
handles are closed. A native backend that ignores cancellation or cleanup can
still delay shutdown. Use process supervision when a hard termination boundary
is required.

Cancellation does not imply that a message was delivered or failed. This API
controls connection construction; delivery and acknowledgement contracts remain
separate.

Protocol-startup rollback delegates to the concrete transport's close path.
For TCP, this releases the socket before the final reader join, rather than
waiting for a reader that cannot wake until its transport closes. Construction
cancellation is masked during this teardown so partial serial cleanup can finish.

BLE construction cancellation waits for the native coroutine's cancellation
cleanup before closing its transport. Cancelling its concurrent future alone is
not proof that cleanup has finished: BlueZ may still be awaiting a Disconnect
reply on the same D-Bus connection. This retirement wait uses the existing
five-second disconnect budget. If the backend exceeds that budget, a warning
records the incomplete cleanup and ordinary best-effort teardown proceeds;
cancellation cannot force an uncooperative backend to release its resources.
