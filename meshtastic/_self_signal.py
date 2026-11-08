"""Process-level fatal-signal delivery seam for CLI entrypoints.

The CLI's self-kill paths — the TERM/HUP re-delivery after graceful
teardown, the second-delivery forced exit, and the SIGINT re-delivery after
a Ctrl+C teardown — all deliver through :func:`_signal_self`. The module is
deliberately import-cheap (stdlib only, no ``meshtastic`` imports) so test
conftests can replace the function without importing the CLI stack, and so
replacing it cannot create import-order effects: it registers nothing and
mutates nothing at import time.
"""

from __future__ import annotations

import logging
import os
import signal

logger = logging.getLogger(__name__)


def _signal_self(signum: int, *, reset_to_default: bool) -> None:
    """Best-effort: make this process die by ``signum``.

    POSIX-only. With ``reset_to_default`` the disposition for ``signum`` is
    reset to ``SIG_DFL`` first so the kernel terminates the process instead
    of a Python-level handler raising again. Returns when the platform is
    not POSIX or the delivery failed; callers keep their exit-status
    fallbacks for that case. When delivery succeeds the process dies inside
    this call and the caller never resumes.
    """
    if os.name != "posix":
        return
    try:
        if reset_to_default:
            signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    except (OSError, ValueError):
        logger.debug("Could not deliver signal %s to self", signum, exc_info=True)
