"""Utility functions."""

import base64
import binascii
import glob  # noqa: F401
import logging
import os
import platform  # noqa: F401
import re
import subprocess  # noqa: F401
import sys
import threading
import time
import warnings
from collections.abc import Iterable
from queue import Empty, Queue
from typing import (
    Any,
    Callable,
    NoReturn,
    cast,
)

import packaging.version as pkg_version
import requests
import serial.tools.list_ports  # type: ignore[import-untyped] # noqa: F401
from google.protobuf.json_format import MessageToJson
from google.protobuf.message import Message

import meshtastic._port_discovery as _port_discovery  # pylint: disable=consider-using-from-import
from meshtastic.supported_device import SupportedDevice
from meshtastic.version import get_active_version

# Keep these module imports available for downstream tests and integrations that
# historically monkeypatch meshtastic.util.<module> during port discovery.
_PORT_DISCOVERY_MONKEYPATCH_MODULES = (
    glob,
    platform,
    subprocess,
    serial.tools.list_ports,
)

"""Some devices such as a seger jlink or st-link we never want to accidentally open
     0483 STMicroelectronics ST-LINK/V2
     0136 SEGGER J-Link
     1915 NordicSemi (PPK2)
     0925 Lakeview Research Saleae Logic (logic analyzer)
04b4:602a Cypress Semiconductor Corp. Hantek DSO-6022BL (oscilloscope)
"""
BLACKLIST_VIDS: set[int] = {0x1366, 0x0483, 0x1915, 0x0925, 0x04B4}

"""Some devices are highly likely to be meshtastic.
0x239a RAK4631
0x303a Heltec tracker"""
WHITELIST_VIDS: set[int] = {0x239A, 0x303A}

# COMPAT_STABLE_SHIM: BLACKLIST_VIDS/WHITELIST_VIDS are canonical UPPER_SNAKE_CASE
# constants; blacklistVids/whitelistVids are legacy compatibility aliases.
blacklistVids: set[int] = BLACKLIST_VIDS
whitelistVids: set[int] = WHITELIST_VIDS

# Interval for polling the deferred execution queue in seconds
_DEFERRED_QUEUE_POLL_TIMEOUT_SECONDS = 0.1

logger = logging.getLogger(__name__)

DEFAULT_KEY = base64.b64decode("1PG7OiApB1nwvP+rz05pAQ==".encode("utf-8"))

# Timeout for HTTP requests (e.g., PyPI version checks)
HTTP_REQUEST_TIMEOUT_SECONDS = 5.0

# Ordered candidates for PyPI metadata checks. Fork builds can publish to an
# alternate package while preserving upstream fallback behavior.
DISTRIBUTION_NAME_CANDIDATES: tuple[str, ...] = ("mtjk", "meshtastic")


_PSK_SIMPLE_MSG = 'Invalid PSK format: expected "simpleN" with N in 0..254'
_ALLOWED_RAW_BASE64_PSK_BYTE_LENGTHS: tuple[int, ...] = (16, 24, 32)


def quoteBooleans(a_string: str) -> str:
    """Replace occurrences of the literal substrings ": true" and ": false" with ": 'true'" and ": 'false'".

    Only the exact, case-sensitive substrings are replaced; other variants (e.g.,
    "True", "true,", or without the leading colon and space) are not
    modified.

    Parameters
    ----------
    a_string : str
        Input string to process.

    Returns
    -------
    str
        The input string with matching boolean literals quoted.
    """
    tmp: str = a_string.replace(": true", ": 'true'")
    tmp = tmp.replace(": false", ": 'false'")
    return tmp


def genPSK256() -> bytes:
    """Generate a 32-byte preshared key for use as a PSK.

    Returns
    -------
    bytes
        32 bytes of cryptographically secure random data.
    """
    return os.urandom(32)


def fromPSK(valstr: str) -> bytes:
    """Parse a user-provided PSK specification into the internal PSK byte representation.

    Recognizes these special forms:
    - "random": generate and return a new 32-byte random PSK.
    - "none": return a single zero byte to indicate no encryption.
    - "default": return a single byte with value 1 to indicate the default channel PSK.
    - "simpleN": where N is an integer in 0..254; return a single byte with value (N + 1).

    For any other input, parse using strict byte-oriented rules (hex, base64:, or raw base64).
    Raw base64-encoded PSK values (without the ``base64:`` prefix) are accepted only
    when they decode to a standard AES key length (16, 24, or 32 bytes).

    Parameters
    ----------
    valstr : str
        PSK specification string provided by the user.

    Returns
    -------
    bytes
        The PSK as bytes.

    Raises
    ------
    ValueError
        If the input is not a recognized PSK format or cannot be decoded as bytes.
    """
    result: bytes | None = None

    if valstr == "random":
        result = genPSK256()
    elif valstr == "none":
        result = bytes([0])  # Use the 'no encryption' PSK
    elif valstr == "default":
        result = bytes([1])  # Use default channel psk
    elif valstr.startswith("simple"):
        digits = valstr[6:]
        if not digits or not digits.isdigit():
            raise ValueError(_PSK_SIMPLE_MSG)
        n = int(digits)
        if not 0 <= n <= 254:
            raise ValueError(f"{_PSK_SIMPLE_MSG}, got {n}")
        # Use one of the single byte encodings
        result = bytes([n + 1])
    elif len(valstr) == 0:
        result = bytes()
    elif valstr.startswith("0x"):
        # Parse hex and preserve compatibility with "0x"/single-nibble forms.
        hex_value = valstr[2:]
        if len(hex_value) == 0:
            hex_value = "00"
        elif len(hex_value) % 2 == 1:
            hex_value = "0" + hex_value
        try:
            result = bytes.fromhex(hex_value)
        except ValueError as e:
            raise ValueError(f"Invalid hex PSK: {valstr!r}") from e
    elif valstr.startswith("base64:"):
        try:
            result = base64.b64decode(valstr[7:], validate=True)
        except (binascii.Error, ValueError) as e:
            raise ValueError(f"Invalid base64 PSK: {valstr!r}") from e
    else:
        # Auto-detection of raw base64 (only for standard AES key lengths)
        try:
            decoded = base64.b64decode(valstr, validate=True)
            if len(decoded) in _ALLOWED_RAW_BASE64_PSK_BYTE_LENGTHS:
                result = decoded
        except (binascii.Error, ValueError):
            pass

    if result is None:
        raise ValueError(
            f"Invalid PSK format: {valstr!r}. Expected a keyword (none, default, random, simpleN), "
            "hex (0x...), explicit base64 (base64:...), or raw base64 (16, 24, or 32 bytes)."
        )
    return result


def fromStr(valstr: str) -> Any:
    r"""Parse a string into bytes, bool, int, float, or str according to common encodings and literal forms.

    Recognized forms:
    - empty string -> empty bytes.
    - "0x..." -> hex-decoded bytes (a single hex nibble is left-padded with a zero; "0x" alone yields b'\\x00').
    - "base64:<data>" -> base64-decoded bytes of <data>.
    - case-insensitive "t", "true", "yes" -> True; "f", "false", "no" -> False.
    - otherwise attempts int parsing, then float parsing, and falls back to the original string.

    Parameters
    ----------
    valstr : str
        Input string to interpret.

    Returns
    -------
    Any
        The value represented by the input string.
    """
    val: Any
    if len(valstr) == 0:  # Treat an emptystring as an empty bytes
        val = bytes()
    elif valstr.startswith("0x"):
        # Parse hex and preserve compatibility with "0x"/single-nibble forms.
        hex_value = valstr[2:]
        if len(hex_value) == 0:
            hex_value = "00"
        elif len(hex_value) % 2 == 1:
            hex_value = "0" + hex_value
        val = bytes.fromhex(hex_value)
    elif valstr.startswith("base64:"):
        val = base64.b64decode(valstr[7:])
    elif valstr.lower() in {"t", "true", "yes"}:
        val = True
    elif valstr.lower() in {"f", "false", "no"}:
        val = False
    else:
        try:
            val = int(valstr)
        except ValueError:
            try:
                val = float(valstr)
            except ValueError:
                val = valstr  # Not a float or an int, assume string
    return val


def toStr(raw_value: Any) -> str:
    """Convert a value into a string suitable for configuration storage.

    For `bytes`, returns "base64:<data>" where `<data>` is the base64 encoding of the bytes; otherwise returns `str(raw_value)`.

    Parameters
    ----------
    raw_value : Any
        Value to convert to string.

    Returns
    -------
    str
        A string suitable for storing in configuration — "base64:<data>" for bytes, otherwise the result of `str(raw_value)`.
    """
    if isinstance(raw_value, bytes):
        return "base64:" + base64.b64encode(raw_value).decode("utf-8")
    return str(raw_value)


def pskToString(psk: bytes) -> str:
    """Produce a privacy-preserving label for a preshared key (PSK).

    Parameters
    ----------
    psk : bytes
        PSK byte sequence to describe.

    Returns
    -------
    str
        One of:
        - "unencrypted" for an empty PSK or a single zero byte,
        - "default" for a single byte equal to 1,
        - "simpleN" for a single byte greater than 1 where N is the byte value minus one,
        - "secret" for any multi-byte PSK.
    """
    if len(psk) == 0:
        return "unencrypted"
    elif len(psk) == 1:
        b = psk[0]
        if b == 0:
            return "unencrypted"
        elif b == 1:
            return "default"
        else:
            return f"simple{b - 1}"
    else:
        return "secret"


def stripnl(s: Any) -> str:
    """Normalize input by replacing newlines with spaces and collapsing consecutive whitespace into single spaces.

    Parameters
    ----------
    s : Any
        Value to normalize; will be converted to a string.

    Returns
    -------
    str
        Single-line string with no newline characters and with consecutive whitespace collapsed to single spaces.
    """
    s = str(s).replace("\n", " ")
    return " ".join(s.split())


class FixmeError(Exception):
    """Exception for marking code that needs to be fixed."""


def fixme(message: str) -> NoReturn:
    """Raise a FixmeError with the given message prefixed by "FIXME: ".

    Parameters
    ----------
    message : str
        Message to include in the exception.

    Raises
    ------
    FixmeError
        Always raised with the message prefixed by "FIXME: ".
    """
    raise FixmeError("FIXME: " + message)


def ourExit(message: str, return_value: int = 1) -> NoReturn:
    """Compatibility helper that prints a message and exits the process.

    This function is retained for backward compatibility with existing external
    callers. Entrypoint modules should prefer local CLI helpers (for example
    ``_cli_exit()`` in ``meshtastic/__main__.py``) so CLI policy remains owned
    by the CLI layer.

    Library code should raise exceptions instead of calling this directly so
    callers can handle errors programmatically.

    Stream routing matches conventional CLI behavior:
    - `return_value == 0`: message is written to stdout
    - `return_value != 0`: message is written to stderr

    Parameters
    ----------
    message : str
        Message to print before exiting. An empty string is allowed and still
        emits a newline, preserving historical CLI behavior.
    return_value : int
        Exit code passed to `sys.exit()` (default 1).
    """
    output_stream = sys.stdout if return_value == 0 else sys.stderr
    print(message, file=output_stream)
    sys.exit(return_value)


# COMPAT_STABLE_SHIM: historical snake_case alias for ourExit().
def our_exit(message: str, return_value: int = 1) -> NoReturn:
    """Compatibility alias for ourExit()."""
    ourExit(message, return_value)


def catchAndIgnore(reason: str, closure: Callable[[], Any]) -> None:
    """Execute a callable and suppress any exception it raises, logging the failure.

    Parameters
    ----------
    reason : str
        Contextual message included in the log if the callable raises an exception.
    closure : Callable[[], Any]
        Zero-argument callable to execute; its exceptions are caught and logged.
    """
    try:
        closure()
    except Exception:
        logger.exception("Exception thrown in %s", reason)


def findPorts(eliminate_duplicates: bool = False) -> list[str]:
    """Return a sorted list of serial device paths likely to be Meshtastic radios.

    If any connected ports have vendor IDs in WHITELIST_VIDS, only those ports are returned;
    otherwise returns all ports whose vendor IDs are not in BLACKLIST_VIDS. If
    eliminate_duplicates is True, the list is reduced via eliminate_duplicate_port before returning.

    Parameters
    ----------
    eliminate_duplicates : bool
        If True, collapse likely-duplicate port entries before returning. (Default value = False)

    Returns
    -------
    list[str]
        Sorted list of serial device path strings.
    """
    return _port_discovery._find_ports(
        eliminate_duplicates=eliminate_duplicates,
        blacklist_vids=BLACKLIST_VIDS,
        whitelist_vids=WHITELIST_VIDS,
        eliminate_duplicate_port_fn=eliminate_duplicate_port,
    )


class DotDict(dict[str, Any]):
    """dot.notation access to dictionary attributes."""

    __getattr__ = dict.get
    __setattr__ = dict.__setitem__
    __delattr__ = dict.__delitem__


# Track warn-once state for dotdict deprecation (process-wide).
_warned_deprecations: set[str] = set()
_warned_deprecations_lock = threading.Lock()


# COMPAT_DEPRECATE: snake_case alias for DotDict (warns once)
class dotdict(DotDict):  # pylint: disable=invalid-name
    """Backward-compatible deprecated alias for DotDict."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the deprecated `dotdict` alias, forwarding arguments to dict
        initialization and emits a DeprecationWarning advising to use `DotDict`.

        Parameters
        ----------
        *args : Any
            Positional arguments passed to dict constructor.
        **kwargs : Any
            Keyword arguments passed to dict constructor.
        """
        should_warn = False
        with _warned_deprecations_lock:
            if "dotdict" not in _warned_deprecations:
                _warned_deprecations.add("dotdict")
                should_warn = True
        if should_warn:
            warnings.warn(
                "dotdict is deprecated; use DotDict instead",
                DeprecationWarning,
                stacklevel=2,
            )
        super().__init__(*args, **kwargs)


class Timeout:
    """Timeout class."""

    def __init__(self, maxSecs: float = 20.0) -> None:
        """Create a Timeout helper configured with a maximum wait duration.

        Parameters
        ----------
        maxSecs : float
            Maximum number of seconds to wait before the timeout expires (default 20.0).

        Attributes
        ----------
        expireTime : float
            Timestamp when the timeout will expire (initially 0.0).
        sleepInterval : float
            Polling sleep interval in seconds (default 0.1).
        expireTimeout : float
            Configured timeout duration in seconds (set from `maxSecs`).
        """
        self.expireTime: float = 0.0
        self.sleepInterval: float = 0.1
        self.expireTimeout: float = maxSecs

    def reset(self, expireTimeout: float | None = None) -> None:
        """Reset the expiration time used by wait loops.

        Parameters
        ----------
        expireTimeout : float | None
            Seconds from now until expiration. If ``None`` (the default), the
            instance's configured ``expireTimeout`` is used.
        """
        self.expireTime = time.time() + (
            self.expireTimeout if expireTimeout is None else expireTimeout
        )

    def waitForSet(self, target: Any, attrs: Iterable[str] = ()) -> bool:
        """Wait for one or more named attributes on `target` to exist and evaluate truthy before the timeout.

        Parameters
        ----------
        target : object
            Object whose attributes will be checked.
        attrs : Iterable[str]
            Names of attributes to wait for; if empty, the function returns immediately. (Default value = ())

        Returns
        -------
        bool
            `True` if all named attributes are present on `target` and evaluate to true before the timeout, `False` otherwise.
        """
        attr_names = tuple(attrs)
        self.reset()
        while time.time() < self.expireTime:
            if all(getattr(target, attr_name, None) for attr_name in attr_names):
                return True
            time.sleep(self.sleepInterval)
        return False

    def waitForAckNak(
        self,
        acknowledgment: Any,
        attrs: tuple[str, ...] = ("receivedAck", "receivedNak", "receivedImplAck"),
    ) -> bool:
        """Wait until any of the specified acknowledgment flags on an acknowledgment object becomes true or the timeout expires.

        Parameters
        ----------
        acknowledgment : object
            An object with boolean attributes named in `attrs` and a `reset()` method.
        attrs : tuple[str]
            Names of boolean attributes to check on `acknowledgment` (default: ("receivedAck", "receivedNak", "receivedImplAck")).

        Returns
        -------
        bool
            True if any specified acknowledgment flag was set before the timeout elapsed, False otherwise.
        """
        self.reset()
        while time.time() < self.expireTime:
            if any(map(lambda a: getattr(acknowledgment, a, None), attrs)):
                acknowledgment.reset()
                return True
            time.sleep(self.sleepInterval)
        return False

    def waitForTraceRoute(
        self,
        waitFactor: float,
        acknowledgment: "Acknowledgment",
        attr: str = "receivedTraceRoute",
    ) -> bool:
        """Wait up to an adjusted timeout for a traceroute acknowledgment flag on the given Acknowledgment to become set.

        Parameters
        ----------
        waitFactor : float
            Multiplier applied to this Timeout's configured expire timeout before waiting.
        acknowledgment : 'Acknowledgment'
            Object whose attribute named by `attr` will be polled.
        attr : str
            Attribute name on `acknowledgment` to check (default "receivedTraceRoute").

        Returns
        -------
        bool
            True if the specified acknowledgment attribute became set before the timeout expired, False otherwise.
        """
        self.reset(self.expireTimeout * waitFactor)
        while time.time() < self.expireTime:
            if getattr(acknowledgment, attr, None):
                acknowledgment.reset()
                return True
            time.sleep(self.sleepInterval)
        return False

    def _wait_for_ack_attribute(self, acknowledgment: Any, attr: str) -> bool:
        """Wait until one acknowledgment attribute is set, then reset the object."""
        self.reset()
        while time.time() < self.expireTime:
            if getattr(acknowledgment, attr, None):
                acknowledgment.reset()
                return True
            time.sleep(self.sleepInterval)
        return False

    def waitForTelemetry(self, acknowledgment: Any) -> bool:
        """Wait until a telemetry acknowledgement is observed or timeout expires.

        Parameters
        ----------
        acknowledgment : object
            Object that exposes a boolean `receivedTelemetry` attribute
            and a `reset()` method; the attribute is polled and reset on success.

        Returns
        -------
        bool
            True if telemetry acknowledgement was received before timeout,
            False otherwise.
        """
        return self._wait_for_ack_attribute(acknowledgment, "receivedTelemetry")

    def waitForPosition(self, acknowledgment: Any) -> bool:
        """Wait until a position acknowledgment is observed or the timeout expires.

        Parameters
        ----------
        acknowledgment : object
            Object with a boolean `receivedPosition` attribute and a
            `reset()` method; the attribute is polled and `reset()` is called on
            success.

        Returns
        -------
        bool
            True if the position acknowledgment was received before timeout, False otherwise.
        """
        return self._wait_for_ack_attribute(acknowledgment, "receivedPosition")

    def waitForWaypoint(self, acknowledgment: Any) -> bool:
        """Wait until a waypoint acknowledgement is observed or the timeout expires.

        Parameters
        ----------
        acknowledgment : object
            Object that exposes a boolean `receivedWaypoint` attribute and a `reset()`
            method; the attribute is polled and reset on success.

        Returns
        -------
        bool
            True if a waypoint acknowledgement was received before the timeout, False otherwise.
        """
        return self._wait_for_ack_attribute(acknowledgment, "receivedWaypoint")


class Acknowledgment:
    """A class that records which type of acknowledgment was just received, if any."""

    def __init__(self) -> None:
        """Create an Acknowledgment instance with all acknowledgment flags initialized to False.

        Tracks the following boolean flags: receivedAck, receivedNak, receivedImplAck,
        receivedTraceRoute, receivedTelemetry, receivedPosition, and receivedWaypoint.
        """
        self.receivedAck = False
        self.receivedNak = False
        self.receivedImplAck = False
        self.receivedTraceRoute = False
        self.receivedTelemetry = False
        self.receivedPosition = False
        self.receivedWaypoint = False

    def reset(self) -> None:
        """Clear all acknowledgment flags on this Acknowledgment instance.

        This method sets each tracked flag (receivedAck, receivedNak, receivedImplAck,
        receivedTraceRoute, receivedTelemetry, receivedPosition, receivedWaypoint) to False.
        """
        self.receivedAck = False
        self.receivedNak = False
        self.receivedImplAck = False
        self.receivedTraceRoute = False
        self.receivedTelemetry = False
        self.receivedPosition = False
        self.receivedWaypoint = False


class DeferredExecution:
    """A thread that accepts closures to run, and runs them as they are received."""

    # Sentinel object to signal shutdown of the worker thread
    _SHUTDOWN = object()
    _stop_lock: threading.Lock

    def __init__(self, name: str) -> None:
        """Create a DeferredExecution instance and start its daemon worker thread.

        Initializes an internal work queue and launches a daemon thread (named by
        the `name` parameter) that runs the instance's _run method to process queued work.

        Parameters
        ----------
        name : str
            Name assigned to the worker thread.
        """
        self.queue: Queue[Any] = Queue()
        self._shutdown: bool = False
        self._stop_lock = threading.Lock()
        # this thread must be marked as daemon, otherwise it will prevent clients from exiting
        self.thread = threading.Thread(
            target=self._run, args=(), name=name, daemon=True
        )
        self.thread.start()

    def queueWork(self, runnable: Callable[[], Any]) -> None:
        """Enqueue a callable to be executed by the background worker thread.

        Parameters
        ----------
        runnable : Callable[[], Any]
            A zero-argument callable to be executed later.
        """
        self.queue.put(runnable)

    def stop(self) -> None:
        """Signal the worker thread to shut down gracefully.

        Enqueues a sentinel value that causes the worker loop to exit. After calling
        stop(), the worker will finish processing pending items and exits. This method
        is safe to call multiple times and is a no-op if already stopped.
        """
        with self._stop_lock:
            if not self._shutdown:
                self._shutdown = True
                self.queue.put(self._SHUTDOWN)

    def join(self, timeout: float | None = None) -> bool:
        """Wait for the worker thread to finish.

        Note: Call `stop()` before `join()` to signal the worker to exit;
        otherwise this method may block indefinitely (or until timeout).

        Parameters
        ----------
        timeout : float | None, optional
            Maximum time to wait in seconds. If None, waits indefinitely.

        Returns
        -------
        bool
            True if the thread finished within the timeout, False otherwise.
        """
        if self.thread is not None:
            self.thread.join(timeout)
            return not self.thread.is_alive()
        return True

    def _run(self) -> None:
        """Continuously executes callables retrieved from the internal work queue.

        Runs an infinite loop that takes callables from self.queue and invokes them; any
        exception raised by a callable is logged and processing continues. The loop exits
        when the _SHUTDOWN sentinel is received or when stop() is called.
        """
        while not self._shutdown:
            try:
                o = self.queue.get(timeout=_DEFERRED_QUEUE_POLL_TIMEOUT_SECONDS)
                if o is self._SHUTDOWN:
                    break
                o()
            except Empty:
                continue
            except Exception:
                logger.exception("Unexpected error in deferred execution")


def removeKeysFromDict(
    keys: tuple[Any, ...] | list[Any] | set[Any], adict: dict[str, Any]
) -> dict[str, Any]:
    """Remove the given keys from a dictionary and all nested dictionaries.

    Parameters
    ----------
    keys : tuple[Any, ...] | list[Any] | set[Any]
        Iterable of keys to remove from the dictionary and any nested dict values.
    adict : dict[str, Any]
        Dictionary to process; this dictionary is modified in place.

    Returns
    -------
    dict[str, Any]
        The same `adict` after removal of matching keys.
    """
    for key in keys:
        try:
            del adict[key]
        except KeyError:
            pass
    for val in adict.values():
        if isinstance(val, dict):
            removeKeysFromDict(keys, val)
    return adict


# COMPAT_STABLE_SHIM: historical snake_case alias for removeKeysFromDict().
def remove_keys_from_dict(
    keys: tuple[Any, ...] | list[Any] | set[Any], adict: dict[str, Any]
) -> dict[str, Any]:
    """Compatibility alias for removeKeysFromDict()."""
    return removeKeysFromDict(keys, adict)


def channel_hash(data: bytes) -> int:
    """Compute an XOR-based hash of the given byte sequence.

    Parameters
    ----------
    data : bytes
        Byte sequence to hash.

    Returns
    -------
    int
        Integer hash equal to the bitwise XOR of all bytes in `data`.
    """
    result = 0
    for char in data:
        result ^= char
    return result


def generate_channel_hash(name: str | bytes, key: str | bytes) -> int:
    """Compute a channel number from a channel name and a preshared key.

    Parameters
    ----------
    name : str | bytes
        Channel name as a UTF-8 string or raw bytes; strings are UTF-8 encoded.
    key : str | bytes
        PSK as raw bytes or a base64 string (URL-safe '-'/'_'
        accepted). If the key is a single byte, it is combined with DEFAULT_KEY to
        derive a full-length key.

    Returns
    -------
    int
        Channel number computed by XOR-ing the hash of the name with the hash of the key.
    """
    # Handle key as str or bytes
    if isinstance(key, str):
        key = base64.b64decode(key.replace("-", "+").replace("_", "/").encode("utf-8"))

    if len(key) == 1:
        key = DEFAULT_KEY[:-1] + key

    # Handle name as str or bytes
    if isinstance(name, str):
        name = name.encode("utf-8")

    h_name = channel_hash(name)
    h_key = channel_hash(key)
    result: int = h_name ^ h_key
    return result


def hexstr(barray: bytes) -> str:
    """Convert a byte sequence to a colon-separated lowercase hex string.

    Parameters
    ----------
    barray : bytes
        Byte sequence to convert.

    Returns
    -------
    str
        Colon-separated two-digit lowercase hexadecimal pairs representing the input bytes (e.g., "01:ab:ff").
    """
    return ":".join(f"{x:02x}" for x in barray)


def ipstr(barray: bytes) -> str:
    r"""Convert a byte sequence to a dotted-decimal IPv4-style string.

    Parameters
    ----------
    barray : bytes
        Sequence of bytes to format; each byte becomes one decimal octet.

    Returns
    -------
    str
        Dotted-decimal string with one decimal octet per input byte (e.g., b'\xc0\xa8\x01\x01' -> "192.168.1.1").
    """
    return ".".join(f"{x}" for x in barray)


def readnet_u16(p: bytes | bytearray | memoryview, offset: int) -> int:
    """Read an unsigned 16-bit big-endian integer from a buffer at a byte offset.

    Parameters
    ----------
    p : bytes | bytearray | memoryview
        Buffer containing at least two bytes at the given offset.
    offset : int
        Byte index within `p` where the 2-byte big-endian value starts.

    Returns
    -------
    int
        The 16-bit unsigned integer read from `p[offset:offset+2]`.
    """
    return p[offset] * 256 + p[offset + 1]


def convert_mac_addr(val: str) -> str:
    """Convert a value into a colon-separated MAC address string.

    If `val` already matches a hexadecimal MAC address format (with optional separators),
    it is returned unchanged; otherwise `val` is interpreted as base64-encoded bytes and
    decoded to a colon-separated hexadecimal MAC string (e.g., 'fd:cd:20:17:28:5b').

    Parameters
    ----------
    val : str
        A hexadecimal MAC-like string or a base64-encoded byte string.

    Returns
    -------
    str
        A colon-separated hexadecimal MAC address.
    """
    if not re.match("[0-9a-f]{2}([-:]?)[0-9a-f]{2}(\\1[0-9a-f]{2}){4}$", val):
        val_as_bytes: bytes = base64.b64decode(val)
        return hexstr(val_as_bytes)
    return val


def snake_to_camel(a_string: str) -> str:
    """Convert a snake_case identifier to camelCase.

    Parameters
    ----------
    a_string : str
        Input string in snake_case.

    Returns
    -------
    str
        camelCase version of the input string.
    """
    # split underscore using split
    temp = a_string.split("_")
    # joining result
    result = temp[0] + "".join(ele.title() for ele in temp[1:])
    return result


def camel_to_snake(a_string: str) -> str:
    """Convert a camelCase or PascalCase identifier to snake_case.

    Parameters
    ----------
    a_string : str
        Input string in camelCase or PascalCase.

    Returns
    -------
    str
        The input converted to snake_case.
    """
    return "".join(["_" + i.lower() if i.isupper() else i for i in a_string]).lstrip(
        "_"
    )


def detect_supported_devices() -> set[SupportedDevice]:
    """Detect available supported USB devices on the host by matching discovered vendor IDs against known supported vendor IDs.

    Searches the host OS for attached USB devices (Linux: lsusb, Windows: Get-PnpDevice
    via PowerShell, macOS: system_profiler) and collects any devices whose vendor IDs
    appear in the module's known vendor list.

    Returns
    -------
    set[SupportedDevice]
        A set of supported device descriptors for matching devices; empty set if none are found.
    """
    return _port_discovery._detect_supported_devices()


def detect_windows_needs_driver(sd: Any, print_reason: bool = False) -> bool:
    """Determine whether Windows reports a failed driver installation for the given supported device.

    Parameters
    ----------
    sd : Any
        SupportedDevice object (or None). Its `usb_vendor_id_in_hex` attribute is used to query Windows PnP devices when present.
    print_reason : bool
        If True and a failed installation is detected, log the detailed PowerShell output explaining the failure. (Default value = False)

    Returns
    -------
    bool
        `True` if Windows indicates the device has a failed installation (a driver likely needs installation), `False` otherwise.
    """
    return _port_discovery._detect_windows_needs_driver(
        cast(SupportedDevice | None, sd),
        log_reason=print_reason,
    )


def eliminate_duplicate_port(ports: list[str]) -> list[str]:
    """Reduce paired serial port paths to a single representative when they likely refer to the same physical device.

    This function examines a list of serial port path strings and collapses duplicate pairs
    matching known naming patterns (e.g., usbserial vs wchusbserial, usbmodem vs wchusbserial,
    SLAB_USBtoUART vs usbserial) into a single preferred port. Duplicate pairs are collapsed
    even inside larger port lists, not only when exactly two ports are provided.

    Parameters
    ----------
    ports : list
        A list of serial port device path strings.

    Returns
    -------
    list
        The deduplicated port list with duplicate pairs collapsed to one representative each.
    """
    return _port_discovery._eliminate_duplicate_port(ports)


def is_windows11() -> bool:
    """Detect whether the host operating system is Windows 11.

    Returns
    -------
    bool
        `True` if the OS is Windows and the OS build (version patch) is 22000 or greater, `False` otherwise.
    """
    return _port_discovery._is_windows11()


def get_unique_vendor_ids() -> set[str]:
    """Collect unique USB vendor ID strings from the module's supported_devices.

    Returns
    -------
    set[str]
        A set of normalized lowercase USB vendor IDs from all known VID/PID
        tuples (primary IDs and aliases).
    """
    return _port_discovery._get_unique_vendor_ids()


def get_devices_with_vendor_id(vid: str) -> set[SupportedDevice]:
    """Get supported devices matching a USB vendor ID.

    Parameters
    ----------
    vid : str
        USB vendor ID as a hex string (e.g., "0x239A").

    Returns
    -------
    set[SupportedDevice]
        Set of SupportedDevice entries whose primary or aliased VID/PID tuples
        include the provided vendor id.
    """
    return _port_discovery._get_devices_with_vendor_id(vid)


def _discover_unix_ports(bp: str) -> set[str]:
    """Discover Unix serial-device paths matching the provided base-port prefix.

    Parameters
    ----------
    bp : str
        Base port prefix to glob under `/dev` (for example, `ttyUSB`).

    Returns
    -------
    set[str]
        Matching absolute device paths, or an empty set.
    """
    return _port_discovery._discover_unix_ports(bp)


def active_ports_on_supported_devices(
    sds: Iterable[SupportedDevice], eliminate_duplicates: bool = False
) -> set[str]:
    """Collect active serial port paths corresponding to the given supported devices for the current operating system.

    Parameters
    ----------
    sds : collections.abc.Iterable[meshtastic.supported_device.SupportedDevice]
        An iterable of SupportedDevice-like objects that expose
        platform-specific base port attributes (e.g., `baseport_on_linux`,
        `baseport_on_mac`, `baseport_on_windows`) used to discover matching ports.
    eliminate_duplicates : bool
        If True, collapse likely duplicate port entries using platform-dependent heuristics before returning. (Default value = False)

    Returns
    -------
    set[str]
        A set of active port path strings (for example, "/dev/ttyUSB0" on Unix or "COM3" on Windows).
    """
    return _port_discovery._active_ports_on_supported_devices(
        sds,
        eliminate_duplicates=eliminate_duplicates,
        detect_windows_port_fn=detectWindowsPort,
        eliminate_duplicate_port_fn=eliminate_duplicate_port,
        detect_windows_port_from_output_fn=(
            _port_discovery._detect_windows_port_from_output
            if detectWindowsPort is _DEFAULT_DETECT_WINDOWS_PORT
            else None
        ),
    )


def detectWindowsPort(sd: SupportedDevice | None) -> set[str]:
    """Detect Windows COM ports associated with a supported USB device.

    Searches present PnP devices on Windows for entries containing the device's
    usb_vendor_id_in_hex and returns any discovered COM port identifiers.

    Parameters
    ----------
    sd : SupportedDevice | None
        SupportedDevice whose `usb_vendor_id_in_hex`
        will be used to find matching PnP devices. If `None` or if the system
        is not Windows or the vendor id is missing, the function returns an
        empty set.

    Returns
    -------
    set[str]
        A set of COM port names (e.g., "COM3", "COM4") discovered for the
        device; empty if none found.
    """
    return _port_discovery._detect_windows_port(sd)


# COMPAT_STABLE_SHIM: historical snake_case alias for detectWindowsPort().
def detect_windows_port(sd: SupportedDevice | None) -> set[str]:
    """Compatibility alias for detectWindowsPort()."""
    return detectWindowsPort(sd)


_DEFAULT_DETECT_WINDOWS_PORT = detectWindowsPort


def check_if_newer_version() -> str | None:
    """Check PyPI for a newer Meshtastic release than the active installation.

    Attempts to fetch package metadata from PyPI for each distribution name in
    ``DISTRIBUTION_NAME_CANDIDATES`` and compares the newest discovered version
    to the currently active version. Returns the PyPI version string when it is
    newer; returns ``None`` if no newer version is available or if all checks fail.

    Returns
    -------
    pypi_version : str | None
        The newer PyPI version string if available, `None` otherwise.
    """
    pypi_version: str | None = None
    for distribution_name in DISTRIBUTION_NAME_CANDIDATES:
        try:
            url = f"https://pypi.org/pypi/{distribution_name}/json"
            data = requests.get(url, timeout=HTTP_REQUEST_TIMEOUT_SECONDS).json()
            pypi_version = data["info"]["version"]
            break
        except Exception:
            logger.debug(
                "PyPI version check failed for %s",
                distribution_name,
                exc_info=True,
            )
    act_version = get_active_version()

    if pypi_version is None:
        return None
    try:
        parsed_act_version = pkg_version.parse(act_version)
        parsed_pypi_version = pkg_version.parse(pypi_version)
    except pkg_version.InvalidVersion:
        return pypi_version

    if parsed_pypi_version <= parsed_act_version:
        return None

    return pypi_version


def _message_to_json(message: Message, multiline: bool = False) -> str:
    """Serialize a protobuf Message to JSON while including fields that have no presence.

    Parameters
    ----------
    message : Message
        The protobuf message to serialize.
    multiline : bool
        Preserve multi-line formatting when True; use compact
        single-line JSON when False. (Default value = False)

    Returns
    -------
    str
        JSON string representation of the message.
    """
    try:
        json_str = MessageToJson(
            message,
            always_print_fields_with_no_presence=True,
            indent=2 if multiline else None,
        )
    except TypeError:
        json_str = MessageToJson(  # type: ignore[call-arg]  # pylint: disable=E1123
            message,
            # pyright: ignore[reportCallIssue]  # Older protobuf uses including_default_value_fields
            including_default_value_fields=True,  # pyright: ignore[reportCallIssue]
            indent=2 if multiline else None,
        )
    return json_str


def messageToJson(message: Message, multiline: bool = False) -> str:
    """Serialize a protobuf message into JSON.

    Parameters
    ----------
    message : Message
        Protobuf message to serialize.
    multiline : bool
        If True, format with newlines and indentation; if False, produce a compact single-line JSON. (Default value = False)

    Returns
    -------
    json_str : str
        JSON representation of the provided message.
    """
    return _message_to_json(message, multiline=multiline)


# COMPAT_STABLE_SHIM: historical public snake_case alias.
def message_to_json(message: Message, multiline: bool = False) -> str:
    """Backward-compatible snake_case alias for messageToJson."""
    return messageToJson(message, multiline=multiline)


def _to_node_num(node_id: int | str) -> int:
    """Normalize a node identifier to its integer node number.

    Accepts an int or a string in these forms: decimal (e.g., "42"), hexadecimal with
    "0x" prefix (e.g., "0x2A"), hexadecimal without "0x" (e.g., "2A"), and
    any of the above prefixed with "!" (e.g., "!0x2A").

    Parameters
    ----------
    node_id : int | str
        Node identifier to normalize.

    Returns
    -------
    int
        The parsed integer node number.
    """
    if isinstance(node_id, int):
        return node_id
    s = str(node_id).strip()
    if s.startswith("!"):
        s = s[1:]
    if s.lower().startswith("0x"):
        return int(s, 16)
    try:
        return int(s, 10)
    except ValueError:
        return int(s, 16)


def toNodeNum(node_id: int | str) -> int:
    """Normalize a node identifier to its integer node number.

    Parameters
    ----------
    node_id : int | str
        Node identifier to normalize.

    Returns
    -------
    int
        The parsed integer node number.
    """
    return _to_node_num(node_id)


# COMPAT_STABLE_SHIM: historical public snake_case alias.
def to_node_num(node_id: int | str) -> int:
    """Backward-compatible wrapper for :func:`toNodeNum`."""
    return toNodeNum(node_id)


def _flags_to_list(flag_type: Any, flags: int) -> list[str]:
    """Convert a protobuf enum bitfield into a list of active flag names.

    Parameters
    ----------
    flag_type : Any
        Protobuf EnumTypeWrapper providing `.keys()` and `.Value(name)` for enum members.
    flags : int
        Integer bitfield containing combined enum flag values.

    Returns
    -------
    list[str]
        Ordered list of enum member names present in `flags`. If any bits remain that do not match known members,
        a single string of the form `UNKNOWN_ADDITIONAL_FLAGS(<remaining>)` is appended.
    """
    ret = []
    for key in flag_type.keys():
        if key == "EXCLUDED_NONE":
            continue
        if flags & flag_type.Value(key):
            ret.append(key)
            flags &= ~flag_type.Value(key)
    if flags > 0:
        ret.append(f"UNKNOWN_ADDITIONAL_FLAGS({flags})")
    return ret


def flagsToList(flag_type: Any, flags: int) -> list[str]:
    """Convert a protobuf enum bitfield into a list of active flag names.

    Parameters
    ----------
    flag_type : Any
        Protobuf EnumTypeWrapper providing `.keys()` and `.Value(name)` for enum members.
    flags : int
        Integer bitfield containing combined enum flag values.

    Returns
    -------
    list[str]
        Ordered list of enum member names present in `flags`. If any bits remain that do not match known members,
        a single string of the form `UNKNOWN_ADDITIONAL_FLAGS(<remaining>)` is appended.
    """
    return _flags_to_list(flag_type, flags)


# COMPAT_STABLE_SHIM: historical public snake_case alias.
def flags_to_list(flag_type: Any, flags: int) -> list[str]:
    """Backward-compatible wrapper for :func:`flagsToList`."""
    return flagsToList(flag_type, flags)
