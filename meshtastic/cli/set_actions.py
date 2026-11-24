"""CLI ``--set`` apply and dry-run preview pipeline.

The connected dispatch binds :func:`_handle_set_command` and
:func:`_preview_set_command` through ``ConfigureActionHooks``. Process exit and
user-facing reporting are entrypoint-owned effects and arrive as the
``cli_exit`` and ``cli_print`` parameters; everything else (validation,
section loading, protobuf copies, verification) is owned here.
"""

from __future__ import annotations

import binascii
import time
from collections.abc import Callable, Sequence
from typing import Any

from google.protobuf.descriptor import FieldDescriptor

from meshtastic._command_scope import _get_command_scope
from meshtastic._deadline import _remaining_timeout
from meshtastic.cli import config_io
from meshtastic.cli import configure_actions as cli_configure_actions
from meshtastic.cli.config_preview import (
    PREVIEW_CURRENT_NOT_SET,
    ConfigSnapshotCopies,
    render_preview_value,
)
from meshtastic.cli.config_readiness import ensure_config_sections_loaded
from meshtastic.cli.context import CliExit, _terminate_cli
from meshtastic.cli.invocation import current_camel_case
from meshtastic.cli.preference_runtime import (
    CONFIGURE_PREFLIGHT_MODE,
    PREF_VALIDATION_REPORTER,
    REDACTED_PREF_VALUE,
    SET_VALUE_REJECTED_MESSAGE,
    PreferenceValueError,
    fatal_preference_value_errors,
    is_secret_pref,
    normalize_pref_name,
    report_pref_validation,
    resolve_pref,
    set_pref,
    split_compound_name,
    walk_config_path,
)
from meshtastic.configure_verify import LocalApplyStatus, verify_local_config_apply
from meshtastic.mesh_interface import MeshInterface

LOCAL_SET_APPLY_VERIFY_SECONDS = 12.0
"""Shared verification budget for one local ``--set`` apply, across all sections.

Local config writes have no routing acknowledgment, so the only honest proof is
a fresh config readback. One bounded budget (never multiplied per section)
covers every section reload of a single batch; 12s matches the node's
correlated admin-response wait, which is generous for a healthy local getter
round trip while still failing a dead device within seconds.
"""

MODULE_PREFIX_SEGMENT = "module"
MODULE_PREFIX_HINT = (
    "Preference paths are written without a 'module.' prefix; module sections"
    " resolve directly (for example, 'mesh_beacon.flags')."
)


def _module_prefix_strip_hint(pref_name: str) -> str | None:
    """Return a hint when an unknown preference path carries a 'module.' prefix."""
    first_segment = pref_name.split(".", maxsplit=1)[0].strip().lower()
    if first_segment != MODULE_PREFIX_SEGMENT:
        return None
    stripped = pref_name.split(".", maxsplit=1)[1] if "." in pref_name else ""
    if stripped:
        return f"{MODULE_PREFIX_HINT} Did you mean '{stripped}'?"
    return MODULE_PREFIX_HINT


def _print_set_field_choices(
    node: Any, pref_names: Sequence[str], *, cli_print: Callable[[str], None] = print
) -> None:
    """Print historical field-not-found guidance for one or more --set names.

    Parameters
    ----------
    node : Any
        Node whose local and module configuration schemas provide the choices.
    pref_names : Sequence[str]
        Unknown preference names to identify before printing choices.
    cli_print : Callable[[str], None]
        Sink receiving each output line; defaults to ``print``.
    """
    names = list(dict.fromkeys(pref_names))
    for pref_name in names:
        cli_print(
            f"{node.localConfig.__class__.__name__} and "
            f"{node.moduleConfig.__class__.__name__} do not have an attribute {pref_name}."
        )
        hint = _module_prefix_strip_hint(pref_name)
        if hint is not None:
            cli_print(hint)
    cli_print("Choices are...")
    config_io.print_config(
        node.localConfig, camel_case=current_camel_case(), cli_print=cli_print
    )
    config_io.print_config(
        node.moduleConfig, camel_case=current_camel_case(), cli_print=cli_print
    )


def _set_pref(
    config: Any, comp_name: str, raw_value: Any, *, cli_print: Callable[..., None]
) -> bool:
    """Assign one preference through the runtime with the active naming mode."""
    return set_pref(
        config,
        comp_name,
        raw_value,
        camel_case=current_camel_case(),
        cli_print=cli_print,
        is_repeated_field=config_io.is_repeated_field,
    )


def _normalize_set_entries(
    set_entries: Sequence[Sequence[Any] | None],
) -> list[tuple[str, Any]]:
    """Normalize argparse --set entries into explicit name/value pairs.

    Parameters
    ----------
    set_entries : Sequence[Sequence[Any] | None]
        Parsed ``--set`` entries, each containing at least a field name and value.
        Any additional elements are ignored for compatibility with the historical
        ``item[0]``/``item[1]`` parsing contract.

    Returns
    -------
    list[tuple[str, Any]]
        Normalized ``(field_name, value)`` pairs with incomplete entries omitted.
    """
    return [
        (str(item[0]), item[1])
        for item in set_entries
        if item is not None and len(item) >= 2
    ]


def _format_set_preflight_exception(pref_name: str, exc: Exception) -> str:
    """Format a preflight exception without exposing secret preference values.

    Parameters
    ----------
    pref_name : str
        Canonical preference path whose validation raised ``exc``.
    exc : Exception
        Validation exception raised by the protobuf/runtime conversion path.

    Returns
    -------
    str
        Safe error detail suitable for the aggregated CLI failure message.
    """
    if isinstance(exc, PreferenceValueError):
        return str(exc)
    if is_secret_pref(pref_name):
        return (
            f"{pref_name}: invalid value {REDACTED_PREF_VALUE} ({type(exc).__name__})"
        )
    return f"{pref_name}: {exc}"


def _resolve_set_target(
    configs: Sequence[Any], pref_name: str
) -> tuple[Any, FieldDescriptor] | None:
    """Resolve the owning config message and root field for a normalized preference.

    Parameters
    ----------
    configs : Sequence[Any]
        Configuration wrapper messages to search in resolution order.
    pref_name : str
        Normalized dotted preference path.

    Returns
    -------
    tuple[Any, FieldDescriptor] | None
        Owning config wrapper and root field descriptor, or ``None`` when the
        root field does not exist in any supplied configuration.
    """
    root_field = split_compound_name(pref_name)[0]
    for config in configs:
        config_type = config.DESCRIPTOR.fields_by_name.get(root_field)
        if config_type is not None:
            return config, config_type
    return None


def _ensure_set_sections_loaded(
    node: Any, set_entries: Sequence[tuple[str, Any]], *, cli_exit: CliExit
) -> None:
    """Request missing config sections and wait for them before preflight.

    Parameters
    ----------
    node : Any
        Target node whose cached local/module configuration will be validated.
    set_entries : Sequence[tuple[str, Any]]
        Parsed ``--set`` name/value entries. Names are normalized before resolution.
    cli_exit : CliExit
        Entrypoint-owned exit effect used when a requested section never arrives.

    Notes
    -----
    Requests are deduplicated by config section. Protobuf message presence, not
    ``ListFields()``, distinguishes an already-loaded default-valued section from
    a section that has never been received. Unknown preference paths do not
    trigger device reads. Requested sections share one bounded wait using the
    node's existing timeout so the write and preview paths validate and render
    against received state instead of protobuf defaults; a section that never
    arrives exits before any validation, rendering, or write. ``noProto`` nodes
    are never waited on because no response can arrive without protocol use.

    The dedup/request/wait/abort policy itself lives in
    :mod:`meshtastic.cli.config_readiness`, shared with the ``--configure``
    paths; this wrapper only collects the section pairs implied by the parsed
    ``--set`` entries.
    """
    configs = (node.localConfig, node.moduleConfig)
    sections: list[tuple[Any, FieldDescriptor]] = []
    for raw_pref_name, _raw_value in set_entries:
        pref_name = normalize_pref_name(raw_pref_name)
        resolved = _resolve_set_target(configs, pref_name)
        if resolved is None:
            continue
        config, config_type = resolved
        if not resolve_pref(config, pref_name):
            continue
        sections.append((config, config_type))
    ensure_config_sections_loaded(node, sections, cli_exit=cli_exit)


def _validate_set_entries_against_configs(
    node: Any,
    config_copies: Sequence[Any],
    set_entries: Sequence[tuple[str, Any]],
    *,
    cli_exit: CliExit,
    cli_print: Callable[..., None],
) -> bool:
    """
    Validate --set entries against prepared configuration copies as one batch.

    Parameters
    ----------
    node : Any
        Node providing the cached configuration used for field-choice guidance.
    config_copies : Sequence[Any]
        Mutable protobuf copies that receive the validated assignments.
    set_entries : Sequence[tuple[str, Any]]
        Preference names and raw values to validate as one batch.
    cli_exit : CliExit
        Entrypoint-owned exit effect used to reject the batch.
    cli_print : Callable[..., None]
        Entrypoint-owned reporter. Field-choice guidance is printed through a
        forced variant so it remains visible under ``--quiet``; value
        rejections and assignment messages use the quiet-aware base sink.

    Returns
    -------
    bool
        ``True`` if every entry is valid; ``False`` if an unknown field or semantic
        validation failure rejects the batch.

    Notes
    -----
    Shared by the normal ``--set`` preflight and the ``--dry-run`` preview so both
    paths apply identical normalization, descriptor, metadata, and nanopb
    validation semantics. Unknown fields print field-choice guidance, fatal value
    errors aggregate into one ``cli_exit``, and value rejections are reported
    individually.
    """
    forced_print = _forced_reporter(cli_print)
    fatal_errors: list[str] = []
    unknown_fields: list[str] = []
    value_rejections: list[tuple[str, tuple[str, ...]]] = []
    token = CONFIGURE_PREFLIGHT_MODE.set(True)
    try:
        for raw_pref_name, raw_value in set_entries:
            pref_name = normalize_pref_name(raw_pref_name)
            resolved = _resolve_set_target(config_copies, pref_name)
            if resolved is None:
                unknown_fields.append(pref_name)
                continue
            candidate, _config_type = resolved
            if not resolve_pref(candidate, pref_name):
                unknown_fields.append(pref_name)
                continue

            validation_messages: list[str] = []
            reporter_token = PREF_VALIDATION_REPORTER.set(validation_messages.append)
            try:
                try:
                    with fatal_preference_value_errors():
                        valid = _set_pref(
                            candidate, pref_name, raw_value, cli_print=cli_print
                        )
                finally:
                    PREF_VALIDATION_REPORTER.reset(reporter_token)
            except (TypeError, ValueError, OverflowError, binascii.Error) as exc:
                fatal_errors.append(_format_set_preflight_exception(pref_name, exc))
                continue
            if not valid:
                value_rejections.append((pref_name, tuple(validation_messages)))
    finally:
        CONFIGURE_PREFLIGHT_MODE.reset(token)

    if unknown_fields:
        _print_set_field_choices(node, unknown_fields, cli_print=forced_print)

    if fatal_errors:
        detail_lines = [f"  - {error}" for error in fatal_errors]
        for pref_name, messages in value_rejections:
            detail_lines.append(f"  - {pref_name}: {SET_VALUE_REJECTED_MESSAGE}")
            detail_lines.extend(f"      {message}" for message in messages)
        details = "\n".join(detail_lines)
        cli_exit(f"ERROR: --set batch rejected before applying changes:\n{details}")

    for pref_name, messages in value_rejections:
        if messages:
            for message in messages:
                report_pref_validation(message, cli_print=cli_print)
        else:
            report_pref_validation(
                f"{pref_name}: {SET_VALUE_REJECTED_MESSAGE}", cli_print=cli_print
            )

    return not (unknown_fields or value_rejections)


def _forced_reporter(cli_print: Callable[..., None]) -> Callable[[str], None]:
    """Wrap the entrypoint reporter so guidance stays visible under ``--quiet``."""

    def _report(message: str) -> None:
        cli_print(message, force=True)

    return _report


def _preflight_set_entries(
    node: Any,
    set_entries: Sequence[tuple[str, Any]],
    *,
    cli_exit: CliExit,
    cli_print: Callable[..., None],
) -> bool:
    """
    Validate all --set entries against configuration copies before applying changes.

    Parameters
    ----------
    node : Any
        Node providing the current local and module configuration.
    set_entries : Sequence[tuple[str, Any]]
        Preference names and raw values to validate as one batch.
    cli_exit : CliExit
        Entrypoint-owned exit effect used to reject the batch.
    cli_print : Callable[..., None]
        Entrypoint-owned reporter forwarded to validation.

    Returns
    -------
    bool
        ``True`` if every entry is valid; ``False`` if an unknown field or semantic
        validation failure rejects the batch.
    """
    snapshot = ConfigSnapshotCopies.from_node(node)
    return _validate_set_entries_against_configs(
        node,
        (snapshot.local_config, snapshot.module_config),
        set_entries,
        cli_exit=cli_exit,
        cli_print=cli_print,
    )


def _handle_set_command(
    interface: MeshInterface,
    args: Any,
    getNode_kwargs: dict[str, Any],
    *,
    cli_exit: CliExit,
    cli_print: Callable[..., None],
) -> None:
    """
    Validate and apply a CLI ``--set`` batch without partial updates.

    Parameters
    ----------
    interface : MeshInterface
        Active interface used to resolve the target node.
    args : Any
        Parsed CLI arguments containing the ``--set`` entries and destination.
    getNode_kwargs : dict[str, Any]
        Additional keyword arguments forwarded to ``interface.getNode``.
    cli_exit : CliExit
        Entrypoint-owned exit effect used for refusals and divergence failures.
    cli_print : Callable[..., None]
        Entrypoint-owned reporter for progress output.

    Notes
    -----
    Invalid batches are rejected before modifying the device. Valid batches write
    all affected configuration sections and use a transaction when multiple
    sections are changed.
    """
    node = interface.getNode(args.dest, False, **getNode_kwargs)
    set_entries = _normalize_set_entries(args.set)
    _ensure_set_sections_loaded(node, set_entries, cli_exit=cli_exit)
    if not _preflight_set_entries(
        node, set_entries, cli_exit=cli_exit, cli_print=cli_print
    ):
        if _get_command_scope(interface) is not None:
            cli_exit("Invalid --set preference batch", 2)
        return

    pre_write_config_id = getattr(interface, "configId", None)
    live_configs = (node.localConfig, node.moduleConfig)
    fields: set[str] = set()
    for raw_pref_name, raw_value in set_entries:
        normalized_pref_name = normalize_pref_name(raw_pref_name)
        resolved = _resolve_set_target(live_configs, normalized_pref_name)
        if resolved is None:
            cli_exit(
                "ERROR: --set field no longer resolves after successful preflight: "
                f"{normalized_pref_name}."
            )
        config, config_type = resolved
        try:
            found = _set_pref(
                config, normalized_pref_name, raw_value, cli_print=cli_print
            )
        except (TypeError, ValueError, OverflowError, binascii.Error) as exc:
            detail = _format_set_preflight_exception(normalized_pref_name, exc)
            cli_exit(
                f"ERROR: --set apply diverged after successful preflight:\n  - {detail}"
            )
        if not found:
            cli_exit(
                "ERROR: --set apply diverged after successful preflight for "
                f"{normalized_pref_name}."
            )
        fields.add(config_type.name)

    if fields:
        cli_print("Writing modified preferences to device")
        if len(fields) > 1:
            cli_print("Using a configuration transaction")
            node.beginSettingsTransaction()
        for field in fields:
            cli_print(f"Writing {field} configuration to device")
            node.writeConfig(field)
        if len(fields) > 1:
            node.commitSettingsTransaction()
            # Mirror the --configure path: give the device time to settle
            # (and, if it reboots, to drop the link) before verifying.
            time.sleep(
                _remaining_timeout(cli_configure_actions.CONFIG_COMMIT_SETTLE_SECONDS)
            )

    local_node = getattr(interface, "localNode", None)
    if fields and node is local_node and not getattr(node, "noProto", False):
        # Freeze the staged requested values before verification clears and
        # reloads the cached sections with fresh device state.
        config_fields, module_config_fields = _freeze_set_expected_values(
            node, set_entries
        )
        _verify_local_set_apply(
            interface,
            node,
            node_dest=args.dest,
            pre_write_config_id=pre_write_config_id,
            config_fields=config_fields,
            module_config_fields=module_config_fields,
            cli_exit=cli_exit,
            cli_print=cli_print,
        )


def _freeze_set_expected_values(
    node: Any, set_entries: Sequence[tuple[str, Any]]
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Freeze the requested leaf values per section from the staged cache.

    Parameters
    ----------
    node : Any
        Local node whose cached configuration holds the staged assignments.
    set_entries : Sequence[tuple[str, Any]]
        Parsed ``--set`` name/value entries. Names are normalized before
        resolution, matching the apply loop.

    Returns
    -------
    tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]
        ``LocalConfig`` and ``LocalModuleConfig`` section mappings holding the
        frozen requested values (plain scalars, lists for repeated fields, and
        nested dicts for nested message paths).
    """
    config_fields: dict[str, dict[str, Any]] = {}
    module_config_fields: dict[str, dict[str, Any]] = {}
    for raw_pref_name, _raw_value in set_entries:
        pref_name = normalize_pref_name(raw_pref_name)
        resolved = _resolve_set_target((node.localConfig, node.moduleConfig), pref_name)
        if resolved is None:
            continue
        config, config_type = resolved
        resolved_leaf = _resolve_set_leaf(config, pref_name)
        if resolved_leaf is None:
            continue
        parts = split_compound_name(pref_name)[1:]
        if not parts:
            continue
        target, leaf_field = resolved_leaf
        section_fields = (
            config_fields if config is node.localConfig else module_config_fields
        )
        section_values = section_fields.setdefault(config_type.name, {})
        cursor = section_values
        for part in parts[:-1]:
            nested = cursor.get(part)
            if not isinstance(nested, dict):
                nested = {}
                cursor[part] = nested
            cursor = nested
        cursor[parts[-1]] = _read_set_leaf_value(target, leaf_field)
    return config_fields, module_config_fields


def _availability_changing_set_fields(
    config_fields: dict[str, dict[str, Any]],
    module_config_fields: dict[str, dict[str, Any]],
) -> tuple[str, ...]:
    """Return requested ``--set`` leaf paths that can interrupt node availability.

    Parameters
    ----------
    config_fields : dict[str, dict[str, Any]]
        Frozen requested ``LocalConfig`` values by section.
    module_config_fields : dict[str, dict[str, Any]]
        Frozen requested ``LocalModuleConfig`` values by section.

    Returns
    -------
    tuple[str, ...]
        Dotted snake_case leaf paths (in request order, deduplicated) whose
        write is in :data:`cli_configure_actions.PREFS_WITH_AVAILABILITY_CHANGES`.
    """
    paths: list[str] = []
    for fields in (config_fields, module_config_fields):
        for section, values in fields.items():
            paths.extend(cli_configure_actions._flatten_leaf_paths(section, values))
    return tuple(
        path
        for path in dict.fromkeys(paths)
        if path in cli_configure_actions.PREFS_WITH_AVAILABILITY_CHANGES
    )


def _verify_local_set_apply(
    interface: MeshInterface,
    node: Any,
    *,
    node_dest: str,
    pre_write_config_id: int | None,
    config_fields: dict[str, dict[str, Any]],
    module_config_fields: dict[str, dict[str, Any]],
    cli_exit: CliExit,
    cli_print: Callable[..., None],
) -> None:
    """Verify fresh device state for a local ``--set`` batch; exit on failure.

    Parameters
    ----------
    interface : MeshInterface
        Connected interface observed for reboot/disconnect handling.
    node : Any
        Local node that received the ``--set`` writes.
    node_dest : str
        Destination string the node was resolved from; reused by the
        reconnect-based fallback re-verification.
    pre_write_config_id : int | None
        Interface config-generation id captured before any writes. A changed
        generation proves that a reboot/config restart occurred even when the
        link reconnected before the readback budget expired.
    config_fields : dict[str, dict[str, Any]]
        Frozen requested ``LocalConfig`` values by section.
    module_config_fields : dict[str, dict[str, Any]]
        Frozen requested ``LocalModuleConfig`` values by section.
    cli_exit : CliExit
        Entrypoint-owned exit effect used when verification fails.
    cli_print : Callable[..., None]
        Entrypoint-owned reporter for verification outcomes.

    Notes
    -----
    Local config writes select no acknowledgment callback, so success is only
    proven by freshly reloading the affected sections and comparing the
    requested values against device state. Any non-verified outcome fails the
    CLI instead of reporting an unqualified success; verification is skipped
    entirely for ``noProto`` nodes by the caller. Send failures from the
    verification request propagate out of the seam and map to a CLI error
    here. When the readback budget expires because the device dropped the
    link (a commit-triggered reboot), one bounded reconnect-based
    re-verification using the existing ``--configure`` machinery gets the
    final word; a connection-alive reload failure still fails immediately.

    Region writes retain bounded readback and mismatch detection. If no fresh
    response arrives, report an unverified send because provisioning or a
    radio restart can interrupt availability.
    """
    try:
        if not config_fields and not module_config_fields:
            # Only requested sections are verifiable; an empty freeze would be
            # vacuously VERIFIED, which proves nothing about the sent write.
            raise ValueError("no requested sections were frozen for verification")
        verification = verify_local_config_apply(
            node,
            config_fields=config_fields or None,
            module_config_fields=module_config_fields or None,
            timeout_sec=LOCAL_SET_APPLY_VERIFY_SECONDS,
        )
    except Exception as exc:
        _terminate_cli(
            cli_exit,
            "ERROR: local --set verification could not read fresh device state: "
            f"{exc}. The requested change could not be verified.",
        )
    if verification.status == LocalApplyStatus.VERIFIED:
        cli_print("Verified: fresh device state matches the requested --set values.")
        return
    if (
        verification.status == LocalApplyStatus.MISMATCH
        or verification.mismatched_fields
    ):
        fields_text = ", ".join(verification.mismatched_fields)
        _terminate_cli(
            cli_exit,
            "ERROR: --set was sent, but fresh device state reports different "
            f"values for: {fields_text}. The device did not apply the requested "
            "value(s).",
        )
    availability_fields = _availability_changing_set_fields(
        config_fields, module_config_fields
    )
    if verification.status == LocalApplyStatus.RELOAD_FAILED and availability_fields:
        cli_print(
            "Region write may interrupt node availability ("
            f"{', '.join(availability_fields)}); read-back verification was "
            "unavailable. Requested changes were sent, not verified."
        )
        return
    if verification.status == LocalApplyStatus.RELOAD_FAILED and (
        _link_dropped(interface)
        or _config_generation_changed(interface, pre_write_config_id)
    ):
        _verify_local_set_after_reconnect(
            interface,
            node_dest=node_dest,
            config_fields=config_fields,
            module_config_fields=module_config_fields,
            cli_exit=cli_exit,
            cli_print=cli_print,
        )
        return
    if verification.status == LocalApplyStatus.RELOAD_FAILED:
        sections_text = ", ".join(verification.missing_sections)
        _terminate_cli(
            cli_exit,
            "ERROR: --set was sent, but the device did not return the "
            f"{sections_text} configuration section(s) within "
            f"{LOCAL_SET_APPLY_VERIFY_SECONDS:g} seconds; the requested value(s) "
            "could not be verified.",
        )
    _terminate_cli(
        cli_exit,
        "ERROR: local --set verification returned an unrecognized outcome "
        f"({verification.status!r}); the requested change could not be confirmed.",
    )


def _config_generation_changed(
    interface: MeshInterface, previous_config_id: int | None
) -> bool:
    """Return whether the interface began a new config generation since the write.

    A fast reboot can drop and restore the transport entirely within the local
    readback budget. ``isConnected`` then looks healthy at the final check, but
    ``configId`` still records the reboot/config restart and must trigger the
    reconnect/full-config verification path. Interfaces without a generation
    attribute preserve the historical link-state-only behavior.
    """
    return getattr(interface, "configId", previous_config_id) != previous_config_id


def _link_dropped(interface: MeshInterface) -> bool:
    """Return whether the interface link is currently down (possible reboot)."""
    connected = getattr(interface, "isConnected", None)
    if connected is None:
        return False
    return not connected.is_set()


def _verify_local_set_after_reconnect(
    interface: MeshInterface,
    *,
    node_dest: str,
    config_fields: dict[str, dict[str, Any]],
    module_config_fields: dict[str, dict[str, Any]],
    cli_exit: CliExit,
    cli_print: Callable[..., None],
) -> None:
    """Re-verify a local ``--set`` once over one bounded reconnect.

    Parameters
    ----------
    interface : MeshInterface
        Interface whose link dropped after the commit (reboot in progress).
    node_dest : str
        Destination string the local node was resolved from.
    config_fields : dict[str, dict[str, Any]]
        Frozen requested ``LocalConfig`` values by section.
    module_config_fields : dict[str, dict[str, Any]]
        Frozen requested ``LocalModuleConfig`` values by section.
    cli_exit : CliExit
        Entrypoint-owned exit effect used when the reconnect check fails.
    cli_print : Callable[..., None]
        Entrypoint-owned reporter for verification outcomes.

    Notes
    -----
    Reuses the ``--configure`` reconnect/verification machinery so a
    commit-triggered device reboot gets the same tolerance as ``--configure``:
    one bounded wait for reconnect, one config reload, and the same value
    comparator over the frozen requested values.
    """
    reconnect_result = cli_configure_actions._post_configure_reconnect_and_verify(
        interface,
        timeout=cli_configure_actions.CONFIG_RECONNECT_WAIT_SECONDS,
        node_dest=node_dest,
        verify_config_fields=config_fields or None,
        verify_module_config_fields=module_config_fields or None,
        verify_channel_url_against_state=cli_configure_actions._verify_channel_url_against_state,
    )
    if reconnect_result == cli_configure_actions.ConfigureReconnectResult.VERIFIED:
        cli_print("Verified: fresh device state matches the requested --set values.")
        return
    _terminate_cli(
        cli_exit,
        "ERROR: --set was sent, but the device dropped the link before the write "
        "could be verified and the reconnect check failed "
        f"({cli_configure_actions._configure_reconnect_message(reconnect_result)}). "
        "The requested value(s) could not be verified.",
    )


def _resolve_set_leaf(
    config: Any, pref_name: str
) -> tuple[Any, FieldDescriptor] | None:
    """Resolve the leaf parent message and descriptor for one preference path.

    Parameters
    ----------
    config : Any
        Configuration wrapper message to resolve the path against.
    pref_name : str
        Normalized dotted preference path.

    Returns
    -------
    tuple[Any, FieldDescriptor] | None
        Owning parent message and leaf field descriptor, or ``None`` when the
        path does not resolve.
    """
    name_parts = split_compound_name(pref_name)
    parent, config_type = walk_config_path(config, name_parts)
    if config_type is None:
        return None
    if config_type.message_type is not None:
        leaf_field = config_type.message_type.fields_by_name.get(name_parts[-1])
    else:
        leaf_field = config_type
    if leaf_field is None:
        return None
    target = (
        getattr(parent, config_type.name)
        if config_type.message_type is not None
        else parent
    )
    return target, leaf_field


def _read_set_leaf_value(parent: Any, leaf_field: FieldDescriptor) -> Any:
    """Read one preference value from its parent message, listifying repeated fields.

    Parameters
    ----------
    parent : Any
        Message owning the leaf field.
    leaf_field : FieldDescriptor
        Descriptor of the leaf field to read.

    Returns
    -------
    Any
        Scalar leaf value or a plain list for repeated fields.
    """
    value = getattr(parent, leaf_field.name)
    if config_io.is_repeated_field(leaf_field):
        return list(value)
    return value


def _render_preview_current_value(pref_name: str, live_configs: Sequence[Any]) -> str:
    """Render one preference's current value from the live cached configuration.

    Parameters
    ----------
    pref_name : str
        Normalized dotted preference path.
    live_configs : Sequence[Any]
        The node's cached local and module configuration messages.

    Returns
    -------
    str
        Rendered current value, ``not set`` for an unloaded section, with
        secret-bearing paths redacted on every side.
    """
    resolved = _resolve_set_target(live_configs, pref_name)
    if resolved is None:
        return PREVIEW_CURRENT_NOT_SET
    live_config, config_type = resolved
    try:
        section_present = live_config.HasField(config_type.name)
    except ValueError:
        section_present = True
    if not section_present:
        return PREVIEW_CURRENT_NOT_SET
    resolved_leaf = _resolve_set_leaf(live_config, pref_name)
    if resolved_leaf is None:
        return PREVIEW_CURRENT_NOT_SET
    target, leaf_field = resolved_leaf
    current_value = _read_set_leaf_value(target, leaf_field)
    return render_preview_value(pref_name, leaf_field, current_value)


def _dedupe_set_entries_last_wins(
    set_entries: Sequence[tuple[str, Any]],
) -> list[tuple[str, Any]]:
    """Collapse duplicate preference assignments, keeping the last value.

    Parameters
    ----------
    set_entries : Sequence[tuple[str, Any]]
        Normalized ``(field_name, value)`` pairs in batch order.

    Returns
    -------
    list[tuple[str, Any]]
        One entry per normalized preference path, in first-mention order,
        carrying the last assigned value.
    """
    collapsed: dict[str, Any] = {}
    for raw_pref_name, raw_value in set_entries:
        collapsed[normalize_pref_name(raw_pref_name)] = raw_value
    return list(collapsed.items())


def _preview_set_command(
    interface: MeshInterface,
    args: Any,
    get_node_kwargs: dict[str, Any],
    *,
    cli_exit: CliExit,
    cli_print: Callable[..., None],
) -> ConfigSnapshotCopies:
    """
    Preview a CLI ``--set`` batch without writing changes to the device.

    Parameters
    ----------
    interface : MeshInterface
        Active interface used to resolve the target node.
    args : Any
        Parsed CLI arguments containing the ``--set`` entries and destination.
    get_node_kwargs : dict[str, Any]
        Additional keyword arguments forwarded to ``interface.getNode``.
    cli_exit : CliExit
        Entrypoint-owned exit effect used to reject an invalid batch.
    cli_print : Callable[..., None]
        Entrypoint-owned reporter for the rendered plan.

    Returns
    -------
    ConfigSnapshotCopies
        Deep copies of the node configuration carrying the previewed
        assignments, chained into any later ``--configure`` preview.

    Notes
    -----
    Read-only by construction: only missing-section read requests are issued
    and awaited, validated assignments are applied to the returned protobuf
    copies, and no
    writes, transactions, or acknowledgments are ever performed. Invalid
    batches report the same preflight errors as the normal path and exit
    without any preview output. Validation considers every entry — including
    superseded duplicates — so an invalid earlier assignment rejects the
    preview exactly as the real batch would be rejected, while the rendered
    plan collapses a duplicated preference path to one line with its last
    assigned value.
    """
    node = interface.getNode(args.dest, False, **get_node_kwargs)
    batch_entries = _normalize_set_entries(args.set)
    _ensure_set_sections_loaded(node, batch_entries, cli_exit=cli_exit)
    snapshot = ConfigSnapshotCopies.from_node(node)
    config_copies = (snapshot.local_config, snapshot.module_config)
    if not _validate_set_entries_against_configs(
        node,
        config_copies,
        batch_entries,
        cli_exit=cli_exit,
        cli_print=cli_print,
    ):
        cli_exit(
            "ERROR: --set batch rejected during dry run; "
            "no changes were written to the device."
        )

    # Validation sees every entry — including superseded duplicates — so an
    # invalid earlier assignment rejects the preview exactly as it would the
    # real batch. Only the rendered plan collapses to last-write-wins.
    display_entries = _dedupe_set_entries_last_wins(batch_entries)
    live_configs = (node.localConfig, node.moduleConfig)
    cli_print("Dry run: previewing --set batch without writing changes.")
    sections: set[str] = set()
    for raw_pref_name, _raw_value in display_entries:
        pref_name = normalize_pref_name(raw_pref_name)
        resolved = _resolve_set_target(config_copies, pref_name)
        if resolved is None:
            cli_exit(
                "ERROR: --set field no longer resolves after successful preflight: "
                f"{pref_name}."
            )
        candidate, config_type = resolved
        resolved_leaf = _resolve_set_leaf(candidate, pref_name)
        if resolved_leaf is None:
            cli_exit(
                "ERROR: --set field no longer resolves after successful preflight: "
                f"{pref_name}."
            )
        target, leaf_field = resolved_leaf
        new_value = _read_set_leaf_value(target, leaf_field)
        new_rendered = render_preview_value(pref_name, leaf_field, new_value)
        current_rendered = _render_preview_current_value(pref_name, live_configs)
        sections.add(config_type.name)
        cli_print(
            f"Would set {pref_name} = {new_rendered} (current: {current_rendered})"
        )

    if sections:
        cli_print("Would write modified preferences to device")
        if len(sections) > 1:
            cli_print("Would use a configuration transaction")
        for section in sections:
            cli_print(f"Would write {section} configuration to device")
    return snapshot
