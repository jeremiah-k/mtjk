#!/usr/bin/env python3
"""Rewrite upstream Meshtastic proto namespaces for the Python package layout.

The upstream schema uses the ``meshtastic`` protobuf package and imports files
from ``meshtastic/``. The Python client historically generates descriptors in
``meshtastic.protobuf`` from files staged under ``meshtastic/protobuf/``.

Keep those rewrites in one tested transformation so every code-level reference
to the relocated package moves with it while comments and string literals stay
byte-for-byte unchanged.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

TARGET_PACKAGE = "meshtastic.protobuf"
TARGET_IMPORT_PREFIX = "meshtastic/protobuf/"
NANOPB_TARGET_IMPORT = "meshtastic/protobuf/nanopb.proto"

# Statement grammars accept any whitespace run between tokens: protobuf treats
# comments as whitespace, and masking comments to same-length spaces keeps
# matches aligned with the original bytes. Only the named ``target`` group is
# ever replaced, so comments crossed by a match stay byte-for-byte intact.
_PACKAGE_RE = re.compile(r"(?m)^\s*package\s+(?P<target>meshtastic)(?=\s*;)")
_MESHTASTIC_IMPORT_RE = re.compile(
    r"(?m)^\s*import(?:\s+(?:public|weak))?\s+['\"]"
    r"(?P<target>meshtastic/)(?!protobuf/)"
)
_NANOPB_IMPORT_RE = re.compile(
    r"(?m)^\s*import(?:\s+(?:public|weak))?\s+"
    r"(?P<quote>['\"])(?P<target>nanopb\.proto)(?P=quote)(?=\s*;)"
)
_QUALIFIED_SYMBOL_RE = re.compile(
    r"(?P<target>meshtastic)(?=\s*\.(?!\s*protobuf\b))"
)

# Proto comments and quoted strings are the only regions namespace rewriting
# must not inspect. Matching both together keeps comment markers embedded inside
# quoted metadata from being mistaken for real comments.
_PROTECTED_RE = re.compile(
    r"//[^\n]*" r"|/\*.*?\*/" r'|"(?:\\.|[^"\\])*"' r"|'(?:\\.|[^'\\])*'",
    re.DOTALL,
)


def _blank_preserving_newlines(text: str) -> str:
    """Return an equal-length mask that preserves line boundaries."""
    return "".join("\n" if char == "\n" else " " for char in text)


def _masked_source(source: str, *, protect_strings: bool) -> str:
    """Mask comments and, optionally, quoted strings without shifting offsets."""

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        is_string = token.startswith(('"', "'"))
        if is_string and not protect_strings:
            return token
        return _blank_preserving_newlines(token)

    return _PROTECTED_RE.sub(replace, source)


def _rewrite_target_matches(
    source: str,
    pattern: re.Pattern[str],
    replacement: str,
    *,
    protect_strings: bool,
) -> str:
    """Rewrite only each named ``target`` group outside protected source.

    Matches are located on a same-length masked copy, but only the target token
    is replaced in the original source. This preserves comments and unusual
    whitespace that may legally appear between surrounding protobuf tokens.
    """
    masked = _masked_source(source, protect_strings=protect_strings)
    matches = list(pattern.finditer(masked))
    if not matches:
        return source

    rewritten = source
    for match in reversed(matches):
        start, end = match.span("target")
        rewritten = rewritten[:start] + replacement + rewritten[end:]
    return rewritten


def _previous_non_whitespace(source: str, index: int) -> int:
    """Return the previous non-whitespace offset, or ``-1`` when absent."""
    while index >= 0 and source[index].isspace():
        index -= 1
    return index


def _is_root_qualified_symbol(masked: str, start: int) -> bool:
    """Return whether ``meshtastic`` at ``start`` begins a root qualification.

    Protobuf permits whitespace/comments around ``.`` tokens. Inspect the
    masked lexical context so ``vendor . meshtastic . Type`` remains foreign,
    while ``. meshtastic . Type`` and ``meshtastic . Type`` are relocated.
    """
    previous = _previous_non_whitespace(masked, start - 1)
    if previous < 0:
        return True

    previous_char = masked[previous]
    if previous_char != ".":
        return not (previous_char.isalnum() or previous_char == "_")

    before_dot = _previous_non_whitespace(masked, previous - 1)
    return before_dot < 0 or not (
        masked[before_dot].isalnum() or masked[before_dot] == "_"
    )


def _rewrite_qualified_symbols(source: str) -> str:
    """Relocate root-qualified Meshtastic symbols outside strings/comments."""
    masked = _masked_source(source, protect_strings=True)
    matches = [
        match
        for match in _QUALIFIED_SYMBOL_RE.finditer(masked)
        if _is_root_qualified_symbol(masked, match.start("target"))
    ]
    rewritten = source
    for match in reversed(matches):
        start, end = match.span("target")
        rewritten = rewritten[:start] + TARGET_PACKAGE + rewritten[end:]
    return rewritten


def _rewrite_proto_source(source: str) -> str:
    r"""Return one proto source rewritten for ``meshtastic.protobuf``.

    Package declarations and import strings are rewritten outside comments.
    Fully-qualified ``meshtastic.*`` symbols are rewritten only in protobuf code,
    including legal whitespace/comments around the qualification dot, so
    option/documentation strings and comments remain untouched. This covers
    custom options such as ``(meshtastic.field_metadata)`` as well as future
    fully-qualified message or enum references.

    The operation is idempotent so callers can safely apply it repeatedly to the
    same staged source.
    """
    rewritten = _rewrite_target_matches(
        source,
        _PACKAGE_RE,
        TARGET_PACKAGE,
        protect_strings=False,
    )
    rewritten = _rewrite_target_matches(
        rewritten,
        _MESHTASTIC_IMPORT_RE,
        TARGET_IMPORT_PREFIX,
        protect_strings=False,
    )
    rewritten = _rewrite_target_matches(
        rewritten,
        _NANOPB_IMPORT_RE,
        NANOPB_TARGET_IMPORT,
        protect_strings=False,
    )
    return _rewrite_qualified_symbols(rewritten)


def _rewrite_proto_file(path: Path) -> bool:
    """Rewrite ``path`` in place and return whether its contents changed."""
    source = path.read_text(encoding="utf-8")
    rewritten = _rewrite_proto_source(source)
    if rewritten == source:
        return False
    path.write_text(rewritten, encoding="utf-8")
    return True


def _rewrite_proto_directory(directory: Path) -> int:
    """Rewrite all top-level ``*.proto`` files and return the changed count."""
    if not directory.is_dir():
        raise NotADirectoryError(directory)

    changed = 0
    for path in sorted(directory.glob("*.proto")):
        changed += _rewrite_proto_file(path)
    return changed


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rewrite staged Meshtastic protobuf namespaces for Python codegen."
    )
    parser.add_argument(
        "directory", type=Path, help="directory containing staged .proto files"
    )
    return parser.parse_args()


def main() -> int:
    """CLI entrypoint used by ``regen-protobufs.sh``."""
    args = _parse_args()
    changed = _rewrite_proto_directory(args.directory)
    print(f"Rewrote protobuf namespaces in {changed} file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
