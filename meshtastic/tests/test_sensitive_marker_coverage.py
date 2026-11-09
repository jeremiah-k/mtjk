"""Drift canary for upstream ``sensitive`` field markers vs. mtjk redaction.

Upstream protobufs (open PR #774) mark credential-bearing leaf fields with a
``sensitive`` option in ``field_metadata_pb2.FieldMetadata``. This module pins
the contract that every marked field is either redacted by mtjk's preference
chokepoint (:mod:`meshtastic.cli.preference_runtime`) or explicitly documented
in ``ALLOWED_VISIBLE``, and that mtjk's deliberate extra redactions stay a
conscious, documented set.

Honest coverage limits (deliberate): this canary asserts field-granular
preference redaction only. The legacy ``--info`` and ``--export-config``
commands keep showing/exporting sensitive data for backwards compatibility
(user policy decision, 2026-11-09), packet-path and BLE text layers are
outside marker scope, and none of those surfaces are asserted here.

Self-activation: on trees whose ``field_metadata_pb2.FieldMetadata`` lacks the
``sensitive`` option (develop before #774) the whole module skips. Once a
schema digest carrying markers lands, the assertions activate without further
edits; a later digest that adds, moves, or un-marks markers forces a conscious
edit here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from functools import lru_cache
from types import ModuleType
from typing import Protocol, cast

import pytest
from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.message import Message

import meshtastic.protobuf.admin_pb2 as admin_pb2
import meshtastic.protobuf.channel_pb2 as channel_pb2
import meshtastic.protobuf.clientonly_pb2 as clientonly_pb2
import meshtastic.protobuf.config_pb2 as config_pb2
import meshtastic.protobuf.connection_status_pb2 as connection_status_pb2
import meshtastic.protobuf.device_ui_pb2 as device_ui_pb2
import meshtastic.protobuf.module_config_pb2 as module_config_pb2
from meshtastic.cli.preference_runtime import is_secret_pref
from meshtastic.schema_metadata import _resolve_config_field

field_metadata_pb2: ModuleType = pytest.importorskip(
    "meshtastic.protobuf.field_metadata_pb2"
)

if "sensitive" not in field_metadata_pb2.FieldMetadata.DESCRIPTOR.fields_by_name:
    pytest.skip(
        "field_metadata_pb2 declares no 'sensitive' option (pre-#774 schema); "
        "this canary self-activates once the markers land",
        allow_module_level=True,
    )


# Schema roots that contain (or are) sensitive-marked fields. Each root is
# discovered by enumerating its module's message types rather than by
# hardcoding descriptor locations, so digest reorganizations surface as a
# loud failure instead of silent under-coverage.
_ROOT_MODULES: tuple[ModuleType, ...] = (
    config_pb2,
    module_config_pb2,
    channel_pb2,
    admin_pb2,
    clientonly_pb2,
    connection_status_pb2,
    device_ui_pb2,
)
_ROOT_CLASS_NAMES: tuple[str, ...] = (
    "Config",
    "ModuleConfig",
    "ChannelSettings",
    "LockdownAuth",
    "DeviceProfile",
    "BluetoothConnectionStatus",
    "DeviceUIConfig",
)

# How each root's marked paths are spelled for ``is_secret_pref`` evaluation.
# Config and ModuleConfig sections are addressed by the CLI without the root
# qualifier ("network.wifi_psk"), matching the getPref/set/preview secret
# classification. DeviceUIConfig only appears embedded as the local-config
# section "device_ui", so paths from its standalone root are spelled with that
# section name. The remaining roots have no CLI preference-path representation;
# their root-qualified spellings are exactly what ``ALLOWED_VISIBLE`` documents.
_ROOT_PATH_SPELLING: dict[str, str] = {
    "Config": "",
    "ModuleConfig": "",
    "DeviceUIConfig": "device_ui",
    "ChannelSettings": "ChannelSettings",
    "LockdownAuth": "LockdownAuth",
    "DeviceProfile": "DeviceProfile",
    "BluetoothConnectionStatus": "BluetoothConnectionStatus",
}

# DeviceProfile embeds full local/module config subtrees. Those subtrees are
# the same schema messages already walked from the Config and ModuleConfig
# roots, so recursing here would only duplicate leaf coverage.
_PROFILE_EMBEDDED_SUBTREES: frozenset[str] = frozenset({"config", "module_config"})

# Marked fields that mtjk deliberately leaves visible, with the reason each
# entry exists. Anything not listed here and not classified by
# ``is_secret_pref`` fails the coverage assertion.
ALLOWED_VISIBLE: dict[str, str] = {
    "DeviceProfile.channel_url": (
        "legacy round-trip export compatibility by policy (user decision "
        "2026-11-09): --export-config keeps channel_url verbatim"
    ),
    "LockdownAuth.passphrase": (
        "no mtjk display path exists; LockdownAuth travels in admin messages only"
    ),
    "BluetoothConnectionStatus.pin": (
        "no mtjk display path exists; the pin is connection-state reporting only"
    ),
}

# Schema-resolvable paths mtjk redacts although upstream does not mark them.
# Each entry must stay a conscious policy decision.
MTJK_EXTRA_REDACTIONS: dict[str, str] = {
    "mqtt.username": (
        "deliberate mtjk policy: broker identity redacts alongside broker "
        "credentials even though upstream treats it as an identifier"
    ),
}

# Roots whose sections are addressable as CLI preference paths; this mirrors
# the lookup universe of ``schema_metadata._resolve_config_field``.
_PREF_UNIVERSE_ROOTS: tuple[str, ...] = ("Config", "ModuleConfig")


class DescriptorLike(Protocol):
    """Structural descriptor type shared by pure-Python and upb protobuf runtimes."""

    @property
    def fields(self) -> Sequence[FieldDescriptor]:
        """Return fields declared by this message descriptor."""

    @property
    def fields_by_name(self) -> Mapping[str, FieldDescriptor]:
        """Return the declared fields keyed by name."""


@lru_cache(maxsize=1)
def _message_types_by_name() -> dict[str, type[Message]]:
    """Enumerate the root modules' message classes for root discovery."""
    available: dict[str, type[Message]] = {}
    for module in _ROOT_MODULES:
        for name in dir(module):
            obj = getattr(module, name)
            if isinstance(obj, type) and issubclass(obj, Message):
                available[name] = obj
    return available


@lru_cache(maxsize=1)
def _root_types() -> dict[str, type[Message]]:
    """Resolve one descriptor-bearing message class per required schema root."""
    available = _message_types_by_name()
    missing = [name for name in _ROOT_CLASS_NAMES if name not in available]
    if missing:
        pytest.fail(f"schema roots missing message types: {missing}")
    return {name: available[name] for name in _ROOT_CLASS_NAMES}


def _is_marked_sensitive(field: FieldDescriptor) -> bool:
    """Return whether a field descriptor carries a true ``sensitive`` marker."""
    options = field.GetOptions()
    if not options.HasExtension(field_metadata_pb2.field_metadata):
        return False
    metadata = options.Extensions[field_metadata_pb2.field_metadata]
    return bool(metadata.HasField("sensitive") and metadata.sensitive)


def _walk_marked_fields(
    descriptor: DescriptorLike,
    *,
    root_name: str,
    prefix: str,
    marked: dict[str, FieldDescriptor],
) -> None:
    """Recursively collect spelled paths of sensitive-marked fields under a root."""
    for field in descriptor.fields:
        if root_name == "DeviceProfile" and field.name in _PROFILE_EMBEDDED_SUBTREES:
            continue
        path = f"{prefix}.{field.name}" if prefix else field.name
        if _is_marked_sensitive(field):
            marked.setdefault(path, field)
        if field.message_type is not None:
            _walk_marked_fields(
                field.message_type,
                root_name=root_name,
                prefix=path,
                marked=marked,
            )


def _marked_pref_paths() -> dict[str, FieldDescriptor]:
    """Map every sensitive-marked leaf's CLI-pref spelling to its descriptor."""
    marked: dict[str, FieldDescriptor] = {}
    for root_name, message_type in _root_types().items():
        # Protobuf's public descriptor classes recognize the upb equivalents
        # at runtime; their stubs describe the two implementations as
        # distinct types.
        _walk_marked_fields(
            cast(DescriptorLike, message_type.DESCRIPTOR),
            root_name=root_name,
            prefix=_ROOT_PATH_SPELLING[root_name],
            marked=marked,
        )
    return marked


def _collect_unmarked_redacted_paths(
    descriptor: DescriptorLike, *, prefix: str, found: set[str]
) -> None:
    """Collect leaf paths mtjk redacts that the schema does not mark sensitive."""
    for field in descriptor.fields:
        path = f"{prefix}.{field.name}" if prefix else field.name
        if (
            field.message_type is None
            and is_secret_pref(path)
            and _resolve_config_field(path) is not None
            and not _is_marked_sensitive(field)
        ):
            found.add(path)
        if field.message_type is not None:
            _collect_unmarked_redacted_paths(
                field.message_type, prefix=path, found=found
            )


def _unmarked_redacted_schema_paths() -> set[str]:
    """Return redacted resolvable paths upstream does not mark sensitive."""
    found: set[str] = set()
    for root_name in _PREF_UNIVERSE_ROOTS:
        _collect_unmarked_redacted_paths(
            cast(DescriptorLike, _root_types()[root_name].DESCRIPTOR),
            prefix="",
            found=found,
        )
    return found


def _resolve_root_qualified(
    path: str, root_types: dict[str, type[Message]]
) -> FieldDescriptor | None:
    """Resolve a root-qualified dotted path to its field descriptor, or None."""
    root_name, _, remainder = path.partition(".")
    root = root_types.get(root_name)
    if root is None or not remainder:
        return None
    descriptor: DescriptorLike = cast(DescriptorLike, root.DESCRIPTOR)
    field: FieldDescriptor | None = None
    for part in remainder.split("."):
        field = descriptor.fields_by_name.get(part)
        if field is None:
            return None
        if field.message_type is not None:
            descriptor = field.message_type
    return field


@pytest.mark.unit
def test_sensitive_marker_paths_are_covered_or_documented() -> None:
    """Every upstream-marked path is redacted by mtjk or explicitly allowlisted."""
    marked = _marked_pref_paths()
    assert marked, (
        "no sensitive-marked fields found although the schema declares the "
        "'sensitive' option; verify the marker extraction"
    )
    uncovered = sorted(
        path
        for path in marked
        if path not in ALLOWED_VISIBLE and not is_secret_pref(path)
    )
    assert uncovered == [], (
        "sensitive-marked paths are neither redacted by the preference "
        "chokepoint nor documented in ALLOWED_VISIBLE; extend SECRET_PREF_* "
        f"or ALLOWED_VISIBLE deliberately: {uncovered}"
    )


@pytest.mark.unit
def test_mtjk_extra_redactions_stay_deliberate() -> None:
    """Paths mtjk redacts beyond upstream markers must be exactly the extras."""
    extras = _unmarked_redacted_schema_paths()
    assert extras == set(MTJK_EXTRA_REDACTIONS), (
        "mtjk redacts schema-resolvable paths upstream does not mark beyond "
        "the documented extras (or a documented extra no longer applies); "
        f"reconcile MTJK_EXTRA_REDACTIONS with the schema: computed={extras}"
    )


@pytest.mark.unit
def test_sensitive_markers_stay_leaf_only() -> None:
    """Upstream marks leaf fields only; a marked container must fail loudly."""
    containers = sorted(
        path
        for path, field in _marked_pref_paths().items()
        if field.message_type is not None
    )
    assert containers == [], (
        "sensitive markers on container (message-typed) fields break the "
        f"leaf-only rule; mtjk redaction is field-granular: {containers}"
    )


@pytest.mark.unit
def test_canary_allowlist_entries_resolve_to_schema_paths() -> None:
    """Allowlist and extras entries must resolve, with statuses still accurate."""
    root_types = _root_types()
    for path, reason in ALLOWED_VISIBLE.items():
        field = _resolve_root_qualified(path, root_types)
        assert field is not None, (
            f"ALLOWED_VISIBLE entry does not resolve in the schema: {path} "
            f"({reason})"
        )
        assert not is_secret_pref(path), (
            f"ALLOWED_VISIBLE entry is now actually redacted; refresh the "
            f"documentation: {path} ({reason})"
        )
        assert _is_marked_sensitive(field), (
            f"ALLOWED_VISIBLE entry is no longer upstream-marked, so it no "
            f"longer needs the documentation: {path} ({reason})"
        )

    for path, reason in MTJK_EXTRA_REDACTIONS.items():
        field = _resolve_config_field(path)
        assert field is not None, (
            f"MTJK_EXTRA_REDACTIONS entry does not resolve in the schema: "
            f"{path} ({reason})"
        )
        assert is_secret_pref(
            path
        ), f"MTJK_EXTRA_REDACTIONS entry is no longer redacted: {path} ({reason})"
        assert not _is_marked_sensitive(field), (
            f"MTJK_EXTRA_REDACTIONS entry is now upstream-marked and no longer "
            f"'extra'; reconcile the documentation: {path} ({reason})"
        )
