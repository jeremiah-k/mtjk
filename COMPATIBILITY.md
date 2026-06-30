# Compatibility Inventory and Deprecation Matrix

This document is the canonical source of truth for compatibility aliases,
deprecations, and legacy compatibility behaviors in this repository.

If a compatibility symbol is not listed here, do not add or keep it by default.

## Scope and Policy

- Runtime baseline is Python 3.10+.
- Public API names prefer `camelCase`.
- Historical compatibility shims remain callable where documented below.
- Internal helpers prefer underscore-prefixed `snake_case`.
- Naming-only compatibility aliases are silent unless explicitly marked deprecated.
- Naming-only deprecations must be warn-once.
- Semantic deprecations may warn on every invalid usage.

## Status Legend

- `PRIMARY`: canonical symbol for new code.
- `COMPAT_STABLE_SHIM`: maintained compatibility alias; callable and silent.
- `COMPAT_DEPRECATE`: maintained compatibility alias that emits
  `DeprecationWarning` (warn-once unless explicitly noted).
- `SEMANTIC_DEPRECATE`: behavioral migration warning (not naming-only).
- `INTERNAL_COMPAT`: compatibility entrypoint intentionally retained for
  integrations, but not considered public API growth.

## Runtime Import Compatibility

This section documents the stability guarantees for runtime decomposition modules
(`mesh_interface_runtime`, `node_runtime`). These modules contain internal
implementation details that are NOT part of the public stable API.

### Stability Categories

1. **Public Stable API** - Guaranteed stable, use for production code:
   - `meshtastic.Node` → from `meshtastic.node`
   - `meshtastic.MeshInterface` → from `meshtastic.mesh_interface`
   - `meshtastic.BROADCAST_ADDR`, `meshtastic.LOCAL_ADDR`

2. **Runtime Modules** - Internal implementation, NOT guaranteed stable:
   - `meshtastic.mesh_interface_runtime.*` - Internal runtime decomposition
   - `meshtastic.node_runtime.*` - Internal runtime decomposition
   - These modules may change without deprecation warnings

3. **Compatibility Exports** - Specific exports retained for mocking/testing:
   - `meshtastic.node_runtime.settings_runtime.toNodeNum` - For test mocking compatibility
   - Documented individually below

4. **Private Internals** - Underscore-prefixed, NOT guaranteed stable:
   - Any symbol starting with `_` (e.g., `_NodeSettingsRuntime`)
   - May change at any time without notice
   - Do not import directly in production code

### Documented Runtime Compatibility Exports

The following specific exports from runtime modules are retained for
test ecosystem compatibility. These are the ONLY runtime imports with
stability guarantees:

| Module Path                                | Export      | Purpose                    | Status               |
| ------------------------------------------ | ----------- | -------------------------- | -------------------- |
| `meshtastic.node_runtime.settings_runtime` | `toNodeNum` | Test mocking compatibility | `COMPAT_STABLE_SHIM` |

### Policy for External Test Authors

If you need to mock or patch Meshtastic internals in your tests:

1. **Prefer public API** - Mock at the public API level when possible
2. **Use documented compatibility exports** - Only these are guaranteed stable
3. **Accept breakage risk** - Importing underscore-prefixed internals may break in any release
4. **Open an issue** - If you need a specific internal for mocking, request it be added as `INTERNAL_COMPAT`

### Import Stability Matrix

```markdown
meshtastic.Node STABLE (public API)
meshtastic.mesh_interface.MeshInterface STABLE (public API)
meshtastic.node_runtime.settings_runtime.toNodeNum COMPAT (testing)
meshtastic.node_runtime.settings_runtime.\_NodeSettingsRuntime INTERNAL (may change)
meshtastic.mesh_interface_runtime.\_RequestWaitRuntime INTERNAL (may change)
```

---

## Authoritative Baselines

- BLE historical baseline tag: `2.7.7`
- BLE baseline commit: `b26d80f1866ffa765467e5cb7688c59dee7f2bb2`
- Baseline file: `meshtastic/ble_interface.py`

## BLE Historical Baseline (2.7.7)

The following historical BLE symbols are required compatibility surface and must
remain callable and silent.

| Symbol                                  | Status               | Warning policy | Notes                                  |
| --------------------------------------- | -------------------- | -------------- | -------------------------------------- |
| `BLEClient.async_await`                 | `COMPAT_STABLE_SHIM` | Silent         | Delegates to `_async_await`.           |
| `BLEClient.async_run`                   | `COMPAT_STABLE_SHIM` | Silent         | Delegates to `_async_run`.             |
| `BLEInterface.from_num_handler`         | `COMPAT_STABLE_SHIM` | Silent         | Delegates to `_from_num_handler`.      |
| `BLEInterface.log_radio_handler`        | `COMPAT_STABLE_SHIM` | Silent         | Keep historical `async def` signature. |
| `BLEInterface.legacy_log_radio_handler` | `COMPAT_STABLE_SHIM` | Silent         | Keep historical `async def` signature. |

Additional approved BLE compatibility and promotions:

| Symbol                                                                                     | Status               | Warning policy | Notes                                                                              |
| ------------------------------------------------------------------------------------------ | -------------------- | -------------- | ---------------------------------------------------------------------------------- |
| `BLEClient.find_device`                                                                    | `COMPAT_STABLE_SHIM` | Silent         | Historical snake_case.                                                             |
| `BLEClient.findDevice`                                                                     | `PRIMARY`            | Silent         | Approved promoted camelCase name.                                                  |
| `BLEClient.read_gatt_char`                                                                 | `PRIMARY`            | Silent         | Historical snake_case BLE API; no promoted camelCase alias.                        |
| `BLEClient.start_notify`                                                                   | `PRIMARY`            | Silent         | Historical snake_case BLE API; no promoted camelCase alias.                        |
| `BLEClient.is_connected`                                                                   | `COMPAT_STABLE_SHIM` | Silent         | Shim for `isConnected`.                                                            |
| `BLEClient.isConnected`                                                                    | `PRIMARY`            | Silent         | Approved promoted camelCase name.                                                  |
| `BLEClient.stop_notify`                                                                    | `COMPAT_STABLE_SHIM` | Silent         | Shim for `stopNotify`.                                                             |
| `BLEClient.stopNotify`                                                                     | `PRIMARY`            | Silent         | Approved promoted camelCase name.                                                  |
| `BLEErrorHandler.safe_execute`                                                             | `COMPAT_STABLE_SHIM` | Silent         | Wrapper alias for `_safe_execute`.                                                 |
| `BLEErrorHandler.safe_cleanup`                                                             | `COMPAT_STABLE_SHIM` | Silent         | Wrapper alias for `_safe_cleanup`.                                                 |
| `BLECompatibilityEventService.publish_connection_status_legacy`                            | `COMPAT_STABLE_SHIM` | Silent         | Wrapper alias for `publish_connection_status`.                                     |
| `BLECompatibilityEventPublisher.publish_connection_status_legacy`                          | `COMPAT_STABLE_SHIM` | Silent         | Bound wrapper alias for `publish_connection_status`.                               |
| `meshtastic.interfaces.ble.lifecycle_service._is_currently_connected_elsewhere`            | `INTERNAL_COMPAT`    | Silent         | Internal monkeypatch/probe seam re-exported from `gating`.                         |
| `meshtastic.interfaces.ble.lifecycle_service._ORIGINAL_GET_CONNECTED_CLIENT_STATUS`        | `INTERNAL_COMPAT`    | Silent         | Internal runtime baseline alias for status monkeypatch detection.                  |
| `meshtastic.interfaces.ble.lifecycle_service._ORIGINAL_GET_CONNECTED_CLIENT_STATUS_LOCKED` | `INTERNAL_COMPAT`    | Silent         | Internal runtime baseline alias for locked status monkeypatch detection.           |
| `meshtastic.interfaces.ble.lifecycle_service._ORIGINAL_VERIFY_OWNERSHIP_SNAPSHOT`          | `INTERNAL_COMPAT`    | Silent         | Internal runtime baseline alias for ownership-snapshot monkeypatch detection.      |
| `meshtastic.interfaces.ble.lifecycle_service._ORIGINAL_FINALIZE_CONNECTION_GATES`          | `INTERNAL_COMPAT`    | Silent         | Internal runtime baseline alias for gate-finalization monkeypatch detection.       |
| `meshtastic.interfaces.ble.lifecycle_service._ORIGINAL_IS_OWNED_CONNECTED_CLIENT`          | `INTERNAL_COMPAT`    | Silent         | Internal runtime baseline alias for ownership-check monkeypatch detection.         |
| `BLEInterface.find_device`                                                                 | `COMPAT_STABLE_SHIM` | Silent         | Historical snake_case wrapper.                                                     |
| `BLEInterface.findDevice`                                                                  | `PRIMARY`            | Silent         | Approved promoted camelCase name.                                                  |
| `BLEInterface._from_num_handler`                                                           | `COMPAT_STABLE_SHIM` | Silent         | Preserved compatibility entrypoint (shim).                                         |
| `BLEInterface._log_radio_handler`                                                          | `COMPAT_STABLE_SHIM` | Silent         | Preserved compatibility entrypoint (shim).                                         |
| `BLEInterface._legacy_log_radio_handler`                                                   | `COMPAT_STABLE_SHIM` | Silent         | Preserved compatibility entrypoint (shim).                                         |
| `BLENotificationDispatcher._log_radio_handler`                                             | `COMPAT_STABLE_SHIM` | Silent         | Preserved compatibility entrypoint (shim).                                         |
| `BLENotificationDispatcher._legacy_log_radio_handler`                                      | `COMPAT_STABLE_SHIM` | Silent         | Preserved compatibility entrypoint (shim).                                         |
| `BLEClient._discover`                                                                      | `COMPAT_STABLE_SHIM` | Silent         | Historical internal discovery entrypoint.                                          |
| `BLEReceiveRecoveryService`                                                                | `COMPAT_STABLE_SHIM` | Silent         | Legacy receive-service class alias re-exported from `receive_service`.             |
| `BLEStateManager.current_state`                                                            | `INTERNAL_COMPAT`    | Silent         | Compatibility alias for `_current_state`.                                          |
| `BLEStateManager.is_connected`                                                             | `INTERNAL_COMPAT`    | Silent         | Compatibility alias for `_is_connected`.                                           |
| `BLEStateManager.is_closing`                                                               | `INTERNAL_COMPAT`    | Silent         | Compatibility alias for `_is_closing`.                                             |
| `BLEStateManager.can_connect`                                                              | `INTERNAL_COMPAT`    | Silent         | Compatibility alias for `_can_connect`.                                            |
| `BLEStateManager.is_connecting`                                                            | `INTERNAL_COMPAT`    | Silent         | Public property alias for `_is_connecting`.                                        |
| `BLEStateManager.is_active`                                                                | `INTERNAL_COMPAT`    | Silent         | Compatibility alias for `_is_active`.                                              |
| `BLEStateManager.transition_to()`                                                          | `INTERNAL_COMPAT`    | Silent         | Compatibility wrapper for `_transition_to()`.                                      |
| `BLEStateManager.reset_to_disconnected()`                                                  | `INTERNAL_COMPAT`    | Silent         | Compatibility wrapper for `_reset_to_disconnected()`.                              |
| `BLEStateManager._lock`                                                                    | `INTERNAL_COMPAT`    | Silent         | Legacy alias for `lock` property.                                                  |
| `BLEManagementCommandHandler._start_management_phase()`                                    | `INTERNAL_COMPAT`    | Silent         | Internal compatibility alias for `start_management_phase()`.                       |
| `BLEManagementCommandHandler._resolve_management_target()`                                 | `INTERNAL_COMPAT`    | Silent         | Internal compatibility alias for `resolve_management_target()`.                    |
| `BLEManagementCommandHandler._acquire_client_for_target()`                                 | `INTERNAL_COMPAT`    | Silent         | Internal compatibility alias for `acquire_client_for_target()`.                    |
| `BLEManagementCommandHandler._execute_with_client()`                                       | `INTERNAL_COMPAT`    | Silent         | Internal compatibility alias for `execute_with_client()`.                          |
| `BLEManagementCommandsService._resolve_handler()`                                          | `INTERNAL_COMPAT`    | Silent         | Internal compatibility alias for service shim handler resolution.                  |
| `BLEManagementCommandsService._make_handler()`                                             | `INTERNAL_COMPAT`    | Silent         | Internal compatibility alias for service shim handler resolution.                  |
| `BLEInterface._fromnum_notify_enabled`                                                     | `INTERNAL_COMPAT`    | Silent         | Internal compatibility bridge to dispatcher-backed FROMNUM notify state.           |
| `BLEInterface._malformed_notification_count`                                               | `INTERNAL_COMPAT`    | Silent         | Internal compatibility bridge to dispatcher-backed malformed-notification counter. |
| `BLEInterface._malformed_notification_lock`                                                | `INTERNAL_COMPAT`    | Silent         | Internal compatibility bridge to dispatcher-backed malformed-notification lock.    |
| `meshtastic.ble_interface.BleakClient`                                                     | `COMPAT_STABLE_SHIM` | Silent         | Legacy module-level import compatibility.                                          |
| `meshtastic.ble_interface.BleakScanner`                                                    | `COMPAT_STABLE_SHIM` | Silent         | Legacy module-level import compatibility.                                          |
| `meshtastic.ble_interface.BLEDevice`                                                       | `COMPAT_STABLE_SHIM` | Silent         | Legacy module-level import compatibility.                                          |
| `meshtastic.ble_interface.BleakError`                                                      | `COMPAT_STABLE_SHIM` | Silent         | Legacy module-level import compatibility.                                          |
| `meshtastic.ble_interface.BleakDBusError`                                                  | `COMPAT_STABLE_SHIM` | Silent         | Legacy module-level import compatibility.                                          |

Approved BLE deprecation:

| Symbol                                                      | Status             | Warning policy                | Notes                            |
| ----------------------------------------------------------- | ------------------ | ----------------------------- | -------------------------------- |
| `BLECoroutineRunner._run_coroutine_threadsafe(timeout=...)` | `COMPAT_DEPRECATE` | Warn-once per runner instance | Alias for `startup_timeout=...`. |

## Deprecated Compatibility Aliases

These are intentionally maintained deprecated aliases.

| Symbol                                | Canonical replacement               | Status             | Warning policy                             |
| ------------------------------------- | ----------------------------------- | ------------------ | ------------------------------------------ |
| `mt_config.tunnelInstance`            | `mt_config.tunnel_instance`         | `COMPAT_DEPRECATE` | Warn-once per process (read/write/delete). |
| `util.dotdict`                        | `util.DotDict`                      | `COMPAT_DEPRECATE` | Warn-once per process.                     |
| `slog.root_dir()`                     | `slog.rootDir()`                    | `COMPAT_DEPRECATE` | Warn-once per process.                     |
| `PowerLogger.store_current_reading()` | `PowerLogger.storeCurrentReading()` | `COMPAT_DEPRECATE` | Warn-once per instance.                    |
| `PowerMeter.getAverageCurrentmA()`    | `PowerMeter.getAverageCurrentMA()`  | `COMPAT_DEPRECATE` | Warn-once per process key.                 |
| `PowerMeter.getMinCurrentmA()`        | `PowerMeter.getMinCurrentMA()`      | `COMPAT_DEPRECATE` | Warn-once per process key.                 |
| `PowerMeter.getMaxCurrentmA()`        | `PowerMeter.getMaxCurrentMA()`      | `COMPAT_DEPRECATE` | Warn-once per process key.                 |

Semantic deprecation:

| Behavior                                                                                | Status               | Warning policy               | Notes                                          |
| --------------------------------------------------------------------------------------- | -------------------- | ---------------------------- | ---------------------------------------------- |
| `MeshInterface.sendTelemetry(telemetryType=<unsupported>)` fallback to `device_metrics` | `SEMANTIC_DEPRECATE` | Warn every unsupported input | Behavioral migration warning, not naming-only. |

## Stable Compatibility Aliases (Silent)

Note: underscore-prefixed canonical symbols listed here are implementation
details. For `COMPAT_STABLE_SHIM` rows, treat the compatibility symbol column as
the intended external entrypoint. `INTERNAL_COMPAT` rows are retained for
compatibility/patching and are not recommended public surface.

### Core Package and CLI

| Module                | Compatibility symbol                    | Canonical symbol                       |
| --------------------- | --------------------------------------- | -------------------------------------- |
| `meshtastic.__init__` | `meshtastic.serial` (lazy module alias) | third-party `serial` (pyserial) module |
| `meshtastic.__main__` | `support_info()`                        | `supportInfo()`                        |
| `meshtastic.__main__` | `export_config`                         | `exportConfig`                         |
| `meshtastic.__main__` | `create_power_meter`                    | `_create_power_meter`                  |
| `meshtastic.__main__` | `_PREFERENCE_FIELD_ALIASES` legacy keys | canonical protobuf preference names    |
| `meshtastic.version`  | `get_active_version()`                  | `getActiveVersion()`                   |
| `meshtastic.test`     | `subscribe()`                           | `subscribeToNodeUpdates()`             |

### Runtime Module Compatibility Exports

The following runtime module exports are explicitly retained for test ecosystem
compatibility. These are the ONLY runtime module imports with stability guarantees:

| Module                                     | Compatibility symbol | Canonical symbol | Notes                                           |
| ------------------------------------------ | -------------------- | ---------------- | ----------------------------------------------- |
| `meshtastic.node_runtime.settings_runtime` | `toNodeNum`          | `util.toNodeNum` | Test mocking compatibility. COMPAT_STABLE_SHIM. |

All other runtime module internals (especially underscore-prefixed) are NOT
considered stable API and may change without deprecation warnings.

`_PREFERENCE_FIELD_ALIASES` currently normalizes:

- `display.use_12_hour -> display.use_12h_clock`
- `display.use12_hour -> display.use_12h_clock`
- `display.use12h_clock -> display.use_12h_clock`
- `display.use12_h_clock -> display.use_12h_clock`

### Utility and Config

| Module                 | Compatibility symbol      | Canonical symbol       |
| ---------------------- | ------------------------- | ---------------------- |
| `meshtastic.host_port` | `parse_host_and_port()`   | `parseHostAndPort()`   |
| `meshtastic.util`      | `blacklistVids`           | `BLACKLIST_VIDS`       |
| `meshtastic.util`      | `whitelistVids`           | `WHITELIST_VIDS`       |
| `meshtastic.util`      | `our_exit()`              | `ourExit()`            |
| `meshtastic.util`      | `remove_keys_from_dict()` | `removeKeysFromDict()` |
| `meshtastic.util`      | `detect_windows_port()`   | `detectWindowsPort()`  |
| `meshtastic.util`      | `message_to_json()`       | `messageToJson()`      |
| `meshtastic.util`      | `to_node_num()`           | `toNodeNum()`          |
| `meshtastic.util`      | `flags_to_list()`         | `flagsToList()`        |
| `meshtastic.util`      | `flags_from_list()`       | `flagsFromList()`      |

### Node and Tunnel

| Module                      | Compatibility symbol                                   | Canonical symbol                        |
| --------------------------- | ------------------------------------------------------ | --------------------------------------- |
| `meshtastic.mesh_interface` | `_generatePacketId()`                                  | `_generate_packet_id()`                 |
| `meshtastic.mesh_interface` | `_sendPacket()`                                        | `_send_packet()`                        |
| `meshtastic.node`           | `position_flags_list()`                                | `positionFlagsList()`                   |
| `meshtastic.node`           | `excluded_modules_list()`                              | `excludedModulesList()`                 |
| `meshtastic.node`           | `module_available()`                                   | `moduleAvailable()`                     |
| `meshtastic.node`           | `get_ringtone()`                                       | `getRingtone()`                         |
| `meshtastic.node`           | `set_ringtone()`                                       | `setRingtone()`                         |
| `meshtastic.node`           | `get_canned_message()`                                 | `getCannedMessage()`                    |
| `meshtastic.node`           | `set_canned_message()`                                 | `setCannedMessage()`                    |
| `meshtastic.node`           | `get_channels_with_hash()`                             | `getChannelsWithHash()`                 |
| `meshtastic.node`           | `startOTA(ota_mode=..., ota_hash=..., hash=...)`       | `startOTA(mode=..., ota_file_hash=...)` |
| `meshtastic.node`           | live-channel semantics of `getChannelByChannelIndex()` | historical mutate-then-write behavior   |
| `meshtastic.tunnel`         | `udpBlacklist`                                         | `UDP_BLACKLIST`                         |
| `meshtastic.tunnel`         | `tcpBlacklist`                                         | `TCP_BLACKLIST`                         |
| `meshtastic.tunnel`         | `protocolBlacklist`                                    | `PROTOCOL_BLACKLIST`                    |
| `meshtastic.tunnel`         | `_shouldFilterPacket()`                                | `_should_filter_packet()`               |
| `meshtastic.tunnel`         | `_ipToNodeId()`                                        | `_ip_to_node_id()`                      |
| `meshtastic.tunnel`         | `_nodeNumToIp()`                                       | `_node_num_to_ip()`                     |
| `meshtastic.tunnel`         | `sendPacket()`                                         | `_send_packet()`                        |

For `Node.startOTA`, use canonical call style in first-party code and docs:
`startOTA(mode=..., ota_file_hash=...)`. Legacy aliases (`ota_mode`, `ota_hash`,
and legacy `hash`) remain accepted silently for compatibility.

### BLE and Related Exports

`meshtastic.interfaces.ble.management_service` remains a facade/re-export path,
so management mappings intentionally appear for both source modules.

| Module                                                | Compatibility symbol                                                | Canonical symbol                                                                                  |
| ----------------------------------------------------- | ------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `meshtastic.interfaces.ble.client`                    | `BLEClient.find_device()`                                           | `BLEClient.findDevice()`                                                                          |
| `meshtastic.interfaces.ble.client`                    | `BLEClient._discover()`                                             | `BLEClient.discover()`                                                                            |
| `meshtastic.interfaces.ble.client`                    | `BLEClient.is_connected()`                                          | `BLEClient.isConnected()`                                                                         |
| `meshtastic.interfaces.ble.client`                    | `BLEClient.stop_notify()`                                           | `BLEClient.stopNotify()`                                                                          |
| `meshtastic.interfaces.ble.client`                    | `BLEClient.async_await()`                                           | `BLEClient._async_await()`                                                                        |
| `meshtastic.interfaces.ble.client`                    | `BLEClient.async_run()`                                             | `BLEClient._async_run()`                                                                          |
| `meshtastic.interfaces.ble.errors`                    | `BLEErrorHandler.safe_execute()`                                    | `BLEErrorHandler._safe_execute()`                                                                 |
| `meshtastic.interfaces.ble.errors`                    | `BLEErrorHandler.safe_cleanup()`                                    | `BLEErrorHandler._safe_cleanup()`                                                                 |
| `meshtastic.interfaces.ble.compatibility_service`     | `BLECompatibilityEventService.publish_connection_status_legacy()`   | `BLECompatibilityEventService.publish_connection_status()`                                        |
| `meshtastic.interfaces.ble.compatibility_service`     | `BLECompatibilityEventPublisher.publish_connection_status_legacy()` | `BLECompatibilityEventPublisher.publish_connection_status()`                                      |
| `meshtastic.ble_interface`                            | `BleakClient`                                                       | `bleak.BleakClient`                                                                               |
| `meshtastic.ble_interface`                            | `BleakScanner`                                                      | `bleak.BleakScanner`                                                                              |
| `meshtastic.ble_interface`                            | `BLEDevice`                                                         | `bleak.backends.device.BLEDevice`                                                                 |
| `meshtastic.ble_interface`                            | `BleakError`                                                        | `bleak.exc.BleakError`                                                                            |
| `meshtastic.ble_interface`                            | `BleakDBusError`                                                    | `bleak.exc.BleakDBusError`                                                                        |
| `meshtastic.interfaces.ble.lifecycle_service`         | `_is_currently_connected_elsewhere`                                 | `meshtastic.interfaces.ble.gating._is_currently_connected_elsewhere`                              |
| `meshtastic.interfaces.ble.lifecycle_service`         | `_ORIGINAL_GET_CONNECTED_CLIENT_STATUS`                             | `meshtastic.interfaces.ble.lifecycle_compat_service._ORIGINAL_GET_CONNECTED_CLIENT_STATUS`        |
| `meshtastic.interfaces.ble.lifecycle_service`         | `_ORIGINAL_GET_CONNECTED_CLIENT_STATUS_LOCKED`                      | `meshtastic.interfaces.ble.lifecycle_compat_service._ORIGINAL_GET_CONNECTED_CLIENT_STATUS_LOCKED` |
| `meshtastic.interfaces.ble.lifecycle_service`         | `_ORIGINAL_VERIFY_OWNERSHIP_SNAPSHOT`                               | `meshtastic.interfaces.ble.lifecycle_compat_service._ORIGINAL_VERIFY_OWNERSHIP_SNAPSHOT`          |
| `meshtastic.interfaces.ble.lifecycle_service`         | `_ORIGINAL_FINALIZE_CONNECTION_GATES`                               | `meshtastic.interfaces.ble.lifecycle_compat_service._ORIGINAL_FINALIZE_CONNECTION_GATES`          |
| `meshtastic.interfaces.ble.lifecycle_service`         | `_ORIGINAL_IS_OWNED_CONNECTED_CLIENT`                               | `meshtastic.interfaces.ble.lifecycle_compat_service._ORIGINAL_IS_OWNED_CONNECTED_CLIENT`          |
| `meshtastic.interfaces.ble.interface`                 | `find_device()`                                                     | `findDevice()`                                                                                    |
| `meshtastic.interfaces.ble.interface`                 | `from_num_handler()`                                                | `BLENotificationDispatcher.from_num_handler()`                                                    |
| `meshtastic.interfaces.ble.interface`                 | `log_radio_handler()`                                               | `BLENotificationDispatcher.log_radio_handler()`                                                   |
| `meshtastic.interfaces.ble.interface`                 | `legacy_log_radio_handler()`                                        | `BLENotificationDispatcher.legacy_log_radio_handler()`                                            |
| `meshtastic.interfaces.ble.interface`                 | `_from_num_handler()`                                               | `BLENotificationDispatcher.from_num_handler()`                                                    |
| `meshtastic.interfaces.ble.interface`                 | `_log_radio_handler()`                                              | `BLENotificationDispatcher.log_radio_handler()`                                                   |
| `meshtastic.interfaces.ble.interface`                 | `_legacy_log_radio_handler()`                                       | `BLENotificationDispatcher.legacy_log_radio_handler()`                                            |
| `meshtastic.interfaces.ble.interface`                 | `_fromnum_notify_enabled`                                           | `BLENotificationDispatcher.fromnum_notify_enabled`                                                |
| `meshtastic.interfaces.ble.interface`                 | `_malformed_notification_count`                                     | `BLENotificationDispatcher.malformed_notification_count`                                          |
| `meshtastic.interfaces.ble.interface`                 | `_malformed_notification_lock`                                      | `BLENotificationDispatcher.malformed_notification_lock`                                           |
| `meshtastic.interfaces.ble.notifications`             | `BLENotificationDispatcher._log_radio_handler()`                    | `BLENotificationDispatcher.log_radio_handler()`                                                   |
| `meshtastic.interfaces.ble.notifications`             | `BLENotificationDispatcher._legacy_log_radio_handler()`             | `BLENotificationDispatcher.legacy_log_radio_handler()`                                            |
| `meshtastic.interfaces.ble.receive_service`           | `BLEReceiveRecoveryService`                                         | `BLEReceiveRecoveryController`                                                                    |
| `meshtastic.interfaces.ble.management_runtime`        | `BLEManagementCommandHandler._start_management_phase()`             | `BLEManagementCommandHandler.start_management_phase()`                                            |
| `meshtastic.interfaces.ble.management_runtime`        | `BLEManagementCommandHandler._resolve_management_target()`          | `BLEManagementCommandHandler.resolve_management_target()`                                         |
| `meshtastic.interfaces.ble.management_runtime`        | `BLEManagementCommandHandler._acquire_client_for_target()`          | `BLEManagementCommandHandler.acquire_client_for_target()`                                         |
| `meshtastic.interfaces.ble.management_runtime`        | `BLEManagementCommandHandler._execute_with_client()`                | `BLEManagementCommandHandler.execute_with_client()`                                               |
| `meshtastic.interfaces.ble.management_service`        | `BLEManagementCommandHandler._start_management_phase()`             | `BLEManagementCommandHandler.start_management_phase()`                                            |
| `meshtastic.interfaces.ble.management_service`        | `BLEManagementCommandHandler._resolve_management_target()`          | `BLEManagementCommandHandler.resolve_management_target()`                                         |
| `meshtastic.interfaces.ble.management_service`        | `BLEManagementCommandHandler._acquire_client_for_target()`          | `BLEManagementCommandHandler.acquire_client_for_target()`                                         |
| `meshtastic.interfaces.ble.management_service`        | `BLEManagementCommandHandler._execute_with_client()`                | `BLEManagementCommandHandler.execute_with_client()`                                               |
| `meshtastic.interfaces.ble.management_compat_service` | `BLEManagementCommandsService._resolve_handler()`                   | `BLEManagementCommandsService._handler_for_shim()`                                                |
| `meshtastic.interfaces.ble.management_compat_service` | `BLEManagementCommandsService._make_handler()`                      | `BLEManagementCommandsService._handler_for_shim()`                                                |
| `meshtastic.interfaces.ble.management_service`        | `BLEManagementCommandsService._resolve_handler()`                   | `BLEManagementCommandsService._handler_for_shim()`                                                |
| `meshtastic.interfaces.ble.management_service`        | `BLEManagementCommandsService._make_handler()`                      | `BLEManagementCommandsService._handler_for_shim()`                                                |
| `meshtastic.interfaces.ble.state`                     | `current_state`                                                     | `_current_state`                                                                                  |
| `meshtastic.interfaces.ble.state`                     | `is_connected`                                                      | `_is_connected`                                                                                   |
| `meshtastic.interfaces.ble.state`                     | `is_closing`                                                        | `_is_closing`                                                                                     |
| `meshtastic.interfaces.ble.state`                     | `can_connect`                                                       | `_can_connect`                                                                                    |
| `meshtastic.interfaces.ble.state`                     | `is_connecting`                                                     | `_is_connecting`                                                                                  |
| `meshtastic.interfaces.ble.state`                     | `is_active`                                                         | `_is_active`                                                                                      |
| `meshtastic.interfaces.ble.state`                     | `transition_to()`                                                   | `_transition_to()`                                                                                |
| `meshtastic.interfaces.ble.state`                     | `reset_to_disconnected()`                                           | `_reset_to_disconnected()`                                                                        |
| `meshtastic.interfaces.ble.state`                     | `_lock` property (internal compatibility alias)                     | `lock` property                                                                                   |

### Powermon and Slog

| Module                                        | Compatibility symbol             | Canonical symbol                                                       |
| --------------------------------------------- | -------------------------------- | ---------------------------------------------------------------------- |
| `meshtastic.powermon.power_supply.PowerMeter` | `get_average_current_mA()`       | `getAverageCurrentMA()`                                                |
| `meshtastic.powermon.power_supply.PowerMeter` | `get_min_current_mA()`           | `getMinCurrentMA()`                                                    |
| `meshtastic.powermon.power_supply.PowerMeter` | `get_max_current_mA()`           | `getMaxCurrentMA()`                                                    |
| `meshtastic.powermon.power_supply.PowerMeter` | `reset_measurements()`           | `resetMeasurements()`                                                  |
| `meshtastic.powermon.ppk2.PPK2PowerSupply`    | `reset_measurements()`           | `resetMeasurements()`                                                  |
| `meshtastic.powermon.riden.RidenPowerSupply`  | `get_average_current_mA()`       | `getAverageCurrentMA()`                                                |
| `meshtastic.powermon.riden.RidenPowerSupply`  | `_getRawWattHour()`              | `_get_raw_watt_hour()`                                                 |
| `meshtastic.powermon.riden.RidenPowerSupply`  | `nowWattHour` attribute          | `now_watt_hour`-style internal value not introduced (legacy preserved) |
| `meshtastic.powermon.sim.SimPowerSupply`      | `get_average_current_mA()`       | `getAverageCurrentMA()`                                                |
| `meshtastic.powermon.stress`                  | `handle_power_stress_response()` | `handlePowerStressResponse()`                                          |
| `meshtastic.powermon.stress`                  | `onPowerStressResponse()`        | `handlePowerStressResponse()`                                          |
| `meshtastic.slog.arrow.ArrowWriter`           | `set_schema()`                   | `setSchema()`                                                          |
| `meshtastic.slog.arrow.ArrowWriter`           | `add_row()`                      | `addRow()`                                                             |
| `meshtastic.slog.slog`                        | `p_meter` property               | `pMeter` property                                                      |
| `meshtastic.slog.slog`                        | `_onLogMessage()`                | `_on_log_message()`                                                    |

Slog schema compatibility fields retained in `PowerLogger` rows and Arrow schema:

- `average_mW` (legacy alias field)
- `max_mW` (legacy alias field)
- `min_mW` (legacy alias field)

Preferred fields for current values are `average_mA`, `max_mA`, and `min_mA`.

### Other Modules

| Module                                        | Compatibility symbol | Canonical symbol  |
| --------------------------------------------- | -------------------- | ----------------- |
| `meshtastic.analysis.__main__`                | `to_pmon_names()`    | `toPmonNames()`   |
| `meshtastic.ota.ESP32WiFiOTA`                 | `hash_bytes()`       | `hashBytes()`     |
| `meshtastic.ota.ESP32WiFiOTA`                 | `hash_hex()`         | `hashHex()`       |
| `meshtastic.remote_hardware`                  | `onGpioReceive()`    | `onGPIOReceive()` |
| `meshtastic.remote_hardware`                  | `onGPIOreceive()`    | `onGPIOReceive()` |
| `meshtastic.supported_device.SupportedDevice` | `usb_ids` property   | `usbIds` property |

## Non-Public and Boundary Rules

- Symbols under `meshtastic/interfaces/ble/*` are internal by default unless
  explicitly exported via:
  - `meshtastic/ble_interface.py`, or
  - `meshtastic/interfaces/ble/__init__.py`.
- Do not add compatibility aliases for internal orchestration helpers
  (`runner`, `policies`, `coordination`, etc.) without explicit approval.
- `ReconnectPolicy` canonical names remain `next_attempt` and
  `get_attempt_count` (snake_case).
- `meshtastic.interfaces.ble.runner.get_zombie_runner_count()` remains
  internal snake_case-only.

## Maintenance Checklist

When adding/changing compatibility behavior:

1. Update or add the code path with explicit `COMPAT_STABLE_SHIM` or
   `COMPAT_DEPRECATE` marker where appropriate.
2. Update this document in the same change.
3. Add/adjust tests for callability and warning behavior.
4. Run compatibility inventory grep and verify changes:
   - `rg -n "COMPAT_STABLE_SHIM|COMPAT_DEPRECATE" meshtastic`
5. Keep `.github/workflows/ci.yml` compatibility validation green:
   - inventory marker check (`rg -n "COMPAT_STABLE_SHIM|COMPAT_DEPRECATE" meshtastic`)
   - compatibility-focused pytest targets for alias callability and warning behavior
6. Run full project checks as documented in `CONTRIBUTING.md`.
7. If a compatibility symbol is listed in both BLE status and module-mapping
   tables, update both entries in the same change to keep inventories aligned.
