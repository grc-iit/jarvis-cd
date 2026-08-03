"""Run a supplied STAR to DESeq2 workflow without runtime installation or network use."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import re
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

_SAMPLE_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")
_REQUIRED_SAMPLE_COLUMNS = frozenset(
    {"sample", "condition", "set_id", "accession", "fastq"}
)
_RESULT_SCHEMA = "jarvis.rnaseq-star-deseq2-result.v1"


class RnaSeqExecutionError(RuntimeError):
    """Raised when native RNA-seq inputs or products violate their contract."""


@dataclass(frozen=True, slots=True)
class Sample:
    """One supplied single-end RNA-seq sample."""

    name: str
    condition: str
    set_id: int
    accession: str
    fastq: Path


def _safe_relative(value: str, *, field: str) -> PurePosixPath:
    if "\\" in value:
        raise RnaSeqExecutionError(f"{field} must use portable separators")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RnaSeqExecutionError(f"{field} must be a confined relative path")
    return path


def _resolve_input(root: Path, value: str, *, field: str) -> Path:
    relative = _safe_relative(value, field=field)
    candidate = root.joinpath(*relative.parts)
    try:
        candidate.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise RnaSeqExecutionError(f"{field} escapes the supplied input root") from exc
    if candidate.is_symlink() or not candidate.is_file():
        raise RnaSeqExecutionError(f"{field} is not a regular supplied file")
    return candidate


def read_samples(path: Path, *, input_root: Path) -> tuple[Sample, ...]:
    """Read and validate a two-condition, replicated sample design."""

    try:
        with path.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream, delimiter="\t")
            if (
                reader.fieldnames is None
                or set(reader.fieldnames) != _REQUIRED_SAMPLE_COLUMNS
            ):
                raise RnaSeqExecutionError("RNA-seq sample table columns differ")
            rows = tuple(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise RnaSeqExecutionError("RNA-seq sample table is unreadable") from exc
    if not 4 <= len(rows) <= 64:
        raise RnaSeqExecutionError("RNA-seq sample count must be between 4 and 64")
    samples: list[Sample] = []
    for row in rows:
        name = row["sample"]
        condition = row["condition"]
        accession = row["accession"]
        if _SAMPLE_PATTERN.fullmatch(name) is None:
            raise RnaSeqExecutionError(f"invalid RNA-seq sample name: {name!r}")
        if _SAMPLE_PATTERN.fullmatch(condition) is None:
            raise RnaSeqExecutionError(f"invalid RNA-seq condition: {condition!r}")
        if _SAMPLE_PATTERN.fullmatch(accession) is None:
            raise RnaSeqExecutionError(f"invalid RNA-seq accession: {accession!r}")
        try:
            set_id = int(row["set_id"])
        except ValueError as exc:
            raise RnaSeqExecutionError("RNA-seq set_id must be an integer") from exc
        if set_id <= 0:
            raise RnaSeqExecutionError("RNA-seq set_id must be positive")
        samples.append(
            Sample(
                name=name,
                condition=condition,
                set_id=set_id,
                accession=accession,
                fastq=_resolve_input(input_root, row["fastq"], field="sample fastq"),
            )
        )
    names = [sample.name for sample in samples]
    accessions = [sample.accession for sample in samples]
    if len(names) != len(set(names)) or len(accessions) != len(set(accessions)):
        raise RnaSeqExecutionError("RNA-seq sample names and accessions must be unique")
    by_condition = Counter(sample.condition for sample in samples)
    if len(by_condition) != 2 or any(count < 2 for count in by_condition.values()):
        raise RnaSeqExecutionError("RNA-seq design requires two replicated conditions")
    paired_sets = {
        condition: {
            sample.set_id for sample in samples if sample.condition == condition
        }
        for condition in by_condition
    }
    if len({frozenset(values) for values in paired_sets.values()}) != 1:
        raise RnaSeqExecutionError(
            "RNA-seq conditions must use the same replicate-set IDs"
        )
    return tuple(samples)


def _read_length(path: Path) -> int:
    try:
        with gzip.open(path, "rb") as stream:
            header = stream.readline()
            sequence = stream.readline().rstrip(b"\r\n")
            plus = stream.readline()
            quality = stream.readline().rstrip(b"\r\n")
    except (OSError, EOFError) as exc:
        raise RnaSeqExecutionError(f"cannot read supplied FASTQ: {path.name}") from exc
    if (
        not header.startswith(b"@")
        or not plus.startswith(b"+")
        or not sequence
        or len(sequence) != len(quality)
    ):
        raise RnaSeqExecutionError(f"supplied FASTQ is malformed: {path.name}")
    return len(sequence)


def _run(command: list[str], *, label: str) -> None:
    print(f"[rnaseq] {label}: {command[0]}", flush=True)
    try:
        completed = subprocess.run(command, check=False)  # noqa: S603
    except OSError as exc:
        raise RnaSeqExecutionError(f"could not launch {label}") from exc
    if completed.returncode != 0:
        raise RnaSeqExecutionError(f"{label} exited with status {completed.returncode}")


def _decompress(source: Path, destination: Path) -> None:
    try:
        with gzip.open(source, "rb") as compressed, destination.open("wb") as output:
            shutil.copyfileobj(compressed, output)
    except (OSError, EOFError) as exc:
        raise RnaSeqExecutionError(f"could not decompress {source.name}") from exc
    if destination.stat().st_size <= 0:
        raise RnaSeqExecutionError(f"decompressed {source.name} is empty")


def _write_counts(samples: tuple[Sample, ...], star_root: Path, output: Path) -> None:
    genes: list[str] | None = None
    columns: list[list[int]] = []
    for sample in samples:
        path = star_root / sample.name / "ReadsPerGene.out.tab"
        try:
            rows = [
                line.split("\t")
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
        except (OSError, UnicodeError) as exc:
            raise RnaSeqExecutionError(
                f"STAR counts are unavailable for {sample.name}"
            ) from exc
        if len(rows) <= 4 or any(len(row) != 4 for row in rows):
            raise RnaSeqExecutionError(f"STAR counts are malformed for {sample.name}")
        observed_genes = [row[0] for row in rows[4:]]
        if genes is None:
            genes = observed_genes
        elif observed_genes != genes:
            raise RnaSeqExecutionError("STAR gene order differs across samples")
        try:
            columns.append([int(row[1]) for row in rows[4:]])
        except ValueError as exc:
            raise RnaSeqExecutionError(
                f"STAR counts are non-integral for {sample.name}"
            ) from exc
    assert genes is not None
    with output.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write("gene_id\t" + "\t".join(sample.name for sample in samples) + "\n")
        for index, gene in enumerate(genes):
            values = "\t".join(str(column[index]) for column in columns)
            stream.write(f"{gene}\t{values}\n")


def _star_metrics(path: Path) -> dict[str, int | float]:
    values: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "|" in line:
                name, value = line.split("|", 1)
                values[name.strip()] = value.strip()
    except (OSError, UnicodeError) as exc:
        raise RnaSeqExecutionError(f"STAR log is unavailable: {path}") from exc
    try:
        return {
            "input_reads": int(values["Number of input reads"]),
            "uniquely_mapped_reads": int(values["Uniquely mapped reads number"]),
            "uniquely_mapped_percent": float(
                values["Uniquely mapped reads %"].rstrip("%")
            ),
        }
    except (KeyError, ValueError) as exc:
        raise RnaSeqExecutionError(f"STAR log omitted mapping metrics: {path}") from exc


def _finite_or_none(value: str) -> float | None:
    if value in {"", "NA", "NaN", "nan"}:
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _deseq_summary(path: Path) -> tuple[int, int, list[dict[str, Any]]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as stream:
            rows = tuple(csv.DictReader(stream, delimiter="\t"))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise RnaSeqExecutionError("DESeq2 result table is unreadable") from exc
    required = {
        "gene_id",
        "baseMean",
        "log2FoldChange",
        "lfcSE",
        "stat",
        "pvalue",
        "padj",
    }
    if not rows or set(rows[0]) != required:
        raise RnaSeqExecutionError("DESeq2 result columns differ")
    significant: list[dict[str, Any]] = []
    tested = 0
    for row in rows:
        padj = _finite_or_none(row["padj"])
        fold = _finite_or_none(row["log2FoldChange"])
        if padj is not None:
            tested += 1
        if padj is not None and fold is not None and padj <= 0.05 and abs(fold) >= 1.0:
            significant.append(
                {
                    "gene_id": row["gene_id"],
                    "log2_fold_change": fold,
                    "adjusted_p_value": padj,
                }
            )
    significant.sort(
        key=lambda item: (item["adjusted_p_value"], -abs(item["log2_fold_change"]))
    )
    return tested, len(significant), significant[:20]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_records(paths: Iterable[Path], *, root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(paths):
        if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
            raise RnaSeqExecutionError(f"required RNA-seq product is missing: {path}")
        relative = path.relative_to(root).as_posix()
        records.append(
            {
                "path": relative,
                "sha256": _sha256(path),
                "size_bytes": path.stat().st_size,
            }
        )
    return records


def run_workflow(arguments: argparse.Namespace) -> dict[str, Any]:
    """Execute STAR and DESeq2 and return the closed result document."""

    input_root = arguments.input_root.resolve(strict=True)
    samples_path = arguments.samples.resolve(strict=True)
    samples = read_samples(samples_path, input_root=input_root)
    conditions = list(dict.fromkeys(sample.condition for sample in samples))
    reference_condition, comparison_condition = conditions
    output_root = arguments.output_root.resolve()
    work_root = arguments.work_root.resolve()
    if (
        output_root == work_root
        or output_root in work_root.parents
        or work_root in output_root.parents
    ):
        raise RnaSeqExecutionError("RNA-seq work and output roots must be separate")
    for root in (output_root, work_root):
        if root.exists() or root.is_symlink():
            raise RnaSeqExecutionError(f"RNA-seq target already exists: {root}")
        root.mkdir(parents=True)
    genome = work_root / "genome.fa"
    annotation = work_root / "genes.gtf"
    _decompress(arguments.genome.resolve(strict=True), genome)
    _decompress(arguments.annotation.resolve(strict=True), annotation)
    index_root = work_root / "star-index"
    index_root.mkdir()
    read_length = max(_read_length(sample.fastq) for sample in samples)
    _run(
        [
            arguments.star,
            "--runMode",
            "genomeGenerate",
            "--runThreadN",
            str(arguments.cores),
            "--genomeDir",
            str(index_root),
            "--genomeFastaFiles",
            str(genome),
            "--sjdbGTFfile",
            str(annotation),
            "--sjdbOverhang",
            str(read_length - 1),
            "--genomeSAindexNbases",
            "10",
        ],
        label="STAR genome generation",
    )
    star_root = output_root / "star"
    star_root.mkdir()
    for sample in samples:
        sample_root = star_root / sample.name
        sample_root.mkdir()
        _run(
            [
                arguments.star,
                "--runThreadN",
                str(arguments.cores),
                "--genomeDir",
                str(index_root),
                "--readFilesIn",
                str(sample.fastq),
                "--readFilesCommand",
                "zcat",
                "--outFileNamePrefix",
                str(sample_root) + "/",
                "--outSAMtype",
                "BAM",
                "SortedByCoordinate",
                "--quantMode",
                "GeneCounts",
            ],
            label=f"STAR alignment {sample.name}",
        )
    counts = output_root / "gene-counts.tsv"
    _write_counts(samples, star_root, counts)
    design = output_root / "samples.tsv"
    shutil.copyfile(samples_path, design)
    differential = output_root / "differential-expression.tsv"
    normalized = output_root / "normalized-counts.tsv"
    session_info = output_root / "deseq2-session-info.txt"
    _run(
        [
            arguments.rscript,
            str(arguments.deseq_script.resolve(strict=True)),
            str(counts),
            str(design),
            reference_condition,
            comparison_condition,
            str(differential),
            str(normalized),
            str(session_info),
        ],
        label="DESeq2 differential expression",
    )
    tested, significant_count, top = _deseq_summary(differential)
    mapping = {
        sample.name: _star_metrics(star_root / sample.name / "Log.final.out")
        for sample in samples
    }
    products = [counts, design, differential, normalized, session_info]
    for sample in samples:
        sample_root = star_root / sample.name
        products.extend(
            (
                sample_root / "Aligned.sortedByCoord.out.bam",
                sample_root / "ReadsPerGene.out.tab",
                sample_root / "Log.final.out",
            )
        )
    return {
        "artifacts": _artifact_records(products, root=output_root),
        "comparison_condition": comparison_condition,
        "deseq2": {
            "adjusted_p_value_threshold": 0.05,
            "absolute_log2_fold_change_threshold": 1.0,
            "significant_gene_count": significant_count,
            "tested_gene_count": tested,
            "top_significant_genes": top,
        },
        "mapping": mapping,
        "reference_condition": reference_condition,
        "sample_count": len(samples),
        "schema_version": _RESULT_SCHEMA,
        "star_read_length": read_length,
    }


def main() -> int:
    """Parse the confined native execution contract and run it."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", required=True, type=Path)
    parser.add_argument("--samples", required=True, type=Path)
    parser.add_argument("--genome", required=True, type=Path)
    parser.add_argument("--annotation", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--work-root", required=True, type=Path)
    parser.add_argument("--deseq-script", required=True, type=Path)
    parser.add_argument("--star", required=True)
    parser.add_argument("--rscript", required=True)
    parser.add_argument("--cores", required=True, type=int)
    arguments = parser.parse_args()
    if not 1 <= arguments.cores <= 64:
        parser.error("--cores must be between 1 and 64")
    result = run_workflow(arguments)
    result_path = arguments.output_root / "rnaseq-result.json"
    temporary = result_path.with_suffix(".json.partial")
    temporary.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(result_path)
    print(
        "[rnaseq] completed: "
        f"{result['deseq2']['significant_gene_count']} significant genes",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
