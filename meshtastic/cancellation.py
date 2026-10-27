"""Cooperative cancellation for synchronous interface construction."""

from __future__ import annotations

import contextlib as _contextlib
import errno as _errno
import functools as _functools
import selectors as _selectors
import socket as _socket
import threading as _threading
import time as _time
from collections.abc import Callable as _Callable
from collections.abc import Iterator as _Iterator
from concurrent.futures import Future as _Future
from typing import ParamSpec as _ParamSpec
from typing import Protocol as _Protocol
from typing import TypeVar as _TypeVar
from typing import cast as _cast
from typing import runtime_checkable as _runtime_checkable

from meshtastic._interface_errors import MeshInterfaceError as _MeshInterfaceError

__all__ = ["ConnectionCancelledError"]

_P = _ParamSpec("_P")
_T = _TypeVar("_T")
_POLL_SECONDS = 0.05
# Winsock uses distinct numeric connect-in-progress codes. Fall back to the
# documented WSA values to keep this path testable on non-Windows hosts.
_IN_PROGRESS_CONNECT_ERRORS = frozenset(
    {
        _errno.EINPROGRESS,
        _errno.EWOULDBLOCK,
        _errno.EALREADY,
        _errno.EINTR,
        *(
            getattr(_errno, name, number)
            for name, number in (
                ("WSAEWOULDBLOCK", 10035),
                ("WSAEINPROGRESS", 10036),
                ("WSAEALREADY", 10037),
            )
        ),
    }
)
_scope = _threading.local()


class ConnectionCancelledError(_MeshInterfaceError):
    """Interface construction was cancelled by its caller before adoption."""

    def __init__(
        self, message: str = "Interface connection construction was cancelled"
    ) -> None:
        """Initialize a cancellation outcome compatible with exception transport.

        Parameters
        ----------
        message : str
            Human-readable cancellation diagnosis, retained in exception args.
        """
        super().__init__(message)


def _current_event() -> _threading.Event | None:
    """Return only the calling constructor thread's cancellation event."""
    return _cast(_threading.Event | None, getattr(_scope, "event", None))


def _check_cancelled(cancel_event: _threading.Event | None = None) -> None:
    """Stop a construction wait without claiming a transport failure."""
    event = cancel_event if cancel_event is not None else _current_event()
    if event is not None and event.is_set():
        raise ConnectionCancelledError()


def _uncancelled_cleanup(function: _Callable[_P, _T]) -> _Callable[_P, _T]:
    """Keep native teardown usable after the construction event is set."""

    @_functools.wraps(function)
    def cleanup(*args: _P.args, **kwargs: _P.kwargs) -> _T:
        previous = _current_event()
        _scope.event = None
        try:
            return function(*args, **kwargs)
        finally:
            _scope.event = previous

    return cleanup


def _cancellable_init(function: _Callable[_P, _T]) -> _Callable[_P, _T]:
    """Scope explicit constructor cancellation without retaining it in live I/O."""

    @_functools.wraps(function)
    def initialize(*args: _P.args, **kwargs: _P.kwargs) -> _T:
        event = kwargs.get("cancelEvent")
        if event is not None and not isinstance(event, _threading.Event):
            raise TypeError("cancelEvent must be a threading.Event or None")
        previous = _current_event()
        _scope.event = event if event is not None else previous
        try:
            _check_cancelled()
            try:
                result = function(*args, **kwargs)
                _check_cancelled()
                return result
            except Exception as error:
                selected = _current_event()
                if selected is None or not selected.is_set():
                    raise
                # Concrete constructors also own their normal rollback paths.
                # This guard covers cancellation at the final adoption boundary.
                if args and (event is not None or previous is None):
                    close = getattr(args[0], "close", None)
                    if callable(close):
                        try:
                            _uncancelled_cleanup(close)()
                        except Exception as cleanup_error:
                            cancelled = (
                                error
                                if isinstance(error, ConnectionCancelledError)
                                else ConnectionCancelledError()
                            )
                            cancelled.add_note(
                                "Native connection rollback did not complete"
                            )
                            raise cancelled from cleanup_error
                if isinstance(error, ConnectionCancelledError):
                    raise
                raise ConnectionCancelledError() from error
        finally:
            _scope.event = previous

    return initialize


def _sleep(seconds: float) -> None:
    """Make construction backoff interruptible; preserve ordinary sleep otherwise."""
    event = _current_event()
    if event is None:
        _time.sleep(seconds)
    elif event.wait(seconds):
        raise ConnectionCancelledError()


def _wait_future(future: _Future[_T], timeout: float | None) -> _T:
    """Cancel a native coroutine future when its constructing caller cancels."""
    if _current_event() is None:
        return future.result(timeout)
    deadline = None if timeout is None else _time.monotonic() + timeout
    try:
        while True:
            _check_cancelled()
            remaining = (
                None if deadline is None else max(0.0, deadline - _time.monotonic())
            )
            try:
                result = future.result(
                    _POLL_SECONDS
                    if remaining is None
                    else min(_POLL_SECONDS, remaining)
                )
            except TimeoutError:
                if future.done():
                    # Completion may race the poll timeout. Recover the actual
                    # outcome; result() still propagates native TimeoutError.
                    result = future.result()
                elif deadline is not None and _time.monotonic() >= deadline:
                    raise
                else:
                    continue
            _check_cancelled()
            return result
    except ConnectionCancelledError:
        future.cancel()
        raise


@_runtime_checkable
class _CancellableLock(_Protocol):
    """Structural acquisition contract required only by opt-in cancellation."""

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        """Acquire ownership within the requested native timeout."""
        ...

    def release(self) -> None:
        """Release ownership acquired by this caller."""
        ...


@_contextlib.contextmanager
def _acquire_lock(lock: _contextlib.AbstractContextManager[object]) -> _Iterator[None]:
    """Wait for shared connection ownership without hiding caller cancellation."""
    if _current_event() is None:
        with lock:
            yield
        return
    if not isinstance(lock, _CancellableLock):
        raise TypeError("Connection lock does not support timed acquisition")
    while True:
        _check_cancelled()
        if lock.acquire(timeout=_POLL_SECONDS):
            break
    try:
        _check_cancelled()
        yield
    finally:
        lock.release()


def _connect_socket(address: tuple[str, int], timeout: float | None) -> _socket.socket:
    """Connect without blocking the caller for a whole socket-connect timeout."""
    _check_cancelled()
    addresses = _socket.getaddrinfo(*address, 0, _socket.SOCK_STREAM)
    _check_cancelled()  # System DNS resolution itself is not preemptible.
    error: OSError | None = None
    effective_timeout = _socket.getdefaulttimeout() if timeout is None else timeout
    for family, kind, protocol, _, target in addresses:
        _check_cancelled()
        sock: _socket.socket | None = None
        try:
            sock = _socket.socket(family, kind, protocol)
            sock.setblocking(False)
            code = sock.connect_ex(target)
            if code not in (0, _errno.EISCONN):
                if code not in _IN_PROGRESS_CONNECT_ERRORS:
                    raise OSError(code, "TCP connection failed")
                deadline = (
                    None
                    if effective_timeout is None
                    else _time.monotonic() + effective_timeout
                )
                with _selectors.DefaultSelector() as selector:
                    selector.register(sock, _selectors.EVENT_WRITE)
                    while True:
                        _check_cancelled()
                        remaining = (
                            None if deadline is None else deadline - _time.monotonic()
                        )
                        if remaining is not None and remaining <= 0:
                            raise TimeoutError("TCP connection timed out")
                        if selector.select(
                            _POLL_SECONDS
                            if remaining is None
                            else min(_POLL_SECONDS, remaining)
                        ):
                            code = sock.getsockopt(_socket.SOL_SOCKET, _socket.SO_ERROR)
                            if code:
                                raise OSError(code, "TCP connection failed")
                            break
            _check_cancelled()
            sock.setblocking(True)
            return sock
        except OSError as exc:
            error = exc
            if sock is not None:
                sock.close()
        except BaseException:
            if sock is not None:
                sock.close()
            raise
    if error is not None:
        raise error
    raise OSError("TCP address resolution returned no candidates")
