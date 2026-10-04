# USB lockdown support

This document describes the Python client contract for firmware lockdown provisioning,
authentication, and status handling.

Lockdown is intentionally treated as a **local USB-serial security operation**. The
firmware protocol carries the lockdown passphrase in cleartext inside the admin
request, so `mtjk` refuses to send lockdown authentication over BLE or TCP.

This restriction is a client-side safety boundary, not a claim that the underlying
protobuf field is encrypted by some other transport.

## Scope

The public helper module is `meshtastic.lockdown`. It provides:

- `validate_lockdown_passphrase()`;
- `read_lockdown_passphrase_file()`;
- `build_lockdown_auth()`; and
- `send_lockdown_auth()`.

The CLI exposes the same behavior through:

- `--lockdown-provision`;
- `--lockdown-unlock`;
- `--lockdown-lock-now`; and
- `--lockdown-disable`.

These actions apply only to the directly connected local node.

## Security model

### USB serial only

`send_lockdown_auth()` requires a real `SerialInterface` instance. Passing a TCP or BLE
interface raises `ValueError` before the request is sent.

The lockdown request is sent to the local node with:

- `ADMIN_APP` as the port;
- channel index `0`;
- `wantAck=True`;
- `pkiEncrypted=False`; and
- `RELIABLE` packet priority.

The unencrypted setting is deliberate because the lockdown authentication exchange is
defined separately from the normal encrypted admin-session path. Because the
passphrase is present in cleartext on this wire exchange, extending this helper to a
remote or radio transport would weaken the safety assumption documented here.

### Passphrase size

Lockdown passphrases are byte strings with an enforced length of **1 through 32
bytes**.

`validate_lockdown_passphrase()` converts the input to an owned `bytes` object and
rejects empty values or values longer than 32 bytes. The library does not silently
truncate oversized input.

Interactive and command-line text is encoded as UTF-8 before byte-length validation,
so a 32-character Unicode string is not necessarily a valid 32-byte passphrase.

### Passphrase files

`read_lockdown_passphrase_file()` is the preferred non-interactive input path.

On platforms with POSIX permission bits, the file must have no group or world access.
Mode `0600` is the normal expected form. A file such as `0640` is rejected with
`PermissionError`.

On Windows the POSIX mode check is not applied because those mode bits do not express
the platform's ACL semantics reliably.

The helper reads raw bytes and treats at most one conventional final line ending as
file framing:

- one trailing `\r\n`; or
- one trailing `\n`.

That terminal sequence is removed before passphrase validation. The file-input path
therefore cannot represent a passphrase whose exact final bytes are a single `\n` or
`\r\n`; use another input path if those bytes are part of the passphrase. Additional
trailing CR/LF bytes are not repeatedly stripped, so a file ending in two newlines
retains one newline as passphrase data.

## Building `LockdownAuth`

`build_lockdown_auth()` constructs the protobuf after validating client-side bounds.

Parameters include:

- `passphrase`;
- `boots_remaining`;
- `valid_until_epoch`;
- `max_session_seconds`;
- `lock_now`; and
- `disable`.

The Python-side validation is:

- a non-empty passphrase must be 1–32 bytes;
- `boots_remaining` must be in the unsigned 8-bit range `0..255`;
- `valid_until_epoch` must not be negative; and
- `max_session_seconds` must not be negative.

A passphrase is optional for a library `lock_now` request sent on an already
authorized USB connection. A fresh CLI connection must authenticate first.
The CLI documents zero values for the time/session limits according to firmware policy
rather than inventing additional client-side semantics.

## Sending and waiting for status

`send_lockdown_auth()` performs a short request/status transaction:

1. verify the interface is USB serial;
2. verify the timeout is positive;
3. require `interface.myInfo` so the local node number is known;
4. subscribe to the lockdown-status topic **before** sending;
5. send the local `ADMIN_APP` request;
6. wait for a command-result status associated with the same interface;
7. return a defensive protobuf copy of that status; and
8. unsubscribe in a `finally` block regardless of success or failure.

The subscription filters by interface identity. A status event produced by another
interface cannot complete the transaction.

Startup notifications such as `DISABLED`, `NEEDS_PROVISION`, or `LOCKED` can
arrive after a command is sent. Authentication waits for `UNLOCKED` or
`UNLOCK_FAILED`; lock-now waits for `LOCKED` or `UNLOCK_FAILED`; disable also
accepts `DISABLED`. A `LOCKED` storage-corruption refusal and unknown future
states are returned to the caller rather than silently discarded.

Authentication authorizes the existing USB connection; it does not replace
the configuration snapshot received while locked. After a cold unlock, the
client must request a fresh configuration stream on the same connection
before using cached settings or node-addressed getters. This also refreshes
identity fields that firmware populated from its locked placeholders.
Reopening USB creates a connection that must authenticate again.

On firmware 2.8.1, a cold unlock can restore the saved profile without
initializing the live encryption and signing keys. Local USB administration
then works while encrypted remote administration times out. A reboot while
an unlock token remains valid initializes those keys from the saved profile;
the fresh USB connection still requires authentication. Immediate remote
administration after cold unlock requires the firmware crypto-reload fix.

If no status arrives before the timeout, the normal behavior is `TimeoutError`.
`allow_reboot_without_status=True` changes timeout behavior to return `None`; the CLI
uses this for `--lockdown-lock-now`, where the device may reboot before a final status
can be delivered.

The default library timeout is 20 seconds.

## Receiving lockdown status

Firmware status arrives as `FromRadio.lockdown_status`.

The receive pipeline:

- makes a defensive `LockdownStatus` copy;
- stores it on `MeshInterface.lockdownStatus`; and
- publishes `meshtastic.lockdown_status` with the copied status.

`MeshInterface.lockdownStatus` starts as `None` and represents the most recently
received status for that interface. Consumers that need event-by-event behavior should
subscribe to the topic rather than polling the cached value.

The helper itself also copies the status before returning it to the caller, so callers
do not retain a mutable receive-pipeline protobuf object.

## CLI behavior

The four lockdown actions are mutually exclusive.

### Provision

`--lockdown-provision` provisions or unlocks a hardened local device.

When the passphrase is entered interactively, the CLI asks for it twice and rejects a
mismatch. Provision is considered destructive/sensitive and requires the user to type
`yes` for confirmation unless `--lockdown-yes` is supplied.

### Unlock

`--lockdown-unlock` authenticates the current USB connection to a provisioned device.
It requires a passphrase but does not require the destructive-action confirmation.

### Lock now

`--lockdown-lock-now` asks the device to revoke current lockdown sessions and reboot
into the locked state. It requires a passphrase and authenticates the same USB
connection before sending the lock request. Authentication must return `UNLOCKED`;
a refusal, timeout, or other state prevents the reboot request. A device that does
not report an active lockdown state is rejected before authentication so this
action cannot accidentally provision a device with lockdown disabled. The CLI
waits up to `--lockdown-wait` for the initial status because firmware can queue
it after USB configuration completion; status events from other interfaces
cannot satisfy this wait.

It requires the user to type `yes` for confirmation unless `--lockdown-yes` is
supplied. The CLI permits the device to reboot before a structured status arrives and
reports that the command may already be taking effect when no final status is available.

### Disable

`--lockdown-disable` requests disabling lockdown and reverting the firmware-managed
storage state according to the device's lockdown implementation.

It requires a passphrase and requires the user to type `yes` for confirmation unless
`--lockdown-yes` is supplied.

## CLI passphrase input

The CLI chooses passphrase input in this order:

1. `--lockdown-passphrase-file`;
2. `--lockdown-passphrase`; or
3. hidden interactive input through `getpass`.

Putting a passphrase directly on the command line is intentionally discouraged because
argv may be exposed through shell history and process inspection. Using
`--lockdown-passphrase` therefore also requires:

```text
--insecure-lockdown-passphrase-on-command-line
```

Without that explicit acknowledgement the CLI exits with an error.

For non-interactive automation, prefer an operator-only passphrase file rather than an
argv passphrase.

## Additional CLI limits

The CLI passes these request fields through `build_lockdown_auth()`, which applies the
client-side bounds described above:

- `--lockdown-boots`;
- `--lockdown-valid-until`; and
- `--lockdown-max-session-seconds`.

`--lockdown-wait` is separate from `LockdownAuth`. It controls how many seconds
`send_lockdown_auth()` waits for a structured `LockdownStatus`; that helper rejects a
non-positive timeout before sending.

The CLI refuses lockdown actions when `--dest` identifies a remote node, even before
the USB-only helper performs its transport check.

## Status presentation and failures

When the CLI receives a status, it prints the protobuf `LockdownStatus.State` name. If
the firmware supplies an unknown enum value, the CLI prints a numeric fallback rather
than crashing.

A nonzero `backoff_seconds` value is displayed as retry guidance.

Backoff enforcement belongs to the firmware. A reported delay does not promise
that elapsed time alone permits another attempt: firmware can also require a
reboot after an authentication failure. The CLI does not reboot or retry
credentials automatically. Keep the passphrase and a recovery image available
when testing lockdown, and disable lockdown before restoring firmware that
cannot read its encrypted storage.

`UNLOCK_FAILED` is treated as an authentication failure and results in a nonzero CLI
exit.

Library callers instead receive normal Python exceptions:

- `ValueError` for invalid input, invalid timeout, or non-USB transport;
- `PermissionError` for an insecure POSIX passphrase file;
- `RuntimeError` when the device has not provided `myInfo`; and
- `TimeoutError` when no status arrives and reboot-without-status is not allowed.

The library does not terminate the process. This follows the repository-wide error
policy described in [COMPATIBILITY.md](COMPATIBILITY.md).

## Compatibility and maintenance rules

When modifying lockdown support:

- do not broaden transport support without revisiting the cleartext-passphrase threat
  model;
- subscribe before sending so an immediate firmware status cannot be missed;
- keep status filtering scoped to the originating interface;
- keep unsubscription in all exit paths;
- return/store defensive protobuf copies;
- keep passphrase byte limits explicit and reject instead of truncating;
- do not weaken POSIX passphrase-file permissions for convenience; and
- preserve the CLI's explicit acknowledgement before accepting argv passphrases.

If the firmware protocol changes how lockdown credentials are protected on the wire,
this document and the transport restriction should be reviewed together.

## Primary tests

The focused coverage is in:

- `meshtastic/tests/test_lockdown.py`;
- the lockdown-status receive tests in `meshtastic/tests/test_receive_pipeline.py`;
- lockdown CLI tests in the device-action/main test suites; and
- API/import compatibility tests for the public helper surface.

Treat changes here as security-boundary changes even when the code diff is small.
