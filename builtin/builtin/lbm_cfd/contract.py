"""Pure source, execution-directory, and result contract for LBM-CFD."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from jarvis_cd.input_bundle import InputBundleError, MaterializedInputBundle

LATTICES = ("d3q15", "d3q19", "d3q27")
DIMENSIONS = (64, 32, 32)
TIME_STEPS = 501
FINAL_STEP = 500
RESULT_NAME = "lbm-stencil-result.json"
RESULT_SCHEMA = "scientific-benchmark.lbm-stencil-result.v1"
RESULT_PREFIX = "BENCHMARK_LBM "
EXPECTED_FILES = {
    "COPYING",
    "Makefile",
    "README.md",
    "include/lbm_mpi.hpp",
    "include/paraview_sim.hpp",
    "src/main.cpp",
}


@dataclass(frozen=True, slots=True)
class VorticityStatistics:
    """Validated field statistics for one LBM lattice stencil."""

    lattice: str
    points: int
    minimum: float
    maximum: float
    mean: float
    rms: float
    nonzero_points: int
    field_sha256: str
    source: str


def validate_lbm_bundle(bundle: MaterializedInputBundle) -> None:
    """Require the exact pinned, license-bearing LBM-CFD source tree."""

    observed = {item.path for item in bundle.manifest.files}
    if observed != EXPECTED_FILES:
        raise InputBundleError("LBM-CFD bundle does not contain the closed source tree")
    by_path = {item.path: item.role for item in bundle.manifest.files}
    if by_path.get("COPYING") != "license" or by_path.get("Makefile") != "build_recipe":
        raise InputBundleError("LBM-CFD bundle omitted its license or build contract")
    if any(
        by_path.get(path) != "application_source"
        for path in ("include/lbm_mpi.hpp", "include/paraview_sim.hpp", "src/main.cpp")
    ):
        raise InputBundleError("LBM-CFD bundle source roles are invalid")
    if bundle.manifest.entrypoint != "Makefile":
        raise InputBundleError("LBM-CFD bundle entrypoint is not its build recipe")


def _safe_execution_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or any(
            character
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for character in value
        )
    ):
        raise InputBundleError("JARVIS execution identity is not a safe path token")
    return value


def materialize_run(
    bundle: MaterializedInputBundle, run_parent: Path, execution_id: object
) -> Path:
    """Copy immutable LBM-CFD sources into a new execution-specific directory."""

    validate_lbm_bundle(bundle)
    token = _safe_execution_id(execution_id)
    run_parent = run_parent.resolve()
    run_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = run_parent / token
    if destination.exists():
        raise InputBundleError("LBM-CFD execution working directory already exists")
    temporary = Path(tempfile.mkdtemp(prefix=f".{token}.", dir=run_parent))
    try:
        for item in bundle.manifest.files:
            source = bundle.root / PurePosixPath(item.path)
            target = temporary / PurePosixPath(item.path)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(source, target, follow_symlinks=False)
            os.chmod(target, 0o600)
        (temporary / "input-provenance.json").write_text(
            json.dumps(
                {
                    "bundle_sha256": bundle.bundle_sha256,
                    "dimensions": list(DIMENSIONS),
                    "lattices": list(LATTICES),
                    "schema_version": "scientific-benchmark.lbm-stencil-run.v1",
                    "time_steps": TIME_STEPS,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary / "input-provenance.json", 0o600)
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return destination


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def parse_vorticity_field(path: Path, *, lattice: str) -> VorticityStatistics:
    """Parse and validate the bounded ASCII VTK vorticity field."""

    if lattice not in LATTICES:
        raise ValueError(f"unsupported LBM lattice: {lattice}")
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"LBM-CFD field is missing or unsafe: {path.name}")
    size = path.stat().st_size
    if size < 1024 or size > 64 * 1024 * 1024:
        raise ValueError(f"LBM-CFD field size is outside its bound: {path.name}")
    prefix = path.read_bytes()[:256]
    if b"<!DOCTYPE" in prefix.upper() or b"<VTKFile" not in prefix:
        raise ValueError("LBM-CFD field is not a closed VTK document")
    root = ET.parse(path).getroot()
    grid = root.find("StructuredGrid")
    if grid is None:
        raise ValueError("LBM-CFD field omitted StructuredGrid")
    expected_extent = (
        f"0 {DIMENSIONS[0] - 1} 0 {DIMENSIONS[1] - 1} 0 {DIMENSIONS[2] - 1}"
    )
    if grid.get("WholeExtent") != expected_extent:
        raise ValueError("LBM-CFD field dimensions differ from the benchmark")
    data = root.find('.//PointData/DataArray[@Name="vorticity"]')
    if data is None or data.get("format") != "ascii" or data.text is None:
        raise ValueError("LBM-CFD field omitted its ASCII vorticity values")
    try:
        values = [float(token) for token in data.text.split()]
    except ValueError as error:
        raise ValueError("LBM-CFD vorticity contains malformed values") from error
    expected_points = math.prod(DIMENSIONS)
    if len(values) != expected_points or not all(
        math.isfinite(value) for value in values
    ):
        raise ValueError("LBM-CFD vorticity is incomplete or non-finite")
    nonzero = sum(value != 0.0 for value in values)
    rms = math.sqrt(sum(value * value for value in values) / len(values))
    if nonzero < expected_points // 100 or rms <= 0:
        raise ValueError("LBM-CFD vorticity field is scientifically empty")
    return VorticityStatistics(
        lattice=lattice,
        points=len(values),
        minimum=min(values),
        maximum=max(values),
        mean=sum(values) / len(values),
        rms=rms,
        nonzero_points=nonzero,
        field_sha256=_sha256(path),
        source=path.name,
    )


def _result_document(
    run_root: Path, bundle: MaterializedInputBundle
) -> dict[str, object]:
    cases = [
        parse_vorticity_field(
            run_root / lattice / f"simulation_state_t{FINAL_STEP:05d}.vts",
            lattice=lattice,
        )
        for lattice in LATTICES
    ]
    if len({case.field_sha256 for case in cases}) != len(LATTICES):
        raise ValueError("LBM-CFD lattice stencils produced indistinguishable fields")
    baseline = next(case.rms for case in cases if case.lattice == "d3q19")
    return {
        "cases": [
            {**asdict(case), "rms_ratio_to_d3q19": case.rms / baseline}
            for case in cases
        ],
        "dimensions": list(DIMENSIONS),
        "input_bundle_sha256": bundle.bundle_sha256,
        "schema_version": RESULT_SCHEMA,
        "time_steps": TIME_STEPS,
    }


def write_lbm_result(
    run_root: Path, destination: Path, bundle: MaterializedInputBundle
) -> dict[str, object]:
    """Validate the three fields and atomically write their comparison."""

    validate_lbm_bundle(bundle)
    document = _result_document(run_root, bundle)
    if destination.exists():
        raise ValueError("LBM-CFD result destination already exists")
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return document


def validate_result_document(
    run_root: Path, result_path: Path, bundle: MaterializedInputBundle
) -> dict[str, object]:
    """Require the result document to exactly match all generated fields."""

    if result_path.is_symlink() or not result_path.is_file():
        raise ValueError("LBM-CFD result document is missing or unsafe")
    observed = json.loads(result_path.read_text(encoding="utf-8", errors="strict"))
    expected = _result_document(run_root, bundle)
    if observed != expected:
        raise ValueError("LBM-CFD result document does not match generated fields")
    return expected


def result_summary_line(document: dict[str, object]) -> str:
    """Render the closed terminal summary consumed by JARVIS progress."""

    cases = document.get("cases")
    if not isinstance(cases, list) or len(cases) != len(LATTICES):
        raise ValueError("LBM-CFD result cases are missing")
    summaries: list[str] = []
    for case in cases:
        lattice = case.get("lattice") if isinstance(case, dict) else None
        if lattice not in LATTICES:
            raise ValueError("LBM-CFD result contains a malformed lattice case")
        measurements = {
            "mean": case.get("mean"),
            "rms": case.get("rms"),
            "maximum": case.get("maximum"),
            "rms_ratio_to_d3q19": case.get("rms_ratio_to_d3q19"),
        }
        if any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            for value in measurements.values()
        ):
            raise ValueError("LBM-CFD result contains malformed summary measurements")
        summaries.append(
            f"lattice={str(lattice).upper()},mean={measurements['mean']:.9g},"
            f"rms={measurements['rms']:.9g},maximum={measurements['maximum']:.9g},"
            f"rms_ratio_to_d3q19={measurements['rms_ratio_to_d3q19']:.9g}"
        )
    return (
        f"{RESULT_PREFIX}schema={RESULT_SCHEMA} lattices=D3Q15,D3Q19,D3Q27 "
        f"dimensions={DIMENSIONS[0]}x{DIMENSIONS[1]}x{DIMENSIONS[2]} step={FINAL_STEP} "
        f"cases={'|'.join(summaries)}"
    )
