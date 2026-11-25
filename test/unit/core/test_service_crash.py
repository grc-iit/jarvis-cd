"""A crashed background service is reported apart from the application's result."""

import threading
import time
from typing import Any

import pytest
from jarvis_cd.core.pipeline import Pipeline


class FakeService:
    """A background service that is ready at once and may crash on cue."""

    def __init__(
        self,
        crash: threading.Event | None = None,
        before_ready: bool = False,
        after_steps: str = "auto",
    ) -> None:
        self.config: dict[str, Any] = {"after_steps": after_steps, "start_timeout": 10}
        self.env: dict[str, Any] = {}
        self.crash = crash
        self.before_ready = before_ready
        self.is_ready = False
        self.dead = threading.Event()
        self.release = threading.Event()
        self.stops = 0

    def starts_in_background(self) -> bool:
        return True

    def ready(self) -> bool | None:
        return True if self.is_ready else None

    def start(self) -> None:
        try:
            if self.before_ready:
                raise RuntimeError("segfault before ready")
            self.is_ready = True
            while not self.release.is_set():
                if self.crash is not None and self.crash.is_set():
                    raise RuntimeError("pvserver exited with signal 11")
                time.sleep(0.01)
        finally:
            self.dead.set()

    def stop(self) -> None:
        self.stops += 1
        if self.dead.is_set():
            raise RuntimeError("supervisor exited with status 1")
        self.release.set()


class FakeApp:
    """A foreground step; it can trigger the service crash and wait for it."""

    def __init__(
        self, crash: threading.Event | None = None, service: FakeService | None = None
    ) -> None:
        self.config: dict[str, Any] = {}
        self.env: dict[str, Any] = {}
        self.crash = crash
        self.service = service

    def start(self) -> None:
        if self.crash is not None and self.service is not None:
            self.crash.set()
            assert self.service.dead.wait(5)
            for thread in threading.enumerate():
                if thread.name == "jarvis-start-pkg0":
                    thread.join(5)


def _pipeline(
    monkeypatch: pytest.MonkeyPatch, instances: list[Any], states: list[tuple]
) -> Pipeline:
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.name = "crash"
    pipeline.container_image = ""
    pipeline.env = {}
    pipeline.packages = [
        {"pkg_id": f"pkg{index}", "pkg_type": f"demo.pkg{index}", "config": {}}
        for index, _ in enumerate(instances)
    ]
    by_id = {f"pkg{index}": instance for index, instance in enumerate(instances)}
    monkeypatch.setattr(pipeline, "_validate_interceptor_links", lambda: None, raising=False)
    monkeypatch.setattr(
        pipeline, "_bind_package_execution_environment", lambda pkg_def: None, raising=False
    )
    monkeypatch.setattr(
        pipeline,
        "_load_package_instance",
        lambda pkg_def, env: by_id[pkg_def["pkg_id"]],
        raising=False,
    )
    monkeypatch.setattr(
        pipeline, "_apply_interceptors_to_package", lambda instance, pkg_def: None, raising=False
    )
    monkeypatch.setattr(pipeline, "_component_hosts", lambda pkg_def: None, raising=False)
    monkeypatch.setattr(pipeline, "_stop_was_requested", lambda: False, raising=False)
    monkeypatch.setattr(pipeline, "_claim_package_stop", lambda pkg_id: True, raising=False)
    monkeypatch.setattr(
        pipeline,
        "_component_state",
        lambda pkg_def, state, hosts=None, reason=None: states.append(
            (pkg_def["pkg_id"], state, reason)
        ),
        raising=False,
    )
    return pipeline


class FakeStore:
    def __init__(self) -> None:
        self.metadata: dict[str, Any] = {}

    def update(self, execution_id: str, **fields: Any) -> None:
        self.metadata.update(fields.get("metadata") or {})


def _outcomes(pipeline: Pipeline) -> dict[str, Any]:
    store = FakeStore()
    pipeline._publish_outcomes(store, "exec-1")
    return store.metadata["outcomes"]


def test_service_crash_during_steps_does_not_fail_the_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crash = threading.Event()
    service = FakeService(crash=crash)
    app = FakeApp(crash=crash, service=service)
    states: list[tuple] = []
    pipeline = _pipeline(monkeypatch, [service, app], states)

    pipeline.start()

    outcomes = _outcomes(pipeline)
    assert outcomes["schema_version"] == "jarvis.execution.outcomes.v1"
    assert outcomes["application"] == {
        "state": "completed",
        "steps": ["pkg1"],
        "failed_package": None,
    }
    assert outcomes["services_crashed"] == ["pkg0"]
    [record] = outcomes["services"]
    assert record["outcome"] == "crashed"
    assert record["after_steps"] == "stop"
    assert record["after_ready"] is True
    assert record["during_steps"] is True
    assert "signal 11" in record["error"]
    assert ("pkg1", "done", "returned") in states
    assert states[-1][0:2] == ("pkg0", "failed")
    assert states[-1][2].startswith("service crashed:")
    # stop() still ran once so the service could clean up; its error is tolerated.
    assert service.stops == 1
    assert "pkg0" in pipeline._stopped_after_steps


def test_a_healthy_service_is_stopped_after_the_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeService()
    states: list[tuple] = []
    pipeline = _pipeline(monkeypatch, [service, FakeApp()], states)

    pipeline.start()

    outcomes = _outcomes(pipeline)
    assert outcomes["application"]["state"] == "completed"
    assert outcomes["services_crashed"] == []
    assert outcomes["services"][0]["outcome"] == "stopped"
    assert ("pkg0", "done", "stopped after the last step (after_steps)") in states


def test_a_kept_service_that_crashes_still_fails_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    crash = threading.Event()
    service = FakeService(crash=crash, after_steps="keep")
    states: list[tuple] = []
    pipeline = _pipeline(monkeypatch, [FakeApp(), service], states)
    threading.Timer(0.2, crash.set).start()

    with pytest.raises(RuntimeError, match="Background package 'pkg1' failed"):
        pipeline.start()

    outcomes = _outcomes(pipeline)
    assert outcomes["application"]["state"] == "completed"
    assert outcomes["services_crashed"] == ["pkg1"]
    assert outcomes["services"][0]["after_steps"] == "keep"
    assert outcomes["services"][0]["during_steps"] is False


def test_a_service_that_fails_before_ready_fails_the_start(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeService(before_ready=True)
    states: list[tuple] = []
    pipeline = _pipeline(monkeypatch, [service, FakeApp()], states)

    with pytest.raises(RuntimeError, match="Pipeline startup failed at package 'pkg0'"):
        pipeline.start()

    outcomes = _outcomes(pipeline)
    assert outcomes["application"]["state"] == "failed"
    assert outcomes["application"]["failed_package"] == "pkg0"
    assert states[-1][0:2] == ("pkg0", "failed")
