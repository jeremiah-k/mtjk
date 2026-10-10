# mtjk

`mtjk` is a maintained fork of the Meshtastic Python library. It keeps the
existing `meshtastic` Python import namespace while publishing under a separate
package name so the work maintained here can be installed independently.

The fork began as an exploration of BLE connection reliability for
[MMRelay](https://github.com/jeremiah-k/meshtastic-matrix-relay).
What initially looked like a transport-specific problem turned out to touch
lifecycle ownership, concurrency, typing, dependency management, testability,
and several large implementation boundaries. The work expanded incrementally
and was repeatedly cleaned up and tested rather than carried as one very large
upstream patch. Over time that working branch became the maintained fork that
exists today.

The upstream project remains the primary Meshtastic Python project. The goal of
this fork is narrower: maintain the changes developed here, keep them coherent
and well-tested, preserve familiar Meshtastic Python usage where practical, and
make isolated improvements available for upstreaming when they can be separated
cleanly.

## Project goals

The main priorities are:

- preserve the established Meshtastic Python API and import namespace where
  practical;
- improve connection and lifecycle reliability, especially for long-running
  integrations;
- keep concurrency, resource ownership, and failure handling explicit;
- maintain strong typing, linting, tests, and dependency hygiene;
- track current firmware behavior, including Meshtastic 2.8 protocol and CLI
  features;
- keep internal architecture maintainable without forcing downstream callers to
  follow those internal changes.

The repository does not currently accept external pull requests. General Meshtastic
community development should continue to go to
[meshtastic/python](https://github.com/meshtastic/python). Issues that are
specific to `mtjk` can be reported in this repository.

## Compatibility

`mtjk` is designed to be usable as a replacement dependency for applications
that already use Meshtastic Python, but it does not claim perfect behavioral
identity with every upstream release.

Compatibility that is intentionally maintained includes:

- `import meshtastic` remains the Python package namespace;
- `mtjk` is the preferred CLI command, while the historical `meshtastic` command
  remains installed as a silent compatibility entry point;
- established `Node`, `MeshInterface`, BLE, utility, and configuration entry
  points are guarded by API and behavioral compatibility tests;
- historical camelCase and documented legacy aliases remain callable where the
  compatibility policy says they do;
- newer structured BLE exceptions remain catchable as
  `BLEInterface.BLEError`, including the historical `.kind` classification
  contract.

There are also deliberate behavioral differences where retaining the old
behavior would make the library harder or less safe to embed. The most important
example is error handling: library code generally raises exceptions instead of
terminating the host process with `sys.exit()`. Safer defaults and internal
logging behavior may also differ from older upstream releases.

Local CLI configuration writes use fresh device readback where supported. A failed or incomplete verification is reported as a failed CLI operation, rather than a confirmed apply. Dry runs and remote operations have different verification limits; see [CLI configuration and verification](docs/guides/cli-configuration.md) for those distinctions.

See [COMPATIBILITY.md](docs/compatibility.md) for the maintained compatibility
contract and known behavioral differences.

## Notable work maintained here

The exact change set evolves with the upstream project, but the larger areas of
work include:

- BLE connection, reconnect, shutdown, ownership, and notification lifecycle
  hardening;
- request/response correlation and concurrency fixes for long-running clients;
- decomposition of large `MeshInterface`, `Node`, CLI, and BLE implementation
  paths behind compatibility facades;
- stronger typing, linting, static analysis, API baselines, and regression
  coverage;
- dependency and CI cleanup;
- current firmware protocol support and validation, including Meshtastic 2.8
  features;
- simplified Trusted Publisher-based PyPI releases.

For current design details, see [ARCHITECTURE.md](docs/architecture.md). BLE-specific
implementation and integration notes live in [BLE.md](docs/guides/ble.md).

## Installation

### CLI installation with pipx

[pipx](https://pipx.pypa.io/stable/) is recommended for command-line use so the
package runs in an isolated environment.

If upstream `meshtastic` is already installed in that environment, remove it
first. The two distributions intentionally share the `meshtastic` Python
namespace and historical CLI command, so they are not designed to coexist in one
environment.

```bash
pipx uninstall meshtastic || true
pipx install mtjk
```

Verify the installation:

```bash
mtjk --version
```

The package installs both `mtjk` and `meshtastic` console commands. New shell
usage should prefer `mtjk`; existing automation that invokes `meshtastic`
continues to use the same implementation.

### Install the latest `develop`

```bash
pipx uninstall mtjk || true
pipx install "git+https://github.com/jeremiah-k/mtjk.git@develop"
```

### Upgrade or uninstall

```bash
pipx upgrade mtjk
pipx uninstall mtjk
```

### CLI installation with uv

[uv's tool installer](https://docs.astral.sh/uv/guides/tools/) also installs
the CLI in an isolated environment.

```bash
uv tool install mtjk
mtjk --version
```

Upgrade with `uv tool upgrade mtjk`, or uninstall with `uv tool uninstall mtjk`.
To install the unreleased `develop` branch, use:

```bash
uv tool install "git+https://github.com/jeremiah-k/mtjk.git@develop"
```

If `mtjk` is not found after installation, run `uv tool update-shell` and
restart your shell. For development inside a cloned checkout, use the
project environment described in [CONTRIBUTING.md](docs/contributing.md#local-setup-and-validation).

## Using mtjk as a Python dependency

The **distribution name** is `mtjk`, but the **import namespace** remains
`meshtastic`.

Use `mtjk` in dependency declarations:

```text
mtjk
```

or, for the unreleased `develop` branch:

```text
mtjk @ git+https://github.com/jeremiah-k/mtjk.git@develop
```

Install the dependency into your application's environment with its package
manager: for example, `uv add mtjk` in a uv project, or
`python -m pip install mtjk` inside an activated virtual environment.
The isolated environments created by pipx and `uv tool install` are for CLI
use and do not provide imports to a separate application.

The CLI niceties (`segno` for `--qr`, `print-color`, `argcomplete`,
`wcwidth`) are core dependencies — a plain `mtjk` install gets the full
CLI, including QR rendering. The `cli` extra is still accepted as a
no-op, so `mtjk[cli]` keeps working for existing scripts.

`--qr`, `--qr-all`, and `--qr-contact` use compact Unicode blocks.
Interactive output selects the strongest error correction that fits the
terminal while preserving the QR quiet zone. If no complete code fits,
enlarge the terminal or open the displayed URL. Redirected output keeps
maximum error correction; output encodings without block glyphs use ANSI
rendering.

Python code continues to use the familiar imports:

```python
import meshtastic
import meshtastic.serial_interface

with meshtastic.serial_interface.SerialInterface() as interface:
    interface.sendText("hello mesh")
```

There is intentionally no `import mtjk` package.

### Working with cached nodes

`MeshInterface.queryNodes()` returns detached cached node observations and applies filters without making a radio request. `mtjk --nodes --json` exposes the same versioned data for command-line consumers. See [Cached node queries](docs/guides/node-queries.md) for examples, filtering, numeric values, JSON normalization, and the limits of cached data.

## Documentation

Start with the [documentation index](docs/index.md) for setup, library APIs, CLI behavior, compatibility, maintenance, and implementation references. The root README is an introduction, not a second documentation tree.

## Support

Report `mtjk`-specific issues here:

- <https://github.com/jeremiah-k/mtjk/issues>

Please do not file `mtjk`-specific issues with upstream maintainers.

Release procedures and publishing requirements are documented in [Releases](docs/maintainers/releases.md).
