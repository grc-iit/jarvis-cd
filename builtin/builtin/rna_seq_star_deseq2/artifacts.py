"""Generated-artifact semantics for native STAR to DESeq2 workflows."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .result_contract import RnaSeqProduct, load_rnaseq_result
from jarvis_cd.artifacts import (
    ArtifactLocation,
    ArtifactObservation,
    ArtifactOwnership,
    ArtifactRole,
    ArtifactState,
    ArtifactStructure,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _optional_absolute_path(value: object) -> PurePosixPath | None:
    if not isinstance(value, str) or not value:
        return None
    path = PurePosixPath(value)
    return path if path.is_absolute() else None


def _configured_output_dir(
    value: object, *, shared_dir: object, runtime_cwd: object
) -> PurePosixPath:
    raw = "run" if value in (None, "") else value
    if not isinstance(raw, str) or not raw or "\\" in raw:
        raise ValueError("RNA-seq artifacts require a printable output path")
    path = PurePosixPath(raw)
    if not path.is_absolute():
        base = _optional_absolute_path(shared_dir) or _optional_absolute_path(
            runtime_cwd
        )
        if base is None:
            raise ValueError(
                "relative RNA-seq artifact output requires shared_dir or runtime_cwd"
            )
        path = base / path
    if any(part in {"", ".", ".."} for part in path.parts[1:]):
        raise ValueError("RNA-seq artifact output is not confined")
    shared = _optional_absolute_path(shared_dir)
    if shared is not None and not path.is_relative_to(shared):
        raise ValueError("RNA-seq artifact output escaped its shared root")
    return path / "results"


@dataclass(slots=True)
class RnaSeqArtifactAdapter:
    """Publish a closed differential-expression result and its native products."""

    output_dir: PurePosixPath
    _local_root: Path | None = None
    _finalized: bool = False

    def observe_artifacts(self, text: str) -> list[ArtifactObservation]:
        """Wait for the authoritative process exit before publishing products."""

        del text
        return []

    def finalize_artifacts(self) -> list[ArtifactObservation]:
        """Finalize products for a successful legacy completion callback."""

        return self._finalize(return_code=0)

    def finalize_artifacts_for_exit(
        self, return_code: int
    ) -> list[ArtifactObservation]:
        """Finalize native products using the authoritative driver exit status."""

        return self._finalize(return_code=return_code)

    def reset_artifacts(self) -> None:
        """Permit discovery after an execution stream is replaced."""

        self._finalized = False

    def _local_output_path(self) -> Path:
        if self._local_root is not None:
            return self._local_root
        return Path(self.output_dir.as_posix())

    def _file(
        self,
        product: RnaSeqProduct,
        *,
        logical_name: str,
        kind: str,
        role: ArtifactRole,
        format_name: str,
        media_type: str,
        state: ArtifactState,
        metadata: dict[str, Any],
    ) -> ArtifactObservation:
        return ArtifactObservation(
            logical_name=logical_name,
            kind=kind,
            role=role,
            structure=ArtifactStructure.FILE,
            ownership=ArtifactOwnership.SHARED,
            state=state,
            location=ArtifactLocation.cluster_path(
                self.output_dir / product.relative_path
            ),
            media_type=media_type,
            format=format_name,
            size_bytes=product.size_bytes,
            checksum=f"sha256:{product.sha256}",
            message="Closed STAR to DESeq2 product",
            metadata=metadata,
        )

    def _finalize(self, *, return_code: int) -> list[ArtifactObservation]:
        if self._finalized:
            return []
        self._finalized = True
        local = self._local_output_path()
        if return_code != 0 and not (local / "rnaseq-result.json").is_file():
            return []
        validated = load_rnaseq_result(local)
        document = validated.document
        state = (
            ArtifactState.FINALIZED if return_code == 0 else ArtifactState.INCOMPLETE
        )
        metadata: dict[str, Any] = {
            "application": "star_deseq2",
            "comparison_condition": document["comparison_condition"],
            "reference_condition": document["reference_condition"],
            "sample_count": document["sample_count"],
            "significant_gene_count": document["deseq2"]["significant_gene_count"],
            "tested_gene_count": document["deseq2"]["tested_gene_count"],
        }
        products = validated.products
        result_size = validated.result_path.stat().st_size
        result_sha = _sha256(validated.result_path)
        observations = [
            ArtifactObservation(
                logical_name="rna-seq-result-tree",
                kind="scientific_result",
                role=ArtifactRole.OUTPUT,
                structure=ArtifactStructure.COLLECTION,
                ownership=ArtifactOwnership.SHARED,
                state=state,
                location=ArtifactLocation.cluster_path(self.output_dir),
                format="star-deseq2-result-tree",
                size_bytes=result_size
                + sum(product.size_bytes for product in products.values()),
                message="STAR to DESeq2 result tree finalized",
                metadata={**metadata, "member_count": len(products) + 1},
            ),
            ArtifactObservation(
                logical_name="rna-seq-result",
                kind="scientific_result",
                role=ArtifactRole.OUTPUT,
                structure=ArtifactStructure.FILE,
                ownership=ArtifactOwnership.SHARED,
                state=state,
                location=ArtifactLocation.cluster_path(
                    self.output_dir / "rnaseq-result.json"
                ),
                media_type="application/json",
                format="jarvis.rnaseq-star-deseq2-result.v1",
                size_bytes=result_size,
                checksum=f"sha256:{result_sha}",
                message="Closed STAR to DESeq2 result summary",
                metadata=metadata,
            ),
        ]
        file_specs = (
            (
                "differential-expression.tsv",
                "differential-expression",
                "scientific_table",
                ArtifactRole.OUTPUT,
                "deseq2-results-tsv",
                "text/tab-separated-values",
            ),
            (
                "gene-counts.tsv",
                "gene-counts",
                "scientific_table",
                ArtifactRole.OUTPUT,
                "star-gene-counts-tsv",
                "text/tab-separated-values",
            ),
            (
                "normalized-counts.tsv",
                "normalized-counts",
                "scientific_table",
                ArtifactRole.OUTPUT,
                "deseq2-normalized-counts-tsv",
                "text/tab-separated-values",
            ),
            (
                "samples.tsv",
                "rna-seq-sample-design",
                "configuration",
                ArtifactRole.PROVENANCE,
                "sample-design-tsv",
                "text/tab-separated-values",
            ),
            (
                "deseq2-session-info.txt",
                "deseq2-session-info",
                "provenance",
                ArtifactRole.PROVENANCE,
                "r-session-info",
                "text/plain",
            ),
        )
        for path, logical, kind, role, format_name, media_type in file_specs:
            observations.append(
                self._file(
                    products[path],
                    logical_name=logical,
                    kind=kind,
                    role=role,
                    format_name=format_name,
                    media_type=media_type,
                    state=state,
                    metadata=metadata,
                )
            )
        alignments = [
            product
            for path, product in products.items()
            if path.endswith("/Aligned.sortedByCoord.out.bam")
        ]
        observations.append(
            ArtifactObservation(
                logical_name="star-alignments",
                kind="scientific_dataset",
                role=ArtifactRole.OUTPUT,
                structure=ArtifactStructure.COLLECTION,
                ownership=ArtifactOwnership.SHARED,
                state=state,
                location=ArtifactLocation.cluster_path(self.output_dir / "star"),
                media_type="application/octet-stream",
                format="bam-collection",
                size_bytes=sum(product.size_bytes for product in alignments),
                message="Coordinate-sorted STAR alignment collection finalized",
                metadata={**metadata, "member_count": len(alignments)},
            )
        )
        return observations


def adapter_from_package(package: dict[str, Any]) -> RnaSeqArtifactAdapter | None:
    """Create artifact semantics only for native STAR to DESeq2 packages."""

    if package.get("pkg_type") != "builtin.rna_seq_star_deseq2":
        return None
    if str(package.get("effective_deploy_mode") or "default").casefold() == "container":
        return None
    output_dir = _configured_output_dir(
        package.get("out"),
        shared_dir=package.get("shared_dir"),
        runtime_cwd=package.get("runtime_cwd"),
    )
    return RnaSeqArtifactAdapter(output_dir)


__all__ = ["RnaSeqArtifactAdapter", "adapter_from_package"]
