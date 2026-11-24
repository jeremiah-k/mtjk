"""Channel/config request bootstrap runtime owner."""

import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Protocol, cast

from meshtastic.node_runtime.ports import _NodePort
from meshtastic.protobuf import admin_pb2, channel_pb2, mesh_pb2
from meshtastic.util import Timeout

from .channel_state import _NodeChannelState
from .shared import MAX_CHANNELS

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)


class _HasChannelRequestFailed(Protocol):
    """Protocol for objects providing channel request failure check."""

    def has_channel_request_failed(self) -> bool:
        """Return True if a channel request has failed."""
        ...  # pylint: disable=unnecessary-ellipsis


def _get_channel_request_failed_fn(
    channel_response_runtime: object,
) -> Callable[[], bool] | None:
    """Extract the channel-request failure probe when available."""
    fn = getattr(channel_response_runtime, "has_channel_request_failed", None)
    if callable(fn):
        return cast(Callable[[], bool] | None, fn)
    return None


class _LocalConfigFieldProbe:
    """Expose local-config field presence as a boolean wait target."""

    def __init__(self, *, has_field_fn: Callable[[str], bool], name: str) -> None:
        self._has_field_fn = has_field_fn
        self._name = name

    @property
    def is_set(self) -> bool:
        """Return whether the target localConfig field is currently set."""
        try:
            return bool(self._has_field_fn(self._name))
        except (TypeError, ValueError) as exc:
            logger.debug("HasField check failed for %r: %s", self._name, exc)
            return False


class _ChannelRequestCompletionProbe:
    """Expose channel-request completion as a boolean wait target."""

    def __init__(
        self,
        *,
        channel_state: _NodeChannelState,
        channel_response_runtime: _HasChannelRequestFailed | None,
    ) -> None:
        self._channel_state = channel_state
        self._channel_response_runtime = channel_response_runtime

    @property
    def is_set(self) -> bool:
        """Return True once channels are loaded or the request has terminally failed."""
        if self._channel_state.has_channels():
            return True
        has_channel_request_failed = _get_channel_request_failed_fn(
            self._channel_response_runtime
        )
        return bool(
            has_channel_request_failed is not None and has_channel_request_failed()
        )


class _NodeChannelRequestRuntime:
    """Owns channel/config bootstrap, waiting, and request-channel send path."""

    def __init__(
        self,
        node: "Node",
        port: _NodePort | None = None,
        *,
        channel_state: _NodeChannelState,
    ) -> None:
        """Initialize the runtime with its facade and collaborator port.

        Parameters
        ----------
        node : Node
            The facade whose public state the runtime reads.
        port : _NodePort | None
            Narrow port over the facade's internals; derived from ``node``
            when omitted.
        channel_state : _NodeChannelState
            Shared channel-state owner.
        """
        self._node = node
        self._port = port if port is not None else _NodePort(node)
        self._channel_state = channel_state

    def set_channels(self, channels: Sequence[channel_pb2.Channel]) -> None:
        """Set channels from a sequence with copy and normalization semantics."""
        self._channel_state.replace_with_copies(channels)

    def request_channels(self, *, starting_index: int = 0) -> None:
        """Bootstrap channel request flow from ``starting_index``."""
        logger.debug("requestChannels for nodeNum:%s", self._node.nodeNum)
        if not 0 <= starting_index < MAX_CHANNELS:
            logger.warning(
                "Invalid starting_index %d (must be 0-%d), ignoring request.",
                starting_index,
                MAX_CHANNELS - 1,
            )
            return
        if starting_index == 0:
            self._channel_state.reset_for_download()
        self.request_channel(starting_index)

    def wait_for_config(self, *, attribute: str = "channels") -> bool:
        """Wait for node attribute using historical timeout semantics."""
        if attribute == "channels":
            channel_response_runtime = getattr(
                self._node,
                "_channel_response_runtime",
                None,
            )
            has_channel_request_failed = (
                getattr(channel_response_runtime, "has_channel_request_failed", None)
                if channel_response_runtime is not None
                else None
            )
            if callable(has_channel_request_failed):
                probe = _ChannelRequestCompletionProbe(
                    channel_state=self._channel_state,
                    channel_response_runtime=channel_response_runtime,
                )
                completed = self._port.timeout.waitForSet(
                    probe,
                    attrs=("is_set",),
                )
                if not completed:
                    return False
                if self._channel_state.has_channels():
                    return True
                return not bool(has_channel_request_failed())
            return self._port.timeout.waitForSet(
                self._node,
                attrs=("channels",),
            )

        local_config = self._node.localConfig
        has_field = getattr(local_config, "HasField", None)
        if callable(has_field):
            return self._port.timeout.waitForSet(
                _LocalConfigFieldProbe(
                    has_field_fn=cast(Callable[[str], bool], has_field),
                    name=attribute,
                ),
                attrs=("is_set",),
            )

        return self._port.timeout.waitForSet(
            local_config,
            attrs=(attribute,),
        )

    def _timeout_for_field(self, field_name: str, max_secs: float) -> bool:
        """Wait for a localConfig field with a dedicated timeout.

        Returns True if the field is populated within max_secs, False otherwise.
        If the protobuf doesn't have this field, returns True (skip gracefully
        for backwards compatibility with old firmware).
        """
        local_config = self._node.localConfig
        desc = getattr(local_config, "DESCRIPTOR", None)
        if desc is not None:
            if field_name not in desc.fields_by_name:
                return True

        has_field = getattr(local_config, "HasField", None)
        if callable(has_field):
            probe = _LocalConfigFieldProbe(
                has_field_fn=has_field,
                name=field_name,
            )
            short_timeout = Timeout(maxSecs=max_secs)
            return short_timeout.waitForSet(probe, attrs=("is_set",))

        return False

    def request_channel(self, channel_num: int) -> mesh_pb2.MeshPacket | None:
        """Send one get-channel request preserving progress logging behavior."""
        if not 0 <= channel_num < MAX_CHANNELS:
            logger.warning(
                "Invalid channel_num %d (must be 0-%d), ignoring request.",
                channel_num,
                MAX_CHANNELS - 1,
            )
            return None
        channel_response_runtime = getattr(
            self._node,
            "_channel_response_runtime",
            None,
        )
        mark_channel_request_sent = (
            getattr(channel_response_runtime, "mark_channel_request_sent", None)
            if channel_response_runtime is not None
            else None
        )
        mark_channel_request_send_failed = (
            getattr(
                channel_response_runtime,
                "mark_channel_request_send_failed",
                None,
            )
            if channel_response_runtime is not None
            else None
        )
        if callable(mark_channel_request_sent):
            mark_channel_request_sent(channel_num)

        message = admin_pb2.AdminMessage()
        # Protocol uses 1-indexed channel numbers; API uses 0-indexed
        message.get_channel_request = channel_num + 1

        if self._node != self._node.iface.localNode:
            logger.info(
                "Requesting channel %s info from remote node (this could take a while)",
                channel_num,
            )
        else:
            logger.debug("Requesting channel %s", channel_num)

        try:
            request = self._port.send_admin(
                message,
                want_response=True,
                on_response=self._node.onResponseRequestChannel,
            )
        except Exception:
            if callable(mark_channel_request_send_failed):
                mark_channel_request_send_failed(channel_num)
            raise
        if request is None:
            if callable(mark_channel_request_send_failed):
                mark_channel_request_send_failed(channel_num)
        return request
