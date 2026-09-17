"""Unit tests for the in-tree DotMap implementation in meshtastic.test."""

from __future__ import annotations

import pytest

from meshtastic.test import DotMap, _FallbackDotMap


@pytest.mark.unit
def test_dotmap_autovivifies_missing_nested_keys() -> None:
    """Missing attribute chains should persist newly created child maps."""
    dmap = DotMap()

    dmap.alpha.beta = 1

    assert isinstance(dmap["alpha"], DotMap)
    assert dmap["alpha"]["beta"] == 1


@pytest.mark.unit
def test_dotmap_wraps_existing_dict_and_persists_wrapper() -> None:
    """Existing dict values should be wrapped once and stored back for subsequent writes."""
    dmap = DotMap({"config": {"threshold": 5}})

    wrapped = dmap.config
    wrapped.enabled = True

    assert isinstance(wrapped, DotMap)
    assert dmap["config"] is wrapped
    assert dmap["config"]["threshold"] == 5
    assert dmap["config"]["enabled"] is True


@pytest.mark.unit
def test_dotmap_autovivifies_three_levels() -> None:
    """Deep nested autovivification should persist intermediate maps."""
    dmap = DotMap()

    dmap.a.b.c = 42

    assert isinstance(dmap["a"]["b"], DotMap)
    assert dmap["a"]["b"]["c"] == 42


@pytest.mark.unit
def test_dotmap_dunder_guard_raises_attribute_error() -> None:
    """Dunder attribute access should be blocked to match safety expectations."""
    dmap = DotMap()

    with pytest.raises(AttributeError, match="__foo__"):
        _ = dmap.__foo__


@pytest.mark.unit
def test_dotmap_delattr_and_missing_access() -> None:
    """Deleting a present key should remove it and missing delete should raise."""
    dmap = DotMap()
    dmap.x = 1

    delattr(dmap, "x")
    assert "x" not in dmap

    with pytest.raises(AttributeError, match="x"):
        delattr(dmap, "x")


@pytest.mark.unit
def test_dotmap_scalar_readback() -> None:
    """Scalar values should be returned directly, not wrapped in maps."""
    dmap = DotMap()
    dmap.x = 42

    assert dmap.x == 42
    assert not isinstance(dmap.x, DotMap)


@pytest.mark.unit
def test_dotmap_dunder_setattr_delegates_to_object() -> None:
    """Dunder setattr should delegate to object attributes, not dict entries."""
    dmap = DotMap()

    dmap.__custom__ = 1

    assert "__custom__" not in dmap


@pytest.mark.unit
def test_fallback_dotmap_alias_remains_the_canonical_class() -> None:
    """The historical _FallbackDotMap name should stay importable and identical."""
    assert _FallbackDotMap is DotMap

    dmap = _FallbackDotMap()
    dmap.alpha.beta = 2

    assert dmap["alpha"]["beta"] == 2
