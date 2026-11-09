"""BLE constants and configuration."""

import logging
import re

logger = logging.getLogger("meshtastic.ble")

# BLE Service and Characteristic UUIDs
SERVICE_UUID: str = "6ba1b218-15a8-461f-9fa8-5dcae273eafd"
TORADIO_UUID: str = "f75c76d2-129e-4dad-a1dd-7866124401e7"
FROMRADIO_UUID: str = "2c55e69e-4993-11ed-b878-0242ac120002"
FROMNUM_UUID: str = "ed9da18c-a800-4f66-a670-aa7547e34453"
LEGACY_LOGRADIO_UUID: str = "6c6fd238-78fa-436b-aacf-15c5be1ef2e2"
LOGRADIO_UUID: str = "5a3d6e49-06e6-4423-9944-e9de8cdf9547"

# Timeout constants
DISCONNECT_TIMEOUT_SECONDS = 5.0
RECEIVE_THREAD_JOIN_TIMEOUT = 2.0
EVENT_THREAD_JOIN_TIMEOUT = 2.0

# Thread/event coordination constants
READ_TRIGGER_EVENT: str = "read_trigger"
RECONNECTED_EVENT: str = "reconnected_event"


# BLE timeout and retry constants
class BLEConfig:
    """Configuration constants for BLE operations."""

    BLE_SCAN_TIMEOUT = 10.0
    RECEIVE_WAIT_TIMEOUT = 0.5
    EMPTY_READ_RETRY_DELAY = 0.1
    EMPTY_READ_MAX_DELAY = 1.0
    EMPTY_READ_MAX_RETRIES = 5
    MALFORMED_NOTIFICATION_THRESHOLD = 10
    TRANSIENT_READ_MAX_RETRIES = 3
    TRANSIENT_READ_RETRY_DELAY = 0.1
    TRANSIENT_READ_MAX_DELAY = 2.0
    SEND_PROPAGATION_DELAY = 0.01
    SERVICE_CHARACTERISTIC_RETRY_COUNT = 2
    SERVICE_CHARACTERISTIC_RETRY_DELAY = 0.1
    GATT_IO_TIMEOUT = 10.0
    NOTIFICATION_START_TIMEOUT: float | None = 10.0
    CONNECTION_TIMEOUT = 60.0
    MANAGEMENT_AWAIT_TIMEOUT: float = 60.0
    EMPTY_READ_WARNING_COOLDOWN = 10.0
    AUTO_RECONNECT_INITIAL_DELAY = 1.0
    AUTO_RECONNECT_MAX_DELAY = 30.0
    AUTO_RECONNECT_BACKOFF = 2.0
    AUTO_RECONNECT_JITTER_RATIO = 0.15
    DBUS_ERROR_RECONNECT_DELAY = 30.0
    CONNECTION_GATE_UNOWNED_STALE_SECONDS = 300.0
    BLECLIENT_EVENT_THREAD_JOIN_TIMEOUT = 2.0
    # Runner configuration
    RUNNER_LOOP_READY_TIMEOUT_SECONDS = 5.0
    RUNNER_ZOMBIE_WARN_THRESHOLD = 3
    RUNNER_SHUTDOWN_TIMEOUT_SECONDS = 2.0
    RUNNER_ATEXIT_SHUTDOWN_TIMEOUT_SECONDS = 1.0
    # Wake the runner loop periodically so queued thread-safe callbacks are
    # serviced even if OS wakeup signaling is delayed or unavailable.
    RUNNER_IDLE_WAKE_INTERVAL_SECONDS = 0.1
    # Connection timeouts
    DIRECT_CONNECT_TIMEOUT_SECONDS = 12.0
    AWAIT_TIMEOUT_BUFFER_SECONDS = 5.0
    # Stale-connection clearance before central connects: when True, an
    # explicit central connect to a specific device first asks BlueZ to
    # disconnect that device if BlueZ still reports it connected. BlueZ
    # Device1.Disconnect is device-global: it cannot tell a dead process's
    # link from another local application's, so clearance also disconnects
    # another local program using that radio. Auto-reconnect never clears.
    # Embedders disable the behavior with CLEAR_STALE_CONNECTIONS = False.
    # STALE_CONNECTION_DISCONNECT_TIMEOUT bounds each clearance attempt; see
    # meshtastic/interfaces/ble/stale_connection.py.
    CLEAR_STALE_CONNECTIONS = True
    STALE_CONNECTION_DISCONNECT_TIMEOUT = 3.0
    # Receive recovery configuration
    MAX_DRAIN_ITERATIONS = 10_000
    RECEIVE_RECOVERY_RAPID_FAILURE_THRESHOLD = 3
    RECEIVE_RECOVERY_MAX_BACKOFF_SEC = 30
    RECEIVE_RECOVERY_STABILITY_RESET_SEC = 60


# Backwards-compatible aliases for legacy module-level constants.
# These are import-time snapshots of BLEConfig class attributes and will not
# reflect dynamic BLEConfig changes made after this module is imported.
BLE_SCAN_TIMEOUT = BLEConfig.BLE_SCAN_TIMEOUT
RECEIVE_WAIT_TIMEOUT = BLEConfig.RECEIVE_WAIT_TIMEOUT
EMPTY_READ_RETRY_DELAY = BLEConfig.EMPTY_READ_RETRY_DELAY
EMPTY_READ_MAX_DELAY = BLEConfig.EMPTY_READ_MAX_DELAY
EMPTY_READ_MAX_RETRIES = BLEConfig.EMPTY_READ_MAX_RETRIES
MALFORMED_NOTIFICATION_THRESHOLD = BLEConfig.MALFORMED_NOTIFICATION_THRESHOLD
TRANSIENT_READ_MAX_RETRIES = BLEConfig.TRANSIENT_READ_MAX_RETRIES
TRANSIENT_READ_RETRY_DELAY = BLEConfig.TRANSIENT_READ_RETRY_DELAY
TRANSIENT_READ_MAX_DELAY = BLEConfig.TRANSIENT_READ_MAX_DELAY
SEND_PROPAGATION_DELAY = BLEConfig.SEND_PROPAGATION_DELAY
SERVICE_CHARACTERISTIC_RETRY_COUNT = BLEConfig.SERVICE_CHARACTERISTIC_RETRY_COUNT
SERVICE_CHARACTERISTIC_RETRY_DELAY = BLEConfig.SERVICE_CHARACTERISTIC_RETRY_DELAY
GATT_IO_TIMEOUT = BLEConfig.GATT_IO_TIMEOUT
NOTIFICATION_START_TIMEOUT = BLEConfig.NOTIFICATION_START_TIMEOUT
CONNECTION_TIMEOUT = BLEConfig.CONNECTION_TIMEOUT
MANAGEMENT_AWAIT_TIMEOUT: float = BLEConfig.MANAGEMENT_AWAIT_TIMEOUT
EMPTY_READ_WARNING_COOLDOWN = BLEConfig.EMPTY_READ_WARNING_COOLDOWN
AUTO_RECONNECT_INITIAL_DELAY = BLEConfig.AUTO_RECONNECT_INITIAL_DELAY
AUTO_RECONNECT_MAX_DELAY = BLEConfig.AUTO_RECONNECT_MAX_DELAY
AUTO_RECONNECT_BACKOFF = BLEConfig.AUTO_RECONNECT_BACKOFF
AUTO_RECONNECT_JITTER_RATIO = BLEConfig.AUTO_RECONNECT_JITTER_RATIO
DBUS_ERROR_RECONNECT_DELAY = BLEConfig.DBUS_ERROR_RECONNECT_DELAY
CONNECTION_GATE_UNOWNED_STALE_SECONDS = BLEConfig.CONNECTION_GATE_UNOWNED_STALE_SECONDS
# Connection timeouts
DIRECT_CONNECT_TIMEOUT_SECONDS = BLEConfig.DIRECT_CONNECT_TIMEOUT_SECONDS
AWAIT_TIMEOUT_BUFFER_SECONDS = BLEConfig.AWAIT_TIMEOUT_BUFFER_SECONDS
# Stale-connection clearance before central connects
CLEAR_STALE_CONNECTIONS = BLEConfig.CLEAR_STALE_CONNECTIONS
STALE_CONNECTION_DISCONNECT_TIMEOUT = BLEConfig.STALE_CONNECTION_DISCONNECT_TIMEOUT
# Receive recovery configuration
MAX_DRAIN_ITERATIONS = BLEConfig.MAX_DRAIN_ITERATIONS
RECEIVE_RECOVERY_RAPID_FAILURE_THRESHOLD = (
    BLEConfig.RECEIVE_RECOVERY_RAPID_FAILURE_THRESHOLD
)
RECEIVE_RECOVERY_MAX_BACKOFF_SEC = BLEConfig.RECEIVE_RECOVERY_MAX_BACKOFF_SEC
RECEIVE_RECOVERY_STABILITY_RESET_SEC = BLEConfig.RECEIVE_RECOVERY_STABILITY_RESET_SEC

# Error message constants
ERROR_TIMEOUT: str = "{0} timed out after {1:.1f} seconds"
ERROR_MULTIPLE_DEVICES: str = (
    "Multiple Meshtastic BLE peripherals found matching '{0}'. Please specify one:\n{1}"
)
ERROR_MULTIPLE_DEVICES_DISCOVERY: str = (
    "Multiple Meshtastic BLE peripherals found. Please specify one:\n{0}"
)
ERROR_READING_BLE: str = "Error reading BLE"
ERROR_NO_PERIPHERAL_FOUND: str = (
    "No Meshtastic BLE peripheral with identifier or address '{0}' found. Try --ble-scan to find it."
)

ERROR_WRITING_BLE: str = (
    "Error writing BLE. This is often caused by missing Bluetooth "
    "permissions (e.g. not being in the 'bluetooth' group) or pairing issues "
    "(did you enter the pairing PIN on your computer?)."
)
ERROR_CONNECTION_FAILED: str = "Connection failed: {0}"
ERROR_NO_PERIPHERALS_FOUND: str = (
    "No Meshtastic BLE peripherals found. Try --ble-scan to find them."
)
ERROR_DISCOVERY_MANAGER_UNAVAILABLE: str = "Discovery manager not available"
ERROR_ADDRESS_RESOLUTION_FAILED: str = "Address resolution failed, cannot create device"
ERROR_INTERFACE_CLOSING: str = "Cannot connect while interface is closing"
ERROR_CONNECTION_SUPPRESSED: str = "Connection suppressed: recently connected elsewhere"
ERROR_STALE_CLEARANCE_ABANDONED: str = (
    "Stale connection clearance did not finish; not starting a new connection "
    "while a late disconnect could tear it down."
)
ERROR_NO_CLIENT_ESTABLISHED: str = "Connection failed: no BLE client established"
ERROR_MANAGEMENT_ADDRESS_EMPTY: str = (
    "Management operations require a non-empty address."
)
ERROR_MANAGEMENT_ADDRESS_REQUIRED: str = (
    "Management operations require an explicit address when no device is connected."
)
ERROR_MANAGEMENT_AWAIT_TIMEOUT_INVALID: str = (
    "pair()/unpair() require a finite positive await_timeout to avoid blocking shutdown."
)
ERROR_MANAGEMENT_TARGET_CHANGED: str = (
    "Management target changed while the operation was pending; retry with an explicit address."
)
ERROR_MANAGEMENT_CONNECTING: str = (
    "Management operations are unavailable while a connection is in progress."
)
ERROR_INVALID_CONNECT_TIMEOUT: str = "invalid connect_timeout: {exc}"
ERROR_TRUST_ADDRESS_NOT_RESOLVED: str = (
    "Cannot trust device: {address!r} did not resolve to a BLE address."
)
ERROR_TRUST_INVALID_TIMEOUT: str = "Trust timeout must be a positive number of seconds."
ERROR_TRUST_LINUX_ONLY: str = (
    "trust() is only supported on Linux hosts via bluetoothctl."
)
ERROR_TRUST_BLUETOOTHCTL_MISSING: str = (
    "Cannot trust device: bluetoothctl was not found."
)
ERROR_TRUST_COMMAND_TIMEOUT: str = (
    "bluetoothctl trust timed out after {timeout:.1f}s for {address}"
)
ERROR_TRUST_COMMAND_FAILED: str = "bluetoothctl trust failed for {address}: {detail}"
CONNECTION_ERROR_EMPTY_ADDRESS: str = "Cannot connect: empty address provided"
CONNECTION_ERROR_INVALIDATED_BY_CONCURRENT_DISCONNECT: str = (
    "Connection invalidated by concurrent disconnect"
)
CONNECTION_ERROR_CLIENT_DISCONNECTED_DURING_FINALIZATION: str = (
    "Connection invalidated: client disconnected during finalization"
)
CONNECTION_ERROR_STATE_TRANSITION_INVALIDATED: str = (
    "Connection invalidated during state transition to connected"
)
CONNECTION_ERROR_LOST_OWNERSHIP: str = (
    "Connection invalidated: interface lost ownership of connected client"
)
UNREACHABLE_ADDRESSED_DEVICES_MSG: str = (
    "Unreachable: all addressed_devices length cases are handled"
)

# BLEClient-specific constants
# Alias preserves legacy access while sourcing value from BLEConfig
BLECLIENT_EVENT_THREAD_JOIN_TIMEOUT: float = (
    BLEConfig.BLECLIENT_EVENT_THREAD_JOIN_TIMEOUT
)
BLECLIENT_ERROR_ASYNC_TIMEOUT: str = "Async operation timed out"
BLECLIENT_ERROR_CANCELLED: str = "Async operation was cancelled"
BLECLIENT_ERROR_CANNOT_PAIR_NOT_INITIALIZED: str = (
    "Cannot pair: BLE client not initialized"
)
BLECLIENT_ERROR_CANNOT_PAIR_UNSUPPORTED: str = (
    "Cannot pair: BLE backend does not support pairing on this platform"
)
BLECLIENT_MANAGEMENT_AWAIT_TIMEOUT: float = BLEConfig.MANAGEMENT_AWAIT_TIMEOUT
BLECLIENT_ERROR_CANNOT_UNPAIR_NOT_INITIALIZED: str = (
    "Cannot unpair: BLE client not initialized"
)
BLECLIENT_ERROR_CANNOT_UNPAIR_UNSUPPORTED: str = (
    "Cannot unpair: BLE backend does not support unpair on this platform"
)
BLECLIENT_ERROR_CANNOT_CONNECT_NOT_INITIALIZED: str = (
    "Cannot connect: BLE client not initialized"
)
BLECLIENT_ERROR_CANNOT_DISCONNECT_NOT_INITIALIZED: str = (
    "Cannot disconnect: BLE client not initialized"
)
BLECLIENT_ERROR_CANNOT_READ_NOT_INITIALIZED: str = (
    "Cannot read: BLE client not initialized"
)
BLECLIENT_ERROR_CANNOT_WRITE_NOT_INITIALIZED: str = (
    "Cannot write: BLE client not initialized"
)
BLECLIENT_ERROR_CANNOT_GET_SERVICES_NOT_INITIALIZED: str = (
    "Cannot get services: BLE client not initialized"
)
BLECLIENT_ERROR_CANNOT_GET_SERVICES_NOT_DISCOVERED: str = (
    "Cannot get services: service discovery not completed"
)
BLECLIENT_ERROR_CANNOT_START_NOTIFY_NOT_INITIALIZED: str = (
    "Cannot start notify: BLE client not initialized"
)
BLECLIENT_ERROR_CANNOT_STOP_NOTIFY_NOT_INITIALIZED: str = (
    "Cannot stop notify: BLE client not initialized"
)
BLECLIENT_ERROR_CANNOT_SCHEDULE_CLOSED: str = (
    "Cannot schedule operation: BLE client is closed"
)
BLECLIENT_ERROR_RUNNER_THREAD_WAIT: str = (
    "Cannot wait on async operation from the runner thread"
)
BLECLIENT_ERROR_ASYNC_OPERATION_FAILED: str = "Async operation failed: {0}"
BLECLIENT_ERROR_FAILED_TO_SCHEDULE: str = "Failed to schedule operation: {0}"
BLECLIENT_ERROR_LOOP_START_FAILED: str = "BLE event loop failed to start"
BLECLIENT_ERROR_LOOP_NOT_AVAILABLE: str = "BLECoroutineRunner loop is not available"
BLECLIENT_ERROR_LOOP_RESTART_FAILED: str = "BLE event loop failed to restart"
BLECLIENT_ERROR_TIMEOUT_PARAM_CONFLICT: str = (
    "Specify only one of timeout or startup_timeout"
)
BLECLIENT_ERROR_SUBSCRIPTION_TOKEN_EXHAUSTED: str = (
    "Subscription token space exhausted."  # noqa: S105
)
BLECLIENT_ERROR_CANNOT_CONNECT_WHILE_CLOSING: str = (
    "Cannot connect while interface is closing"
)
BLECLIENT_ERROR_ALREADY_CONNECTED: str = "Already connected or connection in progress"


_MISSING_ATTR_MSG = "module {!r} has no attribute {!r}"
_UPPER_SNAKE_ATTR_RE = re.compile(r"^[A-Z0-9_]+$")


def __getattr__(name: str) -> object:
    """Dynamically delegate unknown module attributes to BLEConfig.

    This preserves backward-compatible module-level constant access when new
    BLEConfig attributes are introduced without requiring an explicit alias.
    """
    if (
        not name.startswith("__")
        and _UPPER_SNAKE_ATTR_RE.fullmatch(name) is not None
        and name in vars(BLEConfig)
    ):
        return getattr(BLEConfig, name)
    raise AttributeError(_MISSING_ATTR_MSG.format(__name__, name))
