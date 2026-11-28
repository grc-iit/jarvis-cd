"""Import PSI/J on every host JARVIS runs on."""

from __future__ import annotations

import signal
import sys
import types


def load_psij() -> None:
    """Make ``import psij`` work, also on hosts without ``SIGCHLD``.

    ``psij.executors.local`` installs a ``SIGCHLD`` handler at import time,
    which fails on Windows. There a placeholder module stands in, so the
    batch executors still load and only the local executor refuses.
    """
    name = "psij.executors.local"
    if not hasattr(signal, "SIGCHLD") and name not in sys.modules:
        placeholder = types.ModuleType(name)

        class LocalJobExecutor:
            def __init__(self, *args: object, **kwargs: object) -> None:
                raise RuntimeError(
                    "the PSI/J local executor needs SIGCHLD, which this host lacks")

        setattr(placeholder, "LocalJobExecutor", LocalJobExecutor)
        sys.modules[name] = placeholder
    import psij  # noqa: F401
