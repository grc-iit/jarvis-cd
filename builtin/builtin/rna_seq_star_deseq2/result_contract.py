"""Closed result validation shared by RNA-seq lifecycle and artifact semantics."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

RESULT_NAME = "rnaseq-result.json"
RESULT_SCHEMA = "jarvis.rnaseq-star-deseq2-result.v1"
_TOP_LEVEL_FIELDS = {
    "artifacts",
    "comparison_condition",
    "deseq2",
    "mapping",
    "reference_condition",
    "sample_count",
    "schema_version",
    "star_read_length",
}


@dataclass(frozen=True, slots=True)
class RnaSeqProduct:
    """One result-declared, digest-verified RNA-seq product."""

    relative_path: PurePosixPath
    local_path: Path
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ValidatedRnaSeqResult:
    """A closed RNA-seq result and all of its verified products."""

    result_path: Path
    document: dict[str, Any]
    products: dict[str, RnaSeqProduct]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_path(value: object) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise RuntimeError("RNA-seq result contains an invalid artifact path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RuntimeError("RNA-seq result artifact path is not confined")
    return path


def _positive_integer(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RuntimeError(f"RNA-seq result {field} must be a positive integer")
    return value


def _validate_mapping(
    document: dict[str, Any], *, sample_count: int
) -> tuple[str, ...]:
    mapping = document.get("mapping")
    if not isinstance(mapping, dict) or len(mapping) != sample_count:
        raise RuntimeError("RNA-seq result mapping summary differs from sample count")
    names: list[str] = []
    for name, raw in mapping.items():
        if not isinstance(name, str) or not name or not isinstance(raw, dict):
            raise RuntimeError("RNA-seq result contains an invalid sample mapping")
        if set(raw) != {
            "input_reads",
            "uniquely_mapped_percent",
            "uniquely_mapped_reads",
        }:
            raise RuntimeError("RNA-seq sample mapping fields differ")
        input_reads = _positive_integer(raw["input_reads"], field="input_reads")
        mapped_reads = raw["uniquely_mapped_reads"]
        percent = raw["uniquely_mapped_percent"]
        if (
            isinstance(mapped_reads, bool)
            or not isinstance(mapped_reads, int)
            or not 0 <= mapped_reads <= input_reads
            or isinstance(percent, bool)
            or not isinstance(percent, (int, float))
            or not 0 <= float(percent) <= 100
        ):
            raise RuntimeError("RNA-seq sample mapping metric is outside its bounds")
        names.append(name)
    return tuple(names)


def _validate_deseq2(document: dict[str, Any]) -> None:
    summary = document.get("deseq2")
    if not isinstance(summary, dict) or set(summary) != {
        "absolute_log2_fold_change_threshold",
        "adjusted_p_value_threshold",
        "significant_gene_count",
        "tested_gene_count",
        "top_significant_genes",
    }:
        raise RuntimeError("RNA-seq DESeq2 summary fields differ")
    tested = _positive_integer(summary["tested_gene_count"], field="tested_gene_count")
    significant = summary["significant_gene_count"]
    top = summary["top_significant_genes"]
    if (
        isinstance(significant, bool)
        or not isinstance(significant, int)
        or not 0 <= significant <= tested
        or not isinstance(top, list)
        or len(top) > min(significant, 20)
    ):
        raise RuntimeError("RNA-seq DESeq2 summary values are inconsistent")
    for item in top:
        if not isinstance(item, dict) or set(item) != {
            "adjusted_p_value",
            "gene_id",
            "log2_fold_change",
        }:
            raise RuntimeError("RNA-seq significant-gene record fields differ")


def load_rnaseq_result(output_root: Path) -> ValidatedRnaSeqResult:
    """Load a result only when its closed document and exact products agree."""

    root = output_root.resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("RNA-seq output root is not a real directory")
    result_path = root / RESULT_NAME
    if (
        result_path.is_symlink()
        or not result_path.is_file()
        or not 0 < result_path.stat().st_size <= 2 * 1024 * 1024
    ):
        raise RuntimeError("RNA-seq result document is missing, unsafe, or oversized")
    try:
        document = json.loads(result_path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("RNA-seq result document is not valid JSON") from exc
    if (
        not isinstance(document, dict)
        or set(document) != _TOP_LEVEL_FIELDS
        or document.get("schema_version") != RESULT_SCHEMA
    ):
        raise RuntimeError("RNA-seq result document has the wrong schema")
    sample_count = _positive_integer(document.get("sample_count"), field="sample_count")
    if not 4 <= sample_count <= 64:
        raise RuntimeError("RNA-seq sample count is outside its supported bound")
    read_length = _positive_integer(
        document.get("star_read_length"), field="star_read_length"
    )
    if read_length > 1000:
        raise RuntimeError("RNA-seq read length is outside its supported bound")
    reference = document.get("reference_condition")
    comparison = document.get("comparison_condition")
    if (
        not isinstance(reference, str)
        or not reference
        or not isinstance(comparison, str)
        or not comparison
        or reference == comparison
    ):
        raise RuntimeError("RNA-seq result conditions are invalid")
    sample_names = _validate_mapping(document, sample_count=sample_count)
    _validate_deseq2(document)

    raw_products = document.get("artifacts")
    expected_count = 5 + 3 * sample_count
    if not isinstance(raw_products, list) or len(raw_products) != expected_count:
        raise RuntimeError(
            "RNA-seq result artifact count differs from its sample count"
        )
    products: dict[str, RnaSeqProduct] = {}
    for raw in raw_products:
        if not isinstance(raw, dict) or set(raw) != {"path", "sha256", "size_bytes"}:
            raise RuntimeError("RNA-seq result artifact record fields differ")
        relative = _relative_path(raw["path"])
        expected_sha = raw["sha256"]
        expected_size = raw["size_bytes"]
        if (
            relative.as_posix() in products
            or not isinstance(expected_sha, str)
            or len(expected_sha) != 64
            or not set(expected_sha).issubset("0123456789abcdef")
            or isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size <= 0
        ):
            raise RuntimeError("RNA-seq result artifact identity is invalid")
        lexical = root.joinpath(*relative.parts)
        if lexical.is_symlink():
            raise RuntimeError(f"RNA-seq artifact is a symbolic link: {relative}")
        candidate = lexical.resolve(strict=True)
        if (
            not candidate.is_relative_to(root)
            or candidate.is_symlink()
            or not candidate.is_file()
            or candidate.stat().st_size != expected_size
            or _sha256(candidate) != expected_sha
        ):
            raise RuntimeError(f"RNA-seq artifact differs from result: {relative}")
        products[relative.as_posix()] = RnaSeqProduct(
            relative_path=relative,
            local_path=candidate,
            sha256=expected_sha,
            size_bytes=expected_size,
        )
    required = {
        "gene-counts.tsv",
        "samples.tsv",
        "differential-expression.tsv",
        "normalized-counts.tsv",
        "deseq2-session-info.txt",
    }
    for name in sample_names:
        required.update(
            {
                f"star/{name}/Aligned.sortedByCoord.out.bam",
                f"star/{name}/ReadsPerGene.out.tab",
                f"star/{name}/Log.final.out",
            }
        )
    if set(products) != required:
        raise RuntimeError("RNA-seq result does not declare the complete product set")
    return ValidatedRnaSeqResult(result_path, document, products)


__all__ = [
    "RESULT_NAME",
    "RESULT_SCHEMA",
    "RnaSeqProduct",
    "ValidatedRnaSeqResult",
    "load_rnaseq_result",
]
