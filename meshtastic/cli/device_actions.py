"""Connected CLI actions that mutate or administer a node/device.

The public CLI entrypoint remains ``meshtastic.__main__``. This module owns the
internal execution policy for device-oriented actions while dependencies that
are established compatibility/test seams are supplied explicitly by the
entrypoint.
"""

from __future__ import annotations

import contextlib
import getpass
import logging
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, cast

import yaml
from google.protobuf.json_format import MessageToDict, ParseDict, ParseError
from pubsub import pub

import meshtastic.ota
import meshtastic.serial_interface
import meshtastic.tcp_interface
import meshtastic.util
from meshtastic._core_constants import BROADCAST_ADDR, BROADCAST_NUM, LOCAL_ADDR
from meshtastic._topics import LOCKDOWN_STATUS_TOPIC
from meshtastic.cli.context import CliContext, CliExit, _terminate_cli
from meshtastic.key_verification import STAGE_INITIATE as _KV_STAGE_INITIATE
from meshtastic.key_verification import STAGE_NO_VERIFY as _KV_STAGE_NO_VERIFY
from meshtastic.key_verification import STAGE_VERIFY as _KV_STAGE_VERIFY
from meshtastic.key_verification import (
    buildKeyVerificationAdmin as _default_build_key_verification_admin,
)
from meshtastic.key_verification import (
    sendKeyVerification as _default_send_key_verification,
)
from meshtastic.mesh_interface import MeshInterface
from meshtastic.protobuf import (
    admin_pb2,
    connection_status_pb2,
    device_ui_pb2,
    mesh_pb2,
)

logger = logging.getLogger(__name__)


FACTORY_RESET_READY_PROBE_TIMEOUT_SECONDS: float = 20.0
FACTORY_RESET_READY_PROBE_MAX_ATTEMPTS: int = 3
FACTORY_RESET_READY_PROBE_RETRY_DELAY_SECONDS: float = 1.0
FACTORY_RESET_ACCEPTANCE_TIMEOUT_SECONDS: float = 8.0
FACTORY_RESET_ACCEPTANCE_POLL_SECONDS: float = 0.05
FACTORY_RESET_RESEND_INTERVAL_SECONDS: float = 4.0
FACTORY_RESET_MAX_SENDS: int = 4
POSITION_ALTITUDE_MIN = -(1 << 31)
POSITION_ALTITUDE_MAX = (1 << 31) - 1
OTA_REBOOT_WAIT_SECONDS: float = 5.0
OTA_RETRY_DELAY_SECONDS: float = 2.0
OTA_MAX_RETRIES: int = 5


@dataclass(frozen=True, slots=True)
class DeviceActionHooks:
    """Entrypoint-owned dependencies used by device action execution.

    Parameters
    ----------
    cli_exit : CliExit
        User-facing CLI exit function.
    cli_print : Callable[[str], None]
        Quiet-aware CLI reporter.
    set_pref : Callable[[Any, str, Any], bool]
        Preference assignment compatibility seam.
    is_local_destination : Callable[[Any, str], bool]
        Destination classifier.
    send_local_factory_reset_and_wait : Callable[..., Any]
        Local destructive-reset acceptance helper.
    post_factory_reset_ready_probe : Callable[[MeshInterface], bool]
        Best-effort serial readiness probe seam. Not invoked by the built-in
        reset path - a full local reset returns to the shell after its
        acceptance wait; the entrypoint may still call it. Returns ``True``
        once the device reconnected on serial, ``False`` when every probe
        attempt was exhausted without the device returning.
    handle_ota_update : Callable[[MeshInterface, Any, dict[str, Any]], None]
        Wi-Fi OTA execution helper.
    build_key_verification_admin, send_key_verification : Callable[..., Any]
        Key-verification handshake seams retained by the entrypoint.
    build_lockdown_auth, read_lockdown_passphrase_file, send_lockdown_auth,
    validate_lockdown_passphrase : Callable[..., Any]
        Lockdown compatibility seams retained by the entrypoint.
    """

    cli_exit: CliExit
    cli_print: Callable[[str], None]
    set_pref: Callable[[Any, str, Any], bool]
    is_local_destination: Callable[[Any, str], bool]
    send_local_factory_reset_and_wait: Callable[..., Any]
    post_factory_reset_ready_probe: Callable[[MeshInterface], bool]
    handle_ota_update: Callable[[MeshInterface, Any, dict[str, Any]], None]
    build_lockdown_auth: Callable[..., Any]
    read_lockdown_passphrase_file: Callable[[str], bytes]
    send_lockdown_auth: Callable[..., Any]
    validate_lockdown_passphrase: Callable[[bytes], bytes]
    build_key_verification_admin: Callable[..., Any] = (
        _default_build_key_verification_admin
    )
    send_key_verification: Callable[..., Any] = _default_send_key_verification


def _send_local_factory_reset_and_wait(  # pylint: disable=inconsistent-return-statements
    reset_node: Any,
    *,
    full: bool,
    cli_print: Callable[[str], None],
    timeout: float | None = None,
) -> mesh_pb2.MeshPacket | None:
    """Send a local factory reset and wait for ACK/NAK or reboot transport loss.

    Parameters
    ----------
    reset_node : Any
        Local node whose interface transports the destructive reset request.
    full : bool
        Whether to request a complete device factory reset.
    cli_print : Callable[[str], None]
        Quiet-aware CLI reporter used for the acceptance wait message.
    timeout : float | None
        Optional acceptance deadline override.

    Returns
    -------
    mesh_pb2.MeshPacket | None
        Sent request packet, or ``None`` when the node returns no packet.

    Raises
    ------
    ValueError
        If the supplied timeout is not positive.
    MeshInterface.MeshInterfaceError
        If the device rejects the request with a NAK.

    Notes
    -----
    The request is resent on a bounded interval while acceptance remains
    pending; some devices silently drop the first destructive request. The
    overall acceptance deadline is never extended. If no acceptance or reboot
    signal arrives before that deadline, the timeout is logged and the sent
    request is returned; no error is raised.
    """
    reset_interface = reset_node.iface
    acceptance_timeout = (
        FACTORY_RESET_ACCEPTANCE_TIMEOUT_SECONDS if timeout is None else float(timeout)
    )
    if acceptance_timeout <= 0:
        raise ValueError("factory reset acceptance timeout must be positive")

    disconnect_observed = threading.Event()
    request_queued = threading.Event()

    def _on_connection_lost(interface: MeshInterface) -> None:
        """Record transport loss only after the reset request is queued."""
        if request_queued.is_set() and interface is reset_interface:
            disconnect_observed.set()

    acknowledgment = getattr(reset_interface, "_acknowledgment", None)
    reset_acknowledgment = getattr(acknowledgment, "reset", None)
    if callable(reset_acknowledgment):
        reset_acknowledgment()

    pub.subscribe(_on_connection_lost, "meshtastic.connection.lost")
    request: mesh_pb2.MeshPacket | None = None
    request_id: int | None = None
    try:
        request = reset_node.factoryReset(full=full)
        if request is None:
            return None
        # A connection-loss event observed before the send returns is not tied to
        # this request and cannot prove that firmware accepted a destructive reset.
        # Genuine reset transport loss remains visible through the connection and
        # transport snapshots below even if publication raced with this boundary.
        request_queued.set()
        sends = 1
        last_send_monotonic = time.monotonic()

        raw_request_id = getattr(request, "id", None)
        if isinstance(raw_request_id, int) and not isinstance(raw_request_id, bool):
            request_id = raw_request_id if raw_request_id > 0 else None

        missing_transport = object()
        socket_after_send = getattr(reset_interface, "socket", missing_transport)
        stream_after_send = getattr(reset_interface, "stream", missing_transport)
        wait_for_request_ack = getattr(reset_interface, "_wait_for_request_ack", None)
        raise_wait_error = getattr(
            reset_interface, "_raise_wait_error_if_present", None
        )
        retire_wait = getattr(reset_interface, "_retire_wait_request", None)
        scoped_wait_available = (
            request_id is not None
            and callable(wait_for_request_ack)
            and callable(raise_wait_error)
        )

        cli_print("Waiting for factory reset acknowledgment or reboot disconnect")
        deadline = time.monotonic() + acceptance_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            if (
                sends < FACTORY_RESET_MAX_SENDS
                and not disconnect_observed.is_set()
                and time.monotonic() - last_send_monotonic
                >= FACTORY_RESET_RESEND_INTERVAL_SECONDS
            ):
                original_acknowledged = bool(
                    getattr(acknowledgment, "receivedImplAck", False)
                )
                if (
                    not original_acknowledged
                    and request_id is not None
                    and callable(wait_for_request_ack)
                    and wait_for_request_ack(
                        "receivedNak", request_id, timeout_seconds=0
                    )
                ):
                    original_acknowledged = True
                if original_acknowledged:
                    # The original request already carries an acceptance
                    # signal; resending would retire it and duplicate a
                    # destructive request. Stop resending and let the
                    # acceptance checks below return the acknowledged request.
                    sends = FACTORY_RESET_MAX_SENDS
                else:
                    try:
                        resend = reset_node.factoryReset(full=full)
                    except Exception:
                        logger.debug(
                            "Factory reset resend attempt failed; waiting on "
                            "the original request instead.",
                            exc_info=True,
                        )
                        sends = FACTORY_RESET_MAX_SENDS
                        last_send_monotonic = time.monotonic()
                    else:
                        # Retire the original scope and clear stale
                        # acknowledgment state only once the resend produced a
                        # request, so a completion for the original request
                        # stays observable while the resend is in flight.
                        if callable(retire_wait) and request_id is not None:
                            retire_wait("receivedNak", request_id=request_id)
                        if callable(reset_acknowledgment):
                            reset_acknowledgment()
                        last_send_monotonic = time.monotonic()
                        if resend is None:
                            sends = FACTORY_RESET_MAX_SENDS
                        else:
                            request = resend
                            sends += 1
                            request_id = None
                            raw_request_id = getattr(request, "id", None)
                            if (
                                isinstance(raw_request_id, int)
                                and not isinstance(raw_request_id, bool)
                                and raw_request_id > 0
                            ):
                                request_id = raw_request_id
                            scoped_wait_available = (
                                request_id is not None
                                and callable(wait_for_request_ack)
                                and callable(raise_wait_error)
                            )
                            socket_after_send = getattr(
                                reset_interface, "socket", missing_transport
                            )
                            stream_after_send = getattr(
                                reset_interface, "stream", missing_transport
                            )
                            logger.info(
                                "Factory reset acceptance pending; resent "
                                "request (%d/%d)",
                                sends,
                                FACTORY_RESET_MAX_SENDS,
                            )

            if (
                scoped_wait_available
                and callable(wait_for_request_ack)
                and callable(raise_wait_error)
            ):
                completed = wait_for_request_ack(
                    "receivedNak",
                    request_id,
                    timeout_seconds=min(
                        FACTORY_RESET_ACCEPTANCE_POLL_SECONDS, remaining
                    ),
                )
                if completed:
                    raise_wait_error("receivedNak", request_id=request_id)
                    return request
                if bool(getattr(acknowledgment, "receivedImplAck", False)):
                    # An implicit ACK is not request-correlated, so the scoped
                    # registry cannot record it; the legacy flag is the only
                    # carrier. Session-key responses only set receivedAck, so
                    # this flag cannot be satisfied by the session-key
                    # exchange that precedes the destructive request.
                    return request
            else:
                if callable(raise_wait_error):
                    raise_wait_error("receivedNak", request_id=request_id)
                received_ack = bool(getattr(acknowledgment, "receivedAck", False))
                received_implicit_ack = bool(
                    getattr(acknowledgment, "receivedImplAck", False)
                )
                if received_ack or received_implicit_ack:
                    return request
                if bool(getattr(acknowledgment, "receivedNak", False)):
                    raise reset_interface.MeshInterfaceError(
                        "Factory reset request was rejected by the device"
                    )
                time.sleep(min(FACTORY_RESET_ACCEPTANCE_POLL_SECONDS, remaining))

            connected_event = getattr(reset_interface, "isConnected", None)
            connected = True
            is_set = getattr(connected_event, "is_set", None)
            if callable(is_set):
                connected = bool(is_set())

            current_socket = getattr(reset_interface, "socket", missing_transport)
            current_stream = getattr(reset_interface, "stream", missing_transport)
            socket_replaced = (
                socket_after_send is not missing_transport
                and current_socket is not socket_after_send
            )
            stream_replaced = (
                stream_after_send is not missing_transport
                and current_stream is not stream_after_send
            )
            if (
                disconnect_observed.is_set()
                or not connected
                or socket_replaced
                or stream_replaced
            ):
                if callable(raise_wait_error):
                    raise_wait_error("receivedNak", request_id=request_id)
                logger.info(
                    "Device transport changed after local factory reset request; "
                    "treating reboot as command acceptance."
                )
                return request

        if callable(raise_wait_error):
            raise_wait_error("receivedNak", request_id=request_id)
        logger.info(
            "No factory reset acknowledgment observed; the request has been "
            "sent and the node will reboot and apply the reset on its own."
        )
        return request
    finally:
        retire_wait = getattr(reset_interface, "_retire_wait_request", None)
        if callable(retire_wait) and request_id is not None:
            retire_wait("receivedNak", request_id=request_id)
        if callable(reset_acknowledgment):
            reset_acknowledgment()
        try:
            pub.unsubscribe(_on_connection_lost, "meshtastic.connection.lost")
        except Exception:
            logger.debug(
                "Factory reset: failed to remove connection-loss observer.",
                exc_info=True,
            )


@contextlib.contextmanager
def _temporary_instance_attributes(
    instance: Any, overrides: dict[str, Any]
) -> Iterator[None]:
    """Temporarily override instance attributes and restore exact prior state.

    Parameters
    ----------
    instance : Any
        Object whose instance dictionary is temporarily modified.
    overrides : dict[str, Any]
        Attribute/value overrides to install for the duration of the context.

    Yields
    ------
    None
        Control while the overrides are active.
    """
    missing = object()
    instance_values = vars(instance)
    previous_values = {name: instance_values.get(name, missing) for name in overrides}
    try:
        for name, value in overrides.items():
            setattr(instance, name, value)
        yield
    finally:
        for name, previous in previous_values.items():
            if previous is missing:
                with contextlib.suppress(AttributeError):
                    delattr(instance, name)
            else:
                setattr(instance, name, previous)


def _post_factory_reset_ready_probe(interface: MeshInterface) -> bool:
    """Close, then retry the serial readiness probe across a bounded budget.

    A factory reset wipes the device's prefs and forces a reboot. On most
    T-Beam hardware the device returns within ~5s, but the first reconnect
    attempt after BLE/WiFi teardown can race the kernel re-enumerating the
    USB-CDC device. A single 20s probe often gives up just before the device
    reappears, leaving the CLI to report success when the next command is
    the one that actually has to wait. This routine closes the port, then
    attempts the probe up to
    [FACTORY_RESET_READY_PROBE_MAX_ATTEMPTS] times, sleeping
    [FACTORY_RESET_READY_PROBE_RETRY_DELAY_SECONDS] between attempts, with
    each individual attempt bound by
    [FACTORY_RESET_READY_PROBE_TIMEOUT_SECONDS].

    Parameters
    ----------
    interface : MeshInterface
        Connected interface; non-serial transports are ignored.

    Returns
    -------
    bool
        ``True`` once a probe connected to the device, ``False`` when every
        attempt was exhausted without the device returning. Callers decide how
        to treat an exhausted budget; the factory-reset command intentionally
        logs clearly and returns to the shell without failing closed.
    """
    serial_interface_cls = getattr(meshtastic.serial_interface, "SerialInterface", None)
    if not isinstance(serial_interface_cls, type) or not isinstance(
        interface, serial_interface_cls
    ):
        return True

    serial_interface = cast(meshtastic.serial_interface.SerialInterface, interface)

    def _safe_close() -> None:
        try:
            serial_interface.close()
        except Exception:
            logger.debug("Factory reset: serial close failed.", exc_info=True)

    logger.debug("Factory reset: closing serial interface to release port.")
    _safe_close()

    logger.debug(
        "Factory reset: probing reconnect readiness (max_attempts=%d, per_attempt=%.1fs, retry_delay=%.1fs)...",
        FACTORY_RESET_READY_PROBE_MAX_ATTEMPTS,
        FACTORY_RESET_READY_PROBE_TIMEOUT_SECONDS,
        FACTORY_RESET_READY_PROBE_RETRY_DELAY_SECONDS,
    )
    probe_overrides = {
        "_connect_wait_timeout_seconds": FACTORY_RESET_READY_PROBE_TIMEOUT_SECONDS,
        "_connect_retry_budget_seconds": FACTORY_RESET_READY_PROBE_TIMEOUT_SECONDS,
        "_suppress_connect_failure_logging": True,
    }
    probe_start = time.monotonic()
    last_error: Exception | None = None
    attempts_made = 0
    with _temporary_instance_attributes(serial_interface, probe_overrides):
        for attempt_index in range(FACTORY_RESET_READY_PROBE_MAX_ATTEMPTS):
            attempts_made += 1
            try:
                serial_interface.connect()
                logger.info(
                    "Factory reset: device reconnected on attempt %d/%d after %.1fs.",
                    attempts_made,
                    FACTORY_RESET_READY_PROBE_MAX_ATTEMPTS,
                    time.monotonic() - probe_start,
                )
                _safe_close()
                return True
            except Exception as exc:  # noqa: BLE001 - we translate the entire family
                last_error = exc
                # A failed connect can leave the port open or reader threads
                # running; release it so the next attempt fails only when the
                # device is genuinely absent, not because the tty is held.
                _safe_close()
                if attempt_index + 1 < FACTORY_RESET_READY_PROBE_MAX_ATTEMPTS:
                    logger.debug(
                        "Factory reset: probe attempt %d/%d failed (%s); retrying in %.1fs.",
                        attempts_made,
                        FACTORY_RESET_READY_PROBE_MAX_ATTEMPTS,
                        exc,
                        FACTORY_RESET_READY_PROBE_RETRY_DELAY_SECONDS,
                    )
                    time.sleep(FACTORY_RESET_READY_PROBE_RETRY_DELAY_SECONDS)

    elapsed = time.monotonic() - probe_start
    logger.warning(
        "Factory reset: device did not respond after %d probes spanning %.1fs "
        "(last error: %s). The next command may need to reconnect itself, or "
        "the factory reset may not have completed.",
        attempts_made,
        elapsed,
        last_error,
    )
    _safe_close()
    return False


def _handle_ota_update(
    interface: MeshInterface,
    args: Any,
    get_node_kwargs: dict[str, Any],
    *,
    cli_exit: CliExit,
    cli_print: Callable[[str], None],
    is_local_destination: Callable[[Any, str], bool],
) -> None:
    """Initiate a Wi-Fi OTA update for the directly connected local node.

    Parameters
    ----------
    interface : MeshInterface
        TCP interface connected to the target node.
    args : Any
        CLI arguments containing the OTA update path and destination.
    get_node_kwargs : dict[str, Any]
        Additional arguments for retrieving the local node.
    cli_exit : CliExit
        User-facing exit handler.
    cli_print : Callable[[str], None]
        Quiet-aware reporter.
    is_local_destination : Callable[[Any, str], bool]
        Destination classifier.
    """
    if not isinstance(interface, meshtastic.tcp_interface.TCPInterface):
        _terminate_cli(
            cli_exit,
            "Error: OTA update currently requires a TCP connection to the node (use --host).",
            1,
        )
    if not is_local_destination(interface, args.dest):
        _terminate_cli(
            cli_exit,
            "Error: OTA update only supports the directly connected local node; "
            "omit --dest or use --dest ^local.",
            1,
        )

    try:
        ota = meshtastic.ota.ESP32WiFiOTA(args.ota_update, interface.hostname)
    except meshtastic.ota.OTAError as exc:
        _terminate_cli(cli_exit, f"OTA update failed: {exc}", 1)

    cli_print(f"Triggering OTA update on {interface.hostname}...")
    interface.getNode(LOCAL_ADDR, requestChannels=False, **get_node_kwargs).startOTA(
        mode=admin_pb2.OTAMode.OTA_WIFI, ota_file_hash=ota.hash_bytes()
    )
    cli_print("Waiting for device to reboot into OTA mode...")
    time.sleep(OTA_REBOOT_WAIT_SECONDS)

    retries = OTA_MAX_RETRIES
    while retries > 0:
        try:
            ota.update()
            break
        except meshtastic.ota.OTATransportError as exc:
            retries -= 1
            if retries == 0:
                _terminate_cli(cli_exit, f"OTA update failed: {exc}", 1)
            time.sleep(OTA_RETRY_DELAY_SECONDS)
        except meshtastic.ota.OTAError as exc:
            _terminate_cli(cli_exit, f"OTA update failed: {exc}", 1)

    cli_print("\nOTA update completed successfully!")


def _handle_device_actions(context: CliContext, hooks: DeviceActionHooks) -> None:
    """Execute device- and node-administration actions in historical CLI order.

    Parameters
    ----------
    context : CliContext
        Connected invocation state and lifecycle outcome.
    hooks : DeviceActionHooks
        Entrypoint-owned compatibility dependencies.
    """
    interface = context.interface
    args = context.args
    get_node_kwargs = context.get_node_kwargs
    outcome = context.outcome

    if args.set_time is not None:
        interface.getNode(args.dest, False, **get_node_kwargs).setTime(args.set_time)

    if args.remove_position:
        outcome.close_now = True
        outcome.wait_for_ack_nak = True
        hooks.cli_print("Removing fixed position and disabling fixed position setting")
        interface.getNode(args.dest, False, **get_node_kwargs).removeFixedPosition()
    elif any(value is not None for value in (args.setlat, args.setlon, args.setalt)):
        outcome.close_now = True
        outcome.wait_for_ack_nak = True
        if args.setalt is not None:
            try:
                alt = int(args.setalt)
            except (TypeError, ValueError):
                _terminate_cli(
                    hooks.cli_exit, f"ERROR: Invalid altitude value: {args.setalt}", 1
                )
            if not POSITION_ALTITUDE_MIN <= alt <= POSITION_ALTITUDE_MAX:
                _terminate_cli(
                    hooks.cli_exit,
                    "ERROR: altitude must fit the signed 32-bit position field, "
                    f"got: {alt}",
                    1,
                )
            hooks.cli_print(f"Fixing altitude at {alt} meters")
        else:
            alt = 0
        lat = _parse_coordinate(args.setlat, "latitude", hooks)
        lon = _parse_coordinate(args.setlon, "longitude", hooks)
        _validate_coordinate_range(lat, "latitude", 90, hooks)
        _validate_coordinate_range(lon, "longitude", 180, hooks)
        if args.setlat is not None:
            hooks.cli_print(f"Fixing latitude at {lat} degrees")
        if args.setlon is not None:
            hooks.cli_print(f"Fixing longitude at {lon} degrees")
        hooks.cli_print("Setting device position and enabling fixed position setting")
        interface.getNode(args.dest, False, **get_node_kwargs).setFixedPosition(
            lat, lon, alt
        )

    if args.set_owner or args.set_owner_short or args.set_is_unmessageable is not None:
        outcome.close_now = True
        outcome.wait_for_ack_nak = True
        long_name = args.set_owner.strip() if args.set_owner else None
        short_name = args.set_owner_short.strip() if args.set_owner_short else None
        if long_name is not None and not long_name:
            _terminate_cli(
                hooks.cli_exit,
                "ERROR: Long Name cannot be empty or contain only whitespace characters",
                1,
            )
        if short_name is not None and not short_name:
            _terminate_cli(
                hooks.cli_exit,
                "ERROR: Short Name cannot be empty or contain only whitespace characters",
                1,
            )
        if long_name and short_name:
            hooks.cli_print(
                f"Setting device owner to {long_name} and short name to {short_name}"
            )
        elif long_name:
            hooks.cli_print(f"Setting device owner to {long_name}")
        elif short_name:
            hooks.cli_print(f"Setting device owner short to {short_name}")

        unmessagable = None
        if args.set_is_unmessageable is not None:
            unmessagable = (
                meshtastic.util.fromStr(args.set_is_unmessageable)
                if isinstance(args.set_is_unmessageable, str)
                else args.set_is_unmessageable
            )
            hooks.cli_print(f"Setting device owner is_unmessageable to {unmessagable}")
        interface.getNode(args.dest, False, **get_node_kwargs).setOwner(
            long_name=long_name,
            short_name=short_name,
            is_unmessagable=unmessagable,
        )

    _handle_content_updates(context, hooks)
    _handle_position_fields(context, hooks)

    if args.set_ham:
        ham_id = args.set_ham.strip()
        if not ham_id:
            _terminate_cli(
                hooks.cli_exit,
                "ERROR: Ham radio callsign cannot be empty or contain only whitespace characters",
                1,
            )
        outcome.close_now = True
        hooks.cli_print(f"Setting Ham ID to {ham_id} and turning off encryption")
        ham_node = interface.getNode(args.dest, **get_node_kwargs)
        ham_node.setOwner(ham_id, is_licensed=True)
        ham_node.turnOffEncryptionOnPrimaryChannel()

    _handle_reboot_and_reset_actions(context, hooks)
    if outcome.stop_processing:
        return
    _handle_node_database_actions(context)
    _handle_admin_utility_actions(context, hooks)


def _parse_coordinate(
    raw_value: Any,
    coordinate_name: str,
    hooks: DeviceActionHooks,
) -> int | float:
    """Parse one CLI coordinate using the historical dual representation.

    Parameters
    ----------
    raw_value : Any
        Raw argparse value, or ``None`` when the coordinate was omitted. Integer
        values represent protobuf coordinates premultiplied by ``1e7``; decimal
        values represent degrees.
    coordinate_name : str
        Human-readable coordinate name for diagnostics.
    hooks : DeviceActionHooks
        CLI reporting/exit hooks.

    Returns
    -------
    int | float
        Parsed scaled integer or decimal-degree coordinate. Omitted coordinates
        retain the historical ``0.0`` default.
    """
    if raw_value is None:
        return 0.0
    if isinstance(raw_value, bool):
        _terminate_cli(
            hooks.cli_exit,
            f"ERROR: Invalid {coordinate_name} value: {raw_value}",
            1,
        )
    if isinstance(raw_value, (int, float)):
        return raw_value
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        try:
            return float(raw_value)
        except (TypeError, ValueError):
            _terminate_cli(
                hooks.cli_exit,
                f"ERROR: Invalid {coordinate_name} value: {raw_value}",
                1,
            )


def _validate_coordinate_range(
    value: int | float,
    coordinate_name: str,
    degree_limit: int,
    hooks: DeviceActionHooks,
) -> None:
    """Validate a coordinate according to its CLI representation.

    Parameters
    ----------
    value : int | float
        Parsed coordinate. Integers are already scaled by ``1e7``; floats are
        decimal degrees.
    coordinate_name : str
        Human-readable coordinate name for diagnostics.
    degree_limit : int
        Absolute geographic degree limit (90 for latitude, 180 for longitude).
    hooks : DeviceActionHooks
        CLI exit hook used for invalid values.
    """
    if isinstance(value, int):
        scaled_limit = degree_limit * 10_000_000
        if not -scaled_limit <= value <= scaled_limit:
            _terminate_cli(
                hooks.cli_exit,
                f"ERROR: {coordinate_name} premultiplied integer must be between "
                f"{-scaled_limit} and {scaled_limit}, got: {value}",
                1,
            )
        return

    if not -degree_limit <= value <= degree_limit:
        _terminate_cli(
            hooks.cli_exit,
            f"ERROR: {coordinate_name} must be between {-degree_limit} and "
            f"{degree_limit}, got: {value}",
            1,
        )


def _handle_content_updates(context: CliContext, hooks: DeviceActionHooks) -> None:
    """Apply canned-message and ringtone updates in historical CLI order.

    Parameters
    ----------
    context : CliContext
        Connected invocation state. ``close_now`` is enabled for every requested
        one-shot update, while ``wait_for_ack_nak`` is enabled only when the
        firmware exposes the target module and a write packet is actually sent.
    hooks : DeviceActionHooks
        Reporting and compatibility seams used by the updates.
    """
    interface, args, kwargs, outcome = (
        context.interface,
        context.args,
        context.get_node_kwargs,
        context.outcome,
    )
    content_updates = (
        (
            args.set_canned_message,
            mesh_pb2.CANNEDMSG_CONFIG,
            "set_canned_message",
            "Setting canned plugin message to {value}",
            "Canned Message module is excluded by firmware; skipping set.",
        ),
        (
            args.set_ringtone,
            mesh_pb2.EXTNOTIF_CONFIG,
            "set_ringtone",
            "Setting ringtone to {value}",
            "External Notification is excluded by firmware; skipping ringtone set.",
        ),
    )
    for value, module_id, method_name, message, skip_warning in content_updates:
        if not value:
            continue

        # A requested content update remains a completed one-shot CLI operation even
        # when firmware excludes the corresponding module. Only arm the shared ACK
        # wait after a packet is actually sent.
        outcome.close_now = True
        node = interface.getNode(args.dest, False, **kwargs)
        if not node.module_available(module_id):
            logger.warning(skip_warning)
            continue

        outcome.wait_for_ack_nak = True
        hooks.cli_print(message.format(value=value))
        getattr(node, method_name)(value)


def _handle_position_fields(context: CliContext, hooks: DeviceActionHooks) -> None:
    """Read or write the position-field bitmask requested by the CLI.

    Parameters
    ----------
    context : CliContext
        Connected invocation state. ``close_now`` is enabled when position
        fields are read or modified.
    hooks : DeviceActionHooks
        Preference assignment, reporting, and exit seams.
    """
    interface, args, kwargs, outcome = (
        context.interface,
        context.args,
        context.get_node_kwargs,
        context.outcome,
    )
    if args.pos_fields:
        outcome.close_now = True
        node = interface.getNode(args.dest, **kwargs)
        position_config = node.localConfig.position
        all_fields = 0
        try:
            for field in args.pos_fields:
                all_fields |= position_config.PositionFlags.Value(field)
        except ValueError:
            supported = ", ".join(position_config.PositionFlags.keys())
            _terminate_cli(
                hooks.cli_exit,
                "ERROR: Unsupported position field. Supported position fields are: "
                f"{supported}. If no fields are specified, the current value is displayed.",
                1,
            )
        else:
            hooks.cli_print(f"Setting position fields to {all_fields}")
            if not hooks.set_pref(position_config, "position_flags", f"{all_fields:d}"):
                _terminate_cli(
                    hooks.cli_exit,
                    "ERROR: Failed to set position_flags preference.",
                    1,
                )
            hooks.cli_print("Writing modified preferences to device")
            node.writeConfig("position")
    elif args.pos_fields is not None:
        outcome.close_now = True
        position_config = interface.getNode(args.dest, **kwargs).localConfig.position
        field_names = [
            position_config.PositionFlags.Name(bit)
            for bit in position_config.PositionFlags.values()
            if position_config.position_flags & bit
        ]
        hooks.cli_print(" ".join(field_names))


def _handle_reboot_and_reset_actions(
    context: CliContext, hooks: DeviceActionHooks
) -> None:
    """Execute reboot, shutdown, transaction, metadata, and reset actions.

    Parameters
    ----------
    context : CliContext
        Connected invocation state. The handler updates ``close_now``,
        ``wait_for_ack_nak``, ``skip_ack_wait``, and ``stop_processing`` to
        preserve each action's historical lifecycle behavior.
    hooks : DeviceActionHooks
        Reset, reporting, and destination-classification seams.
    """
    interface, args, kwargs, outcome = (
        context.interface,
        context.args,
        context.get_node_kwargs,
        context.outcome,
    )
    reboot_actions = (
        (args.reboot, "reboot"),
        (args.reboot_ota, "rebootOTA"),
        (args.enter_dfu, "enterDFUMode"),
        (args.shutdown, "shutdown"),
    )
    for enabled, method_name in reboot_actions:
        if not enabled:
            continue
        outcome.close_now = True
        outcome.wait_for_ack_nak = True
        outcome.skip_ack_wait = True
        getattr(interface.getNode(args.dest, False, **kwargs), method_name)()

    if args.ota_update:
        outcome.close_now = True
        outcome.skip_ack_wait = True
        hooks.handle_ota_update(interface, args, kwargs)
        outcome.stop_processing = True
        return

    if args.device_metadata:
        outcome.close_now = True
        interface.getNode(args.dest, False, **kwargs).getMetadata()
    if args.begin_edit:
        outcome.close_now = True
        interface.getNode(args.dest, False, **kwargs).beginSettingsTransaction()
    if args.commit_edit:
        outcome.close_now = True
        interface.getNode(args.dest, False, **kwargs).commitSettingsTransaction()

    if args.factory_reset or args.factory_reset_device:
        outcome.close_now = True
        outcome.wait_for_ack_nak = True
        outcome.skip_ack_wait = True
        full = bool(args.factory_reset_device)
        reset_node = interface.getNode(args.dest, False, **kwargs)
        is_local_reset = hooks.is_local_destination(interface, args.dest)
        if is_local_reset:
            hooks.send_local_factory_reset_and_wait(reset_node, full=full)
        else:
            reset_node.factoryReset(full=full)


def _handle_node_database_actions(context: CliContext) -> None:
    """Apply node-database favorite, ignored, removal, and reset actions.

    Parameters
    ----------
    context : CliContext
        Connected invocation state. Requested writes enable ``close_now`` and
        ``wait_for_ack_nak`` for shared finalization.
    """
    interface, args, kwargs, outcome = (
        context.interface,
        context.args,
        context.get_node_kwargs,
        context.outcome,
    )
    actions = (
        (args.remove_node, "removeNode"),
        (args.set_favorite_node, "setFavorite"),
        (args.remove_favorite_node, "removeFavorite"),
        (args.set_ignored_node, "setIgnored"),
        (args.remove_ignored_node, "removeIgnored"),
        (getattr(args, "toggle_muted_node", None), "toggleMutedNode"),
    )
    for value, method_name in actions:
        if value:
            outcome.close_now = True
            outcome.wait_for_ack_nak = True
            getattr(interface.getNode(args.dest, False, **kwargs), method_name)(value)
    if args.reset_nodedb:
        outcome.close_now = True
        outcome.wait_for_ack_nak = True
        interface.getNode(args.dest, False, **kwargs).resetNodeDb()


def _handle_lockdown_action(context: CliContext, hooks: DeviceActionHooks) -> None:
    """Execute the mutually exclusive firmware-lockdown action, if requested.

    Parameters
    ----------
    context : CliContext
        Connected invocation state.
    hooks : DeviceActionHooks
        Entrypoint-owned lockdown and reporting dependencies.
    """
    args = context.args
    lockdown_action = next(
        (
            name
            for name, enabled in (
                ("provision", args.lockdown_provision),
                ("unlock", args.lockdown_unlock),
                ("lock-now", args.lockdown_lock_now),
                ("disable", args.lockdown_disable),
            )
            if enabled
        ),
        None,
    )
    if lockdown_action is None:
        return

    context.outcome.close_now = True
    if not hooks.is_local_destination(context.interface, args.dest):
        _terminate_cli(
            hooks.cli_exit,
            "Lockdown commands apply only to the directly connected local node.",
            1,
        )
    if (
        lockdown_action in {"provision", "lock-now", "disable"}
        and not args.lockdown_yes
    ):
        _confirm_lockdown_action(lockdown_action, hooks)

    if lockdown_action == "lock-now":
        initial_status = _wait_for_initial_lockdown_status(
            context.interface, args.lockdown_wait
        )
        if initial_status is None or initial_status.state not in {
            mesh_pb2.LockdownStatus.LOCKED,
            mesh_pb2.LockdownStatus.UNLOCKED,
        }:
            _terminate_cli(
                hooks.cli_exit,
                "Lock now requires a provisioned device reporting lockdown status.",
                1,
            )

    try:
        passphrase = _read_lockdown_passphrase(args, lockdown_action, hooks)
        auth = hooks.build_lockdown_auth(
            passphrase,
            boots_remaining=args.lockdown_boots,
            valid_until_epoch=args.lockdown_valid_until,
            max_session_seconds=args.lockdown_max_session_seconds,
            lock_now=False,
            disable=lockdown_action == "disable",
        )
    except (OSError, ValueError) as exc:
        _terminate_cli(hooks.cli_exit, f"Invalid lockdown options: {exc}", 1)

    try:
        if lockdown_action == "lock-now":
            # Firmware requires prior authorization on this same connection.
            authorized = hooks.send_lockdown_auth(
                context.interface,
                auth,
                timeout=args.lockdown_wait,
                allow_reboot_without_status=False,
            )
            if (
                authorized is None
                or authorized.state != mesh_pb2.LockdownStatus.UNLOCKED
            ):
                raise RuntimeError(
                    "Lockdown authentication did not authorize this connection."
                )
            auth = hooks.build_lockdown_auth(lock_now=True)
        status = hooks.send_lockdown_auth(
            context.interface,
            auth,
            timeout=args.lockdown_wait,
            allow_reboot_without_status=lockdown_action == "lock-now",
        )
    except (TimeoutError, ValueError, RuntimeError) as exc:
        _terminate_cli(hooks.cli_exit, f"Lockdown command failed: {exc}", 1)

    if status is None:
        hooks.cli_print("Lockdown command accepted; device may already be rebooting.")
        return
    try:
        state_name = mesh_pb2.LockdownStatus.State.Name(status.state)
    except ValueError:
        state_name = f"STATE_{status.state}"
    hooks.cli_print(f"Lockdown status: {state_name}")
    if status.backoff_seconds:
        hooks.cli_print(f"Retry backoff: {status.backoff_seconds}s")
    if status.state == mesh_pb2.LockdownStatus.UNLOCK_FAILED:
        _terminate_cli(hooks.cli_exit, "Lockdown authentication failed.", 1)


def _wait_for_initial_lockdown_status(
    interface: Any, timeout: float
) -> mesh_pb2.LockdownStatus | None:
    """Wait for capability status queued after USB configuration completion."""
    if interface.lockdownStatus is not None:
        return cast(mesh_pb2.LockdownStatus, interface.lockdownStatus)
    event = threading.Event()
    received: mesh_pb2.LockdownStatus | None = None

    def _on_status(
        *, interface: Any, status: mesh_pb2.LockdownStatus, **_kwargs: Any
    ) -> None:
        nonlocal received
        if interface is target_interface:
            received = mesh_pb2.LockdownStatus()
            received.CopyFrom(status)
            event.set()

    target_interface = interface
    pub.subscribe(_on_status, LOCKDOWN_STATUS_TOPIC)
    try:
        if interface.lockdownStatus is None:
            event.wait(max(0.0, timeout))
        cached = interface.lockdownStatus
        return cast(mesh_pb2.LockdownStatus, cached) if cached is not None else received
    finally:
        pub.unsubscribe(_on_status, LOCKDOWN_STATUS_TOPIC)


def _confirm_lockdown_action(lockdown_action: str, hooks: DeviceActionHooks) -> None:
    """Require explicit interactive confirmation for a destructive lockdown action.

    Parameters
    ----------
    lockdown_action : str
        Destructive lockdown action being confirmed.
    hooks : DeviceActionHooks
        CLI-exit seam used to report aborted or non-interactive confirmation.
    """
    try:
        confirmation = (
            input(f"Type 'yes' to confirm lockdown {lockdown_action}: ")
            .strip()
            .casefold()
        )
    except EOFError:
        _terminate_cli(
            hooks.cli_exit,
            "Lockdown confirmation requires an interactive terminal; "
            "pass --lockdown-yes for non-interactive use.",
            1,
        )
    if confirmation != "yes":
        _terminate_cli(hooks.cli_exit, "Aborted.", 1)


def _prompt_lockdown_passphrase(prompt: str, hooks: DeviceActionHooks) -> str:
    """Read a lockdown passphrase without exposing a traceback on closed stdin.

    Parameters
    ----------
    prompt : str
        Prompt shown by :func:`getpass.getpass`.
    hooks : DeviceActionHooks
        CLI-exit seam used to report non-interactive input.

    Returns
    -------
    str
        Passphrase supplied by the operator.
    """
    try:
        return getpass.getpass(prompt)
    except EOFError:
        _terminate_cli(
            hooks.cli_exit,
            "Lockdown passphrase input requires an interactive terminal; "
            "use --lockdown-passphrase-file for non-interactive use.",
            1,
        )


def _read_lockdown_passphrase(
    args: Any, lockdown_action: str, hooks: DeviceActionHooks
) -> bytes:
    """Read and validate the passphrase required by one lockdown action.

    Parameters
    ----------
    args : Any
        Parsed lockdown CLI arguments.
    lockdown_action : str
        Selected action name (for example ``"provision"`` or ``"lock-now"``).
    hooks : DeviceActionHooks
        Passphrase file, validation, and exit seams.

    Returns
    -------
    bytes
        Validated passphrase bytes.
    """
    if args.lockdown_passphrase_file:
        return hooks.read_lockdown_passphrase_file(args.lockdown_passphrase_file)
    if args.lockdown_passphrase is not None:
        if not args.insecure_lockdown_passphrase_on_command_line:
            _terminate_cli(
                hooks.cli_exit,
                "--lockdown-passphrase requires "
                "--insecure-lockdown-passphrase-on-command-line; "
                "prefer an operator-only file or interactive entry.",
                1,
            )
        return hooks.validate_lockdown_passphrase(
            args.lockdown_passphrase.encode("utf-8")
        )

    entered = _prompt_lockdown_passphrase("Lockdown passphrase: ", hooks)
    if lockdown_action == "provision":
        confirmed = _prompt_lockdown_passphrase(
            "Lockdown passphrase (confirm): ", hooks
        )
        if entered != confirmed:
            _terminate_cli(hooks.cli_exit, "Lockdown passphrases do not match.", 1)
    return hooks.validate_lockdown_passphrase(entered.encode("utf-8"))


def _handle_key_verification_action(
    context: CliContext, hooks: DeviceActionHooks
) -> None:
    """Execute one stage of the firmware key-verification handshake, if requested.

    Parameters
    ----------
    context : CliContext
        Connected invocation state. The action enables ``close_now`` and
        ``stop_processing`` so the one-shot CLI exits after the handshake stage
        completes instead of continuing into information or long-running actions.
    hooks : DeviceActionHooks
        Entrypoint-owned key-verification and reporting dependencies.
    """
    args = context.args
    stage = getattr(args, "key_verify", None)
    if stage is None:
        return

    context.outcome.close_now = True
    context.outcome.stop_processing = True
    remote_nodenum = 0
    if stage == _KV_STAGE_INITIATE:
        remote_nodenum = _resolve_key_verification_peer(context, hooks)

    try:
        request = hooks.build_key_verification_admin(
            stage,
            remote_nodenum=remote_nodenum,
            nonce=args.key_verify_nonce,
            security_number=args.key_verify_security_number,
        )
    except ValueError as exc:
        _terminate_cli(hooks.cli_exit, f"Invalid key-verification options: {exc}", 1)

    try:
        notification = hooks.send_key_verification(
            context.interface, request, timeout=args.key_verify_wait
        )
    except (TimeoutError, RuntimeError, ValueError) as exc:
        _terminate_cli(hooks.cli_exit, f"Key verification failed: {exc}", 1)

    _report_key_verification_notification(notification, stage, hooks)


def _resolve_key_verification_peer(
    context: CliContext, hooks: DeviceActionHooks
) -> int:
    """Resolve and validate the remote peer named by ``--dest``.

    Parameters
    ----------
    context : CliContext
        Connected invocation state carrying the parsed destination.
    hooks : DeviceActionHooks
        CLI-exit seam used to report unusable destinations.

    Returns
    -------
    int
        Node number of the remote peer to verify.
    """
    dest = getattr(context.args, "dest", None)
    if not dest or dest in (BROADCAST_ADDR, LOCAL_ADDR):
        _terminate_cli(
            hooks.cli_exit,
            "key-verification initiation requires --dest naming the remote peer.",
            1,
        )
    try:
        nodenum = meshtastic.util.toNodeNum(dest)
    except ValueError:
        _terminate_cli(
            hooks.cli_exit, f"Could not parse --dest {dest!r} as a node id.", 1
        )
    if nodenum == BROADCAST_NUM:
        _terminate_cli(
            hooks.cli_exit,
            "key verification cannot target the broadcast address.",
            1,
        )
    my_info = getattr(context.interface, "myInfo", None)
    if my_info is not None and nodenum == my_info.my_node_num:
        _terminate_cli(
            hooks.cli_exit,
            "key verification verifies a remote peer, not the local node.",
            1,
        )
    return nodenum


def _render_key_verification_notification(
    notification: mesh_pb2.ClientNotification | None,
    stage: str,
    cli_print: Callable[[str], None],
) -> None:
    """Render one key-verification notification through a CLI print function."""
    if notification is None:
        decision = "accepted" if stage == _KV_STAGE_VERIFY else "rejected"
        if stage in (_KV_STAGE_VERIFY, _KV_STAGE_NO_VERIFY):
            cli_print(f"Key-verification decision sent: {decision}.")
        else:
            cli_print(
                "Key-verification stage sent; the device reported no notification."
            )
        return
    if notification.HasField("key_verification_number_inform"):
        inform = notification.key_verification_number_inform
        cli_print(
            f"Security number for {inform.remote_longname}: "
            f"{inform.security_number:06d}"
        )
        cli_print(
            "Share this number out of band with the remote operator, then wait for "
            "the final verification-character confirmation before accepting or rejecting."
        )
    elif notification.HasField("key_verification_number_request"):
        request = notification.key_verification_number_request
        cli_print(
            f"{request.remote_longname} requests the security number shown on "
            "that node; reply with --key-verify provide "
            f"--key-verify-nonce {request.nonce} "
            "--key-verify-security-number NNNNNN."
        )
    elif notification.HasField("key_verification_final"):
        final = notification.key_verification_final
        cli_print(
            f"Final key-verification confirmation with {final.remote_longname} is ready."
        )
        if final.verification_characters:
            cli_print(f"Verification characters: {final.verification_characters}")
        cli_print(
            "Compare the final confirmation on both nodes, then accept with "
            f"--key-verify verify --key-verify-nonce {final.nonce} (or reject with "
            f"--key-verify no-verify --key-verify-nonce {final.nonce})."
        )
    else:
        cli_print(f"Key-verification notification: {notification.message}")


def _report_key_verification_notification(
    notification: mesh_pb2.ClientNotification | None,
    stage: str,
    hooks: DeviceActionHooks,
) -> None:
    """Print the device's key-verification progress notification, if any."""
    _render_key_verification_notification(notification, stage, hooks.cli_print)


def _handle_admin_utility_actions(
    context: CliContext, hooks: DeviceActionHooks
) -> None:
    """Run preference backups, file deletion, input events, and status reads.

    Parameters
    ----------
    context : CliContext
        Connected invocation state. Each requested action enables ``close_now``
        plus ACK/NAK finalization for shared lifecycle handling.
    hooks : DeviceActionHooks
        CLI reporting and exit seams.
    """
    args = context.args
    interface = context.interface
    kwargs = context.get_node_kwargs
    outcome = context.outcome

    backup_actions = (
        (getattr(args, "backup_preferences", None), "backupPreferences", "Backing up"),
        (getattr(args, "restore_preferences", None), "restorePreferences", "Restoring"),
        (
            getattr(args, "remove_backup_preferences", None),
            "removeBackupPreferences",
            "Removing",
        ),
    )
    for location, method_name, verb in backup_actions:
        if location is None:
            continue
        outcome.close_now = True
        outcome.wait_for_ack_nak = True
        hooks.cli_print(f"{verb} preferences ({location})")
        getattr(interface.getNode(args.dest, False, **kwargs), method_name)(location)

    delete_file = getattr(args, "delete_file", None)
    if delete_file:
        outcome.close_now = True
        outcome.wait_for_ack_nak = True
        hooks.cli_print(f"Deleting file {delete_file}")
        interface.getNode(args.dest, False, **kwargs).deleteFile(delete_file)

    input_event = getattr(args, "send_input_event", None)
    if input_event is not None:
        outcome.close_now = True
        outcome.wait_for_ack_nak = True
        kb_char = getattr(args, "input_kb_char", None)
        char_code = 0
        if kb_char is not None:
            if len(kb_char) != 1:
                _terminate_cli(
                    hooks.cli_exit,
                    "ERROR: --input-kb-char accepts exactly one character.",
                    1,
                )
            char_code = ord(kb_char)
            if char_code > 0xFF:
                _terminate_cli(
                    hooks.cli_exit,
                    "ERROR: --input-kb-char must fit the firmware 8-bit keyboard field.",
                    1,
                )
        interface.getNode(args.dest, False, **kwargs).sendInputEvent(
            input_event,
            kb_char=char_code,
            touch_x=getattr(args, "input_touch_x", 0) or 0,
            touch_y=getattr(args, "input_touch_y", 0) or 0,
        )

    if getattr(args, "request_connection_status", False):
        outcome.close_now = True
        status = interface.getNode(
            args.dest, False, **kwargs
        ).requestDeviceConnectionStatus()
        if status is None:
            _terminate_cli(
                hooks.cli_exit,
                "No device connection status response received; "
                "firmware must support device connection-status queries.",
                1,
            )
        _print_device_connection_status(status, hooks)

    if getattr(args, "get_ui_config", False):
        outcome.close_now = True
        ui_config = interface.getNode(args.dest, False, **kwargs).requestUiConfig()
        if ui_config is None:
            _terminate_cli(
                hooks.cli_exit,
                "No device UI configuration response received; "
                "firmware must support device UI configuration.",
                1,
            )
        hooks.cli_print(_yaml_dump_ui_config(ui_config))

    store_ui = getattr(args, "store_ui_config", None)
    if store_ui:
        outcome.close_now = True
        outcome.wait_for_ack_nak = True
        config = _load_ui_config_document(store_ui, hooks)
        interface.getNode(args.dest, False, **kwargs).storeUiConfig(config)


def _print_device_connection_status(
    status: connection_status_pb2.DeviceConnectionStatus, hooks: DeviceActionHooks
) -> None:
    """Print each transport's connection state reported by the device.

    Parameters
    ----------
    status : connection_status_pb2.DeviceConnectionStatus
        Aggregated per-transport status from the node.
    hooks : DeviceActionHooks
        Quiet-aware CLI reporter.
    """
    for section in ("wifi", "ethernet"):
        if not status.HasField(section):
            continue
        sub = getattr(status, section)
        state = "unknown"
        extras: list[str] = []
        if sub.HasField("status"):
            network = sub.status
            state = "connected" if network.is_connected else "disconnected"
            if network.ip_address:
                extras.append(_ip4_to_str(network.ip_address))
            if network.is_mqtt_connected:
                extras.append("mqtt")
            if network.is_syslog_connected:
                extras.append("syslog")
        if section == "wifi":
            if sub.ssid:
                extras.append(f"ssid {sub.ssid}")
            if sub.rssi:
                extras.append(f"rssi {sub.rssi}")
        detail = f" ({', '.join(extras)})" if extras else ""
        hooks.cli_print(f"{section}: {state}{detail}")
    if status.HasField("bluetooth"):
        bluetooth = status.bluetooth
        state = "connected" if bluetooth.is_connected else "disconnected"
        extras = [f"rssi {bluetooth.rssi}"] if bluetooth.rssi else []
        detail = f" ({', '.join(extras)})" if extras else ""
        hooks.cli_print(f"bluetooth: {state}{detail}")
    if status.HasField("serial"):
        serial = status.serial
        state = "connected" if serial.is_connected else "disconnected"
        hooks.cli_print(f"serial: {state} (baud {serial.baud})")


def _ip4_to_str(address: int) -> str:
    """Format a packed 32-bit IPv4 address as dotted quad text."""
    return ".".join(str((address >> shift) & 0xFF) for shift in (0, 8, 16, 24))


def _yaml_dump_ui_config(config: device_ui_pb2.DeviceUIConfig) -> str:
    """Render a DeviceUIConfig as YAML for display and later re-import."""
    return yaml.safe_dump(MessageToDict(config), sort_keys=False)


def _load_ui_config_document(
    path: str, hooks: DeviceActionHooks
) -> device_ui_pb2.DeviceUIConfig:
    """Load a DeviceUIConfig from a YAML document.

    Parameters
    ----------
    path : str
        YAML file produced by ``--get-ui-config``.
    hooks : DeviceActionHooks
        CLI exit seam used to report unreadable or invalid documents.

    Returns
    -------
    device_ui_pb2.DeviceUIConfig
        Parsed configuration ready to store.
    """
    try:
        with open(path, encoding="utf8") as file:
            document = yaml.safe_load(file.read())
    except (OSError, yaml.YAMLError, UnicodeDecodeError) as exc:
        _terminate_cli(hooks.cli_exit, f"ERROR: Failed to read UI config: {exc}", 1)
    if not isinstance(document, dict):
        _terminate_cli(
            hooks.cli_exit, "ERROR: UI config YAML must be a mapping/dictionary.", 1
        )
    config = device_ui_pb2.DeviceUIConfig()
    try:
        ParseDict(document, config, ignore_unknown_fields=False)
    except (ParseError, TypeError) as exc:
        _terminate_cli(
            hooks.cli_exit, f"ERROR: Invalid device UI configuration: {exc}", 1
        )
    return config
