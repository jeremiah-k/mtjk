"""Unit tests for the terminal QR renderer in meshtastic.cli.qr."""

from __future__ import annotations

import re

import pytest

from meshtastic.cli import qr as cli_qr

# Characters that may appear in segno's ANSI terminal rendering:
# escape sequences (ESC [ <digits> m), spaces, and newlines.
_TERMINAL_ALLOWED_CHARS = set(" \n\x1b[0123456789m")

# ANSI color/control sequences (e.g. "\x1b[7m", "\x1b[0m").
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


@pytest.fixture
def _require_segno() -> None:
    """Skip integration tests when the optional segno dependency is absent."""
    if cli_qr.segno is None:
        pytest.skip("segno is not installed")


@pytest.mark.unit
def test_render_terminal_qr_pins_segno_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The renderer should request full QR codes with fixed ECC and border settings."""
    make_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    terminal_calls: list[dict[str, object]] = []

    class _StubCode:
        def terminal(self, *, out: object, border: int) -> None:
            terminal_calls.append({"out": out, "border": border})
            out.write("<stub-terminal-qr>\n")  # type: ignore[attr-defined]

    class _StubSegno:
        def make(self, *args: object, **kwargs: object) -> _StubCode:
            make_calls.append((args, kwargs))
            return _StubCode()

    monkeypatch.setattr(cli_qr, "segno", _StubSegno())

    rendered = cli_qr.render_terminal_qr("https://meshtastic.org/e/#abc")

    assert rendered == "<stub-terminal-qr>\n"
    assert len(make_calls) == 1
    args, kwargs = make_calls[0]
    assert args == ("https://meshtastic.org/e/#abc",)
    assert kwargs == {
        "error": cli_qr.QR_ERROR_CORRECTION,
        "micro": False,
        "boost_error": False,
    }
    assert cli_qr.QR_ERROR_CORRECTION == "H"
    assert len(terminal_calls) == 1
    assert terminal_calls[0]["border"] == cli_qr.QR_BORDER_MODULES
    assert terminal_calls[0]["border"] == 4


@pytest.mark.unit
def test_render_terminal_qr_raises_without_segno(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing segno dependency should raise instead of returning empty output."""
    monkeypatch.setattr(cli_qr, "segno", None)

    with pytest.raises(RuntimeError, match="segno"):
        cli_qr.render_terminal_qr("https://meshtastic.org/e/#abc")


@pytest.mark.unit
@pytest.mark.usefixtures("_require_segno")
def test_render_terminal_qr_output_is_terminal_text() -> None:
    """Rendered output should be newline-terminated ANSI terminal text.

    Do not assert exact matrix bytes: valid QR mask choices may differ between
    library versions. Structure (row count, charset) is the stable contract.
    """
    value = "https://meshtastic.org/e/#deterministic-primary"
    rendered = cli_qr.render_terminal_qr(value)

    assert rendered.endswith("\n")
    assert set(rendered) <= _TERMINAL_ALLOWED_CHARS
    lines = rendered.splitlines()
    assert len(lines) > 1


@pytest.mark.unit
@pytest.mark.usefixtures("_require_segno")
def test_render_terminal_qr_matches_full_qr_geometry() -> None:
    """Rendered rows should match the equivalent full (non-micro) QR geometry."""
    import segno  # pylint: disable=import-outside-toplevel

    # Short value that segno could encode as Micro QR when micro is allowed.
    value = "https://meshtastic.org/e/#A"
    code = segno.make(
        value,
        error=cli_qr.QR_ERROR_CORRECTION,
        micro=False,
        boost_error=False,
    )
    assert not code.is_micro

    rendered = cli_qr.render_terminal_qr(value)
    module_rows = len(code.matrix)
    module_cols = len(code.matrix[0])
    quiet = 2 * cli_qr.QR_BORDER_MODULES
    lines = rendered.splitlines()
    assert len(lines) == module_rows + quiet

    # Two terminal columns per module (segno doubles width for terminal
    # aspect ratio): after stripping ANSI escapes, every row must span the
    # full quiet-zoned code width, so a rendering that dropped or doubled
    # columns would fail here.
    stripped = [re.sub(_ANSI_ESCAPE_RE, "", line) for line in lines]
    assert {len(line) for line in stripped} == {2 * (module_cols + quiet)}
    assert all(line for line in stripped)


@pytest.mark.unit
@pytest.mark.usefixtures("_require_segno")
def test_render_terminal_qr_uses_high_error_correction() -> None:
    """The equivalent segno construction should select error level H."""
    import segno  # pylint: disable=import-outside-toplevel

    value = "https://meshtastic.org/e/#A"
    code = segno.make(
        value,
        error=cli_qr.QR_ERROR_CORRECTION,
        micro=False,
        boost_error=False,
    )
    assert code.error == "H"
