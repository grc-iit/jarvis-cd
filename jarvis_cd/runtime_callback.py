"""Process-output callback ownership for multi-phase JARVIS packages."""

from __future__ import annotations

from typing import Callable, cast


class RuntimePhaseLineCallback:
    """Bind one shared runtime callback to an intermediate or terminal process.

    Multi-process packages must not finalize their package-level progress and
    artifact providers after each successful process. Intermediate phases keep
    the shared callback open on success, while any failed phase and the declared
    terminal phase preserve the normal process-owned finalization semantics.
    """

    def __init__(
        self,
        delegate: Callable[[str, str], None],
        *,
        terminal: bool,
    ) -> None:
        self._delegate = delegate
        self._terminal = terminal

    def __call__(self, stream_name: str, line: str) -> None:
        """Forward one captured process-output line to the shared callback."""
        self._delegate(stream_name, line)

    def finalize_process(self, return_code: int) -> None:
        """Finalize only a failed phase or the declared terminal phase."""
        if not self._terminal and return_code == 0:
            return
        process_finalizer = getattr(self._delegate, "finalize_process", None)
        finalizer = getattr(self._delegate, "finalize", None)
        if callable(process_finalizer):
            cast(Callable[[int], None], process_finalizer)(return_code)
        elif callable(finalizer):
            cast(Callable[[], None], finalizer)()

    def reconcile_process_exit(self, return_code: int) -> None:
        """Forward effective nonzero-exit reconciliation to the shared callback."""
        reconciler = getattr(self._delegate, "reconcile_process_exit", None)
        if callable(reconciler):
            cast(Callable[[int], None], reconciler)(return_code)
