"""Process-output bridge for the WRF tropical-cyclone package."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


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


def read_wrf_diagnostic(case_root: Path, *, max_bytes: int = 8192) -> str:
    """Read bounded tails of WRF rank-zero diagnostics from one case directory."""

    if not 1 <= max_bytes <= 16384:
        raise ValueError("max_bytes must be between 1 and 16384")
    resolved_root = case_root.resolve(strict=True)
    names = ("rsl.error.0000", "rsl.out.0000")
    per_file = max(1, max_bytes // len(names))
    sections: list[str] = []
    for name in names:
        path = resolved_root / name
        if not path.is_file():
            continue
        try:
            payload = path.read_bytes()
        except OSError as error:
            raise RuntimeError(f"failed to read WRF diagnostic: {name}") from error
        tail = payload[-per_file:].decode("utf-8", errors="replace")
        sections.append(f"[{name}]\n{tail}")
    return "\n".join(sections)


__all__ = ["DeferredRuntimeCallback", "read_wrf_diagnostic"]
