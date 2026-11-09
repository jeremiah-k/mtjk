"""Connection-generation fence for heartbeat sends.

Internal module — not part of stable public API.

A heartbeat marks itself in flight inside one connection lifetime. If the
interface is closed and reconnected while that send is still parked, the
transport write must never land on the replacement transport. The heartbeat
callback records the connection generation at the moment the send is marked
in flight, and the transport write path compares that recorded generation
against the interface's current generation at the point where it binds the
transport object for the write. A mismatch means the send began in a lifetime
that has since been closed, so the frame is dropped.

The recorded generation travels on a thread-local because a heartbeat send is
fully synchronous on one thread (the heartbeat callback thread): the send
runs _send_to_radio -> send_impl -> _write_bytes without ever crossing to
another thread. Sends that do not run under a fence context (everything
except heartbeat timer callbacks) are never fenced, so their behavior is
unchanged.
"""

from __future__ import annotations

import threading

_fence_state = threading.local()


def _connection_fence_begin(generation: int) -> None:
    """Mark the calling thread's send as belonging to the given generation."""
    _fence_state.generation = generation


def _connection_fence_end() -> None:
    """Clear the calling thread's fence marker."""
    _fence_state.generation = None


def _connection_fence_expected() -> int | None:
    """Return the calling thread's fenced generation, or None when unfenced."""
    generation: int | None = getattr(_fence_state, "generation", None)
    return generation


def _connection_fence_is_stale(current_generation: int) -> bool:
    """Return whether the calling send was fenced to an older connection.

    Sends running outside a fence context return ``False``: only heartbeat
    timer callbacks establish a fence, so every other send is unaffected.
    """
    expected = _connection_fence_expected()
    if expected is None:
        return False
    return expected != current_generation
