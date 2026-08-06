"""Process-output bridge for the multi-run LBM-CFD package."""

from __future__ import annotations

from collections.abc import Callable


class DeferredRuntimeCallback:
    """Forward lines while deferring success until the terminal command."""

    def __init__(
        self,
        delegate: Callable[[str, str], None] | None,
        *,
        terminal: bool,
    ) -> None:
        self._delegate = delegate
        self._terminal = terminal

    def __call__(self, stream_name: str, line: str) -> None:
        if self._delegate is not None:
            self._delegate(stream_name, line)

    def finalize_process(self, return_code: int) -> None:
        if self._delegate is not None and (self._terminal or return_code != 0):
            finalizer = getattr(self._delegate, "finalize_process", None)
            if callable(finalizer):
                finalizer(return_code)

    def reconcile_process_exit(self, return_code: int) -> None:
        if self._delegate is not None:
            reconciler = getattr(self._delegate, "reconcile_process_exit", None)
            if callable(reconciler):
                reconciler(return_code)


__all__ = ["DeferredRuntimeCallback"]
