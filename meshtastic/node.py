# pylint: disable=too-many-lines
"""Node class for representing and managing mesh nodes.

This module provides the Node class which represents a (local or remote) node
in the mesh, including methods for localConfig, moduleConfig, and channels management.
"""

import logging
import sys
import threading
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    ClassVar,
    NoReturn,
    Sequence,
    TypeVar,
    cast,
)

from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.message import Message

from meshtastic._interface_errors import MeshInterfaceError as _MeshInterfaceError
from meshtastic.node_runtime import contact_runtime
from meshtastic.node_runtime.admin_wait import (
    _send_admin_with_ack_scope,
    _wait_for_admin_ack,
)
from meshtastic.node_runtime.channel_export_runtime import _NodeChannelExportRuntime
from meshtastic.node_runtime.channel_lookup_runtime import _NodeChannelLookupRuntime
from meshtastic.node_runtime.channel_normalization_runtime import (
    _NodeChannelNormalizationRuntime,
)
from meshtastic.node_runtime.channel_presentation_runtime import (
    _NodeChannelPresentationRuntime,
)
from meshtastic.node_runtime.channel_request_runtime import _NodeChannelRequestRuntime
from meshtastic.node_runtime.channel_state import _ChannelLock, _NodeChannelState
from meshtastic.node_runtime.settings_runtime import (
    _NodeAdminCommandRuntime,
    _NodeOwnerProfileRuntime,
    _NodeSettingsMessageBuilder,
    _NodeSettingsResponseRuntime,
    _NodeSettingsRuntime,
)
from meshtastic.node_runtime.seturl_runtime import (
    _SetUrlParser,
    _SetUrlTransactionCoordinator,
)
from meshtastic.node_runtime.shared import EMPTY_LONG_NAME_MSG as _EMPTY_LONG_NAME_MSG
from meshtastic.node_runtime.shared import EMPTY_SHORT_NAME_MSG as _EMPTY_SHORT_NAME_MSG
from meshtastic.node_runtime.shared import (
    FACTORY_RESET_REQUEST_VALUE as _FACTORY_RESET_REQUEST_VALUE,
)
from meshtastic.node_runtime.shared import (
    MAX_CANNED_MESSAGE_LENGTH as _MAX_CANNED_MESSAGE_LENGTH,
)
from meshtastic.node_runtime.shared import MAX_CHANNELS as _MAX_CHANNELS
from meshtastic.node_runtime.shared import (
    MAX_DELETE_FILE_PATH_BYTES as _MAX_DELETE_FILE_PATH_BYTES,
)
from meshtastic.node_runtime.shared import MAX_INPUT_EVENT_CODE as _MAX_INPUT_EVENT_CODE
from meshtastic.node_runtime.shared import MAX_INPUT_KB_CHAR as _MAX_INPUT_KB_CHAR
from meshtastic.node_runtime.shared import MAX_INPUT_TOUCH_X as _MAX_INPUT_TOUCH_X
from meshtastic.node_runtime.shared import MAX_INPUT_TOUCH_Y as _MAX_INPUT_TOUCH_Y
from meshtastic.node_runtime.shared import MAX_LONG_NAME_LEN as _MAX_LONG_NAME_LEN
from meshtastic.node_runtime.shared import MAX_RINGTONE_LENGTH as _MAX_RINGTONE_LENGTH
from meshtastic.node_runtime.shared import MAX_SHORT_NAME_LEN as _MAX_SHORT_NAME_LEN
from meshtastic.node_runtime.shared import (
    METADATA_STDOUT_COMPAT_WAIT_SECONDS,
    _delete_file_path_error,
)
from meshtastic.node_runtime.transport_runtime import (
    _NodeAckNakRuntime,
    _NodeAdminSessionRuntime,
    _NodeAdminTransportRuntime,
    _NodeChannelWriteRuntime,
    _NodeDeleteChannelRuntime,
)
from meshtastic.protobuf import (
    admin_pb2,
    channel_pb2,
    config_pb2,
    connection_status_pb2,
    device_ui_pb2,
    localonly_pb2,
    mesh_pb2,
)
from meshtastic.util import (
    Timeout,
    flagsToList,
    toNodeNum,
)

if TYPE_CHECKING:
    from meshtastic.mesh_interface import MeshInterface
    from meshtastic.node_runtime.content_runtime import (
        _NodeAdminContentRuntime,
        _NodeContentCacheStore,
        _NodeContentResponseRuntime,
    )
    from meshtastic.node_runtime.response_runtime import (
        _NodeChannelResponseRuntime,
        _NodeMetadataResponseRuntime,
    )
    from meshtastic.node_runtime.transport_runtime import (
        _NodePositionTimeCommandRuntime,
    )

logger = logging.getLogger(__name__)
_ResultT = TypeVar("_ResultT")
_AdminResponseT = TypeVar("_AdminResponseT", bound=Message)
ADMIN_RESPONSE_WAIT_SECONDS = 12.0
# COMPAT_STABLE_SHIM: Compatibility re-exports preserved for callers/tests importing constants from meshtastic.node.
EMPTY_LONG_NAME_MSG = _EMPTY_LONG_NAME_MSG
EMPTY_SHORT_NAME_MSG = _EMPTY_SHORT_NAME_MSG
MAX_CANNED_MESSAGE_LENGTH = _MAX_CANNED_MESSAGE_LENGTH
FACTORY_RESET_REQUEST_VALUE = _FACTORY_RESET_REQUEST_VALUE
MAX_CHANNELS = _MAX_CHANNELS
MAX_DELETE_FILE_PATH_BYTES = _MAX_DELETE_FILE_PATH_BYTES
MAX_LONG_NAME_LEN = _MAX_LONG_NAME_LEN
MAX_RINGTONE_LENGTH = _MAX_RINGTONE_LENGTH
MAX_SHORT_NAME_LEN = _MAX_SHORT_NAME_LEN
MAX_INPUT_EVENT_CODE = _MAX_INPUT_EVENT_CODE
MAX_INPUT_KB_CHAR = _MAX_INPUT_KB_CHAR
MAX_INPUT_TOUCH_X = _MAX_INPUT_TOUCH_X
MAX_INPUT_TOUCH_Y = _MAX_INPUT_TOUCH_Y


def _backup_location_value(
    location: str,
) -> admin_pb2.AdminMessage.BackupLocation.ValueType:
    """Convert a backup location name to its ``BackupLocation`` enum value.

    Parameters
    ----------
    location : str
        User-supplied location name such as ``flash`` or ``sd``.

    Returns
    -------
    admin_pb2.AdminMessage.BackupLocation.ValueType
        The matching ``AdminMessage.BackupLocation`` enum value.

    Raises
    ------
    MeshInterface.MeshInterfaceError
        If the location name is not a valid backup location.
    """
    try:
        return admin_pb2.AdminMessage.BackupLocation.Value(location.strip().upper())
    except ValueError:
        valid = ", ".join(admin_pb2.AdminMessage.BackupLocation.keys())
        raise _MeshInterfaceError(
            f"Unknown backup location {location!r}; expected one of: {valid}"
        ) from None


_MAX_CONTACT_URL_PAYLOAD = contact_runtime.MAX_CONTACT_URL_PAYLOAD
_decode_node_bytes_field = contact_runtime._decode_node_bytes_field


class Node:  # pylint: disable=too-many-instance-attributes
    """A model of a (local or remote) node in the mesh.

    Includes methods for localConfig, moduleConfig and channels
    """

    _channel_state_bootstrap_lock: ClassVar[_ChannelLock] = threading.Lock()

    def __init__(
        self,
        iface: "MeshInterface",
        nodeNum: int | str,
        noProto: bool = False,
        timeout: float = 300.0,
    ) -> None:
        """Create and initialize a Node instance that holds configuration, channel state, and runtime flags for a mesh node.

        Parameters
        ----------
        iface : 'MeshInterface'
            Interface used for network I/O and device interactions.
        nodeNum : int | str
            Node identifier (numeric or string convertible to a node number).
        noProto : bool
            If True, protocol-based operations are disabled for this node. (Default value = False)
        timeout : float
            Maximum seconds used for operations that wait for responses. (Default value = 300.0)
        """
        self.iface = iface
        self.nodeNum = toNodeNum(nodeNum) if isinstance(nodeNum, str) else nodeNum
        self.localConfig = localonly_pb2.LocalConfig()
        self.moduleConfig = localonly_pb2.LocalModuleConfig()
        self._channel_state = _NodeChannelState()
        self._timeout = Timeout(maxSecs=timeout)
        self.noProto = noProto
        self.cannedPluginMessage: str | None = None
        self.cannedPluginMessageMessages: str | None = None
        self.ringtone: str | None = None
        self.ringtonePart: str | None = None
        self._ringtone_lock = threading.Lock()
        self._canned_message_lock = threading.Lock()
        self._metadata_stdout_event_lock = threading.Lock()
        self._metadata_stdout_event: threading.Event | None = None
        self._admin_session_runtime = _NodeAdminSessionRuntime(self)
        self._admin_transport_runtime = _NodeAdminTransportRuntime(self)
        self._channel_lookup_runtime = _NodeChannelLookupRuntime(self._channel_state)
        self._channel_normalization_runtime = _NodeChannelNormalizationRuntime(
            self._channel_state
        )
        self._channel_export_runtime = _NodeChannelExportRuntime(
            self, channel_state=self._channel_state
        )
        self._channel_request_runtime = _NodeChannelRequestRuntime(
            self, channel_state=self._channel_state
        )
        self._channel_presentation_runtime = _NodeChannelPresentationRuntime(
            self,
            channel_state=self._channel_state,
            export_runtime=self._channel_export_runtime,
        )
        self._contact_runtime = contact_runtime._NodeContactRuntime(self)
        self._channel_write_runtime = _NodeChannelWriteRuntime(
            self, channel_state=self._channel_state
        )
        self._delete_channel_runtime = _NodeDeleteChannelRuntime(
            self,
            channel_state=self._channel_state,
            channel_write_runtime=self._channel_write_runtime,
        )
        self._ack_nak_runtime = _NodeAckNakRuntime(self)
        self._settings_message_builder = _NodeSettingsMessageBuilder(self)
        self._settings_runtime = _NodeSettingsRuntime(
            self,
            message_builder=self._settings_message_builder,
        )
        self._settings_response_runtime = _NodeSettingsResponseRuntime(self)
        self._admin_command_runtime = _NodeAdminCommandRuntime(self)
        self._owner_profile_runtime = _NodeOwnerProfileRuntime(
            self,
            admin_command_runtime=self._admin_command_runtime,
        )
        self._content_cache_store_cache: "_NodeContentCacheStore | None" = None
        self._content_response_runtime_cache: "_NodeContentResponseRuntime | None" = (
            None
        )
        self._content_request_runtime_cache: "_NodeAdminContentRuntime | None" = None
        self._metadata_response_runtime_cache: "_NodeMetadataResponseRuntime | None" = (
            None
        )
        self._channel_response_runtime_cache: "_NodeChannelResponseRuntime | None" = (
            None
        )
        self._position_time_runtime_cache: "_NodePositionTimeCommandRuntime | None" = (
            None
        )
        self._lazy_init_lock = threading.RLock()

    def _get_channel_state(self) -> _NodeChannelState:
        """Return the channel-state owner, migrating legacy raw instance fields."""
        channel_state = cast(
            _NodeChannelState | None, self.__dict__.get("_channel_state")
        )
        if channel_state is not None:
            return channel_state

        with self._channel_state_bootstrap_lock:
            channel_state = cast(
                _NodeChannelState | None, self.__dict__.get("_channel_state")
            )
            if channel_state is not None:
                return channel_state

            missing = object()
            legacy_channels = self.__dict__.pop("channels", missing)
            legacy_partial_channels = self.__dict__.pop("partialChannels", missing)
            legacy_lock = self.__dict__.pop("_channels_lock", missing)
            channel_state = _NodeChannelState()
            if legacy_lock is not missing:
                channel_state._replace_lock(legacy_lock)  # noqa: SLF001
            if legacy_channels is not missing:
                channel_state.channels = legacy_channels
            if legacy_partial_channels is not missing:
                channel_state.partial_channels = legacy_partial_channels
            object.__setattr__(self, "_channel_state", channel_state)
        return channel_state

    channels: list[channel_pb2.Channel] | None
    partialChannels: list[channel_pb2.Channel]  # noqa: N815 - public compatibility

    def __getattr__(self, name: str) -> Any:
        """Resolve historical channel-cache instance attributes through the owner."""
        if name == "_channel_state":
            return self._get_channel_state()
        if name == "channels":
            return self._get_channel_state().channels
        if name == "partialChannels":
            return self._get_channel_state().partial_channels
        raise AttributeError(f"{type(self).__name__!s} has no attribute {name!r}")

    def __setattr__(self, name: str, value: Any) -> None:
        """Route historical channel-cache assignments through the state owner."""
        if name == "channels":
            self._get_channel_state().channels = value
            return
        if name == "partialChannels":
            self._get_channel_state().partial_channels = value
            return
        object.__setattr__(self, name, value)

    @property
    def _channels_lock(self) -> _ChannelLock:
        """Return the compatibility lock backed by the channel-state owner."""
        return self._get_channel_state().lock

    @_channels_lock.setter
    def _channels_lock(self, value: _ChannelLock) -> None:
        """Replace the compatibility lock for legacy instrumentation tests."""
        self._get_channel_state()._replace_lock(value)  # noqa: SLF001

    @property
    def _content_cache_store(self) -> "_NodeContentCacheStore":
        """Lazy-init for content cache store (thread-safe)."""
        if self._content_cache_store_cache is None:
            with self._lazy_init_lock:
                if self._content_cache_store_cache is None:
                    from meshtastic.node_runtime.content_runtime import (  # pylint: disable=import-outside-toplevel
                        _NodeContentCacheStore,
                    )

                    self._content_cache_store_cache = _NodeContentCacheStore(self)
        return self._content_cache_store_cache

    @property
    def _content_response_runtime(self) -> "_NodeContentResponseRuntime":
        """Lazy-init for content response runtime (thread-safe)."""
        if self._content_response_runtime_cache is None:
            with self._lazy_init_lock:
                if self._content_response_runtime_cache is None:
                    from meshtastic.node_runtime.content_runtime import (  # pylint: disable=import-outside-toplevel
                        _NodeContentResponseRuntime,
                    )

                    self._content_response_runtime_cache = _NodeContentResponseRuntime(
                        self,
                        cache_store=self._content_cache_store,
                    )
        return self._content_response_runtime_cache

    @property
    def _content_request_runtime(self) -> "_NodeAdminContentRuntime":
        """Lazy-init for content request runtime (thread-safe)."""
        if self._content_request_runtime_cache is None:
            with self._lazy_init_lock:
                if self._content_request_runtime_cache is None:
                    from meshtastic.node_runtime.content_runtime import (  # pylint: disable=import-outside-toplevel
                        _NodeAdminContentRuntime,
                    )

                    self._content_request_runtime_cache = _NodeAdminContentRuntime(
                        self,
                        cache_store=self._content_cache_store,
                        response_runtime=self._content_response_runtime,
                    )
        return self._content_request_runtime_cache

    @property
    def _metadata_response_runtime(self) -> "_NodeMetadataResponseRuntime":
        """Lazy-init for metadata response runtime (thread-safe)."""
        if self._metadata_response_runtime_cache is None:
            with self._lazy_init_lock:
                if self._metadata_response_runtime_cache is None:
                    from meshtastic.node_runtime.response_runtime import (  # pylint: disable=import-outside-toplevel
                        _NodeMetadataResponseRuntime,
                    )

                    self._metadata_response_runtime_cache = (
                        _NodeMetadataResponseRuntime(self)
                    )
        return self._metadata_response_runtime_cache

    @property
    def _channel_response_runtime(self) -> "_NodeChannelResponseRuntime":
        """Lazy-init for channel response runtime (thread-safe)."""
        if self._channel_response_runtime_cache is None:
            with self._lazy_init_lock:
                if self._channel_response_runtime_cache is None:
                    from meshtastic.node_runtime.response_runtime import (  # pylint: disable=import-outside-toplevel
                        _NodeChannelResponseRuntime,
                    )

                    self._channel_response_runtime_cache = _NodeChannelResponseRuntime(
                        self, channel_state=self._get_channel_state()
                    )
        return self._channel_response_runtime_cache

    @property
    def _position_time_runtime(self) -> "_NodePositionTimeCommandRuntime":
        """Lazy-init for position/time command runtime (thread-safe)."""
        if self._position_time_runtime_cache is None:
            with self._lazy_init_lock:
                if self._position_time_runtime_cache is None:
                    from meshtastic.node_runtime.transport_runtime import (  # pylint: disable=import-outside-toplevel
                        _NodePositionTimeCommandRuntime,
                    )

                    self._position_time_runtime_cache = _NodePositionTimeCommandRuntime(
                        self
                    )
        return self._position_time_runtime_cache

    def __repr__(self) -> str:
        """Return a developer-oriented string identifying the Node.

        Returns
        -------
        str
            A debug-friendly representation containing the interface repr, the node number
            formatted as eight-hex digits (prefixed with '0x'), and any active
            non-default flags such as `noProto` or a non-default `timeout`.
        """
        r = f"Node({self.iface!r}, 0x{self.nodeNum:08x}"
        if self.noProto:
            r += ", noProto=True"
        if self._timeout.expireTimeout != 300.0:
            r += f", timeout={self._timeout.expireTimeout!r}"
        r += ")"
        return r

    @staticmethod
    def positionFlagsList(position_flags: int) -> list[str]:
        """Convert a PositionConfig position flags bitfield into a list of flag names.

        Parameters
        ----------
        position_flags : int
            Bitfield of flags from Config.PositionConfig.PositionFlags.

        Returns
        -------
        list[str]
            Names of the flags set in `position_flags`.
        """
        return flagsToList(
            config_pb2.Config.PositionConfig.PositionFlags, position_flags
        )

    # COMPAT_STABLE_SHIM: alias for positionFlagsList
    @staticmethod
    def position_flags_list(position_flags: int) -> list[str]:
        """Backward-compatible alias for positionFlagsList."""
        return Node.positionFlagsList(position_flags)

    @staticmethod
    def excludedModulesList(excluded_modules: int) -> list[str]:
        """Convert an ExcludedModules bitfield to a list of excluded module names.

        Parameters
        ----------
        excluded_modules : int
            Bitfield using values from mesh_pb2.ExcludedModules.

        Returns
        -------
        list[str]
            Names of modules whose bits are set in the bitfield.
        """
        return flagsToList(mesh_pb2.ExcludedModules, excluded_modules)

    # COMPAT_STABLE_SHIM: alias for excludedModulesList
    @staticmethod
    def excluded_modules_list(excluded_modules: int) -> list[str]:
        """Backward-compatible alias for excludedModulesList."""
        return Node.excludedModulesList(excluded_modules)

    @staticmethod
    def _emit_metadata_line(line: str) -> None:
        """Log a metadata line and mirror it to redirected stdout for compatibility."""
        logger.info("%s", line)
        # Historical callers parse getMetadata() output from redirected stdout.
        if sys.stdout is not sys.__stdout__:
            print(line)

    def _signal_metadata_stdout_event(self) -> None:
        """Signal redirected-stdout metadata waiters that a terminal response arrived."""
        with self._metadata_stdout_event_lock:
            metadata_stdout_event = self._metadata_stdout_event
        if metadata_stdout_event is not None:
            metadata_stdout_event.set()

    def _execute_with_node_db_lock(self, func: Callable[[], _ResultT]) -> _ResultT:
        """Execute ``func`` while holding ``iface._node_db_lock`` when available."""
        node_db_lock = getattr(self.iface, "_node_db_lock", None)
        if (
            node_db_lock is None
            or not hasattr(node_db_lock, "__enter__")
            or not hasattr(node_db_lock, "__exit__")
        ):
            return func()
        with node_db_lock:
            return func()

    def _get_metadata_snapshot(self) -> mesh_pb2.DeviceMetadata | None:
        """Return a stable snapshot of ``iface.metadata`` under the node DB lock when available."""

        def _read_and_copy() -> mesh_pb2.DeviceMetadata | None:
            metadata = getattr(self.iface, "metadata", None)
            if not isinstance(metadata, mesh_pb2.DeviceMetadata):
                return None
            metadata_snapshot = mesh_pb2.DeviceMetadata()
            metadata_snapshot.CopyFrom(metadata)
            return metadata_snapshot

        return self._execute_with_node_db_lock(_read_and_copy)

    def _set_metadata_snapshot(
        self, metadata_snapshot: mesh_pb2.DeviceMetadata
    ) -> None:
        """Persist a metadata snapshot to ``iface.metadata`` under the node DB lock when available."""

        def _write() -> None:
            stored_metadata = mesh_pb2.DeviceMetadata()
            stored_metadata.CopyFrom(metadata_snapshot)
            self.iface.metadata = stored_metadata

        self._execute_with_node_db_lock(_write)

    def _emit_cached_metadata_for_stdout(self) -> bool:
        """Emit metadata lines from ``self.iface.metadata`` for stdout parser compatibility."""
        metadata = self._get_metadata_snapshot()
        firmware_version = getattr(metadata, "firmware_version", "")
        if not isinstance(firmware_version, str) or not firmware_version:
            return False

        self._emit_metadata_line(f"\nfirmware_version: {firmware_version}")
        self._emit_metadata_line(
            f"device_state_version: {getattr(metadata, 'device_state_version', 0)}"
        )
        role = getattr(metadata, "role", 0)
        if role in config_pb2.Config.DeviceConfig.Role.values():
            self._emit_metadata_line(
                f"role: {config_pb2.Config.DeviceConfig.Role.Name(role)}"  # type: ignore[arg-type]
            )
        else:
            self._emit_metadata_line(f"role: {role}")
        self._emit_metadata_line(
            f"position_flags: {self.position_flags_list(getattr(metadata, 'position_flags', 0))}"
        )
        hw_model = getattr(metadata, "hw_model", 0)
        if hw_model in mesh_pb2.HardwareModel.values():
            self._emit_metadata_line(
                f"hw_model: {mesh_pb2.HardwareModel.Name(hw_model)}"  # type: ignore[arg-type]
            )
        else:
            self._emit_metadata_line(f"hw_model: {hw_model}")
        self._emit_metadata_line(f"hasPKC: {getattr(metadata, 'hasPKC', False)}")
        excluded_modules = getattr(metadata, "excluded_modules", 0)
        if excluded_modules > 0:
            self._emit_metadata_line(
                f"excluded_modules: {self.excluded_modules_list(excluded_modules)}"
            )
        return True

    def moduleAvailable(self, excluded_bit: int) -> bool:
        """Determine whether a specific module bit is allowed by the interface metadata.

        Parameters
        ----------
        excluded_bit : int
            Bit mask for a module as defined in DeviceMetadata.excluded_modules.

        Returns
        -------
        bool
            `True` if the bit is not set in the interface metadata (module available), or if
            metadata is missing or an error occurs; `False` if the bit is set (module excluded).
        """
        meta = getattr(self.iface, "metadata", None)
        if meta is None:
            return True
        try:
            return bool((meta.excluded_modules & excluded_bit) == 0)
        except Exception as ex:  # noqa: BLE001 - defensive metadata compatibility
            logger.debug("Unable to evaluate module availability: %s", ex)
            return True

    # COMPAT_STABLE_SHIM: alias for moduleAvailable
    def module_available(self, excluded_bit: int) -> bool:
        """Backward-compatible alias for moduleAvailable."""
        return self.moduleAvailable(excluded_bit)

    def showChannels(self) -> None:
        """Print a human-readable list of configured channels and their shareable URLs.

        Each non-disabled channel is printed with its index, role, masked PSK, and settings as JSON.
        After listing channels, the primary channel URL is printed; if the full URL that includes
        all channels differs, it is printed as the "Complete URL".
        """
        self._channel_presentation_runtime._show_channels()  # noqa: SLF001

    def showInfo(self) -> None:
        """Print the node's local and module configurations (as JSON when available) followed by its configured channels.

        If a configuration is not present, an empty placeholder is printed for that
        section. Channels are displayed using the node's channel listing format.
        """
        self._channel_presentation_runtime._show_info()  # noqa: SLF001

    def setChannels(self, channels: Sequence[channel_pb2.Channel]) -> None:
        """Set the node's channel list and normalize channel entries.

        Parameters
        ----------
        channels : collections.abc.Sequence[meshtastic.protobuf.channel_pb2.Channel]
            Sequence of channel protobufs to assign to this node. The assigned
            list will be normalized (indices fixed) and padded as needed to meet expected
            channel count.
        """
        self._channel_request_runtime.set_channels(channels)

    def requestChannels(self, startingIndex: int = 0) -> None:
        """Request channel definitions from the node, starting at the given channel index.

        When called with startingIndex 0, clears any cached channels and begins a fresh fetch into
        an internal partialChannels list. The method initiates the network request for the
        specified channel index.

        Parameters
        ----------
        startingIndex : int
            Zero-based channel index to start fetching from (typically 0-7). (Default value = 0)
        """
        self._channel_request_runtime.request_channels(starting_index=startingIndex)

    def _invalidate_channel_cache(self) -> None:
        """Clear cached channel state under the channel-state owner lock."""
        self._get_channel_state().invalidate()

    def onResponseRequestSettings(self, p: dict[str, Any]) -> None:
        """Process an admin response for a settings request and update the node's config objects.

        Parses the decoded response packet `p` to determine whether the request was acknowledged or
        rejected, marks the interface acknowledgment flags accordingly, and if the response contains
        `getConfigResponse` or `getModuleConfigResponse` copies the returned raw config into
        `self.localConfig` or `self.moduleConfig` respectively and logs the populated field.

        Parameters
        ----------
        p : dict[str, Any]
            Decoded response packet containing at least a `"decoded"` mapping with
            optional `"routing"` and `"admin"` entries. The `"admin"` entry is expected to
            contain either `getConfigResponse` or `getModuleConfigResponse` and accompanying
            `raw` bytes for the returned field.
        """
        self._settings_response_runtime.handle_settings_response(p)

    def requestConfig(
        self, configType: int | FieldDescriptor, adminIndex: int | None = None
    ) -> None:
        """Request a configuration subset or the full configuration from this node.

        If `configType` is an int it is treated as a config index. If it is a protobuf
        field descriptor, its `index` is used and the request targets `LocalConfig`
        when `containing_type.name == "LocalConfig"`, otherwise the module config is
        requested. For the local node the admin request is sent without a response
        handler; for a remote node this method registers a response handler and waits
        for an ACK/NAK before returning.

        Parameters
        ----------
        configType : int | FieldDescriptor
            Numeric config index or a
            protobuf field descriptor indicating which config field to fetch.
        adminIndex : int | None
            Admin channel index to use for sending; when None the node's
            configured admin channel is used. Pass 0 to force channel 0.
            (Default value = None)
        """
        self._settings_runtime.request_config(
            configType,
            admin_index=adminIndex,
        )

    def turnOffEncryptionOnPrimaryChannel(self) -> None:
        """Disable encryption on the primary channel and write the updated channel to the device.

        Raises
        ------
        MeshInterfaceError
            if channel data has not been loaded.
        """
        self._channel_export_runtime._turn_off_encryption_on_primary_channel()

    def waitForConfig(self, attribute: str = "channels") -> bool:
        """Wait until a node config/channel attribute is populated or timeout elapses.

        Parameters
        ----------
        attribute : str
            Attribute to wait for. ``"channels"`` waits on ``self.channels`` and
            other values wait on ``self.localConfig.<attribute>``.

        Returns
        -------
        bool
            True if the attribute was set before the timeout expired, False otherwise.
        """
        return self._channel_request_runtime.wait_for_config(attribute=attribute)

    def _raise_interface_error(self, message: str) -> NoReturn:
        """Raise a MeshInterface-style error with the provided message.

        Parameters
        ----------
        message : str
            The error message to use for the raised MeshInterfaceError.

        Raises
        ------
        MeshInterface.MeshInterfaceError
            Always raised with the provided message.
        """
        raise _MeshInterfaceError(message)

    def writeConfig(self, config_name: str) -> None:
        """Write a single named subsection of the node's edited configuration to the device.

        Sends only the specified device or module configuration section from this Node's cached
        localConfig/moduleConfig to the target node. For remote nodes the send expects an
        acknowledgment (ACK/NAK); for the local node the message is sent without waiting for an ACK/NAK.

        Parameters
        ----------
        config_name : str
            Configuration section to write. Valid values:
            "device", "position", "power", "network", "display", "lora", "bluetooth",
            "security", "sessionkey"* , "device_ui"* , "mqtt", "serial",
            "external_notification", "store_forward", "range_test", "telemetry",
            "canned_message", "audio", "remote_hardware", "neighbor_info",
            "detection_sensor", "ambient_lighting", "paxcounter",
            "statusmessage", "traffic_management".
            * Available only when present in the active protobuf schema.

        Raises
        ------
        MeshInterfaceError
            If `config_name` is not one of the supported names, or if
            localConfig/moduleConfig has not been loaded.
        """
        self._settings_runtime.write_config(config_name)

    def _write_channel_snapshot(
        self,
        channel_to_write: channel_pb2.Channel,
        adminIndex: int | None = None,
    ) -> None:
        """Write a pre-built channel snapshot to the device.

        Parameters
        ----------
        channel_to_write : channel_pb2.Channel
            Snapshot payload to send via `set_channel`.
        adminIndex : int | None
            Admin channel index to use for sending; when None the node's
            configured admin channel is used. Pass 0 to force channel 0.
            (Default value = None)
        """
        self._channel_write_runtime._write_channel_snapshot(
            channel_to_write,
            admin_index=adminIndex,
        )

    def writeChannel(
        self,
        channelIndex: int,
        adminIndex: int | None = None,
        **kwargs: Any,
    ) -> None:
        """Write the current local channel settings to the device.

        An admin session key is requested if one is not already present,
        ensuring that an admin session key is present before sending.

        Parameters
        ----------
        channelIndex : int
            Index of the channel to write.
        adminIndex : int | None
            Admin channel index to use for sending; when None the node's
            configured admin channel is used. (Default value = None)

        Raises
        ------
        MeshInterfaceError
            If channels have not been loaded (no channels to write).
        TypeError
            If unexpected keyword arguments are provided.
        """
        if kwargs:
            unexpected = ", ".join(sorted(kwargs.keys()))
            raise TypeError(
                f"writeChannel() got unexpected keyword argument(s): {unexpected}"
            )
        self._channel_write_runtime._write_channel(
            channelIndex,
            admin_index=adminIndex,
        )

    # COMPAT_STABLE_SHIM: historical channel lookup helpers return live Channel
    # objects for mutate-then-write workflows (get*() -> edit -> writeChannel()).
    # Switching these accessors to defensive copies would be a behavioral break.
    def getChannelByChannelIndex(self, channelIndex: int) -> channel_pb2.Channel | None:
        """Retrieve the channel at the given zero-based index from this node's channels.

        Parameters
        ----------
        channelIndex : int
            Zero-based channel index (typically 0-7).

        Returns
        -------
        channel_pb2.Channel | None
            The channel at the specified index, or None if channels are unset or the index is out of range.

        Notes
        -----
        Returns a live channel object by design for backward compatibility with
        existing callers that mutate a selected channel and then persist via
        `writeChannel()`.
        """
        return self._channel_lookup_runtime._get_channel_by_index(channelIndex)

    def getChannelCopyByChannelIndex(
        self, channelIndex: int
    ) -> channel_pb2.Channel | None:
        """Retrieve a defensive copy of the channel at the given zero-based index."""
        return self._channel_lookup_runtime._get_channel_copy_by_index(channelIndex)

    def deleteChannel(self, channelIndex: int) -> None:
        """Delete the channel at the given zero-based index and rewrite subsequent channels to normalize device channel state.

        Only channels with role SECONDARY or DISABLED may be removed; after
        removal, the channel list is normalized to the device channel count and
        affected channels are written back to the device. When operating on the local
        node, admin-channel indexing is adjusted so ongoing writes use the correct
        admin index.

        Parameters
        ----------
        channelIndex : int
            Zero-based index of the channel to delete.

        Raises
        ------
        MeshInterfaceError
            If channels have not been loaded.
        MeshInterfaceError
            If the channel at channelIndex is not Role.SECONDARY or Role.DISABLED.
        """
        self._delete_channel_runtime._delete_channel(channelIndex)

    def getChannelByName(self, name: str) -> channel_pb2.Channel | None:
        """Find a channel whose settings.name exactly matches the provided name.

        Parameters
        ----------
        name : str
            The channel name to search for.

        Returns
        -------
        channel_pb2.Channel | None
            The matching channel object if found, `None` otherwise.

        Notes
        -----
        Returns a live channel object by design for backward compatibility with
        existing callers that mutate a selected channel and then persist via
        `writeChannel()`.
        """
        return self._channel_lookup_runtime._get_channel_by_name(name)

    def getChannelCopyByName(self, name: str) -> channel_pb2.Channel | None:
        """Find a channel by name and return a defensive copy for read-only use."""
        return self._channel_lookup_runtime._get_channel_copy_by_name(name)

    def getDisabledChannel(self) -> channel_pb2.Channel | None:
        """Find the first channel whose role is DISABLED.

        Returns
        -------
        channel_pb2.Channel | None
            The first disabled channel if present, `None` otherwise.

        Notes
        -----
        Returns a live channel object by design for backward compatibility with
        existing callers that mutate a selected channel and then persist via
        `writeChannel()`.
        """
        return self._channel_lookup_runtime._get_disabled_channel()

    def getDisabledChannelCopy(self) -> channel_pb2.Channel | None:
        """Find the first disabled channel and return a defensive copy for read-only use."""
        return self._channel_lookup_runtime._get_disabled_channel_copy()

    def getAdminChannelIndex(self) -> int:
        """Public accessor for the admin channel index on this node."""
        return self._get_admin_channel_index()

    def _get_named_admin_channel_index(self) -> int | None:
        """Return the index of a channel explicitly named ``admin``, if present."""
        return self._channel_lookup_runtime._get_named_admin_channel_index()

    def _get_admin_channel_index(self) -> int:
        """Get the index of the channel named "admin", or 0 if no such channel exists.

        Returns
        -------
        int
            Index of the admin channel, or 0 if no channel with name "admin" is present.
        """
        return self._channel_lookup_runtime._get_admin_channel_index()

    def setOwner(
        self,
        long_name: str | None = None,
        short_name: str | None = None,
        is_licensed: bool = False,
        is_unmessagable: bool | None = None,
    ) -> mesh_pb2.MeshPacket | None:
        """Set the device owner fields (long and short names) and optional license/unmessagable flags for this node.

        Parameters
        ----------
        long_name : str | None
            Owner long name; leading/trailing whitespace is trimmed. If provided and empty after trimming, an error is raised. (Default value = None)
        short_name : str | None
            Owner short name; leading/trailing whitespace
            is trimmed and truncated to 4 characters if longer. If provided and empty
            after trimming, an error is raised. (Default value = None)
        is_licensed : bool
            If `long_name` is provided, set the owner's licensed flag. (Default value = False)
        is_unmessagable : bool | None
            If provided, set the owner's unmessagable flag. (Default value = None)

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The sent Admin message packet if available, otherwise `None`.

        Raises
        ------
        MeshInterfaceError
            If `long_name` or `short_name` is provided but empty or whitespace-only after trimming.
        """
        return self._owner_profile_runtime.set_owner(
            long_name=long_name,
            short_name=short_name,
            is_licensed=is_licensed,
            is_unmessagable=is_unmessagable,
        )

    def getURL(self, includeAll: bool = True) -> str:
        """Build a sharable meshtastic URL encoding the node's primary channel and LoRa configuration.

        Includes the secondary channels in the URL when requested.

        Parameters
        ----------
        includeAll : bool
            If True, include secondary channels in addition to the primary channel. (Default value = True)

        Returns
        -------
        share_url : str
            A meshtastic.org URL containing the encoded channel set and LoRa configuration.
        """
        return self._channel_export_runtime.get_url(include_all=includeAll)

    def setURL(self, url: str, addOnly: bool = False) -> None:
        """Parse a Mesh URL and apply its channel and LoRa configuration to this node.

        If addOnly is False, replace the node's channel list with the channels encoded in the URL
        (first becomes PRIMARY, subsequent become SECONDARY) and write each channel to the device.
        If addOnly is True, add only channels from the URL whose names are not already present,
        placing each into the first available DISABLED channel and writing it.

        Parameters
        ----------
        url : str
            A Mesh share URL containing a base64-encoded ChannelSet (e.g., .../#<base64> or .../?add=true#<base64>).
        addOnly : bool
            If True, add channels without modifying existing ones; if False, replace channels with those from the URL. (Default value = False)

        Raises
        ------
        MeshInterfaceError
            If channels or configuration are not loaded, the URL is invalid or
            contains no settings, or no free channel slot is available when adding.
        """
        channel_state = self._get_channel_state()
        if not channel_state.has_channels():
            self._raise_interface_error("Config or channels not loaded")
        parsed_input = _SetUrlParser._parse(
            url,
            raise_interface_error=self._raise_interface_error,
        )
        transaction = _SetUrlTransactionCoordinator(
            self,
            parsed_input=parsed_input,
            channel_state=channel_state,
        )
        if addOnly:
            transaction._apply_add_only()  # noqa: SLF001
            return
        transaction._apply_replace_all()  # noqa: SLF001

    def getContactURL(
        self,
        node_id: int | str,
        should_ignore: bool = False,
        manually_verified: bool = False,
    ) -> str:
        """Generate a shareable contact URL for the specified node.

        Parameters
        ----------
        node_id : int | str
            Node identifier (may be ``!hex``, ``0xhex``, decimal int/string).
        should_ignore : bool
            Mark the contact as blocked/ignored in the generated URL.
        manually_verified : bool
            Set the IS_KEY_MANUALLY_VERIFIED bit in the generated URL.

        Returns
        -------
        str
            A ``https://meshtastic.org/v/#…`` URL encoding a SharedContact protobuf.

        Raises
        ------
        MeshInterfaceError
            If the node is not in the local NodeDB, has no user data, or
            has no usable user ID. Also raised for malformed macaddr or
            publicKey fields in the NodeDB.
        """
        return self._contact_runtime.get_contact_url(
            node_id,
            should_ignore=should_ignore,
            manually_verified=manually_verified,
            decode_bytes_field=_decode_node_bytes_field,
        )

    def addContactURL(self, url: str) -> mesh_pb2.MeshPacket | None:
        """Add a contact (User) to the NodeDB from a shareable contact URL.

        Accepts a Meshtastic contact URL or any URL containing a compatible
        contact fragment (the base64 payload after ``#`` is self-contained).

        Parameters
        ----------
        url : str
            A ``https://meshtastic.org/v/#<base64>`` contact URL (or any URL
            whose fragment contains a SharedContact protobuf).

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The sent Admin message packet if available, otherwise ``None``.

        Raises
        ------
        MeshInterfaceError
            If the URL is malformed, cannot be parsed, or yields an invalid contact.
        """
        return self._contact_runtime.add_contact_url(
            url, max_payload=_MAX_CONTACT_URL_PAYLOAD
        )

    def onResponseRequestRingtone(self, p: dict[str, Any]) -> None:
        """Process an admin response containing a ringtone fragment and cache it on the Node.

        If the decoded response has no routing error and contains an admin.raw
        get_ringtone_response, stores that value in self.ringtonePart; if a routing
        error is present, the cached ringtone is not modified.

        Parameters
        ----------
        p : dict[str, Any]
            Decoded response packet from the interface. Expected to include
            a "decoded" dict with optional "routing" (containing "errorReason") and
            "admin" -> "raw" -> get_ringtone_response payload.
        """
        self._content_response_runtime._handle_ringtone_response(p)

    def _get_ringtone(self) -> str | None:
        """Retrieve the node's ringtone as a single concatenated string.

        This call will wait for a device response and may block until the node replies
        or the node's timeout elapses. If the External Notification module is excluded
        by firmware, or if no ringtone is available or the request times out, the
        method returns None.

        Returns
        -------
        str | None
            The complete ringtone string if available, `None` if the
            module is not present, the ringtone is unavailable, or the request
            timed out.
        """
        return self._content_request_runtime.read_ringtone()

    def _set_ringtone(self, ringtone: str) -> mesh_pb2.MeshPacket | None:
        """Set the node's ringtone.

        Validates that the External Notification module is available and that the ringtone length
        is 230 characters or fewer; ensures an admin session key, then sends one admin message.
        Returns None if the External Notification module is not available. For remote nodes the
        send waits for an ACK/NAK response.

        Parameters
        ----------
        ringtone : str
            The ringtone text to set.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The result of sending the AdminMessage for the first chunk, or `None` if the External Notification module is unavailable.

        Raises
        ------
        MeshInterfaceError
            If `ringtone` length exceeds 230 characters.
        """
        return self._content_request_runtime.write_ringtone(ringtone)

    def onResponseRequestCannedMessagePluginMessageMessages(
        self, p: dict[str, Any]
    ) -> None:
        """Handle the admin response for a canned-message plugin messages request.

        If the response indicates a routing error, prints the error. On a successful response,
        stores the `get_canned_message_module_messages_response` payload from the admin raw data
        into `self.cannedPluginMessageMessages`.

        Parameters
        ----------
        p : dict[str, Any]
            Decoded packet dictionary containing response fields, expected to include
            keys like `"decoded"`, `"decoded"]["routing"]`, and `"decoded"]["admin"]["raw"]`.
        """
        self._content_response_runtime._handle_canned_message_response(p)

    def _get_canned_message(self) -> str | None:
        """Retrieve the device's canned message, requesting parts from the node if not already cached.

        If the canned-message module is excluded by firmware, returns None. When a
        request is made this call blocks until a response is received or the operation
        times out.

        Returns
        -------
        str | None
            str or None: The assembled canned message if available, or None if the module is unavailable or no response was received.
        """
        return self._content_request_runtime.read_canned_message()

    def _set_canned_message(self, message: str) -> mesh_pb2.MeshPacket | None:
        """Set the device's canned message.

        If the canned-message module is not available on the device, the method logs a warning and
        returns None. If the provided message is longer than 200 characters, a MeshInterfaceError
        is raised. The message is sent with one admin request (waiting for an ACK/NAK when
        targeting a remote node).

        Parameters
        ----------
        message : str
            The canned message to set; must be 200 characters or fewer.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The result returned by _send_admin for the first chunk, or `None` if the canned-message module is unavailable.

        Raises
        ------
        MeshInterfaceError
            If `message` length is greater than 200 characters.
        """
        return self._content_request_runtime.write_canned_message(message)

    # COMPAT_STABLE_SHIM: alias for getRingtone
    def get_ringtone(self) -> str | None:
        """Compatibility wrapper that returns the node's ringtone.

        Canonical public method: getRingtone().

        Returns
        -------
        ringtone : str | None
            The ringtone string if available, or None if unavailable or unsupported.
        """
        return self.getRingtone()

    # COMPAT_STABLE_SHIM: alias for setRingtone
    def set_ringtone(self, ringtone: str) -> mesh_pb2.MeshPacket | None:
        """Set the device's ringtone.

        Backward-compatibility alias for setRingtone().

        Parameters
        ----------
        ringtone : str
            Ringtone payload to apply to the device.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The Admin MeshPacket sent for the request, or `None` if no packet was produced.
        """
        return self.setRingtone(ringtone)

    # COMPAT_STABLE_SHIM: alias for getCannedMessage
    def get_canned_message(self) -> str | None:
        """Return the device's canned message.

        Canonical public method: getCannedMessage().

        Returns
        -------
        str | None
            The canned message string if available, `None` otherwise.
        """
        return self.getCannedMessage()

    # COMPAT_STABLE_SHIM: alias for setCannedMessage
    def set_canned_message(self, message: str) -> mesh_pb2.MeshPacket | None:
        """Set the device's canned message using a backward-compatible snake_case wrapper.

        Backward-compatibility alias for setCannedMessage().

        Parameters
        ----------
        message : str
            The canned message text to set (maximum 200 characters).

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The Admin MeshPacket that was sent, or `None` if no packet is produced.
        """
        return self.setCannedMessage(message)

    def getRingtone(self) -> str | None:
        """Get the node's ringtone.

        Returns
        -------
        str | None
            The ringtone data as a single concatenated string, or `None` if the ringtone is unavailable.
        """
        return self._get_ringtone()

    def setRingtone(self, ringtone: str) -> mesh_pb2.MeshPacket | None:
        """Set the node's ringtone.

        Parameters
        ----------
        ringtone : str
            Ringtone string to set (maximum 230 characters).

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The Admin MeshPacket sent to set the ringtone,
            or `None` if the operation could not be completed (for example, the
            ringtone feature is unavailable or the request timed out).
        """
        return self._set_ringtone(ringtone)

    def getCannedMessage(self) -> str | None:
        """Retrieve the node's canned message.

        Returns
        -------
        str | None
            The canned message string if available, `None` otherwise.
        """
        return self._get_canned_message()

    def setCannedMessage(self, message: str) -> mesh_pb2.MeshPacket | None:
        """Set the node's canned message.

        Validates module availability and that `message` is at most 200 characters,
        ensures an admin session key, sends the AdminMessage to write the canned
        message, and invalidates any cached canned-message state.

        Parameters
        ----------
        message : str
            The canned message text to set (maximum 200 characters).

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The sent MeshPacket if a packet was transmitted, `None` if no packet was sent.
        """
        return self._set_canned_message(message)

    def exitSimulator(self) -> mesh_pb2.MeshPacket | None:
        """Request the target simulator process to exit; has no effect on non-simulator nodes.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            A MeshPacket for the sent admin request, or `None` if the admin message was not sent.
        """
        return self._admin_command_runtime.exit_simulator()

    def reboot(self, secs: int = 10) -> mesh_pb2.MeshPacket | None:
        """Request the node to reboot after a delay.

        Parameters
        ----------
        secs : int
            Number of seconds to wait before rebooting. (Default value = 10)

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The AdminMessage packet sent to the node, or `None` if no packet was sent.
        """
        return self._admin_command_runtime.reboot(secs)

    def beginSettingsTransaction(self) -> mesh_pb2.MeshPacket | None:
        """Request the node to open a settings edit transaction.

        Ensures an admin session key exists before sending the request and uses
        ACK/NAK handling for remote nodes while not waiting for a response from
        the local node.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The sent admin packet if available, or `None` otherwise.
        """
        return self._admin_command_runtime.begin_settings_transaction()

    def commitSettingsTransaction(self) -> mesh_pb2.MeshPacket | None:
        """Commit the node's open settings edit transaction.

        For remote nodes, waits for an ACK/NAK response; for the local node the commit is sent without waiting for a response.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The sent Admin `MeshPacket` when available, or `None`.
        """
        return self._admin_command_runtime.commit_settings_transaction()

    def rebootOTA(self, secs: int = 10) -> mesh_pb2.MeshPacket | None:
        """Request the node to perform an OTA reboot after a given delay.

        Parameters
        ----------
        secs : int
            Seconds to wait before rebooting. (Default value = 10)

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The sent Admin message packet, or `None` if no packet was produced.
        """
        return self._admin_command_runtime.reboot_ota(secs)

    def startOTA(
        self,
        mode: admin_pb2.OTAMode.ValueType | None = None,
        ota_file_hash: bytes | None = None,
        *,
        ota_mode: admin_pb2.OTAMode.ValueType | None = None,
        ota_hash: bytes | None = None,
        **kwargs: Any,
    ) -> mesh_pb2.MeshPacket | None:
        """Request OTA mode for local node firmware that supports ota_request.

        Parameters
        ----------
        mode : admin_pb2.OTAMode.ValueType | None
            OTA transport mode to use after reboot (for example, ``admin_pb2.OTA_WIFI``).
            Can also be passed as positional first argument for backward compatibility.
        ota_file_hash : bytes | None
            Firmware hash bytes used by the node to validate OTA payload consistency.
            Can also be passed as positional second argument for backward compatibility.
        ota_mode : admin_pb2.OTAMode.ValueType | None
            Backward-compatible keyword alias for ``mode``.
        ota_hash : bytes | None
            Backward-compatible keyword alias for ``ota_file_hash``.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The sent Admin message packet, or ``None`` if no packet was produced.

        Raises
        ------
        MeshInterfaceError
            If called for a non-local node.
        """
        return self._admin_command_runtime.start_ota(
            mode=mode,
            ota_file_hash=ota_file_hash,
            ota_mode=ota_mode,
            ota_hash=ota_hash,
            **kwargs,
        )

    def enterDFUMode(self) -> mesh_pb2.MeshPacket | None:
        """Request the node to enter DFU (NRF52) mode.

        Ensures an admin session key exists and sends an AdminMessage requesting DFU mode.
        When targeting a remote node, waits for an ACK/NAK response; local node sends without waiting.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The sent Admin message packet, or `None` if no packet was sent.
        """
        return self._admin_command_runtime.enter_dfu_mode()

    def shutdown(self, secs: int = 10) -> mesh_pb2.MeshPacket | None:
        """Request the node to shut down after a given number of seconds.

        Parameters
        ----------
        secs : int
            Number of seconds until the node shuts down. (Default value = 10)

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The AdminMessage packet that was sent, or `None` if no packet was sent.
        """
        return self._admin_command_runtime.shutdown(secs)

    def getMetadata(self) -> None:
        """Request the node's device metadata and wait for an acknowledgement.

        Sends a metadata request to the node and blocks until an ACK or NAK is received; the
        received metadata is processed asynchronously when the response arrives.
        """
        p = admin_pb2.AdminMessage()
        p.get_device_metadata_request = True
        logger.info("Requesting device metadata")
        metadata_stdout_event = threading.Event()
        with self._metadata_stdout_event_lock:
            self._metadata_stdout_event = metadata_stdout_event
        try:
            request = _send_admin_with_ack_scope(
                self,
                p,
                scope_ack=True,
                wantResponse=True,
                onResponse=self.onRequestGetMetadata,
            )
            _wait_for_admin_ack(self, request)
            if sys.stdout is not sys.__stdout__:
                callback_completed = metadata_stdout_event.wait(
                    METADATA_STDOUT_COMPAT_WAIT_SECONDS
                )
                # Ensure redirected-stdout parsers receive a deterministic metadata line
                # only when callback output may have been missed.
                if not callback_completed:
                    self._emit_cached_metadata_for_stdout()
        finally:
            with self._metadata_stdout_event_lock:
                if self._metadata_stdout_event is metadata_stdout_event:
                    self._metadata_stdout_event = None

    def factoryReset(self, full: bool = False) -> mesh_pb2.MeshPacket | None:
        """Request a factory reset on the node.

        Parameters
        ----------
        full : bool
            If True, perform a full device factory reset; if False, reset configuration only. (Default value = False)

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The sent admin packet if sending succeeded, or None otherwise.
        """
        return self._admin_command_runtime.factory_reset(full=full)

    def _get_factory_reset_request_value(self) -> int:
        """Return factory-reset sentinel value used for admin reset requests."""
        return FACTORY_RESET_REQUEST_VALUE

    def removeNode(self, nodeId: int | str) -> mesh_pb2.MeshPacket | None:
        """Request removal of the mesh node identified by nodeId.

        Converts nodeId to a numeric node number and sends a remove-by-node-number
        admin request to the device. For remote targets, the request uses ACK/NAK
        handling; for the local node, no response callback is used.

        Parameters
        ----------
        nodeId : int | str
            Node number or a string convertible to a node number.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The admin packet returned by the send operation if available, `None` otherwise.
        """
        return self._admin_command_runtime.remove_node(nodeId)

    def setFavorite(self, nodeId: int | str) -> mesh_pb2.MeshPacket | None:
        """Mark a node as a favorite in the target device's NodeDB.

        Parameters
        ----------
        nodeId : int | str
            Node identifier (numeric or numeric string); will be converted to a node number.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The response packet if one was received, `None` otherwise.
        """
        return self._admin_command_runtime.set_favorite(nodeId)

    def removeFavorite(self, nodeId: int | str) -> mesh_pb2.MeshPacket | None:
        """Unmark a node as a favorite in the device's NodeDB.

        Parameters
        ----------
        nodeId : int | str
            Numeric node identifier or a string that can be converted to one.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The Admin packet sent to the device, or `None` if no packet was sent.
        """
        return self._admin_command_runtime.remove_favorite(nodeId)

    def setIgnored(self, nodeId: int | str) -> mesh_pb2.MeshPacket | None:
        """Mark a node in the device NodeDB as ignored.

        Parameters
        ----------
        nodeId : int | str
            Node number or string convertible to a node number.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The AdminMessage/packet sent to request the change, or `None` if no packet was sent.
        """
        return self._admin_command_runtime.set_ignored(nodeId)

    def removeIgnored(self, nodeId: int | str) -> mesh_pb2.MeshPacket | None:
        """Unmark a node as ignored in the device's NodeDB.

        Parameters
        ----------
        nodeId : int | str
            Node identifier (integer or numeric string). It will be converted to a numeric node number.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            `mesh_pb2.MeshPacket` if an AdminMessage was sent, `None` otherwise.
        """
        return self._admin_command_runtime.remove_ignored(nodeId)

    def resetNodeDb(self) -> mesh_pb2.MeshPacket | None:
        """Request that the node clear its stored NodeDB (node database).

        Ensures an admin session key exists before sending. For remote targets, this
        waits for an ACK/NAK response; for the local node, it does not wait.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The AdminMessage packet sent, or `None` if no packet was sent.
        """
        return self._admin_command_runtime.reset_node_db()

    def _send_admin_op(
        self, message: admin_pb2.AdminMessage
    ) -> mesh_pb2.MeshPacket | None:
        """Send a one-shot admin message, waiting for ACK/NAK on remote nodes."""
        self.ensureSessionKey()
        if self is self.iface.localNode:
            return self._send_admin(message)
        request = _send_admin_with_ack_scope(
            self,
            message,
            scope_ack=True,
            onResponse=self.onAckNak,
        )
        if request is not None:
            _wait_for_admin_ack(self, request)
        return request

    def backupPreferences(self, location: str = "flash") -> mesh_pb2.MeshPacket | None:
        """Back up the device's preferences to internal flash or SD storage.

        Parameters
        ----------
        location : str
            ``flash`` or ``sd`` backup location.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The AdminMessage packet sent, or `None` if no packet was sent.
        """
        message = admin_pb2.AdminMessage()
        message.backup_preferences = _backup_location_value(location)
        logger.info("Backing up preferences to %s", location)
        return self._send_admin_op(message)

    def restorePreferences(self, location: str = "flash") -> mesh_pb2.MeshPacket | None:
        """Restore the device's preferences from internal flash or SD storage.

        Parameters
        ----------
        location : str
            ``flash`` or ``sd`` backup location.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The AdminMessage packet sent, or `None` if no packet was sent.
        """
        message = admin_pb2.AdminMessage()
        message.restore_preferences = _backup_location_value(location)
        logger.info("Restoring preferences from %s", location)
        return self._send_admin_op(message)

    def removeBackupPreferences(
        self, location: str = "flash"
    ) -> mesh_pb2.MeshPacket | None:
        """Remove a stored preferences backup from flash or SD.

        Parameters
        ----------
        location : str
            ``flash`` or ``sd`` backup location.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The AdminMessage packet sent, or `None` if no packet was sent.
        """
        message = admin_pb2.AdminMessage()
        message.remove_backup_preferences = _backup_location_value(location)
        logger.info("Removing %s preferences backup", location)
        return self._send_admin_op(message)

    def toggleMutedNode(self, nodeId: int | str) -> mesh_pb2.MeshPacket | None:
        """Toggle the muted flag for a node in the target device's NodeDB.

        Parameters
        ----------
        nodeId : int | str
            Node identifier (numeric or numeric string).

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The AdminMessage packet sent, or `None` if no packet was sent.
        """
        message = admin_pb2.AdminMessage()
        message.toggle_muted_node = toNodeNum(nodeId)
        logger.info("Toggling muted flag for node %s", nodeId)
        return self._send_admin_op(message)

    def deleteFile(self, path: str) -> mesh_pb2.MeshPacket | None:
        """Delete a file from the node's filesystem.

        Parameters
        ----------
        path : str
            Absolute path of the file on the device filesystem.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The AdminMessage packet sent, or `None` if no packet was sent.
        """
        if not path.startswith("/"):
            self._raise_interface_error("File path must be absolute (start with '/').")
        validation_error = _delete_file_path_error(path)
        if validation_error is not None:
            self._raise_interface_error(validation_error)
        message = admin_pb2.AdminMessage()
        message.delete_file_request = path
        logger.info("Requesting deletion of %s", path)
        return self._send_admin_op(message)

    def sendInputEvent(
        self,
        event_code: int,
        kb_char: int = 0,
        touch_x: int = 0,
        touch_y: int = 0,
    ) -> mesh_pb2.MeshPacket | None:
        """Inject a physical input event (button/keyboard/touch) into the node.

        Parameters
        ----------
        event_code : int
            Firmware input event code.
        kb_char : int
            Keyboard character code for key events.
        touch_x : int
            Horizontal touch coordinate for touch events.
        touch_y : int
            Vertical touch coordinate for touch events.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The AdminMessage packet sent, or `None` if no packet was sent.
        """
        if not 0 <= event_code <= _MAX_INPUT_EVENT_CODE:
            self._raise_interface_error(
                f"event_code must be between 0 and {_MAX_INPUT_EVENT_CODE}, got {event_code}"
            )
        if not 0 <= kb_char <= _MAX_INPUT_KB_CHAR:
            self._raise_interface_error(
                f"kb_char must be between 0 and {_MAX_INPUT_KB_CHAR}, got {kb_char}"
            )
        if not 0 <= touch_x <= _MAX_INPUT_TOUCH_X:
            self._raise_interface_error(
                f"touch_x must be between 0 and {_MAX_INPUT_TOUCH_X}, got {touch_x}"
            )
        if not 0 <= touch_y <= _MAX_INPUT_TOUCH_Y:
            self._raise_interface_error(
                f"touch_y must be between 0 and {_MAX_INPUT_TOUCH_Y}, got {touch_y}"
            )
        message = admin_pb2.AdminMessage()
        message.send_input_event.event_code = event_code
        message.send_input_event.kb_char = kb_char
        message.send_input_event.touch_x = touch_x
        message.send_input_event.touch_y = touch_y
        logger.info("Sending input event %d", event_code)
        return self._send_admin_op(message)

    def _request_admin_response(
        self,
        message: admin_pb2.AdminMessage,
        response_field_name: str,
        response_type: type[_AdminResponseT],
        *,
        response_timeout_seconds: float,
    ) -> _AdminResponseT | None:
        """Send an admin request and return a copied named response field."""
        if response_timeout_seconds <= 0:
            raise ValueError("response timeout must be positive")

        result: _AdminResponseT | None = None
        completed = threading.Event()

        def _on_response(packet: dict[str, Any]) -> None:
            nonlocal result
            decoded = packet.get("decoded") if isinstance(packet, dict) else None
            admin_section = decoded.get("admin") if isinstance(decoded, dict) else None
            raw_admin = (
                admin_section.get("raw") if isinstance(admin_section, dict) else None
            )
            has_field = getattr(raw_admin, "HasField", None)
            try:
                present = callable(has_field) and bool(has_field(response_field_name))
            except (TypeError, ValueError):
                present = False
            if not present or raw_admin is None:
                return
            response = response_type()
            response.CopyFrom(getattr(raw_admin, response_field_name))
            result = response
            completed.set()

        request = _send_admin_with_ack_scope(
            self,
            message,
            scope_ack=False,
            wantResponse=True,
            onResponse=_on_response,
        )
        if request is None:
            return None
        # A want_response admin request is answered by the data response
        # itself; firmware sends no separate RoutingACK for it, so no scoped
        # ACK bookkeeping is registered (``scope_ack=False``) and nothing ever
        # retires it. The bounded correlated-response event below decides
        # success, and skipping the WAIT_ATTR_NAK registration keeps the
        # response handler prunable when the device never answers.
        if not completed.wait(timeout=response_timeout_seconds):
            return None
        return result

    def requestDeviceConnectionStatus(
        self, *, response_timeout_seconds: float = ADMIN_RESPONSE_WAIT_SECONDS
    ) -> connection_status_pb2.DeviceConnectionStatus | None:
        """Request the node's connectivity status and wait for the response.

        Parameters
        ----------
        response_timeout_seconds : float, optional
            Seconds to wait for the correlated admin RESPONSE packet after
            the request is sent. No ACK/NAK wait is performed. ``None`` is
            returned if the wait expires before the device reports back.
            Defaults to ``ADMIN_RESPONSE_WAIT_SECONDS``.

        Returns
        -------
        connection_status_pb2.DeviceConnectionStatus | None
            The reported status, or `None` when no response arrived.
        """
        message = admin_pb2.AdminMessage()
        message.get_device_connection_status_request = True
        logger.info("Requesting device connection status")
        return self._request_admin_response(
            message,
            "get_device_connection_status_response",
            connection_status_pb2.DeviceConnectionStatus,
            response_timeout_seconds=response_timeout_seconds,
        )

    def requestUiConfig(
        self, *, response_timeout_seconds: float = ADMIN_RESPONSE_WAIT_SECONDS
    ) -> device_ui_pb2.DeviceUIConfig | None:
        """Request the node's device UI configuration and wait for the response.

        Parameters
        ----------
        response_timeout_seconds : float, optional
            Seconds to wait for the correlated admin RESPONSE packet after
            the request is sent. No ACK/NAK wait is performed. ``None`` is
            returned if the wait expires before the device reports back.
            Defaults to ``ADMIN_RESPONSE_WAIT_SECONDS``.

        Returns
        -------
        device_ui_pb2.DeviceUIConfig | None
            The reported UI configuration, or `None` when no response arrived.
        """
        message = admin_pb2.AdminMessage()
        message.get_ui_config_request = True
        logger.info("Requesting device UI configuration")
        return self._request_admin_response(
            message,
            "get_ui_config_response",
            device_ui_pb2.DeviceUIConfig,
            response_timeout_seconds=response_timeout_seconds,
        )

    def requestNodeRemoteHardwarePins(
        self, *, response_timeout_seconds: float = ADMIN_RESPONSE_WAIT_SECONDS
    ) -> admin_pb2.NodeRemoteHardwarePinsResponse | None:
        """Request the mesh's nodes with their available GPIO pins for RemoteHardware use.

        Parameters
        ----------
        response_timeout_seconds : float, optional
            Seconds to wait for the correlated admin RESPONSE packet after
            the request is sent. No ACK/NAK wait is performed. ``None`` is
            returned if the wait expires before the device reports back.
            Defaults to ``ADMIN_RESPONSE_WAIT_SECONDS``.

        Returns
        -------
        admin_pb2.NodeRemoteHardwarePinsResponse | None
            The reported remote-hardware pin inventory, or ``None`` when no
            response arrived.
        """
        message = admin_pb2.AdminMessage()
        message.get_node_remote_hardware_pins_request = True
        logger.info("Requesting node remote hardware pins")
        return self._request_admin_response(
            message,
            "get_node_remote_hardware_pins_response",
            admin_pb2.NodeRemoteHardwarePinsResponse,
            response_timeout_seconds=response_timeout_seconds,
        )

    def storeUiConfig(
        self, config: device_ui_pb2.DeviceUIConfig
    ) -> mesh_pb2.MeshPacket | None:
        """Store a device UI configuration onto the node.

        Parameters
        ----------
        config : device_ui_pb2.DeviceUIConfig
            Complete UI configuration to persist.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The AdminMessage packet sent, or `None` if no packet was sent.
        """
        message = admin_pb2.AdminMessage()
        message.store_ui_config.CopyFrom(config)
        logger.info("Storing device UI configuration")
        return self._send_admin_op(message)

    def setFixedPosition(
        self, lat: int | float | None, lon: int | float | None, alt: int | None
    ) -> mesh_pb2.MeshPacket | None:
        """Set the node's fixed position and enable the fixed-position setting on the device.

        Parameters
        ----------
        lat : int | float | None
            Latitude specified either as an integer in 1e-7 degrees units or as a float in decimal degrees.
            Pass ``None`` to leave latitude unset.
        lon : int | float | None
            Longitude specified either as an integer in 1e-7 degrees units or as a float in decimal degrees.
            Pass ``None`` to leave longitude unset.
        alt : int | None
            Altitude in meters. Pass ``None`` to leave altitude unset.

        Returns
        -------
        mesh_packet : mesh_pb2.MeshPacket | None
            The result from sending the AdminMessage, or `None` if no packet was sent.
        """
        return self._position_time_runtime._set_fixed_position(
            lat=lat,
            lon=lon,
            alt=alt,
        )

    def removeFixedPosition(self) -> mesh_pb2.MeshPacket | None:
        """Clear the node's fixed position setting.

        Sends an AdminMessage requesting removal of the node's fixed position; remote nodes will use ACK/NAK handling.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The sent AdminMessage (`mesh_pb2.MeshPacket`) if a packet was transmitted, or `None` if sending was skipped.
        """
        return self._position_time_runtime._remove_fixed_position()

    def setTime(self, timeSec: int = 0) -> mesh_pb2.MeshPacket | None:
        """Set the node's clock to the specified Unix timestamp.

        If `timeSec` is 0, the system's current time is used. The call sends an
        AdminMessage to set the node time; for remote nodes, the function waits for
        an ACK/NAK response.

        Parameters
        ----------
        timeSec : int
            Unix timestamp in seconds to set on the node; pass 0 to use the current system time. (Default value = 0)

        Returns
        -------
        mesh_pb2.MeshPacket | None
            mesh_pb2.MeshPacket or None: The sent AdminMessage packet when available, or `None` if no packet is produced.
        """
        return self._position_time_runtime._set_time(time_sec=timeSec)

    def _fixup_channels(self) -> None:
        """Normalize the node's channel list by assigning sequential index values and ensuring the list contains the expected number of channels.

        If `channels` is None this is a no-op. Otherwise this method truncates channel lists
        that exceed the device limit, sets each remaining channel's `index` field to its position
        in the list (starting at 0), and appends disabled channels as needed so the channel list
        reaches the required length.
        """
        self._channel_normalization_runtime._fixup_channels()

    def _fixup_channels_locked(self) -> None:
        """Normalize channel indices and size while holding ``self._channels_lock``."""
        self._channel_normalization_runtime._fixup_channels_locked()

    def _fill_channels(self) -> None:
        """Ensure the node has exactly eight channels by appending DISABLED channels as needed.

        If `self.channels` is None this is a no-op. Appends new Channel objects with
        role `DISABLED` and sequential `index` values until the list length reaches
        ``MAX_CHANNELS``.
        """
        self._channel_normalization_runtime._fill_channels()

    def _fill_channels_locked(self) -> None:
        """Append disabled channels up to ``MAX_CHANNELS`` while holding ``self._channels_lock``."""
        self._channel_normalization_runtime._fill_channels_locked()

    def onRequestGetMetadata(self, p: dict[str, Any]) -> None:
        """Handle an incoming device metadata response packet and display the parsed metadata.

        Parses the decoded packet, updates the interface acknowledgment state (ACK/NAK), handles
        routing-layer ACK/NAK packets, and logs the device metadata
        fields (firmware_version, device_state_version, role, position_flags, hw_model, hasPKC,
        and excluded_modules) when available.

        Parameters
        ----------
        p : dict[str, Any]
            Decoded packet containing at minimum a 'decoded' key with routing and
            admin/raw get_device_metadata_response fields.
        """
        self._metadata_response_runtime.handle_metadata_response(p)

    def onResponseRequestChannel(self, p: dict[str, Any]) -> None:
        """Process a response packet for a previously requested channel and update the Node's channel state.

        If the packet is a routing message with a retryable error, retry the in-flight channel
        request index. If the packet contains an admin get_channel_response, append that channel to
        the node's partial channel list, reset the request timeout, and either continue requesting
        the next channel or, when the final channel is received, replace the node's channels with
        the collected channels and normalize them.

        Parameters
        ----------
        p : dict[str, Any]
            Decoded packet dictionary from the interface. Expected to contain either
            - a routing message with 'routing.errorReason', or
            - an admin message with 'admin.raw.get_channel_response' (a Channel protobuf-like object with an `index` field).
        """
        self._channel_response_runtime.handle_channel_response(p)

    def onAckNak(self, p: dict[str, Any]) -> None:
        """Handle an incoming ACK/NAK admin response and update interface acknowledgment state.

        Inspect the routing error reason in the parsed packet `p` and:
        - If the errorReason is not "NONE", log a NAK message and set
          iface._acknowledgment.receivedNak to True.
        - If the errorReason is "NONE" and the packet originates from the local node, log an
          implicit-ACK message and set iface._acknowledgment.receivedImplAck to True.
        - Otherwise log a normal ACK message and set iface._acknowledgment.receivedAck to True.

        Parameters
        ----------
        p : dict[str, Any]
            Parsed packet dictionary expected to contain:
            - p["decoded"]["routing"]["errorReason"]: routing error reason string.
            - p["from"]: numeric origin node identifier (string or int convertible).
        """
        self._ack_nak_runtime._handle_ack_nak(p)

    def _request_channel(self, channelNum: int) -> mesh_pb2.MeshPacket | None:
        """Request settings for a single channel from this node.

        Sends an admin request for the channel at the given zero-based index and registers the response handler.

        Parameters
        ----------
        channelNum : int
            Zero-based index of the channel to request.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The AdminMessage packet sent to the interface, or `None` if sending was skipped (e.g., protocol disabled).
        """
        return self._channel_request_runtime.request_channel(channelNum)

    # pylint: disable=R1710
    def _send_admin(
        self,
        p: admin_pb2.AdminMessage,
        wantResponse: bool = False,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        adminIndex: int | None = None,
        responseWaitAttr: str | None = None,
    ) -> mesh_pb2.MeshPacket | None:
        """Send an AdminMessage to this Node's admin channel.

        Parameters
        ----------
        p : admin_pb2.AdminMessage
            AdminMessage to send; a session passkey may be attached.
        wantResponse : bool
            Request a response from the recipient when True. (Default value = False)
        onResponse : Callable[[dict[str, Any]], Any] | None
            Optional callback invoked with the received response packet. (Default value = None)
        adminIndex : int | None
            Channel index to use for the admin message; when None the node's
            configured admin channel is used. Pass 0 to force channel 0.
            (Default value = None)
        responseWaitAttr : str | None
            Optional internal request-scoped wait attribute registered before
            transmission. This prevents fast responses from racing the caller's
            post-send wait setup. (Default value = None)

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The MeshPacket returned by the send operation,
            or `None` if sending was skipped because protocol use is disabled.
        """
        return self._admin_transport_runtime._send_admin(
            p,
            want_response=wantResponse,
            on_response=onResponse,
            admin_index=adminIndex,
            response_wait_attr=responseWaitAttr,
        )

    def ensureSessionKey(self, adminIndex: int | None = None) -> None:
        """Ensure an admin session key exists for this node, requesting one if missing.

        If protocol use is disabled (`noProto`), no action is taken. Otherwise, if the node has no
        `adminSessionPassKey` recorded, a session-key request is sent.

        Parameters
        ----------
        adminIndex : int | None
            Admin channel index to use for the session key request; when None
            the node's configured admin channel is used. Pass 0 to force
            channel 0. (Default value = None)
        """
        self._admin_session_runtime._ensure_session_key(admin_index=adminIndex)

    def _get_channels_with_hash(self) -> list[dict[str, Any]]:
        """Return a list of channel descriptors containing index, role, name, and an optional hash.

        Returns
        -------
        list[dict[str, Any]]
            A list of dictionaries, each with keys:
            - "index" (int): The channel's zero-based index.
            - "role" (str): The channel role name.
            - "name" (str): The channel settings name, or an empty string if missing.
            - "hash" (int or None): Computed channel hash when both name and PSK are present, otherwise None.
        """
        return self._channel_export_runtime.get_channels_with_hash()

    # COMPAT_STABLE_SHIM: alias for getChannelsWithHash
    def get_channels_with_hash(self) -> list[dict[str, Any]]:
        """Get channel entries with computed per-channel hashes.

        Each entry is a dict containing:
        - `index` (int): zero-based channel index.
        - `role` (str): channel role name.
        - `name` (str): channel settings name, or an empty string if unset.
        - `hash` (int | None): computed channel hash when both `name` and PSK are present, otherwise `None`.

        Returns
        -------
        list[dict[str, Any]]
            The list of channel entries described above.
        """
        return self.getChannelsWithHash()

    def getChannelsWithHash(self) -> list[dict[str, Any]]:
        """Compatibility wrapper that returns channel entries including computed per-channel hashes.

        Returns
        -------
        list[dict[str, Any]]
            A list of dictionaries, each with keys 'index', 'role', 'name', and 'hash'
            where 'hash' is the computed channel hash when both name and PSK are
            present, or `None` otherwise.
        """
        return self._get_channels_with_hash()
