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
_REFLECTIVE_ACCESSORS = {"getattr", "hasattr", "setattr", "delattr"}


def _is_node_reference(value: ast.expr) -> bool:
    """Recognize the local node parameter and conventional bound facade."""
    return (isinstance(value, ast.Name) and value.id == "node") or (
        isinstance(value, ast.Attribute)
        and value.attr == "_node"
        and isinstance(value.value, ast.Name)
        and value.value.id == "self"
    )


def _node_private_access(expr: ast.AST) -> str | None:
    """Return a directly or reflectively accessed private Node attribute."""
    if isinstance(expr, ast.Attribute) and _is_node_reference(expr.value):
        return expr.attr if expr.attr.startswith("_") else None
    if (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Name)
        and expr.func.id in _REFLECTIVE_ACCESSORS
        and len(expr.args) >= 2
        and _is_node_reference(expr.args[0])
    ):
        attr = expr.args[1]
        if (
            isinstance(attr, ast.Constant)
            and isinstance(attr.value, str)
            and attr.value.startswith("_")
        ):
            return attr.value
    return None


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("node._timeout", "_timeout"),
        ("getattr(self._node, '_node_db_lock', None)", "_node_db_lock"),
        ("hasattr(node, '_timeout')", "_timeout"),
        ("setattr(node, '_timeout', value)", "_timeout"),
        ("delattr(node, '_timeout')", "_timeout"),
        ("getattr(self._node, 'noProto', False)", None),
        ("getattr(node.iface, '_node_db_lock', None)", None),
    ],
)
@pytest.mark.unit
def test_node_private_access_detection(source: str, expected: str | None) -> None:
    """Pin the detector against the access forms used in the runtime modules."""
    assert _node_private_access(ast.parse(source, mode="eval").body) == expected


@pytest.mark.unit
def test_node_runtime_reaches_node_privates_only_through_the_port() -> None:
    """Only meshtastic/node_runtime/ports.py may touch Node private attributes."""
    violations: list[str] = []
    for path in sorted(_NODE_RUNTIME_DIR.rglob("*.py")):
        if path == _NODE_RUNTIME_DIR / "ports.py":
            continue
        text = path.read_text(encoding="utf-8")
        for expr in ast.walk(ast.parse(text, filename=str(path))):
            if isinstance(expr, ast.expr):
                attr = _node_private_access(expr)
                if attr is not None:
                    violations.append(f"{path}: {expr.lineno}: {attr}")
    assert not violations, (
        "node_runtime modules must reach Node internals through "
        "meshtastic/node_runtime/ports.py; violations:\n" + "\n".join(violations)
    )
