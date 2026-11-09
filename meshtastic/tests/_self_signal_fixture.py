"""Shared real-signal guard for both MTJK pytest collection roots."""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _no_real_self_signals() -> Iterator[list[tuple[int, bool]]]:
    """Replace the CLI self-signal seam so no test can signal this process.

    Every CLI fatal-signal path (the TERM/HUP re-delivery after teardown, the
    second-delivery forced exit, and the SIGINT re-delivery after a Ctrl+C
    teardown) delivers through ``meshtastic._self_signal._signal_self``. The
    recorder notes each request and returns, so callers keep their
    ``SystemExit`` fallbacks and the pytest process is never signaled. Tests
    that verify the delivery itself opt back in by patching the seam with
    their own fake. The seam module is imported lazily here and is stdlib-only,
    so conftest collection imports nothing heavy and no import-order effects
    are possible.
    """
    from meshtastic import _self_signal

    requests: list[tuple[int, bool]] = []

    def _recorder(signum: int, *, reset_to_default: bool) -> None:
        requests.append((signum, reset_to_default))

    # A private patcher, NOT the shared monkeypatch fixture: requesting that
    # fixture from an autouse conftest fixture sets it up before module-level
    # autouse fixtures, and LIFO teardown would then run their teardowns while
    # the test's own monkeypatches were still active. The context below keeps
    # this guard's patch bracketed around the test without touching that
    # order: a test's shared-monkeypatch undo restores the recorder first,
    # and this guard's undo restores the real function last.
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(_self_signal, "_signal_self", _recorder)
        yield requests
