"""Pin the Node port as the only reach-through into Node internals.

``node_runtime`` collaborators must reach ``Node`` private attributes only
through ``meshtastic/node_runtime/ports.py``. This pin fails when any other
``node_runtime`` module touches a ``Node`` private, so the port seam cannot
silently regress.
"""

import re
from pathlib import Path

import pytest

_NODE_RUNTIME_DIR = Path(__file__).resolve().parents[1] / "node_runtime"
_NODE_PRIVATE_REACH = re.compile(r"self\._node\._[a-z]")


@pytest.mark.unit
def test_node_runtime_reaches_node_privates_only_through_the_port() -> None:
    """Only meshtastic/node_runtime/ports.py may touch Node private attributes."""
    violations: list[str] = []
    for path in sorted(_NODE_RUNTIME_DIR.rglob("*.py")):
        if path.name == "ports.py":
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if _NODE_PRIVATE_REACH.search(line):
                violations.append(f"{path}: {lineno}: {line.strip()}")
    assert not violations, (
        "node_runtime modules must reach Node internals through "
        "meshtastic/node_runtime/ports.py; violations:\n" + "\n".join(violations)
    )
