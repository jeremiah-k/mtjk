# Getting started

`mtjk` distributes the `meshtastic` Python package under a separate distribution name. It is maintained as a fork for the work described in the [README](../../README.md), and should not be installed alongside upstream `meshtastic` in the same Python environment: both supply the same import namespace.

## CLI

For an isolated command-line installation:

```bash
pipx install mtjk
mtjk --version
```

The historical `meshtastic` console command remains available for compatibility; new examples use `mtjk`. For development against the unreleased branch, replace any installed release in that **isolated** environment with a clean rebuild:

```bash
pipx uninstall mtjk  # skip if mtjk is not installed yet
pipx install 'git+https://github.com/jeremiah-k/mtjk.git@develop'
```

Repeating both commands moves to a newer branch tip. Reinstalling from scratch means nothing lingers from earlier builds. A branch reference moves; production deployments should pin a released version or immutable commit.

## Python projects

Declare `mtjk` in your dependency manager and import `meshtastic`:

```python
import meshtastic.serial_interface

with meshtastic.serial_interface.SerialInterface() as interface:
    interface.sendText("hello mesh")
```

At interpreter exit, the library attempts to close connected serial and TCP interfaces, closing each on its own thread and waiting up to three seconds in total, so one slow device cannot prevent the others from being closed. Cleanup may not complete, so callers should close interfaces explicitly for a deterministic disconnect. A serial port opened with `connectNow=False` is not tracked until `connect()` succeeds and must be closed by the caller.

A tool installation with `pipx` or `uv tool` does not provide the package to a separate Python application's environment. An application with an existing Meshtastic installation should deliberately replace it rather than attempting to load both distributions.

## Signals and shutdown

The CLI entrypoint installs handlers for `SIGTERM` and `SIGHUP`: the first delivery raises `SystemExit(128 + signum)` so the normal session teardown runs (interface close and the regular `finally` paths), after which the recorded signal is re-delivered and the process dies by that signal (supervisors report exit status 143 for `SIGTERM`, 129 for `SIGHUP`). A second signal during teardown terminates the process immediately instead of waiting on cleanup. Ctrl+C prints `Interrupted.` and exits with status 130; the process-owning console entrypoints (`mtjk`, `meshtastic`, `mesh-tunnel`, and `python -m meshtastic`) then re-deliver `SIGINT` so the process ends the same way as for an unhandled interrupt.

Embedder guarantees: importing the library installs no signal handlers, so applications keep full control of their signal policy; dispositions inherited as `SIG_IGN` (for example from `nohup` or a supervisor) and non-Python handlers are preserved. A forked child does not close the parent's tracked interfaces; an `os.register_at_fork` child hook resets the tracked-interface registry in `stream_interface`.

## Choosing an API

- Use [`queryNodes`](node-queries.md) for detached **cached** node observations.
- Use [fresh configuration reads](configuration-reads.md) when a response from the device matters.
- Use [embedded commands](embedded-commands.md) when an application needs supported finite CLI actions over its own connection; the caller supplies authorization and destination policy.
- Use the standalone CLI for transport selection, interactive and long-running operations, firmware updates, and file-based configuration workflows.

## Limitations

A successful send or local completion is not necessarily proof that a remote device applied a change. The firmware, radio path, request correlation, and explicit response or ACK semantics determine what was confirmed. Consult [CLI configuration verification](cli-configuration.md), [admin response contracts](../internals/admin-response-contracts.md), and [compatibility](../compatibility.md) before relying on an operation for unattended administration.
