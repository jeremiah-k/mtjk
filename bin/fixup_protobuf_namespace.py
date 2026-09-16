#!/usr/bin/env python3
"""Rewrite upstream Meshtastic proto namespaces for the Python package layout.

The upstream schema uses the ``meshtastic`` protobuf package and imports files
from ``meshtastic/``.  The Python client historically generates descriptors in
``meshtastic.protobuf`` from files staged under ``meshtastic/protobuf/``.

Keep those rewrites in one tested transformation so package-qualified custom
options (for example ``(meshtastic.field_metadata)``) move with the package
that defines them.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

TARGET_PACKAGE = "meshtastic.protobuf"
TARGET_IMPORT_PREFIX = "meshtastic/protobuf/"
NANOPB_TARGET_IMPORT = "meshtastic/protobuf/nanopb.proto"

_PACKAGE_RE = re.compile(r"(?m)^(?P<prefix>\s*package\s+)meshtastic(?P<suffix>\s*;)")
_MESHTASTIC_IMPORT_RE = re.compile(
    r'(?m)^(?P<prefix>\s*import(?:\s+(?:public|weak))?\s+")'
    r"meshtastic/(?!protobuf/)"
)
_NANOPB_IMPORT_RE = re.compile(
    r'(?m)^(?P<prefix>\s*import(?:\s+(?:public|weak))?\s+")'
    r'nanopb\.proto(?P<suffix>"\s*;)'
)


_PROTECTED_OR_CUSTOM_OPTION_RE = re.compile(
    r"//[^\n]*"
    r"|/\*.*?\*/"
    r'|"(?:\\.|[^"\\])*"'
    r"|'(?:\\.|[^'\\])*'"
    r"|(?P<option_prefix>\(\s*)meshtastic\.(?!protobuf\.)",
    re.DOTALL,
)


def _rewrite_custom_option_references(source: str) -> str:
    """Rewrite Meshtastic custom-option names outside comments and strings."""

    def replace(match: re.Match[str]) -> str:
        prefix = match.group("option_prefix")
        if prefix is None:
            return match.group(0)
        return f"{prefix}{TARGET_PACKAGE}."

    return _PROTECTED_OR_CUSTOM_OPTION_RE.sub(replace, source)


def rewrite_proto_source(source: str) -> str:
    """Return one proto source rewritten for ``meshtastic.protobuf``.

    The transformation is intentionally narrow: it updates the protobuf package,
    Meshtastic import paths, nanopb's import path, and package-qualified custom
    option references.  Language-specific option strings such as
    ``java_package = \"org.meshtastic.proto\"`` remain untouched.

    The operation is idempotent so callers and tests can safely apply it more
    than once to the same staged source.
    """

    rewritten = _PACKAGE_RE.sub(
        rf"\g<prefix>{TARGET_PACKAGE}\g<suffix>", source
    )
    rewritten = _MESHTASTIC_IMPORT_RE.sub(
        rf"\g<prefix>{TARGET_IMPORT_PREFIX}", rewritten
    )
    rewritten = _NANOPB_IMPORT_RE.sub(
        rf"\g<prefix>{NANOPB_TARGET_IMPORT}\g<suffix>", rewritten
    )
    return _rewrite_custom_option_references(rewritten)


def rewrite_proto_file(path: Path) -> bool:
    """Rewrite ``path`` in place and return whether its contents changed."""

    source = path.read_text(encoding="utf-8")
    rewritten = rewrite_proto_source(source)
    if rewritten == source:
        return False
    path.write_text(rewritten, encoding="utf-8")
    return True


def rewrite_proto_directory(directory: Path) -> int:
    """Rewrite all top-level ``*.proto`` files and return the changed count."""

    if not directory.is_dir():
        raise NotADirectoryError(directory)

    changed = 0
    for path in sorted(directory.glob("*.proto")):
        changed += rewrite_proto_file(path)
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
    changed = rewrite_proto_directory(args.directory)
    print(f"Rewrote protobuf namespaces in {changed} file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
