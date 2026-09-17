# Meshtastic Python Agent Guide

This document tracks coding standards and API refactoring decisions for the Meshtastic Python library.

## Naming Conventions

### Public API (camelCase)

- Standard public methods should use `camelCase` (e.g., `sendText`, `sendData`).
- BLE promoted camelCase: `findDevice`, `isConnected`, `stopNotify`.
- Powermon promoted camelCase: `getAverageCurrentMA`, `getMinCurrentMA`, `getMaxCurrentMA`, `resetMeasurements`.
- Slog promoted camelCase: `rootDir`, `storeCurrentReading`, `getHealth`.

### Compatibility Shims (snake_case or deprecated camelCase)

- Historical BLE `snake_case` names must remain callable: `find_device`, `read_gatt_char`, `start_notify`.
- BLE `snake_case` shims for promoted names: `is_connected`, `stop_notify`.
- Powermon `snake_case` shims: `get_average_current_mA`, `get_min_current_mA`, `get_max_current_mA`, `reset_measurements`.
- Powermon deprecated camelCase: `getAverageCurrentmA`, `getMinCurrentmA`, `getMaxCurrentmA`.
- Slog `snake_case` shims: `root_dir`, `store_current_reading`.
- Util `snake_case` shim: `message_to_json`.

### Internal Helpers (\_snake_case)

- All internal methods and helpers should be prefixed with an underscore and use `snake_case` (e.g., `_send_packet`, `_handle_disconnect`).
- Exception for BLE compatibility: underscore-prefixed wrappers listed in the BLE
  2.7.7 compatibility matrix (for example `_async_await`, `_async_run`,
  `_from_num_handler`, `_log_radio_handler`, `_legacy_log_radio_handler`) are
  intentionally preserved compatibility entrypoints, not new internal helpers.

## BLE API Refactoring Decisions

Canonical compatibility/deprecation inventory now lives in
`COMPATIBILITY.md`.
`CONTRIBUTING.md` keeps the policy summary and links to the inventory.
Use `COMPATIBILITY.md` as the single source of truth for:

- pinned BLE baseline (`2.7.7`),
- required historical BLE compatibility shims (including
  `_async_await`, `_async_run`, `_from_num_handler`, `_log_radio_handler`,
  `_legacy_log_radio_handler` wrappers),
- warning policy for compatibility aliases.

Any BLE methods introduced only in the refactored `meshtastic/interfaces/ble/*`
package are not historical compatibility surface by default and should not get
"backwards compatibility" wrappers across files unless explicitly approved.

### BLE warning policy (explicit)

- **No deprecation warnings** for historical BLE public methods listed above.
- `find_device` is a historical compatibility shim to `findDevice` and should remain silent.
- Do not add deprecation warnings to other BLE compatibility entrypoints unless explicitly approved.
- Current approved BLE deprecation: `BLECoroutineRunner.run_coroutine_threadsafe(timeout=...)` as alias for `startup_timeout=...` (semantic API migration, not naming-only).

### BLE internal orchestration naming

- Internal handlers and helpers stay `_snake_case`.
- `ReconnectPolicy` is internal orchestration: canonical methods are `next_attempt` and `get_attempt_count` (snake_case).
- Do not treat internal orchestration method names as public compatibility surface.

### BLE export boundary

- Keep BLE public exports explicit and narrow (`meshtastic/ble_interface.py` and `meshtastic/interfaces/ble/__init__.py`).
- Do not leak internal modules/symbols (`runner`, `policies`, `coordination`, etc.) via package exports.

### BLE legacy import compatibility

- `meshtastic.ble_interface` remains a compatibility layer and should keep common historical module-level imports callable for existing callers.
- Preserve silent compatibility for legacy Bleak imports from `meshtastic.ble_interface` (for example `BleakClient`, `BleakScanner`, `BLEDevice`, `BleakError`, `BleakDBusError`) unless removal is explicitly approved.

## Deprecation Tracking (lightweight)

Use grep-friendly code comments for compatibility wrappers so future cleanup is one pass:

- `COMPAT_STABLE_SHIM`: historical/public compatibility shim that should remain callable and should not warn.
- `COMPAT_DEPRECATE`: compatibility shim that should emit `DeprecationWarning` and is a removal candidate in a future major release.

Apply these markers only to compatibility methods that are intentionally part of
the maintained public surface. Do not use these markers for new internal helpers.

### Warning discipline

- Prefer **silent** wrappers for naming-only compatibility aliases unless a removal timeline is explicitly approved.
- Prefer warnings for semantic/behavioral migrations.
- For deprecated APIs that may be called in loops, use warn-once behavior (per process or per instance) to avoid warning spam and overhead.
- All naming-only deprecation warnings MUST be warn-once (not every call).

Quick inventory command:

- `rg -n "COMPAT_STABLE_SHIM|COMPAT_DEPRECATE" meshtastic`

### Compatibility Alias Inventory (source of truth)

- Treat `COMPATIBILITY.md` as authoritative for maintained compatibility names,
  warning policy, and status.
- Treat `COMPAT_STABLE_SHIM` / `COMPAT_DEPRECATE` markers as the grep-able
  implementation inventory for intentionally maintained aliases.
- If a symbol is not listed in `COMPATIBILITY.md` and is not marked with a
  `COMPAT_*` marker in code, do not add compatibility aliases by default.
- For `Node.startOTA`, canonical first-party usage is
  `startOTA(mode=..., ota_file_hash=...)`; legacy aliases (`ota_mode`,
  `ota_hash`, legacy `hash`) remain silently accepted for compatibility.
- `meshtastic.interfaces.ble.runner.get_zombie_runner_count()` is internal diagnostics and intentionally remains snake_case-only unless explicitly approved to expand public surface.

Current `COMPAT_DEPRECATE` methods:

- mt_config: `tunnelInstance` (module attribute alias to `tunnel_instance`, warn-once)
- util: `dotdict` (class alias to `DotDict`, warn-once)
- Slog: `root_dir`, `PowerLogger.store_current_reading` (warn-once)
- Powermon: `PowerMeter.getAverageCurrentmA`, `PowerMeter.getMinCurrentmA`, `PowerMeter.getMaxCurrentmA`
- BLE runner: `BLECoroutineRunner.run_coroutine_threadsafe(timeout=...)` alias for `startup_timeout=...` (warn-once, old internal alias: `_run_coroutine_threadsafe`)

## Powermon API Refactoring Decisions

| Class        | Method                   | Refactor Action        |
| ------------ | ------------------------ | ---------------------- |
| `PowerMeter` | `getAverageCurrentMA`    | Primary implementation |
| `PowerMeter` | `get_average_current_mA` | Compatibility shim     |
| `PowerMeter` | `getMinCurrentMA`        | Primary implementation |
| `PowerMeter` | `get_min_current_mA`     | Compatibility shim     |
| `PowerMeter` | `getMaxCurrentMA`        | Primary implementation |
| `PowerMeter` | `get_max_current_mA`     | Compatibility shim     |
| `PowerMeter` | `resetMeasurements`      | Primary implementation |
| `PowerMeter` | `reset_measurements`     | Compatibility shim     |

### Powermon warning policy (explicit)

- Deprecated camelCase spellings with lowercase unit suffix (`getAverageCurrentmA`, `getMinCurrentmA`, `getMaxCurrentmA`) emit `DeprecationWarning`.
- Canonical camelCase (`getAverageCurrentMA`, `getMinCurrentMA`, `getMaxCurrentMA`) and snake_case shims do not warn.

## Slog API Refactoring Decisions

| Class              | Method                  | Refactor Action              |
| ------------------ | ----------------------- | ---------------------------- |
| `PowerLogger`      | `_p_meter`              | Internal attribute           |
| `PowerLogger`      | `pMeter`                | Public property              |
| `PowerLogger`      | `storeCurrentReading`   | Primary implementation       |
| `PowerLogger`      | `store_current_reading` | Compatibility shim           |
| `PowerLogger`      | `getHealth`             | Public health snapshot       |
| `StructuredLogger` | `getHealth`             | Public health snapshot       |
| `LogSet`           | `getHealth`             | Aggregate health snapshot    |
| `slog module`      | `SlogHealthSnapshot`    | Public immutable health type |
| `slog module`      | `rootDir`               | Primary helper function      |
| `slog module`      | `root_dir`              | Compatibility shim           |

### Slog warning policy (explicit)

- `root_dir` and `PowerLogger.store_current_reading` are maintained
  compatibility shims and currently emit warn-once `DeprecationWarning`.
- Canonical names `rootDir` and `storeCurrentReading` do not warn.

## mt_config API Refactoring Decisions

| Module Attribute  | Refactor Action                |
| ----------------- | ------------------------------ |
| `tunnel_instance` | Primary implementation         |
| `tunnelInstance`  | Compatibility shim (warn-once) |

### mt_config warning policy (explicit)

- `tunnelInstance` emits a warn-once `DeprecationWarning` per process.
- Both `__getattr__` (read) and `__setattr__` (write) paths use the same warn-once tracking.

## util API Refactoring Decisions

| Class     | Name              | Refactor Action                |
| --------- | ----------------- | ------------------------------ |
| `DotDict` | `DotDict`         | Primary implementation         |
| `dotdict` | `dotdict`         | Compatibility shim (warn-once) |
| `util`    | `messageToJson`   | Primary implementation         |
| `util`    | `message_to_json` | Compatibility shim (silent)    |

### util warning policy (explicit)

- `dotdict` emits a warn-once `DeprecationWarning` per process on first instantiation.

## mesh_interface API Refactoring Decisions

| Method                | Refactor Action      |
| --------------------- | -------------------- |
| `telemetryType` param | Semantic deprecation |

### mesh_interface warning policy (semantic)

- The `telemetryType` fallback warning in `sendTelemetry` is a **semantic** deprecation (behavioral change, not naming-only). This warning should emit on every unsupported value to alert callers their input is being silently converted.

### mesh_interface concurrency and shutdown policy

- Keep shared state partitioned by lock responsibility (`_node_db_lock`, `_queue_lock`, `_response_handlers_lock`, `_packet_id_lock`, `_heartbeat_lock`).
- Current contract is **no nested MeshInterface lock acquisition**; snapshot state under one lock and perform I/O/callback publication after releasing it.
- `MeshInterface` must not call `random.seed()` during initialization; library code should not mutate global RNG seeding.
- `_disconnected()` should publish `meshtastic.connection.lost` once per connected session (no duplicate lost events from repeated shutdown paths).

## Release Workflow Modernization Policy

- Release workflow behavior should stay functionally aligned with the historical
  manual release flow on `master`/`2.7.7`:
  - manual dispatch,
  - patch-version bump and commit,
  - draft prerelease creation,
  - packaged artifacts upload,
  - PyPI publish.
- Modernization is allowed and encouraged when tooling is unmaintained or EOL:
  - upgrade deprecated GitHub actions to maintained pinned versions,
  - move Python runtime from 3.9 (EOL) to supported 3.10+.
- Keep release semantics stable while modernizing implementation details.

## Constant Extraction Policy

- Prefer named module-level constants (`UPPER_SNAKE_CASE`) for repeated
  literals (magic numbers/strings), especially for:
  - timeouts and retry/backoff values,
  - protocol/default payload values,
  - repeated warning/error text.
- Avoid mass churn; extract constants in touched code paths or when repetition
  materially impacts readability/maintenance.
- Constant extraction must not change behavior.

## Type Checking

- CI runs mypy without `--strict` to avoid blocking PRs on minor type issues.
- The codebase is currently `--strict` compatible; maintainers can run
  `mypy meshtastic/ --strict` locally to catch regressions.
- `make ci` runs the same checks as CI (no `--strict`).
- `make ci-strict` runs CI checks with strict mypy (for maintainers).

## Lint Scope

- CI `pylint` intentionally targets production/library paths (`meshtastic`,
  `examples`) and excludes `meshtastic/tests/**`.
- Test linting is enforced by `ruff check meshtastic/tests tests` (CI `Ruff Tests`
  job and local `make lint-tests` / `make ci`).

## Baseline Generation

When generating API baselines, always run via poetry to ensure all dependencies are available:

```bash
make api-baseline
make api-baseline-master
```

- `make api-baseline` writes `meshtastic/tests/api_baselines/api_baseline.json` from the current tree via `bin/extract_api_surface.py`.
- `make api-baseline-master` writes `meshtastic/tests/api_baselines/api_baseline_master.json` from `upstream/master` via `bin/generate_master_api_baseline.sh`.
  - Requires an `upstream` remote pointing at `meshtastic/python` (`git remote add upstream https://github.com/meshtastic/python.git` if missing).
  - A nightly workflow (`.github/workflows/sync-upstream-master.yml`) refreshes the committed snapshot and opens a PR against `develop` when the upstream surface changes. Note: CI cannot mirror `upstream/master` to a fork branch because `GITHUB_TOKEN` cannot push trees containing workflow files; local runs fetch `upstream` directly.

Running without poetry may miss modules like `slog` that depend on optional packages. (The mesh tunnel is in-tree and stdlib-only.)
