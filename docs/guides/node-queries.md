# Cached node queries

## Python API

`MeshInterface.queryNodes()` returns detached node records with the same filters
and sorting as the node table, without printing or sending radio requests:

```python
result = interface.queryNodes(
    includeSelf=False,
    roleFilter=["client"],
    hwModelFilter=["rak"],
    sortField="last_seen",
    limit=20,
)
for node in result.nodes:
    print(node["num"], node.get("user", {}).get("longName"))
print(result.returned, result.matched, result.total)
```

The records are copies, including nested dictionaries and protobufs. Changes to
one result cannot change the client cache, and receive updates cannot change a
captured result. A zero limit returns every match. Filters accept alternatives
within each field and combine role and hardware filters with AND. Unknown sort
fields and invalid query options raise `ValueError`.

For machine-readable command output, use `mtjk --nodes --json`, optionally with
`--role`, `--hwmodel`, `--sort`, and `--limit`. It emits one document containing
`schema_version: 1`, `captured_at` (Unix seconds), `total` (after self selection),
`matched` (before limiting), `returned`, `truncated`, and `nodes`. Node keys retain
the cache's camelCase spelling; measurements and timestamps remain numeric.
`result.toDict()` produces the same document in Python. Bytes use `base64:` plus
Base64, protobuf values use protobuf JSON conventions, and non-finite numbers
become null. Internal packet payloads and administrative session keys are omitted
from JSON. Other actions and table-only `--show-fields` cannot be combined with
`--nodes --json`.
