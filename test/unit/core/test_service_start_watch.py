"""A background start waits while it shows activity; only a stall fails it."""

import os
import threading
import time
from typing import Any, Optional

import pytest
from jarvis_cd.core.pipeline import Pipeline
from jarvis_cd.shell.core_exec import LocalExec
from jarvis_cd.shell.exec_info import LocalExecInfo
from jarvis_cd.util import stall

needs_proc = pytest.mark.skipif(not os.path.isdir("/proc/self"),
                                reason="activity is read from /proc")


class SlowService:
    def __init__(self, start_timeout: Any = 1) -> None:
        self.config: dict[str, Any] = {"start_timeout": start_timeout}
        self.env: dict[str, Any] = {}
        self.is_ready = False
        self.release = threading.Event()
        self.exec: Optional[LocalExec] = None

    def ready(self) -> Optional[bool]:
        return True if self.is_ready else None


class BusyService(SlowService):
    """Computes silently for 2.5 s (longer than its window), then is ready."""

    def start(self) -> None:
        end = time.monotonic() + 2.5
        while time.monotonic() < end:
            pass
        self.is_ready = True
        self.release.wait(30)


class StuckService(SlowService):
    """Prints one line, then waits on a process that never does anything."""

    def start(self) -> None:
        self.exec = LocalExec("echo loading model; exec sleep 60",
                              LocalExecInfo(collect_output=True, hide_output=True,
                                            exec_async=True))
        self.exec.wait("localhost")


def _start(service: SlowService) -> dict[str, Any]:
    pipeline = Pipeline.__new__(Pipeline)
    return pipeline._start_package_in_background(
        service, {"pkg_id": "svc", "pkg_type": "demo.svc"})


@needs_proc
def test_a_busy_start_runs_past_its_stall_window() -> None:
    service = BusyService(start_timeout=1)
    try:
        handle = _start(service)
    finally:
        service.release.set()
    assert handle["start_outcome"] == "ready"
    assert handle["start_watch"]["window_s"] == 1
    assert "stalled" not in handle["start_watch"]


@needs_proc
def test_a_stalled_start_fails_with_its_last_output() -> None:
    service = StuckService(start_timeout=1)
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="stalled") as raised:
            _start(service)
    finally:
        if service.exec is not None:
            service.exec.kill("localhost")
    assert time.monotonic() - started < 20
    assert "stall window 1s" in str(raised.value)
    assert "loading model" in str(raised.value)


def test_start_timeout_is_a_stall_window_with_no_fixed_default(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(stall.STALL_SECONDS_ENV, "45")
    assert Pipeline._start_stall_window(None) == 45
    assert Pipeline._start_stall_window(0) is None
    assert Pipeline._start_stall_window("120") == 120
