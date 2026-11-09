"""CLI entrypoint fatal-signal shutdown tests (TERM/HUP and Ctrl+C re-delivery)."""

from __future__ import annotations

import ast
import os
import signal
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn

import pytest

from meshtastic import __main__ as cli_main
from meshtastic import _self_signal, mt_config
from meshtastic._interface_errors import MeshInterfaceError

pytestmark = pytest.mark.unit

_CLI_HANDLER = Callable[[int, Any], None]

# Deterministic stand-in for os.getpid() in re-delivery tests; os.kill is
# always monkeypatched too, so no test ever delivers a real signal.
_SENTINEL_PID = 4711

# Captured before any fixture runs: the autouse safety fixture replaces the
# seam with a recorder, so tests that exercise the real delivery opt back in
# through this reference.
_REAL_SIGNAL_SELF = _self_signal._signal_self
# Captured at import time as well: the disposition guard below must read and
# restore real signal state through these references even while a test has
# the signal module functions monkeypatched.
_ORIGINAL_GETSIGNAL = signal.getsignal
_ORIGINAL_SIGNAL_FUNCTION = signal.signal

# Signals whose real dispositions the autouse disposition guard watches.
_GUARDED_SIGNALS: tuple[int, ...] = tuple(
    sig
    for sig in (
        signal.SIGINT,
        signal.SIGTERM,
        getattr(signal, "SIGHUP", None),
    )
    if sig is not None
)


def _custom_sigint_handler(signum: int, frame: Any) -> None:
    """Stand in for an embedder-owned SIGINT handler."""


def _mock_default_sigint_with_fake_signal_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[int, Any]:
    """Emulate default SIGINT without changing the process's real TERM/HUP handlers."""
    installed: dict[int, Any] = {}

    def _register(sig: int, handler: Any) -> None:
        installed[sig] = handler

    def _get(sig: int) -> Any:
        if sig == signal.SIGINT:
            return signal.default_int_handler
        return installed.get(sig, signal.SIG_DFL)

    monkeypatch.setattr(signal, "signal", _register)
    monkeypatch.setattr(signal, "getsignal", _get)
    return installed


@pytest.fixture(autouse=True)
def _reset_shutdown_flag() -> Iterator[None]:
    """Keep the CLI second-signal guard state isolated between tests."""
    cli_main._cli_signal_shutdown_started = False
    cli_main._cli_signal_received = None
    cli_main._cli_signal_shutdown_lock = threading.Lock()
    cli_main._cli_signal_defer_exit = False
    cli_main._cli_signal_invocation_active = False
    yield
    cli_main._cli_signal_shutdown_started = False
    cli_main._cli_signal_received = None
    cli_main._cli_signal_shutdown_lock = threading.Lock()
    cli_main._cli_signal_defer_exit = False
    cli_main._cli_signal_invocation_active = False


@pytest.fixture(autouse=True)
def _restore_real_signal_dispositions() -> Iterator[None]:
    """Fail a test that changed this process's real signal dispositions.

    Driving the CLI entrypoints installs the real ``_cli_signal_handler`` for
    TERM/HUP, and the real self-signal seam resets a disposition to
    ``SIG_DFL``. A test that pairs a stubbed ``getsignal`` with a real
    ``signal.signal``, or opts into the seam without stubbing it, therefore
    leaves the pytest process holding a CLI handler (a later SIGTERM raises a
    spurious ``SystemExit(143)`` inside an unrelated test) or with SIGINT
    dead (Ctrl+C kills the process instead of raising
    ``KeyboardInterrupt``). The guard records the real INT/TERM/HUP
    dispositions before each test, restores anything the test changed
    through the real functions, and fails naming the leaked signal. It
    deliberately avoids the shared ``monkeypatch`` fixture; see
    ``_no_real_self_signals`` in conftest.py for the teardown ordering that
    requesting it would break.
    """
    before: dict[int, Any] = {sig: _ORIGINAL_GETSIGNAL(sig) for sig in _GUARDED_SIGNALS}
    yield
    changed: dict[int, Any] = {
        sig: value
        for sig, value in ((sig, _ORIGINAL_GETSIGNAL(sig)) for sig in _GUARDED_SIGNALS)
        if value is not before[sig]
    }
    for sig, previous in before.items():
        if sig in changed:
            _ORIGINAL_SIGNAL_FUNCTION(sig, previous)
    if changed:
        leaked = ", ".join(getattr(sig, "name", str(sig)) for sig in sorted(changed))
        pytest.fail(
            f"test changed this process's real signal disposition for "
            f"{leaked}; the leaked disposition was restored, but the test "
            "must stub signal.signal together with getsignal (or restore "
            "the disposition itself)",
            pytrace=False,
        )


@pytest.mark.parametrize(
    ("signum", "expected_code"),
    [(signal.SIGTERM, 143)]
    + ([(signal.SIGHUP, 129)] if hasattr(signal, "SIGHUP") else []),
)
def test_cli_signal_handler_raises_system_exit(signum: int, expected_code: int) -> None:
    """The first signal delivery raises SystemExit(128 + signum)."""
    with pytest.raises(SystemExit) as excinfo:
        cli_main._cli_signal_handler(signum, None)
    assert excinfo.value.code == expected_code
    assert cli_main._cli_signal_shutdown_started is True
    assert cli_main._cli_signal_received == signum


def test_second_cli_signal_dies_by_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second delivery must not wait on cleanup: it dies by the signal now."""
    events: list[str] = []
    seam_requests: list[tuple[int, bool]] = []

    class _ImmediateExit(BaseException):
        """Simulates real os._exit: control never returns to the caller."""

    def _fake_seam(signum: int, *, reset_to_default: bool) -> None:
        seam_requests.append((signum, reset_to_default))
        events.append("kill")

    def _fake_exit(code: int) -> None:
        events.append("exit")
        raise _ImmediateExit()

    monkeypatch.setattr(_self_signal, "_signal_self", _fake_seam)
    monkeypatch.setattr(os, "_exit", _fake_exit)
    cli_main._cli_signal_shutdown_started = True
    with pytest.raises(_ImmediateExit):
        cli_main._cli_signal_handler(signal.SIGTERM, None)
    assert seam_requests == [(signal.SIGTERM, True)]
    # The delivery is attempted first; os._exit remains the fallback.
    assert events == ["kill", "exit"]


def test_second_cli_signal_non_posix_falls_back_to_immediate_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off POSIX the second delivery keeps the immediate os._exit fallback."""
    exits: list[int] = []

    class _ImmediateExit(BaseException):
        """Simulates real os._exit: control never returns to the caller."""

    def _fake_exit(code: int) -> None:
        exits.append(code)
        raise _ImmediateExit()

    # Opt back into the real seam: with os.name patched off POSIX it no-ops,
    # so no signal can reach this process.
    monkeypatch.setattr(_self_signal, "_signal_self", _REAL_SIGNAL_SELF)
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(os, "_exit", _fake_exit)
    cli_main._cli_signal_shutdown_started = True
    with pytest.raises(_ImmediateExit):
        cli_main._cli_signal_handler(signal.SIGTERM, None)
    assert exits == [143]


def test_atomic_second_signal_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even before the Python flag is set, a nested handler must force-exit."""

    class ExitAttempt(BaseException):
        pass

    seam_requests: list[tuple[int, bool]] = []
    exits: list[int] = []

    def fake_seam(signum: int, *, reset_to_default: bool) -> None:
        seam_requests.append((signum, reset_to_default))

    def force_exit(code: int) -> None:
        exits.append(code)
        raise ExitAttempt()

    monkeypatch.setattr(_self_signal, "_signal_self", fake_seam)
    monkeypatch.setattr(os, "_exit", force_exit)
    cli_main._cli_signal_shutdown_lock.acquire()
    with pytest.raises(ExitAttempt):
        cli_main._cli_signal_handler(signal.SIGTERM, None)
    assert seam_requests == [(signal.SIGTERM, True)]
    assert exits == [143]


@pytest.mark.parametrize(
    ("signum", "expected_code"),
    [(signal.SIGTERM, 143)]
    + ([(signal.SIGHUP, 129)] if hasattr(signal, "SIGHUP") else []),
)
def test_entrypoint_re_delivers_recorded_signal(
    monkeypatch: pytest.MonkeyPatch, signum: int, expected_code: int
) -> None:
    """Teardown completes, dispositions are restored, then the signal kills us."""
    registered: dict[int, Any] = {}
    order: list[str] = []
    seam_requests: list[tuple[int, bool]] = []

    def _fake_signal(sig: int, handler: Any) -> None:
        registered[sig] = handler
        if handler is signal.SIG_DFL:
            order.append("restore")

    def _fake_getsignal(sig: int) -> Any:
        return registered.get(sig, signal.SIG_DFL)

    def _fake_seam(sig: int, *, reset_to_default: bool) -> None:
        seam_requests.append((sig, reset_to_default))
        order.append(f"kill:{sig}")

    monkeypatch.setattr(signal, "signal", _fake_signal)
    monkeypatch.setattr(signal, "getsignal", _fake_getsignal)
    monkeypatch.setattr(_self_signal, "_signal_self", _fake_seam)

    def _body() -> None:
        cli_main._cli_signal_handler(signum, None)

    with pytest.raises(SystemExit) as excinfo:
        cli_main._run_cli_entrypoint(_body)
    assert excinfo.value.code == expected_code
    assert seam_requests == [(signum, False)]
    # Restoration of every replaced disposition precedes the fatal kill.
    assert order[-1] == f"kill:{signum}"
    assert "restore" in order[:-1]
    # The recorded signal was consumed; it must not leak to the next run.
    assert cli_main._cli_signal_received is None


def test_second_term_during_tail_flush_dies_by_signal(
    monkeypatch: pytest.MonkeyPatch,
    _no_real_self_signals: list[tuple[int, bool]],
) -> None:
    """A TERM arriving in the post-teardown tail cannot unwind as SystemExit.

    The first TERM is recorded in the body. While the entrypoint tail
    flushes output streams, a second TERM is delivered: the forced-exit
    guard must still be armed through the fatal re-delivery, so the handler
    dies by the signal instead of raising a catchable SystemExit(143) out
    of the graceful path.
    """
    registered: dict[int, Any] = {}

    def _fake_signal(sig: int, handler: Any) -> None:
        registered[sig] = handler

    def _fake_getsignal(sig: int) -> Any:
        return registered.get(sig, signal.SIG_DFL)

    class _ImmediateExit(BaseException):
        """Simulates real os._exit: control never returns to the caller."""

    forced: list[tuple[int, bool]] = []
    exits: list[int] = []
    guard_lock_before = cli_main._cli_signal_shutdown_lock

    def _forced_seam(sig: int, *, reset_to_default: bool) -> None:
        forced.append((sig, reset_to_default))

    def _fake_exit(code: int) -> None:
        exits.append(code)
        raise _ImmediateExit()

    def _flush_delivering_second_term() -> None:
        # TERM #2 arrives while the tail flushes output streams.
        cli_main._cli_signal_handler(signal.SIGTERM, None)

    monkeypatch.setattr(signal, "signal", _fake_signal)
    monkeypatch.setattr(signal, "getsignal", _fake_getsignal)
    monkeypatch.setattr(_self_signal, "_signal_self", _forced_seam)
    monkeypatch.setattr(os, "_exit", _fake_exit)
    monkeypatch.setattr(
        cli_main, "_flush_cli_output_streams", _flush_delivering_second_term
    )

    def _body() -> None:
        # TERM #1 during the body: recorded, graceful SystemExit(143).
        cli_main._cli_signal_handler(signal.SIGTERM, None)

    with pytest.raises(_ImmediateExit):
        cli_main._run_cli_entrypoint(_body, owns_process=True)
    # The second delivery took the forced-exit path: die by the signal, then
    # the immediate os._exit fallback. No graceful SystemExit(143) surfaced.
    assert forced == [(signal.SIGTERM, True)]
    assert exits == [143]
    # The guard reset stayed behind the fatal re-delivery and still ran in
    # the tail finally after the forced exit interrupted it.
    assert cli_main._cli_signal_shutdown_started is False
    assert cli_main._cli_signal_shutdown_lock is not guard_lock_before
    assert cli_main._cli_signal_defer_exit is False
    assert cli_main._cli_signal_invocation_active is False


@pytest.mark.parametrize(
    "flush_error",
    [OSError, ValueError, AttributeError],
)
def test_re_delivery_flushes_streams_and_suppresses_flush_errors(
    monkeypatch: pytest.MonkeyPatch,
    _no_real_self_signals: list[tuple[int, bool]],
    flush_error: type[BaseException],
) -> None:
    """A failing stdout/stderr flush must not block the fatal re-delivery."""
    flush_calls: list[str] = []

    class _BrokenStream:
        def flush(self) -> None:
            flush_calls.append("flush")
            raise flush_error()

    monkeypatch.setattr(sys, "stdout", _BrokenStream())
    monkeypatch.setattr(sys, "stderr", _BrokenStream())
    with pytest.raises(SystemExit) as excinfo:
        cli_main._re_deliver_recorded_signal(signal.SIGTERM)
    assert flush_calls == ["flush", "flush"]
    assert _no_real_self_signals == [(signal.SIGTERM, False)]
    assert excinfo.value.code == 143


def test_entrypoint_without_signal_does_not_re_deliver(
    _no_real_self_signals: list[tuple[int, bool]],
) -> None:
    """A run that completes without a signal exits normally, with no kill."""
    ran: list[bool] = []

    def _body() -> None:
        ran.append(True)

    cli_main._run_cli_entrypoint(_body)
    assert ran == [True]
    assert _no_real_self_signals == []
    assert cli_main._cli_signal_received is None


def test_recorded_signal_reset_between_invocations(
    monkeypatch: pytest.MonkeyPatch,
    _no_real_self_signals: list[tuple[int, bool]],
) -> None:
    """The recorded signal is consumed by one shutdown, not carried over."""
    registered: dict[int, Any] = {}

    def _fake_signal(sig: int, handler: Any) -> None:
        registered[sig] = handler

    def _fake_getsignal(sig: int) -> Any:
        return registered.get(sig, signal.SIG_DFL)

    monkeypatch.setattr(signal, "signal", _fake_signal)
    monkeypatch.setattr(signal, "getsignal", _fake_getsignal)

    def _signaled_body() -> None:
        cli_main._cli_signal_handler(signal.SIGTERM, None)

    with pytest.raises(SystemExit):
        cli_main._run_cli_entrypoint(_signaled_body)
    assert _no_real_self_signals == [(signal.SIGTERM, False)]
    assert cli_main._cli_signal_received is None

    ran: list[bool] = []

    def _clean_body() -> None:
        ran.append(True)

    cli_main._run_cli_entrypoint(_clean_body)
    assert ran == [True]
    assert _no_real_self_signals == [(signal.SIGTERM, False)]


# ---------------------------------------------------------------------------
# Self-signal delivery seam and SIGINT re-delivery policy
# ---------------------------------------------------------------------------


def test_signal_self_resets_disposition_then_kills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seam sets SIG_DFL before the kill and targets its own PID."""
    events: list[str] = []
    kills: list[tuple[int, int]] = []

    def _fake_signal(sig: int, handler: Any) -> None:
        if handler is signal.SIG_DFL:
            events.append(f"reset:{sig}")

    def _fake_kill(pid: int, sig: int) -> None:
        kills.append((pid, sig))
        events.append(f"kill:{sig}")

    monkeypatch.setattr(_self_signal, "_signal_self", _REAL_SIGNAL_SELF)
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(os, "getpid", lambda: _SENTINEL_PID)
    monkeypatch.setattr(signal, "signal", _fake_signal)
    monkeypatch.setattr(os, "kill", _fake_kill)

    _self_signal._signal_self(signal.SIGINT, reset_to_default=True)

    assert kills == [(_SENTINEL_PID, signal.SIGINT)]
    assert events == [f"reset:{signal.SIGINT}", f"kill:{signal.SIGINT}"]


def test_signal_self_without_reset_keeps_current_disposition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TERM/HUP re-delivery rides the restored disposition instead of SIG_DFL."""
    resets: list[int] = []
    kills: list[tuple[int, int]] = []

    def _fake_signal(sig: int, handler: Any) -> None:
        if handler is signal.SIG_DFL:
            resets.append(sig)

    monkeypatch.setattr(_self_signal, "_signal_self", _REAL_SIGNAL_SELF)
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(os, "getpid", lambda: _SENTINEL_PID)
    monkeypatch.setattr(signal, "signal", _fake_signal)
    monkeypatch.setattr(os, "kill", lambda pid, sig: kills.append((pid, sig)))

    _self_signal._signal_self(signal.SIGTERM, reset_to_default=False)

    assert resets == []
    assert kills == [(_SENTINEL_PID, signal.SIGTERM)]


def test_signal_self_off_posix_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Off POSIX the seam delivers nothing and lets the caller fall back."""
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(_self_signal, "_signal_self", _REAL_SIGNAL_SELF)
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(os, "kill", lambda pid, sig: kills.append((pid, sig)))

    _self_signal._signal_self(signal.SIGINT, reset_to_default=True)

    assert kills == []


def test_signal_self_suppresses_delivery_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing kill is suppressed so the caller's exit-status fallback runs."""
    resets: list[int] = []

    def _fake_signal(sig: int, handler: Any) -> None:
        if handler is signal.SIG_DFL:
            resets.append(sig)

    def _raising_kill(pid: int, sig: int) -> None:
        raise OSError("delivery refused")

    monkeypatch.setattr(_self_signal, "_signal_self", _REAL_SIGNAL_SELF)
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(os, "getpid", lambda: _SENTINEL_PID)
    # The reset must not run through the real signal API: it would leave this
    # process's real SIGINT disposition at SIG_DFL for the whole session.
    monkeypatch.setattr(signal, "signal", _fake_signal)
    monkeypatch.setattr(os, "kill", _raising_kill)

    _self_signal._signal_self(signal.SIGINT, reset_to_default=True)

    assert resets == [signal.SIGINT]


@pytest.mark.skipif(
    os.name != "posix", reason="real self-signal delivery requires POSIX"
)
def test_signal_self_preserves_restored_handler_exception() -> None:
    """A restored host handler's exception propagates from the re-delivery seam.

    The TERM/HUP re-delivery rides the host's restored disposition: when that
    Python-level handler raises while receiving the delivered signal, its
    exception must reach the caller instead of being masked by the seam's
    silent delivery-failure fallback. The real disposition is restored in a
    finally so the autouse disposition guard sees no leak.
    """
    before = _ORIGINAL_GETSIGNAL(signal.SIGTERM)

    def _failing_host_handler(signum: int, frame: Any) -> None:
        raise ValueError("host handler failed")

    _ORIGINAL_SIGNAL_FUNCTION(signal.SIGTERM, _failing_host_handler)
    try:
        with pytest.raises(ValueError, match="host handler failed"):
            _REAL_SIGNAL_SELF(signal.SIGTERM, reset_to_default=False)
    finally:
        _ORIGINAL_SIGNAL_FUNCTION(signal.SIGTERM, before)


def test_sigint_re_delivery_eligible_under_default_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The interpreter's default SIGINT handler qualifies for re-delivery."""
    monkeypatch.setattr(os, "name", "posix")
    _mock_default_sigint_with_fake_signal_handlers(monkeypatch)
    assert cli_main._sigint_re_delivery_eligible() is True


@pytest.mark.parametrize(
    "handler",
    [
        pytest.param(signal.SIG_IGN, id="sig-ign"),
        pytest.param(signal.SIG_DFL, id="sig-dfl"),
        pytest.param(None, id="non-python-handler"),
        pytest.param(_custom_sigint_handler, id="custom-handler"),
    ],
)
def test_sigint_re_delivery_skips_non_default_handlers(
    monkeypatch: pytest.MonkeyPatch, handler: Any
) -> None:
    """An embedder-owned SIGINT disposition never qualifies for re-delivery."""
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(signal, "getsignal", lambda sig: handler)
    assert cli_main._sigint_re_delivery_eligible() is False


def test_sigint_re_delivery_skips_non_posix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off POSIX the re-delivery keeps the plain SystemExit(130)."""
    monkeypatch.setattr(os, "name", "nt")
    _mock_default_sigint_with_fake_signal_handlers(monkeypatch)
    assert cli_main._sigint_re_delivery_eligible() is False


@pytest.mark.parametrize(
    "interactive_marker",
    [
        pytest.param("flags-interactive", id="sys-flags-interactive"),
        pytest.param("ps1-attribute", id="sys-ps1-attribute"),
    ],
)
def test_sigint_re_delivery_skips_interactive_interpreter(
    monkeypatch: pytest.MonkeyPatch,
    interactive_marker: str,
) -> None:
    """An interactive interpreter owns its session: Ctrl+C returns to the prompt."""
    monkeypatch.setattr(os, "name", "posix")
    _mock_default_sigint_with_fake_signal_handlers(monkeypatch)
    if interactive_marker == "flags-interactive":
        monkeypatch.setattr(sys, "flags", SimpleNamespace(interactive=True))
    else:
        monkeypatch.setattr(sys, "ps1", ">>> ", raising=False)
    assert cli_main._sigint_re_delivery_eligible() is False


def test_sigint_re_delivery_skips_off_main_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker threads must never attempt the fatal SIGINT delivery."""
    _mock_default_sigint_with_fake_signal_handlers(monkeypatch)
    results: list[bool] = []

    def _worker() -> None:
        results.append(cli_main._sigint_re_delivery_eligible())

    worker = threading.Thread(target=_worker)
    worker.start()
    worker.join()
    assert results == [False]


class _RecordingStream:
    """Stand-in stdout/stderr that records flush attempts while absorbing writes."""

    def __init__(self, events: list[str]) -> None:
        self._events = events

    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        self._events.append("flush")


@pytest.mark.skipif(os.name != "posix", reason="fatal SIGINT delivery requires POSIX")
def test_entrypoint_redelivers_sigint_when_default_handler_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl+C teardown is restored, flushed, then ended by a real SIGINT."""
    registered: dict[int, Any] = {}
    events: list[str] = []
    seam_requests: list[tuple[int, bool]] = []

    def _fake_signal(sig: int, handler: Any) -> None:
        registered[sig] = handler
        if handler is signal.SIG_DFL:
            events.append(f"restore:{sig}")

    def _fake_getsignal(sig: int) -> Any:
        if sig == signal.SIGINT:
            return signal.default_int_handler
        return registered.get(sig, signal.SIG_DFL)

    def _fake_seam(sig: int, *, reset_to_default: bool) -> None:
        seam_requests.append((sig, reset_to_default))
        events.append(f"kill:{sig}")

    monkeypatch.setattr(signal, "signal", _fake_signal)
    monkeypatch.setattr(signal, "getsignal", _fake_getsignal)
    monkeypatch.setattr(sys, "stdout", _RecordingStream(events))
    monkeypatch.setattr(sys, "stderr", _RecordingStream(events))
    monkeypatch.setattr(_self_signal, "_signal_self", _fake_seam)

    def _body() -> None:
        raise KeyboardInterrupt()

    with pytest.raises(SystemExit) as excinfo:
        cli_main._run_cli_entrypoint(_body, owns_process=True)
    assert excinfo.value.code == 130
    assert seam_requests == [(signal.SIGINT, True)]
    # TERM/HUP restoration precedes the output flush, which precedes the kill.
    assert events[-1] == f"kill:{signal.SIGINT}"
    assert events[-3:-1] == ["flush", "flush"]
    assert any(event.startswith("restore:") for event in events[:-3])


@pytest.mark.skipif(os.name != "posix", reason="fatal SIGINT delivery requires POSIX")
def test_entrypoint_sigint_falls_back_to_130_when_delivery_returns(
    monkeypatch: pytest.MonkeyPatch,
    _no_real_self_signals: list[tuple[int, bool]],
) -> None:
    """When the seam returns without killing, the plain SystemExit(130) stands."""
    actual_handler = signal.getsignal(signal.SIGTERM)
    _mock_default_sigint_with_fake_signal_handlers(monkeypatch)

    def _body() -> None:
        raise KeyboardInterrupt()

    with pytest.raises(SystemExit) as excinfo:
        cli_main._run_cli_entrypoint(_body, owns_process=True)
    assert excinfo.value.code == 130
    assert _no_real_self_signals == [(signal.SIGINT, True)]
    assert _ORIGINAL_GETSIGNAL(signal.SIGTERM) is actual_handler


@pytest.mark.parametrize(
    "handler",
    [
        pytest.param(signal.SIG_IGN, id="sig-ign"),
        pytest.param(signal.SIG_DFL, id="sig-dfl"),
        pytest.param(None, id="non-python-handler"),
        pytest.param(_custom_sigint_handler, id="custom-handler"),
    ],
)
def test_entrypoint_sigint_skips_when_sigint_not_default(
    monkeypatch: pytest.MonkeyPatch,
    _no_real_self_signals: list[tuple[int, bool]],
    handler: Any,
) -> None:
    """An embedder-owned SIGINT disposition keeps the plain exit 130."""
    registered: dict[int, Any] = {}

    def _fake_signal(sig: int, handler_arg: Any) -> None:
        registered[sig] = handler_arg

    def _fake_getsignal(sig: int) -> Any:
        if sig == signal.SIGINT:
            return handler
        return registered.get(sig, signal.SIG_DFL)

    monkeypatch.setattr(signal, "signal", _fake_signal)
    monkeypatch.setattr(signal, "getsignal", _fake_getsignal)

    def _body() -> None:
        raise KeyboardInterrupt()

    with pytest.raises(SystemExit) as excinfo:
        cli_main._run_cli_entrypoint(_body, owns_process=True)
    assert excinfo.value.code == 130
    assert _no_real_self_signals == []


def test_entrypoint_sigint_skips_non_posix(
    monkeypatch: pytest.MonkeyPatch,
    _no_real_self_signals: list[tuple[int, bool]],
) -> None:
    """Off POSIX the Ctrl+C teardown keeps the plain SystemExit(130)."""
    monkeypatch.setattr(os, "name", "nt")

    def _body() -> None:
        raise KeyboardInterrupt()

    with pytest.raises(SystemExit) as excinfo:
        cli_main._run_cli_entrypoint(_body, owns_process=True)
    assert excinfo.value.code == 130
    assert _no_real_self_signals == []


def test_entrypoint_sigint_skips_off_main_thread(
    monkeypatch: pytest.MonkeyPatch,
    _no_real_self_signals: list[tuple[int, bool]],
) -> None:
    """A worker-thread entrypoint run never attempts the fatal SIGINT delivery."""
    registered: dict[int, Any] = {}

    def _fake_signal(sig: int, handler: Any) -> None:
        registered[sig] = handler

    def _fake_getsignal(sig: int) -> Any:
        return registered.get(sig, signal.SIG_DFL)

    monkeypatch.setattr(signal, "signal", _fake_signal)
    monkeypatch.setattr(signal, "getsignal", _fake_getsignal)
    outcomes: list[tuple[str, Any]] = []

    def _worker() -> None:
        def _body() -> None:
            raise KeyboardInterrupt()

        try:
            cli_main._run_cli_entrypoint(_body, owns_process=True)
        except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
            outcomes.append(("raised", exc))
        else:
            outcomes.append(("returned", None))

    worker = threading.Thread(target=_worker)
    worker.start()
    worker.join()
    assert len(outcomes) == 1
    kind, exc = outcomes[0]
    assert kind == "raised"
    assert isinstance(exc, SystemExit)
    assert exc.code == 130
    assert _no_real_self_signals == []


def test_entrypoint_sigint_skips_when_cli_exit_raises_other_error(
    monkeypatch: pytest.MonkeyPatch,
    _no_real_self_signals: list[tuple[int, bool]],
) -> None:
    """A failing exit handler keeps its own exception; no SIGINT re-delivery."""

    def _broken_exit(message: str, return_value: int = 1) -> NoReturn:
        raise RuntimeError("exit handler exploded")

    monkeypatch.setattr(cli_main, "_cli_exit", _broken_exit)

    def _body() -> None:
        raise KeyboardInterrupt()

    with pytest.raises(RuntimeError, match="exit handler exploded"):
        cli_main._run_cli_entrypoint(_body, owns_process=True)
    assert _no_real_self_signals == []


def test_entrypoint_sigint_skips_when_cli_exit_maps_other_code(
    monkeypatch: pytest.MonkeyPatch,
    _no_real_self_signals: list[tuple[int, bool]],
) -> None:
    """A SystemExit other than 130 from _cli_exit is not turned into a kill."""

    def _alt_exit(message: str, return_value: int = 1) -> NoReturn:
        raise SystemExit(1)

    monkeypatch.setattr(cli_main, "_cli_exit", _alt_exit)

    def _body() -> None:
        raise KeyboardInterrupt()

    with pytest.raises(SystemExit) as excinfo:
        cli_main._run_cli_entrypoint(_body, owns_process=True)
    assert excinfo.value.code == 1
    assert _no_real_self_signals == []


def test_recorded_sigterm_takes_precedence_over_sigint_redelivery(
    monkeypatch: pytest.MonkeyPatch,
    _no_real_self_signals: list[tuple[int, bool]],
) -> None:
    """A TERM recorded during the body kills first; the SIGINT path never runs."""
    _mock_default_sigint_with_fake_signal_handlers(monkeypatch)
    monkeypatch.setattr(cli_main, "_sigint_re_delivery_eligible", lambda: True)
    body_calls: list[bool] = []

    def _body() -> None:
        body_calls.append(True)
        cli_main._cli_signal_received = signal.SIGTERM
        raise KeyboardInterrupt()

    with pytest.raises(SystemExit) as excinfo:
        cli_main._run_cli_entrypoint(_body, owns_process=True)
    assert body_calls == [True]
    assert excinfo.value.code == 143
    assert _no_real_self_signals == [(signal.SIGTERM, False)]


@pytest.mark.usefixtures("reset_mt_config")
def test_embedded_main_keyboard_interrupt_keeps_catchable_exit_130(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _no_real_self_signals: list[tuple[int, bool]],
) -> None:
    """Ctrl+C through an embedded main() call stays a catchable SystemExit(130)."""
    actual_handler = _ORIGINAL_GETSIGNAL(signal.SIGTERM)
    _mock_default_sigint_with_fake_signal_handlers(monkeypatch)
    monkeypatch.setattr(cli_main, "initParser", lambda: None)

    def _interrupt() -> None:
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli_main, "common", _interrupt)
    with pytest.raises(SystemExit) as excinfo:
        cli_main.main()
    assert excinfo.value.code == 130
    assert "Interrupted." in capsys.readouterr().err
    # An embedder keeps its process: no fatal SIGINT is forged against it.
    assert _no_real_self_signals == []
    assert _ORIGINAL_GETSIGNAL(signal.SIGTERM) is actual_handler


@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.skipif(os.name != "posix", reason="fatal SIGINT delivery requires POSIX")
def test_console_main_keyboard_interrupt_records_sigint_request(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _no_real_self_signals: list[tuple[int, bool]],
) -> None:
    """The process-owning console entrypoint still requests the fatal SIGINT."""
    actual_handler = _ORIGINAL_GETSIGNAL(signal.SIGTERM)
    _mock_default_sigint_with_fake_signal_handlers(monkeypatch)
    monkeypatch.setattr(cli_main, "initParser", lambda: None)

    def _interrupt() -> None:
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli_main, "common", _interrupt)
    with pytest.raises(SystemExit) as excinfo:
        cli_main._console_main()
    assert excinfo.value.code == 130
    assert "Interrupted." in capsys.readouterr().err
    assert _no_real_self_signals == [(signal.SIGINT, True)]
    assert _ORIGINAL_GETSIGNAL(signal.SIGTERM) is actual_handler


@pytest.mark.usefixtures("reset_mt_config")
def test_embedded_main_recorded_sigterm_is_still_re_delivered(
    monkeypatch: pytest.MonkeyPatch,
    _no_real_self_signals: list[tuple[int, bool]],
) -> None:
    """TERM re-delivery is universal, embedded callers included.

    A recorded TERM is re-delivered after teardown so it rides the host's
    restored disposition: a host handler runs, and a host with SIG_DFL dies.
    Only the SIGINT re-delivery is reserved for process-owning entrypoints.
    """
    _mock_default_sigint_with_fake_signal_handlers(monkeypatch)
    monkeypatch.setattr(cli_main, "initParser", lambda: None)
    monkeypatch.setattr(
        cli_main,
        "common",
        lambda: cli_main._cli_signal_handler(signal.SIGTERM, None),
    )
    with pytest.raises(SystemExit) as excinfo:
        cli_main.main()
    assert excinfo.value.code == 143
    assert _no_real_self_signals == [(signal.SIGTERM, False)]


def test_flush_worker_is_reused_while_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocked flush worker is not duplicated by repeated shutdowns."""
    started: list[object] = []

    class _BlockedThread:
        def __init__(self, *, target: Any, daemon: bool, name: str) -> None:
            self._target = target

        def start(self) -> None:
            started.append(self)

        def join(self, timeout: float | None = None) -> None:
            pass

        def is_alive(self) -> bool:
            return True

    monkeypatch.setattr(cli_main, "threading", SimpleNamespace(Thread=_BlockedThread))
    monkeypatch.setattr(cli_main, "_cli_signal_flush_worker", None)

    cli_main._flush_cli_output_streams()
    cli_main._flush_cli_output_streams()

    assert len(started) == 1


def test_install_and_restore_reset_recorded_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Install and restore reset the recorded-signal slot between invocations."""
    monkeypatch.setattr(signal, "getsignal", lambda sig: signal.SIG_DFL)
    monkeypatch.setattr(signal, "signal", lambda sig, handler: None)
    cli_main._install_cli_signal_handlers()
    cli_main._cli_signal_received = signal.SIGTERM
    cli_main._restore_cli_signal_handlers({signal.SIGTERM: signal.SIG_DFL})
    assert cli_main._cli_signal_received is None
    cli_main._cli_signal_received = signal.SIGTERM
    cli_main._install_cli_signal_handlers()
    assert cli_main._cli_signal_received is None


def test_cli_signal_handler_performs_no_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signal must unwind even when the interrupted logger is unavailable."""

    def forbidden_log(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("signal handler attempted logging")

    monkeypatch.setattr(cli_main.logger, "info", forbidden_log)
    with pytest.raises(SystemExit):
        cli_main._cli_signal_handler(signal.SIGTERM, None)


def test_install_registers_handlers_on_main_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The installer registers the CLI handler for SIGTERM and SIGHUP."""
    registered: dict[Any, _CLI_HANDLER] = {}

    def _fake_signal(signum: Any, handler: _CLI_HANDLER) -> None:
        registered[signum] = handler

    monkeypatch.setattr(signal, "signal", _fake_signal)
    monkeypatch.setattr(signal, "getsignal", lambda _sig: signal.SIG_DFL)
    cli_main._install_cli_signal_handlers()
    assert registered.get(signal.SIGTERM) is cli_main._cli_signal_handler
    if hasattr(signal, "SIGHUP"):
        assert registered.get(signal.SIGHUP) is cli_main._cli_signal_handler


def test_install_registers_real_handlers_and_restores() -> None:
    """Registration works through the real signal API on the main thread."""
    previous_term = signal.getsignal(signal.SIGTERM)
    previous_hup = (
        signal.getsignal(signal.SIGHUP) if hasattr(signal, "SIGHUP") else None
    )
    try:
        cli_main._install_cli_signal_handlers()
        if previous_term is not signal.SIG_IGN and previous_term is not None:
            assert signal.getsignal(signal.SIGTERM) is cli_main._cli_signal_handler
        else:
            assert signal.getsignal(signal.SIGTERM) is previous_term
        if previous_hup is not None:
            expected_hup = (
                signal.SIG_IGN
                if previous_hup is signal.SIG_IGN
                else cli_main._cli_signal_handler
            )
            assert signal.getsignal(signal.SIGHUP) is expected_hup
    finally:
        if previous_term is not None:
            signal.signal(signal.SIGTERM, previous_term)
        if previous_hup is not None:
            signal.signal(signal.SIGHUP, previous_hup)


def test_install_skipped_off_main_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off-main-thread callers never touch signal registration."""
    registered: dict[Any, _CLI_HANDLER] = {}

    def _fake_signal(signum: Any, handler: _CLI_HANDLER) -> None:
        registered[signum] = handler

    monkeypatch.setattr(signal, "signal", _fake_signal)
    errors: list[BaseException] = []

    def _install_elsewhere() -> None:
        try:
            cli_main._install_cli_signal_handlers()
        except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
            errors.append(exc)

    worker = threading.Thread(target=_install_elsewhere)
    worker.start()
    worker.join()
    assert registered == {}
    assert errors == []


def test_install_tolerates_registration_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Platforms or embedders rejecting registration must not break startup."""

    def _raising_signal(_signum: Any, _handler: _CLI_HANDLER) -> None:
        raise ValueError("signal only works in main thread")

    monkeypatch.setattr(signal, "signal", _raising_signal)
    cli_main._install_cli_signal_handlers()


def test_inherited_sighup_ignore_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Respect nohup/supervisor SIG_IGN instead of replacing it."""
    if not hasattr(signal, "SIGHUP"):
        pytest.skip("SIGHUP is unavailable on this platform")
    registrations: list[Any] = []
    monkeypatch.setattr(
        signal,
        "getsignal",
        lambda sig: signal.SIG_IGN if sig == signal.SIGHUP else signal.SIG_DFL,
    )
    monkeypatch.setattr(signal, "signal", lambda sig, _fn: registrations.append(sig))
    cli_main._install_cli_signal_handlers()
    assert signal.SIGTERM in registrations
    assert signal.SIGHUP not in registrations


def test_inherited_non_python_handler_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-Python disposition (getsignal None) is skipped and never restored."""
    registrations: list[Any] = []
    monkeypatch.setattr(signal, "getsignal", lambda _sig: None)
    monkeypatch.setattr(signal, "signal", lambda sig, _fn: registrations.append(sig))
    previous = cli_main._install_cli_signal_handlers()
    assert registrations == []
    assert previous == {}
    # Restoration must not attempt signal.signal(sig, None), which raises.
    cli_main._restore_cli_signal_handlers(previous)
    assert registrations == []


@pytest.mark.usefixtures("reset_mt_config")
def test_entrypoint_maps_keyboard_interrupt_during_handler_install(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ctrl+C during disposition setup still reports Interrupted and restores."""
    restored: list[dict[int, Any]] = []

    def _interrupt_install(_previous: dict[int, Any] | None = None) -> dict[int, Any]:
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli_main, "_install_cli_signal_handlers", _interrupt_install)
    monkeypatch.setattr(
        cli_main,
        "_restore_cli_signal_handlers",
        lambda previous, *, reset_state=True: restored.append(previous),
    )

    with pytest.raises(SystemExit) as excinfo:
        cli_main._run_cli_entrypoint(lambda: None)

    assert excinfo.value.code == 130
    assert "Interrupted." in capsys.readouterr().err
    assert restored == [{}]


@pytest.mark.usefixtures("reset_mt_config")
def test_keyboard_interrupt_during_restoration_resets_invocation_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Ctrl+C escaping handler restoration still resets invocation ownership.

    Restoration runs inside the entrypoint's finally: an interrupt delivered at
    that boundary must not skip the ownership-reset tail, or a later embedded
    entrypoint would silently lose handler ownership for the rest of the
    process.
    """

    def _interrupted_restore(
        previous: dict[int, Any] | None, *, reset_state: bool = True
    ) -> None:
        raise KeyboardInterrupt()

    _mock_default_sigint_with_fake_signal_handlers(monkeypatch)
    monkeypatch.setattr(cli_main, "_restore_cli_signal_handlers", _interrupted_restore)
    with pytest.raises(KeyboardInterrupt):
        cli_main._run_cli_entrypoint(lambda: None)
    assert not cli_main._cli_signal_invocation_active
    assert not cli_main._cli_signal_defer_exit
    assert not cli_main._cli_signal_shutdown_started


def test_cli_main_installs_handlers_at_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Handlers are installed by the CLI entry, not on library import."""
    installed = False

    def _record_install(previous: dict[int, Any]) -> None:
        nonlocal installed
        installed = True

    monkeypatch.setattr(cli_main, "_install_cli_signal_handlers", _record_install)
    monkeypatch.setattr(cli_main, "initParser", lambda: None)
    monkeypatch.setattr(cli_main, "common", lambda: None)
    cli_main.main()
    assert installed is True


def test_library_import_does_not_install_signal_handlers() -> None:
    """Detect import-time signal mutations in a fresh interpreter."""
    import subprocess
    import sys

    probe = """import signal
before = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, getattr(signal, 'SIGHUP', signal.SIGTERM))}
import meshtastic
from meshtastic import __main__
assert all(signal.getsignal(sig) is old for sig, old in before.items())
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_main_restores_signal_dispositions_after_normal_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running an embedded CLI invocation cannot replace its host's handlers."""
    before = {sig: signal.getsignal(sig) for sig in cli_main._CLI_HANDLED_SIGNALS}
    monkeypatch.setattr(mt_config, "parser", None)
    monkeypatch.setattr(cli_main, "initParser", lambda: None)
    monkeypatch.setattr(cli_main, "common", lambda: None)
    cli_main.main()
    assert all(signal.getsignal(sig) is handler for sig, handler in before.items())


def test_tunnel_entry_installs_and_restores_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mesh-tunnel entrypoint follows the same shutdown policy."""
    import argparse

    before = {sig: signal.getsignal(sig) for sig in cli_main._CLI_HANDLED_SIGNALS}
    monkeypatch.setattr(mt_config, "args", None)
    monkeypatch.setattr(mt_config, "parser", None)

    def _set_args() -> None:
        mt_config.args = argparse.Namespace()

    monkeypatch.setattr(cli_main, "initParser", _set_args)
    monkeypatch.setattr(cli_main, "common", lambda: None)
    cli_main.tunnelMain()
    args = mt_config.args
    assert args is not None
    assert args.tunnel is True
    assert all(signal.getsignal(sig) is handler for sig, handler in before.items())


@pytest.mark.usefixtures("reset_mt_config")
def test_tunnel_main_keyboard_interrupt_maps_to_130(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _no_real_self_signals: list[tuple[int, bool]],
) -> None:
    """Ctrl+C in the tunnel entrypoint reports Interrupted. with status 130."""
    import argparse

    before = {sig: signal.getsignal(sig) for sig in cli_main._CLI_HANDLED_SIGNALS}

    def _set_args() -> None:
        mt_config.args = argparse.Namespace()

    monkeypatch.setattr(cli_main, "initParser", _set_args)

    def _interrupt() -> None:
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli_main, "common", _interrupt)
    with pytest.raises(SystemExit) as excinfo:
        cli_main.tunnelMain()
    assert excinfo.value.code == 130
    assert "Interrupted." in capsys.readouterr().err
    # An embedded tunnelMain() keeps the host alive: no fatal SIGINT request.
    assert _no_real_self_signals == []
    assert all(signal.getsignal(sig) is handler for sig, handler in before.items())


@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.skipif(os.name != "posix", reason="fatal SIGINT delivery requires POSIX")
def test_console_tunnel_main_keyboard_interrupt_records_sigint_request(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _no_real_self_signals: list[tuple[int, bool]],
) -> None:
    """The mesh-tunnel console entrypoint still requests the fatal SIGINT."""
    import argparse

    _mock_default_sigint_with_fake_signal_handlers(monkeypatch)

    def _set_args() -> None:
        mt_config.args = argparse.Namespace()

    monkeypatch.setattr(cli_main, "initParser", _set_args)

    def _interrupt() -> None:
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli_main, "common", _interrupt)
    with pytest.raises(SystemExit) as excinfo:
        cli_main._console_tunnel_main()
    assert excinfo.value.code == 130
    assert "Interrupted." in capsys.readouterr().err
    assert _no_real_self_signals == [(signal.SIGINT, True)]


@pytest.mark.usefixtures("reset_mt_config")
def test_tunnel_main_mesh_interface_error_maps_to_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An interface failure in the tunnel entrypoint reports ERROR with status 1."""
    import argparse

    before = {sig: signal.getsignal(sig) for sig in cli_main._CLI_HANDLED_SIGNALS}

    def _set_args() -> None:
        mt_config.args = argparse.Namespace()

    monkeypatch.setattr(cli_main, "initParser", _set_args)

    def _fail() -> None:
        raise MeshInterfaceError("radio vanished")

    monkeypatch.setattr(cli_main, "common", _fail)
    with pytest.raises(SystemExit) as excinfo:
        cli_main.tunnelMain()
    assert excinfo.value.code == 1
    assert "ERROR: radio vanished" in capsys.readouterr().err
    assert all(signal.getsignal(sig) is handler for sig, handler in before.items())


def test_signal_registration_is_cli_entry_only() -> None:
    """Signal API use stays confined to the entrypoint and its delivery seam.

    Pins that the library package never installs handlers on import: embedders
    keep full control of their own signal policy. ``_self_signal.py`` is the
    single delivery seam every CLI self-kill path shares.
    """
    package_root = Path(cli_main.__file__).resolve().parent
    offenders: set[str] = set()
    for path in package_root.rglob("*.py"):
        relative = path.relative_to(package_root)
        if relative.parts[0] == "tests":
            continue
        text = path.read_text(encoding="utf-8")
        if "signal.signal(" in text:
            offenders.add(str(relative))
    assert offenders == {"__main__.py", "_self_signal.py"}


def test_main_block_invokes_console_wrapper() -> None:
    """``python -m meshtastic`` dispatches to the process-owning wrapper.

    Pinned with an AST check rather than runpy: runpy executes the module in
    a fresh namespace where monkeypatches on the already-imported module are
    invisible, so intercepting the dispatch would require running the real
    CLI with its device probing. Parsing the module proves the wiring —
    the ``__main__`` block calls ``_console_main()`` and nothing else —
    without executing any of it.
    """
    source_path = Path(cli_main.__file__).resolve()
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    dispatches: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "__name__"
            and len(test.comparators) == 1
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Eq)
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == "__main__"
        ):
            continue
        for statement in node.body:
            if (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Call)
                and isinstance(statement.value.func, ast.Name)
            ):
                dispatches.append(statement.value.func.id)
    assert dispatches == ["_console_main"]


@pytest.mark.parametrize("phase", ["install", "restore"])
def test_first_signal_during_handler_transition_is_restored_and_redelivered(
    monkeypatch: pytest.MonkeyPatch,
    _no_real_self_signals: list[tuple[int, bool]],
    phase: str,
) -> None:
    installed: dict[int, Any] = {}
    fired = False
    body_calls: list[bool] = []

    def install(sig: int, handler: Any) -> None:
        nonlocal fired
        installed[sig] = handler
        changing_to_cli = handler is cli_main._cli_signal_handler
        if not fired and changing_to_cli == (phase == "install"):
            fired = True
            cli_main._cli_signal_handler(signal.SIGTERM, None)

    monkeypatch.setattr(signal, "signal", install)
    monkeypatch.setattr(
        signal, "getsignal", lambda sig: installed.get(sig, signal.SIG_DFL)
    )
    with pytest.raises(SystemExit) as error:
        cli_main._run_cli_entrypoint(lambda: body_calls.append(True))
    assert error.value.code == 143
    assert fired
    assert body_calls == ([] if phase == "install" else [True])
    assert all(handler is signal.SIG_DFL for handler in installed.values())
    assert _no_real_self_signals == [(signal.SIGTERM, False)]
    assert cli_main._cli_signal_received is None
    assert not cli_main._cli_signal_invocation_active


def test_worker_entrypoint_does_not_redeliver_main_thread_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    _no_real_self_signals: list[tuple[int, bool]],
) -> None:
    _mock_default_sigint_with_fake_signal_handlers(monkeypatch)
    completed = threading.Event()
    owner_state: list[tuple[bool, bool, bool]] = []

    def body() -> None:
        try:
            cli_main._cli_signal_handler(signal.SIGTERM, None)
        finally:

            def worker_body() -> None:
                cli_main._run_cli_entrypoint(lambda: None)
                completed.set()

            worker = threading.Thread(target=worker_body)
            worker.start()
            worker.join(timeout=2)
            assert completed.is_set()
            assert _no_real_self_signals == []
            owner_state.append(
                (
                    cli_main._cli_signal_shutdown_started,
                    cli_main._cli_signal_shutdown_lock.locked(),
                    cli_main._cli_signal_invocation_active,
                )
            )

    with pytest.raises(SystemExit):
        cli_main._run_cli_entrypoint(body)
    assert _no_real_self_signals == [(signal.SIGTERM, False)]
    # Check outside the fatal re-delivery: its SystemExit can mask a failed
    # assertion in the body's cleanup.
    assert owner_state == [(True, True, True)]


def test_blocked_output_flush_cannot_prevent_signal_redelivery(
    monkeypatch: pytest.MonkeyPatch,
    _no_real_self_signals: list[tuple[int, bool]],
) -> None:
    release = threading.Event()
    finished = threading.Event()

    class BlockedStream:
        def flush(self) -> None:
            try:
                release.wait(timeout=5)
            finally:
                finished.set()

    monkeypatch.setattr(sys, "stdout", BlockedStream())
    monkeypatch.setattr(
        cli_main, "_CLI_OUTPUT_FLUSH_TIMEOUT_SECONDS", 0.01, raising=False
    )
    try:
        with pytest.raises(SystemExit):
            cli_main._re_deliver_recorded_signal(signal.SIGTERM)
        assert _no_real_self_signals == [(signal.SIGTERM, False)]
        assert not finished.is_set()
    finally:
        release.set()
        assert finished.wait(timeout=2)


@pytest.mark.parametrize("failure", ["reset", "kill"])
def test_self_signal_failure_returns_without_logging(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    def reset(sig: int, handler: Any) -> None:
        if failure == "reset":
            raise ValueError("cannot reset handler")

    def kill(pid: int, sig: int) -> None:
        raise OSError("cannot signal process")

    def forbidden_log(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("fatal signal fallback attempted logging")

    monkeypatch.setattr(signal, "signal", reset)
    monkeypatch.setattr(
        _self_signal,
        "os",
        SimpleNamespace(name="posix", getpid=lambda: _SENTINEL_PID, kill=kill),
    )
    monkeypatch.setattr(
        _self_signal, "logger", SimpleNamespace(debug=forbidden_log), raising=False
    )
    _REAL_SIGNAL_SELF(signal.SIGTERM, reset_to_default=True)


def test_pending_signal_at_body_handoff_prevents_body_entry(
    monkeypatch: pytest.MonkeyPatch,
    _no_real_self_signals: list[tuple[int, bool]],
) -> None:
    """A signal between the installation check and body admission is not lost."""
    _mock_default_sigint_with_fake_signal_handlers(monkeypatch)
    tree = ast.parse(Path(cli_main.__file__).read_text(encoding="utf-8"))
    entry = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_run_cli_entrypoint"
    )
    handoff_line = min(
        node.lineno
        for node in ast.walk(entry)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "_cli_signal_defer_exit"
            for target in node.targets
        )
        and isinstance(node.value, ast.Constant)
        and node.value.value is False
    )
    body_calls: list[bool] = []
    delivered = False

    def trace(frame: Any, event: str, arg: Any) -> Any:
        nonlocal delivered
        if (
            not delivered
            and event == "line"
            and frame.f_code is cli_main._run_cli_entrypoint.__code__
            and frame.f_lineno == handoff_line
        ):
            delivered = True
            cli_main._cli_signal_handler(signal.SIGTERM, None)
        return trace

    previous_trace = sys.gettrace()
    try:
        sys.settrace(trace)
        with pytest.raises(SystemExit):
            cli_main._run_cli_entrypoint(lambda: body_calls.append(True))
    finally:
        sys.settrace(previous_trace)
    assert delivered
    assert body_calls == []
    assert _no_real_self_signals == [(signal.SIGTERM, False)]
