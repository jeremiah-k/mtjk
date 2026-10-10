# Getting started

`mtjk` distributes the `meshtastic` Python package under a separate distribution name. It is maintained as a fork for the work described in the [README](../../README.md), and should not be installed alongside upstream `meshtastic` in the same Python environment: both supply the same import namespace.

## CLI

For an isolated command-line installation:

```bash
pipx install mtjk
mtjk --version
```

The historical `meshtastic` console command remains available for compatibility; new examples use `mtjk`. For development against the unreleased branch, replace the installed package in that **isolated** environment with `pipx install 'git+https://github.com/jeremiah-k/mtjk.git@develop'`. A branch reference moves; production deployments should pin a released version or immutable commit.

## Python projects

Declare `mtjk` in your dependency manager and import `meshtastic`:

```python
import meshtastic.serial_interface

with meshtastic.serial_interface.SerialInterface() as interface:
    interface.sendText("hello mesh")
```

A tool installation with `pipx` or `uv tool` does not provide the package to a separate Python application's environment. An application with an existing Meshtastic installation should deliberately replace it rather than attempting to load both distributions.

## Choosing an API

- Use [`queryNodes`](node-queries.md) for detached **cached** node observations.
- Use [fresh configuration reads](configuration-reads.md) when a response from the device matters.
- Use [embedded commands](embedded-commands.md) when an application needs supported finite CLI actions over its own connection; the caller supplies authorization and destination policy.
- Use the standalone CLI for transport selection, interactive and long-running operations, firmware updates, and file-based configuration workflows.

## Limitations

A successful send or local completion is not necessarily proof that a remote device applied a change. The firmware, radio path, request correlation, and explicit response or ACK semantics determine what was confirmed. Consult [CLI configuration verification](cli-configuration.md), [admin response contracts](../internals/admin-response-contracts.md), and [compatibility](../compatibility.md) before relying on an operation for unattended administration.
