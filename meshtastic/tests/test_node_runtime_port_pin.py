"""Pin the Node port as the only reach-through into Node internals.

``node_runtime`` collaborators must reach ``Node`` private attributes only
through ``meshtastic/node_runtime/ports.py``. This pin fails when any other
``node_runtime`` module touches a ``Node`` private, so the port seam cannot
silently regress.
"""

import ast
from pathlib import Path

import pytest

_NODE_RUNTIME_DIR = Path(__file__).resolve().parents[1] / "node_runtime"


def _is_node_reference(value: ast.expr) -> bool:
    """Recognize the local node parameter and conventional bound facade."""
    return (isinstance(value, ast.Name) and value.id == "node") or (
        isinstance(value, ast.Attribute)
        and value.attr == "_node"
        and isinstance(value.value, ast.Name)
        and value.value.id == "self"
    )


@pytest.mark.unit
def test_node_runtime_reaches_node_privates_only_through_the_port() -> None:
    """Only meshtastic/node_runtime/ports.py may touch Node private attributes."""
    violations: list[str] = []
    for path in sorted(_NODE_RUNTIME_DIR.rglob("*.py")):
        if path.name == "ports.py":
            continue
        text = path.read_text(encoding="utf-8")
        for expr in ast.walk(ast.parse(text, filename=str(path))):
            if (
                isinstance(expr, ast.Attribute)
                and expr.attr.startswith("_")
                and _is_node_reference(expr.value)
            ):
                violations.append(f"{path}: {expr.lineno}: {expr.attr}")
    assert not violations, (
        "node_runtime modules must reach Node internals through "
        "meshtastic/node_runtime/ports.py; violations:\n" + "\n".join(violations)
    )
