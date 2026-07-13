"""Tests for JARVIS-owned generic package progress semantics."""

from __future__ import annotations

import io
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from jarvis_cd.core.pkg import Pkg
from jarvis_cd.shell.core_exec import LocalExec
from jarvis_cd.shell.exec_info import LocalExecInfo
from jarvis_cd.progress import (
    PROGRESS_LINE_PREFIX,
    ProgressEvent,
    ProgressObservation,
    ProgressReporter,
    ProgressState,
    ProgressStore,
    event_from_progress_line,
    provider_from_package,
)


def _event(*, sequence: int = 1) -> ProgressEvent:
    return ProgressEvent(
        package_name="site.application",
        package_id="simulation_a",
        execution_id="exec_123",
        label="iteration",
        current=2,
        total=10,
        unit="iteration",
        message="Completed iteration 2 of 10",
        sequence=sequence,
        metadata={"source": "application"},
    )


def test_progress_event_round_trip_preserves_instance_identity() -> None:
    """The schema distinguishes a package type from its pipeline alias."""
    event = _event()

    restored = ProgressEvent.from_json(event.to_json())

    assert restored == event
    assert restored.determinate
    assert restored.as_adapter_record()["metadata"]["package_id"] == "simulation_a"


def test_progress_event_rejects_unknown_duplicate_and_nonfinite_fields() -> None:
    """Untrusted JSON cannot smuggle ambiguous or non-finite progress."""
    payload = _event().as_dict()
    payload["unexpected"] = True
    with pytest.raises(ValueError, match="unknown progress event fields"):
        ProgressEvent.from_dict(payload)

    duplicate = (
        _event()
        .to_json()
        .replace(
            '"package_id":"simulation_a"',
            '"package_id":"simulation_a","package_id":"other"',
        )
    )
    with pytest.raises(ValueError, match="not valid JSON"):
        ProgressEvent.from_json(duplicate)

    payload = _event().as_dict()
    payload["current"] = float("nan")
    with pytest.raises(ValueError, match="finite"):
        ProgressEvent.from_dict(payload)

    with pytest.raises(ValueError, match="non-negative integer"):
        replace(_event(), sequence=cast(Any, 1.5))
    with pytest.raises(ValueError, match="ProgressState"):
        replace(_event(), state=cast(Any, "running"))


def test_indeterminate_event_never_invents_a_total() -> None:
    """Lifecycle readiness remains truthful rather than a made-up percentage."""
    event = ProgressEvent(
        package_name="builtin.paraview",
        package_id="visualizer",
        execution_id="exec_ready",
        label="pvserver",
        state=ProgressState.READY,
        sequence=1,
        message="Ready for a client",
    )

    assert not event.determinate
    assert "current" not in event.as_dict()
    assert "total" not in event.as_dict()


def test_reporter_emits_stdout_and_owned_sidecar(tmp_path: Path) -> None:
    """The same event schema works over structured lines and durable JSONL."""
    stream = io.StringIO()
    reporter = ProgressReporter(
        package_name="site.application",
        package_id="app_a",
        execution_id="exec_line",
        stream=stream,
    )
    emitted = reporter.emit(label="phase", message="Application is running")
    assert stream.getvalue().startswith(PROGRESS_LINE_PREFIX)
    assert event_from_progress_line(stream.getvalue()) == emitted

    path = tmp_path / "owned" / "progress.jsonl"
    sidecar = ProgressReporter(
        package_name="site.application",
        package_id="app_b",
        execution_id="exec_file",
        path=path,
    )
    event = sidecar.emit(label="item", current=1, total=2, unit="item")

    assert ProgressStore(path).latest() == event
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0


def test_reporter_honors_stdout_transport_inside_container(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A container reporter emits framed stdout instead of opening a host path."""
    sidecar = tmp_path / "unmounted" / "progress.jsonl"
    stream = io.StringIO()
    monkeypatch.setenv("JARVIS_PROGRESS_PATH", str(sidecar))
    monkeypatch.setenv("JARVIS_PROGRESS_TRANSPORT", "stdout")

    reporter = ProgressReporter(
        package_name="site.application",
        package_id="container_app",
        execution_id="exec_container",
        stream=stream,
    )
    reporter.emit(label="phase", message="Container phase completed")

    assert stream.getvalue().startswith(PROGRESS_LINE_PREFIX)
    assert not sidecar.exists()


def test_store_rejects_sequence_replay_and_symlink(tmp_path: Path) -> None:
    """Durable query fails closed on ambiguous ordering and redirected files."""
    path = tmp_path / "progress.jsonl"
    store = ProgressStore(path)
    store.append(_event(sequence=1))
    with pytest.raises(ValueError, match="increase strictly"):
        store.append(_event(sequence=1))

    if hasattr(os, "symlink"):
        target = tmp_path / "target.jsonl"
        target.write_text(_event().to_json() + "\n", encoding="utf-8")
        link = tmp_path / "redirected.jsonl"
        try:
            link.symlink_to(target)
        except OSError:
            # Unprivileged Windows commonly disables symlink creation. The
            # sequence-replay assertion above still exercises fail-closed
            # durable reads on that platform.
            return
        with pytest.raises(ValueError, match="symlink"):
            ProgressStore(link).read_all()


def test_store_rejects_junction_ancestor_without_writing(tmp_path: Path) -> None:
    """Windows junctions and POSIX symlinks cannot redirect a sidecar root."""
    target = tmp_path / "outside"
    target.mkdir()
    redirected = tmp_path / "redirected"
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(redirected))
    else:
        redirected.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="redirected"):
        ProgressStore(redirected / "created" / "progress.jsonl").append(_event())
    assert not (target / "created").exists()


def test_store_rejects_identity_changes_and_incomplete_framing(
    tmp_path: Path,
) -> None:
    """One sidecar cannot mix identities or append after a torn JSONL record."""
    path = tmp_path / "progress.jsonl"
    store = ProgressStore(path)
    store.append(_event(sequence=1))
    with pytest.raises(ValueError, match="identity must remain stable"):
        store.append(replace(_event(sequence=2), execution_id="different_execution"))

    path.write_text(_event(sequence=1).to_json(), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    with pytest.raises(ValueError, match="incomplete final JSONL record"):
        store.append(_event(sequence=2))
    with pytest.raises(ValueError, match="incomplete final JSONL record"):
        store.read_all()


def test_store_reader_rejects_preexisting_mixed_identity(tmp_path: Path) -> None:
    """A tampered or legacy mixed-identity stream fails closed on query."""
    path = tmp_path / "progress.jsonl"
    first = _event(sequence=1)
    second = replace(first, sequence=2, package_id="different_package")
    path.write_text(
        first.to_json() + "\n" + second.to_json() + "\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        path.chmod(0o600)

    with pytest.raises(ValueError, match="identity must remain stable"):
        ProgressStore(path).read_all()


def test_reporter_adopts_only_a_matching_existing_stream(tmp_path: Path) -> None:
    """Reporter sequence adoption cannot cross execution or package identity."""
    path = tmp_path / "progress.jsonl"
    ProgressStore(path).append(_event(sequence=1))

    reporter = ProgressReporter(
        package_name="site.application",
        package_id="simulation_a",
        execution_id="exec_123",
        path=path,
    )
    emitted = reporter.emit(label="iteration", current=3, total=10)
    assert emitted.sequence == 2

    with pytest.raises(ValueError, match="does not match this reporter"):
        ProgressReporter(
            package_name="site.application",
            package_id="simulation_a",
            execution_id="different_execution",
            path=path,
        )


def test_package_binding_uses_owned_execution_environment(tmp_path: Path) -> None:
    """Packages receive identity and sidecar paths from execution core only."""
    package = object.__new__(Pkg)
    package.pkg_type = "site.application"
    package.pkg_id = "application_a"
    package.env = {}
    package.mod_env = {}
    path = (tmp_path / "progress.jsonl").resolve()

    package.bind_execution_progress("exec_owned", path)

    assert package.env == package.mod_env
    assert package.mod_env["JARVIS_EXECUTION_ID"] == "exec_owned"
    assert package.mod_env["JARVIS_PACKAGE_NAME"] == "site.application"
    assert package.mod_env["JARVIS_PACKAGE_ID"] == "application_a"
    assert package.mod_env["JARVIS_PROGRESS_PATH"] == str(path)
    assert package.mod_env["JARVIS_PROGRESS_TRANSPORT"] == "sidecar"
    with pytest.raises(ValueError, match="absolute"):
        package.bind_execution_progress("exec_owned", "relative.jsonl")


def test_package_callback_persists_typed_final_observation(tmp_path: Path) -> None:
    """JARVIS owns identity and flushes a provider's final stdout fragment."""

    class Provider:
        def __init__(self) -> None:
            self.text = ""

        def observe_progress(self, text: str) -> list[ProgressObservation]:
            self.text += text
            return []

        def finalize_progress(self) -> list[ProgressObservation]:
            return [
                ProgressObservation(
                    label="phase",
                    message=self.text,
                    metadata={"provider_semantic": "complete_fragment"},
                )
            ]

        def reset_progress(self) -> None:
            self.text = ""

    path = (tmp_path / "progress.jsonl").resolve()
    package = object.__new__(Pkg)
    package.mod_env = {
        "JARVIS_EXECUTION_ID": "exec_typed",
        "JARVIS_PACKAGE_ID": "app_a",
        "JARVIS_PACKAGE_NAME": "site.application",
        "JARVIS_PROGRESS_PATH": str(path),
    }
    provider = Provider()
    package.get_progress_provider = lambda: provider  # type: ignore[method-assign]

    callback = package.progress_line_callback()
    assert callback is not None
    callback("stdout", "unterminated output")
    finalizer = getattr(callback, "finalize")
    finalizer()
    finalizer()

    event = ProgressStore(path).latest()
    assert event is not None
    assert event.execution_id == "exec_typed"
    assert event.package_name == "site.application"
    assert event.package_id == "app_a"
    assert event.message == "unterminated output"
    assert event.sequence == 1


def test_package_callback_passes_owned_process_exit_to_provider(
    tmp_path: Path,
) -> None:
    """The optional exit-aware SPI receives JARVIS's authoritative code."""

    class Provider:
        def observe_progress(self, text: str) -> list[ProgressObservation]:
            del text
            return [ProgressObservation(label="phase", current=1, total=1)]

        def finalize_progress(self) -> list[ProgressObservation]:
            raise AssertionError("exit-aware provider used legacy finalization")

        def finalize_progress_for_exit(
            self, return_code: int
        ) -> list[ProgressObservation]:
            return [
                ProgressObservation(
                    label="phase",
                    state=(
                        ProgressState.COMPLETED
                        if return_code == 0
                        else ProgressState.RUNNING
                    ),
                    current=1,
                    total=1,
                    metadata={"return_code": return_code},
                )
            ]

        def reset_progress(self) -> None:
            return None

    path = (tmp_path / "exit-progress.jsonl").resolve()
    package = object.__new__(Pkg)
    package.mod_env = {
        "JARVIS_EXECUTION_ID": "exec_exit",
        "JARVIS_PACKAGE_ID": "app_a",
        "JARVIS_PACKAGE_NAME": "site.application",
        "JARVIS_PROGRESS_PATH": str(path),
    }
    package.get_progress_provider = lambda: Provider()  # type: ignore[method-assign]

    callback = package.progress_line_callback()
    assert callback is not None
    callback("stdout", "finished work\n")
    getattr(callback, "finalize_process")(0)

    event = ProgressStore(path).latest()
    assert event is not None
    assert event.state is ProgressState.COMPLETED
    assert event.metadata["return_code"] == 0
    assert event.sequence == 2


def test_container_callback_persists_structured_stdout_without_provider(
    tmp_path: Path,
) -> None:
    """Generic container instrumentation needs no application-specific parser."""
    path = (tmp_path / "progress.jsonl").resolve()
    stream = io.StringIO()
    child_reporter = ProgressReporter(
        package_name="site.application",
        package_id="container_app",
        execution_id="exec_container",
        stream=stream,
    )
    child_reporter.emit(label="frame", current=1, total=2, unit="frame")

    package = object.__new__(Pkg)
    package.mod_env = {
        "JARVIS_EXECUTION_ID": "exec_container",
        "JARVIS_PACKAGE_ID": "container_app",
        "JARVIS_PACKAGE_NAME": "site.application",
        "JARVIS_PROGRESS_PATH": str(path),
        "JARVIS_PROGRESS_TRANSPORT": "stdout",
    }
    package.get_progress_provider = lambda: None  # type: ignore[method-assign]

    callback = package.progress_line_callback()
    assert callback is not None
    callback("stdout", stream.getvalue())
    getattr(callback, "finalize")()

    event = ProgressStore(path).latest()
    assert event is not None
    assert event.execution_id == "exec_container"
    assert event.package_id == "container_app"
    assert event.current == 1


def test_structured_completion_is_corrected_after_nonzero_process_exit(
    tmp_path: Path,
) -> None:
    """JARVIS process ownership overrides a premature application success."""
    path = (tmp_path / "reconciled-progress.jsonl").resolve()
    stream = io.StringIO()
    child_reporter = ProgressReporter(
        package_name="site.application",
        package_id="container_app",
        execution_id="exec_failed_container",
        stream=stream,
    )
    completed = child_reporter.emit(
        label="frame",
        state=ProgressState.COMPLETED,
        current=2,
        total=2,
        unit="frame",
        message="Application reported completion",
        metadata={"application_signal": "last_frame"},
    )

    package = object.__new__(Pkg)
    package.mod_env = {
        "JARVIS_EXECUTION_ID": "exec_failed_container",
        "JARVIS_PACKAGE_ID": "container_app",
        "JARVIS_PACKAGE_NAME": "site.application",
        "JARVIS_PROGRESS_PATH": str(path),
        "JARVIS_PROGRESS_TRANSPORT": "stdout",
    }
    package.get_progress_provider = lambda: None  # type: ignore[method-assign]

    callback = package.progress_line_callback()
    assert callback is not None
    callback("stdout", stream.getvalue())
    getattr(callback, "finalize_process")(23)
    getattr(callback, "finalize_process")(23)

    events = ProgressStore(path).read_all()
    assert len(events) == 2
    assert events[0] == completed
    corrected = events[1]
    assert corrected.state is ProgressState.FAILED
    assert corrected.sequence == completed.sequence + 1
    assert corrected.current == completed.current
    assert corrected.total == completed.total
    assert corrected.unit == completed.unit
    assert corrected.metadata["jarvis_process_exit"] == {
        "reported_state": "completed",
        "return_code": 23,
        "source": "jarvis_process_owner",
    }


def test_structured_completion_remains_terminal_after_zero_process_exit(
    tmp_path: Path,
) -> None:
    """A zero JARVIS-owned return code leaves structured completion intact."""
    path = (tmp_path / "successful-progress.jsonl").resolve()
    stream = io.StringIO()
    child_reporter = ProgressReporter(
        package_name="site.application",
        package_id="container_app",
        execution_id="exec_successful_container",
        stream=stream,
    )
    completed = child_reporter.emit(
        label="frame",
        state=ProgressState.COMPLETED,
        current=2,
        total=2,
    )

    package = object.__new__(Pkg)
    package.mod_env = {
        "JARVIS_EXECUTION_ID": "exec_successful_container",
        "JARVIS_PACKAGE_ID": "container_app",
        "JARVIS_PACKAGE_NAME": "site.application",
        "JARVIS_PROGRESS_PATH": str(path),
        "JARVIS_PROGRESS_TRANSPORT": "stdout",
    }
    package.get_progress_provider = lambda: None  # type: ignore[method-assign]

    callback = package.progress_line_callback()
    assert callback is not None
    callback("stdout", stream.getvalue())
    getattr(callback, "finalize_process")(0)

    assert ProgressStore(path).read_all() == [completed]


def test_line_callback_failure_reconciles_structured_completion(
    tmp_path: Path,
) -> None:
    """A stream failure cannot leave an earlier structured success terminal."""
    progress_path = (tmp_path / "line-failure-progress.jsonl").resolve()
    stream = io.StringIO()
    child_reporter = ProgressReporter(
        package_name="site.application",
        package_id="container_app",
        execution_id="exec_line_failure",
        stream=stream,
    )
    child_reporter.emit(
        label="frame",
        state=ProgressState.COMPLETED,
        current=2,
        total=2,
    )
    structured_line = stream.getvalue().strip()

    package = object.__new__(Pkg)
    package.mod_env = {
        "JARVIS_EXECUTION_ID": "exec_line_failure",
        "JARVIS_PACKAGE_ID": "container_app",
        "JARVIS_PACKAGE_NAME": "site.application",
        "JARVIS_PROGRESS_PATH": str(progress_path),
        "JARVIS_PROGRESS_TRANSPORT": "stdout",
    }
    package.get_progress_provider = lambda: None  # type: ignore[method-assign]
    package.get_artifact_provider = lambda: None  # type: ignore[method-assign]
    runtime_callback = package.runtime_line_callback()
    assert runtime_callback is not None

    class FailAfterStructuredSuccess:
        def __call__(self, stream_name: str, line: str) -> None:
            runtime_callback(stream_name, line)
            if line.strip() == "trigger-callback-failure":
                raise RuntimeError("forced failure after structured success")

        def finalize_process(self, return_code: int) -> None:
            getattr(runtime_callback, "finalize_process")(return_code)

    command = subprocess.list2cmdline(
        [
            sys.executable,
            "-c",
            (
                "import time; "
                f"print({structured_line!r}, flush=True); "
                "print('trigger-callback-failure', flush=True); "
                "time.sleep(30)"
            ),
        ]
    )
    execution = LocalExec(
        command,
        LocalExecInfo(
            hide_output=True,
            line_callback=FailAfterStructuredSuccess(),
        ),
    )

    assert execution.exit_code["localhost"] != 0
    events = ProgressStore(progress_path).read_all()
    assert [event.state for event in events] == [
        ProgressState.COMPLETED,
        ProgressState.FAILED,
    ]
    assert events[-1].metadata["jarvis_process_exit"]["return_code"] != 0


def test_later_artifact_finalizer_failure_reconciles_progress_success(
    tmp_path: Path,
) -> None:
    """A later semantic failure corrects progress finalized earlier in the batch."""

    class CompletedProgressProvider:
        def observe_progress(self, text: str) -> list[ProgressObservation]:
            del text
            return []

        def finalize_progress(self) -> list[ProgressObservation]:
            return [
                ProgressObservation(
                    label="phase",
                    state=ProgressState.COMPLETED,
                    current=1,
                    total=1,
                )
            ]

        def reset_progress(self) -> None:
            return None

    class FailingArtifactProvider:
        def observe_artifacts(self, text: str) -> list[Any]:
            del text
            return []

        def finalize_artifacts(self) -> list[Any]:
            raise RuntimeError("artifact finalizer failed after progress")

        def reset_artifacts(self) -> None:
            return None

    progress_path = (tmp_path / "ordered-progress.jsonl").resolve()
    artifact_path = (tmp_path / "ordered-artifacts.jsonl").resolve()
    package = object.__new__(Pkg)
    package.mod_env = {
        "JARVIS_EXECUTION_ID": "exec_ordered_failure",
        "JARVIS_PACKAGE_ID": "application_a",
        "JARVIS_PACKAGE_NAME": "site.application",
        "JARVIS_PROGRESS_PATH": str(progress_path),
        "JARVIS_ARTIFACT_PATH": str(artifact_path),
    }
    package.get_progress_provider = (  # type: ignore[method-assign]
        lambda: CompletedProgressProvider()
    )
    package.get_artifact_provider = (  # type: ignore[method-assign]
        lambda: FailingArtifactProvider()
    )
    runtime_callback = package.runtime_line_callback()
    assert runtime_callback is not None

    execution = LocalExec(
        subprocess.list2cmdline([sys.executable, "-c", "pass"]),
        LocalExecInfo(hide_output=True, line_callback=runtime_callback),
    )

    assert execution.exit_code["localhost"] == 1
    assert "artifact finalizer failed" in execution.stderr["localhost"]
    events = ProgressStore(progress_path).read_all()
    assert [event.state for event in events] == [
        ProgressState.COMPLETED,
        ProgressState.FAILED,
    ]
    assert events[-1].metadata["jarvis_process_exit"] == {
        "reported_state": "completed",
        "return_code": 1,
        "source": "jarvis_process_owner",
    }


def test_filesystem_repository_discovers_sibling_progress_module(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A registered repository does not need a distribution entry point."""
    repository = tmp_path / "site_repo"
    package_dir = repository / "demo"
    package_dir.mkdir(parents=True)
    (repository / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "pkg.py").write_text(
        "class Demo:\n    pass\n",
        encoding="utf-8",
    )
    (package_dir / "progress.py").write_text(
        """
class Provider:
    def observe_progress(self, text): return []
    def finalize_progress(self): return []
    def reset_progress(self): pass
def adapter_from_package(package):
    return Provider() if package.get("pkg_type") == "site_repo.demo" else None
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    module = __import__("site_repo.demo.pkg", fromlist=["Demo"])
    package = module.Demo()
    package.config = {}
    package.pkg_type = "site_repo.demo"
    package.pkg_id = "demo_a"
    package.global_id = "pipeline.demo_a"
    package.pipeline = SimpleNamespace()
    package.mod_env = {
        "JARVIS_EXECUTION_ID": "exec_fs",
        "JARVIS_PROGRESS_PATH": str(tmp_path / "fs.jsonl"),
    }

    provider = provider_from_package(package)

    assert provider is not None
    assert provider.observe_progress("anything") == []
