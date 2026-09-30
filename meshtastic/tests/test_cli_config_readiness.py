"""Behavioral tests for the shared config-section readiness owner."""

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, NoReturn
from unittest.mock import MagicMock

import pytest

from meshtastic.cli.config_readiness import (
    ConfigSectionProbe,
    ensure_config_sections_loaded,
    wait_for_config_sections,
)
from meshtastic.protobuf import localonly_pb2

_LORA_FD = localonly_pb2.LocalConfig.DESCRIPTOR.fields_by_name["lora"]
_BLUETOOTH_FD = localonly_pb2.LocalConfig.DESCRIPTOR.fields_by_name["bluetooth"]
_MQTT_FD = localonly_pb2.LocalModuleConfig.DESCRIPTOR.fields_by_name["mqtt"]

_TIMEOUT_MESSAGE_TEMPLATE = (
    "ERROR: timed out waiting for the {section} configuration section "
    "from the device; no changes were made."
)


def _readiness_node(
    *,
    no_proto: bool = False,
    wait_for_set: Any = None,
) -> SimpleNamespace:
    """Return a node stub with real protobuf config wrappers.

    Parameters
    ----------
    no_proto : bool
        Whether the node should present itself as a ``noProto`` node.
    wait_for_set : Any
        Callable (or any object) exposed as ``_timeout.waitForSet``.

    Returns
    -------
    SimpleNamespace
        Node stub exposing ``localConfig``, ``moduleConfig``, ``noProto``, and
        ``_timeout`` the way the readiness owner consumes them.
    """
    return SimpleNamespace(
        localConfig=localonly_pb2.LocalConfig(),
        moduleConfig=localonly_pb2.LocalModuleConfig(),
        noProto=no_proto,
        requestConfig=MagicMock(),
        _timeout=SimpleNamespace(waitForSet=wait_for_set),
    )


def _recording_exit(calls: list[str]) -> Callable[[str], NoReturn]:
    """Return a ``cli_exit`` seam that records its message and aborts.

    Parameters
    ----------
    calls : list[str]
        List that receives each abort message before the raised exception.

    Returns
    -------
    Callable[[str], NoReturn]
        Abort seam raising ``SystemExit`` so callers cannot continue.
    """

    def _exit(message: str) -> NoReturn:
        calls.append(message)
        raise SystemExit(message)

    return _exit


@pytest.mark.unit
def test_duplicate_entries_dedup_requests_across_wrapper_roots() -> None:
    """Repeated pairs dedup per root, and both wrapper roots request once each."""
    node = _readiness_node(wait_for_set=MagicMock(return_value=True))

    ensure_config_sections_loaded(
        node,
        [
            (node.localConfig, _LORA_FD),
            (node.localConfig, _LORA_FD),
            (node.moduleConfig, _MQTT_FD),
            (node.moduleConfig, _MQTT_FD),
        ],
        cli_exit=_recording_exit([]),
    )

    assert [call.args[0].name for call in node.requestConfig.call_args_list] == [
        "lora",
        "mqtt",
    ]
    node._timeout.waitForSet.assert_called_once()


@pytest.mark.unit
def test_present_default_valued_section_skips_request_and_wait() -> None:
    """A present section with only default fields counts as loaded."""
    node = _readiness_node(wait_for_set=MagicMock(return_value=True))
    node.localConfig.lora.SetInParent()
    assert node.localConfig.lora.ListFields() == []

    ensure_config_sections_loaded(
        node,
        [(node.localConfig, _LORA_FD)],
        cli_exit=_recording_exit([]),
    )

    node.requestConfig.assert_not_called()
    node._timeout.waitForSet.assert_not_called()


@pytest.mark.unit
def test_multi_section_batch_performs_exactly_one_wait() -> None:
    """The whole batch shares one waitForSet call wired to the probe."""
    node = _readiness_node(wait_for_set=MagicMock(return_value=True))

    ensure_config_sections_loaded(
        node,
        [(node.localConfig, _LORA_FD), (node.moduleConfig, _MQTT_FD)],
        cli_exit=_recording_exit([]),
    )

    node._timeout.waitForSet.assert_called_once()
    probe = node._timeout.waitForSet.call_args.args[0]
    assert node._timeout.waitForSet.call_args.kwargs == {"attrs": ("is_set",)}
    assert probe.is_set is False
    node.localConfig.lora.SetInParent()
    assert probe.is_set is False
    node.moduleConfig.mqtt.SetInParent()
    assert probe.is_set is True


@pytest.mark.unit
def test_noproto_node_never_waits_but_still_requests() -> None:
    """NoProto nodes request missing sections but never burn the timeout."""
    node = _readiness_node(no_proto=True, wait_for_set=MagicMock(return_value=True))

    ensure_config_sections_loaded(
        node,
        [(node.localConfig, _LORA_FD)],
        cli_exit=_recording_exit([]),
    )

    node.requestConfig.assert_called_once_with(_LORA_FD)
    node._timeout.waitForSet.assert_not_called()


@pytest.mark.unit
def test_node_without_wait_machinery_proceeds_without_exit() -> None:
    """Nodes lacking waitForSet treat the wait as satisfied."""
    node = SimpleNamespace(
        localConfig=localonly_pb2.LocalConfig(),
        moduleConfig=localonly_pb2.LocalModuleConfig(),
        noProto=False,
        requestConfig=MagicMock(),
        _timeout=SimpleNamespace(),
    )
    exit_calls: list[str] = []

    ensure_config_sections_loaded(
        node,
        [(node.localConfig, _LORA_FD)],
        cli_exit=_recording_exit(exit_calls),
    )

    node.requestConfig.assert_called_once_with(_LORA_FD)
    assert exit_calls == []
    assert wait_for_config_sections(node, [(node.localConfig, _LORA_FD)]) is True


@pytest.mark.unit
def test_timeout_aborts_with_exact_message_and_nothing_after() -> None:
    """An unsatisfied wait aborts through cli_exit before any further action."""
    node = _readiness_node(wait_for_set=MagicMock(return_value=False))
    exit_calls: list[str] = []

    with pytest.raises(SystemExit) as excinfo:
        ensure_config_sections_loaded(
            node,
            [(node.localConfig, _LORA_FD)],
            cli_exit=_recording_exit(exit_calls),
        )

    assert excinfo.value.code == _TIMEOUT_MESSAGE_TEMPLATE.format(section="lora")
    assert exit_calls == [_TIMEOUT_MESSAGE_TEMPLATE.format(section="lora")]
    node.requestConfig.assert_called_once_with(_LORA_FD)


@pytest.mark.unit
def test_timeout_names_first_still_missing_section() -> None:
    """Sections that arrived during the wait are not named on timeout."""
    node = _readiness_node(wait_for_set=None)

    def timed_out_wait(probe: ConfigSectionProbe, attrs: Any) -> bool:
        assert attrs == ("is_set",)
        node.localConfig.lora.SetInParent()
        return probe.is_set

    node._timeout.waitForSet = timed_out_wait
    exit_calls: list[str] = []

    with pytest.raises(SystemExit) as excinfo:
        ensure_config_sections_loaded(
            node,
            [(node.localConfig, _LORA_FD), (node.moduleConfig, _MQTT_FD)],
            cli_exit=_recording_exit(exit_calls),
        )

    assert excinfo.value.code == _TIMEOUT_MESSAGE_TEMPLATE.format(section="mqtt")
    assert exit_calls == [_TIMEOUT_MESSAGE_TEMPLATE.format(section="mqtt")]


@pytest.mark.unit
def test_probe_reports_not_set_when_a_check_raises() -> None:
    """A section check that raises (e.g. non-message field) keeps the probe unset."""

    def raises_value_error(_name: str) -> bool:
        raise ValueError("not a message field")

    probe = ConfigSectionProbe(
        [
            (lambda name: name == "lora", "lora"),
            (raises_value_error, "device"),
        ]
    )

    assert probe.is_set is False


@pytest.mark.unit
def test_probe_reports_set_when_every_check_passes() -> None:
    """All-present sections make the probe report satisfied."""
    probe = ConfigSectionProbe([(lambda _name: True, "lora")])

    assert probe.is_set is True
