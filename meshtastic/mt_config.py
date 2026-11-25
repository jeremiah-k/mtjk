"""Module-level singleton namespace.

The Global object is gone, as are all its setters and getters. Instead the
module itself is the singleton namespace, which can be imported into
whichever module is used. The associated tests have also been removed,
since we now rely on built in Python mechanisms.

This is intended to make the Python read more naturally, and to make the
intention of the code clearer and more compact. It is merely a sticking
plaster over the use of shared mt_config, but the coupling issues will be dealt
with rather more easily once the code is simplified by this change.
"""

from __future__ import annotations

import argparse
import inspect
import os
import sys
import threading
import types
import warnings
from typing import IO, TYPE_CHECKING, Any

if TYPE_CHECKING:
    from meshtastic.tunnel import Tunnel

# NOTE: Any additional public annotated module-level constants that are not
# reset-managed state must either be prefixed with "_" or excluded from the
# _annotated_state filter below.
MODULE_STATE_DEFAULTS: dict[str, Any] = {
    "args": None,
    "parser": None,
    "channel_index": None,
    "logfile": None,
    "tunnel_instance": None,
    # Keep the current naming mode stable until an explicit breaking-change flip.
    "camel_case": False,
}


def reset() -> None:
    """Reset module-level state variables to their defined default values.

    Defaults are applied from MODULE_STATE_DEFAULTS to restore the module to
    its initial pristine state.

    This helper is intended for test isolation/bootstrap and is not thread-safe
    with concurrent module attribute reads/writes. Callers that need serialized
    access should coordinate around `_warned_deprecations_lock` (or another
    explicit module-wide lock) before invoking reset().

    Resetting the compatibility attribute ``tunnel_instance`` leaves the
    owning Tunnel open. Call ``Tunnel.close()`` to retire it.

    Returns
    -------
    None
        This function does not return a value.
    """
    module_globals = globals()
    for name, default in MODULE_STATE_DEFAULTS.items():
        module_globals[name] = default

    def _clear_warned_deprecations_set() -> None:
        warned_deprecations = module_globals.get("_warned_deprecations")
        if isinstance(warned_deprecations, set):
            warned_deprecations.clear()

    # reset() runs once before compatibility warn-once state is defined below,
    # so this bootstrap path must tolerate lock/set absence during import.
    warned_deprecations_lock = module_globals.get("_warned_deprecations_lock")
    if warned_deprecations_lock is not None:
        with warned_deprecations_lock:
            _clear_warned_deprecations_set()
    else:
        _clear_warned_deprecations_set()


# Declared module state managed via reset().
args: argparse.Namespace | None = None
parser: argparse.ArgumentParser | None = None
channel_index: int | None = None
logfile: IO[str] | None = None
tunnel_instance: Tunnel | None = None
camel_case: bool = False

# Sanity-check: keep MODULE_STATE_DEFAULTS keys and annotations in sync.
# Python 3.14+ can defer module annotations; inspect.get_annotations() handles
# eager and deferred annotation models consistently.
_state_keys: frozenset[str] = frozenset(MODULE_STATE_DEFAULTS)
_module_annotations: dict[str, Any] = inspect.get_annotations(
    sys.modules[__name__], eval_str=False
)
_annotated_state: frozenset[str] = frozenset(
    key
    for key in _module_annotations
    if not key.startswith("_") and key != "MODULE_STATE_DEFAULTS"
)
if not _module_annotations:
    warnings.warn(
        "inspect.get_annotations() returned no module annotations; skipping "
        "mt_config state drift validation.",
        RuntimeWarning,
        stacklevel=1,
    )
elif _state_keys != _annotated_state:
    drift_message = (
        "Drift between MODULE_STATE_DEFAULTS and type annotations — "
        f"missing annotations: {_state_keys - _annotated_state}, "
        f"missing defaults: {_annotated_state - _state_keys}"
    )
    if os.getenv("CI_ASSERT_MODULE_STATE_DRIFT", "").lower() in {
        "1",
        "true",
        "yes",
    }:
        raise AssertionError(drift_message)
    warnings.warn(drift_message, RuntimeWarning, stacklevel=1)

reset()


# ---------------------------------------------------------------------------
# Backwards compatibility aliases for renamed module state
# ---------------------------------------------------------------------------

# COMPAT_DEPRECATE: camelCase alias for tunnel_instance (warns once)
_COMPAT_ALIASES: dict[str, str] = {
    "tunnelInstance": "tunnel_instance",
}

# Track which deprecation warnings have been emitted (warn-once per process)
_warned_deprecations: set[str] = set()
_warned_deprecations_lock: threading.Lock = threading.Lock()


def _warn_compat_alias_once(old_name: str, new_name: str) -> None:
    """Emit the mt_config compatibility deprecation warning once per alias."""
    with _warned_deprecations_lock:
        if old_name in _warned_deprecations:
            return
        _warned_deprecations.add(old_name)
    # stacklevel=3 targets user callsites through __getattr__/__setattr__:
    # _warn_compat_alias_once -> accessor (__getattr__/__setattr__) -> user code.
    # If additional wrappers are introduced around these accessors, increase this.
    warnings.warn(
        f"mt_config.{old_name} is deprecated, use mt_config.{new_name} instead",
        DeprecationWarning,
        stacklevel=3,
    )


def __getattr__(name: str) -> Any:
    """Provide backwards-compatible access to renamed module attributes."""
    if name in _COMPAT_ALIASES:
        new_name = _COMPAT_ALIASES[name]
        _warn_compat_alias_once(name, new_name)
        try:
            return globals()[new_name]
        except KeyError:
            raise AttributeError(  # noqa: TRY003
                f"module {__name__!r} has no attribute {new_name!r}"
            ) from None
    raise AttributeError(  # noqa: TRY003
        f"module {__name__!r} has no attribute {name!r}"
    )


class _MtConfigModule(types.ModuleType):
    """Module subclass used to intercept deprecated compatibility assignments."""

    def __setattr__(self, name: str, value: Any) -> None:
        """Support deprecated alias assignment by redirecting writes to canonical names."""
        if name in _COMPAT_ALIASES:
            new_name = _COMPAT_ALIASES[name]
            _warn_compat_alias_once(name, new_name)
            super().__setattr__(new_name, value)
            return
        super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        """Support deprecated alias deletion by redirecting to canonical names."""
        if name in _COMPAT_ALIASES:
            new_name = _COMPAT_ALIASES[name]
            _warn_compat_alias_once(name, new_name)
            super().__delattr__(new_name)
            return
        super().__delattr__(name)


sys.modules[__name__].__class__ = _MtConfigModule
