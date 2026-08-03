#!/usr/bin/env python3
"""Generate, translate, execute, and close one WfCommons workflow cell."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

RESULT_SCHEMA = "jarvis.wfcommons-result.v1"
RESULT_NAME = "wfcommons-result.json"
DEPENDENCY_LOCK_NAME = "dependency-lock.txt"
WORKFLOW_LOG_NAME = "workflow.log"
RECIPE_IMPORTS = {
    "montage": "MontageRecipe",
    "genome": "GenomeRecipe",
    "cycles": "CyclesRecipe",
    "blast": "BlastRecipe",
    "bwa": "BwaRecipe",
    "srasearch": "SrasearchRecipe",
    "epigenomics": "EpigenomicsRecipe",
    "seismology": "SeismologyRecipe",
    "soykb": "SoykbRecipe",
    "rnaseq": "RnaseqRecipe",
}


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of one regular file."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_file(run_root: Path, path: Path, *, label: str) -> str:
    """Return a confined relative path for one closed result member."""

    root = run_root.resolve(strict=True)
    resolved = path.resolve(strict=True)
    if (
        resolved.is_symlink()
        or not resolved.is_file()
        or not resolved.is_relative_to(root)
    ):
        raise ValueError(f"{label} is not a confined regular file")
    return resolved.relative_to(root).as_posix()


def workflow_identity(path: Path) -> tuple[int, int, str]:
    """Return observed tasks, directed edges, and a topology-only hash."""

    raw = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    try:
        tasks = raw["workflow"]["specification"]["tasks"]
    except (KeyError, TypeError) as exc:
        raise ValueError("WfCommons workflow omitted specification tasks") from exc
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("WfCommons workflow tasks must be a non-empty list")
    topology: list[dict[str, Any]] = []
    edge_count = 0
    for task in tasks:
        if not isinstance(task, dict):
            raise ValueError("WfCommons workflow task is malformed")
        task_id = task.get("id", task.get("name"))
        parents = task.get("parents", [])
        children = task.get("children", [])
        if (
            not isinstance(task_id, str)
            or not task_id
            or not isinstance(parents, list)
            or not isinstance(children, list)
        ):
            raise ValueError("WfCommons workflow topology is malformed")
        normalized_parents = sorted(str(value) for value in parents)
        normalized_children = sorted(str(value) for value in children)
        edge_count += len(normalized_children)
        topology.append(
            {
                "children": normalized_children,
                "id": task_id,
                "parents": normalized_parents,
            }
        )
    topology.sort(key=lambda item: item["id"])
    encoded = json.dumps(topology, sort_keys=True, separators=(",", ":")).encode()
    return len(tasks), edge_count, hashlib.sha256(encoded).hexdigest()


def write_dependency_lock(destination: Path) -> None:
    """Write a deterministic snapshot of the prepared Python environment."""

    packages = sorted(
        {
            f"{distribution.metadata['Name']}=={distribution.version}"
            for distribution in importlib.metadata.distributions()
            if distribution.metadata.get("Name")
        },
        key=str.casefold,
    )
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text("\n".join(packages) + "\n", encoding="utf-8")
    os.replace(temporary, destination)


def build_result_document(
    *,
    run_root: Path,
    recipe: str,
    requested_task_count: int,
    data_footprint_mb: int,
    seed: int,
    elapsed_seconds: float,
    return_code: int,
    workflow_path: Path,
    workflow_log: Path,
    dependency_lock: Path,
    schema_path: Path,
    wfcommons_version: str,
    python_version: str,
) -> dict[str, object]:
    """Build one closed, generic WfCommons result document."""

    observed_tasks, edge_count, topology_sha256 = workflow_identity(workflow_path)
    return {
        "schema_version": RESULT_SCHEMA,
        "recipe": recipe,
        "requested_task_count": requested_task_count,
        "observed_task_count": observed_tasks,
        "data_footprint_mb": data_footprint_mb,
        "seed": seed,
        "elapsed_seconds": elapsed_seconds,
        "return_code": return_code,
        "dag_edge_count": edge_count,
        "topology_sha256": topology_sha256,
        "workflow_manifest": _relative_file(
            run_root, workflow_path, label="workflow manifest"
        ),
        "workflow_sha256": sha256_file(workflow_path),
        "workflow_log": _relative_file(run_root, workflow_log, label="workflow log"),
        "workflow_log_sha256": sha256_file(workflow_log),
        "dependency_lock": _relative_file(
            run_root, dependency_lock, label="dependency lock"
        ),
        "dependency_lock_sha256": sha256_file(dependency_lock),
        "schema_file": _relative_file(run_root, schema_path, label="WfFormat schema"),
        "schema_sha256": sha256_file(schema_path),
        "wfcommons_version": wfcommons_version,
        "python_version": python_version,
    }


def write_result(destination: Path, document: dict[str, object]) -> None:
    """Atomically publish one completed result document."""

    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, destination)


def load_recipe(name: str) -> type[Any]:
    """Resolve one explicitly supported WfCommons recipe class."""

    import wfcommons  # pyright: ignore[reportMissingImports]

    attribute = RECIPE_IMPORTS.get(name.casefold())
    if attribute is None or not hasattr(wfcommons, attribute):
        raise RuntimeError(
            f"unknown recipe {name!r}; choices: {sorted(RECIPE_IMPORTS)}"
        )
    return getattr(wfcommons, attribute)


def _rewrite_clio_paths(workflow_path: Path, runner: Path) -> int:
    """Prefix only manifest-declared workflow data paths with ``clio::``."""

    workflow = json.loads(workflow_path.read_text(encoding="utf-8", errors="strict"))
    tasks = workflow["workflow"]["specification"]["tasks"]
    paths: set[str] = set()
    for task in tasks:
        for key in ("inputFiles", "input_files", "outputFiles", "output_files"):
            for value in task.get(key, []) or []:
                if isinstance(value, dict) and isinstance(value.get("name"), str):
                    paths.add(value["name"])
                elif isinstance(value, str):
                    paths.add(value)
    candidates = sorted(
        {f"data/{path}" for path in paths} | paths, key=len, reverse=True
    )
    text = runner.read_text(encoding="utf-8", errors="strict")
    rewritten = 0
    for raw in candidates:
        pattern = re.compile(r'(\\")' + re.escape(raw) + r'(\\")')
        text, count = pattern.subn(r"\1clio::" + raw + r"\2", text)
        rewritten += count
    runner.write_text(text, encoding="utf-8")
    return rewritten


def _parse_arguments() -> argparse.Namespace:
    """Parse the bounded one-cell driver interface."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recipe", choices=sorted(RECIPE_IMPORTS), required=True)
    parser.add_argument("--num-tasks", type=int, required=True)
    parser.add_argument("--cpu-work", type=int, required=True)
    parser.add_argument("--data-footprint-mb", type=int, required=True)
    parser.add_argument("--percent-cpu", type=float, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--schema-file", type=Path, required=True)
    parser.add_argument("--expected-wfcommons-version", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--clio-prefix", action="store_true")
    return parser.parse_args()


def main() -> int:
    """Execute one workflow cell and return its workflow process status."""

    arguments = _parse_arguments()
    run_root = arguments.out.resolve(strict=True)
    schema_path = arguments.schema_file.resolve(strict=True)
    if schema_path.parent != run_root or schema_path.name != "wfcommons-schema.json":
        raise ValueError("schema file must be the JARVIS-staged output member")

    import numpy as np  # pyright: ignore[reportMissingImports]
    import wfcommons  # pyright: ignore[reportMissingImports]
    from wfcommons.wfbench import (  # pyright: ignore[reportMissingImports]
        BashTranslator,
        WorkflowBenchmark,
    )

    version = str(getattr(wfcommons, "__version__", ""))
    if version != arguments.expected_wfcommons_version:
        raise RuntimeError(
            f"WfCommons version differs: expected {arguments.expected_wfcommons_version}, got {version or 'unknown'}"
        )
    random.seed(arguments.seed)
    np.random.seed(arguments.seed)
    write_dependency_lock(run_root / DEPENDENCY_LOCK_NAME)

    benchmark_root = run_root / "benchmark"
    recipe = load_recipe(arguments.recipe)
    generator = WorkflowBenchmark(recipe=recipe, num_tasks=arguments.num_tasks)
    create_arguments: dict[str, object] = {
        "save_dir": benchmark_root,
        "cpu_work": arguments.cpu_work,
        "percent_cpu": arguments.percent_cpu,
    }
    if arguments.data_footprint_mb > 0:
        create_arguments["data"] = arguments.data_footprint_mb
    workflow_path = Path(generator.create_benchmark(**create_arguments)).resolve(
        strict=True
    )
    bash_root = run_root / "bash"
    if bash_root.exists():
        raise ValueError("WfCommons translator output already exists")
    BashTranslator(workflow=workflow_path).translate(output_folder=bash_root)
    runner = bash_root / "run_workflow.sh"
    if runner.is_symlink() or not runner.is_file():
        raise RuntimeError("WfCommons translator omitted run_workflow.sh")
    if arguments.clio_prefix:
        rewritten = _rewrite_clio_paths(workflow_path, runner)
        print(f"[wfcommons] clio-prefixed path occurrences={rewritten}", flush=True)

    environment = dict(os.environ)
    environment["PATH"] = f"{Path(sys.executable).parent}:{environment.get('PATH', '')}"
    log_path = run_root / WORKFLOW_LOG_NAME
    started = time.perf_counter()
    with log_path.open("wb") as log:
        completed = subprocess.run(
            ["bash", runner.name],
            cwd=bash_root,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
    elapsed = time.perf_counter() - started
    document = build_result_document(
        run_root=run_root,
        recipe=arguments.recipe,
        requested_task_count=arguments.num_tasks,
        data_footprint_mb=arguments.data_footprint_mb,
        seed=arguments.seed,
        elapsed_seconds=elapsed,
        return_code=completed.returncode,
        workflow_path=workflow_path,
        workflow_log=log_path,
        dependency_lock=run_root / DEPENDENCY_LOCK_NAME,
        schema_path=schema_path,
        wfcommons_version=version,
        python_version=sys.version.split()[0],
    )
    write_result(run_root / RESULT_NAME, document)
    print(
        "WFCOMMONS_RESULT "
        f"schema={RESULT_SCHEMA} recipe={arguments.recipe} "
        f"requested_tasks={arguments.num_tasks} "
        f"observed_tasks={document['observed_task_count']} "
        f"footprint_mb={arguments.data_footprint_mb} "
        f"elapsed_s={elapsed:.9g} return_code={completed.returncode}",
        flush=True,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
