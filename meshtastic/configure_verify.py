"""Post-configure value-aware verification helpers."""

from __future__ import annotations

import base64
import dataclasses
import enum
import logging
import math
import time
from collections.abc import Callable
from typing import Any, cast

import meshtastic.util
from meshtastic.mesh_interface import MeshInterface
from meshtastic.node_runtime.admin_wait import (
    WAIT_ATTR_NAK,
    _extract_request_id_from_sent_packet,
    _send_admin_with_ack_scope,
)
from meshtastic.node_runtime.settings_runtime.message import (  # pylint: disable=no-name-in-module
    _NodeSettingsMessageBuilder,
)
from meshtastic.protobuf import apponly_pb2, channel_pb2

# Firmware: src/mesh/Default.h default_neighbor_info_broadcast_secs.
_NEIGHBOR_INFO_EFFECTIVE_DEFAULT_SECS = 21600


def _neighbor_info_effective_default_equivalence(
    section_path: str, field_name: str, requested: Any, actual: Any
) -> bool:
    """Treat NeighborInfo ``update_interval`` 0 as the firmware effective default.

    The firmware resolves a zero ``neighbor_info.update_interval`` to
    ``default_neighbor_info_broadcast_secs``, so a profile storing 0 and a
    device reporting that default hold the same effective setting, and vice
    versa. Schema drift that removes or renames the leaf is reported as an
    ordinary mismatch before this predicate is consulted.

    Parameters
    ----------
    section_path : str
        Configure-document section path containing the compared field.
    field_name : str
        Normalized protobuf field name being compared.
    requested : Any
        Value requested by the configure document.
    actual : Any
        Value read back from the device.

    Returns
    -------
    bool
        ``True`` only for the NeighborInfo effective-default equivalence.
    """
    if (
        field_name != "update_interval"
        # Section paths keep the document's original casing (snake or camel).
        or meshtastic.util.camel_to_snake(section_path) != "neighbor_info"
    ):
        return False
    requested_is_int = isinstance(requested, int) and not isinstance(requested, bool)
    actual_is_int = isinstance(actual, int) and not isinstance(actual, bool)
    return (
        requested_is_int
        and actual_is_int
        and requested in (0, _NEIGHBOR_INFO_EFFECTIVE_DEFAULT_SECS)
        and actual in (0, _NEIGHBOR_INFO_EFFECTIVE_DEFAULT_SECS)
    )


logger = logging.getLogger(__name__)


class ConfigureReconnectResult(enum.Enum):
    """Outcome of local reconnect/config reload verification after configure."""

    RECONNECT_FAILED = "reconnect_failed"
    CONFIG_RELOAD_FAILED = "config_reload_failed"
    VERIFICATION_INCOMPLETE = "verification_incomplete"
    VERIFIED = "verified"


class LocalApplyStatus(enum.Enum):
    """Outcome of fresh value-aware verification of a local config apply."""

    VERIFIED = "verified"
    MISMATCH = "mismatch"
    RELOAD_FAILED = "reload_failed"


@dataclasses.dataclass(frozen=True)
class LocalApplyVerification:
    """Result of one :func:`verify_local_config_apply` operation.

    Attributes
    ----------
    status : LocalApplyStatus
        Overall outcome of the operation.
    mismatched_fields : tuple[str, ...]
        Dotted paths of requested fields whose fresh device value differs
        from the requested value (field names in snake_case, section prefix
        as requested). Populated only for ``MISMATCH``.
    missing_sections : tuple[str, ...]
        Requested section names that were not freshly repopulated within
        the operation budget. Populated only for ``RELOAD_FAILED``.
    """

    status: LocalApplyStatus
    mismatched_fields: tuple[str, ...] = ()
    missing_sections: tuple[str, ...] = ()


def _is_repeated_field(field_desc: Any) -> bool:
    is_repeated = getattr(field_desc, "is_repeated", None)
    if isinstance(is_repeated, bool):
        return is_repeated
    label = getattr(field_desc, "label", None)
    label_repeated = getattr(field_desc, "LABEL_REPEATED", None)
    return label is not None and label == label_repeated


def _coerce_element(field_desc: Any, element: Any) -> Any:
    # TYPE_STRING: preserve as-is (do not apply fromStr)
    if _is_string_field(field_desc):
        return element
    # Enum fields: convert enum name to enum number
    if field_desc.enum_type is not None and isinstance(element, str):
        enum_val = field_desc.enum_type.values_by_name.get(element)
        if enum_val is not None:
            return enum_val.number
        logger.debug(
            "Unknown enum name %r for repeated element in field; treating as mismatch.",
            element,
        )
        return element
    # Non-string, non-enum fields: apply fromStr for coercion
    # Note: TYPE_STRING fields are already handled above and returned unchanged
    if isinstance(element, str) and field_desc.enum_type is None:
        return meshtastic.util.fromStr(element)
    return element


def _is_string_field(field_desc: Any) -> bool:
    field_type = getattr(field_desc, "type", None)
    type_string = getattr(field_desc, "TYPE_STRING", None)
    return (
        field_type is not None and type_string is not None and field_type == type_string
    )


def _verify_requested_fields(
    yaml_dict: dict[str, Any],
    proto_message: Any,
    section_path: str,
) -> list[str]:
    """Compare YAML-requested field values against a protobuf message.

    Recursively walks *yaml_dict*, converting each camelCase key to
    snake_case and looking up the corresponding field on *proto_message*.
    For leaf values, enum strings are resolved to their numeric form and
    non-enum strings are coerced via ``meshtastic.util.fromStr()``.
    Repeated fields are compared as lists; non-repeated fields that
    receive a list use only the first element.

    Parameters
    ----------
    yaml_dict : dict[str, Any]
        Mapping of camelCase field names to the YAML-requested values.
    proto_message : Any
        Protocol buffer message whose current field values are the
        source of truth for comparison.
    section_path : str
        Dot-separated path prefix used in mismatch reports (e.g.
        ``"Config.lora"``).

    Returns
    -------
    list[str]
        Dot-separated paths of fields whose requested value does not
        match the protobuf value.  Empty list means all fields match.
    """
    mismatches: list[str] = []
    for key, yaml_value in yaml_dict.items():
        snake_key = meshtastic.util.camel_to_snake(key)
        field_desc = proto_message.DESCRIPTOR.fields_by_name.get(snake_key)
        if field_desc is None:
            mismatches.append(f"{section_path}.{key}")
            continue
        if isinstance(yaml_value, dict):
            sub_msg = getattr(proto_message, snake_key)
            if not hasattr(sub_msg, "DESCRIPTOR"):
                mismatches.append(
                    f"{section_path}.{snake_key}: expected scalar but got mapping"
                )
                continue
            mismatches.extend(
                _verify_requested_fields(
                    yaml_value, sub_msg, f"{section_path}.{snake_key}"
                )
            )
        else:
            actual = getattr(proto_message, snake_key)
            if _is_repeated_field(field_desc):
                yaml_list = (
                    yaml_value
                    if isinstance(yaml_value, (list, tuple))
                    else [yaml_value]
                )
                coerced = [_coerce_element(field_desc, el) for el in yaml_list]
                if list(coerced) != list(actual):
                    mismatches.append(f"{section_path}.{snake_key}")
            else:
                scalar: Any = yaml_value
                if field_desc.enum_type is not None and isinstance(yaml_value, str):
                    enum_val = field_desc.enum_type.values_by_name.get(yaml_value)
                    if enum_val is not None:
                        scalar = enum_val.number
                    else:
                        logger.debug(
                            "Unknown enum name %r for field %s.%s; treating as mismatch.",
                            yaml_value,
                            section_path,
                            snake_key,
                        )
                        scalar = yaml_value
                if (
                    isinstance(yaml_value, str)
                    and field_desc.enum_type is None
                    and not _is_string_field(field_desc)
                ):
                    scalar = meshtastic.util.fromStr(yaml_value)
                if isinstance(scalar, (list, tuple)):
                    logger.debug(
                        "YAML provided a list for non-repeated field %s.%s; using first element.",
                        section_path,
                        snake_key,
                    )
                    scalar = scalar[0] if scalar else scalar
                if (
                    scalar != actual
                    and not _neighbor_info_effective_default_equivalence(
                        section_path, snake_key, scalar, actual
                    )
                ):
                    mismatches.append(f"{section_path}.{snake_key}")
    return mismatches


def _verify_channel_url_match(
    requested_url: str,
    device_url: str,
) -> bool:
    req_cs = _parse_channel_set(requested_url)
    dev_cs = _parse_channel_set(device_url)
    if req_cs is None or dev_cs is None:
        return False
    return _verify_channel_sets_match(req_cs, dev_cs, emit_warnings=True)


def _verify_channel_url_against_state(
    requested_url: str,
    *,
    device_channels: list[Any] | None,
    device_lora_config: Any | None,
    emit_warnings: bool = True,
) -> bool:
    """Verify requested channel URL against already-loaded device channel/LoRa state."""
    requested_channel_set = _parse_channel_set(requested_url)
    if requested_channel_set is None:
        if emit_warnings:
            logger.warning(
                "Channel URL verification: requested URL could not be parsed."
            )
        return False
    device_channel_set = _build_channel_set_from_state(
        device_channels=device_channels,
        device_lora_config=device_lora_config,
        emit_warnings=emit_warnings,
    )
    if device_channel_set is None:
        return False
    return _verify_channel_sets_match(
        requested_channel_set,
        device_channel_set,
        emit_warnings=emit_warnings,
    )


def _parse_channel_set(url: str) -> apponly_pb2.ChannelSet | None:
    try:
        b64 = url.split("#")[-1]
        b64 += "=" * ((4 - len(b64) % 4) % 4)
        raw = base64.b64decode(b64, altchars=b"-_")
        channel_set = apponly_pb2.ChannelSet()
        channel_set.ParseFromString(raw)
        return channel_set
    except Exception:
        return None


def _build_channel_set_from_state(
    *,
    device_channels: list[Any] | None,
    device_lora_config: Any | None,
    emit_warnings: bool,
) -> apponly_pb2.ChannelSet | None:
    if not device_channels:
        if emit_warnings:
            logger.warning("Channel URL verification: device channels are not loaded.")
        return None

    primary_channel = next(
        (
            channel
            for channel in device_channels
            if channel.role == channel_pb2.Channel.Role.PRIMARY
        ),
        None,
    )
    if primary_channel is None:
        if emit_warnings:
            logger.warning(
                "Channel URL verification: no primary channel in device state."
            )
        return None

    channel_set = apponly_pb2.ChannelSet()
    channel_set.settings.append(primary_channel.settings)
    for channel in device_channels:
        if channel.role == channel_pb2.Channel.Role.SECONDARY:
            channel_set.settings.append(channel.settings)

    if device_lora_config is None:
        if emit_warnings:
            logger.warning(
                "Channel URL verification: device LoRa config is not loaded."
            )
        return None
    try:
        channel_set.lora_config.CopyFrom(device_lora_config)
    except Exception:
        if emit_warnings:
            logger.warning(
                "Channel URL verification: failed to copy device LoRa config.",
                exc_info=True,
            )
        return None
    return channel_set


def _settings_match(req: Any, dev: Any) -> bool:
    checks = [
        req.psk == dev.psk,
        req.name == dev.name,
        req.id == dev.id,
        req.uplink_enabled == dev.uplink_enabled,
        req.downlink_enabled == dev.downlink_enabled,
        req.module_settings.position_precision
        == dev.module_settings.position_precision,
        req.module_settings.is_muted == dev.module_settings.is_muted,
    ]
    return all(checks)


def _has_duplicate_names(
    names: list[str],
    *,
    source_label: str,
    emit_warnings: bool,
) -> bool:
    if len(names) == len(set(names)):
        return False
    if emit_warnings:
        logger.warning(
            "Channel URL verification: duplicate channel names in %s URL "
            "(%s); cannot verify unambiguously.",
            source_label,
            ", ".join(names),
        )
    return True


def _lora_config_match(
    req: apponly_pb2.ChannelSet,
    dev: apponly_pb2.ChannelSet,
    *,
    emit_warnings: bool,
) -> bool:
    req_has_lora = req.HasField("lora_config")
    dev_has_lora = dev.HasField("lora_config")
    if req_has_lora != dev_has_lora:
        if emit_warnings:
            logger.warning(
                "Channel URL verification: lora_config presence mismatch "
                "(requested=%s, device=%s).",
                req_has_lora,
                dev_has_lora,
            )
        return False
    if req_has_lora and (
        req.lora_config.SerializeToString() != dev.lora_config.SerializeToString()
    ):
        if emit_warnings:
            logger.warning(
                "Channel URL verification: lora_config differs between requested and device URLs."
            )
        return False
    return True


def _verify_channel_sets_match(
    requested_channel_set: apponly_pb2.ChannelSet,
    device_channel_set: apponly_pb2.ChannelSet,
    *,
    emit_warnings: bool,
) -> bool:
    # Both sets must have a primary channel entry
    if not requested_channel_set.settings or not device_channel_set.settings:
        if emit_warnings:
            logger.warning(
                "Channel URL verification: missing primary channel entry "
                "(requested=%d, device=%d).",
                len(requested_channel_set.settings),
                len(device_channel_set.settings),
            )
        return False

    # Primary channel must match exactly (fail early if primary differs)
    requested_primary = requested_channel_set.settings[0]
    device_primary = device_channel_set.settings[0]
    if not _settings_match(requested_primary, device_primary):
        if emit_warnings:
            logger.warning(
                "Channel URL verification: primary channel entry differs "
                "(requested primary=%r, device primary=%r).",
                requested_primary.name,
                device_primary.name,
            )
        return False

    requested_names = [settings.name for settings in requested_channel_set.settings]
    device_names = [settings.name for settings in device_channel_set.settings]
    if _has_duplicate_names(
        requested_names,
        source_label="requested",
        emit_warnings=emit_warnings,
    ) or _has_duplicate_names(
        device_names,
        source_label="device",
        emit_warnings=emit_warnings,
    ):
        return False

    requested_lookup: dict[str, Any] = {
        settings.name: settings for settings in requested_channel_set.settings
    }
    device_lookup: dict[str, Any] = {
        settings.name: settings for settings in device_channel_set.settings
    }
    requested_name_set = set(requested_lookup.keys())
    device_name_set = set(device_lookup.keys())
    if requested_name_set != device_name_set:
        missing_on_device = requested_name_set - device_name_set
        extra_on_device = device_name_set - requested_name_set
        parts: list[str] = []
        if missing_on_device:
            parts.append(f"missing on device: {sorted(missing_on_device)}")
        if extra_on_device:
            parts.append(f"extra on device: {sorted(extra_on_device)}")
        if emit_warnings:
            logger.warning(
                "Channel URL verification: channel name sets do not match (%s).",
                "; ".join(parts),
            )
        return False

    if not _lora_config_match(
        requested_channel_set,
        device_channel_set,
        emit_warnings=emit_warnings,
    ):
        return False

    return all(
        _settings_match(requested_settings, device_lookup[name])
        for name, requested_settings in requested_lookup.items()
    )


class _SectionReloadProbe:
    """Probe reporting whether one cleared config section was repopulated."""

    def __init__(self, has_field_fn: Callable[[str], bool], name: str) -> None:
        self._has_field_fn = has_field_fn
        self._name = name

    def is_set(self) -> bool:
        """Return ``True`` once the probed section is present again."""
        return bool(self._has_field_fn(self._name))


def _wait_for_section_reload(
    target_node: Any, proto_config: Any, section_snake: str
) -> bool:
    """Wait for one re-requested config section response to arrive.

    Parameters
    ----------
    target_node : Any
        Node whose bounded wait timeout scopes the section wait.
    proto_config : Any
        The protobuf config root (local or module) holding the section.
    section_snake : str
        Snake-case name of the cleared section being re-requested.

    Returns
    -------
    bool
        ``True`` when the section is present again before the node's wait
        timeout expires, ``False`` otherwise.
    """
    has_field = getattr(proto_config, "HasField", None)
    if not callable(has_field):
        return True
    timeout = getattr(target_node, "_timeout", None)
    if timeout is None:
        # Older node doubles without a wait owner cannot block here; the
        # subsequent section verification reports any missing state.
        return True
    probe = _SectionReloadProbe(cast(Callable[[str], bool], has_field), section_snake)
    return bool(timeout.waitForSet(probe, attrs=("is_set",)))


def _verification_poll_interval(target_node: Any) -> float:
    """Return the polling interval, preferring the node timeout owner's pacing.

    Parameters
    ----------
    target_node : Any
        Node whose timeout owner may define a sleep interval.

    Returns
    -------
    float
        Positive polling interval in seconds.
    """
    node_timeout = getattr(target_node, "_timeout", None)
    sleep_interval = getattr(node_timeout, "sleepInterval", None)
    if isinstance(sleep_interval, (int, float)) and not isinstance(
        sleep_interval, bool
    ):
        return max(0.01, float(sleep_interval))
    return 0.1


def _wait_for_section_reload_under_deadline(
    target_node: Any, proto_config: Any, section_snake: str, *, deadline: float
) -> bool:
    """Wait for one section to reappear, bounded by a shared monotonic deadline.

    Unlike :func:`_wait_for_section_reload`, which restarts the node's
    wall-clock ``expireTimeout`` on every call, this wait consumes only the
    budget remaining until *deadline*, so N sections can never multiply a
    caller's operation timeout.

    Parameters
    ----------
    target_node : Any
        Node whose timeout owner supplies the polling interval.
    proto_config : Any
        The protobuf config root (local or module) holding the section.
    section_snake : str
        Snake-case name of the cleared section being re-requested.
    deadline : float
        Operation-wide ``time.monotonic()`` deadline.

    Returns
    -------
    bool
        ``True`` when the section is present again before the deadline,
        ``False`` otherwise.
    """
    has_field = getattr(proto_config, "HasField", None)
    if not callable(has_field):
        return True
    probe = _SectionReloadProbe(cast(Callable[[str], bool], has_field), section_snake)
    poll_interval = _verification_poll_interval(target_node)
    while time.monotonic() < deadline:
        if probe.is_set():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll_interval, remaining))
    return probe.is_set()


def _reload_requested_sections(
    target_node: Any,
    *,
    config_fields: dict[str, dict[str, Any]] | None,
    module_config_fields: dict[str, dict[str, Any]] | None,
    send_request: Callable[[Any, Any], tuple[bool, int | None]],
    wait_for_section: Callable[[Any, Any, str], bool],
    sent_request_ids: list[int],
) -> list[str]:
    """Clear and re-request each touched section; return names not repopulated.

    For every requested section the cached section is cleared, one fresh
    readback request is sent through ``send_request``, and
    ``wait_for_section`` decides whether the section was repopulated.
    Sections without a matching field descriptor are warned about and
    reported without clearing anything.

    Parameters
    ----------
    target_node : Any
        Node whose cached local/module configuration is refreshed.
    config_fields : dict[str, dict[str, Any]] | None
        Requested local-config section/value mappings.
    module_config_fields : dict[str, dict[str, Any]] | None
        Requested module-config section/value mappings.
    send_request : Callable[[Any, Any], tuple[bool, int | None]]
        Issues one readback request for ``(target_node, field_desc)``,
        returning whether a waitable request was issued and the sent
        request id (``None`` when the path exposes no id).
    wait_for_section : Callable[[Any, Any, str], bool]
        Per-section reload probe receiving ``(target_node, proto_config,
        section_snake)``.
    sent_request_ids : list[int]
        Output list collecting every sent request id for caller-side
        retirement.

    Returns
    -------
    list[str]
        Requested section names that were skipped or not repopulated, in
        request order.
    """
    not_repopulated: list[str] = []
    for (
        fields,
        proto_root,
        warning_label,
        skip_label,
    ) in (
        (config_fields, getattr(target_node, "localConfig", None), "Config", "config"),
        (
            module_config_fields,
            getattr(target_node, "moduleConfig", None),
            "Module config",
            "module_config",
        ),
    ):
        if not fields:
            continue
        proto_config: Any = proto_root
        fields_by_name = (
            getattr(getattr(proto_config, "DESCRIPTOR", None), "fields_by_name", None)
            or {}
        )
        for section_name in fields:
            section_snake = meshtastic.util.camel_to_snake(section_name)
            field_desc = fields_by_name.get(section_snake)
            if field_desc is None:
                logger.warning(
                    "Skipping %s refresh for unknown section %r.",
                    skip_label,
                    section_name,
                )
                not_repopulated.append(section_name)
                continue
            proto_config.ClearField(section_snake)
            waitable, request_id = send_request(target_node, field_desc)
            if request_id is not None:
                sent_request_ids.append(request_id)
            if waitable and not wait_for_section(
                target_node, proto_config, section_snake
            ):
                logger.warning(
                    "%s section %r was not repopulated before verification.",
                    warning_label,
                    section_name,
                )
                not_repopulated.append(section_name)
    return not_repopulated


def _send_refresh_request_via_request_config(
    target_node: Any, field_desc: Any
) -> tuple[bool, int | None]:
    """Send one section refresh through the public ``requestConfig`` path.

    Used by the historical refresh wrapper; the node's own wait owner bounds
    any scoped acknowledgment wait on this path and owns the request
    lifecycle, so no id is reported for retirement.

    Parameters
    ----------
    target_node : Any
        Node whose configuration is re-requested.
    field_desc : Any
        Field descriptor of the section to re-request.

    Returns
    -------
    tuple[bool, int | None]
        Whether a request was issued (``False`` when the node has no
        callable ``requestConfig``) and no id.
    """
    request_config = getattr(target_node, "requestConfig", None)
    if callable(request_config):
        request_config(field_desc)
        return True, None
    return False, None


def _send_section_refresh_request(
    target_node: Any, field_desc: Any
) -> tuple[bool, int | None]:
    """Send one bounded section readback without enrolling a scoped wait.

    Mirrors the historical local settings-request send (response handler
    registered with the typed admin response contract; the payload applies
    when the correlated data reply arrives) while deliberately passing
    ``scope_ack=False`` so NO request-scoped acknowledgment wait is opened:
    the caller's deadline loop is the only wait, and a device that never
    answers cannot block the operation on the node's own timeout.

    Falls back to the public ``requestConfig`` when the private send seam is
    unavailable on a node double; the operation bound then depends on the
    node's wait owner.

    Parameters
    ----------
    target_node : Any
        Node whose configuration is re-requested.
    field_desc : Any
        Field descriptor of the section to re-request.

    Returns
    -------
    tuple[bool, int | None]
        Whether a request was issued and the sent request id for caller
        retirement (``None`` on the fallback path).
    """
    on_response = getattr(target_node, "onResponseRequestSettings", None)
    if callable(on_response) and callable(getattr(target_node, "_send_admin", None)):
        message = _NodeSettingsMessageBuilder(target_node).build_request_message(
            field_desc
        )
        request = _send_admin_with_ack_scope(
            target_node,
            message,
            scope_ack=False,
            wantResponse=True,
            onResponse=on_response,
        )
        return True, _extract_request_id_from_sent_packet(target_node, request)
    return _send_refresh_request_via_request_config(target_node, field_desc)


def _refresh_no_disconnect_verify_state(
    target_node: Any,
    *,
    verify_channel_url: str | None,
    verify_config_fields: dict[str, dict[str, Any]] | None,
    verify_module_config_fields: dict[str, dict[str, Any]] | None,
) -> None:
    """Invalidate touched cached state before post-reconnect verification."""
    _reload_requested_sections(
        target_node,
        config_fields=verify_config_fields,
        module_config_fields=verify_module_config_fields,
        send_request=_send_refresh_request_via_request_config,
        wait_for_section=_wait_for_section_reload,
        sent_request_ids=[],
    )

    if verify_channel_url:
        invalidate_channel_cache = getattr(
            target_node, "_invalidate_channel_cache", None
        )
        if callable(invalidate_channel_cache):
            invalidate_channel_cache()  # noqa: SLF001 - Node cache owner API
        request_channels = getattr(target_node, "requestChannels", None)
        if callable(request_channels):
            request_channels(0)


def _device_lora_config(target_node: Any) -> Any | None:
    """Return the loaded device LoRa config, or ``None`` when unavailable.

    Parameters
    ----------
    target_node : Any
        Node whose loaded local configuration is inspected.

    Returns
    -------
    Any | None
        Loaded LoRa protobuf message when present, otherwise ``None``.
    """
    local_config = getattr(target_node, "localConfig", None)
    has_field = getattr(local_config, "HasField", None)
    if local_config is None or not callable(has_field) or not has_field("lora"):
        return None
    return local_config.lora


def _channel_url_matches_current_device_state(
    target_node: Any,
    requested_channel_url: str,
    *,
    verify_channel_url_against_state: Callable[..., bool] = (
        _verify_channel_url_against_state
    ),
) -> bool:
    """Return True when requested channel URL already matches loaded device state."""
    device_lora_config = _device_lora_config(target_node)
    if device_lora_config is None:
        return False
    return verify_channel_url_against_state(
        requested_channel_url,
        device_channels=getattr(target_node, "channels", None),
        device_lora_config=device_lora_config,
        emit_warnings=False,
    )


def _flatten_leaf_paths(prefix: str, mapping: dict[str, Any]) -> list[str]:
    """Recursively flatten a nested mapping into dotted leaf paths."""
    paths: list[str] = []
    for key, value in mapping.items():
        dotted = f"{prefix}.{key}"
        if isinstance(value, dict) and value:
            paths.extend(_flatten_leaf_paths(dotted, value))
        else:
            paths.append(dotted)
    return paths


def _verify_config_sections(
    config_fields: dict[str, dict[str, Any]],
    proto_config: Any,
    label: str,
    verified_fields: list[str] | None = None,
) -> bool:
    """Verify requested configuration sections against a reloaded protobuf.

    Parameters
    ----------
    config_fields : dict[str, dict[str, Any]]
        Requested section/value mappings from the configure document.
    proto_config : Any
        Reloaded protobuf configuration root.
    label : str
        Human-readable label used in diagnostics.
    verified_fields : list[str] | None
        Optional list mutated in place with verified dotted leaf paths.

    Returns
    -------
    bool
        ``True`` only when every requested section and field matches.
    """
    for section_name, yaml_values in config_fields.items():
        section_snake = meshtastic.util.camel_to_snake(section_name)
        if not proto_config.HasField(section_snake):
            logger.warning(
                "%s section %r not present after reload.",
                label,
                section_name,
            )
            return False
        proto_section = getattr(proto_config, section_snake)
        mismatches = _verify_requested_fields(yaml_values, proto_section, section_name)
        if mismatches:
            logger.warning(
                "%s section %r field mismatches: %s",
                label,
                section_name,
                ", ".join(mismatches),
            )
            return False
        if verified_fields is not None:
            verified_fields.extend(_flatten_leaf_paths(section_snake, yaml_values))
        logger.debug(
            "%s section %r verified (all requested field values match).",
            label,
            section_name,
        )
    return True


def _verify_post_reconnect_config(
    interface: MeshInterface,
    node_dest: str,
    *,
    verify_channel_url: str | None = None,
    verify_config_fields: dict[str, dict[str, Any]] | None = None,
    verify_module_config_fields: dict[str, dict[str, Any]] | None = None,
    verify_channel_url_against_state: Callable[..., bool] = (
        _verify_channel_url_against_state
    ),
) -> ConfigureReconnectResult:
    """Verify requested values after reconnect/config reload.

    Parameters
    ----------
    interface : MeshInterface
        Reconnected interface containing refreshed device state.
    node_dest : str
        Destination whose configuration is verified.
    verify_channel_url : str | None
        Normalized channel URL expected after reload.
    verify_config_fields : dict[str, dict[str, Any]] | None
        Requested local-config sections/fields to compare.
    verify_module_config_fields : dict[str, dict[str, Any]] | None
        Requested module-config sections/fields to compare.
    verify_channel_url_against_state : Callable[..., bool]
        Channel-state comparison seam.

    Returns
    -------
    ConfigureReconnectResult
        ``VERIFIED`` on a complete match, otherwise ``VERIFICATION_INCOMPLETE``.
    """
    if not interface.isConnected.is_set():
        logger.warning("Post-reconnect verification skipped: transport disconnected.")
        return ConfigureReconnectResult.VERIFICATION_INCOMPLETE

    target_node = interface.getNode(node_dest)
    verified_fields: list[str] = []

    if verify_channel_url:
        device_lora_config = _device_lora_config(target_node)
        if not verify_channel_url_against_state(
            verify_channel_url,
            device_channels=getattr(target_node, "channels", None),
            device_lora_config=device_lora_config,
        ):
            logger.warning(
                "Channel URL verification: device state does not match requested URL."
            )
            return ConfigureReconnectResult.VERIFICATION_INCOMPLETE
        verified_fields.append("channel_url")

    if verify_config_fields and not _verify_config_sections(
        verify_config_fields,
        target_node.localConfig,
        "Config",
        verified_fields=verified_fields,
    ):
        return ConfigureReconnectResult.VERIFICATION_INCOMPLETE

    if verify_module_config_fields and not _verify_config_sections(
        verify_module_config_fields,
        target_node.moduleConfig,
        "Module config",
        verified_fields=verified_fields,
    ):
        return ConfigureReconnectResult.VERIFICATION_INCOMPLETE

    if not interface.isConnected.is_set():
        logger.warning(
            "Post-reconnect verification did not complete: transport disconnected."
        )
        return ConfigureReconnectResult.VERIFICATION_INCOMPLETE

    if verified_fields:
        logger.info("Verified: %s", ", ".join(verified_fields))

    return ConfigureReconnectResult.VERIFIED


def _collect_section_mismatches(
    config_fields: dict[str, dict[str, Any]],
    proto_config: Any,
    *,
    absent_sections: list[str],
) -> list[str]:
    """Collect dotted mismatch paths for freshly reloaded sections.

    Uses the same per-section presence check and field comparator as
    :func:`_verify_config_sections`, but keeps every mismatched path instead
    of stopping at the first failing section. A requested section that is
    not present at comparison time is reported through *absent_sections*
    (missing-section vocabulary) instead of as a field mismatch.

    Parameters
    ----------
    config_fields : dict[str, dict[str, Any]]
        Requested section/value mappings.
    proto_config : Any
        Reloaded protobuf configuration root.
    absent_sections : list[str]
        Output list collecting requested section names that lost presence.

    Returns
    -------
    list[str]
        Dotted paths of requested fields whose fresh value differs.
    """
    mismatches: list[str] = []
    for section_name, requested_values in config_fields.items():
        section_snake = meshtastic.util.camel_to_snake(section_name)
        if not proto_config.HasField(section_snake):
            absent_sections.append(section_name)
            continue
        mismatches.extend(
            _verify_requested_fields(
                requested_values, getattr(proto_config, section_snake), section_name
            )
        )
    return mismatches


def verify_local_config_apply(
    target_node: Any,
    *,
    config_fields: dict[str, dict[str, Any]] | None,
    module_config_fields: dict[str, dict[str, Any]] | None,
    timeout_sec: float,
) -> LocalApplyVerification:
    """Verify requested values were freshly applied to a local node.

    For every requested section the node's cached section is cleared, a
    fresh ``requestConfig`` is sent, and the operation waits for the device
    to repopulate the section (``HasField``) before comparing requested
    values against the fresh device state. Cached or staged values alone are
    never accepted as evidence: a section that stays cleared (for example
    because firmware silently dropped the preceding write) yields
    ``RELOAD_FAILED`` even when the cleared cache previously held the
    requested values.

    Comparison reuses the existing field comparators (camel/snake
    normalization, enum-name coercion, repeated lists, NeighborInfo
    effective-default equivalence). Only requested fields are compared;
    untouched fields may differ freely. A real section whose values are all
    defaults (0/false/empty repeated) verifies when the device truly
    repopulates it, because presence is a ``HasField`` observation, not a
    nonzero-value observation.

    Timing: *all* sections and waits share ONE ``time.monotonic()`` budget
    of ``timeout_sec``; each per-section wait consumes only the remaining
    budget, so several never-repopulating sections cannot multiply the
    caller's timeout. Readback requests are issued without enrolling any
    request-scoped acknowledgment wait, so a device that never answers
    cannot block the operation on the node's own wait timeout. The one
    exception is the ``requestConfig`` fallback on nodes without the
    private send seams: there the node's wait owner bounds the wait, not
    this budget.

    Correlation strength and limits: on the real transport the section
    response is applied by the response handler registered for the sent
    request id, further gated by the admin response contract (response
    variant and source must match, including the local-only source-0
    allowance), so presence is normally satisfied only by a reply belonging
    to this operation's own request. A stale or duplicate reply that still
    matches the contract can in principle satisfy the presence probe; if it
    carries different values the operation reports ``MISMATCH`` rather than
    a false success, and a stale-but-identical reply is value-equivalent
    evidence of the same device state. Wrong-source or wrong-variant replies
    never repopulate the probed section through the registered handler, so
    they cannot produce ``VERIFIED`` beyond what presence plus value
    comparison already proves.

    Cleanup: the operation registers no callbacks, markers, or wait state of
    its own and never holds locks across sends or callbacks. Every readback
    request id it sends is retired in an operation-level ``finally`` through
    the interface's request-wait runtime (the same retirement the bounded
    admin getters use), so no managed response handler or wait bookkeeping
    outlives the operation on success, mismatch, timeout, or send failure.

    Failure behavior: send failures (including transport errors raised by
    the private readback send) PROPAGATE as exceptions (typically
    ``MeshInterface.MeshInterfaceError``) so callers can distinguish "could
    not ask the device" from "device did not answer"; mapping them to CLI
    errors is the caller's responsibility.

    Cache side effect: requested sections are cleared and reloaded from
    fresh device replies. If a section fails to repopulate (or the operation
    raises mid-way), the affected cached section may remain cleared: the
    cache then honestly reflects "device state unknown" instead of stale
    values.

    Parameters
    ----------
    target_node : Any
        Local node whose ``localConfig``/``moduleConfig`` are verified.
        Readback requests are sent without enrolling any scoped
        acknowledgment wait when the node offers the private send seam
        (``onResponseRequestSettings`` plus ``_send_admin``). On nodes
        lacking those seams the operation falls back to the public
        ``requestConfig``; that blocking wait is NOT bounded by
        ``timeout_sec`` — the node's own wait owner applies.
    config_fields : dict[str, dict[str, Any]] | None
        Requested local-config sections mapping section name to
        ``{field name: frozen expected value}``. Keys may be camelCase or
        snake_case; values must already be normalized (ints, bools, bytes,
        lists for repeated fields, nested dicts for submessages).
    module_config_fields : dict[str, dict[str, Any]] | None
        Same mapping for module-config sections.
    timeout_sec : float
        Positive, finite shared budget in seconds for the whole operation.

    Returns
    -------
    LocalApplyVerification
        ``VERIFIED`` when every requested section freshly repopulated and
        all requested values match (vacuously true when nothing is
        requested); ``MISMATCH`` with ``mismatched_fields`` when fresh state
        is present but at least one requested value differs; ``RELOAD_FAILED``
        with ``missing_sections`` when any requested section could not be
        freshly repopulated within the budget (or was no longer present at
        comparison time; this status takes precedence;
        ``mismatched_fields`` is then empty).

    Raises
    ------
    TypeError
        If ``timeout_sec`` is not a real number.
    ValueError
        If ``timeout_sec`` is not positive and finite.
    """
    if isinstance(timeout_sec, bool) or not isinstance(timeout_sec, (int, float)):
        raise TypeError("timeout_sec must be a positive number of seconds")
    budget = float(timeout_sec)
    if not math.isfinite(budget) or budget <= 0:
        raise ValueError("timeout_sec must be a positive, finite number of seconds")

    requested_sections: tuple[str, ...] = (
        *(config_fields or {}),
        *(module_config_fields or {}),
    )
    if not requested_sections:
        return LocalApplyVerification(LocalApplyStatus.VERIFIED, (), ())
    if not (
        callable(getattr(target_node, "onResponseRequestSettings", None))
        or callable(getattr(target_node, "requestConfig", None))
    ):
        # Without any way to re-request sections nothing can be freshly
        # reloaded; report every requested section as missing without
        # mutating any cached state.
        return LocalApplyVerification(
            LocalApplyStatus.RELOAD_FAILED, (), requested_sections
        )

    deadline = time.monotonic() + budget

    def _wait_bounded(node: Any, proto_config: Any, section_snake: str) -> bool:
        return _wait_for_section_reload_under_deadline(
            node, proto_config, section_snake, deadline=deadline
        )

    sent_request_ids: list[int] = []
    runtime = getattr(
        getattr(target_node, "iface", None), "_request_wait_runtime", None
    )
    try:
        not_repopulated = _reload_requested_sections(
            target_node,
            config_fields=config_fields,
            module_config_fields=module_config_fields,
            send_request=_send_section_refresh_request,
            wait_for_section=_wait_bounded,
            sent_request_ids=sent_request_ids,
        )
        if not_repopulated:
            return LocalApplyVerification(
                LocalApplyStatus.RELOAD_FAILED, (), tuple(not_repopulated)
            )

        mismatches: list[str] = []
        absent_sections: list[str] = []
        if config_fields:
            mismatches.extend(
                _collect_section_mismatches(
                    config_fields,
                    target_node.localConfig,
                    absent_sections=absent_sections,
                )
            )
        if module_config_fields:
            mismatches.extend(
                _collect_section_mismatches(
                    module_config_fields,
                    target_node.moduleConfig,
                    absent_sections=absent_sections,
                )
            )
        if absent_sections:
            return LocalApplyVerification(
                LocalApplyStatus.RELOAD_FAILED,
                (),
                tuple(dict.fromkeys(absent_sections)),
            )
        if mismatches:
            return LocalApplyVerification(
                LocalApplyStatus.MISMATCH, tuple(dict.fromkeys(mismatches)), ()
            )
        return LocalApplyVerification(LocalApplyStatus.VERIFIED, (), ())
    finally:
        # Retire every readback request id this operation sent so no managed
        # response handler or wait bookkeeping outlives the operation on any
        # exit path (the same retirement the bounded admin getters use).
        if runtime is not None:
            for request_id in sent_request_ids:
                runtime.retire_wait_request(WAIT_ATTR_NAK, request_id=request_id)
