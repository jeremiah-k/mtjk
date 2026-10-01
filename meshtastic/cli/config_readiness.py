"""Shared private contract for configuration-section acquisition readiness.

This module is the single private owner of the policy for making sure a node's
cached configuration messages actually contain the sections a CLI action is
about to validate or write. Both the ``--set`` path and the ``--configure``
paths (apply and preview) delegate here so they cannot drift apart:

- sections already present on the cached node are never re-requested (a
  present default-valued section counts as loaded, so ``HasField`` presence,
  not ``ListFields()``, decides);
- missing sections are requested once each, deduplicated by wrapper-root full
  name and section field name;
- the whole batch shares exactly one bounded wait through the node's existing
  ``Timeout.waitForSet`` machinery;
- a batch that is still incomplete when the wait gives up aborts the action
  before any validation, rendering, or write.

This module is internal to the CLI package and must not be re-exported from
the public ``meshtastic`` API.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Any, NoReturn

from google.protobuf.descriptor import FieldDescriptor

_TIMEOUT_EXIT_PREFIX = "ERROR: timed out waiting for the "
_TIMEOUT_EXIT_SUFFIX = " configuration section from the device; no changes were made."


class ConfigSectionProbe:
    """Expose pending config-section presence as one boolean wait target."""

    def __init__(self, checks: Sequence[tuple[Callable[[str], bool], str]]) -> None:
        """Bundle per-section presence checks into a single wait target.

        Parameters
        ----------
        checks : Sequence[tuple[Callable[[str], bool], str]]
            ``(HasField, section_name)`` pairs, one per pending section.
        """
        self._checks = checks

    @property
    def is_set(self) -> bool:
        """Return whether every requested config section is currently present."""
        for has_field_fn, name in self._checks:
            try:
                if not has_field_fn(name):
                    return False
            except (TypeError, ValueError):
                return False
        return True


def wait_for_config_sections(
    node: Any, sections: Sequence[tuple[Any, FieldDescriptor]]
) -> bool:
    """Wait once for all requested config sections to arrive on the cached node.

    Parameters
    ----------
    node : Any
        Node whose ``_timeout.waitForSet`` machinery performs the bounded wait.
    sections : Sequence[tuple[Any, FieldDescriptor]]
        ``(config_root, section_field)`` pairs that were requested from the
        device.

    Returns
    -------
    bool
        ``True`` only when callable wait machinery confirms the probe. Nodes
        without wait machinery return ``False`` so the owner can re-check the
        requested protobuf presence and fail closed if it is still absent.
    """
    timeout = getattr(node, "_timeout", None)
    wait_for_set = getattr(timeout, "waitForSet", None)
    if not callable(wait_for_set):
        return False
    probe = ConfigSectionProbe(
        [(config.HasField, config_type.name) for config, config_type in sections]
    )
    return wait_for_set(probe, attrs=("is_set",))


def ensure_config_sections_loaded(
    node: Any,
    sections: Iterable[tuple[Any, FieldDescriptor]],
    *,
    cli_exit: Callable[[str], NoReturn],
) -> None:
    """Request missing config sections and wait once for all of them.

    Parameters
    ----------
    node : Any
        Target node whose cached configuration is validated or written next.
    sections : Iterable[tuple[Any, FieldDescriptor]]
        ``(config_root, section_field)`` pairs the caller resolved from its own
        path-specific collection. Duplicates are harmless.
    cli_exit : Callable[[str], NoReturn]
        Caller's abort seam; invoked when the requested state is still missing.
        The helper defensively raises if an injected seam returns instead of
        honoring its ``NoReturn`` contract.

    Notes
    -----
    Requests are deduplicated by ``(root.DESCRIPTOR.full_name, field.name)``.
    Sections already present via ``root.HasField(field.name)`` are skipped — a
    present default-valued section counts as loaded and is never re-read. Each
    missing section triggers exactly one ``node.requestConfig(field)`` call,
    and the whole batch shares exactly one bounded wait through the node's
    existing ``Timeout.waitForSet`` machinery (never per-section waits).
    ``noProto`` nodes are never waited on because no response can arrive
    without protocol use. On timeout the first still-missing section is named
    and the caller's ``cli_exit`` seam aborts before any validation, rendering,
    or write. Compatibility nodes without callable wait machinery are treated
    as unsatisfied and may continue only when the requested section was populated
    synchronously before the final presence check.
    """
    requested_sections: set[tuple[str, str]] = set()
    pending_sections: list[tuple[Any, FieldDescriptor]] = []
    for config, config_type in sections:
        section_key = (config.DESCRIPTOR.full_name, config_type.name)
        if section_key in requested_sections:
            continue
        requested_sections.add(section_key)
        if not config.HasField(config_type.name):
            node.requestConfig(config_type)
            pending_sections.append((config, config_type))
    # A noProto node can never deliver a config response, so waiting would only
    # burn the timeout; unloaded sections keep their fail-closed rendering there.
    if (
        pending_sections
        and not getattr(node, "noProto", False)
        and not wait_for_config_sections(node, pending_sections)
    ):
        for config, config_type in pending_sections:
            if config.HasField(config_type.name):
                continue
            cli_exit(
                _TIMEOUT_EXIT_PREFIX + f"{config_type.name}" + _TIMEOUT_EXIT_SUFFIX
            )
            # ``cli_exit`` is a NoReturn contract, but injected/downstream seams
            # occasionally return. Match the CLI runtime's defensive contract
            # guard rather than allowing validation or writes to continue.
            raise AssertionError("cli_exit returned unexpectedly") from None
