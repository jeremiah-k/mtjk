# ADMIN_APP response correlation contracts

This document describes how `mtjk` correlates administrative getter responses to
outgoing `ADMIN_APP` requests. It is a maintenance contract for the Python client,
not a description of every firmware-side admin behavior.

The short version is:

- a response must carry the request ID of the pending request;
- typed admin getters also bind the response to the expected source node and
  response oneof;
- config/module getters additionally bind the expected response subtype;
- a wrong-source or wrong-shape packet does **not** consume the pending callback;
- routing ACK/NAK handling remains separate from the typed data response; and
- managed callbacks are bounded so abandoned requests cannot accumulate forever.

These rules exist because request IDs alone are not sufficient to identify the
response an administrative getter is waiting for, especially when multiple nodes or
multiple admin request types are active concurrently.

## Layers involved

The relevant implementation is intentionally split across a few components:

- `meshtastic.admin_response`
  defines the typed request-to-response contracts;
- `meshtastic.node_runtime.transport_runtime.admin`
  builds and sends `ADMIN_APP` messages and attaches a response matcher when one is
  available;
- `meshtastic.mesh_interface_runtime.request_wait`
  owns response-handler registration, matching, cleanup, ACK/NAK interaction, and
  stale-handler pruning;
- public `Node` methods build the request and decide whether they need an ACK wait,
  a data response, or both.

The public `MeshInterface.responseHandlers` compatibility surface still stores the
historical two-field `ResponseHandler(callback, ackPermitted)` tuple. Typed response
matchers and lifecycle metadata are deliberately kept in internal side tables so
adding stronger correlation did not change that public tuple shape.

## Typed request contracts

`contract_for_admin_request()` inspects the outgoing `AdminMessage` oneof and, for
known getter requests, returns an immutable `AdminResponseContract` with:

- `expected_sources`: acceptable `from` node numbers;
- `response_variant`: the expected `AdminMessage` response oneof field; and
- `response_subtype`: the expected nested config/module subtype when applicable.

If a request is not a recognized getter, no typed contract is attached and the
historical request-ID callback behavior remains available.

### Request/response mapping

The current named mappings are:

| Request field                                | Expected response field                       |
| -------------------------------------------- | --------------------------------------------- |
| `get_channel_request`                        | `get_channel_response`                        |
| `get_owner_request`                          | `get_owner_response`                          |
| `get_config_request`                         | `get_config_response`                         |
| `get_module_config_request`                  | `get_module_config_response`                  |
| `get_canned_message_module_messages_request` | `get_canned_message_module_messages_response` |
| `get_device_metadata_request`                | `get_device_metadata_response`                |
| `get_ringtone_request`                       | `get_ringtone_response`                       |
| `get_device_connection_status_request`       | `get_device_connection_status_response`       |
| `get_node_remote_hardware_pins_request`      | `get_node_remote_hardware_pins_response`      |
| `get_ui_config_request`                      | `get_ui_config_response`                      |

The mapping is explicit by protobuf field name. Descriptor field order is not used as
an enum-to-field mapping because descriptor ordering is not a compatibility contract.

### Config and module subtype matching

`get_config_request` and `get_module_config_request` need one more level of
correlation because many request types share the same outer response field.

For example, a request for LoRa config accepts only:

```text
AdminMessage.get_config_response.lora
```

A `get_config_response.position` packet with the same request ID is not the response
to that request and must leave the callback pending.

The same rule applies to module configuration. Named enum values are mapped to named
nested response fields such as `mqtt`, `telemetry`, `remote_hardware`,
`statusmessage`, `traffic_management`, `mesh_beacon`, and the other supported module
config variants.

## Source-node matching

For a remote request, the expected source is the requested destination node.

For a request to the local node, the client accepts either:

- the local node number; or
- source node `0`.

The local `0` exception preserves historical PhoneAPI behavior seen across firmware
and transport generations. It is intentionally limited to a request whose
destination is the local node.

A packet from any other source does not satisfy the contract even if its request ID
and response oneof happen to match.

## Correlation sequence

For a typed administrative getter, the normal sequence is:

1. the `Node` builds an `AdminMessage` getter;
2. admin transport copies the message and adds a cached admin session passkey when
   one is available as bytes;
3. the client builds a typed response contract for the request;
4. `sendData()` / `_send_data_with_wait()` registers the callback under the packet
   request ID and stores the matcher internally;
5. an inbound packet is decoded and its request ID is extracted;
6. the request-wait runtime finds the registered callback for that ID;
7. non-ACK packets are checked against the typed matcher;
8. only a matching packet consumes the handler and invokes the callback.

A response matcher is applied only after the request ID identifies a pending handler.
The matcher is therefore an additional constraint, not a replacement for request-ID
correlation.

## Wrong or malformed responses

A non-ACK packet does not consume the typed response handler when:

- it comes from the wrong source;
- it carries the wrong admin response oneof;
- it carries the wrong config/module subtype; or
- the matcher itself fails with an exception.

Matcher exceptions are logged and treated as non-matches. Keeping the handler pending
is important: a malformed or unrelated packet must not steal the request slot from a
later valid response.

Admin decode failures are handled separately by the receive pipeline. A decoded
admin failure is not passed to an ordinary data-response callback as if it were the
requested protobuf payload.

## Routing ACK/NAK is not the getter payload

The send path may request routing acknowledgment, but routing status and the
administrative data response have different responsibilities.

For the newer bounded getter methods such as:

- `Node.requestDeviceConnectionStatus()`;
- `Node.requestUiConfig()`; and
- `Node.requestNodeRemoteHardwarePins()`;

completion is decided by the correlated data response. These methods send with
`wantResponse=True` and deliberately do not open a separate request-scoped ACK wait.
A routing ACK is never converted into or returned as the requested response object.

The helper waits on a bounded event for the named response field and returns:

- a defensive protobuf copy when the response arrives;
- `None` when the send is skipped or the bounded response wait expires.

A non-positive response timeout is rejected with `ValueError`.

Other admin operations still use request-scoped ACK/NAK waits when their contract is
about successful mutation rather than retrieving a typed response. Do not conflate
these two patterns when adding new operations.

## Response-handler lifetime

Managed response callbacks are not permanent registrations.

The request-wait runtime tracks registration time independently of the public
`ResponseHandler` tuple. Stale managed callbacks are pruned after the configured
response-handler TTL unless the request ID is still part of an active scoped wait.
The current default TTL for generic managed response callbacks is one hour.

Closing/clearing an interface removes managed response-handler metadata along with the
public response-handler entries.

This lifetime bound is a safety net for asynchronous callers that register a callback
but never receive a response. Short synchronous getter methods should normally finish
through their own explicit timeout well before this pruning horizon.

## Admin session passkeys

Before sending an ordinary encrypted admin operation, the transport checks the target
node record for `adminSessionPassKey`.

- A bytes value is copied into `AdminMessage.session_passkey`.
- A non-bytes value is ignored with a warning.

The admin transport copies the caller's protobuf before adding the passkey so the
caller's message object is not mutated as a side effect of sending it.

This is separate from the USB lockdown flow documented in [LOCKDOWN.md](LOCKDOWN.md),
which intentionally uses a different local, unencrypted transport contract.

## Adding a new administrative getter

When firmware adds a request/response getter and the Python client exposes it, keep the
following sequence intact:

1. add the request-to-response mapping in `meshtastic.admin_response`;
2. if the response is multiplexed under a config/module oneof, add the explicit named
   subtype mapping;
3. use `wantResponse=True` with an `onResponse` callback;
4. return or cache a defensive protobuf copy rather than retaining a mutable view into
   the receive object;
5. choose deliberately whether the API waits for a data response, an ACK/NAK, or both;
6. add tests for the correct source/variant/subtype;
7. add tests showing wrong-source and wrong-shape packets do not consume the handler;
8. test timeout and skipped-send behavior for synchronous getters.

Do not infer response fields from protobuf descriptor order, and do not broaden source
matching merely to make a test packet pass.

## Compatibility expectations

The correlation machinery is stricter than older request-ID-only behavior for the
admin getters that have a typed contract. That strictness is intentional because it
prevents an unrelated administrative packet from completing the wrong request.

At the same time, the compatibility boundary is preserved where practical:

- `ResponseHandler` remains the historical two-field tuple;
- generic `onResponse` callbacks remain supported;
- ACK-permitted callbacks retain their existing behavior;
- matcher metadata is internal; and
- unknown/unmapped admin requests fall back to ordinary request-ID correlation rather
  than becoming unusable.

See the repository-level [COMPATIBILITY.md](COMPATIBILITY.md) for the broader
compatibility policy.

## Primary tests

The most focused regression coverage lives in:

- `meshtastic/tests/test_admin_response_contracts.py`;
- `meshtastic/tests/test_admin_ack_wait_scoping.py`;
- `meshtastic/tests/test_response_handler_compat.py`;
- `meshtastic/tests/test_cli_admin_utility_actions.py`; and
- the request/response sections of `meshtastic/tests/test_node_runtime_response.py`.

Changes to response correlation should be reviewed as concurrency and compatibility
changes, not only as protobuf plumbing.
