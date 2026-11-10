# Runtime state-ownership audit

This is a dated audit record (2026-11-06) of the long-lived runtime owners in
`mtjk` and of the ownership fixes it produced. It covers lifecycle and
concurrency owners across the BLE runner, thread coordinator, BLE reconnect
worker, TCP reconnect, and mesh TX queue.

Status: **recorded (2026-11-06)** — sections A–E describe the changes in
[#756](https://github.com/jeremiah-k/mtjk/pull/756) (BLE runner and thread
coordinator), [#757](https://github.com/jeremiah-k/mtjk/pull/757) (BLE
reconnect worker), [#758](https://github.com/jeremiah-k/mtjk/pull/758) (TCP
reconnect), and [#759](https://github.com/jeremiah-k/mtjk/pull/759) (TX
queue), including their review follow-ups. Public names, wire contracts, and
compatibility shims are unchanged by those fixes.

## Scope and decision rule

This is an audit of **long-lived runtime owners**, not an automatic conversion
of every conditional or status boolean to an `Enum`. An implicit state machine
has externally meaningful transitions such as create/start/stop, claim/release,
send/ACK/timeout, connect/reconnect/close, or stage/commit/rollback. The risky
cases are those where (1) work continues after its owner is forgotten, (2) a
stale completion mutates a successor's state, (3) a partially completed action
is published as successful, or (4) a collection of pending states grows without
any retirement rule.

The canonical BLE `ConnectionState` is **already explicit** in
`interfaces/ble/state.py`; do not create a competing top-level BLE enum.
Likewise, request/response wait state, management command counts and session
projections already have owners. Changes here are scoped to concrete defects,
not speculative architectural reworks that could break old BLE compatibility
entrypoints or mesh packet protocol behavior.

Disposition legend: **FIXED** = changed with a focused regression;
**RETAINED** = existing owner/lock/generation policy is preserved;
**NEEDS INTEGRATION** = a meaningful timing/error path that requires hardware
or integration-level fault injection to establish.

## A. Corrected machine: shared BLE coroutine runner

**Source:** `meshtastic/interfaces/ble/runner.py` (`BLECoroutineRunner`).

| Logical phase | Legal progress        | Rule                                                                                                    |
| ------------- | --------------------- | ------------------------------------------------------------------------------------------------------- |
| Not started   | Starting              | Publish thread and fresh readiness event under instance lock                                            |
| Starting      | Running, failed start | Starter must own thread until readiness/failure; a failed native `Thread.start` rolls back              |
| Running       | Stopping              | Stop request prevents new submissions and cancels every admitted future                                 |
| Stopping      | Stopped, timed-out    | Thread remains owned until it **actually** exits, regardless of whether its event loop is still running |
| Stopped       | Starting              | `restart()` may create a new owner only after the previous thread terminates                            |

**Defects:** a stopped loop could coexist with a live teardown thread, yet
`restart()` previously cleared the thread reference and created another loop;
and concurrent `run_coroutine_threadsafe()` and `stop()` could allow a future
to be posted _after_ stop's cancellation snapshot but _before_ being added to
`_pending_futures`. This could leave work on a closing loop. Thread startup
failure also left an inert owner installed.

**Fix:** `restart()` joins an old _stopping_ thread outside the reentrant lock
with a bounded timeout; if still alive it raises the existing restart error
rather than overlapping owners. A thread that is still starting (no published
loop, no stop request) instead reuses that startup's own readiness event. A
restart from the owner thread itself is rejected to prevent self-join or
self-wait. A second locked check handles competing restarters. Future
submission, tracking, and stop admission now share one atomic handoff under
the instance lock. A native start failure rolls back the empty owner. The
legacy deprecated `timeout=` argument and its warning policy remain intact.

## B. Corrected machine: BLE worker and event coordinator

**Source:** `meshtastic/interfaces/ble/coordination.py` (`ThreadCoordinator`).

- **Created**: registered, `ident is None`; can start unless cleanup started.
- **Start handoff**: registered in `_pending_start` **and** `_starting_now`.
  `thread.start()` runs outside lock, but cleanup retains this handoff.
- **Running**: may be joined by shutdown; a timeout does **not** unown it.
- **Cleanup**: mark closed; wake registered events; join known running threads
  outside lock; keep alive and still-starting owners for a later cleanup.
- **Retired**: owner has exited or native `start()` failed before a thread ID
  was assigned; do not retain an unstartable zombie registration.

**Defects:** cleanup dropped all references _before_ joining workers; a
slow worker could remain alive after the join timeout with no tracking. New
thread admission also needed an explicit post-cleanup guard. A start racing
cleanup could be untracked. The separate `_starting_now` set distinguishes
actual native-start handoffs from historical `_pending_start` reservations.
The coordinator remains one-shot: cleanup does **not** reopen it.

## C. Corrected machine: BLE automatic reconnect worker

**Source:** `meshtastic/interfaces/ble/reconnection.py` (`ReconnectScheduler`).

**Idle → Scheduled → Active → Exit → Idle.** Each schedule owns an exact worker
identity. Late `on_exit` notifications, repeated completion, or start-failure
cleanup may only clear that matching worker. Previously they unconditionally
cleared `_reconnect_thread`, so a stale callback could erase the successor's
admission guard, permitting overlapping reconnect attempts. The optional
`owner=` parameter is internal and the no-argument helper still works for
existing test/compatibility seams. No independent auto-reconnect was enabled.

## D. Corrected machine: TCP connect/reconnect and socket adoption

**Source:** `meshtastic/tcp_interface.py` (`TCPInterface`).

| Logical phase          | Guard                                                                                                                                                                                                                                                |
| ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Disconnected           | No current socket, no fatal/exit signal                                                                                                                                                                                                              |
| Reconnect claimed      | Attempt lock + `_reconnect_attempt_in_progress` serialize work                                                                                                                                                                                       |
| Connected, configuring | Candidate socket adopted, `_start_config()` runs outside reconnect lock                                                                                                                                                                              |
| Connected, committed   | Re-check not closing, not fatal, **same socket identity** after configuration before declaring success/resetting retry count                                                                                                                         |
| Closing/fatal          | Callers waiting on an in-flight attempt and callers about to open a new socket reject immediately; a caller that already owns an active socket keeps the historical silent return (`StreamInterface.connect()` coordinates that path with `close()`) |

**Defects:** reconnect could return `True` after `close()`/another owner
invalidated its socket during `_start_config()`. Public `connect()` could
wait on an in-progress attempt without checking a simultaneous shutdown.

**Fix:** the configuration-success path snapshots the installed socket before
`_start_config()` and, under the reconnect lock, resets the retry counter and
reports success only when exit/fatal flags are clear **and** the installed
socket is still the snapshot. The success path no longer assigns
`_fatal_disconnect = False`: the guard runs only when the flag is already
`False`, and the flag is sticky by design — `_on_fatal_disconnect()` sets it
once, and only the recovery paths in `myConnect` and `_read_bytes` clear it.
The shutdown/fatal guard in `_connect_socket_if_needed()` covers callers
waiting on an in-flight attempt and callers about to open a new socket; a
caller that already owns an active socket keeps the historical silent return.
The fix preserves the existing bounded backoff and socket-close ownership
rather than introducing new locks around potentially blocking config I/O.

## E. Corrected machine: mesh firmware TX queue / ACK correlation

**Source:** `meshtastic/mesh_interface_runtime/queue_send.py`
(`_QueueSendRuntime`; shared `_queue_lock` supplied by `MeshInterface`).

**Queued → Claimed/dequeued → Sending → Awaiting ACK / ACK received / Requeued
on error → Retired.** For nonzero IDs, packet ID is an ownership key, not
merely a dictionary key. `_active_send_ids` with reference-counted claims
(`_active_send_id_claims`) keep each nonzero ID reserved from caller enqueue
through dequeue, native `send_impl()` and reconciliation, **including backlog
packets popped by another sender's drain loop**. Both send loops pop and
claim under a single `_lock` acquisition (`_pop_for_send_locked()` for the
waiting loop, `_pop_capacity_exempt_locked()` for the capacity-exempt loop;
`_pop_for_send()` remains a locked wrapper), so dequeue and ID claim are
atomic without nested lock entry. Callers attempting duplicate live nonzero
IDs are rejected with `ValueError` instead of silently destroying
ACK/capacity bookkeeping. Releasing an ID without a matching claim is a
tolerated no-op, so a release inside a `finally` block cannot mask the
exception being unwound. Packet ID 0 is firmware-assigned and is not a unique
ACK/ownership key: multiple queued zero-ID packets remain separate using
private negative queue keys and are sent in order, and a `QueueStatus` reply
whose `mesh_packet_id` is 0 is never correlated, so no reply can remove a
queued zero-ID packet. When dropping an aborted queued packet, remove only
the **identical** entry; if another caller replaced it, refresh the
replacement's capacity classification so a local packet can progress with
zero RF slots.

**Unexpected ACK submachine:**
`Unknown ACK → Provisional False marker → Correlated by sending thread` OR
`Expired/evicted`. Unexpected markers are necessary to detect an ACK arriving
under a send, but prior code inserted them in a second lock acquisition and
never bounded the cache if unsolicited ACKs continued. An ACK and marker
insertion are now one atomic lock-protected operation. Unowned tombstones
are limited to **1,024** packet IDs with **300-second** TTL; a marker for an
actively sending ID is pinned until reconciliation, even if it crosses the
ordinary count/age limit, to prevent duplicate retransmission after an early
ACK and an ambiguous transport exception. Pruning never deletes a live
`ToRadio` packet replaced under the same ID. These are _housekeeping_ bounds,
not claims that a packet is delivered or acknowledged exactly once.

**Important semantic boundary:** these fixes only prevent _concurrent live_
packet-ID reuse. A previously completed but still potentially radio-in-flight
packet can be reused after its owner retires. Changing that requires explicit
packet-ID allocation, wraparound, ACK, and replay-window guarantees and must
not be slipped into a backward-compatible state-machine fix. Likewise,
transport errors may leave radio delivery ambiguous; do not blindly retry all
in-flight packets.

## Full runtime state-owner inventory

The entries below are distinct resources/transitions, not lists of every
function. A `RETAINED` entry means **no defect has been proven by this audit**,
not that every hardware-dependent race has passed integration testing.

| #   | State machine / resource                                   | Primary owner / code                                                       | Disposition                                              |
| --- | ---------------------------------------------------------- | -------------------------------------------------------------------------- | -------------------------------------------------------- |
| 1   | Shared BLE background loop startup, stop, restart          | `interfaces/ble/runner.py`                                                 | **FIXED**                                                |
| 2   | BLE submission/future registration/cancellation            | `interfaces/ble/runner.py`                                                 | **FIXED**                                                |
| 3   | BLE thread creation/start/retirement                       | `interfaces/ble/coordination.py`                                           | **FIXED**                                                |
| 4   | BLE named Event creation/wake/cleanup                      | `interfaces/ble/coordination.py`                                           | **FIXED** (owner retained through cleanup)               |
| 5   | BLE reconnect worker admission/completion                  | `interfaces/ble/reconnection.py`                                           | **FIXED**                                                |
| 6   | BLE reconnect retry/backoff budget                         | `interfaces/ble/reconnection.py`, `policies.py`                            | RETAINED                                                 |
| 7   | Canonical BLE connection transition graph                  | `interfaces/ble/state.py` (`ConnectionState`, `BLEStateManager`)           | RETAINED — explicit                                      |
| 8   | BLE session flags/projection/legacy state adaptation       | `interfaces/ble/session_state.py`                                          | RETAINED                                                 |
| 9   | Global BLE address gate: connecting/connected claims       | `interfaces/ble/gating.py`                                                 | RETAINED — claim owner refs, locks, stale pruning        |
| 10  | BLE connect snapshot, client adoption and rollback         | `interfaces/ble/connect_transaction.py`, `connection.py`                   | RETAINED                                                 |
| 11  | BLE client verification and connected publication          | `interfaces/ble/lifecycle_ownership_runtime.py`                            | RETAINED — revalidate lock-free probes under shared lock |
| 12  | BLE disconnect event, duplicate suppression, late callback | `interfaces/ble/lifecycle_disconnect_runtime.py`, `lifecycle_decisions.py` | RETAINED — needs device validation                       |
| 13  | BLE receive thread wanted/actual/recovery state            | `interfaces/ble/lifecycle_receive_runtime.py`, `receive_service.py`        | RETAINED                                                 |
| 14  | BLE notification registration, stop, stale callback        | `interfaces/ble/notification_registration.py`, `notifications.py`          | RETAINED                                                 |
| 15  | BLE scan/discovery/cached client cleanup                   | `interfaces/ble/discovery.py`                                              | RETAINED                                                 |
| 16  | BLE management in-flight work and drain condition          | `interfaces/ble/management_state.py`, `management_runtime.py`              | RETAINED                                                 |
| 17  | BLE management client borrowing vs temporary ownership     | `interfaces/ble/management_compat_service.py`, `management_service.py`     | RETAINED                                                 |
| 18  | BLE close, pending worker drain, shutdown notification     | `interfaces/ble/lifecycle_shutdown_runtime.py`                             | RETAINED; bounded native teardown NEEDS INTEGRATION      |
| 19  | BLE failure classification, suppress/retry/gate recovery   | `interfaces/ble/failure_policy.py`, `lifecycle_controller_runtime.py`      | RETAINED                                                 |
| 20  | BLE client coroutine cancellation/read/write               | `interfaces/ble/client.py`, `cancellation.py`                              | RETAINED                                                 |
| 21  | TCP reconnect attempt ownership and socket publication     | `tcp_interface.py`                                                         | **FIXED**                                                |
| 22  | TCP read/write and close vs reconnect                      | `tcp_interface.py`, `stream_interface.py`                                  | RETAINED; device/network fault NEEDS INTEGRATION         |
| 23  | Serial auto-detection/stream open/retry window             | `serial_interface.py`, `transport_retry.py`                                | RETAINED — retry planner is explicit                     |
| 24  | Stream bootstrap framing/connect/reader thread/close       | `stream_interface.py`                                                      | RETAINED                                                 |
| 25  | Constructor-scoped cooperative cancellation                | `cancellation.py`, serial/TCP/BLE initializers                             | RETAINED                                                 |
| 26  | Base MeshInterface lifecycle, close, heartbeat inflight    | `mesh_interface.py`                                                        | RETAINED — lock and condition ownership documented       |
| 27  | Packet ID allocation and public packet send                | `mesh_interface.py`, `mesh_interface_runtime/send_pipeline.py`             | RETAINED                                                 |
| 28  | Firmware queue slot claim/sending/ACK/timeout              | `mesh_interface_runtime/queue_send.py`                                     | **FIXED**                                                |
| 29  | Awaited response ID reservation and TTL retirement         | `mesh_interface_runtime/request_wait.py`                                   | RETAINED — scoped response handlers and retired IDs      |
| 30  | Inbound protobuf packet dispatch and node DB update        | `mesh_interface_runtime/receive_pipeline.py`, `_protocol_runtime.py`       | RETAINED                                                 |
| 31  | Node/channel cached configuration and partial download     | `node_runtime/channel_state.py`, `node_runtime/response_runtime.py`        | RETAINED — mutation lock/snapshot                        |
| 32  | Administrative request ACK and response scoping            | `node_runtime/admin_wait.py`, `node_runtime/transport_runtime/ack.py`      | RETAINED                                                 |
| 33  | setURL add-only vs replace-all staged writes               | `node_runtime/seturl/execution.py` (`_ReplaceAllStage`)                    | RETAINED — already explicit                              |
| 34  | setURL reconnect/reload/partial cache invalidation         | `node_runtime/seturl/coordinator.py`, `cache.py`                           | RETAINED — transaction owner                             |
| 35  | CLI transport acquisition/failure cleanup                  | `cli/bootstrap.py`, `cli/dispatch.py`, `cli/session_resources.py`          | RETAINED — `ExitStack` owner                             |
| 36  | CLI config document planning/apply/reconnect verification  | `cli/configure_actions.py`, `cli/config_io.py`, `configure_verify.py`      | RETAINED                                                 |
| 37  | OTA/firmware command and administrative response tracking  | `ota.py`, `node_runtime/admin_wait.py`                                     | RETAINED; NEEDS INTEGRATION                              |
| 38  | Tunnel and peripheral helper lifetime                      | `tunnel.py`, `tunnel_device.py`, `powermon/*`                              | RETAINED; not part of common mesh/BLE runtime            |

## Compatibility and lock-order constraints

1. `meshtastic/ble_interface.py` remains a public/legacy re-export boundary;
   `interfaces/ble/*` remains internal. These fixes **do not add exported
   symbols**, public aliases, or new deprecation warnings.
2. Historical `_generatePacketId()` and `_sendPacket()` shims, on which
   MMRelay may depend, remain unchanged. The packet wire format, ACK type and
   public `sendData`/`sendText` contracts are unchanged.
3. BLE session state already contains many booleans mirrored to legacy
   objects. Do not replace these with a rival enum: the authoritative
   `BLEStateManager` owns the canonical state and the session projection owns
   compatibility state. These fixes handle _missing ownership boundaries_.
4. Avoid joining workers or performing network I/O under an outer lifecycle
   RLock. Existing per-address BLE gate, socket ownership lock and queue lock
   contracts stay in force. The TX queue send loops pop and claim under a
   single `_lock` acquisition, and the queue runtime does not nest
   acquisitions of that lock.
5. mtjk's BLE auto-reconnect is opt-in. Its availability must not cause a
   second reconnect orchestrator to compete with MMRelay's own reconnect
   supervisor.
6. No protobuf schema changes, lockfile changes, dependency upgrades, or
   public API renames were introduced.

## Validation

Regression tests added with the fixes:

- `tests/test_ble_runner_ownership.py`
- `tests/test_ble_coordination_ownership.py`
- `meshtastic/tests/test_ble_reconnection_coverage.py` (extended with the
  reconnect-worker ownership-fence tests)
- `meshtastic/tests/test_tcp_interface_reconnect_ownership.py`
- `meshtastic/tests/test_mesh_interface_queue_send_ownership.py` (extended
  with the queue review follow-up cases, patching the queue runtime module's
  clock instead of the process-wide `time.monotonic`)
- `meshtastic/tests/test_mesh_interface_queue_send_runtime.py` (updated for
  the queue-send ownership semantics)
- `meshtastic/tests/test_mesh_interface_wait_receive.py` (updated: send-loop
  patch targets moved to `_pop_for_send_locked`)

CI gates for these paths: `make ci` — pytest, pylint, ruff on tests, and
`mypy meshtastic/ examples/ tests/ --strict`.

### Not covered by unit tests

Required integration fault injections: late BLE restart during native thread
cleanup; repeated auto-reconnect shutdown/start with a synthetic delayed
`on_exit` callback; BLE management/notifications active during close; TCP
close/socket replacement during `_start_config`, and reconnect while a caller
waits; serial unplug/replug while constructor retry is active; ACK arriving
before, during and after a real RF queue send; packet-ID wraparound; multiple
concurrent senders after `QueueStatus` saturation; setURL replace-all
failure/reconnect with cache invalidation; live BLE device validation,
including peripheral D-Bus long reads. These paths require hardware or
integration-level fault injection and are not covered by the unit tests above.

## Follow-up decisions (not closed by these fixes)

- Should packet IDs be reserved for a post-send replay/ACK window instead of
  only a concurrent active-send window? This affects retransmission semantics
  and callers that explicitly provide packet IDs. Define policy with hardware
  evidence before changing it.
- A native BLE worker hung inside platform I/O cannot be forcibly killed
  safely by Python. Ownership is retained on timeout, but complete resource
  recovery still requires application-level restart/recovery policy.
- The compatibility queue can be mutated by external callers. The new
  enqueue guard and identity-based drop protect standard pathways; direct
  replacement by an external caller remains a noncooperative integration
  seam that should be restricted, documented, or gradually deprecated.
- Error delivery across radio/ACK boundaries is at-least-once/unknown when
  native send returns an ambiguous transport failure. Exactly-once delivery
  is not implied by these lifecycle improvements.

## Review follow-up (2026-11-06)

A follow-up adversarial review of #756–#761 confirmed the transition rules
below. They supplement, and do not replace, the 38-owner inventory above.

- **Runner / coordinator (#756).** A runner that publishes an event loop
  before `run_forever()` executes remains **Starting** until its readiness
  callback runs. A native-start interruption can leave an owner whose
  Python `ident` has not become observable; runner and coordinator retain
  such owners for later cleanup instead of declaring them stopped, and only
  an `OSError`/`RuntimeError` failure with no published `ident` rolls the
  owner back. A close that times out waiting on workers retries coordinator
  cleanup with its remaining budget. Regression tests verify out-of-lock
  restart joins, startup reuse, and interrupted starts staying owned.
- **BLE reconnect (#757).** The start-failure regression exercises the real
  `_schedule_reconnect()` exception path: a failing start publishes a
  replacement worker before the failure is reported, and stale cleanup must
  not erase it. The change is test-only, with typed `Thread`-compatible
  fakes replacing untyped stand-ins.
- **TCP reconnect (#758).** Reconnect success and socket invalidation are
  serialized under `_reconnect_lock`: invalidation precedes the blocking
  native close, so a reconnect success check can no longer report a socket
  being closed as the current owner. Config failure closes only the adopted
  attempt socket, not a successor installed during configuration. The
  installed-socket silent return is hoisted to the top of the locked
  section of `_connect_socket_if_needed()` and applies even while a
  reconnect is mid-config or shutdown was requested afterwards. Tests
  coordinate config-startup and native-close races with `threading.Event`
  waits.
- **TX queue (#759).** Zero-ID packets admitted through the runtime always
  receive distinct private negative queue keys; an externally pre-queued
  key `0` remains supported as backlog. The deadline drop compares the
  packet's own queue key rather than its firmware packet ID. A queue entry
  left by a failed transport send may be replaced by a later same-ID caller
  only when it is still the identical, unclaimed failed instance; live
  owned IDs and unrelated queued IDs continue to reject duplicates. This
  preserves retry admission and does not establish end-to-end idempotency
  after an ambiguous transport error.
- **Timing tests (#761).** The command API tests patch module-scoped
  `LocalTime`/`LocalThreading` proxies and a `ClockedEvent` onto the
  deadline-consuming modules instead of the process-global clock, so the
  timeout case enters the response wait with a nonzero remaining budget and
  the rejection test measures controlled clock deltas rather than elapsed
  process-global scheduler time. The design rejects slow response-wait
  regressions without assuming host scheduling latency.

Draft PRs #690 (interface proxy) and #708 (transport selection) remain
outside this series. The current #690 branch ships an offset-aware BlueZ
D-Bus `ReadValue` handler and related tests, superseding the earlier BlueZ
offset-read review finding; neither branch received modifications in this
follow-up. Both still require backend and hardware integration validation
before any merge decision.

Follow-up (2026-11-09): #690 and #708 merged on 2026-11-07 (commits
7790c314b and b1cc29004). Their behavior is documented in the
[interface proxy](../guides/interface-proxy.md) and
[transport selection](../guides/transport-selection.md) guides.

Follow-up (2026-11-09): #773 (PowerLogger join window and teardown
ownership) merged 2026-11-09 (commit ff0c1e312). PowerLogger.close() joins
the sampler with a bounded window that covers only the sampling loop's exit;
dependency teardown (meter close, Arrow flush, Feather conversion) runs on
the caller thread after the worker is reaped. An interrupted join hands
cleanup ownership to the worker when it is still sampling, or finishes the
cleanup on the caller thread when the loop has already exited, and
re-raises the caller's original exception. A worker that outlives the join
keeps the meter and writer open and the caller receives
`_PowerLoggerShutdownError`.

The follow-up regressions are subject to the same CI gates as the fixes
above and to the integration gaps listed in **Not covered by unit tests**.
