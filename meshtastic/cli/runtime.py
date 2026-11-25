"""Serial reconnect and CLI listen-loop runtime helpers."""

from __future__ import annotations

import logging
import time

import meshtastic.serial_interface
from meshtastic._deadline import _remaining_timeout
from meshtastic.mesh_interface import MeshInterface

logger = logging.getLogger(__name__)

# Sleep interval for persistent non-serial listen sessions.
MAIN_LOOP_IDLE_SLEEP_SECONDS = 1000

# Delay before retrying a failed serial reconnect attempt.
SERIAL_RECONNECT_RETRY_SECONDS = 0.5

# Polling interval while a serial listen session remains healthy.
SERIAL_LISTEN_CONNECTED_SLEEP_SECONDS = 2.0

# Maximum wait for an old serial reader thread to stop.
SERIAL_RX_THREAD_JOIN_TIMEOUT_SECONDS = 5.0


def _is_serial_reconnect_client(client: MeshInterface) -> bool:
    """Return True if *client* is a real SerialInterface that supports reconnect.

    Uses a guarded isinstance check that is safe even when tests patch
    ``SerialInterface`` with a MagicMock. Does **not** rely on
    ``devPath`` because auto-detected devices intentionally reset
    ``devPath = None`` during reconnect so port detection can re-run.
    """
    serial_cls = getattr(meshtastic.serial_interface, "SerialInterface", None)
    return isinstance(serial_cls, type) and isinstance(client, serial_cls)


def _serial_transport_is_live(client: MeshInterface) -> bool:
    """Check if a serial interface has a live transport (stream open, reader alive).

    In noProto mode, ``isConnected`` is never set because the protocol handshake
    is skipped. This helper checks transport-level liveness instead: the stream
    exists and is open, and the reader thread is alive.
    """
    stream = getattr(client, "stream", None)
    if stream is None or not getattr(stream, "is_open", True):
        return False
    rx_thread = getattr(client, "_rxThread", None)
    if rx_thread is None or not rx_thread.is_alive():
        return False
    return True


def _serial_should_reconnect(client: MeshInterface) -> bool:
    """Return True if a serial client needs reconnect attention.

    For protocol mode: reconnect when ``isConnected`` is cleared.
    For noProto mode: reconnect when the transport itself is dead.
    """
    if getattr(client, "_wantExit", False):
        return False
    if getattr(client, "noProto", False):
        return not _serial_transport_is_live(client)
    return not client.isConnected.is_set()


def _poll_serial_reconnect(client: MeshInterface) -> None:
    """Attempt one round of serial reconnection, sleeping on failure.

    Catches connection-related errors and retryable MeshInterfaceError.
    Waits for the old reader thread to exit before reconnecting.
    """
    logger.debug("Serial reconnect poll: transport is down, attempting connect...")

    # Wait for the old reader thread to exit before reconnecting
    rx_thread = getattr(client, "_rxThread", None)
    if rx_thread is not None and rx_thread.is_alive():
        rx_thread.join(
            timeout=_remaining_timeout(SERIAL_RX_THREAD_JOIN_TIMEOUT_SECONDS)
        )
        if rx_thread.is_alive():
            logger.warning(
                "Reader thread is still alive after join timeout; delaying reconnect."
            )
            time.sleep(_remaining_timeout(SERIAL_RECONNECT_RETRY_SECONDS))
            return

    try:
        client.connect()  # type: ignore[attr-defined]
        if client.isConnected.is_set() or _serial_transport_is_live(client):
            logger.info("Serial reconnected.")
        else:
            logger.debug("Reconnect returned but interface is not live yet.")
            time.sleep(_remaining_timeout(SERIAL_RECONNECT_RETRY_SECONDS))
    except Exception as exc:
        # Route all exceptions through the serial retry classifier when available.
        # Unconditionally retry OS/connection errors; conditionally retry
        # MeshInterfaceError based on _is_retryable_connect_error().
        if isinstance(exc, (ConnectionError, OSError, TimeoutError)):
            retryable = True
        elif isinstance(exc, MeshInterface.MeshInterfaceError):
            retryable = bool(
                hasattr(client, "_is_retryable_connect_error")
                and client._is_retryable_connect_error(exc)
            )
        else:
            retryable = False

        if retryable:
            logger.debug("Reconnect attempt failed (retryable): %s", exc)
            time.sleep(_remaining_timeout(SERIAL_RECONNECT_RETRY_SECONDS))
        else:
            raise


def _listen_loop_poll_once(client: MeshInterface) -> bool:
    """Execute one iteration of the persistent listen loop.

    Returns True if the caller should ``continue`` immediately (reconnect
    was attempted, timing is self-contained), False if the caller should
    proceed to the next iteration normally (sleep already handled).
    """
    if _is_serial_reconnect_client(client):
        if _serial_should_reconnect(client):
            _poll_serial_reconnect(client)
            return True  # reconnect timing is self-contained, skip main sleep
        time.sleep(SERIAL_LISTEN_CONNECTED_SLEEP_SECONDS)
    else:
        time.sleep(MAIN_LOOP_IDLE_SLEEP_SECONDS)
    return False
