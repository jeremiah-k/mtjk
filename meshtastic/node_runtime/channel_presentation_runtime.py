"""User-facing channel/config presentation runtime owner."""

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from google.protobuf.message import Message

from meshtastic.protobuf import channel_pb2
from meshtastic.util import messageToJson, pskToString

from .channel_export_runtime import _NodeChannelExportRuntime
from .channel_state import _NodeChannelState

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)


def _get_role_name(role: int) -> str:
    """Return the role name or UNKNOWN(<value>) for unrecognized roles."""
    if role in channel_pb2.Channel.Role.values():
        return channel_pb2.Channel.Role.Name(role)  # type: ignore[arg-type]
    return f"UNKNOWN({role})"


class _NodeChannelPresentationRuntime:
    """Owns channel/info display formatting and presentation orchestration."""

    def __init__(
        self,
        node: "Node",
        *,
        channel_state: _NodeChannelState,
        export_runtime: _NodeChannelExportRuntime,
        cli_print: Callable[[str], None] = print,
    ) -> None:
        self._node = node
        self._channel_state = channel_state
        self._export_runtime = export_runtime
        self._cli_print = cli_print

    def _show_channels(self, *, cli_print: Callable[[str], None] | None = None) -> None:
        """Print channels and URL exports preserving historical output behavior."""
        cli_print = self._cli_print if cli_print is None else cli_print
        cli_print("Channels:")
        channels_snapshot = self._channel_state.snapshot_channels()
        if channels_snapshot:
            logger.debug(
                "channel snapshot captured (%d entries): %s",
                len(channels_snapshot),
                [
                    {
                        "index": channel.index,
                        "role": _get_role_name(channel.role),
                        "name": channel.settings.name if channel.settings else "",
                    }
                    for channel in channels_snapshot
                ],
            )
            for channel in channels_snapshot:
                if channel.role == channel_pb2.Channel.Role.DISABLED:
                    continue
                role_name = _get_role_name(channel.role)
                channel_string = messageToJson(channel.settings)
                cli_print(
                    f"  Index {channel.index}: {role_name} "
                    f"psk={pskToString(channel.settings.psk)} {channel_string}"
                )
        try:
            public_url = self._resolve_export_url(
                channels_snapshot,
                include_all=False,
            )
        except Exception as exc:  # noqa: BLE001 - show_info should remain non-fatal
            logger.warning("Unable to export primary channel URL: %s", exc)
            cli_print("\nPrimary channel URL: unavailable")
            return

        admin_url = public_url
        try:
            admin_url = self._resolve_export_url(
                channels_snapshot,
                include_all=True,
            )
        except Exception as exc:  # noqa: BLE001 - show_info should remain non-fatal
            logger.warning("Unable to export complete channel URL: %s", exc)

        cli_print(f"\nPrimary channel URL: {public_url}")
        if admin_url != public_url:
            cli_print(f"Complete URL (includes all channels): {admin_url}")

    def _resolve_export_url(
        self,
        channels_snapshot: list[channel_pb2.Channel],
        *,
        include_all: bool,
    ) -> str:
        """Resolve URL export path while preserving compatibility with export mocks."""
        snapshot_export = getattr(
            self._export_runtime,
            "_get_url_from_snapshot",
            None,
        )
        if (
            callable(snapshot_export)
            and getattr(snapshot_export, "__func__", None)
            is _NodeChannelExportRuntime._get_url_from_snapshot  # noqa: SLF001
        ):
            return cast(
                str,
                snapshot_export(
                    channels_snapshot,
                    include_all=include_all,
                ),
            )
        get_url = getattr(self._export_runtime, "get_url", None)
        if callable(get_url):
            return cast(str, get_url(include_all=include_all))
        return self._export_runtime._get_url_from_snapshot(  # noqa: SLF001
            channels_snapshot,
            include_all=include_all,
        )

    def _show_info(self, *, cli_print: Callable[[str], None] | None = None) -> None:
        """Print local/module preferences and current channel presentation."""
        cli_print = self._cli_print if cli_print is None else cli_print
        local_config_snapshot: Message | None = None
        module_config_snapshot: Message | None = None
        node_db_lock = getattr(self._node, "_node_db_lock", None)
        if (
            node_db_lock is not None
            and hasattr(node_db_lock, "__enter__")
            and hasattr(node_db_lock, "__exit__")
        ):
            with node_db_lock:
                (
                    local_config_snapshot,
                    module_config_snapshot,
                ) = self._snapshot_configs()
        else:
            local_config_snapshot, module_config_snapshot = self._snapshot_configs()

        prefs = ""
        if local_config_snapshot:
            prefs = messageToJson(local_config_snapshot, multiline=True)
        cli_print(f"Preferences: {prefs}\n")
        prefs = ""
        if module_config_snapshot:
            prefs = messageToJson(module_config_snapshot, multiline=True)
        cli_print(f"Module preferences: {prefs}\n")
        self._show_channels(cli_print=cli_print)

    def _snapshot_configs(self) -> tuple[Message | None, Message | None]:
        """Return detached snapshots of local/module configs when present."""
        local_config_snapshot: Message | None = None
        module_config_snapshot: Message | None = None
        if self._node.localConfig is not None:
            local_config_snapshot = cast(Message, type(self._node.localConfig)())
            local_config_snapshot.CopyFrom(self._node.localConfig)
        if self._node.moduleConfig is not None:
            module_config_snapshot = cast(Message, type(self._node.moduleConfig)())
            module_config_snapshot.CopyFrom(self._node.moduleConfig)
        return local_config_snapshot, module_config_snapshot
