# Documentation

These guides describe the interfaces and maintenance practices of `mtjk`, a maintained fork of the Meshtastic Python library. The [project README](../README.md) explains the work's origins and its relationship with the upstream [Meshtastic Python project](https://github.com/meshtastic/python). Upstream remains the primary project; this repository documents the particular behavior it maintains, without claiming full parity across versions or environments.

## Using the library and CLI

- [Getting started](guides/getting-started.md) — installation boundaries, CLI versus Python dependency, and first checks.
- [CLI configuration and verification](guides/cli-configuration.md) — fresh readback, unverified paths, and exit behavior.
- [Cached node queries](guides/node-queries.md) — `queryNodes` and versioned `--nodes --json` output.
- [Fresh configuration reads](guides/configuration-reads.md) — `readConfig`, `readModuleConfig`, `readPreference`, and request failure semantics.
- [Embedded commands](guides/embedded-commands.md) — `executeCommand`, captured output, connection ownership, and bounded waits.
- [BLE integration](guides/ble.md) — lifecycle, reconnect, and BLE behavior.
- [Lockdown](guides/lockdown.md) — USB-only local lockdown workflow and its limitations.
- [Firmware region presets](guides/region-presets.md) — advertised region/preset capabilities and conservative validation.

## Compatibility and project maintenance

- [Compatibility contract](compatibility.md) — maintained public surfaces, historical aliases, and known differences.
- [Architecture](architecture.md) — current responsibilities and resource ownership.
- [Contributing and validation](contributing.md) — maintainer workflow, project checks, and compatibility safeguards.
- [Dependency policy](maintainers/dependency-policy.md) — dependency decisions and review criteria.
- [Release procedure](maintainers/releases.md) — repository-specific release steps.
- [Admin response contracts](internals/admin-response-contracts.md) — request correlation and callback lifecycle.
- [PPK2 backend evaluation](maintainers/ppk2lab-evaluation.md) — a parked, dated hardware-evaluation plan, not current implementation guidance.

Project-specific test instructions remain close to their tests (for example, [`meshtastic/tests/SIMRADIO.md`](../meshtastic/tests/SIMRADIO.md)). `LICENSE.md`, `README.md`, and `AGENTS.md` remain at the repository root for repository and tooling conventions.
