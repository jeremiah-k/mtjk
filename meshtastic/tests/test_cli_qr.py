"""Unit tests for the terminal QR renderer in meshtastic.cli.qr."""

from __future__ import annotations

import io
import math
import os
import sys

import pytest

from meshtastic.cli import qr as cli_qr

# Compact output packs two module rows into each terminal row.
_TERMINAL_ALLOWED_CHARS = set(" \n▀▄█")


@pytest.fixture(autouse=True)
def _redirected_utf8_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep terminal-size policy independent of the test runner screen."""
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)


@pytest.fixture
def _require_segno() -> None:
    """Skip integration tests when the optional segno dependency is absent."""
    if cli_qr.segno is None:
        pytest.skip("segno is not installed")


@pytest.mark.unit
def test_load_segno_returns_none_when_optional_dependency_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loader should turn an optional-dependency ImportError into None."""

    def missing_segno(name: str) -> object:
        assert name == "segno"
        raise ModuleNotFoundError("segno unavailable", name="segno")

    monkeypatch.setattr(cli_qr.importlib, "import_module", missing_segno)

    assert cli_qr._load_segno() is None


@pytest.mark.unit
def test_load_segno_does_not_hide_broken_transitive_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken Segno installation should surface instead of looking absent."""

    def broken_segno(name: str) -> object:
        assert name == "segno"
        raise ModuleNotFoundError("dependency unavailable", name="segno_dependency")

    monkeypatch.setattr(cli_qr.importlib, "import_module", broken_segno)

    with pytest.raises(ModuleNotFoundError, match="dependency unavailable"):
        cli_qr._load_segno()


@pytest.mark.unit
def test_renderTerminalQr_pins_segno_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The renderer should request full QR codes with fixed ECC and border settings."""
    make_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    terminal_calls: list[dict[str, object]] = []

    class _StubCode:
        def terminal(self, *, out: object, border: int, compact: bool) -> None:
            terminal_calls.append({"out": out, "border": border, "compact": compact})
            out.write("<stub-terminal-qr>\n")  # type: ignore[attr-defined]

    class _StubSegno:
        def make(self, *args: object, **kwargs: object) -> _StubCode:
            make_calls.append((args, kwargs))
            return _StubCode()

    monkeypatch.setattr(cli_qr, "segno", _StubSegno())

    rendered = cli_qr.renderTerminalQr("https://meshtastic.org/e/#abc")

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
    assert terminal_calls[0]["compact"] is True


@pytest.mark.unit
def test_renderTerminalQr_raises_without_segno(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing segno dependency should raise instead of returning empty output."""
    monkeypatch.setattr(cli_qr, "segno", None)

    with pytest.raises(RuntimeError, match="segno"):
        cli_qr.renderTerminalQr("https://meshtastic.org/e/#abc")


@pytest.mark.unit
@pytest.mark.usefixtures("_require_segno")
def test_renderTerminalQr_output_is_terminal_text() -> None:
    """Rendered output should be newline-terminated compact terminal text.

    Do not assert exact matrix bytes: valid QR mask choices may differ between
    library versions. Structure (row count, charset) is the stable contract.
    """
    value = "https://meshtastic.org/e/#deterministic-primary"
    rendered = cli_qr.renderTerminalQr(value)

    assert rendered.endswith("\n")
    assert set(rendered) <= _TERMINAL_ALLOWED_CHARS
    lines = rendered.splitlines()
    assert len(lines) > 1


@pytest.mark.unit
@pytest.mark.usefixtures("_require_segno")
def test_renderTerminalQr_matches_full_qr_geometry() -> None:
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

    rendered = cli_qr.renderTerminalQr(value)
    module_rows = len(code.matrix)
    module_cols = len(code.matrix[0])
    quiet = 2 * cli_qr.QR_BORDER_MODULES
    lines = rendered.splitlines()
    assert len(lines) == math.ceil((module_rows + quiet) / 2)

    assert {len(line) for line in lines} == {module_cols + quiet}
    # Recover every module, including the complete quiet zone, from the blocks.
    pixels = {" ": (1, 1), "▀": (0, 1), "▄": (1, 0), "█": (0, 0)}
    decoded = []
    for line in lines:
        decoded.extend([[pixels[char][half] for char in line] for half in (0, 1)])
    expected = [list(row) for row in code.matrix_iter(border=4)]
    assert decoded[: len(expected)] == expected


@pytest.mark.unit
@pytest.mark.usefixtures("_require_segno")
def test_renderTerminalQr_uses_high_error_correction() -> None:
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


@pytest.mark.unit
@pytest.mark.usefixtures("_require_segno")
@pytest.mark.parametrize("level", ["H", "Q", "M", "L"])
@pytest.mark.parametrize("limiting_dimension", ["columns", "rows"])
def test_qr_selects_highest_error_correction_that_fits(
    monkeypatch: pytest.MonkeyPatch, level: str, limiting_dimension: str
) -> None:
    """Adapt dense URLs without wrapping or removing the quiet zone."""
    import segno

    value = "https://meshtastic.org/e/#" + "aZ012bcD" * 30
    code = segno.make(value, error=level, micro=False, boost_error=False)
    width, height = (int(size) for size in code.symbol_size(border=4))
    rows = math.ceil(height / 2) + 4
    if limiting_dimension == "columns":
        rows = 200
    else:
        width = 200
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(
        cli_qr.shutil, "get_terminal_size", lambda: os.terminal_size((width, rows))
    )
    rendered = cli_qr.renderTerminalQr(value)
    expected = io.StringIO()
    code.terminal(out=expected, border=4, compact=True)
    assert rendered == expected.getvalue()
    assert max(map(len, rendered.splitlines())) <= width
    assert len(rendered.splitlines()) <= rows - 4


@pytest.mark.unit
@pytest.mark.usefixtures("_require_segno")
def test_qr_explains_when_no_complete_code_fits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tiny terminal should get a usable instruction instead of wrapped modules."""
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(
        cli_qr.shutil, "get_terminal_size", lambda: os.terminal_size((10, 10))
    )
    rendered = cli_qr.renderTerminalQr("https://meshtastic.org/e/#abc")
    assert "Widen" in rendered
    assert "URL" in rendered
    assert not set("▀▄█") & set(rendered)


@pytest.mark.unit
@pytest.mark.usefixtures("_require_segno")
def test_qr_falls_back_for_ascii_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """The compact renderer must not crash on terminals without block glyphs."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        cli_qr.sys, "stdout", SimpleNamespace(encoding="ascii", isatty=lambda: False)
    )
    rendered = cli_qr.renderTerminalQr("https://meshtastic.org/e/#abc")
    assert "\x1b[" in rendered
    rendered.encode("ascii")
