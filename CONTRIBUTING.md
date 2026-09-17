# mtjk Development Guide

> **This repository does not currently accept external pull requests.** General community
> development should be directed to
> [meshtastic/python](https://github.com/meshtastic/python). This document is the
> maintenance guide for the `mtjk` tree itself: local setup, compatibility rules,
> generated code, and CI-equivalent checks.

## Maintained documentation

Keep documentation changes focused on the small set of files that describe the
current project rather than creating new refactor journals or one-off checklists:

- `README.md` — project purpose, installation, status, and user-facing overview;
- `ARCHITECTURE.md` — current internal boundaries and ownership model;
- `COMPATIBILITY.md` — public compatibility policy and alias inventory;
- `CONTRIBUTING.md` — maintenance workflow and validation commands;
- `DEPENDENCY_POLICY.md` — dependency health policy: Renovate abandonment
  exceptions (with revisit dates) and the rules for Git source dependencies;
- `BLE.md` — detailed BLE architecture and integration contracts;
- subsystem contract documents under `meshtastic/` where the contract belongs
  next to the implementation.

Git history is the record for completed refactor plans, dependency campaigns,
and temporary investigation notes. Do not keep those files as active policy once
the work has landed.

## Repository resources

- repository: <https://github.com/jeremiah-k/mtjk>
- issue tracker: <https://github.com/jeremiah-k/mtjk/issues>
- upstream project: <https://github.com/meshtastic/python>

For architecture and compatibility decisions, use the local documents above;
upstream documentation remains useful for Meshtastic concepts but is not the
source of truth for `mtjk` maintenance policy.

## Python and typing baseline

- Runtime baseline is Python 3.10+ (see `pyproject.toml`: `python = "^3.10,<3.15"`).
- Use PEP 604 unions (`X | None`, `A | B`) and built-in generics
  (`dict[K, V]`, `list[T]`, `tuple[T, ...]`) for new and edited annotations.
- Do not churn code with typing-only mass rewrites; normalize typing style only
  in areas already being edited.
- If your LSP/type checker suggests replacing `|` with `Optional`/`Union`,
  fix the tool's interpreter/version configuration first (Poetry-managed env),
  rather than rewriting annotations for legacy pre-3.10 compatibility.
- Do not require contributors to manually create/activate a venv; use
  `poetry install ...` and run tools via `poetry run ...`.

## Docstring style

- The linted docstring convention is NumPy style (Ruff pydocstyle).
- Prefer NumPy-style docstrings for new and edited docstrings.
- Avoid mass docstring rewrites unrelated to the code you are changing.

## API naming and compatibility policy

Use this policy for all code changes (especially AI-assisted refactors):

- Canonical compatibility/deprecation inventory is maintained in
  `COMPATIBILITY.md`.
- New public API names should prefer `camelCase` (for example `sendText`,
  `sendData`).
- Existing public compatibility names must remain callable, including legacy BLE
  `snake_case` names documented in `COMPATIBILITY.md`.
- Internal helpers should be underscore-prefixed `snake_case` (for example
  `_send_packet`).
- Do not break existing public API names for compatibility.
- Symbols in internal subsystem modules (like `meshtastic/interfaces/ble/*`) are
  internal by default unless exposed through the primary package facade.
- AI-assisted refactors must not auto-rename BLE compatibility symbols or
  remove compatibility aliases unless maintainers explicitly request it.

### BLE compatibility rule

The BLE surface has historical public `snake_case` names from the
pre-refactor `meshtastic.ble_interface` API (for example `find_device`,
`read_gatt_char`, `start_notify`). Those names are compatibility APIs and must
remain callable.

When modernizing BLE naming:

1. Keep historical `snake_case` methods callable.
2. Keep only the currently approved BLE camelCase promotions callable:
   `findDevice`, `isConnected`, and `stopNotify`.
3. Route compatibility names to a single implementation (prefer internal
   underscore-prefixed helper methods).
4. Do not add new BLE aliases unless explicitly requested by maintainers.
5. Do not silently remove or hard-rename legacy methods.
6. Update tests/monkeypatch points if alias names are introduced.

#### Historical BLE compatibility baseline

Use this pinned baseline for BLE compatibility decisions:

- Tag: `2.7.7`
- Commit: `b26d80f1866ffa765467e5cb7688c59dee7f2bb2`
- Baseline file: `meshtastic/ble_interface.py`

Historical required BLE wrappers and warning policy are tracked in
`COMPATIBILITY.md` under **BLE Historical Baseline (2.7.7)**.

## Local setup and validation

Install the repository environment with Poetry. For the broadest local check,
include the optional CLI/analysis extras and power-monitor group:

```bash
poetry install --all-extras --with dev,powermon
```

### Updating protobufs

To update the protobuf submodule and regenerate `meshtastic/protobuf/*_pb2.py`
and `*_pb2.pyi` files:

```bash
make protobufs-update
```

The generator needs a `protoc` compiler. The CI workflow uses the `protoc`
binary bundled with nanopb's Linux release package from
<https://jpa.kapsi.fi/nanopb/download/>:

```bash
curl -fsSL -o nanopb-0.4.9.2-linux-x86.tar.gz \
  https://jpa.kapsi.fi/nanopb/download/nanopb-0.4.9.2-linux-x86.tar.gz
printf '%s  %s\n' \
  7e05f5908f0dff5d91cb90d11ca487876de8ec274695887959b3903e4b307887 \
  nanopb-0.4.9.2-linux-x86.tar.gz | sha256sum -c -
tar xzf nanopb-0.4.9.2-linux-x86.tar.gz
mv nanopb-0.4.9.2-linux-x86 nanopb-0.4.9.2
```

The `nanopb-*` directory is intentionally ignored by git. If you already have
another compatible `protoc`, run `PROTOC=/path/to/protoc ./bin/regen-protobufs.sh`.
To allow discovery from `PATH`, run
`ALLOW_SYSTEM_PROTOC=1 ./bin/regen-protobufs.sh`.

### Quick check (recommended)

Run all CI checks locally with a single command:

```bash
make ci
```

This runs the same checks as CI (pylint for library code, ruff for tests, mypy, pytest with coverage).

### Quality-tool ownership

Trunk is the repository-wide linter orchestrator and the version source for
standalone tools such as Ruff, Black, isort, ShellCheck, and the security
scanners. Their pins live in `.trunk/trunk.yaml`; do not copy them into a
second versions file. The standalone Ruff CI job reads its install version
directly from that configuration through `bin/check_quality_tool_versions.py`.

Poetry owns the Python environment and pins project-aware Python tools such as
Pylint and Mypy in `pyproject.toml` and `poetry.lock`. Trunk's
`pylint-poetry` and `mypy-poetry` definitions orchestrate those
Poetry-installed tools rather than installing competing copies. Pylint behavior
is configured in `.pylintrc`, and Ruff behavior is configured in `ruff.toml`
plus Trunk's managed base configuration.

The Poetry application version used to install those dependencies is a build
tool rather than a project dependency. It is pinned in CI and container builds,
and one Renovate custom manager updates every `poetry==X.Y.Z` installation in a
single dependency branch. The container-only export plugin is independently
pinned and maintained by Renovate so image dependency resolution is
reproducible without adding the plugin to every development environment.

Mypy is the canonical Python type checker for this repository. Pyright is not
part of the quality gate: maintaining two overlapping type-checker baselines
added cost without a distinct compatibility guarantee, and a static Pyright
virtualenv path cannot reliably identify Poetry's environment across machines.

### Unified lint/type check via Trunk

Run lint and type checks (including Poetry-managed `pylint` + `mypy`) with one command:

```bash
TRUNK_INTERACTIVE=0 .trunk/trunk check --fix --show-existing
```

This does not run `pytest`; use `make ci` (or `poetry run pytest ...`) for test execution.

### Manual checks

Alternatively, run each check individually:

```bash
poetry run pytest --cov=meshtastic --cov-report=xml
poetry run pylint meshtastic examples/
.trunk/trunk check --filter=ruff meshtastic/tests tests
poetry run mypy meshtastic/
```

To run the `meshtasticd` simulator integration lane locally (same flow as CI):

```bash
./bin/run-smokevirt-with-meshtasticd.sh
```

This requires Docker and runs stable daemon-focused integration tests in
`meshtastic/tests/test_meshtasticd_ci.py` and
`meshtastic/tests/test_meshtasticd_tcp_interface_ci.py` against a simulated
localhost daemon.

To run the full legacy smokevirt suite manually:

```bash
MESHTASTICD_PYTEST_TARGETS="meshtastic/tests/test_smokevirt.py" \
MESHTASTICD_PYTEST_MARK_EXPR="smokevirt and not smoke1_destructive" \
./bin/run-smokevirt-with-meshtasticd.sh
```

For hardware-backed serial smoke tests (`meshtastic/tests/test_smoke1.py`):

```bash
make smoke1
```

This runs only the stable non-destructive smoke1 lane (`smoke1 and not
smoke1_destructive`).

To run the destructive lane (reboot/factory reset/config mutation checks):

> Warning: `make smoke1-destructive` reboots the attached device, mutates
> configuration, and can factory-reset the node. Run this only on disposable
> test hardware, or export/backup the device configuration first.

```bash
make smoke1-destructive
```

Hardware smoke expectations belong in the tests themselves. When firmware
behavior changes intentionally, update the affected smoke assertions and any
user-visible compatibility notes in the same change.

For stricter type checking (optional, not required by CI):

```bash
poetry run mypy meshtastic/ --strict
```

For more commands see [CI workflow](.github/workflows/ci.yml)
