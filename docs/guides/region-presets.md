# Firmware-declared LoRa region/preset capabilities

Firmware can provide a `LoRaRegionPresetMap` describing which modem presets it
considers compatible with each LoRa region. `mtjk` retains both the raw protobuf and a
small immutable lookup intended for applications and CLI presentation.

The central rule is deliberately conservative:

> Missing, unknown, or malformed capability metadata means **no additional
> client-side constraint**.

The firmware remains the source of truth for whether a configuration is accepted. The
Python client must not turn absent capability data into a false prohibition that older
firmware never advertised.

## Wire model

The protobuf is grouped to avoid repeating identical preset lists for multiple regions.
It contains:

- `groups`: `LoRaPresetGroup` entries, each with
  - a repeated list of modem presets;
  - a default preset; and
  - a `licensed_only` flag;
- `region_groups`: entries mapping a region enum value to one group index.

The client flattens that representation with
`meshtastic.region_presets.decode_region_preset_map()`.

## `RegionPresetInfo`

Each usable region is represented as the frozen dataclass:

```python
RegionPresetInfo(
    presets: tuple[int, ...],
    default_preset: int,
    licensed_only: bool,
)
```

The values are protobuf enum integers. Keeping them as integers preserves unknown
future enum values instead of forcing the client to know every firmware enum name in
advance.

`presets` is a tuple and the outer mapping is a `MappingProxyType`, so the decoded
capability view is immutable to callers.

## Decoding rules

`decode_region_preset_map()` converts the grouped protobuf into a mapping from region
integer to `RegionPresetInfo`.

The decoder applies these rules:

1. a region's `group_index` must refer to an existing group;
2. duplicate preset values inside a group are removed while preserving their first
   occurrence order;
3. the resulting preset list must not be empty;
4. the group's default preset must also appear in that preset list;
5. malformed entries are skipped rather than converted into a restrictive empty
   capability; and
6. if the protobuf lists the same region more than once, the later usable entry wins.

The last rule follows the ordinary assignment semantics of the flattened mapping and is
covered by tests. It allows the client to represent the wire payload deterministically
without inventing merge semantics not present in the protobuf.

## Interface state

Every `MeshInterface` starts with:

- `regionPresetMap = None`; and
- `regionPresets` as an empty immutable mapping.

When `FromRadio.region_presets` is received, the receive pipeline:

1. creates a defensive copy of the raw `LoRaRegionPresetMap`;
2. decodes the copy into the immutable lookup;
3. stores both values on the interface; and
4. publishes `meshtastic.region_presets` with both the decoded view and raw copy.

The stored `regionPresetMap` is not the protobuf object owned by the receive message.
Consumers can inspect it without sharing mutable ownership with the receive pipeline.

An empty decoded map does not necessarily mean the firmware sent nothing: it can also
mean that every declared entry was unusable under the validation rules above. Code
that needs to distinguish "no message received" from "message received but no usable
entries" can inspect `regionPresetMap is None` separately from `regionPresets`.

## Public lookup methods

`MeshInterface` exposes both historical camelCase and snake_case aliases:

- `getRegionPresetInfo(region)` / `get_region_preset_info(region)`;
- `getAllowedModemPresets(region)` / `get_allowed_modem_presets(region)`.

### `getRegionPresetInfo`

The region argument may be:

- an integer protobuf region value;
- a string that can be converted to that integer; or
- `None` / an invalid value.

Invalid or unknown values return `None` rather than raising a conversion error.

A returned `RegionPresetInfo` means the firmware supplied usable capability data for
that region.

### `getAllowedModemPresets`

This returns the immutable preset tuple when the region has usable metadata.

It returns `None` when the client has no usable capability information for that region.
`None` is important: it means **unconstrained by this client-side metadata**, not "no
preset is legal."

Do not change this to an empty tuple for unknown regions; that would reverse the
fallback policy and could break configuration against older firmware.

## Advisory, not automatic enforcement

At present the region/preset API exposes firmware-declared capabilities for consumers
and CLI display. The core setter path does not automatically reject every LoRa write
that falls outside `regionPresets`.

That is intentional. Capability metadata may be absent, malformed, newer than the
client's enum definitions, or not provided by older firmware. Firmware remains the
final authority when a configuration is written.

Applications may use the map to improve their own UI or validation, but should retain
a safe fallback when no capability entry is available.

## Default preset handling

A usable region entry always has a default that is also present in its declared preset
list.

Applications changing regions can use `default_preset` as the firmware-declared choice
when the current modem preset is not valid for the new region. The Python client does
not silently rewrite a caller's LoRa configuration simply because capability metadata
was received.

After LoRa writes, consumers that depend on the effective region should re-read or
observe refreshed configuration rather than assuming the requested region value is
necessarily the final firmware value. Firmware may apply its own region-selection
rules, including closely related regional variants.

## Licensed-only groups

`licensed_only=True` is preserved in `RegionPresetInfo` and shown by the CLI.

It is capability metadata, not a client-side licensing mechanism. The Python library
surfaces the firmware declaration so callers can make an informed choice; it does not
attempt to decide whether a particular operator is licensed.

## CLI display

`--show-region-presets` prints the local node's usable capability map and then closes
the connected CLI session.

The command is local-node only. Supplying a remote destination reports that the
capabilities are available only from the local node.

If no usable decoded entries are available, the CLI reports that the firmware did not
provide usable region/preset compatibility metadata and that preset choices remain
unconstrained.

For each usable entry the CLI displays:

- the region enum name;
- the default modem preset;
- `licensed-only` when applicable; and
- the declared modem preset list.

Unknown future region or preset enum integers are rendered with numeric fallbacks such
as `REGION_<n>` and `PRESET_<n>` instead of failing. This is deliberate forward-
compatibility behavior.

## Pubsub integration

Subscribers can listen for:

```text
meshtastic.region_presets
```

The publication includes:

- `region_presets`: the immutable decoded mapping; and
- `raw`: the defensive `LoRaRegionPresetMap` copy.

As with other Meshtastic pubsub topics, the interface is supplied by the publication
pipeline so consumers can distinguish multiple active interfaces.

Use the decoded mapping for ordinary application logic and the raw protobuf when a
consumer needs fields that are not represented by `RegionPresetInfo`.

## Compatibility expectations

Region/preset capability support is additive. Older firmware that never sends
`FromRadio.region_presets` continues to behave as before because:

- `regionPresetMap` remains `None`;
- `regionPresets` remains empty; and
- lookup helpers return `None` rather than imposing restrictions.

The camelCase methods remain available for existing users, with snake_case aliases
provided alongside them.

Unknown enum values should remain representable as integers. Do not require a current
Python enum name before storing or displaying a firmware-provided capability.

See [COMPATIBILITY.md](../compatibility.md) for the repository-wide naming and
replacement-compatibility policy.

## Adding or changing capability behavior

When extending this subsystem:

- keep the raw protobuf as a defensive copy;
- keep the decoded public view immutable;
- prefer skipping malformed entries over creating false restrictions;
- preserve unknown enum integers;
- distinguish "no usable metadata" from "empty legal set";
- do not make optional firmware metadata mandatory for old firmware;
- update the CLI fallback rendering for any new displayed enum values; and
- add tests for malformed groups, default membership, duplicate presets, duplicate
  region entries, unknown enum integers, and receive publication.

If client-side write enforcement is ever introduced, it should be a separately reviewed
behavioral change with an explicit compatibility fallback for absent metadata.

## Primary tests

Focused regression coverage lives in:

- `meshtastic/tests/test_region_presets.py`;
- the `FromRadio.region_presets` receive tests in
  `meshtastic/tests/test_receive_pipeline.py`;
- `meshtastic/tests/test_main_firmware_28.py` for CLI presentation and local-only
  behavior; and
- `meshtastic/tests/test_cli_channel_contact_edge_cases.py` for unknown enum and
  formatting behavior.
