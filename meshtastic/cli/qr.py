"""Terminal QR rendering for channel and contact URLs.

Wraps the ``segno`` package (optional ``cli`` extra) behind a single
renderer so the CLI's QR behavior stays independent of the QR library.
"""

from __future__ import annotations

import io
from types import ModuleType

try:
    import segno as _segno
except ImportError:  # pragma: no cover - depends on optional cli extra
    _segno = None  # type: ignore[assignment]

segno: ModuleType | None = _segno
# Match PyQRCode's effective defaults: maximum error correction, never
# Micro QR, and no silent error-correction boosting.
QR_ERROR_CORRECTION = "H"
# Quiet-zone width in modules, matching the historical terminal rendering.
QR_BORDER_MODULES = 4


def render_terminal_qr(value: str) -> str:
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
        If the optional ``segno`` dependency is not installed.
    """
    if segno is None:
        raise RuntimeError(
            "render_terminal_qr requires the optional 'segno' dependency"
        )
    out = io.StringIO()
    segno.make(
        value,
        error=QR_ERROR_CORRECTION,
        micro=False,
        boost_error=False,
    ).terminal(out=out, border=QR_BORDER_MODULES)
    return out.getvalue()
