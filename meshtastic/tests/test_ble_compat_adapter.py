"""Behavioral tests for structural BLE compatibility dispatch."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from meshtastic.interfaces.ble.compat_adapter import (
    _get_declared_callable,
    _get_declared_lock,
    _get_declared_member,
    _iter_declared_callables,
    _iter_declared_members,
    _resolve_declared_callable,
    _resolve_declared_member,
)

pytestmark = pytest.mark.unit


class _DynamicProxy:
    """Collaborator that synthesizes undeclared attributes dynamically."""

    def __getattr__(self, name: str) -> str:
        return f"dynamic:{name}"


class _DescriptorWithDynamicFallback:
    """Declared descriptor whose failure would normally trigger ``__getattr__``."""

    @property
    def current(self) -> str:
        raise AttributeError("descriptor unavailable")

    def __getattr__(self, name: str) -> str:
        return f"dynamic:{name}"


class _DescriptorOwner:
    """Collaborator exposing a real descriptor-backed compatibility member."""

    @property
    def current(self) -> str:
        return "descriptor-value"


class _CustomAttributeOwner:
    """Collaborator with declared members mediated by ``__getattribute__``."""

    current = "class-default"

    def __getattribute__(self, name: str) -> object:
        if name == "current":
            return "custom-value"
        return super().__getattribute__(name)


def test_declared_member_ignores_dynamic_fallback() -> None:
    """Compatibility lookup must not treat ``__getattr__`` output as a contract."""
    assert _get_declared_member(_DynamicProxy(), "missing") is None


def test_declared_member_does_not_fall_through_to_dynamic_proxy() -> None:
    """Descriptor failure must not re-enable a synthesized ``__getattr__`` value."""
    assert _get_declared_member(_DescriptorWithDynamicFallback(), "current") is None


def test_declared_member_preserves_descriptor_semantics() -> None:
    """Static declaration detection should still evaluate real descriptors."""
    assert _get_declared_member(_DescriptorOwner(), "current") == "descriptor-value"


def test_declared_member_preserves_custom_attribute_access() -> None:
    """Lookup should honor an object's declared ``__getattribute__`` semantics."""
    assert _get_declared_member(_CustomAttributeOwner(), "current") == "custom-value"


def test_resolve_member_prefers_current_then_legacy_and_skips_none() -> None:
    """Current names win, while explicit ``None`` permits legacy fallback."""
    owner = SimpleNamespace(current="new", legacy="old")
    assert _resolve_declared_member(owner, "current", "legacy") == "new"

    owner.current = None
    assert _resolve_declared_member(owner, "current", "legacy") == "old"


def test_resolve_callable_ignores_noncallable_current_member() -> None:
    """A legacy callable remains usable when the preferred member is data."""
    legacy = lambda: "legacy"  # noqa: E731 - compact callable identity for assertion
    owner = SimpleNamespace(current=False, legacy=legacy)

    assert _resolve_declared_callable(owner, "current", "legacy") is legacy
    assert _get_declared_callable(owner, "current") is None


def test_iter_members_preserves_precedence_and_filters_missing_values() -> None:
    """Iteration should expose only declared, non-``None`` compatibility members."""
    owner = SimpleNamespace(first=1, second=None, third=3)

    assert list(_iter_declared_members(owner, "first", "second", "third")) == [
        ("first", 1),
        ("third", 3),
    ]


def test_iter_callables_preserves_names_for_failure_reporting() -> None:
    """Callable iteration should retain member names for policy-aware logging."""
    first = lambda: 1  # noqa: E731 - compact callable identity for assertion
    third = lambda: 3  # noqa: E731 - compact callable identity for assertion
    owner = SimpleNamespace(first=first, second=2, third=third)

    assert list(_iter_declared_callables(owner, "first", "second", "third")) == [
        ("first", first),
        ("third", third),
    ]


def test_declared_lock_accepts_real_context_manager_and_rejects_dynamic_only() -> None:
    """Lock resolution should require an explicitly declared context-manager contract."""
    import threading

    lock = threading.RLock()
    assert _get_declared_lock(SimpleNamespace(lock=lock), "lock") is lock

    class _DynamicLockOwner:
        def __getattr__(self, _name: str) -> object:
            return _DynamicProxy()

    assert _get_declared_lock(_DynamicLockOwner(), "lock") is None


def test_declared_lock_rejects_instance_only_context_methods() -> None:
    """Lock validation should match Python's type-level special-method lookup."""

    class _InstanceOnlyContextManager:
        def __init__(self) -> None:
            self.__enter__ = lambda: self
            self.__exit__ = lambda *_args: None

    candidate = _InstanceOnlyContextManager()
    owner = SimpleNamespace(lock=candidate)

    assert _get_declared_lock(owner, "lock") is None
    # 3.10 raises the raw AttributeError from the type-level special-method
    # lookup; 3.11+ wraps it into a TypeError.
    with pytest.raises((TypeError, AttributeError)):
        with candidate:
            pass
