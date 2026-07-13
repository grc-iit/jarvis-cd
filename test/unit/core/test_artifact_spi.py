"""Tests for JARVIS-owned generic generated artifact semantics."""

from __future__ import annotations

import io
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from jarvis_cd.artifacts import (
    ARTIFACT_LINE_PREFIX,
    ArtifactEvent,
    ArtifactLocation,
    ArtifactLocationKind,
    ArtifactObservation,
    ArtifactOwnership,
    ArtifactReporter,
    ArtifactRole,
    ArtifactState,
    ArtifactStore,
    ArtifactStructure,
    event_from_artifact_line,
    new_artifact_id,
    provider_from_package,
    validate_artifact_history,
)
from jarvis_cd.core.pkg import Pkg


def _event(
    *,
    artifact_id: str | None = None,
    sequence: int = 1,
    revision: int = 1,
    state: ArtifactState = ArtifactState.AVAILABLE,
    location: ArtifactLocation | None = None,
    ownership: ArtifactOwnership = ArtifactOwnership.EXECUTION,
) -> ArtifactEvent:
    return ArtifactEvent(
        package_name="site.application",
        package_id="simulation_a",
        execution_id="exec_123",
        artifact_id=artifact_id or new_artifact_id(),
        logical_name="simulation-output",
        kind="dataset",
        role=ArtifactRole.OUTPUT,
        structure=ArtifactStructure.COLLECTION,
        ownership=ownership,
        state=state,
        location=location or ArtifactLocation.execution_relative("output/data.bp"),
        format="adios2-bp5",
        sequence=sequence,
        revision=revision,
        metadata={"source": "application"},
    )


def test_artifact_event_round_trip_preserves_typed_identity() -> None:
    """The stable schema retains package, artifact, and cleanup identity."""
    event = _event()

    restored = ArtifactEvent.from_json(event.to_json())

    assert restored == event
    assert restored.terminal is False
    assert restored.ownership is ArtifactOwnership.EXECUTION
    assert restored.location is not None
    assert restored.location.kind is ArtifactLocationKind.EXECUTION_PATH
    assert restored.as_dict()["location"] == {
        "kind": "execution_path",
        "value": "output/data.bp",
    }


def test_artifact_ids_are_opaque_and_schema_rejects_ambiguous_json() -> None:
    """IDs carry no path semantics and untrusted JSON fails closed."""
    first = new_artifact_id()
    second = new_artifact_id()
    assert first.startswith("art_")
    assert first != second
    assert "simulation" not in first

    with pytest.raises(ValueError, match="opaque"):
        replace(_event(), artifact_id="output/data.bp")

    payload = _event().as_dict()
    payload["unexpected"] = True
    with pytest.raises(ValueError, match="unknown artifact event fields"):
        ArtifactEvent.from_dict(payload)

    duplicate = (
        _event()
        .to_json()
        .replace(
            '"package_id":"simulation_a"',
            '"package_id":"simulation_a","package_id":"other"',
        )
    )
    with pytest.raises(ValueError, match="not valid JSON"):
        ArtifactEvent.from_json(duplicate)

    with pytest.raises(ValueError, match="finite JSON"):
        replace(_event(), metadata={"bad": float("nan")})
    with pytest.raises(ValueError, match="positive integer"):
        replace(_event(), sequence=cast(Any, 0))


def test_artifact_locations_are_explicit_and_non_authorizing() -> None:
    """Owned paths cannot escape, while cluster paths remain opaque references."""
    execution_path = ArtifactLocation.execution_relative("frames/step-0001.h5")
    cluster_path = ArtifactLocation.cluster_path("/mnt/results/run/data.bp")
    object_uri = ArtifactLocation.external_uri("s3://science/run/data.bp")

    assert execution_path.kind is ArtifactLocationKind.EXECUTION_PATH
    assert cluster_path.kind is ArtifactLocationKind.CLUSTER_PATH
    assert object_uri.kind is ArtifactLocationKind.EXTERNAL_URI

    for unsafe in ("../outside", "/absolute", "C:/windows", "a\\b", "a//b"):
        with pytest.raises(ValueError):
            ArtifactLocation.execution_relative(unsafe)
    for unsafe in ("relative/path", "/", "/a/../b", "/a//b", "C:\\output"):
        with pytest.raises(ValueError):
            ArtifactLocation.cluster_path(unsafe)
    for unsafe in (
        "file:///tmp/output",
        "data:text/plain,secret",
        "https://user:password@example.test/output",
        "https:/missing-authority/output",
        "C:/windows/path",
        "no-scheme",
    ):
        with pytest.raises(ValueError):
            ArtifactLocation.external_uri(unsafe)

    with pytest.raises(ValueError, match="owned by the execution"):
        replace(_event(), ownership=ArtifactOwnership.SHARED)
    with pytest.raises(ValueError, match="execution-relative"):
        replace(
            _event(),
            location=cluster_path,
            ownership=ArtifactOwnership.EXECUTION,
        )


def test_available_artifact_requires_a_resolvable_location() -> None:
    """A manifest cannot claim availability without saying how to resolve it."""
    with pytest.raises(ValueError, match="requires a location"):
        replace(_event(), location=None)

    producing = replace(
        _event(),
        state=ArtifactState.PRODUCING,
        location=None,
    )
    assert producing.location is None


def test_artifact_history_enforces_revisions_and_terminal_lifecycle() -> None:
    """Immutable identity and forward-only state prevent manifest ambiguity."""
    artifact_id = new_artifact_id()
    producing = _event(
        artifact_id=artifact_id,
        state=ArtifactState.PRODUCING,
        location=ArtifactLocation.execution_relative("output/data.bp"),
    )
    available = replace(
        producing,
        state=ArtifactState.AVAILABLE,
        revision=2,
        sequence=2,
        size_bytes=1024,
    )
    finalized = replace(
        available,
        state=ArtifactState.FINALIZED,
        revision=3,
        sequence=3,
        checksum="sha256:" + "a" * 64,
    )

    validate_artifact_history([producing, available, finalized])
    assert finalized.terminal

    with pytest.raises(ValueError, match="cannot change"):
        validate_artifact_history(
            [producing, replace(available, logical_name="renamed")]
        )
    with pytest.raises(ValueError, match="cannot decrease"):
        validate_artifact_history(
            [
                producing,
                available,
                replace(
                    finalized,
                    state=ArtifactState.FINALIZED,
                    size_bytes=100,
                ),
            ]
        )
    with pytest.raises(ValueError, match="cannot be cleared"):
        validate_artifact_history(
            [
                producing,
                available,
                replace(
                    finalized,
                    state=ArtifactState.FINALIZED,
                    size_bytes=None,
                ),
            ]
        )
    with pytest.raises(ValueError, match="cannot transition"):
        validate_artifact_history(
            [
                producing,
                available,
                finalized,
                replace(finalized, revision=4, sequence=4),
            ]
        )
    with pytest.raises(ValueError, match="contiguous"):
        validate_artifact_history([producing, replace(available, sequence=3)])


def test_store_is_durable_and_terminalization_seals_only_producing(
    tmp_path: Path,
) -> None:
    """Terminalization never silently promotes available data to finalized."""
    path = tmp_path / "artifacts.jsonl"
    store = ArtifactStore(path)
    producing = _event(
        state=ArtifactState.PRODUCING,
        location=ArtifactLocation.cluster_path("/scratch/run/live.bp"),
        ownership=ArtifactOwnership.SHARED,
    )
    available = _event(
        sequence=2,
        location=ArtifactLocation.cluster_path("/shared/run/result.bp"),
        ownership=ArtifactOwnership.SHARED,
    )
    available = replace(
        available,
        role=ArtifactRole.INTERMEDIATE,
        logical_name="shared-snapshot",
    )
    store.append(producing)
    store.append(available)

    sealed = store.finalize_open()

    assert len(sealed) == 1
    assert sealed[0].artifact_id == producing.artifact_id
    assert sealed[0].state is ArtifactState.INCOMPLETE
    current = store.current()
    assert current[producing.artifact_id].state is ArtifactState.INCOMPLETE
    assert current[available.artifact_id].state is ArtifactState.AVAILABLE
    assert store.is_sealed()
    assert store.finalize_open() == []
    with pytest.raises(RuntimeError, match="sealed"):
        store.append(
            _event(
                artifact_id=new_artifact_id(),
                sequence=4,
                location=ArtifactLocation.cluster_path("/scratch/run/too-late.bp"),
                ownership=ArtifactOwnership.SHARED,
            )
        )
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0


def test_store_rejects_identity_replay_torn_records_and_redirects(
    tmp_path: Path,
) -> None:
    """Mixed identities, replay, partial writes, and symlinks fail closed."""
    path = tmp_path / "artifacts.jsonl"
    store = ArtifactStore(path)
    event = _event()
    store.append(event)
    with pytest.raises(ValueError, match="contiguous"):
        store.append(replace(event, artifact_id=new_artifact_id(), revision=1))
    with pytest.raises(ValueError, match="identity must remain stable"):
        store.append(
            replace(
                event,
                artifact_id=new_artifact_id(),
                execution_id="other",
                sequence=2,
            )
        )

    path.write_text(event.to_json(), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    with pytest.raises(ValueError, match="incomplete final JSONL record"):
        store.read_all()

    if hasattr(os, "symlink"):
        target = tmp_path / "target.jsonl"
        target.write_text(event.to_json() + "\n", encoding="utf-8")
        link = tmp_path / "redirected.jsonl"
        try:
            link.symlink_to(target)
        except OSError:
            return
        with pytest.raises(ValueError, match="symlink"):
            ArtifactStore(link).read_all()


def test_reporter_emits_stdout_sidecar_and_seals_open_artifacts(
    tmp_path: Path,
) -> None:
    """One typed event contract works over stdout and durable sidecars."""
    stream = io.StringIO()
    stdout_reporter = ArtifactReporter(
        package_name="site.application",
        package_id="app_a",
        execution_id="exec_line",
        stream=stream,
    )
    emitted = stdout_reporter.emit(
        logical_name="application-log",
        kind="log",
        role=ArtifactRole.LOG,
        structure=ArtifactStructure.STREAM,
        ownership=ArtifactOwnership.EXECUTION,
        state=ArtifactState.PRODUCING,
        location="logs/stdout.log",
    )
    sealed = stdout_reporter.finalize_execution()

    lines = stream.getvalue().splitlines()
    assert all(line.startswith(ARTIFACT_LINE_PREFIX) for line in lines)
    assert event_from_artifact_line(lines[0]) == emitted
    assert sealed[0].state is ArtifactState.INCOMPLETE
    assert event_from_artifact_line(lines[1]) == sealed[0]

    path = tmp_path / "owned" / "artifacts.jsonl"
    sidecar = ArtifactReporter(
        package_name="site.application",
        package_id="app_b",
        execution_id="exec_file",
        path=path,
    )
    event = sidecar.emit(
        logical_name="final-image",
        kind="image",
        role=ArtifactRole.OUTPUT,
        structure=ArtifactStructure.FILE,
        ownership=ArtifactOwnership.EXECUTION,
        state=ArtifactState.FINALIZED,
        location="images/final.png",
        media_type="image/png",
        size_bytes=42,
    )

    assert ArtifactStore(path).latest(event.artifact_id) == event
    assert sidecar.finalize_execution() == []


def test_reporter_adopts_only_a_matching_existing_manifest(tmp_path: Path) -> None:
    """Sequence adoption cannot cross execution or package identity."""
    path = tmp_path / "artifacts.jsonl"
    first = _event()
    ArtifactStore(path).append(first)
    reporter = ArtifactReporter(
        package_name="site.application",
        package_id="simulation_a",
        execution_id="exec_123",
        path=path,
    )
    second = reporter.emit(
        logical_name="second-log",
        kind="log",
        role=ArtifactRole.LOG,
        structure=ArtifactStructure.FILE,
        ownership=ArtifactOwnership.EXECUTION,
        location="logs/second.log",
    )
    assert second.sequence == 2

    with pytest.raises(ValueError, match="does not match this reporter"):
        ArtifactReporter(
            package_name="site.application",
            package_id="simulation_a",
            execution_id="different",
            path=path,
        )


def test_multiple_reporters_allocate_sidecar_sequences_under_the_store_lock(
    tmp_path: Path,
) -> None:
    """Reporter caches cannot create duplicate sequences across producers."""
    path = tmp_path / "artifacts.jsonl"
    first = ArtifactReporter(
        package_name="site.application",
        package_id="simulation",
        execution_id="exec-concurrent",
        path=path,
    )
    second = ArtifactReporter(
        package_name="site.application",
        package_id="simulation",
        execution_id="exec-concurrent",
        path=path,
    )

    first_event = first.emit(
        logical_name="first",
        kind="log",
        role=ArtifactRole.LOG,
        structure=ArtifactStructure.FILE,
        ownership=ArtifactOwnership.EXECUTION,
        location="logs/first.log",
    )
    second_event = second.emit(
        logical_name="second",
        kind="log",
        role=ArtifactRole.LOG,
        structure=ArtifactStructure.FILE,
        ownership=ArtifactOwnership.EXECUTION,
        location="logs/second.log",
    )

    assert (first_event.sequence, second_event.sequence) == (1, 2)
    assert [event.sequence for event in ArtifactStore(path).read_all()] == [1, 2]


def test_observation_has_no_authoritative_execution_identity() -> None:
    """Package providers describe semantics but cannot select the owning run."""
    observation = ArtifactObservation(
        logical_name="checkpoint",
        kind="checkpoint",
        role=ArtifactRole.CHECKPOINT,
        structure=ArtifactStructure.FILE,
        ownership=ArtifactOwnership.SHARED,
        location=ArtifactLocation.cluster_path("/shared/checkpoints/latest.h5"),
    )

    assert "execution_id" not in observation.__dataclass_fields__
    assert "package_id" not in observation.__dataclass_fields__


def test_filesystem_repository_discovers_sibling_artifacts_module(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A registered repository needs no distribution entry point for artifacts."""
    repository = tmp_path / "artifact_site_repo"
    package_dir = repository / "demo"
    package_dir.mkdir(parents=True)
    (repository / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "pkg.py").write_text("class Demo:\n    pass\n", encoding="utf-8")
    (package_dir / "artifacts.py").write_text(
        """
class Provider:
    def observe_artifacts(self, text): return []
    def finalize_artifacts(self): return []
    def reset_artifacts(self): pass
def adapter_from_package(package):
    return Provider() if package.get("pkg_type") == "artifact_site_repo.demo" else None
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    module = __import__("artifact_site_repo.demo.pkg", fromlist=["Demo"])
    package = module.Demo()
    package.config = {}
    package.pkg_type = "artifact_site_repo.demo"
    package.pkg_id = "demo_a"
    package.global_id = "pipeline.demo_a"
    package.pipeline = SimpleNamespace()

    provider = provider_from_package(cast(Any, package))

    assert provider is not None
    assert provider.observe_artifacts("anything") == []


def test_package_runtime_callback_persists_artifact_provider_output(
    tmp_path: Path,
) -> None:
    """The combined runtime callback adds authoritative identity and storage."""

    class Provider:
        def observe_artifacts(self, text: str) -> list[ArtifactObservation]:
            if text.strip() != "wrote result.bp":
                return []
            return [
                ArtifactObservation(
                    logical_name="result",
                    kind="scientific_dataset",
                    role=ArtifactRole.OUTPUT,
                    structure=ArtifactStructure.COLLECTION,
                    ownership=ArtifactOwnership.SHARED,
                    state=ArtifactState.FINALIZED,
                    location=ArtifactLocation.cluster_path("/scratch/result.bp"),
                    format="adios2-bp5",
                )
            ]

        def finalize_artifacts(self) -> list[ArtifactObservation]:
            return []

        def reset_artifacts(self) -> None:
            return None

    package = Pkg.__new__(Pkg)
    path = (tmp_path / "artifacts.jsonl").resolve()
    package.mod_env = {
        "JARVIS_EXECUTION_ID": "exec-runtime",
        "JARVIS_PACKAGE_NAME": "site.application",
        "JARVIS_PACKAGE_ID": "application-a",
        "JARVIS_ARTIFACT_PATH": str(path),
        "JARVIS_ARTIFACT_TRANSPORT": "sidecar",
    }
    package.get_progress_provider = lambda: None  # type: ignore[method-assign]
    package.get_artifact_provider = lambda: Provider()  # type: ignore[method-assign]

    callback = package.runtime_line_callback()
    assert callback is not None
    callback("stdout", "wrote result.bp\n")
    getattr(callback, "finalize")()

    event = ArtifactStore(path).latest()
    assert event is not None
    assert event.execution_id == "exec-runtime"
    assert event.package_name == "site.application"
    assert event.package_id == "application-a"
    assert event.state is ArtifactState.FINALIZED
