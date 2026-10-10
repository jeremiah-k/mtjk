# CLI configuration and verification

Local CLI configuration writes are verified against fresh device state. After a local `--set` batch is written and committed, and after a local `--configure` reconnect/reload, the CLI re-reads the affected `LocalConfig`/`LocalModuleConfig` sections from the device and compares them to the requested values. A mismatch, a section that never reloads, or a failed verification readback returns a nonzero exit status and names the fields or sections involved. Only successful readback is reported as a verified apply.

`--dry-run` never writes or verifies. noProto `--set` execution skips verification and must not be described as device-verified. Remote `--dest` configuration continues to follow its separate semantics; a transmitted command is not a guarantee of applied device state. These distinctions affect CLI reporting, not the existing timing or return values of public library setters.

For callers needing a fresh typed value rather than parsing CLI text, use [Configuration reads](configuration-reads.md). For embedded finite CLI actions and the limits of their time budgets, see [Embedded commands](embedded-commands.md).
