"""Generated-artifact semantics for the builtin WfCommons package."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from jarvis_cd.artifacts import (
    ArtifactLocation,
    ArtifactObservation,
    ArtifactOwnership,
    ArtifactRole,
    ArtifactState,
    ArtifactStructure,
)

RESULT_SCHEMA = "jarvis.wfcommons-result.v1"
RESULT_NAME = "wfcommons-result.json"


def _sha256(path: Path) -> str:
    """Return the SHA-256 digest of one bounded artifact."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(slots=True)
class WfcommonsArtifactAdapter:
    """Report a closed workflow cell and its exact provenance members."""

    output_dir: PurePosixPath
    _local_root: Path | None = None
    _finalized: bool = False

    def observe_artifacts(self, text: str) -> list[ArtifactObservation]:
        """Wait for authoritative process completion before reporting files."""

        del text
        return []

    def finalize_artifacts(self) -> list[ArtifactObservation]:
        """Finalize products for a successful legacy completion callback."""

        return self._finalize(return_code=0)

    def finalize_artifacts_for_exit(
        self, return_code: int
    ) -> list[ArtifactObservation]:
        """Finalize products using the authoritative driver exit status."""

        return self._finalize(return_code=return_code)

    def reset_artifacts(self) -> None:
        """Permit discovery after an execution stream is replaced."""

        self._finalized = False

    def _result(self) -> tuple[Path, dict[str, Any]]:
        """Load and minimally validate the package-owned result document."""

        root = self._local_output_path().resolve(strict=True)
        if root.is_symlink() or not root.is_dir():
            raise RuntimeError("WfCommons output root is not a real directory")
        path = root / RESULT_NAME
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise RuntimeError("WfCommons result is missing, unsafe, or oversized")
        try:
            document = json.loads(path.read_text(encoding="utf-8", errors="strict"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("WfCommons result is not valid JSON") from exc
        if (
            not isinstance(document, dict)
            or document.get("schema_version") != RESULT_SCHEMA
        ):
            raise RuntimeError("WfCommons result has the wrong schema")
        return path, document

    def _member(
        self,
        document: dict[str, Any],
        *,
        path_field: str,
        hash_field: str,
        maximum_bytes: int,
    ) -> tuple[Path, PurePosixPath]:
        """Resolve and digest-check one result-declared artifact member."""

        relative = document.get(path_field)
        expected = document.get(hash_field)
        if (
            not isinstance(relative, str)
            or not relative
            or not isinstance(expected, str)
            or len(expected) != 64
        ):
            raise RuntimeError(f"WfCommons result omitted {path_field}")
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise RuntimeError(
                f"WfCommons artifact escaped its output root: {relative}"
            )
        root = self._local_output_path().resolve(strict=True)
        path = (root / candidate).resolve(strict=False)
        if not path.is_relative_to(root):
            raise RuntimeError(
                f"WfCommons artifact escaped its output root: {relative}"
            )
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"WfCommons artifact is missing or unsafe: {relative}")
        size = path.stat().st_size
        if size <= 0 or size > maximum_bytes:
            raise RuntimeError(
                f"WfCommons artifact is outside its size bound: {relative}"
            )
        if _sha256(path) != expected:
            raise RuntimeError(f"WfCommons artifact hash differs: {relative}")
        return path, PurePosixPath(relative)

    def _local_output_path(self) -> Path:
        """Project the declared cluster directory into this host filesystem."""

        if self._local_root is not None:
            return self._local_root
        return Path(self.output_dir.as_posix())

    def _observation(
        self,
        path: Path,
        cluster_path: PurePosixPath,
        *,
        logical_name: str,
        kind: str,
        role: ArtifactRole,
        format_name: str,
        media_type: str,
        state: ArtifactState,
        metadata: dict[str, Any],
    ) -> ArtifactObservation:
        """Create one exact file observation."""

        return ArtifactObservation(
            logical_name=logical_name,
            kind=kind,
            role=role,
            structure=ArtifactStructure.FILE,
            ownership=ArtifactOwnership.SHARED,
            state=state,
            location=ArtifactLocation.cluster_path(cluster_path),
            media_type=media_type,
            format=format_name,
            size_bytes=path.stat().st_size,
            checksum=f"sha256:{_sha256(path)}",
            message="Closed WfCommons workflow-cell product",
            metadata=metadata,
        )

    def _finalize(self, *, return_code: int) -> list[ArtifactObservation]:
        """Validate and report the exact result-declared files once."""

        if self._finalized:
            return []
        self._finalized = True
        result_path, document = self._result()
        declared_return_code = document.get("return_code")
        if not isinstance(declared_return_code, int) or isinstance(
            declared_return_code, bool
        ):
            raise RuntimeError("WfCommons result omitted its process return code")
        state = (
            ArtifactState.FINALIZED
            if return_code == 0 and declared_return_code == 0
            else ArtifactState.INCOMPLETE
        )
        workflow, workflow_relative = self._member(
            document,
            path_field="workflow_manifest",
            hash_field="workflow_sha256",
            maximum_bytes=64 * 1024 * 1024,
        )
        log, log_relative = self._member(
            document,
            path_field="workflow_log",
            hash_field="workflow_log_sha256",
            maximum_bytes=64 * 1024 * 1024,
        )
        lock, lock_relative = self._member(
            document,
            path_field="dependency_lock",
            hash_field="dependency_lock_sha256",
            maximum_bytes=4 * 1024 * 1024,
        )
        schema, schema_relative = self._member(
            document,
            path_field="schema_file",
            hash_field="schema_sha256",
            maximum_bytes=1024 * 1024,
        )
        metadata = {
            "application": "wfcommons",
            "data_footprint_mb": document.get("data_footprint_mb"),
            "observed_task_count": document.get("observed_task_count"),
            "recipe": document.get("recipe"),
            "requested_task_count": document.get("requested_task_count"),
            "seed": document.get("seed"),
            "topology_sha256": document.get("topology_sha256"),
        }
        return [
            self._observation(
                result_path,
                self.output_dir / RESULT_NAME,
                logical_name="wfcommons-result",
                kind="scientific_result",
                role=ArtifactRole.OUTPUT,
                format_name=RESULT_SCHEMA,
                media_type="application/json",
                state=state,
                metadata=metadata,
            ),
            self._observation(
                workflow,
                self.output_dir / workflow_relative,
                logical_name="wfcommons-workflow",
                kind="workflow_manifest",
                role=ArtifactRole.OUTPUT,
                format_name="wfcommons-wfformat-json",
                media_type="application/json",
                state=state,
                metadata=metadata,
            ),
            self._observation(
                log,
                self.output_dir / log_relative,
                logical_name="wfcommons-workflow-log",
                kind="log",
                role=ArtifactRole.LOG,
                format_name="text-log",
                media_type="text/plain",
                state=state,
                metadata=metadata,
            ),
            self._observation(
                lock,
                self.output_dir / lock_relative,
                logical_name="wfcommons-dependency-lock",
                kind="provenance",
                role=ArtifactRole.PROVENANCE,
                format_name="python-distributions",
                media_type="text/plain",
                state=state,
                metadata=metadata,
            ),
            self._observation(
                schema,
                self.output_dir / schema_relative,
                logical_name="wfcommons-schema",
                kind="configuration",
                role=ArtifactRole.PROVENANCE,
                format_name="wfformat-json-schema",
                media_type="application/schema+json",
                state=state,
                metadata=metadata,
            ),
        ]


def adapter_from_package(package: dict[str, Any]) -> WfcommonsArtifactAdapter | None:
    """Create artifact semantics only for builtin WfCommons."""

    if package.get("pkg_type") != "builtin.wfcommons":
        return None
    configured = package.get("out")
    if not isinstance(configured, str) or not configured:
        raise ValueError("WfCommons artifact provider requires its output directory")
    output_dir = PurePosixPath(configured)
    if not output_dir.is_absolute() or not configured.startswith("/"):
        raise ValueError("WfCommons artifact output must be an absolute cluster path")
    shared = package.get("shared_dir")
    if isinstance(shared, str) and shared:
        shared_root = PurePosixPath(shared)
        if not output_dir.is_relative_to(shared_root):
            raise ValueError("WfCommons output directory escaped its shared root")
    return WfcommonsArtifactAdapter(output_dir)


__all__ = ["WfcommonsArtifactAdapter", "adapter_from_package"]
