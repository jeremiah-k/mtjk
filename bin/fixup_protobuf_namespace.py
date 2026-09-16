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
from collections.abc import Callable
from pathlib import Path

TARGET_PACKAGE = "meshtastic.protobuf"
TARGET_IMPORT_PREFIX = "meshtastic/protobuf/"
NANOPB_TARGET_IMPORT = "meshtastic/protobuf/nanopb.proto"

_PACKAGE_RE = re.compile(
    r"(?m)^(?P<prefix>[ \t]*package[ \t]+)meshtastic(?P<suffix>[ \t]*;)"
)
_MESHTASTIC_IMPORT_RE = re.compile(
    r'(?m)^(?P<prefix>[ \t]*import(?:[ \t]+(?:public|weak))?[ \t]+")'
    r"meshtastic/(?!protobuf/)"
)
_NANOPB_IMPORT_RE = re.compile(
    r'(?m)^(?P<prefix>[ \t]*import(?:[ \t]+(?:public|weak))?[ \t]+")'
    r'nanopb\.proto(?P<suffix>"[ \t]*;)'
)
_QUALIFIED_SYMBOL_RE = re.compile(
    r"(?P<prefix>(?<![A-Za-z0-9_.])\.?)meshtastic\.(?!protobuf\b)"
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


def _rewrite_matches(
    source: str,
    pattern: re.Pattern[str],
    replacement: str | Callable[[re.Match[str]], str],
    *,
    protect_strings: bool,
) -> str:
    """Apply one regex only where the corresponding source is not protected."""
    masked = _masked_source(source, protect_strings=protect_strings)
    matches = list(pattern.finditer(masked))
    if not matches:
        return source

    rewritten = source
    for match in reversed(matches):
        if isinstance(replacement, str):
            value = match.expand(replacement)
        else:
            value = replacement(match)
        rewritten = rewritten[: match.start()] + value + rewritten[match.end() :]
    return rewritten


def _rewrite_proto_source(source: str) -> str:
    r"""Return one proto source rewritten for ``meshtastic.protobuf``.

    Package declarations and import strings are rewritten outside comments.
    Fully-qualified ``meshtastic.*`` symbols are rewritten only in protobuf code,
    so option/documentation strings and comments remain untouched. This covers
    custom options such as ``(meshtastic.field_metadata)`` as well as future
    fully-qualified message or enum references.

    The operation is idempotent so callers can safely apply it repeatedly to the
    same staged source.
    """
    rewritten = _rewrite_matches(
        source,
        _PACKAGE_RE,
        rf"\g<prefix>{TARGET_PACKAGE}\g<suffix>",
        protect_strings=False,
    )
    rewritten = _rewrite_matches(
        rewritten,
        _MESHTASTIC_IMPORT_RE,
        rf"\g<prefix>{TARGET_IMPORT_PREFIX}",
        protect_strings=False,
    )
    rewritten = _rewrite_matches(
        rewritten,
        _NANOPB_IMPORT_RE,
        rf"\g<prefix>{NANOPB_TARGET_IMPORT}\g<suffix>",
        protect_strings=False,
    )
    return _rewrite_matches(
        rewritten,
        _QUALIFIED_SYMBOL_RE,
        rf"\g<prefix>{TARGET_PACKAGE}.",
        protect_strings=True,
    )


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
