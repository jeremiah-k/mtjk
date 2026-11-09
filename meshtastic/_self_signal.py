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
from types import CodeType


def _signal_self(signum: int, *, reset_to_default: bool) -> None:
    """Best-effort: make this process die by ``signum``.

    POSIX-only. With ``reset_to_default`` the disposition for ``signum`` is
    reset to ``SIG_DFL`` first so the kernel terminates the process instead
    of a Python-level handler raising again. Returns when the platform is
    not POSIX or the delivery failed; callers keep their exit-status
    fallbacks for that case. A default fatal disposition terminates the
    process; a restored host handler can run and return. An exception raised
    by such a handler propagates to the caller — the handler belongs to the
    host that owns the disposition — while failures of the delivery calls
    themselves are silent: this helper also runs inside the forced-exit
    signal handler, where logging could deadlock on a lock held by the
    interrupted code.
    """
    if os.name != "posix":
        return
    handler_code: CodeType | None = None
    try:
        if reset_to_default:
            # A delivery that arrived just before the reset may still run the
            # previously installed handler at the next bytecode boundary, so
            # capture it before the disposition is replaced.
            handler_code = getattr(signal.getsignal(signum), "__code__", None)
            signal.signal(signum, signal.SIG_DFL)
        else:
            # With the disposition left in place, the delivered signal runs a
            # Python-level handler (e.g. the host's restored handler on the
            # re-delivery path) at the interpreter's next bytecode boundary.
            # Capture its code object so the handler below can tell a
            # handler's exception from a delivery failure.
            handler_code = getattr(signal.getsignal(signum), "__code__", None)
        os.kill(os.getpid(), signum)
    except (OSError, ValueError) as exc:
        # A Python-level handler invoked by this delivery raises at the
        # interpreter's next bytecode boundary, so its exception surfaces
        # inside this try block even though it did not come from the calls
        # above. Its traceback passes through the handler's own frame; a
        # genuine delivery failure never touches that frame. Re-raise the
        # former so a restored host handler's errors propagate to the owner
        # of that disposition instead of being masked by the caller's
        # exit-status fallback.
        tb = exc.__traceback__
        while tb is not None and (
            handler_code is None or tb.tb_frame.f_code is not handler_code
        ):
            tb = tb.tb_next
        if tb is not None:
            raise
