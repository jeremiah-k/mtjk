"""Best-effort BlueZ stale-connection clearance before central BLE connects.

A central link left open at the OS level (for example by a process that died
without disconnecting) keeps the radio from advertising and blocks both new
central connects and scans until something disconnects it. ``BLEClient.close()``
handles the transport owned by *this* process; nothing clears a link still
owned by a *previous* process. These helpers ask BlueZ, over D-Bus, to
disconnect the one target device before a central connection is opened.

Clearance is best-effort for transport failures: those are logged at debug
level and do not fail the connection attempt. An attempt that does not retire
within its wait budget cannot be confirmed, so it raises
:class:`_ClearanceAbandonedError` — both from the coroutine itself and through
the synchronous bridge — and the caller must refuse a replacement link while
a late ``Disconnect`` could still tear it down. Construction cancellation and
fatal-signal exceptions propagate so the caller can unwind promptly.

Embedders can disable the behavior entirely by setting
``BLEConfig.CLEAR_STALE_CONNECTIONS = False``; the D-Bus bus is then never
opened. Each clearance attempt is bounded by
``BLEConfig.STALE_CONNECTION_DISCONNECT_TIMEOUT`` seconds.

This module is internal to the BLE package and is deliberately not re-exported
from ``meshtastic.interfaces.ble``.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import sys
import threading
from collections.abc import Iterator
from typing import Any

from meshtastic.cancellation import (
    ConnectionCancelledError,
    _check_cancelled,
    _current_event,
    _wait_future,
)
from meshtastic.interfaces.ble.constants import (
    AWAIT_TIMEOUT_BUFFER_SECONDS,
    BLEConfig,
)
from meshtastic.interfaces.ble.utils import sanitize_address

logger = logging.getLogger("meshtastic.ble")

_BLUEZ_SERVICE = "org.bluez"
_OBJECT_MANAGER_INTERFACE = "org.freedesktop.DBus.ObjectManager"
_DEVICE_INTERFACE = "org.bluez.Device1"
_GET_MANAGED_OBJECTS = "GetManagedObjects"
_DISCONNECT_METHOD = "Disconnect"


class _ClearanceAbandonedError(RuntimeError):
    """A queued clearance could not be confirmed retired before abandonment.

    Raised when the synchronous bridge's wait budget expires with the runner
    job still outstanding. The job may have submitted ``Device1.Disconnect``
    already or may do so after the caller resumes, so the caller must not
    establish a replacement link that a late disconnect could tear down.
    """


class _ClearanceAbandonment(threading.Event):
    """Fence caller abandonment and construction cancellation independently."""

    def __init__(self, cancel_event: threading.Event | None) -> None:
        super().__init__()
        self._cancel_event = cancel_event

    def is_set(self) -> bool:
        return super().is_set() or (
            self._cancel_event is not None and self._cancel_event.is_set()
        )


# Suppression flag for code paths that reconnect to a radio they already own.
# Device1.Disconnect is device-global: BlueZ cannot tell a dead process's link
# from a live local application's, so an auto-reconnect loop must never clear.
_stale_clearance_suppressed_flag: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "stale_clearance_suppressed", default=False
)


def _stale_clearance_suppressed() -> bool:
    """Return whether the current context suppresses stale-link clearance.

    The auto-reconnect worker reconnects to a radio this process already owns;
    because ``Device1.Disconnect`` is device-global and BlueZ cannot tell a
    dead process's link from a live local application's, letting the reconnect
    loop clear would make two long-lived processes on the same radio keep
    disconnecting each other. The reconnect worker therefore marks its context
    so ``connect()`` skips clearance. BlueZ may reuse another process's link;
    this guard does not enforce exclusivity. The check runs on the calling thread because the
    bridged D-Bus coroutine executes on the shared runner loop, which has a
    different context.

    Returns
    -------
    bool
        True when explicit connects in this context must not clear.
    """
    return _stale_clearance_suppressed_flag.get()


@contextlib.contextmanager
def _suppress_stale_clearance() -> Iterator[None]:
    """Suppress stale-link clearance for explicit connects in this block.

    The auto-reconnect worker wraps its ``interface.connect(...)`` call with
    this guard: clearing before a reconnect would tear down the very radio the
    loop is trying to re-acquire whenever another local process holds it, and
    two processes auto-reconnecting to the same radio would otherwise keep
    disconnecting each other forever. Suppression is scoped to the block via a
    contextvar token so an exception inside the block cannot leak the flag
    into subsequent explicit connects on the same thread.
    """
    token = _stale_clearance_suppressed_flag.set(True)
    try:
        yield
    finally:
        _stale_clearance_suppressed_flag.reset(token)


def _clear_stale_connection_enabled() -> bool:
    """Return whether stale-connection clearance may run on this host.

    Clearance is a Linux/BlueZ-only behavior and is gated by the
    ``BLEConfig.CLEAR_STALE_CONNECTIONS`` embedder toggle. On other platforms
    this is a cheap no-op check so callers can call the helpers unconditionally.

    Returns
    -------
    bool
        True when clearance may attempt a D-Bus interaction.
    """
    return sys.platform == "linux" and bool(BLEConfig.CLEAR_STALE_CONNECTIONS)


def _variant_value(property_value: Any) -> Any:
    """Unwrap a D-Bus variant into its plain Python value.

    BlueZ ObjectManager snapshots wrap properties in D-Bus variants, but fake
    or alternate transports may provide plain values already.

    Parameters
    ----------
    property_value : Any
        Raw property value from an ObjectManager snapshot.

    Returns
    -------
    Any
        The unwrapped value.
    """
    return property_value.value if hasattr(property_value, "value") else property_value


def _connected_device_paths(managed_objects: Any, normalized_address: str) -> list[str]:
    """Collect BlueZ object paths reporting the address as connected.

    Parameters
    ----------
    managed_objects : Any
        Body of an ``ObjectManager.GetManagedObjects`` reply.
    normalized_address : str
        Address normalized via :func:`sanitize_address`.

    Returns
    -------
    list[str]
        Object paths of ``org.bluez.Device1`` entries whose ``Address`` matches
        and whose ``Connected`` property is truthy.
    """
    paths: list[str] = []
    for path, interfaces in managed_objects.items():
        properties = interfaces.get(_DEVICE_INTERFACE)
        if properties is None:
            continue
        raw_address = properties.get("Address")
        if raw_address is None:
            continue
        device_address = sanitize_address(str(_variant_value(raw_address)))
        if device_address != normalized_address:
            continue
        raw_connected = properties.get("Connected")
        if raw_connected is None or not bool(_variant_value(raw_connected)):
            continue
        paths.append(str(path))
    return paths


def _select_device_path(paths: list[str], adapter: str | None) -> str:
    """Choose deterministically among BlueZ device paths for one address.

    Prefers the requested adapter's instance; otherwise falls back to the
    lexicographically first path so repeated calls pick the same device.

    Parameters
    ----------
    paths : list[str]
        Object paths of matching connected devices.
    adapter : str | None
        BlueZ controller name (e.g. ``hci0``) to prefer, if any.

    Returns
    -------
    str
        The selected device object path.
    """
    if adapter is not None:
        prefix = f"/org/bluez/{adapter}/"
        scoped = [path for path in paths if path.startswith(prefix)]
        if scoped:
            return sorted(scoped)[0]
    return sorted(paths)[0]


async def _disconnect_connected_target(
    normalized_address: str,
    *,
    adapter: str | None,
    aborted: threading.Event | None = None,
) -> bool:
    """Ask BlueZ to disconnect the target device if it reports it connected.

    Opens a private system-bus connection, enumerates devices via
    ``ObjectManager.GetManagedObjects``, and issues ``Device1.Disconnect`` on
    the first matching connected object path.

    Parameters
    ----------
    normalized_address : str
        Target address normalized via :func:`sanitize_address`.
    adapter : str | None
        BlueZ controller name to prefer, if any.
    aborted : threading.Event | None
        Set by the caller when this attempt has been abandoned (for example
        after a synchronous timeout). Work that observes the flag before its
        final submission check skips the disconnect. A request that has passed
        that check is not atomically coordinated with caller abandonment.
        (Default value = None)

    Returns
    -------
    bool
        True when a ``Disconnect`` call was issued and acknowledged.
    """
    # dbus-fast is installed on Linux alongside bleak; other platforms never
    # reach this import because _clear_stale_connection_enabled() gates the
    # callers.
    # pylint: disable=import-outside-toplevel
    from dbus_fast import Message, MessageType
    from dbus_fast.aio import MessageBus
    from dbus_fast.constants import BusType
    from dbus_fast.errors import DBusError

    bus = MessageBus(bus_type=BusType.SYSTEM)
    try:
        await bus.connect()
        reply = await bus.call(
            Message(
                destination=_BLUEZ_SERVICE,
                path="/",
                interface=_OBJECT_MANAGER_INTERFACE,
                member=_GET_MANAGED_OBJECTS,
            )
        )
        if reply is None or reply.message_type == MessageType.ERROR:
            logger.debug(
                "BlueZ ObjectManager query failed while clearing stale "
                "connection to %s",
                normalized_address,
            )
            return False
        connected_paths = _connected_device_paths(reply.body[0], normalized_address)
        if not connected_paths:
            return False
        target_path = _select_device_path(connected_paths, adapter)
        # A synchronous caller may time out before this coroutine is serviced.
        # Do not let a delayed BlueZ callback disconnect a subsequent link.
        if aborted is not None and aborted.is_set():
            return False
        disconnect_reply = await bus.call(
            Message(
                destination=_BLUEZ_SERVICE,
                path=target_path,
                interface=_DEVICE_INTERFACE,
                member=_DISCONNECT_METHOD,
            )
        )
        if (
            disconnect_reply is None
            or disconnect_reply.message_type == MessageType.ERROR
        ):
            logger.debug(
                "BlueZ Device1.Disconnect rejected for %s via %s",
                normalized_address,
                target_path,
            )
            return False
        logger.debug(
            "Cleared stale BLE connection to %s via %s", normalized_address, target_path
        )
        return True
    except DBusError:
        logger.debug(
            "D-Bus error while clearing stale BLE connection to %s",
            normalized_address,
            exc_info=True,
        )
        return False
    finally:
        bus.disconnect()


async def _clear_stale_connection(
    address: str, *, adapter: str | None = None, aborted: threading.Event | None = None
) -> bool:
    """Disconnect the target radio at the BlueZ level if it is still connected.

    Intended to run immediately before a central connection is opened to a
    specific device, so a link stranded by a previous process cannot stop the
    radio from accepting the new central. Only the single target address is
    ever touched; nothing else on the bus is disconnected.

    The whole D-Bus interaction (bus connect, enumeration, disconnect) is
    bounded by ``BLEConfig.STALE_CONNECTION_DISCONNECT_TIMEOUT``. Transport
    failures are swallowed and logged at debug level; a wait-budget timeout
    raises :class:`_ClearanceAbandonedError` so the caller refuses a new link
    a late ``Disconnect`` could still tear down.

    Disabled (no bus query, immediate ``False``) when
    ``BLEConfig.CLEAR_STALE_CONNECTIONS`` is False or the host is not
    Linux/BlueZ.

    Parameters
    ----------
    address : str
        BLE address of the target device.
    adapter : str | None
        BlueZ controller name (e.g. ``hci0``) to prefer when the same address
        is present on several adapters. (Default value = None)
    aborted : threading.Event | None
        Abandonment fence shared with :func:`_disconnect_connected_target`.
        (Default value = None)

    Returns
    -------
    bool
        True when a stale connection was found and the ``Disconnect`` call
        succeeded; False when clearance is disabled, the address is
        unusable, or a transport failure occurred.

    Raises
    ------
    _ClearanceAbandonedError
        If the attempt did not retire within the wait budget and can still
        land its ``Device1.Disconnect`` after this call returns.
    """
    if not _clear_stale_connection_enabled():
        return False
    normalized = sanitize_address(address)
    if normalized is None:
        return False
    try:
        return await asyncio.wait_for(
            _disconnect_connected_target(normalized, adapter=adapter, aborted=aborted),
            timeout=BLEConfig.STALE_CONNECTION_DISCONNECT_TIMEOUT,
        )
    except TimeoutError as exc:
        raise _ClearanceAbandonedError(
            "stale-connection clearance did not retire within the D-Bus budget"
        ) from exc
    except Exception:  # noqa: BLE001 - transport failures are best-effort
        logger.debug(
            "Stale BLE connection clearance failed for %s",
            normalized,
            exc_info=True,
        )
        return False


def _clear_stale_connection_before_central_connect(
    address: str | None, *, adapter: str | None = None
) -> bool:
    """Run :func:`_clear_stale_connection` from synchronous code on the shared runner.

    The coroutine is bridged through the shared ``BLECoroutineRunner`` loop
    (the same loop ``BLEClient`` uses); no new event loop or runner is created.
    Transport failures return False. An inner wait-budget timeout arrives
    through the future result as :class:`_ClearanceAbandonedError` and
    propagates. Construction cancellation and fatal-signal
    exceptions propagate after the abandoned job is fenced and cancelled.

    The bridged job checks an abandonment fence: on any exit
    other than a completed result — failure, timeout, or a BaseException
    unwinding through the wait (a signal handler's SystemExit, a
    KeyboardInterrupt) — an abandonment event is set before the submission is
    cancelled, and the coroutine checks that fence before issuing
    ``Device1.Disconnect``. This protects work queued behind a busy runner;
    submission already in progress is not revoked by cancellation. The final
    check and request submission are not atomic with the caller's abandonment,
    so a wait-budget expiry with the job still outstanding raises
    :class:`_ClearanceAbandonedError` instead of returning: the caller must
    not establish a replacement link while the late request can still land.

    Parameters
    ----------
    address : str | None
        BLE address of the target device; None or empty is a no-op.
    adapter : str | None
        BlueZ controller name to prefer, if any. (Default value = None)

    Returns
    -------
    bool
        True when a stale connection was cleared; False otherwise (including
        when the job was never submitted).

    Raises
    ------
    _ClearanceAbandonedError
        If the submitted job did not retire within the wait budget and can
        still issue ``Device1.Disconnect`` after this call returns.
    """
    if not _clear_stale_connection_enabled():
        return False
    normalized = sanitize_address(address)
    if normalized is None:
        return False
    coroutine: Any = None
    future: Any = None
    aborted = _ClearanceAbandonment(_current_event())
    completed = False
    try:
        _check_cancelled()
        # Imported lazily so importing this module (for example from the BLE
        # proxy channel) does not pull the bleak-backed runner into modules
        # that only use the D-Bus coroutine directly.
        # pylint: disable=import-outside-toplevel
        from meshtastic.interfaces.ble.runner import BLECoroutineRunner

        runner = BLECoroutineRunner()
        runner_thread = getattr(runner, "_thread", None)
        if runner_thread is not None and runner_thread is threading.current_thread():
            logger.debug(
                "Skipping stale BLE connection clearance from the runner thread"
            )
            return False
        coroutine = _clear_stale_connection(
            normalized, adapter=adapter, aborted=aborted
        )
        future = runner._run_coroutine_threadsafe(
            coroutine,
            startup_timeout=BLEConfig.RUNNER_LOOP_READY_TIMEOUT_SECONDS,
        )
        cleared = bool(
            _wait_future(
                future,
                BLEConfig.STALE_CONNECTION_DISCONNECT_TIMEOUT
                + AWAIT_TIMEOUT_BUFFER_SECONDS,
            )
        )
        completed = True
        return cleared
    except ConnectionCancelledError:
        raise
    except TimeoutError as exc:
        if future is not None:
            # The coroutine was submitted but the runner did not retire it
            # within the wait budget. It can still submit Device1.Disconnect
            # after this caller resumes, so report an unconfirmed attempt and
            # let the caller refuse to establish a link that could be torn
            # down by the late request.
            raise _ClearanceAbandonedError(
                "stale-connection clearance did not retire before the caller's deadline"
            ) from exc
        logger.debug(
            "Stale BLE connection clearance could not be scheduled for %s",
            normalized,
            exc_info=True,
        )
        return False
    except _ClearanceAbandonedError:
        # The inner job exceeded its own wait budget; the unconfirmed attempt
        # arrives through the future result and must refuse the caller.
        raise
    except Exception:  # noqa: BLE001 - clearance is strictly best-effort
        logger.debug(
            "Stale BLE connection clearance could not run for %s",
            normalized,
            exc_info=True,
        )
        return False
    finally:
        if not completed:
            # Set the abandonment fence before requesting cancellation: a job
            # queued behind a busy runner must not act after this caller
            # proceeds. This runs on every non-completed exit, including a
            # BaseException (a signal handler's SystemExit or a
            # KeyboardInterrupt) unwinding through the wait, so shutdown can
            # never leave a queued BlueZ disconnect alive behind it.
            aborted.set()
            with contextlib.suppress(Exception):
                if future is not None:
                    future.cancel()
                elif coroutine is not None:
                    coroutine.close()
