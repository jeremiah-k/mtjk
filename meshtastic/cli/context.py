"""Internal connected-CLI execution context.

This module deliberately owns no CLI command behavior. It provides the small,
explicit state contract shared by the connected action runtimes so command
handlers do not need to reach back into ``meshtastic.__main__`` globals.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, NoReturn, Protocol

from meshtastic.cli.invocation import CliInvocation, get_current_invocation
from meshtastic.cli.session_resources import (
    CliSessionResources,
    SessionCleanup,
    get_current_session_resources,
)

if TYPE_CHECKING:
    from meshtastic.mesh_interface import MeshInterface


class CliExit(Protocol):
    """Callable contract for terminating CLI execution with an optional status."""

    def __call__(self, message: str, return_value: int = 1) -> NoReturn:
        """Report *message* and terminate with *return_value*."""


def _terminate_cli(cli_exit: CliExit, message: str, return_value: int = 1) -> NoReturn:
    """Invoke a CLI exit seam and fail closed if an injected seam returns.

    Parameters
    ----------
    cli_exit : CliExit
        Exit callable supplied by the entrypoint or a test seam.
    message : str
        User-facing failure message.
    return_value : int
        Process exit status forwarded to ``cli_exit``.

    Raises
    ------
    AssertionError
        If a non-conforming injected ``cli_exit`` returns instead of terminating.

    Notes
    -----
    Production ``cli_exit`` implementations are non-returning. Keeping this guard
    in one place makes extracted action runtimes fail closed even when tests or
    downstream compatibility seams inject a returning callable.
    """
    cli_exit(message, return_value)
    raise AssertionError("cli_exit returned unexpectedly") from None


@dataclass(slots=True)
class ActionOutcome:
    """Track connection-lifecycle decisions made by connected CLI actions.

    Attributes
    ----------
    close_now : bool
        Whether the connected interface should be closed after actions finish.
    wait_for_ack_nak : bool
        Whether a remote operation expects the shared final ACK/NAK wait.
    skip_ack_wait : bool
        Whether an action owns its acknowledgment/reboot completion and the
        shared final ACK/NAK wait must be skipped.
    stop_processing : bool
        Whether action dispatch should return immediately after the current group.
    failure_cleanup_callbacks : list[Callable[[], None]]
        Rollback callbacks for resources that must survive successful dispatch but
        be released if connected action processing fails.
    interface_close_attempted : bool
        Whether dispatch has already consumed the one-shot interface-close request.
    """

    close_now: bool = False
    wait_for_ack_nak: bool = False
    skip_ack_wait: bool = False
    stop_processing: bool = False
    failure_cleanup_callbacks: list[Callable[[], None]] = field(default_factory=list)
    interface_close_attempted: bool = False


@dataclass(slots=True)
class CliContext:
    """State shared by connected CLI action handlers.

    Parameters
    ----------
    interface : MeshInterface
        Connected interface on which actions operate.
    args : argparse.Namespace
        Parsed command-line arguments for this invocation.
    get_node_kwargs : dict[str, Any]
        Keyword arguments historically forwarded to ``MeshInterface.getNode``.
    outcome : ActionOutcome
        Mutable lifecycle outcome accumulated across compatible actions.
    invocation : CliInvocation | None
        Top-level invocation state when connected dispatch runs inside ``common()``.
    session_resources : CliSessionResources | None
        Invocation owner for resources that must survive successful action dispatch.
    owns_interface : bool
        Whether dispatch may close the connection and print a connection banner.
        Embedded callers retain ownership and receive requested output through hooks.
    """

    interface: MeshInterface
    args: argparse.Namespace
    get_node_kwargs: dict[str, Any]
    outcome: ActionOutcome = field(default_factory=ActionOutcome)
    invocation: CliInvocation | None = field(default_factory=get_current_invocation)
    session_resources: CliSessionResources | None = field(
        default_factory=get_current_session_resources
    )
    owns_interface: bool = True

    def retain_failure_cleanup(
        self, cleanup: Callable[[], None]
    ) -> SessionCleanup | None:
        """Transfer one armed rollback cleanup to invocation ownership.

        When no invocation resource owner is active, the callback stays in the
        historical failure-cleanup list so direct ``onConnected()`` callers keep
        their existing lifecycle behavior.
        """
        if self.session_resources is None:
            return None
        lease = self.session_resources.register_cleanup(cleanup)
        self.outcome.failure_cleanup_callbacks.remove(cleanup)
        return lease

    @property
    def destination(self) -> str:
        """Return the selected destination address.

        Returns
        -------
        str
            Destination value from parsed CLI arguments.
        """

        return str(self.args.dest)
