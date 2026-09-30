#!/usr/bin/env python3
"""Statically extract the public API surface from meshtastic source files.

Uses the ast module to parse Python source without importing anything.
Outputs a JSON baseline that can be diffed between branches.

Usage:
    python bin/extract_api_surface.py /path/to/meshtastic-package-dir
    python bin/extract_api_surface.py /path/to/meshtastic \
        --provenance-ref upstream/master --provenance-sha <sha>
"""

import argparse
import ast
import json
from pathlib import Path
from typing import Any

RUNTIME_COMPATIBILITY_MANIFEST = "_runtime_compatibility.json"
_RUNTIME_MODULE_STATUSES = {"INTERNAL_COMPAT"}
_RUNTIME_EXPORT_STATUSES = {"COMPAT_STABLE_SHIM", "COMPAT_DEPRECATE", "INTERNAL_COMPAT"}


def _load_runtime_compatibility_import_paths(pkg_dir: Path) -> list[str]:
    """Return documented runtime compatibility module paths for one source tree.

    Older source trees may predate the manifest; in that case they have no
    manifest-backed runtime import guarantees.
    """
    manifest_path = pkg_dir / RUNTIME_COMPATIBILITY_MANIFEST
    if not manifest_path.exists():
        return []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise ValueError(
            f"Unsupported runtime compatibility manifest schema: {manifest_path}"
        )
    modules = manifest.get("modules")
    if not isinstance(modules, list):
        raise ValueError(
            f"Runtime compatibility manifest modules must be a list: {manifest_path}"
        )
    paths: list[str] = []
    seen_paths: set[str] = set()
    for entry in modules:
        if not isinstance(entry, dict):
            raise ValueError(
                f"Runtime compatibility manifest has an invalid module entry: {entry!r}"
            )
        path = entry.get("path")
        status = entry.get("status")
        purpose = entry.get("purpose")
        exports = entry.get("exports")
        if not isinstance(path, str) or not path:
            raise ValueError(f"Runtime compatibility module path is invalid: {entry!r}")
        if path in seen_paths:
            raise ValueError(f"Duplicate runtime compatibility module path: {path}")
        if status not in _RUNTIME_MODULE_STATUSES:
            allowed_statuses = ", ".join(sorted(_RUNTIME_MODULE_STATUSES))
            raise ValueError(
                "Unsupported runtime compatibility module status for "
                f"{path}: {status!r}; expected one of: {allowed_statuses}"
            )
        if not isinstance(purpose, str) or not purpose.strip():
            raise ValueError(f"Runtime compatibility purpose is required for {path}")
        if not isinstance(exports, dict) or not exports:
            raise ValueError(f"Runtime compatibility exports are required for {path}")
        for export_name, export_status in exports.items():
            if not isinstance(export_name, str) or not export_name:
                raise ValueError(
                    f"Runtime compatibility export name is invalid for {path}: {export_name!r}"
                )
            if export_status not in _RUNTIME_EXPORT_STATUSES:
                allowed_statuses = ", ".join(sorted(_RUNTIME_EXPORT_STATUSES))
                raise ValueError(
                    "Unsupported runtime compatibility export status for "
                    f"{path}.{export_name}: {export_status!r}; "
                    f"expected one of: {allowed_statuses}"
                )
        seen_paths.add(path)
        paths.append(path)
    return sorted(paths)


def _annotation_to_str(node: ast.AST | None) -> str:
    if node is None:
        return ""
    if isinstance(node, ast.Constant):
        return repr(node.value)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_annotation_to_str(node.value)}.{node.attr}"
    if isinstance(node, ast.Subscript):
        return f"{_annotation_to_str(node.value)}[{_annotation_to_str(node.slice)}]"
    if isinstance(node, ast.Tuple):
        return ", ".join(_annotation_to_str(e) for e in node.elts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return f"{_annotation_to_str(node.left)} | {_annotation_to_str(node.right)}"
    if isinstance(node, ast.Starred):
        return f"*{_annotation_to_str(node.value)}"
    if isinstance(node, ast.List):
        return f"[{', '.join(_annotation_to_str(e) for e in node.elts)}]"
    return ast.dump(node)


def _default_to_str(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            return repr(node.value)
        if node.value is None:
            return "None"
        return str(node.value)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _annotation_to_str(node)
    if isinstance(node, ast.List):
        items = [str(_default_to_str(e) or "") for e in node.elts]
        return f"[{', '.join(items)}]"
    if isinstance(node, ast.Tuple):
        items = [str(_default_to_str(e) or "") for e in node.elts]
        return f"({', '.join(items)})"
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return f"-{_default_to_str(node.operand)}"
    if isinstance(node, ast.Call):
        return f"{_annotation_to_str(node.func)}(...)"
    return "..."


def _signature_from_function(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> str:
    params = []

    # Handle positional-only arguments
    for arg in func_node.args.posonlyargs:
        s = arg.arg
        ann = _annotation_to_str(arg.annotation) if arg.annotation else None
        if ann:
            s += f":{ann}"
        params.append(s)

    # Add "/" separator if positional-only args exist
    if func_node.args.posonlyargs:
        params.append("/")

    # Handle regular positional arguments
    for arg in func_node.args.args:
        s = arg.arg
        ann = _annotation_to_str(arg.annotation) if arg.annotation else None
        if ann:
            s += f":{ann}"
        params.append(s)

    # Calculate defaults - applies to last N args of combined posonlyargs + args
    posonlyargs = func_node.args.posonlyargs
    args = func_node.args.args
    defaults = func_node.args.defaults
    n_posonly = len(posonlyargs)
    n_args = len(args)
    n_defaults = len(defaults)
    has_posonly = bool(posonlyargs)

    # Defaults apply to the last n_defaults of combined (posonlyargs + args)
    # First apply defaults to posonlyargs (if any), then to args
    n_posonly_defaults = max(0, n_defaults - n_args)
    n_args_defaults = n_defaults - n_posonly_defaults

    # Apply defaults to posonlyargs
    for i in range(n_posonly_defaults):
        default_idx = i
        arg_idx = n_posonly - n_posonly_defaults + i
        dv = _default_to_str(defaults[default_idx])
        if dv is not None:
            params[arg_idx] += f"={dv}"

    # Apply defaults to regular args
    slash_offset = 1 if has_posonly else 0
    for i in range(n_args_defaults):
        default_idx = n_posonly_defaults + i
        arg_idx = n_posonly + slash_offset + n_args - n_args_defaults + i
        dv = _default_to_str(defaults[default_idx])
        if dv is not None:
            params[arg_idx] += f"={dv}"

    if func_node.args.vararg:
        params.append(f"*{func_node.args.vararg.arg}")
    if func_node.args.kwonlyargs:
        if not func_node.args.vararg:
            params.append("*")
        for kw_arg, kw_default in zip(
            func_node.args.kwonlyargs, func_node.args.kw_defaults, strict=False
        ):
            s = kw_arg.arg
            ann = _annotation_to_str(kw_arg.annotation) if kw_arg.annotation else None
            if ann:
                s += f":{ann}"
            dv = _default_to_str(kw_default)
            if dv is not None:
                s += f"={dv}"
            params.append(s)
    if func_node.args.kwarg:
        params.append(f"**{func_node.args.kwarg.arg}")

    return f"({', '.join(params)})"


def _extract_class_methods(tree: ast.AST, class_name: str) -> dict[str, str]:
    methods = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if item.name.startswith("_"):
                        continue
                    methods[item.name] = _signature_from_function(item)
    return methods


def _find_source_file(pkg_dir: Path, module_name: str) -> Path | None:
    p = pkg_dir / f"{module_name}.py"
    if p.exists():
        return p
    p = pkg_dir / module_name / "__init__.py"
    if p.exists():
        return p
    return None


def _get_top_level_exports(pkg_dir: Path) -> list[str]:
    init_path = pkg_dir / "__init__.py"
    if not init_path.exists():
        return []
    tree = ast.parse(init_path.read_text(encoding="utf-8"))

    # Check for __all__ first - if defined, it controls the public API
    # (including lazy __getattr__ aliases, which are skipped below).
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    if isinstance(node.value, (ast.List, ast.Tuple)):
                        all_exports = [
                            elt.value
                            for elt in node.value.elts
                            if isinstance(elt, ast.Constant)
                            and isinstance(elt.value, str)
                        ]
                        return sorted(all_exports)

    exports = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    exports.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if not node.target.id.startswith("_"):
                exports.add(node.target.id)
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            exports.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                exports.add(node.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and not node.module.startswith("_"):
                for alias in node.names:
                    name = alias.asname if alias.asname else alias.name
                    if not name.startswith("_"):
                        exports.add(name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname if alias.asname else alias.name.split(".", 1)[0]
                if not name.startswith("_"):
                    exports.add(name)

    # Lazy compatibility aliases served by a module-level __getattr__ (such as
    # meshtastic.serial) are part of the import surface; capture them without
    # importing anything.
    exports.update(_get_lazy_getattr_exports(tree))

    # Historically importable top-level meshtastic modules/subpackages.
    # Keep this compatibility surface explicit and stable for baseline checks.
    # Only add names that are present on disk.
    historical_exports = {
        "analysis",
        "host_port",
        "interfaces",
        "mesh_interface",
        "mesh_interface_runtime",
        "mt_config",
        "node",
        "node_runtime",
        "ota",
        "powermon",
        "protobuf",
        "remote_hardware",
        "serial_interface",
        "slog",
        "stream_interface",
        "supported_device",
        "tcp_interface",
        "tunnel",
        "util",
        "version",
    }
    for name in historical_exports:
        # Check if module file exists on disk
        if _find_source_file(pkg_dir, name) is not None:
            exports.add(name)

    return sorted(exports)


def _get_lazy_getattr_exports(tree: ast.AST) -> set[str]:
    """Return attribute names served by a module-level ``__getattr__``.

    Lazy compatibility aliases (for example ``meshtastic.serial``) are provided
    by a module-level ``__getattr__`` instead of an eager import, so import
    scanning alone cannot see them. Capture the literal attribute names the
    function recognizes without importing anything. Only ``name == "<literal>"``
    comparisons are recognized; dynamic membership or lookup-table lazy
    resolvers are not discovered.
    """
    lazy_names: set[str] = set()
    for node in ast.iter_child_nodes(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "__getattr__"):
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.If):
                continue
            condition = child.test
            if (
                not isinstance(condition, ast.Compare)
                or len(condition.ops) != 1
                or not isinstance(condition.ops[0], ast.Eq)
                or len(condition.comparators) != 1
            ):
                continue
            left, right = condition.left, condition.comparators[0]
            for side, other in ((left, right), (right, left)):
                if (
                    isinstance(side, ast.Name)
                    and side.id == "name"
                    and isinstance(other, ast.Constant)
                    and isinstance(other.value, str)
                    and not other.value.startswith("_")
                ):
                    lazy_names.add(other.value)
    return lazy_names


def _camel_to_snake(name: str) -> str:
    """Convert camelCase or PascalCase to snake_case.

    Examples
    --------
        "BLEInterface" -> "ble_interface"
        "SerialInterface" -> "serial_interface"
        "MeshInterface" -> "mesh_interface"
    """
    result = []
    for i, char in enumerate(name):
        if char.isupper():
            if i > 0 and (
                name[i - 1].islower() or (i + 1 < len(name) and name[i + 1].islower())
            ):
                result.append("_")
            result.append(char.lower())
        else:
            result.append(char)
    return "".join(result)


def _module_path_exists(pkg_dir: Path, dotted_path: str) -> bool:
    """Return whether dotted module path exists under pkg_dir."""
    if not dotted_path.startswith("meshtastic."):
        return False
    relative_parts = dotted_path.split(".")[1:]
    module_py = pkg_dir.joinpath(*relative_parts).with_suffix(".py")
    if module_py.exists():
        return True
    module_init = pkg_dir.joinpath(*relative_parts, "__init__.py")
    return module_init.exists()


def _capture_legacy_import_paths(pkg_dir: Path) -> list[str]:
    """Capture manifest-backed runtime compatibility paths present in the tree."""
    documented_paths = _load_runtime_compatibility_import_paths(pkg_dir)
    return [path for path in documented_paths if _module_path_exists(pkg_dir, path)]


def extract_api_surface(
    pkg_dir: str | Path, classes: list[str] | None = None
) -> dict[str, Any]:
    pkg_dir = Path(pkg_dir)
    if classes is None:
        classes = ["MeshInterface", "Node"]

    module_map = {}
    for cls in classes:
        # Derive module name from class name using snake_case convention
        module_name = _camel_to_snake(cls)
        src = _find_source_file(pkg_dir, module_name)
        if src is None:
            continue
        if src in module_map:
            continue
        tree = ast.parse(src.read_text(encoding="utf-8"))
        methods = _extract_class_methods(tree, cls)
        if methods:
            module_map[src] = (tree, module_name)

    result = {
        "node_methods": {},
        "mesh_interface_methods": {},
        "top_level_exports": _get_top_level_exports(pkg_dir),
        "legacy_import_paths": _capture_legacy_import_paths(pkg_dir),
    }

    for cls in classes:
        if cls == "MeshInterface":
            key = "mesh_interface_methods"
        elif cls == "Node":
            key = "node_methods"
        else:
            module_name = _camel_to_snake(cls)
            key = f"{module_name}_methods"
        for tree, _mod in module_map.values():
            methods = _extract_class_methods(tree, cls)
            if methods:
                result[key] = dict(sorted(methods.items()))
                break

    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Statically extract the public API surface of a meshtastic package tree."
    )
    parser.add_argument("pkg_dir", help="Path to the meshtastic package directory")
    parser.add_argument(
        "--provenance-ref",
        help=(
            "Owning ref label for committed baselines (e.g. upstream/master). "
            "Requires --provenance-sha."
        ),
    )
    parser.add_argument(
        "--provenance-sha",
        help="Resolved full commit SHA of the extracted source tree. Requires --provenance-ref.",
    )
    args = parser.parse_args()
    if (args.provenance_ref is None) != (args.provenance_sha is None):
        parser.error("--provenance-ref and --provenance-sha must be passed together")

    surface = extract_api_surface(args.pkg_dir)
    if args.provenance_ref is not None and args.provenance_sha is not None:
        # Provenance records which source tree a committed baseline snapshot
        # came from. It is metadata only: the comparator reads just the
        # surface keys, so this never affects API-diff semantics.
        surface["provenance"] = {
            "ref": args.provenance_ref,
            "sha": args.provenance_sha,
        }
    print(json.dumps(surface, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
