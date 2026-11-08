"""CLI entrypoint fatal-signal shutdown tests (TERM/HUP and Ctrl+C re-delivery)."""

from __future__ import annotations

import os
import signal
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
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
    yield
    cli_main._cli_signal_shutdown_started = False
    cli_main._cli_signal_received = None
    cli_main._cli_signal_shutdown_lock = threading.Lock()


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
        cli_main._run_cli_entrypoint(_body)
    assert excinfo.value.code == 130
    assert seam_requests == [(signal.SIGINT, True)]
    # TERM/HUP restoration precedes the output flush, which precedes the kill.
    assert events[-1] == f"kill:{signal.SIGINT}"
    assert events[-3:-1] == ["flush", "flush"]
    assert any(event.startswith("restore:") for event in events[:-3])


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
        cli_main._run_cli_entrypoint(_body)
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
        cli_main._run_cli_entrypoint(_body)
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
        cli_main._run_cli_entrypoint(_body)
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
            cli_main._run_cli_entrypoint(_body)
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
        cli_main._run_cli_entrypoint(_body)
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
        cli_main._run_cli_entrypoint(_body)
    assert excinfo.value.code == 1
    assert _no_real_self_signals == []


def test_recorded_sigterm_takes_precedence_over_sigint_redelivery(
    monkeypatch: pytest.MonkeyPatch,
    _no_real_self_signals: list[tuple[int, bool]],
) -> None:
    """A TERM recorded during the body kills first; the SIGINT path never runs."""
    registered: dict[int, Any] = {}

    def _fake_signal(sig: int, handler: Any) -> None:
        registered[sig] = handler

    def _fake_getsignal(sig: int) -> Any:
        return registered.get(sig, signal.SIG_DFL)

    monkeypatch.setattr(signal, "signal", _fake_signal)
    monkeypatch.setattr(signal, "getsignal", _fake_getsignal)

    real_install = cli_main._install_cli_signal_handlers

    def _install_then_record_term() -> dict[int, Any]:
        previous = real_install()
        # Simulate a TERM arriving during the body: the handler recorded it.
        cli_main._cli_signal_received = signal.SIGTERM
        return previous

    monkeypatch.setattr(
        cli_main, "_install_cli_signal_handlers", _install_then_record_term
    )

    def _body() -> None:
        raise KeyboardInterrupt()

    with pytest.raises(SystemExit) as excinfo:
        cli_main._run_cli_entrypoint(_body)
    assert excinfo.value.code == 143
    assert _no_real_self_signals == [(signal.SIGTERM, False)]


@pytest.mark.usefixtures("reset_mt_config")
def test_main_keyboard_interrupt_safety_fixture_records_sigint_request(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _no_real_self_signals: list[tuple[int, bool]],
) -> None:
    """The safety fixture turns Ctrl+C through main() into exit 130, no signal."""
    # Make the re-delivery eligible even though the pytest runner owns SIGINT:
    # without the fixture this exact path would signal this process.
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
    assert _no_real_self_signals == [(signal.SIGINT, True)]
    assert _ORIGINAL_GETSIGNAL(signal.SIGTERM) is actual_handler


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


def test_cli_signal_handler_logs_signal_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The handler logs which signal was received at info level."""
    with caplog.at_level("INFO", logger=cli_main.__name__):
        with pytest.raises(SystemExit):
            cli_main._cli_signal_handler(signal.SIGTERM, None)
    assert any(
        "Received SIGTERM" in record.getMessage() and record.levelname == "INFO"
        for record in caplog.records
    )


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
        if previous_term is not signal.SIG_IGN:
            assert signal.getsignal(signal.SIGTERM) is cli_main._cli_signal_handler
        else:
            assert signal.getsignal(signal.SIGTERM) is signal.SIG_IGN
        if previous_hup is not None:
            expected_hup = (
                signal.SIG_IGN
                if previous_hup is signal.SIG_IGN
                else cli_main._cli_signal_handler
            )
            assert signal.getsignal(signal.SIGHUP) is expected_hup
    finally:
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


def test_cli_main_installs_handlers_at_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Handlers are installed by the CLI entry, not on library import."""
    installed = False

    def _record_install() -> None:
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


def test_tunnel_main_keyboard_interrupt_maps_to_130(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
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
    assert all(signal.getsignal(sig) is handler for sig, handler in before.items())


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
