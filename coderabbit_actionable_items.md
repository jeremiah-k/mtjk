<!-- trunk-ignore-all -->
<!-- markdownlint-disable -->

# CodeRabbit IDE/Linting Comments - Actionable Items

> Total items: 3
> Generated: 2026-10-10T16:32:16.769783

## Summary by Severity

| Severity | Count |
| -------- | ----- |
| Minor    | 3     |

## MINOR (3 items)

### `meshtastic/admin_response.py:95`

**Type:** actionable

**Make the feedback matcher public, or rename it.**

`_NodeAdminTransportRuntime` in `meshtastic/node_runtime/transport_runtime/admin.py` binds `contract._matches_feedback` from another module. The tests also call it directly. That makes this method part of the contract's cross-module API. Rename it to `matches_feedback`, which matches `matches_source` and `matches`.

---

### `meshtastic/_command_scope.py:111`

**Type:** actionable

**Remove the dead `None` check.**

Lines 102-103 already return when `sent is None`. The check `sent.to if sent is not None else None` can therefore never take the `None` branch. Use `nodeNum=sent.to`.

---

### `docs/internals/admin-response-contracts.md:109`

**Type:** actionable

**Update the routing classification text later in this file.**

Lines 199-202 say a routing ACK means `errorReason` is absent or `NONE`. The runtime now also treats `0` and `None` as ACKs. Add `0` and `None` to that sentence so it matches `correlate_inbound_response`.

---
