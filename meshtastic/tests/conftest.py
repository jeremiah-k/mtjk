"""Common pytest code (place for fixtures)."""

# pylint: disable=redefined-outer-name
# Pytest fixtures use parameter injection which pylint sees as redefinition.

import argparse
import copy
import importlib
import itertools
import math
import os
import shutil
import sys
import threading
import time
from collections.abc import Callable, Generator
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, NoReturn, cast
from unittest.mock import MagicMock, create_autospec, patch

import pytest

import meshtastic.util as util_module
from meshtastic import mt_config, publishingThread
from meshtastic._branding import PRIMARY_CLI_NAME
from meshtastic.powermon import power_supply as power_supply_module

# Registers the autouse _no_real_self_signals fixture for this test root.
from meshtastic.tests._self_signal_fixture import _no_real_self_signals  # noqa: F401
from meshtastic.util import DeferredExecution

from ..mesh_interface import MeshInterface
from ..serial_interface import SerialInterface
from .simradio_harness import (
    CHAIN_TOPOLOGY,
    DEFAULT_BASE_PORT,
    SimMesh,
    SimNode,
    find_meshtasticd,
    is_compatible_host,
)
from .simradio_helpers import set_region

if TYPE_CHECKING:
    from meshtastic.powermon.power_supply import PowerSupply
    from meshtastic.powermon.ppk2 import PPK2PowerSupply
    from meshtastic.powermon.riden import RidenPowerSupply
    from meshtastic.powermon.stress import PowerStressClient


SINGLE_NODE_PORT_OFFSET = 100
_SINGLE_NODE_PORT_SEQUENCE = itertools.count()


@pytest.fixture(name="isolated_dotdict_deprecation_state")
def _isolated_dotdict_deprecation_state() -> Generator[None, None, None]:
    """Isolate the process-wide warn-once state used by the deprecated dotdict alias."""
    with util_module._warned_deprecations_lock:  # noqa: SLF001 - test isolation seam
        previous = set(util_module._warned_deprecations)  # noqa: SLF001
        util_module._warned_deprecations.discard("dotdict")  # noqa: SLF001
    try:
        yield
    finally:
        with util_module._warned_deprecations_lock:  # noqa: SLF001
            util_module._warned_deprecations.clear()  # noqa: SLF001
            util_module._warned_deprecations.update(previous)  # noqa: SLF001


def _skip_simradio_if_unavailable() -> None:
    """Skip native firmware tests when meshtasticd cannot run on this host."""
    if not is_compatible_host():
        pytest.skip("native meshtasticd simulator tests require Linux")
    if find_meshtasticd() is None:
        pytest.skip("meshtasticd not found; set MESHTASTICD_BIN or install it on PATH")


def _simradio_base_port() -> int:
    """Return the optionally configured native simulator base port."""
    raw_value = os.environ.get("MESHTASTICD_SIM_BASE_PORT")
    if raw_value is None:
        return DEFAULT_BASE_PORT
    try:
        return int(raw_value)
    except ValueError as exc:
        raise pytest.UsageError(
            f"MESHTASTICD_SIM_BASE_PORT must be an integer, got {raw_value!r}"
        ) from exc


def _simradio_log_root() -> Path | None:
    """Return the optional persistent simulator log artifact directory."""
    raw_value = os.environ.get("MESHTASTICD_SIM_LOG_DIR")
    return Path(raw_value).expanduser() if raw_value else None


def _next_simradio_single_node_port() -> int:
    """Return the next deterministic function-scoped simulator port."""
    return (
        _simradio_base_port()
        + SINGLE_NODE_PORT_OFFSET
        + next(_SINGLE_NODE_PORT_SEQUENCE)
    )


@pytest.fixture(scope="function")
def firmware_node() -> Generator[SimNode, None, None]:
    """Yield one freshly erased, US-region native meshtasticd simulator."""
    _skip_simradio_if_unavailable()
    # Function-scoped simulators use a fresh port instead of immediately
    # rebinding the same listener after every test.  Recent firmware opens
    # several short-lived TCP clients during reboot/config flows, and the
    # kernel can retain the previous port in a non-bindable state briefly
    # after teardown.  A deterministic sequence preserves debuggable ports
    # while eliminating that cross-test lifecycle race.
    single_node_port = _next_simradio_single_node_port()
    mesh = SimMesh(
        node_count=1,
        base_port=single_node_port,
        log_root=_simradio_log_root(),
    )
    try:
        mesh.start()
        node = mesh.get_node(0)
        node.disconnect()
        # LoRa changes apply live.  set_region() drains the CLI write and then
        # verifies the persisted value through an independent connection, which
        # it closes before returning.
        set_region(node.port, "US")
        yield node
    finally:
        mesh.stop()


@pytest.fixture(scope="module")
def firmware_mesh() -> Generator[SimMesh, None, None]:
    """Yield a converged three-node A-B-C native simradio chain."""
    _skip_simradio_if_unavailable()
    mesh = SimMesh(
        node_count=3,
        topology=CHAIN_TOPOLOGY,
        base_port=_simradio_base_port(),
        log_root=_simradio_log_root(),
    )
    try:
        mesh.start()
        for node in mesh.nodes:
            node.disconnect()
            set_region(node.port, "US")
        mesh.reconnect_all()
        if not mesh.wait_for_convergence(timeout=30.0):
            raise AssertionError(
                "simradio mesh failed to converge; "
                f"node DB counts={mesh.node_db_counts()}"
            )
        yield mesh
    finally:
        mesh.stop()


def pytest_addoption(  # pylint: disable=unused-argument
    parser: pytest.Parser, pluginmanager: pytest.PytestPluginManager
) -> None:
    """Add custom command line options for baseline tests.

    Parameters
    ----------
    parser : pytest.Parser
        The pytest argument parser to add options to.
    pluginmanager : pytest.PytestPluginManager
        The pytest plugin manager (unused but required by hook signature).
    """
    parser.addoption(
        "--update-baselines",
        action="store_true",
        default=False,
        help="Update stored API baselines to match current code",
    )


_MT_CONFIG_SENTINEL = object()
_OPTIONAL_ANALYSIS_DEPS = {
    "dash",
    "dash_bootstrap_components",
    "matplotlib",
    "numpy",
    "pandas",
    "plotly",
    "pyarrow",
}


def _create_context_manager_mock(spec_class: type[Any]) -> MagicMock:
    """Create a MagicMock that behaves as a context manager for the given spec class.

    Parameters
    ----------
    spec_class : type[Any]
        Class to use as the mock's specification (e.g., SerialInterface).

    Returns
    -------
    MagicMock
        A mock whose `__enter__` returns the mock itself and whose `__exit__` returns `None`.
    """
    mock = create_autospec(spec_class, instance=True)
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=None)
    return cast(MagicMock, mock)


class FakeTimer:
    """Simple timer stub for heartbeat timer tests."""

    created: ClassVar[list["FakeTimer"]] = []

    def __init__(
        self,
        interval: float,
        function: Callable[..., Any],
        args: tuple[Any, ...] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> None:
        """Initialize a FakeTimer and append it to the class-level `created` list for test inspection.

        Parameters
        ----------
        interval : float
            Time interval in seconds represented by the timer.
        function : Callable[..., Any]
            Callback that would be invoked when a real timer fires.
        args : tuple[Any, ...] | None
            Optional positional arguments for the callback; defaults to empty tuple.
        kwargs : dict[str, Any] | None
            Optional keyword arguments for the callback; defaults to empty dict.
        """
        self.interval = interval
        self.function = function
        self.args = args or ()
        self.kwargs = kwargs or {}
        self.daemon = False
        self.started = False
        self.cancelled = False
        type(self).created.append(self)

    def start(self) -> None:
        """Mark the FakeTimer instance as started.

        The timer's callback is not invoked automatically; to execute it in a test,
            call the stored function directly (for example:
            type(timer).created[i].function()).
        """
        self.started = True

    def cancel(self) -> None:
        """Mark the timer as cancelled by setting its `cancelled` attribute to True."""
        self.cancelled = True


@pytest.fixture(name="fake_timer_cls")
def _fake_timer_cls_fixture(monkeypatch: pytest.MonkeyPatch) -> type["FakeTimer"]:
    """Install a per-fixture FakeTimer subclass in place of meshtastic.mesh_interface.threading.Timer for use in tests.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Pytest fixture used to replace the real Timer with the fake subclass for the duration of the test.

    Returns
    -------
    type['FakeTimer']
        The FakeTimer subclass that was installed.
    """

    class FakeTimerForTest(FakeTimer):
        """Per-fixture timer class with isolated created-state."""

        created: ClassVar[list["FakeTimer"]] = []

    monkeypatch.setattr("meshtastic.mesh_interface.threading.Timer", FakeTimerForTest)
    return FakeTimerForTest


@pytest.fixture
def reset_mt_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the global mt_config state and install a fresh ArgumentParser for tests.

    Creates a new argparse.ArgumentParser with add_help=False, calls mt_config.reset(), and assigns
    the new parser to mt_config.parser so tests start with a clean configuration.
    """
    parser = argparse.ArgumentParser(add_help=False)
    # Register process-wide CLI state before resetting it. Tests in the legacy
    # CLI suite still assign these globals directly, so pytest must own their
    # restoration even when an assertion or SystemExit interrupts the test.
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    monkeypatch.setattr(mt_config, "args", mt_config.args)
    mt_config.reset()
    monkeypatch.setattr(mt_config, "parser", parser)


@pytest.fixture
def mt_config_state() -> Generator[None, None, None]:
    """Snapshot and restore mt_config module state mutated by tests."""
    state_keys = tuple(mt_config.MODULE_STATE_DEFAULTS.keys())
    snapshot: dict[str, Any] = {}
    for key in state_keys:
        value = getattr(mt_config, key, _MT_CONFIG_SENTINEL)
        if value is _MT_CONFIG_SENTINEL:
            snapshot[key] = value
            continue
        try:
            snapshot[key] = copy.deepcopy(value)
        except (TypeError, copy.Error):
            snapshot[key] = value
    # Also snapshot and restore warn-once tracking for deprecation warnings
    # Clear before yield so each test starts with fresh warn-once state
    warned_deprecations_lock = getattr(mt_config, "_warned_deprecations_lock", None)
    warned_deprecations: Any = getattr(mt_config, "_warned_deprecations", set())
    if warned_deprecations_lock is not None:
        with cast(Any, warned_deprecations_lock):
            if isinstance(warned_deprecations, set):
                try:
                    warned_snapshot: set[str] = copy.deepcopy(warned_deprecations)
                except (TypeError, copy.Error):
                    warned_snapshot = set(warned_deprecations)
                # Ensure each test starts from a clean warn-once registry
                warned_deprecations.clear()
            else:
                warned_snapshot = set()
                mt_config._warned_deprecations = set()
    elif isinstance(warned_deprecations, set):
        try:
            warned_snapshot = copy.deepcopy(warned_deprecations)
        except (TypeError, copy.Error):
            warned_snapshot = set(warned_deprecations)
        warned_deprecations.clear()
    else:
        warned_snapshot = set()
        mt_config._warned_deprecations = set()
    try:
        yield
    finally:
        for key, value in snapshot.items():
            if value is _MT_CONFIG_SENTINEL:
                if hasattr(mt_config, key):
                    delattr(mt_config, key)
            else:
                setattr(mt_config, key, value)
        warned_deprecations_restore = getattr(mt_config, "_warned_deprecations", None)
        warned_deprecations_lock_restore = getattr(
            mt_config, "_warned_deprecations_lock", None
        )
        if warned_deprecations_lock_restore is not None:
            with cast(Any, warned_deprecations_lock_restore):
                if isinstance(warned_deprecations_restore, set):
                    warned_deprecations_restore.clear()
                    warned_deprecations_restore.update(warned_snapshot)
                else:
                    mt_config._warned_deprecations = set(warned_snapshot)
        elif isinstance(warned_deprecations_restore, set):
            warned_deprecations_restore.clear()
            warned_deprecations_restore.update(warned_snapshot)
        else:
            mt_config._warned_deprecations = set(warned_snapshot)


@pytest.fixture
def cli_exit_capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch analysis_main._cli_exit and return a capture dict for message/code assertions."""
    try:
        analysis_main = importlib.import_module("meshtastic.analysis.__main__")
    except ModuleNotFoundError as exc:
        missing = (exc.name or "").split(".", maxsplit=1)[0]
        if missing in _OPTIONAL_ANALYSIS_DEPS:
            pytest.skip("Can't import meshtastic.analysis")
        raise

    captured: dict[str, Any] = {}

    def _fake_cli_exit(message: str, return_value: int = 1) -> NoReturn:
        captured["message"] = message
        captured["code"] = return_value
        raise SystemExit(return_value)

    monkeypatch.setattr(analysis_main, "_cli_exit", _fake_cli_exit)
    return captured


@pytest.fixture
def reset_power_supply_deprecations() -> Generator[None, None, None]:
    """Reset and restore powermon deprecation warn-once state for isolated tests."""
    warned_deprecations = power_supply_module._warned_deprecations
    warned_lock = getattr(power_supply_module, "_warned_deprecations_lock", None)
    if warned_lock is not None:
        with cast(Any, warned_lock):
            previous = set(warned_deprecations)
            warned_deprecations.clear()
    else:
        previous = set(warned_deprecations)
        warned_deprecations.clear()
    try:
        yield
    finally:
        if warned_lock is not None:
            with cast(Any, warned_lock):
                warned_deprecations.clear()
                warned_deprecations.update(previous)
        else:
            warned_deprecations.clear()
            warned_deprecations.update(previous)


@pytest.fixture
def iface_with_nodes() -> Generator[MeshInterface, None, None]:
    """Provide a MeshInterface populated with a sample node and a mocked myInfo for tests.

    Yields a MeshInterface whose `nodes` and `nodesByNum` each contain a single node (numeric id 2475227164)
    and whose `myInfo.my_node_num` is set to 2475227164. Ensures the interface is closed when the fixture
    teardown runs.

    Yields
    ------
    MeshInterface
        Interface instance populated with test node data and a mocked myInfo.
    """
    nodes_by_id = {
        "!9388f81c": {
            "num": 2475227164,
            "user": {
                "id": "!9388f81c",
                "longName": "Unknown f81c",
                "shortName": "?1C",
                "macaddr": "RBeTiPgc",
                "hwModel": "TBEAM",
            },
            "position": {"time": 1640206266},
            "lastHeard": 1640204888,
        }
    }

    nodes_by_num = {
        2475227164: {
            "num": 2475227164,
            "user": {
                "id": "!9388f81c",
                "longName": "Unknown f81c",
                "shortName": "?1C",
                "macaddr": "RBeTiPgc",
                "hwModel": "TBEAM",
            },
            "position": {"time": 1640206266},
            "lastHeard": 1640206266,
        }
    }
    iface = MeshInterface(noProto=True)
    try:
        iface.nodes = nodes_by_id
        iface.nodesByNum = nodes_by_num
        myInfo = MagicMock()
        iface.myInfo = myInfo
        iface.myInfo.my_node_num = 2475227164
        yield iface
    finally:
        iface.close()


@pytest.fixture
def mock_serial_interface() -> MagicMock:
    """Create a MagicMock that mimics a SerialInterface for node-related tests.

    The mock supports the context manager protocol, its localNode.getChannelByName returns None,
    and its myInfo.max_channels is set to 8.

    Returns
    -------
    MagicMock
        A mock configured to act like a SerialInterface with the above behavior.
    """
    mock_iface = _create_context_manager_mock(SerialInterface)
    mock_iface.localNode = MagicMock(spec=["getChannelByName", "getChannelCopyByName"])
    mock_iface.localNode.getChannelByName.return_value = None
    mock_iface.localNode.getChannelCopyByName.return_value = None
    mock_iface.myInfo = MagicMock(spec=["max_channels"])
    mock_iface.myInfo.max_channels = 8
    return mock_iface


@pytest.fixture
def autospec_local_node_iface() -> Callable[[type[Any]], MagicMock]:
    """Provide a factory for creating autospecced interface mocks with localNode.

    Returns
    -------
    Callable[[type[Any]], MagicMock]
        A factory function that takes a spec class (e.g., MeshInterface, SerialInterface)
        and returns an autospecced mock with a localNode attribute configured with
        _get_admin_channel_index returning 0, _get_named_admin_channel_index
        returning None, and getAdminChannelIndex returning 0.
    """

    def _factory(spec_class: type[Any]) -> MagicMock:
        iface = create_autospec(spec_class, instance=True)
        local_node = MagicMock(
            spec=[
                "_get_admin_channel_index",
                "_get_named_admin_channel_index",
                "getAdminChannelIndex",
            ]
        )
        local_node._get_admin_channel_index.return_value = 0
        local_node._get_named_admin_channel_index.return_value = None
        local_node.getAdminChannelIndex.return_value = 0
        iface.localNode = local_node
        return cast(MagicMock, iface)

    return _factory


@pytest.fixture
def power_supply() -> "PowerSupply":
    """Provide a fresh PowerSupply instance for voltage-validation tests."""
    from meshtastic.powermon.power_supply import PowerSupply  # pylint: disable=C0415

    return PowerSupply()


@pytest.fixture
def riden_stub() -> "RidenPowerSupply":
    """Create a minimally initialized RidenPowerSupply test instance."""
    from meshtastic.powermon.riden import RidenPowerSupply  # pylint: disable=C0415

    # Bypass __init__ to avoid opening a real serial port during unit tests.
    pps = object.__new__(RidenPowerSupply)
    pps.r = MagicMock()
    pps.prevPowerTime = time.monotonic() - 10.0
    pps.prevWattHour = 100.0
    pps.v = 3.3
    return pps


@pytest.fixture
def ppk2_stub() -> "PPK2PowerSupply":
    """Create a minimally initialized PPK2PowerSupply test instance."""
    from meshtastic.powermon.ppk2 import PPK2PowerSupply  # pylint: disable=C0415

    # Bypass __init__ to avoid starting the background measurement thread in tests.
    ppk = object.__new__(PPK2PowerSupply)
    ppk.r = MagicMock()
    ppk.measuring = False
    ppk.measurement_thread = threading.Thread(
        target=lambda: None, daemon=True, name="ppk2 test thread"
    )
    ppk._v = 3.3
    ppk_any = cast(Any, ppk)
    ppk_any._is_supply = False
    ppk_any._closed = False
    ppk_any._shutdown_event = threading.Event()
    # Keep both names initialized: legacy code paths may still touch
    # measurement_thread while current code uses _measurement_thread.
    ppk_any._measurement_thread = None
    ppk._result_lock = threading.Condition()
    ppk._want_measurement = threading.Condition()
    ppk._measurement_state_lock = threading.Lock()
    ppk.current_sum = 0
    ppk.current_num_samples = 0
    ppk.current_min = 0
    ppk.current_max = 0
    ppk.current_average = math.nan
    ppk.last_reported_min = math.nan
    ppk.last_reported_max = math.nan
    ppk.num_data_reads = 0
    ppk.total_data_len = 0
    ppk.max_data_len = 0
    return ppk


@pytest.fixture
def power_stress_client() -> tuple[MagicMock, "PowerStressClient"]:
    """Create a PowerStressClient with a default mocked interface."""
    from meshtastic.powermon.stress import PowerStressClient  # pylint: disable=C0415

    iface = MagicMock()
    iface.myInfo = MagicMock()
    iface.myInfo.my_node_num = 1
    client = PowerStressClient(iface)
    return iface, client


def _mock_iface_with_gpio_channel(channel_index: int = 0) -> MagicMock:
    """Create a SerialInterface mock that provides a stubbed GPIO channel."""
    iface = create_autospec(SerialInterface, instance=True)
    iface.localNode = MagicMock()
    channel = MagicMock()
    channel.index = channel_index
    iface.localNode.getChannelByName.return_value = channel
    iface.localNode.getChannelCopyByName.return_value = channel
    return cast(MagicMock, iface)


def _resolve_cli_binary_or_skip(binary_name: str) -> str:
    """Resolve a CLI binary path or skip tests when it is unavailable."""
    exe = shutil.which(binary_name)
    if exe is None:
        pytest.skip(f"{binary_name} CLI not found in PATH")
    return exe


@pytest.fixture(name="mock_gpio_iface")
def _mock_gpio_iface_fixture() -> Generator[MagicMock, None, None]:
    """Provide a GPIO-capable mocked interface for GPIO tests."""
    yield _mock_iface_with_gpio_channel()


@pytest.fixture(scope="session")
def primary_cli_bin() -> str:
    """Resolve the configured preferred CLI binary or skip when unavailable."""
    return _resolve_cli_binary_or_skip(PRIMARY_CLI_NAME)


@pytest.fixture(scope="session")
def meshtastic_bin() -> str:
    """Resolve the meshtastic CLI binary path or skip tests if not found.

    Returns
    -------
    str
        The absolute path to the meshtastic CLI binary.

    Raises
    ------
    pytest.skip
        If the meshtastic CLI is not found in PATH.
    """
    return _resolve_cli_binary_or_skip("meshtastic")


@pytest.fixture(scope="session")
def mesh_tunnel_bin() -> str:
    """Resolve the mesh-tunnel CLI binary path or skip tests if not found.

    Returns
    -------
    str
        The absolute path to the mesh-tunnel CLI binary.

    Raises
    ------
    pytest.skip
        If the mesh-tunnel CLI is not found in PATH.
    """
    return _resolve_cli_binary_or_skip("mesh-tunnel")


@pytest.fixture(name="platform_socket_mocks")
def _platform_socket_mocks() -> Generator[tuple[MagicMock, MagicMock], None, None]:
    """Patch platform.system and socket.socket for tunnel tests."""
    with (
        patch("platform.system", return_value="Linux") as platform_mock,
        patch("socket.socket") as socket_mock,
    ):
        yield platform_mock, socket_mock


@pytest.fixture(scope="session", autouse=True)
def _shutdown_publishing_thread() -> Generator[None, None, None]:
    """Ensure the global publishing thread is shut down after all tests complete.

    The publishingThread is a global DeferredExecution instance whose daemon
    worker starts lazily on first use. If tests start it, this fixture guarantees
    clean shutdown at the end of the session.
    """
    yield
    # After all tests complete, shut down the publishing thread

    if publishingThread is not None:
        if isinstance(publishingThread, DeferredExecution):
            publishingThread.stop()
            publishingThread.join(timeout=5.0)


@pytest.fixture
def mock_interface() -> Generator[MeshInterface, None, None]:
    """Provide a MeshInterface with mocked internals for behavioral testing.

    Yields
    ------
    MeshInterface
        Interface instance with mocked `_send_to_radio_impl`, populated nodes,
        and a mocked `myInfo`.
    """
    iface = MeshInterface(noProto=True)
    try:
        # Mock critical methods to avoid hardware dependency
        iface._send_to_radio_impl = MagicMock()  # type: ignore[method-assign]
        iface.myInfo = MagicMock()
        iface.myInfo.my_node_num = 2475227164

        # Set up sample nodes - use non-hex IDs to ensure DB lookup path
        iface.nodes = {
            "!9388f81c": {
                "num": 2475227164,
                "user": {
                    "id": "!9388f81c",
                    "longName": "Test Node",
                    "shortName": "TN",
                    "macaddr": "RBeTiPgc",
                    "hwModel": "TBEAM",
                },
                "position": {"time": 1640206266},
                "lastHeard": 1640204888,
            },
            "!testnode1": {
                "num": 11259375,
                "user": {
                    "id": "!testnode1",
                    "longName": "Remote Node",
                    "shortName": "RN",
                    "macaddr": "Test1234",
                    "hwModel": "RAK4631",
                },
                "position": {},
                "lastHeard": 1640205000,
            },
        }
        iface.nodesByNum = {
            2475227164: iface.nodes["!9388f81c"],
            11259375: iface.nodes["!testnode1"],
        }

        yield iface
    finally:
        iface.close()


@pytest.fixture
def mock_interface_with_nodes(mock_interface: MeshInterface) -> MeshInterface:
    """Provide a mock interface pre-configured with nodes.

    This is a semantic alias for ``mock_interface`` for tests that explicitly
    require a node database to be populated.

    Returns
    -------
    MeshInterface
        The same instance as ``mock_interface``, already configured with sample nodes.
    """
    # Already configured in mock_interface
    return mock_interface
