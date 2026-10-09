"""Focused tests for connected CLI dispatch lifecycle ownership."""

from __future__ import annotations

import argparse
import logging
from typing import cast
from unittest.mock import MagicMock, create_autospec

import pytest

from meshtastic.cli import dispatch
from meshtastic.cli.context import ActionOutcome, CliContext
from meshtastic.mesh_interface import MeshInterface


def _context() -> CliContext:
    """Build a minimal connected CLI context for lifecycle helper tests."""
    args = argparse.Namespace(
        ack=False,
        dest="^all",
        export_config=False,
        seriallog=False,
        wait_to_disconnect=None,
    )
    interface = create_autospec(MeshInterface, instance=True)
    interface.devPath = ""
    return CliContext(
        interface=cast(MeshInterface, interface),
        args=args,
        get_node_kwargs={},
        outcome=ActionOutcome(),
    )


@pytest.fixture
def isolated_dispatch_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub connected action entrypoints so lifecycle tests isolate dispatch itself."""
    targets = (
        (dispatch.device_actions, "_handle_device_actions"),
        (dispatch.channel_contact_actions, "_handle_contact_import"),
        (dispatch.messaging_service_actions, "_handle_messaging_actions"),
        (dispatch.configure_actions, "_handle_configure_actions"),
        (dispatch.channel_contact_actions, "_handle_channel_mutations"),
        (dispatch.messaging_service_actions, "_handle_content_reads"),
        (dispatch.channel_contact_actions, "_handle_region_preset_display"),
        (dispatch.device_actions, "_handle_lockdown_action"),
        (dispatch.messaging_service_actions, "_handle_information_actions"),
        (dispatch.channel_contact_actions, "_handle_channel_contact_display"),
        (dispatch.messaging_service_actions, "_handle_long_running_services"),
    )
    for module, name in targets:
        monkeypatch.setattr(module, name, lambda *_args, **_kwargs: None)


@pytest.mark.unit
def test_failure_cleanup_runs_in_reverse_order() -> None:
    """Unwind failure rollback resources once in reverse registration order."""
    context = _context()
    calls: list[str] = []
    context.outcome.failure_cleanup_callbacks.extend(
        [lambda: calls.append("first"), lambda: calls.append("second")]
    )

    error = dispatch._cleanup_failed_resources(context)  # noqa: SLF001

    assert error is None
    assert calls == ["second", "first"]
    assert context.outcome.failure_cleanup_callbacks == []


@pytest.mark.unit
def test_failure_cleanup_returns_first_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Continue failure rollback while retaining the first exception."""
    context = _context()
    first = RuntimeError("first")
    second = ValueError("second")

    def _raise(exc: BaseException) -> None:
        raise exc

    context.outcome.failure_cleanup_callbacks.extend(
        [lambda: _raise(second), lambda: _raise(first)]
    )

    error = dispatch._cleanup_failed_resources(context)  # noqa: SLF001

    assert error is first
    assert "Additional connected-action cleanup failed" in caplog.text
    assert context.outcome.failure_cleanup_callbacks == []


@pytest.mark.unit
@pytest.mark.parametrize("control_flow_error", [KeyboardInterrupt(), SystemExit(2)])
def test_failure_cleanup_prioritizes_control_flow_errors(
    control_flow_error: BaseException,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Operator control-flow exceptions must outrank earlier rollback failures."""
    context = _context()
    ordinary_error = RuntimeError("ordinary cleanup failure")

    def _raise(exc: BaseException) -> None:
        raise exc

    # Rollback order is reversed, so the ordinary exception is observed first.
    context.outcome.failure_cleanup_callbacks.extend(
        [lambda: _raise(control_flow_error), lambda: _raise(ordinary_error)]
    )

    error = dispatch._cleanup_failed_resources(context)  # noqa: SLF001

    assert error is control_flow_error
    assert "Additional connected-action cleanup failed" in caplog.text
    assert context.outcome.failure_cleanup_callbacks == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("dev_path", "stable_path", "expected"),
    [
        ("", None, "Connected to radio"),
        ("/dev/ttyUSB0", None, "Connected to radio on ttyUSB0"),
        (
            "/dev/ttyUSB0",
            "/dev/serial/by-id/mesh-radio",
            "Connected to radio on ttyUSB0 (stable: /dev/serial/by-id/mesh-radio)",
        ),
    ],
)
def test_print_connection_banner_variants(
    dev_path: str, stable_path: str | None, expected: str
) -> None:
    """Connection banners should describe generic, device, and stable paths."""
    context = _context()
    interface = create_autospec(MeshInterface, instance=True)
    interface.devPath = dev_path
    interface._stable_path = stable_path
    context.interface = cast(MeshInterface, interface)
    hooks = MagicMock()

    dispatch._print_connection(context, hooks)  # noqa: SLF001

    hooks.cli_print.assert_called_once_with(expected)


@pytest.mark.unit
def test_print_connection_suppresses_banner_during_export() -> None:
    """Configuration export must keep stdout free of the connection banner."""
    context = _context()
    context.args.export_config = True
    hooks = MagicMock()

    dispatch._print_connection(context, hooks)  # noqa: SLF001

    hooks.cli_print.assert_not_called()


@pytest.mark.unit
def test_dispatch_does_not_suppress_cleanup_base_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A control-flow BaseException raised during cleanup must remain observable."""
    context = _context()
    context.outcome.failure_cleanup_callbacks.append(
        MagicMock(side_effect=KeyboardInterrupt)
    )
    monkeypatch.setattr(
        dispatch,
        "_print_connection",
        MagicMock(side_effect=ValueError("primary")),
    )

    with pytest.raises(KeyboardInterrupt) as exc_info:
        dispatch._dispatch_connected(context, MagicMock())

    assert isinstance(exc_info.value.__context__, ValueError)
    assert str(exc_info.value.__context__) == "primary"
    assert context.outcome.failure_cleanup_callbacks == []


@pytest.mark.unit
def test_dispatch_preserves_primary_failure_when_cleanup_also_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cleanup failure must not replace the action failure being unwound."""
    context = _context()
    context.outcome.failure_cleanup_callbacks.append(
        MagicMock(side_effect=RuntimeError("cleanup"))
    )
    monkeypatch.setattr(
        dispatch,
        "_print_connection",
        MagicMock(side_effect=ValueError("primary")),
    )
    hooks = MagicMock()

    with pytest.raises(ValueError, match="primary"):
        dispatch._dispatch_connected(context, hooks)

    assert context.outcome.failure_cleanup_callbacks == []


@pytest.mark.unit
def test_stop_processing_still_runs_final_disconnect_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Early action termination should skip later actions but still finalize/close."""
    context = _context()
    interface = create_autospec(MeshInterface, instance=True)
    interface.devPath = ""
    context.interface = cast(MeshInterface, interface)
    context.args.wait_to_disconnect = 2
    context.outcome.close_now = True

    def _stop(_context: CliContext, _hooks: object) -> None:
        _context.outcome.stop_processing = True

    monkeypatch.setattr(dispatch.device_actions, "_handle_device_actions", _stop)
    later_action = MagicMock()
    monkeypatch.setattr(
        dispatch.messaging_service_actions,
        "_handle_messaging_actions",
        later_action,
    )
    sleep = MagicMock()
    hooks = MagicMock()
    hooks.sleep = sleep

    dispatch._dispatch_connected(context, hooks)

    later_action.assert_not_called()
    sleep.assert_called_once_with(2)
    interface.close.assert_called_once_with()


@pytest.mark.unit
def test_finalize_waits_for_ack_when_requested() -> None:
    """An explicit --ack request must trigger the shared final ACK/NAK wait."""
    context = _context()
    interface = create_autospec(MeshInterface, instance=True)
    node = interface.getNode.return_value
    context.interface = cast(MeshInterface, interface)
    context.args.ack = True
    hooks = MagicMock()

    dispatch._finalize_connected_actions(context, hooks)  # noqa: SLF001

    interface.getNode.assert_called_once_with("^all", False)
    node.iface.waitForAckNak.assert_called_once_with()


@pytest.mark.unit
def test_finalize_skip_ack_wait_takes_precedence() -> None:
    """Actions that own completion must suppress the shared ACK/NAK wait."""
    context = _context()
    interface = create_autospec(MeshInterface, instance=True)
    context.interface = cast(MeshInterface, interface)
    context.args.ack = True
    context.outcome.skip_ack_wait = True

    dispatch._finalize_connected_actions(context, MagicMock())  # noqa: SLF001

    interface.getNode.assert_not_called()


@pytest.mark.unit
def test_interface_close_failure_is_diagnostic_and_not_retried(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Keep transport-close failures diagnostic-only and consume the close request."""
    caplog.set_level(logging.DEBUG, logger=dispatch.__name__)
    context = _context()
    interface = create_autospec(MeshInterface, instance=True)
    interface.close.side_effect = RuntimeError("close failed")
    context.interface = cast(MeshInterface, interface)
    context.outcome.close_now = True

    dispatch._close_interface_if_requested(context)  # noqa: SLF001
    dispatch._close_interface_if_requested(context)  # noqa: SLF001

    interface.close.assert_called_once_with()
    assert "Error during interface close" in caplog.text


@pytest.mark.unit
def test_seriallog_active_skips_interface_close() -> None:
    """An active seriallog owns the transport; one-shot close must be skipped."""
    context = _context()
    interface = create_autospec(MeshInterface, instance=True)
    context.interface = cast(MeshInterface, interface)
    context.args.seriallog = "out.log"
    context.outcome.close_now = True

    dispatch._close_interface_if_requested(context)  # noqa: SLF001

    interface.close.assert_not_called()


@pytest.mark.unit
def test_finalize_waits_for_requested_unicast_ack() -> None:
    """A unicast operation that delegates completion must wait for ACK/NAK."""
    context = _context()
    interface = create_autospec(MeshInterface, instance=True)
    node = interface.getNode.return_value
    context.interface = cast(MeshInterface, interface)
    context.args.dest = "!12345678"
    context.outcome.wait_for_ack_nak = True

    dispatch._finalize_connected_actions(context, MagicMock())  # noqa: SLF001

    node.iface.waitForAckNak.assert_called_once_with()


@pytest.mark.unit
def test_finalize_skips_delegated_ack_wait_for_broadcast() -> None:
    """Broadcast operations must not enter the shared ACK/NAK wait."""
    context = _context()
    interface = create_autospec(MeshInterface, instance=True)
    context.interface = cast(MeshInterface, interface)
    context.outcome.wait_for_ack_nak = True

    dispatch._finalize_connected_actions(context, MagicMock())  # noqa: SLF001

    interface.getNode.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("isolated_dispatch_actions")
def test_ack_wait_failure_still_closes_one_shot_interface() -> None:
    """A failed final ACK wait must not bypass a requested interface close."""
    context = _context()
    interface = create_autospec(MeshInterface, instance=True)
    interface.devPath = ""
    interface.getNode.return_value.iface.waitForAckNak.side_effect = RuntimeError(
        "ack failed"
    )
    context.interface = cast(MeshInterface, interface)
    context.args.ack = True
    context.outcome.close_now = True
    with pytest.raises(RuntimeError, match="ack failed"):
        dispatch._dispatch_connected(context, MagicMock())

    interface.close.assert_called_once_with()


@pytest.mark.unit
def test_action_failure_after_close_request_still_closes_interface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An action failure after requesting closure must still close the interface."""
    context = _context()
    interface = create_autospec(MeshInterface, instance=True)
    interface.devPath = ""
    context.interface = cast(MeshInterface, interface)

    def _fail(action_context: CliContext, _hooks: object) -> None:
        action_context.outcome.close_now = True
        raise RuntimeError("action failed")

    monkeypatch.setattr(dispatch.device_actions, "_handle_device_actions", _fail)

    with pytest.raises(RuntimeError, match="action failed"):
        dispatch._dispatch_connected(context, MagicMock())

    interface.close.assert_called_once_with()


@pytest.mark.unit
def test_successful_dispatch_retains_started_service_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure rollback callbacks must not stop services after successful dispatch."""
    context = _context()
    rollback = MagicMock()

    def _register(action_context: CliContext, _hooks: object) -> None:
        action_context.outcome.failure_cleanup_callbacks.append(rollback)
        action_context.outcome.stop_processing = True

    monkeypatch.setattr(dispatch.device_actions, "_handle_device_actions", _register)

    dispatch._dispatch_connected(context, MagicMock())

    rollback.assert_not_called()
    assert context.outcome.failure_cleanup_callbacks == []
