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

import os
import signal


def _signal_self(signum: int, *, reset_to_default: bool) -> None:
    """Best-effort: make this process die by ``signum``.

    POSIX-only. With ``reset_to_default`` the disposition for ``signum`` is
    reset to ``SIG_DFL`` first so the kernel terminates the process instead
    of a Python-level handler raising again. Returns when the platform is
    not POSIX or the delivery failed; callers keep their exit-status
    fallbacks for that case. A default fatal disposition terminates the
    process; a restored host handler can run and return. Failure is silent:
    this helper also runs inside the forced-exit signal handler, where logging
    could deadlock on a lock held by the interrupted code.
    """
    if os.name != "posix":
        return
    try:
        if reset_to_default:
            signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    except (OSError, ValueError):
        pass
