"""TCPInterface class for communicating with Meshtastic devices over TCP connections.

This module provides the TCPInterface class which handles communication with
Meshtastic devices via TCP/IP network connections.
"""

# pylint: disable=R0917
import contextlib
import logging
import math
import select
import socket
import threading
import time
from typing import IO, Any, Callable

from meshtastic.stream_interface import (
    WRITE_PROGRESS_TIMEOUT_SECONDS,
    StreamInterface,
)
from meshtastic.transport_retry import (
    _exponential_retry_delay,
    _plan_counted_retry,
    _sleep_interruptibly,
)

DEFAULT_TCP_PORT = 4403
TCP_CLOSE_DRAIN_TIMEOUT_SECONDS = 0.25
TCP_IO_EXCEPTIONS: tuple[type[BaseException], ...] = (
    OSError,
    ValueError,
    TypeError,
)
logger = logging.getLogger(__name__)
_TRANSPORT_FD_STATE_VALUEERROR_MARKERS: tuple[str, ...] = (
    "fd is none",
    "file descriptor cannot be a negative integer",
    "invalid file descriptor",
    "negative file descriptor",
    "bad file descriptor",
    "closed file",
)


def _is_transport_fd_state_value_error(exc: ValueError) -> bool:
    """Return whether a ValueError indicates a transient fd/state race."""
    message = str(exc).casefold()
    return any(marker in message for marker in _TRANSPORT_FD_STATE_VALUEERROR_MARKERS)


class TCPInterface(StreamInterface):
    """Interface class for meshtastic devices over a TCP link."""

    DEFAULT_CONNECT_TIMEOUT = 10.0
    CONNECT_TIMEOUT_ERROR = (
        "connectTimeout must be a positive finite number (bool values are not accepted), "
        "got {0!r} (type: {1})"
    )
    SOCKET_NOT_CONNECTED_ERROR = "TCP socket is closed or not connected"
    SOCKET_READ_DEAD_ERROR_LOG = "Socket read error, treating as dead socket: %s"
    READ_LENGTH_ERROR = "length must be a positive int, got {value!r}"
    WRITE_TIMEOUT_ERROR = "TCP write timed out waiting for socket readiness"
    WRITE_NO_PROGRESS_ERROR = "TCP write returned no bytes"
    CONNECT_SHUTTING_DOWN_ERROR = "Cannot connect to {}: interface is shutting down"
    RECONNECT_DISABLED_AFTER_FATAL_ERROR = (
        "Cannot connect to {}: reconnect disabled after fatal disconnect"
    )
    DEFAULT_MAX_RECONNECT_ATTEMPTS = 8
    DEFAULT_RECONNECT_BACKOFF = 1.6
    DEFAULT_RECONNECT_BASE_DELAY = 1.0
    DEFAULT_RECONNECT_MAX_DELAY = 30.0
    DEFAULT_RECONNECT_SLEEP_SLICE = 0.25

    def __init__(
        self,
        hostname: str,
        debugOut: IO[str] | Callable[[str], Any] | None = None,
        noProto: bool = False,
        connectNow: bool = True,
        portNumber: int = DEFAULT_TCP_PORT,
        noNodes: bool = False,
        timeout: float = 300.0,
        connectTimeout: float | None = DEFAULT_CONNECT_TIMEOUT,
    ) -> None:
        """Initialize a TCPInterface for a meshtastic device and optionally establish a TCP connection.

        Parameters
        ----------
        hostname : str
            Hostname or IP address of the device to connect to.
        debugOut : IO[str] | Callable[[str], Any] | None
            Optional debug output stream or callable; passed to the base class. (Default value = None)
        noProto : bool
            If True, disable protocol handling. (Default value = False)
        connectNow : bool
            If True, attempt to open the TCP connection during initialization. (Default value = True)
        portNumber : int
            TCP port to connect to (default: DEFAULT_TCP_PORT).
        noNodes : bool
            If True, do not populate node state. (Default value = False)
        timeout : float
            Request/response timeout in seconds (default: 300.0).
        connectTimeout : float | None
            Timeout in seconds for socket connect attempts (default: 10.0).
            ``None`` omits the timeout parameter, allowing the platform socket
            default to apply.
        """
        if connectTimeout is not None:
            if (
                isinstance(connectTimeout, bool)
                or not isinstance(connectTimeout, (int, float))
                or not math.isfinite(connectTimeout)
                or connectTimeout <= 0
            ):
                raise ValueError(
                    self.CONNECT_TIMEOUT_ERROR.format(
                        connectTimeout, type(connectTimeout).__name__
                    )
                )

        self.stream = None
        self._provides_own_stream = True

        self.hostname: str = hostname
        self.portNumber: int = portNumber
        self._connect_now: bool = connectNow
        self._connect_timeout: float | None = connectTimeout
        # Pre-assign base-class attributes so __repr__ stays safe even if
        # myConnect() raises before StreamInterface.__init__ runs.
        self.debugOut = debugOut
        self.noProto = noProto
        self.noNodes = noNodes

        self.socket: socket.socket | None = None
        self._reconnect_attempts = 0
        self._max_reconnect_attempts = self.DEFAULT_MAX_RECONNECT_ATTEMPTS
        self._reconnect_backoff = self.DEFAULT_RECONNECT_BACKOFF
        self._reconnect_base_delay = self.DEFAULT_RECONNECT_BASE_DELAY
        self._reconnect_max_delay = self.DEFAULT_RECONNECT_MAX_DELAY
        self._reconnect_lock = threading.RLock()
        self._reconnect_attempt_lock = threading.Lock()
        self._reconnect_attempt_in_progress = False
        self._fatal_disconnect = False

        if connectNow:
            self.myConnect()

        try:
            super().__init__(
                debugOut=debugOut,
                noProto=noProto,
                connectNow=connectNow,
                noNodes=noNodes,
                timeout=timeout,
            )
        except Exception:
            # myConnect() runs before base init so ensure we don't leak socket
            # resources if StreamInterface setup fails.
            sock = self.socket
            if sock is not None:
                self._close_socket_handle(sock)
                with self._reconnect_lock:
                    self.socket = None
            raise

    def __repr__(self) -> str:
        """Return a concise string representation of the TCPInterface instance including hostname and relevant flags.

        Returns
        -------
        str
            A representation showing the hostname and any active options:
            `debugOut`, `noProto`, `connectNow=False` (when configured), socket
            state, a non-default `portNumber`, and `noNodes`.
        """
        rep = f"TCPInterface({self.hostname!r}"
        if self.debugOut is not None:
            rep += f", debugOut={self.debugOut!r}"
        if self.noProto:
            rep += ", noProto=True"
        if not self._connect_now:
            rep += ", connectNow=False"
        if self.socket is None:
            rep += ", socket=None"
        if self.portNumber != DEFAULT_TCP_PORT:
            rep += f", portNumber={self.portNumber!r}"
        if self.noNodes:
            rep += ", noNodes=True"
        rep += ")"
        return rep

    def _socket_shutdown(self, sock: socket.socket | None = None) -> None:
        """Initiate a bidirectional shutdown of the specified socket if one exists.

        When ``sock`` is omitted, operates on ``self.socket``.
        """
        sock_to_shutdown = self.socket if sock is None else sock
        if sock_to_shutdown is not None:
            with contextlib.suppress(OSError):
                sock_to_shutdown.shutdown(socket.SHUT_RDWR)

    def _close_socket_handle(self, sock: socket.socket | None) -> None:
        """Best-effort shutdown/close for a concrete socket handle."""
        if sock is None:
            return
        with contextlib.suppress(Exception):
            self._socket_shutdown(sock)
        with contextlib.suppress(Exception):
            sock.close()

    def _raise_if_connect_blocked_locked(self) -> None:
        """Raise when current state disallows opening a new TCP socket.

        Must be called while holding ``_reconnect_lock``.
        """
        if getattr(self, "_wantExit", False):
            raise ConnectionError(
                self.CONNECT_SHUTTING_DOWN_ERROR.format(self.hostname)
            )
        if self._fatal_disconnect:
            raise ConnectionError(
                self.RECONNECT_DISABLED_AFTER_FATAL_ERROR.format(self.hostname)
            )

    def _connect_socket_if_needed(self) -> None:
        """Open a TCP socket when none is active, with reconnect-safe serialization."""
        while True:
            reconnect_in_progress = False
            with self._reconnect_attempt_lock:
                should_connect = False
                with self._reconnect_lock:
                    if self._reconnect_attempt_in_progress:
                        reconnect_in_progress = True
                    elif self.socket is None:
                        self._raise_if_connect_blocked_locked()
                        should_connect = True
                    else:
                        return
                if should_connect:
                    self.myConnect()
                    return
            if not reconnect_in_progress:
                return
            # Give the in-flight reconnect attempt a chance to finish before
            # reevaluating socket state. A condition variable would avoid polling,
            # but the small sleep keeps this path simple and lock-order-safe.
            time.sleep(self.DEFAULT_RECONNECT_SLEEP_SLICE)

    def _close_socket_if_current(self, sock: socket.socket | None) -> bool:
        """Best-effort socket teardown and conditional state clear.

        Returns
        -------
        bool
            `True` if this call cleared `self.socket` (because it still matched
            `sock`), otherwise `False`.
        """
        if sock is None:
            return False
        self._close_socket_handle(sock)
        with self._reconnect_lock:
            if self.socket is sock:
                self.socket = None
                return True
        return False

    def myConnect(self) -> None:
        """Establish a TCP connection to the instance hostname and port.

        Stores the resulting connected socket on self.socket.
        """
        logger.debug("Connecting to %s", self.hostname)
        server_address = (self.hostname, self.portNumber)
        if self._connect_timeout is None:
            connected_socket = socket.create_connection(server_address)
        else:
            connected_socket = socket.create_connection(
                server_address, timeout=self._connect_timeout
            )
        try:
            connected_socket.settimeout(None)
        except Exception:
            self._close_socket_handle(connected_socket)
            raise
        discard_redundant_socket = False
        closing = False
        with self._reconnect_lock:
            if getattr(self, "_wantExit", False):
                closing = True
            elif self.socket is not None:
                discard_redundant_socket = True
            else:
                self.socket = connected_socket
                self._fatal_disconnect = False
        if closing or discard_redundant_socket:
            self._close_socket_handle(connected_socket)
            if closing:
                raise ConnectionError(
                    self.CONNECT_SHUTTING_DOWN_ERROR.format(self.hostname)
                )
            logger.debug(
                "Dropping redundant TCP socket for %s because a socket is already active.",
                self.hostname,
            )

    @staticmethod
    def _notify_pending_sender_failure(pending_entry: Any, reason: str) -> bool:
        """Best-effort notify a pending sender entry about reconnect queue drop.

        Supports common waiter/future-like interfaces when present. Returns
        True when any notifier was invoked.
        """
        # Future-like objects.
        set_exception = getattr(pending_entry, "set_exception", None)
        if callable(set_exception):
            set_exception(ConnectionError(reason))
            return True

        # Custom waiter patterns.
        set_failed = getattr(pending_entry, "set_failed", None)
        if callable(set_failed):
            set_failed(ConnectionError(reason))
            return True

        signal_set = getattr(pending_entry, "set", None)
        if callable(signal_set):
            # Event-style waiters only receive wakeup semantics; callers must check
            # reconnect/error state separately for failure details.
            signal_set()
            return True
        return False

    def close(self) -> None:
        """Close the TCP connection and stop the reader thread.

        Send the disconnect frame, half-close the send side, and allow a bounded
        reader drain before forcing socket teardown. Join the reader after
        teardown as well so a peer that keeps its send side open cannot strand
        the reader indefinitely.
        """
        logger.debug("Closing TCP stream")
        # Request shutdown using StreamInterface shared cleanup and then perform
        # TCP-specific socket teardown before joining the reader thread.
        self._shared_close()
        with self._reconnect_lock:
            sock = self.socket
            self.socket = None
        if sock is not None:
            # Closing with unread data can discard the final write on some
            # platforms. Preserve the send-side FIN before disposing of the
            # socket, while bounding the wait for a peer that never closes.
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_WR)
            rx_thread = getattr(self, "_rxThread", None)
            if (
                rx_thread is not None
                and rx_thread is not threading.current_thread()
                and rx_thread.is_alive()
            ):
                rx_thread.join(timeout=TCP_CLOSE_DRAIN_TIMEOUT_SECONDS)
        self._close_socket_handle(sock)

        # Join after socket teardown so a blocking recv() can exit promptly.
        self._join_reader_thread()

    def connect(self) -> None:
        """Ensure socket availability, then run shared StreamInterface startup.

        Raises
        ------
        ConnectionError
            If connect is attempted while shutdown is in progress.
        ConnectionError
            If reconnect has been disabled after a fatal disconnect.
        """
        self._connect_socket_if_needed()
        super().connect()

    def _write_bytes(self, b: bytes) -> None:
        """Send the full byte sequence over the TCP socket with progress timeout protection.

        Uses ``select.select`` to wait for socket writability before each partial
        send so this method cannot block indefinitely inside a single ``send()``
        call.

        Parameters
        ----------
        b : bytes
            Bytes to send.

        Raises
        ------
        ConnectionError
            If the TCP socket is missing or disconnected.
        OSError
            If the underlying socket write fails.
        """
        # Intentionally snapshot without _reconnect_lock; concurrent close/reconnect
        # is handled by sendall() exceptions and _close_socket_if_current().
        sock = self.socket
        if sock is None:
            raise ConnectionError(self.SOCKET_NOT_CONNECTED_ERROR)
        # Validate payload shape before entering socket-recovery handling so
        # caller contract errors do not reset an otherwise healthy socket.
        payload = memoryview(b)
        try:
            total_sent = 0
            write_deadline = time.monotonic() + WRITE_PROGRESS_TIMEOUT_SECONDS
            while total_sent < len(payload):
                remaining = write_deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(self.WRITE_TIMEOUT_ERROR)
                _, writable, _ = select.select([], [sock], [], remaining)
                if not writable:
                    raise TimeoutError(self.WRITE_TIMEOUT_ERROR)
                sent = sock.send(payload[total_sent:])
                if sent <= 0:
                    raise OSError(self.WRITE_NO_PROGRESS_ERROR)
                total_sent += sent
                write_deadline = time.monotonic() + WRITE_PROGRESS_TIMEOUT_SECONDS
        except TCP_IO_EXCEPTIONS as ex:
            logger.warning(
                "TCP write failed (%d bytes), resetting socket: %s", len(b), ex
            )
            if self._close_socket_if_current(sock):
                logger.debug(
                    "Reconnect deferred to reader/reconnect path for %s",
                    self.hostname,
                )
            if isinstance(ex, TypeError):
                # Socket backends can surface fd-state races here (for example
                # TypeError("fd is None")) after a descriptor is invalidated
                # during close/reconnect. These cases are intentional,
                # regression-tested transport failures and should continue down
                # the reconnect path as OSError, not be treated as programmer
                # bugs or silently ignored.
                raise OSError(str(ex)) from ex
            if isinstance(ex, ValueError):
                if _is_transport_fd_state_value_error(ex):
                    raise OSError(str(ex)) from ex
                raise
            raise

    def _compute_reconnect_delay(self) -> float:
        """Compute exponential reconnect backoff delay in seconds."""
        return _exponential_retry_delay(
            attempt=self._reconnect_attempts,
            base_delay=self._reconnect_base_delay,
            backoff=self._reconnect_backoff,
            max_delay=self._reconnect_max_delay,
        )

    def _sleep_reconnect_delay(self, delay: float) -> bool:
        """Sleep reconnect delay with frequent shutdown checks.

        Returns
        -------
        bool
            `True` if the full delay elapsed, `False` if interrupted by shutdown.
        """
        return _sleep_interruptibly(
            delay,
            should_abort=lambda: self._wantExit or self._fatal_disconnect,
            sleep_slice=self.DEFAULT_RECONNECT_SLEEP_SLICE,
            monotonic=time.monotonic,
            sleep=time.sleep,
        )

    def _on_fatal_disconnect(self, reason: str) -> None:
        """Mark the interface as fatally disconnected and stop reconnect attempts."""
        attempts = 0
        with self._reconnect_lock:
            if self._fatal_disconnect:
                return
            self._fatal_disconnect = True
            self._wantExit = True
            attempts = self._reconnect_attempts
        logger.error(
            "Stopping TCP reconnect for %s after %d attempts: %s",
            self.hostname,
            attempts,
            reason,
        )

    def _attempt_reconnect(self) -> bool:
        """Attempt to re-establish the TCP socket and rerun protocol startup.

        Returns
        -------
        bool
            `True` if reconnect and post-connect startup succeeded, `False` otherwise.
        """
        if not self._reconnect_attempt_lock.acquire(  # pylint: disable=consider-using-with
            blocking=False
        ):
            logger.debug("Reconnect already in progress for %s", self.hostname)
            return False
        released_attempt_lock = False
        owns_reconnect_attempt = False
        try:
            abort_reconnect = False
            should_mark_fatal = False
            attempt_number = 0
            delay = 0.0
            with self._reconnect_lock:
                if self._reconnect_attempt_in_progress:
                    abort_reconnect = True
                elif self._wantExit or self._fatal_disconnect:
                    abort_reconnect = True
                else:
                    retry_decision = _plan_counted_retry(
                        completed_attempts=self._reconnect_attempts,
                        max_attempts=self._max_reconnect_attempts,
                        base_delay=self._reconnect_base_delay,
                        backoff=self._reconnect_backoff,
                        max_delay=self._reconnect_max_delay,
                    )
                    if not retry_decision.should_retry:
                        should_mark_fatal = True
                    else:
                        self._reconnect_attempt_in_progress = True
                        owns_reconnect_attempt = True
                        self._reconnect_attempts = retry_decision.attempt
                        attempt_number = retry_decision.attempt
                        delay = retry_decision.delay

            if should_mark_fatal:
                self._on_fatal_disconnect("reconnect retry limit reached")
                return False
            if abort_reconnect:
                return False
            # Release the attempt lock before blocking delay/connect I/O. A per-attempt
            # flag still prevents overlapping reconnect/connect work.
            self._reconnect_attempt_lock.release()
            released_attempt_lock = True

            logger.debug(
                "Reconnect attempt %d/%d for %s in %.1fs",
                attempt_number,
                self._max_reconnect_attempts,
                self.hostname,
                delay,
            )
            if delay > 0.0 and not self._sleep_reconnect_delay(delay):
                return False
            reconnect_ok = False
            connect_failed = False
            try:
                self.myConnect()
            except ConnectionError as connect_ex:
                logger.debug("Reconnect for %s aborted: %s", self.hostname, connect_ex)
                connect_failed = True
            except OSError as connect_ex:
                logger.warning("Reconnect to %s failed: %s", self.hostname, connect_ex)
                connect_failed = True

            if not connect_failed:
                if self.socket is None:
                    logger.warning(
                        "myConnect() returned without setting socket for %s",
                        self.hostname,
                    )
                elif self._wantExit:
                    # close() may race while we reconnect; tear down the new socket.
                    reconnect_sock = self.socket
                    self._close_socket_if_current(reconnect_sock)
                else:
                    # _start_config() can call _send_to_radio(), which drains self.queue and may
                    # block on queue space. During reader-thread reconnect this can deadlock
                    # because queue updates are also processed by the reader thread.
                    if threading.current_thread() is getattr(self, "_rxThread", None):
                        dropped = 0
                        notified = 0
                        drop_reason = (
                            f"Queued send dropped during reconnect for {self.hostname}"
                        )
                        pending_entries: list[Any] = []
                        with self._queue_lock:
                            pending_queue = getattr(self, "queue", None)
                            if isinstance(pending_queue, dict) and pending_queue:
                                dropped = len(pending_queue)
                                pending_entries = list(pending_queue.values())
                                pending_queue.clear()
                        for pending_entry in pending_entries:
                            with contextlib.suppress(Exception):
                                if self._notify_pending_sender_failure(
                                    pending_entry, drop_reason
                                ):
                                    notified += 1
                        if dropped > 0:
                            logger.warning(
                                "Dropped %d queued packet(s) before reconnect config on %s (notified=%d)",
                                dropped,
                                self.hostname,
                                notified,
                            )

                    try:
                        self._start_config()
                    # Keep reader thread alive on unexpected post-reconnect errors.
                    except Exception as config_ex:  # noqa: BLE001
                        logger.warning(
                            "Post-reconnect config for %s failed: %s",
                            self.hostname,
                            config_ex,
                        )
                        reconnect_sock = self.socket
                        self._close_socket_if_current(reconnect_sock)
                    else:
                        with self._reconnect_lock:
                            self._reconnect_attempts = 0
                            self._fatal_disconnect = False
                        reconnect_ok = True

            return reconnect_ok
        finally:
            if not released_attempt_lock:
                self._reconnect_attempt_lock.release()
            if owns_reconnect_attempt:
                with self._reconnect_lock:
                    self._reconnect_attempt_in_progress = False

    # pylint: disable=too-many-return-statements
    # Multiple early returns are intentional here for clear handling of each
    # exit condition: no socket, shutdown requested (checked twice during
    # reconnect wait), reconnect failure, socket not set after reconnect,
    # shutdown during reconnect, successful reconnect, and normal data return.
    def _read_bytes(self, length: int) -> bytes:
        """Read up to `length` bytes from the TCP socket, handling dead connections and automatic reconnection.

        If a socket is present and data is available, returns the received bytes. If the
        socket is detected as disconnected, the method initiates a reconnect sequence and
        returns `b""`. If no socket is available, the method attempts reconnect unless a
        shutdown is already requested.

        Parameters
        ----------
        length : int
            Maximum number of bytes to read. Must be a positive integer
            (booleans are rejected).

        Returns
        -------
        bytes
            The received bytes, or `b""` if no data was returned
            because the socket is absent, a reconnect was started, or shutdown was requested.

        Raises
        ------
        ValueError
            If `length` is not a positive integer (for example, bool,
            non-int, or `<= 0`).
        """
        if isinstance(length, bool) or not isinstance(length, int) or length <= 0:
            raise ValueError(self.READ_LENGTH_ERROR.format(value=length))
        sock = self.socket
        if sock is not None:
            try:
                data = sock.recv(length)
            except OSError as ex:
                logger.debug(self.SOCKET_READ_DEAD_ERROR_LOG, ex)
                data = b""
            except ValueError as ex:
                if not _is_transport_fd_state_value_error(ex):
                    raise
                logger.debug(self.SOCKET_READ_DEAD_ERROR_LOG, ex)
                data = b""
            except TypeError as ex:
                logger.debug(self.SOCKET_READ_DEAD_ERROR_LOG, ex)
                data = b""
            # empty byte indicates a disconnected socket,
            # we need to handle it to avoid an infinite loop reading from null socket
            if data == b"":
                logger.debug("dead socket, re-connecting")
                # cleanup and reconnect socket without breaking reader thread
                if self._close_socket_if_current(sock):
                    self._attempt_reconnect()
                else:
                    logger.debug(
                        "Socket changed during read cleanup, skipping reconnect"
                    )
                return b""
            with self._reconnect_lock:
                # Reset reconnect backoff after any successful read from the active
                # socket so transient disconnects do not keep stale penalties.
                self._reconnect_attempts = 0
                self._fatal_disconnect = False
            return data

        # Socket may be briefly nulled by a concurrent writer failure; try to
        # recover unless shutdown was explicitly requested.
        if not self._wantExit and not self._fatal_disconnect:
            logger.debug("Socket unavailable, attempting reconnect")
            self._attempt_reconnect()
        return b""
