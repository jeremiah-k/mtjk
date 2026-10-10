#!/usr/bin/env python3
"""Compare two API surface JSON files and report differences.

Used by CI to detect breaking API changes between a baseline ref and PR branch.
Exits 0 if no removed methods/exports, exits 1 if breaking changes found.
Only the surface keys are compared; unrelated metadata a baseline embeds
(such as provenance written by bin/generate_master_api_baseline.sh) is ignored.

Usage:
    python3 bin/compare_api_surfaces.py base_surface.json pr_surface.json
    python3 bin/compare_api_surfaces.py base_surface.json pr_surface.json --base-label master
"""

import argparse
import json
import os
import re
import sys
from typing import Any

# Approved signature equivalences - these are NOT breaking changes
# Maps method names to lists of equivalent parameter name aliases
APPROVED_SIGNATURE_ALIASES: dict[str, dict[str, dict[str, str]]] = {
    "Node": {
        "startOTA": {
            # PR name -> baseline name (new keyword-only aliases for positional params)
            "mode": "ota_mode",
            "ota_hash": "ota_file_hash",
        }
    }
}

MAX_WARNING_PREVIEW_ITEMS = 6


def _escape_github_command_value(value: str) -> str:
    """Escape text for GitHub workflow command payloads."""
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _emit_github_annotation(level: str, message: str) -> None:
    """Emit a GitHub Actions annotation when running in Actions."""
    if os.getenv("GITHUB_ACTIONS") == "true":
        print(f"::{level}::{_escape_github_command_value(message)}")


def _get_param_aliases(class_name: str, method_name: str) -> dict[str, str]:
    """Get approved parameter aliases for a given class.method.

    Returns a dict mapping new parameter names to their equivalent old names.
    """
    if class_name in APPROVED_SIGNATURE_ALIASES:
        if method_name in APPROVED_SIGNATURE_ALIASES[class_name]:
            return APPROVED_SIGNATURE_ALIASES[class_name][method_name]
    return {}


def _normalize_type(type_str: str) -> str:
    type_str = re.sub(r"\bUnion\[([^\[\]]+),\s*None\]", r"Optional[\1]", type_str)
    type_str = re.sub(r"\bOptional\[([^\[\]]+)\]", r"\1 | None", type_str)
    type_str = re.sub(r"typing\.", "", type_str)
    return type_str


def _find_top_level_char(text: str, target: str) -> int:
    """Return index of target at top level, or -1 if absent."""
    depth_paren = 0
    depth_bracket = 0
    depth_brace = 0
    for index, char in enumerate(text):
        if char == "(":
            depth_paren += 1
        elif char == ")":
            depth_paren -= 1
        elif char == "[":
            depth_bracket += 1
        elif char == "]":
            depth_bracket -= 1
        elif char == "{":
            depth_brace += 1
        elif char == "}":
            depth_brace -= 1
        elif (
            char == target
            and depth_paren == 0
            and depth_bracket == 0
            and depth_brace == 0
        ):
            return index
    return -1


def _split_top_level_params(sig: str) -> list[str]:
    """Split `(a, b: X)` into top-level parameter tokens."""
    trimmed = sig.strip()
    if trimmed.startswith("(") and trimmed.endswith(")"):
        trimmed = trimmed[1:-1]

    if not trimmed:
        return []

    params: list[str] = []
    start = 0
    depth_paren = 0
    depth_bracket = 0
    depth_brace = 0

    for index, char in enumerate(trimmed):
        if char == "(":
            depth_paren += 1
        elif char == ")":
            depth_paren -= 1
        elif char == "[":
            depth_bracket += 1
        elif char == "]":
            depth_bracket -= 1
        elif char == "{":
            depth_brace += 1
        elif char == "}":
            depth_brace -= 1
        elif (
            char == "," and depth_paren == 0 and depth_bracket == 0 and depth_brace == 0
        ):
            params.append(trimmed[start:index].strip())
            start = index + 1

    params.append(trimmed[start:].strip())
    return params


def _normalize_param(param: str) -> str:
    """Normalize a single parameter token while preserving defaults."""
    if param in {"", "*", "/"}:
        return param

    colon_index = _find_top_level_char(param, ":")
    if colon_index == -1:
        return param

    name_part = param[:colon_index].strip()
    rest = param[colon_index + 1 :].strip()

    equals_index = _find_top_level_char(rest, "=")
    if equals_index == -1:
        type_part = rest
        default_part = None
    else:
        type_part = rest[:equals_index].strip()
        default_part = rest[equals_index + 1 :].strip()

    normalized = f"{name_part}: {_normalize_type(type_part)}"
    if default_part is not None:
        normalized += f"={default_part}"
    return normalized


def _normalize_sig(sig: str) -> str:
    """Normalize signature with top-level parameter parsing."""
    params = _split_top_level_params(sig)
    normalized = [_normalize_param(param) for param in params]
    return f"({', '.join(normalized)})"


def _parse_signature_shape(sig: str) -> list[dict[str, Any]]:
    """Parse signature into comparable parameter-shape records.

    Ignores annotation text and default values, but keeps whether a parameter
    is required and its call kind.
    """
    tokens = _split_top_level_params(sig)
    slash_index = tokens.index("/") if "/" in tokens else -1

    params: list[dict[str, Any]] = []
    kw_only_mode = False

    for index, token in enumerate(tokens):
        if not token:
            continue
        if token == "/":
            continue
        if token == "*":
            kw_only_mode = True
            continue

        raw = token.strip()
        if raw.startswith("**"):
            name_body = raw[2:].strip()
            kind = "var_keyword"
            required = False
        elif raw.startswith("*"):
            name_body = raw[1:].strip()
            kind = "var_positional"
            required = False
            kw_only_mode = True
        else:
            name_body = raw
            if slash_index != -1 and index < slash_index:
                kind = "positional_only"
            elif kw_only_mode:
                kind = "keyword_only"
            else:
                kind = "positional_or_keyword"

            equals_index = _find_top_level_char(name_body, "=")
            required = equals_index == -1

        equals_index = _find_top_level_char(name_body, "=")
        before_default = (
            name_body if equals_index == -1 else name_body[:equals_index].strip()
        )
        colon_index = _find_top_level_char(before_default, ":")
        name = (
            before_default
            if colon_index == -1
            else before_default[:colon_index].strip()
        )

        params.append(
            {
                "name": name,
                "kind": kind,
                "required": required,
            }
        )

    return params


def _is_breaking_signature_change(
    base_sig: str, pr_sig: str, class_name: str = "", method_name: str = ""
) -> bool:
    """Return True if PR signature is call-incompatible with baseline.

    Args:
        base_sig: Baseline method signature
        pr_sig: PR method signature
        class_name: Name of the class (for alias lookup)
        method_name: Name of the method (for alias lookup)
    """
    base_params = _parse_signature_shape(base_sig)
    pr_params = _parse_signature_shape(pr_sig)

    # Get approved aliases for this class.method
    aliases = _get_param_aliases(class_name, method_name)

    pr_index = 0
    for base_param in base_params:
        base_name = base_param["name"]
        found_index = -1

        # Look for matching parameter, considering aliases
        for idx in range(pr_index, len(pr_params)):
            pr_name = pr_params[idx]["name"]
            # Direct match or aliased match (pr_name is alias for base_name)
            if pr_name == base_name or aliases.get(pr_name) == base_name:
                found_index = idx
                break

        if found_index == -1:
            return True

        for inserted_param in pr_params[pr_index:found_index]:
            if inserted_param["required"]:
                return True
            if inserted_param["kind"] in ("positional_only", "positional_or_keyword"):
                return True

        pr_param = pr_params[found_index]
        if pr_param["kind"] != base_param["kind"]:
            return True

        if not base_param["required"] and pr_param["required"]:
            return True

        pr_index = found_index + 1

    return any(trailing_param["required"] for trailing_param in pr_params[pr_index:])


def compare_methods(
    base: dict[str, str],
    pr: dict[str, str],
    class_name: str,
    base_label: str,
) -> tuple[list[str], list[str]]:
    blocking = []
    informational = []
    base_methods = set(base.keys())
    pr_methods = set(pr.keys())

    removed = base_methods - pr_methods
    if removed:
        blocking.append(f"REMOVED {class_name} methods: {sorted(removed)}")

    added = pr_methods - base_methods
    if added:
        informational.append(f"ADDED {class_name} methods: {sorted(added)}")

    for name in sorted(base_methods & pr_methods):
        m_sig = _normalize_sig(base[name])
        p_sig = _normalize_sig(pr[name])
        if m_sig != p_sig:
            if _is_breaking_signature_change(base[name], pr[name], class_name, name):
                blocking.append(f"CHANGED {class_name}.{name}:")
                blocking.append(f"  {base_label}: {base[name]}")
                blocking.append(f"  pr:     {pr[name]}")
            else:
                informational.append(
                    f"CHANGED (non-blocking, annotation-only) {class_name}.{name}:"
                )
                informational.append(f"  {base_label}: {base[name]}")
                informational.append(f"  pr:     {pr[name]}")

    return blocking, informational


NOISE_EXPORTS = {
    # Names in this set are accidental implementation-detail leaks from
    # __init__.py (stdlib/typing/third-party imports), NOT established API.
    # Removing an established root export (anything imported, assigned, or
    # served by a documented lazy __getattr__ alias in __init__.py — e.g.
    # `fixme`, the lazy `meshtastic.serial` alias) is a breaking change and
    # must never be silenced here. See docs/compatibility.md.
    # stdlib modules imported in __init__.py - implementation details, not public API
    "*",
    "base64",
    "datetime",
    "os",
    "platform",
    "random",
    "socket",
    "stat",
    "sys",
    "threading",
    "time",
    "traceback",
    # typing helpers that can leak via module implementation details
    "Any",
    "Callable",
    "NamedTuple",
    "TypeGuard",
    "cast",
    "copy",
    "import_module",
    # third-party imports that leaked into namespace
    "google",
    "tabulate",
    "google.protobuf.json_format",
    # any alias that starts with underscore (private)
}


def compare_exports(
    base: list[str],
    pr: list[str],
) -> tuple[list[str], list[str]]:
    blocking = []
    informational = []
    removed = set(base) - set(pr)
    real_removed = {r for r in removed if r not in NOISE_EXPORTS}
    noise_removed = removed - real_removed
    if real_removed:
        blocking.append(f"REMOVED exports: {sorted(real_removed)}")
    if noise_removed:
        informational.append(
            f"REMOVED (noise/implementation detail): {sorted(noise_removed)}"
        )
    added = set(pr) - set(base)
    if added:
        informational.append(f"ADDED exports: {sorted(added)}")
    return blocking, informational


def compare_legacy_import_paths(
    base: list[str],
    pr: list[str],
) -> tuple[list[str], list[str]]:
    """Compare legacy import compatibility paths."""
    blocking = []
    informational = []
    removed = set(base) - set(pr)
    added = set(pr) - set(base)
    if removed:
        blocking.append(f"REMOVED legacy import paths: {sorted(removed)}")
    if added:
        informational.append(f"ADDED legacy import paths: {sorted(added)}")
    return blocking, informational


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare API surfaces and detect breaking compatibility changes."
    )
    parser.add_argument("base_surface", help="Path to baseline API surface JSON file")
    parser.add_argument("pr_surface", help="Path to PR API surface JSON file")
    parser.add_argument(
        "--base-label",
        default="base",
        help="Label shown in output for baseline signatures (default: %(default)s)",
    )
    parser.add_argument(
        "--warning-only",
        action="store_true",
        help=(
            "Always exit 0 even when breaking changes are detected. "
            "Useful for advisory checks."
        ),
    )
    return parser.parse_args(argv)


def main() -> int:
    args = _parse_args(sys.argv[1:])
    base_label = args.base_label
    warning_only = args.warning_only

    with open(args.base_surface, encoding="utf-8") as f:
        base = json.load(f)
    with open(args.pr_surface, encoding="utf-8") as f:
        pr = json.load(f)

    all_informational = []
    all_blocking = []

    for cls_key, cls_name in [
        ("node_methods", "Node"),
        ("mesh_interface_methods", "MeshInterface"),
    ]:
        blocking, informational = compare_methods(
            base.get(cls_key, {}),
            pr.get(cls_key, {}),
            cls_name,
            base_label,
        )
        if informational:
            all_informational.append(f"{cls_name} API changes (informational):")
            all_informational.extend(informational)
        if blocking:
            all_blocking.extend(blocking)

    export_blocking, export_informational = compare_exports(
        base.get("top_level_exports", []), pr.get("top_level_exports", [])
    )
    if export_informational:
        all_informational.append("Top-level export changes (informational):")
        all_informational.extend(export_informational)
    if export_blocking:
        all_blocking.extend(export_blocking)

    import_blocking, import_informational = compare_legacy_import_paths(
        base.get("legacy_import_paths", []), pr.get("legacy_import_paths", [])
    )
    if import_informational:
        all_informational.append("Legacy import path changes (informational):")
        all_informational.extend(import_informational)
    if import_blocking:
        all_blocking.extend(import_blocking)

    if all_informational:
        print("\n".join(all_informational))
        print()

    if all_blocking:
        print("\n".join(all_blocking))
        headline_entries = [line for line in all_blocking if not line.startswith("  ")]
        preview = headline_entries[:MAX_WARNING_PREVIEW_ITEMS]
        if preview:
            summary = "; ".join(preview)
            remaining = len(headline_entries) - len(preview)
            if remaining > 0:
                summary = f"{summary}; +{remaining} more"
        else:
            summary = "See job logs for details."
        if warning_only:
            _emit_github_annotation(
                "warning",
                f"Advisory API changes detected vs {base_label}: {summary}",
            )
            print(
                f"\nBREAKING API changes detected vs {base_label} "
                "(warning-only mode; not failing)."
            )
            return 0
        _emit_github_annotation(
            "error",
            f"Blocking API changes detected vs {base_label}: {summary}",
        )
        print(f"\nBREAKING API changes detected vs {base_label}!")
        return 1

    print(
        f"No breaking API changes detected vs {base_label} "
        "(informational changes above)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
