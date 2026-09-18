"""Terminal QR rendering for channel and contact URLs.

Wraps the ``segno`` package behind a single
renderer so the CLI's QR behavior stays independent of the QR library.
"""

from __future__ import annotations

import importlib
import io
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
# Match PyQRCode's effective defaults: maximum error correction, never
# Micro QR, and no silent error-correction boosting.
QR_ERROR_CORRECTION = "H"
# Quiet-zone width in modules, matching the historical terminal rendering.
QR_BORDER_MODULES = 4


def renderTerminalQr(value: str) -> str:
    """Render ``value`` as a QR code suitable for printing to a terminal.

    Parameters
    ----------
    value : str
        Text to encode (typically a channel or contact URL).

    Returns
    -------
    str
        Terminal-rendered QR code (ANSI reverse-video blocks), including a
        trailing newline.

    Raises
    ------
    RuntimeError
        If ``segno`` is not installed.
    """
    if segno is None:
        raise RuntimeError("renderTerminalQr requires the 'segno' dependency")
    out = io.StringIO()
    segno.make(
        value,
        error=QR_ERROR_CORRECTION,
        micro=False,
        boost_error=False,
    ).terminal(out=out, border=QR_BORDER_MODULES)
    return out.getvalue()
