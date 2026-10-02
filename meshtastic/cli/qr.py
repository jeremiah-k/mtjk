"""Terminal QR rendering for channel and contact URLs.

Wraps the ``segno`` package behind a single
renderer so the CLI's QR behavior stays independent of the QR library.
"""

from __future__ import annotations

import importlib
import io
import shutil
import sys
from types import ModuleType


def _load_segno() -> ModuleType | None:
    """Import the optional Segno dependency when it is installed."""
    try:
        return importlib.import_module("segno")
    except ModuleNotFoundError as exc:
        if exc.name != "segno":
            raise
        return None


segno: ModuleType | None = _load_segno()
# Prefer maximum error correction, reducing it only to fit a known terminal.
QR_ERROR_CORRECTION = "H"
# Quiet-zone width in modules, matching the historical terminal rendering.
QR_BORDER_MODULES = 4
QR_ERROR_CORRECTION_LEVELS = (QR_ERROR_CORRECTION, "Q", "M", "L")
# Leave room for the description, URL and shell prompt.
QR_TERMINAL_RESERVED_ROWS = 4


def renderTerminalQr(value: str) -> str:
    """Render ``value`` as a QR code suitable for printing to a terminal.

    Parameters
    ----------
    value : str
        Text to encode (typically a channel or contact URL).

    Returns
    -------
    str
        Compact Unicode blocks, or ANSI blocks for output encodings without
        those glyphs. Interactive output selects the strongest error correction
        that fits the terminal; if none fits, a sizing hint replaces the code.
        Redirected output keeps maximum error correction without a size limit.

    Raises
    ------
    RuntimeError
        If ``segno`` is not installed.
    """
    if segno is None:
        raise RuntimeError("renderTerminalQr requires the 'segno' dependency")
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        "▀▄█".encode(encoding)
        compact = True
    except (UnicodeEncodeError, LookupError):
        compact = False
    terminal_size = shutil.get_terminal_size() if sys.stdout.isatty() else None
    levels = QR_ERROR_CORRECTION_LEVELS if terminal_size else (QR_ERROR_CORRECTION,)
    for level in levels:
        code = segno.make(value, error=level, micro=False, boost_error=False)
        if terminal_size:
            width, height = code.symbol_size(border=QR_BORDER_MODULES)
            columns = width if compact else width * 2
            rows = (height + 1) // 2 if compact else height
            if (
                columns > terminal_size.columns
                or rows + QR_TERMINAL_RESERVED_ROWS > terminal_size.lines
            ):
                continue
        out = io.StringIO()
        code.terminal(out=out, border=QR_BORDER_MODULES, compact=compact)
        return out.getvalue()
    return (
        f"QR needs at least {columns} columns and "
        f"{rows + QR_TERMINAL_RESERVED_ROWS} rows. "
        "Widen or enlarge the terminal, or open the URL above.\n"
    )
