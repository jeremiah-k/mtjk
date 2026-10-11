"""Tests for invocation-scoped CLI state and legacy mirroring."""

from __future__ import annotations

import argparse
from io import StringIO
from unittest.mock import patch

import pytest

import meshtastic.__main__ as main_module
import meshtastic.cli.bootstrap as cli_bootstrap
from meshtastic import mt_config
from meshtastic.cli.invocation import (
    CliInvocation,
    activate_invocation,
    get_current_invocation,
)


def _invocation(
    *, channel_index: int | None = None, camel_case: bool = False
) -> CliInvocation:
    """Build one minimal invocation state for unit tests."""
    return CliInvocation(
        args=argparse.Namespace(quiet=False),
        parser=argparse.ArgumentParser(add_help=False),
        channel_index=channel_index,
        camel_case=camel_case,
    )


@pytest.mark.unit
def test_activate_invocation_restores_outer_context() -> None:
    """Nested invocation activation should restore the previous context exactly."""
    outer = _invocation(channel_index=1)
    inner = _invocation(channel_index=2)

    assert get_current_invocation() is None
    with activate_invocation(outer):
        assert get_current_invocation() is outer
        with activate_invocation(inner):
            assert get_current_invocation() is inner
        assert get_current_invocation() is outer
    assert get_current_invocation() is None


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_invocation_mutations_mirror_legacy_mt_config() -> None:
    """Internal invocation ownership must preserve historical module-global reads."""
    invocation = _invocation(channel_index=3)
    logfile = StringIO()

    with activate_invocation(invocation):
        main_module._set_current_channel_index(7)
        main_module._set_current_logfile(logfile)

        assert main_module._current_channel_index() == 7
        assert invocation.channel_index == 7
        assert mt_config.channel_index == 7
        assert invocation.logfile is logfile
        assert mt_config.logfile is logfile

        main_module._clear_session_logfile(logfile)
        assert invocation.logfile is None
        assert mt_config.logfile is None


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_invocation_state_precedes_legacy_fallback() -> None:
    """Active invocation values should win without changing direct-call fallback behavior."""
    mt_config.camel_case = False
    mt_config.channel_index = 4
    invocation = _invocation(channel_index=8, camel_case=True)

    with activate_invocation(invocation):
        assert main_module._current_camel_case() is True
        assert main_module._current_channel_index() == 8

    assert main_module._current_camel_case() is False
    assert main_module._current_channel_index() == 4


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_common_activates_invocation_only_for_bootstrap_call() -> None:
    """The compatibility entrypoint should scope explicit state around bootstrap execution."""
    args = argparse.Namespace()
    parser = argparse.ArgumentParser(add_help=False)
    mt_config.args = args
    mt_config.parser = parser
    mt_config.channel_index = 5
    mt_config.camel_case = True
    seen: list[CliInvocation] = []

    def _run_common(
        actual_args: argparse.Namespace,
        actual_parser: argparse.ArgumentParser,
        _hooks: object,
    ) -> None:
        assert actual_args is args
        assert actual_parser is parser
        current = get_current_invocation()
        assert current is not None
        seen.append(current)
        assert current.channel_index == 5
        assert current.camel_case is True

    with patch.object(cli_bootstrap, "run_common", side_effect=_run_common):
        main_module.common()

    assert len(seen) == 1
    assert get_current_invocation() is None
