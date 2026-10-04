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
- `PPK2LAB_EVALUATION.md` — living evaluation plan for a possible ppk2lab
  powermon backend; retire it into git history once the backend decision is
  made;
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

- Runtime baseline is Python 3.11+ (see `pyproject.toml`: `requires-python = ">=3.11,<3.15"`).
- Use PEP 604 unions (`X | None`, `A | B`) and built-in generics
  (`dict[K, V]`, `list[T]`, `tuple[T, ...]`) for new and edited annotations.
- Do not churn code with typing-only mass rewrites; normalize typing style only
  in areas already being edited.
- If your LSP/type checker suggests replacing `|` with `Optional`/`Union`,
  fix the tool's interpreter/version configuration first (uv-managed env),
  rather than rewriting annotations for legacy pre-3.11 compatibility.
- Do not require contributors to manually create/activate a venv; use
  `uv sync --locked ...` and run tools via `uv run --locked ...`.

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

uv manages the project's Python environment, installs dependencies, and runs
commands inside that environment. No manual virtual-environment
activation is needed when using `uv run` or the Make targets.

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and check
the installation with `uv --version`. The minimum supported uv version is
declared by `tool.uv.required-version` in `pyproject.toml`; upgrade uv if it
reports that your version is too old. From the repository root, run
`uv sync --locked` to create the local `.venv` with the core package and the
default development group. Python 3.11–3.14 is supported; use `--python 3.13`
to select an interpreter explicitly. For the same dependency selection as CI,
include the optional analysis extra and power-monitor group:

```bash
uv sync --locked --all-extras --group powermon
```

Use `uv sync --locked --no-dev` for a core-only environment and
`uv run --locked --no-dev mtjk --version` to keep development tools excluded.
The published analysis extra is
selected with `--extra analysis`; the separate `analysis` dependency group
contains notebook and plotting tools and is selected with `--group analysis`.
For notebooks, install both with `uv sync --locked --extra analysis --group analysis`.

Project commands use `uv run --locked` so stale lockfiles fail instead of being
rewritten implicitly. Use `uv add`, `uv remove`, and `uv lock` deliberately when
changing dependencies, and commit both `pyproject.toml` and `uv.lock`.
`uv lock --upgrade-package NAME` updates one locked dependency. For a local
wheel and source build, run `uv build`; consumers can install either artifact
with pip without having uv installed.

Standalone asset recovery for historical tags without a uv lockfile uses an
isolated, pinned Poetry tool through uv. The build script selects the tagged
lockfile; development and CI use uv directly.

### Common uv commands

| Command                          | Purpose                                                        |
| -------------------------------- | -------------------------------------------------------------- |
| `uv sync --locked`               | Install the project's locked dependencies into `.venv`.        |
| `uv run --locked mtjk --version` | Run this checkout's CLI in the project environment.            |
| `uv run --locked pytest -m unit` | Run unit tests with the project's development dependencies.    |
| `uv add NAME` / `uv remove NAME` | Change a project dependency and update the lockfile.           |
| `uv lock --upgrade-package NAME` | Update one dependency within its declared version constraints. |
| `uv tool install mtjk`           | Install a separate CLI environment for end-user use.           |

`pyproject.toml` declares dependencies; `uv.lock` records the selected versions;
`.venv` holds the installed project and dependencies. The `--locked` flag checks
that the declarations and lockfile agree and fails if they do not. It does not
prevent uv from creating or updating `.venv` to match the lockfile.

### Updating protobufs

To update the protobuf submodule and regenerate `meshtastic/protobuf/*_pb2.py`
and `*_pb2.pyi` files:

```bash
make protobufs-update
```

The generator needs a `protoc` compiler. The CI workflow uses the `protoc`
binary bundled with nanopb's Linux release package. It is fetched from the
GitHub release first, with the upstream download page
(<https://jpa.kapsi.fi/nanopb/download/>) as a fallback:

```bash
curl -fsSL -o nanopb-0.4.9.2-linux-x86.tar.gz \
  https://github.com/nanopb/nanopb/releases/download/nanopb-0.4.9.2/nanopb-0.4.9.2-linux-x86.tar.gz
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

### Artifact smoke gate

`make check-artifacts` (or `python3 bin/smoke_distributions.py`) builds the
final-candidate sdist and wheel from the current checkout with
`python -m build`, then proves them as installed distributions in fresh
isolated consumer venvs: wheel/core, wheel/analysis, sdist/core, sdist/analysis.
It checks wheel RECORD/metadata/entry-points contents, protobuf roundtrips and
the `meshtastic.protobuf` namespace plus its compatibility facade, offline CLI
surfaces (`--version`, `--help`, `--list-fields`, `--describe-field`), and a
real Feather read plus `mesh-analysis --no-server --slog` run in the analysis
cells. Any failure exits nonzero with the failing phase, artifact, profile,
and command.

Prerequisites: a `python3` with the `venv` module, `pip`, network access for
dependency resolution, and `pip install build` for the interpreter that runs
the gate. To validate retained artifacts instead of building fresh, pass
`--dist-dir PATH --skip-build`. `--work-dir PATH` relocates the gate's owned
scratch workspace (consumer venvs, probes, pip cache) when the default system
temporary directory is unsuitable.

### Quality-tool ownership

Trunk is the repository-wide linter orchestrator and the version source for
standalone tools such as Ruff, Black, isort, ShellCheck, and the security
scanners. Their pins live in `.trunk/trunk.yaml`; do not copy them into a
second versions file. The standalone Ruff CI job reads its install version
directly from that configuration through `bin/check_quality_tool_versions.py`.

uv owns the Python environment and pins project-aware Python tools such as
Pylint and Mypy in `pyproject.toml` and `uv.lock`. Trunk's
`pylint-uv` and `mypy-uv` definitions orchestrate those
uv-installed tools rather than installing competing copies. Pylint behavior
is configured in `.pylintrc`, and Ruff behavior is configured in `ruff.toml`
plus Trunk's managed base configuration.

The uv application version is pinned in CI and container builds. Renovate's
PEP 621 manager maintains dependencies and the uv lockfile; a custom manager
keeps the uv version aligned across setup actions and container installation.
The uv build backend is declared separately in the standard build-system table.

Mypy is the canonical Python type checker for this repository. Pyright is not
part of the quality gate: maintaining two overlapping type-checker baselines
added cost without a distinct compatibility guarantee, and a static Pyright
virtualenv path cannot reliably identify uv's environment across machines.

### Unified lint/type check via Trunk

Run lint and type checks (including uv-managed `pylint` + `mypy`) with one command:

```bash
TRUNK_INTERACTIVE=0 .trunk/trunk check --fix --show-existing
```

This does not run `pytest`; use `make ci` (or `uv run --locked pytest ...`) for test execution.

### Manual checks

Alternatively, run each check individually:

```bash
uv run --locked pytest --cov=meshtastic --cov-report=xml
uv run --locked pylint meshtastic examples/
.trunk/trunk check --filter=ruff meshtastic/tests tests
uv run --locked mypy meshtastic/
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
uv run --locked mypy meshtastic/ --strict
```

For more commands see [CI workflow](.github/workflows/ci.yml)
