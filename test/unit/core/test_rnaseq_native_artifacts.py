"""Native runner and closed-artifact tests for STAR to DESeq2."""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import sys
from importlib import import_module
from pathlib import Path
from types import ModuleType

import pytest


def _load_module(name: str) -> ModuleType:
    repository_root = Path(__file__).resolve().parents[3] / "builtin"
    sys.path.insert(0, str(repository_root))
    try:
        return import_module(name)
    finally:
        sys.path.remove(str(repository_root))


runner = _load_module("builtin.rna_seq_star_deseq2.native_runner")
contract = _load_module("builtin.rna_seq_star_deseq2.result_contract")
artifacts = _load_module("builtin.rna_seq_star_deseq2.artifacts")


def _write_fastq(path: Path, *, sequence: str = "ACGT") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="ascii", newline="\n") as stream:
        stream.write(f"@read\n{sequence}\n+\n{'I' * len(sequence)}\n")


def _write_samples(root: Path, *, second_set: int = 2) -> Path:
    rows = (
        ("a1", "control", 1, "SRR1", "reads/a1.fastq.gz"),
        ("a2", "control", second_set, "SRR2", "reads/a2.fastq.gz"),
        ("b1", "treated", 1, "SRR3", "reads/b1.fastq.gz"),
        ("b2", "treated", 2, "SRR4", "reads/b2.fastq.gz"),
    )
    for row in rows:
        _write_fastq(root / row[4])
    path = root / "samples.tsv"
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow(("sample", "condition", "set_id", "accession", "fastq"))
        writer.writerows(rows)
    return path


def test_sample_design_requires_paired_replicates_and_confined_reads(
    tmp_path: Path,
) -> None:
    samples = _write_samples(tmp_path)

    observed = runner.read_samples(samples, input_root=tmp_path)

    assert [sample.name for sample in observed] == ["a1", "a2", "b1", "b2"]
    assert {sample.condition for sample in observed} == {"control", "treated"}

    unpaired_root = tmp_path / "unpaired"
    unpaired_root.mkdir()
    unpaired = _write_samples(unpaired_root, second_set=3)
    with pytest.raises(runner.RnaSeqExecutionError, match="same replicate-set IDs"):
        runner.read_samples(unpaired, input_root=unpaired_root)

    text = samples.read_text(encoding="utf-8").replace(
        "reads/a1.fastq.gz", "../a1.fastq.gz"
    )
    samples.write_text(text, encoding="utf-8")
    with pytest.raises(runner.RnaSeqExecutionError, match="confined relative path"):
        runner.read_samples(samples, input_root=tmp_path)


def test_star_gene_counts_require_identical_gene_order(tmp_path: Path) -> None:
    sample_path = _write_samples(tmp_path)
    samples = runner.read_samples(sample_path, input_root=tmp_path)
    star = tmp_path / "star"
    for index, sample in enumerate(samples):
        sample_root = star / sample.name
        sample_root.mkdir(parents=True)
        genes = ("gene1", "gene2") if index != 3 else ("gene2", "gene1")
        (sample_root / "ReadsPerGene.out.tab").write_text(
            "N_unmapped\t0\t0\t0\n"
            "N_multimapping\t0\t0\t0\n"
            "N_noFeature\t0\t0\t0\n"
            "N_ambiguous\t0\t0\t0\n"
            + "".join(f"{gene}\t{index + 1}\t0\t0\n" for gene in genes),
            encoding="utf-8",
        )

    with pytest.raises(runner.RnaSeqExecutionError, match="gene order differs"):
        runner._write_counts(samples, star, tmp_path / "counts.tsv")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_closed_result(root: Path) -> dict[str, object]:
    root.mkdir(parents=True, exist_ok=True)
    samples = ("a1", "a2", "b1", "b2")
    paths = [
        "gene-counts.tsv",
        "samples.tsv",
        "differential-expression.tsv",
        "normalized-counts.tsv",
        "deseq2-session-info.txt",
    ]
    for sample in samples:
        paths.extend(
            (
                f"star/{sample}/Aligned.sortedByCoord.out.bam",
                f"star/{sample}/ReadsPerGene.out.tab",
                f"star/{sample}/Log.final.out",
            )
        )
    records: list[dict[str, object]] = []
    for relative in paths:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"product:{relative}\n".encode())
        records.append(
            {
                "path": relative,
                "sha256": _sha256(path),
                "size_bytes": path.stat().st_size,
            }
        )
    document: dict[str, object] = {
        "artifacts": records,
        "comparison_condition": "treated",
        "deseq2": {
            "adjusted_p_value_threshold": 0.05,
            "absolute_log2_fold_change_threshold": 1.0,
            "significant_gene_count": 0,
            "tested_gene_count": 10,
            "top_significant_genes": [],
        },
        "mapping": {
            sample: {
                "input_reads": 100,
                "uniquely_mapped_reads": 80,
                "uniquely_mapped_percent": 80.0,
            }
            for sample in samples
        },
        "reference_condition": "control",
        "sample_count": 4,
        "schema_version": contract.RESULT_SCHEMA,
        "star_read_length": 100,
    }
    (root / contract.RESULT_NAME).write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return document


def test_closed_result_rejects_changed_products(tmp_path: Path) -> None:
    _write_closed_result(tmp_path)

    validated = contract.load_rnaseq_result(tmp_path)
    assert len(validated.products) == 17

    (tmp_path / "gene-counts.tsv").write_text("changed\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="differs from result"):
        contract.load_rnaseq_result(tmp_path)


def test_artifact_adapter_finalizes_result_tables_and_alignment_collection(
    tmp_path: Path,
) -> None:
    _write_closed_result(tmp_path)
    adapter = artifacts.RnaSeqArtifactAdapter(
        artifacts.PurePosixPath("/cluster/run/results"),
        _local_root=tmp_path,
    )

    observed = adapter.finalize_artifacts_for_exit(0)

    by_name = {item.logical_name: item for item in observed}
    assert set(by_name) == {
        "rna-seq-result-tree",
        "rna-seq-result",
        "differential-expression",
        "gene-counts",
        "normalized-counts",
        "rna-seq-sample-design",
        "deseq2-session-info",
        "star-alignments",
    }
    assert all(item.state.value == "finalized" for item in observed)
    assert by_name["star-alignments"].metadata["member_count"] == 4
    assert by_name["rna-seq-result"].checksum.startswith("sha256:")
    assert adapter.finalize_artifacts_for_exit(0) == []


def test_failed_process_without_closed_result_publishes_nothing(tmp_path: Path) -> None:
    adapter = artifacts.RnaSeqArtifactAdapter(
        artifacts.PurePosixPath("/cluster/run/results"),
        _local_root=tmp_path,
    )

    assert adapter.finalize_artifacts_for_exit(2) == []


def test_adapter_resolves_relative_output_below_shared_storage() -> None:
    adapter = artifacts.adapter_from_package(
        {
            "pkg_type": "builtin.rna_seq_star_deseq2",
            "effective_deploy_mode": "default",
            "out": "study",
            "shared_dir": "/cluster/shared",
        }
    )

    assert adapter is not None
    assert adapter.output_dir.as_posix() == "/cluster/shared/study/results"
