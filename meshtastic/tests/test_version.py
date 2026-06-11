"""Tests for package-version resolution and fork-aware update checks."""

import warnings
from importlib.metadata import PackageNotFoundError
from typing import Protocol

import pytest
import requests

import meshtastic.util as util_module
import meshtastic.version as version_module


class PackageNotPublishedError(requests.RequestException):
    """Simulated PyPI lookup failure for unpublished distribution names."""


class ResponseLike(Protocol):
    """Small response protocol used by version-check tests."""

    def json(self) -> dict[str, dict[str, str]]:
        """Return a mapping containing the PyPI version payload."""
        ...  # pylint: disable=unnecessary-ellipsis


def _make_fake_response(version: str) -> ResponseLike:
    """Create a minimal fake response object for PyPI version checks."""

    class _FakeResponse:
        """Stub response payload for the PyPI version endpoint."""

        def json(self) -> dict[str, dict[str, str]]:
            """Return fake PyPI response JSON."""
            return {"info": {"version": version}}

    fake_response: ResponseLike = _FakeResponse()
    return fake_response


def _fake_installed_mtjk_version(distribution_name: str) -> str:
    """Return a fake installed version for mtjk and raise otherwise."""
    if distribution_name == "mtjk":
        return "2.7.8"
    raise PackageNotFoundError


@pytest.mark.unit
def test_get_active_version_prefers_mtjk(monkeypatch: pytest.MonkeyPatch) -> None:
    """The active version lookup should prefer the fork distribution name."""
    monkeypatch.setattr(version_module, "version", _fake_installed_mtjk_version)
    assert version_module.get_active_version() == "2.7.8"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert version_module.getActiveVersion() == "2.7.8"
    assert not [
        warning
        for warning in caught
        if issubclass(warning.category, DeprecationWarning)
    ]


@pytest.mark.unit
def test_get_active_version_falls_back_to_meshtastic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The active version lookup should fall back to upstream distribution name."""

    def _fake_version(distribution_name: str) -> str:
        if distribution_name == "meshtastic":
            return "2.7.8"
        raise PackageNotFoundError

    monkeypatch.setattr(version_module, "version", _fake_version)
    assert version_module.get_active_version() == "2.7.8"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert version_module.getActiveVersion() == "2.7.8"
    assert not [
        warning
        for warning in caught
        if issubclass(warning.category, DeprecationWarning)
    ]


@pytest.mark.unit
def test_get_active_version_returns_unknown_when_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Version lookup should return unknown when no distribution metadata is present."""

    def _fake_version(distribution_name: str) -> str:
        _ = distribution_name
        raise PackageNotFoundError

    monkeypatch.setattr(version_module, "version", _fake_version)
    assert version_module.get_active_version() == "unknown"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert version_module.getActiveVersion() == "unknown"
    assert not [
        warning
        for warning in caught
        if issubclass(warning.category, DeprecationWarning)
    ]


@pytest.mark.unit
def test_check_if_newer_version_checks_only_mtjk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PyPI checks should only query the fork's own package name."""

    calls: list[str] = []

    def _fake_get(url: str, timeout: float) -> object:
        _ = timeout
        calls.append(url)
        return _make_fake_response("2.7.9")

    monkeypatch.setattr("meshtastic.util.requests.get", _fake_get)
    monkeypatch.setattr(version_module, "version", _fake_installed_mtjk_version)

    assert util_module.check_if_newer_version() == "2.7.9"
    assert calls == [
        "https://pypi.org/pypi/mtjk/json",
    ]


@pytest.mark.unit
def test_check_if_newer_version_returns_none_on_pypi_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PyPI checks should return None when the fork package is not found."""

    def _fake_get(url: str, timeout: float) -> object:
        _ = (url, timeout)
        raise PackageNotPublishedError

    monkeypatch.setattr("meshtastic.util.requests.get", _fake_get)
    monkeypatch.setattr(version_module, "version", _fake_installed_mtjk_version)

    assert util_module.check_if_newer_version() is None


@pytest.mark.unit
def test_check_if_newer_version_returns_none_when_not_newer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PyPI checks should return None when the fetched version is not newer."""

    def _fake_get(url: str, timeout: float) -> object:
        _ = (url, timeout)
        return _make_fake_response("2.7.8")

    monkeypatch.setattr("meshtastic.util.requests.get", _fake_get)
    monkeypatch.setattr(version_module, "version", _fake_installed_mtjk_version)

    assert util_module.check_if_newer_version() is None
