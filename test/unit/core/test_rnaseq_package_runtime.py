"""Runtime-contract tests for the builtin STAR to DESeq2 package."""

from __future__ import annotations

import hashlib
import io
import json
import sys
import tarfile
from importlib import import_module
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from jarvis_cd.input_bundle import (
    INPUT_BUNDLE_MANIFEST_NAME,
    INPUT_BUNDLE_SCHEMA_VERSION,
)
from jarvis_cd.util.hostfile import Hostfile


def _load_package() -> ModuleType:
    repository_root = Path(__file__).resolve().parents[3] / "builtin"
    sys.path.insert(0, str(repository_root))
    try:
        return import_module("builtin.rna_seq_star_deseq2.pkg")
    finally:
        sys.path.remove(str(repository_root))


rnaseq_package = _load_package()


class _CapturedExec:
    commands: list[str] = []
    infos: list[Any] = []
    return_code = 0

    def __init__(self, command: str, exec_info: Any) -> None:
        self.command = command
        self.exec_info = exec_info
        self.exit_code = {"localhost": self.return_code}
        self.commands.append(command)
        self.infos.append(exec_info)

    def run(self) -> _CapturedExec:
        return self


class _CapturedMkdir:
    def __init__(self, path: str, exec_info: Any) -> None:
        self.path = path
        self.exec_info = exec_info
        self.exit_code = {"localhost": 0}

    def run(self) -> _CapturedMkdir:
        Path(self.path).mkdir(parents=True, exist_ok=True)
        return self


class _CapturedRm:
    calls: list[tuple[str, bool]] = []

    def __init__(self, path: str, exec_info: Any, *, recursive: bool = False) -> None:
        self.path = path
        self.exec_info = exec_info
        self.recursive = recursive
        self.exit_code = {"localhost": 0}

    def run(self) -> _CapturedRm:
        self.calls.append((self.path, self.recursive))
        return self


@pytest.fixture(autouse=True)
def _capture_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    _CapturedExec.commands = []
    _CapturedExec.infos = []
    _CapturedExec.return_code = 0
    _CapturedRm.calls = []
    monkeypatch.setattr(rnaseq_package, "Exec", _CapturedExec)
    monkeypatch.setattr(rnaseq_package, "Mkdir", _CapturedMkdir)
    monkeypatch.setattr(rnaseq_package, "Rm", _CapturedRm)


def _base_config(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "deploy_mode": "default",
        "input_bundle": "",
        "out": "run",
        "cores": 4,
        "nprocs": 1,
        "ppn": 1,
    }
    config.update(overrides)
    return config


def _package(tmp_path: Path, config: dict[str, Any]) -> Any:
    package = object.__new__(rnaseq_package.RnaSeqStarDeseq2)
    package.config = config
    package.shared_dir = tmp_path / "shared"
    package.private_dir = tmp_path / "private"
    package.env = {}
    package.mod_env = {"PATH": "/runtime/bin"}
    package.star_bin = "STAR"
    package.rscript_bin = "Rscript"
    package.pipeline = SimpleNamespace(
        get_hostfile=lambda: Hostfile(find_ips=False),
        _has_containerized_packages=lambda: False,
    )
    package.runtime_line_callback = lambda: None
    return package


def _write_bundle(
    destination: Path, *, annotation_role: str = "gene_annotation"
) -> Path:
    files = {
        "samples.tsv": (
            "sample\tcondition\tset_id\taccession\tfastq\n"
            "a1\ta\t1\tSRR1\treads/a1.fastq.gz\n"
            "a2\ta\t2\tSRR2\treads/a2.fastq.gz\n"
            "b1\tb\t1\tSRR3\treads/b1.fastq.gz\n"
            "b2\tb\t2\tSRR4\treads/b2.fastq.gz\n"
        ).encode(),
        "reference/genome.fa.gz": b"genome",
        "reference/genes.gtf.gz": b"annotation",
        "reads/a1.fastq.gz": b"read-a1",
        "reads/a2.fastq.gz": b"read-a2",
        "reads/b1.fastq.gz": b"read-b1",
        "reads/b2.fastq.gz": b"read-b2",
    }
    roles = {
        "samples.tsv": "sample_design",
        "reference/genome.fa.gz": "reference_genome",
        "reference/genes.gtf.gz": annotation_role,
    }
    manifest = {
        "schema_version": INPUT_BUNDLE_SCHEMA_VERSION,
        "entrypoint": "samples.tsv",
        "files": [
            {
                "path": name,
                "role": roles.get(name, "single_end_fastq"),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
            for name, payload in files.items()
        ],
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True).encode()
    with tarfile.open(destination, mode="w") as archive:
        info = tarfile.TarInfo(INPUT_BUNDLE_MANIFEST_NAME)
        info.size = len(manifest_bytes)
        archive.addfile(info, io.BytesIO(manifest_bytes))
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return destination


def test_agent_contract_exposes_native_study_and_hides_legacy_controls() -> None:
    package = object.__new__(rnaseq_package.RnaSeqStarDeseq2)
    package.config = _base_config()
    package.env = {"PATH": ""}
    package.mod_env = {"PATH": ""}

    menu = {item["name"]: item for item in package._configure_menu()}
    contract = package._deployment_contract().to_dict()

    assert menu["input_bundle"]["input_binding"] == {
        "schema_version": "jarvis.configuration-input-binding.v1",
        "kind": "local_file",
        "structure": "regular_file",
    }
    assert menu["out"]["default"] == "run"
    assert menu["cores"]["default"] == 4
    assert all(
        menu[name]["agent_visible"] is False
        for name in {
            "nprocs",
            "ppn",
            "replicates",
            "parallel_reps",
            "parallel_scratch_root",
            "omp_threads",
            "base_image",
        }
    )
    assert [profile["name"] for profile in contract["execution_profiles"]] == [
        "native_supplied_reads"
    ]
    assert [item["id"] for item in contract["runtime_requirements"]] == [
        "star",
        "deseq2",
    ]
    assert contract["execution_profiles"][0]["readiness"] == {
        "mechanism": "process_exit",
        "condition": "successful_exit_with_required_products",
    }


def test_bundle_is_verified_and_staged_before_native_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _write_bundle(tmp_path / "study.tar")
    source = bundle.read_bytes()
    package = _package(tmp_path, _base_config(input_bundle=str(bundle)))
    validated: list[Path] = []
    monkeypatch.setattr(
        rnaseq_package,
        "load_rnaseq_result",
        lambda path: validated.append(path),
    )

    package.start()

    root = tmp_path / "shared" / "run"
    assert (root / "input" / "samples.tsv").is_file()
    assert (root / "input" / "reads" / "a1.fastq.gz").read_bytes() == b"read-a1"
    assert bundle.read_bytes() == source
    assert validated == [(root / "results").resolve()]
    command = _CapturedExec.commands[-1]
    assert "native_runner.py" in command
    assert "--samples" in command
    assert "--genome" in command
    assert "--annotation" in command
    assert "--cores 4" in command
    assert _CapturedExec.infos[-1].cwd == str(root.resolve())


def test_bundle_roles_are_closed_before_launch(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path / "study.tar", annotation_role="support_file")
    package = _package(tmp_path, _base_config(input_bundle=str(bundle)))

    with pytest.raises(ValueError, match="unsupported or missing roles"):
        package.start()

    assert _CapturedExec.commands == []


@pytest.mark.parametrize("cores", [0, 65, True, "4"])
def test_native_configuration_rejects_invalid_core_counts(
    tmp_path: Path, cores: object
) -> None:
    package = _package(
        tmp_path,
        _base_config(input_bundle="study.tar", cores=cores),
    )

    with pytest.raises(ValueError, match="cores must be an integer"):
        package.start()


def test_native_configuration_requires_a_supplied_bundle(tmp_path: Path) -> None:
    package = _package(tmp_path, _base_config())

    with pytest.raises(ValueError, match="requires input_bundle"):
        package.start()


def test_stale_execution_tree_cannot_satisfy_completion(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path / "study.tar")
    package = _package(tmp_path, _base_config(input_bundle=str(bundle)))
    stale = tmp_path / "shared" / "run" / "results"
    stale.mkdir(parents=True)

    with pytest.raises(ValueError, match="target already exists: results"):
        package.start()

    assert _CapturedExec.commands == []


def test_native_process_failure_fails_package_lifecycle(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path / "study.tar")
    package = _package(tmp_path, _base_config(input_bundle=str(bundle)))
    _CapturedExec.return_code = 9

    with pytest.raises(RuntimeError, match="workflow failed"):
        package.start()


def test_clean_uses_one_exact_recursive_output_without_wildcard(tmp_path: Path) -> None:
    package = _package(tmp_path, _base_config(out="results"))

    package.clean()

    assert _CapturedRm.calls == [
        (str((tmp_path / "shared" / "results").resolve()), True)
    ]
    assert "*" not in _CapturedRm.calls[0][0]
