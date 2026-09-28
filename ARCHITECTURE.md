# mtjk Architecture

This document describes the current architecture and maintenance boundaries of
`mtjk`. It replaces the older refactor-program documents, which described work
while it was in progress rather than the structure that exists now.

## Design principles

The codebase is organized around a few recurring rules:

1. **Keep public compatibility at the edge.** Public `Node`, `MeshInterface`,
   transport, CLI, and historical compatibility entry points remain stable while
   implementation details are allowed to move behind them.
2. **Give state and resources an owner.** Locks, background workers, pending
   requests, transport handles, and mutable session state should have one clear
   lifecycle owner.
3. **Separate decisions from effects.** Connection, retry, configuration, and
   request flows should make policy decisions independently from the I/O that
   executes them where practical.
4. **Prefer explicit failure semantics.** Library code raises typed exceptions
   instead of terminating the process; CLI code translates those failures into
   user-facing messages and exit codes.
5. **Preserve behavior with tests, not file layout.** Internal modules may be
   reorganized without making their paths public. Compatibility tests protect the
   API and behavioral seams downstream code actually uses.

## Public package surface

The distribution name is `mtjk`, but the Python package remains `meshtastic`.
The main public facades are:

- `meshtastic.MeshInterface` / `meshtastic.mesh_interface.MeshInterface`;
- `meshtastic.Node` / `meshtastic.node.Node`;
- transport interfaces such as Serial, TCP, and BLE;
- CLI entry points `mtjk` and compatibility command `meshtastic`;
- the established top-level constants, protobuf exports, utilities, and pubsub
  behavior guarded by the API baseline.

Public facades intentionally remain relatively broad because shrinking them by
removing historical methods would create downstream churn. Large public files
are therefore not automatically considered architecture problems when their
methods are thin delegates into narrower runtimes.

## Runtime decomposition

### MeshInterface

`MeshInterface` is the common transport-facing facade. Internal
`mesh_interface_runtime` modules own narrower responsibilities such as:

- inbound packet dispatch;
- request/response and ACK/NAK wait state;
- send queue and packet bookkeeping;
- node-database access and views;
- transport-facing collaborator ports.

The runtime package is internal unless a specific compatibility export is listed
in `COMPATIBILITY.md` and `meshtastic/_runtime_compatibility.json`.

### Node

`Node` remains the public API for configuration and administration of local and
remote nodes. Internal `node_runtime` modules own areas such as:

- channel state and channel writes;
- configuration requests and writes;
- URL/configuration transactions;
- content/admin operations;
- contact and settings helpers.

The public facade preserves historical mutate-then-write workflows, method
names, positional arguments, and documented aliases even when the implementation
behind them has changed substantially.

### CLI

`meshtastic.__main__` remains the compatibility entry point, while internal CLI
modules own parser construction, bootstrap/session resources, connected actions,
preference conversion, configuration planning, and rendering.

The CLI and library intentionally have different failure responsibilities:
internal library operations raise exceptions; the CLI decides how those failures
should be presented and which process exit code should be used.

## BLE subsystem

BLE has the most explicit internal decomposition because connection lifecycle,
platform backends, notifications, reconnects, and shutdown all share concurrent
state.

The current design uses dedicated components for:

- client and backend adaptation;
- discovery and target resolution;
- connection establishment and retry policy;
- lifecycle/session state;
- address/connection ownership gates;
- notification registration and receive recovery;
- management operations such as pairing/trust;
- compatibility event publication.

`interfaces/ble/interface.py` itself is deliberately allowed to remain large.
Its remaining content is mostly compatibility wiring and collaborator
orchestration, and further mechanical decomposition there has historically been
riskier than the size it removes. Treat it as a stable orchestration facade:
make targeted bug fixes rather than new extraction passes.

The public `meshtastic.ble_interface` module remains a compatibility facade.
Historical `BLEInterface.BLEError` catching behavior and its `kind` metadata are
preserved while newer typed BLE exceptions provide more specific context.

See [BLE.md](BLE.md) for detailed BLE contracts, locking rules, and integration
examples.

## Concurrency and lifecycle ownership

Long-running integrations are a primary use case, so concurrency work generally
follows these rules:

- mutable subsystem state is protected by an explicitly owned lock;
- lock scope is kept separate from blocking I/O and user callbacks where
  possible;
- pending request handlers have bounded lifetimes;
- request IDs and ACK/NAK state are scoped to the request that owns them;
- background workers define start, stop, drain, and cancellation behavior;
- transport replacement must not allow stale work from a previous connection to
  act on the new one;
- cleanup paths are expected to be idempotent and safe during partial startup or
  partial failure.

These rules are validated heavily in behavioral and concurrency tests because
many failures only appear during reconnect, shutdown, or overlapping requests.

## Compatibility boundary

Compatibility is a design constraint rather than an attempt to freeze every
internal detail.

The repository maintains:

- a checked-in public API baseline;
- compatibility-focused behavioral tests;
- a machine-readable runtime compatibility manifest for the small number of
  internal paths intentionally kept stable;
- documented naming aliases and deprecation behavior;
- historical BLE compatibility wrappers and monkeypatch seams where existing
  users/tests rely on them.

Internal runtime modules, underscore-prefixed helpers, and collaborator classes
are free to change unless they are explicitly promoted to a compatibility
contract.

See [COMPATIBILITY.md](COMPATIBILITY.md) for the detailed policy.

## Error handling

A deliberate difference from older Meshtastic Python behavior is that reusable
library code should not call `sys.exit()` when an operation fails. Public library
methods raise exceptions that callers can catch, retry, translate, or allow to
propagate.

`MeshInterface.MeshInterfaceError` remains the common interface-level error
surface. BLE failures remain catchable through `BLEInterface.BLEError`, with
structured subclasses used where more detail is useful.

The command-line application may still terminate the process after translating a
library failure into a message and exit code; that is appropriate at the CLI
boundary and is separate from library behavior.

## Firmware and protobuf boundaries

Generated protobuf code is treated as generated source and is updated through
the repository's protobuf regeneration workflow. Firmware field widths and
nanopb limits that are narrower than the Python protobuf representation are
validated at the Python API/CLI boundary when known.

Protocol additions should normally include:

- decode/handler registration;
- retention or response behavior where appropriate;
- API/CLI exposure only when there is a clear consumer-facing use case;
- tests for malformed input and compatibility behavior.

## Quality gates

The project uses tests and static tooling as architecture guardrails rather than
as cleanup-only tools. The maintained checks include:

- pytest with coverage;
- Pylint for production/examples;
- Ruff for maintained Python/test surfaces;
- Mypy for type checking;
- API baseline and import-compatibility checks;
- simulator and hardware smoke lanes for transport/firmware behavior.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the commands that mirror CI.

## Upstream relationship

`mtjk` develops independently of `meshtastic/python`. The histories have
diverged far enough that a merge would be ceremonial rather than a real
integration, so upstream changes are ported — cherry-picked and adapted — when
they are wanted, and an upstream merge is done only occasionally, after the
fixes of interest have already been ported. Porting a fix does not require a
merge.

Two automations watch upstream without coupling this fork's history to it:

- the protobufs digest automation tracks the current `meshtastic/protobufs`
  revision and regenerates the bindings;
- the nightly API-baseline workflow snapshots the upstream master surface and
  opens a PR when it drifts, so upstream API changes stay visible.

This fork does not open pull requests against upstream.

## Project evolution

The architecture was not designed as one rewrite. It grew from a series of
connection-reliability and maintenance fixes that exposed broader ownership and
coupling problems. The larger early modernization work established the typing,
linting, testing, and BLE foundations; later changes progressively moved state
and behavior behind narrower boundaries while retaining public compatibility
facades.

Git history is the authoritative record for that evolution. This document is
intentionally about the architecture that should be maintained now, not a
chronological refactor log.
