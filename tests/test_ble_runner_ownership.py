"""Ownership regressions for the BLE singleton coroutine runner.

These tests pin the runner's teardown/restart state machine: a live stopping
owner thread must never be silently abandoned, a failed thread start must
publish no owner, and coroutine submission plus future tracking must be atomic
against a concurrent stop.
"""

import asyncio
import threading
from collections.abc import Generator
from concurrent.futures import Future
from typing import Any, cast

import pytest

from meshtastic.interfaces.ble import runner as _runner_module
from meshtastic.interfaces.ble.constants import (
    BLECLIENT_ERROR_LOOP_RESTART_FAILED,
    BLEConfig,
)
from meshtastic.interfaces.ble.runner import BLECoroutineRunner

pytestmark = pytest.mark.unit


class _StubbedOwnerThread:
    """Fake runner owner thread with controllable retirement behavior."""

    def __init__(self, *, retires_on_join: bool) -> None:
        """Initialize the stub owner thread.

        Parameters
        ----------
        retires_on_join : bool
            Whether :meth:`join` flips the thread into the exited state.
        """
        self.name = "stubbed-runner-owner"
        self.join_calls: list[float | None] = []
        self._retires_on_join = retires_on_join
        self._alive = True

    def is_alive(self) -> bool:
        """Report whether the stub owner has retired.

        Returns
        -------
        bool
            ``True`` until a retiring join marks the thread exited.
        """
        return self._alive

    def join(self, timeout: float | None = None) -> None:
        """Record the join call and optionally retire the stub owner.

        Parameters
        ----------
        timeout : float | None
            Join timeout requested by the caller.
        """
        self.join_calls.append(timeout)
        if self._retires_on_join:
            self._alive = False


class _RunningLoopStub:
    """Loop stub that always reports a running event loop."""

    @staticmethod
    def is_running() -> bool:
        """Report whether the loop is active.

        Returns
        -------
        bool
            Always ``True``.
        """
        return True


def _noop_ensure_running(timeout: float | None = None) -> None:
    """Test stub for ``_ensure_running`` that accepts timeout kwargs."""
    _ = timeout


async def _noop() -> None:
    """Run a coroutine that does nothing."""
    return None


@pytest.fixture
def fresh_runner() -> Generator[BLECoroutineRunner, None, None]:
    """Provide an isolated BLECoroutineRunner instance for one test.

    Swap the singleton out for the duration of the test so ownership state is
    not leaked into (or from) other runner tests, and restore the original
    singleton plus zombie counter afterwards.

    Yields
    ------
    BLECoroutineRunner
        A fresh, stopped runner instance.
    """
    original_instance = BLECoroutineRunner._instance

    with _runner_module._zombie_lock:
        saved_zombie_count = _runner_module._zombie_runner_count
        _runner_module._zombie_runner_count = 0

    BLECoroutineRunner._instance = None
    runner = BLECoroutineRunner()

    try:
        yield runner
    finally:
        with runner._instance_lock:
            runner._thread = None
            runner._loop = None
            runner._stop_requested = False
        current = BLECoroutineRunner._instance
        if current is not None and current is not original_instance:
            handler = getattr(current, "_atexit_handler", None)
            if callable(handler):
                _runner_module.atexit.unregister(handler)
            if hasattr(current, "_atexit_registered"):
                current._atexit_registered = False
        BLECoroutineRunner._instance = original_instance
        with _runner_module._zombie_lock:
            _runner_module._zombie_runner_count = saved_zombie_count


def test_restart_does_not_abandon_live_stopping_owner(
    fresh_runner: BLECoroutineRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restart must wait for (or fail on) a live stopping owner, never drop it."""
    runner = fresh_runner
    stopping_owner = _StubbedOwnerThread(retires_on_join=False)
    start_calls: list[bool] = []

    def _record_start_locked() -> threading.Event | None:
        start_calls.append(True)
        return None

    monkeypatch.setattr(
        BLEConfig, "RUNNER_SHUTDOWN_TIMEOUT_SECONDS", 0.01, raising=True
    )
    monkeypatch.setattr(runner, "_start_locked", _record_start_locked)

    with runner._instance_lock:
        runner._thread = cast(Any, stopping_owner)
        runner._loop = None
        runner._stop_requested = True

    with pytest.raises(RuntimeError, match=BLECLIENT_ERROR_LOOP_RESTART_FAILED):
        runner._restart()

    assert start_calls == []
    with runner._instance_lock:
        assert runner._thread is cast(Any, stopping_owner)
    assert stopping_owner.join_calls == [0.01]


def test_restart_reuses_in_progress_start_without_joining(
    fresh_runner: BLECoroutineRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live pre-publication startup is not a stopped runner to join."""
    runner = fresh_runner
    starting_owner = _StubbedOwnerThread(retires_on_join=False)
    ready = threading.Event()
    ready.set()
    starts: list[bool] = []

    def _reuse_start_locked() -> threading.Event:
        starts.append(True)
        return ready

    monkeypatch.setattr(runner, "_start_locked", _reuse_start_locked)
    with runner._instance_lock:
        runner._thread = cast(Any, starting_owner)
        runner._loop = None
        runner._stop_requested = False
        runner._loop_ready = ready

    assert runner._restart() is True
    assert starts == [True]
    assert starting_owner.join_calls == []
    assert runner._thread is cast(Any, starting_owner)


def test_restart_never_waits_on_its_own_starting_thread(
    fresh_runner: BLECoroutineRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even the pre-publication startup path must reject a self-wait."""
    runner = fresh_runner
    called: list[bool] = []

    def _record_start() -> threading.Event:
        called.append(True)
        return threading.Event()

    monkeypatch.setattr(runner, "_start_locked", _record_start)
    with runner._instance_lock:
        runner._thread = threading.current_thread()
        runner._loop = None
        runner._stop_requested = False

    with pytest.raises(RuntimeError, match=BLECLIENT_ERROR_LOOP_RESTART_FAILED):
        runner._restart()
    assert called == []


def test_restart_joins_retiring_owner_before_starting_new_one(
    fresh_runner: BLECoroutineRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restart must join a retiring owner outside the lock before rebuilding."""
    runner = fresh_runner
    retiring_owner = _StubbedOwnerThread(retires_on_join=True)
    ready = threading.Event()
    ready.set()
    start_calls: list[bool] = []

    def _stub_start_locked() -> threading.Event | None:
        start_calls.append(True)
        return ready

    monkeypatch.setattr(
        BLEConfig, "RUNNER_SHUTDOWN_TIMEOUT_SECONDS", 0.01, raising=True
    )
    monkeypatch.setattr(runner, "_start_locked", _stub_start_locked)

    with runner._instance_lock:
        runner._thread = cast(Any, retiring_owner)
        runner._loop = None
        runner._stop_requested = True

    assert runner._restart() is True

    assert start_calls == [True]
    assert retiring_owner.join_calls == [0.01]
    with runner._instance_lock:
        assert runner._thread is None
        assert runner._loop is None


def test_failed_thread_start_publishes_no_owner(
    fresh_runner: BLECoroutineRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A thread whose native start() fails must not remain installed as owner."""
    runner = fresh_runner

    class _FailingStartThread:
        """Thread stub whose native start raises, as under allocation failure."""

        def __init__(self, **_kwargs: Any) -> None:
            """Accept the keyword arguments used by ``_start_locked``."""

        @staticmethod
        def start() -> None:
            """Simulate a rejected native thread start.

            Raises
            ------
            OSError
                Simulates thread allocation failure.
            """
            raise OSError("thread allocation failure")

    monkeypatch.setattr(threading, "Thread", cast(Any, _FailingStartThread))
    with runner._instance_lock:
        runner._pending_futures.clear()
        runner._atexit_registered = True

        with pytest.raises(OSError, match="thread allocation failure"):
            runner._start_locked()

        assert runner._thread is None
        assert runner._loop is None
        assert runner._atexit_registered is False


def test_submit_and_tracking_are_atomic_against_stop(
    fresh_runner: BLECoroutineRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop must not cancel its snapshot between submission and tracking."""
    runner = fresh_runner
    loop_stub = _RunningLoopStub()
    submitted = threading.Event()
    submit_release = threading.Event()
    stopper_finished_early = threading.Event()
    pending: Future[None] = Future()
    scheduled_loops: list[Any] = []
    stop_requested_at_submit: list[bool] = []
    errors: list[Exception] = []

    def _blocking_submit(coro: Any, scheduled_loop: Any) -> Future[None]:
        """Hold the instance lock mid-submission to widen the race window."""
        coro.close()
        scheduled_loops.append(scheduled_loop)
        with runner._instance_lock:
            stop_requested_at_submit.append(runner._stop_requested)
        submitted.set()
        assert submit_release.wait(timeout=2.0)
        return pending

    monkeypatch.setattr(runner, "_ensure_running", _noop_ensure_running)
    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", _blocking_submit)

    with runner._instance_lock:
        runner._loop = cast(Any, loop_stub)
        runner._thread = None
        runner._stop_requested = False

    def _submit() -> None:
        try:
            runner._run_coroutine_threadsafe(_noop())
        except Exception as exc:  # noqa: BLE001 - recorded by the assertion below
            errors.append(exc)

    def _stop() -> None:
        runner._stop()
        stopper_finished_early.set()

    stopper = threading.Thread(target=_stop, name="ownership-stopper")
    submitter = threading.Thread(target=_submit, name="ownership-submitter")
    submitter.start()
    try:
        assert submitted.wait(timeout=2.0)

        stopper.start()
        # While submission is in flight and holds the instance lock, stop()
        # must still be blocked inside its snapshot section.
        stopper.join(timeout=0.05)
        assert not stopper_finished_early.is_set()

        submit_release.set()
        submitter.join(timeout=2.0)
        stopper.join(timeout=2.0)
    finally:
        submit_release.set()
        submitter.join(timeout=2.0)
        if stopper.ident is not None:
            stopper.join(timeout=2.0)
        with runner._instance_lock:
            runner._loop = None
            runner._thread = None

    assert not submitter.is_alive()
    assert not stopper.is_alive()
    assert scheduled_loops == [loop_stub]
    assert stop_requested_at_submit == [False]
    assert errors == []
    # The future was tracked before stop() took its cancellation snapshot.
    assert pending.cancelled()
