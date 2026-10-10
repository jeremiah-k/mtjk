# Embedded command execution

Applications can execute supported CLI actions on a connection they already own:

```python
from meshtastic.commands import executeCommand, getCommandCapabilities
from meshtastic.errors import RequestRejectedError

capabilities = getCommandCapabilities()
if "--get" in capabilities.supportedOptions:
    result = executeCommand(
        interface,
        ["--dest", "!12345678", "--get", "lora.region"],
        timeout=15,
    )
    if result.succeeded:
        print(result.output)
    elif isinstance(result.error, RequestRejectedError):
        print(result.error.reason)
    else:
        print(result.exitCode, result.error)
```

Pass a token sequence without a program name. Values are parsed by the CLI's
argument definitions; shell expansion and shell execution are never performed.
getCommandCapabilities returns API contract version 1 and the accepted option
spellings, including aliases. `executeCommand(interface, ["--help"])` returns
help for that same surface. Additive options preserve the API version.

## Output and errors

CommandResult contains exitCode, output, error, truncated, and the succeeded
property. Status zero means action execution completed. Status 2 indicates
argument validation failure; status 1 indicates execution failure. The error
retains the original exception, including typed request errors where available.
Errors raised by CLI termination are returned without exiting the process.
KeyboardInterrupt and other control flow exceptions propagate after cleanup.

Output capture uses invocation-local reporters and reader-thread callback
contexts. It does not replace stdout or stderr, change logging configuration,
or populate legacy CLI globals. Requested node, preference, telemetry, position,
and traceroute results are captured, including under --quiet. Preference reads
request fresh device sections and preserve the existing CLI's secret redaction.

The default output limit is 64 KiB of UTF-8 text. Set maxOutputBytes to another
positive integer to change it. Text is truncated at a character boundary and
truncated reports omitted text. Execution continues when output is truncated.
An optional output callback receives the retained chunks, including newlines;
joining those chunks produces result.output. The callback runs synchronously
on the thread producing output, which can be a reader thread, and should return
promptly. Callback failures are retained in result.error. Invalid programmatic
timeout or output limits raise ValueError before execution.

## Connection and request ownership

executeCommand uses the provided interface and never opens or closes a
transport. Device actions such as reboot retain their device-side meaning;
the application remains responsible for connection recovery. Mutations sent
before a timeout or later failure are not rolled back.

Commands on one interface serialize. Different interfaces can execute
concurrently. Nested executeCommand calls are refused to avoid reentrant
command lifetimes. The timeout budget begins before parsing and includes
waiting for another command, connection readiness, TX queue capacity, node
polling, response reads, and request-scoped acknowledgment waits. Nested waits
cannot renew this monotonic budget. Blocking transport I/O and application
callback code retain their own execution limits.

Only command-owned response handlers and waits are retired on completion,
failure, timeout, or interruption. Unrelated response registrations and pending
legacy wait outcomes are preserved. Embedded ACK waits use request IDs and
source checks rather than shared acknowledgment flags. This does not make
concurrent uses of historical unscoped waitForAckNak calls safe; applications
sharing a connection should use request-scoped operations.

## Supported actions

The maintained surface includes preference reads and writes, settings
transactions, channel operations and QR output, node and interface information,
text messages, telemetry and position requests, traceroute, reboot and shutdown,
node database changes, preference backups, device file deletion, input events,
and connection-status/UI reads. Consult capabilities for exact option spellings.
Configuration writes retain CLI validation, batching, and device verification.

Transport selection, listening, power monitoring, tunnels, interactive flows,
firmware upload, and file-based configure/import/export workflows belong to the
standalone CLI. Unsupported options fail before actions run. The embedding
application owns authorization, destination restrictions, and any narrower
command policy it requires.

Use queryNodes for structured cached node data and readConfig,
readModuleConfig, or readPreference for typed device configuration values.
CommandResult.output remains human-readable CLI text; the dedicated library
APIs avoid parsing it.
