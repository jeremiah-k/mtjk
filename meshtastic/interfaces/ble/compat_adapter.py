"""Structural compatibility dispatch for BLE collaborators.

The BLE implementation preserves a number of historical public/underscore
member pairs.  Resolution belongs in one place so production code does not
need to know how dynamic proxies, partial collaborators, or legacy spellings
are represented.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator
from importlib import import_module
from types import ModuleType
from typing import Any, TypeVar, cast

from meshtastic.interfaces.ble.ports import _LockPort

T = TypeVar("T")
_MISSING = object()


def _load_runtime_module(module_name: str) -> ModuleType:
    """Load a runtime dependency lazily to preserve circular-import boundaries.

    Lazy resolution is intentional for BLE compatibility modules because tests and
    downstream integrations may monkeypatch module-level shims after import.
    """
    return import_module(module_name)


def _get_declared_member(
    target: object | None,
    name: str,
    default: T | None = None,
) -> object | T | None:
    """Return an explicitly declared member without dynamic fallback.

    Parameters
    ----------
    target : object | None
        Object to inspect.
    name : str
        Attribute name to resolve.
    default : T | None
        Value returned when the member is not explicitly declared.

    Returns
    -------
    object | T | None
        The resolved member or ``default``.

    Notes
    -----
    ``inspect.getattr_static`` is used only to establish declaration.  The
    subsequent direct ``__getattribute__`` call preserves the target type's
    declared access semantics while deliberately bypassing dynamic ``__getattr__``
    synthesis, including when a declared descriptor itself raises
    ``AttributeError``.
    """
    if target is None:
        return default
    try:
        inspect.getattr_static(target, name)
    except AttributeError:
        return default
    try:
        resolved: object | T | None = type(  # pylint: disable=unnecessary-dunder-call
            target
        ).__getattribute__(target, name)
    except AttributeError:
        return default
    return resolved


def _get_declared_callable(
    target: object | None,
    name: str,
) -> Callable[..., Any] | None:
    """Return an explicitly declared callable member when available."""
    candidate = _get_declared_member(target, name)
    return cast(Callable[..., Any], candidate) if callable(candidate) else None


def _iter_declared_members(
    target: object | None,
    *names: str,
) -> Iterator[tuple[str, object]]:
    """Yield explicitly declared non-``None`` members in precedence order."""
    for name in names:
        candidate = _get_declared_member(target, name, _MISSING)
        if candidate is not _MISSING and candidate is not None:
            yield name, candidate


def _iter_declared_callables(
    target: object | None,
    *names: str,
) -> Iterator[tuple[str, Callable[..., Any]]]:
    """Yield explicitly declared callable members in precedence order."""
    for name, candidate in _iter_declared_members(target, *names):
        if callable(candidate):
            yield name, cast(Callable[..., Any], candidate)


def _resolve_declared_member(
    target: object | None,
    *names: str,
    default: T | None = None,
) -> object | T | None:
    """Resolve the first non-``None`` explicitly declared compatibility member.

    Parameters
    ----------
    target : object | None
        Collaborator exposing current and/or legacy members.
    *names : str
        Member names in precedence order.
    default : T | None
        Fallback returned when no usable member exists.

    Returns
    -------
    object | T | None
        First declared non-``None`` member or ``default``.
    """
    for _name, candidate in _iter_declared_members(target, *names):
        return candidate
    return default


def _resolve_declared_callable(
    target: object | None,
    *names: str,
) -> Callable[..., Any] | None:
    """Resolve the first explicitly declared callable in precedence order."""
    for _name, candidate in _iter_declared_callables(target, *names):
        return candidate
    return None


def _get_declared_lock(
    target: object | None,
    name: str,
) -> _LockPort | None:
    """Return a declared context-manager lock member when available."""
    candidate = _get_declared_member(target, name)
    if candidate is None:
        return None
    candidate_type = type(candidate)
    if (
        _get_declared_callable(candidate_type, "__enter__") is None
        or _get_declared_callable(candidate_type, "__exit__") is None
    ):
        return None
    return cast(_LockPort, candidate)
