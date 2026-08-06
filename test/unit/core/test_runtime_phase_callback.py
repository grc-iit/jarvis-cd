"""Tests for multi-phase package runtime callback ownership."""

from __future__ import annotations

from jarvis_cd.runtime_callback import RuntimePhaseLineCallback


class _RecordingCallback:
    """Record callback forwarding and terminal lifecycle operations."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []
        self.finalized: list[int] = []
        self.reconciled: list[int] = []

    def __call__(self, stream_name: str, line: str) -> None:
        """Record one forwarded output line."""
        self.lines.append((stream_name, line))

    def finalize_process(self, return_code: int) -> None:
        """Record one delegated process finalization."""
        self.finalized.append(return_code)

    def reconcile_process_exit(self, return_code: int) -> None:
        """Record one delegated effective-exit reconciliation."""
        self.reconciled.append(return_code)


def test_runtime_phase_callback_defers_only_successful_intermediate_exit() -> None:
    """One callback can span processes without hiding an intermediate failure."""
    delegate = _RecordingCallback()
    intermediate = RuntimePhaseLineCallback(delegate, terminal=False)
    terminal = RuntimePhaseLineCallback(delegate, terminal=True)

    intermediate("stdout", "generating\n")
    intermediate.finalize_process(0)
    terminal("stdout", "training\n")
    terminal.finalize_process(0)

    assert delegate.lines == [
        ("stdout", "generating\n"),
        ("stdout", "training\n"),
    ]
    assert delegate.finalized == [0]

    failed_delegate = _RecordingCallback()
    failed_intermediate = RuntimePhaseLineCallback(failed_delegate, terminal=False)
    failed_intermediate.finalize_process(9)
    failed_intermediate.reconcile_process_exit(9)

    assert failed_delegate.finalized == [9]
    assert failed_delegate.reconciled == [9]
